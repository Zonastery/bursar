package bursar

import (
	"bytes"
	_ "embed"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"os"
	"path/filepath"
	"reflect"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/dlclark/regexp2"
	"github.com/santhosh-tekuri/jsonschema/v6"
	"github.com/shopspring/decimal"
	"gopkg.in/yaml.v3"
)

// BursarConfig is the validated, normalized representation of a Bursar
// pricing catalog.  Its fields intentionally use Go naming while input and
// canonical serialization use the stable snake_case configuration contract.
type BursarConfig struct {
	Version      int
	Catalog      CatalogConfig
	Pricing      *PricingConfig
	Credits      CreditsConfig
	Entitlements EntitlementsConfig
	Admission    AdmissionConfig
	Plans        map[string]PlanDefinition
	Commerce     CommerceConfig
}

// ParsedBursarConfig is retained as an explicit name for parity with the
// JavaScript and Python SDK documentation.
type ParsedBursarConfig = BursarConfig

type CatalogConfig struct {
	DefaultPlan *string
}

type Duration struct {
	Unit  string
	Count int
}

type BillingInterval struct {
	Unit  string `json:"unit"`
	Count int    `json:"count"`
}

// Window is one of calendar, rolling, or plan_assignment.  Calendar fields
// use Unit/Count/Timezone, rolling uses Duration, and plan_assignment uses
// Interval/Timezone.
type Window struct {
	Type     string
	Unit     string
	Count    int
	Timezone string
	Duration *Duration
	Interval *BillingInterval
}

type Availability struct {
	StartsAt *string
	EndsAt   *string
	Regions  []string
}

type ExpiryPolicy struct {
	Type     string
	Interval *BillingInterval
	Timezone string
	Window   *Window
	At       *string
}

type MeasureDefinition struct {
	Unit string
}

type DimensionDefinition struct {
	Type     string
	Required bool
}

// MatcherScalar is restricted to the three configuration dimension kinds.
type MatcherScalar = any

type DimensionMatcher struct {
	Op     string
	Value  MatcherScalar
	Values []MatcherScalar
	GT     *Amount
	GTE    *Amount
	LT     *Amount
	LTE    *Amount
}

type GraduatedTier struct {
	UpTo *Amount
	Rate Amount
}

// Charge represents every supported pricing charge.  The Type field selects
// the relevant fields: flat, per_unit, package, graduated, volume,
// expression, or sum.
type Charge struct {
	Type       string
	Amount     Amount
	Measure    string
	Rate       Amount
	UnitSize   Amount
	Units      Amount
	Rounding   string
	Tiers      []GraduatedTier
	Formula    string
	Components []Charge
}

type PriceRule struct {
	When   map[string]DimensionMatcher
	Charge Charge
}

type UnmatchedPolicy struct {
	Action string
	Charge *Charge
}

type OperationPricing struct {
	Rules     []PriceRule
	Unmatched UnmatchedPolicy
}

type OperationDefinition struct {
	Measures   map[string]MeasureDefinition
	Dimensions map[string]DimensionDefinition
}

type RateCard struct {
	Extends    *string
	Operations map[string]OperationPricing
}

type PricingConfig struct {
	Operations map[string]OperationDefinition
	RateCards  map[string]RateCard
}

type BucketDefinition struct {
	Priority int
	Expiry   ExpiryPolicy
}

type CreditPolicy struct {
	Type  string
	Limit *Amount
}

type GrantAward struct {
	Recipient string
	Amount    Amount
	Bucket    string
	Expiry    *ExpiryPolicy
}

type GrantProgram struct {
	Trigger             string
	Awards              []GrantAward
	Availability        *Availability
	EligibilityPlans    []string
	EligibilityRegions  []string
	MaxAwardsPerSubject int
	IdempotencyScope    string
}

type CreditDisplay struct {
	Currency      string `json:"currency"`
	UnitsPerMajor Amount `json:"unitsPerMajor"`
}

type CreditsConfig struct {
	Buckets       map[string]BucketDefinition
	DefaultBucket *string
	Policies      map[string]CreditPolicy
	GrantPrograms map[string]GrantProgram
	Display       *CreditDisplay
}

type FeatureDefinition struct {
	Type    string
	Default any
	Values  []string
	Minimum *int
	Maximum *int
	Pattern *string
}

type EntitlementsConfig struct {
	Features map[string]FeatureDefinition
}

type OperationAdmission struct {
	MaxInFlight int
}

type AdmissionPolicy struct {
	MaxInFlight *int
	Operations  map[string]OperationAdmission
}

type AdmissionConfig struct {
	Policies map[string]AdmissionPolicy
}

type CreditAllowance struct {
	Amount   Amount
	Priority int
	Window   Window
}

type QuotaDefinition struct {
	Operation     string
	Measure       string
	Limit         Amount
	Window        Window
	Enforcement   string
	EmitAtPercent []int
}

type PlanEvolution struct {
	DefaultRollout string
}

type PlanDefinition struct {
	DisplayName       string
	Rank              int
	Description       *string
	RateCard          *string
	AllowedOperations []string
	Features          map[string]any
	CreditAllowance   *CreditAllowance
	Quotas            map[string]QuotaDefinition
	CreditPolicy      *string
	AdmissionPolicy   *string
	Evolution         PlanEvolution
}

type ProviderDefinition struct {
	Type    string
	Adapter *string
}

type ProviderReference struct {
	Type       string
	PriceID    *string
	ProductID  *string
	ObjectKind *string
	ExternalID *string
}

type OfferPrice struct {
	AmountMinor int64  `json:"amountMinor"`
	Currency    string `json:"currency"`
	TaxBehavior string `json:"taxBehavior"`
}

type OfferQuantity struct {
	Minimum int `json:"minimum"`
	Maximum int `json:"maximum"`
	Default int `json:"default"`
}

type CycleGrant struct {
	Amount  Amount
	Bucket  string
	Renewal string
	Expiry  ExpiryPolicy
}

type CommerceOffer struct {
	Type            string
	DisplayName     string
	Description     *string
	SortOrder       int
	Availability    *Availability
	Price           OfferPrice
	Providers       map[string]ProviderReference
	Plan            *string
	BillingInterval *BillingInterval
	Trial           *BillingInterval
	CycleGrant      *CycleGrant
	CreditsPerUnit  *Amount
	Quantity        *OfferQuantity
	Bucket          *string
	Expiry          *ExpiryPolicy
	LotBehavior     string
}

type SubscriptionChangePolicy struct {
	Effective      string
	Proration      string
	PaymentFailure string
}

type DecimalRange struct {
	Minimum Amount
	Maximum Amount
	Default Amount
}

type AutoRechargeLimits struct {
	MaxPurchases          int
	Window                Window
	MaxChargeMinor        int64
	Cooldown              Duration
	MaxConsecutiveFailure int
	FailureAction         string
}

type AutoRechargeGuardrails struct {
	EligibleTopups []string
	BalanceBelow   DecimalRange
	RearmAbove     Amount
	Quantity       OfferQuantity
	Limits         AutoRechargeLimits
}

type CommerceConfig struct {
	Providers           map[string]ProviderDefinition
	Offers              map[string]CommerceOffer
	SubscriptionChanges map[string]SubscriptionChangePolicy
	AutoRecharge        *AutoRechargeGuardrails
}

type PlanRollout struct {
	Effective     string
	IncludePinned bool
}

type CatalogRollout struct {
	Plans map[string]PlanRollout
}

// schemaRegexp adapts regexp2's ECMAScript-compatible engine to the schema
// validator. The canonical schema intentionally uses ECMA lookaheads, which
// Go's RE2 implementation does not support.
type schemaRegexp struct {
	source string
	value  *regexp2.Regexp
}

func (r schemaRegexp) String() string { return r.source }

func (r schemaRegexp) MatchString(value string) bool {
	matches, err := r.value.MatchString(value)
	return err == nil && matches
}

func compileSchemaRegexp(expression string) (jsonschema.Regexp, error) {
	compiled, err := regexp2.Compile(expression, regexp2.ECMAScript)
	if err != nil {
		return nil, err
	}
	return schemaRegexp{source: expression, value: compiled}, nil
}

const (
	configSchemaURL = "https://zonastery.github.io/bursar/pricing-config.schema.json"
	safeIntegerMax  = int64(9007199254740991)
)

//go:embed internal/config/pricing-config.schema.json
var pricingConfigSchemaJSON []byte

var (
	compiledConfigSchemaOnce sync.Once
	compiledConfigSchema     *jsonschema.Schema
	compiledConfigSchemaErr  error
)

// LoadConfigFromMap validates and parses a raw snake_case configuration map.
func LoadConfigFromMap(data map[string]any) (*BursarConfig, error) {
	if data == nil {
		return nil, newConfigError("configuration must be an object", nil)
	}
	// Programmatic Go callers naturally construct []string and typed maps,
	// whereas JSON decoded input uses []any. Normalize those containers without
	// converting any number through float64 before schema validation.
	normalized := canonicalJSONMap(data)
	if err := validateConfigStructure(normalized); err != nil {
		return nil, err
	}
	return parseBursarConfig(normalized)
}

// LoadConfigFromDict is a compatibility alias for callers migrating from the
// Python and JavaScript SDK terminology.
func LoadConfigFromDict(data map[string]any) (*BursarConfig, error) {
	return LoadConfigFromMap(data)
}

// LoadConfigJSON decodes JSON without converting numbers through float64,
// then validates and parses the configuration.
func LoadConfigJSON(data []byte) (*BursarConfig, error) {
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.UseNumber()
	raw, err := decodeJSONValue(decoder)
	if err != nil {
		return nil, newConfigError("invalid JSON configuration", err)
	}
	if _, err := decoder.Token(); err != io.EOF {
		if err == nil {
			return nil, newConfigError("invalid JSON configuration: trailing content", nil)
		}
		return nil, newConfigError("invalid JSON configuration", err)
	}
	object, ok := raw.(map[string]any)
	if !ok {
		return nil, newConfigError("configuration must be an object", nil)
	}
	return LoadConfigFromMap(object)
}

// decodeJSONValue recursively decodes an arbitrary JSON value while rejecting
// duplicate object keys. encoding/json otherwise follows JavaScript's
// last-key-wins behavior, which is unsafe for financial configuration because
// a duplicate can silently replace a reviewed value.
func decodeJSONValue(decoder *json.Decoder) (any, error) {
	token, err := decoder.Token()
	if err != nil {
		return nil, err
	}
	delimiter, isDelimiter := token.(json.Delim)
	if !isDelimiter {
		return token, nil
	}
	switch delimiter {
	case '{':
		result := map[string]any{}
		for decoder.More() {
			keyToken, err := decoder.Token()
			if err != nil {
				return nil, err
			}
			key, ok := keyToken.(string)
			if !ok {
				return nil, fmt.Errorf("JSON object key is not a string")
			}
			if _, exists := result[key]; exists {
				return nil, fmt.Errorf("duplicate JSON key %q", key)
			}
			value, err := decodeJSONValue(decoder)
			if err != nil {
				return nil, err
			}
			result[key] = value
		}
		end, err := decoder.Token()
		if err != nil {
			return nil, err
		}
		if end != json.Delim('}') {
			return nil, fmt.Errorf("expected JSON object terminator")
		}
		return result, nil
	case '[':
		result := []any{}
		for decoder.More() {
			value, err := decodeJSONValue(decoder)
			if err != nil {
				return nil, err
			}
			result = append(result, value)
		}
		end, err := decoder.Token()
		if err != nil {
			return nil, err
		}
		if end != json.Delim(']') {
			return nil, fmt.Errorf("expected JSON array terminator")
		}
		return result, nil
	default:
		return nil, fmt.Errorf("unexpected JSON delimiter %q", delimiter)
	}
}

// LoadConfigYAML decodes a YAML mapping and rejects aliases, non-string keys,
// and duplicate mapping keys before applying the same schema and semantics as
// JSON input.
func LoadConfigYAML(data []byte) (*BursarConfig, error) {
	var node yaml.Node
	if err := yaml.Unmarshal(data, &node); err != nil {
		return nil, newConfigError("invalid YAML configuration", err)
	}
	if err := validateYAMLNode(&node); err != nil {
		return nil, err
	}
	var raw any
	if err := node.Decode(&raw); err != nil {
		return nil, newConfigError("invalid YAML configuration", err)
	}
	object, ok := normalizeYAMLValue(raw).(map[string]any)
	if !ok {
		return nil, newConfigError("configuration must be an object", nil)
	}
	return LoadConfigFromMap(object)
}

// LoadConfigFile loads a .json, .yaml, or .yml catalog file. It intentionally
// contains no command-line behavior, so applications remain free to compose it
// with their own configuration systems.
func LoadConfigFile(filename string) (*BursarConfig, error) {
	contents, err := os.ReadFile(filename)
	if err != nil {
		return nil, newConfigError("unable to read configuration file", err)
	}
	switch strings.ToLower(filepath.Ext(filename)) {
	case ".json":
		return LoadConfigJSON(contents)
	case ".yaml", ".yml":
		return LoadConfigYAML(contents)
	default:
		return nil, newConfigError("configuration file must end in .json, .yaml, or .yml", nil)
	}
}

func validateConfigStructure(data map[string]any) error {
	compiledConfigSchemaOnce.Do(func() {
		decoder := json.NewDecoder(bytes.NewReader(pricingConfigSchemaJSON))
		decoder.UseNumber()
		var schemaDocument any
		if err := decoder.Decode(&schemaDocument); err != nil {
			compiledConfigSchemaErr = fmt.Errorf("decode embedded pricing config schema: %w", err)
			return
		}
		compiler := jsonschema.NewCompiler()
		compiler.UseRegexpEngine(compileSchemaRegexp)
		compiler.AssertFormat()
		if err := compiler.AddResource(configSchemaURL, schemaDocument); err != nil {
			compiledConfigSchemaErr = fmt.Errorf("register embedded pricing config schema: %w", err)
			return
		}
		compiledConfigSchema, compiledConfigSchemaErr = compiler.Compile(configSchemaURL)
	})
	if compiledConfigSchemaErr != nil {
		return newConfigError("embedded pricing config schema is invalid", compiledConfigSchemaErr)
	}
	if err := compiledConfigSchema.Validate(data); err != nil {
		return newConfigError("configuration does not match the Bursar schema", err)
	}
	return nil
}

func validateYAMLNode(node *yaml.Node) error {
	if node == nil {
		return newConfigError("configuration must be an object", nil)
	}
	switch node.Kind {
	case yaml.DocumentNode:
		for _, child := range node.Content {
			if err := validateYAMLNode(child); err != nil {
				return err
			}
		}
	case yaml.AliasNode:
		return newConfigError("YAML aliases are not supported in configuration", nil)
	case yaml.MappingNode:
		keys := make(map[string]struct{}, len(node.Content)/2)
		for index := 0; index+1 < len(node.Content); index += 2 {
			key := node.Content[index]
			if key.Kind != yaml.ScalarNode || key.Tag != "!!str" {
				return newConfigError("YAML configuration keys must be strings", nil)
			}
			if _, exists := keys[key.Value]; exists {
				return newConfigError(fmt.Sprintf("YAML configuration contains duplicate key %q", key.Value), nil)
			}
			keys[key.Value] = struct{}{}
			if err := validateYAMLNode(node.Content[index+1]); err != nil {
				return err
			}
		}
	case yaml.SequenceNode:
		for _, child := range node.Content {
			if err := validateYAMLNode(child); err != nil {
				return err
			}
		}
	}
	return nil
}

func normalizeYAMLValue(value any) any {
	switch typed := value.(type) {
	case map[string]any:
		result := make(map[string]any, len(typed))
		for key, child := range typed {
			result[key] = normalizeYAMLValue(child)
		}
		return result
	case map[any]any:
		result := make(map[string]any, len(typed))
		for key, child := range typed {
			stringKey, ok := key.(string)
			if !ok {
				return value
			}
			result[stringKey] = normalizeYAMLValue(child)
		}
		return result
	case []any:
		result := make([]any, len(typed))
		for index, child := range typed {
			result[index] = normalizeYAMLValue(child)
		}
		return result
	default:
		return value
	}
}

func configObject(value any, path string) (map[string]any, error) {
	object, ok := value.(map[string]any)
	if !ok {
		return nil, newConfigError(path+" must be an object", nil)
	}
	return object, nil
}

func configArray(value any, path string) ([]any, error) {
	array, ok := value.([]any)
	if !ok {
		return nil, newConfigError(path+" must be an array", nil)
	}
	return array, nil
}

func configString(value any, path string) (string, error) {
	stringValue, ok := value.(string)
	if !ok {
		return "", newConfigError(path+" must be a string", nil)
	}
	return stringValue, nil
}

func configBool(value any, path string) (bool, error) {
	boolValue, ok := value.(bool)
	if !ok {
		return false, newConfigError(path+" must be boolean", nil)
	}
	return boolValue, nil
}

func configInteger(value any, path string) (int, error) {
	parsed, err := configInt64(value, path)
	if err != nil {
		return 0, err
	}
	if int64(int(parsed)) != parsed {
		return 0, newConfigError(path+" is outside the platform integer range", nil)
	}
	return int(parsed), nil
}

func configInt64(value any, path string) (int64, error) {
	var parsed int64
	switch number := value.(type) {
	case json.Number:
		var err error
		parsed, err = number.Int64()
		if err != nil {
			return 0, newConfigError(path+" must be an integer", err)
		}
	case int:
		parsed = int64(number)
	case int8:
		parsed = int64(number)
	case int16:
		parsed = int64(number)
	case int32:
		parsed = int64(number)
	case int64:
		parsed = number
	case uint:
		if uint64(number) > uint64(math.MaxInt64) {
			return 0, newConfigError(path+" is outside the platform integer range", nil)
		}
		parsed = int64(number)
	case uint8:
		parsed = int64(number)
	case uint16:
		parsed = int64(number)
	case uint32:
		parsed = int64(number)
	case uint64:
		if number > uint64(math.MaxInt64) {
			return 0, newConfigError(path+" is outside the platform integer range", nil)
		}
		parsed = int64(number)
	case float64:
		if math.IsNaN(number) || math.IsInf(number, 0) || math.Trunc(number) != number {
			return 0, newConfigError(path+" must be an integer", nil)
		}
		if number > float64(safeIntegerMax) || number < -float64(safeIntegerMax) {
			return 0, newConfigError(path+" is outside the safe integer range", nil)
		}
		parsed = int64(number)
	default:
		return 0, newConfigError(path+" must be an integer", nil)
	}
	if parsed > safeIntegerMax || parsed < -safeIntegerMax {
		return 0, newConfigError(path+" is outside the safe integer range", nil)
	}
	return parsed, nil
}

func configDecimal(value any, path string) (Amount, error) {
	stringValue, ok := value.(string)
	if !ok {
		return decimal.Zero, newConfigError(path+" must be a decimal string", nil)
	}
	parsed, err := NewAmount(stringValue)
	if err != nil {
		return decimal.Zero, newConfigError(path+" must be a valid decimal string", err)
	}
	return parsed, nil
}

var configIdentifierPattern = regexp.MustCompile(`^[a-z][a-z0-9_]*$`)
var configRegionPattern = regexp.MustCompile(`^[A-Z]{2}(?:-[A-Z0-9]{1,3})?$`)

func validateConfigIdentifier(value, path string) error {
	if !configIdentifierPattern.MatchString(value) {
		return newConfigError(path+" must be a non-empty snake_case identifier", nil)
	}
	return nil
}

func validateConfigIdentifiers(values map[string]any, path string) error {
	for key := range values {
		if err := validateConfigIdentifier(key, path+"."+key); err != nil {
			return err
		}
	}
	return nil
}

func sortedConfigKeys(values map[string]any) []string {
	keys := make([]string, 0, len(values))
	for key := range values {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	return keys
}

func configOptionalString(raw map[string]any, key, path string) (*string, error) {
	value, ok := raw[key]
	if !ok || value == nil {
		return nil, nil
	}
	parsed, err := configString(value, path)
	if err != nil {
		return nil, err
	}
	return &parsed, nil
}

func configOptionalInt(raw map[string]any, key, path string) (*int, error) {
	value, ok := raw[key]
	if !ok || value == nil {
		return nil, nil
	}
	parsed, err := configInteger(value, path)
	if err != nil {
		return nil, err
	}
	return &parsed, nil
}

func configMap(raw map[string]any, key, path string) (map[string]any, error) {
	value, ok := raw[key]
	if !ok || value == nil {
		return map[string]any{}, nil
	}
	return configObject(value, path)
}

func configSliceStrings(value any, path string) ([]string, error) {
	items, err := configArray(value, path)
	if err != nil {
		return nil, err
	}
	result := make([]string, len(items))
	for index, item := range items {
		parsed, err := configString(item, fmt.Sprintf("%s[%d]", path, index))
		if err != nil {
			return nil, err
		}
		result[index] = parsed
	}
	return result, nil
}

func parseDuration(value any, path string) (Duration, error) {
	raw, err := configObject(value, path)
	if err != nil {
		return Duration{}, err
	}
	unit, err := configString(raw["unit"], path+".unit")
	if err != nil {
		return Duration{}, err
	}
	count, err := configInteger(raw["count"], path+".count")
	if err != nil {
		return Duration{}, err
	}
	return Duration{Unit: unit, Count: count}, nil
}

func parseBillingInterval(value any, path string) (BillingInterval, error) {
	raw, err := configObject(value, path)
	if err != nil {
		return BillingInterval{}, err
	}
	unit, err := configString(raw["unit"], path+".unit")
	if err != nil {
		return BillingInterval{}, err
	}
	count := 1
	if input, ok := raw["count"]; ok && input != nil {
		count, err = configInteger(input, path+".count")
		if err != nil {
			return BillingInterval{}, err
		}
	}
	return BillingInterval{Unit: unit, Count: count}, nil
}

func parseTimezone(value any, path string) (string, error) {
	zone, err := configString(value, path)
	if err != nil {
		return "", err
	}
	if zone == "" || zone == "Local" {
		return "", newConfigError(path+" must be a valid IANA timezone", nil)
	}
	if _, err := time.LoadLocation(zone); err != nil {
		return "", newConfigError(path+" must be a valid IANA timezone", err)
	}
	return zone, nil
}

func parseWindow(value any, path string) (Window, error) {
	raw, err := configObject(value, path)
	if err != nil {
		return Window{}, err
	}
	typeValue, err := configString(raw["type"], path+".type")
	if err != nil {
		return Window{}, err
	}
	switch typeValue {
	case "rolling":
		duration, err := parseDuration(raw["duration"], path+".duration")
		if err != nil {
			return Window{}, err
		}
		return Window{Type: "rolling", Duration: &duration}, nil
	case "plan_assignment":
		interval, err := parseBillingInterval(raw["interval"], path+".interval")
		if err != nil {
			return Window{}, err
		}
		timezone := "UTC"
		if input, ok := raw["timezone"]; ok && input != nil {
			timezone, err = parseTimezone(input, path+".timezone")
		} else {
			_, err = time.LoadLocation(timezone)
		}
		if err != nil {
			return Window{}, newConfigError(path+".timezone must be a valid IANA timezone", err)
		}
		return Window{Type: "plan_assignment", Interval: &interval, Timezone: timezone}, nil
	case "calendar":
		unit, err := configString(raw["unit"], path+".unit")
		if err != nil {
			return Window{}, err
		}
		count := 1
		if input, ok := raw["count"]; ok && input != nil {
			count, err = configInteger(input, path+".count")
			if err != nil {
				return Window{}, err
			}
		}
		timezone := "UTC"
		if input, ok := raw["timezone"]; ok && input != nil {
			timezone, err = parseTimezone(input, path+".timezone")
			if err != nil {
				return Window{}, err
			}
		}
		return Window{Type: "calendar", Unit: unit, Count: count, Timezone: timezone}, nil
	default:
		return Window{}, newConfigError(path+".type must be calendar, rolling, or plan_assignment", nil)
	}
}

func parseAvailability(value any, path string) (Availability, error) {
	raw, err := configObject(value, path)
	if err != nil {
		return Availability{}, err
	}
	startsAt, err := configOptionalString(raw, "starts_at", path+".starts_at")
	if err != nil {
		return Availability{}, err
	}
	endsAt, err := configOptionalString(raw, "ends_at", path+".ends_at")
	if err != nil {
		return Availability{}, err
	}
	regions := []string{}
	if input, ok := raw["regions"]; ok && input != nil {
		regions, err = configSliceStrings(input, path+".regions")
		if err != nil {
			return Availability{}, err
		}
	}
	seen := make(map[string]struct{}, len(regions))
	for _, region := range regions {
		if !configRegionPattern.MatchString(region) {
			return Availability{}, newConfigError(path+".regions must contain uppercase ISO-style region codes", nil)
		}
		if _, exists := seen[region]; exists {
			return Availability{}, newConfigError(path+".regions must not contain duplicates", nil)
		}
		seen[region] = struct{}{}
	}
	if startsAt != nil && endsAt != nil {
		start, startErr := time.Parse(time.RFC3339, *startsAt)
		end, endErr := time.Parse(time.RFC3339, *endsAt)
		if startErr == nil && endErr == nil && !end.After(start) {
			return Availability{}, newConfigError(path+".ends_at must be later than starts_at", nil)
		}
	}
	return Availability{StartsAt: startsAt, EndsAt: endsAt, Regions: regions}, nil
}

func parseExpiry(value any, path string) (ExpiryPolicy, error) {
	raw, err := configObject(value, path)
	if err != nil {
		return ExpiryPolicy{}, err
	}
	typeValue, err := configString(raw["type"], path+".type")
	if err != nil {
		return ExpiryPolicy{}, err
	}
	switch typeValue {
	case "never", "subscription_end":
		return ExpiryPolicy{Type: typeValue}, nil
	case "after_grant":
		interval, err := parseBillingInterval(raw["interval"], path+".interval")
		if err != nil {
			return ExpiryPolicy{}, err
		}
		timezone := "UTC"
		if input, ok := raw["timezone"]; ok && input != nil {
			timezone, err = parseTimezone(input, path+".timezone")
			if err != nil {
				return ExpiryPolicy{}, err
			}
		}
		return ExpiryPolicy{Type: typeValue, Interval: &interval, Timezone: timezone}, nil
	case "end_of_window":
		window, err := parseWindow(raw["window"], path+".window")
		if err != nil {
			return ExpiryPolicy{}, err
		}
		if window.Type == "rolling" {
			return ExpiryPolicy{}, newConfigError(path+" cannot use a rolling window", nil)
		}
		return ExpiryPolicy{Type: typeValue, Window: &window}, nil
	case "fixed_at":
		at, err := configString(raw["at"], path+".at")
		if err != nil {
			return ExpiryPolicy{}, err
		}
		return ExpiryPolicy{Type: typeValue, At: &at}, nil
	default:
		return ExpiryPolicy{}, newConfigError(path+".type is invalid", nil)
	}
}

func parseBursarConfig(raw map[string]any) (*BursarConfig, error) {
	version, err := configInteger(raw["version"], "version")
	if err != nil {
		return nil, err
	}
	if version != 1 {
		return nil, newConfigError("version must equal 1", nil)
	}

	catalogRaw, err := configMap(raw, "catalog", "catalog")
	if err != nil {
		return nil, err
	}
	defaultPlan, err := configOptionalString(catalogRaw, "default_plan", "catalog.default_plan")
	if err != nil {
		return nil, err
	}

	credits, err := parseCredits(raw["credits"])
	if err != nil {
		return nil, err
	}
	entitlements, err := parseEntitlements(raw["entitlements"])
	if err != nil {
		return nil, err
	}
	admission, err := parseAdmission(raw["admission"])
	if err != nil {
		return nil, err
	}
	var pricing *PricingConfig
	if input, ok := raw["pricing"]; ok && input != nil {
		pricing, err = parsePricing(input)
		if err != nil {
			return nil, err
		}
	}
	if pricing != nil {
		for policyKey, policy := range admission.Policies {
			for operation := range policy.Operations {
				if _, exists := pricing.Operations[operation]; !exists {
					return nil, newConfigError(fmt.Sprintf("admission policy '%s' references unknown operation %s", policyKey, operation), nil)
				}
			}
		}
	}
	commerce, err := parseCommerce(raw["commerce"], credits)
	if err != nil {
		return nil, err
	}
	plans, err := parsePlans(raw["plans"], pricing, credits, entitlements, admission, commerce)
	if err != nil {
		return nil, err
	}
	for offerKey, offer := range commerce.Offers {
		if offer.Type == "subscription" && (offer.Plan == nil || plans[*offer.Plan].DisplayName == "") {
			return nil, newConfigError(fmt.Sprintf("commerce.offers.%s.plan references unknown plan", offerKey), nil)
		}
	}
	for programKey, program := range credits.GrantPrograms {
		for _, plan := range program.EligibilityPlans {
			if _, exists := plans[plan]; !exists {
				return nil, newConfigError(fmt.Sprintf("grant program '%s' references unknown plans %s", programKey, plan), nil)
			}
		}
	}
	if defaultPlan != nil {
		if _, exists := plans[*defaultPlan]; !exists {
			return nil, newConfigError("catalog.default_plan references unknown plan '"+*defaultPlan+"'", nil)
		}
	}
	if len(plans) > 0 && defaultPlan == nil {
		return nil, newConfigError("catalog.default_plan is required when plans are configured", nil)
	}
	return &BursarConfig{
		Version:      1,
		Catalog:      CatalogConfig{DefaultPlan: defaultPlan},
		Pricing:      pricing,
		Credits:      credits,
		Entitlements: entitlements,
		Admission:    admission,
		Plans:        plans,
		Commerce:     commerce,
	}, nil
}

func parseCredits(value any) (CreditsConfig, error) {
	raw, err := configObject(value, "credits")
	if err != nil {
		return CreditsConfig{}, err
	}
	bucketsRaw, err := configMap(raw, "buckets", "credits.buckets")
	if err != nil {
		return CreditsConfig{}, err
	}
	policiesRaw, err := configMap(raw, "policies", "credits.policies")
	if err != nil {
		return CreditsConfig{}, err
	}
	programsRaw, err := configMap(raw, "grant_programs", "credits.grant_programs")
	if err != nil {
		return CreditsConfig{}, err
	}
	for path, items := range map[string]map[string]any{
		"credits.buckets": bucketsRaw, "credits.policies": policiesRaw, "credits.grant_programs": programsRaw,
	} {
		if err := validateConfigIdentifiers(items, path); err != nil {
			return CreditsConfig{}, err
		}
	}
	buckets := make(map[string]BucketDefinition, len(bucketsRaw))
	for _, key := range sortedConfigKeys(bucketsRaw) {
		bucket, err := configObject(bucketsRaw[key], "credits.buckets."+key)
		if err != nil {
			return CreditsConfig{}, err
		}
		priority, err := configInteger(bucket["priority"], "credits.buckets."+key+".priority")
		if err != nil {
			return CreditsConfig{}, err
		}
		expiryInput := any(map[string]any{"type": "never"})
		if input, exists := bucket["expiry"]; exists && input != nil {
			expiryInput = input
		}
		expiry, err := parseExpiry(expiryInput, "credits.buckets."+key+".expiry")
		if err != nil {
			return CreditsConfig{}, err
		}
		if expiry.Type == "subscription_end" {
			return CreditsConfig{}, newConfigError("credits.buckets."+key+".expiry cannot be subscription_end", nil)
		}
		buckets[key] = BucketDefinition{Priority: priority, Expiry: expiry}
	}
	priorities := make(map[int]string, len(buckets))
	for key, bucket := range buckets {
		if previous, exists := priorities[bucket.Priority]; exists {
			return CreditsConfig{}, newConfigError(fmt.Sprintf("credits bucket priorities must be unique (%s and %s)", previous, key), nil)
		}
		priorities[bucket.Priority] = key
	}
	defaultBucket, err := configOptionalString(raw, "default_bucket", "credits.default_bucket")
	if err != nil {
		return CreditsConfig{}, err
	}
	if defaultBucket != nil {
		if _, exists := buckets[*defaultBucket]; !exists {
			return CreditsConfig{}, newConfigError("credits.default_bucket references an unknown bucket", nil)
		}
	}
	policies := make(map[string]CreditPolicy, len(policiesRaw))
	for _, key := range sortedConfigKeys(policiesRaw) {
		policy, err := configObject(policiesRaw[key], "credits.policies."+key)
		if err != nil {
			return CreditsConfig{}, err
		}
		typeValue, err := configString(policy["type"], "credits.policies."+key+".type")
		if err != nil {
			return CreditsConfig{}, err
		}
		parsed := CreditPolicy{Type: typeValue}
		if typeValue == "credit_line" {
			limit, err := configDecimal(policy["limit"], "credits.policies."+key+".limit")
			if err != nil {
				return CreditsConfig{}, err
			}
			parsed.Limit = &limit
		}
		policies[key] = parsed
	}
	grantPrograms := make(map[string]GrantProgram, len(programsRaw))
	for _, key := range sortedConfigKeys(programsRaw) {
		program, err := parseGrantProgram(key, programsRaw[key], buckets)
		if err != nil {
			return CreditsConfig{}, err
		}
		grantPrograms[key] = program
	}
	var display *CreditDisplay
	if input, exists := raw["display"]; exists && input != nil {
		displayRaw, err := configObject(input, "credits.display")
		if err != nil {
			return CreditsConfig{}, err
		}
		currency, err := configString(displayRaw["currency"], "credits.display.currency")
		if err != nil {
			return CreditsConfig{}, err
		}
		units, err := configDecimal(displayRaw["units_per_major"], "credits.display.units_per_major")
		if err != nil {
			return CreditsConfig{}, err
		}
		display = &CreditDisplay{Currency: strings.ToUpper(currency), UnitsPerMajor: units}
	}
	return CreditsConfig{
		Buckets:       buckets,
		DefaultBucket: defaultBucket,
		Policies:      policies,
		GrantPrograms: grantPrograms,
		Display:       display,
	}, nil
}

func parseGrantProgram(key string, value any, buckets map[string]BucketDefinition) (GrantProgram, error) {
	path := "credits.grant_programs." + key
	raw, err := configObject(value, path)
	if err != nil {
		return GrantProgram{}, err
	}
	trigger, err := configString(raw["trigger"], path+".trigger")
	if err != nil {
		return GrantProgram{}, err
	}
	awardsRaw, err := configArray(raw["awards"], path+".awards")
	if err != nil {
		return GrantProgram{}, err
	}
	awards := make([]GrantAward, 0, len(awardsRaw))
	for index, input := range awardsRaw {
		awardPath := fmt.Sprintf("%s.awards[%d]", path, index)
		awardRaw, err := configObject(input, awardPath)
		if err != nil {
			return GrantProgram{}, err
		}
		recipient := "subject"
		if input, exists := awardRaw["recipient"]; exists && input != nil {
			recipient, err = configString(input, awardPath+".recipient")
			if err != nil {
				return GrantProgram{}, err
			}
		}
		amount, err := configDecimal(awardRaw["amount"], awardPath+".amount")
		if err != nil {
			return GrantProgram{}, err
		}
		bucket, err := configString(awardRaw["bucket"], awardPath+".bucket")
		if err != nil {
			return GrantProgram{}, err
		}
		if _, exists := buckets[bucket]; !exists {
			return GrantProgram{}, newConfigError(fmt.Sprintf("grant program '%s' references unknown bucket '%s'", key, bucket), nil)
		}
		var expiry *ExpiryPolicy
		if input, exists := awardRaw["expiry"]; exists && input != nil {
			parsed, err := parseExpiry(input, awardPath+".expiry")
			if err != nil {
				return GrantProgram{}, err
			}
			if parsed.Type == "subscription_end" {
				return GrantProgram{}, newConfigError(fmt.Sprintf("grant program '%s' cannot use subscription_end expiry", key), nil)
			}
			expiry = &parsed
		}
		awards = append(awards, GrantAward{Recipient: recipient, Amount: amount, Bucket: bucket, Expiry: expiry})
	}
	eligibilityRaw, err := configMap(raw, "eligibility", path+".eligibility")
	if err != nil {
		return GrantProgram{}, err
	}
	plans := []string{}
	if input, exists := eligibilityRaw["plans"]; exists && input != nil {
		plans, err = configSliceStrings(input, path+".eligibility.plans")
		if err != nil {
			return GrantProgram{}, err
		}
	}
	regions := []string{}
	if input, exists := eligibilityRaw["regions"]; exists && input != nil {
		regions, err = configSliceStrings(input, path+".eligibility.regions")
		if err != nil {
			return GrantProgram{}, err
		}
	}
	seenRegions := map[string]struct{}{}
	for _, region := range regions {
		if !configRegionPattern.MatchString(region) {
			return GrantProgram{}, newConfigError(path+".eligibility.regions must contain uppercase ISO-style region codes", nil)
		}
		if _, exists := seenRegions[region]; exists {
			return GrantProgram{}, newConfigError(path+".eligibility.regions must not contain duplicates", nil)
		}
		seenRegions[region] = struct{}{}
	}
	if trigger != "referral_completed" {
		for _, award := range awards {
			if award.Recipient == "referrer" {
				return GrantProgram{}, newConfigError(fmt.Sprintf("grant program '%s' referrer awards require referral_completed", key), nil)
			}
		}
	}
	maxAwards := 1
	if input, exists := raw["max_awards_per_subject"]; exists && input != nil {
		maxAwards, err = configInteger(input, path+".max_awards_per_subject")
		if err != nil {
			return GrantProgram{}, err
		}
	}
	idempotencyScope := "subject"
	if input, exists := raw["idempotency_scope"]; exists && input != nil {
		idempotencyScope, err = configString(input, path+".idempotency_scope")
		if err != nil {
			return GrantProgram{}, err
		}
	}
	var availability *Availability
	if input, exists := raw["availability"]; exists && input != nil {
		parsed, err := parseAvailability(input, path+".availability")
		if err != nil {
			return GrantProgram{}, err
		}
		availability = &parsed
	}
	return GrantProgram{
		Trigger:             trigger,
		Awards:              awards,
		Availability:        availability,
		EligibilityPlans:    plans,
		EligibilityRegions:  regions,
		MaxAwardsPerSubject: maxAwards,
		IdempotencyScope:    idempotencyScope,
	}, nil
}

func parseEntitlements(value any) (EntitlementsConfig, error) {
	if value == nil {
		return EntitlementsConfig{Features: map[string]FeatureDefinition{}}, nil
	}
	raw, err := configObject(value, "entitlements")
	if err != nil {
		return EntitlementsConfig{}, err
	}
	featuresRaw, err := configMap(raw, "features", "entitlements.features")
	if err != nil {
		return EntitlementsConfig{}, err
	}
	if err := validateConfigIdentifiers(featuresRaw, "entitlements.features"); err != nil {
		return EntitlementsConfig{}, err
	}
	features := make(map[string]FeatureDefinition, len(featuresRaw))
	for _, key := range sortedConfigKeys(featuresRaw) {
		path := "entitlements.features." + key
		rawFeature, err := configObject(featuresRaw[key], path)
		if err != nil {
			return EntitlementsConfig{}, err
		}
		typeValue, err := configString(rawFeature["type"], path+".type")
		if err != nil {
			return EntitlementsConfig{}, err
		}
		definition := FeatureDefinition{Type: typeValue, Default: rawFeature["default"]}
		switch typeValue {
		case "boolean":
			if _, err := configBool(definition.Default, path+".default"); err != nil {
				return EntitlementsConfig{}, err
			}
		case "enum":
			values, err := configSliceStrings(rawFeature["values"], path+".values")
			if err != nil {
				return EntitlementsConfig{}, err
			}
			defaultValue, err := configString(definition.Default, path+".default")
			if err != nil {
				return EntitlementsConfig{}, err
			}
			seen := map[string]struct{}{}
			found := false
			for _, item := range values {
				if _, exists := seen[item]; exists {
					return EntitlementsConfig{}, newConfigError(path+".values must be unique", nil)
				}
				seen[item] = struct{}{}
				found = found || item == defaultValue
			}
			if !found {
				return EntitlementsConfig{}, newConfigError(path+".default must be one of values", nil)
			}
			definition.Values = values
		case "integer":
			defaultValue, err := configInteger(definition.Default, path+".default")
			if err != nil {
				return EntitlementsConfig{}, err
			}
			definition.Default = defaultValue
			minimum, err := configOptionalInt(rawFeature, "minimum", path+".minimum")
			if err != nil {
				return EntitlementsConfig{}, err
			}
			maximum, err := configOptionalInt(rawFeature, "maximum", path+".maximum")
			if err != nil {
				return EntitlementsConfig{}, err
			}
			if minimum != nil && maximum != nil && *minimum > *maximum {
				return EntitlementsConfig{}, newConfigError(path+".minimum cannot exceed maximum", nil)
			}
			if minimum != nil && defaultValue < *minimum {
				return EntitlementsConfig{}, newConfigError(path+".default is below minimum", nil)
			}
			if maximum != nil && defaultValue > *maximum {
				return EntitlementsConfig{}, newConfigError(path+".default exceeds maximum", nil)
			}
			definition.Minimum, definition.Maximum = minimum, maximum
		case "string":
			if _, err := configString(definition.Default, path+".default"); err != nil {
				return EntitlementsConfig{}, err
			}
			if pattern, err := configOptionalString(rawFeature, "pattern", path+".pattern"); err != nil {
				return EntitlementsConfig{}, err
			} else if pattern != nil {
				if _, err := regexp2.Compile(*pattern, regexp2.ECMAScript); err != nil {
					return EntitlementsConfig{}, newConfigError(path+".pattern must be a valid regular expression", err)
				}
				definition.Pattern = pattern
			}
		default:
			return EntitlementsConfig{}, newConfigError(path+".type is invalid", nil)
		}
		features[key] = definition
	}
	return EntitlementsConfig{Features: features}, nil
}

func parseAdmission(value any) (AdmissionConfig, error) {
	if value == nil {
		return AdmissionConfig{Policies: map[string]AdmissionPolicy{}}, nil
	}
	raw, err := configObject(value, "admission")
	if err != nil {
		return AdmissionConfig{}, err
	}
	policiesRaw, err := configMap(raw, "policies", "admission.policies")
	if err != nil {
		return AdmissionConfig{}, err
	}
	if err := validateConfigIdentifiers(policiesRaw, "admission.policies"); err != nil {
		return AdmissionConfig{}, err
	}
	policies := make(map[string]AdmissionPolicy, len(policiesRaw))
	for _, key := range sortedConfigKeys(policiesRaw) {
		path := "admission.policies." + key
		policyRaw, err := configObject(policiesRaw[key], path)
		if err != nil {
			return AdmissionConfig{}, err
		}
		maxInFlight, err := configOptionalInt(policyRaw, "max_in_flight", path+".max_in_flight")
		if err != nil {
			return AdmissionConfig{}, err
		}
		operationsRaw, err := configMap(policyRaw, "operations", path+".operations")
		if err != nil {
			return AdmissionConfig{}, err
		}
		if err := validateConfigIdentifiers(operationsRaw, path+".operations"); err != nil {
			return AdmissionConfig{}, err
		}
		operations := make(map[string]OperationAdmission, len(operationsRaw))
		for operation, input := range operationsRaw {
			definition, err := configObject(input, path+".operations."+operation)
			if err != nil {
				return AdmissionConfig{}, err
			}
			max, err := configInteger(definition["max_in_flight"], path+".operations."+operation+".max_in_flight")
			if err != nil {
				return AdmissionConfig{}, err
			}
			operations[operation] = OperationAdmission{MaxInFlight: max}
		}
		policies[key] = AdmissionPolicy{MaxInFlight: maxInFlight, Operations: operations}
	}
	return AdmissionConfig{Policies: policies}, nil
}

func parseMatcherScalar(value any, definition DimensionDefinition, path string) (MatcherScalar, error) {
	switch definition.Type {
	case "boolean":
		return configBool(value, path+" matcher values")
	case "number":
		return configMatcherDecimal(value, path+" matcher values")
	case "string":
		return configString(value, path+" matcher values")
	default:
		return nil, newConfigError(path+" references an invalid dimension type", nil)
	}
}

// configMatcherDecimal accepts JSON numeric dimensions as well as decimal
// strings, matching the cross-SDK matcher contract. It never accepts a
// float64 financial amount: this helper is restricted to non-financial
// dimension predicates and preserves JSON numbers through json.Number.
func configMatcherDecimal(value any, path string) (Amount, error) {
	switch typed := value.(type) {
	case string:
		parsed, err := decimal.NewFromString(typed)
		if err != nil {
			return decimal.Zero, newConfigError(path+" must be a decimal", err)
		}
		return parsed, nil
	case json.Number:
		parsed, err := decimal.NewFromString(typed.String())
		if err != nil {
			return decimal.Zero, newConfigError(path+" must be a decimal", err)
		}
		return parsed, nil
	case int:
		return decimal.NewFromInt(int64(typed)), nil
	case int8:
		return decimal.NewFromInt(int64(typed)), nil
	case int16:
		return decimal.NewFromInt(int64(typed)), nil
	case int32:
		return decimal.NewFromInt(int64(typed)), nil
	case int64:
		return decimal.NewFromInt(typed), nil
	case uint:
		return decimal.NewFromString(fmt.Sprintf("%d", typed))
	case uint8:
		return decimal.NewFromInt(int64(typed)), nil
	case uint16:
		return decimal.NewFromInt(int64(typed)), nil
	case uint32:
		return decimal.NewFromInt(int64(typed)), nil
	case uint64:
		return decimal.NewFromString(fmt.Sprintf("%d", typed))
	case float64:
		if math.IsNaN(typed) || math.IsInf(typed, 0) {
			return decimal.Zero, newConfigError(path+" must be finite", nil)
		}
		return decimal.NewFromString(fmt.Sprintf("%g", typed))
	default:
		return decimal.Zero, newConfigError(path+" must be a decimal string or number", nil)
	}
}

func parseDimensionMatcher(value any, definition DimensionDefinition, path string) (DimensionMatcher, error) {
	raw, err := configObject(value, path)
	if err != nil {
		return DimensionMatcher{}, err
	}
	op, err := configString(raw["op"], path+".op")
	if err != nil {
		return DimensionMatcher{}, err
	}
	matcher := DimensionMatcher{Op: op}
	switch op {
	case "eq":
		parsed, err := parseMatcherScalar(raw["value"], definition, path)
		if err != nil {
			return DimensionMatcher{}, err
		}
		matcher.Value = parsed
	case "in", "not_in":
		items, err := configArray(raw["values"], path+".values")
		if err != nil {
			return DimensionMatcher{}, err
		}
		matcher.Values = make([]MatcherScalar, 0, len(items))
		for index, item := range items {
			parsed, err := parseMatcherScalar(item, definition, fmt.Sprintf("%s.values[%d]", path, index))
			if err != nil {
				return DimensionMatcher{}, err
			}
			matcher.Values = append(matcher.Values, parsed)
		}
	case "prefix":
		if definition.Type != "string" {
			return DimensionMatcher{}, newConfigError(path+" prefix matcher requires a string dimension", nil)
		}
		parsed, err := configString(raw["value"], path+".value")
		if err != nil {
			return DimensionMatcher{}, err
		}
		matcher.Value = parsed
	case "range":
		if definition.Type != "number" {
			return DimensionMatcher{}, newConfigError(path+" range matcher requires a number dimension", nil)
		}
		for key, target := range map[string]**Amount{
			"gt": &matcher.GT, "gte": &matcher.GTE, "lt": &matcher.LT, "lte": &matcher.LTE,
		} {
			if input, exists := raw[key]; exists && input != nil {
				parsed, err := configDecimal(input, path+"."+key)
				if err != nil {
					return DimensionMatcher{}, err
				}
				*target = &parsed
			}
		}
		if matcher.GT == nil && matcher.GTE == nil && matcher.LT == nil && matcher.LTE == nil {
			return DimensionMatcher{}, newConfigError(path+" range matcher requires at least one bound", nil)
		}
		if matcher.GT != nil && matcher.GTE != nil {
			return DimensionMatcher{}, newConfigError(path+" range matcher cannot combine gt and gte", nil)
		}
		if matcher.LT != nil && matcher.LTE != nil {
			return DimensionMatcher{}, newConfigError(path+" range matcher cannot combine lt and lte", nil)
		}
		lower := matcher.GT
		if lower == nil {
			lower = matcher.GTE
		}
		upper := matcher.LT
		if upper == nil {
			upper = matcher.LTE
		}
		if lower != nil && upper != nil && !lower.LessThan(*upper) {
			return DimensionMatcher{}, newConfigError(path+" range matcher lower bound must be less than upper bound", nil)
		}
	default:
		return DimensionMatcher{}, newConfigError(path+".op is invalid", nil)
	}
	return matcher, nil
}

func parseTiers(value any, path string) ([]GraduatedTier, error) {
	inputs, err := configArray(value, path)
	if err != nil {
		return nil, err
	}
	tiers := make([]GraduatedTier, 0, len(inputs))
	for index, input := range inputs {
		tierPath := fmt.Sprintf("%s[%d]", path, index)
		raw, err := configObject(input, tierPath)
		if err != nil {
			return nil, err
		}
		rate, err := configDecimal(raw["rate"], tierPath+".rate")
		if err != nil {
			return nil, err
		}
		var upTo *Amount
		if candidate, exists := raw["up_to"]; exists && candidate != nil {
			parsed, err := configDecimal(candidate, tierPath+".up_to")
			if err != nil {
				return nil, err
			}
			upTo = &parsed
		}
		tiers = append(tiers, GraduatedTier{UpTo: upTo, Rate: rate})
	}
	if len(tiers) == 0 || tiers[len(tiers)-1].UpTo != nil {
		return nil, newConfigError("graduated and volume tiers must end with exactly one open-ended tier", nil)
	}
	var previous *Amount
	for _, tier := range tiers[:len(tiers)-1] {
		if tier.UpTo == nil {
			return nil, newConfigError("graduated and volume tiers must end with exactly one open-ended tier", nil)
		}
		if previous != nil && !previous.LessThan(*tier.UpTo) {
			return nil, newConfigError("graduated and volume tier bounds must be strictly increasing", nil)
		}
		current := *tier.UpTo
		previous = &current
	}
	return tiers, nil
}

func parseCharge(value any, path string, depth int) (Charge, error) {
	if depth > 64 {
		return Charge{}, newConfigError(path+" exceeds the maximum nested charge depth", nil)
	}
	raw, err := configObject(value, path)
	if err != nil {
		return Charge{}, err
	}
	typeValue, err := configString(raw["type"], path+".type")
	if err != nil {
		return Charge{}, err
	}
	charge := Charge{Type: typeValue}
	switch typeValue {
	case "flat":
		charge.Amount, err = configDecimal(raw["amount"], path+".amount")
	case "per_unit":
		charge.Measure, err = configString(raw["measure"], path+".measure")
		if err == nil {
			charge.Rate, err = configDecimal(raw["rate"], path+".rate")
		}
		charge.UnitSize = decimal.NewFromInt(1)
		if err == nil {
			if input, exists := raw["unit_size"]; exists && input != nil {
				charge.UnitSize, err = configDecimal(input, path+".unit_size")
			}
		}
		if err == nil && (!charge.UnitSize.GreaterThan(decimal.Zero)) {
			err = newConfigError(path+".unit_size must be greater than zero", nil)
		}
	case "package":
		charge.Measure, err = configString(raw["measure"], path+".measure")
		if err == nil {
			charge.Units, err = configDecimal(raw["units"], path+".units")
		}
		if err == nil {
			charge.Amount, err = configDecimal(raw["amount"], path+".amount")
		}
		charge.Rounding = "ceil"
		if err == nil {
			if input, exists := raw["rounding"]; exists && input != nil {
				charge.Rounding, err = configString(input, path+".rounding")
			}
		}
		if err == nil && !charge.Units.GreaterThan(decimal.Zero) {
			err = newConfigError(path+".units must be greater than zero", nil)
		}
	case "graduated", "volume":
		charge.Measure, err = configString(raw["measure"], path+".measure")
		if err == nil {
			charge.Tiers, err = parseTiers(raw["tiers"], path+".tiers")
		}
	case "expression":
		charge.Formula, err = configString(raw["formula"], path+".formula")
	case "sum":
		inputs, parseErr := configArray(raw["components"], path+".components")
		if parseErr != nil {
			err = parseErr
			break
		}
		charge.Components = make([]Charge, 0, len(inputs))
		for index, input := range inputs {
			component, componentErr := parseCharge(input, fmt.Sprintf("%s.components[%d]", path, index), depth+1)
			if componentErr != nil {
				err = componentErr
				break
			}
			charge.Components = append(charge.Components, component)
		}
	default:
		return Charge{}, newConfigError(fmt.Sprintf("unsupported charge type '%s'", typeValue), nil)
	}
	if err != nil {
		return Charge{}, err
	}
	return charge, nil
}

func chargeMeasures(charge Charge, result map[string]struct{}) {
	if charge.Measure != "" {
		result[charge.Measure] = struct{}{}
	}
	for _, component := range charge.Components {
		chargeMeasures(component, result)
	}
}

func validatePricingCharge(charge Charge, definition OperationDefinition, operationKey string) error {
	measures := map[string]struct{}{}
	chargeMeasures(charge, measures)
	for measure := range measures {
		if _, exists := definition.Measures[measure]; !exists {
			return newConfigError(fmt.Sprintf("pricing for operation '%s' references undeclared measures %s", operationKey, measure), nil)
		}
	}
	var validateExpressionCharge func(Charge) error
	validateExpressionCharge = func(current Charge) error {
		if current.Type == "expression" {
			known := make([]string, 0, len(definition.Measures))
			for name := range definition.Measures {
				known = append(known, name)
			}
			sort.Strings(known)
			if err := ValidateExpression(current.Formula, known); err != nil {
				return err
			}
		}
		for _, component := range current.Components {
			if err := validateExpressionCharge(component); err != nil {
				return err
			}
		}
		return nil
	}
	return validateExpressionCharge(charge)
}

func parsePricing(value any) (*PricingConfig, error) {
	raw, err := configObject(value, "pricing")
	if err != nil {
		return nil, err
	}
	operationsRaw, err := configMap(raw, "operations", "pricing.operations")
	if err != nil {
		return nil, err
	}
	cardsRaw, err := configMap(raw, "rate_cards", "pricing.rate_cards")
	if err != nil {
		return nil, err
	}
	if err := validateConfigIdentifiers(operationsRaw, "pricing.operations"); err != nil {
		return nil, err
	}
	if err := validateConfigIdentifiers(cardsRaw, "pricing.rate_cards"); err != nil {
		return nil, err
	}
	if len(operationsRaw) == 0 || len(cardsRaw) == 0 {
		return nil, newConfigError("pricing.operations and pricing.rate_cards must not be empty", nil)
	}
	operations := make(map[string]OperationDefinition, len(operationsRaw))
	for _, operationKey := range sortedConfigKeys(operationsRaw) {
		path := "pricing.operations." + operationKey
		operationRaw, err := configObject(operationsRaw[operationKey], path)
		if err != nil {
			return nil, err
		}
		measuresRaw, err := configMap(operationRaw, "measures", path+".measures")
		if err != nil {
			return nil, err
		}
		dimensionsRaw, err := configMap(operationRaw, "dimensions", path+".dimensions")
		if err != nil {
			return nil, err
		}
		if err := validateConfigIdentifiers(measuresRaw, path+".measures"); err != nil {
			return nil, err
		}
		if err := validateConfigIdentifiers(dimensionsRaw, path+".dimensions"); err != nil {
			return nil, err
		}
		if len(measuresRaw) == 0 {
			return nil, newConfigError(path+".measures must not be empty", nil)
		}
		measures := make(map[string]MeasureDefinition, len(measuresRaw))
		for name, value := range measuresRaw {
			definition, err := configObject(value, path+".measures."+name)
			if err != nil {
				return nil, err
			}
			unit, err := configString(definition["unit"], path+".measures."+name+".unit")
			if err != nil {
				return nil, err
			}
			if err := validateConfigIdentifier(unit, path+".measures."+name+".unit"); err != nil {
				return nil, err
			}
			measures[name] = MeasureDefinition{Unit: unit}
		}
		dimensions := make(map[string]DimensionDefinition, len(dimensionsRaw))
		for name, value := range dimensionsRaw {
			if _, overlaps := measures[name]; overlaps {
				return nil, newConfigError(fmt.Sprintf("operation '%s' reuses names as measures and dimensions", operationKey), nil)
			}
			definition, err := configObject(value, path+".dimensions."+name)
			if err != nil {
				return nil, err
			}
			typeValue, err := configString(definition["type"], path+".dimensions."+name+".type")
			if err != nil {
				return nil, err
			}
			required := true
			if input, exists := definition["required"]; exists && input != nil {
				required, err = configBool(input, path+".dimensions."+name+".required")
				if err != nil {
					return nil, err
				}
			}
			dimensions[name] = DimensionDefinition{Type: typeValue, Required: required}
		}
		operations[operationKey] = OperationDefinition{Measures: measures, Dimensions: dimensions}
	}

	rateCards := make(map[string]RateCard, len(cardsRaw))
	for _, cardKey := range sortedConfigKeys(cardsRaw) {
		path := "pricing.rate_cards." + cardKey
		cardRaw, err := configObject(cardsRaw[cardKey], path)
		if err != nil {
			return nil, err
		}
		extends, err := configOptionalString(cardRaw, "extends", path+".extends")
		if err != nil {
			return nil, err
		}
		operationPricesRaw, err := configMap(cardRaw, "operations", path+".operations")
		if err != nil {
			return nil, err
		}
		operationPrices := make(map[string]OperationPricing, len(operationPricesRaw))
		for operationKey, input := range operationPricesRaw {
			definition, exists := operations[operationKey]
			if !exists {
				return nil, newConfigError(fmt.Sprintf("rate card '%s' references unknown operation '%s'", cardKey, operationKey), nil)
			}
			operationPath := path + ".operations." + operationKey
			operationRaw, err := configObject(input, operationPath)
			if err != nil {
				return nil, err
			}
			rulesRaw := []any{}
			if input, exists := operationRaw["rules"]; exists && input != nil {
				rulesRaw, err = configArray(input, operationPath+".rules")
				if err != nil {
					return nil, err
				}
			}
			rules := make([]PriceRule, 0, len(rulesRaw))
			for index, ruleInput := range rulesRaw {
				rulePath := fmt.Sprintf("%s.rules[%d]", operationPath, index)
				ruleRaw, err := configObject(ruleInput, rulePath)
				if err != nil {
					return nil, err
				}
				whenRaw, err := configObject(ruleRaw["when"], rulePath+".when")
				if err != nil {
					return nil, err
				}
				when := make(map[string]DimensionMatcher, len(whenRaw))
				for name, matcherInput := range whenRaw {
					dimension, exists := definition.Dimensions[name]
					if !exists {
						return nil, newConfigError(fmt.Sprintf("%s matches undeclared dimension '%s'", rulePath, name), nil)
					}
					matcher, err := parseDimensionMatcher(matcherInput, dimension, rulePath+".when."+name)
					if err != nil {
						return nil, err
					}
					when[name] = matcher
				}
				charge, err := parseCharge(ruleRaw["charge"], rulePath+".charge", 0)
				if err != nil {
					return nil, err
				}
				if err := validatePricingCharge(charge, definition, operationKey); err != nil {
					return nil, err
				}
				rules = append(rules, PriceRule{When: when, Charge: charge})
			}
			unmatchedRaw, err := configObject(operationRaw["unmatched"], operationPath+".unmatched")
			if err != nil {
				return nil, err
			}
			action, err := configString(unmatchedRaw["action"], operationPath+".unmatched.action")
			if err != nil {
				return nil, err
			}
			unmatched := UnmatchedPolicy{Action: action}
			if action == "charge" {
				charge, err := parseCharge(unmatchedRaw["charge"], operationPath+".unmatched.charge", 0)
				if err != nil {
					return nil, err
				}
				if err := validatePricingCharge(charge, definition, operationKey); err != nil {
					return nil, err
				}
				unmatched.Charge = &charge
			}
			operationPrices[operationKey] = OperationPricing{Rules: rules, Unmatched: unmatched}
		}
		rateCards[cardKey] = RateCard{Extends: extends, Operations: operationPrices}
	}
	pricing := &PricingConfig{Operations: operations, RateCards: rateCards}
	if err := validateRateCardInheritance(pricing); err != nil {
		return nil, err
	}
	return pricing, nil
}

func validateRateCardInheritance(pricing *PricingConfig) error {
	visiting := map[string]struct{}{}
	visited := map[string]struct{}{}
	var visit func(string) error
	visit = func(key string) error {
		if _, exists := visiting[key]; exists {
			return newConfigError(fmt.Sprintf("pricing rate-card inheritance cycle includes '%s'", key), nil)
		}
		if _, exists := visited[key]; exists {
			return nil
		}
		card, exists := pricing.RateCards[key]
		if !exists {
			return newConfigError(fmt.Sprintf("unknown rate card '%s'", key), nil)
		}
		visiting[key] = struct{}{}
		if card.Extends != nil {
			if err := visit(*card.Extends); err != nil {
				return err
			}
		}
		delete(visiting, key)
		visited[key] = struct{}{}
		return nil
	}
	for key := range pricing.RateCards {
		if err := visit(key); err != nil {
			return err
		}
	}
	return nil
}

// ResolvesOperation reports whether a rate card or one of its parents prices
// an operation. It is useful when validating plan authorization rules.
func ResolvesOperation(pricing *PricingConfig, cardKey, operation string) bool {
	if pricing == nil {
		return false
	}
	seen := map[string]struct{}{}
	for key := cardKey; key != ""; {
		if _, exists := seen[key]; exists {
			return false
		}
		seen[key] = struct{}{}
		card, exists := pricing.RateCards[key]
		if !exists {
			return false
		}
		if _, exists := card.Operations[operation]; exists {
			return true
		}
		if card.Extends == nil {
			return false
		}
		key = *card.Extends
	}
	return false
}

func parsePlans(
	value any,
	pricing *PricingConfig,
	credits CreditsConfig,
	entitlements EntitlementsConfig,
	admission AdmissionConfig,
	commerce CommerceConfig,
) (map[string]PlanDefinition, error) {
	if value == nil {
		return map[string]PlanDefinition{}, nil
	}
	raw, err := configObject(value, "plans")
	if err != nil {
		return nil, err
	}
	if err := validateConfigIdentifiers(raw, "plans"); err != nil {
		return nil, err
	}
	subscriptionPlans := map[string]struct{}{}
	for _, offer := range commerce.Offers {
		if offer.Type == "subscription" && offer.Plan != nil {
			subscriptionPlans[*offer.Plan] = struct{}{}
		}
	}
	plans := make(map[string]PlanDefinition, len(raw))
	for _, key := range sortedConfigKeys(raw) {
		path := "plans." + key
		planRaw, err := configObject(raw[key], path)
		if err != nil {
			return nil, err
		}
		displayName, err := configString(planRaw["display_name"], path+".display_name")
		if err != nil {
			return nil, err
		}
		rank := 0
		if input, exists := planRaw["rank"]; exists && input != nil {
			rank, err = configInteger(input, path+".rank")
			if err != nil {
				return nil, err
			}
		}
		description, err := configOptionalString(planRaw, "description", path+".description")
		if err != nil {
			return nil, err
		}
		rateCard, err := configOptionalString(planRaw, "rate_card", path+".rate_card")
		if err != nil {
			return nil, err
		}
		if rateCard != nil {
			if pricing == nil {
				return nil, newConfigError(fmt.Sprintf("plans.%s.rate_card references unknown rate card '%s'", key, *rateCard), nil)
			}
			if _, exists := pricing.RateCards[*rateCard]; !exists {
				return nil, newConfigError(fmt.Sprintf("plans.%s.rate_card references unknown rate card '%s'", key, *rateCard), nil)
			}
		}
		allowedOperations := []string{}
		if input, exists := planRaw["allowed_operations"]; exists && input != nil {
			allowedOperations, err = configSliceStrings(input, path+".allowed_operations")
			if err != nil {
				return nil, err
			}
		}
		seenOperations := map[string]struct{}{}
		for _, operation := range allowedOperations {
			if _, exists := seenOperations[operation]; exists {
				return nil, newConfigError(path+".allowed_operations must not contain duplicates", nil)
			}
			seenOperations[operation] = struct{}{}
			if err := validateConfigIdentifier(operation, path+".allowed_operations"); err != nil {
				return nil, err
			}
			if pricing == nil {
				return nil, newConfigError(fmt.Sprintf("plans.%s references unknown operation '%s'", key, operation), nil)
			}
			if _, exists := pricing.Operations[operation]; !exists {
				return nil, newConfigError(fmt.Sprintf("plans.%s references unknown operation '%s'", key, operation), nil)
			}
			if rateCard == nil || !ResolvesOperation(pricing, *rateCard, operation) {
				return nil, newConfigError(fmt.Sprintf("plans.%s enables '%s' without pricing", key, operation), nil)
			}
		}
		featuresRaw, err := configMap(planRaw, "features", path+".features")
		if err != nil {
			return nil, err
		}
		if err := validateConfigIdentifiers(featuresRaw, path+".features"); err != nil {
			return nil, err
		}
		features := make(map[string]any, len(featuresRaw))
		for featureKey, featureValue := range featuresRaw {
			definition, exists := entitlements.Features[featureKey]
			if !exists {
				return nil, newConfigError(fmt.Sprintf("plans.%s references unknown feature '%s'", key, featureKey), nil)
			}
			parsed, err := validatePlanFeatureValue(featureValue, definition, path+".features."+featureKey)
			if err != nil {
				return nil, err
			}
			features[featureKey] = parsed
		}
		quotasRaw, err := configMap(planRaw, "quotas", path+".quotas")
		if err != nil {
			return nil, err
		}
		if err := validateConfigIdentifiers(quotasRaw, path+".quotas"); err != nil {
			return nil, err
		}
		quotas := make(map[string]QuotaDefinition, len(quotasRaw))
		for quotaKey, quotaInput := range quotasRaw {
			quota, err := parseQuota(quotaInput, path+".quotas."+quotaKey, pricing)
			if err != nil {
				return nil, err
			}
			quotas[quotaKey] = quota
		}
		creditPolicy, err := configOptionalString(planRaw, "credit_policy", path+".credit_policy")
		if err != nil {
			return nil, err
		}
		if creditPolicy != nil {
			if _, exists := credits.Policies[*creditPolicy]; !exists {
				return nil, newConfigError(path+".credit_policy references unknown policy", nil)
			}
		}
		admissionPolicy, err := configOptionalString(planRaw, "admission_policy", path+".admission_policy")
		if err != nil {
			return nil, err
		}
		if admissionPolicy != nil {
			if _, exists := admission.Policies[*admissionPolicy]; !exists {
				return nil, newConfigError(path+".admission_policy references unknown policy", nil)
			}
		}
		var allowance *CreditAllowance
		if input, exists := planRaw["credit_allowance"]; exists && input != nil {
			parsed, err := parseCreditAllowance(input, path+".credit_allowance")
			if err != nil {
				return nil, err
			}
			if _, priorityExists := mapBucketPriorities(credits.Buckets)[parsed.Priority]; priorityExists {
				return nil, newConfigError(fmt.Sprintf("plans.%s.credit_allowance.priority conflicts with credit bucket priority %d", key, parsed.Priority), nil)
			}
			if credits.DefaultBucket == nil {
				return nil, newConfigError(fmt.Sprintf("plans.%s.credit_allowance requires credits.default_bucket", key), nil)
			}
			allowance = &parsed
		}
		defaultRollout := "immediate"
		if _, subscriptionBacked := subscriptionPlans[key]; subscriptionBacked {
			defaultRollout = "next_renewal"
		}
		if input, exists := planRaw["evolution"]; exists && input != nil {
			evolution, err := configObject(input, path+".evolution")
			if err != nil {
				return nil, err
			}
			defaultRollout, err = configString(evolution["default_rollout"], path+".evolution.default_rollout")
			if err != nil {
				return nil, err
			}
		}
		if defaultRollout != "immediate" && defaultRollout != "next_renewal" && defaultRollout != "new_assignments_only" {
			return nil, newConfigError(path+".evolution.default_rollout is invalid", nil)
		}
		if defaultRollout == "next_renewal" {
			if _, subscriptionBacked := subscriptionPlans[key]; !subscriptionBacked {
				return nil, newConfigError(path+".evolution.default_rollout=next_renewal requires a subscription offer", nil)
			}
		}
		plans[key] = PlanDefinition{
			DisplayName:       displayName,
			Rank:              rank,
			Description:       description,
			RateCard:          rateCard,
			AllowedOperations: allowedOperations,
			Features:          features,
			CreditAllowance:   allowance,
			Quotas:            quotas,
			CreditPolicy:      creditPolicy,
			AdmissionPolicy:   admissionPolicy,
			Evolution:         PlanEvolution{DefaultRollout: defaultRollout},
		}
	}
	return plans, nil
}

func mapBucketPriorities(buckets map[string]BucketDefinition) map[int]struct{} {
	priorities := make(map[int]struct{}, len(buckets))
	for _, bucket := range buckets {
		priorities[bucket.Priority] = struct{}{}
	}
	return priorities
}

func validatePlanFeatureValue(value any, definition FeatureDefinition, path string) (any, error) {
	switch definition.Type {
	case "boolean":
		return configBool(value, path)
	case "integer":
		integer, err := configInteger(value, path)
		if err != nil {
			return nil, err
		}
		if definition.Minimum != nil && integer < *definition.Minimum {
			return nil, newConfigError(path+" is below the feature minimum", nil)
		}
		if definition.Maximum != nil && integer > *definition.Maximum {
			return nil, newConfigError(path+" exceeds the feature maximum", nil)
		}
		return integer, nil
	case "enum":
		stringValue, err := configString(value, path)
		if err != nil {
			return nil, err
		}
		for _, allowed := range definition.Values {
			if stringValue == allowed {
				return stringValue, nil
			}
		}
		return nil, newConfigError(path+" has an invalid enum value", nil)
	case "string":
		stringValue, err := configString(value, path)
		if err != nil {
			return nil, err
		}
		if definition.Pattern != nil {
			matcher, compileErr := regexp2.Compile(*definition.Pattern, regexp2.ECMAScript)
			matched := false
			if compileErr == nil {
				matched, compileErr = matcher.MatchString(stringValue)
			}
			if compileErr != nil || !matched {
				return nil, newConfigError(path+" does not match the feature pattern", compileErr)
			}
		}
		return stringValue, nil
	default:
		return nil, newConfigError(path+" has an invalid feature definition", nil)
	}
}

func parseQuota(value any, path string, pricing *PricingConfig) (QuotaDefinition, error) {
	raw, err := configObject(value, path)
	if err != nil {
		return QuotaDefinition{}, err
	}
	operation, err := configString(raw["operation"], path+".operation")
	if err != nil {
		return QuotaDefinition{}, err
	}
	measure, err := configString(raw["measure"], path+".measure")
	if err != nil {
		return QuotaDefinition{}, err
	}
	if pricing == nil || pricing.Operations[operation].Measures == nil || pricing.Operations[operation].Measures[measure].Unit == "" {
		return QuotaDefinition{}, newConfigError(path+" references an unknown operation measure", nil)
	}
	limit, err := configDecimal(raw["limit"], path+".limit")
	if err != nil {
		return QuotaDefinition{}, err
	}
	window, err := parseWindow(raw["window"], path+".window")
	if err != nil {
		return QuotaDefinition{}, err
	}
	enforcement, err := configString(raw["enforcement"], path+".enforcement")
	if err != nil {
		return QuotaDefinition{}, err
	}
	emitAtPercent := []int{100}
	if input, exists := raw["emit_at_percent"]; exists && input != nil {
		values, err := configArray(input, path+".emit_at_percent")
		if err != nil {
			return QuotaDefinition{}, err
		}
		emitAtPercent = make([]int, len(values))
		for index, threshold := range values {
			parsed, err := configInteger(threshold, fmt.Sprintf("%s.emit_at_percent[%d]", path, index))
			if err != nil {
				return QuotaDefinition{}, err
			}
			emitAtPercent[index] = parsed
		}
	}
	for index, threshold := range emitAtPercent {
		if threshold < 1 || threshold > 100 || (index > 0 && threshold <= emitAtPercent[index-1]) {
			return QuotaDefinition{}, newConfigError(path+".emit_at_percent must be unique, increasing, and between 1 and 100", nil)
		}
	}
	return QuotaDefinition{
		Operation: operation, Measure: measure, Limit: limit, Window: window,
		Enforcement: enforcement, EmitAtPercent: emitAtPercent,
	}, nil
}

func parseCreditAllowance(value any, path string) (CreditAllowance, error) {
	raw, err := configObject(value, path)
	if err != nil {
		return CreditAllowance{}, err
	}
	amount, err := configDecimal(raw["amount"], path+".amount")
	if err != nil {
		return CreditAllowance{}, err
	}
	priority, err := configInteger(raw["priority"], path+".priority")
	if err != nil {
		return CreditAllowance{}, err
	}
	if priority < 0 {
		return CreditAllowance{}, newConfigError(path+".priority must be non-negative", nil)
	}
	window, err := parseWindow(raw["window"], path+".window")
	if err != nil {
		return CreditAllowance{}, err
	}
	return CreditAllowance{Amount: amount, Priority: priority, Window: window}, nil
}

func parseCommerce(value any, credits CreditsConfig) (CommerceConfig, error) {
	if value == nil {
		return CommerceConfig{Providers: map[string]ProviderDefinition{}, Offers: map[string]CommerceOffer{}, SubscriptionChanges: map[string]SubscriptionChangePolicy{}}, nil
	}
	raw, err := configObject(value, "commerce")
	if err != nil {
		return CommerceConfig{}, err
	}
	providersRaw, err := configMap(raw, "providers", "commerce.providers")
	if err != nil {
		return CommerceConfig{}, err
	}
	offersRaw, err := configMap(raw, "offers", "commerce.offers")
	if err != nil {
		return CommerceConfig{}, err
	}
	if err := validateConfigIdentifiers(providersRaw, "commerce.providers"); err != nil {
		return CommerceConfig{}, err
	}
	if err := validateConfigIdentifiers(offersRaw, "commerce.offers"); err != nil {
		return CommerceConfig{}, err
	}
	providers := make(map[string]ProviderDefinition, len(providersRaw))
	for _, key := range sortedConfigKeys(providersRaw) {
		path := "commerce.providers." + key
		providerRaw, err := configObject(providersRaw[key], path)
		if err != nil {
			return CommerceConfig{}, err
		}
		typeValue, err := configString(providerRaw["type"], path+".type")
		if err != nil {
			return CommerceConfig{}, err
		}
		provider := ProviderDefinition{Type: typeValue}
		if typeValue == "custom" {
			adapter, err := configString(providerRaw["adapter"], path+".adapter")
			if err != nil {
				return CommerceConfig{}, err
			}
			provider.Adapter = &adapter
		}
		providers[key] = provider
	}
	offers := make(map[string]CommerceOffer, len(offersRaw))
	seenProviderObjects := map[string]struct{}{}
	for _, key := range sortedConfigKeys(offersRaw) {
		offer, err := parseCommerceOffer(key, offersRaw[key], providers, credits, seenProviderObjects)
		if err != nil {
			return CommerceConfig{}, err
		}
		offers[key] = offer
	}
	subscriptionChanges, err := parseSubscriptionChanges(raw["subscription_changes"])
	if err != nil {
		return CommerceConfig{}, err
	}
	var autoRecharge *AutoRechargeGuardrails
	if input, exists := raw["auto_recharge"]; exists && input != nil {
		parsed, err := parseAutoRecharge(input, offers)
		if err != nil {
			return CommerceConfig{}, err
		}
		autoRecharge = &parsed
	}
	return CommerceConfig{
		Providers: providers, Offers: offers, SubscriptionChanges: subscriptionChanges, AutoRecharge: autoRecharge,
	}, nil
}

func parseProviderReference(value any, path string) (ProviderReference, error) {
	raw, err := configObject(value, path)
	if err != nil {
		return ProviderReference{}, err
	}
	typeValue, err := configString(raw["type"], path+".type")
	if err != nil {
		return ProviderReference{}, err
	}
	reference := ProviderReference{Type: typeValue}
	switch typeValue {
	case "stripe_price":
		priceID, err := configString(raw["price_id"], path+".price_id")
		if err != nil {
			return ProviderReference{}, err
		}
		reference.PriceID = &priceID
	case "dodo_product":
		productID, err := configString(raw["product_id"], path+".product_id")
		if err != nil {
			return ProviderReference{}, err
		}
		reference.ProductID = &productID
	case "custom_object":
		objectKind, err := configString(raw["object_kind"], path+".object_kind")
		if err != nil {
			return ProviderReference{}, err
		}
		externalID, err := configString(raw["external_id"], path+".external_id")
		if err != nil {
			return ProviderReference{}, err
		}
		reference.ObjectKind, reference.ExternalID = &objectKind, &externalID
	default:
		return ProviderReference{}, newConfigError(path+".type is invalid", nil)
	}
	return reference, nil
}

func providerReferenceExternalID(reference ProviderReference) string {
	if reference.PriceID != nil {
		return *reference.PriceID
	}
	if reference.ProductID != nil {
		return *reference.ProductID
	}
	if reference.ExternalID != nil {
		return *reference.ExternalID
	}
	return ""
}

func providerReferenceCompatible(provider ProviderDefinition, reference ProviderReference) bool {
	return (provider.Type == "stripe" && reference.Type == "stripe_price") ||
		(provider.Type == "dodo" && reference.Type == "dodo_product") ||
		(provider.Type == "custom" && reference.Type == "custom_object")
}

func parseCommerceOffer(
	key string,
	value any,
	providers map[string]ProviderDefinition,
	credits CreditsConfig,
	seenProviderObjects map[string]struct{},
) (CommerceOffer, error) {
	path := "commerce.offers." + key
	raw, err := configObject(value, path)
	if err != nil {
		return CommerceOffer{}, err
	}
	typeValue, err := configString(raw["type"], path+".type")
	if err != nil {
		return CommerceOffer{}, err
	}
	displayName, err := configString(raw["display_name"], path+".display_name")
	if err != nil {
		return CommerceOffer{}, err
	}
	description, err := configOptionalString(raw, "description", path+".description")
	if err != nil {
		return CommerceOffer{}, err
	}
	sortOrder := 0
	if input, exists := raw["sort_order"]; exists && input != nil {
		sortOrder, err = configInteger(input, path+".sort_order")
		if err != nil {
			return CommerceOffer{}, err
		}
	}
	var availability *Availability
	if input, exists := raw["availability"]; exists && input != nil {
		parsed, err := parseAvailability(input, path+".availability")
		if err != nil {
			return CommerceOffer{}, err
		}
		availability = &parsed
	}
	priceRaw, err := configObject(raw["price"], path+".price")
	if err != nil {
		return CommerceOffer{}, err
	}
	amountMinor, err := configInt64(priceRaw["amount_minor"], path+".price.amount_minor")
	if err != nil {
		return CommerceOffer{}, err
	}
	currency, err := configString(priceRaw["currency"], path+".price.currency")
	if err != nil {
		return CommerceOffer{}, err
	}
	if !regexp.MustCompile(`^[A-Z]{3}$`).MatchString(currency) {
		return CommerceOffer{}, newConfigError(path+".price.currency must be an uppercase ISO-4217 currency code", nil)
	}
	taxBehavior := "unspecified"
	if input, exists := priceRaw["tax_behavior"]; exists && input != nil {
		taxBehavior, err = configString(input, path+".price.tax_behavior")
		if err != nil {
			return CommerceOffer{}, err
		}
	}
	referencesRaw, err := configObject(raw["providers"], path+".providers")
	if err != nil {
		return CommerceOffer{}, err
	}
	references := make(map[string]ProviderReference, len(referencesRaw))
	for providerKey, input := range referencesRaw {
		provider, exists := providers[providerKey]
		if !exists {
			return CommerceOffer{}, newConfigError(path+" references unknown provider", nil)
		}
		reference, err := parseProviderReference(input, path+".providers."+providerKey)
		if err != nil {
			return CommerceOffer{}, err
		}
		if !providerReferenceCompatible(provider, reference) {
			return CommerceOffer{}, newConfigError(path+" has incompatible provider reference", nil)
		}
		uniqueKey := providerKey + "/" + providerReferenceExternalID(reference)
		if _, exists := seenProviderObjects[uniqueKey]; exists {
			return CommerceOffer{}, newConfigError("duplicate provider object reference "+uniqueKey, nil)
		}
		seenProviderObjects[uniqueKey] = struct{}{}
		references[providerKey] = reference
	}
	offer := CommerceOffer{
		Type: typeValue, DisplayName: displayName, Description: description, SortOrder: sortOrder,
		Availability: availability, Price: OfferPrice{AmountMinor: amountMinor, Currency: currency, TaxBehavior: taxBehavior}, Providers: references,
	}
	switch typeValue {
	case "subscription":
		plan, err := configString(raw["plan"], path+".plan")
		if err != nil {
			return CommerceOffer{}, err
		}
		billingInterval, err := parseBillingInterval(raw["billing_interval"], path+".billing_interval")
		if err != nil {
			return CommerceOffer{}, err
		}
		offer.Plan, offer.BillingInterval = &plan, &billingInterval
		if input, exists := raw["trial"]; exists && input != nil {
			trial, err := parseBillingInterval(input, path+".trial")
			if err != nil {
				return CommerceOffer{}, err
			}
			offer.Trial = &trial
		}
		if input, exists := raw["cycle_grant"]; exists && input != nil {
			grant, err := parseCycleGrant(input, path+".cycle_grant", credits)
			if err != nil {
				return CommerceOffer{}, err
			}
			offer.CycleGrant = &grant
		}
	case "topup":
		creditsPerUnit, err := configDecimal(raw["credits_per_unit"], path+".credits_per_unit")
		if err != nil {
			return CommerceOffer{}, err
		}
		bucket, err := configString(raw["bucket"], path+".bucket")
		if err != nil {
			return CommerceOffer{}, err
		}
		if _, exists := credits.Buckets[bucket]; !exists {
			return CommerceOffer{}, newConfigError(path+".bucket references unknown bucket", nil)
		}
		quantity, err := parseOfferQuantity(raw["quantity"], path+".quantity")
		if err != nil {
			return CommerceOffer{}, err
		}
		offer.CreditsPerUnit, offer.Bucket, offer.Quantity = &creditsPerUnit, &bucket, &quantity
		lotBehavior := "separate_lots"
		if input, exists := raw["lot_behavior"]; exists && input != nil {
			lotBehavior, err = configString(input, path+".lot_behavior")
			if err != nil {
				return CommerceOffer{}, err
			}
		}
		offer.LotBehavior = lotBehavior
		if input, exists := raw["expiry"]; exists && input != nil {
			expiry, err := parseExpiry(input, path+".expiry")
			if err != nil {
				return CommerceOffer{}, err
			}
			if expiry.Type == "subscription_end" {
				return CommerceOffer{}, newConfigError(path+" top-up cannot use subscription_end expiry", nil)
			}
			offer.Expiry = &expiry
		}
	default:
		return CommerceOffer{}, newConfigError(path+".type is invalid", nil)
	}
	return offer, nil
}

func parseOfferQuantity(value any, path string) (OfferQuantity, error) {
	if value == nil {
		return OfferQuantity{Minimum: 1, Maximum: 1, Default: 1}, nil
	}
	raw, err := configObject(value, path)
	if err != nil {
		return OfferQuantity{}, err
	}
	minimum, maximum, defaultValue := 1, 1, 1
	if input, exists := raw["minimum"]; exists && input != nil {
		minimum, err = configInteger(input, path+".minimum")
		if err != nil {
			return OfferQuantity{}, err
		}
	}
	if input, exists := raw["maximum"]; exists && input != nil {
		maximum, err = configInteger(input, path+".maximum")
		if err != nil {
			return OfferQuantity{}, err
		}
	}
	if input, exists := raw["default"]; exists && input != nil {
		defaultValue, err = configInteger(input, path+".default")
		if err != nil {
			return OfferQuantity{}, err
		}
	}
	if minimum > maximum || defaultValue < minimum || defaultValue > maximum {
		return OfferQuantity{}, newConfigError(path+" requires minimum <= default <= maximum", nil)
	}
	return OfferQuantity{Minimum: minimum, Maximum: maximum, Default: defaultValue}, nil
}

func parseCycleGrant(value any, path string, credits CreditsConfig) (CycleGrant, error) {
	raw, err := configObject(value, path)
	if err != nil {
		return CycleGrant{}, err
	}
	amount, err := configDecimal(raw["amount"], path+".amount")
	if err != nil {
		return CycleGrant{}, err
	}
	bucket, err := configString(raw["bucket"], path+".bucket")
	if err != nil {
		return CycleGrant{}, err
	}
	if _, exists := credits.Buckets[bucket]; !exists {
		return CycleGrant{}, newConfigError(path+" references unknown bucket", nil)
	}
	renewal, err := configString(raw["renewal"], path+".renewal")
	if err != nil {
		return CycleGrant{}, err
	}
	expiryInput := any(map[string]any{"type": "subscription_end"})
	if input, exists := raw["expiry"]; exists && input != nil {
		expiryInput = input
	}
	expiry, err := parseExpiry(expiryInput, path+".expiry")
	if err != nil {
		return CycleGrant{}, err
	}
	return CycleGrant{Amount: amount, Bucket: bucket, Renewal: renewal, Expiry: expiry}, nil
}

func parseSubscriptionChanges(value any) (map[string]SubscriptionChangePolicy, error) {
	result := map[string]SubscriptionChangePolicy{}
	if value == nil {
		return result, nil
	}
	raw, err := configObject(value, "commerce.subscription_changes")
	if err != nil {
		return nil, err
	}
	for _, classification := range []string{"upgrade", "downgrade", "lateral", "cadence_change"} {
		input, exists := raw[classification]
		if !exists || input == nil {
			continue
		}
		path := "commerce.subscription_changes." + classification
		policyRaw, err := configObject(input, path)
		if err != nil {
			return nil, err
		}
		effective, err := configString(policyRaw["effective"], path+".effective")
		if err != nil {
			return nil, err
		}
		proration, err := configString(policyRaw["proration"], path+".proration")
		if err != nil {
			return nil, err
		}
		paymentFailure := "prevent_change"
		if candidate, exists := policyRaw["payment_failure"]; exists && candidate != nil {
			paymentFailure, err = configString(candidate, path+".payment_failure")
			if err != nil {
				return nil, err
			}
		}
		result[classification] = SubscriptionChangePolicy{Effective: effective, Proration: proration, PaymentFailure: paymentFailure}
	}
	return result, nil
}

func parseAutoRecharge(value any, offers map[string]CommerceOffer) (AutoRechargeGuardrails, error) {
	raw, err := configObject(value, "commerce.auto_recharge")
	if err != nil {
		return AutoRechargeGuardrails{}, err
	}
	eligibleTopups, err := configSliceStrings(raw["eligible_topups"], "commerce.auto_recharge.eligible_topups")
	if err != nil {
		return AutoRechargeGuardrails{}, err
	}
	var currency string
	for _, key := range eligibleTopups {
		offer, exists := offers[key]
		if !exists || offer.Type != "topup" {
			return AutoRechargeGuardrails{}, newConfigError(fmt.Sprintf("commerce.auto_recharge references non-top-up offer '%s'", key), nil)
		}
		if currency == "" {
			currency = offer.Price.Currency
		} else if currency != offer.Price.Currency {
			return AutoRechargeGuardrails{}, newConfigError("commerce.auto_recharge eligible top-ups must use one currency", nil)
		}
	}
	thresholdRaw, err := configObject(raw["balance_below"], "commerce.auto_recharge.balance_below")
	if err != nil {
		return AutoRechargeGuardrails{}, err
	}
	minimum, err := configDecimal(thresholdRaw["minimum"], "commerce.auto_recharge.balance_below.minimum")
	if err != nil {
		return AutoRechargeGuardrails{}, err
	}
	maximum, err := configDecimal(thresholdRaw["maximum"], "commerce.auto_recharge.balance_below.maximum")
	if err != nil {
		return AutoRechargeGuardrails{}, err
	}
	defaultValue, err := configDecimal(thresholdRaw["default"], "commerce.auto_recharge.balance_below.default")
	if err != nil {
		return AutoRechargeGuardrails{}, err
	}
	if minimum.GreaterThan(maximum) || defaultValue.LessThan(minimum) || defaultValue.GreaterThan(maximum) {
		return AutoRechargeGuardrails{}, newConfigError("commerce.auto_recharge.balance_below requires minimum <= default <= maximum", nil)
	}
	rearmAbove, err := configDecimal(raw["rearm_above"], "commerce.auto_recharge.rearm_above")
	if err != nil {
		return AutoRechargeGuardrails{}, err
	}
	if !rearmAbove.GreaterThan(maximum) {
		return AutoRechargeGuardrails{}, newConfigError("commerce.auto_recharge.rearm_above must exceed balance_below.maximum", nil)
	}
	quantity, err := parseOfferQuantity(raw["quantity"], "commerce.auto_recharge.quantity")
	if err != nil {
		return AutoRechargeGuardrails{}, err
	}
	for _, key := range eligibleTopups {
		offer := offers[key]
		if offer.Quantity == nil || quantity.Minimum < offer.Quantity.Minimum || quantity.Maximum > offer.Quantity.Maximum {
			return AutoRechargeGuardrails{}, newConfigError(fmt.Sprintf("commerce.auto_recharge.quantity must fit commerce.offers.%s.quantity", key), nil)
		}
	}
	limitsRaw, err := configObject(raw["limits"], "commerce.auto_recharge.limits")
	if err != nil {
		return AutoRechargeGuardrails{}, err
	}
	maxPurchases, err := configInteger(limitsRaw["max_purchases"], "commerce.auto_recharge.limits.max_purchases")
	if err != nil {
		return AutoRechargeGuardrails{}, err
	}
	window, err := parseWindow(limitsRaw["window"], "commerce.auto_recharge.limits.window")
	if err != nil {
		return AutoRechargeGuardrails{}, err
	}
	if window.Type == "plan_assignment" {
		return AutoRechargeGuardrails{}, newConfigError("commerce.auto_recharge.limits.window must be calendar or rolling", nil)
	}
	maxChargeMinor, err := configInt64(limitsRaw["max_charge_minor"], "commerce.auto_recharge.limits.max_charge_minor")
	if err != nil {
		return AutoRechargeGuardrails{}, err
	}
	cooldown, err := parseDuration(limitsRaw["cooldown"], "commerce.auto_recharge.limits.cooldown")
	if err != nil {
		return AutoRechargeGuardrails{}, err
	}
	maxConsecutiveFailures := 3
	if input, exists := limitsRaw["max_consecutive_failures"]; exists && input != nil {
		maxConsecutiveFailures, err = configInteger(input, "commerce.auto_recharge.limits.max_consecutive_failures")
		if err != nil {
			return AutoRechargeGuardrails{}, err
		}
	}
	return AutoRechargeGuardrails{
		EligibleTopups: eligibleTopups,
		BalanceBelow:   DecimalRange{Minimum: minimum, Maximum: maximum, Default: defaultValue},
		RearmAbove:     rearmAbove,
		Quantity:       quantity,
		Limits: AutoRechargeLimits{
			MaxPurchases: maxPurchases, Window: window, MaxChargeMinor: maxChargeMinor, Cooldown: cooldown,
			MaxConsecutiveFailure: maxConsecutiveFailures, FailureAction: "pause",
		},
	}, nil
}

// LoadCatalogRollout parses the small one-release manifest used during
// catalog activation. It accepts either snake_case or Go-style includePinned
// for interoperability with programmatic callers.
func LoadCatalogRollout(value map[string]any) (CatalogRollout, error) {
	if value == nil {
		return CatalogRollout{Plans: map[string]PlanRollout{}}, nil
	}
	for key := range value {
		if key != "plans" {
			return CatalogRollout{}, newConfigError("rollout contains unknown field '"+key+"'", nil)
		}
	}
	plansRaw, err := configMap(value, "plans", "rollout.plans")
	if err != nil {
		return CatalogRollout{}, err
	}
	plans := make(map[string]PlanRollout, len(plansRaw))
	for key, input := range plansRaw {
		if err := validateConfigIdentifier(key, "rollout.plans."+key); err != nil {
			return CatalogRollout{}, err
		}
		path := "rollout.plans." + key
		raw, err := configObject(input, path)
		if err != nil {
			return CatalogRollout{}, err
		}
		for field := range raw {
			if field != "effective" && field != "include_pinned" && field != "includePinned" {
				return CatalogRollout{}, newConfigError(path+" contains unknown field '"+field+"'", nil)
			}
		}
		effective, err := configString(raw["effective"], path+".effective")
		if err != nil {
			return CatalogRollout{}, err
		}
		if effective != "immediate" && effective != "next_renewal" && effective != "new_assignments_only" {
			return CatalogRollout{}, newConfigError(path+".effective must be immediate, next_renewal, or new_assignments_only", nil)
		}
		_, hasSnake := raw["include_pinned"]
		_, hasCamel := raw["includePinned"]
		if hasSnake && hasCamel {
			return CatalogRollout{}, newConfigError(path+" must not set both include_pinned and includePinned", nil)
		}
		includePinned := false
		if input, exists := raw["include_pinned"]; exists && input != nil {
			includePinned, err = configBool(input, path+".include_pinned")
			if err != nil {
				return CatalogRollout{}, err
			}
		} else if input, exists := raw["includePinned"]; exists && input != nil {
			includePinned, err = configBool(input, path+".includePinned")
			if err != nil {
				return CatalogRollout{}, err
			}
		}
		plans[key] = PlanRollout{Effective: effective, IncludePinned: includePinned}
	}
	return CatalogRollout{Plans: plans}, nil
}

// ValidateCatalogRollout checks a rollout against the parsed target catalog.
func ValidateCatalogRollout(config *BursarConfig, rollout CatalogRollout) (CatalogRollout, error) {
	if config == nil {
		return CatalogRollout{}, newConfigError("catalog configuration is required", nil)
	}
	subscriptionPlans := map[string]struct{}{}
	for _, offer := range config.Commerce.Offers {
		if offer.Type == "subscription" && offer.Plan != nil {
			subscriptionPlans[*offer.Plan] = struct{}{}
		}
	}
	for key, policy := range rollout.Plans {
		if _, exists := config.Plans[key]; !exists {
			return CatalogRollout{}, newConfigError("rollout.plans references unknown plan '"+key+"'", nil)
		}
		if policy.Effective == "next_renewal" {
			if _, exists := subscriptionPlans[key]; !exists {
				return CatalogRollout{}, newConfigError("rollout.plans."+key+".effective=next_renewal requires a subscription offer", nil)
			}
		}
	}
	return rollout, nil
}

// CanonicalBursarConfigDict parses a raw catalog and returns its canonical
// snake_case representation. Decimal values are rendered at Bursar's stable
// six-place accounting precision.
func CanonicalBursarConfigDict(data map[string]any) (map[string]any, error) {
	config, err := LoadConfigFromMap(data)
	if err != nil {
		return nil, err
	}
	return CanonicalParsedBursarConfigDict(config), nil
}

// CanonicalConfig is a shorter alias for CanonicalBursarConfigDict.
func CanonicalConfig(data map[string]any) (map[string]any, error) {
	return CanonicalBursarConfigDict(data)
}

// CanonicalParsedBursarConfigDict serializes a validated parsed catalog
// without reinterpreting it as raw input. The returned map is detached.
func CanonicalParsedBursarConfigDict(config *BursarConfig) map[string]any {
	if config == nil {
		return nil
	}
	result := map[string]any{
		"version": config.Version,
		"credits": canonicalCredits(config.Credits),
	}
	if config.Catalog.DefaultPlan != nil {
		result["catalog"] = map[string]any{"default_plan": *config.Catalog.DefaultPlan}
	} else {
		result["catalog"] = map[string]any{}
	}
	if config.Pricing != nil {
		result["pricing"] = canonicalPricing(*config.Pricing)
	}
	result["entitlements"] = canonicalEntitlements(config.Entitlements)
	result["admission"] = canonicalAdmission(config.Admission)
	result["plans"] = canonicalPlans(config.Plans)
	result["commerce"] = canonicalCommerce(config.Commerce)
	return canonicalJSONMap(result)
}

// canonicalJSONMap converts Go-specific typed slices and maps into the JSON
// value shapes accepted by the shared JSON Schema validator without routing
// financial integers through float64.
func canonicalJSONMap(value map[string]any) map[string]any {
	converted, _ := canonicalJSONValue(value).(map[string]any)
	return converted
}

func canonicalJSONValue(value any) any {
	switch typed := value.(type) {
	case map[string]any:
		result := make(map[string]any, len(typed))
		for key, child := range typed {
			result[key] = canonicalJSONValue(child)
		}
		return result
	case []any:
		result := make([]any, len(typed))
		for index, child := range typed {
			result[index] = canonicalJSONValue(child)
		}
		return result
	case decimal.Decimal, json.Number, string, bool, nil,
		int, int8, int16, int32, int64, uint, uint8, uint16, uint32, uint64,
		float32, float64:
		return value
	}
	raw := reflect.ValueOf(value)
	if !raw.IsValid() {
		return nil
	}
	if raw.Kind() == reflect.Slice || raw.Kind() == reflect.Array {
		result := make([]any, raw.Len())
		for index := 0; index < raw.Len(); index++ {
			result[index] = canonicalJSONValue(raw.Index(index).Interface())
		}
		return result
	}
	if raw.Kind() == reflect.Map && raw.Type().Key().Kind() == reflect.String {
		result := make(map[string]any, raw.Len())
		iterator := raw.MapRange()
		for iterator.Next() {
			result[iterator.Key().String()] = canonicalJSONValue(iterator.Value().Interface())
		}
		return result
	}
	return value
}

func amountConfigString(value Amount) string {
	return QuantizeMoney(value).StringFixed(MoneyDecimalPlaces)
}

func canonicalDuration(value Duration) map[string]any {
	return map[string]any{"unit": value.Unit, "count": value.Count}
}

func canonicalInterval(value BillingInterval) map[string]any {
	return map[string]any{"unit": value.Unit, "count": value.Count}
}

func canonicalWindow(value Window) map[string]any {
	switch value.Type {
	case "rolling":
		result := map[string]any{"type": "rolling"}
		if value.Duration != nil {
			result["duration"] = canonicalDuration(*value.Duration)
		}
		return result
	case "plan_assignment":
		result := map[string]any{"type": "plan_assignment", "timezone": value.Timezone}
		if value.Interval != nil {
			result["interval"] = canonicalInterval(*value.Interval)
		}
		return result
	default:
		return map[string]any{"type": "calendar", "unit": value.Unit, "count": value.Count, "timezone": value.Timezone}
	}
}

func canonicalExpiry(value ExpiryPolicy) map[string]any {
	result := map[string]any{"type": value.Type}
	switch value.Type {
	case "after_grant":
		if value.Interval != nil {
			result["interval"] = canonicalInterval(*value.Interval)
		}
		result["timezone"] = value.Timezone
	case "end_of_window":
		if value.Window != nil {
			result["window"] = canonicalWindow(*value.Window)
		}
	case "fixed_at":
		if value.At != nil {
			result["at"] = *value.At
		}
	}
	return result
}

func canonicalAvailability(value Availability) map[string]any {
	result := map[string]any{"regions": append([]string(nil), value.Regions...)}
	if value.StartsAt != nil {
		result["starts_at"] = *value.StartsAt
	}
	if value.EndsAt != nil {
		result["ends_at"] = *value.EndsAt
	}
	return result
}

func canonicalCredits(value CreditsConfig) map[string]any {
	buckets := make(map[string]any, len(value.Buckets))
	for key, bucket := range value.Buckets {
		buckets[key] = map[string]any{"priority": bucket.Priority, "expiry": canonicalExpiry(bucket.Expiry)}
	}
	policies := make(map[string]any, len(value.Policies))
	for key, policy := range value.Policies {
		entry := map[string]any{"type": policy.Type}
		if policy.Limit != nil {
			entry["limit"] = amountConfigString(*policy.Limit)
		}
		policies[key] = entry
	}
	programs := make(map[string]any, len(value.GrantPrograms))
	for key, program := range value.GrantPrograms {
		awards := make([]any, 0, len(program.Awards))
		for _, award := range program.Awards {
			entry := map[string]any{"recipient": award.Recipient, "amount": amountConfigString(award.Amount), "bucket": award.Bucket}
			if award.Expiry != nil {
				entry["expiry"] = canonicalExpiry(*award.Expiry)
			}
			awards = append(awards, entry)
		}
		entry := map[string]any{
			"trigger": program.Trigger, "awards": awards,
			"eligibility":            map[string]any{"plans": append([]string(nil), program.EligibilityPlans...), "regions": append([]string(nil), program.EligibilityRegions...)},
			"max_awards_per_subject": program.MaxAwardsPerSubject, "idempotency_scope": program.IdempotencyScope,
		}
		if program.Availability != nil {
			entry["availability"] = canonicalAvailability(*program.Availability)
		}
		programs[key] = entry
	}
	result := map[string]any{"buckets": buckets, "policies": policies, "grant_programs": programs}
	if value.DefaultBucket != nil {
		result["default_bucket"] = *value.DefaultBucket
	}
	if value.Display != nil {
		result["display"] = map[string]any{"currency": value.Display.Currency, "units_per_major": amountConfigString(value.Display.UnitsPerMajor)}
	}
	return result
}

func canonicalEntitlements(value EntitlementsConfig) map[string]any {
	features := make(map[string]any, len(value.Features))
	for key, definition := range value.Features {
		entry := map[string]any{"type": definition.Type, "default": definition.Default}
		if len(definition.Values) > 0 {
			entry["values"] = append([]string(nil), definition.Values...)
		}
		if definition.Minimum != nil {
			entry["minimum"] = *definition.Minimum
		}
		if definition.Maximum != nil {
			entry["maximum"] = *definition.Maximum
		}
		if definition.Pattern != nil {
			entry["pattern"] = *definition.Pattern
		}
		features[key] = entry
	}
	return map[string]any{"features": features}
}

func canonicalAdmission(value AdmissionConfig) map[string]any {
	policies := make(map[string]any, len(value.Policies))
	for key, policy := range value.Policies {
		operations := make(map[string]any, len(policy.Operations))
		for operation, definition := range policy.Operations {
			operations[operation] = map[string]any{"max_in_flight": definition.MaxInFlight}
		}
		entry := map[string]any{"operations": operations}
		if policy.MaxInFlight != nil {
			entry["max_in_flight"] = *policy.MaxInFlight
		}
		policies[key] = entry
	}
	return map[string]any{"policies": policies}
}

func canonicalMatcher(value DimensionMatcher) map[string]any {
	result := map[string]any{"op": value.Op}
	switch value.Op {
	case "eq", "prefix":
		result["value"] = canonicalMatcherScalar(value.Value)
	case "in", "not_in":
		items := make([]any, len(value.Values))
		for index, item := range value.Values {
			items[index] = canonicalMatcherScalar(item)
		}
		result["values"] = items
	case "range":
		if value.GT != nil {
			result["gt"] = amountConfigString(*value.GT)
		}
		if value.GTE != nil {
			result["gte"] = amountConfigString(*value.GTE)
		}
		if value.LT != nil {
			result["lt"] = amountConfigString(*value.LT)
		}
		if value.LTE != nil {
			result["lte"] = amountConfigString(*value.LTE)
		}
	}
	return result
}

func canonicalMatcherScalar(value MatcherScalar) any {
	if amount, ok := value.(decimal.Decimal); ok {
		return amountConfigString(amount)
	}
	return value
}

func canonicalCharge(value Charge) map[string]any {
	result := map[string]any{"type": value.Type}
	switch value.Type {
	case "flat":
		result["amount"] = amountConfigString(value.Amount)
	case "per_unit":
		result["measure"] = value.Measure
		result["rate"] = amountConfigString(value.Rate)
		result["unit_size"] = amountConfigString(value.UnitSize)
	case "package":
		result["measure"] = value.Measure
		result["units"] = amountConfigString(value.Units)
		result["amount"] = amountConfigString(value.Amount)
		result["rounding"] = value.Rounding
	case "graduated", "volume":
		result["measure"] = value.Measure
		tiers := make([]any, 0, len(value.Tiers))
		for _, tier := range value.Tiers {
			entry := map[string]any{"rate": amountConfigString(tier.Rate)}
			if tier.UpTo != nil {
				entry["up_to"] = amountConfigString(*tier.UpTo)
			}
			tiers = append(tiers, entry)
		}
		result["tiers"] = tiers
	case "expression":
		result["formula"] = value.Formula
	case "sum":
		components := make([]any, 0, len(value.Components))
		for _, component := range value.Components {
			components = append(components, canonicalCharge(component))
		}
		result["components"] = components
	}
	return result
}

func canonicalPricing(value PricingConfig) map[string]any {
	operations := make(map[string]any, len(value.Operations))
	for key, operation := range value.Operations {
		measures := make(map[string]any, len(operation.Measures))
		for name, definition := range operation.Measures {
			measures[name] = map[string]any{"unit": definition.Unit}
		}
		dimensions := make(map[string]any, len(operation.Dimensions))
		for name, definition := range operation.Dimensions {
			dimensions[name] = map[string]any{"type": definition.Type, "required": definition.Required}
		}
		operations[key] = map[string]any{"measures": measures, "dimensions": dimensions}
	}
	cards := make(map[string]any, len(value.RateCards))
	for key, card := range value.RateCards {
		operationPrices := make(map[string]any, len(card.Operations))
		for operation, price := range card.Operations {
			rules := make([]any, 0, len(price.Rules))
			for _, rule := range price.Rules {
				when := make(map[string]any, len(rule.When))
				for name, matcher := range rule.When {
					when[name] = canonicalMatcher(matcher)
				}
				rules = append(rules, map[string]any{"when": when, "charge": canonicalCharge(rule.Charge)})
			}
			unmatched := map[string]any{"action": price.Unmatched.Action}
			if price.Unmatched.Charge != nil {
				unmatched["charge"] = canonicalCharge(*price.Unmatched.Charge)
			}
			operationPrices[operation] = map[string]any{"rules": rules, "unmatched": unmatched}
		}
		entry := map[string]any{"operations": operationPrices}
		if card.Extends != nil {
			entry["extends"] = *card.Extends
		}
		cards[key] = entry
	}
	return map[string]any{"operations": operations, "rate_cards": cards}
}

func canonicalPlans(value map[string]PlanDefinition) map[string]any {
	plans := make(map[string]any, len(value))
	for key, plan := range value {
		features := cloneAnyMap(plan.Features)
		quotas := make(map[string]any, len(plan.Quotas))
		for quotaKey, quota := range plan.Quotas {
			quotas[quotaKey] = map[string]any{
				"operation": quota.Operation, "measure": quota.Measure, "limit": amountConfigString(quota.Limit),
				"window": canonicalWindow(quota.Window), "enforcement": quota.Enforcement,
				"emit_at_percent": append([]int(nil), quota.EmitAtPercent...),
			}
		}
		entry := map[string]any{
			"display_name": plan.DisplayName, "rank": plan.Rank,
			"allowed_operations": append([]string(nil), plan.AllowedOperations...),
			"features":           features, "quotas": quotas,
			"evolution": map[string]any{"default_rollout": plan.Evolution.DefaultRollout},
		}
		if plan.Description != nil {
			entry["description"] = *plan.Description
		}
		if plan.RateCard != nil {
			entry["rate_card"] = *plan.RateCard
		}
		if plan.CreditAllowance != nil {
			entry["credit_allowance"] = map[string]any{
				"amount": amountConfigString(plan.CreditAllowance.Amount), "priority": plan.CreditAllowance.Priority,
				"window": canonicalWindow(plan.CreditAllowance.Window),
			}
		}
		if plan.CreditPolicy != nil {
			entry["credit_policy"] = *plan.CreditPolicy
		}
		if plan.AdmissionPolicy != nil {
			entry["admission_policy"] = *plan.AdmissionPolicy
		}
		plans[key] = entry
	}
	return plans
}

func canonicalProviderReference(value ProviderReference) map[string]any {
	result := map[string]any{"type": value.Type}
	if value.PriceID != nil {
		result["price_id"] = *value.PriceID
	}
	if value.ProductID != nil {
		result["product_id"] = *value.ProductID
	}
	if value.ObjectKind != nil {
		result["object_kind"] = *value.ObjectKind
	}
	if value.ExternalID != nil {
		result["external_id"] = *value.ExternalID
	}
	return result
}

func canonicalCommerce(value CommerceConfig) map[string]any {
	providers := make(map[string]any, len(value.Providers))
	for key, provider := range value.Providers {
		entry := map[string]any{"type": provider.Type}
		if provider.Adapter != nil {
			entry["adapter"] = *provider.Adapter
		}
		providers[key] = entry
	}
	offers := make(map[string]any, len(value.Offers))
	for key, offer := range value.Offers {
		references := make(map[string]any, len(offer.Providers))
		for providerKey, reference := range offer.Providers {
			references[providerKey] = canonicalProviderReference(reference)
		}
		entry := map[string]any{
			"type": offer.Type, "display_name": offer.DisplayName, "sort_order": offer.SortOrder,
			"price":     map[string]any{"amount_minor": offer.Price.AmountMinor, "currency": offer.Price.Currency, "tax_behavior": offer.Price.TaxBehavior},
			"providers": references,
		}
		if offer.Description != nil {
			entry["description"] = *offer.Description
		}
		if offer.Availability != nil {
			entry["availability"] = canonicalAvailability(*offer.Availability)
		}
		if offer.Type == "subscription" {
			if offer.Plan != nil {
				entry["plan"] = *offer.Plan
			}
			if offer.BillingInterval != nil {
				entry["billing_interval"] = canonicalInterval(*offer.BillingInterval)
			}
			if offer.Trial != nil {
				entry["trial"] = canonicalInterval(*offer.Trial)
			}
			if offer.CycleGrant != nil {
				entry["cycle_grant"] = map[string]any{
					"amount": amountConfigString(offer.CycleGrant.Amount), "bucket": offer.CycleGrant.Bucket,
					"renewal": offer.CycleGrant.Renewal, "expiry": canonicalExpiry(offer.CycleGrant.Expiry),
				}
			}
		} else {
			if offer.CreditsPerUnit != nil {
				entry["credits_per_unit"] = amountConfigString(*offer.CreditsPerUnit)
			}
			if offer.Quantity != nil {
				entry["quantity"] = map[string]any{
					"minimum": offer.Quantity.Minimum, "maximum": offer.Quantity.Maximum, "default": offer.Quantity.Default,
				}
			}
			if offer.Bucket != nil {
				entry["bucket"] = *offer.Bucket
			}
			if offer.Expiry != nil {
				entry["expiry"] = canonicalExpiry(*offer.Expiry)
			}
			entry["lot_behavior"] = offer.LotBehavior
		}
		offers[key] = entry
	}
	result := map[string]any{"providers": providers, "offers": offers}
	if len(value.SubscriptionChanges) > 0 {
		changes := make(map[string]any, len(value.SubscriptionChanges))
		for key, policy := range value.SubscriptionChanges {
			changes[key] = map[string]any{
				"effective": policy.Effective, "proration": policy.Proration, "payment_failure": policy.PaymentFailure,
			}
		}
		result["subscription_changes"] = changes
	}
	if value.AutoRecharge != nil {
		auto := value.AutoRecharge
		result["auto_recharge"] = map[string]any{
			"eligible_topups": append([]string(nil), auto.EligibleTopups...),
			"balance_below": map[string]any{
				"minimum": amountConfigString(auto.BalanceBelow.Minimum), "maximum": amountConfigString(auto.BalanceBelow.Maximum),
				"default": amountConfigString(auto.BalanceBelow.Default),
			},
			"rearm_above": amountConfigString(auto.RearmAbove),
			"quantity":    map[string]any{"minimum": auto.Quantity.Minimum, "maximum": auto.Quantity.Maximum, "default": auto.Quantity.Default},
			"limits": map[string]any{
				"max_purchases": auto.Limits.MaxPurchases, "window": canonicalWindow(auto.Limits.Window),
				"max_charge_minor": auto.Limits.MaxChargeMinor, "cooldown": canonicalDuration(auto.Limits.Cooldown),
				"max_consecutive_failures": auto.Limits.MaxConsecutiveFailure, "failure_action": auto.Limits.FailureAction,
			},
		}
	}
	return result
}

// CanonicalCatalogRolloutDict serializes a validated rollout manifest.
func CanonicalCatalogRolloutDict(value map[string]any) (map[string]any, error) {
	rollout, err := LoadCatalogRollout(value)
	if err != nil {
		return nil, err
	}
	plans := make(map[string]any, len(rollout.Plans))
	for key, policy := range rollout.Plans {
		plans[key] = map[string]any{"effective": policy.Effective, "include_pinned": policy.IncludePinned}
	}
	return map[string]any{"plans": plans}, nil
}
