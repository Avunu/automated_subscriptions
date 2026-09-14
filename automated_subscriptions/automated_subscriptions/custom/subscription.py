from erpnext.accounts.doctype.subscription.subscription import Subscription as BaseSubscription
from frappe import _
from frappe.types import DF

from automated_subscriptions.automated_subscriptions.billing.exceptions import UnsupportedBillingGrid
from automated_subscriptions.automated_subscriptions.billing.profile import (
	BillingProfile,
	get_billing_profile,
	is_consolidating_customer,
)


class Subscription(BaseSubscription):
	"""Mixin registered via extend_doctype_class; must derive from core Subscription directly.

	Annotations only, never assignments (a class attribute would shadow document values)."""

	service_identifier: DF.Data | None

	def _billing_profile(self) -> BillingProfile | None:
		try:
			return get_billing_profile(self.party_type, self.party)
		except (
			ValueError
		) as e:  # Anniversary customer without a date (API/legacy write) -> never a bare exception
			raise UnsupportedBillingGrid(_("Customer {0}: {1}").format(self.party, e)) from e

	def is_consolidating(self) -> bool:
		return is_consolidating_customer(self.party_type, self.party)
