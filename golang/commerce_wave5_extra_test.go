package bursar

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"
)

func TestCommerceConstructionCacheAndBoundaryFailures(t *testing.T) {
	store := &checkoutStoreStub{config: checkoutTestConfig(t)}
	provider := &checkoutProviderStub{name: "stripe"}
	registry, err := NewProviderRegistry(ProviderFactoryContext{ProviderEnvironment: ProviderEnvironmentTest}, map[string]ProviderFactory{"stripe": func(context.Context, ProviderFactoryContext) (PaymentProvider, error) { return provider, nil }})
	if err != nil {
		t.Fatal(err)
	}
	billing := &BillingService{store: &checkoutBillingStoreStub{}}
	catalog := &CatalogService{store: store}
	credits := &CreditsService{}
	for _, options := range []CommerceOptions{{}, {Store: store}, {Store: store, Providers: registry, DefaultProvider: "missing", CheckoutIntentTTL: -1}} {
		if _, err := NewCommerceService(billing, catalog, credits, options); err == nil {
			t.Errorf("invalid commerce options accepted: %#v", options)
		}
	}
	service, err := NewCommerceService(billing, catalog, credits, CommerceOptions{Store: store, Providers: registry})
	if err != nil {
		t.Fatal(err)
	}
	if got := service.Providers(); len(got) != 1 || got[0] != "stripe" {
		t.Fatalf("providers = %#v", got)
	}
	service.ClearProviderCache()
	service.Close()
	service.Close()
	if _, err := service.GetCheckoutIntent(context.Background(), "", "subject"); err == nil {
		t.Fatal("empty intent ID accepted")
	}
	if _, err := service.GetCheckoutIntent(context.Background(), "intent", ""); err == nil {
		t.Fatal("empty subject accepted")
	}
	if _, err := service.ProviderForAccount(context.Background(), "account", "one", "two"); err == nil {
		t.Fatal("multiple offer keys accepted")
	}
	if _, err := service.GetInvoiceLink(context.Background(), GetInvoiceLinkInput{}); err == nil {
		t.Fatal("empty invoice input accepted")
	}
	if _, err := service.PreviewPlanChange(context.Background(), PreviewPlanChangeInput{}); err == nil {
		t.Fatal("empty plan preview accepted")
	}
	if _, err := service.ConfirmPlanChange(context.Background(), ConfirmPlanChangeInput{}); err == nil {
		t.Fatal("empty plan confirmation accepted")
	}
	if err := service.CancelScheduledPlanChange(context.Background(), "", ""); err == nil {
		t.Fatal("empty scheduled cancellation accepted")
	}
	if _, err := service.UpdatePreferences(context.Background(), "", PreferencePatch{}); err == nil {
		t.Fatal("empty preference account accepted")
	}
	if _, err := service.GetAccountOverview(context.Background(), ""); err == nil {
		t.Fatal("empty overview account accepted")
	}
}

type commerceWebhookFailureProvider struct {
	checkoutProviderStub
	webHookErr error
	nilEvent   bool
}

func (p *commerceWebhookFailureProvider) HandleWebhook(context.Context, WebhookRequest) (WebhookResult, error) {
	if p.webHookErr != nil {
		return WebhookResult{}, p.webHookErr
	}
	if p.nilEvent {
		return WebhookResult{Received: true}, nil
	}
	return p.checkoutProviderStub.HandleWebhook(context.Background(), WebhookRequest{})
}

func TestCommerceWebhookFailuresRemainTypedAndProviderScoped(t *testing.T) {
	store := &checkoutStoreStub{config: checkoutTestConfig(t)}
	provider := &commerceWebhookFailureProvider{checkoutProviderStub: checkoutProviderStub{name: "stripe"}, webHookErr: errors.New("provider transport")}
	registry, err := NewProviderRegistry(ProviderFactoryContext{ProviderEnvironment: ProviderEnvironmentTest}, map[string]ProviderFactory{"stripe": func(context.Context, ProviderFactoryContext) (PaymentProvider, error) { return provider, nil }})
	if err != nil {
		t.Fatal(err)
	}
	billing, err := NewBillingService(&checkoutBillingStoreStub{})
	if err != nil {
		t.Fatal(err)
	}
	service := &CommerceService{store: store, providers: registry, billing: billing}
	if _, err := service.HandleWebhook(context.Background(), "stripe", WebhookRequest{}); !strings.Contains(err.Error(), "provider transport") {
		t.Fatalf("provider error = %v", err)
	}
	provider.webHookErr = nil
	provider.nilEvent = true
	if _, err := service.HandleWebhook(context.Background(), "stripe", WebhookRequest{}); err == nil {
		t.Fatal("nil normalized event accepted")
	}
	if _, err := service.HandleWebhook(context.Background(), "unknown", WebhookRequest{}); err == nil {
		t.Fatal("unknown provider accepted")
	}
}

func TestAutoRechargeProcessDecisionAndRetryBoundaries(t *testing.T) {
	ctx := context.Background()
	provider := &autoRechargeProviderStub{autoRechargeProviderBase: autoRechargeProviderBase{name: "stripe"}, methods: []PaymentMethodInfo{{ID: "pm", Last4: "4242", Brand: "visa", ExpiryMonth: 1, ExpiryYear: 2030, IsDefault: true}}}
	for _, tc := range []struct {
		name    string
		store   *autoRechargeStoreStub
		balance string
		want    AutoRechargeOutcome
	}{
		{"disabled", &autoRechargeStoreStub{topup: &AutoRechargeTopup{ID: "topup"}}, "0", AutoRechargeOutcomeDisabled},
		{"above threshold", &autoRechargeStoreStub{topup: &AutoRechargeTopup{ID: "topup"}, profile: autoRechargeActiveProfile()}, "5", AutoRechargeOutcomeAboveThreshold},
		{"missing payment customer", &autoRechargeStoreStub{topup: &AutoRechargeTopup{ID: "topup"}, profile: autoRechargeActiveProfile()}, "0", AutoRechargeOutcomeFailed},
		{"claim limit", &autoRechargeStoreStub{topup: &AutoRechargeTopup{ID: "topup"}, profile: autoRechargeActiveProfile(), customer: &AutoRechargeCustomer{UserID: "user-1", Provider: "stripe", ProviderCustomerID: "cus"}, claimNil: true}, "0", AutoRechargeOutcomeLimitReached},
	} {
		t.Run(tc.name, func(t *testing.T) {
			service := autoRechargeTestService(t, tc.store, "key")
			result, err := service.ProcessIfNeeded(ctx, provider, AutoRechargeProcessInput{UserID: "user-1", Balance: MustAmount(tc.balance)})
			if err != nil || result.Outcome != tc.want {
				t.Fatalf("result=%#v err=%v", result, err)
			}
		})
	}
	store := &autoRechargeStoreStub{topup: &AutoRechargeTopup{ID: "topup"}, profile: autoRechargeActiveProfile(), customer: &AutoRechargeCustomer{UserID: "user-1", Provider: "stripe", ProviderCustomerID: "cus"}}
	service := autoRechargeTestService(t, store, "retry-key")
	provider.charge = SavedPaymentChargeResult{Status: SavedPaymentChargeFailed, ProviderPaymentID: "pi"}
	result, err := service.Retry(ctx, provider, AutoRechargeProcessInput{UserID: "user-1", Balance: MustAmount("0")})
	if err != nil || result.Outcome != AutoRechargeOutcomeFailed {
		t.Fatalf("retry result=%#v err=%v", result, err)
	}
	if store.profile.State != AutoRechargeStateActive {
		t.Fatalf("retry profile=%#v", store.profile)
	}
}

func TestAutoRechargeWindowAndIdentityBoundaries(t *testing.T) {
	now := time.Date(2026, 8, 19, 12, 0, 0, 0, time.UTC)
	for _, unit := range []string{"second", "minute", "hour", "day", "week"} {
		window, err := resolveAutoRechargeWindow(Window{Type: "rolling", Duration: &Duration{Unit: unit, Count: 1}}, now)
		if err != nil || window.Anchor != "rolling" {
			t.Fatalf("rolling %q = %#v, %v", unit, window, err)
		}
	}
	for _, unit := range []string{"day", "week", "month", "year"} {
		window, err := resolveAutoRechargeWindow(Window{Type: "calendar", Unit: unit, Count: 1, Timezone: "UTC"}, now)
		if err != nil || window.Anchor != "calendar" {
			t.Fatalf("calendar %q = %#v, %v", unit, window, err)
		}
	}
	if _, err := resolveAutoRechargeWindow(Window{Type: "calendar", Unit: "day", Count: 1, Timezone: "Not/AZone"}, now); err == nil {
		t.Fatal("invalid calendar timezone accepted")
	}
	if _, err := resolveAutoRechargeWindow(Window{Type: "rolling", Duration: &Duration{Unit: "day", Count: 1}}, time.Time{}); err == nil {
		t.Fatal("zero window instant accepted")
	}
	if _, err := autoRechargeProviderName(nil); err == nil {
		t.Fatal("nil provider accepted")
	}
	if err := autoRechargeProfileProviderMatches(&AutoRechargeProfile{Provider: "stripe"}, "dodo"); err == nil {
		t.Fatal("provider mismatch accepted")
	}
	if autoRechargeDiagnosticSummary(errors.New("provider")) != "auto_recharge_provider_failed:provider_error" {
		t.Fatal("diagnostic leaked provider error")
	}
}
