# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""Demo data for frappe-demo and the screenshots in docs/screenshots/.

One customer on the billing engine: calendar-anchored and consolidating, with four
monthly subscriptions that start on the demo day. The consolidated runner then bills
the four prorated first periods on one Sales Invoice, which is what the screenshots
show. No plan has a payment gateway, so no Payment Request is made and nothing leaves
the site.
"""

from unittest.mock import patch

import frappe
from erpnext import get_default_company

from automated_subscriptions.automated_subscriptions.billing.runner import run_consolidated_billing

CUSTOMER = "Riverbend Dental Group"
BEGINNING = "Beginning of the current subscription period"

# item code, monthly price
PLANS = (
	("Managed Website Hosting", 49.0),
	("Business Email Mailbox", 6.0),
	("Uptime Monitoring", 12.0),
)

# plan, quantity, service identifier
SUBSCRIPTIONS = (
	("Managed Website Hosting", 1, "riverbenddental.com"),
	("Managed Website Hosting", 1, "booking.riverbenddental.com"),
	("Business Email Mailbox", 5, "Mailboxes @riverbenddental.com"),
	("Uptime Monitoring", 1, "riverbenddental.com monitoring"),
)


def _leaf(doctype: str, preferred: str, root: str) -> str:
	return preferred if frappe.db.exists(doctype, preferred) else root


def _make_plans(currency: str) -> None:
	item_group = _leaf("Item Group", "Services", "All Item Groups")
	for item_code, cost in PLANS:
		if not frappe.db.exists("Item", item_code):
			frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": item_code,
					"item_name": item_code,
					"item_group": item_group,
					"stock_uom": "Nos",
					"is_stock_item": 0,
				}
			).insert(ignore_permissions=True)
		if not frappe.db.exists("Subscription Plan", item_code):
			frappe.get_doc(
				{
					"doctype": "Subscription Plan",
					"plan_name": item_code,
					"item": item_code,
					"currency": currency,
					"price_determination": "Fixed Rate",
					"cost": cost,
					"billing_interval": "Month",
					"billing_interval_count": 1,
				}
			).insert(ignore_permissions=True)


def _make_customer() -> None:
	if frappe.db.exists("Customer", CUSTOMER):
		return
	frappe.get_doc(
		{
			"doctype": "Customer",
			"customer_name": CUSTOMER,
			"customer_type": "Company",
			"customer_group": _leaf("Customer Group", "Commercial", "All Customer Groups"),
			"territory": _leaf("Territory", "United States", "All Territories"),
			"consolidate_subscription_invoices": 1,
			"subscription_billing_anchor_mode": "Calendar",
		}
	).insert(ignore_permissions=True)


def _make_subscriptions(company: str, start) -> None:
	for plan, qty, identifier in SUBSCRIPTIONS:
		if frappe.db.exists("Subscription", {"party": CUSTOMER, "service_identifier": identifier}):
			continue
		sub = frappe.get_doc(
			{
				"doctype": "Subscription",
				"party_type": "Customer",
				"party": CUSTOMER,
				"company": company,
				"start_date": start,
				"generate_invoice_at": BEGINNING,
				"days_until_due": 14,
				"submit_invoice": 1,
				"service_identifier": identifier,
				"plans": [{"plan": plan, "qty": qty}],
			}
		)
		# Inserted the way a data import is: the runner below bills them, not the insert's catch-up.
		frappe.flags.in_import = True
		try:
			sub.insert(ignore_permissions=True)
		finally:
			frappe.flags.in_import = False


def setup(ctx):
	"""Create the demo customer, plans and subscriptions, then bill them once on `ctx["today"]`."""
	company = ctx.get("company") or get_default_company()
	currency = frappe.get_cached_value("Company", company, "default_currency")
	_make_plans(currency)
	_make_customer()
	_make_subscriptions(company, ctx["today"])
	if frappe.db.exists("Sales Invoice", {"customer": CUSTOMER, "docstatus": 1}):
		return
	# The runner commits between customers so the next lock holder sees the invoice; frappe-demo commits
	# once, after this hook, so the commit is held back here.
	with patch.object(frappe.db, "commit"):
		results = run_consolidated_billing(posting_date=ctx["today"], party=CUSTOMER, company=company)
	if not any(r.get("invoice") for r in results):
		frappe.throw(f"The demo's consolidated invoice was not created: {results}")
