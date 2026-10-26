---
title: Install and upgrade
description: Install Automated Subscriptions on a Frappe v16 bench, see what the install adds to the site, upgrade it and remove it.
nav_title: Install
order: 1
updated: 2026-10-26
---

Automated Subscriptions installs like any Frappe app. It has no front-end build and no settings page of its own: everything it adds is custom fields and property setters on ERPNext documents, an index and the defaults of four settings.

## Before you start

- A bench with Frappe v16, and a site that already has ERPNext and [Frappe Payments](https://github.com/frappe/payments) installed. Both are in the app's `required_apps`.
- Python 3.14 or newer in the bench's environment.

> [!WARNING]
> Installing the app on a site with subscription plans that have a **Payment Gateway** set turns on automatic charging for their invoices straight away. On a staging copy of a live site, set the kill switch first: see [Stop all automatic charging](configuration.md#stop-all-automatic-charging).

## Install

Run these from the bench directory. `<SITE_NAME>` is your site, for example `site1.example.com`.

```bash
bench get-app https://github.com/Avunu/automated_subscriptions --branch version-16
bench --site <SITE_NAME> install-app automated_subscriptions
```

The `version-16` branch is made by the first release. Until then, get the app from `develop`:

```bash
bench get-app https://github.com/Avunu/automated_subscriptions --branch develop
```

## What installing sets up

| What                         | Details                                                                                                                                                                                                                                                                                                                  |
| ---------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Custom fields                | 19 fields on Customer, Subscription, Subscription Settings, Sales Invoice Item and Process Subscription. [Configuration](configuration.md#what-the-app-adds-to-your-site) lists each one.                                                                                                                                |
| Property setters             | Subscription is searched by **Party** and **Service Identifier**, and **Sales Invoice.subscription** is marked as indexed.                                                                                                                                                                                               |
| A database index             | An index on the `subscription` column of Sales Invoice, added by the `after_install` hook. It speeds up the lookups of the invoices a subscription has.                                                                                                                                                                  |
| Subscription Settings values | The defaults of the four settings the app adds (and of ERPNext's own) are written to the database: 16.67 for **Annual Discount Percentage**, on for **Credit Note On Cancellation**, Immediate for **Mid-term Billing Mode** and 30 for **Auto-charge Max Lateness (days)**. A value already saved is never overwritten. |
| Behaviour                    | Hooks on Customer and Sales Invoice, and extensions of the Subscription, Sales Invoice and Process Subscription classes. See [Configuration](configuration.md#behaviour).                                                                                                                                                |

The app creates no roles, no records and no scheduled job of its own. It adds no workspace or report either.

Nothing changes for existing customers: the new Customer fields are blank or off, so every customer stays on stock ERPNext billing until you set them. The new invoice lines of every customer subscription do carry the subscription, plan and period, and its **Service Identifier** as the description once you set one. Automatic charging, on the other hand, starts at once for every subscription plan that has a **Payment Gateway**.

## Upgrade

Update the app the way you update any Frappe app: `bench update`, or pull the new code in `apps/automated_subscriptions` and run:

```bash
bench --site <SITE_NAME> migrate
```

`bench migrate` re-syncs the custom fields and property setters, writes the default of any Subscription Settings field that has no stored value yet, and runs the patches the site has not run:

| Patch                                                                       | What it does                                                                                                                                                                                                                           |
| --------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `automated_subscriptions.patches.v1_1.add_sales_invoice_subscription_index` | Adds the index on `Sales Invoice.subscription` to a site that had the app before the install did it. It does nothing when the index exists.                                                                                            |
| `automated_subscriptions.patches.v1_2.drop_auto_billing_delay`              | Deletes the **Auto-billing Delay** custom field of Subscription Settings and its stored value. Earlier versions had that setting, and nothing read it: when a payment is collected comes from **Days Until Due** on each Subscription. |

A new site never runs these patches: Frappe's `install-app` marks every patch of a new app as done, and the install does the same work itself.

## Uninstall

```bash
bench --site <SITE_NAME> uninstall-app automated_subscriptions
```

Frappe takes a backup first (unless you pass `--no-backup`), then deletes the app's module, **Automated Subscriptions**, and the records that belong to it: every custom field and property setter listed in [Configuration](configuration.md#what-the-app-adds-to-your-site). The hooks and class extensions stop with the app.

What stays behind:

- The database columns of the custom fields, with their data. Frappe deletes the field definitions, not the columns.
- The index on `Sales Invoice.subscription`.
- The stored Subscription Settings values of the app's fields.
- Every document the app made: Sales Invoices with their lines, Credit Notes and Payment Requests. Invoice lines keep their subscription and period values in the columns above, though the form no longer shows them.

After uninstalling, the site's subscriptions are billed by stock ERPNext, one invoice per subscription, and nothing raises Payment Requests for them.

## Next

- [Configuration](configuration.md) lists every setting and everything the app adds.
- [Charge subscription invoices](payment-requests.md) sets up the gateway on your plans.
