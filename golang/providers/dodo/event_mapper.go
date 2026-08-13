// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package dodo

import (
	"encoding/json"
	"fmt"
	"regexp"
	"strconv"
	"strings"
	"time"

	bursar "github.com/Zonastery/bursar/golang/v2"
)

var dodoCurrencyPattern = regexp.MustCompile(`^[A-Za-z]{3}$`)

var dodoBursarMetadataKeys = map[string]struct{}{
	"bursar_account_id":  {},
	"plan_slug":          {},
	"billing_interval":   {},
	"credits":            {},
	"checkout_intent_id": {},
}

func mapDodoEvent(providerType string, occurredAt time.Time, raw []byte, data map[string]any) (*bursar.BillingEvent, error) {
	if data == nil {
		return nil, dodoWebhookMappingError("Dodo webhook data must be an object")
	}
	if occurredAt.IsZero() {
		return nil, dodoWebhookMappingError("Dodo webhook timestamp must be a valid instant")
	}
	metadata, err := dodoWebhookMetadata(data["metadata"])
	if err != nil {
		return nil, err
	}
	accountID := metadata["bursar_account_id"]
	customer, err := dodoCustomer(data, accountID, metadata)
	if err != nil {
		return nil, err
	}

	base := &bursar.BillingEvent{
		Provider:   ProviderName,
		OccurredAt: occurredAt.UTC(),
		AccountID:  accountID,
		Customer:   customer,
		Metadata:   metadataAny(metadata),
		RawPayload: append(json.RawMessage(nil), raw...),
	}

	var resourceID string
	switch providerType {
	case "subscription.active", "subscription.renewed", "subscription.cancelled", "subscription.expired", "subscription.failed", "subscription.on_hold", "subscription.paused", "subscription.updated", "subscription.plan_changed":
		resourceID, err = dodoRequiredText(data["subscription_id"], "Dodo subscription.subscription_id")
		if err != nil {
			return nil, err
		}
		status := ""
		switch providerType {
		case "subscription.active":
			base.Type, status = bursar.BillingEventSubscriptionCreated, "active"
			if value, exists := data["status"]; exists && value != nil {
				status, err = dodoSubscriptionStatus(value)
			}
		case "subscription.renewed":
			base.Type, status = bursar.BillingEventSubscriptionRenewed, "active"
		case "subscription.cancelled":
			base.Type, status = bursar.BillingEventSubscriptionCanceled, "canceled"
		case "subscription.expired":
			base.Type, status = bursar.BillingEventSubscriptionExpired, "expired"
		case "subscription.failed", "subscription.on_hold":
			base.Type, status = bursar.BillingEventSubscriptionUpdated, "past_due"
		case "subscription.paused":
			base.Type, status = bursar.BillingEventSubscriptionPaused, "paused"
		case "subscription.updated":
			base.Type = bursar.BillingEventSubscriptionUpdated
			if value, exists := data["status"]; exists && value != nil {
				status, err = dodoSubscriptionStatus(value)
			}
		case "subscription.plan_changed":
			base.Type, status = bursar.BillingEventSubscriptionPlanChanged, "active"
		}
		if err != nil {
			return nil, err
		}
		base.Subscription, err = dodoSubscription(data, metadata, accountID, resourceID, status)
	case "payment.succeeded":
		base.Type = bursar.BillingEventPaymentSucceeded
		resourceID, base.Payment, base.Subscription, err = dodoPayment(data, metadata, accountID, true, false)
	case "payment.failed", "payment.cancelled":
		base.Type = bursar.BillingEventPaymentFailed
		resourceID, base.Payment, base.Subscription, err = dodoPayment(data, metadata, accountID, false, providerType == "payment.cancelled")
	case "refund.succeeded", "refund.failed":
		if providerType == "refund.succeeded" {
			base.Type = bursar.BillingEventRefundCreated
		} else {
			base.Type = bursar.BillingEventRefundFailed
		}
		resourceID, base.Refund, err = dodoRefund(data, metadata, providerType == "refund.succeeded")
	case "dispute.opened", "dispute.challenged", "dispute.won", "dispute.lost", "dispute.accepted", "dispute.cancelled", "dispute.expired":
		base.Type = bursar.BillingEventDisputeCreated
		if providerType != "dispute.opened" && providerType != "dispute.challenged" {
			base.Type = bursar.BillingEventDisputeClosed
		}
		resourceID, base.Dispute, err = dodoDispute(data, metadata, providerType)
	default:
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	base.EventID = dodoCanonicalEventID(providerType, resourceID, occurredAt)
	base.ID = base.EventID
	base.BillingEventID = base.EventID
	if err := base.Validate(); err != nil {
		return nil, dodoWebhookMappingCause("invalid normalized Dodo webhook", err)
	}
	return base, nil
}

func dodoCanonicalEventID(providerType, resourceID string, occurredAt time.Time) string {
	return "dodo:" + providerType + ":" + resourceID + ":" + occurredAt.UTC().Format("2006-01-02T15:04:05.000Z07:00")
}

func dodoEventResourceID(providerType string, data map[string]any) (string, error) {
	switch {
	case strings.HasPrefix(providerType, "payment."):
		return dodoRequiredText(data["payment_id"], "Dodo webhook object identifier")
	case strings.HasPrefix(providerType, "subscription."):
		return dodoRequiredText(data["subscription_id"], "Dodo webhook object identifier")
	case strings.HasPrefix(providerType, "refund."):
		return dodoFirstRequiredText("Dodo webhook object identifier", data["refund_id"], data["id"])
	case strings.HasPrefix(providerType, "dispute."):
		return dodoFirstRequiredText("Dodo webhook object identifier", data["dispute_id"], data["id"])
	default:
		return dodoRequiredText(data["id"], "Dodo webhook object identifier")
	}
}

func dodoCustomer(data map[string]any, accountID string, metadata map[string]string) (*bursar.BillingCustomer, error) {
	customerObject, err := dodoOptionalObject(data["customer"], "Dodo customer")
	if err != nil {
		return nil, err
	}
	customerID, err := dodoOptionalText(data["customer_id"], "Dodo customer_id")
	if err != nil {
		return nil, err
	}
	if customerID == "" && customerObject != nil {
		customerID, err = dodoOptionalText(customerObject["customer_id"], "Dodo customer.customer_id")
		if err != nil {
			return nil, err
		}
	}
	email := ""
	if customerObject != nil {
		email, err = dodoOptionalText(customerObject["email"], "Dodo customer.email")
		if err != nil {
			return nil, err
		}
	}
	if customerID == "" && email == "" {
		return nil, nil
	}
	return &bursar.BillingCustomer{
		ProviderCustomerID: customerID,
		ID:                 customerID,
		AccountID:          accountID,
		Email:              email,
		Metadata:           metadataAny(metadata),
	}, nil
}

func dodoSubscription(data map[string]any, metadata map[string]string, accountID, subscriptionID, status string) (*bursar.BillingSubscription, error) {
	interval, err := dodoSubscriptionInterval(data, metadata)
	if err != nil {
		return nil, err
	}
	intervalCount, err := dodoSubscriptionIntervalCount(data, interval)
	if err != nil {
		return nil, err
	}
	periodStart, err := dodoOptionalTime(data["previous_billing_date"], "Dodo subscription.previous_billing_date")
	if err != nil {
		return nil, err
	}
	periodEnd, err := dodoOptionalTime(data["next_billing_date"], "Dodo subscription.next_billing_date")
	if err != nil {
		return nil, err
	}
	cancelAtPeriodEnd, err := dodoOptionalBool(data["cancel_at_next_billing_date"], "Dodo subscription.cancel_at_next_billing_date")
	if err != nil {
		return nil, err
	}
	refs, err := dodoSubscriptionRefs(data, metadata)
	if err != nil {
		return nil, err
	}
	return &bursar.BillingSubscription{
		ProviderSubscriptionID: subscriptionID,
		ID:                     subscriptionID,
		Provider:               ProviderName,
		AccountID:              accountID,
		Status:                 status,
		Interval:               interval,
		IntervalCount:          intervalCount,
		CurrentPeriodStart:     periodStart,
		CurrentPeriodEnd:       periodEnd,
		PeriodStart:            periodStart,
		PeriodEnd:              periodEnd,
		CancelAtPeriodEnd:      cancelAtPeriodEnd != nil && *cancelAtPeriodEnd,
		Refs:                   refs,
		Metadata:               metadataAny(metadata),
	}, nil
}

func dodoPayment(data map[string]any, metadata map[string]string, accountID string, succeeded, canceled bool) (string, *bursar.BillingPayment, *bursar.BillingSubscription, error) {
	paymentID, err := dodoRequiredText(data["payment_id"], "Dodo payment.payment_id")
	if err != nil {
		return "", nil, nil, err
	}
	subscriptionID, err := dodoOptionalText(data["subscription_id"], "Dodo payment.subscription_id")
	if err != nil {
		return "", nil, nil, err
	}
	amountField, currencyField, taxField := "total_amount", "currency", "tax"
	if succeeded {
		amountField, currencyField, taxField = "settlement_amount", "settlement_currency", "settlement_tax"
	}
	amount, err := dodoMinorUnits(data[amountField], "Dodo payment."+amountField, false)
	if err != nil {
		return "", nil, nil, err
	}
	taxValue := data[taxField]
	if taxValue == nil {
		taxValue = json.Number("0")
	}
	tax, err := dodoMinorUnits(taxValue, "Dodo payment."+taxField, false)
	if err != nil {
		return "", nil, nil, err
	}
	currency, err := dodoCurrency(data[currencyField], "Dodo payment."+currencyField)
	if err != nil {
		return "", nil, nil, err
	}
	productID, err := dodoProductID(data)
	if err != nil {
		return "", nil, nil, err
	}
	status := "failed"
	if succeeded {
		status = "succeeded"
	} else if canceled {
		status = "canceled"
	}
	purpose := "credit_topup"
	if subscriptionID != "" {
		purpose = "subscription"
	}
	payment := &bursar.BillingPayment{
		ProviderPaymentID: paymentID,
		ID:                paymentID,
		Provider:          ProviderName,
		AccountID:         accountID,
		SubscriptionID:    subscriptionID,
		Status:            status,
		Currency:          currency,
		AmountMinor:       amount,
		TaxMinor:          tax,
		Purpose:           purpose,
		Metadata:          metadataAny(metadata),
	}
	if productID != "" {
		payment.Refs = &bursar.ProviderRef{ProductID: productID}
	}
	var subscription *bursar.BillingSubscription
	if subscriptionID != "" {
		subscription = &bursar.BillingSubscription{
			ProviderSubscriptionID: subscriptionID,
			ID:                     subscriptionID,
			Provider:               ProviderName,
			AccountID:              accountID,
			Metadata:               metadataAny(metadata),
		}
		if succeeded {
			subscription.Status = "active"
			if value, exists := data["subscription_status"]; exists && value != nil {
				subscription.Status, err = dodoSubscriptionStatus(value)
				if err != nil {
					return "", nil, nil, err
				}
			}
			subscription.PeriodStart, err = dodoOptionalTime(data["previous_billing_date"], "Dodo payment.previous_billing_date")
			if err != nil {
				return "", nil, nil, err
			}
			subscription.PeriodEnd, err = dodoOptionalTime(data["next_billing_date"], "Dodo payment.next_billing_date")
			if err != nil {
				return "", nil, nil, err
			}
			subscription.CurrentPeriodStart = subscription.PeriodStart
			subscription.CurrentPeriodEnd = subscription.PeriodEnd
		}
	}
	return paymentID, payment, subscription, nil
}

func dodoRefund(data map[string]any, metadata map[string]string, succeeded bool) (string, *bursar.BillingRefund, error) {
	refundID, err := dodoFirstRequiredText("Dodo refund.refund_id", data["refund_id"], data["id"])
	if err != nil {
		return "", nil, err
	}
	paymentID, err := dodoRequiredText(data["payment_id"], "Dodo refund.payment_id")
	if err != nil {
		return "", nil, err
	}
	amountValue := data["refund_amount"]
	if amountValue == nil {
		amountValue = data["amount"]
	}
	amount, err := dodoMinorUnits(amountValue, "Dodo refund.amount", true)
	if err != nil {
		return "", nil, err
	}
	currency, err := dodoCurrency(data["currency"], "Dodo refund.currency")
	if err != nil {
		return "", nil, err
	}
	reason, err := dodoOptionalText(data["reason"], "Dodo refund.reason")
	if err != nil {
		return "", nil, err
	}
	status := "failed"
	if succeeded {
		status = "succeeded"
	}
	return refundID, &bursar.BillingRefund{
		ProviderRefundID:  refundID,
		ProviderPaymentID: paymentID,
		ID:                refundID,
		Provider:          ProviderName,
		PaymentID:         paymentID,
		Status:            status,
		Currency:          currency,
		AmountMinor:       amount,
		Reason:            reason,
		Metadata:          metadataAny(metadata),
	}, nil
}

func dodoDispute(data map[string]any, metadata map[string]string, providerType string) (string, *bursar.BillingDispute, error) {
	disputeID, err := dodoFirstRequiredText("Dodo dispute.dispute_id", data["dispute_id"], data["id"])
	if err != nil {
		return "", nil, err
	}
	paymentID, err := dodoRequiredText(data["payment_id"], "Dodo dispute.payment_id")
	if err != nil {
		return "", nil, err
	}
	reason, err := dodoOptionalText(data["reason"], "Dodo dispute.reason")
	if err != nil {
		return "", nil, err
	}
	status := "closed"
	switch providerType {
	case "dispute.opened":
		status = "needs_response"
	case "dispute.challenged":
		status = "under_review"
	case "dispute.won":
		status = "won"
	case "dispute.lost", "dispute.accepted":
		status = "lost"
	}
	return disputeID, &bursar.BillingDispute{
		ProviderDisputeID: disputeID,
		ProviderPaymentID: paymentID,
		ID:                disputeID,
		Provider:          ProviderName,
		PaymentID:         paymentID,
		Status:            status,
		Reason:            reason,
		Metadata:          metadataAny(metadata),
	}, nil
}

func dodoSubscriptionInterval(data map[string]any, metadata map[string]string) (string, error) {
	values := []struct {
		value any
		field string
	}{
		{data["payment_frequency_interval"], "Dodo subscription.payment_frequency_interval"},
		{data["subscription_period_interval"], "Dodo subscription.subscription_period_interval"},
	}
	if metadataInterval, exists := metadata["billing_interval"]; exists {
		values = append(values, struct {
			value any
			field string
		}{metadataInterval, "Dodo metadata.billing_interval"})
	}
	for _, candidate := range values {
		value, err := dodoOptionalText(candidate.value, candidate.field)
		if err != nil {
			return "", err
		}
		if value == "" {
			continue
		}
		value = strings.ToLower(value)
		switch value {
		case "day", "week", "month", "year":
			return value, nil
		default:
			return "", dodoWebhookMappingError(candidate.field + " must be day, week, month, or year")
		}
	}
	return "", nil
}

func dodoSubscriptionIntervalCount(data map[string]any, interval string) (int, error) {
	value := data["payment_frequency_count"]
	if value == nil {
		value = data["subscription_period_count"]
	}
	if value == nil && interval != "" {
		return 1, nil
	}
	if value == nil {
		return 0, nil
	}
	if interval == "" {
		return 0, dodoWebhookMappingError("Dodo subscription interval count requires an interval")
	}
	parsed, err := dodoMinorUnits(value, "Dodo subscription interval count", true)
	if err != nil || parsed > int64(^uint(0)>>1) {
		if err != nil {
			return 0, err
		}
		return 0, dodoWebhookMappingError("Dodo subscription interval count is too large")
	}
	return int(parsed), nil
}

func dodoSubscriptionRefs(data map[string]any, metadata map[string]string) (*bursar.ProviderRef, error) {
	productID, err := dodoProductID(data)
	if err != nil {
		return nil, err
	}
	if productID != "" {
		return &bursar.ProviderRef{ProductID: productID}, nil
	}
	if lookupKey := strings.TrimSpace(metadata["plan_slug"]); lookupKey != "" {
		return &bursar.ProviderRef{LookupKey: lookupKey}, nil
	}
	return nil, nil
}

func dodoProductID(data map[string]any) (string, error) {
	direct, err := dodoOptionalText(data["product_id"], "Dodo product_id")
	if err != nil || direct != "" {
		return direct, err
	}
	value, exists := data["product_cart"]
	if !exists || value == nil {
		return "", nil
	}
	items, ok := value.([]any)
	if !ok {
		return "", dodoWebhookMappingError("Dodo product_cart must be an array")
	}
	for _, item := range items {
		object, ok := item.(map[string]any)
		if !ok {
			return "", dodoWebhookMappingError("Dodo product_cart item must be an object")
		}
		productID, err := dodoOptionalText(object["product_id"], "Dodo product_cart item.product_id")
		if err != nil {
			return "", err
		}
		if productID != "" {
			return productID, nil
		}
	}
	return "", nil
}

func dodoSubscriptionStatus(value any) (string, error) {
	status, err := dodoRequiredText(value, "Dodo subscription.status")
	if err != nil {
		return "", err
	}
	switch status {
	case "pending":
		return "incomplete", nil
	case "trialing", "active", "paused", "expired":
		return status, nil
	case "on_hold", "failed":
		return "past_due", nil
	case "cancelled":
		return "canceled", nil
	default:
		// Dodo may add states before the portable billing vocabulary does. The
		// JS/Python reference adapters preserve the event while omitting an
		// unsupported status rather than inventing a state transition.
		return "", nil
	}
}

func dodoWebhookMetadata(value any) (map[string]string, error) {
	if value == nil {
		return map[string]string{}, nil
	}
	object, ok := value.(map[string]any)
	if !ok {
		return nil, dodoWebhookMappingError("Dodo webhook metadata must be an object")
	}
	metadata := make(map[string]string, len(object))
	for key, value := range object {
		var text string
		switch item := value.(type) {
		case string:
			text = item
		case bool:
			text = strconv.FormatBool(item)
		case json.Number:
			if _, err := strconv.ParseFloat(item.String(), 64); err != nil {
				return nil, dodoWebhookMappingError("Dodo webhook metadata." + key + " must be a scalar value")
			}
			text = item.String()
		case float64:
			text = strconv.FormatFloat(item, 'g', -1, 64)
		default:
			return nil, dodoWebhookMappingError("Dodo webhook metadata." + key + " must be a scalar value")
		}
		if _, restricted := dodoBursarMetadataKeys[key]; restricted {
			if _, isString := value.(string); !isString {
				return nil, dodoWebhookMappingError("Dodo webhook metadata." + key + " must be a string")
			}
		}
		metadata[key] = text
	}
	return metadata, nil
}

func metadataAny(metadata map[string]string) map[string]any {
	if len(metadata) == 0 {
		return nil
	}
	result := make(map[string]any, len(metadata))
	for key, value := range metadata {
		result[key] = value
	}
	return result
}

func dodoOptionalObject(value any, field string) (map[string]any, error) {
	if value == nil {
		return nil, nil
	}
	object, ok := value.(map[string]any)
	if !ok {
		return nil, dodoWebhookMappingError(field + " must be an object")
	}
	return object, nil
}

func dodoRequiredText(value any, field string) (string, error) {
	text, ok := value.(string)
	if !ok || strings.TrimSpace(text) == "" {
		return "", dodoWebhookMappingError(field + " must be a non-empty string")
	}
	return strings.TrimSpace(text), nil
}

func dodoFirstRequiredText(field string, values ...any) (string, error) {
	for _, value := range values {
		if value == nil {
			continue
		}
		return dodoRequiredText(value, field)
	}
	return "", dodoWebhookMappingError(field + " must be a non-empty string")
}

func dodoOptionalText(value any, field string) (string, error) {
	if value == nil {
		return "", nil
	}
	return dodoRequiredText(value, field)
}

func dodoOptionalBool(value any, field string) (*bool, error) {
	if value == nil {
		return nil, nil
	}
	parsed, ok := value.(bool)
	if !ok {
		return nil, dodoWebhookMappingError(field + " must be a boolean")
	}
	return &parsed, nil
}

func dodoMinorUnits(value any, field string, positive bool) (int64, error) {
	var parsed int64
	var err error
	switch item := value.(type) {
	case json.Number:
		parsed, err = item.Int64()
	case int64:
		parsed = item
	case int:
		parsed = int64(item)
	case string:
		if item == "" || strings.Trim(item, "0123456789") != "" {
			err = fmt.Errorf("not an unsigned integer")
		} else {
			parsed, err = strconv.ParseInt(item, 10, 64)
		}
	default:
		err = fmt.Errorf("not an integer")
	}
	minimum := int64(0)
	if positive {
		minimum = 1
	}
	if err != nil || parsed < minimum {
		word := "non-negative"
		if positive {
			word = "positive"
		}
		return 0, dodoWebhookMappingError(field + " must be a " + word + " integer")
	}
	return parsed, nil
}

func dodoCurrency(value any, field string) (string, error) {
	text, ok := value.(string)
	if !ok || !dodoCurrencyPattern.MatchString(text) {
		return "", dodoWebhookMappingError(field + " must be a three-letter currency code")
	}
	return strings.ToUpper(text), nil
}

func dodoOptionalTime(value any, field string) (*time.Time, error) {
	if value == nil {
		return nil, nil
	}
	text, ok := value.(string)
	if !ok || strings.TrimSpace(text) == "" {
		return nil, dodoWebhookMappingError(field + " must be a valid instant")
	}
	text = strings.TrimSpace(text)
	for _, layout := range []string{time.RFC3339Nano, "2006-01-02T15:04:05.999999999-0700", "Mon Jan 02 2006 15:04:05 GMT-0700"} {
		candidate := text
		if layout != time.RFC3339Nano {
			if index := strings.Index(candidate, " ("); index >= 0 {
				candidate = candidate[:index]
			}
		}
		parsed, err := time.Parse(layout, candidate)
		if err == nil {
			parsed = parsed.UTC()
			return &parsed, nil
		}
	}
	return nil, dodoWebhookMappingError(field + " must be a valid instant")
}

func dodoWebhookMappingError(message string) *bursar.BursarError {
	return bursar.NewError(message, bursar.ErrorOptions{Code: bursar.ErrorCodeProviderResponseInvalid, Category: bursar.ErrorCategoryInvalidRequest})
}

func dodoWebhookMappingCause(message string, cause error) *bursar.BursarError {
	return bursar.NewError(message, bursar.ErrorOptions{Code: bursar.ErrorCodeProviderResponseInvalid, Category: bursar.ErrorCategoryInvalidRequest, Cause: cause})
}
