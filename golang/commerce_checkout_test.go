package bursar

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"strings"
	"testing"
	"time"
)

type checkoutStoreStub struct {
	CreditStore
	CommerceStore
	CommerceStateStore
	config          map[string]any
	intent          CheckoutIntent
	created         CheckoutIntentCreate
	updated         CheckoutIntentUpdate
	subscription    *CommerceSubscription
	customers       map[string]*BillingCustomerRecord
	preferences     *BillingPreferences
	catalogErr      error
	createErr       error
	updateErr       error
	subscriptionErr error
	customerErr     error
	matchDigest     bool
}

func (s *checkoutStoreStub) GetActiveCatalog(context.Context) (*CatalogRevision, error) {
	if s.catalogErr != nil {
		return nil, s.catalogErr
	}
	return &CatalogRevision{ID: "catalog-1", Version: 1, Config: s.config}, nil
}

func (s *checkoutStoreStub) CreateOrGetCheckoutIntent(_ context.Context, input CheckoutIntentCreate) (CheckoutIntent, error) {
	if s.createErr != nil {
		return CheckoutIntent{}, s.createErr
	}
	if s.matchDigest {
		s.intent.RequestDigest = input.RequestDigest
	}
	s.created = input
	if s.intent.ID == "" {
		s.intent = CheckoutIntent{
			ID:            "intent-1",
			SubjectID:     input.SubjectID,
			RequestDigest: input.RequestDigest,
			Status:        "open",
			ExpiresAt:     input.ExpiresAt,
		}
	}
	return s.intent, nil
}

func (s *checkoutStoreStub) UpdateCheckoutIntent(_ context.Context, _ string, _ string, update CheckoutIntentUpdate) error {
	if s.updateErr != nil {
		return s.updateErr
	}
	s.updated = update
	if update.ProviderSessionID != "" {
		s.intent.ProviderSessionID = update.ProviderSessionID
		s.intent.ProviderURL = update.ProviderURL
	}
	if update.Status != "" {
		s.intent.Status = update.Status
	}
	return nil
}

func (s *checkoutStoreStub) GetCheckoutIntent(context.Context, string, string) (*CheckoutIntent, error) {
	return &s.intent, nil
}

func (s *checkoutStoreStub) GetBillingCustomer(_ context.Context, _ string, provider string) (*BillingCustomerRecord, error) {
	if s.customerErr != nil {
		return nil, s.customerErr
	}
	if provider != "" {
		return s.customers[provider], nil
	}
	for _, customer := range s.customers {
		return customer, nil
	}
	return nil, nil
}

func (s *checkoutStoreStub) GetBillingSubscription(_ context.Context, _ string, statuses []string) (*CommerceSubscription, error) {
	if s.subscriptionErr != nil {
		return nil, s.subscriptionErr
	}
	if s.subscription == nil || (statuses != nil && !containsText(statuses, s.subscription.Status)) {
		return nil, nil
	}
	return s.subscription, nil
}

func (s *checkoutStoreStub) GetBillingPreferences(context.Context, string) (*BillingPreferences, error) {
	return s.preferences, nil
}

func (s *checkoutStoreStub) UpsertBillingPreferences(_ context.Context, preferences BillingPreferences) error {
	s.preferences = &preferences
	return nil
}

type checkoutBillingStoreStub struct{ BillingStore }

func (checkoutBillingStoreStub) ProviderEnvironment() ProviderEnvironment {
	return ProviderEnvironmentTest
}

func (checkoutBillingStoreStub) ClaimBillingEvent(_ context.Context, event BillingEvent, _ map[string]any) (BillingEventClaim, error) {
	return BillingEventClaim{State: BillingEventClaimed, ClaimToken: "claim-1", BillingEventID: event.ID}, nil
}

func (checkoutBillingStoreStub) CompleteBillingEvent(context.Context, string, string, string) (bool, error) {
	return true, nil
}

func (checkoutBillingStoreStub) FailBillingEvent(context.Context, string, string, string, string) (bool, error) {
	return true, nil
}

type checkoutProviderStub struct {
	name    string
	request CheckoutSessionRequest
	session CheckoutSession
	err     error
}

func (p *checkoutProviderStub) Name() string { return p.name }

func (p *checkoutProviderStub) CreateCheckoutSession(_ context.Context, request CheckoutSessionRequest) (CheckoutSession, error) {
	p.request = request
	if p.err != nil {
		return CheckoutSession{}, p.err
	}
	if p.session.ID != "" || p.session.URL != "" || p.session.CustomerID != "" {
		return p.session, nil
	}
	return CheckoutSession{ID: "session-" + p.name, URL: "https://checkout.test/" + p.name}, nil
}

func (p *checkoutProviderStub) HandleWebhook(context.Context, WebhookRequest) (WebhookResult, error) {
	return WebhookResult{
		Received: true,
		Provider: p.name,
		Event:    &BillingEvent{ID: "expired-1", Provider: p.name, Type: BillingEventCheckoutExpired, OccurredAt: time.Now().UTC()},
	}, nil
}

func checkoutTestConfig(t *testing.T) map[string]any {
	t.Helper()
	data, err := os.ReadFile("../common/commerce-parity.json")
	if err != nil {
		t.Fatal(err)
	}
	var fixture struct {
		Catalog map[string]any `json:"catalog"`
	}
	if err := json.Unmarshal(data, &fixture); err != nil {
		t.Fatal(err)
	}
	commerce := fixture.Catalog["commerce"].(map[string]any)
	commerce["providers"] = map[string]any{
		"alpha":  map[string]any{"type": "custom", "adapter": "alpha"},
		"stripe": map[string]any{"type": "custom", "adapter": "stripe"},
		"dodo":   map[string]any{"type": "custom", "adapter": "dodo"},
	}
	offers := commerce["offers"].(map[string]any)
	offer := offers["starter_month"].(map[string]any)
	offer["providers"] = map[string]any{
		"stripe": map[string]any{"type": "custom_object", "object_kind": "subscription", "external_id": "stripe-starter"},
		"dodo":   map[string]any{"type": "custom_object", "object_kind": "subscription", "external_id": "dodo-starter"},
	}
	return fixture.Catalog
}

func newCheckoutService(t *testing.T, store *checkoutStoreStub, providers map[string]*checkoutProviderStub, ttl time.Duration, preferenceDefaults ...PreferencePatch) *CommerceService {
	t.Helper()
	factories := make(map[string]ProviderFactory, len(providers))
	for name, provider := range providers {
		name, provider := name, provider
		factories[name] = func(context.Context, ProviderFactoryContext) (PaymentProvider, error) {
			return provider, nil
		}
	}
	registry, err := NewProviderRegistry(ProviderFactoryContext{ProviderEnvironment: ProviderEnvironmentTest}, factories)
	if err != nil {
		t.Fatal(err)
	}
	catalog := &CatalogService{store: store}
	billing := &BillingService{store: checkoutBillingStoreStub{}}
	defaultProvider := ""
	if _, ok := providers["dodo"]; ok {
		defaultProvider = "dodo"
	}
	defaults := PreferencePatch{}
	if len(preferenceDefaults) > 0 {
		defaults = preferenceDefaults[0]
	}
	service, err := NewCommerceService(billing, catalog, &CreditsService{}, CommerceOptions{
		Store:              store,
		StateStore:         store,
		Providers:          registry,
		DefaultProvider:    defaultProvider,
		CheckoutIntentTTL:  ttl,
		PreferenceDefaults: defaults,
	})
	if err != nil {
		t.Fatal(err)
	}
	return service
}

func TestCreateCheckoutUsesAccountProviderCustomerAndProtectedMetadata(t *testing.T) {
	store := &checkoutStoreStub{
		config:       checkoutTestConfig(t),
		subscription: &CommerceSubscription{Provider: "stripe", Status: "canceled"},
		customers: map[string]*BillingCustomerRecord{
			"stripe": {Provider: "stripe", ProviderCustomerID: "cus-persisted", AccountID: "account-1"},
		},
	}
	stripe := &checkoutProviderStub{name: "stripe"}
	dodo := &checkoutProviderStub{name: "dodo"}
	service := newCheckoutService(t, store, map[string]*checkoutProviderStub{"stripe": stripe, "dodo": dodo}, 2*time.Hour)

	result, err := service.CreateCheckout(context.Background(), CreateCheckoutInput{
		SubjectID:      "subject-1",
		AccountID:      "account-1",
		OfferKey:       "starter_month",
		SuccessURL:     "https://app.test/success/{intentId}",
		CancelURL:      "https://app.test/cancel/{intentId}",
		CustomerID:     "cus-attacker-supplied",
		IdempotencyKey: "checkout-1",
		Metadata: map[string]string{
			"bursar_account_id":  "attacker-account",
			"checkout_intent_id": "attacker-intent",
			"plan_slug":          "attacker-plan",
			"billing_interval":   "attacker-interval",
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	if result.Intent.ID != "intent-1" {
		t.Fatalf("intent ID = %q, want intent-1", result.Intent.ID)
	}
	if stripe.request.CustomerID != "cus-persisted" || dodo.request.CustomerID != "" {
		t.Fatalf("provider customer IDs = stripe %q, dodo %q", stripe.request.CustomerID, dodo.request.CustomerID)
	}
	if stripe.request.SuccessURL != "https://app.test/success/intent-1" || stripe.request.CancelURL != "https://app.test/cancel/intent-1" {
		t.Fatalf("intent URLs = %q, %q", stripe.request.SuccessURL, stripe.request.CancelURL)
	}
	wantMetadata := map[string]string{
		"bursar_account_id":  "account-1",
		"checkout_intent_id": "intent-1",
		"plan_slug":          "starter",
		"billing_interval":   "month",
	}
	for key, want := range wantMetadata {
		if got := stripe.request.Metadata[key]; got != want {
			t.Errorf("metadata[%q] = %q, want %q", key, got, want)
		}
	}
	if got := store.created.ExpiresAt.Sub(time.Now().UTC()); got < 90*time.Minute || got > 2*time.Hour {
		t.Fatalf("checkout TTL = %s, want approximately 2h", got)
	}
}

func TestCreateCheckoutRejectsSubscriptionQuantity(t *testing.T) {
	store := &checkoutStoreStub{config: checkoutTestConfig(t)}
	service := newCheckoutService(t, store, map[string]*checkoutProviderStub{"stripe": {name: "stripe"}}, 0)
	quantity := int64(2)
	_, err := service.CreateCheckout(context.Background(), CreateCheckoutInput{
		SubjectID:      "subject-1",
		AccountID:      "account-1",
		OfferKey:       "starter_month",
		Quantity:       &quantity,
		SuccessURL:     "https://app.test/success",
		CancelURL:      "https://app.test/cancel",
		IdempotencyKey: "checkout-quantity",
	})
	var bursarErr *BursarError
	if !errors.As(err, &bursarErr) || bursarErr.Code != ErrorCodeInvalidOfferQuantity {
		t.Fatalf("error = %v, want invalid quantity, %T", err, err)
	}
	if store.created.IdempotencyKey != "" {
		t.Fatal("checkout intent was created for invalid subscription quantity")
	}
}

func TestCreateCheckoutRejectsOfferTypeMismatchAndDoesNotTrustCustomerID(t *testing.T) {
	store := &checkoutStoreStub{config: checkoutTestConfig(t)}
	provider := &checkoutProviderStub{name: "stripe"}
	service := newCheckoutService(t, store, map[string]*checkoutProviderStub{"stripe": provider}, 0)
	_, err := service.CreateCheckout(context.Background(), CreateCheckoutInput{
		SubjectID: "subject-1", AccountID: "account-1", OfferKey: "starter_month", Type: "credit_pack", Provider: "stripe",
		SuccessURL: "https://app.test/success", CancelURL: "https://app.test/cancel", IdempotencyKey: "type-mismatch",
	})
	if err == nil {
		t.Fatal("expected checkout type mismatch")
	}

	result, err := service.CreateCheckout(context.Background(), CreateCheckoutInput{
		SubjectID: "subject-1", AccountID: "account-1", OfferKey: "starter_month", Type: "subscription", Provider: "stripe",
		CustomerID: "cus-untrusted", SuccessURL: "https://app.test/success", CancelURL: "https://app.test/cancel", IdempotencyKey: "customer-authority",
	})
	if err != nil {
		t.Fatal(err)
	}
	if result.Intent.ID == "" || provider.request.CustomerID != "" {
		t.Fatalf("durable state-less customer resolution = intent %q, provider customer %q; want empty customer", result.Intent.ID, provider.request.CustomerID)
	}
}

func TestCreateCheckoutAcceptsCreditPackTypeForTopupOffer(t *testing.T) {
	config := checkoutTestConfig(t)
	commerce := config["commerce"].(map[string]any)
	offers := commerce["offers"].(map[string]any)
	starter := offers["starter_month"].(map[string]any)
	offers["pack_small"] = map[string]any{
		"display_name":     "Small credit pack",
		"type":             "topup",
		"price":            starter["price"],
		"providers":        map[string]any{"stripe": map[string]any{"type": "custom_object", "object_kind": "one_time", "external_id": "stripe-pack-small"}},
		"credits_per_unit": "10",
		"quantity":         map[string]any{"minimum": 1, "maximum": 10, "default": 1},
		"bucket":           "general",
		"lot_behavior":     "separate_lots",
	}
	store := &checkoutStoreStub{config: config}
	provider := &checkoutProviderStub{name: "stripe"}
	service := newCheckoutService(t, store, map[string]*checkoutProviderStub{"stripe": provider}, 0)
	quantity := int64(2)
	result, err := service.CreateCheckout(context.Background(), CreateCheckoutInput{
		SubjectID: "subject-1", AccountID: "account-1", OfferKey: "pack_small", Type: "credit_pack", Provider: "stripe",
		Quantity: &quantity, SuccessURL: "https://app.test/success", CancelURL: "https://app.test/cancel", IdempotencyKey: "credit-pack-type",
	})
	if err != nil {
		t.Fatal(err)
	}
	if result.Intent.ID == "" || provider.request.Quantity != 2 || provider.request.Metadata["quantity"] != "2" {
		t.Fatalf("credit-pack checkout = intent %q quantity %d metadata %q; want persisted intent, quantity 2", result.Intent.ID, provider.request.Quantity, provider.request.Metadata["quantity"])
	}
}

func TestProviderForAccountPrefersPersistedSubscriptionProvider(t *testing.T) {
	store := &checkoutStoreStub{
		config:       checkoutTestConfig(t),
		subscription: &CommerceSubscription{Provider: "stripe", Status: "active"},
		customers:    map[string]*BillingCustomerRecord{"dodo": {Provider: "dodo", ProviderCustomerID: "cus-dodo"}},
	}
	service := newCheckoutService(t, store, map[string]*checkoutProviderStub{
		"stripe": {name: "stripe"},
		"dodo":   {name: "dodo"},
	}, 0)
	provider, err := service.ProviderForAccount(context.Background(), "account-1", "starter_month")
	if err != nil {
		t.Fatal(err)
	}
	if provider.Name() != "stripe" {
		t.Fatalf("provider = %q, want persisted subscription provider stripe", provider.Name())
	}
}

func TestCommercePreferenceDefaultsApplyBeforePatch(t *testing.T) {
	store := &checkoutStoreStub{config: checkoutTestConfig(t)}
	defaults := PreferencePatch{AutoRecharge: boolPointer(true), OverageProtection: boolPointer(false)}
	service := newCheckoutService(t, store, map[string]*checkoutProviderStub{"stripe": {name: "stripe"}}, 0, defaults)
	preferences, err := service.UpdatePreferences(context.Background(), "account-1", PreferencePatch{UsageAlerts: boolPointer(false)})
	if err != nil {
		t.Fatal(err)
	}
	if !preferences.AutoRecharge || preferences.OverageProtection || preferences.UsageAlerts {
		t.Fatalf("preferences = %+v, want option defaults plus patch", preferences)
	}
}

func boolPointer(value bool) *bool { return &value }

type updateOnlyPaymentMethodProvider struct{ checkoutProviderStub }

func (p *updateOnlyPaymentMethodProvider) CreateUpdatePaymentMethodSession(context.Context, string, string, string) (string, error) {
	return "https://provider.test/update", nil
}

func TestPortalUsesOnlyTheSelectedPaymentMethodCapability(t *testing.T) {
	store := &checkoutStoreStub{
		customers:    map[string]*BillingCustomerRecord{"stripe": {Provider: "stripe", ProviderCustomerID: "cus-1"}},
		subscription: &CommerceSubscription{Provider: "stripe", ProviderSubscriptionID: "sub-1", Status: "active"},
	}
	provider := &updateOnlyPaymentMethodProvider{checkoutProviderStub{name: "stripe"}}
	registry, err := NewProviderRegistry(ProviderFactoryContext{ProviderEnvironment: ProviderEnvironmentTest}, map[string]ProviderFactory{
		"stripe": func(context.Context, ProviderFactoryContext) (PaymentProvider, error) { return provider, nil },
	})
	if err != nil {
		t.Fatal(err)
	}
	service, err := NewCommerceService(&BillingService{store: checkoutBillingStoreStub{}}, &CatalogService{store: store}, &CreditsService{}, CommerceOptions{
		Store: store, StateStore: store, Providers: registry,
	})
	if err != nil {
		t.Fatal(err)
	}
	url, err := service.CreatePortalSession(context.Background(), PortalSessionInput{AccountID: "account-1", Purpose: PortalPurposePaymentMethod, ReturnURL: "https://app.test/return"})
	if err != nil {
		t.Fatal(err)
	}
	if url != "https://provider.test/update" {
		t.Fatalf("portal URL = %q, want update capability URL", url)
	}
}

func TestCreateCheckoutRejectsPersistedBlockingSubscription(t *testing.T) {
	store := &checkoutStoreStub{
		config: checkoutTestConfig(t),
		subscription: &CommerceSubscription{
			Provider:               "stripe",
			ProviderSubscriptionID: "sub-active",
			Status:                 "active",
		},
	}
	provider := &checkoutProviderStub{name: "stripe"}
	service := newCheckoutService(t, store, map[string]*checkoutProviderStub{"stripe": provider}, 0)

	_, err := service.CreateCheckout(context.Background(), CreateCheckoutInput{
		SubjectID:      "subject-1",
		AccountID:      "account-1",
		OfferKey:       "starter_month",
		SuccessURL:     "https://app.test/success",
		CancelURL:      "https://app.test/cancel",
		IdempotencyKey: "checkout-active-subscription",
	})
	var bursarErr *BursarError
	if !errors.As(err, &bursarErr) || bursarErr.Code != ErrorCodeActiveSubscription {
		t.Fatalf("error = %v, want active subscription, %T", err, err)
	}
	if store.created.IdempotencyKey != "" || provider.request.IdempotencyKey != "" {
		t.Fatal("checkout side effects occurred for an account with a blocking subscription")
	}
}

func TestHandleWebhookSelectsOnlyConfiguredProviderWhenOmitted(t *testing.T) {
	store := &checkoutStoreStub{config: checkoutTestConfig(t)}
	service := newCheckoutService(t, store, map[string]*checkoutProviderStub{"stripe": {name: "stripe"}}, 0)
	result, err := service.HandleWebhook(context.Background(), "", WebhookRequest{RawBody: []byte("raw")})
	if err != nil {
		t.Fatal(err)
	}
	if !result.Billing.Handled || !result.Billing.Ignored {
		t.Fatalf("billing result = %+v, want handled ignored", result.Billing)
	}

	ambiguous := newCheckoutService(t, store, map[string]*checkoutProviderStub{
		"stripe": {name: "stripe"},
		"dodo":   {name: "dodo"},
	}, 0)
	if _, err := ambiguous.HandleWebhook(context.Background(), "", WebhookRequest{RawBody: []byte("raw")}); err == nil || !strings.Contains(err.Error(), "ambiguous") {
		t.Fatalf("ambiguous webhook error = %v", err)
	}
}
