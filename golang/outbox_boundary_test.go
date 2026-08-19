package bursar

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"
)

func TestOutboxWorkerRejectsInvalidConstructionAndRuntimeCalls(t *testing.T) {
	handler := &outboxHandlerStub{topics: []string{"usage"}}
	if _, err := NewOutboxWorker(nil, []OutboxHandler{handler}, OutboxWorkerOptions{}); err == nil {
		t.Fatal("nil outbox store accepted")
	}
	if _, err := NewOutboxWorker(&outboxStoreStub{}, nil, OutboxWorkerOptions{}); err == nil {
		t.Fatal("empty outbox handlers accepted")
	}
	for name, invalid := range map[string]OutboxHandler{
		"nil handler":    nil,
		"empty topics":   &outboxHandlerStub{},
		"blank topic":    &outboxHandlerStub{topics: []string{" "}},
		"oversize topic": &outboxHandlerStub{topics: []string{strings.Repeat("x", 256)}},
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := NewOutboxWorker(&outboxStoreStub{}, []OutboxHandler{invalid}, OutboxWorkerOptions{}); err == nil {
				t.Fatal("invalid outbox handler accepted")
			}
		})
	}
	topics := make([]string, 65)
	for index := range topics {
		topics[index] = "topic." + string(rune('A'+index))
	}
	if _, err := NewOutboxWorker(&outboxStoreStub{}, []OutboxHandler{&outboxHandlerStub{topics: topics}}, OutboxWorkerOptions{}); err == nil {
		t.Fatal("more than 64 outbox topics accepted")
	}

	var nilWorker *OutboxWorker
	if err := nilWorker.Start(context.Background()); err == nil {
		t.Fatal("nil outbox worker started")
	}
	if _, err := nilWorker.RunOnce(context.Background()); err == nil {
		t.Fatal("nil outbox worker ran")
	}
	if err := nilWorker.Flush(context.Background()); err == nil {
		t.Fatal("nil outbox worker flushed")
	}
	if err := nilWorker.Health(context.Background()); err == nil {
		t.Fatal("nil outbox worker reported healthy")
	}
	if err := nilWorker.Close(context.Background()); err != nil {
		t.Fatalf("nil Close() error = %v", err)
	}
}

func TestOutboxWorkerOptionBounds(t *testing.T) {
	for name, options := range map[string]OutboxWorkerOptions{
		"batch":       {BatchSize: -1},
		"concurrency": {Concurrency: 101},
		"lease":       {LeaseSeconds: 3_601},
		"poll":        {PollInterval: time.Millisecond},
		"retry":       {RetryDelaySeconds: 86_401},
		"maximum":     {MaxRetryDelaySeconds: 86_401},
		"attempts":    {AttemptLimit: 101},
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := options.normalized(); err == nil {
				t.Fatal("invalid outbox worker options accepted")
			}
		})
	}
}

func TestOutboxWorkerLifecycleAndFailureBoundaries(t *testing.T) {
	worker, err := NewOutboxWorker(
		&outboxStoreStub{},
		[]OutboxHandler{&outboxHandlerStub{topics: []string{"usage"}}},
		OutboxWorkerOptions{PollInterval: time.Hour},
	)
	if err != nil {
		t.Fatal(err)
	}
	if err := worker.Health(context.Background()); err == nil {
		t.Fatal("unstarted outbox worker reported healthy")
	}
	canceled, cancel := context.WithCancel(context.Background())
	cancel()
	if err := worker.Start(canceled); !errors.Is(err, context.Canceled) {
		t.Fatalf("Start(canceled) error = %v", err)
	}
	if err := worker.Start(context.Background()); err != nil {
		t.Fatal(err)
	}
	if err := worker.Start(context.Background()); err != nil {
		t.Fatalf("idempotent Start() error = %v", err)
	}
	if err := worker.Flush(context.Background()); err != nil {
		t.Fatalf("Flush() error = %v", err)
	}
	if err := worker.Close(context.Background()); err != nil {
		t.Fatal(err)
	}

	callbackRan := false
	worker.options.OnError = func(error) {
		callbackRan = true
		panic("host callback")
	}
	worker.reportError(errors.New("delivery failed"))
	if !callbackRan {
		t.Fatal("outbox error callback was not invoked")
	}
}

type boundaryProjectionRepository struct {
	usage        *UsageChargeExport
	usageErr     error
	billing      *BillingEventPayloadExport
	billingErr   error
	recordResult bool
	recordErr    error
}

func (r *boundaryProjectionRepository) GetUsageCharge(context.Context, string) (*UsageChargeExport, error) {
	return r.usage, r.usageErr
}

func (r *boundaryProjectionRepository) GetBillingEventPayload(context.Context, string) (*BillingEventPayloadExport, error) {
	return r.billing, r.billingErr
}

func (r *boundaryProjectionRepository) ArchiveBillingEventPayload(context.Context, string, string, *string) (bool, error) {
	return r.recordResult, r.recordErr
}

type boundaryUsageSink struct{ err error }

func (s *boundaryUsageSink) WriteUsage(context.Context, UsageChargeExport, string) error {
	return s.err
}

type boundaryBillingArchive struct {
	result BillingPayloadArchiveResult
	err    error
}

func (a *boundaryBillingArchive) Archive(context.Context, BillingEventPayloadExport) (BillingPayloadArchiveResult, error) {
	return a.result, a.err
}

func TestStorageHandlersUseValidatedEmbeddedPayloads(t *testing.T) {
	now := time.Date(2026, 8, 19, 1, 2, 3, 0, time.UTC)
	usagePayload := map[string]any{
		"tenant_id": storageTestTenant, "charge_id": storageTestCharge,
		"account_id": storageTestAccount, "subject_id": storageTestSubject, "operation": "generate",
		"measures": map[string]any{"tokens": "10"}, "dimensions": map[string]any{}, "metadata": map[string]any{},
		"requested": "1.250000", "charged": "1.200000", "allowance_requested": "0.050000",
		"allowance_covered": "0.050000", "billing_disposition": "billable",
		"pricing_snapshot": map[string]any{}, "idempotency_key": "usage-1", "request_digest": "digest",
		"event_at": now, "created_at": now,
	}
	repository := &boundaryProjectionRepository{usageErr: errors.New("authoritative lookup must not run")}
	usage, err := NewUsageChargeOutboxHandler(repository, &boundaryUsageSink{})
	if err != nil {
		t.Fatal(err)
	}
	if err := usage.Handle(context.Background(), OutboxEvent{
		EventID: "17", TenantID: storageTestTenant, AggregateID: storageTestCharge,
		PayloadVersion: 1, Payload: usagePayload,
	}); err != nil {
		t.Fatalf("embedded usage payload = %v", err)
	}

	billingPayload := map[string]any{
		"tenant_id": storageTestTenant, "event_id": storageTestBilling,
		"provider": "stripe", "provider_environment": "test", "provider_event_id": "evt_1",
		"event_type": "invoice.paid", "status": "completed", "received_at": now,
	}
	billingRepository := &boundaryProjectionRepository{billingErr: errors.New("stored payload unavailable"), recordResult: true}
	billing, err := NewBillingPayloadOutboxHandler(billingRepository, &boundaryBillingArchive{result: BillingPayloadArchiveResult{Key: "tenant/event.json"}})
	if err != nil {
		t.Fatal(err)
	}
	// The received-event envelope is authoritative and can be archived even if
	// the repository lookup fails only after that lookup succeeds.
	billingRepository.billingErr = nil
	if err := billing.Handle(context.Background(), OutboxEvent{
		TenantID: storageTestTenant, AggregateID: storageTestBilling,
		Topic: OutboxTopicBillingWebhookReceived, PayloadVersion: 1, Payload: billingPayload,
	}); err != nil {
		t.Fatalf("embedded billing payload = %v", err)
	}
}

func TestStorageHandlersFailClosedAtPersistenceBoundaries(t *testing.T) {
	baseUsage := OutboxEvent{TenantID: storageTestTenant, AggregateID: storageTestCharge, PayloadVersion: 1}
	for name, repository := range map[string]*boundaryProjectionRepository{
		"missing usage": {},
		"wrong charge":  {usage: &UsageChargeExport{TenantID: storageTestTenant, ChargeID: storageTestBilling}},
	} {
		t.Run(name, func(t *testing.T) {
			handler, err := NewUsageChargeOutboxHandler(repository, &boundaryUsageSink{})
			if err != nil {
				t.Fatal(err)
			}
			if err := handler.Handle(context.Background(), baseUsage); err == nil {
				t.Fatal("invalid usage export accepted")
			}
		})
	}

	baseBilling := OutboxEvent{
		TenantID: storageTestTenant, AggregateID: storageTestBilling,
		Topic: OutboxTopicBillingWebhookCompleted, PayloadVersion: 1,
	}
	payload := &BillingEventPayloadExport{TenantID: storageTestTenant, EventID: storageTestBilling}
	tests := []struct {
		name       string
		repository *boundaryProjectionRepository
		archive    *boundaryBillingArchive
	}{
		{"repository error", &boundaryProjectionRepository{billingErr: errors.New("lookup failed")}, &boundaryBillingArchive{}},
		{"missing payload", &boundaryProjectionRepository{}, &boundaryBillingArchive{}},
		{"wrong tenant", &boundaryProjectionRepository{billing: &BillingEventPayloadExport{TenantID: storageOtherTenant, EventID: storageTestBilling}}, &boundaryBillingArchive{}},
		{"wrong event", &boundaryProjectionRepository{billing: &BillingEventPayloadExport{TenantID: storageTestTenant, EventID: storageTestCharge}}, &boundaryBillingArchive{}},
		{"archive error", &boundaryProjectionRepository{billing: payload}, &boundaryBillingArchive{err: errors.New("archive failed")}},
		{"record error", &boundaryProjectionRepository{billing: payload, recordErr: errors.New("record failed")}, &boundaryBillingArchive{result: BillingPayloadArchiveResult{Key: "event.json"}}},
		{"record rejected", &boundaryProjectionRepository{billing: payload}, &boundaryBillingArchive{result: BillingPayloadArchiveResult{Key: "event.json"}}},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			handler, err := NewBillingPayloadOutboxHandler(test.repository, test.archive)
			if err != nil {
				t.Fatal(err)
			}
			if err := handler.Handle(context.Background(), baseBilling); err == nil {
				t.Fatal("invalid billing archive flow accepted")
			}
		})
	}
}

func TestUsageBatcherRejectsInvalidCallsAndRecoversNonErrorPanics(t *testing.T) {
	var nilBatcher *usageWriteBatcher
	if err := nilBatcher.submit(context.Background(), UsageExportEntry{}); err == nil {
		t.Fatal("nil usage batcher accepted a request")
	}
	batcher := newUsageWriteBatcher(&batchUsageSinkStub{})
	//lint:ignore SA1012 This boundary test intentionally verifies nil-context rejection.
	if err := batcher.submit(nil, UsageExportEntry{}); err == nil {
		t.Fatal("nil usage batch context accepted")
	}
	canceled, cancel := context.WithCancel(context.Background())
	cancel()
	if err := batcher.submit(canceled, UsageExportEntry{}); !errors.Is(err, context.Canceled) {
		t.Fatalf("canceled batch request error = %v", err)
	}
	batcher.flush()

	panicSink := &batchUsageSinkStub{panicValue: "projection panic"}
	panicBatcher := newUsageWriteBatcher(panicSink)
	panicBatcher.window = time.Millisecond
	err := panicBatcher.submit(context.Background(), UsageExportEntry{})
	if err == nil || !strings.Contains(err.Error(), "usage batch sink panicked") {
		t.Fatalf("non-error panic result = %v", err)
	}
}
