// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import (
	"context"
	"encoding/json"
	"strings"

	"github.com/jackc/pgx/v5/pgtype"
)

// PostgresStore also implements BillingStore's durable provider-event claim
// lifecycle. The mutations remain the existing Bursar SQL RPCs under the same
// tenant-scoped transaction used for credit accounting.
var (
	_ BillingStore           = (*PostgresStore)(nil)
	_ BillingAccountResolver = (*PostgresStore)(nil)
)

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
	// Start from the same canonical document used by the Python and JavaScript
	// services, then allow a caller to add provider-neutral diagnostic fields.
	// Go struct aliases and raw provider payloads must not affect the hash.
	payload := billingEventClaimEnvelope(event)
	for key, value := range envelope {
		payload[key] = value
	}
	eventID := event.canonicalEventID()
	encoded, err := json.Marshal(payload)
	if err != nil {
		return result, NewError("serialize billing event envelope", ErrorOptions{Code: ErrorCodeBilling, Category: ErrorCategoryInvalidRequest, Cause: err})
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		// json.RawMessage keeps pgx on the JSON/JSONB codec path. A bare []byte
		// is encoded as bytea and cannot resolve this SQL RPC overload.
		rows, callErr := tx.Call(ctx, "claim_billing_event", event.Provider, eventID, string(event.Type), json.RawMessage(encoded))
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
			billingEventID, idErr := requiredRowText(row, "event_id", "claim_billing_event")
			if idErr != nil {
				return idErr
			}
			result = BillingEventClaim{State: BillingEventClaimed, ClaimToken: claimToken, BillingEventID: billingEventID}
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
	typedClaimToken, err := postgresUUID(claimToken, "billing claim token")
	if err != nil {
		return false, err
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, callErr := tx.Call(ctx, "complete_billing_event", provider, eventID, typedClaimToken)
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
	typedClaimToken, err := postgresUUID(claimToken, "billing claim token")
	if err != nil {
		return false, err
	}
	diagnostic = strings.TrimSpace(diagnostic)
	if len(diagnostic) > 8_192 {
		diagnostic = diagnostic[:8_192]
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, callErr := tx.Call(ctx, "fail_billing_event", provider, eventID, typedClaimToken, nullableText(diagnostic))
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

func postgresUUID(value, label string) (pgtype.UUID, error) {
	var result pgtype.UUID
	if err := result.Scan(strings.TrimSpace(value)); err != nil || !result.Valid {
		return pgtype.UUID{}, NewError(label+" must be a UUID", ErrorOptions{Code: ErrorCodeConfig, Category: ErrorCategoryInvalidRequest, Cause: err})
	}
	return result, nil
}

// nullablePostgresUUID preserves PostgreSQL's UUID parameter type even when
// the value is SQL NULL. Passing a nil interface or a Go string leaves pgx
// without the UUID OID needed to resolve schema functions reliably.
func nullablePostgresUUID(value, label string) (pgtype.UUID, error) {
	if strings.TrimSpace(value) == "" {
		return pgtype.UUID{Valid: false}, nil
	}
	return postgresUUID(value, label)
}

// ResolveBillingEventAccount resolves explicit Bursar account metadata. The
// catalog/checkout flow always writes bursar_account_id, so this avoids a
// provider-side lookup or an unscoped customer identifier at webhook time.
func (s *PostgresStore) ResolveBillingEventAccount(ctx context.Context, event BillingEvent) (string, error) {
	if strings.TrimSpace(event.AccountID) != "" {
		return strings.TrimSpace(event.AccountID), nil
	}
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
	if event.Customer != nil && event.Customer.canonicalProviderCustomerID() != "" {
		customer, err := s.GetBillingCustomerByProvider(ctx, event.Provider, event.Customer.canonicalProviderCustomerID())
		if err != nil {
			return "", err
		}
		if customer != nil {
			return customer.AccountID, nil
		}
	}
	if event.Subscription != nil && event.Subscription.canonicalProviderSubscriptionID() != "" {
		subscription, err := s.GetBillingSubscriptionByProvider(ctx, event.Provider, event.Subscription.canonicalProviderSubscriptionID())
		if err != nil {
			return "", err
		}
		if subscription != nil {
			return subscription.AccountID, nil
		}
	}
	providerPaymentID := ""
	if event.Payment != nil {
		providerPaymentID = event.Payment.canonicalProviderPaymentID()
	} else if event.Refund != nil {
		providerPaymentID = event.Refund.canonicalProviderPaymentID()
	} else if event.Dispute != nil {
		providerPaymentID = event.Dispute.canonicalProviderPaymentID()
	}
	if providerPaymentID != "" {
		payment, err := s.GetBillingPaymentByProvider(ctx, event.Provider, providerPaymentID)
		if err != nil {
			return "", err
		}
		if payment != nil {
			return payment.AccountID, nil
		}
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
