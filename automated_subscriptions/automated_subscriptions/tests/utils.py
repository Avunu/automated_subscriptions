# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""Shared fixtures for the automated_subscriptions test modules."""

import datetime
from contextlib import contextmanager
from typing import TYPE_CHECKING, cast
from unittest.mock import patch

import frappe

from automated_subscriptions.automated_subscriptions.billing.dates import as_date

if TYPE_CHECKING:
	from automated_subscriptions.automated_subscriptions.custom.sales_invoice import AutoSubSalesInvoice
	from automated_subscriptions.automated_subscriptions.custom.subscription import AutoSubSubscription


def make_anchored_customer(name, mode, anchor_date=None, interval=None, consolidate=0):
	if frappe.db.exists("Customer", name):
		return frappe.get_doc("Customer", name)
	customer = frappe.get_doc(
		{
			"doctype": "Customer",
			"customer_name": name,
			"customer_type": "Company",
			"customer_group": "_Test Customer Group",
			"territory": "_Test Territory",
			"subscription_billing_anchor_mode": mode,
			"subscription_billing_anchor_date": anchor_date,
			"subscription_billing_interval": interval,
			"consolidate_subscription_invoices": consolidate,
		}
	)
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
	frozen = datetime.datetime.combine(as_date(date_str), datetime.time(10, 0, 0, 123456))
	with patch("frappe.utils.data.now_datetime", return_value=frozen):
		yield frozen


def make_payment_terms_template(name, credit_days):
	"""One-row template, 100 % due `credit_days` after the invoice date."""
	if frappe.db.exists("Payment Terms Template", name):
		return name
	doc = frappe.get_doc({"doctype": "Payment Terms Template", "template_name": name})
	doc.append(
		"terms",
		{
			"invoice_portion": 100,
			"credit_days": credit_days,
			"due_date_based_on": "Day(s) after invoice date",
		},
	)
	doc.insert(ignore_permissions=True)
	return doc.name


@contextmanager
def full_rollbacks_suppressed():
	"""Keep the test's transaction through code that answers an error with a full frappe.db.rollback().

	IntegrationTestCase runs each test inside one transaction that it rolls back afterwards. Core's process_all
	(and the engine's own error paths) roll back everything on a failure, which would also discard the test's
	fixtures, so the full rollback is skipped here while savepoint rollbacks, the runner's per-group isolation,
	still reach the database. Core's own subscription tests stub the method for the same reason. Yields the mock,
	so a test can assert that a full rollback was asked for."""
	rollback = frappe.db.rollback

	def savepoints_only(*, save_point=None, chain=False):
		if save_point:
			rollback(save_point=save_point, chain=chain)

	with patch.object(frappe.db, "rollback", side_effect=savepoints_only) as mock:
		yield mock


def get_invoice(name: str | None) -> AutoSubSalesInvoice:
	"""frappe.get_doc for a Sales Invoice, typed as the composed controller the tests exercise."""
	assert name, "an unsaved Sales Invoice has no name"
	return cast("AutoSubSalesInvoice", frappe.get_doc("Sales Invoice", name))


def get_subscription(name: str | None) -> AutoSubSubscription:
	"""frappe.get_doc for a Subscription, typed as the composed controller the tests exercise."""
	assert name, "an unsaved Subscription has no name"
	return cast("AutoSubSubscription", frappe.get_doc("Subscription", name))
