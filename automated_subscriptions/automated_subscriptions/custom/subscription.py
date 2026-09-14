import contextlib

import frappe
from erpnext import get_default_company
from erpnext.accounts.doctype.accounting_dimension.accounting_dimension import get_accounting_dimensions
from erpnext.accounts.doctype.subscription.subscription import Subscription as BaseSubscription
from erpnext.accounts.doctype.subscription.subscription import is_prorate
from erpnext.accounts.doctype.subscription_plan.subscription_plan import get_plan_rate
from frappe import _
from frappe.model.meta import get_field_precision
from frappe.query_builder.functions import Max, Min
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
	_is_due,
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


def _line_periods(subscription: str) -> dict[str, frappe._dict]:
	"""Per Sales Invoice, the window this subscription's own lines bill on it (NULL when the lines carry none)."""
	sii = frappe.qb.DocType("Sales Invoice Item")
	rows = (
		frappe.qb.from_(sii)
		.select(
			sii.parent,
			Min(sii.subscription_period_start).as_("period_start"),
			Max(sii.subscription_period_end).as_("period_end"),
		)
		.where((sii.parenttype == "Sales Invoice") & (sii.subscription == subscription))
		.groupby(sii.parent)
	).run(as_dict=True)
	return {row.parent: row for row in rows}


def billing_invoices(subscription: str) -> list[frappe._dict]:
	"""Every non-return Sales Invoice that bills `subscription` by header link *or* line link, any docstatus.

	Each row carries core's header dates plus this subscription's own `period_start` / `period_end` on that
	invoice (the header dates when its lines carry none: pre-migration rows, stock header-only invoices)."""
	line_periods = _line_periods(subscription)
	si = frappe.qb.DocType("Sales Invoice")
	links = si.subscription == subscription
	if line_periods:
		links = links | si.name.isin(list(line_periods))
	rows = (
		frappe.qb.from_(si)
		.select(
			si.name,
			si.docstatus,
			si.status,
			si.posting_date,
			si.due_date,
			si.is_return,
			si.from_date,
			si.to_date,
			si.creation,
		)
		.where((si.is_return == 0) & links)
	).run(as_dict=True)
	for row in rows:
		lines = line_periods.get(row.name)
		row.period_start = (lines and lines.period_start) or row.from_date
		row.period_end = (lines and lines.period_end) or row.to_date
	return rows


def _date_key(value):
	return getdate(value) if value else getdate("1900-01-01")


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

	def _credit_note_cancellation_applies(self) -> bool:
		"""Credit-note cancellation is opt-in via the customer (anchored or consolidating) and the setting, and
		only for subscriptions billed in advance: a postpaid sub ("End of the current subscription period") has
		nothing prepaid to credit and needs core's prorated arrears invoice instead. Every other subscription
		keeps core's cancellation (DECISIONS.md D-12)."""
		return (
			self.party_type == "Customer"
			and self.generate_invoice_at != "End of the current subscription period"
			and (self._billing_profile() is not None or self.is_consolidating())
			and bool(get_settings().credit_note_on_cancellation)
		)

	@frappe.whitelist()  # core whitelists cancel_subscription (desk button -> run_doc_method -> is_whitelisted)
	def cancel_subscription(self, cancel_date=None):
		"""Desk button and API: credit the unused service instead of core's arrears invoice when the customer
		has opted in; `cancel_date` (default today) is the last billed day. The kwarg name is what the desk
		button passes (`frm.call` -> run_doc_method)."""
		if not self._credit_note_cancellation_applies():
			return super().cancel_subscription()
		from automated_subscriptions.automated_subscriptions.billing.credit_note import (
			cancel_with_credit_note,
		)

		return cancel_with_credit_note(self.name, cancel_date or nowdate())

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
		maintenance only (billed -> not due); a sub still due after the run (more than one period behind) gets
		status maintenance only too and waits for the next daily run, as stock bills one period per run. This
		copy has no `consolidation_current_invoice` flag: the line-aware get_current_invoice below (header or
		line link, ordered by this sub's own line period end) is what makes its status pass -- and every later
		daily run's -- see the sink instead of sub[k>0]'s previous invoice or, for the header owner, a
		sibling-widened sink outranking its own later invoices.
		A failed group raises so no member is billed on its own. Core's trailing save() persists the healed
		period on the delegated path only because the realignment is repeated after the reload."""
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
			# no company filter: a blank-company sub (core's backward-compat case) must be visited and grouped
			# under the default company, exactly as group_key does; the memo key above uses the same fallback
			run_consolidated_billing(posting_date, party=self.party, party_type=self.party_type)
			# the runner records "ok"/"failed" per visited party and never re-raises per group; when the party had
			# no due group at all it records nothing, so memoise "ok" here or every sibling in the same process_all
			# job repeats the full candidate scan (O(N^2) document loads per party per day). setdefault never
			# overwrites a "failed" verdict, and a party-scoped lock timeout raises above before reaching this line.
			state = ctx.done.setdefault(key, "ok")
		if state == "failed":
			raise ConsolidatedBillingError(
				_(
					"Consolidated billing for {0} failed earlier in this run; not billing {1} standalone"
				).format(self.party, self.name)
			)
		self.reload()  # the runner saved a different copy of this row (and discarded the realignment above)
		if self._billing_profile() is not None:
			# a member the runner skipped as not due still carries the end stored before the anchor was set;
			# core's trigger / cap / re-anchor checks below and its trailing save() must see and persist the
			# aligned one, exactly like the non-consolidating anchored path (PR 2 R2-02 / R7), or an
			# End-of-period sub fires on the stale end and bills the stub standalone
			self._realign_current_period()
		if _is_due(self, posting_date):
			# The runner billed this party for the run date and advanced the period; the row is still due only
			# when it was more than one period behind (back-dated insert within validate()'s one-cycle cap, or a
			# daily-job outage > 1 cycle). Core's generation branch would now bill the next period standalone
			# (create_invoice sees no active run). Stock bills one period per run: do status maintenance only
			# and let the next daily run consolidate the next period.
			self.set_subscription_status(posting_date=posting_date)
			self.save()
			return None
		return super().process(posting_date)  # billed -> not due -> status maintenance only

	def get_current_invoice(self):
		"""The most recent invoice that bills this subscription: header link *or* a line link (DECISIONS.md D-10).

		The run-scoped guard stays first: for the rest of the process() call that put this sub's lines on the
		consolidation sink, the sink *is* the current invoice (the runner's own doc copies only).

		Ordered by this sub's own line period end, not the header to_date: a monthly sub sharing January's
		consolidated invoice with a yearly sub would otherwise see the January sink (to_date Dec 31) outrank its
		February invoice for the rest of the year and be status-evaluated against the wrong due date. Tie-breaks:
		posting_date keeps a late fire ahead of a stale draft; docstatus / creation prefer the submitted, newest
		amendment. Credit notes are excluded (their header period is NULL today; the exclusion is load-bearing
		once PR 5 stamps the credited window on them). Lines without a period (pre-migration rows, stock
		header-only invoices) fall back to the header to_date, i.e. core's ordering. Supplier subs go to core."""
		sink = self.flags.get("consolidation_current_invoice")
		if sink is not None:
			return sink
		if self.party_type != "Customer":
			return super().get_current_invoice()
		rows = [row for row in billing_invoices(self.name) if row.docstatus < 2]
		if not rows:
			return None
		latest = max(
			rows,
			key=lambda row: (
				_date_key(row.period_end),
				_date_key(row.posting_date),
				row.docstatus,
				row.creation,
			),
		)
		return frappe.get_doc("Sales Invoice", latest.name)

	@property
	def invoices(self):
		"""Every non-return invoice billing this subscription (header or line link), all docstatuses like core,
		oldest period first. Rows carry core's header from_date / to_date plus this sub's own period_start /
		period_end (header dates when the lines carry none). Supplier subs go to core."""
		if self.party_type != "Customer":
			return super().invoices
		return sorted(
			billing_invoices(self.name),
			key=lambda row: (_date_key(row.period_start), _date_key(row.posting_date), row.creation),
		)

	def has_outstanding_invoice(self):
		"""Submitted, non-return invoices billing this subscription that are neither Paid nor Credit Note Issued.

		Core counts `status != 'Paid'` on the header link: a Credit Note (status Return) would count forever and
		a fully settled original re-stamped `Credit Note Issued` (core set_status tests it before Paid) would keep
		every subscription on a consolidated invoice outstanding for good. Supplier subs go to core."""
		if self.party_type != "Customer":
			return super().has_outstanding_invoice()
		return sum(
			1
			for row in billing_invoices(self.name)
			if row.docstatus == 1 and row.status not in ("Paid", "Credit Note Issued")
		)

	@staticmethod
	def is_paid(invoice) -> bool:
		"""Core: status == Paid. A fully credited invoice is `Credit Note Issued`; without this every sub on it
		would be "past due" -> grace -> Unpaid / Cancelled (current_invoice_is_past_due)."""
		return invoice.status in ("Paid", "Credit Note Issued")

	def is_current_invoice_generated(self, _current_start_date=None, _current_end_date=None):
		"""Core: the current invoice's header posting_date lies inside the period. On a consolidated invoice the
		posting_date is sub[0]'s period start, so a member with a different current_invoice_start (a stub, or a
		member unblocked later) would fail that check and be billed again by the runner's in-lock re-check. Key
		on this sub's own line period start instead: every engine line carries subscription_period_start ==
		current_invoice_start at generation, so "start inside the window" is the exact analogue of core's rule.
		Lines without a period (pre-migration rows, stock header-only invoices) fall back to posting_date, i.e.
		core's rule. Supplier subs go to core.

		Deliberate stock-path deviation (DECISIONS.md D-10): a non-consolidating Customer sub billed at "Days
		before the current subscription period" whose end_date makes the period final has a header posting_date
		before the period start, so core re-bills that period on every later run inside its one-cycle window;
		the line rule bills it once."""
		if self.party_type != "Customer":
			return super().is_current_invoice_generated(_current_start_date, _current_end_date)
		if not (_current_start_date and _current_end_date):
			_current_start_date, _current_end_date = self._get_subscription_period(
				date=add_days(self.current_invoice_end, 1)
			)
		invoice = self.current_invoice
		if not invoice:
			return False
		starts = [
			d.subscription_period_start
			for d in invoice.get("items") or []
			if d.get("subscription") == self.name and d.get("subscription_period_start")
		]
		anchor = min(starts) if starts else invoice.posting_date  # legacy / stock invoice -> core's rule
		return getdate(_current_start_date) <= getdate(anchor) <= getdate(_current_end_date)

	def create_invoice(self, from_date=None, to_date=None, posting_date=None):
		"""Outside a consolidation run (or for a non-consolidating customer): the standalone invoice. Inside a
		run: sub[0] creates the sink through core's own create_invoice (submission suppressed), every later sub
		absorbs its lines into that same draft object; the runner submits it once. A sub's *second*
		create_invoice in one run (core's cancellation arrears) stays standalone, exactly like stock."""
		run = _active_run()
		if run is None or not self.is_consolidating():
			return self._create_standalone_invoice(from_date, to_date, posting_date)
		if from_date or to_date or any(m.name == self.name for m in run.members):
			# core's cancel_subscription (cancel_at_period_end / end_date branches of process()) generates a
			# second, explicitly windowed arrears invoice for the sub whose period invoice already went into
			# the sink; core produces a separate invoice there and its duplicate-line check
			# (allow_multiple_items = 0) would reject the same lines twice on one sink, so keep that invoice
			# standalone exactly like stock
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
		# core (create_invoice, "Discounts") stamps sub[0]'s fixed additional_discount_amount on the header once;
		# one standalone invoice per member would subtract it once each, so the sink must carry the sum.
		# Percentages scale with the total and need no adjustment. Same amount / apply_discount_on / trial
		# state across members is guaranteed by group_key; the guard mirrors core's is_trialling branch.
		if flt(self.additional_discount_amount) and not self.is_trialling():
			sink.discount_amount = flt(sink.discount_amount) + flt(self.additional_discount_amount)
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
