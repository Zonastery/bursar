package bursar

import (
	"context"
	"errors"
	"io"
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

func TestProviderRegistryRejectsInvalidFactoriesAndResults(t *testing.T) {
	factoryContext := ProviderFactoryContext{ProviderEnvironment: ProviderEnvironmentTest}
	valid := func(context.Context, ProviderFactoryContext) (PaymentProvider, error) {
		return providerStub{name: "stub"}, nil
	}
	checks := []struct {
		name      string
		construct func() error
	}{
		{"invalid environment", func() error {
			_, err := NewProviderRegistry(ProviderFactoryContext{}, map[string]ProviderFactory{"stub": valid})
			return err
		}},
		{"empty factories", func() error { _, err := NewProviderRegistry(factoryContext, nil); return err }},
		{"blank name", func() error {
			_, err := NewProviderRegistry(factoryContext, map[string]ProviderFactory{" ": valid})
			return err
		}},
		{"nil factory", func() error {
			_, err := NewProviderRegistry(factoryContext, map[string]ProviderFactory{"stub": nil})
			return err
		}},
		{"trimmed duplicate", func() error {
			_, err := NewProviderRegistry(factoryContext, map[string]ProviderFactory{"stub": valid, " stub ": valid})
			return err
		}},
	}
	for _, test := range checks {
		t.Run(test.name, func(t *testing.T) {
			if err := test.construct(); err == nil {
				t.Fatal("invalid provider configuration was accepted")
			}
		})
	}

	factoryError := errors.New("provider unavailable")
	registries := []struct {
		name    string
		factory ProviderFactory
	}{
		{"factory error", func(context.Context, ProviderFactoryContext) (PaymentProvider, error) { return nil, factoryError }},
		{"nil provider", func(context.Context, ProviderFactoryContext) (PaymentProvider, error) { return nil, nil }},
		{"wrong name", func(context.Context, ProviderFactoryContext) (PaymentProvider, error) {
			return providerStub{name: "other"}, nil
		}},
	}
	for _, test := range registries {
		t.Run(test.name, func(t *testing.T) {
			registry, err := NewProviderRegistry(factoryContext, map[string]ProviderFactory{"stub": test.factory})
			if err != nil {
				t.Fatal(err)
			}
			if _, err := registry.Get(context.Background(), "stub"); err == nil {
				t.Fatal("invalid provider result was accepted")
			}
		})
	}

	registry, err := NewProviderRegistry(factoryContext, map[string]ProviderFactory{"stub": valid})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := registry.Get(context.Background(), "missing"); err == nil {
		t.Fatal("unknown provider was accepted")
	}
	if _, err := registry.Get(context.Background(), " "); err == nil {
		t.Fatal("blank provider was accepted")
	}
	if got, err := registry.Select(context.Background(), "", "", []string{"stub"}); err != nil || got.Name() != "stub" {
		t.Fatalf("single compatible provider = %v, %v", got, err)
	}
	if got, err := registry.Select(context.Background(), "", "stub", []string{"stub"}); err != nil || got.Name() != "stub" {
		t.Fatalf("fallback provider = %v, %v", got, err)
	}
	if _, err := registry.Select(context.Background(), "other", "", []string{"stub"}); err == nil {
		t.Fatal("incompatible requested provider was accepted")
	}
	if _, err := registry.Select(context.Background(), "", "", nil); err == nil {
		t.Fatal("empty compatible provider set was accepted")
	}
	var nilRegistry *ProviderRegistry
	if nilRegistry.Configured() != nil || nilRegistry.Environment() != "" {
		t.Fatal("nil registry accessors returned state")
	}
	nilRegistry.Clear()
	if _, err := nilRegistry.Get(context.Background(), "stub"); err == nil {
		t.Fatal("nil registry returned a provider")
	}
}

type failingWebhookReader struct{ err error }

func (r failingWebhookReader) Read([]byte) (int, error) { return 0, r.err }

func TestWebhookRequestFromHTTPRejectsInvalidRequestBoundaries(t *testing.T) {
	if _, err := WebhookRequestFromHTTP(nil, 1); err == nil {
		t.Fatal("nil request was accepted")
	}
	request := httptest.NewRequest(http.MethodPost, "https://example.test/webhook", strings.NewReader("body"))
	request.Body = nil
	if _, err := WebhookRequestFromHTTP(request, 1); err == nil {
		t.Fatal("nil body was accepted")
	}
	request = httptest.NewRequest(http.MethodPost, "https://example.test/webhook", strings.NewReader("body"))
	if _, err := WebhookRequestFromHTTP(request, 0); err == nil {
		t.Fatal("unbounded body was accepted")
	}
	readError := errors.New("read failed")
	request = httptest.NewRequest(http.MethodPost, "https://example.test/webhook", strings.NewReader("body"))
	request.Body = io.NopCloser(failingWebhookReader{err: readError})
	if _, err := WebhookRequestFromHTTP(request, 32); !errors.Is(err, readError) {
		t.Fatalf("read error = %v", err)
	}
}
