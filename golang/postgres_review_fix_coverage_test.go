// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import (
	"context"
	"strings"
	"testing"
	"time"

	"github.com/pashagolub/pgxmock/v5"
)

func TestBillingStateReviewFixTransactionValidation(t *testing.T) {
	ctx := context.Background()
	validID := "00000000-0000-4000-8000-000000000401"
	state := CommerceSubscription{
		AccountID: validID, Provider: "stripe", ProviderSubscriptionID: "sub-review-fix",
		OfferID: validID, Status: "active", ProviderUpdatedAt: time.Unix(1, 0).UTC(),
	}

	for _, test := range []struct {
		name   string
		mutate func(*CommerceSubscription)
	}{
		{"account UUID", func(value *CommerceSubscription) { value.AccountID = "not-a-uuid" }},
		{"offer UUID", func(value *CommerceSubscription) { value.OfferID = "not-a-uuid" }},
		{"metadata serialization", func(value *CommerceSubscription) { value.Metadata = map[string]any{"unsupported": make(chan int)} }},
	} {
		t.Run(test.name, func(t *testing.T) {
			value := state
			test.mutate(&value)
			if _, err := upsertBillingSubscriptionStateTx(ctx, &PostgresTransaction{}, value); err == nil {
				t.Fatal("invalid subscription state reached a successful transaction")
			}
		})
	}

	if _, err := upsertBillingSubscriptionStateTx(ctx, &PostgresTransaction{}, state); err == nil {
		t.Fatal("inactive transaction was accepted")
	}

	if got, err := validateBillingSubscriptionState(state); err != nil || got.AccountID != state.AccountID || got.OfferID != state.OfferID {
		t.Fatalf("validated subscription = %+v, error = %v", got, err)
	}
}

func TestBillingStateReviewFixUninitializedStoreBoundaries(t *testing.T) {
	ctx := context.Background()
	validID := "00000000-0000-4000-8000-000000000405"
	var store *PostgresStore

	if _, err := store.reconcileSubscriptionEntitlement(ctx, validID, validID, validID, "active", time.Unix(1, 0).UTC(), nil, true, "", "subscription_updated"); err == nil {
		t.Fatal("uninitialized store reconciled a subscription entitlement")
	}
	if _, err := store.PseudonymizeFinancialSubject(ctx, validID); err == nil {
		t.Fatal("uninitialized store pseudonymized a financial subject")
	}
}

func TestBillingStateReviewFixEntitlementInputContract(t *testing.T) {
	t.Parallel()
	ctx := context.Background()
	validID := "00000000-0000-4000-8000-000000000405"
	type entitlementInput struct {
		accountID, subscriptionID, eventID string
		status                             string
		updatedAt                          time.Time
		terminalPlanKey, reason            string
	}
	valid := func() entitlementInput {
		return entitlementInput{
			accountID: validID, subscriptionID: validID, eventID: validID,
			status: "active", updatedAt: time.Unix(1, 0).UTC(), reason: "subscription_updated",
		}
	}

	tests := []struct {
		name   string
		mutate func(*entitlementInput)
	}{
		{"account UUID", func(input *entitlementInput) { input.accountID = "not-a-uuid" }},
		{"subscription UUID", func(input *entitlementInput) { input.subscriptionID = "not-a-uuid" }},
		{"billing event UUID", func(input *entitlementInput) { input.eventID = "not-a-uuid" }},
		{"subscription status", func(input *entitlementInput) { input.status = "unknown" }},
		{"provider timestamp", func(input *entitlementInput) { input.updatedAt = time.Time{} }},
		{"terminal plan key length", func(input *entitlementInput) { input.terminalPlanKey = strings.Repeat("x", 256) }},
		{"blank reason", func(input *entitlementInput) { input.reason = " " }},
		{"reason length", func(input *entitlementInput) { input.reason = strings.Repeat("x", 256) }},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			input := valid()
			test.mutate(&input)
			if _, err := (*PostgresStore)(nil).reconcileSubscriptionEntitlement(ctx, input.accountID, input.subscriptionID, input.eventID, input.status, input.updatedAt, nil, true, input.terminalPlanKey, input.reason); err == nil {
				t.Fatal("invalid entitlement reconciliation input was accepted")
			}
		})
	}
}

func TestCommerceStateReviewFixTransactionErrors(t *testing.T) {
	ctx := context.Background()
	tx := &PostgresTransaction{}

	if _, err := getOpenBillingSubscriptionChangeTx(ctx, tx, "stripe", "sub-review-fix"); err == nil {
		t.Fatal("inactive transaction returned an open change")
	}
	if err := advanceBillingSubscriptionChangeTx(ctx, tx, "17", 17, "applied", nil, nil); err == nil {
		t.Fatal("inactive transaction advanced a subscription change")
	}

	state := CommerceSubscription{
		AccountID: "00000000-0000-4000-8000-000000000402", Provider: "stripe",
		ProviderSubscriptionID: "sub-review-fix", OfferID: "00000000-0000-4000-8000-000000000402",
		Status: "active", ProviderUpdatedAt: time.Unix(1, 0).UTC(),
	}
	if err := (*PostgresStore)(nil).applyOpenBillingSubscriptionChange(ctx, state.Provider, state.ProviderSubscriptionID, state.OfferID); err == nil {
		t.Fatal("uninitialized store applied a plan change")
	}
	if err := (*PostgresStore)(nil).applyOpenBillingSubscriptionChange(ctx, "", "", ""); err == nil {
		t.Fatal("invalid plan change state was accepted")
	}
}

func TestCommerceStateReviewFixTransactionSuccessAndRejection(t *testing.T) {
	ctx := context.Background()
	offerID := "00000000-0000-4000-8000-000000000403"
	revisionID := "00000000-0000-4000-8000-000000000404"
	mock, transaction := newReviewFixTransaction(t)
	mock.ExpectQuery("SELECT * FROM get_open_billing_subscription_change($1, $2)").
		WithArgs("stripe", "sub-review-fix").
		WillReturnRows(pgxmock.NewRows([]string{
			"id", "subscription_id", "to_offer_id", "to_catalog_revision_id",
			"effective_at", "effective_behavior", "state", "proration_behavior",
			"created_at", "updated_at", "idempotency_key",
		}).AddRow(
			"19", "sub-review-fix", offerID, revisionID,
			time.Unix(1, 0).UTC(), "renewal", "scheduled", "none",
			time.Unix(1, 0).UTC(), time.Unix(2, 0).UTC(), "review-fix",
		)).RowsWillBeClosed()
	mock.ExpectQuery("SELECT * FROM get_catalog_offer_context($1, $2)").
		WithArgs(pgxmock.AnyArg(), pgxmock.AnyArg()).
		WillReturnRows(pgxmock.NewRows([]string{
			"offer_key", "plan_key", "plan_id", "billing_unit", "billing_count",
		}).AddRow("pro_month", "pro", "plan", "month", 1)).RowsWillBeClosed()
	mock.ExpectQuery("SELECT * FROM advance_subscription_change($1, $2, $3, $4)").
		WithArgs(int64(19), "applied", nil, nil).
		WillReturnRows(pgxmock.NewRows([]string{"advanced"}).AddRow(true)).
		RowsWillBeClosed()
	change, err := getOpenBillingSubscriptionChangeTx(ctx, transaction, "stripe", "sub-review-fix")
	if err != nil || change == nil || change.ID != "19" || change.ToOfferKey != "pro_month" {
		t.Fatalf("open change = %+v, error = %v", change, err)
	}
	if err := advanceBillingSubscriptionChangeTx(ctx, transaction, change.ID, 19, "applied", nil, nil); err != nil {
		t.Fatalf("advance change = %v", err)
	}

	mock.ExpectQuery("SELECT * FROM advance_subscription_change($1, $2, $3, $4)").
		WithArgs(int64(19), "applied", nil, nil).
		WillReturnRows(pgxmock.NewRows([]string{"advanced"}).AddRow(false)).
		RowsWillBeClosed()
	if err := advanceBillingSubscriptionChangeTx(ctx, transaction, change.ID, 19, "applied", nil, nil); err == nil {
		t.Fatal("rejected subscription change was accepted")
	}
	mock.ExpectQuery("SELECT * FROM advance_subscription_change($1, $2, $3, $4)").
		WithArgs(int64(19), "applied", nil, nil).
		WillReturnRows(pgxmock.NewRows([]string{"advanced"})).
		RowsWillBeClosed()
	if err := advanceBillingSubscriptionChangeTx(ctx, transaction, change.ID, 19, "applied", nil, nil); err == nil {
		t.Fatal("missing transition result was accepted")
	}
	mock.ExpectQuery("SELECT * FROM get_open_billing_subscription_change($1, $2)").
		WithArgs("stripe", "sub-review-fix").
		WillReturnRows(pgxmock.NewRows([]string{"id"})).
		RowsWillBeClosed()
	if missing, err := getOpenBillingSubscriptionChangeTx(ctx, transaction, "stripe", "sub-review-fix"); err != nil || missing != nil {
		t.Fatalf("missing open change = %+v, error = %v", missing, err)
	}
}

func newReviewFixTransaction(t *testing.T) (pgxmock.PgxConnIface, *PostgresTransaction) {
	t.Helper()
	ctx := context.Background()
	mock, err := pgxmock.NewConn(pgxmock.QueryMatcherOption(pgxmock.QueryMatcherEqual))
	if err != nil {
		t.Fatalf("create pgx mock: %v", err)
	}
	mock.ExpectBegin()
	tx, err := mock.Begin(ctx)
	if err != nil {
		t.Fatalf("begin pgx mock transaction: %v", err)
	}
	transaction := &PostgresTransaction{tx: tx}
	t.Cleanup(func() {
		mock.ExpectRollback()
		if err := tx.Rollback(context.Background()); err != nil {
			t.Errorf("rollback pgx mock transaction: %v", err)
		}
		mock.ExpectClose()
		if err := mock.Close(context.Background()); err != nil {
			t.Errorf("close pgx mock: %v", err)
		}
		if err := mock.ExpectationsWereMet(); err != nil {
			t.Errorf("pgx mock expectations: %v", err)
		}
	})
	return mock, transaction
}

func TestBillingLifecycleReviewFixUtilityBoundaries(t *testing.T) {
	if got := customerProviderID(nil); got != "" {
		t.Fatalf("nil customer provider ID = %q", got)
	}
	if got := subscriptionEventID(BillingEvent{}); got != "" {
		t.Fatalf("nil subscription event ID = %q", got)
	}
	if got := existingText(nil, func(*CommerceSubscription) string { return "unexpected" }); got != "" {
		t.Fatalf("nil existing text = %q", got)
	}
	if got := existingTime(nil, func(*CommerceSubscription) *time.Time { return nil }); got != nil {
		t.Fatalf("nil existing time = %v", got)
	}
	if got := firstTime(nil, nil); got != nil {
		t.Fatalf("empty first time = %v", got)
	}
	if got := handledBilling(BillingEventSubscriptionUpdated, "account", "subscription"); !got.Handled || got.AccountID != "account" || got.SubscriptionID != "subscription" {
		t.Fatalf("handled billing result = %+v", got)
	}
}

func TestBillingLifecycleReviewFixValidationAndNoMutationEvents(t *testing.T) {
	t.Parallel()
	ctx := context.Background()
	processor := postgresBillingLifecycle{}
	validIntentID := "00000000-0000-4000-8000-000000000405"

	for _, test := range []struct {
		name     string
		metadata map[string]any
		wantErr  bool
	}{
		{"absent", nil, false},
		{"valid UUID", map[string]any{"checkout_intent_id": validIntentID}, false},
		{"wrong type", map[string]any{"checkout_intent_id": 17}, true},
		{"blank", map[string]any{"checkout_intent_id": " "}, true},
		{"malformed UUID", map[string]any{"checkout_intent_id": "not-a-uuid"}, true},
	} {
		t.Run("metadata "+test.name, func(t *testing.T) {
			if err := validateBillingLifecycleMetadata(test.metadata); (err != nil) != test.wantErr {
				t.Fatalf("validateBillingLifecycleMetadata() error = %v, wantErr %v", err, test.wantErr)
			}
		})
	}

	for _, eventType := range []BillingEventType{
		BillingEventCustomerDeleted,
		BillingEventCheckoutExpired,
		BillingEventInvoiceUpcoming,
		BillingEventPaymentMethodAttached,
	} {
		t.Run(string(eventType), func(t *testing.T) {
			result, err := processor.process(ctx, BillingEvent{Type: eventType, AccountID: "account"}, "")
			if err != nil || !result.Handled || result.AccountID != "account" {
				t.Fatalf("process(%q) = %+v, error = %v", eventType, result, err)
			}
		})
	}

	if _, err := processor.process(ctx, BillingEvent{Type: BillingEventType("provider.private"), AccountID: "account"}, ""); err == nil {
		t.Fatal("unsupported provider lifecycle event was accepted")
	}
	if _, err := processor.process(ctx, BillingEvent{Type: BillingEventCheckoutCompleted, Subscription: &BillingSubscription{}}, ""); err == nil {
		t.Fatal("subscription checkout without a resolvable account was accepted")
	}
	if err := processor.persistInvoice(ctx, BillingEvent{}, "account"); err != nil {
		t.Fatalf("persistInvoice(nil invoice) = %v", err)
	}
}

func TestBillingLifecycleReviewFixEntitlementOutcomeContract(t *testing.T) {
	t.Parallel()
	for _, test := range []struct {
		name             string
		status           string
		outcome          subscriptionEntitlementOutcome
		applyEntitlement bool
		wantErr          bool
	}{
		{"active applied", "active", subscriptionEntitlementApplied, true, false},
		{"trialing preserved when disabled", "trialing", subscriptionEntitlementPreserved, false, false},
		{"past due preserved", "past_due", subscriptionEntitlementPreserved, true, false},
		{"terminal revoked", "canceled", subscriptionEntitlementRevoked, true, false},
		{"terminal manual assignment preserved", "expired", subscriptionEntitlementPreserved, true, false},
		{"active preserved while enabled", "active", subscriptionEntitlementPreserved, true, true},
		{"unknown status", "unknown", subscriptionEntitlementPreserved, true, true},
	} {
		t.Run(test.name, func(t *testing.T) {
			if err := validateSubscriptionEntitlementOutcome(test.status, test.outcome, test.applyEntitlement); (err != nil) != test.wantErr {
				t.Fatalf("validateSubscriptionEntitlementOutcome() error = %v, wantErr %v", err, test.wantErr)
			}
		})
	}
}
