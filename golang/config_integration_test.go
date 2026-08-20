// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import (
	"encoding/json"
	"math"
	"os"
	"path/filepath"
	"reflect"
	"testing"
	"time"
)

func TestCompletePricingConfigurationRoundTrip(t *testing.T) {
	filename := "../skills/bursar/assets/pricing.config.example.yaml"
	config, err := LoadConfigFile(filename)
	if err != nil {
		if typed, ok := AsBursarError(err); ok && typed.Unwrap() != nil {
			t.Fatalf("load complete pricing example: %v: %v", err, typed.Unwrap())
		}
		t.Fatalf("load complete pricing example: %v", err)
	}
	if config.Catalog.DefaultPlan == nil || *config.Catalog.DefaultPlan != "free" {
		t.Fatalf("default plan = %v, want free", config.Catalog.DefaultPlan)
	}
	if len(config.Pricing.Operations) != 2 || len(config.Pricing.RateCards) != 2 {
		t.Fatalf("pricing = %+v", config.Pricing)
	}
	if len(config.Credits.GrantPrograms) != 1 || len(config.Entitlements.Features) != 4 || len(config.Plans) != 2 {
		t.Fatalf("catalog sections were not retained: credits=%+v entitlements=%+v plans=%+v", config.Credits, config.Entitlements, config.Plans)
	}
	if config.Commerce.AutoRecharge == nil || len(config.Commerce.Offers) != 2 || len(config.Commerce.SubscriptionChanges) != 4 {
		t.Fatalf("commerce = %+v", config.Commerce)
	}

	canonical := CanonicalParsedBursarConfigDict(config)
	parsedAgain, err := LoadConfigFromDict(canonical)
	if err != nil {
		t.Fatalf("load canonical parsed configuration: %v", err)
	}
	if !reflect.DeepEqual(CanonicalParsedBursarConfigDict(parsedAgain), canonical) {
		t.Fatal("canonical parsed configuration changed after a round trip")
	}
	normalized, err := CanonicalBursarConfigDict(canonical)
	if err != nil {
		t.Fatalf("canonicalize configuration: %v", err)
	}
	alias, err := CanonicalConfig(canonical)
	if err != nil {
		t.Fatalf("canonicalize through alias: %v", err)
	}
	if !reflect.DeepEqual(normalized, alias) || !reflect.DeepEqual(normalized, canonical) {
		t.Fatal("canonical configuration aliases diverged")
	}

	encoded, err := json.Marshal(canonical)
	if err != nil {
		t.Fatal(err)
	}
	fromJSON, err := LoadConfigJSON(encoded)
	if err != nil {
		t.Fatalf("load canonical JSON: %v", err)
	}
	if !reflect.DeepEqual(CanonicalParsedBursarConfigDict(fromJSON), canonical) {
		t.Fatal("canonical JSON changed after a round trip")
	}

	temporaryJSON := filepath.Join(t.TempDir(), "pricing.json")
	if err := os.WriteFile(temporaryJSON, encoded, 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := LoadConfigFile(temporaryJSON); err != nil {
		t.Fatalf("load JSON file: %v", err)
	}
	if _, err := LoadConfigFile(filepath.Join(t.TempDir(), "missing.yaml")); err == nil {
		t.Fatal("missing configuration file was accepted")
	}
	unsupported := filepath.Join(t.TempDir(), "pricing.txt")
	if err := os.WriteFile(unsupported, encoded, 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := LoadConfigFile(unsupported); err == nil {
		t.Fatal("unsupported configuration extension was accepted")
	}
}

func TestConfigurationDefensiveDecoders(t *testing.T) {
	if _, err := LoadConfigFromMap(nil); err == nil {
		t.Fatal("nil configuration was accepted")
	}
	for name, data := range map[string][]byte{
		"trailing JSON":    []byte(`{"version":1,"credits":{}} true`),
		"JSON array":       []byte(`[]`),
		"truncated JSON":   []byte(`{"version":`),
		"YAML scalar":      []byte(`value`),
		"YAML alias":       []byte("base: &base {value: true}\ncopy: *base\n"),
		"YAML numeric key": []byte("1: value\n"),
	} {
		t.Run(name, func(t *testing.T) {
			var err error
			if name[:4] == "YAML" {
				_, err = LoadConfigYAML(data)
			} else {
				_, err = LoadConfigJSON(data)
			}
			if err == nil {
				t.Fatal("invalid configuration was accepted")
			}
		})
	}

	regexp, err := compileSchemaRegexp(`^(?!bad$)[a-z]+$`)
	if err != nil {
		t.Fatal(err)
	}
	if regexp.String() == "" || !regexp.MatchString("good") || regexp.MatchString("bad") {
		t.Fatal("schema regexp adapter mismatch")
	}
	if _, err := compileSchemaRegexp("["); err == nil {
		t.Fatal("invalid schema regexp compiled")
	}
}

func TestCatalogRolloutCanonicalRoundTrip(t *testing.T) {
	raw := map[string]any{"plans": map[string]any{
		"upgrade": map[string]any{"effective": "immediate", "include_pinned": true},
		"renewal": map[string]any{"effective": "next_renewal", "include_pinned": false},
		"new":     map[string]any{"effective": "new_assignments_only"},
	}}
	rollout, err := LoadCatalogRollout(raw)
	if err != nil {
		t.Fatal(err)
	}
	if len(rollout.Plans) != 3 || !rollout.Plans["upgrade"].IncludePinned {
		t.Fatalf("rollout = %+v", rollout)
	}
	canonical, err := CanonicalCatalogRolloutDict(raw)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := LoadCatalogRollout(canonical); err != nil {
		t.Fatalf("canonical rollout was rejected: %v", err)
	}
	for name, invalid := range map[string]map[string]any{
		"non-object plans":  {"plans": "invalid"},
		"unknown effective": {"plans": map[string]any{"pro": map[string]any{"effective": "later"}}},
		"unknown field":     {"plans": map[string]any{"pro": map[string]any{"effective": "immediate", "extra": true}}},
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := LoadCatalogRollout(invalid); err == nil {
				t.Fatal("invalid rollout was accepted")
			}
		})
	}
}

func TestConfigurationSupportsEveryPricingAndWindowVariant(t *testing.T) {
	base, err := LoadConfigFile("../skills/bursar/assets/pricing.config.example.yaml")
	if err != nil {
		t.Fatal(err)
	}
	raw := CanonicalParsedBursarConfigDict(base)
	pricing := raw["pricing"].(map[string]any)
	operations := pricing["operations"].(map[string]any)
	completion := operations["completion"].(map[string]any)
	dimensions := completion["dimensions"].(map[string]any)
	dimensions["temperature"] = map[string]any{"type": "number", "required": false}
	dimensions["cached"] = map[string]any{"type": "boolean", "required": false}
	rateCards := pricing["rate_cards"].(map[string]any)
	standard := rateCards["standard"].(map[string]any)
	pricedCompletion := standard["operations"].(map[string]any)["completion"].(map[string]any)
	pricedCompletion["rules"] = []any{
		map[string]any{"when": map[string]any{"model": map[string]any{"op": "eq", "value": "flat"}}, "charge": map[string]any{"type": "flat", "amount": "1.25"}},
		map[string]any{"when": map[string]any{"model": map[string]any{"op": "not_in", "values": []any{"free", "legacy"}}}, "charge": map[string]any{"type": "package", "measure": "input_tokens", "units": "1000", "amount": "2", "rounding": "ceil"}},
		map[string]any{"when": map[string]any{"temperature": map[string]any{"op": "range", "gte": "0", "lt": "1"}}, "charge": map[string]any{"type": "graduated", "measure": "input_tokens", "tiers": []any{map[string]any{"up_to": "1000", "rate": "0.1"}, map[string]any{"up_to": nil, "rate": "0.05"}}}},
		map[string]any{"when": map[string]any{"model": map[string]any{"op": "prefix", "value": "gpt-"}}, "charge": map[string]any{"type": "volume", "measure": "output_tokens", "tiers": []any{map[string]any{"up_to": "100", "rate": "0.2"}, map[string]any{"up_to": nil, "rate": "0.1"}}}},
		map[string]any{"when": map[string]any{"cached": map[string]any{"op": "eq", "value": true}}, "charge": map[string]any{"type": "sum", "components": []any{map[string]any{"type": "flat", "amount": "0.5"}, map[string]any{"type": "expression", "formula": "input_tokens * 0.001"}}}},
	}

	credits := raw["credits"].(map[string]any)
	buckets := credits["buckets"].(map[string]any)
	buckets["never"] = map[string]any{"priority": 20, "expiry": map[string]any{"type": "never"}}
	buckets["after"] = map[string]any{"priority": 21, "expiry": map[string]any{"type": "after_grant", "interval": map[string]any{"unit": "week", "count": 2}, "timezone": "Asia/Kolkata"}}
	buckets["window"] = map[string]any{"priority": 22, "expiry": map[string]any{"type": "end_of_window", "window": map[string]any{"type": "plan_assignment", "interval": map[string]any{"unit": "month", "count": 1}, "timezone": "UTC"}}}
	program := credits["grant_programs"].(map[string]any)["welcome"].(map[string]any)
	program["availability"] = map[string]any{"starts_at": "2026-01-01T00:00:00Z", "ends_at": "2027-01-01T00:00:00Z", "regions": []any{"US", "IN"}}
	program["eligibility"] = map[string]any{"plans": []any{"free", "pro"}, "regions": []any{"US"}}

	plans := raw["plans"].(map[string]any)
	free := plans["free"].(map[string]any)
	free["credit_allowance"] = map[string]any{"amount": "10000", "priority": 5, "window": map[string]any{"type": "rolling", "duration": map[string]any{"unit": "day", "count": 30}}}
	pro := plans["pro"].(map[string]any)
	pro["quotas"] = map[string]any{"assignment_tokens": map[string]any{"operation": "completion", "measure": "output_tokens", "limit": "500000", "window": map[string]any{"type": "plan_assignment", "interval": map[string]any{"unit": "month", "count": 1}, "timezone": "UTC"}, "enforcement": "allow", "emit_at_percent": []any{50, 90}}}

	commerce := raw["commerce"].(map[string]any)
	providers := commerce["providers"].(map[string]any)
	providers["dodo"] = map[string]any{"type": "dodo"}
	providers["custom"] = map[string]any{"type": "custom", "adapter": "custom"}
	for _, offerKey := range []string{"pro_monthly", "credits_10k"} {
		offer := commerce["offers"].(map[string]any)[offerKey].(map[string]any)
		offer["availability"] = map[string]any{"regions": []any{"US", "IN"}}
		providerRefs := offer["providers"].(map[string]any)
		providerRefs["dodo"] = map[string]any{"type": "dodo_product", "product_id": "product_" + offerKey}
		objectKind := "subscription"
		if offerKey == "credits_10k" {
			objectKind = "one_time"
		}
		providerRefs["custom"] = map[string]any{"type": "custom_object", "object_kind": objectKind, "external_id": "custom_" + offerKey}
	}

	config, err := LoadConfigFromMap(raw)
	if err != nil {
		if typed, ok := AsBursarError(err); ok && typed.Unwrap() != nil {
			t.Fatalf("load complete variant configuration: %v: %v", err, typed.Unwrap())
		}
		t.Fatalf("load complete variant configuration: %v", err)
	}
	if config.Pricing == nil || len(config.Pricing.RateCards["standard"].Operations["completion"].Rules) != 5 {
		t.Fatalf("pricing variants = %+v", config.Pricing)
	}
	if !ResolvesOperation(config.Pricing, "batch", "execution") {
		t.Fatal("inherited rate-card operation did not resolve")
	}
	if ResolvesOperation(config.Pricing, "standard", "missing") || ResolvesOperation(nil, "standard", "completion") {
		t.Fatal("unknown rate-card operation resolved")
	}
	canonical := CanonicalParsedBursarConfigDict(config)
	if _, err := LoadConfigFromMap(canonical); err != nil {
		t.Fatalf("canonical variant configuration was rejected: %v", err)
	}
}

func TestConfigurationRejectsUnsafeTemporalPolicies(t *testing.T) {
	validAvailability, err := parseAvailability(map[string]any{
		"starts_at": "2026-01-01T00:00:00Z", "ends_at": "2026-02-01T00:00:00Z", "regions": []any{"US", "IN"},
	}, "availability")
	if err != nil || len(validAvailability.Regions) != 2 {
		t.Fatalf("valid availability = %+v, %v", validAvailability, err)
	}
	for name, value := range map[string]any{
		"duplicate regions": map[string]any{"regions": []any{"US", "US"}},
		"lowercase region":  map[string]any{"regions": []any{"us"}},
		"reversed dates":    map[string]any{"starts_at": "2026-02-01T00:00:00Z", "ends_at": "2026-01-01T00:00:00Z"},
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := parseAvailability(value, "availability"); err == nil {
				t.Fatal("unsafe availability was accepted")
			}
		})
	}

	windows := []map[string]any{
		{"type": "calendar", "unit": "week", "count": 2, "timezone": "Asia/Kolkata"},
		{"type": "rolling", "duration": map[string]any{"unit": "hour", "count": 24}},
		{"type": "plan_assignment", "interval": map[string]any{"unit": "month", "count": 1}},
	}
	for _, value := range windows {
		window, err := parseWindow(value, "window")
		if err != nil {
			t.Fatal(err)
		}
		if canonicalWindow(window)["type"] != window.Type {
			t.Fatalf("canonical window = %+v, parsed = %+v", canonicalWindow(window), window)
		}
	}
	if _, err := parseWindow(map[string]any{"type": "unknown"}, "window"); err == nil {
		t.Fatal("unknown window was accepted")
	}
	if _, err := parseWindow(map[string]any{"type": "calendar", "unit": "day", "timezone": "Local"}, "window"); err == nil {
		t.Fatal("unsafe local timezone was accepted")
	}
	if _, err := parseExpiry(map[string]any{"type": "end_of_window", "window": map[string]any{"type": "rolling", "duration": map[string]any{"unit": "day", "count": 1}}}, "expiry"); err == nil {
		t.Fatal("rolling end-of-window expiry was accepted")
	}
	if _, err := ValidateCatalogRollout(nil, CatalogRollout{}); err == nil {
		t.Fatal("rollout without a catalog was accepted")
	}
}

func TestConfigurationNumericBoundariesPreserveExactTypes(t *testing.T) {
	integers := []any{
		json.Number("7"), int(7), int8(7), int16(7), int32(7), int64(7),
		uint(7), uint8(7), uint16(7), uint32(7), uint64(7), float64(7),
	}
	for _, value := range integers {
		parsed, err := configInt64(value, "value")
		if err != nil || parsed != 7 {
			t.Fatalf("configInt64(%T) = %d, %v", value, parsed, err)
		}
	}
	for name, value := range map[string]any{
		"fraction":           1.5,
		"nan":                math.NaN(),
		"infinity":           math.Inf(1),
		"invalid JSON":       json.Number("1.5"),
		"unsafe float":       float64(safeIntegerMax) + 2,
		"unsafe signed":      int64(safeIntegerMax + 1),
		"oversized uint":     uint64(math.MaxInt64) + 1,
		"unsupported scalar": "7",
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := configInt64(value, "value"); err == nil {
				t.Fatalf("configInt64(%v) succeeded; want error", value)
			}
		})
	}

	decimals := []any{
		"1.25", json.Number("1.25"), int(1), int8(1), int16(1), int32(1), int64(1),
		uint(1), uint8(1), uint16(1), uint32(1), uint64(1), float64(1.25),
	}
	for _, value := range decimals {
		if _, err := configMatcherDecimal(value, "matcher"); err != nil {
			t.Fatalf("configMatcherDecimal(%T) error = %v", value, err)
		}
	}
	for name, value := range map[string]any{
		"invalid string": "not-a-number",
		"invalid JSON":   json.Number("not-a-number"),
		"nan":            math.NaN(),
		"infinity":       math.Inf(-1),
		"unsupported":    true,
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := configMatcherDecimal(value, "matcher"); err == nil {
				t.Fatalf("configMatcherDecimal(%v) succeeded; want error", value)
			}
		})
	}
}

func TestConfigurationContainerNormalizationIsDetached(t *testing.T) {
	instant := time.Date(2026, time.August, 19, 12, 30, 0, 123, time.UTC)
	normalized := normalizeYAMLValue(map[any]any{
		"instant": instant,
		"nested":  map[string]any{"items": []any{map[any]any{"value": true}}},
	}).(map[string]any)
	if normalized["instant"] != instant.Format(time.RFC3339Nano) {
		t.Fatalf("normalized instant = %#v", normalized["instant"])
	}
	if normalizeYAMLValue(map[any]any{1: "value"}) == nil {
		t.Fatal("non-string-key YAML map was discarded")
	}

	type stringSlice []string
	type stringMap map[string]stringSlice
	original := stringMap{"models": {"small", "large"}}
	converted := canonicalJSONValue(original).(map[string]any)
	items := converted["models"].([]any)
	items[0] = "changed"
	if original["models"][0] != "small" {
		t.Fatal("canonicalJSONValue aliased its input")
	}
	array := canonicalJSONValue([2]int{1, 2}).([]any)
	if len(array) != 2 || array[1] != 2 {
		t.Fatalf("canonical array = %#v", array)
	}
	if canonicalJSONValue(nil) != nil {
		t.Fatal("canonical nil changed value")
	}
}

func TestConfigurationMatcherAndRolloutBoundaries(t *testing.T) {
	for name, input := range map[string]map[string]any{
		"range requires number":  {"op": "range", "gte": "1"},
		"prefix requires string": {"op": "prefix", "value": "gpt"},
		"range requires bound":   {"op": "range"},
		"range duplicate lower":  {"op": "range", "gt": "1", "gte": "2"},
		"range duplicate upper":  {"op": "range", "lt": "3", "lte": "2"},
		"range ordered":          {"op": "range", "gte": "3", "lt": "2"},
		"unknown operation":      {"op": "regex", "value": "x"},
	} {
		t.Run(name, func(t *testing.T) {
			definition := DimensionDefinition{Type: "number"}
			if name == "range requires number" {
				definition.Type = "string"
			} else if name == "prefix requires string" {
				definition.Type = "number"
			}
			if _, err := parseDimensionMatcher(input, definition, "matcher"); err == nil {
				t.Fatal("unsafe matcher was accepted")
			}
		})
	}
	if _, err := parseMatcherScalar(true, DimensionDefinition{Type: "object"}, "matcher"); err == nil {
		t.Fatal("matcher accepted an unknown dimension type")
	}

	plan := "pro"
	config := &BursarConfig{
		Plans: map[string]PlanDefinition{"free": {}, "pro": {}},
		Commerce: CommerceConfig{Offers: map[string]CommerceOffer{
			"pro_monthly": {Type: "subscription", Plan: &plan},
		}},
	}
	valid := CatalogRollout{Plans: map[string]PlanRollout{"pro": {Effective: "next_renewal"}}}
	if _, err := ValidateCatalogRollout(config, valid); err != nil {
		t.Fatalf("valid renewal rollout error = %v", err)
	}
	if _, err := ValidateCatalogRollout(config, CatalogRollout{Plans: map[string]PlanRollout{"missing": {Effective: "immediate"}}}); err == nil {
		t.Fatal("rollout accepted an unknown plan")
	}
	if _, err := ValidateCatalogRollout(config, CatalogRollout{Plans: map[string]PlanRollout{"free": {Effective: "next_renewal"}}}); err == nil {
		t.Fatal("renewal rollout accepted a plan without a subscription offer")
	}
}

func TestConfigurationSectionParsersFailClosed(t *testing.T) {
	credits := CreditsConfig{Buckets: map[string]BucketDefinition{"purchased": {}}}
	checks := map[string]func() error{
		"array type":        func() error { _, err := configArray("items", "value"); return err },
		"string type":       func() error { _, err := configString(1, "value"); return err },
		"decimal type":      func() error { _, err := configDecimal(1, "value"); return err },
		"decimal syntax":    func() error { _, err := configDecimal("invalid", "value"); return err },
		"identifier syntax": func() error { return validateConfigIdentifier("Not_Snake", "value") },
		"identifier map":    func() error { return validateConfigIdentifiers(map[string]any{"bad-key": true}, "value") },
		"string slice item": func() error { _, err := configSliceStrings([]any{"ok", 1}, "value"); return err },
		"duration object":   func() error { _, err := parseDuration("day", "duration"); return err },
		"duration unit":     func() error { _, err := parseDuration(map[string]any{"count": 1}, "duration"); return err },
		"duration count": func() error {
			_, err := parseDuration(map[string]any{"unit": "day", "count": "one"}, "duration")
			return err
		},
		"interval object": func() error { _, err := parseBillingInterval("month", "interval"); return err },
		"interval unit":   func() error { _, err := parseBillingInterval(map[string]any{}, "interval"); return err },
		"interval count": func() error {
			_, err := parseBillingInterval(map[string]any{"unit": "month", "count": "one"}, "interval")
			return err
		},
		"timezone type":       func() error { _, err := parseTimezone(true, "timezone"); return err },
		"timezone empty":      func() error { _, err := parseTimezone("", "timezone"); return err },
		"timezone unknown":    func() error { _, err := parseTimezone("Mars/Olympus", "timezone"); return err },
		"window object":       func() error { _, err := parseWindow("rolling", "window"); return err },
		"window type":         func() error { _, err := parseWindow(map[string]any{}, "window"); return err },
		"rolling duration":    func() error { _, err := parseWindow(map[string]any{"type": "rolling"}, "window"); return err },
		"assignment interval": func() error { _, err := parseWindow(map[string]any{"type": "plan_assignment"}, "window"); return err },
		"calendar unit":       func() error { _, err := parseWindow(map[string]any{"type": "calendar"}, "window"); return err },
		"calendar count": func() error {
			_, err := parseWindow(map[string]any{"type": "calendar", "unit": "day", "count": "one"}, "window")
			return err
		},
		"availability object":  func() error { _, err := parseAvailability("all", "availability"); return err },
		"availability start":   func() error { _, err := parseAvailability(map[string]any{"starts_at": 1}, "availability"); return err },
		"availability end":     func() error { _, err := parseAvailability(map[string]any{"ends_at": 1}, "availability"); return err },
		"availability regions": func() error { _, err := parseAvailability(map[string]any{"regions": "US"}, "availability"); return err },
		"expiry object":        func() error { _, err := parseExpiry("never", "expiry"); return err },
		"expiry type":          func() error { _, err := parseExpiry(map[string]any{}, "expiry"); return err },
		"after grant interval": func() error { _, err := parseExpiry(map[string]any{"type": "after_grant"}, "expiry"); return err },
		"fixed expiry instant": func() error { _, err := parseExpiry(map[string]any{"type": "fixed_at"}, "expiry"); return err },
		"credits object":       func() error { _, err := parseCredits("credits"); return err },
		"credits buckets":      func() error { _, err := parseCredits(map[string]any{"buckets": "buckets"}); return err },
		"bucket definition": func() error {
			_, err := parseCredits(map[string]any{"buckets": map[string]any{"purchased": "bucket"}})
			return err
		},
		"bucket priority": func() error {
			_, err := parseCredits(map[string]any{"buckets": map[string]any{"purchased": map[string]any{}}})
			return err
		},
		"bucket expiry": func() error {
			_, err := parseCredits(map[string]any{"buckets": map[string]any{"purchased": map[string]any{"priority": 1, "expiry": map[string]any{"type": "subscription_end"}}}})
			return err
		},
		"bucket priorities": func() error {
			_, err := parseCredits(map[string]any{"buckets": map[string]any{
				"purchased": map[string]any{"priority": 1}, "gifted": map[string]any{"priority": 1},
			}})
			return err
		},
		"default bucket": func() error { _, err := parseCredits(map[string]any{"default_bucket": "missing"}); return err },
		"credit-line limit": func() error {
			_, err := parseCredits(map[string]any{"policies": map[string]any{"postpaid": map[string]any{"type": "credit_line"}}})
			return err
		},
		"credit display":       func() error { _, err := parseCredits(map[string]any{"display": map[string]any{}}); return err },
		"grant program object": func() error { _, err := parseGrantProgram("welcome", "program", credits.Buckets); return err },
		"grant trigger":        func() error { _, err := parseGrantProgram("welcome", map[string]any{}, credits.Buckets); return err },
		"grant awards": func() error {
			_, err := parseGrantProgram("welcome", map[string]any{"trigger": "signup", "awards": "award"}, credits.Buckets)
			return err
		},
		"grant unknown bucket": func() error {
			_, err := parseGrantProgram("welcome", map[string]any{"trigger": "signup", "awards": []any{map[string]any{"amount": "1", "bucket": "missing"}}}, credits.Buckets)
			return err
		},
		"entitlements object":  func() error { _, err := parseEntitlements("features"); return err },
		"entitlement features": func() error { _, err := parseEntitlements(map[string]any{"features": "features"}); return err },
		"boolean entitlement": func() error {
			_, err := parseEntitlements(map[string]any{"features": map[string]any{"enabled": map[string]any{"type": "boolean", "default": "yes"}}})
			return err
		},
		"enum entitlement": func() error {
			_, err := parseEntitlements(map[string]any{"features": map[string]any{"tier": map[string]any{"type": "enum", "values": []any{"a", "a"}, "default": "a"}}})
			return err
		},
		"integer entitlement": func() error {
			_, err := parseEntitlements(map[string]any{"features": map[string]any{"seats": map[string]any{"type": "integer", "default": 2, "minimum": 3}}})
			return err
		},
		"string entitlement regex": func() error {
			_, err := parseEntitlements(map[string]any{"features": map[string]any{"model": map[string]any{"type": "string", "default": "x", "pattern": "["}}})
			return err
		},
		"unknown entitlement": func() error {
			_, err := parseEntitlements(map[string]any{"features": map[string]any{"value": map[string]any{"type": "number"}}})
			return err
		},
		"admission object":   func() error { _, err := parseAdmission("policy"); return err },
		"admission policies": func() error { _, err := parseAdmission(map[string]any{"policies": "policy"}); return err },
		"admission operation": func() error {
			_, err := parseAdmission(map[string]any{"policies": map[string]any{"standard": map[string]any{"operations": map[string]any{"usage": "policy"}}}})
			return err
		},
		"tiers array": func() error { _, err := parseTiers("tiers", "tiers"); return err },
		"tiers empty": func() error { _, err := parseTiers([]any{}, "tiers"); return err },
		"tiers order": func() error {
			_, err := parseTiers([]any{
				map[string]any{"up_to": "10", "rate": "1"},
				map[string]any{"up_to": "5", "rate": "1"},
				map[string]any{"up_to": nil, "rate": "1"},
			}, "tiers")
			return err
		},
		"charge depth":  func() error { _, err := parseCharge(map[string]any{}, "charge", 65); return err },
		"charge object": func() error { _, err := parseCharge("charge", "charge", 0); return err },
		"charge type":   func() error { _, err := parseCharge(map[string]any{}, "charge", 0); return err },
		"charge per-unit zero": func() error {
			_, err := parseCharge(map[string]any{"type": "per_unit", "measure": "units", "rate": "1", "unit_size": "0"}, "charge", 0)
			return err
		},
		"charge unknown":            func() error { _, err := parseCharge(map[string]any{"type": "unknown"}, "charge", 0); return err },
		"provider reference object": func() error { _, err := parseProviderReference("reference", "provider"); return err },
		"provider reference type":   func() error { _, err := parseProviderReference(map[string]any{}, "provider"); return err },
		"provider reference unknown": func() error {
			_, err := parseProviderReference(map[string]any{"type": "unknown"}, "provider")
			return err
		},
		"offer quantity object": func() error { _, err := parseOfferQuantity("quantity", "quantity"); return err },
		"offer quantity range": func() error {
			_, err := parseOfferQuantity(map[string]any{"minimum": 3, "maximum": 2}, "quantity")
			return err
		},
		"cycle grant object": func() error { _, err := parseCycleGrant("grant", "grant", credits); return err },
		"cycle grant bucket": func() error {
			_, err := parseCycleGrant(map[string]any{"amount": "1", "bucket": "missing", "renewal": "replace"}, "grant", credits)
			return err
		},
		"subscription changes object": func() error { _, err := parseSubscriptionChanges("changes"); return err },
		"subscription change effective": func() error {
			_, err := parseSubscriptionChanges(map[string]any{"upgrade": map[string]any{}})
			return err
		},
		"auto recharge object": func() error { _, err := parseAutoRecharge("auto", nil); return err },
		"auto recharge topup": func() error {
			_, err := parseAutoRecharge(map[string]any{"eligible_topups": []any{"missing"}}, nil)
			return err
		},
	}
	for name, check := range checks {
		t.Run(name, func(t *testing.T) {
			if err := check(); err == nil {
				t.Fatal("invalid financial configuration was accepted")
			}
		})
	}
}

func TestConfigurationPricingAndPlanParsersFailClosed(t *testing.T) {
	flat := Charge{Type: "flat", Amount: MustAmount("1")}
	pricing := &PricingConfig{
		Operations: map[string]OperationDefinition{
			"usage": {Measures: map[string]MeasureDefinition{"units": {Unit: "unit"}}, Dimensions: map[string]DimensionDefinition{}},
		},
		RateCards: map[string]RateCard{
			"standard": {Operations: map[string]OperationPricing{
				"usage": {Unmatched: UnmatchedPolicy{Action: "charge", Charge: &flat}},
			}},
		},
	}
	minimum, maximum := 1, 3
	pattern := `^gpt-`
	entitlements := EntitlementsConfig{Features: map[string]FeatureDefinition{
		"enabled": {Type: "boolean"},
		"seats":   {Type: "integer", Minimum: &minimum, Maximum: &maximum},
		"tier":    {Type: "enum", Values: []string{"free", "pro"}},
		"model":   {Type: "string", Pattern: &pattern},
	}}
	credits := CreditsConfig{
		Buckets:  map[string]BucketDefinition{"purchased": {Priority: 1}},
		Policies: map[string]CreditPolicy{"prepaid": {Type: "prepaid"}},
	}
	defaultBucket := "purchased"
	credits.DefaultBucket = &defaultBucket
	admission := AdmissionConfig{Policies: map[string]AdmissionPolicy{"standard": {}}}

	checks := map[string]func() error{
		"pricing object":     func() error { _, err := parsePricing("pricing"); return err },
		"pricing operations": func() error { _, err := parsePricing(map[string]any{"operations": "operations"}); return err },
		"pricing rate cards": func() error {
			_, err := parsePricing(map[string]any{"operations": map[string]any{}, "rate_cards": "cards"})
			return err
		},
		"pricing empty": func() error { _, err := parsePricing(map[string]any{}); return err },
		"operation object": func() error {
			_, err := parsePricing(map[string]any{"operations": map[string]any{"usage": "operation"}, "rate_cards": map[string]any{"standard": map[string]any{}}})
			return err
		},
		"operation measures": func() error {
			_, err := parsePricing(map[string]any{"operations": map[string]any{"usage": map[string]any{"measures": "measures"}}, "rate_cards": map[string]any{"standard": map[string]any{}}})
			return err
		},
		"operation empty measures": func() error {
			_, err := parsePricing(map[string]any{"operations": map[string]any{"usage": map[string]any{}}, "rate_cards": map[string]any{"standard": map[string]any{}}})
			return err
		},
		"measure unit": func() error {
			_, err := parsePricing(map[string]any{"operations": map[string]any{"usage": map[string]any{"measures": map[string]any{"units": map[string]any{}}}}, "rate_cards": map[string]any{"standard": map[string]any{}}})
			return err
		},
		"measure dimension overlap": func() error {
			_, err := parsePricing(map[string]any{"operations": map[string]any{"usage": map[string]any{
				"measures": map[string]any{"units": map[string]any{"unit": "unit"}}, "dimensions": map[string]any{"units": map[string]any{"type": "number"}},
			}}, "rate_cards": map[string]any{"standard": map[string]any{}}})
			return err
		},
		"dimension required": func() error {
			_, err := parsePricing(map[string]any{"operations": map[string]any{"usage": map[string]any{
				"measures": map[string]any{"units": map[string]any{"unit": "unit"}}, "dimensions": map[string]any{"model": map[string]any{"type": "string", "required": "yes"}},
			}}, "rate_cards": map[string]any{"standard": map[string]any{}}})
			return err
		},
		"plan object": func() error {
			_, err := parsePlans("plans", pricing, credits, entitlements, admission, CommerceConfig{})
			return err
		},
		"plan definition": func() error {
			_, err := parsePlans(map[string]any{"free": "plan"}, pricing, credits, entitlements, admission, CommerceConfig{})
			return err
		},
		"plan display name": func() error {
			_, err := parsePlans(map[string]any{"free": map[string]any{}}, pricing, credits, entitlements, admission, CommerceConfig{})
			return err
		},
		"plan rate card without pricing": func() error {
			_, err := parsePlans(map[string]any{"free": map[string]any{"display_name": "Free", "rate_card": "standard"}}, nil, credits, entitlements, admission, CommerceConfig{})
			return err
		},
		"plan unknown rate card": func() error {
			_, err := parsePlans(map[string]any{"free": map[string]any{"display_name": "Free", "rate_card": "missing"}}, pricing, credits, entitlements, admission, CommerceConfig{})
			return err
		},
		"plan duplicate operation": func() error {
			_, err := parsePlans(map[string]any{"free": map[string]any{"display_name": "Free", "rate_card": "standard", "allowed_operations": []any{"usage", "usage"}}}, pricing, credits, entitlements, admission, CommerceConfig{})
			return err
		},
		"plan unknown feature": func() error {
			_, err := parsePlans(map[string]any{"free": map[string]any{"display_name": "Free", "features": map[string]any{"missing": true}}}, pricing, credits, entitlements, admission, CommerceConfig{})
			return err
		},
		"plan credit policy": func() error {
			_, err := parsePlans(map[string]any{"free": map[string]any{"display_name": "Free", "credit_policy": "missing"}}, pricing, credits, entitlements, admission, CommerceConfig{})
			return err
		},
		"plan admission policy": func() error {
			_, err := parsePlans(map[string]any{"free": map[string]any{"display_name": "Free", "admission_policy": "missing"}}, pricing, credits, entitlements, admission, CommerceConfig{})
			return err
		},
		"plan evolution": func() error {
			_, err := parsePlans(map[string]any{"free": map[string]any{"display_name": "Free", "evolution": map[string]any{"default_rollout": "later"}}}, pricing, credits, entitlements, admission, CommerceConfig{})
			return err
		},
		"feature boolean": func() error {
			_, err := validatePlanFeatureValue("true", entitlements.Features["enabled"], "feature")
			return err
		},
		"feature integer low": func() error {
			_, err := validatePlanFeatureValue(0, entitlements.Features["seats"], "feature")
			return err
		},
		"feature integer high": func() error {
			_, err := validatePlanFeatureValue(4, entitlements.Features["seats"], "feature")
			return err
		},
		"feature enum": func() error {
			_, err := validatePlanFeatureValue("enterprise", entitlements.Features["tier"], "feature")
			return err
		},
		"feature pattern": func() error {
			_, err := validatePlanFeatureValue("claude", entitlements.Features["model"], "feature")
			return err
		},
		"feature unknown": func() error {
			_, err := validatePlanFeatureValue(true, FeatureDefinition{Type: "number"}, "feature")
			return err
		},
		"quota object":    func() error { _, err := parseQuota("quota", "quota", pricing); return err },
		"quota operation": func() error { _, err := parseQuota(map[string]any{}, "quota", pricing); return err },
		"quota measure":   func() error { _, err := parseQuota(map[string]any{"operation": "usage"}, "quota", pricing); return err },
		"quota unknown measure": func() error {
			_, err := parseQuota(map[string]any{"operation": "usage", "measure": "missing"}, "quota", pricing)
			return err
		},
		"quota thresholds": func() error {
			_, err := parseQuota(map[string]any{"operation": "usage", "measure": "units", "limit": "10", "window": map[string]any{"type": "calendar", "unit": "month"}, "enforcement": "deny", "emit_at_percent": []any{90, 50}}, "quota", pricing)
			return err
		},
		"allowance object": func() error { _, err := parseCreditAllowance("allowance", "allowance"); return err },
		"allowance priority": func() error {
			_, err := parseCreditAllowance(map[string]any{"amount": "1", "priority": -1}, "allowance")
			return err
		},
		"commerce object":    func() error { _, err := parseCommerce("commerce", credits); return err },
		"commerce providers": func() error { _, err := parseCommerce(map[string]any{"providers": "providers"}, credits); return err },
		"commerce provider": func() error {
			_, err := parseCommerce(map[string]any{"providers": map[string]any{"stripe": "provider"}}, credits)
			return err
		},
	}
	for name, check := range checks {
		t.Run(name, func(t *testing.T) {
			if err := check(); err == nil {
				t.Fatal("invalid pricing or plan configuration was accepted")
			}
		})
	}
}
