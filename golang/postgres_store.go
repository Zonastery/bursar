package bursar

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"math/big"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5/pgtype"
	"github.com/jackc/pgx/v5/pgxpool"
)

// PostgresStoreOptions configures the production CreditStore implementation.
// TenantID and ProviderEnvironment are intentionally required so a store can
// never accidentally operate against unscoped financial data.
type PostgresStoreOptions struct {
	TenantID               string
	Pool                   *pgxpool.Pool
	MaxConnections         int32
	UsageBackend           string
	BillingPayloadBackend  string
	ProviderEnvironment    ProviderEnvironment
	ConnectionTimeout      time.Duration
	StatementTimeout       time.Duration
	IdleTransactionTimeout time.Duration
	ApplicationName        string
	AccessRole             PostgresAccessRole
	OnPoolError            func(error)
	Instrumentation        Instrumentation
}

// PostgresStore is the PostgreSQL implementation of CreditStore. Credit
// accounting stays inside Bursar's SQL RPCs; this type only validates,
// serializes, scopes, and maps their committed outcomes.
type PostgresStore struct {
	client              *PostgresClient
	databaseURL         string
	tenantID            string
	providerEnvironment ProviderEnvironment
}

var _ CreditStore = (*PostgresStore)(nil)

// NewPostgresStore creates a tenant-scoped store around either databaseURL or
// an application-owned options.Pool. Passing both is rejected.
func NewPostgresStore(ctx context.Context, databaseURL string, options PostgresStoreOptions) (*PostgresStore, error) {
	clientOptions, tenantID, environment, err := postgresStoreClientOptions(options)
	if err != nil {
		return nil, err
	}
	if options.Pool != nil {
		if strings.TrimSpace(databaseURL) != "" {
			return nil, errorf(ErrorCodeConfig, ErrorCategoryInvalidRequest, "provide either a database URL or PostgreSQL pool, not both")
		}
		client, err := NewPostgresClientFromPool(options.Pool, clientOptions)
		if err != nil {
			return nil, err
		}
		return &PostgresStore{client: client, tenantID: tenantID, providerEnvironment: environment}, nil
	}
	if strings.TrimSpace(databaseURL) == "" {
		return nil, errorf(ErrorCodeConfig, ErrorCategoryInvalidRequest, "database URL is required when a PostgreSQL pool is not provided")
	}
	client, err := NewPostgresClient(ctx, databaseURL, clientOptions)
	if err != nil {
		return nil, err
	}
	return &PostgresStore{
		client:              client,
		databaseURL:         databaseURL,
		tenantID:            tenantID,
		providerEnvironment: environment,
	}, nil
}

// NewPostgresStoreFromPool creates a tenant-scoped store around an
// application-owned pgx pool. Close leaves the caller's pool open.
func NewPostgresStoreFromPool(pool *pgxpool.Pool, options PostgresStoreOptions) (*PostgresStore, error) {
	if pool == nil {
		return nil, errorf(ErrorCodeConfig, ErrorCategoryInvalidRequest, "PostgreSQL pool is required")
	}
	clientOptions, tenantID, environment, err := postgresStoreClientOptions(options)
	if err != nil {
		return nil, err
	}
	client, err := NewPostgresClientFromPool(pool, clientOptions)
	if err != nil {
		return nil, err
	}
	return &PostgresStore{client: client, tenantID: tenantID, providerEnvironment: environment}, nil
}

func postgresStoreClientOptions(options PostgresStoreOptions) (PostgresClientOptions, string, ProviderEnvironment, error) {
	tenantID, err := normalizeTenantID(options.TenantID)
	if err != nil {
		return PostgresClientOptions{}, "", "", err
	}
	if options.ProviderEnvironment == "" {
		return PostgresClientOptions{}, "", "", errorf(ErrorCodeConfig, ErrorCategoryInvalidRequest, "provider environment is required for PostgresStore")
	}
	if err := options.ProviderEnvironment.Validate(); err != nil {
		return PostgresClientOptions{}, "", "", NewError("invalid provider environment", ErrorOptions{
			Code:     ErrorCodeConfig,
			Category: ErrorCategoryInvalidRequest,
			Cause:    err,
		})
	}
	return PostgresClientOptions{
		TenantID:               tenantID,
		AccessRole:             options.AccessRole,
		UsageBackend:           options.UsageBackend,
		BillingPayloadBackend:  options.BillingPayloadBackend,
		ProviderEnvironment:    options.ProviderEnvironment,
		ConnectionTimeout:      options.ConnectionTimeout,
		StatementTimeout:       options.StatementTimeout,
		IdleTransactionTimeout: options.IdleTransactionTimeout,
		MaxConnections:         options.MaxConnections,
		ApplicationName:        options.ApplicationName,
		OnPoolError:            options.OnPoolError,
		Instrumentation:        options.Instrumentation,
	}, tenantID, options.ProviderEnvironment, nil
}

// TenantID returns the validated UUID attached to every store transaction.
func (s *PostgresStore) TenantID() string {
	if s == nil {
		return ""
	}
	return s.tenantID
}

// ProviderEnvironment returns the explicit financial-provider namespace used
// by catalog and credit operations.
func (s *PostgresStore) ProviderEnvironment() ProviderEnvironment {
	if s == nil {
		return ""
	}
	return s.providerEnvironment
}

// DatabaseURL returns the supplied connection string for SDK-owned pools. It
// returns an error for caller-owned pools so credentials are not synthesized.
func (s *PostgresStore) DatabaseURL() (string, error) {
	if s == nil || s.client == nil {
		return "", NewError("PostgreSQL store is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	if !s.client.OwnsPool() {
		return "", NewError("PostgresStore uses an application-owned pool", ErrorOptions{Code: ErrorCodeConfig, Category: ErrorCategoryConflict})
	}
	return s.databaseURL, nil
}

// Close releases only resources owned by the store.
func (s *PostgresStore) Close() error {
	if s == nil || s.client == nil {
		return nil
	}
	return s.client.Close()
}

func (s *PostgresStore) withTx(ctx context.Context, callback func(context.Context, *PostgresTransaction) error) error {
	if s == nil || s.client == nil {
		return NewError("PostgreSQL store is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	return s.client.WithTx(ctx, callback)
}

func rowRequired(rows []map[string]any, operation string) (map[string]any, error) {
	if len(rows) == 0 || rows[0] == nil {
		return nil, NewStoreError(operation+" returned no result", ErrorOptions{})
	}
	return rows[0], nil
}

func rowOptional(rows []map[string]any) map[string]any {
	if len(rows) == 0 || rows[0] == nil {
		return nil
	}
	for _, value := range rows[0] {
		if value != nil {
			return rows[0]
		}
	}
	return nil
}

func firstScalar(row map[string]any, operation string) (any, error) {
	if len(row) != 1 {
		return nil, NewStoreError(operation+" returned an invalid scalar result", ErrorOptions{})
	}
	for _, value := range row {
		return value, nil
	}
	return nil, NewStoreError(operation+" returned no scalar result", ErrorOptions{})
}

func rowValue(row map[string]any, key string) any {
	if row == nil {
		return nil
	}
	return row[key]
}

func requiredRowText(row map[string]any, key, operation string) (string, error) {
	value := rowValue(row, key)
	text, ok := textValue(value)
	if !ok || strings.TrimSpace(text) == "" {
		return "", NewStoreError(operation+" returned an invalid "+key, ErrorOptions{})
	}
	return text, nil
}

func optionalRowText(row map[string]any, key string) string {
	text, _ := textValue(rowValue(row, key))
	return text
}

func textValue(value any) (string, bool) {
	switch typed := value.(type) {
	case nil:
		return "", false
	case string:
		return typed, true
	case []byte:
		return string(typed), true
	case pgtype.UUID:
		if !typed.Valid {
			return "", false
		}
		return formatUUID(typed.Bytes), true
	case [16]byte:
		return formatUUID(typed), true
	case fmt.Stringer:
		return typed.String(), true
	default:
		return fmt.Sprint(typed), true
	}
}

func formatUUID(bytes [16]byte) string {
	return fmt.Sprintf("%08x-%04x-%04x-%04x-%012x",
		bytes[0:4], bytes[4:6], bytes[6:8], bytes[8:10], bytes[10:16])
}

func rowBool(row map[string]any, key, operation string) (bool, error) {
	value := rowValue(row, key)
	switch typed := value.(type) {
	case bool:
		return typed, nil
	case string:
		parsed, err := strconv.ParseBool(typed)
		if err == nil {
			return parsed, nil
		}
	case []byte:
		parsed, err := strconv.ParseBool(string(typed))
		if err == nil {
			return parsed, nil
		}
	}
	return false, NewStoreError(operation+" returned an invalid "+key, ErrorOptions{})
}

func scalarBool(value any, operation string) (bool, error) {
	return rowBool(map[string]any{"value": value}, "value", operation)
}

func rowInt(row map[string]any, key, operation string) (int, error) {
	value := rowValue(row, key)
	switch typed := value.(type) {
	case int:
		return typed, nil
	case int8:
		return int(typed), nil
	case int16:
		return int(typed), nil
	case int32:
		return int(typed), nil
	case int64:
		if int64(int(typed)) == typed {
			return int(typed), nil
		}
	case uint:
		if uint64(typed) <= uint64(^uint(0)>>1) {
			return int(typed), nil
		}
	case uint32:
		return int(typed), nil
	case uint64:
		if typed <= uint64(^uint(0)>>1) {
			return int(typed), nil
		}
	case string:
		parsed, err := strconv.Atoi(typed)
		if err == nil {
			return parsed, nil
		}
	case json.Number:
		parsed, err := strconv.Atoi(typed.String())
		if err == nil {
			return parsed, nil
		}
	case []byte:
		parsed, err := strconv.Atoi(string(typed))
		if err == nil {
			return parsed, nil
		}
	}
	return 0, NewStoreError(operation+" returned an invalid "+key, ErrorOptions{})
}

func scalarInt(value any, operation string) (int, error) {
	return rowInt(map[string]any{"value": value}, "value", operation)
}

func parseAmount(value any, field string) (Amount, error) {
	if value == nil {
		return DecimalZero, NewStoreError("PostgreSQL returned a null "+field, ErrorOptions{})
	}
	if amount, ok := value.(Amount); ok {
		return amount, nil
	}
	var text string
	switch typed := value.(type) {
	case string:
		text = typed
	case json.Number:
		text = typed.String()
	case float64:
		// pgx may decode JSONB into map[string]any before this boundary. Use
		// fixed-point formatting so ordinary credit-scale values do not acquire
		// exponent notation or binary-artifact digits before decimal parsing.
		text = strconv.FormatFloat(typed, 'f', -1, 64)
	case []byte:
		text = string(typed)
	case pgtype.Numeric:
		if !typed.Valid || typed.Int == nil || typed.NaN || typed.InfinityModifier != pgtype.Finite {
			return DecimalZero, NewStoreError("PostgreSQL returned a null "+field, ErrorOptions{})
		}
		text = numericText(typed.Int, typed.Exp)
	case int:
		text = strconv.Itoa(typed)
	case int8:
		text = strconv.FormatInt(int64(typed), 10)
	case int16:
		text = strconv.FormatInt(int64(typed), 10)
	case int32:
		text = strconv.FormatInt(int64(typed), 10)
	case int64:
		text = strconv.FormatInt(typed, 10)
	case uint:
		text = strconv.FormatUint(uint64(typed), 10)
	case uint8:
		text = strconv.FormatUint(uint64(typed), 10)
	case uint16:
		text = strconv.FormatUint(uint64(typed), 10)
	case uint32:
		text = strconv.FormatUint(uint64(typed), 10)
	case uint64:
		text = strconv.FormatUint(typed, 10)
	default:
		return DecimalZero, NewStoreError("PostgreSQL returned an invalid "+field, ErrorOptions{})
	}
	amount, err := NewAmount(text)
	if err != nil {
		return DecimalZero, NewStoreError("PostgreSQL returned an invalid "+field, ErrorOptions{Cause: err})
	}
	return amount, nil
}

func numericText(integer *big.Int, exponent int32) string {
	text := integer.String()
	negative := strings.HasPrefix(text, "-")
	digits := strings.TrimPrefix(text, "-")
	if exponent >= 0 {
		result := digits + strings.Repeat("0", int(exponent))
		if negative {
			return "-" + result
		}
		return result
	}
	point := len(digits) + int(exponent)
	var result string
	if point > 0 {
		result = digits[:point] + "." + digits[point:]
	} else {
		result = "0." + strings.Repeat("0", -point) + digits
	}
	if negative {
		return "-" + result
	}
	return result
}

func rowAmount(row map[string]any, key, operation string) (Amount, error) {
	return parseAmount(rowValue(row, key), operation+"."+key)
}

func optionalRowAmount(row map[string]any, key, operation string) (*Amount, error) {
	if rowValue(row, key) == nil {
		return nil, nil
	}
	amount, err := rowAmount(row, key, operation)
	if err != nil {
		return nil, err
	}
	return creditAmountPointer(amount), nil
}

func rowTime(row map[string]any, key, operation string) (time.Time, error) {
	value := rowValue(row, key)
	switch typed := value.(type) {
	case time.Time:
		return typed.UTC(), nil
	case string:
		parsed, err := time.Parse(time.RFC3339Nano, typed)
		if err == nil {
			return parsed.UTC(), nil
		}
	case []byte:
		parsed, err := time.Parse(time.RFC3339Nano, string(typed))
		if err == nil {
			return parsed.UTC(), nil
		}
	}
	return time.Time{}, NewStoreError(operation+" returned an invalid "+key, ErrorOptions{})
}

func optionalRowTime(row map[string]any, key, operation string) (*time.Time, error) {
	if rowValue(row, key) == nil {
		return nil, nil
	}
	value, err := rowTime(row, key, operation)
	if err != nil {
		return nil, err
	}
	return timePointer(value), nil
}

func jsonMap(value any, field string) (map[string]any, error) {
	if value == nil {
		return nil, nil
	}
	if mapped, ok := value.(map[string]any); ok {
		return cloneAnyMap(mapped), nil
	}
	var raw []byte
	switch typed := value.(type) {
	case []byte:
		raw = typed
	case string:
		raw = []byte(typed)
	default:
		return nil, NewStoreError("PostgreSQL returned an invalid "+field, ErrorOptions{})
	}
	var mapped map[string]any
	if err := decodeJSONUseNumber(raw, &mapped); err != nil {
		return nil, NewStoreError("PostgreSQL returned invalid JSON for "+field, ErrorOptions{Cause: err})
	}
	return mapped, nil
}

func decodeJSONUseNumber(data []byte, destination any) error {
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.UseNumber()
	return decoder.Decode(destination)
}

func amountMap(value any, field string) (map[string]Amount, error) {
	mapped, err := jsonMap(value, field)
	if err != nil {
		return nil, err
	}
	if mapped == nil {
		return nil, nil
	}
	result := make(map[string]Amount, len(mapped))
	for key, raw := range mapped {
		amount, err := parseAmount(raw, field+"."+key)
		if err != nil {
			return nil, err
		}
		result[key] = amount
	}
	return result, nil
}

func stringSlice(value any, field string) ([]string, error) {
	if value == nil {
		return nil, nil
	}
	switch typed := value.(type) {
	case []string:
		return append([]string(nil), typed...), nil
	case []any:
		result := make([]string, len(typed))
		for index, item := range typed {
			text, ok := textValue(item)
			if !ok {
				return nil, NewStoreError("PostgreSQL returned an invalid "+field, ErrorOptions{})
			}
			result[index] = text
		}
		return result, nil
	case []byte, string:
		var result []string
		var raw []byte
		if bytes, ok := typed.([]byte); ok {
			raw = bytes
		} else {
			raw = []byte(typed.(string))
		}
		if err := json.Unmarshal(raw, &result); err == nil {
			return result, nil
		}
	}
	return nil, NewStoreError("PostgreSQL returned an invalid "+field, ErrorOptions{})
}

func floatSlice(value any, field string) ([]float64, error) {
	if value == nil {
		return nil, nil
	}
	decode := func(item any) (float64, error) {
		switch number := item.(type) {
		case float64:
			return number, nil
		case float32:
			return float64(number), nil
		case int:
			return float64(number), nil
		case int64:
			return float64(number), nil
		case string:
			return strconv.ParseFloat(number, 64)
		case []byte:
			return strconv.ParseFloat(string(number), 64)
		default:
			return 0, fmt.Errorf("invalid number")
		}
	}
	items, ok := value.([]any)
	if !ok {
		if numbers, ok := value.([]float64); ok {
			return append([]float64(nil), numbers...), nil
		}
		return nil, NewStoreError("PostgreSQL returned an invalid "+field, ErrorOptions{})
	}
	result := make([]float64, len(items))
	for index, item := range items {
		parsed, err := decode(item)
		if err != nil {
			return nil, NewStoreError("PostgreSQL returned an invalid "+field, ErrorOptions{Cause: err})
		}
		result[index] = parsed
	}
	return result, nil
}

// JSON marshaling helpers deliberately return json.RawMessage rather than a
// bare []byte. pgx encodes a bare byte slice as bytea, while RawMessage uses
// its JSON codec when PostgreSQL resolves an RPC argument as json/jsonb.
func marshalMetadata(metadata CreditMetadata) (json.RawMessage, error) {
	encoded, err := json.Marshal(metadata.Clone())
	if err != nil {
		return nil, NewError("credit metadata must be JSON serializable", ErrorOptions{Code: ErrorCodeConfig, Category: ErrorCategoryInvalidRequest, Cause: err})
	}
	return encoded, nil
}

func marshalMeasures(measures map[string]Amount) (json.RawMessage, error) {
	if measures == nil {
		return json.RawMessage("{}"), nil
	}
	encoded, err := json.Marshal(measures)
	if err != nil {
		return nil, NewError("credit measures must be JSON serializable", ErrorOptions{Code: ErrorCodeConfig, Category: ErrorCategoryInvalidRequest, Cause: err})
	}
	return encoded, nil
}

func marshalDimensions(dimensions map[string]any) (json.RawMessage, error) {
	if dimensions == nil {
		return json.RawMessage("{}"), nil
	}
	encoded, err := json.Marshal(dimensions)
	if err != nil {
		return nil, NewError("credit dimensions must be JSON serializable", ErrorOptions{Code: ErrorCodeConfig, Category: ErrorCategoryInvalidRequest, Cause: err})
	}
	return encoded, nil
}

func amountArgument(value Amount) string {
	return QuantizeMoney(value).StringFixed(MoneyDecimalPlaces)
}

func nullableText(value string) any {
	if strings.TrimSpace(value) == "" {
		return nil
	}
	return value
}

func nullableTime(value *time.Time) any {
	if value == nil {
		return nil
	}
	return value.UTC()
}

func operationPayload(options OperationUsageOptions, metadata CreditMetadata) (metadataJSON, measuresJSON, dimensionsJSON json.RawMessage, err error) {
	dimensions := make(map[string]any, len(options.Dimensions)+2)
	for key, value := range options.Dimensions {
		dimensions[key] = value
	}
	if options.Model != "" {
		if _, exists := dimensions["model"]; !exists {
			dimensions["model"] = options.Model
		}
	}
	if options.Region != "" {
		if _, exists := dimensions["region"]; !exists {
			dimensions["region"] = options.Region
		}
	}
	metadataJSON, err = marshalMetadata(metadata)
	if err != nil {
		return nil, nil, nil, err
	}
	measuresJSON, err = marshalMeasures(options.Measures)
	if err != nil {
		return nil, nil, nil, err
	}
	dimensionsJSON, err = marshalDimensions(dimensions)
	if err != nil {
		return nil, nil, nil, err
	}
	return metadataJSON, measuresJSON, dimensionsJSON, nil
}

func (s *PostgresStore) creditState(ctx context.Context, tx *PostgresTransaction, userID string) (map[string]any, error) {
	rows, err := tx.Call(ctx, "get_credit_state", userID)
	if err != nil {
		return nil, err
	}
	return rowOptional(rows), nil
}

func balanceFromState(userID string, state map[string]any, operation string) (BalanceResult, error) {
	if state == nil {
		return BalanceResult{UserID: userID, Balance: DecimalZero, LifetimePurchased: DecimalZero}, nil
	}
	balance, err := rowAmount(state, "balance", operation)
	if err != nil {
		return BalanceResult{}, err
	}
	lifetime, err := rowAmount(state, "lifetime_purchased", operation)
	if err != nil {
		return BalanceResult{}, err
	}
	return BalanceResult{UserID: userID, Balance: balance, LifetimePurchased: lifetime}, nil
}

// GetBalance returns a tenant-scoped committed credit balance.
func (s *PostgresStore) GetBalance(ctx context.Context, userID string) (result BalanceResult, err error) {
	userID, err = requireText(userID, "user ID")
	if err != nil {
		return result, err
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		state, err := s.creditState(ctx, tx, userID)
		if err != nil {
			return err
		}
		result, err = balanceFromState(userID, state, "get_credit_state")
		return err
	})
	return result, err
}

// GetAvailable returns a tenant-scoped advisory balance net of active leases.
func (s *PostgresStore) GetAvailable(ctx context.Context, userID string) (result AvailableResult, err error) {
	userID, err = requireText(userID, "user ID")
	if err != nil {
		return result, err
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		state, err := s.creditState(ctx, tx, userID)
		if err != nil {
			return err
		}
		if state == nil {
			result = AvailableResult{UserID: userID, Balance: DecimalZero, Reserved: DecimalZero, Available: DecimalZero}
			return nil
		}
		balance, err := rowAmount(state, "balance", "get_credit_state")
		if err != nil {
			return err
		}
		reserved, err := rowAmount(state, "reserved", "get_credit_state")
		if err != nil {
			return err
		}
		available, err := rowAmount(state, "available", "get_credit_state")
		if err != nil {
			return err
		}
		result = AvailableResult{UserID: userID, Balance: balance, Reserved: reserved, Available: available}
		return nil
	})
	return result, err
}

// AddCredits posts an idempotent grant or adjustment through the authoritative
// post_credit RPC. A replay returns the original committed result.
func (s *PostgresStore) AddCredits(ctx context.Context, userID string, amount Amount, options AddCreditsOptions) (result AddCreditsResult, err error) {
	userID, err = requireText(userID, "user ID")
	if err != nil {
		return result, err
	}
	idempotencyKey, err := requireStableKey(options.IdempotencyKey, "add credits idempotency key")
	if err != nil {
		return result, err
	}
	entryType := strings.TrimSpace(options.Type)
	if entryType == "" {
		entryType = "adjustment"
	}
	metadata := options.Metadata.Clone()
	if options.ExpiresAt != nil {
		metadata["expires_at"] = options.ExpiresAt.UTC().Format(time.RFC3339Nano)
	}
	metadataJSON, err := marshalMetadata(metadata)
	if err != nil {
		return result, err
	}
	entryKind := "grant"
	if amount.IsNegative() {
		entryKind = "adjustment"
	} else if entryType == "purchase" {
		entryKind = "purchase"
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, err := tx.Call(
			ctx,
			"post_credit",
			userID,
			entryKind,
			amountArgument(amount),
			entryType,
			idempotencyKey,
			metadataJSON,
			nullableText(options.Bucket),
			nil,
			nullableTime(options.ExpiresAt),
			"0",
		)
		if err != nil {
			return err
		}
		row, err := rowRequired(rows, "post_credit")
		if err != nil {
			return err
		}
		if errorCode := optionalRowText(row, "error_code"); errorCode != "" {
			return NewStoreError("post_credit failed: "+errorCode, ErrorOptions{Details: map[string]any{"error_code": errorCode}})
		}
		entryID, err := requiredRowText(row, "entry_id", "post_credit")
		if err != nil {
			return err
		}
		newBalance, err := rowAmount(row, "balance_after", "post_credit")
		if err != nil {
			return err
		}
		idempotent, err := rowBool(row, "replayed", "post_credit")
		if err != nil {
			return err
		}
		state, err := s.creditState(ctx, tx, userID)
		if err != nil {
			return err
		}
		balance, err := balanceFromState(userID, state, "get_credit_state")
		if err != nil {
			return err
		}
		bucket := ""
		if amount.GreaterThan(DecimalZero) {
			grantRows, err := tx.Call(ctx, "get_credit_grant_details", userID, entryID)
			if err != nil {
				return err
			}
			if grant := rowOptional(grantRows); grant != nil {
				bucket = optionalRowText(grant, "bucket_key")
				if bucket == "" {
					if scalar, scalarErr := firstScalar(grant, "get_credit_grant_details"); scalarErr == nil {
						bucket, _ = textValue(scalar)
					}
				}
			}
		}
		result = AddCreditsResult{
			EntryID:           entryID,
			UserID:            userID,
			Amount:            QuantizeMoney(amount),
			NewBalance:        newBalance,
			LifetimePurchased: balance.LifetimePurchased,
			Bucket:            bucket,
			Idempotent:        idempotent,
		}
		return nil
	})
	return result, err
}

// DeductWithAllowance atomically consumes allowance and debits the remaining
// amount through charge_usage_for_operation. Business denials stay in
// DeductionResult.ErrorCode so CreditsService can map them to public errors.
func (s *PostgresStore) DeductWithAllowance(ctx context.Context, userID string, amount Amount, options DeductWithAllowanceOptions) (result DeductionResult, err error) {
	userID, err = requireText(userID, "user ID")
	if err != nil {
		return result, err
	}
	if _, err = requireNonNegativeAmount(amount, "deduct"); err != nil {
		return result, err
	}
	operation := strings.TrimSpace(options.Operation)
	if operation == "" {
		operation = "usage"
	}
	idempotencyKey, err := requireStableKey(options.IdempotencyKey, "deduct idempotency key")
	if err != nil {
		return result, err
	}
	metadataJSON, measuresJSON, dimensionsJSON, err := operationPayload(options.OperationUsageOptions, options.Metadata)
	if err != nil {
		return result, err
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, err := tx.Call(
			ctx,
			"charge_usage_for_operation",
			userID,
			operation,
			amountArgument(amount),
			idempotencyKey,
			nullableText(options.Feature),
			nullableText(options.Model),
			nullableText(options.Region),
			metadataJSON,
			measuresJSON,
			dimensionsJSON,
		)
		if err != nil {
			return err
		}
		row, err := rowRequired(rows, "charge_usage_for_operation")
		if err != nil {
			return err
		}
		charged, err := rowAmount(row, "charged", "charge_usage_for_operation")
		if err != nil {
			return err
		}
		allowance, err := rowAmount(row, "allowance_covered", "charge_usage_for_operation")
		if err != nil {
			return err
		}
		errorCode := optionalRowText(row, "error_code")
		if errorCode != "" {
			result = DeductionResult{
				UserID:            userID,
				Amount:            charged,
				AllowanceConsumed: allowance,
				ErrorCode:         errorCode,
			}
			return nil
		}
		entryID, err := requiredRowText(row, "ledger_entry_id", "charge_usage_for_operation")
		if err != nil {
			return err
		}
		chargeID, err := requiredRowText(row, "charge_id", "charge_usage_for_operation")
		if err != nil {
			return err
		}
		idempotent, err := rowBool(row, "replayed", "charge_usage_for_operation")
		if err != nil {
			return err
		}
		detailsRows, err := tx.Call(ctx, "get_credit_operation_details", userID, entryID, idempotencyKey)
		if err != nil {
			return err
		}
		details, err := rowRequired(detailsRows, "get_credit_operation_details")
		if err != nil {
			return err
		}
		balanceAfter, err := rowAmount(details, "balance_after", "get_credit_operation_details")
		if err != nil {
			return err
		}
		breakdown, err := amountMap(rowValue(details, "bucket_breakdown"), "get_credit_operation_details.bucket_breakdown")
		if err != nil {
			return err
		}
		result = DeductionResult{
			EntryID:           entryID,
			UsageChargeID:     chargeID,
			UserID:            userID,
			Amount:            charged,
			BalanceAfter:      creditAmountPointer(balanceAfter),
			AllowanceConsumed: allowance,
			Idempotent:        idempotent,
			BucketBreakdown:   breakdown,
		}
		return nil
	})
	return result, err
}

// RecordUsage writes a priced usage receipt without a second account debit.
func (s *PostgresStore) RecordUsage(ctx context.Context, userID, operation string, requested Amount, options RecordUsageOptions) (result UsageRecordResult, err error) {
	userID, err = requireText(userID, "user ID")
	if err != nil {
		return result, err
	}
	operation, err = requireText(operation, "operation")
	if err != nil {
		return result, err
	}
	if _, err = requireNonNegativeAmount(requested, "record usage"); err != nil {
		return result, err
	}
	idempotencyKey, err := requireStableKey(options.IdempotencyKey, "record usage idempotency key")
	if err != nil {
		return result, err
	}
	metadataJSON, measuresJSON, dimensionsJSON, err := operationPayload(options.OperationUsageOptions, options.Metadata)
	if err != nil {
		return result, err
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, err := tx.Call(ctx, "record_usage", userID, operation, amountArgument(requested), idempotencyKey, nullableText(options.Feature), nullableText(options.Model), nullableText(options.Region), metadataJSON, measuresJSON, dimensionsJSON)
		if err != nil {
			return err
		}
		row, err := rowRequired(rows, "record_usage")
		if err != nil {
			return err
		}
		actualRequested, err := rowAmount(row, "requested", "record_usage")
		if err != nil {
			return err
		}
		errorCode := optionalRowText(row, "error_code")
		if errorCode != "" {
			result = UsageRecordResult{UserID: userID, Requested: actualRequested, ErrorCode: errorCode}
			return nil
		}
		usageID, err := requiredRowText(row, "charge_id", "record_usage")
		if err != nil {
			return err
		}
		idempotent, err := rowBool(row, "replayed", "record_usage")
		if err != nil {
			return err
		}
		result = UsageRecordResult{UsageID: usageID, UserID: userID, Requested: actualRequested, Idempotent: idempotent}
		return nil
	})
	return result, err
}

// CreateLease atomically reserves an amount through the database admission
// RPC. Available balance is read in the same transaction after the RPC so the
// returned projection corresponds to the committed/replayed state.
func (s *PostgresStore) CreateLease(ctx context.Context, userID string, amount Amount, operationType string, options CreateLeaseOptions) (result LeaseResult, err error) {
	userID, err = requireText(userID, "user ID")
	if err != nil {
		return result, err
	}
	operationType, err = requireText(operationType, "operation type")
	if err != nil {
		return result, err
	}
	if _, err = requireNonNegativeAmount(amount, "reserve"); err != nil {
		return result, err
	}
	idempotencyKey, err := requireStableKey(options.IdempotencyKey, "reserve idempotency key")
	if err != nil {
		return result, err
	}
	mode, err := requireBillingMode(options.BillingMode)
	if err != nil {
		return result, err
	}
	ttl := options.TTL
	if ttl == 0 {
		ttl = defaultLeaseTTL
	}
	if ttl, err = requirePositiveDuration(ttl, "lease TTL"); err != nil {
		return result, err
	}
	if options.MaxConcurrent != nil && *options.MaxConcurrent < 1 {
		return result, errorf(ErrorCodeConfig, ErrorCategoryInvalidRequest, "max concurrent leases must be positive")
	}
	if mode == BillingModeStrict && options.Floor.IsNegative() {
		return result, errorf(ErrorCodeConfig, ErrorCategoryInvalidRequest, "strict lease floor must not be negative")
	}
	if mode == BillingModeOverdraft && options.Floor.GreaterThan(DecimalZero) {
		return result, errorf(ErrorCodeConfig, ErrorCategoryInvalidRequest, "overdraft lease floor must not be positive")
	}
	metadataJSON, measuresJSON, dimensionsJSON, err := operationPayload(options.OperationUsageOptions, options.Metadata)
	if err != nil {
		return result, err
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, err := tx.Call(
			ctx,
			"create_lease_for_operation",
			userID,
			operationType,
			amountArgument(amount),
			idempotencyKey,
			fmt.Sprintf("%d seconds", int64(ttl/time.Second)),
			metadataJSON,
			nullableText(options.Feature),
			measuresJSON,
			dimensionsJSON,
			amountArgument(options.Floor),
			options.MaxConcurrent,
		)
		if err != nil {
			return err
		}
		row, err := rowRequired(rows, "create_lease_for_operation")
		if err != nil {
			return err
		}
		availabilityState, err := s.creditState(ctx, tx, userID)
		if err != nil {
			return err
		}
		available := AvailableResult{UserID: userID, Balance: DecimalZero, Reserved: DecimalZero, Available: DecimalZero}
		if availabilityState != nil {
			available.Balance, err = rowAmount(availabilityState, "balance", "get_credit_state")
			if err != nil {
				return err
			}
			available.Reserved, err = rowAmount(availabilityState, "reserved", "get_credit_state")
			if err != nil {
				return err
			}
			available.Available, err = rowAmount(availabilityState, "available", "get_credit_state")
			if err != nil {
				return err
			}
		}
		if errorCode := optionalRowText(row, "error_code"); errorCode != "" {
			var reserved *Amount
			if rowValue(row, "reserved_amount") != nil {
				reserved, err = optionalRowAmount(row, "reserved_amount", "create_lease_for_operation")
				if err != nil {
					return err
				}
			}
			result = LeaseResult{
				UserID:        userID,
				Amount:        reserved,
				Available:     available.Available,
				ReservedTotal: available.Reserved,
				BillingMode:   mode,
				ErrorCode:     errorCode,
			}
			return nil
		}
		leaseID, err := requiredRowText(row, "lease_id", "create_lease_for_operation")
		if err != nil {
			return err
		}
		reserved, err := rowAmount(row, "reserved_amount", "create_lease_for_operation")
		if err != nil {
			return err
		}
		leaseRows, err := tx.Call(ctx, "get_credit_lease", userID, leaseID)
		if err != nil {
			return err
		}
		lease, err := rowRequired(leaseRows, "get_credit_lease")
		if err != nil {
			return err
		}
		expiresAt, err := rowTime(lease, "expires_at", "get_credit_lease")
		if err != nil {
			return err
		}
		minimumBalance, err := rowAmount(lease, "minimum_balance", "get_credit_lease")
		if err != nil {
			return err
		}
		if minimumBalance.IsNegative() {
			mode = BillingModeOverdraft
		} else {
			mode = BillingModeStrict
		}
		result = LeaseResult{
			LeaseID:        leaseID,
			UserID:         userID,
			Amount:         creditAmountPointer(reserved),
			Available:      available.Available,
			ReservedTotal:  available.Reserved,
			MinimumBalance: creditAmountPointer(minimumBalance),
			BillingMode:    mode,
			ExpiresAt:      timePointer(expiresAt),
		}
		return nil
	})
	return result, err
}

// SettleLease atomically charges actual usage and finalizes a reservation. It
// deliberately does not clamp the amount to the original hold.
func (s *PostgresStore) SettleLease(ctx context.Context, userID, leaseID string, amount Amount, options SettleLeaseOptions) (result DeductionResult, err error) {
	userID, err = requireText(userID, "user ID")
	if err != nil {
		return result, err
	}
	leaseID, err = requireText(leaseID, "lease ID")
	if err != nil {
		return result, err
	}
	if _, err = requireNonNegativeAmount(amount, "settle"); err != nil {
		return result, err
	}
	idempotencyKey := options.IdempotencyKey
	if idempotencyKey == "" {
		idempotencyKey = "lease:" + leaseID + ":settle"
	}
	idempotencyKey, err = requireStableKey(idempotencyKey, "settle idempotency key")
	if err != nil {
		return result, err
	}
	metadataJSON, measuresJSON, dimensionsJSON, err := operationPayload(options.OperationUsageOptions, options.Metadata)
	if err != nil {
		return result, err
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, err := tx.Call(
			ctx,
			"settle_lease",
			userID,
			leaseID,
			amountArgument(amount),
			idempotencyKey,
			nullableText(options.Feature),
			nullableText(options.Model),
			nullableText(options.Region),
			measuresJSON,
			dimensionsJSON,
			metadataJSON,
		)
		if err != nil {
			return err
		}
		row, err := rowRequired(rows, "settle_lease")
		if err != nil {
			return err
		}
		settled, err := rowAmount(row, "settled_amount", "settle_lease")
		if err != nil {
			return err
		}
		errorCode := optionalRowText(row, "error_code")
		if errorCode != "" {
			var balanceAfter *Amount
			if rowValue(row, "balance_after") != nil {
				balanceAfter, err = optionalRowAmount(row, "balance_after", "settle_lease")
				if err != nil {
					return err
				}
			}
			result = DeductionResult{UserID: userID, Amount: settled, BalanceAfter: balanceAfter, AllowanceConsumed: DecimalZero, ErrorCode: errorCode}
			return nil
		}
		entryID, err := requiredRowText(row, "ledger_entry_id", "settle_lease")
		if err != nil {
			return err
		}
		chargeID, err := requiredRowText(row, "charge_id", "settle_lease")
		if err != nil {
			return err
		}
		idempotent, err := rowBool(row, "replayed", "settle_lease")
		if err != nil {
			return err
		}
		detailsRows, err := tx.Call(ctx, "get_credit_operation_details", userID, entryID, idempotencyKey)
		if err != nil {
			return err
		}
		details, err := rowRequired(detailsRows, "get_credit_operation_details")
		if err != nil {
			return err
		}
		balanceAfter, err := rowAmount(details, "balance_after", "get_credit_operation_details")
		if err != nil {
			return err
		}
		allowance, err := rowAmount(details, "allowance_covered", "get_credit_operation_details")
		if err != nil {
			return err
		}
		breakdown, err := amountMap(rowValue(details, "bucket_breakdown"), "get_credit_operation_details.bucket_breakdown")
		if err != nil {
			return err
		}
		result = DeductionResult{
			EntryID:           entryID,
			UsageChargeID:     chargeID,
			UserID:            userID,
			Amount:            settled,
			BalanceAfter:      creditAmountPointer(balanceAfter),
			AllowanceConsumed: allowance,
			Idempotent:        idempotent,
			BucketBreakdown:   breakdown,
		}
		return nil
	})
	return result, err
}

// GetLeasePricingContext retrieves the immutable pricing snapshot captured at
// admission. A missing or differently owned lease returns nil.
func (s *PostgresStore) GetLeasePricingContext(ctx context.Context, userID, leaseID string) (result *LeasePricingContext, err error) {
	userID, err = requireText(userID, "user ID")
	if err != nil {
		return nil, err
	}
	leaseID, err = requireText(leaseID, "lease ID")
	if err != nil {
		return nil, err
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, err := tx.Call(ctx, "get_credit_lease_pricing_context", userID, leaseID)
		if err != nil {
			return err
		}
		row := rowOptional(rows)
		if row == nil {
			return nil
		}
		version, err := rowInt(row, "catalog_revision_no", "get_credit_lease_pricing_context")
		if err != nil {
			return err
		}
		result = &LeasePricingContext{
			CatalogVersion: version,
			PlanID:         optionalRowText(row, "plan_id"),
			PlanKey:        optionalRowText(row, "plan_key"),
			RateCard:       optionalRowText(row, "rate_card"),
		}
		return nil
	})
	return result, err
}

// ReleaseLease releases an active or expired lease without a debit. It is safe
// to repeat after finalized or missing leases.
func (s *PostgresStore) ReleaseLease(ctx context.Context, userID, leaseID string) (result ReleaseResult, err error) {
	userID, err = requireText(userID, "user ID")
	if err != nil {
		return result, err
	}
	leaseID, err = requireText(leaseID, "lease ID")
	if err != nil {
		return result, err
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, err := tx.Call(ctx, "release_lease", userID, leaseID)
		if err != nil {
			return err
		}
		row, err := rowRequired(rows, "release_lease")
		if err != nil {
			return err
		}
		value, err := firstScalar(row, "release_lease")
		if err != nil {
			return err
		}
		status, ok := textValue(value)
		if !ok {
			return NewStoreError("release_lease returned an invalid lease status", ErrorOptions{})
		}
		result = ReleaseResult{LeaseID: leaseID, UserID: userID, Released: status == "released"}
		if !result.Released {
			result.Reason = status
		}
		return nil
	})
	return result, err
}

// RenewLease extends an active lease without changing its policy snapshot.
func (s *PostgresStore) RenewLease(ctx context.Context, userID, leaseID string, ttl time.Duration) (result LeaseResult, err error) {
	userID, err = requireText(userID, "user ID")
	if err != nil {
		return result, err
	}
	leaseID, err = requireText(leaseID, "lease ID")
	if err != nil {
		return result, err
	}
	if ttl, err = requirePositiveDuration(ttl, "lease TTL"); err != nil {
		return result, err
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, err := tx.Call(ctx, "renew_lease", userID, leaseID, fmt.Sprintf("%d seconds", int64(ttl/time.Second)))
		if err != nil {
			return err
		}
		row, err := rowRequired(rows, "renew_lease")
		if err != nil {
			return err
		}
		availabilityState, err := s.creditState(ctx, tx, userID)
		if err != nil {
			return err
		}
		available := AvailableResult{UserID: userID, Balance: DecimalZero, Reserved: DecimalZero, Available: DecimalZero}
		if availabilityState != nil {
			available.Reserved, err = rowAmount(availabilityState, "reserved", "get_credit_state")
			if err != nil {
				return err
			}
			available.Available, err = rowAmount(availabilityState, "available", "get_credit_state")
			if err != nil {
				return err
			}
		}
		if errorCode := optionalRowText(row, "error_code"); errorCode != "" {
			result = LeaseResult{UserID: userID, Available: available.Available, ReservedTotal: available.Reserved, ErrorCode: errorCode}
			return nil
		}
		activeLeaseID, err := requiredRowText(row, "lease_id", "renew_lease")
		if err != nil {
			return err
		}
		amount, err := rowAmount(row, "reserved_amount", "renew_lease")
		if err != nil {
			return err
		}
		leaseRows, err := tx.Call(ctx, "get_credit_lease", userID, activeLeaseID)
		if err != nil {
			return err
		}
		lease, err := rowRequired(leaseRows, "get_credit_lease")
		if err != nil {
			return err
		}
		expiresAt, err := rowTime(lease, "expires_at", "get_credit_lease")
		if err != nil {
			return err
		}
		minimumBalance, err := rowAmount(lease, "minimum_balance", "get_credit_lease")
		if err != nil {
			return err
		}
		mode := BillingModeStrict
		if minimumBalance.IsNegative() {
			mode = BillingModeOverdraft
		}
		result = LeaseResult{LeaseID: activeLeaseID, UserID: userID, Amount: creditAmountPointer(amount), Available: available.Available, ReservedTotal: available.Reserved, MinimumBalance: creditAmountPointer(minimumBalance), BillingMode: mode, ExpiresAt: timePointer(expiresAt)}
		return nil
	})
	return result, err
}

// ExpireLeases runs a bounded server-side expiration batch.
func (s *PostgresStore) ExpireLeases(ctx context.Context, limit int) (result int, err error) {
	limit, err = requireBoundedLimit(limit, 100, maxMaintenanceBatchSize, "lease expiry limit")
	if err != nil {
		return 0, err
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, err := tx.Call(ctx, "expire_leases", limit)
		if err != nil {
			return err
		}
		row, err := rowRequired(rows, "expire_leases")
		if err != nil {
			return err
		}
		value, err := firstScalar(row, "expire_leases")
		if err != nil {
			return err
		}
		result, err = scalarInt(value, "expire_leases")
		return err
	})
	return result, err
}

// GetBucketBalances retrieves the server-calculated bucket breakdown ordered
// by priority. The RPC supplies the synthetic default bucket when applicable.
func (s *PostgresStore) GetBucketBalances(ctx context.Context, userID string) (result BucketBalancesResult, err error) {
	userID, err = requireText(userID, "user ID")
	if err != nil {
		return result, err
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, err := tx.Call(ctx, "get_credit_bucket_balances", userID)
		if err != nil {
			return err
		}
		buckets := make([]BucketBalance, 0, len(rows))
		total := DecimalZero
		for _, row := range rows {
			bucketKey, err := requiredRowText(row, "bucket_key", "get_credit_bucket_balances")
			if err != nil {
				return err
			}
			label, err := requiredRowText(row, "label", "get_credit_bucket_balances")
			if err != nil {
				return err
			}
			priority, err := rowInt(row, "priority", "get_credit_bucket_balances")
			if err != nil {
				return err
			}
			expires, err := rowBool(row, "expires", "get_credit_bucket_balances")
			if err != nil {
				return err
			}
			balance, err := rowAmount(row, "balance", "get_credit_bucket_balances")
			if err != nil {
				return err
			}
			buckets = append(buckets, BucketBalance{BucketKey: bucketKey, Label: label, Priority: priority, Expires: expires, Balance: balance})
			total = total.Add(balance)
		}
		result = BucketBalancesResult{UserID: userID, Buckets: buckets, TotalBalance: total}
		return nil
	})
	return result, err
}

// ExecuteGrantProgram executes an idempotent configured award event and
// returns every recipient outcome produced by the server-side program.
func (s *PostgresStore) ExecuteGrantProgram(ctx context.Context, request ExecuteGrantProgramRequest) (result []GrantProgramAwardResult, err error) {
	request.Trigger, err = requireText(request.Trigger, "grant trigger")
	if err != nil {
		return nil, err
	}
	request.ProgramKey, err = requireText(request.ProgramKey, "grant program key")
	if err != nil {
		return nil, err
	}
	request.SubjectID, err = requireText(request.SubjectID, "grant subject ID")
	if err != nil {
		return nil, err
	}
	request.EventKey, err = requireStableKey(request.EventKey, "grant event key")
	if err != nil {
		return nil, err
	}
	metadata, err := marshalMetadata(request.Metadata)
	if err != nil {
		return nil, err
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, err := tx.Call(ctx, "execute_grant_program", request.Trigger, request.ProgramKey, request.SubjectID, request.EventKey, nullableText(request.ReferrerSubjectID), nullableText(request.Region), metadata)
		if err != nil {
			return err
		}
		result = make([]GrantProgramAwardResult, 0, len(rows))
		for _, row := range rows {
			errorCode := optionalRowText(row, "error_code")
			var amount Amount
			if rowValue(row, "amount") != nil {
				amount, err = rowAmount(row, "amount", "execute_grant_program")
				if err != nil {
					return err
				}
			} else if errorCode == "" {
				return NewStoreError("execute_grant_program returned no amount", ErrorOptions{})
			}
			idempotent, err := rowBool(row, "replayed", "execute_grant_program")
			if err != nil {
				return err
			}
			result = append(result, GrantProgramAwardResult{
				GrantEventID:       optionalRowText(row, "grant_event_id"),
				GrantAwardID:       optionalRowText(row, "grant_award_id"),
				RecipientSubjectID: optionalRowText(row, "recipient_subject_id"),
				LedgerEntryID:      optionalRowText(row, "ledger_entry_id"),
				Amount:             amount,
				Idempotent:         idempotent,
				ErrorCode:          errorCode,
			})
		}
		return nil
	})
	return result, err
}

// SweepExpiredCredits asks PostgreSQL to identify or expire a bounded batch of
// credit lots. It never reconstructs balances in process memory.
func (s *PostgresStore) SweepExpiredCredits(ctx context.Context, dryRun bool, userID string, limit int) (result SweepResult, err error) {
	limit, err = requireBoundedLimit(limit, 100, maxMaintenanceBatchSize, "credit expiry limit")
	if err != nil {
		return result, err
	}
	if userID != "" {
		userID, err = requireText(userID, "user ID")
		if err != nil {
			return result, err
		}
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, err := tx.Call(ctx, "sweep_expired_lots", limit, nullableText(userID), dryRun)
		if err != nil {
			return err
		}
		row, err := rowRequired(rows, "sweep_expired_lots")
		if err != nil {
			return err
		}
		count, err := rowInt(row, "expired_count", "sweep_expired_lots")
		if err != nil {
			return err
		}
		amount, err := rowAmount(row, "expired_amount", "sweep_expired_lots")
		if err != nil {
			return err
		}
		byBucket, err := amountMap(rowValue(row, "expired_by_bucket"), "sweep_expired_lots.expired_by_bucket")
		if err != nil {
			return err
		}
		result = SweepResult{ExpiredCount: count, ExpiredAmount: amount, DryRun: dryRun, ExpiredByBucket: byBucket}
		return nil
	})
	return result, err
}

// RevokeCreditsByEntryType removes remaining lots attributable to an operation
// type through the database's LIFO revocation policy.
func (s *PostgresStore) RevokeCreditsByEntryType(ctx context.Context, userID, entryType string) (result RevokeCreditsResult, err error) {
	userID, err = requireText(userID, "user ID")
	if err != nil {
		return result, err
	}
	entryType, err = requireText(entryType, "entry type")
	if err != nil {
		return result, err
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, err := tx.Call(ctx, "revoke_subject_credits_by_operation", userID, entryType)
		if err != nil {
			return err
		}
		row, err := rowRequired(rows, "revoke_subject_credits_by_operation")
		if err != nil {
			return err
		}
		if errorCode := optionalRowText(row, "error_code"); errorCode != "" {
			return NewStoreError("revoke_subject_credits_by_operation failed: "+errorCode, ErrorOptions{Details: map[string]any{"error_code": errorCode}})
		}
		revoked, err := rowAmount(row, "revoked", "revoke_subject_credits_by_operation")
		if err != nil {
			return err
		}
		balanceAfter, err := rowAmount(row, "balance_after", "revoke_subject_credits_by_operation")
		if err != nil {
			return err
		}
		result = RevokeCreditsResult{UserID: userID, EntryType: entryType, Revoked: revoked, BalanceAfter: balanceAfter}
		return nil
	})
	return result, err
}

func catalogRevisionFromRow(row map[string]any, operation string) (CatalogRevision, error) {
	id, err := requiredRowText(row, "id", operation)
	if err != nil {
		return CatalogRevision{}, err
	}
	version, err := rowInt(row, "revision_no", operation)
	if err != nil {
		return CatalogRevision{}, err
	}
	config, err := jsonMap(rowValue(row, "source_document"), operation+".source_document")
	if err != nil {
		return CatalogRevision{}, err
	}
	if config == nil {
		return CatalogRevision{}, NewStoreError(operation+" returned an empty catalog document", ErrorOptions{})
	}
	return CatalogRevision{ID: id, Config: config, Version: version}, nil
}

func defaultCatalogRollout(rollout CatalogRollout) map[string]any {
	plans := make(map[string]any, len(rollout.Plans))
	for key, policy := range rollout.Plans {
		plans[key] = map[string]any{
			"effective":      policy.Effective,
			"include_pinned": policy.IncludePinned,
		}
	}
	return map[string]any{"plans": plans}
}

// GetActiveCatalog returns the tenant's active catalog revision, if any.
func (s *PostgresStore) GetActiveCatalog(ctx context.Context) (result *CatalogRevision, err error) {
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, err := tx.Call(ctx, "active_catalog_revision")
		if err != nil {
			return err
		}
		row := rowOptional(rows)
		if row == nil {
			return nil
		}
		revision, err := catalogRevisionFromRow(row, "active_catalog_revision")
		if err != nil {
			return err
		}
		result = &revision
		return nil
	})
	return result, err
}

// PublishAndActivateCatalog stores a validated catalog document and atomically
// moves the tenant's active revision to it.
func (s *PostgresStore) PublishAndActivateCatalog(ctx context.Context, config map[string]any, label string, rollout CatalogRollout) (result string, err error) {
	if config == nil {
		return "", errorf(ErrorCodeConfig, ErrorCategoryInvalidRequest, "catalog config is required")
	}
	if _, err = json.Marshal(config); err != nil {
		return "", NewError("catalog config must be JSON serializable", ErrorOptions{Code: ErrorCodeConfig, Category: ErrorCategoryInvalidRequest, Cause: err})
	}
	if _, err = json.Marshal(defaultCatalogRollout(rollout)); err != nil {
		return "", NewError("catalog rollout must be JSON serializable", ErrorOptions{Code: ErrorCodeConfig, Category: ErrorCategoryInvalidRequest, Cause: err})
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, err := tx.Call(ctx, "publish_and_activate_catalog", 1, config, nullableText(label), true, defaultCatalogRollout(rollout))
		if err != nil {
			return err
		}
		published, err := rowRequired(rows, "publish_and_activate_catalog")
		if err != nil {
			return err
		}
		version, err := rowInt(published, "revision_no", "publish_and_activate_catalog")
		if err != nil {
			return err
		}
		revisionRows, err := tx.Call(ctx, "catalog_revision_by_number", version)
		if err != nil {
			return err
		}
		revisionRow, err := rowRequired(revisionRows, "catalog_revision_by_number")
		if err != nil {
			return err
		}
		revision, err := catalogRevisionFromRow(revisionRow, "catalog_revision_by_number")
		if err != nil {
			return err
		}
		result = revision.ID
		return nil
	})
	return result, err
}

// PublishCatalogDraft stores an inactive catalog revision without changing the
// active catalog.
func (s *PostgresStore) PublishCatalogDraft(ctx context.Context, config map[string]any, label string) (result string, err error) {
	if config == nil {
		return "", errorf(ErrorCodeConfig, ErrorCategoryInvalidRequest, "catalog config is required")
	}
	if _, err = json.Marshal(config); err != nil {
		return "", NewError("catalog config must be JSON serializable", ErrorOptions{Code: ErrorCodeConfig, Category: ErrorCategoryInvalidRequest, Cause: err})
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, err := tx.Call(ctx, "publish_and_activate_catalog", 1, config, nullableText(label), false, map[string]any{"plans": map[string]any{}})
		if err != nil {
			return err
		}
		published, err := rowRequired(rows, "publish_and_activate_catalog")
		if err != nil {
			return err
		}
		version, err := rowInt(published, "revision_no", "publish_and_activate_catalog")
		if err != nil {
			return err
		}
		revisionRows, err := tx.Call(ctx, "catalog_revision_by_number", version)
		if err != nil {
			return err
		}
		revisionRow, err := rowRequired(revisionRows, "catalog_revision_by_number")
		if err != nil {
			return err
		}
		revision, err := catalogRevisionFromRow(revisionRow, "catalog_revision_by_number")
		if err != nil {
			return err
		}
		result = revision.ID
		return nil
	})
	return result, err
}

// GetCatalogHistory lists catalog revision summaries newest first.
func (s *PostgresStore) GetCatalogHistory(ctx context.Context) (result []CatalogRevisionSummary, err error) {
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, err := tx.Call(ctx, "list_catalog_revisions", 500)
		if err != nil {
			return err
		}
		result = make([]CatalogRevisionSummary, 0, len(rows))
		for _, row := range rows {
			id, err := requiredRowText(row, "id", "list_catalog_revisions")
			if err != nil {
				return err
			}
			version, err := rowInt(row, "revision_no", "list_catalog_revisions")
			if err != nil {
				return err
			}
			createdAt, err := rowTime(row, "created_at", "list_catalog_revisions")
			if err != nil {
				return err
			}
			result = append(result, CatalogRevisionSummary{
				ID:        id,
				Version:   version,
				Label:     optionalRowText(row, "label"),
				Active:    optionalRowText(row, "status") == "active",
				CreatedAt: createdAt,
			})
		}
		return nil
	})
	return result, err
}

// GetCatalogRevision reads a historical revision by version number.
func (s *PostgresStore) GetCatalogRevision(ctx context.Context, version int) (result *CatalogRevision, err error) {
	if version < 1 {
		return nil, errorf(ErrorCodeConfig, ErrorCategoryInvalidRequest, "catalog version must be positive")
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, err := tx.Call(ctx, "catalog_revision_by_number", version)
		if err != nil {
			return err
		}
		row := rowOptional(rows)
		if row == nil {
			return nil
		}
		revision, err := catalogRevisionFromRow(row, "catalog_revision_by_number")
		if err != nil {
			return err
		}
		result = &revision
		return nil
	})
	return result, err
}

// ActivateCatalogRevision switches the active catalog to a historical revision.
func (s *PostgresStore) ActivateCatalogRevision(ctx context.Context, version int, rollout CatalogRollout) (result string, err error) {
	if version < 1 {
		return "", errorf(ErrorCodeConfig, ErrorCategoryInvalidRequest, "catalog version must be positive")
	}
	if _, err = json.Marshal(defaultCatalogRollout(rollout)); err != nil {
		return "", NewError("catalog rollout must be JSON serializable", ErrorOptions{Code: ErrorCodeConfig, Category: ErrorCategoryInvalidRequest, Cause: err})
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, err := tx.Call(ctx, "activate_catalog_revision", version, defaultCatalogRollout(rollout))
		if err != nil {
			return err
		}
		row, err := rowRequired(rows, "activate_catalog_revision")
		if err != nil {
			return err
		}
		revision, err := catalogRevisionFromRow(row, "activate_catalog_revision")
		if err != nil {
			return err
		}
		result = revision.ID
		return nil
	})
	return result, err
}

func optionalRowInt(row map[string]any, key, operation string) (*int, error) {
	if rowValue(row, key) == nil {
		return nil, nil
	}
	value, err := rowInt(row, key, operation)
	if err != nil {
		return nil, err
	}
	return &value, nil
}

func entitlementMap(value any, operation string) (map[string]Entitlement, error) {
	mapped, err := jsonMap(value, operation+".entitlements")
	if err != nil {
		return nil, err
	}
	if mapped == nil {
		return map[string]Entitlement{}, nil
	}
	result := make(map[string]Entitlement, len(mapped))
	for feature, raw := range mapped {
		if document, ok := raw.(map[string]any); ok {
			result[feature] = Entitlement{Value: document["value"]}
			continue
		}
		if document, err := jsonMap(raw, operation+".entitlements."+feature); err == nil && document != nil {
			result[feature] = Entitlement{Value: document["value"]}
			continue
		}
		return nil, NewStoreError(operation+" returned an invalid entitlement", ErrorOptions{})
	}
	return result, nil
}

func admissionPolicy(row map[string]any, operation string) (*PlanAdmissionPolicy, error) {
	maxInFlight, err := optionalRowInt(row, "admission_max_in_flight", operation)
	if err != nil {
		return nil, err
	}
	operationsDocument, err := jsonMap(rowValue(row, "operation_admission"), operation+".operation_admission")
	if err != nil {
		return nil, err
	}
	if maxInFlight == nil && len(operationsDocument) == 0 {
		return nil, nil
	}
	operations := make(map[string]PlanAdmissionOperation, len(operationsDocument))
	for key, raw := range operationsDocument {
		policy, ok := raw.(map[string]any)
		if !ok {
			policy, err = jsonMap(raw, operation+".operation_admission."+key)
			if err != nil {
				return nil, err
			}
		}
		if policy == nil {
			return nil, NewStoreError(operation+" returned an invalid admission policy", ErrorOptions{})
		}
		limit, err := optionalRowInt(policy, "max_in_flight", operation)
		if err != nil {
			return nil, err
		}
		operations[key] = PlanAdmissionOperation{MaxInFlight: limit}
	}
	return &PlanAdmissionPolicy{MaxInFlight: maxInFlight, Operations: operations}, nil
}

func userPlanFromRow(userID string, row map[string]any) (GetUserPlanResult, error) {
	if row == nil {
		return GetUserPlanResult{UserID: userID, Entitlements: map[string]Entitlement{}, AllowedOperations: []string{}}, nil
	}
	allowanceAmount, err := optionalRowAmount(row, "credit_allowance_amount", "get_subject_plan")
	if err != nil {
		return GetUserPlanResult{}, err
	}
	var allowance *PlanAllowancePolicy
	if allowanceAmount != nil {
		priority, err := rowInt(row, "credit_allowance_priority", "get_subject_plan")
		if err != nil {
			return GetUserPlanResult{}, err
		}
		resetCount, err := rowInt(row, "credit_allowance_reset_count", "get_subject_plan")
		if err != nil {
			return GetUserPlanResult{}, err
		}
		resetUnit, err := requiredRowText(row, "credit_allowance_reset_unit", "get_subject_plan")
		if err != nil {
			return GetUserPlanResult{}, err
		}
		resetAnchor, err := requiredRowText(row, "credit_allowance_reset_anchor", "get_subject_plan")
		if err != nil {
			return GetUserPlanResult{}, err
		}
		resetTimezone, err := requiredRowText(row, "credit_allowance_reset_timezone", "get_subject_plan")
		if err != nil {
			return GetUserPlanResult{}, err
		}
		allowance = &PlanAllowancePolicy{Amount: *allowanceAmount, Priority: priority, ResetUnit: resetUnit, ResetCount: resetCount, ResetAnchor: resetAnchor, ResetTimezone: resetTimezone}
	}
	entitlements, err := entitlementMap(rowValue(row, "entitlements"), "get_subject_plan")
	if err != nil {
		return GetUserPlanResult{}, err
	}
	operations, err := stringSlice(rowValue(row, "allowed_operations"), "get_subject_plan.allowed_operations")
	if err != nil {
		return GetUserPlanResult{}, err
	}
	admission, err := admissionPolicy(row, "get_subject_plan")
	if err != nil {
		return GetUserPlanResult{}, err
	}
	var creditPolicy *PlanCreditPolicy
	if creditPolicyType := optionalRowText(row, "credit_policy_type"); creditPolicyType != "" {
		limit, err := optionalRowAmount(row, "credit_limit", "get_subject_plan")
		if err != nil {
			return GetUserPlanResult{}, err
		}
		creditPolicy = &PlanCreditPolicy{Type: creditPolicyType, CreditLimit: limit}
	}
	planAssignedAt, err := optionalRowTime(row, "plan_assigned_at", "get_subject_plan")
	if err != nil {
		return GetUserPlanResult{}, err
	}
	planEndsAt, err := optionalRowTime(row, "plan_assignment_ends_at", "get_subject_plan")
	if err != nil {
		return GetUserPlanResult{}, err
	}
	pinned, err := rowBool(row, "catalog_revision_pinned", "get_subject_plan")
	if err != nil {
		return GetUserPlanResult{}, err
	}
	version, err := optionalRowInt(row, "catalog_revision_no", "get_subject_plan")
	if err != nil {
		return GetUserPlanResult{}, err
	}
	return GetUserPlanResult{
		UserID:                optionalRowText(row, "user_id"),
		PlanID:                optionalRowText(row, "plan_id"),
		PlanKey:               optionalRowText(row, "plan_key"),
		PlanLabel:             optionalRowText(row, "plan_label"),
		Allowance:             allowance,
		Entitlements:          entitlements,
		RateCard:              optionalRowText(row, "rate_card"),
		CreditPolicy:          creditPolicy,
		Admission:             admission,
		AllowedOperations:     operations,
		PlanAssignedAt:        planAssignedAt,
		PlanAssignmentEndsAt:  planEndsAt,
		AssignmentSourceType:  optionalRowText(row, "assignment_source_type"),
		AssignmentSourceID:    optionalRowText(row, "assignment_source_id"),
		CatalogRevisionPinned: pinned,
		CatalogVersion:        version,
	}, nil
}

// GetUserPlan returns the subject's effective plan projection or a stable
// empty-plan result when no assignment exists.
func (s *PostgresStore) GetUserPlan(ctx context.Context, userID string) (result GetUserPlanResult, err error) {
	userID, err = requireText(userID, "user ID")
	if err != nil {
		return result, err
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, err := tx.Call(ctx, "get_subject_plan", userID)
		if err != nil {
			return err
		}
		result, err = userPlanFromRow(userID, rowOptional(rows))
		if result.UserID == "" {
			result.UserID = userID
		}
		return err
	})
	return result, err
}

// CheckFeature asks the public entitlement projection rather than inspecting
// config in memory, preserving the database's plan-revision semantics.
func (s *PostgresStore) CheckFeature(ctx context.Context, userID, feature string) (result CheckFeatureResult, err error) {
	userID, err = requireText(userID, "user ID")
	if err != nil {
		return result, err
	}
	feature, err = requireText(feature, "feature")
	if err != nil {
		return result, err
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, err := tx.Call(ctx, "get_subject_entitlements", userID)
		if err != nil {
			return err
		}
		result = CheckFeatureResult{UserID: userID, Feature: feature}
		for _, row := range rows {
			if optionalRowText(row, "feature_key") != feature {
				continue
			}
			value := rowValue(row, "feature_value")
			result.Value = value
			if value == nil {
				return nil
			}
			if boolean, ok := value.(bool); ok && !boolean {
				return nil
			}
			result.HasFeature = true
			return nil
		}
		return nil
	})
	return result, err
}

// SetUserPlan assigns a tenant subject to a catalog plan.
func (s *PostgresStore) SetUserPlan(ctx context.Context, userID, planKey string, options SetUserPlanOptions) (result SetUserPlanResult, err error) {
	userID, err = requireText(userID, "user ID")
	if err != nil {
		return result, err
	}
	planKey, err = requireText(planKey, "plan key")
	if err != nil {
		return result, err
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, err := tx.Call(ctx, "set_subject_plan", userID, planKey, nullableTime(options.PlanAssignedAt))
		if err != nil {
			return err
		}
		row, err := rowRequired(rows, "set_subject_plan")
		if err != nil {
			return err
		}
		assignedAt, err := rowTime(row, "plan_assigned_at", "set_subject_plan")
		if err != nil {
			return err
		}
		result = SetUserPlanResult{UserID: userID, PlanID: optionalRowText(row, "plan_id"), PlanKey: optionalRowText(row, "plan_key"), PlanAssignedAt: assignedAt, AssignmentState: optionalRowText(row, "assignment_state")}
		return nil
	})
	return result, err
}

// UnsetUserPlan removes an assignment through the public unassign RPC.
func (s *PostgresStore) UnsetUserPlan(ctx context.Context, userID string) (result UnsetUserPlanResult, err error) {
	userID, err = requireText(userID, "user ID")
	if err != nil {
		return result, err
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, err := tx.Call(ctx, "unassign_plan", userID, "sdk_unassignment")
		if err != nil {
			return err
		}
		row, err := rowRequired(rows, "unassign_plan")
		if err != nil {
			return err
		}
		value, err := firstScalar(row, "unassign_plan")
		if err != nil {
			return err
		}
		removed, err := scalarBool(value, "unassign_plan")
		if err != nil {
			return err
		}
		if !removed {
			return NewStoreError("unassign_plan returned false", ErrorOptions{})
		}
		result = UnsetUserPlanResult{UserID: userID}
		return nil
	})
	return result, err
}

// SetPlanRevisionPin pins or unpins the current assignment's catalog revision.
func (s *PostgresStore) SetPlanRevisionPin(ctx context.Context, userID string, pinned bool) (result bool, err error) {
	userID, err = requireText(userID, "user ID")
	if err != nil {
		return false, err
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, err := tx.Call(ctx, "set_plan_revision_pin", userID, pinned)
		if err != nil {
			return err
		}
		row, err := rowRequired(rows, "set_plan_revision_pin")
		if err != nil {
			return err
		}
		value, err := firstScalar(row, "set_plan_revision_pin")
		if err != nil {
			return err
		}
		result, err = scalarBool(value, "set_plan_revision_pin")
		return err
	})
	return result, err
}

// ApplyDuePlanChanges advances a bounded batch of scheduled plan changes.
func (s *PostgresStore) ApplyDuePlanChanges(ctx context.Context, limit int) (result int, err error) {
	limit, err = requireBoundedLimit(limit, 100, maxMaintenanceBatchSize, "plan change limit")
	if err != nil {
		return 0, err
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, err := tx.Call(ctx, "apply_due_plan_assignment_changes", limit)
		if err != nil {
			return err
		}
		row, err := rowRequired(rows, "apply_due_plan_assignment_changes")
		if err != nil {
			return err
		}
		value, err := firstScalar(row, "apply_due_plan_assignment_changes")
		if err != nil {
			return err
		}
		result, err = scalarInt(value, "apply_due_plan_assignment_changes")
		return err
	})
	return result, err
}

// StartPlanMigration creates a resumable server-side migration.
func (s *PostgresStore) StartPlanMigration(ctx context.Context, fromPlanID, toPlanID string) (result PlanMigrationStartResult, err error) {
	if fromPlanID != "" {
		fromPlanID, err = requireText(fromPlanID, "source plan ID")
		if err != nil {
			return result, err
		}
	}
	toPlanID, err = requireText(toPlanID, "target plan ID")
	if err != nil {
		return result, err
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, err := tx.Call(ctx, "start_plan_migration", nullableText(fromPlanID), toPlanID)
		if err != nil {
			return err
		}
		row, err := rowRequired(rows, "start_plan_migration")
		if err != nil {
			return err
		}
		value, err := firstScalar(row, "start_plan_migration")
		if err != nil {
			return err
		}
		migrationID, ok := textValue(value)
		if !ok {
			return NewStoreError("start_plan_migration returned an invalid migration ID", ErrorOptions{})
		}
		migrationID, err = normalizeTenantID(migrationID)
		if err != nil {
			return NewStoreError("start_plan_migration returned an invalid migration ID", ErrorOptions{Cause: err, Indeterminate: true})
		}
		result = PlanMigrationStartResult{MigrationID: migrationID}
		return nil
	})
	return result, err
}

// MigratePlanBatch advances one bounded migration batch.
func (s *PostgresStore) MigratePlanBatch(ctx context.Context, migrationID string, batchSize int) (result PlanMigrationBatchResult, err error) {
	migrationID, err = requireText(migrationID, "migration ID")
	if err != nil {
		return result, err
	}
	batchSize, err = requireBoundedLimit(batchSize, 100, maxMaintenanceBatchSize, "migration batch size")
	if err != nil {
		return result, err
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, err := tx.Call(ctx, "migrate_plan_batch", migrationID, batchSize)
		if err != nil {
			return err
		}
		row, err := rowRequired(rows, "migrate_plan_batch")
		if err != nil {
			return err
		}
		migrated, err := rowInt(row, "migrated", "migrate_plan_batch")
		if err != nil {
			return err
		}
		done, err := rowBool(row, "done", "migrate_plan_batch")
		if err != nil {
			return err
		}
		result = PlanMigrationBatchResult{Migrated: migrated, Done: done, NextCursor: optionalRowText(row, "next_cursor")}
		return nil
	})
	return result, err
}

// GetQuotaState retrieves current quota windows for a subject.
func (s *PostgresStore) GetQuotaState(ctx context.Context, userID, quotaKey string) (result []QuotaState, err error) {
	userID, err = requireText(userID, "user ID")
	if err != nil {
		return nil, err
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, err := tx.Call(ctx, "get_subject_quota_state", userID, nullableText(quotaKey))
		if err != nil {
			return err
		}
		result = make([]QuotaState, 0, len(rows))
		for _, row := range rows {
			limit, err := rowAmount(row, "quota_limit", "get_subject_quota_state")
			if err != nil {
				return err
			}
			consumed, err := rowAmount(row, "consumed", "get_subject_quota_state")
			if err != nil {
				return err
			}
			reserved, err := rowAmount(row, "reserved", "get_subject_quota_state")
			if err != nil {
				return err
			}
			remaining, err := rowAmount(row, "remaining", "get_subject_quota_state")
			if err != nil {
				return err
			}
			overage, err := rowAmount(row, "overage", "get_subject_quota_state")
			if err != nil {
				return err
			}
			windowStart, err := rowTime(row, "window_start", "get_subject_quota_state")
			if err != nil {
				return err
			}
			windowEnd, err := rowTime(row, "window_end", "get_subject_quota_state")
			if err != nil {
				return err
			}
			emitAt, err := floatSlice(rowValue(row, "emit_at_percent"), "get_subject_quota_state.emit_at_percent")
			if err != nil {
				return err
			}
			result = append(result, QuotaState{
				UserID:        optionalRowText(row, "user_id"),
				QuotaKey:      optionalRowText(row, "quota_key"),
				Operation:     optionalRowText(row, "operation_key"),
				Measure:       optionalRowText(row, "measure_key"),
				Limit:         limit,
				Consumed:      consumed,
				Reserved:      reserved,
				Remaining:     remaining,
				Overage:       overage,
				Enforcement:   optionalRowText(row, "enforcement"),
				WindowStart:   windowStart,
				WindowEnd:     windowEnd,
				EmitAtPercent: emitAt,
			})
		}
		return nil
	})
	return result, err
}

// CheckAllowance returns the database-owned active allowance window, if any.
func (s *PostgresStore) CheckAllowance(ctx context.Context, userID string) (result *AllowanceResult, err error) {
	userID, err = requireText(userID, "user ID")
	if err != nil {
		return nil, err
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, err := tx.Call(ctx, "get_subject_allowance", userID)
		if err != nil {
			return err
		}
		row := rowOptional(rows)
		if row == nil {
			return nil
		}
		remaining, err := rowAmount(row, "allowance_remaining", "get_subject_allowance")
		if err != nil {
			return err
		}
		periodStart, err := rowTime(row, "period_start", "get_subject_allowance")
		if err != nil {
			return err
		}
		periodEnd, err := rowTime(row, "period_end", "get_subject_allowance")
		if err != nil {
			return err
		}
		result = &AllowanceResult{PlanID: optionalRowText(row, "plan_id"), AllowanceRemaining: remaining, PeriodStart: periodStart, PeriodEnd: periodEnd}
		return nil
	})
	return result, err
}

// ListQuotaEvents returns a stable tenant-scoped history of quota events.
func (s *PostgresStore) ListQuotaEvents(ctx context.Context, userID string, options ListQuotaEventsOptions) (result []QuotaEvent, err error) {
	userID, err = requireText(userID, "user ID")
	if err != nil {
		return nil, err
	}
	limit, err := requireBoundedLimit(options.Limit, 100, maxPageSize, "quota event limit")
	if err != nil {
		return nil, err
	}
	if options.AfterID != "" && options.After == nil {
		return nil, errorf(ErrorCodeConfig, ErrorCategoryInvalidRequest, "quota event after ID requires an after timestamp")
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, err := tx.Call(ctx, "list_subject_quota_events", userID, nullableTime(options.After), limit, nullableText(options.IdempotencyKey), nullableText(options.AfterID))
		if err != nil {
			return err
		}
		result = make([]QuotaEvent, 0, len(rows))
		for _, row := range rows {
			createdAt, err := rowTime(row, "created_at", "list_subject_quota_events")
			if err != nil {
				return err
			}
			var threshold *float64
			if raw := rowValue(row, "threshold_percent"); raw != nil {
				values, err := floatSlice([]any{raw}, "list_subject_quota_events.threshold_percent")
				if err != nil {
					return err
				}
				threshold = &values[0]
			}
			result = append(result, QuotaEvent{
				EventID:          optionalRowText(row, "event_id"),
				QuotaKey:         optionalRowText(row, "quota_key"),
				Operation:        optionalRowText(row, "operation_key"),
				Measure:          optionalRowText(row, "measure_key"),
				EventType:        optionalRowText(row, "event_type"),
				ThresholdPercent: threshold,
				IdempotencyKey:   optionalRowText(row, "idempotency_key"),
				UsageChargeID:    optionalRowText(row, "usage_charge_id"),
				CreatedAt:        createdAt,
			})
		}
		return nil
	})
	return result, err
}

// RefundCredits posts an idempotent entry-scoped refund through the database.
func (s *PostgresStore) RefundCredits(ctx context.Context, entryID string, amount *Amount, reason string, metadata CreditMetadata, idempotencyKey string) (result RefundResult, err error) {
	entryID, err = requireText(entryID, "ledger entry ID")
	if err != nil {
		return result, err
	}
	idempotencyKey, err = requireStableKey(idempotencyKey, "refund idempotency key")
	if err != nil {
		return result, err
	}
	if amount != nil {
		if _, err = requirePositiveAmount(*amount, "refund"); err != nil {
			return result, err
		}
	}
	metadataJSON, err := marshalMetadata(metadata)
	if err != nil {
		return result, err
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		amountValue := any(nil)
		if amount != nil {
			amountValue = amountArgument(*amount)
		}
		rows, err := tx.Call(ctx, "refund_credit_by_entry", entryID, amountValue, idempotencyKey, nullableText(reason), metadataJSON)
		if err != nil {
			return err
		}
		row, err := rowRequired(rows, "refund_credit_by_entry")
		if err != nil {
			return err
		}
		errorCode := optionalRowText(row, "error_code")
		refunded, err := optionalRowAmount(row, "amount", "refund_credit_by_entry")
		if err != nil {
			return err
		}
		balanceAfter, err := optionalRowAmount(row, "balance_after", "refund_credit_by_entry")
		if err != nil {
			return err
		}
		result = RefundResult{
			RefundEntryID:   optionalRowText(row, "entry_id"),
			OriginalEntryID: entryID,
			UserID:          optionalRowText(row, "subject_id"),
			Amount:          refunded,
			NewBalance:      balanceAfter,
			ErrorCode:       errorCode,
		}
		if errorCode != "" {
			result.RefundEntryID = ""
		}
		return nil
	})
	return result, err
}

func requireTimeRange(start, end time.Time, operation string) error {
	if start.IsZero() || end.IsZero() {
		return errorf(ErrorCodeConfig, ErrorCategoryInvalidRequest, "%s requires start and end timestamps", operation)
	}
	if end.Before(start) {
		return errorf(ErrorCodeConfig, ErrorCategoryInvalidRequest, "%s end timestamp must not precede start", operation)
	}
	return nil
}

func rowTextFallback(row map[string]any, primary, fallback string) string {
	value := optionalRowText(row, primary)
	if value == "" && fallback != "" {
		return optionalRowText(row, fallback)
	}
	return value
}

// SpendByUser aggregates spend for a tenant-scoped time range.
func (s *PostgresStore) SpendByUser(ctx context.Context, start, end time.Time) (result []SpendByUserRow, err error) {
	if err = requireTimeRange(start, end, "spend by user"); err != nil {
		return nil, err
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, err := tx.Call(ctx, "spend_by_user", start.UTC(), end.UTC())
		if err != nil {
			return err
		}
		result = make([]SpendByUserRow, 0, len(rows))
		for _, row := range rows {
			amount, err := rowAmount(row, "total_spend", "spend_by_user")
			if err != nil {
				return err
			}
			count, err := rowInt(row, "charge_count", "spend_by_user")
			if err != nil {
				count, err = rowInt(row, "entry_count", "spend_by_user")
				if err != nil {
					return err
				}
			}
			result = append(result, SpendByUserRow{UserID: rowTextFallback(row, "subject_id", "user_id"), TotalSpend: amount, EntryCount: count})
		}
		return nil
	})
	return result, err
}

// SpendByModel aggregates spend for a tenant-scoped time range.
func (s *PostgresStore) SpendByModel(ctx context.Context, start, end time.Time) (result []SpendByModelRow, err error) {
	if err = requireTimeRange(start, end, "spend by model"); err != nil {
		return nil, err
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, err := tx.Call(ctx, "spend_by_model", start.UTC(), end.UTC())
		if err != nil {
			return err
		}
		result = make([]SpendByModelRow, 0, len(rows))
		for _, row := range rows {
			amount, err := rowAmount(row, "total_spend", "spend_by_model")
			if err != nil {
				return err
			}
			count, err := rowInt(row, "charge_count", "spend_by_model")
			if err != nil {
				count, err = rowInt(row, "entry_count", "spend_by_model")
				if err != nil {
					return err
				}
			}
			result = append(result, SpendByModelRow{Model: optionalRowText(row, "model"), TotalSpend: amount, EntryCount: count})
		}
		return nil
	})
	return result, err
}

// TopUsers returns the highest-spend subjects, preserving the canonical
// spend_by_user RPC ordering and tenant scope.
func (s *PostgresStore) TopUsers(ctx context.Context, limit int, start, end time.Time) (result []TopUserRow, err error) {
	if err = requireTimeRange(start, end, "top users"); err != nil {
		return nil, err
	}
	limit, err = requireBoundedLimit(limit, defaultPageSize, maxPageSize, "top user limit")
	if err != nil {
		return nil, err
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, err := tx.Call(ctx, "spend_by_user", start.UTC(), end.UTC())
		if err != nil {
			return err
		}
		if len(rows) > limit {
			rows = rows[:limit]
		}
		result = make([]TopUserRow, 0, len(rows))
		for _, row := range rows {
			amount, err := rowAmount(row, "total_spend", "spend_by_user")
			if err != nil {
				return err
			}
			result = append(result, TopUserRow{UserID: rowTextFallback(row, "subject_id", "user_id"), TotalSpend: amount})
		}
		return nil
	})
	return result, err
}

func rowCalendarDate(row map[string]any, key, operation string) (time.Time, error) {
	value := rowValue(row, key)
	if timestamp, ok := value.(time.Time); ok {
		return time.Date(timestamp.Year(), timestamp.Month(), timestamp.Day(), 0, 0, 0, 0, time.UTC), nil
	}
	text, ok := textValue(value)
	if !ok {
		return time.Time{}, NewStoreError(operation+" returned an invalid "+key, ErrorOptions{})
	}
	parsed, err := time.Parse("2006-01-02", text)
	if err != nil {
		return time.Time{}, NewStoreError(operation+" returned an invalid "+key, ErrorOptions{Cause: err})
	}
	return parsed.UTC(), nil
}

// DailySpend returns daily tenant-spend aggregates.
func (s *PostgresStore) DailySpend(ctx context.Context, start, end time.Time) (result []DailySpendRow, err error) {
	if err = requireTimeRange(start, end, "daily spend"); err != nil {
		return nil, err
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, err := tx.Call(ctx, "daily_spend", start.UTC(), end.UTC())
		if err != nil {
			return err
		}
		result = make([]DailySpendRow, 0, len(rows))
		for _, row := range rows {
			date, err := rowCalendarDate(row, "day", "daily_spend")
			if err != nil {
				date, err = rowCalendarDate(row, "date", "daily_spend")
				if err != nil {
					return err
				}
			}
			amount, err := rowAmount(row, "total_spend", "daily_spend")
			if err != nil {
				return err
			}
			count, err := rowInt(row, "charge_count", "daily_spend")
			if err != nil {
				count, err = rowInt(row, "entry_count", "daily_spend")
				if err != nil {
					return err
				}
			}
			result = append(result, DailySpendRow{Date: date, TotalSpend: amount, EntryCount: count})
		}
		return nil
	})
	return result, err
}

// AggregateStats returns the standard tenant-spend rollup.
func (s *PostgresStore) AggregateStats(ctx context.Context, start, end time.Time) (result AggregateStats, err error) {
	if err = requireTimeRange(start, end, "aggregate usage stats"); err != nil {
		return result, err
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, err := tx.Call(ctx, "aggregate_usage_stats", start.UTC(), end.UTC())
		if err != nil {
			return err
		}
		row, err := rowRequired(rows, "aggregate_usage_stats")
		if err != nil {
			return err
		}
		consumed, err := rowAmount(row, "total_credits_consumed", "aggregate_usage_stats")
		if err != nil {
			return err
		}
		activeUsers, err := rowInt(row, "active_users", "aggregate_usage_stats")
		if err != nil {
			return err
		}
		average, err := rowAmount(row, "avg_daily_spend", "aggregate_usage_stats")
		if err != nil {
			return err
		}
		result = AggregateStats{TotalCreditsConsumed: consumed, ActiveUsers: activeUsers, AverageDailySpend: average, TopModel: optionalRowText(row, "top_model"), TopUser: optionalRowText(row, "top_user")}
		return nil
	})
	return result, err
}

func ledgerEntryFromRow(row map[string]any, operation string) (LedgerEntry, error) {
	entryID, err := requiredRowText(row, "entry_id", operation)
	if err != nil {
		return LedgerEntry{}, err
	}
	accountID, err := requiredRowText(row, "account_id", operation)
	if err != nil {
		return LedgerEntry{}, err
	}
	amount, err := rowAmount(row, "amount", operation)
	if err != nil {
		return LedgerEntry{}, err
	}
	createdAt, err := rowTime(row, "created_at", operation)
	if err != nil {
		return LedgerEntry{}, err
	}
	metadata, err := jsonMap(rowValue(row, "metadata"), operation+".metadata")
	if err != nil {
		return LedgerEntry{}, err
	}
	return LedgerEntry{
		EntryID:          entryID,
		AccountID:        accountID,
		ActorUserID:      optionalRowText(row, "actor_user_id"),
		Amount:           amount,
		EntryType:        optionalRowText(row, "entry_type"),
		Operation:        optionalRowText(row, "operation"),
		ReferenceEntryID: optionalRowText(row, "reference_entry_id"),
		IdempotencyKey:   optionalRowText(row, "idempotency_key"),
		Metadata:         CreditMetadata(metadata),
		CreatedAt:        createdAt,
	}, nil
}

func validateLedgerOptions(options ListLedgerEntriesOptions) (int, error) {
	limit, err := requireBoundedLimit(options.Limit, defaultPageSize, maxLedgerPageSize, "ledger page limit")
	if err != nil {
		return 0, err
	}
	if options.To != nil && options.From != nil && options.To.Before(*options.From) {
		return 0, errorf(ErrorCodeConfig, ErrorCategoryInvalidRequest, "ledger end timestamp must not precede start")
	}
	return limit, nil
}

func (s *PostgresStore) listLedger(ctx context.Context, userID string, options ListLedgerEntriesOptions, usageOnly bool) (result LedgerPage, err error) {
	userID, err = requireText(userID, "user ID")
	if err != nil {
		return result, err
	}
	limit, err := validateLedgerOptions(options)
	if err != nil {
		return result, err
	}
	entryTypes := options.EntryTypes
	if usageOnly {
		entryTypes = []string{"usage"}
	}
	cursorAt := any(nil)
	cursorID := any(nil)
	if options.Cursor != nil {
		if options.Cursor.CreatedAt.IsZero() {
			return result, errorf(ErrorCodeConfig, ErrorCategoryInvalidRequest, "ledger cursor timestamp is required")
		}
		var cursorErr error
		cursorID, cursorErr = requireText(options.Cursor.EntryID, "ledger cursor entry ID")
		if cursorErr != nil {
			return result, cursorErr
		}
		cursorAt = options.Cursor.CreatedAt.UTC()
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, err := tx.Call(ctx, "list_ledger", userID, cursorAt, cursorID, limit+1, entryTypes, nullableTime(options.From), nullableTime(options.To), usageOnly)
		if err != nil {
			return err
		}
		hasMore := len(rows) > limit
		if hasMore {
			rows = rows[:limit]
		}
		items := make([]LedgerEntry, 0, len(rows))
		for _, row := range rows {
			item, err := ledgerEntryFromRow(row, "list_ledger")
			if err != nil {
				return err
			}
			items = append(items, item)
		}
		result = LedgerPage{Items: items}
		if hasMore && len(items) > 0 {
			last := items[len(items)-1]
			result.NextCursor = &LedgerCursor{CreatedAt: last.CreatedAt, EntryID: last.EntryID}
		}
		return nil
	})
	return result, err
}

// ListLedgerEntries lists subject-visible ledger entries with a stable cursor.
func (s *PostgresStore) ListLedgerEntries(ctx context.Context, userID string, options ListLedgerEntriesOptions) (LedgerPage, error) {
	return s.listLedger(ctx, userID, options, false)
}

// ListUsageEntries lists usage-only ledger entries with the canonical ledger cursor.
func (s *PostgresStore) ListUsageEntries(ctx context.Context, userID string, options ListLedgerEntriesOptions) (LedgerPage, error) {
	return s.listLedger(ctx, userID, options, true)
}

func usageChargeFromRow(row map[string]any, operation string) (UsageCharge, error) {
	usageID, err := requiredRowText(row, "usage_id", operation)
	if err != nil {
		return UsageCharge{}, err
	}
	accountID, err := requiredRowText(row, "account_id", operation)
	if err != nil {
		return UsageCharge{}, err
	}
	requested, err := rowAmount(row, "requested", operation)
	if err != nil {
		return UsageCharge{}, err
	}
	charged, err := rowAmount(row, "charged", operation)
	if err != nil {
		return UsageCharge{}, err
	}
	allowanceRequested, err := rowAmount(row, "allowance_requested", operation)
	if err != nil {
		return UsageCharge{}, err
	}
	allowanceCovered, err := rowAmount(row, "allowance_covered", operation)
	if err != nil {
		return UsageCharge{}, err
	}
	eventAt, err := rowTime(row, "event_at", operation)
	if err != nil {
		return UsageCharge{}, err
	}
	createdAt, err := rowTime(row, "created_at", operation)
	if err != nil {
		return UsageCharge{}, err
	}
	metadata, err := jsonMap(rowValue(row, "metadata"), operation+".metadata")
	if err != nil {
		return UsageCharge{}, err
	}
	return UsageCharge{
		UsageID:            usageID,
		AccountID:          accountID,
		Operation:          optionalRowText(row, "operation"),
		Requested:          requested,
		Charged:            charged,
		AllowanceRequested: allowanceRequested,
		AllowanceCovered:   allowanceCovered,
		BillingDisposition: optionalRowText(row, "billing_disposition"),
		Feature:            optionalRowText(row, "feature"),
		Model:              optionalRowText(row, "model"),
		Region:             optionalRowText(row, "region"),
		EventAt:            eventAt,
		IdempotencyKey:     optionalRowText(row, "idempotency_key"),
		Metadata:           CreditMetadata(metadata),
		CreatedAt:          createdAt,
	}, nil
}

// ListUsageCharges lists metered usage receipts, including allowance-covered
// and (optionally) record-only events.
func (s *PostgresStore) ListUsageCharges(ctx context.Context, userID string, options ListUsageChargesOptions) (result UsageChargePage, err error) {
	userID, err = requireText(userID, "user ID")
	if err != nil {
		return result, err
	}
	limit, err := requireBoundedLimit(options.Limit, defaultPageSize, maxLedgerPageSize, "usage charge page limit")
	if err != nil {
		return result, err
	}
	if options.To != nil && options.From != nil && options.To.Before(*options.From) {
		return result, errorf(ErrorCodeConfig, ErrorCategoryInvalidRequest, "usage charge end timestamp must not precede start")
	}
	cursorAt := any(nil)
	cursorID := any(nil)
	if options.Cursor != nil {
		if options.Cursor.EventAt.IsZero() {
			return result, errorf(ErrorCodeConfig, ErrorCategoryInvalidRequest, "usage charge cursor timestamp is required")
		}
		cursorID, err = requireText(options.Cursor.UsageID, "usage charge cursor ID")
		if err != nil {
			return result, err
		}
		cursorAt = options.Cursor.EventAt.UTC()
	}
	includeRecordOnly := true
	if options.IncludeRecordOnly != nil {
		includeRecordOnly = *options.IncludeRecordOnly
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, err := tx.Call(ctx, "list_usage_charges", userID, cursorAt, cursorID, limit+1, nullableTime(options.From), nullableTime(options.To), includeRecordOnly)
		if err != nil {
			return err
		}
		hasMore := len(rows) > limit
		if hasMore {
			rows = rows[:limit]
		}
		items := make([]UsageCharge, 0, len(rows))
		for _, row := range rows {
			item, err := usageChargeFromRow(row, "list_usage_charges")
			if err != nil {
				return err
			}
			items = append(items, item)
		}
		result = UsageChargePage{Items: items}
		if hasMore && len(items) > 0 {
			last := items[len(items)-1]
			result.NextCursor = &UsageChargeCursor{EventAt: last.EventAt, UsageID: last.UsageID}
		}
		return nil
	})
	return result, err
}

// GetLedgerEntry returns a ledger entry only when it belongs to userID.
func (s *PostgresStore) GetLedgerEntry(ctx context.Context, userID, entryID string) (result *LedgerEntry, err error) {
	userID, err = requireText(userID, "user ID")
	if err != nil {
		return nil, err
	}
	entryID, err = requireText(entryID, "ledger entry ID")
	if err != nil {
		return nil, err
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, err := tx.Call(ctx, "get_ledger_entry", userID, entryID)
		if err != nil {
			return err
		}
		row := rowOptional(rows)
		if row == nil {
			return nil
		}
		entry, err := ledgerEntryFromRow(row, "get_ledger_entry")
		if err != nil {
			return err
		}
		result = &entry
		return nil
	})
	return result, err
}

// CreateTeam creates an idempotent shared balance owned by ownerUserID.
func (s *PostgresStore) CreateTeam(ctx context.Context, ownerUserID, name string, options CreateTeamOptions) (result CreateTeamResult, err error) {
	ownerUserID, err = requireText(ownerUserID, "team owner user ID")
	if err != nil {
		return result, err
	}
	name, err = requireText(name, "team name")
	if err != nil {
		return result, err
	}
	idempotencyKey, err := requireStableKey(options.IdempotencyKey, "team creation idempotency key")
	if err != nil {
		return result, err
	}
	if _, err = requireNonNegativeAmount(options.InitialBalance, "team initial balance"); err != nil {
		return result, err
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, err := tx.Call(ctx, "create_team", ownerUserID, name, idempotencyKey, amountArgument(options.InitialBalance))
		if err != nil {
			return err
		}
		row, err := rowRequired(rows, "create_team")
		if err != nil {
			return err
		}
		teamID, err := requiredRowText(row, "team_id", "create_team")
		if err != nil {
			return err
		}
		teamName, err := requiredRowText(row, "name", "create_team")
		if err != nil {
			return err
		}
		idempotent, err := rowBool(row, "idempotent", "create_team")
		if err != nil {
			return err
		}
		result = CreateTeamResult{TeamID: teamID, Name: teamName, Idempotent: idempotent}
		return nil
	})
	return result, err
}

// GetTeamBalance returns nil for a missing team.
func (s *PostgresStore) GetTeamBalance(ctx context.Context, teamID string) (result *TeamBalanceResult, err error) {
	teamID, err = requireText(teamID, "team ID")
	if err != nil {
		return nil, err
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, err := tx.Call(ctx, "get_team_balance", teamID)
		if err != nil {
			return err
		}
		row := rowOptional(rows)
		if row == nil {
			return nil
		}
		balance, err := rowAmount(row, "balance", "get_team_balance")
		if err != nil {
			return err
		}
		memberCount, err := rowInt(row, "member_count", "get_team_balance")
		if err != nil {
			return err
		}
		result = &TeamBalanceResult{TeamID: optionalRowText(row, "team_id"), Name: optionalRowText(row, "name"), Balance: balance, MemberCount: memberCount}
		return nil
	})
	return result, err
}

// AddTeamMember assigns a role and optional per-member spending cap.
func (s *PostgresStore) AddTeamMember(ctx context.Context, teamID, userID string, options AddTeamMemberOptions) (result AddTeamMemberResult, err error) {
	teamID, err = requireText(teamID, "team ID")
	if err != nil {
		return result, err
	}
	userID, err = requireText(userID, "user ID")
	if err != nil {
		return result, err
	}
	role := options.Role
	if role == "" {
		role = TeamRoleMember
	}
	if role != TeamRoleOwner && role != TeamRoleAdmin && role != TeamRoleMember {
		return result, errorf(ErrorCodeConfig, ErrorCategoryInvalidRequest, "team role is invalid")
	}
	if options.SpendCap != nil && options.SpendCap.IsNegative() {
		return result, errorf(ErrorCodeConfig, ErrorCategoryInvalidRequest, "team spend cap must not be negative")
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		spendCap := any(nil)
		if options.SpendCap != nil {
			spendCap = amountArgument(*options.SpendCap)
		}
		rows, err := tx.Call(ctx, "set_team_member", teamID, userID, string(role), spendCap)
		if err != nil {
			return err
		}
		row, err := rowRequired(rows, "set_team_member")
		if err != nil {
			return err
		}
		value, err := firstScalar(row, "set_team_member")
		if err != nil {
			return err
		}
		added, err := scalarBool(value, "set_team_member")
		if err != nil {
			return err
		}
		if !added {
			return NewStoreError("set_team_member returned false", ErrorOptions{})
		}
		result = AddTeamMemberResult{TeamID: teamID, UserID: userID, Role: role}
		return nil
	})
	return result, err
}

// GetTeamMembers returns current membership and per-member usage totals.
func (s *PostgresStore) GetTeamMembers(ctx context.Context, teamID string) (result []TeamMember, err error) {
	teamID, err = requireText(teamID, "team ID")
	if err != nil {
		return nil, err
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, err := tx.Call(ctx, "list_team_members", teamID)
		if err != nil {
			return err
		}
		result = make([]TeamMember, 0, len(rows))
		for _, row := range rows {
			spendCap, err := optionalRowAmount(row, "spend_cap", "list_team_members")
			if err != nil {
				return err
			}
			totalSpent, err := rowAmount(row, "total_spent", "list_team_members")
			if err != nil {
				return err
			}
			role := TeamRole(optionalRowText(row, "role"))
			if role != TeamRoleOwner && role != TeamRoleAdmin && role != TeamRoleMember {
				return NewStoreError("list_team_members returned an invalid role", ErrorOptions{})
			}
			result = append(result, TeamMember{UserID: optionalRowText(row, "user_id"), Role: role, SpendCap: spendCap, TotalSpent: totalSpent})
		}
		return nil
	})
	return result, err
}

// RemoveTeamMember returns false when the member is absent or the final owner.
func (s *PostgresStore) RemoveTeamMember(ctx context.Context, teamID, userID string) (result bool, err error) {
	teamID, err = requireText(teamID, "team ID")
	if err != nil {
		return false, err
	}
	userID, err = requireText(userID, "user ID")
	if err != nil {
		return false, err
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, err := tx.Call(ctx, "remove_team_member", teamID, userID)
		if err != nil {
			return err
		}
		row, err := rowRequired(rows, "remove_team_member")
		if err != nil {
			return err
		}
		value, err := firstScalar(row, "remove_team_member")
		if err != nil {
			return err
		}
		result, err = scalarBool(value, "remove_team_member")
		return err
	})
	return result, err
}

// DeductTeam atomically charges a team balance on behalf of a member.
func (s *PostgresStore) DeductTeam(ctx context.Context, teamID, userID string, amount Amount, options TeamDeductionOptions) (result TeamDeductionResult, err error) {
	teamID, err = requireText(teamID, "team ID")
	if err != nil {
		return result, err
	}
	userID, err = requireText(userID, "user ID")
	if err != nil {
		return result, err
	}
	if _, err = requirePositiveAmount(amount, "team deduct"); err != nil {
		return result, err
	}
	idempotencyKey, err := requireStableKey(options.IdempotencyKey, "team deduct idempotency key")
	if err != nil {
		return result, err
	}
	operation, err := requireText(options.Operation, "team operation")
	if err != nil {
		return result, err
	}
	metadata, err := marshalMetadata(options.Metadata)
	if err != nil {
		return result, err
	}
	err = s.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, err := tx.Call(ctx, "deduct_team", teamID, userID, amountArgument(amount), idempotencyKey, operation, metadata)
		if err != nil {
			return err
		}
		row, err := rowRequired(rows, "deduct_team")
		if err != nil {
			return err
		}
		committedAmount, err := rowAmount(row, "amount", "deduct_team")
		if err != nil {
			return err
		}
		errorCode := optionalRowText(row, "error_code")
		var balanceAfter *Amount
		if rowValue(row, "balance_after") != nil {
			balanceAfter, err = optionalRowAmount(row, "balance_after", "deduct_team")
			if err != nil {
				return err
			}
		}
		idempotent, err := rowBool(row, "replayed", "deduct_team")
		if err != nil {
			return err
		}
		if errorCode == "" && !committedAmount.Equal(QuantizeMoney(amount)) {
			return NewStoreError("deduct_team committed amount differs from request", ErrorOptions{Indeterminate: true})
		}
		result = TeamDeductionResult{
			EntryID:          optionalRowText(row, "entry_id"),
			TeamID:           optionalRowText(row, "team_id"),
			UserID:           optionalRowText(row, "subject_id"),
			Amount:           QuantizeMoney(amount),
			TeamBalanceAfter: balanceAfter,
			Idempotent:       idempotent,
			ErrorCode:        errorCode,
		}
		return nil
	})
	return result, err
}
