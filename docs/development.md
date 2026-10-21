# Development

## Tooling

The repository is a frappe-nix app. `nix develop` (or `direnv allow`) gives a shell with a bench that has Frappe,
ERPNext and Frappe Payments, and the same checks CI runs:

```sh
nix develop        # or: direnv allow
prek install       # the git hooks: ruff (lint, import order, format), ssort, oxfmt, oxlint and the rest
prek run --all-files
frappe-test        # the test suite with coverage, the way CI runs it
```

Python is indented with tabs and formatted by ruff; methods are ordered by ssort.

On a bench of your own, the suite runs against a site with ERPNext and Frappe Payments installed:

```sh
bench --site your-test-site run-tests --app automated_subscriptions
```

The tests build on ERPNext's own subscription test fixtures (`_Test Company`, `_Test Customer`, the
`_Test Plan Name` plans). `tests/utils.py` has the shared helpers: an anchored customer, a frozen `nowdate()`, and a
context that keeps the test's transaction through code that answers an error with a full rollback.

## Source layout

| Path                                              | What                                                                                       |
| ------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| `automated_subscriptions/hooks.py`                | Doc events on Customer and Sales Invoice, the `extend_doctype_class` mixins, install hooks  |
| `automated_subscriptions/install.py`              | `after_install`: the index on `Sales Invoice.subscription`                                 |
| `automated_subscriptions/automated_subscriptions/custom/` | The custom fields (`*.json`) and the Subscription, Sales Invoice, Customer and Process Subscription extensions |
| `automated_subscriptions/automated_subscriptions/billing/anchor.py`   | Period ends and proration factors for calendar and anniversary anchors |
| `automated_subscriptions/automated_subscriptions/billing/profile.py`  | A customer's billing profile (anchor, interval, consolidation)         |
| `automated_subscriptions/automated_subscriptions/billing/runner.py`   | The consolidated billing runner and its dry run                         |
| `automated_subscriptions/automated_subscriptions/billing/credit_note.py` | Credit notes on cancellation                                         |
| `automated_subscriptions/automated_subscriptions/billing/settings.py` | Subscription Settings with defaults, and the defaults written on migrate |
| `automated_subscriptions/demo.py`                 | Demo data for `frappe-demo` and the screenshots                                            |

## Demo and screenshots

`automated_subscriptions/demo.py` creates one calendar-anchored, consolidating customer with four monthly
subscriptions that start on the demo day, then runs the consolidated billing runner once, so the site's first Sales
Invoice bills all four prorated first periods. `marketplace/screenshots.ts` photographs that site:

```sh
frappe-shots --recreate-site --update
```

## Design decisions

The deviations from the original implementation plan, and the verified ERPNext and Frappe behaviours that forced
them, are recorded in [DECISIONS.md](DECISIONS.md).
