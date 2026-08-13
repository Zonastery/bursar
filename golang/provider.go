// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"sort"
	"strings"
	"sync"
)

// ProviderEnvironment partitions financial provider data. Applications must
// select it explicitly for database-backed stores so test credentials and live
// financial state can never be mixed accidentally.
type ProviderEnvironment string

const (
	ProviderEnvironmentLive    ProviderEnvironment = "live"
	ProviderEnvironmentTest    ProviderEnvironment = "test"
	ProviderEnvironmentSandbox ProviderEnvironment = "sandbox"
)

// Validate reports whether e is a supported financial provider environment.
func (e ProviderEnvironment) Validate() error {
	switch e {
	case ProviderEnvironmentLive, ProviderEnvironmentTest, ProviderEnvironmentSandbox:
		return nil
	default:
		return fmt.Errorf("bursar: unsupported provider environment %q", e)
	}
}

// WebhookRequest is the framework-neutral raw request boundary used by payment
// providers. RawBody is intentionally retained because provider signatures
// must be verified before JSON is decoded or normalized.
type WebhookRequest struct {
	RawBody []byte
	Header  http.Header
}

// WebhookRequestFromHTTP reads one bounded raw webhook request. It copies the
// headers and body so callers can safely hand it to asynchronous billing work.
func WebhookRequestFromHTTP(r *http.Request, maxBytes int64) (WebhookRequest, error) {
	if r == nil {
		return WebhookRequest{}, errors.New("bursar: webhook request is required")
	}
	if r.Body == nil {
		return WebhookRequest{}, errors.New("bursar: webhook request body is required")
	}
	if maxBytes <= 0 {
		return WebhookRequest{}, errors.New("bursar: webhook body limit must be positive")
	}
	body, err := io.ReadAll(io.LimitReader(r.Body, maxBytes+1))
	if err != nil {
		return WebhookRequest{}, fmt.Errorf("bursar: read webhook body: %w", err)
	}
	if int64(len(body)) > maxBytes {
		return WebhookRequest{}, fmt.Errorf("bursar: webhook body exceeds %d bytes", maxBytes)
	}
	return WebhookRequest{RawBody: body, Header: r.Header.Clone()}, nil
}

// CheckoutSessionRequest is the provider-neutral hosted checkout request.
// ProductID is deliberately provider-internal: public commerce accepts
// Bursar's offer key and resolves it before calling a provider.
type CheckoutSessionRequest struct {
	AccountID string
	ProductID string
	// Mode is provider checkout mode (for example, "payment" or
	// "subscription"). It is resolved from the catalog offer rather than
	// accepted from an untrusted browser request.
	Mode           string
	Quantity       int64
	SuccessURL     string
	CancelURL      string
	CustomerID     string
	CustomerEmail  string
	Metadata       map[string]string
	IdempotencyKey string
}

// CheckoutSession is the minimum provider result required by Bursar's
// idempotent checkout-intent lifecycle.
type CheckoutSession struct {
	ID         string
	URL        string
	CustomerID string
}

// WebhookResult contains the normalized result of a provider-verified webhook.
// Event is populated only after a provider SDK has authenticated RawBody.
type WebhookResult struct {
	Received  bool
	Retryable bool
	Provider  string
	EventID   string
	EventType string
	Event     *BillingEvent
}

// PaymentProvider is the portable provider contract. Implementations must
// verify raw webhook signatures before returning a BillingEvent.
type PaymentProvider interface {
	Name() string
	CreateCheckoutSession(context.Context, CheckoutSessionRequest) (CheckoutSession, error)
	HandleWebhook(context.Context, WebhookRequest) (WebhookResult, error)
}

// The optional interfaces below model provider capabilities without forcing
// every provider to implement an artificial lowest common denominator.
type CheckoutStatusProvider interface {
	GetCheckoutSessionStatus(context.Context, string) (string, error)
}

type CustomerPortalProvider interface {
	CreateCustomerPortalSession(context.Context, string, string) (string, error)
}

type PaymentMethodPortalProvider interface {
	CreateUpdatePaymentMethodSession(context.Context, string, string, string) (string, error)
	CreatePaymentMethodSetupSession(context.Context, string, string, string) (string, error)
}

type CustomerProvider interface {
	CreateCustomer(context.Context, CreateCustomerRequest) (string, error)
}

// CreateCustomerRequest makes provider customer creation replay-safe. The
// idempotency key is required because a timeout after provider commit cannot
// otherwise be reconciled without creating duplicate customer records.
type CreateCustomerRequest struct {
	Email          string
	Name           string
	Metadata       map[string]string
	IdempotencyKey string
}

type SubscriptionProvider interface {
	CancelSubscription(context.Context, string, string) error
	ReactivateSubscription(context.Context, string, string) error
}

type InvoiceProvider interface {
	GetInvoiceURL(context.Context, string) (string, error)
}

// ProviderFactoryContext is passed to lazily initialized application-owned
// providers. It contains no mutable SDK state and is safe to retain.
type ProviderFactoryContext struct {
	TenantID            string
	ProviderEnvironment ProviderEnvironment
}

// ProviderFactory creates one reusable, concurrency-safe provider instance.
type ProviderFactory func(context.Context, ProviderFactoryContext) (PaymentProvider, error)

type providerLoad struct {
	done       chan struct{}
	generation uint64
	provider   PaymentProvider
	err        error
}

// ProviderRegistry owns lazy provider creation. It ensures a provider is
// initialized once even under concurrent checkout and webhook traffic.
type ProviderRegistry struct {
	mu         sync.Mutex
	context    ProviderFactoryContext
	factories  map[string]ProviderFactory
	instances  map[string]PaymentProvider
	loading    map[string]*providerLoad
	generation uint64
}

// NewProviderRegistry constructs a registry from non-empty named factories.
func NewProviderRegistry(factoryContext ProviderFactoryContext, factories map[string]ProviderFactory) (*ProviderRegistry, error) {
	if err := factoryContext.ProviderEnvironment.Validate(); err != nil {
		return nil, err
	}
	if len(factories) == 0 {
		return nil, errors.New("bursar: at least one payment provider is required")
	}
	cloned := make(map[string]ProviderFactory, len(factories))
	for name, factory := range factories {
		name = strings.TrimSpace(name)
		if name == "" || factory == nil {
			return nil, errors.New("bursar: provider names and factories are required")
		}
		if _, exists := cloned[name]; exists {
			return nil, fmt.Errorf("bursar: duplicate provider %q", name)
		}
		cloned[name] = factory
	}
	return &ProviderRegistry{
		context:   factoryContext,
		factories: cloned,
		instances: make(map[string]PaymentProvider, len(cloned)),
		loading:   make(map[string]*providerLoad),
	}, nil
}

// Configured returns provider names in deterministic order.
func (r *ProviderRegistry) Configured() []string {
	if r == nil {
		return nil
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	names := make([]string, 0, len(r.factories))
	for name := range r.factories {
		names = append(names, name)
	}
	sort.Strings(names)
	return names
}

// Environment returns the registry's explicitly configured provider
// environment. Bursar uses it to reject a test-provider registry attached to
// a live tenant-bound store (and vice versa).
func (r *ProviderRegistry) Environment() ProviderEnvironment {
	if r == nil {
		return ""
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.context.ProviderEnvironment
}

// Clear discards cached and in-flight provider registrations. Existing callers
// may still receive the provider instance they were already awaiting, but it
// cannot repopulate the cleared cache; the next lookup creates a fresh
// instance. This mirrors the provider-cache lifecycle in the other SDKs.
func (r *ProviderRegistry) Clear() {
	if r == nil {
		return
	}
	r.mu.Lock()
	r.generation++
	r.instances = make(map[string]PaymentProvider, len(r.factories))
	r.loading = make(map[string]*providerLoad)
	r.mu.Unlock()
}

// Get lazily constructs and returns provider name.
func (r *ProviderRegistry) Get(ctx context.Context, name string) (PaymentProvider, error) {
	if r == nil {
		return nil, errors.New("bursar: provider registry is not configured")
	}
	name = strings.TrimSpace(name)
	if name == "" {
		return nil, errors.New("bursar: provider name is required")
	}

	r.mu.Lock()
	if provider := r.instances[name]; provider != nil {
		r.mu.Unlock()
		return provider, nil
	}
	if load := r.loading[name]; load != nil {
		r.mu.Unlock()
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-load.done:
			return load.provider, load.err
		}
	}
	factory := r.factories[name]
	if factory == nil {
		r.mu.Unlock()
		return nil, fmt.Errorf("bursar: provider %q is not configured", name)
	}
	load := &providerLoad{done: make(chan struct{}), generation: r.generation}
	r.loading[name] = load
	r.mu.Unlock()

	provider, err := factory(ctx, r.context)
	if err == nil {
		if provider == nil {
			err = fmt.Errorf("bursar: provider factory %q returned nil", name)
		} else if strings.TrimSpace(provider.Name()) != name {
			err = fmt.Errorf("bursar: provider factory %q returned provider %q", name, provider.Name())
		}
	}

	r.mu.Lock()
	if r.loading[name] == load {
		delete(r.loading, name)
	}
	load.provider, load.err = provider, err
	if err == nil && load.generation == r.generation {
		r.instances[name] = provider
	}
	close(load.done)
	r.mu.Unlock()
	return provider, err
}

// Select chooses an explicitly requested provider, then a compatible default,
// then the only compatible provider. Ambiguity is rejected rather than guessed.
func (r *ProviderRegistry) Select(ctx context.Context, requested, fallback string, compatible []string) (PaymentProvider, error) {
	allowed := make(map[string]struct{}, len(compatible))
	for _, name := range compatible {
		if name = strings.TrimSpace(name); name != "" {
			allowed[name] = struct{}{}
		}
	}
	if len(allowed) == 0 {
		return nil, errors.New("bursar: no compatible payment provider is configured")
	}
	for _, candidate := range []string{requested, fallback} {
		candidate = strings.TrimSpace(candidate)
		if candidate == "" {
			continue
		}
		if _, ok := allowed[candidate]; !ok {
			return nil, fmt.Errorf("bursar: provider %q is not compatible with this offer", candidate)
		}
		return r.Get(ctx, candidate)
	}
	if len(allowed) != 1 {
		return nil, errors.New("bursar: provider selection is ambiguous")
	}
	for name := range allowed {
		return r.Get(ctx, name)
	}
	panic("unreachable")
}
