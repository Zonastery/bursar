package bursar

import (
	"errors"
	"fmt"
)

// ErrorCategory is the transport-neutral classification of a Bursar failure.
// Applications can map it to protocol behaviour without coupling to a
// particular credit, billing, or provider error code.
type ErrorCategory string

const (
	ErrorCategoryInvalidRequest  ErrorCategory = "invalid_request"
	ErrorCategoryPaymentRequired ErrorCategory = "payment_required"
	ErrorCategoryForbidden       ErrorCategory = "forbidden"
	ErrorCategoryNotFound        ErrorCategory = "not_found"
	ErrorCategoryConflict        ErrorCategory = "conflict"
	ErrorCategoryRateLimited     ErrorCategory = "rate_limited"
	ErrorCategoryUnavailable     ErrorCategory = "unavailable"
	ErrorCategoryInternal        ErrorCategory = "internal"
)

// ErrorCode is a stable, low-cardinality SDK error code.
type ErrorCode string

const (
	ErrorCodeAutoRechargeDisabled           ErrorCode = "AUTO_RECHARGE_DISABLED"
	ErrorCodeAutoRechargeNotConfigured      ErrorCode = "AUTO_RECHARGE_NOT_CONFIGURED"
	ErrorCodeBilling                        ErrorCode = "BILLING_ERROR"
	ErrorCodeBursar                         ErrorCode = "BURSAR_ERROR"
	ErrorCodeImport                         ErrorCode = "BURSAR_IMPORT_ERROR"
	ErrorCodeCapabilityNotConfigured        ErrorCode = "CAPABILITY_NOT_CONFIGURED"
	ErrorCodeCapabilityNotSupported         ErrorCode = "CAPABILITY_NOT_SUPPORTED"
	ErrorCodeCapReached                     ErrorCode = "CAP_REACHED"
	ErrorCodeCatalogNotLoaded               ErrorCode = "CATALOG_NOT_LOADED"
	ErrorCodeConcurrencyLimitReached        ErrorCode = "CONCURRENCY_LIMIT_REACHED"
	ErrorCodeConfig                         ErrorCode = "CONFIG_ERROR"
	ErrorCodeCredit                         ErrorCode = "CREDIT_ERROR"
	ErrorCodeExpression                     ErrorCode = "EXPRESSION_ERROR"
	ErrorCodeFeatureNotEntitled             ErrorCode = "FEATURE_NOT_ENTITLED"
	ErrorCodeInsufficientCredits            ErrorCode = "INSUFFICIENT_CREDITS"
	ErrorCodeLeaseExpired                   ErrorCode = "LEASE_EXPIRED"
	ErrorCodeLeaseNotFound                  ErrorCode = "LEASE_NOT_FOUND"
	ErrorCodeOperationNotAllowed            ErrorCode = "OPERATION_NOT_ALLOWED"
	ErrorCodePaymentMethodRequired          ErrorCode = "PAYMENT_METHOD_REQUIRED"
	ErrorCodeProviderCapabilityNotSupported ErrorCode = "PROVIDER_CAPABILITY_NOT_SUPPORTED"
	ErrorCodeProviderResponseInvalid        ErrorCode = "PROVIDER_RESPONSE_INVALID"
	ErrorCodeQuotaExceeded                  ErrorCode = "QUOTA_EXCEEDED"
	ErrorCodeRefundRejected                 ErrorCode = "REFUND_REJECTED"
	ErrorCodeStoreClosed                    ErrorCode = "STORE_CLOSED"
	ErrorCodeStore                          ErrorCode = "STORE_ERROR"
	ErrorCodeStoreTimeout                   ErrorCode = "STORE_TIMEOUT"
	ErrorCodeStoreUnavailable               ErrorCode = "STORE_UNAVAILABLE"
	ErrorCodeCommerceNotConfigured          ErrorCode = "COMMERCE_NOT_CONFIGURED"
	ErrorCodeUnknownOffer                   ErrorCode = "UNKNOWN_OFFER"
	ErrorCodeInvalidOfferQuantity           ErrorCode = "INVALID_OFFER_QUANTITY"
	ErrorCodeActiveSubscription             ErrorCode = "ACTIVE_SUBSCRIPTION"
	ErrorCodeCheckoutConflict               ErrorCode = "CHECKOUT_CONFLICT"
	ErrorCodeCheckoutCompleted              ErrorCode = "CHECKOUT_COMPLETED"
	ErrorCodeCommerceResourceNotFound       ErrorCode = "COMMERCE_RESOURCE_NOT_FOUND"
	ErrorCodeProviderSelectionFailed        ErrorCode = "PROVIDER_SELECTION_FAILED"
	ErrorCodeQuoteChanged                   ErrorCode = "QUOTE_CHANGED"
	ErrorCodePlanChangePolicyMissing        ErrorCode = "PLAN_CHANGE_POLICY_MISSING"
	ErrorCodeCoreBillingDataUnavailable     ErrorCode = "CORE_BILLING_DATA_UNAVAILABLE"
)

// ErrorOptions configures a BursarError. Details must be non-secret,
// transport-safe diagnostic context.
type ErrorOptions struct {
	Code          ErrorCode
	Category      ErrorCategory
	Retryable     bool
	Indeterminate bool
	Details       map[string]any
	Cause         error
}

// BursarError is the SDK's structured failure type. Store errors set
// Indeterminate when PostgreSQL may have committed a mutation before a
// transport error prevented the caller from observing its result.
type BursarError struct {
	Message       string
	Code          ErrorCode
	Category      ErrorCategory
	Retryable     bool
	Indeterminate bool
	Details       map[string]any
	cause         error
}

func (e *BursarError) Error() string {
	if e == nil {
		return "<nil>"
	}
	return e.Message
}

// Unwrap preserves the driver/provider cause for server-side diagnostics.
func (e *BursarError) Unwrap() error {
	if e == nil {
		return nil
	}
	return e.cause
}

// Is supports errors.Is against a BursarError carrying the same stable code.
func (e *BursarError) Is(target error) bool {
	other, ok := target.(*BursarError)
	return ok && other != nil && e != nil && other.Code != "" && e.Code == other.Code
}

// SerializedError is a safe, predictable representation for logs and HTTP
// adapters. It intentionally excludes the underlying cause.
type SerializedError struct {
	Message       string         `json:"message"`
	Code          ErrorCode      `json:"code"`
	Category      ErrorCategory  `json:"category"`
	Retryable     bool           `json:"retryable"`
	Indeterminate bool           `json:"indeterminate,omitempty"`
	Details       map[string]any `json:"details,omitempty"`
}

// Serialize returns a detached error representation suitable for output.
func (e *BursarError) Serialize() SerializedError {
	if e == nil {
		return SerializedError{Message: "unknown Bursar error", Code: ErrorCodeBursar, Category: ErrorCategoryInternal}
	}
	return SerializedError{
		Message:       e.Message,
		Code:          e.Code,
		Category:      e.Category,
		Retryable:     e.Retryable,
		Indeterminate: e.Indeterminate,
		Details:       cloneAnyMap(e.Details),
	}
}

// NewError builds a structured Bursar failure. Code and category default to
// BURSAR_ERROR/internal when omitted.
func NewError(message string, options ErrorOptions) *BursarError {
	code := options.Code
	if code == "" {
		code = ErrorCodeBursar
	}
	category := options.Category
	if category == "" {
		category = ErrorCategoryInternal
	}
	return &BursarError{
		Message:       message,
		Code:          code,
		Category:      category,
		Retryable:     options.Retryable,
		Indeterminate: options.Indeterminate,
		Details:       cloneAnyMap(options.Details),
		cause:         options.Cause,
	}
}

func newConfigError(message string, cause error) *BursarError {
	return NewError(message, ErrorOptions{Code: ErrorCodeConfig, Category: ErrorCategoryInvalidRequest, Cause: cause})
}

func newExpressionError(message string, cause error) *BursarError {
	return NewError(message, ErrorOptions{Code: ErrorCodeExpression, Category: ErrorCategoryInvalidRequest, Cause: cause})
}

// NewStoreError creates a structured storage failure. Callers must retry an
// indeterminate mutation only with the exact same idempotency key.
func NewStoreError(message string, options ErrorOptions) *BursarError {
	options.Code = firstErrorCode(options.Code, ErrorCodeStore)
	options.Category = firstErrorCategory(options.Category, ErrorCategoryUnavailable)
	return NewError(message, options)
}

// NewStoreUnavailableError creates a transient transport/storage failure.
func NewStoreUnavailableError(message string, cause error) *BursarError {
	return NewError(message, ErrorOptions{
		Code:      ErrorCodeStoreUnavailable,
		Category:  ErrorCategoryUnavailable,
		Retryable: true,
		Cause:     cause,
	})
}

// NewStoreTimeoutError creates a transient deadline failure.
func NewStoreTimeoutError(message string, cause error) *BursarError {
	return NewError(message, ErrorOptions{
		Code:      ErrorCodeStoreTimeout,
		Category:  ErrorCategoryUnavailable,
		Retryable: true,
		Cause:     cause,
	})
}

func firstErrorCode(value, fallback ErrorCode) ErrorCode {
	if value == "" {
		return fallback
	}
	return value
}

func firstErrorCategory(value, fallback ErrorCategory) ErrorCategory {
	if value == "" {
		return fallback
	}
	return value
}

// AsBursarError recognizes wrapped Bursar errors using Go's standard error
// chain semantics.
func AsBursarError(err error) (*BursarError, bool) {
	var target *BursarError
	if errors.As(err, &target) {
		return target, true
	}
	return nil, false
}

// IsRetryableError reports whether a classified Bursar error is safe to retry
// from a transport perspective. Mutation callers still need idempotency.
func IsRetryableError(err error) bool {
	bursarError, ok := AsBursarError(err)
	return ok && bursarError.Retryable
}

// ErrorHTTPStatus maps a Bursar category to its conventional HTTP status.
func ErrorHTTPStatus(err error) int {
	bursarError, ok := AsBursarError(err)
	if !ok {
		return 500
	}
	switch bursarError.Category {
	case ErrorCategoryInvalidRequest:
		return 400
	case ErrorCategoryPaymentRequired:
		return 402
	case ErrorCategoryForbidden:
		return 403
	case ErrorCategoryNotFound:
		return 404
	case ErrorCategoryConflict:
		return 409
	case ErrorCategoryRateLimited:
		return 429
	case ErrorCategoryUnavailable:
		return 503
	default:
		return 500
	}
}

// PublicErrorMessage returns safe generic copy for a customer-facing surface.
func PublicErrorMessage(err error) string {
	bursarError, ok := AsBursarError(err)
	if !ok {
		return "Billing service is temporarily unavailable. Please try again."
	}
	switch bursarError.Category {
	case ErrorCategoryInvalidRequest:
		return "The billing request is invalid."
	case ErrorCategoryPaymentRequired:
		return "Payment is required to continue."
	case ErrorCategoryForbidden:
		return "Your current plan does not allow this operation."
	case ErrorCategoryNotFound:
		return "The requested billing resource was not found."
	case ErrorCategoryConflict:
		return "The request conflicts with the current billing state."
	case ErrorCategoryRateLimited:
		return "A billing or usage limit has been reached."
	default:
		return "Billing service is temporarily unavailable. Please try again."
	}
}

func cloneAnyMap(value map[string]any) map[string]any {
	if value == nil {
		return nil
	}
	cloned := make(map[string]any, len(value))
	for key, item := range value {
		cloned[key] = item
	}
	return cloned
}

func errorf(code ErrorCode, category ErrorCategory, format string, args ...any) *BursarError {
	return NewError(fmt.Sprintf(format, args...), ErrorOptions{Code: code, Category: category})
}
