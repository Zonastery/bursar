// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

// Package postgres manages the pgx connection pool used by the bursar web service.
package postgres

import (
	"context"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

// Default pool sizing tuned for a small low-latency billing API.
const (
	DefaultMaxConns       = 20
	DefaultMinConns       = 2
	DefaultConnLifetime   = 30 * time.Minute
	DefaultConnIdleTime   = 5 * time.Minute
	DefaultHealthCheck    = 30 * time.Second
	DefaultConnectTimeout = 5 * time.Second
	pingTimeout           = 2 * time.Second
)

// NewPool parses dsn and opens a lazily-connecting pool with production
// defaults (bounded connections, limited lifetime, health checks, connect timeout).
// It does not dial the database; use Ping to probe connectivity.
func NewPool(ctx context.Context, dsn string) (*pgxpool.Pool, error) {
	cfg, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		return nil, fmt.Errorf("parse postgres config: %w", err)
	}

	cfg.MaxConns = DefaultMaxConns
	cfg.MinConns = DefaultMinConns
	cfg.MaxConnLifetime = DefaultConnLifetime
	cfg.MaxConnIdleTime = DefaultConnIdleTime
	cfg.HealthCheckPeriod = DefaultHealthCheck
	cfg.ConnConfig.ConnectTimeout = DefaultConnectTimeout

	pool, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		return nil, fmt.Errorf("create postgres pool: %w", err)
	}
	return pool, nil
}

// Ping probes database connectivity with a short deadline.
func Ping(ctx context.Context, pool *pgxpool.Pool) error {
	ctx, cancel := context.WithTimeout(ctx, pingTimeout)
	defer cancel()
	return pool.Ping(ctx)
}
