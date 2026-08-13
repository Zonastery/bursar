// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

// Package clickhouse provides the optional high-volume usage projection and
// analytics store. PostgreSQL remains authoritative for balances, leases, the
// compact ledger, and the transactional outbox.
package clickhouse

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"sync"
	"time"

	ch "github.com/ClickHouse/clickhouse-go/v2"
	"github.com/ClickHouse/clickhouse-go/v2/lib/driver"
	bursar "github.com/Zonastery/bursar/golang/v2"
	"github.com/google/uuid"
	"github.com/shopspring/decimal"
	"golang.org/x/sync/errgroup"
)

const defaultTable = "bursar_usage_events"

var (
	tablePattern               = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?$`)
	unsignedIntegerPattern     = regexp.MustCompile(`^(?:0|[1-9][0-9]*)$`)
	clickHouseTimestampPattern = regexp.MustCompile(`^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d{1,6})?$`)
)

// Options configures a ClickHouseUsageStore around the official clickhouse-go
// v2 connection. The caller owns the connection unless OwnsConnection is true.
type Options struct {
	Connection     ch.Conn
	TenantID       string
	Table          string
	CreateTable    bool
	RetentionDays  *int
	OwnsConnection bool
}

// ClickHouseUsageStore is an idempotent ReplacingMergeTree projection plus the
// read-only usage analytics and receipt-history surface exposed by the other
// Bursar SDKs.
type ClickHouseUsageStore struct {
	connection     ch.Conn
	tenantID       uuid.UUID
	table          string
	quotedTable    string
	createTable    bool
	retentionDays  *int
	ownsConnection bool

	lifecycleMu  sync.RWMutex
	closed       bool
	initializeMu sync.Mutex
	initialized  bool
}

var (
	_ bursar.UsageEventSink             = (*ClickHouseUsageStore)(nil)
	_ bursar.BatchUsageEventSink        = (*ClickHouseUsageStore)(nil)
	_ bursar.UsageProjectionInitializer = (*ClickHouseUsageStore)(nil)
	_ bursar.UsageProjectionSchema      = (*ClickHouseUsageStore)(nil)
	_ bursar.RuntimeComponent           = (*ClickHouseUsageStore)(nil)
	_ bursar.RuntimeHealthChecker       = (*ClickHouseUsageStore)(nil)
)

// NewUsageStore validates local configuration without contacting ClickHouse.
func NewUsageStore(options Options) (*ClickHouseUsageStore, error) {
	if options.Connection == nil {
		return nil, errors.New("ClickHouse connection is required")
	}
	tenantID, err := uuid.Parse(strings.TrimSpace(options.TenantID))
	if err != nil {
		return nil, fmt.Errorf("ClickHouse tenant ID must be a UUID: %w", err)
	}
	table := options.Table
	if table == "" {
		table = defaultTable
	}
	if !tablePattern.MatchString(table) {
		return nil, errors.New("ClickHouse table must be an identifier or database.identifier")
	}
	var retentionDays *int
	if options.RetentionDays != nil {
		if *options.RetentionDays < 1 || *options.RetentionDays > 36_500 {
			return nil, errors.New("ClickHouse retention days must be between 1 and 36500")
		}
		value := *options.RetentionDays
		retentionDays = &value
	}
	return &ClickHouseUsageStore{
		connection: options.Connection, tenantID: tenantID, table: table,
		quotedTable: quoteTable(table), createTable: options.CreateTable,
		retentionDays: retentionDays, ownsConnection: options.OwnsConnection,
	}, nil
}

// Initialize creates the standalone projection only when CreateTable is true.
func (s *ClickHouseUsageStore) Initialize(ctx context.Context) error {
	if s == nil {
		return errors.New("ClickHouse usage store is not initialized")
	}
	release, err := s.enter(ctx)
	if err != nil {
		return err
	}
	defer release()
	if !s.createTable {
		return nil
	}
	return s.initializeSchema(ctx)
}

// InitializeSchema explicitly creates Bursar's standalone ClickHouse table.
// Failed attempts are not cached, allowing a later retry to recover.
func (s *ClickHouseUsageStore) InitializeSchema(ctx context.Context) error {
	if s == nil {
		return errors.New("ClickHouse usage store is not initialized")
	}
	release, err := s.enter(ctx)
	if err != nil {
		return err
	}
	defer release()
	return s.initializeSchema(ctx)
}

func (s *ClickHouseUsageStore) initializeSchema(ctx context.Context) error {
	s.initializeMu.Lock()
	defer s.initializeMu.Unlock()
	if s.initialized {
		return nil
	}
	ttl := ""
	if s.retentionDays != nil {
		ttl = fmt.Sprintf("\nTTL event_at + toIntervalDay(%d) DELETE", *s.retentionDays)
	}
	query := fmt.Sprintf(`CREATE TABLE IF NOT EXISTS %s (
  tenant_id UUID,
  outbox_event_id UInt64,
  charge_id UUID,
  account_id UUID,
  subject_id UUID,
  operation LowCardinality(String),
  feature Nullable(String),
  model Nullable(String),
  region Nullable(String),
  measures String,
  dimensions String,
  metadata String,
  requested Decimal(20, 6),
  charged Decimal(20, 6),
  allowance_requested Decimal(20, 6),
  allowance_covered Decimal(20, 6),
  billing_disposition LowCardinality(String) DEFAULT 'billable',
  catalog_revision_id Nullable(UUID),
  plan_id Nullable(UUID),
  rate_card_key Nullable(String),
  pricing_snapshot String,
  ledger_entry_id Nullable(UUID),
  correction_of_charge_id Nullable(UUID),
  idempotency_key String,
  request_digest String,
  event_at DateTime64(6, 'UTC'),
  created_at DateTime64(6, 'UTC'),
  ingested_at DateTime64(6, 'UTC') DEFAULT now64(6)
)
ENGINE = ReplacingMergeTree(outbox_event_id)
PARTITION BY toYYYYMM(event_at)
ORDER BY (tenant_id, event_at, charge_id)%s`, s.quotedTable, ttl)
	if err := s.connection.Exec(ctx, query); err != nil {
		return fmt.Errorf("create ClickHouse usage projection: %w", err)
	}
	s.initialized = true
	return nil
}

// CheckSchemaCompatibility validates an operator-managed table without
// creating or mutating it.
func (s *ClickHouseUsageStore) CheckSchemaCompatibility(ctx context.Context) error {
	if s == nil {
		return errors.New("ClickHouse usage store is not initialized")
	}
	release, err := s.enter(ctx)
	if err != nil {
		return err
	}
	defer release()
	database, tableName := splitTable(s.table)
	queryCtx := withParameters(ctx, map[string]string{"database": database, "table_name": tableName})
	rows, err := s.connection.Query(queryCtx, `SELECT
  c.name,
  c.type,
  t.engine,
  t.engine_full,
  t.sorting_key
FROM system.columns AS c
INNER JOIN system.tables AS t
  ON t.database = c.database AND t.name = c.table
WHERE c.database = if(empty({database:String}), currentDatabase(), {database:String})
  AND c.table = {table_name:String}
ORDER BY c.position`)
	if err != nil {
		return fmt.Errorf("inspect ClickHouse usage schema: %w", err)
	}
	defer rows.Close()

	actual := make(map[string]string)
	engine, engineFull, sortingKey := "", "", ""
	for rows.Next() {
		var name, columnType, rowEngine, rowEngineFull, rowSortingKey string
		if err := rows.Scan(&name, &columnType, &rowEngine, &rowEngineFull, &rowSortingKey); err != nil {
			return fmt.Errorf("scan ClickHouse usage schema: %w", err)
		}
		actual[name] = normalizeType(columnType)
		if engine == "" {
			engine, engineFull, sortingKey = rowEngine, rowEngineFull, rowSortingKey
		}
	}
	if err := rows.Err(); err != nil {
		return fmt.Errorf("read ClickHouse usage schema: %w", err)
	}
	if len(actual) == 0 {
		return fmt.Errorf("ClickHouse table %s does not exist", s.table)
	}

	mismatches := make([]string, 0)
	for _, expectation := range schemaExpectations {
		actualType, ok := actual[expectation.name]
		if !ok {
			mismatches = append(mismatches, "missing "+expectation.name)
			continue
		}
		accepted := make([]string, len(expectation.types))
		for index, value := range expectation.types {
			accepted[index] = normalizeType(value)
		}
		if !slices.Contains(accepted, actualType) {
			mismatches = append(mismatches, fmt.Sprintf("%s is %s, expected %s", expectation.name, actualType, strings.Join(accepted, " or ")))
		}
	}
	if !strings.HasSuffix(engine, "ReplacingMergeTree") {
		if engine == "" {
			engine = "unknown"
		}
		mismatches = append(mismatches, fmt.Sprintf("engine %s is not a ReplacingMergeTree", engine))
	} else if !containsSQLIdentifier(engineFull, "outbox_event_id") {
		mismatches = append(mismatches, "ReplacingMergeTree does not use outbox_event_id as its version column")
	}
	for _, column := range []string{"tenant_id", "event_at", "charge_id"} {
		if !containsSQLIdentifier(sortingKey, column) {
			mismatches = append(mismatches, "sorting key does not include "+column)
		}
	}
	if len(mismatches) > 0 {
		return fmt.Errorf("ClickHouse table %s is incompatible: %s", s.table, strings.Join(mismatches, "; "))
	}
	return nil
}

// WriteUsage projects one usage charge with its durable outbox identity.
func (s *ClickHouseUsageStore) WriteUsage(ctx context.Context, event bursar.UsageChargeExport, outboxEventID string) error {
	return s.WriteUsageBatch(ctx, []bursar.UsageExportEntry{{Event: event, OutboxEventID: outboxEventID}})
}

// WriteUsageBatch inserts one batch while retaining each outbox identity.
// ReplacingMergeTree(outbox_event_id) makes a replay after worker failure safe.
func (s *ClickHouseUsageStore) WriteUsageBatch(ctx context.Context, entries []bursar.UsageExportEntry) error {
	if s == nil {
		return errors.New("ClickHouse usage store is not initialized")
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	if len(entries) == 0 {
		return nil
	}
	release, err := s.enter(ctx)
	if err != nil {
		return err
	}
	defer release()

	projected := make([][]any, len(entries))
	for index, entry := range entries {
		row, err := s.projectUsage(entry.Event, entry.OutboxEventID)
		if err != nil {
			return fmt.Errorf("project usage entry %d: %w", index, err)
		}
		projected[index] = row
	}
	if s.createTable {
		if err := s.initializeSchema(ctx); err != nil {
			return err
		}
	}

	batch, err := s.connection.PrepareBatch(ctx, fmt.Sprintf("INSERT INTO %s (%s)", s.quotedTable, strings.Join(insertColumns, ", ")))
	if err != nil {
		return fmt.Errorf("prepare ClickHouse usage batch: %w", err)
	}
	defer batch.Close()
	for index, row := range projected {
		if err := batch.Append(row...); err != nil {
			_ = batch.Abort()
			return fmt.Errorf("append ClickHouse usage row %d: %w", index, err)
		}
	}
	if err := batch.Send(); err != nil {
		return fmt.Errorf("send ClickHouse usage batch: %w", err)
	}
	return nil
}

// SpendByUser returns billable spend grouped by subject.
func (s *ClickHouseUsageStore) SpendByUser(ctx context.Context, start, end time.Time) ([]bursar.SpendByUserRow, error) {
	rows, err := s.spendRows(ctx, "subject_id", start, end, 0)
	if err != nil {
		return nil, err
	}
	result := make([]bursar.SpendByUserRow, len(rows))
	for index, row := range rows {
		result[index] = bursar.SpendByUserRow{UserID: row.key, TotalSpend: row.totalSpend, EntryCount: row.entryCount}
	}
	return result, nil
}

// SpendByModel returns billable spend grouped by model, using "unknown" for
// events without a model dimension.
func (s *ClickHouseUsageStore) SpendByModel(ctx context.Context, start, end time.Time) ([]bursar.SpendByModelRow, error) {
	rows, err := s.spendRows(ctx, "coalesce(model, 'unknown')", start, end, 0)
	if err != nil {
		return nil, err
	}
	result := make([]bursar.SpendByModelRow, len(rows))
	for index, row := range rows {
		result[index] = bursar.SpendByModelRow{Model: row.key, TotalSpend: row.totalSpend, EntryCount: row.entryCount}
	}
	return result, nil
}

// TopUsers returns the highest-spending billable subjects.
func (s *ClickHouseUsageStore) TopUsers(ctx context.Context, limit int, start, end time.Time) ([]bursar.TopUserRow, error) {
	if limit < 1 || limit > 10_000 {
		return nil, errors.New("top users limit must be between 1 and 10000")
	}
	rows, err := s.spendRows(ctx, "subject_id", start, end, limit)
	if err != nil {
		return nil, err
	}
	result := make([]bursar.TopUserRow, len(rows))
	for index, row := range rows {
		result[index] = bursar.TopUserRow{UserID: row.key, TotalSpend: row.totalSpend}
	}
	return result, nil
}

// DailySpend returns UTC-day billable spend.
func (s *ClickHouseUsageStore) DailySpend(ctx context.Context, start, end time.Time) ([]bursar.DailySpendRow, error) {
	if s == nil {
		return nil, errors.New("ClickHouse usage store is not initialized")
	}
	if err := validateRange(start, end); err != nil {
		return nil, err
	}
	query := fmt.Sprintf(`SELECT
  formatDateTime(toStartOfDay(event_at), '%%F') AS key,
  toString(sum(charged)) AS total_spend,
  toString(count()) AS entry_count
FROM %s FINAL
WHERE tenant_id = {tenant_id:UUID}
  AND billing_disposition = 'billable'
  AND event_at >= parseDateTime64BestEffort({start:String})
  AND event_at < parseDateTime64BestEffort({end:String})
GROUP BY key
ORDER BY key`, s.quotedTable)
	rows, err := s.query(ctx, query, analyticsParameters(s.tenantID, start, end))
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := make([]bursar.DailySpendRow, 0)
	for rows.Next() {
		var day, amountText, countText string
		if err := rows.Scan(&day, &amountText, &countText); err != nil {
			return nil, fmt.Errorf("scan ClickHouse daily spend: %w", err)
		}
		date, err := time.ParseInLocation("2006-01-02", day, time.UTC)
		if err != nil {
			return nil, fmt.Errorf("parse ClickHouse spend date: %w", err)
		}
		amount, count, err := parseSpendValues(amountText, countText)
		if err != nil {
			return nil, err
		}
		result = append(result, bursar.DailySpendRow{Date: date, TotalSpend: amount, EntryCount: count})
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("read ClickHouse daily spend: %w", err)
	}
	return result, nil
}

// AggregateStats returns the standard usage analytics summary.
func (s *ClickHouseUsageStore) AggregateStats(ctx context.Context, start, end time.Time) (bursar.AggregateStats, error) {
	if s == nil {
		return bursar.AggregateStats{}, errors.New("ClickHouse usage store is not initialized")
	}
	if err := validateRange(start, end); err != nil {
		return bursar.AggregateStats{}, err
	}
	var total bursar.Amount
	var activeUsers int
	var models, users []spendRow
	group, groupCtx := errgroup.WithContext(ctx)
	group.Go(func() error {
		query := fmt.Sprintf(`SELECT
  toString(sum(charged)) AS total_spend,
  toString(uniqExact(subject_id)) AS active_users
FROM %s FINAL
WHERE tenant_id = {tenant_id:UUID}
  AND billing_disposition = 'billable'
  AND event_at >= parseDateTime64BestEffort({start:String})
  AND event_at < parseDateTime64BestEffort({end:String})`, s.quotedTable)
		rows, err := s.query(groupCtx, query, analyticsParameters(s.tenantID, start, end))
		if err != nil {
			return err
		}
		defer rows.Close()
		if rows.Next() {
			var totalText, activeText string
			if err := rows.Scan(&totalText, &activeText); err != nil {
				return fmt.Errorf("scan ClickHouse aggregate totals: %w", err)
			}
			total, err = parseAmount(totalText)
			if err != nil {
				return err
			}
			activeUsers, err = parseCount(activeText)
			if err != nil {
				return err
			}
		}
		return rows.Err()
	})
	group.Go(func() error {
		var err error
		models, err = s.spendRows(groupCtx, "coalesce(model, 'unknown')", start, end, 1)
		return err
	})
	group.Go(func() error {
		var err error
		users, err = s.spendRows(groupCtx, "subject_id", start, end, 1)
		return err
	})
	if err := group.Wait(); err != nil {
		return bursar.AggregateStats{}, err
	}
	days := max(int(math.Ceil(end.Sub(start).Hours()/24)), 1)
	result := bursar.AggregateStats{
		TotalCreditsConsumed: total,
		ActiveUsers:          activeUsers,
		AverageDailySpend:    total.Div(decimal.NewFromInt(int64(days))),
	}
	if len(models) > 0 {
		result.TopModel = models[0].key
	}
	if len(users) > 0 {
		result.TopUser = users[0].key
	}
	return result, nil
}

// ListUsageCharges pages projected receipts by the same stable
// (event_at, charge_id) cursor used by PostgreSQL.
func (s *ClickHouseUsageStore) ListUsageCharges(ctx context.Context, userID string, options bursar.ListUsageChargesOptions) (bursar.UsageChargePage, error) {
	if s == nil {
		return bursar.UsageChargePage{}, errors.New("ClickHouse usage store is not initialized")
	}
	subjectID, err := uuid.Parse(strings.TrimSpace(userID))
	if err != nil {
		return bursar.UsageChargePage{}, fmt.Errorf("usage subject ID must be a UUID: %w", err)
	}
	limit := options.Limit
	if limit == 0 {
		limit = 50
	}
	if limit < 1 || limit > 200 {
		return bursar.UsageChargePage{}, errors.New("usage charge limit must be between 1 and 200")
	}
	predicates := []string{"tenant_id = {tenant_id:UUID}", "subject_id = {subject_id:UUID}"}
	parameters := map[string]string{"tenant_id": s.tenantID.String(), "subject_id": subjectID.String()}
	if options.From != nil {
		if options.From.IsZero() {
			return bursar.UsageChargePage{}, errors.New("usage charge from time must not be zero")
		}
		predicates = append(predicates, "event_at >= parseDateTime64BestEffort({from_date:String})")
		parameters["from_date"] = options.From.UTC().Format(time.RFC3339Nano)
	}
	if options.To != nil {
		if options.To.IsZero() {
			return bursar.UsageChargePage{}, errors.New("usage charge to time must not be zero")
		}
		predicates = append(predicates, "event_at < parseDateTime64BestEffort({to_date:String})")
		parameters["to_date"] = options.To.UTC().Format(time.RFC3339Nano)
	}
	if options.IncludeRecordOnly != nil && !*options.IncludeRecordOnly {
		predicates = append(predicates, "billing_disposition = 'billable'")
	}
	if options.Cursor != nil {
		if options.Cursor.EventAt.IsZero() {
			return bursar.UsageChargePage{}, errors.New("usage charge cursor event time must not be zero")
		}
		cursorID, err := uuid.Parse(strings.TrimSpace(options.Cursor.UsageID))
		if err != nil {
			return bursar.UsageChargePage{}, fmt.Errorf("usage charge cursor ID must be a UUID: %w", err)
		}
		predicates = append(predicates, "(event_at, charge_id) < (parseDateTime64BestEffort({cursor_event_at:String}), {cursor_usage_id:UUID})")
		parameters["cursor_event_at"] = options.Cursor.EventAt.UTC().Format(time.RFC3339Nano)
		parameters["cursor_usage_id"] = cursorID.String()
	}

	query := fmt.Sprintf(`SELECT
  toString(charge_id) AS usage_id,
  toString(account_id) AS account_id,
  operation,
  toString(requested) AS requested,
  toString(charged) AS charged,
  toString(allowance_requested) AS allowance_requested,
  toString(allowance_covered) AS allowance_covered,
  billing_disposition,
  feature,
  model,
  region,
  toString(event_at) AS event_at,
  idempotency_key,
  metadata,
  toString(created_at) AS created_at
FROM %s FINAL
WHERE %s
ORDER BY event_at DESC, charge_id DESC
LIMIT %d`, s.quotedTable, strings.Join(predicates, " AND "), limit+1)
	rows, err := s.query(ctx, query, parameters)
	if err != nil {
		return bursar.UsageChargePage{}, err
	}
	defer rows.Close()
	items := make([]bursar.UsageCharge, 0, limit+1)
	for rows.Next() {
		var usageID, accountID, operation string
		var requestedText, chargedText, allowanceRequestedText, allowanceCoveredText string
		var disposition string
		var feature, model, region *string
		var eventAtText, idempotencyKey, metadataText, createdAtText string
		if err := rows.Scan(
			&usageID, &accountID, &operation,
			&requestedText, &chargedText, &allowanceRequestedText, &allowanceCoveredText,
			&disposition, &feature, &model, &region, &eventAtText, &idempotencyKey,
			&metadataText, &createdAtText,
		); err != nil {
			return bursar.UsageChargePage{}, fmt.Errorf("scan ClickHouse usage charge: %w", err)
		}
		item, err := readUsageCharge(usageID, accountID, operation, requestedText, chargedText, allowanceRequestedText, allowanceCoveredText, disposition, feature, model, region, eventAtText, idempotencyKey, metadataText, createdAtText)
		if err != nil {
			return bursar.UsageChargePage{}, err
		}
		items = append(items, item)
	}
	if err := rows.Err(); err != nil {
		return bursar.UsageChargePage{}, fmt.Errorf("read ClickHouse usage charges: %w", err)
	}
	hasMore := len(items) > limit
	if hasMore {
		items = items[:limit]
	}
	page := bursar.UsageChargePage{Items: items}
	if hasMore && len(items) > 0 {
		last := items[len(items)-1]
		page.NextCursor = &bursar.UsageChargeCursor{EventAt: last.EventAt, UsageID: last.UsageID}
	}
	return page, nil
}

// Start initializes SDK-managed DDL when configured.
func (s *ClickHouseUsageStore) Start(ctx context.Context) error { return s.Initialize(ctx) }

// Flush is a no-op because batches are sent before WriteUsageBatch returns.
func (*ClickHouseUsageStore) Flush(ctx context.Context) error { return ctx.Err() }

// Health verifies the configured ClickHouse connection.
func (s *ClickHouseUsageStore) Health(ctx context.Context) error {
	if s == nil {
		return errors.New("ClickHouse usage store is not initialized")
	}
	release, err := s.enter(ctx)
	if err != nil {
		return err
	}
	defer release()
	return s.connection.Ping(ctx)
}

// Close marks the store closed and optionally closes the injected connection.
func (s *ClickHouseUsageStore) Close(ctx context.Context) error {
	if s == nil {
		return nil
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	s.lifecycleMu.Lock()
	defer s.lifecycleMu.Unlock()
	if s.closed {
		return nil
	}
	s.closed = true
	if s.ownsConnection {
		return s.connection.Close()
	}
	return nil
}

func (s *ClickHouseUsageStore) spendRows(ctx context.Context, keyExpression string, start, end time.Time, limit int) ([]spendRow, error) {
	if s == nil {
		return nil, errors.New("ClickHouse usage store is not initialized")
	}
	if err := validateRange(start, end); err != nil {
		return nil, err
	}
	limitSQL := ""
	if limit > 0 {
		limitSQL = "\nLIMIT " + strconv.Itoa(limit)
	}
	query := fmt.Sprintf(`SELECT
  toString(%s) AS key,
  toString(sum(charged)) AS total_spend,
  toString(count()) AS entry_count
FROM %s FINAL
WHERE tenant_id = {tenant_id:UUID}
  AND billing_disposition = 'billable'
  AND event_at >= parseDateTime64BestEffort({start:String})
  AND event_at < parseDateTime64BestEffort({end:String})
GROUP BY key
ORDER BY sum(charged) DESC, key%s`, keyExpression, s.quotedTable, limitSQL)
	rows, err := s.query(ctx, query, analyticsParameters(s.tenantID, start, end))
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := make([]spendRow, 0)
	for rows.Next() {
		var key, amountText, countText string
		if err := rows.Scan(&key, &amountText, &countText); err != nil {
			return nil, fmt.Errorf("scan ClickHouse spend rows: %w", err)
		}
		amount, count, err := parseSpendValues(amountText, countText)
		if err != nil {
			return nil, err
		}
		result = append(result, spendRow{key: key, totalSpend: amount, entryCount: count})
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("read ClickHouse spend rows: %w", err)
	}
	return result, nil
}

func (s *ClickHouseUsageStore) query(ctx context.Context, query string, parameters map[string]string) (driver.Rows, error) {
	if s == nil {
		return nil, errors.New("ClickHouse usage store is not initialized")
	}
	release, err := s.enter(ctx)
	if err != nil {
		return nil, err
	}
	if s.createTable {
		if err := s.initializeSchema(ctx); err != nil {
			release()
			return nil, err
		}
	}
	rows, err := s.connection.Query(withParameters(ctx, parameters), query)
	if err != nil {
		release()
		return nil, fmt.Errorf("query ClickHouse usage projection: %w", err)
	}
	return &unlockingRows{Rows: rows, release: release}, nil
}

func (s *ClickHouseUsageStore) projectUsage(event bursar.UsageChargeExport, outboxEventID string) ([]any, error) {
	tenantID, err := parseUUID(event.TenantID, "usage tenant ID")
	if err != nil {
		return nil, err
	}
	if tenantID != s.tenantID {
		return nil, errors.New("usage event tenant ID does not match ClickHouse store tenant ID")
	}
	chargeID, err := parseUUID(event.ChargeID, "usage charge ID")
	if err != nil {
		return nil, err
	}
	accountID, err := parseUUID(event.AccountID, "usage account ID")
	if err != nil {
		return nil, err
	}
	subjectID, err := parseUUID(event.SubjectID, "usage subject ID")
	if err != nil {
		return nil, err
	}
	outboxID, err := parseOutboxEventID(outboxEventID)
	if err != nil {
		return nil, err
	}
	operation, err := requireNonEmpty(event.Operation, "usage operation")
	if err != nil {
		return nil, err
	}
	idempotencyKey, err := requireNonEmpty(event.IdempotencyKey, "usage idempotency key")
	if err != nil {
		return nil, err
	}
	requestDigest, err := requireNonEmpty(event.RequestDigest, "usage request digest")
	if err != nil {
		return nil, err
	}
	if event.BillingDisposition != bursar.BillingDispositionBillable && event.BillingDisposition != bursar.BillingDispositionRecordOnly {
		return nil, errors.New("usage billing disposition must be billable or record_only")
	}
	for name, amount := range map[string]bursar.Amount{
		"requested": event.Requested, "charged": event.Charged,
		"allowance requested": event.AllowanceRequested, "allowance covered": event.AllowanceCovered,
	} {
		if err := validateDecimal20Scale6(amount, name); err != nil {
			return nil, err
		}
	}
	if event.EventAt.IsZero() || event.CreatedAt.IsZero() {
		return nil, errors.New("usage event and created timestamps must not be zero")
	}
	measures, err := marshalObject(event.Measures)
	if err != nil {
		return nil, fmt.Errorf("encode usage measures: %w", err)
	}
	dimensions, err := marshalObject(event.Dimensions)
	if err != nil {
		return nil, fmt.Errorf("encode usage dimensions: %w", err)
	}
	metadata, err := marshalObject(event.Metadata)
	if err != nil {
		return nil, fmt.Errorf("encode usage metadata: %w", err)
	}
	pricingSnapshot, err := marshalObject(event.PricingSnapshot)
	if err != nil {
		return nil, fmt.Errorf("encode usage pricing snapshot: %w", err)
	}
	catalogRevisionID, err := optionalUUID(event.CatalogRevisionID, "catalog revision ID")
	if err != nil {
		return nil, err
	}
	planID, err := optionalUUID(event.PlanID, "plan ID")
	if err != nil {
		return nil, err
	}
	ledgerEntryID, err := optionalUUID(event.LedgerEntryID, "ledger entry ID")
	if err != nil {
		return nil, err
	}
	correctionID, err := optionalUUID(event.CorrectionOfChargeID, "correction charge ID")
	if err != nil {
		return nil, err
	}
	return []any{
		tenantID, outboxID, chargeID, accountID, subjectID, operation,
		event.Feature, event.Model, event.Region, measures, dimensions, metadata,
		event.Requested, event.Charged, event.AllowanceRequested, event.AllowanceCovered,
		string(event.BillingDisposition), catalogRevisionID, planID, event.RateCardKey,
		pricingSnapshot, ledgerEntryID, correctionID, idempotencyKey, requestDigest,
		event.EventAt.UTC(), event.CreatedAt.UTC(),
	}, nil
}

func (s *ClickHouseUsageStore) enter(ctx context.Context) (func(), error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	s.lifecycleMu.RLock()
	if s.closed {
		s.lifecycleMu.RUnlock()
		return nil, errors.New("ClickHouse usage store is closed")
	}
	return s.lifecycleMu.RUnlock, nil
}

type unlockingRows struct {
	driver.Rows
	release func()
	once    sync.Once
}

func (r *unlockingRows) Close() error {
	err := r.Rows.Close()
	r.once.Do(r.release)
	return err
}

type spendRow struct {
	key        string
	totalSpend bursar.Amount
	entryCount int
}

type schemaExpectation struct {
	name  string
	types []string
}

var schemaExpectations = []schemaExpectation{
	{name: "tenant_id", types: []string{"UUID"}},
	{name: "outbox_event_id", types: []string{"UInt64"}},
	{name: "charge_id", types: []string{"UUID"}},
	{name: "account_id", types: []string{"UUID"}},
	{name: "subject_id", types: []string{"UUID"}},
	{name: "operation", types: []string{"String", "LowCardinality(String)"}},
	{name: "feature", types: []string{"Nullable(String)", "LowCardinality(Nullable(String))"}},
	{name: "model", types: []string{"Nullable(String)", "LowCardinality(Nullable(String))"}},
	{name: "region", types: []string{"Nullable(String)", "LowCardinality(Nullable(String))"}},
	{name: "measures", types: []string{"String"}},
	{name: "dimensions", types: []string{"String"}},
	{name: "metadata", types: []string{"String"}},
	{name: "requested", types: []string{"Decimal(20,6)"}},
	{name: "charged", types: []string{"Decimal(20,6)"}},
	{name: "allowance_requested", types: []string{"Decimal(20,6)"}},
	{name: "allowance_covered", types: []string{"Decimal(20,6)"}},
	{name: "billing_disposition", types: []string{"String", "LowCardinality(String)"}},
	{name: "catalog_revision_id", types: []string{"Nullable(UUID)"}},
	{name: "plan_id", types: []string{"Nullable(UUID)"}},
	{name: "rate_card_key", types: []string{"Nullable(String)"}},
	{name: "pricing_snapshot", types: []string{"String"}},
	{name: "ledger_entry_id", types: []string{"Nullable(UUID)"}},
	{name: "correction_of_charge_id", types: []string{"Nullable(UUID)"}},
	{name: "idempotency_key", types: []string{"String"}},
	{name: "request_digest", types: []string{"String"}},
	{name: "event_at", types: []string{"DateTime64(6,'UTC')"}},
	{name: "created_at", types: []string{"DateTime64(6,'UTC')"}},
}

var insertColumns = []string{
	"tenant_id", "outbox_event_id", "charge_id", "account_id", "subject_id",
	"operation", "feature", "model", "region", "measures", "dimensions", "metadata",
	"requested", "charged", "allowance_requested", "allowance_covered", "billing_disposition",
	"catalog_revision_id", "plan_id", "rate_card_key", "pricing_snapshot", "ledger_entry_id",
	"correction_of_charge_id", "idempotency_key", "request_digest", "event_at", "created_at",
}

func readUsageCharge(usageID, accountID, operation, requestedText, chargedText, allowanceRequestedText, allowanceCoveredText, disposition string, feature, model, region *string, eventAtText, idempotencyKey, metadataText, createdAtText string) (bursar.UsageCharge, error) {
	if _, err := parseUUID(usageID, "projected usage ID"); err != nil {
		return bursar.UsageCharge{}, err
	}
	if _, err := parseUUID(accountID, "projected account ID"); err != nil {
		return bursar.UsageCharge{}, err
	}
	requested, err := parseAmount(requestedText)
	if err != nil {
		return bursar.UsageCharge{}, err
	}
	charged, err := parseAmount(chargedText)
	if err != nil {
		return bursar.UsageCharge{}, err
	}
	allowanceRequested, err := parseAmount(allowanceRequestedText)
	if err != nil {
		return bursar.UsageCharge{}, err
	}
	allowanceCovered, err := parseAmount(allowanceCoveredText)
	if err != nil {
		return bursar.UsageCharge{}, err
	}
	if disposition != string(bursar.BillingDispositionBillable) && disposition != string(bursar.BillingDispositionRecordOnly) {
		return bursar.UsageCharge{}, errors.New("projected usage has an invalid billing disposition")
	}
	eventAt, err := parseClickHouseTimestamp(eventAtText)
	if err != nil {
		return bursar.UsageCharge{}, err
	}
	createdAt, err := parseClickHouseTimestamp(createdAtText)
	if err != nil {
		return bursar.UsageCharge{}, err
	}
	metadata := make(map[string]any)
	if err := json.Unmarshal([]byte(metadataText), &metadata); err != nil {
		return bursar.UsageCharge{}, fmt.Errorf("decode projected usage metadata: %w", err)
	}
	return bursar.UsageCharge{
		UsageID: usageID, AccountID: accountID, Operation: operation,
		Requested: requested, Charged: charged, AllowanceRequested: allowanceRequested,
		AllowanceCovered: allowanceCovered, BillingDisposition: disposition,
		Feature: optionalText(feature), Model: optionalText(model), Region: optionalText(region),
		EventAt: eventAt, IdempotencyKey: idempotencyKey, Metadata: metadata, CreatedAt: createdAt,
	}, nil
}

func validateRange(start, end time.Time) error {
	if start.IsZero() || end.IsZero() || !end.After(start) {
		return errors.New("analytics requires end after start")
	}
	return nil
}

func analyticsParameters(tenantID uuid.UUID, start, end time.Time) map[string]string {
	return map[string]string{
		"tenant_id": tenantID.String(),
		"start":     start.UTC().Format(time.RFC3339Nano),
		"end":       end.UTC().Format(time.RFC3339Nano),
	}
}

func withParameters(ctx context.Context, parameters map[string]string) context.Context {
	return ch.Context(ctx, ch.WithParameters(ch.Parameters(parameters)))
}

func parseSpendValues(amountText, countText string) (bursar.Amount, int, error) {
	amount, err := parseAmount(amountText)
	if err != nil {
		return bursar.DecimalZero, 0, err
	}
	count, err := parseCount(countText)
	return amount, count, err
}

func parseAmount(value string) (bursar.Amount, error) {
	amount, err := bursar.NewAmount(value)
	if err != nil {
		return bursar.DecimalZero, fmt.Errorf("parse ClickHouse decimal %q: %w", value, err)
	}
	return amount, nil
}

func parseCount(value string) (int, error) {
	parsed, err := strconv.ParseUint(value, 10, 63)
	if err != nil || parsed > uint64(math.MaxInt) {
		return 0, fmt.Errorf("parse ClickHouse count %q", value)
	}
	return int(parsed), nil
}

func parseClickHouseTimestamp(value string) (time.Time, error) {
	if !clickHouseTimestampPattern.MatchString(value) {
		return time.Time{}, fmt.Errorf("invalid ClickHouse timestamp: %s", value)
	}
	parsed, err := time.ParseInLocation("2006-01-02 15:04:05.999999", value, time.UTC)
	if err != nil {
		return time.Time{}, fmt.Errorf("invalid ClickHouse timestamp %s: %w", value, err)
	}
	return parsed, nil
}

func parseOutboxEventID(value string) (uint64, error) {
	if !unsignedIntegerPattern.MatchString(value) {
		return 0, errors.New("ClickHouse outbox event ID must be an unsigned integer string")
	}
	parsed, err := strconv.ParseUint(value, 10, 64)
	if err != nil {
		return 0, errors.New("ClickHouse outbox event ID exceeds UInt64")
	}
	return parsed, nil
}

func validateDecimal20Scale6(value bursar.Amount, name string) error {
	if !value.Equal(value.Round(bursar.MoneyDecimalPlaces)) {
		return fmt.Errorf("%s exceeds ClickHouse Decimal(20,6) scale", name)
	}
	limit := decimal.New(1, 14)
	if value.Abs().GreaterThanOrEqual(limit) {
		return fmt.Errorf("%s exceeds ClickHouse Decimal(20,6) precision", name)
	}
	return nil
}

func marshalObject(value map[string]any) (string, error) {
	if value == nil {
		value = map[string]any{}
	}
	encoded, err := json.Marshal(value)
	return string(encoded), err
}

func optionalUUID(value *string, name string) (*uuid.UUID, error) {
	if value == nil {
		return nil, nil
	}
	parsed, err := parseUUID(*value, name)
	if err != nil {
		return nil, err
	}
	return &parsed, nil
}

func parseUUID(value, name string) (uuid.UUID, error) {
	parsed, err := uuid.Parse(strings.TrimSpace(value))
	if err != nil {
		return uuid.Nil, fmt.Errorf("%s must be a UUID: %w", name, err)
	}
	return parsed, nil
}

func requireNonEmpty(value, name string) (string, error) {
	normalized := strings.TrimSpace(value)
	if normalized == "" {
		return "", fmt.Errorf("%s must not be empty", name)
	}
	return normalized, nil
}

func optionalText(value *string) string {
	if value == nil {
		return ""
	}
	return *value
}

func quoteTable(table string) string {
	parts := strings.Split(table, ".")
	for index, part := range parts {
		parts[index] = `"` + part + `"`
	}
	return strings.Join(parts, ".")
}

func splitTable(table string) (string, string) {
	parts := strings.SplitN(table, ".", 2)
	if len(parts) == 1 {
		return "", parts[0]
	}
	return parts[0], parts[1]
}

func normalizeType(value string) string { return strings.Join(strings.Fields(value), "") }

func containsSQLIdentifier(value, identifier string) bool {
	pattern := regexp.MustCompile(`\b` + regexp.QuoteMeta(identifier) + `\b`)
	return pattern.MatchString(value)
}
