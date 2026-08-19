// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import (
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
)

func TestPostgresBillingLifecycleEventMatrix(t *testing.T) {
	ctx, cancel, config := financialContext(t)
	defer cancel()
	sdk, store := newFinancialSDK(t, ctx, config, &financialProvider{name: "alpha"})
	runID := uuid.NewString()
	accountID := uuid.NewString()
	customerID := "cus-lifecycle-" + runID
	subscriptionID := "sub-lifecycle-" + runID
	now := time.Now().UTC().Truncate(time.Second)
	periodEnd := now.Add(30 * 24 * time.Hour)

	process := func(event BillingEvent) BillingEventResult {
		t.Helper()
		result, err := store.ProcessBillingEvent(ctx, event, event.AccountID)
		if err != nil {
			if typed, ok := AsBursarError(err); ok && typed.Unwrap() != nil {
				t.Fatalf("process %s: %v: %v", event.Type, err, typed.Unwrap())
			}
			t.Fatalf("process %s: %v", event.Type, err)
		}
		if !result.Handled {
			t.Fatalf("process %s result = %+v, want handled", event.Type, result)
		}
		return result
	}

	customer := &BillingCustomer{ProviderCustomerID: customerID, Email: "lifecycle@example.com"}
	process(BillingEvent{EventID: "evt-" + runID + "-customer-created", Provider: "alpha", Type: BillingEventCustomerCreated, OccurredAt: now, AccountID: accountID, Customer: customer})
	if result := process(BillingEvent{EventID: "evt-" + runID + "-customer-updated", Provider: "alpha", Type: BillingEventCustomerUpdated, OccurredAt: now.Add(time.Second), Customer: customer}); result.AccountID != accountID {
		t.Fatalf("customer lookup account = %q, want %q", result.AccountID, accountID)
	}
	if result := process(BillingEvent{EventID: "evt-" + runID + "-customer-deleted", Provider: "alpha", Type: BillingEventCustomerDeleted, OccurredAt: now.Add(2 * time.Second), Customer: customer}); result.AccountID != accountID {
		t.Fatalf("deleted customer lookup account = %q, want %q", result.AccountID, accountID)
	}

	subscription := &BillingSubscription{
		ProviderSubscriptionID: subscriptionID,
		Status:                 "active",
		Interval:               "month",
		IntervalCount:          1,
		PeriodStart:            &now,
		PeriodEnd:              &periodEnd,
		Refs:                   &ProviderRef{PriceID: "alpha-pro-month"},
		Metadata:               map[string]any{"source": "lifecycle-matrix"},
	}
	created := process(BillingEvent{EventID: "evt-" + runID + "-subscription-created", Provider: "alpha", Type: BillingEventSubscriptionCreated, OccurredAt: now, Customer: customer, Subscription: subscription})
	if created.Action != "subscription_created" || created.SubscriptionID != subscriptionID {
		t.Fatalf("created subscription result = %+v", created)
	}
	conflict := *subscription
	conflict.ProviderSubscriptionID = "sub-conflict-" + runID
	if _, err := store.ProcessBillingEvent(ctx, BillingEvent{EventID: "evt-" + runID + "-subscription-conflict", Provider: "alpha", Type: BillingEventSubscriptionCreated, OccurredAt: now.Add(time.Second), AccountID: accountID, Subscription: &conflict}, accountID); err == nil {
		t.Fatal("second current subscription was accepted")
	}
	if err := (postgresBillingLifecycle{store: store, provisioning: store}).revokeIfCurrent(ctx, accountID, "provider-subscription-that-is-not-current"); err != nil {
		t.Fatalf("revoke non-current subscription: %v", err)
	}

	// These transitions exercise the provider-neutral vocabulary while each
	// assertion remains against the committed PostgreSQL projection.
	for index, eventType := range []BillingEventType{
		BillingEventSubscriptionUpdated,
		BillingEventSubscriptionActivated,
		BillingEventSubscriptionRenewed,
		BillingEventSubscriptionPlanChanged,
	} {
		updated := *subscription
		updated.Refs = nil // resolve the existing active offer, as a webhook may.
		updated.Status = "active"
		result := process(BillingEvent{EventID: "evt-" + runID + "-subscription-transition-" + string(rune('a'+index)), Provider: "alpha", Type: eventType, OccurredAt: now.Add(time.Duration(index+1) * time.Minute), Subscription: &updated})
		if result.Action != strings.ReplaceAll(string(eventType), ".", "_") {
			t.Fatalf("%s action = %q", eventType, result.Action)
		}
	}

	trial := *subscription
	trial.Refs = nil
	trial.Status = "trialing"
	trial.TrialEnd = timePointer(now.Add(24 * time.Hour))
	trialResult := process(BillingEvent{EventID: "evt-" + runID + "-trial-ending", Provider: "alpha", Type: BillingEventSubscriptionTrialWillEnd, OccurredAt: now, Subscription: &trial})
	if trialResult.Action != "trial_will_end_notified" {
		t.Fatalf("trial ending action = %q", trialResult.Action)
	}

	for index, event := range []struct {
		typ    BillingEventType
		status string
		cancel bool
	}{
		{BillingEventSubscriptionCancellationScheduled, "active", true},
		{BillingEventSubscriptionCancellationUnscheduled, "active", false},
		{BillingEventSubscriptionPaused, "paused", false},
		{BillingEventSubscriptionResumed, "active", false},
		{BillingEventSubscriptionUpdated, "past_due", false},
	} {
		updated := *subscription
		updated.Refs = nil
		updated.Status = event.status
		updated.CancelAtPeriodEnd = event.cancel
		result := process(BillingEvent{EventID: "evt-" + runID + "-state-" + string(rune('a'+index)), Provider: "alpha", Type: event.typ, OccurredAt: now.Add(time.Duration(index+8) * time.Minute), Subscription: &updated})
		if result.AccountID != accountID {
			t.Fatalf("%s account = %q, want %q", event.typ, result.AccountID, accountID)
		}
	}

	// Terminal states revoke the currently entitled plan, and a subsequent
	// event can restore it only when the provider truth says the subscription is
	// active again.
	terminal := *subscription
	terminal.Refs = nil
	terminal.Status = "canceled"
	process(BillingEvent{EventID: "evt-" + runID + "-canceled", Provider: "alpha", Type: BillingEventSubscriptionCanceled, OccurredAt: now.Add(20 * time.Minute), Subscription: &terminal})
	terminal.Status = "expired"
	process(BillingEvent{EventID: "evt-" + runID + "-expired", Provider: "alpha", Type: BillingEventSubscriptionExpired, OccurredAt: now.Add(21 * time.Minute), Subscription: &terminal})

	completedCheckout, err := sdk.Commerce.CreateCheckout(ctx, CreateCheckoutInput{
		SubjectID: accountID, AccountID: accountID, OfferKey: "credit_pack", Type: "credit_pack", Quantity: int64Pointer(1),
		SuccessURL: "https://app.example/success/{intentId}", CancelURL: "https://app.example/cancel/{intentId}", IdempotencyKey: "lifecycle-checkout-complete-" + runID,
	})
	if err != nil {
		t.Fatalf("create completed checkout: %v", err)
	}
	process(BillingEvent{EventID: "evt-" + runID + "-checkout-complete", Provider: "alpha", Type: BillingEventCheckoutCompleted, OccurredAt: now, AccountID: accountID, Metadata: map[string]any{"checkout_intent_id": completedCheckout.Intent.ID}})
	completedIntent, err := store.GetCheckoutIntent(ctx, completedCheckout.Intent.ID, accountID)
	if err != nil || completedIntent == nil || completedIntent.Status != "completed" {
		t.Fatalf("completed checkout = %+v, error = %v", completedIntent, err)
	}

	expiredCheckout, err := sdk.Commerce.CreateCheckout(ctx, CreateCheckoutInput{
		SubjectID: accountID, AccountID: accountID, OfferKey: "credit_pack", Type: "credit_pack", Quantity: int64Pointer(1),
		SuccessURL: "https://app.example/success/{intentId}", CancelURL: "https://app.example/cancel/{intentId}", IdempotencyKey: "lifecycle-checkout-expired-" + runID,
	})
	if err != nil {
		t.Fatalf("create expired checkout: %v", err)
	}
	expiredResult := process(BillingEvent{EventID: "evt-" + runID + "-checkout-expired", Provider: "alpha", Type: BillingEventCheckoutExpired, OccurredAt: now, AccountID: accountID, Metadata: map[string]any{"checkout_intent_id": expiredCheckout.Intent.ID}})
	if !expiredResult.Ignored {
		t.Fatalf("expired checkout result = %+v, want ignored", expiredResult)
	}
	expiredIntent, err := store.GetCheckoutIntent(ctx, expiredCheckout.Intent.ID, accountID)
	if err != nil || expiredIntent == nil || expiredIntent.Status != "expired" {
		t.Fatalf("expired checkout = %+v, error = %v", expiredIntent, err)
	}

	invoiceTypes := []BillingEventType{
		BillingEventInvoiceCreated,
		BillingEventInvoiceUpdated,
		BillingEventInvoiceFinalized,
		BillingEventInvoiceFinalizationFailed,
		BillingEventInvoicePaymentFailed,
		BillingEventInvoicePaymentActionRequired,
		BillingEventInvoiceVoided,
	}
	for index, eventType := range invoiceTypes {
		invoice := &BillingInvoice{ProviderInvoiceID: "inv-" + runID + "-" + string(rune('a'+index)), Status: "open", Currency: "USD", AmountDueMinor: 500}
		result := process(BillingEvent{EventID: "evt-" + runID + "-invoice-" + string(rune('a'+index)), Provider: "alpha", Type: eventType, OccurredAt: now, AccountID: accountID, Invoice: invoice})
		if result.Action == "" {
			t.Fatalf("%s returned empty action", eventType)
		}
	}
	upcoming := process(BillingEvent{EventID: "evt-" + runID + "-invoice-upcoming", Provider: "alpha", Type: BillingEventInvoiceUpcoming, OccurredAt: now, AccountID: accountID, Invoice: &BillingInvoice{ProviderInvoiceID: "inv-upcoming-" + runID, Status: "draft", Currency: "USD"}})
	if !upcoming.Ignored {
		t.Fatalf("invoice upcoming result = %+v, want ignored", upcoming)
	}
	process(BillingEvent{EventID: "evt-" + runID + "-invoice-paid-without-document", Provider: "alpha", Type: BillingEventInvoicePaid, OccurredAt: now, AccountID: accountID})
	if _, err := store.ProcessBillingEvent(ctx, BillingEvent{EventID: "evt-" + runID + "-checkout-invalid-id", Provider: "alpha", Type: BillingEventCheckoutExpired, OccurredAt: now, AccountID: accountID, Metadata: map[string]any{"checkout_intent_id": "not-a-uuid"}}, accountID); err == nil {
		t.Fatal("invalid checkout intent ID was accepted")
	}
	if _, err := store.ProcessBillingEvent(ctx, BillingEvent{EventID: "evt-" + runID + "-checkout-rejected", Provider: "alpha", Type: BillingEventCheckoutExpired, OccurredAt: now, AccountID: accountID, Metadata: map[string]any{"checkout_intent_id": completedCheckout.Intent.ID}}, accountID); err == nil {
		t.Fatal("terminal checkout transition was accepted")
	}

	paymentFailed := process(BillingEvent{EventID: "evt-" + runID + "-payment-failed", Provider: "alpha", Type: BillingEventPaymentFailed, OccurredAt: now, AccountID: accountID, Payment: &BillingPayment{ProviderPaymentID: "pay-failed-" + runID, Status: "failed", Currency: "USD", AmountMinor: 500, Purpose: "subscription"}})
	if paymentFailed.Action != "payment_failed_recorded" {
		t.Fatalf("payment failed result = %+v", paymentFailed)
	}
	paymentSubscription := *subscription
	paymentSubscription.Refs = nil
	paymentSubscription.Status = "active"
	paymentSucceeded := process(BillingEvent{EventID: "evt-" + runID + "-payment-succeeded", Provider: "alpha", Type: BillingEventPaymentSucceeded, OccurredAt: now, AccountID: accountID, Payment: &BillingPayment{ProviderPaymentID: "pay-succeeded-" + runID, InvoiceID: "inv-payment-" + runID, Status: "succeeded", Currency: "USD", AmountMinor: 500, Purpose: "subscription", SubscriptionID: subscriptionID}, Subscription: &paymentSubscription})
	if paymentSucceeded.Action != "payment_succeeded" {
		t.Fatalf("payment succeeded result = %+v", paymentSucceeded)
	}
	pastDuePaymentSubscription := *subscription
	pastDuePaymentSubscription.Refs = nil
	pastDuePaymentSubscription.Status = "active"
	paymentFailedWithSubscription := process(BillingEvent{EventID: "evt-" + runID + "-payment-failed-with-subscription", Provider: "alpha", Type: BillingEventPaymentFailed, OccurredAt: now.Add(30 * time.Minute), AccountID: accountID, Payment: &BillingPayment{ProviderPaymentID: "pay-failed-subscription-" + runID, Status: "failed", Currency: "USD", AmountMinor: 500, Purpose: "subscription"}, Subscription: &pastDuePaymentSubscription})
	if paymentFailedWithSubscription.Action != "payment_failed_recorded" {
		t.Fatalf("payment failed subscription result = %+v", paymentFailedWithSubscription)
	}
	outOfBounds := process(BillingEvent{EventID: "evt-" + runID + "-topup-out-of-bounds", Provider: "alpha", Type: BillingEventPaymentSucceeded, OccurredAt: now.Add(31 * time.Minute), AccountID: accountID, Payment: &BillingPayment{ProviderPaymentID: "pay-topup-out-of-bounds-" + runID, AmountMinor: 501, Currency: "USD", Purpose: "credit_topup", Status: "succeeded", Refs: &ProviderRef{PriceID: "alpha-credit-pack"}}})
	if outOfBounds.Action != "payment_succeeded_out_of_bounds" {
		t.Fatalf("out-of-bounds top-up result = %+v", outOfBounds)
	}

	for index, eventType := range []BillingEventType{BillingEventPaymentMethodAttached, BillingEventPaymentMethodUpdated, BillingEventPaymentMethodDetached} {
		process(BillingEvent{EventID: "evt-" + runID + "-method-" + string(rune('a'+index)), Provider: "alpha", Type: eventType, OccurredAt: now, AccountID: accountID})
	}
	for index, eventType := range []BillingEventType{BillingEventRefundCreated, BillingEventRefundUpdated, BillingEventRefundFailed} {
		process(BillingEvent{EventID: "evt-" + runID + "-refund-" + string(rune('a'+index)), Provider: "alpha", Type: eventType, OccurredAt: now, AccountID: accountID, Refund: &BillingRefund{ProviderRefundID: "refund-lifecycle-" + runID + "-" + string(rune('a'+index)), ProviderPaymentID: "pay-succeeded-" + runID, Status: "pending", Currency: "USD", AmountMinor: 100}})
	}
	refundSucceededNonTopup := process(BillingEvent{EventID: "evt-" + runID + "-refund-subscription-payment", Provider: "alpha", Type: BillingEventRefundCreated, OccurredAt: now, AccountID: accountID, Refund: &BillingRefund{ProviderRefundID: "refund-subscription-" + runID, ProviderPaymentID: "pay-succeeded-" + runID, Status: "succeeded", Currency: "USD", AmountMinor: 100}})
	if refundSucceededNonTopup.Action != "refund_recorded" {
		t.Fatalf("subscription refund result = %+v", refundSucceededNonTopup)
	}
	for index, eventType := range []BillingEventType{BillingEventDisputeCreated, BillingEventDisputeUpdated, BillingEventDisputeClosed} {
		process(BillingEvent{EventID: "evt-" + runID + "-dispute-" + string(rune('a'+index)), Provider: "alpha", Type: eventType, OccurredAt: now, AccountID: accountID, Dispute: &BillingDispute{ProviderDisputeID: "dispute-lifecycle-" + runID, ProviderPaymentID: "pay-succeeded-" + runID, Status: "needs_response", Reason: "customer_claim"}})
	}

	if _, err := store.ProcessBillingEvent(ctx, BillingEvent{EventID: "evt-" + runID + "-unsupported", Provider: "alpha", Type: BillingEventType("provider.private"), OccurredAt: now}, accountID); err == nil {
		t.Fatal("unsupported lifecycle event was accepted")
	}
}

func TestPostgresBillingLifecycleResolvesPaymentAndRefundAccounts(t *testing.T) {
	ctx, cancel, config := financialContext(t)
	defer cancel()
	_, store := newFinancialSDK(t, ctx, config, &financialProvider{name: "alpha"})
	runID := uuid.NewString()
	accountID := uuid.NewString()
	now := time.Now().UTC().Truncate(time.Second)
	paymentProviderID := "pay-resolve-" + runID
	if _, err := store.UpsertBillingPaymentState(ctx, BillingPaymentUpsert{Provider: "alpha", ProviderPaymentID: paymentProviderID, AccountID: accountID, AmountMinor: 500, Currency: "USD", Purpose: "subscription", Status: "pending", ProviderUpdatedAt: now}); err != nil {
		t.Fatalf("seed payment for account resolution: %v", err)
	}

	failed, err := store.ProcessBillingEvent(ctx, BillingEvent{EventID: "evt-" + runID + "-payment-failed", Provider: "alpha", Type: BillingEventPaymentFailed, OccurredAt: now.Add(time.Minute), Payment: &BillingPayment{ProviderPaymentID: paymentProviderID, Status: "failed", Currency: "USD", AmountMinor: 500, Purpose: "subscription"}}, "")
	if err != nil || failed.AccountID != accountID || failed.Action != "payment_failed_recorded" {
		if typed, ok := AsBursarError(err); ok && typed.Unwrap() != nil {
			t.Fatalf("payment account resolution = %+v, error = %v: %v", failed, err, typed.Unwrap())
		}
		t.Fatalf("payment account resolution = %+v, error = %v", failed, err)
	}

	refund, err := store.ProcessBillingEvent(ctx, BillingEvent{EventID: "evt-" + runID + "-refund", Provider: "alpha", Type: BillingEventRefundCreated, OccurredAt: now, Refund: &BillingRefund{ProviderRefundID: "refund-resolve-" + runID, ProviderPaymentID: paymentProviderID, Status: "pending", Currency: "USD", AmountMinor: 100}}, "")
	if err != nil || refund.AccountID != accountID || refund.Action != "refund_recorded" {
		t.Fatalf("refund account resolution = %+v, error = %v", refund, err)
	}
}

func TestBillingLifecycleVocabularyIncludesLegacyAliases(t *testing.T) {
	for _, eventType := range []BillingEventType{
		BillingEventInvoiceUpdated,
		BillingEventDisputeUpdated,
	} {
		if !IsBillingLifecycleEventType(eventType) {
			t.Errorf("IsBillingLifecycleEventType(%q) = false", eventType)
		}
	}
	if IsBillingLifecycleEventType(BillingEventType("provider.private")) {
		t.Fatal("unknown provider event accepted")
	}
}

func TestBillingLifecycleHelpers(t *testing.T) {
	if got := firstPositive(0, -1, 3); got != 3 {
		t.Fatalf("firstPositive() = %d, want 3", got)
	}
	if got := firstPositive(0, -1); got != 1 {
		t.Fatalf("firstPositive() default = %d, want 1", got)
	}
	if got := mergedBillingMetadata(nil, nil, map[string]any{"source": "event"}); got["source"] != "event" {
		t.Fatalf("merged metadata = %#v", got)
	}
	if got := mergedBillingMetadata(&CommerceSubscription{Metadata: map[string]any{"source": "existing", "keep": true}}, map[string]any{"source": "event"}); got["source"] != "event" || got["keep"] != true {
		t.Fatalf("merged metadata precedence = %#v", got)
	}
	if got := existingText(nil, func(*CommerceSubscription) string { return "unexpected" }); got != "" {
		t.Fatalf("existingText(nil) = %q", got)
	}
	if got := existingTime(nil, func(*CommerceSubscription) *time.Time { return nil }); got != nil {
		t.Fatalf("existingTime(nil) = %v", got)
	}
	if got := firstTime(nil, timePointer(time.Date(2026, 1, 1, 0, 0, 0, 0, time.FixedZone("offset", 3600)))); got == nil || !got.Equal(time.Date(2025, 12, 31, 23, 0, 0, 0, time.UTC)) {
		t.Fatalf("firstTime UTC normalization = %v", got)
	}
	if got := customerProviderID(nil); got != "" {
		t.Fatalf("customerProviderID(nil) = %q", got)
	}
	if got := subscriptionEventID(BillingEvent{}); got != "" {
		t.Fatalf("subscriptionEventID(empty) = %q", got)
	}
}
