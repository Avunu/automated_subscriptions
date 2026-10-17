# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

from datetime import date, datetime

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import getdate

from automated_subscriptions.automated_subscriptions.billing.dates import as_date


class TestAsDate(IntegrationTestCase):
	def test_matches_getdate(self):
		self.assertEqual(as_date("2026-03-01"), date(2026, 3, 1))
		self.assertEqual(as_date(datetime(2026, 3, 1, 10, 30)), date(2026, 3, 1))
		self.assertEqual(as_date(None), getdate())

	def test_an_invalid_date_is_a_validation_error(self):
		self.assertIsNone(getdate("0000-00-00"))  # what as_date guards against
		with self.assertRaises(frappe.ValidationError):
			as_date("0000-00-00")
