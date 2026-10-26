---
title: Run and preview billing
description: Bill one customer's subscriptions on demand, preview a consolidated billing run without saving it, and load subscriptions that have a billing history.
nav_title: Run and preview billing
order: 7
updated: 2026-10-26
---

ERPNext bills subscriptions once a day, from the scheduler's daily maintenance job, which submits a **Process Subscription** for today. This page covers billing outside that job: for one customer, as a dry run, and for past periods.

## Bill one customer now

The app adds a **Customer** field to **Process Subscription**, so a run can be limited to one customer.

1. Open **Process Subscription** and create a new one.
2. Set **Posting Date** (today, or a past date to bill an earlier period).
3. Set **Customer**. Optionally also set **Subscription** to process only one of that customer's subscriptions.
4. Save and submit.

The customer's subscriptions that are not cancelled are processed in the order they were created, in one background job on the long queue that starts once the submit is saved. For a consolidating customer the first subscription bills the whole customer, as in the daily run. The submit does not wait for the invoices, and a failure does not undo it: each subscription that fails is written to the Error Log (titled "Subscription failed" by ERPNext), and an unexpected error in the job is logged as "Process Subscription (customer) failed".

Without a **Customer**, Process Subscription works as in stock ERPNext.

For a single subscription, **Actions** > **Fetch Subscription Updates** on the Subscription form does the same for today. For a subscription of a consolidating customer it bills the whole customer.

## Bill consolidating customers from the console

`run_consolidated_billing` bills every due subscription of every consolidating customer, or of one customer, and returns a summary of each invoice:

```bash
bench --site <SITE_NAME> execute \
  automated_subscriptions.automated_subscriptions.billing.runner.run_consolidated_billing \
  --kwargs '{"party": "<CUSTOMER>"}'
```

It takes these arguments:

| Argument       | Default       | Meaning                                                                    |
| -------------- | ------------- | -------------------------------------------------------------------------- |
| `posting_date` | Today         | The run date. Subscriptions due by this date are billed.                   |
| `party`        | All customers | One customer.                                                              |
| `party_type`   | `Customer`    | Only customers are consolidated; any other value bills nothing.            |
| `company`      | All companies | One company. A subscription with no company counts as the default company. |
| `lock_timeout` | 60            | Seconds to wait for a customer that another run is billing.                |
| `dry_run`      | False         | True runs the billing and rolls it back, as the preview below does.        |

Each summary names the customer, the invoice, the subscriptions on it, every line with its period and amount, the totals and the payment schedule. A group that failed is reported with its `errors` instead, and the run goes on with the next customer. It needs write permission on Subscription.

## Preview a run

`preview_consolidated_billing` takes the same `posting_date`, `party`, `party_type` and `company` arguments and runs the real billing code up to, but not including, submitting the invoices. It then rolls everything back and returns the summaries, each with `would_submit` in place of a docstatus. No invoice, Payment Request or subscription change is kept.

```bash
bench --site <SITE_NAME> execute \
  automated_subscriptions.automated_subscriptions.billing.runner.preview_consolidated_billing \
  --kwargs '{"posting_date": "2026-02-01"}'
```

It needs read and write permission on Subscription: it checks read itself, then runs `run_consolidated_billing`, which checks write.

> [!WARNING]
> The preview ends with a full database rollback, so anything you have not committed in the same console session is lost. Commit first when you call it from `bench console`.

## Load subscriptions with a history

To bring subscriptions over from another system, with periods that have already been billed there, the backdated start checks get in the way (see [Backdated start dates](anchored-billing.md#backdated-start-dates)). Load them this way instead:

1. Set the kill switch so replayed invoices are not charged: see [Stop all automatic charging](configuration.md#stop-all-automatic-charging). Without it, every replayed invoice whose due date is less than **Auto-charge Max Lateness (days)** old is charged.
2. Untick **Cancel Subscription After Grace Period** in **Subscription Settings** for the duration.
3. Insert the subscriptions with `frappe.flags.in_import` set. The Data Import tool sets it; a script sets it itself. An imported subscription is not checked for backdated starts and is not billed when it is saved.
4. Replay the elapsed periods: submit a **Process Subscription** for each past period, with that period's billing date as **Posting Date**, oldest first. Each run bills one period per subscription.
5. Check the invoices, then turn the settings back.

> [!NOTE]
> ERPNext compares the due date of a subscription's current invoice with today, not with the run's posting date, when it sets the subscription's status. A replay therefore marks subscriptions **Unpaid**, or cancels them if step 2 is skipped, exactly as stock ERPNext would. Do not rely on the statuses a replay leaves: check them, and correct them once the replayed invoices are settled.

If you replay from a script that calls ERPNext's `process_all` more than once in the same process (in `bench console`, for example), set `frappe.local.consolidated_billing = None` between the calls. Otherwise a consolidating customer that was billed or failed in the first call is skipped in the next one. **Process Subscription** and `run_consolidated_billing` do not need this.
