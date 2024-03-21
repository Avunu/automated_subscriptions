app_name = "automated_subscriptions"
app_title = "Automated Subscriptions"
app_publisher = "Avunu LLC"
app_description = "Auto-billing with ERPNext Subscriptions using Frappe Payments"
app_email = "mail@avu.nu"
app_license = "mit"
required_apps = ["erpnext", "payments"]

doc_events = {
	"Sales Invoice": {
		"on_submit": "automated_subscriptions.automated_subscriptions.sales_invoice_subscription_payment_request"
	}
}