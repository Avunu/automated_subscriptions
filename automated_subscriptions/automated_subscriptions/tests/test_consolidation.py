from unittest.mock import patch

import filelock as filelock_lib
import frappe
from erpnext.accounts.doctype.subscription.subscription import process_all
from erpnext.accounts.doctype.subscription.test_subscription import (
	create_parties,
	create_plan,
	create_subscription,
	make_plans,
	reset_settings,
)
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, add_months, add_to_date, flt, get_site_path, getdate, nowdate

from automated_subscriptions.automated_subscriptions.billing.exceptions import (
	ConsolidatedBillingError,
	ConsolidationLocked,
)
from automated_subscriptions.automated_subscriptions.billing.runner import (
	party_lock_name,
	preview_consolidated_billing,
	run_consolidated_billing,
)
from automated_subscriptions.automated_subscriptions.custom.subscription import (
	Subscription as MixinSubscription,
)
from automated_subscriptions.automated_subscriptions.tests.utils import (
	frozen_today,
	make_anchored_customer,
)

CUSTOMER = "_Test Customer"
CUSTOMER_2 = "_Test Customer 2"
COMPANY = "_Test Company"
MONTH_PLAN = "_Test Plan Name"  # 900 / Month, Fixed Rate, INR
MONTH_PLAN_2 = "_Test Plan Name 2"  # 1999 / Month
QUARTER_PLAN = "_Test Plan Name 4"  # 20000 / 3 Months
YEAR_PLAN = "_Test AS Year Plan"  # 10000 / Year
TAX_TEMPLATE = "_Test Sales Taxes and Charges Template - _TC"  # 6 % + 6.36 % on net total
BEGINNING = "Beginning of the current subscription period"
GROUP_FAILURE_TITLE = f"Consolidated billing failed: {CUSTOMER}"


def set_setting(fieldname, value):
	frappe.db.set_single_value("Subscription Settings", fieldname, value)


def set_consolidate(customer, value):
	frappe.db.set_value("Customer", customer, "consolidate_subscription_invoices", value)
	frappe.clear_document_cache("Customer", customer)


def invoices_for(customer):
	return frappe.get_all(
		"Sales Invoice",
		{"customer": customer, "docstatus": ("<", 2)},
		["name", "docstatus", "grand_total", "from_date", "to_date", "due_date", "subscription"],
		order_by="creation asc, name asc",
	)


def error_log_count(title):
	# tabError Log is MyISAM: rows survive the test rollback, so count by title before and after
	return frappe.db.count("Error Log", {"method": title})


def rollback_savepoints_only(original):
	"""process_all does a full frappe.db.rollback() on a ValidationError, which would wipe the test
	transaction (core stubs it the same way, test_subscription.py); the runner's savepoint rollback must
	still work."""

	def rollback(*args, **kwargs):
		if kwargs.get("save_point"):
			return original(*args, **kwargs)

	return rollback


class TestConsolidation(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		make_plans()
		create_parties()
		reset_settings()
		create_plan(plan_name=YEAR_PLAN, cost=10000, billing_interval="Year", currency="INR")
		frappe.db.set_value("Company", COMPANY, "accounts_frozen_till_date", None)
		set_consolidate(CUSTOMER, 1)
		set_consolidate(CUSTOMER_2, 0)
		frappe.db.set_single_value("Selling Settings", "allow_multiple_items", 1)
		set_setting("mid_term_billing_mode", "Next Daily Run")
		frappe.local.consolidated_billing = None
		frappe.flags.subscription_billing = None
		self.today = nowdate()

	def tearDown(self):
		frappe.local.consolidated_billing = None
		frappe.db.rollback()
		super().tearDown()

	# ---- fixtures
	def make_sub(self, party=CUSTOMER, plan=MONTH_PLAN, start_date=None, identifier=None, fields=None, **kw):
		kwargs = {
			"party": party,
			"start_date": start_date or self.today,
			"plans": [{"plan": plan, "qty": 1}],
			"generate_invoice_at": BEGINNING,
			"submit_invoice": 1,
			"generate_new_invoices_past_due_date": 1,
			"days_until_due": 15,
			"do_not_save": True,
		}
		kwargs.update(kw)
		sub = create_subscription(**kwargs)
		sub.service_identifier = identifier
		for fieldname, value in (fields or {}).items():
			sub.set(fieldname, value)
		frappe.flags.in_import = True  # insert without core's catch-up billing
		try:
			sub.insert()
		finally:
			frappe.flags.in_import = False
		return frappe.get_doc("Subscription", sub.name)

	def make_three(self, party=CUSTOMER):
		return [self.make_sub(party=party, identifier=f"site-{i}") for i in (1, 2, 3)]

	def make_pricing_rule(self, title, customer):
		rule = frappe.get_doc(
			{
				"doctype": "Pricing Rule",
				"title": title,
				"company": COMPANY,
				"apply_on": "Item Code",
				"items": [{"item_code": "_Test Non Stock Item"}],
				"applicable_for": "Customer",
				"customer": customer,
				"selling": 1,
				"currency": "INR",
				"price_or_product_discount": "Price",
				"rate_or_discount": "Rate",
				"rate": 2000,
				"valid_from": "2020-01-01",
			}
		)
		rule.insert(ignore_permissions=True)
		return rule

	def single_invoice(self, customer=CUSTOMER):
		invoices = invoices_for(customer)
		self.assertEqual(len(invoices), 1, invoices)
		return frappe.get_doc("Sales Invoice", invoices[0].name)

	def assert_date(self, actual, expected):
		self.assertEqual(getdate(actual), getdate(expected))

	# ---- 1
	def test_three_monthly_one_invoice(self):
		a, b, c = self.make_three()
		results = run_consolidated_billing(self.today, party=CUSTOMER)

		invoice = self.single_invoice()
		self.assertEqual(invoice.docstatus, 1)
		self.assertEqual(len(invoice.items), 3)
		for i, sub in enumerate((a, b, c)):
			line = invoice.items[i]
			self.assertEqual(line.subscription, sub.name)
			self.assertEqual(line.subscription_plan, MONTH_PLAN)
			self.assertEqual(line.description, f"site-{i + 1}")
			self.assertEqual(line.rate, 900.0)
			self.assert_date(line.subscription_period_start, self.today)
			self.assert_date(line.subscription_period_end, add_to_date(self.today, months=1, days=-1))
			self.assertIsNone(line.service_start_date)
		self.assertEqual(invoice.subscription, a.name)  # header = sub[0]
		self.assertEqual(invoice.grand_total, 2700)
		self.assert_date(invoice.from_date, self.today)
		self.assert_date(invoice.to_date, add_to_date(self.today, months=1, days=-1))
		self.assert_date(invoice.due_date, add_days(self.today, 15))
		self.assertEqual(len(invoice.payment_schedule), 1)
		self.assertEqual(invoice.payment_schedule[0].payment_amount, 2700)
		self.assert_date(invoice.payment_schedule[0].due_date, add_days(self.today, 15))
		self.assertEqual(invoice.ignore_pricing_rule, 0)

		for sub in (a, b, c):
			sub.reload()
			self.assert_date(sub.current_invoice_start, add_months(self.today, 1))
			self.assertEqual(sub.status, "Active")
			self.assertIsNone(sub.cancelation_date)

		self.assertEqual(len(results), 1)
		summary = results[0]
		self.assertEqual(len(summary["lines"]), 3)
		self.assertEqual(summary["grand_total"], 2700)
		self.assertEqual(summary["invoice"], invoice.name)
		self.assertEqual(summary["docstatus"], 1)
		self.assertEqual(summary["party"], CUSTOMER)
		self.assertEqual(summary["subscriptions"], [a.name, b.name, c.name])
		self.assertEqual(summary["posting_date"], str(getdate(self.today)))
		self.assertEqual(summary["due_date"], str(getdate(add_days(self.today, 15))))
		self.assertEqual(summary["lines"][1]["service_identifier"], "site-2")
		self.assertEqual(summary["lines"][1]["subscription"], b.name)
		self.assertEqual(summary["lines"][1]["period_start"], str(getdate(self.today)))
		self.assertEqual(summary["payment_schedule"][0]["payment_amount"], 2700)
		self.assertEqual(summary["skipped"], [])
		self.assertIsNone(summary["errors"])
		self.assertNotIn("would_submit", summary)
		# the run context never leaks past the run
		self.assertIsNone(frappe.local.consolidated_billing.active)
		self.assertIsNone(frappe.flags.get("subscription_billing"))

	# ---- 2
	def test_monthly_plus_yearly_jan_anchor(self):
		customer = make_anchored_customer("_Test AS Cons Calendar", "Calendar", consolidate=1).name
		with frozen_today("2026-01-01"):
			m = self.make_sub(party=customer, plan=MONTH_PLAN, start_date="2026-01-01", identifier="m")
			y = self.make_sub(party=customer, plan=YEAR_PLAN, start_date="2026-01-01", identifier="y")
			run_consolidated_billing("2026-01-01", party=customer)

		invoice = self.single_invoice(customer)
		self.assertEqual(invoice.docstatus, 1)
		self.assertEqual(len(invoice.items), 2)
		self.assert_date(invoice.from_date, "2026-01-01")
		self.assert_date(invoice.to_date, "2026-12-31")
		self.assertEqual(invoice.ignore_pricing_rule, 1)  # anchored customer
		line_m, line_y = invoice.items
		self.assertEqual(line_m.subscription, m.name)
		self.assert_date(line_m.subscription_period_start, "2026-01-01")
		self.assert_date(line_m.subscription_period_end, "2026-01-31")
		self.assertEqual(line_m.rate, 900.0)
		self.assertEqual(line_y.subscription, y.name)
		self.assert_date(line_y.subscription_period_start, "2026-01-01")
		self.assert_date(line_y.subscription_period_end, "2026-12-31")
		self.assertEqual(line_y.rate, 10000.0)
		self.assertEqual(invoice.grand_total, 10900)

		with frozen_today("2026-02-01"):
			run_consolidated_billing("2026-02-01", party=customer)

		invoices = invoices_for(customer)
		self.assertEqual(len(invoices), 2)
		second = frappe.get_doc("Sales Invoice", invoices[1].name)
		self.assertEqual(len(second.items), 1)
		self.assertEqual(second.items[0].subscription, m.name)
		self.assert_date(second.from_date, "2026-02-01")
		self.assert_date(second.to_date, "2026-02-28")
		y.reload()
		self.assert_date(y.current_invoice_start, "2027-01-01")
		m.reload()
		self.assert_date(m.current_invoice_start, "2026-03-01")

	# ---- 3
	def test_idempotent_same_day(self):
		a, b, c = self.make_three()
		run_consolidated_billing(self.today, party=CUSTOMER)
		self.assertEqual(len(invoices_for(CUSTOMER)), 1)

		# (a) same request: memo + DB re-check
		self.assertEqual(run_consolidated_billing(self.today, party=CUSTOMER), [])
		self.assertEqual(len(invoices_for(CUSTOMER)), 1)

		# (b) fresh context (another job / process): DB re-check only
		frappe.local.consolidated_billing = None
		self.assertEqual(run_consolidated_billing(self.today, party=CUSTOMER), [])
		self.assertEqual(len(invoices_for(CUSTOMER)), 1)

		# (c) core's daily fan-out on the same day
		before = error_log_count(GROUP_FAILURE_TITLE)
		original_rollback = frappe.db.rollback
		frappe.db.rollback = rollback_savepoints_only(original_rollback)
		try:
			process_all([a.name, b.name, c.name], self.today)
		finally:
			frappe.db.rollback = original_rollback
		self.assertEqual(len(invoices_for(CUSTOMER)), 1)
		self.assertEqual(error_log_count(GROUP_FAILURE_TITLE), before)
		for sub in (a, b, c):
			sub.reload()
			self.assert_date(sub.current_invoice_start, add_months(self.today, 1))

	# ---- 4
	def test_process_all_delegation_reloads(self):
		a, b, c = self.make_three()
		before = error_log_count("Subscription failed")

		process_all([a.name, b.name, c.name], self.today)

		invoice = self.single_invoice()
		self.assertEqual(invoice.docstatus, 1)
		self.assertEqual(len(invoice.items), 3)
		for sub in (a, b, c):
			sub.reload()
			self.assert_date(sub.current_invoice_start, add_months(self.today, 1))
			self.assertEqual(sub.status, "Active")
		self.assertEqual(error_log_count("Subscription failed"), before)
		self.assertEqual(
			frappe.local.consolidated_billing.done, {(COMPANY, "Customer", CUSTOMER, self.today): "ok"}
		)

	# ---- 5
	def test_grace_period_regression(self):
		"""sub[k>0] with its own overdue, past-grace invoice must not be cancelled by the run that just put its
		line on the sink (C4 scenario B): core's header-only current-invoice lookup would return the old invoice.
		Run 1 bills A and B standalone (the pre-consolidation history every live sub has), run 2 consolidates."""
		start = add_months(self.today, -1)
		set_consolidate(CUSTOMER, 0)
		a = self.make_sub(start_date=start, identifier="a")
		b = self.make_sub(start_date=start, identifier="b")
		with frozen_today(start):
			a.process(posting_date=start)
			b.process(posting_date=start)
		first = invoices_for(CUSTOMER)
		self.assertEqual(len(first), 2)
		for invoice in first:
			self.assertEqual(invoice.docstatus, 1)
			self.assert_date(invoice.due_date, add_days(start, 15))
			self.assertLess(getdate(invoice.due_date), getdate(self.today))

		set_consolidate(CUSTOMER, 1)
		set_setting("cancel_after_grace", 1)
		set_setting("grace_period", 0)
		for sub in (a, b):
			sub.db_set("status", "Grace Period")

		run_consolidated_billing(self.today, party=CUSTOMER)

		invoices = invoices_for(CUSTOMER)
		self.assertEqual(len(invoices), 3)
		second = frappe.get_doc("Sales Invoice", invoices[2].name)
		self.assertEqual(second.docstatus, 1)
		self.assert_date(second.from_date, self.today)
		self.assertEqual([d.subscription for d in second.items], [a.name, b.name])
		for sub in (a, b):
			sub.reload()
			self.assertNotEqual(sub.status, "Cancelled")
			self.assertIsNone(sub.cancelation_date)
			self.assertIn(sub.status, ("Grace Period", "Unpaid"))
			self.assert_date(sub.current_invoice_start, add_months(self.today, 1))

		# core parity: the same scenario standalone for a non-consolidating customer
		a2 = self.make_sub(party=CUSTOMER_2, start_date=start, identifier="a")
		b2 = self.make_sub(party=CUSTOMER_2, start_date=start, identifier="b")
		with frozen_today(start):
			a2.process(posting_date=start)
			b2.process(posting_date=start)
		for sub in (a2, b2):
			sub.db_set("status", "Grace Period")
		a2.process(posting_date=self.today)
		b2.process(posting_date=self.today)
		self.assertEqual(len(invoices_for(CUSTOMER_2)), 4)
		for stock, consolidated in ((a2, a), (b2, b)):
			stock.reload()
			self.assertEqual(stock.status, consolidated.status)
			self.assertIsNone(stock.cancelation_date)

	# ---- 6
	def test_differing_tax_template_splits_group(self):
		a = self.make_sub(identifier="a")
		b = self.make_sub(identifier="b")
		c = self.make_sub(identifier="c", fields={"sales_tax_template": TAX_TEMPLATE})

		results = run_consolidated_billing(self.today, party=CUSTOMER)

		invoices = [frappe.get_doc("Sales Invoice", row.name) for row in invoices_for(CUSTOMER)]
		self.assertEqual(len(invoices), 2)
		self.assertEqual(len(results), 2)
		plain = next(i for i in invoices if not i.taxes_and_charges)
		taxed = next(i for i in invoices if i.taxes_and_charges)
		self.assertEqual([d.subscription for d in plain.items], [a.name, b.name])
		self.assertEqual(plain.taxes, [])
		self.assertEqual(plain.grand_total, 1800)
		self.assertEqual(plain.subscription, a.name)
		self.assertEqual([d.subscription for d in taxed.items], [c.name])
		self.assertEqual(taxed.taxes_and_charges, TAX_TEMPLATE)
		self.assertEqual(taxed.net_total, 900)
		self.assertEqual(taxed.total_taxes_and_charges, flt(900 * 0.1236, 2))
		self.assertEqual(taxed.subscription, c.name)
		self.assertEqual({r["sales_tax_template"] for r in results}, {None, TAX_TEMPLATE})

	# ---- 7
	def test_differing_days_until_due_splits_group(self):
		a = self.make_sub(identifier="a", days_until_due=15)
		b = self.make_sub(identifier="b", days_until_due=5)

		run_consolidated_billing(self.today, party=CUSTOMER)

		invoices = invoices_for(CUSTOMER)
		self.assertEqual(len(invoices), 2)
		by_sub = {row.subscription: row for row in invoices}
		self.assert_date(by_sub[a.name].due_date, add_days(self.today, 15))
		self.assert_date(by_sub[b.name].due_date, add_days(self.today, 5))
		for row in invoices:
			self.assertEqual(len(frappe.get_doc("Sales Invoice", row.name).items), 1)

	# ---- 8
	def test_money_conservation(self):
		plans = (MONTH_PLAN, MONTH_PLAN_2, QUARTER_PLAN)  # 900, 1999, 20000 (3-Month)
		for i, plan in enumerate(plans):
			self.make_sub(plan=plan, identifier=f"cons-{i}")
		standalone = [
			self.make_sub(party=CUSTOMER_2, plan=plan, identifier=f"std-{i}") for i, plan in enumerate(plans)
		]

		run_consolidated_billing(self.today, party=CUSTOMER)
		for sub in standalone:
			sub.process(posting_date=self.today)

		consolidated = self.single_invoice()
		self.assertEqual(len(consolidated.items), 3)
		invoices = [frappe.get_doc("Sales Invoice", row.name) for row in invoices_for(CUSTOMER_2)]
		self.assertEqual(len(invoices), 3)
		self.assertEqual(flt(consolidated.grand_total, 2), flt(sum(i.grand_total for i in invoices), 2))
		self.assertEqual(consolidated.grand_total, 22899)
		self.assertEqual(
			flt(consolidated.outstanding_amount, 2), flt(sum(i.outstanding_amount for i in invoices), 2)
		)
		# rounded_total only conserves under a 0.01 fraction (USD live); INR on test_site has fraction 0 so
		# rounding to whole units could differ between one and three invoices
		if flt(frappe.db.get_value("Currency", "INR", "smallest_currency_fraction_value")) == 0.01:
			self.assertEqual(
				flt(consolidated.rounded_total, 2), flt(sum(i.rounded_total for i in invoices), 2)
			)
		self.assertEqual(
			sorted(d.amount for d in consolidated.items),
			sorted(d.amount for i in invoices for d in i.items),
		)
		self.assertEqual(sorted(d.amount for d in consolidated.items), [900.0, 1999.0, 20000.0])

	# ---- 9
	def test_lock_busy_raises_validation_error(self):
		self.make_sub(identifier="a")
		self.make_sub(identifier="b")
		self.assertTrue(issubclass(ConsolidationLocked, frappe.ValidationError))
		lock_path = get_site_path("locks", party_lock_name(COMPANY, "Customer", CUSTOMER) + ".lock")
		holder = filelock_lib.FileLock(lock_path, timeout=0)  # a separate object: the lock is not reentrant
		holder.acquire()
		try:
			with (
				patch("frappe.utils.synchronization.frappe.log_error"),
				self.assertRaises(ConsolidationLocked),
			):
				run_consolidated_billing(self.today, party=CUSTOMER, lock_timeout=0.2)
		finally:
			holder.release()
		self.assertEqual(invoices_for(CUSTOMER), [])
		self.assertIsNone(frappe.local.consolidated_billing.active)

		# transient: once the holder is gone the same run goes through
		run_consolidated_billing(self.today, party=CUSTOMER)
		self.assertEqual(len(self.single_invoice().items), 2)

	# ---- 10
	def test_process_subscription_with_customer(self):
		a = self.make_sub(identifier="a")
		b = self.make_sub(identifier="b")
		set_consolidate(CUSTOMER_2, 1)
		self.make_sub(party=CUSTOMER_2, identifier="x")

		with patch("frappe.enqueue") as enqueue:
			frappe.get_doc(
				{"doctype": "Process Subscription", "posting_date": self.today, "customer": CUSTOMER}
			).submit()
		enqueue.assert_not_called()  # the customer path is synchronous

		invoice = self.single_invoice()
		self.assertEqual([d.subscription for d in invoice.items], [a.name, b.name])
		self.assertEqual(invoices_for(CUSTOMER_2), [])

	def test_process_subscription_without_customer_is_stock(self):
		self.make_sub(identifier="a")
		with patch("frappe.enqueue") as enqueue:
			frappe.get_doc({"doctype": "Process Subscription", "posting_date": self.today}).submit()
		enqueue.assert_called_once()
		self.assertEqual(
			enqueue.call_args.kwargs["method"],
			"erpnext.accounts.doctype.subscription.subscription.process_all",
		)
		self.assertEqual(invoices_for(CUSTOMER), [])

	# ---- 11
	def test_preview_is_side_effect_free(self):
		a = self.make_sub(identifier="a")
		b = self.make_sub(identifier="b")

		results = preview_consolidated_billing(self.today, CUSTOMER)

		self.assertEqual(len(results), 1)
		preview = results[0]
		self.assertEqual(len(preview["lines"]), 2)
		self.assertEqual(preview["grand_total"], 1800)
		self.assertIs(preview["would_submit"], True)
		self.assertIsNone(preview["invoice"])
		self.assertNotIn("docstatus", preview)
		self.assertEqual(preview["subscriptions"], [a.name, b.name])
		self.assertEqual(preview["due_date"], str(getdate(add_days(self.today, 15))))
		self.assertEqual(invoices_for(CUSTOMER), [])
		for sub in (a, b):
			sub.reload()
			self.assert_date(sub.current_invoice_start, self.today)
			self.assertEqual(sub.status, "Active")
		self.assertEqual(frappe.local.consolidated_billing.done, {})
		self.assertIsNone(frappe.local.consolidated_billing.active)

		results = run_consolidated_billing(self.today, party=CUSTOMER)
		invoice = self.single_invoice()
		self.assertEqual(invoice.docstatus, 1)
		self.assertEqual(invoice.grand_total, preview["grand_total"])
		self.assertEqual(results[0]["net_total"], preview["net_total"])
		self.assertEqual(results[0]["payment_schedule"], preview["payment_schedule"])
		self.assertEqual(
			[(line["subscription"], line["rate"], line["amount"]) for line in results[0]["lines"]],
			[(line["subscription"], line["rate"], line["amount"]) for line in preview["lines"]],
		)

	# ---- 12
	def test_group_failure_does_not_kill_batch(self):
		a = self.make_sub(identifier="a")
		b = self.make_sub(identifier="b")
		set_consolidate(CUSTOMER_2, 1)
		x = self.make_sub(party=CUSTOMER_2, identifier="x")
		before = error_log_count(GROUP_FAILURE_TITLE)
		before_sub = error_log_count("Subscription failed")

		original_rollback = frappe.db.rollback
		frappe.db.rollback = rollback_savepoints_only(original_rollback)
		try:
			with patch.object(MixinSubscription, "_absorb_into", side_effect=IndexError("boom")):
				process_all([a.name, b.name, x.name], self.today)
		finally:
			frappe.db.rollback = original_rollback

		self.assertEqual(invoices_for(CUSTOMER), [])  # the group rolled back (sink draft included)
		self.assertEqual(len(invoices_for(CUSTOMER_2)), 1)  # the next party still billed
		self.assertEqual(error_log_count(GROUP_FAILURE_TITLE), before + 1)
		self.assertEqual(error_log_count("Subscription failed"), before_sub + 2)  # A and B, never X
		a.reload()
		self.assert_date(a.current_invoice_start, self.today)
		b.reload()
		self.assert_date(b.current_invoice_start, self.today)
		x.reload()
		self.assert_date(x.current_invoice_start, add_months(self.today, 1))
		self.assertEqual(
			frappe.local.consolidated_billing.done,
			{
				(COMPANY, "Customer", CUSTOMER, self.today): "failed",
				(COMPANY, "Customer", CUSTOMER_2, self.today): "ok",
			},
		)
		self.assertIsNone(frappe.local.consolidated_billing.active)
		# a member reached later in the same run must raise, never bill standalone
		with self.assertRaises(ConsolidatedBillingError):
			frappe.get_doc("Subscription", b.name).process(self.today)
		self.assertEqual(invoices_for(CUSTOMER), [])

	# ---- 13
	def test_non_consolidating_customer_is_stock(self):
		subs = [self.make_sub(party=CUSTOMER_2, identifier=f"s-{i}") for i in (1, 2)]
		for sub in subs:
			sub.process(posting_date=self.today)

		invoices = invoices_for(CUSTOMER_2)
		self.assertEqual(len(invoices), 2)
		self.assertEqual({row.subscription for row in invoices}, {s.name for s in subs})
		for row in invoices:
			self.assertEqual(row.docstatus, 1)
			self.assertEqual(row.grand_total, 900)
		self.assertIsNone(frappe.local.consolidated_billing)

	# ---- 14
	def test_immediate_mode_enqueues_runner(self):
		set_setting("mid_term_billing_mode", "Immediate")
		with patch("frappe.enqueue") as enqueue:
			sub = create_subscription(
				party=CUSTOMER,
				start_date=self.today,
				generate_invoice_at=BEGINNING,
				submit_invoice=1,
				days_until_due=15,
			)  # no in_import: after_insert runs
		self.assertEqual(invoices_for(CUSTOMER), [])
		enqueue.assert_called_once()
		kwargs = enqueue.call_args.kwargs
		self.assertEqual(
			enqueue.call_args.args[0],
			"automated_subscriptions.automated_subscriptions.billing.runner.run_consolidated_billing",
		)
		self.assertTrue(kwargs["enqueue_after_commit"])
		self.assertEqual(kwargs["party"], CUSTOMER)
		self.assertEqual(kwargs["party_type"], "Customer")
		self.assertEqual(kwargs["company"], COMPANY)
		self.assertEqual(kwargs["queue"], "long")
		self.assert_date(sub.current_invoice_start, self.today)

		# Next Daily Run: nothing enqueued, the daily job picks it up
		set_setting("mid_term_billing_mode", "Next Daily Run")
		with patch("frappe.enqueue") as enqueue:
			create_subscription(
				party=CUSTOMER,
				start_date=self.today,
				generate_invoice_at=BEGINNING,
				submit_invoice=1,
				days_until_due=15,
			)
		enqueue.assert_not_called()
		self.assertEqual(invoices_for(CUSTOMER), [])

	# ---- 15
	def test_pricing_rule_kept_for_non_anchored_consolidating_customer(self):
		self.make_pricing_rule("_Test AS Rule Consolidated", CUSTOMER)
		self.make_sub(identifier="a")
		self.make_sub(identifier="b")

		run_consolidated_billing(self.today, party=CUSTOMER)

		invoice = self.single_invoice()
		self.assertEqual(invoice.ignore_pricing_rule, 0)
		self.assertEqual(len(invoice.items), 2)
		for line in invoice.items:
			self.assertEqual(line.rate, 2000.0)
		self.assertEqual(invoice.grand_total, 4000)
