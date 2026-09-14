"""Consolidated billing runner: one Sales Invoice per (party, posting date, header profile) group.

The runner is the only place a consolidating customer's subscriptions are billed. Core's daily job
(`create_subscription_process` -> `process_all` -> `Subscription.process`) reaches it through the mixin's
`process()` delegation; the desk and scripts reach it through `run_consolidated_billing` /
`preview_consolidated_billing`. Nothing here adds a scheduler hook (DECISIONS.md D-08).

Mechanics per group (DECISIONS.md D-18): a site-scoped file lock per party, a savepoint, the subscriptions
re-read with `for_update=True` (fresh snapshot under REPEATABLE-READ), the exact core due predicate re-checked,
then every due sub's own `process()`. Inside the run the mixin's `create_invoice` routes sub[0] through core's
`create_invoice` with submission suppressed (the "sink", a saved draft whose header is sub[0]) and absorbs every
later sub's lines into that same object; `_finalize` widens the header period, re-saves once and submits once.
A failure rolls the whole group back to the savepoint, logs it and marks the party "failed" in the run memo so
the delegating `process()` raises instead of billing standalone; that verdict is sticky for the rest of the run
because one party may split into several groups (§3.2 key) and a later group's success must not clear it.

Replay trap (core semantics, unchanged here): `set_subscription_status` compares the current invoice's due date
against *today*, not the run's posting date, so replaying old periods (Process Subscription with a past posting
date) sets Unpaid / Cancelled exactly as stock does. Run migration replays with `cancel_after_grace = 0` and never
trust statuses produced by a replay (DECISIONS.md Q-10).
"""

import contextlib
import hashlib
from datetime import date

import frappe
from erpnext import get_default_company
from erpnext.accounts.doctype.accounting_dimension.accounting_dimension import get_accounting_dimensions
from frappe import _
from frappe.query_builder.functions import IfNull
from frappe.utils import cint, flt, getdate, sbool
from frappe.utils.file_lock import LockTimeoutError
from frappe.utils.synchronization import filelock

from automated_subscriptions.automated_subscriptions.billing.exceptions import (
	ConsolidatedBillingError,
	ConsolidationLocked,
)

GroupKey = tuple
# (company, party_type, party, effective_posting_date: date, currency, sales_tax_template, cost_center,
#  dims: tuple, submit_invoice: int, is_trialling: bool, additional_discount_percentage: float,
#  additional_discount_amount: float, apply_additional_discount: str, days_until_due: int)
# additional_discount_amount is a *fixed* header amount that core stamps once from sub[0]; equality across the
# group is necessary but not sufficient for money conservation, so the mixin's _absorb_into adds every later
# member's amount to the sink header (one standalone invoice per member would subtract it once each).


# ---- run context (memo + active group) — on frappe.local, never frappe.flags -------------------------------


def _run_ctx() -> frappe._dict:
	"""The request/job-scoped run context, created on first use.

	`active`: None | _dict(key, posting_date, sink, members, dry_run) while a group is being billed.
	`done`:   {(company, party_type, party, "YYYY-MM-DD"): "ok" | "failed"}; never written by dry runs.
	"failed" is sticky for the rest of the run (see _mark_done). Lives on frappe.local (IntegrationTestCase
	deep-copies frappe.local.flags per class, which would copy a Document held there)."""
	ctx = getattr(frappe.local, "consolidated_billing", None)
	if ctx is None:
		ctx = frappe.local.consolidated_billing = frappe._dict(active=None, done={})
	return ctx


def _active_run() -> frappe._dict | None:
	"""The group being billed right now, without creating the context (stock subs never touch frappe.local)."""
	ctx = getattr(frappe.local, "consolidated_billing", None)
	return ctx.active if ctx is not None else None


def _mark_done(ctx, memo_key, state: str) -> None:
	"""The memo is per party (the delegating process() can only compute the party key) while groups are finer
	(tax template, days_until_due, effective posting date...): once any group of a party failed the party stays
	"failed" for the run, so the delegating process() of the failed group's members raises instead of billing
	them standalone. Members of a sibling group that did succeed raise too (no money effect: the runner already
	billed and saved them; they only miss one status pass)."""
	if state == "ok" and ctx.done.get(memo_key) == "failed":
		return
	ctx.done[memo_key] = state


# ---- lock name and group key -------------------------------------------------------------------------------


def party_lock_name(company: str, party_type: str, party: str) -> str:
	"""Site-scoped lock name; party names contain spaces, apostrophes, possibly "/" — never use them raw."""
	digest = hashlib.sha1(f"{company}|{party_type}|{party}".encode(), usedforsecurity=False).hexdigest()[:16]
	return f"subbill-{digest}"


def effective_posting_date(sub, run_posting_date) -> date:
	"""The posting date core will stamp on this sub's invoice (core create_invoice)."""
	sub._realign_current_period()  # current_invoice_end must be the aligned value (PR 2 R7)
	if sub.generate_invoice_at == "Beginning of the current subscription period":
		return getdate(sub.current_invoice_start)
	if sub.generate_invoice_at == "Days before the current subscription period":
		return getdate(run_posting_date or sub.current_invoice_start)
	return getdate(sub.current_invoice_end)


def group_key(sub, run_posting_date) -> GroupKey:
	"""Everything core copies from sub[0] onto the invoice header must be equal across the group.

	The fixed additional_discount_amount is the one header value equality does not settle: _absorb_into sums
	it over the members so the sink subtracts it once per subscription, like standalone billing would."""
	dims = tuple(sub.get(d) or "" for d in get_accounting_dimensions())
	currency = frappe.db.get_value("Subscription Plan", sub.plans[0].plan, "currency") if sub.plans else ""
	return (
		sub.company or get_default_company(),
		sub.party_type,
		sub.party,
		effective_posting_date(sub, run_posting_date),
		currency or "",
		sub.sales_tax_template or "",
		sub.cost_center or "",
		dims,
		cint(sub.submit_invoice),
		bool(sub.is_trialling()),
		flt(sub.additional_discount_percentage),
		flt(sub.additional_discount_amount),
		sub.apply_additional_discount or "",
		cint(sub.days_until_due),
	)


# ---- public entry points -----------------------------------------------------------------------------------


@frappe.whitelist()
def run_consolidated_billing(
	posting_date=None, party=None, party_type="Customer", company=None, *, dry_run=False, lock_timeout=60.0
) -> list[dict]:
	"""Bill every due consolidating subscription, one invoice per group; returns one summary per group.

	A group that failed is rolled back, logged and reported in its summary's `errors`; the next party is still
	visited, including when a party's lock is busy (its memo is marked "failed"). A party-scoped call whose
	lock is busy raises ConsolidationLocked (a ValidationError) instead: nothing was attempted and the caller
	must not treat the party as billed."""
	frappe.has_permission("Subscription", "write", throw=True)
	run_posting_date = getdate(posting_date)  # getdate(None) == today
	dry_run = bool(sbool(dry_run))
	lock_timeout = flt(lock_timeout)
	names = _candidate_subscriptions(party, party_type, company)
	groups = _group_due(names, run_posting_date)
	results = []
	for key, sub_names in groups:
		try:
			summary = _run_group(key, sub_names, run_posting_date, dry_run=dry_run, lock_timeout=lock_timeout)
		except ConsolidatedBillingError as e:  # one party's failure must not poison the next
			if party and isinstance(e, ConsolidationLocked):
				# party-scoped call: nothing else to report; the memo is already "failed" so the delegating
				# process() sees the raise instead of a silent standalone fallback
				raise
			results.append(
				{
					"company": key[0],
					"party_type": key[1],
					"party": key[2],
					"posting_date": str(key[3]),
					"invoice": None,
					"subscriptions": list(sub_names),
					"errors": str(e),
				}
			)
			continue
		if summary is not None:
			results.append(summary)
	return results


@frappe.whitelist()
def preview_consolidated_billing(
	posting_date=None, party=None, party_type="Customer", company=None
) -> list[dict]:
	"""Dry run: the real path up to (not including) submission, then rolled back to the savepoint."""
	frappe.has_permission("Subscription", "read", throw=True)
	return run_consolidated_billing(
		posting_date, party=party, party_type=party_type, company=company, dry_run=True
	)


# ---- candidate scan and grouping (outside the lock — decides which parties to visit) ----------------------


def _candidate_subscriptions(party, party_type, company) -> list[str]:
	"""Non-cancelled Customer subscriptions whose customer consolidates; ordered so the oldest sub of a party
	(after PR 7's M1 the surviving parent) is sub[0] and owns the sink's header.

	`company` matches the *effective* company: a blank stored company (core's backward-compat case, the field
	is not mandatory) bills under the default company (core create_invoice, group_key) and counts as it."""
	if party_type != "Customer":
		return []  # purchase-side subscriptions are never consolidated
	subscription = frappe.qb.DocType("Subscription")
	customer = frappe.qb.DocType("Customer")
	query = (
		frappe.qb.from_(subscription)
		.join(customer)
		.on(customer.name == subscription.party)
		.select(subscription.name)
		.where(
			(subscription.status != "Cancelled")
			& (subscription.party_type == "Customer")
			& (customer.consolidate_subscription_invoices == 1)
		)
		.orderby(subscription.party)
		.orderby(subscription.creation)
		.orderby(subscription.name)
	)
	if party:
		query = query.where(subscription.party == party)
	if company:
		company_filter = subscription.company == company
		if company == get_default_company():
			company_filter |= IfNull(subscription.company, "") == ""
		query = query.where(company_filter)
	return query.run(pluck="name")


def _is_due(sub, run_posting_date) -> bool:
	"""Exactly core process()'s generation predicate; never re-implemented."""
	return not sub.is_current_invoice_generated(
		sub.current_invoice_start, sub.current_invoice_end
	) and sub.can_generate_new_invoice(run_posting_date)


def _group_due(names, run_posting_date) -> list[tuple[GroupKey, list[str]]]:
	groups: dict[GroupKey, list[str]] = {}
	for name in names:
		sub = frappe.get_doc("Subscription", name)
		sub._realign_current_period()  # before the trigger / cap evaluation, like the mixin's process()
		if not _is_due(sub, run_posting_date):
			continue
		groups.setdefault(group_key(sub, run_posting_date), []).append(sub.name)
	# ordered by (party, effective_posting_date); the stable sort keeps creation order inside a group
	return sorted(groups.items(), key=lambda item: (item[0][2], item[0][3]))


# ---- one party group, inside lock + savepoint --------------------------------------------------------------


def _run_group(key, sub_names, run_posting_date, *, dry_run, lock_timeout):
	company, party_type, party = key[0], key[1], key[2]
	memo_key = (company, party_type, party, str(run_posting_date))
	ctx = _run_ctx()
	try:
		with filelock(party_lock_name(company, party_type, party), timeout=lock_timeout):
			if not frappe.in_test and not dry_run:
				frappe.db.commit()  # fresh REPEATABLE-READ snapshot; nothing pending at this point
			sp = "subbill_" + frappe.generate_hash(length=8)  # bare identifier; never derived from the party
			frappe.db.savepoint(sp)
			sp_open = True  # False once released: rollback-to-savepoint would then raise 1305
			ctx.active = frappe._dict(
				key=key, posting_date=run_posting_date, sink=None, members=[], dry_run=dry_run
			)
			try:
				# latest committed rows, row-locked until the group commits
				subs = [frappe.get_doc("Subscription", name, for_update=True) for name in sub_names]
				for sub in subs:
					sub._realign_current_period()
				skipped = [sub.name for sub in subs if not _is_due(sub, run_posting_date)]
				subs = [sub for sub in subs if sub.name not in skipped]
				if not subs:
					frappe.db.release_savepoint(sp)
					sp_open = False
					if not dry_run:
						_mark_done(ctx, memo_key, "ok")
					return None
				for sub in subs:
					sub.process(
						posting_date=run_posting_date
					)  # the mixin sees the active run -> core process
				if ctx.active.sink is None:
					# every member fell through core's own trigger check after all; nothing to finalise
					frappe.db.release_savepoint(sp)
					sp_open = False
					if not dry_run:
						_mark_done(ctx, memo_key, "ok")
					return None
				summary = _finalize(ctx.active, skipped)
				if dry_run:
					frappe.db.rollback(save_point=sp)
					sp_open = False
				else:
					frappe.db.release_savepoint(sp)
					sp_open = False
					if not frappe.in_test:
						frappe.db.commit()  # visible to the next lock holder
					_mark_done(ctx, memo_key, "ok")
				return summary
			except frappe.QueryDeadlockError as e:
				frappe.db.rollback()  # the server already rolled back the transaction; the savepoint is gone
				_log_group_failure(key, e)
				if not dry_run:
					_mark_done(ctx, memo_key, "failed")
				raise ConsolidatedBillingError(
					_("Consolidated billing for {0} hit a deadlock").format(party)
				) from e
			except Exception as e:
				if sp_open:
					frappe.db.rollback(save_point=sp)
				else:
					# the group is already released / committed; a failure inside commit()'s after_commit
					# callbacks (Redis enqueue, email, webhooks) must not turn into a raw 1305 "SAVEPOINT does
					# not exist" that escapes process_all (which only catches ValidationError)
					frappe.db.rollback()
				_log_group_failure(key, e)
				if not frappe.in_test and not dry_run:
					frappe.db.commit()  # persist the Error Log row
				if not dry_run:
					_mark_done(ctx, memo_key, "failed")
				if isinstance(e, ConsolidatedBillingError):
					raise
				raise ConsolidatedBillingError(
					_("Consolidated billing for {0} failed: {1}").format(party, e)
				) from e
			finally:
				ctx.active = None  # never let the sink leak into the next group / sub
	except LockTimeoutError as e:
		if not dry_run:
			# the party is being billed elsewhere: the siblings in this batch must not fall back to standalone
			_mark_done(ctx, memo_key, "failed")
		raise ConsolidationLocked(_("Billing for {0} is running elsewhere").format(party)) from e


def _log_group_failure(key, exc) -> None:
	with contextlib.suppress(Exception):  # logging must never replace the error being handled
		frappe.log_error(
			title=f"Consolidated billing failed: {key[2]}",
			message=frappe.get_traceback(with_context=True) or f"{type(exc).__name__}: {exc}",
			reference_doctype=key[1],
			reference_name=key[2],
		)


# ---- finalisation and summary ------------------------------------------------------------------------------


def _finalize(run, skipped) -> dict:
	sink = run.sink
	sink.from_date = min(
		m.period[0] for m in run.members
	)  # from the absorbed periods, never line service dates
	sink.to_date = max(m.period[1] for m in run.members)
	sink.save()  # re-validate, rescale the payment schedule; from <= to holds by construction
	if all(m.submit_invoice for m in run.members) and not run.dry_run:
		sink.submit()  # ONE submit; on_submit hooks (Payment Request) run once
	return _summarise(sink, run, skipped)


def _date_str(value) -> str | None:
	return str(getdate(value)) if value else None


def _summarise(sink, run, skipped) -> dict:
	members = {m.name: m for m in run.members}
	lines = []
	for d in sink.items:
		member = members.get(d.subscription)
		period = member.period if member else (None, None)
		lines.append(
			{
				"idx": d.idx,
				"subscription": d.subscription,
				"subscription_plan": d.subscription_plan,
				"service_identifier": member.service_identifier if member else None,
				"item_code": d.item_code,
				"description": d.description,
				"qty": d.qty,
				"rate": d.rate,
				"amount": d.amount,
				"period_start": _date_str(d.subscription_period_start or period[0]),
				"period_end": _date_str(d.subscription_period_end or period[1]),
			}
		)
	summary = {
		"company": sink.company,
		"party_type": run.key[1],
		"party": run.key[2],
		"currency": sink.currency,
		"posting_date": _date_str(sink.posting_date),
		"due_date": _date_str(sink.due_date),
		"sales_tax_template": sink.taxes_and_charges or None,
		"invoice": None if run.dry_run else sink.name,  # the dry-run draft is rolled back
		"subscriptions": [m.name for m in run.members],
		"from_date": _date_str(sink.from_date),
		"to_date": _date_str(sink.to_date),
		"lines": lines,
		"net_total": sink.net_total,
		"total_taxes_and_charges": sink.total_taxes_and_charges,
		"grand_total": sink.grand_total,
		"rounded_total": sink.rounded_total,
		"payment_schedule": [
			{
				"due_date": _date_str(row.due_date),
				"invoice_portion": row.invoice_portion,
				"payment_amount": row.payment_amount,
			}
			for row in sink.payment_schedule
		],
		"skipped": [{"subscription": name, "reason": "not due"} for name in skipped],
		"errors": None,
	}
	if run.dry_run:
		summary["would_submit"] = all(m.submit_invoice for m in run.members)
	else:
		summary["docstatus"] = sink.docstatus
	return summary
