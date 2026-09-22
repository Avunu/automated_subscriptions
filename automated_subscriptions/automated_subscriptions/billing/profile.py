from dataclasses import dataclass
from datetime import date
from typing import Literal

import frappe
from frappe.utils import cint, getdate

CUSTOMER_FIELDS = (
	"consolidate_subscription_invoices",
	"subscription_billing_anchor_mode",
	"subscription_billing_anchor_date",
	"subscription_billing_interval",
)
CALENDAR_ANCHOR = date(2000, 1, 1)


@dataclass(frozen=True)
class BillingProfile:
	anchor_mode: Literal["Anniversary", "Calendar"]
	anchor: date  # effective anchor: CALENDAR_ANCHOR for Calendar, the customer's date for Anniversary
	interval: Literal["", "Month", "Year"]


def read_customer_billing_fields(customer: str) -> frappe._dict:
	return frappe.get_cached_value("Customer", customer, CUSTOMER_FIELDS, as_dict=True) or frappe._dict()


def profile_from_fields(fields) -> BillingProfile | None:
	"""Dict-like -> profile; None when anchor mode is blank (stock behaviour). Unit-testable without a Customer."""
	mode = (fields.get("subscription_billing_anchor_mode") or "").strip()
	if not mode:
		return None
	if mode == "Calendar":
		anchor = CALENDAR_ANCHOR
	else:
		raw = fields.get("subscription_billing_anchor_date")
		if not raw:
			raise ValueError("Anniversary anchoring needs subscription_billing_anchor_date")
		anchor = getdate(raw)
	return BillingProfile(mode, anchor, fields.get("subscription_billing_interval") or "")


def get_billing_profile(party_type: str, party: str | None) -> BillingProfile | None:
	if party_type != "Customer" or not party:
		return None
	return profile_from_fields(read_customer_billing_fields(party))


def is_consolidating_customer(party_type: str, party: str | None) -> bool:
	if party_type != "Customer" or not party:
		return False
	return bool(cint(read_customer_billing_fields(party).get("consolidate_subscription_invoices")))
