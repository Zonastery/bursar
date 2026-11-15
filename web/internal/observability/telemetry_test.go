// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package observability

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetrichttp"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
)

// The tests below mutate process-global OpenTelemetry state; keep them
// sequential and ordered (no t.Parallel).

func discardLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

func TestShutdownNilReceiver(t *testing.T) {
	var telemetry *Telemetry
	if err := telemetry.Shutdown(context.Background()); err != nil {
		t.Errorf("Shutdown on nil receiver returned error: %v", err)
	}
}

func TestStartDisabledIsNoop(t *testing.T) {
	before := fmt.Sprintf("%p", otel.GetTracerProvider())

	telemetry, err := Start(context.Background(), TelemetryConfig{Enabled: false}, discardLogger())
	if err != nil {
		t.Fatalf("Start(disabled) returned error: %v", err)
	}
	if telemetry == nil {
		t.Fatal("Start(disabled) returned nil Telemetry")
	}
	if after := fmt.Sprintf("%p", otel.GetTracerProvider()); after != before {
		t.Errorf("Start(disabled) replaced the global tracer provider: before %s, after %s", before, after)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := telemetry.Shutdown(ctx); err != nil {
		t.Fatalf("Shutdown after disabled Start returned error: %v", err)
	}
}

func TestStartDisabledShutdownIdempotent(t *testing.T) {
	ctx := context.Background()

	first, err := Start(ctx, TelemetryConfig{}, discardLogger())
	if err != nil {
		t.Fatalf("first Start(disabled) returned error: %v", err)
	}
	second, err := Start(ctx, TelemetryConfig{}, discardLogger())
	if err != nil {
		t.Fatalf("second Start(disabled) returned error: %v", err)
	}

	for i := range 2 {
		if err := first.Shutdown(ctx); err != nil {
			t.Fatalf("Shutdown #%d returned error: %v", i+1, err)
		}
	}
	if err := second.Shutdown(ctx); err != nil {
		t.Fatalf("Shutdown of second instance returned error: %v", err)
	}
}

func TestStartEnabledNilLoggerFallsBackToDefault(t *testing.T) {
	sink := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.Copy(io.Discard, r.Body)
		w.WriteHeader(http.StatusOK)
	}))
	defer sink.Close()
	t.Setenv("OTEL_EXPORTER_OTLP_ENDPOINT", sink.URL)

	telemetry, err := Start(context.Background(), TelemetryConfig{Enabled: true}, nil)
	if err != nil {
		t.Fatalf("Start(enabled, nil logger) returned error: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := telemetry.Shutdown(ctx); err != nil {
		t.Errorf("Shutdown returned unexpected error: %v", err)
	}
}

func TestStartTraceExporterError(t *testing.T) {
	original := newTraceExporter
	newTraceExporter = func(context.Context, ...otlptracehttp.Option) (*otlptrace.Exporter, error) {
		return nil, errors.New("trace exporter boom")
	}
	defer func() { newTraceExporter = original }()

	_, err := Start(context.Background(), TelemetryConfig{Enabled: true}, discardLogger())
	if err == nil {
		t.Fatal("Start(enabled) with failing trace exporter succeeded, want error")
	}
	if !strings.Contains(err.Error(), "create OTLP trace exporter") {
		t.Errorf("error = %q, want it to mention trace exporter creation", err.Error())
	}
}

func TestStartMetricExporterError(t *testing.T) {
	sink := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.Copy(io.Discard, r.Body)
		w.WriteHeader(http.StatusOK)
	}))
	defer sink.Close()
	t.Setenv("OTEL_EXPORTER_OTLP_ENDPOINT", sink.URL)

	original := newMetricExporter
	newMetricExporter = func(context.Context, ...otlpmetrichttp.Option) (*otlpmetrichttp.Exporter, error) {
		return nil, errors.New("metric exporter boom")
	}
	defer func() { newMetricExporter = original }()

	_, err := Start(context.Background(), TelemetryConfig{Enabled: true}, discardLogger())
	if err == nil {
		t.Fatal("Start(enabled) with failing metric exporter succeeded, want error")
	}
	if !strings.Contains(err.Error(), "create OTLP metric exporter") {
		t.Errorf("error = %q, want it to mention metric exporter creation", err.Error())
	}
}

func TestShutdownWithCancelledContextJoinsErrors(t *testing.T) {
	sink := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.Copy(io.Discard, r.Body)
		w.WriteHeader(http.StatusOK)
	}))
	defer sink.Close()

	t.Setenv("OTEL_EXPORTER_OTLP_ENDPOINT", sink.URL)

	telemetry, err := Start(context.Background(), TelemetryConfig{
		Enabled:     true,
		ServiceName: "bursar-test",
	}, discardLogger())
	if err != nil {
		t.Fatalf("Start(enabled) returned error: %v", err)
	}

	cancelled, cancel := context.WithCancel(context.Background())
	cancel()
	err = telemetry.Shutdown(cancelled)
	if err == nil {
		t.Fatal("Shutdown(cancelled ctx) returned nil, want joined provider errors")
	}
	if !strings.Contains(err.Error(), "shutdown meter provider") || !strings.Contains(err.Error(), "shutdown tracer provider") {
		t.Errorf("error = %q, want it to mention both provider shutdowns", err.Error())
	}
}

func TestStartEnabledExportsOverOTLPHTTP(t *testing.T) {
	var posts atomic.Int32
	sink := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.Copy(io.Discard, r.Body)
		_ = r.Body.Close()
		if r.Method != http.MethodPost {
			t.Errorf("unexpected method %q for %q", r.Method, r.URL.Path)
		}
		posts.Add(1)
		w.WriteHeader(http.StatusOK)
	}))
	defer sink.Close()

	t.Setenv("OTEL_EXPORTER_OTLP_ENDPOINT", sink.URL)

	telemetry, err := Start(context.Background(), TelemetryConfig{
		Enabled:        true,
		ServiceName:    "bursar-test",
		ServiceVersion: "0.0.0-test",
		Environment:    "test",
	}, discardLogger())
	if err != nil {
		t.Fatalf("Start(enabled) returned error: %v", err)
	}
	if telemetry == nil {
		t.Fatal("Start(enabled) returned nil Telemetry")
	}

	_, span := otel.Tracer("observability-test").Start(context.Background(), "observability-test/span")
	span.End()
	counter, err := otel.Meter("observability-test").Int64Counter("observability.test.counter")
	if err != nil {
		t.Fatalf("create counter: %v", err)
	}
	counter.Add(context.Background(), 1)

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := telemetry.Shutdown(ctx); err != nil {
		t.Fatalf("Shutdown returned error: %v", err)
	}

	if got := posts.Load(); got == 0 {
		t.Error("OTLP sink received no POSTs; want at least one export")
	}
}
