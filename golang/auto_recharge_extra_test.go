package bursar

import (
	"context"
	"strings"
	"testing"
	"time"
)

func TestNewAutoRechargeServiceRejectsMissingPorts(t *testing.T) {
	store := &autoRechargeStoreStub{}
	if _, err := NewAutoRechargeService(nil, store, AutoRechargeServiceOptions{}); err == nil {
		t.Fatal("nil catalog was accepted")
	}
	if _, err := NewAutoRechargeService(&autoRechargeCatalogStub{}, nil, AutoRechargeServiceOptions{}); err == nil {
		t.Fatal("nil store was accepted")
	}
}

func TestAutoRechargeResolvePolicyConfigurationMatrix(t *testing.T) {
	base := autoRechargeTestConfig()
	cases := []struct {
		name    string
		mutate  func(*BursarConfig, *autoRechargeStoreStub)
		wantNil bool
		wantErr string
	}{
		{"nil config", func(_ *BursarConfig, _ *autoRechargeStoreStub) {}, true, "catalog source returned nil"},
		{"no guardrails", func(c *BursarConfig, _ *autoRechargeStoreStub) { c.Commerce.AutoRecharge = nil }, true, ""},
		{"empty eligible", func(c *BursarConfig, _ *autoRechargeStoreStub) { c.Commerce.AutoRecharge.EligibleTopups = nil }, false, "eligible top-ups"},
		{"missing offer", func(c *BursarConfig, _ *autoRechargeStoreStub) {
			c.Commerce.AutoRecharge.EligibleTopups = []string{"missing"}
		}, false, "missing"},
		{"no provider reference", func(c *BursarConfig, _ *autoRechargeStoreStub) {
			delete(c.Commerce.Offers["credits_100"].Providers, "stripe")
		}, true, ""},
		{"missing product", func(c *BursarConfig, _ *autoRechargeStoreStub) {
			ref := c.Commerce.Offers["credits_100"].Providers["stripe"]
			ref.PriceID = nil
			c.Commerce.Offers["credits_100"].Providers["stripe"] = ref
		}, false, "product identifier"},
		{"invalid quantity", func(c *BursarConfig, _ *autoRechargeStoreStub) { c.Commerce.AutoRecharge.Quantity.Default = 0 }, false, "quantity"},
		{"missing topup", func(_ *BursarConfig, s *autoRechargeStoreStub) { s.topup = nil }, true, ""},
		{"missing topup id", func(_ *BursarConfig, s *autoRechargeStoreStub) { s.topup = &AutoRechargeTopup{} }, false, "top-up ID"},
		{"invalid window", func(c *BursarConfig, _ *autoRechargeStoreStub) {
			c.Commerce.AutoRecharge.Limits.Window = Window{Type: "other"}
		}, false, "rolling or calendar"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			config := cloneBursarConfigForTest(base)
			store := &autoRechargeStoreStub{topup: &AutoRechargeTopup{ID: "topup-1"}}
			if tc.name == "nil config" {
				config = nil
			}
			tc.mutate(config, store)
			service, err := NewAutoRechargeService(&autoRechargeCatalogStub{config: config}, store, AutoRechargeServiceOptions{Now: func() time.Time { return time.Now() }})
			if err != nil {
				t.Fatal(err)
			}
			policy, err := service.ResolvePolicy(context.Background(), &autoRechargeProviderBase{name: "stripe"})
			if tc.wantErr == "" && !tc.wantNil && err != nil {
				t.Fatalf("ResolvePolicy() error = %v", err)
			}
			if tc.wantNil && policy != nil {
				t.Fatalf("policy = %#v, want nil", policy)
			}
			if tc.wantErr != "" && (err == nil || !strings.Contains(err.Error(), tc.wantErr)) {
				t.Fatalf("error = %v, want %q", err, tc.wantErr)
			}
		})
	}
}

func TestAutoRechargeDecisionAndProviderResponseValidation(t *testing.T) {
	validAttempt := &AutoRechargeAttempt{ID: "a", UserID: "u", Provider: "stripe", IdempotencyKey: "key", State: AutoRechargeAttemptClaimed}
	for _, state := range []AutoRechargeAttemptState{AutoRechargeAttemptClaimed, AutoRechargeAttemptSubmitted, AutoRechargeAttemptProcessing, AutoRechargeAttemptUnknown, AutoRechargeAttemptSucceeded, AutoRechargeAttemptFailed, AutoRechargeAttemptActionRequired} {
		attempt := *validAttempt
		attempt.State = state
		if err := validateAutoRechargeAttempt(&attempt, "stripe", "u"); err != nil {
			t.Errorf("state %q rejected: %v", state, err)
		}
	}
	invalid := []AutoRechargeAttempt{
		{ID: "", UserID: "u", Provider: "stripe", IdempotencyKey: "key", State: AutoRechargeAttemptClaimed},
		{ID: "a", UserID: "other", Provider: "stripe", IdempotencyKey: "key", State: AutoRechargeAttemptClaimed},
		{ID: "a", UserID: "u", Provider: "other", IdempotencyKey: "key", State: AutoRechargeAttemptClaimed},
		{ID: "a", UserID: "u", Provider: "stripe", IdempotencyKey: "key", State: "bogus"},
	}
	for _, attempt := range invalid {
		if err := validateAutoRechargeAttempt(&attempt, "stripe", "u"); err == nil {
			t.Errorf("invalid attempt accepted: %#v", attempt)
		}
	}
	for _, profile := range []*AutoRechargeProfile{
		{UserID: "other"},
		{UserID: "u", Enabled: true, State: AutoRechargeStateActive, Provider: "stripe", TopupID: "topup", Quantity: 0, MaxChargesPerWindow: 1, WindowCount: 1, WindowAnchor: "rolling", WindowUnit: "day", WindowTimezone: "UTC"},
	} {
		if err := validateAutoRechargeProfile(profile, "u"); err == nil {
			t.Errorf("invalid profile accepted: %#v", profile)
		}
	}
	for _, method := range []PaymentMethodInfo{{}, {ID: "pm", Last4: "12x4", Brand: "visa", ExpiryMonth: 1, ExpiryYear: 2030}, {ID: "pm", Last4: "1234", Brand: "", ExpiryMonth: 1, ExpiryYear: 2030}, {ID: "pm", Last4: "1234", Brand: "visa", ExpiryMonth: 13, ExpiryYear: 2030}} {
		if err := validatePaymentMethod(method, "stripe"); err == nil {
			t.Errorf("invalid method accepted: %#v", method)
		}
	}
	for _, quote := range []SavedPaymentChargeQuote{{AmountMinor: -1, Currency: "USD"}, {AmountMinor: 1, Currency: "usd"}} {
		if err := validateSavedPaymentQuote(quote); err == nil {
			t.Errorf("invalid quote accepted: %#v", quote)
		}
	}
	for _, charge := range []SavedPaymentChargeResult{{Status: "bogus"}, {Status: SavedPaymentChargeSucceeded, AmountMinor: int64Pointer(-1)}, {Status: SavedPaymentChargeSucceeded, Currency: "usd"}} {
		if err := validateSavedPaymentCharge(charge); err == nil {
			t.Errorf("invalid charge accepted: %#v", charge)
		}
	}
	if _, err := resolveAutoRechargeWindow(Window{Type: "rolling", Duration: &Duration{Unit: "month", Count: 1}}, time.Now()); err == nil {
		t.Fatal("unsupported rolling unit accepted")
	}
	if _, err := resolveAutoRechargeWindow(Window{Type: "calendar", Unit: "week", Count: 0}, time.Now()); err == nil {
		t.Fatal("zero calendar count accepted")
	}
	if _, _, err := resolveAutoRechargeCalendarWindow(time.Now(), "hour", 1); err == nil {
		t.Fatal("unsupported calendar unit accepted")
	}
	if floorDivide(-1, 2) != -1 {
		t.Fatal("floorDivide did not floor negative quotient")
	}
}

func TestAutoRechargeQuoteDisableRetryAndPostDeduction(t *testing.T) {
	store := &autoRechargeStoreStub{topup: &AutoRechargeTopup{ID: "topup-1"}, customer: &AutoRechargeCustomer{UserID: "user-1", Provider: "stripe", ProviderCustomerID: "cus-1"}, profile: autoRechargeActiveProfile()}
	provider := &autoRechargePreviewProvider{autoRechargeProviderBase: autoRechargeProviderBase{name: "stripe"}, methods: []PaymentMethodInfo{{ID: "pm", Last4: "4242", Brand: "visa", ExpiryMonth: 1, ExpiryYear: 2030, IsDefault: true}}, quote: SavedPaymentChargeQuote{AmountMinor: 100, Currency: "USD"}, charge: SavedPaymentChargeResult{Status: SavedPaymentChargeRequiresCustomerAction, ProviderPaymentID: "pi"}}
	service := autoRechargeTestService(t, store, "retry-key")
	quote, err := service.Quote(context.Background(), "user-1", provider)
	if err != nil || quote == nil || quote.Currency != "USD" {
		t.Fatalf("Quote() = %#v, %v", quote, err)
	}
	if err := service.Disable(context.Background(), "user-1"); err != nil || store.profile.Enabled {
		t.Fatalf("Disable() = %v, profile=%#v", err, store.profile)
	}
	store.profile = nil
	if err := service.Disable(context.Background(), "missing"); err != nil {
		t.Fatalf("idempotent Disable() = %v", err)
	}
	if _, err := service.Retry(context.Background(), provider, AutoRechargeProcessInput{UserID: "user-1", Balance: MustAmount("0")}); err == nil {
		t.Fatal("Retry accepted disabled profile")
	}
	store.profile = autoRechargeActiveProfile()
	result, err := service.ProcessPostDeduction(context.Background(), provider, PostDeductionContext{UserID: "user-1", Deduction: DeductionResult{BalanceAfter: nil}}, "")
	if err != nil || result.Outcome != AutoRechargeOutcomeAboveThreshold {
		t.Fatalf("nil balance post-deduction = %#v, %v", result, err)
	}
}

type autoRechargePreviewProvider struct {
	autoRechargeProviderBase
	methods []PaymentMethodInfo
	quote   SavedPaymentChargeQuote
	charge  SavedPaymentChargeResult
}

func (p *autoRechargePreviewProvider) ListPaymentMethods(context.Context, string) ([]PaymentMethodInfo, error) {
	return p.methods, nil
}
func (p *autoRechargePreviewProvider) PreviewSavedPaymentCharge(context.Context, SavedPaymentChargeParams) (SavedPaymentChargeQuote, error) {
	return p.quote, nil
}
func (p *autoRechargePreviewProvider) ChargeSavedPaymentMethod(context.Context, SavedPaymentChargeParams) (SavedPaymentChargeResult, error) {
	return p.charge, nil
}

func cloneBursarConfigForTest(config *BursarConfig) *BursarConfig {
	if config == nil {
		return nil
	}
	copy := *config
	copy.Commerce.AutoRecharge = cloneAutoRechargeGuardrailsForTest(config.Commerce.AutoRecharge)
	copy.Commerce.Offers = make(map[string]CommerceOffer, len(config.Commerce.Offers))
	for key, offer := range config.Commerce.Offers {
		offerCopy := offer
		offerCopy.Providers = make(map[string]ProviderReference, len(offer.Providers))
		for provider, reference := range offer.Providers {
			offerCopy.Providers[provider] = reference
		}
		copy.Commerce.Offers[key] = offerCopy
	}
	return &copy
}

func cloneAutoRechargeGuardrailsForTest(value *AutoRechargeGuardrails) *AutoRechargeGuardrails {
	if value == nil {
		return nil
	}
	copy := *value
	copy.EligibleTopups = append([]string(nil), value.EligibleTopups...)
	return &copy
}
