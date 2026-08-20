package bursar

import (
	"context"
	"errors"
	"testing"
	"time"
)

func TestBillingManagementDelegatesOptionalStoreCapabilities(t *testing.T) {
	store := &billingManagementStore{
		billingLifecycleStoreStub: &billingLifecycleStoreStub{environment: ProviderEnvironmentTest},
		checkoutStoreStub:         &checkoutStoreStub{intent: CheckoutIntent{ID: "intent-1"}, subscription: &CommerceSubscription{Provider: "stripe", ProviderSubscriptionID: "sub-1", Status: "active"}},
		autoRechargeStoreStub:     &autoRechargeStoreStub{profile: autoRechargeActiveProfile(), topup: &AutoRechargeTopup{ID: "topup-1"}},
		activeCatalog:             &CatalogRevision{Config: map[string]any{"version": 1}},
	}
	service, err := NewBillingService(store)
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	if got, err := service.CreateOrGetCheckoutIntent(ctx, CheckoutIntentCreate{AccountID: "a"}); err != nil || got.ID != "intent-1" {
		t.Fatalf("checkout create = %#v, %v", got, err)
	}
	if err := service.UpdateCheckoutIntent(ctx, "intent-1", "subject", CheckoutIntentUpdate{Status: "open"}); err != nil {
		t.Fatal(err)
	}
	if got, err := service.GetCheckoutIntent(ctx, "intent-1", "subject"); err != nil || got == nil {
		t.Fatalf("checkout get = %#v, %v", got, err)
	}
	if got, err := service.GetUserSubscription(ctx, "a"); err != nil || got == nil {
		t.Fatalf("user subscription = %#v, %v", got, err)
	}
	if got, err := service.GetActiveSubscription(ctx, "a"); err != nil || got == nil {
		t.Fatalf("active subscription = %#v, %v", got, err)
	}
	if got, err := service.GetBlockingSubscription(ctx, "a"); err != nil || got == nil {
		t.Fatalf("blocking subscription = %#v, %v", got, err)
	}
	if got, err := service.ListCancellableSubscriptions(ctx, "a"); err != nil || len(got) != 1 {
		t.Fatalf("cancellable = %#v, %v", got, err)
	}
	if got, err := service.ListCancellableProviderSubscriptionIDs(ctx, "a"); err != nil || len(got) != 1 || got[0] != "sub-1" {
		t.Fatalf("cancellable IDs = %#v, %v", got, err)
	}
	if _, err := service.GetUserPreferences(ctx, "a"); err != nil {
		t.Fatal(err)
	}
	if _, err := service.GetActiveCatalogDocument(ctx); err != nil {
		t.Fatal(err)
	}
	if err := service.UpdateUserPreferences(ctx, BillingPreferences{AccountID: "a"}); err != nil {
		t.Fatal(err)
	}
	if _, err := service.ListBillingInvoices(ctx, "a"); err != nil {
		t.Fatal(err)
	}
	if _, err := service.CreateBillingSubscriptionChange(ctx, BillingSubscriptionChangeCreate{}); err != nil {
		t.Fatal(err)
	}
	if _, err := service.GetOpenBillingSubscriptionChange(ctx, "stripe", "sub-1"); err != nil {
		t.Fatal(err)
	}
	if err := service.UpdateBillingSubscriptionChange(ctx, "change", BillingSubscriptionChangeUpdate{}); err != nil {
		t.Fatal(err)
	}
	if got, err := service.GetCustomerByUserID(ctx, "a", "stripe"); err != nil || got != nil {
		t.Fatalf("customer = %#v, %v", got, err)
	}
	if _, err := service.ResolveOffer(ctx, "stripe", "product", "price"); err != nil {
		t.Fatal(err)
	}
	if _, err := service.ResolveOfferByLookup(ctx, "stripe", "lookup"); err != nil {
		t.Fatal(err)
	}
	if _, err := service.ResolveTopup(ctx, "stripe", "product", "price"); err != nil {
		t.Fatal(err)
	}
	if _, err := service.ResolveTopupByLookup(ctx, "stripe", "lookup"); err != nil {
		t.Fatal(err)
	}
	if err := service.UpsertCustomer(ctx, "stripe", "cus", "a", "a@example.test"); err != nil {
		t.Fatal(err)
	}
	if _, err := service.GetAutoRechargeProfile(ctx, "a"); err != nil {
		t.Fatal(err)
	}
	if err := service.UpsertAutoRechargeProfile(ctx, *autoRechargeActiveProfile(), AutoRechargeProfileUpsertOptions{}); err != nil {
		t.Fatal(err)
	}
	if _, err := service.ClaimAutoRechargeAttempt(ctx, AutoRechargeAttemptClaim{UserID: "a"}); err != nil {
		t.Fatal(err)
	}
	if err := service.UpdateAutoRechargeAttempt(ctx, AutoRechargeAttemptUpdate{ID: "attempt"}); err != nil {
		t.Fatal(err)
	}
	if err := service.UpdateAutoRechargeAttemptByProviderPayment(ctx, AutoRechargeProviderPaymentUpdate{}); err != nil {
		t.Fatal(err)
	}
	if _, err := service.CountAutoRechargeAttempts(ctx, "a", time.Now()); err != nil {
		t.Fatal(err)
	}
	if err := service.RecordSubscriptionConflict(ctx, BillingSubscriptionConflictCreate{}); err != nil {
		t.Fatal(err)
	}
	if id, err := service.UpsertBillingSubscription(ctx, CommerceSubscription{}); err != nil || id != "subscription-id" {
		t.Fatalf("upsert subscription = %q, %v", id, err)
	}
	if err := service.PseudonymizeFinancialSubject(ctx, "a"); err != nil {
		t.Fatal(err)
	}
	service.InvalidateOfferCache()
}

func TestBillingManagementCapabilityAndGraceBoundaries(t *testing.T) {
	service, err := NewBillingService(&billingLifecycleStoreStub{environment: ProviderEnvironmentTest})
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	if _, err := service.CreateOrGetCheckoutIntent(ctx, CheckoutIntentCreate{}); err == nil {
		t.Fatal("checkout capability accepted")
	}
	if _, err := service.GetActiveSubscription(ctx, "a"); err == nil {
		t.Fatal("state capability accepted")
	}
	if _, err := service.GetAutoRechargeProfile(ctx, "a"); err == nil {
		t.Fatal("auto-recharge capability accepted")
	}
	if _, err := service.ResolveTopup(ctx, "stripe", "p", "q"); err == nil {
		t.Fatal("topup capability accepted")
	}
	if _, err := service.ResolveTopupByLookup(ctx, "stripe", "lookup"); err == nil {
		t.Fatal("topup lookup capability accepted")
	}
	if err := service.UpsertCustomer(ctx, "stripe", "customer", "account", "a@example.test"); err == nil {
		t.Fatal("customer writer capability accepted")
	}
	if err := service.UpdateAutoRechargeAttemptByProviderPayment(ctx, AutoRechargeProviderPaymentUpdate{}); err == nil {
		t.Fatal("provider-payment reconciliation capability accepted")
	}
	if err := service.RecordSubscriptionConflict(ctx, BillingSubscriptionConflictCreate{}); err == nil {
		t.Fatal("conflict capability accepted")
	}
	if _, err := service.UpsertBillingSubscription(ctx, CommerceSubscription{}); err == nil {
		t.Fatal("subscription writer capability accepted")
	}
	if err := service.PseudonymizeFinancialSubject(ctx, "a"); err == nil {
		t.Fatal("pseudonymizer capability accepted")
	}
	if got, err := service.ExpirePastDueGracePeriods(ctx, time.Time{}, 1); err != nil || got != 0 {
		t.Fatalf("no provisioning grace = %d, %v", got, err)
	}
	emptyCatalogStore := &billingManagementStore{
		billingLifecycleStoreStub: &billingLifecycleStoreStub{environment: ProviderEnvironmentTest},
		checkoutStoreStub:         &checkoutStoreStub{},
	}
	emptyCatalogService, err := NewBillingService(emptyCatalogStore)
	if err != nil {
		t.Fatal(err)
	}
	if document, err := emptyCatalogService.GetActiveCatalogDocument(ctx); err != nil || document != nil {
		t.Fatalf("empty active catalog = %#v, %v", document, err)
	}
	service.provisioning = &CreditsService{}
	if _, err := service.ExpirePastDueGracePeriods(ctx, time.Time{}, -1); err == nil {
		t.Fatal("negative grace limit accepted")
	}
	graceEnds := time.Now().UTC().Add(-time.Hour)
	store := &billingManagementStore{
		billingLifecycleStoreStub: &billingLifecycleStoreStub{environment: ProviderEnvironmentTest},
		checkoutStoreStub:         &checkoutStoreStub{subscription: &CommerceSubscription{ID: "sub-row", AccountID: "a", Provider: "stripe", ProviderSubscriptionID: "sub-1", Status: "past_due", GraceEndsAt: &graceEnds}},
		expired:                   []CommerceSubscription{{ID: "sub-row", AccountID: "a", Provider: "stripe", ProviderSubscriptionID: "sub-1", Status: "past_due", GraceEndsAt: &graceEnds}},
		activeCatalog:             &CatalogRevision{Config: map[string]any{"version": 1}},
	}
	service, err = NewBillingService(store)
	if err != nil {
		t.Fatal(err)
	}
	provisioning := &billingProvisioningStub{}
	service.provisioning = provisioning
	if count, err := service.ExpirePastDueGracePeriods(ctx, time.Now(), 10); err != nil || count != 1 || store.graceExpired != 1 || provisioning.unset != 0 {
		t.Fatalf("grace expiry = %d, %v atomic=%d unset=%d", count, err, store.graceExpired, provisioning.unset)
	}
	if _, err := service.GetBlockingSubscription(ctx, "a"); err != nil {
		t.Fatal(err)
	}
	service.terminalPlanKey = "free"
	if _, err := service.expireGraceIfNeeded(ctx, store.subscription, time.Now()); err != nil || store.terminalPlanKey != "free" || provisioning.set != 0 {
		t.Fatalf("terminal grace expiry = %v key=%q set=%d", err, store.terminalPlanKey, provisioning.set)
	}
}

func TestBillingManagementNilFacadeFailsClosedAcrossOptionalCapabilities(t *testing.T) {
	var service *BillingService
	ctx := context.Background()
	checks := []struct {
		name string
		call func() error
	}{
		{"checkout update", func() error { return service.UpdateCheckoutIntent(ctx, "intent", "subject", CheckoutIntentUpdate{}) }},
		{"checkout read", func() error { _, err := service.GetCheckoutIntent(ctx, "intent", "subject"); return err }},
		{"user subscription", func() error { _, err := service.GetUserSubscription(ctx, "account"); return err }},
		{"blocking subscription", func() error { _, err := service.GetBlockingSubscription(ctx, "account"); return err }},
		{"cancellable subscriptions", func() error { _, err := service.ListCancellableSubscriptions(ctx, "account"); return err }},
		{"cancellable subscription IDs", func() error { _, err := service.ListCancellableProviderSubscriptionIDs(ctx, "account"); return err }},
		{"preferences read", func() error { _, err := service.GetUserPreferences(ctx, "account"); return err }},
		{"catalog document", func() error { _, err := service.GetActiveCatalogDocument(ctx); return err }},
		{"subscription conflict", func() error { return service.RecordSubscriptionConflict(ctx, BillingSubscriptionConflictCreate{}) }},
		{"subscription upsert", func() error { _, err := service.UpsertBillingSubscription(ctx, CommerceSubscription{}); return err }},
		{"preferences update", func() error { return service.UpdateUserPreferences(ctx, BillingPreferences{}) }},
		{"invoices", func() error { _, err := service.ListBillingInvoices(ctx, "account"); return err }},
		{"create change", func() error {
			_, err := service.CreateBillingSubscriptionChange(ctx, BillingSubscriptionChangeCreate{})
			return err
		}},
		{"open change", func() error {
			_, err := service.GetOpenBillingSubscriptionChange(ctx, "provider", "subscription")
			return err
		}},
		{"update change", func() error {
			return service.UpdateBillingSubscriptionChange(ctx, "change", BillingSubscriptionChangeUpdate{})
		}},
		{"customer", func() error { _, err := service.GetCustomerByUserID(ctx, "account", "provider"); return err }},
		{"offer", func() error { _, err := service.ResolveOffer(ctx, "provider", "product", "price"); return err }},
		{"offer lookup", func() error { _, err := service.ResolveOfferByLookup(ctx, "provider", "lookup"); return err }},
		{"topup", func() error { _, err := service.ResolveTopup(ctx, "provider", "product", "price"); return err }},
		{"topup lookup", func() error { _, err := service.ResolveTopupByLookup(ctx, "provider", "lookup"); return err }},
		{"customer upsert", func() error { return service.UpsertCustomer(ctx, "provider", "customer", "account", "a@example.test") }},
		{"profile upsert", func() error {
			return service.UpsertAutoRechargeProfile(ctx, AutoRechargeProfile{}, AutoRechargeProfileUpsertOptions{})
		}},
		{"attempt claim", func() error { _, err := service.ClaimAutoRechargeAttempt(ctx, AutoRechargeAttemptClaim{}); return err }},
		{"attempt update", func() error { return service.UpdateAutoRechargeAttempt(ctx, AutoRechargeAttemptUpdate{}) }},
		{"attempt provider update", func() error {
			return service.UpdateAutoRechargeAttemptByProviderPayment(ctx, AutoRechargeProviderPaymentUpdate{})
		}},
		{"attempt count", func() error { _, err := service.CountAutoRechargeAttempts(ctx, "account", time.Now()); return err }},
		{"pseudonymize", func() error { return service.PseudonymizeFinancialSubject(ctx, "account") }},
	}
	for _, test := range checks {
		t.Run(test.name, func(t *testing.T) {
			if err := test.call(); err == nil {
				t.Fatal("unconfigured billing capability was accepted")
			}
		})
	}
	service.InvalidateOfferCache()
}

type billingManagementStore struct {
	*billingLifecycleStoreStub
	*checkoutStoreStub
	*autoRechargeStoreStub
	activeCatalog   *CatalogRevision
	expired         []CommerceSubscription
	graceExpired    int
	terminalPlanKey string
}

func (s *billingManagementStore) GetActiveCatalog(context.Context) (*CatalogRevision, error) {
	return s.activeCatalog, nil
}
func (s *billingManagementStore) ResolveBillingOffer(context.Context, string, string, string, string) (*BillingOffer, error) {
	return &BillingOffer{}, nil
}
func (s *billingManagementStore) CreateBillingSubscriptionChange(context.Context, BillingSubscriptionChangeCreate) (BillingSubscriptionChange, error) {
	return BillingSubscriptionChange{ID: "change"}, nil
}
func (s *billingManagementStore) GetOpenBillingSubscriptionChange(context.Context, string, string) (*BillingSubscriptionChange, error) {
	return nil, nil
}
func (s *billingManagementStore) UpdateBillingSubscriptionChange(context.Context, string, BillingSubscriptionChangeUpdate) error {
	return nil
}
func (s *billingManagementStore) ListBillingInvoices(context.Context, string) ([]BillingInvoice, error) {
	return nil, nil
}
func (s *billingManagementStore) ListBillingSubscriptions(context.Context, string) ([]CommerceSubscription, error) {
	if s.subscription == nil {
		return nil, nil
	}
	return []CommerceSubscription{*s.subscription}, nil
}
func (s *billingManagementStore) UpsertBillingCustomer(context.Context, BillingCustomerRecord) error {
	return nil
}
func (s *billingManagementStore) RecordSubscriptionConflict(context.Context, BillingSubscriptionConflictCreate) error {
	return nil
}
func (s *billingManagementStore) UpsertBillingSubscriptionState(context.Context, CommerceSubscription) (string, error) {
	return "subscription-id", nil
}
func (s *billingManagementStore) ResolveBillingTopup(context.Context, string, string, string, string) (*BillingTopupResult, error) {
	return &BillingTopupResult{}, nil
}
func (s *billingManagementStore) UpdateAutoRechargeAttemptByProviderPayment(context.Context, AutoRechargeProviderPaymentUpdate) error {
	return nil
}
func (s *billingManagementStore) PseudonymizeFinancialSubject(context.Context, string) (bool, error) {
	return true, nil
}
func (s *billingManagementStore) ListExpiredGraceSubscriptions(context.Context, time.Time, int) ([]CommerceSubscription, error) {
	return s.expired, nil
}
func (s *billingManagementStore) expirePastDueGracePeriod(_ context.Context, _ CommerceSubscription, _ time.Time, terminalPlanKey string) (bool, error) {
	s.graceExpired++
	s.terminalPlanKey = terminalPlanKey
	return true, nil
}
func (s *billingManagementStore) GetBillingSubscriptionByProvider(context.Context, string, string) (*CommerceSubscription, error) {
	return s.subscription, nil
}

type billingProvisioningStub struct{ unset, set int }

func (*billingProvisioningStub) GetUserPlan(context.Context, string) (GetUserPlanResult, error) {
	return GetUserPlanResult{}, nil
}
func (s *billingProvisioningStub) SetUserPlan(context.Context, string, string, SetUserPlanOptions) (SetUserPlanResult, error) {
	s.set++
	return SetUserPlanResult{}, nil
}
func (s *billingProvisioningStub) UnsetUserPlan(context.Context, string) (UnsetUserPlanResult, error) {
	s.unset++
	return UnsetUserPlanResult{}, nil
}

func TestPublicFacadeSupportBoundaries(t *testing.T) {
	if _, err := (&Bursar{}).RequireBilling(); err == nil {
		t.Fatal("missing billing accepted")
	}
	if _, err := (&Bursar{}).RequireCommerce(); err == nil {
		t.Fatal("missing commerce accepted")
	}
	if err := (&Bursar{}).LoadCatalog(context.Background()); err == nil {
		t.Fatal("missing catalog accepted")
	}
	if _, err := (&Bursar{}).IngestBillingEvent(context.Background(), lifecycleEvent(BillingEventPaymentSucceeded)); err == nil {
		t.Fatal("missing billing accepted")
	}
	if err := (&Bursar{}).Close(); err != nil {
		t.Fatal(err)
	}
	if got := joinStrings([]string{"a", "b"}, ","); got != "a,b" {
		t.Fatal(got)
	}
	if err := (&BursarRuntime{}).Flush(context.Background()); err != nil {
		t.Fatal(err)
	}

	for _, value := range []ProviderEnvironment{"", "invalid"} {
		if value.Validate() == nil {
			t.Fatalf("environment %q accepted", value)
		}
	}
	if err := (UsageMetrics{Operation: "op", Dimensions: map[string]any{"float": 1.5}}).Validate(); err == nil {
		t.Fatal("float metric accepted")
	}
	if err := (UsageMetrics{Operation: "op", Dimensions: map[string]any{"text": "ok"}}).Validate(); err != nil {
		t.Fatal(err)
	}

	base := NewError("message", ErrorOptions{Code: ErrorCodeConfig, Category: ErrorCategoryInvalidRequest, Details: map[string]any{"safe": "value"}, Cause: errors.New("secret")})
	serialized := base.Serialize()
	if serialized.Code != ErrorCodeConfig || serialized.Category != ErrorCategoryInvalidRequest {
		t.Fatalf("serialized error = %#v", serialized)
	}
	if ErrorHTTPStatus(base) != 400 || PublicErrorMessage(errors.New("secret")) != "Billing service is temporarily unavailable. Please try again." {
		t.Fatal("error mapping mismatch")
	}
	if ErrorHTTPStatus(NewStoreTimeoutError("timeout", nil)) != 503 {
		t.Fatal("timeout status mismatch")
	}
}

func TestCreditEmitterClearAndUnsubscribe(t *testing.T) {
	emitter := NewCreditEventEmitter()
	called := 0
	off := emitter.On(CreditEventAdded, func(context.Context, CreditEvent) { called++ })
	emitter.Emit(context.Background(), CreditEvent{Type: CreditEventAdded})
	off()
	emitter.Emit(context.Background(), CreditEvent{Type: CreditEventAdded})
	if called != 1 {
		t.Fatalf("called = %d", called)
	}
	emitter.On(CreditEventAdded, func(context.Context, CreditEvent) { called++ })
	emitter.Clear(CreditEventAdded)
	emitter.Emit(context.Background(), CreditEvent{Type: CreditEventAdded})
	emitter.ClearAll()
	if runtimeName := runtimeComponentName(0); runtimeName != "component_1" {
		t.Fatal(runtimeName)
	}
	if dependencyOutcome("dependency", time.Now(), errors.New("secret")).Error == "secret" {
		t.Fatal("dependency leaked raw error")
	}
}

func TestRuntimeDependencyDiagnosticsAndMetricBoundaries(t *testing.T) {
	catalog := &CatalogService{store: &catalogWaveStore{active: &CatalogRevision{Config: map[string]any{"version": 1}}}}
	runtime, err := NewBursarRuntime(BursarRuntimeOptions{Bursar: &Bursar{Catalog: catalog}, Components: []RuntimeComponent{&runtimeComponentStub{}, &runtimeComponentNoHealth{}}})
	if err != nil {
		t.Fatal(err)
	}
	diagnostics := runtime.CheckDependencies(context.Background())
	if diagnostics.Catalog.Skipped || !diagnostics.Catalog.OK || len(diagnostics.Components) != 2 || !diagnostics.Components[1].Skipped {
		t.Fatalf("diagnostics = %#v", diagnostics)
	}
	if got := (&BursarRuntime{}).CheckDependencies(context.Background()); !got.Catalog.Skipped {
		t.Fatalf("nil diagnostics = %#v", got)
	}
	if err := runtime.Close(context.Background()); err != nil {
		t.Fatal(err)
	}
	if got := runtime.CheckDependencies(context.Background()); !got.Catalog.Skipped {
		t.Fatalf("closed diagnostics = %#v", got)
	}
	for _, metrics := range []UsageMetrics{{Operation: "op", Measures: map[string]Amount{"tokens": MustAmount("-1")}}, {Operation: "op", Dimensions: map[string]any{"": "bad"}}} {
		if err := metrics.Validate(); err == nil {
			t.Fatalf("invalid metrics accepted: %#v", metrics)
		}
	}
}

type runtimeComponentNoHealth struct{}

func (*runtimeComponentNoHealth) Start(context.Context) error { return nil }
func (*runtimeComponentNoHealth) Flush(context.Context) error { return nil }
func (*runtimeComponentNoHealth) Close(context.Context) error { return nil }
