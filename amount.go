package bursar

import (
	"regexp"

	"github.com/shopspring/decimal"
)

const MoneyDecimalPlaces int32 = 6

// Amount is Bursar's exact credit and pricing value. It aliases
// shopspring/decimal.Decimal so consumers retain the battle-tested decimal
// API without exposing any float64 conversion path in SDK method signatures.
type Amount = decimal.Decimal

var (
	decimalStringPattern        = regexp.MustCompile(`^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$`)
	DecimalZero          Amount = decimal.Zero
)

// ParseDecimalString parses the canonical configuration decimal format. It
// deliberately rejects floats, exponent notation, leading plus signs, and
// non-canonical leading zeroes so financial configuration never crosses a
// binary-float boundary.
func ParseDecimalString(value string) (Amount, error) {
	if !decimalStringPattern.MatchString(value) {
		return decimal.Zero, newConfigError("decimal values must be base-10 decimal strings", nil)
	}
	parsed, err := decimal.NewFromString(value)
	if err != nil {
		return decimal.Zero, newConfigError("decimal value is invalid", err)
	}
	return parsed, nil
}

// NewAmount parses a canonical base-10 decimal string for public SDK inputs.
func NewAmount(value string) (Amount, error) {
	return ParseDecimalString(value)
}

// MustAmount is convenient for static configuration and tests. Runtime input
// should use NewAmount so an invalid value is returned to the caller.
func MustAmount(value string) Amount {
	amount, err := NewAmount(value)
	if err != nil {
		panic(err)
	}
	return amount
}

// QuantizeMoney applies Bursar's cross-SDK money precision: six fractional
// places with half-up rounding. shopspring/decimal.Round rounds midpoint ties
// away from zero, which is equivalent to Decimal ROUND_HALF_UP.
func QuantizeMoney(value decimal.Decimal) decimal.Decimal {
	return value.Round(MoneyDecimalPlaces)
}

func requirePositiveAmount(value decimal.Decimal, operation string) (decimal.Decimal, error) {
	if !value.GreaterThan(decimal.Zero) {
		return decimal.Zero, errorf(ErrorCodeConfig, ErrorCategoryInvalidRequest, "%s amount must be positive", operation)
	}
	return value, nil
}

func requireNonNegativeAmount(value decimal.Decimal, operation string) (decimal.Decimal, error) {
	if value.IsNegative() {
		return decimal.Zero, errorf(ErrorCodeConfig, ErrorCategoryInvalidRequest, "%s amount must be non-negative", operation)
	}
	return value, nil
}

func decimalDiv(left, right decimal.Decimal) (decimal.Decimal, error) {
	if right.IsZero() {
		return decimal.Zero, newExpressionError("division by zero", nil)
	}
	// Retain far more precision than Bursar's final 6dp contract so chained
	// pricing operations do not lose an intermediate fractional credit.
	return left.DivRound(right, 32), nil
}

func decimalTruncDiv(left, right decimal.Decimal) (decimal.Decimal, error) {
	quotient, err := decimalDiv(left, right)
	if err != nil {
		return decimal.Zero, err
	}
	return quotient.Truncate(0), nil
}
