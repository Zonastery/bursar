// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

// Package mock provides a deterministic in-memory PaymentProvider for unit
// tests and local examples. It is intentionally explicit: production code
// must configure a real provider adapter.
package mock

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"strings"
	"sync"
	"time"

	bursar "github.com/Zonastery/bursar/golang/v2"
	"github.com/shopspring/decimal"
)

const ProviderName = "mock"

// Options configures the test provider.
type Options struct {
	Name               string
	CheckoutURLBase    string
	Now                func() time.Time
	Environment        bursar.ProviderEnvironment
	PaymentMethods     []bursar.PaymentMethodInfo
	SavedPaymentQuote  bursar.SavedPaymentChargeQuote
	SavedPaymentResult bursar.SavedPaymentChargeResult
	PlanChangePreview  bursar.PlanChangePreview
}

// Provider records checkout requests and accepts preconstructed verified
// events. It never attempts to validate a real provider signature.
type Provider struct {
	name               string
	checkoutURLBase    string
	now                func() time.Time
	environment        bursar.ProviderEnvironment
	paymentMethods     []bursar.PaymentMethodInfo
	savedPaymentQuote  bursar.SavedPaymentChargeQuote
	savedPaymentResult bursar.SavedPaymentChargeResult
	planChangePreview  bursar.PlanChangePreview

	mu          sync.Mutex
	checkouts   map[string]bursar.CheckoutSession
	customers   map[string]string
	charges     map[string]bursar.SavedPaymentChargeResult
	planChanges map[string]bursar.ProviderPlanChangeResult
	events      map[string]bursar.BillingEvent
}

var _ bursar.PaymentProvider = (*Provider)(nil)
var _ bursar.CheckoutStatusProvider = (*Provider)(nil)
var _ bursar.CustomerPortalProvider = (*Provider)(nil)
var _ bursar.PaymentMethodPortalProvider = (*Provider)(nil)
var _ bursar.CustomerProvider = (*Provider)(nil)
var _ bursar.SubscriptionProvider = (*Provider)(nil)
var _ bursar.InvoiceProvider = (*Provider)(nil)
var _ bursar.PaymentMethodsProvider = (*Provider)(nil)
var _ bursar.SavedPaymentPreviewProvider = (*Provider)(nil)
var _ bursar.SavedPaymentChargeProvider = (*Provider)(nil)
var _ bursar.PlanChangePreviewProvider = (*Provider)(nil)
var _ bursar.PlanChangeProvider = (*Provider)(nil)
var _ bursar.ScheduledPlanChangeCancellationProvider = (*Provider)(nil)

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
	environment := options.Environment
	if environment == "" || environment.Validate() != nil {
		environment = bursar.ProviderEnvironmentTest
	}
	paymentMethods := append([]bursar.PaymentMethodInfo(nil), options.PaymentMethods...)
	if len(paymentMethods) == 0 {
		paymentMethods = []bursar.PaymentMethodInfo{{ID: "mock_pm_1", Brand: "visa", Last4: "4242", ExpiryMonth: 12, ExpiryYear: 2099, IsDefault: true}}
	}
	quote := options.SavedPaymentQuote
	if strings.TrimSpace(quote.Currency) == "" {
		quote = bursar.SavedPaymentChargeQuote{AmountMinor: 1000, Currency: "USD"}
	}
	charge := options.SavedPaymentResult
	if charge.Status == "" {
		amount := quote.AmountMinor
		charge = bursar.SavedPaymentChargeResult{Status: bursar.SavedPaymentChargeSucceeded, AmountMinor: &amount, Currency: quote.Currency}
	}
	preview := options.PlanChangePreview
	if strings.TrimSpace(preview.Currency) == "" {
		preview = bursar.PlanChangePreview{TotalAmount: decimal.Zero, SettlementAmount: decimal.Zero, Currency: "USD", EffectiveAt: now().UTC()}
	}
	return &Provider{
		name:               name,
		checkoutURLBase:    base,
		now:                now,
		environment:        environment,
		paymentMethods:     paymentMethods,
		savedPaymentQuote:  quote,
		savedPaymentResult: charge,
		planChangePreview:  preview,
		checkouts:          make(map[string]bursar.CheckoutSession),
		customers:          make(map[string]string),
		charges:            make(map[string]bursar.SavedPaymentChargeResult),
		planChanges:        make(map[string]bursar.ProviderPlanChangeResult),
		events:             make(map[string]bursar.BillingEvent),
	}
}

// ProviderEnvironment returns the deterministic financial namespace.
func (p *Provider) ProviderEnvironment() bursar.ProviderEnvironment {
	if p == nil {
		return ""
	}
	return p.environment
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

// CreateCustomerPortalSession returns a deterministic customer-scoped URL.
func (p *Provider) CreateCustomerPortalSession(_ context.Context, customerID, returnURL string) (string, error) {
	if p == nil {
		return "", mockUninitializedError()
	}
	if err := requireMockText(customerID, "customer ID"); err != nil {
		return "", err
	}
	if err := requireMockText(returnURL, "customer portal return URL"); err != nil {
		return "", err
	}
	return p.checkoutURLBase + "/portal/" + customerID, nil
}

// CreateUpdatePaymentMethodSession returns a deterministic subscription URL.
func (p *Provider) CreateUpdatePaymentMethodSession(_ context.Context, customerID, subscriptionID, returnURL string) (string, error) {
	if p == nil {
		return "", mockUninitializedError()
	}
	for _, input := range []struct{ value, field string }{{customerID, "customer ID"}, {subscriptionID, "subscription ID"}, {returnURL, "payment method return URL"}} {
		if err := requireMockText(input.value, input.field); err != nil {
			return "", err
		}
	}
	return p.checkoutURLBase + "/subscriptions/" + subscriptionID + "/payment-method", nil
}

// CreatePaymentMethodSetupSession returns a deterministic setup URL.
func (p *Provider) CreatePaymentMethodSetupSession(_ context.Context, customerID, returnURL, cancelURL string) (string, error) {
	if p == nil {
		return "", mockUninitializedError()
	}
	for _, input := range []struct{ value, field string }{{customerID, "customer ID"}, {returnURL, "payment method return URL"}, {cancelURL, "payment method cancel URL"}} {
		if err := requireMockText(input.value, input.field); err != nil {
			return "", err
		}
	}
	return p.checkoutURLBase + "/customers/" + customerID + "/payment-method", nil
}

// CreateCustomer memoizes a deterministic customer by stable operation key.
func (p *Provider) CreateCustomer(_ context.Context, request bursar.CreateCustomerRequest) (string, error) {
	if p == nil {
		return "", mockUninitializedError()
	}
	if err := requireMockText(request.Email, "customer email"); err != nil {
		return "", err
	}
	if err := requireMockText(request.Name, "customer name"); err != nil {
		return "", err
	}
	if err := requireMockText(request.IdempotencyKey, "idempotency key"); err != nil {
		return "", err
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	if id := p.customers[request.IdempotencyKey]; id != "" {
		return id, nil
	}
	id := "mock_customer_" + stableMockSuffix(request.IdempotencyKey)
	p.customers[request.IdempotencyKey] = id
	return id, nil
}

// CancelSubscription validates a replay-safe deterministic operation.
func (p *Provider) CancelSubscription(_ context.Context, subscriptionID, idempotencyKey string) error {
	return validateMockSubscriptionOperation(subscriptionID, idempotencyKey)
}

// ReactivateSubscription validates a replay-safe deterministic operation.
func (p *Provider) ReactivateSubscription(_ context.Context, subscriptionID, idempotencyKey string) error {
	return validateMockSubscriptionOperation(subscriptionID, idempotencyKey)
}

// GetInvoiceURL returns a deterministic invoice document URL.
func (p *Provider) GetInvoiceURL(_ context.Context, invoiceID string) (string, error) {
	if p == nil {
		return "", mockUninitializedError()
	}
	if err := requireMockText(invoiceID, "invoice ID"); err != nil {
		return "", err
	}
	return p.checkoutURLBase + "/invoices/" + invoiceID, nil
}

// ListPaymentMethods returns a defensive copy of configured deterministic methods.
func (p *Provider) ListPaymentMethods(_ context.Context, customerID string) ([]bursar.PaymentMethodInfo, error) {
	if p == nil {
		return nil, mockUninitializedError()
	}
	if err := requireMockText(customerID, "customer ID"); err != nil {
		return nil, err
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	return append([]bursar.PaymentMethodInfo(nil), p.paymentMethods...), nil
}

// PreviewSavedPaymentCharge returns the configured deterministic quote.
func (p *Provider) PreviewSavedPaymentCharge(_ context.Context, params bursar.SavedPaymentChargeParams) (bursar.SavedPaymentChargeQuote, error) {
	if p == nil {
		return bursar.SavedPaymentChargeQuote{}, mockUninitializedError()
	}
	if err := validateMockSavedPayment(params, false); err != nil {
		return bursar.SavedPaymentChargeQuote{}, err
	}
	return p.savedPaymentQuote, nil
}

// ChargeSavedPaymentMethod memoizes one result per idempotency key.
func (p *Provider) ChargeSavedPaymentMethod(_ context.Context, params bursar.SavedPaymentChargeParams) (bursar.SavedPaymentChargeResult, error) {
	if p == nil {
		return bursar.SavedPaymentChargeResult{}, mockUninitializedError()
	}
	if err := validateMockSavedPayment(params, true); err != nil {
		return bursar.SavedPaymentChargeResult{}, err
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	if result, exists := p.charges[params.IdempotencyKey]; exists {
		return result, nil
	}
	result := p.savedPaymentResult
	if strings.TrimSpace(result.ProviderPaymentID) == "" {
		result.ProviderPaymentID = "mock_payment_" + stableMockSuffix(params.IdempotencyKey)
	}
	p.charges[params.IdempotencyKey] = result
	return result, nil
}

// PreviewPlanChange returns the configured deterministic exact-money quote.
func (p *Provider) PreviewPlanChange(_ context.Context, request bursar.ProviderPlanChangeRequest) (bursar.PlanChangePreview, error) {
	if p == nil {
		return bursar.PlanChangePreview{}, mockUninitializedError()
	}
	if err := validateMockPlanChange(request, false); err != nil {
		return bursar.PlanChangePreview{}, err
	}
	return p.planChangePreview, nil
}

// ChangePlan memoizes one deterministic operation per idempotency key.
func (p *Provider) ChangePlan(_ context.Context, request bursar.ProviderPlanChangeRequest) (bursar.ProviderPlanChangeResult, error) {
	if p == nil {
		return bursar.ProviderPlanChangeResult{}, mockUninitializedError()
	}
	if err := validateMockPlanChange(request, true); err != nil {
		return bursar.ProviderPlanChangeResult{}, err
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	if result, exists := p.planChanges[request.IdempotencyKey]; exists {
		return result, nil
	}
	result := bursar.ProviderPlanChangeResult{ProviderOperationID: "mock_plan_change_" + stableMockSuffix(request.IdempotencyKey)}
	p.planChanges[request.IdempotencyKey] = result
	return result, nil
}

// CancelScheduledPlanChange validates a replay-safe deterministic operation.
func (p *Provider) CancelScheduledPlanChange(_ context.Context, subscriptionID, _ string, idempotencyKey string) error {
	return validateMockSubscriptionOperation(subscriptionID, idempotencyKey)
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
	if event.EventID == "" {
		event.EventID = event.ID
	}
	if event.ID == "" {
		event.ID = event.EventID
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

func mockUninitializedError() *bursar.BursarError {
	return bursar.NewError("mock provider is not initialized", bursar.ErrorOptions{Code: bursar.ErrorCodeProviderResponseInvalid, Category: bursar.ErrorCategoryInternal})
}

func requireMockText(value, field string) error {
	if strings.TrimSpace(value) == "" {
		return bursar.NewError("mock "+field+" is required", bursar.ErrorOptions{Code: bursar.ErrorCodeConfig, Category: bursar.ErrorCategoryInvalidRequest})
	}
	return nil
}

func validateMockSubscriptionOperation(subscriptionID, idempotencyKey string) error {
	if err := requireMockText(subscriptionID, "subscription ID"); err != nil {
		return err
	}
	return requireMockText(idempotencyKey, "idempotency key")
}

func validateMockSavedPayment(params bursar.SavedPaymentChargeParams, requireKey bool) error {
	for _, input := range []struct{ value, field string }{{params.CustomerID, "customer ID"}, {params.PaymentMethodID, "payment method ID"}, {params.ProductID, "product ID"}} {
		if err := requireMockText(input.value, input.field); err != nil {
			return err
		}
	}
	if params.Quantity < 1 {
		return bursar.NewError("mock saved payment quantity must be positive", bursar.ErrorOptions{Code: bursar.ErrorCodeConfig, Category: bursar.ErrorCategoryInvalidRequest})
	}
	if requireKey {
		return requireMockText(params.IdempotencyKey, "idempotency key")
	}
	return nil
}

func validateMockPlanChange(request bursar.ProviderPlanChangeRequest, requireKey bool) error {
	if err := requireMockText(request.ProviderSubscriptionID, "subscription ID"); err != nil {
		return err
	}
	if err := requireMockText(request.ProductID, "product ID"); err != nil {
		return err
	}
	if request.Quantity < 1 {
		return bursar.NewError("mock plan change quantity must be positive", bursar.ErrorOptions{Code: bursar.ErrorCodeConfig, Category: bursar.ErrorCategoryInvalidRequest})
	}
	if requireKey {
		return requireMockText(request.IdempotencyKey, "idempotency key")
	}
	return nil
}

func stableMockSuffix(key string) string {
	digest := sha256.Sum256([]byte(strings.TrimSpace(key)))
	return hex.EncodeToString(digest[:8])
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
