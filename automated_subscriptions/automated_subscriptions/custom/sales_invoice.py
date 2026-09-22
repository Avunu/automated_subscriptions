"""Sales Invoice side of the engine: the billing-policy hook (PR 2), the subscription lookups' fan-out
(status refresh over every subscription an invoice bills) and the Payment Request that auto-charges a
subscription invoice on submit.

The mixin is registered via extend_doctype_class; the module functions are doc_events (hooks.py)."""

import frappe
from erpnext.accounts.doctype.payment_request.payment_request import make_payment_request
from erpnext.accounts.doctype.sales_invoice.sales_invoice import SalesInvoice as BaseSalesInvoice
from erpnext.accounts.utils import refresh_subscription_status as refresh_one_subscription
from frappe import _
from frappe.types import DF
from frappe.utils import add_days, cint, flt, getdate, nowdate

from automated_subscriptions.automated_subscriptions.billing.settings import get_settings
from automated_subscriptions.automated_subscriptions.custom.sales_invoice_item import SalesInvoiceItem


def get_invoice_subscriptions(doc) -> list[str]:
	"""Every subscription the invoice bills: the header link first, then the line links in idx order,
	de-duplicated. Public: avunu's on_submit (PR 6) uses it too."""
	names = [doc.get("subscription")] if doc.get("subscription") else []
	names += [d.get("subscription") for d in doc.get("items") or [] if d.get("subscription")]
	return list(dict.fromkeys(names))


def is_subscription_invoice(doc) -> bool:
	return bool(get_invoice_subscriptions(doc))


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


def refresh_subscriptions_on_cancel(doc: BaseSalesInvoice, method=None):
	"""on_cancel doc_event: docstatus 2 is already persisted when on_cancel runs (Document._cancel saves
	first), so the subscription lookups no longer see this invoice and every subscription it billed gets its
	status recomputed. No on_submit counterpart: create_invoice runs inside Subscription.process(), which
	saves the subscription itself afterwards; a hook saving it from the DB mid-submit would make that save
	raise TimestampMismatchError."""
	if frappe.flags.in_migrate or frappe.flags.in_patch or frappe.flags.in_install:
		return
	doc.refresh_subscription_status()


class SalesInvoice(BaseSalesInvoice):
	"""Mixin registered via extend_doctype_class.

	Core Subscription.create_invoice calls invoice.set_missing_values() *before* invoice.save(), and that
	first call already applies Pricing Rules (set_missing_item_details -> apply_pricing_rule_on_items) while
	ignore_pricing_rule is still 0 - too early for the before_validate doc_event alone."""

	items: DF.Table[SalesInvoiceItem]  # pyright: ignore[reportIncompatibleVariableOverride]

	# core whitelists set_missing_values and the desk calls it via run_doc_method when is_pos is ticked
	# (sales_invoice.js set_pos_data); is_whitelisted checks the resolved function object, so the override
	# must re-declare it or every POS toggle fails with PermissionError
	@frappe.whitelist()
	def set_missing_values(self, for_validate=False):
		apply_subscription_billing_policy(self)
		return super().set_missing_values(for_validate)

	def refresh_subscription_status(self):
		"""Fan out over every subscription the invoice bills (header and lines); one bad subscription must not
		stop the others. Core refreshes the header link only; the only core caller is Payment Entry
		submit / cancel (trigger_invoice_update_for_subscriptions), plus our on_cancel doc_event."""
		for name in get_invoice_subscriptions(self):
			try:
				refresh_one_subscription(name)
			except Exception:
				frappe.log_error(title=f"Subscription status refresh failed: {name}")


def resolve_payment_gateway_account(doc) -> str | None:
	"""Deterministic gateway account for the invoice: from the line plans, falling back to the header
	subscription's plans for legacy invoices without line links. Blank plan gateways ride along with a set
	one; no set gateway means no auto-charge; two different set gateways are logged and left to a human."""
	plans = {d.get("subscription_plan") for d in doc.get("items") or [] if d.get("subscription_plan")}
	if not plans and doc.get("subscription"):
		plans = set(
			frappe.get_all(
				"Subscription Plan Detail",
				{"parent": doc.subscription, "parenttype": "Subscription"},
				pluck="plan",
			)
		)
	if not plans:
		return None
	gateways = {
		gateway
		for gateway in frappe.get_all(
			"Subscription Plan", {"name": ("in", list(plans))}, pluck="payment_gateway"
		)
		if gateway
	}
	if not gateways:
		return None  # no plan asks for auto-charge
	if len(gateways) > 1:
		frappe.log_error(
			title=f"Mixed payment gateways on subscription invoice {doc.name}",
			message=str(sorted(gateways)),
		)
		return None  # a human raises the Payment Request
	return gateways.pop()


def subscription_payment_request(doc: BaseSalesInvoice, method=None):
	"""on_submit doc_event: raise (and charge, when the gateway handles it) a Payment Request for a
	subscription invoice. Rules, in order: not a subscription invoice; a return or nothing outstanding
	(make_payment_request throws on a zero amount, which would abort a credit-note submit); the site_config
	kill switch `subscription_auto_charge_disabled` (per site directory, not restored with the database:
	restored copies carry live gateway credentials); migrate / patch / install / import; no gateway
	resolves; a stale invoice (due date older than Subscription Settings > Auto-charge Max Lateness) is left
	as a draft Payment Request with a comment and an Error Log; otherwise create, charge and submit.

	Late fires are normal (posting_date = the period start for "Beginning") and must charge: nothing here
	gates on posting_date == today. The collection date is the invoice's payment-terms due date."""
	if not is_subscription_invoice(doc):
		return
	if doc.get("is_return") or flt(doc.outstanding_amount) <= 0:
		return  # credit notes / zero-total (trial) invoices: make_payment_request would throw
	if frappe.conf.get("subscription_auto_charge_disabled"):
		return  # site_config kill switch
	if frappe.flags.in_migrate or frappe.flags.in_patch or frappe.flags.in_install or frappe.flags.in_import:
		return
	gateway_account = resolve_payment_gateway_account(doc)
	if not gateway_account:
		return
	pr = make_payment_request(
		dn=doc.name,
		dt="Sales Invoice",
		party_type="Customer",
		party=doc.customer,
		payment_gateway_account=gateway_account,  # a name; an explicit None would yield a gateway-less PR
		payment_request_type="Inward",
		recipient_id=doc.contact_email,
		return_doc=True,
	)
	# core fills is_a_subscription / subscription_plans from the *header* subscription and validates every
	# such plan's gateway against the request's; a consolidated invoice bills other subscriptions' plans too
	pr.is_a_subscription = 0
	pr.set("subscription_plans", [])
	due_date = getdate(doc.due_date or doc.posting_date)
	pr.transaction_date = due_date  # the payment-terms due date is the collection date
	pr.save(ignore_permissions=True)
	max_lateness = cint(get_settings().auto_charge_max_lateness_days)
	if due_date < getdate(add_days(nowdate(), -max_lateness)):
		pr.add_comment(
			"Comment",
			_("Left as draft: due date {0} is more than {1} days in the past. Charge manually.").format(
				due_date, max_lateness
			),
		)
		frappe.log_error(
			title=f"Stale subscription invoice not auto-charged: {doc.name}",
			message=f"due {due_date}, max lateness {max_lateness} days",
		)
		return  # draft Payment Request, no charge
	# payment_gateway_validation returns False on *any* exception, hence the payment_gateway check; False
	# from a gateway means it handled the payment itself (e.g. a direct-debit mandate): nothing to email
	charge_handled = bool(pr.payment_gateway) and pr.payment_gateway_validation() is False
	pr.mute_email = 1 if charge_handled else 0
	pr.save(ignore_permissions=True)
	pr.submit()
