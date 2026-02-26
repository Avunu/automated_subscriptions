from typing import cast

import frappe
from erpnext.accounts.doctype.payment_request.payment_request import (
	PaymentRequest,
	make_payment_request,
)
from erpnext.accounts.doctype.subscription.subscription import Subscription
from frappe.utils import add_days, nowdate


def sales_invoice_subscription_payment_request(doc, method=None):
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
	pr.submit()
