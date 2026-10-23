# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_field
from frappe.tests import IntegrationTestCase

from automated_subscriptions.patches.v1_2.drop_auto_billing_delay import CUSTOM_FIELD, execute


class TestDropAutoBillingDelay(IntegrationTestCase):
	def test_registered(self):
		with open(frappe.get_app_path("automated_subscriptions", "patches.txt")) as f:
			self.assertIn("automated_subscriptions.patches.v1_2.drop_auto_billing_delay", f.read().split())

	def test_removes_the_field_and_its_value(self):
		"""A site migrated from an older version still has the field and a stored value; the patch drops both."""
		create_custom_field(
			"Subscription Settings",
			{
				"fieldname": "auto_billing_delay",
				"fieldtype": "Int",
				"label": "Auto-billing Delay",
				"insert_after": "sb_automated_subscriptions",
			},
		)
		frappe.db.set_single_value("Subscription Settings", "auto_billing_delay", 3)
		self.assertTrue(frappe.db.exists("Custom Field", CUSTOM_FIELD))

		execute()

		self.assertFalse(frappe.db.exists("Custom Field", CUSTOM_FIELD))
		self.assertIsNone(frappe.get_meta("Subscription Settings").get_field("auto_billing_delay"))
		self.assertFalse(
			frappe.db.exists("Singles", {"doctype": "Subscription Settings", "field": "auto_billing_delay"})
		)

	def test_no_op_when_already_gone(self):
		"""A fresh install never had the field; the patch must not fail there or on a rerun."""
		execute()
		execute()
		self.assertFalse(frappe.db.exists("Custom Field", CUSTOM_FIELD))
