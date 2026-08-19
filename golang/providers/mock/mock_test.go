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

func TestProviderCoversCustomerCheckoutCommerceAndValidationFlows(t *testing.T) {
	provider := New(Options{
		Name:            "mock-test",
		CheckoutURLBase: "https://checkout.example.test/",
		Environment:     bursar.ProviderEnvironmentLive,
		PaymentMethods: []bursar.PaymentMethodInfo{{
			ID: "pm_1", Brand: "visa", Last4: "4242", ExpiryMonth: 12, ExpiryYear: 2099, IsDefault: true,
		}},
	})
	ctx := t.Context()

	checkoutRequest := bursar.CheckoutSessionRequest{AccountID: "acct_1", ProductID: "prod_1", Quantity: 2, CustomerID: "cus_1", IdempotencyKey: "checkout-1"}
	first, err := provider.CreateCheckoutSession(ctx, checkoutRequest)
	if err != nil {
		t.Fatal(err)
	}
	replay, err := provider.CreateCheckoutSession(ctx, checkoutRequest)
	if err != nil || replay != first || first.ID == "" || first.URL != "https://checkout.example.test/mock_checkout_1" {
		t.Fatalf("checkout replay = %#v, first = %#v, err = %v", replay, first, err)
	}
	if status, err := provider.GetCheckoutSessionStatus(ctx, first.ID); err != nil || status != "completed" {
		t.Fatalf("checkout status = %q, err = %v", status, err)
	}
	if _, err := provider.GetCheckoutSessionStatus(ctx, "missing"); err == nil {
		t.Fatal("expected unknown checkout to fail")
	}
	if got, err := provider.CreateCustomerPortalSession(ctx, "cus_1", "https://app.example.test"); err != nil || got != "https://checkout.example.test/portal/cus_1" {
		t.Fatalf("portal URL = %q, err = %v", got, err)
	}
	if got, err := provider.CreateUpdatePaymentMethodSession(ctx, "cus_1", "sub_1", "https://app.example.test"); err != nil || got != "https://checkout.example.test/subscriptions/sub_1/payment-method" {
		t.Fatalf("payment method URL = %q, err = %v", got, err)
	}
	if got, err := provider.CreatePaymentMethodSetupSession(ctx, "cus_1", "https://app.example.test", "https://app.example.test/cancel"); err != nil || got != "https://checkout.example.test/customers/cus_1/payment-method" {
		t.Fatalf("setup URL = %q, err = %v", got, err)
	}
	customer, err := provider.CreateCustomer(ctx, bursar.CreateCustomerRequest{Email: "buyer@example.com", Name: "Buyer", IdempotencyKey: "customer-1"})
	if err != nil || customer == "" {
		t.Fatalf("customer = %q, err = %v", customer, err)
	}
	if err := provider.CancelSubscription(ctx, "sub_1", "cancel-1"); err != nil {
		t.Fatal(err)
	}
	if err := provider.ReactivateSubscription(ctx, "sub_1", "reactivate-1"); err != nil {
		t.Fatal(err)
	}
	if err := provider.CancelScheduledPlanChange(ctx, "sub_1", "schedule-1", "schedule-cancel-1"); err != nil {
		t.Fatal(err)
	}
	if got, err := provider.GetInvoiceURL(ctx, "in_1"); err != nil || got != "https://checkout.example.test/invoices/in_1" {
		t.Fatalf("invoice URL = %q, err = %v", got, err)
	}
	methods, err := provider.ListPaymentMethods(ctx, customer)
	if err != nil || len(methods) != 1 || methods[0].Last4 != "4242" {
		t.Fatalf("payment methods = %#v, err = %v", methods, err)
	}
	chargeParams := bursar.SavedPaymentChargeParams{CustomerID: customer, PaymentMethodID: "pm_1", ProductID: "prod_1", Quantity: 1, IdempotencyKey: "charge-1"}
	if quote, err := provider.PreviewSavedPaymentCharge(ctx, chargeParams); err != nil || quote.Currency != "USD" {
		t.Fatalf("saved-payment quote = %#v, err = %v", quote, err)
	}
	if charge, err := provider.ChargeSavedPaymentMethod(ctx, chargeParams); err != nil || charge.ProviderPaymentID == "" {
		t.Fatalf("saved-payment charge = %#v, err = %v", charge, err)
	}
	planParams := bursar.ProviderPlanChangeRequest{ProviderSubscriptionID: "sub_1", ProductID: "prod_2", Quantity: 1, IdempotencyKey: "plan-1"}
	if preview, err := provider.PreviewPlanChange(ctx, planParams); err != nil || preview.Currency != "USD" {
		t.Fatalf("plan preview = %#v, err = %v", preview, err)
	}
	if result, err := provider.ChangePlan(ctx, planParams); err != nil || result.ProviderOperationID == "" {
		t.Fatalf("plan change = %#v, err = %v", result, err)
	}

	for _, invalid := range []func() error{
		func() error { _, err := provider.CreateCheckoutSession(ctx, bursar.CheckoutSessionRequest{}); return err },
		func() error { _, err := provider.CreateCustomerPortalSession(ctx, "", "return"); return err },
		func() error { _, err := provider.CreatePaymentMethodSetupSession(ctx, "cus", "return", ""); return err },
		func() error { return provider.CancelSubscription(ctx, "", "key") },
		func() error { _, err := provider.PreviewSavedPaymentCharge(ctx, bursar.SavedPaymentChargeParams{}); return err },
		func() error { _, err := provider.PreviewPlanChange(ctx, bursar.ProviderPlanChangeRequest{}); return err },
	} {
		if err := invalid(); err == nil {
			t.Fatal("expected validation error")
		}
	}
}

func TestProviderNilAndInputEdges(t *testing.T) {
	ctx := t.Context()
	var nilProvider *Provider
	if nilProvider.Name() != ProviderName || nilProvider.ProviderEnvironment() != "" {
		t.Fatal("nil provider metadata should remain safe")
	}
	if _, err := nilProvider.CreateCheckoutSession(ctx, bursar.CheckoutSessionRequest{}); err == nil {
		t.Fatal("nil checkout provider should fail")
	}
	if _, err := nilProvider.GetCheckoutSessionStatus(ctx, "id"); err == nil {
		t.Fatal("nil checkout status provider should fail")
	}
	if _, err := nilProvider.CreateCustomerPortalSession(ctx, "cus", "return"); err == nil {
		t.Fatal("nil portal provider should fail")
	}
	if _, err := nilProvider.CreateUpdatePaymentMethodSession(ctx, "cus", "sub", "return"); err == nil {
		t.Fatal("nil payment portal provider should fail")
	}
	if _, err := nilProvider.CreatePaymentMethodSetupSession(ctx, "cus", "return", "cancel"); err == nil {
		t.Fatal("nil setup provider should fail")
	}
	if _, err := nilProvider.CreateCustomer(ctx, bursar.CreateCustomerRequest{}); err == nil {
		t.Fatal("nil customer provider should fail")
	}
	if err := nilProvider.CancelSubscription(ctx, "sub", "key"); err != nil {
		t.Fatal(err)
	}
	if err := nilProvider.ReactivateSubscription(ctx, "sub", "key"); err != nil {
		t.Fatal(err)
	}
	if _, err := nilProvider.GetInvoiceURL(ctx, "invoice"); err == nil {
		t.Fatal("nil invoice provider should fail")
	}
	if _, err := nilProvider.ListPaymentMethods(ctx, "cus"); err == nil {
		t.Fatal("nil payment methods provider should fail")
	}
	if _, err := nilProvider.PreviewSavedPaymentCharge(ctx, bursar.SavedPaymentChargeParams{}); err == nil {
		t.Fatal("nil saved payment preview should fail")
	}
	if _, err := nilProvider.ChargeSavedPaymentMethod(ctx, bursar.SavedPaymentChargeParams{}); err == nil {
		t.Fatal("nil saved payment charge should fail")
	}
	if _, err := nilProvider.PreviewPlanChange(ctx, bursar.ProviderPlanChangeRequest{}); err == nil {
		t.Fatal("nil plan preview should fail")
	}
	if _, err := nilProvider.ChangePlan(ctx, bursar.ProviderPlanChangeRequest{}); err == nil {
		t.Fatal("nil plan change should fail")
	}
	if err := nilProvider.CancelScheduledPlanChange(ctx, "sub", "schedule", "key"); err != nil {
		t.Fatal(err)
	}
	if err := nilProvider.QueueEvent(bursar.BillingEvent{}); err == nil {
		t.Fatal("nil event provider should fail")
	}
	if _, err := nilProvider.HandleWebhook(ctx, bursar.WebhookRequest{}); err == nil {
		t.Fatal("nil webhook provider should fail")
	}

	provider := New(Options{})
	for _, invalid := range []func() error{
		func() error { _, err := provider.GetInvoiceURL(ctx, ""); return err },
		func() error { _, err := provider.CreateCustomerPortalSession(ctx, "", "return"); return err },
		func() error { _, err := provider.ListPaymentMethods(ctx, ""); return err },
		func() error { _, err := provider.PreviewPlanChange(ctx, bursar.ProviderPlanChangeRequest{ProviderSubscriptionID: "sub", ProductID: "prod", Quantity: 0}); return err },
		func() error { _, err := provider.ChangePlan(ctx, bursar.ProviderPlanChangeRequest{ProviderSubscriptionID: "sub", ProductID: "prod", Quantity: 1}); return err },
		func() error { _, err := provider.PreviewSavedPaymentCharge(ctx, bursar.SavedPaymentChargeParams{CustomerID: "cus", PaymentMethodID: "pm", ProductID: "prod", Quantity: 0}); return err },
		func() error { _, err := provider.ChargeSavedPaymentMethod(ctx, bursar.SavedPaymentChargeParams{CustomerID: "cus", PaymentMethodID: "pm", ProductID: "prod", Quantity: 1}); return err },
		func() error { return provider.CancelScheduledPlanChange(ctx, "", "schedule", "key") },
		func() error { return provider.QueueEvent(bursar.BillingEvent{EventID: "bad"}) },
	} {
		if err := invalid(); err == nil {
			t.Fatal("expected input validation error")
		}
	}
	if _, err := provider.HandleWebhook(ctx, bursar.WebhookRequest{}); err == nil {
		t.Fatal("expected webhook header validation error")
	}
	if _, err := provider.HandleWebhook(ctx, bursar.WebhookRequest{Header: http.Header{"X-Bursar-Mock-Event": []string{"missing"}}}); err == nil {
		t.Fatal("expected missing queued event error")
	}
}
