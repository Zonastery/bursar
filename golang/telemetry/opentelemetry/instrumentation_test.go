// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package opentelemetry

import (
	"context"
	"encoding/json"
	"errors"
	"reflect"
	"strings"
	"testing"

	"github.com/Zonastery/bursar/golang/v2"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/metric"
	"go.opentelemetry.io/otel/trace"
)

type recordingTracerProvider struct {
	trace.TracerProvider
	name    string
	version string
	tracer  *recordingTracer
}

func (provider *recordingTracerProvider) Tracer(name string, options ...trace.TracerOption) trace.Tracer {
	provider.name = name
	config := trace.NewTracerConfig(options...)
	provider.version = config.InstrumentationVersion()
	if provider.tracer == nil {
		provider.tracer = &recordingTracer{}
	}
	return provider.tracer
}

type recordingTracer struct {
	trace.Tracer
	spans []*recordingSpan
}

func (tracer *recordingTracer) Start(ctx context.Context, name string, options ...trace.SpanStartOption) (context.Context, trace.Span) {
	config := trace.NewSpanStartConfig(options...)
	span := &recordingSpan{name: name, attributes: keyValues(config.Attributes())}
	tracer.spans = append(tracer.spans, span)
	return ctx, span
}

type recordingSpan struct {
	trace.Span
	name              string
	attributes        map[string]any
	status            codes.Code
	statusDescription string
	endCount          int
	recordErrorCount  int
}

func (span *recordingSpan) SetAttributes(attributes ...attribute.KeyValue) {
	for key, value := range keyValues(attributes) {
		span.attributes[key] = value
	}
}

func (span *recordingSpan) SetStatus(code codes.Code, description string) {
	span.status = code
	span.statusDescription = description
}

func (span *recordingSpan) RecordError(error, ...trace.EventOption) {
	span.recordErrorCount++
}

func (span *recordingSpan) End(...trace.SpanEndOption) {
	span.endCount++
}

type metricRecord struct {
	value      float64
	attributes map[string]any
}

type recordingMeterProvider struct {
	metric.MeterProvider
	name    string
	version string
	meter   *recordingMeter
}

func (provider *recordingMeterProvider) Meter(name string, options ...metric.MeterOption) metric.Meter {
	provider.name = name
	config := metric.NewMeterConfig(options...)
	provider.version = config.InstrumentationVersion()
	if provider.meter == nil {
		provider.meter = &recordingMeter{}
	}
	return provider.meter
}

type recordingMeter struct {
	metric.Meter
	counterName   string
	histogramName string
	counter       *recordingCounter
	histogram     *recordingHistogram
}

func (meter *recordingMeter) Int64Counter(name string, _ ...metric.Int64CounterOption) (metric.Int64Counter, error) {
	meter.counterName = name
	if meter.counter == nil {
		meter.counter = &recordingCounter{}
	}
	return meter.counter, nil
}

func (meter *recordingMeter) Float64Histogram(name string, _ ...metric.Float64HistogramOption) (metric.Float64Histogram, error) {
	meter.histogramName = name
	if meter.histogram == nil {
		meter.histogram = &recordingHistogram{}
	}
	return meter.histogram, nil
}

type recordingCounter struct {
	metric.Int64Counter
	records []metricRecord
}

func (counter *recordingCounter) Add(_ context.Context, value int64, options ...metric.AddOption) {
	config := metric.NewAddConfig(options)
	attributes := config.Attributes()
	counter.records = append(counter.records, metricRecord{value: float64(value), attributes: keyValues(attributes.ToSlice())})
}

type recordingHistogram struct {
	metric.Float64Histogram
	records []metricRecord
}

func (histogram *recordingHistogram) Record(_ context.Context, value float64, options ...metric.RecordOption) {
	config := metric.NewRecordConfig(options)
	attributes := config.Attributes()
	histogram.records = append(histogram.records, metricRecord{value: value, attributes: keyValues(attributes.ToSlice())})
}

func keyValues(values []attribute.KeyValue) map[string]any {
	result := make(map[string]any, len(values))
	for _, item := range values {
		result[string(item.Key)] = item.Value.AsInterface()
	}
	return result
}

func TestInstrumentationUsesAPIProvidersAndRecordsSafeError(t *testing.T) {
	traces := &recordingTracerProvider{}
	metrics := &recordingMeterProvider{}
	instrumentation, err := New(Options{
		TracerProvider:         traces,
		MeterProvider:          metrics,
		InstrumentationVersion: "v-test",
	})
	if err != nil {
		t.Fatalf("New() error = %v", err)
	}
	if traces.name != bursar.InstrumentationScope || metrics.name != bursar.InstrumentationScope {
		t.Fatalf("scope = trace %q / metric %q, want %q", traces.name, metrics.name, bursar.InstrumentationScope)
	}
	if traces.version != "v-test" || metrics.version != "v-test" {
		t.Fatalf("version = trace %q / metric %q, want v-test", traces.version, metrics.version)
	}
	if metrics.meter.counterName != operationCountName || metrics.meter.histogramName != operationDurationName {
		t.Fatalf("metric names = %q/%q", metrics.meter.counterName, metrics.meter.histogramName)
	}

	_, finish := instrumentation.Start(context.Background(), "CreditsGrant", bursar.TelemetryAttributes{
		"bursar.backend": "Postgres",
		"secret":         "sk_live_do_not_record",
	})
	operationErr := bursar.NewStoreUnavailableError("password=do-not-record", errors.New("postgresql://secret"))
	finish(operationErr)
	finish(nil)

	if len(traces.tracer.spans) != 1 {
		t.Fatalf("spans = %d, want 1", len(traces.tracer.spans))
	}
	span := traces.tracer.spans[0]
	if span.name != "bursar.credits_grant" {
		t.Fatalf("span name = %q, want bursar.credits_grant", span.name)
	}
	wantAttributes := map[string]any{
		"bursar.operation": "credits_grant",
		"bursar.backend":   "postgres",
		"bursar.outcome":   "error",
		"error.type":       "bursar_error",
		"error.code":       "store_unavailable",
	}
	if !reflect.DeepEqual(span.attributes, wantAttributes) {
		t.Fatalf("span attributes = %#v, want %#v", span.attributes, wantAttributes)
	}
	if span.status != codes.Error || span.statusDescription != "" {
		t.Fatalf("span status = %v/%q, want Error with no description", span.status, span.statusDescription)
	}
	if span.recordErrorCount != 0 {
		t.Fatalf("RecordError calls = %d, raw exception events must be omitted", span.recordErrorCount)
	}
	if span.endCount != 1 {
		t.Fatalf("span End calls = %d, want idempotent completion", span.endCount)
	}
	assertSafeMetricRecords(t, metrics.meter.counter.records, wantAttributes)
	assertSafeMetricRecords(t, metrics.meter.histogram.records, wantAttributes)
	if metrics.meter.counter.records[0].value != 1 {
		t.Fatalf("operation count = %v, want 1", metrics.meter.counter.records[0].value)
	}
	if metrics.meter.histogram.records[0].value < 0 {
		t.Fatalf("operation duration = %v, want non-negative", metrics.meter.histogram.records[0].value)
	}
}

func TestInstrumentationRecordsSuccessAndEnableRestoresDefault(t *testing.T) {
	traces := &recordingTracerProvider{}
	metrics := &recordingMeterProvider{}
	restore, err := Enable(Options{TracerProvider: traces, MeterProvider: metrics, InstrumentationVersion: "v-test"})
	if err != nil {
		t.Fatalf("Enable() error = %v", err)
	}
	defer restore()
	instrumentation, ok := bursar.DefaultInstrumentation().(*Instrumentation)
	if !ok {
		t.Fatalf("default instrumentation = %T, want OpenTelemetry adapter", bursar.DefaultInstrumentation())
	}

	_, finish := instrumentation.Start(context.Background(), "credits.reserve", nil)
	finish(nil)
	span := traces.tracer.spans[0]
	if span.status != codes.Ok || span.attributes["bursar.outcome"] != "success" {
		t.Fatalf("success span status/attributes = %v/%#v", span.status, span.attributes)
	}
	restore()
	if _, ok := bursar.DefaultInstrumentation().(bursar.NoopInstrumentation); !ok {
		t.Fatalf("default after restore = %T, want no-op", bursar.DefaultInstrumentation())
	}
}

func assertSafeMetricRecords(t *testing.T, records []metricRecord, wantAttributes map[string]any) {
	t.Helper()
	if len(records) != 1 {
		t.Fatalf("metric records = %d, want 1", len(records))
	}
	if !reflect.DeepEqual(records[0].attributes, wantAttributes) {
		t.Fatalf("metric attributes = %#v, want %#v", records[0].attributes, wantAttributes)
	}
	encoded, err := json.Marshal(records[0].attributes)
	if err != nil {
		t.Fatalf("json.Marshal() error = %v", err)
	}
	text := strings.ToLower(string(encoded))
	for _, secret := range []string{"password", "postgresql", "sk_live", "do-not-record"} {
		if strings.Contains(text, secret) {
			t.Fatalf("metric attributes leaked %q: %s", secret, encoded)
		}
	}
}
