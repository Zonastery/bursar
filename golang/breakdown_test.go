package bursar

import "testing"

func TestMakeCostBreakdownQuantizesAndClampsTotal(t *testing.T) {
	breakdown := MakeCostBreakdown(CostBreakdownInput{
		OperationCredits: MustAmount("1.0000004"),
		CacheSavings:     MustAmount("-2"),
	})
	if got := breakdown.OperationCredits.StringFixed(MoneyDecimalPlaces); got != "1.000000" {
		t.Fatalf("operation credits = %s", got)
	}
	if got := breakdown.Total.StringFixed(MoneyDecimalPlaces); got != "0.000000" {
		t.Fatalf("total = %s", got)
	}
}
