// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import (
	"context"
	"errors"
	"testing"
)

type creditsCoverageErrorStore struct {
	CreditStore
	err error
}

func (s creditsCoverageErrorStore) GetBalance(context.Context, string) (BalanceResult, error) {
	return BalanceResult{}, nil
}

func (s creditsCoverageErrorStore) AddCredits(context.Context, string, Amount, AddCreditsOptions) (AddCreditsResult, error) {
	return AddCreditsResult{}, s.err
}

func (s creditsCoverageErrorStore) DeductWithAllowance(context.Context, string, Amount, DeductWithAllowanceOptions) (DeductionResult, error) {
	return DeductionResult{}, s.err
}

func (s creditsCoverageErrorStore) SweepExpiredCredits(context.Context, bool, string, int) (SweepResult, error) {
	return SweepResult{}, s.err
}

func (s creditsCoverageErrorStore) RefundCredits(context.Context, string, *Amount, string, CreditMetadata, string) (RefundResult, error) {
	return RefundResult{}, s.err
}

func (creditsCoverageErrorStore) Close() error { return nil }

func TestCreditsServicePreservesStoreAndLazyExpiryErrors(t *testing.T) {
	t.Parallel()
	want := errors.New("store unavailable")
	store := creditsCoverageErrorStore{err: want}
	ctx := context.Background()

	lazy := &CreditsService{store: store, lazyExpiry: true}
	if _, err := lazy.GetBalance(ctx, "user-1"); !errors.Is(err, want) {
		t.Fatalf("GetBalance() lazy expiry error = %v", err)
	}
	if _, err := lazy.Deduct(ctx, "user-1", MustAmount("1"), DeductWithAllowanceOptions{IdempotencyKey: "deduct-1"}); !errors.Is(err, want) {
		t.Fatalf("Deduct() lazy expiry error = %v", err)
	}

	service := &CreditsService{store: store}
	if _, err := service.Deduct(ctx, "user-1", MustAmount("1"), DeductWithAllowanceOptions{IdempotencyKey: "deduct-2"}); !errors.Is(err, want) {
		t.Fatalf("Deduct() store error = %v", err)
	}
	if _, err := service.AddCredits(ctx, "user-1", MustAmount("1"), AddCreditsOptions{}); !errors.Is(err, want) {
		t.Fatalf("AddCredits() store error = %v", err)
	}
	if _, err := service.DeductCredits(ctx, "user-1", MustAmount("1"), AddCreditsOptions{}); !errors.Is(err, want) {
		t.Fatalf("DeductCredits() store error = %v", err)
	}
	if _, err := service.RefundCredits(ctx, "entry-1", nil, "test", nil, "refund-1"); !errors.Is(err, want) {
		t.Fatalf("RefundCredits() store error = %v", err)
	}
}

func TestDeductUsageRejectsMissingIdempotencyKey(t *testing.T) {
	t.Parallel()
	service := &CreditsService{catalog: &CatalogService{}}
	if _, err := service.DeductUsage(context.Background(), "user-1", UsageMetrics{Operation: "chat"}, PricedUsageOptions{}); err == nil {
		t.Fatal("DeductUsage() accepted a missing idempotency key")
	}
	if _, err := service.RecordUsageMetrics(context.Background(), "user-1", UsageMetrics{Operation: "chat"}, PricedUsageRecordOptions{}); err == nil {
		t.Fatal("RecordUsageMetrics() accepted a missing idempotency key")
	}
}
