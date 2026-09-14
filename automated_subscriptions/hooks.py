app_name = "automated_subscriptions"
app_title = "Automated Subscriptions"
app_publisher = "Avunu LLC"
app_description = "Auto-billing with ERPNext Subscriptions using Frappe Payments"
app_email = "mail@avu.nu"
app_license = "mit"
required_apps = ["erpnext", "payments"]

# Fresh installs mark all patches as completed without running them (frappe/installer.py
# set_all_patches_as_completed); existing sites get this index from the post_model_sync patch.
after_install = ["automated_subscriptions.patches.v1_1.add_sales_invoice_subscription_index.execute"]
# Both run after sync_customizations (install: after_sync; migrate: after_migrate), unlike after_install and
# post_model_sync patches, so the Subscription Settings custom fields exist when the defaults are written.
after_sync = ["automated_subscriptions.automated_subscriptions.billing.settings.materialise_defaults"]
after_migrate = ["automated_subscriptions.automated_subscriptions.billing.settings.materialise_defaults"]

doc_events = {
	"Customer": {
		"validate": "automated_subscriptions.automated_subscriptions.custom.customer.validate",
	},
	"Sales Invoice": {
		"before_validate": "automated_subscriptions.automated_subscriptions.custom.sales_invoice.apply_subscription_billing_policy",
		"on_submit": "automated_subscriptions.automated_subscriptions.custom.sales_invoice.subscription_payment_request",
	},
}
extend_doctype_class = {
	"Sales Invoice": "automated_subscriptions.automated_subscriptions.custom.sales_invoice.SalesInvoice",
	"Subscription": "automated_subscriptions.automated_subscriptions.custom.subscription.Subscription",
	"Subscription Settings": "automated_subscriptions.automated_subscriptions.custom.subscription_settings.SubscriptionSettings",
}
