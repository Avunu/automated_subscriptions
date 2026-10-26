# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""Sales Invoice side of the engine: the billing-policy hook (PR 2), the subscription lookups' fan-out
(status refresh over every subscription an invoice bills) and the Payment Request that auto-charges a
subscription invoice on submit.

The mixin is registered via extend_doctype_class; the module functions are doc_events (hooks.py)."""

from datetime import date

import frappe
from erpnext.accounts.doctype.payment_request.payment_request import make_payment_request
from erpnext.accounts.doctype.sales_invoice.sales_invoice import SalesInvoice as BaseSalesInvoice
from erpnext.accounts.utils import refresh_subscription_status as refresh_one_subscription
from frappe import _
from frappe.types import DF
from frappe.utils import add_days, cint, flt, nowdate

from automated_subscriptions.automated_subscriptions.billing.dates import as_date
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


class AutoSubSalesInvoice(BaseSalesInvoice):
	"""Mixin registered via extend_doctype_class.

	Core Subscription.create_invoice calls invoice.set_missing_values() *before* invoice.save(), and that
	first call already applies Pricing Rules (set_missing_item_details -> apply_pricing_rule_on_items) while
	ignore_pricing_rule is still 0 - too early for the before_validate doc_event alone."""

	# The rows carry this app's custom fields, which SalesInvoiceItem only declares the types of. Narrowing the
	# element type of the invariant list is the point, so the checkers' override rule is silenced for it.
	items: DF.Table[SalesInvoiceItem]  # pyright: ignore[reportIncompatibleVariableOverride]  # ty: ignore[invalid-attribute-override]  # the rows carry this app's custom fields

	# core whitelists set_missing_values and the desk calls it via run_doc_method when is_pos is ticked
	# (sales_invoice.js set_pos_data); is_whitelisted checks the resolved function object, so the override
	# must re-declare it or every POS toggle fails with PermissionError
	@frappe.whitelist()
	def set_missing_values(self, for_validate: bool = False):
		apply_subscription_billing_policy(self)
		return super().set_missing_values(for_validate)

	def set_payment_schedule(self):
		"""Core Subscription.create_invoice stamps posting_date with a datetime.date (current_invoice_start or end),
		and set_payment_schedule hands it to the whitelisted get_payment_term_details, whose `posting_date: str |
		None` frappe's type validation enforces: every subscription invoice whose customer has a Payment Terms
		Template failed with FrappeTypeError. The ISO string is the same date in the form a desk save sends."""
		if isinstance(self.posting_date, date):
			self.posting_date = as_date(self.posting_date).isoformat()
		return super().set_payment_schedule()

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


# Integration Request statuses that mean the gateway accepted the payment. Stock GoCardless logs the request as
# Queued before it calls the payments API and moves it to Authorized or Completed only when the API accepts the
# payment; it leaves it Queued when the call raises (it swallows the error) and sets Failed or Cancelled when the
# payment is refused. None of those three is a charge.
CHARGED_STATUSES = ("Authorized", "Completed")


def gateway_recorded_charge(pr) -> bool:
	"""Whether the gateway accepted a payment for the Payment Request: Frappe Payments controllers log each
	gateway transaction as an Integration Request against it and move it to Authorized or Completed when the
	gateway accepts the payment."""
	return bool(
		frappe.db.exists(
			"Integration Request",
			{
				"reference_doctype": "Payment Request",
				"reference_docname": pr.name,
				"status": ("in", CHARGED_STATUSES),
			},
		)
	)


def submit_and_notify(pr) -> None:
	"""Submit the Payment Request so the gateway is called exactly once, then e-mail or confirm.

	ERPNext's before_submit -> set_payment_request_url -> payment_gateway_validation calls the gateway
	controller's on_payment_request_submission, and for an off-session gateway (stock GoCardless with a
	mandate) that call *is* the charge. So the app never calls payment_gateway_validation itself: core makes
	the one call, with the e-mail held back by flags.mute_email until the outcome is known.

	A payment URL means the customer pays by link (Stripe and other redirect gateways, or GoCardless without a
	mandate): the app sends the e-mail and the Communication core would have sent. A phone request (M-Pesa) was
	sent to the customer's phone by before_submit and never e-mails: nothing to add. No URL and an Integration
	Request in Authorized or Completed means the gateway took the payment: Mute Email is set. Anything else
	means payment_gateway_validation returned False without a confirmed charge: the gateway check raised (it
	swallows every exception), the payments API call failed, or the payment was refused. The request stays
	submitted and unpaid (status Requested, Mute Email off) and an Error Log plus a comment ask a human to
	check the gateway and collect it. It is not resubmitted, so the gateway is never called twice."""
	pr.flags.mute_email = True
	pr.submit()
	if pr.payment_url:
		pr.send_email()
		pr.make_communication_entry()
		return
	if pr.payment_channel == "Phone":
		return
	if gateway_recorded_charge(pr):
		pr.db_set("mute_email", 1)
		return
	pr.add_comment(
		"Comment",
		_(
			"Payment not confirmed: the payment gateway neither confirmed a payment nor returned a payment "
			"link. Check the payment in the gateway and the Error Log, then collect the payment manually."
		),
	)
	frappe.log_error(
		title=f"Subscription payment not confirmed: {pr.name}",
		message=f"gateway {pr.payment_gateway}, invoice {pr.reference_name}",
	)


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
	due_date = as_date(doc.due_date or doc.posting_date)
	# the payment-terms due date is the collection date. An ISO string, not a date: stock payments' GoCardless
	# compares it with the mandate's next_possible_charge_date (a string from the API) via max(), and a date there
	# raises TypeError, which payment_gateway_validation swallows into False: no charge
	pr.transaction_date = due_date.isoformat()
	pr.save(ignore_permissions=True)
	max_lateness = cint(get_settings().auto_charge_max_lateness_days)
	if due_date < as_date(add_days(nowdate(), -max_lateness)):
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
	submit_and_notify(pr)
