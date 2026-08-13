// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import (
	"context"
	"time"
)

// OutboxEvent is one tenant-bound, leased delivery. Event handlers must be
// idempotent because an external write can succeed before Complete is recorded.
type OutboxEvent struct {
	EventID        string         `json:"event_id"`
	TenantID       string         `json:"tenant_id"`
	Topic          string         `json:"topic"`
	AggregateType  string         `json:"aggregate_type"`
	AggregateID    string         `json:"aggregate_id"`
	PayloadVersion int            `json:"payload_version"`
	Payload        map[string]any `json:"payload"`
	ClaimToken     string         `json:"claim_token"`
	AttemptCount   int            `json:"attempt_count"`
	CreatedAt      time.Time      `json:"created_at"`
}

// OutboxClaimRenewalStore extends live claims while handlers are running.
type OutboxClaimRenewalStore interface {
	Renew(context.Context, OutboxEvent, int) (bool, error)
}

// OutboxStore is the durable boundary used by OutboxWorker.
type OutboxStore interface {
	OutboxClaimRenewalStore
	Claim(context.Context, []string, int, int) ([]OutboxEvent, error)
	Complete(context.Context, OutboxEvent) (bool, error)
	Fail(context.Context, OutboxEvent, string, int, int) (bool, error)
}

type OutboxStats struct {
	PendingCount    int        `json:"pending_count"`
	ProcessingCount int        `json:"processing_count"`
	DeliveredCount  int        `json:"delivered_count"`
	DeadLetterCount int        `json:"dead_letter_count"`
	OldestPendingAt *time.Time `json:"oldest_pending_at,omitempty"`
}

type OutboxDeadLetterCursor struct {
	CreatedAt time.Time `json:"created_at"`
	EventID   string    `json:"event_id"`
}

type OutboxDeadLetter struct {
	EventID        string    `json:"event_id"`
	TenantID       string    `json:"tenant_id"`
	Topic          string    `json:"topic"`
	AggregateType  string    `json:"aggregate_type"`
	AggregateID    string    `json:"aggregate_id"`
	PayloadVersion int       `json:"payload_version"`
	AttemptCount   int       `json:"attempt_count"`
	LastError      *string   `json:"last_error,omitempty"`
	CreatedAt      time.Time `json:"created_at"`
	UpdatedAt      time.Time `json:"updated_at"`
}

type OutboxDeadLetterListOptions struct {
	Limit  int
	Cursor *OutboxDeadLetterCursor
}

type OutboxDeadLetterPage struct {
	Items      []OutboxDeadLetter      `json:"items"`
	NextCursor *OutboxDeadLetterCursor `json:"next_cursor,omitempty"`
}

// OutboxRecoveryStore adds operational inspection and recovery without
// weakening claim-token ownership checks.
type OutboxRecoveryStore interface {
	OutboxStore
	Stats(context.Context) (OutboxStats, error)
	ListDeadLetters(context.Context, OutboxDeadLetterListOptions) (OutboxDeadLetterPage, error)
	Requeue(context.Context, string) (bool, error)
}

type OutboxHandler interface {
	Topics() []string
	Handle(context.Context, OutboxEvent) error
}

type BillingDisposition string

const (
	BillingDispositionBillable   BillingDisposition = "billable"
	BillingDispositionRecordOnly BillingDisposition = "record_only"
)

// UsageChargeExport is the canonical immutable usage projection. Financial
// fields use Amount so no binary floating-point conversion is possible.
type UsageChargeExport struct {
	TenantID             string             `json:"tenant_id"`
	ChargeID             string             `json:"charge_id"`
	AccountID            string             `json:"account_id"`
	SubjectID            string             `json:"subject_id"`
	Operation            string             `json:"operation"`
	Feature              *string            `json:"feature,omitempty"`
	Model                *string            `json:"model,omitempty"`
	Region               *string            `json:"region,omitempty"`
	Measures             map[string]any     `json:"measures"`
	Dimensions           map[string]any     `json:"dimensions"`
	Metadata             map[string]any     `json:"metadata"`
	Requested            Amount             `json:"requested"`
	Charged              Amount             `json:"charged"`
	AllowanceRequested   Amount             `json:"allowance_requested"`
	AllowanceCovered     Amount             `json:"allowance_covered"`
	BillingDisposition   BillingDisposition `json:"billing_disposition"`
	CatalogRevisionID    *string            `json:"catalog_revision_id,omitempty"`
	PlanID               *string            `json:"plan_id,omitempty"`
	RateCardKey          *string            `json:"rate_card_key,omitempty"`
	PricingSnapshot      map[string]any     `json:"pricing_snapshot"`
	LedgerEntryID        *string            `json:"ledger_entry_id,omitempty"`
	CorrectionOfChargeID *string            `json:"correction_of_charge_id,omitempty"`
	IdempotencyKey       string             `json:"idempotency_key"`
	RequestDigest        string             `json:"request_digest"`
	EventAt              time.Time          `json:"event_at"`
	CreatedAt            time.Time          `json:"created_at"`
}

type BillingEventPayloadExport struct {
	TenantID            string         `json:"tenant_id"`
	EventID             string         `json:"event_id"`
	Provider            string         `json:"provider"`
	ProviderEnvironment string         `json:"provider_environment"`
	ProviderEventID     string         `json:"provider_event_id"`
	EventType           string         `json:"event_type"`
	Status              string         `json:"status"`
	ReceivedAt          time.Time      `json:"received_at"`
	CompletedAt         *time.Time     `json:"completed_at,omitempty"`
	Envelope            map[string]any `json:"envelope,omitempty"`
	ObjectKey           *string        `json:"object_key,omitempty"`
	ObjectVersion       *string        `json:"object_version,omitempty"`
	ArchivedAt          *time.Time     `json:"archived_at,omitempty"`
}

type UsageEventSink interface {
	WriteUsage(context.Context, UsageChargeExport, string) error
}

type BatchUsageEventSink interface {
	UsageEventSink
	WriteUsageBatch(context.Context, []UsageExportEntry) error
}

type UsageExportEntry struct {
	Event         UsageChargeExport `json:"event"`
	OutboxEventID string            `json:"outbox_event_id"`
}

type UsageProjectionInitializer interface {
	Initialize(context.Context) error
}

type UsageProjectionSchema interface {
	CheckSchemaCompatibility(context.Context) error
}

type BillingPayloadArchiveResult struct {
	Key       string  `json:"key"`
	VersionID *string `json:"version_id,omitempty"`
}

type BillingPayloadArchive interface {
	Archive(context.Context, BillingEventPayloadExport) (BillingPayloadArchiveResult, error)
}
