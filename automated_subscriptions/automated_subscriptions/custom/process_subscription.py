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
	consolidating one delegates to the runner, the rest hit the run memo. A posting_date in the past remains
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
		process_all(names, self.posting_date)
