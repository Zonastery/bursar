// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import (
	"context"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
)

type graceNoAccountCounts struct {
	personalAccounts int64
	ledgerEntries    int64
	grantAwards      int64
}

func TestPostgresGraceExpiryWithoutAccountDoesNotProvisionOrGrant(t *testing.T) {
	ctx, cancel, config := financialContext(t)
	defer cancel()
	sdk, store := newFinancialSDK(t, ctx, config, &financialProvider{name: "alpha"})

	baseline := financialCatalogConfig(t)
	defer func() {
		if _, err := sdk.Catalog.PublishAndActivate(ctx, baseline, "grace-no-account-restore", newAssignmentsRollout(baseline)); err != nil {
			t.Errorf("restore catalog after accountless grace test: %v", err)
		}
	}()

	catalog := financialCatalogConfig(t)
	credits := catalog["credits"].(map[string]any)
	credits["grant_programs"] = map[string]any{
		"account_welcome": map[string]any{
			"trigger": "account_created",
			"awards":  []any{map[string]any{"amount": "7.000000", "bucket": "general"}},
		},
	}
	if _, err := sdk.Catalog.PublishAndActivate(ctx, catalog, "grace-no-account", newAssignmentsRollout(catalog)); err != nil {
		t.Fatalf("publish account-created grant catalog: %v", err)
	}

	unsetSubjectID := uuid.NewString()
	if _, err := sdk.Credits.UnsetUserPlan(ctx, unsetSubjectID); err != nil {
		t.Fatalf("unset plan for accountless subject: %v", err)
	}
	if counts := queryGraceNoAccountCounts(t, ctx, config, unsetSubjectID); counts != (graceNoAccountCounts{}) {
		t.Fatalf("unset plan provisioned accountless subject: %+v", counts)
	}

	subjectID := uuid.NewString()
	subscriptionID := "sub-grace-no-account-" + uuid.NewString()
	now := time.Now().UTC().Truncate(time.Second)
	periodEnd := now.Add(30 * 24 * time.Hour)
	graceEndsAt := now.Add(-time.Minute)
	offer, err := store.ResolveBillingOffer(ctx, "alpha", "", "alpha-pro-month", "")
	if err != nil || offer == nil {
		t.Fatalf("resolve subscription offer = %+v, error = %v", offer, err)
	}
	if _, err := store.UpsertBillingSubscriptionState(ctx, CommerceSubscription{
		AccountID: subjectID, Provider: "alpha", ProviderSubscriptionID: subscriptionID,
		ProviderCustomerID: "cus-" + uuid.NewString(), OfferID: offer.ID, Status: "past_due",
		CurrentPeriodStart: &now, CurrentPeriodEnd: &periodEnd, GraceEndsAt: &graceEndsAt,
		ProviderUpdatedAt: now,
	}); err != nil {
		t.Fatalf("persist past-due subscription without account: %v", err)
	}

	countsBefore := queryGraceNoAccountCounts(t, ctx, config, subjectID)
	if countsBefore != (graceNoAccountCounts{}) {
		t.Fatalf("pre-expiry financial rows for accountless subject = %+v", countsBefore)
	}

	expired, err := store.ExpirePastDueGracePeriods(ctx, now, 10)
	if err != nil {
		t.Fatalf("expire accountless grace period: %v", err)
	}
	if expired != 1 {
		t.Fatalf("expired accountless grace periods = %d, want 1", expired)
	}
	updated, err := store.GetBillingSubscriptionByProvider(ctx, "alpha", subscriptionID)
	if err != nil || updated == nil || updated.GraceExpiredAt == nil {
		t.Fatalf("expired accountless subscription = %+v, error = %v", updated, err)
	}

	countsAfter := queryGraceNoAccountCounts(t, ctx, config, subjectID)
	if countsAfter != (graceNoAccountCounts{}) {
		t.Fatalf("post-expiry financial rows for accountless subject = %+v", countsAfter)
	}
}

func queryGraceNoAccountCounts(t *testing.T, ctx context.Context, config postgresIntegrationConfig, subjectID string) graceNoAccountCounts {
	t.Helper()
	connection, err := pgx.Connect(ctx, config.databaseURL)
	if err != nil {
		t.Fatalf("connect to inspect accountless grace rows: %v", err)
	}
	defer func() { _ = connection.Close(ctx) }()
	var counts graceNoAccountCounts
	err = connection.QueryRow(ctx, `
		SELECT
			(SELECT count(*)
			 FROM bursar.credit_accounts
			 WHERE tenant_id = $2::uuid
			   AND subject_id = $1::uuid
			   AND account_kind = 'personal'),
			(SELECT count(*)
			 FROM bursar.credit_ledger_entries AS entry
			 JOIN bursar.credit_accounts AS account
			   ON account.tenant_id = entry.tenant_id
			  AND account.id = entry.account_id
			 WHERE account.tenant_id = $2::uuid
			   AND account.subject_id = $1::uuid),
			(SELECT count(*)
			 FROM bursar.grant_award_executions
			 WHERE tenant_id = $2::uuid
			   AND recipient_subject_id = $1::uuid)
	`, subjectID, config.tenantID).Scan(&counts.personalAccounts, &counts.ledgerEntries, &counts.grantAwards)
	if err != nil {
		t.Fatalf("query accountless grace financial rows: %v", err)
	}
	return counts
}
