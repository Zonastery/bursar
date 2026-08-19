// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import (
	"context"
	"errors"
	"testing"
	"time"
)

func newCommerceWaveConstructedService(t *testing.T, state *commerceWaveState, credits *commerceWaveCreditStore, provider PaymentProvider) *CommerceService {
	t.Helper()
	registry, err := NewProviderRegistry(ProviderFactoryContext{ProviderEnvironment: ProviderEnvironmentTest}, map[string]ProviderFactory{
		"stripe": func(context.Context, ProviderFactoryContext) (PaymentProvider, error) { return provider, nil },
	})
	if err != nil {
		t.Fatal(err)
	}
	billing, err := NewBillingService(&checkoutBillingStoreStub{})
	if err != nil {
		t.Fatal(err)
	}
	catalog, err := NewCatalogService(credits)
	if err != nil {
		t.Fatal(err)
	}
	service, err := NewCommerceService(billing, catalog, &CreditsService{store: credits}, CommerceOptions{
		Store: state, StateStore: state, Providers: registry, DefaultProvider: "stripe",
	})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(service.Close)
	return service
}

func TestCommerceManagementMissingStateAndCapabilityBranches(t *testing.T) {
	ctx := context.Background()
	service, state, provider, accountID := commerceWavePlanFixture(t)
	state.getSubscriptionErr = errors.New("subscription read failed")
	if _, err := service.PreviewPlanChange(ctx, PreviewPlanChangeInput{AccountID: accountID, OfferKey: "starter_year"}); err == nil {
		t.Fatal("subscription read failure was ignored during preview")
	}
	if err := service.CancelScheduledPlanChange(ctx, accountID, "wave7-read"); err == nil {
		t.Fatal("subscription read failure was ignored during cancellation")
	}
	state.getSubscriptionErr = nil
	state.listSubscriptionsErr = errors.New("subscription list failed")
	if _, err := service.CancelAllSubscriptions(ctx, accountID, "wave7-list"); err == nil {
		t.Fatal("subscription list failure was ignored")
	}
	state.listSubscriptionsErr = nil
	state.getCustomerErr = errors.New("customer read failed")
	if _, err := service.CreatePortalSession(ctx, PortalSessionInput{AccountID: accountID, ReturnURL: "https://app.example/return"}); err == nil {
		t.Fatal("customer read failure was ignored")
	}
	state.getCustomerErr = nil

	state.subscription = nil
	if _, err := service.PreviewPlanChange(ctx, PreviewPlanChangeInput{AccountID: accountID, OfferKey: "starter_year"}); err == nil {
		t.Fatal("preview without a subscription was accepted")
	}
	if err := service.CancelScheduledPlanChange(ctx, accountID, "wave7-no-subscription"); err == nil {
		t.Fatal("scheduled cancellation without a subscription was accepted")
	}
	state.subscription = &CommerceSubscription{AccountID: accountID, Provider: "stripe", ProviderSubscriptionID: "sub-1", Status: "active"}
	state.resolveOfferErr = errors.New("offer lookup failed")
	if _, err := service.PreviewPlanChange(ctx, PreviewPlanChangeInput{AccountID: accountID, OfferKey: "starter_year"}); err == nil {
		t.Fatal("offer lookup failure was ignored")
	}
	state.resolveOfferErr = nil

	state.openChange = &BillingSubscriptionChange{ID: "scheduled", State: "scheduled", Effective: "renewal", ProviderOperationID: "op"}
	state.getChangeErr = errors.New("change read failed")
	if err := service.CancelScheduledPlanChange(ctx, accountID, "wave7-change-read"); err == nil {
		t.Fatal("change read failure was ignored")
	}
	state.getChangeErr = nil
	state.updateChangeErr = errors.New("change update failed")
	if err := service.CancelScheduledPlanChange(ctx, accountID, "wave7-change-update"); err == nil || len(provider.scheduledCalls) != 1 {
		t.Fatalf("change update failure = %v, scheduled calls = %v", err, provider.scheduledCalls)
	}
	state.updateChangeErr = nil
	provider.scheduledErr = errors.New("provider cancellation failed")
	if err := service.CancelScheduledPlanChange(ctx, accountID, "wave7-provider"); err == nil {
		t.Fatal("provider scheduled cancellation failure was ignored")
	}

	state.openChange = nil
	provider.invoiceURL = ""
	state.invoices = []BillingInvoice{{ID: "invoice-1", Provider: "stripe", AccountID: accountID, Status: "paid"}}
	if _, err := service.GetInvoiceLink(ctx, GetInvoiceLinkInput{AccountID: accountID, Document: InvoiceDocumentLocator{Kind: "provider_invoice", Provider: "stripe", ProviderDocumentID: "invoice-1"}}); err == nil {
		t.Fatal("empty provider invoice URL accepted")
	}
}

func TestCommerceCheckoutQuantityAndProviderSelectionErrors(t *testing.T) {
	ctx := context.Background()
	service, state, provider, accountID := commerceWavePlanFixture(t)
	state.subscription = nil
	state.customers = nil
	service = newCommerceWaveConstructedService(t, state, service.credits.store.(*commerceWaveCreditStore), provider)
	base := CreateCheckoutInput{SubjectID: "subject-1", AccountID: accountID, OfferKey: "starter_month", Type: "subscription", SuccessURL: "https://app.example/{intentId}", CancelURL: "https://app.example/cancel/{intentId}", IdempotencyKey: "wave7-checkout"}
	for name, input := range map[string]CreateCheckoutInput{
		"missing success URL":   func() CreateCheckoutInput { copy := base; copy.SuccessURL = ""; return copy }(),
		"unknown offer":         func() CreateCheckoutInput { copy := base; copy.OfferKey = "missing"; return copy }(),
		"invalid type":          func() CreateCheckoutInput { copy := base; copy.Type = "other"; return copy }(),
		"type mismatch":         func() CreateCheckoutInput { copy := base; copy.Type = "credit_pack"; return copy }(),
		"subscription quantity": func() CreateCheckoutInput { copy := base; quantity := int64(2); copy.Quantity = &quantity; return copy }(),
		"unknown provider":      func() CreateCheckoutInput { copy := base; copy.Provider = "missing"; return copy }(),
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := service.CreateCheckout(ctx, input); err == nil {
				t.Fatalf("input accepted: %+v", input)
			}
		})
	}
	created, err := service.CreateCheckout(ctx, base)
	if err != nil || created.Intent.ID == "" || created.Session.ID == "" || provider.Name() != "stripe" {
		t.Fatalf("valid checkout = %+v, error = %v", created, err)
	}
	if selected, err := service.ProviderForAccount(ctx, accountID, "starter_month"); err != nil || selected.Name() != "stripe" {
		t.Fatalf("provider selection = %v, error = %v", selected, err)
	}
	if _, err := service.ProviderForAccount(ctx, accountID, "missing"); err == nil {
		t.Fatal("provider selection accepted unknown offer")
	}
}

func TestCommerceAutoRechargeWrapperEnableAndBalanceErrors(t *testing.T) {
	ctx := context.Background()
	store := &autoRechargeStoreStub{
		topup:    &AutoRechargeTopup{ID: "topup-1"},
		customer: &AutoRechargeCustomer{UserID: "user-1", Provider: "stripe", ProviderCustomerID: "cus-1"},
		profile:  autoRechargeActiveProfile(),
	}
	provider := &autoRechargeProviderStub{autoRechargeProviderBase: autoRechargeProviderBase{name: "stripe"}, methods: []PaymentMethodInfo{{ID: "pm-1", Last4: "4242", Brand: "visa", ExpiryMonth: 1, ExpiryYear: 2030, IsDefault: true}}, charge: SavedPaymentChargeResult{ProviderPaymentID: "payment-1", Status: SavedPaymentChargeProcessing}}
	autoService := autoRechargeTestService(t, store, "wave7-enable")
	credits := &commerceWaveCreditStore{balance: BalanceResult{UserID: "user-1", Balance: MustAmount("1")}}
	state := &commerceWaveState{}
	service := newCommerceWaveService(t, state, credits, provider, checkoutTestConfig(t))
	service.AutoRecharge = &CommerceAutoRecharge{commerce: service, service: autoService}
	status, err := service.AutoRecharge.Enable(ctx, AutoRechargeInput{AccountID: "user-1", ReturnURL: "https://app.example/return"})
	if err != nil || status == nil || !status.Enabled || len(provider.charges) != 1 {
		t.Fatalf("auto-recharge enable = %+v, error = %v, charges = %v", status, err, provider.charges)
	}
	credits.balanceErr = errors.New("balance unavailable")
	if _, err := service.AutoRecharge.ProcessIfNeeded(ctx, AutoRechargeInput{AccountID: "user-1"}); err == nil {
		t.Fatal("balance read failure was ignored")
	}
	store.profile.Enabled = false
	credits.balanceErr = nil
	if _, err := service.AutoRecharge.Retry(ctx, AutoRechargeInput{AccountID: "user-1"}); err == nil {
		t.Fatal("retry accepted disabled profile")
	}
}

func TestCommerceAutoRechargeProviderFallbackAndInvalidCatalog(t *testing.T) {
	ctx := context.Background()
	provider := &autoRechargeProviderStub{autoRechargeProviderBase: autoRechargeProviderBase{name: "stripe"}}
	store := &autoRechargeStoreStub{topup: &AutoRechargeTopup{ID: "topup-1"}}
	autoService := autoRechargeTestService(t, store, "wave7-fallback")
	credits := &commerceWaveCreditStore{active: &CatalogRevision{Config: checkoutTestConfig(t)}}
	state := &commerceWaveState{}
	service := newCommerceWaveService(t, state, credits, provider, checkoutTestConfig(t))
	if _, err := service.autoRechargeProvider(ctx, "user-1", autoService); err == nil {
		t.Fatal("catalog provider fallback accepted a catalog without auto-recharge policy")
	}
}

func TestCommercePlanChangeImmediateAndCapabilityBranches(t *testing.T) {
	ctx := context.Background()
	service, state, provider, accountID := commerceWavePlanFixture(t)
	config := service.credits.store.(*commerceWaveCreditStore).active.Config
	commerce := config["commerce"].(map[string]any)
	offers := commerce["offers"].(map[string]any)
	pro := offers["pro_month"].(map[string]any)
	pro["providers"] = map[string]any{"stripe": map[string]any{"type": "custom_object", "object_kind": "subscription", "external_id": "stripe-pro-month"}}
	state.offers["pro_month"] = &BillingOffer{ID: "offer-pro", Provider: "stripe", OfferKey: "pro_month", PlanKey: "pro", PriceID: "stripe-pro-month", Interval: "month", IntervalCnt: 1}
	state.subscription.Interval = "month"
	preview, err := service.PreviewPlanChange(ctx, PreviewPlanChangeInput{AccountID: accountID, OfferKey: "pro_month"})
	if err != nil || preview.Classification != PlanChangeUpgrade || preview.Scheduled || preview.Preview == nil {
		t.Fatalf("immediate preview = %+v, error = %v", preview, err)
	}
	confirmed, err := service.ConfirmPlanChange(ctx, ConfirmPlanChangeInput{AccountID: accountID, OfferKey: "pro_month", QuoteFingerprint: preview.QuoteFingerprint, OperationKey: "wave7-immediate"})
	if err != nil || !confirmed.Success || confirmed.Scheduled || confirmed.EffectiveAt == nil || state.createdChange == nil || state.createdChange.Effective != "immediate" {
		t.Fatalf("immediate confirmation = %+v, change = %+v, error = %v", confirmed, state.createdChange, err)
	}

	state.openChange = nil
	provider.preview.NextBillingDate = nil
	provider.preview.TotalAmount = MustAmount("12.340000")
	rescheduled, err := service.PreviewPlanChange(ctx, PreviewPlanChangeInput{AccountID: accountID, OfferKey: "starter_year"})
	if err != nil {
		t.Fatalf("scheduled preview after immediate change: %v", err)
	}
	if _, err := service.ConfirmPlanChange(ctx, ConfirmPlanChangeInput{AccountID: accountID, OfferKey: "starter_year", QuoteFingerprint: rescheduled.QuoteFingerprint, OperationKey: "wave7-missing-date"}); err == nil {
		t.Fatal("scheduled confirmation without a billing date accepted")
	}
	state.openChange = nil
	state.createChangeErr = errors.New("durable change write failed")
	provider.preview.NextBillingDate = timePtr(time.Date(2026, 9, 1, 12, 0, 0, 0, time.UTC))
	if _, err := service.ConfirmPlanChange(ctx, ConfirmPlanChangeInput{AccountID: accountID, OfferKey: "starter_year", QuoteFingerprint: rescheduled.QuoteFingerprint, OperationKey: "wave7-create-error"}); err == nil {
		t.Fatal("durable change write failure was ignored")
	}

	state.createChangeErr = nil
	state.openChange = nil
	previewOnly := &commerceWavePreviewProvider{checkoutProviderStub: checkoutProviderStub{name: "stripe"}, preview: provider.preview}
	previewService := newCommerceWaveService(t, state, service.credits.store.(*commerceWaveCreditStore), previewOnly, config)
	if _, err := previewService.ConfirmPlanChange(ctx, ConfirmPlanChangeInput{AccountID: accountID, OfferKey: "starter_year", QuoteFingerprint: rescheduled.QuoteFingerprint, OperationKey: "wave7-no-change-capability"}); err == nil {
		t.Fatal("provider without ChangePlan capability accepted confirmation")
	}

	state.openChange = nil
	state.subscription.CancelAtPeriodEnd = true
	planOnly := &commerceWavePlanProvider{checkoutProviderStub: checkoutProviderStub{name: "stripe"}, preview: provider.preview}
	planService := newCommerceWaveService(t, state, service.credits.store.(*commerceWaveCreditStore), planOnly, config)
	if _, err := planService.ConfirmPlanChange(ctx, ConfirmPlanChangeInput{AccountID: accountID, OfferKey: "starter_year", QuoteFingerprint: rescheduled.QuoteFingerprint, OperationKey: "wave7-no-reactivation"}); err == nil {
		t.Fatal("provider without reactivation capability accepted canceled subscription")
	}
}

func TestCommerceSubscriptionCommandProviderCapabilities(t *testing.T) {
	ctx := context.Background()
	service, state, _, accountID := commerceWavePlanFixture(t)
	state.subscriptions = []CommerceSubscription{*state.subscription}
	base := &commerceWavePaymentProvider{checkoutProviderStub: checkoutProviderStub{name: "stripe"}}
	baseService := newCommerceWaveService(t, state, service.credits.store.(*commerceWaveCreditStore), base, service.credits.store.(*commerceWaveCreditStore).active.Config)
	if _, err := baseService.CancelSubscription(ctx, SubscriptionCommandInput{AccountID: accountID, SubscriptionID: "sub-1", OperationKey: "wave7-cancel-capability"}); err == nil {
		t.Fatal("provider without cancellation capability accepted command")
	}
	state.subscription.CancelAtPeriodEnd = true
	state.subscriptions[0].CancelAtPeriodEnd = true
	if _, err := baseService.ReactivateSubscription(ctx, SubscriptionCommandInput{AccountID: accountID, SubscriptionID: "sub-1", OperationKey: "wave7-reactivate-capability"}); err == nil {
		t.Fatal("provider without reactivation capability accepted command")
	}
	provider := &commerceWaveProvider{checkoutProviderStub: checkoutProviderStub{name: "stripe"}, cancelErr: errors.New("cancel provider failed"), reactivateErr: errors.New("reactivate provider failed")}
	failureService := newCommerceWaveService(t, state, service.credits.store.(*commerceWaveCreditStore), provider, service.credits.store.(*commerceWaveCreditStore).active.Config)
	state.subscription.CancelAtPeriodEnd = false
	state.subscriptions[0].CancelAtPeriodEnd = false
	if _, err := failureService.CancelSubscription(ctx, SubscriptionCommandInput{AccountID: accountID, SubscriptionID: "sub-1", OperationKey: "wave7-cancel-provider"}); err == nil {
		t.Fatal("provider cancellation failure was ignored")
	}
	state.subscription.CancelAtPeriodEnd = true
	state.subscriptions[0].CancelAtPeriodEnd = true
	if _, err := failureService.ReactivateSubscription(ctx, SubscriptionCommandInput{AccountID: accountID, SubscriptionID: "sub-1", OperationKey: "wave7-reactivate-provider"}); err == nil {
		t.Fatal("provider reactivation failure was ignored")
	}
}
