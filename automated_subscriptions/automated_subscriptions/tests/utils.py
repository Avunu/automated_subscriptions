"""Shared fixtures for the automated_subscriptions test modules."""

import datetime
from contextlib import contextmanager
from unittest.mock import patch

import frappe
from frappe.utils import getdate


def make_anchored_customer(name, mode, anchor_date=None, interval=None, consolidate=0):
	if frappe.db.exists("Customer", name):
		return frappe.get_doc("Customer", name)
	customer = frappe.new_doc("Customer")
	customer.customer_name = name
	customer.customer_type = "Company"
	customer.customer_group = "_Test Customer Group"
	customer.territory = "_Test Territory"
	customer.subscription_billing_anchor_mode = mode
	customer.subscription_billing_anchor_date = anchor_date
	customer.subscription_billing_interval = interval
	customer.consolidate_subscription_invoices = consolidate
	customer.append("accounts", {"company": "_Test Company", "account": "_Test Receivable - _TC"})
	customer.insert(ignore_permissions=True)
	return customer


@contextmanager
def frozen_today(date_str):
	"""Freeze nowdate()/today()/getdate(None)/now() for every importer of frappe.utils.data.

	freezegun is not installed on this bench; patching now_datetime is enough for core subscription.py.
	The frozen instant carries non-zero microseconds: Document.check_if_latest compares cstr(db modified)
	("... 10:00:00" for a whole second) with the string now() wrote ("... 10:00:00.000000"), and a save
	followed by submit() under the freeze would raise TimestampMismatchError otherwise."""
	frozen = datetime.datetime.combine(getdate(date_str), datetime.time(10, 0, 0, 123456))
	with patch("frappe.utils.data.now_datetime", return_value=frozen):
		yield frozen
