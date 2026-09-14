import frappe


def execute():
	frappe.db.add_index("Sales Invoice", ["subscription"])
