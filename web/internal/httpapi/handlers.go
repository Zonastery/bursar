// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

// Package httpapi provides HTTP routing, handlers, and middleware for the
// bursar web service.
package httpapi

import (
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
)

// ReadinessChecker reports whether backing dependencies are ready.
type ReadinessChecker interface {
	ReadinessCheck(ctx context.Context) error
}

// VersionInfo carries build metadata surfaced by the version endpoint.
type VersionInfo struct {
	Version string `json:"version"`
	Commit  string `json:"commit"`
	Date    string `json:"date"`
}

// Handlers serves the platform endpoints (health, readiness, version).
type Handlers struct {
	checker ReadinessChecker
	version VersionInfo
	logger  *slog.Logger
}

// NewHandlers constructs Handlers. A nil checker is treated as always ready.
func NewHandlers(checker ReadinessChecker, version VersionInfo, logger *slog.Logger) *Handlers {
	if logger == nil {
		logger = slog.Default()
	}
	return &Handlers{checker: checker, version: version, logger: logger}
}

// Liveness reports that the process is up and able to serve requests.
func (h *Handlers) Liveness(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, statusPayload("ok"))
}

// Readiness reports whether backing dependencies are ready to serve traffic.
func (h *Handlers) Readiness(w http.ResponseWriter, r *http.Request) {
	if h.checker != nil {
		if err := h.checker.ReadinessCheck(r.Context()); err != nil {
			h.logger.ErrorContext(r.Context(), "readiness check failed", slog.Any("error", err))
			writeJSON(w, http.StatusServiceUnavailable, statusPayload("unavailable"))
			return
		}
	}
	writeJSON(w, http.StatusOK, statusPayload("ok"))
}

// Version returns the build metadata for the running binary.
func (h *Handlers) Version(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, h.version)
}

func statusPayload(status string) map[string]string {
	return map[string]string{"status": status}
}

func writeJSON(w http.ResponseWriter, statusCode int, payload any) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(statusCode)
	_ = json.NewEncoder(w).Encode(payload)
}
