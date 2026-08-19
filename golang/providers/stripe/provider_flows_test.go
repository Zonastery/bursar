// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package stripe

import (
	"io"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync"
	"testing"

	bursar "github.com/Zonastery/bursar/golang/v2"
	stripego "github.com/stripe/stripe-go/v84"
)

type stripeFixture struct {
	mu               sync.Mutex
	requests         []string
	scheduleResponse string
}

func (f *stripeFixture) handler(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	f.requests = append(f.requests, r.Method+" "+r.URL.Path)
	f.mu.Unlock()
	_, _ = io.ReadAll(r.Body)
	w.Header().Set("Content-Type", "application/json")
	if r.URL.Path == "/v1/payment_methods" {
		writeStripeJSON(w, `{"object":"list","has_more":false,"data":[{"id":"pm_1","object":"payment_method","type":"card","card":{"brand":"visa","last4":"4242","exp_month":12,"exp_year":2099}},{"id":"pm_1","object":"payment_method","type":"card","card":{"brand":"visa","last4":"4242","exp_month":12,"exp_year":2099}}]}`)
		return
	}
	switch {
	case r.Method == http.MethodPost && r.URL.Path == "/v1/checkout/sessions":
		writeStripeJSON(w, `{"id":"cs_1","object":"checkout.session","url":"https://checkout.stripe.test/cs_1","customer":"cus_1","status":"open","payment_status":"unpaid"}`)
	case r.Method == http.MethodGet && strings.HasPrefix(r.URL.Path, "/v1/checkout/sessions/"):
		writeStripeJSON(w, `{"id":"cs_1","object":"checkout.session","status":"complete","payment_status":"paid"}`)
	case r.Method == http.MethodPost && r.URL.Path == "/v1/billing_portal/sessions":
		writeStripeJSON(w, `{"id":"bps_1","object":"billing_portal.session","url":"https://billing.stripe.test/bps_1"}`)
	case r.Method == http.MethodPost && r.URL.Path == "/v1/customers":
		writeStripeJSON(w, `{"id":"cus_1","object":"customer","email":"buyer@example.com","name":"Buyer"}`)
	case r.Method == http.MethodGet && strings.HasPrefix(r.URL.Path, "/v1/customers/"):
		writeStripeJSON(w, `{"id":"cus_1","object":"customer","invoice_settings":{"default_payment_method":"pm_1"}}`)
	case r.Method == http.MethodGet && strings.HasPrefix(r.URL.Path, "/v1/prices/"):
		writeStripeJSON(w, `{"id":"price_1","object":"price","active":true,"billing_scheme":"per_unit","currency":"usd","unit_amount":1000,"product":"prod_1"}`)
	case r.Method == http.MethodPost && r.URL.Path == "/v1/payment_intents":
		writeStripeJSON(w, `{"id":"pi_1","object":"payment_intent","status":"succeeded","amount":2000,"currency":"usd"}`)
	case r.Method == http.MethodGet && strings.HasPrefix(r.URL.Path, "/v1/payment_intents/"):
		writeStripeJSON(w, `{"id":"pi_1","object":"payment_intent","status":"succeeded","amount":2000,"currency":"usd"}`)
	case r.Method == http.MethodGet && strings.HasPrefix(r.URL.Path, "/v1/invoices/"):
		writeStripeJSON(w, `{"id":"in_1","object":"invoice","hosted_invoice_url":"https://billing.stripe.test/in_1","currency":"usd","total":1200,"amount_due":1200,"created":1784351724}`)
	case r.Method == http.MethodPost && r.URL.Path == "/v1/invoices/create_preview":
		writeStripeJSON(w, `{"id":"upcoming_in_1","object":"invoice","currency":"usd","total":1200,"amount_due":1200,"created":1784351724,"lines":{"object":"list","has_more":false,"data":[{"id":"il_1","description":"Pro","currency":"usd","quantity":1,"subtotal":1000,"parent":{"type":"subscription_item_details","subscription_item_details":{"subscription_item":"si_1"}},"pricing":{"type":"price_details","price_details":{"price":"price_1"}},"taxes":[{"amount":200}]}]}}`)
	case r.Method == http.MethodGet && strings.HasPrefix(r.URL.Path, "/v1/subscriptions/"):
		writeStripeJSON(w, `{"id":"sub_1","object":"subscription","customer":"cus_1","items":{"object":"list","data":[{"id":"si_1","quantity":1,"current_period_end":1787020124,"price":{"id":"price_old","product":"prod_old","unit_amount":1000,"currency":"usd"}}]}}`)
	case r.Method == http.MethodPost && r.URL.Path == "/v1/subscriptions/sub_1":
		writeStripeJSON(w, `{"id":"sub_1","object":"subscription","latest_invoice":"in_1","items":{"object":"list","data":[{"id":"si_1","quantity":1,"current_period_end":1787020124,"price":{"id":"price_1","product":"prod_1","unit_amount":1000,"currency":"usd"}}]}}`)
	case r.Method == http.MethodPost && r.URL.Path == "/v1/subscription_schedules":
		if f.scheduleResponse != "" {
			writeStripeJSON(w, f.scheduleResponse)
		} else {
			writeStripeJSON(w, `{"id":"sched_1","object":"subscription_schedule","phases":[{"start_date":1784351724,"end_date":1787020124,"items":[{"price":"price_old","quantity":1}]}]}`)
		}
	case r.Method == http.MethodPost && r.URL.Path == "/v1/subscription_schedules/sched_1":
		writeStripeJSON(w, `{"id":"sched_1","object":"subscription_schedule"}`)
	case r.Method == http.MethodPost && r.URL.Path == "/v1/subscription_schedules/sched_1/release":
		writeStripeJSON(w, `{"id":"sched_1","object":"subscription_schedule"}`)
	default:
		writeStripeJSON(w, `{"id":"ok","object":"response"}`)
	}
}

func writeStripeJSON(w http.ResponseWriter, payload string) {
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write([]byte(payload))
}

func newStripeFixtureProvider(t *testing.T, fixture *stripeFixture) *Provider {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(fixture.handler))
	t.Cleanup(server.Close)
	client := stripego.NewClient("sk_test", stripego.WithBackends(stripego.NewBackendsWithConfig(&stripego.BackendConfig{
		HTTPClient:        server.Client(),
		URL:               stripego.String(server.URL),
		MaxNetworkRetries: stripego.Int64(0),
	})))
	provider, err := New(Options{Client: client, WebhookSecret: "whsec_test", Environment: bursar.ProviderEnvironmentTest})
	if err != nil {
		t.Fatal(err)
	}
	return provider
}

func newStripeErrorProvider(t *testing.T, status int) *Provider {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_, _ = w.Write([]byte(`{"error":{"type":"api_error","message":"provider unavailable"}}`))
	}))
	t.Cleanup(server.Close)
	client := stripego.NewClient("sk_test", stripego.WithBackends(stripego.NewBackendsWithConfig(&stripego.BackendConfig{
		HTTPClient:        server.Client(),
		URL:               stripego.String(server.URL),
		MaxNetworkRetries: stripego.Int64(0),
	})))
	provider, err := New(Options{Client: client, WebhookSecret: "whsec_test"})
	if err != nil {
		t.Fatal(err)
	}
	return provider
}

func newStripeIncompleteProvider(t *testing.T) *Provider {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{}`))
	}))
	t.Cleanup(server.Close)
	client := stripego.NewClient("sk_test", stripego.WithBackends(stripego.NewBackendsWithConfig(&stripego.BackendConfig{
		HTTPClient:        server.Client(),
		URL:               stripego.String(server.URL),
		MaxNetworkRetries: stripego.Int64(0),
	})))
	provider, err := New(Options{Client: client, WebhookSecret: "whsec_test"})
	if err != nil {
		t.Fatal(err)
	}
	return provider
}

func TestStripeProviderRealWorldHTTPFlows(t *testing.T) {
	fixture := &stripeFixture{}
	provider := newStripeFixtureProvider(t, fixture)
	ctx := t.Context()

	checkout, err := provider.CreateCheckoutSession(ctx, bursar.CheckoutSessionRequest{
		AccountID: "acct_1", ProductID: "price_1", Quantity: 2, CustomerID: "cus_1", CustomerEmail: "buyer@example.com",
		SuccessURL: "https://app.test/success", CancelURL: "https://app.test/cancel", IdempotencyKey: "checkout-1", Mode: "subscription",
		Metadata: map[string]string{"plan": "pro"},
	})
	if err != nil || checkout.ID != "cs_1" || checkout.URL == "" || checkout.CustomerID != "cus_1" {
		t.Fatalf("checkout = %#v, err = %v", checkout, err)
	}
	if status, err := provider.GetCheckoutSessionStatus(ctx, checkout.ID); err != nil || status != "succeeded" {
		t.Fatalf("checkout status = %q, err = %v", status, err)
	}
	if got, err := provider.CreateCustomerPortalSession(ctx, "cus_1", "https://app.test"); err != nil || got == "" {
		t.Fatalf("portal = %q, err = %v", got, err)
	}
	if got, err := provider.CreateUpdatePaymentMethodSession(ctx, "cus_1", "sub_1", "https://app.test"); err != nil || got == "" {
		t.Fatalf("payment-method portal = %q, err = %v", got, err)
	}
	if got, err := provider.CreatePaymentMethodSetupSession(ctx, "cus_1", "https://app.test", ""); err != nil || got == "" {
		t.Fatalf("payment-method setup = %q, err = %v", got, err)
	}
	customer, err := provider.CreateCustomer(ctx, bursar.CreateCustomerRequest{Email: "buyer@example.com", Name: "Buyer", IdempotencyKey: "customer-1"})
	if err != nil || customer != "cus_1" {
		t.Fatalf("customer = %q, err = %v", customer, err)
	}
	if err := provider.CancelSubscription(ctx, "sub_1", "cancel-1"); err != nil {
		t.Fatal(err)
	}
	if err := provider.ReactivateSubscription(ctx, "sub_1", "reactivate-1"); err != nil {
		t.Fatal(err)
	}
	methods, err := provider.ListPaymentMethods(ctx, "cus_1")
	if err != nil || len(methods) != 1 || !methods[0].IsDefault || methods[0].Last4 != "4242" {
		t.Fatalf("payment methods = %#v, err = %v", methods, err)
	}
	charge := bursar.SavedPaymentChargeParams{CustomerID: "cus_1", PaymentMethodID: "pm_1", ProductID: "price_1", Quantity: 2, IdempotencyKey: "charge-1", ReturnURL: "https://app.test/return"}
	quote, err := provider.PreviewSavedPaymentCharge(ctx, charge)
	if err != nil || quote.AmountMinor != 2000 || quote.Currency != "USD" {
		t.Fatalf("quote = %#v, err = %v", quote, err)
	}
	result, err := provider.ChargeSavedPaymentMethod(ctx, charge)
	if err != nil || result.ProviderPaymentID != "pi_1" || result.Status != bursar.SavedPaymentChargeSucceeded || result.AmountMinor == nil || *result.AmountMinor != 2000 {
		t.Fatalf("charge = %#v, err = %v", result, err)
	}
	if got, err := provider.GetInvoiceURL(ctx, "in_1"); err != nil || got == "" {
		t.Fatalf("invoice URL = %q, err = %v", got, err)
	}
	plan := bursar.ProviderPlanChangeRequest{ProviderSubscriptionID: "sub_1", ProductID: "price_1", Quantity: 1, IdempotencyKey: "plan-1", EffectiveAt: "immediately"}
	preview, err := provider.PreviewPlanChange(ctx, plan)
	if err != nil || preview.TotalAmount.IntPart() != 1200 || len(preview.LineItems) != 1 {
		t.Logf("plan preview error details: %#v", err)
		t.Fatalf("plan preview = %#v, err = %v", preview, err)
	}
	changed, err := provider.ChangePlan(ctx, plan)
	if err != nil || changed.ProviderOperationID != "in_1" {
		t.Fatalf("plan change = %#v, err = %v", changed, err)
	}
	next := plan
	next.EffectiveAt = "next_billing_date"
	next.IdempotencyKey = "plan-next-1"
	scheduled, err := provider.ChangePlan(ctx, next)
	if err != nil || scheduled.ProviderOperationID != "sched_1" {
		t.Fatalf("scheduled plan change = %#v, err = %v", scheduled, err)
	}
	if err := provider.CancelScheduledPlanChange(ctx, "sub_1", "sched_1", "schedule-cancel-1"); err != nil {
		t.Fatal(err)
	}

	fixture.mu.Lock()
	requestCount := len(fixture.requests)
	fixture.mu.Unlock()
	if requestCount < 14 {
		t.Fatalf("expected broad official-SDK request coverage, got %d requests", requestCount)
	}
}

func TestStripeProviderValidationAndResponseErrors(t *testing.T) {
	provider := newStripeFixtureProvider(t, &stripeFixture{})
	ctx := t.Context()
	for _, invalid := range []func() error{
		func() error {
			_, err := provider.CreateCheckoutSession(ctx, bursar.CheckoutSessionRequest{})
			return err
		},
		func() error { _, err := provider.GetCheckoutSessionStatus(ctx, ""); return err },
		func() error { _, err := provider.GetInvoiceURL(ctx, ""); return err },
		func() error { _, err := provider.CreateCustomerPortalSession(ctx, "", "return"); return err },
		func() error { _, err := provider.CreateCustomerPortalSession(ctx, "cus", ""); return err },
		func() error {
			_, err := provider.CreateUpdatePaymentMethodSession(ctx, "", "sub", "return")
			return err
		},
		func() error {
			_, err := provider.CreateUpdatePaymentMethodSession(ctx, "cus", "", "return")
			return err
		},
		func() error { _, err := provider.CreateUpdatePaymentMethodSession(ctx, "cus", "sub", ""); return err },
		func() error {
			_, err := provider.CreatePaymentMethodSetupSession(ctx, "", "return", "cancel")
			return err
		},
		func() error {
			_, err := provider.CreatePaymentMethodSetupSession(ctx, "cus", "", "cancel")
			return err
		},
		func() error {
			_, err := provider.CreateCustomer(ctx, bursar.CreateCustomerRequest{Email: "buyer", Name: "Buyer"})
			return err
		},
		func() error {
			_, err := provider.CreateCustomer(ctx, bursar.CreateCustomerRequest{Email: "", Name: "Buyer", IdempotencyKey: "key"})
			return err
		},
		func() error {
			_, err := provider.CreateCustomer(ctx, bursar.CreateCustomerRequest{Email: "buyer", Name: "", IdempotencyKey: "key"})
			return err
		},
		func() error { return provider.CancelSubscription(ctx, "sub", "") },
		func() error { return provider.CancelSubscription(ctx, "", "key") },
		func() error { return provider.ReactivateSubscription(ctx, "", "key") },
		func() error { _, err := provider.ListPaymentMethods(ctx, ""); return err },
		func() error {
			_, err := provider.PreviewSavedPaymentCharge(ctx, bursar.SavedPaymentChargeParams{})
			return err
		},
		func() error {
			_, err := provider.PreviewPlanChange(ctx, bursar.ProviderPlanChangeRequest{})
			return err
		},
		func() error { return provider.CancelScheduledPlanChange(ctx, "sub", "schedule", "") },
		func() error { return provider.CancelScheduledPlanChange(ctx, "", "schedule", "key") },
		func() error { return provider.CancelScheduledPlanChange(ctx, "sub", "", "key") },
	} {
		if err := invalid(); err == nil {
			t.Fatal("expected validation error")
		}
	}
	if _, err := New(Options{WebhookSecret: "secret", Environment: bursar.ProviderEnvironment("staging")}); err == nil {
		t.Fatal("expected invalid environment")
	}
	if _, err := New(Options{WebhookSecret: "secret"}); err == nil {
		t.Fatal("expected missing API key")
	}
	if _, err := New(Options{APIKey: "sk_test"}); err == nil {
		t.Fatal("expected missing webhook secret")
	}

	var nilProvider *Provider
	if _, err := nilProvider.CreateCheckoutSession(ctx, bursar.CheckoutSessionRequest{}); err == nil {
		t.Fatal("expected nil provider error")
	}
	if nilProvider.ProviderEnvironment() != "" {
		t.Fatal("nil provider environment should be empty")
	}
	if _, err := nilProvider.GetInvoiceURL(ctx, "invoice"); err == nil {
		t.Fatal("expected nil invoice error")
	}
	if _, err := nilProvider.GetCheckoutSessionStatus(ctx, "session"); err == nil {
		t.Fatal("expected nil checkout status error")
	}
	if _, err := nilProvider.CreateCustomerPortalSession(ctx, "customer", "return"); err == nil {
		t.Fatal("expected nil portal error")
	}
	if _, err := nilProvider.CreateUpdatePaymentMethodSession(ctx, "customer", "subscription", "return"); err == nil {
		t.Fatal("expected nil payment method portal error")
	}
	if _, err := nilProvider.CreatePaymentMethodSetupSession(ctx, "customer", "return", "cancel"); err == nil {
		t.Fatal("expected nil setup error")
	}
	if _, err := nilProvider.CreateCustomer(ctx, bursar.CreateCustomerRequest{Email: "buyer", Name: "Buyer", IdempotencyKey: "customer"}); err == nil {
		t.Fatal("expected nil customer error")
	}
	if err := nilProvider.CancelSubscription(ctx, "subscription", "cancel"); err == nil {
		t.Fatal("expected nil cancellation error")
	}
	if err := nilProvider.ReactivateSubscription(ctx, "subscription", "reactivate"); err == nil {
		t.Fatal("expected nil reactivation error")
	}
	if _, err := nilProvider.ListPaymentMethods(ctx, "customer"); err == nil {
		t.Fatal("expected nil payment methods error")
	}
	if _, err := nilProvider.PreviewSavedPaymentCharge(ctx, bursar.SavedPaymentChargeParams{}); err == nil {
		t.Fatal("expected nil saved payment preview error")
	}
	if _, err := nilProvider.ChargeSavedPaymentMethod(ctx, bursar.SavedPaymentChargeParams{}); err == nil {
		t.Fatal("expected nil saved payment charge error")
	}
	if _, err := nilProvider.PreviewPlanChange(ctx, bursar.ProviderPlanChangeRequest{}); err == nil {
		t.Fatal("expected nil plan preview error")
	}
	if _, err := nilProvider.ChangePlan(ctx, bursar.ProviderPlanChangeRequest{}); err == nil {
		t.Fatal("expected nil plan change error")
	}
	if err := nilProvider.CancelScheduledPlanChange(ctx, "subscription", "schedule", "cancel"); err == nil {
		t.Fatal("expected nil schedule cancellation error")
	}
}

func TestStripeProviderMapsOfficialClientFailures(t *testing.T) {
	ctx := t.Context()
	for _, status := range []int{404, 409, 429, 500} {
		t.Run(strconv.Itoa(status), func(t *testing.T) {
			provider := newStripeErrorProvider(t, status)
			checkout := bursar.CheckoutSessionRequest{AccountID: "acct", ProductID: "price", Quantity: 1, SuccessURL: "success", CancelURL: "cancel", IdempotencyKey: "checkout"}
			if _, err := provider.CreateCheckoutSession(ctx, checkout); err == nil {
				t.Fatal("expected checkout request failure")
			}
			if _, err := provider.GetCheckoutSessionStatus(ctx, "cs"); err == nil {
				t.Fatal("expected checkout status failure")
			}
			if _, err := provider.CreateCustomerPortalSession(ctx, "cus", "return"); err == nil {
				t.Fatal("expected portal request failure")
			}
			if _, err := provider.CreateUpdatePaymentMethodSession(ctx, "cus", "sub", "return"); err == nil {
				t.Fatal("expected update payment method failure")
			}
			if _, err := provider.CreatePaymentMethodSetupSession(ctx, "cus", "return", "cancel"); err == nil {
				t.Fatal("expected setup payment method failure")
			}
			if _, err := provider.CreateCustomer(ctx, bursar.CreateCustomerRequest{Email: "buyer", Name: "Buyer", IdempotencyKey: "customer"}); err == nil {
				t.Fatal("expected customer request failure")
			}
			if err := provider.CancelSubscription(ctx, "sub", "cancel"); err == nil {
				t.Fatal("expected cancel request failure")
			}
			if err := provider.ReactivateSubscription(ctx, "sub", "reactivate"); err == nil {
				t.Fatal("expected reactivate request failure")
			}
			if _, err := provider.ListPaymentMethods(ctx, "cus"); err == nil {
				t.Fatal("expected payment methods failure")
			}
			params := bursar.SavedPaymentChargeParams{CustomerID: "cus", PaymentMethodID: "pm", ProductID: "price", Quantity: 1, IdempotencyKey: "charge"}
			if _, err := provider.PreviewSavedPaymentCharge(ctx, params); err == nil {
				t.Fatal("expected saved payment preview failure")
			}
			if _, err := provider.ChargeSavedPaymentMethod(ctx, params); err == nil {
				t.Fatal("expected saved payment charge failure")
			}
			if _, err := provider.GetInvoiceURL(ctx, "invoice"); err == nil {
				t.Fatal("expected invoice failure")
			}
			plan := bursar.ProviderPlanChangeRequest{ProviderSubscriptionID: "sub", ProductID: "price", Quantity: 1, IdempotencyKey: "plan"}
			if _, err := provider.PreviewPlanChange(ctx, plan); err == nil {
				t.Fatal("expected plan preview failure")
			}
			if _, err := provider.ChangePlan(ctx, plan); err == nil {
				t.Fatal("expected plan change failure")
			}
			if err := provider.CancelScheduledPlanChange(ctx, "sub", "schedule", "cancel-schedule"); err == nil {
				t.Fatal("expected schedule cancellation failure")
			}
		})
	}
}

func TestStripeProviderRejectsMalformedScheduleResponses(t *testing.T) {
	ctx := t.Context()
	plan := bursar.ProviderPlanChangeRequest{
		ProviderSubscriptionID: "sub_1", ProductID: "price_1", Quantity: 1,
		EffectiveAt: "next_billing_date", IdempotencyKey: "schedule-malformed",
	}
	for _, response := range []string{
		`{}`,
		`{"id":"sched_1","phases":[]}`,
		`{"id":"sched_1","phases":[{"start_date":1784351724,"end_date":0,"items":[{"price":"price_old","quantity":1}]}]}`,
		`{"id":"sched_1","phases":[{"start_date":1784351724,"end_date":1787020124,"items":[{"price":"price_old","quantity":-1}]}]}`,
		`{"id":"sched_1","phases":[{"start_date":1784351724,"end_date":1787020124,"items":[{}]}]}`,
		`{"id":"sched_1","phases":[{"start_date":1784351724,"end_date":1787020124,"items":[{"price":"","quantity":1}]}]}`,
		`{"id":"sched_1","phases":[{"start_date":0,"end_date":1787020124,"items":[{"price":"price_old","quantity":1}]}]}`,
	} {
		fixture := &stripeFixture{scheduleResponse: response}
		provider := newStripeFixtureProvider(t, fixture)
		if _, err := provider.ChangePlan(ctx, plan); err == nil {
			t.Fatalf("expected malformed schedule response to fail: %s", response)
		}
	}
}

func TestStripeProviderRejectsIncompleteOfficialResponses(t *testing.T) {
	ctx := t.Context()
	provider := newStripeIncompleteProvider(t)
	checkout := bursar.CheckoutSessionRequest{AccountID: "acct", ProductID: "price", Quantity: 1, SuccessURL: "success", CancelURL: "cancel", IdempotencyKey: "checkout"}
	if _, err := provider.CreateCheckoutSession(ctx, checkout); err == nil {
		t.Fatal("expected incomplete checkout response error")
	}
	if _, err := provider.CreateCustomerPortalSession(ctx, "cus", "return"); err == nil {
		t.Fatal("expected incomplete portal response error")
	}
	if _, err := provider.CreateUpdatePaymentMethodSession(ctx, "cus", "sub", "return"); err == nil {
		t.Fatal("expected incomplete update response error")
	}
	if _, err := provider.CreatePaymentMethodSetupSession(ctx, "cus", "return", "cancel"); err == nil {
		t.Fatal("expected incomplete setup response error")
	}
	if _, err := provider.CreateCustomer(ctx, bursar.CreateCustomerRequest{Email: "buyer", Name: "Buyer", IdempotencyKey: "customer"}); err == nil {
		t.Fatal("expected incomplete customer response error")
	}
	if _, err := provider.PreviewSavedPaymentCharge(ctx, bursar.SavedPaymentChargeParams{CustomerID: "cus", PaymentMethodID: "pm", ProductID: "price", Quantity: 1, IdempotencyKey: "charge"}); err == nil {
		t.Fatal("expected incomplete price response error")
	}
	if _, err := provider.ChargeSavedPaymentMethod(ctx, bursar.SavedPaymentChargeParams{CustomerID: "cus", PaymentMethodID: "pm", ProductID: "price", Quantity: 1, IdempotencyKey: "charge"}); err == nil {
		t.Fatal("expected incomplete charge response error")
	}
	if _, err := provider.PreviewPlanChange(ctx, bursar.ProviderPlanChangeRequest{ProviderSubscriptionID: "sub", ProductID: "price", Quantity: 1}); err == nil {
		t.Fatal("expected incomplete plan preview error")
	}
	if _, err := provider.ChangePlan(ctx, bursar.ProviderPlanChangeRequest{ProviderSubscriptionID: "sub", ProductID: "price", Quantity: 1, IdempotencyKey: "plan"}); err == nil {
		t.Fatal("expected incomplete plan change error")
	}
}

func TestStripeProviderHandlesDeletedAndMalformedPaymentMethods(t *testing.T) {
	for _, response := range []string{
		`{"id":"cus_1","deleted":true}`,
		`{"id":"cus_1","invoice_settings":{"default_payment_method":"pm_1"}}`,
	} {
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Type", "application/json")
			if strings.HasPrefix(r.URL.Path, "/v1/customers/") {
				_, _ = w.Write([]byte(response))
				return
			}
			_, _ = w.Write([]byte(`{"object":"list","data":[{"id":"pm_1","type":"card","card":{"brand":"visa","last4":"bad","exp_month":13,"exp_year":0}}]}`))
		}))
		client := stripego.NewClient("sk_test", stripego.WithBackends(stripego.NewBackendsWithConfig(&stripego.BackendConfig{HTTPClient: server.Client(), URL: stripego.String(server.URL), MaxNetworkRetries: stripego.Int64(0)})))
		provider, err := New(Options{Client: client, WebhookSecret: "secret"})
		if err != nil {
			t.Fatal(err)
		}
		methods, err := provider.ListPaymentMethods(t.Context(), "cus_1")
		if strings.Contains(response, `"deleted":true`) {
			if err != nil || len(methods) != 0 {
				t.Fatalf("deleted customer methods = %#v, err = %v", methods, err)
			}
		} else if err == nil {
			t.Fatal("expected malformed payment method error")
		}
		server.Close()
	}
}

func TestStripeProviderCheckoutStatusAndSavedPaymentVariants(t *testing.T) {
	for _, status := range []string{"expired", "paid", "no_payment_required", "open", "complete"} {
		t.Run(status, func(t *testing.T) {
			session := &stripego.CheckoutSession{Status: stripego.CheckoutSessionStatus(status)}
			if status == "paid" || status == "no_payment_required" {
				session.PaymentStatus = stripego.CheckoutSessionPaymentStatus(status)
			}
			got, err := stripeCheckoutStatus(session)
			if err != nil || got == "" {
				t.Fatalf("status = %q, err = %v", got, err)
			}
		})
	}
	for _, status := range []stripego.PaymentIntentStatus{
		stripego.PaymentIntentStatusSucceeded, stripego.PaymentIntentStatusProcessing, stripego.PaymentIntentStatusRequiresAction,
		stripego.PaymentIntentStatusRequiresPaymentMethod, stripego.PaymentIntentStatusRequiresConfirmation, stripego.PaymentIntentStatusRequiresCapture,
		stripego.PaymentIntentStatusCanceled,
	} {
		if _, err := stripeSavedPaymentStatus(status); err != nil {
			t.Fatalf("status %q: %v", status, err)
		}
	}
	if _, err := stripeSavedPaymentStatus("unknown"); err == nil {
		t.Fatal("expected unknown payment status error")
	}
	if got := stripePaymentIntentActionURL(&stripego.PaymentIntent{NextAction: &stripego.PaymentIntentNextAction{RedirectToURL: &stripego.PaymentIntentNextActionRedirectToURL{URL: " https://action.test "}}}); got != "https://action.test" {
		t.Fatalf("action URL = %q", got)
	}
	if stripePaymentIntentActionURL(nil) != "" || stripePaymentIntentActionURL(&stripego.PaymentIntent{}) != "" {
		t.Fatal("missing payment action URL should be empty")
	}
}

func TestStripeProviderScopedIdempotencyAndWebhookErrors(t *testing.T) {
	if got, err := stripeScopedIdempotencyKey("key", "scope"); err != nil || got != "key:scope" {
		t.Fatalf("scoped key = %q, err = %v", got, err)
	}
	if got, err := stripeScopedIdempotencyKey(strings.Repeat("x", 255), "scope"); err != nil || !strings.HasPrefix(got, "bursar:") {
		t.Fatalf("hashed scoped key = %q, err = %v", got, err)
	}
	provider := newStripeFixtureProvider(t, &stripeFixture{})
	if _, err := provider.HandleWebhook(t.Context(), bursar.WebhookRequest{}); err == nil {
		t.Fatal("expected empty webhook body error")
	}
	if _, err := provider.HandleWebhook(t.Context(), bursar.WebhookRequest{RawBody: []byte(`{}`)}); err == nil {
		t.Fatal("expected missing webhook signature error")
	}
	if _, err := provider.HandleWebhook(t.Context(), bursar.WebhookRequest{RawBody: []byte(`{}`), Header: http.Header{"Stripe-Signature": []string{"bad"}}}); err == nil {
		t.Fatal("expected invalid webhook signature error")
	}
	for _, payload := range []string{
		`{"id":"evt_no_data","object":"event","created":1784351724,"livemode":false,"type":"product.updated"}`,
		`{"id":"evt_bad_time","object":"event","created":-1,"livemode":false,"type":"product.updated","data":{"object":{}}}`,
	} {
		signed := stripego.GenerateTestSignedPayload(&stripego.UnsignedPayload{Payload: []byte(payload), Secret: "whsec_test"})
		if _, err := provider.HandleWebhook(t.Context(), bursar.WebhookRequest{RawBody: []byte(payload), Header: http.Header{"Stripe-Signature": []string{signed.Header}}}); err == nil {
			t.Fatal("expected webhook payload validation error")
		}
	}
}
