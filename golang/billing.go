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
	// ProviderCustomerID is the provider-owned customer identifier used by the
	// cross-SDK billing contract. ID is retained as a read compatibility alias
	// for callers constructing the original preview SDK types.
	ProviderCustomerID string         `json:"provider_customer_id,omitempty"`
	ID                 string         `json:"id,omitempty"`
	AccountID          string         `json:"account_id,omitempty"`
	Email              string         `json:"email,omitempty"`
	Metadata           map[string]any `json:"metadata,omitempty"`
}

// ProviderRef identifies a provider catalog object without exposing provider
// identifiers through Bursar's public offer catalog.
type ProviderRef struct {
	ProductID string `json:"product_id,omitempty"`
	PriceID   string `json:"price_id,omitempty"`
	VariantID string `json:"variant_id,omitempty"`
	LookupKey string `json:"lookup_key,omitempty"`
}

// BillingSubscription is provider-neutral persisted subscription state.
type BillingSubscription struct {
	ProviderSubscriptionID string         `json:"provider_subscription_id,omitempty"`
	ID                     string         `json:"id,omitempty"`
	Provider               string         `json:"provider"`
	AccountID              string         `json:"account_id"`
	CustomerID             string         `json:"customer_id,omitempty"`
	Plan                   string         `json:"plan,omitempty"`
	Status                 string         `json:"status"`
	Interval               string         `json:"interval,omitempty"`
	IntervalCount          int            `json:"interval_count,omitempty"`
	CurrentPeriodStart     *time.Time     `json:"current_period_start,omitempty"`
	CurrentPeriodEnd       *time.Time     `json:"current_period_end,omitempty"`
	PeriodStart            *time.Time     `json:"period_start,omitempty"`
	PeriodEnd              *time.Time     `json:"period_end,omitempty"`
	TrialEnd               *time.Time     `json:"trial_end,omitempty"`
	CancelAt               *time.Time     `json:"cancel_at,omitempty"`
	EndedAt                *time.Time     `json:"ended_at,omitempty"`
	CancelAtPeriodEnd      bool           `json:"cancel_at_period_end,omitempty"`
	CanceledAt             *time.Time     `json:"canceled_at,omitempty"`
	Refs                   *ProviderRef   `json:"refs,omitempty"`
	Metadata               map[string]any `json:"metadata,omitempty"`
}

// BillingInvoice captures the provider invoice information Bursar needs for
// lifecycle processing and customer-facing document links.
type BillingInvoice struct {
	ProviderInvoiceID string         `json:"provider_invoice_id,omitempty"`
	ID                string         `json:"id,omitempty"`
	Provider          string         `json:"provider"`
	AccountID         string         `json:"account_id,omitempty"`
	SubscriptionID    string         `json:"subscription_id,omitempty"`
	CustomerID        string         `json:"customer_id,omitempty"`
	Status            string         `json:"status,omitempty"`
	Currency          string         `json:"currency,omitempty"`
	AmountPaidMinor   int64          `json:"amount_paid_minor,omitempty"`
	AmountDueMinor    int64          `json:"amount_due_minor,omitempty"`
	PeriodStart       *time.Time     `json:"period_start,omitempty"`
	PeriodEnd         *time.Time     `json:"period_end,omitempty"`
	HostedInvoiceURL  string         `json:"hosted_invoice_url,omitempty"`
	Metadata          map[string]any `json:"metadata,omitempty"`
}

// BillingPayment describes a provider payment. Amounts are minor units: money
// displayed by providers stays integer-valued and is never converted through a
// floating-point major-unit representation.
type BillingPayment struct {
	ProviderPaymentID string         `json:"provider_payment_id,omitempty"`
	ID                string         `json:"id,omitempty"`
	Provider          string         `json:"provider"`
	AccountID         string         `json:"account_id,omitempty"`
	CustomerID        string         `json:"customer_id,omitempty"`
	SubscriptionID    string         `json:"subscription_id,omitempty"`
	InvoiceID         string         `json:"invoice_id,omitempty"`
	Status            string         `json:"status,omitempty"`
	Currency          string         `json:"currency,omitempty"`
	AmountMinor       int64          `json:"amount_minor,omitempty"`
	TaxMinor          int64          `json:"tax_minor,omitempty"`
	Purpose           string         `json:"purpose,omitempty"`
	Refs              *ProviderRef   `json:"refs,omitempty"`
	Metadata          map[string]any `json:"metadata,omitempty"`
}

// BillingRefund describes a provider refund in minor units.
type BillingRefund struct {
	ProviderRefundID  string         `json:"provider_refund_id,omitempty"`
	ProviderPaymentID string         `json:"provider_payment_id,omitempty"`
	ID                string         `json:"id,omitempty"`
	Provider          string         `json:"provider"`
	PaymentID         string         `json:"payment_id,omitempty"`
	Status            string         `json:"status,omitempty"`
	Currency          string         `json:"currency,omitempty"`
	AmountMinor       int64          `json:"amount_minor,omitempty"`
	Reason            string         `json:"reason,omitempty"`
	Metadata          map[string]any `json:"metadata,omitempty"`
}

// BillingDispute is a provider-neutral chargeback/dispute update.
type BillingDispute struct {
	ProviderDisputeID string         `json:"provider_dispute_id,omitempty"`
	ProviderPaymentID string         `json:"provider_payment_id,omitempty"`
	ID                string         `json:"id,omitempty"`
	Provider          string         `json:"provider"`
	PaymentID         string         `json:"payment_id,omitempty"`
	Status            string         `json:"status,omitempty"`
	Reason            string         `json:"reason,omitempty"`
	Metadata          map[string]any `json:"metadata,omitempty"`
}

// BillingEvent is a verified, normalized provider event. Provider adapters
// must construct it only after validating the raw webhook signature.
type BillingEvent struct {
	EventID        string               `json:"event_id,omitempty"`
	ID             string               `json:"id,omitempty"`
	Provider       string               `json:"provider"`
	Type           BillingEventType     `json:"type"`
	OccurredAt     time.Time            `json:"occurred_at"`
	AccountID      string               `json:"account_id,omitempty"`
	BillingEventID string               `json:"billing_event_id,omitempty"`
	Customer       *BillingCustomer     `json:"customer,omitempty"`
	Subscription   *BillingSubscription `json:"subscription,omitempty"`
	Invoice        *BillingInvoice      `json:"invoice,omitempty"`
	Payment        *BillingPayment      `json:"payment,omitempty"`
	Refund         *BillingRefund       `json:"refund,omitempty"`
	Dispute        *BillingDispute      `json:"dispute,omitempty"`
	Metadata       map[string]any       `json:"metadata,omitempty"`
	RawPayload     json.RawMessage      `json:"-"`
}

// Validate checks the common event envelope before Bursar claims it. Provider
// adapters remain responsible for provider-specific invariants.
func (e BillingEvent) Validate() error {
	if strings.TrimSpace(e.canonicalEventID()) == "" {
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
	if !IsBillingLifecycleEventType(e.Type) {
		return fmt.Errorf("bursar: unsupported billing event type %q", e.Type)
	}
	eventType := string(e.Type)
	switch {
	case strings.HasPrefix(eventType, "customer.") && e.Customer == nil:
		return fmt.Errorf("bursar: billing event %q requires customer data", e.Type)
	case strings.HasPrefix(eventType, "subscription.") && e.Subscription == nil:
		return fmt.Errorf("bursar: billing event %q requires subscription data", e.Type)
	case strings.HasPrefix(eventType, "invoice.") && e.Invoice == nil:
		return fmt.Errorf("bursar: billing event %q requires invoice data", e.Type)
	case strings.HasPrefix(eventType, "payment.") && e.Payment == nil:
		return fmt.Errorf("bursar: billing event %q requires payment data", e.Type)
	case strings.HasPrefix(eventType, "refund.") && e.Refund == nil:
		return fmt.Errorf("bursar: billing event %q requires refund data", e.Type)
	case strings.HasPrefix(eventType, "dispute.") && e.Dispute == nil:
		return fmt.Errorf("bursar: billing event %q requires dispute data", e.Type)
	}
	if e.Customer != nil {
		if e.Customer.canonicalProviderCustomerID() == "" && strings.TrimSpace(e.Customer.Email) == "" {
			return fmt.Errorf("bursar: billing event %q customer requires a provider customer ID or email", e.Type)
		}
	}
	if e.Subscription != nil {
		if e.Subscription.canonicalProviderSubscriptionID() == "" {
			return fmt.Errorf("bursar: billing event %q subscription provider ID is required", e.Type)
		}
		if e.Subscription.Status != "" && !billingSubscriptionStatus(e.Subscription.Status) {
			return fmt.Errorf("bursar: billing event %q subscription status %q is unsupported", e.Type, e.Subscription.Status)
		}
		if e.Subscription.Interval != "" && !billingInterval(e.Subscription.Interval) {
			return fmt.Errorf("bursar: billing event %q subscription interval %q is unsupported", e.Type, e.Subscription.Interval)
		}
		if e.Subscription.IntervalCount < 0 || (e.Subscription.Interval != "" && e.Subscription.IntervalCount == 0) {
			return fmt.Errorf("bursar: billing event %q subscription interval count must be positive", e.Type)
		}
		if err := validateProviderRef(e.Subscription.Refs); err != nil {
			return fmt.Errorf("bursar: billing event %q subscription reference: %w", e.Type, err)
		}
		if err := validateBillingInstants(e.Subscription.PeriodStart, e.Subscription.PeriodEnd, e.Subscription.TrialEnd, e.Subscription.CancelAt, e.Subscription.EndedAt); err != nil {
			return fmt.Errorf("bursar: billing event %q subscription: %w", e.Type, err)
		}
	}
	if e.Invoice != nil {
		if e.Invoice.canonicalProviderInvoiceID() == "" {
			return fmt.Errorf("bursar: billing event %q invoice provider ID is required", e.Type)
		}
		if !oneOf(e.Invoice.Status, "draft", "open", "paid", "void", "uncollectible") {
			return fmt.Errorf("bursar: billing event %q invoice status %q is unsupported", e.Type, e.Invoice.Status)
		}
		if e.Invoice.AmountPaidMinor < 0 || e.Invoice.AmountDueMinor < 0 {
			return fmt.Errorf("bursar: billing event %q invoice amounts must be non-negative minor units", e.Type)
		}
		if !billingCurrency(e.Invoice.Currency) {
			return fmt.Errorf("bursar: billing event %q invoice currency must be an uppercase three-letter code", e.Type)
		}
		if err := validateBillingInstants(e.Invoice.PeriodStart, e.Invoice.PeriodEnd); err != nil {
			return fmt.Errorf("bursar: billing event %q invoice: %w", e.Type, err)
		}
	}
	if e.Payment != nil {
		if e.Payment.canonicalProviderPaymentID() == "" {
			return fmt.Errorf("bursar: billing event %q payment provider ID is required", e.Type)
		}
		if e.Payment.AmountMinor < 0 || e.Payment.TaxMinor < 0 {
			return fmt.Errorf("bursar: billing event %q payment amounts must be non-negative minor units", e.Type)
		}
		if !billingCurrency(e.Payment.Currency) {
			return fmt.Errorf("bursar: billing event %q payment currency must be an uppercase three-letter code", e.Type)
		}
		if !oneOf(e.Payment.Purpose, "subscription", "credit_topup") {
			return fmt.Errorf("bursar: billing event %q payment purpose %q is unsupported", e.Type, e.Payment.Purpose)
		}
		if !billingPaymentStatus(e.Payment.Status) {
			return fmt.Errorf("bursar: billing event %q payment status %q is unsupported", e.Type, e.Payment.Status)
		}
		if err := validateProviderRef(e.Payment.Refs); err != nil {
			return fmt.Errorf("bursar: billing event %q payment reference: %w", e.Type, err)
		}
	}
	if e.Refund != nil {
		if e.Refund.canonicalProviderRefundID() == "" || e.Refund.canonicalProviderPaymentID() == "" {
			return fmt.Errorf("bursar: billing event %q refund provider refund and payment IDs are required", e.Type)
		}
		if e.Refund.AmountMinor <= 0 {
			return fmt.Errorf("bursar: billing event %q refund amount must be positive minor units", e.Type)
		}
		if !billingCurrency(e.Refund.Currency) {
			return fmt.Errorf("bursar: billing event %q refund currency must be an uppercase three-letter code", e.Type)
		}
		if !billingPaymentStatus(e.Refund.Status) {
			return fmt.Errorf("bursar: billing event %q refund status %q is unsupported", e.Type, e.Refund.Status)
		}
	}
	if e.Dispute != nil {
		if e.Dispute.canonicalProviderDisputeID() == "" || e.Dispute.canonicalProviderPaymentID() == "" {
			return fmt.Errorf("bursar: billing event %q dispute provider dispute and payment IDs are required", e.Type)
		}
		if !oneOf(e.Dispute.Status, "needs_response", "under_review", "won", "lost", "closed") {
			return fmt.Errorf("bursar: billing event %q dispute status %q is unsupported", e.Type, e.Dispute.Status)
		}
	}
	return nil
}

// billingEventClaimEnvelope returns the provider-neutral, cross-SDK event
// document retained for replay diagnostics and optional archival. Provider
// transport payloads and internal claim identifiers are deliberately absent;
// the provider adapter keeps the authenticated raw body at the ingress edge.
func billingEventClaimEnvelope(event BillingEvent) map[string]any {
	envelope := map[string]any{
		"eventId":   event.canonicalEventID(),
		"provider":  strings.TrimSpace(event.Provider),
		"eventType": string(event.Type),
	}
	if accountID := strings.TrimSpace(event.AccountID); accountID != "" {
		envelope["accountId"] = accountID
	}
	if event.Customer != nil {
		customer := map[string]any{}
		if providerCustomerID := event.Customer.canonicalProviderCustomerID(); providerCustomerID != "" {
			customer["providerCustomerId"] = providerCustomerID
		}
		if email := strings.TrimSpace(event.Customer.Email); email != "" {
			customer["email"] = email
		}
		envelope["customer"] = customer
	}
	if event.Subscription != nil {
		subscription := map[string]any{
			"providerSubscriptionId": event.Subscription.canonicalProviderSubscriptionID(),
			"cancelAtPeriodEnd":      event.Subscription.CancelAtPeriodEnd,
		}
		setBillingEnvelopeText(subscription, "status", event.Subscription.Status)
		periodStart := event.Subscription.PeriodStart
		if periodStart == nil {
			periodStart = event.Subscription.CurrentPeriodStart
		}
		periodEnd := event.Subscription.PeriodEnd
		if periodEnd == nil {
			periodEnd = event.Subscription.CurrentPeriodEnd
		}
		setBillingEnvelopeTime(subscription, "periodStart", periodStart)
		setBillingEnvelopeTime(subscription, "periodEnd", periodEnd)
		setBillingEnvelopeTime(subscription, "trialEnd", event.Subscription.TrialEnd)
		setBillingEnvelopeTime(subscription, "cancelAt", event.Subscription.CancelAt)
		setBillingEnvelopeTime(subscription, "endedAt", event.Subscription.EndedAt)
		if event.Subscription.Refs != nil {
			subscription["refs"] = billingProviderRefEnvelope(event.Subscription.Refs)
		}
		setBillingEnvelopeText(subscription, "interval", event.Subscription.Interval)
		if event.Subscription.IntervalCount != 0 {
			subscription["intervalCount"] = event.Subscription.IntervalCount
		}
		envelope["subscription"] = subscription
	}
	if event.Invoice != nil {
		invoice := map[string]any{
			"providerInvoiceId": event.Invoice.canonicalProviderInvoiceID(),
			"status":            event.Invoice.Status,
			"amountPaidMinor":   event.Invoice.AmountPaidMinor,
			"amountDueMinor":    event.Invoice.AmountDueMinor,
			"currency":          event.Invoice.Currency,
		}
		setBillingEnvelopeTime(invoice, "periodStart", event.Invoice.PeriodStart)
		setBillingEnvelopeTime(invoice, "periodEnd", event.Invoice.PeriodEnd)
		envelope["invoice"] = invoice
	}
	if event.Payment != nil {
		payment := map[string]any{
			"providerPaymentId": event.Payment.canonicalProviderPaymentID(),
			"amountMinor":       event.Payment.AmountMinor,
			"taxMinor":          event.Payment.TaxMinor,
			"currency":          event.Payment.Currency,
			"purpose":           event.Payment.Purpose,
			"status":            event.Payment.Status,
		}
		if event.Payment.Refs != nil {
			payment["refs"] = billingProviderRefEnvelope(event.Payment.Refs)
		}
		envelope["payment"] = payment
	}
	if event.Refund != nil {
		refund := map[string]any{
			"providerRefundId":  event.Refund.canonicalProviderRefundID(),
			"providerPaymentId": event.Refund.canonicalProviderPaymentID(),
			"amountMinor":       event.Refund.AmountMinor,
			"currency":          event.Refund.Currency,
			"status":            event.Refund.Status,
		}
		setBillingEnvelopeText(refund, "reason", event.Refund.Reason)
		envelope["refund"] = refund
	}
	if event.Dispute != nil {
		dispute := map[string]any{
			"providerDisputeId": event.Dispute.canonicalProviderDisputeID(),
			"providerPaymentId": event.Dispute.canonicalProviderPaymentID(),
			"status":            event.Dispute.Status,
		}
		setBillingEnvelopeText(dispute, "reason", event.Dispute.Reason)
		envelope["dispute"] = dispute
	}
	if event.Metadata != nil {
		envelope["metadata"] = event.Metadata
	}
	return envelope
}

func billingMetadataString(metadata CreditMetadata, key string) string {
	if metadata == nil {
		return ""
	}
	value, _ := metadata[key].(string)
	return strings.TrimSpace(value)
}

func billingProviderRefEnvelope(reference *ProviderRef) map[string]any {
	result := map[string]any{}
	setBillingEnvelopeText(result, "productId", reference.ProductID)
	setBillingEnvelopeText(result, "priceId", reference.PriceID)
	setBillingEnvelopeText(result, "variantId", reference.VariantID)
	setBillingEnvelopeText(result, "lookupKey", reference.LookupKey)
	return result
}

func setBillingEnvelopeText(target map[string]any, key, value string) {
	if value = strings.TrimSpace(value); value != "" {
		target[key] = value
	}
}

func setBillingEnvelopeTime(target map[string]any, key string, value *time.Time) {
	if value != nil {
		target[key] = value.UTC().Format(time.RFC3339Nano)
	}
}

func billingSubscriptionStatus(value string) bool {
	return oneOf(value, "incomplete", "incomplete_expired", "trialing", "active", "past_due", "canceled", "unpaid", "paused", "expired")
}

func billingPaymentStatus(value string) bool {
	return oneOf(value, "pending", "succeeded", "failed", "canceled")
}

func billingInterval(value string) bool {
	return oneOf(value, "day", "week", "month", "year")
}

func billingCurrency(value string) bool {
	if len(value) != 3 {
		return false
	}
	for _, character := range value {
		if character < 'A' || character > 'Z' {
			return false
		}
	}
	return true
}

func oneOf(value string, allowed ...string) bool {
	for _, candidate := range allowed {
		if value == candidate {
			return true
		}
	}
	return false
}

func validateProviderRef(reference *ProviderRef) error {
	if reference == nil {
		return nil
	}
	if strings.TrimSpace(reference.ProductID) == "" &&
		strings.TrimSpace(reference.PriceID) == "" &&
		strings.TrimSpace(reference.VariantID) == "" &&
		strings.TrimSpace(reference.LookupKey) == "" {
		return errors.New("must contain a provider identifier")
	}
	return nil
}

func validateBillingInstants(values ...*time.Time) error {
	for _, value := range values {
		if value != nil && value.IsZero() {
			return errors.New("timestamps must be valid instants")
		}
	}
	return nil
}

func (e BillingEvent) canonicalEventID() string {
	if value := strings.TrimSpace(e.EventID); value != "" {
		return value
	}
	return strings.TrimSpace(e.ID)
}

func (c BillingCustomer) canonicalProviderCustomerID() string {
	if value := strings.TrimSpace(c.ProviderCustomerID); value != "" {
		return value
	}
	return strings.TrimSpace(c.ID)
}

func (s BillingSubscription) canonicalProviderSubscriptionID() string {
	if value := strings.TrimSpace(s.ProviderSubscriptionID); value != "" {
		return value
	}
	return strings.TrimSpace(s.ID)
}

func (i BillingInvoice) canonicalProviderInvoiceID() string {
	if value := strings.TrimSpace(i.ProviderInvoiceID); value != "" {
		return value
	}
	return strings.TrimSpace(i.ID)
}

func (p BillingPayment) canonicalProviderPaymentID() string {
	if value := strings.TrimSpace(p.ProviderPaymentID); value != "" {
		return value
	}
	return strings.TrimSpace(p.ID)
}

func (r BillingRefund) canonicalProviderRefundID() string {
	if value := strings.TrimSpace(r.ProviderRefundID); value != "" {
		return value
	}
	return strings.TrimSpace(r.ID)
}

func (r BillingRefund) canonicalProviderPaymentID() string {
	if value := strings.TrimSpace(r.ProviderPaymentID); value != "" {
		return value
	}
	return strings.TrimSpace(r.PaymentID)
}

func (d BillingDispute) canonicalProviderDisputeID() string {
	if value := strings.TrimSpace(d.ProviderDisputeID); value != "" {
		return value
	}
	return strings.TrimSpace(d.ID)
}

func (d BillingDispute) canonicalProviderPaymentID() string {
	if value := strings.TrimSpace(d.ProviderPaymentID); value != "" {
		return value
	}
	return strings.TrimSpace(d.PaymentID)
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
	State          BillingEventClaimState
	ClaimToken     string
	BillingEventID string
	Attempts       int
	Reason         string
}

// BillingEventResult is returned by facade-owned ingestion. It records a
// terminal acknowledgement or an explicit retryable busy state; it never
// disguises a transport failure as a successfully processed event.
type BillingEventResult struct {
	Handled        bool
	Duplicate      bool
	Ignored        bool
	Retryable      bool
	AccountID      string
	Action         string
	Error          string
	SubscriptionID string
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

// BillingEventCallback observes a successfully completed lifecycle event.
// Callback panics are recovered and cannot alter the durable event outcome.
// Use BillingEventHandler only when deliberately replacing the built-in
// lifecycle processor; ordinary application notifications belong here.
type BillingEventCallback func(context.Context, BillingEvent, string)

// BillingProvisioningPort is the narrow credit capability used by the
// provider-neutral subscription lifecycle. PostgresStore deliberately does
// not make plan decisions itself; the facade supplies its CreditsService.
type BillingProvisioningPort interface {
	GetUserPlan(context.Context, string) (GetUserPlanResult, error)
	SetUserPlan(context.Context, string, string, SetUserPlanOptions) (SetUserPlanResult, error)
	UnsetUserPlan(context.Context, string) (UnsetUserPlanResult, error)
}

// BillingService owns normalized event ingestion. It is intentionally composed
// by Bursar so providers cannot bypass the claim/complete/fail lifecycle.
type BillingService struct {
	// AutoRecharge is available when the facade's billing store implements
	// AutoRechargeStore. It mirrors the JS/Python billing capability while
	// remaining nil for intentionally minimal billing-store integrations.
	AutoRecharge                *AutoRechargeService
	store                       BillingStore
	mu                          sync.RWMutex
	handlers                    map[BillingEventType]BillingEventHandler
	eventHandlers               map[BillingEventType]BillingEventCallback
	defaultHandler              BillingEventHandler
	provisioning                BillingProvisioningPort
	autoSelectEntitlementSource bool
	pastDueGracePeriod          time.Duration
	terminalPlanKey             string
}

func NewBillingService(store BillingStore, options ...BillingServiceOptions) (*BillingService, error) {
	if store == nil {
		return nil, errors.New("bursar: billing store is required")
	}
	if len(options) > 1 {
		return nil, errors.New("bursar: billing service accepts at most one options value")
	}
	if err := store.ProviderEnvironment().Validate(); err != nil {
		return nil, err
	}
	service := &BillingService{
		store:                       store,
		handlers:                    make(map[BillingEventType]BillingEventHandler),
		eventHandlers:               make(map[BillingEventType]BillingEventCallback),
		autoSelectEntitlementSource: true,
		pastDueGracePeriod:          7 * 24 * time.Hour,
	}
	if len(options) == 1 {
		if err := applyBillingOptions(service, &options[0]); err != nil {
			return nil, err
		}
	}
	return service, nil
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

// OnEvent registers a failure-isolated notification for one successfully
// processed event type. It does not replace Bursar's built-in lifecycle
// processor. Registering another callback for the same type replaces the
// previous callback, matching the Python SDK's event_handlers mapping.
func (s *BillingService) OnEvent(eventType BillingEventType, callback BillingEventCallback) error {
	if s == nil || callback == nil || strings.TrimSpace(string(eventType)) == "" {
		return errors.New("bursar: billing event type and callback are required")
	}
	s.mu.Lock()
	s.eventHandlers[eventType] = callback
	s.mu.Unlock()
	return nil
}

func (s *BillingService) fireEventHandler(ctx context.Context, event BillingEvent, accountID string) {
	if s == nil || strings.TrimSpace(accountID) == "" {
		return
	}
	s.mu.RLock()
	callback := s.eventHandlers[event.Type]
	s.mu.RUnlock()
	if callback == nil {
		return
	}
	func() {
		defer func() { _ = recover() }()
		callback(ctx, event, accountID)
	}()
}

// Ingest claims, handles, and completes a verified normalized billing event.
func (s *BillingService) Ingest(ctx context.Context, event BillingEvent) (result BillingEventResult, err error) {
	if s == nil {
		return result, errors.New("bursar: billing service is not configured")
	}
	if err := event.Validate(); err != nil {
		return result, err
	}
	envelope := billingEventClaimEnvelope(event)
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
		return result, fmt.Errorf("bursar: billing event %s was rejected: %s", event.canonicalEventID(), claim.Reason)
	case BillingEventClaimed:
		if strings.TrimSpace(claim.ClaimToken) == "" {
			return result, errors.New("bursar: claimed billing event has no claim token")
		}
	default:
		return result, fmt.Errorf("bursar: unknown billing event claim state %q", claim.State)
	}
	event.EventID = event.canonicalEventID()
	event.ID = event.EventID
	event.BillingEventID = claim.BillingEventID

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
	} else if processor, ok := s.store.(configuredBillingLifecycleProcessor); ok && IsBillingLifecycleEventType(event.Type) {
		processed, processErr := processor.processBillingEvent(ctx, event, accountID, s.provisioning, s.autoSelectEntitlementSource, s.pastDueGracePeriod, s.terminalPlanKey)
		if processErr != nil {
			return s.fail(ctx, event, claim.ClaimToken, processErr)
		}
		if !processed.Handled {
			return s.fail(ctx, event, claim.ClaimToken, fmt.Errorf("billing lifecycle processor did not handle event %q", event.Type))
		}
		if processed.AccountID == "" {
			processed.AccountID = accountID
		}
		lifecycleResult = &processed
	} else if processor, ok := s.store.(BillingLifecycleProcessor); ok && IsBillingLifecycleEventType(event.Type) {
		processed, processErr := processor.ProcessBillingEvent(ctx, event, accountID)
		if processErr != nil {
			return s.fail(ctx, event, claim.ClaimToken, processErr)
		}
		if !processed.Handled {
			return s.fail(ctx, event, claim.ClaimToken, fmt.Errorf("billing lifecycle processor did not handle event %q", event.Type))
		}
		if processed.AccountID == "" {
			processed.AccountID = accountID
		}
		lifecycleResult = &processed
	} else {
		// Expired/upcoming provider events are intentionally acknowledged: their
		// state has no financial effect until a later confirmed lifecycle event.
		if event.Type == BillingEventCheckoutExpired || event.Type == BillingEventInvoiceUpcoming {
			if event.Type == BillingEventCheckoutExpired {
				if intentID := billingMetadataString(event.Metadata, "checkout_intent_id"); intentID != "" {
					if intents, ok := s.store.(interface {
						UpdateCheckoutIntent(context.Context, string, string, CheckoutIntentUpdate) error
					}); ok {
						if err := intents.UpdateCheckoutIntent(ctx, intentID, accountID, CheckoutIntentUpdate{Status: "expired"}); err != nil {
							return s.fail(ctx, event, claim.ClaimToken, err)
						}
					}
				}
			}
			if _, err := s.store.CompleteBillingEvent(ctx, event.Provider, event.canonicalEventID(), claim.ClaimToken); err != nil {
				return result, fmt.Errorf("bursar: complete ignored billing event: %w", err)
			}
			return BillingEventResult{Handled: true, Ignored: true, AccountID: accountID}, nil
		}
		return s.fail(ctx, event, claim.ClaimToken, fmt.Errorf("no handler for billing event %q", event.Type))
	}
	completed, err := s.store.CompleteBillingEvent(ctx, event.Provider, event.canonicalEventID(), claim.ClaimToken)
	if err != nil {
		return result, fmt.Errorf("bursar: complete billing event: %w", err)
	}
	if !completed {
		return result, fmt.Errorf("bursar: billing event %s completion claim was lost", event.canonicalEventID())
	}
	if lifecycleResult != nil {
		if !lifecycleResult.Ignored {
			s.fireEventHandler(ctx, event, lifecycleResult.AccountID)
		}
		return *lifecycleResult, nil
	}
	s.fireEventHandler(ctx, event, accountID)
	return BillingEventResult{Handled: true, AccountID: accountID}, nil
}

func (s *BillingService) fail(ctx context.Context, event BillingEvent, claimToken string, cause error) (BillingEventResult, error) {
	message := persistedDiagnosticSummary(cause, "billing_event_failed")
	failed, err := s.store.FailBillingEvent(ctx, event.Provider, event.canonicalEventID(), claimToken, message)
	if err != nil {
		return BillingEventResult{}, fmt.Errorf("bursar: fail billing event after handler error %q: %w", message, err)
	}
	if !failed {
		return BillingEventResult{}, fmt.Errorf("bursar: billing event %s failure claim was lost: %w", event.canonicalEventID(), cause)
	}
	return BillingEventResult{}, cause
}
