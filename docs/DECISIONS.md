# DECISIONS — where the PR specs deviate from the plan, and what still needs Kevin

Plan: `.claude/plans/i-need-a-thorough-temporal-gray.md` (written against ERPNext 16.30; this bench is
16.34.2 / Frappe 16.33.1). Every deviation below was forced by a verified fact on the bench (reader reports and
adversarial verdicts produced while reconciling the plan against the actual 16.34.2 source and live data on
2026-09-14). Line numbers cite the actual files on the bench.

**History note:** this file originally lived in a session-scoped `/tmp` scratchpad and was lost when the sandbox
restarted on 2026-09-17 (nothing else was lost — it was reconstructed from conversation history and committed
here, in the repo, specifically so this can't happen again). Section D records what happened on 2026-09-17 when
Kevin was asked the open questions directly, since the file that would have recorded them the normal way didn't
exist yet.

## A. Deviations baked into the specs

| ID | Plan said | Spec does | Why (evidence) |
|---|---|---|---|
| D-01 | Per-line periods in core `Sales Invoice Item.service_start_date/service_end_date`, widened via two `depends_on` property setters | Two app-owned Date fields `subscription_period_start` / `subscription_period_end`; no property setters; core's deferred block kept verbatim | `AccountsController.validate` calls `clear_stale_deferred_fields()` on every non-return save (`accounts_controller.py:282-287`, `:671-686`) and nulls both dates on non-deferred rows — verified empirically; this is why all 1,425 live lines were NULL before migration. |
| D-02 | `Customer.consolidate_subscription_invoices` default 1 | default `"0"`; M4 sets 1 on the live customers | A Check column is created `DEFAULT cint(default)` (`frappe/database/schema.py:229-230,255-256`) and backfills every row incl. `_Test Customer`, which would route the 48 core tests through the runner. |
| D-03 | "Every override short-circuits when the customer has no anchor mode" (plan §Architecture) | Two independent switches: anchoring/proration keys on `subscription_billing_anchor_mode`; consolidation (PR 3) keys on `consolidate_subscription_invoices`; the invoice lookups key on `party_type == "Customer"`. "All fields blank ⇒ stock" remains the invariant and the core suite the gate. | The plan itself requires consolidation with anchoring off (M4 sets consolidate=1, anchor blank; dry-run "totals must match with anchoring off"); gating the runner on anchor mode gives Manheim 13 invoices + 13 Payment Requests. |
| D-04 | `subscription_billing_anchor_date` mandatory when mode = Calendar | Calendar ignores the date (effective anchor Jan 1 / day 1); Anniversary requires it; server-side `Customer.validate` enforces it (`mandatory_depends_on` is JS-only, `frappe/public/js/frappe/form/save.js:269-282`) | The date is meaningless for Calendar and indispensable for Anniversary; the plan's row was inverted. |
| D-05 | `Subscription Settings.mid_term_billing_mode` default "Next Daily Run" | default **Immediate**; "Next Daily Run" overrides `generate_invoices_till_date()` and adds a `validate` guard (`BackdatedStartNotSupported`) | 16.34's `after_insert` bills synchronously (stock = Immediate); a deferred daily `process(today)` is capped at one cycle and silently re-anchors, losing back-dated periods. The runner commits between groups and must not run inside a user's insert request. |
| D-06 | Line enrichment (links, period, description) implied to be part of the anchored path | `_enrich_items` runs on **both** paths for Customer subs | Needed for consolidation and lookups during the M4→M5 window, and to replace avunu's positional description hack for non-anchored customers; core amounts/dates/statuses unchanged. |
| D-07 | Proration = `get_plan_rate(..., prorate_factor=factor)` | Anchored invoices are saved with `ignore_pricing_rule = 1`; non-anchored invoices keep Pricing Rules | Pricing Rules overwrite `rate` on every save; Gap Church League's ERP is 120 via `PRLE-0002` while `get_plan_rate` = 105. See Q-01. |
| D-08 | PR 3 adds a `daily_long` scheduler hook | No new scheduler hook; core's `daily_maintenance` → `create_subscription_process` → `process_all` → our `process()` delegation is the only entry point | A second daily entry would only add lock contention. |
| D-09 | Group key `(company, party_type, party, currency, tax_template, posting_date)` | Key also includes cost_center, accounting dimensions, `submit_invoice`, `is_trialling()`, additional discounts, `apply_additional_discount`, `days_until_due` | Everything core copies from sub[0] onto the header must be equal across the group. |
| D-10 | Three lookup overrides; `has_outstanding_invoice` excludes returns | Four Subscription overrides: `get_current_invoice`, `invoices`, `has_outstanding_invoice`, `is_paid`, plus `is_current_invoice_generated` keyed on the sub's own line window | `Credit Note Issued` ≠ `Paid` would block every sibling on a consolidated invoice; header `posting_date` check would re-bill members whose period start differs. |
| D-11 | PR 4 fixes the dead posting-date guard; PR 5 adds `if doc.is_return: return` | `subscription_payment_request` rewritten: skip returns / non-positive outstanding / kill switch / migrate flags, gateway resolved from line plans, `transaction_date = due_date`, stale invoices left as a draft PR | Late fires are normal and must charge; a credit note makes `make_payment_request` throw and abort the submit. |
| D-12 | `cancel_subscription` override gated on anchor mode | Gate = Customer AND (anchored OR consolidating) AND `credit_note_on_cancellation` | Between M4 and M5 every live customer is consolidating but un-anchored; an anchor-only gate would keep core's no-credit cancellation for the whole book. |
| D-12a | (added during PR 5 implementation) | The credit-note route additionally requires `generate_invoice_at != "End of the current subscription period"` | A postpaid sub has nothing prepaid to credit; core's arrears-invoice cancellation is the correct outcome there, and is what the core cancellation tests pin. |
| D-13 | M3: `is_current_invoice_generated()` True for every Subscription | M3 = would-bill parity vs a pre-split snapshot | At rest every sub's current period is the next unbilled one → the plan's predicate is False for every non-cancelled sub. |
| D-14 | M2 matches on `(item_code, strip_html(description).lower())`; unmatched → parent | Match by Version-log child-row `name` for dates; multiset consumption + history keys + unique-item fallback for line attribution; unmatched lines stay NULL | Plans were renamed wholesale in 2024; positional/plan-name matching finds nothing; pinning an unmatched line to an arbitrary sibling fabricates history. |
| D-15 | M4 creates templates for 8 template-less customers | Only `NET30` is created; the other six get explicit `NET15` (= the Company default) | `Company "Avunu LLC".payment_terms = NET15` is already the fallback for those six. |
| D-16 | avunu's positional description hack removed "later than PR 3" | PR 6 must be deployed together with PR 3 on any site with avunu | The hack `IndexError`s on the sink's second save, not a `ValidationError` — kills the whole batch. |
| D-17 | `Subscription.service_identifier`: `in_standard_filter`, PS `search_fields` | also `in_list_view: 1`; `search_fields = "party, service_identifier"` | 57 per-item subs are unusable in a list without the site. |
| D-18 | Runner internals (`frappe.flags.consolidation_sink`, generic lock name, etc.) | Run context on `frappe.local.consolidated_billing`; lock name = `subbill-<sha1(...)[:16]>`; savepoint per group; re-check with `for_update=True`; `LockTimeoutError` → `ConsolidationLocked` | `filelock` is not reentrant and uses its name as a raw path component; `IntegrationTestCase` deep-copies `frappe.local.flags`. |
| D-19 | PR 3 relies on PR 4 for the grace-period hazard | PR 3 adds its own run-scoped `get_current_invoice` guard | Makes PR 3 correct on its own; PR 4 keeps the guard first and replaces the body after it. |
| D-20 | `Sales Invoice.subscription` "has no index today" (noted, not fixed) | PS `search_index = 1` + a patch adding the DB index explicitly | A PS-only file never runs `updatedb`; a bare index is dropped by the next `updatedb` without the PS. |
| D-21 | `Subscription Settings` gets 3 fields | 4: also `auto_charge_max_lateness_days` | Engine reads settings via `get_settings()` because Singles defaults are not materialised until saved once. |
| D-22 | Test scaffolding assumptions (`freeze_time`, per-test rollback) | Freeze via `patch("frappe.utils.data.now_datetime")`; explicit rollback; fixtures in `setUp` | `freezegun` is not installed; `IntegrationTestCase` rolls back per class only. |
| D-23 | Sales Invoice hook set: `refresh_subscription_status` mixin only | Also `doc_events["Sales Invoice"]["on_cancel"]`; no `on_submit` status hook | Desk cancellation is outside `process()`; an `on_submit` status hook would raise `TimestampMismatchError` inside the runner. |

## B. Open questions for Kevin, as originally posed (2026-09-14), before any answer

| ID | Question | Recommended default (what the specs assumed until answered) |
|---|---|---|
| Q-01 | Gap Church League's negotiated ERP price (120 via Pricing Rule `PRLE-0002`; plan rate 105). Anchored invoices ignore pricing rules, so the 120 must live in plan pricing before that customer is anchored. | Before M5 for Gap Church League: create a Fixed Rate plan at 120 (or an Item Price) and disable `PRLE-0002`. |
| Q-02 | Stale invoice (due date > `auto_charge_max_lateness_days` in the past): draft Payment Request for review, or skip entirely? | Draft + Error Log; 30 days. |
| Q-03 | Names of the per-line period fields. | `subscription_period_start` / `subscription_period_end`. |
| Q-04 | Split the two cancelled multi-row subs (-00012, -00014) too? | Yes. |
| Q-05 | Design by Analysis (-00004) and Todd Weiner (-00016) are stuck (`days_until_due = 30` vs their template) and unbilled. Zeroing `days_until_due` unblocks them → they will owe their current period. Accept, cancel first, or roll periods forward (discarding what's owed)? | Roll `current_invoice_start/end` forward, print "ARREARS NOT BILLED". |
| Q-06 | Pequea (-00007) skipped its 2026-05-02→2027-05-01 ERP year. | Out of migration scope; bill manually if desired. |
| Q-07 | Norden and LEAD's Static Site effectively due +5 days today; M4 gives NET15 (+10 days). | NET15 (lengthening accepted). |
| Q-08 | Four plan rows have blank descriptions. | Leave `service_identifier` blank. |
| Q-09 | Where the split/backfill helpers live: avunu or the public app. | avunu. |
| Q-10 | Replays with a past `posting_date` set `Unpaid`/`Cancelled` (stock behaviour, grace predicates compare with today). Override? | No — run migration replays with `cancel_after_grace = 0`. |
| Q-11 | Desk "Cancel Subscription": credit-note path for anchored **or** consolidating, vs. anchored only? | Anchored or consolidating. |
| Q-12 | `service_identifier` shown in the Subscription list view? | Yes. |
| Q-13 | Annual discount percentage. | 25 (a settings field, changeable any time). |
| Q-14 | Anniversary customers with subs on different start dates: which date becomes the anchor at M5? | Per customer at M5, after a preview; default = earliest active sub's start_date. |
| Q-15 | `Process Subscription.customer` vs. a generic party pair. | `customer` as planned. |
| Q-16 | Deploy ordering on production (PR 8 runbook). | PR 3+6 together; PR 4 before M4; PR 1-6 before the PR 7 patches; kill switch before any restore write; worker restart after hooks changes. |

## C. Amendments made during implementation

See D-12a above (added while implementing PR 5, before this file was lost).

## D. Kevin's answers (2026-09-17) and what changed

This file was unreachable on 2026-09-17 (session restart wiped the `/tmp` scratchpad it lived in). All 16
questions above were reconstructed from conversation history and put to Kevin directly. Twelve (Q-02, Q-03,
Q-04, Q-06, Q-07, Q-08, Q-09, Q-10, Q-11, Q-12, Q-15) were already shipped exactly as their recommended default
and weren't revisited. Four were asked explicitly:

| ID | Kevin's answer | Consequence |
|---|---|---|
| Q-01 | Move the $120 rate onto the plan itself; disable Pricing Rule `PRLE-0002`. | Confirmed the shipped assumption (D-07). Implemented 2026-09-17 as **fixtures** (`apps/avunu/avunu/fixtures/subscription_plan.json` - a Fixed Rate plan `ERP Hosting - Gap Church League` at $120, item `ERP`; `pricing_rule.json` - `PRLE-0002` exported with `disable: 1`) plus a one-time patch, `avunu.patches.v1_2.assign_gap_church_league_erp_plan`, that calls `sync_fixtures("avunu")` (patches run before fixtures sync in the same migrate) and then reassigns `ACC-SUB-2024-00015`'s plan row from `ERP Hosting` to the new plan via `avunu.avunu.subscription_migration.pricing.reassign_plan` (idempotent). Applied and verified on the local restore: the row now points to the new plan, `PRLE-0002` is disabled, and `subscription_preview_check` still previews Gap Church League's ERP line at exactly $120 - now sourced from the plan, not the rule. |
| Q-05 | **Bill the missed period(s) once the terms conflict is fixed — do not discard them.** | Reverses the original default. `avunu/avunu/subscription_migration/profiles.py`'s `apply_billing_profiles` now defaults to `roll_forward_stuck=False`: `days_until_due` is still zeroed (unblocking the subs) but `current_invoice_start/end` are left at their real, overdue value. The normal billing machinery (daily run / a `Process Subscription` replay) then bills the owed period(s) — capped at one period per `process()` call by core's `can_generate_new_invoice`, so this can never burst multiple invoices from one scheduler tick. The report names each unblocked sub and how many periods are owed (`owed_periods`, a read-only walk of the same period math). The old roll-forward-and-discard behaviour survives as an explicit `roll_forward_stuck=True` opt-in. Re-applied on the local restore 2026-09-17: ACC-SUB-2024-00004 and ACC-SUB-2024-00016 (+ its Domain child ACC-SUB-2026-00038) each correctly show **1 period now due** (2026-01-02..2027-01-01 and 2026-03-02..2027-03-01 respectively) instead of being silently rolled to 2027. |
| Q-13 | **16.7%** (two months free), not 25%. | `annual_discount_percentage`'s shipped default changed from `"25"` to `"16.67"` in `apps/automated_subscriptions/.../custom/subscription_settings.json`. Re-materialised on the local restore 2026-09-17. Does not affect any live customer's *current* pricing (no one is anchored yet); only matters once a customer with a Month-under-Year grid is anchored (M5). |
| Q-16 | Confirmed the runbook as written. | No code change. PR 8 (production cutover) remains entirely untouched and is never run from this bench. |

**Verification after re-applying M1-M4 with the corrected code (2026-09-17, fresh pristine restore):** M3 gate
passed for all 58 subscriptions; M2 linked 1,044 lines (369 NULL, same as before — content-matching is
unaffected by these two changes); `subscription_preview_check` matched 17/21 (customer, period start) groups to
the cent — identical to the original run — with the 4 remaining diffs fully explained: Porter Consulting
Engineers (Completed subscription, no future group expected), Front Range Bible Institute (2026-01 domain
repricing, no comparable history), and Design by Analysis / Todd Weiner (the Q-05 fix's intended effect: their
real, previously-discarded arrears now show as pending instead of vanishing).

## Still open before production

- PR 8 (production cutover) — not started, not planned from this bench.
