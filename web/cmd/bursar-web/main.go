// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

// Command bursar-web serves the Bursar billing HTTP API.
package main

import (
	"context"
	"fmt"
	"os"
	"os/signal"
	"syscall"

	"github.com/Zonastery/bursar/web/internal/app"
	"github.com/Zonastery/bursar/web/internal/config"
)

func main() {
	if err := run(); err != nil {
		fmt.Fprintf(os.Stderr, "bursar-web: %v\n", err)
		os.Exit(1)
	}
}

func run() error {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	cfg, err := config.Parse(os.Environ())
	if err != nil {
		return err
	}
	application, err := app.Build(ctx, cfg)
	if err != nil {
		return err
	}
	return application.Run(ctx)
}
