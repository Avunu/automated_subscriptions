import frappe
from erpnext.accounts.doctype.subscription.test_subscription import (
	create_parties,
	create_subscription,
	make_plans,
	reset_settings,
)
from frappe.tests import IntegrationTestCase

from automated_subscriptions.automated_subscriptions.billing.settings import (
	get_settings,
	materialise_defaults,
)
from automated_subscriptions.automated_subscriptions.tests.utils import make_anchored_customer

OUR_SETTINGS_FIELDS = (
	"annual_discount_percentage",
	"credit_note_on_cancellation",
	"mid_term_billing_mode",
	"auto_charge_max_lateness_days",
)


class TestFields(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		make_plans()
		create_parties()
		reset_settings()

	def tearDown(self):
		frappe.db.rollback()
		super().tearDown()

	def test_customer_fields_exist(self):
		meta = frappe.get_meta("Customer")

		consolidate = meta.get_field("consolidate_subscription_invoices")
		self.assertIsNotNone(consolidate)
		self.assertEqual(consolidate.fieldtype, "Check")
		self.assertEqual(consolidate.default, "0")

		mode = meta.get_field("subscription_billing_anchor_mode")
		self.assertIsNotNone(mode)
		self.assertEqual(mode.fieldtype, "Select")
		self.assertEqual(mode.options, "\nAnniversary\nCalendar")

		anchor_date = meta.get_field("subscription_billing_anchor_date")
		self.assertIsNotNone(anchor_date)
		self.assertEqual(anchor_date.fieldtype, "Date")

		interval = meta.get_field("subscription_billing_interval")
		self.assertIsNotNone(interval)
		self.assertEqual(interval.fieldtype, "Select")
		self.assertEqual(interval.options, "\nMonth\nYear")

		self.assertEqual(
			frappe.db.get_value("Customer", "_Test Customer", "consolidate_subscription_invoices"), 0
		)

	def test_sales_invoice_item_fields_and_index(self):
		meta = frappe.get_meta("Sales Invoice Item")

		subscription = meta.get_field("subscription")
		self.assertIsNotNone(subscription)
		self.assertEqual(subscription.fieldtype, "Link")
		self.assertEqual(subscription.options, "Subscription")
		self.assertEqual(subscription.search_index, 1)
		self.assertEqual(subscription.no_copy, 0)

		plan = meta.get_field("subscription_plan")
		self.assertIsNotNone(plan)
		self.assertEqual(plan.fieldtype, "Link")
		self.assertEqual(plan.options, "Subscription Plan")

		for fieldname in ("subscription_period_start", "subscription_period_end"):
			field = meta.get_field(fieldname)
			self.assertIsNotNone(field, fieldname)
			self.assertEqual(field.fieldtype, "Date")
			self.assertEqual(field.no_copy, 0)

		self.assertTrue(frappe.db.has_index("tabSales Invoice Item", "subscription_index"))
		self.assertTrue(
			frappe.db.sql("SHOW INDEX FROM `tabSales Invoice Item` WHERE Column_name='subscription'")
		)
		# header index from patches.v1_1.add_sales_invoice_subscription_index
		self.assertTrue(frappe.db.sql("SHOW INDEX FROM `tabSales Invoice` WHERE Column_name='subscription'"))
		self.assertEqual(frappe.get_meta("Sales Invoice").get_field("subscription").search_index, 1)
		# fresh installs never run patches (set_all_patches_as_completed): the same function is the after_install hook
		self.assertIn(
			"automated_subscriptions.patches.v1_1.add_sales_invoice_subscription_index.execute",
			frappe.get_hooks("after_install", app_name="automated_subscriptions"),
		)

	def test_subscription_field_and_search_fields(self):
		meta = frappe.get_meta("Subscription")
		field = meta.get_field("service_identifier")
		self.assertIsNotNone(field)
		self.assertEqual(field.fieldtype, "Data")
		self.assertEqual(field.in_list_view, 1)
		self.assertEqual(field.in_standard_filter, 1)
		self.assertEqual(meta.search_fields, "party, service_identifier")

	def test_settings_defaults_when_never_saved(self):
		frappe.db.delete(
			"Singles", {"doctype": "Subscription Settings", "field": ("in", list(OUR_SETTINGS_FIELDS))}
		)
		frappe.clear_document_cache("Subscription Settings", "Subscription Settings")

		settings = get_settings()
		self.assertEqual(settings.annual_discount_percentage, 25.0)
		self.assertEqual(settings.credit_note_on_cancellation, 1)
		self.assertEqual(settings.mid_term_billing_mode, "Immediate")
		self.assertEqual(settings.auto_charge_max_lateness_days, 30)
		# core fields still come through (reset_settings saved them)
		self.assertEqual(settings.grace_period, 0)
		self.assertEqual(settings.cancel_after_grace, 0)

		frappe.db.set_single_value("Subscription Settings", "annual_discount_percentage", 0)
		self.assertEqual(get_settings().annual_discount_percentage, 0.0)

	def test_settings_defaults_survive_save(self):
		# setUp ran reset_settings() - a save() that does not touch our fields - on a site whose tabSingles
		# already carries rows (materialise_defaults ran at migrate): the defaults must not collapse to 0
		settings = get_settings()
		self.assertEqual(settings.annual_discount_percentage, 25.0)
		self.assertEqual(settings.credit_note_on_cancellation, 1)
		self.assertEqual(settings.mid_term_billing_mode, "Immediate")
		self.assertEqual(settings.auto_charge_max_lateness_days, 30)
		rows = frappe.db.get_singles_dict("Subscription Settings")
		self.assertEqual(rows.get("annual_discount_percentage"), "25")
		self.assertEqual(rows.get("credit_note_on_cancellation"), "1")
		self.assertEqual(rows.get("mid_term_billing_mode"), "Immediate")
		self.assertEqual(rows.get("auto_charge_max_lateness_days"), "30")

	def test_settings_defaults_materialised(self):
		self.assertIn(
			"automated_subscriptions.automated_subscriptions.billing.settings.materialise_defaults",
			frappe.get_hooks("after_migrate", app_name="automated_subscriptions"),
		)
		self.assertIn(
			"automated_subscriptions.automated_subscriptions.billing.settings.materialise_defaults",
			frappe.get_hooks("after_sync", app_name="automated_subscriptions"),
		)

		frappe.db.delete(
			"Singles", {"doctype": "Subscription Settings", "field": ("in", list(OUR_SETTINGS_FIELDS))}
		)
		frappe.clear_document_cache("Subscription Settings", "Subscription Settings")
		before = frappe.db.get_singles_dict("Subscription Settings")
		self.assertNotIn("annual_discount_percentage", before)
		# an existing single loads the missing fields as None; a plain save() would persist 0 / 0.0
		self.assertEqual(
			frappe.get_single("Subscription Settings").get_valid_dict()["annual_discount_percentage"], 0.0
		)

		materialise_defaults()

		self.assertEqual(
			frappe.db.get_single_value("Subscription Settings", "annual_discount_percentage"), 25
		)
		self.assertEqual(
			frappe.db.get_single_value("Subscription Settings", "credit_note_on_cancellation"), 1
		)
		self.assertEqual(
			frappe.db.get_single_value("Subscription Settings", "mid_term_billing_mode"), "Immediate"
		)
		self.assertEqual(
			frappe.db.get_single_value("Subscription Settings", "auto_charge_max_lateness_days"), 30
		)
		valid = frappe.get_single("Subscription Settings").get_valid_dict()
		self.assertEqual(valid["annual_discount_percentage"], 25.0)
		self.assertEqual(valid["credit_note_on_cancellation"], 1)
		self.assertEqual(valid["mid_term_billing_mode"], "Immediate")
		self.assertEqual(valid["auto_charge_max_lateness_days"], 30)
		# `modified` is untouched and core rows are preserved
		after = frappe.db.get_singles_dict("Subscription Settings")
		self.assertEqual(after.get("modified"), before.get("modified"))
		self.assertEqual(after.get("grace_period"), before.get("grace_period"))

		# idempotent: a second run changes nothing
		materialise_defaults()
		self.assertEqual(frappe.db.get_singles_dict("Subscription Settings"), after)

		# a subsequent core save (reset_settings) keeps the values
		reset_settings()
		settings = get_settings()
		self.assertEqual(settings.annual_discount_percentage, 25.0)
		self.assertEqual(settings.credit_note_on_cancellation, 1)
		self.assertEqual(settings.mid_term_billing_mode, "Immediate")
		self.assertEqual(settings.auto_charge_max_lateness_days, 30)

	def test_materialise_defaults_never_overwrites_an_existing_row(self):
		frappe.db.delete(
			"Singles", {"doctype": "Subscription Settings", "field": ("in", list(OUR_SETTINGS_FIELDS))}
		)
		frappe.db.set_single_value(
			"Subscription Settings", "auto_charge_max_lateness_days", 7, update_modified=False
		)
		frappe.clear_document_cache("Subscription Settings", "Subscription Settings")

		materialise_defaults()
		materialise_defaults()

		settings = get_settings()
		self.assertEqual(settings.annual_discount_percentage, 25.0)
		self.assertEqual(settings.credit_note_on_cancellation, 1)
		self.assertEqual(settings.mid_term_billing_mode, "Immediate")
		self.assertEqual(settings.auto_charge_max_lateness_days, 7)

	def test_customer_validate_rules(self):
		name = "_Test AS Anniversary"
		self.assertFalse(frappe.db.exists("Customer", name))

		with self.assertRaises(frappe.ValidationError):
			make_anchored_customer(name, "Anniversary")
		self.assertFalse(frappe.db.exists("Customer", name))

		customer = make_anchored_customer(name, "Calendar", anchor_date="2026-03-15", interval="Year")
		self.assertEqual(customer.name, name)
		self.assertIsNone(customer.subscription_billing_anchor_date)
		self.assertIsNone(frappe.db.get_value("Customer", name, "subscription_billing_anchor_date"))
		self.assertEqual(customer.subscription_billing_interval, "Year")

		customer.subscription_billing_anchor_mode = ""
		customer.subscription_billing_interval = "Year"
		customer.save(ignore_permissions=True)
		self.assertEqual(customer.subscription_billing_interval, "")
		self.assertEqual(frappe.db.get_value("Customer", name, "subscription_billing_interval"), "")

		customer.subscription_billing_anchor_mode = "Anniversary"
		customer.subscription_billing_anchor_date = "2026-03-15"
		customer.save(ignore_permissions=True)
		self.assertEqual(
			str(frappe.db.get_value("Customer", name, "subscription_billing_anchor_date")), "2026-03-15"
		)

	def test_stock_customer_save_is_a_noop_for_billing_fields(self):
		# a customer that existed before the migrate has NULL in the new columns; a stock-path save must not
		# turn NULL into "" (a spurious Version 'changed' entry - Customer has track_changes)
		frappe.db.set_value("Customer", "_Test Customer", "subscription_billing_interval", None)
		self.assertIsNone(frappe.db.get_value("Customer", "_Test Customer", "subscription_billing_interval"))

		def billing_versions():
			return frappe.db.count(
				"Version",
				{
					"ref_doctype": "Customer",
					"docname": "_Test Customer",
					"data": ("like", "%subscription_billing_%"),
				},
			)

		before = billing_versions()
		customer = frappe.get_doc("Customer", "_Test Customer")
		customer.save(ignore_permissions=True)

		self.assertIsNone(frappe.db.get_value("Customer", "_Test Customer", "subscription_billing_interval"))
		self.assertIsNone(
			frappe.db.get_value("Customer", "_Test Customer", "subscription_billing_anchor_date")
		)
		self.assertEqual(billing_versions(), before)

	def test_extended_subscription_class(self):
		# in_import stops after_insert from billing; it also skips _set_defaults (document.py:1071),
		# so the mandatory Select must be passed explicitly.
		frappe.flags.in_import = True
		try:
			created = create_subscription(generate_invoice_at="End of the current subscription period")
		finally:
			frappe.flags.in_import = False

		sub = frappe.get_doc("Subscription", created.name)
		self.assertEqual(type(sub).__name__, "ExtendedSubscription")
		self.assertTrue(hasattr(sub, "_billing_profile"))
		self.assertIsNone(sub._billing_profile())
		self.assertIs(sub.is_consolidating(), False)
		self.assertIsNone(sub.service_identifier)

		cached = frappe.get_cached_doc("Subscription", created.name)
		self.assertEqual(type(cached).__name__, "ExtendedSubscription")
