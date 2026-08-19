// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package clickhouse

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	ch "github.com/ClickHouse/clickhouse-go/v2"
	"github.com/ClickHouse/clickhouse-go/v2/lib/driver"
	bursar "github.com/Zonastery/bursar/golang/v2"
)

type analyticsConnectionStub struct {
	ch.Conn
	rows func(string) *rowsStub
}

type analyticsErrorConnectionStub struct {
	ch.Conn
	rows func(string) (driver.Rows, error)
}

func (s *analyticsErrorConnectionStub) Query(_ context.Context, query string, _ ...any) (driver.Rows, error) {
	return s.rows(query)
}

func (s *analyticsConnectionStub) Query(_ context.Context, query string, _ ...any) (driver.Rows, error) {
	return s.rows(query), nil
}

func TestClickHouseAnalyticsAndLifecycleBranches(t *testing.T) {
	t.Parallel()
	start := time.Date(2026, 8, 13, 0, 0, 0, 0, time.UTC)
	end := start.Add(48 * time.Hour)
	connection := &connectionStub{}
	store, err := NewUsageStore(Options{Connection: connection, TenantID: tenantID})
	if err != nil {
		t.Fatal(err)
	}
	if err := store.Start(context.Background()); err != nil {
		t.Fatalf("Start() error = %v", err)
	}
	if err := store.Flush(context.Background()); err != nil {
		t.Fatalf("Flush() error = %v", err)
	}
	canceled, cancel := context.WithCancel(context.Background())
	cancel()
	if err := store.Flush(canceled); !errors.Is(err, context.Canceled) {
		t.Fatalf("canceled Flush() error = %v", err)
	}

	connection.queryRows = &rowsStub{rows: [][]any{{subjectID, "12.500000", "2"}}}
	users, err := store.SpendByUser(context.Background(), start, end)
	if err != nil || len(users) != 1 || users[0].UserID != subjectID || users[0].EntryCount != 2 {
		t.Fatalf("SpendByUser() = %#v, %v", users, err)
	}
	connection.queryRows = &rowsStub{rows: [][]any{{"gpt-test", "12.500000", "2"}}}
	models, err := store.SpendByModel(context.Background(), start, end)
	if err != nil || len(models) != 1 || models[0].Model != "gpt-test" {
		t.Fatalf("SpendByModel() = %#v, %v", models, err)
	}
	connection.queryRows = &rowsStub{rows: [][]any{{subjectID, "12.500000", "2"}}}
	top, err := store.TopUsers(context.Background(), 1, start, end)
	if err != nil || len(top) != 1 || top[0].UserID != subjectID {
		t.Fatalf("TopUsers() = %#v, %v", top, err)
	}
	for _, limit := range []int{0, 10001} {
		if _, err := store.TopUsers(context.Background(), limit, start, end); err == nil {
			t.Fatalf("TopUsers(%d) error = nil", limit)
		}
	}

	connection.queryRows = &rowsStub{rows: [][]any{{"2026-08-13", "12.500000", "2"}}}
	daily, err := store.DailySpend(context.Background(), start, end)
	if err != nil || len(daily) != 1 || daily[0].Date.Location() != time.UTC || daily[0].EntryCount != 2 {
		t.Fatalf("DailySpend() = %#v, %v", daily, err)
	}

	analytics := &analyticsConnectionStub{rows: func(query string) *rowsStub {
		switch {
		case strings.Contains(query, "uniqExact"):
			return &rowsStub{rows: [][]any{{"25.000000", "2"}}}
		case strings.Contains(query, "coalesce(model"):
			return &rowsStub{rows: [][]any{{"gpt-test", "20.000000", "1"}}}
		default:
			return &rowsStub{rows: [][]any{{subjectID, "20.000000", "1"}}}
		}
	}}
	analyticsStore, err := NewUsageStore(Options{Connection: analytics, TenantID: tenantID})
	if err != nil {
		t.Fatal(err)
	}
	stats, err := analyticsStore.AggregateStats(context.Background(), start, end)
	if err != nil || !stats.TotalCreditsConsumed.Equal(bursar.MustAmount("25")) || stats.ActiveUsers != 2 || stats.TopModel != "gpt-test" || stats.TopUser != subjectID || !stats.AverageDailySpend.Equal(bursar.MustAmount("12.5")) {
		t.Fatalf("AggregateStats() = %#v, %v", stats, err)
	}
	if _, err := store.SpendByUser(context.Background(), end, start); err == nil {
		t.Fatal("invalid analytics range error = nil")
	}
}

func TestClickHouseValidationAndProjectionErrors(t *testing.T) {
	t.Parallel()
	if _, err := NewUsageStore(Options{}); err == nil {
		t.Fatal("nil connection error = nil")
	}
	if _, err := NewUsageStore(Options{Connection: &connectionStub{}, TenantID: "bad"}); err == nil {
		t.Fatal("invalid tenant error = nil")
	}
	connection := &connectionStub{batch: &batchStub{appendErr: errors.New("append failed")}}
	store, err := NewUsageStore(Options{Connection: connection, TenantID: tenantID})
	if err != nil {
		t.Fatal(err)
	}
	if err := store.WriteUsage(context.Background(), validUsageExport(), "1"); err == nil || !strings.Contains(err.Error(), "append") {
		t.Fatalf("append failure = %v", err)
	}
	connection.batch = &batchStub{sendErr: errors.New("send failed")}
	if err := store.WriteUsage(context.Background(), validUsageExport(), "1"); err == nil || !strings.Contains(err.Error(), "send") {
		t.Fatalf("send failure = %v", err)
	}
	connection.queryRows = &rowsStub{err: errors.New("row failure")}
	if _, err := store.SpendByUser(context.Background(), time.Now().Add(-time.Hour), time.Now()); err == nil || !strings.Contains(err.Error(), "row failure") {
		t.Fatalf("row failure = %v", err)
	}

	for _, value := range []string{"", "-1", "+1", "18446744073709551616"} {
		if _, err := parseCount(value); err == nil {
			t.Fatalf("parseCount(%q) error = nil", value)
		}
	}
	if got, err := parseCount("42"); err != nil || got != 42 {
		t.Fatalf("parseCount(42) = %d, %v", got, err)
	}
	for _, value := range []string{"", "2026-08-13", "2026-99-13 01:02:03", "2026-08-13 01:02:03.1234567"} {
		if _, err := parseClickHouseTimestamp(value); err == nil {
			t.Fatalf("parseClickHouseTimestamp(%q) error = nil", value)
		}
	}
	if got, err := parseClickHouseTimestamp("2026-08-13 01:02:03.123456"); err != nil || got.Location() != time.UTC {
		t.Fatalf("parseClickHouseTimestamp() = %v, %v", got, err)
	}
	for _, value := range []string{"-1", "+1", "1.0", "18446744073709551616"} {
		if _, err := parseOutboxEventID(value); err == nil {
			t.Fatalf("parseOutboxEventID(%q) error = nil", value)
		}
	}
	if got, err := parseOutboxEventID("42"); err != nil || got != 42 {
		t.Fatalf("parseOutboxEventID(42) = %d, %v", got, err)
	}
	if _, _, err := parseSpendValues("invalid", "1"); err == nil {
		t.Fatal("invalid spend amount error = nil")
	}
	if _, _, err := parseSpendValues("1", "invalid"); err == nil {
		t.Fatal("invalid spend count error = nil")
	}

	for _, input := range []bursar.UsageChargeExport{
		func() bursar.UsageChargeExport { value := validUsageExport(); value.Operation = " "; return value }(),
		func() bursar.UsageChargeExport { value := validUsageExport(); value.IdempotencyKey = " "; return value }(),
		func() bursar.UsageChargeExport { value := validUsageExport(); value.RequestDigest = " "; return value }(),
		func() bursar.UsageChargeExport {
			value := validUsageExport()
			value.BillingDisposition = "invalid"
			return value
		}(),
		func() bursar.UsageChargeExport {
			value := validUsageExport()
			value.EventAt = time.Time{}
			return value
		}(),
		func() bursar.UsageChargeExport {
			value := validUsageExport()
			value.CatalogRevisionID = stringPtr("bad")
			return value
		}(),
	} {
		if err := store.WriteUsage(context.Background(), input, "1"); err == nil {
			t.Fatalf("invalid projection %#v returned nil error", input)
		}
	}
	if err := store.WriteUsageBatch(context.Background(), nil); err != nil {
		t.Fatalf("empty WriteUsageBatch() error = %v", err)
	}
	canceled, cancel := context.WithCancel(context.Background())
	cancel()
	if err := store.WriteUsageBatch(canceled, []bursar.UsageExportEntry{{Event: validUsageExport(), OutboxEventID: "1"}}); !errors.Is(err, context.Canceled) {
		t.Fatalf("canceled WriteUsageBatch() error = %v", err)
	}
	optional := validUsageExport()
	optional.PlanID = stringPtr("55555555-5555-4555-8555-555555555555")
	optional.LedgerEntryID = stringPtr("66666666-6666-4666-8666-666666666666")
	optional.CorrectionOfChargeID = stringPtr("77777777-7777-4777-8777-777777777777")
	optional.Measures = nil
	optional.Dimensions = nil
	optional.Metadata = nil
	optional.PricingSnapshot = nil
	if _, err := store.projectUsage(optional, "1"); err != nil {
		t.Fatalf("optional projectUsage() error = %v", err)
	}
}

func TestClickHouseListAndSchemaFailureBranches(t *testing.T) {
	t.Parallel()
	connection := &connectionStub{}
	store, err := NewUsageStore(Options{Connection: connection, TenantID: tenantID})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.ListUsageCharges(context.Background(), "bad", bursar.ListUsageChargesOptions{}); err == nil {
		t.Fatal("invalid subject error = nil")
	}
	for _, options := range []bursar.ListUsageChargesOptions{{Limit: -1}, {Limit: 201}} {
		if _, err := store.ListUsageCharges(context.Background(), subjectID, options); err == nil {
			t.Fatalf("invalid options %+v error = nil", options)
		}
	}
	zero := time.Time{}
	if _, err := store.ListUsageCharges(context.Background(), subjectID, bursar.ListUsageChargesOptions{From: &zero}); err == nil {
		t.Fatal("zero from error = nil")
	}
	if _, err := store.ListUsageCharges(context.Background(), subjectID, bursar.ListUsageChargesOptions{To: &zero}); err == nil {
		t.Fatal("zero to error = nil")
	}
	if _, err := store.ListUsageCharges(context.Background(), subjectID, bursar.ListUsageChargesOptions{Cursor: &bursar.UsageChargeCursor{UsageID: "bad", EventAt: time.Now()}}); err == nil {
		t.Fatal("bad cursor ID error = nil")
	}
	if _, err := store.ListUsageCharges(context.Background(), subjectID, bursar.ListUsageChargesOptions{Cursor: &bursar.UsageChargeCursor{UsageID: chargeID}}); err == nil {
		t.Fatal("zero cursor time error = nil")
	}

	connection.queryRows = &rowsStub{}
	if err := store.CheckSchemaCompatibility(context.Background()); err == nil || !strings.Contains(err.Error(), "does not exist") {
		t.Fatalf("missing schema error = %v", err)
	}
	connection.queryRows = &rowsStub{err: errors.New("schema read failed")}
	if err := store.CheckSchemaCompatibility(context.Background()); err == nil || !strings.Contains(err.Error(), "schema read failed") {
		t.Fatalf("schema rows error = %v", err)
	}
}

func TestClickHouseQueryAndReadErrorBranches(t *testing.T) {
	t.Parallel()
	start := time.Date(2026, 8, 13, 0, 0, 0, 0, time.UTC)
	end := start.Add(time.Hour)
	connection := &connectionStub{pingErr: errors.New("ping failed")}
	store, err := NewUsageStore(Options{Connection: connection, TenantID: tenantID, CreateTable: true})
	if err != nil {
		t.Fatal(err)
	}
	if err := store.InitializeSchema(context.Background()); err != nil {
		t.Fatalf("InitializeSchema() error = %v", err)
	}
	if err := store.Health(context.Background()); err == nil || !strings.Contains(err.Error(), "ping failed") {
		t.Fatalf("Health() error = %v", err)
	}
	if err := store.Close(context.Background()); err != nil {
		t.Fatal(err)
	}
	if err := store.Close(context.Background()); err != nil {
		t.Fatal(err)
	}
	if err := store.Health(context.Background()); err == nil {
		t.Fatal("Health() after Close error = nil")
	}
	var nilStore *ClickHouseUsageStore
	if err := nilStore.Initialize(context.Background()); err == nil || err.Error() != "ClickHouse usage store is not initialized" {
		t.Fatalf("nil Initialize() error = %v", err)
	}
	if err := nilStore.InitializeSchema(context.Background()); err == nil {
		t.Fatal("nil InitializeSchema() error = nil")
	}
	if err := nilStore.Health(context.Background()); err == nil {
		t.Fatal("nil Health() error = nil")
	}
	if err := nilStore.Flush(context.Background()); err != nil {
		t.Fatalf("nil Flush() error = %v", err)
	}
	if err := nilStore.Start(context.Background()); err == nil {
		t.Fatal("nil Start() error = nil")
	}
	if err := store.Close(func() context.Context { ctx, cancel := context.WithCancel(context.Background()); cancel(); return ctx }()); err == nil {
		t.Fatal("canceled Close() error = nil")
	}
	queryFailure, err := NewUsageStore(Options{Connection: &connectionStub{queryErr: errors.New("query failed")}, TenantID: tenantID})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := queryFailure.SpendByUser(context.Background(), start, end); err == nil || !strings.Contains(err.Error(), "query failed") {
		t.Fatalf("query failure = %v", err)
	}
	createFailure, err := NewUsageStore(Options{Connection: &connectionStub{execErr: errors.New("create failed")}, TenantID: tenantID, CreateTable: true})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := createFailure.SpendByUser(context.Background(), start, end); err == nil || !strings.Contains(err.Error(), "create failed") {
		t.Fatalf("query create failure = %v", err)
	}

	for _, rows := range []*rowsStub{
		{rows: [][]any{{"not-a-date", "1", "1"}}},
		{rows: [][]any{{"2026-08-13", "bad", "1"}}},
		{rows: [][]any{{"2026-08-13", "1", "bad"}}},
		{err: errors.New("daily rows failed")},
	} {
		badStore, err := NewUsageStore(Options{Connection: &connectionStub{queryRows: rows}, TenantID: tenantID})
		if err != nil {
			t.Fatal(err)
		}
		if _, err := badStore.DailySpend(context.Background(), start, end); err == nil {
			t.Fatalf("DailySpend(%#v) error = nil", rows)
		}
	}

	for _, rows := range []*rowsStub{
		{rows: [][]any{{subjectID, "1"}}},
		{rows: [][]any{{subjectID, "bad", "1"}}},
		{rows: [][]any{{subjectID, "1", "bad"}}},
		{err: errors.New("spend rows failed")},
	} {
		badStore, err := NewUsageStore(Options{Connection: &connectionStub{queryRows: rows}, TenantID: tenantID})
		if err != nil {
			t.Fatal(err)
		}
		if _, err := badStore.SpendByUser(context.Background(), start, end); err == nil {
			t.Fatalf("SpendByUser(%#v) error = nil", rows)
		}
	}

	badQuery := &analyticsErrorConnectionStub{rows: func(query string) (driver.Rows, error) {
		if strings.Contains(query, "uniqExact") {
			return nil, errors.New("aggregate query failed")
		}
		return &rowsStub{rows: [][]any{{subjectID, "1", "1"}}}, nil
	}}
	badAggregate, err := NewUsageStore(Options{Connection: badQuery, TenantID: tenantID})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := badAggregate.AggregateStats(context.Background(), start, end); err == nil || !strings.Contains(err.Error(), "aggregate query failed") {
		t.Fatalf("AggregateStats() error = %v", err)
	}
}

func TestClickHouseSchemaAndPureValidationBranches(t *testing.T) {
	t.Parallel()
	connection := &connectionStub{}
	store, err := NewUsageStore(Options{Connection: connection, TenantID: tenantID, Table: "analytics.events"})
	if err != nil {
		t.Fatal(err)
	}
	connection.queryRows = &rowsStub{rows: [][]any{{"tenant_id", "UUID", "MergeTree", "MergeTree", "tenant_id"}}}
	if err := store.CheckSchemaCompatibility(context.Background()); err == nil || !strings.Contains(err.Error(), "missing outbox_event_id") || !strings.Contains(err.Error(), "engine MergeTree") {
		t.Fatalf("incomplete schema error = %v", err)
	}
	connection.queryRows = &rowsStub{rows: [][]any{{"tenant_id", "UUID", "ReplacingMergeTree", "ReplacingMergeTree(other)", "tenant_id"}}}
	if err := store.CheckSchemaCompatibility(context.Background()); err == nil || !strings.Contains(err.Error(), "version column") || !strings.Contains(err.Error(), "event_at") {
		t.Fatalf("schema key mismatch error = %v", err)
	}

	for _, value := range []string{"100000000000000", "99999999999999.9999999"} {
		if err := validateDecimal20Scale6(bursar.MustAmount(value), "amount"); err == nil {
			t.Fatalf("validateDecimal20Scale6(%q) error = nil", value)
		}
	}
	if _, err := marshalObject(map[string]any{"bad": func() {}}); err == nil {
		t.Fatal("marshalObject(function) error = nil")
	}
	database, table := splitTable("events")
	if database != "" || table != "events" {
		t.Fatalf("splitTable(single) = %q/%q", database, table)
	}
	database, table = splitTable("analytics.events")
	if database != "analytics" || table != "events" {
		t.Fatalf("splitTable(qualified) = %q/%q", database, table)
	}
}

func TestClickHouseListDecodeAndDatabaseErrorBranches(t *testing.T) {
	t.Parallel()
	base := []any{chargeID, accountID, "chat", "1", "1", "0", "0", "billable", nil, nil, nil, "2026-08-13 01:02:03", "key", `{}`, "2026-08-13 01:02:04"}
	variants := []struct {
		name   string
		mutate func([]any)
	}{
		{name: "usage ID", mutate: func(row []any) { row[0] = "bad" }},
		{name: "account ID", mutate: func(row []any) { row[1] = "bad" }},
		{name: "requested", mutate: func(row []any) { row[3] = "bad" }},
		{name: "charged", mutate: func(row []any) { row[4] = "bad" }},
		{name: "allowance requested", mutate: func(row []any) { row[5] = "bad" }},
		{name: "allowance covered", mutate: func(row []any) { row[6] = "bad" }},
		{name: "disposition", mutate: func(row []any) { row[7] = "bad" }},
		{name: "event timestamp", mutate: func(row []any) { row[11] = "bad" }},
		{name: "created timestamp", mutate: func(row []any) { row[14] = "bad" }},
		{name: "metadata", mutate: func(row []any) { row[13] = "bad" }},
	}
	for _, variant := range variants {
		t.Run(variant.name, func(t *testing.T) {
			row := append([]any(nil), base...)
			variant.mutate(row)
			store, err := NewUsageStore(Options{Connection: &connectionStub{queryRows: &rowsStub{rows: [][]any{row}}}, TenantID: tenantID})
			if err != nil {
				t.Fatal(err)
			}
			if _, err := store.ListUsageCharges(context.Background(), subjectID, bursar.ListUsageChargesOptions{}); err == nil {
				t.Fatal("invalid projected row returned nil error")
			}
		})
	}
	rowErrorStore, err := NewUsageStore(Options{Connection: &connectionStub{queryRows: &rowsStub{rows: [][]any{base}, err: errors.New("usage rows failed")}}, TenantID: tenantID})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := rowErrorStore.ListUsageCharges(context.Background(), subjectID, bursar.ListUsageChargesOptions{}); err == nil || !strings.Contains(err.Error(), "usage rows failed") {
		t.Fatalf("ListUsageCharges(rows error) = %v", err)
	}

	prepareStore, err := NewUsageStore(Options{Connection: &connectionStub{prepareErr: errors.New("prepare failed")}, TenantID: tenantID})
	if err != nil {
		t.Fatal(err)
	}
	if err := prepareStore.WriteUsage(context.Background(), validUsageExport(), "1"); err == nil || !strings.Contains(err.Error(), "prepare") {
		t.Fatalf("prepare error = %v", err)
	}
	initStore, err := NewUsageStore(Options{Connection: &connectionStub{execErr: errors.New("DDL failed")}, TenantID: tenantID, CreateTable: true})
	if err != nil {
		t.Fatal(err)
	}
	if err := initStore.InitializeSchema(context.Background()); err == nil || !strings.Contains(err.Error(), "DDL failed") {
		t.Fatalf("initialize schema error = %v", err)
	}
}

func stringPtr(value string) *string { return &value }
