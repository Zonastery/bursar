// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

// Package opentelemetry adapts Bursar's vendor-neutral instrumentation
// boundary to the official OpenTelemetry Go API. It configures no SDK,
// processor, reader, or exporter; the embedding application owns those.
package opentelemetry

import (
	"context"
	"fmt"
	"sort"
	"sync"
	"time"

	"github.com/Zonastery/bursar/golang/v2"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/metric"
	"go.opentelemetry.io/otel/trace"
)

const (
	operationCountName    = "bursar.operation.count"
	operationDurationName = "bursar.operation.duration"
)

// Options lets a host provide API providers and override the build-derived
// instrumentation version, primarily for composition and tests.
type Options struct {
	TracerProvider         trace.TracerProvider
	MeterProvider          metric.MeterProvider
	InstrumentationVersion string
}

// Instrumentation emits Bursar operation spans and metrics through
// host-configured OpenTelemetry API providers.
type Instrumentation struct {
	tracer            trace.Tracer
	operationCounter  metric.Int64Counter
	operationDuration metric.Float64Histogram
}

var _ bursar.Instrumentation = (*Instrumentation)(nil)

// New creates an API-only OpenTelemetry instrumentation adapter.
func New(options Options) (*Instrumentation, error) {
	tracerProvider := options.TracerProvider
	if tracerProvider == nil {
		tracerProvider = otel.GetTracerProvider()
	}
	meterProvider := options.MeterProvider
	if meterProvider == nil {
		meterProvider = otel.GetMeterProvider()
	}
	version := options.InstrumentationVersion
	if version == "" {
		version = bursar.InstrumentationVersion()
	}

	tracer := tracerProvider.Tracer(
		bursar.InstrumentationScope,
		trace.WithInstrumentationVersion(version),
	)
	meter := meterProvider.Meter(
		bursar.InstrumentationScope,
		metric.WithInstrumentationVersion(version),
	)
	counter, err := meter.Int64Counter(
		operationCountName,
		metric.WithDescription("Completed Bursar operations"),
		metric.WithUnit("{operation}"),
	)
	if err != nil {
		return nil, fmt.Errorf("create Bursar OpenTelemetry operation counter: %w", err)
	}
	duration, err := meter.Float64Histogram(
		operationDurationName,
		metric.WithDescription("Bursar operation duration"),
		metric.WithUnit("s"),
	)
	if err != nil {
		return nil, fmt.Errorf("create Bursar OpenTelemetry operation duration histogram: %w", err)
	}
	return &Instrumentation{
		tracer:            tracer,
		operationCounter:  counter,
		operationDuration: duration,
	}, nil
}

// Enable creates the adapter and selects it for subsequently constructed
// Bursar components. The returned idempotent function restores the prior
// registration without overwriting a newer one.
func Enable(options Options) (func(), error) {
	instrumentation, err := New(options)
	if err != nil {
		return nil, err
	}
	return bursar.SetDefaultInstrumentation(instrumentation), nil
}

// Start begins an operation span and returns a completion callback. Completion
// records only allowlisted attributes; raw error messages and exception events
// are deliberately omitted because they may contain SQL, identifiers, secrets,
// or provider payloads.
func (instrumentation *Instrumentation) Start(ctx context.Context, operation string, attributes bursar.TelemetryAttributes) (context.Context, func(error)) {
	baseAttributes := bursar.TelemetryOperationAttributes(operation, attributes)
	spanName := "bursar." + baseAttributes["bursar.operation"].(string)
	ctx, span := instrumentation.tracer.Start(
		ctx,
		spanName,
		trace.WithAttributes(openTelemetryAttributes(baseAttributes)...),
	)
	startedAt := time.Now()
	metricContext := context.WithoutCancel(ctx)

	var once sync.Once
	return ctx, func(operationErr error) {
		once.Do(func() {
			completed := cloneAttributes(baseAttributes)
			if operationErr == nil {
				completed["bursar.outcome"] = "success"
				span.SetStatus(codes.Ok, "")
			} else {
				completed["bursar.outcome"] = "error"
				for key, value := range bursar.TelemetryErrorAttributes(operationErr) {
					completed[key] = value
				}
				span.SetStatus(codes.Error, "")
			}
			safeAttributes := openTelemetryAttributes(completed)
			span.SetAttributes(safeAttributes...)
			instrumentation.operationCounter.Add(metricContext, 1, metric.WithAttributes(safeAttributes...))
			instrumentation.operationDuration.Record(
				metricContext,
				max(0, time.Since(startedAt).Seconds()),
				metric.WithAttributes(safeAttributes...),
			)
			span.End()
		})
	}
}

func cloneAttributes(attributes bursar.TelemetryAttributes) bursar.TelemetryAttributes {
	cloned := make(bursar.TelemetryAttributes, len(attributes)+3)
	for key, value := range attributes {
		cloned[key] = value
	}
	return cloned
}

func openTelemetryAttributes(attributes bursar.TelemetryAttributes) []attribute.KeyValue {
	sanitized := bursar.SanitizeTelemetryAttributes(attributes)
	keys := make([]string, 0, len(sanitized))
	for key := range sanitized {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	result := make([]attribute.KeyValue, 0, len(keys))
	for _, key := range keys {
		switch value := sanitized[key].(type) {
		case string:
			result = append(result, attribute.String(key, value))
		case bool:
			result = append(result, attribute.Bool(key, value))
		case int64:
			result = append(result, attribute.Int64(key, value))
		case float64:
			result = append(result, attribute.Float64(key, value))
		}
	}
	return result
}
