import frappe
from erpnext.accounts.doctype.payment_request.payment_request import make_payment_request


def sales_invoice_subscription_payment_request(doc, method=None):
    # import a sales invoice and make a payment request
    if not doc.subscription:
        return

    subscription = frappe.get_doc("Subscription", doc.subscription)
    plan_names = [plan.plan for plan in subscription.plans]
    subscription_plan = frappe.qb.DocType("Subscription Plan")
    payment_gateway = (
        frappe.qb.from_(subscription_plan)
        .select(subscription_plan.payment_gateway)
        .distinct()
        .where(subscription_plan.name.isin(plan_names))
    ).run()[0][0]

    make_payment_request(
        dt="Sales Invoice",
        dn=doc.name,
        party_type=subscription.party_type,
        party=subscription.party,
        recipient_id=doc.contact_email,
        payment_request_type="Inward",
        payment_gateway_account=payment_gateway
    )
