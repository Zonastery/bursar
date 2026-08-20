// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"
)

type billingLifecycleStoreStub struct {
	environment ProviderEnvironment
	accountID   string

	processedEvent     BillingEvent
	processedAccountID string
	processed          int
	processResult      BillingEventResult
	processErr         error

	completed int
	failed    int
	failure   string
	steps     []string
	claimed   map[string]any
}

func (s *billingLifecycleStoreStub) ProviderEnvironment() ProviderEnvironment {
	return s.environment
}

func (s *billingLifecycleStoreStub) ClaimBillingEvent(_ context.Context, _ BillingEvent, envelope map[string]any) (BillingEventClaim, error) {
	s.steps = append(s.steps, "claim")
	s.claimed = envelope
	return BillingEventClaim{State: BillingEventClaimed, ClaimToken: "claim-token"}, nil
}

func (s *billingLifecycleStoreStub) CompleteBillingEvent(_ context.Context, _, _, _ string) (bool, error) {
	s.steps = append(s.steps, "complete")
	s.completed++
	return true, nil
}

func (s *billingLifecycleStoreStub) FailBillingEvent(_ context.Context, _, _, _, diagnostic string) (bool, error) {
	s.steps = append(s.steps, "fail")
	s.failed++
	s.failure = diagnostic
	return true, nil
}

func (s *billingLifecycleStoreStub) ResolveBillingEventAccount(_ context.Context, _ BillingEvent) (string, error) {
	return s.accountID, nil
}

func (s *billingLifecycleStoreStub) ProcessBillingEvent(_ context.Context, event BillingEvent, accountID string) (BillingEventResult, error) {
	s.steps = append(s.steps, "process")
	s.processed++
	s.processedEvent = event
	s.processedAccountID = accountID
	return s.processResult, s.processErr
}

func lifecycleEvent(eventType BillingEventType) BillingEvent {
	event := BillingEvent{
		ID:         "event-1",
		Provider:   "stripe",
		Type:       eventType,
		OccurredAt: time.Date(2026, time.August, 10, 12, 0, 0, 0, time.UTC),
	}
	switch {
	case strings.HasPrefix(string(eventType), "customer."):
		event.Customer = &BillingCustomer{ProviderCustomerID: "cus-1"}
	case strings.HasPrefix(string(eventType), "subscription."):
		event.Subscription = &BillingSubscription{ProviderSubscriptionID: "sub-1", Provider: "stripe", Status: "active"}
	case strings.HasPrefix(string(eventType), "invoice."):
		event.Invoice = &BillingInvoice{ProviderInvoiceID: "in-1", Provider: "stripe", Status: "paid", Currency: "USD"}
	case strings.HasPrefix(string(eventType), "payment."):
		event.Payment = &BillingPayment{ProviderPaymentID: "pay-1", Provider: "stripe", Purpose: "subscription", Status: "succeeded", Currency: "USD"}
	case strings.HasPrefix(string(eventType), "refund."):
		event.Refund = &BillingRefund{ProviderRefundID: "re-1", ProviderPaymentID: "pay-1", Provider: "stripe", Status: "succeeded", Currency: "USD", AmountMinor: 1}
	case strings.HasPrefix(string(eventType), "dispute."):
		event.Dispute = &BillingDispute{ProviderDisputeID: "dp-1", ProviderPaymentID: "pay-1", Provider: "stripe", Status: "under_review"}
	}
	return event
}

func newBillingLifecycleService(t *testing.T, store *billingLifecycleStoreStub) *BillingService {
	t.Helper()
	service, err := NewBillingService(store)
	if err != nil {
		t.Fatalf("NewBillingService() error = %v", err)
	}
	return service
}

func TestBillingServiceHasProvisioningReflectsConfiguredPort(t *testing.T) {
	store := &billingLifecycleStoreStub{environment: ProviderEnvironmentTest}
	service := newBillingLifecycleService(t, store)
	if service.HasProvisioning() {
		t.Fatal("HasProvisioning() = true without a provisioning port")
	}
	service.provisioning = &creditStoreStub{}
	if !service.HasProvisioning() {
		t.Fatal("HasProvisioning() = false with a provisioning port")
	}
}

func TestBillingServiceUsesStoreLifecycleProcessor(t *testing.T) {
	store := &billingLifecycleStoreStub{
		environment:   ProviderEnvironmentTest,
		accountID:     "account-1",
		processResult: BillingEventResult{Handled: true},
	}
	service := newBillingLifecycleService(t, store)

	result, err := service.Ingest(context.Background(), lifecycleEvent(BillingEventSubscriptionRenewed))
	if err != nil {
		t.Fatalf("Ingest() error = %v", err)
	}
	if !result.Handled || result.AccountID != "account-1" {
		t.Fatalf("Ingest() result = %#v, want handled account result", result)
	}
	if store.processed != 1 || store.processedAccountID != "account-1" {
		t.Fatalf("processor calls = %d, account = %q; want one call for account-1", store.processed, store.processedAccountID)
	}
	if store.completed != 1 || store.failed != 0 {
		t.Fatalf("completion/failure = %d/%d, want 1/0", store.completed, store.failed)
	}
	if got, want := strings.Join(store.steps, ","), "claim,process,complete"; got != want {
		t.Fatalf("processing order = %q, want %q", got, want)
	}
}

func TestBillingServiceEventCallbackRunsAfterLifecycleCompletion(t *testing.T) {
	store := &billingLifecycleStoreStub{
		environment:   ProviderEnvironmentTest,
		accountID:     "account-1",
		processResult: BillingEventResult{Handled: true},
	}
	service := newBillingLifecycleService(t, store)
	callbackAccountID := ""
	if err := service.OnEvent(BillingEventSubscriptionRenewed, func(_ context.Context, _ BillingEvent, accountID string) {
		store.steps = append(store.steps, "callback")
		callbackAccountID = accountID
	}); err != nil {
		t.Fatalf("OnEvent() error = %v", err)
	}

	result, err := service.Ingest(context.Background(), lifecycleEvent(BillingEventSubscriptionRenewed))
	if err != nil {
		t.Fatalf("Ingest() error = %v", err)
	}
	if !result.Handled || callbackAccountID != "account-1" {
		t.Fatalf("result = %#v, callback account = %q", result, callbackAccountID)
	}
	if got, want := strings.Join(store.steps, ","), "claim,process,complete,callback"; got != want {
		t.Fatalf("processing order = %q, want %q", got, want)
	}
}

func TestBillingServiceEventCallbackFailureCannotChangeCompletedEvent(t *testing.T) {
	store := &billingLifecycleStoreStub{
		environment:   ProviderEnvironmentTest,
		accountID:     "account-1",
		processResult: BillingEventResult{Handled: true},
	}
	service := newBillingLifecycleService(t, store)
	if err := service.OnEvent(BillingEventPaymentSucceeded, func(context.Context, BillingEvent, string) {
		panic("notification failed")
	}); err != nil {
		t.Fatalf("OnEvent() error = %v", err)
	}

	result, err := service.Ingest(context.Background(), lifecycleEvent(BillingEventPaymentSucceeded))
	if err != nil {
		t.Fatalf("Ingest() error = %v", err)
	}
	if !result.Handled || store.completed != 1 || store.failed != 0 {
		t.Fatalf("result = %#v, completion/failure = %d/%d", result, store.completed, store.failed)
	}
}

func TestBillingServiceClaimsCanonicalEnvelopeWithoutRawPayload(t *testing.T) {
	store := &billingLifecycleStoreStub{
		environment:   ProviderEnvironmentTest,
		processResult: BillingEventResult{Handled: true},
	}
	service := newBillingLifecycleService(t, store)
	event := lifecycleEvent(BillingEventPaymentSucceeded)
	event.AccountID = "account-1"
	event.BillingEventID = "internal-event-id"
	event.RawPayload = []byte(`{"secret":"provider-body"}`)
	event.Metadata = map[string]any{"provider_key": "preserved"}
	event.Payment.ID = "legacy-payment-id"
	event.Payment.ProviderPaymentID = "pay-1"
	event.Payment.AccountID = "account-1"
	event.Payment.Refs = &ProviderRef{ProductID: "prod-1", PriceID: "price-1"}

	if _, err := service.Ingest(context.Background(), event); err != nil {
		t.Fatalf("Ingest() error = %v", err)
	}
	for _, forbidden := range []string{"occurredAt", "occurred_at", "raw", "rawPayload", "billingEventId", "billing_event_id", "id", "type"} {
		if _, exists := store.claimed[forbidden]; exists {
			t.Fatalf("claim envelope contains forbidden field %q: %#v", forbidden, store.claimed)
		}
	}
	if store.claimed["eventId"] != "event-1" || store.claimed["eventType"] != string(BillingEventPaymentSucceeded) || store.claimed["accountId"] != "account-1" {
		t.Fatalf("claim envelope = %#v, want canonical event identity", store.claimed)
	}
	metadata, ok := store.claimed["metadata"].(map[string]any)
	if !ok || metadata["provider_key"] != "preserved" {
		t.Fatalf("claim metadata = %#v, want provider key preserved", store.claimed["metadata"])
	}
	payment, ok := store.claimed["payment"].(map[string]any)
	if !ok || payment["providerPaymentId"] != "pay-1" || payment["purpose"] != "subscription" || payment["status"] != "succeeded" {
		t.Fatalf("claim payment = %#v, want canonical payment", store.claimed["payment"])
	}
	if _, exists := payment["id"]; exists {
		t.Fatalf("claim payment contains legacy ID: %#v", payment)
	}
	refs, ok := payment["refs"].(map[string]any)
	if !ok || refs["productId"] != "prod-1" || refs["priceId"] != "price-1" {
		t.Fatalf("claim payment refs = %#v, want canonical provider references", payment["refs"])
	}
}

func TestBillingServiceExplicitHandlersOverrideLifecycleProcessor(t *testing.T) {
	store := &billingLifecycleStoreStub{
		environment:   ProviderEnvironmentTest,
		processResult: BillingEventResult{Handled: true},
	}
	service := newBillingLifecycleService(t, store)
	called := 0
	if err := service.On(BillingEventPaymentSucceeded, func(_ context.Context, _ BillingEvent, _ string) error {
		called++
		return nil
	}); err != nil {
		t.Fatalf("On() error = %v", err)
	}

	result, err := service.Ingest(context.Background(), lifecycleEvent(BillingEventPaymentSucceeded))
	if err != nil {
		t.Fatalf("Ingest() error = %v", err)
	}
	if !result.Handled || called != 1 || store.processed != 0 || store.completed != 1 {
		t.Fatalf("result = %#v, callback = %d, processor = %d, completed = %d", result, called, store.processed, store.completed)
	}
}

func TestBillingServiceDefaultHandlerOverridesLifecycleProcessor(t *testing.T) {
	store := &billingLifecycleStoreStub{
		environment:   ProviderEnvironmentTest,
		processResult: BillingEventResult{Handled: true},
	}
	service := newBillingLifecycleService(t, store)
	called := 0
	service.SetDefaultHandler(func(_ context.Context, _ BillingEvent, _ string) error {
		called++
		return nil
	})

	result, err := service.Ingest(context.Background(), lifecycleEvent(BillingEventInvoicePaid))
	if err != nil {
		t.Fatalf("Ingest() error = %v", err)
	}
	if !result.Handled || called != 1 || store.processed != 0 || store.completed != 1 {
		t.Fatalf("result = %#v, callback = %d, processor = %d, completed = %d", result, called, store.processed, store.completed)
	}
}

func TestBillingServiceLifecycleProcessorFailureFailsClaim(t *testing.T) {
	processorErr := errors.New("cannot persist subscription state")
	store := &billingLifecycleStoreStub{
		environment: ProviderEnvironmentTest,
		processErr:  processorErr,
	}
	service := newBillingLifecycleService(t, store)

	_, err := service.Ingest(context.Background(), lifecycleEvent(BillingEventSubscriptionCreated))
	if !errors.Is(err, processorErr) {
		t.Fatalf("Ingest() error = %v, want processor error", err)
	}
	if store.processed != 1 || store.completed != 0 || store.failed != 1 {
		t.Fatalf("processor/completion/failure = %d/%d/%d, want 1/0/1", store.processed, store.completed, store.failed)
	}
	if store.failure != "billing_event_failed:Error" {
		t.Fatalf("failure diagnostic = %q, want safe taxonomy", store.failure)
	}
}

func TestBillingServiceAcknowledgesIgnoredEventsWithoutLifecycleProcessor(t *testing.T) {
	store := &billingStoreWithoutLifecycle{environment: ProviderEnvironmentTest}
	service, err := NewBillingService(store)
	if err != nil {
		t.Fatalf("NewBillingService() error = %v", err)
	}

	result, err := service.Ingest(context.Background(), lifecycleEvent(BillingEventCheckoutExpired))
	if err != nil {
		t.Fatalf("Ingest() error = %v", err)
	}
	if !result.Handled || !result.Ignored || store.completed != 1 {
		t.Fatalf("result = %#v, completed = %d; want ignored completion", result, store.completed)
	}
}

func TestBillingServiceExpiresCheckoutIntentForIgnoredEvent(t *testing.T) {
	store := &billingStoreWithCheckoutIntent{billingStoreWithoutLifecycle: billingStoreWithoutLifecycle{environment: ProviderEnvironmentTest}}
	service, err := NewBillingService(store)
	if err != nil {
		t.Fatalf("NewBillingService() error = %v", err)
	}
	event := lifecycleEvent(BillingEventCheckoutExpired)
	event.Metadata = CreditMetadata{"checkout_intent_id": "intent-1"}
	result, err := service.Ingest(context.Background(), event)
	if err != nil {
		t.Fatalf("Ingest() error = %v", err)
	}
	if !result.Handled || !result.Ignored || store.intentID != "intent-1" || store.intentStatus != "expired" {
		t.Fatalf("result = %#v, intent = %q/%q", result, store.intentID, store.intentStatus)
	}
}

type billingStoreWithoutLifecycle struct {
	environment ProviderEnvironment
	completed   int
}

type billingStoreWithCheckoutIntent struct {
	billingStoreWithoutLifecycle
	intentID     string
	intentStatus string
}

func (s *billingStoreWithCheckoutIntent) UpdateCheckoutIntent(_ context.Context, intentID, _ string, update CheckoutIntentUpdate) error {
	s.intentID = intentID
	s.intentStatus = update.Status
	return nil
}

func (s *billingStoreWithoutLifecycle) ProviderEnvironment() ProviderEnvironment {
	return s.environment
}

func (*billingStoreWithoutLifecycle) ClaimBillingEvent(context.Context, BillingEvent, map[string]any) (BillingEventClaim, error) {
	return BillingEventClaim{State: BillingEventClaimed, ClaimToken: "claim-token"}, nil
}

func (s *billingStoreWithoutLifecycle) CompleteBillingEvent(context.Context, string, string, string) (bool, error) {
	s.completed++
	return true, nil
}

func (*billingStoreWithoutLifecycle) FailBillingEvent(context.Context, string, string, string, string) (bool, error) {
	return true, nil
}

func TestIsBillingLifecycleEventTypeCoversSharedVocabulary(t *testing.T) {
	eventTypes := []BillingEventType{
		BillingEventCustomerCreated,
		BillingEventCustomerUpdated,
		BillingEventCustomerDeleted,
		BillingEventCheckoutCompleted,
		BillingEventCheckoutExpired,
		BillingEventSubscriptionCreated,
		BillingEventSubscriptionUpdated,
		BillingEventSubscriptionActivated,
		BillingEventSubscriptionRenewed,
		BillingEventSubscriptionPlanChanged,
		BillingEventSubscriptionCancellationScheduled,
		BillingEventSubscriptionCancellationUnscheduled,
		BillingEventSubscriptionCanceled,
		BillingEventSubscriptionExpired,
		BillingEventSubscriptionPaused,
		BillingEventSubscriptionResumed,
		BillingEventSubscriptionTrialWillEnd,
		BillingEventInvoiceCreated,
		BillingEventInvoiceFinalized,
		BillingEventInvoiceFinalizationFailed,
		BillingEventInvoiceUpcoming,
		BillingEventInvoicePaid,
		BillingEventInvoicePaymentFailed,
		BillingEventInvoicePaymentActionRequired,
		BillingEventInvoiceVoided,
		BillingEventPaymentSucceeded,
		BillingEventPaymentFailed,
		BillingEventRefundCreated,
		BillingEventRefundUpdated,
		BillingEventRefundFailed,
		BillingEventDisputeCreated,
		BillingEventDisputeClosed,
		BillingEventPaymentMethodAttached,
		BillingEventPaymentMethodUpdated,
		BillingEventPaymentMethodDetached,
	}
	for _, eventType := range eventTypes {
		if !IsBillingLifecycleEventType(eventType) {
			t.Errorf("IsBillingLifecycleEventType(%q) = false, want true", eventType)
		}
	}
	if IsBillingLifecycleEventType("provider.private_event") {
		t.Fatal("IsBillingLifecycleEventType accepted an unknown provider event")
	}
}
