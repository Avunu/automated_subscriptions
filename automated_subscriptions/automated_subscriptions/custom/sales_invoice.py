from typing import cast

import frappe
from erpnext.accounts.doctype.payment_request.payment_request import (
	PaymentRequest,
	make_payment_request,
)
from erpnext.accounts.doctype.sales_invoice.sales_invoice import SalesInvoice as BaseSalesInvoice
from erpnext.accounts.doctype.subscription.subscription import Subscription
from frappe.utils import add_days, nowdate


def apply_subscription_billing_policy(doc: BaseSalesInvoice, method=None):
	"""Engine-generated invoices of anchored customers ignore Pricing Rules so the prorated rate survives
	(Pricing Rules overwrite `rate` on every set_missing_values / save otherwise).

	The policy is scoped by custom/subscription.py billing_context; a stored Check keeps it on later saves.
	Registered as the Sales Invoice `before_validate` doc_event and called from the mixin below."""
	policy = frappe.flags.get("subscription_billing")
	if not policy or doc.get("is_return") or not doc.get("subscription"):
		return
	if policy.get("ignore_pricing_rule"):
		doc.ignore_pricing_rule = 1


class SalesInvoice(BaseSalesInvoice):
	"""Mixin registered via extend_doctype_class; a no-op unless frappe.flags.subscription_billing is set.

	Core Subscription.create_invoice calls invoice.set_missing_values() *before* invoice.save(), and that
	first call already applies Pricing Rules (set_missing_item_details -> apply_pricing_rule_on_items) while
	ignore_pricing_rule is still 0 - too early for the before_validate doc_event alone."""

	# core whitelists set_missing_values and the desk calls it via run_doc_method when is_pos is ticked
	# (sales_invoice.js set_pos_data); is_whitelisted checks the resolved function object, so the override
	# must re-declare it or every POS toggle fails with PermissionError
	@frappe.whitelist()
	def set_missing_values(self, for_validate=False):
		apply_subscription_billing_policy(self)
		return super().set_missing_values(for_validate)


def subscription_payment_request(doc: BaseSalesInvoice, method=None):
	# import a sales invoice and make a payment request
	if not doc.subscription:
		return

	# if the posting date is not the current date, do not create a payment request
	if str(doc.posting_date) != str(nowdate()):
		# debug
		frappe.log_error(
			"Posting date mismatch",
			f"Sales Invoice {doc.name} posting date {doc.posting_date} does not match current date {nowdate()}",
		)
		# return

	subscription = cast(Subscription, frappe.get_doc("Subscription", doc.subscription))
	plan_names = [plan.plan for plan in subscription.plans]
	subscription_plan = frappe.qb.DocType("Subscription Plan")
	payment_gateway = (
		frappe.qb.from_(subscription_plan)
		.select(subscription_plan.payment_gateway)
		.distinct()
		.where(subscription_plan.name.isin(plan_names))
	).run(pluck="payment_gateway")
	if not payment_gateway:
		return

	pr: PaymentRequest = cast(
		PaymentRequest,
		make_payment_request(
			dn=doc.name,
			dt="Sales Invoice",
			party_type=subscription.party_type,
			party=subscription.party,
			payment_gateway_account=payment_gateway[0],
			payment_request_type="Inward",
			recipient_id=doc.contact_email,
			return_doc=True,
		),
	)

	# set the transaction date
	if subscription.days_until_due:
		pr.transaction_date = add_days(doc.posting_date, subscription.days_until_due)
	else:
		auto_billing_delay = frappe.db.get_single_value("Subscription Settings", "auto_billing_delay")
		assert isinstance(auto_billing_delay, int), "Auto billing delay is not set in Subscription Settings"
		pr.transaction_date = add_days(doc.posting_date, auto_billing_delay)

	pr.save(ignore_permissions=True)
	if not pr.payment_gateway_validation():
		# If payment gateway validation fails, it means the gateway handled the payment directly (e.g., direct debit via mandate)
		# Mute email since there is no payment URL for the customer to visit
		pr.mute_email = 1
		pr.save(ignore_permissions=True)
	pr.submit()
