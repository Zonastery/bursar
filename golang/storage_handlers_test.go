package bursar

import (
	"context"
	"errors"
	"strings"
	"sync"
	"testing"
	"time"
)

type projectionRepositoryStub struct {
	usage         *UsageChargeExport
	usageByID     map[string]*UsageChargeExport
	billing       *BillingEventPayloadExport
	archivedID    string
	archivedKey   string
	archivedVer   *string
	archiveResult bool
}

func (r *projectionRepositoryStub) GetUsageCharge(_ context.Context, chargeID string) (*UsageChargeExport, error) {
	if r.usageByID != nil {
		return r.usageByID[chargeID], nil
	}
	return r.usage, nil
}

func (r *projectionRepositoryStub) GetBillingEventPayload(context.Context, string) (*BillingEventPayloadExport, error) {
	return r.billing, nil
}

func (r *projectionRepositoryStub) ArchiveBillingEventPayload(_ context.Context, eventID, key string, version *string) (bool, error) {
	r.archivedID, r.archivedKey, r.archivedVer = eventID, key, version
	return r.archiveResult, nil
}

type usageSinkStub struct {
	event         UsageChargeExport
	outboxEventID string
	directCalls   int
}

func (s *usageSinkStub) WriteUsage(_ context.Context, event UsageChargeExport, outboxEventID string) error {
	s.directCalls++
	s.event, s.outboxEventID = event, outboxEventID
	return nil
}

type batchUsageSinkStub struct {
	mu            sync.Mutex
	entries       []UsageExportEntry
	calls         int
	err           error
	panicValue    any
	waitForCancel bool
	started       chan struct{}
	completed     chan struct{}
}

func (s *batchUsageSinkStub) WriteUsage(context.Context, UsageChargeExport, string) error { return nil }

func (s *batchUsageSinkStub) WriteUsageBatch(ctx context.Context, entries []UsageExportEntry) error {
	s.mu.Lock()
	s.calls++
	s.entries = append([]UsageExportEntry(nil), entries...)
	started, completed, err := s.started, s.completed, s.err
	s.mu.Unlock()
	if started != nil {
		close(started)
	}
	if completed != nil {
		defer close(completed)
	}
	if err != nil {
		return err
	}
	if s.panicValue != nil {
		panic(s.panicValue)
	}
	if s.waitForCancel {
		<-ctx.Done()
	}
	return ctx.Err()
}

type billingArchiveStub struct {
	event  BillingEventPayloadExport
	result BillingPayloadArchiveResult
}

func (a *billingArchiveStub) Archive(_ context.Context, event BillingEventPayloadExport) (BillingPayloadArchiveResult, error) {
	a.event = event
	return a.result, nil
}

func TestUsageChargeOutboxHandlerUsesAuthoritativeFallback(t *testing.T) {
	repository := &projectionRepositoryStub{usage: &UsageChargeExport{
		TenantID: storageTestTenant, ChargeID: storageTestCharge, Requested: MustAmount("1.000000"), Charged: MustAmount("1.000000"),
	}}
	sink := &usageSinkStub{}
	handler, err := NewUsageChargeOutboxHandler(repository, sink)
	if err != nil {
		t.Fatal(err)
	}
	event := OutboxEvent{
		EventID: "10", TenantID: storageTestTenant, Topic: OutboxTopicUsageChargeRecorded,
		AggregateID: storageTestCharge, PayloadVersion: 1, Payload: map[string]any{},
	}
	if err := handler.Handle(context.Background(), event); err != nil {
		t.Fatal(err)
	}
	if sink.outboxEventID != "10" || sink.event.ChargeID != storageTestCharge {
		t.Fatalf("sink = %#v / %q", sink.event, sink.outboxEventID)
	}
	if sink.directCalls != 1 {
		t.Fatalf("direct sink calls = %d, want 1", sink.directCalls)
	}
	repository.usage.TenantID = storageOtherTenant
	if err := handler.Handle(context.Background(), event); err == nil {
		t.Fatal("cross-tenant usage export was accepted")
	}
}

func TestUsageChargeOutboxHandlerCoalescesBatchWrites(t *testing.T) {
	usageByID := map[string]*UsageChargeExport{
		"charge-1": {TenantID: storageTestTenant, ChargeID: "charge-1", Requested: MustAmount("1"), Charged: MustAmount("1")},
		"charge-2": {TenantID: storageTestTenant, ChargeID: "charge-2", Requested: MustAmount("2"), Charged: MustAmount("2")},
		"charge-3": {TenantID: storageTestTenant, ChargeID: "charge-3", Requested: MustAmount("3"), Charged: MustAmount("3")},
	}
	sink := &batchUsageSinkStub{}
	handler, err := NewUsageChargeOutboxHandler(&projectionRepositoryStub{usageByID: usageByID}, sink)
	if err != nil {
		t.Fatal(err)
	}
	handler.batcher.window = 100 * time.Millisecond
	events := []OutboxEvent{
		{EventID: "outbox-1", TenantID: storageTestTenant, Topic: OutboxTopicUsageChargeRecorded, AggregateID: "charge-1", PayloadVersion: 1},
		{EventID: "outbox-2", TenantID: storageTestTenant, Topic: OutboxTopicUsageChargeRecorded, AggregateID: "charge-2", PayloadVersion: 1},
		{EventID: "outbox-3", TenantID: storageTestTenant, Topic: OutboxTopicUsageChargeRecorded, AggregateID: "charge-3", PayloadVersion: 1},
	}
	start := make(chan struct{})
	errs := make(chan error, len(events))
	var wait sync.WaitGroup
	for _, event := range events {
		event := event
		wait.Add(1)
		go func() {
			defer wait.Done()
			<-start
			errs <- handler.Handle(context.Background(), event)
		}()
	}
	close(start)
	wait.Wait()
	close(errs)
	for err := range errs {
		if err != nil {
			t.Fatalf("batched Handle() error = %v", err)
		}
	}
	sink.mu.Lock()
	defer sink.mu.Unlock()
	if sink.calls != 1 || len(sink.entries) != len(events) {
		t.Fatalf("batch calls/entries = %d/%d, want 1/%d", sink.calls, len(sink.entries), len(events))
	}
	seen := make(map[string]string, len(sink.entries))
	for _, entry := range sink.entries {
		seen[entry.Event.ChargeID] = entry.OutboxEventID
	}
	for _, event := range events {
		if seen[event.AggregateID] != event.EventID {
			t.Fatalf("batch identity for %s = %q, want %q", event.AggregateID, seen[event.AggregateID], event.EventID)
		}
	}
}

func TestUsageChargeOutboxHandlerSharesBatchError(t *testing.T) {
	sharedErr := errors.New("projection unavailable")
	sink := &batchUsageSinkStub{err: sharedErr}
	handler, err := NewUsageChargeOutboxHandler(&projectionRepositoryStub{usage: &UsageChargeExport{
		TenantID: storageTestTenant, ChargeID: storageTestCharge, Requested: MustAmount("1"), Charged: MustAmount("1"),
	}}, sink)
	if err != nil {
		t.Fatal(err)
	}
	handler.batcher.window = 100 * time.Millisecond
	start := make(chan struct{})
	errs := make(chan error, 2)
	for _, eventID := range []string{"batch-error-1", "batch-error-2"} {
		go func(eventID string) {
			<-start
			errs <- handler.Handle(context.Background(), OutboxEvent{
				EventID: eventID, TenantID: storageTestTenant, Topic: OutboxTopicUsageChargeRecorded,
				AggregateID: storageTestCharge, PayloadVersion: 1,
			})
		}(eventID)
	}
	close(start)
	for range 2 {
		if err := <-errs; !errors.Is(err, sharedErr) {
			t.Fatalf("shared batch error = %v, want %v", err, sharedErr)
		}
	}
	sink.mu.Lock()
	defer sink.mu.Unlock()
	if sink.calls != 1 {
		t.Fatalf("batch calls = %d, want 1", sink.calls)
	}
}

func TestUsageChargeOutboxHandlerConvertsBatchSinkPanicToSharedError(t *testing.T) {
	sink := &batchUsageSinkStub{panicValue: errors.New("projection panic")}
	handler, err := NewUsageChargeOutboxHandler(&projectionRepositoryStub{usage: &UsageChargeExport{
		TenantID: storageTestTenant, ChargeID: storageTestCharge,
		Requested: MustAmount("1"), Charged: MustAmount("1"),
	}}, sink)
	if err != nil {
		t.Fatal(err)
	}
	handler.batcher.window = time.Millisecond
	start := make(chan struct{})
	errs := make(chan error, 2)
	for _, eventID := range []string{"batch-panic-1", "batch-panic-2"} {
		go func(eventID string) {
			<-start
			errs <- handler.Handle(context.Background(), OutboxEvent{
				EventID: eventID, TenantID: storageTestTenant, Topic: OutboxTopicUsageChargeRecorded,
				AggregateID: storageTestCharge, PayloadVersion: 1, Payload: map[string]any{},
			})
		}(eventID)
	}
	close(start)
	for range 2 {
		err := <-errs
		if err == nil || !strings.Contains(err.Error(), "usage batch sink panicked") {
			t.Fatalf("panic result = %v", err)
		}
	}
	if sink.calls != 1 {
		t.Fatalf("batch calls = %d, want 1", sink.calls)
	}
}

func TestUsageChargeOutboxHandlerBatchRespectsContextCancellation(t *testing.T) {
	sink := &batchUsageSinkStub{started: make(chan struct{}), completed: make(chan struct{}), waitForCancel: true}
	handler, err := NewUsageChargeOutboxHandler(&projectionRepositoryStub{usage: &UsageChargeExport{
		TenantID: storageTestTenant, ChargeID: storageTestCharge, Requested: MustAmount("1"), Charged: MustAmount("1"),
	}}, sink)
	if err != nil {
		t.Fatal(err)
	}
	handler.batcher.window = time.Millisecond
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	err = handler.Handle(ctx, OutboxEvent{
		EventID: "cancelled-batch", TenantID: storageTestTenant, Topic: OutboxTopicUsageChargeRecorded,
		AggregateID: storageTestCharge, PayloadVersion: 1,
	})
	if !errors.Is(err, context.DeadlineExceeded) && !errors.Is(err, context.Canceled) {
		t.Fatalf("Handle() error = %v, want context cancellation", err)
	}
	select {
	case <-sink.started:
	case <-time.After(time.Second):
		t.Fatal("batch sink was not called")
	}
	select {
	case <-sink.completed:
	case <-time.After(time.Second):
		t.Fatal("batch sink did not complete after context cancellation")
	}
}

func TestUsageChargeOutboxHandlerCanceledRequestDoesNotPoisonBatch(t *testing.T) {
	sink := &batchUsageSinkStub{}
	handler, err := NewUsageChargeOutboxHandler(&projectionRepositoryStub{usage: &UsageChargeExport{
		TenantID: storageTestTenant, ChargeID: storageTestCharge, Requested: MustAmount("1"), Charged: MustAmount("1"),
	}}, sink)
	if err != nil {
		t.Fatal(err)
	}
	handler.batcher.window = 100 * time.Millisecond
	firstCtx, cancelFirst := context.WithCancel(context.Background())
	results := make(chan error, 2)
	go func() {
		results <- handler.Handle(firstCtx, OutboxEvent{
			EventID: "mixed-context-1", TenantID: storageTestTenant, Topic: OutboxTopicUsageChargeRecorded,
			AggregateID: storageTestCharge, PayloadVersion: 1,
		})
	}()
	go func() {
		results <- handler.Handle(context.Background(), OutboxEvent{
			EventID: "mixed-context-2", TenantID: storageTestTenant, Topic: OutboxTopicUsageChargeRecorded,
			AggregateID: storageTestCharge, PayloadVersion: 1,
		})
	}()

	deadline := time.Now().Add(time.Second)
	for {
		handler.batcher.mu.Lock()
		pending := len(handler.batcher.pending)
		handler.batcher.mu.Unlock()
		if pending == 2 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("batched requests pending = %d, want 2", pending)
		}
		time.Sleep(time.Millisecond)
	}
	cancelFirst()

	firstErr, secondErr := <-results, <-results
	if !errors.Is(firstErr, context.Canceled) && !errors.Is(secondErr, context.Canceled) {
		t.Fatalf("mixed batch errors = %v, %v; want one canceled request", firstErr, secondErr)
	}
	if firstErr != nil && !errors.Is(firstErr, context.Canceled) {
		t.Fatalf("unexpected first batch error = %v", firstErr)
	}
	if secondErr != nil && !errors.Is(secondErr, context.Canceled) {
		t.Fatalf("unexpected second batch error = %v", secondErr)
	}
	if errors.Is(firstErr, context.Canceled) && errors.Is(secondErr, context.Canceled) {
		t.Fatalf("canceled request poisoned active batch peer: %v, %v", firstErr, secondErr)
	}
}

func TestBillingPayloadOutboxHandlerArchivesAndRecordsPointer(t *testing.T) {
	payload := &BillingEventPayloadExport{TenantID: storageTestTenant, EventID: storageTestBilling, Provider: "stripe"}
	repository := &projectionRepositoryStub{billing: payload, archiveResult: true}
	version := "version-1"
	archive := &billingArchiveStub{result: BillingPayloadArchiveResult{Key: "tenant/event.json", VersionID: &version}}
	handler, err := NewBillingPayloadOutboxHandler(repository, archive)
	if err != nil {
		t.Fatal(err)
	}
	event := OutboxEvent{
		EventID: "11", TenantID: storageTestTenant, Topic: OutboxTopicBillingWebhookCompleted,
		AggregateID: storageTestBilling, PayloadVersion: 1, Payload: map[string]any{},
	}
	if err := handler.Handle(context.Background(), event); err != nil {
		t.Fatal(err)
	}
	if repository.archivedID != storageTestBilling || repository.archivedKey != "tenant/event.json" || repository.archivedVer == nil || *repository.archivedVer != version {
		t.Fatalf("archive pointer = %q %q %#v", repository.archivedID, repository.archivedKey, repository.archivedVer)
	}
	existingKey := "already-archived.json"
	repository.billing.ObjectKey = &existingKey
	repository.archivedID = ""
	if err := handler.Handle(context.Background(), event); err != nil {
		t.Fatal(err)
	}
	if repository.archivedID != "" {
		t.Fatal("already archived event was archived again")
	}
}
