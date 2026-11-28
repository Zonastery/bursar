package bursar

import "testing"

func TestCanonicalCatalogPreservesAccessAndRequiredMeasures(t *testing.T) {
	raw := map[string]any{
		"version": 1, "credits": map[string]any{},
		"catalog": map[string]any{"default_plan": "free", "subscription_end_plan": "free"},
		"plans":   map[string]any{"free": map[string]any{"display_name": "Free", "tier": 0, "allowed_operations": "none"}},
		"pricing": map[string]any{
			"operations": map[string]any{"usage": map[string]any{"measures": map[string]any{"units": map[string]any{"unit": "unit", "required": true}}}},
			"rate_cards": map[string]any{"standard": map[string]any{"operations": map[string]any{"usage": map[string]any{"unmatched": map[string]any{"action": "charge", "charge": map[string]any{"type": "per_unit", "measure": "units", "rate": "2"}}}}}},
		},
	}
	config, err := LoadConfigFromMap(raw)
	if err != nil {
		t.Fatal(err)
	}
	saved := CanonicalParsedBursarConfigDict(config)
	reloaded, err := LoadConfigFromMap(saved)
	if err != nil {
		t.Fatal(err)
	}
	plan := reloaded.Plans["free"]
	if plan.OperationAccess != "none" || plan.Tier != 0 {
		t.Fatalf("plan fields lost: %#v", plan)
	}
	if reloaded.Catalog.SubscriptionEndPlan == nil || *reloaded.Catalog.SubscriptionEndPlan != "free" {
		t.Fatal("subscription end plan lost")
	}
	if !reloaded.Pricing.Operations["usage"].Measures["units"].Required {
		t.Fatal("required measure lost")
	}
	engine, err := NewPricingEngine(reloaded)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := engine.Calculate(UsageMetrics{Operation: "usage"}); err == nil {
		t.Fatal("missing required measure accepted")
	}
	if _, err := engine.Calculate(UsageMetrics{Operation: "usage", Measures: map[string]Amount{"units": MustAmount("0")}}); err != nil {
		t.Fatalf("explicit zero rejected: %v", err)
	}
}

func TestCanonicalCatalogRetainsSubMicroRate(t *testing.T) {
	raw := map[string]any{
		"version": 1, "credits": map[string]any{},
		"pricing": map[string]any{
			"operations": map[string]any{"usage": map[string]any{"measures": map[string]any{"units": map[string]any{"unit": "unit"}}}},
			"rate_cards": map[string]any{"standard": map[string]any{"operations": map[string]any{"usage": map[string]any{
				"unmatched": map[string]any{"action": "charge", "charge": map[string]any{"type": "per_unit", "measure": "units", "rate": "0.0000004"}},
			}}}},
		},
	}
	parsed, err := LoadConfigFromMap(raw)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if got := parsed.Pricing.RateCards["standard"].Operations["usage"].Unmatched.Charge.Rate.String(); got != "0.0000004" {
		t.Fatalf("parse changed rate: %s", got)
	}
	saved := CanonicalParsedBursarConfigDict(parsed)
	reloaded, err := LoadConfigFromMap(saved)
	if err != nil {
		t.Fatalf("reload: %v", err)
	}
	if got := reloaded.Pricing.RateCards["standard"].Operations["usage"].Unmatched.Charge.Rate.String(); got != "0.0000004" {
		t.Fatalf("roundtrip changed rate: %s", got)
	}
	engine, err := NewPricingEngine(reloaded)
	if err != nil {
		t.Fatal(err)
	}
	quote, err := engine.Calculate(UsageMetrics{Operation: "usage", Measures: map[string]Amount{"units": MustAmount("1000000")}})
	if err != nil {
		t.Fatal(err)
	}
	if !quote.Total.Equal(MustAmount("0.4")) {
		t.Fatalf("total = %s, want 0.4", quote.Total)
	}
}

func TestSingleSchemaStarterNormalizesAndRoundTrips(t *testing.T) {
	raw := map[string]any{
		"version": 1, "catalog": map[string]any{"default_plan": "free"},
		"credits": map[string]any{"on_signup": "20"},
		"pricing": map[string]any{"operations": map[string]any{"report": map[string]any{"charge": map[string]any{"type": "flat", "amount": "2"}}}},
		"plans":   map[string]any{"free": map[string]any{"display_name": "Free", "allowed_operations": []any{"report"}}},
	}
	parsed, err := LoadConfigFromMap(raw)
	if err != nil {
		t.Fatalf("load starter: %v", err)
	}
	if len(parsed.Pricing.Operations["report"].Measures) != 0 {
		t.Fatal("flat operation acquired synthetic measures")
	}
	canonical := CanonicalParsedBursarConfigDict(parsed)
	if _, err := LoadConfigFromMap(canonical); err != nil {
		t.Fatalf("reload canonical: %v", err)
	}
	engine, err := NewPricingEngine(parsed)
	if err != nil {
		t.Fatal(err)
	}
	quote, err := engine.Calculate(UsageMetrics{Operation: "report"})
	if err != nil || !quote.Total.Equal(MustAmount("2")) {
		t.Fatalf("quote = %#v, err = %v", quote, err)
	}
}

func TestPlanChangeUsesTierNotDisplayRank(t *testing.T) {
	config := &BursarConfig{Plans: map[string]PlanDefinition{
		"basic":   {Tier: 0, Rank: 100},
		"premium": {Tier: 1, Rank: 0},
	}}
	got, err := classifyPlanChange(config, "basic", "month", "premium", "month")
	if err != nil || got != PlanChangeUpgrade {
		t.Fatalf("classification = %v, err = %v", got, err)
	}
}

func TestProviderEnvironmentBindingsResolveBeforeCatalogPublication(t *testing.T) {
	raw := map[string]any{
		"version": 1,
		"commerce": map[string]any{
			"providers": map[string]any{"stripe": map[string]any{"type": "stripe"}},
			"offers": map[string]any{"pack": map[string]any{
				"type": "topup", "display_name": "Pack", "price": map[string]any{"amount_minor": 100, "currency": "USD"},
				"credits_per_unit": "10", "bucket": "default",
				"providers": map[string]any{"stripe": map[string]any{"type": "stripe_price", "price_id": map[string]any{"test": "test_price", "live": "live_price"}}},
			}},
		},
	}
	parsed, err := LoadConfigFromMap(raw)
	if err != nil {
		t.Fatal(err)
	}
	ref := parsed.Commerce.Offers["pack"].Providers["stripe"]
	for _, environment := range []ProviderEnvironment{"test", "live"} {
		resolved, err := ResolveProviderReference(ref, environment)
		if err != nil {
			t.Fatal(err)
		}
		if resolved.PriceID.scalar() != string(environment)+"_price" {
			t.Fatalf("%s ID = %v", environment, resolved.PriceID)
		}
	}
	if _, err := ResolveProviderReference(ref, "sandbox"); err == nil {
		t.Fatal("missing binding accepted")
	}
	roundtrip, err := LoadConfigFromMap(CanonicalParsedBursarConfigDict(parsed))
	if err != nil {
		t.Fatal(err)
	}
	if len(roundtrip.Commerce.Offers["pack"].Providers["stripe"].PriceID.canonical().(map[string]string)) != 2 {
		t.Fatal("bindings lost during roundtrip")
	}

}

func TestProviderBindingsAllowMultiplePartialEnvironments(t *testing.T) {
	config := checkoutTestConfig(t)
	offers := config["commerce"].(map[string]any)["offers"].(map[string]any)
	first := offers["starter_month"].(map[string]any)
	first["providers"] = map[string]any{"stripe": map[string]any{"type": "custom_object", "object_kind": "subscription", "external_id": map[string]any{"test": "shared"}}}
	second := offers["starter_year"].(map[string]any)
	second["providers"] = map[string]any{"stripe": map[string]any{"type": "custom_object", "object_kind": "subscription", "external_id": map[string]any{"live": "shared"}}}
	parsed, err := LoadConfigFromMap(config)
	if err != nil {
		t.Fatal(err)
	}
	again, err := LoadConfigFromMap(CanonicalParsedBursarConfigDict(parsed))
	if err != nil {
		t.Fatal(err)
	}
	if got := again.Commerce.Offers["starter_year"].Providers["stripe"].ExternalID.canonical().(map[string]string)["live"]; got != "shared" {
		t.Fatalf("lost binding: %q", got)
	}
	if _, err := ResolveProviderReference(again.Commerce.Offers["starter_year"].Providers["stripe"], ProviderEnvironmentTest); err == nil {
		t.Fatal("absent binding accepted")
	}
	second["providers"].(map[string]any)["stripe"].(map[string]any)["external_id"] = map[string]any{"test": "shared"}
	if _, err := LoadConfigFromMap(config); err == nil {
		t.Fatal("duplicate in the same environment accepted")
	}
}

func TestProviderIDDefensivelyCopiesBindings(t *testing.T) {
	bindings := map[ProviderEnvironment]string{ProviderEnvironmentTest: "original"}
	id := BoundProviderID(bindings)
	bindings[ProviderEnvironmentTest] = "changed"
	value, err := id.Resolve(ProviderEnvironmentTest)
	if err != nil || value != "original" {
		t.Fatalf("value = %q, error = %v", value, err)
	}
	canonical := id.canonical().(map[string]string)
	canonical["test"] = "changed"
	value, _ = id.Resolve(ProviderEnvironmentTest)
	if value != "original" {
		t.Fatalf("canonical map mutated ID: %q", value)
	}
}
