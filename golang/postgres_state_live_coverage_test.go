// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import (
	"testing"
	"time"

	"github.com/google/uuid"
)

func TestPostgresStateGraceExpiryAndTenantIsolation(t *testing.T) {
	ctx, cancel, config := financialContext(t)
	defer cancel()
	sdk, store := newFinancialSDK(t, ctx, config, &financialProvider{name: "alpha"})
	runID := uuid.NewString()
	accountID := uuid.NewString()
	subscriptionID := "sub-grace-" + runID
	now := time.Now().UTC().Truncate(time.Second)
	periodEnd := now.Add(30 * 24 * time.Hour)
	created, err := sdk.IngestBillingEvent(ctx, BillingEvent{
		EventID: "evt-grace-created-" + runID, Provider: "alpha", Type: BillingEventSubscriptionCreated,
		OccurredAt: now, AccountID: accountID,
		Subscription: &BillingSubscription{
			ProviderSubscriptionID: subscriptionID, Status: "active", Interval: "month", IntervalCount: 1,
			PeriodStart: &now, PeriodEnd: &periodEnd, Refs: &ProviderRef{PriceID: "alpha-pro-month"},
		},
	})
	if err != nil {
		if typed, ok := AsBursarError(err); ok && typed.Unwrap() != nil {
			t.Fatalf("create subscription: %v: %v", err, typed.Unwrap())
		}
		t.Fatalf("create subscription: %v", err)
	}
	if created.Action != "subscription_created" {
		t.Fatalf("create subscription = %+v, error = %v", created, err)
	}
	active, err := store.GetBillingSubscriptionByProvider(ctx, "alpha", subscriptionID)
	if err != nil || active == nil || active.ID == "" {
		t.Fatalf("active subscription = %+v, error = %v", active, err)
	}
	activePlan, err := store.GetUserPlan(ctx, accountID)
	if err != nil || activePlan.AssignmentSourceType != "subscription" || activePlan.AssignmentSourceID != active.ID {
		t.Fatalf("active subscription entitlement = %+v, error = %v", activePlan, err)
	}
	graceEndsAt := now.Add(-time.Minute)
	providerUpdatedAt := now.Add(time.Minute)
	if _, err := store.UpsertBillingSubscriptionState(ctx, CommerceSubscription{
		AccountID: accountID, Provider: "alpha", ProviderSubscriptionID: subscriptionID,
		ProviderCustomerID: "cus-" + runID, OfferID: active.OfferID, Status: "past_due",
		CurrentPeriodStart: &now, CurrentPeriodEnd: &periodEnd, GraceEndsAt: &graceEndsAt,
		ProviderUpdatedAt: providerUpdatedAt, Metadata: map[string]any{"source": "grace-test"},
	}); err != nil {
		t.Fatalf("persist past-due grace state: %v", err)
	}
	candidates, err := store.ListExpiredGraceSubscriptions(ctx, now, 10)
	if err != nil {
		t.Fatalf("list expired grace subscriptions: %v", err)
	}
	found := false
	for _, candidate := range candidates {
		if candidate.ID == active.ID {
			found = true
			if candidate.Status != "past_due" || candidate.GraceEndsAt == nil || candidate.GraceExpiredAt != nil {
				t.Fatalf("grace candidate = %+v", candidate)
			}
		}
	}
	if !found {
		t.Fatalf("expired grace candidates did not contain subscription %s: %+v", active.ID, candidates)
	}
	expired, err := store.ExpirePastDueGracePeriods(ctx, now, 10)
	if err != nil {
		if typed, ok := AsBursarError(err); ok && typed.Unwrap() != nil {
			t.Fatalf("expire grace periods: %v: %v", err, typed.Unwrap())
		}
		t.Fatalf("expire grace periods: %v", err)
	}
	if expired < 1 {
		t.Fatalf("expire grace periods = %d, error = %v", expired, err)
	}
	plan, err := sdk.Credits.GetUserPlan(ctx, accountID)
	if err != nil || plan.PlanKey != "" {
		t.Fatalf("plan after grace expiry = %+v, error = %v", plan, err)
	}
	updated, err := store.GetBillingSubscriptionByProvider(ctx, "alpha", subscriptionID)
	if err != nil || updated == nil || updated.GraceExpiredAt == nil {
		t.Fatalf("expired subscription = %+v, error = %v", updated, err)
	}
	if expired, err := store.ExpirePastDueGracePeriods(ctx, now, 10); err != nil || expired != 0 {
		t.Fatalf("replayed grace expiry = %d, error = %v", expired, err)
	}
	manualAccountID := uuid.NewString()
	manualProviderSubscriptionID := "sub-manual-grace-" + uuid.NewString()
	if _, err := store.SetUserPlan(ctx, manualAccountID, "pro", SetUserPlanOptions{}); err != nil {
		t.Fatalf("assign manual grace plan: %v", err)
	}
	manualSubscriptionID, err := store.UpsertBillingSubscriptionState(ctx, CommerceSubscription{
		AccountID: manualAccountID, Provider: "alpha", ProviderSubscriptionID: manualProviderSubscriptionID,
		OfferID: active.OfferID, Status: "active", CurrentPeriodStart: &now, CurrentPeriodEnd: &periodEnd,
		ProviderUpdatedAt: now,
	})
	if err != nil {
		t.Fatalf("persist manual-source subscription: %v", err)
	}
	entitlementEvent := BillingEvent{
		EventID: "evt-manual-entitlement-" + runID, Provider: "alpha",
		Type: BillingEventSubscriptionCreated, OccurredAt: now, AccountID: manualAccountID,
		Subscription: &BillingSubscription{ProviderSubscriptionID: manualProviderSubscriptionID, Status: "active"},
	}
	claim, err := store.ClaimBillingEvent(ctx, entitlementEvent, billingEventClaimEnvelope(entitlementEvent))
	if err != nil || claim.State != BillingEventClaimed || claim.BillingEventID == "" {
		t.Fatalf("claim manual entitlement event = %+v, error = %v", claim, err)
	}
	outcome, err := store.reconcileSubscriptionEntitlement(ctx, manualAccountID, manualSubscriptionID, claim.BillingEventID, "active", now, &now, true, "", "subscription_created")
	if err != nil || outcome != subscriptionEntitlementApplied {
		t.Fatalf("bind subscription plan source = %q, error = %v", outcome, err)
	}
	if completed, err := store.CompleteBillingEvent(ctx, "alpha", entitlementEvent.EventID, claim.ClaimToken); err != nil || !completed {
		t.Fatalf("complete manual entitlement event = %v, error = %v", completed, err)
	}
	boundPlan, err := store.GetUserPlan(ctx, manualAccountID)
	if err != nil || boundPlan.AssignmentSourceType != "subscription" || boundPlan.AssignmentSourceID != manualSubscriptionID {
		t.Fatalf("subscription-bound plan = %+v, error = %v", boundPlan, err)
	}
	if _, err := store.SetUserPlan(ctx, manualAccountID, "pro", SetUserPlanOptions{}); err != nil {
		t.Fatalf("replace subscription source with manual assignment: %v", err)
	}
	manualPlan, err := store.GetUserPlan(ctx, manualAccountID)
	if err != nil || manualPlan.AssignmentSourceType != "manual" || manualPlan.AssignmentSourceID != "" {
		t.Fatalf("manual plan source = %+v, error = %v", manualPlan, err)
	}
	manualGraceEndsAt := now.Add(-2 * time.Minute)
	if _, err := store.UpsertBillingSubscriptionState(ctx, CommerceSubscription{
		AccountID: manualAccountID, Provider: "alpha", ProviderSubscriptionID: manualProviderSubscriptionID,
		OfferID: active.OfferID, Status: "past_due", CurrentPeriodStart: &now, CurrentPeriodEnd: &periodEnd,
		GraceEndsAt: &manualGraceEndsAt, ProviderUpdatedAt: now.Add(time.Minute),
	}); err != nil {
		t.Fatalf("persist manual-source grace state: %v", err)
	}
	if expired, err := store.ExpirePastDueGracePeriods(ctx, now, 10); err != nil || expired < 1 {
		t.Fatalf("expire manual-source grace = %d, error = %v", expired, err)
	}
	manualPlan, err = store.GetUserPlan(ctx, manualAccountID)
	if err != nil || manualPlan.PlanKey != "pro" || manualPlan.AssignmentSourceType != "manual" {
		t.Fatalf("manual plan was revoked by grace expiry = %+v, error = %v", manualPlan, err)
	}

	if config.secondaryTenantID == "" {
		t.Fatal("BURSAR_SECONDARY_TENANT_ID is required for grace tenant isolation")
	}
	secondary, err := NewPostgresStore(ctx, config.databaseURL, PostgresStoreOptions{
		TenantID: config.secondaryTenantID, ProviderEnvironment: config.providerEnvironment, MaxConnections: 4,
	})
	if err != nil {
		t.Fatalf("construct secondary store: %v", err)
	}
	t.Cleanup(func() { _ = secondary.Close() })
	secondaryCandidates, err := secondary.ListExpiredGraceSubscriptions(ctx, now, 10)
	if err != nil {
		t.Fatalf("secondary expired grace subscriptions: %v", err)
	}
	for _, candidate := range secondaryCandidates {
		if candidate.ID == active.ID || candidate.AccountID == accountID {
			t.Fatalf("secondary tenant observed primary grace candidate: %+v", candidate)
		}
	}
}
