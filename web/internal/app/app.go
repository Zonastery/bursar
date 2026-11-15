// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

// Package app composes configuration, observability, storage, and the HTTP
// server into a single runnable service with graceful lifecycle management.
package app

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"sync/atomic"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/Zonastery/bursar/web/internal/config"
	"github.com/Zonastery/bursar/web/internal/httpapi"
	"github.com/Zonastery/bursar/web/internal/observability"
	"github.com/Zonastery/bursar/web/internal/postgres"
	"github.com/Zonastery/bursar/web/internal/version"
)

const serviceName = "bursar-web"

// App owns every long-lived resource of the service.
type App struct {
	cfg       config.Config
	logger    *slog.Logger
	telemetry *observability.Telemetry
	pool      *pgxpool.Pool
	server    *http.Server
	boundAddr atomic.Value // string, set by Run once the listener is bound
}

// Build constructs the fully wired application: logger, OpenTelemetry
// providers, the optional Postgres pool backing readiness probes, and the
// HTTP server.
func Build(ctx context.Context, cfg config.Config) (*App, error) {
	logger, err := observability.NewLogger(observability.LoggerConfig{
		Level:       cfg.LogLevel,
		Development: cfg.Development(),
	})
	if err != nil {
		return nil, fmt.Errorf("create logger: %w", err)
	}
	slog.SetDefault(logger)

	telemetry, err := observability.Start(ctx, observability.TelemetryConfig{
		Enabled:        cfg.TelemetryEnabled,
		ServiceName:    serviceName,
		ServiceVersion: version.Version,
		Environment:    string(cfg.Env),
	}, logger)
	if err != nil {
		return nil, fmt.Errorf("start telemetry: %w", err)
	}

	a := &App{cfg: cfg, logger: logger, telemetry: telemetry}

	var checker httpapi.ReadinessChecker = alwaysReady{}
	if cfg.DatabaseURL != "" {
		pool, err := postgres.NewPool(ctx, cfg.DatabaseURL)
		if err != nil {
			return nil, errors.Join(err, telemetry.Shutdown(context.WithoutCancel(ctx)))
		}
		a.pool = pool
		checker = poolReadiness{pool: pool}
	}

	a.server = &http.Server{
		Addr: cfg.Addr,
		Handler: httpapi.Routes(httpapi.NewHandlers(checker, httpapi.VersionInfo{
			Version: version.Version,
			Commit:  version.Commit,
			Date:    version.Date,
		}, logger), cfg.HandlerTimeout),
		ReadHeaderTimeout: cfg.ReadHeaderTimeout,
		ReadTimeout:       cfg.ReadTimeout,
		WriteTimeout:      cfg.WriteTimeout,
		IdleTimeout:       cfg.IdleTimeout,
	}
	return a, nil
}

// Run serves traffic until ctx is cancelled or the listener fails, then
// drains connections, closes the database pool, flushes telemetry, and
// returns the first fatal error encountered (nil for a clean shutdown).
func (a *App) Run(ctx context.Context) error {
	listener, err := net.Listen("tcp", a.cfg.Addr)
	if err != nil {
		return fmt.Errorf("listen on %s: %w", a.cfg.Addr, err)
	}
	a.boundAddr.Store(listener.Addr().String())
	a.logger.Info("listening",
		slog.String("addr", listener.Addr().String()),
		slog.String("env", string(a.cfg.Env)),
		slog.String("version", version.Version),
	)

	serveErr := make(chan error, 1)
	go func() { serveErr <- a.server.Serve(listener) }()

	var runErr error
	select {
	case err := <-serveErr:
		if !errors.Is(err, http.ErrServerClosed) {
			runErr = err
		}
	case <-ctx.Done():
	}

	shutdownCtx, cancel := context.WithTimeout(context.Background(), a.cfg.ShutdownGrace)
	defer cancel()
	if err := a.server.Shutdown(shutdownCtx); err != nil && runErr == nil {
		runErr = fmt.Errorf("shutdown server: %w", err)
	}
	if a.pool != nil {
		a.pool.Close()
	}
	if err := a.telemetry.Shutdown(shutdownCtx); err != nil && runErr == nil {
		runErr = fmt.Errorf("shutdown telemetry: %w", err)
	}
	if runErr == nil {
		a.logger.Info("shutdown complete")
	} else {
		a.logger.Error("shutdown with error", slog.Any("error", runErr))
	}
	return runErr
}

// Addr reports the bound listener address after Run starts serving, or an
// empty string before that. Useful for tests and introspection with
// ephemeral ports.
func (a *App) Addr() string {
	addr, _ := a.boundAddr.Load().(string)
	return addr
}

type alwaysReady struct{}

func (alwaysReady) ReadinessCheck(context.Context) error { return nil }

type poolReadiness struct {
	pool *pgxpool.Pool
}

func (p poolReadiness) ReadinessCheck(ctx context.Context) error {
	return postgres.Ping(ctx, p.pool)
}
