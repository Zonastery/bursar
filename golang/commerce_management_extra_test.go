package bursar

import (
	"context"
	"strings"
	"testing"
	"time"
)

func TestCommerceManagementPureValidationAndSerializationHelpers(t *testing.T) {
	if _, _, err := validateSubscriptionCommand(SubscriptionCommandInput{}); err == nil {
		t.Fatal("empty subscription command accepted")
	}
	if _, _, err := validateSubscriptionCommand(SubscriptionCommandInput{AccountID: "a", OperationKey: strings.Repeat("x", maxIdempotencyKeyLength+1)}); err == nil {
		t.Fatal("oversized operation key accepted")
	}
	account, key, err := validateSubscriptionCommand(SubscriptionCommandInput{AccountID: " a ", OperationKey: " op "})
	if err != nil || account != "a" || key != "op" {
		t.Fatalf("validated command = %q, %q, %v", account, key, err)
	}
	if got, err := commerceScopedKey("op", "provider:sub"); err != nil || got == "" || got == "op" {
		t.Fatalf("scoped key = %q, %v", got, err)
	}
	if _, err := commerceScopedKey("", "scope"); err == nil {
		t.Fatal("empty scoped key accepted")
	}

	ref := ProviderReference{ProductID: stringPointer("product"), PriceID: stringPointer("price")}
	if product, price, lookup := providerReferenceIDs(ref); product != "product" || price != "price" || lookup != "" {
		t.Fatalf("reference IDs = %q, %q, %q", product, price, lookup)
	}
	if bursarErr, ok := AsBursarError(providerCapabilityError("stripe", "cancel")); !ok || bursarErr.Code != ErrorCodeProviderCapabilityNotSupported {
		t.Fatal("wrong capability error code")
	}
	if metadataText(CreditMetadata{"key": "value"}, "key") != "value" || metadataText(nil, "missing") != "" {
		t.Fatal("metadataText mismatch")
	}
	amount := MustAmount("1.25")
	if amountString(nil) != nil || amountString(&amount) != "1.25" {
		t.Fatal("amountString mismatch")
	}
	now := time.Date(2026, time.August, 19, 1, 2, 3, 0, time.UTC)
	if timeString(nil) != nil || timeString(&now) != now.Format(time.RFC3339Nano) {
		t.Fatal("timeString mismatch")
	}
	if stringPointer("x") == nil || *stringPointer("x") != "x" {
		t.Fatal("stringPointer mismatch")
	}
	if got, err := planChangeQuoteFingerprint(PlanChangePreview{TotalAmount: MustAmount("10"), Currency: "USD", EffectiveAt: now}); err != nil || got == "" {
		t.Fatalf("fingerprint = %q, %v", got, err)
	}
	if _, err := planChangeQuoteFingerprint(PlanChangePreview{TotalAmount: MustAmount("10"), Currency: "USD", EffectiveAt: now}); err != nil {
		t.Fatal(err)
	}
}

func TestCommerceManagementCheckoutStatusReconcilesProviderStates(t *testing.T) {
	provider := &commerceStatusProvider{checkoutProviderStub: checkoutProviderStub{name: "stripe"}, status: "paid"}
	store := &checkoutStoreStub{intent: CheckoutIntent{ID: "intent", SubjectID: "subject", Provider: "stripe", ProviderSessionID: "session", Status: "open", ExpiresAt: time.Now().Add(time.Hour)}}
	service := newCommerceManagementTestService(t, store, provider)
	result, err := service.GetCheckoutStatus(context.Background(), "intent", "subject")
	if err != nil || result.Status != CheckoutStatusSucceeded || store.updated.Status != "completed" {
		t.Fatalf("status = %#v, update=%#v, err=%v", result, store.updated, err)
	}
	for _, status := range []string{"failed", "cancelled", "canceled", "expired", "completed", "unknown"} {
		got, err := service.resolveCheckoutStatus(context.Background(), CheckoutIntent{Status: status}, "subject")
		if err != nil {
			t.Fatal(err)
		}
		if got == CheckoutStatusPending {
			t.Errorf("status %q remained pending", status)
		}
	}
	provider.status = "open"
	got, err := service.resolveCheckoutStatus(context.Background(), CheckoutIntent{Provider: "stripe", ProviderSessionID: "session", Status: "open", ExpiresAt: time.Now().Add(time.Hour)}, "subject")
	if err != nil || got != CheckoutStatusPending {
		t.Fatalf("open provider status = %q, %v", got, err)
	}
}

func TestCommerceManagementSubscriptionCommandsAreAccountScopedAndWebhookPending(t *testing.T) {
	provider := &commerceLifecycleProvider{checkoutProviderStub: checkoutProviderStub{name: "stripe"}}
	store := &commerceManagementStateStub{checkoutStoreStub: checkoutStoreStub{subscription: &CommerceSubscription{Provider: "stripe", ProviderSubscriptionID: "sub-1", AccountID: "account-1", Status: "active"}}}
	service := newCommerceManagementTestService(t, &store.checkoutStoreStub, provider)
	service.state = store
	result, err := service.CancelSubscription(context.Background(), SubscriptionCommandInput{AccountID: "account-1", SubscriptionID: "sub-1", OperationKey: "cancel-1"})
	if err != nil || !result.OK || !result.Pending || provider.cancelKey != "cancel-1" {
		t.Fatalf("cancel = %#v, %v", result, err)
	}
	store.subscription.CancelAtPeriodEnd = true
	result, err = service.CancelSubscription(context.Background(), SubscriptionCommandInput{AccountID: "account-1", SubscriptionID: "sub-1", OperationKey: "cancel-2"})
	if err != nil || !result.OK || result.Pending {
		t.Fatalf("idempotent cancel = %#v, %v", result, err)
	}
	store.subscription.CancelAtPeriodEnd = false
	store.subscription.Status = "canceled"
	if _, err := service.CancelSubscription(context.Background(), SubscriptionCommandInput{AccountID: "account-1", SubscriptionID: "sub-1", OperationKey: "cancel-3"}); err == nil {
		t.Fatal("canceled subscription accepted")
	}
	store.subscription.Status = "active"
	store.subscription.CancelAtPeriodEnd = true
	result, err = service.ReactivateSubscription(context.Background(), SubscriptionCommandInput{AccountID: "account-1", SubscriptionID: "sub-1", OperationKey: "reactivate-1"})
	if err != nil || !result.OK || !result.Pending {
		t.Fatalf("reactivate = %#v, %v", result, err)
	}
	store.subscription.CancelAtPeriodEnd = false
	result, err = service.ReactivateSubscription(context.Background(), SubscriptionCommandInput{AccountID: "account-1", SubscriptionID: "sub-1", OperationKey: "reactivate-2"})
	if err != nil || !result.OK || result.Pending {
		t.Fatalf("already active reactivate = %#v, %v", result, err)
	}
}

func TestCommerceAutoRechargeAccountFacadeRequiresConfigurationAndUsesProviderProfile(t *testing.T) {
	var facade *CommerceAutoRecharge
	if _, err := facade.GetStatus(context.Background(), "account"); err == nil {
		t.Fatal("nil facade accepted")
	}
	service := autoRechargeTestService(t, &autoRechargeStoreStub{topup: &AutoRechargeTopup{ID: "topup"}, profile: autoRechargeActiveProfile()}, "key")
	provider := &autoRechargeProviderStub{autoRechargeProviderBase: autoRechargeProviderBase{name: "stripe"}}
	registry, err := NewProviderRegistry(ProviderFactoryContext{ProviderEnvironment: ProviderEnvironmentTest}, map[string]ProviderFactory{"stripe": func(context.Context, ProviderFactoryContext) (PaymentProvider, error) { return provider, nil }})
	if err != nil {
		t.Fatal(err)
	}
	commerce := &CommerceService{providers: registry, AutoRecharge: &CommerceAutoRecharge{service: service}}
	commerce.AutoRecharge.commerce = commerce
	if _, err := commerce.AutoRecharge.GetStatus(context.Background(), " "); err == nil {
		t.Fatal("empty account accepted")
	}
	status, err := commerce.AutoRecharge.GetStatus(context.Background(), "user-1")
	if err != nil || status == nil || !status.Enabled {
		t.Fatalf("facade status = %#v, %v", status, err)
	}
	if err := commerce.AutoRecharge.Disable(context.Background(), "user-1"); err != nil {
		t.Fatal(err)
	}
	if _, err := commerce.AutoRecharge.Retry(context.Background(), AutoRechargeInput{}); err == nil {
		t.Fatal("Retry accepted empty account")
	}
	if _, err := commerce.AutoRecharge.ProcessIfNeeded(context.Background(), AutoRechargeInput{}); err == nil {
		t.Fatal("ProcessIfNeeded accepted empty account")
	}
	if _, err := commerce.AutoRecharge.Enable(context.Background(), AutoRechargeInput{}); err == nil {
		t.Fatal("Enable accepted empty account")
	}
}

func TestCommerceAutoRechargeProviderSelectionUsesPersistedAuthority(t *testing.T) {
	stripe := &autoRechargeProviderStub{autoRechargeProviderBase: autoRechargeProviderBase{name: "stripe"}}
	dodo := &autoRechargeProviderStub{autoRechargeProviderBase: autoRechargeProviderBase{name: "dodo"}}
	registry, err := NewProviderRegistry(ProviderFactoryContext{ProviderEnvironment: ProviderEnvironmentTest}, map[string]ProviderFactory{
		"stripe": func(context.Context, ProviderFactoryContext) (PaymentProvider, error) { return stripe, nil },
		"dodo":   func(context.Context, ProviderFactoryContext) (PaymentProvider, error) { return dodo, nil },
	})
	if err != nil {
		t.Fatal(err)
	}
	store := &autoRechargeStoreStub{profile: &AutoRechargeProfile{UserID: "u", Provider: "dodo"}}
	service := autoRechargeTestService(t, store, "key")
	commerce := &CommerceService{providers: registry}
	if got, err := commerce.autoRechargeProvider(context.Background(), "u", service); err != nil || got.Name() != "dodo" {
		t.Fatalf("profile provider = %v, %v", got, err)
	}
	commerce.providers = nil
	if _, err := commerce.autoRechargeProvider(context.Background(), "u", service); err == nil {
		t.Fatal("missing registry accepted")
	}
}

type commerceStatusProvider struct {
	checkoutProviderStub
	status string
}

func (p *commerceStatusProvider) GetCheckoutSessionStatus(context.Context, string) (string, error) {
	return p.status, nil
}

type commerceLifecycleProvider struct {
	checkoutProviderStub
	cancelKey     string
	reactivateKey string
}

func (p *commerceLifecycleProvider) CancelSubscription(_ context.Context, _ string, key string) error {
	p.cancelKey = key
	return nil
}
func (p *commerceLifecycleProvider) ReactivateSubscription(_ context.Context, _ string, key string) error {
	p.reactivateKey = key
	return nil
}

type commerceManagementStateStub struct{ checkoutStoreStub }

func (s *commerceManagementStateStub) ListBillingSubscriptions(context.Context, string) ([]CommerceSubscription, error) {
	if s.subscription == nil {
		return nil, nil
	}
	return []CommerceSubscription{*s.subscription}, nil
}
func (s *commerceManagementStateStub) ResolveBillingOffer(context.Context, string, string, string, string) (*BillingOffer, error) {
	return nil, nil
}
func (s *commerceManagementStateStub) CreateBillingSubscriptionChange(context.Context, BillingSubscriptionChangeCreate) (BillingSubscriptionChange, error) {
	return BillingSubscriptionChange{ID: "change"}, nil
}
func (s *commerceManagementStateStub) GetOpenBillingSubscriptionChange(context.Context, string, string) (*BillingSubscriptionChange, error) {
	return nil, nil
}
func (s *commerceManagementStateStub) UpdateBillingSubscriptionChange(context.Context, string, BillingSubscriptionChangeUpdate) error {
	return nil
}
func (s *commerceManagementStateStub) ListBillingInvoices(context.Context, string) ([]BillingInvoice, error) {
	return nil, nil
}

func newCommerceManagementTestService(t *testing.T, store *checkoutStoreStub, provider PaymentProvider) *CommerceService {
	t.Helper()
	registry, err := NewProviderRegistry(ProviderFactoryContext{ProviderEnvironment: ProviderEnvironmentTest}, map[string]ProviderFactory{"stripe": func(context.Context, ProviderFactoryContext) (PaymentProvider, error) { return provider, nil }})
	if err != nil {
		t.Fatal(err)
	}
	return &CommerceService{store: store, state: store, providers: registry, credits: &CreditsService{}, catalog: &CatalogService{store: store}}
}
