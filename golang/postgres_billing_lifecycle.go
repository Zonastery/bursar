// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import (
	"context"
	"strings"
	"time"
)

var (
	_ BillingLifecycleProcessor           = (*PostgresStore)(nil)
	_ configuredBillingLifecycleProcessor = (*PostgresStore)(nil)
	_ BillingProvisioningPort             = (*PostgresStore)(nil)
)

// ProcessBillingEvent claims and applies verified provider truth to the
// tenant-scoped PostgreSQL billing model. Direct callers go through the same
// claim/complete/fail lifecycle as BillingService so they cannot bypass replay
// protection or entitlement reconciliation's billing-event fence.
func (s *PostgresStore) ProcessBillingEvent(ctx context.Context, event BillingEvent, accountID string) (BillingEventResult, error) {
	if strings.TrimSpace(event.BillingEventID) != "" {
		return BillingEventResult{}, NewError("billing event claim identifiers are internal", ErrorOptions{Code: ErrorCodeBilling, Category: ErrorCategoryInvalidRequest})
	}
	if strings.TrimSpace(event.AccountID) == "" {
		event.AccountID = strings.TrimSpace(accountID)
	}
	service, err := NewBillingService(s, BillingServiceOptions{Provisioning: s})
	if err != nil {
		return BillingEventResult{}, err
	}
	return service.Ingest(ctx, event)
}

func (s *PostgresStore) processBillingEvent(ctx context.Context, event BillingEvent, accountID string, provisioning BillingProvisioningPort, autoSelect bool, gracePeriod time.Duration, terminalPlanKey string) (BillingEventResult, error) {
	processor := postgresBillingLifecycle{store: s, provisioning: provisioning, autoSelectEntitlementSource: autoSelect, pastDueGracePeriod: gracePeriod, terminalPlanKey: terminalPlanKey}
	return processor.process(ctx, event, accountID)
}

type postgresBillingLifecycle struct {
	store                       *PostgresStore
	provisioning                BillingProvisioningPort
	autoSelectEntitlementSource bool
	pastDueGracePeriod          time.Duration
	terminalPlanKey             string
}

func (p postgresBillingLifecycle) process(ctx context.Context, event BillingEvent, accountID string) (BillingEventResult, error) {
	if err := validateBillingLifecycleMetadata(event.Metadata); err != nil {
		return BillingEventResult{}, err
	}
	resolved, err := p.resolveAccount(ctx, event, accountID)
	if err != nil {
		return BillingEventResult{}, err
	}
	accountID = resolved
	switch event.Type {
	case BillingEventCustomerCreated, BillingEventCustomerUpdated:
		if event.Customer != nil && accountID != "" && event.Customer.canonicalProviderCustomerID() != "" {
			if err := p.store.UpsertBillingCustomer(ctx, BillingCustomerRecord{Provider: event.Provider, ProviderCustomerID: event.Customer.canonicalProviderCustomerID(), AccountID: accountID, Email: event.Customer.Email}); err != nil {
				return BillingEventResult{}, err
			}
		}
		return handledBilling(event.Type, accountID, ""), nil
	case BillingEventCustomerDeleted:
		return handledBilling(event.Type, accountID, ""), nil
	case BillingEventCheckoutCompleted:
		if event.Customer != nil && accountID != "" && event.Customer.canonicalProviderCustomerID() != "" {
			if err := p.store.UpsertBillingCustomer(ctx, BillingCustomerRecord{Provider: event.Provider, ProviderCustomerID: event.Customer.canonicalProviderCustomerID(), AccountID: accountID, Email: event.Customer.Email}); err != nil {
				return BillingEventResult{}, err
			}
		}
		if event.Subscription != nil {
			return p.applySubscription(ctx, event, accountID, "subscription_created")
		}
		if err := p.updateCheckoutFromEvent(ctx, event, "completed"); err != nil {
			return BillingEventResult{}, err
		}
		return handledBilling(event.Type, accountID, ""), nil
	case BillingEventCheckoutExpired:
		if err := p.updateCheckoutFromEvent(ctx, event, "expired"); err != nil {
			return BillingEventResult{}, err
		}
		result := handledBilling(event.Type, accountID, "")
		result.Ignored = true
		return result, nil
	case BillingEventSubscriptionCreated:
		return p.applySubscription(ctx, event, accountID, "subscription_created")
	case BillingEventSubscriptionUpdated:
		return p.applySubscription(ctx, event, accountID, "subscription_updated")
	case BillingEventSubscriptionActivated:
		event.Subscription.Status = "active"
		return p.applySubscription(ctx, event, accountID, "subscription_activated")
	case BillingEventSubscriptionRenewed:
		event.Subscription.Status = "active"
		return p.applySubscription(ctx, event, accountID, "subscription_renewed")
	case BillingEventSubscriptionPlanChanged:
		return p.applySubscription(ctx, event, accountID, "subscription_plan_changed")
	case BillingEventSubscriptionCancellationScheduled:
		event.Subscription.CancelAtPeriodEnd = true
		return p.applySubscription(ctx, event, accountID, "cancellation_scheduled")
	case BillingEventSubscriptionCancellationUnscheduled:
		event.Subscription.CancelAtPeriodEnd = false
		return p.applySubscription(ctx, event, accountID, "cancellation_unscheduled")
	case BillingEventSubscriptionCanceled:
		event.Subscription.Status = "canceled"
		event.Subscription.CancelAtPeriodEnd = true
		return p.applySubscription(ctx, event, accountID, "subscription_canceled")
	case BillingEventSubscriptionExpired:
		event.Subscription.Status = "expired"
		return p.applySubscription(ctx, event, accountID, "subscription_expired")
	case BillingEventSubscriptionPaused:
		event.Subscription.Status = "paused"
		return p.applySubscription(ctx, event, accountID, "subscription_paused")
	case BillingEventSubscriptionResumed:
		event.Subscription.Status = "active"
		event.Subscription.CancelAtPeriodEnd = false
		return p.applySubscription(ctx, event, accountID, "subscription_resumed")
	case BillingEventSubscriptionTrialWillEnd:
		result := handledBilling(event.Type, accountID, event.Subscription.canonicalProviderSubscriptionID())
		result.Action = "trial_will_end_notified"
		return result, nil
	case BillingEventInvoicePaid:
		if event.Subscription != nil {
			event.Subscription.Status = "active"
			subscriptionResult, applyErr := p.applySubscription(ctx, event, accountID, "subscription_renewed")
			if applyErr != nil {
				return BillingEventResult{}, applyErr
			}
			if subscriptionResult.Ignored {
				return subscriptionResult, nil
			}
		}
		if err := p.persistInvoice(ctx, event, accountID); err != nil {
			return BillingEventResult{}, err
		}
		return handledBilling(event.Type, accountID, subscriptionEventID(event)), nil
	case BillingEventInvoiceCreated, BillingEventInvoiceUpdated, BillingEventInvoiceFinalized, BillingEventInvoiceFinalizationFailed, BillingEventInvoicePaymentFailed, BillingEventInvoicePaymentActionRequired, BillingEventInvoiceVoided:
		if accountID != "" {
			if err := p.persistInvoice(ctx, event, accountID); err != nil {
				return BillingEventResult{}, err
			}
		}
		return handledBilling(event.Type, accountID, subscriptionEventID(event)), nil
	case BillingEventInvoiceUpcoming:
		result := handledBilling(event.Type, accountID, subscriptionEventID(event))
		result.Ignored = true
		return result, nil
	case BillingEventPaymentSucceeded:
		return p.paymentSucceeded(ctx, event, accountID)
	case BillingEventPaymentFailed:
		return p.paymentFailed(ctx, event, accountID)
	case BillingEventRefundCreated, BillingEventRefundUpdated, BillingEventRefundFailed:
		return p.refund(ctx, event, accountID)
	case BillingEventDisputeCreated, BillingEventDisputeUpdated, BillingEventDisputeClosed:
		if err := p.store.UpsertBillingDisputeState(ctx, BillingDisputeUpsert{Provider: event.Provider, ProviderDisputeID: event.Dispute.canonicalProviderDisputeID(), ProviderPaymentID: event.Dispute.canonicalProviderPaymentID(), Status: event.Dispute.Status, Reason: event.Dispute.Reason, ProviderUpdatedAt: event.OccurredAt, Metadata: event.Metadata}); err != nil {
			return BillingEventResult{}, err
		}
		return handledBilling(event.Type, accountID, ""), nil
	case BillingEventPaymentMethodAttached, BillingEventPaymentMethodUpdated, BillingEventPaymentMethodDetached:
		return handledBilling(event.Type, accountID, ""), nil
	default:
		return BillingEventResult{}, NewError("unsupported billing lifecycle event", ErrorOptions{Code: ErrorCodeBilling, Category: ErrorCategoryInvalidRequest, Details: map[string]any{"event_type": event.Type}})
	}
}

func validateBillingLifecycleMetadata(metadata map[string]any) error {
	value, exists := metadata["checkout_intent_id"]
	if !exists {
		return nil
	}
	intentID, ok := value.(string)
	if !ok || strings.TrimSpace(intentID) == "" {
		return NewError("billing checkout intent ID is invalid", ErrorOptions{Code: ErrorCodeBilling, Category: ErrorCategoryInvalidRequest})
	}
	_, err := postgresUUID(strings.TrimSpace(intentID), "checkout intent ID")
	return err
}

func (p postgresBillingLifecycle) resolveAccount(ctx context.Context, event BillingEvent, accountID string) (string, error) {
	if value := strings.TrimSpace(firstNonEmpty(accountID, event.AccountID)); value != "" {
		return value, nil
	}
	if event.Customer != nil && event.Customer.canonicalProviderCustomerID() != "" {
		customer, err := p.store.GetBillingCustomerByProvider(ctx, event.Provider, event.Customer.canonicalProviderCustomerID())
		if err != nil {
			return "", err
		}
		if customer != nil {
			return customer.AccountID, nil
		}
	}
	if event.Subscription != nil && event.Subscription.canonicalProviderSubscriptionID() != "" {
		subscription, err := p.store.GetBillingSubscriptionByProvider(ctx, event.Provider, event.Subscription.canonicalProviderSubscriptionID())
		if err != nil {
			return "", err
		}
		if subscription != nil {
			return subscription.AccountID, nil
		}
	}
	paymentID := ""
	if event.Payment != nil {
		paymentID = event.Payment.canonicalProviderPaymentID()
	} else if event.Refund != nil {
		paymentID = event.Refund.canonicalProviderPaymentID()
	} else if event.Dispute != nil {
		paymentID = event.Dispute.canonicalProviderPaymentID()
	}
	if paymentID != "" {
		payment, err := p.store.GetBillingPaymentByProvider(ctx, event.Provider, paymentID)
		if err != nil {
			return "", err
		}
		if payment != nil {
			return payment.AccountID, nil
		}
	}
	return "", nil
}

func (p postgresBillingLifecycle) applySubscription(ctx context.Context, event BillingEvent, accountID, action string) (BillingEventResult, error) {
	if accountID == "" {
		return BillingEventResult{}, NewStoreError("billing subscription account could not be resolved", ErrorOptions{Retryable: true})
	}
	subscription := event.Subscription
	providerSubscriptionID := subscription.canonicalProviderSubscriptionID()
	if event.Customer != nil && event.Customer.canonicalProviderCustomerID() != "" {
		if err := p.store.UpsertBillingCustomer(ctx, BillingCustomerRecord{Provider: event.Provider, ProviderCustomerID: event.Customer.canonicalProviderCustomerID(), AccountID: accountID, Email: event.Customer.Email}); err != nil {
			return BillingEventResult{}, err
		}
	}
	existing, err := p.store.GetBillingSubscriptionByProvider(ctx, event.Provider, providerSubscriptionID)
	if err != nil {
		return BillingEventResult{}, err
	}
	offer, err := p.offerForEvent(ctx, event)
	if err != nil {
		return BillingEventResult{}, err
	}
	if offer == nil && existing == nil {
		return BillingEventResult{}, NewStoreError("billing subscription offer could not be resolved", ErrorOptions{Retryable: true, Details: map[string]any{"provider": event.Provider, "provider_subscription_id": providerSubscriptionID}})
	}
	if offer == nil {
		offer = &BillingOffer{ID: existing.OfferID, OfferKey: existing.OfferKey, PlanID: existing.PlanID, PlanKey: existing.PlanKey, Interval: existing.Interval, IntervalCnt: existing.IntervalCount}
	}
	if action == "subscription_created" {
		all, listErr := p.store.ListBillingSubscriptions(ctx, accountID)
		if listErr != nil {
			return BillingEventResult{}, listErr
		}
		for _, other := range all {
			if other.Provider == event.Provider && other.ProviderSubscriptionID != providerSubscriptionID && oneOf(other.Status, "active", "trialing", "past_due", "incomplete") {
				conflictErr := p.store.RecordSubscriptionConflict(ctx, BillingSubscriptionConflictCreate{AccountID: accountID, Provider: event.Provider, DuplicateProviderSubscriptionID: providerSubscriptionID, ExistingProviderSubscriptionID: other.ProviderSubscriptionID, ProviderEventID: event.canonicalEventID(), Metadata: event.Metadata})
				if conflictErr != nil {
					return BillingEventResult{}, conflictErr
				}
				return BillingEventResult{}, NewError("billing subscription conflicts with an existing current subscription", ErrorOptions{Code: ErrorCodeBilling, Category: ErrorCategoryConflict})
			}
		}
	}
	status := subscription.Status
	if status == "" && existing != nil {
		status = existing.Status
	}
	if status == "" {
		status = "incomplete"
	}
	endedAt := firstTime(subscription.EndedAt, subscription.CanceledAt)
	if oneOf(status, "canceled", "expired", "incomplete_expired") && endedAt == nil {
		endedAt = existingTime(existing, func(value *CommerceSubscription) *time.Time { return value.EndedAt })
	}
	state := CommerceSubscription{Provider: event.Provider, ProviderSubscriptionID: providerSubscriptionID, AccountID: accountID, ProviderCustomerID: firstNonEmpty(customerProviderID(event.Customer), subscription.CustomerID, existingText(existing, func(value *CommerceSubscription) string { return value.ProviderCustomerID })), OfferID: offer.ID, OfferKey: offer.OfferKey, PlanID: offer.PlanID, PlanKey: offer.PlanKey, Status: status, Interval: firstNonEmpty(subscription.Interval, offer.Interval), IntervalCount: firstPositive(subscription.IntervalCount, offer.IntervalCnt), CurrentPeriodStart: firstTime(subscription.PeriodStart, subscription.CurrentPeriodStart, existingTime(existing, func(value *CommerceSubscription) *time.Time { return value.CurrentPeriodStart })), CurrentPeriodEnd: firstTime(subscription.PeriodEnd, subscription.CurrentPeriodEnd, existingTime(existing, func(value *CommerceSubscription) *time.Time { return value.CurrentPeriodEnd })), TrialEnd: firstTime(subscription.TrialEnd, existingTime(existing, func(value *CommerceSubscription) *time.Time { return value.TrialEnd })), CancelAt: firstTime(subscription.CancelAt, existingTime(existing, func(value *CommerceSubscription) *time.Time { return value.CancelAt })), EndedAt: endedAt, CancelAtPeriodEnd: subscription.CancelAtPeriodEnd, ProviderUpdatedAt: event.OccurredAt, Metadata: mergedBillingMetadata(existing, event.Metadata, subscription.Metadata)}
	if status == "past_due" {
		if existing != nil && existing.GraceEndsAt != nil {
			state.GraceEndsAt = firstTime(existing.GraceEndsAt)
		} else {
			grace := event.OccurredAt.UTC().Add(p.pastDueGracePeriod)
			state.GraceEndsAt = &grace
		}
	}
	if existing != nil && action != "cancellation_scheduled" && action != "cancellation_unscheduled" && !subscription.CancelAtPeriodEnd {
		state.CancelAtPeriodEnd = existing.CancelAtPeriodEnd
	}
	var preservedAllowanceAnchor *time.Time
	if action == "subscription_plan_changed" && p.provisioning != nil {
		currentPlan, planErr := p.provisioning.GetUserPlan(ctx, accountID)
		if planErr != nil {
			return BillingEventResult{}, planErr
		}
		if currentPlan.PlanAssignedAt != nil && !currentPlan.PlanAssignedAt.After(time.Now().UTC()) {
			preservedAllowanceAnchor = firstTime(currentPlan.PlanAssignedAt)
		}
	}
	subscriptionID, err := p.store.UpsertBillingSubscriptionState(ctx, state)
	if err != nil {
		if action == "subscription_created" {
			all, listErr := p.store.ListBillingSubscriptions(ctx, accountID)
			if listErr == nil {
				for _, other := range all {
					if other.Provider == event.Provider && other.ProviderSubscriptionID != providerSubscriptionID && oneOf(other.Status, "active", "trialing", "past_due", "incomplete") {
						if conflictErr := p.store.RecordSubscriptionConflict(ctx, BillingSubscriptionConflictCreate{AccountID: accountID, Provider: event.Provider, DuplicateProviderSubscriptionID: providerSubscriptionID, ExistingProviderSubscriptionID: other.ProviderSubscriptionID, ProviderEventID: event.canonicalEventID(), Metadata: event.Metadata}); conflictErr != nil {
							return BillingEventResult{}, conflictErr
						}
						return BillingEventResult{}, NewError("billing subscription conflicts with an existing current subscription", ErrorOptions{Code: ErrorCodeBilling, Category: ErrorCategoryConflict})
					}
				}
			}
		}
		return BillingEventResult{}, err
	}
	processedAt := time.Now().UTC()
	graceExpired := status == "past_due" && ((existing != nil && existing.GraceExpiredAt != nil) || (state.GraceEndsAt != nil && !state.GraceEndsAt.After(processedAt)))
	automaticEntitlement := p.provisioning != nil && p.autoSelectEntitlementSource
	assignedAt := state.CurrentPeriodStart
	if action == "subscription_plan_changed" {
		assignedAt = preservedAllowanceAnchor
		if assignedAt == nil {
			assignedAt = firstTime(&event.OccurredAt)
		}
	}
	outcome, reconcileErr := p.store.reconcileSubscriptionEntitlement(
		ctx,
		accountID,
		subscriptionID,
		event.BillingEventID,
		status,
		state.ProviderUpdatedAt,
		assignedAt,
		automaticEntitlement,
		p.terminalPlanKey,
		action,
	)
	if reconcileErr != nil {
		return BillingEventResult{}, reconcileErr
	}
	if outcome == subscriptionEntitlementStale {
		return ignoredSubscriptionBilling(event.Type, accountID, providerSubscriptionID, action), nil
	}
	if reconcileErr = validateSubscriptionEntitlementOutcome(status, outcome, automaticEntitlement); reconcileErr != nil {
		return BillingEventResult{}, reconcileErr
	}
	if action == "subscription_plan_changed" {
		if err := p.store.applyOpenBillingSubscriptionChange(ctx, state.Provider, state.ProviderSubscriptionID, state.OfferID); err != nil {
			return BillingEventResult{}, err
		}
	}
	if automaticEntitlement && graceExpired && state.GraceEndsAt != nil {
		state.ID = subscriptionID
		if _, err := p.store.expirePastDueGracePeriod(ctx, state, processedAt, p.terminalPlanKey); err != nil {
			return BillingEventResult{}, err
		}
	}
	if oneOf(status, "active", "trialing") {
		if err := p.updateCheckoutFromEvent(ctx, event, "completed"); err != nil {
			return BillingEventResult{}, err
		}
	}
	if action == "subscription_renewed" && offer.Grant != nil && offer.Grant.Mode == "cycle_grant" && event.BillingEventID != "" {
		grantID, grantErr := p.store.CreateBillingCreditGrant(ctx, BillingCreditGrantCreate{SubscriptionID: subscriptionID, Credits: offer.Grant.Credits, Quantity: 1, BillingEventID: event.BillingEventID})
		if grantErr != nil {
			return BillingEventResult{}, grantErr
		}
		posting, grantErr := p.store.GrantBillingCredit(ctx, grantID, "billing:"+event.canonicalEventID()+":subscription-cycle")
		if grantErr != nil {
			return BillingEventResult{}, grantErr
		}
		if grantErr = billingLifecyclePostingError("billing subscription grant", posting); grantErr != nil {
			return BillingEventResult{}, grantErr
		}
	}
	result := handledBilling(event.Type, accountID, providerSubscriptionID)
	result.Action = action
	return result, nil
}

func (p postgresBillingLifecycle) offerForEvent(ctx context.Context, event BillingEvent) (*BillingOffer, error) {
	if event.Subscription == nil || event.Subscription.Refs == nil {
		return nil, nil
	}
	refs := event.Subscription.Refs
	return p.store.ResolveBillingOffer(ctx, event.Provider, refs.ProductID, refs.PriceID, refs.LookupKey)
}

func (p postgresBillingLifecycle) paymentSucceeded(ctx context.Context, event BillingEvent, accountID string) (BillingEventResult, error) {
	if accountID == "" {
		return BillingEventResult{}, NewStoreError("billing payment account could not be resolved", ErrorOptions{Retryable: true})
	}
	payment := event.Payment
	if payment.Purpose == "subscription" && event.Subscription != nil {
		event.Subscription.Status = "active"
		subscriptionResult, err := p.applySubscription(ctx, event, accountID, "subscription_renewed")
		if err != nil {
			return BillingEventResult{}, err
		}
		if subscriptionResult.Ignored {
			return subscriptionResult, nil
		}
	}
	metadata := cloneAnyMap(event.Metadata)
	var topup *BillingTopupResult
	var err error
	if payment.Purpose == "credit_topup" && payment.Refs != nil {
		topup, err = p.store.ResolveBillingTopup(ctx, event.Provider, payment.Refs.ProductID, payment.Refs.PriceID, payment.Refs.LookupKey)
		if err != nil {
			return BillingEventResult{}, err
		}
		if topup != nil {
			if metadata == nil {
				metadata = make(map[string]any)
			}
			metadata["credits_per_unit"] = topup.CreditsPerUnit.String()
		}
	}
	paymentID, err := p.store.UpsertBillingPaymentState(ctx, BillingPaymentUpsert{Provider: event.Provider, ProviderPaymentID: payment.canonicalProviderPaymentID(), ProviderInvoiceID: payment.InvoiceID, AccountID: accountID, AmountMinor: payment.AmountMinor, TaxMinor: payment.TaxMinor, Currency: payment.Currency, Purpose: payment.Purpose, Status: payment.Status, ProviderUpdatedAt: event.OccurredAt, Metadata: metadata})
	if err != nil {
		return BillingEventResult{}, err
	}
	if payment.Purpose == "credit_topup" {
		if topup == nil {
			return BillingEventResult{}, NewStoreError("billing top-up reference could not be resolved", ErrorOptions{Retryable: true})
		}
		if payment.AmountMinor < topup.MinAmountMinor || payment.AmountMinor > topup.MaxAmountMinor || topup.AmountMinor <= 0 || payment.AmountMinor%topup.AmountMinor != 0 {
			result := handledBilling(event.Type, accountID, "")
			result.Action = "payment_succeeded_out_of_bounds"
			return result, nil
		}
		quantity := payment.AmountMinor / topup.AmountMinor
		if quantity > int64(^uint(0)>>1) {
			return BillingEventResult{}, NewStoreError("billing top-up quantity overflows this platform", ErrorOptions{})
		}
		grantID, grantErr := p.store.CreateBillingCreditGrant(ctx, BillingCreditGrantCreate{PaymentID: paymentID, TopupID: topup.TopupID, Credits: topup.CreditsPerUnit, Quantity: int(quantity), BillingEventID: event.BillingEventID})
		if grantErr != nil {
			return BillingEventResult{}, grantErr
		}
		posting, grantErr := p.store.GrantBillingCredit(ctx, grantID, "billing:"+event.canonicalEventID()+":topup")
		if grantErr != nil {
			return BillingEventResult{}, grantErr
		}
		if grantErr = billingLifecyclePostingError("billing top-up grant", posting); grantErr != nil {
			return BillingEventResult{}, grantErr
		}
		if err := p.store.UpdateAutoRechargeAttemptByProviderPayment(ctx, AutoRechargeProviderPaymentUpdate{Provider: event.Provider, ProviderPaymentID: payment.canonicalProviderPaymentID(), State: AutoRechargeAttemptSucceeded}); err != nil {
			return BillingEventResult{}, err
		}
	}
	if payment.Purpose == "subscription" && event.Subscription != nil {
		invoice := BillingInvoice{ProviderInvoiceID: firstNonEmpty(payment.InvoiceID, payment.canonicalProviderPaymentID()), Provider: event.Provider, Status: "paid", AmountPaidMinor: payment.AmountMinor, AmountDueMinor: payment.AmountMinor, Currency: payment.Currency, PeriodStart: event.Subscription.PeriodStart, PeriodEnd: event.Subscription.PeriodEnd}
		event.Invoice = &invoice
		if err := p.persistInvoice(ctx, event, accountID); err != nil {
			return BillingEventResult{}, err
		}
	}
	if err := p.updateCheckoutFromEvent(ctx, event, "completed"); err != nil {
		return BillingEventResult{}, err
	}
	result := handledBilling(event.Type, accountID, subscriptionEventID(event))
	result.Action = "payment_succeeded"
	return result, nil
}

func (p postgresBillingLifecycle) paymentFailed(ctx context.Context, event BillingEvent, accountID string) (BillingEventResult, error) {
	if accountID != "" && event.Subscription != nil {
		event.Subscription.Status = "past_due"
		subscriptionResult, err := p.applySubscription(ctx, event, accountID, "payment_failed_recorded")
		if err != nil {
			return BillingEventResult{}, err
		}
		if subscriptionResult.Ignored {
			return subscriptionResult, nil
		}
	}
	if accountID != "" {
		payment := event.Payment
		if _, err := p.store.UpsertBillingPaymentState(ctx, BillingPaymentUpsert{Provider: event.Provider, ProviderPaymentID: payment.canonicalProviderPaymentID(), ProviderInvoiceID: payment.InvoiceID, AccountID: accountID, AmountMinor: payment.AmountMinor, TaxMinor: payment.TaxMinor, Currency: payment.Currency, Purpose: payment.Purpose, Status: payment.Status, ProviderUpdatedAt: event.OccurredAt, Metadata: event.Metadata}); err != nil {
			return BillingEventResult{}, err
		}
	}
	if err := p.store.UpdateAutoRechargeAttemptByProviderPayment(ctx, AutoRechargeProviderPaymentUpdate{Provider: event.Provider, ProviderPaymentID: event.Payment.canonicalProviderPaymentID(), State: AutoRechargeAttemptFailed, FailureCode: "provider_payment_failed"}); err != nil {
		return BillingEventResult{}, err
	}
	if err := p.updateCheckoutFromEvent(ctx, event, "failed"); err != nil {
		return BillingEventResult{}, err
	}
	result := handledBilling(event.Type, accountID, subscriptionEventID(event))
	result.Action = "payment_failed_recorded"
	return result, nil
}

func (p postgresBillingLifecycle) refund(ctx context.Context, event BillingEvent, accountID string) (BillingEventResult, error) {
	if accountID == "" {
		return BillingEventResult{}, NewStoreError("billing refund account could not be resolved", ErrorOptions{Retryable: true})
	}
	refund := event.Refund
	refundID, err := p.store.UpsertBillingRefundState(ctx, BillingRefundUpsert{Provider: event.Provider, ProviderRefundID: refund.canonicalProviderRefundID(), ProviderPaymentID: refund.canonicalProviderPaymentID(), AccountID: accountID, AmountMinor: refund.AmountMinor, Currency: refund.Currency, Reason: refund.Reason, Status: refund.Status, ProviderUpdatedAt: event.OccurredAt, Metadata: event.Metadata})
	if err != nil {
		return BillingEventResult{}, err
	}
	result := handledBilling(event.Type, accountID, "")
	result.Action = "refund_recorded"
	if refund.Status != "succeeded" {
		return result, nil
	}
	payment, err := p.store.GetBillingPaymentByProvider(ctx, event.Provider, refund.canonicalProviderPaymentID())
	if err != nil {
		return BillingEventResult{}, err
	}
	if payment == nil || payment.Purpose != "credit_topup" {
		return result, nil
	}
	grantID, err := p.store.GetBillingCreditGrantByPayment(ctx, payment.ID)
	if err != nil || grantID == "" {
		return result, err
	}
	posting, err := p.store.PostBillingRefund(ctx, refundID, grantID, refund.AmountMinor, "billing:"+event.canonicalEventID()+":refund")
	if err != nil {
		return BillingEventResult{}, err
	}
	if err := billingLifecyclePostingError("billing refund", posting); err != nil {
		return BillingEventResult{}, err
	}
	result.Action = "refund_clawback"
	return result, nil
}

func (p postgresBillingLifecycle) persistInvoice(ctx context.Context, event BillingEvent, accountID string) error {
	if event.Invoice == nil {
		return nil
	}
	return p.store.UpsertBillingInvoiceState(ctx, BillingInvoiceUpsert{Provider: event.Provider, ProviderInvoiceID: event.Invoice.canonicalProviderInvoiceID(), ProviderSubscriptionID: subscriptionEventID(event), AccountID: accountID, Status: event.Invoice.Status, AmountPaidMinor: event.Invoice.AmountPaidMinor, AmountDueMinor: event.Invoice.AmountDueMinor, Currency: event.Invoice.Currency, PeriodStart: event.Invoice.PeriodStart, PeriodEnd: event.Invoice.PeriodEnd, ProviderUpdatedAt: event.OccurredAt, Metadata: event.Metadata})
}

func (p postgresBillingLifecycle) updateCheckoutFromEvent(ctx context.Context, event BillingEvent, status string) error {
	value, ok := event.Metadata["checkout_intent_id"].(string)
	if !ok || strings.TrimSpace(value) == "" {
		return nil
	}
	intentID := strings.TrimSpace(value)
	typedIntentID, err := postgresUUID(intentID, "checkout intent ID")
	if err != nil {
		return err
	}
	// The SQL transition is deliberately internal here: a verified webhook does
	// not carry the authenticated SubjectID required by the public store method.
	return p.store.withTx(ctx, func(ctx context.Context, tx *PostgresTransaction) error {
		rows, err := tx.Call(ctx, "advance_checkout_intent", typedIntentID, status, nil, nil)
		if err != nil {
			return err
		}
		row, err := rowRequired(rows, "advance_checkout_intent")
		if err != nil {
			return err
		}
		value, err := firstScalar(row, "advance_checkout_intent")
		if err != nil {
			return err
		}
		advanced, err := scalarBool(value, "advance_checkout_intent")
		if err != nil {
			return err
		}
		if !advanced {
			return NewStoreError("billing checkout intent transition was rejected", ErrorOptions{Details: map[string]any{"checkout_intent_id": intentID, "status": status}})
		}
		return nil
	})
}

func customerProviderID(customer *BillingCustomer) string {
	if customer == nil {
		return ""
	}
	return customer.canonicalProviderCustomerID()
}

func handledBilling(eventType BillingEventType, accountID, subscriptionID string) BillingEventResult {
	return BillingEventResult{Handled: true, AccountID: accountID, Action: strings.ReplaceAll(string(eventType), ".", "_"), SubscriptionID: subscriptionID}
}

func ignoredSubscriptionBilling(eventType BillingEventType, accountID, subscriptionID, action string) BillingEventResult {
	result := handledBilling(eventType, accountID, subscriptionID)
	result.Action = action
	result.Ignored = true
	return result
}

func validateSubscriptionEntitlementOutcome(status string, outcome subscriptionEntitlementOutcome, applyEntitlement bool) error {
	valid := false
	switch {
	case oneOf(status, "active", "trialing"):
		valid = (applyEntitlement && outcome == subscriptionEntitlementApplied) || (!applyEntitlement && outcome == subscriptionEntitlementPreserved)
	case oneOf(status, "incomplete", "past_due"):
		valid = outcome == subscriptionEntitlementPreserved
	case oneOf(status, "incomplete_expired", "paused", "canceled", "unpaid", "expired"):
		valid = outcome == subscriptionEntitlementRevoked || outcome == subscriptionEntitlementPreserved
	}
	if valid {
		return nil
	}
	return NewStoreError("subscription entitlement reconciliation outcome does not match subscription status", ErrorOptions{Details: map[string]any{"status": status, "outcome": outcome}})
}

func subscriptionEventID(event BillingEvent) string {
	if event.Subscription == nil {
		return ""
	}
	return event.Subscription.canonicalProviderSubscriptionID()
}

func existingText(value *CommerceSubscription, getter func(*CommerceSubscription) string) string {
	if value == nil {
		return ""
	}
	return getter(value)
}

func existingTime(value *CommerceSubscription, getter func(*CommerceSubscription) *time.Time) *time.Time {
	if value == nil {
		return nil
	}
	return getter(value)
}

func firstTime(values ...*time.Time) *time.Time {
	for _, value := range values {
		if value != nil {
			copy := value.UTC()
			return &copy
		}
	}
	return nil
}

func firstPositive(values ...int) int {
	for _, value := range values {
		if value > 0 {
			return value
		}
	}
	return 1
}

func mergedBillingMetadata(existing *CommerceSubscription, maps ...map[string]any) map[string]any {
	result := make(map[string]any)
	if existing != nil {
		for key, value := range existing.Metadata {
			result[key] = value
		}
	}
	for _, values := range maps {
		for key, value := range values {
			result[key] = value
		}
	}
	return result
}
