package bursar

import "testing"

func TestAmountParsesOnlyCanonicalDecimalStringsAndQuantizesHalfUp(t *testing.T) {
	amount, err := NewAmount("1.2345675")
	if err != nil {
		t.Fatalf("NewAmount: %v", err)
	}
	if got := QuantizeMoney(amount).StringFixed(MoneyDecimalPlaces); got != "1.234568" {
		t.Fatalf("quantized = %s", got)
	}
	for _, input := range []string{"1e3", "+1", "01", ".5"} {
		if _, err := NewAmount(input); err == nil {
			t.Fatalf("NewAmount(%q) accepted a non-canonical value", input)
		}
	}
}
