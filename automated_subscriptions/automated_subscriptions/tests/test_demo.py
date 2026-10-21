# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""The demo hook frappe-demo calls (`automated_subscriptions.demo.setup`)."""

import datetime

import frappe
from erpnext import get_default_company
from frappe.tests import IntegrationTestCase

from automated_subscriptions import demo

TODAY = datetime.date(2026, 1, 15)


class TestDemo(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		frappe.local.consolidated_billing = None
		self.ctx = {"today": TODAY, "seed": 1, "company": get_default_company(), "erpnext_demo": False}

	def tearDown(self):
		frappe.local.consolidated_billing = None
		frappe.db.rollback()
		super().tearDown()

	def invoices(self) -> list[str]:
		return frappe.get_all(
			"Sales Invoice", {"customer": demo.CUSTOMER, "docstatus": 1, "is_return": 0}, pluck="name"
		)

	def test_setup_bills_every_subscription_on_one_prorated_invoice(self):
		demo.setup(self.ctx)
		invoices = self.invoices()
		self.assertEqual(len(invoices), 1)
		lines = frappe.get_all(
			"Sales Invoice Item",
			{"parent": invoices[0], "parenttype": "Sales Invoice"},
			["description", "subscription", "subscription_period_start", "subscription_period_end", "rate"],
		)
		self.assertEqual(len(lines), len(demo.SUBSCRIPTIONS))
		self.assertEqual(
			{line.description for line in lines}, {identifier for _, _, identifier in demo.SUBSCRIPTIONS}
		)
		for line in lines:
			self.assertTrue(line.subscription)
			self.assertEqual(str(line.subscription_period_start), "2026-01-15")
			self.assertEqual(str(line.subscription_period_end), "2026-01-31")
		# January 15 to 31 of the 49.00 hosting plan: 17 of 31 days
		hosting = [line.rate for line in lines if line.description == "riverbenddental.com"]
		self.assertEqual(hosting, [round(49 * 17 / 31, 2)])

	def test_setup_is_idempotent(self):
		demo.setup(self.ctx)
		demo.setup(self.ctx)
		self.assertEqual(len(self.invoices()), 1)
		self.assertEqual(frappe.db.count("Customer", {"name": demo.CUSTOMER}), 1)
		self.assertEqual(frappe.db.count("Subscription", {"party": demo.CUSTOMER}), len(demo.SUBSCRIPTIONS))
