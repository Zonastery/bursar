package bursar

import (
	"strings"

	"github.com/shopspring/decimal"
)

// UsageMetrics is one provider-neutral billable operation. Measures and
// numeric dimensions are decimals rather than float64 values.
type UsageMetrics struct {
	Operation  string                     `json:"operation"`
	Measures   map[string]decimal.Decimal `json:"measures,omitempty"`
	Dimensions map[string]any             `json:"dimensions,omitempty"`
	Metadata   map[string]any             `json:"metadata,omitempty"`
}

// Validate enforces the portable metrics boundary before a pricing engine or
// credit service evaluates a request.
func (m UsageMetrics) Validate() error {
	if strings.TrimSpace(m.Operation) == "" {
		return errorf(ErrorCodeConfig, ErrorCategoryInvalidRequest, "usage operation must be a non-empty string")
	}
	for name, value := range m.Measures {
		if strings.TrimSpace(name) == "" {
			return errorf(ErrorCodeConfig, ErrorCategoryInvalidRequest, "usage measure names must not be empty")
		}
		if value.IsNegative() {
			return errorf(ErrorCodeConfig, ErrorCategoryInvalidRequest, "usage measure '%s' must be non-negative", name)
		}
	}
	for name, value := range m.Dimensions {
		if strings.TrimSpace(name) == "" {
			return errorf(ErrorCodeConfig, ErrorCategoryInvalidRequest, "usage dimension names must not be empty")
		}
		switch value.(type) {
		case string, bool, decimal.Decimal:
			// Supported dimension scalar types.
		default:
			return errorf(
				ErrorCodeConfig,
				ErrorCategoryInvalidRequest,
				"usage dimension '%s' must be a string, bool, or decimal.Decimal",
				name,
			)
		}
	}
	return nil
}

func cloneMetrics(metrics UsageMetrics) UsageMetrics {
	cloned := UsageMetrics{Operation: metrics.Operation}
	if metrics.Measures != nil {
		cloned.Measures = make(map[string]decimal.Decimal, len(metrics.Measures))
		for key, value := range metrics.Measures {
			cloned.Measures[key] = value
		}
	}
	cloned.Dimensions = cloneAnyMap(metrics.Dimensions)
	cloned.Metadata = cloneAnyMap(metrics.Metadata)
	return cloned
}
