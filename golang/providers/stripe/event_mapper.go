// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package stripe

import (
	"context"
	"encoding/json"
	"regexp"
	"strings"
	"time"

	bursar "github.com/Zonastery/bursar/golang/v2"
	stripego "github.com/stripe/stripe-go/v84"
)

var stripeCurrencyPattern = regexp.MustCompile(`^[A-Za-z]{3}$`)

type stripeWebhookResources interface {
	retrieveCheckout(context.Context, string) (*stripego.CheckoutSession, error)
	retrieveSubscription(context.Context, string) (*stripego.Subscription, error)
}

type stripeClientWebhookResources struct{ client *stripego.Client }

func (r stripeClientWebhookResources) retrieveCheckout(ctx context.Context, id string) (*stripego.CheckoutSession, error) {
	params := &stripego.CheckoutSessionRetrieveParams{}
	params.AddExpand("line_items")
	return r.client.V1CheckoutSessions.Retrieve(ctx, id, params)
}

func (r stripeClientWebhookResources) retrieveSubscription(ctx context.Context, id string) (*stripego.Subscription, error) {
	return r.client.V1Subscriptions.Retrieve(ctx, id, &stripego.SubscriptionRetrieveParams{})
}

func (p *Provider) mapStripeEvent(ctx context.Context, event stripego.Event, raw []byte) (*bursar.BillingEvent, error) {
	if p == nil || p.client == nil {
		return nil, stripeUninitializedError()
	}
	return mapStripeEvent(ctx, event, raw, stripeClientWebhookResources{client: p.client})
}

func mapStripeEvent(ctx context.Context, event stripego.Event, raw []byte, resources stripeWebhookResources) (*bursar.BillingEvent, error) {
	if strings.TrimSpace(event.ID) == "" {
		return nil, stripeWebhookMappingError("Stripe webhook event ID is required")
	}
	if event.Created < 0 {
		return nil, stripeWebhookMappingError("Stripe event.created must be a non-negative Unix timestamp")
	}
	occurredAt := time.Unix(event.Created, 0).UTC()

	switch string(event.Type) {
	case "checkout.session.completed", "checkout.session.async_payment_succeeded", "checkout.session.async_payment_failed":
		var session stripego.CheckoutSession
		if err := stripeDecodeEventObject(event, &session); err != nil {
			return nil, err
		}
		if string(event.Type) == "checkout.session.completed" && session.PaymentStatus == stripego.CheckoutSessionPaymentStatusUnpaid {
			return nil, nil
		}
		if resources == nil {
			return nil, stripeWebhookMappingError("Stripe webhook resources are required for checkout events")
		}
		sessionID, err := stripeRequiredWebhookText(session.ID, "Stripe checkout session.id")
		if err != nil {
			return nil, err
		}
		expanded, err := resources.retrieveCheckout(ctx, sessionID)
		if err != nil {
			return nil, stripeWebhookResourceError("retrieve Stripe checkout session", err)
		}
		if expanded == nil {
			return nil, stripeWebhookMappingError("Stripe returned no checkout session")
		}
		metadata := session.Metadata
		accountID := strings.TrimSpace(session.ClientReferenceID)
		if accountID == "" {
			accountID = strings.TrimSpace(metadata["bursar_account_id"])
		}
		customer := stripeCheckoutCustomer(&session, accountID, metadata)
		failed := string(event.Type) == "checkout.session.async_payment_failed"
		if failed {
			payment, err := stripeCheckoutPayment(&session, expanded, "failed", accountID, metadata)
			if err != nil {
				return nil, err
			}
			var subscription *bursar.BillingSubscription
			if subscriptionID := stripeSubscriptionID(session.Subscription); subscriptionID != "" {
				providerSubscription, err := resources.retrieveSubscription(ctx, subscriptionID)
				if err != nil {
					return nil, stripeWebhookResourceError("retrieve Stripe checkout subscription", err)
				}
				subscription, err = stripeSubscriptionInfo(providerSubscription, accountID, nil)
				if err != nil {
					return nil, err
				}
			}
			return stripeFinalizeEvent(event, raw, bursar.BillingEventPaymentFailed, accountID, customer, subscription, nil, payment, nil, nil, metadata, occurredAt)
		}
		if session.Mode == stripego.CheckoutSessionModeSubscription {
			if subscriptionID := stripeSubscriptionID(session.Subscription); subscriptionID != "" {
				providerSubscription, err := resources.retrieveSubscription(ctx, subscriptionID)
				if err != nil {
					return nil, stripeWebhookResourceError("retrieve Stripe checkout subscription", err)
				}
				var refs *bursar.ProviderRef
				if planSlug := strings.TrimSpace(metadata["plan_slug"]); planSlug != "" {
					refs = &bursar.ProviderRef{LookupKey: planSlug}
				}
				subscription, err := stripeSubscriptionInfo(providerSubscription, accountID, refs)
				if err != nil {
					return nil, err
				}
				return stripeFinalizeEvent(event, raw, bursar.BillingEventCheckoutCompleted, accountID, customer, subscription, nil, nil, nil, nil, metadata, occurredAt)
			}
		}
		payment, err := stripeCheckoutPayment(&session, expanded, "succeeded", accountID, metadata)
		if err != nil {
			return nil, err
		}
		return stripeFinalizeEvent(event, raw, bursar.BillingEventPaymentSucceeded, accountID, customer, nil, nil, payment, nil, nil, metadata, occurredAt)

	case "checkout.session.expired":
		var session stripego.CheckoutSession
		if err := stripeDecodeEventObject(event, &session); err != nil {
			return nil, err
		}
		metadata := session.Metadata
		accountID := strings.TrimSpace(session.ClientReferenceID)
		if accountID == "" {
			accountID = strings.TrimSpace(metadata["bursar_account_id"])
		}
		return stripeFinalizeEvent(event, raw, bursar.BillingEventCheckoutExpired, accountID, stripeCheckoutCustomer(&session, accountID, metadata), nil, nil, nil, nil, nil, metadata, occurredAt)

	case "customer.subscription.created", "customer.subscription.paused", "customer.subscription.resumed", "customer.subscription.trial_will_end", "customer.subscription.updated", "customer.subscription.deleted":
		var subscription stripego.Subscription
		if err := stripeDecodeEventObject(event, &subscription); err != nil {
			return nil, err
		}
		accountID := strings.TrimSpace(subscription.Metadata["bursar_account_id"])
		info, err := stripeSubscriptionInfo(&subscription, accountID, nil)
		if err != nil {
			return nil, err
		}
		eventType := bursar.BillingEventSubscriptionUpdated
		switch string(event.Type) {
		case "customer.subscription.created":
			eventType = bursar.BillingEventSubscriptionCreated
		case "customer.subscription.paused":
			eventType = bursar.BillingEventSubscriptionPaused
		case "customer.subscription.resumed":
			eventType = bursar.BillingEventSubscriptionResumed
		case "customer.subscription.trial_will_end":
			eventType = bursar.BillingEventSubscriptionTrialWillEnd
		case "customer.subscription.updated":
			if subscription.Status == stripego.SubscriptionStatusCanceled {
				eventType = bursar.BillingEventSubscriptionCanceled
			} else if subscription.CancelAtPeriodEnd {
				eventType = bursar.BillingEventSubscriptionCancellationScheduled
			}
		case "customer.subscription.deleted":
			eventType = bursar.BillingEventSubscriptionCanceled
			info.Status = "canceled"
			if info.EndedAt == nil {
				ended := occurredAt
				info.EndedAt = &ended
			}
		}
		customer := stripeCustomerInfo(stripeCustomerID(subscription.Customer), "", accountID, subscription.Metadata)
		return stripeFinalizeEvent(event, raw, eventType, accountID, customer, info, nil, nil, nil, nil, subscription.Metadata, occurredAt)

	case "payment_intent.succeeded", "payment_intent.payment_failed", "payment_intent.canceled":
		var intent stripego.PaymentIntent
		if err := stripeDecodeEventObject(event, &intent); err != nil {
			return nil, err
		}
		if strings.TrimSpace(intent.Metadata["auto_recharge_attempt_id"]) == "" {
			return nil, nil
		}
		paymentID, err := stripeRequiredWebhookText(intent.ID, "Stripe payment intent.id")
		if err != nil {
			return nil, err
		}
		amount, err := stripeMinorUnits(intent.Amount, "Stripe payment intent.amount", false)
		if err != nil {
			return nil, err
		}
		currency, err := stripeCurrency(string(intent.Currency), "Stripe payment intent.currency")
		if err != nil {
			return nil, err
		}
		status := "failed"
		eventType := bursar.BillingEventPaymentFailed
		if string(event.Type) == "payment_intent.succeeded" {
			status, eventType = "succeeded", bursar.BillingEventPaymentSucceeded
		} else if string(event.Type) == "payment_intent.canceled" {
			status = "canceled"
		}
		accountID := strings.TrimSpace(intent.Metadata["bursar_account_id"])
		payment := &bursar.BillingPayment{
			ProviderPaymentID: paymentID,
			ID:                paymentID,
			Provider:          ProviderName,
			AccountID:         accountID,
			CustomerID:        stripeCustomerID(intent.Customer),
			Status:            status,
			Currency:          currency,
			AmountMinor:       amount,
			Purpose:           "credit_topup",
			Refs: &bursar.ProviderRef{
				ProductID: strings.TrimSpace(intent.Metadata["product_id"]),
				PriceID:   strings.TrimSpace(intent.Metadata["price_id"]),
			},
			Metadata: stripeMetadataAny(intent.Metadata),
		}
		return stripeFinalizeEvent(event, raw, eventType, accountID, nil, nil, nil, payment, nil, nil, intent.Metadata, occurredAt)

	case "charge.dispute.created", "charge.dispute.updated", "charge.dispute.closed":
		var dispute stripego.Dispute
		if err := stripeDecodeEventObject(event, &dispute); err != nil {
			return nil, err
		}
		disputeID, err := stripeRequiredWebhookText(dispute.ID, "Stripe dispute.id")
		if err != nil {
			return nil, err
		}
		paymentID := stripePaymentIntentID(dispute.PaymentIntent)
		if paymentID == "" {
			paymentID = stripeChargeID(dispute.Charge)
		}
		paymentID, err = stripeRequiredWebhookText(paymentID, "Stripe dispute payment identifier")
		if err != nil {
			return nil, err
		}
		status, err := stripeDisputeStatus(string(dispute.Status))
		if err != nil {
			return nil, err
		}
		accountID := strings.TrimSpace(dispute.Metadata["bursar_account_id"])
		info := &bursar.BillingDispute{
			ProviderDisputeID: disputeID,
			ProviderPaymentID: paymentID,
			ID:                disputeID,
			Provider:          ProviderName,
			PaymentID:         paymentID,
			Status:            status,
			Reason:            string(dispute.Reason),
			Metadata:          stripeMetadataAny(dispute.Metadata),
		}
		eventType := bursar.BillingEventDisputeCreated
		if string(event.Type) == "charge.dispute.closed" {
			eventType = bursar.BillingEventDisputeClosed
		}
		return stripeFinalizeEvent(event, raw, eventType, accountID, nil, nil, nil, nil, nil, info, dispute.Metadata, occurredAt)

	case "invoice.paid", "invoice.payment_failed":
		var invoice stripego.Invoice
		if err := stripeDecodeEventObject(event, &invoice); err != nil {
			return nil, err
		}
		subscriptionID := stripeInvoiceSubscriptionID(&invoice)
		if subscriptionID == "" {
			return nil, nil
		}
		if resources == nil {
			return nil, stripeWebhookMappingError("Stripe webhook resources are required for invoice events")
		}
		providerSubscription, err := resources.retrieveSubscription(ctx, subscriptionID)
		if err != nil {
			return nil, stripeWebhookResourceError("retrieve Stripe invoice subscription", err)
		}
		metadata := stripeInvoiceMetadata(&invoice)
		accountID := strings.TrimSpace(metadata["bursar_account_id"])
		if accountID == "" && providerSubscription != nil {
			accountID = strings.TrimSpace(providerSubscription.Metadata["bursar_account_id"])
		}
		subscription, err := stripeSubscriptionInfo(providerSubscription, accountID, nil)
		if err != nil {
			return nil, err
		}
		customer := stripeCustomerInfo(stripeCustomerID(invoice.Customer), "", accountID, metadata)
		if string(event.Type) == "invoice.paid" {
			invoiceID, err := stripeRequiredWebhookText(invoice.ID, "Stripe invoice.id")
			if err != nil {
				return nil, err
			}
			amountPaid, err := stripeMinorUnits(invoice.AmountPaid, "Stripe invoice.amount_paid", false)
			if err != nil {
				return nil, err
			}
			amountDue, err := stripeMinorUnits(invoice.AmountDue, "Stripe invoice.amount_due", false)
			if err != nil {
				return nil, err
			}
			currency, err := stripeCurrency(string(invoice.Currency), "Stripe invoice.currency")
			if err != nil {
				return nil, err
			}
			periodStart, periodEnd := stripeInvoicePeriods(&invoice, subscription)
			subscription.CurrentPeriodStart, subscription.PeriodStart = periodStart, periodStart
			subscription.CurrentPeriodEnd, subscription.PeriodEnd = periodEnd, periodEnd
			invoiceInfo := &bursar.BillingInvoice{
				ProviderInvoiceID: invoiceID,
				ID:                invoiceID,
				Provider:          ProviderName,
				AccountID:         accountID,
				SubscriptionID:    subscriptionID,
				CustomerID:        stripeCustomerID(invoice.Customer),
				Status:            "paid",
				Currency:          currency,
				AmountPaidMinor:   amountPaid,
				AmountDueMinor:    amountDue,
				PeriodStart:       periodStart,
				PeriodEnd:         periodEnd,
				HostedInvoiceURL:  strings.TrimSpace(invoice.HostedInvoiceURL),
				Metadata:          stripeMetadataAny(metadata),
			}
			return stripeFinalizeEvent(event, raw, bursar.BillingEventInvoicePaid, accountID, customer, subscription, invoiceInfo, nil, nil, nil, metadata, occurredAt)
		}
		paymentID := stripeInvoicePaymentID(&invoice)
		amount, err := stripeMinorUnits(invoice.Subtotal, "Stripe invoice.subtotal", false)
		if err != nil {
			return nil, err
		}
		tax, err := stripeInvoiceTaxMinor(&invoice)
		if err != nil {
			return nil, err
		}
		currency, err := stripeCurrency(string(invoice.Currency), "Stripe invoice.currency")
		if err != nil {
			return nil, err
		}
		payment := &bursar.BillingPayment{
			ProviderPaymentID: paymentID,
			ID:                paymentID,
			Provider:          ProviderName,
			AccountID:         accountID,
			CustomerID:        stripeCustomerID(invoice.Customer),
			SubscriptionID:    subscriptionID,
			InvoiceID:         strings.TrimSpace(invoice.ID),
			Status:            "failed",
			Currency:          currency,
			AmountMinor:       amount,
			TaxMinor:          tax,
			Purpose:           "subscription",
			Refs:              subscription.Refs,
			Metadata:          stripeMetadataAny(metadata),
		}
		return stripeFinalizeEvent(event, raw, bursar.BillingEventPaymentFailed, accountID, customer, subscription, nil, payment, nil, nil, metadata, occurredAt)

	case "refund.created", "refund.updated", "refund.failed":
		var refund stripego.Refund
		if err := stripeDecodeEventObject(event, &refund); err != nil {
			return nil, err
		}
		refundID, err := stripeRequiredWebhookText(refund.ID, "Stripe refund.id")
		if err != nil {
			return nil, err
		}
		paymentID, err := stripeRequiredWebhookText(stripePaymentIntentID(refund.PaymentIntent), "Stripe refund.payment_intent")
		if err != nil {
			return nil, err
		}
		amount, err := stripeMinorUnits(refund.Amount, "Stripe refund.amount", true)
		if err != nil {
			return nil, err
		}
		currency, err := stripeCurrency(string(refund.Currency), "Stripe refund.currency")
		if err != nil {
			return nil, err
		}
		status := string(refund.Status)
		switch status {
		case "succeeded", "failed", "canceled":
		default:
			status = "pending"
		}
		accountID := strings.TrimSpace(refund.Metadata["bursar_account_id"])
		info := &bursar.BillingRefund{
			ProviderRefundID:  refundID,
			ProviderPaymentID: paymentID,
			ID:                refundID,
			Provider:          ProviderName,
			PaymentID:         paymentID,
			Status:            status,
			Currency:          currency,
			AmountMinor:       amount,
			Reason:            string(refund.Reason),
			Metadata:          stripeMetadataAny(refund.Metadata),
		}
		eventType := bursar.BillingEventRefundCreated
		if string(event.Type) == "refund.updated" {
			eventType = bursar.BillingEventRefundUpdated
		} else if string(event.Type) == "refund.failed" {
			eventType = bursar.BillingEventRefundFailed
			info.Status = "failed"
		}
		return stripeFinalizeEvent(event, raw, eventType, accountID, nil, nil, nil, nil, info, nil, refund.Metadata, occurredAt)
	default:
		return nil, nil
	}
}

func stripeFinalizeEvent(event stripego.Event, raw []byte, eventType bursar.BillingEventType, accountID string, customer *bursar.BillingCustomer, subscription *bursar.BillingSubscription, invoice *bursar.BillingInvoice, payment *bursar.BillingPayment, refund *bursar.BillingRefund, dispute *bursar.BillingDispute, metadata map[string]string, occurredAt time.Time) (*bursar.BillingEvent, error) {
	result := &bursar.BillingEvent{
		EventID:        event.ID,
		ID:             event.ID,
		BillingEventID: event.ID,
		Provider:       ProviderName,
		Type:           eventType,
		OccurredAt:     occurredAt,
		AccountID:      accountID,
		Customer:       customer,
		Subscription:   subscription,
		Invoice:        invoice,
		Payment:        payment,
		Refund:         refund,
		Dispute:        dispute,
		Metadata:       stripeMetadataAny(metadata),
		RawPayload:     append(json.RawMessage(nil), raw...),
	}
	if err := result.Validate(); err != nil {
		return nil, stripeWebhookMappingCause("invalid normalized Stripe webhook", err)
	}
	return result, nil
}

func stripeDecodeEventObject(event stripego.Event, target any) error {
	if event.Data == nil || len(event.Data.Raw) == 0 {
		return stripeWebhookMappingError("Stripe webhook has no event object")
	}
	if err := json.Unmarshal(event.Data.Raw, target); err != nil {
		return stripeWebhookMappingCause("decode Stripe webhook event object", err)
	}
	return nil
}

func stripeCheckoutCustomer(session *stripego.CheckoutSession, accountID string, metadata map[string]string) *bursar.BillingCustomer {
	if session == nil {
		return nil
	}
	customerID := stripeCustomerID(session.Customer)
	email := ""
	if session.Customer != nil && !session.Customer.Deleted {
		email = strings.TrimSpace(session.Customer.Email)
	}
	if email == "" && session.CustomerDetails != nil {
		email = strings.TrimSpace(session.CustomerDetails.Email)
	}
	return stripeCustomerInfo(customerID, email, accountID, metadata)
}

func stripeCustomerInfo(customerID, email, accountID string, metadata map[string]string) *bursar.BillingCustomer {
	if customerID == "" && email == "" {
		return nil
	}
	return &bursar.BillingCustomer{
		ProviderCustomerID: customerID,
		ID:                 customerID,
		AccountID:          accountID,
		Email:              email,
		Metadata:           stripeMetadataAny(metadata),
	}
}

func stripeCheckoutPayment(session, expanded *stripego.CheckoutSession, status, accountID string, metadata map[string]string) (*bursar.BillingPayment, error) {
	if session == nil || expanded == nil {
		return nil, stripeWebhookMappingError("Stripe checkout session is required")
	}
	paymentID := stripePaymentIntentID(session.PaymentIntent)
	if paymentID == "" {
		paymentID = strings.TrimSpace(session.ID)
	}
	paymentID, err := stripeRequiredWebhookText(paymentID, "Stripe checkout session payment identifier")
	if err != nil {
		return nil, err
	}
	tax := int64(0)
	if session.TotalDetails != nil {
		tax = session.TotalDetails.AmountTax
	}
	if _, err := stripeMinorUnits(tax, "Stripe checkout session.total_details.amount_tax", false); err != nil {
		return nil, err
	}
	amount := session.AmountSubtotal
	if amount == 0 && session.AmountTotal > tax {
		amount = session.AmountTotal - tax
	}
	if _, err := stripeMinorUnits(amount, "Stripe checkout session.amount_subtotal", false); err != nil {
		return nil, err
	}
	currency, err := stripeCurrency(string(session.Currency), "Stripe checkout session.currency")
	if err != nil {
		return nil, err
	}
	purpose := "credit_topup"
	if session.Mode == stripego.CheckoutSessionModeSubscription {
		purpose = "subscription"
	}
	payment := &bursar.BillingPayment{
		ProviderPaymentID: paymentID,
		ID:                paymentID,
		Provider:          ProviderName,
		AccountID:         accountID,
		CustomerID:        stripeCustomerID(session.Customer),
		SubscriptionID:    stripeSubscriptionID(session.Subscription),
		Status:            status,
		Currency:          currency,
		AmountMinor:       amount,
		TaxMinor:          tax,
		Purpose:           purpose,
		Metadata:          stripeMetadataAny(metadata),
	}
	if expanded.LineItems != nil && len(expanded.LineItems.Data) > 0 && expanded.LineItems.Data[0] != nil && expanded.LineItems.Data[0].Price != nil {
		price := expanded.LineItems.Data[0].Price
		payment.Refs = &bursar.ProviderRef{PriceID: strings.TrimSpace(price.ID)}
		if price.Product != nil {
			payment.Refs.ProductID = strings.TrimSpace(price.Product.ID)
		}
	}
	return payment, nil
}

func stripeSubscriptionInfo(subscription *stripego.Subscription, accountID string, overrideRefs *bursar.ProviderRef) (*bursar.BillingSubscription, error) {
	if subscription == nil {
		return nil, stripeWebhookMappingError("Stripe subscription is required")
	}
	subscriptionID, err := stripeRequiredWebhookText(subscription.ID, "Stripe subscription.id")
	if err != nil {
		return nil, err
	}
	status, err := stripeRequiredWebhookText(string(subscription.Status), "Stripe subscription.status")
	if err != nil {
		return nil, err
	}
	if accountID == "" {
		accountID = strings.TrimSpace(subscription.Metadata["bursar_account_id"])
	}
	info := &bursar.BillingSubscription{
		ProviderSubscriptionID: subscriptionID,
		ID:                     subscriptionID,
		Provider:               ProviderName,
		AccountID:              accountID,
		CustomerID:             stripeCustomerID(subscription.Customer),
		Status:                 status,
		CancelAtPeriodEnd:      subscription.CancelAtPeriodEnd,
		TrialEnd:               stripeUnixPointer(subscription.TrialEnd),
		CancelAt:               stripeUnixPointer(subscription.CancelAt),
		CanceledAt:             stripeUnixPointer(subscription.CanceledAt),
		EndedAt:                stripeUnixPointer(subscription.EndedAt),
		Metadata:               stripeMetadataAny(subscription.Metadata),
	}
	if subscription.Items != nil && len(subscription.Items.Data) > 0 && subscription.Items.Data[0] != nil {
		item := subscription.Items.Data[0]
		info.CurrentPeriodStart = stripeUnixPointer(item.CurrentPeriodStart)
		info.CurrentPeriodEnd = stripeUnixPointer(item.CurrentPeriodEnd)
		info.PeriodStart = info.CurrentPeriodStart
		info.PeriodEnd = info.CurrentPeriodEnd
		if item.Price != nil {
			refs := &bursar.ProviderRef{PriceID: strings.TrimSpace(item.Price.ID)}
			if item.Price.Product != nil {
				refs.ProductID = strings.TrimSpace(item.Price.Product.ID)
			}
			info.Refs = refs
			if item.Price.Recurring != nil {
				info.Interval = string(item.Price.Recurring.Interval)
				if item.Price.Recurring.IntervalCount > 0 && item.Price.Recurring.IntervalCount <= int64(^uint(0)>>1) {
					info.IntervalCount = int(item.Price.Recurring.IntervalCount)
				}
			}
		}
	}
	if overrideRefs != nil {
		copy := *overrideRefs
		info.Refs = &copy
	}
	return info, nil
}

func stripeInvoiceSubscriptionID(invoice *stripego.Invoice) string {
	if invoice == nil || invoice.Parent == nil || invoice.Parent.SubscriptionDetails == nil {
		return ""
	}
	return stripeSubscriptionID(invoice.Parent.SubscriptionDetails.Subscription)
}

func stripeInvoiceMetadata(invoice *stripego.Invoice) map[string]string {
	metadata := map[string]string{}
	if invoice != nil && invoice.Parent != nil && invoice.Parent.SubscriptionDetails != nil {
		for key, value := range invoice.Parent.SubscriptionDetails.Metadata {
			metadata[key] = value
		}
	}
	if invoice != nil {
		for key, value := range invoice.Metadata {
			metadata[key] = value
		}
	}
	return metadata
}

func stripeInvoicePeriods(invoice *stripego.Invoice, subscription *bursar.BillingSubscription) (*time.Time, *time.Time) {
	start, end := subscription.PeriodStart, subscription.PeriodEnd
	if start == nil && invoice != nil {
		start = stripeUnixPointer(invoice.PeriodStart)
	}
	if end == nil && invoice != nil {
		end = stripeUnixPointer(invoice.PeriodEnd)
	}
	return start, end
}

func stripeInvoicePaymentID(invoice *stripego.Invoice) string {
	if invoice != nil && invoice.Payments != nil {
		for _, invoicePayment := range invoice.Payments.Data {
			if invoicePayment != nil && invoicePayment.Payment != nil {
				if id := stripePaymentIntentID(invoicePayment.Payment.PaymentIntent); id != "" {
					return id
				}
			}
		}
	}
	if invoice == nil {
		return ""
	}
	return strings.TrimSpace(invoice.ID)
}

func stripeInvoiceTaxMinor(invoice *stripego.Invoice) (int64, error) {
	var total int64
	if invoice == nil {
		return 0, stripeWebhookMappingError("Stripe invoice is required")
	}
	for _, tax := range invoice.TotalTaxes {
		if tax == nil || tax.Amount < 0 || tax.Amount > int64(^uint64(0)>>1)-total {
			return 0, stripeWebhookMappingError("Stripe invoice.total_taxes must contain non-negative safe integers")
		}
		total += tax.Amount
	}
	return total, nil
}

func stripeDisputeStatus(status string) (string, error) {
	switch status {
	case "needs_response", "warning_needs_response":
		return "needs_response", nil
	case "under_review", "warning_under_review":
		return "under_review", nil
	case "won", "prevented":
		return "won", nil
	case "lost":
		return "lost", nil
	case "warning_closed":
		return "closed", nil
	default:
		return "", stripeWebhookMappingError("unsupported Stripe dispute status: " + status)
	}
}

func stripeCustomerID(customer *stripego.Customer) string {
	if customer == nil {
		return ""
	}
	return strings.TrimSpace(customer.ID)
}

func stripeSubscriptionID(subscription *stripego.Subscription) string {
	if subscription == nil {
		return ""
	}
	return strings.TrimSpace(subscription.ID)
}

func stripePaymentIntentID(intent *stripego.PaymentIntent) string {
	if intent == nil {
		return ""
	}
	return strings.TrimSpace(intent.ID)
}

func stripeChargeID(charge *stripego.Charge) string {
	if charge == nil {
		return ""
	}
	return strings.TrimSpace(charge.ID)
}

func stripeUnixPointer(value int64) *time.Time {
	if value <= 0 {
		return nil
	}
	parsed := time.Unix(value, 0).UTC()
	return &parsed
}

func stripeMetadataAny(metadata map[string]string) map[string]any {
	if len(metadata) == 0 {
		return nil
	}
	result := make(map[string]any, len(metadata))
	for key, value := range metadata {
		result[key] = value
	}
	return result
}

func stripeRequiredWebhookText(value, field string) (string, error) {
	if strings.TrimSpace(value) == "" {
		return "", stripeWebhookMappingError(field + " must be a non-empty string")
	}
	return strings.TrimSpace(value), nil
}

func stripeMinorUnits(value int64, field string, positive bool) (int64, error) {
	minimum := int64(0)
	if positive {
		minimum = 1
	}
	if value < minimum {
		word := "non-negative"
		if positive {
			word = "positive"
		}
		return 0, stripeWebhookMappingError(field + " must be a " + word + " integer")
	}
	return value, nil
}

func stripeCurrency(value, field string) (string, error) {
	if !stripeCurrencyPattern.MatchString(value) {
		return "", stripeWebhookMappingError(field + " must be a three-letter currency code")
	}
	return strings.ToUpper(value), nil
}

func stripeWebhookMappingError(message string) *bursar.BursarError {
	return bursar.NewError(message, bursar.ErrorOptions{Code: bursar.ErrorCodeProviderResponseInvalid, Category: bursar.ErrorCategoryInvalidRequest})
}

func stripeWebhookMappingCause(message string, cause error) *bursar.BursarError {
	return bursar.NewError(message, bursar.ErrorOptions{Code: bursar.ErrorCodeProviderResponseInvalid, Category: bursar.ErrorCategoryInvalidRequest, Cause: cause})
}

func stripeWebhookResourceError(message string, cause error) *bursar.BursarError {
	return bursar.NewError(message, bursar.ErrorOptions{Code: bursar.ErrorCodeProviderResponseInvalid, Category: bursar.ErrorCategoryUnavailable, Retryable: true, Cause: cause})
}
