package bursar

import "testing"

func TestPricingEnginePerUnitAndRateCardInheritance(t *testing.T) {
	config, err := LoadConfigFromMap(map[string]any{
		"version": 1,
		"pricing": map[string]any{
			"operations": map[string]any{
				"completion": map[string]any{
					"measures":   map[string]any{"tokens": map[string]any{"unit": "token"}},
					"dimensions": map[string]any{"model": map[string]any{"type": "string", "required": false}},
				},
			},
			"rate_cards": map[string]any{
				"base": map[string]any{
					"operations": map[string]any{
						"completion": map[string]any{
							"rules": []any{},
							"unmatched": map[string]any{"action": "charge", "charge": map[string]any{
								"type": "per_unit", "measure": "tokens", "unit_size": "1000", "rate": "0.0025",
							}},
						},
					},
				},
				"pro": map[string]any{"extends": "base", "operations": map[string]any{}},
			},
		},
		"credits": map[string]any{},
	})
	if err != nil {
		t.Fatalf("load config: %v", err)
	}
	engine, err := NewPricingEngine(config)
	if err != nil {
		t.Fatalf("new engine: %v", err)
	}
	tokens := MustAmount("1500")
	result, err := engine.Calculate(UsageMetrics{Operation: "completion", Measures: map[string]Amount{"tokens": tokens}}, PricingOptions{RateCard: "pro"})
	if err != nil {
		t.Fatalf("calculate: %v", err)
	}
	if got, want := result.Total.StringFixed(MoneyDecimalPlaces), "0.003750"; got != want {
		t.Fatalf("total = %s, want %s", got, want)
	}
}

func TestPricingEngineRulesAndRuntimeDimensionTypes(t *testing.T) {
	config, err := LoadConfigFromMap(map[string]any{
		"version": 1,
		"pricing": map[string]any{
			"operations": map[string]any{
				"completion": map[string]any{
					"measures":   map[string]any{"tokens": map[string]any{"unit": "token"}},
					"dimensions": map[string]any{"model": map[string]any{"type": "string"}},
				},
			},
			"rate_cards": map[string]any{
				"standard": map[string]any{"operations": map[string]any{
					"completion": map[string]any{
						"rules": []any{map[string]any{
							"when":   map[string]any{"model": map[string]any{"op": "prefix", "value": "gpt-"}},
							"charge": map[string]any{"type": "flat", "amount": "2"},
						}},
						"unmatched": map[string]any{"action": "charge", "charge": map[string]any{"type": "flat", "amount": "1"}},
					},
				}},
			},
		},
		"credits": map[string]any{},
	})
	if err != nil {
		t.Fatalf("load config: %v", err)
	}
	engine, err := NewPricingEngine(config)
	if err != nil {
		t.Fatalf("new engine: %v", err)
	}
	result, err := engine.Calculate(UsageMetrics{
		Operation: "completion", Measures: map[string]Amount{"tokens": MustAmount("1")}, Dimensions: map[string]any{"model": "gpt-4"},
	})
	if err != nil {
		t.Fatalf("calculate matching rule: %v", err)
	}
	if got := result.Total.StringFixed(MoneyDecimalPlaces); got != "2.000000" {
		t.Fatalf("total = %s, want 2.000000", got)
	}
	if _, err := engine.Calculate(UsageMetrics{
		Operation: "completion", Measures: map[string]Amount{"tokens": MustAmount("1")}, Dimensions: map[string]any{"model": true},
	}); err == nil {
		t.Fatal("invalid runtime dimension type accepted")
	}
}

func TestPricingEngineChargeKinds(t *testing.T) {
	tests := []struct {
		name    string
		charge  Charge
		measure string
		want    string
	}{
		{
			name:    "package ceil",
			charge:  Charge{Type: "package", Measure: "units", Units: MustAmount("10"), Amount: MustAmount("2"), Rounding: "ceil"},
			measure: "11", want: "4.000000",
		},
		{
			name: "graduated",
			charge: Charge{Type: "graduated", Measure: "units", Tiers: []GraduatedTier{
				{UpTo: amountPointer(MustAmount("10")), Rate: MustAmount("1")}, {Rate: MustAmount("2")},
			}},
			measure: "15", want: "20.000000",
		},
		{
			name: "volume",
			charge: Charge{Type: "volume", Measure: "units", Tiers: []GraduatedTier{
				{UpTo: amountPointer(MustAmount("10")), Rate: MustAmount("1")}, {Rate: MustAmount("2")},
			}},
			measure: "15", want: "30.000000",
		},
		{
			name:    "expression",
			charge:  Charge{Type: "expression", Formula: "units * 0.25 + 1"},
			measure: "4", want: "2.000000",
		},
		{
			name: "sum",
			charge: Charge{Type: "sum", Components: []Charge{
				{Type: "flat", Amount: MustAmount("1")},
				{Type: "per_unit", Measure: "units", UnitSize: MustAmount("2"), Rate: MustAmount("0.5")},
			}},
			measure: "4", want: "2.000000",
		},
	}
	for _, testCase := range tests {
		t.Run(testCase.name, func(t *testing.T) {
			charge := testCase.charge
			engine, err := NewPricingEngine(&BursarConfig{Pricing: &PricingConfig{
				Operations: map[string]OperationDefinition{
					"operation": {Measures: map[string]MeasureDefinition{"units": {Unit: "unit"}}, Dimensions: map[string]DimensionDefinition{}},
				},
				RateCards: map[string]RateCard{
					"standard": {Operations: map[string]OperationPricing{
						"operation": {Unmatched: UnmatchedPolicy{Action: "charge", Charge: &charge}},
					}},
				},
			}})
			if err != nil {
				t.Fatalf("new engine: %v", err)
			}
			result, err := engine.Calculate(UsageMetrics{Operation: "operation", Measures: map[string]Amount{"units": MustAmount(testCase.measure)}})
			if err != nil {
				t.Fatalf("calculate: %v", err)
			}
			if got := result.Total.StringFixed(MoneyDecimalPlaces); got != testCase.want {
				t.Fatalf("total = %s, want %s", got, testCase.want)
			}
		})
	}
}

func amountPointer(value Amount) *Amount { return &value }
