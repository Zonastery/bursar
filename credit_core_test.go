package bursar

import (
	"context"
	"errors"
	"math/big"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgtype"
)

// creditStoreStub embeds the complete portable contract while overriding only
// the methods exercised by a focused service test. It is deliberately not an
// accounting implementation: production mutations always go through a real
// PostgresStore RPC boundary.
type creditStoreStub struct {
	CreditStore
	createLease  func(context.Context, string, Amount, string, CreateLeaseOptions) (LeaseResult, error)
	settleLease  func(context.Context, string, string, Amount, SettleLeaseOptions) (DeductionResult, error)
	releaseLease func(context.Context, string, string) (ReleaseResult, error)
	deduct       func(context.Context, string, Amount, DeductWithAllowanceOptions) (DeductionResult, error)
}

func (s *creditStoreStub) CreateLease(ctx context.Context, userID string, amount Amount, operationType string, options CreateLeaseOptions) (LeaseResult, error) {
	return s.createLease(ctx, userID, amount, operationType, options)
}

func (s *creditStoreStub) SettleLease(ctx context.Context, userID, leaseID string, amount Amount, options SettleLeaseOptions) (DeductionResult, error) {
	return s.settleLease(ctx, userID, leaseID, amount, options)
}

func (s *creditStoreStub) ReleaseLease(ctx context.Context, userID, leaseID string) (ReleaseResult, error) {
	return s.releaseLease(ctx, userID, leaseID)
}

func (s *creditStoreStub) DeductWithAllowance(ctx context.Context, userID string, amount Amount, options DeductWithAllowanceOptions) (DeductionResult, error) {
	return s.deduct(ctx, userID, amount, options)
}

func committedLease(userID string, amount Amount) LeaseResult {
	expiresAt := time.Date(2030, time.January, 1, 0, 0, 0, 0, time.UTC)
	minimum := DecimalZero
	return LeaseResult{
		LeaseID:        "lease-1",
		UserID:         userID,
		Amount:         creditAmountPointer(amount),
		Available:      MustAmount("95"),
		ReservedTotal:  amount,
		MinimumBalance: &minimum,
		BillingMode:    BillingModeStrict,
		ExpiresAt:      &expiresAt,
	}
}

func TestCreditsServiceReserveScopesIdempotencyAndPolicy(t *testing.T) {
	t.Parallel()
	var gotOperation string
	var gotOptions CreateLeaseOptions
	store := &creditStoreStub{
		createLease: func(_ context.Context, userID string, amount Amount, operation string, options CreateLeaseOptions) (LeaseResult, error) {
			gotOperation = operation
			gotOptions = options
			return committedLease(userID, amount), nil
		},
	}
	service, err := NewCreditsService(store, CreditsServiceOptions{})
	if err != nil {
		t.Fatalf("NewCreditsService() error = %v", err)
	}

	result, err := service.Reserve(context.Background(), "user-1", MustAmount("5"), ReserveOptions{
		IdempotencyKey: " reserve-1 ",
		OperationType:  "completion",
	})
	if err != nil {
		t.Fatalf("Reserve() error = %v", err)
	}
	if result.LeaseID != "lease-1" {
		t.Fatalf("Reserve() lease ID = %q, want lease-1", result.LeaseID)
	}
	if gotOperation != "completion" {
		t.Fatalf("operation = %q, want completion", gotOperation)
	}
	if gotOptions.IdempotencyKey != "reserve-1" {
		t.Fatalf("idempotency key = %q, want trimmed stable key", gotOptions.IdempotencyKey)
	}
	if gotOptions.TTL != defaultLeaseTTL {
		t.Fatalf("TTL = %s, want %s", gotOptions.TTL, defaultLeaseTTL)
	}
	if gotOptions.BillingMode != BillingModeStrict || !gotOptions.Floor.Equal(DecimalZero) {
		t.Fatalf("policy = (%q, %s), want strict/0", gotOptions.BillingMode, gotOptions.Floor)
	}
}

func TestBilledOperationUsesScopedReplayKeys(t *testing.T) {
	t.Parallel()
	var reserveKey string
	var settleKey string
	store := &creditStoreStub{
		createLease: func(_ context.Context, userID string, amount Amount, _ string, options CreateLeaseOptions) (LeaseResult, error) {
			reserveKey = options.IdempotencyKey
			return committedLease(userID, amount), nil
		},
		settleLease: func(_ context.Context, userID, _ string, amount Amount, options SettleLeaseOptions) (DeductionResult, error) {
			settleKey = options.IdempotencyKey
			balance := MustAmount("92")
			return DeductionResult{EntryID: "entry-1", UsageChargeID: "usage-1", UserID: userID, Amount: amount, BalanceAfter: &balance}, nil
		},
	}
	service, err := NewCreditsService(store, CreditsServiceOptions{})
	if err != nil {
		t.Fatalf("NewCreditsService() error = %v", err)
	}
	operation, err := service.BeginBilledOperation(context.Background(), "user-1", BeginBilledOperationOptions{
		Estimate:     MustAmount("8"),
		OperationKey: "job-42",
	})
	if err != nil {
		t.Fatalf("BeginBilledOperation() error = %v", err)
	}
	if _, err := operation.Settle(context.Background(), MustAmount("3")); err != nil {
		t.Fatalf("BilledOperation.Settle() error = %v", err)
	}
	if reserveKey != "job-42:reserve" {
		t.Fatalf("reserve key = %q, want scoped key", reserveKey)
	}
	if settleKey != "job-42:settle" {
		t.Fatalf("settle key = %q, want scoped key", settleKey)
	}
}

func TestRunBilledReleasesLeaseWhenWorkFails(t *testing.T) {
	t.Parallel()
	released := false
	workErr := errors.New("worker failed")
	store := &creditStoreStub{
		createLease: func(_ context.Context, userID string, amount Amount, _ string, _ CreateLeaseOptions) (LeaseResult, error) {
			return committedLease(userID, amount), nil
		},
		releaseLease: func(_ context.Context, userID, leaseID string) (ReleaseResult, error) {
			released = true
			return ReleaseResult{UserID: userID, LeaseID: leaseID, Released: true}, nil
		},
	}
	service, err := NewCreditsService(store, CreditsServiceOptions{})
	if err != nil {
		t.Fatalf("NewCreditsService() error = %v", err)
	}
	_, err = service.RunBilled(context.Background(), "user-1", RunBilledOptions{
		Estimate:     MustAmount("8"),
		OperationKey: "job-43",
		DoWork: func(context.Context) (any, Amount, error) {
			return nil, DecimalZero, workErr
		},
	})
	if !errors.Is(err, workErr) {
		t.Fatalf("RunBilled() error = %v, want work error", err)
	}
	if !released {
		t.Fatal("RunBilled() did not release lease after work failure")
	}
}

func TestDeductMapsBusinessDenialToTypedError(t *testing.T) {
	t.Parallel()
	store := &creditStoreStub{
		deduct: func(_ context.Context, userID string, amount Amount, _ DeductWithAllowanceOptions) (DeductionResult, error) {
			return DeductionResult{UserID: userID, Amount: amount, ErrorCode: "insufficient_credits"}, nil
		},
	}
	service, err := NewCreditsService(store, CreditsServiceOptions{})
	if err != nil {
		t.Fatalf("NewCreditsService() error = %v", err)
	}
	_, err = service.Deduct(context.Background(), "user-1", MustAmount("3"), DeductWithAllowanceOptions{IdempotencyKey: "use-1"})
	bursarErr, ok := AsBursarError(err)
	if !ok || bursarErr.Code != ErrorCodeInsufficientCredits {
		t.Fatalf("Deduct() error = %#v, want INSUFFICIENT_CREDITS BursarError", err)
	}
}

func TestCreditsServicePostDeductionHooksArePostCommitAndFailureIsolated(t *testing.T) {
	t.Parallel()
	committed := false
	replayed := false
	balance := MustAmount("7")
	store := &creditStoreStub{
		deduct: func(_ context.Context, userID string, amount Amount, _ DeductWithAllowanceOptions) (DeductionResult, error) {
			committed = true
			return DeductionResult{EntryID: "entry-1", UserID: userID, Amount: amount, BalanceAfter: &balance, Idempotent: replayed}, nil
		},
	}
	service, err := NewCreditsService(store, CreditsServiceOptions{
		PostDeduction: func(context.Context, PostDeductionContext) error {
			return errors.New("hook failure must not affect committed deduction")
		},
	})
	if err != nil {
		t.Fatalf("NewCreditsService() error = %v", err)
	}
	service.AddPostDeductionHook(func(context.Context, PostDeductionContext) error {
		panic("hook panic must not affect committed deduction")
	})
	var got []PostDeductionContext
	remove := service.AddPostDeductionHook(func(_ context.Context, context PostDeductionContext) error {
		if !committed {
			t.Fatal("post-deduction hook ran before the store returned a committed result")
		}
		got = append(got, context)
		return nil
	})

	if _, err := service.Deduct(context.Background(), "user-1", MustAmount("3"), DeductWithAllowanceOptions{IdempotencyKey: "use-1"}); err != nil {
		t.Fatalf("Deduct() error = %v", err)
	}
	if len(got) != 1 {
		t.Fatalf("post-deduction hook calls = %d, want 1", len(got))
	}
	if got[0].Source != PostDeductionSourceDeduct || got[0].UserID != "user-1" || got[0].Deduction.EntryID != "entry-1" {
		t.Fatalf("post-deduction context = %#v, want committed direct deduction", got[0])
	}

	replayed = true
	if _, err := service.Deduct(context.Background(), "user-1", MustAmount("3"), DeductWithAllowanceOptions{IdempotencyKey: "use-1"}); err != nil {
		t.Fatalf("replayed Deduct() error = %v", err)
	}
	if len(got) != 1 {
		t.Fatalf("idempotent deduction invoked hook %d times, want 1", len(got))
	}

	remove()
	replayed = false
	if _, err := service.Deduct(context.Background(), "user-1", MustAmount("3"), DeductWithAllowanceOptions{IdempotencyKey: "use-2"}); err != nil {
		t.Fatalf("Deduct() after hook removal error = %v", err)
	}
	if len(got) != 1 {
		t.Fatalf("removed hook invoked %d times, want 1", len(got))
	}
}

func TestParseAmountPreservesPGNumericPrecision(t *testing.T) {
	t.Parallel()
	integer := big.NewInt(123456789)
	amount, err := parseAmount(pgtype.Numeric{Int: integer, Exp: -6, Valid: true}, "numeric")
	if err != nil {
		t.Fatalf("parseAmount() error = %v", err)
	}
	if !amount.Equal(MustAmount("123.456789")) {
		t.Fatalf("amount = %s, want 123.456789", amount)
	}
}

func TestPostgresClientOptionsNormalizeTenantScopedDefaults(t *testing.T) {
	t.Parallel()
	options, err := (PostgresClientOptions{
		TenantID:            "A0000000-0000-0000-0000-000000000001",
		ProviderEnvironment: ProviderEnvironmentTest,
	}).normalized()
	if err != nil {
		t.Fatalf("normalized() error = %v", err)
	}
	if options.TenantID != "a0000000-0000-0000-0000-000000000001" {
		t.Fatalf("tenant = %q, want canonical UUID", options.TenantID)
	}
	if options.AccessRole != PostgresAccessRoleClient {
		t.Fatalf("role = %q, want bursar_client", options.AccessRole)
	}
	if options.StatementTimeout != 30*time.Second || options.IdleTransactionTimeout != 30*time.Second {
		t.Fatalf("transaction deadlines = (%s, %s), want 30s defaults", options.StatementTimeout, options.IdleTransactionTimeout)
	}
	if _, err := normalizeTenantID("not-a-uuid"); err == nil {
		t.Fatal("normalizeTenantID accepted malformed UUID")
	}
}
