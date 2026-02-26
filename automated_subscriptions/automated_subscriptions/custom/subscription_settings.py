from erpnext.accounts.doctype.subscription_settings.subscription_settings import (
	SubscriptionSettings as BaseSubscriptionSettings,
)
from frappe.types import DF


class SubscriptionSettings(BaseSubscriptionSettings):
	auto_billing_delay: DF.Int
