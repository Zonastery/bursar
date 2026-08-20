// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"time"
)

const (
	OutboxTopicUsageChargeRecorded     = "usage.charge_recorded"
	OutboxTopicBillingWebhookReceived  = "billing.webhook_received"
	OutboxTopicBillingWebhookCompleted = "billing.webhook_completed"
)

type StorageProjectionRepository interface {
	GetUsageCharge(context.Context, string) (*UsageChargeExport, error)
	GetBillingEventPayload(context.Context, string) (*BillingEventPayloadExport, error)
	ArchiveBillingEventPayload(context.Context, string, string, *string) (bool, error)
}

// UsageChargeOutboxHandler exports canonical usage charges to an optional
// projection sink. It trusts embedded payloads only after complete validation
// and otherwise reloads the authoritative PostgreSQL record.
type UsageChargeOutboxHandler struct {
	repository StorageProjectionRepository
	sink       UsageEventSink
	batcher    *usageWriteBatcher
}

const usageWriteBatchWindow = time.Millisecond

type usageWriteRequest struct {
	entry UsageExportEntry
	ctx   context.Context
	done  chan error
}

// usageWriteBatcher coalesces concurrent usage deliveries for one short fixed
// window. The shared sink call is canceled only after every request context is
// canceled, so one impatient caller cannot poison unrelated outbox work while
// worker shutdown still propagates when it cancels all deliveries.
type usageWriteBatcher struct {
	sink    BatchUsageEventSink
	window  time.Duration
	mu      sync.Mutex
	pending []usageWriteRequest
	timer   *time.Timer
}

func newUsageWriteBatcher(sink BatchUsageEventSink) *usageWriteBatcher {
	return &usageWriteBatcher{sink: sink, window: usageWriteBatchWindow}
}

func (b *usageWriteBatcher) submit(ctx context.Context, entry UsageExportEntry) error {
	if b == nil || b.sink == nil {
		return errors.New("usage batch writer is not initialized")
	}
	if ctx == nil {
		return errors.New("usage batch context is required")
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	request := usageWriteRequest{entry: entry, ctx: ctx, done: make(chan error, 1)}
	b.mu.Lock()
	b.pending = append(b.pending, request)
	if b.timer == nil {
		b.timer = time.AfterFunc(b.window, b.flush)
	}
	b.mu.Unlock()
	select {
	case err := <-request.done:
		return err
	case <-ctx.Done():
		return ctx.Err()
	}
}

func (b *usageWriteBatcher) flush() {
	b.mu.Lock()
	pending := b.pending
	b.pending = nil
	b.timer = nil
	b.mu.Unlock()
	if len(pending) == 0 {
		return
	}
	entries := make([]UsageExportEntry, len(pending))
	for index, request := range pending {
		entries[index] = request.entry
	}
	batchCtx, finishBatch := mergedUsageBatchContext(pending)
	var err error
	func() {
		defer func() {
			if recovered := recover(); recovered != nil {
				switch value := recovered.(type) {
				case error:
					err = fmt.Errorf("usage batch sink panicked: %w", value)
				default:
					err = fmt.Errorf("usage batch sink panicked: %v", value)
				}
			}
		}()
		err = b.sink.WriteUsageBatch(batchCtx, entries)
	}()
	finishBatch()
	for _, request := range pending {
		request.done <- err
	}
}

func mergedUsageBatchContext(pending []usageWriteRequest) (context.Context, func()) {
	base := context.Background()
	if len(pending) > 0 {
		base = context.WithoutCancel(pending[0].ctx)
	}
	ctx, cancel := context.WithCancel(base)
	finished := make(chan struct{})
	var finishOnce sync.Once
	finish := func() {
		finishOnce.Do(func() {
			close(finished)
			cancel()
		})
	}
	go func() {
		for _, request := range pending {
			select {
			case <-request.ctx.Done():
			case <-finished:
				return
			}
		}
		cancel()
	}()
	return ctx, finish
}

func NewUsageChargeOutboxHandler(repository StorageProjectionRepository, sink UsageEventSink) (*UsageChargeOutboxHandler, error) {
	if repository == nil || sink == nil {
		return nil, outboxConfigError("usage outbox handler requires a repository and sink")
	}
	handler := &UsageChargeOutboxHandler{repository: repository, sink: sink}
	if batchSink, ok := sink.(BatchUsageEventSink); ok {
		handler.batcher = newUsageWriteBatcher(batchSink)
	}
	return handler, nil
}

func (*UsageChargeOutboxHandler) Topics() []string {
	return []string{OutboxTopicUsageChargeRecorded}
}

func (h *UsageChargeOutboxHandler) Handle(ctx context.Context, event OutboxEvent) error {
	if event.PayloadVersion != 1 {
		return errors.New("unsupported usage outbox payload version")
	}
	var usage *UsageChargeExport
	if event.Payload["charge_id"] != nil {
		parsed, err := usageChargeExportFromPayload(event.Payload)
		if err == nil {
			usage = &parsed
		}
	}
	if usage == nil {
		var err error
		usage, err = h.repository.GetUsageCharge(ctx, event.AggregateID)
		if err != nil {
			return err
		}
	}
	if usage == nil {
		return errors.New("usage charge is unavailable for export")
	}
	if usage.TenantID != event.TenantID {
		return errors.New("usage export tenant does not match its outbox event")
	}
	if usage.ChargeID != event.AggregateID {
		return errors.New("usage export charge does not match its outbox event")
	}
	if h.batcher != nil {
		return h.batcher.submit(ctx, UsageExportEntry{Event: *usage, OutboxEventID: event.EventID})
	}
	return h.sink.WriteUsage(ctx, *usage, event.EventID)
}

// BillingPayloadOutboxHandler archives webhook payloads and records the object
// pointer in PostgreSQL. Replayed completed events are idempotently skipped once
// the authoritative row already contains an archive key.
type BillingPayloadOutboxHandler struct {
	repository StorageProjectionRepository
	archive    BillingPayloadArchive
}

func NewBillingPayloadOutboxHandler(repository StorageProjectionRepository, archive BillingPayloadArchive) (*BillingPayloadOutboxHandler, error) {
	if repository == nil || archive == nil {
		return nil, outboxConfigError("billing outbox handler requires a repository and archive")
	}
	return &BillingPayloadOutboxHandler{repository: repository, archive: archive}, nil
}

func (*BillingPayloadOutboxHandler) Topics() []string {
	return []string{OutboxTopicBillingWebhookReceived, OutboxTopicBillingWebhookCompleted}
}

func (h *BillingPayloadOutboxHandler) Handle(ctx context.Context, event OutboxEvent) error {
	if event.PayloadVersion != 1 {
		return errors.New("unsupported billing outbox payload version")
	}
	stored, err := h.repository.GetBillingEventPayload(ctx, event.AggregateID)
	if err != nil {
		return err
	}
	if stored != nil && stored.ObjectKey != nil {
		return nil
	}
	var payload *BillingEventPayloadExport
	if event.Topic == OutboxTopicBillingWebhookReceived {
		parsed, parseErr := billingPayloadExportFromPayload(event.Payload)
		if parseErr != nil {
			return parseErr
		}
		payload = &parsed
	} else {
		payload = stored
	}
	if payload == nil {
		return errors.New("billing event is unavailable for archive")
	}
	if payload.TenantID != event.TenantID {
		return errors.New("billing export tenant does not match its outbox event")
	}
	if payload.EventID != event.AggregateID {
		return errors.New("billing export event does not match its outbox event")
	}
	archived, err := h.archive.Archive(ctx, *payload)
	if err != nil {
		return err
	}
	if archived.Key == "" {
		return errors.New("billing archive returned an empty object key")
	}
	recorded, err := h.repository.ArchiveBillingEventPayload(ctx, payload.EventID, archived.Key, archived.VersionID)
	if err != nil {
		return err
	}
	if !recorded {
		return errors.New("could not record billing archive pointer")
	}
	return nil
}
