package bursar

import (
	"strings"
	"testing"
)

func engineWithFlatPrice(amount string) *PricingEngine {
	charge := Charge{Type: "flat", Amount: MustAmount(amount)}
	return &PricingEngine{config: &BursarConfig{
		Version: 1,
		Pricing: &PricingConfig{
			Operations: map[string]OperationDefinition{
				"usage": {
					Measures: map[string]MeasureDefinition{"units": {Unit: "unit"}},
					Dimensions: map[string]DimensionDefinition{
						"model":    {Type: "string"},
						"priority": {Type: "boolean"},
						"score":    {Type: "number"},
					},
				},
			},
			RateCards: map[string]RateCard{
				"standard": {Operations: map[string]OperationPricing{
					"usage": {Unmatched: UnmatchedPolicy{Action: "charge", Charge: &charge}},
				}},
			},
		},
	}}
}

func TestPricingEngineConstructorsAndDetachedSchema(t *testing.T) {
	raw := map[string]any{
		"version": 1,
		"pricing": map[string]any{
			"operations": map[string]any{
				"usage": map[string]any{
					"measures":   map[string]any{"units": map[string]any{"unit": "unit"}},
					"dimensions": map[string]any{},
				},
			},
			"rate_cards": map[string]any{
				"standard": map[string]any{"operations": map[string]any{
					"usage": map[string]any{
						"rules": []any{},
						"unmatched": map[string]any{"action": "charge", "charge": map[string]any{
							"type": "flat", "amount": "1",
						}},
					},
				}},
			},
		},
		"credits": map[string]any{},
	}
	for name, constructor := range map[string]func(map[string]any) (*PricingEngine, error){
		"map":  NewPricingEngineFromMap,
		"dict": NewPricingEngineFromDict,
	} {
		t.Run(name, func(t *testing.T) {
			engine, err := constructor(raw)
			if err != nil {
				t.Fatalf("constructor error = %v", err)
			}
			if engine.Config() == nil || engine.PricingSchema()["version"] != 1 {
				t.Fatalf("constructor returned incomplete engine: %#v", engine)
			}
		})
	}
	if _, err := NewPricingEngine(nil); err == nil {
		t.Fatal("NewPricingEngine(nil) succeeded")
	}
	if _, err := NewPricingEngineFromMap(map[string]any{}); err == nil {
		t.Fatal("NewPricingEngineFromMap accepted an empty catalog")
	}
	var nilEngine *PricingEngine
	if nilEngine.Config() != nil || nilEngine.PricingSchema() != nil {
		t.Fatal("nil engine accessors returned data")
	}
	if (&PricingEngine{}).PricingSchema() != nil {
		t.Fatal("unconfigured engine returned a pricing schema")
	}
}

func TestPricingEngineBatchAndPlanRateCardLookup(t *testing.T) {
	engine := engineWithFlatPrice("1.25")
	card := "standard"
	engine.config.Plans = map[string]PlanDefinition{
		"pro":      {RateCard: &card},
		"unpriced": {},
	}
	if got, ok := engine.GetRateCardForPlan("pro"); !ok || got != card {
		t.Fatalf("GetRateCardForPlan(pro) = %q, %v", got, ok)
	}
	for _, planID := range []string{"", "missing", "unpriced"} {
		if _, ok := engine.GetRateCardForPlan(planID); ok {
			t.Fatalf("GetRateCardForPlan(%q) unexpectedly succeeded", planID)
		}
	}
	var nilEngine *PricingEngine
	if _, ok := nilEngine.GetRateCardForPlan("pro"); ok {
		t.Fatal("nil engine returned a plan rate card")
	}

	items := []UsageMetrics{
		{Operation: "usage", Measures: map[string]Amount{"units": MustAmount("1")}},
		{Operation: "usage", Measures: map[string]Amount{"units": MustAmount("2")}},
	}
	results, err := engine.CalculateBatch(items)
	if err != nil || len(results) != len(items) {
		t.Fatalf("CalculateBatch() = %d results, %v", len(results), err)
	}
	if _, err := engine.CalculateBatch(items, PricingOptions{}, PricingOptions{}); err == nil {
		t.Fatal("CalculateBatch accepted multiple option values")
	}
	items[1].Operation = "missing"
	if results, err := engine.CalculateBatch(items); err == nil || results != nil {
		t.Fatalf("CalculateBatch invalid item = %#v, %v; want nil error result", results, err)
	}
}

func TestPricingEngineRejectsInvalidCatalogAndUsageBoundaries(t *testing.T) {
	validMetrics := UsageMetrics{Operation: "usage", Measures: map[string]Amount{"units": MustAmount("1")}}
	var nilEngine *PricingEngine
	if _, err := nilEngine.Calculate(validMetrics); err == nil {
		t.Fatal("nil engine calculated usage")
	}
	if _, err := (&PricingEngine{}).Calculate(validMetrics); err == nil {
		t.Fatal("unconfigured engine calculated usage")
	}
	engine := engineWithFlatPrice("1")
	if _, err := engine.Calculate(validMetrics, PricingOptions{}, PricingOptions{}); err == nil {
		t.Fatal("Calculate accepted multiple option values")
	}

	tests := []struct {
		name    string
		metrics UsageMetrics
		options []PricingOptions
	}{
		{"empty operation", UsageMetrics{}, nil},
		{"unknown operation", UsageMetrics{Operation: "missing"}, nil},
		{"undeclared measure", UsageMetrics{Operation: "usage", Measures: map[string]Amount{"other": MustAmount("1")}}, nil},
		{"undeclared dimension", UsageMetrics{Operation: "usage", Dimensions: map[string]any{"other": "x"}}, nil},
		{"unknown rate card", validMetrics, []PricingOptions{{RateCard: "missing"}}},
	}
	for _, testCase := range tests {
		t.Run(testCase.name, func(t *testing.T) {
			if _, err := engine.Calculate(testCase.metrics, testCase.options...); err == nil {
				t.Fatal("Calculate succeeded; want error")
			}
		})
	}

	required := engineWithFlatPrice("1")
	required.config.Pricing.Operations["usage"] = OperationDefinition{
		Measures: map[string]MeasureDefinition{"units": {Unit: "unit"}},
		Dimensions: map[string]DimensionDefinition{
			"model": {Type: "string", Required: true},
		},
	}
	if _, err := required.Calculate(validMetrics); err == nil {
		t.Fatal("Calculate accepted a missing required dimension")
	}

	multipleCards := engineWithFlatPrice("1")
	multipleCards.config.Pricing.RateCards["premium"] = RateCard{}
	if _, err := multipleCards.Calculate(validMetrics); err == nil {
		t.Fatal("Calculate omitted rate card with multiple configured cards")
	}

	noPricing := engineWithFlatPrice("1")
	noPricing.config.Pricing = nil
	if _, err := noPricing.Calculate(validMetrics); err == nil {
		t.Fatal("Calculate accepted a catalog without usage pricing")
	}

	noMatch := engineWithFlatPrice("1")
	noMatch.config.Pricing.RateCards["standard"] = RateCard{Operations: map[string]OperationPricing{
		"usage": {
			Rules: []PriceRule{{When: map[string]DimensionMatcher{
				"model": {Op: "eq", Value: "premium"},
			}, Charge: Charge{Type: "flat", Amount: MustAmount("1")}}},
			Unmatched: UnmatchedPolicy{Action: "deny"},
		},
	}}
	if _, err := noMatch.Calculate(validMetrics); err == nil {
		t.Fatal("Calculate charged usage without a matching rule")
	}

	negative := engineWithFlatPrice("-1")
	if _, err := negative.Calculate(validMetrics); err == nil {
		t.Fatal("Calculate accepted a negative computed price")
	}
}

func TestPricingEngineDimensionMatchers(t *testing.T) {
	two := MustAmount("2")
	five := MustAmount("5")
	eight := MustAmount("8")
	tests := []struct {
		name    string
		matcher DimensionMatcher
		value   MatcherScalar
		want    bool
	}{
		{"equal string", DimensionMatcher{Op: "eq", Value: "gpt"}, "gpt", true},
		{"equal bool", DimensionMatcher{Op: "eq", Value: true}, true, true},
		{"equal decimal", DimensionMatcher{Op: "eq", Value: five}, MustAmount("5.0"), true},
		{"different scalar types", DimensionMatcher{Op: "eq", Value: "5"}, five, false},
		{"in", DimensionMatcher{Op: "in", Values: []MatcherScalar{"a", "b"}}, "b", true},
		{"not in", DimensionMatcher{Op: "not_in", Values: []MatcherScalar{"a", "b"}}, "c", true},
		{"not in rejected", DimensionMatcher{Op: "not_in", Values: []MatcherScalar{"a", "b"}}, "a", false},
		{"prefix", DimensionMatcher{Op: "prefix", Value: "gpt-"}, "gpt-5", true},
		{"prefix wrong type", DimensionMatcher{Op: "prefix", Value: "gpt-"}, five, false},
		{"range", DimensionMatcher{Op: "range", GT: &two, GTE: &five, LT: &eight, LTE: &five}, five, true},
		{"range lower failure", DimensionMatcher{Op: "range", GT: &five}, five, false},
		{"range upper failure", DimensionMatcher{Op: "range", LT: &five}, five, false},
		{"range wrong type", DimensionMatcher{Op: "range", GTE: &two}, "5", false},
		{"unknown matcher", DimensionMatcher{Op: "regex", Value: "x"}, "x", false},
	}
	for _, testCase := range tests {
		t.Run(testCase.name, func(t *testing.T) {
			if got := matchesDimension(testCase.matcher, testCase.value); got != testCase.want {
				t.Fatalf("matchesDimension() = %v, want %v", got, testCase.want)
			}
		})
	}
	if equalMatcherScalars(1, 1) {
		t.Fatal("equalMatcherScalars accepted unsupported scalar types")
	}
}

func TestPricingEngineChargeDefensiveBoundaries(t *testing.T) {
	engine := engineWithFlatPrice("1")
	measures := map[string]Amount{"units": MustAmount("5")}
	tests := []struct {
		name    string
		charge  Charge
		want    string
		wantErr string
	}{
		{"package floor", Charge{Type: "package", Measure: "units", Units: MustAmount("2"), Amount: MustAmount("3"), Rounding: "floor"}, "6", ""},
		{"package nearest", Charge{Type: "package", Measure: "units", Units: MustAmount("2"), Amount: MustAmount("3"), Rounding: "nearest"}, "9", ""},
		{"missing measure", Charge{Type: "per_unit", Measure: "missing", UnitSize: MustAmount("1"), Rate: MustAmount("1")}, "", "missing usage measure"},
		{"zero unit size", Charge{Type: "per_unit", Measure: "units", UnitSize: DecimalZero, Rate: MustAmount("1")}, "", "division by zero"},
		{"package zero units", Charge{Type: "package", Measure: "units", Units: DecimalZero, Amount: MustAmount("1"), Rounding: "ceil"}, "", "division by zero"},
		{"unsupported rounding", Charge{Type: "package", Measure: "units", Units: MustAmount("1"), Amount: MustAmount("1"), Rounding: "bankers"}, "", "unsupported package rounding"},
		{"volume empty tiers", Charge{Type: "volume", Measure: "units"}, "", "at least one tier"},
		{"volume first tier", Charge{Type: "volume", Measure: "units", Tiers: []GraduatedTier{{UpTo: amountPointer(MustAmount("5")), Rate: MustAmount("2")}, {Rate: MustAmount("3")}}}, "10", ""},
		{"sum component error", Charge{Type: "sum", Components: []Charge{{Type: "flat", Amount: MustAmount("1")}, {Type: "unknown"}}}, "", "unsupported charge type"},
		{"expression undefined measure", Charge{Type: "expression", Formula: "missing + 1"}, "", "undefined variable"},
		{"unsupported charge", Charge{Type: "unknown"}, "", "unsupported charge type"},
	}
	for _, testCase := range tests {
		t.Run(testCase.name, func(t *testing.T) {
			result, err := engine.evaluateCharge(testCase.charge, measures)
			if testCase.wantErr != "" {
				if err == nil || !strings.Contains(err.Error(), testCase.wantErr) {
					t.Fatalf("evaluateCharge() error = %v, want containing %q", err, testCase.wantErr)
				}
				return
			}
			if err != nil {
				t.Fatalf("evaluateCharge() error = %v", err)
			}
			if got := result.String(); got != testCase.want {
				t.Fatalf("evaluateCharge() = %s, want %s", got, testCase.want)
			}
		})
	}
}

func TestPricingEngineInheritanceDefensiveBoundaries(t *testing.T) {
	engine := engineWithFlatPrice("1")
	parent := "parent"
	engine.config.Pricing.RateCards = map[string]RateCard{
		"child":  {Extends: &parent, Operations: map[string]OperationPricing{}},
		"parent": {Operations: map[string]OperationPricing{}},
	}
	if _, err := engine.operationPricing("child", "usage"); err == nil {
		t.Fatal("operationPricing accepted missing inherited pricing")
	}
	missing := "missing"
	engine.config.Pricing.RateCards["child"] = RateCard{Extends: &missing}
	if _, err := engine.operationPricing("child", "usage"); err == nil {
		t.Fatal("operationPricing accepted an unknown parent")
	}
	cycle := "child"
	engine.config.Pricing.RateCards["child"] = RateCard{Extends: &cycle}
	if _, err := engine.operationPricing("child", "usage"); err == nil {
		t.Fatal("operationPricing accepted an inheritance cycle")
	}
}

func TestRuntimeDimensionsRequireExactPortableTypes(t *testing.T) {
	tests := []struct {
		name       string
		value      any
		definition DimensionDefinition
	}{
		{"string", 42, DimensionDefinition{Type: "string"}},
		{"boolean", "true", DimensionDefinition{Type: "boolean"}},
		{"number", 1.5, DimensionDefinition{Type: "number"}},
		{"unknown schema type", "x", DimensionDefinition{Type: "object"}},
	}
	for _, testCase := range tests {
		t.Run(testCase.name, func(t *testing.T) {
			if _, err := validateRuntimeDimension(testCase.value, testCase.definition, "dimension"); err == nil {
				t.Fatal("validateRuntimeDimension succeeded; want error")
			}
		})
	}
	for name, value := range map[string]any{"string": "x", "boolean": true, "number": MustAmount("1.5")} {
		if _, err := validateRuntimeDimension(value, DimensionDefinition{Type: name}, "dimension"); err != nil {
			t.Fatalf("validateRuntimeDimension(%s) error = %v", name, err)
		}
	}
}
