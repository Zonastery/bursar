// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"strings"
	"time"
)

// AutoRechargeState is the durable state of an account's auto-recharge
// profile. A paused profile is enabled but intentionally cannot submit a new
// saved-payment charge until it is retried or re-enabled.
type AutoRechargeState string

const (
	AutoRechargeStateDisabled AutoRechargeState = "disabled"
	AutoRechargeStateActive   AutoRechargeState = "active"
	AutoRechargeStatePaused   AutoRechargeState = "paused"
)

// AutoRechargeAttemptState is the durable lifecycle of a single saved-payment
// charge attempt. The store is authoritative for legal transitions; the SDK
// only requests the state that corresponds to a verified provider response.
type AutoRechargeAttemptState string

const (
	AutoRechargeAttemptClaimed        AutoRechargeAttemptState = "claimed"
	AutoRechargeAttemptSubmitted      AutoRechargeAttemptState = "submitted"
	AutoRechargeAttemptProcessing     AutoRechargeAttemptState = "processing"
	AutoRechargeAttemptUnknown        AutoRechargeAttemptState = "unknown"
	AutoRechargeAttemptSucceeded      AutoRechargeAttemptState = "succeeded"
	AutoRechargeAttemptFailed         AutoRechargeAttemptState = "failed"
	AutoRechargeAttemptActionRequired AutoRechargeAttemptState = "action_required"
)

// AutoRechargeOutcome is the non-error result of evaluating an auto-recharge
// policy. Provider transport and capability failures remain Go errors so an
// application can log or retry them without confusing them for a payment
// outcome.
type AutoRechargeOutcome string

const (
	AutoRechargeOutcomeNotConfigured     AutoRechargeOutcome = "not_configured"
	AutoRechargeOutcomeDisabled          AutoRechargeOutcome = "disabled"
	AutoRechargeOutcomeAboveThreshold    AutoRechargeOutcome = "above_threshold"
	AutoRechargeOutcomeAlreadyProcessing AutoRechargeOutcome = "already_processing"
	AutoRechargeOutcomeLimitReached      AutoRechargeOutcome = "limit_reached"
	AutoRechargeOutcomeSubmitted         AutoRechargeOutcome = "submitted"
	AutoRechargeOutcomeActionRequired    AutoRechargeOutcome = "action_required"
	AutoRechargeOutcomeFailed            AutoRechargeOutcome = "failed"
)

// AutoRechargeWindow is the active policy period resolved from the parsed
// catalog. Start and End are always UTC instants; Timezone retains the catalog
// timezone used to calculate calendar boundaries.
type AutoRechargeWindow struct {
	Unit         string
	Count        int
	Anchor       string
	Timezone     string
	Start        time.Time
	End          time.Time
	DurationDays float64
}

// AutoRechargePolicy is the provider-specific active policy resolved from a
// parsed Bursar catalog. ProductID is provider-internal and is never accepted
// from a browser or other untrusted client request.
type AutoRechargePolicy struct {
	Threshold           Amount
	RearmAbove          Amount
	TopupKey            string
	TopupID             string
	ProductID           string
	Quantity            int64
	MaxChargesPerWindow int
	MaxChargeMinor      int64
	Window              AutoRechargeWindow
}

// AutoRechargeProfile is a persisted account preference and guardrail
// snapshot. Its policy fields are written from the active catalog by Enable;
// a durable store must reject a profile that no longer matches its catalog.
type AutoRechargeProfile struct {
	UserID              string
	Enabled             bool
	State               AutoRechargeState
	Armed               bool
	Provider            string
	TopupID             string
	Quantity            int64
	Threshold           Amount
	MaxChargesPerWindow int
	WindowUnit          string
	WindowCount         int
	WindowAnchor        string
	WindowTimezone      string
	UpdatedAt           time.Time
}

// AutoRechargeAttempt is the durable idempotency and provider-state record for
// one charge. IdempotencyKey must be reused exactly if a returned Unknown
// attempt is resumed after a transport failure.
type AutoRechargeAttempt struct {
	ID                string
	UserID            string
	Provider          string
	IdempotencyKey    string
	ProviderAttemptID string
	TopupID           string
	Quantity          int64
	State             AutoRechargeAttemptState
	WindowStart       time.Time
	WindowEnd         time.Time
	QuotedAmountMinor *int64
	Currency          string
	FailureCode       string
	FailureMessage    string
	Metadata          map[string]any
	CreatedAt         time.Time
	UpdatedAt         time.Time
}

// AutoRechargeAttemptClaim asks a durable store to claim one new attempt. The
// store atomically checks the persisted profile, current balance, cooldown,
// in-flight work, and current policy-window limit before it returns an attempt.
type AutoRechargeAttemptClaim struct {
	UserID         string
	IdempotencyKey string
}

// AutoRechargeAttemptUpdate records the observable provider result. Stores
// must preserve their state-machine checks rather than accepting arbitrary
// caller transitions.
type AutoRechargeAttemptUpdate struct {
	ID                string
	State             AutoRechargeAttemptState
	ProviderAttemptID string
	FailureCode       string
	FailureMessage    string
	Metadata          map[string]any
}

// AutoRechargeProfileUpsertOptions controls a durable profile write.
// ResetCooldown is used only by an explicit retry; normal enable/update flows
// must preserve a currently active cooldown.
type AutoRechargeProfileUpsertOptions struct {
	ResetCooldown bool
}

// AutoRechargeCustomer is the saved-payment customer belonging to one Bursar
// user at a particular provider.
type AutoRechargeCustomer struct {
	UserID             string
	Provider           string
	ProviderCustomerID string
}

// AutoRechargeTopupLookup is the catalog-derived reference a durable store
// resolves to its active provider/environment-scoped top-up record.
type AutoRechargeTopupLookup struct {
	Provider  string
	OfferKey  string
	Reference ProviderReference
}

// AutoRechargeTopup is the durable internal identity for a catalog top-up.
// ProductID is retained here so stores may normalize provider references while
// the service still passes an exact provider product identifier to a charge.
type AutoRechargeTopup struct {
	ID        string
	ProductID string
}

// AutoRechargeStore is the narrow durable port used by AutoRechargeService.
// Implementations must keep balance, cooldown, limits, attempt claims, and
// state transitions in one transactional database authority; the SDK never
// reimplements those financial concurrency checks in process memory.
type AutoRechargeStore interface {
	ResolveAutoRechargeTopup(context.Context, AutoRechargeTopupLookup) (*AutoRechargeTopup, error)
	GetAutoRechargeCustomer(context.Context, string, string) (*AutoRechargeCustomer, error)
	GetAutoRechargeProfile(context.Context, string) (*AutoRechargeProfile, error)
	UpsertAutoRechargeProfile(context.Context, AutoRechargeProfile, AutoRechargeProfileUpsertOptions) error
	ClaimAutoRechargeAttempt(context.Context, AutoRechargeAttemptClaim) (*AutoRechargeAttempt, error)
	UpdateAutoRechargeAttempt(context.Context, AutoRechargeAttemptUpdate) error
	CountAutoRechargeAttempts(context.Context, string, time.Time) (int, error)
}

// AutoRechargeCatalogSource is satisfied by CatalogService. It is deliberately
// limited to already parsed configuration so the auto-recharge core never
// parses an alternate catalog representation or owns catalog caching.
type AutoRechargeCatalogSource interface {
	GetConfig(context.Context) (*BursarConfig, error)
}

// AutoRechargeProviderResolver is satisfied by ProviderRegistry. It lets a
// post-deduction hook select the provider stored in an account's profile
// without coupling the core to a web framework or service locator.
type AutoRechargeProviderResolver interface {
	Get(context.Context, string) (PaymentProvider, error)
}

// PaymentMethodInfo is a provider-normalized saved payment method. The SDK
// uses only the default marker and a minimal customer-safe display projection.
type PaymentMethodInfo struct {
	ID          string
	Last4       string
	Brand       string
	ExpiryMonth int
	ExpiryYear  int
	IsDefault   bool
}

// PaymentMethodsProvider is an optional PaymentProvider capability. A provider
// must return only payment methods already attached to customerID.
type PaymentMethodsProvider interface {
	ListPaymentMethods(context.Context, string) ([]PaymentMethodInfo, error)
}

// SavedPaymentChargeParams is a catalog-derived request to charge an existing
// provider payment method. Metadata must contain only application-safe values;
// Bursar adds the attempt and account identifiers needed by webhook handling.
type SavedPaymentChargeParams struct {
	CustomerID      string
	PaymentMethodID string
	ProductID       string
	Quantity        int64
	ReturnURL       string
	Metadata        map[string]string
	IdempotencyKey  string
}

// SavedPaymentChargeStatus normalizes the provider states relevant to a
// saved-payment top-up. Providers may expose richer states internally but must
// map them into this vocabulary before returning to Bursar.
type SavedPaymentChargeStatus string

const (
	SavedPaymentChargeSucceeded                      SavedPaymentChargeStatus = "succeeded"
	SavedPaymentChargeProcessing                     SavedPaymentChargeStatus = "processing"
	SavedPaymentChargeFailed                         SavedPaymentChargeStatus = "failed"
	SavedPaymentChargeCancelled                      SavedPaymentChargeStatus = "cancelled"
	SavedPaymentChargeRequiresCustomerAction         SavedPaymentChargeStatus = "requires_customer_action"
	SavedPaymentChargeRequiresMerchantAction         SavedPaymentChargeStatus = "requires_merchant_action"
	SavedPaymentChargeRequiresPaymentMethod          SavedPaymentChargeStatus = "requires_payment_method"
	SavedPaymentChargeRequiresConfirmation           SavedPaymentChargeStatus = "requires_confirmation"
	SavedPaymentChargeRequiresCapture                SavedPaymentChargeStatus = "requires_capture"
	SavedPaymentChargePartiallyCaptured              SavedPaymentChargeStatus = "partially_captured"
	SavedPaymentChargePartiallyCapturedAndCapturable SavedPaymentChargeStatus = "partially_captured_and_capturable"
)

// SavedPaymentChargeResult is the provider outcome after an idempotent charge
// submission. A nil ProviderPaymentID is permitted for providers that do not
// allocate an external identifier until later processing.
type SavedPaymentChargeResult struct {
	ProviderPaymentID string
	Status            SavedPaymentChargeStatus
	ActionURL         string
	AmountMinor       *int64
	Currency          string
}

// SavedPaymentChargeQuote is an optional non-mutating provider preview. A
// provider may omit this capability while still supporting saved-payment
// charges, so quote absence is not a configuration error.
type SavedPaymentChargeQuote struct {
	AmountMinor int64
	Currency    string
	TaxMinor    *int64
	ExpiresAt   *time.Time
}

// SavedPaymentPreviewProvider is an optional PaymentProvider capability.
type SavedPaymentPreviewProvider interface {
	PreviewSavedPaymentCharge(context.Context, SavedPaymentChargeParams) (SavedPaymentChargeQuote, error)
}

// SavedPaymentChargeProvider is required to submit an automatic top-up.
type SavedPaymentChargeProvider interface {
	ChargeSavedPaymentMethod(context.Context, SavedPaymentChargeParams) (SavedPaymentChargeResult, error)
}

// AutoRechargeProcessInput supplies the durable credit balance observed after
// a committed debit. Balance is advisory to the service: ClaimAutoRechargeAttempt
// must recheck it atomically before a provider call is allowed.
type AutoRechargeProcessInput struct {
	UserID    string
	Balance   Amount
	ReturnURL string
}

// AutoRechargeProcessResult is the non-error decision returned by an
// auto-recharge evaluation. Charge is populated only after a provider call.
type AutoRechargeProcessResult struct {
	Outcome AutoRechargeOutcome
	Charge  *SavedPaymentChargeResult
}

// AutoRechargeStatus is the customer-safe operational projection used by
// management surfaces. Payment method fields never expose a full PAN or other
// provider credentials.
type AutoRechargeStatus struct {
	Enabled            bool
	State              AutoRechargeState
	ThresholdCredits   Amount
	TopupKey           string
	Quantity           int64
	MaxRecharges       int
	WindowStart        time.Time
	WindowEnd          time.Time
	RechargesInWindow  int
	PaymentMethodID    string
	PaymentMethodLast4 string
	PaymentMethodBrand string
	SuspendedReason    string
	PendingAttemptID   string
	QuoteAmountMinor   *int64
	QuoteCurrency      string
}

// AutoRechargeServiceOptions supplies deterministic test seams without
// changing production behaviour. NewIdempotencyKey must return a non-empty
// key no longer than the store's documented maximum (255 bytes for Bursar's
// PostgreSQL contract).
type AutoRechargeServiceOptions struct {
	Now               func() time.Time
	NewIdempotencyKey func(context.Context, string) (string, error)
}

// AutoRechargeService is a framework-neutral orchestration layer for
// persisted auto-recharge profiles and provider saved-payment capabilities.
// It is safe for concurrent use when its ports are safe for concurrent use.
type AutoRechargeService struct {
	catalog           AutoRechargeCatalogSource
	store             AutoRechargeStore
	now               func() time.Time
	newIdempotencyKey func(context.Context, string) (string, error)
}

// NewAutoRechargeService constructs a service over the parsed active catalog
// and one durable auto-recharge store. It does not start workers or register a
// CLI command; applications choose when to invoke ProcessIfNeeded.
func NewAutoRechargeService(catalog AutoRechargeCatalogSource, store AutoRechargeStore, options AutoRechargeServiceOptions) (*AutoRechargeService, error) {
	if catalog == nil {
		return nil, NewError("auto-recharge requires a catalog source", ErrorOptions{Code: ErrorCodeConfig, Category: ErrorCategoryInvalidRequest})
	}
	if store == nil {
		return nil, NewError("auto-recharge requires a durable store", ErrorOptions{Code: ErrorCodeConfig, Category: ErrorCategoryInvalidRequest})
	}
	now := options.Now
	if now == nil {
		now = time.Now
	}
	key := options.NewIdempotencyKey
	if key == nil {
		key = defaultAutoRechargeIdempotencyKey
	}
	return &AutoRechargeService{catalog: catalog, store: store, now: now, newIdempotencyKey: key}, nil
}

// ResolvePolicy returns the currently active, provider-specific auto-recharge
// policy. A nil result means the active catalog has no usable auto-recharge
// policy for provider; it is not an error condition for normal evaluation.
func (s *AutoRechargeService) ResolvePolicy(ctx context.Context, provider PaymentProvider) (*AutoRechargePolicy, error) {
	if s == nil || s.catalog == nil || s.store == nil {
		return nil, NewError("auto-recharge service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	providerName, err := autoRechargeProviderName(provider)
	if err != nil {
		return nil, err
	}
	return s.resolvePolicy(ctx, providerName)
}

// GetProfile returns an account's persisted auto-recharge profile, if one
// exists. It does not infer state from a current catalog or provider.
func (s *AutoRechargeService) GetProfile(ctx context.Context, userID string) (*AutoRechargeProfile, error) {
	if s == nil || s.store == nil {
		return nil, NewError("auto-recharge service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	userID, err := autoRechargeUserID(userID)
	if err != nil {
		return nil, err
	}
	profile, err := s.store.GetAutoRechargeProfile(ctx, userID)
	if err != nil {
		return nil, err
	}
	if err := validateAutoRechargeProfile(profile, userID); err != nil {
		return nil, err
	}
	return cloneAutoRechargeProfile(profile), nil
}

// Quote returns a saved-payment preview when the catalog, customer, payment
// method, and provider preview capability are all available. A nil quote is a
// normal result when no active policy, saved payment method, or preview
// capability exists.
func (s *AutoRechargeService) Quote(ctx context.Context, userID string, provider PaymentProvider) (*SavedPaymentChargeQuote, error) {
	userID, err := autoRechargeUserID(userID)
	if err != nil {
		return nil, err
	}
	policy, err := s.ResolvePolicy(ctx, provider)
	if err != nil || policy == nil {
		return nil, err
	}
	payment, err := s.paymentMethod(ctx, userID, provider)
	if err != nil || payment == nil {
		return nil, err
	}
	preview, ok := provider.(SavedPaymentPreviewProvider)
	if !ok {
		return nil, nil
	}
	quote, err := preview.PreviewSavedPaymentCharge(ctx, s.savedPaymentParams(*policy, payment.customerID, payment.method, "", "auto-recharge-preview"))
	if err != nil {
		return nil, err
	}
	if err := validateSavedPaymentQuote(quote); err != nil {
		return nil, err
	}
	return &quote, nil
}

// GetStatus returns a policy and payment-method aware customer-safe status. A
// nil status means the active catalog does not configure auto-recharge for the
// selected provider.
func (s *AutoRechargeService) GetStatus(ctx context.Context, userID string, provider PaymentProvider) (*AutoRechargeStatus, error) {
	userID, err := autoRechargeUserID(userID)
	if err != nil {
		return nil, err
	}
	policy, err := s.ResolvePolicy(ctx, provider)
	if err != nil || policy == nil {
		return nil, err
	}
	profile, err := s.GetProfile(ctx, userID)
	if err != nil {
		return nil, err
	}
	providerName, err := autoRechargeProviderName(provider)
	if err != nil {
		return nil, err
	}
	if err := autoRechargeProfileProviderMatches(profile, providerName); err != nil {
		return nil, err
	}

	var payment *autoRechargePayment
	if profile != nil && profile.Enabled {
		payment, err = s.paymentMethod(ctx, userID, provider)
		if err != nil {
			return nil, err
		}
	}
	var quote *SavedPaymentChargeQuote
	if payment != nil {
		if preview, ok := provider.(SavedPaymentPreviewProvider); ok {
			result, previewErr := preview.PreviewSavedPaymentCharge(ctx, s.savedPaymentParams(*policy, payment.customerID, payment.method, "", "auto-recharge-preview"))
			if previewErr != nil {
				return nil, previewErr
			}
			if err := validateSavedPaymentQuote(result); err != nil {
				return nil, err
			}
			quote = &result
		}
	}

	count, err := s.store.CountAutoRechargeAttempts(ctx, userID, policy.Window.Start)
	if err != nil {
		return nil, err
	}
	status := &AutoRechargeStatus{
		Enabled:           profile != nil && profile.Enabled,
		State:             AutoRechargeStateDisabled,
		ThresholdCredits:  policy.Threshold,
		TopupKey:          policy.TopupKey,
		Quantity:          policy.Quantity,
		MaxRecharges:      policy.MaxChargesPerWindow,
		WindowStart:       policy.Window.Start,
		WindowEnd:         policy.Window.End,
		RechargesInWindow: count,
	}
	if profile != nil && profile.Enabled {
		status.State = profile.State
		if profile.State == AutoRechargeStatePaused {
			status.SuspendedReason = "auto_recharge_paused"
		}
	}
	if payment != nil {
		status.PaymentMethodID = payment.method.ID
		status.PaymentMethodLast4 = payment.method.Last4
		status.PaymentMethodBrand = payment.method.Brand
	}
	if quote != nil {
		status.QuoteAmountMinor = int64Pointer(quote.AmountMinor)
		status.QuoteCurrency = quote.Currency
	}
	return status, nil
}

// Enable verifies a saved payment method, persists an active profile matching
// the parsed catalog, evaluates the current balance, then returns refreshed
// status. A provider transport error after persistence is returned so callers
// can safely inspect status or retry with the durable attempt key.
func (s *AutoRechargeService) Enable(ctx context.Context, provider PaymentProvider, input AutoRechargeProcessInput) (*AutoRechargeStatus, error) {
	userID, err := autoRechargeUserID(input.UserID)
	if err != nil {
		return nil, err
	}
	policy, err := s.ResolvePolicy(ctx, provider)
	if err != nil {
		return nil, err
	}
	if policy == nil {
		return nil, NewError("auto-recharge is not configured for this catalog", ErrorOptions{Code: ErrorCodeAutoRechargeNotConfigured, Category: ErrorCategoryUnavailable})
	}
	payment, err := s.paymentMethod(ctx, userID, provider)
	if err != nil {
		return nil, err
	}
	if payment == nil {
		return nil, NewError("a saved payment method is required", ErrorOptions{Code: ErrorCodePaymentMethodRequired, Category: ErrorCategoryPaymentRequired})
	}
	providerName, err := autoRechargeProviderName(provider)
	if err != nil {
		return nil, err
	}
	profile := AutoRechargeProfile{
		UserID:              userID,
		Enabled:             true,
		State:               AutoRechargeStateActive,
		Armed:               true,
		Provider:            providerName,
		TopupID:             policy.TopupID,
		Quantity:            policy.Quantity,
		Threshold:           policy.Threshold,
		MaxChargesPerWindow: policy.MaxChargesPerWindow,
		WindowUnit:          policy.Window.Unit,
		WindowCount:         policy.Window.Count,
		WindowAnchor:        policy.Window.Anchor,
		WindowTimezone:      policy.Window.Timezone,
	}
	if err := s.store.UpsertAutoRechargeProfile(ctx, profile, AutoRechargeProfileUpsertOptions{}); err != nil {
		return nil, err
	}
	if _, err := s.ProcessIfNeeded(ctx, provider, AutoRechargeProcessInput{UserID: userID, Balance: input.Balance, ReturnURL: input.ReturnURL}); err != nil {
		return nil, err
	}
	return s.GetStatus(ctx, userID, provider)
}

// Disable marks an existing profile disabled. Disabling a user with no profile
// is idempotently successful and does not require the active catalog.
func (s *AutoRechargeService) Disable(ctx context.Context, userID string) error {
	userID, err := autoRechargeUserID(userID)
	if err != nil {
		return err
	}
	profile, err := s.GetProfile(ctx, userID)
	if err != nil || profile == nil {
		return err
	}
	profile = cloneAutoRechargeProfile(profile)
	profile.Enabled = false
	profile.State = AutoRechargeStateDisabled
	profile.Armed = true
	return s.store.UpsertAutoRechargeProfile(ctx, *profile, AutoRechargeProfileUpsertOptions{})
}

// Retry re-arms an enabled profile and resets only its persisted cooldown. It
// intentionally does not create a second charge when a webhook-owned attempt
// is still processing; ProcessIfNeeded will return already_processing instead.
func (s *AutoRechargeService) Retry(ctx context.Context, provider PaymentProvider, input AutoRechargeProcessInput) (AutoRechargeProcessResult, error) {
	userID, err := autoRechargeUserID(input.UserID)
	if err != nil {
		return AutoRechargeProcessResult{}, err
	}
	profile, err := s.GetProfile(ctx, userID)
	if err != nil {
		return AutoRechargeProcessResult{}, err
	}
	if profile == nil || !profile.Enabled {
		return AutoRechargeProcessResult{}, NewError("auto-recharge is disabled for this account", ErrorOptions{Code: ErrorCodeAutoRechargeDisabled, Category: ErrorCategoryConflict})
	}
	providerName, err := autoRechargeProviderName(provider)
	if err != nil {
		return AutoRechargeProcessResult{}, err
	}
	if err := autoRechargeProfileProviderMatches(profile, providerName); err != nil {
		return AutoRechargeProcessResult{}, err
	}
	profile = cloneAutoRechargeProfile(profile)
	profile.State = AutoRechargeStateActive
	profile.Armed = true
	if err := s.store.UpsertAutoRechargeProfile(ctx, *profile, AutoRechargeProfileUpsertOptions{ResetCooldown: true}); err != nil {
		return AutoRechargeProcessResult{}, err
	}
	input.UserID = userID
	return s.ProcessIfNeeded(ctx, provider, input)
}

// ProcessIfNeeded evaluates a persisted profile after a credit debit. It
// delegates the final balance, cooldown, policy-window, and in-flight checks
// to ClaimAutoRechargeAttempt before it submits a provider charge.
func (s *AutoRechargeService) ProcessIfNeeded(ctx context.Context, provider PaymentProvider, input AutoRechargeProcessInput) (AutoRechargeProcessResult, error) {
	userID, err := autoRechargeUserID(input.UserID)
	if err != nil {
		return AutoRechargeProcessResult{}, err
	}
	policy, err := s.ResolvePolicy(ctx, provider)
	if err != nil {
		return AutoRechargeProcessResult{}, err
	}
	if policy == nil {
		return AutoRechargeProcessResult{Outcome: AutoRechargeOutcomeNotConfigured}, nil
	}
	profile, err := s.GetProfile(ctx, userID)
	if err != nil {
		return AutoRechargeProcessResult{}, err
	}
	if profile == nil || !profile.Enabled || profile.State != AutoRechargeStateActive {
		return AutoRechargeProcessResult{Outcome: AutoRechargeOutcomeDisabled}, nil
	}
	providerName, err := autoRechargeProviderName(provider)
	if err != nil {
		return AutoRechargeProcessResult{}, err
	}
	if err := autoRechargeProfileProviderMatches(profile, providerName); err != nil {
		return AutoRechargeProcessResult{}, err
	}
	if input.Balance.GreaterThanOrEqual(policy.Threshold) {
		return AutoRechargeProcessResult{Outcome: AutoRechargeOutcomeAboveThreshold}, nil
	}
	charger, ok := provider.(SavedPaymentChargeProvider)
	if !ok {
		return AutoRechargeProcessResult{}, autoRechargeCapabilityError(providerName, "charge_saved_payment_method")
	}
	payment, err := s.paymentMethod(ctx, userID, provider)
	if err != nil {
		return AutoRechargeProcessResult{}, err
	}
	if payment == nil {
		return AutoRechargeProcessResult{Outcome: AutoRechargeOutcomeFailed}, nil
	}
	idempotencyKey, err := s.newIdempotencyKey(ctx, userID)
	if err != nil {
		return AutoRechargeProcessResult{}, err
	}
	idempotencyKey, err = requireStableKey(idempotencyKey, "auto-recharge idempotency key")
	if err != nil {
		return AutoRechargeProcessResult{}, err
	}
	attempt, err := s.store.ClaimAutoRechargeAttempt(ctx, AutoRechargeAttemptClaim{UserID: userID, IdempotencyKey: idempotencyKey})
	if err != nil {
		return AutoRechargeProcessResult{}, err
	}
	if attempt == nil {
		return AutoRechargeProcessResult{Outcome: AutoRechargeOutcomeLimitReached}, nil
	}
	if err := validateAutoRechargeAttempt(attempt, providerName, userID); err != nil {
		return AutoRechargeProcessResult{}, err
	}
	if attempt.IdempotencyKey != idempotencyKey && attempt.State != AutoRechargeAttemptUnknown {
		return AutoRechargeProcessResult{Outcome: AutoRechargeOutcomeAlreadyProcessing}, nil
	}

	charge, chargeErr := charger.ChargeSavedPaymentMethod(ctx, s.savedPaymentParams(*policy, payment.customerID, payment.method, input.ReturnURL, attempt.IdempotencyKey, attempt.ID, userID))
	if chargeErr != nil {
		updateErr := s.store.UpdateAutoRechargeAttempt(ctx, AutoRechargeAttemptUpdate{
			ID:             attempt.ID,
			State:          AutoRechargeAttemptUnknown,
			FailureCode:    "provider_request_failed",
			FailureMessage: autoRechargeDiagnosticSummary(chargeErr),
		})
		if updateErr != nil {
			return AutoRechargeProcessResult{}, errors.Join(chargeErr, updateErr)
		}
		return AutoRechargeProcessResult{}, chargeErr
	}
	if err := validateSavedPaymentCharge(charge); err != nil {
		return AutoRechargeProcessResult{}, err
	}
	chargeCopy := charge
	update := AutoRechargeAttemptUpdate{ID: attempt.ID, ProviderAttemptID: charge.ProviderPaymentID}
	switch charge.Status {
	case SavedPaymentChargeRequiresCustomerAction:
		update.State = AutoRechargeAttemptActionRequired
		if err := s.store.UpdateAutoRechargeAttempt(ctx, update); err != nil {
			return AutoRechargeProcessResult{Charge: &chargeCopy}, err
		}
		return AutoRechargeProcessResult{Outcome: AutoRechargeOutcomeActionRequired, Charge: &chargeCopy}, nil
	case SavedPaymentChargeSucceeded,
		SavedPaymentChargeProcessing,
		SavedPaymentChargeRequiresMerchantAction,
		SavedPaymentChargeRequiresConfirmation,
		SavedPaymentChargeRequiresCapture,
		SavedPaymentChargePartiallyCaptured,
		SavedPaymentChargePartiallyCapturedAndCapturable:
		update.State = AutoRechargeAttemptProcessing
		if err := s.store.UpdateAutoRechargeAttempt(ctx, update); err != nil {
			return AutoRechargeProcessResult{Charge: &chargeCopy}, err
		}
		return AutoRechargeProcessResult{Outcome: AutoRechargeOutcomeSubmitted, Charge: &chargeCopy}, nil
	default:
		update.State = AutoRechargeAttemptFailed
		update.FailureCode = "payment_failed"
		if err := s.store.UpdateAutoRechargeAttempt(ctx, update); err != nil {
			return AutoRechargeProcessResult{Charge: &chargeCopy}, err
		}
		return AutoRechargeProcessResult{Outcome: AutoRechargeOutcomeFailed, Charge: &chargeCopy}, nil
	}
}

// ProcessPostDeduction evaluates a post-commit credit result. It is useful to
// applications that want explicit control over provider selection rather than
// registering PostDeductionHook.
func (s *AutoRechargeService) ProcessPostDeduction(ctx context.Context, provider PaymentProvider, deduction PostDeductionContext, returnURL string) (AutoRechargeProcessResult, error) {
	if deduction.Deduction.BalanceAfter == nil {
		return AutoRechargeProcessResult{Outcome: AutoRechargeOutcomeAboveThreshold}, nil
	}
	return s.ProcessIfNeeded(ctx, provider, AutoRechargeProcessInput{
		UserID:    deduction.UserID,
		Balance:   *deduction.Deduction.BalanceAfter,
		ReturnURL: returnURL,
	})
}

// PostDeductionHook returns a CreditsService-compatible, framework-neutral
// hook. It looks up the profile's selected provider from the supplied resolver
// and leaves delivery policy to CreditsService, which already isolates hook
// failures after a credit debit has committed.
func (s *AutoRechargeService) PostDeductionHook(providers AutoRechargeProviderResolver, returnURL string) PostDeductionHook {
	return func(ctx context.Context, deduction PostDeductionContext) error {
		if deduction.Deduction.BalanceAfter == nil {
			return nil
		}
		if providers == nil {
			return NewError("auto-recharge provider resolver is not configured", ErrorOptions{Code: ErrorCodeCapabilityNotConfigured, Category: ErrorCategoryUnavailable})
		}
		profile, err := s.GetProfile(ctx, deduction.UserID)
		if err != nil || profile == nil || !profile.Enabled || profile.State != AutoRechargeStateActive {
			return err
		}
		provider, err := providers.Get(ctx, profile.Provider)
		if err != nil {
			return err
		}
		_, err = s.ProcessPostDeduction(ctx, provider, deduction, returnURL)
		return err
	}
}

func (s *AutoRechargeService) resolvePolicy(ctx context.Context, providerName string) (*AutoRechargePolicy, error) {
	config, err := s.catalog.GetConfig(ctx)
	if err != nil {
		return nil, err
	}
	if config == nil {
		return nil, NewError("auto-recharge catalog source returned nil configuration", ErrorOptions{Code: ErrorCodeCatalogNotLoaded, Category: ErrorCategoryUnavailable})
	}
	guardrails := config.Commerce.AutoRecharge
	if guardrails == nil {
		return nil, nil
	}
	if len(guardrails.EligibleTopups) == 0 {
		return nil, autoRechargeConfigError("eligible top-ups must not be empty")
	}
	topupKey := strings.TrimSpace(guardrails.EligibleTopups[0])
	offer, found := config.Commerce.Offers[topupKey]
	if !found || offer.Type != "topup" {
		return nil, autoRechargeConfigError("eligible auto-recharge top-up is missing or is not a top-up offer")
	}
	reference, found := offer.Providers[providerName]
	if !found {
		return nil, nil
	}
	productID := providerProductID(reference)
	if productID == "" {
		return nil, autoRechargeConfigError("auto-recharge top-up provider reference has no product identifier")
	}
	if guardrails.Quantity.Default < 1 || guardrails.Limits.MaxPurchases < 1 {
		return nil, autoRechargeConfigError("auto-recharge quantity and max purchases must be positive")
	}
	topup, err := s.store.ResolveAutoRechargeTopup(ctx, AutoRechargeTopupLookup{Provider: providerName, OfferKey: topupKey, Reference: reference})
	if err != nil {
		return nil, err
	}
	if topup == nil {
		return nil, nil
	}
	topupID, err := requireText(topup.ID, "auto-recharge top-up ID")
	if err != nil {
		return nil, err
	}
	window, err := resolveAutoRechargeWindow(guardrails.Limits.Window, s.now())
	if err != nil {
		return nil, err
	}
	return &AutoRechargePolicy{
		Threshold:           guardrails.BalanceBelow.Default,
		RearmAbove:          guardrails.RearmAbove,
		TopupKey:            topupKey,
		TopupID:             topupID,
		ProductID:           productID,
		Quantity:            int64(guardrails.Quantity.Default),
		MaxChargesPerWindow: guardrails.Limits.MaxPurchases,
		MaxChargeMinor:      guardrails.Limits.MaxChargeMinor,
		Window:              window,
	}, nil
}

type autoRechargePayment struct {
	customerID string
	method     PaymentMethodInfo
}

func (s *AutoRechargeService) paymentMethod(ctx context.Context, userID string, provider PaymentProvider) (*autoRechargePayment, error) {
	providerName, err := autoRechargeProviderName(provider)
	if err != nil {
		return nil, err
	}
	customer, err := s.store.GetAutoRechargeCustomer(ctx, userID, providerName)
	if err != nil || customer == nil {
		return nil, err
	}
	if customer.UserID != "" && strings.TrimSpace(customer.UserID) != userID {
		return nil, NewStoreError("auto-recharge customer does not belong to requested user", ErrorOptions{Details: map[string]any{"user_id": userID}})
	}
	if customer.Provider != "" && strings.TrimSpace(customer.Provider) != providerName {
		return nil, NewStoreError("auto-recharge customer does not belong to selected provider", ErrorOptions{Details: map[string]any{"provider": providerName}})
	}
	customerID, err := requireText(customer.ProviderCustomerID, "provider customer ID")
	if err != nil {
		return nil, err
	}
	methodsProvider, ok := provider.(PaymentMethodsProvider)
	if !ok {
		return nil, autoRechargeCapabilityError(providerName, "list_payment_methods")
	}
	methods, err := methodsProvider.ListPaymentMethods(ctx, customerID)
	if err != nil {
		return nil, err
	}
	var selected *PaymentMethodInfo
	for index := range methods {
		candidate := methods[index]
		if !candidate.IsDefault {
			continue
		}
		if selected != nil {
			return nil, NewError("provider returned multiple default payment methods", ErrorOptions{Code: ErrorCodeProviderResponseInvalid, Category: ErrorCategoryUnavailable, Details: map[string]any{"provider": providerName}})
		}
		copy := candidate
		selected = &copy
	}
	if selected == nil && len(methods) == 1 {
		copy := methods[0]
		selected = &copy
	}
	if selected == nil {
		return nil, nil
	}
	if err := validatePaymentMethod(*selected, providerName); err != nil {
		return nil, err
	}
	return &autoRechargePayment{customerID: customerID, method: *selected}, nil
}

func (s *AutoRechargeService) savedPaymentParams(policy AutoRechargePolicy, customerID string, method PaymentMethodInfo, returnURL, idempotencyKey string, details ...string) SavedPaymentChargeParams {
	metadata := map[string]string{}
	if len(details) > 0 && strings.TrimSpace(details[0]) != "" {
		metadata["auto_recharge_attempt_id"] = strings.TrimSpace(details[0])
	}
	if len(details) > 1 && strings.TrimSpace(details[1]) != "" {
		metadata["bursar_account_id"] = strings.TrimSpace(details[1])
	}
	if len(metadata) > 0 {
		metadata["purpose"] = "credit_topup"
	}
	return SavedPaymentChargeParams{
		CustomerID:      customerID,
		PaymentMethodID: method.ID,
		ProductID:       policy.ProductID,
		Quantity:        policy.Quantity,
		ReturnURL:       strings.TrimSpace(returnURL),
		Metadata:        metadata,
		IdempotencyKey:  idempotencyKey,
	}
}

func resolveAutoRechargeWindow(window Window, now time.Time) (AutoRechargeWindow, error) {
	if now.IsZero() || now.Location() == nil {
		return AutoRechargeWindow{}, autoRechargeConfigError("auto-recharge policy window requires a timezone-aware instant")
	}
	now = now.UTC()
	switch window.Type {
	case "rolling":
		if window.Duration == nil || window.Duration.Count < 1 {
			return AutoRechargeWindow{}, autoRechargeConfigError("rolling auto-recharge window requires a positive duration")
		}
		end := now
		var start time.Time
		switch window.Duration.Unit {
		case "second":
			start = end.Add(-time.Duration(window.Duration.Count) * time.Second)
		case "minute":
			start = end.Add(-time.Duration(window.Duration.Count) * time.Minute)
		case "hour":
			start = end.Add(-time.Duration(window.Duration.Count) * time.Hour)
		case "day":
			start = end.Add(-time.Duration(window.Duration.Count) * 24 * time.Hour)
		case "week":
			start = end.Add(-time.Duration(window.Duration.Count) * 7 * 24 * time.Hour)
		default:
			return AutoRechargeWindow{}, autoRechargeConfigError("auto-recharge rolling window unit is not supported")
		}
		return AutoRechargeWindow{
			Unit: window.Duration.Unit, Count: window.Duration.Count, Anchor: "rolling", Timezone: "UTC", Start: start, End: end,
			DurationDays: end.Sub(start).Hours() / 24,
		}, nil
	case "calendar":
		if window.Count < 1 {
			return AutoRechargeWindow{}, autoRechargeConfigError("calendar auto-recharge window requires a positive count")
		}
		timezone := strings.TrimSpace(window.Timezone)
		if timezone == "" {
			timezone = "UTC"
		}
		location, err := time.LoadLocation(timezone)
		if err != nil {
			return AutoRechargeWindow{}, autoRechargeConfigError("calendar auto-recharge window has an invalid timezone")
		}
		zonedNow := now.In(location)
		start, end, err := resolveAutoRechargeCalendarWindow(zonedNow, window.Unit, window.Count)
		if err != nil {
			return AutoRechargeWindow{}, err
		}
		startUTC, endUTC := start.UTC(), end.UTC()
		return AutoRechargeWindow{
			Unit: window.Unit, Count: window.Count, Anchor: "calendar", Timezone: timezone, Start: startUTC, End: endUTC,
			DurationDays: endUTC.Sub(startUTC).Hours() / 24,
		}, nil
	default:
		return AutoRechargeWindow{}, autoRechargeConfigError("auto-recharge window must be rolling or calendar")
	}
}

func resolveAutoRechargeCalendarWindow(now time.Time, unit string, count int) (time.Time, time.Time, error) {
	location := now.Location()
	switch unit {
	case "day", "week":
		anchor := time.Date(2000, 1, 1, 0, 0, 0, 0, location)
		if unit == "week" {
			anchor = time.Date(2000, 1, 3, 0, 0, 0, 0, location)
		}
		calendarNow := time.Date(now.Year(), now.Month(), now.Day(), 0, 0, 0, 0, time.UTC)
		calendarAnchor := time.Date(anchor.Year(), anchor.Month(), anchor.Day(), 0, 0, 0, 0, time.UTC)
		elapsedDays := int(calendarNow.Sub(calendarAnchor) / (24 * time.Hour))
		stepDays := count
		if unit == "week" {
			stepDays *= 7
		}
		start := anchor.AddDate(0, 0, floorDivide(elapsedDays, stepDays)*stepDays)
		return start, start.AddDate(0, 0, stepDays), nil
	case "month":
		currentMonth := now.Year()*12 + int(now.Month()) - 1
		anchorMonth := 2000*12 + int(time.January) - 1
		startMonth := anchorMonth + floorDivide(currentMonth-anchorMonth, count)*count
		start := time.Date(startMonth/12, time.Month(startMonth%12+1), 1, 0, 0, 0, 0, location)
		return start, start.AddDate(0, count, 0), nil
	case "year":
		startYear := 2000 + floorDivide(now.Year()-2000, count)*count
		start := time.Date(startYear, time.January, 1, 0, 0, 0, 0, location)
		return start, start.AddDate(count, 0, 0), nil
	default:
		return time.Time{}, time.Time{}, autoRechargeConfigError("auto-recharge calendar window unit is not supported")
	}
}

func floorDivide(dividend, divisor int) int {
	quotient := dividend / divisor
	remainder := dividend % divisor
	if remainder != 0 && ((remainder < 0) != (divisor < 0)) {
		quotient--
	}
	return quotient
}

func defaultAutoRechargeIdempotencyKey(_ context.Context, _ string) (string, error) {
	var random [24]byte
	if _, err := rand.Read(random[:]); err != nil {
		return "", fmt.Errorf("generate auto-recharge idempotency key: %w", err)
	}
	return "auto-recharge:" + hex.EncodeToString(random[:]), nil
}

func autoRechargeUserID(userID string) (string, error) {
	return requireText(userID, "auto-recharge user ID")
}

func autoRechargeProviderName(provider PaymentProvider) (string, error) {
	if provider == nil {
		return "", NewError("payment provider is required", ErrorOptions{Code: ErrorCodeCapabilityNotConfigured, Category: ErrorCategoryInvalidRequest})
	}
	return requireText(provider.Name(), "payment provider name")
}

func autoRechargeProfileProviderMatches(profile *AutoRechargeProfile, providerName string) error {
	if profile == nil || strings.TrimSpace(profile.Provider) == "" || profile.Provider == providerName {
		return nil
	}
	return NewError("auto-recharge profile provider does not match selected provider", ErrorOptions{
		Code:     ErrorCodeProviderResponseInvalid,
		Category: ErrorCategoryConflict,
		Details:  map[string]any{"profile_provider": profile.Provider, "provider": providerName},
	})
}

func validateAutoRechargeAttempt(attempt *AutoRechargeAttempt, providerName, userID string) error {
	if attempt == nil {
		return NewStoreError("auto-recharge attempt is required", ErrorOptions{})
	}
	if _, err := requireText(attempt.ID, "auto-recharge attempt ID"); err != nil {
		return err
	}
	if _, err := requireStableKey(attempt.IdempotencyKey, "auto-recharge attempt idempotency key"); err != nil {
		return err
	}
	if strings.TrimSpace(attempt.UserID) != userID {
		return NewStoreError("auto-recharge attempt does not belong to requested user", ErrorOptions{Details: map[string]any{"user_id": userID}})
	}
	if strings.TrimSpace(attempt.Provider) != providerName {
		return NewStoreError("auto-recharge attempt does not belong to selected provider", ErrorOptions{Details: map[string]any{"provider": providerName}})
	}
	switch attempt.State {
	case AutoRechargeAttemptClaimed, AutoRechargeAttemptSubmitted, AutoRechargeAttemptProcessing, AutoRechargeAttemptUnknown, AutoRechargeAttemptSucceeded, AutoRechargeAttemptFailed, AutoRechargeAttemptActionRequired:
		return nil
	default:
		return NewStoreError("auto-recharge attempt has an invalid state", ErrorOptions{Details: map[string]any{"state": attempt.State}})
	}
}

func validateAutoRechargeProfile(profile *AutoRechargeProfile, userID string) error {
	if profile == nil {
		return nil
	}
	if strings.TrimSpace(profile.UserID) != userID {
		return NewStoreError("auto-recharge profile does not belong to requested user", ErrorOptions{Details: map[string]any{"user_id": userID}})
	}
	if !profile.Enabled {
		if profile.State != "" && profile.State != AutoRechargeStateDisabled {
			return NewStoreError("disabled auto-recharge profile has an invalid state", ErrorOptions{Details: map[string]any{"state": profile.State}})
		}
		return nil
	}
	if profile.State != AutoRechargeStateActive && profile.State != AutoRechargeStatePaused {
		return NewStoreError("enabled auto-recharge profile has an invalid state", ErrorOptions{Details: map[string]any{"state": profile.State}})
	}
	if strings.TrimSpace(profile.Provider) == "" || strings.TrimSpace(profile.TopupID) == "" || profile.Quantity < 1 || profile.MaxChargesPerWindow < 1 || profile.WindowCount < 1 || profile.Threshold.IsNegative() {
		return NewStoreError("enabled auto-recharge profile is malformed", ErrorOptions{Details: map[string]any{"user_id": userID}})
	}
	if profile.WindowAnchor != "calendar" && profile.WindowAnchor != "rolling" {
		return NewStoreError("enabled auto-recharge profile has an invalid window anchor", ErrorOptions{Details: map[string]any{"anchor": profile.WindowAnchor}})
	}
	if strings.TrimSpace(profile.WindowUnit) == "" || strings.TrimSpace(profile.WindowTimezone) == "" {
		return NewStoreError("enabled auto-recharge profile has an incomplete window", ErrorOptions{Details: map[string]any{"user_id": userID}})
	}
	return nil
}

func validatePaymentMethod(method PaymentMethodInfo, providerName string) error {
	if _, err := requireText(method.ID, "payment method ID"); err != nil {
		return err
	}
	if !isPaymentMethodLast4(method.Last4) || strings.TrimSpace(method.Brand) == "" {
		return NewError("provider returned invalid payment method last4", ErrorOptions{Code: ErrorCodeProviderResponseInvalid, Category: ErrorCategoryUnavailable, Details: map[string]any{"provider": providerName}})
	}
	if method.ExpiryMonth < 1 || method.ExpiryMonth > 12 || method.ExpiryYear < 1 {
		return NewError("provider returned invalid payment method expiry", ErrorOptions{Code: ErrorCodeProviderResponseInvalid, Category: ErrorCategoryUnavailable, Details: map[string]any{"provider": providerName}})
	}
	return nil
}

func validateSavedPaymentQuote(quote SavedPaymentChargeQuote) error {
	if quote.AmountMinor < 0 {
		return NewError("provider returned a negative saved-payment quote", ErrorOptions{Code: ErrorCodeProviderResponseInvalid, Category: ErrorCategoryUnavailable})
	}
	if !isCurrencyCode(quote.Currency) {
		return NewError("provider returned an invalid saved-payment quote currency", ErrorOptions{Code: ErrorCodeProviderResponseInvalid, Category: ErrorCategoryUnavailable})
	}
	return nil
}

func validateSavedPaymentCharge(charge SavedPaymentChargeResult) error {
	switch charge.Status {
	case SavedPaymentChargeSucceeded,
		SavedPaymentChargeProcessing,
		SavedPaymentChargeFailed,
		SavedPaymentChargeCancelled,
		SavedPaymentChargeRequiresCustomerAction,
		SavedPaymentChargeRequiresMerchantAction,
		SavedPaymentChargeRequiresPaymentMethod,
		SavedPaymentChargeRequiresConfirmation,
		SavedPaymentChargeRequiresCapture,
		SavedPaymentChargePartiallyCaptured,
		SavedPaymentChargePartiallyCapturedAndCapturable:
	default:
		return NewError("provider returned saved-payment charge without status", ErrorOptions{Code: ErrorCodeProviderResponseInvalid, Category: ErrorCategoryUnavailable})
	}
	if charge.AmountMinor != nil && *charge.AmountMinor < 0 {
		return NewError("provider returned a negative saved-payment charge amount", ErrorOptions{Code: ErrorCodeProviderResponseInvalid, Category: ErrorCategoryUnavailable})
	}
	if charge.Currency != "" && !isCurrencyCode(charge.Currency) {
		return NewError("provider returned an invalid saved-payment charge currency", ErrorOptions{Code: ErrorCodeProviderResponseInvalid, Category: ErrorCategoryUnavailable})
	}
	return nil
}

func isPaymentMethodLast4(value string) bool {
	if len(value) != 4 {
		return false
	}
	for _, character := range value {
		if character < '0' || character > '9' {
			return false
		}
	}
	return true
}

func isCurrencyCode(value string) bool {
	if len(value) != 3 {
		return false
	}
	for _, character := range value {
		if character < 'A' || character > 'Z' {
			return false
		}
	}
	return true
}

func autoRechargeCapabilityError(provider, capability string) *BursarError {
	return NewError(fmt.Sprintf("payment provider %q does not support %q", provider, capability), ErrorOptions{
		Code:     ErrorCodeProviderCapabilityNotSupported,
		Category: ErrorCategoryUnavailable,
		Details:  map[string]any{"provider": provider, "capability": capability},
	})
}

func autoRechargeConfigError(message string) *BursarError {
	return NewError(message, ErrorOptions{Code: ErrorCodeConfig, Category: ErrorCategoryInvalidRequest})
}

func autoRechargeDiagnosticSummary(err error) string {
	if bursarErr, ok := AsBursarError(err); ok && bursarErr.Code != "" {
		return "auto_recharge_provider_failed:" + string(bursarErr.Code)
	}
	return "auto_recharge_provider_failed:provider_error"
}

func cloneAutoRechargeProfile(profile *AutoRechargeProfile) *AutoRechargeProfile {
	if profile == nil {
		return nil
	}
	clone := *profile
	return &clone
}

func int64Pointer(value int64) *int64 {
	copy := value
	return &copy
}
