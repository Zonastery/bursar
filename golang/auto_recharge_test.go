// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import (
	"context"
	"errors"
	"testing"
	"time"
)

func TestAutoRechargeProcessSubmitsPersistedAttempt(t *testing.T) {
	t.Parallel()
	store := &autoRechargeStoreStub{
		topup:    &AutoRechargeTopup{ID: "topup-1"},
		customer: &AutoRechargeCustomer{UserID: "user-1", Provider: "stripe", ProviderCustomerID: "cus-1"},
		profile:  autoRechargeActiveProfile(),
	}
	provider := &autoRechargeProviderStub{
		autoRechargeProviderBase: autoRechargeProviderBase{name: "stripe"},
		methods:                  []PaymentMethodInfo{{ID: "pm-1", Last4: "4242", Brand: "visa", ExpiryMonth: 1, ExpiryYear: 2030, IsDefault: true}},
		charge:                   SavedPaymentChargeResult{ProviderPaymentID: "pi-1", Status: SavedPaymentChargeProcessing},
	}
	service := autoRechargeTestService(t, store, "auto-recharge-key")

	result, err := service.ProcessIfNeeded(context.Background(), provider, AutoRechargeProcessInput{
		UserID: "user-1", Balance: MustAmount("4.9"), ReturnURL: "https://app.example.test/return",
	})
	if err != nil {
		t.Fatalf("ProcessIfNeeded() error = %v", err)
	}
	if result.Outcome != AutoRechargeOutcomeSubmitted {
		t.Fatalf("outcome = %q, want %q", result.Outcome, AutoRechargeOutcomeSubmitted)
	}
	if store.claim.IdempotencyKey != "auto-recharge-key" {
		t.Fatalf("claim idempotency key = %q", store.claim.IdempotencyKey)
	}
	if len(provider.charges) != 1 {
		t.Fatalf("provider charges = %d, want 1", len(provider.charges))
	}
	charge := provider.charges[0]
	if charge.IdempotencyKey != "auto-recharge-key" || charge.ProductID != "price-credits" {
		t.Fatalf("charge = %#v", charge)
	}
	if charge.Metadata["auto_recharge_attempt_id"] != "attempt-1" || charge.Metadata["bursar_account_id"] != "user-1" || charge.Metadata["purpose"] != "credit_topup" {
		t.Fatalf("charge metadata = %#v", charge.Metadata)
	}
	if len(store.updates) != 1 || store.updates[0].State != AutoRechargeAttemptProcessing || store.updates[0].ProviderAttemptID != "pi-1" {
		t.Fatalf("attempt updates = %#v", store.updates)
	}
	if store.lookup.OfferKey != "credits_100" || store.lookup.Provider != "stripe" {
		t.Fatalf("topup lookup = %#v", store.lookup)
	}
}

func TestAutoRechargeUnknownAttemptReusesPersistedKey(t *testing.T) {
	t.Parallel()
	store := &autoRechargeStoreStub{
		topup:    &AutoRechargeTopup{ID: "topup-1"},
		customer: &AutoRechargeCustomer{UserID: "user-1", Provider: "stripe", ProviderCustomerID: "cus-1"},
		profile:  autoRechargeActiveProfile(),
		claimResult: &AutoRechargeAttempt{
			ID: "attempt-previous", UserID: "user-1", Provider: "stripe", IdempotencyKey: "persisted-provider-key", State: AutoRechargeAttemptUnknown,
		},
	}
	provider := &autoRechargeProviderStub{
		autoRechargeProviderBase: autoRechargeProviderBase{name: "stripe"},
		methods:                  []PaymentMethodInfo{{ID: "pm-1", Last4: "4242", Brand: "visa", ExpiryMonth: 1, ExpiryYear: 2030, IsDefault: true}},
		charge:                   SavedPaymentChargeResult{ProviderPaymentID: "pi-1", Status: SavedPaymentChargeSucceeded},
	}
	service := autoRechargeTestService(t, store, "new-key-that-must-not-replace-persisted-key")

	result, err := service.ProcessIfNeeded(context.Background(), provider, AutoRechargeProcessInput{UserID: "user-1", Balance: MustAmount("0")})
	if err != nil {
		t.Fatalf("ProcessIfNeeded() error = %v", err)
	}
	if result.Outcome != AutoRechargeOutcomeSubmitted {
		t.Fatalf("outcome = %q, want submitted", result.Outcome)
	}
	if len(provider.charges) != 1 || provider.charges[0].IdempotencyKey != "persisted-provider-key" {
		t.Fatalf("charges = %#v", provider.charges)
	}
}

func TestAutoRechargeInFlightAttemptIsWebhookOwned(t *testing.T) {
	t.Parallel()
	store := &autoRechargeStoreStub{
		topup:    &AutoRechargeTopup{ID: "topup-1"},
		customer: &AutoRechargeCustomer{UserID: "user-1", Provider: "stripe", ProviderCustomerID: "cus-1"},
		profile:  autoRechargeActiveProfile(),
		claimResult: &AutoRechargeAttempt{
			ID: "attempt-processing", UserID: "user-1", Provider: "stripe", IdempotencyKey: "existing-key", State: AutoRechargeAttemptProcessing,
		},
	}
	provider := &autoRechargeProviderStub{
		autoRechargeProviderBase: autoRechargeProviderBase{name: "stripe"},
		methods:                  []PaymentMethodInfo{{ID: "pm-1", Last4: "4242", Brand: "visa", ExpiryMonth: 1, ExpiryYear: 2030, IsDefault: true}},
		charge:                   SavedPaymentChargeResult{ProviderPaymentID: "pi-1", Status: SavedPaymentChargeSucceeded},
	}
	service := autoRechargeTestService(t, store, "new-key")

	result, err := service.ProcessIfNeeded(context.Background(), provider, AutoRechargeProcessInput{UserID: "user-1", Balance: MustAmount("0")})
	if err != nil {
		t.Fatalf("ProcessIfNeeded() error = %v", err)
	}
	if result.Outcome != AutoRechargeOutcomeAlreadyProcessing {
		t.Fatalf("outcome = %q, want already_processing", result.Outcome)
	}
	if len(provider.charges) != 0 || len(store.updates) != 0 {
		t.Fatalf("in-flight attempt must not be resubmitted: charges=%d updates=%d", len(provider.charges), len(store.updates))
	}
}

func TestAutoRechargeProviderFailureIsPersistedAsUnknown(t *testing.T) {
	t.Parallel()
	store := &autoRechargeStoreStub{
		topup:    &AutoRechargeTopup{ID: "topup-1"},
		customer: &AutoRechargeCustomer{UserID: "user-1", Provider: "stripe", ProviderCustomerID: "cus-1"},
		profile:  autoRechargeActiveProfile(),
	}
	provider := &autoRechargeProviderStub{
		autoRechargeProviderBase: autoRechargeProviderBase{name: "stripe"},
		methods:                  []PaymentMethodInfo{{ID: "pm-1", Last4: "4242", Brand: "visa", ExpiryMonth: 1, ExpiryYear: 2030, IsDefault: true}},
		chargeErr:                errors.New("secret provider response body"),
	}
	service := autoRechargeTestService(t, store, "attempt-key")

	_, err := service.ProcessIfNeeded(context.Background(), provider, AutoRechargeProcessInput{UserID: "user-1", Balance: MustAmount("0")})
	if err == nil {
		t.Fatal("ProcessIfNeeded() error = nil, want provider error")
	}
	if len(store.updates) != 1 {
		t.Fatalf("attempt updates = %#v", store.updates)
	}
	update := store.updates[0]
	if update.State != AutoRechargeAttemptUnknown || update.FailureCode != "provider_request_failed" {
		t.Fatalf("update = %#v", update)
	}
	if update.FailureMessage != "auto_recharge_provider_failed:provider_error" {
		t.Fatalf("unsafe failure diagnostic = %q", update.FailureMessage)
	}
}

func TestAutoRechargeEnablePersistsCatalogPolicy(t *testing.T) {
	t.Parallel()
	store := &autoRechargeStoreStub{
		topup:    &AutoRechargeTopup{ID: "topup-1"},
		customer: &AutoRechargeCustomer{UserID: "user-1", Provider: "stripe", ProviderCustomerID: "cus-1"},
	}
	provider := &autoRechargeProviderStub{
		autoRechargeProviderBase: autoRechargeProviderBase{name: "stripe"},
		methods:                  []PaymentMethodInfo{{ID: "pm-1", Last4: "4242", Brand: "visa", ExpiryMonth: 1, ExpiryYear: 2030, IsDefault: true}},
	}
	service := autoRechargeTestService(t, store, "attempt-key")

	status, err := service.Enable(context.Background(), provider, AutoRechargeProcessInput{UserID: "user-1", Balance: MustAmount("5")})
	if err != nil {
		t.Fatalf("Enable() error = %v", err)
	}
	if status == nil || !status.Enabled || status.State != AutoRechargeStateActive {
		t.Fatalf("status = %#v", status)
	}
	if store.profile == nil || store.profile.TopupID != "topup-1" || store.profile.Quantity != 2 || !store.profile.Threshold.Equal(MustAmount("5")) {
		t.Fatalf("persisted profile = %#v", store.profile)
	}
	if store.profile.WindowAnchor != "rolling" || store.profile.WindowUnit != "day" || store.profile.WindowCount != 1 {
		t.Fatalf("persisted policy window = %#v", store.profile)
	}
	if len(provider.charges) != 0 || store.claim.IdempotencyKey != "" {
		t.Fatalf("balance at threshold must not claim or charge: claims=%#v charges=%#v", store.claim, provider.charges)
	}
}

func TestAutoRechargeChargeCapabilityError(t *testing.T) {
	t.Parallel()
	store := &autoRechargeStoreStub{
		topup:    &AutoRechargeTopup{ID: "topup-1"},
		customer: &AutoRechargeCustomer{UserID: "user-1", Provider: "stripe", ProviderCustomerID: "cus-1"},
		profile:  autoRechargeActiveProfile(),
	}
	provider := &autoRechargeMethodsOnlyProvider{
		autoRechargeProviderBase: autoRechargeProviderBase{name: "stripe"},
		methods:                  []PaymentMethodInfo{{ID: "pm-1", Last4: "4242", Brand: "visa", ExpiryMonth: 1, ExpiryYear: 2030, IsDefault: true}},
	}
	service := autoRechargeTestService(t, store, "attempt-key")

	_, err := service.ProcessIfNeeded(context.Background(), provider, AutoRechargeProcessInput{UserID: "user-1", Balance: MustAmount("0")})
	if err == nil {
		t.Fatal("ProcessIfNeeded() error = nil, want capability error")
	}
	bursarErr, ok := AsBursarError(err)
	if !ok || bursarErr.Code != ErrorCodeProviderCapabilityNotSupported {
		t.Fatalf("error = %#v, want provider capability error", err)
	}
}

func TestResolveAutoRechargeWindowMatchesCalendarBoundaryContract(t *testing.T) {
	t.Parallel()
	window, err := resolveAutoRechargeWindow(Window{Type: "calendar", Unit: "month", Count: 2, Timezone: "Asia/Kolkata"}, time.Date(2026, time.August, 10, 12, 0, 0, 0, time.UTC))
	if err != nil {
		t.Fatalf("resolveAutoRechargeWindow() error = %v", err)
	}
	// Calendar month periods use the January 2000 anchor, so Aug 2026 belongs
	// to the Jul-Aug two-month period in the catalog's timezone.
	wantStart := time.Date(2026, time.June, 30, 18, 30, 0, 0, time.UTC)
	wantEnd := time.Date(2026, time.August, 31, 18, 30, 0, 0, time.UTC)
	if !window.Start.Equal(wantStart) || !window.End.Equal(wantEnd) || window.Timezone != "Asia/Kolkata" {
		t.Fatalf("window = %#v, want [%s, %s)", window, wantStart, wantEnd)
	}
}

func autoRechargeTestService(t *testing.T, store *autoRechargeStoreStub, key string) *AutoRechargeService {
	t.Helper()
	service, err := NewAutoRechargeService(&autoRechargeCatalogStub{config: autoRechargeTestConfig()}, store, AutoRechargeServiceOptions{
		Now: func() time.Time { return time.Date(2026, time.August, 10, 12, 0, 0, 0, time.UTC) },
		NewIdempotencyKey: func(context.Context, string) (string, error) {
			return key, nil
		},
	})
	if err != nil {
		t.Fatalf("NewAutoRechargeService() error = %v", err)
	}
	return service
}

func autoRechargeTestConfig() *BursarConfig {
	priceID := "price-credits"
	return &BursarConfig{Commerce: CommerceConfig{
		Offers: map[string]CommerceOffer{
			"credits_100": {
				Type: "topup",
				Providers: map[string]ProviderReference{
					"stripe": {Type: "stripe_price", PriceID: &priceID},
				},
			},
		},
		AutoRecharge: &AutoRechargeGuardrails{
			EligibleTopups: []string{"credits_100"},
			BalanceBelow:   DecimalRange{Minimum: MustAmount("0"), Maximum: MustAmount("5"), Default: MustAmount("5")},
			RearmAbove:     MustAmount("6"),
			Quantity:       OfferQuantity{Minimum: 1, Maximum: 5, Default: 2},
			Limits: AutoRechargeLimits{
				MaxPurchases:   3,
				Window:         Window{Type: "rolling", Duration: &Duration{Unit: "day", Count: 1}},
				MaxChargeMinor: 1000,
			},
		},
	}}
}

func autoRechargeActiveProfile() *AutoRechargeProfile {
	return &AutoRechargeProfile{
		UserID: "user-1", Enabled: true, State: AutoRechargeStateActive, Armed: true, Provider: "stripe", TopupID: "topup-1",
		Quantity: 2, Threshold: MustAmount("5"), MaxChargesPerWindow: 3, WindowUnit: "day", WindowCount: 1, WindowAnchor: "rolling", WindowTimezone: "UTC",
	}
}

type autoRechargeCatalogStub struct {
	config *BursarConfig
	err    error
}

func (s *autoRechargeCatalogStub) GetConfig(context.Context) (*BursarConfig, error) {
	return s.config, s.err
}

type autoRechargeStoreStub struct {
	topup       *AutoRechargeTopup
	customer    *AutoRechargeCustomer
	profile     *AutoRechargeProfile
	claimResult *AutoRechargeAttempt
	claimNil    bool
	claim       AutoRechargeAttemptClaim
	updates     []AutoRechargeAttemptUpdate
	lookup      AutoRechargeTopupLookup
	count       int
}

func (s *autoRechargeStoreStub) ResolveAutoRechargeTopup(_ context.Context, lookup AutoRechargeTopupLookup) (*AutoRechargeTopup, error) {
	s.lookup = lookup
	return cloneAutoRechargeTopup(s.topup), nil
}

func (s *autoRechargeStoreStub) GetAutoRechargeCustomer(context.Context, string, string) (*AutoRechargeCustomer, error) {
	if s.customer == nil {
		return nil, nil
	}
	clone := *s.customer
	return &clone, nil
}

func (s *autoRechargeStoreStub) GetAutoRechargeProfile(context.Context, string) (*AutoRechargeProfile, error) {
	return cloneAutoRechargeProfile(s.profile), nil
}

func (s *autoRechargeStoreStub) UpsertAutoRechargeProfile(_ context.Context, profile AutoRechargeProfile, _ AutoRechargeProfileUpsertOptions) error {
	s.profile = cloneAutoRechargeProfile(&profile)
	return nil
}

func (s *autoRechargeStoreStub) ClaimAutoRechargeAttempt(_ context.Context, claim AutoRechargeAttemptClaim) (*AutoRechargeAttempt, error) {
	s.claim = claim
	if s.claimNil {
		return nil, nil
	}
	if s.claimResult != nil {
		return cloneAutoRechargeAttempt(s.claimResult), nil
	}
	return &AutoRechargeAttempt{ID: "attempt-1", UserID: claim.UserID, Provider: "stripe", IdempotencyKey: claim.IdempotencyKey, State: AutoRechargeAttemptClaimed}, nil
}

func (s *autoRechargeStoreStub) UpdateAutoRechargeAttempt(_ context.Context, update AutoRechargeAttemptUpdate) error {
	s.updates = append(s.updates, update)
	return nil
}

func (s *autoRechargeStoreStub) CountAutoRechargeAttempts(context.Context, string, time.Time) (int, error) {
	return s.count, nil
}

func cloneAutoRechargeTopup(value *AutoRechargeTopup) *AutoRechargeTopup {
	if value == nil {
		return nil
	}
	clone := *value
	return &clone
}

func cloneAutoRechargeAttempt(value *AutoRechargeAttempt) *AutoRechargeAttempt {
	if value == nil {
		return nil
	}
	clone := *value
	clone.Metadata = cloneAnyMap(value.Metadata)
	return &clone
}

type autoRechargeProviderBase struct {
	name string
}

func (p *autoRechargeProviderBase) Name() string { return p.name }

func (*autoRechargeProviderBase) CreateCheckoutSession(context.Context, CheckoutSessionRequest) (CheckoutSession, error) {
	return CheckoutSession{}, errors.New("not implemented in auto-recharge test provider")
}

func (*autoRechargeProviderBase) HandleWebhook(context.Context, WebhookRequest) (WebhookResult, error) {
	return WebhookResult{}, errors.New("not implemented in auto-recharge test provider")
}

type autoRechargeProviderStub struct {
	autoRechargeProviderBase
	methods   []PaymentMethodInfo
	charge    SavedPaymentChargeResult
	chargeErr error
	charges   []SavedPaymentChargeParams
}

func (p *autoRechargeProviderStub) ListPaymentMethods(context.Context, string) ([]PaymentMethodInfo, error) {
	return append([]PaymentMethodInfo(nil), p.methods...), nil
}

func (p *autoRechargeProviderStub) ChargeSavedPaymentMethod(_ context.Context, input SavedPaymentChargeParams) (SavedPaymentChargeResult, error) {
	p.charges = append(p.charges, input)
	return p.charge, p.chargeErr
}

type autoRechargeMethodsOnlyProvider struct {
	autoRechargeProviderBase
	methods []PaymentMethodInfo
}

func (p *autoRechargeMethodsOnlyProvider) ListPaymentMethods(context.Context, string) ([]PaymentMethodInfo, error) {
	return append([]PaymentMethodInfo(nil), p.methods...), nil
}
