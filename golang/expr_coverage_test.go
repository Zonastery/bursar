package bursar

import "testing"

type unsupportedExpressionNode struct{}

func (unsupportedExpressionNode) expressionNode() {}

func TestExpressionLanguageFinancialSemantics(t *testing.T) {
	variables := map[string]Amount{
		"x":    MustAmount("5"),
		"y":    MustAmount("2"),
		"zero": DecimalZero,
	}
	tests := []struct {
		name       string
		expression string
		want       string
	}{
		{"scientific literal", "x * 1e2", "500"},
		{"unary plus", "+x", "5"},
		{"unary minus", "-x", "-5"},
		{"boolean not false", "not zero", "1"},
		{"boolean not true", "not x", "0"},
		{"arithmetic precedence", "x + y * 3 - 1", "10"},
		{"division", "x / y", "2.5"},
		{"integer division", "x // y", "2"},
		{"modulo", "x % y", "1"},
		{"equal", "x == 5", "1"},
		{"not equal", "x != y", "1"},
		{"less", "y < x", "1"},
		{"less equal", "x <= 5", "1"},
		{"greater", "x > y", "1"},
		{"greater equal", "x >= 5", "1"},
		{"decimal string contains", "12 in y", "1"},
		{"decimal string excludes", "12 not in 3", "1"},
		{"short circuit and", "zero and x", "0"},
		{"short circuit or", "x or zero", "5"},
		{"ternary true", "x if x > y else y", "5"},
		{"ternary false", "x if zero else y", "2"},
		{"ceil", "ceil(x / y)", "3"},
		{"floor", "floor(x / y)", "2"},
		{"minimum", "min(x, y, 3)", "2"},
		{"maximum", "max(x, y, 7)", "7"},
		{"round integer", "round(x / y)", "3"},
		{"round places", "round(x / 3, y)", "1.67"},
		{"if function true", "if(x, x, y)", "5"},
		{"if function false", "if(zero, x, y)", "2"},
		{"tier match", "tier(y, 3, 10, 20)", "10"},
		{"tier default", "tier(x, 3, 10, 20)", "20"},
		{"clamp low", "clamp(zero, y, x)", "2"},
		{"clamp high", "clamp(7, y, x)", "5"},
		{"clamp middle", "clamp(3, y, x)", "3"},
		{"percentile singleton", "percentile(50, x)", "5"},
		{"percentile interpolation", "percentile(50, 1, 3)", "2"},
		{"percentile upper edge", "percentile(100, 1, 3)", "3"},
	}
	for _, testCase := range tests {
		t.Run(testCase.name, func(t *testing.T) {
			result, err := EvaluateExpression(testCase.expression, variables)
			if err != nil {
				t.Fatalf("EvaluateExpression(%q) error = %v", testCase.expression, err)
			}
			if got := result.String(); got != testCase.want {
				t.Fatalf("EvaluateExpression(%q) = %s, want %s", testCase.expression, got, testCase.want)
			}
		})
	}
}

func TestExpressionLanguageRejectsUnsafeOrAmbiguousInput(t *testing.T) {
	variables := map[string]Amount{"x": MustAmount("5")}
	tests := []string{
		"@",
		".",
		"1..2",
		"1e",
		"1e+",
		"**x",
		"x ** 2",
		"unknown(x)",
		"x y",
		"x not 1",
		"x < 2 < 3",
		"x if x",
		"(x",
		"if(x, 1)",
		"clamp(x, 1)",
		"tier(x, 1, 2)",
		"percentile(x)",
		"min()",
		"max()",
		"ceil(x, 1)",
		"floor(x, 1)",
		"round(x, 1, 2)",
		"x / 0",
		"x // 0",
		"x % 0",
		"percentile(-1, x)",
		"percentile(101, x)",
	}
	for _, expression := range tests {
		t.Run(expression, func(t *testing.T) {
			if _, err := EvaluateExpression(expression, variables); err == nil {
				t.Fatalf("EvaluateExpression(%q) succeeded; want error", expression)
			}
		})
	}
	if _, err := EvaluateExpression("x", nil); err == nil {
		t.Fatal("EvaluateExpression accepted an empty variable set")
	}
	if _, err := EvaluateExpression("missing", variables); err == nil {
		t.Fatal("EvaluateExpression accepted an undefined variable")
	}
}

func TestExpressionDefensiveEvaluatorBoundaries(t *testing.T) {
	nodes := []expressionNode{
		numberExpressionNode{value: "1"},
		identifierExpressionNode{name: "x"},
		binaryExpressionNode{op: "+"},
		unaryExpressionNode{op: "+"},
		callExpressionNode{name: "min"},
		ternaryExpressionNode{},
		comparisonExpressionNode{op: "=="},
		booleanExpressionNode{op: "and"},
	}
	for _, node := range nodes {
		node.expressionNode()
	}

	if _, err := evaluateExpressionNode(numberExpressionNode{value: "."}, nil); err == nil {
		t.Fatal("invalid internal number node succeeded")
	}
	if _, err := evaluateExpressionNode(binaryExpressionNode{
		op: "?", left: numberExpressionNode{value: "1"}, right: numberExpressionNode{value: "2"},
	}, nil); err == nil {
		t.Fatal("unsupported internal binary node succeeded")
	}
	if _, err := evaluateExpressionNode(unsupportedExpressionNode{}, nil); err == nil {
		t.Fatal("unsupported expression node succeeded")
	}
	for _, function := range []string{"ceil", "floor", "round", "if", "tier", "clamp", "percentile", "unknown"} {
		if _, err := evaluateExpressionCall(function, nil); err == nil {
			t.Fatalf("evaluateExpressionCall(%q, nil) succeeded; want error", function)
		}
	}
}
