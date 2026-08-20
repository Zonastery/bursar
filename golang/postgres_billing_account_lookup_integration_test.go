// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import (
	"testing"
	"time"

	"github.com/google/uuid"
)

// TestPostgresBillingEventAccountLookup exercises the durable provider-key
// fallbacks used by webhooks when providers omit Bursar's account metadata.
// The same rows are queried through the second tenant to prove that account
// resolution remains RLS-scoped rather than becoming a global provider lookup.
func TestPostgresBillingEventAccountLookup(t *testing.T) {
	ctx, cancel, config := financialContext(t)
	defer cancel()
	store := openPostgresIntegrationStore(t, ctx, config, config.tenantID)
	defer store.Close()

	accountID := uuid.NewString()
	runID := uuid.NewString()
	provider := "alpha"
	customerID := "lookup-cus-" + runID
	if err := store.UpsertBillingCustomer(ctx, BillingCustomerRecord{
		AccountID: accountID, Provider: provider, ProviderCustomerID: customerID,
		Email: "lookup@example.test",
	}); err != nil {
		t.Fatalf("upsert lookup customer: %v", err)
	}

	customerEvent := BillingEvent{Provider: provider, Customer: &BillingCustomer{ProviderCustomerID: customerID}}
	if got, err := store.ResolveBillingEventAccount(ctx, customerEvent); err != nil || got != accountID {
		t.Fatalf("customer-key account = %q, error = %v; want %q", got, err, accountID)
	}
	processor := postgresBillingLifecycle{store: store}
	if got, err := processor.resolveAccount(ctx, customerEvent, ""); err != nil || got != accountID {
		t.Fatalf("lifecycle customer-key account = %q, error = %v; want %q", got, err, accountID)
	}

	paymentID := "lookup-pay-" + runID
	if _, err := store.UpsertBillingPaymentState(ctx, BillingPaymentUpsert{
		Provider: provider, ProviderPaymentID: paymentID, AccountID: accountID,
		AmountMinor: 1250, Currency: "USD", Purpose: "subscription", Status: "succeeded",
		ProviderUpdatedAt: time.Now().UTC(),
	}); err != nil {
		t.Fatalf("upsert lookup payment: %v", err)
	}
	for name, event := range map[string]BillingEvent{
		"payment": {Provider: provider, Payment: &BillingPayment{ProviderPaymentID: paymentID}},
		"refund":  {Provider: provider, Refund: &BillingRefund{ProviderPaymentID: paymentID}},
		"dispute": {Provider: provider, Dispute: &BillingDispute{ProviderPaymentID: paymentID}},
	} {
		t.Run(name, func(t *testing.T) {
			if got, err := store.ResolveBillingEventAccount(ctx, event); err != nil || got != accountID {
				t.Fatalf("provider payment account = %q, error = %v; want %q", got, err, accountID)
			}
			if got, err := processor.resolveAccount(ctx, event, ""); err != nil || got != accountID {
				t.Fatalf("lifecycle provider-payment account = %q, error = %v; want %q", got, err, accountID)
			}
		})
	}

	secondary := openPostgresIntegrationStore(t, ctx, config, config.secondaryTenantID)
	defer secondary.Close()
	if got, err := secondary.ResolveBillingEventAccount(ctx, customerEvent); err != nil || got != "" {
		t.Fatalf("cross-tenant customer lookup = %q, error = %v; want empty account", got, err)
	}
	if got, err := secondary.ResolveBillingEventAccount(ctx, BillingEvent{Provider: provider, Payment: &BillingPayment{ProviderPaymentID: paymentID}}); err != nil || got != "" {
		t.Fatalf("cross-tenant payment lookup = %q, error = %v; want empty account", got, err)
	}
}
