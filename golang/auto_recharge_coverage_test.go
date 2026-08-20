package bursar

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"
)

func TestAutoRechargeCoverageUsesExactCatalogChargeAndWindow(t *testing.T) {
	store := &autoRechargeStoreStub{
		topup:    &AutoRechargeTopup{ID: "topup-1"},
		customer: &AutoRechargeCustomer{UserID: "user-1", Provider: "stripe", ProviderCustomerID: "cus-1"},
		profile:  autoRechargeActiveProfile(),
	}
	provider := &autoRechargeCoverageProvider{
		autoRechargeProviderBase: autoRechargeProviderBase{name: "stripe"},
		methods:                  []PaymentMethodInfo{{ID: "pm-1", Last4: "4242", Brand: "visa", ExpiryMonth: 1, ExpiryYear: 2030, IsDefault: true}},
		charge:                   SavedPaymentChargeResult{ProviderPaymentID: "pi-1", Status: SavedPaymentChargeProcessing, AmountMinor: int64Pointer(2500), Currency: "USD"},
	}
	service := autoRechargeTestService(t, store, "coverage-key")

	policy, err := service.ResolvePolicy(context.Background(), provider)
	if err != nil {
		t.Fatalf("ResolvePolicy() error = %v", err)
	}
	wantNow := time.Date(2026, time.August, 10, 12, 0, 0, 0, time.UTC)
	if !policy.Window.Start.Equal(wantNow.Add(-24*time.Hour)) || !policy.Window.End.Equal(wantNow) || policy.Window.DurationDays != 1 {
		t.Fatalf("policy window = %#v, want one exact UTC day ending at %s", policy.Window, wantNow)
	}

	result, err := service.ProcessIfNeeded(context.Background(), provider, AutoRechargeProcessInput{
		UserID: "user-1", Balance: MustAmount("4.999"), ReturnURL: " https://app.example.test/return ",
	})
	if err != nil || result.Outcome != AutoRechargeOutcomeSubmitted {
		t.Fatalf("ProcessIfNeeded() = %#v, %v", result, err)
	}
	if len(provider.charges) != 1 {
		t.Fatalf("charges = %#v, want one", provider.charges)
	}
	charge := provider.charges[0]
	if charge.ProductID != "price-credits" || charge.Quantity != 2 || charge.PaymentMethodID != "pm-1" || charge.CustomerID != "cus-1" || charge.ReturnURL != "https://app.example.test/return" || charge.IdempotencyKey != "coverage-key" {
		t.Fatalf("saved-payment params = %#v", charge)
	}
	if charge.Metadata["purpose"] != "credit_topup" || charge.Metadata["auto_recharge_attempt_id"] != "attempt-1" || charge.Metadata["bursar_account_id"] != "user-1" {
		t.Fatalf("saved-payment metadata = %#v", charge.Metadata)
	}
}

func TestAutoRechargeCoverageFailsClosedOnDependencies(t *testing.T) {
	ctx := context.Background()
	provider := &autoRechargeCoverageProvider{
		autoRechargeProviderBase: autoRechargeProviderBase{name: "stripe"},
		methods:                  []PaymentMethodInfo{{ID: "pm-1", Last4: "4242", Brand: "visa", ExpiryMonth: 1, ExpiryYear: 2030, IsDefault: true}},
	}

	service, err := NewAutoRechargeService(&autoRechargeCatalogStub{err: errors.New("catalog unavailable")}, &autoRechargeStoreStub{}, AutoRechargeServiceOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := service.ResolvePolicy(ctx, provider); err == nil || !strings.Contains(err.Error(), "catalog unavailable") {
		t.Fatalf("catalog error = %v", err)
	}

	store := &autoRechargeCoverageStore{autoRechargeStoreStub: &autoRechargeStoreStub{topup: &AutoRechargeTopup{ID: "topup-1"}, profile: autoRechargeActiveProfile()}, topupErr: errors.New("topup unavailable")}
	service = autoRechargeTestService(t, store.autoRechargeStoreStub, "key")
	// The wrapper is used below to exercise a durable lookup error without
	// allowing a stale in-memory policy to proceed to a provider call.
	service.store = store
	if _, err := service.ResolvePolicy(ctx, provider); err == nil || !strings.Contains(err.Error(), "topup unavailable") {
		t.Fatalf("top-up lookup error = %v", err)
	}

	store = &autoRechargeCoverageStore{autoRechargeStoreStub: &autoRechargeStoreStub{
		topup: &AutoRechargeTopup{ID: "topup-1"}, customer: &AutoRechargeCustomer{UserID: "user-1", Provider: "stripe", ProviderCustomerID: "cus-1"}, profile: autoRechargeActiveProfile(),
	}, customerErr: errors.New("customer unavailable")}
	service = autoRechargeTestService(t, store.autoRechargeStoreStub, "key")
	service.store = store
	if _, err := service.ProcessIfNeeded(ctx, provider, AutoRechargeProcessInput{UserID: "user-1", Balance: MustAmount("0")}); err == nil || !strings.Contains(err.Error(), "customer unavailable") {
		t.Fatalf("customer lookup error = %v", err)
	}

	provider.methodsErr = errors.New("payment methods unavailable")
	store = &autoRechargeCoverageStore{autoRechargeStoreStub: &autoRechargeStoreStub{
		topup: &AutoRechargeTopup{ID: "topup-1"}, customer: &AutoRechargeCustomer{UserID: "user-1", Provider: "stripe", ProviderCustomerID: "cus-1"}, profile: autoRechargeActiveProfile(),
	}}
	service = autoRechargeTestService(t, store.autoRechargeStoreStub, "key")
	service.store = store
	if _, err := service.ProcessIfNeeded(ctx, provider, AutoRechargeProcessInput{UserID: "user-1", Balance: MustAmount("0")}); err == nil || !strings.Contains(err.Error(), "payment methods unavailable") {
		t.Fatalf("payment-method error = %v", err)
	}

	service = autoRechargeTestService(t, &autoRechargeStoreStub{topup: &AutoRechargeTopup{ID: "topup-1"}, customer: &AutoRechargeCustomer{UserID: "user-1", Provider: "stripe", ProviderCustomerID: "cus-1"}, profile: autoRechargeActiveProfile()}, "key")
	service.newIdempotencyKey = func(context.Context, string) (string, error) { return " ", nil }
	if _, err := service.ProcessIfNeeded(ctx, provider, AutoRechargeProcessInput{UserID: "user-1", Balance: MustAmount("0")}); err == nil {
		t.Fatal("blank idempotency key accepted")
	}
}

func TestAutoRechargeCoveragePostDeductionHook(t *testing.T) {
	store := &autoRechargeStoreStub{
		topup:    &AutoRechargeTopup{ID: "topup-1"},
		customer: &AutoRechargeCustomer{UserID: "user-1", Provider: "stripe", ProviderCustomerID: "cus-1"},
		profile:  autoRechargeActiveProfile(),
	}
	provider := &autoRechargeCoverageProvider{
		autoRechargeProviderBase: autoRechargeProviderBase{name: "stripe"},
		methods:                  []PaymentMethodInfo{{ID: "pm-1", Last4: "4242", Brand: "visa", ExpiryMonth: 1, ExpiryYear: 2030, IsDefault: true}},
		charge:                   SavedPaymentChargeResult{ProviderPaymentID: "pi-1", Status: SavedPaymentChargeSucceeded},
	}
	service := autoRechargeTestService(t, store, "hook-key")

	if err := service.PostDeductionHook(nil, "")(context.Background(), PostDeductionContext{UserID: "user-1", Deduction: DeductionResult{BalanceAfter: int64Amount("0")}}); err == nil {
		t.Fatal("nil provider resolver accepted")
	}
	resolver := &autoRechargeResolverCoverage{provider: provider}
	hook := service.PostDeductionHook(resolver, "https://return")
	if err := hook(context.Background(), PostDeductionContext{UserID: "user-1", Deduction: DeductionResult{BalanceAfter: int64Amount("0")}}); err != nil {
		t.Fatalf("post-deduction hook error = %v", err)
	}
	if len(provider.charges) != 1 || provider.charges[0].ReturnURL != "https://return" {
		t.Fatalf("post-deduction charges = %#v", provider.charges)
	}
	resolver.err = errors.New("provider registry unavailable")
	if err := hook(context.Background(), PostDeductionContext{UserID: "user-1", Deduction: DeductionResult{BalanceAfter: int64Amount("0")}}); err == nil || !strings.Contains(err.Error(), "provider registry unavailable") {
		t.Fatalf("resolver error = %v", err)
	}
}

func TestCommerceAutoRechargeCoverageFacadeFlow(t *testing.T) {
	ctx := context.Background()
	store := &autoRechargeStoreStub{
		topup:    &AutoRechargeTopup{ID: "topup-1"},
		customer: &AutoRechargeCustomer{UserID: "user-1", Provider: "stripe", ProviderCustomerID: "cus-1"},
		profile:  autoRechargeActiveProfile(),
	}
	provider := &autoRechargeCoverageProvider{
		autoRechargeProviderBase: autoRechargeProviderBase{name: "stripe"},
		methods:                  []PaymentMethodInfo{{ID: "pm-1", Last4: "4242", Brand: "visa", ExpiryMonth: 1, ExpiryYear: 2030, IsDefault: true}},
		charge:                   SavedPaymentChargeResult{ProviderPaymentID: "pi-1", Status: SavedPaymentChargeProcessing},
	}
	registry, err := NewProviderRegistry(ProviderFactoryContext{ProviderEnvironment: ProviderEnvironmentTest}, map[string]ProviderFactory{
		"stripe": func(context.Context, ProviderFactoryContext) (PaymentProvider, error) { return provider, nil },
	})
	if err != nil {
		t.Fatal(err)
	}
	credits := &CreditsService{store: &autoRechargeCoverageCreditStore{balance: BalanceResult{Balance: MustAmount("0")}}}
	commerce := &CommerceService{providers: registry, credits: credits}
	facade := &CommerceAutoRecharge{commerce: commerce, service: autoRechargeTestService(t, store, "facade-key")}

	status, err := facade.GetStatus(ctx, "user-1")
	if err != nil || status == nil || !status.Enabled {
		t.Fatalf("GetStatus() = %#v, %v", status, err)
	}
	if _, err := facade.Enable(ctx, AutoRechargeInput{AccountID: "user-1", ReturnURL: "https://return"}); err != nil {
		t.Fatalf("Enable() error = %v", err)
	}
	if _, err := facade.Retry(ctx, AutoRechargeInput{AccountID: "user-1"}); err != nil {
		t.Fatalf("Retry() error = %v", err)
	}
	if result, err := facade.ProcessIfNeeded(ctx, AutoRechargeInput{AccountID: "user-1"}); err != nil || result.Outcome != AutoRechargeOutcomeSubmitted {
		t.Fatalf("ProcessIfNeeded() = %#v, %v", result, err)
	}
	if err := facade.Disable(ctx, "user-1"); err != nil {
		t.Fatalf("Disable() error = %v", err)
	}
	if result, err := facade.ProcessIfNeeded(ctx, AutoRechargeInput{AccountID: "user-1"}); err != nil || result.Outcome != AutoRechargeOutcomeDisabled {
		t.Fatalf("disabled ProcessIfNeeded() = %#v, %v", result, err)
	}

	credits.store = &autoRechargeCoverageCreditStore{balanceErr: errors.New("balance unavailable")}
	if _, err := facade.ProcessIfNeeded(ctx, AutoRechargeInput{AccountID: "user-1"}); err == nil || !strings.Contains(err.Error(), "balance unavailable") {
		t.Fatalf("balance error = %v", err)
	}
}

func int64Amount(value string) *Amount {
	amount := MustAmount(value)
	return &amount
}

type autoRechargeCoverageStore struct {
	*autoRechargeStoreStub
	topupErr    error
	customerErr error
}

func (s *autoRechargeCoverageStore) ResolveAutoRechargeTopup(ctx context.Context, lookup AutoRechargeTopupLookup) (*AutoRechargeTopup, error) {
	if s.topupErr != nil {
		return nil, s.topupErr
	}
	return s.autoRechargeStoreStub.ResolveAutoRechargeTopup(ctx, lookup)
}

func (s *autoRechargeCoverageStore) GetAutoRechargeCustomer(ctx context.Context, userID, provider string) (*AutoRechargeCustomer, error) {
	if s.customerErr != nil {
		return nil, s.customerErr
	}
	return s.autoRechargeStoreStub.GetAutoRechargeCustomer(ctx, userID, provider)
}

type autoRechargeCoverageProvider struct {
	autoRechargeProviderBase
	methods    []PaymentMethodInfo
	methodsErr error
	charge     SavedPaymentChargeResult
	chargeErr  error
	charges    []SavedPaymentChargeParams
}

func (p *autoRechargeCoverageProvider) ListPaymentMethods(context.Context, string) ([]PaymentMethodInfo, error) {
	if p.methodsErr != nil {
		return nil, p.methodsErr
	}
	return append([]PaymentMethodInfo(nil), p.methods...), nil
}

func (p *autoRechargeCoverageProvider) ChargeSavedPaymentMethod(_ context.Context, params SavedPaymentChargeParams) (SavedPaymentChargeResult, error) {
	p.charges = append(p.charges, params)
	return p.charge, p.chargeErr
}

type autoRechargeResolverCoverage struct {
	provider PaymentProvider
	err      error
}

func (r autoRechargeResolverCoverage) Get(context.Context, string) (PaymentProvider, error) {
	return r.provider, r.err
}

type autoRechargeCoverageCreditStore struct {
	CreditStore
	balance    BalanceResult
	balanceErr error
}

func (s *autoRechargeCoverageCreditStore) GetBalance(context.Context, string) (BalanceResult, error) {
	if s.balanceErr != nil {
		return BalanceResult{}, s.balanceErr
	}
	return s.balance, nil
}
