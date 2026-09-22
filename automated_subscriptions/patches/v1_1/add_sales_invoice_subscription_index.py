import frappe


def execute():
	"""Index the header link. Also the after_install hook: a fresh install marks every patch as completed
	without running it (frappe/installer.py set_all_patches_as_completed) and the PS-only
	custom/sales_invoice.json never runs updatedb. add_index is idempotent (has_index check)."""
	frappe.db.add_index("Sales Invoice", ["subscription"])
