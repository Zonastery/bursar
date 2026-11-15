// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package observability

import (
	"context"
	"errors"
	"fmt"
	"log/slog"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetrichttp"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	"go.opentelemetry.io/otel/propagation"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	semconv "go.opentelemetry.io/otel/semconv/v1.43.0"
)

// TelemetryConfig describes the telemetry runtime.
type TelemetryConfig struct {
	// Enabled controls whether the OpenTelemetry SDK is started.
	Enabled bool
	// ServiceName is recorded on the telemetry resource.
	ServiceName string
	// ServiceVersion is recorded on the telemetry resource.
	ServiceVersion string
	// Environment is the deployment environment recorded on the resource.
	Environment string
}

// Telemetry owns process-wide OpenTelemetry SDK providers.
type Telemetry struct {
	tracerProvider *sdktrace.TracerProvider
	meterProvider  *sdkmetric.MeterProvider
}

// Exporter constructors are variables so tests can inject failures; they
// otherwise behave exactly like the library functions.
var (
	newTraceExporter  = otlptracehttp.New
	newMetricExporter = otlpmetrichttp.New
)

// Start initializes providers. When Enabled is false it returns a no-op
// instance without touching global state; otherwise it constructs OTLP/HTTP
// trace and metric exporters (configured through standard OTEL_* variables),
// registers global tracer/meter providers, installs a W3C propagator
// (TraceContext + Baggage), and routes internal OpenTelemetry SDK
// self-diagnostics errors into the provided logger.
func Start(ctx context.Context, cfg TelemetryConfig, logger *slog.Logger) (*Telemetry, error) {
	if !cfg.Enabled {
		return &Telemetry{}, nil
	}
	if logger == nil {
		logger = slog.Default()
	}

	res, err := resource.Merge(
		resource.Default(),
		resource.NewWithAttributes(
			semconv.SchemaURL,
			semconv.ServiceName(cfg.ServiceName),
			semconv.ServiceVersion(cfg.ServiceVersion),
			semconv.DeploymentEnvironmentNameKey.String(cfg.Environment),
		),
	)
	if err != nil {
		return nil, fmt.Errorf("build telemetry resource: %w", err)
	}

	traceExporter, err := newTraceExporter(ctx)
	if err != nil {
		return nil, fmt.Errorf("create OTLP trace exporter: %w", err)
	}
	tracerProvider := sdktrace.NewTracerProvider(
		sdktrace.WithBatcher(traceExporter),
		sdktrace.WithResource(res),
	)

	meterExporter, err := newMetricExporter(ctx)
	if err != nil {
		if shutdownErr := tracerProvider.Shutdown(context.WithoutCancel(ctx)); shutdownErr != nil {
			logger.Error("opentelemetry", "error", shutdownErr)
		}
		return nil, fmt.Errorf("create OTLP metric exporter: %w", err)
	}
	meterProvider := sdkmetric.NewMeterProvider(
		sdkmetric.WithReader(sdkmetric.NewPeriodicReader(meterExporter)),
		sdkmetric.WithResource(res),
	)

	otel.SetTracerProvider(tracerProvider)
	otel.SetMeterProvider(meterProvider)
	otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(
		propagation.TraceContext{},
		propagation.Baggage{},
	))
	otel.SetErrorHandler(otel.ErrorHandlerFunc(func(err error) {
		logger.Error("opentelemetry", "error", err)
	}))

	return &Telemetry{tracerProvider: tracerProvider, meterProvider: meterProvider}, nil
}

// Shutdown flushes and stops providers (meter first, then traces), joining errors.
func (t *Telemetry) Shutdown(ctx context.Context) error {
	if t == nil {
		return nil
	}
	var errs []error
	if t.meterProvider != nil {
		if err := t.meterProvider.Shutdown(ctx); err != nil {
			errs = append(errs, fmt.Errorf("shutdown meter provider: %w", err))
		}
		t.meterProvider = nil
	}
	if t.tracerProvider != nil {
		if err := t.tracerProvider.Shutdown(ctx); err != nil {
			errs = append(errs, fmt.Errorf("shutdown tracer provider: %w", err))
		}
		t.tracerProvider = nil
	}
	return errors.Join(errs...)
}
