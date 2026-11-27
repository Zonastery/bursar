// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import "testing"

func TestNormalizeCatalogExpandsSignupPricingAndProviderShorthand(t *testing.T) {
	t.Parallel()
	raw := map[string]any{
		"credits": map[string]any{"on_signup": "20"},
		"pricing": map[string]any{
			"operations": map[string]any{"chat": map[string]any{"charge": map[string]any{"fixed": "1"}}},
			"rate_cards": map[string]any{"standard": map[string]any{}},
		},
		"plans": map[string]any{"basic": map[string]any{}},
		"commerce": map[string]any{
			"providers": map[string]any{
				"stripe": map[string]any{"type": "stripe"},
				"dodo":   map[string]any{"type": "dodo"},
			},
			"offers": map[string]any{
				"subscription": map[string]any{
					"providers": map[string]any{
						"stripe": map[string]any{"price_id": "price_test"},
						"dodo":   map[string]any{"product_id": "product_test"},
					},
				},
			},
		},
	}
	if err := normalizeCatalog(raw, "test"); err != nil {
		t.Fatalf("normalizeCatalog() error = %v", err)
	}

	credits := raw["credits"].(map[string]any)
	if credits["default_bucket"] != "default" || credits["on_signup"] != nil {
		t.Fatalf("signup shorthand was not normalized: %#v", credits)
	}
	programs := credits["grant_programs"].(map[string]any)
	if programs["signup"] == nil {
		t.Fatalf("signup grant program = %#v", programs)
	}
	pricing := raw["pricing"].(map[string]any)
	operations := pricing["rate_cards"].(map[string]any)["standard"].(map[string]any)["operations"].(map[string]any)
	if operations["chat"] == nil || raw["plans"].(map[string]any)["basic"].(map[string]any)["rate_card"] != "standard" {
		t.Fatalf("pricing shorthand was not normalized: %#v", pricing)
	}
	offers := raw["commerce"].(map[string]any)["offers"].(map[string]any)
	providers := offers["subscription"].(map[string]any)["providers"].(map[string]any)
	if providers["stripe"].(map[string]any)["type"] != "stripe_price" || providers["dodo"].(map[string]any)["type"] != "dodo_product" {
		t.Fatalf("provider reference types were not inferred: %#v", providers)
	}
}

func TestNormalizeCatalogRejectsAmbiguousSignupAndPricingShorthand(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name string
		raw  map[string]any
	}{
		{
			name: "signup without explicit default bucket",
			raw: map[string]any{"credits": map[string]any{
				"buckets":   map[string]any{},
				"on_signup": "20",
			}},
		},
		{
			name: "signup conflicts with grant program",
			raw: map[string]any{"credits": map[string]any{
				"default_bucket": "default",
				"on_signup":      "20",
				"grant_programs": map[string]any{"signup": map[string]any{"trigger": "account_created"}},
			}},
		},
		{
			name: "non-object rate card",
			raw: map[string]any{
				"pricing": map[string]any{
					"operations": map[string]any{"chat": map[string]any{"charge": "1"}},
					"rate_cards": map[string]any{"standard": "invalid"},
				},
			},
		},
		{
			name: "invalid environment",
			raw:  map[string]any{},
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			environment := "test"
			if test.name == "invalid environment" {
				environment = "production"
			}
			if err := normalizeCatalog(test.raw, environment); err == nil {
				t.Fatal("ambiguous shorthand was accepted")
			}
		})
	}
}

func TestScopeScalarProviderBindingsOnlyAtPublicationBoundary(t *testing.T) {
	t.Parallel()
	canonical := map[string]any{"commerce": map[string]any{"offers": map[string]any{
		"buy": map[string]any{"providers": map[string]any{
			"stripe": map[string]any{"price_id": "price_1", "external_id": "lookup_1"},
			"dodo":   map[string]any{"product_id": map[string]any{"live": "product_live"}},
		}},
	}}}
	if got := scopeScalarProviderBindings(map[string]any{}, ""); len(got) != 0 {
		t.Fatalf("empty environment returned a nonempty map: %#v", got)
	}
	got := scopeScalarProviderBindings(canonical, "test")
	refs := got["commerce"].(map[string]any)["offers"].(map[string]any)["buy"].(map[string]any)["providers"].(map[string]any)
	stripe := refs["stripe"].(map[string]any)
	if stripe["price_id"].(map[string]any)["test"] != "price_1" || stripe["external_id"].(map[string]any)["test"] != "lookup_1" {
		t.Fatalf("scalar bindings were not scoped: %#v", stripe)
	}
}
