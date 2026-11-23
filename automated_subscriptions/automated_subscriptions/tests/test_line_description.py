# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

import unittest
from datetime import date
from types import SimpleNamespace

from automated_subscriptions.automated_subscriptions.custom.subscription import AutoSubSubscription


class _Subscription:
	"""Just what _enrich_items reads from a Subscription."""

	party_type = "Customer"
	name = "SUB-TEST-0001"

	def __init__(self, identifier=None, party_type="Customer"):
		self._identifier = identifier
		self.party_type = party_type

	def get(self, fieldname, default=None):
		return self._identifier if fieldname == "service_identifier" else default

	def _billing_window(self):
		return date(2026, 1, 1), date(2026, 1, 31)


def _enrich(subscription, plans):
	items = [{"item_code": plan.plan, "qty": 1, "rate": 10} for plan in plans]
	return AutoSubSubscription._enrich_items(subscription, plans, items)


class TestLineDescriptions(unittest.TestCase):
	def test_each_line_takes_the_description_of_its_own_plan_row(self):
		plans = [
			SimpleNamespace(plan="DR-COM", description="first.example"),
			SimpleNamespace(plan="DR-COM", description="second.example"),
			SimpleNamespace(plan="DR-COM", description="third.example"),
		]
		items = _enrich(_Subscription(), plans)
		self.assertEqual(
			[item["description"] for item in items],
			["first.example", "second.example", "third.example"],
		)

	def test_service_identifier_wins_over_the_row_description(self):
		plans = [SimpleNamespace(plan="DR-COM", description="first.example")]
		items = _enrich(_Subscription(identifier="Site 3"), plans)
		self.assertEqual(items[0]["description"], "Site 3")

	def test_blank_row_description_leaves_the_item_description_alone(self):
		plans = [SimpleNamespace(plan="DR-COM", description="   ")]
		items = _enrich(_Subscription(), plans)
		self.assertNotIn("description", items[0])

	def test_cleared_text_editor_counts_as_blank(self):
		# what the Text Editor stores after the field is cleared
		cleared = '<div class="ql-editor read-mode"><p><br></p></div>'
		for text in (cleared, "<p>&nbsp;</p>", "<p></p>"):
			with self.subTest(text=text):
				items = _enrich(_Subscription(), [SimpleNamespace(plan="DR-COM", description=text)])
				self.assertNotIn("description", items[0])

	def test_row_without_the_description_field_is_fine(self):
		plans = [SimpleNamespace(plan="DR-COM")]
		items = _enrich(_Subscription(), plans)
		self.assertNotIn("description", items[0])
		self.assertEqual(items[0]["subscription_plan"], "DR-COM")

	def test_row_html_is_sanitized(self):
		plans = [SimpleNamespace(plan="DR-COM", description="<p>first.example</p><script>alert(1)</script>")]
		items = _enrich(_Subscription(), plans)
		self.assertIn("first.example", items[0]["description"])
		self.assertNotIn("<script", items[0]["description"])

	def test_period_and_links_are_still_set_on_every_line(self):
		plans = [
			SimpleNamespace(plan="DR-COM", description="first.example"),
			SimpleNamespace(plan="DR-COM", description="third.example"),
		]
		items = _enrich(_Subscription(), plans)
		for item in items:
			self.assertEqual(item["subscription"], "SUB-TEST-0001")
			self.assertEqual(item["subscription_period_start"], date(2026, 1, 1))
			self.assertEqual(item["subscription_period_end"], date(2026, 1, 31))

	def test_non_customer_subscriptions_are_not_touched(self):
		plans = [SimpleNamespace(plan="DR-COM", description="first.example")]
		items = _enrich(_Subscription(identifier="Site 3", party_type="Supplier"), plans)
		self.assertNotIn("description", items[0])
		self.assertNotIn("subscription", items[0])
