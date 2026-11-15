// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package postgres

import (
	"context"
	"os"
	"strings"
	"testing"
	"time"
)

func TestNewPoolInvalidDSN(t *testing.T) {
	pool, err := NewPool(context.Background(), "://bad")
	if err == nil {
		if pool != nil {
			pool.Close()
		}
		t.Fatal("NewPool() error = nil, want error")
	}
	if !strings.Contains(err.Error(), "parse") {
		t.Fatalf("NewPool() error = %q, want message containing \"parse\"", err)
	}
}

func TestNewPoolUnreachableIsLazy(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()

	pool, err := NewPool(ctx, "postgres://bursar:bursar@127.0.0.1:1/bursar")
	if err != nil {
		t.Fatalf("NewPool() unexpected error for well-formed DSN: %v", err)
	}
	defer pool.Close()

	start := time.Now()
	if err := Ping(ctx, pool); err == nil {
		t.Fatal("Ping() error = nil, want connection error for unreachable host")
	}
	if elapsed := time.Since(start); elapsed >= 3*time.Second {
		t.Fatalf("Ping() took %s, want < 3s", elapsed)
	}
}

func TestNewPoolPingIntegration(t *testing.T) {
	dsn := os.Getenv("TEST_DATABASE_URL")
	if dsn == "" {
		t.Skip("set TEST_DATABASE_URL to run Postgres integration")
	}

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	pool, err := NewPool(ctx, dsn)
	if err != nil {
		t.Fatalf("NewPool() error = %v", err)
	}
	defer pool.Close()

	if err := Ping(ctx, pool); err != nil {
		t.Fatalf("Ping() error = %v", err)
	}
}
