// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import (
	"context"
	"testing"
	"time"
)

func TestPostgresStorePureProjections(t *testing.T) {
	now := time.Date(2026, 8, 19, 12, 0, 0, 0, time.UTC)

	t.Run("exact numeric database values", func(t *testing.T) {
		values := []any{int(2), int8(2), int16(2), int32(2), uint(2), uint8(2), uint16(2), uint32(2), uint64(2)}
		for _, value := range values {
			amount, err := parseAmount(value, "amount")
			if err != nil || !amount.Equal(MustAmount("2")) {
				t.Errorf("parseAmount(%T) = %s, %v", value, amount, err)
			}
		}
		if _, err := parseAmount(true, "amount"); err == nil {
			t.Fatal("non-numeric amount accepted")
		}
	})

	t.Run("available", func(t *testing.T) {
		if got, err := availableFromState("user", nil, "state"); err != nil || !got.Available.Equal(DecimalZero) {
			t.Fatalf("empty available = %+v, %v", got, err)
		}
		valid := map[string]any{"balance": "5", "reserved": "2", "available": "3"}
		if got, err := availableFromState("user", valid, "state"); err != nil || !got.Available.Equal(MustAmount("3")) {
			t.Fatalf("available = %+v, %v", got, err)
		}
		for _, field := range []string{"balance", "reserved", "available"} {
			row := cloneAnyMap(valid)
			row[field] = nil
			if _, err := availableFromState("user", row, "state"); err == nil {
				t.Errorf("invalid %s accepted", field)
			}
		}
	})

	t.Run("usage record", func(t *testing.T) {
		valid := map[string]any{"requested": "1.25", "charge_id": "charge", "replayed": false}
		if got, err := usageRecordFromRow("user", valid); err != nil || got.UsageID != "charge" {
			t.Fatalf("usage record = %+v, %v", got, err)
		}
		if got, err := usageRecordFromRow("user", map[string]any{"requested": "1", "error_code": "denied"}); err != nil || got.ErrorCode != "denied" {
			t.Fatalf("usage denial = %+v, %v", got, err)
		}
		for _, row := range []map[string]any{
			{"requested": nil},
			{"requested": "1", "replayed": false},
			{"requested": "1", "charge_id": "charge", "replayed": "invalid"},
		} {
			if _, err := usageRecordFromRow("user", row); err == nil {
				t.Errorf("invalid usage row accepted: %#v", row)
			}
		}
	})

	t.Run("credit posting", func(t *testing.T) {
		posted := map[string]any{"entry_id": "entry", "balance_after": "5", "replayed": false}
		state := map[string]any{"balance": "5", "lifetime_purchased": "8"}
		if got, err := addCreditsResultFromRows("user", MustAmount("2"), posted, state, map[string]any{"bucket_key": "purchased"}); err != nil || got.EntryID != "entry" || got.Bucket != "purchased" {
			t.Fatalf("posted credit = %+v, %v", got, err)
		}
		if got, err := addCreditsResultFromRows("user", MustAmount("2"), posted, state, map[string]any{"value": "fallback"}); err != nil || got.Bucket != "fallback" {
			t.Fatalf("fallback grant bucket = %+v, %v", got, err)
		}
		if got, err := addCreditsResultFromRows("user", MustAmount("-1"), posted, state, nil); err != nil || !got.Amount.Equal(MustAmount("-1")) {
			t.Fatalf("credit correction = %+v, %v", got, err)
		}
		invalid := []struct {
			row   map[string]any
			state map[string]any
		}{
			{map[string]any{"error_code": "denied"}, nil},
			{map[string]any{"balance_after": "5", "replayed": false}, state},
			{map[string]any{"entry_id": "entry", "balance_after": nil, "replayed": false}, state},
			{map[string]any{"entry_id": "entry", "balance_after": "5", "replayed": "bad"}, state},
			{posted, map[string]any{"balance": nil, "lifetime_purchased": "8"}},
			{posted, map[string]any{"balance": "5", "lifetime_purchased": nil}},
		}
		for index, test := range invalid {
			if _, err := addCreditsResultFromRows("user", MustAmount("2"), test.row, test.state, nil); err == nil {
				t.Errorf("invalid posted credit %d accepted", index)
			}
		}
	})

	t.Run("lease projections", func(t *testing.T) {
		available := AvailableResult{UserID: "user", Available: MustAmount("8"), Reserved: MustAmount("2")}
		created := map[string]any{"lease_id": "lease", "reserved_amount": "2"}
		lease := map[string]any{"expires_at": now.Add(time.Hour), "minimum_balance": "0"}
		if got, err := createLeaseResultFromRows("user", BillingModeStrict, created, available, lease); err != nil || got.LeaseID != "lease" || got.BillingMode != BillingModeStrict {
			t.Fatalf("created lease = %+v, %v", got, err)
		}
		overdraft := cloneAnyMap(lease)
		overdraft["minimum_balance"] = "-5"
		if got, err := createLeaseResultFromRows("user", BillingModeStrict, created, available, overdraft); err != nil || got.BillingMode != BillingModeOverdraft {
			t.Fatalf("overdraft lease = %+v, %v", got, err)
		}
		if got, err := createLeaseResultFromRows("user", BillingModeStrict, map[string]any{"error_code": "insufficient", "reserved_amount": "1"}, available, nil); err != nil || got.ErrorCode != "insufficient" {
			t.Fatalf("lease denial = %+v, %v", got, err)
		}
		invalidCreates := []struct {
			row   map[string]any
			lease map[string]any
		}{
			{map[string]any{"error_code": "denied", "reserved_amount": "bad"}, nil},
			{map[string]any{"reserved_amount": "2"}, lease},
			{map[string]any{"lease_id": "lease", "reserved_amount": "bad"}, lease},
			{created, nil},
			{created, map[string]any{"expires_at": nil, "minimum_balance": "0"}},
			{created, map[string]any{"expires_at": now, "minimum_balance": nil}},
		}
		for index, test := range invalidCreates {
			if _, err := createLeaseResultFromRows("user", BillingModeStrict, test.row, available, test.lease); err == nil {
				t.Errorf("invalid created lease %d accepted", index)
			}
		}

		settled := map[string]any{"settled_amount": "1.5", "ledger_entry_id": "entry", "charge_id": "charge", "replayed": false}
		details := map[string]any{"balance_after": "6.5", "allowance_covered": "0.5", "bucket_breakdown": `{"general":1}`}
		if got, err := settlementResultFromRows("user", settled, details); err != nil || got.EntryID != "entry" || got.BalanceAfter == nil {
			t.Fatalf("settlement = %+v, %v", got, err)
		}
		if got, err := settlementResultFromRows("user", map[string]any{"settled_amount": "0", "error_code": "missing", "balance_after": "8"}, nil); err != nil || got.ErrorCode != "missing" {
			t.Fatalf("settlement denial = %+v, %v", got, err)
		}
		invalidSettlements := []struct {
			row     map[string]any
			details map[string]any
		}{
			{map[string]any{"settled_amount": nil}, nil},
			{map[string]any{"settled_amount": "0", "error_code": "missing", "balance_after": "bad"}, nil},
			{map[string]any{"settled_amount": "1", "charge_id": "charge", "replayed": false}, details},
			{map[string]any{"settled_amount": "1", "ledger_entry_id": "entry", "replayed": false}, details},
			{map[string]any{"settled_amount": "1", "ledger_entry_id": "entry", "charge_id": "charge", "replayed": "bad"}, details},
			{settled, nil},
			{settled, map[string]any{"balance_after": nil, "allowance_covered": "0", "bucket_breakdown": `{}`}},
			{settled, map[string]any{"balance_after": "1", "allowance_covered": nil, "bucket_breakdown": `{}`}},
			{settled, map[string]any{"balance_after": "1", "allowance_covered": "0", "bucket_breakdown": `bad`}},
		}
		for index, test := range invalidSettlements {
			if _, err := settlementResultFromRows("user", test.row, test.details); err == nil {
				t.Errorf("invalid settlement %d accepted", index)
			}
		}

		charged := map[string]any{"charged": "1.5", "allowance_covered": "0.5", "ledger_entry_id": "entry", "charge_id": "charge", "replayed": false}
		chargeDetails := map[string]any{"balance_after": "6.5", "bucket_breakdown": `{"general":1}`}
		if got, err := deductionResultFromRows("user", charged, chargeDetails); err != nil || got.EntryID != "entry" {
			t.Fatalf("deduction = %+v, %v", got, err)
		}
		if got, err := deductionResultFromRows("user", map[string]any{"charged": "0", "allowance_covered": "0", "error_code": "insufficient"}, nil); err != nil || got.ErrorCode != "insufficient" {
			t.Fatalf("deduction denial = %+v, %v", got, err)
		}
		invalidDeductions := []struct {
			row     map[string]any
			details map[string]any
		}{
			{map[string]any{"charged": nil}, nil},
			{map[string]any{"charged": "1", "allowance_covered": nil}, nil},
			{map[string]any{"charged": "1", "allowance_covered": "0", "charge_id": "charge", "replayed": false}, chargeDetails},
			{map[string]any{"charged": "1", "allowance_covered": "0", "ledger_entry_id": "entry", "replayed": false}, chargeDetails},
			{map[string]any{"charged": "1", "allowance_covered": "0", "ledger_entry_id": "entry", "charge_id": "charge", "replayed": "bad"}, chargeDetails},
			{charged, nil},
			{charged, map[string]any{"balance_after": nil, "bucket_breakdown": `{}`}},
			{charged, map[string]any{"balance_after": "1", "bucket_breakdown": `bad`}},
		}
		for index, test := range invalidDeductions {
			if _, err := deductionResultFromRows("user", test.row, test.details); err == nil {
				t.Errorf("invalid deduction %d accepted", index)
			}
		}

		renewed := map[string]any{"lease_id": "lease", "reserved_amount": "2"}
		if got, err := renewedLeaseResultFromRows("user", renewed, available, lease); err != nil || got.LeaseID != "lease" {
			t.Fatalf("renewed lease = %+v, %v", got, err)
		}
		if got, err := renewedLeaseResultFromRows("user", map[string]any{"error_code": "expired"}, available, nil); err != nil || got.ErrorCode != "expired" {
			t.Fatalf("renewal denial = %+v, %v", got, err)
		}
		if got, err := renewedLeaseResultFromRows("user", renewed, available, overdraft); err != nil || got.BillingMode != BillingModeOverdraft {
			t.Fatalf("overdraft renewal = %+v, %v", got, err)
		}
		invalidRenewals := []struct {
			row   map[string]any
			lease map[string]any
		}{
			{map[string]any{"reserved_amount": "2"}, lease},
			{map[string]any{"lease_id": "lease", "reserved_amount": "bad"}, lease},
			{renewed, nil},
			{renewed, map[string]any{"expires_at": nil, "minimum_balance": "0"}},
			{renewed, map[string]any{"expires_at": now, "minimum_balance": nil}},
		}
		for index, test := range invalidRenewals {
			if _, err := renewedLeaseResultFromRows("user", test.row, available, test.lease); err == nil {
				t.Errorf("invalid renewal %d accepted", index)
			}
		}

		pricing := map[string]any{"catalog_revision_no": 3, "plan_id": "plan", "plan_key": "pro", "rate_card": "standard"}
		if got, err := leasePricingContextFromRow(pricing); err != nil || got == nil || got.CatalogVersion != 3 {
			t.Fatalf("lease pricing = %+v, %v", got, err)
		}
		if got, err := leasePricingContextFromRow(nil); err != nil || got != nil {
			t.Fatalf("missing lease pricing = %+v, %v", got, err)
		}
		if _, err := leasePricingContextFromRow(map[string]any{"catalog_revision_no": "bad"}); err == nil {
			t.Fatal("invalid lease pricing accepted")
		}
		if got, err := releaseResultFromRow("user", "lease", map[string]any{"status": "released"}); err != nil || !got.Released {
			t.Fatalf("released lease = %+v, %v", got, err)
		}
		if got, err := releaseResultFromRow("user", "lease", map[string]any{"status": "expired"}); err != nil || got.Released || got.Reason != "expired" {
			t.Fatalf("expired lease release = %+v, %v", got, err)
		}
		for _, row := range []map[string]any{{}, {"a": 1, "b": 2}, {"status": nil}} {
			if _, err := releaseResultFromRow("user", "lease", row); err == nil {
				t.Errorf("invalid release accepted: %#v", row)
			}
		}
	})

	t.Run("buckets and grants", func(t *testing.T) {
		bucket := map[string]any{"bucket_key": "general", "label": "General", "priority": 1, "expires": false, "balance": "2.5"}
		if got, err := bucketBalancesFromRows("user", []map[string]any{bucket}); err != nil || !got.TotalBalance.Equal(MustAmount("2.5")) {
			t.Fatalf("buckets = %+v, %v", got, err)
		}
		for _, field := range []string{"bucket_key", "label", "priority", "expires", "balance"} {
			row := cloneAnyMap(bucket)
			row[field] = nil
			if _, err := bucketBalancesFromRows("user", []map[string]any{row}); err == nil {
				t.Errorf("invalid bucket %s accepted", field)
			}
		}

		award := map[string]any{"grant_event_id": "event", "grant_award_id": "award", "recipient_subject_id": "user", "ledger_entry_id": "entry", "amount": "2.5", "replayed": false}
		if got, err := grantProgramAwardsFromRows([]map[string]any{award}); err != nil || len(got) != 1 || !got[0].Amount.Equal(MustAmount("2.5")) {
			t.Fatalf("grant awards = %+v, %v", got, err)
		}
		if got, err := grantProgramAwardsFromRows([]map[string]any{{"error_code": "unknown", "replayed": false}}); err != nil || got[0].ErrorCode != "unknown" {
			t.Fatalf("grant denial = %+v, %v", got, err)
		}
		for _, row := range []map[string]any{
			{"replayed": false},
			{"amount": "bad", "replayed": false},
			{"amount": "1", "replayed": "bad"},
		} {
			if _, err := grantProgramAwardsFromRows([]map[string]any{row}); err == nil {
				t.Errorf("invalid award accepted: %#v", row)
			}
		}
	})

	t.Run("maintenance", func(t *testing.T) {
		sweep := map[string]any{"expired_count": 2, "expired_amount": "3.25", "expired_by_bucket": `{"general":3.25}`}
		if got, err := sweepResultFromRow(sweep, true); err != nil || got.ExpiredCount != 2 || !got.DryRun {
			t.Fatalf("sweep = %+v, %v", got, err)
		}
		for _, field := range []string{"expired_count", "expired_amount", "expired_by_bucket"} {
			row := cloneAnyMap(sweep)
			row[field] = "bad"
			if _, err := sweepResultFromRow(row, false); err == nil {
				t.Errorf("invalid sweep %s accepted", field)
			}
		}
		revoke := map[string]any{"revoked": "2", "balance_after": "3"}
		if got, err := revokeCreditsResultFromRow("user", "purchase", revoke); err != nil || !got.Revoked.Equal(MustAmount("2")) {
			t.Fatalf("revoke = %+v, %v", got, err)
		}
		for _, row := range []map[string]any{
			{"error_code": "denied"},
			{"revoked": nil, "balance_after": "3"},
			{"revoked": "2", "balance_after": nil},
		} {
			if _, err := revokeCreditsResultFromRow("user", "purchase", row); err == nil {
				t.Errorf("invalid revocation accepted: %#v", row)
			}
		}
	})

	t.Run("catalog", func(t *testing.T) {
		row := map[string]any{"id": "revision", "revision_no": 2, "created_at": now, "label": "stable", "status": "active"}
		if got, err := catalogRevisionSummariesFromRows([]map[string]any{row}); err != nil || len(got) != 1 || !got[0].Active {
			t.Fatalf("catalog summaries = %+v, %v", got, err)
		}
		for _, field := range []string{"id", "revision_no", "created_at"} {
			invalid := cloneAnyMap(row)
			invalid[field] = nil
			if _, err := catalogRevisionSummariesFromRows([]map[string]any{invalid}); err == nil {
				t.Errorf("invalid catalog %s accepted", field)
			}
		}
	})

	t.Run("plan assignment and refund", func(t *testing.T) {
		planRow := map[string]any{"plan_id": "plan", "plan_key": "pro", "plan_assigned_at": now, "assignment_state": "active"}
		if got, err := setUserPlanResultFromRow("user", planRow); err != nil || got.PlanKey != "pro" {
			t.Fatalf("plan assignment = %+v, %v", got, err)
		}
		if _, err := setUserPlanResultFromRow("user", map[string]any{"plan_assigned_at": nil}); err == nil {
			t.Fatal("invalid plan assignment accepted")
		}
		refund := map[string]any{"entry_id": "refund", "subject_id": "user", "amount": "1.5", "balance_after": "5"}
		if got, err := refundResultFromRow("original", refund); err != nil || got.RefundEntryID != "refund" || got.Amount == nil {
			t.Fatalf("refund = %+v, %v", got, err)
		}
		if got, err := refundResultFromRow("original", map[string]any{"entry_id": "refund", "error_code": "already_refunded"}); err != nil || got.RefundEntryID != "" || got.ErrorCode == "" {
			t.Fatalf("refund denial = %+v, %v", got, err)
		}
		for _, row := range []map[string]any{{"amount": "bad"}, {"amount": "1", "balance_after": "bad"}} {
			if _, err := refundResultFromRow("original", row); err == nil {
				t.Errorf("invalid refund accepted: %#v", row)
			}
		}
		migrationID := "00000000-0000-4000-8000-000000000099"
		if got, err := planMigrationStartFromRow(map[string]any{"migration_id": migrationID}); err != nil || got.MigrationID != migrationID {
			t.Fatalf("migration start = %+v, %v", got, err)
		}
		for _, row := range []map[string]any{{}, {"a": "x", "b": "y"}, {"migration_id": nil}, {"migration_id": "bad"}} {
			if _, err := planMigrationStartFromRow(row); err == nil {
				t.Errorf("invalid migration start accepted: %#v", row)
			}
		}
		if got, err := planMigrationBatchFromRow(map[string]any{"migrated": 2, "done": false, "next_cursor": "cursor"}); err != nil || got.Migrated != 2 || got.NextCursor != "cursor" {
			t.Fatalf("migration batch = %+v, %v", got, err)
		}
		for _, row := range []map[string]any{{"migrated": nil, "done": false}, {"migrated": 1, "done": "bad"}} {
			if _, err := planMigrationBatchFromRow(row); err == nil {
				t.Errorf("invalid migration batch accepted: %#v", row)
			}
		}
	})

	t.Run("quota", func(t *testing.T) {
		state := map[string]any{
			"user_id": "user", "quota_key": "tokens", "operation_key": "completion", "measure_key": "tokens",
			"quota_limit": "100", "consumed": "10", "reserved": "2", "remaining": "88", "overage": "0",
			"enforcement": "allow", "window_start": now, "window_end": now.Add(time.Hour), "emit_at_percent": []int32{50, 90},
		}
		if got, err := quotaStatesFromRows([]map[string]any{state}); err != nil || len(got) != 1 || len(got[0].EmitAtPercent) != 2 {
			t.Fatalf("quota states = %+v, %v", got, err)
		}
		for _, field := range []string{"quota_limit", "consumed", "reserved", "remaining", "overage", "window_start", "window_end", "emit_at_percent"} {
			row := cloneAnyMap(state)
			row[field] = "bad"
			if _, err := quotaStatesFromRows([]map[string]any{row}); err == nil {
				t.Errorf("invalid quota %s accepted", field)
			}
		}
		allowance := map[string]any{"plan_id": "plan", "allowance_remaining": "5", "period_start": now, "period_end": now.Add(time.Hour)}
		if got, err := allowanceFromRow(allowance); err != nil || got == nil || !got.AllowanceRemaining.Equal(MustAmount("5")) {
			t.Fatalf("allowance = %+v, %v", got, err)
		}
		if got, err := allowanceFromRow(nil); err != nil || got != nil {
			t.Fatalf("nil allowance = %+v, %v", got, err)
		}
		for _, field := range []string{"allowance_remaining", "period_start", "period_end"} {
			row := cloneAnyMap(allowance)
			row[field] = nil
			if _, err := allowanceFromRow(row); err == nil {
				t.Errorf("invalid allowance %s accepted", field)
			}
		}
		event := map[string]any{"event_id": "event", "created_at": now, "threshold_percent": 90}
		if got, err := quotaEventsFromRows([]map[string]any{event}); err != nil || len(got) != 1 || got[0].ThresholdPercent == nil {
			t.Fatalf("quota events = %+v, %v", got, err)
		}
		if got, err := quotaEventsFromRows([]map[string]any{{"created_at": now}}); err != nil || got[0].ThresholdPercent != nil {
			t.Fatalf("quota event without threshold = %+v, %v", got, err)
		}
		for _, row := range []map[string]any{{"created_at": nil}, {"created_at": now, "threshold_percent": "bad"}} {
			if _, err := quotaEventsFromRows([]map[string]any{row}); err == nil {
				t.Errorf("invalid quota event accepted: %#v", row)
			}
		}
	})

	t.Run("reporting", func(t *testing.T) {
		userRow := map[string]any{"subject_id": "user", "total_spend": "4", "charge_count": 2}
		if got, err := spendByUserFromRows([]map[string]any{userRow}); err != nil || got[0].UserID != "user" {
			t.Fatalf("spend by user = %+v, %v", got, err)
		}
		if got, err := spendByUserFromRows([]map[string]any{{"user_id": "fallback", "total_spend": "4", "entry_count": 2}}); err != nil || got[0].UserID != "fallback" {
			t.Fatalf("fallback spend by user = %+v, %v", got, err)
		}
		modelRow := map[string]any{"model": "model", "total_spend": "4", "charge_count": 2}
		if got, err := spendByModelFromRows([]map[string]any{modelRow}); err != nil || got[0].Model != "model" {
			t.Fatalf("spend by model = %+v, %v", got, err)
		}
		if _, err := spendByModelFromRows([]map[string]any{{"model": "model", "total_spend": "4", "entry_count": 2}}); err != nil {
			t.Fatalf("fallback spend count: %v", err)
		}
		if got, err := topUsersFromRows([]map[string]any{userRow}); err != nil || got[0].UserID != "user" {
			t.Fatalf("top users = %+v, %v", got, err)
		}
		daily := map[string]any{"day": now, "total_spend": "4", "charge_count": 2}
		if got, err := dailySpendFromRows([]map[string]any{daily}); err != nil || got[0].EntryCount != 2 {
			t.Fatalf("daily spend = %+v, %v", got, err)
		}
		if _, err := dailySpendFromRows([]map[string]any{{"date": "2026-08-19", "total_spend": "4", "entry_count": 2}}); err != nil {
			t.Fatalf("daily fallbacks: %v", err)
		}
		stats := map[string]any{"total_credits_consumed": "4", "active_users": 2, "avg_daily_spend": "2", "top_model": "model", "top_user": "user"}
		if got, err := aggregateStatsFromRow(stats); err != nil || got.ActiveUsers != 2 {
			t.Fatalf("aggregate stats = %+v, %v", got, err)
		}

		invalidRows := []func() error{
			func() error {
				_, err := spendByUserFromRows([]map[string]any{{"total_spend": nil, "charge_count": 1}})
				return err
			},
			func() error { _, err := spendByUserFromRows([]map[string]any{{"total_spend": "1"}}); return err },
			func() error {
				_, err := spendByModelFromRows([]map[string]any{{"total_spend": nil, "charge_count": 1}})
				return err
			},
			func() error { _, err := spendByModelFromRows([]map[string]any{{"total_spend": "1"}}); return err },
			func() error { _, err := topUsersFromRows([]map[string]any{{"total_spend": nil}}); return err },
			func() error {
				_, err := dailySpendFromRows([]map[string]any{{"day": nil, "date": nil, "total_spend": "1", "charge_count": 1}})
				return err
			},
			func() error {
				_, err := dailySpendFromRows([]map[string]any{{"day": now, "total_spend": nil, "charge_count": 1}})
				return err
			},
			func() error {
				_, err := dailySpendFromRows([]map[string]any{{"day": now, "total_spend": "1"}})
				return err
			},
			func() error {
				_, err := aggregateStatsFromRow(map[string]any{"total_credits_consumed": nil})
				return err
			},
			func() error {
				_, err := aggregateStatsFromRow(map[string]any{"total_credits_consumed": "1", "active_users": nil})
				return err
			},
			func() error {
				_, err := aggregateStatsFromRow(map[string]any{"total_credits_consumed": "1", "active_users": 1, "avg_daily_spend": nil})
				return err
			},
		}
		for index, invoke := range invalidRows {
			if err := invoke(); err == nil {
				t.Errorf("invalid reporting row %d accepted", index)
			}
		}
	})

	t.Run("teams", func(t *testing.T) {
		team := map[string]any{"team_id": "team", "name": "Team", "idempotent": false}
		if got, err := createTeamResultFromRow(team); err != nil || got.TeamID != "team" {
			t.Fatalf("create team = %+v, %v", got, err)
		}
		for _, field := range []string{"team_id", "name", "idempotent"} {
			row := cloneAnyMap(team)
			row[field] = nil
			if _, err := createTeamResultFromRow(row); err == nil {
				t.Errorf("invalid team %s accepted", field)
			}
		}
		balance := map[string]any{"team_id": "team", "name": "Team", "balance": "5", "member_count": 2}
		if got, err := teamBalanceFromRow(balance); err != nil || got == nil || got.MemberCount != 2 {
			t.Fatalf("team balance = %+v, %v", got, err)
		}
		if got, err := teamBalanceFromRow(nil); err != nil || got != nil {
			t.Fatalf("nil team balance = %+v, %v", got, err)
		}
		for _, field := range []string{"balance", "member_count"} {
			row := cloneAnyMap(balance)
			row[field] = nil
			if _, err := teamBalanceFromRow(row); err == nil {
				t.Errorf("invalid team balance %s accepted", field)
			}
		}
		member := map[string]any{"user_id": "user", "role": "member", "spend_cap": "2", "total_spent": "1"}
		if got, err := teamMembersFromRows([]map[string]any{member}); err != nil || len(got) != 1 || got[0].SpendCap == nil {
			t.Fatalf("team members = %+v, %v", got, err)
		}
		for _, row := range []map[string]any{
			{"role": "member", "spend_cap": "bad", "total_spent": "1"},
			{"role": "member", "total_spent": nil},
			{"role": "invalid", "total_spent": "1"},
		} {
			if _, err := teamMembersFromRows([]map[string]any{row}); err == nil {
				t.Errorf("invalid team member accepted: %#v", row)
			}
		}
		if got, err := addTeamMemberResultFromRow("team", "user", TeamRoleMember, map[string]any{"added": true}); err != nil || got.UserID != "user" {
			t.Fatalf("added team member = %+v, %v", got, err)
		}
		for _, row := range []map[string]any{{}, {"a": true, "b": false}, {"added": "bad"}, {"added": false}} {
			if _, err := addTeamMemberResultFromRow("team", "user", TeamRoleMember, row); err == nil {
				t.Errorf("invalid member result accepted: %#v", row)
			}
		}
		deduction := map[string]any{"entry_id": "entry", "team_id": "team", "subject_id": "user", "amount": "1.25", "balance_after": "3.75", "replayed": false}
		if got, err := teamDeductionResultFromRow(MustAmount("1.25"), deduction); err != nil || got.EntryID != "entry" || got.TeamBalanceAfter == nil {
			t.Fatalf("team deduction = %+v, %v", got, err)
		}
		if got, err := teamDeductionResultFromRow(MustAmount("1.25"), map[string]any{"amount": "0", "replayed": false, "error_code": "insufficient"}); err != nil || got.ErrorCode != "insufficient" {
			t.Fatalf("team denial = %+v, %v", got, err)
		}
		for _, row := range []map[string]any{
			{"amount": nil, "replayed": false},
			{"amount": "1.25", "balance_after": "bad", "replayed": false},
			{"amount": "1.25", "replayed": "bad"},
			{"amount": "2", "replayed": false},
		} {
			if _, err := teamDeductionResultFromRow(MustAmount("1.25"), row); err == nil {
				t.Errorf("invalid team deduction accepted: %#v", row)
			}
		}
	})
}

func TestPostgresStoreValidationMatrix(t *testing.T) {
	ctx := context.Background()
	store := &PostgresStore{}
	negative := MustAmount("-1")
	positiveFloor := MustAmount("1")
	invalidJSON := CreditMetadata{"bad": func() {}}
	invalidUsage := OperationUsageOptions{Dimensions: map[string]any{"bad": func() {}}}
	one := 1
	now := time.Now().UTC()

	tests := []struct {
		name   string
		invoke func() error
	}{
		{"balance user", func() error { _, err := store.GetBalance(ctx, ""); return err }},
		{"available user", func() error { _, err := store.GetAvailable(ctx, ""); return err }},
		{"add user", func() error {
			_, err := store.AddCredits(ctx, "", MustAmount("1"), AddCreditsOptions{IdempotencyKey: "key"})
			return err
		}},
		{"add key", func() error {
			_, err := store.AddCredits(ctx, "user", MustAmount("1"), AddCreditsOptions{})
			return err
		}},
		{"add metadata", func() error {
			_, err := store.AddCredits(ctx, "user", MustAmount("1"), AddCreditsOptions{IdempotencyKey: "key", Metadata: invalidJSON})
			return err
		}},
		{"deduct user", func() error {
			_, err := store.DeductWithAllowance(ctx, "", MustAmount("1"), DeductWithAllowanceOptions{IdempotencyKey: "key"})
			return err
		}},
		{"deduct amount", func() error {
			_, err := store.DeductWithAllowance(ctx, "user", negative, DeductWithAllowanceOptions{IdempotencyKey: "key"})
			return err
		}},
		{"deduct key", func() error {
			_, err := store.DeductWithAllowance(ctx, "user", MustAmount("1"), DeductWithAllowanceOptions{})
			return err
		}},
		{"deduct payload", func() error {
			_, err := store.DeductWithAllowance(ctx, "user", MustAmount("1"), DeductWithAllowanceOptions{OperationUsageOptions: invalidUsage, IdempotencyKey: "key"})
			return err
		}},
		{"record user", func() error {
			_, err := store.RecordUsage(ctx, "", "op", MustAmount("1"), RecordUsageOptions{IdempotencyKey: "key"})
			return err
		}},
		{"record operation", func() error {
			_, err := store.RecordUsage(ctx, "user", "", MustAmount("1"), RecordUsageOptions{IdempotencyKey: "key"})
			return err
		}},
		{"record amount", func() error {
			_, err := store.RecordUsage(ctx, "user", "op", negative, RecordUsageOptions{IdempotencyKey: "key"})
			return err
		}},
		{"record key", func() error {
			_, err := store.RecordUsage(ctx, "user", "op", MustAmount("1"), RecordUsageOptions{})
			return err
		}},
		{"record payload", func() error {
			_, err := store.RecordUsage(ctx, "user", "op", MustAmount("1"), RecordUsageOptions{OperationUsageOptions: invalidUsage, IdempotencyKey: "key"})
			return err
		}},
		{"lease user", func() error {
			_, err := store.CreateLease(ctx, "", MustAmount("1"), "op", CreateLeaseOptions{IdempotencyKey: "key"})
			return err
		}},
		{"lease operation", func() error {
			_, err := store.CreateLease(ctx, "user", MustAmount("1"), "", CreateLeaseOptions{IdempotencyKey: "key"})
			return err
		}},
		{"lease amount", func() error {
			_, err := store.CreateLease(ctx, "user", negative, "op", CreateLeaseOptions{IdempotencyKey: "key"})
			return err
		}},
		{"lease key", func() error {
			_, err := store.CreateLease(ctx, "user", MustAmount("1"), "op", CreateLeaseOptions{})
			return err
		}},
		{"lease mode", func() error {
			_, err := store.CreateLease(ctx, "user", MustAmount("1"), "op", CreateLeaseOptions{IdempotencyKey: "key", BillingMode: "bad"})
			return err
		}},
		{"lease ttl", func() error {
			_, err := store.CreateLease(ctx, "user", MustAmount("1"), "op", CreateLeaseOptions{IdempotencyKey: "key", TTL: -time.Second})
			return err
		}},
		{"lease concurrency", func() error {
			zero := 0
			_, err := store.CreateLease(ctx, "user", MustAmount("1"), "op", CreateLeaseOptions{IdempotencyKey: "key", MaxConcurrent: &zero})
			return err
		}},
		{"lease strict floor", func() error {
			_, err := store.CreateLease(ctx, "user", MustAmount("1"), "op", CreateLeaseOptions{IdempotencyKey: "key", BillingMode: BillingModeStrict, Floor: negative})
			return err
		}},
		{"lease overdraft floor", func() error {
			_, err := store.CreateLease(ctx, "user", MustAmount("1"), "op", CreateLeaseOptions{IdempotencyKey: "key", BillingMode: BillingModeOverdraft, Floor: positiveFloor})
			return err
		}},
		{"lease payload", func() error {
			_, err := store.CreateLease(ctx, "user", MustAmount("1"), "op", CreateLeaseOptions{IdempotencyKey: "key", OperationUsageOptions: invalidUsage})
			return err
		}},
		{"settle user", func() error {
			_, err := store.SettleLease(ctx, "", "lease", MustAmount("1"), SettleLeaseOptions{})
			return err
		}},
		{"settle lease", func() error {
			_, err := store.SettleLease(ctx, "user", "", MustAmount("1"), SettleLeaseOptions{})
			return err
		}},
		{"settle amount", func() error {
			_, err := store.SettleLease(ctx, "user", "lease", negative, SettleLeaseOptions{})
			return err
		}},
		{"settle key", func() error {
			_, err := store.SettleLease(ctx, "user", "lease", MustAmount("1"), SettleLeaseOptions{IdempotencyKey: " "})
			return err
		}},
		{"settle payload", func() error {
			_, err := store.SettleLease(ctx, "user", "lease", MustAmount("1"), SettleLeaseOptions{OperationUsageOptions: invalidUsage})
			return err
		}},
		{"pricing user", func() error { _, err := store.GetLeasePricingContext(ctx, "", "lease"); return err }},
		{"pricing lease", func() error { _, err := store.GetLeasePricingContext(ctx, "user", ""); return err }},
		{"release user", func() error { _, err := store.ReleaseLease(ctx, "", "lease"); return err }},
		{"release lease", func() error { _, err := store.ReleaseLease(ctx, "user", ""); return err }},
		{"renew user", func() error { _, err := store.RenewLease(ctx, "", "lease", time.Second); return err }},
		{"renew lease", func() error { _, err := store.RenewLease(ctx, "user", "", time.Second); return err }},
		{"renew ttl", func() error { _, err := store.RenewLease(ctx, "user", "lease", -time.Second); return err }},
		{"expire limit", func() error { _, err := store.ExpireLeases(ctx, maxMaintenanceBatchSize+1); return err }},
		{"buckets user", func() error { _, err := store.GetBucketBalances(ctx, ""); return err }},
		{"grant trigger", func() error { _, err := store.ExecuteGrantProgram(ctx, ExecuteGrantProgramRequest{}); return err }},
		{"grant program", func() error {
			_, err := store.ExecuteGrantProgram(ctx, ExecuteGrantProgramRequest{Trigger: "manual"})
			return err
		}},
		{"grant subject", func() error {
			_, err := store.ExecuteGrantProgram(ctx, ExecuteGrantProgramRequest{Trigger: "manual", ProgramKey: "award"})
			return err
		}},
		{"grant key", func() error {
			_, err := store.ExecuteGrantProgram(ctx, ExecuteGrantProgramRequest{Trigger: "manual", ProgramKey: "award", SubjectID: "user"})
			return err
		}},
		{"grant metadata", func() error {
			_, err := store.ExecuteGrantProgram(ctx, ExecuteGrantProgramRequest{Trigger: "manual", ProgramKey: "award", SubjectID: "user", EventKey: "key", Metadata: invalidJSON})
			return err
		}},
		{"sweep limit", func() error {
			_, err := store.SweepExpiredCredits(ctx, false, "", maxMaintenanceBatchSize+1)
			return err
		}},
		{"sweep user", func() error { _, err := store.SweepExpiredCredits(ctx, false, " ", 1); return err }},
		{"revoke user", func() error { _, err := store.RevokeCreditsByEntryType(ctx, "", "purchase"); return err }},
		{"revoke type", func() error { _, err := store.RevokeCreditsByEntryType(ctx, "user", ""); return err }},
		{"publish config", func() error { _, err := store.PublishAndActivateCatalog(ctx, nil, "", CatalogRollout{}); return err }},
		{"publish json", func() error {
			_, err := store.PublishAndActivateCatalog(ctx, map[string]any{"bad": func() {}}, "", CatalogRollout{})
			return err
		}},
		{"draft config", func() error { _, err := store.PublishCatalogDraft(ctx, nil, ""); return err }},
		{"draft json", func() error {
			_, err := store.PublishCatalogDraft(ctx, map[string]any{"bad": func() {}}, "")
			return err
		}},
		{"catalog version", func() error { _, err := store.GetCatalogRevision(ctx, 0); return err }},
		{"activate version", func() error { _, err := store.ActivateCatalogRevision(ctx, 0, CatalogRollout{}); return err }},
		{"plan user", func() error { _, err := store.GetUserPlan(ctx, ""); return err }},
		{"feature user", func() error { _, err := store.CheckFeature(ctx, "", "feature"); return err }},
		{"feature key", func() error { _, err := store.CheckFeature(ctx, "user", ""); return err }},
		{"set plan user", func() error { _, err := store.SetUserPlan(ctx, "", "pro", SetUserPlanOptions{}); return err }},
		{"set plan key", func() error { _, err := store.SetUserPlan(ctx, "user", "", SetUserPlanOptions{}); return err }},
		{"unset plan user", func() error { _, err := store.UnsetUserPlan(ctx, ""); return err }},
		{"pin user", func() error { _, err := store.SetPlanRevisionPin(ctx, "", true); return err }},
		{"due limit", func() error { _, err := store.ApplyDuePlanChanges(ctx, maxMaintenanceBatchSize+1); return err }},
		{"migration target", func() error { _, err := store.StartPlanMigration(ctx, "", ""); return err }},
		{"migration id", func() error { _, err := store.MigratePlanBatch(ctx, "", 1); return err }},
		{"migration batch", func() error {
			_, err := store.MigratePlanBatch(ctx, "migration", maxMaintenanceBatchSize+1)
			return err
		}},
		{"quota user", func() error { _, err := store.GetQuotaState(ctx, "", ""); return err }},
		{"allowance user", func() error { _, err := store.CheckAllowance(ctx, ""); return err }},
		{"quota events user", func() error { _, err := store.ListQuotaEvents(ctx, "", ListQuotaEventsOptions{}); return err }},
		{"quota events limit", func() error {
			_, err := store.ListQuotaEvents(ctx, "user", ListQuotaEventsOptions{Limit: maxPageSize + 1})
			return err
		}},
		{"quota events cursor", func() error {
			_, err := store.ListQuotaEvents(ctx, "user", ListQuotaEventsOptions{AfterID: "event"})
			return err
		}},
		{"refund entry", func() error { _, err := store.RefundCredits(ctx, "", nil, "", nil, "key"); return err }},
		{"refund key", func() error { _, err := store.RefundCredits(ctx, "entry", nil, "", nil, ""); return err }},
		{"refund amount", func() error { _, err := store.RefundCredits(ctx, "entry", &negative, "", nil, "key"); return err }},
		{"refund metadata", func() error { _, err := store.RefundCredits(ctx, "entry", nil, "", invalidJSON, "key"); return err }},
		{"spend user range", func() error { _, err := store.SpendByUser(ctx, time.Time{}, now); return err }},
		{"spend model range", func() error { _, err := store.SpendByModel(ctx, now, now.Add(-time.Second)); return err }},
		{"top range", func() error { _, err := store.TopUsers(ctx, 1, time.Time{}, now); return err }},
		{"top limit", func() error { _, err := store.TopUsers(ctx, maxPageSize+1, now, now.Add(time.Second)); return err }},
		{"daily range", func() error { _, err := store.DailySpend(ctx, time.Time{}, now); return err }},
		{"stats range", func() error { _, err := store.AggregateStats(ctx, now, now.Add(-time.Second)); return err }},
		{"ledger user", func() error { _, err := store.ListLedgerEntries(ctx, "", ListLedgerEntriesOptions{}); return err }},
		{"ledger limit", func() error {
			_, err := store.ListLedgerEntries(ctx, "user", ListLedgerEntriesOptions{Limit: maxLedgerPageSize + 1})
			return err
		}},
		{"usage charges user", func() error { _, err := store.ListUsageCharges(ctx, "", ListUsageChargesOptions{}); return err }},
		{"usage charges limit", func() error {
			_, err := store.ListUsageCharges(ctx, "user", ListUsageChargesOptions{Limit: maxLedgerPageSize + 1})
			return err
		}},
		{"usage charges cursor", func() error {
			_, err := store.ListUsageCharges(ctx, "user", ListUsageChargesOptions{Cursor: &UsageChargeCursor{UsageID: "usage"}})
			return err
		}},
		{"ledger entry user", func() error { _, err := store.GetLedgerEntry(ctx, "", "entry"); return err }},
		{"ledger entry id", func() error { _, err := store.GetLedgerEntry(ctx, "user", ""); return err }},
		{"team owner", func() error {
			_, err := store.CreateTeam(ctx, "", "team", CreateTeamOptions{IdempotencyKey: "key"})
			return err
		}},
		{"team name", func() error {
			_, err := store.CreateTeam(ctx, "owner", "", CreateTeamOptions{IdempotencyKey: "key"})
			return err
		}},
		{"team key", func() error { _, err := store.CreateTeam(ctx, "owner", "team", CreateTeamOptions{}); return err }},
		{"team amount", func() error {
			_, err := store.CreateTeam(ctx, "owner", "team", CreateTeamOptions{IdempotencyKey: "key", InitialBalance: negative})
			return err
		}},
		{"team balance", func() error { _, err := store.GetTeamBalance(ctx, ""); return err }},
		{"member team", func() error { _, err := store.AddTeamMember(ctx, "", "user", AddTeamMemberOptions{}); return err }},
		{"member user", func() error { _, err := store.AddTeamMember(ctx, "team", "", AddTeamMemberOptions{}); return err }},
		{"member role", func() error {
			_, err := store.AddTeamMember(ctx, "team", "user", AddTeamMemberOptions{Role: "bad"})
			return err
		}},
		{"member cap", func() error {
			_, err := store.AddTeamMember(ctx, "team", "user", AddTeamMemberOptions{SpendCap: &negative})
			return err
		}},
		{"members team", func() error { _, err := store.GetTeamMembers(ctx, ""); return err }},
		{"remove team", func() error { _, err := store.RemoveTeamMember(ctx, "", "user"); return err }},
		{"remove user", func() error { _, err := store.RemoveTeamMember(ctx, "team", ""); return err }},
		{"deduct team", func() error {
			_, err := store.DeductTeam(ctx, "", "user", MustAmount("1"), TeamDeductionOptions{Operation: "op", IdempotencyKey: "key"})
			return err
		}},
		{"deduct member", func() error {
			_, err := store.DeductTeam(ctx, "team", "", MustAmount("1"), TeamDeductionOptions{Operation: "op", IdempotencyKey: "key"})
			return err
		}},
		{"deduct team amount", func() error {
			_, err := store.DeductTeam(ctx, "team", "user", DecimalZero, TeamDeductionOptions{Operation: "op", IdempotencyKey: "key"})
			return err
		}},
		{"deduct team key", func() error {
			_, err := store.DeductTeam(ctx, "team", "user", MustAmount("1"), TeamDeductionOptions{Operation: "op"})
			return err
		}},
		{"deduct team operation", func() error {
			_, err := store.DeductTeam(ctx, "team", "user", MustAmount("1"), TeamDeductionOptions{IdempotencyKey: "key"})
			return err
		}},
		{"deduct team metadata", func() error {
			_, err := store.DeductTeam(ctx, "team", "user", MustAmount("1"), TeamDeductionOptions{Operation: "op", IdempotencyKey: "key", Metadata: invalidJSON})
			return err
		}},
	}

	_ = one
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if err := test.invoke(); err == nil {
				t.Fatal("invalid input accepted")
			}
		})
	}
}
