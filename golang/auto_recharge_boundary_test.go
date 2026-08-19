package bursar

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"
)

type autoRechargeBoundaryStore struct {
	*autoRechargeStoreStub
	profileErr error
	upsertErr  error
	claimErr   error
	updateErr  error
	countErr   error
}

func (s *autoRechargeBoundaryStore) GetAutoRechargeProfile(ctx context.Context, userID string) (*AutoRechargeProfile, error) {
	if s.profileErr != nil {
		return nil, s.profileErr
	}
	return s.autoRechargeStoreStub.GetAutoRechargeProfile(ctx, userID)
}

func (s *autoRechargeBoundaryStore) UpsertAutoRechargeProfile(ctx context.Context, profile AutoRechargeProfile, options AutoRechargeProfileUpsertOptions) error {
	if s.upsertErr != nil {
		return s.upsertErr
	}
	return s.autoRechargeStoreStub.UpsertAutoRechargeProfile(ctx, profile, options)
}

func (s *autoRechargeBoundaryStore) ClaimAutoRechargeAttempt(ctx context.Context, claim AutoRechargeAttemptClaim) (*AutoRechargeAttempt, error) {
	if s.claimErr != nil {
		return nil, s.claimErr
	}
	return s.autoRechargeStoreStub.ClaimAutoRechargeAttempt(ctx, claim)
}

func (s *autoRechargeBoundaryStore) UpdateAutoRechargeAttempt(ctx context.Context, update AutoRechargeAttemptUpdate) error {
	if s.updateErr != nil {
		return s.updateErr
	}
	return s.autoRechargeStoreStub.UpdateAutoRechargeAttempt(ctx, update)
}

func (s *autoRechargeBoundaryStore) CountAutoRechargeAttempts(ctx context.Context, userID string, since time.Time) (int, error) {
	if s.countErr != nil {
		return 0, s.countErr
	}
	return s.autoRechargeStoreStub.CountAutoRechargeAttempts(ctx, userID, since)
}

type autoRechargeBoundaryPreviewProvider struct {
	autoRechargeCoverageProvider
	quote    SavedPaymentChargeQuote
	quoteErr error
}

func (p *autoRechargeBoundaryPreviewProvider) PreviewSavedPaymentCharge(context.Context, SavedPaymentChargeParams) (SavedPaymentChargeQuote, error) {
	return p.quote, p.quoteErr
}

func autoRechargeBoundaryProvider() *autoRechargeBoundaryPreviewProvider {
	return &autoRechargeBoundaryPreviewProvider{
		autoRechargeCoverageProvider: autoRechargeCoverageProvider{
			autoRechargeProviderBase: autoRechargeProviderBase{name: "stripe"},
			methods: []PaymentMethodInfo{{
				ID: "pm-1", Last4: "4242", Brand: "visa", ExpiryMonth: 1, ExpiryYear: 2030, IsDefault: true,
			}},
			charge: SavedPaymentChargeResult{ProviderPaymentID: "pi-1", Status: SavedPaymentChargeProcessing},
		},
		quote: SavedPaymentChargeQuote{AmountMinor: 2_500, Currency: "USD"},
	}
}

func newAutoRechargeBoundaryService(t *testing.T, store *autoRechargeBoundaryStore, key string) *AutoRechargeService {
	t.Helper()
	service := autoRechargeTestService(t, store.autoRechargeStoreStub, key)
	service.store = store
	return service
}

func TestAutoRechargeReadSurfacesFailClosed(t *testing.T) {
	ctx := context.Background()
	provider := autoRechargeBoundaryProvider()
	var nilService *AutoRechargeService
	if _, err := nilService.ResolvePolicy(ctx, provider); err == nil {
		t.Fatal("nil auto-recharge service resolved policy")
	}
	if _, err := nilService.GetProfile(ctx, "user-1"); err == nil {
		t.Fatal("nil auto-recharge service returned a profile")
	}

	store := &autoRechargeBoundaryStore{autoRechargeStoreStub: &autoRechargeStoreStub{
		topup: &AutoRechargeTopup{ID: "topup-1"}, customer: &AutoRechargeCustomer{UserID: "user-1", Provider: "stripe", ProviderCustomerID: "cus-1"}, profile: autoRechargeActiveProfile(),
	}}
	service := newAutoRechargeBoundaryService(t, store, "key")
	for name, call := range map[string]func() error{
		"policy provider": func() error { _, err := service.ResolvePolicy(ctx, nil); return err },
		"profile user":    func() error { _, err := service.GetProfile(ctx, " "); return err },
		"quote user":      func() error { _, err := service.Quote(ctx, " ", provider); return err },
		"status user":     func() error { _, err := service.GetStatus(ctx, " ", provider); return err },
	} {
		t.Run(name, func(t *testing.T) {
			if err := call(); err == nil {
				t.Fatal("invalid read request accepted")
			}
		})
	}

	store.profileErr = errors.New("profile unavailable")
	if _, err := service.GetProfile(ctx, "user-1"); err == nil {
		t.Fatal("profile persistence error was ignored")
	}
	store.profileErr = nil
	store.profile = autoRechargeActiveProfile()
	store.profile.UserID = "other-user"
	if _, err := service.GetProfile(ctx, "user-1"); err == nil {
		t.Fatal("cross-account profile was accepted")
	}
	store.profile = autoRechargeActiveProfile()

	methodsOnly := &autoRechargeMethodsOnlyProvider{
		autoRechargeProviderBase: autoRechargeProviderBase{name: "stripe"},
		methods:                  []PaymentMethodInfo{{ID: "pm-1", Last4: "4242", Brand: "visa", ExpiryMonth: 1, ExpiryYear: 2030, IsDefault: true}},
	}
	if quote, err := service.Quote(ctx, "user-1", methodsOnly); err != nil || quote != nil {
		t.Fatalf("provider without preview = %#v, %v", quote, err)
	}
	provider.quoteErr = errors.New("preview unavailable")
	if _, err := service.Quote(ctx, "user-1", provider); err == nil {
		t.Fatal("preview error was ignored")
	}
	provider.quoteErr = nil
	provider.quote = SavedPaymentChargeQuote{AmountMinor: -1, Currency: "USD"}
	if _, err := service.Quote(ctx, "user-1", provider); err == nil {
		t.Fatal("negative saved-payment quote was accepted")
	}
	provider.quote = SavedPaymentChargeQuote{AmountMinor: 2_500, Currency: "USD"}

	store.countErr = errors.New("attempt count unavailable")
	if _, err := service.GetStatus(ctx, "user-1", provider); err == nil {
		t.Fatal("attempt-count error was ignored")
	}
	store.countErr = nil
	store.profile.State = AutoRechargeStatePaused
	status, err := service.GetStatus(ctx, "user-1", provider)
	if err != nil || status.State != AutoRechargeStatePaused || status.SuspendedReason == "" || status.QuoteAmountMinor == nil {
		t.Fatalf("paused status = %#v, %v", status, err)
	}
}

func TestAutoRechargeMutationSurfacesFailClosed(t *testing.T) {
	ctx := context.Background()
	provider := autoRechargeBoundaryProvider()
	baseStore := func() *autoRechargeBoundaryStore {
		return &autoRechargeBoundaryStore{autoRechargeStoreStub: &autoRechargeStoreStub{
			topup: &AutoRechargeTopup{ID: "topup-1"}, customer: &AutoRechargeCustomer{UserID: "user-1", Provider: "stripe", ProviderCustomerID: "cus-1"}, profile: autoRechargeActiveProfile(),
		}}
	}

	service := newAutoRechargeBoundaryService(t, baseStore(), "key")
	for name, call := range map[string]func() error{
		"enable user":  func() error { _, err := service.Enable(ctx, provider, AutoRechargeProcessInput{}); return err },
		"disable user": func() error { return service.Disable(ctx, " ") },
		"retry user":   func() error { _, err := service.Retry(ctx, provider, AutoRechargeProcessInput{}); return err },
		"process user": func() error { _, err := service.ProcessIfNeeded(ctx, provider, AutoRechargeProcessInput{}); return err },
	} {
		t.Run(name, func(t *testing.T) {
			if err := call(); err == nil {
				t.Fatal("invalid mutation request accepted")
			}
		})
	}

	store := baseStore()
	store.customer = nil
	service = newAutoRechargeBoundaryService(t, store, "key")
	if _, err := service.Enable(ctx, provider, AutoRechargeProcessInput{UserID: "user-1"}); err == nil {
		t.Fatal("auto-recharge enabled without a saved payment method")
	}

	store = baseStore()
	store.upsertErr = errors.New("profile write failed")
	service = newAutoRechargeBoundaryService(t, store, "key")
	if _, err := service.Enable(ctx, provider, AutoRechargeProcessInput{UserID: "user-1"}); err == nil {
		t.Fatal("profile write failure was ignored by Enable")
	}
	if err := service.Disable(ctx, "user-1"); err == nil {
		t.Fatal("profile write failure was ignored by Disable")
	}
	if _, err := service.Retry(ctx, provider, AutoRechargeProcessInput{UserID: "user-1"}); err == nil {
		t.Fatal("profile write failure was ignored by Retry")
	}

	store = baseStore()
	store.profile = nil
	service = newAutoRechargeBoundaryService(t, store, "key")
	if err := service.Disable(ctx, "user-1"); err != nil {
		t.Fatalf("idempotent Disable() error = %v", err)
	}
	if _, err := service.Retry(ctx, provider, AutoRechargeProcessInput{UserID: "user-1"}); err == nil {
		t.Fatal("disabled auto-recharge retry succeeded")
	}

	service.catalog = &autoRechargeCatalogStub{config: &BursarConfig{}}
	if result, err := service.ProcessIfNeeded(ctx, provider, AutoRechargeProcessInput{UserID: "user-1"}); err != nil || result.Outcome != AutoRechargeOutcomeNotConfigured {
		t.Fatalf("unconfigured policy = %#v, %v", result, err)
	}
	if _, err := service.Enable(ctx, provider, AutoRechargeProcessInput{UserID: "user-1"}); err == nil {
		t.Fatal("unconfigured policy was enabled")
	}
}

func TestAutoRechargeProcessingPersistsEveryOutcome(t *testing.T) {
	ctx := context.Background()
	baseStore := func() *autoRechargeBoundaryStore {
		return &autoRechargeBoundaryStore{autoRechargeStoreStub: &autoRechargeStoreStub{
			topup: &AutoRechargeTopup{ID: "topup-1"}, customer: &AutoRechargeCustomer{UserID: "user-1", Provider: "stripe", ProviderCustomerID: "cus-1"}, profile: autoRechargeActiveProfile(),
		}}
	}
	run := func(store *autoRechargeBoundaryStore, provider PaymentProvider, key string) (AutoRechargeProcessResult, error) {
		t.Helper()
		return newAutoRechargeBoundaryService(t, store, key).ProcessIfNeeded(ctx, provider, AutoRechargeProcessInput{UserID: "user-1", Balance: DecimalZero})
	}

	store := baseStore()
	store.claimErr = errors.New("claim failed")
	if _, err := run(store, autoRechargeBoundaryProvider(), "key"); err == nil {
		t.Fatal("attempt claim failure was ignored")
	}
	store = baseStore()
	store.claimNil = true
	if result, err := run(store, autoRechargeBoundaryProvider(), "key"); err != nil || result.Outcome != AutoRechargeOutcomeLimitReached {
		t.Fatalf("limit result = %#v, %v", result, err)
	}
	store = baseStore()
	store.claimResult = &AutoRechargeAttempt{UserID: "user-1", Provider: "stripe", IdempotencyKey: "key", State: AutoRechargeAttemptClaimed}
	if _, err := run(store, autoRechargeBoundaryProvider(), "key"); err == nil {
		t.Fatal("malformed attempt was accepted")
	}

	store = baseStore()
	provider := autoRechargeBoundaryProvider()
	provider.chargeErr = errors.New("charge failed")
	store.updateErr = errors.New("attempt update failed")
	if _, err := run(store, provider, "key"); err == nil || !strings.Contains(err.Error(), "attempt update failed") {
		t.Fatalf("joined provider/update error = %v", err)
	}

	for _, status := range []SavedPaymentChargeStatus{
		SavedPaymentChargeRequiresCustomerAction,
		SavedPaymentChargeProcessing,
		SavedPaymentChargeFailed,
	} {
		t.Run(string(status), func(t *testing.T) {
			store := baseStore()
			store.updateErr = errors.New("attempt update failed")
			provider := autoRechargeBoundaryProvider()
			provider.charge = SavedPaymentChargeResult{ProviderPaymentID: "pi-1", Status: status}
			result, err := run(store, provider, "key")
			if err == nil || result.Charge == nil {
				t.Fatalf("status %q update result = %#v, %v", status, result, err)
			}
		})
	}

	store = baseStore()
	provider = autoRechargeBoundaryProvider()
	provider.charge = SavedPaymentChargeResult{}
	if _, err := run(store, provider, "key"); err == nil {
		t.Fatal("provider charge without status was accepted")
	}
	if key, err := defaultAutoRechargeIdempotencyKey(ctx, "user-1"); err != nil || !strings.HasPrefix(key, "auto-recharge:") || len(key) != len("auto-recharge:")+48 {
		t.Fatalf("default idempotency key = %q, %v", key, err)
	}
}

func TestAutoRechargeValidationAndWindowBoundaries(t *testing.T) {
	for name, attempt := range map[string]*AutoRechargeAttempt{
		"nil":      nil,
		"key":      {ID: "attempt", UserID: "user", Provider: "stripe", State: AutoRechargeAttemptClaimed},
		"user":     {ID: "attempt", IdempotencyKey: "key", UserID: "other", Provider: "stripe", State: AutoRechargeAttemptClaimed},
		"provider": {ID: "attempt", IdempotencyKey: "key", UserID: "user", Provider: "dodo", State: AutoRechargeAttemptClaimed},
		"state":    {ID: "attempt", IdempotencyKey: "key", UserID: "user", Provider: "stripe", State: "invalid"},
	} {
		t.Run("attempt/"+name, func(t *testing.T) {
			if err := validateAutoRechargeAttempt(attempt, "stripe", "user"); err == nil {
				t.Fatal("invalid attempt accepted")
			}
		})
	}
	for name, profile := range map[string]*AutoRechargeProfile{
		"wrong user":     {UserID: "other"},
		"disabled state": {UserID: "user", State: AutoRechargeStatePaused},
		"enabled state":  {UserID: "user", Enabled: true, State: AutoRechargeStateDisabled},
		"malformed":      {UserID: "user", Enabled: true, State: AutoRechargeStateActive},
		"anchor":         {UserID: "user", Enabled: true, State: AutoRechargeStateActive, Provider: "stripe", TopupID: "topup", Quantity: 1, MaxChargesPerWindow: 1, WindowCount: 1, WindowAnchor: "invalid", WindowUnit: "day", WindowTimezone: "UTC"},
		"window":         {UserID: "user", Enabled: true, State: AutoRechargeStateActive, Provider: "stripe", TopupID: "topup", Quantity: 1, MaxChargesPerWindow: 1, WindowCount: 1, WindowAnchor: "rolling"},
	} {
		t.Run("profile/"+name, func(t *testing.T) {
			if err := validateAutoRechargeProfile(profile, "user"); err == nil {
				t.Fatal("invalid profile accepted")
			}
		})
	}
	for _, window := range []Window{
		{Type: "rolling", Duration: &Duration{Unit: "day", Count: 0}},
		{Type: "rolling", Duration: &Duration{Unit: "month", Count: 1}},
		{Type: "calendar", Unit: "day", Count: 0},
		{Type: "calendar", Unit: "day", Count: 1, Timezone: "not/a-zone"},
		{Type: "calendar", Unit: "quarter", Count: 1},
		{Type: "unknown"},
	} {
		if _, err := resolveAutoRechargeWindow(window, time.Now().UTC()); err == nil {
			t.Errorf("invalid window accepted: %#v", window)
		}
	}
	if isPaymentMethodLast4("12a4") || isCurrencyCode("UsD") {
		t.Fatal("malformed payment display fields accepted")
	}
	if got := autoRechargeDiagnosticSummary(NewError("failed", ErrorOptions{Code: ErrorCodeProviderResponseInvalid})); !strings.Contains(got, string(ErrorCodeProviderResponseInvalid)) {
		t.Fatalf("typed diagnostic = %q", got)
	}
}

func TestCommerceAutoRechargeFailsClosedWithoutProviderAuthority(t *testing.T) {
	ctx := context.Background()
	store := &autoRechargeBoundaryStore{autoRechargeStoreStub: &autoRechargeStoreStub{
		topup: &AutoRechargeTopup{ID: "topup-1"}, profile: autoRechargeActiveProfile(),
	}}
	service := newAutoRechargeBoundaryService(t, store, "key")
	facade := &CommerceAutoRecharge{commerce: &CommerceService{}, service: service}
	for name, call := range map[string]func() error{
		"status": func() error { _, err := facade.GetStatus(ctx, "user-1"); return err },
		"enable": func() error { _, err := facade.Enable(ctx, AutoRechargeInput{AccountID: "user-1"}); return err },
		"retry":  func() error { _, err := facade.Retry(ctx, AutoRechargeInput{AccountID: "user-1"}); return err },
		"process": func() error {
			_, err := facade.ProcessIfNeeded(ctx, AutoRechargeInput{AccountID: "user-1"})
			return err
		},
	} {
		t.Run(name, func(t *testing.T) {
			if err := call(); err == nil {
				t.Fatal("missing provider authority was accepted")
			}
		})
	}

	var nilFacade *CommerceAutoRecharge
	if err := nilFacade.Disable(ctx, "user-1"); err == nil {
		t.Fatal("nil auto-recharge facade disabled an account")
	}
	if _, err := nilFacade.Enable(ctx, AutoRechargeInput{AccountID: "user-1"}); err == nil {
		t.Fatal("nil auto-recharge facade enabled an account")
	}
	if _, err := nilFacade.Retry(ctx, AutoRechargeInput{AccountID: "user-1"}); err == nil {
		t.Fatal("nil auto-recharge facade retried an account")
	}
	if _, err := nilFacade.ProcessIfNeeded(ctx, AutoRechargeInput{AccountID: "user-1"}); err == nil {
		t.Fatal("nil auto-recharge facade processed an account")
	}
	for name, call := range map[string]func() error{
		"enable account": func() error {
			_, err := (&CommerceAutoRecharge{commerce: &CommerceService{}, service: service}).Enable(ctx, AutoRechargeInput{})
			return err
		},
		"retry account": func() error {
			_, err := (&CommerceAutoRecharge{commerce: &CommerceService{}, service: service}).Retry(ctx, AutoRechargeInput{})
			return err
		},
		"process account": func() error {
			_, err := (&CommerceAutoRecharge{commerce: &CommerceService{}, service: service}).ProcessIfNeeded(ctx, AutoRechargeInput{})
			return err
		},
	} {
		t.Run(name, func(t *testing.T) {
			if err := call(); err == nil {
				t.Fatal("empty account was accepted")
			}
		})
	}

	registry := commerceBoundaryRegistry(t, map[string]*checkoutProviderStub{"stripe": {name: "stripe"}})
	facade = &CommerceAutoRecharge{
		commerce: &CommerceService{
			providers: registry,
			credits:   &CreditsService{store: &autoRechargeCoverageCreditStore{balanceErr: errors.New("balance failed")}},
		},
		service: service,
	}
	if _, err := facade.Enable(ctx, AutoRechargeInput{AccountID: "user-1"}); err == nil {
		t.Fatal("balance lookup failure was ignored by Enable")
	}
}

func TestCommerceAutoRechargeProviderResolutionPrecedenceAndErrors(t *testing.T) {
	ctx := context.Background()
	registry := commerceBoundaryRegistry(t, map[string]*checkoutProviderStub{
		"stripe": {name: "stripe"},
	})
	baseStore := &autoRechargeStoreStub{topup: &AutoRechargeTopup{ID: "topup-1"}}
	service := autoRechargeTestService(t, baseStore, "key")

	profileStore := &autoRechargeBoundaryStore{autoRechargeStoreStub: baseStore, profileErr: errors.New("profile failed")}
	service.store = profileStore
	commerce := &CommerceService{providers: registry}
	if _, err := commerce.autoRechargeProvider(ctx, "user-1", service); err == nil {
		t.Fatal("profile lookup failure was ignored")
	}
	profileStore.profileErr = nil

	state := &checkoutStoreStub{subscriptionErr: errors.New("subscription failed")}
	commerce = &CommerceService{providers: registry, state: state}
	if _, err := commerce.autoRechargeProvider(ctx, "user-1", service); err == nil {
		t.Fatal("subscription lookup failure was ignored")
	}
	state.subscriptionErr = nil
	state.subscription = &CommerceSubscription{Provider: "stripe", Status: "active"}
	if selected, err := commerce.autoRechargeProvider(ctx, "user-1", service); err != nil || selected.Name() != "stripe" {
		t.Fatalf("subscription provider = %#v, %v", selected, err)
	}

	state.subscription = nil
	state.customerErr = errors.New("customer failed")
	if _, err := commerce.autoRechargeProvider(ctx, "user-1", service); err == nil {
		t.Fatal("customer lookup failure was ignored")
	}
	state.customerErr = nil
	state.customers = map[string]*BillingCustomerRecord{"stripe": {Provider: "stripe", ProviderCustomerID: "cus-1"}}
	if selected, err := commerce.autoRechargeProvider(ctx, "user-1", service); err != nil || selected.Name() != "stripe" {
		t.Fatalf("customer provider = %#v, %v", selected, err)
	}

	state.customers = nil
	catalogStore := &checkoutStoreStub{catalogErr: errors.New("catalog failed")}
	commerce = &CommerceService{providers: registry, catalog: &CatalogService{store: catalogStore}}
	if _, err := commerce.autoRechargeProvider(ctx, "user-1", service); err == nil {
		t.Fatal("catalog lookup failure was ignored")
	}

	config := checkoutTestConfig(t)
	delete(config["commerce"].(map[string]any), "auto_recharge")
	catalogStore = &checkoutStoreStub{config: config}
	commerce = &CommerceService{providers: registry, catalog: &CatalogService{store: catalogStore}}
	if _, err := commerce.autoRechargeProvider(ctx, "user-1", service); err == nil {
		t.Fatal("catalog without auto-recharge policy was accepted")
	}
}
