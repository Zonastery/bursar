// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import (
	"context"
	"encoding/json"
	"strings"
)

// PostgresStore also implements BillingStore's durable provider-event claim
// lifecycle. The mutations remain the existing Bursar SQL RPCs under the same
// tenant-scoped transaction used for credit accounting.
var _ BillingStore = (*PostgresStore)(nil)

// ClaimBillingEvent atomically claims a verified provider event. The database
// fingerprints provider, event ID, type, and envelope so retries are safe and
// conflicting redeliveries are never acknowledged as duplicates.
func (s *PostgresStore) ClaimBillingEvent(ctx context.Context, event BillingEvent, envelope map[string]any) (result BillingEventClaim, err error) {
	if s == nil || s.client == nil {
		return result, NewStoreError("PostgreSQL billing store is closed", ErrorOptions{Code: ErrorCodeStoreClosed})
	}
	if err := event.Validate(); err != nil {
		return result, NewError("invalid billing event", ErrorOptions{Code: ErrorCodeBilling, Category: ErrorCategoryInvalidRequest, Cause: err})
	}
	// Claim envelopes are part of the provider-event idempotency fingerprint.
	// Start with a writable map even when callers do not supply extra context.
	payload := make(map[string]any, len(envelope)+12)
	for key, value := range envelope {
		payload[key] = value
	}
	payload["event_id"] = event.ID
	payload["event_type"] = string(event.Type)
	payload["provider"] = event.Provider
	payload["customer"] = event.Customer
	payload["subscription"] = event.Subscription
	payload["invoice"] = event.Invoice
	payload["payment"] = event.Payment
	payload["refund"] = event.Refund
	payload["dispute"] = event.Dispute
	payload["metadata"] = cloneAnyMap(event.Metadata)
	if len(event.RawPayload) > 0 && json.Valid(event.RawPayload) {
		payload["raw_event"] = json.RawMessage(append([]byte(nil), event.RawPayload...))
	}
	encoded, err := json.Marshal(payload)
	if err != nil {
		return result, NewError("serialize billing event envelope", ErrorOptions{Code: ErrorCodeBilling, Category: ErrorCategoryInvalidRequest, Cause: err})
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		// json.RawMessage keeps pgx on the JSON/JSONB codec path. A bare []byte
		// is encoded as bytea and cannot resolve this SQL RPC overload.
		rows, callErr := tx.Call(ctx, "claim_billing_event", event.Provider, event.ID, string(event.Type), json.RawMessage(encoded))
		if callErr != nil {
			return callErr
		}
		row, rowErr := rowRequired(rows, "claim_billing_event")
		if rowErr != nil {
			return rowErr
		}
		status := optionalRowText(row, "result")
		switch status {
		case "claimed":
			claimToken, tokenErr := requiredRowText(row, "claim_token", "claim_billing_event")
			if tokenErr != nil {
				return tokenErr
			}
			result = BillingEventClaim{State: BillingEventClaimed, ClaimToken: claimToken}
		case "duplicate":
			result = BillingEventClaim{State: BillingEventDuplicate}
		case "busy":
			result = BillingEventClaim{State: BillingEventBusy}
		case "invalid_request", "idempotency_conflict", "max_retries_exceeded":
			result = BillingEventClaim{State: BillingEventRejected, Reason: status}
		default:
			return NewStoreError("claim_billing_event returned an unsupported result", ErrorOptions{Details: map[string]any{"result": status}})
		}
		return nil
	})
	return result, err
}

// CompleteBillingEvent marks one claimed provider event completed. A false
// result is intentional when a claim lease has expired or been replaced.
func (s *PostgresStore) CompleteBillingEvent(ctx context.Context, provider, eventID, claimToken string) (completed bool, err error) {
	if s == nil || s.client == nil {
		return false, NewStoreError("PostgreSQL billing store is closed", ErrorOptions{Code: ErrorCodeStoreClosed})
	}
	provider, err = requireText(provider, "billing provider")
	if err != nil {
		return false, err
	}
	eventID, err = requireText(eventID, "billing event ID")
	if err != nil {
		return false, err
	}
	claimToken, err = requireText(claimToken, "billing claim token")
	if err != nil {
		return false, err
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, callErr := tx.Call(ctx, "complete_billing_event", provider, eventID, claimToken)
		if callErr != nil {
			return callErr
		}
		row, rowErr := rowRequired(rows, "complete_billing_event")
		if rowErr != nil {
			return rowErr
		}
		value, valueErr := firstScalar(row, "complete_billing_event")
		if valueErr != nil {
			return valueErr
		}
		completed, valueErr = scalarBool(value, "complete_billing_event")
		return valueErr
	})
	return completed, err
}

// FailBillingEvent records a bounded diagnostic and releases the active claim
// for provider retry. It never masks a lost/expired claim as a completed event.
func (s *PostgresStore) FailBillingEvent(ctx context.Context, provider, eventID, claimToken, diagnostic string) (failed bool, err error) {
	if s == nil || s.client == nil {
		return false, NewStoreError("PostgreSQL billing store is closed", ErrorOptions{Code: ErrorCodeStoreClosed})
	}
	provider, err = requireText(provider, "billing provider")
	if err != nil {
		return false, err
	}
	eventID, err = requireText(eventID, "billing event ID")
	if err != nil {
		return false, err
	}
	claimToken, err = requireText(claimToken, "billing claim token")
	if err != nil {
		return false, err
	}
	diagnostic = strings.TrimSpace(diagnostic)
	if len(diagnostic) > 8_192 {
		diagnostic = diagnostic[:8_192]
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, callErr := tx.Call(ctx, "fail_billing_event", provider, eventID, claimToken, nullableText(diagnostic))
		if callErr != nil {
			return callErr
		}
		row, rowErr := rowRequired(rows, "fail_billing_event")
		if rowErr != nil {
			return rowErr
		}
		value, valueErr := firstScalar(row, "fail_billing_event")
		if valueErr != nil {
			return valueErr
		}
		failed, valueErr = scalarBool(value, "fail_billing_event")
		return valueErr
	})
	return failed, err
}

// ResolveBillingEventAccount resolves explicit Bursar account metadata. The
// catalog/checkout flow always writes bursar_account_id, so this avoids a
// provider-side lookup or an unscoped customer identifier at webhook time.
func (s *PostgresStore) ResolveBillingEventAccount(_ context.Context, event BillingEvent) (string, error) {
	for _, metadata := range []map[string]any{
		event.Metadata,
		customerMetadata(event.Customer),
		subscriptionMetadata(event.Subscription),
		invoiceMetadata(event.Invoice),
		paymentMetadata(event.Payment),
	} {
		if metadata == nil {
			continue
		}
		for _, key := range []string{"bursar_account_id", "account_id"} {
			if value, ok := metadata[key].(string); ok && strings.TrimSpace(value) != "" {
				return strings.TrimSpace(value), nil
			}
		}
	}
	if event.Customer != nil && strings.TrimSpace(event.Customer.AccountID) != "" {
		return strings.TrimSpace(event.Customer.AccountID), nil
	}
	if event.Subscription != nil && strings.TrimSpace(event.Subscription.AccountID) != "" {
		return strings.TrimSpace(event.Subscription.AccountID), nil
	}
	return "", nil
}

func customerMetadata(value *BillingCustomer) map[string]any {
	if value == nil {
		return nil
	}
	return value.Metadata
}

func subscriptionMetadata(value *BillingSubscription) map[string]any {
	if value == nil {
		return nil
	}
	return value.Metadata
}

func invoiceMetadata(value *BillingInvoice) map[string]any {
	if value == nil {
		return nil
	}
	return value.Metadata
}

func paymentMetadata(value *BillingPayment) map[string]any {
	if value == nil {
		return nil
	}
	return value.Metadata
}
