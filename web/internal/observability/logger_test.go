// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package observability

import (
	"context"
	"log/slog"
	"strings"
	"testing"
)

func TestNewLoggerValidLevel(t *testing.T) {
	ctx := context.Background()
	tests := []struct {
		name    string
		level   string
		wantOn  slog.Level
		wantOff slog.Level
	}{
		{name: "debug", level: "debug", wantOn: slog.LevelDebug, wantOff: slog.LevelDebug - 1},
		{name: "info", level: "info", wantOn: slog.LevelInfo, wantOff: slog.LevelDebug},
		{name: "warn", level: "warn", wantOn: slog.LevelWarn, wantOff: slog.LevelInfo},
		{name: "error", level: "error", wantOn: slog.LevelError, wantOff: slog.LevelWarn},
		{name: "uppercase", level: "ERROR", wantOn: slog.LevelError, wantOff: slog.LevelWarn},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			logger, err := NewLogger(LoggerConfig{Level: tt.level})
			if err != nil {
				t.Fatalf("NewLogger(%q) returned error: %v", tt.level, err)
			}
			if logger == nil {
				t.Fatal("NewLogger returned nil logger without error")
			}
			if !logger.Handler().Enabled(ctx, tt.wantOn) {
				t.Errorf("handler does not enable %v", tt.wantOn)
			}
			if logger.Handler().Enabled(ctx, tt.wantOff) {
				t.Errorf("handler unexpectedly enables %v", tt.wantOff)
			}
		})
	}
}

func TestNewLoggerInvalidLevel(t *testing.T) {
	logger, err := NewLogger(LoggerConfig{Level: "not-a-level"})
	if err == nil {
		t.Fatal("NewLogger with invalid level returned nil error")
	}
	if !strings.Contains(err.Error(), `parse log level "not-a-level"`) {
		t.Errorf("error = %q, want it to contain %q", err.Error(), `parse log level "not-a-level"`)
	}
	if logger != nil {
		t.Errorf("NewLogger with invalid level returned non-nil logger: %v", logger)
	}
}

func TestNewLoggerDevelopment(t *testing.T) {
	logger, err := NewLogger(LoggerConfig{Level: "info", Development: true})
	if err != nil {
		t.Fatalf("NewLogger returned error: %v", err)
	}
	if logger == nil {
		t.Fatal("NewLogger returned nil logger")
	}
	if !logger.Handler().Enabled(context.Background(), slog.LevelInfo) {
		t.Error("handler does not enable info level")
	}
}
