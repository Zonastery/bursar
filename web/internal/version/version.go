// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

// Package version carries build metadata injected at link time.
package version

// Build metadata, overridden via -ldflags "-X github.com/Zonastery/bursar/web/internal/version.Key=Value".
var (
	Version = "dev"
	Commit  = "unknown"
	Date    = "unknown"
)
