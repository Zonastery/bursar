// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"sync"
	"time"
)

// BillingEventType is Bursar's provider-neutral lifecycle vocabulary. Concrete
// provider adapters translate verified provider payloads into these values.
type BillingEventType string

const (
	BillingEventCustomerCreated                     BillingEventType = "customer.created"
	BillingEventCustomerUpdated                     BillingEventType = "customer.updated"
	BillingEventCustomerDeleted                     BillingEventType = "customer.deleted"
	BillingEventCheckoutCompleted                   BillingEventType = "checkout.completed"
	BillingEventCheckoutExpired                     BillingEventType = "checkout.expired"
	BillingEventSubscriptionCreated                 BillingEventType = "subscription.created"
	BillingEventSubscriptionUpdated                 BillingEventType = "subscription.updated"
	BillingEventSubscriptionActivated               BillingEventType = "subscription.activated"
	BillingEventSubscriptionRenewed                 BillingEventType = "subscription.renewed"
	BillingEventSubscriptionPlanChanged             BillingEventType = "subscription.plan_changed"
	BillingEventSubscriptionCancellationScheduled   BillingEventType = "subscription.cancellation_scheduled"
	BillingEventSubscriptionCancellationUnscheduled BillingEventType = "subscription.cancellation_unscheduled"
	BillingEventSubscriptionCanceled                BillingEventType = "subscription.canceled"
	BillingEventSubscriptionExpired                 BillingEventType = "subscription.expired"
	BillingEventSubscriptionPaused                  BillingEventType = "subscription.paused"
	BillingEventSubscriptionResumed                 BillingEventType = "subscription.resumed"
	BillingEventSubscriptionTrialWillEnd            BillingEventType = "subscription.trial_will_end"
	BillingEventInvoiceCreated                      BillingEventType = "invoice.created"
	BillingEventInvoiceUpdated                      BillingEventType = "invoice.updated"
	BillingEventInvoiceFinalized                    BillingEventType = "invoice.finalized"
	BillingEventInvoiceFinalizationFailed           BillingEventType = "invoice.finalization_failed"
	BillingEventInvoicePaid                         BillingEventType = "invoice.paid"
	BillingEventInvoicePaymentFailed                BillingEventType = "invoice.payment_failed"
	BillingEventInvoicePaymentActionRequired        BillingEventType = "invoice.payment_action_required"
	BillingEventInvoiceVoided                       BillingEventType = "invoice.voided"
	BillingEventInvoiceUpcoming                     BillingEventType = "invoice.upcoming"
	BillingEventPaymentSucceeded                    BillingEventType = "payment.succeeded"
	BillingEventPaymentFailed                       BillingEventType = "payment.failed"
	BillingEventPaymentMethodAttached               BillingEventType = "payment_method.attached"
	BillingEventPaymentMethodUpdated                BillingEventType = "payment_method.updated"
	BillingEventPaymentMethodDetached               BillingEventType = "payment_method.detached"
	BillingEventRefundCreated                       BillingEventType = "refund.created"
	BillingEventRefundUpdated                       BillingEventType = "refund.updated"
	BillingEventRefundFailed                        BillingEventType = "refund.failed"
	BillingEventDisputeCreated                      BillingEventType = "dispute.created"
	BillingEventDisputeUpdated                      BillingEventType = "dispute.updated"
	BillingEventDisputeClosed                       BillingEventType = "dispute.closed"
)

// BillingCustomer identifies a provider customer and the Bursar account it
// belongs to. AccountID is optional until an event is resolved from metadata or
// persisted customer state.
type BillingCustomer struct {
	ID        string         `json:"id"`
	AccountID string         `json:"account_id,omitempty"`
	Email     string         `json:"email,omitempty"`
	Metadata  map[string]any `json:"metadata,omitempty"`
}

// BillingSubscription is provider-neutral persisted subscription state.
type BillingSubscription struct {
	ID                 string         `json:"id"`
	Provider           string         `json:"provider"`
	AccountID          string         `json:"account_id"`
	CustomerID         string         `json:"customer_id,omitempty"`
	Plan               string         `json:"plan,omitempty"`
	Status             string         `json:"status"`
	Interval           string         `json:"interval,omitempty"`
	IntervalCount      int            `json:"interval_count,omitempty"`
	CurrentPeriodStart *time.Time     `json:"current_period_start,omitempty"`
	CurrentPeriodEnd   *time.Time     `json:"current_period_end,omitempty"`
	CancelAtPeriodEnd  bool           `json:"cancel_at_period_end,omitempty"`
	CanceledAt         *time.Time     `json:"canceled_at,omitempty"`
	Metadata           map[string]any `json:"metadata,omitempty"`
}

// BillingInvoice captures the provider invoice information Bursar needs for
// lifecycle processing and customer-facing document links.
type BillingInvoice struct {
	ID               string         `json:"id"`
	Provider         string         `json:"provider"`
	AccountID        string         `json:"account_id,omitempty"`
	SubscriptionID   string         `json:"subscription_id,omitempty"`
	CustomerID       string         `json:"customer_id,omitempty"`
	Status           string         `json:"status,omitempty"`
	Currency         string         `json:"currency,omitempty"`
	AmountPaidMinor  int64          `json:"amount_paid_minor,omitempty"`
	AmountDueMinor   int64          `json:"amount_due_minor,omitempty"`
	PeriodStart      *time.Time     `json:"period_start,omitempty"`
	PeriodEnd        *time.Time     `json:"period_end,omitempty"`
	HostedInvoiceURL string         `json:"hosted_invoice_url,omitempty"`
	Metadata         map[string]any `json:"metadata,omitempty"`
}

// BillingPayment describes a provider payment. Amounts are minor units: money
// displayed by providers stays integer-valued and is never converted through a
// floating-point major-unit representation.
type BillingPayment struct {
	ID             string         `json:"id"`
	Provider       string         `json:"provider"`
	AccountID      string         `json:"account_id,omitempty"`
	CustomerID     string         `json:"customer_id,omitempty"`
	SubscriptionID string         `json:"subscription_id,omitempty"`
	InvoiceID      string         `json:"invoice_id,omitempty"`
	Status         string         `json:"status,omitempty"`
	Currency       string         `json:"currency,omitempty"`
	AmountMinor    int64          `json:"amount_minor,omitempty"`
	Metadata       map[string]any `json:"metadata,omitempty"`
}

// BillingRefund describes a provider refund in minor units.
type BillingRefund struct {
	ID          string         `json:"id"`
	Provider    string         `json:"provider"`
	PaymentID   string         `json:"payment_id,omitempty"`
	Status      string         `json:"status,omitempty"`
	Currency    string         `json:"currency,omitempty"`
	AmountMinor int64          `json:"amount_minor,omitempty"`
	Metadata    map[string]any `json:"metadata,omitempty"`
}

// BillingDispute is a provider-neutral chargeback/dispute update.
type BillingDispute struct {
	ID        string         `json:"id"`
	Provider  string         `json:"provider"`
	PaymentID string         `json:"payment_id,omitempty"`
	Status    string         `json:"status,omitempty"`
	Reason    string         `json:"reason,omitempty"`
	Metadata  map[string]any `json:"metadata,omitempty"`
}

// BillingEvent is a verified, normalized provider event. Provider adapters
// must construct it only after validating the raw webhook signature.
type BillingEvent struct {
	ID           string               `json:"id"`
	Provider     string               `json:"provider"`
	Type         BillingEventType     `json:"type"`
	OccurredAt   time.Time            `json:"occurred_at"`
	Customer     *BillingCustomer     `json:"customer,omitempty"`
	Subscription *BillingSubscription `json:"subscription,omitempty"`
	Invoice      *BillingInvoice      `json:"invoice,omitempty"`
	Payment      *BillingPayment      `json:"payment,omitempty"`
	Refund       *BillingRefund       `json:"refund,omitempty"`
	Dispute      *BillingDispute      `json:"dispute,omitempty"`
	Metadata     map[string]any       `json:"metadata,omitempty"`
	RawPayload   json.RawMessage      `json:"-"`
}

// Validate checks the common event envelope before Bursar claims it. Provider
// adapters remain responsible for provider-specific invariants.
func (e BillingEvent) Validate() error {
	if strings.TrimSpace(e.ID) == "" {
		return errors.New("bursar: billing event id is required")
	}
	if strings.TrimSpace(e.Provider) == "" {
		return errors.New("bursar: billing event provider is required")
	}
	if strings.TrimSpace(string(e.Type)) == "" {
		return errors.New("bursar: billing event type is required")
	}
	if e.OccurredAt.IsZero() {
		return errors.New("bursar: billing event occurrence time is required")
	}
	return nil
}

// BillingEventClaimState describes the replay-safe claim outcome. A busy
// claim is deliberately distinct from duplicate: it means another worker owns
// an incomplete attempt and should be retried later.
type BillingEventClaimState string

const (
	BillingEventClaimed   BillingEventClaimState = "claimed"
	BillingEventDuplicate BillingEventClaimState = "duplicate"
	BillingEventBusy      BillingEventClaimState = "busy"
	BillingEventRejected  BillingEventClaimState = "rejected"
)

type BillingEventClaim struct {
	State      BillingEventClaimState
	ClaimToken string
	Attempts   int
	Reason     string
}

// BillingEventResult is returned by facade-owned ingestion. It records a
// terminal acknowledgement or an explicit retryable busy state; it never
// disguises a transport failure as a successfully processed event.
type BillingEventResult struct {
	Handled   bool
	Duplicate bool
	Ignored   bool
	Retryable bool
	AccountID string
}

// BillingStore is the minimum persistence contract for Bursar's normalized
// event lifecycle. The optional interfaces below add domain capabilities
// without making custom event-only stores implement unrelated commerce paths.
type BillingStore interface {
	ProviderEnvironment() ProviderEnvironment
	ClaimBillingEvent(context.Context, BillingEvent, map[string]any) (BillingEventClaim, error)
	CompleteBillingEvent(context.Context, string, string, string) (bool, error)
	FailBillingEvent(context.Context, string, string, string, string) (bool, error)
}

// BillingAccountResolver resolves a tenant-local account from a verified event.
// A postgres store normally resolves this using Bursar metadata first and then
// its persisted customer/subscription mapping.
type BillingAccountResolver interface {
	ResolveBillingEventAccount(context.Context, BillingEvent) (string, error)
}

// BillingEventHandler performs an application-owned normalized lifecycle
// action. A type-specific handler, or a configured default handler, takes
// precedence over a store-owned BillingLifecycleProcessor. Errors are recorded
// against the claim and leave the provider retryable where appropriate.
type BillingEventHandler func(context.Context, BillingEvent, string) error

// BillingService owns normalized event ingestion. It is intentionally composed
// by Bursar so providers cannot bypass the claim/complete/fail lifecycle.
type BillingService struct {
	store          BillingStore
	mu             sync.RWMutex
	handlers       map[BillingEventType]BillingEventHandler
	defaultHandler BillingEventHandler
}

func NewBillingService(store BillingStore) (*BillingService, error) {
	if store == nil {
		return nil, errors.New("bursar: billing store is required")
	}
	if err := store.ProviderEnvironment().Validate(); err != nil {
		return nil, err
	}
	return &BillingService{store: store, handlers: make(map[BillingEventType]BillingEventHandler)}, nil
}

// On registers a handler for one normalized event type. It is intended for
// application composition and is safe to call before concurrent ingestion.
func (s *BillingService) On(eventType BillingEventType, handler BillingEventHandler) error {
	if s == nil || handler == nil || strings.TrimSpace(string(eventType)) == "" {
		return errors.New("bursar: billing event type and handler are required")
	}
	s.mu.Lock()
	s.handlers[eventType] = handler
	s.mu.Unlock()
	return nil
}

// SetDefaultHandler registers the handler used when no type-specific handler
// exists. Passing nil clears it.
func (s *BillingService) SetDefaultHandler(handler BillingEventHandler) {
	if s == nil {
		return
	}
	s.mu.Lock()
	s.defaultHandler = handler
	s.mu.Unlock()
}

// Ingest claims, handles, and completes a verified normalized billing event.
func (s *BillingService) Ingest(ctx context.Context, event BillingEvent) (result BillingEventResult, err error) {
	if s == nil {
		return result, errors.New("bursar: billing service is not configured")
	}
	if err := event.Validate(); err != nil {
		return result, err
	}
	envelope := map[string]any{"occurred_at": event.OccurredAt.UTC().Format(time.RFC3339Nano)}
	claim, err := s.store.ClaimBillingEvent(ctx, event, envelope)
	if err != nil {
		return result, fmt.Errorf("bursar: claim billing event: %w", err)
	}
	switch claim.State {
	case BillingEventDuplicate:
		return BillingEventResult{Handled: true, Duplicate: true}, nil
	case BillingEventBusy:
		return BillingEventResult{Retryable: true}, nil
	case BillingEventRejected:
		return result, fmt.Errorf("bursar: billing event %s was rejected: %s", event.ID, claim.Reason)
	case BillingEventClaimed:
		if strings.TrimSpace(claim.ClaimToken) == "" {
			return result, errors.New("bursar: claimed billing event has no claim token")
		}
	default:
		return result, fmt.Errorf("bursar: unknown billing event claim state %q", claim.State)
	}

	accountID := ""
	if resolver, ok := s.store.(BillingAccountResolver); ok {
		accountID, err = resolver.ResolveBillingEventAccount(ctx, event)
		if err != nil {
			return s.fail(ctx, event, claim.ClaimToken, err)
		}
	}
	s.mu.RLock()
	handler := s.handlers[event.Type]
	if handler == nil {
		handler = s.defaultHandler
	}
	s.mu.RUnlock()
	var lifecycleResult *BillingEventResult
	if handler != nil {
		if err := handler(ctx, event, accountID); err != nil {
			return s.fail(ctx, event, claim.ClaimToken, err)
		}
	} else if processor, ok := s.store.(BillingLifecycleProcessor); ok && IsBillingLifecycleEventType(event.Type) {
		processed, processErr := processor.ProcessBillingEvent(ctx, event, accountID)
		if processErr != nil {
			return s.fail(ctx, event, claim.ClaimToken, processErr)
		}
		if !processed.Handled {
			return s.fail(ctx, event, claim.ClaimToken, fmt.Errorf("billing lifecycle processor did not handle event %q", event.Type))
		}
		processed.AccountID = accountID
		lifecycleResult = &processed
	} else {
		// Expired/upcoming provider events are intentionally acknowledged: their
		// state has no financial effect until a later confirmed lifecycle event.
		if event.Type == BillingEventCheckoutExpired || event.Type == BillingEventInvoiceUpcoming {
			if _, err := s.store.CompleteBillingEvent(ctx, event.Provider, event.ID, claim.ClaimToken); err != nil {
				return result, fmt.Errorf("bursar: complete ignored billing event: %w", err)
			}
			return BillingEventResult{Handled: true, Ignored: true, AccountID: accountID}, nil
		}
		return s.fail(ctx, event, claim.ClaimToken, fmt.Errorf("no handler for billing event %q", event.Type))
	}
	completed, err := s.store.CompleteBillingEvent(ctx, event.Provider, event.ID, claim.ClaimToken)
	if err != nil {
		return result, fmt.Errorf("bursar: complete billing event: %w", err)
	}
	if !completed {
		return result, fmt.Errorf("bursar: billing event %s completion claim was lost", event.ID)
	}
	if lifecycleResult != nil {
		return *lifecycleResult, nil
	}
	return BillingEventResult{Handled: true, AccountID: accountID}, nil
}

func (s *BillingService) fail(ctx context.Context, event BillingEvent, claimToken string, cause error) (BillingEventResult, error) {
	message := strings.TrimSpace(cause.Error())
	if len(message) > 1_024 {
		message = message[:1_024]
	}
	failed, err := s.store.FailBillingEvent(ctx, event.Provider, event.ID, claimToken, message)
	if err != nil {
		return BillingEventResult{}, fmt.Errorf("bursar: fail billing event after handler error %q: %w", message, err)
	}
	if !failed {
		return BillingEventResult{}, fmt.Errorf("bursar: billing event %s failure claim was lost: %w", event.ID, cause)
	}
	return BillingEventResult{}, cause
}
