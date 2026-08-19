// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

// Package runtime provides the Go SDK composition root for a tenant-scoped
// Bursar process. It wires the existing PostgreSQL authority to optional
// projections and the durable outbox without owning migrations or CLI state.
package runtime

import (
	"context"
	"errors"
	"fmt"
	"regexp"
	"strings"
	"sync"
	"time"

	bursar "github.com/Zonastery/bursar/golang/v2"
	"github.com/Zonastery/bursar/golang/v2/storage/clickhouse"
	"github.com/Zonastery/bursar/golang/v2/storage/s3"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"
)

var tenantSlugPattern = regexp.MustCompile(`^[a-z0-9]+(?:[a-z0-9-]*[a-z0-9])?$`)

// Options configures a complete tenant runtime. The primary connection is
// used for all financial and tenant-scoped operations. The operator
// connection is deliberately separate and unscoped.
type Options struct {
	TenantID            string
	TenantSlug          string
	ProviderEnvironment bursar.ProviderEnvironment
	DatabaseURL         string
	Pool                *pgxpool.Pool
	OperatorDatabaseURL string
	OperatorPool        *pgxpool.Pool
	Postgres            bursar.PostgresStoreOptions
	Operator            bursar.PostgresClientOptions
	CreditsOptions      bursar.CreditsServiceOptions
	Emitter             bursar.CreditEventSink
	BillingOptions      *bursar.BillingServiceOptions
	CommerceOptions     *bursar.CommerceOptions
	UsageSink           bursar.UsageEventSink
	ClickHouse          *clickhouse.Options
	BillingArchive      bursar.BillingPayloadArchive
	S3                  *s3.Options
	Outbox              *bursar.OutboxWorkerOptions
	DisableOutbox       bool
	Dependencies        *Dependencies
}

// Dependencies permits deterministic composition tests and applications that
// already own a facade. Supplied components and the facade are borrowed: they
// may be started or flushed but are never closed by Runtime. If a later
// borrowed component fails to start, its owner remains responsible for final
// shutdown.
type Dependencies struct {
	Bursar               *bursar.Bursar
	Recovery             bursar.OutboxRecoveryStore
	ProjectionRepository bursar.StorageProjectionRepository
	Maintenance          *bursar.BursarMaintenance
	OperatorMaintenance  *bursar.BursarOperatorMaintenance
	Worker               *bursar.OutboxWorker
	Components           []bursar.RuntimeComponent
}

// StartOptions controls catalog loading and its bounded retry policy. A nil
// LoadCatalog means true. A nil Retry uses Bursar's standard retry defaults.
type StartOptions struct {
	LoadCatalog *bool
	Retry       *bursar.BursarRetryOptions
}

// RuntimeHealth is a safe readiness snapshot with no credentials or SQL.
type RuntimeHealth struct {
	Ready           bool
	FinancialReady  bool
	ProjectionReady bool
	Degraded        bool
	Started         bool
	Closed          bool
	CatalogLoaded   bool
}

// RuntimeState records lifecycle state and the configured dependency count.
type RuntimeState struct {
	Started        bool
	Closed         bool
	CatalogLoaded  bool
	Components     int
	DependenciesOK bool
}

// DependencyCheck is one best-effort readiness check.
type DependencyCheck struct {
	Name    string
	OK      bool
	Latency time.Duration
	Error   string
	Skipped bool
}

// Diagnostics combines state with dependency outcomes.
type Diagnostics struct {
	CheckedAt  time.Time
	Health     RuntimeHealth
	Catalog    DependencyCheck
	Components []DependencyCheck
}

type runtimeComponent struct {
	component bursar.RuntimeComponent
	owned     bool
}

// Runtime owns the SDK-created stores, adapters, handlers, and worker. Caller
// supplied pools are borrowed by the underlying Postgres clients and remain
// open after Close.
type Runtime struct {
	Bursar               *bursar.Bursar
	PrimaryStore         *bursar.PostgresStore
	OperatorClient       *bursar.PostgresClient
	Recovery             bursar.OutboxRecoveryStore
	projectionRepository bursar.StorageProjectionRepository
	Maintenance          *bursar.BursarMaintenance
	OperatorMaintenance  *bursar.BursarOperatorMaintenance
	Worker               *bursar.OutboxWorker
	UsageSink            bursar.UsageEventSink
	BillingArchive       bursar.BillingPayloadArchive

	components []runtimeComponent
	mu         sync.Mutex
	startMu    sync.Mutex
	started    bool
	closed     bool
	closeErr   error
	startErr   error
	ownsBursar bool
	tenantID   string
	tenantSlug string
}

// NewBursarRuntime composes the complete runtime from database URLs or
// caller-owned pools. Dependencies mode is intended for deterministic tests
// and advanced hosts that construct their own facade.
func NewBursarRuntime(ctx context.Context, options Options) (*Runtime, error) {
	if ctx == nil {
		return nil, errors.New("runtime context is required")
	}
	if options.Dependencies != nil {
		return newDependencyRuntime(options)
	}
	tenantID, tenantSlug, environment, err := normalizeOptions(&options)
	if err != nil {
		return nil, err
	}
	usageSink, archive, projectionComponents, err := prepareProjectionPorts(options, tenantID)
	if err != nil {
		return nil, err
	}
	cleanupProjections := func() {
		for index := len(projectionComponents) - 1; index >= 0; index-- {
			if projectionComponents[index].owned {
				_ = projectionComponents[index].component.Close(context.Background())
			}
		}
	}
	usageBackend := "postgres"
	if usageSink != nil {
		usageBackend = "clickhouse"
	}
	payloadBackend := "postgres"
	if archive != nil {
		payloadBackend = "s3"
	}

	primaryOptions := options.Postgres
	primaryOptions.TenantID = tenantID
	primaryOptions.ProviderEnvironment = environment
	primaryOptions.Pool = options.Pool
	primaryOptions.AccessRole = bursar.PostgresAccessRoleClient
	primaryOptions.UsageBackend = usageBackend
	primaryOptions.BillingPayloadBackend = payloadBackend
	primary, err := bursar.NewPostgresStore(ctx, strings.TrimSpace(options.DatabaseURL), primaryOptions)
	if err != nil {
		cleanupProjections()
		return nil, fmt.Errorf("create primary PostgreSQL store: %w", err)
	}

	operatorOptions := options.Operator
	operatorOptions.TenantID = ""
	operatorOptions.AccessRole = bursar.PostgresAccessRoleOperator
	operatorOptions.ProviderEnvironment = environment
	operatorOptions.UsageBackend = usageBackend
	operatorOptions.BillingPayloadBackend = payloadBackend
	operator, err := newOperatorClient(ctx, strings.TrimSpace(options.OperatorDatabaseURL), options.OperatorPool, operatorOptions)
	if err != nil {
		_ = primary.Close()
		cleanupProjections()
		return nil, fmt.Errorf("create operator PostgreSQL client: %w", err)
	}
	creditsOptions := options.CreditsOptions
	if analytics, ok := usageSink.(bursar.UsageAnalyticsStore); ok {
		if creditsOptions.Analytics != nil {
			_ = operator.Close()
			_ = primary.Close()
			cleanupProjections()
			return nil, errors.New("configure usage analytics through the runtime usage sink")
		}
		creditsOptions.Analytics = analytics
	}
	if usageStore, ok := usageSink.(bursar.UsageChargeStore); ok {
		if creditsOptions.UsageStore != nil {
			_ = operator.Close()
			_ = primary.Close()
			cleanupProjections()
			return nil, errors.New("configure usage receipts through the runtime usage sink")
		}
		creditsOptions.UsageStore = usageStore
	}

	facade, err := bursar.New(bursar.Options{
		CreditStore:     primary,
		CreditsOptions:  creditsOptions,
		Emitter:         options.Emitter,
		BillingStore:    primary,
		BillingOptions:  options.BillingOptions,
		CommerceOptions: options.CommerceOptions,
	})
	if err != nil {
		_ = operator.Close()
		_ = primary.Close()
		cleanupProjections()
		return nil, fmt.Errorf("create Bursar facade: %w", err)
	}

	recovery, err := bursar.NewPostgresStorageRepositoryFromStore(primary)
	if err != nil {
		_ = operator.Close()
		_ = primary.Close()
		cleanupProjections()
		return nil, fmt.Errorf("create storage repository: %w", err)
	}
	projectionRepository, ok := any(recovery).(bursar.StorageProjectionRepository)
	if !ok {
		_ = operator.Close()
		_ = primary.Close()
		cleanupProjections()
		return nil, errors.New("storage repository does not implement projection export")
	}
	maintenance, err := newMaintenance(primary, facade)
	if err != nil {
		_ = operator.Close()
		_ = primary.Close()
		cleanupProjections()
		return nil, fmt.Errorf("create maintenance: %w", err)
	}
	operatorMaintenance, err := bursar.NewBursarOperatorMaintenance(operator)
	if err != nil {
		_ = operator.Close()
		_ = primary.Close()
		cleanupProjections()
		return nil, fmt.Errorf("create operator maintenance: %w", err)
	}

	runtime := &Runtime{
		Bursar: facade, PrimaryStore: primary, OperatorClient: operator,
		Recovery: recovery, Maintenance: maintenance, OperatorMaintenance: operatorMaintenance,
		projectionRepository: projectionRepository, UsageSink: usageSink, BillingArchive: archive,
		components: projectionComponents, ownsBursar: true, tenantID: tenantID, tenantSlug: tenantSlug,
	}
	if err := runtime.addProjectionWorker(options, recovery, projectionRepository, usageSink, archive); err != nil {
		_ = runtime.Close(context.Background())
		return nil, err
	}
	return runtime, nil
}

// New is an alias for NewBursarRuntime.
func New(ctx context.Context, options Options) (*Runtime, error) {
	return NewBursarRuntime(ctx, options)
}

// TenantID returns the validated tenant UUID bound to this runtime.
func (r *Runtime) TenantID() string {
	if r == nil {
		return ""
	}
	return r.tenantID
}

// TenantSlug returns the optional normalized tenant slug.
func (r *Runtime) TenantSlug() string {
	if r == nil {
		return ""
	}
	return r.tenantSlug
}

func newDependencyRuntime(options Options) (*Runtime, error) {
	if options.Dependencies.Bursar == nil {
		return nil, errors.New("runtime dependencies require Bursar")
	}
	if strings.TrimSpace(options.TenantID) == "" {
		return nil, errors.New("runtime dependencies require a tenant ID")
	}
	tenantID, tenantSlug, _, err := normalizeIdentity(&options)
	if err != nil {
		return nil, err
	}
	r := &Runtime{Bursar: options.Dependencies.Bursar, Recovery: options.Dependencies.Recovery,
		projectionRepository: options.Dependencies.ProjectionRepository,
		Maintenance:          options.Dependencies.Maintenance, OperatorMaintenance: options.Dependencies.OperatorMaintenance,
		Worker: options.Dependencies.Worker, tenantID: tenantID, tenantSlug: tenantSlug}
	for _, component := range options.Dependencies.Components {
		if component == nil {
			return nil, errors.New("runtime dependency component must not be nil")
		}
		r.components = append(r.components, runtimeComponent{component: component})
	}
	if r.Worker != nil {
		r.components = append(r.components, runtimeComponent{component: r.Worker})
	}
	return r, nil
}

func normalizeOptions(options *Options) (string, string, bursar.ProviderEnvironment, error) {
	tenantID, tenantSlug, environment, err := normalizeIdentity(options)
	if err != nil {
		return "", "", "", err
	}
	if strings.TrimSpace(options.DatabaseURL) == "" && options.Pool == nil {
		return "", "", "", errors.New("primary database URL or pool is required")
	}
	if strings.TrimSpace(options.OperatorDatabaseURL) == "" && options.OperatorPool == nil {
		return "", "", "", errors.New("operator database URL or pool is required")
	}
	if options.Pool != nil && options.OperatorPool != nil && options.Pool == options.OperatorPool {
		return "", "", "", errors.New("primary and operator PostgreSQL pools must be distinct")
	}
	primaryURL := strings.TrimSpace(options.DatabaseURL)
	operatorURL := strings.TrimSpace(options.OperatorDatabaseURL)
	if primaryURL != "" && operatorURL != "" && primaryURL == operatorURL {
		return "", "", "", errors.New("primary and operator database URLs must be distinct")
	}
	return tenantID, tenantSlug, environment, nil
}

func normalizeIdentity(options *Options) (string, string, bursar.ProviderEnvironment, error) {
	tenantID, err := uuid.Parse(strings.TrimSpace(options.TenantID))
	if err != nil {
		return "", "", "", fmt.Errorf("tenant ID must be a UUID: %w", err)
	}
	tenantSlug := strings.ToLower(strings.TrimSpace(options.TenantSlug))
	if tenantSlug != "" && (len(tenantSlug) > 100 || !tenantSlugPattern.MatchString(tenantSlug)) {
		return "", "", "", errors.New("tenant slug must be 1-100 lowercase alphanumeric or hyphen characters")
	}
	environment := options.ProviderEnvironment
	if environment == "" {
		environment = bursar.ProviderEnvironmentLive
	}
	if err := environment.Validate(); err != nil {
		return "", "", "", fmt.Errorf("provider environment: %w", err)
	}
	return tenantID.String(), tenantSlug, environment, nil
}

func newOperatorClient(ctx context.Context, databaseURL string, pool *pgxpool.Pool, options bursar.PostgresClientOptions) (*bursar.PostgresClient, error) {
	if pool != nil {
		if databaseURL != "" {
			return nil, errors.New("operator database URL and pool are mutually exclusive")
		}
		return bursar.NewPostgresClientFromPool(pool, options)
	}
	return bursar.NewPostgresClient(ctx, databaseURL, options)
}

func newMaintenance(store *bursar.PostgresStore, facade *bursar.Bursar) (*bursar.BursarMaintenance, error) {
	var expireGrace func(context.Context, time.Time) (int, error)
	if facade.Billing != nil {
		expireGrace = func(ctx context.Context, now time.Time) (int, error) {
			return facade.Billing.ExpirePastDueGracePeriods(ctx, now, 100)
		}
	}
	return bursar.NewBursarMaintenanceFromStore(store, expireGrace)
}

func prepareProjectionPorts(options Options, tenantID string) (bursar.UsageEventSink, bursar.BillingPayloadArchive, []runtimeComponent, error) {
	usageSink := options.UsageSink
	if usageSink != nil && options.ClickHouse != nil {
		return nil, nil, nil, errors.New("usage sink and ClickHouse options are mutually exclusive")
	}
	components := make([]runtimeComponent, 0, 2)
	if options.ClickHouse != nil {
		config := *options.ClickHouse
		if strings.TrimSpace(config.TenantID) == "" {
			config.TenantID = tenantID
		} else if config.TenantID != tenantID {
			return nil, nil, nil, errors.New("ClickHouse tenant ID must match runtime tenant ID")
		}
		store, err := clickhouse.NewUsageStore(config)
		if err != nil {
			return nil, nil, nil, fmt.Errorf("create ClickHouse usage store: %w", err)
		}
		usageSink = store
		components = append(components, runtimeComponent{component: store, owned: true})
	} else if component, ok := usageSink.(bursar.RuntimeComponent); ok {
		components = append(components, runtimeComponent{component: component})
	}

	archive := options.BillingArchive
	if archive != nil && options.S3 != nil {
		for index := len(components) - 1; index >= 0; index-- {
			if components[index].owned {
				_ = components[index].component.Close(context.Background())
			}
		}
		return nil, nil, nil, errors.New("billing archive and S3 options are mutually exclusive")
	}
	if options.S3 != nil {
		archiveValue, err := s3.NewBillingArchive(*options.S3)
		if err != nil {
			for index := len(components) - 1; index >= 0; index-- {
				if components[index].owned {
					_ = components[index].component.Close(context.Background())
				}
			}
			return nil, nil, nil, fmt.Errorf("create S3 billing archive: %w", err)
		}
		archive = archiveValue
		components = append(components, runtimeComponent{component: archiveValue, owned: true})
	} else if component, ok := archive.(bursar.RuntimeComponent); ok {
		components = append(components, runtimeComponent{component: component})
	}
	return usageSink, archive, components, nil
}

func (r *Runtime) addProjectionWorker(options Options, recovery bursar.OutboxRecoveryStore, projectionRepository bursar.StorageProjectionRepository, usageSink bursar.UsageEventSink, archive bursar.BillingPayloadArchive) error {
	if options.DisableOutbox || (usageSink == nil && archive == nil) {
		return nil
	}
	if projectionRepository == nil || recovery == nil {
		return errors.New("projection and recovery repositories are required when configuring the outbox")
	}
	handlers := make([]bursar.OutboxHandler, 0, 2)
	if usageSink != nil {
		handler, err := bursar.NewUsageChargeOutboxHandler(projectionRepository, usageSink)
		if err != nil {
			return fmt.Errorf("create usage outbox handler: %w", err)
		}
		handlers = append(handlers, handler)
	}
	if archive != nil {
		handler, err := bursar.NewBillingPayloadOutboxHandler(projectionRepository, archive)
		if err != nil {
			return fmt.Errorf("create billing outbox handler: %w", err)
		}
		handlers = append(handlers, handler)
	}
	workerOptions := bursar.OutboxWorkerOptions{}
	if options.Outbox != nil {
		workerOptions = *options.Outbox
	}
	worker, err := bursar.NewOutboxWorker(recovery, handlers, workerOptions)
	if err != nil {
		return fmt.Errorf("create outbox worker: %w", err)
	}
	r.Worker = worker
	r.components = append(r.components, runtimeComponent{component: worker, owned: true})
	return nil
}

// Start validates lifecycle state, loads the catalog with bounded retry, and
// starts projections and the outbox worker. Failed starts are rolled back.
func (r *Runtime) Start(ctx context.Context, options ...StartOptions) error {
	if r == nil || r.Bursar == nil {
		return errors.New("runtime is not initialized")
	}
	if ctx == nil {
		return errors.New("runtime context is required")
	}
	r.startMu.Lock()
	defer r.startMu.Unlock()
	r.mu.Lock()
	if r.closed {
		r.mu.Unlock()
		return errors.New("runtime is closed")
	}
	if r.started {
		r.mu.Unlock()
		return nil
	}
	if r.startErr != nil {
		err := r.startErr
		r.mu.Unlock()
		return fmt.Errorf("runtime startup previously failed: %w", err)
	}
	r.mu.Unlock()
	startOptions := StartOptions{}
	if len(options) > 1 {
		return errors.New("runtime accepts at most one start options value")
	}
	if len(options) == 1 {
		startOptions = options[0]
	}
	if err := r.verifyTenantIdentity(ctx); err != nil {
		return err
	}
	loadCatalog := true
	if startOptions.LoadCatalog != nil {
		loadCatalog = *startOptions.LoadCatalog
	}
	if loadCatalog {
		retry := bursar.DefaultBursarRetryOptions()
		if startOptions.Retry != nil {
			retry = *startOptions.Retry
		}
		if _, err := bursar.RetryBursarOperation(ctx, func(ctx context.Context) (struct{}, error) {
			return struct{}{}, r.Bursar.LoadCatalog(ctx)
		}, retry); err != nil {
			return err
		}
	}
	started := 0
	for index, component := range r.components {
		if err := component.component.Start(ctx); err != nil {
			if component.owned {
				_ = component.component.Close(context.Background())
			}
			for rollback := index - 1; rollback >= 0; rollback-- {
				_ = r.components[rollback].component.Flush(ctx)
				if r.components[rollback].owned {
					_ = r.components[rollback].component.Close(context.Background())
				}
			}
			r.mu.Lock()
			r.startErr = err
			r.mu.Unlock()
			return err
		}
		started++
	}
	r.mu.Lock()
	if r.closed {
		r.mu.Unlock()
		for rollback := started - 1; rollback >= 0; rollback-- {
			if r.components[rollback].owned {
				_ = r.components[rollback].component.Close(context.Background())
			}
		}
		return errors.New("runtime closed during startup")
	}
	r.started = true
	r.mu.Unlock()
	return nil
}

func (r *Runtime) verifyTenantIdentity(ctx context.Context) error {
	if r.tenantSlug == "" {
		return nil
	}
	if r.OperatorClient == nil {
		return errors.New("tenant slug verification requires an operator PostgreSQL client")
	}
	rows, err := r.OperatorClient.Query(ctx, "SELECT bursar.resolve_active_tenant_for_trigger($1)::text AS tenant_id", r.tenantSlug)
	if err != nil {
		return fmt.Errorf("verify tenant slug: %w", err)
	}
	if len(rows) != 1 {
		return errors.New("tenant slug resolver returned no tenant")
	}
	resolved := strings.TrimSpace(fmt.Sprint(rows[0]["tenant_id"]))
	if resolved != r.tenantID {
		return fmt.Errorf("tenant slug %q resolves to a different tenant ID", r.tenantSlug)
	}
	return nil
}

// Health reports readiness. A configured projection is ready only when its
// component health check succeeds.
func (r *Runtime) Health(ctx context.Context) RuntimeHealth {
	if r == nil {
		return RuntimeHealth{Closed: true}
	}
	state := r.State()
	projectionReady := state.Started && !state.Closed
	if projectionReady {
		for _, component := range r.components {
			checker, ok := component.component.(bursar.RuntimeHealthChecker)
			if !ok {
				continue
			}
			if err := checker.Health(ctx); err != nil {
				projectionReady = false
			}
		}
	}
	financialReady := state.Started && state.CatalogLoaded && !state.Closed
	return RuntimeHealth{
		Ready:           financialReady && projectionReady,
		FinancialReady:  financialReady,
		ProjectionReady: projectionReady,
		Degraded:        financialReady && !projectionReady,
		Started:         state.Started,
		Closed:          state.Closed,
		CatalogLoaded:   state.CatalogLoaded,
	}
}

// State returns a race-safe lifecycle snapshot.
func (r *Runtime) State() RuntimeState {
	if r == nil {
		return RuntimeState{Closed: true}
	}
	r.mu.Lock()
	started, closed := r.started, r.closed
	r.mu.Unlock()
	loaded := r.Bursar != nil && r.Bursar.Catalog != nil && r.Bursar.Catalog.IsLoaded()
	return RuntimeState{Started: started, Closed: closed, CatalogLoaded: loaded, Components: len(r.components), DependenciesOK: started && !closed}
}

// CheckDependencies performs best-effort catalog and component checks.
func (r *Runtime) CheckDependencies(ctx context.Context) Diagnostics {
	checked := time.Now().UTC()
	diagnostics := Diagnostics{CheckedAt: checked, Health: r.Health(ctx)}
	start := time.Now()
	if r == nil {
		diagnostics.Catalog = DependencyCheck{Name: "catalog", Error: "catalog is not configured", Latency: time.Since(start)}
		return diagnostics
	}
	if r.Bursar == nil || r.Bursar.Catalog == nil {
		diagnostics.Catalog = DependencyCheck{Name: "catalog", Error: "catalog is not configured", Latency: time.Since(start)}
	} else {
		_, err := r.Bursar.Catalog.GetActive(ctx)
		diagnostics.Catalog = dependencyCheck("catalog", start, err)
	}
	diagnostics.Components = make([]DependencyCheck, 0, len(r.components))
	for index, component := range r.components {
		checker, ok := component.component.(bursar.RuntimeHealthChecker)
		if !ok {
			diagnostics.Components = append(diagnostics.Components, DependencyCheck{Name: fmt.Sprintf("component_%d", index), Skipped: true})
			continue
		}
		started := time.Now()
		diagnostics.Components = append(diagnostics.Components, dependencyCheck(fmt.Sprintf("component_%d", index), started, checker.Health(ctx)))
	}
	return diagnostics
}

func dependencyCheck(name string, started time.Time, err error) DependencyCheck {
	check := DependencyCheck{Name: name, OK: err == nil, Latency: time.Since(started)}
	if err != nil {
		check.Error = err.Error()
	}
	return check
}

// Flush runs one bounded pass for each started component in reverse order.
func (r *Runtime) Flush(ctx context.Context) error {
	if r == nil {
		return nil
	}
	if ctx == nil {
		return errors.New("runtime context is required")
	}
	r.startMu.Lock()
	defer r.startMu.Unlock()
	r.mu.Lock()
	closed := r.closed
	started := r.started
	r.mu.Unlock()
	if closed {
		return errors.New("runtime is closed")
	}
	if !started {
		return errors.New("runtime is not started")
	}
	var failures []error
	for index := len(r.components) - 1; index >= 0; index-- {
		if err := r.components[index].component.Flush(ctx); err != nil {
			failures = append(failures, err)
		}
	}
	return errors.Join(failures...)
}

// Close stops owned components and releases SDK-owned stores. Borrowed pools
// and caller-supplied components remain caller-owned.
func (r *Runtime) Close(ctx context.Context) error {
	if r == nil {
		return nil
	}
	if ctx == nil {
		return errors.New("runtime context is required")
	}
	r.startMu.Lock()
	defer r.startMu.Unlock()
	r.mu.Lock()
	if r.closeErr != nil {
		err := r.closeErr
		r.mu.Unlock()
		return err
	}
	if r.closed {
		r.mu.Unlock()
		return nil
	}
	r.closed = true
	components := append([]runtimeComponent(nil), r.components...)
	started := r.started
	bursarFacade := r.Bursar
	ownsBursar := r.ownsBursar
	operator := r.OperatorClient
	r.mu.Unlock()
	var failures []error
	for index := len(components) - 1; index >= 0; index-- {
		if started {
			if err := components[index].component.Flush(ctx); err != nil {
				failures = append(failures, err)
			}
		}
		if components[index].owned {
			if err := components[index].component.Close(ctx); err != nil {
				failures = append(failures, err)
			}
		}
	}
	if ownsBursar && bursarFacade != nil {
		if err := bursarFacade.Close(); err != nil {
			failures = append(failures, err)
		}
	}
	if operator != nil {
		if err := operator.Close(); err != nil {
			failures = append(failures, err)
		}
	}
	err := errors.Join(failures...)
	if err != nil {
		r.mu.Lock()
		r.closeErr = err
		r.mu.Unlock()
	}
	return err
}
