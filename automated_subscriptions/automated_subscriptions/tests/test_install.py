# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""The after_install hook. It runs at site install, before the tests, so it is called here directly; it must be a
no-op on a site that already has the index. (The after_sync / after_migrate hook is covered in test_fields.)"""

import frappe
from frappe.tests import IntegrationTestCase

from automated_subscriptions.install import after_install
from automated_subscriptions.patches.v1_1.add_sales_invoice_subscription_index import execute


def subscription_indexes() -> list[str]:
	"""Names of the indexes on tabSales Invoice whose first column is `subscription`."""
	rows = frappe.db.sql(
		"SHOW INDEX FROM `tabSales Invoice` WHERE Column_name = 'subscription' AND Seq_in_index = 1",
		as_dict=True,
	)
	return sorted({row["Key_name"] for row in rows})


class TestInstallHooks(IntegrationTestCase):
	def test_after_install_indexes_the_header_link(self):
		"""The index exists once this site is installed, and the hook and the existing-site patch both leave it
		alone when it does (a second add_index would be DDL, which commits the test transaction)."""
		self.assertIn(
			"automated_subscriptions.install.after_install",
			frappe.get_hooks("after_install", app_name="automated_subscriptions"),
		)
		before = subscription_indexes()
		self.assertTrue(before)
		after_install()
		execute()
		self.assertEqual(subscription_indexes(), before)
