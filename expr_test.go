package bursar

import (
	"bytes"
	"encoding/json"
	"os"
	"testing"
)

func TestExpressionParityFixture(t *testing.T) {
	contents, err := os.ReadFile("tests/parity/expression_cases.json")
	if err != nil {
		t.Fatalf("read shared parity fixture: %v", err)
	}
	var fixture struct {
		ExpressionCases []struct {
			Name        string                 `json:"name"`
			Expression  string                 `json:"expr"`
			Variables   map[string]json.Number `json:"vars"`
			Expected    string                 `json:"expected"`
			ExpectError bool                   `json:"expect_error"`
		} `json:"expression_cases"`
	}
	decoder := json.NewDecoder(bytes.NewReader(contents))
	decoder.UseNumber()
	if err := decoder.Decode(&fixture); err != nil {
		t.Fatalf("decode shared parity fixture: %v", err)
	}
	for _, testCase := range fixture.ExpressionCases {
		t.Run(testCase.Name, func(t *testing.T) {
			variables := make(map[string]Amount, len(testCase.Variables))
			for name, value := range testCase.Variables {
				amount, err := NewAmount(value.String())
				if err != nil {
					t.Fatalf("parse fixture variable %s: %v", name, err)
				}
				variables[name] = amount
			}
			actual, err := EvaluateExpression(testCase.Expression, variables)
			if testCase.ExpectError {
				if err == nil {
					t.Fatalf("EvaluateExpression(%q) succeeded; expected error", testCase.Expression)
				}
				return
			}
			if err != nil {
				t.Fatalf("EvaluateExpression(%q): %v", testCase.Expression, err)
			}
			if got, want := QuantizeMoney(actual).StringFixed(MoneyDecimalPlaces), testCase.Expected; got != want {
				t.Fatalf("result = %s, want %s", got, want)
			}
		})
	}
}

func TestValidateExpressionRejectsUnknownAndConstantOnly(t *testing.T) {
	if err := ValidateExpression("input_tokens * 0.1", []string{"input_tokens"}); err != nil {
		t.Fatalf("valid expression rejected: %v", err)
	}
	if err := ValidateExpression("missing * 0.1", []string{"input_tokens"}); err == nil {
		t.Fatal("unknown variable accepted")
	}
	if err := ValidateExpression("1", []string{"input_tokens"}); err == nil {
		t.Fatal("constant-only expression accepted")
	}
}
