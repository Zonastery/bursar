// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import (
	"context"
	"errors"
	"fmt"
	"os"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

type postgresIntegrationConfig struct {
	databaseURL         string
	tenantID            string
	secondaryTenantID   string
	providerEnvironment ProviderEnvironment
}

func TestPostgresSDKIntegration(t *testing.T) {
	config := requirePostgresIntegration(t)
	ctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
	defer cancel()

	store := openPostgresIntegrationStore(t, ctx, config, config.tenantID)
	defer store.Close()

	sdk, err := New(Options{CreditStore: store, BillingStore: store})
	if err != nil {
		t.Fatalf("construct Bursar facade: %v", err)
	}
	defer sdk.Close()

	fixture, err := os.ReadFile("../.github/package-smoke/pricing.json")
	if err != nil {
		t.Fatalf("read PostgreSQL integration catalog: %v", err)
	}
	knownCatalog, err := LoadConfigJSON(fixture)
	if err != nil {
		t.Fatalf("parse PostgreSQL integration catalog: %v", err)
	}
	knownDocument := CanonicalParsedBursarConfigDict(knownCatalog)
	if _, err := sdk.Catalog.PublishAndActivate(ctx, knownDocument, "go-postgres-integration", newAssignmentsRollout(knownDocument)); err != nil {
		t.Fatalf("activate PostgreSQL integration catalog: %v", err)
	}

	if got := store.TenantID(); got != config.tenantID {
		t.Fatalf("store tenant ID = %q, want %q", got, config.tenantID)
	}
	if got := store.ProviderEnvironment(); got != config.providerEnvironment {
		t.Fatalf("store provider environment = %q, want %q", got, config.providerEnvironment)
	}

	t.Run("catalog and plan", func(t *testing.T) {
		active, err := sdk.Catalog.GetActive(ctx)
		if err != nil {
			t.Fatalf("get active catalog: %v", err)
		}
		if active == nil || active.Version < 1 {
			t.Fatalf("active catalog = %+v, want a positive revision", active)
		}
		if err := sdk.LoadCatalog(ctx); err != nil {
			t.Fatalf("load catalog: %v", err)
		}
		if !sdk.Catalog.IsLoaded() {
			t.Fatal("catalog did not report loaded after LoadCatalog")
		}
		config, err := sdk.Catalog.GetConfig(ctx)
		if err != nil {
			t.Fatalf("get catalog config: %v", err)
		}
		if _, ok := config.Plans["pro"]; !ok {
			t.Fatalf("catalog plans do not contain pro: %+v", config.Plans)
		}

		userID := integrationUserID()
		if _, err := sdk.Credits.AddCredits(ctx, userID, MustAmount("10"), AddCreditsOptions{
			Type:           "purchase",
			Bucket:         "purchased",
			IdempotencyKey: integrationKey("plan-funding"),
		}); err != nil {
			t.Fatalf("fund plan user: %v", err)
		}
		assigned, err := sdk.Credits.SetUserPlan(ctx, userID, "pro", SetUserPlanOptions{})
		if err != nil {
			t.Fatalf("assign pro plan: %v", err)
		}
		if assigned.PlanKey != "pro" {
			t.Fatalf("assigned plan key = %q, want pro", assigned.PlanKey)
		}
		got, err := sdk.Credits.GetUserPlan(ctx, userID)
		if err != nil {
			t.Fatalf("read assigned plan: %v", err)
		}
		if got.PlanKey != "pro" || got.RateCard != "standard" {
			t.Fatalf("user plan = %+v, want pro/standard", got)
		}
	})

	t.Run("idempotent add deduct and refund", func(t *testing.T) {
		userID := integrationUserID()
		grantKey := integrationKey("grant")
		first, err := sdk.Credits.AddCredits(ctx, userID, MustAmount("10"), AddCreditsOptions{
			Type:           "purchase",
			Bucket:         "purchased",
			IdempotencyKey: grantKey,
		})
		if err != nil {
			t.Fatalf("add credits: %v", err)
		}
		replay, err := sdk.Credits.AddCredits(ctx, userID, MustAmount("10"), AddCreditsOptions{
			Type:           "purchase",
			Bucket:         "purchased",
			IdempotencyKey: grantKey,
		})
		if err != nil {
			t.Fatalf("replay add credits: %v", err)
		}
		if first.EntryID == "" || replay.EntryID != first.EntryID || !replay.Idempotent {
			t.Fatalf("grant replay = %+v, first = %+v", replay, first)
		}

		deductKey := integrationKey("deduct")
		deduction, err := sdk.Credits.Deduct(ctx, userID, MustAmount("3"), DeductWithAllowanceOptions{
			Operation:      "completion",
			IdempotencyKey: deductKey,
		})
		if err != nil {
			t.Fatalf("deduct credits: %v", err)
		}
		deductReplay, err := sdk.Credits.Deduct(ctx, userID, MustAmount("3"), DeductWithAllowanceOptions{
			Operation:      "completion",
			IdempotencyKey: deductKey,
		})
		if err != nil {
			t.Fatalf("replay deduct credits: %v", err)
		}
		if deduction.EntryID == "" || deductReplay.EntryID != deduction.EntryID || !deductReplay.Idempotent {
			t.Fatalf("deduct replay = %+v, first = %+v", deductReplay, deduction)
		}

		refundAmount := deduction.Amount
		refundKey := integrationKey("refund")
		refund, err := sdk.Credits.RefundCredits(ctx, deduction.EntryID, &refundAmount, "go integration test", nil, refundKey)
		if err != nil {
			t.Fatalf("refund credits: %v", err)
		}
		refundReplay, err := sdk.Credits.RefundCredits(ctx, deduction.EntryID, &refundAmount, "go integration test", nil, refundKey)
		if err != nil {
			t.Fatalf("replay refund credits: %v", err)
		}
		if refund.RefundEntryID == "" || refundReplay.RefundEntryID != refund.RefundEntryID {
			t.Fatalf("refund replay = %+v, first = %+v", refundReplay, refund)
		}
		if refundReplay.Amount == nil || !refundReplay.Amount.Equal(refundAmount) {
			t.Fatalf("refund replay amount = %v, want %s", refundReplay.Amount, refundAmount)
		}
		balance, err := sdk.Credits.GetBalance(ctx, userID)
		if err != nil {
			t.Fatalf("read balance after refund: %v", err)
		}
		if !balance.Balance.Equal(MustAmount("10")) {
			t.Fatalf("balance after refund = %s, want 10", balance.Balance)
		}
	})

	t.Run("caller-owned pool preserves exact JSON amounts", func(t *testing.T) {
		pool, err := pgxpool.New(ctx, config.databaseURL)
		if err != nil {
			t.Fatalf("create caller-owned pool: %v", err)
		}
		defer pool.Close()
		borrowed, err := NewPostgresStoreFromPool(pool, PostgresStoreOptions{
			TenantID:            config.tenantID,
			ProviderEnvironment: config.providerEnvironment,
		})
		if err != nil {
			t.Fatalf("construct store from caller-owned pool: %v", err)
		}
		defer borrowed.Close()
		borrowedSDK, err := New(Options{CreditStore: borrowed})
		if err != nil {
			t.Fatalf("construct caller-pool facade: %v", err)
		}
		defer borrowedSDK.Close()

		userID := integrationUserID()
		funding := MustAmount("99999999999999.999999")
		if _, err := borrowedSDK.Credits.AddCredits(ctx, userID, funding, AddCreditsOptions{
			Type: "purchase", Bucket: "purchased", IdempotencyKey: integrationKey("caller-pool-funding"),
		}); err != nil {
			t.Fatalf("fund caller-pool user: %v", err)
		}
		debit := MustAmount("99999999999998.123456")
		result, err := borrowedSDK.Credits.Deduct(ctx, userID, debit, DeductWithAllowanceOptions{
			Operation: "completion", IdempotencyKey: integrationKey("caller-pool-deduct"),
		})
		if err != nil {
			t.Fatalf("deduct through caller-owned pool: %v", err)
		}
		if got := result.BucketBreakdown["purchased"]; !got.Equal(debit) {
			t.Fatalf("caller-pool bucket amount = %s, want %s", got, debit)
		}
	})

	t.Run("reserve and settle replay", func(t *testing.T) {
		userID := integrationUserID()
		if _, err := sdk.Credits.AddCredits(ctx, userID, MustAmount("10"), AddCreditsOptions{
			Type:           "purchase",
			Bucket:         "purchased",
			IdempotencyKey: integrationKey("lease-funding"),
		}); err != nil {
			t.Fatalf("fund lease user: %v", err)
		}
		reserveKey := integrationKey("reserve")
		lease, err := sdk.Credits.Reserve(ctx, userID, MustAmount("4"), ReserveOptions{
			OperationType:  "completion",
			IdempotencyKey: reserveKey,
			TTL:            time.Minute,
		})
		if err != nil {
			t.Fatalf("reserve credits: %v", err)
		}
		leaseReplay, err := sdk.Credits.Reserve(ctx, userID, MustAmount("4"), ReserveOptions{
			OperationType:  "completion",
			IdempotencyKey: reserveKey,
			TTL:            time.Minute,
		})
		if err != nil {
			t.Fatalf("replay reserve credits: %v", err)
		}
		if lease.LeaseID == "" || leaseReplay.LeaseID != lease.LeaseID {
			t.Fatalf("reserve replay = %+v, first = %+v", leaseReplay, lease)
		}

		settleKey := integrationKey("settle")
		settled, err := sdk.Credits.Settle(ctx, userID, lease.LeaseID, MustAmount("2"), SettleOptions{
			IdempotencyKey: settleKey,
		})
		if err != nil {
			t.Fatalf("settle lease: %v", err)
		}
		settleReplay, err := sdk.Credits.Settle(ctx, userID, lease.LeaseID, MustAmount("2"), SettleOptions{
			IdempotencyKey: settleKey,
		})
		if err != nil {
			t.Fatalf("replay settle lease: %v", err)
		}
		if settled.EntryID == "" || settleReplay.EntryID != settled.EntryID || !settleReplay.Idempotent {
			t.Fatalf("settle replay = %+v, first = %+v", settleReplay, settled)
		}
		balance, err := sdk.Credits.GetBalance(ctx, userID)
		if err != nil {
			t.Fatalf("read balance after settlement: %v", err)
		}
		if !balance.Balance.Equal(MustAmount("8")) {
			t.Fatalf("balance after settlement = %s, want 8", balance.Balance)
		}
	})

	t.Run("concurrent double spend prevention", func(t *testing.T) {
		userID := integrationUserID()
		if _, err := sdk.Credits.AddCredits(ctx, userID, MustAmount("5"), AddCreditsOptions{
			Type:           "purchase",
			Bucket:         "purchased",
			IdempotencyKey: integrationKey("concurrency-funding"),
		}); err != nil {
			t.Fatalf("fund concurrent user: %v", err)
		}

		const workers = 8
		results := make(chan struct {
			result DeductionResult
			err    error
		}, workers)
		var waitGroup sync.WaitGroup
		for worker := 0; worker < workers; worker++ {
			worker := worker
			waitGroup.Add(1)
			go func() {
				defer waitGroup.Done()
				result, err := sdk.Credits.Deduct(ctx, userID, MustAmount("1"), DeductWithAllowanceOptions{
					Operation:      "completion",
					IdempotencyKey: integrationKey(fmt.Sprintf("concurrent-deduct-%d", worker)),
				})
				results <- struct {
					result DeductionResult
					err    error
				}{result: result, err: err}
			}()
		}
		waitGroup.Wait()
		close(results)

		successes := 0
		failures := 0
		for outcome := range results {
			if outcome.err == nil {
				successes++
				continue
			}
			failures++
			var bursarErr *BursarError
			if !errors.As(outcome.err, &bursarErr) || bursarErr.Code != ErrorCodeInsufficientCredits {
				t.Fatalf("concurrent deduction error = %v, want insufficient credits", outcome.err)
			}
		}
		if successes != 5 || failures != workers-5 {
			t.Fatalf("concurrent deductions: successes=%d failures=%d, want 5/%d", successes, failures, workers-5)
		}
		balance, err := sdk.Credits.GetBalance(ctx, userID)
		if err != nil {
			t.Fatalf("read balance after concurrent deductions: %v", err)
		}
		if !balance.Balance.Equal(MustAmount("0")) {
			t.Fatalf("balance after concurrent deductions = %s, want 0", balance.Balance)
		}
	})

	if config.secondaryTenantID == "" {
		if os.Getenv("BURSAR_REQUIRE_POSTGRES_TESTS") == "1" {
			t.Fatal("BURSAR_SECONDARY_TENANT_ID is required when PostgreSQL integration tests are required")
		}
		t.Log("BURSAR_SECONDARY_TENANT_ID is not set; skipping tenant isolation check")
		return
	}
	t.Run("tenant RLS isolation", func(t *testing.T) {
		if config.secondaryTenantID == config.tenantID {
			t.Fatal("secondary tenant ID must differ from BURSAR_TENANT_ID")
		}
		secondary := openPostgresIntegrationStore(t, ctx, config, config.secondaryTenantID)
		defer secondary.Close()
		secondarySDK, err := New(Options{CreditStore: secondary, BillingStore: secondary})
		if err != nil {
			t.Fatalf("construct secondary Bursar facade: %v", err)
		}
		defer secondarySDK.Close()
		if err := secondarySDK.LoadCatalog(ctx); err != nil {
			t.Fatalf("load secondary catalog: %v", err)
		}

		primaryUserID := integrationUserID()
		if _, err := sdk.Credits.AddCredits(ctx, primaryUserID, MustAmount("2"), AddCreditsOptions{
			Type:           "purchase",
			Bucket:         "purchased",
			IdempotencyKey: integrationKey("primary-isolation-funding"),
		}); err != nil {
			t.Fatalf("fund primary isolation user: %v", err)
		}
		isolated, err := secondarySDK.Credits.GetBalance(ctx, primaryUserID)
		if err != nil {
			t.Fatalf("read primary user through secondary tenant: %v", err)
		}
		if !isolated.Balance.Equal(MustAmount("0")) {
			t.Fatalf("secondary tenant saw primary balance %s", isolated.Balance)
		}
		secondaryUserID := integrationUserID()
		if _, err := secondarySDK.Credits.AddCredits(ctx, secondaryUserID, MustAmount("3"), AddCreditsOptions{
			Type:           "purchase",
			Bucket:         "purchased",
			IdempotencyKey: integrationKey("secondary-isolation-funding"),
		}); err != nil {
			t.Fatalf("fund secondary isolation user: %v", err)
		}
		primary, err := sdk.Credits.GetBalance(ctx, secondaryUserID)
		if err != nil {
			t.Fatalf("read secondary user through primary tenant: %v", err)
		}
		if !primary.Balance.Equal(MustAmount("0")) {
			t.Fatalf("primary tenant saw secondary balance %s", primary.Balance)
		}
	})
}

func requirePostgresIntegration(t *testing.T) postgresIntegrationConfig {
	t.Helper()
	databaseURL := strings.TrimSpace(os.Getenv("DATABASE_URL"))
	if databaseURL == "" {
		postgresIntegrationUnavailable(t, "DATABASE_URL is not set")
	}
	tenantID := strings.TrimSpace(os.Getenv("BURSAR_TENANT_ID"))
	if tenantID == "" {
		t.Fatal("BURSAR_TENANT_ID is required when DATABASE_URL is set")
	}
	providerEnvironment := ProviderEnvironment(strings.TrimSpace(os.Getenv("BURSAR_PROVIDER_ENVIRONMENT")))
	if providerEnvironment == "" {
		providerEnvironment = ProviderEnvironmentTest
	}
	if err := providerEnvironment.Validate(); err != nil {
		t.Fatalf("invalid BURSAR_PROVIDER_ENVIRONMENT: %v", err)
	}
	return postgresIntegrationConfig{
		databaseURL:         databaseURL,
		tenantID:            tenantID,
		secondaryTenantID:   strings.TrimSpace(os.Getenv("BURSAR_SECONDARY_TENANT_ID")),
		providerEnvironment: providerEnvironment,
	}
}

func openPostgresIntegrationStore(t *testing.T, ctx context.Context, config postgresIntegrationConfig, tenantID string) *PostgresStore {
	t.Helper()
	pingCtx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	connection, err := pgx.Connect(pingCtx, config.databaseURL)
	if err != nil {
		postgresIntegrationUnavailable(t, "PostgreSQL is unavailable: "+err.Error())
	}
	if err := connection.Close(pingCtx); err != nil {
		t.Fatalf("close PostgreSQL readiness connection: %v", err)
	}

	store, err := NewPostgresStore(ctx, config.databaseURL, PostgresStoreOptions{
		TenantID:            tenantID,
		ProviderEnvironment: config.providerEnvironment,
		MaxConnections:      20,
	})
	if err != nil {
		t.Fatalf("construct tenant-scoped PostgreSQL store: %v", err)
	}
	if _, err := store.GetActiveCatalog(ctx); err != nil {
		store.Close()
		t.Fatalf("read active catalog through tenant-scoped store: %v", err)
	}
	return store
}

func postgresIntegrationUnavailable(t *testing.T, message string) {
	t.Helper()
	if os.Getenv("BURSAR_REQUIRE_POSTGRES_TESTS") == "1" {
		t.Fatalf("required PostgreSQL integration tests unavailable: %s", message)
	}
	t.Skip("PostgreSQL integration tests skipped: " + message)
}

func integrationUserID() string {
	return uuid.NewString()
}

func integrationKey(suffix string) string {
	return "go-postgres-integration:" + uuid.NewString() + ":" + suffix
}
