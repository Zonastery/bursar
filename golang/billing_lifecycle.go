// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import (
	"context"
	"time"
)

// BillingLifecycleProcessor is an optional BillingStore capability for the
// durable, built-in billing lifecycle. It is invoked only after Bursar has
// claimed a verified event and resolved its account, and before it completes
// that claim. Implementations must make their domain changes durable before
// returning Handled=true.
//
// Application-registered BillingEventHandlers take precedence over this
// processor. This keeps application callbacks isolated from store-owned
// persistence and lets callers explicitly replace one normalized route when
// necessary.
type BillingLifecycleProcessor interface {
	ProcessBillingEvent(context.Context, BillingEvent, string) (BillingEventResult, error)
}

type configuredBillingLifecycleProcessor interface {
	processBillingEvent(context.Context, BillingEvent, string, BillingProvisioningPort, bool, time.Duration, string) (BillingEventResult, error)
}

// IsBillingLifecycleEventType reports whether eventType belongs to Bursar's
// provider-neutral billing lifecycle vocabulary. It includes the historical
// invoice.updated and dispute.updated aliases accepted by the Go SDK, as well
// as the shared JavaScript and Python vocabulary.
func IsBillingLifecycleEventType(eventType BillingEventType) bool {
	switch eventType {
	case BillingEventCustomerCreated,
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
		BillingEventInvoiceUpdated,
		BillingEventInvoiceFinalized,
		BillingEventInvoiceFinalizationFailed,
		BillingEventInvoiceUpcoming,
		BillingEventInvoicePaid,
		BillingEventInvoicePaymentFailed,
		BillingEventInvoicePaymentActionRequired,
		BillingEventInvoiceVoided,
		BillingEventPaymentSucceeded,
		BillingEventPaymentFailed,
		BillingEventPaymentMethodAttached,
		BillingEventPaymentMethodUpdated,
		BillingEventPaymentMethodDetached,
		BillingEventRefundCreated,
		BillingEventRefundUpdated,
		BillingEventRefundFailed,
		BillingEventDisputeCreated,
		BillingEventDisputeUpdated,
		BillingEventDisputeClosed:
		return true
	default:
		return false
	}
}
