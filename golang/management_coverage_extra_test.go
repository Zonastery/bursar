package bursar

import (
	"context"
	"errors"
	"testing"
	"time"
)

// These cases exercise the account-scoped management boundaries that are easy
// to miss when the happy-path checkout and plan-change tests are run alone.
func TestManagementCoverageCheckoutAndSubscriptionBoundaries(t *testing.T) {
	ctx := context.Background()
	provider := &commerceStatusProvider{checkoutProviderStub: checkoutProviderStub{name: "stripe"}, status: "paid"}
	store := &managementCoverageCheckoutStore{checkoutStoreStub: &checkoutStoreStub{intent: CheckoutIntent{ID: "intent", SubjectID: "subject", Provider: "stripe", ProviderSessionID: "session", Status: "open", ExpiresAt: time.Now().Add(time.Hour)}}}
	service := newCommerceManagementTestService(t, store.checkoutStoreStub, provider)
	service.store, service.state = store, store

	store.intentErr = errors.New("intent read failed")
	if _, err := service.GetCheckoutStatus(ctx, "intent", "subject"); err == nil {
		t.Fatal("checkout intent read failure was ignored")
	}
	store.intentErr = nil
	store.nilIntent = true
	if _, err := service.GetCheckoutStatus(ctx, "intent", "subject"); err == nil {
		t.Fatal("missing checkout intent was accepted")
	}
	store.nilIntent = false
	store.intent.ExpiresAt = time.Now().Add(-time.Minute)
	store.updateErr = errors.New("intent update failed")
	if _, err := service.GetCheckoutStatus(ctx, "intent", "subject"); err == nil {
		t.Fatal("durable expiry failure was ignored")
	}
	store.updateErr = nil
	store.intent.ExpiresAt = time.Now().Add(time.Hour)
	store.intent.ProviderSessionID = ""
	if got, err := service.GetCheckoutStatus(ctx, "intent", "subject"); err != nil || got.Status != CheckoutStatusPending {
		t.Fatalf("session-less checkout = %+v, %v", got, err)
	}
	base := store.intent
	base.ProviderSessionID = "session"
	store.intent = base
	plain := &checkoutProviderStub{name: "stripe"}
	service = newCommerceManagementTestService(t, store.checkoutStoreStub, plain)
	service.store, service.state = store, store
	if got, err := service.GetCheckoutStatus(ctx, "intent", "subject"); err != nil || got.Status != CheckoutStatusPending {
		t.Fatalf("provider without status capability = %+v, %v", got, err)
	}
	provider.status = "paid"
	service = newCommerceManagementTestService(t, store.checkoutStoreStub, provider)
	service.store, service.state = store, store
	store.updateErr = errors.New("provider status persistence failed")
	if _, err := service.GetCheckoutStatus(ctx, "intent", "subject"); err == nil {
		t.Fatal("provider status persistence failure was ignored")
	}

	service, state, lifecycle, accountID := commerceWavePlanFixture(t)
	state.subscription.CancelAtPeriodEnd = false
	lifecycle.cancelErr = errors.New("provider cancel failed")
	if _, err := service.CancelSubscription(ctx, SubscriptionCommandInput{AccountID: accountID, SubscriptionID: "sub-1", OperationKey: "cancel-error"}); err == nil {
		t.Fatal("provider cancellation failure was ignored")
	}
	state.subscription.CancelAtPeriodEnd = true
	if result, err := service.CancelSubscription(ctx, SubscriptionCommandInput{AccountID: accountID, SubscriptionID: "sub-1", OperationKey: "cancel-idempotent"}); err != nil || !result.OK || result.Pending {
		t.Fatalf("idempotent cancellation = %+v, %v", result, err)
	}
	state.subscription.CancelAtPeriodEnd = false
	state.subscription.Status = "paused"
	lifecycle.cancelErr = nil
	if result, err := service.ReactivateSubscription(ctx, SubscriptionCommandInput{AccountID: accountID, SubscriptionID: "sub-1", OperationKey: "reactivate-paused"}); err != nil || !result.Pending {
		t.Fatalf("paused reactivation = %+v, %v", result, err)
	}
	state.subscription.Status = "active"
	state.subscription.CancelAtPeriodEnd = true
	if _, err := service.ReactivateSubscription(ctx, SubscriptionCommandInput{AccountID: accountID, SubscriptionID: "sub-1", OperationKey: "reactivate-error"}); err != nil {
		t.Fatal(err)
	}
}

func TestManagementCoveragePreferencesDocumentsAndSummary(t *testing.T) {
	ctx := context.Background()
	service, state, provider, accountID := commerceWavePlanFixture(t)
	allFalse := false
	allTrue := true
	got, err := service.UpdatePreferences(ctx, accountID, PreferencePatch{AutoRecharge: &allTrue, OverageProtection: &allFalse, EmailNotifications: &allFalse, UsageAlerts: &allFalse, InvoiceReminders: &allTrue})
	if err != nil || !got.AutoRecharge || got.OverageProtection || got.EmailNotifications || got.UsageAlerts || !got.InvoiceReminders {
		t.Fatalf("preference merge = %+v, %v", got, err)
	}
	state.preferences = &BillingPreferences{AccountID: accountID, AutoRecharge: false, OverageProtection: false, EmailNotifications: false, UsageAlerts: false, InvoiceReminders: true}
	if got, err := service.UpdatePreferences(ctx, accountID, PreferencePatch{UsageAlerts: &allTrue}); err != nil || !got.InvoiceReminders || !got.UsageAlerts || got.AutoRecharge {
		t.Fatalf("existing preference merge = %+v, %v", got, err)
	}

	state.openChange = &BillingSubscriptionChange{ID: "pending", State: "scheduled", Effective: "renewal"}
	summary, err := service.GetAccountSubscriptionSummary(ctx, accountID)
	if err != nil || summary.PendingChange == nil || summary.AccessState != "entitled" || !summary.IsCurrent {
		t.Fatalf("subscription summary = %+v, %v", summary, err)
	}
	state.getChangeErr = errors.New("summary pending-change read failed")
	if _, err := service.GetAccountSubscriptionSummary(ctx, accountID); err == nil {
		t.Fatal("summary pending-change failure was ignored")
	}
	state.getChangeErr = nil
	state.subscription = &CommerceSubscription{AccountID: accountID, Provider: "stripe", ProviderSubscriptionID: "sub-1", Status: "past_due", GraceEndsAt: timePtr(time.Now().UTC().Add(time.Hour))}
	state.openChange = nil
	if summary, err = service.GetAccountSubscriptionSummary(ctx, accountID); err != nil || summary.AccessState != "grace" || !summary.IsCancellable {
		t.Fatalf("grace summary = %+v, %v", summary, err)
	}
	state.subscription = nil
	service.credits.store.(*commerceWaveCreditStore).plan = GetUserPlanResult{}
	if summary, err = service.GetAccountSubscriptionSummary(ctx, accountID); err != nil || summary.AccessState != "none" || summary.LifecycleState != "none" {
		t.Fatalf("empty summary = %+v, %v", summary, err)
	}

	state.subscription = &CommerceSubscription{AccountID: accountID, Provider: "stripe", ProviderSubscriptionID: "sub-1", Status: "active"}
	state.invoices = []BillingInvoice{{ID: "invoice-1", Provider: "stripe", AccountID: accountID, ProviderInvoiceID: "provider-invoice-1"}}
	entry := &LedgerEntry{EntryID: "payment-entry", AccountID: accountID, Metadata: CreditMetadata{"provider": "stripe", "provider_payment_id": "payment-1"}}
	service.credits.store.(*commerceWaveCreditStore).entry = entry
	if url, err := service.GetInvoiceLink(ctx, GetInvoiceLinkInput{AccountID: accountID, Document: InvoiceDocumentLocator{Kind: "ledger_entry", LedgerEntryID: entry.EntryID}}); err != nil || url != provider.invoiceURL {
		t.Fatalf("payment ledger invoice = %q, %v", url, err)
	}
	service.credits.store.(*commerceWaveCreditStore).entry = &LedgerEntry{EntryID: "empty-entry", AccountID: accountID}
	if _, err := service.GetInvoiceLink(ctx, GetInvoiceLinkInput{AccountID: accountID, Document: InvoiceDocumentLocator{Kind: "ledger_entry", LedgerEntryID: "empty-entry"}}); err == nil {
		t.Fatal("ledger entry without provider metadata was accepted")
	}
}

func TestManagementCoveragePlanContextAndPortalCapabilities(t *testing.T) {
	ctx := context.Background()
	service, state, _, accountID := commerceWavePlanFixture(t)
	for _, offer := range []string{"missing", "starter_month"} {
		if offer == "starter_month" {
			config := service.credits.store.(*commerceWaveCreditStore).active.Config
			config["commerce"].(map[string]any)["offers"].(map[string]any)[offer].(map[string]any)["providers"] = map[string]any{}
		}
		if _, err := service.PreviewPlanChange(ctx, PreviewPlanChangeInput{AccountID: accountID, OfferKey: offer}); err == nil {
			t.Fatalf("invalid target offer %q was accepted", offer)
		}
	}
	configForReference := service.credits.store.(*commerceWaveCreditStore).active.Config
	configForReference["commerce"].(map[string]any)["offers"].(map[string]any)["starter_year"].(map[string]any)["providers"] = map[string]any{}
	if _, err := service.PreviewPlanChange(ctx, PreviewPlanChangeInput{AccountID: accountID, OfferKey: "starter_year"}); err == nil {
		t.Fatal("target without provider reference was accepted")
	}
	config := service.credits.store.(*commerceWaveCreditStore).active.Config
	config["commerce"].(map[string]any)["offers"].(map[string]any)["starter_year"].(map[string]any)["providers"] = map[string]any{"stripe": map[string]any{"external_id": "stripe-starter-year"}}
	state.offers = nil
	if _, err := service.PreviewPlanChange(ctx, PreviewPlanChangeInput{AccountID: accountID, OfferKey: "starter_year"}); err == nil {
		t.Fatal("unpersisted target offer was accepted")
	}
	state.offers = map[string]*BillingOffer{"starter_year": {ID: "offer-year", Provider: "stripe", OfferKey: "starter_year", PlanKey: "starter", PriceID: "stripe-starter-year", Interval: "year", IntervalCnt: 1}}
	service.credits.store.(*commerceWaveCreditStore).plan = GetUserPlanResult{}
	if _, err := service.PreviewPlanChange(ctx, PreviewPlanChangeInput{AccountID: accountID, OfferKey: "starter_year"}); err == nil {
		t.Fatal("plan without current entitlement was accepted")
	}

	state.customers = nil
	if _, err := service.CreatePortalSession(ctx, PortalSessionInput{AccountID: accountID, ReturnURL: "https://app.example/return"}); err == nil {
		t.Fatal("portal without durable customer was accepted")
	}
}

func TestManagementCoverageProviderErrorsAndPlanChangeDurability(t *testing.T) {
	ctx := context.Background()
	service, state, provider, accountID := commerceWavePlanFixture(t)
	statusProvider := &managementCoverageStatusErrorProvider{checkoutProviderStub: checkoutProviderStub{name: "stripe"}, err: errors.New("status provider failed")}
	checkoutStore := &checkoutStoreStub{intent: CheckoutIntent{ID: "intent", SubjectID: "subject", Provider: "stripe", ProviderSessionID: "session", Status: "open", ExpiresAt: time.Now().Add(time.Hour)}}
	service = newCommerceManagementTestService(t, checkoutStore, statusProvider)
	if _, err := service.GetCheckoutStatus(ctx, "intent", "subject"); err == nil {
		t.Fatal("checkout provider failure was ignored")
	}
	failingFactory, err := NewProviderRegistry(ProviderFactoryContext{ProviderEnvironment: ProviderEnvironmentTest}, map[string]ProviderFactory{
		"stripe": func(context.Context, ProviderFactoryContext) (PaymentProvider, error) {
			return nil, errors.New("provider unavailable")
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	service.providers = failingFactory
	if _, err := service.GetCheckoutStatus(ctx, "intent", "subject"); err == nil {
		t.Fatal("provider registry failure was ignored")
	}

	service, state, provider, accountID = commerceWavePlanFixture(t)
	provider.previewErr = errors.New("quote unavailable")
	if _, err := service.PreviewPlanChange(ctx, PreviewPlanChangeInput{AccountID: accountID, OfferKey: "starter_year"}); err == nil {
		t.Fatal("provider quote failure was ignored")
	}
	provider.previewErr = nil
	preview, err := service.PreviewPlanChange(ctx, PreviewPlanChangeInput{AccountID: accountID, OfferKey: "starter_year"})
	if err != nil {
		t.Fatal(err)
	}
	state.getChangeErr = errors.New("pending-change read failed")
	if _, err := service.ConfirmPlanChange(ctx, ConfirmPlanChangeInput{AccountID: accountID, OfferKey: "starter_year", QuoteFingerprint: preview.QuoteFingerprint, OperationKey: "change-read-error"}); err == nil {
		t.Fatal("pending-change read failure was ignored")
	}
	state.getChangeErr = nil
	state.subscription.CancelAtPeriodEnd = true
	provider.reactivateErr = errors.New("reactivation failed")
	if _, err := service.ConfirmPlanChange(ctx, ConfirmPlanChangeInput{AccountID: accountID, OfferKey: "starter_year", QuoteFingerprint: preview.QuoteFingerprint, OperationKey: "reactivation-error"}); err == nil {
		t.Fatal("reactivation failure was ignored")
	}

	service, state, provider, accountID = commerceWavePlanFixture(t)
	state.subscription.CancelAtPeriodEnd = true
	preview, err = service.PreviewPlanChange(ctx, PreviewPlanChangeInput{AccountID: accountID, OfferKey: "starter_year"})
	if err != nil {
		t.Fatal(err)
	}
	noReact := &commerceWavePlanProvider{checkoutProviderStub: checkoutProviderStub{name: "stripe"}, preview: provider.preview}
	service = newCommerceWaveService(t, state, service.credits.store.(*commerceWaveCreditStore), noReact, service.credits.store.(*commerceWaveCreditStore).active.Config)
	if _, err := service.ConfirmPlanChange(ctx, ConfirmPlanChangeInput{AccountID: accountID, OfferKey: "starter_year", QuoteFingerprint: preview.QuoteFingerprint, OperationKey: "no-reactivation"}); err == nil {
		t.Fatal("plan change without reactivation capability was accepted")
	}

	service, state, provider, accountID = commerceWavePlanFixture(t)
	state.subscription.CancelAtPeriodEnd = true
	provider.changeErr = errors.New("change provider failed")
	provider.cancelErr = errors.New("restore cancellation failed")
	preview, err = service.PreviewPlanChange(ctx, PreviewPlanChangeInput{AccountID: accountID, OfferKey: "starter_year"})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := service.ConfirmPlanChange(ctx, ConfirmPlanChangeInput{AccountID: accountID, OfferKey: "starter_year", QuoteFingerprint: preview.QuoteFingerprint, OperationKey: "restore-error"}); err == nil {
		t.Fatal("provider restoration failure was ignored")
	}
}

func TestManagementCoverageOverviewCreditLineAndProviderCapability(t *testing.T) {
	ctx := context.Background()
	config := checkoutTestConfig(t)
	store := &overviewStoreStub{
		config:    config,
		balance:   BalanceResult{UserID: "account-1", Balance: MustAmount("1")},
		available: AvailableResult{UserID: "account-1", Available: MustAmount("2")},
		buckets:   BucketBalancesResult{UserID: "account-1"},
		plan:      GetUserPlanResult{UserID: "account-1", CreditPolicy: &PlanCreditPolicy{Type: "credit_line", CreditLimit: func() *Amount { value := MustAmount("3"); return &value }()}},
		customer:  &BillingCustomerRecord{Provider: "stripe", ProviderCustomerID: "cus-1", AccountID: "account-1"},
	}
	provider := &overviewNoMethodsProvider{checkoutProviderStub: checkoutProviderStub{name: "stripe"}}
	registry, err := NewProviderRegistry(ProviderFactoryContext{ProviderEnvironment: ProviderEnvironmentTest}, map[string]ProviderFactory{
		"stripe": func(context.Context, ProviderFactoryContext) (PaymentProvider, error) { return provider, nil },
	})
	if err != nil {
		t.Fatal(err)
	}
	service := &CommerceService{state: store, credits: &CreditsService{store: store, usageStore: store, analytics: store}, catalog: &CatalogService{store: store}, providers: registry}
	result, err := service.GetAccountOverview(ctx, "account-1")
	if err != nil {
		t.Fatal(err)
	}
	if !result.Credits.EffectiveSpendableBalance.Equal(MustAmount("5")) || result.Availability.PaymentMethods {
		t.Fatalf("credit-line/provider capability overview = %+v", result)
	}
	store.customer = nil
	if result, err = service.GetAccountOverview(ctx, "account-1"); err != nil || result.Availability.PaymentMethods != true {
		t.Fatalf("customer-less overview = %+v, %v", result, err)
	}

	store.customer = &BillingCustomerRecord{Provider: "stripe", ProviderCustomerID: "cus-1", AccountID: "account-1"}
	arStore := &autoRechargeStoreStub{topup: &AutoRechargeTopup{ID: "topup-1"}, profile: autoRechargeActiveProfile(), customer: &AutoRechargeCustomer{UserID: "account-1", Provider: "stripe", ProviderCustomerID: "cus-1"}}
	arService := autoRechargeTestService(t, arStore, "management-overview")
	autoProvider := &autoRechargeProviderStub{autoRechargeProviderBase: autoRechargeProviderBase{name: "stripe"}, methods: []PaymentMethodInfo{{ID: "pm-1", Last4: "4242", Brand: "visa", IsDefault: true}}}
	autoRegistry, err := NewProviderRegistry(ProviderFactoryContext{ProviderEnvironment: ProviderEnvironmentTest}, map[string]ProviderFactory{
		"stripe": func(context.Context, ProviderFactoryContext) (PaymentProvider, error) { return autoProvider, nil },
	})
	if err != nil {
		t.Fatal(err)
	}
	service.providers = autoRegistry
	service.AutoRecharge = &CommerceAutoRecharge{commerce: service, service: arService}
	if result, err = service.GetAccountOverview(ctx, "account-1"); err != nil || result.AutoRecharge == nil {
		t.Fatalf("auto-recharge overview = %+v, %v", result.AutoRecharge, err)
	}
}

func TestManagementCoverageOverviewCoreReadFailures(t *testing.T) {
	ctx := context.Background()
	base := &overviewStoreStub{config: checkoutTestConfig(t), balance: BalanceResult{Balance: MustAmount("1")}, available: AvailableResult{Available: MustAmount("1")}, buckets: BucketBalancesResult{}, plan: GetUserPlanResult{}}
	for _, test := range []struct {
		name string
		set  func(*managementCoverageOverviewErrors)
	}{
		{"balance", func(s *managementCoverageOverviewErrors) { s.balanceErr = errors.New("balance") }},
		{"available", func(s *managementCoverageOverviewErrors) { s.availableErr = errors.New("available") }},
		{"buckets", func(s *managementCoverageOverviewErrors) { s.bucketsErr = errors.New("buckets") }},
		{"plan", func(s *managementCoverageOverviewErrors) { s.planErr = errors.New("plan") }},
		{"allowance", func(s *managementCoverageOverviewErrors) { s.allowanceErr = errors.New("allowance") }},
		{"preferences", func(s *managementCoverageOverviewErrors) { s.preferencesErr = errors.New("preferences") }},
		{"catalog", func(s *managementCoverageOverviewErrors) { s.catalogErr = errors.New("catalog") }},
	} {
		t.Run(test.name, func(t *testing.T) {
			store := &managementCoverageOverviewErrors{overviewStoreStub: base}
			test.set(store)
			provider := &overviewProviderStub{name: "stripe"}
			service := newManagementCoverageOverviewService(t, store, provider)
			if _, err := service.GetAccountOverview(ctx, "account-1"); err == nil {
				t.Fatal("overview read failure was ignored")
			}
		})
	}
}

func TestManagementCoverageCommandCapabilityErrors(t *testing.T) {
	ctx := context.Background()
	service, state, _, accountID := commerceWavePlanFixture(t)
	plain := &commerceWavePaymentProvider{checkoutProviderStub: checkoutProviderStub{name: "stripe"}}
	service = newCommerceWaveService(t, state, service.credits.store.(*commerceWaveCreditStore), plain, service.credits.store.(*commerceWaveCreditStore).active.Config)
	if _, err := service.CancelSubscription(ctx, SubscriptionCommandInput{AccountID: accountID, SubscriptionID: "sub-1", OperationKey: "no-cancel"}); err == nil {
		t.Fatal("cancel without provider capability was accepted")
	}
	state.subscription.CancelAtPeriodEnd = true
	if _, err := service.ReactivateSubscription(ctx, SubscriptionCommandInput{AccountID: accountID, SubscriptionID: "sub-1", OperationKey: "no-reactivate"}); err == nil {
		t.Fatal("reactivate without provider capability was accepted")
	}
	state.listSubscriptionsErr = errors.New("subscription list failed")
	if _, err := service.CancelSubscription(ctx, SubscriptionCommandInput{AccountID: accountID, SubscriptionID: "sub-1", OperationKey: "list-error"}); err == nil {
		t.Fatal("subscription list failure was ignored")
	}
}

type managementCoverageCheckoutStore struct {
	*checkoutStoreStub
	intentErr error
	nilIntent bool
}

type overviewNoMethodsProvider struct{ checkoutProviderStub }

type managementCoverageOverviewErrors struct {
	*overviewStoreStub
	balanceErr, availableErr, bucketsErr, planErr, allowanceErr, preferencesErr, catalogErr error
}

func (s *managementCoverageOverviewErrors) GetBalance(context.Context, string) (BalanceResult, error) {
	return s.balance, s.balanceErr
}
func (s *managementCoverageOverviewErrors) GetAvailable(context.Context, string) (AvailableResult, error) {
	return s.available, s.availableErr
}
func (s *managementCoverageOverviewErrors) GetBucketBalances(context.Context, string) (BucketBalancesResult, error) {
	return s.buckets, s.bucketsErr
}
func (s *managementCoverageOverviewErrors) GetUserPlan(context.Context, string) (GetUserPlanResult, error) {
	return s.plan, s.planErr
}
func (s *managementCoverageOverviewErrors) CheckAllowance(context.Context, string) (*AllowanceResult, error) {
	return s.allowance, s.allowanceErr
}
func (s *managementCoverageOverviewErrors) GetBillingPreferences(context.Context, string) (*BillingPreferences, error) {
	return s.preferences, s.preferencesErr
}
func (s *managementCoverageOverviewErrors) GetActiveCatalog(context.Context) (*CatalogRevision, error) {
	if s.catalogErr != nil {
		return nil, s.catalogErr
	}
	return s.overviewStoreStub.GetActiveCatalog(context.Background())
}

func newManagementCoverageOverviewService(t *testing.T, store *managementCoverageOverviewErrors, provider *overviewProviderStub) *CommerceService {
	t.Helper()
	registry, err := NewProviderRegistry(ProviderFactoryContext{ProviderEnvironment: ProviderEnvironmentTest}, map[string]ProviderFactory{
		provider.name: func(context.Context, ProviderFactoryContext) (PaymentProvider, error) { return provider, nil },
	})
	if err != nil {
		t.Fatal(err)
	}
	return &CommerceService{state: store, credits: &CreditsService{store: store, usageStore: store, analytics: store}, catalog: &CatalogService{store: store}, providers: registry}
}

type managementCoverageStatusErrorProvider struct {
	checkoutProviderStub
	err error
}

func (p *managementCoverageStatusErrorProvider) GetCheckoutSessionStatus(context.Context, string) (string, error) {
	return "", p.err
}

func (s *managementCoverageCheckoutStore) GetCheckoutIntent(context.Context, string, string) (*CheckoutIntent, error) {
	if s.intentErr != nil {
		return nil, s.intentErr
	}
	if s.nilIntent {
		return nil, nil
	}
	return &s.intent, nil
}
