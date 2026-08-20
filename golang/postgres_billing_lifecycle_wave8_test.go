// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import (
	"context"
	"errors"
	"maps"
	"testing"
	"time"

	"github.com/google/uuid"
)

type lifecycleWave8Provisioning struct {
	get func(context.Context, string) (GetUserPlanResult, error)
}

func (p lifecycleWave8Provisioning) GetUserPlan(ctx context.Context, accountID string) (GetUserPlanResult, error) {
	if p.get != nil {
		return p.get(ctx, accountID)
	}
	return GetUserPlanResult{}, nil
}

func (p lifecycleWave8Provisioning) SetUserPlan(ctx context.Context, accountID, planKey string, options SetUserPlanOptions) (SetUserPlanResult, error) {
	return SetUserPlanResult{}, nil
}

func (p lifecycleWave8Provisioning) UnsetUserPlan(ctx context.Context, accountID string) (UnsetUserPlanResult, error) {
	return UnsetUserPlanResult{}, nil
}

func TestPostgresLifecyclePlanChangeAndTerminalEntitlement(t *testing.T) {
	ctx, cancel, config := financialContext(t)
	defer cancel()
	sdk, store := newFinancialSDK(t, ctx, config, &financialProvider{name: "alpha"})

	catalog := financialCatalogConfig(t)
	offers := catalog["commerce"].(map[string]any)["offers"].(map[string]any)
	yearly := maps.Clone(offers["pro_month"].(map[string]any))
	yearly["display_name"] = "Pro yearly"
	yearly["billing_interval"] = map[string]any{"unit": "year", "count": 1}
	yearly["price"] = map[string]any{"amount_minor": 9000, "currency": "USD"}
	yearly["providers"] = map[string]any{"alpha": map[string]any{"type": "stripe_price", "price_id": "alpha-pro-year"}}
	delete(yearly, "cycle_grant")
	offers["pro_year"] = yearly
	if _, err := sdk.Catalog.PublishAndActivate(ctx, catalog, "lifecycle-wave8-plan-change", newAssignmentsRollout(catalog)); err != nil {
		t.Fatalf("publish yearly offer: %v", err)
	}

	runID := uuid.NewString()
	accountID := uuid.NewString()
	subscriptionID := "sub-plan-" + runID
	now := time.Now().UTC().Add(-10 * time.Minute).Truncate(time.Second)
	periodEnd := now.Add(30 * 24 * time.Hour)
	monthly := &BillingSubscription{
		ProviderSubscriptionID: subscriptionID,
		Status:                 "active",
		Interval:               "month",
		IntervalCount:          1,
		PeriodStart:            &now,
		PeriodEnd:              &periodEnd,
		Refs:                   &ProviderRef{PriceID: "alpha-pro-month"},
	}
	created, err := sdk.IngestBillingEvent(ctx, BillingEvent{
		EventID: "evt-plan-created-" + runID, Provider: "alpha", Type: BillingEventSubscriptionCreated,
		OccurredAt: now, AccountID: accountID, Subscription: monthly,
	})
	if err != nil || created.Action != "subscription_created" {
		t.Fatalf("create subscription = %+v, error = %v", created, err)
	}
	initialPlan, err := sdk.Credits.GetUserPlan(ctx, accountID)
	if err != nil || initialPlan.PlanKey != "pro" || initialPlan.PlanAssignedAt == nil {
		t.Fatalf("initial plan = %+v, error = %v", initialPlan, err)
	}

	yearOffer, err := store.ResolveBillingOffer(ctx, "alpha", "", "alpha-pro-year", "")
	if err != nil || yearOffer == nil {
		t.Fatalf("resolve yearly offer = %+v, error = %v", yearOffer, err)
	}
	change, err := store.CreateBillingSubscriptionChange(ctx, BillingSubscriptionChangeCreate{
		Provider: "alpha", ProviderSubscriptionID: subscriptionID,
		ToOfferID: yearOffer.ID, ToOfferKey: yearOffer.OfferKey, ToPlanKey: yearOffer.PlanKey, ToInterval: yearOffer.Interval,
		Effective: "renewal", EffectiveAt: periodEnd, ProrationBehavior: "none", OperationKey: "lifecycle-wave8-change-" + runID,
	})
	if err != nil || change.ID == "" || change.State != "scheduled" {
		t.Fatalf("create pending plan change = %+v, error = %v", change, err)
	}
	yearlyEnd := now.Add(365 * 24 * time.Hour)
	yearlySubscription := &BillingSubscription{
		ProviderSubscriptionID: subscriptionID,
		Status:                 "active",
		Interval:               "year",
		IntervalCount:          1,
		PeriodStart:            &now,
		PeriodEnd:              &yearlyEnd,
		Refs:                   &ProviderRef{PriceID: "alpha-pro-year"},
	}
	changed, err := sdk.IngestBillingEvent(ctx, BillingEvent{
		EventID: "evt-plan-changed-" + runID, Provider: "alpha", Type: BillingEventSubscriptionPlanChanged,
		OccurredAt: now.Add(2 * time.Minute), AccountID: accountID, Subscription: yearlySubscription,
	})
	if err != nil || changed.Action != "subscription_plan_changed" {
		t.Fatalf("plan change event = %+v, error = %v", changed, err)
	}
	if replay, err := sdk.IngestBillingEvent(ctx, BillingEvent{
		EventID: "evt-plan-changed-" + runID, Provider: "alpha", Type: BillingEventSubscriptionPlanChanged,
		OccurredAt: now.Add(2 * time.Minute), AccountID: accountID, Subscription: yearlySubscription,
	}); err != nil || !replay.Duplicate {
		t.Fatalf("plan change replay = %+v, error = %v", replay, err)
	}
	if open, err := store.GetOpenBillingSubscriptionChange(ctx, "alpha", subscriptionID); err != nil || open != nil {
		t.Fatalf("open change after provider confirmation = %+v, error = %v", open, err)
	}
	current, err := store.GetBillingSubscriptionByProvider(ctx, "alpha", subscriptionID)
	if err != nil || current == nil || current.OfferKey != "pro_year" || current.Interval != "year" {
		t.Fatalf("changed subscription = %+v, error = %v", current, err)
	}
	changedPlan, err := sdk.Credits.GetUserPlan(ctx, accountID)
	if err != nil || changedPlan.PlanAssignedAt == nil || !changedPlan.PlanAssignedAt.Equal(*initialPlan.PlanAssignedAt) {
		t.Fatalf("plan-change allowance anchor = %+v, initial = %+v, error = %v", changedPlan, initialPlan, err)
	}

	zeroGrace := time.Duration(0)
	graceService, err := NewBillingService(store, BillingServiceOptions{Provisioning: store, PastDueGracePeriod: &zeroGrace})
	if err != nil {
		t.Fatalf("construct zero-grace billing service: %v", err)
	}
	failedSubscription := *yearlySubscription
	failedSubscription.Refs = nil
	failed, err := graceService.Ingest(ctx, BillingEvent{
		EventID: "evt-plan-payment-failed-" + runID, Provider: "alpha", Type: BillingEventPaymentFailed,
		OccurredAt: now.Add(3 * time.Minute), AccountID: accountID,
		Payment:      &BillingPayment{ProviderPaymentID: "pay-plan-failed-" + runID, Status: "failed", Currency: "USD", AmountMinor: 9000, Purpose: "subscription"},
		Subscription: &failedSubscription,
	})
	if err != nil || failed.Action != "payment_failed_recorded" {
		t.Fatalf("payment failure = %+v, error = %v", failed, err)
	}
	pastDue, err := store.GetBillingSubscriptionByProvider(ctx, "alpha", subscriptionID)
	if err != nil || pastDue == nil || pastDue.Status != "past_due" || pastDue.GraceEndsAt == nil || pastDue.GraceEndsAt.After(time.Now().UTC()) {
		t.Fatalf("past-due subscription = %+v, error = %v", pastDue, err)
	}
	if plan, err := sdk.Credits.GetUserPlan(ctx, accountID); err != nil || plan.PlanKey != "" {
		t.Fatalf("plan after expired grace = %+v, error = %v", plan, err)
	}

	resumedSubscription := *yearlySubscription
	resumedSubscription.Refs = nil
	if resumed, err := sdk.IngestBillingEvent(ctx, BillingEvent{
		EventID: "evt-plan-resumed-" + runID, Provider: "alpha", Type: BillingEventSubscriptionResumed,
		OccurredAt: now.Add(4 * time.Minute), AccountID: accountID, Subscription: &resumedSubscription,
	}); err != nil || resumed.Action != "subscription_resumed" {
		t.Fatalf("resume subscription = %+v, error = %v", resumed, err)
	}
	terminalService, err := NewBillingService(store, BillingServiceOptions{Provisioning: store, TerminalPlanKey: "starter"})
	if err != nil {
		t.Fatalf("construct terminal-plan billing service: %v", err)
	}
	canceledSubscription := resumedSubscription
	if canceled, err := terminalService.Ingest(ctx, BillingEvent{
		EventID: "evt-plan-canceled-" + runID, Provider: "alpha", Type: BillingEventSubscriptionCanceled,
		OccurredAt: now.Add(5 * time.Minute), AccountID: accountID, Subscription: &canceledSubscription,
	}); err != nil || canceled.Action != "subscription_canceled" {
		if typed, ok := AsBursarError(err); ok && typed.Unwrap() != nil {
			t.Fatalf("cancel subscription = %+v, error = %v: %v", canceled, err, typed.Unwrap())
		}
		t.Fatalf("cancel subscription = %+v, error = %v", canceled, err)
	}
	if plan, err := sdk.Credits.GetUserPlan(ctx, accountID); err != nil || plan.PlanKey != "starter" {
		t.Fatalf("terminal plan = %+v, error = %v", plan, err)
	}
}

func TestPostgresLifecycleCycleGrantAndProviderAccountResolution(t *testing.T) {
	ctx, cancel, config := financialContext(t)
	defer cancel()
	sdk, store := newFinancialSDK(t, ctx, config, &financialProvider{name: "alpha"})
	runID := uuid.NewString()
	accountID := uuid.NewString()
	subscriptionID := "sub-resolution-" + runID
	now := time.Now().UTC().Add(-8 * time.Hour).Truncate(time.Second)
	periodEnd := now.Add(30 * 24 * time.Hour)
	subscription := &BillingSubscription{
		ProviderSubscriptionID: subscriptionID,
		Status:                 "active",
		Interval:               "month",
		IntervalCount:          1,
		PeriodStart:            &now,
		PeriodEnd:              &periodEnd,
		Refs:                   &ProviderRef{PriceID: "alpha-pro-month"},
	}
	if created, err := sdk.IngestBillingEvent(ctx, BillingEvent{
		EventID: "evt-resolution-created-" + runID, Provider: "alpha", Type: BillingEventSubscriptionCreated,
		OccurredAt: now, AccountID: accountID, Subscription: subscription,
	}); err != nil || created.Action != "subscription_created" {
		t.Fatalf("create cycle subscription = %+v, error = %v", created, err)
	}

	unclaimedRenewal := *subscription
	unclaimedRenewal.PeriodStart = timePointer(now.Add(time.Hour))
	unclaimedRenewal.PeriodEnd = timePointer(periodEnd.Add(time.Hour))
	if _, err := store.ProcessBillingEvent(ctx, BillingEvent{
		EventID: "evt-unclaimed-renewal-" + runID, BillingEventID: uuid.NewString(), Provider: "alpha",
		Type: BillingEventSubscriptionRenewed, OccurredAt: now.Add(time.Hour), AccountID: accountID, Subscription: &unclaimedRenewal,
	}, accountID); err == nil {
		t.Fatal("unclaimed cycle renewal minted credits")
	}
	if balance, err := sdk.Credits.GetBalance(ctx, accountID); err != nil || !balance.Balance.Equal(DecimalZero) {
		t.Fatalf("balance after rejected renewal = %+v, error = %v", balance, err)
	}

	claimedRenewal := unclaimedRenewal
	claimedRenewal.PeriodStart = timePointer(now.Add(2 * time.Hour))
	claimedRenewal.PeriodEnd = timePointer(periodEnd.Add(2 * time.Hour))
	renewalEvent := BillingEvent{
		EventID: "evt-claimed-renewal-" + runID, Provider: "alpha", Type: BillingEventSubscriptionRenewed,
		OccurredAt: now.Add(2 * time.Hour), AccountID: accountID, Subscription: &claimedRenewal,
	}
	if renewed, err := sdk.IngestBillingEvent(ctx, renewalEvent); err != nil || renewed.Action != "subscription_renewed" {
		t.Fatalf("claimed renewal = %+v, error = %v", renewed, err)
	}
	if replay, err := sdk.IngestBillingEvent(ctx, renewalEvent); err != nil || !replay.Duplicate {
		t.Fatalf("cycle renewal replay = %+v, error = %v", replay, err)
	}
	if balance, err := sdk.Credits.GetBalance(ctx, accountID); err != nil || !balance.Balance.Equal(MustAmount("12.345678")) {
		t.Fatalf("cycle grant balance = %+v, error = %v", balance, err)
	}

	invoiceID := "inv-resolution-" + runID
	invoiceResult, err := store.ProcessBillingEvent(ctx, BillingEvent{
		EventID: "evt-invoice-resolution-" + runID, Provider: "alpha", Type: BillingEventInvoiceCreated,
		OccurredAt: now.Add(3 * time.Hour), Subscription: &BillingSubscription{ProviderSubscriptionID: subscriptionID},
		Invoice: &BillingInvoice{ProviderInvoiceID: invoiceID, Status: "open", Currency: "USD", AmountDueMinor: 5000},
	}, "")
	if err != nil || invoiceResult.AccountID != accountID {
		t.Fatalf("invoice account resolution = %+v, error = %v", invoiceResult, err)
	}

	paymentID := "pay-resolution-" + runID
	if _, err := store.UpsertBillingPaymentState(ctx, BillingPaymentUpsert{
		Provider: "alpha", ProviderPaymentID: paymentID, AccountID: accountID,
		AmountMinor: 5000, Currency: "USD", Purpose: "subscription", Status: "pending", ProviderUpdatedAt: now.Add(3 * time.Hour),
	}); err != nil {
		t.Fatalf("seed pending payment: %v", err)
	}
	paymentResult, err := store.ProcessBillingEvent(ctx, BillingEvent{
		EventID: "evt-payment-resolution-" + runID, Provider: "alpha", Type: BillingEventPaymentSucceeded,
		OccurredAt: now.Add(4 * time.Hour), Payment: &BillingPayment{ProviderPaymentID: paymentID, AmountMinor: 5000, Currency: "USD", Purpose: "subscription", Status: "succeeded"},
	}, "")
	if err != nil || paymentResult.AccountID != accountID || paymentResult.Action != "payment_succeeded" {
		t.Fatalf("payment account resolution = %+v, error = %v", paymentResult, err)
	}
	refundResult, err := store.ProcessBillingEvent(ctx, BillingEvent{
		EventID: "evt-refund-resolution-" + runID, Provider: "alpha", Type: BillingEventRefundCreated,
		OccurredAt: now.Add(5 * time.Hour), Refund: &BillingRefund{ProviderRefundID: "refund-resolution-" + runID, ProviderPaymentID: paymentID, AmountMinor: 1000, Currency: "USD", Status: "succeeded"},
	}, "")
	if err != nil || refundResult.AccountID != accountID || refundResult.Action != "refund_recorded" {
		t.Fatalf("refund account resolution = %+v, error = %v", refundResult, err)
	}
	if _, err := store.ProcessBillingEvent(ctx, BillingEvent{
		EventID: "evt-refund-missing-" + runID, Provider: "alpha", Type: BillingEventRefundCreated,
		OccurredAt: now.Add(6 * time.Hour), Refund: &BillingRefund{ProviderRefundID: "refund-missing-" + runID, ProviderPaymentID: "pay-missing-" + runID, AmountMinor: 1, Currency: "USD", Status: "succeeded"},
	}, ""); err == nil {
		t.Fatal("refund without a resolvable account was accepted")
	}
}

func TestPostgresLifecycleFailsClosedAtPersistenceBoundaries(t *testing.T) {
	ctx, cancel, config := financialContext(t)
	defer cancel()
	_, store := newFinancialSDK(t, ctx, config, &financialProvider{name: "alpha"})
	runID := uuid.NewString()
	now := time.Now().UTC().Truncate(time.Second)
	accountID := uuid.NewString()
	processor := postgresBillingLifecycle{store: store, provisioning: store, autoSelectEntitlementSource: true, pastDueGracePeriod: 48 * time.Hour}

	canceledCtx, cancelPersistence := context.WithCancel(ctx)
	cancelPersistence()
	customer := &BillingCustomer{ProviderCustomerID: "cus-failure-" + runID}
	subscription := &BillingSubscription{ProviderSubscriptionID: "sub-failure-" + runID, Status: "active", Refs: &ProviderRef{PriceID: "alpha-pro-month"}}
	payment := &BillingPayment{ProviderPaymentID: "pay-failure-" + runID, AmountMinor: 500, Currency: "USD", Purpose: "subscription", Status: "failed"}
	refund := &BillingRefund{ProviderRefundID: "refund-failure-" + runID, ProviderPaymentID: payment.ProviderPaymentID, AmountMinor: 100, Currency: "USD", Status: "succeeded"}
	checkoutID := uuid.NewString()

	billingError := func(_ BillingEventResult, err error) error { return err }
	accountError := func(_ string, err error) error { return err }
	failures := []struct {
		name string
		err  error
	}{
		{"customer upsert", billingError(processor.process(canceledCtx, BillingEvent{Provider: "alpha", Type: BillingEventCustomerCreated, AccountID: accountID, Customer: customer}, accountID))},
		{"checkout customer upsert", billingError(processor.process(canceledCtx, BillingEvent{Provider: "alpha", Type: BillingEventCheckoutCompleted, AccountID: accountID, Customer: customer}, accountID))},
		{"checkout completion", billingError(processor.process(canceledCtx, BillingEvent{Provider: "alpha", Type: BillingEventCheckoutCompleted, AccountID: accountID, Metadata: map[string]any{"checkout_intent_id": checkoutID}}, accountID))},
		{"checkout expiry", billingError(processor.process(canceledCtx, BillingEvent{Provider: "alpha", Type: BillingEventCheckoutExpired, AccountID: accountID, Metadata: map[string]any{"checkout_intent_id": checkoutID}}, accountID))},
		{"paid invoice", billingError(processor.process(canceledCtx, BillingEvent{Provider: "alpha", Type: BillingEventInvoicePaid, AccountID: accountID, Invoice: &BillingInvoice{ProviderInvoiceID: "inv-paid-" + runID}}, accountID))},
		{"created invoice", billingError(processor.process(canceledCtx, BillingEvent{Provider: "alpha", Type: BillingEventInvoiceCreated, AccountID: accountID, Invoice: &BillingInvoice{ProviderInvoiceID: "inv-created-" + runID}}, accountID))},
		{"dispute upsert", billingError(processor.process(canceledCtx, BillingEvent{Provider: "alpha", Type: BillingEventDisputeCreated, AccountID: accountID, Dispute: &BillingDispute{ProviderDisputeID: "dispute-" + runID, ProviderPaymentID: payment.ProviderPaymentID}}, accountID))},
		{"customer account lookup", accountError(processor.resolveAccount(canceledCtx, BillingEvent{Provider: "alpha", Customer: customer}, ""))},
		{"subscription account lookup", accountError(processor.resolveAccount(canceledCtx, BillingEvent{Provider: "alpha", Subscription: subscription}, ""))},
		{"payment account lookup", accountError(processor.resolveAccount(canceledCtx, BillingEvent{Provider: "alpha", Payment: payment}, ""))},
		{"subscription customer upsert", billingError(processor.applySubscription(canceledCtx, BillingEvent{Provider: "alpha", Customer: customer, Subscription: subscription}, accountID, "subscription_updated"))},
		{"subscription lookup", billingError(processor.applySubscription(canceledCtx, BillingEvent{Provider: "alpha", Subscription: subscription}, accountID, "subscription_updated"))},
		{"top-up resolution", billingError(processor.paymentSucceeded(canceledCtx, BillingEvent{Provider: "alpha", Payment: &BillingPayment{ProviderPaymentID: "pay-topup-failure-" + runID, Purpose: "credit_topup", Refs: &ProviderRef{PriceID: "alpha-credit-pack"}}}, accountID))},
		{"payment upsert", billingError(processor.paymentSucceeded(canceledCtx, BillingEvent{Provider: "alpha", Payment: payment}, accountID))},
		{"failed payment upsert", billingError(processor.paymentFailed(canceledCtx, BillingEvent{Provider: "alpha", Payment: payment}, accountID))},
		{"auto-recharge reconciliation", billingError(processor.paymentFailed(canceledCtx, BillingEvent{Provider: "alpha", Payment: payment}, ""))},
		{"refund upsert", billingError(processor.refund(canceledCtx, BillingEvent{Provider: "alpha", Refund: refund}, accountID))},
		{"entitlement reconciliation", func() error {
			_, err := store.reconcileSubscriptionEntitlement(canceledCtx, accountID, uuid.NewString(), uuid.NewString(), "canceled", now, nil, true, "", "subscription_canceled")
			return err
		}()},
	}
	for _, test := range failures {
		if test.err == nil {
			t.Errorf("%s succeeded after persistence context cancellation", test.name)
		}
	}

	if _, err := processor.applySubscription(ctx, BillingEvent{Provider: "alpha", Subscription: subscription}, "", "subscription_updated"); err == nil {
		t.Fatal("subscription without an account was accepted")
	}
	missingOffer := &BillingSubscription{ProviderSubscriptionID: "sub-missing-offer-" + runID, Status: "active"}
	if _, err := processor.applySubscription(ctx, BillingEvent{Provider: "alpha", Subscription: missingOffer}, uuid.NewString(), "subscription_updated"); err == nil {
		t.Fatal("subscription without an existing or catalog offer was accepted")
	}
	if _, err := processor.paymentSucceeded(ctx, BillingEvent{Provider: "alpha", Payment: payment}, ""); err == nil {
		t.Fatal("payment without an account was accepted")
	}

	defaultedAccount := uuid.NewString()
	defaultedSubscription := &BillingSubscription{ProviderSubscriptionID: "sub-defaulted-" + runID, Refs: &ProviderRef{PriceID: "alpha-pro-month"}}
	withoutProvisioning := postgresBillingLifecycle{store: store, pastDueGracePeriod: 48 * time.Hour}
	autoSelect := false
	disabledEntitlementService, err := NewBillingService(store, BillingServiceOptions{AutoSelectEntitlementSource: &autoSelect})
	if err != nil {
		t.Fatalf("construct disabled-entitlement service: %v", err)
	}
	if _, err := disabledEntitlementService.Ingest(ctx, BillingEvent{
		EventID: "evt-defaulted-" + runID, Provider: "alpha", Type: BillingEventSubscriptionUpdated,
		OccurredAt: now, AccountID: defaultedAccount, Subscription: defaultedSubscription,
	}); err != nil {
		t.Fatalf("persist default subscription state: %v", err)
	}
	defaulted, err := store.GetBillingSubscriptionByProvider(ctx, "alpha", defaultedSubscription.ProviderSubscriptionID)
	if err != nil || defaulted == nil || defaulted.Status != "incomplete" {
		t.Fatalf("defaulted subscription = %+v, error = %v", defaulted, err)
	}

	graceAccount := uuid.NewString()
	graceSubscription := &BillingSubscription{ProviderSubscriptionID: "sub-grace-" + runID, Status: "past_due", Refs: &ProviderRef{PriceID: "alpha-pro-month"}}
	if _, err := disabledEntitlementService.Ingest(ctx, BillingEvent{
		EventID: "evt-grace-initial-" + runID, Provider: "alpha", Type: BillingEventSubscriptionUpdated,
		OccurredAt: now, AccountID: graceAccount, Subscription: graceSubscription,
	}); err != nil {
		t.Fatalf("persist initial grace: %v", err)
	}
	initialGrace, err := store.GetBillingSubscriptionByProvider(ctx, "alpha", graceSubscription.ProviderSubscriptionID)
	if err != nil || initialGrace == nil || initialGrace.GraceEndsAt == nil {
		t.Fatalf("initial grace = %+v, error = %v", initialGrace, err)
	}
	graceSubscription.Refs = nil
	if _, err := disabledEntitlementService.Ingest(ctx, BillingEvent{
		EventID: "evt-grace-preserved-" + runID, Provider: "alpha", Type: BillingEventSubscriptionUpdated,
		OccurredAt: now.Add(time.Hour), AccountID: graceAccount, Subscription: graceSubscription,
	}); err != nil {
		t.Fatalf("preserve grace: %v", err)
	}
	preservedGrace, err := store.GetBillingSubscriptionByProvider(ctx, "alpha", graceSubscription.ProviderSubscriptionID)
	if err != nil || preservedGrace == nil || preservedGrace.GraceEndsAt == nil || !preservedGrace.GraceEndsAt.Equal(*initialGrace.GraceEndsAt) {
		t.Fatalf("preserved grace = %+v, initial = %+v, error = %v", preservedGrace, initialGrace, err)
	}

	planChangeEvent := func(subscriptionID string) BillingEvent {
		return BillingEvent{Provider: "alpha", OccurredAt: now, Subscription: &BillingSubscription{ProviderSubscriptionID: subscriptionID, Status: "active", Refs: &ProviderRef{PriceID: "alpha-pro-month"}}}
	}
	sentinel := errors.New("provisioning unavailable")
	getFailure := postgresBillingLifecycle{store: store, provisioning: lifecycleWave8Provisioning{get: func(context.Context, string) (GetUserPlanResult, error) {
		return GetUserPlanResult{}, sentinel
	}}}
	if _, err := getFailure.applySubscription(ctx, planChangeEvent("sub-plan-get-failure-"+runID), uuid.NewString(), "subscription_plan_changed"); !errors.Is(err, sentinel) {
		t.Fatalf("plan lookup error = %v, want %v", err, sentinel)
	}

	changeCtx, cancelChange := context.WithCancel(ctx)
	openChangeFailure := postgresBillingLifecycle{store: store, provisioning: lifecycleWave8Provisioning{get: func(context.Context, string) (GetUserPlanResult, error) {
		cancelChange()
		return GetUserPlanResult{}, nil
	}}}
	if _, err := openChangeFailure.applySubscription(changeCtx, planChangeEvent("sub-open-change-failure-"+runID), uuid.NewString(), "subscription_plan_changed"); err == nil {
		t.Fatal("plan change continued after its persistence context was canceled")
	}

	invalidCheckout := planChangeEvent("sub-checkout-failure-" + runID)
	invalidCheckout.Metadata = map[string]any{"checkout_intent_id": "not-a-uuid"}
	if _, err := processor.process(ctx, invalidCheckout, uuid.NewString()); err == nil {
		t.Fatal("active subscription accepted an invalid checkout reference")
	}

	missingSubscriptionPayment := BillingEvent{Provider: "alpha", OccurredAt: now, Payment: &BillingPayment{ProviderPaymentID: "pay-subscription-missing-" + runID, AmountMinor: 500, Currency: "USD", Purpose: "subscription", Status: "succeeded"}, Subscription: &BillingSubscription{ProviderSubscriptionID: "sub-payment-missing-" + runID}}
	if _, err := withoutProvisioning.paymentSucceeded(ctx, missingSubscriptionPayment, uuid.NewString()); err == nil {
		t.Fatal("subscription payment without subscription catalog state was accepted")
	}

	checkoutPayment := BillingEvent{Provider: "alpha", OccurredAt: now, Metadata: map[string]any{"checkout_intent_id": "not-a-uuid"}, Payment: &BillingPayment{ProviderPaymentID: "pay-checkout-failure-" + runID, AmountMinor: 500, Currency: "USD", Purpose: "subscription", Status: "succeeded"}}
	if _, err := withoutProvisioning.paymentSucceeded(ctx, checkoutPayment, uuid.NewString()); err == nil {
		t.Fatal("payment accepted an invalid checkout reference")
	}

	failedSubscriptionPayment := BillingEvent{Provider: "alpha", OccurredAt: now, Payment: &BillingPayment{ProviderPaymentID: "pay-failed-subscription-" + runID, AmountMinor: 500, Currency: "USD", Purpose: "subscription", Status: "failed"}, Subscription: &BillingSubscription{ProviderSubscriptionID: "sub-failed-missing-" + runID}}
	if _, err := withoutProvisioning.paymentFailed(ctx, failedSubscriptionPayment, uuid.NewString()); err == nil {
		t.Fatal("failed payment accepted missing subscription state")
	}
	failedCheckout := BillingEvent{Provider: "alpha", Metadata: map[string]any{"checkout_intent_id": "not-a-uuid"}, Payment: &BillingPayment{ProviderPaymentID: "pay-failed-checkout-" + runID, Status: "failed"}}
	if _, err := withoutProvisioning.paymentFailed(ctx, failedCheckout, ""); err == nil {
		t.Fatal("failed payment accepted an invalid checkout reference")
	}

	orphanPaymentID, err := store.UpsertBillingPaymentState(ctx, BillingPaymentUpsert{Provider: "alpha", ProviderPaymentID: "pay-orphan-topup-" + runID, AccountID: accountID, AmountMinor: 500, Currency: "USD", Purpose: "credit_topup", Status: "succeeded", ProviderUpdatedAt: now})
	if err != nil {
		t.Fatalf("seed orphan top-up payment: %v", err)
	}
	orphanRefund := BillingEvent{Provider: "alpha", OccurredAt: now, Refund: &BillingRefund{ProviderRefundID: "refund-orphan-" + runID, ProviderPaymentID: "pay-orphan-topup-" + runID, AmountMinor: 100, Currency: "USD", Status: "succeeded"}}
	orphanResult, err := withoutProvisioning.refund(ctx, orphanRefund, accountID)
	if err != nil || orphanResult.Action != "refund_recorded" {
		t.Fatalf("orphan top-up refund = %+v, payment = %s, error = %v", orphanResult, orphanPaymentID, err)
	}
}
