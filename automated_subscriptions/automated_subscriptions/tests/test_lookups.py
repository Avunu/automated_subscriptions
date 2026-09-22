"""Invoice <-> subscription lookups (PR 4, DECISIONS.md D-10): header link OR line link, credit notes excluded,
`Credit Note Issued` counts as paid, the current invoice is the one carrying this sub's latest line period,
and the Sales Invoice mixin fans the status refresh out over every subscription an invoice bills.

The "consolidated" invoice is built by hand (A's own draft plus B's line) so this module does not depend on
the PR 3 runner."""

from unittest.mock import patch

import frappe
from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry
from erpnext.accounts.doctype.sales_invoice.sales_invoice import make_sales_return
from erpnext.accounts.doctype.subscription.subscription import Subscription as BaseSubscription
from erpnext.accounts.doctype.subscription.test_subscription import (
	create_parties,
	create_plan,
	create_subscription,
	make_plans,
	reset_settings,
)
from frappe.tests import IntegrationTestCase
from frappe.utils import add_months, add_to_date, getdate, nowdate

from automated_subscriptions.automated_subscriptions.tests.utils import frozen_today

CUSTOMER = "_Test Customer"
COMPANY = "_Test Company"
ITEM = "_Test Non Stock Item"
MONTH_PLAN = "_Test Plan Name"  # 900 / Month
QUARTER_PLAN = "_Test Plan Name 4"  # 20000 / 3 Months
YEAR_PLAN = "_Test AS Year Plan"
BEGINNING = "Beginning of the current subscription period"


def invoices_of(sub_name):
	return frappe.get_all(
		"Sales Invoice", {"subscription": sub_name, "docstatus": ("<", 2)}, pluck="name", order_by="creation"
	)


class TestLookups(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		make_plans()
		create_parties()
		reset_settings()
		create_plan(plan_name=YEAR_PLAN, cost=12000, billing_interval="Year", currency="INR")
		frappe.db.set_value("Company", COMPANY, "accounts_frozen_till_date", None)
		frappe.db.set_value("Customer", CUSTOMER, "consolidate_subscription_invoices", 0)
		frappe.clear_document_cache("Customer", CUSTOMER)
		frappe.db.set_single_value("Selling Settings", "allow_multiple_items", 1)
		frappe.flags.subscription_billing = None
		self.today = nowdate()

	def tearDown(self):
		frappe.flags.in_import = False
		frappe.db.rollback()
		super().tearDown()

	# ---- fixtures
	def make_sub(self, plan=MONTH_PLAN, start_date=None, in_import=True, **kw):
		"""submit_invoice = 0 and days_until_due = 0 (due = posting, no terms) unless overridden. With
		in_import the insert bills nothing (core after_insert guard); otherwise after_insert creates the
		period's draft invoice and advances the period, exactly like a desk insert."""
		kwargs = {
			"party": CUSTOMER,
			"start_date": start_date or self.today,
			"plans": [{"plan": plan, "qty": 1}],
			"generate_invoice_at": BEGINNING,
			"submit_invoice": 0,
			"days_until_due": 0,
			"do_not_save": True,
		}
		kwargs.update(kw)
		sub = create_subscription(**kwargs)
		frappe.flags.in_import = in_import
		try:
			sub.insert()
		finally:
			frappe.flags.in_import = False
		return frappe.get_doc("Subscription", sub.name)

	def line_for(self, sub, plan, rate):
		return {
			"item_code": ITEM,
			"qty": 1,
			"rate": rate,
			"subscription": sub.name,
			"subscription_plan": plan,
			"subscription_period_start": sub.current_invoice_start,
			"subscription_period_end": sub.current_invoice_end,
		}

	def consolidate(self, a, b, plan_b, rate_b, submit=True):
		"""A's own draft (created by after_insert) plus B's line, header period widened to cover both."""
		names = invoices_of(a.name)
		self.assertEqual(len(names), 1, names)
		invoice = frappe.get_doc("Sales Invoice", names[0])
		self.assertEqual(invoice.docstatus, 0)
		invoice.append("items", self.line_for(b, plan_b, rate_b))
		invoice.to_date = max(getdate(invoice.to_date), getdate(b.current_invoice_end))
		invoice.save()
		if submit:
			invoice.submit()
		return invoice

	def consolidated_pair(self):
		"""A (monthly, billed by after_insert) and B (quarterly, never billed on its own) on one submitted
		invoice; the invoice's due date is its posting date (days_until_due 0, no payment terms)."""
		a = self.make_sub(MONTH_PLAN, in_import=False)
		b = self.make_sub(QUARTER_PLAN)
		invoice = self.consolidate(a, b, QUARTER_PLAN, 20000)
		self.assertEqual([d.subscription for d in invoice.items], [a.name, b.name])
		self.assertEqual(getdate(invoice.due_date), getdate(invoice.posting_date))
		return a, b, invoice

	def statuses(self, *subs):
		return [frappe.db.get_value("Subscription", sub.name, "status") for sub in subs]

	# ---- 1
	def test_payment_driven_status_on_consolidated_invoice(self):
		a, b, invoice = self.consolidated_pair()
		self.assertEqual(type(frappe.get_doc("Sales Invoice", invoice.name)).__name__, "ExtendedSalesInvoice")

		frappe.get_doc("Sales Invoice", invoice.name).refresh_subscription_status()
		self.assertEqual(self.statuses(a, b), ["Unpaid", "Unpaid"])

		payment = get_payment_entry("Sales Invoice", invoice.name, bank_account="_Test Bank - _TC")
		payment.reference_no = "12345"
		payment.reference_date = self.today
		payment.submit()
		self.assertEqual(frappe.db.get_value("Sales Invoice", invoice.name, "status"), "Paid")
		self.assertEqual(self.statuses(a, b), ["Active", "Active"])

		payment.cancel()
		self.assertNotEqual(frappe.db.get_value("Sales Invoice", invoice.name, "status"), "Paid")
		for status in self.statuses(a, b):
			self.assertIn(status, ("Unpaid", "Grace Period"))

	# ---- 2
	def issue_credit_note(self, invoice):
		"""A submitted return for the first line only (core validate_returned_items keys on the source row)."""
		credit_note = make_sales_return(invoice.name)
		credit_note.set("items", [credit_note.items[0]])
		credit_note.set("payment_schedule", [])
		credit_note.insert()
		credit_note.submit()
		self.assertEqual(credit_note.is_return, 1)
		self.assertEqual(credit_note.return_against, invoice.name)
		return credit_note

	def test_credit_note_excluded(self):
		a, b, invoice = self.consolidated_pair()
		credit_note = self.issue_credit_note(invoice)
		self.assertEqual(credit_note.items[0].subscription, a.name)  # links copy onto the return (no_copy 0)

		invoice.db_set("outstanding_amount", 0)
		invoice.db_set("status", "Paid")
		invoice.reload()
		self.assertEqual(a.has_outstanding_invoice(), 0)
		self.assertEqual(a.get_current_invoice().name, invoice.name)
		self.assertNotIn(credit_note.name, [d.name for d in a.invoices])
		self.assertEqual([d.name for d in a.invoices], [invoice.name])
		self.assertTrue(a.is_paid(invoice))
		self.assertEqual(frappe.db.count("Payment Request", {"reference_name": credit_note.name}), 0)
		# control: core counts the return itself (header link copied, status Return != Paid)
		self.assertEqual(BaseSubscription.has_outstanding_invoice(a), 1)
		self.assertIn(credit_note.name, [d.name for d in BaseSubscription.invoices.fget(a)])
		# the line-linked member sees the same
		self.assertEqual(b.has_outstanding_invoice(), 0)
		self.assertEqual(b.get_current_invoice().name, invoice.name)
		self.assertNotIn(credit_note.name, [d.name for d in b.invoices])

	# ---- 3
	def test_credit_note_issued_counts_as_paid(self):
		a, b, invoice = self.consolidated_pair()
		self.issue_credit_note(invoice)
		invoice.db_set("outstanding_amount", 0)
		invoice.db_set("status", "Credit Note Issued")
		for sub in (a, b):
			self.assertEqual(sub.has_outstanding_invoice(), 0)
			self.assertFalse(sub.current_invoice_is_past_due())
			self.assertTrue(sub.is_paid(sub.get_current_invoice()))
		# control: core's rule counts both the original (status != Paid) and the return, and is_paid is False
		self.assertEqual(BaseSubscription.has_outstanding_invoice(a), 2)
		self.assertFalse(BaseSubscription.is_paid(invoice))

	# ---- 4
	def test_line_correct_current_invoice(self):
		with frozen_today("2026-01-01"):
			m = self.make_sub(
				MONTH_PLAN, "2026-01-01", in_import=False, generate_new_invoices_past_due_date=1
			)
			y = self.make_sub(YEAR_PLAN, "2026-01-01")
			january = self.consolidate(m, y, YEAR_PLAN, 12000)
		self.assertEqual(getdate(january.to_date), getdate("2026-12-31"))
		self.assertEqual(getdate(january.from_date), getdate("2026-01-01"))
		self.assertEqual(getdate(m.current_invoice_start), getdate("2026-02-01"))

		with frozen_today("2026-02-01"):
			m.process(posting_date="2026-02-01")
		names = invoices_of(m.name)
		self.assertEqual(len(names), 2)
		february = frappe.get_doc("Sales Invoice", next(n for n in names if n != january.name))
		line = february.items[0]
		self.assertEqual(line.subscription, m.name)
		self.assertEqual(
			(getdate(line.subscription_period_start), getdate(line.subscription_period_end)),
			(getdate("2026-02-01"), getdate("2026-02-28")),
		)
		self.assertLess(getdate(february.to_date), getdate(january.to_date))  # core would rank January first

		m.reload()
		self.assertEqual(m.get_current_invoice().name, february.name)
		self.assertEqual(y.get_current_invoice().name, january.name)
		self.assertTrue(m.is_current_invoice_generated("2026-02-01", "2026-02-28"))
		self.assertFalse(m.is_current_invoice_generated("2026-03-01", "2026-03-31"))
		self.assertEqual([d.name for d in m.invoices], [january.name, february.name])
		self.assertEqual([str(d.period_end) for d in m.invoices], ["2026-01-31", "2026-02-28"])
		self.assertEqual([d.name for d in y.invoices], [january.name])
		self.assertEqual(str(y.invoices[0].period_end), "2026-12-31")

		# control: without line links the header to_date decides, i.e. core's ordering
		for row in frappe.get_all("Sales Invoice Item", {"subscription": m.name}, pluck="name"):
			frappe.db.set_value("Sales Invoice Item", row, "subscription", None)
		self.assertEqual(m.get_current_invoice().name, january.name)
		self.assertEqual(BaseSubscription.get_current_invoice(m).name, january.name)

	# ---- 5
	def test_stock_parity_and_supplier_passthrough(self):
		sub = self.make_sub(
			MONTH_PLAN,
			add_months(self.today, -2),
			in_import=False,
			submit_invoice=1,
			generate_new_invoices_past_due_date=1,
		)
		names = invoices_of(sub.name)
		self.assertEqual(len(names), 3)  # the catch-up loop billed every elapsed period
		self.assertEqual(
			sub.get_current_invoice().name,
			frappe.get_all(
				"Sales Invoice",
				{"subscription": sub.name, "docstatus": ("<", 2)},
				limit=1,
				order_by="to_date desc",
				pluck="name",
			)[0],
		)
		self.assertEqual(
			[d.name for d in sub.invoices],
			[
				d.name
				for d in frappe.get_all(
					"Sales Invoice", filters={"subscription": sub.name}, order_by="from_date asc"
				)
			],
		)
		self.assertEqual(
			sub.has_outstanding_invoice(),
			frappe.db.count(
				"Sales Invoice", {"subscription": sub.name, "docstatus": 1, "status": ["!=", "Paid"]}
			),
		)
		self.assertEqual(sub.has_outstanding_invoice(), 3)
		self.assertEqual(
			sub.is_current_invoice_generated(sub.current_invoice_start, sub.current_invoice_end),
			BaseSubscription.is_current_invoice_generated(
				sub, sub.current_invoice_start, sub.current_invoice_end
			),
		)

		supplier = self.make_sub(MONTH_PLAN, party_type="Supplier", party="_Test Supplier")
		originals = {
			name: getattr(BaseSubscription, name)
			for name in ("get_current_invoice", "has_outstanding_invoice", "is_current_invoice_generated")
		}
		with (
			patch.object(
				BaseSubscription,
				"get_current_invoice",
				autospec=True,
				side_effect=originals["get_current_invoice"],
			) as current,
			patch.object(
				BaseSubscription,
				"has_outstanding_invoice",
				autospec=True,
				side_effect=originals["has_outstanding_invoice"],
			) as outstanding,
			patch.object(
				BaseSubscription,
				"is_current_invoice_generated",
				autospec=True,
				side_effect=originals["is_current_invoice_generated"],
			) as generated,
		):
			self.assertIsNone(supplier.get_current_invoice())
			self.assertEqual(supplier.invoices, [])
			self.assertEqual(supplier.has_outstanding_invoice(), 0)
			self.assertFalse(
				supplier.is_current_invoice_generated(
					supplier.current_invoice_start, supplier.current_invoice_end
				)
			)
		# get_current_invoice: once directly, once through core's is_current_invoice_generated
		self.assertEqual(current.call_count, 2)
		outstanding.assert_called_once()
		generated.assert_called_once()

	# ---- 6
	def test_is_current_invoice_generated_legacy_fallback(self):
		sub = self.make_sub(MONTH_PLAN, in_import=False)
		invoice = frappe.get_doc("Sales Invoice", invoices_of(sub.name)[0])
		billed = (getdate(self.today), getdate(add_to_date(self.today, months=1, days=-1)))
		following = (getdate(sub.current_invoice_start), getdate(sub.current_invoice_end))
		self.assertEqual(getdate(invoice.posting_date), billed[0])

		# legacy lines (no period): core's posting-date rule
		for row in invoice.items:
			frappe.db.set_value(
				"Sales Invoice Item",
				row.name,
				{"subscription_period_start": None, "subscription_period_end": None},
			)
		for window in (billed, following):
			self.assertEqual(
				sub.is_current_invoice_generated(*window),
				BaseSubscription.is_current_invoice_generated(sub, *window),
			)
		self.assertTrue(sub.is_current_invoice_generated(*billed))
		self.assertFalse(sub.is_current_invoice_generated(*following))

		# a line period outside the window wins over a posting date inside it
		frappe.db.set_value(
			"Sales Invoice Item",
			invoice.items[0].name,
			{
				"subscription_period_start": add_months(self.today, -1),
				"subscription_period_end": add_to_date(self.today, days=-1),
			},
		)
		self.assertFalse(sub.is_current_invoice_generated(*billed))
		self.assertTrue(BaseSubscription.is_current_invoice_generated(sub, *billed))

	# ---- 7
	def test_on_cancel_refreshes_all_subscriptions(self):
		a, b, invoice = self.consolidated_pair()
		frappe.get_doc("Sales Invoice", invoice.name).refresh_subscription_status()
		self.assertEqual(self.statuses(a, b), ["Unpaid", "Unpaid"])
		modified = [frappe.db.get_value("Subscription", s.name, "modified") for s in (a, b)]

		invoice.reload()
		invoice.cancel()

		self.assertEqual(frappe.db.get_value("Sales Invoice", invoice.name, "docstatus"), 2)
		for sub in (a, b):
			sub.reload()
			self.assertEqual(sub.has_outstanding_invoice(), 0)
			self.assertIsNone(sub.get_current_invoice())
			self.assertEqual(sub.status, "Active")
		self.assertNotEqual(
			[frappe.db.get_value("Subscription", s.name, "modified") for s in (a, b)], modified
		)
