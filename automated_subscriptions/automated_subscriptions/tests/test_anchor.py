from datetime import date, timedelta

import frappe
from frappe.tests import UnitTestCase

from automated_subscriptions.automated_subscriptions.billing.anchor import (
	Grid,
	period_end,
	prorate_factor,
	resolve_grid,
	term_bounds,
)
from automated_subscriptions.automated_subscriptions.billing.profile import (
	CALENDAR_ANCHOR,
	BillingProfile,
	profile_from_fields,
)

JAN1_YEAR = Grid(date(2026, 1, 1), "Year")
MAR15_YEAR = Grid(date(2026, 3, 15), "Year")
DAY31_MONTH = Grid(date(2026, 1, 31), "Month")
DAY1_MONTH = Grid(date(2026, 1, 1), "Month")
FEB29_YEAR = Grid(date(2024, 2, 29), "Year")
JAN1_QUARTER = Grid(date(2026, 1, 1), "Month", 3)
JAN31_QUARTER = Grid(date(2026, 1, 31), "Month", 3)
JAN1_TRIENNIAL = Grid(date(2024, 1, 1), "Year", 3)

# (grid, date, expected period_end, expected term_start, expected term days)
PERIOD_END_TABLE = (
	(JAN1_YEAR, date(2026, 4, 15), date(2026, 12, 31), date(2026, 1, 1), 365),
	(JAN1_YEAR, date(2027, 1, 1), date(2027, 12, 31), date(2027, 1, 1), 365),
	(JAN1_YEAR, date(2026, 12, 31), date(2026, 12, 31), date(2026, 1, 1), 365),
	(JAN1_YEAR, date(2025, 6, 1), date(2025, 12, 31), date(2025, 1, 1), 365),
	(MAR15_YEAR, date(2026, 4, 15), date(2027, 3, 14), date(2026, 3, 15), 365),
	(MAR15_YEAR, date(2026, 3, 14), date(2026, 3, 14), date(2025, 3, 15), 365),
	(DAY31_MONTH, date(2026, 1, 31), date(2026, 2, 27), date(2026, 1, 31), 28),
	(DAY31_MONTH, date(2026, 2, 10), date(2026, 2, 27), date(2026, 1, 31), 28),
	(DAY31_MONTH, date(2026, 2, 28), date(2026, 3, 30), date(2026, 2, 28), 31),
	(DAY31_MONTH, date(2026, 3, 31), date(2026, 4, 29), date(2026, 3, 31), 30),
	(DAY31_MONTH, date(2026, 4, 30), date(2026, 5, 30), date(2026, 4, 30), 31),
	(DAY31_MONTH, date(2026, 5, 31), date(2026, 6, 29), date(2026, 5, 31), 30),
	(DAY1_MONTH, date(2026, 2, 10), date(2026, 2, 28), date(2026, 2, 1), 28),
	(FEB29_YEAR, date(2025, 6, 1), date(2026, 2, 27), date(2025, 2, 28), 365),
	(FEB29_YEAR, date(2026, 2, 28), date(2027, 2, 27), date(2026, 2, 28), 365),
	(FEB29_YEAR, date(2028, 2, 28), date(2028, 2, 28), date(2027, 2, 28), 366),
	(FEB29_YEAR, date(2028, 2, 29), date(2029, 2, 27), date(2028, 2, 29), 365),
	(JAN1_QUARTER, date(2026, 5, 20), date(2026, 6, 30), date(2026, 4, 1), 91),
	(JAN31_QUARTER, date(2026, 5, 20), date(2026, 7, 30), date(2026, 4, 30), 92),
	(JAN1_TRIENNIAL, date(2026, 5, 20), date(2026, 12, 31), date(2024, 1, 1), 1096),
)

ANCHOR = date(2026, 3, 15)


class TestAnchor(UnitTestCase):
	def test_period_end_table(self):
		for grid, d, expected_end, expected_start, term_days in PERIOD_END_TABLE:
			with self.subTest(grid=grid, d=d):
				self.assertEqual(period_end(grid, d), expected_end)
				ts, te = term_bounds(grid, d)
				self.assertEqual((ts, te), (expected_start, expected_end))
				self.assertEqual((te - ts).days + 1, term_days)
				self.assertLessEqual(ts, d)
				self.assertLessEqual(d, te)

	def test_day_31_month_grid_does_not_drift(self):
		start = date(2026, 1, 31)
		starts = [start]
		for _ in range(5):
			start = period_end(DAY31_MONTH, start) + timedelta(days=1)
			starts.append(start)
		self.assertEqual(
			starts,
			[
				date(2026, 1, 31),
				date(2026, 2, 28),
				date(2026, 3, 31),
				date(2026, 4, 30),
				date(2026, 5, 31),
				date(2026, 6, 30),
			],
		)

	def test_boundary_date_gets_full_term(self):
		self.assertEqual(period_end(JAN1_YEAR, date(2027, 1, 1)), date(2027, 12, 31))
		self.assertEqual(period_end(DAY1_MONTH, date(2026, 2, 1)), date(2026, 2, 28))

	def test_date_before_anchor(self):
		self.assertEqual(period_end(JAN1_YEAR, date(2025, 6, 1)), date(2025, 12, 31))

	def test_prorate_factor_exact_full_terms(self):
		self.assertEqual(prorate_factor(JAN1_YEAR, date(2026, 1, 1), date(2026, 12, 31), 12, 25), 9.0)
		self.assertEqual(prorate_factor(JAN1_YEAR, date(2028, 1, 1), date(2028, 12, 31), 1), 1.0)
		self.assertEqual(
			prorate_factor(JAN1_YEAR, date(2026, 4, 15), date(2026, 12, 31), 12, 25, prorate=False), 9.0
		)

	def test_prorate_factor_stubs(self):
		self.assertAlmostEqual(
			prorate_factor(JAN1_YEAR, date(2026, 4, 15), date(2026, 12, 31), 12), 8.580821918, places=9
		)
		self.assertAlmostEqual(
			prorate_factor(JAN1_YEAR, date(2026, 4, 15), date(2026, 12, 31), 12, 25), 6.435616438, places=9
		)
		self.assertAlmostEqual(prorate_factor(MAR15_YEAR, date(2026, 4, 15), date(2027, 3, 14)), 334 / 365)
		self.assertAlmostEqual(prorate_factor(DAY1_MONTH, date(2026, 2, 10), date(2026, 2, 28)), 19 / 28)
		self.assertAlmostEqual(
			prorate_factor(JAN1_YEAR, date(2028, 3, 1), date(2028, 12, 31), 12), 306 / 366 * 12, places=9
		)
		self.assertAlmostEqual(prorate_factor(JAN1_QUARTER, date(2026, 5, 20), date(2026, 6, 30)), 42 / 91)

	def test_prorate_factor_preconditions(self):
		with self.assertRaises(ValueError):
			prorate_factor(JAN1_YEAR, date(2026, 4, 15), date(2027, 1, 5), 1)
		with self.assertRaises(ValueError):
			prorate_factor(JAN1_YEAR, date(2026, 4, 15), date(2026, 4, 14), 1)

	def test_resolve_grid_table(self):
		# blank / Month customer interval: the plan's own grid, no units multiplier, no discount
		for customer_interval in ("", "Month"):
			with self.subTest(customer_interval=customer_interval):
				self.assertEqual(
					resolve_grid(ANCHOR, customer_interval, "Month", 1, 25),
					(Grid(ANCHOR, "Month", 1), 1, 0.0),
				)
				self.assertEqual(
					resolve_grid(ANCHOR, customer_interval, "Month", 3, 25),
					(Grid(ANCHOR, "Month", 3), 1, 0.0),
				)
				self.assertEqual(
					resolve_grid(ANCHOR, customer_interval, "Year", 1, 25),
					(Grid(ANCHOR, "Year", 1), 1, 0.0),
				)
				self.assertEqual(
					resolve_grid(ANCHOR, customer_interval, "Year", 2, 25),
					(Grid(ANCHOR, "Year", 2), 1, 0.0),
				)
		# a plan_count of 0/None counts as 1
		self.assertEqual(resolve_grid(ANCHOR, "", "Month", 0, 25), (Grid(ANCHOR, "Month", 1), 1, 0.0))
		# Year customer + monthly-priced plan: yearly grid, 12 // n units, the annual discount
		self.assertEqual(resolve_grid(ANCHOR, "Year", "Month", 1, 25), (Grid(ANCHOR, "Year", 1), 12, 25))
		self.assertEqual(resolve_grid(ANCHOR, "Year", "Month", 3, 25), (Grid(ANCHOR, "Year", 1), 4, 25))
		self.assertEqual(resolve_grid(ANCHOR, "Year", "Month", 6, 10), (Grid(ANCHOR, "Year", 1), 2, 10))
		self.assertEqual(resolve_grid(ANCHOR, "Year", "Month", 12, 25), (Grid(ANCHOR, "Year", 1), 1, 25))
		# Year customer + yearly-priced plan: already yearly, no discount
		self.assertEqual(resolve_grid(ANCHOR, "Year", "Year", 1, 25), (Grid(ANCHOR, "Year", 1), 1, 0.0))
		self.assertEqual(resolve_grid(ANCHOR, "Year", "Year", 3, 25), (Grid(ANCHOR, "Year", 3), 1, 0.0))
		# unsupported combinations
		with self.assertRaises(ValueError):
			resolve_grid(ANCHOR, "", "Day", 14, 25)
		with self.assertRaises(ValueError):
			resolve_grid(ANCHOR, "Year", "Week", 1, 25)
		with self.assertRaises(ValueError):
			resolve_grid(ANCHOR, "Year", "Month", 5, 25)

	def test_profile_from_fields(self):
		calendar = profile_from_fields(
			frappe._dict(
				subscription_billing_anchor_mode="Calendar", subscription_billing_anchor_date="2026-03-15"
			)
		)
		self.assertEqual(calendar, BillingProfile("Calendar", CALENDAR_ANCHOR, ""))
		self.assertEqual(calendar.anchor, date(2000, 1, 1))

		anniversary = profile_from_fields(
			frappe._dict(
				subscription_billing_anchor_mode="Anniversary",
				subscription_billing_anchor_date="2026-03-15",
				subscription_billing_interval="Year",
			)
		)
		self.assertEqual(anniversary, BillingProfile("Anniversary", date(2026, 3, 15), "Year"))

		with self.assertRaises(ValueError):
			profile_from_fields(frappe._dict(subscription_billing_anchor_mode="Anniversary"))

		self.assertIsNone(profile_from_fields(frappe._dict(subscription_billing_anchor_mode="")))
		self.assertIsNone(profile_from_fields(frappe._dict()))
		self.assertIsNone(profile_from_fields(frappe._dict(subscription_billing_anchor_date="2026-03-15")))
