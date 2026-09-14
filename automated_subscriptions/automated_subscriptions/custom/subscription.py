import contextlib

import frappe
from erpnext import get_default_company
from erpnext.accounts.doctype.accounting_dimension.accounting_dimension import get_accounting_dimensions
from erpnext.accounts.doctype.subscription.subscription import Subscription as BaseSubscription
from erpnext.accounts.doctype.subscription.subscription import is_prorate
from erpnext.accounts.doctype.subscription_plan.subscription_plan import get_plan_rate
from frappe import _
from frappe.model.meta import get_field_precision
from frappe.types import DF
from frappe.utils import add_days, add_to_date, cint, escape_html, flt, getdate, nowdate

from automated_subscriptions.automated_subscriptions.billing.anchor import (
	period_end,
	prorate_factor,
	resolve_grid,
)
from automated_subscriptions.automated_subscriptions.billing.exceptions import (
	BackdatedStartNotSupported,
	ConsolidatedBillingError,
	UnsupportedBillingGrid,
	ZeroPlanRate,
)
from automated_subscriptions.automated_subscriptions.billing.profile import (
	BillingProfile,
	get_billing_profile,
	is_consolidating_customer,
)
from automated_subscriptions.automated_subscriptions.billing.runner import (
	_active_run,
	_run_ctx,
	run_consolidated_billing,
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


@contextlib.contextmanager
def _submit_suppressed(subscription):
	"""Core's create_invoice saves and submits in one function and reads submit_invoice exactly once; flip it
	on the in-memory object only so the consolidation sink stays a draft until _finalize submits it."""
	saved = subscription.submit_invoice
	subscription.submit_invoice = 0
	try:
		yield
	finally:
		subscription.submit_invoice = saved


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
		if profile is None:
			return super().get_current_invoice_end(date)
		cycle_info = self.get_billing_cycle_and_interval()
		if not cycle_info:
			# no plan rows, or every plan link blank/unknown (an API insert runs before_insert before the mandatory
			# check): core's get_last_day fallback keeps the insert alive so mandatory/link validation reports the row
			return super().get_current_invoice_end(date)
		date = getdate(date)  # getdate(None) == today, matching core's nowdate() fallback
		if self.is_trialling() and date < getdate(self.trial_period_end):
			return getdate(self.trial_period_end)
		info = cycle_info[0]  # validate_plans_billing_cycle rejects more than one distinct cycle
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
		start, end = self._billing_window()
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
		start, end = self._billing_window()
		description = escape_html(self.get("service_identifier") or "") or None
		for plan, item in zip(plans, items, strict=True):  # core appends exactly one item per plan row
			item["subscription"] = self.name
			item["subscription_plan"] = plan.plan
			item["subscription_period_start"] = start
			item["subscription_period_end"] = end
			if description:
				item["description"] = description
		return items

	def _billing_window(self):
		"""(start, end) this invoice bills: the current period, narrowed by the from/to dates create_invoice was
		given (core cancel_subscription passes current_invoice_start..cancelation_date for the arrears invoice)."""
		start, end = getdate(self.current_invoice_start), getdate(self.current_invoice_end)
		window = self.flags.get("subscription_billing_window")
		if window:
			start = max(start, getdate(window[0]))
			end = min(
				end, getdate(window[1])
			)  # clamp to the term: a late cancel never bills more than the term
		return start, end

	def _realign_current_period(self) -> bool:
		"""Anchored subs whose stored period predates the customer's anchor (M5, or any later anchor change)
		keep their start and get the grid's end: the first anchored invoice is the stub start..next anchor - 1.
		Returns True when the end was changed. No-op when already aligned or not anchored."""
		if self._billing_profile() is None or not self.plans or not self.current_invoice_start:
			return False
		aligned_end = getdate(self.get_current_invoice_end(self.current_invoice_start))
		if getdate(self.current_invoice_end) == aligned_end:
			return False
		self.current_invoice_end = aligned_end
		return True

	@frappe.whitelist()  # core whitelists process (desk "Fetch Subscription Updates" -> run_doc_method -> is_whitelisted)
	def process(self, posting_date=None):
		"""Realign a period stored before the customer's anchor was set *before* core evaluates the trigger date,
		the one-cycle cap, the cancel_at_period_end snapshot and is_current_invoice_generated. For "End of the
		current subscription period" the trigger *is* current_invoice_end, so a stale end would either fire the stub
		at the old end with posting_date = the realigned (future) end, or defer it months past the aligned end and
		back-date it. Core's trailing save() persists the healed period (review round R2-02 / R7).

		Consolidating customers are billed by the runner (billing/runner.py), never standalone: the first sub of
		a party that core's process_all reaches runs the whole party group; the siblings hit the run memo. After
		a successful run every member falls through to core's process() on a reloaded copy for status
		maintenance only (billed -> not due). A failed group raises so no member is billed on its own."""
		if self._billing_profile() is not None:
			self._realign_current_period()
		if _active_run() is not None:
			# inside the runner: core's process() on the runner's own copy; create_invoice routes to the sink
			try:
				return super().process(posting_date)
			finally:
				self.flags.pop("consolidation_current_invoice", None)
		if not self.is_consolidating():
			return super().process(posting_date)  # stock behaviour
		ctx = _run_ctx()
		key = (self.company or get_default_company(), self.party_type, self.party, str(getdate(posting_date)))
		state = ctx.done.get(key)
		if state is None:
			run_consolidated_billing(
				posting_date, party=self.party, party_type=self.party_type, company=key[0]
			)
			state = ctx.done.get(key, "ok")  # the runner records "ok"/"failed"; it never re-raises per group
		if state == "failed":
			raise ConsolidatedBillingError(
				_(
					"Consolidated billing for {0} failed earlier in this run; not billing {1} standalone"
				).format(self.party, self.name)
			)
		self.reload()  # the runner saved a different copy of this row
		return super().process(posting_date)  # billed -> not due -> status maintenance only

	def get_current_invoice(self):
		"""Run-scoped guard: for the rest of the process() call that put this sub's lines on the consolidation
		sink, the sink *is* the current invoice (core's header-only lookup would return the sub's previous invoice
		and, past grace with cancel_after_grace, cancel the subscription it just billed). Only ever set on the
		runner's own doc copies."""
		sink = self.flags.get("consolidation_current_invoice")
		if sink is not None:
			return sink
		return super().get_current_invoice()

	def create_invoice(self, from_date=None, to_date=None, posting_date=None):
		"""Outside a consolidation run (or for a non-consolidating customer): the standalone invoice. Inside a
		run: sub[0] creates the sink through core's own create_invoice (submission suppressed), every later sub
		absorbs its lines into that same draft object; the runner submits it once."""
		run = _active_run()
		if run is None or not self.is_consolidating():
			return self._create_standalone_invoice(from_date, to_date, posting_date)
		profile = self._billing_profile()
		if profile is not None:
			self._realign_current_period()  # the period below must be the aligned one
		self.flags.subscription_billing_window = (
			from_date or self.current_invoice_start,
			to_date or self.current_invoice_end,
		)
		try:
			period = (
				getdate(from_date or self.current_invoice_start),
				getdate(to_date or self.current_invoice_end),
			)
			with billing_context(self, ignore_pricing_rule=profile is not None):
				if run.sink is None:
					with _submit_suppressed(self):
						run.sink = super().create_invoice(from_date, to_date, posting_date)  # saved draft
				else:
					self._absorb_into(run.sink, period)
		finally:
			self.flags.pop("subscription_billing_window", None)
		run.members.append(
			frappe._dict(
				name=self.name,
				period=period,
				submit_invoice=cint(self.submit_invoice),
				service_identifier=self.get("service_identifier"),
			)
		)
		self.flags.consolidation_current_invoice = run.sink  # grace-period guard, see get_current_invoice
		return run.sink

	def _create_standalone_invoice(self, from_date=None, to_date=None, posting_date=None):
		profile = self._billing_profile()
		if profile is not None:
			# direct callers (cancel_subscription arrears); a no-op after process() realigned
			self._realign_current_period()  # stub for periods stored before the anchor was set (DECISIONS.md Q-14)
		# core's get_items_from_plans takes no dates: stash the window create_invoice was given (metadata only on
		# the stock path; the anchored factor and the per-line period read it via _billing_window)
		self.flags.subscription_billing_window = (
			from_date or self.current_invoice_start,
			to_date or self.current_invoice_end,
		)
		try:
			if profile is None:
				return super().create_invoice(from_date, to_date, posting_date)
			# anchored invoices ignore Pricing Rules so the prorated plan rate survives the save
			with billing_context(self, ignore_pricing_rule=True):
				return super().create_invoice(from_date, to_date, posting_date)
		finally:
			self.flags.pop("subscription_billing_window", None)

	def _absorb_into(self, sink, period):
		"""Append this sub's lines (factor, links, period, description from get_items_from_plans) to the sink
		and re-save the same object (its flags.ignore_mandatory persists; a fresh get_doc would lose it)."""
		items = self.get_items_from_plans(self.plans, is_prorate())
		existing = {(d.item_code, d.description or "") for d in sink.items}
		allow_dupes = cint(frappe.db.get_single_value("Selling Settings", "allow_multiple_items"))
		for item in items:
			if not flt(item.get("rate")) or not flt(item.get("qty")):
				raise ConsolidatedBillingError(
					_(
						"Subscription {0} produced a zero-rate line; core would re-price it at list price."
					).format(self.name)
				)
			# set_missing_item_details fills a blank description with the Item's, which is what core's
			# duplicate check (validate_for_duplicate_items) compares
			description = item.get("description") or (
				frappe.get_cached_value("Item", item["item_code"], "description") or ""
			)
			if not allow_dupes and (item["item_code"], description) in existing:
				raise ConsolidatedBillingError(
					_(
						"Subscription {0}: identical item and description already on {1}; set a Service "
						"Identifier or enable Selling Settings > Allow Item to Be Added Multiple Times"
					).format(self.name, sink.name)
				)
			sink.append("items", item)
			existing.add((item["item_code"], description))
		sink.from_date = min(getdate(sink.from_date), period[0])
		sink.to_date = max(getdate(sink.to_date), period[1])
		sink.save()

	def validate(self):
		super().validate()
		profile = self._billing_profile()
		if profile is not None and self.follow_calendar_months:
			frappe.throw(
				_("Follow Calendar Months cannot be combined with a customer billing anchor; clear it."),
				UnsupportedBillingGrid,
			)
		if profile is not None:
			# get_current_invoice_end only sees a synthetic plan dict, so a Monthly Rate plan would otherwise
			# fail inside create_invoice (every night, logged by process_all) instead of on this save
			for plan in self.plans:
				if (
					plan.plan
					and frappe.db.get_value("Subscription Plan", plan.plan, "price_determination")
					== "Monthly Rate"
				):
					frappe.throw(
						_("Plan {0} uses Monthly Rate pricing, which cannot be prorated or anchored.").format(
							plan.plan
						),
						UnsupportedBillingGrid,
					)
		if self.is_new() and not (frappe.flags.in_import or frappe.flags.in_migrate):
			# validate runs after before_insert, so current_invoice_end is the stub's end for a new doc.
			today = getdate(nowdate())
			stub_end = getdate(self.current_invoice_end)
			if (
				profile is not None
				and not cint(self.generate_new_invoices_past_due_date)
				and today > stub_end
			):
				# The period after the stub has already begun. With the flag off core bills it only once the
				# stub is Paid (can_generate_new_invoice) and re-anchors the period to today the day after it
				# ends (process() elif branch), so it is dropped unless the customer pays within the days left.
				frappe.throw(
					_(
						"Start Date {0} lies in a billing period that has already ended, so the period starting "
						"{1} would only be billed once the first invoice is paid and is dropped when it ends. "
						"Either enable Generate New Invoices Past Due Date so every elapsed period is billed, or "
						"insert the subscription with frappe.flags.in_import set and replay the elapsed periods "
						"with Process Subscription."
					).format(self.start_date, add_days(stub_end, 1)),
					BackdatedStartNotSupported,
				)
			if self._catch_up_is_deferred():  # anchored Next Daily Run, or consolidating (PR 3 runner)
				# The deferred stub is billed by the daily job, whose first run after this insert posts
				# tomorrow at the earliest (Daily cron 0 0 * * *); core caps that late fire at
				# current_invoice_end + one cycle, so today must be strictly before the cap.
				cycle = self.get_billing_cycle_data()
				upper = getdate(add_to_date(stub_end, **cycle)) if cycle else stub_end
				if today >= upper:
					frappe.throw(
						_(
							"Start Date {0} is too far in the past for the elapsed periods to be billed (they reach "
							"one billing cycle past the first period end). Either enable Generate New Invoices Past "
							"Due Date so every elapsed period is billed on insert, or insert the subscription with "
							"frappe.flags.in_import set and replay the elapsed periods with Process Subscription."
						).format(self.start_date),
						BackdatedStartNotSupported,
					)

	def _catch_up_is_deferred(self) -> bool:
		"""True when after_insert will not bill the stub synchronously (see generate_invoices_till_date).

		The daily run is capped at one cycle past current_invoice_end (core can_generate_new_invoice) and then
		re-anchors the period to today, silently losing the stub; validate() rejects inserts on or past that cap.
		With generate_new_invoices_past_due_date off the tighter "period already ended" check in validate()
		applies in every mode (PR 3 relies on this helper for consolidating subs)."""
		if (
			self.is_consolidating()
		):  # PR 3 routes these through the runner (enqueued), never the catch-up loop
			return True
		return (
			self._billing_profile() is not None and get_settings().mid_term_billing_mode == "Next Daily Run"
		)

	def generate_invoices_till_date(self) -> None:
		"""Override this, not after_insert, so core's in_import / in_migrate / future-start checks stay in force."""
		if self.is_consolidating():
			# never run the runner (which commits between groups) inside the insert request; validate() already
			# rejected starts the deferred run could not bill (one-cycle cap)
			if get_settings().mid_term_billing_mode == "Immediate":
				frappe.enqueue(
					"automated_subscriptions.automated_subscriptions.billing.runner.run_consolidated_billing",
					queue="long",
					enqueue_after_commit=True,
					posting_date=nowdate(),
					party=self.party,
					party_type=self.party_type,
					company=self.company,
				)
			return  # Next Daily Run: the daily job picks it up
		if self._billing_profile() is None:
			return super().generate_invoices_till_date()  # stock catch-up
		if get_settings().mid_term_billing_mode == "Next Daily Run":
			return  # the daily run bills the stub: posting >= current_invoice_start, inside the one-cycle cap
		# Immediate: stock loop, each process() -> our create_invoice; it stops after one invoice unless
		# generate_new_invoices_past_due_date is set (validate guards the rest)
		return super().generate_invoices_till_date()
