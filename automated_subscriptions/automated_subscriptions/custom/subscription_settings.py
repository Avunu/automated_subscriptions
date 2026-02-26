from frappe.types import DF
from erpnext.accounts.doctype.subscription_settings.subscription_settings import SubscriptionSettings as BaseSubscriptionSettings

class SubscriptionSettings(BaseSubscriptionSettings):
    auto_billing_delay: DF.Int