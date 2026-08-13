// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package main

import (
	"context"
	"fmt"
	"os"
	"time"

	bursar "github.com/Zonastery/bursar/golang/v2"
)

const subjectID = "00000000-0000-4000-8000-000000000212"

func main() {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	store, err := bursar.NewPostgresStore(ctx, os.Getenv("DATABASE_URL"), bursar.PostgresStoreOptions{
		TenantID:            os.Getenv("BURSAR_TENANT_ID"),
		ProviderEnvironment: bursar.ProviderEnvironmentTest,
	})
	if err != nil {
		panic(err)
	}
	defer store.Close()

	sdk, err := bursar.New(bursar.Options{CreditStore: store, BillingStore: store})
	if err != nil {
		panic(err)
	}
	if err := sdk.LoadCatalog(ctx); err != nil {
		panic(err)
	}
	billing, err := sdk.RequireBilling()
	if err != nil {
		panic(err)
	}
	verifyBillingLifecycle(ctx, sdk, billing)

	grant := bursar.AddCreditsOptions{
		Type:           "purchase",
		IdempotencyKey: "package-smoke:go:grant",
	}
	first, err := sdk.Credits.AddCredits(ctx, subjectID, bursar.MustAmount("7"), grant)
	if err != nil {
		panic(err)
	}
	replay, err := sdk.Credits.AddCredits(ctx, subjectID, bursar.MustAmount("7"), grant)
	if err != nil {
		panic(err)
	}
	if replay.EntryID != first.EntryID || !replay.Idempotent {
		panic("Go grant did not replay idempotently")
	}
	if !first.Amount.Equal(bursar.MustAmount("7")) || !replay.Amount.Equal(first.Amount) {
		panic(fmt.Sprintf("unexpected Go package-smoke grant amounts: first=%s replay=%s", first.Amount, replay.Amount))
	}
	balance, err := sdk.Credits.GetBalance(ctx, subjectID)
	if err != nil {
		panic(err)
	}
	if !balance.Balance.Equal(first.NewBalance) || !replay.NewBalance.Equal(first.NewBalance) {
		panic(fmt.Sprintf("Go package-smoke balance disagrees with grant: balance=%s first=%s replay=%s", balance.Balance, first.NewBalance, replay.NewBalance))
	}
}

func verifyBillingLifecycle(ctx context.Context, sdk *bursar.Bursar, billing *bursar.BillingService) {
	const (
		provider           = "stripe"
		providerCustomerID = "cus_bursar_package_smoke_go"
	)
	event := bursar.BillingEvent{
		EventID:    "evt_bursar_package_smoke_go_customer_created_v4",
		Provider:   provider,
		Type:       bursar.BillingEventCustomerCreated,
		OccurredAt: time.Date(2026, time.August, 13, 0, 0, 0, 0, time.UTC),
		AccountID:  subjectID,
		Customer: &bursar.BillingCustomer{
			ProviderCustomerID: providerCustomerID,
			AccountID:          subjectID,
			Email:              "go-package-smoke@example.com",
		},
	}

	first, err := sdk.IngestBillingEvent(ctx, event)
	if err != nil {
		panic(err)
	}
	if !first.Handled || first.Retryable {
		panic(fmt.Sprintf("unexpected first customer lifecycle result: %+v", first))
	}
	// A package-smoke job starts from a fresh database and exercises the first
	// branch. Accepting an already-completed delivery keeps this standalone
	// fixture safe to rerun against the same isolated tenant.
	if !first.Duplicate && (first.Action != "customer_created" || first.AccountID != subjectID) {
		panic(fmt.Sprintf("unexpected new customer lifecycle result: %+v", first))
	}
	customer, err := billing.GetCustomerByUserID(ctx, subjectID, provider)
	if err != nil {
		panic(err)
	}
	if customer == nil || customer.ProviderCustomerID != providerCustomerID || customer.AccountID != subjectID || customer.Email != event.Customer.Email {
		panic(fmt.Sprintf("unexpected persisted billing customer: %+v", customer))
	}

	replay, err := sdk.IngestBillingEvent(ctx, event)
	if err != nil {
		panic(err)
	}
	if !replay.Handled || !replay.Duplicate || replay.Retryable {
		panic(fmt.Sprintf("customer lifecycle replay was not idempotent: %+v", replay))
	}
}
