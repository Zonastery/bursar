// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import (
	"context"
	"strings"
	"sync"
)

// InstrumentationScope is stable across Bursar SDKs so traces from different
// services can be queried together without a language-specific taxonomy.
const InstrumentationScope = "github.com/Zonastery/bursar"

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
// stack without forcing a transitive dependency into every Bursar consumer.
type Instrumentation interface {
	Start(context.Context, string, TelemetryAttributes) (context.Context, func(error))
}

// NoopInstrumentation is the default safe zero-overhead implementation.
type NoopInstrumentation struct{}

func (NoopInstrumentation) Start(ctx context.Context, _ string, _ TelemetryAttributes) (context.Context, func(error)) {
	return ctx, func(error) {}
}

var (
	defaultInstrumentationMu sync.RWMutex
	defaultInstrumentation   Instrumentation = NoopInstrumentation{}
)

// DefaultInstrumentation returns the process-wide default. Constructed stores
// should snapshot this value, so changing it never mutates active SDK objects.
func DefaultInstrumentation() Instrumentation {
	defaultInstrumentationMu.RLock()
	defer defaultInstrumentationMu.RUnlock()
	return defaultInstrumentation
}

// SetDefaultInstrumentation changes the instrumentation used by subsequently
// constructed Bursar components. Passing nil restores the no-op implementation.
func SetDefaultInstrumentation(instrumentation Instrumentation) {
	if instrumentation == nil {
		instrumentation = NoopInstrumentation{}
	}
	defaultInstrumentationMu.Lock()
	defaultInstrumentation = instrumentation
	defaultInstrumentationMu.Unlock()
}

// SanitizeTelemetryAttributes returns a detached map containing only bounded,
// low-cardinality Bursar attributes. The exact keys intentionally mirror the
// cross-SDK telemetry contract rather than accepting arbitrary caller labels.
func SanitizeTelemetryAttributes(attributes TelemetryAttributes) TelemetryAttributes {
	if len(attributes) == 0 {
		return nil
	}
	allowed := map[string]struct{}{
		"bursar.backend":         {},
		"bursar.operation":       {},
		"bursar.provider":        {},
		"bursar.environment":     {},
		"bursar.result":          {},
		"bursar.error_code":      {},
		"bursar.catalog_version": {},
	}
	result := make(TelemetryAttributes, len(attributes))
	for key, value := range attributes {
		if _, ok := allowed[key]; !ok {
			continue
		}
		if stringValue, ok := value.(string); ok {
			stringValue = strings.TrimSpace(stringValue)
			if len(stringValue) > 128 {
				stringValue = stringValue[:128]
			}
			if stringValue == "" {
				continue
			}
			result[key] = stringValue
			continue
		}
		switch value.(type) {
		case bool, int, int64, float64:
			result[key] = value
		}
	}
	if len(result) == 0 {
		return nil
	}
	return result
}

func runInstrumented(ctx context.Context, instrumentation Instrumentation, operation string, attributes TelemetryAttributes, run func(context.Context) error) error {
	if instrumentation == nil {
		instrumentation = NoopInstrumentation{}
	}
	ctx, finish := instrumentation.Start(ctx, operation, SanitizeTelemetryAttributes(attributes))
	err := run(ctx)
	finish(err)
	return err
}
