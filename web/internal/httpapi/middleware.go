// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package httpapi

import (
	"log/slog"
	"net/http"
	"time"

	"github.com/go-chi/chi/v5/middleware"
)

func platformEndpoint(path string) bool {
	switch path {
	case "/healthz", "/readyz", "/version":
		return true
	default:
		return false
	}
}

// RequestLogger returns middleware emitting one structured slog record per
// request with msg "http_request" and attributes http.method, http.path,
// http.status, http.bytes, http.duration_ms, request.id, and http.remote_addr.
// Platform endpoints (/healthz, /readyz, /version) are logged at Debug level;
// everything else is logged at Info.
func RequestLogger(logger *slog.Logger) func(http.Handler) http.Handler {
	if logger == nil {
		logger = slog.Default()
	}
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			start := time.Now()
			ww := middleware.NewWrapResponseWriter(w, r.ProtoMajor)
			if id := middleware.GetReqID(r.Context()); id != "" {
				ww.Header().Set("X-Request-Id", id)
			}
			defer func() {
				level := slog.LevelInfo
				if platformEndpoint(r.URL.Path) {
					level = slog.LevelDebug
				}
				status := ww.Status()
				if status == 0 {
					status = http.StatusOK
				}
				logger.Log(r.Context(), level, "http_request",
					slog.String("http.method", r.Method),
					slog.String("http.path", r.URL.Path),
					slog.Int("http.status", status),
					slog.Int("http.bytes", ww.BytesWritten()),
					slog.Float64("http.duration_ms", float64(time.Since(start))/float64(time.Millisecond)),
					slog.String("request.id", middleware.GetReqID(r.Context())),
					slog.String("http.remote_addr", r.RemoteAddr),
				)
			}()
			next.ServeHTTP(ww, r)
		})
	}
}
