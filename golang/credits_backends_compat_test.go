// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar_test

import (
	bursar "github.com/Zonastery/bursar/golang/v2"
	clickhousestore "github.com/Zonastery/bursar/golang/v2/storage/clickhouse"
)

// Keep the optional production adapter structurally compatible with both
// independently selectable CreditsService read ports.
var (
	_ bursar.UsageAnalyticsStore = (*clickhousestore.ClickHouseUsageStore)(nil)
	_ bursar.UsageChargeStore    = (*clickhousestore.ClickHouseUsageStore)(nil)
)
