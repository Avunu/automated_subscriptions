# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

import frappe
from erpnext.accounts.doctype.process_subscription.process_subscription import (
	ProcessSubscription as BaseProcessSubscription,
)
from erpnext.accounts.doctype.subscription.subscription import process_all
from frappe.types import DF
from frappe.utils.data import DateTimeLikeObject


def process_customer_subscriptions(names: list[str], posting_date: DateTimeLikeObject | None) -> None:
	"""The customer path of Process Subscription, run as a background job once the submit is committed (see the
	class docstring).

	One submitted document is one run. The run memo (billing/runner.py _run_ctx) lives on frappe.local, which an
	RQ job rebuilds but a bench console or a test keeps for its whole lifetime: a second run for the same customer
	and posting date would otherwise hit the previous run's "ok" (status maintenance only, so a sub more than one
	period behind never gets its next period) or sticky "failed" (every member raises even after the cause was
	fixed) instead of re-checking the rows. Never touch an active group (the customer path is never entered
	inside the runner).

	process_all logs and rolls back a ValidationError per subscription. Anything else is rolled back and logged
	here under one title, so the job ends normally instead of failing with only the method name as its log."""
	ctx = getattr(frappe.local, "consolidated_billing", None)
	if ctx is not None and ctx.active is None:
		ctx.done.clear()
	try:
		process_all(names, posting_date)
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title="Process Subscription (customer) failed")


class AutoSubProcessSubscription(BaseProcessSubscription):
	"""Extension registered via extend_doctype_class: honours the `customer` custom field.

	Without a customer the stock fan-out runs (every non-cancelled subscription, enqueued in batches of 500).
	With a customer the party's subscriptions are processed in creation order in one background job: the first
	consolidating one delegates to the runner, the rest hit the run memo. Per-subscription failures are logged by
	process_all, never surfaced as a submit error. A posting_date in the past remains the replay tool;
	grace/status predicates compare against today (see billing/runner.py, replay trap).

	The processing is enqueued after the submit commits (like the stock path, on the long queue) rather than run
	inside on_submit. process_all commits per subscription and answers the first ValidationError (e.g.
	ConsolidationLocked, or any stock create_invoice failure) with a full frappe.db.rollback(), which would also
	discard this document's own uncommitted docstatus=1 write: the client would be told "Submitted" while the row
	is gone (API) or back to Draft (desk). Running it as a frappe.db.after_commit callback in the same request is
	not enough either: that rollback also resets the after-commit queue, dropping this document's own on_submit
	webhooks and list refresh, which Document.run_method queues after on_submit. The job runs as the submitting
	user once the submit is committed; the desk no longer waits for the invoices."""

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
		frappe.enqueue(
			process_customer_subscriptions,
			queue="long",
			enqueue_after_commit=True,
			names=names,
			posting_date=self.posting_date,
		)
		return None
