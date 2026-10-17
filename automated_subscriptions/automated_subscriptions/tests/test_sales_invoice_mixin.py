# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from automated_subscriptions.automated_subscriptions.custom.sales_invoice import (
	refresh_subscriptions_on_cancel,
	resolve_payment_gateway_account,
	subscription_payment_request,
)

if TYPE_CHECKING:
	from automated_subscriptions.automated_subscriptions.custom.sales_invoice import AutoSubSalesInvoice

MIXIN_MODULE = "automated_subscriptions.automated_subscriptions.custom.sales_invoice"


class TestSalesInvoiceMixin(IntegrationTestCase):
	def test_set_missing_values_stays_whitelisted(self):
		# The desk calls set_missing_values via run_doc_method when is_pos is ticked (sales_invoice.js
		# set_pos_data); whitelisting is per function object, so the extend_doctype_class override must
		# re-declare it.
		doc = cast("AutoSubSalesInvoice", frappe.new_doc("Sales Invoice"))
		fn = doc.set_missing_values.__func__
		self.assertEqual(fn.__module__, MIXIN_MODULE)
		self.assertIn(fn, frappe.whitelisted)
		frappe.is_whitelisted(fn)  # must not raise PermissionError

	def test_status_refresh_fans_out_past_a_failure(self):
		doc = cast("AutoSubSalesInvoice", frappe.new_doc("Sales Invoice"))
		doc.subscription = "SUB-A"
		doc.append("items", {"subscription": "SUB-B"})
		doc.append("items", {"subscription": "SUB-A"})  # de-duplicated
		with (
			patch(
				f"{MIXIN_MODULE}.refresh_one_subscription", side_effect=[RuntimeError("boom"), None]
			) as refresh,
			patch("frappe.log_error") as log_error,
		):
			doc.refresh_subscription_status()
		self.assertEqual([c.args[0] for c in refresh.call_args_list], ["SUB-A", "SUB-B"])
		log_error.assert_called_once_with(title="Subscription status refresh failed: SUB-A")

	def test_cancel_hook_skips_migrate(self):
		doc = MagicMock()
		with patch.dict(frappe.flags, {"in_migrate": True}):
			refresh_subscriptions_on_cancel(doc)
		doc.refresh_subscription_status.assert_not_called()
		refresh_subscriptions_on_cancel(doc)
		doc.refresh_subscription_status.assert_called_once_with()

	def test_gateway_needs_a_plan(self):
		self.assertIsNone(resolve_payment_gateway_account(frappe._dict(items=[], subscription=None)))

	def test_gateway_falls_back_to_the_header_subscription(self):
		"""A legacy invoice has no line plans: the header subscription's plans decide."""
		with patch("frappe.get_all", side_effect=[["_Test Plan Name"], ["_Test Gateway Account"]]) as get_all:
			account = resolve_payment_gateway_account(frappe._dict(items=[], subscription="SUB-A"))
		self.assertEqual(account, "_Test Gateway Account")
		self.assertEqual(get_all.call_args_list[0].args[0], "Subscription Plan Detail")

	def test_no_payment_request_for_a_plain_invoice(self):
		with patch(f"{MIXIN_MODULE}.make_payment_request") as make_payment_request:
			subscription_payment_request(cast("AutoSubSalesInvoice", frappe.new_doc("Sales Invoice")))
		make_payment_request.assert_not_called()
