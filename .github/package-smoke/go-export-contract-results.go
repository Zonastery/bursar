// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package main

import (
	"encoding/json"
	"fmt"
	"os"

	bursar "github.com/Zonastery/bursar/golang/v2"
)

type expressionFixture struct {
	Cases []expressionCase `json:"expression_cases"`
}

type expressionCase struct {
	Name       string                 `json:"name"`
	Expression string                 `json:"expr"`
	Variables  map[string]json.Number `json:"vars"`
}

type configFixture struct {
	Cases []configCase `json:"cases"`
}

type configCase struct {
	Name   string         `json:"name"`
	Config map[string]any `json:"config"`
}

type contractResults struct {
	Expressions map[string]string `json:"expressions"`
	Configs     map[string]string `json:"configs"`
}

func main() {
	if len(os.Args) != 4 {
		panic("usage: go-export-contract-results <expressions.json> <configs.json> <output.json>")
	}

	expressions := readExpressions(os.Args[1])
	configs := readConfigs(os.Args[2])
	result := contractResults{
		Expressions: make(map[string]string, len(expressions.Cases)),
		Configs:     make(map[string]string, len(configs.Cases)),
	}

	for _, testCase := range expressions.Cases {
		variables := make(map[string]bursar.Amount, len(testCase.Variables))
		valid := true
		for name, raw := range testCase.Variables {
			amount, err := bursar.NewAmount(raw.String())
			if err != nil {
				valid = false
				break
			}
			variables[name] = amount
		}
		if !valid {
			result.Expressions[testCase.Name] = "error"
			continue
		}
		value, err := bursar.EvaluateExpression(testCase.Expression, variables)
		if err != nil {
			result.Expressions[testCase.Name] = "error"
			continue
		}
		result.Expressions[testCase.Name] = value.StringFixed(4)
	}

	for _, testCase := range configs.Cases {
		if _, err := bursar.LoadConfigFromMap(testCase.Config); err != nil {
			result.Configs[testCase.Name] = "reject"
			continue
		}
		result.Configs[testCase.Name] = "accept"
	}

	encoded, err := json.Marshal(result)
	if err != nil {
		panic(fmt.Errorf("encode Go contract results: %w", err))
	}
	encoded = append(encoded, '\n')
	if err := os.WriteFile(os.Args[3], encoded, 0o644); err != nil {
		panic(fmt.Errorf("write Go contract results: %w", err))
	}
}

func readExpressions(path string) expressionFixture {
	file, err := os.Open(path)
	if err != nil {
		panic(fmt.Errorf("open expression fixture: %w", err))
	}
	defer file.Close()

	decoder := json.NewDecoder(file)
	decoder.UseNumber()
	var fixture expressionFixture
	if err := decoder.Decode(&fixture); err != nil {
		panic(fmt.Errorf("decode expression fixture: %w", err))
	}
	return fixture
}

func readConfigs(path string) configFixture {
	contents, err := os.ReadFile(path)
	if err != nil {
		panic(fmt.Errorf("read config fixture: %w", err))
	}
	var fixture configFixture
	if err := json.Unmarshal(contents, &fixture); err != nil {
		panic(fmt.Errorf("decode config fixture: %w", err))
	}
	return fixture
}
