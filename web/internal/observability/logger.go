// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

// Package observability provides structured logging and OpenTelemetry bootstrap.
package observability

import (
	"fmt"
	"log/slog"
	"os"
)

// LoggerConfig configures NewLogger.
type LoggerConfig struct {
	// Level is the slog level name: debug|info|warn|error.
	Level string
	// Development adds source location to records.
	Development bool
}

// NewLogger returns a JSON slog.Logger writing to os.Stdout at the configured
// level, and an error if the level name is not recognized.
func NewLogger(cfg LoggerConfig) (*slog.Logger, error) {
	var lvl slog.Level
	if err := lvl.UnmarshalText([]byte(cfg.Level)); err != nil {
		return nil, fmt.Errorf("parse log level %q: %w", cfg.Level, err)
	}
	return slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{
		Level:     lvl,
		AddSource: cfg.Development,
	})), nil
}
