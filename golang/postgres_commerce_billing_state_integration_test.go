// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import (
	"testing"
	"time"

	"github.com/google/uuid"
)

func TestPostgresCheckoutIntentStateAndAuthorization(t *testing.T) {
	ctx, cancel, config := financialContext(t)
	defer cancel()
	_, store := newFinancialSDK(t, ctx, config, &financialProvider{name: "alpha"})

	subjectID := uuid.NewString()
	key := "go-commerce-state-checkout-" + uuid.NewString()
	input := CheckoutIntentCreate{
		SubjectID:      subjectID,
		AccountID:      uuid.NewString(),
		Provider:       "alpha",
		CheckoutKind:   "credit_topup",
		ProductKey:     "credit_pack",
		Quantity:       2,
		IdempotencyKey: key,
		ExpiresAt:      time.Now().UTC().Add(time.Hour),
		Region:         "us",
	}
	first, err := store.CreateOrGetCheckoutIntent(ctx, input)
	if err != nil || first.ID == "" || first.Status != "open" || first.SubjectID != subjectID {
		t.Fatalf("create checkout intent = %+v, error = %v", first, err)
	}
	replay, err := store.CreateOrGetCheckoutIntent(ctx, input)
	if err != nil || replay.ID != first.ID {
		t.Fatalf("checkout replay = %+v, first = %+v, error = %v", replay, first, err)
	}

	conflict := input
	conflict.ProductKey = "pro_month"
	if _, err := store.CreateOrGetCheckoutIntent(ctx, conflict); err == nil {
		t.Fatal("checkout idempotency key accepted a different request")
	}
	if got, err := store.GetCheckoutIntent(ctx, first.ID, uuid.NewString()); err != nil {
		t.Fatalf("unauthorized checkout lookup error = %v", err)
	} else if got != nil {
		t.Fatalf("unauthorized checkout lookup returned %+v", got)
	}
	got, err := store.GetCheckoutIntent(ctx, first.ID, subjectID)
	if err != nil || got == nil || got.ID != first.ID || got.RequestDigest == "" {
		t.Fatalf("authorized checkout lookup = %+v, error = %v", got, err)
	}

	if err := store.UpdateCheckoutIntent(ctx, first.ID, subjectID, CheckoutIntentUpdate{
		ProviderSessionID: "session-" + uuid.NewString(),
		ProviderURL:       "https://checkout.example/session",
		Status:            "open",
	}); err != nil {
		t.Fatalf("attach checkout session: %v", err)
	}
	if err := store.UpdateCheckoutIntent(ctx, first.ID, subjectID, CheckoutIntentUpdate{Status: "completed"}); err != nil {
		t.Fatalf("complete checkout intent: %v", err)
	}
	if err := store.UpdateCheckoutIntent(ctx, first.ID, subjectID, CheckoutIntentUpdate{Status: "failed"}); err == nil {
		t.Fatal("terminal checkout transition accepted")
	}
	if err := store.UpdateCheckoutIntent(ctx, first.ID, subjectID, CheckoutIntentUpdate{Status: "invalid"}); err == nil {
		t.Fatal("invalid checkout status accepted")
	}
}

func TestPostgresCommerceAndBillingStateSurface(t *testing.T) {
	ctx, cancel, config := financialContext(t)
	defer cancel()
	sdk, store := newFinancialSDK(t, ctx, config, &financialProvider{name: "alpha"})

	// Add a second active offer so the durable change flow exercises a real
	// target transition rather than merely repeating the current offer.
	activeConfig := financialCatalogConfig(t)
	commerce := activeConfig["commerce"].(map[string]any)
	offers := commerce["offers"].(map[string]any)
	month := offers["pro_month"].(map[string]any)
	year := make(map[string]any, len(month))
	for key, value := range month {
		year[key] = value
	}
	year["display_name"] = "Pro yearly"
	year["billing_interval"] = map[string]any{"unit": "year", "count": 1}
	year["price"] = map[string]any{"amount_minor": 20000, "currency": "USD"}
	year["providers"] = map[string]any{"alpha": map[string]any{"type": "stripe_price", "price_id": "alpha-pro-year"}}
	offers["pro_year"] = year
	if _, err := sdk.Catalog.PublishAndActivate(ctx, activeConfig, "go-commerce-state-second-offer", newAssignmentsRollout(activeConfig)); err != nil {
		t.Fatalf("publish second offer catalog: %v", err)
	}

	accountID := uuid.NewString()
	providerCustomerID := "cus-" + uuid.NewString()
	if err := store.UpsertBillingCustomer(ctx, BillingCustomerRecord{
		AccountID: accountID, Provider: "alpha", ProviderCustomerID: providerCustomerID, Email: "state@example.com",
	}); err != nil {
		t.Fatalf("upsert billing customer: %v", err)
	}
	customer, err := store.GetBillingCustomer(ctx, accountID, "alpha")
	if err != nil || customer == nil || customer.ProviderCustomerID != providerCustomerID || customer.Email != "state@example.com" {
		t.Fatalf("billing customer = %+v, error = %v", customer, err)
	}
	byProvider, err := store.GetBillingCustomerByProvider(ctx, "alpha", providerCustomerID)
	if err != nil || byProvider == nil || byProvider.AccountID != accountID {
		t.Fatalf("provider customer = %+v, error = %v", byProvider, err)
	}
	if missing, err := store.GetBillingCustomerByProvider(ctx, "alpha", "missing-"+uuid.NewString()); err != nil || missing != nil {
		t.Fatalf("missing provider customer = %+v, error = %v", missing, err)
	}

	monthOffer, err := store.ResolveBillingOffer(ctx, "alpha", "", "alpha-pro-month", "")
	if err != nil || monthOffer == nil || monthOffer.OfferKey != "pro_month" || monthOffer.Interval != "month" {
		t.Fatalf("monthly offer = %+v, error = %v", monthOffer, err)
	}
	yearOffer, err := store.ResolveBillingOffer(ctx, "alpha", "", "alpha-pro-year", "")
	if err != nil || yearOffer == nil || yearOffer.OfferKey != "pro_year" || yearOffer.Interval != "year" {
		t.Fatalf("yearly offer = %+v, error = %v", yearOffer, err)
	}
	if missing, err := store.ResolveBillingOffer(ctx, "alpha", "", "missing-price", ""); err != nil || missing != nil {
		t.Fatalf("missing offer = %+v, error = %v", missing, err)
	}
	if empty, err := store.ResolveBillingOffer(ctx, "alpha", "", "", ""); err != nil || empty != nil {
		t.Fatalf("empty offer lookup = %+v, error = %v", empty, err)
	}
	topup, err := store.ResolveBillingTopup(ctx, "alpha", "", "alpha-credit-pack", "")
	if err != nil || topup == nil || !topup.CreditsPerUnit.Equal(MustAmount("1234.567891")) || topup.MinQuantity != 1 || topup.MaxQuantity != 10 {
		t.Fatalf("top-up = %+v, error = %v", topup, err)
	}
	if missing, err := store.ResolveBillingTopup(ctx, "alpha", "", "missing-topup", ""); err != nil || missing != nil {
		t.Fatalf("missing top-up = %+v, error = %v", missing, err)
	}

	now := time.Now().UTC().Truncate(time.Second)
	periodEnd := now.Add(30 * 24 * time.Hour)
	subscriptionID, err := store.UpsertBillingSubscriptionState(ctx, CommerceSubscription{
		AccountID: accountID, Provider: "alpha", ProviderSubscriptionID: "sub-" + uuid.NewString(),
		ProviderCustomerID: providerCustomerID, OfferID: monthOffer.ID, Status: "active",
		CurrentPeriodStart: &now, CurrentPeriodEnd: &periodEnd, ProviderUpdatedAt: now,
		Metadata: map[string]any{"source": "wave4"},
	})
	if err != nil || subscriptionID == "" {
		t.Fatalf("upsert subscription = %q, error = %v", subscriptionID, err)
	}
	providerSubscriptionID := ""
	subscription, err := store.GetBillingSubscription(ctx, accountID, nil)
	if err != nil || subscription == nil || subscription.ID != subscriptionID || subscription.PlanKey != "pro" {
		t.Fatalf("current subscription = %+v, error = %v", subscription, err)
	}
	providerSubscriptionID = subscription.ProviderSubscriptionID
	byProviderSubscription, err := store.GetBillingSubscriptionByProvider(ctx, "alpha", providerSubscriptionID)
	if err != nil || byProviderSubscription == nil || byProviderSubscription.ID != subscriptionID {
		t.Fatalf("provider subscription = %+v, error = %v", byProviderSubscription, err)
	}
	listed, err := store.ListBillingSubscriptions(ctx, accountID)
	if err != nil || len(listed) != 1 {
		t.Fatalf("subscription list = %+v, error = %v", listed, err)
	}
	if none, err := store.GetBillingSubscription(ctx, accountID, []string{}); err != nil || none != nil {
		t.Fatalf("empty status filter = %+v, error = %v", none, err)
	}
	if _, err := store.GetBillingSubscription(ctx, accountID, []string{"not-a-status"}); err == nil {
		t.Fatal("invalid subscription status accepted")
	}

	changeInput := BillingSubscriptionChangeCreate{
		Provider: "alpha", ProviderSubscriptionID: providerSubscriptionID, ToOfferID: yearOffer.ID,
		ToOfferKey: "pro_year", ToPlanKey: "pro", ToInterval: "year", Effective: "renewal",
		EffectiveAt: now.Add(24 * time.Hour), OperationKey: "go-commerce-change-" + uuid.NewString(),
	}
	change, err := store.CreateBillingSubscriptionChange(ctx, changeInput)
	if err != nil || change.ID == "" || change.State != "scheduled" || change.ToOfferKey != "pro_year" {
		t.Fatalf("create subscription change = %+v, error = %v", change, err)
	}
	replayedChange, err := store.CreateBillingSubscriptionChange(ctx, changeInput)
	if err != nil || replayedChange.ID != change.ID {
		t.Fatalf("subscription change replay = %+v, first = %+v, error = %v", replayedChange, change, err)
	}
	conflictingChange := changeInput
	conflictingChange.ToOfferID = monthOffer.ID
	if _, err := store.CreateBillingSubscriptionChange(ctx, conflictingChange); err == nil {
		t.Fatal("subscription change idempotency key accepted a different target")
	}
	openChange, err := store.GetOpenBillingSubscriptionChange(ctx, "alpha", providerSubscriptionID)
	if err != nil || openChange == nil || openChange.ID != change.ID {
		t.Fatalf("open subscription change = %+v, error = %v", openChange, err)
	}
	if err := store.UpdateBillingSubscriptionChange(ctx, change.ID, BillingSubscriptionChangeUpdate{State: wave4StringPointer("applied"), ProviderOperationID: wave4StringPointer("provider-op-" + uuid.NewString())}); err != nil {
		t.Fatalf("advance subscription change: %v", err)
	}
	if err := store.UpdateBillingSubscriptionChange(ctx, change.ID, BillingSubscriptionChangeUpdate{State: wave4StringPointer("failed")}); err == nil {
		t.Fatal("terminal subscription change transition accepted")
	}

	preferences := BillingPreferences{AccountID: accountID, AutoRecharge: true, OverageProtection: true, EmailNotifications: true, UsageAlerts: false, InvoiceReminders: true}
	if err := store.UpsertBillingPreferences(ctx, preferences); err != nil {
		t.Fatalf("upsert billing preferences: %v", err)
	}
	gotPreferences, err := store.GetBillingPreferences(ctx, accountID)
	if err != nil || gotPreferences == nil || *gotPreferences != preferences {
		t.Fatalf("billing preferences = %+v, error = %v", gotPreferences, err)
	}

	providerPaymentID := "pay-" + uuid.NewString()
	paymentID, err := store.UpsertBillingPaymentState(ctx, BillingPaymentUpsert{
		Provider: "alpha", ProviderPaymentID: providerPaymentID, ProviderInvoiceID: "inv-" + uuid.NewString(),
		AccountID: accountID, AmountMinor: 500, Currency: "USD", Purpose: "credit_topup", Status: "succeeded",
		ProviderUpdatedAt: now, Metadata: map[string]any{"source": "wave4"},
	})
	if err != nil || paymentID == "" {
		t.Fatalf("upsert payment = %q, error = %v", paymentID, err)
	}
	payment, err := store.GetBillingPaymentByProvider(ctx, "alpha", providerPaymentID)
	if err != nil || payment == nil || payment.ID != paymentID || payment.AmountMinor != 500 {
		t.Fatalf("payment = %+v, error = %v", payment, err)
	}

	grantID, err := store.CreateBillingCreditGrant(ctx, BillingCreditGrantCreate{PaymentID: paymentID, TopupID: topup.TopupID, Credits: topup.CreditsPerUnit, Quantity: 1})
	if err != nil || grantID == "" {
		t.Fatalf("create billing grant = %q, error = %v", grantID, err)
	}
	posting, err := store.GrantBillingCredit(ctx, grantID, "go-commerce-grant-"+uuid.NewString())
	if err != nil || posting.LedgerEntryID == "" || posting.ErrorCode != "" {
		t.Fatalf("grant billing credit = %+v, error = %v", posting, err)
	}
	grantReplay, err := store.GrantBillingCredit(ctx, grantID, "go-commerce-grant-replay-"+uuid.NewString())
	if err != nil || grantReplay.LedgerEntryID != posting.LedgerEntryID || !grantReplay.Replayed {
		t.Fatalf("grant replay = %+v, first = %+v, error = %v", grantReplay, posting, err)
	}
	if gotGrant, err := store.GetBillingCreditGrantByPayment(ctx, paymentID); err != nil || gotGrant != grantID {
		t.Fatalf("grant lookup = %q, error = %v", gotGrant, err)
	}

	refundID, err := store.UpsertBillingRefundState(ctx, BillingRefundUpsert{
		Provider: "alpha", ProviderRefundID: "refund-" + uuid.NewString(), ProviderPaymentID: providerPaymentID,
		AccountID: accountID, AmountMinor: 100, Currency: "USD", Status: "succeeded", Reason: "requested", ProviderUpdatedAt: now,
	})
	if err != nil || refundID == "" {
		t.Fatalf("upsert refund = %q, error = %v", refundID, err)
	}
	refundPosting, err := store.PostBillingRefund(ctx, refundID, grantID, 100, "go-commerce-refund-"+uuid.NewString())
	if err != nil || refundPosting.LedgerEntryID == "" || refundPosting.ErrorCode != "" {
		t.Fatalf("post refund = %+v, error = %v", refundPosting, err)
	}
	refundReplay, err := store.PostBillingRefund(ctx, refundID, grantID, 100, "go-commerce-refund-replay-"+uuid.NewString())
	if err != nil || refundReplay.LedgerEntryID != refundPosting.LedgerEntryID || !refundReplay.Replayed {
		t.Fatalf("refund replay = %+v, first = %+v, error = %v", refundReplay, refundPosting, err)
	}

	if err := store.UpsertBillingDisputeState(ctx, BillingDisputeUpsert{
		Provider: "alpha", ProviderDisputeID: "dispute-" + uuid.NewString(), ProviderPaymentID: providerPaymentID,
		Status: "needs_response", Reason: "customer_claim", ProviderUpdatedAt: now,
	}); err != nil {
		t.Fatalf("upsert dispute: %v", err)
	}
	if err := store.UpsertBillingInvoiceState(ctx, BillingInvoiceUpsert{
		Provider: "alpha", ProviderInvoiceID: "invoice-" + uuid.NewString(), ProviderSubscriptionID: providerSubscriptionID,
		AccountID: accountID, Status: "paid", AmountPaidMinor: 2000, AmountDueMinor: 2000, Currency: "USD",
		PeriodStart: &now, PeriodEnd: &periodEnd, ProviderUpdatedAt: now,
	}); err != nil {
		t.Fatalf("upsert invoice: %v", err)
	}
	invoices, err := store.ListBillingInvoices(ctx, accountID)
	if err != nil || len(invoices) != 1 || invoices[0].Status != "paid" {
		t.Fatalf("billing invoices = %+v, error = %v", invoices, err)
	}
	if err := store.RecordSubscriptionConflict(ctx, BillingSubscriptionConflictCreate{
		AccountID: accountID, Provider: "alpha", DuplicateProviderSubscriptionID: "duplicate-" + uuid.NewString(),
		ExistingProviderSubscriptionID: providerSubscriptionID,
		Metadata:                       map[string]any{"source": "wave4"},
	}); err != nil {
		t.Fatalf("record subscription conflict: %v", err)
	}
	changed, err := store.PseudonymizeFinancialSubject(ctx, accountID)
	if err != nil || !changed {
		t.Fatalf("pseudonymize subject = %v, error = %v", changed, err)
	}
	changed, err = store.PseudonymizeFinancialSubject(ctx, uuid.NewString())
	if err != nil || changed {
		t.Fatalf("missing pseudonymize subject = %v, error = %v", changed, err)
	}
}

func wave4StringPointer(value string) *string { return &value }
