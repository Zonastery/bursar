// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import (
	"testing"
	"time"

	"github.com/google/uuid"
)

// TestPostgresStoreSpendReadModels exercises the read-side projections that
// production reporting uses after a real charge has been committed. Keeping
// this on PostgreSQL catches RPC column-shape drift that mapper-only tests
// cannot detect.
func TestPostgresStoreSpendReadModels(t *testing.T) {
	ctx, cancel, config := financialContext(t)
	defer cancel()
	store := openPostgresIntegrationStore(t, ctx, config, config.tenantID)
	defer store.Close()

	userID := uuid.NewString()
	model := "store-wave9-model-" + userID
	expiresAt := time.Now().UTC().Add(time.Hour)
	added, err := store.AddCredits(ctx, userID, MustAmount("8.125000"), AddCreditsOptions{
		Type: "purchase", Bucket: "purchased", ExpiresAt: &expiresAt,
		Metadata: CreditMetadata{"source": "reporting-test"}, IdempotencyKey: "store-wave9-fund-" + userID,
	})
	if err != nil || added.EntryID == "" {
		t.Fatalf("fund reporting user = %+v, error = %v", added, err)
	}
	deduction, err := store.DeductWithAllowance(ctx, userID, MustAmount("2.125000"), DeductWithAllowanceOptions{
		Operation: "completion",
		OperationUsageOptions: OperationUsageOptions{
			Model: model, Region: "us-east",
			Measures:   map[string]Amount{"tokens": MustAmount("1")},
			Dimensions: map[string]any{"region": "us-east"},
		},
		Metadata:       CreditMetadata{"source": "reporting-test"},
		IdempotencyKey: "store-wave9-deduct-" + userID,
	})
	if err != nil || deduction.EntryID == "" {
		t.Fatalf("commit reporting charge = %+v, error = %v", deduction, err)
	}

	available, err := store.GetAvailable(ctx, userID)
	if err != nil || !available.Available.Equal(MustAmount("6")) {
		t.Fatalf("available balance = %+v, error = %v", available, err)
	}
	adjustment, err := store.AddCredits(ctx, userID, MustAmount("-0.125000"), AddCreditsOptions{
		Type: "adjustment", Bucket: "purchased", Metadata: CreditMetadata{"reason": "correction"},
		IdempotencyKey: "store-wave9-adjustment-" + userID,
	})
	if err != nil || adjustment.EntryID == "" || !adjustment.Amount.Equal(MustAmount("-0.125000")) {
		t.Fatalf("negative correction = %+v, error = %v", adjustment, err)
	}
	entry, err := store.GetLedgerEntry(ctx, userID, deduction.EntryID)
	if err != nil || entry == nil || entry.EntryID != deduction.EntryID {
		t.Fatalf("ledger entry = %+v, error = %v", entry, err)
	}
	if recorded, err := store.RecordUsage(ctx, userID, "completion", MustAmount("1"), RecordUsageOptions{
		OperationUsageOptions: OperationUsageOptions{Model: model, Measures: map[string]Amount{"tokens": MustAmount("1")}},
		Metadata:              CreditMetadata{"source": "reporting-test"}, IdempotencyKey: "store-wave9-record-" + userID,
	}); err != nil || recorded.UsageID == "" {
		t.Fatalf("record usage = %+v, error = %v", recorded, err)
	}

	from := time.Now().UTC().Add(-time.Minute)
	to := time.Now().UTC().Add(time.Minute)
	byUser, err := store.SpendByUser(ctx, from, to)
	if err != nil {
		t.Fatalf("spend by user: %v", err)
	}
	byModel, err := store.SpendByModel(ctx, from, to)
	if err != nil {
		t.Fatalf("spend by model: %v", err)
	}
	// The shared disposable database intentionally retains rows across focused
	// Go runs. Request the contract's bounded maximum instead of assuming this
	// low-spend fixture must rank in an arbitrary top ten.
	top, err := store.TopUsers(ctx, maxPageSize, from, to)
	if err != nil {
		t.Fatalf("top users: %v", err)
	}
	daily, err := store.DailySpend(ctx, from, to)
	if err != nil {
		t.Fatalf("daily spend: %v", err)
	}
	stats, err := store.AggregateStats(ctx, from, to)
	if err != nil {
		t.Fatalf("aggregate usage stats: %v", err)
	}

	if len(byUser) == 0 || len(byModel) == 0 || len(top) == 0 || len(daily) == 0 {
		t.Fatalf("reporting projections unexpectedly empty: users=%+v models=%+v top=%+v daily=%+v stats=%+v", byUser, byModel, top, daily, stats)
	}
	foundUser := false
	for _, row := range byUser {
		if row.UserID == userID {
			foundUser = row.TotalSpend.Equal(MustAmount("2.125000")) && row.EntryCount >= 1
		}
	}
	if !foundUser {
		t.Fatalf("user spend did not contain committed charge: %+v", byUser)
	}
	foundModel := false
	for _, row := range byModel {
		if row.Model == model {
			foundModel = row.TotalSpend.Equal(MustAmount("2.125000")) && row.EntryCount >= 1
		}
	}
	if !foundModel {
		t.Fatalf("model spend did not contain committed charge: %+v", byModel)
	}
	foundTopUser := false
	for _, row := range top {
		if row.UserID == userID && row.TotalSpend.Equal(MustAmount("2.125000")) {
			foundTopUser = true
		}
	}
	if !foundTopUser {
		t.Fatalf("top users did not contain committed charge: %+v", top)
	}
	if daily[0].TotalSpend.LessThan(MustAmount("2.125000")) || daily[0].EntryCount < 1 {
		t.Fatalf("daily spend = %+v", daily[0])
	}
	if stats.TotalCreditsConsumed.LessThan(MustAmount("2.125000")) || stats.ActiveUsers < 1 {
		t.Fatalf("aggregate stats = %+v", stats)
	}

	charges, err := store.ListUsageCharges(ctx, userID, ListUsageChargesOptions{
		Limit: 10, IncludeRecordOnly: postgresStoreBoolPointer(false), From: &from, To: &to,
	})
	if err != nil || len(charges.Items) == 0 {
		t.Fatalf("charged usage receipts = %+v, error = %v", charges, err)
	}
}

func postgresStoreBoolPointer(value bool) *bool { return &value }
