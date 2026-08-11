package bursar

import "testing"

func TestUsageMetricsRejectsFloatDimensions(t *testing.T) {
	err := (UsageMetrics{
		Operation:  "completion",
		Dimensions: map[string]any{"temperature": 0.5},
	}).Validate()
	if err == nil {
		t.Fatal("float64 dimension accepted")
	}
	if err := (UsageMetrics{Operation: "completion", Dimensions: map[string]any{"temperature": MustAmount("0.5")}}).Validate(); err != nil {
		t.Fatalf("decimal dimension rejected: %v", err)
	}
}
