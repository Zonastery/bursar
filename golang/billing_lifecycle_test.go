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
}

func (s *billingLifecycleStoreStub) ProviderEnvironment() ProviderEnvironment {
	return s.environment
}

func (s *billingLifecycleStoreStub) ClaimBillingEvent(_ context.Context, _ BillingEvent, _ map[string]any) (BillingEventClaim, error) {
	s.steps = append(s.steps, "claim")
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
	return BillingEvent{
		ID:         "event-1",
		Provider:   "stripe",
		Type:       eventType,
		OccurredAt: time.Date(2026, time.August, 10, 12, 0, 0, 0, time.UTC),
	}
}

func newBillingLifecycleService(t *testing.T, store *billingLifecycleStoreStub) *BillingService {
	t.Helper()
	service, err := NewBillingService(store)
	if err != nil {
		t.Fatalf("NewBillingService() error = %v", err)
	}
	return service
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
	if !strings.Contains(store.failure, processorErr.Error()) {
		t.Fatalf("failure diagnostic = %q, want processor error", store.failure)
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

type billingStoreWithoutLifecycle struct {
	environment ProviderEnvironment
	completed   int
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
