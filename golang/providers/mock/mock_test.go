// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package mock

import (
	"net/http"
	"testing"
	"time"

	bursar "github.com/Zonastery/bursar/golang/v2"
)

func TestProviderImplementsPortableCapabilities(t *testing.T) {
	provider := New(Options{Now: func() time.Time { return time.Date(2026, 7, 18, 5, 15, 24, 0, time.UTC) }})
	if provider.ProviderEnvironment() != bursar.ProviderEnvironmentTest {
		t.Fatalf("unexpected environment: %q", provider.ProviderEnvironment())
	}
	var _ bursar.PaymentProvider = provider
	var _ bursar.CustomerPortalProvider = provider
	var _ bursar.PaymentMethodPortalProvider = provider
	var _ bursar.CustomerProvider = provider
	var _ bursar.SubscriptionProvider = provider
	var _ bursar.InvoiceProvider = provider
	var _ bursar.PaymentMethodsProvider = provider
	var _ bursar.SavedPaymentPreviewProvider = provider
	var _ bursar.SavedPaymentChargeProvider = provider
	var _ bursar.PlanChangePreviewProvider = provider
	var _ bursar.PlanChangeProvider = provider
	var _ bursar.ScheduledPlanChangeCancellationProvider = provider
}

func TestDeterministicIdempotentCustomerChargeAndPlanChange(t *testing.T) {
	provider := New(Options{})
	request := bursar.CreateCustomerRequest{Email: "buyer@example.com", Name: "Buyer", IdempotencyKey: "customer-op-1"}
	firstCustomer, err := provider.CreateCustomer(t.Context(), request)
	if err != nil {
		t.Fatal(err)
	}
	secondCustomer, err := provider.CreateCustomer(t.Context(), request)
	if err != nil || firstCustomer != secondCustomer || firstCustomer == "" {
		t.Fatalf("customer was not idempotent: %q, %q, %v", firstCustomer, secondCustomer, err)
	}
	if _, err := provider.CreateCustomer(t.Context(), bursar.CreateCustomerRequest{Email: "buyer@example.com", Name: "Buyer"}); err == nil {
		t.Fatal("expected customer idempotency-key validation")
	}

	chargeRequest := bursar.SavedPaymentChargeParams{CustomerID: firstCustomer, PaymentMethodID: "pm_1", ProductID: "prod_1", Quantity: 1, IdempotencyKey: "charge-op-1"}
	firstCharge, err := provider.ChargeSavedPaymentMethod(t.Context(), chargeRequest)
	if err != nil {
		t.Fatal(err)
	}
	secondCharge, err := provider.ChargeSavedPaymentMethod(t.Context(), chargeRequest)
	if err != nil || firstCharge.ProviderPaymentID != secondCharge.ProviderPaymentID || firstCharge.ProviderPaymentID == "" {
		t.Fatalf("charge was not idempotent: %#v, %#v, %v", firstCharge, secondCharge, err)
	}

	planRequest := bursar.ProviderPlanChangeRequest{ProviderSubscriptionID: "sub_1", ProductID: "prod_2", Quantity: 1, IdempotencyKey: "plan-op-1"}
	firstPlan, err := provider.ChangePlan(t.Context(), planRequest)
	if err != nil {
		t.Fatal(err)
	}
	secondPlan, err := provider.ChangePlan(t.Context(), planRequest)
	if err != nil || firstPlan != secondPlan || firstPlan.ProviderOperationID == "" {
		t.Fatalf("plan change was not idempotent: %#v, %#v, %v", firstPlan, secondPlan, err)
	}
}

func TestQueueEventUsesCanonicalEventID(t *testing.T) {
	provider := New(Options{})
	event := bursar.BillingEvent{
		EventID:    "evt_1",
		Provider:   ProviderName,
		Type:       bursar.BillingEventPaymentSucceeded,
		OccurredAt: time.Date(2026, 7, 18, 5, 15, 24, 0, time.UTC),
		Payment:    &bursar.BillingPayment{ProviderPaymentID: "pay_1", Provider: ProviderName, AmountMinor: 1, Currency: "USD", Status: "succeeded", Purpose: "credit_topup"},
	}
	if err := provider.QueueEvent(event); err != nil {
		t.Fatal(err)
	}
	result, err := provider.HandleWebhook(t.Context(), bursar.WebhookRequest{Header: http.Header{"X-Bursar-Mock-Event": []string{"evt_1"}}})
	if err != nil || result.Event == nil || result.Event.EventID != "evt_1" || result.Event.ID != "evt_1" {
		t.Fatalf("unexpected queued event: %#v, %v", result, err)
	}
}
