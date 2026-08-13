// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package clickhouse

import (
	"context"
	"errors"
	"strings"
	"sync"
	"testing"
	"time"

	ch "github.com/ClickHouse/clickhouse-go/v2"
	"github.com/ClickHouse/clickhouse-go/v2/lib/driver"
	bursar "github.com/Zonastery/bursar/golang/v2"
)

const (
	tenantID  = "11111111-1111-4111-8111-111111111111"
	chargeID  = "22222222-2222-4222-8222-222222222222"
	accountID = "33333333-3333-4333-8333-333333333333"
	subjectID = "44444444-4444-4444-8444-444444444444"
)

type connectionStub struct {
	ch.Conn
	mu           sync.Mutex
	prepareQuery string
	batch        *batchStub
	prepareErr   error
	execQueries  []string
	execErr      error
	queryRows    driver.Rows
	queryErr     error
	queryText    string
	pingErr      error
	closeCount   int
}

func (s *connectionStub) PrepareBatch(_ context.Context, query string, _ ...driver.PrepareBatchOption) (driver.Batch, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.prepareQuery = query
	if s.prepareErr != nil {
		return nil, s.prepareErr
	}
	if s.batch == nil {
		s.batch = &batchStub{}
	}
	return s.batch, nil
}

func (s *connectionStub) Exec(_ context.Context, query string, _ ...any) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.execQueries = append(s.execQueries, query)
	return s.execErr
}

func (s *connectionStub) Query(_ context.Context, query string, _ ...any) (driver.Rows, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.queryText = query
	return s.queryRows, s.queryErr
}

func (s *connectionStub) Ping(context.Context) error { return s.pingErr }

func (s *connectionStub) Close() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.closeCount++
	return nil
}

type batchStub struct {
	driver.Batch
	mu         sync.Mutex
	rows       [][]any
	appendErr  error
	sendErr    error
	sent       bool
	abortCount int
	closeCount int
}

func (s *batchStub) Append(values ...any) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.appendErr != nil {
		return s.appendErr
	}
	s.rows = append(s.rows, append([]any(nil), values...))
	return nil
}

func (s *batchStub) Send() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.sent = s.sendErr == nil
	return s.sendErr
}

func (s *batchStub) Abort() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.abortCount++
	return nil
}

func (s *batchStub) Close() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.closeCount++
	return nil
}

type rowsStub struct {
	driver.Rows
	mu         sync.Mutex
	rows       [][]any
	index      int
	err        error
	closeCount int
}

func (s *rowsStub) Next() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.index < len(s.rows)
}

func (s *rowsStub) Scan(destinations ...any) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.index >= len(s.rows) {
		return errors.New("scan past end")
	}
	row := s.rows[s.index]
	s.index++
	if len(row) != len(destinations) {
		return errors.New("destination count mismatch")
	}
	for index, value := range row {
		if err := assignScan(destinations[index], value); err != nil {
			return err
		}
	}
	return nil
}

func (s *rowsStub) Close() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.closeCount++
	return nil
}

func (s *rowsStub) Err() error { return s.err }

func assignScan(destination, value any) error {
	switch target := destination.(type) {
	case *string:
		text, ok := value.(string)
		if !ok {
			return errors.New("value is not a string")
		}
		*target = text
	case **string:
		if value == nil {
			*target = nil
			return nil
		}
		text, ok := value.(string)
		if !ok {
			return errors.New("value is not a nullable string")
		}
		*target = &text
	default:
		return errors.New("unsupported scan destination")
	}
	return nil
}

func TestWriteUsageBatchProjectsExactCanonicalValues(t *testing.T) {
	t.Parallel()
	batch := &batchStub{}
	connection := &connectionStub{batch: batch}
	store, err := NewUsageStore(Options{Connection: connection, TenantID: tenantID, Table: "analytics.bursar_usage_events"})
	if err != nil {
		t.Fatalf("NewUsageStore() error = %v", err)
	}
	feature := "chat"
	model := "gpt-test"
	region := "us-east"
	catalogID := "55555555-5555-4555-8555-555555555555"
	rateCard := "standard"
	eventAt := time.Date(2026, 8, 13, 1, 2, 3, 456789000, time.FixedZone("IST", 5*60*60+30*60))
	event := bursar.UsageChargeExport{
		TenantID: tenantID, ChargeID: chargeID, AccountID: accountID, SubjectID: subjectID,
		Operation: "chat.completion", Feature: &feature, Model: &model, Region: &region,
		Measures: map[string]any{"tokens": 12}, Dimensions: map[string]any{"tier": "pro"},
		Metadata: map[string]any{"request": "req-1"}, Requested: bursar.MustAmount("1.250000"),
		Charged: bursar.MustAmount("1.000000"), AllowanceRequested: bursar.MustAmount("0.250000"),
		AllowanceCovered: bursar.MustAmount("0.250000"), BillingDisposition: bursar.BillingDispositionBillable,
		CatalogRevisionID: &catalogID, RateCardKey: &rateCard, PricingSnapshot: map[string]any{"expression": "tokens / 12"},
		IdempotencyKey: "usage-1", RequestDigest: "sha256:digest", EventAt: eventAt, CreatedAt: eventAt.Add(time.Second),
	}

	if err := store.WriteUsageBatch(context.Background(), []bursar.UsageExportEntry{{Event: event, OutboxEventID: "18446744073709551615"}}); err != nil {
		t.Fatalf("WriteUsageBatch() error = %v", err)
	}
	expected := "INSERT INTO \"analytics\".\"bursar_usage_events\" (" + strings.Join(insertColumns, ", ") + ")"
	if connection.prepareQuery != expected {
		t.Fatalf("PrepareBatch() query = %q", connection.prepareQuery)
	}
	if !batch.sent || len(batch.rows) != 1 || len(batch.rows[0]) != len(insertColumns) {
		t.Fatalf("batch sent=%t rows=%d columns=%d", batch.sent, len(batch.rows), len(batch.rows[0]))
	}
	row := batch.rows[0]
	if row[1] != uint64(^uint64(0)) || row[9] != `{"tokens":12}` || row[12].(bursar.Amount).StringFixed(6) != "1.250000" {
		t.Fatalf("projected row values = %#v", row)
	}
	if !row[25].(time.Time).Equal(eventAt.UTC()) {
		t.Fatalf("projected event time = %v", row[25])
	}
}

func TestWriteUsageRejectsInvalidProjectionBeforeDatabase(t *testing.T) {
	t.Parallel()
	connection := &connectionStub{batch: &batchStub{}}
	store, err := NewUsageStore(Options{Connection: connection, TenantID: tenantID})
	if err != nil {
		t.Fatalf("NewUsageStore() error = %v", err)
	}
	event := validUsageExport()
	event.TenantID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
	if err := store.WriteUsage(context.Background(), event, "1"); err == nil {
		t.Fatal("WriteUsage(cross tenant) error = nil")
	}
	event = validUsageExport()
	if err := store.WriteUsage(context.Background(), event, "18446744073709551616"); err == nil {
		t.Fatal("WriteUsage(UInt64 overflow) error = nil")
	}
	event = validUsageExport()
	event.Charged = bursar.MustAmount("0.0000001")
	if err := store.WriteUsage(context.Background(), event, "1"); err == nil {
		t.Fatal("WriteUsage(scale overflow) error = nil")
	}
	if connection.prepareQuery != "" {
		t.Fatalf("PrepareBatch() called for invalid projection: %q", connection.prepareQuery)
	}
}

func TestInitializeAndSchemaCompatibility(t *testing.T) {
	t.Parallel()
	retention := 30
	connection := &connectionStub{}
	store, err := NewUsageStore(Options{Connection: connection, TenantID: tenantID, CreateTable: true, RetentionDays: &retention})
	if err != nil {
		t.Fatalf("NewUsageStore() error = %v", err)
	}
	if err := store.Initialize(context.Background()); err != nil {
		t.Fatalf("Initialize() error = %v", err)
	}
	if err := store.Initialize(context.Background()); err != nil {
		t.Fatalf("Initialize() second error = %v", err)
	}
	if len(connection.execQueries) != 1 || !strings.Contains(connection.execQueries[0], "ReplacingMergeTree(outbox_event_id)") || !strings.Contains(connection.execQueries[0], "toIntervalDay(30)") {
		t.Fatalf("Initialize() queries = %#v", connection.execQueries)
	}

	schemaRows := make([][]any, 0, len(schemaExpectations))
	for _, expectation := range schemaExpectations {
		schemaRows = append(schemaRows, []any{expectation.name, expectation.types[0], "ReplacingMergeTree", "ReplacingMergeTree(outbox_event_id)", "tenant_id, event_at, charge_id"})
	}
	connection.queryRows = &rowsStub{rows: schemaRows}
	if err := store.CheckSchemaCompatibility(context.Background()); err != nil {
		t.Fatalf("CheckSchemaCompatibility() error = %v", err)
	}

	badRows := make([][]any, len(schemaRows))
	for index, row := range schemaRows {
		badRows[index] = append([]any(nil), row...)
	}
	badRows[0][1] = "String"
	connection.queryRows = &rowsStub{rows: badRows}
	if err := store.CheckSchemaCompatibility(context.Background()); err == nil || !strings.Contains(err.Error(), "tenant_id is String") {
		t.Fatalf("CheckSchemaCompatibility(invalid) error = %v", err)
	}
}

func TestListUsageChargesParsesUTCAndStableCursor(t *testing.T) {
	t.Parallel()
	rows := &rowsStub{rows: [][]any{
		{chargeID, accountID, "chat", "1.250000", "1.000000", "0.250000", "0.250000", "billable", "chat", "gpt-test", nil, "2026-08-13 01:02:03.456789", "usage-1", `{"request":"req-1"}`, "2026-08-13 01:02:04.000001"},
		{"55555555-5555-4555-8555-555555555555", accountID, "chat", "2.000000", "2.000000", "0.000000", "0.000000", "record_only", nil, nil, nil, "2026-08-13 00:02:03.000001", "usage-2", `{}`, "2026-08-13 00:02:04.000001"},
	}}
	connection := &connectionStub{queryRows: rows}
	store, err := NewUsageStore(Options{Connection: connection, TenantID: tenantID})
	if err != nil {
		t.Fatalf("NewUsageStore() error = %v", err)
	}
	page, err := store.ListUsageCharges(context.Background(), subjectID, bursar.ListUsageChargesOptions{Limit: 1})
	if err != nil {
		t.Fatalf("ListUsageCharges() error = %v", err)
	}
	if len(page.Items) != 1 || page.Items[0].EventAt.Location() != time.UTC || page.Items[0].EventAt.Nanosecond() != 456789000 {
		t.Fatalf("ListUsageCharges() items = %#v", page.Items)
	}
	if page.NextCursor == nil || page.NextCursor.UsageID != chargeID || !page.NextCursor.EventAt.Equal(page.Items[0].EventAt) {
		t.Fatalf("ListUsageCharges() cursor = %#v", page.NextCursor)
	}
	if rows.closeCount != 1 {
		t.Fatalf("ListUsageCharges() rows close count = %d", rows.closeCount)
	}
}

func TestConstructorHealthAndOwnership(t *testing.T) {
	t.Parallel()
	if _, err := NewUsageStore(Options{Connection: &connectionStub{}, TenantID: tenantID, Table: `usage; DROP TABLE users`}); err == nil {
		t.Fatal("NewUsageStore(invalid table) error = nil")
	}
	invalidRetention := 0
	if _, err := NewUsageStore(Options{Connection: &connectionStub{}, TenantID: tenantID, RetentionDays: &invalidRetention}); err == nil {
		t.Fatal("NewUsageStore(invalid retention) error = nil")
	}
	connection := &connectionStub{}
	store, err := NewUsageStore(Options{Connection: connection, TenantID: tenantID, OwnsConnection: true})
	if err != nil {
		t.Fatalf("NewUsageStore() error = %v", err)
	}
	if err := store.Health(context.Background()); err != nil {
		t.Fatalf("Health() error = %v", err)
	}
	if err := store.Close(context.Background()); err != nil {
		t.Fatalf("Close() error = %v", err)
	}
	if err := store.Close(context.Background()); err != nil {
		t.Fatalf("Close() second error = %v", err)
	}
	if connection.closeCount != 1 {
		t.Fatalf("connection close count = %d", connection.closeCount)
	}
	if err := store.Health(context.Background()); err == nil {
		t.Fatal("Health() after Close error = nil")
	}
}

func validUsageExport() bursar.UsageChargeExport {
	now := time.Date(2026, 8, 13, 1, 2, 3, 0, time.UTC)
	return bursar.UsageChargeExport{
		TenantID: tenantID, ChargeID: chargeID, AccountID: accountID, SubjectID: subjectID,
		Operation: "chat", Measures: map[string]any{}, Dimensions: map[string]any{}, Metadata: map[string]any{},
		Requested: bursar.MustAmount("1.000000"), Charged: bursar.MustAmount("1.000000"),
		AllowanceRequested: bursar.DecimalZero, AllowanceCovered: bursar.DecimalZero,
		BillingDisposition: bursar.BillingDispositionBillable, PricingSnapshot: map[string]any{},
		IdempotencyKey: "usage-1", RequestDigest: "digest", EventAt: now, CreatedAt: now,
	}
}
