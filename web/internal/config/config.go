// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

// Package config loads service configuration from the process environment.
package config

import (
	"errors"
	"fmt"
	"log/slog"
	"os"
	"strings"
	"time"

	env "github.com/caarlos0/env/v11"
)

// Env selects the deployment environment.
type Env string

const (
	// EnvDevelopment enables development affordances (source in log records).
	EnvDevelopment Env = "development"
	// EnvProduction is the hardened deployment mode.
	EnvProduction Env = "production"
)

// Config is the full runtime configuration of the web service.
type Config struct {
	Env              Env    `env:"BURSAR_WEB_ENV" envDefault:"development"`
	Addr             string `env:"BURSAR_WEB_ADDR" envDefault:":8080"`
	LogLevel         string `env:"BURSAR_WEB_LOG_LEVEL" envDefault:"info"`
	TelemetryEnabled bool   `env:"BURSAR_WEB_TELEMETRY_ENABLED" envDefault:"false"`
	DatabaseURL      string `env:"DATABASE_URL"`

	ShutdownGrace     time.Duration `env:"BURSAR_WEB_SHUTDOWN_GRACE" envDefault:"30s"`
	ReadHeaderTimeout time.Duration `env:"BURSAR_WEB_READ_HEADER_TIMEOUT" envDefault:"10s"`
	ReadTimeout       time.Duration `env:"BURSAR_WEB_READ_TIMEOUT" envDefault:"30s"`
	WriteTimeout      time.Duration `env:"BURSAR_WEB_WRITE_TIMEOUT" envDefault:"60s"`
	IdleTimeout       time.Duration `env:"BURSAR_WEB_IDLE_TIMEOUT" envDefault:"120s"`
	HandlerTimeout    time.Duration `env:"BURSAR_WEB_HANDLER_TIMEOUT" envDefault:"30s"`
}

// Parse reads configuration from an os.Environ-style KEY=VALUE list,
// defaulting to the process environment, and validates the result.
func Parse(environ []string) (Config, error) {
	if environ == nil {
		environ = os.Environ()
	}
	environment := make(map[string]string, len(environ))
	for _, entry := range environ {
		if key, value, ok := strings.Cut(entry, "="); ok {
			environment[key] = value
		}
	}
	cfg := Config{}
	if err := env.ParseWithOptions(&cfg, env.Options{Environment: environment}); err != nil {
		return Config{}, fmt.Errorf("parse environment: %w", err)
	}
	if err := cfg.Validate(); err != nil {
		return Config{}, err
	}
	return cfg, nil
}

// Validate reports misconfigured values, joining every violation found.
func (c Config) Validate() error {
	var errs []error
	switch c.Env {
	case EnvDevelopment, EnvProduction:
	default:
		errs = append(errs, fmt.Errorf("invalid BURSAR_WEB_ENV %q (want %q or %q)", c.Env, EnvDevelopment, EnvProduction))
	}
	var level slog.Level
	if err := level.UnmarshalText([]byte(c.LogLevel)); err != nil {
		errs = append(errs, fmt.Errorf("invalid BURSAR_WEB_LOG_LEVEL %q", c.LogLevel))
	}
	if c.Addr == "" {
		errs = append(errs, errors.New("BURSAR_WEB_ADDR must not be empty"))
	}
	durations := []struct {
		name  string
		value time.Duration
	}{
		{"BURSAR_WEB_SHUTDOWN_GRACE", c.ShutdownGrace},
		{"BURSAR_WEB_READ_HEADER_TIMEOUT", c.ReadHeaderTimeout},
		{"BURSAR_WEB_READ_TIMEOUT", c.ReadTimeout},
		{"BURSAR_WEB_WRITE_TIMEOUT", c.WriteTimeout},
		{"BURSAR_WEB_IDLE_TIMEOUT", c.IdleTimeout},
		{"BURSAR_WEB_HANDLER_TIMEOUT", c.HandlerTimeout},
	}
	for _, d := range durations {
		if d.value <= 0 {
			errs = append(errs, fmt.Errorf("%s must be positive, got %s", d.name, d.value))
		}
	}
	return errors.Join(errs...)
}

// Development reports whether the service runs in the development environment.
func (c Config) Development() bool { return c.Env == EnvDevelopment }

// Production reports whether the service runs in the production environment.
func (c Config) Production() bool { return c.Env == EnvProduction }
