package bursar

import (
	"context"
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
