// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package dodo

import (
	"io"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync"
	"testing"

	bursar "github.com/Zonastery/bursar/golang/v2"
	dodopayments "github.com/dodopayments/dodopayments-go"
	"github.com/dodopayments/dodopayments-go/option"
)

type dodoFixture struct {
	mu                    sync.Mutex
	requests              []string
	status                int
	incomplete            bool
	previewResponse       string
	chargeSessionResponse string
	paymentResponse       string
}

func (f *dodoFixture) handler(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	f.requests = append(f.requests, r.Method+" "+r.URL.Path)
	f.mu.Unlock()
	body, _ := io.ReadAll(r.Body)
	w.Header().Set("Content-Type", "application/json")
	if f.status != 0 {
		w.WriteHeader(f.status)
		_, _ = w.Write([]byte(`{"message":"provider unavailable"}`))
		return
	}
	if f.incomplete {
		writeDodoJSON(w, `{}`)
		return
	}
	switch {
	case r.Method == http.MethodPost && r.URL.Path == "/checkouts":
		if f.chargeSessionResponse != "" && strings.Contains(string(body), `"confirm":true`) {
			writeDodoJSON(w, f.chargeSessionResponse)
		} else {
			writeDodoJSON(w, `{"session_id":"sess_1","checkout_url":"https://checkout.dodo.test/sess_1","payment_id":"pay_1"}`)
		}
	case r.Method == http.MethodGet && strings.HasPrefix(r.URL.Path, "/checkouts/"):
		writeDodoJSON(w, `{"id":"sess_1","created_at":"2026-07-18T05:15:24Z","payment_id":"pay_1","payment_status":"succeeded"}`)
	case r.Method == http.MethodPost && strings.HasSuffix(r.URL.Path, "/customer-portal/session"):
		writeDodoJSON(w, `{"link":"https://portal.dodo.test/session"}`)
	case r.Method == http.MethodPost && strings.HasSuffix(r.URL.Path, "/update-payment-method"):
		writeDodoJSON(w, `{"payment_link":"https://checkout.dodo.test/update"}`)
	case r.Method == http.MethodPost && r.URL.Path == "/customers":
		writeDodoJSON(w, `{"business_id":"bus_1","customer_id":"cus_1","email":"buyer@example.com","name":"Buyer","created_at":"2026-07-18T05:15:24Z","metadata":{}}`)
	case r.Method == http.MethodGet && strings.HasSuffix(r.URL.Path, "/payment-methods"):
		writeDodoJSON(w, `{"items":[{"payment_method":"card","payment_method_id":"pm_1","recurring_enabled":true,"card":{"card_network":"visa","last4_digits":"4242","expiry_month":"12","expiry_year":"2099"}},{"payment_method":"wallet","payment_method_id":"wallet_1","recurring_enabled":true},{"payment_method":"card","payment_method_id":"pm_duplicate","recurring_enabled":true,"card":{"card_network":"visa","last4_digits":"4242","expiry_month":"12","expiry_year":"2099"}}]}`)
	case r.Method == http.MethodPost && r.URL.Path == "/checkouts/preview":
		if f.previewResponse != "" {
			writeDodoJSON(w, f.previewResponse)
		} else {
			writeDodoJSON(w, `{"currency":"USD","current_breakup":{"total_amount":2000,"tax":200}}`)
		}
	case r.Method == http.MethodGet && strings.HasPrefix(r.URL.Path, "/payments/"):
		if f.paymentResponse != "" {
			writeDodoJSON(w, f.paymentResponse)
		} else {
			writeDodoJSON(w, `{"payment_id":"pay_1","currency":"USD","status":"succeeded","total_amount":2000,"tax":200,"invoice_url":"https://billing.dodo.test/pay_1"}`)
		}
	case r.Method == http.MethodPost && strings.HasSuffix(r.URL.Path, "/change-plan/preview"):
		writeDodoJSON(w, dodoPlanPreviewJSON)
	case r.Method == http.MethodPatch:
		writeDodoJSON(w, `{}`)
	case r.Method == http.MethodPost || r.Method == http.MethodDelete:
		w.WriteHeader(http.StatusNoContent)
	default:
		writeDodoJSON(w, `{}`)
	}
}

const dodoPlanPreviewJSON = `{"immediate_charge":{"effective_at":"2026-07-18T05:15:24Z","line_items":[{"id":"line_1","currency":"USD","tax_inclusive":true,"type":"subscription","name":"Pro","product_id":"prod_1","proration_factor":1,"quantity":2,"tax":100,"unit_price":1000},{"id":"addon_1","currency":"USD","type":"addon","product_id":"addon"}],"summary":{"currency":"USD","customer_credits":50,"settlement_amount":2050,"settlement_currency":"USD","total_amount":2200,"settlement_tax":100,"tax":100}},"new_plan":{"currency":"USD","recurring_pre_tax_amount":2000,"next_billing_date":"2026-08-18T05:15:24Z"}}`

func writeDodoJSON(w http.ResponseWriter, payload string) {
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write([]byte(payload))
}

func newDodoFixtureProvider(t *testing.T, fixture *dodoFixture) *Provider {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(fixture.handler))
	t.Cleanup(server.Close)
	client := dodopayments.NewClient(
		option.WithBearerToken("test"),
		option.WithWebhookKey(dodoTestWebhookKey),
		option.WithEnvironmentTestMode(),
		option.WithMaxRetries(0),
		option.WithBaseURL(server.URL),
		option.WithHTTPClient(server.Client()),
	)
	provider, err := New(Options{Client: client, WebhookKey: dodoTestWebhookKey, SetupProductID: "setup_prod", Environment: bursar.ProviderEnvironmentTest})
	if err != nil {
		t.Fatal(err)
	}
	return provider
}

func TestDodoProviderRejectsIncompleteOfficialResponses(t *testing.T) {
	p := newDodoFixtureProvider(t, &dodoFixture{incomplete: true})
	ctx := t.Context()
	checkout := bursar.CheckoutSessionRequest{AccountID: "acct", ProductID: "prod", Quantity: 1, SuccessURL: "success", CancelURL: "cancel", IdempotencyKey: "checkout"}
	if _, err := p.CreateCheckoutSession(ctx, checkout); err == nil {
		t.Fatal("expected incomplete checkout error")
	}
	if _, err := p.CreatePaymentMethodSetupSession(ctx, "cus", "return", "cancel"); err == nil {
		t.Fatal("expected incomplete setup error")
	}
	if _, err := p.CreateCustomer(ctx, bursar.CreateCustomerRequest{Email: "buyer@example.com", Name: "Buyer", IdempotencyKey: "customer"}); err == nil {
		t.Fatal("expected incomplete customer error")
	}
	if _, err := p.CreateCustomerPortalSession(ctx, "cus", "return"); err == nil {
		t.Fatal("expected incomplete portal error")
	}
	if _, err := p.CreateUpdatePaymentMethodSession(ctx, "cus", "sub", "return"); err == nil {
		t.Fatal("expected incomplete update-payment-method error")
	}
	if _, err := p.PreviewSavedPaymentCharge(ctx, bursar.SavedPaymentChargeParams{CustomerID: "cus", ProductID: "prod", Quantity: 1}); err == nil {
		t.Fatal("expected incomplete saved preview error")
	}
	charge := bursar.SavedPaymentChargeParams{CustomerID: "cus", PaymentMethodID: "pm", ProductID: "prod", Quantity: 1, ReturnURL: "return", IdempotencyKey: "charge"}
	if _, err := p.ChargeSavedPaymentMethod(ctx, charge); err == nil {
		t.Fatal("expected incomplete saved charge error")
	}
	if invoiceURL, err := p.GetInvoiceURL(ctx, "pay"); err != nil || invoiceURL != "" {
		t.Fatalf("incomplete invoice = %q, err = %v", invoiceURL, err)
	}
	if _, err := p.PreviewPlanChange(ctx, bursar.ProviderPlanChangeRequest{ProviderSubscriptionID: "sub", ProductID: "prod", Quantity: 1}); err == nil {
		t.Fatal("expected incomplete plan preview error")
	}
}

func TestDodoProviderRejectsMissingOfficialServices(t *testing.T) {
	ctx := t.Context()
	if (&Provider{}).Name() != ProviderName {
		t.Fatal("unexpected provider name")
	}
	checkout := bursar.CheckoutSessionRequest{AccountID: "acct", ProductID: "prod", Quantity: 1, SuccessURL: "success", CancelURL: "cancel", IdempotencyKey: "key"}
	p := newDodoFixtureProvider(t, &dodoFixture{})
	p.client.CheckoutSessions = nil
	if _, err := p.CreateCheckoutSession(ctx, checkout); err == nil {
		t.Fatal("expected missing checkout service error")
	}
	p = newDodoFixtureProvider(t, &dodoFixture{})
	p.client.CheckoutSessions = nil
	if _, err := p.GetCheckoutSessionStatus(ctx, "sess"); err == nil {
		t.Fatal("expected missing checkout status service error")
	}
	p = newDodoFixtureProvider(t, &dodoFixture{})
	p.client.CheckoutSessions = nil
	if _, err := p.CreatePaymentMethodSetupSession(ctx, "cus", "return", "cancel"); err == nil {
		t.Fatal("expected missing setup service error")
	}
	p = newDodoFixtureProvider(t, &dodoFixture{})
	p.client.CheckoutSessions = nil
	if _, err := p.PreviewSavedPaymentCharge(ctx, bursar.SavedPaymentChargeParams{CustomerID: "cus", ProductID: "prod", Quantity: 1}); err == nil {
		t.Fatal("expected missing preview service error")
	}
	p = newDodoFixtureProvider(t, &dodoFixture{})
	p.client.Customers = nil
	if _, err := p.CreateCustomer(ctx, bursar.CreateCustomerRequest{Email: "a@b.test", Name: "Buyer", IdempotencyKey: "key"}); err == nil {
		t.Fatal("expected missing customer service error")
	}
	p = newDodoFixtureProvider(t, &dodoFixture{})
	p.client.Customers = nil
	if _, err := p.CreateCustomerPortalSession(ctx, "cus", "return"); err == nil {
		t.Fatal("expected missing portal service error")
	}
	p = newDodoFixtureProvider(t, &dodoFixture{})
	p.client.Customers = nil
	if _, err := p.ListPaymentMethods(ctx, "cus"); err == nil {
		t.Fatal("expected missing payment-method service error")
	}
	p = newDodoFixtureProvider(t, &dodoFixture{})
	p.client.Customers.CustomerPortal = nil
	if _, err := p.CreateCustomerPortalSession(ctx, "cus", "return"); err == nil {
		t.Fatal("expected missing customer portal service error")
	}
	p = newDodoFixtureProvider(t, &dodoFixture{})
	p.client.Subscriptions = nil
	if _, err := p.CreateUpdatePaymentMethodSession(ctx, "cus", "sub", "return"); err == nil {
		t.Fatal("expected missing subscription service error")
	}
	p = newDodoFixtureProvider(t, &dodoFixture{})
	p.client.Subscriptions = nil
	if err := p.CancelSubscription(ctx, "sub", "key"); err == nil {
		t.Fatal("expected missing cancel service error")
	}
	p = newDodoFixtureProvider(t, &dodoFixture{})
	p.client.Subscriptions = nil
	if err := p.ReactivateSubscription(ctx, "sub", "key"); err == nil {
		t.Fatal("expected missing reactivate service error")
	}
	p = newDodoFixtureProvider(t, &dodoFixture{})
	p.client.Subscriptions = nil
	if err := p.CancelScheduledPlanChange(ctx, "sub", "operation", "key"); err == nil {
		t.Fatal("expected missing scheduled cancellation service error")
	}
	p = newDodoFixtureProvider(t, &dodoFixture{})
	p.client.Subscriptions = nil
	if _, err := p.ChangePlan(ctx, bursar.ProviderPlanChangeRequest{ProviderSubscriptionID: "sub", ProductID: "prod", Quantity: 1, IdempotencyKey: "key"}); err == nil {
		t.Fatal("expected missing plan service error")
	}
	p = newDodoFixtureProvider(t, &dodoFixture{})
	p.client.Subscriptions = nil
	if _, err := p.PreviewPlanChange(ctx, bursar.ProviderPlanChangeRequest{ProviderSubscriptionID: "sub", ProductID: "prod", Quantity: 1}); err == nil {
		t.Fatal("expected missing plan preview service error")
	}
	p = newDodoFixtureProvider(t, &dodoFixture{})
	p.client.Payments = nil
	if _, err := p.GetInvoiceURL(ctx, "pay"); err == nil {
		t.Fatal("expected missing payment service error")
	}
	p = newDodoFixtureProvider(t, &dodoFixture{})
	p.client.Payments = nil
	if _, err := p.ChargeSavedPaymentMethod(ctx, bursar.SavedPaymentChargeParams{CustomerID: "cus", PaymentMethodID: "pm", ProductID: "prod", Quantity: 1, ReturnURL: "return", IdempotencyKey: "key"}); err == nil {
		t.Fatal("expected missing charge payment service error")
	}
	p = newDodoFixtureProvider(t, &dodoFixture{})
	p.client.Webhooks = nil
	if _, err := p.HandleWebhook(ctx, bursar.WebhookRequest{RawBody: []byte(`{}`)}); err == nil {
		t.Fatal("expected missing webhook service error")
	}
	p = newDodoFixtureProvider(t, &dodoFixture{})
	p.setupProductID = ""
	if _, err := p.CreatePaymentMethodSetupSession(ctx, "cus", "return", "cancel"); err == nil {
		t.Fatal("expected missing setup product error")
	}
}

func TestDodoProviderRejectsInvalidSavedPaymentResponses(t *testing.T) {
	ctx := t.Context()
	params := bursar.SavedPaymentChargeParams{CustomerID: "cus", PaymentMethodID: "pm", ProductID: "prod", Quantity: 1, ReturnURL: "return", IdempotencyKey: "charge"}
	for _, response := range []struct {
		preview, payment string
		wantPreview      bool
	}{
		{preview: `{"currency":"","current_breakup":{"total_amount":1}}`, wantPreview: true},
		{preview: `{"currency":"USD","current_breakup":{"total_amount":-1}}`, wantPreview: true},
		{preview: `{"currency":"USD","current_breakup":{"total_amount":1,"tax":-1}}`, wantPreview: true},
		{payment: `{"payment_id":"pay_1","currency":"USD","status":"succeeded","total_amount":-1}`},
		{payment: `{"payment_id":"pay_1","currency":"","status":"succeeded","total_amount":1}`},
		{payment: `{"payment_id":"pay_1","currency":"USD","status":"unknown","total_amount":1}`},
	} {
		fixture := &dodoFixture{previewResponse: response.preview, paymentResponse: response.payment}
		if response.payment != "" {
			fixture.chargeSessionResponse = `{"session_id":"sess_1","payment_id":"pay_1"}`
		}
		p := newDodoFixtureProvider(t, fixture)
		if response.wantPreview {
			if _, err := p.PreviewSavedPaymentCharge(ctx, bursar.SavedPaymentChargeParams{CustomerID: "cus", ProductID: "prod", Quantity: 1}); err == nil {
				t.Fatal("expected invalid saved payment preview response")
			}
		} else if _, err := p.ChargeSavedPaymentMethod(ctx, params); err == nil {
			t.Fatal("expected invalid saved payment charge response")
		}
	}
}

func TestDodoProviderRealWorldHTTPFlows(t *testing.T) {
	fixture := &dodoFixture{}
	provider := newDodoFixtureProvider(t, fixture)
	ctx := t.Context()
	checkout, err := provider.CreateCheckoutSession(ctx, bursar.CheckoutSessionRequest{AccountID: "acct_1", ProductID: "prod_1", Quantity: 2, CustomerID: "cus_1", SuccessURL: "https://app.test/success", CancelURL: "https://app.test/cancel", IdempotencyKey: "checkout-1", Metadata: map[string]string{"plan": "pro"}})
	if err != nil || checkout.ID != "sess_1" || checkout.URL == "" {
		t.Fatalf("checkout = %#v, err = %v", checkout, err)
	}
	if status, err := provider.GetCheckoutSessionStatus(ctx, checkout.ID); err != nil || status != "succeeded" {
		t.Fatalf("checkout status = %q, err = %v", status, err)
	}
	if got, err := provider.CreateCustomerPortalSession(ctx, "cus_1", "https://app.test"); err != nil || got == "" {
		t.Fatalf("portal = %q, err = %v", got, err)
	}
	if got, err := provider.CreateUpdatePaymentMethodSession(ctx, "cus_1", "sub_1", "https://app.test"); err != nil || got == "" {
		t.Fatalf("update payment method = %q, err = %v", got, err)
	}
	if got, err := provider.CreatePaymentMethodSetupSession(ctx, "cus_1", "https://app.test", ""); err != nil || got == "" {
		t.Fatalf("setup payment method = %q, err = %v", got, err)
	}
	if customer, err := provider.CreateCustomer(ctx, bursar.CreateCustomerRequest{Email: "buyer@example.com", Name: "Buyer", IdempotencyKey: "customer-1"}); err != nil || customer != "cus_1" {
		t.Fatalf("customer = %q, err = %v", customer, err)
	}
	if err := provider.CancelSubscription(ctx, "sub_1", "cancel-1"); err != nil {
		t.Fatal(err)
	}
	if err := provider.ReactivateSubscription(ctx, "sub_1", "reactivate-1"); err != nil {
		t.Fatal(err)
	}
	if err := provider.CancelScheduledPlanChange(ctx, "sub_1", "ignored", "scheduled-cancel-1"); err != nil {
		t.Fatal(err)
	}
	methods, err := provider.ListPaymentMethods(ctx, "cus_1")
	if err != nil || len(methods) != 1 || !methods[0].IsDefault || methods[0].Last4 != "4242" {
		t.Fatalf("payment methods = %#v, err = %v", methods, err)
	}
	charge := bursar.SavedPaymentChargeParams{CustomerID: "cus_1", PaymentMethodID: "pm_1", ProductID: "prod_1", Quantity: 2, IdempotencyKey: "charge-1", ReturnURL: "https://app.test/return", Metadata: map[string]string{"purpose": "topup"}}
	if quote, err := provider.PreviewSavedPaymentCharge(ctx, charge); err != nil || quote.AmountMinor != 2000 || quote.TaxMinor == nil || *quote.TaxMinor != 200 {
		t.Fatalf("saved payment quote = %#v, err = %v", quote, err)
	}
	if result, err := provider.ChargeSavedPaymentMethod(ctx, charge); err != nil || result.ProviderPaymentID != "pay_1" || result.Status != bursar.SavedPaymentChargeSucceeded || result.ActionURL == "" {
		t.Fatalf("saved payment charge = %#v, err = %v", result, err)
	}
	if invoiceURL, err := provider.GetInvoiceURL(ctx, "pay_1"); err != nil || invoiceURL == "" {
		t.Fatalf("invoice URL = %q, err = %v", invoiceURL, err)
	}
	plan := bursar.ProviderPlanChangeRequest{ProviderSubscriptionID: "sub_1", ProductID: "prod_2", Quantity: 2, IdempotencyKey: "plan-1", ProrationBillingMode: "difference_immediately", PaymentFailure: "apply_change", Metadata: map[string]string{"plan": "pro"}}
	if preview, err := provider.PreviewPlanChange(ctx, plan); err != nil || preview.TotalAmount.IntPart() != 2200 || len(preview.LineItems) != 1 {
		t.Fatalf("plan preview = %#v, err = %v", preview, err)
	}
	if _, err := provider.ChangePlan(ctx, plan); err != nil {
		t.Fatal(err)
	}
	fixture.mu.Lock()
	requestCount := len(fixture.requests)
	fixture.mu.Unlock()
	if requestCount < 14 {
		t.Fatalf("expected broad official SDK request coverage, got %d", requestCount)
	}
}

func TestDodoProviderValidationAndOfficialErrors(t *testing.T) {
	ctx := t.Context()
	provider := newDodoFixtureProvider(t, &dodoFixture{})
	invalid := []func() error{
		func() error {
			_, err := provider.CreateCheckoutSession(ctx, bursar.CheckoutSessionRequest{})
			return err
		},
		func() error { _, err := provider.GetCheckoutSessionStatus(ctx, ""); return err },
		func() error { _, err := provider.CreateCustomerPortalSession(ctx, "", "return"); return err },
		func() error { _, err := provider.CreateCustomerPortalSession(ctx, "cus", ""); return err },
		func() error {
			_, err := provider.CreateUpdatePaymentMethodSession(ctx, "cus", "", "return")
			return err
		},
		func() error {
			_, err := provider.CreatePaymentMethodSetupSession(ctx, "", "return", "cancel")
			return err
		},
		func() error {
			_, err := provider.CreateCustomer(ctx, bursar.CreateCustomerRequest{Email: "buyer", Name: "Buyer"})
			return err
		},
		func() error { return provider.CancelSubscription(ctx, "sub", "") },
		func() error { return provider.ReactivateSubscription(ctx, "", "key") },
		func() error { return provider.CancelScheduledPlanChange(ctx, "sub", "operation", "") },
		func() error { _, err := provider.ListPaymentMethods(ctx, ""); return err },
		func() error {
			_, err := provider.PreviewSavedPaymentCharge(ctx, bursar.SavedPaymentChargeParams{})
			return err
		},
		func() error {
			_, err := provider.PreviewPlanChange(ctx, bursar.ProviderPlanChangeRequest{})
			return err
		},
		func() error { _, err := provider.GetInvoiceURL(ctx, ""); return err },
	}
	for _, call := range invalid {
		if err := call(); err == nil {
			t.Fatal("expected validation error")
		}
	}
	var nilProvider *Provider
	if _, err := nilProvider.CreateCheckoutSession(ctx, bursar.CheckoutSessionRequest{}); err == nil {
		t.Fatal("expected nil provider error")
	}
	if _, err := nilProvider.ChargeSavedPaymentMethod(ctx, bursar.SavedPaymentChargeParams{}); err == nil {
		t.Fatal("expected nil charge error")
	}
	if nilProvider.ProviderEnvironment() != "" {
		t.Fatal("nil provider environment should be empty")
	}
	for _, status := range []int{404, 409, 429, 500} {
		t.Run(strconv.Itoa(status), func(t *testing.T) {
			p := newDodoFixtureProvider(t, &dodoFixture{status: status})
			if _, err := p.CreateCheckoutSession(ctx, bursar.CheckoutSessionRequest{AccountID: "acct", ProductID: "prod", Quantity: 1, SuccessURL: "success", CancelURL: "cancel", IdempotencyKey: "checkout"}); err == nil {
				t.Fatal("expected provider checkout error")
			}
			if _, err := p.GetCheckoutSessionStatus(ctx, "sess"); err == nil {
				t.Fatal("expected provider status error")
			}
			if _, err := p.PreviewSavedPaymentCharge(ctx, bursar.SavedPaymentChargeParams{CustomerID: "cus", ProductID: "prod", Quantity: 1}); err == nil {
				t.Fatal("expected provider preview error")
			}
			if _, err := p.CreateCustomerPortalSession(ctx, "cus", "return"); err == nil {
				t.Fatal("expected provider portal error")
			}
			if _, err := p.CreateUpdatePaymentMethodSession(ctx, "cus", "sub", "return"); err == nil {
				t.Fatal("expected provider update-payment-method error")
			}
			if _, err := p.CreatePaymentMethodSetupSession(ctx, "cus", "return", "cancel"); err == nil {
				t.Fatal("expected provider setup error")
			}
			if _, err := p.CreateCustomer(ctx, bursar.CreateCustomerRequest{Email: "buyer@example.com", Name: "Buyer", IdempotencyKey: "customer"}); err == nil {
				t.Fatal("expected provider customer error")
			}
			if err := p.CancelSubscription(ctx, "sub", "cancel"); err == nil {
				t.Fatal("expected provider cancel error")
			}
			if err := p.ReactivateSubscription(ctx, "sub", "reactivate"); err == nil {
				t.Fatal("expected provider reactivate error")
			}
			if err := p.CancelScheduledPlanChange(ctx, "sub", "operation", "scheduled"); err == nil {
				t.Fatal("expected provider scheduled-cancel error")
			}
			if _, err := p.ListPaymentMethods(ctx, "cus"); err == nil {
				t.Fatal("expected provider payment-method error")
			}
			charge := bursar.SavedPaymentChargeParams{CustomerID: "cus", PaymentMethodID: "pm", ProductID: "prod", Quantity: 1, ReturnURL: "return", IdempotencyKey: "charge"}
			if _, err := p.ChargeSavedPaymentMethod(ctx, charge); err == nil {
				t.Fatal("expected provider charge error")
			}
			if _, err := p.GetInvoiceURL(ctx, "pay"); err == nil {
				t.Fatal("expected provider invoice error")
			}
			plan := bursar.ProviderPlanChangeRequest{ProviderSubscriptionID: "sub", ProductID: "prod", Quantity: 1, IdempotencyKey: "plan"}
			if _, err := p.PreviewPlanChange(ctx, plan); err == nil {
				t.Fatal("expected provider plan preview error")
			}
			if _, err := p.ChangePlan(ctx, plan); err == nil {
				t.Fatal("expected provider plan change error")
			}
		})
	}
}
