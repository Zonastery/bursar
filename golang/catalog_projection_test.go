package bursar

import "testing"

func TestProjectPublicCatalogSortsAndDoesNotExposeProviderReferences(t *testing.T) {
	config, err := LoadConfigFromMap(map[string]any{
		"version": 1,
		"catalog": map[string]any{"default_plan": "starter"},
		"credits": map[string]any{
			"buckets":        map[string]any{"purchased": map[string]any{"priority": 1}},
			"default_bucket": "purchased",
			"display":        map[string]any{"currency": "USD", "units_per_major": "100"},
		},
		"plans": map[string]any{
			"pro":     map[string]any{"display_name": "Pro", "rank": 10},
			"starter": map[string]any{"display_name": "Starter", "rank": 1},
		},
		"commerce": map[string]any{
			"providers": map[string]any{"stripe": map[string]any{"type": "stripe"}},
			"offers": map[string]any{
				"pro_monthly": map[string]any{
					"type": "subscription", "display_name": "Pro monthly", "sort_order": 2,
					"price":     map[string]any{"amount_minor": 1200, "currency": "USD"},
					"providers": map[string]any{"stripe": map[string]any{"type": "stripe_price", "price_id": "price_pro"}},
					"plan":      "pro", "billing_interval": map[string]any{"unit": "month"},
				},
				"starter_monthly": map[string]any{
					"type": "subscription", "display_name": "Starter monthly", "sort_order": 1,
					"price":     map[string]any{"amount_minor": 500, "currency": "USD"},
					"providers": map[string]any{"stripe": map[string]any{"type": "stripe_price", "price_id": "price_starter"}},
					"plan":      "starter", "billing_interval": map[string]any{"unit": "month"},
				},
				"pack": map[string]any{
					"type": "topup", "display_name": "Pack", "price": map[string]any{"amount_minor": 500, "currency": "USD"},
					"providers":        map[string]any{"stripe": map[string]any{"type": "stripe_price", "price_id": "price_pack"}},
					"credits_per_unit": "100", "bucket": "purchased",
				},
			},
		},
	})
	if err != nil {
		t.Fatalf("load config: %v", err)
	}
	catalog := ProjectPublicCatalog(config)
	if len(catalog.Plans) != 2 || catalog.Plans[0].Key != "starter" || catalog.Plans[1].Key != "pro" {
		t.Fatalf("plans not sorted by rank/key: %#v", catalog.Plans)
	}
	if len(catalog.Topups) != 1 || catalog.Topups[0].Key != "pack" {
		t.Fatalf("topups = %#v", catalog.Topups)
	}
	if catalog.Topups[0].CreditsPerUnit == nil || *catalog.Topups[0].CreditsPerUnit != "100.000000" {
		t.Fatalf("topup credit amount = %#v", catalog.Topups[0].CreditsPerUnit)
	}
	if catalog.Topups[0].Price.Currency != "USD" {
		t.Fatalf("topup price = %#v", catalog.Topups[0].Price)
	}
}
