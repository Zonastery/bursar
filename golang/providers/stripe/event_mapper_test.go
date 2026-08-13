// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package stripe

import (
	"context"
	"encoding/json"
	"net/http"
	"testing"
	"time"

	bursar "github.com/Zonastery/bursar/golang/v2"
	stripego "github.com/stripe/stripe-go/v84"
)

type stripeWebhookResourcesStub struct {
	checkout          *stripego.CheckoutSession
	subscription      *stripego.Subscription
	checkoutCalls     int
	subscriptionCalls int
}

func (s *stripeWebhookResourcesStub) retrieveCheckout(context.Context, string) (*stripego.CheckoutSession, error) {
	s.checkoutCalls++
	return s.checkout, nil
}

func (s *stripeWebhookResourcesStub) retrieveSubscription(context.Context, string) (*stripego.Subscription, error) {
	s.subscriptionCalls++
	return s.subscription, nil
}

func TestHandleWebhookVerifiesRawStripeSignatureAndIgnoresUnknown(t *testing.T) {
	const secret = "whsec_test"
	p, err := New(Options{APIKey: "sk_test", WebhookSecret: secret, Environment: bursar.ProviderEnvironmentTest})
	if err != nil {
		t.Fatal(err)
	}
	payload := []byte(`{"id":"evt_unknown","object":"event","api_version":"2025-12-15.clover","created":1784351724,"livemode":false,"type":"product.updated","data":{"object":{"id":"prod_1","object":"product"}}}`)
	signed := stripego.GenerateTestSignedPayload(&stripego.UnsignedPayload{Payload: payload, Secret: secret, Timestamp: time.Now()})
	request := bursar.WebhookRequest{RawBody: payload, Header: http.Header{"Stripe-Signature": []string{signed.Header}}}
	result, err := p.HandleWebhook(t.Context(), request)
	if err != nil {
		t.Fatal(err)
	}
	if !result.Received || result.Event != nil || result.EventID != "evt_unknown" || result.EventType != "product.updated" {
		t.Fatalf("unexpected unsupported-event result: %#v", result)
	}
	retry, err := p.HandleWebhook(t.Context(), request)
	if err != nil || retry.EventID != result.EventID || retry.Event != nil {
		t.Fatalf("webhook retry was not deterministic: %#v, %v", retry, err)
	}

	request.RawBody = append([]byte(nil), payload...)
	request.RawBody[20] ^= 1
	if _, err := p.HandleWebhook(t.Context(), request); err == nil {
		t.Fatal("expected signature verification failure")
	}
}

func TestHandleWebhookRejectsStripeEnvironmentMismatch(t *testing.T) {
	const secret = "whsec_test"
	p, err := New(Options{APIKey: "sk_test", WebhookSecret: secret, Environment: bursar.ProviderEnvironmentTest})
	if err != nil {
		t.Fatal(err)
	}
	payload := []byte(`{"id":"evt_live","object":"event","api_version":"2025-12-15.clover","created":1784351724,"livemode":true,"type":"product.updated","data":{"object":{"id":"prod_1","object":"product"}}}`)
	signed := stripego.GenerateTestSignedPayload(&stripego.UnsignedPayload{Payload: payload, Secret: secret, Timestamp: time.Now()})
	request := bursar.WebhookRequest{RawBody: payload, Header: http.Header{"Stripe-Signature": []string{signed.Header}}}
	if _, err := p.HandleWebhook(t.Context(), request); err == nil {
		t.Fatal("expected webhook/provider environment mismatch")
	}
	if _, err := New(Options{APIKey: "sk_test", WebhookSecret: secret, Environment: bursar.ProviderEnvironment("staging")}); err == nil {
		t.Fatal("expected invalid provider environment")
	}
}

func TestStripeSubscriptionCancellationMapping(t *testing.T) {
	created := int64(1_784_351_724)
	cases := []struct {
		name         string
		providerType string
		status       string
		cancelEnd    bool
		expected     bursar.BillingEventType
	}{
		{"scheduled", "customer.subscription.updated", "active", true, bursar.BillingEventSubscriptionCancellationScheduled},
		{"terminal update", "customer.subscription.updated", "canceled", false, bursar.BillingEventSubscriptionCanceled},
		{"deleted", "customer.subscription.deleted", "active", false, bursar.BillingEventSubscriptionCanceled},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			payload := json.RawMessage(`{"id":"sub_1","status":"` + tc.status + `","customer":"cus_1","cancel_at_period_end":` + boolJSON(tc.cancelEnd) + `,"metadata":{"bursar_account_id":"acct_1"},"items":{"data":[{"current_period_start":1784351724,"current_period_end":1787020124,"price":{"id":"price_1","product":"prod_1","recurring":{"interval":"month","interval_count":1}}}]}}`)
			event := stripego.Event{ID: "evt_1", Created: created, Type: stripego.EventType(tc.providerType), Data: &stripego.EventData{Raw: payload}}
			mapped, err := mapStripeEvent(t.Context(), event, []byte(`raw`), nil)
			if err != nil {
				t.Fatal(err)
			}
			if mapped.Type != tc.expected || mapped.Subscription.ProviderSubscriptionID != "sub_1" || mapped.AccountID != "acct_1" {
				t.Fatalf("unexpected event: %#v", mapped)
			}
			if tc.name == "deleted" && mapped.Subscription.EndedAt == nil {
				t.Fatal("deleted subscription should have terminal ended_at")
			}
		})
	}
}

func TestStripeAsyncCheckoutFailureAndReferenceExpansion(t *testing.T) {
	subscriptionPayload := []byte(`{"id":"sub_1","status":"past_due","customer":"cus_1","metadata":{"bursar_account_id":"acct_1"},"items":{"data":[{"current_period_start":1784351724,"current_period_end":1787020124,"price":{"id":"price_1","product":"prod_1","recurring":{"interval":"month","interval_count":1}}}]}}`)
	var subscription stripego.Subscription
	if err := json.Unmarshal(subscriptionPayload, &subscription); err != nil {
		t.Fatal(err)
	}
	resources := &stripeWebhookResourcesStub{
		checkout:     &stripego.CheckoutSession{LineItems: &stripego.LineItemList{Data: []*stripego.LineItem{{Price: &stripego.Price{ID: "price_1", Product: &stripego.Product{ID: "prod_1"}}}}}},
		subscription: &subscription,
	}
	payload := json.RawMessage(`{"id":"cs_1","mode":"subscription","payment_status":"unpaid","payment_intent":"pi_1","subscription":"sub_1","customer":"cus_1","amount_subtotal":2999,"amount_total":3239,"total_details":{"amount_tax":240},"currency":"usd","client_reference_id":"acct_1","metadata":{"plan_slug":"pro"}}`)
	event := stripego.Event{ID: "evt_async_fail", Created: 1_784_351_724, Type: stripego.EventType("checkout.session.async_payment_failed"), Data: &stripego.EventData{Raw: payload}}
	mapped, err := mapStripeEvent(t.Context(), event, []byte(`raw`), resources)
	if err != nil {
		t.Fatal(err)
	}
	if mapped.Type != bursar.BillingEventPaymentFailed || mapped.Payment.Status != "failed" || mapped.Payment.ProviderPaymentID != "pi_1" || mapped.Payment.AmountMinor != 2999 || mapped.Payment.TaxMinor != 240 {
		t.Fatalf("unexpected payment: %#v", mapped.Payment)
	}
	if mapped.Subscription == nil || mapped.Subscription.ProviderSubscriptionID != "sub_1" || mapped.Payment.Refs == nil || mapped.Payment.Refs.PriceID != "price_1" || mapped.Payment.Refs.ProductID != "prod_1" {
		t.Fatalf("unexpected expanded mapping: %#v", mapped)
	}
	if resources.checkoutCalls != 1 || resources.subscriptionCalls != 1 {
		t.Fatalf("unexpected retrieval calls: %#v", resources)
	}
}

func TestStripePaymentIntentAllowlistRefundAndDisputeValidation(t *testing.T) {
	ignored := stripego.Event{ID: "evt_pi", Created: 1, Type: stripego.EventType("payment_intent.succeeded"), Data: &stripego.EventData{Raw: json.RawMessage(`{"id":"pi_1","amount":100,"currency":"usd","metadata":{}}`)}}
	mapped, err := mapStripeEvent(t.Context(), ignored, nil, nil)
	if err != nil || mapped != nil {
		t.Fatalf("non-auto-recharge intent should be ignored: %#v, %v", mapped, err)
	}

	refund := stripego.Event{ID: "evt_ref", Created: 1, Type: stripego.EventType("refund.updated"), Data: &stripego.EventData{Raw: json.RawMessage(`{"id":"re_1","payment_intent":"pi_1","amount":250,"currency":"usd","status":"requires_action","reason":"requested_by_customer","metadata":{"bursar_account_id":"acct_1"}}`)}}
	mapped, err = mapStripeEvent(t.Context(), refund, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	if mapped.Type != bursar.BillingEventRefundUpdated || mapped.Refund.Status != "pending" || mapped.Refund.ProviderPaymentID != "pi_1" || mapped.Refund.Reason != "requested_by_customer" {
		t.Fatalf("unexpected refund: %#v", mapped.Refund)
	}

	dispute := stripego.Event{ID: "evt_dp", Created: 1, Type: stripego.EventType("charge.dispute.updated"), Data: &stripego.EventData{Raw: json.RawMessage(`{"id":"dp_1","payment_intent":"pi_1","status":"bogus","reason":"fraudulent"}`)}}
	if _, err := mapStripeEvent(t.Context(), dispute, nil, nil); err == nil {
		t.Fatal("expected unsupported dispute status validation")
	}
}

func TestStripeDisputeVariants(t *testing.T) {
	for _, tc := range []struct {
		providerType   string
		providerStatus string
		eventType      bursar.BillingEventType
		status         string
	}{
		{"charge.dispute.created", "needs_response", bursar.BillingEventDisputeCreated, "needs_response"},
		{"charge.dispute.updated", "warning_under_review", bursar.BillingEventDisputeCreated, "under_review"},
		{"charge.dispute.closed", "prevented", bursar.BillingEventDisputeClosed, "won"},
		{"charge.dispute.closed", "lost", bursar.BillingEventDisputeClosed, "lost"},
		{"charge.dispute.closed", "warning_closed", bursar.BillingEventDisputeClosed, "closed"},
	} {
		t.Run(tc.providerType+"/"+tc.providerStatus, func(t *testing.T) {
			raw := json.RawMessage(`{"id":"dp_1","payment_intent":"pi_1","status":"` + tc.providerStatus + `","reason":"fraudulent","metadata":{"bursar_account_id":"acct_1"}}`)
			event := stripego.Event{ID: "evt_dp", Created: 1, Type: stripego.EventType(tc.providerType), Data: &stripego.EventData{Raw: raw}}
			mapped, err := mapStripeEvent(t.Context(), event, nil, nil)
			if err != nil {
				t.Fatal(err)
			}
			if mapped.Type != tc.eventType || mapped.Dispute.Status != tc.status || mapped.Dispute.ProviderDisputeID != "dp_1" || mapped.Dispute.ProviderPaymentID != "pi_1" || mapped.AccountID != "acct_1" {
				t.Fatalf("unexpected dispute: %#v", mapped)
			}
		})
	}
}

func boolJSON(value bool) string {
	if value {
		return "true"
	}
	return "false"
}
