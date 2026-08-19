package bursar

import (
	"context"
	"encoding/json"
	"errors"
	"sync"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgtype"
)

const (
	storageTestTenant                = "11111111-1111-4111-8111-111111111111"
	storageOtherTenant               = "22222222-2222-4222-8222-222222222222"
	storageTestClaim                 = "33333333-3333-4333-8333-333333333333"
	storageTestCharge                = "44444444-4444-4444-8444-444444444444"
	storageTestBilling               = "55555555-5555-4555-8555-555555555555"
	storageTestAccount               = "66666666-6666-4666-8666-666666666666"
	storageTestSubject               = "77777777-7777-4777-8777-777777777777"
	storageTestBillingWithoutVersion = "88888888-8888-4888-8888-888888888888"
)

type storageCallerStub struct {
	tenantID string
	call     func(context.Context, string, ...any) ([]map[string]any, error)

	mu    sync.Mutex
	names []string
	args  [][]any
}

func (s *storageCallerStub) TenantID() string { return s.tenantID }

func (s *storageCallerStub) Call(ctx context.Context, name string, args ...any) ([]map[string]any, error) {
	s.mu.Lock()
	s.names = append(s.names, name)
	s.args = append(s.args, append([]any(nil), args...))
	s.mu.Unlock()
	if s.call == nil {
		return nil, nil
	}
	return s.call(ctx, name, args...)
}

func TestPostgresStorageRepositoryMapsRecoveryRPCs(t *testing.T) {
	now := time.Date(2026, 8, 13, 1, 2, 3, 0, time.UTC)
	caller := &storageCallerStub{tenantID: storageTestTenant}
	caller.call = func(_ context.Context, name string, _ ...any) ([]map[string]any, error) {
		switch name {
		case "claim_outbox_events":
			return []map[string]any{{
				"event_id": int64(7), "tenant_id": storageTestTenant, "topic": "usage.charge_recorded",
				"aggregate_type": "usage_charge", "aggregate_id": storageTestCharge,
				"payload_version": int16(1), "payload": json.RawMessage(`{"charge_id":"x"}`),
				"claim_token": storageTestClaim, "attempt_count": int32(2), "created_at": now,
			}}, nil
		case "complete_tenant_outbox_event", "renew_tenant_outbox_claim", "fail_tenant_outbox_event", "requeue_outbox_dead_letter":
			return []map[string]any{{"result": true}}, nil
		case "get_outbox_stats":
			return []map[string]any{{
				"pending_count": int64(2), "processing_count": int64(1), "delivered_count": int64(9),
				"dead_letter_count": int64(3), "oldest_pending_at": now,
			}}, nil
		case "list_outbox_dead_letters":
			return []map[string]any{
				deadLetterRow("8", now),
				deadLetterRow("9", now.Add(time.Second)),
			}, nil
		default:
			t.Fatalf("unexpected RPC %q", name)
			return nil, nil
		}
	}
	repository, err := newPostgresStorageRepository(caller)
	if err != nil {
		t.Fatal(err)
	}
	events, err := repository.Claim(context.Background(), []string{"usage.charge_recorded"}, 10, 60)
	if err != nil {
		t.Fatal(err)
	}
	if len(events) != 1 || events[0].EventID != "7" || events[0].AttemptCount != 2 || events[0].Payload["charge_id"] != "x" {
		t.Fatalf("events = %#v", events)
	}
	if completed, err := repository.Complete(context.Background(), events[0]); err != nil || !completed {
		t.Fatalf("complete=%v err=%v", completed, err)
	}
	if renewed, err := repository.Renew(context.Background(), events[0], 60); err != nil || !renewed {
		t.Fatalf("renew=%v err=%v", renewed, err)
	}
	if failed, err := repository.Fail(context.Background(), events[0], "outbox_delivery_failed:Error", 30, 10); err != nil || !failed {
		t.Fatalf("fail=%v err=%v", failed, err)
	}
	stats, err := repository.Stats(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if stats.PendingCount != 2 || stats.DeadLetterCount != 3 || stats.OldestPendingAt == nil || !stats.OldestPendingAt.Equal(now) {
		t.Fatalf("stats = %#v", stats)
	}
	page, err := repository.ListDeadLetters(context.Background(), OutboxDeadLetterListOptions{Limit: 1})
	if err != nil {
		t.Fatal(err)
	}
	if len(page.Items) != 1 || page.Items[0].EventID != "8" || page.NextCursor == nil || page.NextCursor.EventID != "8" {
		t.Fatalf("page = %#v", page)
	}
	if _, err := repository.ListDeadLetters(context.Background(), OutboxDeadLetterListOptions{
		Limit: 1, Cursor: &OutboxDeadLetterCursor{CreatedAt: now, EventID: "8"},
	}); err != nil {
		t.Fatalf("ListDeadLetters(cursor) error = %v", err)
	}
	if requeued, err := repository.Requeue(context.Background(), "8"); err != nil || !requeued {
		t.Fatalf("requeue=%v err=%v", requeued, err)
	}
	claimArgs := storageCallArguments(t, caller, "claim_outbox_events", 0)
	requireStorageUUIDArgument(t, claimArgs[0], storageTestTenant)
	if claimArgs[1] != 10 || claimArgs[2] != 60 {
		t.Fatalf("claim scalar args = %#v", claimArgs)
	}
	completeArgs := storageCallArguments(t, caller, "complete_tenant_outbox_event", 0)
	requireStorageUUIDArgument(t, completeArgs[0], storageTestTenant)
	if completeArgs[1] != int64(7) {
		t.Fatalf("complete event ID arg = %#v (%T)", completeArgs[1], completeArgs[1])
	}
	requireStorageUUIDArgument(t, completeArgs[2], storageTestClaim)
	renewArgs := storageCallArguments(t, caller, "renew_tenant_outbox_claim", 0)
	requireStorageUUIDArgument(t, renewArgs[0], storageTestTenant)
	if renewArgs[1] != int64(7) {
		t.Fatalf("renew event ID arg = %#v (%T)", renewArgs[1], renewArgs[1])
	}
	requireStorageUUIDArgument(t, renewArgs[2], storageTestClaim)
	failArgs := storageCallArguments(t, caller, "fail_tenant_outbox_event", 0)
	requireStorageUUIDArgument(t, failArgs[0], storageTestTenant)
	if failArgs[1] != int64(7) {
		t.Fatalf("fail event ID arg = %#v (%T)", failArgs[1], failArgs[1])
	}
	requireStorageUUIDArgument(t, failArgs[2], storageTestClaim)
	statsArgs := storageCallArguments(t, caller, "get_outbox_stats", 0)
	requireStorageUUIDArgument(t, statsArgs[0], storageTestTenant)
	listArgs := storageCallArguments(t, caller, "list_outbox_dead_letters", 0)
	requireStorageUUIDArgument(t, listArgs[0], storageTestTenant)
	if cursorAt, ok := listArgs[1].(pgtype.Timestamptz); !ok || cursorAt.Valid {
		t.Fatalf("null cursor timestamp arg = %#v (%T)", listArgs[1], listArgs[1])
	}
	if cursorID, ok := listArgs[2].(pgtype.Int8); !ok || cursorID.Valid {
		t.Fatalf("null cursor event ID arg = %#v (%T)", listArgs[2], listArgs[2])
	}
	cursorArgs := storageCallArguments(t, caller, "list_outbox_dead_letters", 1)
	if cursorAt, ok := cursorArgs[1].(pgtype.Timestamptz); !ok || !cursorAt.Valid || !cursorAt.Time.Equal(now) {
		t.Fatalf("cursor timestamp arg = %#v (%T)", cursorArgs[1], cursorArgs[1])
	}
	if cursorID, ok := cursorArgs[2].(pgtype.Int8); !ok || !cursorID.Valid || cursorID.Int64 != 8 {
		t.Fatalf("cursor event ID arg = %#v (%T)", cursorArgs[2], cursorArgs[2])
	}
	requeueArgs := storageCallArguments(t, caller, "requeue_outbox_dead_letter", 0)
	requireStorageUUIDArgument(t, requeueArgs[0], storageTestTenant)
	if requeueArgs[1] != int64(8) {
		t.Fatalf("requeue event ID arg = %#v (%T)", requeueArgs[1], requeueArgs[1])
	}
}

func TestPostgresStorageRepositoryExportsExactAmountsAndArchives(t *testing.T) {
	now := time.Date(2026, 8, 13, 1, 2, 3, 0, time.UTC)
	caller := &storageCallerStub{tenantID: storageTestTenant}
	caller.call = func(_ context.Context, name string, _ ...any) ([]map[string]any, error) {
		switch name {
		case "export_usage_charge":
			return []map[string]any{{"payload": map[string]any{
				"payload_available": true, "tenant_id": storageTestTenant, "charge_id": storageTestCharge,
				"account_id": storageTestAccount, "subject_id": storageTestSubject, "operation": "generate",
				"feature": nil, "model": "gpt", "region": nil,
				"measures": map[string]any{"tokens": "10"}, "dimensions": map[string]any{}, "metadata": map[string]any{},
				"requested": "1.234567", "charged": "1.200000", "allowance_requested": "0.034567",
				"allowance_covered": "0.034567", "billing_disposition": "billable",
				"catalog_revision_id": nil, "plan_id": nil, "rate_card_key": "standard",
				"pricing_snapshot": map[string]any{}, "ledger_entry_id": nil, "correction_of_charge_id": nil,
				"idempotency_key": "usage-1", "request_digest": "digest", "event_at": now, "created_at": now,
			}}}, nil
		case "export_billing_event_payload":
			return []map[string]any{{"payload": map[string]any{
				"tenant_id": storageTestTenant, "event_id": storageTestBilling, "provider": "stripe",
				"provider_environment": "test", "provider_event_id": "evt_1", "event_type": "invoice.paid",
				"status": "completed", "received_at": now, "completed_at": now, "envelope": map[string]any{"id": "evt_1"},
				"object_key": nil, "object_version": nil, "archived_at": nil,
			}}}, nil
		case "archive_billing_event_payload":
			return []map[string]any{{"result": true}}, nil
		default:
			t.Fatalf("unexpected RPC %q", name)
			return nil, nil
		}
	}
	repository, err := newPostgresStorageRepository(caller)
	if err != nil {
		t.Fatal(err)
	}
	usage, err := repository.GetUsageCharge(context.Background(), storageTestCharge)
	if err != nil {
		t.Fatal(err)
	}
	if usage == nil || usage.Requested.StringFixed(MoneyDecimalPlaces) != "1.234567" || usage.Charged.StringFixed(MoneyDecimalPlaces) != "1.200000" {
		t.Fatalf("usage = %#v", usage)
	}
	billing, err := repository.GetBillingEventPayload(context.Background(), storageTestBilling)
	if err != nil {
		t.Fatal(err)
	}
	if billing == nil || billing.ProviderEventID != "evt_1" || billing.Envelope["id"] != "evt_1" {
		t.Fatalf("billing = %#v", billing)
	}
	version := "v1"
	recorded, err := repository.ArchiveBillingEventPayload(context.Background(), storageTestBilling, "tenant/event.json", &version)
	if err != nil || !recorded {
		t.Fatalf("recorded=%v err=%v", recorded, err)
	}
	recorded, err = repository.ArchiveBillingEventPayload(context.Background(), storageTestBillingWithoutVersion, "tenant/event-without-version.json", nil)
	if err != nil || !recorded {
		t.Fatalf("recorded without version=%v err=%v", recorded, err)
	}
	exportUsageArgs := storageCallArguments(t, caller, "export_usage_charge", 0)
	requireStorageUUIDArgument(t, exportUsageArgs[0], storageTestCharge)
	exportBillingArgs := storageCallArguments(t, caller, "export_billing_event_payload", 0)
	requireStorageUUIDArgument(t, exportBillingArgs[0], storageTestBilling)
	archiveArgs := storageCallArguments(t, caller, "archive_billing_event_payload", 0)
	requireStorageUUIDArgument(t, archiveArgs[0], storageTestBilling)
	if versionArg, ok := archiveArgs[2].(pgtype.Text); !ok || !versionArg.Valid || versionArg.String != "v1" {
		t.Fatalf("archive version arg = %#v (%T)", archiveArgs[2], archiveArgs[2])
	}
	nullArchiveArgs := storageCallArguments(t, caller, "archive_billing_event_payload", 1)
	requireStorageUUIDArgument(t, nullArchiveArgs[0], storageTestBillingWithoutVersion)
	if versionArg, ok := nullArchiveArgs[2].(pgtype.Text); !ok || versionArg.Valid {
		t.Fatalf("null archive version arg = %#v (%T)", nullArchiveArgs[2], nullArchiveArgs[2])
	}
}

func TestPostgresStorageRepositoryRejectsTenantAndUnsafeDiagnostics(t *testing.T) {
	repository, err := newPostgresStorageRepository(&storageCallerStub{tenantID: storageTestTenant})
	if err != nil {
		t.Fatal(err)
	}
	event := OutboxEvent{EventID: "1", TenantID: storageOtherTenant, ClaimToken: storageTestClaim}
	if _, err := repository.Complete(context.Background(), event); err == nil {
		t.Fatal("cross-tenant completion was accepted")
	}
	event.TenantID = storageTestTenant
	if _, err := repository.Fail(context.Background(), event, "raw secret URL https://example.test", 30, 10); err == nil {
		t.Fatal("unsafe diagnostic was accepted")
	}
	if _, err := newPostgresStorageRepository(&storageCallerStub{}); err == nil {
		t.Fatal("unscoped client was accepted")
	}
	event.EventID = "9223372036854775808"
	if _, err := repository.Complete(context.Background(), event); err == nil {
		t.Fatal("outbox event ID beyond PostgreSQL bigint was accepted")
	}
}

func deadLetterRow(eventID string, createdAt time.Time) map[string]any {
	lastError := "outbox_delivery_failed:Error"
	return map[string]any{
		"event_id": eventID, "tenant_id": storageTestTenant, "topic": "usage.charge_recorded",
		"aggregate_type": "usage_charge", "aggregate_id": storageTestCharge,
		"payload_version": 1, "attempt_count": 10, "last_error": lastError,
		"created_at": createdAt, "updated_at": createdAt,
	}
}

func storageCallArguments(t *testing.T, caller *storageCallerStub, name string, occurrence int) []any {
	t.Helper()
	caller.mu.Lock()
	defer caller.mu.Unlock()
	seen := 0
	for index, callName := range caller.names {
		if callName != name {
			continue
		}
		if seen == occurrence {
			return append([]any(nil), caller.args[index]...)
		}
		seen++
	}
	t.Fatalf("RPC %s occurrence %d was not called", name, occurrence)
	return nil
}

func requireStorageUUIDArgument(t *testing.T, argument any, expected string) {
	t.Helper()
	value, ok := argument.(pgtype.UUID)
	if !ok || !value.Valid || formatUUID(value.Bytes) != expected {
		t.Fatalf("UUID arg = %#v (%T), want %s", argument, argument, expected)
	}
}

func TestPostgresStorageRepositoryConstructorsAndRPCFailures(t *testing.T) {
	if _, err := NewPostgresStorageRepository(nil); err == nil {
		t.Fatal("nil PostgreSQL client accepted")
	}
	if _, err := NewPostgresStorageRepositoryFromStore(nil); err == nil {
		t.Fatal("nil PostgreSQL store accepted")
	}
	if _, err := newPostgresStorageRepository(&storageCallerStub{tenantID: "not-a-uuid"}); err == nil {
		t.Fatal("invalid tenant PostgreSQL client accepted")
	}
	client := &PostgresClient{options: PostgresClientOptions{TenantID: storageTestTenant}}
	repository, err := NewPostgresStorageRepository(client)
	if err != nil {
		t.Fatal(err)
	}
	if repository.tenantID != storageTestTenant {
		t.Fatalf("repository tenant = %q", repository.tenantID)
	}
	store := &PostgresStore{client: client}
	if fromStore, err := NewPostgresStorageRepositoryFromStore(store); err != nil || fromStore.tenantID != storageTestTenant {
		t.Fatalf("repository from store = %#v, %v", fromStore, err)
	}

	caller := &storageCallerStub{tenantID: storageTestTenant, call: func(context.Context, string, ...any) ([]map[string]any, error) {
		return nil, errors.New("database password must not escape")
	}}
	repository, err = newPostgresStorageRepository(caller)
	if err != nil {
		t.Fatal(err)
	}
	event := OutboxEvent{EventID: "1", TenantID: storageTestTenant, ClaimToken: storageTestClaim}
	if _, err := repository.Claim(context.Background(), []string{"usage"}, 1, 1); err == nil {
		t.Fatal("claim call error was swallowed")
	}
	if _, err := repository.Renew(context.Background(), event, 1); err == nil {
		t.Fatal("renew call error was swallowed")
	}
	if _, err := repository.Complete(context.Background(), event); err == nil {
		t.Fatal("complete call error was swallowed")
	}
	if _, err := repository.Fail(context.Background(), event, "outbox_delivery_failed:Error", 1, 1); err == nil {
		t.Fatal("fail call error was swallowed")
	}
	if _, err := repository.Stats(context.Background()); err == nil {
		t.Fatal("stats call error was swallowed")
	}
	if _, err := repository.ListDeadLetters(context.Background(), OutboxDeadLetterListOptions{Limit: 1}); err == nil {
		t.Fatal("dead-letter call error was swallowed")
	}
	if _, err := repository.Requeue(context.Background(), "1"); err == nil {
		t.Fatal("requeue call error was swallowed")
	}
	if _, err := repository.GetUsageCharge(context.Background(), storageTestCharge); err == nil {
		t.Fatal("usage export call error was swallowed")
	}
	if _, err := repository.GetBillingEventPayload(context.Background(), storageTestBilling); err == nil {
		t.Fatal("billing export call error was swallowed")
	}
	if _, err := repository.ArchiveBillingEventPayload(context.Background(), storageTestBilling, "object", nil); err == nil {
		t.Fatal("archive call error was swallowed")
	}
}

func TestPostgresStorageRepositoryRejectsMalformedPersistenceResponses(t *testing.T) {
	caller := &storageCallerStub{tenantID: storageTestTenant}
	repository, err := newPostgresStorageRepository(caller)
	if err != nil {
		t.Fatal(err)
	}
	event := OutboxEvent{EventID: "1", TenantID: storageTestTenant, ClaimToken: storageTestClaim}

	for _, tc := range []struct {
		name string
		call func() error
	}{
		{"claim tenant mismatch", func() error {
			caller.call = func(context.Context, string, ...any) ([]map[string]any, error) {
				row := map[string]any{"event_id": "1", "tenant_id": storageOtherTenant, "topic": "usage", "aggregate_type": "charge", "aggregate_id": storageTestCharge, "payload_version": 1, "payload": map[string]any{}, "claim_token": storageTestClaim, "attempt_count": 0, "created_at": time.Now()}
				return []map[string]any{row}, nil
			}
			_, err := repository.Claim(context.Background(), []string{"usage"}, 1, 1)
			return err
		}},
		{"claim malformed row", func() error {
			caller.call = func(context.Context, string, ...any) ([]map[string]any, error) {
				return []map[string]any{{"event_id": "1", "tenant_id": storageTestTenant}}, nil
			}
			_, err := repository.Claim(context.Background(), []string{"usage"}, 1, 1)
			return err
		}},
		{"renew scalar malformed", func() error {
			caller.call = func(context.Context, string, ...any) ([]map[string]any, error) {
				return []map[string]any{{"result": "not-bool", "extra": true}}, nil
			}
			_, err := repository.Renew(context.Background(), event, 1)
			return err
		}},
		{"complete missing scalar", func() error {
			caller.call = func(context.Context, string, ...any) ([]map[string]any, error) { return []map[string]any{{}}, nil }
			_, err := repository.Complete(context.Background(), event)
			return err
		}},
		{"fail empty rows", func() error {
			caller.call = func(context.Context, string, ...any) ([]map[string]any, error) { return nil, nil }
			_, err := repository.Fail(context.Background(), event, "outbox_delivery_failed:Error", 1, 1)
			return err
		}},
		{"stats negative", func() error {
			caller.call = func(context.Context, string, ...any) ([]map[string]any, error) {
				return []map[string]any{{"pending_count": -1, "processing_count": 0, "delivered_count": 0, "dead_letter_count": 0}}, nil
			}
			_, err := repository.Stats(context.Background())
			return err
		}},
		{"dead letter tenant mismatch", func() error {
			caller.call = func(context.Context, string, ...any) ([]map[string]any, error) {
				row := deadLetterRow("1", time.Now())
				row["tenant_id"] = storageOtherTenant
				return []map[string]any{row}, nil
			}
			_, err := repository.ListDeadLetters(context.Background(), OutboxDeadLetterListOptions{Limit: 1})
			return err
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if err := tc.call(); err == nil {
				t.Fatal("malformed response accepted")
			}
		})
	}

	for _, topics := range [][]string{nil, {""}, {string(make([]byte, 256))}} {
		if err := validateOutboxClaim(topics, 1, 1); err == nil {
			t.Errorf("invalid topics accepted: %#v", topics)
		}
	}
	for _, tc := range []struct {
		name string
		call func() error
	}{
		{"invalid lease", func() error { _, err := repository.Renew(context.Background(), event, 0); return err }},
		{"invalid retry delay", func() error {
			_, err := repository.Fail(context.Background(), event, "outbox_delivery_failed:Error", 0, 1)
			return err
		}},
		{"invalid attempt limit", func() error {
			_, err := repository.Fail(context.Background(), event, "outbox_delivery_failed:Error", 1, 0)
			return err
		}},
		{"invalid archive key", func() error {
			_, err := repository.ArchiveBillingEventPayload(context.Background(), storageTestBilling, " ", nil)
			return err
		}},
		{"invalid dead-letter limit", func() error {
			_, err := repository.ListDeadLetters(context.Background(), OutboxDeadLetterListOptions{Limit: 101})
			return err
		}},
		{"zero cursor ID", func() error {
			_, err := repository.ListDeadLetters(context.Background(), OutboxDeadLetterListOptions{Limit: 1, Cursor: &OutboxDeadLetterCursor{CreatedAt: time.Now(), EventID: "0"}})
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

func TestPostgresStorageRepositoryExportAvailabilityAndTenantChecks(t *testing.T) {
	caller := &storageCallerStub{tenantID: storageTestTenant}
	repository, err := newPostgresStorageRepository(caller)
	if err != nil {
		t.Fatal(err)
	}
	caller.call = func(_ context.Context, name string, _ ...any) ([]map[string]any, error) {
		switch name {
		case "export_usage_charge":
			return []map[string]any{{"payload": map[string]any{"payload_available": false}}}, nil
		case "export_billing_event_payload":
			return nil, nil
		case "archive_billing_event_payload":
			return []map[string]any{{"result": false}}, nil
		default:
			return nil, nil
		}
	}
	if _, err := repository.GetUsageCharge(context.Background(), storageTestCharge); err == nil {
		t.Fatal("expired usage payload accepted")
	}
	if got, err := repository.GetBillingEventPayload(context.Background(), storageTestBilling); err != nil || got != nil {
		t.Fatalf("missing billing payload = %#v, %v", got, err)
	}
	if got, err := repository.ArchiveBillingEventPayload(context.Background(), storageTestBilling, "object", nil); err != nil || got {
		t.Fatalf("false archive result = %v, %v", got, err)
	}

	caller.call = func(_ context.Context, name string, _ ...any) ([]map[string]any, error) {
		if name == "export_billing_event_payload" {
			payload := validBillingExportPayload(time.Now().UTC())
			payload["tenant_id"] = storageOtherTenant
			return []map[string]any{{"payload": payload}}, nil
		}
		payload := validUsageExportPayload(time.Now().UTC())
		payload["tenant_id"] = storageOtherTenant
		return []map[string]any{{"payload": payload}}, nil
	}
	if _, err := repository.GetBillingEventPayload(context.Background(), storageTestBilling); err == nil {
		t.Fatal("cross-tenant billing payload accepted")
	}
	if _, err := repository.GetUsageCharge(context.Background(), storageTestCharge); err == nil {
		t.Fatal("cross-tenant usage payload accepted")
	}
}

func validUsageExportPayload(now time.Time) map[string]any {
	return map[string]any{
		"payload_available": true, "tenant_id": storageTestTenant, "charge_id": storageTestCharge, "account_id": storageTestAccount, "subject_id": storageTestSubject, "operation": "generate",
		"measures": map[string]any{}, "dimensions": map[string]any{}, "metadata": map[string]any{}, "requested": "1", "charged": "1", "allowance_requested": "0", "allowance_covered": "0", "billing_disposition": "billable", "pricing_snapshot": map[string]any{},
		"idempotency_key": "idempotency", "request_digest": "digest", "event_at": now, "created_at": now,
	}
}

func validBillingExportPayload(now time.Time) map[string]any {
	return map[string]any{
		"tenant_id": storageTestTenant, "event_id": storageTestBilling, "provider": "stripe", "provider_environment": "test", "provider_event_id": "evt", "event_type": "invoice.paid", "status": "completed", "received_at": now,
		"completed_at": nil, "archived_at": nil, "envelope": map[string]any{},
	}
}

func TestStorageExportRowMappersFailClosed(t *testing.T) {
	now := time.Now().UTC()
	usage := validUsageExportPayload(now)
	for _, key := range []string{"tenant_id", "charge_id", "account_id", "subject_id", "operation", "requested", "charged", "allowance_requested", "allowance_covered", "billing_disposition", "measures", "dimensions", "metadata", "pricing_snapshot", "idempotency_key", "request_digest", "event_at", "created_at"} {
		row := cloneAnyMap(usage)
		delete(row, key)
		if _, err := usageChargeExportFromPayload(row); err == nil {
			t.Errorf("usage export missing %s accepted", key)
		}
	}
	for _, key := range []string{"tenant_id", "event_id", "provider", "provider_environment", "provider_event_id", "event_type", "status", "received_at"} {
		row := cloneAnyMap(validBillingExportPayload(now))
		delete(row, key)
		if _, err := billingPayloadExportFromPayload(row); err == nil {
			t.Errorf("billing export missing %s accepted", key)
		}
	}
	for _, row := range []map[string]any{
		func() map[string]any { row := cloneAnyMap(usage); row["billing_disposition"] = "unknown"; return row }(),
		func() map[string]any { row := cloneAnyMap(usage); row["charge_id"] = "bad"; return row }(),
		func() map[string]any { row := cloneAnyMap(usage); row["requested"] = "not-money"; return row }(),
		func() map[string]any { row := cloneAnyMap(usage); row["measures"] = "not-json"; return row }(),
	} {
		if _, err := usageChargeExportFromPayload(row); err == nil {
			t.Errorf("malformed usage export accepted: %#v", row)
		}
	}
	for _, row := range []map[string]any{
		func() map[string]any {
			row := cloneAnyMap(validBillingExportPayload(now))
			row["envelope"] = "not-json"
			return row
		}(),
		func() map[string]any {
			row := cloneAnyMap(validBillingExportPayload(now))
			row["received_at"] = "bad"
			return row
		}(),
	} {
		if _, err := billingPayloadExportFromPayload(row); err == nil {
			t.Errorf("malformed billing export accepted: %#v", row)
		}
	}
	invalidBilling := validBillingExportPayload(now)
	invalidBilling["event_id"] = "bad"
	if _, err := billingPayloadExportFromPayload(invalidBilling); err == nil {
		t.Fatal("invalid billing event UUID accepted")
	}
}

func TestPostgresStorageRepositoryPersistenceShapeBoundaries(t *testing.T) {
	caller := &storageCallerStub{tenantID: storageTestTenant}
	repository, err := newPostgresStorageRepository(caller)
	if err != nil {
		t.Fatal(err)
	}
	event := OutboxEvent{EventID: "1", TenantID: storageTestTenant, ClaimToken: storageTestClaim}
	for _, lease := range []int{0, 3_601} {
		if _, err := repository.Renew(context.Background(), event, lease); err == nil {
			t.Errorf("lease %d accepted", lease)
		}
	}

	validStats := map[string]any{"pending_count": 1, "processing_count": 2, "delivered_count": 3, "dead_letter_count": 4, "oldest_pending_at": time.Now().UTC()}
	for _, response := range [][]map[string]any{nil, {{"pending_count": 1}, {"pending_count": 1}}} {
		caller.call = func(context.Context, string, ...any) ([]map[string]any, error) { return response, nil }
		if _, err := repository.Stats(context.Background()); err == nil {
			t.Errorf("invalid stats row count accepted: %#v", response)
		}
	}
	for _, key := range []string{"pending_count", "processing_count", "delivered_count", "dead_letter_count", "oldest_pending_at"} {
		row := cloneAnyMap(validStats)
		if key == "oldest_pending_at" {
			row[key] = "bad"
		} else {
			delete(row, key)
		}
		caller.call = func(context.Context, string, ...any) ([]map[string]any, error) { return []map[string]any{row}, nil }
		if _, err := repository.Stats(context.Background()); err == nil {
			t.Errorf("malformed stats field %s accepted", key)
		}
	}

	for _, response := range [][]map[string]any{
		{{}},
		{{"payload": "secret"}},
		{{"payload": map[string]any{}}},
	} {
		caller.call = func(context.Context, string, ...any) ([]map[string]any, error) { return response, nil }
		if _, err := repository.GetUsageCharge(context.Background(), storageTestCharge); err == nil {
			t.Errorf("malformed usage export response accepted: %#v", response)
		}
	}
	for _, response := range [][]map[string]any{
		{{}},
		{{"payload": "secret"}},
		{{"payload": map[string]any{}}},
	} {
		caller.call = func(context.Context, string, ...any) ([]map[string]any, error) { return response, nil }
		if _, err := repository.GetBillingEventPayload(context.Background(), storageTestBilling); err == nil {
			t.Errorf("malformed billing export response accepted: %#v", response)
		}
	}

	validEvent := map[string]any{"event_id": "1", "tenant_id": storageTestTenant, "topic": "usage", "aggregate_type": "charge", "aggregate_id": storageTestCharge, "payload_version": 1, "payload": map[string]any{}, "claim_token": storageTestClaim, "attempt_count": 0, "created_at": time.Now().UTC()}
	for _, key := range []string{"event_id", "tenant_id", "topic", "aggregate_type", "aggregate_id", "payload_version", "payload", "claim_token", "attempt_count", "created_at"} {
		row := cloneAnyMap(validEvent)
		delete(row, key)
		if _, err := outboxEventFromRow(row); err == nil {
			t.Errorf("outbox event missing %s accepted", key)
		}
	}
	validLetter := deadLetterRow("1", time.Now().UTC())
	for _, key := range []string{"event_id", "tenant_id", "topic", "aggregate_type", "aggregate_id", "payload_version", "attempt_count", "created_at", "updated_at"} {
		row := cloneAnyMap(validLetter)
		delete(row, key)
		if _, err := outboxDeadLetterFromRow(row); err == nil {
			t.Errorf("dead letter missing %s accepted", key)
		}
	}
}
