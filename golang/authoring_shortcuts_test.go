package bursar

import "testing"

func TestAuthoringRateCardDefaultsAndInlineBase(t *testing.T) {
	raw := map[string]any{"version": 1, "credits": map[string]any{"on_signup": nil}, "catalog": map[string]any{"default_plan": "free"},
		"plans":   map[string]any{"free": map[string]any{"display_name": "Free", "allowed_operations": []any{"report"}}},
		"pricing": map[string]any{"operations": map[string]any{"report": map[string]any{"charge": map[string]any{"type": "flat", "amount": "2"}}}, "rate_cards": map[string]any{"custom": map[string]any{"operations": map[string]any{}}}}}
	parsed, err := LoadConfigFromMap(raw)
	if err != nil {
		t.Fatal(err)
	}
	if parsed.Plans["free"].RateCard == nil || *parsed.Plans["free"].RateCard != "custom" {
		t.Fatal("sole card not inferred")
	}
	if _, err := LoadConfigFromMap(CanonicalParsedBursarConfigDict(parsed)); err != nil {
		t.Fatal(err)
	}
	cards := raw["pricing"].(map[string]any)["rate_cards"].(map[string]any)
	cards["other"] = map[string]any{"operations": map[string]any{}}
	if _, err := LoadConfigFromMap(raw); err == nil {
		t.Fatal("multiple cards accepted without selection")
	}
	raw["plans"].(map[string]any)["free"].(map[string]any)["rate_card"] = "custom"
	if _, err := LoadConfigFromMap(raw); err != nil {
		t.Fatal(err)
	}
}

func TestProviderReferenceShorthandInferred(t *testing.T) {
	raw := map[string]any{"version": 1, "commerce": map[string]any{
		"providers": map[string]any{"stripe": map[string]any{"type": "stripe"}},
		"offers":    map[string]any{"pack": map[string]any{"type": "topup", "display_name": "Pack", "price": map[string]any{"amount_minor": 100, "currency": "USD"}, "credits_per_unit": "10", "bucket": "default", "providers": map[string]any{"stripe": map[string]any{"price_id": "price_1"}}}},
	}}
	parsed, err := LoadConfigFromMap(raw)
	if err != nil {
		t.Fatal(err)
	}
	if got := parsed.Commerce.Offers["pack"].Providers["stripe"].Type; got != "stripe_price" {
		t.Fatalf("kind = %q", got)
	}
	if _, err := LoadConfigFromMap(CanonicalParsedBursarConfigDict(parsed)); err != nil {
		t.Fatal(err)
	}
}
