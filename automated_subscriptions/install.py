# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

import frappe


def add_sales_invoice_subscription_index() -> None:
	"""Index the Sales Invoice header link to its Subscription. add_index is idempotent (has_index check)."""
	frappe.db.add_index("Sales Invoice", ["subscription"])


def after_install() -> None:
	"""A fresh install marks every patch as completed without running it (frappe/installer.py
	set_all_patches_as_completed), and the Process-Subscription-only custom/sales_invoice.json never runs
	updatedb, so the index the v1_1 patch adds on existing sites is added here."""
	add_sales_invoice_subscription_index()
