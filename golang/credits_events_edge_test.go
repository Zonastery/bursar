// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import (
	"context"
	"errors"
	"testing"
)

func TestCreditsQuotaEventsAndLowBalanceStateRemainFailureIsolated(t *testing.T) {
	threshold := 80.0
	store := &creditsSurfaceStore{quotaEvents: []QuotaEvent{
		{QuotaKey: "monthly", Operation: "completion", Measure: "tokens", EventType: "threshold", ThresholdPercent: &threshold, UsageChargeID: "usage-1", IdempotencyKey: "charge-1"},
		{QuotaKey: "monthly", Operation: "completion", Measure: "tokens", EventType: "blocked", UsageChargeID: "usage-2", IdempotencyKey: "charge-1"},
		{QuotaKey: "monthly", EventType: "future_event", IdempotencyKey: "charge-1"},
	}}
	emitter := NewCreditEventEmitter()
	var emitted []CreditEventType
	emitter.On(CreditEventQuotaThreshold, func(_ context.Context, event CreditEvent) { emitted = append(emitted, event.Type) })
	emitter.On(CreditEventQuotaBlocked, func(_ context.Context, event CreditEvent) { emitted = append(emitted, event.Type) })
	service, err := NewCreditsService(store, CreditsServiceOptions{
		EventSink: emitter,
		LowBalanceConfig: &LowBalanceConfig{
			Thresholds: []Amount{MustAmount("5"), MustAmount("10")},
		},
	})
	if err != nil {
		t.Fatal(err)
	}

	service.emitQuotaEvents(context.Background(), "account-1", "charge-1")
	if len(emitted) != 2 || emitted[0] != CreditEventQuotaThreshold || emitted[1] != CreditEventQuotaBlocked {
		t.Fatalf("quota events = %v", emitted)
	}
	service.emitQuotaEvents(context.Background(), "account-1", " ")
	store.quotaEventsErr = errors.New("history unavailable")
	service.emitQuotaEvents(context.Background(), "account-1", "charge-2")
	var nilService *CreditsService
	nilService.emitQuotaEvents(context.Background(), "account-1", "charge-3")
	if len(emitted) != 2 {
		t.Fatalf("failed quota reads emitted events: %v", emitted)
	}

	service.emitLowBalance(context.Background(), "account-1", MustAmount("20"), MustAmount("4"))
	if len(service.lowBalanceState["account-1"]) != 2 {
		t.Fatalf("low-balance state = %#v", service.lowBalanceState)
	}
	service.rearmLowBalance("missing", MustAmount("20"))
	service.rearmLowBalance("account-1", MustAmount("7"))
	if len(service.lowBalanceState["account-1"]) != 1 {
		t.Fatalf("partial rearm state = %#v", service.lowBalanceState["account-1"])
	}
	service.rearmLowBalance("account-1", MustAmount("20"))
	if _, exists := service.lowBalanceState["account-1"]; exists {
		t.Fatal("fully rearmed account retained low-balance state")
	}
	nilService.rearmLowBalance("account-1", MustAmount("20"))
	(&CreditsService{}).rearmLowBalance("account-1", MustAmount("20"))
}

func TestCreditsSmallFinancialBoundaryHelpers(t *testing.T) {
	if got := metricDimensionString(map[string]any{"model": " gpt-5 "}, "model"); got != "gpt-5" {
		t.Fatalf("model dimension = %q", got)
	}
	if got := metricDimensionString(map[string]any{"model": 5}, "model"); got != "" {
		t.Fatalf("non-string dimension = %q", got)
	}
	if _, err := scopedOperationKey("operation", " "); err == nil {
		t.Fatal("empty operation suffix was accepted")
	}
	if got := firstNonEmpty(" ", " value ", "fallback"); got != " value " {
		t.Fatalf("first non-empty value = %q", got)
	}
	if got := firstNonEmpty(" ", ""); got != "" {
		t.Fatalf("empty values produced %q", got)
	}
}
