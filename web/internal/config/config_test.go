// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package config

import (
	"strings"
	"testing"
	"time"
)

func TestParseDefaults(t *testing.T) {
	cfg, err := Parse(nil)
	if err != nil {
		t.Fatalf("Parse() error = %v", err)
	}
	if cfg.Env != EnvDevelopment {
		t.Errorf("Env = %q, want %q", cfg.Env, EnvDevelopment)
	}
	if cfg.Addr != ":8080" {
		t.Errorf("Addr = %q, want :8080", cfg.Addr)
	}
	if cfg.LogLevel != "info" {
		t.Errorf("LogLevel = %q, want info", cfg.LogLevel)
	}
	if cfg.TelemetryEnabled {
		t.Error("TelemetryEnabled = true, want false")
	}
	if cfg.DatabaseURL != "" {
		t.Errorf("DatabaseURL = %q, want empty", cfg.DatabaseURL)
	}
	want := map[string]time.Duration{
		"ShutdownGrace":     30 * time.Second,
		"ReadHeaderTimeout": 10 * time.Second,
		"ReadTimeout":       30 * time.Second,
		"WriteTimeout":      60 * time.Second,
		"IdleTimeout":       120 * time.Second,
		"HandlerTimeout":    30 * time.Second,
	}
	got := map[string]time.Duration{
		"ShutdownGrace":     cfg.ShutdownGrace,
		"ReadHeaderTimeout": cfg.ReadHeaderTimeout,
		"ReadTimeout":       cfg.ReadTimeout,
		"WriteTimeout":      cfg.WriteTimeout,
		"IdleTimeout":       cfg.IdleTimeout,
		"HandlerTimeout":    cfg.HandlerTimeout,
	}
	for name := range want {
		if got[name] != want[name] {
			t.Errorf("%s = %s, want %s", name, got[name], want[name])
		}
	}
	if !cfg.Development() || cfg.Production() {
		t.Errorf("Development() = %v, Production() = %v for default config", cfg.Development(), cfg.Production())
	}
}

func TestParseOverrides(t *testing.T) {
	environ := []string{
		"BURSAR_WEB_ENV=production",
		"BURSAR_WEB_ADDR=127.0.0.1:9090",
		"BURSAR_WEB_LOG_LEVEL=debug",
		"BURSAR_WEB_TELEMETRY_ENABLED=true",
		"DATABASE_URL=postgres://bursar@localhost/bursar",
		"BURSAR_WEB_SHUTDOWN_GRACE=45s",
		"BURSAR_WEB_READ_HEADER_TIMEOUT=2s",
		"BURSAR_WEB_HANDLER_TIMEOUT=15s",
	}
	cfg, err := Parse(environ)
	if err != nil {
		t.Fatalf("Parse() error = %v", err)
	}
	if cfg.Env != EnvProduction || !cfg.Production() {
		t.Errorf("Env = %q, want %q", cfg.Env, EnvProduction)
	}
	if cfg.Addr != "127.0.0.1:9090" {
		t.Errorf("Addr = %q, want 127.0.0.1:9090", cfg.Addr)
	}
	if cfg.LogLevel != "debug" {
		t.Errorf("LogLevel = %q, want debug", cfg.LogLevel)
	}
	if !cfg.TelemetryEnabled {
		t.Error("TelemetryEnabled = false, want true")
	}
	if cfg.DatabaseURL != "postgres://bursar@localhost/bursar" {
		t.Errorf("DatabaseURL = %q, want postgres://bursar@localhost/bursar", cfg.DatabaseURL)
	}
	if cfg.ShutdownGrace != 45*time.Second {
		t.Errorf("ShutdownGrace = %s, want 45s", cfg.ShutdownGrace)
	}
	if cfg.ReadHeaderTimeout != 2*time.Second {
		t.Errorf("ReadHeaderTimeout = %s, want 2s", cfg.ReadHeaderTimeout)
	}
	if cfg.HandlerTimeout != 15*time.Second {
		t.Errorf("HandlerTimeout = %s, want 15s", cfg.HandlerTimeout)
	}
}

func TestValidateRejectsEmptyAddr(t *testing.T) {
	cfg := Config{Env: EnvDevelopment, LogLevel: "info"}
	cfg.ReadHeaderTimeout = time.Second
	err := cfg.Validate()
	if err == nil {
		t.Fatal("Validate() with empty Addr succeeded, want error")
	}
	if !strings.Contains(err.Error(), "must not be empty") {
		t.Errorf("error = %q, want it to mention empty addr", err.Error())
	}
}

func TestParseNilLookupUsesProcessEnvironment(t *testing.T) {
	t.Setenv("BURSAR_WEB_LOG_LEVEL", "warn")
	cfg, err := Parse(nil)
	if err != nil {
		t.Fatalf("Parse(nil) error = %v", err)
	}
	if cfg.LogLevel != "warn" {
		t.Errorf("LogLevel = %q, want warn from process environment", cfg.LogLevel)
	}
}

func TestParseValidationErrors(t *testing.T) {
	cases := []struct {
		name    string
		environ []string
		message string
	}{
		{"invalid env", []string{"BURSAR_WEB_ENV=staging"}, `invalid BURSAR_WEB_ENV "staging"`},
		{"invalid log level", []string{"BURSAR_WEB_LOG_LEVEL=verbose"}, `invalid BURSAR_WEB_LOG_LEVEL "verbose"`},
		{"negative grace", []string{"BURSAR_WEB_SHUTDOWN_GRACE=-1s"}, "BURSAR_WEB_SHUTDOWN_GRACE must be positive"},
		{"zero timeout", []string{"BURSAR_WEB_WRITE_TIMEOUT=0s"}, "BURSAR_WEB_WRITE_TIMEOUT must be positive"},
		{"bad duration", []string{"BURSAR_WEB_IDLE_TIMEOUT=soon"}, "parse environment"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := Parse(tc.environ)
			if err == nil {
				t.Fatalf("Parse(%v) succeeded, want error", tc.environ)
			}
			if !strings.Contains(err.Error(), tc.message) {
				t.Errorf("error = %q, want it to contain %q", err.Error(), tc.message)
			}
		})
	}
}
