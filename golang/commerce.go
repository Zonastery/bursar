// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import (
	"context"
	"encoding/hex"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/shopspring/decimal"
)

// CheckoutIntent is Bursar's durable record for one hosted-provider checkout.
// A store creates it before contacting the provider so retries use the same
// idempotency key and never silently create a second financial checkout.
type CheckoutIntent struct {
	ID string
	// SubjectID is the authenticated actor that owns this intent and may read
	// its status. It is intentionally distinct from the financial AccountID
	// supplied when the checkout was created.
	SubjectID         string
	OfferKey          string
	Provider          string
	CheckoutKind      string
	ProductKey        string
	ProviderSessionID string
	ProviderURL       string
	Status            string
	IdempotencyKey    string
	RequestDigest     string
	ExpiresAt         time.Time
	CreatedAt         time.Time
	UpdatedAt         time.Time
}

// CheckoutIntentCreate is the durable input to create or replay a checkout.
// The catalog-derived provider product ID is kept server-side and never comes
// from a browser request.
type CheckoutIntentCreate struct {
	// SubjectID is the authenticated owner of the durable intent. It is the
	// scope used for all reads and state transitions.
	SubjectID string
	// AccountID is the financial subject that receives the subscription or
	// credits. It is part of the idempotency digest and provider metadata, not
	// a substitute for SubjectID authorization.
	AccountID      string
	Provider       string
	CheckoutKind   string
	ProductKey     string
	Quantity       int64
	IdempotencyKey string
	// RequestDigest identifies the resolved checkout request bound to a stable
	// key. Stores must return it on replay before Commerce contacts a provider.
	RequestDigest string
	ExpiresAt     time.Time
	Region        string
	Metadata      map[string]string
}

// CheckoutIntentUpdate records the externally-created session. A store should
// use the intent ID plus idempotency key as its concurrency boundary.
type CheckoutIntentUpdate struct {
	ProviderSessionID string
	ProviderURL       string
	CustomerID        string
	Status            string
}

// CommerceStore persists provider-neutral checkout intent state. Production
// stores must make CreateOrGetCheckoutIntent idempotent and subject scoped;
// this small interface keeps the SDK portable without embedding a second
// accounting engine in an HTTP handler.
type CommerceStore interface {
	CreateOrGetCheckoutIntent(context.Context, CheckoutIntentCreate) (CheckoutIntent, error)
	UpdateCheckoutIntent(context.Context, string, string, CheckoutIntentUpdate) error
	GetCheckoutIntent(context.Context, string, string) (*CheckoutIntent, error)
}

// CommerceOptions configures provider-backed hosted checkout. It is optional
// on Bursar; passing it requires a billing store and a durable CommerceStore.
type CommerceOptions struct {
	Store CommerceStore
	// StateStore is required for subscription management, invoices,
	// preferences, and plan changes. It is optional for credit-pack checkout
	// only deployments. When omitted, NewCommerceService reuses Store if it
	// also implements CommerceStateStore.
	StateStore CommerceStateStore
	// AutoRechargeStore enables the guarded saved-payment workflow. When
	// omitted, NewCommerceService reuses Store when it implements
	// AutoRechargeStore. A nil store leaves AutoRecharge unavailable rather
	// than silently performing in-memory financial decisions.
	AutoRechargeStore     AutoRechargeStore
	AutoRechargeOptions   AutoRechargeServiceOptions
	AutoRechargeReturnURL string
	Providers             *ProviderRegistry
	DefaultProvider       string
	// CheckoutIntentTTL controls how long a newly-created checkout intent
	// remains reusable. Zero uses the 24-hour default.
	CheckoutIntentTTL time.Duration
	// PreferenceDefaults supplies catalog/application defaults for accounts
	// without a persisted preference row. Nil fields retain Bursar defaults.
	PreferenceDefaults PreferencePatch
}

// CreateCheckoutInput is the application-facing checkout request. OfferKey is
// resolved against the active Bursar catalog before any provider API call.
type CreateCheckoutInput struct {
	// SubjectID is the authenticated actor that owns and may inspect the
	// checkout intent. It must not be inferred from AccountID.
	SubjectID string
	AccountID string
	OfferKey  string
	// Type optionally asserts the caller's intended offer kind ("subscription"
	// or "credit_pack"). An omitted type accepts either catalog kind.
	Type           string
	Quantity       *int64
	Region         string
	Provider       string
	SuccessURL     string
	CancelURL      string
	CustomerID     string
	CustomerEmail  string
	IdempotencyKey string
	Metadata       map[string]string
}

// CreateCheckoutResult returns the reusable durable intent and its provider
// hosted session. Repeated calls with the same stable key return the original
// intent rather than creating another charge opportunity.
type CreateCheckoutResult struct {
	Intent  CheckoutIntent
	Session CheckoutSession
}

// WebhookHandlingResult carries both provider authentication and Bursar's
// durable billing event outcome.
type WebhookHandlingResult struct {
	Webhook WebhookResult
	Billing BillingEventResult
}

// CommerceService composes verified providers, the active catalog, durable
// checkout state, and facade-owned billing ingestion. It has no router or
// framework dependency.
type CommerceService struct {
	billing *BillingService
	catalog *CatalogService
	credits *CreditsService
	store   CommerceStore
	state   CommerceStateStore
	// AutoRecharge is present only when a durable AutoRechargeStore was
	// configured. Its methods remain account-scoped and provider selection is
	// derived from persisted state or the active catalog.
	AutoRecharge             *CommerceAutoRecharge
	autoRechargeCore         *AutoRechargeService
	postDeductionUnsubscribe func()
	providers                *ProviderRegistry
	defaultProvider          string
	checkoutIntentTTL        time.Duration
	preferenceDefaults       PreferencePatch
}

// NewCommerceService constructs commerce from facade-owned capabilities.
func NewCommerceService(billing *BillingService, catalog *CatalogService, credits *CreditsService, options CommerceOptions) (*CommerceService, error) {
	if billing == nil {
		return nil, NewError("commerce requires billing", ErrorOptions{Code: ErrorCodeCommerceNotConfigured, Category: ErrorCategoryInvalidRequest})
	}
	if catalog == nil {
		return nil, NewError("commerce requires a catalog service", ErrorOptions{Code: ErrorCodeCommerceNotConfigured, Category: ErrorCategoryInvalidRequest})
	}
	if credits == nil {
		return nil, NewError("commerce requires credits", ErrorOptions{Code: ErrorCodeCommerceNotConfigured, Category: ErrorCategoryInvalidRequest})
	}
	if options.Store == nil {
		return nil, NewError("commerce requires a durable checkout-intent store", ErrorOptions{Code: ErrorCodeCommerceNotConfigured, Category: ErrorCategoryInvalidRequest})
	}
	if options.Providers == nil {
		return nil, NewError("commerce requires a provider registry", ErrorOptions{Code: ErrorCodeCommerceNotConfigured, Category: ErrorCategoryInvalidRequest})
	}
	if fallback := strings.TrimSpace(options.DefaultProvider); fallback != "" {
		configured := options.Providers.Configured()
		if !containsText(configured, fallback) {
			return nil, NewError("commerce default provider is not configured", ErrorOptions{
				Code:     ErrorCodeProviderSelectionFailed,
				Category: ErrorCategoryInvalidRequest,
				Details:  map[string]any{"provider": fallback},
			})
		}
	}
	checkoutIntentTTL := options.CheckoutIntentTTL
	if checkoutIntentTTL == 0 {
		checkoutIntentTTL = 24 * time.Hour
	}
	if checkoutIntentTTL < 0 {
		return nil, NewError("commerce checkout intent TTL must not be negative", ErrorOptions{Code: ErrorCodeConfig, Category: ErrorCategoryInvalidRequest})
	}
	state := options.StateStore
	if state == nil {
		state, _ = options.Store.(CommerceStateStore)
	}
	service := &CommerceService{
		billing:            billing,
		catalog:            catalog,
		credits:            credits,
		store:              options.Store,
		state:              state,
		providers:          options.Providers,
		defaultProvider:    strings.TrimSpace(options.DefaultProvider),
		checkoutIntentTTL:  checkoutIntentTTL,
		preferenceDefaults: options.PreferenceDefaults,
	}
	autoRecharge := billing.AutoRecharge
	if autoRecharge == nil {
		autoRechargeStore := options.AutoRechargeStore
		if autoRechargeStore == nil {
			autoRechargeStore, _ = options.Store.(AutoRechargeStore)
		}
		if autoRechargeStore != nil {
			created, createErr := NewAutoRechargeService(catalog, autoRechargeStore, options.AutoRechargeOptions)
			if createErr != nil {
				return nil, createErr
			}
			autoRecharge = created
			billing.AutoRecharge = autoRecharge
		}
	}
	if autoRecharge != nil {
		service.autoRechargeCore = autoRecharge
		service.AutoRecharge = &CommerceAutoRecharge{commerce: service, service: autoRecharge}
		service.postDeductionUnsubscribe = credits.AddPostDeductionHook(autoRecharge.PostDeductionHook(options.Providers, options.AutoRechargeReturnURL))
	}
	return service, nil
}

// ProviderForAccount resolves the provider already associated with an
// account, falling back to the configured default or an unambiguous provider.
// When offerKey is supplied, only providers with a catalog reference for that
// offer are eligible.
func (s *CommerceService) ProviderForAccount(ctx context.Context, accountID string, offerKey ...string) (PaymentProvider, error) {
	if s == nil || s.providers == nil {
		return nil, NewError("commerce providers are not configured", ErrorOptions{Code: ErrorCodeCommerceNotConfigured, Category: ErrorCategoryUnavailable})
	}
	accountID, err := requireText(accountID, "account ID")
	if err != nil {
		return nil, err
	}
	if len(offerKey) > 1 {
		return nil, NewError("at most one offer key may be supplied", ErrorOptions{Code: ErrorCodeConfig, Category: ErrorCategoryInvalidRequest})
	}
	current := ""
	if s.state != nil {
		subscription, stateErr := s.state.GetBillingSubscription(ctx, accountID, nil)
		if stateErr != nil {
			return nil, stateErr
		}
		if subscription != nil {
			current = strings.TrimSpace(subscription.Provider)
		}
		customer, customerErr := s.state.GetBillingCustomer(ctx, accountID, current)
		if customerErr != nil {
			return nil, customerErr
		}
		if current == "" && customer != nil {
			current = strings.TrimSpace(customer.Provider)
		}
	}
	compatible := s.providers.Configured()
	if len(offerKey) == 1 && strings.TrimSpace(offerKey[0]) != "" {
		config, configErr := s.catalog.GetConfig(ctx)
		if configErr != nil {
			return nil, configErr
		}
		offer, found := config.Commerce.Offers[strings.TrimSpace(offerKey[0])]
		if !found {
			return nil, NewError("checkout offer was not found", ErrorOptions{Code: ErrorCodeUnknownOffer, Category: ErrorCategoryNotFound, Details: map[string]any{"offer_key": offerKey[0]}})
		}
		compatible = sortedProviderKeys(offer.Providers)
	}
	return s.providers.Select(ctx, "", firstNonEmpty(current, s.defaultProvider), compatible)
}

// Providers returns configured provider names in deterministic order.
func (s *CommerceService) Providers() []string {
	if s == nil || s.providers == nil {
		return nil
	}
	return s.providers.Configured()
}

// ClearProviderCache discards lazily constructed provider instances. It is
// useful after application-owned credential rotation or provider
// reconfiguration; the next operation constructs a fresh instance.
func (s *CommerceService) ClearProviderCache() {
	if s == nil || s.providers == nil {
		return
	}
	s.providers.Clear()
}

// Close releases Commerce-owned registrations. It does not close a provider
// registry or a shared PostgresStore, whose lifecycle belongs to Bursar.
func (s *CommerceService) Close() {
	if s == nil || s.postDeductionUnsubscribe == nil {
		return
	}
	s.postDeductionUnsubscribe()
	s.postDeductionUnsubscribe = nil
}

// CreateCheckout resolves an active offer and creates or replays its hosted
// provider checkout. Product and quantity rules always come from the catalog.
func (s *CommerceService) CreateCheckout(ctx context.Context, input CreateCheckoutInput) (CreateCheckoutResult, error) {
	if s == nil || s.store == nil || s.providers == nil {
		return CreateCheckoutResult{}, NewError("commerce is not configured", ErrorOptions{Code: ErrorCodeCommerceNotConfigured, Category: ErrorCategoryInternal})
	}
	subjectID, err := requireText(input.SubjectID, "checkout subject ID")
	if err != nil {
		return CreateCheckoutResult{}, err
	}
	accountID, err := requireText(input.AccountID, "checkout account ID")
	if err != nil {
		return CreateCheckoutResult{}, err
	}
	offerKey, err := requireText(input.OfferKey, "checkout offer key")
	if err != nil {
		return CreateCheckoutResult{}, err
	}
	idempotencyKey, err := requireStableKey(input.IdempotencyKey, "checkout idempotency key")
	if err != nil {
		return CreateCheckoutResult{}, err
	}
	if strings.TrimSpace(input.SuccessURL) == "" || strings.TrimSpace(input.CancelURL) == "" {
		return CreateCheckoutResult{}, NewError("checkout success and cancel URLs are required", ErrorOptions{Code: ErrorCodeConfig, Category: ErrorCategoryInvalidRequest})
	}

	config, err := s.catalog.GetConfig(ctx)
	if err != nil {
		return CreateCheckoutResult{}, err
	}
	offer, found := config.Commerce.Offers[offerKey]
	if !found {
		return CreateCheckoutResult{}, NewError("checkout offer was not found", ErrorOptions{Code: ErrorCodeUnknownOffer, Category: ErrorCategoryNotFound, Details: map[string]any{"offer_key": offerKey}})
	}
	if requestedType := strings.ToLower(strings.TrimSpace(input.Type)); requestedType != "" {
		if requestedType != "subscription" && requestedType != "credit_pack" {
			return CreateCheckoutResult{}, NewError("checkout type is invalid", ErrorOptions{Code: ErrorCodeConfig, Category: ErrorCategoryInvalidRequest})
		}
		offerType := strings.ToLower(strings.TrimSpace(offer.Type))
		if offerType == "topup" {
			offerType = "credit_pack"
		}
		if requestedType != offerType {
			return CreateCheckoutResult{}, NewError("checkout type does not match the selected offer", ErrorOptions{Code: ErrorCodeUnknownOffer, Category: ErrorCategoryInvalidRequest, Details: map[string]any{"offer_key": offerKey, "type": requestedType}})
		}
	}
	quantity, err := checkoutQuantity(offer, input.Quantity)
	if err != nil {
		return CreateCheckoutResult{}, err
	}
	var currentSubscription *CommerceSubscription
	var accountCustomer *BillingCustomerRecord
	currentProvider := ""
	if s.state != nil {
		currentSubscription, err = s.state.GetBillingSubscription(ctx, accountID, nil)
		if err != nil {
			return CreateCheckoutResult{}, err
		}
		if currentSubscription != nil {
			currentProvider = strings.TrimSpace(currentSubscription.Provider)
		}
		accountCustomer, err = s.state.GetBillingCustomer(ctx, accountID, currentProvider)
		if err != nil {
			return CreateCheckoutResult{}, err
		}
	}
	if offer.Type == "subscription" {
		state, stateErr := s.requireState()
		if stateErr != nil {
			return CreateCheckoutResult{}, stateErr
		}
		blocking, blockingErr := state.GetBillingSubscription(ctx, accountID, blockingSubscriptionStatuses)
		if blockingErr != nil {
			return CreateCheckoutResult{}, blockingErr
		}
		if blocking != nil {
			return CreateCheckoutResult{}, NewError("account already has a blocking subscription", ErrorOptions{
				Code:     ErrorCodeActiveSubscription,
				Category: ErrorCategoryConflict,
				Details:  map[string]any{"account_id": accountID, "subscription_id": blocking.ProviderSubscriptionID},
			})
		}
	}
	compatible := sortedProviderKeys(offer.Providers)
	providerFallback := s.defaultProvider
	if currentProvider != "" {
		providerFallback = currentProvider
	} else if accountCustomer != nil {
		providerFallback = strings.TrimSpace(accountCustomer.Provider)
	}
	provider, err := s.providers.Select(ctx, input.Provider, providerFallback, compatible)
	if err != nil {
		return CreateCheckoutResult{}, err
	}
	if s.state != nil && (accountCustomer == nil || accountCustomer.Provider != provider.Name()) {
		accountCustomer, err = s.state.GetBillingCustomer(ctx, accountID, provider.Name())
		if err != nil {
			return CreateCheckoutResult{}, err
		}
	}
	providerRef, exists := offer.Providers[provider.Name()]
	if !exists {
		return CreateCheckoutResult{}, NewError("selected provider has no offer reference", ErrorOptions{Code: ErrorCodeProviderSelectionFailed, Category: ErrorCategoryInvalidRequest, Details: map[string]any{"provider": provider.Name(), "offer_key": offerKey}})
	}
	productID := providerProductID(providerRef)
	if productID == "" {
		return CreateCheckoutResult{}, NewError("offer provider reference has no product identifier", ErrorOptions{Code: ErrorCodeUnknownOffer, Category: ErrorCategoryInvalidRequest, Details: map[string]any{"provider": provider.Name(), "offer_key": offerKey}})
	}

	metadata := cloneStringMap(input.Metadata)
	metadata["bursar_account_id"] = accountID
	if offer.Type == "subscription" {
		if offer.Plan != nil {
			metadata["plan_slug"] = strings.TrimSpace(*offer.Plan)
		}
		if offer.BillingInterval != nil {
			metadata["billing_interval"] = strings.TrimSpace(offer.BillingInterval.Unit)
		}
	} else {
		if offer.CreditsPerUnit != nil {
			metadata["credits"] = offer.CreditsPerUnit.Mul(decimal.NewFromInt(quantity)).String()
		}
		metadata["quantity"] = strconv.FormatInt(quantity, 10)
	}
	// A durable state store is authoritative for provider customer identity;
	// accepting a caller-supplied ID in that mode could attach checkout to a
	// different customer's provider record. Keep the legacy input only for
	// checkout-only deployments that have no state capability.
	customerID := ""
	if s.state == nil {
		customerID = strings.TrimSpace(input.CustomerID)
	}
	if accountCustomer != nil && strings.TrimSpace(accountCustomer.ProviderCustomerID) != "" {
		customerID = strings.TrimSpace(accountCustomer.ProviderCustomerID)
	}
	create := CheckoutIntentCreate{
		SubjectID:      subjectID,
		AccountID:      accountID,
		Provider:       provider.Name(),
		CheckoutKind:   checkoutIntentKind(offer.Type),
		ProductKey:     offerKey,
		Quantity:       quantity,
		IdempotencyKey: idempotencyKey,
		ExpiresAt:      time.Now().UTC().Add(s.checkoutIntentTTL),
		Region:         input.Region,
		Metadata:       metadata,
	}
	digest, err := checkoutRequestDigest(create)
	if err != nil {
		return CreateCheckoutResult{}, err
	}
	create.RequestDigest = hex.EncodeToString(digest[:])
	intent, err := s.store.CreateOrGetCheckoutIntent(ctx, create)
	if err != nil {
		return CreateCheckoutResult{}, err
	}
	if intent.RequestDigest == "" || !strings.EqualFold(intent.RequestDigest, create.RequestDigest) {
		return CreateCheckoutResult{Intent: intent}, NewError("checkout idempotency key is already bound to a different request", ErrorOptions{
			Code:     ErrorCodeCheckoutConflict,
			Category: ErrorCategoryConflict,
			Details:  map[string]any{"checkout_intent_id": intent.ID},
		})
	}
	switch intent.Status {
	case "completed":
		return CreateCheckoutResult{Intent: intent}, NewError("checkout was already completed", ErrorOptions{Code: ErrorCodeCheckoutCompleted, Category: ErrorCategoryConflict, Details: map[string]any{"checkout_intent_id": intent.ID}})
	case "open":
		// Continue below.
	default:
		return CreateCheckoutResult{Intent: intent}, NewError("checkout operation is terminal; use a new idempotency key", ErrorOptions{Code: ErrorCodeCheckoutConflict, Category: ErrorCategoryConflict, Details: map[string]any{"checkout_intent_id": intent.ID, "status": intent.Status}})
	}
	if !intent.ExpiresAt.IsZero() && !intent.ExpiresAt.After(time.Now().UTC()) {
		if err := s.store.UpdateCheckoutIntent(ctx, intent.ID, subjectID, CheckoutIntentUpdate{Status: "expired"}); err != nil {
			return CreateCheckoutResult{Intent: intent}, err
		}
		intent.Status = "expired"
		return CreateCheckoutResult{Intent: intent}, NewError("checkout operation expired; use a new idempotency key", ErrorOptions{Code: ErrorCodeCheckoutConflict, Category: ErrorCategoryConflict, Details: map[string]any{"checkout_intent_id": intent.ID}})
	}
	metadata["checkout_intent_id"] = intent.ID
	if intent.ProviderSessionID != "" && intent.ProviderURL != "" {
		status, statusErr := s.resolveCheckoutStatus(ctx, intent, subjectID)
		if statusErr != nil {
			return CreateCheckoutResult{Intent: intent}, statusErr
		}
		switch status {
		case CheckoutStatusSucceeded:
			return CreateCheckoutResult{Intent: intent}, NewError("checkout was already completed", ErrorOptions{Code: ErrorCodeCheckoutCompleted, Category: ErrorCategoryConflict, Details: map[string]any{"checkout_intent_id": intent.ID}})
		case CheckoutStatusFailed, CheckoutStatusExpired:
			return CreateCheckoutResult{Intent: intent}, NewError("checkout operation is no longer active; use a new idempotency key", ErrorOptions{Code: ErrorCodeCheckoutConflict, Category: ErrorCategoryConflict, Details: map[string]any{"checkout_intent_id": intent.ID, "status": status}})
		}
		return CreateCheckoutResult{Intent: intent, Session: CheckoutSession{ID: intent.ProviderSessionID, URL: intent.ProviderURL}}, nil
	}

	session, err := provider.CreateCheckoutSession(ctx, CheckoutSessionRequest{
		AccountID:      accountID,
		ProductID:      productID,
		Mode:           checkoutMode(offer.Type),
		Quantity:       quantity,
		SuccessURL:     replaceCheckoutIntentURL(input.SuccessURL, intent.ID),
		CancelURL:      replaceCheckoutIntentURL(input.CancelURL, intent.ID),
		CustomerID:     customerID,
		CustomerEmail:  input.CustomerEmail,
		Metadata:       metadata,
		IdempotencyKey: idempotencyKey,
	})
	if err != nil {
		return CreateCheckoutResult{Intent: intent}, err
	}
	// A provider may create or attach a customer while creating the checkout.
	// Persist that trusted response before publishing the session on the intent,
	// so later portal and saved-payment flows never need a caller-supplied
	// provider customer ID. Checkout-only stores may omit this optional writer.
	if customerID := strings.TrimSpace(session.CustomerID); customerID != "" {
		writer, ok := any(s.state).(BillingCustomerWriter)
		if !ok {
			writer, ok = s.store.(BillingCustomerWriter)
		}
		if ok {
			if err := writer.UpsertBillingCustomer(ctx, BillingCustomerRecord{
				Provider:           provider.Name(),
				ProviderCustomerID: customerID,
				AccountID:          accountID,
				Email:              strings.TrimSpace(input.CustomerEmail),
			}); err != nil {
				return CreateCheckoutResult{Intent: intent, Session: session}, err
			}
		}
	}
	if err := s.store.UpdateCheckoutIntent(ctx, intent.ID, subjectID, CheckoutIntentUpdate{
		ProviderSessionID: session.ID,
		ProviderURL:       session.URL,
		CustomerID:        session.CustomerID,
		Status:            "open",
	}); err != nil {
		return CreateCheckoutResult{Intent: intent, Session: session}, err
	}
	intent.ProviderSessionID = session.ID
	intent.ProviderURL = session.URL
	intent.Status = "open"
	return CreateCheckoutResult{Intent: intent, Session: session}, nil
}

// GetCheckoutIntent returns the authenticated-subject-scoped durable checkout
// intent. AccountID is deliberately not accepted here: it is the financial
// recipient, while SubjectID is the authority boundary for intent visibility.
func (s *CommerceService) GetCheckoutIntent(ctx context.Context, intentID, subjectID string) (*CheckoutIntent, error) {
	if s == nil || s.store == nil {
		return nil, NewError("commerce is not configured", ErrorOptions{Code: ErrorCodeCommerceNotConfigured, Category: ErrorCategoryInternal})
	}
	if _, err := requireText(intentID, "checkout intent ID"); err != nil {
		return nil, err
	}
	if _, err := requireText(subjectID, "checkout subject ID"); err != nil {
		return nil, err
	}
	return s.store.GetCheckoutIntent(ctx, intentID, subjectID)
}

// HandleWebhook verifies a raw provider request, then sends only the verified
// normalized event through the facade-owned billing lifecycle.
func (s *CommerceService) HandleWebhook(ctx context.Context, providerName string, request WebhookRequest) (WebhookHandlingResult, error) {
	if s == nil || s.providers == nil || s.billing == nil {
		return WebhookHandlingResult{}, NewError("commerce is not configured", ErrorOptions{Code: ErrorCodeCommerceNotConfigured, Category: ErrorCategoryInternal})
	}
	providerName = strings.TrimSpace(providerName)
	if providerName == "" {
		configured := s.providers.Configured()
		if len(configured) != 1 {
			return WebhookHandlingResult{}, NewError("webhook provider selection is ambiguous", ErrorOptions{Code: ErrorCodeProviderSelectionFailed, Category: ErrorCategoryInvalidRequest})
		}
		providerName = configured[0]
	}
	provider, err := s.providers.Get(ctx, providerName)
	if err != nil {
		return WebhookHandlingResult{}, err
	}
	webhook, err := provider.HandleWebhook(ctx, request)
	if err != nil {
		return WebhookHandlingResult{}, err
	}
	if webhook.Event == nil {
		return WebhookHandlingResult{}, NewError("provider accepted webhook without a normalized event", ErrorOptions{Code: ErrorCodeProviderResponseInvalid, Category: ErrorCategoryInvalidRequest})
	}
	billingResult, err := s.billing.Ingest(ctx, *webhook.Event)
	if err != nil {
		return WebhookHandlingResult{Webhook: webhook}, err
	}
	return WebhookHandlingResult{Webhook: webhook, Billing: billingResult}, nil
}

func checkoutQuantity(offer CommerceOffer, requested *int64) (int64, error) {
	if offer.Type == "subscription" {
		if requested != nil && *requested != 1 {
			return 0, NewError("subscription checkout quantity must be 1", ErrorOptions{Code: ErrorCodeInvalidOfferQuantity, Category: ErrorCategoryInvalidRequest})
		}
		return 1, nil
	}
	quantity := int64(1)
	if offer.Quantity != nil && offer.Quantity.Default > 0 {
		quantity = int64(offer.Quantity.Default)
	}
	if requested != nil {
		quantity = *requested
	}
	if quantity < 1 {
		return 0, NewError("checkout quantity must be positive", ErrorOptions{Code: ErrorCodeInvalidOfferQuantity, Category: ErrorCategoryInvalidRequest})
	}
	if offer.Quantity != nil {
		if offer.Quantity.Minimum > 0 && quantity < int64(offer.Quantity.Minimum) {
			return 0, NewError("checkout quantity is below offer minimum", ErrorOptions{Code: ErrorCodeInvalidOfferQuantity, Category: ErrorCategoryInvalidRequest})
		}
		if offer.Quantity.Maximum > 0 && quantity > int64(offer.Quantity.Maximum) {
			return 0, NewError("checkout quantity exceeds offer maximum", ErrorOptions{Code: ErrorCodeInvalidOfferQuantity, Category: ErrorCategoryInvalidRequest})
		}
	}
	return quantity, nil
}

func replaceCheckoutIntentURL(value, intentID string) string {
	return strings.ReplaceAll(value, "{intentId}", intentID)
}

func providerProductID(reference ProviderReference) string {
	for _, value := range []*string{reference.PriceID, reference.ProductID, reference.ExternalID} {
		if value != nil && strings.TrimSpace(*value) != "" {
			return strings.TrimSpace(*value)
		}
	}
	return ""
}

func checkoutMode(offerType string) string {
	if offerType == "subscription" {
		return "subscription"
	}
	return "payment"
}

func checkoutIntentKind(offerType string) string {
	if offerType == "subscription" {
		return "subscription"
	}
	return "credit_topup"
}

func sortedProviderKeys(values map[string]ProviderReference) []string {
	keys := make([]string, 0, len(values))
	for key := range values {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	return keys
}

func containsText(values []string, target string) bool {
	for _, value := range values {
		if value == target {
			return true
		}
	}
	return false
}

func cloneStringMap(values map[string]string) map[string]string {
	clone := make(map[string]string, len(values)+1)
	for key, value := range values {
		clone[key] = value
	}
	return clone
}
