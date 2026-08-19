// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import (
	"context"
	"errors"
	"testing"
	"time"
)

// creditsSurfaceStore is a delegation-only fake. It deliberately embeds the
// portable contract; each overridden method returns a committed-shaped value
// so the service tests exercise orchestration and invariant checks without
// rebuilding accounting in a test double.
type creditsSurfaceStore struct {
	CreditStore
	addCredits     func(context.Context, string, Amount, AddCreditsOptions) (AddCreditsResult, error)
	deduct         func(context.Context, string, Amount, DeductWithAllowanceOptions) (DeductionResult, error)
	createLease    func(context.Context, string, Amount, string, CreateLeaseOptions) (LeaseResult, error)
	settleLease    func(context.Context, string, string, Amount, SettleLeaseOptions) (DeductionResult, error)
	releaseLease   func(context.Context, string, string) (ReleaseResult, error)
	renewLease     func(context.Context, string, string, time.Duration) (LeaseResult, error)
	getAvailable   func(context.Context, string) (AvailableResult, error)
	checkAllowance func(context.Context, string) (*AllowanceResult, error)
	checkFeature   func(context.Context, string, string) (CheckFeatureResult, error)
	getUserPlan    func(context.Context, string) (GetUserPlanResult, error)
	setUserPlan    func(context.Context, string, string, SetUserPlanOptions) (SetUserPlanResult, error)
	recordUsage    func(context.Context, string, string, Amount, RecordUsageOptions) (UsageRecordResult, error)
	refund         func(context.Context, string, *Amount, string, CreditMetadata, string) (RefundResult, error)
	deductTeam     func(context.Context, string, string, Amount, TeamDeductionOptions) (TeamDeductionResult, error)
	sweep          func(context.Context, bool, string, int) (SweepResult, error)
	activeCatalog  *CatalogRevision
	leasePricing   *LeasePricingContext
	teamBalance    *TeamBalanceResult
	teamBalanceSet bool
	quotaEvents    []QuotaEvent
	quotaEventsErr error
	closeErr       error
	settleAttempts int
}

func (s *creditsSurfaceStore) Close() error { return s.closeErr }
func (s *creditsSurfaceStore) AddCredits(ctx context.Context, user string, amount Amount, opts AddCreditsOptions) (AddCreditsResult, error) {
	if s.addCredits != nil {
		return s.addCredits(ctx, user, amount, opts)
	}
	return AddCreditsResult{EntryID: "grant-1", UserID: user, Amount: amount, NewBalance: MustAmount("9"), Idempotent: false}, nil
}
func (s *creditsSurfaceStore) DeductWithAllowance(ctx context.Context, user string, amount Amount, opts DeductWithAllowanceOptions) (DeductionResult, error) {
	if s.deduct != nil {
		return s.deduct(ctx, user, amount, opts)
	}
	balance := MustAmount("7")
	return DeductionResult{EntryID: "debit-1", UsageChargeID: "usage-1", UserID: user, Amount: amount, BalanceAfter: &balance}, nil
}
func (s *creditsSurfaceStore) CreateLease(ctx context.Context, user string, amount Amount, operation string, opts CreateLeaseOptions) (LeaseResult, error) {
	if s.createLease != nil {
		return s.createLease(ctx, user, amount, operation, opts)
	}
	return surfaceCommittedLease(user, amount), nil
}
func (s *creditsSurfaceStore) SettleLease(ctx context.Context, user, lease string, amount Amount, opts SettleLeaseOptions) (DeductionResult, error) {
	if s.settleLease != nil {
		return s.settleLease(ctx, user, lease, amount, opts)
	}
	balance := MustAmount("7")
	return DeductionResult{EntryID: "settle-1", UsageChargeID: "usage-1", UserID: user, Amount: amount, BalanceAfter: &balance}, nil
}
func (s *creditsSurfaceStore) ReleaseLease(ctx context.Context, user, lease string) (ReleaseResult, error) {
	if s.releaseLease != nil {
		return s.releaseLease(ctx, user, lease)
	}
	return ReleaseResult{UserID: user, LeaseID: lease, Released: true}, nil
}
func (s *creditsSurfaceStore) RenewLease(ctx context.Context, user, lease string, ttl time.Duration) (LeaseResult, error) {
	if s.renewLease != nil {
		return s.renewLease(ctx, user, lease, ttl)
	}
	return surfaceCommittedLease(user, MustAmount("1")), nil
}

func surfaceCommittedLease(user string, amount Amount) LeaseResult {
	expires := time.Now().UTC().Add(time.Hour)
	minimum := DecimalZero
	return LeaseResult{LeaseID: "lease-1", UserID: user, Amount: &amount, Available: MustAmount("10"), ReservedTotal: amount, MinimumBalance: &minimum, BillingMode: BillingModeStrict, ExpiresAt: &expires}
}
func (s *creditsSurfaceStore) GetAvailable(ctx context.Context, user string) (AvailableResult, error) {
	if s.getAvailable != nil {
		return s.getAvailable(ctx, user)
	}
	return AvailableResult{UserID: user, Available: MustAmount("10"), Balance: MustAmount("10")}, nil
}
func (s *creditsSurfaceStore) CheckAllowance(ctx context.Context, user string) (*AllowanceResult, error) {
	if s.checkAllowance != nil {
		return s.checkAllowance(ctx, user)
	}
	return &AllowanceResult{AllowanceRemaining: MustAmount("2")}, nil
}
func (s *creditsSurfaceStore) CheckFeature(ctx context.Context, user, feature string) (CheckFeatureResult, error) {
	if s.checkFeature != nil {
		return s.checkFeature(ctx, user, feature)
	}
	return CheckFeatureResult{UserID: user, Feature: feature, HasFeature: true}, nil
}
func (s *creditsSurfaceStore) GetUserPlan(ctx context.Context, user string) (GetUserPlanResult, error) {
	if s.getUserPlan != nil {
		return s.getUserPlan(ctx, user)
	}
	return GetUserPlanResult{UserID: user, PlanKey: "pro"}, nil
}
func (s *creditsSurfaceStore) RecordUsage(ctx context.Context, user, operation string, amount Amount, options RecordUsageOptions) (UsageRecordResult, error) {
	if s.recordUsage != nil {
		return s.recordUsage(ctx, user, operation, amount, options)
	}
	return UsageRecordResult{UsageID: "usage-1"}, nil
}
func (s *creditsSurfaceStore) SetUserPlan(ctx context.Context, user, plan string, opts SetUserPlanOptions) (SetUserPlanResult, error) {
	if s.setUserPlan != nil {
		return s.setUserPlan(ctx, user, plan, opts)
	}
	return SetUserPlanResult{UserID: user, PlanKey: plan, AssignmentState: "assigned"}, nil
}

func (s *creditsSurfaceStore) GetBalance(context.Context, string) (BalanceResult, error) {
	return BalanceResult{Balance: MustAmount("10")}, nil
}
func (s *creditsSurfaceStore) GetLeasePricingContext(context.Context, string, string) (*LeasePricingContext, error) {
	if s.leasePricing != nil {
		return s.leasePricing, nil
	}
	return &LeasePricingContext{}, nil
}
func (s *creditsSurfaceStore) ExpireLeases(context.Context, int) (int, error) { return 1, nil }
func (s *creditsSurfaceStore) GetBucketBalances(context.Context, string) (BucketBalancesResult, error) {
	return BucketBalancesResult{TotalBalance: MustAmount("10")}, nil
}
func (s *creditsSurfaceStore) ExecuteGrantProgram(context.Context, ExecuteGrantProgramRequest) ([]GrantProgramAwardResult, error) {
	return []GrantProgramAwardResult{{}}, nil
}

func (s *creditsSurfaceStore) GetActiveCatalog(context.Context) (*CatalogRevision, error) {
	if s.activeCatalog != nil {
		return s.activeCatalog, nil
	}
	return &CatalogRevision{ID: "catalog-1", Version: 1, Config: map[string]any{}}, nil
}
func (s *creditsSurfaceStore) PublishAndActivateCatalog(context.Context, map[string]any, string, CatalogRollout) (string, error) {
	return "catalog-2", nil
}
func (s *creditsSurfaceStore) PublishCatalogDraft(context.Context, map[string]any, string) (string, error) {
	return "catalog-draft", nil
}
func (s *creditsSurfaceStore) GetCatalogHistory(context.Context) ([]CatalogRevisionSummary, error) {
	return []CatalogRevisionSummary{{Version: 1}}, nil
}
func (s *creditsSurfaceStore) GetCatalogRevision(context.Context, int) (*CatalogRevision, error) {
	return &CatalogRevision{Version: 1}, nil
}
func (s *creditsSurfaceStore) ActivateCatalogRevision(context.Context, int, CatalogRollout) (string, error) {
	return "catalog-1", nil
}
func (s *creditsSurfaceStore) UnsetUserPlan(context.Context, string) (UnsetUserPlanResult, error) {
	return UnsetUserPlanResult{}, nil
}
func (s *creditsSurfaceStore) SetPlanRevisionPin(context.Context, string, bool) (bool, error) {
	return true, nil
}
func (s *creditsSurfaceStore) ApplyDuePlanChanges(context.Context, int) (int, error) { return 1, nil }
func (s *creditsSurfaceStore) StartPlanMigration(context.Context, string, string) (PlanMigrationStartResult, error) {
	return PlanMigrationStartResult{}, nil
}
func (s *creditsSurfaceStore) MigratePlanBatch(context.Context, string, int) (PlanMigrationBatchResult, error) {
	return PlanMigrationBatchResult{}, nil
}
func (s *creditsSurfaceStore) GetQuotaState(context.Context, string, string) ([]QuotaState, error) {
	return []QuotaState{}, nil
}

func (s *creditsSurfaceStore) ListQuotaEvents(context.Context, string, ListQuotaEventsOptions) ([]QuotaEvent, error) {
	if s.quotaEventsErr != nil {
		return nil, s.quotaEventsErr
	}
	return append([]QuotaEvent(nil), s.quotaEvents...), nil
}
func (s *creditsSurfaceStore) SpendByUser(context.Context, time.Time, time.Time) ([]SpendByUserRow, error) {
	return []SpendByUserRow{}, nil
}
func (s *creditsSurfaceStore) SpendByModel(context.Context, time.Time, time.Time) ([]SpendByModelRow, error) {
	return []SpendByModelRow{}, nil
}
func (s *creditsSurfaceStore) TopUsers(context.Context, int, time.Time, time.Time) ([]TopUserRow, error) {
	return []TopUserRow{}, nil
}
func (s *creditsSurfaceStore) DailySpend(context.Context, time.Time, time.Time) ([]DailySpendRow, error) {
	return []DailySpendRow{}, nil
}
func (s *creditsSurfaceStore) AggregateStats(context.Context, time.Time, time.Time) (AggregateStats, error) {
	return AggregateStats{}, nil
}
func (s *creditsSurfaceStore) ListLedgerEntries(context.Context, string, ListLedgerEntriesOptions) (LedgerPage, error) {
	return LedgerPage{}, nil
}
func (s *creditsSurfaceStore) ListUsageEntries(context.Context, string, ListLedgerEntriesOptions) (LedgerPage, error) {
	return LedgerPage{}, nil
}
func (s *creditsSurfaceStore) ListUsageCharges(context.Context, string, ListUsageChargesOptions) (UsageChargePage, error) {
	return UsageChargePage{}, nil
}
func (s *creditsSurfaceStore) GetLedgerEntry(context.Context, string, string) (*LedgerEntry, error) {
	return &LedgerEntry{EntryID: "entry-1"}, nil
}
func (s *creditsSurfaceStore) CreateTeam(context.Context, string, string, CreateTeamOptions) (CreateTeamResult, error) {
	return CreateTeamResult{TeamID: "team-1"}, nil
}
func (s *creditsSurfaceStore) GetTeamBalance(context.Context, string) (*TeamBalanceResult, error) {
	if s.teamBalanceSet {
		return s.teamBalance, nil
	}
	if s.teamBalance != nil {
		return s.teamBalance, nil
	}
	return &TeamBalanceResult{Balance: MustAmount("4")}, nil
}
func (s *creditsSurfaceStore) AddTeamMember(context.Context, string, string, AddTeamMemberOptions) (AddTeamMemberResult, error) {
	return AddTeamMemberResult{}, nil
}
func (s *creditsSurfaceStore) GetTeamMembers(context.Context, string) ([]TeamMember, error) {
	return []TeamMember{}, nil
}
func (s *creditsSurfaceStore) RemoveTeamMember(context.Context, string, string) (bool, error) {
	return true, nil
}
func (s *creditsSurfaceStore) DeductTeam(ctx context.Context, teamID, userID string, amount Amount, options TeamDeductionOptions) (TeamDeductionResult, error) {
	if s.deductTeam != nil {
		return s.deductTeam(ctx, teamID, userID, amount, options)
	}
	balance := MustAmount("3")
	return TeamDeductionResult{EntryID: "team-entry", TeamBalanceAfter: &balance}, nil
}
func (s *creditsSurfaceStore) RefundCredits(ctx context.Context, entryID string, amount *Amount, reason string, metadata CreditMetadata, key string) (RefundResult, error) {
	if s.refund != nil {
		return s.refund(ctx, entryID, amount, reason, metadata, key)
	}
	balance, refunded := MustAmount("10"), MustAmount("1")
	return RefundResult{UserID: "user-1", RefundEntryID: "refund-1", Amount: &refunded, NewBalance: &balance}, nil
}
func (s *creditsSurfaceStore) RevokeCreditsByEntryType(context.Context, string, string) (RevokeCreditsResult, error) {
	return RevokeCreditsResult{Revoked: MustAmount("1")}, nil
}
func (s *creditsSurfaceStore) SweepExpiredCredits(ctx context.Context, dryRun bool, userID string, limit int) (SweepResult, error) {
	if s.sweep != nil {
		return s.sweep(ctx, dryRun, userID, limit)
	}
	return SweepResult{ExpiredCount: 1, ExpiredAmount: MustAmount("1")}, nil
}

func newCreditsSurfaceService(t *testing.T, store *creditsSurfaceStore) *CreditsService {
	t.Helper()
	service, err := NewCreditsService(store, CreditsServiceOptions{})
	if err != nil {
		t.Fatalf("NewCreditsService: %v", err)
	}
	return service
}

func TestCreditsServiceDelegatesPersistenceAndReadSurfaces(t *testing.T) {
	ctx := context.Background()
	store := &creditsSurfaceStore{}
	service := newCreditsSurfaceService(t, store)
	if service.Store() != store || service.Catalog() == nil {
		t.Fatal("service capabilities were not initialized")
	}
	user := "user-1"

	if result, err := service.AddCredits(ctx, user, MustAmount("2.1234567"), AddCreditsOptions{}); err != nil || !result.Amount.Equal(MustAmount("2.123457")) {
		t.Fatalf("AddCredits = %+v, %v", result, err)
	}
	var deducted Amount
	store.addCredits = func(_ context.Context, _ string, amount Amount, _ AddCreditsOptions) (AddCreditsResult, error) {
		deducted = amount
		return AddCreditsResult{EntryID: "raw-debit", NewBalance: MustAmount("8")}, nil
	}
	if _, err := service.DeductCredits(ctx, user, MustAmount("1"), AddCreditsOptions{}); err != nil || !deducted.Equal(MustAmount("-1")) {
		t.Fatalf("DeductCredits amount = %s, error = %v", deducted, err)
	}
	if _, err := service.GetBalance(ctx, user); err != nil {
		t.Fatal(err)
	}
	if _, err := service.GetAvailable(ctx, user); err != nil {
		t.Fatal(err)
	}
	if _, err := service.RecordUsage(ctx, user, "completion", MustAmount("1"), RecordUsageOptions{}); err != nil {
		t.Fatal(err)
	}
	if _, err := service.GetBucketBalances(ctx, user); err != nil {
		t.Fatal(err)
	}
	if _, err := service.CheckAllowance(ctx, user); err != nil {
		t.Fatal(err)
	}
	if _, err := service.GetUserPlan(ctx, user); err != nil {
		t.Fatal(err)
	}
	if _, err := service.CheckFeature(ctx, user, "feature"); err != nil {
		t.Fatal(err)
	}
	if _, err := service.SetUserPlan(ctx, user, "pro", SetUserPlanOptions{}); err != nil {
		t.Fatal(err)
	}
	if _, err := service.UnsetUserPlan(ctx, user); err != nil {
		t.Fatal(err)
	}
	if _, err := service.SetPlanRevisionPin(ctx, user, true); err != nil {
		t.Fatal(err)
	}
	if _, err := service.ApplyDuePlanChanges(ctx, 10); err != nil {
		t.Fatal(err)
	}
	if _, err := service.StartPlanMigration(ctx, "from", "to"); err != nil {
		t.Fatal(err)
	}
	if _, err := service.MigratePlanBatch(ctx, "migration", 10); err != nil {
		t.Fatal(err)
	}
	if _, err := service.GetQuotaState(ctx, user, "quota"); err != nil {
		t.Fatal(err)
	}
	if _, err := service.ListQuotaEvents(ctx, user, ListQuotaEventsOptions{}); err != nil {
		t.Fatal(err)
	}
	if _, err := service.ExecuteGrantProgram(ctx, ExecuteGrantProgramRequest{}); err != nil {
		t.Fatal(err)
	}

	for _, call := range []func() error{
		func() error { _, e := service.GetActiveCatalog(ctx); return e },
		func() error {
			_, e := service.PublishAndActivateCatalog(ctx, map[string]any{}, "label", CatalogRollout{})
			return e
		},
		func() error { _, e := service.PublishCatalogDraft(ctx, map[string]any{}, "label"); return e },
		func() error { _, e := service.GetCatalogHistory(ctx); return e },
		func() error { _, e := service.GetCatalogRevision(ctx, 1); return e },
		func() error { _, e := service.ActivateCatalogRevision(ctx, 1, CatalogRollout{}); return e },
	} {
		if err := call(); err != nil {
			t.Fatal(err)
		}
	}

	if _, err := service.ListLedgerEntries(ctx, user, ListLedgerEntriesOptions{}); err != nil {
		t.Fatal(err)
	}
	if _, err := service.ListUsageEntries(ctx, user, ListLedgerEntriesOptions{}); err != nil {
		t.Fatal(err)
	}
	if _, err := service.ListUsageCharges(ctx, user, ListUsageChargesOptions{}); err != nil {
		t.Fatal(err)
	}
	if _, err := service.GetLedgerEntry(ctx, user, "entry-1"); err != nil {
		t.Fatal(err)
	}
	if _, err := service.CreateTeam(ctx, user, "team", CreateTeamOptions{}); err != nil {
		t.Fatal(err)
	}
	if _, err := service.GetTeamBalance(ctx, "team-1"); err != nil {
		t.Fatal(err)
	}
	if _, err := service.AddTeamMember(ctx, "team-1", user, AddTeamMemberOptions{}); err != nil {
		t.Fatal(err)
	}
	if _, err := service.GetTeamMembers(ctx, "team-1"); err != nil {
		t.Fatal(err)
	}
	if _, err := service.RemoveTeamMember(ctx, "team-1", user); err != nil {
		t.Fatal(err)
	}
	if _, err := service.DeductTeam(ctx, "team-1", user, MustAmount("1"), TeamDeductionOptions{}); err != nil {
		t.Fatal(err)
	}
	if _, err := service.RefundCredits(ctx, "entry-1", nil, "test", nil, "refund-1"); err != nil {
		t.Fatal(err)
	}
	if _, err := service.RevokeCreditsByEntryType(ctx, user, "purchase"); err != nil {
		t.Fatal(err)
	}
	if _, err := service.SweepExpiredCredits(ctx, false, user, 10); err != nil {
		t.Fatal(err)
	}

	start, end := time.Unix(1, 0), time.Unix(2, 0)
	if _, err := service.SpendByUser(ctx, start, end); err != nil {
		t.Fatal(err)
	}
	if _, err := service.SpendByModel(ctx, start, end); err != nil {
		t.Fatal(err)
	}
	if _, err := service.TopUsers(ctx, 10, start, end); err != nil {
		t.Fatal(err)
	}
	if _, err := service.DailySpend(ctx, start, end); err != nil {
		t.Fatal(err)
	}
	if _, err := service.AggregateStats(ctx, start, end); err != nil {
		t.Fatal(err)
	}
	if err := service.Close(); err != nil {
		t.Fatal(err)
	}
}

func TestCreditsServiceLeaseAndBilledOperationInvariants(t *testing.T) {
	ctx := context.Background()
	var settleCalls int
	store := &creditsSurfaceStore{
		settleLease: func(_ context.Context, user, _ string, amount Amount, _ SettleLeaseOptions) (DeductionResult, error) {
			settleCalls++
			if settleCalls == 1 {
				return DeductionResult{}, NewError("temporary", ErrorOptions{Retryable: true})
			}
			balance := MustAmount("8")
			return DeductionResult{EntryID: "settled", UserID: user, Amount: amount, BalanceAfter: &balance}, nil
		},
	}
	service := newCreditsSurfaceService(t, store)
	operation, err := service.BeginBilledOperation(ctx, "user-1", BeginBilledOperationOptions{Estimate: MustAmount("3"), OperationKey: "job-1"})
	if err != nil || operation == nil || operation.UserID() != "user-1" || operation.LeaseID() == "" {
		t.Fatalf("begin operation = %+v, %v", operation, err)
	}
	if _, err := operation.Renew(ctx, 0); err != nil {
		t.Fatal(err)
	}
	if _, err := operation.Settle(ctx, MustAmount("2")); err == nil || !IsRetryableError(err) {
		t.Fatalf("first direct settle error = %v, want retryable", err)
	}
	if _, err := operation.Settle(ctx, MustAmount("2")); err != nil {
		t.Fatal(err)
	}
	if settleCalls != 2 {
		t.Fatalf("direct settle calls = %d", settleCalls)
	}
	resumed, err := service.ResumeBilledOperation("user-1", operation.LeaseID(), "job-1", "feature", CreditMetadata{"trace": "x"})
	if err != nil || resumed == nil {
		t.Fatal(err)
	}
	if _, err := resumed.Release(ctx); err != nil {
		t.Fatal(err)
	}

	settleCalls = 0
	run, err := service.RunBilled(ctx, "user-1", RunBilledOptions{Estimate: MustAmount("3"), OperationKey: "job-2", SettlementAttempts: 2, DoWork: func(context.Context) (any, Amount, error) { return "ok", MustAmount("1"), nil }})
	if err != nil || run.Result != "ok" || run.Deduction.EntryID != "settled" || settleCalls != 2 {
		t.Fatalf("RunBilled = %+v, calls=%d, error=%v", run, settleCalls, err)
	}
	if _, err := service.RunBilled(ctx, "user-1", RunBilledOptions{Estimate: MustAmount("1"), OperationKey: "bad", SettlementAttempts: 0, DoWork: nil}); err == nil {
		t.Fatal("RunBilled accepted nil work")
	}
	if _, err := service.RunBilled(ctx, "user-1", RunBilledOptions{Estimate: MustAmount("1"), OperationKey: "bad", SettlementAttempts: -1, DoWork: func(context.Context) (any, Amount, error) { return nil, DecimalZero, nil }}); err == nil {
		t.Fatal("RunBilled accepted negative attempts")
	}
	if _, err := service.ResumeBilledOperation("", "lease", "key", "", nil); err == nil {
		t.Fatal("ResumeBilledOperation accepted empty user")
	}
}

func TestCreditsServiceSubscriptionGrantAndAffordability(t *testing.T) {
	ctx := context.Background()
	store := &creditsSurfaceStore{}
	service := newCreditsSurfaceService(t, store)
	grant, err := service.GrantSubscriptionCycle(ctx, "user-1", MustAmount("4"), GrantSubscriptionCycleOptions{IdempotencyKey: "cycle-1", PlanKey: "pro", TTLDays: 1})
	if err != nil || grant.EntryID == "" {
		t.Fatalf("subscription grant = %+v, error = %v", grant, err)
	}
	if _, err := service.GrantSubscriptionCycle(ctx, "user-1", MustAmount("1"), GrantSubscriptionCycleOptions{IdempotencyKey: "cycle-bad", TTLDays: -1}); err == nil {
		t.Fatal("subscription grant accepted a negative TTL")
	}
	store.addCredits = func(_ context.Context, user string, amount Amount, _ AddCreditsOptions) (AddCreditsResult, error) {
		return AddCreditsResult{EntryID: "replayed", UserID: user, Amount: amount, NewBalance: MustAmount("8"), Idempotent: true}, nil
	}
	store.getUserPlan = func(context.Context, string) (GetUserPlanResult, error) {
		return GetUserPlanResult{PlanKey: "old"}, nil
	}
	if _, err := service.GrantSubscriptionCycle(ctx, "user-1", MustAmount("1"), GrantSubscriptionCycleOptions{IdempotencyKey: "cycle-replay", PlanKey: "pro"}); err != nil {
		t.Fatalf("replayed subscription grant = %v", err)
	}
	store.getAvailable = func(context.Context, string) (AvailableResult, error) {
		return AvailableResult{Available: MustAmount("1")}, nil
	}
	store.checkAllowance = func(context.Context, string) (*AllowanceResult, error) {
		return &AllowanceResult{AllowanceRemaining: MustAmount("3")}, nil
	}
	store.checkFeature = func(context.Context, string, string) (CheckFeatureResult, error) {
		return CheckFeatureResult{HasFeature: false}, nil
	}
	if result, err := service.CanAfford(ctx, "user-1", MustAmount("3"), CanAffordOptions{}); err != nil || !result.Affordable {
		t.Fatalf("strict affordability = %+v, error = %v", result, err)
	}
	if result, err := service.CanAfford(ctx, "user-1", MustAmount("1"), CanAffordOptions{Feature: "missing"}); err != nil || result.Affordable || result.Reason != "feature_not_entitled" {
		t.Fatalf("feature affordability = %+v, error = %v", result, err)
	}
	store.checkAllowance = func(context.Context, string) (*AllowanceResult, error) { return nil, nil }
	if result, err := service.CanAfford(ctx, "user-1", MustAmount("2"), CanAffordOptions{BillingMode: BillingModeOverdraft}); err != nil || result.Reason != "insufficient_credits" {
		t.Fatalf("overdraft affordability = %+v, error = %v", result, err)
	}
}

func TestCreditsServiceZeroCostTeamAndUsageRecoveryPaths(t *testing.T) {
	ctx := context.Background()
	store := &creditsSurfaceStore{
		activeCatalog:  &CatalogRevision{ID: "catalog-zero", Version: 1, Config: pricedCreditsCatalog("pro", "0")},
		teamBalance:    &TeamBalanceResult{TeamID: "team", Balance: MustAmount("5")},
		teamBalanceSet: true,
		quotaEventsErr: errors.New("quota history unavailable"),
	}
	service := newCreditsSurfaceService(t, store)
	if err := service.Catalog().Load(ctx); err != nil {
		t.Fatalf("load zero-cost catalog: %v", err)
	}
	metrics := pricedCreditsMetrics("1000")
	zero, err := service.DeductTeamUsage(ctx, "team", "user", metrics, PricedTeamDeductionOptions{IdempotencyKey: "team-zero"})
	if err != nil || !zero.Amount.IsZero() || zero.TeamBalanceAfter == nil || !zero.TeamBalanceAfter.Equal(MustAmount("5")) {
		t.Fatalf("zero-cost team deduction = %+v, error = %v", zero, err)
	}
	store.teamBalance = nil
	if _, err := service.DeductTeamUsage(ctx, "missing", "user", metrics, PricedTeamDeductionOptions{IdempotencyKey: "team-missing"}); err == nil {
		t.Fatal("missing zero-cost team accepted")
	}
	store.teamBalance = &TeamBalanceResult{TeamID: "team", Balance: MustAmount("5")}
	if _, err := service.DeductUsage(ctx, "user", metrics, PricedUsageOptions{IdempotencyKey: "usage-quota-error"}); err != nil {
		t.Fatalf("usage with quota-history failure = %v", err)
	}
	failed, err := service.RunBilledUsage(ctx, "user", RunBilledUsageOptions{
		Estimate: pricedCreditsMetrics("1000"), OperationKey: "usage-failure",
		DoWork: func(context.Context) (any, UsageMetrics, error) {
			return nil, UsageMetrics{}, errors.New("work failed")
		},
	})
	if err == nil || failed.Result != nil {
		t.Fatalf("failed billed usage = %+v, error = %v", failed, err)
	}
}

func TestCreditsServiceBusinessDenialsAndInvariantFailures(t *testing.T) {
	ctx := context.Background()
	store := &creditsSurfaceStore{}
	service := newCreditsSurfaceService(t, store)
	if _, err := service.AddCredits(ctx, "user", DecimalZero, AddCreditsOptions{}); err == nil {
		t.Fatal("zero grant accepted")
	}
	if _, err := service.Deduct(ctx, "user", MustAmount("-1"), DeductWithAllowanceOptions{}); err == nil {
		t.Fatal("negative deduction accepted")
	}
	store.deduct = func(context.Context, string, Amount, DeductWithAllowanceOptions) (DeductionResult, error) {
		return DeductionResult{ErrorCode: "insufficient_credits"}, nil
	}
	if _, err := service.Deduct(ctx, "user", MustAmount("1"), DeductWithAllowanceOptions{}); err == nil {
		t.Fatal("insufficient deduction accepted")
	}
	store.deduct = func(context.Context, string, Amount, DeductWithAllowanceOptions) (DeductionResult, error) {
		return DeductionResult{}, nil
	}
	if _, err := service.Deduct(ctx, "user", MustAmount("1"), DeductWithAllowanceOptions{}); err == nil {
		t.Fatal("deduction without balance accepted")
	}
	if _, err := service.DeductCredits(ctx, "user", DecimalZero, AddCreditsOptions{}); err == nil {
		t.Fatal("zero raw debit accepted")
	}
	if _, err := service.Reserve(ctx, "user", MustAmount("1"), ReserveOptions{IdempotencyKey: ""}); err == nil {
		t.Fatal("reserve without idempotency key accepted")
	}
	if _, err := service.Reserve(ctx, "user", MustAmount("1"), ReserveOptions{IdempotencyKey: "key", BillingMode: BillingMode("invalid")}); err == nil {
		t.Fatal("reserve with invalid billing mode accepted")
	}
	if _, err := service.Reserve(ctx, "user", MustAmount("1"), ReserveOptions{IdempotencyKey: "key", TTL: time.Nanosecond}); err == nil {
		t.Fatal("reserve with sub-second TTL accepted")
	}
	if _, err := service.Settle(ctx, "user", "lease", MustAmount("1"), SettleOptions{IdempotencyKey: string(make([]byte, 256))}); err == nil {
		t.Fatal("settle with oversized idempotency key accepted")
	}
	if _, err := service.CanAfford(ctx, "user", MustAmount("-1"), CanAffordOptions{}); err == nil {
		t.Fatal("negative affordability request accepted")
	}
	store.getAvailable = func(context.Context, string) (AvailableResult, error) {
		return AvailableResult{}, errors.New("availability failed")
	}
	if _, err := service.CanAfford(ctx, "user", MustAmount("1"), CanAffordOptions{}); err == nil {
		t.Fatal("availability storage error swallowed")
	}
	store.getAvailable = nil
	store.checkAllowance = func(context.Context, string) (*AllowanceResult, error) { return nil, errors.New("allowance failed") }
	if _, err := service.CanAfford(ctx, "user", MustAmount("1"), CanAffordOptions{}); err == nil {
		t.Fatal("allowance storage error swallowed")
	}
	store.checkAllowance = nil
	store.checkFeature = func(context.Context, string, string) (CheckFeatureResult, error) {
		return CheckFeatureResult{}, errors.New("feature failed")
	}
	if _, err := service.CanAfford(ctx, "user", MustAmount("1"), CanAffordOptions{Feature: "feature"}); err == nil {
		t.Fatal("feature storage error swallowed")
	}
	store.checkFeature = nil
	if _, err := service.GrantSubscriptionCycle(ctx, "user", MustAmount("1"), GrantSubscriptionCycleOptions{IdempotencyKey: "key", ExpiresAt: timePointer(time.Now()), TTLDays: 1}); err == nil {
		t.Fatal("conflicting subscription expiry accepted")
	}
	store.recordUsage = func(context.Context, string, string, Amount, RecordUsageOptions) (UsageRecordResult, error) {
		return UsageRecordResult{ErrorCode: "quota_exceeded"}, nil
	}
	if _, err := service.RecordUsage(ctx, "user", "op", MustAmount("1"), RecordUsageOptions{}); err == nil {
		t.Fatal("usage business denial accepted")
	}
	store.recordUsage = nil
	store.renewLease = func(context.Context, string, string, time.Duration) (LeaseResult, error) {
		return LeaseResult{ErrorCode: "lease_expired"}, nil
	}
	if _, err := service.Renew(ctx, "user", "lease", time.Minute); err == nil {
		t.Fatal("expired lease renewed")
	}
	store.settleLease = func(context.Context, string, string, Amount, SettleLeaseOptions) (DeductionResult, error) {
		return DeductionResult{ErrorCode: "lease_expired"}, nil
	}
	if _, err := service.Settle(ctx, "user", "lease", MustAmount("1"), SettleOptions{}); err == nil {
		t.Fatal("expired lease settled")
	}
	if _, err := service.SweepExpiredCredits(ctx, true, "user", 1); err != nil {
		t.Fatal(err)
	}
	store.deductTeam = func(context.Context, string, string, Amount, TeamDeductionOptions) (TeamDeductionResult, error) {
		return TeamDeductionResult{ErrorCode: "insufficient_credits"}, nil
	}
	if _, err := service.DeductTeam(ctx, "team", "user", MustAmount("1"), TeamDeductionOptions{}); err == nil {
		t.Fatal("team insufficient balance accepted")
	}
	store.deductTeam = func(context.Context, string, string, Amount, TeamDeductionOptions) (TeamDeductionResult, error) {
		return TeamDeductionResult{}, nil
	}
	if _, err := service.DeductTeam(ctx, "team", "user", MustAmount("1"), TeamDeductionOptions{}); err == nil {
		t.Fatal("team debit without committed balance accepted")
	}
	store.refund = func(context.Context, string, *Amount, string, CreditMetadata, string) (RefundResult, error) {
		return RefundResult{UserID: "user", ErrorCode: "over_refund"}, nil
	}
	if _, err := service.RefundCredits(ctx, "entry", nil, "test", nil, "refund"); err == nil {
		t.Fatal("over-refund accepted")
	}
	store.sweep = func(context.Context, bool, string, int) (SweepResult, error) {
		return SweepResult{}, errors.New("sweep failed")
	}
	if _, err := service.SweepExpiredCredits(ctx, false, "user", 1); err == nil {
		t.Fatal("sweep storage error was swallowed")
	}
}

func TestCreditsServiceNilAndClosePaths(t *testing.T) {
	ctx := context.Background()
	var nilService *CreditsService
	if nilService.Catalog() != nil || nilService.Store() != nil || nilService.Close() != nil || nilService.AddPostDeductionHook(nil) == nil {
		t.Fatal("nil service helpers not safe")
	}
	work := func(context.Context) (any, Amount, error) { return nil, DecimalZero, nil }
	cases := []struct {
		name string
		call func() error
	}{
		{"balance", func() error { _, e := nilService.GetBalance(ctx, "user"); return e }},
		{"available", func() error { _, e := nilService.GetAvailable(ctx, "user"); return e }},
		{"add", func() error {
			_, e := nilService.AddCredits(ctx, "user", MustAmount("1"), AddCreditsOptions{})
			return e
		}},
		{"deduct credits", func() error {
			_, e := nilService.DeductCredits(ctx, "user", MustAmount("1"), AddCreditsOptions{})
			return e
		}},
		{"deduct", func() error {
			_, e := nilService.Deduct(ctx, "user", MustAmount("1"), DeductWithAllowanceOptions{})
			return e
		}},
		{"deduct usage", func() error {
			_, e := nilService.DeductUsage(ctx, "user", UsageMetrics{Operation: "op"}, PricedUsageOptions{IdempotencyKey: "key"})
			return e
		}},
		{"flat job", func() error {
			_, e := nilService.DeductFlatJob(ctx, "user", "job", PricedUsageOptions{IdempotencyKey: "key"})
			return e
		}},
		{"reserve", func() error { _, e := nilService.Reserve(ctx, "user", MustAmount("1"), ReserveOptions{}); return e }},
		{"reserve usage", func() error {
			_, e := nilService.ReserveUsage(ctx, "user", UsageMetrics{Operation: "op"}, ReserveOptions{IdempotencyKey: "key"})
			return e
		}},
		{"settle", func() error {
			_, e := nilService.Settle(ctx, "user", "lease", MustAmount("1"), SettleOptions{})
			return e
		}},
		{"settle usage", func() error {
			_, e := nilService.SettleUsage(ctx, "user", "lease", UsageMetrics{Operation: "op"}, SettleOptions{IdempotencyKey: "key"})
			return e
		}},
		{"release", func() error { _, e := nilService.Release(ctx, "user", "lease"); return e }},
		{"renew", func() error { _, e := nilService.Renew(ctx, "user", "lease", time.Minute); return e }},
		{"can afford", func() error { _, e := nilService.CanAfford(ctx, "user", MustAmount("1"), CanAffordOptions{}); return e }},
		{"can afford usage", func() error {
			_, e := nilService.CanAffordUsage(ctx, "user", UsageMetrics{Operation: "op"}, CanAffordOptions{})
			return e
		}},
		{"grant cycle", func() error {
			_, e := nilService.GrantSubscriptionCycle(ctx, "user", MustAmount("1"), GrantSubscriptionCycleOptions{IdempotencyKey: "key"})
			return e
		}},
		{"set plan", func() error { _, e := nilService.SetUserPlan(ctx, "user", "plan", SetUserPlanOptions{}); return e }},
		{"unset plan", func() error { _, e := nilService.UnsetUserPlan(ctx, "user"); return e }},
		{"get plan", func() error { _, e := nilService.GetUserPlan(ctx, "user"); return e }},
		{"feature", func() error { _, e := nilService.CheckFeature(ctx, "user", "feature"); return e }},
		{"buckets", func() error { _, e := nilService.GetBucketBalances(ctx, "user"); return e }},
		{"allowance", func() error { _, e := nilService.CheckAllowance(ctx, "user"); return e }},
		{"refund", func() error { _, e := nilService.RefundCredits(ctx, "entry", nil, "reason", nil, "key"); return e }},
		{"revoke", func() error { _, e := nilService.RevokeCreditsByEntryType(ctx, "user", "purchase"); return e }},
		{"sweep", func() error { _, e := nilService.SweepExpiredCredits(ctx, false, "user", 1); return e }},
		{"active catalog", func() error { _, e := nilService.GetActiveCatalog(ctx); return e }},
		{"publish activate", func() error {
			_, e := nilService.PublishAndActivateCatalog(ctx, nil, "label", CatalogRollout{})
			return e
		}},
		{"publish draft", func() error { _, e := nilService.PublishCatalogDraft(ctx, nil, "label"); return e }},
		{"history", func() error { _, e := nilService.GetCatalogHistory(ctx); return e }},
		{"revision", func() error { _, e := nilService.GetCatalogRevision(ctx, 1); return e }},
		{"activate", func() error { _, e := nilService.ActivateCatalogRevision(ctx, 1, CatalogRollout{}); return e }},
		{"pin", func() error { _, e := nilService.SetPlanRevisionPin(ctx, "user", true); return e }},
		{"due plans", func() error { _, e := nilService.ApplyDuePlanChanges(ctx, 1); return e }},
		{"migration start", func() error { _, e := nilService.StartPlanMigration(ctx, "from", "to"); return e }},
		{"migration batch", func() error { _, e := nilService.MigratePlanBatch(ctx, "migration", 1); return e }},
		{"quota state", func() error { _, e := nilService.GetQuotaState(ctx, "user", "quota"); return e }},
		{"quota events", func() error { _, e := nilService.ListQuotaEvents(ctx, "user", ListQuotaEventsOptions{}); return e }},
		{"grant program", func() error { _, e := nilService.ExecuteGrantProgram(ctx, ExecuteGrantProgramRequest{}); return e }},
		{"record usage", func() error {
			_, e := nilService.RecordUsage(ctx, "user", "op", MustAmount("1"), RecordUsageOptions{})
			return e
		}},
		{"record usage metrics", func() error {
			_, e := nilService.RecordUsageMetrics(ctx, "user", UsageMetrics{Operation: "op"}, PricedUsageRecordOptions{IdempotencyKey: "key"})
			return e
		}},
		{"spend user", func() error { _, e := nilService.SpendByUser(ctx, time.Time{}, time.Time{}); return e }},
		{"spend model", func() error { _, e := nilService.SpendByModel(ctx, time.Time{}, time.Time{}); return e }},
		{"top users", func() error { _, e := nilService.TopUsers(ctx, 1, time.Time{}, time.Time{}); return e }},
		{"daily spend", func() error { _, e := nilService.DailySpend(ctx, time.Time{}, time.Time{}); return e }},
		{"aggregate", func() error { _, e := nilService.AggregateStats(ctx, time.Time{}, time.Time{}); return e }},
		{"ledger", func() error { _, e := nilService.ListLedgerEntries(ctx, "user", ListLedgerEntriesOptions{}); return e }},
		{"usage ledger", func() error { _, e := nilService.ListUsageEntries(ctx, "user", ListLedgerEntriesOptions{}); return e }},
		{"usage charges", func() error { _, e := nilService.ListUsageCharges(ctx, "user", ListUsageChargesOptions{}); return e }},
		{"ledger entry", func() error { _, e := nilService.GetLedgerEntry(ctx, "user", "entry"); return e }},
		{"team create", func() error { _, e := nilService.CreateTeam(ctx, "user", "team", CreateTeamOptions{}); return e }},
		{"team balance", func() error { _, e := nilService.GetTeamBalance(ctx, "team"); return e }},
		{"team member add", func() error { _, e := nilService.AddTeamMember(ctx, "team", "user", AddTeamMemberOptions{}); return e }},
		{"team members", func() error { _, e := nilService.GetTeamMembers(ctx, "team"); return e }},
		{"team member remove", func() error { _, e := nilService.RemoveTeamMember(ctx, "team", "user"); return e }},
		{"team deduct", func() error {
			_, e := nilService.DeductTeam(ctx, "team", "user", MustAmount("1"), TeamDeductionOptions{})
			return e
		}},
		{"team usage", func() error {
			_, e := nilService.DeductTeamUsage(ctx, "team", "user", UsageMetrics{Operation: "op"}, PricedTeamDeductionOptions{IdempotencyKey: "key"})
			return e
		}},
		{"begin billed", func() error {
			_, e := nilService.BeginBilledOperation(ctx, "user", BeginBilledOperationOptions{OperationKey: "key"})
			return e
		}},
		{"begin billed usage", func() error {
			_, e := nilService.BeginBilledUsageOperation(ctx, "user", BeginBilledUsageOperationOptions{OperationKey: "key", Estimate: UsageMetrics{Operation: "op"}})
			return e
		}},
		{"resume billed", func() error { _, e := nilService.ResumeBilledOperation("user", "lease", "key", "", nil); return e }},
		{"run billed", func() error {
			_, e := nilService.RunBilled(ctx, "user", RunBilledOptions{OperationKey: "key", DoWork: work})
			return e
		}},
		{"run billed usage", func() error {
			_, e := nilService.RunBilledUsage(ctx, "user", RunBilledUsageOptions{OperationKey: "key", DoWork: func(context.Context) (any, UsageMetrics, error) { return nil, UsageMetrics{Operation: "op"}, nil }})
			return e
		}},
	}
	for _, test := range cases {
		t.Run(test.name, func(t *testing.T) {
			if err := test.call(); err == nil {
				t.Fatalf("nil service call succeeded")
			}
		})
	}
	store := &creditsSurfaceStore{closeErr: errors.New("close")}
	service := newCreditsSurfaceService(t, store)
	if err := service.Close(); err == nil {
		t.Fatal("close error was swallowed")
	}
	var nilOperation *BilledOperation
	if nilOperation.UserID() != "" || nilOperation.LeaseID() != "" {
		t.Fatal("nil billed operation exposed state")
	}
	if _, err := nilOperation.Renew(ctx, time.Minute); err == nil {
		t.Fatal("nil billed operation renewed")
	}
	if _, err := nilOperation.Release(ctx); err == nil {
		t.Fatal("nil billed operation released")
	}
	if _, err := nilOperation.Settle(ctx, MustAmount("1")); err == nil {
		t.Fatal("nil billed operation settled")
	}
	if _, err := nilOperation.SettleUsage(ctx, UsageMetrics{Operation: "op"}); err == nil {
		t.Fatal("nil billed operation usage-settled")
	}
}
