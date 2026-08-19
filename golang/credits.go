package bursar

import (
	"container/list"
	"context"
	"fmt"
	"reflect"
	"sort"
	"strings"
	"sync"
	"time"
)

// CreditsService is the portable orchestration layer over CreditStore. It does
// not calculate or persist accounting locally: it delegates admission, quota,
// allowance, idempotency, and ledger mutations to the configured store.
type CreditsService struct {
	store           CreditStore
	catalog         *CatalogService
	instrumentation Instrumentation
	analytics       UsageAnalyticsStore
	usageStore      UsageChargeStore

	policy               CreditPolicyPreset
	overdraftFloor       Amount
	maxConcurrent        *int
	defaultLeaseTTL      time.Duration
	events               CreditEventSink
	lowBalance           []Amount
	lowBalanceHandler    CreditEventHandler
	lowBalanceMaxTracked int
	lazyExpiry           bool

	lowBalanceMu    sync.Mutex
	lowBalanceState map[string]map[string]struct{}
	lowBalanceOrder *list.List
	lowBalanceUsers map[string]*list.Element

	postDeductionMu     sync.RWMutex
	postDeductionHooks  map[uint64]PostDeductionHook
	nextPostDeductionID uint64
}

// NewCreditsService constructs a service around a durable CreditStore.
func NewCreditsService(store CreditStore, options CreditsServiceOptions) (*CreditsService, error) {
	if store == nil {
		return nil, errorf(ErrorCodeConfig, ErrorCategoryInvalidRequest, "credit store is required")
	}
	policy := options.Policy
	if policy == "" {
		policy = CreditPolicyStrictPrepaid
	}
	if policy != CreditPolicyStrictPrepaid && policy != CreditPolicyOverdraft {
		return nil, errorf(ErrorCodeConfig, ErrorCategoryInvalidRequest, "credit policy must be %q or %q", CreditPolicyStrictPrepaid, CreditPolicyOverdraft)
	}
	overdraftFloor := DecimalZero
	if options.OverdraftFloor != nil {
		overdraftFloor = QuantizeMoney(*options.OverdraftFloor)
		if overdraftFloor.GreaterThan(DecimalZero) {
			return nil, errorf(ErrorCodeConfig, ErrorCategoryInvalidRequest, "overdraft floor must be less than or equal to zero")
		}
	}
	if options.MaxConcurrent != nil && *options.MaxConcurrent < 1 {
		return nil, errorf(ErrorCodeConfig, ErrorCategoryInvalidRequest, "max concurrent operations must be positive")
	}
	defaultTTL := options.DefaultLeaseTTL
	if defaultTTL == 0 {
		defaultTTL = defaultLeaseTTL
	}
	if _, err := requirePositiveDuration(defaultTTL, "default lease TTL"); err != nil {
		return nil, err
	}
	if options.LowBalanceConfig != nil && len(options.LowBalance) > 0 {
		return nil, errorf(ErrorCodeConfig, ErrorCategoryInvalidRequest, "configure low balance with either LowBalance or LowBalanceConfig")
	}
	lowBalance := append([]Amount(nil), options.LowBalance...)
	lowBalanceHandler := CreditEventHandler(nil)
	lowBalanceMaxTracked := 100_000
	if options.LowBalanceConfig != nil {
		lowBalance = append([]Amount(nil), options.LowBalanceConfig.Thresholds...)
		lowBalanceHandler = options.LowBalanceConfig.OnTrigger
		if options.LowBalanceConfig.MaxTrackedUsers != 0 {
			lowBalanceMaxTracked = options.LowBalanceConfig.MaxTrackedUsers
		}
	}
	if lowBalanceMaxTracked < 1 {
		return nil, errorf(ErrorCodeConfig, ErrorCategoryInvalidRequest, "low balance max tracked users must be positive")
	}
	for _, threshold := range lowBalance {
		if threshold.IsNegative() {
			return nil, errorf(ErrorCodeConfig, ErrorCategoryInvalidRequest, "low balance thresholds must not be negative")
		}
	}
	sort.Slice(lowBalance, func(left, right int) bool { return lowBalance[left].GreaterThan(lowBalance[right]) })
	postDeductionHooks := make(map[uint64]PostDeductionHook)
	var nextPostDeductionID uint64
	if options.PostDeduction != nil {
		nextPostDeductionID = 1
		postDeductionHooks[nextPostDeductionID] = options.PostDeduction
	}
	catalogCacheTTL := defaultCatalogCacheTTL
	if options.CatalogCacheTTL != nil {
		catalogCacheTTL = *options.CatalogCacheTTL
	}
	catalog, err := NewCatalogServiceWithOptions(store, CatalogServiceOptions{CacheTTL: catalogCacheTTL})
	if err != nil {
		return nil, err
	}
	instrumentation := options.Instrumentation
	if isNilInstrumentation(instrumentation) {
		instrumentation = DefaultInstrumentation()
	}
	analytics := options.Analytics
	if isNilCreditsReadBackend(analytics) {
		analytics = store
	}
	usageStore := options.UsageStore
	if isNilCreditsReadBackend(usageStore) {
		usageStore = store
	}
	return &CreditsService{
		store:                store,
		catalog:              catalog,
		instrumentation:      instrumentation,
		analytics:            analytics,
		usageStore:           usageStore,
		policy:               policy,
		overdraftFloor:       overdraftFloor,
		maxConcurrent:        options.MaxConcurrent,
		defaultLeaseTTL:      defaultTTL,
		events:               options.EventSink,
		lowBalance:           lowBalance,
		lowBalanceHandler:    lowBalanceHandler,
		lowBalanceMaxTracked: lowBalanceMaxTracked,
		lazyExpiry:           options.LazyExpiry,
		lowBalanceState:      make(map[string]map[string]struct{}),
		lowBalanceOrder:      list.New(),
		lowBalanceUsers:      make(map[string]*list.Element),
		postDeductionHooks:   postDeductionHooks,
		nextPostDeductionID:  nextPostDeductionID,
	}, nil
}

// Optional read backends are commonly passed as pointers. Treat a typed nil
// the same as an omitted interface so Go callers get the documented
// PostgreSQL fallback rather than a delayed nil-receiver failure.
func isNilCreditsReadBackend(backend any) bool {
	if backend == nil {
		return true
	}
	value := reflect.ValueOf(backend)
	switch value.Kind() {
	case reflect.Chan, reflect.Func, reflect.Interface, reflect.Map, reflect.Pointer, reflect.Slice:
		return value.IsNil()
	default:
		return false
	}
}

func (s *CreditsService) telemetry() Instrumentation {
	if s == nil || isNilInstrumentation(s.instrumentation) {
		return NoopInstrumentation{}
	}
	return s.instrumentation
}

// Catalog returns the catalog runtime used for revision-aware usage pricing.
// Bursar exposes this same instance as its Catalog capability.
func (s *CreditsService) Catalog() *CatalogService {
	if s == nil {
		return nil
	}
	return s.catalog
}

// Store exposes the durable store for integrations that need an advanced
// capability not wrapped by the service. Accounting should still flow through
// the store's atomic methods rather than application-side state.
func (s *CreditsService) Store() CreditStore {
	if s == nil {
		return nil
	}
	return s.store
}

// AddPostDeductionHook registers a best-effort post-commit hook and returns
// an unsubscribe function. Nil hooks and a nil service return a no-op
// unsubscribe function. Hooks run only for non-idempotent successful raw,
// direct, and lease-settlement deductions; they cannot affect the committed
// accounting result.
func (s *CreditsService) AddPostDeductionHook(hook PostDeductionHook) func() {
	if s == nil || hook == nil {
		return func() {}
	}
	s.postDeductionMu.Lock()
	s.nextPostDeductionID++
	id := s.nextPostDeductionID
	if s.postDeductionHooks == nil {
		s.postDeductionHooks = make(map[uint64]PostDeductionHook)
	}
	s.postDeductionHooks[id] = hook
	s.postDeductionMu.Unlock()

	return func() {
		s.postDeductionMu.Lock()
		delete(s.postDeductionHooks, id)
		s.postDeductionMu.Unlock()
	}
}

// Close closes store-owned resources.
func (s *CreditsService) Close() error {
	if s == nil || s.store == nil {
		return nil
	}
	return s.store.Close()
}

// GetBalance returns the current durable balance.
func (s *CreditsService) GetBalance(ctx context.Context, userID string) (BalanceResult, error) {
	if s == nil || s.store == nil {
		return BalanceResult{}, NewError("credits service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	if err := s.maybeLazyExpire(ctx, userID); err != nil {
		return BalanceResult{}, err
	}
	return s.store.GetBalance(ctx, userID)
}

func (s *CreditsService) maybeLazyExpire(ctx context.Context, userID string) error {
	if s == nil || !s.lazyExpiry {
		return nil
	}
	_, err := s.SweepExpiredCredits(ctx, false, userID, 100)
	return err
}

// GetAvailable returns an advisory snapshot; it is not an admission gate.
func (s *CreditsService) GetAvailable(ctx context.Context, userID string) (AvailableResult, error) {
	if s == nil || s.store == nil {
		return AvailableResult{}, NewError("credits service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	return s.store.GetAvailable(ctx, userID)
}

// AddCredits creates a positive idempotent grant and emits credits.added after
// the store reports a committed result.
func (s *CreditsService) AddCredits(ctx context.Context, userID string, amount Amount, options AddCreditsOptions) (AddCreditsResult, error) {
	return runInstrumentedValue(ctx, s.telemetry(), telemetryOperationCreditsGrant, nil, func(ctx context.Context) (AddCreditsResult, error) {
		if s == nil || s.store == nil {
			return AddCreditsResult{}, NewError("credits service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
		}
		if _, err := requirePositiveAmount(amount, "add credits"); err != nil {
			return AddCreditsResult{}, err
		}
		result, err := s.store.AddCredits(ctx, userID, QuantizeMoney(amount), options)
		if err != nil {
			return AddCreditsResult{}, err
		}
		emitCreditEvent(ctx, s.events, CreditEventAdded, userID, CreditMetadata{
			"entry_id":    result.EntryID,
			"amount":      result.Amount,
			"new_balance": result.NewBalance,
			"type":        options.Type,
			"idempotent":  result.Idempotent,
		})
		s.rearmLowBalance(userID, result.NewBalance)
		return result, nil
	})
}

// DeductCredits creates a raw administrative debit. Usage-based charging
// should use Deduct or Settle so plan allowance and quota policy are applied.
func (s *CreditsService) DeductCredits(ctx context.Context, userID string, amount Amount, options AddCreditsOptions) (AddCreditsResult, error) {
	if s == nil || s.store == nil {
		return AddCreditsResult{}, NewError("credits service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	if _, err := requirePositiveAmount(amount, "deduct credits"); err != nil {
		return AddCreditsResult{}, err
	}
	result, err := s.store.AddCredits(ctx, userID, QuantizeMoney(amount).Neg(), options)
	if err != nil {
		return AddCreditsResult{}, err
	}
	emitCreditEvent(ctx, s.events, CreditEventDeducted, userID, CreditMetadata{
		"entry_id":    result.EntryID,
		"amount":      amount,
		"new_balance": result.NewBalance,
		"entry_type":  options.Type,
		"idempotent":  result.Idempotent,
	})
	if !result.Idempotent {
		s.emitPostDeduction(ctx, userID, PostDeductionSourceRaw, DeductionResult{EntryID: result.EntryID, UserID: userID, Amount: amount, BalanceAfter: creditAmountPointer(result.NewBalance)})
	}
	return result, nil
}

// Deduct charges an exact, already-priced amount through the atomic allowance
// and quota RPC. It returns typed credit-domain errors for business denials.
func (s *CreditsService) Deduct(ctx context.Context, userID string, amount Amount, options DeductWithAllowanceOptions) (DeductionResult, error) {
	return runInstrumentedValue(ctx, s.telemetry(), telemetryOperationCreditsDeduct, nil, func(ctx context.Context) (DeductionResult, error) {
		if s == nil || s.store == nil {
			return DeductionResult{}, NewError("credits service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
		}
		if _, err := requireNonNegativeAmount(amount, "deduct"); err != nil {
			return DeductionResult{}, err
		}
		if err := s.maybeLazyExpire(ctx, userID); err != nil {
			return DeductionResult{}, err
		}
		result, err := s.store.DeductWithAllowance(ctx, userID, QuantizeMoney(amount), options)
		if err != nil {
			return DeductionResult{}, err
		}
		if result.ErrorCode != "" {
			emitCreditEvent(ctx, s.events, CreditEventDeductFailed, userID, CreditMetadata{
				"amount":  amount,
				"error":   result.ErrorCode,
				"feature": options.Feature,
			})
			return result, creditBusinessError("deduct", userID, result.ErrorCode)
		}
		if result.BalanceAfter == nil {
			return result, NewStoreError("deduct succeeded without a committed balance", ErrorOptions{})
		}
		emitCreditEvent(ctx, s.events, CreditEventDeducted, userID, CreditMetadata{
			"entry_id":           result.EntryID,
			"usage_charge_id":    result.UsageChargeID,
			"amount":             result.Amount,
			"allowance_consumed": result.AllowanceConsumed,
			"balance_after":      *result.BalanceAfter,
			"model":              options.Model,
			"idempotent":         result.Idempotent,
		})
		if !result.Idempotent {
			s.emitPostDeduction(ctx, userID, PostDeductionSourceDeduct, result)
		}
		return result, nil
	})
}

// DeductUsage prices metrics with the subject's effective rate card and then
// performs the same atomic allowance, entitlement, quota, receipt, and ledger
// mutation as Deduct. Zero-cost usage still reaches PostgreSQL so free rates
// cannot bypass authorization, quotas, or usage history.
func (s *CreditsService) DeductUsage(ctx context.Context, userID string, metrics UsageMetrics, options PricedUsageOptions) (DeductionResult, error) {
	return runInstrumentedValue(ctx, s.telemetry(), telemetryOperationCreditsDeduct, nil, func(ctx context.Context) (DeductionResult, error) {
		if s == nil || s.catalog == nil {
			return DeductionResult{}, NewError("credits service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
		}
		idempotencyKey, err := requireStableKey(options.IdempotencyKey, "deduct idempotency key")
		if err != nil {
			return DeductionResult{}, err
		}
		breakdown, err := s.catalog.CalculateForUser(ctx, userID, metrics)
		if err != nil {
			return DeductionResult{}, err
		}
		operationOptions := pricedOperationOptions(metrics, breakdown, idempotencyKey, options.Feature, options.Metadata)
		result, err := s.Deduct(ctx, userID, breakdown.Total, DeductWithAllowanceOptions{
			OperationUsageOptions: operationOptions,
			IdempotencyKey:        idempotencyKey,
			Operation:             metrics.Operation,
			Metadata:              pricedMetadata(metrics, breakdown, idempotencyKey, options.Metadata),
		})
		if err != nil {
			if result.ErrorCode == "quota_exceeded" {
				s.emitQuotaEvents(ctx, userID, idempotencyKey)
			}
			return result, err
		}
		if !result.Idempotent {
			s.emitQuotaEvents(ctx, userID, idempotencyKey)
		}
		return result, nil
	})
}

// DeductFlatJob charges one configured named job with the canonical jobs=1
// measure rather than making applications duplicate pricing metadata.
func (s *CreditsService) DeductFlatJob(ctx context.Context, userID, jobName string, options PricedUsageOptions) (DeductionResult, error) {
	return runInstrumentedValue(ctx, s.telemetry(), telemetryOperationCreditsDeduct, nil, func(ctx context.Context) (DeductionResult, error) {
		return s.DeductUsage(ctx, userID, UsageMetrics{
			Operation: jobName,
			Measures:  map[string]Amount{"jobs": MustAmount("1")},
		}, options)
	})
}

func (s *CreditsService) resolvedLeaseOptions(options ReserveOptions) (CreateLeaseOptions, string, error) {
	operationType := strings.TrimSpace(options.OperationType)
	if operationType == "" {
		operationType = "usage"
	}
	idempotencyKey, err := requireStableKey(options.IdempotencyKey, "reserve idempotency key")
	if err != nil {
		return CreateLeaseOptions{}, "", err
	}
	mode := options.BillingMode
	if mode == "" {
		if s.policy == CreditPolicyOverdraft {
			mode = BillingModeOverdraft
		} else {
			mode = BillingModeStrict
		}
	}
	mode, err = requireBillingMode(mode)
	if err != nil {
		return CreateLeaseOptions{}, "", err
	}
	ttl := options.TTL
	if ttl == 0 {
		ttl = s.defaultLeaseTTL
	}
	if _, err = requirePositiveDuration(ttl, "lease TTL"); err != nil {
		return CreateLeaseOptions{}, "", err
	}
	floor := DecimalZero
	var overdraftFloor *Amount
	if mode == BillingModeOverdraft {
		floor = s.overdraftFloor
		overdraftFloor = creditAmountPointer(floor)
	}
	return CreateLeaseOptions{
		OperationUsageOptions: options.OperationUsageOptions,
		IdempotencyKey:        idempotencyKey,
		BillingMode:           mode,
		Floor:                 floor,
		MaxConcurrent:         s.maxConcurrent,
		OverdraftFloor:        overdraftFloor,
		TTL:                   ttl,
		Metadata:              options.Metadata,
	}, operationType, nil
}

// Reserve atomically admits work by acquiring a durable credit lease.
func (s *CreditsService) Reserve(ctx context.Context, userID string, amount Amount, options ReserveOptions) (LeaseResult, error) {
	return runInstrumentedValue(ctx, s.telemetry(), telemetryOperationCreditsReserve, nil, func(ctx context.Context) (LeaseResult, error) {
		if s == nil || s.store == nil {
			return LeaseResult{}, NewError("credits service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
		}
		if _, err := requireNonNegativeAmount(amount, "reserve"); err != nil {
			return LeaseResult{}, err
		}
		storeOptions, operationType, err := s.resolvedLeaseOptions(options)
		if err != nil {
			return LeaseResult{}, err
		}
		result, err := s.store.CreateLease(ctx, userID, QuantizeMoney(amount), operationType, storeOptions)
		if err != nil {
			return LeaseResult{}, err
		}
		if result.ErrorCode != "" {
			emitCreditEvent(ctx, s.events, CreditEventDeductFailed, userID, CreditMetadata{
				"error":          result.ErrorCode,
				"amount":         amount,
				"stage":          "reserve",
				"operation_type": operationType,
			})
			return result, creditBusinessError("reserve", userID, result.ErrorCode)
		}
		if result.LeaseID == "" || result.Amount == nil || result.MinimumBalance == nil || result.ExpiresAt == nil {
			return result, NewStoreError("reserve succeeded without a committed lease", ErrorOptions{})
		}
		emitCreditEvent(ctx, s.events, CreditEventReserved, userID, CreditMetadata{
			"lease_id":       result.LeaseID,
			"amount":         *result.Amount,
			"available":      result.Available,
			"billing_mode":   result.BillingMode,
			"operation_type": operationType,
			"expires_at":     *result.ExpiresAt,
		})
		return result, nil
	})
}

// ReserveUsage prices metrics with the subject's current effective catalog
// and atomically reserves that worst-case cost while persisting the measures
// and dimensions used by admission and quota policy.
func (s *CreditsService) ReserveUsage(ctx context.Context, userID string, metrics UsageMetrics, options ReserveOptions) (LeaseResult, error) {
	return runInstrumentedValue(ctx, s.telemetry(), telemetryOperationCreditsReserve, nil, func(ctx context.Context) (LeaseResult, error) {
		if s == nil || s.catalog == nil {
			return LeaseResult{}, NewError("credits service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
		}
		breakdown, err := s.catalog.CalculateForUser(ctx, userID, metrics)
		if err != nil {
			return LeaseResult{}, err
		}
		if strings.TrimSpace(options.OperationType) == "" {
			options.OperationType = metrics.Operation
		}
		options.OperationUsageOptions = pricedOperationOptions(metrics, breakdown, options.IdempotencyKey, options.Feature, options.Metadata)
		options.Metadata = pricedMetadata(metrics, breakdown, options.IdempotencyKey, options.Metadata)
		result, err := s.Reserve(ctx, userID, breakdown.Total, options)
		if err != nil {
			if result.ErrorCode == "quota_exceeded" {
				s.emitQuotaEvents(ctx, userID, options.IdempotencyKey)
			}
			return result, err
		}
		s.emitQuotaEvents(ctx, userID, options.IdempotencyKey)
		return result, nil
	})
}

// Settle finalizes a lease with its actual exact cost. A successful settlement
// never releases the lease afterward, preserving replay safety after unknown
// commit outcomes.
func (s *CreditsService) Settle(ctx context.Context, userID, leaseID string, amount Amount, options SettleOptions) (DeductionResult, error) {
	return runInstrumentedValue(ctx, s.telemetry(), telemetryOperationCreditsSettle, nil, func(ctx context.Context) (DeductionResult, error) {
		if s == nil || s.store == nil {
			return DeductionResult{}, NewError("credits service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
		}
		if _, err := requireNonNegativeAmount(amount, "settle"); err != nil {
			return DeductionResult{}, err
		}
		if strings.TrimSpace(options.IdempotencyKey) == "" {
			options.IdempotencyKey = "lease:" + strings.TrimSpace(leaseID) + ":settle"
		}
		if _, err := requireStableKey(options.IdempotencyKey, "settle idempotency key"); err != nil {
			return DeductionResult{}, err
		}
		result, err := s.store.SettleLease(ctx, userID, leaseID, QuantizeMoney(amount), SettleLeaseOptions(options))
		if err != nil {
			return DeductionResult{}, err
		}
		if result.ErrorCode != "" {
			emitCreditEvent(ctx, s.events, CreditEventDeductFailed, userID, CreditMetadata{
				"error":    result.ErrorCode,
				"amount":   amount,
				"stage":    "settle",
				"lease_id": leaseID,
			})
			if isLeaseExpiredCode(result.ErrorCode) {
				emitCreditEvent(ctx, s.events, CreditEventLeaseExpired, userID, CreditMetadata{"lease_id": leaseID})
			}
			return result, creditBusinessError("settle", userID, result.ErrorCode)
		}
		if result.BalanceAfter == nil {
			return result, NewStoreError("settle succeeded without a committed balance", ErrorOptions{})
		}
		emitCreditEvent(ctx, s.events, CreditEventDeducted, userID, CreditMetadata{
			"entry_id":           result.EntryID,
			"usage_charge_id":    result.UsageChargeID,
			"amount":             result.Amount,
			"allowance_consumed": result.AllowanceConsumed,
			"balance_after":      *result.BalanceAfter,
			"model":              options.Model,
			"lease_id":           leaseID,
			"idempotent":         result.Idempotent,
		})
		if !result.Idempotent {
			s.emitPostDeduction(ctx, userID, PostDeductionSourceSettle, result)
		}
		return result, nil
	})
}

// SettleUsage prices actual metrics using the immutable catalog and rate card
// snapshot captured by the lease, then commits the full (unclamped) cost.
func (s *CreditsService) SettleUsage(ctx context.Context, userID, leaseID string, metrics UsageMetrics, options SettleOptions) (DeductionResult, error) {
	return runInstrumentedValue(ctx, s.telemetry(), telemetryOperationCreditsSettle, nil, func(ctx context.Context) (DeductionResult, error) {
		if s == nil || s.catalog == nil {
			return DeductionResult{}, NewError("credits service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
		}
		breakdown, err := s.catalog.CalculateForLease(ctx, userID, leaseID, metrics)
		if err != nil {
			return DeductionResult{}, err
		}
		if strings.TrimSpace(options.IdempotencyKey) == "" {
			options.IdempotencyKey = "lease:" + strings.TrimSpace(leaseID) + ":settle"
		}
		options.OperationUsageOptions = pricedOperationOptions(metrics, breakdown, options.IdempotencyKey, options.Feature, options.Metadata)
		options.Metadata = pricedMetadata(metrics, breakdown, options.IdempotencyKey, options.Metadata)
		result, err := s.Settle(ctx, userID, leaseID, breakdown.Total, options)
		if err != nil {
			if result.ErrorCode == "quota_exceeded" {
				s.emitQuotaEvents(ctx, userID, options.IdempotencyKey)
			}
			return result, err
		}
		if !result.Idempotent {
			s.emitQuotaEvents(ctx, userID, options.IdempotencyKey)
		}
		return result, nil
	})
}

// Release releases a failed/aborted operation's lease without a charge.
func (s *CreditsService) Release(ctx context.Context, userID, leaseID string) (ReleaseResult, error) {
	return runInstrumentedValue(ctx, s.telemetry(), telemetryOperationCreditsRelease, nil, func(ctx context.Context) (ReleaseResult, error) {
		if s == nil || s.store == nil {
			return ReleaseResult{}, NewError("credits service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
		}
		result, err := s.store.ReleaseLease(ctx, userID, leaseID)
		if err != nil {
			return ReleaseResult{}, err
		}
		if result.Released {
			emitCreditEvent(ctx, s.events, CreditEventReservationReleased, userID, CreditMetadata{"lease_id": leaseID, "reason": result.Reason})
		}
		return result, nil
	})
}

// Renew extends an active operation lease with the configured default TTL when
// ttl is zero.
func (s *CreditsService) Renew(ctx context.Context, userID, leaseID string, ttl time.Duration) (LeaseResult, error) {
	if s == nil || s.store == nil {
		return LeaseResult{}, NewError("credits service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	if ttl == 0 {
		ttl = s.defaultLeaseTTL
	}
	result, err := s.store.RenewLease(ctx, userID, leaseID, ttl)
	if err != nil {
		return LeaseResult{}, err
	}
	if result.ErrorCode != "" {
		if isLeaseExpiredCode(result.ErrorCode) {
			emitCreditEvent(ctx, s.events, CreditEventLeaseExpired, userID, CreditMetadata{"lease_id": leaseID})
		}
		return result, creditBusinessError("renew", userID, result.ErrorCode)
	}
	return result, nil
}

// CanAfford is an advisory check for UI. It must never replace Reserve as the
// concurrency-safe admission gate.
func (s *CreditsService) CanAfford(ctx context.Context, userID string, worstCase Amount, options CanAffordOptions) (CanAffordResult, error) {
	return runInstrumentedValue(ctx, s.telemetry(), telemetryOperationCreditsCanAfford, nil, func(ctx context.Context) (CanAffordResult, error) {
		if s == nil || s.store == nil {
			return CanAffordResult{}, NewError("credits service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
		}
		if _, err := requireNonNegativeAmount(worstCase, "can afford"); err != nil {
			return CanAffordResult{}, err
		}
		available, err := s.store.GetAvailable(ctx, userID)
		if err != nil {
			return CanAffordResult{}, err
		}
		mode := options.BillingMode
		if mode == "" {
			if s.policy == CreditPolicyOverdraft {
				mode = BillingModeOverdraft
			} else {
				mode = BillingModeStrict
			}
		}
		mode, err = requireBillingMode(mode)
		if err != nil {
			return CanAffordResult{}, err
		}
		floor := DecimalZero
		if mode == BillingModeOverdraft {
			floor = s.overdraftFloor
		}
		allowance, err := s.store.CheckAllowance(ctx, userID)
		if err != nil {
			return CanAffordResult{}, err
		}
		spendable := available.Available.Sub(floor)
		if allowance != nil {
			spendable = spendable.Add(allowance.AllowanceRemaining)
		}
		result := CanAffordResult{Affordable: spendable.GreaterThanOrEqual(worstCase), Spendable: spendable, WorstCase: QuantizeMoney(worstCase)}
		if options.Feature != "" {
			feature, err := s.store.CheckFeature(ctx, userID, options.Feature)
			if err != nil {
				return CanAffordResult{}, err
			}
			if !feature.HasFeature {
				result.Affordable = false
				result.Reason = "feature_not_entitled"
				return result, nil
			}
		}
		if !result.Affordable {
			result.Reason = "insufficient_credits"
		}
		return result, nil
	})
}

// CanAffordUsage is the metric-priced advisory counterpart to ReserveUsage.
// It is useful for UI hints but remains non-atomic and must not replace a
// durable reservation for admission.
func (s *CreditsService) CanAffordUsage(ctx context.Context, userID string, metrics UsageMetrics, options CanAffordOptions) (CanAffordResult, error) {
	return runInstrumentedValue(ctx, s.telemetry(), telemetryOperationCreditsCanAfford, nil, func(ctx context.Context) (CanAffordResult, error) {
		if s == nil || s.catalog == nil {
			return CanAffordResult{}, NewError("credits service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
		}
		breakdown, err := s.catalog.CalculateForUser(ctx, userID, metrics)
		if err != nil {
			return CanAffordResult{}, err
		}
		if strings.TrimSpace(options.OperationType) == "" {
			options.OperationType = metrics.Operation
		}
		return s.CanAfford(ctx, userID, breakdown.Total, options)
	})
}

// GrantSubscriptionCycle grants one provider cycle and optionally updates the
// subject plan. The grant itself is idempotent; a replay only repairs a plan
// assignment when it is missing/different and never double-grants credits.
func (s *CreditsService) GrantSubscriptionCycle(ctx context.Context, userID string, amount Amount, options GrantSubscriptionCycleOptions) (AddCreditsResult, error) {
	return runInstrumentedValue(ctx, s.telemetry(), telemetryOperationCreditsGrantSubscriptionCycle, nil, func(ctx context.Context) (AddCreditsResult, error) {
		if s == nil || s.store == nil {
			return AddCreditsResult{}, NewError("credits service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
		}
		if _, err := requirePositiveAmount(amount, "subscription cycle grant"); err != nil {
			return AddCreditsResult{}, err
		}
		if options.ExpiresAt != nil && options.TTLDays != 0 {
			return AddCreditsResult{}, errorf(ErrorCodeConfig, ErrorCategoryInvalidRequest, "subscription cycle expiry and TTL days are mutually exclusive")
		}
		if options.TTLDays < 0 {
			return AddCreditsResult{}, errorf(ErrorCodeConfig, ErrorCategoryInvalidRequest, "subscription cycle TTL days must be positive")
		}
		expiresAt := options.ExpiresAt
		if options.TTLDays > 0 {
			value := time.Now().UTC().Add(time.Duration(options.TTLDays) * 24 * time.Hour)
			expiresAt = &value
		}
		bucket := options.Bucket
		if bucket == "" {
			bucket = "subscription"
		}
		result, err := s.store.AddCredits(ctx, userID, QuantizeMoney(amount), AddCreditsOptions{
			Type:           "purchase",
			Bucket:         bucket,
			ExpiresAt:      expiresAt,
			Metadata:       options.Metadata,
			IdempotencyKey: options.IdempotencyKey,
		})
		if err != nil {
			return AddCreditsResult{}, err
		}
		if options.PlanKey != "" {
			assignPlan := !result.Idempotent
			if result.Idempotent {
				plan, planErr := s.store.GetUserPlan(ctx, userID)
				if planErr != nil {
					return AddCreditsResult{}, planErr
				}
				assignPlan = plan.PlanKey != options.PlanKey
			}
			if assignPlan {
				if _, err := s.SetUserPlan(ctx, userID, options.PlanKey, SetUserPlanOptions{}); err != nil {
					return AddCreditsResult{}, err
				}
			}
		}
		emitCreditEvent(ctx, s.events, CreditEventCycleRenewed, userID, CreditMetadata{
			"entry_id":        result.EntryID,
			"amount":          result.Amount,
			"new_balance":     result.NewBalance,
			"bucket":          bucket,
			"plan_key":        options.PlanKey,
			"idempotency_key": options.IdempotencyKey,
			"idempotent":      result.Idempotent,
		})
		return result, nil
	})
}

// SetUserPlan delegates a plan assignment and emits the committed lifecycle
// event. The post-commit event never replaces the store result.
func (s *CreditsService) SetUserPlan(ctx context.Context, userID, planKey string, options SetUserPlanOptions) (SetUserPlanResult, error) {
	if s == nil || s.store == nil {
		return SetUserPlanResult{}, NewError("credits service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	result, err := s.store.SetUserPlan(ctx, userID, planKey, options)
	if err != nil {
		return SetUserPlanResult{}, err
	}
	emitCreditEvent(ctx, s.events, CreditEventPlanChanged, userID, CreditMetadata{
		"plan_key":         result.PlanKey,
		"plan_assigned_at": result.PlanAssignedAt,
		"assignment_state": result.AssignmentState,
	})
	return result, nil
}

// UnsetUserPlan clears a subject plan assignment.
func (s *CreditsService) UnsetUserPlan(ctx context.Context, userID string) (UnsetUserPlanResult, error) {
	if s == nil || s.store == nil {
		return UnsetUserPlanResult{}, NewError("credits service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	return s.store.UnsetUserPlan(ctx, userID)
}

// GetUserPlan returns the database projection of the effective plan.
func (s *CreditsService) GetUserPlan(ctx context.Context, userID string) (GetUserPlanResult, error) {
	if s == nil || s.store == nil {
		return GetUserPlanResult{}, NewError("credits service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	return s.store.GetUserPlan(ctx, userID)
}

// CheckFeature returns the durable entitlement result.
func (s *CreditsService) CheckFeature(ctx context.Context, userID, feature string) (CheckFeatureResult, error) {
	if s == nil || s.store == nil {
		return CheckFeatureResult{}, NewError("credits service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	return s.store.CheckFeature(ctx, userID, feature)
}

// GetBucketBalances returns the tenant-scoped bucket projection.
func (s *CreditsService) GetBucketBalances(ctx context.Context, userID string) (BucketBalancesResult, error) {
	if s == nil || s.store == nil {
		return BucketBalancesResult{}, NewError("credits service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	return s.store.GetBucketBalances(ctx, userID)
}

// CheckAllowance returns the current database-owned allowance window.
func (s *CreditsService) CheckAllowance(ctx context.Context, userID string) (*AllowanceResult, error) {
	if s == nil || s.store == nil {
		return nil, NewError("credits service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	return s.store.CheckAllowance(ctx, userID)
}

// RefundCredits posts a durable refund and emits only after a successful
// committed outcome. Rejected refunds receive a typed credit-domain error.
func (s *CreditsService) RefundCredits(ctx context.Context, entryID string, amount *Amount, reason string, metadata CreditMetadata, idempotencyKey string) (RefundResult, error) {
	return runInstrumentedValue(ctx, s.telemetry(), telemetryOperationCreditsRefund, nil, func(ctx context.Context) (RefundResult, error) {
		if s == nil || s.store == nil {
			return RefundResult{}, NewError("credits service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
		}
		result, err := s.store.RefundCredits(ctx, entryID, amount, reason, metadata, idempotencyKey)
		if err != nil {
			return RefundResult{}, err
		}
		if result.ErrorCode != "" {
			if result.UserID != "" {
				emitCreditEvent(ctx, s.events, CreditEventRefundFailed, result.UserID, CreditMetadata{"entry_id": entryID, "error": result.ErrorCode, "reason": reason})
			}
			return result, creditBusinessError("refund", result.UserID, result.ErrorCode)
		}
		if result.UserID == "" || result.RefundEntryID == "" || result.Amount == nil || result.NewBalance == nil {
			return result, NewStoreError("refund succeeded without committed fields", ErrorOptions{})
		}
		emitCreditEvent(ctx, s.events, CreditEventRefunded, result.UserID, CreditMetadata{
			"entry_id":        entryID,
			"refund_entry_id": result.RefundEntryID,
			"amount":          *result.Amount,
			"new_balance":     *result.NewBalance,
			"reason":          reason,
		})
		return result, nil
	})
}

// RevokeCreditsByEntryType removes remaining lots for a subscription or grant
// operation and emits credits.revoked after commit.
func (s *CreditsService) RevokeCreditsByEntryType(ctx context.Context, userID, entryType string) (RevokeCreditsResult, error) {
	if s == nil || s.store == nil {
		return RevokeCreditsResult{}, NewError("credits service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	result, err := s.store.RevokeCreditsByEntryType(ctx, userID, entryType)
	if err != nil {
		return RevokeCreditsResult{}, err
	}
	emitCreditEvent(ctx, s.events, CreditEventRevoked, userID, CreditMetadata{"entry_type": entryType, "revoked": result.Revoked, "balance_after": result.BalanceAfter})
	return result, nil
}

// SweepExpiredCredits executes a bounded expiry pass and emits its committed
// aggregate outcome when it expires at least one lot.
func (s *CreditsService) SweepExpiredCredits(ctx context.Context, dryRun bool, userID string, limit int) (SweepResult, error) {
	if s == nil || s.store == nil {
		return SweepResult{}, NewError("credits service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	result, err := s.store.SweepExpiredCredits(ctx, dryRun, userID, limit)
	if err != nil {
		return SweepResult{}, err
	}
	if !dryRun && result.ExpiredCount > 0 {
		emitCreditEvent(ctx, s.events, CreditEventExpired, firstNonEmpty(userID, "system"), CreditMetadata{"expired_count": result.ExpiredCount, "expired_amount": result.ExpiredAmount, "expired_by_bucket": result.ExpiredByBucket})
	}
	return result, nil
}

// GetActiveCatalog returns the active tenant catalog revision.
func (s *CreditsService) GetActiveCatalog(ctx context.Context) (*CatalogRevision, error) {
	if s == nil || s.store == nil {
		return nil, NewError("credits service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	return s.store.GetActiveCatalog(ctx)
}

// PublishAndActivateCatalog delegates revision validation/persistence to the
// store and returns the committed revision ID.
func (s *CreditsService) PublishAndActivateCatalog(ctx context.Context, config map[string]any, label string, rollout CatalogRollout) (string, error) {
	if s == nil || s.store == nil {
		return "", NewError("credits service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	return s.store.PublishAndActivateCatalog(ctx, config, label, rollout)
}

// PublishCatalogDraft writes an inactive catalog revision.
func (s *CreditsService) PublishCatalogDraft(ctx context.Context, config map[string]any, label string) (string, error) {
	if s == nil || s.store == nil {
		return "", NewError("credits service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	return s.store.PublishCatalogDraft(ctx, config, label)
}

// GetCatalogHistory lists historical catalog revisions.
func (s *CreditsService) GetCatalogHistory(ctx context.Context) ([]CatalogRevisionSummary, error) {
	if s == nil || s.store == nil {
		return nil, NewError("credits service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	return s.store.GetCatalogHistory(ctx)
}

// GetCatalogRevision reads a historical catalog revision.
func (s *CreditsService) GetCatalogRevision(ctx context.Context, version int) (*CatalogRevision, error) {
	if s == nil || s.store == nil {
		return nil, NewError("credits service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	return s.store.GetCatalogRevision(ctx, version)
}

// ActivateCatalogRevision moves the live catalog to a historical version.
func (s *CreditsService) ActivateCatalogRevision(ctx context.Context, version int, rollout CatalogRollout) (string, error) {
	if s == nil || s.store == nil {
		return "", NewError("credits service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	return s.store.ActivateCatalogRevision(ctx, version, rollout)
}

// SetPlanRevisionPin controls whether an assignment follows later catalog revisions.
func (s *CreditsService) SetPlanRevisionPin(ctx context.Context, userID string, pinned bool) (bool, error) {
	if s == nil || s.store == nil {
		return false, NewError("credits service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	return s.store.SetPlanRevisionPin(ctx, userID, pinned)
}

// ApplyDuePlanChanges advances a bounded scheduled-plan batch.
func (s *CreditsService) ApplyDuePlanChanges(ctx context.Context, limit int) (int, error) {
	if s == nil || s.store == nil {
		return 0, NewError("credits service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	return s.store.ApplyDuePlanChanges(ctx, limit)
}

// StartPlanMigration creates a resumable plan migration.
func (s *CreditsService) StartPlanMigration(ctx context.Context, fromPlanID, toPlanID string) (PlanMigrationStartResult, error) {
	if s == nil || s.store == nil {
		return PlanMigrationStartResult{}, NewError("credits service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	return s.store.StartPlanMigration(ctx, fromPlanID, toPlanID)
}

// MigratePlanBatch advances one bounded migration batch.
func (s *CreditsService) MigratePlanBatch(ctx context.Context, migrationID string, batchSize int) (PlanMigrationBatchResult, error) {
	if s == nil || s.store == nil {
		return PlanMigrationBatchResult{}, NewError("credits service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	return s.store.MigratePlanBatch(ctx, migrationID, batchSize)
}

// GetQuotaState retrieves current quota windows.
func (s *CreditsService) GetQuotaState(ctx context.Context, userID, quotaKey string) ([]QuotaState, error) {
	if s == nil || s.store == nil {
		return nil, NewError("credits service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	return s.store.GetQuotaState(ctx, userID, quotaKey)
}

// ListQuotaEvents reads persisted quota threshold/block events.
func (s *CreditsService) ListQuotaEvents(ctx context.Context, userID string, options ListQuotaEventsOptions) ([]QuotaEvent, error) {
	if s == nil || s.store == nil {
		return nil, NewError("credits service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	return s.store.ListQuotaEvents(ctx, userID, options)
}

// ExecuteGrantProgram runs a configured server-side grant event.
func (s *CreditsService) ExecuteGrantProgram(ctx context.Context, request ExecuteGrantProgramRequest) ([]GrantProgramAwardResult, error) {
	return runInstrumentedValue(ctx, s.telemetry(), telemetryOperationCreditsGrantProgram, nil, func(ctx context.Context) ([]GrantProgramAwardResult, error) {
		if s == nil || s.store == nil {
			return nil, NewError("credits service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
		}
		return s.store.ExecuteGrantProgram(ctx, request)
	})
}

// RecordUsage records a priced receipt without another account debit.
func (s *CreditsService) RecordUsage(ctx context.Context, userID, operation string, requested Amount, options RecordUsageOptions) (UsageRecordResult, error) {
	return runInstrumentedValue(ctx, s.telemetry(), telemetryOperationCreditsRecordUsage, nil, func(ctx context.Context) (UsageRecordResult, error) {
		if s == nil || s.store == nil {
			return UsageRecordResult{}, NewError("credits service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
		}
		result, err := s.store.RecordUsage(ctx, userID, operation, requested, options)
		if err != nil {
			return UsageRecordResult{}, err
		}
		if result.ErrorCode != "" {
			return result, creditBusinessError("record usage", userID, result.ErrorCode)
		}
		return result, nil
	})
}

// RecordUsageMetrics prices and persists a usage receipt without debiting the
// subject again. It is intended for externally billed usage while retaining
// the same catalog snapshot and audit metadata as a normal Bursar charge.
func (s *CreditsService) RecordUsageMetrics(ctx context.Context, userID string, metrics UsageMetrics, options PricedUsageRecordOptions) (UsageRecordResult, error) {
	return runInstrumentedValue(ctx, s.telemetry(), telemetryOperationCreditsRecordUsage, nil, func(ctx context.Context) (UsageRecordResult, error) {
		if s == nil || s.catalog == nil {
			return UsageRecordResult{}, NewError("credits service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
		}
		idempotencyKey, err := requireStableKey(options.IdempotencyKey, "record usage idempotency key")
		if err != nil {
			return UsageRecordResult{}, err
		}
		breakdown, err := s.catalog.CalculateForUser(ctx, userID, metrics)
		if err != nil {
			return UsageRecordResult{}, err
		}
		operationOptions := pricedOperationOptions(metrics, breakdown, idempotencyKey, "", options.Metadata)
		return s.RecordUsage(ctx, userID, metrics.Operation, breakdown.Total, RecordUsageOptions{
			OperationUsageOptions: operationOptions,
			IdempotencyKey:        idempotencyKey,
			Metadata:              pricedMetadata(metrics, breakdown, idempotencyKey, options.Metadata),
		})
	})
}

// SpendByUser returns tenant-scoped usage aggregates.
func (s *CreditsService) SpendByUser(ctx context.Context, start, end time.Time) ([]SpendByUserRow, error) {
	if s == nil || s.analytics == nil {
		return nil, NewError("credits service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	return s.analytics.SpendByUser(ctx, start, end)
}

// SpendByModel returns tenant-scoped model aggregates.
func (s *CreditsService) SpendByModel(ctx context.Context, start, end time.Time) ([]SpendByModelRow, error) {
	if s == nil || s.analytics == nil {
		return nil, NewError("credits service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	return s.analytics.SpendByModel(ctx, start, end)
}

// TopUsers returns the highest-spend users in a tenant range.
func (s *CreditsService) TopUsers(ctx context.Context, limit int, start, end time.Time) ([]TopUserRow, error) {
	if s == nil || s.analytics == nil {
		return nil, NewError("credits service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	return s.analytics.TopUsers(ctx, limit, start, end)
}

// DailySpend returns daily tenant-spend aggregates.
func (s *CreditsService) DailySpend(ctx context.Context, start, end time.Time) ([]DailySpendRow, error) {
	if s == nil || s.analytics == nil {
		return nil, NewError("credits service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	return s.analytics.DailySpend(ctx, start, end)
}

// AggregateStats returns aggregate tenant usage statistics.
func (s *CreditsService) AggregateStats(ctx context.Context, start, end time.Time) (AggregateStats, error) {
	if s == nil || s.analytics == nil {
		return AggregateStats{}, NewError("credits service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	return s.analytics.AggregateStats(ctx, start, end)
}

// ListLedgerEntries returns a stable page of subject ledger entries.
func (s *CreditsService) ListLedgerEntries(ctx context.Context, userID string, options ListLedgerEntriesOptions) (LedgerPage, error) {
	if s == nil || s.store == nil {
		return LedgerPage{}, NewError("credits service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	return s.store.ListLedgerEntries(ctx, userID, options)
}

// ListUsageEntries returns usage-only ledger rows.
func (s *CreditsService) ListUsageEntries(ctx context.Context, userID string, options ListLedgerEntriesOptions) (LedgerPage, error) {
	if s == nil || s.store == nil {
		return LedgerPage{}, NewError("credits service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	return s.store.ListUsageEntries(ctx, userID, options)
}

// ListUsageCharges returns canonical usage receipts.
func (s *CreditsService) ListUsageCharges(ctx context.Context, userID string, options ListUsageChargesOptions) (UsageChargePage, error) {
	if s == nil || s.usageStore == nil {
		return UsageChargePage{}, NewError("credits service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	return s.usageStore.ListUsageCharges(ctx, userID, options)
}

// GetLedgerEntry returns one subject-owned ledger row.
func (s *CreditsService) GetLedgerEntry(ctx context.Context, userID, entryID string) (*LedgerEntry, error) {
	if s == nil || s.store == nil {
		return nil, NewError("credits service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	return s.store.GetLedgerEntry(ctx, userID, entryID)
}

// CreateTeam creates a durable shared credit balance.
func (s *CreditsService) CreateTeam(ctx context.Context, ownerUserID, name string, options CreateTeamOptions) (CreateTeamResult, error) {
	if s == nil || s.store == nil {
		return CreateTeamResult{}, NewError("credits service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	return s.store.CreateTeam(ctx, ownerUserID, name, options)
}

// GetTeamBalance reads a shared balance projection.
func (s *CreditsService) GetTeamBalance(ctx context.Context, teamID string) (*TeamBalanceResult, error) {
	if s == nil || s.store == nil {
		return nil, NewError("credits service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	return s.store.GetTeamBalance(ctx, teamID)
}

// AddTeamMember configures a shared-balance membership.
func (s *CreditsService) AddTeamMember(ctx context.Context, teamID, userID string, options AddTeamMemberOptions) (AddTeamMemberResult, error) {
	if s == nil || s.store == nil {
		return AddTeamMemberResult{}, NewError("credits service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	return s.store.AddTeamMember(ctx, teamID, userID, options)
}

// GetTeamMembers lists shared-balance members.
func (s *CreditsService) GetTeamMembers(ctx context.Context, teamID string) ([]TeamMember, error) {
	if s == nil || s.store == nil {
		return nil, NewError("credits service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	return s.store.GetTeamMembers(ctx, teamID)
}

// RemoveTeamMember removes one shared-balance member.
func (s *CreditsService) RemoveTeamMember(ctx context.Context, teamID, userID string) (bool, error) {
	if s == nil || s.store == nil {
		return false, NewError("credits service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	return s.store.RemoveTeamMember(ctx, teamID, userID)
}

// DeductTeam atomically charges a shared balance on behalf of a member.
func (s *CreditsService) DeductTeam(ctx context.Context, teamID, userID string, amount Amount, options TeamDeductionOptions) (TeamDeductionResult, error) {
	return runInstrumentedValue(ctx, s.telemetry(), telemetryOperationCreditsDeductTeam, nil, func(ctx context.Context) (TeamDeductionResult, error) {
		if s == nil || s.store == nil {
			return TeamDeductionResult{}, NewError("credits service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
		}
		if err := s.maybeLazyExpire(ctx, userID); err != nil {
			return TeamDeductionResult{}, err
		}
		result, err := s.store.DeductTeam(ctx, teamID, userID, amount, options)
		if err != nil {
			return TeamDeductionResult{}, err
		}
		if result.ErrorCode != "" {
			emitCreditEvent(ctx, s.events, CreditEventDeductFailed, userID, CreditMetadata{"error": result.ErrorCode, "amount": amount, "team_id": teamID, "deduct_type": "team"})
			return result, creditBusinessError("team deduct", userID, result.ErrorCode)
		}
		if result.TeamBalanceAfter == nil {
			return result, NewStoreError("team deduct succeeded without a committed balance", ErrorOptions{})
		}
		emitCreditEvent(ctx, s.events, CreditEventDeducted, userID, CreditMetadata{"entry_id": result.EntryID, "amount": result.Amount, "team_balance_after": *result.TeamBalanceAfter, "team_id": teamID, "deduct_type": "team"})
		return result, nil
	})
}

// DeductTeamUsage prices metrics using the member's effective plan and then
// atomically charges the shared team pool. A zero-cost operation is a genuine
// no-op for the team ledger but still returns the current durable balance.
func (s *CreditsService) DeductTeamUsage(ctx context.Context, teamID, userID string, metrics UsageMetrics, options PricedTeamDeductionOptions) (TeamDeductionResult, error) {
	return runInstrumentedValue(ctx, s.telemetry(), telemetryOperationCreditsDeductTeam, nil, func(ctx context.Context) (TeamDeductionResult, error) {
		if s == nil || s.catalog == nil {
			return TeamDeductionResult{}, NewError("credits service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
		}
		idempotencyKey, err := requireStableKey(options.IdempotencyKey, "team deduction idempotency key")
		if err != nil {
			return TeamDeductionResult{}, err
		}
		breakdown, err := s.catalog.CalculateForUser(ctx, userID, metrics)
		if err != nil {
			return TeamDeductionResult{}, err
		}
		if breakdown.Total.IsZero() {
			if err := s.maybeLazyExpire(ctx, userID); err != nil {
				return TeamDeductionResult{}, err
			}
			team, err := s.store.GetTeamBalance(ctx, teamID)
			if err != nil {
				return TeamDeductionResult{}, err
			}
			if team == nil {
				return TeamDeductionResult{}, NewError("team was not found", ErrorOptions{Code: ErrorCodeCommerceResourceNotFound, Category: ErrorCategoryNotFound})
			}
			balance := team.Balance
			return TeamDeductionResult{TeamID: teamID, UserID: userID, Amount: DecimalZero, TeamBalanceAfter: &balance}, nil
		}
		return s.DeductTeam(ctx, teamID, userID, breakdown.Total, TeamDeductionOptions{
			IdempotencyKey: idempotencyKey,
			Operation:      metrics.Operation,
			Metadata:       pricedMetadata(metrics, breakdown, idempotencyKey, options.Metadata),
		})
	})
}

// BeginBilledOperation reserves a replay-safe lease for a complete application
// operation. Persist LeaseID and OperationKey to resume after a process crash.
func (s *CreditsService) BeginBilledOperation(ctx context.Context, userID string, options BeginBilledOperationOptions) (*BilledOperation, error) {
	if s == nil || s.store == nil {
		return nil, NewError("credits service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	operationKey, err := requireStableKey(options.OperationKey, "operation key")
	if err != nil {
		return nil, err
	}
	reserveKey, err := scopedOperationKey(operationKey, "reserve")
	if err != nil {
		return nil, err
	}
	lease, err := s.Reserve(ctx, userID, options.Estimate, ReserveOptions{
		OperationUsageOptions: OperationUsageOptions{Feature: options.Feature},
		IdempotencyKey:        reserveKey,
		OperationType:         options.OperationType,
		BillingMode:           options.BillingMode,
		TTL:                   options.TTL,
		Metadata:              options.Metadata,
	})
	if err != nil {
		return nil, err
	}
	if lease.LeaseID == "" {
		return nil, NewStoreError("reserve succeeded without a lease ID", ErrorOptions{})
	}
	return &BilledOperation{
		service:      s,
		userID:       userID,
		leaseID:      lease.LeaseID,
		operationKey: operationKey,
		feature:      options.Feature,
		metadata:     options.Metadata.Clone(),
	}, nil
}

// BeginBilledUsageOperation is the metric-priced counterpart to
// BeginBilledOperation. The estimate is priced against the subject's current
// catalog, while final usage is priced against the lease's captured revision.
func (s *CreditsService) BeginBilledUsageOperation(ctx context.Context, userID string, options BeginBilledUsageOperationOptions) (*BilledOperation, error) {
	if s == nil || s.store == nil {
		return nil, NewError("credits service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	operationKey, err := requireStableKey(options.OperationKey, "operation key")
	if err != nil {
		return nil, err
	}
	reserveKey, err := scopedOperationKey(operationKey, "reserve")
	if err != nil {
		return nil, err
	}
	lease, err := s.ReserveUsage(ctx, userID, options.Estimate, ReserveOptions{
		OperationUsageOptions: OperationUsageOptions{Feature: options.Feature},
		IdempotencyKey:        reserveKey,
		OperationType:         options.OperationType,
		BillingMode:           options.BillingMode,
		TTL:                   options.TTL,
		Metadata:              options.Metadata,
	})
	if err != nil {
		return nil, err
	}
	if lease.LeaseID == "" {
		return nil, NewStoreError("reserve succeeded without a lease ID", ErrorOptions{})
	}
	return &BilledOperation{
		service: s, userID: userID, leaseID: lease.LeaseID,
		operationKey: operationKey, feature: options.Feature, metadata: options.Metadata.Clone(),
	}, nil
}

// ResumeBilledOperation recreates a durable operation handle from application
// job/callback state without acquiring another lease.
func (s *CreditsService) ResumeBilledOperation(userID, leaseID, operationKey string, feature string, metadata CreditMetadata) (*BilledOperation, error) {
	if s == nil || s.store == nil {
		return nil, NewError("credits service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	userID, err := requireText(userID, "user ID")
	if err != nil {
		return nil, err
	}
	leaseID, err = requireText(leaseID, "lease ID")
	if err != nil {
		return nil, err
	}
	operationKey, err = requireStableKey(operationKey, "operation key")
	if err != nil {
		return nil, err
	}
	return &BilledOperation{service: s, userID: userID, leaseID: leaseID, operationKey: operationKey, feature: feature, metadata: metadata.Clone()}, nil
}

// BilledOperation is a durable handle to a previously admitted lease.
type BilledOperation struct {
	service      *CreditsService
	userID       string
	leaseID      string
	operationKey string
	feature      string
	metadata     CreditMetadata
}

// UserID returns the admitted operation's subject identifier.
func (o *BilledOperation) UserID() string {
	if o == nil {
		return ""
	}
	return o.userID
}

// LeaseID returns the durable lease identifier.
func (o *BilledOperation) LeaseID() string {
	if o == nil {
		return ""
	}
	return o.leaseID
}

// Renew extends the operation lease using the service default when ttl is zero.
func (o *BilledOperation) Renew(ctx context.Context, ttl time.Duration) (LeaseResult, error) {
	if o == nil || o.service == nil {
		return LeaseResult{}, NewError("billed operation is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	return o.service.Renew(ctx, o.userID, o.leaseID, ttl)
}

// Release releases the operation's lease after work fails or is abandoned.
func (o *BilledOperation) Release(ctx context.Context) (ReleaseResult, error) {
	if o == nil || o.service == nil {
		return ReleaseResult{}, NewError("billed operation is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	return o.service.Release(ctx, o.userID, o.leaseID)
}

// Settle finalizes the operation using an operation-key-scoped replay key.
func (o *BilledOperation) Settle(ctx context.Context, actual Amount) (DeductionResult, error) {
	if o == nil || o.service == nil {
		return DeductionResult{}, NewError("billed operation is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	settleKey, err := scopedOperationKey(o.operationKey, "settle")
	if err != nil {
		return DeductionResult{}, err
	}
	return o.service.Settle(ctx, o.userID, o.leaseID, actual, SettleOptions{
		OperationUsageOptions: OperationUsageOptions{Feature: o.feature},
		IdempotencyKey:        settleKey,
		Metadata:              o.metadata.Clone(),
	})
}

// SettleUsage prices final metrics against the immutable catalog and plan
// context captured by this operation's lease.
func (o *BilledOperation) SettleUsage(ctx context.Context, actual UsageMetrics) (DeductionResult, error) {
	if o == nil || o.service == nil {
		return DeductionResult{}, NewError("billed operation is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	settleKey, err := scopedOperationKey(o.operationKey, "settle")
	if err != nil {
		return DeductionResult{}, err
	}
	return o.service.SettleUsage(ctx, o.userID, o.leaseID, actual, SettleOptions{
		OperationUsageOptions: OperationUsageOptions{Feature: o.feature},
		IdempotencyKey:        settleKey,
		Metadata:              o.metadata.Clone(),
	})
}

// RunBilled reserves, executes application work, then settles the durable
// lease. If work fails the lease is released; after work succeeds a failed
// settle is retried only when its transport error is classified retryable.
func (s *CreditsService) RunBilled(ctx context.Context, userID string, options RunBilledOptions) (RunBilledResult, error) {
	if options.DoWork == nil {
		return RunBilledResult{}, errorf(ErrorCodeConfig, ErrorCategoryInvalidRequest, "billed work callback is required")
	}
	attempts, err := normalizeSettlementAttempts(options.SettlementAttempts)
	if err != nil {
		return RunBilledResult{}, err
	}
	operation, err := s.BeginBilledOperation(ctx, userID, BeginBilledOperationOptions{
		Estimate:      options.Estimate,
		OperationKey:  options.OperationKey,
		OperationType: options.OperationType,
		BillingMode:   options.BillingMode,
		TTL:           options.TTL,
		Feature:       options.Feature,
		Metadata:      options.Metadata,
	})
	if err != nil {
		return RunBilledResult{}, err
	}
	workResult, actual, workErr := options.DoWork(ctx)
	if workErr != nil {
		releaseCtx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		_, _ = operation.Release(releaseCtx)
		cancel()
		return RunBilledResult{}, workErr
	}
	var deduction DeductionResult
	for attempt := 0; attempt < attempts; attempt++ {
		deduction, err = operation.Settle(ctx, actual)
		if err == nil {
			return RunBilledResult{Result: workResult, Deduction: deduction}, nil
		}
		if !IsRetryableError(err) || ctx.Err() != nil {
			return RunBilledResult{}, err
		}
	}
	return RunBilledResult{}, err
}

// RunBilledUsage reserves estimated metrics, executes application work, and
// settles the returned final metrics against the lease's immutable pricing
// context. Work failures release the lease; settlement retries reuse one key.
func (s *CreditsService) RunBilledUsage(ctx context.Context, userID string, options RunBilledUsageOptions) (RunBilledResult, error) {
	if options.DoWork == nil {
		return RunBilledResult{}, errorf(ErrorCodeConfig, ErrorCategoryInvalidRequest, "billed usage work callback is required")
	}
	attempts, err := normalizeSettlementAttempts(options.SettlementAttempts)
	if err != nil {
		return RunBilledResult{}, err
	}
	operation, err := s.BeginBilledUsageOperation(ctx, userID, BeginBilledUsageOperationOptions{
		Estimate: options.Estimate, OperationKey: options.OperationKey,
		OperationType: options.OperationType, BillingMode: options.BillingMode,
		TTL: options.TTL, Feature: options.Feature, Metadata: options.Metadata,
	})
	if err != nil {
		return RunBilledResult{}, err
	}
	workResult, actual, workErr := options.DoWork(ctx)
	if workErr != nil {
		releaseCtx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		_, _ = operation.Release(releaseCtx)
		cancel()
		return RunBilledResult{}, workErr
	}
	var deduction DeductionResult
	for attempt := 0; attempt < attempts; attempt++ {
		deduction, err = operation.SettleUsage(ctx, actual)
		if err == nil {
			return RunBilledResult{Result: workResult, Deduction: deduction}, nil
		}
		if !IsRetryableError(err) || ctx.Err() != nil {
			return RunBilledResult{}, err
		}
	}
	return RunBilledResult{}, err
}

func normalizeSettlementAttempts(attempts int) (int, error) {
	if attempts == 0 {
		return 3, nil
	}
	if attempts < 1 {
		return 0, errorf(ErrorCodeConfig, ErrorCategoryInvalidRequest, "settlement attempts must be positive")
	}
	return attempts, nil
}

func pricedOperationOptions(metrics UsageMetrics, _ CostBreakdown, _ string, feature string, _ CreditMetadata) OperationUsageOptions {
	measures := make(map[string]Amount, len(metrics.Measures))
	for key, value := range metrics.Measures {
		measures[key] = value
	}
	dimensions := cloneAnyMap(metrics.Dimensions)
	return OperationUsageOptions{
		Feature:    feature,
		Model:      metricDimensionString(metrics.Dimensions, "model"),
		Region:     metricDimensionString(metrics.Dimensions, "region"),
		Measures:   measures,
		Dimensions: dimensions,
	}
}

func pricedMetadata(metrics UsageMetrics, breakdown CostBreakdown, idempotencyKey string, caller CreditMetadata) CreditMetadata {
	metadata := caller.Clone()
	if metadata == nil {
		metadata = make(CreditMetadata)
	}
	measures := make(map[string]string, len(metrics.Measures))
	for key, value := range metrics.Measures {
		measures[key] = value.String()
	}
	metadata["operation"] = metrics.Operation
	metadata["measures"] = measures
	metadata["dimensions"] = cloneAnyMap(metrics.Dimensions)
	metadata["breakdown_total"] = breakdown.Total.String()
	metadata["idempotency_key"] = idempotencyKey
	return metadata
}

func metricDimensionString(dimensions map[string]any, key string) string {
	value, ok := dimensions[key].(string)
	if !ok {
		return ""
	}
	return strings.TrimSpace(value)
}

func (s *CreditsService) emitQuotaEvents(ctx context.Context, userID, idempotencyKey string) {
	if s == nil || s.store == nil || strings.TrimSpace(idempotencyKey) == "" {
		return
	}
	events, err := s.store.ListQuotaEvents(ctx, userID, ListQuotaEventsOptions{IdempotencyKey: idempotencyKey, Limit: 100})
	if err != nil {
		return
	}
	for _, event := range events {
		data := CreditMetadata{
			"quota_key":         event.QuotaKey,
			"operation":         event.Operation,
			"measure":           event.Measure,
			"threshold_percent": event.ThresholdPercent,
			"usage_charge_id":   event.UsageChargeID,
			"idempotency_key":   event.IdempotencyKey,
		}
		switch event.EventType {
		case "blocked":
			emitCreditEvent(ctx, s.events, CreditEventQuotaBlocked, userID, data)
		case "threshold":
			emitCreditEvent(ctx, s.events, CreditEventQuotaThreshold, userID, data)
		}
	}
}

func scopedOperationKey(operationKey, suffix string) (string, error) {
	key, err := requireStableKey(operationKey, "operation key")
	if err != nil {
		return "", err
	}
	suffix, err = requireText(suffix, "operation key suffix")
	if err != nil {
		return "", err
	}
	return requireStableKey(key+":"+suffix, "operation idempotency key")
}

func isLeaseExpiredCode(code string) bool {
	return code == "lease_expired" || code == "expired_lease"
}

func creditBusinessError(operation, userID, code string) error {
	code = strings.TrimSpace(code)
	message := fmt.Sprintf("credit %s failed for user %s: %s", operation, userID, code)
	details := map[string]any{"operation": operation, "user_id": userID, "store_error_code": code}
	switch code {
	case "insufficient_credits", "insufficient_headroom":
		return NewError(message, ErrorOptions{Code: ErrorCodeInsufficientCredits, Category: ErrorCategoryPaymentRequired, Details: details})
	case "concurrency_limit", "max_concurrent_reached", "concurrency_limit_reached":
		return NewError(message, ErrorOptions{Code: ErrorCodeConcurrencyLimitReached, Category: ErrorCategoryRateLimited, Details: details})
	case "quota_exceeded":
		return NewError(message, ErrorOptions{Code: ErrorCodeQuotaExceeded, Category: ErrorCategoryRateLimited, Details: details})
	case "feature_not_entitled":
		return NewError(message, ErrorOptions{Code: ErrorCodeFeatureNotEntitled, Category: ErrorCategoryForbidden, Details: details})
	case "operation_not_allowed":
		return NewError(message, ErrorOptions{Code: ErrorCodeOperationNotAllowed, Category: ErrorCategoryForbidden, Details: details})
	case "lease_expired", "expired_lease":
		return NewError(message, ErrorOptions{Code: ErrorCodeLeaseExpired, Category: ErrorCategoryConflict, Details: details})
	case "lease_not_found", "not_found", "missing_lease", "released_lease", "settled_lease":
		return NewError(message, ErrorOptions{Code: ErrorCodeLeaseNotFound, Category: ErrorCategoryNotFound, Details: details})
	case "settlement_conflict":
		return NewStoreError(message, ErrorOptions{Category: ErrorCategoryConflict, Details: details})
	case "missing_quota_measure", "invalid_measure", "policy_mismatch", "invalid_amount", "invalid_request":
		return NewError(message, ErrorOptions{Code: ErrorCodeConfig, Category: ErrorCategoryInvalidRequest, Details: details})
	case "refund_rejected", "already_refunded", "over_refund":
		return NewError(message, ErrorOptions{Code: ErrorCodeRefundRejected, Category: ErrorCategoryConflict, Details: details})
	default:
		return NewError(message, ErrorOptions{Code: ErrorCodeCredit, Category: ErrorCategoryConflict, Details: details})
	}
}

func (s *CreditsService) emitPostDeduction(ctx context.Context, userID string, source PostDeductionSource, result DeductionResult) {
	if s == nil || result.BalanceAfter == nil {
		return
	}
	balanceAfter := *result.BalanceAfter
	if balanceAfter.IsNegative() {
		emitCreditEvent(ctx, s.events, CreditEventOverdraft, userID, CreditMetadata{"balance": balanceAfter, "amount": result.Amount})
	}
	s.emitLowBalance(ctx, userID, balanceAfter.Add(result.Amount), balanceAfter)
	s.runPostDeductionHooks(ctx, PostDeductionContext{UserID: userID, Source: source, Deduction: cloneDeductionResult(result)})
}

func cloneDeductionResult(result DeductionResult) DeductionResult {
	clone := result
	if result.BalanceAfter != nil {
		balanceAfter := *result.BalanceAfter
		clone.BalanceAfter = &balanceAfter
	}
	if result.BucketBreakdown != nil {
		clone.BucketBreakdown = make(map[string]Amount, len(result.BucketBreakdown))
		for bucket, amount := range result.BucketBreakdown {
			clone.BucketBreakdown[bucket] = amount
		}
	}
	return clone
}

func (s *CreditsService) runPostDeductionHooks(ctx context.Context, deductionContext PostDeductionContext) {
	if s == nil {
		return
	}
	s.postDeductionMu.RLock()
	ids := make([]uint64, 0, len(s.postDeductionHooks))
	for id := range s.postDeductionHooks {
		ids = append(ids, id)
	}
	sort.Slice(ids, func(left, right int) bool { return ids[left] < ids[right] })
	hooks := make([]PostDeductionHook, 0, len(ids))
	for _, id := range ids {
		hooks = append(hooks, s.postDeductionHooks[id])
	}
	s.postDeductionMu.RUnlock()

	for _, hook := range hooks {
		if hook == nil {
			continue
		}
		func() {
			defer func() { _ = recover() }()
			_ = hook(ctx, deductionContext)
		}()
	}
}

func (s *CreditsService) emitLowBalance(ctx context.Context, userID string, balanceBefore, balanceAfter Amount) {
	if s == nil {
		return
	}
	if len(s.lowBalance) == 0 {
		if balanceBefore.GreaterThan(DecimalZero) && balanceAfter.LessThanOrEqual(DecimalZero) {
			s.fireLowBalance(ctx, userID, balanceAfter, DecimalZero)
		}
		return
	}
	s.lowBalanceMu.Lock()
	below := s.lowBalanceStateForUserLocked(userID)
	var fire *Amount
	for _, threshold := range s.lowBalance {
		key := threshold.StringFixed(MoneyDecimalPlaces)
		if balanceAfter.LessThanOrEqual(threshold) {
			if _, alreadyBelow := below[key]; !alreadyBelow {
				below[key] = struct{}{}
				candidate := threshold
				if fire == nil || candidate.LessThan(*fire) {
					fire = &candidate
				}
			}
		} else {
			delete(below, key)
		}
	}
	s.lowBalanceMu.Unlock()
	if fire != nil {
		s.fireLowBalance(ctx, userID, balanceAfter, *fire)
	}
}

func (s *CreditsService) fireLowBalance(ctx context.Context, userID string, balance, threshold Amount) {
	data := CreditMetadata{"balance": balance, "threshold": threshold}
	emitCreditEvent(ctx, s.events, CreditEventLowBalance, userID, data)
	if s.lowBalanceHandler == nil {
		return
	}
	event := CreditEvent{Type: CreditEventLowBalance, Timestamp: time.Now().UTC(), UserID: userID, Data: data.Clone()}
	func() {
		defer func() { _ = recover() }()
		s.lowBalanceHandler(ctx, event)
	}()
}

func (s *CreditsService) lowBalanceStateForUserLocked(userID string) map[string]struct{} {
	if element := s.lowBalanceUsers[userID]; element != nil {
		s.lowBalanceOrder.MoveToBack(element)
		return s.lowBalanceState[userID]
	}
	below := make(map[string]struct{})
	s.lowBalanceState[userID] = below
	s.lowBalanceUsers[userID] = s.lowBalanceOrder.PushBack(userID)
	for s.lowBalanceOrder.Len() > s.lowBalanceMaxTracked {
		oldest := s.lowBalanceOrder.Front()
		oldestUser, _ := oldest.Value.(string)
		s.lowBalanceOrder.Remove(oldest)
		delete(s.lowBalanceUsers, oldestUser)
		delete(s.lowBalanceState, oldestUser)
	}
	return below
}

func (s *CreditsService) rearmLowBalance(userID string, balance Amount) {
	if s == nil || len(s.lowBalance) == 0 {
		return
	}
	s.lowBalanceMu.Lock()
	defer s.lowBalanceMu.Unlock()
	below := s.lowBalanceState[userID]
	if below == nil {
		return
	}
	for _, threshold := range s.lowBalance {
		if balance.GreaterThan(threshold) {
			delete(below, threshold.StringFixed(MoneyDecimalPlaces))
		}
	}
	if len(below) == 0 {
		delete(s.lowBalanceState, userID)
		if element := s.lowBalanceUsers[userID]; element != nil {
			s.lowBalanceOrder.Remove(element)
			delete(s.lowBalanceUsers, userID)
		}
	}
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			return value
		}
	}
	return ""
}
