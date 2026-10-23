# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

import frappe

CUSTOM_FIELD = "Subscription Settings-auto_billing_delay"


def execute():
	"""Drop the Auto-billing Delay setting. Nothing read it; when a payment falls due comes from Days Until Due
	on the Subscription. The custom JSON no longer ships the field, and a sync never deletes one, so remove it
	here along with its stored value."""
	frappe.delete_doc("Custom Field", CUSTOM_FIELD, ignore_missing=True, force=True)
	frappe.db.delete("Singles", {"doctype": "Subscription Settings", "field": "auto_billing_delay"})
	frappe.clear_cache(doctype="Subscription Settings")
