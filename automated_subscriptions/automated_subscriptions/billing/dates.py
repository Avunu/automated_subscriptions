# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""frappe.utils.getdate with a return type the engine can rely on."""

from datetime import date

import frappe
from frappe import _
from frappe.utils import getdate
from frappe.utils.data import DateTimeLikeObject


def as_date(value: DateTimeLikeObject | None = None) -> date:
	"""getdate(value), typed `date`: today for a blank value, as getdate does.

	getdate returns None for a string it recognises as an invalid date ("0000-00-00"); every engine caller compares
	or does arithmetic on the result, which would fail later with a bare TypeError. Raise the ValidationError that
	process_all logs per subscription instead."""
	result = getdate(value)
	if result is None:
		raise frappe.ValidationError(_("{0} is not a valid date").format(value))
	return result
