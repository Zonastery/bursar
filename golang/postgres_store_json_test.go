// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import (
	"encoding/json"
	"testing"
)

func TestAmountMapPreservesExactJSONDecimalNumbers(t *testing.T) {
	amounts, err := amountMap([]byte(`{"purchased":1.230000,"fractional":0.000001}`), "bucket_breakdown")
	if err != nil {
		t.Fatalf("amountMap() error = %v", err)
	}
	if got := amounts["purchased"]; !got.Equal(MustAmount("1.230000")) {
		t.Fatalf("purchased amount = %s, want 1.230000", got)
	}
	if got := amounts["fractional"]; !got.Equal(MustAmount("0.000001")) {
		t.Fatalf("fractional amount = %s, want 0.000001", got)
	}
}

func TestParseAmountAcceptsJSONNumberWithoutFloatConversion(t *testing.T) {
	var number json.Number
	if err := json.Unmarshal([]byte(`1.230000`), &number); err != nil {
		t.Fatalf("decode JSON number: %v", err)
	}
	got, err := parseAmount(number, "bucket_breakdown.purchased")
	if err != nil {
		t.Fatalf("parseAmount() error = %v", err)
	}
	if !got.Equal(MustAmount("1.230000")) {
		t.Fatalf("parsed amount = %s, want 1.230000", got)
	}
}

func TestAmountMapAcceptsPgxDecodedJSONNumbers(t *testing.T) {
	amounts, err := amountMap(map[string]any{"purchased": float64(1.230001)}, "bucket_breakdown")
	if err != nil {
		t.Fatalf("amountMap() error = %v", err)
	}
	if got := amounts["purchased"]; !got.Equal(MustAmount("1.230001")) {
		t.Fatalf("purchased amount = %s, want 1.230001", got)
	}
}
