package bursar

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"testing"
	"time"
)

func TestStoragePostgresDecodingAndValidationBoundaries(t *testing.T) {
	for _, tc := range []struct {
		name         string
		topics       []string
		limit, lease int
		valid        bool
	}{
		{"valid", []string{"usage"}, 10, 60, true},
		{"empty topic", nil, 10, 60, false},
		{"zero limit", []string{"usage"}, 0, 60, false},
		{"large limit", []string{"usage"}, 10001, 60, false},
		{"zero lease", []string{"usage"}, 10, 0, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			err := validateOutboxClaim(tc.topics, tc.limit, tc.lease)
			if (err == nil) != tc.valid {
				t.Fatalf("validateOutboxClaim() = %v, valid=%v", err, tc.valid)
			}
		})
	}
	validRow := map[string]any{
		"event_id": int64(7), "tenant_id": storageTestTenant, "topic": "usage", "aggregate_type": "charge", "aggregate_id": storageTestCharge,
		"payload_version": 1, "payload": json.RawMessage(`{"charge_id":"c"}`), "claim_token": storageTestClaim, "attempt_count": 2,
		"created_at": time.Now().UTC(),
	}
	event, err := outboxEventFromRow(validRow)
	if err != nil || event.EventID != "7" || event.Payload["charge_id"] != "c" {
		t.Fatalf("event=%#v err=%v", event, err)
	}
	for _, key := range []string{"event_id", "tenant_id", "topic", "payload"} {
		row := cloneAnyMap(validRow)
		delete(row, key)
		if _, err := outboxEventFromRow(row); err == nil {
			t.Errorf("missing %s accepted", key)
		}
	}
	if _, err := outboxDeadLetterFromRow(map[string]any{"event_id": "bad", "tenant_id": storageTestTenant}); err == nil {
		t.Fatal("malformed dead letter accepted")
	}
	if _, err := requiredJSONMap("secret", "payload"); err == nil {
		t.Fatal("scalar JSON accepted as map")
	}
	if _, err := exactlyOneRow(nil, "operation"); err == nil {
		t.Fatal("empty row result accepted")
	}
	if _, err := exactlyOneRow([]map[string]any{{}, {}}, "operation"); err == nil {
		t.Fatal("multiple row result accepted")
	}
	for _, value := range []string{"", "-1", "abc"} {
		if _, err := positiveEventID(value); err == nil {
			t.Errorf("positiveEventID(%q) accepted", value)
		}
	}
	if _, err := postgresOutboxEventID("9223372036854775808"); err == nil {
		t.Fatal("outbox bigint overflow accepted")
	}
	if got, err := normalizedUUID(storageTestTenant, "tenant"); err != nil || got != storageTestTenant {
		t.Fatalf("normalized UUID = %q, %v", got, err)
	}
	if _, err := normalizedUUID("not-a-uuid", "tenant"); err == nil {
		t.Fatal("invalid UUID accepted")
	}
	if _, err := optionalNormalizedUUIDPointer(map[string]any{"id": "not-a-uuid"}, "id", "id"); err == nil {
		t.Fatal("invalid optional UUID accepted")
	}
}

func TestStorageMaintenanceResultAndNumericDecoding(t *testing.T) {
	if got, err := storageMaintenanceResult(map[string]any{"status": string(StorageMaintenanceBusy)}); err != nil || got.Status != StorageMaintenanceBusy || !got.HasMore {
		t.Fatalf("busy = %#v, %v", got, err)
	}
	if got, err := storageMaintenanceResult(map[string]any{"status": string(StorageMaintenanceNotDue)}); err != nil || got.Status != StorageMaintenanceNotDue {
		t.Fatalf("not due = %#v, %v", got, err)
	}
	if _, err := storageMaintenanceResult(map[string]any{"status": "unknown"}); err == nil {
		t.Fatal("unknown maintenance status accepted")
	}
	if got, err := partitionMaintenanceResult(StoragePartitionUsageChargePayloads, map[string]any{"status": string(PartitionMaintenanceBusy)}); err != nil || got.Status != PartitionMaintenanceBusy {
		t.Fatalf("partition busy = %#v, %v", got, err)
	}
	if _, err := partitionMaintenanceResult(StoragePartitionUsageChargePayloads, map[string]any{"status": "completed", "parent_table": "wrong"}); err == nil {
		t.Fatal("wrong partition accepted")
	}
	for _, value := range []any{float64(1), json.Number("2"), int64(3), float64(-1), json.Number("bad"), "bad"} {
		got, err := maintenanceNonnegativeInt(map[string]any{"value": value}, "value", "maintenance")
		if value == float64(1) || value == json.Number("2") || value == int64(3) {
			if err != nil || got < 1 {
				t.Errorf("numeric %v = %d, %v", value, got, err)
			}
		} else if err == nil {
			t.Errorf("invalid numeric %v accepted", value)
		}
	}
	if got, err := optionalJSONTime(map[string]any{}, "at", "maintenance"); err != nil || got != nil {
		t.Fatalf("missing time = %v, %v", got, err)
	}
	if _, err := optionalJSONTime(map[string]any{"at": "bad"}, "at", "maintenance"); err == nil {
		t.Fatal("bad time accepted")
	}
	if _, err := NewBursarMaintenanceFromStore(nil, nil); err == nil {
		t.Fatal("nil store maintenance accepted")
	}
}

func TestStorageHandlerTopicsAndOutboxDiagnosticBoundaries(t *testing.T) {
	if _, err := NewUsageChargeOutboxHandler(nil, nil); err == nil {
		t.Fatal("nil usage handler dependencies accepted")
	}
	if _, err := NewBillingPayloadOutboxHandler(nil, nil); err == nil {
		t.Fatal("nil billing handler dependencies accepted")
	}
	usage, err := NewUsageChargeOutboxHandler(&projectionRepositoryStub{usage: &UsageChargeExport{TenantID: storageTestTenant, ChargeID: storageTestCharge, Requested: MustAmount("1"), Charged: MustAmount("1")}}, &usageSinkStub{})
	if err != nil || len(usage.Topics()) != 1 {
		t.Fatalf("usage topics = %#v, %v", usage.Topics(), err)
	}
	billing, err := NewBillingPayloadOutboxHandler(&projectionRepositoryStub{}, &billingArchiveStub{})
	if err != nil || len(billing.Topics()) != 2 {
		t.Fatalf("billing topics = %#v, %v", billing.Topics(), err)
	}
	for _, value := range []string{"bad", "a:b:c", "raw secret"} {
		if _, err := validatePersistedDiagnosticSummary(value); err == nil {
			t.Errorf("unsafe summary %q accepted", value)
		}
	}
	if got := sanitizeDiagnosticCode("bad code!", "fallback"); got != "bad_code_" {
		t.Fatalf("sanitized code = %q", got)
	}
	if got := persistedDiagnosticSummary(errors.New("secret"), "raw fallback"); !strings.HasPrefix(got, "raw_fallback:") {
		t.Fatalf("diagnostic = %q", got)
	}
	if err := invokeOutboxHandler(context.Background(), &outboxHandlerStub{handle: func(context.Context, OutboxEvent) error { panic("secret") }}, OutboxEvent{}); err == nil || !strings.Contains(err.Error(), "panicked") {
		t.Fatalf("panic result = %v", err)
	}
}

func TestPostgresClientOptionAndErrorBoundaries(t *testing.T) {
	options, err := (PostgresClientOptions{TenantID: storageTestTenant}).normalized()
	if err != nil || options.AccessRole != PostgresAccessRoleClient || options.ApplicationName != "bursar-go" {
		t.Fatalf("normalized options = %#v, %v", options, err)
	}
	for _, options := range []PostgresClientOptions{{TenantID: "bad"}, {TenantID: storageTestTenant, AccessRole: "bad"}, {TenantID: storageTestTenant, UsageBackend: "bad"}, {TenantID: storageTestTenant, BillingPayloadBackend: "bad"}, {TenantID: storageTestTenant, ConnectionTimeout: -time.Second}} {
		if _, err := options.normalized(); err == nil {
			t.Errorf("invalid options accepted: %#v", options)
		}
	}
	if normalizePostgresError(context.DeadlineExceeded, "query", false) == nil || normalizePostgresError(context.Canceled, "query", false) == nil {
		t.Fatal("context errors were not normalized")
	}
	if normalizePostgresError(nil, "query", false) != nil {
		t.Fatal("nil error changed")
	}
	if got := postgresTimeout(1500 * time.Millisecond); got != "1500" {
		t.Fatal(got)
	}
}

func TestStorageRepositoryLifecycleAndTenantGuards(t *testing.T) {
	caller := &storageCallerStub{tenantID: storageTestTenant}
	caller.call = func(_ context.Context, name string, _ ...any) ([]map[string]any, error) {
		switch name {
		case "claim_outbox_events":
			return []map[string]any{{
				"event_id": int64(7), "tenant_id": storageTestTenant, "topic": "usage",
				"aggregate_type": "charge", "aggregate_id": storageTestCharge, "payload_version": 1,
				"payload": map[string]any{"charge_id": storageTestCharge}, "claim_token": storageTestClaim,
				"attempt_count": 1, "created_at": time.Now().UTC(),
			}}, nil
		case "get_outbox_stats":
			return []map[string]any{{"pending_count": 1, "processing_count": 2, "delivered_count": 3, "dead_letter_count": 4}}, nil
		case "renew_tenant_outbox_claim", "complete_tenant_outbox_event", "fail_tenant_outbox_event", "requeue_outbox_dead_letter", "archive_billing_event_payload":
			return []map[string]any{{"result": true}}, nil
		default:
			return nil, nil
		}
	}
	repository, err := newPostgresStorageRepository(caller)
	if err != nil {
		t.Fatal(err)
	}
	event, err := repository.Claim(context.Background(), []string{"usage"}, 1, 60)
	if err != nil || len(event) != 1 {
		t.Fatalf("Claim() = %#v, %v", event, err)
	}

	claimed := OutboxEvent{EventID: "7", TenantID: storageTestTenant, ClaimToken: storageTestClaim}
	if renewed, err := repository.Renew(context.Background(), claimed, 60); err != nil || !renewed {
		t.Fatalf("Renew() = %v, %v", renewed, err)
	}
	if completed, err := repository.Complete(context.Background(), claimed); err != nil || !completed {
		t.Fatalf("Complete() = %v, %v", completed, err)
	}
	if failed, err := repository.Fail(context.Background(), claimed, "outbox_delivery_failed:Error", 2, 3); err != nil || !failed {
		t.Fatalf("Fail() = %v, %v", failed, err)
	}
	if requeued, err := repository.Requeue(context.Background(), "7"); err != nil || !requeued {
		t.Fatalf("Requeue() = %v, %v", requeued, err)
	}
	stats, err := repository.Stats(context.Background())
	if err != nil || stats.PendingCount != 1 || stats.DeadLetterCount != 4 {
		t.Fatalf("Stats() = %#v, %v", stats, err)
	}
	for _, tc := range []struct {
		name string
		call func() error
	}{
		{"cross-tenant complete", func() error {
			bad := claimed
			bad.TenantID = storageOtherTenant
			_, err := repository.Complete(context.Background(), bad)
			return err
		}},
		{"invalid summary", func() error {
			_, err := repository.Fail(context.Background(), claimed, "raw secret", 2, 3)
			return err
		}},
		{"invalid event id", func() error {
			_, err := repository.Requeue(context.Background(), "0")
			return err
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if err := tc.call(); err == nil {
				t.Fatal("invalid request accepted")
			}
		})
	}
}

func TestStorageHandlerRejectsMalformedAndReplayedEvents(t *testing.T) {
	usageRepository := &wave6ProjectionRepository{usageErr: errors.New("projection unavailable")}
	usage, err := NewUsageChargeOutboxHandler(usageRepository, &wave6UsageSink{})
	if err != nil {
		t.Fatal(err)
	}
	baseUsage := OutboxEvent{TenantID: storageTestTenant, AggregateID: storageTestCharge, PayloadVersion: 1}
	for _, event := range []OutboxEvent{
		{TenantID: storageTestTenant, AggregateID: storageTestCharge, PayloadVersion: 2},
		baseUsage,
	} {
		if err := usage.Handle(context.Background(), event); err == nil {
			t.Fatal("malformed usage event accepted")
		}
	}

	billingRepository := &wave6ProjectionRepository{billing: &BillingEventPayloadExport{TenantID: storageTestTenant, EventID: storageTestBilling}}
	billing, err := NewBillingPayloadOutboxHandler(billingRepository, &wave6BillingArchive{result: BillingPayloadArchiveResult{}})
	if err != nil {
		t.Fatal(err)
	}
	if err := billing.Handle(context.Background(), OutboxEvent{TenantID: storageTestTenant, AggregateID: storageTestBilling, PayloadVersion: 2}); err == nil {
		t.Fatal("unsupported billing version accepted")
	}
	if err := billing.Handle(context.Background(), OutboxEvent{TenantID: storageTestTenant, AggregateID: storageTestBilling, PayloadVersion: 1, Topic: OutboxTopicBillingWebhookCompleted}); err == nil {
		t.Fatal("empty billing archive accepted")
	}

	archived := "already-archived"
	billingRepository.billing.ObjectKey = &archived
	if err := billing.Handle(context.Background(), OutboxEvent{TenantID: storageTestTenant, AggregateID: storageTestBilling, PayloadVersion: 1, Topic: OutboxTopicBillingWebhookCompleted}); err != nil {
		t.Fatalf("replayed billing event = %v", err)
	}
}

type wave6ProjectionRepository struct {
	usage    *UsageChargeExport
	usageErr error
	billing  *BillingEventPayloadExport
}

func (r *wave6ProjectionRepository) GetUsageCharge(context.Context, string) (*UsageChargeExport, error) {
	return r.usage, r.usageErr
}

func (r *wave6ProjectionRepository) GetBillingEventPayload(context.Context, string) (*BillingEventPayloadExport, error) {
	return r.billing, nil
}

func (*wave6ProjectionRepository) ArchiveBillingEventPayload(context.Context, string, string, *string) (bool, error) {
	return true, nil
}

type wave6UsageSink struct{}

func (*wave6UsageSink) WriteUsage(context.Context, UsageChargeExport, string) error { return nil }

type wave6BillingArchive struct{ result BillingPayloadArchiveResult }

func (a *wave6BillingArchive) Archive(context.Context, BillingEventPayloadExport) (BillingPayloadArchiveResult, error) {
	return a.result, nil
}
