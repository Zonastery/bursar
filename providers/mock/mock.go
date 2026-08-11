// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

// Package mock provides a deterministic in-memory PaymentProvider for unit
// tests and local examples. It is intentionally explicit: production code
// must configure a real provider adapter.
package mock

import (
	"context"
	"fmt"
	"strings"
	"sync"
	"time"

	bursar "github.com/Zonastery/bursar/v2"
)

const ProviderName = "mock"

// Options configures the test provider.
type Options struct {
	Name            string
	CheckoutURLBase string
	Now             func() time.Time
}

// Provider records checkout requests and accepts preconstructed verified
// events. It never attempts to validate a real provider signature.
type Provider struct {
	name            string
	checkoutURLBase string
	now             func() time.Time

	mu        sync.Mutex
	checkouts map[string]bursar.CheckoutSession
	events    map[string]bursar.BillingEvent
}

var _ bursar.PaymentProvider = (*Provider)(nil)
var _ bursar.CheckoutStatusProvider = (*Provider)(nil)

// New constructs a deterministic mock provider.
func New(options Options) *Provider {
	name := strings.TrimSpace(options.Name)
	if name == "" {
		name = ProviderName
	}
	base := strings.TrimRight(strings.TrimSpace(options.CheckoutURLBase), "/")
	if base == "" {
		base = "https://mock.bursar.invalid/checkout"
	}
	now := options.Now
	if now == nil {
		now = time.Now
	}
	return &Provider{
		name:            name,
		checkoutURLBase: base,
		now:             now,
		checkouts:       make(map[string]bursar.CheckoutSession),
		events:          make(map[string]bursar.BillingEvent),
	}
}

// Name returns the configured provider name.
func (p *Provider) Name() string {
	if p == nil {
		return ProviderName
	}
	return p.name
}

// CreateCheckoutSession records an idempotency-keyed deterministic checkout.
func (p *Provider) CreateCheckoutSession(_ context.Context, request bursar.CheckoutSessionRequest) (bursar.CheckoutSession, error) {
	if p == nil {
		return bursar.CheckoutSession{}, bursar.NewError("mock provider is not initialized", bursar.ErrorOptions{Code: bursar.ErrorCodeProviderResponseInvalid})
	}
	if strings.TrimSpace(request.AccountID) == "" || strings.TrimSpace(request.ProductID) == "" || request.Quantity < 1 || strings.TrimSpace(request.IdempotencyKey) == "" {
		return bursar.CheckoutSession{}, bursar.NewError("mock checkout requires account, product, positive quantity, and idempotency key", bursar.ErrorOptions{Code: bursar.ErrorCodeConfig, Category: bursar.ErrorCategoryInvalidRequest})
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	if session, exists := p.checkouts[request.IdempotencyKey]; exists {
		return session, nil
	}
	id := fmt.Sprintf("mock_checkout_%d", len(p.checkouts)+1)
	session := bursar.CheckoutSession{ID: id, URL: p.checkoutURLBase + "/" + id, CustomerID: request.CustomerID}
	p.checkouts[request.IdempotencyKey] = session
	return session, nil
}

// GetCheckoutSessionStatus returns a stable completed status for a recorded
// checkout. Unknown IDs are reported as a typed not-found error.
func (p *Provider) GetCheckoutSessionStatus(_ context.Context, sessionID string) (string, error) {
	if p == nil {
		return "", bursar.NewError("mock provider is not initialized", bursar.ErrorOptions{Code: bursar.ErrorCodeProviderResponseInvalid})
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	for _, session := range p.checkouts {
		if session.ID == sessionID {
			return "completed", nil
		}
	}
	return "", bursar.NewError("mock checkout session not found", bursar.ErrorOptions{Code: bursar.ErrorCodeCommerceResourceNotFound, Category: bursar.ErrorCategoryNotFound})
}

// QueueEvent makes a preverified event available to HandleWebhook. This keeps
// tests explicit about the boundary that real adapters protect with a provider
// signature verifier.
func (p *Provider) QueueEvent(event bursar.BillingEvent) error {
	if p == nil {
		return bursar.NewError("mock provider is not initialized", bursar.ErrorOptions{Code: bursar.ErrorCodeProviderResponseInvalid})
	}
	if event.Provider == "" {
		event.Provider = p.name
	}
	if event.OccurredAt.IsZero() {
		event.OccurredAt = p.now().UTC()
	}
	if err := event.Validate(); err != nil {
		return err
	}
	p.mu.Lock()
	p.events[event.ID] = event
	p.mu.Unlock()
	return nil
}

// HandleWebhook looks up the queued event named by X-Bursar-Mock-Event. The
// custom header is intentionally limited to this test-only package.
func (p *Provider) HandleWebhook(_ context.Context, request bursar.WebhookRequest) (bursar.WebhookResult, error) {
	if p == nil {
		return bursar.WebhookResult{}, bursar.NewError("mock provider is not initialized", bursar.ErrorOptions{Code: bursar.ErrorCodeProviderResponseInvalid})
	}
	eventID := strings.TrimSpace(request.Header.Get("X-Bursar-Mock-Event"))
	if eventID == "" {
		return bursar.WebhookResult{}, bursar.NewError("X-Bursar-Mock-Event header is required", bursar.ErrorOptions{Code: bursar.ErrorCodeConfig, Category: bursar.ErrorCategoryInvalidRequest})
	}
	p.mu.Lock()
	event, exists := p.events[eventID]
	p.mu.Unlock()
	if !exists {
		return bursar.WebhookResult{}, bursar.NewError("mock billing event not found", bursar.ErrorOptions{Code: bursar.ErrorCodeCommerceResourceNotFound, Category: bursar.ErrorCategoryNotFound})
	}
	return bursar.WebhookResult{Received: true, Provider: p.name, EventID: event.ID, EventType: string(event.Type), Event: &event}, nil
}
