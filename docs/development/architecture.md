---
title: How it works
description: The design behind Automated Subscriptions, from the class extensions and anchored date arithmetic to the consolidation runner, the Payment Request hook and credit notes.
nav_title: How it works
order: 10
updated: 2026-10-26
---

This page describes the design behind the code, for developers. For the commands, see [Develop Automated Subscriptions](README.md). The decisions referred to as D-01 to D-23 in code comments are listed in [Design decisions](#design-decisions).

## The pieces

The app has no doctypes of its own. It changes ERPNext through custom fields, three class extensions and four document hooks, all registered in `hooks.py`.

| Piece                       | Where                            | Registered as                                                             |
| --------------------------- | -------------------------------- | ------------------------------------------------------------------------- |
| Subscription extension      | `custom/subscription.py`         | `extend_doctype_class` on Subscription (`AutoSubSubscription`)            |
| Sales Invoice extension     | `custom/sales_invoice.py`        | `extend_doctype_class` on Sales Invoice (`AutoSubSalesInvoice`)           |
| Process Subscription filter | `custom/process_subscription.py` | `extend_doctype_class` on Process Subscription                            |
| Payment Request hook        | `custom/sales_invoice.py`        | Sales Invoice `on_submit`                                                 |
| Pricing Rule policy         | `custom/sales_invoice.py`        | Sales Invoice `before_validate`, and the `set_missing_values` override    |
| Status refresh              | `custom/sales_invoice.py`        | Sales Invoice `on_cancel`, and the `refresh_subscription_status` override |
| Customer rules              | `custom/customer.py`             | Customer `validate`                                                       |
| Settings defaults           | `billing/settings.py`            | `after_sync` and `after_migrate`                                          |
| Index                       | `install.py`, `patches/v1_1/`    | `after_install`, and a `post_model_sync` patch                            |

The paths are relative to `automated_subscriptions/automated_subscriptions/`, except `hooks.py`, `install.py` and `patches/`.

## Two switches, one invariant

A customer's billing profile (`billing/profile.py`) is read from its four custom fields. Anchoring and proration key on **Subscription Billing Anchor**; consolidation keys on **Consolidate Subscription Invoices**; the invoice lookups key on the party type being Customer (D-03). They are independent so that a customer can consolidate without being anchored.

The invariant is that a customer with every field blank is billed by stock ERPNext. Every override in `AutoSubSubscription` returns `super()` when the profile does not apply, and the tests check that the stock path is untouched. Two additive changes apply to every customer subscription: the line links and periods (D-06), and the line-aware lookups (D-10).

## Anchored periods and proration

`billing/anchor.py` is pure date arithmetic with no database access. A `Grid` is an anchor date, an interval (Month or Year) and a count. `occurrence(grid, k)` computes the k-th anchor date directly from the anchor, never by iterating, and clamps a day the month does not have to its last day. `term_bounds` finds the term containing a date, and `period_end` is the day before the next occurrence. Calendar anchoring uses the fixed anchor 2000-01-01.

`resolve_grid` maps a customer interval and a plan interval to a grid, a number of plan units per term and a discount. A monthly plan under a yearly customer interval becomes a yearly grid with `12 / count` units and the **Annual Discount Percentage**. Day and Week plans, and counts that do not divide 12, raise `ValueError`, which the Subscription extension turns into `UnsupportedBillingGrid`.

On Subscription:

- `get_current_invoice_end` returns the anchored period end, keeping ERPNext's trial branch and end-date clamp.
- `get_items_from_plans` computes the factor with `prorate_factor` (billed days over term days, with a full term short-circuited to an exact value) and passes it to ERPNext's `get_plan_rate`. ERPNext's own `get_prorata_factor` is never used on the anchored path. A zero rate raises `ZeroPlanRate`.
- `_realign_current_period` keeps the stored start and replaces the end with the anchored one, so a subscription that existed before its customer was anchored bills a stub up to the anchor. `process()` calls it before ERPNext evaluates the trigger date, because for **End of the current subscription period** the trigger is the period end itself.
- `validate` refuses **Follow Calendar Months** and **Monthly Rate** plans for anchored customers, and refuses backdated starts the deferred catch-up could not bill (D-05).

Pricing Rules overwrite an item's `rate` whenever the invoice's missing values are set (D-07). ERPNext's `create_invoice` calls `set_missing_values()` before it saves, so the `before_validate` hook alone is too late. The Subscription extension wraps invoice creation in `billing_context`, which sets `frappe.flags.subscription_billing`; `AutoSubSalesInvoice.set_missing_values` and the `before_validate` hook both read it and set `ignore_pricing_rule` on the invoice, which then keeps the setting on later saves. The override re-declares `@frappe.whitelist()`, because the desk calls `set_missing_values` through `run_doc_method` when **Is POS** is ticked.

`AutoSubSalesInvoice.set_payment_schedule` turns the `datetime.date` posting date that ERPNext's `create_invoice` stamps into an ISO string before ERPNext builds the payment schedule. ERPNext's whitelisted `get_payment_term_details` declares `posting_date: str | None`, and without the conversion Frappe's type check refuses the date, so every subscription invoice of a customer with a **Payment Terms Template** fails with `FrappeTypeError`.

`_enrich_items` stamps every Customer invoice line with its subscription, plan and billed period, and uses the **Service Identifier** as the description. The period goes into the app's own **Subscription Period Start** and **Subscription Period End**, not into ERPNext's service dates, because ERPNext clears those on every non-deferred line when it validates (D-01).

## Consolidated billing

`billing/runner.py` is the only place a consolidating customer's subscriptions are billed. There is no scheduler hook (D-08). ERPNext's daily job runs `create_subscription_process`, which submits a Process Subscription, which enqueues `process_all`, which calls `process()` on each subscription. The extension's `process()` hands the first subscription of a consolidating customer to `run_consolidated_billing` for that customer. The run memo then makes every sibling skip billing and fall through to ERPNext's `process()` for status maintenance only.

A run works like this:

1. **Scan.** `_candidate_subscriptions` lists the non-cancelled subscriptions of consolidating customers, oldest first. `_group_due` realigns each one, keeps those that ERPNext's own predicate says are due (`_is_due`: not `is_current_invoice_generated` and `can_generate_new_invoice`), and buckets them by `group_key`. The key holds everything ERPNext copies from the first subscription onto the invoice header (D-09). A subscription that cannot even be evaluated is logged and skipped, and it never gets billed on its own because its own `process()` raises the same error first.
2. **Lock.** Each group takes a site-scoped `filelock` named from a hash of the party (`party_lock_name`), commits to get a fresh snapshot, opens a savepoint and re-reads the subscriptions with `for_update=True` (D-18). Members that are no longer due, or whose key changed since the scan, are skipped.
3. **Bill.** Each member's `process()` runs inside the active run. The extension's `create_invoice` lets the first member create the invoice through ERPNext's own `create_invoice` with submission suppressed, so it stays a saved draft (the "sink"). Every later member's lines are appended to that same object by `_absorb_into`, which also adds up fixed **Additional Discount Amount**s and refuses duplicate item and description pairs.
4. **Finalise.** `_finalize` widens the header's **From Date** and **To Date** to cover every member's period, saves once and submits once when every member has **Submit Generated Invoices** on, so the `on_submit` hook raises one Payment Request.
5. **Commit or roll back.** The savepoint is released and committed. On any failure the group is rolled back to the savepoint, logged as "Consolidated billing failed: `<PARTY>`", and the party is marked failed in the memo. "failed" is sticky for the rest of the run, so the delegating `process()` of every member raises instead of billing standalone.

The run context (`RunContext`) lives on `frappe.local`, not `frappe.flags`, because Frappe's test base class deep-copies `frappe.local.flags` and would duplicate the sink. While a member is on the sink, `get_current_invoice` returns the sink, so a grace-period status check inside the run sees the new invoice (D-19).

A second `create_invoice` for a member in the same run (ERPNext's arrears invoice from `cancel_at_period_end` or an end date) stays standalone, as in stock ERPNext.

`preview_consolidated_billing` runs the same path with `dry_run=True`: no submit, then a rollback to the savepoint and, outside tests, a full rollback, because InnoDB keeps row locks taken after a savepoint until the transaction ends.

### Process Subscription with a customer

`AutoSubProcessSubscription.process_all_subscription` enqueues `process_customer_subscriptions` on the long queue with `enqueue_after_commit=True` instead of processing inside `on_submit`. `process_all` answers the first `ValidationError` with a full `frappe.db.rollback()`, which would discard the Process Subscription's own uncommitted submit. The job also clears the run memo first, so submitting a second Process Subscription in the same worker re-checks the rows.

### Mid-term subscriptions

`generate_invoices_till_date` is overridden rather than `after_insert`, so ERPNext's import, migrate and future-start checks still apply. For a consolidating customer it never runs the runner inside the insert request, because the runner commits between groups: on **Immediate** it enqueues `run_consolidated_billing` after the commit, and on **Next Daily Run** it does nothing. For an anchored, non-consolidating customer **Immediate** keeps ERPNext's catch-up loop.

ERPNext's daily run bills at most one cycle past `current_invoice_end` and then re-anchors the period to the run date, so a deferred first invoice for a start date too far back would silently lose periods. `validate` refuses those inserts (`BackdatedStartNotSupported`) instead (D-05).

## Invoice lookups

Consolidated invoices link subscriptions by line, not only by header, so the extension replaces ERPNext's lookups for Customer subscriptions (D-10):

- `billing_invoices` finds every non-return invoice with the subscription in its header or on a line, with that subscription's own period on it.
- `get_current_invoice` orders them by the subscription's own line period end, not the header's **To Date**: a monthly subscription sharing an invoice with a yearly one would otherwise see the yearly period as current all year.
- `has_outstanding_invoice` and `is_paid` treat **Credit Note Issued** like **Paid**, so a credited invoice does not keep every subscription on it outstanding.
- `is_current_invoice_generated` checks the subscription's own line period start rather than the header posting date, which on a consolidated invoice is the first member's period start.

Lines without a period (invoices made before the app, or by stock ERPNext) fall back to the header dates, which is ERPNext's own rule. Supplier subscriptions go straight to ERPNext.

When an invoice is cancelled, the `on_cancel` hook refreshes the status of every subscription on it, and `refresh_subscription_status` (which ERPNext calls when a Payment Entry is submitted or cancelled) does the same, logging and skipping a subscription that fails. There is deliberately no `on_submit` refresh: the invoice is submitted inside `Subscription.process()`, which saves the subscription afterwards, and a refresh saving it mid-submit would cause a `TimestampMismatchError` (D-23).

## Payment Requests

`subscription_payment_request` runs on submit of every Sales Invoice (D-11). It returns early for a non-subscription invoice, a return or nothing outstanding (ERPNext's `make_payment_request` throws on a zero amount, which would abort a credit note's submit), the kill switch, and migrate, patch, install or import. `resolve_payment_gateway_account` takes the gateways of the plans on the lines, or of the header subscription's plans for older invoices, and gives up with an Error Log when they differ.

The request is created with `make_payment_request(..., return_doc=True)`, then:

- `is_a_subscription` is cleared and `subscription_plans` emptied, because ERPNext fills them from the header subscription and validates every plan's gateway against the request's, which fails on a consolidated invoice.
- `transaction_date` is set to the invoice's due date as an ISO string. The stock GoCardless controller compares it with the mandate's next possible charge date, a string from the API, and a `date` there raises a `TypeError` that `payment_gateway_validation` swallows.
- A due date older than **Auto-charge Max Lateness (days)** leaves the request as a draft with a comment and an Error Log.
- `payment_gateway_validation()` returning `False` means the gateway handled the payment itself, so the request is submitted with `mute_email` set.

Late runs are normal (a period billed at its start may post days later), so nothing compares the posting date with today.

## Credit notes

`billing/credit_note.py` replaces ERPNext's `cancel_subscription` when `_credit_note_cancellation_applies` (D-12, D-12a). ERPNext's version bills the elapsed days in arrears and fixes the cancel date to today.

`cancel_with_credit_note` takes the same party lock as the runner, so a cancellation never runs alongside a billing run for the customer. For each source invoice with a line whose period ends on or after the cancel date, it builds a return with ERPNext's `make_sales_return` limited to those rows (`frappe.flags.selected_children`), and scales each row's rate, not its quantity, by the unused share. ERPNext's return validation refuses only a rate above the original, and the full negative quantity makes ERPNext itself refuse a second return on the same row. `_guard_double_refund` checks existing returns first. The credit note gets `update_outstanding_for_self = 1`, no payment schedule and a proportional share of a fixed discount; a tax row of type Actual is refused because it cannot be scaled.

## Settings and their defaults

Frappe does not write a Single doctype's field defaults to `tabSingles` until the document is saved, and `db.get_single_value` reads a missing row as 0, so a default of 16.67 would read as 0 (D-21). Worse, once the single has been saved, a field added later loads as `None` and the next save stores 0. `get_settings` applies the field defaults to missing values on every read, and `materialise_defaults` writes every missing default after the customizations are synced, on install (`after_sync`) and on every migrate (`after_migrate`). It never overwrites a stored value, and treats a NULL row as missing.

## The Sales Invoice index

The header link `Sales Invoice.subscription` had no index, and every lookup above filters on it (D-20). A property setter alone does not trigger a schema sync, and an index added outside the customization framework could be dropped by the next one, so the app ships a property setter marking the field indexed and adds the index explicitly: in `after_install` for new sites, and in a patch for sites installed earlier.

## Design decisions

These are the places where the code departs from what ERPNext or a first design would suggest, with the reason. Code comments refer to them by ID.

| ID    | Decision                                                                                                                                                      | Why                                                                                                                                                                                              |
| ----- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| D-01  | Line periods go in the app's own `subscription_period_start` and `subscription_period_end`, not in ERPNext's service dates.                                   | `AccountsController.validate` clears the service dates on every non-deferred line (`clear_stale_deferred_fields`).                                                                               |
| D-02  | **Consolidate Subscription Invoices** defaults to off.                                                                                                        | A Check column is created with its default and backfills every existing row: a default of 1 would route every existing customer through the runner.                                              |
| D-03  | Anchoring, consolidation and the lookups are separate switches.                                                                                               | A customer can consolidate without having its billing dates changed.                                                                                                                             |
| D-04  | Calendar ignores the anchor date; Anniversary requires it, enforced in `Customer.validate`.                                                                   | `mandatory_depends_on` is only checked in the browser.                                                                                                                                           |
| D-05  | **Mid-term Billing Mode** defaults to Immediate; Next Daily Run adds the backdated start check.                                                               | ERPNext bills on insert; a deferred daily run is capped at one cycle and re-anchors, losing periods. The runner commits between groups and must not run inside a user's request.                 |
| D-06  | Line links, periods and descriptions are added on the stock path too, for Customer subscriptions.                                                             | Consolidation and the line-aware lookups need them for a customer that consolidates without an anchor. Amounts, dates and statuses are unchanged.                                                |
| D-07  | Anchored invoices ignore Pricing Rules.                                                                                                                       | A Pricing Rule overwrites `rate` on every save and would undo the proration.                                                                                                                     |
| D-08  | No scheduler hook of the app's own.                                                                                                                           | ERPNext's daily Process Subscription already reaches every subscription; a second entry point would only contend for the lock.                                                                   |
| D-09  | The group key holds every header field ERPNext copies from the first subscription.                                                                            | Members of one invoice must agree on everything the header carries.                                                                                                                              |
| D-10  | The lookups find invoices by header or line link and order them by the subscription's own line period.                                                        | **Credit Note Issued** is not **Paid** in ERPNext, and the header posting date of a consolidated invoice belongs to its first member, so the stock lookups would block or re-bill other members. |
| D-11  | The Payment Request hook skips returns, zero outstanding, the kill switch and migrations; dates the request on the due date; leaves stale invoices as drafts. | Late runs are normal and must charge; a credit note would make `make_payment_request` throw and abort the submit.                                                                                |
| D-12  | Credit notes on cancellation apply to Customer subscriptions of anchored or consolidating customers, with the setting on.                                     | A consolidating customer that is not anchored yet still needs the credit.                                                                                                                        |
| D-12a | Subscriptions billed at the end of the period keep ERPNext's cancellation.                                                                                    | Nothing was prepaid, and ERPNext's arrears invoice is the right result there.                                                                                                                    |
| D-17  | **Service Identifier** is in the list view and in the search fields, not only a filter.                                                                       | With many subscriptions per customer, the identifier is what tells them apart.                                                                                                                   |
| D-18  | The runner keeps its context on `frappe.local`, locks on a hash of the party, uses a savepoint per group and re-reads rows `for_update`.                      | `filelock` is not re-entrant and uses its name as a file path; the test base class deep-copies `frappe.local.flags`.                                                                             |
| D-19  | The runner guards the current invoice itself while a member is on the sink.                                                                                   | The runner is correct on its own, whatever the lookups do.                                                                                                                                       |
| D-20  | The Sales Invoice index is added explicitly, with a property setter for the field.                                                                            | A property-setter-only customization never triggers a schema sync, and a bare index can be dropped by one.                                                                                       |
| D-21  | Settings are read through `get_settings`, with the defaults written on migrate.                                                                               | Frappe does not store a Single's defaults until it is saved.                                                                                                                                     |
| D-22  | Tests freeze today by patching the date function directly, roll back in `tearDown`, and create their records in `setUp`.                                      | `freezegun` is not a dependency, and the integration test base class rolls back once per class, not per test.                                                                                    |
| D-23  | Status refresh on `on_cancel`, never on `on_submit`.                                                                                                          | Cancelling from the desk happens outside `process()`; a refresh on submit would raise `TimestampMismatchError` inside the runner, which saves the subscription itself.                           |
