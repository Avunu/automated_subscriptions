// Copyright (c) 2026, Avunu LLC and contributors
// For license information, please see license.txt

import type { ShotSpec } from "./shots.d.ts";

// The demo from automated_subscriptions/demo.py: one calendar-anchored, consolidating customer whose
// four subscriptions' prorated first periods are billed on the site's first Sales Invoice.
const INVOICE = "ACC-SINV-2026-00001";
const CUSTOMER = "Riverbend Dental Group";

const formReady = (doctype: string, name: string) =>
	`window.cur_frm && cur_frm.doctype === ${JSON.stringify(doctype)} && cur_frm.doc.name === ${JSON.stringify(name)} && !!document.querySelector(".form-layout")`;

// Bring a section into view the way the desk does: switch to the tab of a field in it, then scroll the
// form's own scroller (.main-section) so the section heading sits just below the page head.
// scroll_to_field would also highlight the field for two seconds, which the shot would catch.
const showSection = (section: string, field: string) => [
	{
		eval: `(() => {
			const tab = cur_frm.get_field(${JSON.stringify(field)}).tab;
			if (tab && !tab.is_active()) tab.set_active();
			frappe.utils.scroll_to($('.form-section[data-fieldname=${JSON.stringify(section)}]'), false, 15, $(".main-section"));
		})()`,
	},
	{ wait: 800 },
];

export default {
	demo: { date: "2026-01-15", seed: 1 },
	shots: [
		{
			name: "consolidated-invoice",
			route: `/app/sales-invoice/${INVOICE}`,
			alt: "One Sales Invoice billing four subscriptions of Riverbend Dental Group, each prorated for January 15 to 31",
			waitForFn: formReady("Sales Invoice", INVOICE),
			actions: [{ wait: 500 }],
			mask: [".frappe-timestamp"],
			readme: "hero",
			featured: true,
		},
		{
			name: "invoice-line",
			route: `/app/sales-invoice/${INVOICE}`,
			alt: "An invoice line opened: the subscription, the plan and the exact period the line bills",
			waitForFn: formReady("Sales Invoice", INVOICE),
			actions: [
				{ eval: "cur_frm.fields_dict.items.grid.grid_rows[0].show_form()" },
				{ waitFor: ".grid-row-open" },
				{ wait: 800 },
				{
					eval: `document.querySelector('.grid-row-open [data-fieldname="subscription_billing_section"]').scrollIntoView({ block: "center" })`,
				},
				{ wait: 800 },
			],
			mask: [".frappe-timestamp"],
			readme: "feature",
			featured: true,
		},
		{
			name: "customer-billing",
			route: `/app/customer/${encodeURIComponent(CUSTOMER)}`,
			alt: "The Subscription Billing section of a Customer: consolidated invoices on, billing anchored to the calendar",
			waitForFn: formReady("Customer", CUSTOMER),
			actions: showSection("subscription_billing_section", "consolidate_subscription_invoices"),
			mask: [".frappe-timestamp"],
			readme: "feature",
			featured: true,
		},
		{
			name: "subscription-settings",
			route: "/app/subscription-settings",
			alt: "The Automated Subscriptions section of Subscription Settings with its defaults",
			waitForFn: formReady("Subscription Settings", "Subscription Settings"),
			actions: showSection("sb_automated_subscriptions", "auto_billing_delay"),
			mask: [".frappe-timestamp"],
			readme: "feature",
		},
		{
			name: "subscriptions",
			route: "/app/subscription",
			alt: "The Subscription list with the Service Identifier column",
			waitFor: ".list-row-container",
			mask: [".frappe-timestamp"],
			readme: "feature",
		},
	],
} satisfies ShotSpec;
