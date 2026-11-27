package runtime

import (
	"context"
	"errors"
	"time"

	bursar "github.com/Zonastery/bursar/golang/v2"
)

type usageBackendGuard interface {
	WithUsageBackend(context.Context, string, func() error) error
}

type guardedUsage struct {
	store     usageBackendGuard
	analytics bursar.UsageAnalyticsStore
	charges   bursar.UsageChargeStore
}

func (g guardedUsage) SpendByUser(ctx context.Context, start, end time.Time) (result []bursar.SpendByUserRow, err error) {
	err = g.store.WithUsageBackend(ctx, "clickhouse", func() error { var e error; result, e = g.analytics.SpendByUser(ctx, start, end); return e })
	return
}
func (g guardedUsage) SpendByModel(ctx context.Context, start, end time.Time) (result []bursar.SpendByModelRow, err error) {
	err = g.store.WithUsageBackend(ctx, "clickhouse", func() error { var e error; result, e = g.analytics.SpendByModel(ctx, start, end); return e })
	return
}
func (g guardedUsage) TopUsers(ctx context.Context, limit int, start, end time.Time) (result []bursar.TopUserRow, err error) {
	err = g.store.WithUsageBackend(ctx, "clickhouse", func() error { var e error; result, e = g.analytics.TopUsers(ctx, limit, start, end); return e })
	return
}
func (g guardedUsage) DailySpend(ctx context.Context, start, end time.Time) (result []bursar.DailySpendRow, err error) {
	err = g.store.WithUsageBackend(ctx, "clickhouse", func() error { var e error; result, e = g.analytics.DailySpend(ctx, start, end); return e })
	return
}
func (g guardedUsage) AggregateStats(ctx context.Context, start, end time.Time) (result bursar.AggregateStats, err error) {
	err = g.store.WithUsageBackend(ctx, "clickhouse", func() error { var e error; result, e = g.analytics.AggregateStats(ctx, start, end); return e })
	return
}
func (g guardedUsage) ListUsageCharges(ctx context.Context, user string, options bursar.ListUsageChargesOptions) (result bursar.UsageChargePage, err error) {
	err = g.store.WithUsageBackend(ctx, "clickhouse", func() error {
		if g.charges == nil {
			return errors.New("ClickHouse usage receipt history is not configured")
		}
		var e error
		result, e = g.charges.ListUsageCharges(ctx, user, options)
		return e
	})
	return
}
