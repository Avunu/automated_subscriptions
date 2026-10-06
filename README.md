# Automated Subscriptions

Auto-billing for ERPNext Subscriptions using Frappe Payments.

ERPNext's Subscription module generates a Sales Invoice every billing period, but it does not collect the money.
For a direct-debit gateway such as GoCardless, where the customer has already signed a mandate, nothing tells the
gateway to charge that mandate for the new invoice. This app closes that gap: every time a Sales Invoice that
belongs to a Subscription is submitted, it raises a Payment Request for the outstanding amount and, when the
gateway supports it, charges the customer's existing mandate straight away.

The same app also contains an opt-in billing engine for businesses that sell many small recurring services to
the same customer: calendar or anniversary billing anchors with day-accurate proration, one consolidated invoice
per customer per due date, and credit notes for mid-term cancellations. Every engine feature is switched on per
customer; a customer with no engine fields set is billed by stock ERPNext.

## Requirements

- Python 3.12 or newer
- Frappe and ERPNext version 16 (developed and tested against ERPNext 16.34.2 and Frappe 16.33.1; other versions
  are untested)
- The [Frappe Payments](https://github.com/frappe/payments) app, with a Payment Gateway Account configured for
  the gateway you collect through (for example GoCardless)

`erpnext` and `payments` are declared in `required_apps`, so both must be present on the bench and installed on
the site before this app.

## Installation

```sh
bench get-app https://github.com/Avunu/automated_subscriptions
bench --site your-site.example install-app automated_subscriptions
```

Installation creates the custom fields described below and an index on `Sales Invoice.subscription`. Sites that
already have the app get the index from a patch on the next `bench migrate`.

## Configuration

### Charging a subscription invoice

1. Set up the gateway in Frappe Payments, with a Payment Gateway Account.
2. On each **Subscription Plan** that should be auto-charged, set **Payment Gateway** to that account. A plan
   with no gateway is never charged automatically.
3. Make sure the customer has an active mandate (or equivalent authorisation) with the gateway.

No other setup is needed for the Payment Request behaviour.

### Subscription Settings

The app adds an **Automated Subscriptions** section to Subscription Settings:

| Field                          | Default        | Effect                                                                                                                                                                  |
| ------------------------------ | -------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Auto-charge Max Lateness       | 30 days        | A subscription invoice whose due date is more than this many days in the past is not charged; its Payment Request is left as a draft for review.                        |
| Annual Discount Percentage     | 16.67          | Discount used when a monthly-priced plan is billed on a yearly term: yearly rate = monthly rate x 12 x (1 - discount).                                                  |
| Credit Note On Cancellation    | on             | On mid-term cancellation of an anchored or consolidated subscription, issue a Credit Note for the unused part of the current period.                                    |
| Mid-term Billing Mode          | Immediate      | For anchored or consolidated subscriptions added mid-term: bill the prorated first invoice as soon as the subscription is created (Immediate), or leave it to the next daily run (Next Daily Run). |
| Auto-billing Delay             | 0              | Present for historical reasons; the current code does not read it.                                                                                                      |

Defaults are written to the database on install and on every `bench migrate`, so the form and the engine always
see the same values.

### Per-customer billing engine

The **Subscription Billing** section on the Customer form controls the engine. All fields blank means stock
ERPNext behaviour.

| Field                               | Effect                                                                                                                                                                      |
| ----------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Consolidate Subscription Invoices   | Bill every subscription of this customer that is due on the same day on one invoice. Off by default, including for existing customers.                                      |
| Subscription Billing Anchor         | Blank: stock behaviour. **Calendar**: periods follow calendar months or years. **Anniversary**: periods follow the day (and, for yearly terms, the month) of the anchor date. |
| Subscription Billing Anchor Date    | Required for Anniversary and cleared otherwise. Periods end the day before the date's next occurrence.                                                                      |
| Subscription Billing Interval       | Blank: follow each plan's own interval. **Year**: bill monthly-priced plans once a year at 12 x monthly x (1 - Annual Discount Percentage).                                 |

Subscriptions also gain a free-text **Service Identifier** (what the subscription bills: a site, a domain, a seat
pool). It is shown in the list view, searchable, and used as the invoice line description, which is what tells
the lines of a consolidated invoice apart. **Process Subscription** gains an optional **Customer** field to limit a
run to one customer.

### Kill switch

Add `"subscription_auto_charge_disabled": 1` to the site's `site_config.json` to stop automatic charging on that
site. The setting lives in the site directory and is not restored with a database backup, which keeps a restored
staging copy from charging live mandates.

## How it works

### Payment Requests

When a Sales Invoice is submitted, the app creates a Payment Request for it if all of the following hold:

- the invoice bills a Subscription (by its header link or by any line link);
- it is not a return (credit note) and has an outstanding amount greater than zero;
- the site kill switch is not set, and the site is not in the middle of a migrate, patch, install or data import;
- exactly one payment gateway can be determined from the plans on the invoice (blank plan gateways ride along
  with a set one; no gateway means no charge, and two different gateways are logged to the Error Log and left to
  a person);
- the due date is not older than the configured maximum lateness.

The Payment Request is dated on the invoice's due date, so a late-running scheduler still charges. If the gateway
takes the payment itself, as GoCardless does for a mandate, the Payment Request is submitted without sending an
email; otherwise it is submitted normally and the customer is notified. An invoice that is too stale to
auto-charge gets a draft Payment Request with an explanatory comment and an Error Log entry.

### Anchored billing and proration

For a customer with a billing anchor, the end of each period is the day before the next anchor occurrence. A
subscription that starts mid-term bills a first invoice for the stub, with the plan rate prorated by calendar days
(billed days divided by days in the term; a full term is never rounded). Prorated invoices ignore Pricing Rules,
because Pricing Rules overwrite the rate on every save and would undo the proration; move customer-specific
pricing onto the plan before anchoring such a customer. Anniversary months and days that do not exist in a month
(the 31st, February 29th) clamp to the last day of that month.

### Consolidated billing

For a customer that consolidates, the daily subscription job hands the customer's due subscriptions to a runner
(`billing/runner.py`). The runner groups them by everything that ends up on the invoice header (company,
currency, tax template, cost center, accounting dimensions, posting date, submit flag, trial state, additional
discounts, days until due), takes a per-customer lock, bills each group on a single Sales Invoice with one line per
subscription, and submits it once, so one invoice raises one Payment Request. Each group runs in its own
savepoint: a failure rolls that group back, is written to the Error Log and does not stop other customers.

Each invoice line records the subscription, the plan and the exact period it bills. A dry run builds
the real invoice and rolls it back; run it from a bench:

```sh
bench --site your-site.example execute \
  automated_subscriptions.automated_subscriptions.billing.runner.preview_consolidated_billing
```

`run_consolidated_billing` takes the same arguments (`posting_date`, `party`, `party_type`, `company`) and
creates the invoices. There is deliberately no second scheduler hook: the engine runs from ERPNext's own daily
Process Subscription.

### Cancellation with a credit note

When a customer is anchored or consolidating, **Credit Note On Cancellation** is on, and the subscription is billed
in advance, Cancel Subscription issues one Credit Note per source invoice for the unused days of every billed
line, instead of ERPNext's arrears invoice. The cancel date is the last billed day. The credit is left as
unallocated customer credit; the original invoice is untouched and no refund is sent to the gateway. A line that has
already been credited is refused rather than credited twice.

## Limitations

- Customer subscriptions only. Supplier-side (purchase) subscriptions are never anchored or consolidated.
- Anchoring supports plans billed in months or years. Day and Week plans, plans using Monthly Rate price
  determination, and monthly plans whose month count does not divide a year (when the customer is billed yearly)
  cannot be anchored, and Follow Calendar Months cannot be combined with an anchor. The app refuses these with an
  error on save rather than billing wrongly.
- A consolidated invoice needs one payment gateway. If the plans on it point to different gateways, no automatic
  charge is made.
- Backdated start dates are validated for anchored and consolidating customers. A start date inside a billing
  period that has already ended is rejected unless Generate New Invoices Past Due Date is enabled, and when catch-up
  billing is left to the daily run, a start date a full billing cycle or more behind the first period end is
  rejected as well. To load history, insert with `frappe.flags.in_import` set and replay the elapsed periods with
  Process Subscription and a past posting date.
- Credit notes are not issued for subscriptions billed in arrears, and gateway refunds are out of scope.
- Two lines with the same item and description cannot share one consolidated invoice unless Selling Settings allows
  adding an item multiple times; set a Service Identifier per subscription.

## Development

The code follows the repository's `pre-commit` configuration (ruff for linting, import sorting and formatting,
ssort for method order, tabs for indentation):

```sh
cd apps/automated_subscriptions
pre-commit install
pre-commit run --all-files
```

The test suite runs against a bench site with ERPNext installed:

```sh
bench --site your-test-site run-tests --app automated_subscriptions
```

Design decisions, and the verified ERPNext and Frappe behaviours that forced them, are recorded in
[docs/DECISIONS.md](docs/DECISIONS.md).

## License

[MIT](license.txt) - Copyright (c) 2026 Avunu LLC
