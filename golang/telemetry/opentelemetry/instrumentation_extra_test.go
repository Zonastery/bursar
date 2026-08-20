// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package opentelemetry

import (
	"errors"
	"testing"

	"go.opentelemetry.io/otel/metric"
)

type failingMeterProvider struct {
	metric.MeterProvider
	meter metric.Meter
}

func (p failingMeterProvider) Meter(string, ...metric.MeterOption) metric.Meter { return p.meter }

type failingMeter struct {
	metric.Meter
	counterErr   error
	histogramErr error
}

func (m failingMeter) Int64Counter(string, ...metric.Int64CounterOption) (metric.Int64Counter, error) {
	return nil, m.counterErr
}

func (m failingMeter) Float64Histogram(string, ...metric.Float64HistogramOption) (metric.Float64Histogram, error) {
	return nil, m.histogramErr
}

func TestInstrumentationConstructorAndAttributeEdgeCases(t *testing.T) {
	t.Parallel()
	counterErr := errors.New("counter unavailable")
	if _, err := New(Options{MeterProvider: failingMeterProvider{meter: failingMeter{counterErr: counterErr}}}); err == nil || !errors.Is(err, counterErr) {
		t.Fatalf("counter constructor error = %v", err)
	}
	histogramErr := errors.New("histogram unavailable")
	if _, err := New(Options{MeterProvider: failingMeterProvider{meter: failingMeter{histogramErr: histogramErr}}}); err == nil || !errors.Is(err, histogramErr) {
		t.Fatalf("histogram constructor error = %v", err)
	}
	if _, err := New(Options{}); err != nil {
		t.Fatalf("default provider constructor error = %v", err)
	}

	attributes := openTelemetryAttributes(map[string]any{
		"bursar.operation": "value",
		"bursar.outcome":   true,
		"bursar.backend":   int64(2),
		"bursar.provider":  1.5,
		"ignored":          struct{}{},
	})
	if len(attributes) != 4 {
		t.Fatalf("converted attributes = %#v", attributes)
	}
	cloned := cloneAttributes(map[string]any{"key": "value"})
	cloned["key"] = "changed"
	if cloned["key"] != "changed" {
		t.Fatal("clone was not mutable")
	}

}
