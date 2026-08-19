// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import (
	"testing"
	"time"

	"github.com/google/uuid"
)

func TestPostgresBillingPersistenceSurface(t *testing.T) {
	ctx, cancel, config := financialContext(t)
	defer cancel()
	provider := &financialProvider{name: "alpha"}
	sdk, store := newFinancialSDK(t, ctx, config, provider)
	accountID := uuid.NewString()
	runID := uuid.NewString()
	now := time.Now().UTC().Truncate(time.Second)
	periodEnd := now.Add(30 * 24 * time.Hour)

	if err := sdk.Billing.UpsertCustomer(ctx, "alpha", "cus-"+runID, accountID, "billing@example.com"); err != nil {
		t.Fatalf("upsert customer: %v", err)
	}
	customer, err := sdk.Billing.GetCustomerByUserID(ctx, accountID, "alpha")
	if err != nil || customer == nil || customer.ProviderCustomerID != "cus-"+runID {
		t.Fatalf("customer = %+v, error = %v", customer, err)
	}
	byProvider, err := store.GetBillingCustomerByProvider(ctx, "alpha", "cus-"+runID)
	if err != nil || byProvider == nil || byProvider.AccountID != accountID {
		t.Fatalf("provider customer = %+v, error = %v", byProvider, err)
	}
	catalog := financialCatalogConfig(t)
	commerce := catalog["commerce"].(map[string]any)
	offers := commerce["offers"].(map[string]any)
	offers["pro_year"] = map[string]any{
		"type": "subscription", "display_name": "Pro yearly", "plan": "pro",
		"billing_interval": map[string]any{"unit": "year", "count": 1},
		"price":            map[string]any{"amount_minor": 9000, "currency": "USD"},
		"providers":        map[string]any{"alpha": map[string]any{"type": "stripe_price", "price_id": "alpha-pro-year"}},
	}
	if _, err := sdk.Credits.PublishAndActivateCatalog(ctx, catalog, "go-persistence-billing", newAssignmentsRollout(catalog)); err != nil {
		t.Fatalf("publish isolated change catalog: %v", err)
	}
	offer, err := sdk.Billing.ResolveOffer(ctx, "alpha", "", "alpha-pro-month")
	if err != nil || offer == nil || offer.ID == "" {
		t.Fatalf("resolved offer = %+v, error = %v", offer, err)
	}

	subscriptionID := "sub-" + runID
	internalSubscriptionID, err := store.UpsertBillingSubscriptionState(ctx, CommerceSubscription{
		Provider: "alpha", ProviderSubscriptionID: subscriptionID, AccountID: accountID,
		ProviderCustomerID: "cus-" + runID, OfferID: offer.ID, Status: "active",
		CurrentPeriodStart: &now, CurrentPeriodEnd: &periodEnd, ProviderUpdatedAt: now,
	})
	if err != nil || internalSubscriptionID == "" {
		t.Fatalf("upsert subscription = %q, error = %v", internalSubscriptionID, err)
	}
	bySubscription, err := store.GetBillingSubscriptionByProvider(ctx, "alpha", subscriptionID)
	if err != nil || bySubscription == nil || bySubscription.AccountID != accountID {
		t.Fatalf("provider subscription = %+v, error = %v", bySubscription, err)
	}
	allSubscriptions, err := sdk.Billing.GetUserSubscription(ctx, accountID)
	if err != nil || allSubscriptions == nil || allSubscriptions.ProviderSubscriptionID != subscriptionID {
		t.Fatalf("user subscription = %+v, error = %v", allSubscriptions, err)
	}
	targetOffer, err := sdk.Billing.ResolveOffer(ctx, "alpha", "", "alpha-pro-year")
	if err != nil || targetOffer == nil || targetOffer.ID == offer.ID {
		t.Fatalf("target offer = %+v, error = %v", targetOffer, err)
	}
	changeInput := BillingSubscriptionChangeCreate{Provider: "alpha", ProviderSubscriptionID: subscriptionID, ToOfferID: targetOffer.ID, ToOfferKey: targetOffer.OfferKey, ToPlanKey: targetOffer.PlanKey, ToInterval: targetOffer.Interval, Effective: "renewal", EffectiveAt: periodEnd, ProrationBehavior: "none", OperationKey: "persist-change-" + runID}
	change, err := sdk.Billing.CreateBillingSubscriptionChange(ctx, changeInput)
	if err != nil || change.ID == "" || change.State != "scheduled" {
		t.Fatalf("create subscription change = %+v, error = %v", change, err)
	}
	replayedChange, err := sdk.Billing.CreateBillingSubscriptionChange(ctx, changeInput)
	if err != nil || replayedChange.ID != change.ID {
		t.Fatalf("subscription change replay = %+v, error = %v", replayedChange, err)
	}
	openChange, err := sdk.Billing.GetOpenBillingSubscriptionChange(ctx, "alpha", subscriptionID)
	if err != nil || openChange == nil || openChange.ID != change.ID {
		t.Fatalf("open subscription change = %+v, error = %v", openChange, err)
	}
	applied := "applied"
	providerOperationID := "provider-op-" + runID
	if err := sdk.Billing.UpdateBillingSubscriptionChange(ctx, change.ID, BillingSubscriptionChangeUpdate{State: &applied, ProviderOperationID: &providerOperationID}); err != nil {
		t.Fatalf("apply subscription change: %v", err)
	}
	if openChange, err := sdk.Billing.GetOpenBillingSubscriptionChange(ctx, "alpha", subscriptionID); err != nil || openChange != nil {
		t.Fatalf("open change after apply = %+v, error = %v", openChange, err)
	}

	preferences := BillingPreferences{AccountID: accountID, AutoRecharge: true, OverageProtection: true, EmailNotifications: true, UsageAlerts: true, InvoiceReminders: false}
	if err := sdk.Billing.UpdateUserPreferences(ctx, preferences); err != nil {
		t.Fatalf("update preferences: %v", err)
	}
	gotPreferences, err := sdk.Billing.GetUserPreferences(ctx, accountID)
	if err != nil || gotPreferences == nil || !gotPreferences.AutoRecharge || gotPreferences.InvoiceReminders {
		t.Fatalf("preferences = %+v, error = %v", gotPreferences, err)
	}

	invoiceID := "inv-" + runID
	if err := store.UpsertBillingInvoiceState(ctx, BillingInvoiceUpsert{Provider: "alpha", ProviderInvoiceID: invoiceID, ProviderSubscriptionID: subscriptionID, AccountID: accountID, Status: "paid", AmountPaidMinor: 1000, AmountDueMinor: 1000, Currency: "USD", PeriodStart: &now, PeriodEnd: &periodEnd, ProviderUpdatedAt: now}); err != nil {
		t.Fatalf("upsert invoice: %v", err)
	}
	invoices, err := sdk.Billing.ListBillingInvoices(ctx, accountID)
	if err != nil || len(invoices) != 1 || invoices[0].canonicalProviderInvoiceID() != invoiceID {
		t.Fatalf("invoices = %+v, error = %v", invoices, err)
	}

	paymentID := "pay-" + runID
	internalPaymentID, err := store.UpsertBillingPaymentState(ctx, BillingPaymentUpsert{Provider: "alpha", ProviderPaymentID: paymentID, AccountID: accountID, AmountMinor: 500, Currency: "USD", Purpose: "credit_topup", Status: "succeeded", ProviderUpdatedAt: now})
	if err != nil || internalPaymentID == "" {
		t.Fatalf("upsert payment = %q, error = %v", internalPaymentID, err)
	}
	payment, err := store.GetBillingPaymentByProvider(ctx, "alpha", paymentID)
	if err != nil || payment == nil || payment.AmountMinor != 500 {
		t.Fatalf("payment = %+v, error = %v", payment, err)
	}
	refundID, err := store.UpsertBillingRefundState(ctx, BillingRefundUpsert{Provider: "alpha", ProviderRefundID: "refund-" + runID, ProviderPaymentID: paymentID, AccountID: accountID, AmountMinor: 100, Currency: "USD", Reason: "requested_by_customer", Status: "succeeded", ProviderUpdatedAt: now})
	if err != nil || refundID == "" {
		t.Fatalf("upsert refund = %q, error = %v", refundID, err)
	}
	if _, err := store.UpsertBillingRefundState(ctx, BillingRefundUpsert{Provider: "alpha", ProviderRefundID: "refund-missing-" + runID, ProviderPaymentID: "missing-" + runID, AccountID: accountID, AmountMinor: 100, Currency: "USD", Reason: "requested_by_customer", Status: "succeeded", ProviderUpdatedAt: now}); err == nil {
		t.Fatal("refund for unknown payment was accepted")
	}

	claimEvent := BillingEvent{EventID: "evt-claim-" + runID, Provider: "alpha", Type: BillingEventCustomerCreated, OccurredAt: now, AccountID: accountID, Customer: &BillingCustomer{ProviderCustomerID: "cus-" + runID}}
	claim, err := store.ClaimBillingEvent(ctx, claimEvent, map[string]any{"source": "integration"})
	if err != nil || claim.State != BillingEventClaimed || claim.ClaimToken == "" {
		t.Fatalf("claim = %+v, error = %v", claim, err)
	}
	resolvedAccount, err := store.ResolveBillingEventAccount(ctx, claimEvent)
	if err != nil || resolvedAccount != accountID {
		t.Fatalf("resolved event account = %q, error = %v", resolvedAccount, err)
	}
	completed, err := store.CompleteBillingEvent(ctx, "alpha", claimEvent.EventID, claim.ClaimToken)
	if err != nil || !completed {
		t.Fatalf("complete claim = %v, error = %v", completed, err)
	}
	duplicateClaim, err := store.ClaimBillingEvent(ctx, claimEvent, map[string]any{"source": "integration"})
	if err != nil || duplicateClaim.State != BillingEventDuplicate {
		t.Fatalf("duplicate claim = %+v, error = %v", duplicateClaim, err)
	}

	retryEvent := claimEvent
	retryEvent.EventID = "evt-retry-" + runID
	retryClaim, err := store.ClaimBillingEvent(ctx, retryEvent, nil)
	if err != nil || retryClaim.State != BillingEventClaimed {
		t.Fatalf("retry claim = %+v, error = %v", retryClaim, err)
	}
	failed, err := store.FailBillingEvent(ctx, "alpha", retryEvent.EventID, retryClaim.ClaimToken, "provider timeout")
	if err != nil || !failed {
		t.Fatalf("fail claim = %v, error = %v", failed, err)
	}
	retryAgain, err := store.ClaimBillingEvent(ctx, retryEvent, nil)
	if err != nil || retryAgain.State != BillingEventClaimed {
		t.Fatalf("reclaimed event = %+v, error = %v", retryAgain, err)
	}
	completed, err = store.CompleteBillingEvent(ctx, "alpha", retryEvent.EventID, retryAgain.ClaimToken)
	if err != nil || !completed {
		t.Fatalf("complete retry = %v, error = %v", completed, err)
	}
}

// TestPostgresCreditPersistenceSurface keeps the high-value read/write
// projections together: each assertion is against the committed PostgreSQL
// result, rather than an in-memory service projection.
func TestPostgresCreditPersistenceSurface(t *testing.T) {
	ctx, cancel, config := financialContext(t)
	defer cancel()
	store := openPostgresIntegrationStore(t, ctx, config, config.tenantID)
	defer store.Close()
	sdk, err := New(Options{CreditStore: store, BillingStore: store})
	if err != nil {
		t.Fatalf("construct facade: %v", err)
	}
	defer sdk.Close()

	userID := uuid.NewString()
	grant, err := sdk.Credits.AddCredits(ctx, userID, MustAmount("20.125000"), AddCreditsOptions{
		Type: "purchase", Bucket: "purchased", IdempotencyKey: "persist-grant-" + userID,
	})
	if err != nil {
		t.Fatalf("grant credits: %v", err)
	}
	if grant.EntryID == "" || !grant.NewBalance.Equal(MustAmount("20.125000")) {
		t.Fatalf("grant = %+v", grant)
	}

	usage, err := sdk.Credits.RecordUsage(ctx, userID, "completion", MustAmount("1.250000"), RecordUsageOptions{
		OperationUsageOptions: OperationUsageOptions{Model: "model-a", Region: "us-east"},
		IdempotencyKey:        "persist-usage-" + userID,
	})
	if err != nil {
		t.Fatalf("record usage: %v", err)
	}
	if usage.UsageID == "" {
		t.Fatal("record usage returned no usage ID")
	}
	charged, err := sdk.Credits.Deduct(ctx, userID, MustAmount("2.125000"), DeductWithAllowanceOptions{
		Operation: "completion", OperationUsageOptions: OperationUsageOptions{Model: "model-a", Region: "us-east"},
		IdempotencyKey: "persist-deduct-" + userID,
	})
	if err != nil {
		t.Fatalf("deduct credits: %v", err)
	}
	if charged.EntryID == "" || charged.UsageChargeID == "" {
		t.Fatalf("deduction = %+v", charged)
	}

	ledger, err := sdk.Credits.ListLedgerEntries(ctx, userID, ListLedgerEntriesOptions{Limit: 10})
	if err != nil || len(ledger.Items) < 2 {
		t.Fatalf("ledger = %+v, error = %v", ledger, err)
	}
	entry, err := sdk.Credits.GetLedgerEntry(ctx, userID, charged.EntryID)
	if err != nil || entry == nil || entry.EntryID != charged.EntryID {
		t.Fatalf("ledger entry = %+v, error = %v", entry, err)
	}
	usagePage, err := sdk.Credits.ListUsageCharges(ctx, userID, ListUsageChargesOptions{Limit: 10})
	if err != nil || len(usagePage.Items) == 0 {
		t.Fatalf("usage charges = %+v, error = %v", usagePage, err)
	}

	lease, err := sdk.Credits.Reserve(ctx, userID, MustAmount("3"), ReserveOptions{OperationType: "completion", IdempotencyKey: "persist-lease-" + userID, TTL: time.Minute})
	if err != nil || lease.LeaseID == "" {
		t.Fatalf("reserve = %+v, error = %v", lease, err)
	}
	if _, err := sdk.Credits.Renew(ctx, userID, lease.LeaseID, 2*time.Minute); err != nil {
		t.Fatalf("renew lease: %v", err)
	}
	released, err := sdk.Credits.Release(ctx, userID, lease.LeaseID)
	if err != nil || released.LeaseID != lease.LeaseID {
		t.Fatalf("release = %+v, error = %v", released, err)
	}
	releasedAgain, err := sdk.Credits.Release(ctx, userID, lease.LeaseID)
	if err != nil || releasedAgain.LeaseID != lease.LeaseID {
		t.Fatalf("release replay = %+v, error = %v", releasedAgain, err)
	}

	start := time.Now().UTC().Add(-time.Hour)
	end := time.Now().UTC().Add(time.Hour)
	if rows, err := sdk.Credits.SpendByUser(ctx, start, end); err != nil || len(rows) == 0 {
		t.Fatalf("spend by user = %+v, error = %v", rows, err)
	}
	if rows, err := sdk.Credits.SpendByModel(ctx, start, end); err != nil || len(rows) == 0 {
		t.Fatalf("spend by model = %+v, error = %v", rows, err)
	}
	if rows, err := sdk.Credits.TopUsers(ctx, 10, start, end); err != nil || len(rows) == 0 {
		t.Fatalf("top users = %+v, error = %v", rows, err)
	}
	if rows, err := sdk.Credits.DailySpend(ctx, start, end); err != nil || len(rows) == 0 {
		t.Fatalf("daily spend = %+v, error = %v", rows, err)
	}
	stats, err := sdk.Credits.AggregateStats(ctx, start, end)
	if err != nil || stats.TotalCreditsConsumed.LessThan(MustAmount("2.125000")) {
		t.Fatalf("aggregate stats = %+v, error = %v", stats, err)
	}

	team, err := sdk.Credits.CreateTeam(ctx, uuid.NewString(), "persist-team", CreateTeamOptions{IdempotencyKey: "persist-team-" + userID, InitialBalance: MustAmount("5")})
	if err != nil || team.TeamID == "" {
		t.Fatalf("create team = %+v, error = %v", team, err)
	}
	member, err := sdk.Credits.AddTeamMember(ctx, team.TeamID, userID, AddTeamMemberOptions{Role: TeamRoleMember})
	if err != nil || member.UserID != userID {
		t.Fatalf("add team member = %+v, error = %v", member, err)
	}
	teamDebit, err := sdk.Credits.DeductTeam(ctx, team.TeamID, userID, MustAmount("1"), TeamDeductionOptions{Operation: "team_completion", IdempotencyKey: "persist-team-debit-" + userID})
	if err != nil || teamDebit.EntryID == "" {
		t.Fatalf("team debit = %+v, error = %v", teamDebit, err)
	}
	teamReplay, err := sdk.Credits.DeductTeam(ctx, team.TeamID, userID, MustAmount("1"), TeamDeductionOptions{Operation: "team_completion", IdempotencyKey: "persist-team-debit-" + userID})
	if err != nil || !teamReplay.Idempotent || teamReplay.EntryID != teamDebit.EntryID {
		t.Fatalf("team replay = %+v, error = %v", teamReplay, err)
	}
	if members, err := sdk.Credits.GetTeamMembers(ctx, team.TeamID); err != nil || len(members) < 2 {
		t.Fatalf("team members = %+v, error = %v", members, err)
	}
	if balance, err := sdk.Credits.GetTeamBalance(ctx, team.TeamID); err != nil || balance == nil || !balance.Balance.Equal(MustAmount("4")) {
		t.Fatalf("team balance = %+v, error = %v", balance, err)
	}

	active, err := sdk.Credits.GetActiveCatalog(ctx)
	if err != nil || active == nil || active.Version < 1 {
		t.Fatalf("active catalog = %+v, error = %v", active, err)
	}
	history, err := sdk.Credits.GetCatalogHistory(ctx)
	if err != nil || len(history) == 0 {
		t.Fatalf("catalog history = %+v, error = %v", history, err)
	}
	revision, err := sdk.Credits.GetCatalogRevision(ctx, active.Version)
	if err != nil || revision == nil || revision.Version != active.Version {
		t.Fatalf("catalog revision = %+v, error = %v", revision, err)
	}
}
