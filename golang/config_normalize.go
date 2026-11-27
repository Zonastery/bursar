package bursar

import "fmt"

// normalizeCatalog compiles public shorthand without changing the source map.
func normalizeCatalog(raw map[string]any, environment string) error {
	if environment != "" && environment != "test" && environment != "live" && environment != "sandbox" {
		return newConfigError("invalid provider environment", nil)
	}
	credits, ok := raw["credits"].(map[string]any)
	if !ok {
		credits = map[string]any{}
		raw["credits"] = credits
	}
	if _, ok := credits["buckets"]; !ok {
		credits["buckets"] = map[string]any{"default": map[string]any{"priority": 0}}
		if _, ok := credits["default_bucket"]; !ok {
			credits["default_bucket"] = "default"
		}
	}
	if signup, ok := credits["on_signup"]; ok && signup != nil {
		bucket, ok := credits["default_bucket"].(string)
		if !ok {
			return newConfigError("credits.on_signup requires credits.default_bucket", nil)
		}
		programs, ok := credits["grant_programs"].(map[string]any)
		if !ok {
			programs = map[string]any{}
			credits["grant_programs"] = programs
		}
		if _, ok := programs["signup"]; ok {
			return newConfigError("credits.on_signup conflicts with grant program", nil)
		}
		for _, value := range programs {
			if program, ok := value.(map[string]any); ok && program["trigger"] == "account_created" {
				return newConfigError("credits.on_signup conflicts with grant program", nil)
			}
		}
		programs["signup"] = map[string]any{"trigger": "account_created", "awards": []any{map[string]any{"amount": signup, "bucket": bucket}}}
		delete(credits, "on_signup")
	}
	delete(credits, "on_signup")
	pricing, _ := raw["pricing"].(map[string]any)
	if pricing != nil {
		operations, _ := pricing["operations"].(map[string]any)
		cards, ok := pricing["rate_cards"].(map[string]any)
		if !ok {
			cards = map[string]any{}
			pricing["rate_cards"] = cards
		}
		shorthand := map[string]any{}
		for key, value := range operations {
			op, ok := value.(map[string]any)
			if !ok {
				continue
			}
			if charge, exists := op["charge"]; exists {
				if charge != nil {
					shorthand[key] = map[string]any{"unmatched": map[string]any{"action": "charge", "charge": charge}}
				}
				delete(op, "charge")
			}
		}
		if len(shorthand) > 0 {
			if len(cards) == 0 {
				cards["standard"] = map[string]any{"operations": map[string]any{}}
			}
			for cardKey, value := range cards {
				card, ok := value.(map[string]any)
				if !ok {
					return newConfigError(fmt.Sprintf("pricing.rate_cards.%s must be an object", cardKey), nil)
				}
				if card["extends"] != nil {
					continue
				}
				prices, exists := card["operations"]
				if !exists {
					prices = map[string]any{}
					card["operations"] = prices
				}
				priceMap, ok := prices.(map[string]any)
				if !ok {
					return newConfigError(fmt.Sprintf("pricing.rate_cards.%s.operations must be an object", cardKey), nil)
				}
				for key, price := range shorthand {
					if _, exists := priceMap[key]; !exists {
						priceMap[key] = price
					}
				}
			}
		}
		if len(cards) == 1 {
			for cardKey := range cards {
				if plans, ok := raw["plans"].(map[string]any); ok {
					for _, value := range plans {
						if plan, ok := value.(map[string]any); ok {
							if _, exists := plan["rate_card"]; !exists {
								plan["rate_card"] = cardKey
							}
						}
					}
				}
			}
		}

	}
	inferProviderTypes(raw)

	return nil
}

// inferProviderTypes expands provider-specific reference shorthand.
func inferProviderTypes(raw map[string]any) {
	commerce, _ := raw["commerce"].(map[string]any)
	definitions, _ := commerce["providers"].(map[string]any)
	offers, _ := commerce["offers"].(map[string]any)
	for _, value := range offers {
		offer, _ := value.(map[string]any)
		refs, _ := offer["providers"].(map[string]any)
		for key, value := range refs {
			ref, ok := value.(map[string]any)
			if !ok {
				continue
			}
			if _, exists := ref["type"]; exists {
				continue
			}
			provider, _ := definitions[key].(map[string]any)
			switch provider["type"] {
			case "stripe":
				ref["type"] = "stripe_price"
			case "dodo":
				ref["type"] = "dodo_product"
			}
		}
	}
}

// scopeScalarProviderBindings binds scalar IDs only at publication boundaries.
func scopeScalarProviderBindings(canonical map[string]any, environment string) map[string]any {
	if environment == "" {
		return canonical
	}
	commerce, _ := canonical["commerce"].(map[string]any)
	offers, _ := commerce["offers"].(map[string]any)
	for _, rawOffer := range offers {
		offer, _ := rawOffer.(map[string]any)
		refs, _ := offer["providers"].(map[string]any)
		for _, rawRef := range refs {
			ref, _ := rawRef.(map[string]any)
			for _, field := range []string{"price_id", "product_id", "external_id"} {
				if scalar, ok := ref[field].(string); ok {
					ref[field] = map[string]any{environment: scalar}
				}
			}
		}
	}
	return canonical
}
