// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import (
	"context"
	"errors"
	"os"
	"strconv"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
)

// financialProvider is deliberately a provider-boundary fake: all financial
// state below is persisted by Postgres and all provider calls remain visible
// for idempotency assertions.
type financialProvider struct {
	name          string
	checkoutCalls []CheckoutSessionRequest
	checkoutState string
	methods       []PaymentMethodInfo
	chargeStatus  SavedPaymentChargeStatus
	chargeCalls   []SavedPaymentChargeParams
	paymentID     string
}

func (p *financialProvider) Name() string { return p.name }

func (p *financialProvider) CreateCheckoutSession(_ context.Context, request CheckoutSessionRequest) (CheckoutSession, error) {
	p.checkoutCalls = append(p.checkoutCalls, request)
	return CheckoutSession{
		ID:         "session-" + request.IdempotencyKey,
		URL:        "https://checkout.example/" + request.IdempotencyKey,
		CustomerID: "cus-" + request.AccountID,
	}, nil
}

func (p *financialProvider) HandleWebhook(context.Context, WebhookRequest) (WebhookResult, error) {
	return WebhookResult{}, errors.New("financial integration test does not use provider webhook normalization")
}

func (p *financialProvider) GetCheckoutSessionStatus(context.Context, string) (string, error) {
	if p.checkoutState == "" {
		return "open", nil
	}
	return p.checkoutState, nil
}

func (p *financialProvider) ListPaymentMethods(context.Context, string) ([]PaymentMethodInfo, error) {
	return p.methods, nil
}

func (p *financialProvider) PreviewSavedPaymentCharge(context.Context, SavedPaymentChargeParams) (SavedPaymentChargeQuote, error) {
	return SavedPaymentChargeQuote{AmountMinor: 500, Currency: "USD"}, nil
}

func (p *financialProvider) ChargeSavedPaymentMethod(_ context.Context, params SavedPaymentChargeParams) (SavedPaymentChargeResult, error) {
	p.chargeCalls = append(p.chargeCalls, params)
	status := p.chargeStatus
	if status == "" {
		status = SavedPaymentChargeProcessing
	}
	paymentID := p.paymentID
	if paymentID == "" {
		paymentID = "auto-" + params.IdempotencyKey
	}
	return SavedPaymentChargeResult{ProviderPaymentID: paymentID, Status: status, AmountMinor: int64Pointer(500), Currency: "USD"}, nil
}

func financialCatalogConfig(t *testing.T) map[string]any {
	t.Helper()
	config := checkoutTestConfig(t)
	commerce := config["commerce"].(map[string]any)
	commerce["providers"].(map[string]any)["alpha"] = map[string]any{"type": "stripe"}
	offers := commerce["offers"].(map[string]any)
	proMonth := offers["pro_month"].(map[string]any)
	proMonth["providers"] = map[string]any{
		"alpha": map[string]any{
			"type":     "stripe_price",
			"price_id": "alpha-pro-month",
		},
	}
	offers = map[string]any{"pro_month": proMonth}
	commerce["offers"] = offers
	offers["credit_pack"] = map[string]any{
		"type":             "topup",
		"display_name":     "Precise credit pack",
		"credits_per_unit": "1234.567891",
		"bucket":           "general",
		"lot_behavior":     "separate_lots",
		"quantity":         map[string]any{"minimum": 1, "maximum": 10, "default": 1},
		"price":            map[string]any{"amount_minor": 500, "currency": "USD"},
		"providers": map[string]any{
			"alpha": map[string]any{
				"type":     "stripe_price",
				"price_id": "alpha-credit-pack",
			},
		},
	}
	commerce["auto_recharge"] = map[string]any{
		"eligible_topups": []string{"credit_pack"},
		"balance_below":   map[string]any{"minimum": "100", "maximum": "5000", "default": "1000"},
		"rearm_above":     "6000",
		"quantity":        map[string]any{"minimum": 1, "maximum": 3, "default": 1},
		"limits": map[string]any{
			"max_purchases":    3,
			"window":           map[string]any{"type": "rolling", "duration": map[string]any{"unit": "day", "count": 30}},
			"max_charge_minor": 500,
			"cooldown":         map[string]any{"unit": "hour", "count": 1},
		},
	}
	return config
}

func newFinancialSDK(t *testing.T, ctx context.Context, config postgresIntegrationConfig, provider *financialProvider) (*Bursar, *PostgresStore) {
	t.Helper()
	store := openPostgresIntegrationStore(t, ctx, config, config.tenantID)
	t.Cleanup(func() { _ = store.Close() })
	registry, err := NewProviderRegistry(
		ProviderFactoryContext{TenantID: config.tenantID, ProviderEnvironment: config.providerEnvironment},
		map[string]ProviderFactory{
			provider.name: func(context.Context, ProviderFactoryContext) (PaymentProvider, error) {
				return provider, nil
			},
		},
	)
	if err != nil {
		t.Fatalf("construct provider registry: %v", err)
	}
	sdk, err := New(Options{
		CreditStore:  store,
		BillingStore: store,
		CommerceOptions: &CommerceOptions{
			Providers:       registry,
			DefaultProvider: provider.name,
		},
	})
	if err != nil {
		t.Fatalf("construct financial facade: %v", err)
	}
	t.Cleanup(func() { _ = sdk.Close() })
	previous, err := sdk.Catalog.GetActive(ctx)
	if err != nil || previous == nil {
		t.Fatalf("read previous active catalog: %+v, error = %v", previous, err)
	}
	t.Cleanup(func() {
		restoreCtx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
		defer cancel()
		if _, restoreErr := sdk.Catalog.PublishAndActivate(restoreCtx, previous.Config, "go-financial-integration-restore", newAssignmentsRollout(previous.Config)); restoreErr != nil {
			t.Errorf("restore active catalog: %v", restoreErr)
		}
	})
	catalog := financialCatalogConfig(t)
	if _, err := sdk.Catalog.PublishAndActivate(ctx, catalog, "go-financial-integration", newAssignmentsRollout(catalog)); err != nil {
		if typed, ok := AsBursarError(err); ok && typed.Unwrap() != nil {
			t.Fatalf("publish financial catalog: %v: %v", err, typed.Unwrap())
		}
		t.Fatalf("publish financial catalog: %v", err)
	}
	return sdk, store
}

func newAssignmentsRollout(catalog map[string]any) CatalogRollout {
	rollout := CatalogRollout{Plans: make(map[string]PlanRollout)}
	if plans, ok := catalog["plans"].(map[string]any); ok {
		for planKey := range plans {
			rollout.Plans[planKey] = PlanRollout{Effective: "new_assignments_only"}
		}
	}
	return rollout
}

func financialContext(t *testing.T) (context.Context, context.CancelFunc, postgresIntegrationConfig) {
	t.Helper()
	config := requirePostgresIntegration(t)
	ctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
	return ctx, cancel, config
}

func TestPostgresCheckoutTopupRefundFinancialInvariants(t *testing.T) {
	ctx, cancel, config := financialContext(t)
	defer cancel()
	provider := &financialProvider{name: "alpha"}
	sdk, _ := newFinancialSDK(t, ctx, config, provider)
	accountID := uuid.NewString()
	runID := uuid.NewString()
	checkoutKey := "go-financial:" + runID + ":checkout"
	paymentID := "go-financial:" + runID + ":payment"
	resolvedTopup, err := sdk.Billing.ResolveTopup(ctx, "alpha", "", "alpha-credit-pack")
	if err != nil || resolvedTopup == nil {
		t.Fatalf("resolve published top-up = %+v, error = %v", resolvedTopup, err)
	}

	checkout, err := sdk.Commerce.CreateCheckout(ctx, CreateCheckoutInput{
		SubjectID:      accountID,
		AccountID:      accountID,
		OfferKey:       "credit_pack",
		Type:           "credit_pack",
		Quantity:       int64Pointer(2),
		SuccessURL:     "https://app.example/success/{intentId}",
		CancelURL:      "https://app.example/cancel/{intentId}",
		IdempotencyKey: checkoutKey,
	})
	if err != nil {
		t.Fatalf("create top-up checkout: %v", err)
	}
	if checkout.Intent.ID == "" || checkout.Session.ID == "" || len(provider.checkoutCalls) != 1 {
		t.Fatalf("checkout = %+v, calls = %d", checkout, len(provider.checkoutCalls))
	}
	if checkout.Session.URL != "https://checkout.example/"+checkoutKey {
		t.Fatalf("checkout URL = %q", checkout.Session.URL)
	}
	if checkout.Session.CustomerID != "cus-"+accountID {
		t.Fatalf("provider customer ID = %q", checkout.Session.CustomerID)
	}

	payment := BillingEvent{
		EventID:    "evt-" + runID + "-topup",
		Provider:   "alpha",
		Type:       BillingEventPaymentSucceeded,
		OccurredAt: time.Now().UTC(),
		AccountID:  accountID,
		Metadata:   map[string]any{"checkout_intent_id": checkout.Intent.ID},
		Payment: &BillingPayment{
			ProviderPaymentID: paymentID,
			AmountMinor:       1000,
			Currency:          "USD",
			Purpose:           "credit_topup",
			Status:            "succeeded",
			Refs:              &ProviderRef{PriceID: "alpha-credit-pack"},
		},
	}
	result, err := sdk.IngestBillingEvent(ctx, payment)
	if err != nil || !result.Handled || result.Action != "payment_succeeded" {
		t.Fatalf("top-up payment result = %+v, error = %v", result, err)
	}
	replay, err := sdk.IngestBillingEvent(ctx, payment)
	if err != nil || !replay.Handled || !replay.Duplicate {
		t.Fatalf("top-up replay = %+v, error = %v", replay, err)
	}

	balance, err := sdk.Credits.GetBalance(ctx, accountID)
	if err != nil {
		t.Fatalf("read top-up balance: %v", err)
	}
	if !balance.Balance.Equal(MustAmount("2469.135782")) {
		t.Fatalf("top-up balance = %s, want 2469.135782", balance.Balance)
	}
	status, err := sdk.Commerce.GetCheckoutStatus(ctx, checkout.Intent.ID, accountID)
	if err != nil || status.Status != CheckoutStatusSucceeded {
		t.Fatalf("checkout status = %+v, error = %v", status, err)
	}

	_, err = sdk.Commerce.CreateCheckout(ctx, CreateCheckoutInput{
		SubjectID:      accountID,
		AccountID:      accountID,
		OfferKey:       "credit_pack",
		Type:           "credit_pack",
		Quantity:       int64Pointer(3),
		SuccessURL:     "https://app.example/success/{intentId}",
		CancelURL:      "https://app.example/cancel/{intentId}",
		IdempotencyKey: checkoutKey,
	})
	if err == nil {
		t.Fatal("same checkout idempotency key accepted a different request")
	}
	if len(provider.checkoutCalls) != 1 {
		t.Fatalf("provider calls after checkout conflict = %d, want 1", len(provider.checkoutCalls))
	}

	refundEvent := func(eventID, refundID string, amount int64) BillingEvent {
		return BillingEvent{
			EventID:    eventID,
			Provider:   "alpha",
			Type:       BillingEventRefundCreated,
			OccurredAt: time.Now().UTC(),
			AccountID:  accountID,
			Refund: &BillingRefund{
				ProviderRefundID:  refundID,
				ProviderPaymentID: paymentID,
				AmountMinor:       amount,
				Currency:          "USD",
				Status:            "succeeded",
			},
		}
	}
	firstRefund, err := sdk.IngestBillingEvent(ctx, refundEvent("evt-"+runID+"-refund-1", "refund-"+runID+"-1", 123))
	if err != nil || !firstRefund.Handled || firstRefund.Action != "refund_clawback" {
		t.Fatalf("first refund = %+v, error = %v", firstRefund, err)
	}
	balance, err = sdk.Credits.GetBalance(ctx, accountID)
	if err != nil {
		t.Fatalf("read balance after first refund: %v", err)
	}
	if !balance.Balance.Equal(MustAmount("2165.432081")) {
		t.Fatalf("balance after first refund = %s, want 2165.432081", balance.Balance)
	}

	refundReplay, err := sdk.IngestBillingEvent(ctx, refundEvent("evt-"+runID+"-refund-1-replay", "refund-"+runID+"-1", 123))
	if err != nil || !refundReplay.Handled || refundReplay.Action != "refund_clawback" {
		t.Fatalf("refund identity replay = %+v, error = %v", refundReplay, err)
	}
	balance, err = sdk.Credits.GetBalance(ctx, accountID)
	if err != nil {
		t.Fatalf("read balance after refund replay: %v", err)
	}
	if !balance.Balance.Equal(MustAmount("2165.432081")) {
		t.Fatalf("balance after refund replay = %s, want 2165.432081", balance.Balance)
	}

	secondRefund, err := sdk.IngestBillingEvent(ctx, refundEvent("evt-"+runID+"-refund-2", "refund-"+runID+"-2", 456))
	if err != nil || !secondRefund.Handled || secondRefund.Action != "refund_clawback" {
		t.Fatalf("second refund = %+v, error = %v", secondRefund, err)
	}
	balance, err = sdk.Credits.GetBalance(ctx, accountID)
	if err != nil {
		t.Fatalf("read balance after second refund: %v", err)
	}
	if !balance.Balance.Equal(MustAmount("1039.506164")) {
		t.Fatalf("balance after second refund = %s, want 1039.506164", balance.Balance)
	}

	if _, err := sdk.IngestBillingEvent(ctx, refundEvent("evt-"+runID+"-refund-over", "refund-"+runID+"-over", 500)); err == nil {
		t.Fatal("over-refund was accepted")
	}
	balance, err = sdk.Credits.GetBalance(ctx, accountID)
	if err != nil {
		t.Fatalf("read balance after over-refund rejection: %v", err)
	}
	if !balance.Balance.Equal(MustAmount("1039.506164")) {
		t.Fatalf("balance changed after over-refund rejection = %s", balance.Balance)
	}
}

func TestPostgresSubscriptionLifecycleAndAccountProjection(t *testing.T) {
	ctx, cancel, config := financialContext(t)
	defer cancel()
	provider := &financialProvider{name: "alpha", methods: []PaymentMethodInfo{{ID: "pm-1", Brand: "visa", Last4: "4242", ExpiryMonth: 12, ExpiryYear: 2030, IsDefault: true}}}
	sdk, _ := newFinancialSDK(t, ctx, config, provider)
	accountID := uuid.NewString()
	runID := uuid.NewString()
	subscriptionID := "sub-" + runID
	invoiceID := "inv-" + runID
	now := time.Now().UTC().Truncate(time.Second)
	periodEnd := now.Add(30 * 24 * time.Hour)

	customerEvent := BillingEvent{
		EventID:    "evt-" + runID + "-customer",
		Provider:   "alpha",
		Type:       BillingEventCustomerCreated,
		OccurredAt: now,
		AccountID:  accountID,
		Customer:   &BillingCustomer{ProviderCustomerID: "cus-" + runID, Email: "buyer@example.com"},
	}
	if result, err := sdk.IngestBillingEvent(ctx, customerEvent); err != nil || !result.Handled {
		t.Fatalf("customer event = %+v, error = %v", result, err)
	}

	subscription := &BillingSubscription{
		ProviderSubscriptionID: subscriptionID,
		Status:                 "active",
		Interval:               "month",
		IntervalCount:          1,
		PeriodStart:            &now,
		PeriodEnd:              &periodEnd,
		Refs:                   &ProviderRef{PriceID: "alpha-pro-month"},
	}
	created, err := sdk.IngestBillingEvent(ctx, BillingEvent{
		EventID:      "evt-" + runID + "-subscription-created",
		Provider:     "alpha",
		Type:         BillingEventSubscriptionCreated,
		OccurredAt:   now,
		AccountID:    accountID,
		Customer:     customerEvent.Customer,
		Subscription: subscription,
	})
	if err != nil || !created.Handled || created.Action != "subscription_created" {
		t.Fatalf("subscription create = %+v, error = %v", created, err)
	}
	plan, err := sdk.Credits.GetUserPlan(ctx, accountID)
	if err != nil || plan.PlanKey != "pro" {
		t.Fatalf("assigned plan = %+v, error = %v", plan, err)
	}
	active, err := sdk.Billing.GetActiveSubscription(ctx, accountID)
	if err != nil || active == nil || active.ProviderSubscriptionID != subscriptionID {
		t.Fatalf("active subscription = %+v, error = %v", active, err)
	}

	duplicate, err := sdk.IngestBillingEvent(ctx, BillingEvent{
		EventID:      "evt-" + runID + "-subscription-created",
		Provider:     "alpha",
		Type:         BillingEventSubscriptionCreated,
		OccurredAt:   now,
		AccountID:    accountID,
		Customer:     customerEvent.Customer,
		Subscription: subscription,
	})
	if err != nil || !duplicate.Handled || !duplicate.Duplicate {
		t.Fatalf("subscription duplicate = %+v, error = %v", duplicate, err)
	}

	paused := *subscription
	paused.Refs = nil
	paused.Status = ""
	pausedResult, err := sdk.IngestBillingEvent(ctx, BillingEvent{
		EventID:      "evt-" + runID + "-subscription-paused",
		Provider:     "alpha",
		Type:         BillingEventSubscriptionPaused,
		OccurredAt:   now.Add(time.Minute),
		AccountID:    accountID,
		Subscription: &paused,
	})
	if err != nil || !pausedResult.Handled {
		t.Fatalf("subscription pause = %+v, error = %v", pausedResult, err)
	}
	if got, err := sdk.Credits.GetUserPlan(ctx, accountID); err != nil || got.PlanKey != "" {
		t.Fatalf("plan after pause = %+v, error = %v", got, err)
	}

	resumed := paused
	resumed.Refs = nil
	resumedResult, err := sdk.IngestBillingEvent(ctx, BillingEvent{
		EventID:      "evt-" + runID + "-subscription-resumed",
		Provider:     "alpha",
		Type:         BillingEventSubscriptionResumed,
		OccurredAt:   now.Add(2 * time.Minute),
		AccountID:    accountID,
		Subscription: &resumed,
	})
	if err != nil || !resumedResult.Handled {
		t.Fatalf("subscription resume = %+v, error = %v", resumedResult, err)
	}
	if got, err := sdk.Credits.GetUserPlan(ctx, accountID); err != nil || got.PlanKey != "pro" {
		t.Fatalf("plan after resume = %+v, error = %v", got, err)
	}

	invoiceResult, err := sdk.IngestBillingEvent(ctx, BillingEvent{
		EventID:      "evt-" + runID + "-invoice-paid",
		Provider:     "alpha",
		Type:         BillingEventInvoicePaid,
		OccurredAt:   now.Add(3 * time.Minute),
		AccountID:    accountID,
		Subscription: &resumed,
		Invoice: &BillingInvoice{
			ProviderInvoiceID: invoiceID,
			Status:            "paid",
			Currency:          "USD",
			AmountPaidMinor:   2000,
			AmountDueMinor:    2000,
			PeriodStart:       &now,
			PeriodEnd:         &periodEnd,
		},
	})
	if err != nil || !invoiceResult.Handled {
		t.Fatalf("invoice event = %+v, error = %v", invoiceResult, err)
	}
	invoices, err := sdk.Billing.ListBillingInvoices(ctx, accountID)
	if err != nil || len(invoices) != 1 || invoices[0].canonicalProviderInvoiceID() != invoiceID || invoices[0].Status != "paid" {
		t.Fatalf("persisted invoices = %+v, error = %v", invoices, err)
	}

	overview, err := sdk.Commerce.GetAccountOverview(ctx, accountID)
	if err != nil {
		t.Fatalf("account overview: %v", err)
	}
	if overview.SubscriptionSummary.PlanKey != "pro" || overview.SubscriptionSummary.AccessState != "entitled" {
		t.Fatalf("subscription overview = %+v", overview.SubscriptionSummary)
	}
	if len(overview.PaymentMethods) != 1 || overview.PaymentMethods[0].Last4 != "4242" {
		t.Fatalf("payment methods = %+v", overview.PaymentMethods)
	}
	if len(overview.ProviderInvoices) != 1 || overview.ProviderInvoices[0].canonicalProviderInvoiceID() != invoiceID {
		t.Fatalf("provider invoices = %+v", overview.ProviderInvoices)
	}
}

func TestPostgresAutoRechargeAttemptAndWebhookReconciliation(t *testing.T) {
	ctx, cancel, config := financialContext(t)
	defer cancel()
	runID := uuid.NewString()
	paymentID := "auto-payment-" + runID
	provider := &financialProvider{
		name:      "alpha",
		methods:   []PaymentMethodInfo{{ID: "pm-auto", Brand: "visa", Last4: "4242", ExpiryMonth: 12, ExpiryYear: 2030, IsDefault: true}},
		paymentID: paymentID,
	}
	sdk, _ := newFinancialSDK(t, ctx, config, provider)
	accountID := uuid.NewString()
	providerCustomerID := "cus-" + runID
	if err := sdk.Billing.UpsertCustomer(ctx, "alpha", providerCustomerID, accountID, "auto@example.com"); err != nil {
		t.Fatalf("persist auto-recharge customer: %v", err)
	}

	status, err := sdk.Commerce.AutoRecharge.Enable(ctx, AutoRechargeInput{AccountID: accountID, ReturnURL: "https://app.example/auto"})
	if err != nil {
		t.Fatalf("enable auto-recharge: %v", err)
	}
	if status == nil || !status.Enabled || status.State != AutoRechargeStateActive || status.PaymentMethodLast4 != "4242" || status.QuoteAmountMinor == nil || *status.QuoteAmountMinor != 500 {
		t.Fatalf("auto-recharge status = %+v", status)
	}
	if len(provider.chargeCalls) != 1 || provider.chargeCalls[0].CustomerID != providerCustomerID || provider.chargeCalls[0].PaymentMethodID != "pm-auto" {
		t.Fatalf("saved payment charge calls = %+v", provider.chargeCalls)
	}
	if count, err := sdk.Billing.CountAutoRechargeAttempts(ctx, accountID, time.Now().UTC().Add(-time.Hour)); err != nil || count != 1 {
		t.Fatalf("auto-recharge attempts = %d, error = %v", count, err)
	}

	if err := sdk.Billing.UpdateAutoRechargeAttemptByProviderPayment(ctx, AutoRechargeProviderPaymentUpdate{
		Provider:          "alpha",
		ProviderPaymentID: paymentID,
		State:             AutoRechargeAttemptSucceeded,
	}); err != nil {
		t.Fatalf("reconcile auto-recharge payment: %v", err)
	}
	profile, err := sdk.Billing.GetAutoRechargeProfile(ctx, accountID)
	if err != nil || profile == nil || profile.State != AutoRechargeStateActive || profile.Armed {
		t.Fatalf("reconciled auto-recharge profile = %+v, error = %v", profile, err)
	}

	result, err := sdk.IngestBillingEvent(ctx, BillingEvent{
		EventID:    "evt-" + runID + "-auto-payment-succeeded",
		Provider:   "alpha",
		Type:       BillingEventPaymentSucceeded,
		OccurredAt: time.Now().UTC(),
		AccountID:  accountID,
		Payment: &BillingPayment{
			ProviderPaymentID: paymentID,
			AmountMinor:       500,
			Currency:          "USD",
			Purpose:           "credit_topup",
			Status:            "succeeded",
			Refs:              &ProviderRef{PriceID: "alpha-credit-pack"},
		},
	})
	if err != nil || !result.Handled {
		t.Fatalf("auto-recharge payment event = %+v, error = %v", result, err)
	}
	balance, err := sdk.Credits.GetBalance(ctx, accountID)
	if err != nil || !balance.Balance.Equal(MustAmount("1234.567891")) {
		t.Fatalf("auto-recharge balance = %s, error = %v", balance.Balance, err)
	}
	decision, err := sdk.Commerce.AutoRecharge.ProcessIfNeeded(ctx, AutoRechargeInput{AccountID: accountID, ReturnURL: "https://app.example/auto"})
	if err != nil || decision.Outcome != AutoRechargeOutcomeAboveThreshold {
		t.Fatalf("above-threshold decision = %+v, error = %v", decision, err)
	}
	if err := sdk.Commerce.AutoRecharge.Disable(ctx, accountID); err != nil {
		t.Fatalf("disable auto-recharge: %v", err)
	}
	disabled, err := sdk.Commerce.AutoRecharge.ProcessIfNeeded(ctx, AutoRechargeInput{AccountID: accountID, ReturnURL: "https://app.example/auto"})
	if err != nil || disabled.Outcome != AutoRechargeOutcomeDisabled {
		t.Fatalf("disabled decision = %+v, error = %v", disabled, err)
	}
}

func TestPostgresOutboxRecoveryIsTenantScopedAndReplaySafe(t *testing.T) {
	ctx, cancel, config := financialContext(t)
	defer cancel()
	runID := uuid.NewString()
	topic := "test.financial." + runID
	firstID := seedFinancialOutboxEvent(t, ctx, config, topic, "dead_letter", 1, "outbox-"+runID+"-complete")
	secondID := seedFinancialOutboxEvent(t, ctx, config, topic, "dead_letter", 1, "outbox-"+runID+"-fail")

	client, err := NewPostgresClient(ctx, config.databaseURL, PostgresClientOptions{
		TenantID:            config.tenantID,
		AccessRole:          PostgresAccessRoleOperator,
		ProviderEnvironment: config.providerEnvironment,
		MaxConnections:      2,
	})
	if err != nil {
		t.Fatalf("construct operator outbox client: %v", err)
	}
	t.Cleanup(func() { _ = client.Close() })
	repository, err := NewPostgresStorageRepository(client)
	if err != nil {
		t.Fatalf("construct outbox repository: %v", err)
	}

	stats, err := repository.Stats(ctx)
	if err != nil {
		t.Fatalf("read outbox stats: %v", err)
	}
	if stats.DeadLetterCount < 2 {
		t.Fatalf("dead-letter count = %d, want at least 2", stats.DeadLetterCount)
	}
	page, err := repository.ListDeadLetters(ctx, OutboxDeadLetterListOptions{Limit: 1})
	if err != nil || len(page.Items) != 1 || page.NextCursor == nil {
		t.Fatalf("dead-letter page = %+v, error = %v", page, err)
	}

	if ok, err := repository.Requeue(ctx, firstID); err != nil || !ok {
		t.Fatalf("requeue first event = %v, error = %v", ok, err)
	}
	if ok, err := repository.Requeue(ctx, firstID); err != nil || ok {
		t.Fatalf("requeue replay = %v, error = %v, want false", ok, err)
	}
	claimed, err := repository.Claim(ctx, []string{topic}, 1, 60)
	if err != nil || len(claimed) != 1 || claimed[0].EventID != firstID {
		t.Fatalf("claimed event = %+v, error = %v", claimed, err)
	}
	if ok, err := repository.Renew(ctx, claimed[0], 60); err != nil || !ok {
		t.Fatalf("renew claim = %v, error = %v", ok, err)
	}
	if ok, err := repository.Complete(ctx, claimed[0]); err != nil || !ok {
		t.Fatalf("complete claim = %v, error = %v", ok, err)
	}
	if ok, err := repository.Complete(ctx, claimed[0]); err != nil || ok {
		t.Fatalf("complete replay = %v, error = %v, want false", ok, err)
	}

	if ok, err := repository.Requeue(ctx, secondID); err != nil || !ok {
		t.Fatalf("requeue second event = %v, error = %v", ok, err)
	}
	failed, err := repository.Claim(ctx, []string{topic}, 1, 60)
	if err != nil || len(failed) != 1 || failed[0].EventID != secondID {
		t.Fatalf("claimed second event = %+v, error = %v", failed, err)
	}
	if ok, err := repository.Fail(ctx, failed[0], "outbox_delivery_failed:RuntimeError", 1, 1); err != nil || !ok {
		t.Fatalf("fail claim = %v, error = %v", ok, err)
	}

	if config.secondaryTenantID == "" {
		if os.Getenv("BURSAR_REQUIRE_POSTGRES_TESTS") == "1" {
			t.Fatal("BURSAR_SECONDARY_TENANT_ID is required for the tenant-scoped outbox test")
		}
		return
	}
	secondaryClient, err := NewPostgresClient(ctx, config.databaseURL, PostgresClientOptions{
		TenantID:            config.secondaryTenantID,
		AccessRole:          PostgresAccessRoleOperator,
		ProviderEnvironment: config.providerEnvironment,
		MaxConnections:      2,
	})
	if err != nil {
		t.Fatalf("construct secondary outbox client: %v", err)
	}
	t.Cleanup(func() { _ = secondaryClient.Close() })
	secondaryRepository, err := NewPostgresStorageRepository(secondaryClient)
	if err != nil {
		t.Fatalf("construct secondary outbox repository: %v", err)
	}
	if _, err := secondaryRepository.Stats(ctx); err != nil {
		t.Fatalf("secondary tenant outbox stats: %v", err)
	}
	if ok, err := secondaryRepository.Requeue(ctx, secondID); err != nil || ok {
		t.Fatalf("secondary tenant requeue of primary event = %v, error = %v, want false", ok, err)
	}
}

func seedFinancialOutboxEvent(t *testing.T, ctx context.Context, config postgresIntegrationConfig, topic, status string, attempts int, key string) string {
	t.Helper()
	connection, err := pgx.Connect(ctx, config.databaseURL)
	if err != nil {
		t.Fatalf("connect to seed outbox event: %v", err)
	}
	t.Cleanup(func() { _ = connection.Close(ctx) })
	if _, err := connection.Exec(ctx, "SELECT set_config('bursar.tenant_id', $1, true)", config.tenantID); err != nil {
		t.Fatalf("set outbox tenant: %v", err)
	}
	if _, err := connection.Exec(ctx, "SELECT set_config('bursar.provider_environment', $1, true)", string(config.providerEnvironment)); err != nil {
		t.Fatalf("set outbox provider environment: %v", err)
	}
	if _, err := connection.Exec(ctx, "SELECT set_config('bursar.mutation_context', 'internal', true)"); err != nil {
		t.Fatalf("set outbox mutation context: %v", err)
	}
	var eventID int64
	err = connection.QueryRow(ctx, `
		INSERT INTO bursar.event_outbox(
			tenant_id, topic, aggregate_type, aggregate_id, idempotency_key,
			status, attempt_count, last_error, created_at
		)
		VALUES ($1::uuid, $2, 'credit_usage_charge', $3::uuid,
			$4, $5, $6, 'outbox_delivery_failed:RuntimeError', now())
		RETURNING id
	`, config.tenantID, topic, uuid.NewString(), key, status, attempts).Scan(&eventID)
	if err != nil {
		t.Fatalf("seed outbox event: %v", err)
	}
	return strconv.FormatInt(eventID, 10)
}
