// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

// Package dodo adapts Dodo Payments' maintained Go SDK to Bursar's portable
// payment provider contract. It verifies Standard Webhooks signatures through
// Dodo's SDK before a payload is normalized or persisted.
package dodo

import (
	"bytes"
	"context"
	"encoding/json"
	"math"
	"net/http"
	"strconv"
	"strings"

	bursar "github.com/Zonastery/bursar/golang/v2"
	dodopayments "github.com/dodopayments/dodopayments-go"
	"github.com/dodopayments/dodopayments-go/option"
	"github.com/dodopayments/dodopayments-go/shared"
	"github.com/shopspring/decimal"
)

const ProviderName = "dodo"

// Options configures a Dodo Payments provider. Client may be supplied for a
// customized official client; otherwise APIKey and WebhookKey create the
// standard Dodo client. A webhook key is mandatory because unsigned events are
// never allowed into Bursar's event lifecycle.
type Options struct {
	APIKey     string
	WebhookKey string
	// Environment selects Dodo's live or test endpoint. Sandbox uses Dodo's
	// test endpoint while retaining Bursar's distinct sandbox namespace.
	Environment bursar.ProviderEnvironment
	// HTTPClient is used by the official SDK and is primarily useful for
	// deterministic tests and application-owned transports.
	HTTPClient *http.Client
	// SetupProductID is the Dodo product used for a mandate-only checkout when
	// an account has no active subscription to update. It is optional unless
	// CreatePaymentMethodSetupSession is used.
	SetupProductID string
	Client         *dodopayments.Client
}

// Provider is a concurrency-safe adapter around Dodo's official Go client.
type Provider struct {
	client         *dodopayments.Client
	setupProductID string
	environment    bursar.ProviderEnvironment
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

// New constructs a Dodo Payments provider.
func New(options Options) (*Provider, error) {
	webhookKey := strings.TrimSpace(options.WebhookKey)
	if webhookKey == "" {
		return nil, bursar.NewError("Dodo webhook key is required", bursar.ErrorOptions{
			Code:     bursar.ErrorCodeConfig,
			Category: bursar.ErrorCategoryInvalidRequest,
		})
	}
	environment := options.Environment
	if environment == "" {
		environment = bursar.ProviderEnvironmentTest
	}
	if err := environment.Validate(); err != nil {
		return nil, bursar.NewError("invalid Dodo provider environment", bursar.ErrorOptions{Code: bursar.ErrorCodeConfig, Category: bursar.ErrorCategoryInvalidRequest, Cause: err})
	}
	client := options.Client
	if client == nil {
		apiKey := strings.TrimSpace(options.APIKey)
		if apiKey == "" {
			return nil, bursar.NewError("Dodo API key is required when no client is supplied", bursar.ErrorOptions{
				Code:     bursar.ErrorCodeConfig,
				Category: bursar.ErrorCategoryInvalidRequest,
			})
		}
		environmentOption := option.WithEnvironmentTestMode()
		if environment == bursar.ProviderEnvironmentLive {
			environmentOption = option.WithEnvironmentLiveMode()
		}
		clientOptions := []option.RequestOption{
			option.WithBearerToken(apiKey),
			option.WithWebhookKey(webhookKey),
			environmentOption,
		}
		if options.HTTPClient != nil {
			clientOptions = append(clientOptions, option.WithHTTPClient(options.HTTPClient))
		}
		client = dodopayments.NewClient(clientOptions...)
	}
	return &Provider{client: client, setupProductID: strings.TrimSpace(options.SetupProductID), environment: environment}, nil
}

// Name returns the stable catalog provider key.
func (*Provider) Name() string { return ProviderName }

// ProviderEnvironment returns the financial namespace selected for this
// client. It is intentionally separate from the provider name.
func (p *Provider) ProviderEnvironment() bursar.ProviderEnvironment {
	if p == nil {
		return ""
	}
	return p.environment
}

// CreateCheckoutSession creates a Dodo hosted checkout for a catalog-resolved
// product. The Bursar account identifier is placed in Dodo metadata so a later
// verified webhook can be resolved without trusting browser input.
func (p *Provider) CreateCheckoutSession(ctx context.Context, request bursar.CheckoutSessionRequest) (bursar.CheckoutSession, error) {
	if p == nil || p.client == nil || p.client.CheckoutSessions == nil {
		return bursar.CheckoutSession{}, bursar.NewError("Dodo provider is not initialized", bursar.ErrorOptions{Code: bursar.ErrorCodeProviderResponseInvalid})
	}
	if err := validateCheckoutRequest(request); err != nil {
		return bursar.CheckoutSession{}, err
	}
	metadata := make(dodopayments.MetadataParam, len(request.Metadata)+1)
	for key, value := range request.Metadata {
		metadata[key] = shared.UnionString(value)
	}
	metadata["bursar_account_id"] = shared.UnionString(request.AccountID)
	params := dodopayments.CheckoutSessionNewParams{
		CheckoutSessionRequest: dodopayments.CheckoutSessionRequestParam{
			ProductCart: dodopayments.F([]dodopayments.ProductItemReqParam{{
				ProductID: dodopayments.F(request.ProductID),
				Quantity:  dodopayments.F(request.Quantity),
			}}),
			CancelURL: dodopayments.F(request.CancelURL),
			Metadata:  dodopayments.F(metadata),
			ReturnURL: dodopayments.F(request.SuccessURL),
		},
	}
	if customerID := strings.TrimSpace(request.CustomerID); customerID != "" {
		params.CheckoutSessionRequest.Customer = dodopayments.F[dodopayments.CustomerRequestUnionParam](dodopayments.AttachExistingCustomerParam{
			CustomerID: dodopayments.F(customerID),
		})
	} else if email := strings.TrimSpace(request.CustomerEmail); email != "" {
		params.CheckoutSessionRequest.Customer = dodopayments.F[dodopayments.CustomerRequestUnionParam](dodopayments.NewCustomerParam{
			Email: dodopayments.F(email),
		})
	}
	session, err := p.client.CheckoutSessions.New(ctx, params, option.WithHeader("Idempotency-Key", request.IdempotencyKey))
	if err != nil {
		return bursar.CheckoutSession{}, bursar.NewError("create Dodo checkout session", bursar.ErrorOptions{
			Code:          bursar.ErrorCodeProviderResponseInvalid,
			Category:      bursar.ErrorCategoryUnavailable,
			Retryable:     true,
			Indeterminate: true,
			Cause:         err,
		})
	}
	if session == nil || strings.TrimSpace(session.SessionID) == "" || strings.TrimSpace(session.CheckoutURL) == "" {
		return bursar.CheckoutSession{}, bursar.NewError("Dodo returned an incomplete checkout session", bursar.ErrorOptions{Code: bursar.ErrorCodeProviderResponseInvalid, Category: bursar.ErrorCategoryUnavailable})
	}
	return bursar.CheckoutSession{ID: session.SessionID, URL: session.CheckoutURL}, nil
}

// GetCheckoutSessionStatus returns the Dodo checkout's payment status.
func (p *Provider) GetCheckoutSessionStatus(ctx context.Context, sessionID string) (string, error) {
	if p == nil || p.client == nil || p.client.CheckoutSessions == nil {
		return "", bursar.NewError("Dodo provider is not initialized", bursar.ErrorOptions{Code: bursar.ErrorCodeProviderResponseInvalid})
	}
	if strings.TrimSpace(sessionID) == "" {
		return "", bursar.NewError("Dodo checkout session ID is required", bursar.ErrorOptions{Code: bursar.ErrorCodeConfig, Category: bursar.ErrorCategoryInvalidRequest})
	}
	status, err := p.client.CheckoutSessions.Get(ctx, sessionID)
	if err != nil {
		return "", bursar.NewError("retrieve Dodo checkout session", bursar.ErrorOptions{Code: bursar.ErrorCodeProviderResponseInvalid, Category: bursar.ErrorCategoryUnavailable, Retryable: true, Cause: err})
	}
	if status == nil {
		return "", bursar.NewError("Dodo returned no checkout session", bursar.ErrorOptions{Code: bursar.ErrorCodeProviderResponseInvalid, Category: bursar.ErrorCategoryUnavailable})
	}
	return string(status.PaymentStatus), nil
}

// CreateCustomerPortalSession creates Dodo's hosted customer-management
// session. The account-to-customer authorization decision is made by
// CommerceService before this provider receives the trusted customer ID.
func (p *Provider) CreateCustomerPortalSession(ctx context.Context, customerID, returnURL string) (string, error) {
	if p == nil || p.client == nil || p.client.Customers == nil || p.client.Customers.CustomerPortal == nil {
		return "", dodoUninitializedError()
	}
	customerID, err := requireDodoInputText(customerID, "customer ID")
	if err != nil {
		return "", err
	}
	returnURL, err = requireDodoInputText(returnURL, "customer portal return URL")
	if err != nil {
		return "", err
	}
	session, err := p.client.Customers.CustomerPortal.New(ctx, customerID, dodopayments.CustomerCustomerPortalNewParams{
		ReturnURL: dodopayments.F(returnURL),
	})
	if err != nil {
		return "", dodoRequestError("create customer portal session", err, false)
	}
	if session == nil {
		return "", dodoResponseError("create customer portal session", "link")
	}
	return requireDodoResponseText(session.Link, "create customer portal session", "link")
}

// CreateUpdatePaymentMethodSession returns Dodo's hosted route for changing
// the saved payment method of an existing subscription. Dodo identifies the
// subscription in the URL; customerID is validated here and scoped by the
// caller before the provider method is reached.
func (p *Provider) CreateUpdatePaymentMethodSession(ctx context.Context, customerID, subscriptionID, returnURL string) (string, error) {
	if p == nil || p.client == nil || p.client.Subscriptions == nil {
		return "", dodoUninitializedError()
	}
	if _, err := requireDodoInputText(customerID, "customer ID"); err != nil {
		return "", err
	}
	subscriptionID, err := requireDodoInputText(subscriptionID, "subscription ID")
	if err != nil {
		return "", err
	}
	returnURL, err = requireDodoInputText(returnURL, "payment method return URL")
	if err != nil {
		return "", err
	}
	response, err := p.client.Subscriptions.UpdatePaymentMethod(ctx, subscriptionID, dodopayments.SubscriptionUpdatePaymentMethodParams{
		PaymentMethod: dodopayments.SubscriptionUpdatePaymentMethodParamsPaymentMethodNew{
			Type:      dodopayments.F(dodopayments.SubscriptionUpdatePaymentMethodParamsPaymentMethodNewTypeNew),
			ReturnURL: dodopayments.F(returnURL),
		},
	})
	if err != nil {
		return "", dodoRequestError("create update payment method session", err, false)
	}
	if response == nil {
		return "", dodoResponseError("create update payment method session", "payment_link")
	}
	return requireDodoResponseText(response.PaymentLink, "create update payment method session", "payment_link")
}

// CreatePaymentMethodSetupSession creates a mandate-only Dodo checkout for a
// customer without an active subscription. SetupProductID is intentionally a
// provider configuration value rather than browser input.
func (p *Provider) CreatePaymentMethodSetupSession(ctx context.Context, customerID, returnURL, cancelURL string) (string, error) {
	if p == nil || p.client == nil || p.client.CheckoutSessions == nil {
		return "", dodoUninitializedError()
	}
	if p.setupProductID == "" {
		return "", bursar.NewError("Dodo payment method setup product is not configured", bursar.ErrorOptions{
			Code:     bursar.ErrorCodeCapabilityNotConfigured,
			Category: bursar.ErrorCategoryUnavailable,
		})
	}
	customerID, err := requireDodoInputText(customerID, "customer ID")
	if err != nil {
		return "", err
	}
	returnURL, err = requireDodoInputText(returnURL, "payment method return URL")
	if err != nil {
		return "", err
	}
	request := dodopayments.CheckoutSessionRequestParam{
		ProductCart: dodopayments.F([]dodopayments.ProductItemReqParam{{
			ProductID: dodopayments.F(p.setupProductID),
			Quantity:  dodopayments.F(int64(1)),
		}}),
		Customer: dodopayments.F[dodopayments.CustomerRequestUnionParam](dodopayments.AttachExistingCustomerParam{
			CustomerID: dodopayments.F(customerID),
		}),
		ReturnURL: dodopayments.F(returnURL),
		Metadata:  dodopayments.F(dodoMetadata(map[string]string{"purpose": "setup_payment_method"})),
		SubscriptionData: dodopayments.F(dodopayments.SubscriptionDataParam{
			OnDemand: dodopayments.F(dodopayments.OnDemandSubscriptionParam{
				MandateOnly: dodopayments.F(true),
			}),
		}),
	}
	if cancelURL = strings.TrimSpace(cancelURL); cancelURL != "" {
		request.CancelURL = dodopayments.F(cancelURL)
	}
	session, err := p.client.CheckoutSessions.New(ctx, dodopayments.CheckoutSessionNewParams{CheckoutSessionRequest: request})
	if err != nil {
		return "", dodoRequestError("create payment method setup session", err, true)
	}
	if session == nil {
		return "", dodoResponseError("create payment method setup session", "checkout_url")
	}
	return requireDodoResponseText(session.CheckoutURL, "create payment method setup session", "checkout_url")
}

// CreateCustomer creates an idempotent provider customer for an
// already-authorized Bursar account.
func (p *Provider) CreateCustomer(ctx context.Context, request bursar.CreateCustomerRequest) (string, error) {
	if p == nil || p.client == nil || p.client.Customers == nil {
		return "", dodoUninitializedError()
	}
	email, err := requireDodoInputText(request.Email, "customer email")
	if err != nil {
		return "", err
	}
	name, err := requireDodoInputText(request.Name, "customer name")
	if err != nil {
		return "", err
	}
	idempotencyKey, err := requireDodoIdempotencyKey(request.IdempotencyKey)
	if err != nil {
		return "", err
	}
	customer, err := p.client.Customers.New(ctx, dodopayments.CustomerNewParams{
		Email:    dodopayments.F(email),
		Name:     dodopayments.F(name),
		Metadata: dodopayments.F(dodoMetadata(request.Metadata)),
	}, dodoIdempotencyOption(idempotencyKey))
	if err != nil {
		return "", dodoRequestError("create customer", err, true)
	}
	if customer == nil {
		return "", dodoResponseError("create customer", "customer_id")
	}
	return requireDodoResponseText(customer.CustomerID, "create customer", "customer_id")
}

// CancelSubscription asks Dodo to cancel at the next billing date. The final
// subscription state is still applied only from a verified webhook.
func (p *Provider) CancelSubscription(ctx context.Context, subscriptionID, idempotencyKey string) error {
	if p == nil || p.client == nil || p.client.Subscriptions == nil {
		return dodoUninitializedError()
	}
	subscriptionID, err := requireDodoInputText(subscriptionID, "subscription ID")
	if err != nil {
		return err
	}
	idempotencyKey, err = requireDodoIdempotencyKey(idempotencyKey)
	if err != nil {
		return err
	}
	_, err = p.client.Subscriptions.Update(ctx, subscriptionID, dodopayments.SubscriptionUpdateParams{
		CancelAtNextBillingDate: dodopayments.F(true),
	}, dodoIdempotencyOption(idempotencyKey))
	if err != nil {
		return dodoRequestError("cancel subscription", err, true)
	}
	return nil
}

// ReactivateSubscription removes a previously scheduled cancellation.
func (p *Provider) ReactivateSubscription(ctx context.Context, subscriptionID, idempotencyKey string) error {
	if p == nil || p.client == nil || p.client.Subscriptions == nil {
		return dodoUninitializedError()
	}
	subscriptionID, err := requireDodoInputText(subscriptionID, "subscription ID")
	if err != nil {
		return err
	}
	idempotencyKey, err = requireDodoIdempotencyKey(idempotencyKey)
	if err != nil {
		return err
	}
	_, err = p.client.Subscriptions.Update(ctx, subscriptionID, dodopayments.SubscriptionUpdateParams{
		CancelAtNextBillingDate: dodopayments.F(false),
	}, dodoIdempotencyOption(idempotencyKey))
	if err != nil {
		return dodoRequestError("reactivate subscription", err, true)
	}
	return nil
}

// CancelScheduledPlanChange cancels Dodo's one scheduled plan change. Dodo's
// endpoint is keyed by subscription ID and does not expose Bursar's stored
// provider operation identifier.
func (p *Provider) CancelScheduledPlanChange(ctx context.Context, subscriptionID, _ string, idempotencyKey string) error {
	if p == nil || p.client == nil || p.client.Subscriptions == nil {
		return dodoUninitializedError()
	}
	subscriptionID, err := requireDodoInputText(subscriptionID, "subscription ID")
	if err != nil {
		return err
	}
	idempotencyKey, err = requireDodoIdempotencyKey(idempotencyKey)
	if err != nil {
		return err
	}
	if err := p.client.Subscriptions.CancelChangePlan(ctx, subscriptionID, dodoIdempotencyOption(idempotencyKey)); err != nil {
		return dodoRequestError("cancel scheduled plan change", err, true)
	}
	return nil
}

// ListPaymentMethods returns recurring cards only, because a non-recurring or
// non-card method cannot safely be selected for an automatic Bursar top-up.
func (p *Provider) ListPaymentMethods(ctx context.Context, customerID string) ([]bursar.PaymentMethodInfo, error) {
	if p == nil || p.client == nil || p.client.Customers == nil {
		return nil, dodoUninitializedError()
	}
	customerID, err := requireDodoInputText(customerID, "customer ID")
	if err != nil {
		return nil, err
	}
	response, err := p.client.Customers.GetPaymentMethods(ctx, customerID)
	if err != nil {
		return nil, dodoRequestError("list payment methods", err, false)
	}
	if response == nil {
		return nil, dodoResponseError("list payment methods", "items")
	}
	methods := make([]bursar.PaymentMethodInfo, 0, len(response.Items))
	seen := make(map[string]struct{}, len(response.Items))
	for _, item := range response.Items {
		if item.PaymentMethod != dodopayments.CustomerGetPaymentMethodsResponseItemsPaymentMethodCard || !item.RecurringEnabled {
			continue
		}
		methodID, err := requireDodoResponseText(item.PaymentMethodID, "list payment methods", "payment_method_id")
		if err != nil {
			return nil, err
		}
		last4, err := requireDodoCardLast4(item.Card.Last4Digits)
		if err != nil {
			return nil, err
		}
		brand, err := requireDodoResponseText(item.Card.CardNetwork, "list payment methods", "card.card_network")
		if err != nil {
			return nil, err
		}
		expiryMonth, err := parseDodoCardInteger(item.Card.ExpiryMonth, "card.expiry_month", 1, 12)
		if err != nil {
			return nil, err
		}
		expiryYear, err := parseDodoCardInteger(item.Card.ExpiryYear, "card.expiry_year", 1, 0)
		if err != nil {
			return nil, err
		}
		key := strings.ToLower(brand) + ":" + last4 + ":" + strconv.Itoa(expiryMonth) + ":" + strconv.Itoa(expiryYear)
		if _, duplicate := seen[key]; duplicate {
			continue
		}
		seen[key] = struct{}{}
		methods = append(methods, bursar.PaymentMethodInfo{
			ID: methodID, Last4: last4, Brand: brand, ExpiryMonth: expiryMonth, ExpiryYear: expiryYear,
		})
	}
	if len(methods) == 1 {
		methods[0].IsDefault = true
	}
	return methods, nil
}

// PreviewSavedPaymentCharge obtains Dodo's non-mutating product-cart quote.
func (p *Provider) PreviewSavedPaymentCharge(ctx context.Context, params bursar.SavedPaymentChargeParams) (bursar.SavedPaymentChargeQuote, error) {
	if p == nil || p.client == nil || p.client.CheckoutSessions == nil {
		return bursar.SavedPaymentChargeQuote{}, dodoUninitializedError()
	}
	if err := validateSavedPaymentChargeParams(params, false); err != nil {
		return bursar.SavedPaymentChargeQuote{}, err
	}
	response, err := p.client.CheckoutSessions.Preview(ctx, dodopayments.CheckoutSessionPreviewParams{
		CheckoutSessionRequest: dodoSavedPaymentCheckoutRequest(params, false),
	})
	if err != nil {
		return bursar.SavedPaymentChargeQuote{}, dodoRequestError("preview saved payment charge", err, false)
	}
	if response == nil {
		return bursar.SavedPaymentChargeQuote{}, dodoResponseError("preview saved payment charge", "current_breakup")
	}
	currency, err := requireDodoResponseText(string(response.Currency), "preview saved payment charge", "currency")
	if err != nil {
		return bursar.SavedPaymentChargeQuote{}, err
	}
	if response.CurrentBreakup.TotalAmount < 0 {
		return bursar.SavedPaymentChargeQuote{}, dodoResponseError("preview saved payment charge", "current_breakup.total_amount")
	}
	quote := bursar.SavedPaymentChargeQuote{AmountMinor: response.CurrentBreakup.TotalAmount, Currency: currency}
	if !response.CurrentBreakup.JSON.Tax.IsNull() {
		tax := response.CurrentBreakup.Tax
		if tax < 0 {
			return bursar.SavedPaymentChargeQuote{}, dodoResponseError("preview saved payment charge", "current_breakup.tax")
		}
		quote.TaxMinor = &tax
	}
	return quote, nil
}

// ChargeSavedPaymentMethod confirms a Dodo checkout against an attached saved
// method, then retrieves the authoritative payment state from Dodo.
func (p *Provider) ChargeSavedPaymentMethod(ctx context.Context, params bursar.SavedPaymentChargeParams) (bursar.SavedPaymentChargeResult, error) {
	if p == nil || p.client == nil || p.client.CheckoutSessions == nil || p.client.Payments == nil {
		return bursar.SavedPaymentChargeResult{}, dodoUninitializedError()
	}
	if err := validateSavedPaymentChargeParams(params, true); err != nil {
		return bursar.SavedPaymentChargeResult{}, err
	}
	session, err := p.client.CheckoutSessions.New(ctx, dodopayments.CheckoutSessionNewParams{
		CheckoutSessionRequest: dodoSavedPaymentCheckoutRequest(params, true),
	}, dodoIdempotencyOption(params.IdempotencyKey))
	if err != nil {
		return bursar.SavedPaymentChargeResult{}, dodoRequestError("charge saved payment method", err, true)
	}
	if session == nil {
		return bursar.SavedPaymentChargeResult{}, dodoResponseError("charge saved payment method", "payment_id")
	}
	paymentID, err := requireDodoResponseText(session.PaymentID, "charge saved payment method", "payment_id")
	if err != nil {
		return bursar.SavedPaymentChargeResult{}, err
	}
	payment, err := p.client.Payments.Get(ctx, paymentID)
	if err != nil {
		return bursar.SavedPaymentChargeResult{}, dodoRequestError("retrieve saved payment charge", err, true)
	}
	if payment == nil {
		return bursar.SavedPaymentChargeResult{}, dodoResponseError("charge saved payment method", "payment")
	}
	providerPaymentID, err := requireDodoResponseText(payment.PaymentID, "charge saved payment method", "payment_id")
	if err != nil {
		return bursar.SavedPaymentChargeResult{}, err
	}
	status, err := dodoSavedPaymentStatus(payment.Status)
	if err != nil {
		return bursar.SavedPaymentChargeResult{}, err
	}
	if payment.TotalAmount < 0 {
		return bursar.SavedPaymentChargeResult{}, dodoResponseError("charge saved payment method", "total_amount")
	}
	amount := payment.TotalAmount
	result := bursar.SavedPaymentChargeResult{
		ProviderPaymentID: providerPaymentID,
		Status:            status,
		AmountMinor:       &amount,
	}
	if currency := strings.TrimSpace(string(payment.Currency)); currency != "" {
		result.Currency = currency
	} else {
		return bursar.SavedPaymentChargeResult{}, dodoResponseError("charge saved payment method", "currency")
	}
	if actionURL := strings.TrimSpace(session.CheckoutURL); actionURL != "" {
		result.ActionURL = actionURL
	}
	return result, nil
}

// GetInvoiceURL retrieves the invoice URL attached to a Dodo payment. The
// pinned official SDK exposes invoice URLs through Payments.Get and exposes no
// standalone invoice retrieval endpoint, so callers must pass a payment ID.
func (p *Provider) GetInvoiceURL(ctx context.Context, paymentID string) (string, error) {
	if p == nil || p.client == nil || p.client.Payments == nil {
		return "", dodoUninitializedError()
	}
	paymentID, err := requireDodoInputText(paymentID, "Dodo payment ID")
	if err != nil {
		return "", err
	}
	payment, err := p.client.Payments.Get(ctx, paymentID)
	if err != nil {
		return "", dodoRequestError("retrieve invoice URL", err, false)
	}
	if payment == nil {
		return "", dodoResponseError("retrieve invoice URL", "payment")
	}
	return strings.TrimSpace(payment.InvoiceURL), nil
}

// PreviewPlanChange returns Dodo's provider-authored plan-change quote. The
// amount conversion is exact for all integer minor-unit fields; the official
// SDK represents Dodo's proration factor as float64, so it is converted via
// its shortest decimal representation and rejected when non-finite.
func (p *Provider) PreviewPlanChange(ctx context.Context, request bursar.ProviderPlanChangeRequest) (bursar.PlanChangePreview, error) {
	if p == nil || p.client == nil || p.client.Subscriptions == nil {
		return bursar.PlanChangePreview{}, dodoUninitializedError()
	}
	subscriptionID, body, err := dodoPlanChangeRequest(request, false)
	if err != nil {
		return bursar.PlanChangePreview{}, err
	}
	response, err := p.client.Subscriptions.PreviewChangePlan(ctx, subscriptionID, dodopayments.SubscriptionPreviewChangePlanParams{
		UpdateSubscriptionPlanReq: body,
	})
	if err != nil {
		return bursar.PlanChangePreview{}, dodoRequestError("preview plan change", err, false)
	}
	if response == nil {
		return bursar.PlanChangePreview{}, dodoResponseError("preview plan change", "response")
	}
	return dodoPlanChangePreview(*response)
}

// ChangePlan submits a Dodo plan change. Dodo's official endpoint returns no
// operation identifier; scheduled-change cancellation is addressed by the
// subscription ID through CancelScheduledPlanChange.
func (p *Provider) ChangePlan(ctx context.Context, request bursar.ProviderPlanChangeRequest) (bursar.ProviderPlanChangeResult, error) {
	if p == nil || p.client == nil || p.client.Subscriptions == nil {
		return bursar.ProviderPlanChangeResult{}, dodoUninitializedError()
	}
	subscriptionID, body, err := dodoPlanChangeRequest(request, true)
	if err != nil {
		return bursar.ProviderPlanChangeResult{}, err
	}
	if err := p.client.Subscriptions.ChangePlan(ctx, subscriptionID, dodopayments.SubscriptionChangePlanParams{
		UpdateSubscriptionPlanReq: body,
	}, dodoIdempotencyOption(request.IdempotencyKey)); err != nil {
		return bursar.ProviderPlanChangeResult{}, dodoRequestError("change plan", err, true)
	}
	return bursar.ProviderPlanChangeResult{}, nil
}

// HandleWebhook authenticates the raw Standard Webhooks request with Dodo's
// SDK, then converts its event into Bursar's provider-neutral event envelope.
func (p *Provider) HandleWebhook(_ context.Context, request bursar.WebhookRequest) (bursar.WebhookResult, error) {
	if p == nil || p.client == nil || p.client.Webhooks == nil {
		return bursar.WebhookResult{}, bursar.NewError("Dodo provider is not initialized", bursar.ErrorOptions{Code: bursar.ErrorCodeProviderResponseInvalid})
	}
	if len(request.RawBody) == 0 {
		return bursar.WebhookResult{}, bursar.NewError("Dodo webhook body is required", bursar.ErrorOptions{Code: bursar.ErrorCodeConfig, Category: bursar.ErrorCategoryInvalidRequest})
	}
	verified, err := p.client.Webhooks.Unwrap(request.RawBody, request.Header)
	if err != nil {
		return bursar.WebhookResult{}, bursar.NewError("verify Dodo webhook", bursar.ErrorOptions{Code: bursar.ErrorCodeProviderResponseInvalid, Category: bursar.ErrorCategoryInvalidRequest, Cause: err})
	}
	if verified == nil {
		return bursar.WebhookResult{}, bursar.NewError("Dodo returned no verified webhook event", bursar.ErrorOptions{Code: bursar.ErrorCodeProviderResponseInvalid, Category: bursar.ErrorCategoryInvalidRequest})
	}
	envelope, err := decodeWebhookPayload(request.RawBody)
	if err != nil {
		return bursar.WebhookResult{}, err
	}
	if envelope.Type != "" && envelope.Type != string(verified.Type) {
		return bursar.WebhookResult{}, bursar.NewError("Dodo verified webhook type does not match its payload", bursar.ErrorOptions{Code: bursar.ErrorCodeProviderResponseInvalid, Category: bursar.ErrorCategoryInvalidRequest})
	}
	normalized, err := mapDodoEvent(string(verified.Type), verified.Timestamp, request.RawBody, envelope.Data)
	if err != nil {
		return bursar.WebhookResult{}, err
	}
	eventID := ""
	if normalized != nil {
		eventID = normalized.EventID
	} else {
		resourceID, resourceErr := dodoEventResourceID(string(verified.Type), envelope.Data)
		if resourceErr != nil {
			return bursar.WebhookResult{}, resourceErr
		}
		eventID = dodoCanonicalEventID(string(verified.Type), resourceID, verified.Timestamp)
	}
	return bursar.WebhookResult{
		Received:  true,
		Provider:  ProviderName,
		EventID:   eventID,
		EventType: string(verified.Type),
		Event:     normalized,
	}, nil
}

type dodoWebhookEnvelope struct {
	Type string
	Data map[string]any
}

func decodeWebhookPayload(payload []byte) (dodoWebhookEnvelope, error) {
	decoder := json.NewDecoder(bytes.NewReader(payload))
	decoder.UseNumber()
	var envelope struct {
		Type string          `json:"type"`
		Data json.RawMessage `json:"data"`
	}
	if err := decoder.Decode(&envelope); err != nil {
		return dodoWebhookEnvelope{}, bursar.NewError("decode verified Dodo webhook", bursar.ErrorOptions{Code: bursar.ErrorCodeProviderResponseInvalid, Category: bursar.ErrorCategoryInvalidRequest, Cause: err})
	}
	if len(envelope.Data) == 0 {
		return dodoWebhookEnvelope{}, bursar.NewError("Dodo webhook data is required", bursar.ErrorOptions{Code: bursar.ErrorCodeProviderResponseInvalid, Category: bursar.ErrorCategoryInvalidRequest})
	}
	dataDecoder := json.NewDecoder(bytes.NewReader(envelope.Data))
	dataDecoder.UseNumber()
	object := map[string]any{}
	if err := dataDecoder.Decode(&object); err != nil {
		return dodoWebhookEnvelope{}, bursar.NewError("decode verified Dodo webhook data", bursar.ErrorOptions{Code: bursar.ErrorCodeProviderResponseInvalid, Category: bursar.ErrorCategoryInvalidRequest, Cause: err})
	}
	return dodoWebhookEnvelope{Type: strings.TrimSpace(envelope.Type), Data: object}, nil
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

func dodoUninitializedError() *bursar.BursarError {
	return bursar.NewError("Dodo provider is not initialized", bursar.ErrorOptions{
		Code:     bursar.ErrorCodeProviderResponseInvalid,
		Category: bursar.ErrorCategoryInternal,
	})
}

func requireDodoInputText(value, field string) (string, error) {
	trimmed := strings.TrimSpace(value)
	if trimmed == "" {
		return "", bursar.NewError("Dodo "+field+" is required", bursar.ErrorOptions{
			Code:     bursar.ErrorCodeConfig,
			Category: bursar.ErrorCategoryInvalidRequest,
		})
	}
	return trimmed, nil
}

func requireDodoIdempotencyKey(value string) (string, error) {
	key, err := requireDodoInputText(value, "idempotency key")
	if err != nil {
		return "", err
	}
	if len(key) > 255 {
		return "", bursar.NewError("Dodo idempotency key must be at most 255 characters", bursar.ErrorOptions{
			Code:     bursar.ErrorCodeConfig,
			Category: bursar.ErrorCategoryInvalidRequest,
		})
	}
	return key, nil
}

func requireDodoResponseText(value, operation, field string) (string, error) {
	trimmed := strings.TrimSpace(value)
	if trimmed == "" {
		return "", dodoResponseError(operation, field)
	}
	return trimmed, nil
}

func dodoResponseError(operation, field string) *bursar.BursarError {
	return bursar.NewError("Dodo returned an invalid response for "+operation, bursar.ErrorOptions{
		Code:     bursar.ErrorCodeProviderResponseInvalid,
		Category: bursar.ErrorCategoryUnavailable,
		Details:  map[string]any{"provider": ProviderName, "operation": operation, "field": field},
	})
}

func dodoRequestError(operation string, cause error, indeterminate bool) *bursar.BursarError {
	return bursar.NewError("Dodo "+operation, bursar.ErrorOptions{
		Code:          bursar.ErrorCodeProviderResponseInvalid,
		Category:      bursar.ErrorCategoryUnavailable,
		Retryable:     true,
		Indeterminate: indeterminate,
		Cause:         cause,
		Details:       map[string]any{"provider": ProviderName, "operation": operation},
	})
}

func dodoIdempotencyOption(key string) option.RequestOption {
	return option.WithHeader("Idempotency-Key", key)
}

func dodoMetadata(values map[string]string) dodopayments.MetadataParam {
	metadata := make(dodopayments.MetadataParam, len(values))
	for key, value := range values {
		metadata[key] = shared.UnionString(value)
	}
	return metadata
}

func requireDodoCardLast4(value string) (string, error) {
	last4 := strings.TrimSpace(value)
	if len(last4) != 4 {
		return "", dodoResponseError("list payment methods", "card.last4_digits")
	}
	for _, digit := range last4 {
		if digit < '0' || digit > '9' {
			return "", dodoResponseError("list payment methods", "card.last4_digits")
		}
	}
	return last4, nil
}

func parseDodoCardInteger(value, field string, minimum, maximum int) (int, error) {
	parsed, err := strconv.Atoi(strings.TrimSpace(value))
	if err != nil || parsed < minimum || (maximum > 0 && parsed > maximum) {
		return 0, dodoResponseError("list payment methods", field)
	}
	return parsed, nil
}

func validateSavedPaymentChargeParams(params bursar.SavedPaymentChargeParams, confirm bool) error {
	if _, err := requireDodoInputText(params.CustomerID, "customer ID"); err != nil {
		return err
	}
	if _, err := requireDodoInputText(params.ProductID, "product ID"); err != nil {
		return err
	}
	if params.Quantity < 1 {
		return bursar.NewError("Dodo saved payment quantity must be positive", bursar.ErrorOptions{
			Code:     bursar.ErrorCodeInvalidOfferQuantity,
			Category: bursar.ErrorCategoryInvalidRequest,
		})
	}
	if confirm {
		if _, err := requireDodoInputText(params.PaymentMethodID, "payment method ID"); err != nil {
			return err
		}
		if _, err := requireDodoInputText(params.ReturnURL, "saved payment return URL"); err != nil {
			return err
		}
		if _, err := requireDodoIdempotencyKey(params.IdempotencyKey); err != nil {
			return err
		}
	}
	return nil
}

func dodoSavedPaymentCheckoutRequest(params bursar.SavedPaymentChargeParams, confirm bool) dodopayments.CheckoutSessionRequestParam {
	request := dodopayments.CheckoutSessionRequestParam{
		ProductCart: dodopayments.F([]dodopayments.ProductItemReqParam{{
			ProductID: dodopayments.F(strings.TrimSpace(params.ProductID)),
			Quantity:  dodopayments.F(params.Quantity),
		}}),
		Customer: dodopayments.F[dodopayments.CustomerRequestUnionParam](dodopayments.AttachExistingCustomerParam{
			CustomerID: dodopayments.F(strings.TrimSpace(params.CustomerID)),
		}),
	}
	if confirm {
		request.Confirm = dodopayments.F(true)
		request.PaymentMethodID = dodopayments.F(strings.TrimSpace(params.PaymentMethodID))
		request.ReturnURL = dodopayments.F(strings.TrimSpace(params.ReturnURL))
		request.Metadata = dodopayments.F(dodoMetadata(params.Metadata))
	}
	return request
}

func dodoSavedPaymentStatus(value dodopayments.IntentStatus) (bursar.SavedPaymentChargeStatus, error) {
	status := bursar.SavedPaymentChargeStatus(value)
	switch status {
	case bursar.SavedPaymentChargeSucceeded,
		bursar.SavedPaymentChargeProcessing,
		bursar.SavedPaymentChargeFailed,
		bursar.SavedPaymentChargeCancelled,
		bursar.SavedPaymentChargeRequiresCustomerAction,
		bursar.SavedPaymentChargeRequiresMerchantAction,
		bursar.SavedPaymentChargeRequiresPaymentMethod,
		bursar.SavedPaymentChargeRequiresConfirmation,
		bursar.SavedPaymentChargeRequiresCapture,
		bursar.SavedPaymentChargePartiallyCaptured,
		bursar.SavedPaymentChargePartiallyCapturedAndCapturable:
		return status, nil
	default:
		return "", dodoResponseError("charge saved payment method", "status")
	}
}

func dodoPlanChangeRequest(request bursar.ProviderPlanChangeRequest, mutation bool) (string, dodopayments.UpdateSubscriptionPlanReqParam, error) {
	subscriptionID, err := requireDodoInputText(request.ProviderSubscriptionID, "subscription ID")
	if err != nil {
		return "", dodopayments.UpdateSubscriptionPlanReqParam{}, err
	}
	productID, err := requireDodoInputText(request.ProductID, "product ID")
	if err != nil {
		return "", dodopayments.UpdateSubscriptionPlanReqParam{}, err
	}
	if request.Quantity < 1 {
		return "", dodopayments.UpdateSubscriptionPlanReqParam{}, bursar.NewError("Dodo plan change quantity must be positive", bursar.ErrorOptions{
			Code:     bursar.ErrorCodeInvalidOfferQuantity,
			Category: bursar.ErrorCategoryInvalidRequest,
		})
	}
	proration, err := dodoProrationMode(request.ProrationBillingMode)
	if err != nil {
		return "", dodopayments.UpdateSubscriptionPlanReqParam{}, err
	}
	effectiveAt, err := dodoEffectiveAt(request.EffectiveAt)
	if err != nil {
		return "", dodopayments.UpdateSubscriptionPlanReqParam{}, err
	}
	body := dodopayments.UpdateSubscriptionPlanReqParam{
		ProductID:            dodopayments.F(productID),
		ProrationBillingMode: dodopayments.F(proration),
		Quantity:             dodopayments.F(request.Quantity),
		EffectiveAt:          dodopayments.F(effectiveAt),
	}
	if mutation {
		if _, err := requireDodoIdempotencyKey(request.IdempotencyKey); err != nil {
			return "", dodopayments.UpdateSubscriptionPlanReqParam{}, err
		}
		if paymentFailure := strings.TrimSpace(request.PaymentFailure); paymentFailure != "" {
			mode, err := dodoPaymentFailureMode(paymentFailure)
			if err != nil {
				return "", dodopayments.UpdateSubscriptionPlanReqParam{}, err
			}
			body.OnPaymentFailure = dodopayments.F(mode)
		}
		if len(request.Metadata) > 0 {
			body.Metadata = dodopayments.F(dodoMetadata(request.Metadata))
		}
	}
	return subscriptionID, body, nil
}

func dodoProrationMode(value string) (dodopayments.UpdateSubscriptionPlanReqProrationBillingMode, error) {
	switch strings.TrimSpace(value) {
	case "", "prorated_immediately":
		return dodopayments.UpdateSubscriptionPlanReqProrationBillingModeProratedImmediately, nil
	case "full_immediately":
		return dodopayments.UpdateSubscriptionPlanReqProrationBillingModeFullImmediately, nil
	case "difference_immediately":
		return dodopayments.UpdateSubscriptionPlanReqProrationBillingModeDifferenceImmediately, nil
	case "do_not_bill":
		return dodopayments.UpdateSubscriptionPlanReqProrationBillingModeDoNotBill, nil
	default:
		return "", bursar.NewError("Dodo plan change proration billing mode is invalid", bursar.ErrorOptions{
			Code:     bursar.ErrorCodeConfig,
			Category: bursar.ErrorCategoryInvalidRequest,
		})
	}
}

func dodoEffectiveAt(value string) (dodopayments.UpdateSubscriptionPlanReqEffectiveAt, error) {
	switch strings.TrimSpace(value) {
	case "", "immediately":
		return dodopayments.UpdateSubscriptionPlanReqEffectiveAtImmediately, nil
	case "next_billing_date":
		return dodopayments.UpdateSubscriptionPlanReqEffectiveAtNextBillingDate, nil
	default:
		return "", bursar.NewError("Dodo plan change effective time is invalid", bursar.ErrorOptions{
			Code:     bursar.ErrorCodeConfig,
			Category: bursar.ErrorCategoryInvalidRequest,
		})
	}
}

func dodoPaymentFailureMode(value string) (dodopayments.UpdateSubscriptionPlanReqOnPaymentFailure, error) {
	switch value {
	case "prevent_change":
		return dodopayments.UpdateSubscriptionPlanReqOnPaymentFailurePreventChange, nil
	case "apply_change":
		return dodopayments.UpdateSubscriptionPlanReqOnPaymentFailureApplyChange, nil
	default:
		return "", bursar.NewError("Dodo plan change payment failure policy is invalid", bursar.ErrorOptions{
			Code:     bursar.ErrorCodeConfig,
			Category: bursar.ErrorCategoryInvalidRequest,
		})
	}
}

func dodoPlanChangePreview(response dodopayments.SubscriptionPreviewChangePlanResponse) (bursar.PlanChangePreview, error) {
	summary := response.ImmediateCharge.Summary
	currency, err := requireDodoResponseText(string(summary.SettlementCurrency), "preview plan change", "immediate_charge.summary.settlement_currency")
	if err != nil {
		return bursar.PlanChangePreview{}, err
	}
	if summary.TotalAmount < 0 || summary.SettlementAmount < 0 {
		return bursar.PlanChangePreview{}, dodoResponseError("preview plan change", "immediate_charge.summary")
	}
	if response.ImmediateCharge.EffectiveAt.IsZero() {
		return bursar.PlanChangePreview{}, dodoResponseError("preview plan change", "immediate_charge.effective_at")
	}
	lineItems := make([]bursar.PlanChangeLineItem, 0, len(response.ImmediateCharge.LineItems))
	for _, item := range response.ImmediateCharge.LineItems {
		if item.Type != dodopayments.SubscriptionPreviewChangePlanResponseImmediateChargeLineItemsTypeSubscription {
			continue
		}
		productID, err := requireDodoResponseText(item.ProductID, "preview plan change", "immediate_charge.line_items.product_id")
		if err != nil {
			return bursar.PlanChangePreview{}, err
		}
		name, err := requireDodoResponseText(firstNonEmpty(item.Name, item.Description), "preview plan change", "immediate_charge.line_items.name")
		if err != nil {
			return bursar.PlanChangePreview{}, err
		}
		if item.UnitPrice < 0 || item.Quantity < 1 || item.Tax < 0 {
			return bursar.PlanChangePreview{}, dodoResponseError("preview plan change", "immediate_charge.line_items")
		}
		factor, err := dodoProviderFloatAmount(item.ProrationFactor, "preview plan change", "immediate_charge.line_items.proration_factor")
		if err != nil {
			return bursar.PlanChangePreview{}, err
		}
		lineCurrency, err := requireDodoResponseText(string(item.Currency), "preview plan change", "immediate_charge.line_items.currency")
		if err != nil {
			return bursar.PlanChangePreview{}, err
		}
		unitPrice := decimal.NewFromInt(item.UnitPrice)
		quantity := decimal.NewFromInt(item.Quantity)
		subtotal := unitPrice.Mul(quantity).Mul(factor).Round(0)
		lineItems = append(lineItems, bursar.PlanChangeLineItem{
			ProductID: productID, Name: name, UnitPrice: unitPrice, Quantity: item.Quantity,
			ProrationFactor: factor, Currency: lineCurrency, Tax: decimal.NewFromInt(item.Tax), Subtotal: subtotal,
		})
	}
	newPlanCurrency, err := requireDodoResponseText(string(response.NewPlan.Currency), "preview plan change", "new_plan.currency")
	if err != nil {
		return bursar.PlanChangePreview{}, err
	}
	if response.NewPlan.RecurringPreTaxAmount < 0 || response.NewPlan.NextBillingDate.IsZero() {
		return bursar.PlanChangePreview{}, dodoResponseError("preview plan change", "new_plan")
	}
	recurringAmount := decimal.NewFromInt(response.NewPlan.RecurringPreTaxAmount)
	nextBillingDate := response.NewPlan.NextBillingDate.UTC()
	customerCredits := decimal.NewFromInt(summary.CustomerCredits)
	preview := bursar.PlanChangePreview{
		TotalAmount:       decimal.NewFromInt(summary.TotalAmount),
		SettlementAmount:  decimal.NewFromInt(summary.SettlementAmount),
		Currency:          currency,
		LineItems:         lineItems,
		EffectiveAt:       response.ImmediateCharge.EffectiveAt.UTC(),
		RecurringAmount:   &recurringAmount,
		RecurringCurrency: newPlanCurrency,
		NextBillingDate:   &nextBillingDate,
		CustomerCredits:   &customerCredits,
	}
	if !summary.JSON.SettlementTax.IsNull() {
		if summary.SettlementTax < 0 {
			return bursar.PlanChangePreview{}, dodoResponseError("preview plan change", "immediate_charge.summary.settlement_tax")
		}
		tax := decimal.NewFromInt(summary.SettlementTax)
		preview.TaxAmount = &tax
	} else if !summary.JSON.Tax.IsNull() {
		if summary.Tax < 0 {
			return bursar.PlanChangePreview{}, dodoResponseError("preview plan change", "immediate_charge.summary.tax")
		}
		tax := decimal.NewFromInt(summary.Tax)
		preview.TaxAmount = &tax
	}
	return preview, nil
}

func dodoProviderFloatAmount(value float64, operation, field string) (bursar.Amount, error) {
	if math.IsNaN(value) || math.IsInf(value, 0) {
		return decimal.Zero, dodoResponseError(operation, field)
	}
	parsed, err := decimal.NewFromString(strconv.FormatFloat(value, 'f', -1, 64))
	if err != nil {
		return decimal.Zero, dodoResponseError(operation, field)
	}
	return parsed, nil
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if value = strings.TrimSpace(value); value != "" {
			return value
		}
	}
	return ""
}
