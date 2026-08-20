// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import (
	"context"
	"time"
)

// BillingGrantResult is the catalog-owned subscription cycle grant attached
// to an offer. Amount is exact and is posted only through the database billing
// grant workflow.
type BillingGrantResult struct {
	Mode         string
	Credits      Amount
	Bucket       string
	ReplacePrior bool
}

// BillingTopupResult is the active catalog top-up resolved from a verified
// provider reference. Provider webhook payloads never supply these monetary
// or quantity rules.
type BillingTopupResult struct {
	TopupID         string
	TopupKey        string
	CreditsPerUnit  Amount
	DepositTo       string
	AmountMinor     int64
	Currency        string
	MinQuantity     int
	MaxQuantity     int
	DefaultQuantity int
	MinAmountMinor  int64
	MaxAmountMinor  int64
}

// BillingPaymentRecord is persisted provider payment truth used to attribute
// later refunds and disputes without trusting identifiers from the webhook.
type BillingPaymentRecord struct {
	ID                string
	Provider          string
	ProviderPaymentID string
	ProviderInvoiceID string
	AccountID         string
	AmountMinor       int64
	TaxMinor          int64
	Currency          string
	Purpose           string
	Status            string
	ProviderUpdatedAt time.Time
	Metadata          map[string]any
}

// BillingCreditPostingResult is returned by database-owned top-up,
// subscription-cycle, and refund ledger posting.
type BillingCreditPostingResult struct {
	LedgerEntryID string
	BalanceAfter  *Amount
	Replayed      bool
	ErrorCode     string
}

// BillingPaymentUpsert is the validated persistence input for provider
// payment truth. ProviderUpdatedAt fences stale webhook deliveries.
type BillingPaymentUpsert struct {
	Provider          string
	ProviderPaymentID string
	ProviderInvoiceID string
	AccountID         string
	AmountMinor       int64
	TaxMinor          int64
	Currency          string
	Purpose           string
	Status            string
	ProviderUpdatedAt time.Time
	Metadata          map[string]any
}

type BillingCreditGrantCreate struct {
	PaymentID      string
	SubscriptionID string
	TopupID        string
	Credits        Amount
	Quantity       int
	BillingEventID string
}

type BillingRefundUpsert struct {
	Provider          string
	ProviderRefundID  string
	ProviderPaymentID string
	AccountID         string
	AmountMinor       int64
	Currency          string
	Reason            string
	Status            string
	ProviderUpdatedAt time.Time
	Metadata          map[string]any
}

type BillingInvoiceUpsert struct {
	Provider               string
	ProviderInvoiceID      string
	ProviderSubscriptionID string
	AccountID              string
	Status                 string
	AmountPaidMinor        int64
	AmountDueMinor         int64
	Currency               string
	PeriodStart            *time.Time
	PeriodEnd              *time.Time
	ProviderUpdatedAt      time.Time
	Metadata               map[string]any
}

type BillingDisputeUpsert struct {
	Provider          string
	ProviderDisputeID string
	ProviderPaymentID string
	Status            string
	Reason            string
	ProviderUpdatedAt time.Time
	Metadata          map[string]any
}

// BillingSubscriptionConflictCreate records a provider-side duplicate without
// inventing a terminal state for either subscription.
type BillingSubscriptionConflictCreate struct {
	AccountID                       string
	Provider                        string
	DuplicateProviderSubscriptionID string
	ExistingProviderSubscriptionID  string
	ProviderEventID                 string
	Metadata                        map[string]any
}

// AutoRechargeProviderPaymentUpdate reconciles an attempt from a verified
// provider payment webhook when only the provider payment identity is known.
type AutoRechargeProviderPaymentUpdate struct {
	Provider          string
	ProviderPaymentID string
	State             AutoRechargeAttemptState
	FailureCode       string
	FailureMessage    string
}

type billingSubscriptionWriter interface {
	UpsertBillingSubscriptionState(context.Context, CommerceSubscription) (string, error)
}

type billingSubscriptionConflictRecorder interface {
	RecordSubscriptionConflict(context.Context, BillingSubscriptionConflictCreate) error
}

type billingActiveCatalogSource interface {
	GetActiveCatalog(context.Context) (*CatalogRevision, error)
}

type billingTopupResolver interface {
	ResolveBillingTopup(context.Context, string, string, string, string) (*BillingTopupResult, error)
}

type billingPseudonymizer interface {
	PseudonymizeFinancialSubject(context.Context, string) (bool, error)
}

type billingGraceStateStore interface {
	ListExpiredGraceSubscriptions(context.Context, time.Time, int) ([]CommerceSubscription, error)
}

type billingGraceExpiryStore interface {
	expirePastDueGracePeriod(context.Context, CommerceSubscription, time.Time, string) (bool, error)
}

type autoRechargeProviderPaymentUpdater interface {
	UpdateAutoRechargeAttemptByProviderPayment(context.Context, AutoRechargeProviderPaymentUpdate) error
}

func (s *BillingService) commerceStore() (CommerceStore, error) {
	if s == nil || s.store == nil {
		return nil, NewError("billing service is not configured", ErrorOptions{Code: ErrorCodeCapabilityNotConfigured, Category: ErrorCategoryUnavailable})
	}
	store, ok := s.store.(CommerceStore)
	if !ok {
		return nil, NewError("billing checkout persistence is not configured", ErrorOptions{Code: ErrorCodeCapabilityNotConfigured, Category: ErrorCategoryUnavailable})
	}
	return store, nil
}

func (s *BillingService) stateStore() (CommerceStateStore, error) {
	if s == nil || s.store == nil {
		return nil, NewError("billing service is not configured", ErrorOptions{Code: ErrorCodeCapabilityNotConfigured, Category: ErrorCategoryUnavailable})
	}
	store, ok := s.store.(CommerceStateStore)
	if !ok {
		return nil, NewError("billing state persistence is not configured", ErrorOptions{Code: ErrorCodeCapabilityNotConfigured, Category: ErrorCategoryUnavailable})
	}
	return store, nil
}

func (s *BillingService) autoRechargeStore() (AutoRechargeStore, error) {
	if s == nil || s.store == nil {
		return nil, NewError("billing service is not configured", ErrorOptions{Code: ErrorCodeAutoRechargeNotConfigured, Category: ErrorCategoryUnavailable})
	}
	store, ok := s.store.(AutoRechargeStore)
	if !ok {
		return nil, NewError("billing auto-recharge persistence is not configured", ErrorOptions{Code: ErrorCodeAutoRechargeNotConfigured, Category: ErrorCategoryUnavailable})
	}
	return store, nil
}

// HasProvisioning reports whether subscription lifecycle events can update
// the application's credit-plan projection. Durable billing persistence and
// financial provisioning are separate capabilities, as in the Python and
// JavaScript SDKs.
func (s *BillingService) HasProvisioning() bool {
	return s != nil && s.provisioning != nil
}

func (s *BillingService) CreateOrGetCheckoutIntent(ctx context.Context, input CheckoutIntentCreate) (CheckoutIntent, error) {
	store, err := s.commerceStore()
	if err != nil {
		return CheckoutIntent{}, err
	}
	return store.CreateOrGetCheckoutIntent(ctx, input)
}

// UpdateCheckoutIntent keeps SubjectID explicit because intent visibility and
// mutation are authenticated-subject scoped, not financial-account scoped.
func (s *BillingService) UpdateCheckoutIntent(ctx context.Context, intentID, subjectID string, update CheckoutIntentUpdate) error {
	store, err := s.commerceStore()
	if err != nil {
		return err
	}
	return store.UpdateCheckoutIntent(ctx, intentID, subjectID, update)
}

func (s *BillingService) GetCheckoutIntent(ctx context.Context, intentID, subjectID string) (*CheckoutIntent, error) {
	store, err := s.commerceStore()
	if err != nil {
		return nil, err
	}
	return store.GetCheckoutIntent(ctx, intentID, subjectID)
}

func (s *BillingService) GetUserSubscription(ctx context.Context, accountID string) (*CommerceSubscription, error) {
	store, err := s.stateStore()
	if err != nil {
		return nil, err
	}
	subscription, err := store.GetBillingSubscription(ctx, accountID, []string{"active", "trialing", "canceled", "past_due", "incomplete"})
	if err != nil {
		return nil, err
	}
	return s.expireGraceIfNeeded(ctx, subscription, time.Now().UTC())
}

func (s *BillingService) GetActiveSubscription(ctx context.Context, accountID string) (*CommerceSubscription, error) {
	store, err := s.stateStore()
	if err != nil {
		return nil, err
	}
	return store.GetBillingSubscription(ctx, accountID, []string{"active", "trialing"})
}

func (s *BillingService) GetBlockingSubscription(ctx context.Context, accountID string) (*CommerceSubscription, error) {
	store, err := s.stateStore()
	if err != nil {
		return nil, err
	}
	subscription, err := store.GetBillingSubscription(ctx, accountID, []string{"active", "trialing", "past_due", "incomplete"})
	if err != nil {
		return nil, err
	}
	return s.expireGraceIfNeeded(ctx, subscription, time.Now().UTC())
}

func (s *BillingService) ListCancellableSubscriptions(ctx context.Context, accountID string) ([]CommerceSubscription, error) {
	store, err := s.stateStore()
	if err != nil {
		return nil, err
	}
	all, err := store.ListBillingSubscriptions(ctx, accountID)
	if err != nil {
		return nil, err
	}
	result := make([]CommerceSubscription, 0, len(all))
	for _, subscription := range all {
		if oneOf(subscription.Status, "active", "trialing", "past_due", "incomplete", "unpaid", "paused") && subscription.ProviderSubscriptionID != "" {
			result = append(result, subscription)
		}
	}
	return result, nil
}

func (s *BillingService) ListCancellableProviderSubscriptionIDs(ctx context.Context, accountID string) ([]string, error) {
	subscriptions, err := s.ListCancellableSubscriptions(ctx, accountID)
	if err != nil {
		return nil, err
	}
	result := make([]string, 0, len(subscriptions))
	for _, subscription := range subscriptions {
		result = append(result, subscription.ProviderSubscriptionID)
	}
	return result, nil
}

func (s *BillingService) GetUserPreferences(ctx context.Context, accountID string) (*BillingPreferences, error) {
	store, err := s.stateStore()
	if err != nil {
		return nil, err
	}
	return store.GetBillingPreferences(ctx, accountID)
}

// GetActiveCatalogDocument returns the validated catalog document currently
// authoritative for billing policy. The document remains store-owned; this
// method does not introduce a second billing-side catalog cache.
func (s *BillingService) GetActiveCatalogDocument(ctx context.Context) (map[string]any, error) {
	if s == nil {
		return nil, NewError("billing catalog access is not configured", ErrorOptions{Code: ErrorCodeCapabilityNotConfigured, Category: ErrorCategoryUnavailable})
	}
	source, ok := s.store.(billingActiveCatalogSource)
	if !ok {
		return nil, NewError("billing catalog access is not configured", ErrorOptions{Code: ErrorCodeCapabilityNotConfigured, Category: ErrorCategoryUnavailable})
	}
	revision, err := source.GetActiveCatalog(ctx)
	if err != nil || revision == nil {
		return nil, err
	}
	return revision.Config, nil
}

func (s *BillingService) UpdateUserPreferences(ctx context.Context, preferences BillingPreferences) error {
	store, err := s.stateStore()
	if err != nil {
		return err
	}
	return store.UpsertBillingPreferences(ctx, preferences)
}

func (s *BillingService) ListBillingInvoices(ctx context.Context, accountID string) ([]BillingInvoice, error) {
	store, err := s.stateStore()
	if err != nil {
		return nil, err
	}
	return store.ListBillingInvoices(ctx, accountID)
}

func (s *BillingService) CreateBillingSubscriptionChange(ctx context.Context, input BillingSubscriptionChangeCreate) (BillingSubscriptionChange, error) {
	store, err := s.stateStore()
	if err != nil {
		return BillingSubscriptionChange{}, err
	}
	return store.CreateBillingSubscriptionChange(ctx, input)
}

func (s *BillingService) GetOpenBillingSubscriptionChange(ctx context.Context, provider, providerSubscriptionID string) (*BillingSubscriptionChange, error) {
	store, err := s.stateStore()
	if err != nil {
		return nil, err
	}
	return store.GetOpenBillingSubscriptionChange(ctx, provider, providerSubscriptionID)
}

func (s *BillingService) UpdateBillingSubscriptionChange(ctx context.Context, id string, update BillingSubscriptionChangeUpdate) error {
	store, err := s.stateStore()
	if err != nil {
		return err
	}
	return store.UpdateBillingSubscriptionChange(ctx, id, update)
}

// RecordSubscriptionConflict persists a verified provider-side duplicate so
// operators can resolve it without silently selecting a second entitlement
// source in process memory.
func (s *BillingService) RecordSubscriptionConflict(ctx context.Context, input BillingSubscriptionConflictCreate) error {
	if s == nil {
		return NewError("billing subscription-conflict persistence is not configured", ErrorOptions{Code: ErrorCodeCapabilityNotConfigured, Category: ErrorCategoryUnavailable})
	}
	recorder, ok := s.store.(billingSubscriptionConflictRecorder)
	if !ok {
		return NewError("billing subscription-conflict persistence is not configured", ErrorOptions{Code: ErrorCodeCapabilityNotConfigured, Category: ErrorCategoryUnavailable})
	}
	return recorder.RecordSubscriptionConflict(ctx, input)
}

func (s *BillingService) UpsertBillingSubscription(ctx context.Context, state CommerceSubscription) (string, error) {
	if s == nil {
		return "", NewError("billing subscription persistence is not configured", ErrorOptions{Code: ErrorCodeCapabilityNotConfigured, Category: ErrorCategoryUnavailable})
	}
	writer, ok := s.store.(billingSubscriptionWriter)
	if !ok {
		return "", NewError("billing subscription persistence is not configured", ErrorOptions{Code: ErrorCodeCapabilityNotConfigured, Category: ErrorCategoryUnavailable})
	}
	return writer.UpsertBillingSubscriptionState(ctx, state)
}

func (s *BillingService) GetCustomerByUserID(ctx context.Context, accountID, provider string) (*BillingCustomerRecord, error) {
	store, err := s.stateStore()
	if err != nil {
		return nil, err
	}
	return store.GetBillingCustomer(ctx, accountID, provider)
}

func (s *BillingService) ResolveOffer(ctx context.Context, provider, productID, priceID string) (*BillingOffer, error) {
	store, err := s.stateStore()
	if err != nil {
		return nil, err
	}
	return store.ResolveBillingOffer(ctx, provider, productID, priceID, "")
}

func (s *BillingService) ResolveOfferByLookup(ctx context.Context, provider, lookupKey string) (*BillingOffer, error) {
	store, err := s.stateStore()
	if err != nil {
		return nil, err
	}
	return store.ResolveBillingOffer(ctx, provider, "", "", lookupKey)
}

func (s *BillingService) ResolveTopup(ctx context.Context, provider, productID, priceID string) (*BillingTopupResult, error) {
	if s == nil {
		return nil, NewError("billing top-up resolution is not configured", ErrorOptions{Code: ErrorCodeCapabilityNotConfigured, Category: ErrorCategoryUnavailable})
	}
	resolver, ok := s.store.(billingTopupResolver)
	if !ok {
		return nil, NewError("billing top-up resolution is not configured", ErrorOptions{Code: ErrorCodeCapabilityNotConfigured, Category: ErrorCategoryUnavailable})
	}
	return resolver.ResolveBillingTopup(ctx, provider, productID, priceID, "")
}

func (s *BillingService) ResolveTopupByLookup(ctx context.Context, provider, lookupKey string) (*BillingTopupResult, error) {
	if s == nil {
		return nil, NewError("billing top-up resolution is not configured", ErrorOptions{Code: ErrorCodeCapabilityNotConfigured, Category: ErrorCategoryUnavailable})
	}
	resolver, ok := s.store.(billingTopupResolver)
	if !ok {
		return nil, NewError("billing top-up resolution is not configured", ErrorOptions{Code: ErrorCodeCapabilityNotConfigured, Category: ErrorCategoryUnavailable})
	}
	return resolver.ResolveBillingTopup(ctx, provider, "", "", lookupKey)
}

func (s *BillingService) UpsertCustomer(ctx context.Context, provider, providerCustomerID, accountID, email string) error {
	store, err := s.stateStore()
	if err != nil {
		return err
	}
	writer, ok := store.(interface {
		UpsertBillingCustomer(context.Context, BillingCustomerRecord) error
	})
	if !ok {
		return NewError("billing customer persistence is not configured", ErrorOptions{Code: ErrorCodeCapabilityNotConfigured, Category: ErrorCategoryUnavailable})
	}
	return writer.UpsertBillingCustomer(ctx, BillingCustomerRecord{Provider: provider, ProviderCustomerID: providerCustomerID, AccountID: accountID, Email: email})
}

func (s *BillingService) GetAutoRechargeProfile(ctx context.Context, accountID string) (*AutoRechargeProfile, error) {
	store, err := s.autoRechargeStore()
	if err != nil {
		return nil, err
	}
	return store.GetAutoRechargeProfile(ctx, accountID)
}

func (s *BillingService) UpsertAutoRechargeProfile(ctx context.Context, profile AutoRechargeProfile, options AutoRechargeProfileUpsertOptions) error {
	store, err := s.autoRechargeStore()
	if err != nil {
		return err
	}
	return store.UpsertAutoRechargeProfile(ctx, profile, options)
}

func (s *BillingService) ClaimAutoRechargeAttempt(ctx context.Context, claim AutoRechargeAttemptClaim) (*AutoRechargeAttempt, error) {
	store, err := s.autoRechargeStore()
	if err != nil {
		return nil, err
	}
	return store.ClaimAutoRechargeAttempt(ctx, claim)
}

func (s *BillingService) UpdateAutoRechargeAttempt(ctx context.Context, update AutoRechargeAttemptUpdate) error {
	store, err := s.autoRechargeStore()
	if err != nil {
		return err
	}
	return store.UpdateAutoRechargeAttempt(ctx, update)
}

func (s *BillingService) UpdateAutoRechargeAttemptByProviderPayment(ctx context.Context, update AutoRechargeProviderPaymentUpdate) error {
	if s == nil {
		return NewError("billing auto-recharge provider reconciliation is not configured", ErrorOptions{Code: ErrorCodeAutoRechargeNotConfigured, Category: ErrorCategoryUnavailable})
	}
	store, ok := s.store.(autoRechargeProviderPaymentUpdater)
	if !ok {
		return NewError("billing auto-recharge provider reconciliation is not configured", ErrorOptions{Code: ErrorCodeAutoRechargeNotConfigured, Category: ErrorCategoryUnavailable})
	}
	return store.UpdateAutoRechargeAttemptByProviderPayment(ctx, update)
}

func (s *BillingService) CountAutoRechargeAttempts(ctx context.Context, accountID string, since time.Time) (int, error) {
	store, err := s.autoRechargeStore()
	if err != nil {
		return 0, err
	}
	return store.CountAutoRechargeAttempts(ctx, accountID, since)
}

func (s *BillingService) ExpirePastDueGracePeriods(ctx context.Context, now time.Time, limit int) (int, error) {
	if s == nil || s.provisioning == nil || !s.autoSelectEntitlementSource {
		return 0, nil
	}
	state, ok := s.store.(billingGraceStateStore)
	if !ok {
		return 0, NewError("billing grace-period maintenance is not configured", ErrorOptions{Code: ErrorCodeCapabilityNotConfigured, Category: ErrorCategoryUnavailable})
	}
	if now.IsZero() {
		now = time.Now().UTC()
	}
	if limit == 0 {
		limit = 100
	}
	if limit < 1 || limit > 1000 {
		return 0, NewError("billing grace-period limit must be between 1 and 1000", ErrorOptions{Code: ErrorCodeConfig, Category: ErrorCategoryInvalidRequest})
	}
	candidates, err := state.ListExpiredGraceSubscriptions(ctx, now, limit)
	if err != nil {
		return 0, err
	}
	expirer, ok := s.store.(billingGraceExpiryStore)
	if !ok {
		return 0, NewError("atomic billing grace-period expiry is not configured", ErrorOptions{Code: ErrorCodeCapabilityNotConfigured, Category: ErrorCategoryUnavailable})
	}
	expired := 0
	for _, candidate := range candidates {
		if candidate.ID == "" || candidate.GraceEndsAt == nil || candidate.Status != "past_due" || candidate.GraceExpiredAt != nil {
			continue
		}
		marked, markErr := expirer.expirePastDueGracePeriod(ctx, candidate, now, s.terminalPlanKey)
		if markErr != nil {
			return expired, markErr
		}
		if marked {
			expired++
		}
	}
	return expired, nil
}

func (s *BillingService) expireGraceIfNeeded(ctx context.Context, subscription *CommerceSubscription, now time.Time) (*CommerceSubscription, error) {
	if subscription == nil || s == nil || s.provisioning == nil || !s.autoSelectEntitlementSource || subscription.Status != "past_due" || subscription.GraceExpiredAt != nil || subscription.GraceEndsAt == nil || subscription.GraceEndsAt.After(now) || subscription.ID == "" {
		return subscription, nil
	}
	expirer, ok := s.store.(billingGraceExpiryStore)
	if !ok {
		return nil, NewError("atomic billing grace-period expiry is not configured", ErrorOptions{Code: ErrorCodeCapabilityNotConfigured, Category: ErrorCategoryUnavailable})
	}
	marked, err := expirer.expirePastDueGracePeriod(ctx, *subscription, now, s.terminalPlanKey)
	if err != nil {
		return nil, err
	}
	if !marked {
		return subscription, nil
	}
	value := now.UTC()
	copy := *subscription
	copy.GraceExpiredAt = &value
	return &copy, nil
}

func (s *BillingService) PseudonymizeFinancialSubject(ctx context.Context, accountID string) error {
	if s == nil {
		return NewError("billing subject pseudonymization is not configured", ErrorOptions{Code: ErrorCodeCapabilityNotConfigured, Category: ErrorCategoryUnavailable})
	}
	pseudonymizer, ok := s.store.(billingPseudonymizer)
	if !ok {
		return NewError("billing subject pseudonymization is not configured", ErrorOptions{Code: ErrorCodeCapabilityNotConfigured, Category: ErrorCategoryUnavailable})
	}
	changed, err := pseudonymizer.PseudonymizeFinancialSubject(ctx, accountID)
	if err != nil {
		return err
	}
	if !changed {
		return NewError("billing subject was not found", ErrorOptions{Code: ErrorCodeCommerceResourceNotFound, Category: ErrorCategoryNotFound})
	}
	return nil
}

// InvalidateOfferCache mirrors the cross-SDK capability. Go's Postgres store
// performs tenant-scoped uncached RPC lookups, so no local state is retained.
func (s *BillingService) InvalidateOfferCache() {}
