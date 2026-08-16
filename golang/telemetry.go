// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import (
	"context"
	"errors"
	"math"
	"reflect"
	"regexp"
	"runtime/debug"
	"strings"
	"sync"
)

// InstrumentationScope identifies this Go module to telemetry providers. Each
// Bursar SDK uses its package/module name while sharing operation names and
// attributes across languages.
const InstrumentationScope = "github.com/Zonastery/bursar/golang/v2"

const unknownInstrumentationVersion = "0+unknown"

// InstrumentationVersion returns the module version recorded by the Go
// toolchain. Source-tree and replace builds deliberately report 0+unknown
// rather than claiming a release version they may not contain.
func InstrumentationVersion() string {
	info, ok := debug.ReadBuildInfo()
	if !ok {
		return unknownInstrumentationVersion
	}
	if info.Main.Path == InstrumentationScope {
		return usableModuleVersion(info.Main.Version)
	}
	for _, dependency := range info.Deps {
		if dependency.Path != InstrumentationScope {
			continue
		}
		if dependency.Replace != nil {
			return usableModuleVersion(dependency.Replace.Version)
		}
		return usableModuleVersion(dependency.Version)
	}
	return unknownInstrumentationVersion
}

func usableModuleVersion(version string) string {
	version = strings.TrimSpace(version)
	if version == "" || version == "(devel)" {
		return unknownInstrumentationVersion
	}
	return version
}

// TelemetryAttributeValue is checked by SanitizeTelemetryAttributes before it
// reaches an instrumentation backend. It is an alias rather than a type-set
// interface because Go permits union interfaces only as generic constraints.
type TelemetryAttributeValue = any

// TelemetryAttributes is a flat, allowlisted attribute map. Do not put raw
// provider payloads, SQL, secrets, account emails, or full UUIDs in it.
type TelemetryAttributes map[string]TelemetryAttributeValue

// Instrumentation creates an operation span/scope and returns a callback that
// must be invoked exactly once with the final error. It is deliberately small
// so applications can adapt OpenTelemetry or their existing observability
// stack without forcing a telemetry SDK or exporter into Bursar core.
type Instrumentation interface {
	Start(context.Context, string, TelemetryAttributes) (context.Context, func(error))
}

// NoopInstrumentation is the default safe zero-overhead implementation.
type NoopInstrumentation struct{}

func (NoopInstrumentation) Start(ctx context.Context, _ string, _ TelemetryAttributes) (context.Context, func(error)) {
	return ctx, func(error) {}
}

func isNilInstrumentation(instrumentation Instrumentation) bool {
	if instrumentation == nil {
		return true
	}
	value := reflect.ValueOf(instrumentation)
	switch value.Kind() {
	case reflect.Chan, reflect.Func, reflect.Interface, reflect.Map, reflect.Pointer, reflect.Slice:
		return value.IsNil()
	default:
		return false
	}
}

type instrumentationRegistration struct {
	instrumentation Instrumentation
	active          bool
}

var (
	defaultInstrumentationMu     sync.RWMutex
	defaultInstrumentation       Instrumentation = NoopInstrumentation{}
	instrumentationRegistrations []*instrumentationRegistration
)

// DefaultInstrumentation returns the process-wide default. Constructed stores
// and services should snapshot this value, so later registrations never mutate
// active SDK objects.
func DefaultInstrumentation() Instrumentation {
	defaultInstrumentationMu.RLock()
	defer defaultInstrumentationMu.RUnlock()
	return defaultInstrumentation
}

// SetDefaultInstrumentation selects the instrumentation used by subsequently
// constructed Bursar components and returns an idempotent restore function.
// Passing nil selects the no-op implementation. Restoring an older registration
// cannot overwrite a newer active registration.
func SetDefaultInstrumentation(instrumentation Instrumentation) func() {
	if isNilInstrumentation(instrumentation) {
		instrumentation = NoopInstrumentation{}
	}
	registration := &instrumentationRegistration{instrumentation: instrumentation, active: true}
	defaultInstrumentationMu.Lock()
	instrumentationRegistrations = append(instrumentationRegistrations, registration)
	refreshDefaultInstrumentationLocked()
	defaultInstrumentationMu.Unlock()

	var once sync.Once
	return func() {
		once.Do(func() {
			defaultInstrumentationMu.Lock()
			registration.active = false
			refreshDefaultInstrumentationLocked()
			defaultInstrumentationMu.Unlock()
		})
	}
}

func refreshDefaultInstrumentationLocked() {
	for len(instrumentationRegistrations) > 0 {
		last := instrumentationRegistrations[len(instrumentationRegistrations)-1]
		if last.active {
			defaultInstrumentation = last.instrumentation
			return
		}
		instrumentationRegistrations = instrumentationRegistrations[:len(instrumentationRegistrations)-1]
	}
	defaultInstrumentation = NoopInstrumentation{}
}

var (
	telemetryCamelCaseBoundary = regexp.MustCompile(`([a-z0-9])([A-Z])`)
	telemetryUnsafeTokenChars  = regexp.MustCompile(`[^a-zA-Z0-9._-]+`)
	telemetryTokenEdges        = regexp.MustCompile(`^[_\-.]+|[_\-.]+$`)
)

const maxTelemetryAttributeLength = 64

const (
	telemetryOperationCreditsDeduct                 = "credits.deduct"
	telemetryOperationCreditsGrant                  = "credits.grant"
	telemetryOperationCreditsGrantProgram           = "credits.grant_program"
	telemetryOperationCreditsGrantSubscriptionCycle = "credits.grant_subscription_cycle"
	telemetryOperationCreditsRefund                 = "credits.refund"
	telemetryOperationCreditsRelease                = "credits.release"
	telemetryOperationCreditsReserve                = "credits.reserve"
	telemetryOperationCreditsSettle                 = "credits.settle"
	telemetryOperationPostgresQuery                 = "postgres.query"
	telemetryOperationPostgresRPC                   = "postgres.rpc"
	telemetryOperationCreditsCanAfford              = "credits.can_afford"
	telemetryOperationCreditsRecordUsage            = "credits.record_usage"
	telemetryOperationCreditsDeductTeam             = "credits.deduct_team"
)

var allowedTelemetryAttributeKeys = map[string]struct{}{
	"bursar.operation": {},
	"bursar.outcome":   {},
	"bursar.backend":   {},
	"bursar.provider":  {},
	"error.type":       {},
	"error.code":       {},
}

func normalizeTelemetryToken(value, fallback string) string {
	normalized := telemetryCamelCaseBoundary.ReplaceAllString(strings.TrimSpace(value), `${1}_${2}`)
	normalized = telemetryUnsafeTokenChars.ReplaceAllString(normalized, "_")
	normalized = strings.ToLower(telemetryTokenEdges.ReplaceAllString(normalized, ""))
	if len(normalized) > maxTelemetryAttributeLength {
		normalized = normalized[:maxTelemetryAttributeLength]
	}
	if normalized == "" {
		return fallback
	}
	return normalized
}

// SanitizeTelemetryAttributes returns a detached map containing only bounded,
// low-cardinality Bursar attributes. Unknown keys, non-finite numbers, complex
// values, identifiers, metadata, and raw payloads are discarded.
func SanitizeTelemetryAttributes(attributes TelemetryAttributes) TelemetryAttributes {
	if len(attributes) == 0 {
		return nil
	}
	result := make(TelemetryAttributes, len(attributes))
	for key, value := range attributes {
		if _, ok := allowedTelemetryAttributeKeys[key]; !ok {
			continue
		}
		if sanitized, ok := sanitizeTelemetryAttributeValue(value); ok {
			result[key] = sanitized
		}
	}
	if len(result) == 0 {
		return nil
	}
	return result
}

func sanitizeTelemetryAttributeValue(value TelemetryAttributeValue) (TelemetryAttributeValue, bool) {
	reflected := reflect.ValueOf(value)
	if !reflected.IsValid() {
		return nil, false
	}
	switch reflected.Kind() {
	case reflect.String:
		normalized := normalizeTelemetryToken(reflected.String(), "")
		return normalized, normalized != ""
	case reflect.Bool:
		return reflected.Bool(), true
	case reflect.Int, reflect.Int8, reflect.Int16, reflect.Int32, reflect.Int64:
		return reflected.Int(), true
	case reflect.Uint, reflect.Uint8, reflect.Uint16, reflect.Uint32, reflect.Uint64:
		unsigned := reflected.Uint()
		if unsigned <= math.MaxInt64 {
			return int64(unsigned), true
		}
	case reflect.Float32, reflect.Float64:
		floating := reflected.Float()
		if !math.IsNaN(floating) && !math.IsInf(floating, 0) {
			return floating, true
		}
	}
	return nil, false
}

// TelemetryOperationAttributes builds the safe base attributes for one Bursar
// operation. The explicit operation argument always wins over caller input.
func TelemetryOperationAttributes(operation string, attributes TelemetryAttributes) TelemetryAttributes {
	result := SanitizeTelemetryAttributes(attributes)
	if result == nil {
		result = make(TelemetryAttributes, 1)
	}
	result["bursar.operation"] = normalizeTelemetryToken(operation, "unknown")
	return result
}

// TelemetryErrorAttributes classifies an error without reading or recording
// its message, details, cause text, SQL, identifiers, or provider payloads.
func TelemetryErrorAttributes(err error) TelemetryAttributes {
	errorType := "unknown_error"
	result := make(TelemetryAttributes, 2)
	if bursarError, ok := AsBursarError(err); ok {
		errorType = "BursarError"
		if code := normalizeTelemetryToken(string(bursarError.Code), ""); code != "" && isKnownTelemetryErrorCode(bursarError.Code) {
			result["error.code"] = code
		}
	} else if err != nil {
		// Do not trust arbitrary concrete type names as telemetry dimensions.
		// Bursar errors carry the stable typed taxonomy; every other Go error is
		// deliberately collapsed to the bounded language-level error category.
		errorType = "Error"
	}
	result["error.type"] = normalizeTelemetryToken(errorType, "unknown_error")
	return result
}

func isKnownTelemetryErrorCode(code ErrorCode) bool {
	switch code {
	case ErrorCodeAutoRechargeDisabled,
		ErrorCodeAutoRechargeNotConfigured,
		ErrorCodeBilling,
		ErrorCodeBursar,
		ErrorCodeImport,
		ErrorCodeCapabilityNotConfigured,
		ErrorCodeCapabilityNotSupported,
		ErrorCodeCapReached,
		ErrorCodeCatalogNotLoaded,
		ErrorCodeConcurrencyLimitReached,
		ErrorCodeConfig,
		ErrorCodeCredit,
		ErrorCodeExpression,
		ErrorCodeFeatureNotEntitled,
		ErrorCodeInsufficientCredits,
		ErrorCodeLeaseExpired,
		ErrorCodeLeaseNotFound,
		ErrorCodeOperationNotAllowed,
		ErrorCodePaymentMethodRequired,
		ErrorCodeProviderCapabilityNotSupported,
		ErrorCodeProviderResponseInvalid,
		ErrorCodeQuotaExceeded,
		ErrorCodeRefundRejected,
		ErrorCodeStoreClosed,
		ErrorCodeStore,
		ErrorCodeStoreTimeout,
		ErrorCodeStoreUnavailable,
		ErrorCodeCommerceNotConfigured,
		ErrorCodeUnknownOffer,
		ErrorCodeInvalidOfferQuantity,
		ErrorCodeActiveSubscription,
		ErrorCodeCheckoutConflict,
		ErrorCodeCheckoutCompleted,
		ErrorCodeCommerceResourceNotFound,
		ErrorCodeProviderSelectionFailed,
		ErrorCodeQuoteChanged,
		ErrorCodePlanChangePolicyMissing,
		ErrorCodeCoreBillingDataUnavailable:
		return true
	default:
		return false
	}
}

type activeTelemetryOperationKey struct{}

var errInstrumentedOperationPanicked = errors.New("Bursar operation panicked")

func beginInstrumented(ctx context.Context, instrumentation Instrumentation, operation string, attributes TelemetryAttributes) (context.Context, func(error)) {
	if ctx == nil {
		ctx = context.Background()
	}
	if activeOperation, ok := ctx.Value(activeTelemetryOperationKey{}).(string); ok && activeOperation == operation {
		return ctx, func(error) {}
	}
	if isNilInstrumentation(instrumentation) {
		instrumentation = NoopInstrumentation{}
	}
	startedContext, finish := instrumentation.Start(ctx, operation, TelemetryOperationAttributes(operation, attributes))
	if startedContext == nil {
		startedContext = ctx
	}
	if finish == nil {
		finish = func(error) {}
	}
	return context.WithValue(startedContext, activeTelemetryOperationKey{}, operation), finish
}

func runInstrumentedValue[T any](ctx context.Context, instrumentation Instrumentation, operation string, attributes TelemetryAttributes, run func(context.Context) (T, error)) (result T, err error) {
	ctx, finish := beginInstrumented(ctx, instrumentation, operation, attributes)
	defer func() {
		if recovered := recover(); recovered != nil {
			finish(errInstrumentedOperationPanicked)
			panic(recovered)
		}
		finish(err)
	}()
	return run(ctx)
}
