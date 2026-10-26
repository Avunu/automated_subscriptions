---
title: Develop Automated Subscriptions
description: Set up the frappe-nix dev shell, run the tests and checks, rebuild the demo and screenshots, and see what CI and the release flow do.
nav_title: Development
order: 9
updated: 2026-10-26
---

This page is for people changing the app. [How it works](architecture.md) explains the design behind the code. The managed files, checks and release flow that every Avunu app shares are described once in the frappe-nix [app standards documentation](https://docs.avunu.net/frappe-nix/docs/app-standards/); this page covers what is specific to this repository.

## Set up

The repository is a frappe-nix app. `nix develop --no-pure-eval` (or `direnv allow`) gives a shell with a bench that has Frappe, ERPNext and Frappe Payments at the versions CI tests against, and the same tools CI runs:

```bash
nix develop --no-pure-eval   # or: direnv allow
prek install                 # the git hooks CI runs
prek run --all-files
frappe-test                  # the tests with coverage, the way CI runs them
```

The app has no front-end code and no build step. `package.json` holds only the formatter, the linter and the TypeScript check of `marketplace/screenshots.ts`.

On a bench of your own, install ERPNext and Frappe Payments on a test site and run the tests there. Use a development or test site, never production:

```bash
bench --site <TEST_SITE> set-config allow_tests true
bench --site <TEST_SITE> run-tests --app automated_subscriptions
bench --site <TEST_SITE> run-tests --module automated_subscriptions.automated_subscriptions.tests.test_proration
```

## Where things live

```text
automated_subscriptions/
  hooks.py                     doc events, extend_doctype_class, install and migrate hooks
  install.py                   after_install: the index on Sales Invoice.subscription
  demo.py                      demo data for frappe-demo and the screenshots
  patches/v1_1/                the index for sites installed before after_install added it
  patches/v1_2/                drops the unused Auto-billing Delay setting
  automated_subscriptions/     the app's one module, Automated Subscriptions
    custom/                    custom fields and property setters (*.json) and the class extensions
      customer.py              Customer validate hook
      subscription.py          AutoSubSubscription: anchors, proration, consolidation, lookups, cancellation
      sales_invoice.py         AutoSubSalesInvoice, the Payment Request hook, the status refresh
      process_subscription.py  AutoSubProcessSubscription: the Customer filter
      sales_invoice_item.py    types of the custom fields only
      subscription_settings.py types of the custom fields only
    billing/
      anchor.py                period ends and proration factors (pure date arithmetic, no database)
      profile.py               a customer's billing profile: anchor, interval, consolidation
      runner.py                the consolidated billing runner and its dry run
      credit_note.py           credit notes on cancellation
      settings.py              Subscription Settings with defaults, and the defaults written on migrate
      dates.py                 a typed getdate
      exceptions.py            the engine's ValidationError subclasses
    tests/                     the tests and their helpers
marketplace/                   registry listing and the screenshot script
docs/                          this documentation
```

## Tests

The tests are Frappe `IntegrationTestCase`s in `automated_subscriptions/automated_subscriptions/tests/`. They build on ERPNext's own subscription test records (`_Test Company`, `_Test Customer`, the `_Test Plan Name` plans). `tests/utils.py` has the shared helpers: an anchored customer, a frozen `nowdate()`, and a context that keeps the test's transaction through code that answers an error with a full rollback.

| File                          | Covers                                                                                                                                                                                                            |
| ----------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `test_anchor.py`              | The date arithmetic of `billing/anchor.py`: occurrences, clamping, terms and the supported plan grids                                                                                                             |
| `test_proration.py`           | Anchored periods and prorated rates on real subscriptions, yearly billing with the discount, Pricing Rules, the refused plans, backdated starts, Mid-term Billing Mode and changing an existing customer's anchor |
| `test_consolidation.py`       | The runner: grouping, one invoice per group, money conservation with discounts, locks, failures and rollback, the dry run, and Process Subscription per customer                                                  |
| `test_lookups.py`             | Finding a subscription's invoices by header or line link, statuses with credit notes, and the status refresh on cancel                                                                                            |
| `test_payment_request.py`     | The Payment Request on submit: gateway resolution, skips, the kill switch, lateness and muted e-mail                                                                                                              |
| `test_credit_note.py`         | Cancellation with a credit note: amounts, periods, guards against double credit, and the fall-back to stock                                                                                                       |
| `test_sales_invoice_mixin.py` | The Sales Invoice extension and hooks: the whitelisted `set_missing_values`, the status refresh, gateway resolution                                                                                               |
| `test_fields.py`              | The custom fields, the Subscription Settings defaults and the Customer validation                                                                                                                                 |
| `test_install.py`             | The `after_install` hook                                                                                                                                                                                          |
| `test_patches.py`             | The `drop_auto_billing_delay` patch                                                                                                                                                                               |
| `test_runner_lock.py`         | The per-customer lock                                                                                                                                                                                             |
| `test_dates.py`               | `as_date`                                                                                                                                                                                                         |
| `test_demo.py`                | The demo setup                                                                                                                                                                                                    |

`frappe-test` also enforces the coverage floor, `fail_under` in `pyproject.toml` (92%).

## Demo and screenshots

`automated_subscriptions/demo.py` is what `frappe-demo` runs (`automated_subscriptions.demo.setup`). It creates one calendar-anchored, consolidating customer with four monthly subscriptions that start on the demo day, then runs the consolidated billing runner once, so the site's first Sales Invoice bills all four prorated first periods. No plan has a payment gateway, so nothing is charged.

`marketplace/screenshots.ts` photographs that site into `docs/screenshots/`, with `manifest.json` describing each picture:

```bash
frappe-shots --recreate-site --update
```

The screenshots are generated: do not edit them by hand.

## Continuous integration and releases

These workflows are managed by frappe-nix (`frappe-init --sync` writes them); the app standards documentation describes each:

- `ci.yml` runs the checks `ci / lint` (the pre-commit hooks, with prek), `ci / typecheck`, `ci / test` (the tests with coverage) and `ci / marketplace` on every pull request, every push to `develop` and once a day.
- `pr-policy.yml` adds `ci / pr-policy`, which checks a pull request's base, title and conventional commits.
- `release.yml` runs release-please on `develop` and moves the `version-16` release branch forward.
- `nightly.yml` and `deps.yml` run the nightly suites and look after dependency pull requests.

Commit messages follow Conventional Commits (`committed.toml`): `fix:` makes a patch release and `feat:` a minor one.

## Conventions

- Server code uses the Frappe ORM and Query Builder, never raw SQL.
- Python is indented with tabs and formatted by ruff (line length 110); methods are ordered by ssort.
- A class extension (`extend_doctype_class`) declares its fields as type annotations only, never as class attributes, which would shadow the document's values.
- Patches are listed in `patches.txt`. A fresh install marks every patch as done without running it, so anything a new site needs must also happen in `after_install` (`install.py`), as the Sales Invoice index does.
- The engine's errors subclass `frappe.ValidationError` (`billing/exceptions.py`), because ERPNext's `process_all` logs and rolls back only that class per subscription.

## Working on these docs

The documentation is Markdown in `docs/`, published at [docs.avunu.net/automated-subscriptions/docs/](https://docs.avunu.net/automated-subscriptions/docs/) by [Avunu/site-docs](https://github.com/Avunu/site-docs), which picks up the latest release of this repository. The `Docs` workflow (`.github/workflows/docs.yml`) checks every pull request that changes `docs/`.

To run the same check locally, clone Avunu/site-docs beside this repository and, from its folder, run:

```bash
npm ci --ignore-scripts && npm run build
CI=true node build/cli.js build --only automated-subscriptions --source automated-subscriptions=../automated_subscriptions
```

The writing rules are in [Writing documentation](https://github.com/Avunu/site-docs/blob/main/docs/guide/writing-docs.md).
