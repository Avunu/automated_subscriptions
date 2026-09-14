import frappe
from frappe.tests import IntegrationTestCase

MIXIN_MODULE = "automated_subscriptions.automated_subscriptions.custom.sales_invoice"


class TestSalesInvoiceMixin(IntegrationTestCase):
	def test_set_missing_values_stays_whitelisted(self):
		# The desk calls set_missing_values via run_doc_method when is_pos is ticked (sales_invoice.js
		# set_pos_data); whitelisting is per function object, so the extend_doctype_class override must
		# re-declare it.
		doc = frappe.new_doc("Sales Invoice")
		fn = doc.set_missing_values.__func__
		self.assertEqual(fn.__module__, MIXIN_MODULE)
		self.assertIn(fn, frappe.whitelisted)
		frappe.is_whitelisted(fn)  # must not raise PermissionError
