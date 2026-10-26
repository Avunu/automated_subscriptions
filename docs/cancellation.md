---
title: Cancel with a credit note
description: Cancel an anchored or consolidated subscription part-way through a prepaid period and credit the customer for the unused days.
nav_title: Cancellation
order: 6
updated: 2026-10-26
---

When you cancel a subscription in stock ERPNext, it bills the days used so far in the current period. That suits subscriptions billed at the end of the period. For a subscription billed in advance, the customer has already paid for the whole period, so the app credits the days that will not be used instead.

## When a cancellation is credited

**Cancel Subscription** issues credit notes when all of these hold:

- The subscription's party is a customer whose **Subscription Billing Anchor** is set or who has **Consolidate Subscription Invoices** ticked.
- **Credit Note On Cancellation** is ticked in **Subscription Settings** (it is by default).
- The subscription is billed in advance: **Generate Invoice At** is **Beginning of the current subscription period** or **Days before the current subscription period**.

Any other subscription is cancelled the stock ERPNext way.

## Cancel a subscription

1. Open the **Subscription**.
2. Choose **Actions** > **Cancel Subscription** and confirm.

The subscription is cancelled as of today, and today counts as a used day. Its status becomes **Cancelled** and **Cancelation Date** is set to today. No further invoices are made for it.

## What the credit note contains

The app looks at every submitted invoice line that bills the subscription and whose period ends on or after the cancel date, and issues one Credit Note per source invoice:

- Each credited line has the original quantity and the original rate scaled by the unused share of its period: unused days (the days after the cancel date) divided by the days in the period, both in calendar days.
- Its description gets a second line, "Credit for unused service `<FROM>` - `<TO>` (`<UNUSED>` of `<TOTAL>` days)", and its **Subscription Period Start** and **Subscription Period End** record the credited days.
- Taxes are worked out again on the credited amounts. A fixed **Additional Discount Amount** on the source invoice is scaled down in proportion.
- The Credit Note is posted on the cancel date, or on the source invoice's posting date if that is later.
- **Update Outstanding for Self** is ticked, so the credit stays on the Credit Note as unallocated customer credit. The source invoice is not changed, and nothing is refunded through the payment gateway.

On a consolidated invoice only the cancelled subscription's lines are credited.

For example, a monthly plan of 49.00 billed for 1 to 31 January and cancelled on 10 January is credited for 11 to 31 January: 49.00 x 21 / 31 = 33.19.

## Cancel on another date

The desk button always cancels as of today. To cancel as of another date, call `cancel_with_credit_note` with the subscription, the cancel date and, when the cancel date falls in a closed or frozen accounting period, a posting date for the Credit Notes:

```bash
bench --site <SITE_NAME> execute \
  automated_subscriptions.automated_subscriptions.billing.credit_note.cancel_with_credit_note \
  --kwargs '{"subscription": "<SUBSCRIPTION>", "cancel_date": "2026-03-15"}'
```

It returns the subscription, the names of the Credit Notes and the total credited. The days are still counted from `cancel_date` when you pass `posting_date`. You need write permission on the Subscription and submit permission on Sales Invoice. Unlike the desk button, it refuses a subscription that does not meet the conditions above instead of falling back to stock cancellation.

## Credits are never issued twice

If a submitted Credit Note already credits one of the lines, the cancellation is refused with "Credit note `<CREDIT_NOTE>` already credits row `<ROW>` of `<INVOICE>`.", and nothing is cancelled. A draft return against the same lines does not stop it: the app shows an alert naming the draft so you can delete it.

For the other messages a cancellation can raise, see [Troubleshooting](troubleshooting.md#cancellation-with-a-credit-note).
