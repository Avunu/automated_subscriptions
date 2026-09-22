from erpnext.accounts.doctype.subscription_settings.subscription_settings import (
	SubscriptionSettings as BaseSubscriptionSettings,
)
from frappe.types import DF


class SubscriptionSettings(BaseSubscriptionSettings):
	annual_discount_percentage: DF.Percent
	auto_billing_delay: DF.Int
	auto_charge_max_lateness_days: DF.Int
	credit_note_on_cancellation: DF.Check
	mid_term_billing_mode: DF.Literal["Immediate", "Next Daily Run"]
