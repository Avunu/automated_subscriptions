"""Mid-term cancellation with a Credit Note for the unused remainder of every billed line.

The subscription is cancelled effective `cancel_date` (inclusive: the cancel day is still billed). For every
submitted, non-return invoice line that bills the subscription and whose period ends on or after that date,
one Credit Note per source invoice is issued with the line's rate scaled by the unused share of its period.
The rate, never the qty, is scaled: core's return validation only refuses a rate *above* the original, and the
full negative qty makes core itself refuse any second return on the row. The credit sits as unallocated
customer credit (`update_outstanding_for_self = 1`); the original invoice is untouched. Gateway refunds are
out of scope.

Core's `cancel_subscription` is deliberately not called: it cuts an arrears invoice for the elapsed part of the
period and hard-codes the cancellation date to today."""

import frappe
from erpnext.accounts.doctype.sales_invoice.sales_invoice import make_sales_return
from erpnext.accounts.doctype.subscription.subscription import InvoiceCancelled
from frappe import _
from frappe.query_builder.functions import IfNull
from frappe.utils import add_days, date_diff, flt, formatdate, getdate
from frappe.utils.file_lock import LockTimeoutError
from frappe.utils.synchronization import filelock

from automated_subscriptions.automated_subscriptions.billing.exceptions import (
	ConsolidationLocked,
	CreditNoteAlreadyIssued,
	SubscriptionBillingError,
)
from automated_subscriptions.automated_subscriptions.billing.runner import party_lock_name
from automated_subscriptions.automated_subscriptions.billing.settings import get_settings


@frappe.whitelist()
def cancel_with_credit_note(subscription: str, cancel_date=None, posting_date=None) -> dict:
	"""Cancel `subscription` effective `cancel_date` (default today) and credit the unused service.

	`posting_date` is only for a cancel date that falls in a frozen or closed period: the credit notes are
	posted on it while the credited window is still computed from `cancel_date`.
	Returns {"subscription", "credit_notes": [names], "credited": total credited (positive)}."""
	sub = frappe.get_doc("Subscription", subscription)
	frappe.has_permission("Subscription", "write", doc=sub, throw=True)
	frappe.has_permission("Sales Invoice", "submit", throw=True)

	if sub.status == "Cancelled":
		frappe.throw(_("subscription is already cancelled."), InvoiceCancelled)
	if sub.party_type != "Customer":
		frappe.throw(
			_("Credit notes on cancellation are only supported for Customer subscriptions."),
			SubscriptionBillingError,
		)
	if sub.generate_invoice_at == "End of the current subscription period":
		frappe.throw(
			_("{0} is billed in arrears; nothing is prepaid. Use Cancel Subscription instead.").format(
				sub.name
			),
			SubscriptionBillingError,
		)
	if not get_settings().credit_note_on_cancellation:
		frappe.throw(
			_("Credit Note On Cancellation is disabled in Subscription Settings."), SubscriptionBillingError
		)

	cancel_date = getdate(cancel_date)
	posting_date = getdate(posting_date) if posting_date else None

	try:
		with filelock(party_lock_name(sub.company, sub.party_type, sub.party), timeout=60):
			credit_notes = _issue_credit_notes(sub, cancel_date, posting_date)
			sub.status = "Cancelled"
			sub.cancelation_date = cancel_date
			sub.save()
	except LockTimeoutError as e:
		raise ConsolidationLocked(_("Billing for {0} is running elsewhere").format(sub.party)) from e

	return {
		"subscription": sub.name,
		"credit_notes": [cn.name for cn in credit_notes],
		"credited": flt(sum(-cn.grand_total for cn in credit_notes), 2),
	}


def billed_lines(subscription: str) -> list[frappe._dict]:
	"""Submitted, non-return invoice lines billing `subscription`: engine lines (line link) plus legacy
	header-only lines (header link, no line link). Each carries the line's period, falling back to the
	invoice header dates."""
	si = frappe.qb.DocType("Sales Invoice")
	sii = frappe.qb.DocType("Sales Invoice Item")
	rows = (
		frappe.qb.from_(sii)
		.join(si)
		.on(si.name == sii.parent)
		.select(
			si.name.as_("invoice"),
			si.posting_date,
			si.posting_time,
			si.from_date,
			si.to_date,
			sii.name.as_("row_name"),
			sii.idx,
			sii.item_code,
			sii.qty,
			sii.rate,
			sii.amount,
			sii.subscription_period_start,
			sii.subscription_period_end,
		)
		.where(
			(sii.parenttype == "Sales Invoice")
			& (si.docstatus == 1)
			& (si.is_return == 0)
			& (
				(sii.subscription == subscription)
				| ((si.subscription == subscription) & (IfNull(sii.subscription, "") == ""))
			)
		)
		.orderby(si.posting_date)
		.orderby(si.name)
		.orderby(sii.idx)
	).run(as_dict=True)
	for row in rows:
		row.pstart = row.subscription_period_start or row.from_date
		row.pend = row.subscription_period_end or row.to_date
	return rows


def unused_factor(pstart, pend, cancel_date) -> tuple[float, int, int]:
	"""(factor, unused_days, total_days): calendar days, both period bounds inclusive, the cancel day counts
	as used. The complement of the engine's inclusive billed_days / term_days, so add-then-cancel conserves
	money on the same basis."""
	total_days = date_diff(pend, pstart) + 1
	unused_days = min(max(date_diff(pend, cancel_date), 0), total_days)
	return unused_days / total_days, unused_days, total_days


def existing_returns(invoice: str, row_names: list[str]) -> list[frappe._dict]:
	"""Returns against `invoice` that already reference any of `row_names` (any docstatus < 2)."""
	si = frappe.qb.DocType("Sales Invoice")
	sii = frappe.qb.DocType("Sales Invoice Item")
	return (
		frappe.qb.from_(sii)
		.join(si)
		.on(si.name == sii.parent)
		.select(si.name, si.docstatus, sii.sales_invoice_item)
		.where(
			(sii.parenttype == "Sales Invoice")
			& (si.is_return == 1)
			& (si.return_against == invoice)
			& (si.docstatus < 2)
			& (sii.sales_invoice_item.isin(row_names))
		)
	).run(as_dict=True)


def _issue_credit_notes(sub, cancel_date, posting_date) -> list:
	lines = [row for row in billed_lines(sub.name) if row.pend and getdate(row.pend) >= cancel_date]
	for row in lines:
		if not row.pstart:
			frappe.throw(
				_("Row {0} of {1} carries no service period; issue that credit note manually.").format(
					row.idx, row.invoice
				),
				SubscriptionBillingError,
			)

	by_invoice: dict[str, list[frappe._dict]] = {}
	for row in lines:
		row.factor, row.unused_days, row.total_days = unused_factor(row.pstart, row.pend, cancel_date)
		if row.factor > 0:
			by_invoice.setdefault(row.invoice, []).append(row)

	credit_notes = []
	for invoice, rows in by_invoice.items():
		cn = _build_credit_note(sub, invoice, rows, cancel_date, posting_date)
		if cn is None:
			continue
		cn.flags.ignore_mandatory = True
		cn.insert()
		cn.submit()
		credit_notes.append(cn)
	return credit_notes


def _guard_double_refund(invoice: str, rows: list[frappe._dict]) -> None:
	hits = existing_returns(invoice, [row.row_name for row in rows])
	submitted = [hit for hit in hits if hit.docstatus == 1]
	if submitted:
		frappe.throw(
			_("Credit note {0} already credits row {1} of {2}.").format(
				submitted[0].name, submitted[0].sales_invoice_item, invoice
			),
			CreditNoteAlreadyIssued,
		)
	drafts = sorted({hit.name for hit in hits if hit.docstatus == 0})
	if drafts:
		frappe.msgprint(
			_("Draft return(s) {0} against {1} also reference the credited rows.").format(
				", ".join(drafts), invoice
			),
			alert=True,
		)


def _build_credit_note(sub, invoice: str, rows: list[frappe._dict], cancel_date, posting_date):
	_guard_double_refund(invoice, rows)
	by_row = {row.row_name: row for row in rows}

	previous = frappe.flags.get("selected_children")
	frappe.flags.selected_children = {"items": list(by_row)}
	try:
		cn = make_sales_return(invoice)
	finally:
		frappe.flags.selected_children = previous

	for tax in cn.get("taxes") or []:
		if tax.charge_type == "Actual":
			frappe.throw(
				_("{0} carries an Actual-amount tax row; issue this credit note manually.").format(invoice),
				SubscriptionBillingError,
			)

	for item in list(cn.items):
		source = by_row.get(item.sales_invoice_item)
		if source is None:
			cn.remove(item)
			continue
		if not item.qty:
			frappe.throw(
				_("Row {0} of {1} has already been fully returned.").format(source.idx, invoice),
				CreditNoteAlreadyIssued,
			)
		new_rate = flt(item.rate * source.factor, item.precision("rate"))
		if new_rate <= 0:
			cn.remove(item)
			continue
		credited_from = max(getdate(source.pstart), add_days(cancel_date, 1))
		item.rate = new_rate
		item.subscription = sub.name
		item.subscription_period_start = credited_from
		item.subscription_period_end = getdate(source.pend)
		item.description = "{}<br>{}".format(
			item.description or item.item_name or item.item_code,
			_("Credit for unused service {0} - {1} ({2} of {3} days)").format(
				formatdate(credited_from), formatdate(source.pend), source.unused_days, source.total_days
			),
		)
	if not cn.items:
		return None

	ref = rows[0]
	ref_posting = getdate(ref.posting_date)
	cn.subscription = sub.name
	cn.set_posting_time = 1
	cn.posting_date = posting_date or max(cancel_date, ref_posting)
	cn.posting_time = ref.posting_time if getdate(cn.posting_date) == ref_posting else "00:00:00"
	cn.update_outstanding_for_self = 1
	cn.from_date = None
	cn.to_date = None
	cn.payment_terms_template = None
	cn.set("payment_schedule", [])
	cn.remarks = _("Credit note for cancellation of Subscription {0} effective {1}, against {2}").format(
		sub.name, formatdate(cancel_date), invoice
	)
	if flt(cn.discount_amount):
		source_net = flt(frappe.db.get_value("Sales Invoice", invoice, "net_total"))
		credited_net = flt(sum(flt(item.rate) * flt(item.qty) for item in cn.items))
		cn.discount_amount = flt(cn.discount_amount * abs(credited_net) / source_net, 2) if source_net else 0
	cn.run_method("calculate_taxes_and_totals")
	if not flt(cn.grand_total):
		return None
	return cn
