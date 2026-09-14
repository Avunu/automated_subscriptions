import contextlib

import frappe
from erpnext.accounts.doctype.accounting_dimension.accounting_dimension import get_accounting_dimensions
from erpnext.accounts.doctype.subscription.subscription import Subscription as BaseSubscription
from erpnext.accounts.doctype.subscription_plan.subscription_plan import get_plan_rate
from frappe import _
from frappe.model.meta import get_field_precision
from frappe.types import DF
from frappe.utils import add_to_date, cint, escape_html, flt, getdate, nowdate

from automated_subscriptions.automated_subscriptions.billing.anchor import (
	period_end,
	prorate_factor,
	resolve_grid,
)
from automated_subscriptions.automated_subscriptions.billing.exceptions import (
	BackdatedStartNotSupported,
	UnsupportedBillingGrid,
	ZeroPlanRate,
)
from automated_subscriptions.automated_subscriptions.billing.profile import (
	BillingProfile,
	get_billing_profile,
	is_consolidating_customer,
)
from automated_subscriptions.automated_subscriptions.billing.settings import get_settings


@contextlib.contextmanager
def billing_context(subscription, *, ignore_pricing_rule):
	"""Scope a policy for the Sales Invoice `before_validate` handler to one engine-generated save.

	The invoice hook (custom/sales_invoice.py apply_subscription_billing_policy) reads
	frappe.flags.subscription_billing; nesting restores the previous policy on exit."""
	previous = frappe.flags.get("subscription_billing")
	frappe.flags.subscription_billing = frappe._dict(
		subscription=subscription.name, ignore_pricing_rule=ignore_pricing_rule
	)
	try:
		yield
	finally:
		frappe.flags.subscription_billing = previous


class Subscription(BaseSubscription):
	"""Mixin registered via extend_doctype_class; must derive from core Subscription directly.

	Annotations only, never assignments (a class attribute would shadow document values).
	Every override returns super() when the customer has no billing anchor (stock behaviour)."""

	service_identifier: DF.Data | None

	def _billing_profile(self) -> BillingProfile | None:
		try:
			return get_billing_profile(self.party_type, self.party)
		except (
			ValueError
		) as e:  # Anniversary customer without a date (API/legacy write) -> never a bare exception
			raise UnsupportedBillingGrid(_("Customer {0}: {1}").format(self.party, e)) from e

	def is_consolidating(self) -> bool:
		return is_consolidating_customer(self.party_type, self.party)

	def _resolve_grid_for_plan(self, profile, plan_doc, discount_pct):
		if plan_doc.price_determination == "Monthly Rate":
			raise UnsupportedBillingGrid(
				_("Plan {0} uses Monthly Rate pricing, which cannot be prorated or anchored.").format(
					plan_doc.name
				)
			)
		try:
			return resolve_grid(
				profile.anchor,
				profile.interval,
				plan_doc.billing_interval,
				cint(plan_doc.billing_interval_count) or 1,
				discount_pct,
			)
		except ValueError as e:
			raise UnsupportedBillingGrid(_("Subscription {0}: {1}").format(self.name or "", e)) from e

	def get_current_invoice_end(self, date=None):
		"""Anchored: the day before the next anchor occurrence after `date` (a boundary date gets a full term).
		The trial branch and the end_date clamp are core's, verbatim."""
		profile = self._billing_profile()
		if profile is None or not self.plans:
			return super().get_current_invoice_end(date)
		date = getdate(date)  # getdate(None) == today, matching core's nowdate() fallback
		if self.is_trialling() and date < getdate(self.trial_period_end):
			return getdate(self.trial_period_end)
		info = self.get_billing_cycle_and_interval()[0]  # validate_plans_billing_cycle guarantees one row
		grid, _units, _pct = self._resolve_grid_for_plan(
			profile,
			frappe._dict(
				name="",
				price_determination="",
				billing_interval=info.billing_interval,
				billing_interval_count=info.billing_interval_count,
			),
			0,
		)
		end = period_end(grid, date)
		if self.end_date and end > getdate(self.end_date):
			end = getdate(self.end_date)
		return end

	def get_items_from_plans(self, plans, prorate=0):
		"""Anchored: day-accurate proration of the plan rate over the anchored term; core's get_prorata_factor
		is never consulted. Stock: core's items plus the per-line metadata."""
		profile = self._billing_profile()
		if profile is None:
			return self._enrich_items(plans, super().get_items_from_plans(plans, prorate))
		start, end = getdate(self.current_invoice_start), getdate(self.current_invoice_end)
		settings = get_settings()
		precision = get_field_precision(frappe.get_meta("Sales Invoice Item").get_field("rate"))
		items = []
		for plan in plans:
			plan_doc = frappe.get_doc("Subscription Plan", plan.plan)
			grid, units, pct = self._resolve_grid_for_plan(
				profile, plan_doc, settings.annual_discount_percentage
			)
			try:
				factor = prorate_factor(grid, start, end, units, pct, prorate=bool(prorate))
			except ValueError as e:
				raise UnsupportedBillingGrid(_("Subscription {0}: {1}").format(self.name, e)) from e
			# positional like core; the third argument is `customer`
			rate = flt(get_plan_rate(plan.plan, plan.qty, self.party, start, end, factor), precision)
			if not rate:
				raise ZeroPlanRate(
					_("Plan {0} resolves to a zero rate (missing Item Price?)").format(plan.plan)
				)
			deferred_field = (
				"enable_deferred_revenue" if self.party_type == "Customer" else "enable_deferred_expense"
			)
			deferred = frappe.db.get_value("Item", plan_doc.item, deferred_field)
			item = {
				"item_code": plan_doc.item,
				"qty": plan.qty,
				"rate": rate,
				"cost_center": plan_doc.cost_center,
			}
			if deferred:
				item.update({deferred_field: deferred, "service_start_date": start, "service_end_date": end})
			for dimension in get_accounting_dimensions():
				if plan_doc.get(dimension):
					item[dimension] = plan_doc.get(dimension)
			items.append(item)
		return self._enrich_items(plans, items)

	def _enrich_items(self, plans, items):
		"""Additive per-line metadata on both paths: subscription / plan links, the billed period and the
		description from service_identifier. Purchase Invoice Item has none of these columns."""
		if self.party_type != "Customer":
			return items
		start, end = getdate(self.current_invoice_start), getdate(self.current_invoice_end)
		description = escape_html(self.get("service_identifier") or "") or None
		for plan, item in zip(plans, items, strict=True):  # core appends exactly one item per plan row
			item["subscription"] = self.name
			item["subscription_plan"] = plan.plan
			item["subscription_period_start"] = start
			item["subscription_period_end"] = end
			if description:
				item["description"] = description
		return items

	def create_invoice(self, from_date=None, to_date=None, posting_date=None):
		profile = self._billing_profile()
		if profile is None:
			return super().create_invoice(from_date, to_date, posting_date)
		# anchored invoices ignore Pricing Rules so the prorated plan rate survives the save
		with billing_context(self, ignore_pricing_rule=True):
			return super().create_invoice(from_date, to_date, posting_date)

	def validate(self):
		super().validate()
		profile = self._billing_profile()
		if profile is not None and self.follow_calendar_months:
			frappe.throw(
				_("Follow Calendar Months cannot be combined with a customer billing anchor; clear it."),
				UnsupportedBillingGrid,
			)
		if (
			self.is_new()
			and not (frappe.flags.in_import or frappe.flags.in_migrate)
			and self._catch_up_is_deferred()
		):
			# validate runs after before_insert, so current_invoice_end is already set for a new doc
			cycle = self.get_billing_cycle_data()
			upper = (
				getdate(add_to_date(self.current_invoice_end, **cycle))
				if cycle
				else getdate(self.current_invoice_end)
			)
			if getdate(nowdate()) > upper:
				frappe.throw(
					_(
						"Start Date {0} is more than one billing cycle in the past. Insert the subscription with "
						"frappe.flags.in_import set and replay the elapsed periods with Process Subscription."
					).format(self.start_date),
					BackdatedStartNotSupported,
				)

	def _catch_up_is_deferred(self) -> bool:
		"""True when after_insert will not bill elapsed periods synchronously (see generate_invoices_till_date).

		The daily run is capped at one cycle past current_invoice_end (core can_generate_new_invoice) and then
		re-anchors the period to today, silently losing the elapsed periods."""
		if (
			self.is_consolidating()
		):  # PR 3 routes these through the runner (enqueued), never the catch-up loop
			return True
		return (
			self._billing_profile() is not None and get_settings().mid_term_billing_mode == "Next Daily Run"
		)

	def generate_invoices_till_date(self) -> None:
		"""Override this, not after_insert, so core's in_import / in_migrate / future-start checks stay in force."""
		if self._billing_profile() is None:
			return super().generate_invoices_till_date()  # stock catch-up
		if get_settings().mid_term_billing_mode == "Next Daily Run":
			return  # the daily run bills the stub: posting >= current_invoice_start, inside the one-cycle cap
		return (
			super().generate_invoices_till_date()
		)  # Immediate: stock loop, each process() -> our create_invoice
