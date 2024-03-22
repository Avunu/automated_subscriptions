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

    pr = make_payment_request(
        dn=doc.name,
        dt="Sales Invoice",
        party_type=subscription.party_type,
        party=subscription.party,
        payment_gateway_account=payment_gateway,
        payment_request_type="Inward",
        recipient_id=doc.contact_email,
        return_doc=True
    )

    # set the transaction date to the posting date + auto_billing_delay
    auto_billing_delay = frappe.db.get_single_value("Subscription Settings", "auto_billing_delay")
    pr.transaction_date = frappe.utils.add_days(doc.posting_date, auto_billing_delay)

    pr.save(ignore_permissions=True)
    pr.submit()