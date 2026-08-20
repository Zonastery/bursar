package bursar

import (
	"context"
	"testing"
	"time"

	"github.com/google/uuid"
)

func TestPostgresStateMapperRealRows(t *testing.T) {
	ctx, cancel, config := financialContext(t)
	defer cancel()
	_, store := newFinancialSDK(t, ctx, config, &financialProvider{name: "alpha"})

	offer, err := store.ResolveBillingOffer(ctx, "alpha", "", "alpha-pro-month", "")
	if err != nil || offer == nil {
		t.Fatalf("resolve mapper offer = %+v, %v", offer, err)
	}
	accountID := uuid.NewString()
	now := time.Now().UTC().Truncate(time.Second)
	periodEnd := now.Add(30 * 24 * time.Hour)
	providerSubscriptionID := "mapper-sub-" + uuid.NewString()
	subscriptionID, err := store.UpsertBillingSubscriptionState(ctx, CommerceSubscription{
		AccountID: accountID, Provider: "alpha", ProviderSubscriptionID: providerSubscriptionID,
		OfferID: offer.ID, Status: "active", CurrentPeriodStart: &now, CurrentPeriodEnd: &periodEnd,
		TrialEnd: &periodEnd, CancelAt: &periodEnd, ProviderUpdatedAt: now, Metadata: map[string]any{"mapper": true},
	})
	if err != nil || subscriptionID == "" {
		t.Fatalf("persist mapper subscription = %q, %v", subscriptionID, err)
	}
	typedAccountID, err := postgresUUID(accountID, "mapper account ID")
	if err != nil {
		t.Fatal(err)
	}
	if err := store.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		subscriptionRows, callErr := tx.Call(ctx, "list_billing_subscriptions", typedAccountID)
		if callErr != nil {
			return callErr
		}
		subscriptionRow := rowOptional(subscriptionRows)
		if subscriptionRow == nil {
			return NewStoreError("mapper subscription row was not returned", ErrorOptions{})
		}
		mappedSubscription, mapErr := commerceStateSubscriptionFromRow(ctx, tx, subscriptionRow, "mapper_subscription")
		if mapErr != nil || mappedSubscription.TrialEnd == nil || mappedSubscription.CancelAt == nil {
			return NewStoreError("mapper subscription row did not map optional timestamps", ErrorOptions{Cause: mapErr})
		}
		for _, key := range []string{"current_period_start", "current_period_end", "trial_end", "cancel_at", "ended_at", "grace_ends_at", "grace_expired_at", "provider_updated_at", "cancel_at_period_end", "metadata"} {
			row := cloneAnyMap(subscriptionRow)
			row[key] = "bad"
			if _, mapErr := commerceStateSubscriptionFromRow(ctx, tx, row, "mapper_subscription"); mapErr == nil {
				return NewStoreError("malformed subscription row was accepted", ErrorOptions{Details: map[string]any{"field": key}})
			}
		}

		offerRows, callErr := tx.Call(ctx, "resolve_catalog_offer", "alpha", "price_id", "alpha-pro-month")
		if callErr != nil {
			return callErr
		}
		offerRow := rowOptional(offerRows)
		if offerRow == nil {
			return NewStoreError("mapper offer row was not returned", ErrorOptions{})
		}
		mappedOffer, mapErr := commerceStateBillingOfferFromRow(ctx, tx, offerRow, "alpha", "", "alpha-pro-month", "", "mapper")
		if mapErr != nil || mappedOffer.OfferKey != offer.OfferKey || mappedOffer.Grant == nil {
			return NewStoreError("mapper offer row did not map cycle grant", ErrorOptions{Cause: mapErr})
		}
		for _, mutate := range []func(map[string]any){
			func(row map[string]any) { row["offer_key"] = "different" },
			func(row map[string]any) { row["cycle_grant_amount"] = "bad" },
			func(row map[string]any) { row["cycle_grant_bucket_key"] = nil },
		} {
			row := cloneAnyMap(offerRow)
			mutate(row)
			if _, mapErr := commerceStateBillingOfferFromRow(ctx, tx, row, "alpha", "", "alpha-pro-month", "", "mapper"); mapErr == nil {
				return NewStoreError("malformed offer row was accepted", ErrorOptions{})
			}
		}

		changeRow := map[string]any{
			"id": "1", "subscription_id": subscriptionID, "to_offer_id": offer.ID,
			"to_catalog_revision_id": rowValue(offerRow, "catalog_revision_id"), "effective_at": periodEnd,
			"effective_behavior": "renewal", "state": "scheduled", "proration_behavior": "none",
			"created_at": now, "updated_at": now, "idempotency_key": "mapper-change-" + uuid.NewString(),
		}
		mappedChange, mapErr := commerceStateSubscriptionChangeFromRow(ctx, tx, changeRow, "alpha", providerSubscriptionID, "mapper_change")
		if mapErr != nil || mappedChange.ID != "1" || mappedChange.State != "scheduled" {
			return NewStoreError("mapper change row did not map", ErrorOptions{Cause: mapErr})
		}
		for _, mutate := range []func(map[string]any){
			func(row map[string]any) { row["effective_at"] = "bad" },
			func(row map[string]any) { row["effective_behavior"] = "bad" },
			func(row map[string]any) { row["state"] = "bad" },
			func(row map[string]any) { row["proration_behavior"] = "bad" },
			func(row map[string]any) { row["created_at"] = "bad" },
			func(row map[string]any) { row["updated_at"] = "bad" },
			func(row map[string]any) { row["idempotency_key"] = "" },
		} {
			row := cloneAnyMap(changeRow)
			mutate(row)
			if _, mapErr := commerceStateSubscriptionChangeFromRow(ctx, tx, row, "alpha", providerSubscriptionID, "mapper_change"); mapErr == nil {
				return NewStoreError("malformed change row was accepted", ErrorOptions{})
			}
		}

		return nil
	}); err != nil {
		t.Fatal(err)
	}
}
