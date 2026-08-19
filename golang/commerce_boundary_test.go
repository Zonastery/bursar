package bursar

import (
	"context"
	"errors"
	"testing"
	"time"
)

func commerceBoundaryRegistry(t *testing.T, providers map[string]*checkoutProviderStub) *ProviderRegistry {
	t.Helper()
	factories := make(map[string]ProviderFactory, len(providers))
	for name, provider := range providers {
		provider := provider
		factories[name] = func(context.Context, ProviderFactoryContext) (PaymentProvider, error) {
			return provider, nil
		}
	}
	registry, err := NewProviderRegistry(ProviderFactoryContext{ProviderEnvironment: ProviderEnvironmentTest}, factories)
	if err != nil {
		t.Fatal(err)
	}
	return registry
}

func validCommerceCheckoutInput() CreateCheckoutInput {
	return CreateCheckoutInput{
		SubjectID: "subject-1", AccountID: "account-1", OfferKey: "starter_month",
		SuccessURL: "https://app.test/success", CancelURL: "https://app.test/cancel",
		IdempotencyKey: "checkout-1", Provider: "stripe",
	}
}

func TestCommerceConstructionRejectsMissingOrUnsafeCapabilities(t *testing.T) {
	store := &checkoutStoreStub{}
	registry := commerceBoundaryRegistry(t, map[string]*checkoutProviderStub{"stripe": {name: "stripe"}})
	billing, catalog, credits := &BillingService{}, &CatalogService{}, &CreditsService{}
	tests := []struct {
		name    string
		billing *BillingService
		catalog *CatalogService
		credits *CreditsService
		options CommerceOptions
	}{
		{"billing", nil, catalog, credits, CommerceOptions{Store: store, Providers: registry}},
		{"catalog", billing, nil, credits, CommerceOptions{Store: store, Providers: registry}},
		{"credits", billing, catalog, nil, CommerceOptions{Store: store, Providers: registry}},
		{"store", billing, catalog, credits, CommerceOptions{Providers: registry}},
		{"providers", billing, catalog, credits, CommerceOptions{Store: store}},
		{"default provider", billing, catalog, credits, CommerceOptions{Store: store, Providers: registry, DefaultProvider: "dodo"}},
		{"negative ttl", billing, catalog, credits, CommerceOptions{Store: store, Providers: registry, CheckoutIntentTTL: -time.Second}},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if _, err := NewCommerceService(test.billing, test.catalog, test.credits, test.options); err == nil {
				t.Fatal("invalid commerce construction succeeded")
			}
		})
	}
}

func TestCommerceNilAndLifecycleBoundaries(t *testing.T) {
	var service *CommerceService
	if _, err := service.ProviderForAccount(context.Background(), "account"); err == nil {
		t.Fatal("nil commerce service selected a provider")
	}
	if service.Providers() != nil {
		t.Fatal("nil commerce service exposed providers")
	}
	service.ClearProviderCache()
	service.Close()
	if _, err := service.CreateCheckout(context.Background(), validCommerceCheckoutInput()); err == nil {
		t.Fatal("nil commerce service created a checkout")
	}
	if _, err := service.GetCheckoutIntent(context.Background(), "intent", "subject"); err == nil {
		t.Fatal("nil commerce service returned a checkout")
	}
	if _, err := service.HandleWebhook(context.Background(), "stripe", WebhookRequest{}); err == nil {
		t.Fatal("nil commerce service handled a webhook")
	}

	closed := false
	service = &CommerceService{postDeductionUnsubscribe: func() { closed = true }}
	service.Close()
	if !closed || service.postDeductionUnsubscribe != nil {
		t.Fatal("commerce registrations were not released")
	}
}

func TestProviderForAccountFailsClosedAtAuthorityBoundaries(t *testing.T) {
	newService := func(store *checkoutStoreStub) *CommerceService {
		return newCheckoutService(t, store, map[string]*checkoutProviderStub{
			"stripe": {name: "stripe"},
			"dodo":   {name: "dodo"},
		}, 0)
	}
	for name, run := range map[string]func() error{
		"empty account": func() error {
			_, err := newService(&checkoutStoreStub{config: checkoutTestConfig(t)}).ProviderForAccount(context.Background(), " ")
			return err
		},
		"multiple offers": func() error {
			_, err := newService(&checkoutStoreStub{config: checkoutTestConfig(t)}).ProviderForAccount(context.Background(), "account", "one", "two")
			return err
		},
		"subscription lookup": func() error {
			_, err := newService(&checkoutStoreStub{config: checkoutTestConfig(t), subscriptionErr: errors.New("subscription lookup failed")}).ProviderForAccount(context.Background(), "account")
			return err
		},
		"customer lookup": func() error {
			_, err := newService(&checkoutStoreStub{config: checkoutTestConfig(t), customerErr: errors.New("customer lookup failed")}).ProviderForAccount(context.Background(), "account")
			return err
		},
		"catalog lookup": func() error {
			_, err := newService(&checkoutStoreStub{config: checkoutTestConfig(t), catalogErr: errors.New("catalog failed")}).ProviderForAccount(context.Background(), "account", "starter_month")
			return err
		},
		"unknown offer": func() error {
			_, err := newService(&checkoutStoreStub{config: checkoutTestConfig(t)}).ProviderForAccount(context.Background(), "account", "missing")
			return err
		},
	} {
		t.Run(name, func(t *testing.T) {
			if err := run(); err == nil {
				t.Fatal("provider authority failure was ignored")
			}
		})
	}

	store := &checkoutStoreStub{
		config: checkoutTestConfig(t),
		customers: map[string]*BillingCustomerRecord{
			"stripe": {Provider: "stripe", ProviderCustomerID: "cus_1"},
		},
	}
	provider, err := newService(store).ProviderForAccount(context.Background(), "account")
	if err != nil || provider.Name() != "stripe" {
		t.Fatalf("customer-owned provider = %#v, %v", provider, err)
	}
}

func TestCreateCheckoutRejectsInvalidRequestBeforeSideEffects(t *testing.T) {
	store := &checkoutStoreStub{config: checkoutTestConfig(t)}
	service := newCheckoutService(t, store, map[string]*checkoutProviderStub{"stripe": {name: "stripe"}}, 0)
	tests := map[string]func(*CreateCheckoutInput){
		"subject":     func(input *CreateCheckoutInput) { input.SubjectID = "" },
		"account":     func(input *CreateCheckoutInput) { input.AccountID = "" },
		"offer":       func(input *CreateCheckoutInput) { input.OfferKey = "" },
		"idempotency": func(input *CreateCheckoutInput) { input.IdempotencyKey = "" },
		"success url": func(input *CreateCheckoutInput) { input.SuccessURL = "" },
		"cancel url":  func(input *CreateCheckoutInput) { input.CancelURL = "" },
		"type":        func(input *CreateCheckoutInput) { input.Type = "donation" },
	}
	for name, mutate := range tests {
		t.Run(name, func(t *testing.T) {
			input := validCommerceCheckoutInput()
			mutate(&input)
			if _, err := service.CreateCheckout(context.Background(), input); err == nil {
				t.Fatal("invalid checkout request accepted")
			}
		})
	}
	if store.created.IdempotencyKey != "" {
		t.Fatal("invalid checkout request reached durable intent creation")
	}
}

func TestCreateCheckoutPropagatesCatalogAndStateFailures(t *testing.T) {
	input := validCommerceCheckoutInput()
	for name, store := range map[string]*checkoutStoreStub{
		"catalog": {
			config: checkoutTestConfig(t), catalogErr: errors.New("catalog failed"),
		},
		"subscription": {
			config: checkoutTestConfig(t), subscriptionErr: errors.New("subscription failed"),
		},
		"customer": {
			config: checkoutTestConfig(t), customerErr: errors.New("customer failed"),
		},
	} {
		t.Run(name, func(t *testing.T) {
			service := newCheckoutService(t, store, map[string]*checkoutProviderStub{"stripe": {name: "stripe"}}, 0)
			if _, err := service.CreateCheckout(context.Background(), input); err == nil {
				t.Fatal("checkout dependency failure was ignored")
			}
		})
	}

	store := &checkoutStoreStub{config: checkoutTestConfig(t)}
	service := newCheckoutService(t, store, map[string]*checkoutProviderStub{"stripe": {name: "stripe"}}, 0)
	service.state = nil
	if _, err := service.CreateCheckout(context.Background(), input); err == nil {
		t.Fatal("subscription checkout without durable state was accepted")
	}
}

func TestCreateCheckoutHandlesDurableIntentAndProviderFailures(t *testing.T) {
	input := validCommerceCheckoutInput()
	future := time.Now().UTC().Add(time.Hour)
	past := time.Now().UTC().Add(-time.Hour)
	tests := []struct {
		name     string
		store    *checkoutStoreStub
		provider *checkoutProviderStub
	}{
		{"create intent", &checkoutStoreStub{config: checkoutTestConfig(t), createErr: errors.New("create failed")}, &checkoutProviderStub{name: "stripe"}},
		{"digest conflict", &checkoutStoreStub{config: checkoutTestConfig(t), intent: CheckoutIntent{ID: "intent", Status: "open", RequestDigest: "different", ExpiresAt: future}}, &checkoutProviderStub{name: "stripe"}},
		{"completed", &checkoutStoreStub{config: checkoutTestConfig(t), matchDigest: true, intent: CheckoutIntent{ID: "intent", Status: "completed", ExpiresAt: future}}, &checkoutProviderStub{name: "stripe"}},
		{"terminal", &checkoutStoreStub{config: checkoutTestConfig(t), matchDigest: true, intent: CheckoutIntent{ID: "intent", Status: "failed", ExpiresAt: future}}, &checkoutProviderStub{name: "stripe"}},
		{"expired update", &checkoutStoreStub{config: checkoutTestConfig(t), matchDigest: true, updateErr: errors.New("update failed"), intent: CheckoutIntent{ID: "intent", Status: "open", ExpiresAt: past}}, &checkoutProviderStub{name: "stripe"}},
		{"expired", &checkoutStoreStub{config: checkoutTestConfig(t), matchDigest: true, intent: CheckoutIntent{ID: "intent", Status: "open", ExpiresAt: past}}, &checkoutProviderStub{name: "stripe"}},
		{"provider", &checkoutStoreStub{config: checkoutTestConfig(t)}, &checkoutProviderStub{name: "stripe", err: errors.New("provider failed")}},
		{"publish intent", &checkoutStoreStub{config: checkoutTestConfig(t), updateErr: errors.New("update failed")}, &checkoutProviderStub{name: "stripe"}},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			service := newCheckoutService(t, test.store, map[string]*checkoutProviderStub{"stripe": test.provider}, 0)
			if _, err := service.CreateCheckout(context.Background(), input); err == nil {
				t.Fatal("checkout failure was ignored")
			}
		})
	}

	store := &checkoutStoreStub{
		config: checkoutTestConfig(t), matchDigest: true,
		intent: CheckoutIntent{
			ID: "intent", Provider: "stripe", Status: "open", ExpiresAt: future,
			ProviderSessionID: "session", ProviderURL: "https://checkout.test/session",
		},
	}
	result, err := newCheckoutService(t, store, map[string]*checkoutProviderStub{"stripe": {name: "stripe"}}, 0).CreateCheckout(context.Background(), input)
	if err != nil || result.Session.ID != "session" || result.Session.URL == "" {
		t.Fatalf("replayed open checkout = %#v, %v", result, err)
	}
}

func TestCreateCheckoutRejectsProviderIdentityOutsideOffer(t *testing.T) {
	store := &checkoutStoreStub{config: checkoutTestConfig(t)}
	provider := &checkoutProviderStub{name: "renamed-provider"}
	service := newCheckoutService(t, store, map[string]*checkoutProviderStub{"stripe": provider}, 0)
	if _, err := service.CreateCheckout(context.Background(), validCommerceCheckoutInput()); err == nil {
		t.Fatal("provider identity without an offer reference was accepted")
	}
}
