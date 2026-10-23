# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""The Payment Request raised on submit of a subscription invoice (PR 4, DECISIONS.md D-11): gateway resolved
from the line plans, header-sub plan validation switched off, the payment-terms due date as the collection
date, a kill switch, a staleness window that leaves a draft, and no request at all for returns."""

from unittest.mock import patch

import frappe
from erpnext.accounts.doctype.payment_request.payment_request import PaymentRequest
from erpnext.accounts.doctype.sales_invoice.sales_invoice import make_sales_return
from erpnext.accounts.doctype.subscription.test_subscription import (
	create_parties,
	create_subscription,
	make_plans,
	reset_settings,
)
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, nowdate
from payments.payment_gateways.doctype.gocardless_settings.gocardless_settings import GoCardlessSettings

from automated_subscriptions.automated_subscriptions.billing.dates import as_date
from automated_subscriptions.automated_subscriptions.tests.utils import (
	get_subscription,
	make_payment_terms_template,
)

CUSTOMER = "_Test Customer"
COMPANY = "_Test Company"
ITEM = "_Test Non Stock Item"
MONTH_PLAN = "_Test Plan Name"  # 900 / Month; carries the gateway in setUp
MONTH_PLAN_2 = "_Test Plan Name 2"  # 1999 / Month; no gateway
GATEWAY = "_Test AS Gateway"
GATEWAY_2 = "_Test AS Gateway 2"
TEMPLATE = "_Test AS NET15"
BEGINNING = "Beginning of the current subscription period"
VALIDATION = "payment_gateway_validation"


def make_gateway_account(gateway):
	"""A Payment Gateway without a settings doctype plus its INR account for _Test Company."""
	if not frappe.db.exists("Payment Gateway", gateway):
		frappe.get_doc({"doctype": "Payment Gateway", "gateway": gateway}).insert(ignore_permissions=True)
	name = frappe.db.get_value("Payment Gateway Account", {"payment_gateway": gateway, "company": COMPANY})
	if name:
		return name
	account = frappe.get_doc(
		{
			"doctype": "Payment Gateway Account",
			"payment_gateway": gateway,
			"payment_account": "_Test Bank - _TC",
			"company": COMPANY,
		}
	).insert(ignore_permissions=True)
	return account.name


def set_plan_gateway(plan, account):
	frappe.db.set_value("Subscription Plan", plan, "payment_gateway", account)


def payment_requests_for(invoice):
	return frappe.get_all(
		"Payment Request",
		{"reference_doctype": "Sales Invoice", "reference_name": invoice.name},
		["name", "docstatus"],
	)


def error_log_count(title):
	# tabError Log is MyISAM: rows survive the rollback, so count by title before and after
	return frappe.db.count("Error Log", {"method": title})


class TestPaymentRequest(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		make_plans()
		create_parties()
		reset_settings()
		frappe.db.set_value("Company", COMPANY, "accounts_frozen_till_date", None)
		frappe.db.set_value("Customer", CUSTOMER, "consolidate_subscription_invoices", 0)
		frappe.db.set_value("Customer", CUSTOMER, "payment_terms", make_payment_terms_template(TEMPLATE, 15))
		frappe.clear_document_cache("Customer", CUSTOMER)
		frappe.db.set_single_value("Selling Settings", "allow_multiple_items", 1)
		frappe.db.set_single_value("Subscription Settings", "auto_charge_max_lateness_days", 30)
		frappe.db.set_single_value("Subscription Settings", "auto_billing_delay", 0)
		frappe.flags.subscription_billing = None
		self.account = make_gateway_account(GATEWAY)
		set_plan_gateway(MONTH_PLAN, self.account)
		set_plan_gateway(MONTH_PLAN_2, None)
		self.today = nowdate()

	def tearDown(self):
		frappe.flags.in_import = False
		frappe.db.rollback()
		super().tearDown()

	# ---- fixtures
	def make_sub(self, plan, start_date=None, days_until_due=0):
		sub = create_subscription(
			party=CUSTOMER,
			start_date=start_date or self.today,
			plans=[{"plan": plan, "qty": 1}],
			generate_invoice_at=BEGINNING,
			submit_invoice=0,
			days_until_due=days_until_due,  # 0: the customer's NET15 template sets the due date
			do_not_save=True,
		)
		frappe.flags.in_import = True  # insert without core's catch-up billing
		try:
			sub.insert()
		finally:
			frappe.flags.in_import = False
		return get_subscription(sub.name)

	def make_invoice(self, plans=(MONTH_PLAN,), posting_date=None, submit=True, days_until_due=0, due_in=15):
		"""One subscription per plan; the first sub's own draft (posting_date = its period start) carries the
		other subs' lines, like a consolidated invoice. Submitting fires the on_submit hook."""
		posting_date = posting_date or self.today
		subs = [self.make_sub(plan, posting_date, days_until_due) for plan in plans]
		invoice = subs[0].create_invoice()
		for sub, plan in zip(subs[1:], plans[1:], strict=True):
			invoice.append(
				"items",
				{
					"item_code": ITEM,
					"qty": 1,
					"rate": frappe.db.get_value("Subscription Plan", plan, "cost"),
					"subscription": sub.name,
					"subscription_plan": plan,
					"subscription_period_start": sub.current_invoice_start,
					"subscription_period_end": sub.current_invoice_end,
				},
			)
		invoice.save()
		self.assertEqual(as_date(invoice.posting_date), as_date(posting_date))
		self.assertEqual(as_date(invoice.due_date), as_date(add_days(posting_date, due_in)))
		if submit:
			invoice.submit()
		return invoice

	def single_request(self, invoice):
		requests = payment_requests_for(invoice)
		self.assertEqual(len(requests), 1, requests)
		return frappe.get_doc("Payment Request", requests[0].name)

	# ---- (a)
	def test_gateway_from_line_plans(self):
		with patch.object(PaymentRequest, VALIDATION, return_value=False) as validation:
			invoice = self.make_invoice((MONTH_PLAN, MONTH_PLAN_2))
		# once by the hook (charge / mute decision), once by core's before_submit -> set_payment_request_url;
		# a real gateway caches the outcome on pr.flags so the second call never charges again
		self.assertEqual(validation.call_count, 2)
		self.assertEqual([d.subscription_plan for d in invoice.items], [MONTH_PLAN, MONTH_PLAN_2])
		pr = self.single_request(invoice)
		self.assertEqual(pr.payment_gateway_account, self.account)
		self.assertEqual(pr.payment_gateway, GATEWAY)
		self.assertEqual(pr.is_a_subscription, 0)
		self.assertEqual(pr.subscription_plans, [])
		self.assertEqual(as_date(pr.transaction_date), as_date(invoice.due_date))
		self.assertEqual(as_date(pr.transaction_date), as_date(add_days(invoice.posting_date, 15)))
		self.assertEqual(pr.mute_email, 1)
		self.assertEqual(pr.docstatus, 1)
		self.assertEqual(pr.status, "Requested")
		self.assertEqual(pr.grand_total, invoice.outstanding_amount)
		self.assertEqual(pr.party, CUSTOMER)

	# ---- (b)
	def test_no_gateway_no_pr(self):
		set_plan_gateway(MONTH_PLAN, None)
		with patch.object(PaymentRequest, VALIDATION, return_value=False) as validation:
			invoice = self.make_invoice((MONTH_PLAN, MONTH_PLAN_2))
		validation.assert_not_called()
		self.assertEqual(payment_requests_for(invoice), [])

	# ---- (c)
	def test_mixed_gateways_no_pr(self):
		set_plan_gateway(MONTH_PLAN_2, make_gateway_account(GATEWAY_2))
		before = error_log_count(("like", "Mixed payment gateways on subscription invoice %"))
		with patch.object(PaymentRequest, VALIDATION, return_value=False) as validation:
			invoice = self.make_invoice((MONTH_PLAN, MONTH_PLAN_2))
		validation.assert_not_called()
		self.assertEqual(payment_requests_for(invoice), [])
		self.assertEqual(invoice.docstatus, 1)  # a human raises the request; the submit is not aborted
		self.assertEqual(
			error_log_count(("like", "Mixed payment gateways on subscription invoice %")), before + 1
		)
		self.assertGreaterEqual(  # MyISAM: earlier runs' rows for the same (reused) invoice name survive
			error_log_count(f"Mixed payment gateways on subscription invoice {invoice.name}"), 1
		)

	# ---- (d)
	def test_stale_due_date_leaves_draft(self):
		posting = add_days(self.today, -60)  # due = posting + 15 = today - 45 > 30 days late
		title = ("like", "Stale subscription invoice not auto-charged: %")
		before = error_log_count(title)
		with patch.object(PaymentRequest, VALIDATION, return_value=False) as validation:
			invoice = self.make_invoice(posting_date=posting)
		validation.assert_not_called()
		self.assertEqual(as_date(invoice.due_date), as_date(add_days(self.today, -45)))
		pr = self.single_request(invoice)
		self.assertEqual(pr.docstatus, 0)
		self.assertEqual(pr.payment_gateway_account, self.account)
		self.assertEqual(as_date(pr.transaction_date), as_date(invoice.due_date))
		self.assertEqual(error_log_count(title), before + 1)
		self.assertGreaterEqual(
			error_log_count(f"Stale subscription invoice not auto-charged: {invoice.name}"), 1
		)
		comments = frappe.get_all(
			"Comment",
			{"reference_doctype": "Payment Request", "reference_name": pr.name, "comment_type": "Comment"},
			pluck="content",
		)
		self.assertEqual(len(comments), 1)
		self.assertIn("Left as draft", comments[0])

		# the window is a setting: widen it and the same invoice charges
		frappe.db.set_single_value("Subscription Settings", "auto_charge_max_lateness_days", 60)
		with patch.object(PaymentRequest, VALIDATION, return_value=False) as validation:
			invoice = self.make_invoice(posting_date=posting)
		validation.assert_called()
		self.assertEqual(self.single_request(invoice).docstatus, 1)

	# ---- (e)
	def test_kill_switch(self):
		with (
			patch.dict(frappe.local.conf, {"subscription_auto_charge_disabled": True}),
			patch.object(PaymentRequest, VALIDATION, return_value=False) as validation,
		):
			invoice = self.make_invoice()
		validation.assert_not_called()
		self.assertEqual(payment_requests_for(invoice), [])
		self.assertEqual(invoice.docstatus, 1)

	# ---- (f)
	def test_gateway_validation_outcome_drives_mute(self):
		with patch.object(PaymentRequest, VALIDATION, return_value=False):
			invoice = self.make_invoice()
		self.assertEqual(self.single_request(invoice).mute_email, 1)  # the gateway charged the mandate itself

		# True = the customer must visit the payment URL: the request is emailed. The test gateway has no
		# controller, so the URL and the mail (a Redis-backed enqueue) are stubbed at their entry points.
		with (
			patch.object(PaymentRequest, VALIDATION, return_value=True),
			patch.object(PaymentRequest, "get_payment_url", return_value="https://example.invalid/pay"),
			patch.object(PaymentRequest, "send_email") as send_email,
		):
			invoice = self.make_invoice()
		pr = self.single_request(invoice)
		self.assertEqual(pr.mute_email, 0)
		self.assertEqual(pr.docstatus, 1)
		self.assertEqual(pr.payment_url, "https://example.invalid/pay")
		send_email.assert_called_once()

	# ---- (g)
	def test_late_fire_still_charges(self):
		with patch.object(PaymentRequest, VALIDATION, return_value=False) as validation:
			invoice = self.make_invoice(posting_date=add_days(self.today, -3))
		validation.assert_called()
		self.assertEqual(as_date(invoice.due_date), as_date(add_days(self.today, 12)))
		pr = self.single_request(invoice)
		self.assertEqual(pr.docstatus, 1)
		self.assertEqual(as_date(pr.transaction_date), as_date(invoice.due_date))

	# ---- (h)
	def test_return_creates_no_pr(self):
		with patch.object(PaymentRequest, VALIDATION, return_value=False) as validation:
			invoice = self.make_invoice()
			self.assertEqual(self.single_request(invoice).docstatus, 1)
			calls_for_original = validation.call_count
			credit_note = make_sales_return(invoice.name)
			credit_note.set("payment_schedule", [])
			credit_note.insert()
			credit_note.submit()
		self.assertEqual(validation.call_count, calls_for_original)  # nothing for the return
		self.assertEqual(credit_note.docstatus, 1)
		self.assertEqual(credit_note.is_return, 1)
		self.assertEqual(credit_note.subscription, invoice.subscription)  # still a subscription invoice
		self.assertEqual(payment_requests_for(credit_note), [])

	# ---- (i)
	def test_hook_skipped_under_in_import(self):
		with patch.object(PaymentRequest, VALIDATION, return_value=False) as validation:
			invoice = self.make_invoice(submit=False)
			frappe.flags.in_import = True
			try:
				invoice.submit()
			finally:
				frappe.flags.in_import = False
		validation.assert_not_called()
		self.assertEqual(invoice.docstatus, 1)
		self.assertEqual(payment_requests_for(invoice), [])

	# ---- (j)
	def test_stock_gocardless_charges_on_the_due_date(self):
		"""Stock payments' GoCardless charges on max(transaction_date, the mandate's next_possible_charge_date), and
		the API returns that date as a string. A datetime.date transaction_date made max() raise TypeError, which
		payment_gateway_validation turns into False ("handled"): a mandate customer was neither charged nor emailed.
		The mandate lookup (an API call) and the charge itself are stubbed at their entry points."""
		controller = frappe.new_doc("GoCardless Settings")
		next_possible = add_days(self.today, 3)  # before the NET15 due date, so the due date wins
		with (
			patch(
				"erpnext.accounts.doctype.payment_request.payment_request._get_payment_gateway_controller",
				return_value=controller,
			),
			patch.object(
				GoCardlessSettings,
				"check_mandate_validity",
				return_value=({"mandate": "MD000AUTOSUBTEST"}, next_possible),
			),
			patch.object(GoCardlessSettings, "create_payment_request") as charge,
		):
			invoice = self.make_invoice()
		pr = self.single_request(invoice)
		self.assertEqual(pr.docstatus, 1)
		self.assertEqual(pr.mute_email, 1)  # the mandate was charged: nothing to email
		charge.assert_called()
		data = charge.call_args.args[0]
		self.assertEqual(data["mandate"], "MD000AUTOSUBTEST")
		self.assertEqual(data["charge_date"], str(as_date(invoice.due_date)))

	# ---- (k) Auto-billing Delay: the fallback for subscriptions without Days Until Due
	def set_delay(self, days):
		frappe.db.set_single_value("Subscription Settings", "auto_billing_delay", days)

	def drop_payment_terms(self):
		frappe.db.set_value("Customer", CUSTOMER, "payment_terms", None)
		frappe.clear_document_cache("Customer", CUSTOMER)

	def charged_on(self, invoice):
		pr = self.single_request(invoice)
		self.assertEqual(pr.docstatus, 1)
		return as_date(pr.transaction_date)

	def test_delay_applies_without_days_until_due(self):
		"""No payment terms and no Days Until Due: the invoice is due on its posting date and the charge is
		collected Auto-billing Delay days later (posting date + delay, as before the engine rewrite)."""
		self.drop_payment_terms()
		self.set_delay(5)
		with patch.object(PaymentRequest, VALIDATION, return_value=False):
			invoice = self.make_invoice(due_in=0)
		self.assertEqual(self.charged_on(invoice), as_date(add_days(invoice.posting_date, 5)))

	def test_delay_never_collects_before_the_due_date(self):
		"""With payment terms (NET15) and no Days Until Due, posting date + delay wins only when it is later
		than the due date: the delay can move a charge later, never earlier."""
		self.set_delay(20)
		with patch.object(PaymentRequest, VALIDATION, return_value=False):
			invoice = self.make_invoice()
		self.assertEqual(self.charged_on(invoice), as_date(add_days(invoice.posting_date, 20)))

		self.set_delay(5)
		with patch.object(PaymentRequest, VALIDATION, return_value=False):
			invoice = self.make_invoice()
		self.assertEqual(self.charged_on(invoice), as_date(invoice.due_date))
		self.assertEqual(self.charged_on(invoice), as_date(add_days(invoice.posting_date, 15)))

	def test_delay_ignored_with_days_until_due(self):
		"""A subscription with Days Until Due dates its invoice's due date itself; the delay does not apply,
		even when it is longer."""
		self.drop_payment_terms()
		self.set_delay(20)
		with patch.object(PaymentRequest, VALIDATION, return_value=False):
			invoice = self.make_invoice(days_until_due=10, due_in=10)
		self.assertEqual(self.charged_on(invoice), as_date(add_days(invoice.posting_date, 10)))

	def test_delay_ignored_when_any_line_sets_days_until_due(self):
		"""Consolidated invoices group subscriptions by Days Until Due, so they agree; if a hand-built invoice
		mixes them, one subscription with Days Until Due is enough to keep the due date."""
		self.drop_payment_terms()
		self.set_delay(20)
		set_plan_gateway(MONTH_PLAN_2, self.account)  # core checks the header subscription's plans' gateway
		posting = self.today
		with_due = self.make_sub(MONTH_PLAN, posting, days_until_due=10)
		without_due = self.make_sub(MONTH_PLAN_2, posting)
		invoice = without_due.create_invoice()
		invoice.append(
			"items",
			{
				"item_code": ITEM,
				"qty": 1,
				"rate": 900,
				"subscription": with_due.name,
				"subscription_plan": MONTH_PLAN,
				"subscription_period_start": with_due.current_invoice_start,
				"subscription_period_end": with_due.current_invoice_end,
			},
		)
		invoice.save()
		self.assertEqual(as_date(invoice.due_date), as_date(posting))
		with patch.object(PaymentRequest, VALIDATION, return_value=False):
			invoice.submit()
		self.assertEqual(self.charged_on(invoice), as_date(posting))

	def test_zero_or_blank_delay_is_no_delay(self):
		self.drop_payment_terms()
		self.set_delay(0)
		with patch.object(PaymentRequest, VALIDATION, return_value=False):
			invoice = self.make_invoice(due_in=0)
		self.assertEqual(self.charged_on(invoice), as_date(invoice.posting_date))

		# blank: no tabSingles row at all (a site that never saved the field)
		frappe.db.delete("Singles", {"doctype": "Subscription Settings", "field": "auto_billing_delay"})
		frappe.clear_document_cache("Subscription Settings", "Subscription Settings")
		with patch.object(PaymentRequest, VALIDATION, return_value=False):
			invoice = self.make_invoice(due_in=0)
		self.assertEqual(self.charged_on(invoice), as_date(invoice.posting_date))

		# a negative value (written past the form's non-negative check) is treated as no delay
		self.set_delay(-5)
		with patch.object(PaymentRequest, VALIDATION, return_value=False):
			invoice = self.make_invoice(due_in=0)
		self.assertEqual(self.charged_on(invoice), as_date(invoice.posting_date))

	def test_delay_does_not_revive_a_stale_invoice(self):
		"""Staleness is judged on the due date: a delay that puts the collection date inside the lateness
		window still leaves an invoice whose due date is too old as a draft."""
		self.set_delay(60)  # posting - 60 + 60 = today, but due = today - 45
		with patch.object(PaymentRequest, VALIDATION, return_value=False) as validation:
			invoice = self.make_invoice(posting_date=add_days(self.today, -60))
		validation.assert_not_called()
		pr = self.single_request(invoice)
		self.assertEqual(pr.docstatus, 0)
		self.assertEqual(as_date(pr.transaction_date), as_date(self.today))

	def test_gocardless_charge_date_carries_the_delay(self):
		"""End to end through stock payments' GoCardless: the delayed collection date is the mandate charge date."""
		self.drop_payment_terms()
		self.set_delay(7)
		controller = frappe.new_doc("GoCardless Settings")
		with (
			patch(
				"erpnext.accounts.doctype.payment_request.payment_request._get_payment_gateway_controller",
				return_value=controller,
			),
			patch.object(
				GoCardlessSettings,
				"check_mandate_validity",
				return_value=({"mandate": "MD000AUTOSUBTEST"}, add_days(self.today, 3)),
			),
			patch.object(GoCardlessSettings, "create_payment_request") as charge,
		):
			invoice = self.make_invoice(due_in=0)
		charge.assert_called()
		self.assertEqual(
			charge.call_args.args[0]["charge_date"], str(as_date(add_days(invoice.posting_date, 7)))
		)
