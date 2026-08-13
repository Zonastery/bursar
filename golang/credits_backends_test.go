// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import (
	"context"
	"testing"
	"time"
)

type analyticsStoreStub struct {
	CreditStore
	name string

	spendByUserCalls  int
	spendByModelCalls int
	topUsersCalls     int
	dailySpendCalls   int
	aggregateCalls    int
	usageCalls        int
}

var (
	_ UsageAnalyticsStore = (*analyticsStoreStub)(nil)
	_ UsageChargeStore    = (*analyticsStoreStub)(nil)
)

func (s *analyticsStoreStub) SpendByUser(context.Context, time.Time, time.Time) ([]SpendByUserRow, error) {
	s.spendByUserCalls++
	return []SpendByUserRow{{UserID: s.name, TotalSpend: MustAmount("3"), EntryCount: 1}}, nil
}

func (s *analyticsStoreStub) SpendByModel(context.Context, time.Time, time.Time) ([]SpendByModelRow, error) {
	s.spendByModelCalls++
	return []SpendByModelRow{{Model: s.name, TotalSpend: MustAmount("4"), EntryCount: 2}}, nil
}

func (s *analyticsStoreStub) TopUsers(context.Context, int, time.Time, time.Time) ([]TopUserRow, error) {
	s.topUsersCalls++
	return []TopUserRow{{UserID: s.name, TotalSpend: MustAmount("5")}}, nil
}

func (s *analyticsStoreStub) DailySpend(context.Context, time.Time, time.Time) ([]DailySpendRow, error) {
	s.dailySpendCalls++
	return []DailySpendRow{{Date: time.Unix(10, 0).UTC(), TotalSpend: MustAmount("6"), EntryCount: 3}}, nil
}

func (s *analyticsStoreStub) AggregateStats(context.Context, time.Time, time.Time) (AggregateStats, error) {
	s.aggregateCalls++
	return AggregateStats{TotalCreditsConsumed: MustAmount("7"), TopModel: s.name, TopUser: s.name}, nil
}

func (s *analyticsStoreStub) ListUsageCharges(context.Context, string, ListUsageChargesOptions) (UsageChargePage, error) {
	s.usageCalls++
	return UsageChargePage{Items: []UsageCharge{{UsageID: s.name}}}, nil
}

func TestCreditsServiceSelectsIndependentReadOnlyBackends(t *testing.T) {
	primary := &analyticsStoreStub{name: "postgres"}
	analytics := &analyticsStoreStub{name: "clickhouse"}
	usage := &analyticsStoreStub{name: "history"}
	service, err := NewCreditsService(primary, CreditsServiceOptions{Analytics: analytics, UsageStore: usage})
	if err != nil {
		t.Fatalf("NewCreditsService() error = %v", err)
	}
	ctx := context.Background()
	start, end := time.Unix(1, 0), time.Unix(2, 0)

	users, err := service.SpendByUser(ctx, start, end)
	if err != nil || len(users) != 1 || users[0].UserID != "clickhouse" {
		t.Fatalf("SpendByUser() = %#v, %v", users, err)
	}
	models, err := service.SpendByModel(ctx, start, end)
	if err != nil || len(models) != 1 || models[0].Model != "clickhouse" {
		t.Fatalf("SpendByModel() = %#v, %v", models, err)
	}
	top, err := service.TopUsers(ctx, 5, start, end)
	if err != nil || len(top) != 1 || top[0].UserID != "clickhouse" {
		t.Fatalf("TopUsers() = %#v, %v", top, err)
	}
	daily, err := service.DailySpend(ctx, start, end)
	if err != nil || len(daily) != 1 || !daily[0].TotalSpend.Equal(MustAmount("6")) {
		t.Fatalf("DailySpend() = %#v, %v", daily, err)
	}
	aggregate, err := service.AggregateStats(ctx, start, end)
	if err != nil || aggregate.TopModel != "clickhouse" {
		t.Fatalf("AggregateStats() = %#v, %v", aggregate, err)
	}
	page, err := service.ListUsageCharges(ctx, "user", ListUsageChargesOptions{})
	if err != nil || len(page.Items) != 1 || page.Items[0].UsageID != "history" {
		t.Fatalf("ListUsageCharges() = %#v, %v", page, err)
	}

	if analytics.spendByUserCalls != 1 || analytics.spendByModelCalls != 1 || analytics.topUsersCalls != 1 || analytics.dailySpendCalls != 1 || analytics.aggregateCalls != 1 {
		t.Fatalf("analytics calls = %#v", analytics)
	}
	if usage.usageCalls != 1 || primary.spendByUserCalls != 0 || primary.usageCalls != 0 {
		t.Fatalf("usage calls = %d, primary analytics/usage calls = %d/%d", usage.usageCalls, primary.spendByUserCalls, primary.usageCalls)
	}
}

func TestCreditsServiceDefaultsReadBackendsToCreditStore(t *testing.T) {
	primary := &analyticsStoreStub{name: "postgres"}
	service, err := NewCreditsService(primary, CreditsServiceOptions{})
	if err != nil {
		t.Fatalf("NewCreditsService() error = %v", err)
	}
	ctx := context.Background()
	start, end := time.Unix(1, 0), time.Unix(2, 0)
	users, err := service.SpendByUser(ctx, start, end)
	if err != nil || users[0].UserID != "postgres" {
		t.Fatalf("SpendByUser() = %#v, %v", users, err)
	}
	page, err := service.ListUsageCharges(ctx, "user", ListUsageChargesOptions{})
	if err != nil || page.Items[0].UsageID != "postgres" {
		t.Fatalf("ListUsageCharges() = %#v, %v", page, err)
	}
	if primary.spendByUserCalls != 1 || primary.usageCalls != 1 {
		t.Fatalf("primary calls = analytics %d, usage %d", primary.spendByUserCalls, primary.usageCalls)
	}
}

func TestCreditsServiceTreatsTypedNilReadBackendsAsOmitted(t *testing.T) {
	primary := &analyticsStoreStub{name: "postgres"}
	var typedNil *analyticsStoreStub
	service, err := NewCreditsService(primary, CreditsServiceOptions{Analytics: typedNil, UsageStore: typedNil})
	if err != nil {
		t.Fatalf("NewCreditsService() error = %v", err)
	}
	users, err := service.SpendByUser(context.Background(), time.Unix(1, 0), time.Unix(2, 0))
	if err != nil || len(users) != 1 || users[0].UserID != "postgres" {
		t.Fatalf("SpendByUser() = %#v, %v", users, err)
	}
	page, err := service.ListUsageCharges(context.Background(), "user", ListUsageChargesOptions{})
	if err != nil || len(page.Items) != 1 || page.Items[0].UsageID != "postgres" {
		t.Fatalf("ListUsageCharges() = %#v, %v", page, err)
	}
}
