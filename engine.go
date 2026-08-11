package bursar

import (
	"fmt"
	"sort"

	"github.com/shopspring/decimal"
)

// PricingOptions selects a configured rate card. When a catalog has exactly
// one rate card, callers may omit it.
type PricingOptions struct {
	RateCard string
}

// PricingEngine evaluates a validated Bursar pricing catalog using exact
// decimal arithmetic. It is safe for concurrent reads after construction as
// long as callers do not mutate the supplied configuration.
type PricingEngine struct {
	config *BursarConfig
}

// NewPricingEngine creates an engine from a parsed configuration.
func NewPricingEngine(config *BursarConfig) (*PricingEngine, error) {
	if config == nil {
		return nil, newConfigError("pricing configuration is required", nil)
	}
	return &PricingEngine{config: config}, nil
}

// NewPricingEngineFromMap validates raw snake_case configuration and creates
// a pricing engine in one step.
func NewPricingEngineFromMap(data map[string]any) (*PricingEngine, error) {
	config, err := LoadConfigFromMap(data)
	if err != nil {
		return nil, err
	}
	return NewPricingEngine(config)
}

// NewPricingEngineFromDict is a compatibility alias for cross-SDK examples.
func NewPricingEngineFromDict(data map[string]any) (*PricingEngine, error) {
	return NewPricingEngineFromMap(data)
}

// Config returns the parsed catalog owned by this engine. Consumers should
// treat the result as immutable; PricingSchema provides a detached map for
// serialization or inspection.
func (e *PricingEngine) Config() *BursarConfig {
	if e == nil {
		return nil
	}
	return e.config
}

// PricingSchema returns the canonical, JSON-safe snake_case catalog.
func (e *PricingEngine) PricingSchema() map[string]any {
	if e == nil || e.config == nil {
		return nil
	}
	return CanonicalParsedBursarConfigDict(e.config)
}

// Calculate prices one usage event. The optional PricingOptions is provided
// as a variadic argument to keep the common single-rate-card call concise.
func (e *PricingEngine) Calculate(metrics UsageMetrics, options ...PricingOptions) (CostBreakdown, error) {
	if e == nil || e.config == nil {
		return CostBreakdown{}, newConfigError("pricing engine is not configured", nil)
	}
	if len(options) > 1 {
		return CostBreakdown{}, newConfigError("at most one PricingOptions value may be supplied", nil)
	}
	if err := metrics.Validate(); err != nil {
		return CostBreakdown{}, err
	}
	pricing := e.config.Pricing
	if pricing == nil {
		return CostBreakdown{}, newConfigError("usage pricing not configured", nil)
	}
	operation, exists := pricing.Operations[metrics.Operation]
	if !exists {
		return CostBreakdown{}, newConfigError(fmt.Sprintf("unknown usage operation '%s'", metrics.Operation), nil)
	}
	for name := range metrics.Measures {
		if _, exists := operation.Measures[name]; !exists {
			return CostBreakdown{}, newConfigError(fmt.Sprintf("operation '%s' received undeclared measure '%s'", metrics.Operation, name), nil)
		}
	}
	for name := range metrics.Dimensions {
		if _, exists := operation.Dimensions[name]; !exists {
			return CostBreakdown{}, newConfigError(fmt.Sprintf("operation '%s' received undeclared dimension '%s'", metrics.Operation, name), nil)
		}
	}
	dimensions := make(map[string]MatcherScalar, len(operation.Dimensions))
	for name, definition := range operation.Dimensions {
		input, supplied := metrics.Dimensions[name]
		if !supplied || input == nil {
			if definition.Required {
				return CostBreakdown{}, newConfigError(fmt.Sprintf("operation '%s' requires dimension '%s'", metrics.Operation, name), nil)
			}
			continue
		}
		parsed, err := validateRuntimeDimension(input, definition, name)
		if err != nil {
			return CostBreakdown{}, err
		}
		dimensions[name] = parsed
	}
	measures := make(map[string]Amount, len(operation.Measures))
	for name := range operation.Measures {
		value := decimal.Zero
		if input, exists := metrics.Measures[name]; exists {
			value = input
		}
		if value.IsNegative() {
			return CostBreakdown{}, newConfigError(fmt.Sprintf("usage measure '%s' must be finite and non-negative", name), nil)
		}
		measures[name] = value
	}
	requestedCard := ""
	if len(options) == 1 {
		requestedCard = options[0].RateCard
	}
	cardKey, err := e.resolveRateCard(requestedCard)
	if err != nil {
		return CostBreakdown{}, err
	}
	operationPricing, err := e.operationPricing(cardKey, metrics.Operation)
	if err != nil {
		return CostBreakdown{}, err
	}
	var selected *Charge
	for _, rule := range operationPricing.Rules {
		if e.matches(rule, dimensions) {
			charge := rule.Charge
			selected = &charge
			break
		}
	}
	if selected == nil && operationPricing.Unmatched.Action == "charge" && operationPricing.Unmatched.Charge != nil {
		selected = operationPricing.Unmatched.Charge
	}
	if selected == nil {
		return CostBreakdown{}, newConfigError(fmt.Sprintf("no price rule matched operation '%s' in rate card '%s'", metrics.Operation, cardKey), nil)
	}
	value, err := e.evaluateCharge(*selected, measures)
	if err != nil {
		return CostBreakdown{}, err
	}
	if value.IsNegative() {
		return CostBreakdown{}, newConfigError(fmt.Sprintf("price charge for '%s' produced a negative credit cost", metrics.Operation), nil)
	}
	measureBreakdown := make(map[string]string, len(measures))
	for name, amount := range measures {
		measureBreakdown[name] = amount.String()
	}
	return MakeCostBreakdown(CostBreakdownInput{
		OperationCredits: QuantizeMoney(value),
		Breakdown: map[string]any{
			"operation":  metrics.Operation,
			"rateCard":   cardKey,
			"chargeType": selected.Type,
			"measures":   measureBreakdown,
			"dimensions": cloneAnyMap(metrics.Dimensions),
		},
	}), nil
}

// CalculateBatch calculates events independently in input order. Evaluation
// stops at the first invalid event so callers never receive a partial result
// without an explicit transaction boundary of their own.
func (e *PricingEngine) CalculateBatch(metrics []UsageMetrics, options ...PricingOptions) ([]CostBreakdown, error) {
	if len(options) > 1 {
		return nil, newConfigError("at most one PricingOptions value may be supplied", nil)
	}
	results := make([]CostBreakdown, 0, len(metrics))
	for _, item := range metrics {
		result, err := e.Calculate(item, options...)
		if err != nil {
			return nil, err
		}
		results = append(results, result)
	}
	return results, nil
}

// GetRateCardForPlan returns a plan's explicitly configured rate card.
func (e *PricingEngine) GetRateCardForPlan(planID string) (string, bool) {
	if e == nil || e.config == nil || planID == "" {
		return "", false
	}
	plan, exists := e.config.Plans[planID]
	if !exists || plan.RateCard == nil {
		return "", false
	}
	return *plan.RateCard, true
}

func validateRuntimeDimension(value any, definition DimensionDefinition, name string) (MatcherScalar, error) {
	switch definition.Type {
	case "string":
		stringValue, ok := value.(string)
		if !ok {
			return nil, newConfigError("dimension '"+name+"' must be string", nil)
		}
		return stringValue, nil
	case "boolean":
		boolValue, ok := value.(bool)
		if !ok {
			return nil, newConfigError("dimension '"+name+"' must be boolean", nil)
		}
		return boolValue, nil
	case "number":
		amount, ok := value.(decimal.Decimal)
		if !ok {
			return nil, newConfigError("dimension '"+name+"' must be decimal.Decimal", nil)
		}
		return amount, nil
	default:
		return nil, newConfigError("dimension '"+name+"' has an invalid type", nil)
	}
}

func (e *PricingEngine) resolveRateCard(requested string) (string, error) {
	if requested != "" {
		if _, exists := e.config.Pricing.RateCards[requested]; !exists {
			return "", newConfigError("unknown rate card '"+requested+"'", nil)
		}
		return requested, nil
	}
	keys := make([]string, 0, len(e.config.Pricing.RateCards))
	for key := range e.config.Pricing.RateCards {
		keys = append(keys, key)
	}
	if len(keys) == 1 {
		return keys[0], nil
	}
	sort.Strings(keys)
	return "", newConfigError("rateCard is required when more than one rate card is configured", nil)
}

func (e *PricingEngine) operationPricing(cardKey, operation string) (OperationPricing, error) {
	seen := map[string]struct{}{}
	for key := cardKey; ; {
		if _, exists := seen[key]; exists {
			return OperationPricing{}, newConfigError("pricing rate-card inheritance cycle detected", nil)
		}
		seen[key] = struct{}{}
		card, exists := e.config.Pricing.RateCards[key]
		if !exists {
			return OperationPricing{}, newConfigError("unknown rate card '"+key+"'", nil)
		}
		if operationPricing, exists := card.Operations[operation]; exists {
			return operationPricing, nil
		}
		if card.Extends == nil {
			return OperationPricing{}, newConfigError(fmt.Sprintf("rate card '%s' has no price for operation '%s'", cardKey, operation), nil)
		}
		key = *card.Extends
	}
}

func (e *PricingEngine) matches(rule PriceRule, dimensions map[string]MatcherScalar) bool {
	for name, matcher := range rule.When {
		value, exists := dimensions[name]
		if !exists || !matchesDimension(matcher, value) {
			return false
		}
	}
	return true
}

func matchesDimension(matcher DimensionMatcher, value MatcherScalar) bool {
	switch matcher.Op {
	case "eq":
		return equalMatcherScalars(value, matcher.Value)
	case "in":
		for _, candidate := range matcher.Values {
			if equalMatcherScalars(value, candidate) {
				return true
			}
		}
		return false
	case "not_in":
		for _, candidate := range matcher.Values {
			if equalMatcherScalars(value, candidate) {
				return false
			}
		}
		return true
	case "prefix":
		stringValue, stringOK := value.(string)
		prefix, prefixOK := matcher.Value.(string)
		return stringOK && prefixOK && len(prefix) <= len(stringValue) && stringValue[:len(prefix)] == prefix
	case "range":
		amount, ok := value.(decimal.Decimal)
		if !ok {
			return false
		}
		if matcher.GT != nil && !amount.GreaterThan(*matcher.GT) {
			return false
		}
		if matcher.GTE != nil && !amount.GreaterThanOrEqual(*matcher.GTE) {
			return false
		}
		if matcher.LT != nil && !amount.LessThan(*matcher.LT) {
			return false
		}
		if matcher.LTE != nil && !amount.LessThanOrEqual(*matcher.LTE) {
			return false
		}
		return true
	default:
		return false
	}
}

func equalMatcherScalars(left, right MatcherScalar) bool {
	leftDecimal, leftIsDecimal := left.(decimal.Decimal)
	rightDecimal, rightIsDecimal := right.(decimal.Decimal)
	if leftIsDecimal || rightIsDecimal {
		return leftIsDecimal && rightIsDecimal && leftDecimal.Equal(rightDecimal)
	}
	switch leftValue := left.(type) {
	case string:
		rightValue, ok := right.(string)
		return ok && leftValue == rightValue
	case bool:
		rightValue, ok := right.(bool)
		return ok && leftValue == rightValue
	default:
		return false
	}
}

func (e *PricingEngine) evaluateCharge(charge Charge, measures map[string]Amount) (Amount, error) {
	requiredMeasure := func(name string) (Amount, error) {
		value, exists := measures[name]
		if !exists {
			return decimal.Zero, newConfigError("price charge references missing usage measure '"+name+"'", nil)
		}
		return value, nil
	}
	switch charge.Type {
	case "flat":
		return charge.Amount, nil
	case "per_unit":
		measure, err := requiredMeasure(charge.Measure)
		if err != nil {
			return decimal.Zero, err
		}
		units, err := decimalDiv(measure, charge.UnitSize)
		if err != nil {
			return decimal.Zero, err
		}
		return units.Mul(charge.Rate), nil
	case "package":
		measure, err := requiredMeasure(charge.Measure)
		if err != nil {
			return decimal.Zero, err
		}
		packages, err := decimalDiv(measure, charge.Units)
		if err != nil {
			return decimal.Zero, err
		}
		switch charge.Rounding {
		case "ceil":
			packages = packages.Ceil()
		case "floor":
			packages = packages.Floor()
		case "nearest":
			packages = packages.Round(0)
		default:
			return decimal.Zero, newConfigError("unsupported package rounding '"+charge.Rounding+"'", nil)
		}
		return packages.Mul(charge.Amount), nil
	case "graduated":
		remaining, err := requiredMeasure(charge.Measure)
		if err != nil {
			return decimal.Zero, err
		}
		previous := decimal.Zero
		total := decimal.Zero
		for _, tier := range charge.Tiers {
			units := remaining
			if tier.UpTo != nil {
				width := tier.UpTo.Sub(previous)
				if remaining.LessThan(width) {
					units = remaining
				} else {
					units = width
				}
			}
			if units.GreaterThan(decimal.Zero) {
				total = total.Add(units.Mul(tier.Rate))
				remaining = remaining.Sub(units)
			}
			if !remaining.GreaterThan(decimal.Zero) {
				break
			}
			if tier.UpTo != nil {
				previous = *tier.UpTo
			}
		}
		return total, nil
	case "volume":
		measure, err := requiredMeasure(charge.Measure)
		if err != nil {
			return decimal.Zero, err
		}
		if len(charge.Tiers) == 0 {
			return decimal.Zero, newConfigError("volume charge requires at least one tier", nil)
		}
		selected := charge.Tiers[len(charge.Tiers)-1]
		for _, tier := range charge.Tiers {
			if tier.UpTo == nil || measure.LessThanOrEqual(*tier.UpTo) {
				selected = tier
				break
			}
		}
		return measure.Mul(selected.Rate), nil
	case "expression":
		return EvaluateExpression(charge.Formula, measures)
	case "sum":
		total := decimal.Zero
		for _, component := range charge.Components {
			value, err := e.evaluateCharge(component, measures)
			if err != nil {
				return decimal.Zero, err
			}
			total = total.Add(value)
		}
		return total, nil
	default:
		return decimal.Zero, newConfigError("unsupported charge type '"+charge.Type+"'", nil)
	}
}
