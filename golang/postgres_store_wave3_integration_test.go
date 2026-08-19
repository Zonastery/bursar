// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import (
	"context"
	"testing"
	"time"

	"github.com/google/uuid"
)

func TestPostgresStoreCatalogPlanQuotaAndMigrationSurface(t *testing.T) {
	ctx, cancel, config := financialContext(t)
	defer cancel()
	store := openPostgresIntegrationStore(t, ctx, config, config.tenantID)
	defer store.Close()
	sdk, err := New(Options{CreditStore: store})
	if err != nil {
		t.Fatalf("construct credits facade: %v", err)
	}
	defer sdk.Close()

	active, err := store.GetActiveCatalog(ctx)
	if err != nil || active == nil || active.Version < 1 {
		t.Fatalf("active catalog = %+v, error = %v", active, err)
	}
	history, err := store.GetCatalogHistory(ctx)
	if err != nil || len(history) == 0 {
		t.Fatalf("catalog history = %+v, error = %v", history, err)
	}
	revision, err := store.GetCatalogRevision(ctx, active.Version)
	if err != nil || revision == nil || revision.ID != active.ID {
		t.Fatalf("catalog revision = %+v, error = %v", revision, err)
	}
	if _, err := store.GetCatalogRevision(ctx, 0); err == nil {
		t.Fatal("catalog version zero accepted")
	}

	draftID, err := store.PublishCatalogDraft(ctx, active.Config, "go-wave3-draft-"+uuid.NewString())
	if err != nil || draftID == "" {
		t.Fatalf("publish catalog draft = %q, error = %v", draftID, err)
	}
	draftVersion := 0
	for _, summary := range mustCatalogHistory(t, store, ctx) {
		if summary.ID == draftID {
			draftVersion = summary.Version
			break
		}
	}
	if draftVersion < 1 {
		t.Fatalf("published draft %q was not present in catalog history", draftID)
	}
	rollout := newAssignmentsRollout(active.Config)
	if _, err := store.ActivateCatalogRevision(ctx, draftVersion, rollout); err != nil {
		t.Fatalf("activate draft revision: %v", err)
	}
	if _, err := store.ActivateCatalogRevision(ctx, active.Version, rollout); err != nil {
		t.Fatalf("restore active revision: %v", err)
	}

	userID := uuid.NewString()
	assigned, err := sdk.Credits.SetUserPlan(ctx, userID, "pro", SetUserPlanOptions{})
	if err != nil || assigned.PlanKey != "pro" || assigned.PlanID == "" {
		t.Fatalf("set plan = %+v, error = %v", assigned, err)
	}
	pinned, err := sdk.Credits.SetPlanRevisionPin(ctx, userID, true)
	if err != nil || !pinned {
		t.Fatalf("pin plan = %v, error = %v", pinned, err)
	}
	plan, err := sdk.Credits.GetUserPlan(ctx, userID)
	if err != nil || !plan.CatalogRevisionPinned || plan.PlanID != assigned.PlanID {
		t.Fatalf("pinned plan = %+v, error = %v", plan, err)
	}
	if _, err := sdk.Credits.SetPlanRevisionPin(ctx, userID, false); err != nil {
		t.Fatalf("unpin plan: %v", err)
	}
	feature, err := sdk.Credits.CheckFeature(ctx, userID, "missing-feature")
	if err != nil || feature.HasFeature {
		t.Fatalf("missing feature = %+v, error = %v", feature, err)
	}
	if _, err := sdk.Credits.CheckAllowance(ctx, userID); err != nil {
		t.Fatalf("check allowance: %v", err)
	}
	if _, err := sdk.Credits.GetQuotaState(ctx, userID, ""); err != nil {
		t.Fatalf("quota state: %v", err)
	}
	if _, err := sdk.Credits.ListQuotaEvents(ctx, userID, ListQuotaEventsOptions{Limit: 10}); err != nil {
		t.Fatalf("quota events: %v", err)
	}
	if _, err := sdk.Credits.ListQuotaEvents(ctx, userID, ListQuotaEventsOptions{AfterID: "cursor-without-time"}); err == nil {
		t.Fatal("quota event cursor without timestamp accepted")
	}
	if _, err := sdk.Credits.ApplyDuePlanChanges(ctx, 10); err != nil {
		t.Fatalf("apply due plan changes: %v", err)
	}
	if _, err := sdk.Credits.ApplyDuePlanChanges(ctx, maxMaintenanceBatchSize+1); err == nil {
		t.Fatal("oversized plan change batch accepted")
	}
	if _, err := sdk.Credits.StartPlanMigration(ctx, "", ""); err == nil {
		t.Fatal("migration without target plan accepted")
	}
	if _, err := sdk.Credits.MigratePlanBatch(ctx, uuid.NewString(), 0); err == nil {
		t.Fatal("migration for unknown migration accepted")
	}
	if _, err := sdk.Credits.MigratePlanBatch(ctx, uuid.NewString(), maxMaintenanceBatchSize+1); err == nil {
		t.Fatal("oversized migration batch accepted")
	}
	if _, err := sdk.Credits.UnsetUserPlan(ctx, userID); err != nil {
		t.Fatalf("unset plan: %v", err)
	}
}

func mustCatalogHistory(t *testing.T, store *PostgresStore, ctx context.Context) []CatalogRevisionSummary {
	t.Helper()
	history, err := store.GetCatalogHistory(ctx)
	if err != nil {
		t.Fatalf("read catalog history: %v", err)
	}
	return history
}

func TestPostgresStoreLedgerUsageLeaseAndCorrectionSurface(t *testing.T) {
	ctx, cancel, config := financialContext(t)
	defer cancel()
	store := openPostgresIntegrationStore(t, ctx, config, config.tenantID)
	defer store.Close()
	sdk, err := New(Options{CreditStore: store})
	if err != nil {
		t.Fatalf("construct credits facade: %v", err)
	}
	defer sdk.Close()

	userID := uuid.NewString()
	if _, err := sdk.Credits.AddCredits(ctx, userID, MustAmount("12"), AddCreditsOptions{Type: "purchase", Bucket: "purchased", IdempotencyKey: "wave3-grant-" + userID}); err != nil {
		t.Fatalf("fund ledger user: %v", err)
	}
	if _, err := sdk.Credits.RecordUsage(ctx, userID, "completion", MustAmount("1"), RecordUsageOptions{IdempotencyKey: "wave3-record-" + userID}); err != nil {
		t.Fatalf("record usage: %v", err)
	}
	first, err := sdk.Credits.Deduct(ctx, userID, MustAmount("2"), DeductWithAllowanceOptions{Operation: "completion", OperationUsageOptions: OperationUsageOptions{Model: "wave3-model"}, IdempotencyKey: "wave3-deduct-1-" + userID})
	if err != nil {
		t.Fatalf("first deduction: %v", err)
	}
	if _, err := sdk.Credits.Deduct(ctx, userID, MustAmount("1"), DeductWithAllowanceOptions{Operation: "completion", OperationUsageOptions: OperationUsageOptions{Model: "wave3-model"}, IdempotencyKey: "wave3-deduct-2-" + userID}); err != nil {
		t.Fatalf("second deduction: %v", err)
	}

	page, err := sdk.Credits.ListLedgerEntries(ctx, userID, ListLedgerEntriesOptions{Limit: 1})
	if err != nil || len(page.Items) != 1 || page.NextCursor == nil {
		t.Fatalf("first ledger page = %+v, error = %v", page, err)
	}
	next, err := sdk.Credits.ListLedgerEntries(ctx, userID, ListLedgerEntriesOptions{Limit: 1, Cursor: page.NextCursor})
	if err != nil || len(next.Items) != 1 || next.Items[0].EntryID == page.Items[0].EntryID {
		t.Fatalf("cursor ledger page = %+v, error = %v", next, err)
	}
	usagePage, err := sdk.Credits.ListUsageEntries(ctx, userID, ListLedgerEntriesOptions{Limit: 1})
	if err != nil || len(usagePage.Items) == 0 {
		t.Fatalf("usage ledger page = %+v, error = %v", usagePage, err)
	}
	charges, err := sdk.Credits.ListUsageCharges(ctx, userID, ListUsageChargesOptions{Limit: 1})
	if err != nil || len(charges.Items) == 0 {
		t.Fatalf("usage charges = %+v, error = %v", charges, err)
	}
	if charges.NextCursor != nil {
		if _, err := sdk.Credits.ListUsageCharges(ctx, userID, ListUsageChargesOptions{Limit: 1, Cursor: charges.NextCursor}); err != nil {
			t.Fatalf("usage charge cursor: %v", err)
		}
	}
	from, to := time.Now().UTC(), time.Now().UTC().Add(-time.Minute)
	if _, err := sdk.Credits.ListLedgerEntries(ctx, userID, ListLedgerEntriesOptions{From: &from, To: &to}); err == nil {
		t.Fatal("reversed ledger range accepted")
	}
	if _, err := sdk.Credits.ListUsageCharges(ctx, userID, ListUsageChargesOptions{From: &from, To: &to}); err == nil {
		t.Fatal("reversed usage range accepted")
	}
	if _, err := sdk.Credits.ListLedgerEntries(ctx, userID, ListLedgerEntriesOptions{Cursor: &LedgerCursor{EntryID: first.EntryID}}); err == nil {
		t.Fatal("ledger cursor without timestamp accepted")
	}

	refund, err := sdk.Credits.RefundCredits(ctx, first.EntryID, nil, "wave3 correction", nil, "wave3-refund-"+userID)
	if err != nil || refund.RefundEntryID == "" || refund.Amount == nil {
		t.Fatalf("refund = %+v, error = %v", refund, err)
	}
	replayed, err := sdk.Credits.RefundCredits(ctx, first.EntryID, nil, "wave3 correction", nil, "wave3-refund-"+userID)
	if err != nil || replayed.RefundEntryID != refund.RefundEntryID {
		t.Fatalf("refund replay = %+v, error = %v", replayed, err)
	}

	revokeUser := uuid.NewString()
	if _, err := sdk.Credits.AddCredits(ctx, revokeUser, MustAmount("3"), AddCreditsOptions{Type: "purchase", Bucket: "purchased", IdempotencyKey: "wave3-revoke-" + revokeUser}); err != nil {
		t.Fatalf("fund revoke user: %v", err)
	}
	revoked, err := sdk.Credits.RevokeCreditsByEntryType(ctx, revokeUser, "purchase")
	if err != nil || !revoked.Revoked.Equal(MustAmount("3")) {
		t.Fatalf("revocation = %+v, error = %v", revoked, err)
	}
	sweep, err := sdk.Credits.SweepExpiredCredits(ctx, true, revokeUser, 10)
	if err != nil || !sweep.DryRun {
		t.Fatalf("dry-run sweep = %+v, error = %v", sweep, err)
	}

	leaseUser := uuid.NewString()
	if _, err := sdk.Credits.AddCredits(ctx, leaseUser, MustAmount("5"), AddCreditsOptions{Type: "purchase", Bucket: "purchased", IdempotencyKey: "wave3-lease-fund-" + leaseUser}); err != nil {
		t.Fatalf("fund lease user: %v", err)
	}
	lease, err := sdk.Credits.Reserve(ctx, leaseUser, MustAmount("2"), ReserveOptions{OperationType: "completion", IdempotencyKey: "wave3-lease-" + leaseUser, TTL: time.Minute})
	if err != nil || lease.LeaseID == "" {
		t.Fatalf("reserve lease = %+v, error = %v", lease, err)
	}
	if pricing, err := store.GetLeasePricingContext(ctx, leaseUser, lease.LeaseID); err != nil || pricing == nil {
		t.Fatalf("lease pricing context = %+v, error = %v", pricing, err)
	}
	if renewed, err := sdk.Credits.Renew(ctx, leaseUser, lease.LeaseID, 2*time.Minute); err != nil || renewed.LeaseID != lease.LeaseID {
		t.Fatalf("renew lease = %+v, error = %v", renewed, err)
	}
	settled, err := sdk.Credits.Settle(ctx, leaseUser, lease.LeaseID, MustAmount("1"), SettleOptions{IdempotencyKey: "wave3-settle-" + leaseUser})
	if err != nil || settled.EntryID == "" {
		t.Fatalf("settle lease = %+v, error = %v", settled, err)
	}
	settledReplay, err := sdk.Credits.Settle(ctx, leaseUser, lease.LeaseID, MustAmount("1"), SettleOptions{IdempotencyKey: "wave3-settle-" + leaseUser})
	if err != nil || !settledReplay.Idempotent {
		t.Fatalf("settle replay = %+v, error = %v", settledReplay, err)
	}
	if _, err := sdk.Credits.Renew(ctx, leaseUser, uuid.NewString(), time.Minute); err == nil {
		t.Fatal("renew of unknown lease succeeded")
	}
	if _, err := sdk.Credits.Settle(ctx, leaseUser, uuid.NewString(), MustAmount("1"), SettleOptions{IdempotencyKey: "wave3-unknown-settle"}); err == nil {
		t.Fatal("settle of unknown lease succeeded")
	}
	if _, err := store.ExpireLeases(ctx, 10); err != nil {
		t.Fatalf("expire leases: %v", err)
	}
}
