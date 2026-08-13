// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import (
	"context"
	"errors"
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
}

func NewUsageChargeOutboxHandler(repository StorageProjectionRepository, sink UsageEventSink) (*UsageChargeOutboxHandler, error) {
	if repository == nil || sink == nil {
		return nil, outboxConfigError("usage outbox handler requires a repository and sink")
	}
	return &UsageChargeOutboxHandler{repository: repository, sink: sink}, nil
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
