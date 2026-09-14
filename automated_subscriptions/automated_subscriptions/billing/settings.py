import frappe
from frappe.utils import cint, flt

FIELDS = (
	# core
	"prorate",
	"grace_period",
	"cancel_after_grace",
	# ours
	"annual_discount_percentage",
	"credit_note_on_cancellation",
	"mid_term_billing_mode",
	"auto_charge_max_lateness_days",
)


def get_settings() -> frappe._dict:
	"""Subscription Settings with field defaults applied for rows that were never saved.

	tabSingles has no row for a field until the single is saved once, and db.get_single_value casts
	None -> 0 (frappe/database/database.py:878-923), which would turn a default of 25 into 0."""
	rows = frappe.db.get_singles_dict("Subscription Settings")
	meta = frappe.get_meta("Subscription Settings")
	out = frappe._dict()
	for fieldname in FIELDS:
		df = meta.get_field(fieldname)
		value = rows.get(fieldname)
		if value is None or value == "":
			value = df.default if df else None
		if df and df.fieldtype in ("Check", "Int"):
			value = cint(value)
		elif df and df.fieldtype in ("Percent", "Float", "Currency"):
			value = flt(value)
		out[fieldname] = value
	return out
