import frappe
from frappe.model import no_value_fields
from frappe.utils import cint, flt

DOCTYPE = "Subscription Settings"

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
	None -> 0 (frappe/database/database.py:878-923), which would turn a default of 16.67 into 0.
	materialise_defaults() (after_sync / after_migrate) writes the rows so the desk form and a later save()
	see the same values; this reader stays as the belt-and-braces fallback."""
	rows = frappe.db.get_singles_dict(DOCTYPE)
	meta = frappe.get_meta(DOCTYPE)
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


def materialise_defaults() -> None:
	"""Write every Subscription Settings field default into tabSingles when the row is missing.

	Document.load_from_db applies new_doc defaults only while tabSingles has no row for the single at all
	(frappe/model/document.py:256-266); on a site that has saved the single once, a field added later loads as
	None and the next save() persists 0 / 0.0 (document.py get_valid_dict -> update_single), silently turning
	16.67 % / checked / 30 into 0. Registered as after_sync (install) and after_migrate, both of which run after
	sync_customizations has created the fields. Idempotent: an existing row is never overwritten (a row whose
	value is NULL is treated as missing and filled) and `modified` is left alone. Uses uncached meta because the
	fields may have been created in this same run."""
	meta = frappe.get_meta(DOCTYPE, cached=False)
	# a NULL row (a doc saved while the field still loaded as None) counts as missing; '' is a saved value
	rows = {k: v for k, v in frappe.db.get_singles_dict(DOCTYPE).items() if v is not None}
	missing = {
		df.fieldname: df.default
		for df in meta.fields
		if df.default not in (None, "") and df.fieldname not in rows and df.fieldtype not in no_value_fields
	}
	if missing:
		frappe.db.set_single_value(DOCTYPE, missing, update_modified=False)
