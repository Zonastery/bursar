// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import (
	"context"
	"fmt"
	"time"
)

var (
	_ billingSubscriptionWriter           = (*PostgresStore)(nil)
	_ billingSubscriptionConflictRecorder = (*PostgresStore)(nil)
	_ billingActiveCatalogSource          = (*PostgresStore)(nil)
	_ billingTopupResolver                = (*PostgresStore)(nil)
	_ billingPseudonymizer                = (*PostgresStore)(nil)
	_ billingGraceStateStore              = (*PostgresStore)(nil)
	_ billingSubscriptionLookup           = (*PostgresStore)(nil)
	_ autoRechargeProviderPaymentUpdater  = (*PostgresStore)(nil)
)

func (s *PostgresStore) GetBillingCustomerByProvider(ctx context.Context, provider, providerCustomerID string) (result *BillingCustomerRecord, err error) {
	if provider, err = requireText(provider, "billing customer provider"); err != nil {
		return nil, err
	}
	if providerCustomerID, err = requireText(providerCustomerID, "billing provider customer ID"); err != nil {
		return nil, err
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, callErr := tx.Call(ctx, "get_billing_customer_by_provider", provider, providerCustomerID)
		if callErr != nil {
			return callErr
		}
		if len(rows) > 1 {
			return NewStoreError("get_billing_customer_by_provider returned multiple customers", ErrorOptions{})
		}
		row := rowOptional(rows)
		if row == nil {
			return nil
		}
		mapped, mapErr := commerceStateCustomerFromRow(row, "get_billing_customer_by_provider")
		if mapErr != nil {
			return mapErr
		}
		if mapped.Provider != provider || mapped.ProviderCustomerID != providerCustomerID {
			return NewStoreError("provider customer lookup returned mismatched identity", ErrorOptions{})
		}
		result = mapped
		return nil
	})
	return result, err
}

func (s *PostgresStore) GetBillingSubscriptionByProvider(ctx context.Context, provider, providerSubscriptionID string) (result *CommerceSubscription, err error) {
	if provider, err = requireText(provider, "billing subscription provider"); err != nil {
		return nil, err
	}
	if providerSubscriptionID, err = requireText(providerSubscriptionID, "billing provider subscription ID"); err != nil {
		return nil, err
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, callErr := tx.Call(ctx, "get_billing_subscription_by_provider", provider, providerSubscriptionID)
		if callErr != nil {
			return callErr
		}
		if len(rows) > 1 {
			return NewStoreError("get_billing_subscription_by_provider returned multiple subscriptions", ErrorOptions{})
		}
		row := rowOptional(rows)
		if row == nil {
			return nil
		}
		mapped, mapErr := commerceStateSubscriptionFromRow(ctx, tx, row, "get_billing_subscription_by_provider")
		if mapErr != nil {
			return mapErr
		}
		if mapped.Provider != provider || mapped.ProviderSubscriptionID != providerSubscriptionID {
			return NewStoreError("provider subscription lookup returned mismatched identity", ErrorOptions{})
		}
		result = mapped
		return nil
	})
	return result, err
}

func (s *PostgresStore) UpsertBillingSubscriptionState(ctx context.Context, state CommerceSubscription) (result string, err error) {
	if state.AccountID, err = requireText(state.AccountID, "billing subscription account ID"); err != nil {
		return "", err
	}
	if state.Provider, err = requireText(state.Provider, "billing subscription provider"); err != nil {
		return "", err
	}
	if state.ProviderSubscriptionID, err = requireText(state.ProviderSubscriptionID, "billing provider subscription ID"); err != nil {
		return "", err
	}
	if state.OfferID, err = requireText(state.OfferID, "billing subscription offer ID"); err != nil {
		return "", err
	}
	if !billingSubscriptionStatus(state.Status) {
		return "", NewError("billing subscription status is invalid", ErrorOptions{Code: ErrorCodeBilling, Category: ErrorCategoryInvalidRequest})
	}
	if state.ProviderUpdatedAt.IsZero() {
		return "", NewError("billing subscription provider update time is required", ErrorOptions{Code: ErrorCodeBilling, Category: ErrorCategoryInvalidRequest})
	}
	if state.Status != "past_due" && state.GraceEndsAt != nil {
		return "", NewError("billing subscription grace deadline requires past_due status", ErrorOptions{Code: ErrorCodeBilling, Category: ErrorCategoryInvalidRequest})
	}
	typedAccountID, err := postgresUUID(state.AccountID, "billing subscription account ID")
	if err != nil {
		return "", err
	}
	typedOfferID, err := postgresUUID(state.OfferID, "billing subscription offer ID")
	if err != nil {
		return "", err
	}
	metadata, err := marshalMetadata(CreditMetadata(state.Metadata))
	if err != nil {
		return "", err
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, callErr := tx.Call(ctx, "upsert_billing_subscription",
			typedAccountID,
			state.Provider,
			state.ProviderSubscriptionID,
			nullableText(state.ProviderCustomerID),
			typedOfferID,
			state.Status,
			nullableTime(state.CurrentPeriodStart),
			nullableTime(state.CurrentPeriodEnd),
			state.CancelAtPeriodEnd,
			metadata,
			nullableTime(state.TrialEnd),
			nullableTime(state.CancelAt),
			nullableTime(state.EndedAt),
			state.ProviderUpdatedAt.UTC(),
			nullableTime(state.GraceEndsAt),
		)
		if callErr != nil {
			return callErr
		}
		row, rowErr := rowRequired(rows, "upsert_billing_subscription")
		if rowErr != nil {
			return rowErr
		}
		value, valueErr := firstScalar(row, "upsert_billing_subscription")
		if valueErr != nil {
			return valueErr
		}
		result, valueErr = textFromScalar(value, "upsert_billing_subscription")
		return valueErr
	})
	return result, err
}

func (s *PostgresStore) GetBillingPaymentByProvider(ctx context.Context, provider, providerPaymentID string) (result *BillingPaymentRecord, err error) {
	if provider, err = requireText(provider, "billing payment provider"); err != nil {
		return nil, err
	}
	if providerPaymentID, err = requireText(providerPaymentID, "billing provider payment ID"); err != nil {
		return nil, err
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, callErr := tx.Call(ctx, "get_billing_payment_by_provider", provider, providerPaymentID)
		if callErr != nil {
			return callErr
		}
		if len(rows) > 1 {
			return NewStoreError("get_billing_payment_by_provider returned multiple payments", ErrorOptions{})
		}
		row := rowOptional(rows)
		if row == nil {
			return nil
		}
		mapped, mapErr := billingPaymentRecordFromRow(row, "get_billing_payment_by_provider")
		if mapErr != nil {
			return mapErr
		}
		if mapped.Provider != provider || mapped.ProviderPaymentID != providerPaymentID {
			return NewStoreError("provider payment lookup returned mismatched identity", ErrorOptions{})
		}
		result = mapped
		return nil
	})
	return result, err
}

func billingPaymentRecordFromRow(row map[string]any, operation string) (*BillingPaymentRecord, error) {
	id, err := requiredRowText(row, "id", operation)
	if err != nil {
		return nil, err
	}
	provider, err := requiredRowText(row, "provider", operation)
	if err != nil {
		return nil, err
	}
	providerPaymentID, err := requiredRowText(row, "provider_payment_id", operation)
	if err != nil {
		return nil, err
	}
	accountID, err := requiredRowText(row, "subject_id", operation)
	if err != nil {
		return nil, err
	}
	amountMinor, err := commerceStateRowInt64(row, "amount_minor", operation)
	if err != nil {
		return nil, err
	}
	taxMinor, err := commerceStateRowInt64(row, "tax_minor", operation)
	if err != nil {
		return nil, err
	}
	currency, err := requiredRowText(row, "currency", operation)
	if err != nil || !billingCurrency(currency) {
		return nil, NewStoreError(operation+" returned an invalid currency", ErrorOptions{Cause: err})
	}
	purpose, err := requiredRowText(row, "purpose", operation)
	if err != nil || !oneOf(purpose, "subscription", "credit_topup") {
		return nil, NewStoreError(operation+" returned an invalid payment purpose", ErrorOptions{Cause: err})
	}
	status, err := requiredRowText(row, "status", operation)
	if err != nil || !billingPaymentStatus(status) {
		return nil, NewStoreError(operation+" returned an invalid payment status", ErrorOptions{Cause: err})
	}
	providerUpdatedAt, err := rowTime(row, "provider_updated_at", operation)
	if err != nil {
		return nil, err
	}
	metadata, err := jsonMap(rowValue(row, "metadata"), operation+".metadata")
	if err != nil {
		return nil, err
	}
	return &BillingPaymentRecord{ID: id, Provider: provider, ProviderPaymentID: providerPaymentID, ProviderInvoiceID: optionalRowText(row, "provider_invoice_id"), AccountID: accountID, AmountMinor: amountMinor, TaxMinor: taxMinor, Currency: currency, Purpose: purpose, Status: status, ProviderUpdatedAt: providerUpdatedAt, Metadata: metadata}, nil
}

func (s *PostgresStore) UpsertBillingPaymentState(ctx context.Context, input BillingPaymentUpsert) (result string, err error) {
	if input.AccountID, err = requireText(input.AccountID, "billing payment account ID"); err != nil {
		return "", err
	}
	if input.Provider, err = requireText(input.Provider, "billing payment provider"); err != nil {
		return "", err
	}
	if input.ProviderPaymentID, err = requireText(input.ProviderPaymentID, "billing provider payment ID"); err != nil {
		return "", err
	}
	if input.AmountMinor < 0 || input.TaxMinor < 0 || !billingCurrency(input.Currency) || !oneOf(input.Purpose, "subscription", "credit_topup") || !billingPaymentStatus(input.Status) || input.ProviderUpdatedAt.IsZero() {
		return "", NewError("billing payment input is invalid", ErrorOptions{Code: ErrorCodeBilling, Category: ErrorCategoryInvalidRequest})
	}
	typedAccountID, err := postgresUUID(input.AccountID, "billing payment account ID")
	if err != nil {
		return "", err
	}
	metadata, err := marshalMetadata(CreditMetadata(input.Metadata))
	if err != nil {
		return "", err
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, callErr := tx.Call(ctx, "upsert_billing_payment", typedAccountID, input.Provider, input.ProviderPaymentID, input.AmountMinor, input.TaxMinor, input.Currency, input.Purpose, input.Status, input.ProviderUpdatedAt.UTC(), nullableText(input.ProviderInvoiceID), metadata)
		if callErr != nil {
			return callErr
		}
		row, rowErr := rowRequired(rows, "upsert_billing_payment")
		if rowErr != nil {
			return rowErr
		}
		value, valueErr := firstScalar(row, "upsert_billing_payment")
		if valueErr != nil {
			return valueErr
		}
		result, valueErr = textFromScalar(value, "upsert_billing_payment")
		return valueErr
	})
	return result, err
}

func (s *PostgresStore) ResolveBillingTopup(ctx context.Context, provider, productID, priceID, lookupKey string) (result *BillingTopupResult, err error) {
	if provider, err = requireText(provider, "billing top-up provider"); err != nil {
		return nil, err
	}
	lookupType, lookupValue := commerceStateOfferLookup(productID, priceID, lookupKey)
	if lookupType == "" {
		return nil, nil
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, callErr := tx.Call(ctx, "resolve_catalog_topup", provider, lookupType, lookupValue)
		if callErr != nil {
			return callErr
		}
		if len(rows) > 1 {
			return NewStoreError("resolve_catalog_topup returned multiple top-ups", ErrorOptions{})
		}
		row := rowOptional(rows)
		if row == nil {
			return nil
		}
		id, mapErr := requiredRowText(row, "id", "resolve_catalog_topup")
		if mapErr != nil {
			return mapErr
		}
		key, mapErr := requiredRowText(row, "topup_key", "resolve_catalog_topup")
		if mapErr != nil {
			return mapErr
		}
		credits, mapErr := rowAmount(row, "credits_per_unit", "resolve_catalog_topup")
		if mapErr != nil || !credits.IsPositive() {
			return NewStoreError("resolve_catalog_topup returned invalid credits", ErrorOptions{Cause: mapErr})
		}
		amountMinor, mapErr := commerceStateRowInt64(row, "amount_minor", "resolve_catalog_topup")
		if mapErr != nil || amountMinor < 0 {
			return NewStoreError("resolve_catalog_topup returned invalid amount", ErrorOptions{Cause: mapErr})
		}
		currency, mapErr := requiredRowText(row, "currency", "resolve_catalog_topup")
		if mapErr != nil || !billingCurrency(currency) {
			return NewStoreError("resolve_catalog_topup returned invalid currency", ErrorOptions{Cause: mapErr})
		}
		minQuantity, mapErr := rowInt(row, "min_quantity", "resolve_catalog_topup")
		if mapErr != nil {
			return mapErr
		}
		maxQuantity, mapErr := rowInt(row, "max_quantity", "resolve_catalog_topup")
		if mapErr != nil {
			return mapErr
		}
		defaultQuantity, mapErr := rowInt(row, "default_quantity", "resolve_catalog_topup")
		if mapErr != nil || minQuantity < 1 || maxQuantity < minQuantity || defaultQuantity < minQuantity || defaultQuantity > maxQuantity {
			return NewStoreError("resolve_catalog_topup returned invalid quantity bounds", ErrorOptions{Cause: mapErr})
		}
		const maxSafeMinor = int64(9_007_199_254_740_991)
		if amountMinor > 0 && int64(maxQuantity) > maxSafeMinor/amountMinor {
			return NewStoreError("resolve_catalog_topup amount bounds overflow safe minor units", ErrorOptions{})
		}
		result = &BillingTopupResult{TopupID: id, TopupKey: key, CreditsPerUnit: credits, DepositTo: optionalRowText(row, "bucket_key"), AmountMinor: amountMinor, Currency: currency, MinQuantity: minQuantity, MaxQuantity: maxQuantity, DefaultQuantity: defaultQuantity, MinAmountMinor: amountMinor * int64(minQuantity), MaxAmountMinor: amountMinor * int64(maxQuantity)}
		return nil
	})
	return result, err
}

func (s *PostgresStore) CreateBillingCreditGrant(ctx context.Context, input BillingCreditGrantCreate) (result string, err error) {
	if !input.Credits.IsPositive() || input.Quantity < 1 {
		return "", NewError("billing credit grant is invalid", ErrorOptions{Code: ErrorCodeBilling, Category: ErrorCategoryInvalidRequest})
	}
	if (input.TopupID != "" && (input.PaymentID == "" || input.SubscriptionID != "")) || (input.TopupID == "" && input.SubscriptionID == "") {
		return "", NewError("billing credit grant source is invalid", ErrorOptions{Code: ErrorCodeBilling, Category: ErrorCategoryInvalidRequest})
	}
	typedPaymentID, err := nullablePostgresUUID(input.PaymentID, "billing payment ID")
	if err != nil {
		return "", err
	}
	typedSubscriptionID, err := nullablePostgresUUID(input.SubscriptionID, "billing subscription ID")
	if err != nil {
		return "", err
	}
	typedTopupID, err := nullablePostgresUUID(input.TopupID, "billing top-up ID")
	if err != nil {
		return "", err
	}
	typedBillingEventID, err := nullablePostgresUUID(input.BillingEventID, "billing event ID")
	if err != nil {
		return "", err
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, callErr := tx.Call(ctx, "create_billing_credit_grant", typedPaymentID, typedSubscriptionID, typedTopupID, amountArgument(input.Credits), input.Quantity, typedBillingEventID)
		if callErr != nil {
			return callErr
		}
		row, rowErr := rowRequired(rows, "create_billing_credit_grant")
		if rowErr != nil {
			return rowErr
		}
		value, valueErr := firstScalar(row, "create_billing_credit_grant")
		if valueErr != nil {
			return valueErr
		}
		result, valueErr = textFromScalar(value, "create_billing_credit_grant")
		return valueErr
	})
	return result, err
}

func (s *PostgresStore) GrantBillingCredit(ctx context.Context, grantID, idempotencyKey string) (result BillingCreditPostingResult, err error) {
	if grantID, err = requireText(grantID, "billing credit grant ID"); err != nil {
		return result, err
	}
	if idempotencyKey, err = requireStableKey(idempotencyKey, "billing credit grant idempotency key"); err != nil {
		return result, err
	}
	typedGrantID, err := postgresUUID(grantID, "billing credit grant ID")
	if err != nil {
		return result, err
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, callErr := tx.Call(ctx, "grant_billing_credit", typedGrantID, idempotencyKey)
		if callErr != nil {
			return callErr
		}
		row, rowErr := rowRequired(rows, "grant_billing_credit")
		if rowErr != nil {
			return rowErr
		}
		balance, valueErr := optionalRowAmount(row, "balance_after", "grant_billing_credit")
		if valueErr != nil {
			return valueErr
		}
		replayed, valueErr := rowBool(row, "replayed", "grant_billing_credit")
		if valueErr != nil {
			return valueErr
		}
		result = BillingCreditPostingResult{LedgerEntryID: optionalRowText(row, "ledger_entry_id"), BalanceAfter: balance, Replayed: replayed, ErrorCode: optionalRowText(row, "error_code")}
		return nil
	})
	return result, err
}

func (s *PostgresStore) GetBillingCreditGrantByPayment(ctx context.Context, paymentID string) (result string, err error) {
	if paymentID, err = requireText(paymentID, "billing payment ID"); err != nil {
		return "", err
	}
	typedPaymentID, err := postgresUUID(paymentID, "billing payment ID")
	if err != nil {
		return "", err
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, callErr := tx.Call(ctx, "get_billing_credit_grant_by_payment", typedPaymentID)
		if callErr != nil {
			return callErr
		}
		row := rowOptional(rows)
		if row == nil {
			return nil
		}
		result, callErr = requiredRowText(row, "id", "get_billing_credit_grant_by_payment")
		return callErr
	})
	return result, err
}

func (s *PostgresStore) UpsertBillingRefundState(ctx context.Context, input BillingRefundUpsert) (result string, err error) {
	if input.Provider, err = requireText(input.Provider, "billing refund provider"); err != nil {
		return "", err
	}
	if input.ProviderPaymentID, err = requireText(input.ProviderPaymentID, "billing refund provider payment ID"); err != nil {
		return "", err
	}
	if input.ProviderRefundID, err = requireText(input.ProviderRefundID, "billing provider refund ID"); err != nil {
		return "", err
	}
	if input.AccountID, err = requireText(input.AccountID, "billing refund account ID"); err != nil {
		return "", err
	}
	if input.AmountMinor <= 0 || !billingCurrency(input.Currency) || !billingPaymentStatus(input.Status) || input.ProviderUpdatedAt.IsZero() {
		return "", NewError("billing refund input is invalid", ErrorOptions{Code: ErrorCodeBilling, Category: ErrorCategoryInvalidRequest})
	}
	payment, err := s.GetBillingPaymentByProvider(ctx, input.Provider, input.ProviderPaymentID)
	if err != nil {
		return "", err
	}
	if payment == nil {
		return "", NewError("billing refund payment was not found", ErrorOptions{Code: ErrorCodeCommerceResourceNotFound, Category: ErrorCategoryNotFound})
	}
	typedPaymentID, err := postgresUUID(payment.ID, "billing payment ID")
	if err != nil {
		return "", err
	}
	typedAccountID, err := postgresUUID(input.AccountID, "billing refund account ID")
	if err != nil {
		return "", err
	}
	metadata, err := marshalMetadata(CreditMetadata(input.Metadata))
	if err != nil {
		return "", err
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, callErr := tx.Call(ctx, "upsert_billing_refund", typedPaymentID, input.ProviderRefundID, input.AmountMinor, input.Status, nullableText(input.Reason), input.ProviderUpdatedAt.UTC(), typedAccountID, input.Currency, metadata)
		if callErr != nil {
			return callErr
		}
		row, rowErr := rowRequired(rows, "upsert_billing_refund")
		if rowErr != nil {
			return rowErr
		}
		value, valueErr := firstScalar(row, "upsert_billing_refund")
		if valueErr != nil {
			return valueErr
		}
		result, valueErr = textFromScalar(value, "upsert_billing_refund")
		return valueErr
	})
	return result, err
}

func (s *PostgresStore) PostBillingRefund(ctx context.Context, refundID, grantID string, amountMinor int64, idempotencyKey string) (result BillingCreditPostingResult, err error) {
	if refundID, err = requireText(refundID, "billing refund ID"); err != nil {
		return result, err
	}
	if grantID, err = requireText(grantID, "billing credit grant ID"); err != nil {
		return result, err
	}
	if amountMinor <= 0 {
		return result, NewError("billing refund amount must be positive", ErrorOptions{Code: ErrorCodeBilling, Category: ErrorCategoryInvalidRequest})
	}
	if idempotencyKey, err = requireStableKey(idempotencyKey, "billing refund idempotency key"); err != nil {
		return result, err
	}
	typedRefundID, err := postgresUUID(refundID, "billing refund ID")
	if err != nil {
		return result, err
	}
	typedGrantID, err := postgresUUID(grantID, "billing credit grant ID")
	if err != nil {
		return result, err
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, callErr := tx.Call(ctx, "post_billing_refund", typedRefundID, typedGrantID, amountMinor, idempotencyKey)
		if callErr != nil {
			return callErr
		}
		row, rowErr := rowRequired(rows, "post_billing_refund")
		if rowErr != nil {
			return rowErr
		}
		balance, valueErr := optionalRowAmount(row, "balance_after", "post_billing_refund")
		if valueErr != nil {
			return valueErr
		}
		replayed, valueErr := rowBool(row, "replayed", "post_billing_refund")
		if valueErr != nil {
			return valueErr
		}
		result = BillingCreditPostingResult{LedgerEntryID: optionalRowText(row, "ledger_entry_id"), BalanceAfter: balance, Replayed: replayed, ErrorCode: optionalRowText(row, "error_code")}
		return nil
	})
	return result, err
}

func (s *PostgresStore) UpsertBillingInvoiceState(ctx context.Context, input BillingInvoiceUpsert) (err error) {
	if input.Provider, err = requireText(input.Provider, "billing invoice provider"); err != nil {
		return err
	}
	if input.ProviderInvoiceID, err = requireText(input.ProviderInvoiceID, "billing provider invoice ID"); err != nil {
		return err
	}
	if input.AccountID, err = requireText(input.AccountID, "billing invoice account ID"); err != nil {
		return err
	}
	if !oneOf(input.Status, "draft", "open", "paid", "void", "uncollectible") || input.AmountPaidMinor < 0 || input.AmountDueMinor < 0 || !billingCurrency(input.Currency) || input.ProviderUpdatedAt.IsZero() {
		return NewError("billing invoice input is invalid", ErrorOptions{Code: ErrorCodeBilling, Category: ErrorCategoryInvalidRequest})
	}
	var subscriptionID string
	if input.ProviderSubscriptionID != "" {
		subscription, lookupErr := s.GetBillingSubscriptionByProvider(ctx, input.Provider, input.ProviderSubscriptionID)
		if lookupErr != nil {
			return lookupErr
		}
		if subscription == nil || subscription.AccountID != input.AccountID {
			return NewError("billing invoice subscription was not found", ErrorOptions{Code: ErrorCodeCommerceResourceNotFound, Category: ErrorCategoryNotFound})
		}
		subscriptionID = subscription.ID
	}
	typedAccountID, err := postgresUUID(input.AccountID, "billing invoice account ID")
	if err != nil {
		return err
	}
	typedSubscriptionID, err := nullablePostgresUUID(subscriptionID, "billing subscription ID")
	if err != nil {
		return err
	}
	metadata, err := marshalMetadata(CreditMetadata(input.Metadata))
	if err != nil {
		return err
	}
	return s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, callErr := tx.Call(ctx, "upsert_billing_invoice", typedAccountID, input.Provider, input.ProviderInvoiceID, typedSubscriptionID, input.Status, input.AmountDueMinor, input.AmountPaidMinor, input.Currency, nullableTime(input.PeriodStart), nullableTime(input.PeriodEnd), metadata, input.ProviderUpdatedAt.UTC())
		if callErr != nil {
			return callErr
		}
		row, rowErr := rowRequired(rows, "upsert_billing_invoice")
		if rowErr != nil {
			return rowErr
		}
		value, valueErr := firstScalar(row, "upsert_billing_invoice")
		if valueErr != nil {
			return valueErr
		}
		_, valueErr = textFromScalar(value, "upsert_billing_invoice")
		return valueErr
	})
}

func (s *PostgresStore) UpsertBillingDisputeState(ctx context.Context, input BillingDisputeUpsert) (err error) {
	if input.Provider, err = requireText(input.Provider, "billing dispute provider"); err != nil {
		return err
	}
	if input.ProviderDisputeID, err = requireText(input.ProviderDisputeID, "billing provider dispute ID"); err != nil {
		return err
	}
	if input.ProviderPaymentID, err = requireText(input.ProviderPaymentID, "billing dispute provider payment ID"); err != nil {
		return err
	}
	if !oneOf(input.Status, "needs_response", "under_review", "won", "lost", "closed") || input.ProviderUpdatedAt.IsZero() {
		return NewError("billing dispute input is invalid", ErrorOptions{Code: ErrorCodeBilling, Category: ErrorCategoryInvalidRequest})
	}
	payment, err := s.GetBillingPaymentByProvider(ctx, input.Provider, input.ProviderPaymentID)
	if err != nil {
		return err
	}
	if payment == nil {
		return NewError("billing dispute payment was not found", ErrorOptions{Code: ErrorCodeCommerceResourceNotFound, Category: ErrorCategoryNotFound})
	}
	typedPaymentID, err := postgresUUID(payment.ID, "billing payment ID")
	if err != nil {
		return err
	}
	metadata, err := marshalMetadata(CreditMetadata(input.Metadata))
	if err != nil {
		return err
	}
	return s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, callErr := tx.Call(ctx, "upsert_billing_dispute", input.Provider, input.ProviderDisputeID, typedPaymentID, input.Status, nullableText(input.Reason), metadata, input.ProviderUpdatedAt.UTC())
		if callErr != nil {
			return callErr
		}
		row, rowErr := rowRequired(rows, "upsert_billing_dispute")
		if rowErr != nil {
			return rowErr
		}
		value, valueErr := firstScalar(row, "upsert_billing_dispute")
		if valueErr != nil {
			return valueErr
		}
		_, valueErr = textFromScalar(value, "upsert_billing_dispute")
		return valueErr
	})
}

func (s *PostgresStore) RecordSubscriptionConflict(ctx context.Context, input BillingSubscriptionConflictCreate) (err error) {
	if input.Provider, err = requireText(input.Provider, "billing conflict provider"); err != nil {
		return err
	}
	if input.DuplicateProviderSubscriptionID, err = requireText(input.DuplicateProviderSubscriptionID, "duplicate provider subscription ID"); err != nil {
		return err
	}
	typedAccountID, err := nullablePostgresUUID(input.AccountID, "billing conflict account ID")
	if err != nil {
		return err
	}
	metadata, err := marshalMetadata(CreditMetadata(input.Metadata))
	if err != nil {
		return err
	}
	return s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, callErr := tx.Call(ctx, "record_subscription_conflict", typedAccountID, input.Provider, input.DuplicateProviderSubscriptionID, nullableText(input.ExistingProviderSubscriptionID), nullableText(input.ProviderEventID), metadata)
		if callErr != nil {
			return callErr
		}
		_, rowErr := rowRequired(rows, "record_subscription_conflict")
		return rowErr
	})
}

func (s *PostgresStore) SelectSubscriptionEntitlementSource(ctx context.Context, accountID, subscriptionID string) (selected bool, err error) {
	if accountID, err = requireText(accountID, "billing entitlement account ID"); err != nil {
		return false, err
	}
	if subscriptionID, err = requireText(subscriptionID, "billing entitlement subscription ID"); err != nil {
		return false, err
	}
	typedAccountID, err := postgresUUID(accountID, "billing entitlement account ID")
	if err != nil {
		return false, err
	}
	typedSubscriptionID, err := postgresUUID(subscriptionID, "billing entitlement subscription ID")
	if err != nil {
		return false, err
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, callErr := tx.Call(ctx, "select_entitlement_source", typedAccountID, typedSubscriptionID)
		if callErr != nil {
			return callErr
		}
		row, rowErr := rowRequired(rows, "select_entitlement_source")
		if rowErr != nil {
			return rowErr
		}
		value, valueErr := firstScalar(row, "select_entitlement_source")
		if valueErr != nil {
			return valueErr
		}
		selected, valueErr = scalarBool(value, "select_entitlement_source")
		return valueErr
	})
	return selected, err
}

func (s *PostgresStore) PseudonymizeFinancialSubject(ctx context.Context, accountID string) (changed bool, err error) {
	if accountID, err = requireText(accountID, "billing pseudonymization account ID"); err != nil {
		return false, err
	}
	typedAccountID, err := postgresUUID(accountID, "billing pseudonymization account ID")
	if err != nil {
		return false, err
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, callErr := tx.Call(ctx, "pseudonymize_financial_subject", typedAccountID)
		if callErr != nil {
			return callErr
		}
		row, rowErr := rowRequired(rows, "pseudonymize_financial_subject")
		if rowErr != nil {
			return rowErr
		}
		value, valueErr := firstScalar(row, "pseudonymize_financial_subject")
		if valueErr != nil {
			return valueErr
		}
		changed, valueErr = scalarBool(value, "pseudonymize_financial_subject")
		return valueErr
	})
	return changed, err
}

func (s *PostgresStore) UpdateAutoRechargeAttemptByProviderPayment(ctx context.Context, update AutoRechargeProviderPaymentUpdate) error {
	provider, err := requireText(update.Provider, "auto-recharge provider")
	if err != nil {
		return err
	}
	providerPaymentID, err := requireText(update.ProviderPaymentID, "auto-recharge provider payment ID")
	if err != nil {
		return err
	}
	var attempt *AutoRechargeAttempt
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, callErr := tx.Call(ctx, "get_auto_recharge_attempt_by_provider", provider, providerPaymentID)
		if callErr != nil {
			return callErr
		}
		row := rowOptional(rows)
		if row == nil {
			return nil
		}
		attempt, callErr = commerceStateAutoRechargeAttemptFromRow(row, "get_auto_recharge_attempt_by_provider")
		return callErr
	})
	if err != nil || attempt == nil {
		return err
	}
	return s.UpdateAutoRechargeAttempt(ctx, AutoRechargeAttemptUpdate{ID: attempt.ID, State: update.State, ProviderAttemptID: providerPaymentID, FailureCode: update.FailureCode, FailureMessage: update.FailureMessage})
}

func (s *PostgresStore) ExpirePastDueGracePeriods(ctx context.Context, asOf time.Time, limit int) (expired int, err error) {
	if asOf.IsZero() {
		asOf = time.Now().UTC()
	}
	if limit < 1 || limit > 1000 {
		return 0, NewError("billing grace-period limit must be between 1 and 1000", ErrorOptions{Code: ErrorCodeConfig, Category: ErrorCategoryInvalidRequest})
	}
	candidates, err := s.ListExpiredGraceSubscriptions(ctx, asOf, limit)
	if err != nil {
		return 0, err
	}
	for _, candidate := range candidates {
		if candidate.GraceEndsAt == nil || candidate.Status != "past_due" || candidate.GraceExpiredAt != nil {
			continue
		}
		plan, planErr := s.GetUserPlan(ctx, candidate.AccountID)
		if planErr != nil {
			return expired, planErr
		}
		if plan.AssignmentSourceID == candidate.ID {
			if _, unsetErr := s.UnsetUserPlan(ctx, candidate.AccountID); unsetErr != nil {
				return expired, unsetErr
			}
		}
		marked, markErr := s.MarkSubscriptionGraceExpired(ctx, candidate.ID, *candidate.GraceEndsAt, asOf)
		if markErr != nil {
			return expired, markErr
		}
		if marked {
			expired++
		}
	}
	return expired, nil
}

func (s *PostgresStore) ListExpiredGraceSubscriptions(ctx context.Context, asOf time.Time, limit int) (candidates []CommerceSubscription, err error) {
	if asOf.IsZero() {
		asOf = time.Now().UTC()
	}
	if limit < 1 || limit > 1000 {
		return nil, NewError("billing grace-period limit must be between 1 and 1000", ErrorOptions{Code: ErrorCodeConfig, Category: ErrorCategoryInvalidRequest})
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, callErr := tx.Call(ctx, "list_expired_grace_subscriptions", asOf.UTC(), limit)
		if callErr != nil {
			return callErr
		}
		candidates = make([]CommerceSubscription, 0, len(rows))
		for _, row := range rows {
			mapped, mapErr := commerceStateSubscriptionFromRow(ctx, tx, row, "list_expired_grace_subscriptions")
			if mapErr != nil {
				return mapErr
			}
			candidates = append(candidates, *mapped)
		}
		return nil
	})
	return candidates, err
}

func (s *PostgresStore) MarkSubscriptionGraceExpired(ctx context.Context, subscriptionID string, expectedGraceEndsAt, expiredAt time.Time) (marked bool, err error) {
	typedSubscriptionID, err := postgresUUID(subscriptionID, "billing subscription ID")
	if err != nil {
		return false, err
	}
	if expectedGraceEndsAt.IsZero() || expiredAt.IsZero() {
		return false, NewError("billing grace timestamps are required", ErrorOptions{Code: ErrorCodeConfig, Category: ErrorCategoryInvalidRequest})
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, callErr := tx.Call(ctx, "mark_subscription_grace_expired", typedSubscriptionID, expectedGraceEndsAt.UTC(), expiredAt.UTC())
		if callErr != nil {
			return callErr
		}
		row := rowOptional(rows)
		if row == nil {
			return nil
		}
		value, valueErr := firstScalar(row, "mark_subscription_grace_expired")
		if valueErr != nil {
			return valueErr
		}
		marked, valueErr = scalarBool(value, "mark_subscription_grace_expired")
		return valueErr
	})
	return marked, err
}

func billingLifecyclePostingError(operation string, result BillingCreditPostingResult) error {
	if result.ErrorCode == "" {
		return nil
	}
	return NewStoreError(fmt.Sprintf("%s was rejected: %s", operation, result.ErrorCode), ErrorOptions{Details: map[string]any{"error_code": result.ErrorCode}})
}
