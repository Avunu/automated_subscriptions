import unittest
from unittest.mock import patch

import filelock as filelock_lib
import frappe
from erpnext import get_default_company
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

from automated_subscriptions.automated_subscriptions.billing import runner as runner_module
from automated_subscriptions.automated_subscriptions.billing.exceptions import (
	ConsolidatedBillingError,
	ConsolidationLocked,
)
from automated_subscriptions.automated_subscriptions.billing.runner import (
	_candidate_subscriptions,
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
	# pinned: with start = add_months(today, -1) core's period 2 (start_date + 1 month) misses today after a
	# shorter month (03-29..31, 05-31, 07-31, 10-31, 12-31), so from_date / current_invoice_start would not match
	GRACE_START, GRACE_TODAY = "2026-01-10", "2026-02-10"

	def _grace_scenario(self):
		"""Run 1 bills A and B standalone at GRACE_START (the pre-consolidation history every live sub has); run 2
		consolidates at GRACE_TODAY with cancel_after_grace on and no grace, both subs db_set to Grace Period.
		Core parity: the same scenario standalone for the non-consolidating customer 2 (a2, b2)."""
		start, today = self.GRACE_START, self.GRACE_TODAY
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
			self.assertLess(getdate(invoice.due_date), getdate(today))

		set_consolidate(CUSTOMER, 1)
		set_setting("cancel_after_grace", 1)
		set_setting("grace_period", 0)
		for sub in (a, b):
			sub.db_set("status", "Grace Period")

		with frozen_today(today):  # core's grace check compares the sink's due date with nowdate()
			run_consolidated_billing(today, party=CUSTOMER)

		a2 = self.make_sub(party=CUSTOMER_2, start_date=start, identifier="a")
		b2 = self.make_sub(party=CUSTOMER_2, start_date=start, identifier="b")
		with frozen_today(start):
			a2.process(posting_date=start)
			b2.process(posting_date=start)
		for sub in (a2, b2):
			sub.db_set("status", "Grace Period")
		with frozen_today(today):
			a2.process(posting_date=today)
			b2.process(posting_date=today)
		return a, b, a2, b2

	def test_grace_period_regression(self):
		"""sub[k>0] with its own overdue, past-grace invoice must not be cancelled by the run that just put its
		line on the sink (C4 scenario B): core's header-only current-invoice lookup would return the old invoice."""
		today = self.GRACE_TODAY
		a, b, a2, b2 = self._grace_scenario()

		invoices = invoices_for(CUSTOMER)
		self.assertEqual(len(invoices), 3)
		second = frappe.get_doc("Sales Invoice", invoices[2].name)
		self.assertEqual(second.docstatus, 1)
		self.assert_date(second.from_date, today)
		self.assertEqual([d.subscription for d in second.items], [a.name, b.name])
		for sub in (a, b):
			sub.reload()
			self.assertNotEqual(sub.status, "Cancelled")
			self.assertIsNone(sub.cancelation_date)
			self.assertIn(sub.status, ("Grace Period", "Unpaid"))
			self.assert_date(sub.current_invoice_start, add_months(today, 1))

		# core parity: the same scenario standalone for a non-consolidating customer
		self.assertEqual(len(invoices_for(CUSTOMER_2)), 4)
		for stock, consolidated in ((a2, a), (b2, b)):
			stock.reload()
			self.assertEqual(stock.status, consolidated.status)
			self.assertIsNone(stock.cancelation_date)

	@unittest.expectedFailure
	def test_grace_period_after_run_needs_pr4(self):
		"""The run-scoped guard protects the runner's own doc copies only. The delegating copy in process() and
		every later daily run go through core's header-only get_current_invoice for sub[k>0] (and for sub[0] once
		a sibling's longer period has widened the sink's to_date), which returns the wrong invoice and cancels
		it. PR 4's line-aware lookup closes this: drop the decorator there.
		Until then no customer may carry consolidate_subscription_invoices = 1 on a site without PR 4."""
		today = self.GRACE_TODAY
		a, b, a2, b2 = self._grace_scenario()

		frappe.local.consolidated_billing = None
		original_rollback = frappe.db.rollback
		frappe.db.rollback = rollback_savepoints_only(original_rollback)
		try:
			with frozen_today(today):
				process_all([a.name, b.name], today)  # scheduler path: sub[0] delegates, sub[1] hits the memo
		finally:
			frappe.db.rollback = original_rollback
		for sub in (a, b):
			sub.reload()
			self.assertNotEqual(sub.status, "Cancelled")
			self.assertIsNone(sub.cancelation_date)

		frappe.local.consolidated_billing = None
		next_day = add_days(today, 1)
		with frozen_today(next_day):
			for sub in (a, b, a2, b2):
				sub.process(posting_date=next_day)
				sub.reload()
		self.assertEqual(a.status, a2.status)
		self.assertEqual(b.status, b2.status)
		self.assertIsNone(b.cancelation_date)

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

	# ---- review round: sticky "failed" memo (one party, several groups in one run)
	def test_failed_group_memo_survives_later_ok_group(self):
		# two groups for one party: {a, a2} (no tax template, created first) and {b} (tax template)
		a = self.make_sub(identifier="a")
		a2 = self.make_sub(identifier="a2")
		b = self.make_sub(identifier="b", fields={"sales_tax_template": TAX_TEMPLATE})
		before = error_log_count(GROUP_FAILURE_TITLE)
		original_rollback = frappe.db.rollback
		frappe.db.rollback = rollback_savepoints_only(original_rollback)
		try:
			with patch.object(MixinSubscription, "_absorb_into", side_effect=IndexError("boom")):
				process_all([a.name, a2.name, b.name], self.today)
		finally:
			frappe.db.rollback = original_rollback

		self.assertEqual(error_log_count(GROUP_FAILURE_TITLE), before + 1)
		self.assertEqual(
			frappe.local.consolidated_billing.done[(COMPANY, "Customer", CUSTOMER, self.today)], "failed"
		)
		invoices = invoices_for(CUSTOMER)
		# only the taxed group billed; nothing standalone for the rolled-back group
		self.assertEqual([row.subscription for row in invoices], [b.name])
		self.assertEqual(len(frappe.get_doc("Sales Invoice", invoices[0].name).items), 1)
		for sub in (a, a2):
			sub.reload()
			self.assert_date(sub.current_invoice_start, self.today)  # rolled back, not re-billed standalone
			with self.assertRaises(ConsolidatedBillingError):
				frappe.get_doc("Subscription", sub.name).process(self.today)
		self.assertEqual([row.subscription for row in invoices_for(CUSTOMER)], [b.name])

	def test_failed_group_stays_failed_when_sibling_group_succeeds(self):
		a = self.make_sub(identifier="a", days_until_due=15)  # first group (creation order)
		b = self.make_sub(identifier="b", days_until_due=5)  # second group, must succeed
		before_group = error_log_count(GROUP_FAILURE_TITLE)
		before_sub = error_log_count("Subscription failed")
		original_finalize = runner_module._finalize

		def finalize_fails_for_a(run, skipped):
			if any(m.name == a.name for m in run.members):
				raise IndexError("boom")
			return original_finalize(run, skipped)

		original_rollback = frappe.db.rollback
		frappe.db.rollback = rollback_savepoints_only(original_rollback)
		try:
			with patch.object(runner_module, "_finalize", side_effect=finalize_fails_for_a):
				process_all([a.name, b.name], self.today)
		finally:
			frappe.db.rollback = original_rollback

		invoices = invoices_for(CUSTOMER)
		self.assertEqual(len(invoices), 1)  # B consolidated; A must NOT have been billed standalone
		self.assertEqual(invoices[0].subscription, b.name)
		self.assert_date(invoices[0].due_date, add_days(self.today, 5))
		a.reload()
		self.assert_date(a.current_invoice_start, self.today)  # rolled back, still unbilled
		b.reload()
		self.assert_date(b.current_invoice_start, add_months(self.today, 1))
		self.assertEqual(error_log_count(GROUP_FAILURE_TITLE), before_group + 1)
		# A raised (unbilled); B raised too (noise, safe: the runner already billed and saved it)
		self.assertEqual(error_log_count("Subscription failed"), before_sub + 2)
		self.assertEqual(
			frappe.local.consolidated_billing.done, {(COMPANY, "Customer", CUSTOMER, self.today): "failed"}
		)
		with self.assertRaises(ConsolidatedBillingError):
			frappe.get_doc("Subscription", a.name).process(self.today)
		self.assertEqual(len(invoices_for(CUSTOMER)), 1)

	def test_failed_sibling_group_never_bills_standalone(self):
		a = self.make_sub(identifier="a", days_until_due=15)
		b = self.make_sub(identifier="b", days_until_due=15)
		c = self.make_sub(identifier="c", days_until_due=5)
		before_sub = error_log_count("Subscription failed")
		original_rollback = frappe.db.rollback
		frappe.db.rollback = rollback_savepoints_only(original_rollback)
		try:
			with patch.object(MixinSubscription, "_absorb_into", side_effect=IndexError("boom")):
				process_all([a.name, b.name, c.name], self.today)
		finally:
			frappe.db.rollback = original_rollback

		invoice = self.single_invoice()
		self.assertEqual([d.subscription for d in invoice.items], [c.name])  # the {C} group succeeded
		for sub in (a, b):
			sub.reload()
			self.assert_date(sub.current_invoice_start, self.today)  # never billed standalone
		self.assertEqual(
			frappe.local.consolidated_billing.done[(COMPANY, "Customer", CUSTOMER, self.today)], "failed"
		)
		# A, B raise ConsolidatedBillingError; C raises too because the party memo is failed (already billed)
		self.assertEqual(error_log_count("Subscription failed"), before_sub + 3)
		with self.assertRaises(ConsolidatedBillingError):
			frappe.get_doc("Subscription", b.name).process(self.today)
		self.assertEqual(len(invoices_for(CUSTOMER)), 1)

	# ---- review round: delegation reload must not resurrect a stale (pre-anchor) period end
	def test_skipped_anchored_member_is_not_billed_standalone_on_stale_end(self):
		"""Delegation reload must not resurrect a period end stored before the customer's anchor was set: the
		consolidating twin of test_proration's R2-02 case (the healed end is persisted by core's trailing save)."""
		customer = make_anchored_customer("_Test AS Cons Late Anchor", "", consolidate=1).name
		with frozen_today("2026-10-02"):
			sub = self.make_sub(
				party=customer,
				start_date="2026-10-02",
				generate_invoice_at="End of the current subscription period",
			)
		self.assert_date(sub.current_invoice_end, "2026-11-01")  # stock period, stored before the anchor
		frappe.db.set_value(
			"Customer",
			customer,
			{"subscription_billing_anchor_mode": "Calendar", "subscription_billing_interval": "Year"},
		)
		frappe.clear_document_cache("Customer", customer)
		sub = frappe.get_doc("Subscription", sub.name)
		# between the stale trigger (11-01, inside core's one-cycle cap) and the aligned one (12-31)
		original_rollback = frappe.db.rollback
		frappe.db.rollback = rollback_savepoints_only(original_rollback)
		try:
			with frozen_today("2026-11-01"):
				process_all([sub.name], "2026-11-01")  # delegates; the runner skips it as not due
		finally:
			frappe.db.rollback = original_rollback
		self.assertEqual(invoices_for(customer), [])  # no early standalone invoice
		sub.reload()
		self.assert_date(sub.current_invoice_start, "2026-10-02")
		self.assert_date(
			sub.current_invoice_end, "2026-12-31"
		)  # healed and persisted by core's trailing save
		self.assertEqual(sub.status, "Active")

		with frozen_today("2026-12-31"):
			sub.process(posting_date="2026-12-31")
		invoice = self.single_invoice(customer)
		self.assertEqual(invoice.docstatus, 1)
		self.assert_date(invoice.posting_date, "2026-12-31")  # == today, never in the future
		self.assert_date(invoice.from_date, "2026-10-02")
		self.assert_date(invoice.to_date, "2026-12-31")
		sub.reload()
		self.assert_date(sub.current_invoice_start, "2027-01-01")

	# ---- review round: a busy lock must not abort a book-wide run
	def test_lock_busy_other_party_still_billed(self):
		self.make_sub(identifier="a")
		set_consolidate(CUSTOMER_2, 1)
		self.make_sub(party=CUSTOMER_2, identifier="x")
		lock_path = get_site_path("locks", party_lock_name(COMPANY, "Customer", CUSTOMER) + ".lock")
		holder = filelock_lib.FileLock(lock_path, timeout=0)
		holder.acquire()
		try:
			with patch("frappe.utils.synchronization.frappe.log_error"):
				results = run_consolidated_billing(self.today, lock_timeout=0.2)  # no party: book-wide
		finally:
			holder.release()
		self.assertEqual(len(results), 2)
		self.assertEqual(results[0]["party"], CUSTOMER)  # groups are ordered by party; the locked one first
		self.assertIsNone(results[0]["invoice"])
		self.assertIn("running elsewhere", results[0]["errors"])
		self.assertEqual(results[1]["party"], CUSTOMER_2)
		self.assertEqual(len(results[1]["lines"]), 1)
		self.assertEqual(invoices_for(CUSTOMER), [])
		self.assertEqual(len(invoices_for(CUSTOMER_2)), 1)
		self.assertEqual(
			frappe.local.consolidated_billing.done,
			{
				(COMPANY, "Customer", CUSTOMER, self.today): "failed",
				(COMPANY, "Customer", CUSTOMER_2, self.today): "ok",
			},
		)
		self.assertIsNone(frappe.local.consolidated_billing.active)

	# ---- review round: a blank company bills under the default company, consolidated
	def test_blank_company_member_is_consolidated(self):
		frappe.db.set_default("company", COMPANY)
		frappe.db.set_value("Customer", CUSTOMER, "default_currency", "INR")
		try:
			self.assertEqual(get_default_company(), COMPANY)
			# company is not mandatory; a blank one also gets no default cost center (a group-key element), so
			# give it the sibling's to isolate the company element of the key
			cost_center = frappe.get_cached_value("Company", COMPANY, "cost_center")
			blank = self.make_sub(identifier="blank", fields={"company": None, "cost_center": cost_center})
			sibling = self.make_sub(identifier="sibling")
			self.assertFalse(frappe.db.get_value("Subscription", blank.name, "company"))
			self.assertIn(blank.name, _candidate_subscriptions(CUSTOMER, "Customer", COMPANY))

			original_rollback = frappe.db.rollback
			frappe.db.rollback = rollback_savepoints_only(original_rollback)
			try:
				process_all([blank.name, sibling.name], self.today)
			finally:
				frappe.db.rollback = original_rollback

			invoice = self.single_invoice()
			self.assertEqual(invoice.company, COMPANY)
			self.assertEqual([d.subscription for d in invoice.items], [blank.name, sibling.name])
			self.assertEqual(invoice.grand_total, 1800)
			self.assertEqual(
				frappe.local.consolidated_billing.done, {(COMPANY, "Customer", CUSTOMER, self.today): "ok"}
			)
		finally:
			frappe.db.set_value("Customer", CUSTOMER, "default_currency", None)
			frappe.defaults.clear_default("company", parent="__default")

	# ---- review round: Process Subscription persists itself before core's process_all can roll back
	def test_process_subscription_commits_before_process_all(self):
		self.make_sub(identifier="a")
		order = []
		with (
			patch.object(frappe, "in_test", False),
			patch.object(frappe.db, "commit", side_effect=lambda *a, **k: order.append("commit")),
			patch(
				"automated_subscriptions.automated_subscriptions.custom.process_subscription.process_all",
				side_effect=lambda *a, **k: order.append("process_all"),
			),
			patch("frappe.enqueue"),
		):
			frappe.get_doc(
				{"doctype": "Process Subscription", "posting_date": self.today, "customer": CUSTOMER}
			).submit()
		self.assertIn("process_all", order)
		self.assertEqual(order[order.index("process_all") - 1], "commit")

	# ---- review round: a failure after the savepoint was released is still a ConsolidatedBillingError
	def test_late_failure_after_release_is_reported(self):
		self.make_sub(identifier="a")
		self.make_sub(identifier="b")
		before = error_log_count(GROUP_FAILURE_TITLE)
		calls = []

		def commit(*args, **kwargs):
			calls.append(1)
			if (
				len(calls) == 2
			):  # the post-release commit (the first is the fresh-snapshot one after the lock)
				raise RuntimeError("redis down")

		original_rollback = frappe.db.rollback
		frappe.db.rollback = rollback_savepoints_only(original_rollback)
		try:
			with (
				patch.object(frappe, "in_test", False),
				patch.object(frappe.db, "commit", side_effect=commit),
				patch("frappe.enqueue"),
			):
				results = run_consolidated_billing(self.today, party=CUSTOMER)
		finally:
			frappe.db.rollback = original_rollback

		self.assertEqual(len(results), 1)
		self.assertIsNone(results[0]["invoice"])
		self.assertIn("redis down", results[0]["errors"])
		self.assertEqual(error_log_count(GROUP_FAILURE_TITLE), before + 1)
		self.assertEqual(
			frappe.local.consolidated_billing.done, {(COMPANY, "Customer", CUSTOMER, self.today): "failed"}
		)
		self.assertIsNone(frappe.local.consolidated_billing.active)

	# ---- review round: stock cadence, one period per run
	def test_more_than_one_period_behind_bills_one_period_per_run(self):
		# 45 days behind; pinned like test 5: a month-end start chains clamped (01-30 -> 02-28 -> 03-28) so
		# add_months(start, 2) misses on 03-15..17, 05-15, 07-15, 10-15, 12-15
		start, today = "2026-01-01", "2026-02-15"
		a = self.make_sub(start_date=start, identifier="a")
		b = self.make_sub(start_date=start, identifier="b")
		before_sub = error_log_count("Subscription failed")
		with (
			frozen_today(today),
			patch.object(frappe.db, "rollback", rollback_savepoints_only(frappe.db.rollback)),
		):
			process_all([a.name, b.name], today)
		invoices = invoices_for(CUSTOMER)
		self.assertEqual(len(invoices), 1)
		self.assertEqual(invoices[0].docstatus, 1)
		self.assert_date(invoices[0].from_date, start)
		self.assertEqual(len(frappe.get_doc("Sales Invoice", invoices[0].name).items), 2)
		for name in (a.name, b.name):
			sub = frappe.get_doc("Subscription", name)
			self.assert_date(sub.current_invoice_start, add_months(start, 1))  # still due, but not re-billed
		self.assertEqual(error_log_count("Subscription failed"), before_sub)

		# the next run (fresh memo) consolidates the next period
		frappe.local.consolidated_billing = None
		with (
			frozen_today(today),
			patch.object(frappe.db, "rollback", rollback_savepoints_only(frappe.db.rollback)),
		):
			process_all([a.name, b.name], today)
		invoices = invoices_for(CUSTOMER)
		self.assertEqual(len(invoices), 2)
		self.assert_date(invoices[1].from_date, add_months(start, 1))
		self.assertTrue(all(len(frappe.get_doc("Sales Invoice", i.name).items) == 2 for i in invoices))
		for name in (a.name, b.name):
			self.assert_date(frappe.get_doc("Subscription", name).current_invoice_start, add_months(start, 2))

	# ---- review round: core's cancellation arrears invoice stays standalone inside a run
	def test_cancel_at_period_end_arrears_is_standalone(self):
		# pinned (see test 5): b.current_invoice_end must equal today - 1, impossible on 03-29..31, 05-31,
		# 07-31, 10-31, 12-31
		start, today = "2026-01-10", "2026-02-10"
		frappe.db.set_single_value("Selling Settings", "allow_multiple_items", 0)
		a = self.make_sub(start_date=today, identifier="a")
		b = self.make_sub(
			identifier="b",
			start_date=start,
			generate_invoice_at="End of the current subscription period",
			cancel_at_period_end=1,
		)
		self.assert_date(b.current_invoice_end, add_days(today, -1))
		with frozen_today(today):  # cancel_subscription stamps cancelation_date = nowdate()
			results = run_consolidated_billing(today, party=CUSTOMER)

		self.assertEqual([r["errors"] for r in results], [None, None])
		invoices = [frappe.get_doc("Sales Invoice", row.name) for row in invoices_for(CUSTOMER)]
		self.assertEqual(len(invoices), 3)
		for invoice in invoices:
			self.assertEqual(invoice.docstatus, 1)
		sink_a = next(i for i in invoices if i.subscription == a.name)
		self.assertEqual([d.subscription for d in sink_a.items], [a.name])
		period_b, arrears_b = sorted(
			(i for i in invoices if i.subscription == b.name), key=lambda i: getdate(i.from_date)
		)
		self.assert_date(period_b.from_date, start)
		self.assert_date(period_b.to_date, add_days(today, -1))
		self.assertEqual([d.subscription for d in period_b.items], [b.name])
		b.reload()
		self.assertEqual(b.status, "Cancelled")
		self.assert_date(b.cancelation_date, today)
		self.assert_date(arrears_b.from_date, b.current_invoice_start)  # the arrears window core passes
		self.assert_date(arrears_b.to_date, today)
		self.assertEqual([d.subscription for d in arrears_b.items], [b.name])
		a.reload()
		self.assert_date(a.current_invoice_start, add_months(today, 1))

		# stock parity: the same sub for a non-consolidating customer yields the same two invoices
		b2 = self.make_sub(
			party=CUSTOMER_2,
			identifier="b",
			start_date=start,
			generate_invoice_at="End of the current subscription period",
			cancel_at_period_end=1,
		)
		with frozen_today(today):
			b2.process(posting_date=today)
		stock = [frappe.get_doc("Sales Invoice", row.name) for row in invoices_for(CUSTOMER_2)]
		self.assertEqual(
			sorted((getdate(i.from_date), getdate(i.to_date), getdate(i.posting_date)) for i in stock),
			sorted(
				(getdate(i.from_date), getdate(i.to_date), getdate(i.posting_date))
				for i in (period_b, arrears_b)
			),
		)
		b2.reload()
		self.assertEqual(b2.status, "Cancelled")

	# ---- review round: an end_date member's final period is billed exactly once
	def test_end_date_member_billed_once_for_final_period(self):
		# pinned (see test 5): P2 must start exactly today or the final-period scenario collapses
		b_start, today = "2026-01-10", "2026-02-10"
		a = self.make_sub(start_date=today, identifier="a")
		b = self.make_sub(
			identifier="b",
			start_date=b_start,
			end_date=add_to_date(today, months=1, days=-1),  # must exceed one cycle from start
			cancel_at_period_end=0,
		)
		with frozen_today(b_start):
			run_consolidated_billing(b_start, party=CUSTOMER)  # B's first period alone; A is not due yet
		self.assertEqual(len(invoices_for(CUSTOMER)), 1)
		b.reload()
		self.assert_date(b.current_invoice_start, today)

		frappe.local.consolidated_billing = None
		with frozen_today(today):
			run_consolidated_billing(today, party=CUSTOMER)
		invoices = invoices_for(CUSTOMER)
		self.assertEqual(len(invoices), 2)
		self.assertEqual(
			[d.subscription for d in frappe.get_doc("Sales Invoice", invoices[1].name).items],
			[a.name, b.name],
		)

		for day in (1, 2):
			frappe.local.consolidated_billing = None
			with frozen_today(add_days(today, day)):
				self.assertEqual(run_consolidated_billing(add_days(today, day), party=CUSTOMER), [])
		self.assertEqual(len(invoices_for(CUSTOMER)), 2)
		lines = frappe.get_all(
			"Sales Invoice Item",
			{"subscription": b.name, "subscription_period_start": today, "docstatus": ("<", 2)},
			pluck="name",
		)
		self.assertEqual(len(lines), 1)
		b.reload()
		self.assert_date(b.current_invoice_start, today)  # final period: not advanced, as stock
		self.assertNotEqual(b.status, "Cancelled")

	# ---- review round: a fixed additional discount is carried once per member
	def test_money_conservation_with_fixed_discount(self):
		discount = {"additional_discount_amount": 10, "fields": {"apply_additional_discount": "Grand Total"}}
		for i in (1, 2, 3):
			self.make_sub(identifier=f"cons-{i}", **discount)
		standalone = [self.make_sub(party=CUSTOMER_2, identifier=f"std-{i}", **discount) for i in (1, 2, 3)]

		preview = preview_consolidated_billing(self.today, CUSTOMER)
		self.assertEqual(len(preview), 1)
		self.assertEqual(preview[0]["grand_total"], 2670)

		run_consolidated_billing(self.today, party=CUSTOMER)
		for sub in standalone:
			sub.process(posting_date=self.today)

		consolidated = self.single_invoice()
		self.assertEqual(len(consolidated.items), 3)
		self.assertEqual(flt(consolidated.discount_amount), 30)
		invoices = [frappe.get_doc("Sales Invoice", row.name) for row in invoices_for(CUSTOMER_2)]
		self.assertEqual(len(invoices), 3)
		self.assertEqual([flt(i.discount_amount) for i in invoices], [10, 10, 10])
		self.assertEqual(flt(consolidated.grand_total, 2), flt(sum(i.grand_total for i in invoices), 2))
		self.assertEqual(consolidated.grand_total, 2670)
		self.assertEqual(
			flt(consolidated.outstanding_amount, 2), flt(sum(i.outstanding_amount for i in invoices), 2)
		)

	# ---- review round R2-01: a header-relevant edit between the scan and the lock leaves the row for the next run
	def test_row_regrouped_between_scan_and_lock_is_not_absorbed(self):
		a = self.make_sub(identifier="a")
		b = self.make_sub(identifier="b")
		original_group_due = runner_module._group_due

		def group_due_then_edit(names, run_posting_date):
			groups = original_group_due(names, run_posting_date)
			# a desk save lands inside the scan -> lock window: B now carries a tax template
			frappe.db.set_value("Subscription", b.name, "sales_tax_template", TAX_TEMPLATE)
			return groups

		with patch.object(runner_module, "_group_due", side_effect=group_due_then_edit):
			results = run_consolidated_billing(self.today, party=CUSTOMER)

		self.assertEqual(len(results), 1)
		self.assertEqual(results[0]["subscriptions"], [a.name])
		self.assertEqual(results[0]["skipped"], [{"subscription": b.name, "reason": "regrouped"}])
		invoice = self.single_invoice()
		self.assertEqual([d.subscription for d in invoice.items], [a.name])
		self.assertEqual(invoice.grand_total, 900)
		self.assertIsNone(invoice.taxes_and_charges or None)
		b.reload()
		self.assert_date(b.current_invoice_start, self.today)  # untouched, waits for the next run

		# the next run bills B under its new key: its own taxed invoice
		frappe.local.consolidated_billing = None
		run_consolidated_billing(self.today, party=CUSTOMER)
		invoices = [frappe.get_doc("Sales Invoice", row.name) for row in invoices_for(CUSTOMER)]
		self.assertEqual(len(invoices), 2)
		taxed = next(i for i in invoices if i.taxes_and_charges)
		self.assertEqual([d.subscription for d in taxed.items], [b.name])
		self.assertEqual(taxed.taxes_and_charges, TAX_TEMPLATE)
		self.assertEqual(taxed.net_total, 900)
		self.assertEqual(taxed.subscription, b.name)

	# ---- review round R2-02: a candidate that cannot be evaluated is skipped, never the whole run
	def test_scan_error_skips_only_that_subscription(self):
		create_plan(plan_name="_Test AS Week Plan", cost=100, billing_interval="Week", currency="INR")
		y = self.make_sub(identifier="y")
		x = self.make_sub(plan="_Test AS Week Plan", identifier="x")  # inserted while un-anchored
		# anchoring afterwards: the Week plan cannot be realigned, so every scan of X raises
		frappe.db.set_value("Customer", CUSTOMER, "subscription_billing_anchor_mode", "Calendar")
		frappe.clear_document_cache("Customer", CUSTOMER)
		scan_title = f"Consolidated billing scan failed: {x.name}"
		before_scan = error_log_count(scan_title)
		before_group = error_log_count(GROUP_FAILURE_TITLE)

		results = run_consolidated_billing(self.today)  # book-wide

		self.assertEqual(len(results), 2)
		failed = next(r for r in results if r["invoice"] is None)
		self.assertEqual(failed["subscriptions"], [x.name])
		self.assertEqual(failed["party"], CUSTOMER)
		self.assertIn("cannot anchor a Week plan", failed["errors"])
		billed = next(r for r in results if r["invoice"] is not None)
		self.assertEqual(billed["subscriptions"], [y.name])
		self.assertEqual(len(billed["lines"]), 1)
		invoice = self.single_invoice()
		self.assertEqual(invoice.subscription, y.name)
		self.assertEqual([d.subscription for d in invoice.items], [y.name])
		self.assertEqual(error_log_count(scan_title), before_scan + 1)
		self.assertEqual(error_log_count(GROUP_FAILURE_TITLE), before_group)
		x.reload()
		self.assert_date(x.current_invoice_start, self.today)  # untouched

		# the scheduler path with fresh subs (X, Y above stay: X keeps failing the scan, Y is not due any more):
		# X2's own process() raises (logged by process_all), Y2 is billed through the runner
		frappe.local.consolidated_billing = None
		frappe.db.set_value("Customer", CUSTOMER, "subscription_billing_anchor_mode", "")
		frappe.clear_document_cache("Customer", CUSTOMER)
		y2 = self.make_sub(identifier="y2")
		x2 = self.make_sub(plan="_Test AS Week Plan", identifier="x2")  # before_insert raises once anchored
		frappe.db.set_value("Customer", CUSTOMER, "subscription_billing_anchor_mode", "Calendar")
		frappe.clear_document_cache("Customer", CUSTOMER)
		before_sub = error_log_count("Subscription failed")
		with patch.object(frappe.db, "rollback", rollback_savepoints_only(frappe.db.rollback)):
			process_all([x2.name, y2.name], self.today)
		invoices = invoices_for(CUSTOMER)
		self.assertEqual(len(invoices), 2)
		second = frappe.get_doc("Sales Invoice", invoices[1].name)
		self.assertEqual([d.subscription for d in second.items], [y2.name])
		self.assertEqual(error_log_count("Subscription failed"), before_sub + 1)  # X2 only
		self.assertEqual(error_log_count(scan_title), before_scan + 2)  # X, on both scans
		self.assertEqual(
			frappe.local.consolidated_billing.done, {(COMPANY, "Customer", CUSTOMER, self.today): "ok"}
		)
		y2.reload()
		self.assertGreater(getdate(y2.current_invoice_start), getdate(self.today))
		for sub in (x, x2):
			sub.reload()
			self.assert_date(sub.current_invoice_start, self.today)
			self.assertEqual(frappe.get_all("Sales Invoice Item", {"subscription": sub.name}), [])

	# ---- review round R2-04: one submitted Process Subscription is one run (memo cleared per document)
	def _submit_process_subscription(self, customer=CUSTOMER, posting_date=None):
		with (
			patch("frappe.enqueue"),
			patch.object(frappe.db, "rollback", rollback_savepoints_only(frappe.db.rollback)),
		):
			frappe.get_doc(
				{
					"doctype": "Process Subscription",
					"posting_date": posting_date or self.today,
					"customer": customer,
				}
			).submit()

	def test_process_subscription_resubmit_bills_next_period(self):
		start, today = "2026-01-01", "2026-02-15"  # 45 days behind, pinned like test 5
		a = self.make_sub(start_date=start, identifier="a")
		b = self.make_sub(start_date=start, identifier="b")
		with frozen_today(today):
			self._submit_process_subscription(posting_date=today)
			self.assertEqual(len(invoices_for(CUSTOMER)), 1)
			self._submit_process_subscription(posting_date=today)  # same session, memo NOT reset by the test
		invoices = invoices_for(CUSTOMER)
		self.assertEqual(len(invoices), 2)
		self.assertTrue(all(len(frappe.get_doc("Sales Invoice", i.name).items) == 2 for i in invoices))
		self.assert_date(invoices[0].from_date, start)
		self.assert_date(invoices[1].from_date, add_months(start, 1))
		for sub in (a, b):
			sub.reload()
			self.assert_date(sub.current_invoice_start, add_months(start, 2))

	def test_process_subscription_resubmit_retries_failed_group(self):
		a = self.make_sub(identifier="a")
		b = self.make_sub(identifier="b")
		with patch.object(MixinSubscription, "_absorb_into", side_effect=IndexError("boom")):
			self._submit_process_subscription()
		self.assertEqual(invoices_for(CUSTOMER), [])
		self.assertEqual(
			frappe.local.consolidated_billing.done, {(COMPANY, "Customer", CUSTOMER, self.today): "failed"}
		)
		before_sub = error_log_count("Subscription failed")

		self._submit_process_subscription()  # cause fixed: the memo must not keep the party failed

		invoice = self.single_invoice()
		self.assertEqual([d.subscription for d in invoice.items], [a.name, b.name])
		self.assertEqual(error_log_count("Subscription failed"), before_sub)
		self.assertEqual(
			frappe.local.consolidated_billing.done, {(COMPANY, "Customer", CUSTOMER, self.today): "ok"}
		)

	# ---- review round R3-05 / R2-07: a party with nothing due is scanned once per batch, not once per sibling
	def test_party_with_nothing_due_is_scanned_once(self):
		future = add_days(self.today, 5)
		subs = [self.make_sub(start_date=future, identifier=f"s-{i}") for i in (1, 2, 3)]
		with (
			patch.object(
				runner_module, "_candidate_subscriptions", wraps=runner_module._candidate_subscriptions
			) as scan,
			patch.object(frappe.db, "rollback", rollback_savepoints_only(frappe.db.rollback)),
		):
			process_all([s.name for s in subs], self.today)
		self.assertEqual(scan.call_count, 1)
		self.assertEqual(
			frappe.local.consolidated_billing.done, {(COMPANY, "Customer", CUSTOMER, self.today): "ok"}
		)
		self.assertEqual(invoices_for(CUSTOMER), [])
		for sub in subs:
			sub.reload()
			self.assert_date(sub.current_invoice_start, future)

	# ---- review round R3-04: the documented stock-path deviation of the line fallback
	def test_stock_days_before_final_period_billed_once(self):
		"""Non-consolidating, non-anchored customer, "Days before" with an end_date that makes the second period
		final: core's header posting_date (trigger = start - N, outside the period) means core would re-bill that
		period on every later run inside its one-cycle window; the line fallback bills it once (DECISIONS.md D-10).
		Pinned dates: core requires end_date > start + one cycle, so the final period is the second one."""
		start, end, today = "2026-03-10", "2026-04-20", "2026-04-05"
		p2_start = "2026-04-10"  # P1 = 03-10..04-09, P2 = 04-10..04-20 (clamped by end_date, final)
		with frozen_today(start):
			sub = create_subscription(
				party=CUSTOMER_2,
				start_date=start,
				end_date=end,
				generate_invoice_at="Days before the current subscription period",
				number_of_days=10,
				generate_new_invoices_past_due_date=1,
				submit_invoice=1,
				days_until_due=15,
			)  # no in_import: after_insert's catch-up bills P1 (trigger 02-28) and advances to P2
		self.assertEqual(len(invoices_for(CUSTOMER_2)), 1)
		self.assert_date(sub.current_invoice_start, p2_start)
		self.assert_date(sub.current_invoice_end, end)

		with frozen_today(today):  # P2's trigger (03-31) has passed: the final period is billed once
			sub.process(posting_date=today)
		invoices = invoices_for(CUSTOMER_2)
		self.assertEqual(len(invoices), 2)
		self.assertEqual({i.subscription for i in invoices}, {sub.name})
		final = frappe.get_doc("Sales Invoice", invoices[1].name)
		self.assert_date(
			final.posting_date, today
		)  # header date outside the period: core's rule cannot see it
		self.assert_date(final.items[0].subscription_period_start, p2_start)
		self.assert_date(final.to_date, end)

		for day in (1, 2):
			sub.reload()
			with frozen_today(add_days(today, day)):
				sub.process(posting_date=add_days(today, day))
		self.assertEqual(len(invoices_for(CUSTOMER_2)), 2)
		sub.reload()
		self.assert_date(sub.current_invoice_start, p2_start)  # final period: never advanced, as stock
		self.assertNotEqual(sub.status, "Cancelled")
		self.assertIsNone(frappe.local.consolidated_billing)
