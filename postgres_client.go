package bursar

import (
	"context"
	"errors"
	"fmt"
	"regexp"
	"strings"
	"sync"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
)

// PostgresAccessRole is the least-privileged database role used inside SDK
// transactions. bursar_client is selected automatically for tenant-bound
// clients; bursar_operator is reserved for operational work such as migration
// and maintenance tooling.
type PostgresAccessRole string

const (
	PostgresAccessRoleClient   PostgresAccessRole = "bursar_client"
	PostgresAccessRoleOperator PostgresAccessRole = "bursar_operator"
)

const (
	usageBackendPostgres   = "postgres"
	usageBackendClickHouse = "clickhouse"
	payloadBackendPostgres = "postgres"
	payloadBackendS3       = "s3"
)

// PostgresClientOptions controls transaction scoping and pool ownership. All
// database-backed SDK operations should use this boundary so tenancy and local
// configuration cannot be accidentally skipped.
type PostgresClientOptions struct {
	TenantID               string
	AccessRole             PostgresAccessRole
	UsageBackend           string
	BillingPayloadBackend  string
	ProviderEnvironment    ProviderEnvironment
	ConnectionTimeout      time.Duration
	StatementTimeout       time.Duration
	IdleTransactionTimeout time.Duration
	MaxConnections         int32
	ApplicationName        string
	OnPoolError            func(error)
}

func (o PostgresClientOptions) normalized() (PostgresClientOptions, error) {
	if o.TenantID != "" {
		tenantID, err := normalizeTenantID(o.TenantID)
		if err != nil {
			return PostgresClientOptions{}, err
		}
		o.TenantID = tenantID
	}
	if o.AccessRole == "" && o.TenantID != "" {
		o.AccessRole = PostgresAccessRoleClient
	}
	if o.AccessRole != "" && o.AccessRole != PostgresAccessRoleClient && o.AccessRole != PostgresAccessRoleOperator {
		return PostgresClientOptions{}, errorf(
			ErrorCodeConfig,
			ErrorCategoryInvalidRequest,
			"access role must be %q or %q",
			PostgresAccessRoleClient,
			PostgresAccessRoleOperator,
		)
	}
	if o.UsageBackend == "" {
		o.UsageBackend = usageBackendPostgres
	}
	if o.UsageBackend != usageBackendPostgres && o.UsageBackend != usageBackendClickHouse {
		return PostgresClientOptions{}, errorf(ErrorCodeConfig, ErrorCategoryInvalidRequest, "usage backend must be %q or %q", usageBackendPostgres, usageBackendClickHouse)
	}
	if o.BillingPayloadBackend == "" {
		o.BillingPayloadBackend = payloadBackendPostgres
	}
	if o.BillingPayloadBackend != payloadBackendPostgres && o.BillingPayloadBackend != payloadBackendS3 {
		return PostgresClientOptions{}, errorf(ErrorCodeConfig, ErrorCategoryInvalidRequest, "billing payload backend must be %q or %q", payloadBackendPostgres, payloadBackendS3)
	}
	if o.ProviderEnvironment == "" {
		o.ProviderEnvironment = ProviderEnvironmentLive
	}
	if err := o.ProviderEnvironment.Validate(); err != nil {
		return PostgresClientOptions{}, NewError("invalid provider environment", ErrorOptions{
			Code:     ErrorCodeConfig,
			Category: ErrorCategoryInvalidRequest,
			Cause:    err,
		})
	}
	if o.ConnectionTimeout == 0 {
		o.ConnectionTimeout = 10 * time.Second
	}
	if o.StatementTimeout == 0 {
		o.StatementTimeout = 30 * time.Second
	}
	if o.IdleTransactionTimeout == 0 {
		o.IdleTransactionTimeout = 30 * time.Second
	}
	for _, setting := range []struct {
		name  string
		value time.Duration
	}{
		{"connection timeout", o.ConnectionTimeout},
		{"statement timeout", o.StatementTimeout},
		{"idle transaction timeout", o.IdleTransactionTimeout},
	} {
		if setting.value < 0 {
			return PostgresClientOptions{}, errorf(ErrorCodeConfig, ErrorCategoryInvalidRequest, "%s must not be negative", setting.name)
		}
	}
	if o.MaxConnections == 0 {
		o.MaxConnections = 20
	}
	if o.MaxConnections < 1 {
		return PostgresClientOptions{}, errorf(ErrorCodeConfig, ErrorCategoryInvalidRequest, "max connections must be positive")
	}
	o.ApplicationName = strings.TrimSpace(o.ApplicationName)
	if o.ApplicationName == "" {
		o.ApplicationName = "bursar-go"
	}
	if strings.ContainsRune(o.ApplicationName, '\x00') {
		return PostgresClientOptions{}, errorf(ErrorCodeConfig, ErrorCategoryInvalidRequest, "application name must not contain a null byte")
	}
	return o, nil
}

// PostgresClient owns or borrows a pgx pool and gives every operation the same
// transactional RLS setup. Close only closes an owned pool.
type PostgresClient struct {
	pool    *pgxpool.Pool
	options PostgresClientOptions
	owned   bool

	mu     sync.RWMutex
	closed bool
}

// NewPostgresClient creates an SDK-owned pgx pool.
func NewPostgresClient(ctx context.Context, databaseURL string, options PostgresClientOptions) (*PostgresClient, error) {
	if strings.TrimSpace(databaseURL) == "" {
		return nil, errorf(ErrorCodeConfig, ErrorCategoryInvalidRequest, "database URL must not be empty")
	}
	normalized, err := options.normalized()
	if err != nil {
		return nil, err
	}
	config, err := pgxpool.ParseConfig(databaseURL)
	if err != nil {
		return nil, NewStoreError("invalid PostgreSQL connection configuration", ErrorOptions{Cause: err})
	}
	config.MaxConns = normalized.MaxConnections
	config.ConnConfig.ConnectTimeout = normalized.ConnectionTimeout
	if config.ConnConfig.RuntimeParams == nil {
		config.ConnConfig.RuntimeParams = make(map[string]string)
	}
	config.ConnConfig.RuntimeParams["application_name"] = normalized.ApplicationName
	pool, err := pgxpool.NewWithConfig(ctx, config)
	if err != nil {
		return nil, normalizePostgresError(err, "connect to PostgreSQL", false)
	}
	return &PostgresClient{pool: pool, options: normalized, owned: true}, nil
}

// NewPostgresClientFromPool binds an application-owned pool to the Bursar
// transaction boundary. Closing the resulting client never closes pool.
func NewPostgresClientFromPool(pool *pgxpool.Pool, options PostgresClientOptions) (*PostgresClient, error) {
	if pool == nil {
		return nil, errorf(ErrorCodeConfig, ErrorCategoryInvalidRequest, "PostgreSQL pool is required")
	}
	normalized, err := options.normalized()
	if err != nil {
		return nil, err
	}
	return &PostgresClient{pool: pool, options: normalized, owned: false}, nil
}

// TenantID is the validated tenant bound to transactions made by this client.
func (c *PostgresClient) TenantID() string {
	if c == nil {
		return ""
	}
	return c.options.TenantID
}

// OwnsPool reports whether Close will close the underlying pgx pool.
func (c *PostgresClient) OwnsPool() bool {
	return c != nil && c.owned
}

// Close releases an SDK-owned pool. Borrowed pools remain application-owned.
func (c *PostgresClient) Close() error {
	if c == nil {
		return nil
	}
	c.mu.Lock()
	if c.closed {
		c.mu.Unlock()
		return nil
	}
	c.closed = true
	pool := c.pool
	owned := c.owned
	c.mu.Unlock()
	if owned && pool != nil {
		pool.Close()
	}
	return nil
}

func (c *PostgresClient) acquire(ctx context.Context) (*pgxpool.Conn, error) {
	if c == nil {
		return nil, NewStoreError("PostgreSQL client is nil", ErrorOptions{Code: ErrorCodeStoreClosed})
	}
	c.mu.RLock()
	closed := c.closed
	pool := c.pool
	c.mu.RUnlock()
	if closed || pool == nil {
		return nil, NewError("PostgreSQL client has been closed", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	conn, err := pool.Acquire(ctx)
	if err != nil {
		classified := normalizePostgresError(err, "acquire PostgreSQL connection", false)
		c.notifyPoolError(classified)
		return nil, classified
	}
	return conn, nil
}

// PostgresTransaction is a configured transaction that can call Bursar's
// stable SQL RPCs. It is valid only inside WithTx's callback.
type PostgresTransaction struct {
	tx pgx.Tx
}

var postgresRPCNamePattern = regexp.MustCompile(`^[a-z][a-z0-9_]*$`)

// Call invokes one schema-local PostgreSQL function and returns its rows keyed
// by output-column name. Function names are validated before interpolation;
// values are always sent as pgx parameters.
func (tx *PostgresTransaction) Call(ctx context.Context, name string, arguments ...any) ([]map[string]any, error) {
	if tx == nil || tx.tx == nil {
		return nil, NewStoreError("PostgreSQL transaction is not active", ErrorOptions{Code: ErrorCodeStoreClosed})
	}
	if !postgresRPCNamePattern.MatchString(name) {
		return nil, errorf(ErrorCodeConfig, ErrorCategoryInvalidRequest, "invalid PostgreSQL RPC name")
	}
	placeholders := make([]string, len(arguments))
	for index := range arguments {
		placeholders[index] = fmt.Sprintf("$%d", index+1)
	}
	query := fmt.Sprintf("SELECT * FROM %s(%s)", name, strings.Join(placeholders, ", "))
	rows, err := tx.tx.Query(ctx, query, arguments...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	fieldDescriptions := rows.FieldDescriptions()
	result := make([]map[string]any, 0)
	for rows.Next() {
		values, err := rows.Values()
		if err != nil {
			return nil, err
		}
		row := make(map[string]any, len(fieldDescriptions))
		for index, field := range fieldDescriptions {
			row[string(field.Name)] = values[index]
		}
		result = append(result, row)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	return result, nil
}

// Query runs parameterized SQL inside a tenant-scoped transaction. It is
// intended for repository projections that cannot be represented by a stable
// database RPC; accounting mutations should use Call instead.
func (tx *PostgresTransaction) Query(ctx context.Context, query string, arguments ...any) ([]map[string]any, error) {
	if tx == nil || tx.tx == nil {
		return nil, NewStoreError("PostgreSQL transaction is not active", ErrorOptions{Code: ErrorCodeStoreClosed})
	}
	rows, err := tx.tx.Query(ctx, query, arguments...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	fields := rows.FieldDescriptions()
	result := make([]map[string]any, 0)
	for rows.Next() {
		values, err := rows.Values()
		if err != nil {
			return nil, err
		}
		row := make(map[string]any, len(fields))
		for index, field := range fields {
			row[string(field.Name)] = values[index]
		}
		result = append(result, row)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	return result, nil
}

// WithTx begins, configures, and commits one tenant-scoped transaction. If
// rollback fails, the checked-out connection is closed before release so a
// poisoned connection cannot be reused by the pool.
func (c *PostgresClient) WithTx(ctx context.Context, callback func(context.Context, *PostgresTransaction) error) (err error) {
	if callback == nil {
		return errorf(ErrorCodeConfig, ErrorCategoryInvalidRequest, "PostgreSQL transaction callback is required")
	}
	conn, err := c.acquire(ctx)
	if err != nil {
		return err
	}
	defer conn.Release()

	tx, err := conn.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return normalizePostgresError(err, "begin PostgreSQL transaction", false)
	}
	committed := false
	defer func() {
		if committed {
			return
		}
		if rollbackErr := tx.Rollback(context.Background()); rollbackErr != nil && !errors.Is(rollbackErr, pgx.ErrTxClosed) {
			_ = conn.Conn().Close(context.Background())
			if err == nil {
				err = normalizePostgresError(rollbackErr, "rollback PostgreSQL transaction", true)
			}
		}
	}()

	if err = c.configure(ctx, tx); err != nil {
		return normalizePostgresError(err, "configure PostgreSQL transaction", false)
	}
	if err = callback(ctx, &PostgresTransaction{tx: tx}); err != nil {
		return normalizePostgresError(err, "execute PostgreSQL transaction", true)
	}
	if err = tx.Commit(ctx); err != nil {
		return normalizePostgresError(err, "commit PostgreSQL transaction", true)
	}
	committed = true
	return nil
}

// Call executes a single PostgreSQL RPC in a configured transaction.
func (c *PostgresClient) Call(ctx context.Context, name string, arguments ...any) ([]map[string]any, error) {
	var rows []map[string]any
	err := c.WithTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		var err error
		rows, err = tx.Call(ctx, name, arguments...)
		return err
	})
	return rows, err
}

func (c *PostgresClient) configure(ctx context.Context, tx pgx.Tx) error {
	if c.options.AccessRole != "" {
		// AccessRole is validated during construction and never accepts arbitrary SQL.
		if _, err := tx.Exec(ctx, "SET LOCAL ROLE "+string(c.options.AccessRole)); err != nil {
			return err
		}
	}
	if _, err := tx.Exec(
		ctx,
		"SELECT set_config('statement_timeout', $1, true), set_config('idle_in_transaction_session_timeout', $2, true)",
		postgresTimeout(c.options.StatementTimeout),
		postgresTimeout(c.options.IdleTransactionTimeout),
	); err != nil {
		return err
	}
	if c.options.TenantID != "" {
		if _, err := tx.Exec(
			ctx,
			"SELECT set_config('bursar.tenant_id', $1, true), set_config('bursar.usage_backend', $2, true), set_config('bursar.billing_payload_backend', $3, true), set_config('bursar.provider_environment', $4, true)",
			c.options.TenantID,
			c.options.UsageBackend,
			c.options.BillingPayloadBackend,
			string(c.options.ProviderEnvironment),
		); err != nil {
			return err
		}
	}
	_, err := tx.Exec(ctx, "SET LOCAL search_path TO bursar, public")
	return err
}

func (c *PostgresClient) notifyPoolError(err error) {
	if c == nil || c.options.OnPoolError == nil {
		return
	}
	defer func() { _ = recover() }()
	c.options.OnPoolError(err)
}

func postgresTimeout(value time.Duration) string {
	return fmt.Sprintf("%d", value.Milliseconds())
}

func normalizeTenantID(value string) (string, error) {
	value = strings.ToLower(strings.TrimSpace(value))
	if len(value) != 36 {
		return "", errorf(ErrorCodeConfig, ErrorCategoryInvalidRequest, "tenant ID must be a UUID")
	}
	for index, character := range value {
		if index == 8 || index == 13 || index == 18 || index == 23 {
			if character != '-' {
				return "", errorf(ErrorCodeConfig, ErrorCategoryInvalidRequest, "tenant ID must be a UUID")
			}
			continue
		}
		if !(character >= '0' && character <= '9') && !(character >= 'a' && character <= 'f') {
			return "", errorf(ErrorCodeConfig, ErrorCategoryInvalidRequest, "tenant ID must be a UUID")
		}
	}
	return value, nil
}

func normalizePostgresError(err error, operation string, indeterminate bool) error {
	if err == nil {
		return nil
	}
	if bursarErr, ok := AsBursarError(err); ok {
		if indeterminate && bursarErr.Code == ErrorCodeStore && !bursarErr.Indeterminate {
			return NewStoreError(bursarErr.Message, ErrorOptions{
				Code:          bursarErr.Code,
				Category:      bursarErr.Category,
				Retryable:     bursarErr.Retryable,
				Indeterminate: true,
				Details:       bursarErr.Details,
				Cause:         bursarErr,
			})
		}
		return bursarErr
	}
	if errors.Is(err, context.DeadlineExceeded) {
		return NewStoreTimeoutError(operation+" timed out", err)
	}
	if errors.Is(err, context.Canceled) {
		return NewError(operation+" canceled", ErrorOptions{Code: ErrorCodeStore, Category: ErrorCategoryUnavailable, Cause: err})
	}
	var postgresErr *pgconn.PgError
	if errors.As(err, &postgresErr) {
		code := postgresErr.Code
		if code == "57014" {
			return NewStoreTimeoutError(operation+" timed out", err)
		}
		if strings.HasPrefix(code, "08") || code == "57P01" || code == "57P02" || code == "57P03" {
			return NewStoreUnavailableError(operation+" is unavailable", err)
		}
		return NewStoreError(operation+" failed", ErrorOptions{
			Cause:         err,
			Indeterminate: indeterminate,
			Details:       map[string]any{"postgres_code": code},
		})
	}
	return NewStoreError(operation+" failed", ErrorOptions{Cause: err, Indeterminate: indeterminate})
}
