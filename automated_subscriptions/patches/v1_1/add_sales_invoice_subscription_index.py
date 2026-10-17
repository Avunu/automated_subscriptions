# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

from automated_subscriptions.install import add_sales_invoice_subscription_index


def execute():
	"""Index the header link on existing sites; a fresh install gets it from install.after_install."""
	add_sales_invoice_subscription_index()
