// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

// Package normalize contains shared, deliberately conservative translation
// from verified provider webhook documents into Bursar's provider-neutral
// event vocabulary. It is internal so provider details never become part of
// the public SDK contract.
package normalize

import (
	"encoding/json"
	"strconv"
	"strings"
	"time"

	bursar "github.com/Zonastery/bursar/golang/v2"
)

// Event builds a normalized event from a provider-authenticated payload. The
// caller is responsible for signature verification before invoking this
// function. Unknown event types are retained verbatim so applications can use
// a BillingService default handler without losing provider lifecycle changes.
func Event(provider, id, providerType string, occurredAt time.Time, rawPayload []byte, object map[string]any) *bursar.BillingEvent {
	metadata := metadataValue(object["metadata"])
	accountID := firstText(
		metadata["bursar_account_id"],
		metadata["account_id"],
		object["bursar_account_id"],
		object["account_id"],
		object["client_reference_id"],
	)
	metadata = cloneMetadata(metadata)
	if accountID != "" {
		metadata["bursar_account_id"] = accountID
	}

	event := &bursar.BillingEvent{
		ID:         id,
		Provider:   provider,
		Type:       canonicalType(providerType),
		OccurredAt: occurredAt.UTC(),
		Metadata:   metadata,
		RawPayload: append(json.RawMessage(nil), rawPayload...),
	}

	switch {
	case strings.HasPrefix(providerType, "customer."):
		event.Customer = &bursar.BillingCustomer{
			ID:        firstText(object["id"], object["customer"]),
			AccountID: accountID,
			Email:     firstText(object["email"], nested(object, "customer_details", "email")),
			Metadata:  metadata,
		}
	case strings.Contains(providerType, "subscription"):
		event.Subscription = subscription(provider, object, accountID, metadata)
	case strings.HasPrefix(providerType, "invoice."):
		event.Invoice = invoice(provider, object, accountID, metadata)
	case strings.Contains(providerType, "refund"):
		event.Refund = refund(provider, object, metadata)
	case strings.Contains(providerType, "dispute"):
		event.Dispute = dispute(provider, object, metadata)
	case strings.Contains(providerType, "payment") || strings.HasPrefix(providerType, "charge."):
		event.Payment = payment(provider, object, accountID, metadata)
	case strings.HasPrefix(providerType, "checkout."):
		// A Checkout Session carries an account reference in its metadata. It
		// is represented as a customer-like projection because the billing
		// event lifecycle resolves the canonical account before provisioning.
		event.Customer = &bursar.BillingCustomer{
			ID:        firstText(object["customer"], nested(object, "customer", "id")),
			AccountID: accountID,
			Email:     firstText(object["customer_email"], nested(object, "customer_details", "email")),
			Metadata:  metadata,
		}
	}
	return event
}

func canonicalType(providerType string) bursar.BillingEventType {
	providerType = strings.TrimSpace(providerType)
	aliases := map[string]bursar.BillingEventType{
		"checkout.session.completed":           bursar.BillingEventCheckoutCompleted,
		"checkout.completed":                   bursar.BillingEventCheckoutCompleted,
		"checkout.session.expired":             bursar.BillingEventCheckoutExpired,
		"checkout.expired":                     bursar.BillingEventCheckoutExpired,
		"customer.created":                     bursar.BillingEventCustomerCreated,
		"customer.updated":                     bursar.BillingEventCustomerUpdated,
		"customer.deleted":                     bursar.BillingEventCustomerDeleted,
		"customer.subscription.created":        bursar.BillingEventSubscriptionCreated,
		"customer.subscription.updated":        bursar.BillingEventSubscriptionUpdated,
		"customer.subscription.deleted":        bursar.BillingEventSubscriptionCanceled,
		"customer.subscription.paused":         bursar.BillingEventSubscriptionPaused,
		"customer.subscription.resumed":        bursar.BillingEventSubscriptionResumed,
		"customer.subscription.trial_will_end": bursar.BillingEventSubscriptionTrialWillEnd,
		"subscription.created":                 bursar.BillingEventSubscriptionCreated,
		"subscription.updated":                 bursar.BillingEventSubscriptionUpdated,
		"subscription.active":                  bursar.BillingEventSubscriptionActivated,
		"subscription.renewed":                 bursar.BillingEventSubscriptionRenewed,
		"subscription.plan_changed":            bursar.BillingEventSubscriptionPlanChanged,
		"subscription.cancelled":               bursar.BillingEventSubscriptionCanceled,
		"subscription.canceled":                bursar.BillingEventSubscriptionCanceled,
		"subscription.expired":                 bursar.BillingEventSubscriptionExpired,
		"subscription.on_hold":                 bursar.BillingEventSubscriptionPaused,
		"invoice.created":                      bursar.BillingEventInvoiceCreated,
		"invoice.updated":                      bursar.BillingEventInvoiceUpdated,
		"invoice.paid":                         bursar.BillingEventInvoicePaid,
		"invoice.payment_succeeded":            bursar.BillingEventInvoicePaid,
		"invoice.payment_failed":               bursar.BillingEventInvoicePaymentFailed,
		"invoice.upcoming":                     bursar.BillingEventInvoiceUpcoming,
		"payment.succeeded":                    bursar.BillingEventPaymentSucceeded,
		"payment.failed":                       bursar.BillingEventPaymentFailed,
		"payment_method.attached":              bursar.BillingEventPaymentMethodAttached,
		"payment_method.detached":              bursar.BillingEventPaymentMethodDetached,
		"payment_intent.succeeded":             bursar.BillingEventPaymentSucceeded,
		"payment_intent.payment_failed":        bursar.BillingEventPaymentFailed,
		"charge.succeeded":                     bursar.BillingEventPaymentSucceeded,
		"charge.failed":                        bursar.BillingEventPaymentFailed,
		"refund.created":                       bursar.BillingEventRefundCreated,
		"refund.updated":                       bursar.BillingEventRefundUpdated,
		"refund.succeeded":                     bursar.BillingEventRefundCreated,
		"charge.refunded":                      bursar.BillingEventRefundCreated,
		"charge.refund.updated":                bursar.BillingEventRefundUpdated,
		"charge.dispute.created":               bursar.BillingEventDisputeCreated,
		"charge.dispute.updated":               bursar.BillingEventDisputeUpdated,
		"dispute.opened":                       bursar.BillingEventDisputeCreated,
		"dispute.updated":                      bursar.BillingEventDisputeUpdated,
	}
	if eventType, ok := aliases[providerType]; ok {
		return eventType
	}
	return bursar.BillingEventType(providerType)
}

func subscription(provider string, object map[string]any, accountID string, metadata map[string]any) *bursar.BillingSubscription {
	return &bursar.BillingSubscription{
		ID:                 firstText(object["id"], object["subscription_id"]),
		Provider:           provider,
		AccountID:          accountID,
		CustomerID:         firstText(object["customer"], object["customer_id"]),
		Plan:               firstText(nested(object, "items", "data", "0", "price", "id"), object["product_id"], object["plan_id"]),
		Status:             firstText(object["status"]),
		Interval:           firstText(nested(object, "items", "data", "0", "price", "recurring", "interval"), object["billing_interval"]),
		IntervalCount:      intValue(nested(object, "items", "data", "0", "price", "recurring", "interval_count")),
		CurrentPeriodStart: unixTimePointer(object["current_period_start"]),
		CurrentPeriodEnd:   unixTimePointer(object["current_period_end"]),
		CancelAtPeriodEnd:  boolValue(object["cancel_at_period_end"]),
		CanceledAt:         unixTimePointer(object["canceled_at"]),
		Metadata:           metadata,
	}
}

func invoice(provider string, object map[string]any, accountID string, metadata map[string]any) *bursar.BillingInvoice {
	return &bursar.BillingInvoice{
		ID:               firstText(object["id"], object["invoice_id"]),
		Provider:         provider,
		AccountID:        accountID,
		SubscriptionID:   firstText(object["subscription"], object["subscription_id"]),
		CustomerID:       firstText(object["customer"], object["customer_id"]),
		Status:           firstText(object["status"]),
		Currency:         strings.ToUpper(firstText(object["currency"])),
		AmountPaidMinor:  int64Value(object["amount_paid"]),
		AmountDueMinor:   int64Value(object["amount_due"]),
		PeriodStart:      unixTimePointer(object["period_start"]),
		PeriodEnd:        unixTimePointer(object["period_end"]),
		HostedInvoiceURL: firstText(object["hosted_invoice_url"], object["invoice_url"]),
		Metadata:         metadata,
	}
}

func payment(provider string, object map[string]any, accountID string, metadata map[string]any) *bursar.BillingPayment {
	return &bursar.BillingPayment{
		ID:             firstText(object["id"], object["payment_id"]),
		Provider:       provider,
		AccountID:      accountID,
		CustomerID:     firstText(object["customer"], object["customer_id"]),
		SubscriptionID: firstText(object["subscription"], object["subscription_id"]),
		InvoiceID:      firstText(object["invoice"], object["invoice_id"]),
		Status:         firstText(object["status"]),
		Currency:       strings.ToUpper(firstText(object["currency"])),
		AmountMinor:    firstInt64(object["amount"], object["amount_paid"], object["total_amount"]),
		Metadata:       metadata,
	}
}

func refund(provider string, object map[string]any, metadata map[string]any) *bursar.BillingRefund {
	return &bursar.BillingRefund{
		ID:          firstText(object["id"], object["refund_id"]),
		Provider:    provider,
		PaymentID:   firstText(object["payment_intent"], object["charge"], object["payment_id"]),
		Status:      firstText(object["status"]),
		Currency:    strings.ToUpper(firstText(object["currency"])),
		AmountMinor: firstInt64(object["amount"], object["amount_refunded"]),
		Metadata:    metadata,
	}
}

func dispute(provider string, object map[string]any, metadata map[string]any) *bursar.BillingDispute {
	return &bursar.BillingDispute{
		ID:        firstText(object["id"], object["dispute_id"]),
		Provider:  provider,
		PaymentID: firstText(object["payment_intent"], object["charge"], object["payment_id"]),
		Status:    firstText(object["status"]),
		Reason:    firstText(object["reason"]),
		Metadata:  metadata,
	}
}

func nested(object map[string]any, keys ...string) any {
	var value any = object
	for _, key := range keys {
		switch current := value.(type) {
		case map[string]any:
			value = current[key]
		case []any:
			index, err := strconv.Atoi(key)
			if err != nil || index < 0 || index >= len(current) {
				return nil
			}
			value = current[index]
		default:
			return nil
		}
	}
	return value
}

func metadataValue(value any) map[string]any {
	metadata, ok := value.(map[string]any)
	if !ok || metadata == nil {
		return map[string]any{}
	}
	return metadata
}

func cloneMetadata(value map[string]any) map[string]any {
	clone := make(map[string]any, len(value))
	for key, item := range value {
		clone[key] = item
	}
	return clone
}

func firstText(values ...any) string {
	for _, value := range values {
		switch item := value.(type) {
		case string:
			if trimmed := strings.TrimSpace(item); trimmed != "" {
				return trimmed
			}
		case json.Number:
			if text := strings.TrimSpace(item.String()); text != "" {
				return text
			}
		}
	}
	return ""
}

func boolValue(value any) bool {
	parsed, ok := value.(bool)
	return ok && parsed
}

func intValue(value any) int {
	parsed := int64Value(value)
	if parsed > int64(^uint(0)>>1) || parsed < -int64(^uint(0)>>1)-1 {
		return 0
	}
	return int(parsed)
}

func firstInt64(values ...any) int64 {
	for _, value := range values {
		if parsed, ok := parseInt64(value); ok {
			return parsed
		}
	}
	return 0
}

func int64Value(value any) int64 {
	parsed, _ := parseInt64(value)
	return parsed
}

func parseInt64(value any) (int64, bool) {
	switch item := value.(type) {
	case int64:
		return item, true
	case int:
		return int64(item), true
	case float64:
		if float64(int64(item)) == item {
			return int64(item), true
		}
	case json.Number:
		parsed, err := item.Int64()
		return parsed, err == nil
	case string:
		parsed, err := strconv.ParseInt(item, 10, 64)
		return parsed, err == nil
	}
	return 0, false
}

func unixTimePointer(value any) *time.Time {
	seconds, ok := parseInt64(value)
	if !ok || seconds <= 0 {
		return nil
	}
	parsed := time.Unix(seconds, 0).UTC()
	return &parsed
}
