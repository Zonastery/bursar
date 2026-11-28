// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package runtime

import (
	"context"
	"errors"
	"testing"
	"time"

	bursar "github.com/Zonastery/bursar/golang/v2"
)

type usageBackendGuardStub struct {
	backend string
	err     error
}

func (s *usageBackendGuardStub) WithUsageBackend(_ context.Context, backend string, read func() error) error {
	s.backend = backend
	if s.err != nil {
		return s.err
	}
	return read()
}

func TestGuardedUsageDelegatesAnalyticsAndReceiptReadsUnderCutoverFence(t *testing.T) {
	t.Parallel()
	ctx := context.Background()
	start, end := time.Now().UTC().Add(-time.Hour), time.Now().UTC()
	guard := &usageBackendGuardStub{}
	usage := guardedUsage{store: guard, analytics: runtimeAnalyticsStub{}, charges: runtimeUsageStoreStub{}}

	if _, err := usage.SpendByUser(ctx, start, end); err != nil {
		t.Fatalf("SpendByUser() error = %v", err)
	}
	if _, err := usage.SpendByModel(ctx, start, end); err != nil {
		t.Fatalf("SpendByModel() error = %v", err)
	}
	if _, err := usage.TopUsers(ctx, 10, start, end); err != nil {
		t.Fatalf("TopUsers() error = %v", err)
	}
	if _, err := usage.DailySpend(ctx, start, end); err != nil {
		t.Fatalf("DailySpend() error = %v", err)
	}
	if _, err := usage.AggregateStats(ctx, start, end); err != nil {
		t.Fatalf("AggregateStats() error = %v", err)
	}
	if _, err := usage.ListUsageCharges(ctx, "user-1", bursar.ListUsageChargesOptions{}); err != nil {
		t.Fatalf("ListUsageCharges() error = %v", err)
	}
	if guard.backend != "clickhouse" {
		t.Fatalf("backend fence = %q, want clickhouse", guard.backend)
	}

	guard.err = errors.New("cutover fence unavailable")
	if _, err := usage.SpendByUser(ctx, start, end); err == nil || err.Error() != guard.err.Error() {
		t.Fatalf("SpendByUser() fence error = %v", err)
	}
}

func TestGuardedUsageRequiresReceiptHistoryPort(t *testing.T) {
	t.Parallel()
	usage := guardedUsage{store: &usageBackendGuardStub{}, analytics: runtimeAnalyticsStub{}}
	if _, err := usage.ListUsageCharges(context.Background(), "user-1", bursar.ListUsageChargesOptions{}); err == nil || err.Error() != "ClickHouse usage receipt history is not configured" {
		t.Fatalf("ListUsageCharges() without receipt port error = %v", err)
	}
}
