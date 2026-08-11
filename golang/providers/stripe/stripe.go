// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

// Package stripe adapts Stripe's maintained Go SDK to Bursar's portable
// PaymentProvider contract. It deliberately exposes no HTTP framework layer:
// callers pass raw bytes and headers from net/http, Chi, Gin, Fiber, or any
// other transport to HandleWebhook.
package stripe

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"time"

	bursar "github.com/Zonastery/bursar/golang/v2"
	"github.com/Zonastery/bursar/golang/v2/providers/internal/normalize"
	"github.com/shopspring/decimal"
	stripego "github.com/stripe/stripe-go/v84"
)

const ProviderName = "stripe"

// Options configures a Stripe provider. Client is useful when an application
// supplies a fully configured stripe-go client (for example, a custom HTTP
// transport); otherwise APIKey creates Stripe's standard concurrent client.
// WebhookSecret is always required because Bursar never accepts an unsigned
// provider event.
type Options struct {
	APIKey        string
	WebhookSecret string
	Client        *stripego.Client
}

// Provider uses Stripe's official client for checkout requests and official
// webhook verifier for signature validation.
type Provider struct {
	client        *stripego.Client
	webhookSecret string
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

// New constructs a reusable Stripe provider.
func New(options Options) (*Provider, error) {
	webhookSecret := strings.TrimSpace(options.WebhookSecret)
	if webhookSecret == "" {
		return nil, bursar.NewError("Stripe webhook secret is required", bursar.ErrorOptions{
			Code:     bursar.ErrorCodeConfig,
			Category: bursar.ErrorCategoryInvalidRequest,
		})
	}
	client := options.Client
	if client == nil {
		apiKey := strings.TrimSpace(options.APIKey)
		if apiKey == "" {
			return nil, bursar.NewError("Stripe API key is required when no client is supplied", bursar.ErrorOptions{
				Code:     bursar.ErrorCodeConfig,
				Category: bursar.ErrorCategoryInvalidRequest,
			})
		}
		client = stripego.NewClient(apiKey)
	}
	return &Provider{client: client, webhookSecret: webhookSecret}, nil
}

// Name returns the stable catalog provider key.
func (*Provider) Name() string { return ProviderName }

// CreateCheckoutSession creates a hosted Stripe Checkout Session. ProductID is
// interpreted as a Stripe Price ID, and the Bursar account ID is written to
// both metadata and client_reference_id for webhook reconciliation.
func (p *Provider) CreateCheckoutSession(ctx context.Context, request bursar.CheckoutSessionRequest) (bursar.CheckoutSession, error) {
	if p == nil || p.client == nil {
		return bursar.CheckoutSession{}, bursar.NewError("Stripe provider is not initialized", bursar.ErrorOptions{
			Code:     bursar.ErrorCodeProviderResponseInvalid,
			Category: bursar.ErrorCategoryInternal,
		})
	}
	if err := validateCheckoutRequest(request); err != nil {
		return bursar.CheckoutSession{}, err
	}

	mode := strings.TrimSpace(request.Mode)
	if mode == "" {
		mode = string(stripego.CheckoutSessionModePayment)
	}
	metadata := cloneMetadata(request.Metadata)
	metadata["bursar_account_id"] = request.AccountID
	params := &stripego.CheckoutSessionCreateParams{
		AutomaticTax:      &stripego.CheckoutSessionCreateAutomaticTaxParams{Enabled: stripego.Bool(true)},
		CancelURL:         stripego.String(request.CancelURL),
		ClientReferenceID: stripego.String(request.AccountID),
		CustomerEmail:     optionalString(request.CustomerEmail),
		Customer:          optionalString(request.CustomerID),
		LineItems: []*stripego.CheckoutSessionCreateLineItemParams{{
			Price:    stripego.String(request.ProductID),
			Quantity: stripego.Int64(request.Quantity),
		}},
		Metadata:   metadata,
		Mode:       stripego.String(mode),
		SuccessURL: stripego.String(request.SuccessURL),
	}
	if mode == string(stripego.CheckoutSessionModeSubscription) {
		params.SubscriptionData = &stripego.CheckoutSessionCreateSubscriptionDataParams{Metadata: cloneMetadata(metadata)}
	} else if mode == string(stripego.CheckoutSessionModePayment) {
		params.PaymentIntentData = &stripego.CheckoutSessionCreatePaymentIntentDataParams{Metadata: cloneMetadata(metadata)}
	}
	params.SetIdempotencyKey(request.IdempotencyKey)

	session, err := p.client.V1CheckoutSessions.Create(ctx, params)
	if err != nil {
		return bursar.CheckoutSession{}, bursar.NewError("create Stripe checkout session", bursar.ErrorOptions{
			Code:          bursar.ErrorCodeProviderResponseInvalid,
			Category:      bursar.ErrorCategoryUnavailable,
			Retryable:     true,
			Indeterminate: true,
			Cause:         err,
		})
	}
	if session == nil || strings.TrimSpace(session.ID) == "" || strings.TrimSpace(session.URL) == "" {
		return bursar.CheckoutSession{}, bursar.NewError("Stripe returned an incomplete checkout session", bursar.ErrorOptions{
			Code:     bursar.ErrorCodeProviderResponseInvalid,
			Category: bursar.ErrorCategoryUnavailable,
		})
	}
	customerID := ""
	if session.Customer != nil {
		customerID = session.Customer.ID
	}
	return bursar.CheckoutSession{ID: session.ID, URL: session.URL, CustomerID: customerID}, nil
}

// GetCheckoutSessionStatus returns Stripe's current provider status for a
// previously created checkout session.
func (p *Provider) GetCheckoutSessionStatus(ctx context.Context, sessionID string) (string, error) {
	if p == nil || p.client == nil {
		return "", bursar.NewError("Stripe provider is not initialized", bursar.ErrorOptions{Code: bursar.ErrorCodeProviderResponseInvalid})
	}
	var err error
	if sessionID, err = requireStripeInputText(sessionID, "checkout session ID"); err != nil {
		return "", err
	}
	params := &stripego.CheckoutSessionRetrieveParams{}
	params.AddExpand("payment_intent")
	session, err := p.client.V1CheckoutSessions.Retrieve(ctx, sessionID, params)
	if err != nil {
		return "", stripeRequestError("retrieve checkout session", err, false)
	}
	if session == nil {
		return "", stripeResponseError("retrieve checkout session", "session")
	}
	return stripeCheckoutStatus(session)
}

// CreateCustomerPortalSession creates Stripe's hosted customer-management
// session. CommerceService scopes customerID to an account before this method
// receives it, so the provider never authorizes a browser-supplied ID.
func (p *Provider) CreateCustomerPortalSession(ctx context.Context, customerID, returnURL string) (string, error) {
	if p == nil || p.client == nil {
		return "", stripeUninitializedError()
	}
	var err error
	if customerID, err = requireStripeInputText(customerID, "customer ID"); err != nil {
		return "", err
	}
	if returnURL, err = requireStripeInputText(returnURL, "customer portal return URL"); err != nil {
		return "", err
	}
	session, err := p.client.V1BillingPortalSessions.Create(ctx, &stripego.BillingPortalSessionCreateParams{
		Customer:  stripego.String(customerID),
		ReturnURL: stripego.String(returnURL),
	})
	if err != nil {
		return "", stripeRequestError("create customer portal session", err, false)
	}
	if session == nil {
		return "", stripeResponseError("create customer portal session", "session")
	}
	return requireStripeResponseText(session.URL, "create customer portal session", "url")
}

// CreateUpdatePaymentMethodSession starts Stripe Billing Portal's hosted
// payment-method update flow for an existing subscription. Stripe's flow is
// customer-scoped; subscriptionID is validated here because CommerceService
// resolved it from durable account-owned state before the call.
func (p *Provider) CreateUpdatePaymentMethodSession(ctx context.Context, customerID, subscriptionID, returnURL string) (string, error) {
	if p == nil || p.client == nil {
		return "", stripeUninitializedError()
	}
	var err error
	if customerID, err = requireStripeInputText(customerID, "customer ID"); err != nil {
		return "", err
	}
	if _, err = requireStripeInputText(subscriptionID, "subscription ID"); err != nil {
		return "", err
	}
	if returnURL, err = requireStripeInputText(returnURL, "payment method return URL"); err != nil {
		return "", err
	}
	session, err := p.client.V1BillingPortalSessions.Create(ctx, &stripego.BillingPortalSessionCreateParams{
		Customer:  stripego.String(customerID),
		ReturnURL: stripego.String(returnURL),
		FlowData: &stripego.BillingPortalSessionCreateFlowDataParams{
			Type: stripego.String("payment_method_update"),
		},
	})
	if err != nil {
		return "", stripeRequestError("create update payment method session", err, false)
	}
	if session == nil {
		return "", stripeResponseError("create update payment method session", "session")
	}
	return requireStripeResponseText(session.URL, "create update payment method session", "url")
}

// CreatePaymentMethodSetupSession creates a hosted Checkout setup session so
// Stripe can attach a card to the already authorized customer for later use.
func (p *Provider) CreatePaymentMethodSetupSession(ctx context.Context, customerID, returnURL, cancelURL string) (string, error) {
	if p == nil || p.client == nil {
		return "", stripeUninitializedError()
	}
	var err error
	if customerID, err = requireStripeInputText(customerID, "customer ID"); err != nil {
		return "", err
	}
	if returnURL, err = requireStripeInputText(returnURL, "payment method return URL"); err != nil {
		return "", err
	}
	if cancelURL = strings.TrimSpace(cancelURL); cancelURL == "" {
		cancelURL = returnURL
	}
	session, err := p.client.V1CheckoutSessions.Create(ctx, &stripego.CheckoutSessionCreateParams{
		CancelURL:          stripego.String(cancelURL),
		Customer:           stripego.String(customerID),
		Mode:               stripego.String(string(stripego.CheckoutSessionModeSetup)),
		PaymentMethodTypes: []*string{stripego.String("card")},
		SuccessURL:         stripego.String(returnURL),
	})
	if err != nil {
		return "", stripeRequestError("create payment method setup session", err, true)
	}
	if session == nil {
		return "", stripeResponseError("create payment method setup session", "session")
	}
	return requireStripeResponseText(session.URL, "create payment method setup session", "url")
}

// CreateCustomer creates a Stripe customer for a previously authorized Bursar
// account. CustomerProvider has no idempotency key, so an indeterminate
// response is explicit and callers must reconcile it before retrying.
func (p *Provider) CreateCustomer(ctx context.Context, email, name string, metadata map[string]string) (string, error) {
	if p == nil || p.client == nil {
		return "", stripeUninitializedError()
	}
	var err error
	if email, err = requireStripeInputText(email, "customer email"); err != nil {
		return "", err
	}
	if name, err = requireStripeInputText(name, "customer name"); err != nil {
		return "", err
	}
	customer, err := p.client.V1Customers.Create(ctx, &stripego.CustomerCreateParams{
		Email:    stripego.String(email),
		Name:     stripego.String(name),
		Metadata: cloneMetadata(metadata),
	})
	if err != nil {
		return "", stripeRequestError("create customer", err, true)
	}
	if customer == nil {
		return "", stripeResponseError("create customer", "customer")
	}
	return requireStripeResponseText(customer.ID, "create customer", "id")
}

// CancelSubscription asks Stripe to retain the subscription through its
// current period. The final state remains webhook-owned by Bursar.
func (p *Provider) CancelSubscription(ctx context.Context, subscriptionID, idempotencyKey string) error {
	if p == nil || p.client == nil {
		return stripeUninitializedError()
	}
	var err error
	if subscriptionID, err = requireStripeInputText(subscriptionID, "subscription ID"); err != nil {
		return err
	}
	if idempotencyKey, err = requireStripeIdempotencyKey(idempotencyKey); err != nil {
		return err
	}
	params := &stripego.SubscriptionUpdateParams{CancelAtPeriodEnd: stripego.Bool(true)}
	params.SetIdempotencyKey(idempotencyKey)
	if _, err = p.client.V1Subscriptions.Update(ctx, subscriptionID, params); err != nil {
		return stripeRequestError("cancel subscription", err, true)
	}
	return nil
}

// ReactivateSubscription removes a pending Stripe period-end cancellation.
func (p *Provider) ReactivateSubscription(ctx context.Context, subscriptionID, idempotencyKey string) error {
	if p == nil || p.client == nil {
		return stripeUninitializedError()
	}
	var err error
	if subscriptionID, err = requireStripeInputText(subscriptionID, "subscription ID"); err != nil {
		return err
	}
	if idempotencyKey, err = requireStripeIdempotencyKey(idempotencyKey); err != nil {
		return err
	}
	params := &stripego.SubscriptionUpdateParams{CancelAtPeriodEnd: stripego.Bool(false)}
	params.SetIdempotencyKey(idempotencyKey)
	if _, err = p.client.V1Subscriptions.Update(ctx, subscriptionID, params); err != nil {
		return stripeRequestError("reactivate subscription", err, true)
	}
	return nil
}

// ListPaymentMethods returns customer-attached cards only. Card details are
// intentionally reduced to a customer-safe display projection.
func (p *Provider) ListPaymentMethods(ctx context.Context, customerID string) ([]bursar.PaymentMethodInfo, error) {
	if p == nil || p.client == nil {
		return nil, stripeUninitializedError()
	}
	var err error
	if customerID, err = requireStripeInputText(customerID, "customer ID"); err != nil {
		return nil, err
	}
	customer, err := p.client.V1Customers.Retrieve(ctx, customerID, nil)
	if err != nil {
		return nil, stripeRequestError("retrieve customer payment methods", err, false)
	}
	if customer == nil {
		return nil, stripeResponseError("retrieve customer payment methods", "customer")
	}
	if customer.Deleted {
		return []bursar.PaymentMethodInfo{}, nil
	}
	defaultID := ""
	if customer.InvoiceSettings != nil && customer.InvoiceSettings.DefaultPaymentMethod != nil {
		defaultID = strings.TrimSpace(customer.InvoiceSettings.DefaultPaymentMethod.ID)
	}
	methods := make([]bursar.PaymentMethodInfo, 0)
	seen := make(map[string]int)
	for paymentMethod, listErr := range p.client.V1PaymentMethods.List(ctx, &stripego.PaymentMethodListParams{
		Customer: stripego.String(customerID),
		Type:     stripego.String("card"),
	}) {
		if listErr != nil {
			return nil, stripeRequestError("list payment methods", listErr, false)
		}
		if paymentMethod == nil || paymentMethod.Card == nil {
			return nil, stripeResponseError("list payment methods", "card")
		}
		methodID, responseErr := requireStripeResponseText(paymentMethod.ID, "list payment methods", "id")
		if responseErr != nil {
			return nil, responseErr
		}
		last4, responseErr := requireStripeCardLast4(paymentMethod.Card.Last4)
		if responseErr != nil {
			return nil, responseErr
		}
		brand, responseErr := requireStripeResponseText(string(paymentMethod.Card.Brand), "list payment methods", "card.brand")
		if responseErr != nil {
			return nil, responseErr
		}
		expiryMonth, responseErr := stripeCardInteger(paymentMethod.Card.ExpMonth, "card.exp_month", 1, 12)
		if responseErr != nil {
			return nil, responseErr
		}
		expiryYear, responseErr := stripeCardInteger(paymentMethod.Card.ExpYear, "card.exp_year", 1, 9999)
		if responseErr != nil {
			return nil, responseErr
		}
		method := bursar.PaymentMethodInfo{ID: methodID, Last4: last4, Brand: brand, ExpiryMonth: expiryMonth, ExpiryYear: expiryYear, IsDefault: methodID == defaultID}
		if index, duplicate := seen[methodID]; duplicate {
			methods[index].IsDefault = methods[index].IsDefault || method.IsDefault
			continue
		}
		seen[methodID] = len(methods)
		methods = append(methods, method)
	}
	return methods, nil
}

// PreviewSavedPaymentCharge returns the exact fixed Stripe Price amount that
// would be used by ChargeSavedPaymentMethod. Stripe's Price API does not
// provide a separate cart preview for a standalone PaymentIntent.
func (p *Provider) PreviewSavedPaymentCharge(ctx context.Context, params bursar.SavedPaymentChargeParams) (bursar.SavedPaymentChargeQuote, error) {
	if p == nil || p.client == nil {
		return bursar.SavedPaymentChargeQuote{}, stripeUninitializedError()
	}
	if err := validateStripeSavedPaymentChargeParams(params); err != nil {
		return bursar.SavedPaymentChargeQuote{}, err
	}
	price, err := p.retrieveFixedPrice(ctx, params.ProductID, "preview saved payment charge", true)
	if err != nil {
		return bursar.SavedPaymentChargeQuote{}, err
	}
	amount, err := stripeMultiplyMinor(price.UnitAmount, params.Quantity, "preview saved payment charge")
	if err != nil {
		return bursar.SavedPaymentChargeQuote{}, err
	}
	currency, err := requireStripeCurrency(string(price.Currency), "preview saved payment charge", "price.currency")
	if err != nil {
		return bursar.SavedPaymentChargeQuote{}, err
	}
	return bursar.SavedPaymentChargeQuote{AmountMinor: amount, Currency: currency}, nil
}

// ChargeSavedPaymentMethod creates and confirms an off-session Stripe
// PaymentIntent using the catalog-resolved fixed Price and saved card. The
// provider idempotency key is supplied by Bursar's durable charge attempt.
func (p *Provider) ChargeSavedPaymentMethod(ctx context.Context, params bursar.SavedPaymentChargeParams) (bursar.SavedPaymentChargeResult, error) {
	if p == nil || p.client == nil {
		return bursar.SavedPaymentChargeResult{}, stripeUninitializedError()
	}
	if err := validateStripeSavedPaymentChargeParams(params); err != nil {
		return bursar.SavedPaymentChargeResult{}, err
	}
	price, err := p.retrieveFixedPrice(ctx, params.ProductID, "charge saved payment method", true)
	if err != nil {
		return bursar.SavedPaymentChargeResult{}, err
	}
	amount, err := stripeMultiplyMinor(price.UnitAmount, params.Quantity, "charge saved payment method")
	if err != nil {
		return bursar.SavedPaymentChargeResult{}, err
	}
	if amount < 1 {
		return bursar.SavedPaymentChargeResult{}, stripeResponseError("charge saved payment method", "price.unit_amount")
	}
	metadata := cloneMetadata(params.Metadata)
	metadata["price_id"] = strings.TrimSpace(params.ProductID)
	intentParams := &stripego.PaymentIntentCreateParams{
		Amount:        stripego.Int64(amount),
		Confirm:       stripego.Bool(true),
		Currency:      stripego.String(string(price.Currency)),
		Customer:      stripego.String(strings.TrimSpace(params.CustomerID)),
		Metadata:      metadata,
		OffSession:    stripego.Bool(true),
		PaymentMethod: stripego.String(strings.TrimSpace(params.PaymentMethodID)),
		ReturnURL:     optionalString(params.ReturnURL),
	}
	intentParams.SetIdempotencyKey(strings.TrimSpace(params.IdempotencyKey))
	intent, err := p.client.V1PaymentIntents.Create(ctx, intentParams)
	if err != nil {
		return bursar.SavedPaymentChargeResult{}, stripeRequestError("charge saved payment method", err, true)
	}
	if intent == nil {
		return bursar.SavedPaymentChargeResult{}, stripeResponseError("charge saved payment method", "payment_intent")
	}
	paymentID, err := requireStripeResponseText(intent.ID, "charge saved payment method", "id")
	if err != nil {
		return bursar.SavedPaymentChargeResult{}, err
	}
	status, err := stripeSavedPaymentStatus(intent.Status)
	if err != nil {
		return bursar.SavedPaymentChargeResult{}, err
	}
	currency, err := requireStripeCurrency(string(intent.Currency), "charge saved payment method", "currency")
	if err != nil {
		return bursar.SavedPaymentChargeResult{}, err
	}
	chargedAmount := intent.Amount
	return bursar.SavedPaymentChargeResult{
		ProviderPaymentID: paymentID,
		Status:            status,
		ActionURL:         stripePaymentIntentActionURL(intent),
		AmountMinor:       &chargedAmount,
		Currency:          currency,
	}, nil
}

// GetInvoiceURL returns Stripe's hosted invoice URL. A draft invoice has no
// hosted URL, which CommerceService maps to its account-scoped not-found path.
func (p *Provider) GetInvoiceURL(ctx context.Context, invoiceID string) (string, error) {
	if p == nil || p.client == nil {
		return "", stripeUninitializedError()
	}
	var err error
	if invoiceID, err = requireStripeInputText(invoiceID, "invoice ID"); err != nil {
		return "", err
	}
	invoice, err := p.client.V1Invoices.Retrieve(ctx, invoiceID, nil)
	if err != nil {
		return "", stripeRequestError("retrieve invoice URL", err, false)
	}
	if invoice == nil {
		return "", stripeResponseError("retrieve invoice URL", "invoice")
	}
	return strings.TrimSpace(invoice.HostedInvoiceURL), nil
}

// PreviewPlanChange obtains Stripe's provider-authored invoice preview before
// a Bursar plan-change confirmation can be accepted. All returned money is
// kept as integer minor units converted directly to decimal.Amount values.
func (p *Provider) PreviewPlanChange(ctx context.Context, request bursar.ProviderPlanChangeRequest) (bursar.PlanChangePreview, error) {
	if p == nil || p.client == nil {
		return bursar.PlanChangePreview{}, stripeUninitializedError()
	}
	request, err := normalizeStripePlanChangeRequest(request, false)
	if err != nil {
		return bursar.PlanChangePreview{}, err
	}
	subscription, err := p.client.V1Subscriptions.Retrieve(ctx, request.ProviderSubscriptionID, nil)
	if err != nil {
		return bursar.PlanChangePreview{}, stripeRequestError("retrieve subscription for plan preview", err, false)
	}
	item, err := stripeSubscriptionItem(subscription, "preview plan change")
	if err != nil {
		return bursar.PlanChangePreview{}, err
	}
	if subscription.Customer == nil {
		return bursar.PlanChangePreview{}, stripeResponseError("preview plan change", "subscription.customer")
	}
	customerID, err := requireStripeResponseText(subscription.Customer.ID, "preview plan change", "subscription.customer")
	if err != nil {
		return bursar.PlanChangePreview{}, err
	}
	proration, err := stripeProrationBehavior(request.ProrationBillingMode)
	if err != nil {
		return bursar.PlanChangePreview{}, err
	}
	if request.EffectiveAt == "next_billing_date" {
		proration = "none"
	}
	invoice, err := p.client.V1Invoices.CreatePreview(ctx, &stripego.InvoiceCreatePreviewParams{
		Customer:     stripego.String(customerID),
		Subscription: stripego.String(request.ProviderSubscriptionID),
		SubscriptionDetails: &stripego.InvoiceCreatePreviewSubscriptionDetailsParams{
			Items: []*stripego.InvoiceCreatePreviewSubscriptionDetailsItemParams{{
				ID:       stripego.String(item.ID),
				Price:    stripego.String(request.ProductID),
				Quantity: stripego.Int64(request.Quantity),
			}},
			ProrationBehavior: stripego.String(proration),
		},
	})
	if err != nil {
		return bursar.PlanChangePreview{}, stripeRequestError("preview plan change", err, false)
	}
	if invoice == nil {
		return bursar.PlanChangePreview{}, stripeResponseError("preview plan change", "invoice")
	}
	targetPrice, err := p.retrieveFixedPrice(ctx, request.ProductID, "preview plan change", true)
	if err != nil {
		return bursar.PlanChangePreview{}, err
	}
	return p.stripePlanChangePreview(ctx, invoice, targetPrice, item, request.EffectiveAt)
}

// ChangePlan submits an immediate subscription update or creates a two-phase
// Stripe Subscription Schedule for a renewal-effective change. The operation
// key is deterministically scoped per Stripe mutation so a retry reuses the
// same remote operation even if a prior response was lost.
func (p *Provider) ChangePlan(ctx context.Context, request bursar.ProviderPlanChangeRequest) (bursar.ProviderPlanChangeResult, error) {
	if p == nil || p.client == nil {
		return bursar.ProviderPlanChangeResult{}, stripeUninitializedError()
	}
	request, err := normalizeStripePlanChangeRequest(request, true)
	if err != nil {
		return bursar.ProviderPlanChangeResult{}, err
	}
	subscription, err := p.client.V1Subscriptions.Retrieve(ctx, request.ProviderSubscriptionID, nil)
	if err != nil {
		return bursar.ProviderPlanChangeResult{}, stripeRequestError("retrieve subscription for plan change", err, false)
	}
	item, err := stripeSubscriptionItem(subscription, "change plan")
	if err != nil {
		return bursar.ProviderPlanChangeResult{}, err
	}
	if request.EffectiveAt == "next_billing_date" {
		if subscription.Schedule != nil && strings.TrimSpace(subscription.Schedule.ID) != "" {
			return bursar.ProviderPlanChangeResult{}, bursar.NewError("Stripe subscription already has a schedule", bursar.ErrorOptions{
				Code:     bursar.ErrorCodeOperationNotAllowed,
				Category: bursar.ErrorCategoryConflict,
				Details:  map[string]any{"provider": ProviderName, "operation": "change plan"},
			})
		}
		return p.schedulePlanChange(ctx, request)
	}
	proration, err := stripeProrationBehavior(request.ProrationBillingMode)
	if err != nil {
		return bursar.ProviderPlanChangeResult{}, err
	}
	paymentBehavior, err := stripePaymentBehavior(request.PaymentFailure)
	if err != nil {
		return bursar.ProviderPlanChangeResult{}, err
	}
	updateKey, err := stripeScopedIdempotencyKey(request.IdempotencyKey, "subscription-update")
	if err != nil {
		return bursar.ProviderPlanChangeResult{}, err
	}
	params := &stripego.SubscriptionUpdateParams{
		Items: []*stripego.SubscriptionUpdateItemParams{{
			ID:       stripego.String(item.ID),
			Price:    stripego.String(request.ProductID),
			Quantity: stripego.Int64(request.Quantity),
		}},
		Metadata:          cloneMetadata(request.Metadata),
		PaymentBehavior:   stripego.String(paymentBehavior),
		ProrationBehavior: stripego.String(proration),
	}
	params.SetIdempotencyKey(updateKey)
	updated, err := p.client.V1Subscriptions.Update(ctx, request.ProviderSubscriptionID, params)
	if err != nil {
		return bursar.ProviderPlanChangeResult{}, stripeRequestError("change plan", err, true)
	}
	if updated == nil {
		return bursar.ProviderPlanChangeResult{}, stripeResponseError("change plan", "subscription")
	}
	result := bursar.ProviderPlanChangeResult{}
	if updated.LatestInvoice != nil {
		result.ProviderOperationID = strings.TrimSpace(updated.LatestInvoice.ID)
	}
	return result, nil
}

// CancelScheduledPlanChange releases the Stripe Subscription Schedule created
// for a renewal-effective plan change while preserving the active underlying
// subscription. subscriptionID is still validated because CommerceService
// resolved it from durable account-owned state before reaching this adapter.
func (p *Provider) CancelScheduledPlanChange(ctx context.Context, subscriptionID, providerOperationID, idempotencyKey string) error {
	if p == nil || p.client == nil {
		return stripeUninitializedError()
	}
	var err error
	if _, err = requireStripeInputText(subscriptionID, "subscription ID"); err != nil {
		return err
	}
	if providerOperationID, err = requireStripeInputText(providerOperationID, "scheduled plan change ID"); err != nil {
		return err
	}
	if idempotencyKey, err = requireStripeIdempotencyKey(idempotencyKey); err != nil {
		return err
	}
	params := &stripego.SubscriptionScheduleReleaseParams{}
	params.SetIdempotencyKey(idempotencyKey)
	if _, err = p.client.V1SubscriptionSchedules.Release(ctx, providerOperationID, params); err != nil {
		return stripeRequestError("cancel scheduled plan change", err, true)
	}
	return nil
}

func (p *Provider) schedulePlanChange(ctx context.Context, request bursar.ProviderPlanChangeRequest) (bursar.ProviderPlanChangeResult, error) {
	createKey, err := stripeScopedIdempotencyKey(request.IdempotencyKey, "schedule-create")
	if err != nil {
		return bursar.ProviderPlanChangeResult{}, err
	}
	createParams := &stripego.SubscriptionScheduleCreateParams{FromSubscription: stripego.String(request.ProviderSubscriptionID)}
	createParams.SetIdempotencyKey(createKey)
	schedule, err := p.client.V1SubscriptionSchedules.Create(ctx, createParams)
	if err != nil {
		return bursar.ProviderPlanChangeResult{}, stripeRequestError("create scheduled plan change", err, true)
	}
	if schedule == nil {
		return bursar.ProviderPlanChangeResult{}, stripeResponseError("create scheduled plan change", "schedule")
	}
	scheduleID, err := requireStripeResponseText(schedule.ID, "create scheduled plan change", "schedule.id")
	if err != nil {
		return bursar.ProviderPlanChangeResult{}, err
	}
	if len(schedule.Phases) != 1 || schedule.Phases[0] == nil {
		return bursar.ProviderPlanChangeResult{}, stripeResponseError("change plan", "schedule.phases")
	}
	currentPhase, err := stripeSchedulePhaseParams(schedule.Phases[0])
	if err != nil {
		return bursar.ProviderPlanChangeResult{}, err
	}
	if schedule.Phases[0].EndDate <= 0 {
		return bursar.ProviderPlanChangeResult{}, stripeResponseError("change plan", "schedule.phases.end_date")
	}
	updateKey, err := stripeScopedIdempotencyKey(request.IdempotencyKey, "schedule-update")
	if err != nil {
		return bursar.ProviderPlanChangeResult{}, err
	}
	updateParams := &stripego.SubscriptionScheduleUpdateParams{
		Phases: []*stripego.SubscriptionScheduleUpdatePhaseParams{
			currentPhase,
			{
				Items: []*stripego.SubscriptionScheduleUpdatePhaseItemParams{{
					Price:    stripego.String(request.ProductID),
					Quantity: stripego.Int64(request.Quantity),
				}},
				Metadata:          cloneMetadata(request.Metadata),
				ProrationBehavior: stripego.String("none"),
				StartDate:         stripego.Int64(schedule.Phases[0].EndDate),
			},
		},
		ProrationBehavior: stripego.String("none"),
	}
	updateParams.SetIdempotencyKey(updateKey)
	if _, err = p.client.V1SubscriptionSchedules.Update(ctx, scheduleID, updateParams); err != nil {
		return bursar.ProviderPlanChangeResult{}, stripeRequestError("schedule plan change", err, true)
	}
	return bursar.ProviderPlanChangeResult{ProviderOperationID: scheduleID}, nil
}

func (p *Provider) stripePlanChangePreview(ctx context.Context, invoice *stripego.Invoice, targetPrice *stripego.Price, item *stripego.SubscriptionItem, effectiveAt string) (bursar.PlanChangePreview, error) {
	if invoice == nil || targetPrice == nil || item == nil {
		return bursar.PlanChangePreview{}, stripeResponseError("preview plan change", "response")
	}
	if invoice.Total < 0 || invoice.AmountDue < 0 || item.CurrentPeriodEnd <= 0 {
		return bursar.PlanChangePreview{}, stripeResponseError("preview plan change", "invoice")
	}
	currency, err := requireStripeCurrency(string(invoice.Currency), "preview plan change", "invoice.currency")
	if err != nil {
		return bursar.PlanChangePreview{}, err
	}
	recurringCurrency, err := requireStripeCurrency(string(targetPrice.Currency), "preview plan change", "price.currency")
	if err != nil {
		return bursar.PlanChangePreview{}, err
	}
	if invoice.Lines == nil {
		return bursar.PlanChangePreview{}, stripeResponseError("preview plan change", "invoice.lines")
	}
	nextBillingDate := time.Unix(item.CurrentPeriodEnd, 0).UTC()
	previewEffectiveAt := nextBillingDate
	if effectiveAt == "immediately" {
		if invoice.Created <= 0 {
			return bursar.PlanChangePreview{}, stripeResponseError("preview plan change", "invoice.created")
		}
		previewEffectiveAt = time.Unix(invoice.Created, 0).UTC()
	}
	lineItems := make([]bursar.PlanChangeLineItem, 0, len(invoice.Lines.Data))
	prices := make(map[string]*stripego.Price)
	for _, line := range invoice.Lines.Data {
		if line == nil || line.Parent == nil || line.Parent.SubscriptionItemDetails == nil {
			continue
		}
		if line.Pricing == nil || line.Pricing.PriceDetails == nil {
			return bursar.PlanChangePreview{}, stripeResponseError("preview plan change", "invoice.lines.pricing.price_details")
		}
		priceID, err := requireStripeResponseText(line.Pricing.PriceDetails.Price, "preview plan change", "invoice.lines.pricing.price_details.price")
		if err != nil {
			return bursar.PlanChangePreview{}, err
		}
		linePrice := prices[priceID]
		if linePrice == nil {
			linePrice, err = p.retrieveFixedPrice(ctx, priceID, "preview plan change", false)
			if err != nil {
				return bursar.PlanChangePreview{}, err
			}
			prices[priceID] = linePrice
		}
		quantity := line.Quantity
		if quantity == 0 {
			quantity = 1
		}
		if quantity < 1 {
			return bursar.PlanChangePreview{}, stripeResponseError("preview plan change", "invoice.lines.quantity")
		}
		name, err := requireStripeResponseText(line.Description, "preview plan change", "invoice.lines.description")
		if err != nil {
			return bursar.PlanChangePreview{}, err
		}
		lineCurrency, err := requireStripeCurrency(string(line.Currency), "preview plan change", "invoice.lines.currency")
		if err != nil {
			return bursar.PlanChangePreview{}, err
		}
		tax, err := stripeInvoiceLineTax(line, "preview plan change")
		if err != nil {
			return bursar.PlanChangePreview{}, err
		}
		unitPrice := decimal.NewFromInt(linePrice.UnitAmount)
		expectedSubtotal := unitPrice.Mul(decimal.NewFromInt(quantity))
		prorationFactor := decimal.NewFromInt(1)
		if !expectedSubtotal.IsZero() {
			prorationFactor = decimal.NewFromInt(line.Subtotal).DivRound(expectedSubtotal, 32)
		}
		lineItems = append(lineItems, bursar.PlanChangeLineItem{
			ProductID:       priceID,
			Name:            name,
			UnitPrice:       unitPrice,
			Quantity:        quantity,
			ProrationFactor: prorationFactor,
			Currency:        lineCurrency,
			Tax:             decimal.NewFromInt(tax),
			Subtotal:        decimal.NewFromInt(line.Subtotal),
		})
	}
	tax, err := stripeInvoiceTotalTax(invoice, "preview plan change")
	if err != nil {
		return bursar.PlanChangePreview{}, err
	}
	recurringAmount := decimal.NewFromInt(targetPrice.UnitAmount)
	taxAmount := decimal.NewFromInt(tax)
	return bursar.PlanChangePreview{
		TotalAmount:       decimal.NewFromInt(invoice.Total),
		SettlementAmount:  decimal.NewFromInt(invoice.AmountDue),
		Currency:          currency,
		LineItems:         lineItems,
		EffectiveAt:       previewEffectiveAt,
		RecurringAmount:   &recurringAmount,
		RecurringCurrency: recurringCurrency,
		NextBillingDate:   &nextBillingDate,
		TaxAmount:         &taxAmount,
	}, nil
}

// HandleWebhook verifies Stripe-Signature against the exact raw request body
// before decoding and normalizing its event. Invalid signatures are surfaced as
// errors and never enter Bursar's billing-event lifecycle.
func (p *Provider) HandleWebhook(_ context.Context, request bursar.WebhookRequest) (bursar.WebhookResult, error) {
	if p == nil || p.webhookSecret == "" {
		return bursar.WebhookResult{}, bursar.NewError("Stripe provider is not initialized", bursar.ErrorOptions{Code: bursar.ErrorCodeProviderResponseInvalid})
	}
	if len(request.RawBody) == 0 {
		return bursar.WebhookResult{}, bursar.NewError("Stripe webhook body is required", bursar.ErrorOptions{Code: bursar.ErrorCodeConfig, Category: bursar.ErrorCategoryInvalidRequest})
	}
	signature := request.Header.Get("Stripe-Signature")
	if strings.TrimSpace(signature) == "" {
		return bursar.WebhookResult{}, bursar.NewError("Stripe-Signature header is required", bursar.ErrorOptions{Code: bursar.ErrorCodeConfig, Category: bursar.ErrorCategoryInvalidRequest})
	}
	event, err := stripego.ConstructEvent(request.RawBody, signature, p.webhookSecret)
	if err != nil {
		return bursar.WebhookResult{}, bursar.NewError("verify Stripe webhook", bursar.ErrorOptions{
			Code:     bursar.ErrorCodeProviderResponseInvalid,
			Category: bursar.ErrorCategoryInvalidRequest,
			Cause:    err,
		})
	}
	if event.Data == nil {
		return bursar.WebhookResult{}, bursar.NewError("Stripe webhook has no event data", bursar.ErrorOptions{Code: bursar.ErrorCodeProviderResponseInvalid, Category: bursar.ErrorCategoryInvalidRequest})
	}
	object := make(map[string]any, len(event.Data.Object))
	for key, value := range event.Data.Object {
		object[key] = value
	}
	occurredAt := time.Unix(event.Created, 0).UTC()
	if event.Created <= 0 {
		return bursar.WebhookResult{}, bursar.NewError("Stripe webhook has an invalid timestamp", bursar.ErrorOptions{Code: bursar.ErrorCodeProviderResponseInvalid, Category: bursar.ErrorCategoryInvalidRequest})
	}
	normalized := normalize.Event(ProviderName, event.ID, string(event.Type), occurredAt, request.RawBody, object)
	return bursar.WebhookResult{
		Received:  true,
		Provider:  ProviderName,
		EventID:   event.ID,
		EventType: string(event.Type),
		Event:     normalized,
	}, nil
}

func validateCheckoutRequest(request bursar.CheckoutSessionRequest) error {
	if strings.TrimSpace(request.AccountID) == "" || strings.TrimSpace(request.ProductID) == "" {
		return bursar.NewError("checkout account and product IDs are required", bursar.ErrorOptions{Code: bursar.ErrorCodeConfig, Category: bursar.ErrorCategoryInvalidRequest})
	}
	if request.Quantity < 1 {
		return bursar.NewError("checkout quantity must be positive", bursar.ErrorOptions{Code: bursar.ErrorCodeInvalidOfferQuantity, Category: bursar.ErrorCategoryInvalidRequest})
	}
	if strings.TrimSpace(request.SuccessURL) == "" || strings.TrimSpace(request.CancelURL) == "" {
		return bursar.NewError("checkout success and cancel URLs are required", bursar.ErrorOptions{Code: bursar.ErrorCodeConfig, Category: bursar.ErrorCategoryInvalidRequest})
	}
	if strings.TrimSpace(request.IdempotencyKey) == "" {
		return bursar.NewError("checkout idempotency key is required", bursar.ErrorOptions{Code: bursar.ErrorCodeConfig, Category: bursar.ErrorCategoryInvalidRequest})
	}
	return nil
}

func cloneMetadata(metadata map[string]string) map[string]string {
	clone := make(map[string]string, len(metadata)+1)
	for key, value := range metadata {
		clone[key] = value
	}
	return clone
}

func optionalString(value string) *string {
	if strings.TrimSpace(value) == "" {
		return nil
	}
	return stripego.String(value)
}

func stripeUninitializedError() *bursar.BursarError {
	return bursar.NewError("Stripe provider is not initialized", bursar.ErrorOptions{
		Code:     bursar.ErrorCodeProviderResponseInvalid,
		Category: bursar.ErrorCategoryInternal,
	})
}

func requireStripeInputText(value, field string) (string, error) {
	trimmed := strings.TrimSpace(value)
	if trimmed == "" {
		return "", bursar.NewError("Stripe "+field+" is required", bursar.ErrorOptions{
			Code:     bursar.ErrorCodeConfig,
			Category: bursar.ErrorCategoryInvalidRequest,
		})
	}
	return trimmed, nil
}

func requireStripeIdempotencyKey(value string) (string, error) {
	key, err := requireStripeInputText(value, "idempotency key")
	if err != nil {
		return "", err
	}
	if len(key) > 255 {
		return "", bursar.NewError("Stripe idempotency key must be at most 255 characters", bursar.ErrorOptions{
			Code:     bursar.ErrorCodeConfig,
			Category: bursar.ErrorCategoryInvalidRequest,
		})
	}
	return key, nil
}

func stripeScopedIdempotencyKey(value, scope string) (string, error) {
	key, err := requireStripeIdempotencyKey(value)
	if err != nil {
		return "", err
	}
	scope, err = requireStripeInputText(scope, "idempotency key scope")
	if err != nil {
		return "", err
	}
	candidate := key + ":" + scope
	if len(candidate) <= 255 {
		return candidate, nil
	}
	// Stripe caps keys at 255 bytes. Hash only the scoped form so each remote
	// endpoint remains distinct while retries of the same Bursar operation stay
	// stable even when the original key uses its full allowed length.
	digest := sha256.Sum256([]byte(candidate))
	return "bursar:" + hex.EncodeToString(digest[:]), nil
}

func requireStripeResponseText(value, operation, field string) (string, error) {
	trimmed := strings.TrimSpace(value)
	if trimmed == "" {
		return "", stripeResponseError(operation, field)
	}
	return trimmed, nil
}

func requireStripeCurrency(value, operation, field string) (string, error) {
	currency := strings.ToUpper(strings.TrimSpace(value))
	if len(currency) != 3 {
		return "", stripeResponseError(operation, field)
	}
	for _, character := range currency {
		if character < 'A' || character > 'Z' {
			return "", stripeResponseError(operation, field)
		}
	}
	return currency, nil
}

func stripeResponseError(operation, field string) *bursar.BursarError {
	return bursar.NewError("Stripe returned an invalid response for "+operation, bursar.ErrorOptions{
		Code:     bursar.ErrorCodeProviderResponseInvalid,
		Category: bursar.ErrorCategoryUnavailable,
		Details:  map[string]any{"provider": ProviderName, "operation": operation, "field": field},
	})
}

func stripeRequestError(operation string, cause error, mutation bool) *bursar.BursarError {
	options := bursar.ErrorOptions{
		Code:          bursar.ErrorCodeProviderResponseInvalid,
		Category:      bursar.ErrorCategoryUnavailable,
		Retryable:     true,
		Indeterminate: mutation,
		Cause:         cause,
		Details:       map[string]any{"provider": ProviderName, "operation": operation},
	}
	var stripeError *stripego.Error
	if errors.As(cause, &stripeError) && stripeError != nil {
		switch {
		case stripeError.Type == stripego.ErrorTypeCard:
			options.Category = bursar.ErrorCategoryPaymentRequired
			options.Retryable = false
			options.Indeterminate = false
		case stripeError.HTTPStatusCode == 404:
			options.Category = bursar.ErrorCategoryNotFound
			options.Retryable = false
			options.Indeterminate = false
		case stripeError.HTTPStatusCode == 409:
			options.Category = bursar.ErrorCategoryConflict
			options.Retryable = false
			options.Indeterminate = false
		case stripeError.HTTPStatusCode >= 400 && stripeError.HTTPStatusCode < 500:
			options.Retryable = stripeError.HTTPStatusCode == 429
			options.Indeterminate = false
		}
	}
	if errors.Is(cause, context.Canceled) {
		options.Retryable = false
	}
	return bursar.NewError(fmt.Sprintf("Stripe %s", operation), options)
}

func requireStripeCardLast4(value string) (string, error) {
	last4 := strings.TrimSpace(value)
	if len(last4) != 4 {
		return "", stripeResponseError("list payment methods", "card.last4")
	}
	for _, digit := range last4 {
		if digit < '0' || digit > '9' {
			return "", stripeResponseError("list payment methods", "card.last4")
		}
	}
	return last4, nil
}

func stripeCardInteger(value int64, field string, minimum, maximum int) (int, error) {
	if value < int64(minimum) || (maximum > 0 && value > int64(maximum)) {
		return 0, stripeResponseError("list payment methods", field)
	}
	return int(value), nil
}

func validateStripeSavedPaymentChargeParams(params bursar.SavedPaymentChargeParams) error {
	if _, err := requireStripeInputText(params.CustomerID, "customer ID"); err != nil {
		return err
	}
	if _, err := requireStripeInputText(params.PaymentMethodID, "payment method ID"); err != nil {
		return err
	}
	if _, err := requireStripeInputText(params.ProductID, "price ID"); err != nil {
		return err
	}
	if params.Quantity < 1 {
		return bursar.NewError("Stripe saved payment quantity must be positive", bursar.ErrorOptions{
			Code:     bursar.ErrorCodeInvalidOfferQuantity,
			Category: bursar.ErrorCategoryInvalidRequest,
		})
	}
	if _, err := requireStripeIdempotencyKey(params.IdempotencyKey); err != nil {
		return err
	}
	return nil
}

func (p *Provider) retrieveFixedPrice(ctx context.Context, productID, operation string, requireActive bool) (*stripego.Price, error) {
	productID, err := requireStripeInputText(productID, "price ID")
	if err != nil {
		return nil, err
	}
	price, err := p.client.V1Prices.Retrieve(ctx, productID, nil)
	if err != nil {
		return nil, stripeRequestError("retrieve price for "+operation, err, false)
	}
	if price == nil || price.Deleted || (requireActive && !price.Active) || price.BillingScheme != stripego.PriceBillingSchemePerUnit || price.CustomUnitAmount != nil || price.UnitAmount < 0 {
		return nil, stripeResponseError(operation, "price")
	}
	if _, err := requireStripeCurrency(string(price.Currency), operation, "price.currency"); err != nil {
		return nil, err
	}
	return price, nil
}

func stripeMultiplyMinor(unitAmount, quantity int64, operation string) (int64, error) {
	if unitAmount < 0 || quantity < 1 || (unitAmount > 0 && unitAmount > int64(^uint64(0)>>1)/quantity) {
		return 0, stripeResponseError(operation, "price.unit_amount")
	}
	return unitAmount * quantity, nil
}

func stripeSavedPaymentStatus(value stripego.PaymentIntentStatus) (bursar.SavedPaymentChargeStatus, error) {
	switch value {
	case stripego.PaymentIntentStatusSucceeded:
		return bursar.SavedPaymentChargeSucceeded, nil
	case stripego.PaymentIntentStatusProcessing:
		return bursar.SavedPaymentChargeProcessing, nil
	case stripego.PaymentIntentStatusRequiresAction:
		return bursar.SavedPaymentChargeRequiresCustomerAction, nil
	case stripego.PaymentIntentStatusRequiresPaymentMethod:
		return bursar.SavedPaymentChargeRequiresPaymentMethod, nil
	case stripego.PaymentIntentStatusRequiresConfirmation:
		return bursar.SavedPaymentChargeRequiresConfirmation, nil
	case stripego.PaymentIntentStatusRequiresCapture:
		return bursar.SavedPaymentChargeRequiresCapture, nil
	case stripego.PaymentIntentStatusCanceled:
		return bursar.SavedPaymentChargeCancelled, nil
	default:
		return "", stripeResponseError("charge saved payment method", "status")
	}
}

func stripePaymentIntentActionURL(intent *stripego.PaymentIntent) string {
	if intent == nil || intent.NextAction == nil || intent.NextAction.RedirectToURL == nil {
		return ""
	}
	return strings.TrimSpace(intent.NextAction.RedirectToURL.URL)
}

func stripeCheckoutStatus(session *stripego.CheckoutSession) (string, error) {
	if session == nil {
		return "", stripeResponseError("retrieve checkout session", "session")
	}
	switch strings.TrimSpace(string(session.Status)) {
	case "expired":
		return "expired", nil
	}
	switch strings.TrimSpace(string(session.PaymentStatus)) {
	case "paid", "no_payment_required":
		return "succeeded", nil
	}
	if strings.TrimSpace(string(session.Status)) == "open" {
		return "processing", nil
	}
	if session.PaymentIntent != nil {
		switch session.PaymentIntent.Status {
		case stripego.PaymentIntentStatusSucceeded:
			return "succeeded", nil
		case stripego.PaymentIntentStatusProcessing:
			return "processing", nil
		case stripego.PaymentIntentStatusRequiresAction:
			return "requires_customer_action", nil
		case stripego.PaymentIntentStatusRequiresPaymentMethod:
			return "requires_payment_method", nil
		case stripego.PaymentIntentStatusRequiresConfirmation:
			return "requires_confirmation", nil
		case stripego.PaymentIntentStatusRequiresCapture:
			return "requires_capture", nil
		case stripego.PaymentIntentStatusCanceled:
			return "cancelled", nil
		}
	}
	if strings.TrimSpace(string(session.Status)) == "complete" {
		return "complete", nil
	}
	return "", nil
}

func normalizeStripePlanChangeRequest(request bursar.ProviderPlanChangeRequest, mutation bool) (bursar.ProviderPlanChangeRequest, error) {
	var err error
	if request.ProviderSubscriptionID, err = requireStripeInputText(request.ProviderSubscriptionID, "subscription ID"); err != nil {
		return bursar.ProviderPlanChangeRequest{}, err
	}
	if request.ProductID, err = requireStripeInputText(request.ProductID, "price ID"); err != nil {
		return bursar.ProviderPlanChangeRequest{}, err
	}
	if request.Quantity < 1 {
		return bursar.ProviderPlanChangeRequest{}, bursar.NewError("Stripe plan change quantity must be positive", bursar.ErrorOptions{
			Code:     bursar.ErrorCodeInvalidOfferQuantity,
			Category: bursar.ErrorCategoryInvalidRequest,
		})
	}
	if request.EffectiveAt, err = stripeEffectiveAt(request.EffectiveAt); err != nil {
		return bursar.ProviderPlanChangeRequest{}, err
	}
	if _, err = stripeProrationBehavior(request.ProrationBillingMode); err != nil {
		return bursar.ProviderPlanChangeRequest{}, err
	}
	if mutation {
		if request.IdempotencyKey, err = requireStripeIdempotencyKey(request.IdempotencyKey); err != nil {
			return bursar.ProviderPlanChangeRequest{}, err
		}
		if _, err = stripePaymentBehavior(request.PaymentFailure); err != nil {
			return bursar.ProviderPlanChangeRequest{}, err
		}
	}
	return request, nil
}

func stripeEffectiveAt(value string) (string, error) {
	switch strings.TrimSpace(value) {
	case "", "immediately":
		return "immediately", nil
	case "next_billing_date":
		return "next_billing_date", nil
	default:
		return "", bursar.NewError("Stripe plan change effective time is invalid", bursar.ErrorOptions{
			Code:     bursar.ErrorCodeConfig,
			Category: bursar.ErrorCategoryInvalidRequest,
		})
	}
}

func stripeProrationBehavior(value string) (string, error) {
	switch strings.TrimSpace(value) {
	case "", "prorated_immediately":
		return "always_invoice", nil
	case "do_not_bill":
		return "none", nil
	default:
		return "", bursar.NewError("Stripe plan change proration billing mode is invalid", bursar.ErrorOptions{
			Code:     bursar.ErrorCodeConfig,
			Category: bursar.ErrorCategoryInvalidRequest,
		})
	}
}

func stripePaymentBehavior(value string) (string, error) {
	switch strings.TrimSpace(value) {
	case "", "prevent_change":
		return "pending_if_incomplete", nil
	case "apply_change":
		return "allow_incomplete", nil
	default:
		return "", bursar.NewError("Stripe plan change payment failure policy is invalid", bursar.ErrorOptions{
			Code:     bursar.ErrorCodeConfig,
			Category: bursar.ErrorCategoryInvalidRequest,
		})
	}
}

func stripeSubscriptionItem(subscription *stripego.Subscription, operation string) (*stripego.SubscriptionItem, error) {
	if subscription == nil || subscription.Items == nil || len(subscription.Items.Data) != 1 || subscription.Items.Data[0] == nil {
		return nil, stripeResponseError(operation, "subscription.items")
	}
	item := subscription.Items.Data[0]
	if _, err := requireStripeResponseText(item.ID, operation, "subscription.items.id"); err != nil {
		return nil, err
	}
	return item, nil
}

func stripeSchedulePhaseParams(phase *stripego.SubscriptionSchedulePhase) (*stripego.SubscriptionScheduleUpdatePhaseParams, error) {
	if phase == nil || phase.StartDate <= 0 || phase.EndDate <= phase.StartDate || len(phase.Items) == 0 {
		return nil, stripeResponseError("change plan", "schedule.phases")
	}
	items := make([]*stripego.SubscriptionScheduleUpdatePhaseItemParams, 0, len(phase.Items))
	for _, item := range phase.Items {
		if item == nil || item.Price == nil {
			return nil, stripeResponseError("change plan", "schedule.phases.items.price")
		}
		priceID, err := requireStripeResponseText(item.Price.ID, "change plan", "schedule.phases.items.price")
		if err != nil {
			return nil, err
		}
		quantity := item.Quantity
		if quantity == 0 {
			quantity = 1
		}
		if quantity < 1 {
			return nil, stripeResponseError("change plan", "schedule.phases.items.quantity")
		}
		params := &stripego.SubscriptionScheduleUpdatePhaseItemParams{
			Metadata: cloneMetadata(item.Metadata),
			Price:    stripego.String(priceID),
			Quantity: stripego.Int64(quantity),
		}
		for _, taxRate := range item.TaxRates {
			if taxRate == nil {
				return nil, stripeResponseError("change plan", "schedule.phases.items.tax_rates")
			}
			taxRateID, err := requireStripeResponseText(taxRate.ID, "change plan", "schedule.phases.items.tax_rates")
			if err != nil {
				return nil, err
			}
			params.TaxRates = append(params.TaxRates, stripego.String(taxRateID))
		}
		items = append(items, params)
	}
	params := &stripego.SubscriptionScheduleUpdatePhaseParams{
		Items:              items,
		Metadata:           cloneMetadata(phase.Metadata),
		StartDate:          stripego.Int64(phase.StartDate),
		EndDate:            stripego.Int64(phase.EndDate),
		ProrationBehavior:  optionalString(string(phase.ProrationBehavior)),
		BillingCycleAnchor: optionalString(string(phase.BillingCycleAnchor)),
		Currency:           optionalString(string(phase.Currency)),
		Description:        optionalString(phase.Description),
	}
	if phase.AutomaticTax != nil {
		params.AutomaticTax = &stripego.SubscriptionScheduleUpdatePhaseAutomaticTaxParams{Enabled: stripego.Bool(phase.AutomaticTax.Enabled)}
	}
	if phase.CollectionMethod != nil {
		params.CollectionMethod = stripego.String(string(*phase.CollectionMethod))
	}
	if phase.DefaultPaymentMethod != nil {
		paymentMethodID, err := requireStripeResponseText(phase.DefaultPaymentMethod.ID, "change plan", "schedule.phases.default_payment_method")
		if err != nil {
			return nil, err
		}
		params.DefaultPaymentMethod = stripego.String(paymentMethodID)
	}
	for _, taxRate := range phase.DefaultTaxRates {
		if taxRate == nil {
			return nil, stripeResponseError("change plan", "schedule.phases.default_tax_rates")
		}
		taxRateID, err := requireStripeResponseText(taxRate.ID, "change plan", "schedule.phases.default_tax_rates")
		if err != nil {
			return nil, err
		}
		params.DefaultTaxRates = append(params.DefaultTaxRates, stripego.String(taxRateID))
	}
	if phase.TrialEnd > 0 {
		params.TrialEnd = stripego.Int64(phase.TrialEnd)
	}
	return params, nil
}

func stripeInvoiceLineTax(line *stripego.InvoiceLineItem, operation string) (int64, error) {
	if line == nil {
		return 0, stripeResponseError(operation, "invoice.lines")
	}
	var total int64
	for _, item := range line.Taxes {
		if item == nil || item.Amount < 0 || (item.Amount > 0 && total > int64(^uint64(0)>>1)-item.Amount) {
			return 0, stripeResponseError(operation, "invoice.lines.taxes.amount")
		}
		total += item.Amount
	}
	return total, nil
}

func stripeInvoiceTotalTax(invoice *stripego.Invoice, operation string) (int64, error) {
	if invoice == nil {
		return 0, stripeResponseError(operation, "invoice")
	}
	var total int64
	for _, item := range invoice.TotalTaxes {
		if item == nil || item.Amount < 0 || (item.Amount > 0 && total > int64(^uint64(0)>>1)-item.Amount) {
			return 0, stripeResponseError(operation, "invoice.total_taxes.amount")
		}
		total += item.Amount
	}
	return total, nil
}
