package bursar

import "testing"

func TestProviderReceiptValidatesAndClonesPricingInputs(t *testing.T) {
	t.Parallel()

	receipt := ProviderReceipt{
		Metrics: UsageMetrics{
			Operation:  "completion",
			Measures:   map[string]Amount{"tokens": MustAmount("12")},
			Dimensions: map[string]any{"model": "gpt-5"},
			Metadata:   map[string]any{"request_id": "provider-1"},
		},
		Metadata: CreditMetadata{"trace_id": "trace-1"},
	}
	if err := receipt.Validate(); err != nil {
		t.Fatalf("Validate() error = %v", err)
	}
	clone := receipt.Clone()
	receipt.Metrics.Measures["tokens"] = MustAmount("99")
	receipt.Metrics.Dimensions["model"] = "changed"
	receipt.Metrics.Metadata["request_id"] = "changed"
	receipt.Metadata["trace_id"] = "changed"
	if !clone.Metrics.Measures["tokens"].Equal(MustAmount("12")) || clone.Metrics.Dimensions["model"] != "gpt-5" || clone.Metrics.Metadata["request_id"] != "provider-1" || clone.Metadata["trace_id"] != "trace-1" {
		t.Fatalf("Clone() retained mutable input: %#v", clone)
	}
}

func TestProviderReceiptRejectsInvalidMetrics(t *testing.T) {
	t.Parallel()

	if err := (ProviderReceipt{}).Validate(); err == nil {
		t.Fatal("Validate() accepted an empty provider receipt")
	}
}
