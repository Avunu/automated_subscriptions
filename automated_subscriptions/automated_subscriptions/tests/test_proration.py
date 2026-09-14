from datetime import date
from unittest.mock import patch

import frappe
from erpnext.accounts.doctype.subscription import subscription as core_subscription
from erpnext.accounts.doctype.subscription.test_subscription import (
	create_parties,
	create_plan,
	create_subscription,
	make_plans,
	reset_settings,
)
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, add_to_date, flt, getdate, nowdate

from automated_subscriptions.automated_subscriptions.billing.anchor import Grid, period_end
from automated_subscriptions.automated_subscriptions.billing.exceptions import (
	BackdatedStartNotSupported,
	UnsupportedBillingGrid,
	ZeroPlanRate,
)
from automated_subscriptions.automated_subscriptions.tests.utils import (
	frozen_today,
	make_anchored_customer,
)

CALENDAR = "_Test AS Calendar Customer"
CALENDAR_YEAR = "_Test AS Calendar Year Customer"
DAY31 = "_Test AS Day31 Customer"
YEAR_PLAN = "_Test AS Year Plan"
MONTH_PLAN = "_Test Plan Name"  # 900 / Month, Fixed Rate, INR
CORE_PRORATA = "erpnext.accounts.doctype.subscription.subscription.get_prorata_factor"
BEGINNING = "Beginning of the current subscription period"


def invoice_for(sub):
	return frappe.get_doc(
		"Sales Invoice", frappe.get_all("Sales Invoice", {"subscription": sub.name}, pluck="name")[0]
	)


def invoices_for(sub):
	return [
		frappe.get_doc("Sales Invoice", name)
		for name in frappe.get_all(
			"Sales Invoice", {"subscription": sub.name}, order_by="from_date asc", pluck="name"
		)
	]


def set_setting(fieldname, value):
	frappe.db.set_single_value("Subscription Settings", fieldname, value)


class TestProration(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		make_plans()
		create_parties()
		reset_settings()
		create_plan(plan_name=YEAR_PLAN, cost=12000, billing_interval="Year", currency="INR")
		frappe.db.set_value("Company", "_Test Company", "accounts_frozen_till_date", None)
		set_setting("prorate", 1)
		set_setting("mid_term_billing_mode", "Immediate")
		set_setting("annual_discount_percentage", 25)
		frappe.flags.subscription_billing = None
		make_anchored_customer(CALENDAR, "Calendar")
		make_anchored_customer(CALENDAR_YEAR, "Calendar", interval="Year")
		make_anchored_customer(DAY31, "Anniversary", anchor_date="2026-01-31")

	def tearDown(self):
		frappe.db.rollback()
		super().tearDown()

	def anchored_subscription(self, party, plan, start, today=None, **kwargs):
		"""Insert an anchored subscription with today frozen at its start date (or `today`)."""
		kwargs.setdefault("days_until_due", 15)  # due date in the future -> status stays Active
		with frozen_today(today or start):
			return create_subscription(
				party=party,
				plans=[{"plan": plan, "qty": 1}],
				start_date=start,
				generate_invoice_at=BEGINNING,
				submit_invoice=1,
				**kwargs,
			)

	def assert_period(self, doc, start, end, start_field="from_date", end_field="to_date"):
		self.assertEqual(getdate(doc.get(start_field)), getdate(start))
		self.assertEqual(getdate(doc.get(end_field)), getdate(end))

	def assert_current_period(self, sub, start, end):
		self.assert_period(sub, start, end, "current_invoice_start", "current_invoice_end")

	# ---- 1
	def test_stub_period_and_prorated_rate_on_insert(self):
		sub = self.anchored_subscription(CALENDAR, YEAR_PLAN, "2026-04-15")

		self.assert_current_period(sub, "2027-01-01", "2027-12-31")
		self.assertEqual(len(invoices_for(sub)), 1)
		invoice = invoice_for(sub)
		self.assert_period(invoice, "2026-04-15", "2026-12-31")
		line = invoice.items[0]
		self.assertEqual(line.rate, 8580.82)  # 12000 * 261 / 365
		self.assertEqual(line.subscription, sub.name)
		self.assertEqual(line.subscription_plan, YEAR_PLAN)
		self.assert_period(
			line, "2026-04-15", "2026-12-31", "subscription_period_start", "subscription_period_end"
		)
		self.assertIsNone(line.service_start_date)
		self.assertIsNone(line.service_end_date)
		self.assertEqual(invoice.ignore_pricing_rule, 1)
		self.assertEqual(invoice.docstatus, 1)
		self.assertEqual(getdate(invoice.posting_date), date(2026, 4, 15))
		self.assertEqual(sub.status, "Active")

	# ---- 2
	def test_full_period_is_exact(self):
		sub = self.anchored_subscription(CALENDAR, YEAR_PLAN, "2026-01-01")

		invoice = invoice_for(sub)
		self.assert_period(invoice, "2026-01-01", "2026-12-31")
		self.assertEqual(invoice.items[0].rate, 12000.0)
		self.assert_current_period(sub, "2027-01-01", "2027-12-31")

	# ---- 3
	def test_month_plan_under_year_grid_with_discount(self):
		set_setting("annual_discount_percentage", 25)

		sub = self.anchored_subscription(CALENDAR_YEAR, MONTH_PLAN, "2026-04-15")
		invoice = invoice_for(sub)
		self.assert_period(invoice, "2026-04-15", "2026-12-31")
		self.assertEqual(invoice.items[0].rate, flt(900 * 261 / 365 * 12 * 0.75, 2))
		self.assertEqual(invoice.items[0].rate, 5792.05)
		self.assert_current_period(sub, "2027-01-01", "2027-12-31")

		sub = self.anchored_subscription(CALENDAR_YEAR, MONTH_PLAN, "2026-01-01")
		self.assertEqual(invoice_for(sub).items[0].rate, 8100.0)

		set_setting("annual_discount_percentage", 0)
		sub = self.anchored_subscription(CALENDAR_YEAR, MONTH_PLAN, "2026-01-01")
		self.assertEqual(invoice_for(sub).items[0].rate, 10800.0)

	# ---- 4
	def test_year_plan_under_year_grid_takes_no_discount(self):
		set_setting("annual_discount_percentage", 25)
		sub = self.anchored_subscription(CALENDAR_YEAR, YEAR_PLAN, "2026-01-01")
		self.assertEqual(invoice_for(sub).items[0].rate, 12000.0)

	# ---- 5
	def test_month_grid_day_31_anchor(self):
		sub = self.anchored_subscription(DAY31, MONTH_PLAN, "2026-02-10")

		invoice = invoice_for(sub)
		self.assert_period(invoice, "2026-02-10", "2026-02-27")
		self.assertEqual(invoice.items[0].rate, flt(900 * 18 / 28, 2))
		self.assertEqual(invoice.items[0].rate, 578.57)
		self.assert_period(
			invoice.items[0],
			"2026-02-10",
			"2026-02-27",
			"subscription_period_start",
			"subscription_period_end",
		)
		self.assert_current_period(sub, "2026-02-28", "2026-03-30")

	# ---- 6
	def test_prorate_setting_off_bills_full_units(self):
		set_setting("prorate", 0)
		sub = self.anchored_subscription(CALENDAR, YEAR_PLAN, "2026-04-15")

		invoice = invoice_for(sub)
		self.assert_period(invoice, "2026-04-15", "2026-12-31")
		self.assertEqual(invoice.items[0].rate, 12000.0)

	# ---- 7
	def test_trial_branch_preserved(self):
		today = "2026-04-15"
		trial_end = add_days(today, 30)  # 2026-05-15
		with frozen_today(today):
			sub = create_subscription(
				party=CALENDAR,
				plans=[{"plan": YEAR_PLAN, "qty": 1}],
				start_date=today,
				trial_period_start=today,
				trial_period_end=trial_end,
				generate_invoice_at=BEGINNING,
				submit_invoice=1,
				days_until_due=15,
			)

		self.assertEqual(getdate(sub.current_invoice_start), getdate(add_days(trial_end, 1)))
		self.assertEqual(
			getdate(sub.current_invoice_end),
			period_end(Grid(date(2000, 1, 1), "Year"), getdate(add_days(trial_end, 1))),
		)
		self.assert_current_period(sub, "2026-05-16", "2026-12-31")
		self.assertEqual(sub.status, "Trialing")
		self.assertEqual(invoices_for(sub), [])

	# ---- 8
	def test_end_date_clamp_on_second_period(self):
		# generate_new_invoices_past_due_date lets the second period bill while the stub is still unpaid
		sub = self.anchored_subscription(
			CALENDAR, YEAR_PLAN, "2026-04-15", end_date="2027-06-30", generate_new_invoices_past_due_date=1
		)
		first = invoice_for(sub)
		self.assert_period(first, "2026-04-15", "2026-12-31")
		self.assertEqual(first.items[0].rate, 8580.82)
		self.assert_current_period(sub, "2027-01-01", "2027-06-30")

		sub.reload()
		with frozen_today("2027-01-01"):
			sub.process(posting_date="2027-01-01")

		invoices = invoices_for(sub)
		self.assertEqual(len(invoices), 2)
		second = invoices[1]
		self.assert_period(second, "2027-01-01", "2027-06-30")
		self.assertEqual(second.items[0].rate, flt(12000 * 181 / 365, 2))
		self.assertEqual(second.items[0].rate, 5950.68)
		self.assert_period(
			second.items[0],
			"2027-01-01",
			"2027-06-30",
			"subscription_period_start",
			"subscription_period_end",
		)

	# ---- 9
	def test_stock_path_untouched_and_uses_core_prorata(self):
		start = nowdate()
		sub = create_subscription(
			start_date=start,
			generate_invoice_at="End of the current subscription period",
			submit_invoice=1,
		)  # _Test Customer: no anchor
		self.assertIsNone(sub._billing_profile())
		end = add_to_date(start, months=1, days=-1)
		self.assertEqual(getdate(sub.current_invoice_end), getdate(end))

		# "End of the current subscription period" only fires at the period end (core can_generate_new_invoice:
		# posting < trigger -> False), so process at the period end with today frozen there
		original = core_subscription.get_prorata_factor
		with frozen_today(end), patch(CORE_PRORATA, wraps=original) as m:
			sub.process(posting_date=end)
		m.assert_called()
		self.assertEqual(m.call_args.args[0], end)
		self.assertEqual(m.call_args.args[1], start)

		invoice = invoice_for(sub)
		self.assert_period(invoice, start, end)
		self.assertEqual(invoice.items[0].rate, 900.0)  # core factor = 1 at the period end
		self.assertEqual(invoice.ignore_pricing_rule, 0)
		line = invoice.items[0]
		self.assertEqual(line.subscription, sub.name)
		self.assertEqual(line.subscription_plan, MONTH_PLAN)
		self.assertEqual(getdate(line.subscription_period_start), getdate(start))
		self.assertEqual(getdate(line.subscription_period_end), getdate(end))
		# stock period roll-over after billing
		self.assert_current_period(sub, add_to_date(start, months=1), add_to_date(start, months=2, days=-1))

	# ---- 10
	def test_core_prorata_factor_not_in_anchored_path(self):
		with patch(CORE_PRORATA, side_effect=AssertionError("must not run")) as m:
			sub = self.anchored_subscription(CALENDAR, YEAR_PLAN, "2026-04-15")
			self.assertEqual(invoice_for(sub).items[0].rate, 8580.82)
			sub = self.anchored_subscription(DAY31, MONTH_PLAN, "2026-02-10")
			self.assertEqual(invoice_for(sub).items[0].rate, 578.57)
		m.assert_not_called()

	# ---- 11
	def test_pricing_rule_does_not_defeat_proration(self):
		self.make_pricing_rule("_Test AS Rule Calendar", CALENDAR)
		sub = self.anchored_subscription(CALENDAR, YEAR_PLAN, "2026-04-15")
		invoice = invoice_for(sub)
		self.assertEqual(invoice.items[0].rate, 8580.82)
		self.assertEqual(invoice.ignore_pricing_rule, 1)

		# control: the stock path keeps Pricing Rules (documents stock behaviour)
		self.make_pricing_rule("_Test AS Rule Stock", "_Test Customer")
		today = nowdate()
		sub = create_subscription(
			plans=[{"plan": YEAR_PLAN, "qty": 1}],
			start_date=today,
			generate_invoice_at=BEGINNING,
			submit_invoice=1,
			days_until_due=15,
		)
		invoice = invoice_for(sub)
		self.assertEqual(invoice.ignore_pricing_rule, 0)
		self.assertEqual(invoice.items[0].rate, 2000.0)

	def make_pricing_rule(self, title, customer):
		rule = frappe.get_doc(
			{
				"doctype": "Pricing Rule",
				"title": title,
				"company": "_Test Company",
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

	# ---- 12
	def test_next_daily_run_defers_catch_up(self):
		set_setting("mid_term_billing_mode", "Next Daily Run")
		sub = self.anchored_subscription(CALENDAR, YEAR_PLAN, "2026-04-15")
		self.assertEqual(invoices_for(sub), [])
		self.assert_current_period(sub, "2026-04-15", "2026-12-31")

		sub.reload()
		with frozen_today("2026-04-15"):
			sub.process(posting_date="2026-04-15")
		invoice = invoice_for(sub)
		self.assert_period(invoice, "2026-04-15", "2026-12-31")
		self.assertEqual(invoice.items[0].rate, 8580.82)
		self.assertEqual(invoice.items[0].subscription, sub.name)
		self.assertEqual(invoice.ignore_pricing_rule, 1)
		self.assertEqual(invoice.docstatus, 1)
		self.assert_current_period(sub, "2027-01-01", "2027-12-31")

		set_setting("mid_term_billing_mode", "Immediate")
		sub = self.anchored_subscription(CALENDAR, YEAR_PLAN, "2026-04-15")
		self.assertEqual(len(invoices_for(sub)), 1)

		# stock catch-up untouched by the setting
		set_setting("mid_term_billing_mode", "Next Daily Run")
		sub = create_subscription(
			start_date=nowdate(), generate_invoice_at=BEGINNING, submit_invoice=1, days_until_due=15
		)
		self.assertEqual(len(invoices_for(sub)), 1)

	# ---- 13
	def test_backdated_start_rejected_when_deferred(self):
		set_setting("mid_term_billing_mode", "Next Daily Run")
		today = "2026-09-14"
		# more than one billing cycle past the first period end (core caps the late fire at
		# current_invoice_end + one cycle): Month plan 2025-06-30 + 1 month, Year plan 2024-12-31 + 1 year
		with self.assertRaises(BackdatedStartNotSupported):
			self.anchored_subscription(CALENDAR, MONTH_PLAN, "2025-06-01", today=today)
		with self.assertRaises(BackdatedStartNotSupported):
			self.anchored_subscription(CALENDAR, YEAR_PLAN, "2024-06-01", today=today)

		# inside the one-cycle window (2025-12-31 + 1 year >= today): accepted and left to the daily run
		sub = self.anchored_subscription(CALENDAR, YEAR_PLAN, "2025-06-01", today=today)
		self.assertEqual(invoices_for(sub), [])
		self.assert_current_period(sub, "2025-06-01", "2025-12-31")

		frappe.flags.in_import = True
		try:
			sub = self.anchored_subscription(CALENDAR, MONTH_PLAN, "2025-06-01", today=today)
		finally:
			frappe.flags.in_import = False
		self.assertEqual(invoices_for(sub), [])
		self.assert_current_period(sub, "2025-06-01", "2025-06-30")

		set_setting("mid_term_billing_mode", "Immediate")
		# generate_new_invoices_past_due_date: the stock catch-up loop stops after one invoice otherwise
		sub = self.anchored_subscription(
			CALENDAR, YEAR_PLAN, "2025-06-01", today=today, generate_new_invoices_past_due_date=1
		)
		invoices = invoices_for(sub)
		self.assertEqual(len(invoices), 2)
		self.assert_period(invoices[0], "2025-06-01", "2025-12-31")
		self.assertEqual(invoices[0].items[0].rate, flt(12000 * 214 / 365, 2))
		self.assert_period(invoices[1], "2026-01-01", "2026-12-31")
		self.assertEqual(invoices[1].items[0].rate, 12000.0)
		self.assert_current_period(sub, "2027-01-01", "2027-12-31")

	def test_backdated_start_guard_boundary_is_the_next_daily_run(self):
		# The daily job posts tomorrow at the earliest; core caps its late fire at current_invoice_end + one
		# cycle, so an insert on the cap day itself would be re-anchored (stub never billed) by the next run.
		set_setting("mid_term_billing_mode", "Next Daily Run")
		# stub 2025-12-15..2025-12-31, cap = 2026-01-30
		with self.assertRaises(BackdatedStartNotSupported):
			self.anchored_subscription(CALENDAR, MONTH_PLAN, "2025-12-15", today="2026-01-30")

		sub = self.anchored_subscription(CALENDAR, MONTH_PLAN, "2025-12-15", today="2026-01-29")
		self.assertEqual(invoices_for(sub), [])
		self.assert_current_period(sub, "2025-12-15", "2025-12-31")
		# the accepted boundary is still billable by the next run
		sub.reload()
		with frozen_today("2026-01-30"):
			sub.process(posting_date="2026-01-30")
		invoice = invoice_for(sub)
		self.assert_period(invoice, "2025-12-15", "2025-12-31")
		self.assertEqual(invoice.items[0].rate, flt(900 * 17 / 31, 2))
		self.assert_current_period(sub, "2026-01-01", "2026-01-31")

	def test_backdated_start_rejected_when_catch_up_stops_after_one_invoice(self):
		# Immediate mode, generate_new_invoices_past_due_date = 0 (the default): core's catch-up loop bills only
		# the stub and the daily run would then silently re-anchor the period to today
		today = "2026-09-14"
		with self.assertRaises(BackdatedStartNotSupported):
			self.anchored_subscription(CALENDAR, MONTH_PLAN, "2026-06-10", today=today)
		with self.assertRaises(BackdatedStartNotSupported):
			self.anchored_subscription(CALENDAR, MONTH_PLAN, "2025-06-01", today=today)

		# inside one cycle of the first period end (2026-08-31 + 1 month > today): accepted, stub only
		sub = self.anchored_subscription(CALENDAR, MONTH_PLAN, "2026-08-10", today=today)
		self.assertEqual(len(invoices_for(sub)), 1)
		self.assert_period(invoice_for(sub), "2026-08-10", "2026-08-31")
		self.assert_current_period(sub, "2026-09-01", "2026-09-30")
		# Year plan: 2025-12-31 + 1 year > today
		sub = self.anchored_subscription(CALENDAR, YEAR_PLAN, "2025-06-01", today=today)
		self.assertEqual(len(invoices_for(sub)), 1)
		self.assert_period(invoice_for(sub), "2025-06-01", "2025-12-31")
		self.assertEqual(invoice_for(sub).items[0].rate, flt(12000 * 214 / 365, 2))
		self.assert_current_period(sub, "2026-01-01", "2026-12-31")

		# flag 1: every elapsed period is billed on insert
		sub = self.anchored_subscription(
			CALENDAR, MONTH_PLAN, "2026-06-10", today=today, generate_new_invoices_past_due_date=1
		)
		self.assertEqual(
			[(getdate(i.from_date), getdate(i.to_date), i.items[0].rate) for i in invoices_for(sub)],
			[
				(getdate("2026-06-10"), getdate("2026-06-30"), 630.0),
				(getdate("2026-07-01"), getdate("2026-07-31"), 900.0),
				(getdate("2026-08-01"), getdate("2026-08-31"), 900.0),
				(getdate("2026-09-01"), getdate("2026-09-30"), 900.0),
			],
		)
		self.assert_current_period(sub, "2026-10-01", "2026-10-31")

		# stock control: never guarded
		with frozen_today(today):
			sub = create_subscription(
				start_date="2026-06-10", generate_invoice_at=BEGINNING, submit_invoice=1, days_until_due=15
			)
		self.assertIsNone(sub._billing_profile())
		self.assertEqual(len(invoices_for(sub)), 1)

	# ---- 14
	def test_follow_calendar_months_rejected_when_anchored(self):
		with frozen_today("2026-04-15"), self.assertRaises(UnsupportedBillingGrid):
			create_subscription(
				party=CALENDAR,
				plans=[{"plan": MONTH_PLAN, "qty": 1}],
				start_date="2026-04-15",
				end_date="2026-12-31",
				follow_calendar_months=1,
				generate_invoice_at=BEGINNING,
				submit_invoice=1,
				days_until_due=15,
			)

	# ---- 15
	def test_monthly_rate_plan_rejected(self):
		plan = "_Test AS Monthly Rate"
		create_plan(plan_name=plan, price_determination="Monthly Rate", cost=900, currency="INR")
		with self.assertRaises(UnsupportedBillingGrid):
			self.anchored_subscription(CALENDAR, plan, "2026-04-15")

		# rejected on the insert itself, not by the first (deferred) billing run
		set_setting("mid_term_billing_mode", "Next Daily Run")
		with self.assertRaises(UnsupportedBillingGrid):
			self.anchored_subscription(CALENDAR, plan, "2026-04-15")
		set_setting("mid_term_billing_mode", "Immediate")
		with self.assertRaises(UnsupportedBillingGrid):
			self.anchored_subscription(CALENDAR, plan, "2026-04-15", today="2026-04-01")  # future start

		# and on a later save that adds the plan (same billing cycle, so core's mixed-cycle check stays quiet)
		sub = self.anchored_subscription(CALENDAR, MONTH_PLAN, "2026-04-15")
		sub.append("plans", {"plan": plan, "qty": 1})
		with self.assertRaises(UnsupportedBillingGrid):
			sub.save()

		# stock customers keep core behaviour (Monthly Rate is a core option; core's own month math only
		# holds for a month-start date)
		with frozen_today("2026-04-01"):
			sub = create_subscription(
				plans=[{"plan": plan, "qty": 1}],
				start_date="2026-04-01",
				generate_invoice_at=BEGINNING,
				submit_invoice=1,
				days_until_due=15,
			)
		self.assertEqual(len(invoices_for(sub)), 1)

	# ---- 16
	def test_zero_rate_rejected(self):
		price_list = "_Test AS Empty Price List"
		if not frappe.db.exists("Price List", price_list):
			frappe.get_doc(
				{
					"doctype": "Price List",
					"price_list_name": price_list,
					"currency": "INR",
					"selling": 1,
					"enabled": 1,
				}
			).insert(ignore_permissions=True)
		plan_name = "_Test AS Price List"
		if not frappe.db.exists("Subscription Plan", plan_name):
			frappe.get_doc(
				{
					"doctype": "Subscription Plan",
					"plan_name": plan_name,
					"item": "_Test Non Stock Item",
					"price_determination": "Based On Price List",
					"price_list": price_list,
					"billing_interval": "Month",
					"billing_interval_count": 1,
					"currency": "INR",
				}
			).insert(ignore_permissions=True)
		self.assertFalse(
			frappe.db.exists("Item Price", {"item_code": "_Test Non Stock Item", "price_list": price_list})
		)

		with self.assertRaises(ZeroPlanRate):
			self.anchored_subscription(CALENDAR, plan_name, "2026-04-15")

	# ---- 17
	def test_due_date_from_template_when_days_until_due_zero(self):
		template = "_Test AS NET15"
		if not frappe.db.exists("Payment Terms Template", template):
			doc = frappe.new_doc("Payment Terms Template")
			doc.template_name = template
			doc.append(
				"terms",
				{"invoice_portion": 100, "credit_days": 15, "due_date_based_on": "Day(s) after invoice date"},
			)
			doc.insert(ignore_permissions=True)
		frappe.db.set_value("Customer", CALENDAR, "payment_terms", template)

		sub = self.anchored_subscription(CALENDAR, YEAR_PLAN, "2026-04-15", days_until_due=0)
		invoice = invoice_for(sub)
		self.assertEqual(getdate(invoice.posting_date), date(2026, 4, 15))
		self.assertEqual(getdate(invoice.due_date), getdate(add_days(invoice.posting_date, 15)))
		self.assertEqual(len(invoice.payment_schedule), 1)
		self.assertEqual(invoice.payment_terms_template, template)

		# a preset days_until_due row wins (stock precedence)
		sub = self.anchored_subscription(CALENDAR, YEAR_PLAN, "2026-04-15", days_until_due=5)
		invoice = invoice_for(sub)
		self.assertEqual(getdate(invoice.due_date), getdate(add_days(invoice.posting_date, 5)))
		self.assertEqual(len(invoice.payment_schedule), 1)

	# ---- 18
	def test_money_baseline(self):
		cases = (
			(CALENDAR, YEAR_PLAN, "2026-04-15", 8580.82),
			(CALENDAR_YEAR, MONTH_PLAN, "2026-04-15", 5792.05),
			(DAY31, MONTH_PLAN, "2026-02-10", 578.57),
		)
		for party, plan, start, rate in cases:
			sub = self.anchored_subscription(party, plan, start)
			invoice = invoice_for(sub)
			self.assertEqual(invoice.items[0].rate, rate)
			self.assertEqual(invoice.grand_total, flt(sum(i.rate * i.qty for i in invoice.items), 2))
			self.assertEqual(invoice.grand_total, rate)

	# ---- cancellation arrears (core cancel_subscription passes current_invoice_start..cancelation_date)
	def test_cancellation_arrears_is_prorated(self):
		for party in (CALENDAR, "_Test Customer"):
			with frozen_today("2026-01-01"):
				sub = create_subscription(
					party=party,
					plans=[{"plan": YEAR_PLAN, "qty": 1}],
					start_date="2026-01-01",
					generate_invoice_at="End of the current subscription period",
					submit_invoice=1,
					days_until_due=15,
				)
			self.assertEqual(invoices_for(sub), [])
			self.assert_current_period(sub, "2026-01-01", "2026-12-31")
			self.assertEqual(sub.status, "Active")

			with frozen_today("2026-03-31"):
				sub.cancel_subscription()

			invoices = invoices_for(sub)
			self.assertEqual(len(invoices), 1, party)
			invoice = invoices[0]
			self.assert_period(invoice, "2026-01-01", "2026-03-31")
			self.assertEqual(invoice.items[0].rate, flt(12000 * 90 / 365, 2), party)
			self.assertEqual(invoice.items[0].rate, 2958.9)
			self.assert_period(
				invoice.items[0],
				"2026-01-01",
				"2026-03-31",
				"subscription_period_start",
				"subscription_period_end",
			)
			self.assertEqual(sub.status, "Cancelled")
			self.assertNotIn("subscription_billing_window", sub.flags)

	# ---- anchor switched on after the sub exists (M5, DECISIONS.md Q-14)
	def test_anchor_switched_on_existing_sub_bills_stub(self):
		customer = make_anchored_customer("_Test AS Late Anchor Customer", "").name
		with frozen_today("2026-09-02"):
			sub = create_subscription(
				party=customer,
				plans=[{"plan": MONTH_PLAN, "qty": 1}],
				start_date="2026-09-02",
				generate_invoice_at=BEGINNING,
				submit_invoice=1,
				days_until_due=15,
				generate_new_invoices_past_due_date=1,  # the stub is still unpaid on the next run
			)
		self.assertIsNone(sub._billing_profile())
		self.assert_period(invoice_for(sub), "2026-09-02", "2026-10-01")  # stock
		self.assert_current_period(sub, "2026-10-02", "2026-11-01")

		frappe.db.set_value("Customer", customer, "subscription_billing_anchor_mode", "Calendar")
		frappe.clear_cache(doctype="Customer")
		sub.reload()
		self.assertIsNotNone(sub._billing_profile())
		with frozen_today("2026-10-02"):
			sub.process(posting_date="2026-10-02")

		invoices = invoices_for(sub)
		self.assertEqual(len(invoices), 2)
		stub = invoices[1]
		self.assert_period(stub, "2026-10-02", "2026-10-31")
		self.assertEqual(stub.items[0].rate, flt(900 * 30 / 31, 2))
		self.assertEqual(stub.items[0].rate, 870.97)
		self.assertEqual(getdate(stub.items[0].subscription_period_end), getdate("2026-10-31"))
		self.assert_current_period(sub, "2026-11-01", "2026-11-30")
		sub.reload()
		self.assert_current_period(sub, "2026-11-01", "2026-11-30")
		self.assertFalse(sub._realign_current_period())

		# an already aligned period (Anniversary anchor on the sub's own start day) is left alone
		make_anchored_customer("_Test AS Sep2 Customer", "Anniversary", anchor_date="2026-09-02")
		sub = self.anchored_subscription("_Test AS Sep2 Customer", MONTH_PLAN, "2026-09-02")
		self.assert_current_period(sub, "2026-10-02", "2026-11-01")
		self.assertFalse(sub._realign_current_period())
		self.assert_current_period(sub, "2026-10-02", "2026-11-01")

	# ---- blank plan row: a ValidationError, never an IndexError from the anchored period computation
	def test_blank_plan_row_reports_mandatory_error(self):
		with frozen_today("2026-04-15"), self.assertRaises(frappe.MandatoryError):
			frappe.get_doc(
				{
					"doctype": "Subscription",
					"party_type": "Customer",
					"party": CALENDAR,
					"company": "_Test Company",
					"start_date": "2026-04-15",
					"generate_invoice_at": BEGINNING,
					"plans": [{"plan": "", "qty": 1}],
				}
			).insert()
