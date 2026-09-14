from erpnext.accounts.doctype.sales_invoice_item.sales_invoice_item import (
	SalesInvoiceItem as BaseSalesInvoiceItem,
)
from frappe.types import DF


class SalesInvoiceItem(BaseSalesInvoiceItem):
	"""Typing shell only; not registered in hooks."""

	subscription: DF.Link | None
	subscription_period_end: DF.Date | None
	subscription_period_start: DF.Date | None
	subscription_plan: DF.Link | None
