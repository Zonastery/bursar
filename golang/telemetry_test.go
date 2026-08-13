// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import (
	"context"
	"encoding/json"
	"errors"
	"math"
	"os"
	"reflect"
	"strings"
	"sync"
	"testing"
)

type recordedTelemetryStart struct {
	operation  string
	attributes TelemetryAttributes
}

type recordingInstrumentation struct {
	mu       sync.Mutex
	starts   []recordedTelemetryStart
	finishes []error
}

func (instrumentation *recordingInstrumentation) Start(ctx context.Context, operation string, attributes TelemetryAttributes) (context.Context, func(error)) {
	instrumentation.mu.Lock()
	instrumentation.starts = append(instrumentation.starts, recordedTelemetryStart{
		operation:  operation,
		attributes: cloneTelemetryAttributes(attributes),
	})
	instrumentation.mu.Unlock()
	return ctx, func(err error) {
		instrumentation.mu.Lock()
		instrumentation.finishes = append(instrumentation.finishes, err)
		instrumentation.mu.Unlock()
	}
}

func (instrumentation *recordingInstrumentation) operations() []string {
	instrumentation.mu.Lock()
	defer instrumentation.mu.Unlock()
	operations := make([]string, len(instrumentation.starts))
	for index, start := range instrumentation.starts {
		operations[index] = start.operation
	}
	return operations
}

func cloneTelemetryAttributes(attributes TelemetryAttributes) TelemetryAttributes {
	if attributes == nil {
		return nil
	}
	cloned := make(TelemetryAttributes, len(attributes))
	for key, value := range attributes {
		cloned[key] = value
	}
	return cloned
}

func TestSanitizeTelemetryAttributesMatchesSharedContract(t *testing.T) {
	type telemetryCount int16
	longValue := strings.Repeat("A", 80)
	original := TelemetryAttributes{
		"bursar.operation":   "  CreditsGrantProgram  ",
		"bursar.outcome":     true,
		"bursar.backend":     longValue,
		"bursar.provider":    "Stripe Webhook/Receiver",
		"error.type":         telemetryCount(42),
		"error.code":         float64(3.5),
		"bursar.environment": "must-be-dropped",
		"secret":             "sk_live_do_not_record",
	}

	got := SanitizeTelemetryAttributes(original)
	want := TelemetryAttributes{
		"bursar.operation": "credits_grant_program",
		"bursar.outcome":   true,
		"bursar.backend":   strings.Repeat("a", maxTelemetryAttributeLength),
		"bursar.provider":  "stripe_webhook_receiver",
		"error.type":       int64(42),
		"error.code":       3.5,
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("SanitizeTelemetryAttributes() = %#v, want %#v", got, want)
	}
	original["bursar.provider"] = "changed"
	if got["bursar.provider"] != "stripe_webhook_receiver" {
		t.Fatal("sanitized attributes alias the caller's map")
	}

	nonFinite := SanitizeTelemetryAttributes(TelemetryAttributes{
		"bursar.backend":  math.NaN(),
		"bursar.provider": math.Inf(1),
		"error.type":      []string{"not", "scalar"},
		"error.code":      uint64(math.MaxInt64) + 1,
	})
	if nonFinite != nil {
		t.Fatalf("non-finite/complex attributes = %#v, want nil", nonFinite)
	}
}

func TestTelemetryOperationAndErrorAttributesDoNotLeakMessages(t *testing.T) {
	operation := TelemetryOperationAttributes("  CreditsGrantProgram  ", TelemetryAttributes{
		"bursar.operation": "caller-controlled",
		"bursar.backend":   "Postgres",
		"secret":           "password=do-not-record",
	})
	if operation["bursar.operation"] != "credits_grant_program" || operation["bursar.backend"] != "postgres" {
		t.Fatalf("operation attributes = %#v", operation)
	}
	if _, ok := operation["secret"]; ok {
		t.Fatal("unknown secret attribute passed the allowlist")
	}

	classified := NewStoreUnavailableError("password=do-not-record", errors.New("postgresql://secret"))
	errorAttributes := TelemetryErrorAttributes(classified)
	want := TelemetryAttributes{"error.type": "bursar_error", "error.code": "store_unavailable"}
	if !reflect.DeepEqual(errorAttributes, want) {
		t.Fatalf("TelemetryErrorAttributes() = %#v, want %#v", errorAttributes, want)
	}
	encoded, err := json.Marshal(errorAttributes)
	if err != nil {
		t.Fatalf("json.Marshal() error = %v", err)
	}
	if strings.Contains(string(encoded), "password") || strings.Contains(string(encoded), "postgresql") {
		t.Fatalf("safe error attributes leaked raw diagnostics: %s", encoded)
	}
	if got := TelemetryErrorAttributes(errors.New("secret plain error")); !reflect.DeepEqual(got, TelemetryAttributes{"error.type": "error"}) {
		t.Fatalf("plain error attributes = %#v, want fixed error taxonomy", got)
	}
	unknownCode := NewError("secret", ErrorOptions{Code: ErrorCode("USER_CONTROLLED_SECRET_CODE")})
	if got := TelemetryErrorAttributes(unknownCode); !reflect.DeepEqual(got, TelemetryAttributes{"error.type": "bursar_error"}) {
		t.Fatalf("unknown code attributes = %#v, want code omitted", got)
	}
}

func TestSetDefaultInstrumentationRestoresWithoutClobberingNewerRegistration(t *testing.T) {
	first := &recordingInstrumentation{}
	second := &recordingInstrumentation{}
	restoreFirst := SetDefaultInstrumentation(first)
	restoreSecond := SetDefaultInstrumentation(second)
	defer restoreFirst()
	defer restoreSecond()

	if DefaultInstrumentation() != second {
		t.Fatal("latest registration was not selected")
	}
	restoreFirst()
	if DefaultInstrumentation() != second {
		t.Fatal("stale restore overwrote the newer registration")
	}
	restoreSecond()
	if _, ok := DefaultInstrumentation().(NoopInstrumentation); !ok {
		t.Fatalf("default after restores = %T, want NoopInstrumentation", DefaultInstrumentation())
	}
	restoreSecond()
	var typedNil *recordingInstrumentation
	restoreNil := SetDefaultInstrumentation(typedNil)
	if _, ok := DefaultInstrumentation().(NoopInstrumentation); !ok {
		t.Fatalf("typed nil selected %T, want NoopInstrumentation", DefaultInstrumentation())
	}
	restoreNil()
}

func TestConstructedComponentsSnapshotInstrumentation(t *testing.T) {
	first := &recordingInstrumentation{}
	second := &recordingInstrumentation{}
	restoreFirst := SetDefaultInstrumentation(first)
	defer restoreFirst()

	service, err := NewCreditsService(&creditStoreStub{}, CreditsServiceOptions{})
	if err != nil {
		t.Fatalf("NewCreditsService() error = %v", err)
	}
	postgresOptions, err := (PostgresClientOptions{}).normalized()
	if err != nil {
		t.Fatalf("PostgresClientOptions.normalized() error = %v", err)
	}
	restoreSecond := SetDefaultInstrumentation(second)
	defer restoreSecond()

	if service.instrumentation != first || postgresOptions.Instrumentation != first {
		t.Fatal("constructed components did not snapshot the active instrumentation")
	}
	explicit, err := NewCreditsService(&creditStoreStub{}, CreditsServiceOptions{Instrumentation: second})
	if err != nil {
		t.Fatalf("NewCreditsService(explicit) error = %v", err)
	}
	if explicit.instrumentation != second {
		t.Fatal("explicit instrumentation was not selected")
	}
}

func TestNestedIdenticalOperationIsInstrumentedOnce(t *testing.T) {
	instrumentation := &recordingInstrumentation{}
	_, err := runInstrumentedValue(context.Background(), instrumentation, telemetryOperationCreditsDeduct, nil, func(ctx context.Context) (string, error) {
		return runInstrumentedValue(ctx, instrumentation, telemetryOperationCreditsDeduct, nil, func(context.Context) (string, error) {
			return "ok", nil
		})
	})
	if err != nil {
		t.Fatalf("runInstrumentedValue() error = %v", err)
	}
	if got := instrumentation.operations(); !reflect.DeepEqual(got, []string{telemetryOperationCreditsDeduct}) {
		t.Fatalf("operations = %v, want one deduct operation", got)
	}
	if len(instrumentation.finishes) != 1 || instrumentation.finishes[0] != nil {
		t.Fatalf("finishes = %#v, want one successful completion", instrumentation.finishes)
	}
}

func TestInstrumentedOperationCompletesOnceAndPreservesPanic(t *testing.T) {
	instrumentation := &recordingInstrumentation{}
	panicValue := struct{ secret string }{secret: "do-not-record"}
	func() {
		defer func() {
			if recovered := recover(); recovered != panicValue {
				t.Fatalf("recovered panic = %#v, want original value", recovered)
			}
		}()
		_, _ = runInstrumentedValue(context.Background(), instrumentation, "credits.deduct", nil, func(context.Context) (int, error) {
			panic(panicValue)
		})
	}()
	if len(instrumentation.finishes) != 1 || instrumentation.finishes[0] != errInstrumentedOperationPanicked {
		t.Fatalf("panic finishes = %#v, want one fixed safe error", instrumentation.finishes)
	}
}

func TestSharedTelemetryOperationsAreWiredToGoBoundaries(t *testing.T) {
	fixtureBytes, err := os.ReadFile("../tests/parity/telemetry_operations.json")
	if err != nil {
		t.Fatalf("read shared telemetry fixture: %v", err)
	}
	var fixture []string
	if err := json.Unmarshal(fixtureBytes, &fixture); err != nil {
		t.Fatalf("decode shared telemetry fixture: %v", err)
	}

	instrumentation := &recordingInstrumentation{}
	service := &CreditsService{instrumentation: instrumentation}
	ctx := context.Background()
	_, _ = service.Deduct(ctx, "secret-user", DecimalZero, DeductWithAllowanceOptions{})
	_, _ = service.AddCredits(ctx, "secret-user", DecimalZero, AddCreditsOptions{})
	_, _ = service.ExecuteGrantProgram(ctx, ExecuteGrantProgramRequest{})
	_, _ = service.GrantSubscriptionCycle(ctx, "secret-user", DecimalZero, GrantSubscriptionCycleOptions{})
	_, _ = service.RefundCredits(ctx, "secret-entry", nil, "secret reason", nil, "secret-key")
	_, _ = service.Release(ctx, "secret-user", "secret-lease")
	_, _ = service.Reserve(ctx, "secret-user", DecimalZero, ReserveOptions{})
	_, _ = service.Settle(ctx, "secret-user", "secret-lease", DecimalZero, SettleOptions{})

	transaction := &PostgresTransaction{instrumentation: instrumentation}
	_, _ = transaction.Query(ctx, "SELECT 'secret-sql'")
	_, _ = transaction.Call(ctx, "secret_rpc", "secret-argument")

	if got := instrumentation.operations(); !reflect.DeepEqual(got, fixture) {
		t.Fatalf("instrumented operations = %v, want shared fixture %v", got, fixture)
	}
	for _, start := range instrumentation.starts {
		for key, value := range start.attributes {
			text := strings.ToLower(key + "=" + strings.TrimSpace(toTelemetryTestString(value)))
			if strings.Contains(text, "secret") || strings.Contains(text, "select") {
				t.Fatalf("operation %q leaked sensitive input in %q", start.operation, text)
			}
		}
	}
}

func TestMetricPricedConvenienceMethodsUseTerminalOperationNames(t *testing.T) {
	instrumentation := &recordingInstrumentation{}
	service := &CreditsService{instrumentation: instrumentation}
	ctx := context.Background()
	metrics := UsageMetrics{Operation: "secret-operation"}

	_, _ = service.DeductUsage(ctx, "user", metrics, PricedUsageOptions{})
	_, _ = service.DeductFlatJob(ctx, "user", "secret-job", PricedUsageOptions{})
	_, _ = service.ReserveUsage(ctx, "user", metrics, ReserveOptions{})
	_, _ = service.SettleUsage(ctx, "user", "lease", metrics, SettleOptions{})
	_, _ = service.CanAffordUsage(ctx, "user", metrics, CanAffordOptions{})
	_, _ = service.RecordUsageMetrics(ctx, "user", metrics, PricedUsageRecordOptions{})
	_, _ = service.DeductTeamUsage(ctx, "team", "user", metrics, PricedTeamDeductionOptions{})

	want := []string{
		telemetryOperationCreditsDeduct,
		telemetryOperationCreditsDeduct,
		telemetryOperationCreditsReserve,
		telemetryOperationCreditsSettle,
		telemetryOperationCreditsCanAfford,
		telemetryOperationCreditsRecordUsage,
		telemetryOperationCreditsDeductTeam,
	}
	if got := instrumentation.operations(); !reflect.DeepEqual(got, want) {
		t.Fatalf("convenience method operations = %v, want %v", got, want)
	}
}

func toTelemetryTestString(value any) string {
	encoded, _ := json.Marshal(value)
	return string(encoded)
}
