// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"strings"
	"time"
)

// PostgresStore implements the durable checkout-intent boundary used by
// CommerceService. It delegates every state transition to the existing SQL
// functions, including catalog/provider-reference eligibility checks.
var _ CommerceStore = (*PostgresStore)(nil)

// CreateOrGetCheckoutIntent creates or replays one catalog-bound checkout
// intent. The request digest follows the shared Python/JavaScript contract so
// a stable operation key can safely resume across SDKs.
func (s *PostgresStore) CreateOrGetCheckoutIntent(ctx context.Context, input CheckoutIntentCreate) (intent CheckoutIntent, err error) {
	if s == nil || s.client == nil {
		return intent, NewStoreError("PostgreSQL commerce store is closed", ErrorOptions{Code: ErrorCodeStoreClosed})
	}
	if input.SubjectID, err = requireText(input.SubjectID, "checkout subject ID"); err != nil {
		return intent, err
	}
	if input.AccountID, err = requireText(input.AccountID, "checkout account ID"); err != nil {
		return intent, err
	}
	if input.Provider, err = requireText(input.Provider, "checkout provider"); err != nil {
		return intent, err
	}
	if input.IdempotencyKey, err = requireStableKey(input.IdempotencyKey, "checkout idempotency key"); err != nil {
		return intent, err
	}
	if input.CheckoutKind != "subscription" && input.CheckoutKind != "credit_topup" {
		return intent, NewError("checkout kind must be subscription or credit_topup", ErrorOptions{Code: ErrorCodeConfig, Category: ErrorCategoryInvalidRequest})
	}
	if input.ProductKey, err = requireText(input.ProductKey, "checkout product key"); err != nil {
		return intent, err
	}
	if input.Quantity < 1 {
		return intent, NewError("checkout quantity must be positive", ErrorOptions{Code: ErrorCodeInvalidOfferQuantity, Category: ErrorCategoryInvalidRequest})
	}
	if input.ExpiresAt.IsZero() {
		input.ExpiresAt = time.Now().UTC().Add(24 * time.Hour)
	}
	if !input.ExpiresAt.After(time.Now().UTC()) {
		return intent, NewError("checkout expiration must be in the future", ErrorOptions{Code: ErrorCodeConfig, Category: ErrorCategoryInvalidRequest})
	}
	typedSubjectID, err := postgresUUID(input.SubjectID, "checkout subject ID")
	if err != nil {
		return intent, err
	}
	digest, err := checkoutRequestDigest(input)
	if err != nil {
		return intent, err
	}
	if input.RequestDigest != "" && !strings.EqualFold(input.RequestDigest, hex.EncodeToString(digest[:])) {
		return intent, NewError("checkout request digest does not match its resolved request", ErrorOptions{Code: ErrorCodeCheckoutConflict, Category: ErrorCategoryConflict})
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, callErr := tx.Call(
			ctx,
			"create_checkout_intent",
			typedSubjectID,
			input.Provider,
			input.IdempotencyKey,
			input.CheckoutKind,
			input.ProductKey,
			digest[:],
			input.ExpiresAt.UTC(),
			nil,
			nil,
			nullableText(strings.ToUpper(strings.TrimSpace(input.Region))),
		)
		if callErr != nil {
			return callErr
		}
		intentID, valueErr := checkoutIntentIDFromRows(rows)
		if valueErr != nil {
			return valueErr
		}
		loaded, loadErr := s.checkoutIntent(ctx, tx, intentID, input.SubjectID)
		if loadErr != nil {
			return loadErr
		}
		if loaded == nil {
			return NewStoreError("checkout intent could not be read after creation", ErrorOptions{Indeterminate: true, Details: map[string]any{"checkout_intent_id": intentID}})
		}
		intent = *loaded
		return nil
	})
	return intent, err
}

func checkoutIntentIDFromRows(rows []map[string]any) (string, error) {
	row, err := rowRequired(rows, "create_checkout_intent")
	if err != nil {
		return "", err
	}
	intentID, err := requiredRowText(row, "create_checkout_intent", "create_checkout_intent")
	if err == nil {
		return intentID, nil
	}
	// A scalar function's output column name is driver-dependent.
	value, scalarErr := firstScalar(row, "create_checkout_intent")
	if scalarErr != nil {
		return "", err
	}
	return textFromScalar(value, "create_checkout_intent")
}

// UpdateCheckoutIntent advances a subject-scoped checkout intent after a
// provider session is created. Terminal state transitions are rejected by SQL.
func (s *PostgresStore) UpdateCheckoutIntent(ctx context.Context, intentID, subjectID string, update CheckoutIntentUpdate) (err error) {
	if s == nil || s.client == nil {
		return NewStoreError("PostgreSQL commerce store is closed", ErrorOptions{Code: ErrorCodeStoreClosed})
	}
	if intentID, err = requireText(intentID, "checkout intent ID"); err != nil {
		return err
	}
	if subjectID, err = requireText(subjectID, "checkout subject ID"); err != nil {
		return err
	}
	if update.Status != "" && update.Status != "open" && update.Status != "completed" && update.Status != "failed" && update.Status != "expired" {
		return NewError("invalid checkout intent status", ErrorOptions{Code: ErrorCodeConfig, Category: ErrorCategoryInvalidRequest})
	}
	typedIntentID, err := postgresUUID(intentID, "checkout intent ID")
	if err != nil {
		return err
	}
	return s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		loaded, loadErr := s.checkoutIntent(ctx, tx, intentID, subjectID)
		if loadErr != nil {
			return loadErr
		}
		if loaded == nil {
			return NewError("checkout intent was not found", ErrorOptions{Code: ErrorCodeCommerceResourceNotFound, Category: ErrorCategoryNotFound})
		}
		rows, callErr := tx.Call(ctx, "advance_checkout_intent", typedIntentID, nullableText(update.Status), nullableText(update.ProviderSessionID), nullableText(update.ProviderURL))
		if callErr != nil {
			return callErr
		}
		row, rowErr := rowRequired(rows, "advance_checkout_intent")
		if rowErr != nil {
			return rowErr
		}
		value, valueErr := firstScalar(row, "advance_checkout_intent")
		if valueErr != nil {
			return valueErr
		}
		advanced, valueErr := scalarBool(value, "advance_checkout_intent")
		if valueErr != nil {
			return valueErr
		}
		if !advanced {
			return NewError("checkout intent transition was rejected", ErrorOptions{Code: ErrorCodeCheckoutConflict, Category: ErrorCategoryConflict, Details: map[string]any{"checkout_intent_id": intentID}})
		}
		return nil
	})
}

// GetCheckoutIntent returns a subject-scoped intent, if it exists in the
// current tenant/provider environment.
func (s *PostgresStore) GetCheckoutIntent(ctx context.Context, intentID, subjectID string) (result *CheckoutIntent, err error) {
	if s == nil || s.client == nil {
		return nil, NewStoreError("PostgreSQL commerce store is closed", ErrorOptions{Code: ErrorCodeStoreClosed})
	}
	if intentID, err = requireText(intentID, "checkout intent ID"); err != nil {
		return nil, err
	}
	if subjectID, err = requireText(subjectID, "checkout subject ID"); err != nil {
		return nil, err
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		result, err = s.checkoutIntent(ctx, tx, intentID, subjectID)
		return err
	})
	return result, err
}

func (s *PostgresStore) checkoutIntent(ctx context.Context, tx *PostgresTransaction, intentID, subjectID string) (*CheckoutIntent, error) {
	typedIntentID, err := postgresUUID(intentID, "checkout intent ID")
	if err != nil {
		return nil, err
	}
	typedSubjectID, err := postgresUUID(subjectID, "checkout subject ID")
	if err != nil {
		return nil, err
	}
	rows, err := tx.Call(ctx, "get_checkout_intent", typedIntentID, typedSubjectID)
	if err != nil {
		return nil, err
	}
	return checkoutIntentFromRows(rows)
}

// checkoutIntentFromRows keeps projection validation independent from the
// transport. PostgreSQL remains the integration boundary, while malformed or
// incomplete rows can be verified deterministically without mocking pgx.
func checkoutIntentFromRows(rows []map[string]any) (*CheckoutIntent, error) {
	if len(rows) == 0 {
		return nil, nil
	}
	row, err := rowRequired(rows, "get_checkout_intent")
	if err != nil {
		return nil, err
	}
	id, err := requiredRowText(row, "id", "get_checkout_intent")
	if err != nil {
		return nil, err
	}
	userID, err := requiredRowText(row, "subject_id", "get_checkout_intent")
	if err != nil {
		return nil, err
	}
	provider, err := requiredRowText(row, "provider", "get_checkout_intent")
	if err != nil {
		return nil, err
	}
	kind, err := requiredRowText(row, "checkout_kind", "get_checkout_intent")
	if err != nil {
		return nil, err
	}
	productKey, err := requiredRowText(row, "product_key", "get_checkout_intent")
	if err != nil {
		return nil, err
	}
	status, err := requiredRowText(row, "status", "get_checkout_intent")
	if err != nil {
		return nil, err
	}
	expiresAt, err := rowTime(row, "expires_at", "get_checkout_intent")
	if err != nil {
		return nil, err
	}
	createdAt, err := rowTime(row, "created_at", "get_checkout_intent")
	if err != nil {
		return nil, err
	}
	updatedAt, err := rowTime(row, "updated_at", "get_checkout_intent")
	if err != nil {
		return nil, err
	}
	requestDigest, err := checkoutRowDigest(row)
	if err != nil {
		return nil, err
	}
	return &CheckoutIntent{
		ID:                id,
		SubjectID:         userID,
		OfferKey:          productKey,
		Provider:          provider,
		CheckoutKind:      kind,
		ProductKey:        productKey,
		ProviderSessionID: optionalRowText(row, "provider_session_id"),
		ProviderURL:       optionalRowText(row, "checkout_url"),
		Status:            status,
		IdempotencyKey:    optionalRowText(row, "operation_key"),
		RequestDigest:     requestDigest,
		ExpiresAt:         expiresAt,
		CreatedAt:         createdAt,
		UpdatedAt:         updatedAt,
	}, nil
}

func checkoutRequestDigest(input CheckoutIntentCreate) ([32]byte, error) {
	// This exact camelCase compact map is shared with the Python and
	// JavaScript SDKs. Subject ownership and idempotency are enforced by SQL's
	// subject-scoped operation-key constraint; including them here would make
	// cross-SDK replays falsely conflict.
	kind := input.CheckoutKind
	if kind == "credit_topup" {
		kind = "topup"
	}
	payload := map[string]any{
		"accountId":    input.AccountID,
		"checkoutKind": kind,
		"offerKey":     input.ProductKey,
		"provider":     input.Provider,
		"quantity":     input.Quantity,
	}
	encoded, err := json.Marshal(payload)
	if err != nil {
		return [32]byte{}, NewError("serialize checkout request", ErrorOptions{Code: ErrorCodeConfig, Category: ErrorCategoryInvalidRequest, Cause: err})
	}
	return sha256.Sum256(encoded), nil
}

func textFromScalar(value any, operation string) (string, error) {
	if text, ok := textValue(value); ok && strings.TrimSpace(text) != "" {
		return strings.TrimSpace(text), nil
	}
	return "", NewStoreError(operation+" returned an invalid identifier", ErrorOptions{})
}

func checkoutRowDigest(row map[string]any) (string, error) {
	value, exists := row["request_digest"]
	if !exists || value == nil {
		return "", NewStoreError("get_checkout_intent returned no request digest", ErrorOptions{})
	}
	switch raw := value.(type) {
	case []byte:
		if len(raw) != sha256.Size {
			return "", NewStoreError("get_checkout_intent returned an invalid request digest", ErrorOptions{})
		}
		return hex.EncodeToString(raw), nil
	case string:
		decoded, err := hex.DecodeString(raw)
		if err != nil || len(decoded) != sha256.Size {
			return "", NewStoreError("get_checkout_intent returned an invalid request digest", ErrorOptions{Cause: err})
		}
		return strings.ToLower(raw), nil
	default:
		return "", NewStoreError("get_checkout_intent returned an invalid request digest", ErrorOptions{})
	}
}
