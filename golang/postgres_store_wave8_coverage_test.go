// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import (
	"context"
	"testing"
	"time"
)

func TestPostgresStorePlanProjectionMappers(t *testing.T) {
	empty, err := userPlanFromRow("user-1", nil)
	if err != nil || empty.UserID != "user-1" || len(empty.Entitlements) != 0 || len(empty.AllowedOperations) != 0 {
		t.Fatalf("empty plan = %+v, error = %v", empty, err)
	}

	assignedAt := time.Date(2026, 8, 19, 10, 11, 12, 0, time.UTC)
	endsAt := assignedAt.Add(time.Hour)
	row := map[string]any{
		"user_id":                         "user-1",
		"plan_id":                         "plan-pro",
		"plan_key":                        "pro",
		"plan_label":                      "Pro",
		"credit_allowance_amount":         "10.125000",
		"credit_allowance_priority":       2,
		"credit_allowance_reset_count":    1,
		"credit_allowance_reset_unit":     "month",
		"credit_allowance_reset_anchor":   "plan_assignment",
		"credit_allowance_reset_timezone": "UTC",
		"entitlements": map[string]any{
			"enabled": map[string]any{"value": true},
			"tier":    `{"value":"pro"}`,
		},
		"allowed_operations":      []any{"completion", "record"},
		"admission_max_in_flight": 3,
		"operation_admission": map[string]any{
			"completion": map[string]any{"max_in_flight": 2},
			"stream":     `{"max_in_flight":1}`,
		},
		"credit_policy_type":      "prepaid",
		"credit_limit":            "99.500000",
		"plan_assigned_at":        assignedAt,
		"plan_assignment_ends_at": endsAt.Format(time.RFC3339Nano),
		"assignment_source_type":  "checkout",
		"assignment_source_id":    "sub-1",
		"catalog_revision_pinned": "true",
		"catalog_revision_no":     "7",
	}
	got, err := userPlanFromRow("user-1", row)
	if err != nil {
		t.Fatalf("full plan = %+v, error = %v", got, err)
	}
	if got.PlanID != "plan-pro" || got.PlanKey != "pro" || got.Allowance == nil || !got.Allowance.Amount.Equal(MustAmount("10.125000")) {
		t.Fatalf("plan identity/allowance = %+v", got)
	}
	if got.Entitlements["enabled"].Value != true || got.Entitlements["tier"].Value != "pro" || len(got.AllowedOperations) != 2 {
		t.Fatalf("plan projection = %+v", got)
	}
	if got.Admission == nil || got.Admission.MaxInFlight == nil || *got.Admission.MaxInFlight != 3 || got.Admission.Operations["stream"].MaxInFlight == nil {
		t.Fatalf("admission projection = %+v", got.Admission)
	}
	if got.CreditPolicy == nil || got.CreditPolicy.CreditLimit == nil || !got.CreditPolicy.CreditLimit.Equal(MustAmount("99.500000")) || got.CatalogVersion == nil || *got.CatalogVersion != 7 {
		t.Fatalf("credit/revision projection = %+v", got)
	}

	for _, value := range []any{nil, `[]`, map[string]any{"enabled": true}} {
		if _, err := entitlementMap(value, "entitlement"); value == nil && err != nil {
			t.Fatalf("nil entitlement map: %v", err)
		}
	}
	for name, value := range map[string]any{
		"bad json":        "not-json",
		"bad entitlement": map[string]any{"enabled": 42},
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := entitlementMap(value, "entitlement"); err == nil {
				t.Fatal("malformed entitlement accepted")
			}
		})
	}
	for name, value := range map[string]map[string]any{
		"empty":         {},
		"max only":      {"admission_max_in_flight": "4"},
		"operation raw": {"operation_admission": map[string]any{"completion": `{"max_in_flight":2}`}},
	} {
		t.Run("admission/"+name, func(t *testing.T) {
			policy, err := admissionPolicy(value, "admission")
			if err != nil || (name == "empty" && policy != nil) || (name != "empty" && policy == nil) {
				t.Fatalf("admissionPolicy(%#v) = %+v, %v", value, policy, err)
			}
		})
	}
	for _, value := range []map[string]any{
		{"admission_max_in_flight": "bad"},
		{"operation_admission": `[]`},
		{"operation_admission": map[string]any{"completion": 7}},
	} {
		if _, err := admissionPolicy(value, "admission"); err == nil {
			t.Errorf("malformed admission accepted: %#v", value)
		}
	}

	malformed := []map[string]any{
		{"credit_allowance_amount": "1"},
		{"entitlements": `[]`},
		{"allowed_operations": []any{nil}},
		{"catalog_revision_pinned": "maybe"},
	}
	for _, value := range malformed {
		if _, err := userPlanFromRow("user-1", value); err == nil {
			t.Errorf("malformed user plan accepted: %#v", value)
		}
	}
}

func TestPostgresStoreLedgerAndUsageMappers(t *testing.T) {
	at := time.Date(2026, 8, 19, 10, 11, 12, 0, time.UTC)
	ledgerRow := map[string]any{
		"entry_id": "entry-1", "account_id": "account-1", "actor_user_id": "actor-1",
		"amount": "2.500000", "entry_type": "usage", "operation": "completion",
		"reference_entry_id": "ref-1", "idempotency_key": "key-1", "metadata": `{"model":"x"}`, "created_at": at,
	}
	entry, err := ledgerEntryFromRow(ledgerRow, "ledger")
	if err != nil || entry.EntryID != "entry-1" || !entry.Amount.Equal(MustAmount("2.500000")) || entry.Metadata["model"] != "x" {
		t.Fatalf("ledger entry = %+v, error = %v", entry, err)
	}
	usageRow := map[string]any{
		"usage_id": "usage-1", "account_id": "account-1", "operation": "completion",
		"requested": "3", "charged": "2", "allowance_requested": "1", "allowance_covered": "1",
		"billing_disposition": "charged", "feature": "enabled", "model": "x", "region": "us",
		"event_at": at.Format(time.RFC3339Nano), "created_at": at, "idempotency_key": "usage-key", "metadata": `{"ok":true}`,
	}
	charge, err := usageChargeFromRow(usageRow, "usage")
	if err != nil || charge.UsageID != "usage-1" || !charge.Charged.Equal(MustAmount("2")) || charge.Metadata["ok"] != true {
		t.Fatalf("usage charge = %+v, error = %v", charge, err)
	}
	for _, key := range []string{"entry_id", "account_id", "amount", "created_at"} {
		row := cloneAnyMap(ledgerRow)
		delete(row, key)
		if _, err := ledgerEntryFromRow(row, "ledger"); err == nil {
			t.Errorf("ledger row missing %s accepted", key)
		}
	}
	for _, key := range []string{"usage_id", "account_id", "requested", "charged", "allowance_requested", "allowance_covered", "event_at", "created_at"} {
		row := cloneAnyMap(usageRow)
		delete(row, key)
		if _, err := usageChargeFromRow(row, "usage"); err == nil {
			t.Errorf("usage row missing %s accepted", key)
		}
	}
}

func TestPostgresStoreFinancialValidationBoundaries(t *testing.T) {
	ctx := context.Background()
	store := &PostgresStore{}
	if _, err := store.GetBalance(ctx, ""); err == nil {
		t.Fatal("empty user accepted by GetBalance")
	}
	if _, err := store.GetAvailable(ctx, ""); err == nil {
		t.Fatal("empty user accepted by GetAvailable")
	}
	if _, err := store.AddCredits(ctx, "user", DecimalZero, AddCreditsOptions{}); err == nil {
		t.Fatal("empty add-credit idempotency key accepted")
	}
	if _, err := store.DeductWithAllowance(ctx, "user", MustAmount("-1"), DeductWithAllowanceOptions{IdempotencyKey: "key"}); err == nil {
		t.Fatal("negative deduction accepted")
	}
	if _, err := store.RecordUsage(ctx, "user", "", DecimalZero, RecordUsageOptions{}); err == nil {
		t.Fatal("empty usage operation accepted")
	}
	if _, err := store.CreateLease(ctx, "user", DecimalZero, "completion", CreateLeaseOptions{IdempotencyKey: "key", TTL: -time.Second}); err == nil {
		t.Fatal("negative lease TTL accepted")
	}
	if _, err := store.SettleLease(ctx, "user", "", DecimalZero, SettleLeaseOptions{}); err == nil {
		t.Fatal("empty lease ID accepted")
	}
	if _, err := store.ReleaseLease(ctx, "user", ""); err == nil {
		t.Fatal("empty release lease ID accepted")
	}
	if _, err := store.RenewLease(ctx, "user", "lease", 0); err == nil {
		t.Fatal("zero renew TTL accepted")
	}
	if _, err := store.RefundCredits(ctx, "entry", nil, "", nil, ""); err == nil {
		t.Fatal("empty refund idempotency key accepted")
	}
	if _, err := store.GetQuotaState(ctx, "", ""); err == nil {
		t.Fatal("empty quota user accepted")
	}
	if _, err := store.ListQuotaEvents(ctx, "", ListQuotaEventsOptions{}); err == nil {
		t.Fatal("empty quota event user accepted")
	}
	if _, err := store.CheckFeature(ctx, "", "feature"); err == nil {
		t.Fatal("empty feature user accepted")
	}
	if _, err := store.StartPlanMigration(ctx, "", ""); err == nil {
		t.Fatal("empty migration target accepted")
	}
	if _, err := store.MigratePlanBatch(ctx, "", 1); err == nil {
		t.Fatal("empty migration ID accepted")
	}
	if _, err := store.SpendByUser(ctx, time.Time{}, time.Time{}); err == nil {
		t.Fatal("empty spend range accepted")
	}
	if _, err := store.TopUsers(ctx, 0, time.Now().UTC().Add(-time.Hour), time.Now().UTC()); err == nil {
		t.Fatal("zero top-user limit accepted")
	}
}

func TestPostgresStoreFeatureQuotaAndMigrationPostgres(t *testing.T) {
	ctx, cancel, config := financialContext(t)
	defer cancel()
	sdk, store := newFinancialSDK(t, ctx, config, &financialProvider{name: "alpha"})

	catalog := financialCatalogConfig(t)
	catalog["pricing"] = map[string]any{
		"operations": map[string]any{"completion": map[string]any{
			"measures":   map[string]any{"tokens": map[string]any{"unit": "token"}},
			"dimensions": map[string]any{},
		}},
		"rate_cards": map[string]any{"standard": map[string]any{"operations": map[string]any{
			"completion": map[string]any{"rules": []any{}, "unmatched": map[string]any{"action": "charge", "charge": map[string]any{"type": "per_unit", "measure": "tokens", "rate": "1"}}},
		}}},
	}
	entitlements := catalog["entitlements"].(map[string]any)
	entitlements["features"] = map[string]any{"enabled": map[string]any{"type": "boolean", "default": false}}
	plans := catalog["plans"].(map[string]any)
	pro := plans["pro"].(map[string]any)
	pro["features"] = map[string]any{"enabled": true}
	pro["quotas"] = map[string]any{"assignment_tokens": map[string]any{
		"operation": "completion", "measure": "tokens", "limit": "500000",
		"window":      map[string]any{"type": "plan_assignment", "interval": map[string]any{"unit": "month", "count": 1}, "timezone": "UTC"},
		"enforcement": "allow", "emit_at_percent": []any{50, 90},
	}}
	starter := plans["starter"].(map[string]any)
	starter["features"] = map[string]any{"enabled": false}
	starter["credit_allowance"] = map[string]any{"amount": "10", "priority": 1, "window": map[string]any{"type": "rolling", "duration": map[string]any{"unit": "day", "count": 30}}}
	if _, err := sdk.Catalog.PublishAndActivate(ctx, catalog, "go-store-wave8", newAssignmentsRollout(catalog)); err != nil {
		if typed, ok := AsBursarError(err); ok && typed.Unwrap() != nil {
			t.Fatalf("publish feature/quota catalog: %v: %v", err, typed.Unwrap())
		}
		t.Fatalf("publish feature/quota catalog: %v", err)
	}

	proUser, starterUser := "store-wave8-pro", "store-wave8-starter"
	assignedPro, err := sdk.Credits.SetUserPlan(ctx, proUser, "pro", SetUserPlanOptions{})
	if err != nil || assignedPro.PlanID == "" {
		t.Fatalf("assign pro = %+v, error = %v", assignedPro, err)
	}
	assignedStarter, err := sdk.Credits.SetUserPlan(ctx, starterUser, "starter", SetUserPlanOptions{})
	if err != nil || assignedStarter.PlanID == "" {
		t.Fatalf("assign starter = %+v, error = %v", assignedStarter, err)
	}
	if feature, err := store.CheckFeature(ctx, proUser, "enabled"); err != nil || !feature.HasFeature || feature.Value != true {
		t.Fatalf("enabled feature = %+v, error = %v", feature, err)
	}
	if feature, err := store.CheckFeature(ctx, starterUser, "enabled"); err != nil || feature.HasFeature || feature.Value != false {
		t.Fatalf("disabled feature = %+v, error = %v", feature, err)
	}
	if state, err := store.GetQuotaState(ctx, proUser, "assignment_tokens"); err != nil {
		t.Fatalf("quota state: %v", err)
	} else if len(state) == 0 {
		t.Fatal("configured quota state was empty")
	}
	if _, err := store.ListQuotaEvents(ctx, proUser, ListQuotaEventsOptions{Limit: 10}); err != nil {
		t.Fatalf("quota events: %v", err)
	}
	if allowance, err := store.CheckAllowance(ctx, starterUser); err != nil || allowance == nil || !allowance.AllowanceRemaining.Equal(MustAmount("10")) {
		t.Fatalf("allowance = %+v, error = %v", allowance, err)
	}

	migration, err := store.StartPlanMigration(ctx, assignedStarter.PlanID, assignedPro.PlanID)
	if err != nil || migration.MigrationID == "" {
		t.Fatalf("start migration = %+v, error = %v", migration, err)
	}
	batch, err := store.MigratePlanBatch(ctx, migration.MigrationID, 100)
	if err != nil || batch.Migrated < 0 {
		t.Fatalf("migration batch = %+v, error = %v", batch, err)
	}
}
