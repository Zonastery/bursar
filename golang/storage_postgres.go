// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import (
	"context"
	"encoding/json"
	"regexp"
	"strconv"
	"strings"

	"github.com/jackc/pgx/v5/pgtype"
)

type storagePostgresCaller interface {
	TenantID() string
	Call(context.Context, string, ...any) ([]map[string]any, error)
}

// PostgresStorageRepository implements durable outbox recovery and projection
// export against Bursar's stable SQL RPCs. The supplied client must be tenant
// scoped; every call therefore inherits transaction-local RLS configuration.
type PostgresStorageRepository struct {
	client     storagePostgresCaller
	tenantID   string
	tenantUUID pgtype.UUID
}

var _ OutboxRecoveryStore = (*PostgresStorageRepository)(nil)

func NewPostgresStorageRepository(client *PostgresClient) (*PostgresStorageRepository, error) {
	return newPostgresStorageRepository(client)
}

func NewPostgresStorageRepositoryFromStore(store *PostgresStore) (*PostgresStorageRepository, error) {
	if store == nil || store.client == nil {
		return nil, NewError("PostgreSQL store is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	return newPostgresStorageRepository(store.client)
}

func newPostgresStorageRepository(client storagePostgresCaller) (*PostgresStorageRepository, error) {
	if client == nil {
		return nil, outboxConfigError("PostgreSQL client is required")
	}
	tenantID, err := normalizeTenantID(client.TenantID())
	if err != nil {
		return nil, NewError("PostgresStorageRepository requires a tenant-scoped client", ErrorOptions{Code: ErrorCodeConfig, Category: ErrorCategoryInvalidRequest, Cause: err})
	}
	tenantUUID, err := postgresUUID(tenantID, "storage repository tenant ID")
	if err != nil {
		return nil, err
	}
	return &PostgresStorageRepository{client: client, tenantID: tenantID, tenantUUID: tenantUUID}, nil
}

func (r *PostgresStorageRepository) Claim(ctx context.Context, topics []string, limit, leaseSeconds int) ([]OutboxEvent, error) {
	if err := validateOutboxClaim(topics, limit, leaseSeconds); err != nil {
		return nil, err
	}
	rows, err := r.client.Call(ctx, "claim_outbox_events", r.tenantUUID, limit, leaseSeconds, topics)
	if err != nil {
		return nil, err
	}
	events := make([]OutboxEvent, 0, len(rows))
	for _, row := range rows {
		event, err := outboxEventFromRow(row)
		if err != nil {
			return nil, err
		}
		if event.TenantID != r.tenantID {
			return nil, NewStoreError("claim_outbox_events returned an event for another tenant", ErrorOptions{})
		}
		events = append(events, event)
	}
	return events, nil
}

func (r *PostgresStorageRepository) Renew(ctx context.Context, event OutboxEvent, leaseSeconds int) (bool, error) {
	eventID, claimToken, err := r.outboxClaimArguments(event)
	if err != nil {
		return false, err
	}
	if leaseSeconds < 1 || leaseSeconds > 3_600 {
		return false, outboxConfigError("lease seconds must be between 1 and 3600")
	}
	rows, err := r.client.Call(ctx, "renew_tenant_outbox_claim", r.tenantUUID, eventID, claimToken, leaseSeconds)
	return postgresBooleanResult(rows, "renew_tenant_outbox_claim", err)
}

func (r *PostgresStorageRepository) Complete(ctx context.Context, event OutboxEvent) (bool, error) {
	eventID, claimToken, err := r.outboxClaimArguments(event)
	if err != nil {
		return false, err
	}
	rows, err := r.client.Call(ctx, "complete_tenant_outbox_event", r.tenantUUID, eventID, claimToken)
	return postgresBooleanResult(rows, "complete_tenant_outbox_event", err)
}

func (r *PostgresStorageRepository) Fail(ctx context.Context, event OutboxEvent, summary string, retryDelaySeconds, attemptLimit int) (bool, error) {
	eventID, claimToken, err := r.outboxClaimArguments(event)
	if err != nil {
		return false, err
	}
	normalizedSummary, err := validatePersistedDiagnosticSummary(summary)
	if err != nil {
		return false, err
	}
	if retryDelaySeconds < 1 || retryDelaySeconds > 86_400 {
		return false, outboxConfigError("retry delay seconds must be between 1 and 86400")
	}
	if attemptLimit < 1 || attemptLimit > 100 {
		return false, outboxConfigError("attempt limit must be between 1 and 100")
	}
	rows, callErr := r.client.Call(ctx, "fail_tenant_outbox_event", r.tenantUUID, eventID, claimToken, normalizedSummary, retryDelaySeconds, attemptLimit)
	return postgresBooleanResult(rows, "fail_tenant_outbox_event", callErr)
}

func (r *PostgresStorageRepository) Stats(ctx context.Context) (OutboxStats, error) {
	rows, err := r.client.Call(ctx, "get_outbox_stats", r.tenantUUID)
	if err != nil {
		return OutboxStats{}, err
	}
	row, err := exactlyOneRow(rows, "get_outbox_stats")
	if err != nil {
		return OutboxStats{}, err
	}
	pending, err := nonnegativeRowInt(row, "pending_count", "get_outbox_stats")
	if err != nil {
		return OutboxStats{}, err
	}
	processing, err := nonnegativeRowInt(row, "processing_count", "get_outbox_stats")
	if err != nil {
		return OutboxStats{}, err
	}
	delivered, err := nonnegativeRowInt(row, "delivered_count", "get_outbox_stats")
	if err != nil {
		return OutboxStats{}, err
	}
	deadLetters, err := nonnegativeRowInt(row, "dead_letter_count", "get_outbox_stats")
	if err != nil {
		return OutboxStats{}, err
	}
	oldest, err := optionalRowTime(row, "oldest_pending_at", "get_outbox_stats")
	if err != nil {
		return OutboxStats{}, err
	}
	return OutboxStats{
		PendingCount: pending, ProcessingCount: processing, DeliveredCount: delivered,
		DeadLetterCount: deadLetters, OldestPendingAt: oldest,
	}, nil
}

func (r *PostgresStorageRepository) ListDeadLetters(ctx context.Context, options OutboxDeadLetterListOptions) (OutboxDeadLetterPage, error) {
	if options.Limit == 0 {
		options.Limit = 100
	}
	if options.Limit < 1 || options.Limit > 100 {
		return OutboxDeadLetterPage{}, outboxConfigError("dead-letter page limit must be between 1 and 100")
	}
	cursorAt := pgtype.Timestamptz{Valid: false}
	cursorID := pgtype.Int8{Valid: false}
	if options.Cursor != nil {
		if options.Cursor.CreatedAt.IsZero() {
			return OutboxDeadLetterPage{}, outboxConfigError("dead-letter cursor timestamp is required")
		}
		parsedCursorID, err := postgresOutboxEventID(options.Cursor.EventID)
		if err != nil {
			return OutboxDeadLetterPage{}, err
		}
		cursorAt = pgtype.Timestamptz{Time: options.Cursor.CreatedAt.UTC(), Valid: true}
		cursorID = pgtype.Int8{Int64: parsedCursorID, Valid: true}
	}
	rows, err := r.client.Call(ctx, "list_outbox_dead_letters", r.tenantUUID, cursorAt, cursorID, options.Limit)
	if err != nil {
		return OutboxDeadLetterPage{}, err
	}
	letters := make([]OutboxDeadLetter, 0, min(len(rows), options.Limit))
	for index, row := range rows {
		if index == options.Limit {
			break
		}
		letter, err := outboxDeadLetterFromRow(row)
		if err != nil {
			return OutboxDeadLetterPage{}, err
		}
		if letter.TenantID != r.tenantID {
			return OutboxDeadLetterPage{}, NewStoreError("list_outbox_dead_letters returned an event for another tenant", ErrorOptions{})
		}
		letters = append(letters, letter)
	}
	page := OutboxDeadLetterPage{Items: letters}
	if len(rows) > options.Limit && len(letters) > 0 {
		last := letters[len(letters)-1]
		page.NextCursor = &OutboxDeadLetterCursor{CreatedAt: last.CreatedAt, EventID: last.EventID}
	}
	return page, nil
}

func (r *PostgresStorageRepository) Requeue(ctx context.Context, eventID string) (bool, error) {
	normalized, err := postgresOutboxEventID(eventID)
	if err != nil {
		return false, err
	}
	rows, callErr := r.client.Call(ctx, "requeue_outbox_dead_letter", r.tenantUUID, normalized)
	return postgresBooleanResult(rows, "requeue_outbox_dead_letter", callErr)
}

func (r *PostgresStorageRepository) GetUsageCharge(ctx context.Context, chargeID string) (*UsageChargeExport, error) {
	typedChargeID, err := postgresUUID(chargeID, "usage charge ID")
	if err != nil {
		return nil, err
	}
	rows, err := r.client.Call(ctx, "export_usage_charge", typedChargeID)
	if err != nil {
		return nil, err
	}
	payload, err := optionalJSONScalar(rows, "export_usage_charge")
	if err != nil || payload == nil {
		return nil, err
	}
	available, err := rowBool(payload, "payload_available", "export_usage_charge")
	if err != nil {
		return nil, err
	}
	if !available {
		return nil, NewStoreError("usage charge payload expired before export", ErrorOptions{})
	}
	result, err := usageChargeExportFromPayload(payload)
	if err != nil {
		return nil, err
	}
	if result.TenantID != r.tenantID {
		return nil, NewStoreError("export_usage_charge returned another tenant's payload", ErrorOptions{})
	}
	return &result, nil
}

func (r *PostgresStorageRepository) GetBillingEventPayload(ctx context.Context, eventID string) (*BillingEventPayloadExport, error) {
	typedEventID, err := postgresUUID(eventID, "billing event ID")
	if err != nil {
		return nil, err
	}
	rows, err := r.client.Call(ctx, "export_billing_event_payload", typedEventID)
	if err != nil {
		return nil, err
	}
	payload, err := optionalJSONScalar(rows, "export_billing_event_payload")
	if err != nil || payload == nil {
		return nil, err
	}
	result, err := billingPayloadExportFromPayload(payload)
	if err != nil {
		return nil, err
	}
	if result.TenantID != r.tenantID {
		return nil, NewStoreError("export_billing_event_payload returned another tenant's payload", ErrorOptions{})
	}
	return &result, nil
}

func (r *PostgresStorageRepository) ArchiveBillingEventPayload(ctx context.Context, eventID, objectKey string, objectVersion *string) (bool, error) {
	typedEventID, err := postgresUUID(eventID, "billing event ID")
	if err != nil {
		return false, err
	}
	objectKey = strings.TrimSpace(objectKey)
	if objectKey == "" {
		return false, outboxConfigError("billing archive object key is required")
	}
	version := pgtype.Text{Valid: false}
	if objectVersion != nil {
		normalized := strings.TrimSpace(*objectVersion)
		if normalized != "" {
			version = pgtype.Text{String: normalized, Valid: true}
		}
	}
	rows, callErr := r.client.Call(ctx, "archive_billing_event_payload", typedEventID, objectKey, version)
	return postgresBooleanResult(rows, "archive_billing_event_payload", callErr)
}

func (r *PostgresStorageRepository) outboxClaimArguments(event OutboxEvent) (int64, pgtype.UUID, error) {
	tenantID, err := normalizeTenantID(event.TenantID)
	if err != nil || tenantID != r.tenantID {
		return 0, pgtype.UUID{}, outboxConfigError("outbox event tenant does not match repository tenant")
	}
	eventID, err := postgresOutboxEventID(event.EventID)
	if err != nil {
		return 0, pgtype.UUID{}, err
	}
	claimToken, err := postgresUUID(event.ClaimToken, "outbox claim token")
	if err != nil {
		return 0, pgtype.UUID{}, err
	}
	return eventID, claimToken, nil
}

func validateOutboxClaim(topics []string, limit, leaseSeconds int) error {
	if limit < 1 || limit > 1_000 {
		return outboxConfigError("outbox claim limit must be between 1 and 1000")
	}
	if leaseSeconds < 1 || leaseSeconds > 3_600 {
		return outboxConfigError("lease seconds must be between 1 and 3600")
	}
	if len(topics) < 1 || len(topics) > 64 {
		return outboxConfigError("outbox claim must contain between 1 and 64 topics")
	}
	for _, topic := range topics {
		if strings.TrimSpace(topic) == "" || len(topic) > 255 {
			return outboxConfigError("outbox topics must contain between 1 and 255 characters")
		}
	}
	return nil
}

func outboxEventFromRow(row map[string]any) (OutboxEvent, error) {
	const operation = "claim_outbox_events"
	eventID, err := requiredRowText(row, "event_id", operation)
	if err != nil {
		return OutboxEvent{}, err
	}
	eventID, err = positiveEventID(eventID)
	if err != nil {
		return OutboxEvent{}, err
	}
	tenantID, err := requiredRowText(row, "tenant_id", operation)
	if err != nil {
		return OutboxEvent{}, err
	}
	tenantID, err = normalizedUUID(tenantID, "outbox tenant ID")
	if err != nil {
		return OutboxEvent{}, err
	}
	topic, err := requiredRowText(row, "topic", operation)
	if err != nil {
		return OutboxEvent{}, err
	}
	aggregateType, err := requiredRowText(row, "aggregate_type", operation)
	if err != nil {
		return OutboxEvent{}, err
	}
	aggregateID, err := requiredRowText(row, "aggregate_id", operation)
	if err != nil {
		return OutboxEvent{}, err
	}
	aggregateID, err = normalizedUUID(aggregateID, "outbox aggregate ID")
	if err != nil {
		return OutboxEvent{}, err
	}
	claimToken, err := requiredRowText(row, "claim_token", operation)
	if err != nil {
		return OutboxEvent{}, err
	}
	claimToken, err = normalizedUUID(claimToken, "outbox claim token")
	if err != nil {
		return OutboxEvent{}, err
	}
	payloadVersion, err := nonnegativeRowInt(row, "payload_version", operation)
	if err != nil {
		return OutboxEvent{}, err
	}
	attemptCount, err := nonnegativeRowInt(row, "attempt_count", operation)
	if err != nil {
		return OutboxEvent{}, err
	}
	payload, err := requiredJSONMap(rowValue(row, "payload"), operation+".payload")
	if err != nil {
		return OutboxEvent{}, err
	}
	createdAt, err := rowTime(row, "created_at", operation)
	if err != nil {
		return OutboxEvent{}, err
	}
	return OutboxEvent{
		EventID: eventID, TenantID: tenantID, Topic: topic, AggregateType: aggregateType,
		AggregateID: aggregateID, PayloadVersion: payloadVersion, Payload: payload,
		ClaimToken: claimToken, AttemptCount: attemptCount, CreatedAt: createdAt,
	}, nil
}

func outboxDeadLetterFromRow(row map[string]any) (OutboxDeadLetter, error) {
	const operation = "list_outbox_dead_letters"
	eventID, err := requiredRowText(row, "event_id", operation)
	if err != nil {
		return OutboxDeadLetter{}, err
	}
	eventID, err = positiveEventID(eventID)
	if err != nil {
		return OutboxDeadLetter{}, err
	}
	tenantID, err := requiredRowText(row, "tenant_id", operation)
	if err != nil {
		return OutboxDeadLetter{}, err
	}
	tenantID, err = normalizedUUID(tenantID, "outbox tenant ID")
	if err != nil {
		return OutboxDeadLetter{}, err
	}
	topic, err := requiredRowText(row, "topic", operation)
	if err != nil {
		return OutboxDeadLetter{}, err
	}
	aggregateType, err := requiredRowText(row, "aggregate_type", operation)
	if err != nil {
		return OutboxDeadLetter{}, err
	}
	aggregateID, err := requiredRowText(row, "aggregate_id", operation)
	if err != nil {
		return OutboxDeadLetter{}, err
	}
	aggregateID, err = normalizedUUID(aggregateID, "outbox aggregate ID")
	if err != nil {
		return OutboxDeadLetter{}, err
	}
	payloadVersion, err := nonnegativeRowInt(row, "payload_version", operation)
	if err != nil {
		return OutboxDeadLetter{}, err
	}
	attemptCount, err := nonnegativeRowInt(row, "attempt_count", operation)
	if err != nil {
		return OutboxDeadLetter{}, err
	}
	createdAt, err := rowTime(row, "created_at", operation)
	if err != nil {
		return OutboxDeadLetter{}, err
	}
	updatedAt, err := rowTime(row, "updated_at", operation)
	if err != nil {
		return OutboxDeadLetter{}, err
	}
	lastError := optionalTextPointer(row, "last_error")
	return OutboxDeadLetter{
		EventID: eventID, TenantID: tenantID, Topic: topic, AggregateType: aggregateType,
		AggregateID: aggregateID, PayloadVersion: payloadVersion, AttemptCount: attemptCount,
		LastError: lastError, CreatedAt: createdAt, UpdatedAt: updatedAt,
	}, nil
}

func usageChargeExportFromPayload(row map[string]any) (UsageChargeExport, error) {
	const operation = "export_usage_charge"
	required := func(key string) (string, error) { return requiredRowText(row, key, operation) }
	tenantID, err := required("tenant_id")
	if err != nil {
		return UsageChargeExport{}, err
	}
	tenantID, err = normalizedUUID(tenantID, "usage export tenant ID")
	if err != nil {
		return UsageChargeExport{}, err
	}
	chargeID, err := required("charge_id")
	if err != nil {
		return UsageChargeExport{}, err
	}
	chargeID, err = normalizedUUID(chargeID, "usage export charge ID")
	if err != nil {
		return UsageChargeExport{}, err
	}
	accountID, err := required("account_id")
	if err != nil {
		return UsageChargeExport{}, err
	}
	accountID, err = normalizedUUID(accountID, "usage export account ID")
	if err != nil {
		return UsageChargeExport{}, err
	}
	subjectID, err := required("subject_id")
	if err != nil {
		return UsageChargeExport{}, err
	}
	subjectID, err = normalizedUUID(subjectID, "usage export subject ID")
	if err != nil {
		return UsageChargeExport{}, err
	}
	operationName, err := required("operation")
	if err != nil {
		return UsageChargeExport{}, err
	}
	requested, err := rowAmount(row, "requested", operation)
	if err != nil {
		return UsageChargeExport{}, err
	}
	charged, err := rowAmount(row, "charged", operation)
	if err != nil {
		return UsageChargeExport{}, err
	}
	allowanceRequested, err := rowAmount(row, "allowance_requested", operation)
	if err != nil {
		return UsageChargeExport{}, err
	}
	allowanceCovered, err := rowAmount(row, "allowance_covered", operation)
	if err != nil {
		return UsageChargeExport{}, err
	}
	dispositionText, err := required("billing_disposition")
	if err != nil {
		return UsageChargeExport{}, err
	}
	disposition := BillingDisposition(dispositionText)
	if disposition != BillingDispositionBillable && disposition != BillingDispositionRecordOnly {
		return UsageChargeExport{}, NewStoreError("export_usage_charge returned an invalid billing_disposition", ErrorOptions{})
	}
	measures, err := requiredJSONMap(rowValue(row, "measures"), operation+".measures")
	if err != nil {
		return UsageChargeExport{}, err
	}
	dimensions, err := requiredJSONMap(rowValue(row, "dimensions"), operation+".dimensions")
	if err != nil {
		return UsageChargeExport{}, err
	}
	metadata, err := requiredJSONMap(rowValue(row, "metadata"), operation+".metadata")
	if err != nil {
		return UsageChargeExport{}, err
	}
	pricing, err := requiredJSONMap(rowValue(row, "pricing_snapshot"), operation+".pricing_snapshot")
	if err != nil {
		return UsageChargeExport{}, err
	}
	idempotencyKey, err := required("idempotency_key")
	if err != nil {
		return UsageChargeExport{}, err
	}
	requestDigest, err := required("request_digest")
	if err != nil {
		return UsageChargeExport{}, err
	}
	eventAt, err := rowTime(row, "event_at", operation)
	if err != nil {
		return UsageChargeExport{}, err
	}
	createdAt, err := rowTime(row, "created_at", operation)
	if err != nil {
		return UsageChargeExport{}, err
	}
	catalogRevisionID, err := optionalNormalizedUUIDPointer(row, "catalog_revision_id", "usage export catalog revision ID")
	if err != nil {
		return UsageChargeExport{}, err
	}
	planID, err := optionalNormalizedUUIDPointer(row, "plan_id", "usage export plan ID")
	if err != nil {
		return UsageChargeExport{}, err
	}
	ledgerEntryID, err := optionalNormalizedUUIDPointer(row, "ledger_entry_id", "usage export ledger entry ID")
	if err != nil {
		return UsageChargeExport{}, err
	}
	correctionOfChargeID, err := optionalNormalizedUUIDPointer(row, "correction_of_charge_id", "usage export correction charge ID")
	if err != nil {
		return UsageChargeExport{}, err
	}
	return UsageChargeExport{
		TenantID: tenantID, ChargeID: chargeID, AccountID: accountID, SubjectID: subjectID,
		Operation: operationName, Feature: optionalTextPointer(row, "feature"), Model: optionalTextPointer(row, "model"),
		Region: optionalTextPointer(row, "region"), Measures: measures, Dimensions: dimensions, Metadata: metadata,
		Requested: QuantizeMoney(requested), Charged: QuantizeMoney(charged), AllowanceRequested: QuantizeMoney(allowanceRequested),
		AllowanceCovered: QuantizeMoney(allowanceCovered), BillingDisposition: disposition,
		CatalogRevisionID: catalogRevisionID, PlanID: planID,
		RateCardKey: optionalTextPointer(row, "rate_card_key"), PricingSnapshot: pricing,
		LedgerEntryID: ledgerEntryID, CorrectionOfChargeID: correctionOfChargeID,
		IdempotencyKey: idempotencyKey, RequestDigest: requestDigest, EventAt: eventAt, CreatedAt: createdAt,
	}, nil
}

func billingPayloadExportFromPayload(row map[string]any) (BillingEventPayloadExport, error) {
	const operation = "export_billing_event_payload"
	required := func(key string) (string, error) { return requiredRowText(row, key, operation) }
	tenantID, err := required("tenant_id")
	if err != nil {
		return BillingEventPayloadExport{}, err
	}
	tenantID, err = normalizedUUID(tenantID, "billing export tenant ID")
	if err != nil {
		return BillingEventPayloadExport{}, err
	}
	eventID, err := required("event_id")
	if err != nil {
		return BillingEventPayloadExport{}, err
	}
	eventID, err = normalizedUUID(eventID, "billing export event ID")
	if err != nil {
		return BillingEventPayloadExport{}, err
	}
	provider, err := required("provider")
	if err != nil {
		return BillingEventPayloadExport{}, err
	}
	environment, err := required("provider_environment")
	if err != nil {
		return BillingEventPayloadExport{}, err
	}
	providerEventID, err := required("provider_event_id")
	if err != nil {
		return BillingEventPayloadExport{}, err
	}
	eventType, err := required("event_type")
	if err != nil {
		return BillingEventPayloadExport{}, err
	}
	status, err := required("status")
	if err != nil {
		return BillingEventPayloadExport{}, err
	}
	receivedAt, err := rowTime(row, "received_at", operation)
	if err != nil {
		return BillingEventPayloadExport{}, err
	}
	completedAt, err := optionalRowTime(row, "completed_at", operation)
	if err != nil {
		return BillingEventPayloadExport{}, err
	}
	archivedAt, err := optionalRowTime(row, "archived_at", operation)
	if err != nil {
		return BillingEventPayloadExport{}, err
	}
	var envelope map[string]any
	if rowValue(row, "envelope") != nil {
		envelope, err = requiredJSONMap(rowValue(row, "envelope"), operation+".envelope")
		if err != nil {
			return BillingEventPayloadExport{}, err
		}
	}
	return BillingEventPayloadExport{
		TenantID: tenantID, EventID: eventID, Provider: provider, ProviderEnvironment: environment,
		ProviderEventID: providerEventID, EventType: eventType, Status: status, ReceivedAt: receivedAt,
		CompletedAt: completedAt, Envelope: envelope, ObjectKey: optionalTextPointer(row, "object_key"),
		ObjectVersion: optionalTextPointer(row, "object_version"), ArchivedAt: archivedAt,
	}, nil
}

func postgresBooleanResult(rows []map[string]any, operation string, callErr error) (bool, error) {
	if callErr != nil {
		return false, callErr
	}
	row, err := exactlyOneRow(rows, operation)
	if err != nil {
		return false, err
	}
	value, err := firstScalar(row, operation)
	if err != nil {
		return false, err
	}
	return scalarBool(value, operation)
}

func exactlyOneRow(rows []map[string]any, operation string) (map[string]any, error) {
	if len(rows) != 1 || rows[0] == nil {
		return nil, NewStoreError(operation+" returned an invalid row count", ErrorOptions{})
	}
	return rows[0], nil
}

func optionalJSONScalar(rows []map[string]any, operation string) (map[string]any, error) {
	if len(rows) == 0 {
		return nil, nil
	}
	row, err := exactlyOneRow(rows, operation)
	if err != nil {
		return nil, err
	}
	value, err := firstScalar(row, operation)
	if err != nil || value == nil {
		return nil, err
	}
	return requiredJSONMap(value, operation+".payload")
}

func requiredJSONMap(value any, field string) (map[string]any, error) {
	if raw, ok := value.(json.RawMessage); ok {
		value = []byte(raw)
	}
	mapped, err := jsonMap(value, field)
	if err != nil {
		return nil, err
	}
	if mapped == nil {
		return nil, NewStoreError("PostgreSQL returned a null "+field, ErrorOptions{})
	}
	return mapped, nil
}

func nonnegativeRowInt(row map[string]any, key, operation string) (int, error) {
	value, err := rowInt(row, key, operation)
	if err != nil {
		return 0, err
	}
	if value < 0 {
		return 0, NewStoreError(operation+" returned a negative "+key, ErrorOptions{})
	}
	return value, nil
}

func optionalTextPointer(row map[string]any, key string) *string {
	if rowValue(row, key) == nil {
		return nil
	}
	value := optionalRowText(row, key)
	return &value
}

func optionalNormalizedUUIDPointer(row map[string]any, key, name string) (*string, error) {
	if rowValue(row, key) == nil {
		return nil, nil
	}
	value, err := requiredRowText(row, key, name)
	if err != nil {
		return nil, err
	}
	value, err = normalizedUUID(value, name)
	if err != nil {
		return nil, err
	}
	return &value, nil
}

var positiveEventIDPattern = regexp.MustCompile(`^[1-9][0-9]*$`)

func positiveEventID(value string) (string, error) {
	if !positiveEventIDPattern.MatchString(value) {
		return "", outboxConfigError("outbox event ID must be a positive integer string")
	}
	return value, nil
}

func postgresOutboxEventID(value string) (int64, error) {
	normalized, err := positiveEventID(value)
	if err != nil {
		return 0, err
	}
	parsed, err := strconv.ParseInt(normalized, 10, 64)
	if err != nil {
		return 0, outboxConfigError("outbox event ID must fit PostgreSQL bigint")
	}
	return parsed, nil
}

func normalizedUUID(value, name string) (string, error) {
	normalized, err := normalizeTenantID(value)
	if err != nil {
		return "", NewError(name+" must be a UUID", ErrorOptions{Code: ErrorCodeConfig, Category: ErrorCategoryInvalidRequest, Cause: err})
	}
	return normalized, nil
}
