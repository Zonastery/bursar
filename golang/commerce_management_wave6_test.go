// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"
)

type commerceWaveCreditStore struct {
	CreditStore
	active     *CatalogRevision
	plan       GetUserPlanResult
	balance    BalanceResult
	balanceErr error
	entry      *LedgerEntry
}

func (s *commerceWaveCreditStore) GetActiveCatalog(context.Context) (*CatalogRevision, error) {
	return s.active, nil
}

func (s *commerceWaveCreditStore) GetUserPlan(context.Context, string) (GetUserPlanResult, error) {
	return s.plan, nil
}

func (s *commerceWaveCreditStore) GetBalance(context.Context, string) (BalanceResult, error) {
	if s.balanceErr != nil {
		return BalanceResult{}, s.balanceErr
	}
	return s.balance, nil
}

func (s *commerceWaveCreditStore) GetLedgerEntry(context.Context, string, string) (*LedgerEntry, error) {
	return s.entry, nil
}

type commerceWaveState struct {
	checkoutStoreStub
	subscription         *CommerceSubscription
	subscriptions        []CommerceSubscription
	offers               map[string]*BillingOffer
	openChange           *BillingSubscriptionChange
	createdChange        *BillingSubscriptionChangeCreate
	updatedChange        *BillingSubscriptionChangeUpdate
	invoices             []BillingInvoice
	createChangeErr      error
	getSubscriptionErr   error
	listSubscriptionsErr error
	resolveOfferErr      error
	getChangeErr         error
	updateChangeErr      error
	getCustomerErr       error
	listInvoicesErr      error
}

func (s *commerceWaveState) GetBillingCustomer(ctx context.Context, accountID, provider string) (*BillingCustomerRecord, error) {
	if s.getCustomerErr != nil {
		return nil, s.getCustomerErr
	}
	return s.checkoutStoreStub.GetBillingCustomer(ctx, accountID, provider)
}

func (s *commerceWaveState) GetBillingSubscription(_ context.Context, _ string, statuses []string) (*CommerceSubscription, error) {
	if s.getSubscriptionErr != nil {
		return nil, s.getSubscriptionErr
	}
	if s.subscription == nil || (statuses != nil && !containsText(statuses, s.subscription.Status)) {
		return nil, nil
	}
	copy := *s.subscription
	return &copy, nil
}

func (s *commerceWaveState) ListBillingSubscriptions(context.Context, string) ([]CommerceSubscription, error) {
	if s.listSubscriptionsErr != nil {
		return nil, s.listSubscriptionsErr
	}
	if s.subscriptions != nil {
		return append([]CommerceSubscription(nil), s.subscriptions...), nil
	}
	if s.subscription == nil {
		return nil, nil
	}
	return []CommerceSubscription{*s.subscription}, nil
}

func (s *commerceWaveState) ResolveBillingOffer(_ context.Context, provider, productID, priceID, lookupKey string) (*BillingOffer, error) {
	if s.resolveOfferErr != nil {
		return nil, s.resolveOfferErr
	}
	for _, offer := range s.offers {
		if offer.Provider == provider && (offer.PriceID == priceID || offer.ProductID == productID || offer.LookupKey == lookupKey) {
			copy := *offer
			return &copy, nil
		}
	}
	return nil, nil
}

func (s *commerceWaveState) CreateBillingSubscriptionChange(_ context.Context, input BillingSubscriptionChangeCreate) (BillingSubscriptionChange, error) {
	if s.createChangeErr != nil {
		return BillingSubscriptionChange{}, s.createChangeErr
	}
	s.createdChange = &input
	state := "awaiting_payment"
	if input.Effective == "renewal" {
		state = "scheduled"
	}
	change := BillingSubscriptionChange{ID: "change-1", Provider: input.Provider, SubscriptionID: "subscription-1", ToOfferID: input.ToOfferID, ToOfferKey: input.ToOfferKey, ToPlanKey: input.ToPlanKey, ToInterval: input.ToInterval, Effective: input.Effective, EffectiveAt: input.EffectiveAt, ProrationBehavior: input.ProrationBehavior, State: state, ProviderOperationID: "provider-operation"}
	s.openChange = &change
	copy := change
	return copy, nil
}

func (s *commerceWaveState) GetOpenBillingSubscriptionChange(context.Context, string, string) (*BillingSubscriptionChange, error) {
	if s.getChangeErr != nil {
		return nil, s.getChangeErr
	}
	if s.openChange == nil {
		return nil, nil
	}
	copy := *s.openChange
	return &copy, nil
}

func (s *commerceWaveState) UpdateBillingSubscriptionChange(_ context.Context, id string, update BillingSubscriptionChangeUpdate) error {
	if s.updateChangeErr != nil {
		return s.updateChangeErr
	}
	s.updatedChange = &update
	if s.openChange == nil || s.openChange.ID != id {
		return errors.New("change not found")
	}
	if update.State != nil {
		s.openChange.State = *update.State
	}
	if update.ProviderOperationID != nil {
		s.openChange.ProviderOperationID = *update.ProviderOperationID
	}
	if update.ErrorMessage != nil {
		s.openChange.ErrorMessage = *update.ErrorMessage
	}
	return nil
}

func (s *commerceWaveState) ListBillingInvoices(context.Context, string) ([]BillingInvoice, error) {
	if s.listInvoicesErr != nil {
		return nil, s.listInvoicesErr
	}
	return append([]BillingInvoice(nil), s.invoices...), nil
}

type commerceWaveProvider struct {
	checkoutProviderStub
	preview        PlanChangePreview
	previewErr     error
	changeErr      error
	changeRequests []ProviderPlanChangeRequest
	cancelErr      error
	cancelKeys     []string
	reactivateErr  error
	reactivateKeys []string
	scheduledErr   error
	scheduledCalls []string
	portalURL      string
	updateURL      string
	setupURL       string
	invoiceURL     string
	invoiceErr     error
}

func (p *commerceWaveProvider) PreviewPlanChange(context.Context, ProviderPlanChangeRequest) (PlanChangePreview, error) {
	return p.preview, p.previewErr
}

func (p *commerceWaveProvider) ChangePlan(_ context.Context, request ProviderPlanChangeRequest) (ProviderPlanChangeResult, error) {
	p.changeRequests = append(p.changeRequests, request)
	return ProviderPlanChangeResult{ProviderOperationID: "provider-change"}, p.changeErr
}

func (p *commerceWaveProvider) CancelSubscription(_ context.Context, _, key string) error {
	p.cancelKeys = append(p.cancelKeys, key)
	return p.cancelErr
}

func (p *commerceWaveProvider) ReactivateSubscription(_ context.Context, _, key string) error {
	p.reactivateKeys = append(p.reactivateKeys, key)
	return p.reactivateErr
}

func (p *commerceWaveProvider) CancelScheduledPlanChange(_ context.Context, _, _, key string) error {
	p.scheduledCalls = append(p.scheduledCalls, key)
	return p.scheduledErr
}

func (p *commerceWaveProvider) CreateCustomerPortalSession(context.Context, string, string) (string, error) {
	return p.portalURL, nil
}

func (p *commerceWaveProvider) CreateUpdatePaymentMethodSession(context.Context, string, string, string) (string, error) {
	return p.updateURL, nil
}

func (p *commerceWaveProvider) CreatePaymentMethodSetupSession(context.Context, string, string, string) (string, error) {
	return p.setupURL, nil
}

func (p *commerceWaveProvider) GetInvoiceURL(context.Context, string) (string, error) {
	return p.invoiceURL, p.invoiceErr
}

type commerceWavePaymentProvider struct{ checkoutProviderStub }

type commerceWavePreviewProvider struct {
	checkoutProviderStub
	preview PlanChangePreview
}

func (p *commerceWavePreviewProvider) PreviewPlanChange(context.Context, ProviderPlanChangeRequest) (PlanChangePreview, error) {
	return p.preview, nil
}

type commerceWavePlanProvider struct {
	checkoutProviderStub
	preview   PlanChangePreview
	changeErr error
}

func (p *commerceWavePlanProvider) PreviewPlanChange(context.Context, ProviderPlanChangeRequest) (PlanChangePreview, error) {
	return p.preview, nil
}

func (p *commerceWavePlanProvider) ChangePlan(context.Context, ProviderPlanChangeRequest) (ProviderPlanChangeResult, error) {
	return ProviderPlanChangeResult{}, p.changeErr
}

func newCommerceWaveService(t *testing.T, state *commerceWaveState, credits *commerceWaveCreditStore, provider PaymentProvider, config map[string]any) *CommerceService {
	t.Helper()
	registry, err := NewProviderRegistry(ProviderFactoryContext{ProviderEnvironment: ProviderEnvironmentTest}, map[string]ProviderFactory{
		"stripe": func(context.Context, ProviderFactoryContext) (PaymentProvider, error) { return provider, nil },
	})
	if err != nil {
		t.Fatal(err)
	}
	catalog := &CatalogService{store: credits}
	return &CommerceService{state: state, credits: &CreditsService{store: credits}, catalog: catalog, providers: registry, defaultProvider: "stripe", store: state}
}

func commerceWavePlanFixture(t *testing.T) (*CommerceService, *commerceWaveState, *commerceWaveProvider, string) {
	t.Helper()
	config := checkoutTestConfig(t)
	commerce := config["commerce"].(map[string]any)
	if offers, ok := commerce["offers"].(map[string]any); ok {
		if year, ok := offers["starter_year"].(map[string]any); ok {
			year["providers"] = map[string]any{"stripe": map[string]any{"type": "custom_object", "object_kind": "subscription", "external_id": "stripe-starter-year"}}
		}
	}
	credits := &commerceWaveCreditStore{active: &CatalogRevision{ID: "catalog-wave6", Version: 1, Config: config}, plan: GetUserPlanResult{PlanKey: "starter"}}
	provider := &commerceWaveProvider{checkoutProviderStub: checkoutProviderStub{name: "stripe"}, preview: PlanChangePreview{TotalAmount: MustAmount("12.340000"), SettlementAmount: MustAmount("12.340000"), Currency: "USD", EffectiveAt: time.Date(2026, 8, 20, 12, 0, 0, 0, time.UTC), NextBillingDate: timePtr(time.Date(2026, 9, 1, 12, 0, 0, 0, time.UTC)), LineItems: []PlanChangeLineItem{{ProductID: "starter-year", UnitPrice: MustAmount("12.340000"), Quantity: 1, ProrationFactor: MustAmount("1"), Currency: "usd", Tax: MustAmount("0"), Subtotal: MustAmount("12.340000")}}}, portalURL: "https://portal.example", updateURL: "https://update.example", setupURL: "https://setup.example", invoiceURL: "https://invoice.example"}
	state := &commerceWaveState{subscription: &CommerceSubscription{ID: "subscription-1", AccountID: "account-1", Provider: "stripe", ProviderSubscriptionID: "sub-1", ProviderCustomerID: "cus-1", PlanKey: "starter", Status: "active", Interval: "month"}, offers: map[string]*BillingOffer{"starter_year": {ID: "offer-year", Provider: "stripe", OfferKey: "starter_year", PlanKey: "starter", PriceID: "stripe-starter-year", Interval: "year", IntervalCnt: 1}}}
	state.customers = map[string]*BillingCustomerRecord{"stripe": {Provider: "stripe", ProviderCustomerID: "cus-1", AccountID: "account-1"}}
	service := newCommerceWaveService(t, state, credits, provider, config)
	return service, state, provider, "account-1"
}

func timePtr(value time.Time) *time.Time { return &value }

func TestCommercePlanChangeQuoteAndProviderLifecycle(t *testing.T) {
	ctx := context.Background()
	service, state, provider, accountID := commerceWavePlanFixture(t)
	preview, err := service.PreviewPlanChange(ctx, PreviewPlanChangeInput{AccountID: accountID, OfferKey: "starter_year"})
	if err != nil || preview.Preview == nil || preview.Classification != PlanChangeCadenceChange || !preview.Scheduled || preview.QuoteFingerprint == "" {
		t.Fatalf("plan preview = %+v, error = %v", preview, err)
	}
	fingerprint := preview.QuoteFingerprint
	provider.preview.TotalAmount = MustAmount("99.99")
	if _, err := service.ConfirmPlanChange(ctx, ConfirmPlanChangeInput{AccountID: accountID, OfferKey: "starter_year", QuoteFingerprint: fingerprint, OperationKey: "wave6-stale"}); err == nil {
		t.Fatal("stale plan quote accepted")
	}
	provider.preview.TotalAmount = MustAmount("12.340000")
	confirmed, err := service.ConfirmPlanChange(ctx, ConfirmPlanChangeInput{AccountID: accountID, OfferKey: "starter_year", QuoteFingerprint: fingerprint, OperationKey: "wave6-success"})
	if err != nil || !confirmed.Success || !confirmed.Pending || !confirmed.Scheduled || confirmed.EffectiveAt == nil || len(provider.changeRequests) != 1 {
		t.Fatalf("confirmed plan change = %+v, error = %v", confirmed, err)
	}
	if state.createdChange == nil || state.createdChange.Effective != "renewal" || state.createdChange.EffectiveAt != *confirmed.EffectiveAt {
		t.Fatalf("durable scheduled change = %+v, result = %+v", state.createdChange, confirmed)
	}

	state.openChange = &BillingSubscriptionChange{ID: "pending", State: "scheduled", Effective: "renewal"}
	if _, err := service.ConfirmPlanChange(ctx, ConfirmPlanChangeInput{AccountID: accountID, OfferKey: "starter_year", QuoteFingerprint: fingerprint, OperationKey: "wave6-conflict"}); err == nil {
		t.Fatal("pending plan change conflict was ignored")
	}
	state.openChange = nil
	provider.changeErr = errors.New("provider declined plan change")
	state.subscription.CancelAtPeriodEnd = true
	if _, err := service.ConfirmPlanChange(ctx, ConfirmPlanChangeInput{AccountID: accountID, OfferKey: "starter_year", QuoteFingerprint: fingerprint, OperationKey: "wave6-provider-failure"}); err == nil || len(provider.reactivateKeys) != 1 || len(provider.cancelKeys) != 1 {
		t.Fatalf("provider failure restoration: error=%v reactivate=%v cancel=%v", err, provider.reactivateKeys, provider.cancelKeys)
	}
	if state.updatedChange == nil || state.openChange == nil || state.openChange.State != "failed" {
		t.Fatalf("failed durable change = %+v update=%+v", state.openChange, state.updatedChange)
	}
}

func TestCommerceBulkCancellationScheduledCancellationPortalsAndInvoiceLinks(t *testing.T) {
	ctx := context.Background()
	service, state, provider, accountID := commerceWavePlanFixture(t)
	state.subscriptions = []CommerceSubscription{
		{ID: "z", AccountID: accountID, Provider: "stripe", ProviderSubscriptionID: "sub-z", Status: "active"},
		{ID: "a", AccountID: accountID, Provider: "stripe", ProviderSubscriptionID: "sub-a", Status: "trialing"},
		{ID: "skip", AccountID: accountID, Provider: "stripe", ProviderSubscriptionID: "sub-skip", Status: "active", CancelAtPeriodEnd: true},
		{ID: "terminal", AccountID: accountID, Provider: "stripe", ProviderSubscriptionID: "sub-terminal", Status: "canceled"},
	}
	result, err := service.CancelAllSubscriptions(ctx, accountID, "wave6-cancel-all")
	if err != nil || result.CanceledCount != 2 || len(result.Subscriptions) != 2 || provider.cancelKeys[0] == "" || provider.cancelKeys[1] == "" {
		t.Fatalf("bulk cancellation = %+v, error = %v, keys = %v", result, err, provider.cancelKeys)
	}
	provider.cancelErr = errors.New("cancel failed")
	state.subscriptions = []CommerceSubscription{{ID: "one", AccountID: accountID, Provider: "stripe", ProviderSubscriptionID: "sub-one", Status: "active"}}
	failed, err := service.CancelAllSubscriptions(ctx, accountID, "wave6-cancel-failure")
	if err == nil || failed.CanceledCount != 0 || len(failed.Subscriptions) != 1 || failed.Subscriptions[0].Error == "" {
		t.Fatalf("bulk cancellation failure = %+v, error = %v", failed, err)
	}

	state.subscription = &CommerceSubscription{AccountID: accountID, Provider: "stripe", ProviderSubscriptionID: "sub-1", Status: "active"}
	state.openChange = &BillingSubscriptionChange{ID: "scheduled-1", State: "scheduled", Effective: "renewal", ProviderOperationID: "provider-op"}
	provider.cancelErr = nil
	if err := service.CancelScheduledPlanChange(ctx, accountID, "wave6-scheduled-cancel"); err != nil || len(provider.scheduledCalls) != 1 || state.openChange.State != "canceled" {
		t.Fatalf("scheduled cancellation = %v, calls = %v, change = %+v", err, provider.scheduledCalls, state.openChange)
	}
	state.openChange = &BillingSubscriptionChange{ID: "not-scheduled", State: "awaiting_payment", Effective: "immediate"}
	if err := service.CancelScheduledPlanChange(ctx, accountID, "wave6-not-scheduled"); err == nil {
		t.Fatal("non-scheduled change accepted for cancellation")
	}

	state.subscription = &CommerceSubscription{AccountID: accountID, Provider: "stripe", ProviderSubscriptionID: "sub-1", Status: "active"}
	state.openChange = nil
	if url, err := service.CreatePortalSession(ctx, PortalSessionInput{AccountID: accountID, Purpose: PortalPurposeBilling, ReturnURL: "https://app.example/return"}); err != nil || url != provider.portalURL {
		t.Fatalf("billing portal = %q, error = %v", url, err)
	}
	if url, err := service.CreatePortalSession(ctx, PortalSessionInput{AccountID: accountID, Purpose: PortalPurposePaymentMethod, ReturnURL: "https://app.example/return"}); err != nil || url != provider.updateURL {
		t.Fatalf("payment-method portal = %q, error = %v", url, err)
	}
	state.subscription = nil
	if url, err := service.CreatePortalSession(ctx, PortalSessionInput{AccountID: accountID, Purpose: PortalPurposePaymentMethod, ReturnURL: "https://app.example/return", CancelURL: "https://app.example/cancel"}); err != nil || url != provider.setupURL {
		t.Fatalf("payment setup portal = %q, error = %v", url, err)
	}
	if _, err := service.CreatePortalSession(ctx, PortalSessionInput{AccountID: accountID, Purpose: PortalPurpose("other"), ReturnURL: "https://app.example/return"}); err == nil {
		t.Fatal("invalid portal purpose accepted")
	}

	state.invoices = []BillingInvoice{{ID: "invoice-1", Provider: "stripe", AccountID: accountID, Status: "paid"}}
	if url, err := service.GetInvoiceLink(ctx, GetInvoiceLinkInput{AccountID: accountID, Document: InvoiceDocumentLocator{Kind: "provider_invoice", Provider: "stripe", ProviderDocumentID: "invoice-1"}}); err != nil || url != provider.invoiceURL {
		t.Fatalf("provider invoice link = %q, error = %v", url, err)
	}
	entry := &LedgerEntry{EntryID: "ledger-1", AccountID: accountID, Metadata: CreditMetadata{"provider": "stripe", "provider_invoice_id": "invoice-2"}}
	service.credits.store.(*commerceWaveCreditStore).entry = entry
	if url, err := service.GetInvoiceLink(ctx, GetInvoiceLinkInput{AccountID: accountID, Document: InvoiceDocumentLocator{Kind: "ledger_entry", LedgerEntryID: entry.EntryID}}); err != nil || url != provider.invoiceURL {
		t.Fatalf("ledger invoice link = %q, error = %v", url, err)
	}
	if _, err := service.GetInvoiceLink(ctx, GetInvoiceLinkInput{AccountID: accountID, Document: InvoiceDocumentLocator{Kind: "provider_invoice", Provider: "stripe", ProviderDocumentID: "missing"}}); err == nil {
		t.Fatal("unknown provider invoice accepted")
	}
}

func TestCommercePortalAndPlanCapabilityErrors(t *testing.T) {
	ctx := context.Background()
	service, state, _, accountID := commerceWavePlanFixture(t)
	base := &commerceWavePaymentProvider{checkoutProviderStub: checkoutProviderStub{name: "stripe"}}
	service = newCommerceWaveService(t, state, service.credits.store.(*commerceWaveCreditStore), base, service.credits.store.(*commerceWaveCreditStore).active.Config)
	if _, err := service.CreatePortalSession(ctx, PortalSessionInput{AccountID: accountID, ReturnURL: "https://app.example/return"}); err == nil {
		t.Fatal("missing customer portal capability accepted")
	}
	if _, err := service.PreviewPlanChange(ctx, PreviewPlanChangeInput{AccountID: accountID, OfferKey: "starter_year"}); err == nil {
		t.Fatal("missing plan preview capability accepted")
	}
}

func TestCommerceAutoRechargeRetryAndPersistedProviderSelection(t *testing.T) {
	ctx := context.Background()
	store := &autoRechargeStoreStub{topup: &AutoRechargeTopup{ID: "topup-1"}, customer: &AutoRechargeCustomer{UserID: "user-1", Provider: "stripe", ProviderCustomerID: "cus-1"}, profile: autoRechargeActiveProfile()}
	store.profile.State = AutoRechargeStatePaused
	store.profile.Armed = false
	provider := &autoRechargeProviderStub{autoRechargeProviderBase: autoRechargeProviderBase{name: "stripe"}, methods: []PaymentMethodInfo{{ID: "pm-1", Last4: "4242", Brand: "visa", ExpiryMonth: 1, ExpiryYear: 2030, IsDefault: true}}, charge: SavedPaymentChargeResult{ProviderPaymentID: "payment-1", Status: SavedPaymentChargeProcessing}}
	autoService := autoRechargeTestService(t, store, "wave6-retry")
	credits := &commerceWaveCreditStore{balance: BalanceResult{UserID: "user-1", Balance: MustAmount("1")}}
	state := &commerceWaveState{}
	service := newCommerceWaveService(t, state, credits, provider, checkoutTestConfig(t))
	service.AutoRecharge = &CommerceAutoRecharge{commerce: service, service: autoService}
	result, err := service.AutoRecharge.Retry(ctx, AutoRechargeInput{AccountID: "user-1", ReturnURL: "https://app.example/return"})
	if err != nil || result.Outcome != AutoRechargeOutcomeSubmitted || len(provider.charges) != 1 || store.profile.State != AutoRechargeStateActive || !store.profile.Armed {
		t.Fatalf("auto-recharge retry = %+v, error = %v, charges = %v, profile = %+v", result, err, provider.charges, store.profile)
	}

	noProfile := &autoRechargeStoreStub{topup: &AutoRechargeTopup{ID: "topup-1"}, customer: &AutoRechargeCustomer{UserID: "user-2", Provider: "dodo", ProviderCustomerID: "cus-2"}}
	dodo := &autoRechargeProviderStub{autoRechargeProviderBase: autoRechargeProviderBase{name: "dodo"}}
	stripe := &autoRechargeProviderStub{autoRechargeProviderBase: autoRechargeProviderBase{name: "stripe"}}
	registry, err := NewProviderRegistry(ProviderFactoryContext{ProviderEnvironment: ProviderEnvironmentTest}, map[string]ProviderFactory{
		"dodo":   func(context.Context, ProviderFactoryContext) (PaymentProvider, error) { return dodo, nil },
		"stripe": func(context.Context, ProviderFactoryContext) (PaymentProvider, error) { return stripe, nil },
	})
	if err != nil {
		t.Fatal(err)
	}
	autoService = autoRechargeTestService(t, noProfile, "wave6-selection")
	selection := &CommerceService{providers: registry, state: &commerceWaveState{checkoutStoreStub: checkoutStoreStub{customers: map[string]*BillingCustomerRecord{"dodo": {Provider: "dodo", ProviderCustomerID: "cus-2", AccountID: "user-2"}}}}, catalog: &CatalogService{store: &commerceWaveCreditStore{active: &CatalogRevision{Config: checkoutTestConfig(t)}}}}
	selected, err := selection.autoRechargeProvider(ctx, "user-2", autoService)
	if err != nil || selected.Name() != "dodo" {
		t.Fatalf("persisted customer provider = %v, error = %v", selected, err)
	}
}

func TestCommerceAutoRechargeRetryProviderMismatchIsRejected(t *testing.T) {
	store := &autoRechargeStoreStub{topup: &AutoRechargeTopup{ID: "topup-1"}, profile: autoRechargeActiveProfile()}
	service := autoRechargeTestService(t, store, "wave6-mismatch")
	provider := &autoRechargeProviderStub{autoRechargeProviderBase: autoRechargeProviderBase{name: "dodo"}}
	if _, err := service.Retry(context.Background(), provider, AutoRechargeProcessInput{UserID: "user-1", Balance: MustAmount("0")}); err == nil || !strings.Contains(err.Error(), "provider") {
		t.Fatalf("provider mismatch error = %v", err)
	}
}
