<!-- Copyright (c) 2026, Avunu LLC and contributors
For license information, please see license.txt-->

<!-- frappe-nix:begin header -->

<div align="center">

<img src="automated_subscriptions/public/images/automated_subscriptions-logo.svg" width="80" alt="">

# Automated Subscriptions

Subscription auto-billing for ERPNext: payment requests, anchors and proration

[![CI](https://github.com/Avunu/automated_subscriptions/actions/workflows/ci.yml/badge.svg?branch=develop)](https://github.com/Avunu/automated_subscriptions/actions/workflows/ci.yml)
[![Latest release](https://img.shields.io/github/v/release/Avunu/automated_subscriptions)](https://github.com/Avunu/automated_subscriptions/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](license.txt)
[![Frappe v16](https://img.shields.io/badge/Frappe-v16-blue)](https://github.com/frappe/frappe/tree/version-16)

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/consolidated-invoice-dark.webp">
  <img src="docs/screenshots/consolidated-invoice-light.webp" alt="One Sales Invoice billing four subscriptions of Riverbend Dental Group, each prorated for January 15 to 31">
</picture>

</div>

<!-- frappe-nix:end header -->

## Overview

ERPNext's Subscription module makes a Sales Invoice every billing period, but it does not collect the money. For a
direct-debit gateway such as GoCardless, where the customer has already signed a mandate, nothing tells the gateway
to charge that mandate for the new invoice. This app closes that gap: when a Sales Invoice that bills a Subscription
is submitted, it raises a Payment Request for the outstanding amount and, when the gateway supports it, charges the
customer's existing mandate straight away.

It also has an opt-in billing engine for businesses that sell many small recurring services to the same customer:
calendar or anniversary billing anchors with day-accurate proration, one consolidated invoice per customer per due
date, and credit notes for mid-term cancellations. The engine is switched on per customer; a customer with none of
its fields set is billed by stock ERPNext.

Full documentation: [docs.avunu.net/automated-subscriptions/docs/](https://docs.avunu.net/automated-subscriptions/docs/).

## Features

- **Payment Requests for subscription invoices.** Every submitted subscription invoice with an outstanding amount
  gets a Payment Request through the gateway set on its plans. With a gateway that takes the payment itself, such as
  GoCardless with a mandate, the request is submitted without e-mailing the customer.
- **Safe defaults.** Credit notes and fully paid invoices are skipped, an invoice whose due date is too far in the
  past is left as a draft Payment Request for review, and a site-level kill switch stops all automatic charging.
- **Billing anchors.** Align a customer's subscriptions to calendar months or years, or to an anniversary date, with
  the first period prorated by calendar days. Monthly plans can be billed yearly with a discount.
- **Consolidated invoices.** Bill every subscription of a customer that falls due on the same day on one Sales
  Invoice, one line per subscription, each line recording the plan and the exact period it bills.
- **Credit notes on cancellation.** Cancelling an anchored or consolidated subscription that was billed in advance
  credits the unused days.
- **Service Identifier.** A free-text field on Subscription (a site, a domain, a seat pool), shown in the list,
  searchable, and used as the invoice line description.
- **Process Subscription per customer.** An optional Customer field limits a Process Subscription run to one
  customer.

<!-- frappe-nix:begin compatibility -->

## Compatibility

| Frappe | Branch       |
| ------ | ------------ |
| v16    | `version-16` |

| Requires                                       | Version            |
| ---------------------------------------------- | ------------------ |
| [erpnext](https://github.com/frappe/erpnext)   | `>=16.0.0,<17.0.0` |
| [payments](https://github.com/frappe/payments) | `>=0.0.1,<1.0.0`   |

<!-- frappe-nix:end compatibility -->

<!-- frappe-nix:begin installation -->

## Installation

```bash
bench get-app https://github.com/Avunu/automated_subscriptions --branch version-16
bench --site <site> install-app automated_subscriptions
```

<!-- frappe-nix:end installation -->

Until the first release creates the `version-16` branch, install from `develop` instead:
`bench get-app https://github.com/Avunu/automated_subscriptions --branch develop`.

The app needs ERPNext and the [Frappe Payments](https://github.com/frappe/payments) app (both are in
`required_apps`, so install them on the site first) and Python 3.14 or newer. Installation adds the custom fields
listed in [docs/configuration.md](docs/configuration.md) and an index on `Sales Invoice.subscription`; sites that
already have the app get the index from a patch on the next `bench migrate`. See [docs/install.md](docs/install.md)
for upgrading and uninstalling.

## Configuration

1. To charge subscription invoices automatically, set **Payment Gateway** on each **Subscription Plan** to a
   **Payment Gateway Account** from Frappe Payments, and make sure the customer has an active mandate. A plan with no
   gateway is never charged.
2. To use the billing engine for a customer, set the fields of the **Subscription Billing** section on the
   **Customer**: **Consolidate Subscription Invoices**, **Subscription Billing Anchor** (Calendar or Anniversary),
   **Subscription Billing Anchor Date** and **Subscription Billing Interval**.
3. Check the **Automated Subscriptions** section of **Subscription Settings**: **Annual Discount Percentage**
   (16.67), **Credit Note On Cancellation** (on), **Mid-term Billing Mode** (Immediate) and **Auto-charge Max
   Lateness (days)** (30).

Payment timing comes from **Days Until Due** on each Subscription: the Payment Request is dated on the invoice's due
date. Add `"subscription_auto_charge_disabled": 1` to a site's `site_config.json` to stop all automatic charging on
that site; it is not restored with a database backup, so a restored staging copy does not charge live mandates.

Every setting and everything the app adds to a site are in [docs/configuration.md](docs/configuration.md).

## How It Works

- **Payment Requests.** An `on_submit` hook on Sales Invoice raises a Payment Request for a subscription invoice with
  an outstanding amount, through the one gateway its plans name, dated on the due date. Returns, mixed gateways and
  invoices more than **Auto-charge Max Lateness** days overdue are not charged. See
  [docs/payment-requests.md](docs/payment-requests.md).
- **Anchored billing.** Each period ends the day before the next anchor date, and a part period is billed at the
  plan rate times billed days over the days in the term. Anchored invoices ignore Pricing Rules. See
  [docs/anchored-billing.md](docs/anchored-billing.md).
- **Consolidated billing.** ERPNext's daily Process Subscription hands a consolidating customer's due subscriptions
  to one runner, which bills them on one invoice per set of matching header fields, under a per-customer lock, and
  submits it once. A failure rolls that customer's group back and is logged. See
  [docs/consolidated-invoices.md](docs/consolidated-invoices.md) and [docs/run-billing.md](docs/run-billing.md).
- **Cancellation.** For an anchored or consolidating customer, cancelling a subscription billed in advance issues a
  Credit Note for the unused days, left as customer credit. See [docs/cancellation.md](docs/cancellation.md).

The design is described in [docs/development/architecture.md](docs/development/architecture.md).

## Limitations

- Customer subscriptions only; supplier (purchase) subscriptions are never anchored or consolidated.
- Anchoring supports plans billed in months or years. Day and Week plans, plans priced by Monthly Rate, monthly
  plans whose month count does not divide a year (when the customer is billed yearly), and **Follow Calendar
  Months** cannot be combined with an anchor.
- A consolidated invoice needs one payment gateway. If its plans point to different gateways, no automatic charge is
  made.
- Backdated start dates are limited for anchored and consolidating customers; load such subscriptions as an import
  and replay the elapsed periods with Process Subscription.
- No credit notes for subscriptions billed at the end of the period, and no gateway refunds.
- With the stock Frappe Payments app, GoCardless charges a mandate but does not settle the invoice, so the Payment
  Entry for a collected payment is made by hand.
- Two lines with the same item and description cannot share a consolidated invoice unless Selling Settings allows an
  item more than once; give each subscription its own Service Identifier.

The full list, and the messages the app raises, are in [docs/troubleshooting.md](docs/troubleshooting.md).

<!-- frappe-nix:begin support -->

## Support

- Bug reports and questions: [GitHub issues](https://github.com/Avunu/automated_subscriptions/issues)
- E-mail: <mail@avu.nu>

<!-- frappe-nix:end support -->

<!-- frappe-nix:begin development -->

## Development

```bash
nix develop        # or: direnv allow
prek install       # the git hooks CI runs
frappe-test        # the tests, the way CI runs them
```

The managed files, checks and release flow are described in the [app standards documentation](https://docs.avunu.net/frappe-nix/docs/app-standards/).

<!-- frappe-nix:end development -->

The code layout, the tests and the design of the engine are described in
[docs/development/README.md](docs/development/README.md) and [docs/development/architecture.md](docs/development/architecture.md).

<!-- frappe-nix:begin license -->

## License

MIT, Copyright (c) 2024–present Avunu LLC → [license.txt](license.txt)

<!-- frappe-nix:end license -->
