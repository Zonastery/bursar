// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package app

import (
	"context"
	"encoding/json"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/Zonastery/bursar/web/internal/config"
)

func testConfig(addr string) config.Config {
	return config.Config{
		Env:               config.EnvDevelopment,
		Addr:              addr,
		LogLevel:          "error",
		ShutdownGrace:     2 * time.Second,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       10 * time.Second,
		WriteTimeout:      10 * time.Second,
		IdleTimeout:       30 * time.Second,
		HandlerTimeout:    5 * time.Second,
	}
}

func TestBuildWithoutDatabase(t *testing.T) {
	a, err := Build(context.Background(), testConfig("127.0.0.1:0"))
	if err != nil {
		t.Fatalf("Build() error = %v", err)
	}
	if a.server == nil {
		t.Fatal("Build() left server nil")
	}
	if a.pool != nil {
		t.Error("Build() created a pool without DATABASE_URL")
	}
	if a.server.Handler == nil {
		t.Error("Build() left handler nil")
	}
}

func TestRunWithCleanShutdown(t *testing.T) {
	a, err := Build(context.Background(), testConfig("127.0.0.1:0"))
	if err != nil {
		t.Fatalf("Build() error = %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	runErr := make(chan error, 1)
	go func() { runErr <- a.Run(ctx) }()

	time.Sleep(100 * time.Millisecond)
	cancel()

	select {
	case err := <-runErr:
		if err != nil {
			t.Fatalf("Run() error = %v, want nil on cancelled context", err)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("Run() did not return after context cancellation")
	}
}

func TestRunListenError(t *testing.T) {
	occupied, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("net.Listen() error = %v", err)
	}
	defer occupied.Close()

	a, err := Build(context.Background(), testConfig(occupied.Addr().String()))
	if err != nil {
		t.Fatalf("Build() error = %v", err)
	}
	err = a.Run(context.Background())
	if err == nil {
		t.Fatal("Run() succeeded on an occupied address, want error")
	}
	if !strings.Contains(err.Error(), "listen") {
		t.Errorf("error = %q, want it to mention listen failure", err.Error())
	}
}

func TestBuildInvalidLoggerLevel(t *testing.T) {
	cfg := testConfig("127.0.0.1:0")
	cfg.LogLevel = "bogus"
	_, err := Build(context.Background(), cfg)
	if err == nil {
		t.Fatal("Build() succeeded with invalid log level, want error")
	}
	if !strings.Contains(err.Error(), "create logger") {
		t.Errorf("error = %q, want it to mention logger creation", err.Error())
	}
}

func TestRunShutdownTimesOutOnActiveConnection(t *testing.T) {
	cfg := testConfig("127.0.0.1:0")
	cfg.ShutdownGrace = 200 * time.Millisecond
	a, err := Build(context.Background(), cfg)
	if err != nil {
		t.Fatalf("Build() error = %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	runErr := make(chan error, 1)
	go func() { runErr <- a.Run(ctx) }()

	if !waitForAddr(t, a) {
		t.Fatal("server never bound an address")
	}
	conn, err := net.Dial("tcp", a.Addr())
	if err != nil {
		t.Fatalf("net.Dial() error = %v", err)
	}
	defer conn.Close()
	// An unterminated request keeps the connection active so Shutdown must
	// wait for the grace period instead of draining instantly.
	if _, err := conn.Write([]byte("GET /healthz HTTP/1.1\r\nHost: bursar\r\n")); err != nil {
		t.Fatalf("write partial request: %v", err)
	}
	time.Sleep(100 * time.Millisecond)

	cancel()
	select {
	case err := <-runErr:
		if err == nil {
			t.Fatal("Run() returned nil despite an undrained connection, want error")
		}
		if !strings.Contains(err.Error(), "shutdown server") {
			t.Errorf("error = %q, want it to mention server shutdown failure", err.Error())
		}
	case <-time.After(15 * time.Second):
		t.Fatal("Run() did not return after cancellation")
	}
}

func TestRunReportsTelemetryFlushFailure(t *testing.T) {
	sink := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer sink.Close()
	t.Setenv("OTEL_EXPORTER_OTLP_ENDPOINT", sink.URL)

	cfg := testConfig("127.0.0.1:0")
	cfg.TelemetryEnabled = true
	cfg.ShutdownGrace = 500 * time.Millisecond
	a, err := Build(context.Background(), cfg)
	if err != nil {
		t.Fatalf("Build() error = %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	runErr := make(chan error, 1)
	go func() { runErr <- a.Run(ctx) }()

	if !waitForAddr(t, a) {
		t.Fatal("server never bound an address")
	}
	cancel()

	select {
	case err := <-runErr:
		if err == nil {
			t.Fatal("Run() returned nil despite failing telemetry export, want error")
		}
		if !strings.Contains(err.Error(), "shutdown telemetry") {
			t.Errorf("error = %q, want it to mention telemetry shutdown failure", err.Error())
		}
	case <-time.After(15 * time.Second):
		t.Fatal("Run() did not return after cancellation")
	}
}

// waitForAddr polls until the running App reports its bound address.
func waitForAddr(t *testing.T, a *App) bool {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if a.Addr() != "" {
			return true
		}
		time.Sleep(10 * time.Millisecond)
	}
	return false
}

func TestBuildInvalidDatabaseURL(t *testing.T) {
	cfg := testConfig("127.0.0.1:0")
	cfg.DatabaseURL = "://bad"
	_, err := Build(context.Background(), cfg)
	if err == nil {
		t.Fatal("Build() succeeded with invalid DATABASE_URL, want error")
	}
	if !strings.Contains(err.Error(), "parse postgres") {
		t.Errorf("error = %q, want it to contain parse failure", err.Error())
	}
}

func TestRunReadyzReportsUnavailableDatabase(t *testing.T) {
	cfg := testConfig("127.0.0.1:0")
	cfg.DatabaseURL = "postgres://bursar:bursar@127.0.0.1:1/bursar"
	a, err := Build(context.Background(), cfg)
	if err != nil {
		t.Fatalf("Build() error = %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	runErr := make(chan error, 1)
	go func() { runErr <- a.Run(ctx) }()

	t.Cleanup(cancel)
	var body struct {
		Status string `json:"status"`
	}
	readyzUnavailable(t, a, &body)
	if body.Status != "unavailable" {
		t.Errorf("readyz status = %q, want unavailable", body.Status)
	}

	cancel()
	select {
	case err := <-runErr:
		if err != nil {
			t.Fatalf("Run() error = %v, want nil on cancelled context", err)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("Run() did not return after context cancellation")
	}
}

// readyzUnavailable polls /readyz until the server answers with 503.
func readyzUnavailable(t *testing.T, a *App, out any) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		addr := a.Addr()
		if addr != "" {
			resp, err := http.Get("http://" + addr + "/readyz")
			if err == nil {
				defer resp.Body.Close()
				if resp.StatusCode == http.StatusServiceUnavailable {
					if err := json.NewDecoder(resp.Body).Decode(out); err != nil {
						t.Fatalf("decode readyz response: %v", err)
					}
					return
				}
			}
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatal("/readyz never reported 503 unavailable")
}

func TestAlwaysReady(t *testing.T) {
	if err := (alwaysReady{}).ReadinessCheck(context.Background()); err != nil {
		t.Errorf("ReadinessCheck() error = %v, want nil", err)
	}
}
