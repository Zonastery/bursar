// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import (
	"context"
	"strings"
	"testing"
	"time"
)

const postgresStateCoverageTenant = "00000000-0000-4000-8000-000000000001"

func TestPostgresBillingStateValidationCoverage(t *testing.T) {
	ctx := context.Background()
	store := (*PostgresStore)(nil)
	now := time.Date(2026, 8, 19, 1, 2, 3, 0, time.UTC)

	tests := []struct {
		name string
		call func() error
	}{
		{"customer provider", func() error {
			return store.UpsertBillingCustomer(ctx, BillingCustomerRecord{AccountID: postgresStateCoverageTenant, ProviderCustomerID: "cus"})
		}},
		{"customer id", func() error {
			return store.UpsertBillingCustomer(ctx, BillingCustomerRecord{AccountID: postgresStateCoverageTenant, Provider: "stripe"})
		}},
		{"customer uuid", func() error {
			return store.UpsertBillingCustomer(ctx, BillingCustomerRecord{AccountID: "bad", Provider: "stripe", ProviderCustomerID: "cus"})
		}},
		{"customer lookup account", func() error { _, err := store.GetBillingCustomer(ctx, "", "stripe"); return err }},
		{"customer lookup uuid", func() error { _, err := store.GetBillingCustomer(ctx, "bad", "stripe"); return err }},
		{"customer by provider", func() error { _, err := store.GetBillingCustomerByProvider(ctx, "", "cus"); return err }},
		{"customer by provider id", func() error { _, err := store.GetBillingCustomerByProvider(ctx, "stripe", ""); return err }},
		{"subscription lookup provider", func() error { _, err := store.GetBillingSubscriptionByProvider(ctx, "", "sub"); return err }},
		{"subscription lookup id", func() error { _, err := store.GetBillingSubscriptionByProvider(ctx, "stripe", ""); return err }},
		{"subscription account", func() error { _, err := store.GetBillingSubscription(ctx, "", nil); return err }},
		{"subscription uuid", func() error { _, err := store.GetBillingSubscription(ctx, "bad", nil); return err }},
		{"subscription list account", func() error { _, err := store.ListBillingSubscriptions(ctx, ""); return err }},
		{"payment lookup provider", func() error { _, err := store.GetBillingPaymentByProvider(ctx, "", "pi"); return err }},
		{"payment lookup id", func() error { _, err := store.GetBillingPaymentByProvider(ctx, "stripe", ""); return err }},
		{"payment account", func() error {
			_, err := store.UpsertBillingPaymentState(ctx, BillingPaymentUpsert{Provider: "stripe", ProviderPaymentID: "pi", Currency: "USD", Purpose: "subscription", Status: "succeeded", ProviderUpdatedAt: now})
			return err
		}},
		{"payment amount", func() error {
			_, err := store.UpsertBillingPaymentState(ctx, BillingPaymentUpsert{AccountID: postgresStateCoverageTenant, Provider: "stripe", ProviderPaymentID: "pi", AmountMinor: -1, Currency: "USD", Purpose: "subscription", Status: "succeeded", ProviderUpdatedAt: now})
			return err
		}},
		{"payment currency", func() error {
			_, err := store.UpsertBillingPaymentState(ctx, BillingPaymentUpsert{AccountID: postgresStateCoverageTenant, Provider: "stripe", ProviderPaymentID: "pi", Currency: "US", Purpose: "subscription", Status: "succeeded", ProviderUpdatedAt: now})
			return err
		}},
		{"payment purpose", func() error {
			_, err := store.UpsertBillingPaymentState(ctx, BillingPaymentUpsert{AccountID: postgresStateCoverageTenant, Provider: "stripe", ProviderPaymentID: "pi", Currency: "USD", Purpose: "other", Status: "succeeded", ProviderUpdatedAt: now})
			return err
		}},
		{"payment status", func() error {
			_, err := store.UpsertBillingPaymentState(ctx, BillingPaymentUpsert{AccountID: postgresStateCoverageTenant, Provider: "stripe", ProviderPaymentID: "pi", Currency: "USD", Purpose: "subscription", Status: "other", ProviderUpdatedAt: now})
			return err
		}},
		{"payment time", func() error {
			_, err := store.UpsertBillingPaymentState(ctx, BillingPaymentUpsert{AccountID: postgresStateCoverageTenant, Provider: "stripe", ProviderPaymentID: "pi", Currency: "USD", Purpose: "subscription", Status: "succeeded"})
			return err
		}},
		{"payment uuid", func() error {
			_, err := store.UpsertBillingPaymentState(ctx, BillingPaymentUpsert{AccountID: "bad", Provider: "stripe", ProviderPaymentID: "pi", Currency: "USD", Purpose: "subscription", Status: "succeeded", ProviderUpdatedAt: now})
			return err
		}},
		{"topup provider", func() error { _, err := store.ResolveBillingTopup(ctx, "", "product", "", ""); return err }},
		{"grant credits", func() error {
			_, err := store.CreateBillingCreditGrant(ctx, BillingCreditGrantCreate{Quantity: 1})
			return err
		}},
		{"grant quantity", func() error {
			_, err := store.CreateBillingCreditGrant(ctx, BillingCreditGrantCreate{Credits: MustAmount("1")})
			return err
		}},
		{"grant source", func() error {
			_, err := store.CreateBillingCreditGrant(ctx, BillingCreditGrantCreate{Credits: MustAmount("1"), Quantity: 1})
			return err
		}},
		{"grant payment uuid", func() error {
			_, err := store.CreateBillingCreditGrant(ctx, BillingCreditGrantCreate{Credits: MustAmount("1"), Quantity: 1, PaymentID: "bad"})
			return err
		}},
		{"grant id", func() error { _, err := store.GrantBillingCredit(ctx, "", "key"); return err }},
		{"grant key", func() error { _, err := store.GrantBillingCredit(ctx, postgresStateCoverageTenant, " "); return err }},
		{"grant uuid", func() error { _, err := store.GrantBillingCredit(ctx, "bad", "key"); return err }},
		{"grant lookup", func() error { _, err := store.GetBillingCreditGrantByPayment(ctx, ""); return err }},
		{"grant lookup uuid", func() error { _, err := store.GetBillingCreditGrantByPayment(ctx, "bad"); return err }},
		{"refund provider", func() error { _, err := store.UpsertBillingRefundState(ctx, BillingRefundUpsert{}); return err }},
		{"refund amount", func() error {
			_, err := store.UpsertBillingRefundState(ctx, BillingRefundUpsert{Provider: "stripe", ProviderRefundID: "re", ProviderPaymentID: "pi", AccountID: postgresStateCoverageTenant, AmountMinor: 0, Currency: "USD", Status: "succeeded", ProviderUpdatedAt: now})
			return err
		}},
		{"refund currency", func() error {
			_, err := store.UpsertBillingRefundState(ctx, BillingRefundUpsert{Provider: "stripe", ProviderRefundID: "re", ProviderPaymentID: "pi", AccountID: postgresStateCoverageTenant, AmountMinor: 1, Currency: "US", Status: "succeeded", ProviderUpdatedAt: now})
			return err
		}},
		{"refund posting id", func() error {
			_, err := store.PostBillingRefund(ctx, "", postgresStateCoverageTenant, 1, "key")
			return err
		}},
		{"refund posting amount", func() error {
			_, err := store.PostBillingRefund(ctx, postgresStateCoverageTenant, postgresStateCoverageTenant, 0, "key")
			return err
		}},
		{"refund posting key", func() error {
			_, err := store.PostBillingRefund(ctx, postgresStateCoverageTenant, postgresStateCoverageTenant, 1, " ")
			return err
		}},
		{"invoice provider", func() error { return store.UpsertBillingInvoiceState(ctx, BillingInvoiceUpsert{}) }},
		{"invoice status", func() error {
			return store.UpsertBillingInvoiceState(ctx, BillingInvoiceUpsert{Provider: "stripe", ProviderInvoiceID: "in", AccountID: postgresStateCoverageTenant, Status: "pending", Currency: "USD", ProviderUpdatedAt: now})
		}},
		{"invoice amount", func() error {
			return store.UpsertBillingInvoiceState(ctx, BillingInvoiceUpsert{Provider: "stripe", ProviderInvoiceID: "in", AccountID: postgresStateCoverageTenant, Status: "paid", AmountPaidMinor: -1, Currency: "USD", ProviderUpdatedAt: now})
		}},
		{"invoice currency", func() error {
			return store.UpsertBillingInvoiceState(ctx, BillingInvoiceUpsert{Provider: "stripe", ProviderInvoiceID: "in", AccountID: postgresStateCoverageTenant, Status: "paid", Currency: "US", ProviderUpdatedAt: now})
		}},
		{"dispute provider", func() error { return store.UpsertBillingDisputeState(ctx, BillingDisputeUpsert{}) }},
		{"dispute status", func() error {
			return store.UpsertBillingDisputeState(ctx, BillingDisputeUpsert{Provider: "stripe", ProviderDisputeID: "d", ProviderPaymentID: "pi", Status: "pending", ProviderUpdatedAt: now})
		}},
		{"conflict provider", func() error { return store.RecordSubscriptionConflict(ctx, BillingSubscriptionConflictCreate{}) }},
		{"conflict uuid", func() error {
			return store.RecordSubscriptionConflict(ctx, BillingSubscriptionConflictCreate{Provider: "stripe", DuplicateProviderSubscriptionID: "sub", AccountID: "bad"})
		}},
		{"entitlement account", func() error {
			_, err := store.SelectSubscriptionEntitlementSource(ctx, "", postgresStateCoverageTenant)
			return err
		}},
		{"entitlement id", func() error {
			_, err := store.SelectSubscriptionEntitlementSource(ctx, postgresStateCoverageTenant, "bad")
			return err
		}},
		{"pseudonymize account", func() error { _, err := store.PseudonymizeFinancialSubject(ctx, ""); return err }},
		{"pseudonymize uuid", func() error { _, err := store.PseudonymizeFinancialSubject(ctx, "bad"); return err }},
		{"auto recharge provider", func() error {
			return store.UpdateAutoRechargeAttemptByProviderPayment(ctx, AutoRechargeProviderPaymentUpdate{})
		}},
		{"auto recharge payment", func() error {
			return store.UpdateAutoRechargeAttemptByProviderPayment(ctx, AutoRechargeProviderPaymentUpdate{Provider: "stripe"})
		}},
		{"grace limit", func() error { _, err := store.ExpirePastDueGracePeriods(ctx, now, 0); return err }},
		{"grace list limit", func() error { _, err := store.ListExpiredGraceSubscriptions(ctx, now, 1001); return err }},
		{"grace mark id", func() error { _, err := store.MarkSubscriptionGraceExpired(ctx, "bad", now, now); return err }},
		{"grace mark timestamps", func() error {
			_, err := store.MarkSubscriptionGraceExpired(ctx, postgresStateCoverageTenant, time.Time{}, now)
			return err
		}},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if err := test.call(); err == nil {
				t.Fatal("invalid input was accepted")
			}
		})
	}
	if result, err := store.ResolveBillingTopup(ctx, "stripe", "", "", ""); err != nil || result != nil {
		t.Fatalf("empty top-up lookup = %#v, %v", result, err)
	}

	if err := billingLifecyclePostingError("grant", BillingCreditPostingResult{}); err != nil {
		t.Fatalf("empty posting error = %v", err)
	}
	if err := billingLifecyclePostingError("grant", BillingCreditPostingResult{ErrorCode: "insufficient_balance"}); err == nil {
		t.Fatal("posting error code was ignored")
	}
}

func TestPostgresCommerceStateValidationCoverage(t *testing.T) {
	ctx := context.Background()
	store := (*PostgresStore)(nil)
	now := time.Date(2026, 8, 19, 1, 2, 3, 0, time.UTC)

	for _, test := range []struct {
		name string
		call func() error
	}{
		{"offer provider", func() error { _, err := store.ResolveBillingOffer(ctx, "", "product", "", ""); return err }},
		{"change provider", func() error {
			_, err := store.CreateBillingSubscriptionChange(ctx, BillingSubscriptionChangeCreate{})
			return err
		}},
		{"change subscription", func() error {
			_, err := store.CreateBillingSubscriptionChange(ctx, BillingSubscriptionChangeCreate{Provider: "stripe"})
			return err
		}},
		{"change target", func() error {
			_, err := store.CreateBillingSubscriptionChange(ctx, BillingSubscriptionChangeCreate{Provider: "stripe", ProviderSubscriptionID: "sub"})
			return err
		}},
		{"change key", func() error {
			_, err := store.CreateBillingSubscriptionChange(ctx, BillingSubscriptionChangeCreate{Provider: "stripe", ProviderSubscriptionID: "sub", ToOfferID: postgresStateCoverageTenant, OperationKey: " "})
			return err
		}},
		{"change time", func() error {
			_, err := store.CreateBillingSubscriptionChange(ctx, BillingSubscriptionChangeCreate{Provider: "stripe", ProviderSubscriptionID: "sub", ToOfferID: postgresStateCoverageTenant, OperationKey: "key"})
			return err
		}},
		{"change effective", func() error {
			_, err := store.CreateBillingSubscriptionChange(ctx, BillingSubscriptionChangeCreate{Provider: "stripe", ProviderSubscriptionID: "sub", ToOfferID: postgresStateCoverageTenant, OperationKey: "key", EffectiveAt: now, Effective: "later"})
			return err
		}},
		{"change proration", func() error {
			_, err := store.CreateBillingSubscriptionChange(ctx, BillingSubscriptionChangeCreate{Provider: "stripe", ProviderSubscriptionID: "sub", ToOfferID: postgresStateCoverageTenant, OperationKey: "key", EffectiveAt: now, Effective: "immediate", ProrationBehavior: "later"})
			return err
		}},
		{"change target uuid", func() error {
			_, err := store.CreateBillingSubscriptionChange(ctx, BillingSubscriptionChangeCreate{Provider: "stripe", ProviderSubscriptionID: "sub", ToOfferID: "bad", OperationKey: "key", EffectiveAt: now, Effective: "immediate"})
			return err
		}},
		{"open change provider", func() error { _, err := store.GetOpenBillingSubscriptionChange(ctx, "", "sub"); return err }},
		{"open change id", func() error { _, err := store.GetOpenBillingSubscriptionChange(ctx, "stripe", ""); return err }},
		{"update id", func() error { return store.UpdateBillingSubscriptionChange(ctx, "", BillingSubscriptionChangeUpdate{}) }},
		{"update id value", func() error {
			state := "applied"
			return store.UpdateBillingSubscriptionChange(ctx, "bad", BillingSubscriptionChangeUpdate{State: &state})
		}},
		{"preferences account", func() error { _, err := store.GetBillingPreferences(ctx, ""); return err }},
		{"preferences uuid", func() error { _, err := store.GetBillingPreferences(ctx, "bad"); return err }},
		{"preferences upsert account", func() error { return store.UpsertBillingPreferences(ctx, BillingPreferences{}) }},
		{"invoice list account", func() error { _, err := store.ListBillingInvoices(ctx, ""); return err }},
		{"invoice list uuid", func() error { _, err := store.ListBillingInvoices(ctx, "bad"); return err }},
		{"auto topup provider", func() error { _, err := store.ResolveAutoRechargeTopup(ctx, AutoRechargeTopupLookup{}); return err }},
		{"auto customer user", func() error { _, err := store.GetAutoRechargeCustomer(ctx, "", "stripe"); return err }},
		{"auto customer provider", func() error {
			_, err := store.GetAutoRechargeCustomer(ctx, postgresStateCoverageTenant, "")
			return err
		}},
		{"auto profile user", func() error { _, err := store.GetAutoRechargeProfile(ctx, ""); return err }},
		{"auto profile uuid", func() error { _, err := store.GetAutoRechargeProfile(ctx, "bad"); return err }},
		{"auto profile upsert user", func() error {
			return store.UpsertAutoRechargeProfile(ctx, AutoRechargeProfile{}, AutoRechargeProfileUpsertOptions{})
		}},
		{"auto profile enabled state", func() error {
			return store.UpsertAutoRechargeProfile(ctx, AutoRechargeProfile{UserID: postgresStateCoverageTenant, Enabled: true, State: AutoRechargeStateDisabled}, AutoRechargeProfileUpsertOptions{})
		}},
		{"auto profile enabled fields", func() error {
			return store.UpsertAutoRechargeProfile(ctx, AutoRechargeProfile{UserID: postgresStateCoverageTenant, Enabled: true, State: AutoRechargeStateActive, Provider: "stripe", TopupID: "bad"}, AutoRechargeProfileUpsertOptions{})
		}},
		{"auto attempt user", func() error { _, err := store.ClaimAutoRechargeAttempt(ctx, AutoRechargeAttemptClaim{}); return err }},
		{"auto attempt key", func() error {
			_, err := store.ClaimAutoRechargeAttempt(ctx, AutoRechargeAttemptClaim{UserID: postgresStateCoverageTenant})
			return err
		}},
		{"auto update id", func() error { return store.UpdateAutoRechargeAttempt(ctx, AutoRechargeAttemptUpdate{}) }},
		{"auto update state", func() error {
			return store.UpdateAutoRechargeAttempt(ctx, AutoRechargeAttemptUpdate{ID: postgresStateCoverageTenant, State: "bad"})
		}},
		{"auto update message", func() error {
			return store.UpdateAutoRechargeAttempt(ctx, AutoRechargeAttemptUpdate{ID: postgresStateCoverageTenant, State: AutoRechargeAttemptSucceeded, FailureMessage: strings.Repeat("x", 8193)})
		}},
		{"auto update uuid", func() error {
			return store.UpdateAutoRechargeAttempt(ctx, AutoRechargeAttemptUpdate{ID: "bad", State: AutoRechargeAttemptSucceeded})
		}},
		{"auto count user", func() error { _, err := store.CountAutoRechargeAttempts(ctx, "", now); return err }},
		{"auto count time", func() error {
			_, err := store.CountAutoRechargeAttempts(ctx, postgresStateCoverageTenant, time.Time{})
			return err
		}},
	} {
		t.Run(test.name, func(t *testing.T) {
			if err := test.call(); err == nil {
				t.Fatal("invalid input was accepted")
			}
		})
	}
	if result, err := store.ResolveBillingOffer(ctx, "stripe", "", "", ""); err != nil || result != nil {
		t.Fatalf("empty offer lookup = %#v, %v", result, err)
	}

	for _, state := range []AutoRechargeAttemptState{AutoRechargeAttemptClaimed, AutoRechargeAttemptSubmitted, AutoRechargeAttemptProcessing, AutoRechargeAttemptUnknown, AutoRechargeAttemptSucceeded, AutoRechargeAttemptFailed, AutoRechargeAttemptActionRequired} {
		if !commerceStateAutoRechargeAttemptState(state) {
			t.Errorf("valid auto-recharge attempt state %q rejected", state)
		}
	}
	if commerceStateAutoRechargeAttemptState("invalid") {
		t.Fatal("invalid auto-recharge attempt state accepted")
	}
	for _, transition := range []struct {
		from, to AutoRechargeAttemptState
		want     int
	}{
		{AutoRechargeAttemptClaimed, AutoRechargeAttemptProcessing, 2},
		{AutoRechargeAttemptSubmitted, AutoRechargeAttemptActionRequired, 1},
		{AutoRechargeAttemptUnknown, AutoRechargeAttemptSucceeded, 1},
		{AutoRechargeAttemptActionRequired, AutoRechargeAttemptFailed, 1},
		{AutoRechargeAttemptSucceeded, AutoRechargeAttemptFailed, -1},
	} {
		path, err := commerceStateAutoRechargeTransitionPath(transition.from, transition.to)
		if transition.want < 0 {
			if err == nil {
				t.Errorf("invalid transition %s -> %s accepted", transition.from, transition.to)
			}
		} else if err != nil || len(path) != transition.want {
			t.Errorf("transition %s -> %s = %#v, %v", transition.from, transition.to, path, err)
		}
	}
}

func TestPostgresCommerceStateAutoRechargeMappersFailClosed(t *testing.T) {
	now := time.Date(2026, 8, 19, 1, 2, 3, 0, time.UTC)
	profile := map[string]any{
		"subject_id": postgresStateCoverageTenant, "enabled": true, "armed": true, "state": string(AutoRechargeStateActive),
		"provider": "stripe", "topup_id": postgresStateCoverageTenant, "quantity": int64(2), "threshold": "1.25",
		"max_charges_per_window": int64(3), "window_unit": "month", "window_count": int64(1), "window_anchor": "calendar",
		"window_timezone": "UTC", "updated_at": now,
	}
	mapped, err := commerceStateAutoRechargeProfileFromRow(profile, "profile")
	if err != nil || mapped.UserID != postgresStateCoverageTenant || mapped.Quantity != 2 || !mapped.Threshold.Equal(MustAmount("1.25")) {
		t.Fatalf("auto-recharge profile = %#v, %v", mapped, err)
	}
	disabled := cloneAnyMap(profile)
	disabled["enabled"], disabled["state"] = false, string(AutoRechargeStateDisabled)
	if mapped, err := commerceStateAutoRechargeProfileFromRow(disabled, "profile"); err != nil || mapped.Enabled {
		t.Fatalf("disabled auto-recharge profile = %#v, %v", mapped, err)
	}
	for _, mutate := range []func(map[string]any){
		func(row map[string]any) { row["state"] = "invalid" },
		func(row map[string]any) { row["enabled"] = false },
		func(row map[string]any) { row["provider"] = nil },
		func(row map[string]any) { row["quantity"] = int64(0) },
		func(row map[string]any) { row["threshold"] = "-1" },
		func(row map[string]any) { row["window_anchor"] = "invalid" },
		func(row map[string]any) { row["updated_at"] = "bad" },
	} {
		row := cloneAnyMap(profile)
		mutate(row)
		if _, err := commerceStateAutoRechargeProfileFromRow(row, "profile"); err == nil {
			t.Fatalf("malformed auto-recharge profile accepted: %#v", row)
		}
	}

	attempt := map[string]any{
		"id": postgresStateCoverageTenant, "subject_id": postgresStateCoverageTenant, "provider": "stripe", "idempotency_key": "attempt-key",
		"provider_attempt_id": "provider-attempt", "topup_id": postgresStateCoverageTenant, "quantity": int64(2),
		"state": string(AutoRechargeAttemptSucceeded), "window_start": now, "window_end": now.Add(time.Hour),
		"quoted_amount_minor": int64(250), "currency": "usd", "metadata": `{"source":"test"}`,
		"created_at": now, "updated_at": now,
	}
	mappedAttempt, err := commerceStateAutoRechargeAttemptFromRow(attempt, "attempt")
	if err != nil || mappedAttempt.QuotedAmountMinor == nil || *mappedAttempt.QuotedAmountMinor != 250 || mappedAttempt.Currency != "USD" {
		t.Fatalf("auto-recharge attempt = %#v, %v", mappedAttempt, err)
	}
	for _, mutate := range []func(map[string]any){
		func(row map[string]any) { row["quantity"] = int64(0) },
		func(row map[string]any) { row["state"] = "invalid" },
		func(row map[string]any) { row["window_end"] = now },
		func(row map[string]any) { row["currency"] = "US" },
		func(row map[string]any) { row["currency"] = nil },
		func(row map[string]any) { row["metadata"] = "[]" },
	} {
		row := cloneAnyMap(attempt)
		mutate(row)
		if _, err := commerceStateAutoRechargeAttemptFromRow(row, "attempt"); err == nil {
			t.Fatalf("malformed auto-recharge attempt accepted: %#v", row)
		}
	}
}
