package bursar

import (
	"context"
	"testing"
	"time"
)

func TestCreditsServiceRejectsUnsafeFinancialConfiguration(t *testing.T) {
	positiveFloor := MustAmount("1")
	zeroConcurrency := 0
	tests := map[string]CreditsServiceOptions{
		"unknown policy":     {Policy: "unknown"},
		"positive overdraft": {Policy: CreditPolicyOverdraft, OverdraftFloor: &positiveFloor},
		"zero concurrency":   {MaxConcurrent: &zeroConcurrency},
		"negative lease ttl": {DefaultLeaseTTL: -time.Second},
		"mixed low balance":  {LowBalance: []Amount{MustAmount("1")}, LowBalanceConfig: &LowBalanceConfig{}},
		"unbounded tracking": {LowBalanceConfig: &LowBalanceConfig{MaxTrackedUsers: -1}},
		"negative threshold": {LowBalance: []Amount{MustAmount("-1")}},
	}
	for name, options := range tests {
		t.Run(name, func(t *testing.T) {
			if _, err := NewCreditsService(&creditOptionsStoreStub{}, options); err == nil {
				t.Fatal("unsafe credits configuration accepted")
			}
		})
	}
	if _, err := NewCreditsService(nil, CreditsServiceOptions{}); err == nil {
		t.Fatal("nil credit store accepted")
	}
}

func TestNilCreditsServiceFailsClosedAcrossFinancialOperations(t *testing.T) {
	var service *CreditsService
	ctx := context.Background()
	tests := map[string]func() error{
		"balance": func() error {
			_, err := service.GetBalance(ctx, "user")
			return err
		},
		"available": func() error {
			_, err := service.GetAvailable(ctx, "user")
			return err
		},
		"grant": func() error {
			_, err := service.AddCredits(ctx, "user", MustAmount("1"), AddCreditsOptions{})
			return err
		},
		"raw debit": func() error {
			_, err := service.DeductCredits(ctx, "user", MustAmount("1"), AddCreditsOptions{})
			return err
		},
		"deduct": func() error {
			_, err := service.Deduct(ctx, "user", DecimalZero, DeductWithAllowanceOptions{})
			return err
		},
		"deduct usage": func() error {
			_, err := service.DeductUsage(ctx, "user", UsageMetrics{}, PricedUsageOptions{})
			return err
		},
		"reserve": func() error {
			_, err := service.Reserve(ctx, "user", DecimalZero, ReserveOptions{})
			return err
		},
		"reserve usage": func() error {
			_, err := service.ReserveUsage(ctx, "user", UsageMetrics{}, ReserveOptions{})
			return err
		},
		"settle": func() error {
			_, err := service.Settle(ctx, "user", "lease", DecimalZero, SettleOptions{})
			return err
		},
		"settle usage": func() error {
			_, err := service.SettleUsage(ctx, "user", "lease", UsageMetrics{}, SettleOptions{})
			return err
		},
		"release": func() error {
			_, err := service.Release(ctx, "user", "lease")
			return err
		},
		"renew": func() error {
			_, err := service.Renew(ctx, "user", "lease", time.Minute)
			return err
		},
		"afford": func() error {
			_, err := service.CanAfford(ctx, "user", DecimalZero, CanAffordOptions{})
			return err
		},
		"afford usage": func() error {
			_, err := service.CanAffordUsage(ctx, "user", UsageMetrics{}, CanAffordOptions{})
			return err
		},
		"subscription grant": func() error {
			_, err := service.GrantSubscriptionCycle(ctx, "user", MustAmount("1"), GrantSubscriptionCycleOptions{})
			return err
		},
		"usage receipt": func() error {
			_, err := service.RecordUsage(ctx, "user", "generate", DecimalZero, RecordUsageOptions{})
			return err
		},
		"usage metrics receipt": func() error {
			_, err := service.RecordUsageMetrics(ctx, "user", UsageMetrics{}, PricedUsageRecordOptions{})
			return err
		},
		"team debit": func() error {
			_, err := service.DeductTeam(ctx, "team", "user", DecimalZero, TeamDeductionOptions{})
			return err
		},
		"team usage debit": func() error {
			_, err := service.DeductTeamUsage(ctx, "team", "user", UsageMetrics{}, PricedTeamDeductionOptions{})
			return err
		},
		"begin billed": func() error {
			_, err := service.BeginBilledOperation(ctx, "user", BeginBilledOperationOptions{})
			return err
		},
		"begin billed usage": func() error {
			_, err := service.BeginBilledUsageOperation(ctx, "user", BeginBilledUsageOperationOptions{})
			return err
		},
		"resume billed": func() error {
			_, err := service.ResumeBilledOperation("user", "lease", "operation", "", nil)
			return err
		},
	}
	for name, operation := range tests {
		t.Run(name, func(t *testing.T) {
			if err := operation(); err == nil {
				t.Fatal("nil credits service operation succeeded")
			}
		})
	}
	if service.Catalog() != nil || service.Store() != nil {
		t.Fatal("nil credits service exposed capabilities")
	}
	if err := service.Close(); err != nil {
		t.Fatalf("nil Close() error = %v", err)
	}
	service.AddPostDeductionHook(nil)()
}

func TestCreditsOptionalBackendAndHookInitialization(t *testing.T) {
	if isNilCreditsReadBackend(42) {
		t.Fatal("non-nil scalar backend was treated as nil")
	}
	service := &CreditsService{}
	unsubscribe := service.AddPostDeductionHook(func(context.Context, PostDeductionContext) error { return nil })
	if len(service.postDeductionHooks) != 1 {
		t.Fatal("post-deduction hook registry was not initialized")
	}
	unsubscribe()
	if len(service.postDeductionHooks) != 0 {
		t.Fatal("post-deduction hook was not removed")
	}
}
