// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package runtime

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	ch "github.com/ClickHouse/clickhouse-go/v2"
	bursar "github.com/Zonastery/bursar/golang/v2"
	"github.com/Zonastery/bursar/golang/v2/storage/clickhouse"
	"github.com/Zonastery/bursar/golang/v2/storage/s3"
	"github.com/jackc/pgx/v5/pgxpool"
)

type runtimeProjectionSink struct{}

func (runtimeProjectionSink) WriteUsage(context.Context, bursar.UsageChargeExport, string) error {
	return nil
}

type runtimeComponentSink struct{ runtimeProjectionSink }

func (runtimeComponentSink) Start(context.Context) error  { return nil }
func (runtimeComponentSink) Flush(context.Context) error  { return nil }
func (runtimeComponentSink) Close(context.Context) error  { return nil }
func (runtimeComponentSink) Health(context.Context) error { return nil }

type runtimeCreditEventSink struct{}

func (runtimeCreditEventSink) Emit(context.Context, bursar.CreditEvent) {}

type runtimeArchive struct{}

func (runtimeArchive) Archive(context.Context, bursar.BillingEventPayloadExport) (bursar.BillingPayloadArchiveResult, error) {
	return bursar.BillingPayloadArchiveResult{}, nil
}

type runtimeArchiveComponent struct{ runtimeArchive }

func (runtimeArchiveComponent) Start(context.Context) error  { return nil }
func (runtimeArchiveComponent) Flush(context.Context) error  { return nil }
func (runtimeArchiveComponent) Close(context.Context) error  { return nil }
func (runtimeArchiveComponent) Health(context.Context) error { return nil }

type runtimeErrorComponent struct {
	startErr error
	flushErr error
	closeErr error
}

func (c *runtimeErrorComponent) Start(context.Context) error  { return c.startErr }
func (c *runtimeErrorComponent) Flush(context.Context) error  { return c.flushErr }
func (c *runtimeErrorComponent) Close(context.Context) error  { return c.closeErr }
func (c *runtimeErrorComponent) Health(context.Context) error { return nil }

type runtimeCloseDuringStartComponent struct{ runtime *Runtime }

func (c *runtimeCloseDuringStartComponent) Start(context.Context) error {
	c.runtime.mu.Lock()
	c.runtime.closed = true
	c.runtime.mu.Unlock()
	return nil
}
func (c *runtimeCloseDuringStartComponent) Flush(context.Context) error { return nil }
func (c *runtimeCloseDuringStartComponent) Close(context.Context) error { return nil }

type runtimeClosableStore struct {
	runtimeCatalogStore
	closeErr error
}

func (s *runtimeClosableStore) Close() error { return s.closeErr }

func TestRuntimeAliasIdentityAndDependencyValidation(t *testing.T) {
	t.Parallel()
	const id = "00000000-0000-0000-0000-000000000001"
	var nilContext context.Context
	if _, err := NewBursarRuntime(nilContext, Options{}); err == nil || !strings.Contains(err.Error(), "context") {
		t.Fatalf("nil constructor context error = %v", err)
	}
	load := false
	runtime, err := New(context.Background(), Options{TenantID: id, Dependencies: &Dependencies{Bursar: &bursar.Bursar{}}})
	if err != nil {
		t.Fatalf("New() error = %v", err)
	}
	if runtime.TenantID() != id || runtime.TenantSlug() != "" {
		t.Fatalf("runtime identity = %q/%q", runtime.TenantID(), runtime.TenantSlug())
	}
	if _, slug, _, err := normalizeIdentity(&Options{TenantID: id, TenantSlug: " Tenant-01 "}); err != nil || slug != "tenant-01" {
		t.Fatalf("normalizeIdentity() slug = %q, %v", slug, err)
	}
	if err := runtime.Start(context.Background(), StartOptions{LoadCatalog: &load}); err != nil {
		t.Fatalf("Start() error = %v", err)
	}
	if err := runtime.Close(context.Background()); err != nil {
		t.Fatalf("Close() error = %v", err)
	}
	var nilRuntime *Runtime
	if nilRuntime.TenantID() != "" || nilRuntime.TenantSlug() != "" {
		t.Fatal("nil runtime returned identity")
	}

	for _, options := range []Options{
		{TenantID: id, Dependencies: &Dependencies{}},
		{TenantID: "", Dependencies: &Dependencies{Bursar: &bursar.Bursar{}}},
		{TenantID: id, Dependencies: &Dependencies{Bursar: &bursar.Bursar{}, Components: []bursar.RuntimeComponent{nil}}},
	} {
		if _, err := NewBursarRuntime(context.Background(), options); err == nil {
			t.Fatalf("invalid dependency options %+v returned nil error", options)
		}
	}
}

func TestRuntimeProjectionPortCompositionAndCleanup(t *testing.T) {
	t.Parallel()
	const id = "00000000-0000-0000-0000-000000000001"
	if _, _, _, err := prepareProjectionPorts(Options{UsageSink: runtimeProjectionSink{}, ClickHouse: &clickhouse.Options{}}, id); err == nil {
		t.Fatal("usage sink and ClickHouse conflict error = nil")
	}
	if _, _, _, err := prepareProjectionPorts(Options{BillingArchive: runtimeArchive{}, S3: &s3.Options{Bucket: "archive"}}, id); err == nil {
		t.Fatal("billing archive and S3 conflict error = nil")
	}

	usage, billing, components, err := prepareProjectionPorts(Options{S3: &s3.Options{Bucket: "archive"}}, id)
	if err != nil || usage != nil || billing == nil || len(components) != 1 || !components[0].owned {
		t.Fatalf("S3 projection ports = %v, %v, %+v, %v", usage, billing, components, err)
	}
	if err := components[0].component.Close(context.Background()); err != nil {
		t.Fatalf("close S3 component = %v", err)
	}

	var connection ch.Conn = struct{ ch.Conn }{}
	usage, billing, components, err = prepareProjectionPorts(Options{ClickHouse: &clickhouse.Options{Connection: connection}}, id)
	if err != nil || usage == nil || billing != nil || len(components) != 1 || !components[0].owned {
		t.Fatalf("ClickHouse projection ports = %v, %v, %+v, %v", usage, billing, components, err)
	}
	if err := components[0].component.Close(context.Background()); err != nil {
		t.Fatalf("close ClickHouse component = %v", err)
	}
	usage, billing, components, err = prepareProjectionPorts(Options{UsageSink: runtimeComponentSink{}, BillingArchive: runtimeArchiveComponent{}}, id)
	if err != nil || usage == nil || billing == nil || len(components) != 2 {
		t.Fatalf("borrowed projection ports = %v, %v, %+v, %v", usage, billing, components, err)
	}
	if _, _, _, err := prepareProjectionPorts(Options{ClickHouse: &clickhouse.Options{Connection: connection, TenantID: "00000000-0000-0000-0000-000000000002"}}, id); err == nil {
		t.Fatal("mismatched ClickHouse tenant error = nil")
	}
	if _, _, _, err := prepareProjectionPorts(Options{ClickHouse: &clickhouse.Options{Connection: connection}, S3: &s3.Options{Bucket: ""}}, id); err == nil {
		t.Fatal("invalid S3 cleanup error = nil")
	}
}

func TestRuntimeNormalizationAndLifecycleErrors(t *testing.T) {
	t.Parallel()
	const id = "00000000-0000-0000-0000-000000000001"
	if _, _, _, err := normalizeOptions(&Options{TenantID: id}); err == nil || !strings.Contains(err.Error(), "primary database") {
		t.Fatal("missing primary database error = nil")
	}
	if _, _, _, err := normalizeOptions(&Options{TenantID: id, DatabaseURL: "postgres://primary"}); err == nil || !strings.Contains(err.Error(), "operator database") {
		t.Fatal("missing operator database error = nil")
	}
	pool := &pgxpool.Pool{}
	if _, _, _, err := normalizeOptions(&Options{TenantID: id, Pool: pool, OperatorPool: pool}); err == nil || !strings.Contains(err.Error(), "pools must be distinct") {
		t.Fatal("same pool error = nil")
	}
	if _, _, _, err := normalizeOptions(&Options{TenantID: id, Pool: pool}); err == nil || !strings.Contains(err.Error(), "operator database") {
		t.Fatal("missing operator pool error = nil")
	}

	flushErr := errors.New("flush failed")
	closeErr := errors.New("close failed")
	runtime := &Runtime{Bursar: &bursar.Bursar{}, started: true, components: []runtimeComponent{{component: &runtimeErrorComponent{flushErr: flushErr, closeErr: closeErr}, owned: true}}}
	if err := runtime.Flush(context.Background()); !errors.Is(err, flushErr) {
		t.Fatalf("Flush() error = %v", err)
	}
	if err := runtime.Close(context.Background()); !errors.Is(err, flushErr) || !errors.Is(err, closeErr) {
		t.Fatalf("Close() joined error = %v", err)
	}
	if err := runtime.Close(context.Background()); !errors.Is(err, flushErr) {
		t.Fatalf("Close() cached error = %v", err)
	}
	if state := runtime.State(); !state.Closed || state.Components != 1 || state.DependenciesOK {
		t.Fatalf("closed state = %#v", state)
	}
	if err := runtime.Start(context.Background()); err == nil || !strings.Contains(err.Error(), "closed") {
		t.Fatalf("Start() after Close error = %v", err)
	}
}

func TestRuntimeStartHealthAndDiagnosticsBranches(t *testing.T) {
	t.Parallel()
	const id = "00000000-0000-0000-0000-000000000001"
	if err := (&Runtime{}).Start(context.Background()); err == nil || !strings.Contains(err.Error(), "not initialized") {
		t.Fatal("uninitialized Start() error = nil")
	}
	var nilContext context.Context
	if err := (&Runtime{Bursar: &bursar.Bursar{}}).Start(nilContext); err == nil || !strings.Contains(err.Error(), "context") {
		t.Fatalf("nil context Start() error = %v", err)
	}
	closed := &Runtime{Bursar: &bursar.Bursar{}, closed: true}
	if err := closed.Start(context.Background()); err == nil || !strings.Contains(err.Error(), "closed") {
		t.Fatalf("closed Start() error = %v", err)
	}
	failed := &Runtime{Bursar: &bursar.Bursar{}, startErr: errors.New("previous failure")}
	if err := failed.Start(context.Background()); err == nil || !strings.Contains(err.Error(), "previous failure") {
		t.Fatalf("previous startup failure = %v", err)
	}
	optionsRuntime := &Runtime{Bursar: &bursar.Bursar{}}
	if err := optionsRuntime.Start(context.Background(), StartOptions{}, StartOptions{}); err == nil || !strings.Contains(err.Error(), "at most one") {
		t.Fatalf("multiple StartOptions error = %v", err)
	}
	if err := optionsRuntime.Start(context.Background(), StartOptions{Retry: &bursar.BursarRetryOptions{MaxAttempts: 1, MaxElapsed: time.Second}}); err == nil {
		t.Fatal("missing catalog Start() error = nil")
	}
	noSlug := &Runtime{Bursar: &bursar.Bursar{}, tenantID: id}
	load := false
	if err := noSlug.Start(context.Background(), StartOptions{LoadCatalog: &load}); err != nil {
		t.Fatalf("no-slug Start() error = %v", err)
	}
	if err := noSlug.Start(context.Background(), StartOptions{LoadCatalog: &load}); err != nil {
		t.Fatalf("second Start() error = %v", err)
	}

	slugRuntime := &Runtime{Bursar: &bursar.Bursar{}, tenantID: id, tenantSlug: "tenant"}
	if err := slugRuntime.Start(context.Background(), StartOptions{LoadCatalog: &load}); err == nil || !strings.Contains(err.Error(), "operator") {
		t.Fatalf("slug verification error = %v", err)
	}
	closedDuringStart := &Runtime{Bursar: &bursar.Bursar{}, components: []runtimeComponent{{component: &runtimeCloseDuringStartComponent{}, owned: true}}}
	closedDuringStart.components[0].component.(*runtimeCloseDuringStartComponent).runtime = closedDuringStart
	if err := closedDuringStart.Start(context.Background(), StartOptions{LoadCatalog: &load}); err == nil || !strings.Contains(err.Error(), "closed during") {
		t.Fatalf("closed during Start() error = %v", err)
	}
	ownedFailure := &Runtime{Bursar: &bursar.Bursar{}, components: []runtimeComponent{{component: &runtimeErrorComponent{}, owned: true}, {component: &runtimeErrorComponent{startErr: errors.New("owned start failed")}, owned: true}}}
	if err := ownedFailure.Start(context.Background(), StartOptions{LoadCatalog: &load}); err == nil || !strings.Contains(err.Error(), "owned start failed") {
		t.Fatalf("owned start failure = %v", err)
	}
	operatorClient, err := bursar.NewPostgresClientFromPool(&pgxpool.Pool{}, bursar.PostgresClientOptions{TenantID: id, ProviderEnvironment: bursar.ProviderEnvironmentTest, AccessRole: bursar.PostgresAccessRoleOperator})
	if err != nil {
		t.Fatal(err)
	}
	_ = operatorClient.Close()
	queryErrorRuntime := &Runtime{Bursar: &bursar.Bursar{}, OperatorClient: operatorClient, tenantID: id, tenantSlug: "tenant"}
	if err := queryErrorRuntime.verifyTenantIdentity(context.Background()); err == nil || !strings.Contains(err.Error(), "verify tenant slug") {
		t.Fatalf("operator query error = %v", err)
	}

	if health := (&Runtime{Bursar: &bursar.Bursar{}, started: true}).Health(context.Background()); health.Ready || health.FinancialReady || !health.ProjectionReady || health.Degraded {
		t.Fatalf("started but unloaded health = %#v", health)
	}
	if health := (&Runtime{Bursar: &bursar.Bursar{}, started: true, components: []runtimeComponent{{component: &componentStub{health: errors.New("down")}}}}).Health(context.Background()); health.ProjectionReady || health.Degraded {
		t.Fatalf("degraded unloaded health = %#v", health)
	}

	store := &runtimeCatalogStore{active: &bursar.CatalogRevision{ID: "catalog-1", Version: 1, Config: map[string]any{"version": 1, "credits": map[string]any{}}}}
	credits, err := bursar.NewCreditsService(store, bursar.CreditsServiceOptions{})
	if err != nil {
		t.Fatal(err)
	}
	loaded := &Runtime{Bursar: &bursar.Bursar{Credits: credits, Catalog: credits.Catalog()}, tenantID: id, started: true, components: []runtimeComponent{{component: &componentStub{}}}}
	if err := loaded.Bursar.LoadCatalog(context.Background()); err != nil {
		t.Fatalf("load test catalog = %v", err)
	}
	health := loaded.Health(context.Background())
	if !health.Ready || !health.FinancialReady || !health.ProjectionReady || health.Degraded || !health.CatalogLoaded {
		t.Fatalf("loaded health = %#v", health)
	}
	closeErr := errors.New("facade close failed")
	closableStore := &runtimeClosableStore{runtimeCatalogStore: runtimeCatalogStore{active: store.active}, closeErr: closeErr}
	closableCredits, err := bursar.NewCreditsService(closableStore, bursar.CreditsServiceOptions{})
	if err != nil {
		t.Fatal(err)
	}
	ownedFacade := &Runtime{Bursar: &bursar.Bursar{Credits: closableCredits, Catalog: closableCredits.Catalog()}, ownsBursar: true}
	if err := ownedFacade.Close(context.Background()); !errors.Is(err, closeErr) {
		t.Fatalf("owned facade close error = %v", err)
	}
	diagnostics := loaded.CheckDependencies(context.Background())
	if !diagnostics.Catalog.OK || len(diagnostics.Components) != 1 || !diagnostics.Components[0].OK {
		t.Fatalf("loaded diagnostics = %#v", diagnostics)
	}
	if diagnostics.Catalog.Latency <= 0 {
		t.Fatalf("catalog diagnostic latency = %v", diagnostics.Catalog.Latency)
	}
	withoutCatalog := &Runtime{Bursar: &bursar.Bursar{}, components: []runtimeComponent{{component: &runtimeBasicComponent{}}}}
	withoutCatalogDiagnostics := withoutCatalog.CheckDependencies(context.Background())
	if withoutCatalogDiagnostics.Catalog.Error != "catalog is not configured" || len(withoutCatalogDiagnostics.Components) != 1 || !withoutCatalogDiagnostics.Components[0].Skipped {
		t.Fatalf("without catalog diagnostics = %#v", withoutCatalogDiagnostics)
	}
	if err := (&Runtime{}).Flush(context.Background()); err == nil || !strings.Contains(err.Error(), "not started") {
		t.Fatalf("unstarted Flush() error = %v", err)
	}
	if err := (&Runtime{closed: true}).Flush(context.Background()); err == nil || !strings.Contains(err.Error(), "closed") {
		t.Fatalf("closed Flush() error = %v", err)
	}
	var nilRuntime *Runtime
	if err := nilRuntime.Flush(context.Background()); err != nil {
		t.Fatalf("nil Flush() error = %v", err)
	}
	if err := nilRuntime.Close(context.Background()); err != nil {
		t.Fatalf("nil Close() error = %v", err)
	}
}

type runtimeCatalogStore struct {
	bursar.CreditStore
	active *bursar.CatalogRevision
}

type runtimeBasicComponent struct{}

func (runtimeBasicComponent) Start(context.Context) error { return nil }
func (runtimeBasicComponent) Flush(context.Context) error { return nil }
func (runtimeBasicComponent) Close(context.Context) error { return nil }

func (s *runtimeCatalogStore) GetActiveCatalog(context.Context) (*bursar.CatalogRevision, error) {
	return s.active, nil
}

func TestRuntimeConstructsWithCallerOwnedPools(t *testing.T) {
	t.Parallel()
	const id = "00000000-0000-0000-0000-000000000001"
	primaryPool := &pgxpool.Pool{}
	operatorPool := &pgxpool.Pool{}
	runtime, err := NewBursarRuntime(context.Background(), Options{
		TenantID: id, ProviderEnvironment: bursar.ProviderEnvironmentTest,
		Pool: primaryPool, OperatorPool: operatorPool,
	})
	if err != nil {
		t.Fatalf("NewBursarRuntime(caller pools) error = %v", err)
	}
	if runtime.PrimaryStore == nil || runtime.OperatorClient == nil || runtime.Maintenance == nil || runtime.OperatorMaintenance == nil || runtime.Bursar == nil {
		t.Fatalf("constructed runtime is incomplete: %#v", runtime)
	}
	if runtime.TenantID() != id || runtime.TenantSlug() != "" {
		t.Fatalf("runtime identity = %q/%q", runtime.TenantID(), runtime.TenantSlug())
	}
	if err := runtime.Close(context.Background()); err != nil {
		t.Fatalf("Close() error = %v", err)
	}
}

func TestRuntimeConstructorProjectionAndConfigurationBranches(t *testing.T) {
	t.Parallel()
	const id = "00000000-0000-0000-0000-000000000001"
	primaryPool := &pgxpool.Pool{}
	operatorPool := &pgxpool.Pool{}
	registry, err := bursar.NewProviderRegistry(bursar.ProviderFactoryContext{TenantID: id, ProviderEnvironment: bursar.ProviderEnvironmentTest}, map[string]bursar.ProviderFactory{
		"test": func(context.Context, bursar.ProviderFactoryContext) (bursar.PaymentProvider, error) {
			return runtimePaymentProvider{}, nil
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	for _, options := range []Options{
		{TenantID: id, ProviderEnvironment: bursar.ProviderEnvironmentTest, Pool: primaryPool, OperatorPool: operatorPool, S3: &s3.Options{Bucket: "archive"}},
		{TenantID: id, ProviderEnvironment: bursar.ProviderEnvironmentTest, Pool: primaryPool, OperatorPool: operatorPool, ClickHouse: &clickhouse.Options{Connection: struct{ ch.Conn }{}}},
		{TenantID: id, ProviderEnvironment: bursar.ProviderEnvironmentTest, Pool: primaryPool, OperatorPool: operatorPool, UsageSink: runtimeProjectionSink{}, DisableOutbox: true},
		{TenantID: id, ProviderEnvironment: bursar.ProviderEnvironmentTest, Pool: primaryPool, OperatorPool: operatorPool, BillingOptions: &bursar.BillingServiceOptions{}},
		{TenantID: id, ProviderEnvironment: bursar.ProviderEnvironmentTest, Pool: primaryPool, OperatorPool: operatorPool, CommerceOptions: &bursar.CommerceOptions{Providers: registry}},
	} {
		runtime, err := NewBursarRuntime(context.Background(), options)
		if err != nil {
			t.Fatalf("projection constructor error = %v", err)
		}
		if err := runtime.Close(context.Background()); err != nil {
			t.Fatalf("projection constructor Close() error = %v", err)
		}
	}
	if _, err := NewBursarRuntime(context.Background(), Options{TenantID: id, ProviderEnvironment: bursar.ProviderEnvironmentTest, Pool: primaryPool, OperatorPool: operatorPool, ClickHouse: &clickhouse.Options{Connection: struct{ ch.Conn }{}}, CreditsOptions: bursar.CreditsServiceOptions{Analytics: runtimeAnalyticsStub{}}}); err == nil {
		t.Fatal("duplicate analytics configuration error = nil")
	}
	if _, err := NewBursarRuntime(context.Background(), Options{TenantID: id, ProviderEnvironment: bursar.ProviderEnvironmentTest, Pool: primaryPool, OperatorPool: operatorPool, ClickHouse: &clickhouse.Options{Connection: struct{ ch.Conn }{}}, CreditsOptions: bursar.CreditsServiceOptions{UsageStore: runtimeUsageStoreStub{}}}); err == nil {
		t.Fatal("duplicate usage store configuration error = nil")
	}
	if _, err := NewBursarRuntime(context.Background(), Options{TenantID: id, ProviderEnvironment: bursar.ProviderEnvironmentTest, Pool: primaryPool, OperatorPool: operatorPool, OperatorDatabaseURL: "postgres://operator"}); err == nil {
		t.Fatal("operator URL and pool conflict error = nil")
	}
	if _, err := NewBursarRuntime(context.Background(), Options{TenantID: id, ProviderEnvironment: bursar.ProviderEnvironmentTest, Pool: primaryPool, OperatorPool: operatorPool, Emitter: runtimeCreditEventSink{}, CreditsOptions: bursar.CreditsServiceOptions{EventSink: runtimeCreditEventSink{}}}); err == nil {
		t.Fatal("duplicate event sink error = nil")
	}
	if _, err := NewBursarRuntime(context.Background(), Options{TenantID: id, ProviderEnvironment: bursar.ProviderEnvironmentTest, Pool: primaryPool, OperatorPool: operatorPool, CommerceOptions: &bursar.CommerceOptions{}}); err == nil || !strings.Contains(err.Error(), "provider registry") {
		t.Fatalf("missing commerce provider registry error = %v", err)
	}
	if _, err := NewBursarRuntime(context.Background(), Options{TenantID: id, ProviderEnvironment: bursar.ProviderEnvironmentTest, DatabaseURL: "not-a-url", OperatorDatabaseURL: "postgres://operator"}); err == nil {
		t.Fatal("invalid primary URL error = nil")
	}
	if _, err := NewBursarRuntime(context.Background(), Options{TenantID: id, ProviderEnvironment: bursar.ProviderEnvironmentTest, Pool: primaryPool, OperatorDatabaseURL: "not-a-url"}); err == nil {
		t.Fatal("invalid operator URL error = nil")
	}
	if _, err := NewBursarRuntime(context.Background(), Options{TenantID: id, ProviderEnvironment: bursar.ProviderEnvironmentTest, Pool: primaryPool, OperatorPool: operatorPool, ClickHouse: &clickhouse.Options{}}); err == nil || !strings.Contains(err.Error(), "ClickHouse connection") {
		t.Fatalf("invalid ClickHouse projection error = %v", err)
	}
}

type runtimeAnalyticsStub struct{}

func (runtimeAnalyticsStub) SpendByUser(context.Context, time.Time, time.Time) ([]bursar.SpendByUserRow, error) {
	return nil, nil
}
func (runtimeAnalyticsStub) SpendByModel(context.Context, time.Time, time.Time) ([]bursar.SpendByModelRow, error) {
	return nil, nil
}
func (runtimeAnalyticsStub) TopUsers(context.Context, int, time.Time, time.Time) ([]bursar.TopUserRow, error) {
	return nil, nil
}
func (runtimeAnalyticsStub) DailySpend(context.Context, time.Time, time.Time) ([]bursar.DailySpendRow, error) {
	return nil, nil
}
func (runtimeAnalyticsStub) AggregateStats(context.Context, time.Time, time.Time) (bursar.AggregateStats, error) {
	return bursar.AggregateStats{}, nil
}

type runtimeUsageStoreStub struct{}

func (runtimeUsageStoreStub) ListUsageCharges(context.Context, string, bursar.ListUsageChargesOptions) (bursar.UsageChargePage, error) {
	return bursar.UsageChargePage{}, nil
}

type runtimePaymentProvider struct{}

func (runtimePaymentProvider) Name() string { return "test" }
func (runtimePaymentProvider) CreateCheckoutSession(context.Context, bursar.CheckoutSessionRequest) (bursar.CheckoutSession, error) {
	return bursar.CheckoutSession{}, nil
}
func (runtimePaymentProvider) HandleWebhook(context.Context, bursar.WebhookRequest) (bursar.WebhookResult, error) {
	return bursar.WebhookResult{}, nil
}

type runtimeRecoveryStub struct{}

func (runtimeRecoveryStub) Claim(context.Context, []string, int, int) ([]bursar.OutboxEvent, error) {
	return nil, nil
}
func (runtimeRecoveryStub) Renew(context.Context, bursar.OutboxEvent, int) (bool, error) {
	return true, nil
}
func (runtimeRecoveryStub) Complete(context.Context, bursar.OutboxEvent) (bool, error) {
	return true, nil
}
func (runtimeRecoveryStub) Fail(context.Context, bursar.OutboxEvent, string, int, int) (bool, error) {
	return true, nil
}
func (runtimeRecoveryStub) Stats(context.Context) (bursar.OutboxStats, error) {
	return bursar.OutboxStats{}, nil
}
func (runtimeRecoveryStub) ListDeadLetters(context.Context, bursar.OutboxDeadLetterListOptions) (bursar.OutboxDeadLetterPage, error) {
	return bursar.OutboxDeadLetterPage{}, nil
}
func (runtimeRecoveryStub) Requeue(context.Context, string) (bool, error) { return true, nil }

type runtimeProjectionRepositoryStub struct{}

func (runtimeProjectionRepositoryStub) GetUsageCharge(context.Context, string) (*bursar.UsageChargeExport, error) {
	return nil, nil
}
func (runtimeProjectionRepositoryStub) GetBillingEventPayload(context.Context, string) (*bursar.BillingEventPayloadExport, error) {
	return nil, nil
}
func (runtimeProjectionRepositoryStub) ArchiveBillingEventPayload(context.Context, string, string, *string) (bool, error) {
	return true, nil
}

func TestRuntimeAddsProjectionWorkerWhenPortsAreConfigured(t *testing.T) {
	t.Parallel()
	runtime := &Runtime{}
	err := runtime.addProjectionWorker(Options{}, runtimeRecoveryStub{}, runtimeProjectionRepositoryStub{}, runtimeProjectionSink{}, nil)
	if err != nil {
		t.Fatalf("addProjectionWorker() error = %v", err)
	}
	if runtime.Worker == nil || len(runtime.components) != 1 {
		t.Fatalf("projection worker = %#v components=%d", runtime.Worker, len(runtime.components))
	}
	if err := runtime.Worker.Close(context.Background()); err != nil {
		t.Fatalf("worker Close() error = %v", err)
	}
	if err := (&Runtime{}).addProjectionWorker(Options{DisableOutbox: true}, nil, nil, nil, nil); err != nil {
		t.Fatalf("disabled outbox error = %v", err)
	}
	if err := (&Runtime{}).addProjectionWorker(Options{}, nil, nil, runtimeProjectionSink{}, nil); err == nil {
		t.Fatal("missing outbox repositories error = nil")
	}
	archiveRuntime := &Runtime{}
	if err := archiveRuntime.addProjectionWorker(Options{Outbox: &bursar.OutboxWorkerOptions{BatchSize: 0}}, runtimeRecoveryStub{}, runtimeProjectionRepositoryStub{}, nil, runtimeArchive{}); err != nil {
		t.Fatalf("archive projection worker error = %v", err)
	}
	if archiveRuntime.Worker == nil {
		t.Fatal("archive projection worker is nil")
	}
	if err := archiveRuntime.Worker.Close(context.Background()); err != nil {
		t.Fatalf("archive worker Close() error = %v", err)
	}
	invalidWorker := &Runtime{}
	if err := invalidWorker.addProjectionWorker(Options{Outbox: &bursar.OutboxWorkerOptions{BatchSize: -1}}, runtimeRecoveryStub{}, runtimeProjectionRepositoryStub{}, runtimeProjectionSink{}, nil); err == nil {
		t.Fatal("invalid outbox options error = nil")
	}
	if _, err := newMaintenance(&bursar.PostgresStore{}, &bursar.Bursar{Billing: &bursar.BillingService{}}); err != nil {
		t.Fatalf("newMaintenance(billing) error = %v", err)
	}
	worker := runtime.Worker
	dependencyRuntime, err := NewBursarRuntime(context.Background(), Options{TenantID: "00000000-0000-0000-0000-000000000001", Dependencies: &Dependencies{Bursar: &bursar.Bursar{}, Worker: worker}})
	if err != nil {
		t.Fatalf("worker dependency runtime error = %v", err)
	}
	if len(dependencyRuntime.components) != 1 {
		t.Fatalf("worker dependency components = %d", len(dependencyRuntime.components))
	}
}
