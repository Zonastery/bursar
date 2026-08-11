package bursar

import (
	"fmt"
	"sort"
	"strings"
	"unicode"

	"github.com/shopspring/decimal"
)

// ValidateExpression verifies Bursar's deliberately small pricing expression
// language and requires every referenced metric variable to be declared.
func ValidateExpression(expression string, knownVariables []string) error {
	node, err := parsePricingExpression(expression)
	if err != nil {
		return err
	}
	known := make(map[string]struct{}, len(knownVariables))
	for _, variable := range knownVariables {
		known[variable] = struct{}{}
	}
	variables := make(map[string]struct{})
	collectExpressionVariables(node, variables)
	if len(variables) == 0 {
		return newExpressionError("expression references no variables -- must use at least one metric", nil)
	}
	for variable := range variables {
		if _, ok := known[variable]; !ok {
			return newExpressionError(fmt.Sprintf("unknown variable: '%s'", variable), nil)
		}
	}
	return nil
}

// EvaluateExpression evaluates a safe pricing expression with exact decimal
// arithmetic. Variables must be decimal values; float64 is never accepted.
func EvaluateExpression(expression string, variables map[string]Amount) (Amount, error) {
	if len(variables) == 0 {
		return decimal.Zero, newExpressionError("cannot evaluate: variables dict is empty", nil)
	}
	node, err := parsePricingExpression(expression)
	if err != nil {
		return decimal.Zero, err
	}
	if err := validateExpressionVariables(node, variables); err != nil {
		return decimal.Zero, err
	}
	result, err := evaluateExpressionNode(node, variables)
	if err != nil {
		return decimal.Zero, err
	}
	return result, nil
}

type expressionTokenType string

const (
	tokenNumber     expressionTokenType = "number"
	tokenIdentifier expressionTokenType = "identifier"
	tokenPlus       expressionTokenType = "+"
	tokenMinus      expressionTokenType = "-"
	tokenMultiply   expressionTokenType = "*"
	tokenDivide     expressionTokenType = "/"
	tokenIntDivide  expressionTokenType = "//"
	tokenModulo     expressionTokenType = "%"
	tokenPower      expressionTokenType = "**"
	tokenLeftParen  expressionTokenType = "("
	tokenRightParen expressionTokenType = ")"
	tokenComma      expressionTokenType = ","
	tokenEqual      expressionTokenType = "=="
	tokenNotEqual   expressionTokenType = "!="
	tokenLess       expressionTokenType = "<"
	tokenLessEqual  expressionTokenType = "<="
	tokenGreater    expressionTokenType = ">"
	tokenGreaterEq  expressionTokenType = ">="
	tokenIn         expressionTokenType = "in"
	tokenNot        expressionTokenType = "not"
	tokenAnd        expressionTokenType = "and"
	tokenOr         expressionTokenType = "or"
	tokenIf         expressionTokenType = "if"
	tokenElse       expressionTokenType = "else"
)

type expressionToken struct {
	type_ expressionTokenType
	value string
}

func tokenizePricingExpression(source string) ([]expressionToken, error) {
	tokens := make([]expressionToken, 0)
	for index := 0; index < len(source); {
		character := rune(source[index])
		if unicode.IsSpace(character) {
			index++
			continue
		}
		if index+1 < len(source) {
			pair := source[index : index+2]
			switch pair {
			case "**", "//", "==", "!=", "<=", ">=":
				tokens = append(tokens, expressionToken{type_: expressionTokenType(pair), value: pair})
				index += 2
				continue
			}
		}
		switch source[index] {
		case '(', ')', ',', '+', '-', '*', '/', '%', '<', '>':
			value := source[index : index+1]
			tokens = append(tokens, expressionToken{type_: expressionTokenType(value), value: value})
			index++
			continue
		}
		if isExpressionNumberStart(source[index]) {
			value, next, err := readExpressionNumber(source, index)
			if err != nil {
				return nil, err
			}
			tokens = append(tokens, expressionToken{type_: tokenNumber, value: value})
			index = next
			continue
		}
		if isExpressionWordStart(source[index]) {
			value, next := readExpressionWord(source, index)
			switch value {
			case "true":
				tokens = append(tokens, expressionToken{type_: tokenNumber, value: "1"})
			case "false":
				tokens = append(tokens, expressionToken{type_: tokenNumber, value: "0"})
			case "and", "or", "if", "else", "in", "not":
				tokens = append(tokens, expressionToken{type_: expressionTokenType(value), value: value})
			default:
				tokens = append(tokens, expressionToken{type_: tokenIdentifier, value: value})
			}
			index = next
			continue
		}
		return nil, newExpressionError(fmt.Sprintf("unexpected character: '%s'", source[index:index+1]), nil)
	}
	return tokens, nil
}

func isExpressionNumberStart(value byte) bool {
	return (value >= '0' && value <= '9') || value == '.'
}

func isExpressionWordStart(value byte) bool {
	return (value >= 'a' && value <= 'z') || (value >= 'A' && value <= 'Z') || value == '_'
}

func readExpressionNumber(source string, start int) (string, int, error) {
	index := start
	dotSeen := false
	for index < len(source) {
		character := source[index]
		if (character < '0' || character > '9') && character != '.' {
			break
		}
		if character == '.' {
			if dotSeen {
				return "", 0, newExpressionError(fmt.Sprintf("invalid number literal: '%s.'", source[start:index]), nil)
			}
			dotSeen = true
		}
		index++
	}
	value := source[start:index]
	if value == "." || !containsExpressionDigit(value) {
		return "", 0, newExpressionError(fmt.Sprintf("invalid number literal: '%s'", value), nil)
	}
	if index < len(source) && (source[index] == 'e' || source[index] == 'E') {
		index++
		if index < len(source) && (source[index] == '+' || source[index] == '-') {
			index++
		}
		exponentStart := index
		for index < len(source) && source[index] >= '0' && source[index] <= '9' {
			index++
		}
		if index == exponentStart {
			return "", 0, newExpressionError(fmt.Sprintf("invalid number literal: '%s'", source[start:index]), nil)
		}
		value = source[start:index]
	}
	if _, err := decimal.NewFromString(value); err != nil {
		return "", 0, newExpressionError(fmt.Sprintf("invalid number literal: '%s'", value), err)
	}
	return value, index, nil
}

func containsExpressionDigit(value string) bool {
	for _, character := range value {
		if character >= '0' && character <= '9' {
			return true
		}
	}
	return false
}

func readExpressionWord(source string, start int) (string, int) {
	index := start
	for index < len(source) {
		character := source[index]
		if !((character >= 'a' && character <= 'z') || (character >= 'A' && character <= 'Z') || (character >= '0' && character <= '9') || character == '_') {
			break
		}
		index++
	}
	return source[start:index], index
}

type expressionNode interface{ expressionNode() }

type numberExpressionNode struct{ value string }
type identifierExpressionNode struct{ name string }
type binaryExpressionNode struct {
	op          string
	left, right expressionNode
}
type unaryExpressionNode struct {
	op      string
	operand expressionNode
}
type callExpressionNode struct {
	name string
	args []expressionNode
}
type ternaryExpressionNode struct {
	condition, then, otherwise expressionNode
}
type comparisonExpressionNode struct {
	op          string
	left, right expressionNode
}
type booleanExpressionNode struct {
	op          string
	left, right expressionNode
}

func (numberExpressionNode) expressionNode()     {}
func (identifierExpressionNode) expressionNode() {}
func (binaryExpressionNode) expressionNode()     {}
func (unaryExpressionNode) expressionNode()      {}
func (callExpressionNode) expressionNode()       {}
func (ternaryExpressionNode) expressionNode()    {}
func (comparisonExpressionNode) expressionNode() {}
func (booleanExpressionNode) expressionNode()    {}

type pricingExpressionParser struct {
	tokens   []expressionToken
	position int
}

func parsePricingExpression(source string) (expressionNode, error) {
	tokens, err := tokenizePricingExpression(source)
	if err != nil {
		return nil, err
	}
	parser := pricingExpressionParser{tokens: tokens}
	node, err := parser.parse()
	if err != nil {
		return nil, err
	}
	if !parser.atEnd() {
		return nil, newExpressionError(fmt.Sprintf("unexpected token after expression: '%s'", parser.peek().value), nil)
	}
	if err := validateExpressionCalls(node); err != nil {
		return nil, err
	}
	return node, nil
}

func (p *pricingExpressionParser) parse() (expressionNode, error) {
	expression, err := p.booleanExpression()
	if err != nil {
		return nil, err
	}
	if !p.match(tokenIf) {
		return expression, nil
	}
	condition, err := p.booleanExpression()
	if err != nil {
		return nil, err
	}
	if _, err := p.consume(tokenElse, "expected 'else' in ternary expression"); err != nil {
		return nil, err
	}
	otherwise, err := p.booleanExpression()
	if err != nil {
		return nil, err
	}
	return ternaryExpressionNode{condition: condition, then: expression, otherwise: otherwise}, nil
}

func (p *pricingExpressionParser) booleanExpression() (expressionNode, error) {
	left, err := p.andExpression()
	if err != nil {
		return nil, err
	}
	for p.match(tokenOr) {
		right, err := p.andExpression()
		if err != nil {
			return nil, err
		}
		left = booleanExpressionNode{op: "or", left: left, right: right}
	}
	return left, nil
}

func (p *pricingExpressionParser) andExpression() (expressionNode, error) {
	left, err := p.notExpression()
	if err != nil {
		return nil, err
	}
	for p.match(tokenAnd) {
		right, err := p.notExpression()
		if err != nil {
			return nil, err
		}
		left = booleanExpressionNode{op: "and", left: left, right: right}
	}
	return left, nil
}

func (p *pricingExpressionParser) notExpression() (expressionNode, error) {
	if !p.match(tokenNot) {
		return p.comparison()
	}
	operand, err := p.notExpression()
	if err != nil {
		return nil, err
	}
	return unaryExpressionNode{op: "not", operand: operand}, nil
}

func (p *pricingExpressionParser) comparison() (expressionNode, error) {
	left, err := p.addition()
	if err != nil {
		return nil, err
	}
	var operation string
	if p.check(tokenNot) {
		p.position++
		if !p.match(tokenIn) {
			return nil, newExpressionError("expected 'in' after 'not'", nil)
		}
		operation = "not in"
	} else if p.match(tokenEqual, tokenNotEqual, tokenLess, tokenLessEqual, tokenGreater, tokenGreaterEq, tokenIn) {
		operation = p.previous().value
	} else {
		return left, nil
	}
	right, err := p.addition()
	if err != nil {
		return nil, err
	}
	if p.check(tokenEqual, tokenNotEqual, tokenLess, tokenLessEqual, tokenGreater, tokenGreaterEq, tokenIn, tokenNot) {
		return nil, newExpressionError("chained comparisons are not supported", nil)
	}
	return comparisonExpressionNode{op: operation, left: left, right: right}, nil
}

func (p *pricingExpressionParser) addition() (expressionNode, error) {
	left, err := p.multiplication()
	if err != nil {
		return nil, err
	}
	for p.match(tokenPlus, tokenMinus) {
		operation := p.previous().value
		right, err := p.multiplication()
		if err != nil {
			return nil, err
		}
		left = binaryExpressionNode{op: operation, left: left, right: right}
	}
	return left, nil
}

func (p *pricingExpressionParser) multiplication() (expressionNode, error) {
	left, err := p.unary()
	if err != nil {
		return nil, err
	}
	for p.match(tokenMultiply, tokenDivide, tokenIntDivide, tokenModulo) {
		operation := p.previous().value
		right, err := p.unary()
		if err != nil {
			return nil, err
		}
		left = binaryExpressionNode{op: operation, left: left, right: right}
	}
	if p.check(tokenPower) {
		return nil, newExpressionError("exponentiation operator '**' is not allowed in pricing expressions", nil)
	}
	return left, nil
}

func (p *pricingExpressionParser) unary() (expressionNode, error) {
	if !p.match(tokenPlus, tokenMinus) {
		return p.primary()
	}
	operation := p.previous().value
	operand, err := p.unary()
	if err != nil {
		return nil, err
	}
	return unaryExpressionNode{op: operation, operand: operand}, nil
}

func (p *pricingExpressionParser) primary() (expressionNode, error) {
	if p.check(tokenPower) {
		return nil, newExpressionError("exponentiation operator '**' is not allowed in pricing expressions", nil)
	}
	if p.match(tokenNumber) {
		return numberExpressionNode{value: p.previous().value}, nil
	}
	if p.match(tokenIdentifier) {
		name := p.previous().value
		if !p.match(tokenLeftParen) {
			return identifierExpressionNode{name: name}, nil
		}
		if !allowedExpressionFunctions[name] {
			return nil, newExpressionError(fmt.Sprintf("disallowed function: %s", name), nil)
		}
		arguments, err := p.callArguments()
		if err != nil {
			return nil, err
		}
		return callExpressionNode{name: name, args: arguments}, nil
	}
	if p.match(tokenIf) && p.match(tokenLeftParen) {
		arguments, err := p.callArguments()
		if err != nil {
			return nil, err
		}
		return callExpressionNode{name: "if", args: arguments}, nil
	}
	if p.match(tokenLeftParen) {
		expression, err := p.parse()
		if err != nil {
			return nil, err
		}
		if _, err := p.consume(tokenRightParen, "expected ')'"); err != nil {
			return nil, err
		}
		return expression, nil
	}
	return nil, newExpressionError(fmt.Sprintf("unexpected token: '%s'", p.peekValue()), nil)
}

func (p *pricingExpressionParser) callArguments() ([]expressionNode, error) {
	arguments := make([]expressionNode, 0)
	if !p.check(tokenRightParen) {
		for {
			argument, err := p.booleanExpression()
			if err != nil {
				return nil, err
			}
			arguments = append(arguments, argument)
			if !p.match(tokenComma) {
				break
			}
		}
	}
	if _, err := p.consume(tokenRightParen, "expected ')'"); err != nil {
		return nil, err
	}
	return arguments, nil
}

func (p *pricingExpressionParser) atEnd() bool { return p.position >= len(p.tokens) }
func (p *pricingExpressionParser) peek() expressionToken {
	if p.atEnd() {
		return expressionToken{}
	}
	return p.tokens[p.position]
}
func (p *pricingExpressionParser) peekValue() string {
	if p.atEnd() {
		return "EOF"
	}
	return p.peek().value
}
func (p *pricingExpressionParser) previous() expressionToken { return p.tokens[p.position-1] }
func (p *pricingExpressionParser) check(types ...expressionTokenType) bool {
	if p.atEnd() {
		return false
	}
	for _, tokenType := range types {
		if p.peek().type_ == tokenType {
			return true
		}
	}
	return false
}
func (p *pricingExpressionParser) match(types ...expressionTokenType) bool {
	if !p.check(types...) {
		return false
	}
	p.position++
	return true
}
func (p *pricingExpressionParser) consume(tokenType expressionTokenType, message string) (expressionToken, error) {
	if !p.check(tokenType) {
		return expressionToken{}, newExpressionError(message, nil)
	}
	p.position++
	return p.previous(), nil
}

var allowedExpressionFunctions = map[string]bool{
	"ceil":       true,
	"floor":      true,
	"min":        true,
	"max":        true,
	"round":      true,
	"if":         true,
	"tier":       true,
	"clamp":      true,
	"percentile": true,
}

func validateExpressionCalls(node expressionNode) error {
	switch value := node.(type) {
	case callExpressionNode:
		argumentCount := len(value.args)
		switch value.name {
		case "if", "clamp":
			if argumentCount != 3 {
				return newExpressionError(fmt.Sprintf("%s() requires exactly 3 arguments", value.name), nil)
			}
		case "tier":
			if argumentCount < 4 || argumentCount%2 != 0 {
				return newExpressionError("tier() requires an even number of arguments >= 4 (value, threshold, rate, ..., default)", nil)
			}
		case "percentile":
			if argumentCount < 2 {
				return newExpressionError("percentile() requires at least 2 arguments (p, v1, [v2, ...])", nil)
			}
		case "min", "max":
			if argumentCount < 1 {
				return newExpressionError(fmt.Sprintf("%s() requires at least 1 argument", value.name), nil)
			}
		case "ceil", "floor":
			if argumentCount != 1 {
				return newExpressionError(fmt.Sprintf("%s() requires exactly 1 argument", value.name), nil)
			}
		case "round":
			if argumentCount != 1 && argumentCount != 2 {
				return newExpressionError("round() requires 1 or 2 arguments: round(x[, ndigits])", nil)
			}
		}
		for _, argument := range value.args {
			if err := validateExpressionCalls(argument); err != nil {
				return err
			}
		}
	case unaryExpressionNode:
		return validateExpressionCalls(value.operand)
	case binaryExpressionNode:
		if err := validateExpressionCalls(value.left); err != nil {
			return err
		}
		return validateExpressionCalls(value.right)
	case comparisonExpressionNode:
		if err := validateExpressionCalls(value.left); err != nil {
			return err
		}
		return validateExpressionCalls(value.right)
	case booleanExpressionNode:
		if err := validateExpressionCalls(value.left); err != nil {
			return err
		}
		return validateExpressionCalls(value.right)
	case ternaryExpressionNode:
		if err := validateExpressionCalls(value.condition); err != nil {
			return err
		}
		if err := validateExpressionCalls(value.then); err != nil {
			return err
		}
		return validateExpressionCalls(value.otherwise)
	}
	return nil
}

func collectExpressionVariables(node expressionNode, variables map[string]struct{}) {
	switch value := node.(type) {
	case identifierExpressionNode:
		variables[value.name] = struct{}{}
	case unaryExpressionNode:
		collectExpressionVariables(value.operand, variables)
	case binaryExpressionNode:
		collectExpressionVariables(value.left, variables)
		collectExpressionVariables(value.right, variables)
	case callExpressionNode:
		for _, argument := range value.args {
			collectExpressionVariables(argument, variables)
		}
	case ternaryExpressionNode:
		collectExpressionVariables(value.condition, variables)
		collectExpressionVariables(value.then, variables)
		collectExpressionVariables(value.otherwise, variables)
	case comparisonExpressionNode:
		collectExpressionVariables(value.left, variables)
		collectExpressionVariables(value.right, variables)
	case booleanExpressionNode:
		collectExpressionVariables(value.left, variables)
		collectExpressionVariables(value.right, variables)
	}
}

func validateExpressionVariables(node expressionNode, variables map[string]Amount) error {
	referenced := make(map[string]struct{})
	collectExpressionVariables(node, referenced)
	for variable := range referenced {
		if _, ok := variables[variable]; !ok {
			return newExpressionError(fmt.Sprintf("undefined variable: '%s'", variable), nil)
		}
	}
	return nil
}

func evaluateExpressionNode(node expressionNode, variables map[string]Amount) (Amount, error) {
	switch value := node.(type) {
	case numberExpressionNode:
		parsed, err := decimal.NewFromString(value.value)
		if err != nil {
			return decimal.Zero, newExpressionError("invalid number literal", err)
		}
		return parsed, nil
	case identifierExpressionNode:
		return variables[value.name], nil
	case unaryExpressionNode:
		operand, err := evaluateExpressionNode(value.operand, variables)
		if err != nil {
			return decimal.Zero, err
		}
		switch value.op {
		case "-":
			return operand.Neg(), nil
		case "not":
			if operand.IsZero() {
				return decimal.NewFromInt(1), nil
			}
			return decimal.Zero, nil
		default:
			return operand, nil
		}
	case binaryExpressionNode:
		left, err := evaluateExpressionNode(value.left, variables)
		if err != nil {
			return decimal.Zero, err
		}
		right, err := evaluateExpressionNode(value.right, variables)
		if err != nil {
			return decimal.Zero, err
		}
		switch value.op {
		case "+":
			return left.Add(right), nil
		case "-":
			return left.Sub(right), nil
		case "*":
			return left.Mul(right), nil
		case "/":
			return decimalDiv(left, right)
		case "//":
			return decimalTruncDiv(left, right)
		case "%":
			if right.IsZero() {
				return decimal.Zero, newExpressionError("modulo by zero", nil)
			}
			return left.Mod(right), nil
		}
	case comparisonExpressionNode:
		left, err := evaluateExpressionNode(value.left, variables)
		if err != nil {
			return decimal.Zero, err
		}
		right, err := evaluateExpressionNode(value.right, variables)
		if err != nil {
			return decimal.Zero, err
		}
		matches := false
		switch value.op {
		case "==":
			matches = left.Equal(right)
		case "!=":
			matches = !left.Equal(right)
		case "<":
			matches = left.LessThan(right)
		case "<=":
			matches = left.LessThanOrEqual(right)
		case ">":
			matches = left.GreaterThan(right)
		case ">=":
			matches = left.GreaterThanOrEqual(right)
		case "in":
			matches = strings.Contains(left.String(), right.String())
		case "not in":
			matches = !strings.Contains(left.String(), right.String())
		}
		if matches {
			return decimal.NewFromInt(1), nil
		}
		return decimal.Zero, nil
	case booleanExpressionNode:
		left, err := evaluateExpressionNode(value.left, variables)
		if err != nil {
			return decimal.Zero, err
		}
		if value.op == "and" {
			if left.IsZero() {
				return left, nil
			}
			return evaluateExpressionNode(value.right, variables)
		}
		if !left.IsZero() {
			return left, nil
		}
		return evaluateExpressionNode(value.right, variables)
	case ternaryExpressionNode:
		condition, err := evaluateExpressionNode(value.condition, variables)
		if err != nil {
			return decimal.Zero, err
		}
		if condition.IsZero() {
			return evaluateExpressionNode(value.otherwise, variables)
		}
		return evaluateExpressionNode(value.then, variables)
	case callExpressionNode:
		arguments := make([]Amount, 0, len(value.args))
		for _, argumentNode := range value.args {
			argument, err := evaluateExpressionNode(argumentNode, variables)
			if err != nil {
				return decimal.Zero, err
			}
			arguments = append(arguments, argument)
		}
		return evaluateExpressionCall(value.name, arguments)
	}
	return decimal.Zero, newExpressionError("invalid expression node", nil)
}

func evaluateExpressionCall(name string, args []Amount) (Amount, error) {
	requireArg := func(index int) (Amount, error) {
		if index >= len(args) {
			return decimal.Zero, newExpressionError(fmt.Sprintf("%s() received an invalid number of arguments", name), nil)
		}
		return args[index], nil
	}
	switch name {
	case "ceil":
		value, err := requireArg(0)
		return value.Ceil(), err
	case "floor":
		value, err := requireArg(0)
		return value.Floor(), err
	case "min", "max":
		if len(args) == 0 {
			return decimal.Zero, newExpressionError(fmt.Sprintf("%s() requires at least 1 argument", name), nil)
		}
		value := args[0]
		for _, candidate := range args[1:] {
			if (name == "min" && candidate.LessThan(value)) || (name == "max" && candidate.GreaterThan(value)) {
				value = candidate
			}
		}
		return value, nil
	case "round":
		value, err := requireArg(0)
		if err != nil {
			return decimal.Zero, err
		}
		places := int32(0)
		if len(args) == 2 {
			places = int32(args[1].IntPart())
		}
		return value.Round(places), nil
	case "if":
		condition, err := requireArg(0)
		if err != nil {
			return decimal.Zero, err
		}
		if condition.IsZero() {
			return requireArg(2)
		}
		return requireArg(1)
	case "tier":
		value, err := requireArg(0)
		if err != nil {
			return decimal.Zero, err
		}
		for index := 1; index < len(args)-1; index += 2 {
			if value.LessThan(args[index]) {
				return args[index+1], nil
			}
		}
		return args[len(args)-1], nil
	case "clamp":
		value, err := requireArg(0)
		if err != nil {
			return decimal.Zero, err
		}
		minimum, err := requireArg(1)
		if err != nil {
			return decimal.Zero, err
		}
		maximum, err := requireArg(2)
		if err != nil {
			return decimal.Zero, err
		}
		if value.LessThan(minimum) {
			return minimum, nil
		}
		if value.GreaterThan(maximum) {
			return maximum, nil
		}
		return value, nil
	case "percentile":
		percentage, err := requireArg(0)
		if err != nil {
			return decimal.Zero, err
		}
		if percentage.LessThan(decimal.Zero) || percentage.GreaterThan(decimal.NewFromInt(100)) {
			return decimal.Zero, newExpressionError("percentile() p must be between 0 and 100", nil)
		}
		values := append([]Amount(nil), args[1:]...)
		sort.Slice(values, func(left, right int) bool { return values[left].LessThan(values[right]) })
		if len(values) == 1 {
			return values[0], nil
		}
		rank, err := decimalDiv(percentage, decimal.NewFromInt(100))
		if err != nil {
			return decimal.Zero, err
		}
		rank = rank.Mul(decimal.NewFromInt(int64(len(values) - 1)))
		lower := rank.Floor()
		lowerIndex := int(lower.IntPart())
		upperIndex := lowerIndex + 1
		if upperIndex >= len(values) {
			upperIndex = len(values) - 1
		}
		fraction := rank.Sub(lower)
		return values[lowerIndex].Mul(decimal.NewFromInt(1).Sub(fraction)).Add(values[upperIndex].Mul(fraction)), nil
	}
	return decimal.Zero, newExpressionError(fmt.Sprintf("disallowed function: %s", name), nil)
}
