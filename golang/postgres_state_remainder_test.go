// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import (
	"context"
	"testing"
	"time"

	"github.com/google/uuid"
)

func TestPostgresCommerceStateAutoRechargeAndEmptyLookups(t *testing.T) {
	ctx, cancel, config := financialContext(t)
	defer cancel()
	sdk, store := newFinancialSDK(t, ctx, config, &financialProvider{name: "alpha"})
	accountID := uuid.NewString()
	providerCustomerID := "state-customer-" + uuid.NewString()

	if err := store.UpsertBillingCustomer(ctx, BillingCustomerRecord{
		AccountID: accountID, Provider: "alpha", ProviderCustomerID: providerCustomerID,
	}); err != nil {
		t.Fatalf("persist auto-recharge customer: %v", err)
	}
	customer, err := store.GetAutoRechargeCustomer(ctx, accountID, "alpha")
	if err != nil || customer == nil || customer.ProviderCustomerID != providerCustomerID {
		t.Fatalf("auto-recharge customer = %+v, error = %v", customer, err)
	}
	if missing, err := store.GetAutoRechargeCustomer(ctx, uuid.NewString(), "alpha"); err != nil || missing != nil {
		t.Fatalf("missing auto-recharge customer = %+v, error = %v", missing, err)
	}

	topup, err := store.ResolveBillingTopup(ctx, "alpha", "", "alpha-credit-pack", "")
	if err != nil || topup == nil {
		t.Fatalf("resolve auto-recharge top-up = %+v, error = %v", topup, err)
	}
	priceID := "alpha-credit-pack"
	resolved, err := store.ResolveAutoRechargeTopup(ctx, AutoRechargeTopupLookup{
		Provider: "alpha", OfferKey: "credit_pack", Reference: ProviderReference{PriceID: ScalarProviderID(priceID)},
	})
	if err != nil || resolved == nil || resolved.ID != topup.TopupID || resolved.ProductID != priceID {
		t.Fatalf("resolved auto-recharge top-up = %+v, error = %v", resolved, err)
	}
	wrongOffer, err := store.ResolveAutoRechargeTopup(ctx, AutoRechargeTopupLookup{
		Provider: "alpha", OfferKey: "not-credit-pack", Reference: ProviderReference{PriceID: ScalarProviderID(priceID)},
	})
	if err != nil || wrongOffer != nil {
		t.Fatalf("mismatched auto-recharge top-up = %+v, error = %v", wrongOffer, err)
	}
	if absent, err := store.ResolveAutoRechargeTopup(ctx, AutoRechargeTopupLookup{
		Provider: "alpha", OfferKey: "credit_pack", Reference: ProviderReference{PriceID: ScalarProviderID("missing-price")},
	}); err != nil || absent != nil {
		t.Fatalf("missing auto-recharge top-up = %+v, error = %v", absent, err)
	}

	profile := AutoRechargeProfile{
		UserID: accountID, Enabled: true, State: AutoRechargeStateActive, Armed: true,
		Provider: "alpha", TopupID: topup.TopupID, Quantity: 1, Threshold: MustAmount("100"),
		MaxChargesPerWindow: 3, WindowUnit: "day", WindowCount: 30, WindowAnchor: "rolling", WindowTimezone: "UTC",
	}
	if err := store.UpsertAutoRechargeProfile(ctx, profile, AutoRechargeProfileUpsertOptions{}); err != nil {
		t.Fatalf("persist enabled auto-recharge profile: %v", err)
	}
	gotProfile, err := store.GetAutoRechargeProfile(ctx, accountID)
	if err != nil || gotProfile == nil || gotProfile.Provider != "alpha" || gotProfile.State != AutoRechargeStateActive {
		t.Fatalf("enabled auto-recharge profile = %+v, error = %v", gotProfile, err)
	}

	// The exported compatibility option must use the same guarded resume path.
	if err := store.UpsertAutoRechargeProfile(ctx, profile, AutoRechargeProfileUpsertOptions{ResetCooldown: true}); err != nil {
		t.Fatalf("compatibility resume failed: %v", err)
	}
	invalidTopup := profile
	invalidTopup.TopupID = "not-a-uuid"
	if err := store.UpsertAutoRechargeProfile(ctx, invalidTopup, AutoRechargeProfileUpsertOptions{}); err == nil {
		t.Fatal("invalid enrollment top-up was accepted")
	}
	canceled, stop := context.WithCancel(ctx)
	stop()
	if err := store.UpsertAutoRechargeProfile(canceled, profile, AutoRechargeProfileUpsertOptions{ResetCooldown: true}); err == nil {
		t.Fatal("compatibility resume ignored cancellation")
	}
	if _, err := store.GetAutoRechargeOperationalWindow(canceled, accountID); err == nil {
		t.Fatal("operational window ignored cancellation")
	}
	window, err := store.GetAutoRechargeOperationalWindow(ctx, accountID)
	if err != nil || window == nil || window.MaxRecharges != 3 || window.Count != 0 || !window.End.After(window.Start) {
		t.Fatalf("initial operational window = %+v, error = %v", window, err)
	}
	if _, err := store.GetAutoRechargeOperationalWindow(ctx, "not-a-uuid"); err == nil {
		t.Fatal("invalid operational window subject was accepted")
	}
	if resumed, err := store.ResumeAutoRechargeProfile(ctx, profile); err != nil || !resumed {
		t.Fatalf("resume enabled profile = %v, error = %v", resumed, err)
	}
	wrongProfile := profile
	wrongProfile.Quantity = 2
	if resumed, err := store.ResumeAutoRechargeProfile(ctx, wrongProfile); err != nil || resumed {
		t.Fatalf("resume changed preferences = %v, error = %v", resumed, err)
	}
	wrongProfile.UserID = "bad"
	if _, err := store.ResumeAutoRechargeProfile(ctx, wrongProfile); err == nil {
		t.Fatal("invalid resume subject was accepted")
	}
	wrongProfile = profile
	wrongProfile.TopupID = "bad"
	if _, err := store.ResumeAutoRechargeProfile(ctx, wrongProfile); err == nil {
		t.Fatal("invalid resume topup was accepted")
	}

	attempt, err := store.ClaimAutoRechargeAttempt(ctx, AutoRechargeAttemptClaim{UserID: accountID, IdempotencyKey: "state-attempt-" + uuid.NewString(), ExpectedProductID: priceID, ExpectedQuantity: 1, ExpectedTopupKey: "credit_pack"})
	if err != nil || attempt == nil || attempt.State != AutoRechargeAttemptClaimed {
		t.Fatalf("claimed auto-recharge attempt = %+v, error = %v", attempt, err)
	}
	if err := store.UpdateAutoRechargeAttempt(ctx, AutoRechargeAttemptUpdate{ID: attempt.ID, State: AutoRechargeAttemptSucceeded, ProviderAttemptID: "provider-attempt-" + uuid.NewString()}); err != nil {
		t.Fatalf("advance auto-recharge attempt: %v", err)
	}
	if count, err := store.CountAutoRechargeAttempts(ctx, accountID, time.Now().UTC().Add(-time.Hour)); err != nil || count != 1 {
		t.Fatalf("auto-recharge attempt count = %d, error = %v", count, err)
	}
	window, err = store.GetAutoRechargeOperationalWindow(ctx, accountID)
	if err != nil || window == nil || window.Count != 1 {
		t.Fatalf("operational window after charge = %+v, error = %v", window, err)
	}
	if err := store.UpdateAutoRechargeAttemptByProviderPayment(ctx, AutoRechargeProviderPaymentUpdate{Provider: "alpha", ProviderPaymentID: "missing-payment-" + uuid.NewString(), State: AutoRechargeAttemptSucceeded}); err != nil {
		t.Fatalf("missing provider payment reconciliation: %v", err)
	}

	if err := store.UpsertAutoRechargeProfile(ctx, AutoRechargeProfile{UserID: accountID, State: AutoRechargeStateDisabled}, AutoRechargeProfileUpsertOptions{}); err != nil {
		t.Fatalf("disable auto-recharge profile: %v", err)
	}
	disabled, err := store.GetAutoRechargeProfile(ctx, accountID)
	if err != nil || disabled == nil || disabled.Enabled || disabled.State != AutoRechargeStateDisabled {
		t.Fatalf("disabled auto-recharge profile = %+v, error = %v", disabled, err)
	}

	if resumed, err := store.ResumeAutoRechargeProfile(ctx, profile); err != nil || resumed {
		t.Fatalf("resume disabled profile = %v, error = %v", resumed, err)
	}
	if err := store.UpsertAutoRechargeProfile(ctx, profile, AutoRechargeProfileUpsertOptions{ResetCooldown: true}); err == nil {
		t.Fatal("compatibility resume accepted a disabled profile")
	}

	if missingOffer, err := store.ResolveBillingOffer(ctx, "alpha", "", "missing-price-"+uuid.NewString(), ""); err != nil || missingOffer != nil {
		t.Fatalf("missing offer lookup = %+v, error = %v", missingOffer, err)
	}
	selectionAccount := uuid.NewString()
	offer, err := store.ResolveBillingOffer(ctx, "alpha", "", "alpha-pro-month", "")
	if err != nil || offer == nil {
		t.Fatalf("resolve selection offer = %+v, error = %v", offer, err)
	}
	selectionNow := time.Now().UTC().Add(-time.Minute).Truncate(time.Second)
	if _, err := store.UpsertBillingSubscriptionState(ctx, CommerceSubscription{
		AccountID: selectionAccount, Provider: "alpha", ProviderSubscriptionID: "selection-canceled-" + uuid.NewString(),
		OfferID: offer.ID, Status: "canceled", ProviderUpdatedAt: selectionNow,
	}); err != nil {
		t.Fatalf("persist canceled selection subscription: %v", err)
	}
	if _, err := store.UpsertBillingSubscriptionState(ctx, CommerceSubscription{
		AccountID: selectionAccount, Provider: "alpha", ProviderSubscriptionID: "selection-active-" + uuid.NewString(),
		OfferID: offer.ID, Status: "active", ProviderUpdatedAt: selectionNow.Add(time.Minute),
	}); err != nil {
		t.Fatalf("persist active selection subscription: %v", err)
	}
	selected, err := store.GetBillingSubscription(ctx, selectionAccount, nil)
	if err != nil || selected == nil || selected.Status != "active" {
		t.Fatalf("selected current subscription = %+v, error = %v", selected, err)
	}
	selectedRows, err := store.ListBillingSubscriptions(ctx, selectionAccount)
	if err != nil || len(selectedRows) != 2 {
		t.Fatalf("selected subscription rows = %+v, error = %v", selectedRows, err)
	}

	if subscription, err := store.GetBillingSubscription(ctx, uuid.NewString(), nil); err != nil || subscription != nil {
		t.Fatalf("empty subscription lookup = %+v, error = %v", subscription, err)
	}
	if subscriptions, err := store.ListBillingSubscriptions(ctx, uuid.NewString()); err != nil || len(subscriptions) != 0 {
		t.Fatalf("empty subscription list = %+v, error = %v", subscriptions, err)
	}
	if preferences, err := store.GetBillingPreferences(ctx, uuid.NewString()); err != nil || preferences != nil {
		t.Fatalf("empty preferences lookup = %+v, error = %v", preferences, err)
	}
	if invoices, err := store.ListBillingInvoices(ctx, uuid.NewString()); err != nil || len(invoices) != 0 {
		t.Fatalf("empty invoice list = %+v, error = %v", invoices, err)
	}
	if change, err := store.GetOpenBillingSubscriptionChange(ctx, "alpha", "missing-subscription-"+uuid.NewString()); err != nil || change != nil {
		t.Fatalf("empty open change lookup = %+v, error = %v", change, err)
	}

	withoutAutoRecharge := financialCatalogConfig(t)
	delete(withoutAutoRecharge["commerce"].(map[string]any), "auto_recharge")
	if _, err := sdk.Catalog.PublishAndActivate(ctx, withoutAutoRecharge, "go-no-recharge-policy", newAssignmentsRollout(withoutAutoRecharge)); err != nil {
		t.Fatalf("publish catalog without auto-recharge: %v", err)
	}
	if window, err := store.GetAutoRechargeOperationalWindow(ctx, accountID); err != nil || window != nil {
		t.Fatalf("window without policy = %+v, error = %v", window, err)
	}
}

func TestPostgresBillingStateRejectsInvalidDurableReferences(t *testing.T) {
	ctx, cancel, config := financialContext(t)
	defer cancel()
	_, store := newFinancialSDK(t, ctx, config, &financialProvider{name: "alpha"})
	accountID := uuid.NewString()
	now := time.Now().UTC().Truncate(time.Second)

	cases := []struct {
		name string
		call func() error
	}{
		{"subscription offer", func() error {
			_, err := store.UpsertBillingSubscriptionState(ctx, CommerceSubscription{AccountID: accountID, Provider: "alpha", ProviderSubscriptionID: "bad-offer-" + uuid.NewString(), OfferID: uuid.NewString(), Status: "active", ProviderUpdatedAt: now})
			return err
		}},
		{"credit grant payment", func() error {
			_, err := store.CreateBillingCreditGrant(ctx, BillingCreditGrantCreate{PaymentID: uuid.NewString(), Credits: MustAmount("1"), Quantity: 1})
			return err
		}},
	}
	for _, test := range cases {
		t.Run(test.name, func(t *testing.T) {
			if err := test.call(); err == nil {
				t.Fatal("invalid durable reference was accepted")
			}
		})
	}
}
