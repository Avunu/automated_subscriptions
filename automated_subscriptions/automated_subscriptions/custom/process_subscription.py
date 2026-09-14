import frappe
from erpnext.accounts.doctype.process_subscription.process_subscription import (
	ProcessSubscription as BaseProcessSubscription,
)
from erpnext.accounts.doctype.subscription.subscription import process_all
from frappe.types import DF


class ProcessSubscription(BaseProcessSubscription):
	"""Mixin registered via extend_doctype_class: honours the `customer` custom field.

	Without a customer the stock fan-out runs (every non-cancelled subscription, enqueued in batches of 500).
	With a customer the party's subscriptions are processed synchronously in creation order: the first
	consolidating one delegates to the runner, the rest hit the run memo. The document is committed before
	process_all runs, because process_all commits per subscription and does a full frappe.db.rollback() on
	the first ValidationError; the stock fan-out never needs this because it only enqueues. Per-subscription
	failures are logged by process_all, never surfaced as a submit error. A posting_date in the past remains
	the replay tool; grace/status predicates compare against today (see billing/runner.py, replay trap)."""

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
		if not frappe.in_test:
			# process_all() runs inside this submit request: it answers the first ValidationError (e.g.
			# ConsolidationLocked raised before the runner's in-lock commit, or any stock create_invoice
			# failure) with a full frappe.db.rollback(), which would also discard this document's own
			# uncommitted docstatus=1 write: the client is told "Submitted" while the row is gone (API
			# submit) or back to Draft (desk). Persist it first, as the runner does after taking its lock.
			# Tests keep the class transaction open; core's own tests stub frappe.db.rollback for the same
			# reason.
			frappe.db.commit()
		process_all(names, self.posting_date)
