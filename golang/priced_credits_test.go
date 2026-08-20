package bursar

import (
	"context"
	"testing"
	"time"
)

type pricedCreditsStoreStub struct {
	CreditStore

	active       *CatalogRevision
	activeCalls  int
	revisions    map[int]*CatalogRevision
	plan         GetUserPlanResult
	leasePricing *LeasePricingContext
	available    AvailableResult

	deductAmount  Amount
	deductOptions DeductWithAllowanceOptions
	reserveAmount Amount
	reserveOpts   CreateLeaseOptions
	settleAmount  Amount
	settleOptions SettleLeaseOptions
	recordAmount  Amount
	recordOptions RecordUsageOptions
	teamAmount    Amount
	teamOptions   TeamDeductionOptions
	quotaKeys     []string
}

func (*pricedCreditsStoreStub) Close() error { return nil }

func (s *pricedCreditsStoreStub) GetActiveCatalog(context.Context) (*CatalogRevision, error) {
	s.activeCalls++
	return s.active, nil
}

func (s *pricedCreditsStoreStub) GetCatalogRevision(_ context.Context, version int) (*CatalogRevision, error) {
	return s.revisions[version], nil
}

func (s *pricedCreditsStoreStub) GetUserPlan(context.Context, string) (GetUserPlanResult, error) {
	return s.plan, nil
}

func (s *pricedCreditsStoreStub) GetLeasePricingContext(context.Context, string, string) (*LeasePricingContext, error) {
	return s.leasePricing, nil
}

func (s *pricedCreditsStoreStub) DeductWithAllowance(_ context.Context, userID string, amount Amount, options DeductWithAllowanceOptions) (DeductionResult, error) {
	s.deductAmount, s.deductOptions = amount, options
	balance := MustAmount("9")
	return DeductionResult{EntryID: "deduct-entry", UserID: userID, Amount: amount, BalanceAfter: &balance}, nil
}

func (s *pricedCreditsStoreStub) CreateLease(_ context.Context, userID string, amount Amount, _ string, options CreateLeaseOptions) (LeaseResult, error) {
	s.reserveAmount, s.reserveOpts = amount, options
	expiresAt := time.Now().UTC().Add(time.Minute)
	reserved := amount
	minimumBalance := DecimalZero
	return LeaseResult{LeaseID: "lease-1", UserID: userID, Amount: &reserved, Available: MustAmount("9"), MinimumBalance: &minimumBalance, ExpiresAt: &expiresAt, BillingMode: options.BillingMode}, nil
}

func (s *pricedCreditsStoreStub) SettleLease(_ context.Context, userID, _ string, amount Amount, options SettleLeaseOptions) (DeductionResult, error) {
	s.settleAmount, s.settleOptions = amount, options
	balance := MustAmount("8")
	return DeductionResult{EntryID: "settle-entry", UserID: userID, Amount: amount, BalanceAfter: &balance}, nil
}

func (s *pricedCreditsStoreStub) GetAvailable(context.Context, string) (AvailableResult, error) {
	return s.available, nil
}

func (*pricedCreditsStoreStub) CheckAllowance(context.Context, string) (*AllowanceResult, error) {
	return nil, nil
}

func (s *pricedCreditsStoreStub) RecordUsage(_ context.Context, userID, _ string, requested Amount, options RecordUsageOptions) (UsageRecordResult, error) {
	s.recordAmount, s.recordOptions = requested, options
	return UsageRecordResult{UsageID: "usage-1", UserID: userID, Requested: requested}, nil
}

func (s *pricedCreditsStoreStub) DeductTeam(_ context.Context, teamID, userID string, amount Amount, options TeamDeductionOptions) (TeamDeductionResult, error) {
	s.teamAmount, s.teamOptions = amount, options
	balance := MustAmount("20")
	return TeamDeductionResult{TeamID: teamID, UserID: userID, EntryID: "team-entry", Amount: amount, TeamBalanceAfter: &balance}, nil
}

func (s *pricedCreditsStoreStub) ListQuotaEvents(_ context.Context, _ string, options ListQuotaEventsOptions) ([]QuotaEvent, error) {
	s.quotaKeys = append(s.quotaKeys, options.IdempotencyKey)
	return nil, nil
}

func TestMetricPricedCreditMethodsUseCatalogAndLeaseSnapshots(t *testing.T) {
	t.Parallel()

	active := &CatalogRevision{ID: "catalog-current", Version: 2, Config: pricedCreditsCatalog("pro", "0.001")}
	historical := &CatalogRevision{ID: "catalog-historical", Version: 1, Config: pricedCreditsCatalog("legacy", "0.004")}
	store := &pricedCreditsStoreStub{
		active:       active,
		revisions:    map[int]*CatalogRevision{1: historical, 2: active},
		plan:         GetUserPlanResult{UserID: "user-1", PlanKey: "pro", RateCard: "pro"},
		leasePricing: &LeasePricingContext{CatalogVersion: 1, PlanKey: "legacy", RateCard: "legacy"},
		available:    AvailableResult{UserID: "user-1", Balance: MustAmount("1"), Available: MustAmount("1")},
	}
	service, err := NewCreditsService(store, CreditsServiceOptions{})
	if err != nil {
		t.Fatalf("NewCreditsService() error = %v", err)
	}
	if err := service.Catalog().Load(context.Background()); err != nil {
		t.Fatalf("load active catalog: %v", err)
	}
	ctx := context.Background()

	t.Run("DeductUsage", func(t *testing.T) {
		metrics := pricedCreditsMetrics("1500")
		result, err := service.DeductUsage(ctx, "user-1", metrics, PricedUsageOptions{
			IdempotencyKey: "deduct-1",
			Feature:        "completion",
			Metadata:       CreditMetadata{"caller": "kept", "operation": "cannot-override"},
		})
		if err != nil {
			t.Fatalf("DeductUsage() error = %v", err)
		}
		assertAmount(t, result.Amount, "0.001500")
		assertAmount(t, store.deductAmount, "0.001500")
		if store.deductOptions.Operation != "completion" || store.deductOptions.Feature != "completion" {
			t.Fatalf("deduct operation options = %#v", store.deductOptions)
		}
		if store.deductOptions.Model != "gpt-4" || store.deductOptions.Region != "us" {
			t.Fatalf("deduct dimensions were not promoted: %#v", store.deductOptions.OperationUsageOptions)
		}
		if store.deductOptions.Metadata["caller"] != "kept" || store.deductOptions.Metadata["operation"] != "completion" {
			t.Fatalf("deduct metadata = %#v", store.deductOptions.Metadata)
		}
	})

	t.Run("ReserveUsage", func(t *testing.T) {
		result, err := service.ReserveUsage(ctx, "user-1", pricedCreditsMetrics("2000"), ReserveOptions{IdempotencyKey: "reserve-1"})
		if err != nil {
			t.Fatalf("ReserveUsage() error = %v", err)
		}
		if result.LeaseID != "lease-1" {
			t.Fatalf("lease ID = %q", result.LeaseID)
		}
		assertAmount(t, store.reserveAmount, "0.002000")
		if store.reserveOpts.IdempotencyKey != "reserve-1" || store.reserveOpts.OperationUsageOptions.Measures["tokens"].String() != "2000" {
			t.Fatalf("reserve options = %#v", store.reserveOpts)
		}
	})

	t.Run("SettleUsageUsesHistoricalLeaseRevision", func(t *testing.T) {
		result, err := service.SettleUsage(ctx, "user-1", "lease-1", pricedCreditsMetrics("1500"), SettleOptions{})
		if err != nil {
			t.Fatalf("SettleUsage() error = %v", err)
		}
		assertAmount(t, result.Amount, "0.006000")
		assertAmount(t, store.settleAmount, "0.006000")
		if store.settleOptions.IdempotencyKey != "lease:lease-1:settle" {
			t.Fatalf("settle idempotency key = %q", store.settleOptions.IdempotencyKey)
		}
		if store.settleOptions.Metadata["breakdown_total"] != "0.006" {
			t.Fatalf("settle metadata = %#v", store.settleOptions.Metadata)
		}
	})

	t.Run("CanAffordUsage", func(t *testing.T) {
		result, err := service.CanAffordUsage(ctx, "user-1", pricedCreditsMetrics("3000"), CanAffordOptions{})
		if err != nil {
			t.Fatalf("CanAffordUsage() error = %v", err)
		}
		if !result.Affordable {
			t.Fatalf("CanAffordUsage() = %#v, want affordable", result)
		}
		assertAmount(t, result.WorstCase, "0.003000")
	})

	t.Run("RecordUsageMetrics", func(t *testing.T) {
		result, err := service.RecordUsageMetrics(ctx, "user-1", pricedCreditsMetrics("500"), PricedUsageRecordOptions{IdempotencyKey: "record-1"})
		if err != nil {
			t.Fatalf("RecordUsageMetrics() error = %v", err)
		}
		assertAmount(t, result.Requested, "0.000500")
		assertAmount(t, store.recordAmount, "0.000500")
		if store.recordOptions.IdempotencyKey != "record-1" || store.recordOptions.Metadata["operation"] != "completion" {
			t.Fatalf("record options = %#v", store.recordOptions)
		}
	})

	t.Run("DeductTeamUsage", func(t *testing.T) {
		result, err := service.DeductTeamUsage(ctx, "team-1", "user-1", pricedCreditsMetrics("4000"), PricedTeamDeductionOptions{IdempotencyKey: "team-1"})
		if err != nil {
			t.Fatalf("DeductTeamUsage() error = %v", err)
		}
		assertAmount(t, result.Amount, "0.004000")
		assertAmount(t, store.teamAmount, "0.004000")
		if store.teamOptions.Operation != "completion" || store.teamOptions.Metadata["breakdown_total"] != "0.004" {
			t.Fatalf("team options = %#v", store.teamOptions)
		}
	})

	if len(store.quotaKeys) != 3 {
		t.Fatalf("quota event lookups = %v, want deduct, reserve, and settle", store.quotaKeys)
	}
}

func TestRunBilledUsagePricesEstimateAndActualFromCapturedCatalogs(t *testing.T) {
	t.Parallel()

	active := &CatalogRevision{ID: "catalog-current", Version: 2, Config: pricedCreditsCatalog("pro", "0.001")}
	historical := &CatalogRevision{ID: "catalog-historical", Version: 1, Config: pricedCreditsCatalog("legacy", "0.004")}
	store := &pricedCreditsStoreStub{
		active:       active,
		revisions:    map[int]*CatalogRevision{1: historical, 2: active},
		plan:         GetUserPlanResult{UserID: "user-1", PlanKey: "pro", RateCard: "pro"},
		leasePricing: &LeasePricingContext{CatalogVersion: 1, PlanKey: "legacy", RateCard: "legacy"},
	}
	service, err := NewCreditsService(store, CreditsServiceOptions{})
	if err != nil {
		t.Fatalf("NewCreditsService() error = %v", err)
	}

	result, err := service.RunBilledUsage(context.Background(), "user-1", RunBilledUsageOptions{
		Estimate:     pricedCreditsMetrics("2000"),
		OperationKey: "completion-42",
		DoWork: func(context.Context) (any, UsageMetrics, error) {
			return "generated", pricedCreditsMetrics("1500"), nil
		},
	})
	if err != nil {
		t.Fatalf("RunBilledUsage() error = %v", err)
	}
	if result.Result != "generated" {
		t.Fatalf("work result = %#v", result.Result)
	}
	assertAmount(t, store.reserveAmount, "0.002000")
	assertAmount(t, store.settleAmount, "0.006000")
	if store.reserveOpts.IdempotencyKey != "completion-42:reserve" || store.settleOptions.IdempotencyKey != "completion-42:settle" {
		t.Fatalf("replay keys = %q / %q", store.reserveOpts.IdempotencyKey, store.settleOptions.IdempotencyKey)
	}
	if store.reserveOpts.OperationUsageOptions.Measures["tokens"].String() != "2000" || store.settleOptions.OperationUsageOptions.Measures["tokens"].String() != "1500" {
		t.Fatalf("captured measures = reserve %#v / settle %#v", store.reserveOpts.Measures, store.settleOptions.Measures)
	}
}

func TestCatalogRefreshesUnpinnedPricingAfterTTL(t *testing.T) {
	t.Parallel()

	base := time.Date(2030, time.January, 1, 0, 0, 0, 0, time.UTC)
	store := &pricedCreditsStoreStub{
		active:    &CatalogRevision{ID: "catalog-1", Version: 1, Config: pricedCreditsCatalog("pro", "0.001")},
		revisions: map[int]*CatalogRevision{},
		plan:      GetUserPlanResult{UserID: "user-1", PlanKey: "pro", RateCard: "pro"},
	}
	catalog, err := NewCatalogServiceWithOptions(store, CatalogServiceOptions{CacheTTL: time.Minute})
	if err != nil {
		t.Fatalf("NewCatalogServiceWithOptions() error = %v", err)
	}
	now := base
	catalog.now = func() time.Time { return now }

	first, err := catalog.CalculateForUser(context.Background(), "user-1", pricedCreditsMetrics("1000"))
	if err != nil {
		t.Fatalf("first CalculateForUser() error = %v", err)
	}
	assertAmount(t, first.Total, "0.001000")
	if store.activeCalls != 1 {
		t.Fatalf("initial catalog reads = %d, want 1", store.activeCalls)
	}

	now = now.Add(30 * time.Second)
	if _, err := catalog.CalculateForUser(context.Background(), "user-1", pricedCreditsMetrics("1000")); err != nil {
		t.Fatalf("fresh CalculateForUser() error = %v", err)
	}
	if store.activeCalls != 1 {
		t.Fatalf("fresh catalog reads = %d, want 1", store.activeCalls)
	}

	store.active = &CatalogRevision{ID: "catalog-2", Version: 2, Config: pricedCreditsCatalog("pro", "0.002")}
	now = now.Add(time.Minute)
	second, err := catalog.CalculateForUser(context.Background(), "user-1", pricedCreditsMetrics("1000"))
	if err != nil {
		t.Fatalf("stale CalculateForUser() error = %v", err)
	}
	assertAmount(t, second.Total, "0.002000")
	if store.activeCalls != 2 {
		t.Fatalf("stale catalog reads = %d, want 2", store.activeCalls)
	}

	catalog.Invalidate()
	store.active = &CatalogRevision{ID: "catalog-3", Version: 3, Config: pricedCreditsCatalog("pro", "0.003")}
	third, err := catalog.CalculateForUser(context.Background(), "user-1", pricedCreditsMetrics("1000"))
	if err != nil {
		t.Fatalf("invalidated CalculateForUser() error = %v", err)
	}
	assertAmount(t, third.Total, "0.003000")
	if store.activeCalls != 3 {
		t.Fatalf("invalidated catalog reads = %d, want 3", store.activeCalls)
	}
}

func pricedCreditsCatalog(rateCard, rate string) map[string]any {
	return map[string]any{
		"version": 1,
		"pricing": map[string]any{
			"operations": map[string]any{
				"completion": map[string]any{
					"measures": map[string]any{"tokens": map[string]any{"unit": "token"}},
					"dimensions": map[string]any{
						"model":  map[string]any{"type": "string", "required": false},
						"region": map[string]any{"type": "string", "required": false},
					},
				},
			},
			"rate_cards": map[string]any{
				rateCard: map[string]any{
					"operations": map[string]any{
						"completion": map[string]any{
							"rules": []any{},
							"unmatched": map[string]any{
								"action": "charge",
								"charge": map[string]any{"type": "per_unit", "measure": "tokens", "unit_size": "1000", "rate": rate},
							},
						},
					},
				},
			},
		},
		"credits": map[string]any{},
	}
}

func pricedCreditsMetrics(tokens string) UsageMetrics {
	return UsageMetrics{
		Operation: "completion",
		Measures:  map[string]Amount{"tokens": MustAmount(tokens)},
		Dimensions: map[string]any{
			"model":  "gpt-4",
			"region": "us",
		},
	}
}

func assertAmount(t *testing.T, got Amount, want string) {
	t.Helper()
	if expected := MustAmount(want); !got.Equal(expected) {
		t.Fatalf("amount = %s, want %s", got, expected)
	}
}
