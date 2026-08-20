package bursar

import (
	"context"
	"testing"
	"time"
)

type creditOptionsStoreStub struct {
	CreditStore
	balance    BalanceResult
	sweepUsers []string
	deduct     func(string, Amount) DeductionResult
}

func (*creditOptionsStoreStub) Close() error { return nil }

func (s *creditOptionsStoreStub) GetBalance(context.Context, string) (BalanceResult, error) {
	return s.balance, nil
}

func (s *creditOptionsStoreStub) SweepExpiredCredits(_ context.Context, dryRun bool, userID string, limit int) (SweepResult, error) {
	if dryRun || limit != 100 {
		panic("lazy expiry must use a committed bounded sweep")
	}
	s.sweepUsers = append(s.sweepUsers, userID)
	return SweepResult{}, nil
}

func (s *creditOptionsStoreStub) DeductWithAllowance(_ context.Context, userID string, amount Amount, _ DeductWithAllowanceOptions) (DeductionResult, error) {
	if s.deduct != nil {
		return s.deduct(userID, amount), nil
	}
	balanceAfter := MustAmount("4")
	return DeductionResult{EntryID: "entry-1", UserID: userID, Amount: amount, BalanceAfter: &balanceAfter}, nil
}

func TestCreditsServiceLazyExpiryRunsBeforeBalanceAndDeduction(t *testing.T) {
	t.Parallel()

	store := &creditOptionsStoreStub{balance: BalanceResult{UserID: "user-1", Balance: MustAmount("5")}}
	service, err := NewCreditsService(store, CreditsServiceOptions{LazyExpiry: true})
	if err != nil {
		t.Fatalf("NewCreditsService() error = %v", err)
	}
	if _, err := service.GetBalance(context.Background(), "user-1"); err != nil {
		t.Fatalf("GetBalance() error = %v", err)
	}
	if _, err := service.Deduct(context.Background(), "user-1", MustAmount("1"), DeductWithAllowanceOptions{IdempotencyKey: "charge-1"}); err != nil {
		t.Fatalf("Deduct() error = %v", err)
	}
	if len(store.sweepUsers) != 2 || store.sweepUsers[0] != "user-1" || store.sweepUsers[1] != "user-1" {
		t.Fatalf("lazy expiry users = %#v, want two user-scoped sweeps", store.sweepUsers)
	}
}

func TestCreditsServiceLowBalanceCallbackIsEdgeTriggeredBoundedAndFailureIsolated(t *testing.T) {
	t.Parallel()

	store := &creditOptionsStoreStub{deduct: func(userID string, amount Amount) DeductionResult {
		balanceAfter := MustAmount("8")
		return DeductionResult{EntryID: "entry-" + userID, UserID: userID, Amount: amount, BalanceAfter: &balanceAfter}
	}}
	var callbacks []CreditEvent
	service, err := NewCreditsService(store, CreditsServiceOptions{LowBalanceConfig: &LowBalanceConfig{
		Thresholds:      []Amount{MustAmount("5"), MustAmount("10")},
		MaxTrackedUsers: 2,
		OnTrigger: func(_ context.Context, event CreditEvent) {
			callbacks = append(callbacks, event)
			panic("callback failure must not change a committed deduction")
		},
	}})
	if err != nil {
		t.Fatalf("NewCreditsService() error = %v", err)
	}

	charge := func(userID string) {
		t.Helper()
		result, chargeErr := service.Deduct(context.Background(), userID, MustAmount("7"), DeductWithAllowanceOptions{IdempotencyKey: "charge-" + userID})
		if chargeErr != nil || result.EntryID == "" {
			t.Fatalf("Deduct(%q) = %#v, %v", userID, result, chargeErr)
		}
	}
	charge("user-1")
	charge("user-2")
	charge("user-3")
	charge("user-3")
	if len(callbacks) != 3 {
		t.Fatalf("callbacks = %d, want one edge per first crossing", len(callbacks))
	}
	for _, event := range callbacks {
		if threshold, ok := event.Data["threshold"].(Amount); !ok || !threshold.Equal(MustAmount("10")) {
			t.Fatalf("low-balance event = %#v", event)
		}
	}
	if len(service.lowBalanceState) != 2 {
		t.Fatalf("tracked users = %d, want bounded size 2", len(service.lowBalanceState))
	}
	if _, retained := service.lowBalanceState["user-1"]; retained {
		t.Fatal("least-recently-used low-balance state was not evicted")
	}

	charge("user-1")
	if len(callbacks) != 4 {
		t.Fatalf("callback count after evicted user crosses again = %d, want 4", len(callbacks))
	}
}

func TestCreditsServiceDefaultsLowBalanceToZeroCrossing(t *testing.T) {
	t.Parallel()

	store := &creditOptionsStoreStub{deduct: func(userID string, amount Amount) DeductionResult {
		balanceAfter := DecimalZero
		return DeductionResult{EntryID: "entry-1", UserID: userID, Amount: amount, BalanceAfter: &balanceAfter}
	}}
	emitter := NewCreditEventEmitter()
	var lowBalance []CreditEvent
	emitter.On(CreditEventLowBalance, func(_ context.Context, event CreditEvent) { lowBalance = append(lowBalance, event) })
	service, err := NewCreditsService(store, CreditsServiceOptions{EventSink: emitter})
	if err != nil {
		t.Fatalf("NewCreditsService() error = %v", err)
	}
	if _, err := service.Deduct(context.Background(), "user-1", MustAmount("2"), DeductWithAllowanceOptions{IdempotencyKey: "charge-1"}); err != nil {
		t.Fatalf("Deduct() error = %v", err)
	}
	if len(lowBalance) != 1 {
		t.Fatalf("zero-crossing events = %d, want 1", len(lowBalance))
	}
	threshold, ok := lowBalance[0].Data["threshold"].(Amount)
	if !ok || !threshold.IsZero() {
		t.Fatalf("zero-crossing threshold = %#v", lowBalance[0].Data["threshold"])
	}
}

func TestCreditsServiceRejectsNegativeCatalogCacheTTL(t *testing.T) {
	t.Parallel()

	ttl := -time.Second
	if _, err := NewCreditsService(&creditOptionsStoreStub{}, CreditsServiceOptions{CatalogCacheTTL: &ttl}); err == nil {
		t.Fatal("NewCreditsService() accepted a negative catalog cache TTL")
	}
}
