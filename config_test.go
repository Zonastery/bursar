package bursar

import (
	"bytes"
	"encoding/json"
	"os"
	"testing"
)

func TestConfigValidationParityFixture(t *testing.T) {
	contents, err := os.ReadFile("tests/parity/config_validation_cases.json")
	if err != nil {
		t.Fatalf("read shared validation fixture: %v", err)
	}
	var fixture struct {
		Cases []struct {
			Name   string          `json:"name"`
			Expect string          `json:"expect"`
			Config json.RawMessage `json:"config"`
		} `json:"cases"`
	}
	if err := json.Unmarshal(contents, &fixture); err != nil {
		t.Fatalf("decode shared validation fixture envelope: %v", err)
	}
	for _, testCase := range fixture.Cases {
		t.Run(testCase.Name, func(t *testing.T) {
			decoder := json.NewDecoder(bytes.NewReader(testCase.Config))
			decoder.UseNumber()
			var raw map[string]any
			if err := decoder.Decode(&raw); err != nil {
				t.Fatalf("decode fixture config: %v", err)
			}
			parsed, err := LoadConfigFromMap(raw)
			if testCase.Expect == "accept" && err != nil {
				t.Fatalf("config rejected: %v", err)
			}
			if testCase.Expect == "accept" {
				canonical := CanonicalParsedBursarConfigDict(parsed)
				if _, err := LoadConfigFromMap(canonical); err != nil {
					if typed, ok := AsBursarError(err); ok && typed.Unwrap() != nil {
						t.Fatalf("canonical config rejected: %v: %v", err, typed.Unwrap())
					}
					t.Fatalf("canonical config rejected: %v", err)
				}
			}
			if testCase.Expect == "reject" && err == nil {
				t.Fatal("config accepted; expected rejection")
			}
		})
	}
}

func TestConfigYAMLUsesSameValidationBoundary(t *testing.T) {
	config, err := LoadConfigYAML([]byte("version: 1\ncredits: {}\n"))
	if err != nil {
		t.Fatalf("valid YAML config rejected: %v", err)
	}
	if config.Version != 1 {
		t.Fatalf("version = %d, want 1", config.Version)
	}
	if _, err := LoadConfigYAML([]byte("version: 1\nversion: 1\ncredits: {}\n")); err == nil {
		t.Fatal("duplicate YAML keys accepted")
	}
	if _, err := LoadConfigYAML([]byte("version: 1\ncredits:\n  display:\n    currency: USD\n    units_per_major: 1.5\n")); err == nil {
		t.Fatal("numeric YAML decimal accepted")
	}
	if _, err := LoadConfigJSON([]byte(`{"version":1,"credits":{},"credits":{}}`)); err == nil {
		t.Fatal("duplicate JSON keys accepted")
	}
	if _, err := LoadConfigJSON([]byte(`{"version":1,"credits":{"buckets":{},"buckets":{}}}`)); err == nil {
		t.Fatal("nested duplicate JSON keys accepted")
	}
}

func TestCatalogRolloutValidation(t *testing.T) {
	rollout, err := LoadCatalogRollout(map[string]any{
		"plans": map[string]any{
			"pro": map[string]any{"effective": "immediate", "include_pinned": true},
		},
	})
	if err != nil {
		t.Fatalf("load rollout: %v", err)
	}
	if !rollout.Plans["pro"].IncludePinned {
		t.Fatal("include_pinned was not retained")
	}
	if _, err := LoadCatalogRollout(map[string]any{
		"plans": map[string]any{
			"pro": map[string]any{"effective": "immediate", "include_pinned": true, "includePinned": false},
		},
	}); err == nil {
		t.Fatal("ambiguous rollout aliases accepted")
	}
}

func TestConfigAcceptsTypedGoContainersWithoutFloatCoercion(t *testing.T) {
	config, err := LoadConfigFromMap(map[string]any{
		"version": 1,
		"credits": map[string]any{},
		"catalog": map[string]any{"default_plan": "free"},
		"plans": map[string]any{
			"free": map[string]any{
				"display_name":       "Free",
				"allowed_operations": []string{},
			},
		},
	})
	if err != nil {
		t.Fatalf("typed Go config rejected: %v", err)
	}
	if config.Plans["free"].DisplayName != "Free" {
		t.Fatalf("parsed plan = %#v", config.Plans["free"])
	}
}
