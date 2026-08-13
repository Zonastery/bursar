// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import (
	"context"
	"encoding/json"
	"errors"
	"math"
	"strings"
	"time"
)

type MaintenanceTaskStatus string

const (
	MaintenanceTaskCompleted   MaintenanceTaskStatus = "completed"
	MaintenanceTaskSkipped     MaintenanceTaskStatus = "skipped"
	MaintenanceTaskUnsupported MaintenanceTaskStatus = "unsupported"
	MaintenanceTaskFailed      MaintenanceTaskStatus = "failed"
)

type MaintenanceRunStatus string

const (
	MaintenanceRunCompleted MaintenanceRunStatus = "completed"
	MaintenanceRunPartial   MaintenanceRunStatus = "partial"
	MaintenanceRunFailed    MaintenanceRunStatus = "failed"
)

type MaintenanceTaskResult struct {
	Status  MaintenanceTaskStatus `json:"status"`
	Count   int                   `json:"count"`
	Limit   int                   `json:"limit"`
	HasMore bool                  `json:"has_more"`
	Reason  *string               `json:"reason,omitempty"`
	Error   *string               `json:"error,omitempty"`
}

type MaintenanceTasks struct {
	ExpiredLeases       MaintenanceTaskResult `json:"expired_leases"`
	ExpiredCredits      MaintenanceTaskResult `json:"expired_credits"`
	DuePlanChanges      MaintenanceTaskResult `json:"due_plan_changes"`
	PastDueGracePeriods MaintenanceTaskResult `json:"past_due_grace_periods"`
}

type MaintenanceRunResult struct {
	Status  MaintenanceRunStatus `json:"status"`
	Count   int                  `json:"count"`
	HasMore bool                 `json:"has_more"`
	Tasks   MaintenanceTasks     `json:"tasks"`
}

type MaintenanceRunOptions struct {
	Limit int
	Now   time.Time
}

type MaintenanceOperations struct {
	ExpireLeases                         func(context.Context, int) (int, error)
	ExpireCredits                        func(context.Context, int) (int, error)
	ApplyDuePlanChanges                  func(context.Context, int) (int, error)
	ExpirePastDueGracePeriods            func(context.Context, time.Time) (int, error)
	PastDueGracePeriodLimit              int
	PastDueGracePeriodsUnavailableReason string
}

// BursarMaintenance performs one host-invoked, tenant-scoped bounded pass. It
// deliberately never schedules itself.
type BursarMaintenance struct {
	operations MaintenanceOperations
}

func NewBursarMaintenance(operations MaintenanceOperations) (*BursarMaintenance, error) {
	if operations.PastDueGracePeriodLimit == 0 {
		operations.PastDueGracePeriodLimit = 100
	}
	if operations.PastDueGracePeriodLimit < 1 || operations.PastDueGracePeriodLimit > 1_000 {
		return nil, outboxConfigError("past-due grace-period limit must be between 1 and 1000")
	}
	return &BursarMaintenance{operations: operations}, nil
}

// NewBursarMaintenanceFromStore wires the three credit-store maintenance
// operations while leaving billing grace-period expiry as an explicit optional
// capability.
func NewBursarMaintenanceFromStore(store CreditStore, expirePastDueGracePeriods func(context.Context, time.Time) (int, error)) (*BursarMaintenance, error) {
	if store == nil {
		return nil, outboxConfigError("credit store is required")
	}
	return NewBursarMaintenance(MaintenanceOperations{
		ExpireLeases: store.ExpireLeases,
		ExpireCredits: func(ctx context.Context, limit int) (int, error) {
			result, err := store.SweepExpiredCredits(ctx, false, "", limit)
			return result.ExpiredCount, err
		},
		ApplyDuePlanChanges:       store.ApplyDuePlanChanges,
		ExpirePastDueGracePeriods: expirePastDueGracePeriods,
	})
}

func (m *BursarMaintenance) RunOnce(ctx context.Context, options MaintenanceRunOptions) (MaintenanceRunResult, error) {
	if m == nil {
		return MaintenanceRunResult{}, outboxConfigError("maintenance is not initialized")
	}
	if options.Limit == 0 {
		options.Limit = 100
	}
	if options.Limit < 1 || options.Limit > 1_000 {
		return MaintenanceRunResult{}, outboxConfigError("maintenance limit must be between 1 and 1000")
	}
	if options.Now.IsZero() {
		options.Now = time.Now().UTC()
	} else {
		options.Now = options.Now.UTC()
	}
	tasks := MaintenanceTasks{
		ExpiredLeases:  runMaintenanceTask(ctx, m.operations.ExpireLeases, options.Limit),
		ExpiredCredits: runMaintenanceTask(ctx, m.operations.ExpireCredits, options.Limit),
		DuePlanChanges: runMaintenanceTask(ctx, m.operations.ApplyDuePlanChanges, options.Limit),
		PastDueGracePeriods: runTimeMaintenanceTask(
			ctx,
			m.operations.ExpirePastDueGracePeriods,
			options.Now,
			m.operations.PastDueGracePeriodLimit,
			m.operations.PastDueGracePeriodsUnavailableReason,
		),
	}
	results := []MaintenanceTaskResult{tasks.ExpiredLeases, tasks.ExpiredCredits, tasks.DuePlanChanges, tasks.PastDueGracePeriods}
	result := MaintenanceRunResult{Status: MaintenanceRunCompleted, Tasks: tasks}
	applicable := 0
	failed := 0
	for _, task := range results {
		result.Count += task.Count
		result.HasMore = result.HasMore || task.HasMore
		if task.Status == MaintenanceTaskCompleted || task.Status == MaintenanceTaskFailed {
			applicable++
			if task.Status == MaintenanceTaskFailed {
				failed++
			}
		}
	}
	if applicable > 0 && failed == applicable {
		result.Status = MaintenanceRunFailed
	} else if failed > 0 {
		result.Status = MaintenanceRunPartial
	}
	return result, nil
}

func runMaintenanceTask(ctx context.Context, operation func(context.Context, int) (int, error), limit int) MaintenanceTaskResult {
	if operation == nil {
		return unavailableMaintenanceTask(limit, "")
	}
	count, err := operation(ctx, limit)
	if count < 0 && err == nil {
		err = errors.New("maintenance task returned an invalid count")
	}
	if err != nil {
		summary := persistedDiagnosticSummary(err, "maintenance_task_failed")
		return MaintenanceTaskResult{Status: MaintenanceTaskFailed, Limit: limit, HasMore: true, Error: &summary}
	}
	return MaintenanceTaskResult{Status: MaintenanceTaskCompleted, Count: count, Limit: limit, HasMore: count == limit}
}

func runTimeMaintenanceTask(ctx context.Context, operation func(context.Context, time.Time) (int, error), now time.Time, limit int, reason string) MaintenanceTaskResult {
	if operation == nil {
		return unavailableMaintenanceTask(limit, reason)
	}
	count, err := operation(ctx, now)
	if count < 0 && err == nil {
		err = errors.New("maintenance task returned an invalid count")
	}
	if err != nil {
		summary := persistedDiagnosticSummary(err, "maintenance_task_failed")
		return MaintenanceTaskResult{Status: MaintenanceTaskFailed, Limit: limit, HasMore: true, Error: &summary}
	}
	return MaintenanceTaskResult{Status: MaintenanceTaskCompleted, Count: count, Limit: limit, HasMore: count == limit}
}

func unavailableMaintenanceTask(limit int, reason string) MaintenanceTaskResult {
	result := MaintenanceTaskResult{Status: MaintenanceTaskUnsupported, Limit: limit}
	if reason = strings.TrimSpace(reason); reason != "" {
		result.Status = MaintenanceTaskSkipped
		result.Reason = &reason
	}
	return result
}

type StorageMaintenanceStatus string

const (
	StorageMaintenanceCompleted StorageMaintenanceStatus = "completed"
	StorageMaintenanceBusy      StorageMaintenanceStatus = "busy"
	StorageMaintenanceNotDue    StorageMaintenanceStatus = "not_due"
	StorageMaintenanceFailed    StorageMaintenanceStatus = "failed"
)

type StorageMaintenanceCounts struct {
	UsagePayloadsPurged      int `json:"usage_payloads_purged"`
	RecordOnlyUsagePurged    int `json:"record_only_usage_purged"`
	BillingPayloadsPurged    int `json:"billing_payloads_purged"`
	QuotaUsageEventsPurged   int `json:"quota_usage_events_purged"`
	QuotaNotificationsPurged int `json:"quota_notifications_purged"`
	TerminalLeasesCompacted  int `json:"terminal_leases_compacted"`
	UsageRollupsPurged       int `json:"usage_rollups_purged"`
	OutboxEventsPurged       int `json:"outbox_events_purged"`
}

type StorageMaintenanceResult struct {
	Status            StorageMaintenanceStatus `json:"status"`
	Count             int                      `json:"count"`
	HasMore           bool                     `json:"has_more"`
	BatchSize         *int                     `json:"batch_size,omitempty"`
	Counts            StorageMaintenanceCounts `json:"counts"`
	LastMaintenanceAt *time.Time               `json:"last_maintenance_at,omitempty"`
	NextMaintenanceAt *time.Time               `json:"next_maintenance_at,omitempty"`
	Error             *string                  `json:"error,omitempty"`
}

type OperatorMaintenanceMode string

const (
	OperatorMaintenanceIfDue OperatorMaintenanceMode = "if_due"
	OperatorMaintenanceForce OperatorMaintenanceMode = "force"
)

type OperatorMaintenanceRunOptions struct {
	Mode OperatorMaintenanceMode
	Now  time.Time
}

type StoragePartition string

const (
	StoragePartitionUsageChargePayloads  StoragePartition = "usage_charge_payloads"
	StoragePartitionBillingEventPayloads StoragePartition = "billing_event_payloads"
)

type PartitionMaintenanceStatus string

const (
	PartitionMaintenanceCompleted PartitionMaintenanceStatus = "completed"
	PartitionMaintenanceBusy      PartitionMaintenanceStatus = "busy"
	PartitionMaintenanceFailed    PartitionMaintenanceStatus = "failed"
)

type PartitionMaintenanceResult struct {
	Status                  PartitionMaintenanceStatus `json:"status"`
	ParentTable             StoragePartition           `json:"parent_table"`
	Count                   int                        `json:"count"`
	PartitionsCreated       int                        `json:"partitions_created"`
	PartitionsDropped       int                        `json:"partitions_dropped"`
	PartitionLockTimeouts   int                        `json:"partition_lock_timeouts"`
	DefaultPartitionHasRows bool                       `json:"default_partition_has_rows"`
	HasMore                 bool                       `json:"has_more"`
	Error                   *string                    `json:"error,omitempty"`
}

type BursarOperatorMaintenance struct {
	client storagePostgresCaller
}

func NewBursarOperatorMaintenance(client *PostgresClient) (*BursarOperatorMaintenance, error) {
	if client == nil {
		return nil, outboxConfigError("operator PostgreSQL client is required")
	}
	if client.options.AccessRole != PostgresAccessRoleOperator || client.TenantID() != "" {
		return nil, outboxConfigError("operator maintenance requires an unscoped bursar_operator PostgreSQL client")
	}
	return &BursarOperatorMaintenance{client: client}, nil
}

func newBursarOperatorMaintenance(client storagePostgresCaller) (*BursarOperatorMaintenance, error) {
	if client == nil {
		return nil, outboxConfigError("operator PostgreSQL client is required")
	}
	return &BursarOperatorMaintenance{client: client}, nil
}

func (m *BursarOperatorMaintenance) RunOnce(ctx context.Context, options OperatorMaintenanceRunOptions) StorageMaintenanceResult {
	if m == nil || m.client == nil {
		return failedStorageMaintenance(outboxConfigError("operator maintenance is not initialized"))
	}
	if options.Mode == "" {
		options.Mode = OperatorMaintenanceIfDue
	}
	if options.Mode != OperatorMaintenanceIfDue && options.Mode != OperatorMaintenanceForce {
		return failedStorageMaintenance(outboxConfigError("operator maintenance mode must be if_due or force"))
	}
	if options.Now.IsZero() {
		options.Now = time.Now().UTC()
	} else {
		options.Now = options.Now.UTC()
	}
	functionName := "maybe_run_storage_maintenance"
	if options.Mode == OperatorMaintenanceForce {
		functionName = "run_storage_maintenance"
	}
	payload, err := callMaintenanceJSON(ctx, m.client, functionName, options.Now)
	if err != nil {
		return failedStorageMaintenance(err)
	}
	result, err := storageMaintenanceResult(payload)
	if err != nil {
		return failedStorageMaintenance(err)
	}
	return result
}

func (m *BursarOperatorMaintenance) RunPartitionOnce(ctx context.Context, parent StoragePartition, now time.Time) PartitionMaintenanceResult {
	if parent != StoragePartitionUsageChargePayloads && parent != StoragePartitionBillingEventPayloads {
		return failedPartitionMaintenance(parent, outboxConfigError("parent table must be a Bursar storage partition"))
	}
	if m == nil || m.client == nil {
		return failedPartitionMaintenance(parent, outboxConfigError("operator maintenance is not initialized"))
	}
	if now.IsZero() {
		now = time.Now().UTC()
	} else {
		now = now.UTC()
	}
	payload, err := callMaintenanceJSON(ctx, m.client, "run_storage_partition_maintenance", string(parent), now)
	if err != nil {
		return failedPartitionMaintenance(parent, err)
	}
	result, err := partitionMaintenanceResult(parent, payload)
	if err != nil {
		return failedPartitionMaintenance(parent, err)
	}
	return result
}

func callMaintenanceJSON(ctx context.Context, client storagePostgresCaller, functionName string, arguments ...any) (map[string]any, error) {
	rows, err := client.Call(ctx, functionName, arguments...)
	if err != nil {
		return nil, err
	}
	row, err := exactlyOneRow(rows, functionName)
	if err != nil {
		return nil, err
	}
	value, err := firstScalar(row, functionName)
	if err != nil {
		return nil, err
	}
	return requiredJSONMap(value, functionName+".maintenance_result")
}

func storageMaintenanceResult(payload map[string]any) (StorageMaintenanceResult, error) {
	status, err := requiredRowText(payload, "status", "storage maintenance")
	if err != nil {
		return StorageMaintenanceResult{}, err
	}
	if status == string(StorageMaintenanceBusy) {
		return StorageMaintenanceResult{Status: StorageMaintenanceBusy, HasMore: true}, nil
	}
	if status == string(StorageMaintenanceNotDue) {
		last, err := optionalJSONTime(payload, "last_maintenance_at", "storage maintenance")
		if err != nil {
			return StorageMaintenanceResult{}, err
		}
		next, err := optionalJSONTime(payload, "next_maintenance_at", "storage maintenance")
		if err != nil {
			return StorageMaintenanceResult{}, err
		}
		return StorageMaintenanceResult{Status: StorageMaintenanceNotDue, LastMaintenanceAt: last, NextMaintenanceAt: next}, nil
	}
	if status != string(StorageMaintenanceCompleted) {
		return StorageMaintenanceResult{}, NewStoreError("maintenance RPC returned an unknown status", ErrorOptions{})
	}
	counts := StorageMaintenanceCounts{}
	fields := []struct {
		key    string
		target *int
	}{
		{"usage_payloads_purged", &counts.UsagePayloadsPurged},
		{"record_only_usage_purged", &counts.RecordOnlyUsagePurged},
		{"billing_payloads_purged", &counts.BillingPayloadsPurged},
		{"quota_usage_events_purged", &counts.QuotaUsageEventsPurged},
		{"quota_notifications_purged", &counts.QuotaNotificationsPurged},
		{"terminal_leases_compacted", &counts.TerminalLeasesCompacted},
		{"usage_rollups_purged", &counts.UsageRollupsPurged},
		{"outbox_events_purged", &counts.OutboxEventsPurged},
	}
	total := 0
	for _, field := range fields {
		value, err := maintenanceNonnegativeInt(payload, field.key, "storage maintenance")
		if err != nil {
			return StorageMaintenanceResult{}, err
		}
		*field.target = value
		total += value
	}
	batchSize, err := maintenanceNonnegativeInt(payload, "batch_size", "storage maintenance")
	if err != nil {
		return StorageMaintenanceResult{}, err
	}
	hasMore, err := rowBool(payload, "has_more", "storage maintenance")
	if err != nil {
		return StorageMaintenanceResult{}, err
	}
	return StorageMaintenanceResult{Status: StorageMaintenanceCompleted, Count: total, HasMore: hasMore, BatchSize: &batchSize, Counts: counts}, nil
}

func partitionMaintenanceResult(parent StoragePartition, payload map[string]any) (PartitionMaintenanceResult, error) {
	status, err := requiredRowText(payload, "status", "partition maintenance")
	if err != nil {
		return PartitionMaintenanceResult{}, err
	}
	if status == string(PartitionMaintenanceBusy) {
		return PartitionMaintenanceResult{Status: PartitionMaintenanceBusy, ParentTable: parent, HasMore: true}, nil
	}
	returnedParent, err := requiredRowText(payload, "parent_table", "partition maintenance")
	if err != nil {
		return PartitionMaintenanceResult{}, err
	}
	if status != string(PartitionMaintenanceCompleted) || returnedParent != string(parent) {
		return PartitionMaintenanceResult{}, NewStoreError("partition maintenance RPC returned a malformed result", ErrorOptions{})
	}
	created, err := maintenanceNonnegativeInt(payload, "partitions_created", "partition maintenance")
	if err != nil {
		return PartitionMaintenanceResult{}, err
	}
	dropped, err := maintenanceNonnegativeInt(payload, "partitions_dropped", "partition maintenance")
	if err != nil {
		return PartitionMaintenanceResult{}, err
	}
	timeouts, err := maintenanceNonnegativeInt(payload, "partition_lock_timeouts", "partition maintenance")
	if err != nil {
		return PartitionMaintenanceResult{}, err
	}
	defaultRows, err := rowBool(payload, "default_partition_has_rows", "partition maintenance")
	if err != nil {
		return PartitionMaintenanceResult{}, err
	}
	hasMore, err := rowBool(payload, "has_more", "partition maintenance")
	if err != nil {
		return PartitionMaintenanceResult{}, err
	}
	return PartitionMaintenanceResult{
		Status: PartitionMaintenanceCompleted, ParentTable: parent, Count: created + dropped,
		PartitionsCreated: created, PartitionsDropped: dropped, PartitionLockTimeouts: timeouts,
		DefaultPartitionHasRows: defaultRows, HasMore: hasMore,
	}, nil
}

func failedStorageMaintenance(err error) StorageMaintenanceResult {
	summary := persistedDiagnosticSummary(err, "storage_maintenance_failed")
	return StorageMaintenanceResult{Status: StorageMaintenanceFailed, HasMore: true, Error: &summary}
}

func failedPartitionMaintenance(parent StoragePartition, err error) PartitionMaintenanceResult {
	summary := persistedDiagnosticSummary(err, "partition_maintenance_failed")
	return PartitionMaintenanceResult{Status: PartitionMaintenanceFailed, ParentTable: parent, HasMore: true, Error: &summary}
}

func optionalJSONTime(payload map[string]any, key, operation string) (*time.Time, error) {
	value := rowValue(payload, key)
	if value == nil {
		return nil, nil
	}
	if raw, ok := value.(json.Number); ok {
		value = raw.String()
	}
	return optionalRowTime(map[string]any{key: value}, key, operation)
}

func maintenanceNonnegativeInt(payload map[string]any, key, operation string) (int, error) {
	value := rowValue(payload, key)
	switch typed := value.(type) {
	case float64:
		if math.IsNaN(typed) || math.IsInf(typed, 0) || typed < 0 || typed > float64(safeIntegerMax) || math.Trunc(typed) != typed {
			break
		}
		return int(typed), nil
	case json.Number:
		parsed, err := typed.Int64()
		if err == nil && parsed >= 0 && parsed <= safeIntegerMax {
			return int(parsed), nil
		}
	default:
		return nonnegativeRowInt(payload, key, operation)
	}
	return 0, NewStoreError(operation+" returned an invalid "+key, ErrorOptions{})
}
