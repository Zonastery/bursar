// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import (
	"context"
	"strconv"
	"strings"
	"time"
)

// PostgresStore keeps commerce projections in the same tenant-scoped SQL
// authority as checkout intents and billing events. It never reconstructs
// subscription, invoice, or auto-recharge state from provider responses in
// process memory.
var (
	_ CommerceStateStore    = (*PostgresStore)(nil)
	_ AutoRechargeStore     = (*PostgresStore)(nil)
	_ BillingCustomerWriter = (*PostgresStore)(nil)
)

var commerceStateDefaultSubscriptionStatuses = []string{"active", "trialing", "canceled", "past_due", "incomplete"}

// BillingCustomerWriter is an optional durable capability used after a
// provider returns a trusted customer identifier from hosted checkout. It is
// deliberately narrower than BillingStore: lifecycle processing still enters
// through verified webhooks.
type BillingCustomerWriter interface {
	UpsertBillingCustomer(context.Context, BillingCustomerRecord) error
}

// UpsertBillingCustomer records a customer identifier obtained directly from
// a configured provider response. Callers must not use it with browser-supplied
// identifiers; account authorization happens before this durable write.
func (s *PostgresStore) UpsertBillingCustomer(ctx context.Context, customer BillingCustomerRecord) (err error) {
	if customer.AccountID, err = requireText(customer.AccountID, "billing customer account ID"); err != nil {
		return err
	}
	if customer.Provider, err = requireText(customer.Provider, "billing customer provider"); err != nil {
		return err
	}
	if customer.ProviderCustomerID, err = requireText(customer.ProviderCustomerID, "billing customer provider customer ID"); err != nil {
		return err
	}
	typedAccountID, err := postgresUUID(customer.AccountID, "billing customer account ID")
	if err != nil {
		return err
	}
	return s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, callErr := tx.Call(ctx, "upsert_billing_customer", typedAccountID, customer.Provider, customer.ProviderCustomerID, nullableText(customer.Email))
		if callErr != nil {
			return callErr
		}
		row, rowErr := rowRequired(rows, "upsert_billing_customer")
		if rowErr != nil {
			return rowErr
		}
		value, valueErr := firstScalar(row, "upsert_billing_customer")
		if valueErr != nil {
			return valueErr
		}
		if _, valueErr = textFromScalar(value, "upsert_billing_customer"); valueErr != nil {
			return valueErr
		}
		return nil
	})
}

func (s *PostgresStore) GetBillingCustomer(ctx context.Context, accountID, provider string) (result *BillingCustomerRecord, err error) {
	accountID, err = requireText(accountID, "billing customer account ID")
	if err != nil {
		return nil, err
	}
	typedAccountID, err := postgresUUID(accountID, "billing customer account ID")
	if err != nil {
		return nil, err
	}
	provider = strings.TrimSpace(provider)
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, callErr := tx.Call(ctx, "get_billing_customer", typedAccountID, nullableText(provider))
		if callErr != nil {
			return callErr
		}
		row := rowOptional(rows)
		if row == nil {
			return nil
		}
		mapped, mapErr := commerceStateCustomerFromRow(row, "get_billing_customer")
		if mapErr != nil {
			return mapErr
		}
		if mapped.AccountID != accountID {
			return NewStoreError("billing customer does not belong to requested account", ErrorOptions{Details: map[string]any{"account_id": accountID}})
		}
		result = mapped
		return nil
	})
	return result, err
}

func commerceStateCustomerFromRow(row map[string]any, operation string) (*BillingCustomerRecord, error) {
	provider, err := requiredRowText(row, "provider", operation)
	if err != nil {
		return nil, err
	}
	providerCustomerID, err := requiredRowText(row, "provider_customer_id", operation)
	if err != nil {
		return nil, err
	}
	accountID, err := requiredRowText(row, "subject_id", operation)
	if err != nil {
		return nil, err
	}
	return &BillingCustomerRecord{
		Provider:           provider,
		ProviderCustomerID: providerCustomerID,
		AccountID:          accountID,
		Email:              optionalRowText(row, "email"),
	}, nil
}

// GetBillingSubscription returns the most recently provider-updated account
// subscription whose status is allowed. A nil status slice follows the
// established self-service contract and selects the current lifecycle states
// used for account summaries; an explicit empty slice selects none.
func (s *PostgresStore) GetBillingSubscription(ctx context.Context, accountID string, statuses []string) (result *CommerceSubscription, err error) {
	accountID, err = requireText(accountID, "billing subscription account ID")
	if err != nil {
		return nil, err
	}
	typedAccountID, err := postgresUUID(accountID, "billing subscription account ID")
	if err != nil {
		return nil, err
	}
	allowed, err := commerceStateSubscriptionStatusSet(statuses)
	if err != nil {
		return nil, err
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, callErr := tx.Call(ctx, "list_billing_subscriptions", typedAccountID)
		if callErr != nil {
			return callErr
		}
		var selected map[string]any
		var selectedUpdatedAt time.Time
		for _, row := range rows {
			status, statusErr := requiredRowText(row, "status", "list_billing_subscriptions")
			if statusErr != nil {
				return statusErr
			}
			updatedAt, timeErr := rowTime(row, "provider_updated_at", "list_billing_subscriptions")
			if timeErr != nil {
				return timeErr
			}
			if !allowed[status] {
				continue
			}
			if selected == nil || updatedAt.After(selectedUpdatedAt) {
				selected, selectedUpdatedAt = row, updatedAt
			}
		}
		if selected == nil {
			return nil
		}
		mapped, mapErr := commerceStateSubscriptionFromRow(ctx, tx, selected, "get_billing_subscription")
		if mapErr != nil {
			return mapErr
		}
		if mapped.AccountID != accountID {
			return NewStoreError("billing subscription does not belong to requested account", ErrorOptions{Details: map[string]any{"account_id": accountID}})
		}
		result = mapped
		return nil
	})
	return result, err
}

// ListBillingSubscriptions returns all persisted subscriptions visible to the
// requested account in the current provider environment.
func (s *PostgresStore) ListBillingSubscriptions(ctx context.Context, accountID string) (result []CommerceSubscription, err error) {
	accountID, err = requireText(accountID, "billing subscription account ID")
	if err != nil {
		return nil, err
	}
	typedAccountID, err := postgresUUID(accountID, "billing subscription account ID")
	if err != nil {
		return nil, err
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, callErr := tx.Call(ctx, "list_billing_subscriptions", typedAccountID)
		if callErr != nil {
			return callErr
		}
		result = make([]CommerceSubscription, 0, len(rows))
		for _, row := range rows {
			mapped, mapErr := commerceStateSubscriptionFromRow(ctx, tx, row, "list_billing_subscriptions")
			if mapErr != nil {
				return mapErr
			}
			if mapped.AccountID != accountID {
				return NewStoreError("billing subscription does not belong to requested account", ErrorOptions{Details: map[string]any{"account_id": accountID}})
			}
			result = append(result, *mapped)
		}
		return nil
	})
	return result, err
}

func commerceStateSubscriptionStatusSet(statuses []string) (map[string]bool, error) {
	if statuses == nil {
		statuses = commerceStateDefaultSubscriptionStatuses
	}
	allowed := make(map[string]bool, len(statuses))
	for _, status := range statuses {
		status = strings.TrimSpace(status)
		if !commerceStateSubscriptionStatus(status) {
			return nil, NewError("invalid billing subscription status", ErrorOptions{Code: ErrorCodeConfig, Category: ErrorCategoryInvalidRequest, Details: map[string]any{"status": status}})
		}
		allowed[status] = true
	}
	return allowed, nil
}

func commerceStateSubscriptionStatus(status string) bool {
	switch status {
	case "incomplete", "incomplete_expired", "trialing", "active", "past_due", "canceled", "unpaid", "paused", "expired":
		return true
	default:
		return false
	}
}

func commerceStateSubscriptionFromRow(ctx context.Context, tx *PostgresTransaction, row map[string]any, operation string) (*CommerceSubscription, error) {
	id, err := requiredRowText(row, "id", operation)
	if err != nil {
		return nil, err
	}
	accountID, err := requiredRowText(row, "subject_id", operation)
	if err != nil {
		return nil, err
	}
	provider, err := requiredRowText(row, "provider", operation)
	if err != nil {
		return nil, err
	}
	providerSubscriptionID, err := requiredRowText(row, "provider_subscription_id", operation)
	if err != nil {
		return nil, err
	}
	offerID, err := requiredRowText(row, "offer_id", operation)
	if err != nil {
		return nil, err
	}
	revisionID, err := requiredRowText(row, "catalog_revision_id", operation)
	if err != nil {
		return nil, err
	}
	status, err := requiredRowText(row, "status", operation)
	if err != nil {
		return nil, err
	}
	if !commerceStateSubscriptionStatus(status) {
		return nil, NewStoreError(operation+" returned an unsupported subscription status", ErrorOptions{Details: map[string]any{"status": status}})
	}
	interval, intervalCount, offerKey, planID, planKey, err := commerceStateOfferContext(ctx, tx, offerID, revisionID, operation)
	if err != nil {
		return nil, err
	}
	currentPeriodStart, err := optionalRowTime(row, "current_period_start", operation)
	if err != nil {
		return nil, err
	}
	currentPeriodEnd, err := optionalRowTime(row, "current_period_end", operation)
	if err != nil {
		return nil, err
	}
	trialEnd, err := optionalRowTime(row, "trial_end", operation)
	if err != nil {
		return nil, err
	}
	cancelAt, err := optionalRowTime(row, "cancel_at", operation)
	if err != nil {
		return nil, err
	}
	endedAt, err := optionalRowTime(row, "ended_at", operation)
	if err != nil {
		return nil, err
	}
	graceEndsAt, err := optionalRowTime(row, "grace_ends_at", operation)
	if err != nil {
		return nil, err
	}
	graceExpiredAt, err := optionalRowTime(row, "grace_expired_at", operation)
	if err != nil {
		return nil, err
	}
	providerUpdatedAt, err := rowTime(row, "provider_updated_at", operation)
	if err != nil {
		return nil, err
	}
	cancelAtPeriodEnd, err := rowBool(row, "cancel_at_period_end", operation)
	if err != nil {
		return nil, err
	}
	metadata, err := jsonMap(rowValue(row, "metadata"), operation+".metadata")
	if err != nil {
		return nil, err
	}
	return &CommerceSubscription{
		ID:                     id,
		CatalogRevisionID:      revisionID,
		Provider:               provider,
		ProviderSubscriptionID: providerSubscriptionID,
		AccountID:              accountID,
		ProviderCustomerID:     optionalRowText(row, "provider_customer_id"),
		OfferID:                offerID,
		OfferKey:               offerKey,
		PlanID:                 planID,
		PlanKey:                planKey,
		Status:                 status,
		Interval:               interval,
		IntervalCount:          intervalCount,
		CurrentPeriodStart:     currentPeriodStart,
		CurrentPeriodEnd:       currentPeriodEnd,
		TrialEnd:               trialEnd,
		CancelAt:               cancelAt,
		EndedAt:                endedAt,
		CancelAtPeriodEnd:      cancelAtPeriodEnd,
		GraceEndsAt:            graceEndsAt,
		GraceExpiredAt:         graceExpiredAt,
		ProviderUpdatedAt:      providerUpdatedAt,
		Metadata:               metadata,
	}, nil
}

func commerceStateOfferContext(ctx context.Context, tx *PostgresTransaction, offerID, revisionID, operation string) (interval string, intervalCount int, offerKey, planID, planKey string, err error) {
	typedOfferID, parseErr := postgresUUID(offerID, operation+" offer ID")
	if parseErr != nil {
		err = parseErr
		return
	}
	typedRevisionID, parseErr := postgresUUID(revisionID, operation+" catalog revision ID")
	if parseErr != nil {
		err = parseErr
		return
	}
	rows, callErr := tx.Call(ctx, "get_catalog_offer_context", typedOfferID, typedRevisionID)
	if callErr != nil {
		err = callErr
		return
	}
	row, rowErr := rowRequired(rows, operation+".get_catalog_offer_context")
	if rowErr != nil {
		err = rowErr
		return
	}
	offerKey, err = requiredRowText(row, "offer_key", operation+".get_catalog_offer_context")
	if err != nil {
		return
	}
	planKey, err = requiredRowText(row, "plan_key", operation+".get_catalog_offer_context")
	if err != nil {
		return
	}
	planID, err = requiredRowText(row, "plan_id", operation+".get_catalog_offer_context")
	if err != nil {
		return
	}
	interval, err = requiredRowText(row, "billing_unit", operation+".get_catalog_offer_context")
	if err != nil {
		return
	}
	if interval != "day" && interval != "week" && interval != "month" && interval != "year" {
		err = NewStoreError(operation+" returned an invalid billing interval", ErrorOptions{Details: map[string]any{"interval": interval}})
		return
	}
	intervalCount, err = rowInt(row, "billing_count", operation+".get_catalog_offer_context")
	if err != nil {
		return
	}
	if intervalCount < 1 {
		err = NewStoreError(operation+" returned an invalid billing interval count", ErrorOptions{})
	}
	return
}

// ResolveBillingOffer resolves a provider reference through the active,
// provider-environment-scoped catalog. The database is the authority for the
// mapping; the supplied identifiers are used only to select its stable lookup
// type and to retain the provider's exact product reference for a later call.
func (s *PostgresStore) ResolveBillingOffer(ctx context.Context, provider, productID, priceID, lookupKey string) (result *BillingOffer, err error) {
	provider, err = requireText(provider, "billing offer provider")
	if err != nil {
		return nil, err
	}
	lookupType, lookupValue := commerceStateOfferLookup(productID, priceID, lookupKey)
	if lookupType == "" {
		return nil, nil
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, callErr := tx.Call(ctx, "resolve_catalog_offer", provider, lookupType, lookupValue)
		if callErr != nil {
			return callErr
		}
		if len(rows) > 1 {
			return NewStoreError("resolve_catalog_offer returned multiple offers", ErrorOptions{Details: map[string]any{"provider": provider, "lookup_type": lookupType}})
		}
		row := rowOptional(rows)
		if row == nil {
			return nil
		}
		mapped, mapErr := commerceStateBillingOfferFromRow(ctx, tx, row, provider, productID, priceID, lookupKey, "resolve_catalog_offer")
		if mapErr != nil {
			return mapErr
		}
		result = mapped
		return nil
	})
	return result, err
}

func commerceStateOfferLookup(productID, priceID, lookupKey string) (string, string) {
	if priceID = strings.TrimSpace(priceID); priceID != "" {
		return "price_id", priceID
	}
	if productID = strings.TrimSpace(productID); productID != "" {
		return "product_id", productID
	}
	if lookupKey = strings.TrimSpace(lookupKey); lookupKey != "" {
		return "external_id", lookupKey
	}
	return "", ""
}

func commerceStateBillingOfferFromRow(ctx context.Context, tx *PostgresTransaction, row map[string]any, provider, productID, priceID, lookupKey, operation string) (*BillingOffer, error) {
	id, err := requiredRowText(row, "id", operation)
	if err != nil {
		return nil, err
	}
	revisionID, err := requiredRowText(row, "catalog_revision_id", operation)
	if err != nil {
		return nil, err
	}
	offerKey, err := requiredRowText(row, "offer_key", operation)
	if err != nil {
		return nil, err
	}
	planKey, err := requiredRowText(row, "plan_key", operation)
	if err != nil {
		return nil, err
	}
	interval, intervalCount, contextOfferKey, planID, contextPlanKey, err := commerceStateOfferContext(ctx, tx, id, revisionID, operation)
	if err != nil {
		return nil, err
	}
	if offerKey != contextOfferKey || planKey != contextPlanKey {
		return nil, NewStoreError("resolved catalog offer context does not match offer", ErrorOptions{Details: map[string]any{"offer_id": id, "catalog_revision_id": revisionID}})
	}
	var grant *BillingGrantResult
	if rowValue(row, "cycle_grant_amount") != nil {
		credits, valueErr := rowAmount(row, "cycle_grant_amount", operation)
		if valueErr != nil {
			return nil, valueErr
		}
		bucket, valueErr := requiredRowText(row, "cycle_grant_bucket_key", operation)
		if valueErr != nil {
			return nil, valueErr
		}
		renewal := optionalRowText(row, "cycle_grant_renewal")
		grant = &BillingGrantResult{Mode: "cycle_grant", Credits: credits, Bucket: bucket, ReplacePrior: renewal == "replace_previous"}
	}
	return &BillingOffer{
		ID:          id,
		Provider:    provider,
		OfferKey:    offerKey,
		PlanID:      planID,
		PlanKey:     planKey,
		ProductID:   strings.TrimSpace(productID),
		PriceID:     strings.TrimSpace(priceID),
		LookupKey:   strings.TrimSpace(lookupKey),
		Interval:    interval,
		IntervalCnt: intervalCount,
		Grant:       grant,
	}, nil
}

// CreateBillingSubscriptionChange durably opens an account-independent,
// provider-scoped transition before the caller asks a payment provider to
// mutate a subscription. PostgreSQL enforces uniqueness and legal state.
func (s *PostgresStore) CreateBillingSubscriptionChange(ctx context.Context, input BillingSubscriptionChangeCreate) (result BillingSubscriptionChange, err error) {
	if input.Provider, err = requireText(input.Provider, "subscription change provider"); err != nil {
		return result, err
	}
	if input.ProviderSubscriptionID, err = requireText(input.ProviderSubscriptionID, "subscription change provider subscription ID"); err != nil {
		return result, err
	}
	if input.ToOfferID, err = requireText(input.ToOfferID, "subscription change target offer ID"); err != nil {
		return result, err
	}
	if input.OperationKey, err = requireStableKey(input.OperationKey, "subscription change operation key"); err != nil {
		return result, err
	}
	if input.EffectiveAt.IsZero() {
		return result, NewError("subscription change effective time is required", ErrorOptions{Code: ErrorCodeConfig, Category: ErrorCategoryInvalidRequest})
	}
	if input.Effective != "immediate" && input.Effective != "renewal" {
		return result, NewError("subscription change effective behavior is invalid", ErrorOptions{Code: ErrorCodeConfig, Category: ErrorCategoryInvalidRequest})
	}
	if input.ProrationBehavior == "" {
		input.ProrationBehavior = "provider_default"
	}
	if input.ProrationBehavior != "provider_default" && input.ProrationBehavior != "invoice_immediately" && input.ProrationBehavior != "none" {
		return result, NewError("subscription change proration behavior is invalid", ErrorOptions{Code: ErrorCodeConfig, Category: ErrorCategoryInvalidRequest})
	}
	typedOfferID, err := postgresUUID(input.ToOfferID, "subscription change target offer ID")
	if err != nil {
		return result, err
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		subscriptionRows, callErr := tx.Call(ctx, "get_billing_subscription_by_provider", input.Provider, input.ProviderSubscriptionID)
		if callErr != nil {
			return callErr
		}
		subscriptionRow := rowOptional(subscriptionRows)
		if subscriptionRow == nil {
			return NewStoreError("subscription change requires a persisted subscription", ErrorOptions{Retryable: true, Details: map[string]any{"provider": input.Provider, "provider_subscription_id": input.ProviderSubscriptionID}})
		}
		subscriptionID, valueErr := requiredRowText(subscriptionRow, "id", "get_billing_subscription_by_provider")
		if valueErr != nil {
			return valueErr
		}
		typedSubscriptionID, valueErr := postgresUUID(subscriptionID, "subscription change subscription ID")
		if valueErr != nil {
			return valueErr
		}
		rows, openErr := tx.Call(ctx, "open_subscription_change", typedSubscriptionID, typedOfferID, input.EffectiveAt.UTC(), input.Effective, input.OperationKey, input.ProrationBehavior)
		if openErr != nil {
			return openErr
		}
		row, rowErr := rowRequired(rows, "open_subscription_change")
		if rowErr != nil {
			return rowErr
		}
		if errorCode := optionalRowText(row, "error_code"); errorCode != "" {
			return commerceStateSubscriptionChangeRejected(errorCode)
		}
		changeID, valueErr := commerceStateRowInt64(row, "change_id", "open_subscription_change")
		if valueErr != nil {
			return valueErr
		}
		if changeID < 1 {
			return NewStoreError("open_subscription_change returned an invalid change identifier", ErrorOptions{})
		}
		changeRows, getErr := tx.Call(ctx, "get_billing_subscription_change", changeID)
		if getErr != nil {
			return getErr
		}
		changeRow := rowOptional(changeRows)
		if changeRow == nil {
			return NewStoreError("subscription change could not be read after creation", ErrorOptions{Indeterminate: true, Details: map[string]any{"subscription_change_id": strconv.FormatInt(changeID, 10)}})
		}
		mapped, mapErr := commerceStateSubscriptionChangeFromRow(ctx, tx, changeRow, input.Provider, input.ProviderSubscriptionID, "create_billing_subscription_change")
		if mapErr != nil {
			return mapErr
		}
		result = *mapped
		return nil
	})
	return result, err
}

func commerceStateSubscriptionChangeRejected(code string) error {
	options := ErrorOptions{Code: ErrorCodeBilling, Category: ErrorCategoryInvalidRequest, Details: map[string]any{"error_code": code}}
	switch code {
	case "idempotency_conflict", "open_change_exists":
		options.Code = ErrorCodeCheckoutConflict
		options.Category = ErrorCategoryConflict
	case "missing_subscription", "invalid_target_offer":
		options.Code = ErrorCodeCommerceResourceNotFound
		options.Category = ErrorCategoryNotFound
	case "invalid_request":
		// Keep the default invalid-request classification.
	default:
		return NewStoreError("subscription change returned an unknown rejection", ErrorOptions{Details: map[string]any{"error_code": code}})
	}
	return NewError("subscription change rejected: "+code, options)
}

func (s *PostgresStore) GetOpenBillingSubscriptionChange(ctx context.Context, provider, providerSubscriptionID string) (result *BillingSubscriptionChange, err error) {
	provider, err = requireText(provider, "subscription change provider")
	if err != nil {
		return nil, err
	}
	providerSubscriptionID, err = requireText(providerSubscriptionID, "subscription change provider subscription ID")
	if err != nil {
		return nil, err
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, callErr := tx.Call(ctx, "get_open_billing_subscription_change", provider, providerSubscriptionID)
		if callErr != nil {
			return callErr
		}
		row := rowOptional(rows)
		if row == nil {
			return nil
		}
		mapped, mapErr := commerceStateSubscriptionChangeFromRow(ctx, tx, row, provider, providerSubscriptionID, "get_open_billing_subscription_change")
		if mapErr != nil {
			return mapErr
		}
		result = mapped
		return nil
	})
	return result, err
}

// UpdateBillingSubscriptionChange intentionally mirrors the established
// cross-SDK SQL contract: state-less updates are a no-op because the existing
// advance_subscription_change RPC transitions state atomically and is the only
// mutation API. In particular, a provider-operation ID is retained only when
// it accompanies a legal state transition.
func (s *PostgresStore) UpdateBillingSubscriptionChange(ctx context.Context, id string, update BillingSubscriptionChangeUpdate) (err error) {
	id, err = requireText(id, "subscription change ID")
	if err != nil {
		return err
	}
	if update.State == nil {
		return nil
	}
	changeID, err := commerceStateSubscriptionChangeID(id)
	if err != nil {
		return err
	}
	state := strings.TrimSpace(*update.State)
	if !commerceStateSubscriptionChangeState(state) {
		return NewError("subscription change state is invalid", ErrorOptions{Code: ErrorCodeConfig, Category: ErrorCategoryInvalidRequest})
	}
	if update.ProviderOperationID != nil {
		if _, err := requireText(*update.ProviderOperationID, "subscription change provider operation ID"); err != nil {
			return err
		}
	}
	if update.ErrorMessage != nil && len(*update.ErrorMessage) > 8_192 {
		return NewError("subscription change error message is too long", ErrorOptions{Code: ErrorCodeConfig, Category: ErrorCategoryInvalidRequest})
	}
	return s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, callErr := tx.Call(ctx, "advance_subscription_change", changeID, state, nullableText(pointerText(update.ProviderOperationID)), nullableText(pointerText(update.ErrorMessage)))
		if callErr != nil {
			return callErr
		}
		row, rowErr := rowRequired(rows, "advance_subscription_change")
		if rowErr != nil {
			return rowErr
		}
		advanced, valueErr := scalarBoolFromRow(row, "advance_subscription_change")
		if valueErr != nil {
			return valueErr
		}
		if !advanced {
			return NewError("subscription change transition was rejected", ErrorOptions{Code: ErrorCodeCheckoutConflict, Category: ErrorCategoryConflict, Details: map[string]any{"subscription_change_id": id}})
		}
		return nil
	})
}

func commerceStateSubscriptionChangeID(id string) (int64, error) {
	parsed, err := strconv.ParseInt(strings.TrimSpace(id), 10, 64)
	if err != nil || parsed < 1 {
		return 0, NewError("subscription change ID is invalid", ErrorOptions{Code: ErrorCodeConfig, Category: ErrorCategoryInvalidRequest, Cause: err})
	}
	return parsed, nil
}

func commerceStateSubscriptionChangeState(state string) bool {
	switch state {
	case "awaiting_payment", "scheduled", "applied", "failed", "canceled":
		return true
	default:
		return false
	}
}

func commerceStateSubscriptionChangeFromRow(ctx context.Context, tx *PostgresTransaction, row map[string]any, provider, providerSubscriptionID, operation string) (*BillingSubscriptionChange, error) {
	id, err := requiredRowText(row, "id", operation)
	if err != nil {
		return nil, err
	}
	subscriptionID, err := requiredRowText(row, "subscription_id", operation)
	if err != nil {
		return nil, err
	}
	toOfferID, err := requiredRowText(row, "to_offer_id", operation)
	if err != nil {
		return nil, err
	}
	toRevisionID, err := requiredRowText(row, "to_catalog_revision_id", operation)
	if err != nil {
		return nil, err
	}
	interval, _, offerKey, _, planKey, err := commerceStateOfferContext(ctx, tx, toOfferID, toRevisionID, operation)
	if err != nil {
		return nil, err
	}
	effectiveAt, err := rowTime(row, "effective_at", operation)
	if err != nil {
		return nil, err
	}
	effective, err := requiredRowText(row, "effective_behavior", operation)
	if err != nil {
		return nil, err
	}
	if effective != "immediate" && effective != "renewal" {
		return nil, NewStoreError(operation+" returned an invalid effective behavior", ErrorOptions{})
	}
	state, err := requiredRowText(row, "state", operation)
	if err != nil {
		return nil, err
	}
	if !commerceStateSubscriptionChangeState(state) {
		return nil, NewStoreError(operation+" returned an invalid state", ErrorOptions{Details: map[string]any{"state": state}})
	}
	proration, err := requiredRowText(row, "proration_behavior", operation)
	if err != nil {
		return nil, err
	}
	if proration != "provider_default" && proration != "invoice_immediately" && proration != "none" {
		return nil, NewStoreError(operation+" returned an invalid proration behavior", ErrorOptions{})
	}
	createdAt, err := rowTime(row, "created_at", operation)
	if err != nil {
		return nil, err
	}
	updatedAt, err := rowTime(row, "updated_at", operation)
	if err != nil {
		return nil, err
	}
	if _, err := requiredRowText(row, "idempotency_key", operation); err != nil {
		return nil, err
	}
	return &BillingSubscriptionChange{
		ID:                  id,
		Provider:            provider,
		SubscriptionID:      subscriptionID,
		ToOfferID:           toOfferID,
		ToOfferKey:          offerKey,
		ToPlanKey:           planKey,
		ToInterval:          interval,
		Effective:           effective,
		EffectiveAt:         effectiveAt,
		ProrationBehavior:   proration,
		State:               state,
		ProviderOperationID: optionalRowText(row, "provider_operation_id"),
		ErrorMessage:        optionalRowText(row, "error_message"),
		CreatedAt:           createdAt,
		UpdatedAt:           updatedAt,
	}, nil
}

func pointerText(value *string) string {
	if value == nil {
		return ""
	}
	return *value
}

func scalarBoolFromRow(row map[string]any, operation string) (bool, error) {
	value, err := firstScalar(row, operation)
	if err != nil {
		return false, err
	}
	return scalarBool(value, operation)
}

func (s *PostgresStore) GetBillingPreferences(ctx context.Context, accountID string) (result *BillingPreferences, err error) {
	accountID, err = requireText(accountID, "billing preferences account ID")
	if err != nil {
		return nil, err
	}
	typedAccountID, err := postgresUUID(accountID, "billing preferences account ID")
	if err != nil {
		return nil, err
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, callErr := tx.Call(ctx, "get_billing_preferences", typedAccountID)
		if callErr != nil {
			return callErr
		}
		row := rowOptional(rows)
		if row == nil {
			return nil
		}
		mapped, mapErr := commerceStateBillingPreferencesFromRow(row, "get_billing_preferences")
		if mapErr != nil {
			return mapErr
		}
		if mapped.AccountID != accountID {
			return NewStoreError("billing preferences do not belong to requested account", ErrorOptions{Details: map[string]any{"account_id": accountID}})
		}
		result = mapped
		return nil
	})
	return result, err
}

func commerceStateBillingPreferencesFromRow(row map[string]any, operation string) (*BillingPreferences, error) {
	accountID, err := requiredRowText(row, "subject_id", operation)
	if err != nil {
		return nil, err
	}
	autoRecharge, err := rowBool(row, "auto_recharge", operation)
	if err != nil {
		return nil, err
	}
	overageProtection, err := rowBool(row, "overage_protection", operation)
	if err != nil {
		return nil, err
	}
	emailNotifications, err := rowBool(row, "email_notifications", operation)
	if err != nil {
		return nil, err
	}
	usageAlerts, err := rowBool(row, "usage_alerts", operation)
	if err != nil {
		return nil, err
	}
	invoiceReminders, err := rowBool(row, "invoice_reminders", operation)
	if err != nil {
		return nil, err
	}
	return &BillingPreferences{
		AccountID:          accountID,
		AutoRecharge:       autoRecharge,
		OverageProtection:  overageProtection,
		EmailNotifications: emailNotifications,
		UsageAlerts:        usageAlerts,
		InvoiceReminders:   invoiceReminders,
	}, nil
}

func (s *PostgresStore) UpsertBillingPreferences(ctx context.Context, preferences BillingPreferences) (err error) {
	if preferences.AccountID, err = requireText(preferences.AccountID, "billing preferences account ID"); err != nil {
		return err
	}
	typedAccountID, err := postgresUUID(preferences.AccountID, "billing preferences account ID")
	if err != nil {
		return err
	}
	return s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, callErr := tx.Call(
			ctx,
			"upsert_billing_preferences",
			typedAccountID,
			preferences.AutoRecharge,
			preferences.OverageProtection,
			preferences.EmailNotifications,
			preferences.UsageAlerts,
			preferences.InvoiceReminders,
		)
		if callErr != nil {
			return callErr
		}
		row, rowErr := rowRequired(rows, "upsert_billing_preferences")
		if rowErr != nil {
			return rowErr
		}
		updated, valueErr := scalarBoolFromRow(row, "upsert_billing_preferences")
		if valueErr != nil {
			return valueErr
		}
		if !updated {
			return NewStoreError("billing preferences update was rejected", ErrorOptions{Indeterminate: true, Details: map[string]any{"account_id": preferences.AccountID}})
		}
		return nil
	})
}

// ListBillingInvoices returns only provider documents already persisted for
// the requested account. Public ID deliberately equals provider_invoice_id so
// Commerce can pass it to an InvoiceProvider without leaking Bursar's internal
// invoice UUID or treating that UUID as a provider document reference.
func (s *PostgresStore) ListBillingInvoices(ctx context.Context, accountID string) (result []BillingInvoice, err error) {
	accountID, err = requireText(accountID, "billing invoices account ID")
	if err != nil {
		return nil, err
	}
	typedAccountID, err := postgresUUID(accountID, "billing invoices account ID")
	if err != nil {
		return nil, err
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, callErr := tx.Call(ctx, "list_billing_invoices", typedAccountID)
		if callErr != nil {
			return callErr
		}
		result = make([]BillingInvoice, 0, len(rows))
		for _, row := range rows {
			mapped, mapErr := commerceStateInvoiceFromRow(row, "list_billing_invoices")
			if mapErr != nil {
				return mapErr
			}
			if mapped.AccountID != accountID {
				return NewStoreError("billing invoice does not belong to requested account", ErrorOptions{Details: map[string]any{"account_id": accountID}})
			}
			result = append(result, *mapped)
		}
		return nil
	})
	return result, err
}

func commerceStateInvoiceFromRow(row map[string]any, operation string) (*BillingInvoice, error) {
	provider, err := requiredRowText(row, "provider", operation)
	if err != nil {
		return nil, err
	}
	providerInvoiceID, err := requiredRowText(row, "provider_invoice_id", operation)
	if err != nil {
		return nil, err
	}
	accountID, err := requiredRowText(row, "subject_id", operation)
	if err != nil {
		return nil, err
	}
	status, err := requiredRowText(row, "status", operation)
	if err != nil {
		return nil, err
	}
	if status != "draft" && status != "open" && status != "paid" && status != "void" && status != "uncollectible" {
		return nil, NewStoreError(operation+" returned an invalid invoice status", ErrorOptions{Details: map[string]any{"status": status}})
	}
	currency, err := requiredRowText(row, "currency", operation)
	if err != nil {
		return nil, err
	}
	currency = strings.ToUpper(currency)
	if len(currency) != 3 {
		return nil, NewStoreError(operation+" returned an invalid invoice currency", ErrorOptions{})
	}
	amountPaid, err := commerceStateRowInt64(row, "amount_paid_minor", operation)
	if err != nil {
		return nil, err
	}
	amountDue, err := commerceStateRowInt64(row, "amount_due_minor", operation)
	if err != nil {
		return nil, err
	}
	if amountPaid < 0 || amountDue < 0 {
		return nil, NewStoreError(operation+" returned a negative invoice amount", ErrorOptions{})
	}
	periodStart, err := optionalRowTime(row, "period_start", operation)
	if err != nil {
		return nil, err
	}
	periodEnd, err := optionalRowTime(row, "period_end", operation)
	if err != nil {
		return nil, err
	}
	metadata, err := jsonMap(rowValue(row, "metadata"), operation+".metadata")
	if err != nil {
		return nil, err
	}
	return &BillingInvoice{
		ID:              providerInvoiceID,
		Provider:        provider,
		AccountID:       accountID,
		SubscriptionID:  optionalRowText(row, "subscription_id"),
		Status:          status,
		Currency:        currency,
		AmountPaidMinor: amountPaid,
		AmountDueMinor:  amountDue,
		PeriodStart:     periodStart,
		PeriodEnd:       periodEnd,
		Metadata:        metadata,
	}, nil
}

func commerceStateRowInt64(row map[string]any, key, operation string) (int64, error) {
	value := rowValue(row, key)
	switch typed := value.(type) {
	case int:
		return int64(typed), nil
	case int8:
		return int64(typed), nil
	case int16:
		return int64(typed), nil
	case int32:
		return int64(typed), nil
	case int64:
		return typed, nil
	case uint:
		if uint64(typed) <= uint64(^uint64(0)>>1) {
			return int64(typed), nil
		}
	case uint8:
		return int64(typed), nil
	case uint16:
		return int64(typed), nil
	case uint32:
		return int64(typed), nil
	case uint64:
		if typed <= uint64(^uint64(0)>>1) {
			return int64(typed), nil
		}
	case string:
		if parsed, parseErr := strconv.ParseInt(typed, 10, 64); parseErr == nil {
			return parsed, nil
		}
	case []byte:
		if parsed, parseErr := strconv.ParseInt(string(typed), 10, 64); parseErr == nil {
			return parsed, nil
		}
	}
	return 0, NewStoreError(operation+" returned an invalid "+key, ErrorOptions{})
}

// ResolveAutoRechargeTopup maps the already parsed catalog reference back to
// the active database top-up. Both the provider lookup and the expected offer
// key are checked so a stale catalog document cannot charge a different item.
func (s *PostgresStore) ResolveAutoRechargeTopup(ctx context.Context, lookup AutoRechargeTopupLookup) (result *AutoRechargeTopup, err error) {
	lookup.Provider, err = requireText(lookup.Provider, "auto-recharge provider")
	if err != nil {
		return nil, err
	}
	lookup.OfferKey, err = requireText(lookup.OfferKey, "auto-recharge top-up key")
	if err != nil {
		return nil, err
	}
	lookupType, lookupValue := commerceStateProviderReferenceLookup(lookup.Reference)
	if lookupType == "" {
		return nil, NewError("auto-recharge top-up provider reference has no product identifier", ErrorOptions{Code: ErrorCodeConfig, Category: ErrorCategoryInvalidRequest})
	}
	productID := providerProductID(lookup.Reference)
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, callErr := tx.Call(ctx, "resolve_catalog_topup", lookup.Provider, lookupType, lookupValue)
		if callErr != nil {
			return callErr
		}
		if len(rows) > 1 {
			return NewStoreError("resolve_catalog_topup returned multiple top-ups", ErrorOptions{Details: map[string]any{"provider": lookup.Provider, "lookup_type": lookupType}})
		}
		row := rowOptional(rows)
		if row == nil {
			return nil
		}
		resolvedKey, keyErr := requiredRowText(row, "topup_key", "resolve_catalog_topup")
		if keyErr != nil {
			return keyErr
		}
		if resolvedKey != lookup.OfferKey {
			return nil
		}
		id, valueErr := requiredRowText(row, "id", "resolve_catalog_topup")
		if valueErr != nil {
			return valueErr
		}
		result = &AutoRechargeTopup{ID: id, ProductID: productID}
		return nil
	})
	return result, err
}

func commerceStateProviderReferenceLookup(reference ProviderReference) (string, string) {
	if reference.PriceID != nil && strings.TrimSpace(*reference.PriceID) != "" {
		return "price_id", strings.TrimSpace(*reference.PriceID)
	}
	if reference.ProductID != nil && strings.TrimSpace(*reference.ProductID) != "" {
		return "product_id", strings.TrimSpace(*reference.ProductID)
	}
	if reference.ExternalID != nil && strings.TrimSpace(*reference.ExternalID) != "" {
		return "external_id", strings.TrimSpace(*reference.ExternalID)
	}
	return "", ""
}

func (s *PostgresStore) GetAutoRechargeCustomer(ctx context.Context, userID, provider string) (result *AutoRechargeCustomer, err error) {
	userID, err = requireText(userID, "auto-recharge user ID")
	if err != nil {
		return nil, err
	}
	provider, err = requireText(provider, "auto-recharge provider")
	if err != nil {
		return nil, err
	}
	customer, err := s.GetBillingCustomer(ctx, userID, provider)
	if err != nil || customer == nil {
		return nil, err
	}
	if customer.AccountID != userID || customer.Provider != provider {
		return nil, NewStoreError("auto-recharge customer does not belong to requested user", ErrorOptions{Details: map[string]any{"user_id": userID, "provider": provider}})
	}
	return &AutoRechargeCustomer{UserID: userID, Provider: provider, ProviderCustomerID: customer.ProviderCustomerID}, nil
}

func (s *PostgresStore) GetAutoRechargeProfile(ctx context.Context, userID string) (result *AutoRechargeProfile, err error) {
	userID, err = requireText(userID, "auto-recharge user ID")
	if err != nil {
		return nil, err
	}
	typedUserID, err := postgresUUID(userID, "auto-recharge user ID")
	if err != nil {
		return nil, err
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, callErr := tx.Call(ctx, "get_auto_recharge_profile", typedUserID)
		if callErr != nil {
			return callErr
		}
		row := rowOptional(rows)
		if row == nil {
			return nil
		}
		mapped, mapErr := commerceStateAutoRechargeProfileFromRow(row, "get_auto_recharge_profile")
		if mapErr != nil {
			return mapErr
		}
		if mapped.UserID != userID {
			return NewStoreError("auto-recharge profile does not belong to requested user", ErrorOptions{Details: map[string]any{"user_id": userID}})
		}
		result = mapped
		return nil
	})
	return result, err
}

func commerceStateAutoRechargeProfileFromRow(row map[string]any, operation string) (*AutoRechargeProfile, error) {
	userID, err := requiredRowText(row, "subject_id", operation)
	if err != nil {
		return nil, err
	}
	enabled, err := rowBool(row, "enabled", operation)
	if err != nil {
		return nil, err
	}
	armed, err := rowBool(row, "armed", operation)
	if err != nil {
		return nil, err
	}
	stateText, err := requiredRowText(row, "state", operation)
	if err != nil {
		return nil, err
	}
	state := AutoRechargeState(stateText)
	if state != AutoRechargeStateDisabled && state != AutoRechargeStateActive && state != AutoRechargeStatePaused {
		return nil, NewStoreError(operation+" returned an invalid auto-recharge state", ErrorOptions{Details: map[string]any{"state": stateText}})
	}
	if enabled != (state != AutoRechargeStateDisabled) {
		return nil, NewStoreError(operation+" returned inconsistent enabled and state fields", ErrorOptions{})
	}
	quantity, err := commerceStateOptionalRowInt(row, "quantity", operation)
	if err != nil {
		return nil, err
	}
	maxCharges, err := commerceStateOptionalRowInt(row, "max_charges_per_window", operation)
	if err != nil {
		return nil, err
	}
	threshold := DecimalZero
	if rowValue(row, "threshold") != nil {
		threshold, err = rowAmount(row, "threshold", operation)
		if err != nil {
			return nil, err
		}
	}
	updatedAt, err := rowTime(row, "updated_at", operation)
	if err != nil {
		return nil, err
	}
	windowCount, err := commerceStateOptionalRowInt(row, "window_count", operation)
	if err != nil {
		return nil, err
	}
	profile := &AutoRechargeProfile{
		UserID:              userID,
		Enabled:             enabled,
		Armed:               armed,
		State:               state,
		Provider:            optionalRowText(row, "provider"),
		TopupID:             optionalRowText(row, "topup_id"),
		Quantity:            int64(quantity),
		Threshold:           threshold,
		MaxChargesPerWindow: maxCharges,
		WindowUnit:          optionalRowText(row, "window_unit"),
		WindowCount:         windowCount,
		WindowAnchor:        optionalRowText(row, "window_anchor"),
		WindowTimezone:      optionalRowText(row, "window_timezone"),
		UpdatedAt:           updatedAt,
	}
	if profile.Enabled && (profile.Provider == "" || profile.TopupID == "" || profile.Quantity < 1 || profile.MaxChargesPerWindow < 1 || profile.WindowCount < 1 || profile.Threshold.IsNegative() || profile.WindowUnit == "" || profile.WindowTimezone == "") {
		return nil, NewStoreError(operation+" returned a malformed enabled auto-recharge profile", ErrorOptions{})
	}
	if profile.Enabled && profile.WindowAnchor != "calendar" && profile.WindowAnchor != "rolling" {
		return nil, NewStoreError(operation+" returned an invalid auto-recharge window anchor", ErrorOptions{})
	}
	return profile, nil
}

func commerceStateOptionalRowInt(row map[string]any, key, operation string) (int, error) {
	if rowValue(row, key) == nil {
		return 0, nil
	}
	return rowInt(row, key, operation)
}

func (s *PostgresStore) UpsertAutoRechargeProfile(ctx context.Context, profile AutoRechargeProfile, options AutoRechargeProfileUpsertOptions) (err error) {
	if profile.UserID, err = requireText(profile.UserID, "auto-recharge user ID"); err != nil {
		return err
	}
	if profile.Enabled {
		if profile.Provider, err = requireText(profile.Provider, "auto-recharge provider"); err != nil {
			return err
		}
		if profile.TopupID, err = requireText(profile.TopupID, "auto-recharge top-up ID"); err != nil {
			return err
		}
		if profile.State != AutoRechargeStateActive && profile.State != AutoRechargeStatePaused {
			return NewError("enabled auto-recharge state is invalid", ErrorOptions{Code: ErrorCodeConfig, Category: ErrorCategoryInvalidRequest})
		}
		if profile.Quantity < 1 || profile.Quantity > 2_147_483_647 || profile.MaxChargesPerWindow < 1 || profile.MaxChargesPerWindow > 2_147_483_647 || profile.WindowCount < 1 || profile.WindowCount > 2_147_483_647 || profile.Threshold.IsNegative() {
			return NewError("enabled auto-recharge profile is invalid", ErrorOptions{Code: ErrorCodeConfig, Category: ErrorCategoryInvalidRequest})
		}
		if profile.WindowUnit == "" || profile.WindowTimezone == "" || (profile.WindowAnchor != "calendar" && profile.WindowAnchor != "rolling") {
			return NewError("enabled auto-recharge window is invalid", ErrorOptions{Code: ErrorCodeConfig, Category: ErrorCategoryInvalidRequest})
		}
	} else if profile.State != "" && profile.State != AutoRechargeStateDisabled {
		return NewError("disabled auto-recharge state is invalid", ErrorOptions{Code: ErrorCodeConfig, Category: ErrorCategoryInvalidRequest})
	}
	typedUserID, err := postgresUUID(profile.UserID, "auto-recharge user ID")
	if err != nil {
		return err
	}
	typedTopupID, err := nullablePostgresUUID(profile.TopupID, "auto-recharge top-up ID")
	if err != nil {
		return err
	}
	return s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		var provider, quantity, threshold, maxCharges, windowUnit, windowCount, windowAnchor, windowTimezone, armed, state any
		var topupID any = typedTopupID
		if profile.Enabled {
			provider = profile.Provider
			quantity = int(profile.Quantity)
			threshold = amountArgument(profile.Threshold)
			maxCharges = profile.MaxChargesPerWindow
			windowUnit = profile.WindowUnit
			windowCount = profile.WindowCount
			windowAnchor = profile.WindowAnchor
			windowTimezone = profile.WindowTimezone
			armed = profile.Armed
			state = string(profile.State)
		} else {
			armed = true
			state = string(AutoRechargeStateDisabled)
		}
		rows, callErr := tx.Call(ctx, "upsert_auto_recharge_profile", typedUserID, profile.Enabled, provider, topupID, quantity, threshold, maxCharges, windowUnit, windowCount, windowAnchor, windowTimezone, armed, state, options.ResetCooldown)
		if callErr != nil {
			return callErr
		}
		row, rowErr := rowRequired(rows, "upsert_auto_recharge_profile")
		if rowErr != nil {
			return rowErr
		}
		updated, valueErr := scalarBoolFromRow(row, "upsert_auto_recharge_profile")
		if valueErr != nil {
			return valueErr
		}
		if !updated {
			return NewStoreError("auto-recharge profile update was rejected", ErrorOptions{Details: map[string]any{"user_id": profile.UserID}})
		}
		return nil
	})
}

func (s *PostgresStore) ClaimAutoRechargeAttempt(ctx context.Context, claim AutoRechargeAttemptClaim) (result *AutoRechargeAttempt, err error) {
	if claim.UserID, err = requireText(claim.UserID, "auto-recharge user ID"); err != nil {
		return nil, err
	}
	if claim.IdempotencyKey, err = requireStableKey(claim.IdempotencyKey, "auto-recharge idempotency key"); err != nil {
		return nil, err
	}
	typedUserID, err := postgresUUID(claim.UserID, "auto-recharge user ID")
	if err != nil {
		return nil, err
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, callErr := tx.Call(ctx, "claim_auto_recharge_attempt", typedUserID, claim.IdempotencyKey)
		if callErr != nil {
			return callErr
		}
		row := rowOptional(rows)
		if row == nil {
			return nil
		}
		mapped, mapErr := commerceStateAutoRechargeAttemptFromRow(row, "claim_auto_recharge_attempt")
		if mapErr != nil {
			return mapErr
		}
		if mapped.UserID != claim.UserID {
			return NewStoreError("auto-recharge attempt does not belong to requested user", ErrorOptions{Details: map[string]any{"user_id": claim.UserID}})
		}
		result = mapped
		return nil
	})
	return result, err
}

func commerceStateAutoRechargeAttemptFromRow(row map[string]any, operation string) (*AutoRechargeAttempt, error) {
	id, err := requiredRowText(row, "id", operation)
	if err != nil {
		return nil, err
	}
	userID, err := requiredRowText(row, "subject_id", operation)
	if err != nil {
		return nil, err
	}
	provider, err := requiredRowText(row, "provider", operation)
	if err != nil {
		return nil, err
	}
	idempotencyKey, err := requiredRowText(row, "idempotency_key", operation)
	if err != nil {
		return nil, err
	}
	if _, keyErr := requireStableKey(idempotencyKey, "auto-recharge attempt idempotency key"); keyErr != nil {
		return nil, NewStoreError(operation+" returned an invalid idempotency key", ErrorOptions{Cause: keyErr})
	}
	topupID, err := requiredRowText(row, "topup_id", operation)
	if err != nil {
		return nil, err
	}
	quantity, err := rowInt(row, "quantity", operation)
	if err != nil {
		return nil, err
	}
	if quantity < 1 {
		return nil, NewStoreError(operation+" returned an invalid quantity", ErrorOptions{})
	}
	stateText, err := requiredRowText(row, "state", operation)
	if err != nil {
		return nil, err
	}
	state := AutoRechargeAttemptState(stateText)
	if !commerceStateAutoRechargeAttemptState(state) {
		return nil, NewStoreError(operation+" returned an invalid auto-recharge attempt state", ErrorOptions{Details: map[string]any{"state": stateText}})
	}
	windowStart, err := rowTime(row, "window_start", operation)
	if err != nil {
		return nil, err
	}
	windowEnd, err := rowTime(row, "window_end", operation)
	if err != nil {
		return nil, err
	}
	if !windowEnd.After(windowStart) {
		return nil, NewStoreError(operation+" returned an invalid auto-recharge window", ErrorOptions{})
	}
	quotedAmount, err := commerceStateOptionalRowInt64(row, "quoted_amount_minor", operation)
	if err != nil {
		return nil, err
	}
	currency := strings.ToUpper(optionalRowText(row, "currency"))
	if (quotedAmount == nil) != (currency == "") {
		return nil, NewStoreError(operation+" returned an inconsistent auto-recharge quote", ErrorOptions{})
	}
	if currency != "" && len(currency) != 3 {
		return nil, NewStoreError(operation+" returned an invalid auto-recharge currency", ErrorOptions{})
	}
	metadata, err := jsonMap(rowValue(row, "metadata"), operation+".metadata")
	if err != nil {
		return nil, err
	}
	createdAt, err := rowTime(row, "created_at", operation)
	if err != nil {
		return nil, err
	}
	updatedAt, err := rowTime(row, "updated_at", operation)
	if err != nil {
		return nil, err
	}
	return &AutoRechargeAttempt{
		ID:                id,
		UserID:            userID,
		Provider:          provider,
		IdempotencyKey:    idempotencyKey,
		ProviderAttemptID: optionalRowText(row, "provider_attempt_id"),
		TopupID:           topupID,
		Quantity:          int64(quantity),
		State:             state,
		WindowStart:       windowStart,
		WindowEnd:         windowEnd,
		QuotedAmountMinor: quotedAmount,
		Currency:          currency,
		FailureCode:       optionalRowText(row, "failure_code"),
		FailureMessage:    optionalRowText(row, "failure_message"),
		Metadata:          metadata,
		CreatedAt:         createdAt,
		UpdatedAt:         updatedAt,
	}, nil
}

func commerceStateOptionalRowInt64(row map[string]any, key, operation string) (*int64, error) {
	if rowValue(row, key) == nil {
		return nil, nil
	}
	value, err := commerceStateRowInt64(row, key, operation)
	if err != nil {
		return nil, err
	}
	return int64Pointer(value), nil
}

func commerceStateAutoRechargeAttemptState(state AutoRechargeAttemptState) bool {
	switch state {
	case AutoRechargeAttemptClaimed,
		AutoRechargeAttemptSubmitted,
		AutoRechargeAttemptProcessing,
		AutoRechargeAttemptUnknown,
		AutoRechargeAttemptSucceeded,
		AutoRechargeAttemptFailed,
		AutoRechargeAttemptActionRequired:
		return true
	default:
		return false
	}
}

// UpdateAutoRechargeAttempt follows the same legal multi-step transition
// path as the Python repository. The SQL RPC intentionally allows only
// adjacent state changes, so a claimed attempt progressing to processing is
// persisted as claimed -> submitted -> processing within one transaction.
func (s *PostgresStore) UpdateAutoRechargeAttempt(ctx context.Context, update AutoRechargeAttemptUpdate) (err error) {
	if update.ID, err = requireText(update.ID, "auto-recharge attempt ID"); err != nil {
		return err
	}
	if !commerceStateAutoRechargeAttemptState(update.State) {
		return NewError("auto-recharge attempt state is invalid", ErrorOptions{Code: ErrorCodeConfig, Category: ErrorCategoryInvalidRequest})
	}
	if update.ProviderAttemptID != "" {
		if _, err := requireText(update.ProviderAttemptID, "auto-recharge provider attempt ID"); err != nil {
			return err
		}
	}
	if update.FailureCode != "" {
		if _, err := requireText(update.FailureCode, "auto-recharge failure code"); err != nil {
			return err
		}
	}
	if len(update.FailureMessage) > 8_192 {
		return NewError("auto-recharge failure message is too long", ErrorOptions{Code: ErrorCodeConfig, Category: ErrorCategoryInvalidRequest})
	}
	metadata, err := marshalMetadata(CreditMetadata(update.Metadata))
	if err != nil {
		return err
	}
	typedAttemptID, err := postgresUUID(update.ID, "auto-recharge attempt ID")
	if err != nil {
		return err
	}
	return s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, callErr := tx.Call(ctx, "get_auto_recharge_attempt", typedAttemptID)
		if callErr != nil {
			return callErr
		}
		row := rowOptional(rows)
		if row == nil {
			return NewStoreError("auto-recharge attempt was not found", ErrorOptions{Details: map[string]any{"attempt_id": update.ID}})
		}
		current, mapErr := commerceStateAutoRechargeAttemptFromRow(row, "get_auto_recharge_attempt")
		if mapErr != nil {
			return mapErr
		}
		path, pathErr := commerceStateAutoRechargeTransitionPath(current.State, update.State)
		if pathErr != nil {
			return pathErr
		}
		for _, next := range path {
			advancedRows, advanceErr := tx.Call(ctx, "advance_auto_recharge_attempt", typedAttemptID, string(next), nullableText(update.ProviderAttemptID), nullableText(update.FailureCode), nullableText(update.FailureMessage), metadata)
			if advanceErr != nil {
				return advanceErr
			}
			advancedRow, rowErr := rowRequired(advancedRows, "advance_auto_recharge_attempt")
			if rowErr != nil {
				return rowErr
			}
			advanced, valueErr := scalarBoolFromRow(advancedRow, "advance_auto_recharge_attempt")
			if valueErr != nil {
				return valueErr
			}
			if !advanced {
				return NewStoreError("auto-recharge attempt transition was rejected", ErrorOptions{Details: map[string]any{"attempt_id": update.ID, "state": next}})
			}
		}
		return nil
	})
}

func commerceStateAutoRechargeTransitionPath(current, target AutoRechargeAttemptState) ([]AutoRechargeAttemptState, error) {
	if current == target {
		return nil, nil
	}
	paths := map[AutoRechargeAttemptState]map[AutoRechargeAttemptState][]AutoRechargeAttemptState{
		AutoRechargeAttemptClaimed: {
			AutoRechargeAttemptSubmitted:      {AutoRechargeAttemptSubmitted},
			AutoRechargeAttemptProcessing:     {AutoRechargeAttemptSubmitted, AutoRechargeAttemptProcessing},
			AutoRechargeAttemptSucceeded:      {AutoRechargeAttemptSubmitted, AutoRechargeAttemptProcessing, AutoRechargeAttemptSucceeded},
			AutoRechargeAttemptFailed:         {AutoRechargeAttemptSubmitted, AutoRechargeAttemptProcessing, AutoRechargeAttemptFailed},
			AutoRechargeAttemptUnknown:        {AutoRechargeAttemptSubmitted, AutoRechargeAttemptProcessing, AutoRechargeAttemptUnknown},
			AutoRechargeAttemptActionRequired: {AutoRechargeAttemptSubmitted, AutoRechargeAttemptActionRequired},
		},
		AutoRechargeAttemptSubmitted: {
			AutoRechargeAttemptProcessing:     {AutoRechargeAttemptProcessing},
			AutoRechargeAttemptSucceeded:      {AutoRechargeAttemptProcessing, AutoRechargeAttemptSucceeded},
			AutoRechargeAttemptFailed:         {AutoRechargeAttemptProcessing, AutoRechargeAttemptFailed},
			AutoRechargeAttemptUnknown:        {AutoRechargeAttemptProcessing, AutoRechargeAttemptUnknown},
			AutoRechargeAttemptActionRequired: {AutoRechargeAttemptActionRequired},
		},
		AutoRechargeAttemptProcessing: {
			AutoRechargeAttemptSucceeded:      {AutoRechargeAttemptSucceeded},
			AutoRechargeAttemptFailed:         {AutoRechargeAttemptFailed},
			AutoRechargeAttemptUnknown:        {AutoRechargeAttemptUnknown},
			AutoRechargeAttemptActionRequired: {AutoRechargeAttemptActionRequired},
		},
		AutoRechargeAttemptUnknown: {
			AutoRechargeAttemptProcessing:     {AutoRechargeAttemptProcessing},
			AutoRechargeAttemptSucceeded:      {AutoRechargeAttemptSucceeded},
			AutoRechargeAttemptFailed:         {AutoRechargeAttemptFailed},
			AutoRechargeAttemptActionRequired: {AutoRechargeAttemptActionRequired},
		},
		AutoRechargeAttemptActionRequired: {
			AutoRechargeAttemptProcessing: {AutoRechargeAttemptProcessing},
			AutoRechargeAttemptSucceeded:  {AutoRechargeAttemptSucceeded},
			AutoRechargeAttemptFailed:     {AutoRechargeAttemptFailed},
		},
	}
	if path, ok := paths[current][target]; ok {
		return path, nil
	}
	return nil, NewStoreError("auto-recharge attempt transition was rejected", ErrorOptions{Details: map[string]any{"current_state": current, "requested_state": target}})
}

func (s *PostgresStore) CountAutoRechargeAttempts(ctx context.Context, userID string, since time.Time) (result int, err error) {
	userID, err = requireText(userID, "auto-recharge user ID")
	if err != nil {
		return 0, err
	}
	if since.IsZero() {
		return 0, NewError("auto-recharge attempt window start is required", ErrorOptions{Code: ErrorCodeConfig, Category: ErrorCategoryInvalidRequest})
	}
	typedUserID, err := postgresUUID(userID, "auto-recharge user ID")
	if err != nil {
		return 0, err
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, callErr := tx.Call(ctx, "count_auto_recharge_attempts", typedUserID, since.UTC())
		if callErr != nil {
			return callErr
		}
		row, rowErr := rowRequired(rows, "count_auto_recharge_attempts")
		if rowErr != nil {
			return rowErr
		}
		value, valueErr := firstScalar(row, "count_auto_recharge_attempts")
		if valueErr != nil {
			return valueErr
		}
		result, valueErr = scalarInt(value, "count_auto_recharge_attempts")
		if valueErr != nil {
			return valueErr
		}
		if result < 0 {
			return NewStoreError("count_auto_recharge_attempts returned a negative count", ErrorOptions{})
		}
		return nil
	})
	return result, err
}
