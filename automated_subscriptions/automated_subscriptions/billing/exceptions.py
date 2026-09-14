import frappe


class SubscriptionBillingError(frappe.ValidationError):
	"""Base for every engine error; process_all only catches frappe.ValidationError."""


class UnsupportedBillingGrid(SubscriptionBillingError):
	"""Plan interval / price determination cannot be anchored."""


class BackdatedStartNotSupported(SubscriptionBillingError):
	"""start_date is more than one billing cycle in the past and catch-up billing is deferred."""


class ZeroPlanRate(SubscriptionBillingError):
	"""get_plan_rate resolved to 0 (missing Item Price) on an anchored subscription."""


class ConsolidatedBillingError(SubscriptionBillingError):
	"""A consolidation group failed and was rolled back."""


class ConsolidationLocked(ConsolidatedBillingError):
	"""Another process holds the party's billing lock."""


class CreditNoteAlreadyIssued(SubscriptionBillingError):
	"""A submitted return already credits this invoice row."""
