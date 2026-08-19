// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import "testing"

func TestPricingParserRejectsIncompleteFinancialRules(t *testing.T) {
	operation := map[string]any{
		"measures":   map[string]any{"units": map[string]any{"unit": "unit"}},
		"dimensions": map[string]any{"model": map[string]any{"type": "string"}},
	}
	validUnmatched := map[string]any{"action": "charge", "charge": map[string]any{"type": "flat", "amount": "1"}}
	pricing := func(card map[string]any) map[string]any {
		return map[string]any{
			"operations": map[string]any{"usage": operation},
			"rate_cards": map[string]any{"standard": card},
		}
	}
	pricedOperation := func(value map[string]any) map[string]any {
		return map[string]any{"operations": map[string]any{"usage": value}}
	}

	checks := []struct {
		name  string
		check func() error
	}{
		{"flat amount", func() error { _, err := parseCharge(map[string]any{"type": "flat"}, "charge", 0); return err }},
		{"per-unit measure", func() error { _, err := parseCharge(map[string]any{"type": "per_unit"}, "charge", 0); return err }},
		{"per-unit rate", func() error {
			_, err := parseCharge(map[string]any{"type": "per_unit", "measure": "units"}, "charge", 0)
			return err
		}},
		{"per-unit unit size", func() error {
			_, err := parseCharge(map[string]any{"type": "per_unit", "measure": "units", "rate": "1", "unit_size": true}, "charge", 0)
			return err
		}},
		{"package measure", func() error { _, err := parseCharge(map[string]any{"type": "package"}, "charge", 0); return err }},
		{"package units", func() error {
			_, err := parseCharge(map[string]any{"type": "package", "measure": "units"}, "charge", 0)
			return err
		}},
		{"package amount", func() error {
			_, err := parseCharge(map[string]any{"type": "package", "measure": "units", "units": "10"}, "charge", 0)
			return err
		}},
		{"package rounding", func() error {
			_, err := parseCharge(map[string]any{"type": "package", "measure": "units", "units": "10", "amount": "1", "rounding": true}, "charge", 0)
			return err
		}},
		{"package positive units", func() error {
			_, err := parseCharge(map[string]any{"type": "package", "measure": "units", "units": "0", "amount": "1"}, "charge", 0)
			return err
		}},
		{"tiered measure", func() error { _, err := parseCharge(map[string]any{"type": "graduated"}, "charge", 0); return err }},
		{"tiered tiers", func() error {
			_, err := parseCharge(map[string]any{"type": "volume", "measure": "units", "tiers": "invalid"}, "charge", 0)
			return err
		}},
		{"expression formula", func() error { _, err := parseCharge(map[string]any{"type": "expression"}, "charge", 0); return err }},
		{"sum components", func() error {
			_, err := parseCharge(map[string]any{"type": "sum", "components": "invalid"}, "charge", 0)
			return err
		}},
		{"sum nested charge", func() error {
			_, err := parseCharge(map[string]any{"type": "sum", "components": []any{map[string]any{"type": "flat"}}}, "charge", 0)
			return err
		}},
		{"undeclared measure", func() error {
			return validatePricingCharge(Charge{Type: "per_unit", Measure: "missing"}, OperationDefinition{Measures: map[string]MeasureDefinition{}}, "usage")
		}},
		{"invalid nested expression", func() error {
			return validatePricingCharge(Charge{Type: "sum", Components: []Charge{{Type: "expression", Formula: "unknown + 1"}}}, OperationDefinition{Measures: map[string]MeasureDefinition{"units": {}}}, "usage")
		}},
		{"operation identifiers", func() error {
			_, err := parsePricing(map[string]any{"operations": map[string]any{"bad-key": operation}, "rate_cards": map[string]any{"standard": map[string]any{}}})
			return err
		}},
		{"rate-card identifiers", func() error {
			_, err := parsePricing(map[string]any{"operations": map[string]any{"usage": operation}, "rate_cards": map[string]any{"Bad": map[string]any{}}})
			return err
		}},
		{"dimension object", func() error {
			bad := map[string]any{"measures": operation["measures"], "dimensions": map[string]any{"model": "string"}}
			_, err := parsePricing(map[string]any{"operations": map[string]any{"usage": bad}, "rate_cards": map[string]any{"standard": map[string]any{}}})
			return err
		}},
		{"dimension type", func() error {
			bad := map[string]any{"measures": operation["measures"], "dimensions": map[string]any{"model": map[string]any{}}}
			_, err := parsePricing(map[string]any{"operations": map[string]any{"usage": bad}, "rate_cards": map[string]any{"standard": map[string]any{}}})
			return err
		}},
		{"rate-card object", func() error {
			_, err := parsePricing(map[string]any{"operations": map[string]any{"usage": operation}, "rate_cards": map[string]any{"standard": "invalid"}})
			return err
		}},
		{"rate-card extends", func() error { _, err := parsePricing(pricing(map[string]any{"extends": true})); return err }},
		{"rate-card operations", func() error { _, err := parsePricing(pricing(map[string]any{"operations": "invalid"})); return err }},
		{"unknown priced operation", func() error {
			_, err := parsePricing(pricing(map[string]any{"operations": map[string]any{"missing": validUnmatched}}))
			return err
		}},
		{"priced operation object", func() error {
			_, err := parsePricing(pricing(map[string]any{"operations": map[string]any{"usage": "invalid"}}))
			return err
		}},
		{"rules array", func() error {
			_, err := parsePricing(pricing(pricedOperation(map[string]any{"rules": "invalid", "unmatched": validUnmatched})))
			return err
		}},
		{"rule object", func() error {
			_, err := parsePricing(pricing(pricedOperation(map[string]any{"rules": []any{"invalid"}, "unmatched": validUnmatched})))
			return err
		}},
		{"rule matcher object", func() error {
			_, err := parsePricing(pricing(pricedOperation(map[string]any{"rules": []any{map[string]any{"when": "invalid"}}, "unmatched": validUnmatched})))
			return err
		}},
		{"rule unknown dimension", func() error {
			rule := map[string]any{"when": map[string]any{"region": map[string]any{"op": "eq", "value": "US"}}, "charge": map[string]any{"type": "flat", "amount": "1"}}
			_, err := parsePricing(pricing(pricedOperation(map[string]any{"rules": []any{rule}, "unmatched": validUnmatched})))
			return err
		}},
		{"rule matcher", func() error {
			rule := map[string]any{"when": map[string]any{"model": map[string]any{"op": "range"}}, "charge": map[string]any{"type": "flat", "amount": "1"}}
			_, err := parsePricing(pricing(pricedOperation(map[string]any{"rules": []any{rule}, "unmatched": validUnmatched})))
			return err
		}},
		{"rule charge", func() error {
			rule := map[string]any{"when": map[string]any{"model": map[string]any{"op": "eq", "value": "gpt"}}, "charge": map[string]any{"type": "flat"}}
			_, err := parsePricing(pricing(pricedOperation(map[string]any{"rules": []any{rule}, "unmatched": validUnmatched})))
			return err
		}},
		{"unmatched object", func() error {
			_, err := parsePricing(pricing(pricedOperation(map[string]any{"unmatched": "invalid"})))
			return err
		}},
		{"unmatched action", func() error {
			_, err := parsePricing(pricing(pricedOperation(map[string]any{"unmatched": map[string]any{}})))
			return err
		}},
		{"unmatched charge", func() error {
			_, err := parsePricing(pricing(pricedOperation(map[string]any{"unmatched": map[string]any{"action": "charge"}})))
			return err
		}},
		{"unknown parent rate card", func() error { _, err := parsePricing(pricing(map[string]any{"extends": "missing"})); return err }},
	}

	for _, test := range checks {
		t.Run(test.name, func(t *testing.T) {
			if err := test.check(); err == nil {
				t.Fatal("unsafe pricing rule was accepted")
			}
		})
	}
}

func TestCommerceParserRejectsUnsafeProviderAndAutoRechargePolicies(t *testing.T) {
	credits := CreditsConfig{Buckets: map[string]BucketDefinition{"general": {Priority: 1}}}
	quantity := OfferQuantity{Minimum: 1, Maximum: 5, Default: 1}
	offers := map[string]CommerceOffer{
		"usd": {Type: "topup", Price: OfferPrice{Currency: "USD"}, Quantity: &quantity},
		"eur": {Type: "topup", Price: OfferPrice{Currency: "EUR"}, Quantity: &quantity},
	}
	validAutoRecharge := func() map[string]any {
		return map[string]any{
			"eligible_topups": []any{"usd"},
			"balance_below":   map[string]any{"minimum": "1", "maximum": "5", "default": "3"},
			"rearm_above":     "6",
			"quantity":        map[string]any{"minimum": 1, "maximum": 5, "default": 1},
			"limits": map[string]any{
				"max_purchases": 2, "window": map[string]any{"type": "rolling", "duration": map[string]any{"unit": "day", "count": 1}},
				"max_charge_minor": int64(1000), "cooldown": map[string]any{"unit": "minute", "count": 5},
			},
		}
	}
	topupOffer := func(overrides map[string]any) map[string]any {
		result := map[string]any{
			"type": "topup", "display_name": "Pack", "price": map[string]any{"amount_minor": int64(1000), "currency": "USD"},
			"providers":        map[string]any{"stripe": map[string]any{"type": "stripe_price", "price_id": "price_pack"}},
			"credits_per_unit": "10", "bucket": "general", "quantity": map[string]any{"minimum": 1, "maximum": 5, "default": 1},
		}
		for key, value := range overrides {
			result[key] = value
		}
		return result
	}
	provider := map[string]ProviderDefinition{"stripe": {Type: "stripe"}}

	checks := []struct {
		name  string
		check func() error
	}{
		{"offers map", func() error { _, err := parseCommerce(map[string]any{"offers": "invalid"}, credits); return err }},
		{"provider identifiers", func() error {
			_, err := parseCommerce(map[string]any{"providers": map[string]any{"Bad": map[string]any{"type": "stripe"}}}, credits)
			return err
		}},
		{"offer identifiers", func() error {
			_, err := parseCommerce(map[string]any{"offers": map[string]any{"bad-key": map[string]any{}}}, credits)
			return err
		}},
		{"provider type", func() error {
			_, err := parseCommerce(map[string]any{"providers": map[string]any{"stripe": map[string]any{}}}, credits)
			return err
		}},
		{"custom adapter", func() error {
			_, err := parseCommerce(map[string]any{"providers": map[string]any{"custom": map[string]any{"type": "custom"}}}, credits)
			return err
		}},
		{"stripe price", func() error {
			_, err := parseProviderReference(map[string]any{"type": "stripe_price"}, "provider")
			return err
		}},
		{"dodo product", func() error {
			_, err := parseProviderReference(map[string]any{"type": "dodo_product"}, "provider")
			return err
		}},
		{"custom object kind", func() error {
			_, err := parseProviderReference(map[string]any{"type": "custom_object"}, "provider")
			return err
		}},
		{"custom external ID", func() error {
			_, err := parseProviderReference(map[string]any{"type": "custom_object", "object_kind": "one_time"}, "provider")
			return err
		}},
		{"offer type", func() error {
			_, err := parseCommerceOffer("pack", map[string]any{}, provider, credits, map[string]struct{}{})
			return err
		}},
		{"offer display", func() error {
			_, err := parseCommerceOffer("pack", map[string]any{"type": "topup"}, provider, credits, map[string]struct{}{})
			return err
		}},
		{"offer description", func() error {
			_, err := parseCommerceOffer("pack", topupOffer(map[string]any{"description": true}), provider, credits, map[string]struct{}{})
			return err
		}},
		{"offer sort order", func() error {
			_, err := parseCommerceOffer("pack", topupOffer(map[string]any{"sort_order": "first"}), provider, credits, map[string]struct{}{})
			return err
		}},
		{"offer availability", func() error {
			_, err := parseCommerceOffer("pack", topupOffer(map[string]any{"availability": "all"}), provider, credits, map[string]struct{}{})
			return err
		}},
		{"offer price", func() error {
			_, err := parseCommerceOffer("pack", topupOffer(map[string]any{"price": "invalid"}), provider, credits, map[string]struct{}{})
			return err
		}},
		{"offer amount", func() error {
			_, err := parseCommerceOffer("pack", topupOffer(map[string]any{"price": map[string]any{"currency": "USD"}}), provider, credits, map[string]struct{}{})
			return err
		}},
		{"offer currency", func() error {
			_, err := parseCommerceOffer("pack", topupOffer(map[string]any{"price": map[string]any{"amount_minor": 1}}), provider, credits, map[string]struct{}{})
			return err
		}},
		{"ISO currency", func() error {
			_, err := parseCommerceOffer("pack", topupOffer(map[string]any{"price": map[string]any{"amount_minor": 1, "currency": "usd"}}), provider, credits, map[string]struct{}{})
			return err
		}},
		{"tax behavior", func() error {
			_, err := parseCommerceOffer("pack", topupOffer(map[string]any{"price": map[string]any{"amount_minor": 1, "currency": "USD", "tax_behavior": true}}), provider, credits, map[string]struct{}{})
			return err
		}},
		{"provider references", func() error {
			_, err := parseCommerceOffer("pack", topupOffer(map[string]any{"providers": "invalid"}), provider, credits, map[string]struct{}{})
			return err
		}},
		{"unknown provider", func() error {
			_, err := parseCommerceOffer("pack", topupOffer(map[string]any{"providers": map[string]any{"missing": map[string]any{"type": "stripe_price", "price_id": "p"}}}), provider, credits, map[string]struct{}{})
			return err
		}},
		{"incompatible provider", func() error {
			_, err := parseCommerceOffer("pack", topupOffer(map[string]any{"providers": map[string]any{"stripe": map[string]any{"type": "dodo_product", "product_id": "p"}}}), provider, credits, map[string]struct{}{})
			return err
		}},
		{"duplicate provider object", func() error {
			_, err := parseCommerceOffer("pack", topupOffer(nil), provider, credits, map[string]struct{}{"stripe/price_pack": {}})
			return err
		}},
		{"topup credits", func() error {
			_, err := parseCommerceOffer("pack", topupOffer(map[string]any{"credits_per_unit": true}), provider, credits, map[string]struct{}{})
			return err
		}},
		{"topup bucket", func() error {
			_, err := parseCommerceOffer("pack", topupOffer(map[string]any{"bucket": true}), provider, credits, map[string]struct{}{})
			return err
		}},
		{"topup unknown bucket", func() error {
			_, err := parseCommerceOffer("pack", topupOffer(map[string]any{"bucket": "missing"}), provider, credits, map[string]struct{}{})
			return err
		}},
		{"topup quantity", func() error {
			_, err := parseCommerceOffer("pack", topupOffer(map[string]any{"quantity": "invalid"}), provider, credits, map[string]struct{}{})
			return err
		}},
		{"topup lot behavior", func() error {
			_, err := parseCommerceOffer("pack", topupOffer(map[string]any{"lot_behavior": true}), provider, credits, map[string]struct{}{})
			return err
		}},
		{"topup expiry", func() error {
			_, err := parseCommerceOffer("pack", topupOffer(map[string]any{"expiry": "invalid"}), provider, credits, map[string]struct{}{})
			return err
		}},
		{"topup subscription expiry", func() error {
			_, err := parseCommerceOffer("pack", topupOffer(map[string]any{"expiry": map[string]any{"type": "subscription_end"}}), provider, credits, map[string]struct{}{})
			return err
		}},
		{"quantity minimum", func() error { _, err := parseOfferQuantity(map[string]any{"minimum": "one"}, "quantity"); return err }},
		{"quantity maximum", func() error { _, err := parseOfferQuantity(map[string]any{"maximum": "one"}, "quantity"); return err }},
		{"quantity default", func() error { _, err := parseOfferQuantity(map[string]any{"default": "one"}, "quantity"); return err }},
		{"cycle amount", func() error { _, err := parseCycleGrant(map[string]any{}, "grant", credits); return err }},
		{"cycle bucket", func() error { _, err := parseCycleGrant(map[string]any{"amount": "1"}, "grant", credits); return err }},
		{"cycle renewal", func() error {
			_, err := parseCycleGrant(map[string]any{"amount": "1", "bucket": "general"}, "grant", credits)
			return err
		}},
		{"cycle expiry", func() error {
			_, err := parseCycleGrant(map[string]any{"amount": "1", "bucket": "general", "renewal": "replace", "expiry": "invalid"}, "grant", credits)
			return err
		}},
		{"subscription change policy", func() error { _, err := parseSubscriptionChanges(map[string]any{"upgrade": "invalid"}); return err }},
		{"subscription change proration", func() error {
			_, err := parseSubscriptionChanges(map[string]any{"upgrade": map[string]any{"effective": "immediate"}})
			return err
		}},
		{"subscription change payment failure", func() error {
			_, err := parseSubscriptionChanges(map[string]any{"upgrade": map[string]any{"effective": "immediate", "proration": "prorated", "payment_failure": true}})
			return err
		}},
		{"auto recharge topups type", func() error {
			raw := validAutoRecharge()
			raw["eligible_topups"] = "usd"
			_, err := parseAutoRecharge(raw, offers)
			return err
		}},
		{"auto recharge mixed currency", func() error {
			raw := validAutoRecharge()
			raw["eligible_topups"] = []any{"usd", "eur"}
			_, err := parseAutoRecharge(raw, offers)
			return err
		}},
		{"auto recharge threshold object", func() error {
			raw := validAutoRecharge()
			raw["balance_below"] = "invalid"
			_, err := parseAutoRecharge(raw, offers)
			return err
		}},
		{"auto recharge threshold minimum", func() error {
			raw := validAutoRecharge()
			raw["balance_below"] = map[string]any{"maximum": "5", "default": "3"}
			_, err := parseAutoRecharge(raw, offers)
			return err
		}},
		{"auto recharge threshold maximum", func() error {
			raw := validAutoRecharge()
			raw["balance_below"] = map[string]any{"minimum": "1", "default": "3"}
			_, err := parseAutoRecharge(raw, offers)
			return err
		}},
		{"auto recharge threshold default", func() error {
			raw := validAutoRecharge()
			raw["balance_below"] = map[string]any{"minimum": "1", "maximum": "5"}
			_, err := parseAutoRecharge(raw, offers)
			return err
		}},
		{"auto recharge threshold order", func() error {
			raw := validAutoRecharge()
			raw["balance_below"] = map[string]any{"minimum": "5", "maximum": "1", "default": "3"}
			_, err := parseAutoRecharge(raw, offers)
			return err
		}},
		{"auto recharge rearm type", func() error {
			raw := validAutoRecharge()
			raw["rearm_above"] = true
			_, err := parseAutoRecharge(raw, offers)
			return err
		}},
		{"auto recharge rearm order", func() error {
			raw := validAutoRecharge()
			raw["rearm_above"] = "5"
			_, err := parseAutoRecharge(raw, offers)
			return err
		}},
		{"auto recharge quantity", func() error {
			raw := validAutoRecharge()
			raw["quantity"] = "invalid"
			_, err := parseAutoRecharge(raw, offers)
			return err
		}},
		{"auto recharge offer quantity", func() error {
			raw := validAutoRecharge()
			raw["quantity"] = map[string]any{"minimum": 1, "maximum": 6, "default": 1}
			_, err := parseAutoRecharge(raw, offers)
			return err
		}},
		{"auto recharge limits", func() error {
			raw := validAutoRecharge()
			raw["limits"] = "invalid"
			_, err := parseAutoRecharge(raw, offers)
			return err
		}},
		{"auto recharge max purchases", func() error {
			raw := validAutoRecharge()
			raw["limits"].(map[string]any)["max_purchases"] = "two"
			_, err := parseAutoRecharge(raw, offers)
			return err
		}},
		{"auto recharge window", func() error {
			raw := validAutoRecharge()
			raw["limits"].(map[string]any)["window"] = "invalid"
			_, err := parseAutoRecharge(raw, offers)
			return err
		}},
		{"auto recharge assignment window", func() error {
			raw := validAutoRecharge()
			raw["limits"].(map[string]any)["window"] = map[string]any{"type": "plan_assignment", "interval": map[string]any{"unit": "month", "count": 1}}
			_, err := parseAutoRecharge(raw, offers)
			return err
		}},
		{"auto recharge max charge", func() error {
			raw := validAutoRecharge()
			raw["limits"].(map[string]any)["max_charge_minor"] = "1000"
			_, err := parseAutoRecharge(raw, offers)
			return err
		}},
		{"auto recharge cooldown", func() error {
			raw := validAutoRecharge()
			raw["limits"].(map[string]any)["cooldown"] = "invalid"
			_, err := parseAutoRecharge(raw, offers)
			return err
		}},
		{"auto recharge failure count", func() error {
			raw := validAutoRecharge()
			raw["limits"].(map[string]any)["max_consecutive_failures"] = "three"
			_, err := parseAutoRecharge(raw, offers)
			return err
		}},
	}

	if providerReferenceExternalID(ProviderReference{}) != "" {
		t.Fatal("empty provider reference gained an external identifier")
	}
	if empty, err := parseCommerce(nil, credits); err != nil || len(empty.Offers) != 0 || len(empty.Providers) != 0 {
		t.Fatalf("empty commerce config = %#v, %v", empty, err)
	}
	if _, err := parseAutoRecharge(validAutoRecharge(), offers); err != nil {
		t.Fatalf("valid auto-recharge policy failed: %v", err)
	}
	for _, test := range checks {
		t.Run(test.name, func(t *testing.T) {
			if err := test.check(); err == nil {
				t.Fatal("unsafe commerce policy was accepted")
			}
		})
	}
}
