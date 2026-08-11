package bursar

import "github.com/shopspring/decimal"

// CostBreakdown is the exact, JSON-safe cost report for a usage event. Total
// is always recomputed from the components rather than accepted from callers.
type CostBreakdown struct {
	ModelCredits     decimal.Decimal `json:"modelCredits"`
	ToolCredits      decimal.Decimal `json:"toolCredits"`
	SearchCredits    decimal.Decimal `json:"searchCredits"`
	CacheSavings     decimal.Decimal `json:"cacheSavings"`
	FixedCredits     decimal.Decimal `json:"fixedCredits"`
	OperationCredits decimal.Decimal `json:"operationCredits"`
	Total            decimal.Decimal `json:"total"`
	Breakdown        map[string]any  `json:"breakdown"`
}

// CostBreakdownInput omits Total intentionally; MakeCostBreakdown derives it
// using Bursar's 6dp HALF_UP accounting semantics.
type CostBreakdownInput struct {
	ModelCredits     decimal.Decimal `json:"modelCredits"`
	ToolCredits      decimal.Decimal `json:"toolCredits"`
	SearchCredits    decimal.Decimal `json:"searchCredits"`
	CacheSavings     decimal.Decimal `json:"cacheSavings"`
	FixedCredits     decimal.Decimal `json:"fixedCredits"`
	OperationCredits decimal.Decimal `json:"operationCredits"`
	Breakdown        map[string]any  `json:"breakdown"`
}

// MakeCostBreakdown quantizes components, clamps the resulting total at zero,
// and returns a detached breakdown map.
func MakeCostBreakdown(input CostBreakdownInput) CostBreakdown {
	modelCredits := QuantizeMoney(input.ModelCredits)
	toolCredits := QuantizeMoney(input.ToolCredits)
	searchCredits := QuantizeMoney(input.SearchCredits)
	cacheSavings := QuantizeMoney(input.CacheSavings)
	fixedCredits := QuantizeMoney(input.FixedCredits)
	operationCredits := QuantizeMoney(input.OperationCredits)
	total := modelCredits.
		Add(toolCredits).
		Add(searchCredits).
		Add(fixedCredits).
		Add(operationCredits).
		Add(cacheSavings)
	if total.IsNegative() {
		total = decimal.Zero
	}
	return CostBreakdown{
		ModelCredits:     modelCredits,
		ToolCredits:      toolCredits,
		SearchCredits:    searchCredits,
		CacheSavings:     cacheSavings,
		FixedCredits:     fixedCredits,
		OperationCredits: operationCredits,
		Total:            QuantizeMoney(total),
		Breakdown:        cloneAnyMap(input.Breakdown),
	}
}
