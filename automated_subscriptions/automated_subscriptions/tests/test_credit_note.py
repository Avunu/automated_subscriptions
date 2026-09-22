"""Mid-term cancellation with a Credit Note (PR 5, DECISIONS.md D-12).

Subscriptions are billed through the PR 3 runner so every January line carries its own link and period; the
customer is anchored (Anniversary, Jan 1) and consolidating, which is what routes the desk button to the
credit-note path."""

import frappe
from erpnext.accounts.doctype.subscription.subscription import InvoiceCancelled
from erpnext.accounts.doctype.subscription.test_subscription import (
	create_parties,
	create_plan,
	create_subscription,
	make_plans,
	reset_settings,
)
from frappe.tests import IntegrationTestCase
from frappe.utils import getdate

from automated_subscriptions.automated_subscriptions.billing.credit_note import cancel_with_credit_note
from automated_subscriptions.automated_subscriptions.billing.exceptions import (
	CreditNoteAlreadyIssued,
	SubscriptionBillingError,
)
from automated_subscriptions.automated_subscriptions.billing.runner import run_consolidated_billing
from automated_subscriptions.automated_subscriptions.tests.test_payment_request import (
	make_gateway_account,
	set_plan_gateway,
)
from automated_subscriptions.automated_subscriptions.tests.utils import frozen_today, make_anchored_customer

COMPANY = "_Test Company"
CUSTOMER = "_Test AS CN Customer"
STOCK_CUSTOMER = "_Test Customer"
PLAN_31 = "_Test AS Plan 31"
PLAN_62 = "_Test AS Plan 62"
PLAN_93 = "_Test AS Plan 93"
PLAN_10 = "_Test AS Plan 10"
BEGINNING = "Beginning of the current subscription period"
JAN_1 = "2018-01-01"


def credit_notes_against(invoice):
	return frappe.get_all(
		"Sales Invoice",
		{"is_return": 1, "return_against": invoice, "docstatus": ("<", 2)},
		pluck="name",
		order_by="creation",
	)


def invoices_for(customer):
	return frappe.get_all(
		"Sales Invoice",
		{"customer": customer, "is_return": 0, "docstatus": 1},
		pluck="name",
		order_by="posting_date, creation",
	)


class TestCreditNote(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		make_plans()
		create_parties()
		reset_settings()
		for plan, cost in ((PLAN_31, 31), (PLAN_62, 62), (PLAN_93, 93), (PLAN_10, 10)):
			create_plan(plan_name=plan, cost=cost, currency="INR")
		make_anchored_customer(CUSTOMER, "Anniversary", JAN_1, consolidate=1)
		frappe.db.set_value("Customer", STOCK_CUSTOMER, "consolidate_subscription_invoices", 0)
		frappe.clear_document_cache("Customer", STOCK_CUSTOMER)
		frappe.db.set_value("Company", COMPANY, "accounts_frozen_till_date", None)
		frappe.db.set_single_value("Selling Settings", "allow_multiple_items", 1)
		frappe.db.set_single_value("Subscription Settings", "credit_note_on_cancellation", 1)
		frappe.db.set_single_value("Subscription Settings", "mid_term_billing_mode", "Immediate")
		frappe.local.consolidated_billing = None
		frappe.flags.subscription_billing = None

	def tearDown(self):
		frappe.flags.in_import = False
		frappe.flags.selected_children = None
		frappe.set_user("Administrator")
		frappe.db.rollback()
		super().tearDown()

	# ---- fixtures
	def make_sub(self, plan=PLAN_31, qty=1, party=CUSTOMER, start_date=JAN_1):
		with frozen_today(start_date):
			sub = create_subscription(
				party=party,
				start_date=start_date,
				generate_invoice_at=BEGINNING,
				submit_invoice=1,
				days_until_due=15,
				plans=[{"plan": plan, "qty": qty}],
				do_not_save=True,
			)
			frappe.flags.in_import = True
			try:
				sub.insert()
			finally:
				frappe.flags.in_import = False
		return frappe.get_doc("Subscription", sub.name)

	def bill(self, posting_date=JAN_1, party=CUSTOMER):
		with frozen_today(posting_date):
			run_consolidated_billing(posting_date, party=party)
		names = invoices_for(party)
		return frappe.get_doc("Sales Invoice", names[-1])

	def billed_sub(self, plan=PLAN_31, qty=1):
		sub = self.make_sub(plan, qty)
		invoice = self.bill()
		self.assertEqual([d.subscription for d in invoice.items], [sub.name])
		self.assertEqual(
			(
				getdate(invoice.items[0].subscription_period_start),
				getdate(invoice.items[0].subscription_period_end),
			),
			(getdate("2018-01-01"), getdate("2018-01-31")),
		)
		return sub, invoice

	@staticmethod
	def pay(invoice):
		invoice.db_set("outstanding_amount", 0)
		invoice.db_set("status", "Paid")

	def cancel(self, sub, cancel_date, **kw):
		with frozen_today(cancel_date):
			return cancel_with_credit_note(sub.name, cancel_date, **kw)

	def single_credit_note(self, invoice):
		names = credit_notes_against(invoice.name)
		self.assertEqual(len(names), 1, names)
		return frappe.get_doc("Sales Invoice", names[0])

	# ---- 1
	def test_half_month_credit(self):
		sub, invoice = self.billed_sub()
		self.pay(invoice)

		result = self.cancel(sub, "2018-01-16")

		cn = self.single_credit_note(invoice)
		self.assertEqual(result["credit_notes"], [cn.name])
		self.assertEqual(result["credited"], 15.0)
		self.assertEqual(cn.docstatus, 1)
		self.assertEqual(cn.is_return, 1)
		self.assertEqual(cn.return_against, invoice.name)
		self.assertEqual(getdate(cn.posting_date), getdate("2018-01-16"))
		self.assertEqual(cn.status, "Return")
		self.assertEqual(cn.update_outstanding_for_self, 1)
		self.assertEqual(cn.outstanding_amount, -15.0)
		self.assertEqual(cn.grand_total, -15.0)
		self.assertEqual(cn.payment_schedule, [])
		self.assertEqual(cn.subscription, sub.name)
		self.assertEqual(len(cn.items), 1)
		row = cn.items[0]
		self.assertEqual(row.qty, -1)
		self.assertEqual(row.rate, 15.0)  # flt(31 * 15/31, 2)
		self.assertEqual(row.amount, -15.0)
		self.assertEqual(row.sales_invoice_item, invoice.items[0].name)
		self.assertEqual(row.subscription, sub.name)
		self.assertEqual(getdate(row.subscription_period_start), getdate("2018-01-17"))
		self.assertEqual(getdate(row.subscription_period_end), getdate("2018-01-31"))
		self.assertIn("15 of 31 days", row.description)

		invoice.reload()
		self.assertEqual(invoice.outstanding_amount, 0)
		self.assertEqual(invoice.status, "Paid")

		sub.reload()
		self.assertEqual(sub.status, "Cancelled")
		self.assertEqual(getdate(sub.cancelation_date), getdate("2018-01-16"))
		self.assertEqual(sub.has_outstanding_invoice(), 0)

		gl = frappe.get_all(
			"GL Entry",
			{"voucher_no": cn.name, "is_cancelled": 0},
			["account", "debit", "credit", "against_voucher"],
		)
		debtor = [e for e in gl if e.credit == 15.0]
		self.assertEqual(len(debtor), 1, gl)
		self.assertEqual(debtor[0].against_voucher, cn.name)
		self.assertEqual(sum(e.debit for e in gl), 15.0)

	# ---- 2
	def test_one_line_of_three_line_consolidated_invoice(self):
		s31 = self.make_sub(PLAN_31)
		s62 = self.make_sub(PLAN_62)
		s93 = self.make_sub(PLAN_93)
		invoice = self.bill()
		self.assertEqual(len(invoice.items), 3)
		self.assertEqual(invoice.grand_total, 186.0)
		self.pay(invoice)

		self.cancel(s62, "2018-01-16")

		cn = self.single_credit_note(invoice)
		self.assertEqual(len(cn.items), 1)
		row_62 = next(d for d in invoice.items if d.subscription == s62.name)
		self.assertEqual(cn.items[0].sales_invoice_item, row_62.name)
		self.assertEqual(cn.grand_total, -30.0)
		invoice.reload()
		self.assertEqual(invoice.outstanding_amount, 0)
		for other in (s31, s93):
			other.reload()
			self.assertEqual(other.status, "Active")
			self.assertEqual(other.has_outstanding_invoice(), 0)

	# ---- 3
	def test_seats_qty_gt_1(self):
		sub, invoice = self.billed_sub(PLAN_10, qty=9)
		self.pay(invoice)

		self.cancel(sub, "2018-01-16")

		cn = self.single_credit_note(invoice)
		row = cn.items[0]
		self.assertEqual(row.qty, -9)
		self.assertEqual(row.rate, 4.84)
		self.assertEqual(row.amount, -43.56)  # documented 1-cent deviation from flt(90 * 15/31, 2) == 43.55
		self.assertEqual(cn.grand_total, -43.56)

	# ---- 4
	def test_no_lines_to_credit(self):
		sub, invoice = self.billed_sub()

		result = self.cancel(sub, "2018-02-05")

		self.assertEqual(result["credit_notes"], [])
		self.assertEqual(credit_notes_against(invoice.name), [])
		sub.reload()
		self.assertEqual(sub.status, "Cancelled")
		self.assertEqual(getdate(sub.cancelation_date), getdate("2018-02-05"))

	# ---- 5
	def test_future_prepaid_line(self):
		sub, january = self.billed_sub()
		self.pay(january)  # an unpaid January past its due date would block February (stock past-due gate)
		february = self.bill("2018-02-01")
		self.assertNotEqual(february.name, january.name)
		self.assertEqual(getdate(february.items[0].subscription_period_start), getdate("2018-02-01"))

		result = self.cancel(sub, "2018-01-20")

		self.assertEqual(len(result["credit_notes"]), 2)
		cn_jan = self.single_credit_note(january)
		cn_feb = self.single_credit_note(february)
		self.assertEqual(cn_jan.grand_total, -11.0)  # unused 11 of 31 days
		self.assertEqual(getdate(cn_jan.posting_date), getdate("2018-01-20"))
		self.assertEqual(cn_feb.grand_total, -31.0)  # the whole prepaid period
		self.assertEqual(getdate(cn_feb.posting_date), getdate("2018-02-01"))  # max(cancel_date, ref posting)
		self.assertEqual(getdate(cn_feb.items[0].subscription_period_start), getdate("2018-02-01"))
		self.assertEqual(result["credited"], 42.0)

	# ---- 6
	def test_double_cancel_guard(self):
		sub, invoice = self.billed_sub()
		self.pay(invoice)
		self.cancel(sub, "2018-01-16")

		with self.assertRaises(InvoiceCancelled):
			self.cancel(sub, "2018-01-16")

		sub.db_set("status", "Active")
		with self.assertRaises(CreditNoteAlreadyIssued):
			self.cancel(sub, "2018-01-16")
		self.assertEqual(
			frappe.db.count(
				"Sales Invoice", {"is_return": 1, "return_against": invoice.name, "docstatus": 1}
			),
			1,
		)

	# ---- 7
	def test_no_payment_request_on_credit_note(self):
		set_plan_gateway(PLAN_31, make_gateway_account("_Test AS CN Gateway"))
		sub, invoice = self.billed_sub()
		self.pay(invoice)

		self.cancel(sub, "2018-01-16")

		cn = self.single_credit_note(invoice)
		self.assertEqual(
			frappe.db.count(
				"Payment Request", {"reference_doctype": "Sales Invoice", "reference_name": cn.name}
			),
			0,
		)

	# ---- 8
	def test_unpaid_original(self):
		sub, invoice = self.billed_sub()

		self.cancel(sub, "2018-01-16")

		cn = self.single_credit_note(invoice)
		self.assertEqual(cn.grand_total, -15.0)
		invoice.reload()
		self.assertEqual(invoice.outstanding_amount, 31.0)
		self.assertNotIn(invoice.status, ("Paid", "Credit Note Issued"))
		sub.reload()
		self.assertEqual(sub.has_outstanding_invoice(), 1)

	# ---- 9
	def test_credit_note_issued_status_regression(self):
		s31 = self.make_sub(PLAN_31)
		s62 = self.make_sub(PLAN_62)
		invoice = self.bill()
		self.pay(invoice)
		self.cancel(s62, "2018-01-16")

		invoice.db_set("status", "Credit Note Issued")
		s31.reload()
		self.assertEqual(s31.has_outstanding_invoice(), 0)
		with frozen_today("2018-02-20"):
			self.assertFalse(s31.current_invoice_is_past_due())

	# ---- 10
	def test_stock_path_untouched(self):
		with frozen_today(JAN_1):
			sub = create_subscription(party=STOCK_CUSTOMER, start_date=JAN_1, generate_invoice_at=BEGINNING)
		before = frappe.db.count("Sales Invoice", {"customer": STOCK_CUSTOMER, "is_return": 1})

		sub.cancel_subscription()

		sub.reload()
		self.assertEqual(sub.status, "Cancelled")
		self.assertEqual(
			frappe.db.count("Sales Invoice", {"customer": STOCK_CUSTOMER, "is_return": 1}), before
		)

	# ---- 11
	def test_permission_denied_rolls_back(self):
		sub, invoice = self.billed_sub()
		user = "test_as_cn_user@example.com"
		if not frappe.db.exists("User", user):
			frappe.get_doc(
				{"doctype": "User", "email": user, "first_name": "AS CN", "send_welcome_email": 0}
			).insert(ignore_permissions=True)
		frappe.set_user(user)
		try:
			with self.assertRaises(frappe.PermissionError):
				self.cancel(sub, "2018-01-16")
		finally:
			frappe.set_user("Administrator")
		sub.reload()
		self.assertEqual(sub.status, "Active")
		self.assertEqual(credit_notes_against(invoice.name), [])

	# ---- 12
	def test_setting_off(self):
		frappe.db.set_single_value("Subscription Settings", "credit_note_on_cancellation", 0)
		sub, invoice = self.billed_sub()

		with self.assertRaises(SubscriptionBillingError):
			self.cancel(sub, "2018-01-16")

		with frozen_today("2018-01-16"):
			sub.cancel_subscription()
		sub.reload()
		self.assertEqual(sub.status, "Cancelled")
		self.assertEqual(credit_notes_against(invoice.name), [])

	# ---- 13
	def test_button_routes_by_customer_flags(self):
		# consolidating but not anchored -> credit note
		frappe.db.set_value("Customer", STOCK_CUSTOMER, "consolidate_subscription_invoices", 1)
		frappe.clear_document_cache("Customer", STOCK_CUSTOMER)
		sub = self.make_sub(PLAN_31, party=STOCK_CUSTOMER)
		invoice = self.bill(party=STOCK_CUSTOMER)
		self.pay(invoice)
		with frozen_today("2018-01-16"):
			sub.cancel_subscription(cancel_date="2018-01-16")
		cn = self.single_credit_note(invoice)
		self.assertEqual(cn.grand_total, -15.0)
		sub.reload()
		self.assertEqual(getdate(sub.cancelation_date), getdate("2018-01-16"))

		# both flags off -> stock
		frappe.db.set_value("Customer", STOCK_CUSTOMER, "consolidate_subscription_invoices", 0)
		frappe.clear_document_cache("Customer", STOCK_CUSTOMER)
		with frozen_today(JAN_1):
			stock = create_subscription(party=STOCK_CUSTOMER, start_date=JAN_1, generate_invoice_at=BEGINNING)
		before = frappe.db.count("Sales Invoice", {"customer": STOCK_CUSTOMER, "is_return": 1})
		stock.cancel_subscription()
		self.assertEqual(
			frappe.db.count("Sales Invoice", {"customer": STOCK_CUSTOMER, "is_return": 1}), before
		)
