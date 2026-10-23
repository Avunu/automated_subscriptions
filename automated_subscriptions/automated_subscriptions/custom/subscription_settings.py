# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

from erpnext.accounts.doctype.subscription_settings.subscription_settings import (
	SubscriptionSettings as BaseSubscriptionSettings,
)
from frappe.types import DF


class SubscriptionSettings(BaseSubscriptionSettings):
	"""Typing shell only; not registered in hooks (it adds no behaviour). Read the values through
	billing/settings.py get_settings, which applies the field defaults."""

	annual_discount_percentage: DF.Percent
	auto_charge_max_lateness_days: DF.Int
	credit_note_on_cancellation: DF.Check
	mid_term_billing_mode: DF.Literal["Immediate", "Next Daily Run"]
