---
title: Charge subscription invoices
description: Set a payment gateway on your subscription plans so that every submitted subscription invoice raises a Payment Request and charges the customer's mandate.
nav_title: Payment Requests
order: 3
updated: 2026-10-26
---

When a Sales Invoice that bills a Subscription is submitted, the app raises a Payment Request for its outstanding amount through the gateway set on the subscription's plans. With a gateway that takes the payment itself, such as GoCardless with an active mandate, that request charges the customer without any further step. This works for every customer, whether or not the billing engine is switched on for them.

## Set it up

1. Set up the gateway in Frappe Payments, with a **Payment Gateway Account** for it.
2. Open each **Subscription Plan** that should be charged automatically and set **Payment Gateway** to that account. A plan without a gateway is never charged.
3. Make sure the customer has an active mandate, or the equivalent authorisation, with the gateway. For GoCardless that is a **GoCardless Mandate** for the customer that is not disabled.

Nothing else is needed. The next subscription invoice that is submitted, by the daily job or by hand, gets its Payment Request.

## When a Payment Request is made

The `on_submit` hook of Sales Invoice makes one when all of these hold:

- The invoice bills a Subscription, through its header **Subscription** or through the **Subscription** of any line.
- It is not a return (a credit note), and its outstanding amount is greater than zero. Trial invoices with a zero total and invoices paid at submission are skipped.
- The site's kill switch is not set: see [Stop all automatic charging](configuration.md#stop-all-automatic-charging).
- The site is not migrating, running a patch, installing an app or importing data.
- Exactly one payment gateway follows from the plans on the invoice. Plans without a gateway ride along with a plan that has one. When no plan has a gateway, nothing is charged. When two plans name different gateways, the app writes an Error Log titled "Mixed payment gateways on subscription invoice `<INVOICE>`" and leaves the Payment Request to you.

The gateway is read from the **Subscription Plan** of each invoice line. For an older invoice whose lines carry no plan, it is read from the plans of the subscription in the invoice header.

## When the customer is charged

The Payment Request is dated on the invoice's due date, and the gateway uses that date as the charge date. For GoCardless the charge is made on that date or on the mandate's next possible charge date, whichever is later.

The due date comes from **Days Until Due** on the Subscription: the invoice is due that many days after its posting date. With **Days Until Due** at 0, the invoice's payment terms (the customer's **Payment Terms Template**, for example) decide.

A late run still charges. If the daily job runs a day late, or you process an earlier period by hand, the invoice is charged as long as it is not too late (below).

## E-mail or no e-mail

The gateway is called once per Payment Request, when the request is submitted. For a gateway that takes the payment itself, such as GoCardless with an active mandate, that call is the charge. The app holds the e-mail back until the gateway has answered, then:

- **The gateway returned a payment link.** This is a redirect gateway such as Stripe, or GoCardless for a customer without an active mandate. Nothing is charged, and the app e-mails the customer the payment link at the invoice's contact e-mail, with the same message and Communication that ERPNext sends.
- **The gateway took the payment.** It recorded the charge as an Integration Request against the Payment Request with the status Authorized or Completed. **Mute Email** is set, and the customer gets no e-mail, because nothing is left for them to do.
- **The gateway did neither.** ERPNext's check of the gateway gives the same answer when it fails with an error, for example when the gateway cannot be reached, as when the gateway took the payment. GoCardless also gives that answer when its payments API fails or refuses the payment, after it has logged an Integration Request, which it leaves Queued or sets to Failed or Cancelled. When no Integration Request is Authorized or Completed, the app treats the payment as not confirmed. The request stays submitted with the status Requested, not Paid, and **Mute Email** stays off. Nothing is e-mailed, the gateway is not called again, and the app adds the comment "Payment not confirmed: the payment gateway neither confirmed a payment nor returned a payment link. Check the payment in the gateway and the Error Log, then collect the payment manually." and writes an Error Log titled "Subscription payment not confirmed: `<PAYMENT_REQUEST>`".

A gateway account with the Phone payment channel, such as M-Pesa, is left to ERPNext: it sends the request to the customer's phone and no e-mail.

## Invoices that are too late

An invoice whose due date is more than **Auto-charge Max Lateness (days)** in the past (30 by default, in **Subscription Settings**) is not charged. Its Payment Request is saved as a draft with the comment "Left as draft: due date `<DUE_DATE>` is more than `<DAYS>` days in the past. Charge manually.", and an Error Log titled "Stale subscription invoice not auto-charged: `<INVOICE>`" is written.

To charge it, open the draft Payment Request, check it and submit it.

## Consolidated invoices

A consolidated invoice is submitted once, after all its lines are on it, so it raises one Payment Request for the whole amount. All its plans must point to the same gateway, or to none: see [Consolidate invoices](consolidated-invoices.md).

## After the payment

With the stock Frappe Payments app, GoCardless charges the mandate but does not settle the invoice: its webhooks handle mandate events only. Make the Payment Entry for a collected payment by hand, or use a Payments build that handles payment events. When a Payment Entry against the invoice is submitted, the status of every subscription on the invoice is refreshed.
