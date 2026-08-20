// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

func TestPostgresClientBoundaryHelpers(t *testing.T) {
	const tenant = "00000000-0000-4000-8000-000000000201"
	if _, err := NewPostgresClient(context.Background(), "postgres://%zz", PostgresClientOptions{}); err == nil {
		t.Fatal("malformed PostgreSQL URL accepted")
	}

	for _, value := range []string{
		"00000000-0000-4000-8000-00000000020Z",
		"00000000-0000-4000_8000-000000000201",
	} {
		if _, err := normalizeTenantID(value); err == nil {
			t.Errorf("normalizeTenantID(%q) accepted malformed UUID", value)
		}
	}
	if got, err := normalizeTenantID(strings.ToUpper(tenant)); err != nil || got != tenant {
		t.Fatalf("normalizeTenantID uppercase = %q, %v", got, err)
	}
	if got, err := normalizeTenantID("  " + tenant + "  "); err != nil || got != tenant {
		t.Fatalf("normalizeTenantID trimmed = %q, %v", got, err)
	}

	var callbackErr error
	client := &PostgresClient{options: PostgresClientOptions{
		OnPoolError: func(err error) { callbackErr = err },
	}}
	client.notifyPoolError(errors.New("pool unavailable"))
	if callbackErr == nil || callbackErr.Error() != "pool unavailable" {
		t.Fatalf("pool error callback = %v", callbackErr)
	}
	client.options.OnPoolError = func(error) { panic("callback must not escape") }
	client.notifyPoolError(errors.New("pool unavailable"))
	(*PostgresClient)(nil).notifyPoolError(errors.New("ignored"))

	borrowed, err := NewPostgresClientFromPool(&pgxpool.Pool{}, PostgresClientOptions{TenantID: tenant})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := (&PostgresClient{}).acquire(context.Background()); err == nil {
		t.Fatal("client without a pool acquired a connection")
	}
	if err := borrowed.Close(); err != nil {
		t.Fatal(err)
	}

	if _, err := (PostgresClientOptions{ProviderEnvironment: "invalid"}).normalized(); err == nil {
		t.Fatal("invalid provider environment accepted")
	}
	for _, options := range []PostgresClientOptions{
		{ConnectionTimeout: -time.Nanosecond},
		{StatementTimeout: -time.Nanosecond},
		{IdleTransactionTimeout: -time.Nanosecond},
		{MaxConnections: -1},
		{ApplicationName: "bursar\x00sdk"},
	} {
		if _, err := options.normalized(); err == nil {
			t.Errorf("invalid options accepted: %#v", options)
		}
	}
}

func TestPostgresClientRealTransactionAndJSONBoundaries(t *testing.T) {
	config := requirePostgresIntegration(t)
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	client, err := NewPostgresClient(ctx, config.databaseURL, PostgresClientOptions{
		TenantID:            config.tenantID,
		AccessRole:          PostgresAccessRoleClient,
		ProviderEnvironment: config.providerEnvironment,
		MaxConnections:      1,
	})
	if err != nil {
		t.Fatalf("construct client: %v", err)
	}
	defer client.Close()
	if !client.OwnsPool() || client.TenantID() != config.tenantID {
		t.Fatalf("client identity = tenant %q, owns %v", client.TenantID(), client.OwnsPool())
	}

	rows, err := client.Query(ctx, `
		SELECT 7::bigint AS integer_value,
		       '{"amount":99999999999999.999999}'::jsonb AS payload,
		       'ok'::text AS state`)
	if err != nil {
		t.Fatalf("query exact JSON row: %v", err)
	}
	if len(rows) != 1 || rows[0]["integer_value"] != int64(7) {
		t.Fatalf("query row = %#v", rows)
	}
	got, ok := rows[0]["payload"].([]byte)
	if !ok {
		t.Fatalf("JSON payload = %#v", rows[0]["payload"])
	}
	var payload map[string]json.Number
	decoder := json.NewDecoder(strings.NewReader(string(got)))
	decoder.UseNumber()
	if err := decoder.Decode(&payload); err != nil || payload["amount"] != json.Number("99999999999999.999999") {
		t.Fatalf("JSON payload = %q, decode error = %v", got, err)
	}

	settings, err := client.Call(ctx, "current_setting", "bursar.tenant_id", true)
	if err != nil || len(settings) != 1 || settings[0]["current_setting"] != config.tenantID {
		t.Fatalf("current tenant setting = %#v, %v", settings, err)
	}
	if _, err := client.Call(ctx, "invalid-rpc-name", true); err == nil {
		t.Fatal("invalid RPC name accepted")
	}

	var poolError error
	client.options.OnPoolError = func(err error) { poolError = err }
	holding, err := client.pool.Acquire(ctx)
	if err != nil {
		t.Fatalf("hold sole pool connection: %v", err)
	}
	shortContext, shortCancel := context.WithTimeout(ctx, 50*time.Millisecond)
	_, acquireErr := client.acquire(shortContext)
	shortCancel()
	holding.Release()
	if acquireErr == nil || poolError == nil {
		t.Fatalf("pool exhaustion error = %v, callback = %v", acquireErr, poolError)
	}

	callbackErr := errors.New("callback failure")
	if err := client.WithTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		if _, err := tx.Query(ctx, "SELECT 1"); err != nil {
			return err
		}
		return callbackErr
	}); err == nil || !errors.Is(err, callbackErr) {
		t.Fatalf("callback rollback error = %v", err)
	}
	if _, err := client.Query(ctx, "SELECT definitely_not_a_bursar_column"); err == nil {
		t.Fatal("invalid SQL query succeeded")
	}
	if err := client.WithTx(ctx, nil); err == nil {
		t.Fatal("nil transaction callback succeeded")
	}
}
