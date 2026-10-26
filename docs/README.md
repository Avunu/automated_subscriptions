---
title: Automated Subscriptions
description: Subscription auto-billing for ERPNext that raises Payment Requests, anchors billing periods, prorates by the day and consolidates invoices.
nav_title: Overview
order: 0
updated: 2026-10-26
---

ERPNext's Subscription module makes a Sales Invoice every billing period, but it does not collect the money. For a direct-debit gateway such as GoCardless, where the customer has already signed a mandate, nothing tells the gateway to charge that mandate for the new invoice. Automated Subscriptions closes that gap: when a Sales Invoice that bills a Subscription is submitted, it raises a Payment Request for the outstanding amount and, when the gateway supports it, charges the customer's existing mandate. It also adds an opt-in billing engine for businesses that sell many small recurring services to the same customer: calendar or anniversary billing anchors with proration by the day, one consolidated invoice per customer per due date, and credit notes for cancellations part-way through a period.

It is for ERPNext accounts teams that bill recurring services. The billing engine is switched on per customer, and a customer with none of its fields set is billed by stock ERPNext. The app runs on Frappe v16 and is free software under the [MIT licence](../license.txt).

## What you get

### Payment Requests for subscription invoices

Every submitted subscription invoice with an outstanding amount gets a Payment Request through the gateway set on its plans, dated on the invoice's due date. With a gateway that takes the payment itself, such as GoCardless with a mandate, the request is submitted without e-mailing the customer. Credit notes and paid invoices are skipped, an invoice that is too far past due is left as a draft Payment Request, and a site setting stops all automatic charging. See [Charge subscription invoices](payment-requests.md).

### Billing anchors and proration

Align a customer's subscriptions to calendar months or years, or to an anniversary date. A subscription that starts part-way through a period is billed for the days left, at the plan rate prorated by calendar days, and monthly plans can be billed once a year with a discount. See [Anchor billing periods](anchored-billing.md).

![The Subscription Billing section of a Customer: consolidated invoices on, billing anchored to the calendar](screenshots/customer-billing-light.webp)

### Consolidated invoices

Bill every subscription of a customer that falls due on the same day on one Sales Invoice, with one line per subscription. Each line records the subscription, the plan and the exact period it bills, and a **Service Identifier** on the Subscription names what the line is for. See [Consolidate invoices](consolidated-invoices.md).

![One Sales Invoice billing four subscriptions of Riverbend Dental Group, each prorated for January 15 to 31](screenshots/consolidated-invoice-light.webp)

### Credit notes on cancellation

Cancelling an anchored or consolidated subscription that was billed in advance issues a Credit Note for the unused days, in place of ERPNext's invoice for the days used. See [Cancel with a credit note](cancellation.md).

### Billing runs you can scope and preview

Process Subscription gets a **Customer** field that limits a run to one customer, and a dry run shows the consolidated invoices a run would make without saving them. See [Run and preview billing](run-billing.md).

## Where to start

| You are                                     | Read                                                                                                                                                                      |
| ------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| An accounts user billing customers          | [Charge subscription invoices](payment-requests.md), [Consolidate invoices](consolidated-invoices.md) and [Cancel with a credit note](cancellation.md)                    |
| The person setting up a customer's billing  | [Anchor billing periods](anchored-billing.md) and [Consolidate invoices](consolidated-invoices.md)                                                                        |
| The administrator installing and running it | [Install and upgrade](install.md), [Configuration](configuration.md), [Run and preview billing](run-billing.md) and [Limitations and troubleshooting](troubleshooting.md) |
| A developer changing the app                | [Development](development/README.md) and [How it works](development/architecture.md)                                                                                      |

## Requirements

- A bench with Frappe v16. The app declares `frappe >=16.0.0,<17.0.0`.
- ERPNext (`>=16.0.0,<17.0.0`) and [Frappe Payments](https://github.com/frappe/payments) (`>=0.0.1,<1.0.0`) on the same site. Both are in `required_apps`: install ERPNext, then Payments, then this app.
- Python 3.14 or newer, as `pyproject.toml` requires.
- For automatic charging, a payment gateway set up in Frappe Payments with a **Payment Gateway Account**, and a mandate (or equivalent authorisation) for each customer.

## Source and changes

The source is at [github.com/Avunu/automated_subscriptions](https://github.com/Avunu/automated_subscriptions). Release notes are on the [GitHub releases page](https://github.com/Avunu/automated_subscriptions/releases); release-please writes them to `CHANGELOG.md` with each release.
