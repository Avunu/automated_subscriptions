# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

from functools import partial

import frappe
from erpnext.accounts.doctype.process_subscription.process_subscription import (
	ProcessSubscription as BaseProcessSubscription,
)
from erpnext.accounts.doctype.subscription.subscription import process_all
from frappe.types import DF


def process_customer_subscriptions(names: list[str], posting_date: str | None) -> None:
	"""The customer path of Process Subscription, run once the submit is committed (see the class docstring).

	One submitted document is one run. The run memo (billing/runner.py _run_ctx) lives on frappe.local, which a
	request / RQ job / bench execute rebuilds but a bench console or a long script keeps for its whole lifetime: a
	second submit for the same customer and posting date would otherwise hit the previous run's "ok" (status
	maintenance only, so a sub more than one period behind never gets its next period) or sticky "failed" (every
	member raises even after the cause was fixed) instead of re-checking the rows. Never touch an active group
	(the customer path is never entered inside the runner).

	process_all logs and rolls back a ValidationError per subscription. Anything else would escape the request's
	own commit as a bare server error after the document was saved, so it is rolled back and logged too."""
	ctx = getattr(frappe.local, "consolidated_billing", None)
	if ctx is not None and ctx.active is None:
		ctx.done.clear()
	try:
		process_all(names, posting_date)
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title="Process Subscription (customer) failed")


class ProcessSubscription(BaseProcessSubscription):
	"""Extension registered via extend_doctype_class: honours the `customer` custom field.

	Without a customer the stock fan-out runs (every non-cancelled subscription, enqueued in batches of 500).
	With a customer the party's subscriptions are processed in creation order within the same request: the first
	consolidating one delegates to the runner, the rest hit the run memo. Per-subscription failures are logged by
	process_all, never surfaced as a submit error. A posting_date in the past remains the replay tool;
	grace/status predicates compare against today (see billing/runner.py, replay trap).

	The processing is queued on frappe.db.after_commit rather than run inside on_submit. process_all commits per
	subscription and answers the first ValidationError (e.g. ConsolidationLocked, or any stock create_invoice
	failure) with a full frappe.db.rollback(), which would also discard this document's own uncommitted
	docstatus=1 write: the client would be told "Submitted" while the row is gone (API) or back to Draft (desk).
	After the commit there is nothing of this document left to lose. The request commits at its end, so the
	response still waits for the run; a console or script that submits one runs it at its next commit."""

	customer: DF.Link | None

	def process_all_subscription(self):
		if not self.get("customer"):
			return super().process_all_subscription()
		filters = {"status": ("!=", "Cancelled"), "party_type": "Customer", "party": self.customer}
		if self.subscription:
			filters["name"] = self.subscription
		names = frappe.get_all(
			"Subscription", filters=filters, pluck="name", order_by="creation asc, name asc"
		)
		frappe.db.after_commit.add(partial(process_customer_subscriptions, names, self.posting_date))
		return None
