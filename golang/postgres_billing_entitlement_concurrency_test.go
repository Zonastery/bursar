// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import (
	"context"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/google/uuid"
)

func TestPostgresSubscriptionUpsertAndGraceExpiryDoNotDeadlock(t *testing.T) {
	ctx, cancel, config := financialContext(t)
	defer cancel()
	sdk, store := newFinancialSDK(t, ctx, config, &financialProvider{name: "alpha"})

	runID := uuid.NewString()
	accountID := uuid.NewString()
	subscriptionID := "sub-entitlement-race-" + runID
	now := time.Now().UTC().Truncate(time.Second)
	periodEnd := now.Add(30 * 24 * time.Hour)
	active := &BillingSubscription{
		ProviderSubscriptionID: subscriptionID,
		Status:                 "active",
		Interval:               "month",
		IntervalCount:          1,
		PeriodStart:            &now,
		PeriodEnd:              &periodEnd,
		Refs:                   &ProviderRef{PriceID: "alpha-pro-month"},
	}
	if result, err := sdk.IngestBillingEvent(ctx, BillingEvent{
		EventID: "evt-entitlement-race-created-" + runID, Provider: "alpha",
		Type: BillingEventSubscriptionCreated, OccurredAt: now, AccountID: accountID,
		Subscription: active,
	}); err != nil || result.Action != "subscription_created" {
		t.Fatalf("create race subscription = %+v, error = %v", result, err)
	}

	graceEndsAt := now.Add(-time.Minute)
	if _, err := store.UpsertBillingSubscriptionState(ctx, CommerceSubscription{
		AccountID: accountID, Provider: "alpha", ProviderSubscriptionID: subscriptionID,
		ProviderCustomerID: "cus-entitlement-race-" + runID, OfferID: activeOfferID(t, ctx, store),
		Status: "past_due", CurrentPeriodStart: &now, CurrentPeriodEnd: &periodEnd,
		GraceEndsAt: &graceEndsAt, ProviderUpdatedAt: now.Add(time.Minute),
	}); err != nil {
		t.Fatalf("seed expired grace subscription: %v", err)
	}
	candidate, err := store.GetBillingSubscriptionByProvider(ctx, "alpha", subscriptionID)
	if err != nil || candidate == nil || candidate.ID == "" || candidate.GraceEndsAt == nil {
		t.Fatalf("read grace candidate = %+v, error = %v", candidate, err)
	}

	raceCtx, raceCancel := context.WithTimeout(ctx, 8*time.Second)
	defer raceCancel()
	start := make(chan struct{})
	results := make(chan error, 16)
	var waitGroup sync.WaitGroup
	for worker := 0; worker < 8; worker++ {
		waitGroup.Add(2)
		go func() {
			defer waitGroup.Done()
			<-start
			_, err := store.UpsertBillingSubscriptionState(raceCtx, CommerceSubscription{
				AccountID: accountID, Provider: "alpha", ProviderSubscriptionID: subscriptionID,
				ProviderCustomerID: "cus-entitlement-race-" + runID, OfferID: candidate.OfferID,
				Status: "past_due", CurrentPeriodStart: &now, CurrentPeriodEnd: &periodEnd,
				GraceEndsAt: candidate.GraceEndsAt, ProviderUpdatedAt: now.Add(time.Minute),
			})
			results <- err
		}()
		go func() {
			defer waitGroup.Done()
			<-start
			_, err := store.expirePastDueGracePeriod(raceCtx, *candidate, now.Add(10*time.Minute), "")
			results <- err
		}()
	}
	close(start)
	waitGroup.Wait()
	close(results)
	for err := range results {
		if err != nil {
			t.Fatalf("concurrent subscription transition: %v", err)
		}
	}

	final, err := store.GetBillingSubscriptionByProvider(ctx, "alpha", subscriptionID)
	if err != nil || final == nil || final.Status != "past_due" || final.GraceExpiredAt == nil {
		t.Fatalf("final subscription state = %+v, error = %v", final, err)
	}
	plan, err := sdk.Credits.GetUserPlan(ctx, accountID)
	if err != nil {
		t.Fatalf("read final entitlement: %v", err)
	}
	if plan.PlanKey != "" || plan.AssignmentSourceType != "" || plan.AssignmentSourceID != "" {
		t.Fatalf("final entitlement = %+v, want no active assignment", plan)
	}
}

func TestPostgresSubscriptionEqualTimestampEventsApplySideEffectsOnce(t *testing.T) {
	ctx, cancel, config := financialContext(t)
	defer cancel()
	sdk, _ := newFinancialSDK(t, ctx, config, &financialProvider{name: "alpha"})

	runID := uuid.NewString()
	accountID := uuid.NewString()
	subscriptionID := "sub-equal-timestamp-" + runID
	createdAt := time.Now().UTC().Add(-time.Hour).Truncate(time.Second)
	periodEnd := createdAt.Add(30 * 24 * time.Hour)
	subscription := BillingSubscription{
		ProviderSubscriptionID: subscriptionID,
		Status:                 "active",
		Interval:               "month",
		IntervalCount:          1,
		PeriodStart:            &createdAt,
		PeriodEnd:              &periodEnd,
		Refs:                   &ProviderRef{PriceID: "alpha-pro-month"},
	}
	if result, err := sdk.IngestBillingEvent(ctx, BillingEvent{
		EventID: "evt-equal-created-" + runID, Provider: "alpha",
		Type: BillingEventSubscriptionCreated, OccurredAt: createdAt, AccountID: accountID,
		Subscription: &subscription,
	}); err != nil || result.Action != "subscription_created" {
		t.Fatalf("create subscription = %+v, error = %v", result, err)
	}

	var callbackCount atomic.Int32
	if err := sdk.Billing.OnEvent(BillingEventSubscriptionRenewed, func(context.Context, BillingEvent, string) {
		callbackCount.Add(1)
	}); err != nil {
		t.Fatal(err)
	}
	renewedAt := createdAt.Add(time.Minute)
	renewalStart := createdAt.Add(30 * 24 * time.Hour)
	renewalEnd := renewalStart.Add(30 * 24 * time.Hour)
	start := make(chan struct{})
	type ingestion struct {
		result BillingEventResult
		err    error
	}
	results := make(chan ingestion, 2)
	for index := 0; index < 2; index++ {
		index := index
		go func() {
			<-start
			current := subscription
			current.PeriodStart = &renewalStart
			current.PeriodEnd = &renewalEnd
			result, err := sdk.IngestBillingEvent(ctx, BillingEvent{
				EventID: "evt-equal-renewed-" + runID + "-" + string(rune('a'+index)), Provider: "alpha",
				Type: BillingEventSubscriptionRenewed, OccurredAt: renewedAt, AccountID: accountID,
				Subscription: &current,
			})
			results <- ingestion{result: result, err: err}
		}()
	}
	close(start)
	ignored := 0
	for range 2 {
		outcome := <-results
		if outcome.err != nil || !outcome.result.Handled {
			t.Fatalf("equal-timestamp renewal = %+v, error = %v", outcome.result, outcome.err)
		}
		if outcome.result.Ignored {
			ignored++
		}
	}
	if ignored != 1 {
		t.Fatalf("ignored equal-timestamp renewals = %d, want 1", ignored)
	}
	if callbackCount.Load() != 1 {
		t.Fatalf("renewal callbacks = %d, want 1", callbackCount.Load())
	}
	if balance, err := sdk.Credits.GetBalance(ctx, accountID); err != nil || !balance.Balance.Equal(MustAmount("12.345678")) {
		t.Fatalf("equal-timestamp cycle balance = %+v, error = %v", balance, err)
	}
}

func TestPostgresStaleRenewalCannotUndoTerminalEntitlement(t *testing.T) {
	ctx, cancel, config := financialContext(t)
	defer cancel()
	sdk, store := newFinancialSDK(t, ctx, config, &financialProvider{name: "alpha"})

	runID := uuid.NewString()
	accountID := uuid.NewString()
	subscriptionID := "sub-terminal-fence-" + runID
	createdAt := time.Now().UTC().Add(-time.Hour).Truncate(time.Second)
	periodEnd := createdAt.Add(30 * 24 * time.Hour)
	subscription := BillingSubscription{
		ProviderSubscriptionID: subscriptionID,
		Status:                 "active",
		Interval:               "month",
		IntervalCount:          1,
		PeriodStart:            &createdAt,
		PeriodEnd:              &periodEnd,
		Refs:                   &ProviderRef{PriceID: "alpha-pro-month"},
	}
	if result, err := sdk.IngestBillingEvent(ctx, BillingEvent{
		EventID: "evt-terminal-created-" + runID, Provider: "alpha",
		Type: BillingEventSubscriptionCreated, OccurredAt: createdAt, AccountID: accountID,
		Subscription: &subscription,
	}); err != nil || result.Action != "subscription_created" {
		t.Fatalf("create subscription = %+v, error = %v", result, err)
	}
	terminalAt := createdAt.Add(2 * time.Minute)
	if result, err := sdk.IngestBillingEvent(ctx, BillingEvent{
		EventID: "evt-terminal-canceled-" + runID, Provider: "alpha",
		Type: BillingEventSubscriptionCanceled, OccurredAt: terminalAt, AccountID: accountID,
		Subscription: &subscription,
	}); err != nil || result.Action != "subscription_canceled" || result.Ignored {
		t.Fatalf("cancel subscription = %+v, error = %v", result, err)
	}

	var callbackCount atomic.Int32
	if err := sdk.Billing.OnEvent(BillingEventSubscriptionRenewed, func(context.Context, BillingEvent, string) {
		callbackCount.Add(1)
	}); err != nil {
		t.Fatal(err)
	}
	renewedAt := createdAt.Add(time.Minute)
	staleResult, err := sdk.IngestBillingEvent(ctx, BillingEvent{
		EventID: "evt-terminal-stale-renewal-" + runID, Provider: "alpha",
		Type: BillingEventSubscriptionRenewed, OccurredAt: renewedAt, AccountID: accountID,
		Subscription: &subscription,
	})
	if err != nil || !staleResult.Handled || !staleResult.Ignored || staleResult.Action != "subscription_renewed" {
		t.Fatalf("stale renewal = %+v, error = %v", staleResult, err)
	}
	if callbackCount.Load() != 0 {
		t.Fatalf("stale renewal callbacks = %d, want 0", callbackCount.Load())
	}
	if balance, err := sdk.Credits.GetBalance(ctx, accountID); err != nil || !balance.Balance.Equal(DecimalZero) {
		t.Fatalf("stale renewal balance = %+v, error = %v", balance, err)
	}
	if plan, err := sdk.Credits.GetUserPlan(ctx, accountID); err != nil || plan.PlanKey != "" || plan.AssignmentSourceType != "" {
		t.Fatalf("terminal entitlement = %+v, error = %v", plan, err)
	}
	if current, err := store.GetBillingSubscriptionByProvider(ctx, "alpha", subscriptionID); err != nil || current == nil || current.Status != "canceled" {
		t.Fatalf("terminal subscription state = %+v, error = %v", current, err)
	}
}

func activeOfferID(t *testing.T, ctx context.Context, store *PostgresStore) string {
	t.Helper()
	offer, err := store.ResolveBillingOffer(ctx, "alpha", "", "alpha-pro-month", "")
	if err != nil || offer == nil {
		t.Fatalf("resolve race subscription offer = %+v, error = %v", offer, err)
	}
	return offer.ID
}
