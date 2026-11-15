// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package httpapi

import (
	"net/http"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"
	"go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp"
)

// Routes builds the fully wired HTTP handler: a chi router with RequestID,
// RequestLogger, and Recoverer middleware (in that order), plus
// middleware.Timeout when handlerTimeout is positive; the platform routes
// GET /healthz, GET /readyz, GET /version; JSON 404 and 405 responses; and
// the whole chain wrapped with otelhttp instrumentation.
//
// Client address resolution is deliberately left to the trusted reverse
// proxy in front of the service: chi's RealIP middleware is deprecated
// because it trusts spoofable X-Forwarded-For style headers.
func Routes(h *Handlers, handlerTimeout time.Duration) http.Handler {
	r := chi.NewRouter()
	r.Use(middleware.RequestID)
	r.Use(RequestLogger(h.logger))
	if handlerTimeout > 0 {
		r.Use(middleware.Timeout(handlerTimeout))
	}
	r.Use(middleware.Recoverer)

	r.Get("/healthz", h.Liveness)
	r.Get("/readyz", h.Readiness)
	r.Get("/version", h.Version)

	r.NotFound(http.HandlerFunc(notFound))
	r.MethodNotAllowed(http.HandlerFunc(methodNotAllowed))

	return otelhttp.NewHandler(r, "bursar-web")
}

func notFound(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusNotFound, statusPayload("not_found"))
}

func methodNotAllowed(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusMethodNotAllowed, statusPayload("method_not_allowed"))
}
