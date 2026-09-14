import frappe
from frappe import _


def validate(doc, method=None):
	"""Server-side rule: mandatory_depends_on is JS-only."""
	mode = (doc.get("subscription_billing_anchor_mode") or "").strip()
	if mode == "Anniversary" and not doc.get("subscription_billing_anchor_date"):
		frappe.throw(_("Subscription Billing Anchor Date is required for Anniversary anchoring."))
	if mode != "Anniversary":
		doc.subscription_billing_anchor_date = None  # Calendar ignores it; blank has none
	if not mode and doc.get("subscription_billing_interval"):
		# only touch it when set: pre-migrate rows are NULL and a NULL -> "" write would log a Version entry
		doc.subscription_billing_interval = ""
