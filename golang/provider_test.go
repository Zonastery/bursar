package bursar

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

type providerStub struct{ name string }

func (p providerStub) Name() string { return p.name }

func (p providerStub) CreateCheckoutSession(context.Context, CheckoutSessionRequest) (CheckoutSession, error) {
	return CheckoutSession{ID: "checkout_1", URL: "https://example.test/checkout_1"}, nil
}

func (p providerStub) HandleWebhook(_ context.Context, _ WebhookRequest) (WebhookResult, error) {
	return WebhookResult{
		Received: true,
		Provider: p.name,
		Event: &BillingEvent{
			ID: "event_1", Provider: p.name, Type: BillingEventPaymentSucceeded, OccurredAt: time.Now(),
		},
	}, nil
}

func TestProviderRegistryLazilyInitializesOnce(t *testing.T) {
	t.Parallel()
	var calls atomic.Int32
	registry, err := NewProviderRegistry(ProviderFactoryContext{ProviderEnvironment: ProviderEnvironmentTest}, map[string]ProviderFactory{
		"stub": func(context.Context, ProviderFactoryContext) (PaymentProvider, error) {
			calls.Add(1)
			return providerStub{name: "stub"}, nil
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	first, err := registry.Get(context.Background(), "stub")
	if err != nil {
		t.Fatal(err)
	}
	second, err := registry.Get(context.Background(), "stub")
	if err != nil {
		t.Fatal(err)
	}
	if first != second {
		t.Fatal("registry did not reuse provider instance")
	}
	if got := calls.Load(); got != 1 {
		t.Fatalf("factory calls = %d, want 1", got)
	}
}

func TestProviderRegistryClearCreatesFreshInstance(t *testing.T) {
	t.Parallel()
	var calls atomic.Int32
	registry, err := NewProviderRegistry(ProviderFactoryContext{ProviderEnvironment: ProviderEnvironmentTest}, map[string]ProviderFactory{
		"stub": func(context.Context, ProviderFactoryContext) (PaymentProvider, error) {
			calls.Add(1)
			return &providerStub{name: "stub"}, nil
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	first, err := registry.Get(context.Background(), "stub")
	if err != nil {
		t.Fatal(err)
	}
	registry.Clear()
	second, err := registry.Get(context.Background(), "stub")
	if err != nil {
		t.Fatal(err)
	}
	if first == second {
		t.Fatal("Clear() reused the cached provider instance")
	}
	if got := calls.Load(); got != 2 {
		t.Fatalf("factory calls = %d, want 2", got)
	}
}

func TestProviderRegistryRejectsAmbiguousSelection(t *testing.T) {
	t.Parallel()
	registry, err := NewProviderRegistry(ProviderFactoryContext{ProviderEnvironment: ProviderEnvironmentTest}, map[string]ProviderFactory{
		"one": func(context.Context, ProviderFactoryContext) (PaymentProvider, error) {
			return providerStub{name: "one"}, nil
		},
		"two": func(context.Context, ProviderFactoryContext) (PaymentProvider, error) {
			return providerStub{name: "two"}, nil
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := registry.Select(context.Background(), "", "", []string{"one", "two"}); err == nil {
		t.Fatal("expected ambiguous provider selection error")
	}
}

func TestWebhookRequestFromHTTPLimitsAndCopies(t *testing.T) {
	t.Parallel()
	req := httptest.NewRequest(http.MethodPost, "https://example.test/webhook", strings.NewReader("signed-body"))
	req.Header.Set("X-Signature", "signature")
	webhook, err := WebhookRequestFromHTTP(req, 32)
	if err != nil {
		t.Fatal(err)
	}
	if got, want := string(webhook.RawBody), "signed-body"; got != want {
		t.Fatalf("body = %q, want %q", got, want)
	}
	req.Header.Set("X-Signature", "changed")
	if got, want := webhook.Header.Get("X-Signature"), "signature"; got != want {
		t.Fatalf("header = %q, want %q", got, want)
	}

	overstated := httptest.NewRequest(http.MethodPost, "https://example.test/webhook", strings.NewReader("too-large"))
	if _, err := WebhookRequestFromHTTP(overstated, 3); err == nil {
		t.Fatal("expected size limit error")
	}
}
