package bursar

import (
	"context"
	"encoding/json"
	"errors"
	"testing"
	"time"
)

func TestBursarMaintenanceAggregatesPartialRun(t *testing.T) {
	now := time.Date(2026, 8, 13, 4, 5, 6, 0, time.UTC)
	maintenance, err := NewBursarMaintenance(MaintenanceOperations{
		ExpireLeases:  func(_ context.Context, limit int) (int, error) { return limit, nil },
		ExpireCredits: func(context.Context, int) (int, error) { return 3, nil },
		ApplyDuePlanChanges: func(context.Context, int) (int, error) {
			return 0, errors.New("database DSN and secret payload")
		},
		PastDueGracePeriodLimit:              25,
		PastDueGracePeriodsUnavailableReason: "billing store is not configured",
	})
	if err != nil {
		t.Fatal(err)
	}
	result, err := maintenance.RunOnce(context.Background(), MaintenanceRunOptions{Limit: 100, Now: now})
	if err != nil {
		t.Fatal(err)
	}
	if result.Status != MaintenanceRunPartial || result.Count != 103 || !result.HasMore {
		t.Fatalf("result = %#v", result)
	}
	if result.Tasks.DuePlanChanges.Error == nil || *result.Tasks.DuePlanChanges.Error != "maintenance_task_failed:Error" {
		t.Fatalf("unsafe diagnostic = %#v", result.Tasks.DuePlanChanges.Error)
	}
	if result.Tasks.PastDueGracePeriods.Status != MaintenanceTaskSkipped || result.Tasks.PastDueGracePeriods.Limit != 25 {
		t.Fatalf("grace task = %#v", result.Tasks.PastDueGracePeriods)
	}
}

func TestBursarMaintenanceRejectsUnboundedLimits(t *testing.T) {
	if _, err := NewBursarMaintenance(MaintenanceOperations{PastDueGracePeriodLimit: 1_001}); err == nil {
		t.Fatal("expected grace-period limit error")
	}
	maintenance, err := NewBursarMaintenance(MaintenanceOperations{})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := maintenance.RunOnce(context.Background(), MaintenanceRunOptions{Limit: 1_001}); err == nil {
		t.Fatal("expected maintenance limit error")
	}
}

func TestOperatorMaintenanceMapsCompletedAndPartitionResults(t *testing.T) {
	now := time.Date(2026, 8, 13, 4, 5, 6, 789_000_000, time.FixedZone("IST", 5*60*60+30*60))
	caller := &storageCallerStub{call: func(_ context.Context, name string, _ ...any) ([]map[string]any, error) {
		switch name {
		case "run_storage_maintenance":
			return []map[string]any{{"maintenance_result": map[string]any{
				"status": "completed", "usage_payloads_purged": 1, "record_only_usage_purged": 2,
				"billing_payloads_purged": 3, "quota_usage_events_purged": 4,
				"quota_notifications_purged": 5, "terminal_leases_compacted": 6,
				"usage_rollups_purged": 7, "outbox_events_purged": 8,
				"batch_size": 100, "has_more": true,
			}}}, nil
		case "run_storage_partition_maintenance":
			return []map[string]any{{"maintenance_result": `{
				"status":"completed","parent_table":"usage_charge_payloads",
				"partitions_created":2,"partitions_dropped":1,"partition_lock_timeouts":0,
				"default_partition_has_rows":false,"has_more":false
			}`}}, nil
		default:
			t.Fatalf("unexpected RPC %q", name)
			return nil, nil
		}
	}}
	maintenance, err := newBursarOperatorMaintenance(caller)
	if err != nil {
		t.Fatal(err)
	}
	result := maintenance.RunOnce(context.Background(), OperatorMaintenanceRunOptions{Mode: OperatorMaintenanceForce, Now: now})
	if result.Status != StorageMaintenanceCompleted || result.Count != 36 || result.BatchSize == nil || *result.BatchSize != 100 || !result.HasMore {
		t.Fatalf("storage result = %#v", result)
	}
	partition := maintenance.RunPartitionOnce(context.Background(), StoragePartitionUsageChargePayloads, now)
	if partition.Status != PartitionMaintenanceCompleted || partition.Count != 3 || partition.HasMore {
		t.Fatalf("partition result = %#v", partition)
	}
	maintenanceArgs := storageCallArguments(t, caller, "run_storage_maintenance", 0)
	if len(maintenanceArgs) != 1 {
		t.Fatalf("run_storage_maintenance args = %#v", maintenanceArgs)
	}
	maintenanceNow, ok := maintenanceArgs[0].(time.Time)
	if !ok || !maintenanceNow.Equal(now.UTC()) || maintenanceNow.Location() != time.UTC {
		t.Fatalf("run_storage_maintenance timestamp = %#v (%T)", maintenanceArgs[0], maintenanceArgs[0])
	}
	partitionArgs := storageCallArguments(t, caller, "run_storage_partition_maintenance", 0)
	if len(partitionArgs) != 2 || partitionArgs[0] != string(StoragePartitionUsageChargePayloads) {
		t.Fatalf("run_storage_partition_maintenance args = %#v", partitionArgs)
	}
	partitionNow, ok := partitionArgs[1].(time.Time)
	if !ok || !partitionNow.Equal(now.UTC()) || partitionNow.Location() != time.UTC {
		t.Fatalf("run_storage_partition_maintenance timestamp = %#v (%T)", partitionArgs[1], partitionArgs[1])
	}
}

func TestOperatorMaintenanceFailureIsSafe(t *testing.T) {
	caller := &storageCallerStub{call: func(context.Context, string, ...any) ([]map[string]any, error) {
		return nil, errors.New("postgresql://user:password@example.test/secret")
	}}
	maintenance, err := newBursarOperatorMaintenance(caller)
	if err != nil {
		t.Fatal(err)
	}
	result := maintenance.RunOnce(context.Background(), OperatorMaintenanceRunOptions{})
	if result.Status != StorageMaintenanceFailed || result.Error == nil || *result.Error != "storage_maintenance_failed:Error" {
		t.Fatalf("result = %#v", result)
	}
	partition := maintenance.RunPartitionOnce(context.Background(), "not_a_partition", time.Time{})
	if partition.Status != PartitionMaintenanceFailed || partition.Error == nil || *partition.Error != "partition_maintenance_failed:CONFIG_ERROR" {
		t.Fatalf("partition = %#v", partition)
	}
}

func TestBursarMaintenanceCoversUnavailableAndFailureBranches(t *testing.T) {
	if _, err := NewBursarMaintenance(MaintenanceOperations{PastDueGracePeriodLimit: -1}); err == nil {
		t.Fatal("negative grace-period limit accepted")
	}
	var nilMaintenance *BursarMaintenance
	if _, err := nilMaintenance.RunOnce(context.Background(), MaintenanceRunOptions{}); err == nil {
		t.Fatal("nil maintenance accepted")
	}
	maintenance, err := NewBursarMaintenance(MaintenanceOperations{
		ExpireLeases: func(context.Context, int) (int, error) { return -1, nil },
		ExpireCredits: func(context.Context, int) (int, error) {
			return 0, errors.New("secret database URL")
		},
		ApplyDuePlanChanges:       func(context.Context, int) (int, error) { return 0, nil },
		ExpirePastDueGracePeriods: func(context.Context, time.Time) (int, error) { return -1, nil },
	})
	if err != nil {
		t.Fatal(err)
	}
	result, err := maintenance.RunOnce(context.Background(), MaintenanceRunOptions{Limit: 1, Now: time.Date(2026, 8, 13, 1, 2, 3, 0, time.FixedZone("IST", 19800))})
	if err != nil || result.Status != MaintenanceRunPartial || result.Tasks.ExpiredLeases.Status != MaintenanceTaskFailed || result.Tasks.PastDueGracePeriods.Status != MaintenanceTaskFailed {
		t.Fatalf("result = %#v, error = %v", result, err)
	}
	if result.Tasks.ExpiredLeases.Error == nil || *result.Tasks.ExpiredLeases.Error != "maintenance_task_failed:Error" {
		t.Fatalf("unsafe lease error = %#v", result.Tasks.ExpiredLeases.Error)
	}

	if got := runMaintenanceTask(context.Background(), nil, 10); got.Status != MaintenanceTaskUnsupported || got.Limit != 10 {
		t.Fatalf("nil task = %#v", got)
	}
	if got := runTimeMaintenanceTask(context.Background(), nil, time.Now(), 10, "  billing unavailable "); got.Status != MaintenanceTaskSkipped || got.Reason == nil || *got.Reason != "billing unavailable" {
		t.Fatalf("skipped task = %#v", got)
	}
	if got := unavailableMaintenanceTask(10, " "); got.Status != MaintenanceTaskUnsupported {
		t.Fatalf("blank reason task = %#v", got)
	}
}

func TestBursarMaintenanceFromStoreWiresOperations(t *testing.T) {
	store := &creditsSurfaceStore{}
	called := false
	maintenance, err := NewBursarMaintenanceFromStore(store, func(context.Context, time.Time) (int, error) {
		called = true
		return 2, nil
	})
	if err != nil {
		t.Fatal(err)
	}
	result, err := maintenance.RunOnce(context.Background(), MaintenanceRunOptions{Limit: 2, Now: time.Now()})
	if err != nil || result.Count != 5 || !called || result.Tasks.PastDueGracePeriods.Status != MaintenanceTaskCompleted {
		t.Fatalf("wired maintenance = %#v, error = %v, called = %v", result, err, called)
	}
}

func TestOperatorMaintenanceConstructorsAndMalformedResponses(t *testing.T) {
	if _, err := NewBursarOperatorMaintenance(nil); err == nil {
		t.Fatal("nil operator client accepted")
	}
	for _, client := range []*PostgresClient{
		{options: PostgresClientOptions{AccessRole: PostgresAccessRoleClient, TenantID: storageTestTenant}},
		{options: PostgresClientOptions{AccessRole: PostgresAccessRoleOperator, TenantID: storageTestTenant}},
	} {
		if _, err := NewBursarOperatorMaintenance(client); err == nil {
			t.Fatal("invalid operator client accepted")
		}
	}
	valid, err := NewBursarOperatorMaintenance(&PostgresClient{options: PostgresClientOptions{AccessRole: PostgresAccessRoleOperator}})
	if err != nil {
		t.Fatal(err)
	}
	if valid == nil {
		t.Fatal("valid operator client returned nil")
	}
	var nilOperator *BursarOperatorMaintenance
	if got := nilOperator.RunOnce(context.Background(), OperatorMaintenanceRunOptions{}); got.Status != StorageMaintenanceFailed {
		t.Fatalf("nil operator result = %#v", got)
	}

	caller := &storageCallerStub{call: func(context.Context, string, ...any) ([]map[string]any, error) {
		return []map[string]any{{"maintenance_result": json.Number("1")}}, nil
	}}
	maintenance, err := newBursarOperatorMaintenance(caller)
	if err != nil {
		t.Fatal(err)
	}
	for _, mode := range []OperatorMaintenanceMode{"bad", "force"} {
		got := maintenance.RunOnce(context.Background(), OperatorMaintenanceRunOptions{Mode: mode})
		if mode == "bad" && got.Status != StorageMaintenanceFailed {
			t.Fatalf("invalid mode result = %#v", got)
		}
		if mode == "force" && got.Status != StorageMaintenanceFailed {
			t.Fatalf("malformed result = %#v", got)
		}
	}
	if got := maintenance.RunPartitionOnce(context.Background(), StoragePartitionUsageChargePayloads, time.Time{}); got.Status != PartitionMaintenanceFailed {
		t.Fatalf("malformed partition result = %#v", got)
	}
	if got := maintenance.RunPartitionOnce(context.Background(), StoragePartitionBillingEventPayloads, time.Now()); got.Status != PartitionMaintenanceFailed {
		t.Fatalf("second malformed partition result = %#v", got)
	}
}

func TestMaintenanceResultValidationMatrix(t *testing.T) {
	now := time.Date(2026, 8, 13, 1, 2, 3, 0, time.UTC)
	if got, err := storageMaintenanceResult(map[string]any{
		"status": "not_due", "last_maintenance_at": json.Number("2026-08-13T01:02:03Z"), "next_maintenance_at": now,
	}); err != nil || got.LastMaintenanceAt == nil || got.NextMaintenanceAt == nil {
		t.Fatalf("not due times = %#v, %v", got, err)
	}
	completed := map[string]any{"status": "completed", "batch_size": int64(1), "has_more": false}
	for _, key := range []string{"usage_payloads_purged", "record_only_usage_purged", "billing_payloads_purged", "quota_usage_events_purged", "quota_notifications_purged", "terminal_leases_compacted", "usage_rollups_purged", "outbox_events_purged"} {
		completed[key] = int64(0)
	}
	if got, err := storageMaintenanceResult(completed); err != nil || got.Count != 0 || got.BatchSize == nil {
		t.Fatalf("zero completed = %#v, %v", got, err)
	}
	for _, bad := range []map[string]any{
		{"status": "not_due", "last_maintenance_at": "bad"},
		{"status": "completed"},
		{"status": "completed", "batch_size": -1},
	} {
		if _, err := storageMaintenanceResult(bad); err == nil {
			t.Errorf("malformed maintenance result accepted: %#v", bad)
		}
	}
	if got, err := partitionMaintenanceResult(StoragePartitionBillingEventPayloads, map[string]any{"status": "busy"}); err != nil || got.ParentTable != StoragePartitionBillingEventPayloads || !got.HasMore {
		t.Fatalf("busy partition = %#v, %v", got, err)
	}
	validPartition := map[string]any{"status": "completed", "parent_table": string(StoragePartitionBillingEventPayloads), "partitions_created": 0, "partitions_dropped": 0, "partition_lock_timeouts": 0, "default_partition_has_rows": true, "has_more": false}
	if got, err := partitionMaintenanceResult(StoragePartitionBillingEventPayloads, validPartition); err != nil || got.DefaultPartitionHasRows != true {
		t.Fatalf("zero partition = %#v, %v", got, err)
	}
	for _, bad := range []map[string]any{
		{"status": "failed", "parent_table": string(StoragePartitionBillingEventPayloads)},
		{"status": "completed"},
		{"status": "completed", "parent_table": string(StoragePartitionBillingEventPayloads), "partitions_created": -1},
	} {
		if _, err := partitionMaintenanceResult(StoragePartitionBillingEventPayloads, bad); err == nil {
			t.Errorf("malformed partition result accepted: %#v", bad)
		}
	}
}
