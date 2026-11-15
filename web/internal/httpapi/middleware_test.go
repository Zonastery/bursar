// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package httpapi

import (
	"bytes"
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"
)

type captureHandler struct {
	mu      sync.Mutex
	level   slog.Leveler
	records []slog.Record
}

func newCaptureHandler(level slog.Leveler) *captureHandler {
	return &captureHandler{level: level}
}

func (h *captureHandler) Enabled(_ context.Context, l slog.Level) bool {
	return l >= h.level.Level()
}

func (h *captureHandler) Handle(_ context.Context, r slog.Record) error {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.records = append(h.records, r.Clone())
	return nil
}

func (h *captureHandler) WithAttrs([]slog.Attr) slog.Handler { return h }

func (h *captureHandler) WithGroup(string) slog.Handler { return h }

func (h *captureHandler) find(msg string) (slog.Record, bool) {
	h.mu.Lock()
	defer h.mu.Unlock()
	for _, r := range h.records {
		if r.Message == msg {
			return r, true
		}
	}
	return slog.Record{}, false
}

func attrValue(r slog.Record, key string) (slog.Value, bool) {
	var found slog.Value
	var ok bool
	r.Attrs(func(a slog.Attr) bool {
		if a.Key == key && !ok {
			found = a.Value
			ok = true
		}
		return !ok
	})
	return found, ok
}

func TestRequestLoggerRecordsAttributes(t *testing.T) {
	capture := newCaptureHandler(slog.LevelDebug)
	logger := slog.New(capture)

	r := chi.NewRouter()
	r.Use(middleware.RequestID)
	r.Use(RequestLogger(logger))
	r.Get("/dummy", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	})

	req := httptest.NewRequest(http.MethodGet, "/dummy", nil)
	rec := httptest.NewRecorder()
	r.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want %d", rec.Code, http.StatusOK)
	}

	record, ok := capture.find("http_request")
	if !ok {
		t.Fatal("expected an http_request log record")
	}
	if record.Level != slog.LevelInfo {
		t.Errorf("level = %v, want Info", record.Level)
	}
	if v, ok := attrValue(record, "http.method"); !ok || v.String() != http.MethodGet {
		t.Errorf("http.method = (%v, %v), want GET", v, ok)
	}
	if v, ok := attrValue(record, "http.status"); !ok || v.Int64() != http.StatusOK {
		t.Errorf("http.status = (%v, %v), want 200", v, ok)
	}
	if v, ok := attrValue(record, "request.id"); !ok || v.String() == "" {
		t.Errorf("request.id = (%v, %v), want non-empty", v, ok)
	}
	if v, ok := attrValue(record, "http.duration_ms"); !ok || v.Float64() < 0 {
		t.Errorf("http.duration_ms = (%v, %v), want >= 0", v, ok)
	}
	if v, ok := attrValue(record, "http.bytes"); !ok || v.Int64() < 0 {
		t.Errorf("http.bytes = (%v, %v), want >= 0", v, ok)
	}
	if v, ok := attrValue(record, "http.remote_addr"); !ok || v.String() == "" {
		t.Errorf("http.remote_addr = (%v, %v), want non-empty", v, ok)
	}
}

func TestRequestLoggerPlatformEndpointsAtDebug(t *testing.T) {
	infoCapture := newCaptureHandler(slog.LevelInfo)
	debugCapture := newCaptureHandler(slog.LevelDebug)

	newRouter := func(logger *slog.Logger) http.Handler {
		h := NewHandlers(nil, VersionInfo{}, logger)
		return Routes(h, 0)
	}

	req := httptest.NewRequest(http.MethodGet, "/healthz", nil)

	infoRecorder := httptest.NewRecorder()
	newRouter(slog.New(infoCapture)).ServeHTTP(infoRecorder, req)
	if _, found := infoCapture.find("http_request"); found {
		t.Error("/healthz should not be logged at Info level")
	}

	debugRecorder := httptest.NewRecorder()
	newRouter(slog.New(debugCapture)).ServeHTTP(debugRecorder, req)
	record, found := debugCapture.find("http_request")
	if !found {
		t.Fatal("/healthz should be logged at Debug level")
	}
	if record.Level != slog.LevelDebug {
		t.Errorf("level = %v, want Debug", record.Level)
	}
}

func TestRequestLoggerNilLoggerFallsBackToDefault(t *testing.T) {
	if RequestLogger(nil) == nil {
		t.Fatal("RequestLogger(nil) returned nil middleware")
	}
}

func TestRequestLoggerDefaultsZeroStatusToOK(t *testing.T) {
	var buf bytes.Buffer
	logger := slog.New(slog.NewJSONHandler(&buf, &slog.HandlerOptions{Level: slog.LevelDebug}))

	r := chi.NewRouter()
	r.Use(RequestLogger(logger))
	r.Get("/silent", func(w http.ResponseWriter, _ *http.Request) {})

	req := httptest.NewRequest(http.MethodGet, "/silent", nil)
	rec := httptest.NewRecorder()
	r.ServeHTTP(rec, req)

	var record struct {
		Status int `json:"http.status"`
	}
	if err := json.Unmarshal(buf.Bytes(), &record); err != nil {
		t.Fatalf("decode log record: %v", err)
	}
	if record.Status != http.StatusOK {
		t.Errorf("logged status = %d, want %d for a handler that never wrote", record.Status, http.StatusOK)
	}
}
