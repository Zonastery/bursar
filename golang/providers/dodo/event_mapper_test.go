// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package dodo

import (
	"encoding/json"
	"net/http"
	"strconv"
	"strings"
	"testing"
	"time"

	bursar "github.com/Zonastery/bursar/golang/v2"
	standardwebhooks "github.com/standard-webhooks/standard-webhooks/libraries/go"
)

const dodoTestWebhookKey = "whsec_c2VjcmV0Cg=="

func TestHandleWebhookVerifiesAndMapsCanonicalPayment(t *testing.T) {
	p, err := New(Options{APIKey: "test", WebhookKey: dodoTestWebhookKey, Environment: bursar.ProviderEnvironmentTest})
	if err != nil {
		t.Fatal(err)
	}
	payload := []byte(`{"business_id":"bus_test","type":"payment.succeeded","timestamp":"2026-07-18T05:15:24Z","data":{"payment_id":"pay_123","subscription_id":"sub_123","customer_id":"cus_123","settlement_amount":2999,"settlement_tax":240,"settlement_currency":"usd","product_id":"prod_123","metadata":{"bursar_account_id":"acct_123","credits":"100"}}}`)
	request := bursar.WebhookRequest{RawBody: payload, Header: signDodoWebhook(t, "wh_123", payload)}

	result, err := p.HandleWebhook(t.Context(), request)
	if err != nil {
		t.Fatal(err)
	}
	if result.Event == nil {
		t.Fatal("expected normalized event")
	}
	event := result.Event
	if event.Type != bursar.BillingEventPaymentSucceeded || event.EventID != "dodo:payment.succeeded:pay_123:2026-07-18T05:15:24.000Z" {
		t.Fatalf("unexpected event: %#v", event)
	}
	if event.AccountID != "acct_123" || event.Payment.ProviderPaymentID != "pay_123" || event.Payment.AmountMinor != 2999 || event.Payment.TaxMinor != 240 || event.Payment.Currency != "USD" || event.Payment.Purpose != "subscription" {
		t.Fatalf("unexpected payment: %#v", event.Payment)
	}
	if event.Subscription == nil || event.Subscription.ProviderSubscriptionID != "sub_123" || event.Subscription.Status != "active" {
		t.Fatalf("unexpected subscription: %#v", event.Subscription)
	}
	if event.Payment.Refs == nil || event.Payment.Refs.ProductID != "prod_123" {
		t.Fatalf("unexpected refs: %#v", event.Payment.Refs)
	}

	result2, err := p.HandleWebhook(t.Context(), request)
	if err != nil || result2.EventID != result.EventID {
		t.Fatalf("webhook retry was not deterministic: %#v, %v", result2, err)
	}
}

func TestHandleWebhookRejectsInvalidDodoSignature(t *testing.T) {
	p, err := New(Options{APIKey: "test", WebhookKey: dodoTestWebhookKey})
	if err != nil {
		t.Fatal(err)
	}
	payload := []byte(`{"business_id":"bus_test","type":"payment.failed","timestamp":"2026-07-18T05:15:24Z","data":{"payment_id":"pay_123","total_amount":10,"currency":"USD"}}`)
	headers := signDodoWebhook(t, "wh_bad", payload)
	payload[10] ^= 1
	if _, err := p.HandleWebhook(t.Context(), bursar.WebhookRequest{RawBody: payload, Header: headers}); err == nil {
		t.Fatal("expected invalid signature error")
	}
}

func TestHandleWebhookAcknowledgesUnsupportedDodoEventWithCanonicalID(t *testing.T) {
	p, err := New(Options{APIKey: "test", WebhookKey: dodoTestWebhookKey})
	if err != nil {
		t.Fatal(err)
	}
	payload := []byte(`{"business_id":"bus_test","type":"payment.processing","timestamp":"2026-07-18T05:15:24Z","data":{"payment_id":"pay_processing","status":"processing","total_amount":10,"currency":"USD"}}`)
	result, err := p.HandleWebhook(t.Context(), bursar.WebhookRequest{RawBody: payload, Header: signDodoWebhook(t, "wh_processing", payload)})
	if err != nil {
		t.Fatal(err)
	}
	if !result.Received || result.Event != nil || result.EventID != "dodo:payment.processing:pay_processing:2026-07-18T05:15:24.000Z" {
		t.Fatalf("unexpected unsupported-event result: %#v", result)
	}
}

func TestDodoMapperAllowlistAndTerminalStates(t *testing.T) {
	occurredAt := time.Date(2026, 7, 18, 5, 15, 24, 0, time.UTC)
	unknown, err := mapDodoEvent("payment.processing", occurredAt, nil, map[string]any{"payment_id": "pay_1"})
	if err != nil || unknown != nil {
		t.Fatalf("unsupported event should be acknowledged without normalization: %#v, %v", unknown, err)
	}

	cases := []struct {
		providerType string
		eventType    bursar.BillingEventType
		status       string
	}{
		{"subscription.on_hold", bursar.BillingEventSubscriptionUpdated, "past_due"},
		{"subscription.failed", bursar.BillingEventSubscriptionUpdated, "past_due"},
		{"subscription.cancelled", bursar.BillingEventSubscriptionCanceled, "canceled"},
		{"subscription.expired", bursar.BillingEventSubscriptionExpired, "expired"},
	}
	for _, tc := range cases {
		t.Run(tc.providerType, func(t *testing.T) {
			event, err := mapDodoEvent(tc.providerType, occurredAt, nil, map[string]any{
				"subscription_id":             "sub_1",
				"product_id":                  "prod_1",
				"cancel_at_next_billing_date": true,
				"next_billing_date":           "Sat Jul 18 2026 05:15:24 GMT+0000 (Coordinated Universal Time)",
			})
			if err != nil {
				t.Fatal(err)
			}
			if event.Type != tc.eventType || event.Subscription.Status != tc.status || !event.Subscription.CancelAtPeriodEnd || event.Subscription.PeriodEnd == nil {
				t.Fatalf("unexpected mapping: %#v", event)
			}
		})
	}
}

func TestDodoMapperValidatesFieldsAndVariants(t *testing.T) {
	occurredAt := time.Date(2026, 7, 18, 5, 15, 24, 0, time.UTC)
	cancelled, err := mapDodoEvent("payment.cancelled", occurredAt, nil, map[string]any{
		"payment_id": "pay_1", "total_amount": json.Number("2999"), "tax": json.Number("240"), "currency": "usd",
	})
	if err != nil {
		t.Fatal(err)
	}
	if cancelled.Type != bursar.BillingEventPaymentFailed || cancelled.Payment.Status != "canceled" || cancelled.Payment.AmountMinor != 2999 || cancelled.Payment.TaxMinor != 240 {
		t.Fatalf("unexpected canceled payment: %#v", cancelled.Payment)
	}
	if _, err := mapDodoEvent("refund.succeeded", occurredAt, nil, map[string]any{
		"refund_id": "ref_1", "payment_id": "pay_1", "refund_amount": json.Number("0"), "currency": "USD",
	}); err == nil {
		t.Fatal("expected positive refund amount validation")
	}
	if _, err := mapDodoEvent("payment.failed", occurredAt, nil, map[string]any{
		"payment_id": "pay_1", "total_amount": json.Number("1"), "currency": "USD", "metadata": map[string]any{"bursar_account_id": json.Number("3")},
	}); err == nil || !strings.Contains(err.Error(), "must be a string") {
		t.Fatalf("expected protected metadata validation, got %v", err)
	}
}

func TestDodoRefundAndDisputeVariants(t *testing.T) {
	occurredAt := time.Date(2026, 7, 18, 5, 15, 24, 0, time.UTC)
	for _, tc := range []struct {
		providerType string
		eventType    bursar.BillingEventType
		status       string
	}{
		{"refund.succeeded", bursar.BillingEventRefundCreated, "succeeded"},
		{"refund.failed", bursar.BillingEventRefundFailed, "failed"},
	} {
		t.Run(tc.providerType, func(t *testing.T) {
			event, err := mapDodoEvent(tc.providerType, occurredAt, nil, map[string]any{
				"refund_id": "ref_1", "payment_id": "pay_1", "refund_amount": json.Number("500"), "currency": "usd", "reason": "requested_by_customer",
			})
			if err != nil {
				t.Fatal(err)
			}
			if event.Type != tc.eventType || event.Refund.Status != tc.status || event.Refund.ProviderRefundID != "ref_1" || event.Refund.ProviderPaymentID != "pay_1" || event.Refund.Reason != "requested_by_customer" {
				t.Fatalf("unexpected refund: %#v", event.Refund)
			}
		})
	}

	for _, tc := range []struct {
		providerType string
		eventType    bursar.BillingEventType
		status       string
	}{
		{"dispute.opened", bursar.BillingEventDisputeCreated, "needs_response"},
		{"dispute.challenged", bursar.BillingEventDisputeCreated, "under_review"},
		{"dispute.won", bursar.BillingEventDisputeClosed, "won"},
		{"dispute.lost", bursar.BillingEventDisputeClosed, "lost"},
		{"dispute.accepted", bursar.BillingEventDisputeClosed, "lost"},
		{"dispute.cancelled", bursar.BillingEventDisputeClosed, "closed"},
		{"dispute.expired", bursar.BillingEventDisputeClosed, "closed"},
	} {
		t.Run(tc.providerType, func(t *testing.T) {
			event, err := mapDodoEvent(tc.providerType, occurredAt, nil, map[string]any{
				"dispute_id": "dp_1", "payment_id": "pay_1", "reason": "fraudulent",
			})
			if err != nil {
				t.Fatal(err)
			}
			if event.Type != tc.eventType || event.Dispute.Status != tc.status || event.Dispute.ProviderDisputeID != "dp_1" || event.Dispute.ProviderPaymentID != "pay_1" {
				t.Fatalf("unexpected dispute: %#v", event.Dispute)
			}
		})
	}
}

func signDodoWebhook(t *testing.T, id string, payload []byte) http.Header {
	t.Helper()
	webhook, err := standardwebhooks.NewWebhook(dodoTestWebhookKey)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now().UTC()
	signature, err := webhook.Sign(id, now, payload)
	if err != nil {
		t.Fatal(err)
	}
	header := make(http.Header)
	header.Set("webhook-id", id)
	header.Set("webhook-signature", signature)
	header.Set("webhook-timestamp", strconv.FormatInt(now.Unix(), 10))
	return header
}
