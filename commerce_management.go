// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"sort"
	"strings"
	"time"
)

var (
	currentSubscriptionStatuses   = []string{"active", "trialing", "past_due", "incomplete"}
	blockingSubscriptionStatuses  = []string{"active", "trialing", "past_due", "incomplete"}
	cancellableSubscriptionStates = []string{"active", "trialing", "past_due", "incomplete", "unpaid", "paused"}
	terminalSubscriptionStatuses  = []string{"canceled", "unpaid", "paused", "incomplete_expired", "expired"}
)

func (s *CommerceService) requireState() (CommerceStateStore, error) {
	if s == nil || s.state == nil {
		return nil, NewError("core billing state is unavailable for this commerce operation", ErrorOptions{
			Code:      ErrorCodeCoreBillingDataUnavailable,
			Category:  ErrorCategoryUnavailable,
			Retryable: true,
		})
	}
	return s.state, nil
}

// GetCheckoutStatus returns a subject-scoped checkout status and reconciles a
// completed, failed, or expired provider session into its durable intent. The
// provider remains advisory; verified webhooks remain the authoritative
// financial lifecycle input.
func (s *CommerceService) GetCheckoutStatus(ctx context.Context, intentID, subjectID string) (CheckoutStatusResult, error) {
	intent, err := s.GetCheckoutIntent(ctx, intentID, subjectID)
	if err != nil {
		return CheckoutStatusResult{}, err
	}
	if intent == nil {
		return CheckoutStatusResult{}, NewError("checkout intent was not found", ErrorOptions{Code: ErrorCodeCommerceResourceNotFound, Category: ErrorCategoryNotFound})
	}
	status, err := s.resolveCheckoutStatus(ctx, *intent, subjectID)
	if err != nil {
		return CheckoutStatusResult{}, err
	}
	return CheckoutStatusResult{IntentID: intent.ID, Status: status}, nil
}

func (s *CommerceService) resolveCheckoutStatus(ctx context.Context, intent CheckoutIntent, subjectID string) (CheckoutStatus, error) {
	switch strings.ToLower(strings.TrimSpace(intent.Status)) {
	case "completed":
		return CheckoutStatusSucceeded, nil
	case "failed", "cancelled", "canceled":
		return CheckoutStatusFailed, nil
	case "expired":
		return CheckoutStatusExpired, nil
	case "open", "":
		// Continue with expiry/provider reconciliation.
	default:
		return CheckoutStatusFailed, nil
	}
	if !intent.ExpiresAt.IsZero() && !intent.ExpiresAt.After(time.Now().UTC()) {
		if err := s.store.UpdateCheckoutIntent(ctx, intent.ID, subjectID, CheckoutIntentUpdate{Status: "expired"}); err != nil {
			return "", err
		}
		return CheckoutStatusExpired, nil
	}
	if strings.TrimSpace(intent.ProviderSessionID) == "" || s.providers == nil {
		return CheckoutStatusPending, nil
	}
	provider, err := s.providers.Get(ctx, intent.Provider)
	if err != nil {
		return "", err
	}
	statusProvider, ok := provider.(CheckoutStatusProvider)
	if !ok {
		return CheckoutStatusPending, nil
	}
	providerStatus, err := statusProvider.GetCheckoutSessionStatus(ctx, intent.ProviderSessionID)
	if err != nil {
		return "", err
	}
	status := strings.ToLower(strings.TrimSpace(providerStatus))
	var durableStatus string
	var result CheckoutStatus
	switch status {
	case "complete", "completed", "paid", "succeeded":
		durableStatus, result = "completed", CheckoutStatusSucceeded
	case "expired":
		durableStatus, result = "expired", CheckoutStatusExpired
	case "failed", "cancelled", "canceled", "requires_payment_method":
		durableStatus, result = "failed", CheckoutStatusFailed
	default:
		return CheckoutStatusPending, nil
	}
	if err := s.store.UpdateCheckoutIntent(ctx, intent.ID, subjectID, CheckoutIntentUpdate{Status: durableStatus}); err != nil {
		return "", err
	}
	return result, nil
}

// CancelSubscription requests cancellation of one account-owned current
// subscription. The final state is provider-webhook-owned, so a successful
// response is marked Pending rather than claiming an immediate cancellation.
func (s *CommerceService) CancelSubscription(ctx context.Context, input SubscriptionCommandInput) (SubscriptionCommandResult, error) {
	accountID, operationKey, err := validateSubscriptionCommand(input)
	if err != nil {
		return SubscriptionCommandResult{}, err
	}
	subscription, err := s.subscriptionForCommand(ctx, accountID, input.SubscriptionID, currentSubscriptionStatuses)
	if err != nil {
		return SubscriptionCommandResult{}, err
	}
	if subscription == nil {
		return SubscriptionCommandResult{}, NewError("no active subscription was found", ErrorOptions{Code: ErrorCodeCommerceResourceNotFound, Category: ErrorCategoryNotFound})
	}
	if !containsText(cancellableSubscriptionStates, subscription.Status) || subscription.CancelAtPeriodEnd {
		return SubscriptionCommandResult{OK: true}, nil
	}
	provider, err := s.providers.Get(ctx, subscription.Provider)
	if err != nil {
		return SubscriptionCommandResult{}, err
	}
	cancellation, ok := provider.(SubscriptionProvider)
	if !ok {
		return SubscriptionCommandResult{}, providerCapabilityError(provider.Name(), "cancel_subscription")
	}
	if err := cancellation.CancelSubscription(ctx, subscription.ProviderSubscriptionID, operationKey); err != nil {
		return SubscriptionCommandResult{}, err
	}
	return SubscriptionCommandResult{OK: true, Pending: true}, nil
}

// ReactivateSubscription removes a scheduled cancellation for one
// account-owned subscription when the configured provider supports it.
func (s *CommerceService) ReactivateSubscription(ctx context.Context, input SubscriptionCommandInput) (SubscriptionCommandResult, error) {
	accountID, operationKey, err := validateSubscriptionCommand(input)
	if err != nil {
		return SubscriptionCommandResult{}, err
	}
	subscription, err := s.subscriptionForCommand(ctx, accountID, input.SubscriptionID, nil)
	if err != nil {
		return SubscriptionCommandResult{}, err
	}
	if subscription == nil {
		return SubscriptionCommandResult{}, NewError("no subscription was found", ErrorOptions{Code: ErrorCodeCommerceResourceNotFound, Category: ErrorCategoryNotFound})
	}
	if subscription.Status == "active" && !subscription.CancelAtPeriodEnd {
		return SubscriptionCommandResult{OK: true}, nil
	}
	provider, err := s.providers.Get(ctx, subscription.Provider)
	if err != nil {
		return SubscriptionCommandResult{}, err
	}
	reactivation, ok := provider.(SubscriptionProvider)
	if !ok {
		return SubscriptionCommandResult{}, providerCapabilityError(provider.Name(), "reactivate_subscription")
	}
	if err := reactivation.ReactivateSubscription(ctx, subscription.ProviderSubscriptionID, operationKey); err != nil {
		return SubscriptionCommandResult{}, err
	}
	return SubscriptionCommandResult{OK: true, Pending: true}, nil
}

// CancelAllSubscriptions cancels all eligible subscriptions for an account.
// Each provider call gets a stable child key derived from OperationKey, making
// a retry safe even after a partial provider outage.
func (s *CommerceService) CancelAllSubscriptions(ctx context.Context, accountID, operationKey string) (CancelAllSubscriptionsResult, error) {
	accountID, err := requireText(accountID, "account ID")
	if err != nil {
		return CancelAllSubscriptionsResult{}, err
	}
	operationKey, err = requireStableKey(operationKey, "cancel all operation key")
	if err != nil {
		return CancelAllSubscriptionsResult{}, err
	}
	state, err := s.requireState()
	if err != nil {
		return CancelAllSubscriptionsResult{}, err
	}
	subscriptions, err := state.ListBillingSubscriptions(ctx, accountID)
	if err != nil {
		return CancelAllSubscriptionsResult{}, err
	}
	sort.Slice(subscriptions, func(left, right int) bool {
		if subscriptions[left].Provider != subscriptions[right].Provider {
			return subscriptions[left].Provider < subscriptions[right].Provider
		}
		return subscriptions[left].ProviderSubscriptionID < subscriptions[right].ProviderSubscriptionID
	})
	result := CancelAllSubscriptionsResult{AccountID: accountID, Subscriptions: make([]SubscriptionCancellationResult, 0, len(subscriptions))}
	for _, subscription := range subscriptions {
		if !containsText(cancellableSubscriptionStates, subscription.Status) || subscription.CancelAtPeriodEnd {
			continue
		}
		childKey, keyErr := commerceScopedKey(operationKey, subscription.Provider+":"+subscription.ProviderSubscriptionID)
		if keyErr != nil {
			return result, keyErr
		}
		command, commandErr := s.CancelSubscription(ctx, SubscriptionCommandInput{
			AccountID: accountID, SubscriptionID: subscription.ProviderSubscriptionID, OperationKey: childKey,
		})
		item := SubscriptionCancellationResult{Provider: subscription.Provider, ProviderSubscriptionID: subscription.ProviderSubscriptionID, Canceled: command.OK}
		if commandErr != nil {
			item.Canceled = false
			item.Error = commandErr.Error()
		}
		result.Subscriptions = append(result.Subscriptions, item)
		if item.Canceled {
			result.CanceledCount++
		}
	}
	for _, item := range result.Subscriptions {
		if !item.Canceled {
			return result, NewError("one or more subscriptions could not be cancelled", ErrorOptions{Code: ErrorCodeBilling, Category: ErrorCategoryUnavailable, Details: map[string]any{"account_id": accountID, "failed_subscription": item.ProviderSubscriptionID}})
		}
	}
	return result, nil
}

// GetAccountSubscriptionSummary returns the account-scoped lifecycle and
// entitlement summary used by self-service billing views.
func (s *CommerceService) GetAccountSubscriptionSummary(ctx context.Context, accountID string) (AccountSubscriptionSummary, error) {
	accountID, err := requireText(accountID, "account ID")
	if err != nil {
		return AccountSubscriptionSummary{}, err
	}
	state, err := s.requireState()
	if err != nil {
		return AccountSubscriptionSummary{}, err
	}
	subscription, err := state.GetBillingSubscription(ctx, accountID, nil)
	if err != nil {
		return AccountSubscriptionSummary{}, err
	}
	entitlement, err := s.credits.GetUserPlan(ctx, accountID)
	if err != nil {
		return AccountSubscriptionSummary{}, err
	}
	var pending *BillingSubscriptionChange
	if subscription != nil && subscription.ProviderSubscriptionID != "" {
		pending, err = state.GetOpenBillingSubscriptionChange(ctx, subscription.Provider, subscription.ProviderSubscriptionID)
		if err != nil {
			return AccountSubscriptionSummary{}, err
		}
	}
	return buildSubscriptionSummary(accountID, subscription, entitlement, pending), nil
}

// PreviewPlanChange obtains a fresh provider quote for an account-owned
// subscription. The quote must be fingerprinted and revalidated by
// ConfirmPlanChange before a provider mutation is attempted.
func (s *CommerceService) PreviewPlanChange(ctx context.Context, input PreviewPlanChangeInput) (PlanChangePreviewResult, error) {
	planContext, err := s.planChangeContext(ctx, input)
	if err != nil {
		return PlanChangePreviewResult{}, err
	}
	if planContext.classification == PlanChangeUnchanged {
		return PlanChangePreviewResult{Unchanged: true, Classification: PlanChangeUnchanged, PlanKey: planContext.targetPlan, Interval: planContext.targetInterval}, nil
	}
	previewProvider, ok := planContext.provider.(PlanChangePreviewProvider)
	if !ok {
		return PlanChangePreviewResult{}, providerCapabilityError(planContext.provider.Name(), "preview_plan_change")
	}
	preview, err := previewProvider.PreviewPlanChange(ctx, planContext.providerRequest(""))
	if err != nil {
		return PlanChangePreviewResult{}, err
	}
	fingerprint, err := planChangeQuoteFingerprint(preview)
	if err != nil {
		return PlanChangePreviewResult{}, err
	}
	return PlanChangePreviewResult{
		Classification:   planContext.classification,
		Scheduled:        planContext.policy.Effective == "renewal",
		PlanKey:          planContext.targetPlan,
		Interval:         planContext.targetInterval,
		Preview:          &preview,
		QuoteFingerprint: fingerprint,
	}, nil
}

// ConfirmPlanChange re-prices the change, rejects a stale quote, creates a
// durable idempotency record, and then asks the provider to perform it.
func (s *CommerceService) ConfirmPlanChange(ctx context.Context, input ConfirmPlanChangeInput) (ConfirmPlanChangeResult, error) {
	operationKey, err := requireStableKey(input.OperationKey, "plan change operation key")
	if err != nil {
		return ConfirmPlanChangeResult{}, err
	}
	planContext, err := s.planChangeContext(ctx, PreviewPlanChangeInput{AccountID: input.AccountID, OfferKey: input.OfferKey})
	if err != nil {
		return ConfirmPlanChangeResult{}, err
	}
	if planContext.classification == PlanChangeUnchanged {
		return ConfirmPlanChangeResult{Success: true, Unchanged: true, PlanKey: planContext.targetPlan, Interval: planContext.targetInterval}, nil
	}
	previewProvider, ok := planContext.provider.(PlanChangePreviewProvider)
	if !ok {
		return ConfirmPlanChangeResult{}, providerCapabilityError(planContext.provider.Name(), "preview_plan_change")
	}
	preview, err := previewProvider.PreviewPlanChange(ctx, planContext.providerRequest(""))
	if err != nil {
		return ConfirmPlanChangeResult{}, err
	}
	fingerprint, err := planChangeQuoteFingerprint(preview)
	if err != nil {
		return ConfirmPlanChangeResult{}, err
	}
	if strings.TrimSpace(input.QuoteFingerprint) == "" || !strings.EqualFold(input.QuoteFingerprint, fingerprint) {
		return ConfirmPlanChangeResult{}, NewError("the financial preview changed", ErrorOptions{Code: ErrorCodeQuoteChanged, Category: ErrorCategoryConflict, Details: map[string]any{"quote_fingerprint": fingerprint}})
	}
	state, err := s.requireState()
	if err != nil {
		return ConfirmPlanChangeResult{}, err
	}
	existing, err := state.GetOpenBillingSubscriptionChange(ctx, planContext.subscription.Provider, planContext.subscription.ProviderSubscriptionID)
	if err != nil {
		return ConfirmPlanChangeResult{}, err
	}
	if existing != nil {
		return ConfirmPlanChangeResult{}, NewError("a subscription change is already pending", ErrorOptions{Code: ErrorCodeCheckoutConflict, Category: ErrorCategoryConflict, Details: map[string]any{"subscription_change_id": existing.ID}})
	}
	scheduled := planContext.policy.Effective == "renewal"
	effectiveAt := preview.EffectiveAt
	if scheduled {
		if preview.NextBillingDate == nil {
			return ConfirmPlanChangeResult{}, NewError("provider did not return the scheduled change date", ErrorOptions{Code: ErrorCodeCoreBillingDataUnavailable, Category: ErrorCategoryUnavailable, Retryable: true})
		}
		effectiveAt = *preview.NextBillingDate
	}
	changeProvider, ok := planContext.provider.(PlanChangeProvider)
	if !ok {
		return ConfirmPlanChangeResult{}, providerCapabilityError(planContext.provider.Name(), "change_plan")
	}
	change, err := state.CreateBillingSubscriptionChange(ctx, BillingSubscriptionChangeCreate{
		Provider:               planContext.subscription.Provider,
		ProviderSubscriptionID: planContext.subscription.ProviderSubscriptionID,
		ToOfferID:              planContext.offer.ID,
		ToOfferKey:             input.OfferKey,
		ToPlanKey:              planContext.targetPlan,
		ToInterval:             planContext.targetInterval,
		Effective:              planContext.policy.Effective,
		EffectiveAt:            effectiveAt,
		ProrationBehavior:      planContext.policy.Proration,
		OperationKey:           operationKey,
	})
	if err != nil {
		return ConfirmPlanChangeResult{}, err
	}
	var reactivated bool
	if planContext.subscription.CancelAtPeriodEnd {
		provider, ok := planContext.provider.(SubscriptionProvider)
		if !ok {
			failure := providerCapabilityError(planContext.provider.Name(), "reactivate_subscription")
			message := failure.Error()
			_ = state.UpdateBillingSubscriptionChange(ctx, change.ID, BillingSubscriptionChangeUpdate{State: stringPointer("failed"), ErrorMessage: &message})
			return ConfirmPlanChangeResult{}, failure
		}
		keepKey, keyErr := commerceScopedKey(operationKey, "keep-cancellation")
		if keyErr != nil {
			message := keyErr.Error()
			_ = state.UpdateBillingSubscriptionChange(ctx, change.ID, BillingSubscriptionChangeUpdate{State: stringPointer("failed"), ErrorMessage: &message})
			return ConfirmPlanChangeResult{}, keyErr
		}
		if err := provider.ReactivateSubscription(ctx, planContext.subscription.ProviderSubscriptionID, keepKey); err != nil {
			message := err.Error()
			_ = state.UpdateBillingSubscriptionChange(ctx, change.ID, BillingSubscriptionChangeUpdate{State: stringPointer("failed"), ErrorMessage: &message})
			return ConfirmPlanChangeResult{}, err
		}
		reactivated = true
	}
	_, providerErr := changeProvider.ChangePlan(ctx, planContext.providerRequest(operationKey))
	if providerErr != nil {
		failure := providerErr
		if reactivated {
			if provider, ok := planContext.provider.(SubscriptionProvider); ok {
				restoreKey, keyErr := commerceScopedKey(operationKey, "restore-cancellation")
				if keyErr == nil {
					if restoreErr := provider.CancelSubscription(ctx, planContext.subscription.ProviderSubscriptionID, restoreKey); restoreErr != nil {
						failure = fmt.Errorf("plan change failed: %w; restoring cancellation: %v", providerErr, restoreErr)
					}
				}
			}
		}
		message := failure.Error()
		_ = state.UpdateBillingSubscriptionChange(ctx, change.ID, BillingSubscriptionChangeUpdate{State: stringPointer("failed"), ErrorMessage: &message})
		return ConfirmPlanChangeResult{}, failure
	}
	// The database creates renewal changes in scheduled state and immediate
	// changes in awaiting_payment state. Both terminal transitions are applied
	// by verified provider webhooks; issuing a same-state update would be an
	// intentional database no-op and could not persist an operation ID alone.
	return ConfirmPlanChangeResult{Success: true, Pending: true, Scheduled: scheduled, EffectiveAt: &effectiveAt, PlanKey: planContext.targetPlan, Interval: planContext.targetInterval}, nil
}

// CancelScheduledPlanChange cancels an account-owned renewal-scheduled change
// through providers that expose the matching optional capability.
func (s *CommerceService) CancelScheduledPlanChange(ctx context.Context, accountID, operationKey string) error {
	accountID, err := requireText(accountID, "account ID")
	if err != nil {
		return err
	}
	operationKey, err = requireStableKey(operationKey, "scheduled plan change operation key")
	if err != nil {
		return err
	}
	subscription, err := s.subscriptionForCommand(ctx, accountID, "", currentSubscriptionStatuses)
	if err != nil {
		return err
	}
	if subscription == nil {
		return NewError("no active subscription was found", ErrorOptions{Code: ErrorCodeCommerceResourceNotFound, Category: ErrorCategoryNotFound})
	}
	state, err := s.requireState()
	if err != nil {
		return err
	}
	change, err := state.GetOpenBillingSubscriptionChange(ctx, subscription.Provider, subscription.ProviderSubscriptionID)
	if err != nil {
		return err
	}
	if change == nil || change.State != "scheduled" || change.Effective != "renewal" {
		return NewError("no scheduled plan change was found", ErrorOptions{Code: ErrorCodeCommerceResourceNotFound, Category: ErrorCategoryNotFound})
	}
	provider, err := s.providers.Get(ctx, subscription.Provider)
	if err != nil {
		return err
	}
	cancel, ok := provider.(ScheduledPlanChangeCancellationProvider)
	if !ok {
		return providerCapabilityError(provider.Name(), "cancel_scheduled_plan_change")
	}
	if err := cancel.CancelScheduledPlanChange(ctx, subscription.ProviderSubscriptionID, change.ProviderOperationID, operationKey); err != nil {
		return err
	}
	return state.UpdateBillingSubscriptionChange(ctx, change.ID, BillingSubscriptionChangeUpdate{State: stringPointer("canceled")})
}

// CreatePortalSession creates an account-scoped hosted billing or payment
// method route using a resolved persisted customer, never a caller-provided
// provider customer ID.
func (s *CommerceService) CreatePortalSession(ctx context.Context, input PortalSessionInput) (string, error) {
	accountID, err := requireText(input.AccountID, "account ID")
	if err != nil {
		return "", err
	}
	if strings.TrimSpace(input.ReturnURL) == "" {
		return "", NewError("portal return URL is required", ErrorOptions{Code: ErrorCodeConfig, Category: ErrorCategoryInvalidRequest})
	}
	state, err := s.requireState()
	if err != nil {
		return "", err
	}
	subscription, err := state.GetBillingSubscription(ctx, accountID, nil)
	if err != nil {
		return "", err
	}
	providerName := ""
	if subscription != nil {
		providerName = subscription.Provider
	}
	customer, err := state.GetBillingCustomer(ctx, accountID, providerName)
	if err != nil {
		return "", err
	}
	if customer == nil || strings.TrimSpace(customer.ProviderCustomerID) == "" {
		return "", NewError("no billing customer was found", ErrorOptions{Code: ErrorCodeCommerceResourceNotFound, Category: ErrorCategoryNotFound})
	}
	provider, err := s.providers.Get(ctx, customer.Provider)
	if err != nil {
		return "", err
	}
	purpose := input.Purpose
	if purpose == "" {
		purpose = PortalPurposeBilling
	}
	if purpose == PortalPurposePaymentMethod {
		portals, ok := provider.(PaymentMethodPortalProvider)
		if !ok {
			return "", providerCapabilityError(provider.Name(), "payment_method_portal")
		}
		if subscription != nil && strings.TrimSpace(subscription.ProviderSubscriptionID) != "" {
			return portals.CreateUpdatePaymentMethodSession(ctx, customer.ProviderCustomerID, subscription.ProviderSubscriptionID, input.ReturnURL)
		}
		return portals.CreatePaymentMethodSetupSession(ctx, customer.ProviderCustomerID, input.ReturnURL, input.CancelURL)
	}
	if purpose != PortalPurposeBilling {
		return "", NewError("portal purpose is invalid", ErrorOptions{Code: ErrorCodeConfig, Category: ErrorCategoryInvalidRequest})
	}
	portal, ok := provider.(CustomerPortalProvider)
	if !ok {
		return "", providerCapabilityError(provider.Name(), "customer_portal")
	}
	return portal.CreateCustomerPortalSession(ctx, customer.ProviderCustomerID, input.ReturnURL)
}

// UpdatePreferences applies a partial account-owned preference update.
func (s *CommerceService) UpdatePreferences(ctx context.Context, accountID string, patch PreferencePatch) (BillingPreferences, error) {
	accountID, err := requireText(accountID, "account ID")
	if err != nil {
		return BillingPreferences{}, err
	}
	state, err := s.requireState()
	if err != nil {
		return BillingPreferences{}, err
	}
	current, err := state.GetBillingPreferences(ctx, accountID)
	if err != nil {
		return BillingPreferences{}, err
	}
	next := defaultBillingPreferences(accountID)
	if current != nil {
		next = *current
		next.AccountID = accountID
	}
	if patch.AutoRecharge != nil {
		next.AutoRecharge = *patch.AutoRecharge
	}
	if patch.OverageProtection != nil {
		next.OverageProtection = *patch.OverageProtection
	}
	if patch.EmailNotifications != nil {
		next.EmailNotifications = *patch.EmailNotifications
	}
	if patch.UsageAlerts != nil {
		next.UsageAlerts = *patch.UsageAlerts
	}
	if patch.InvoiceReminders != nil {
		next.InvoiceReminders = *patch.InvoiceReminders
	}
	if err := state.UpsertBillingPreferences(ctx, next); err != nil {
		return BillingPreferences{}, err
	}
	return next, nil
}

// GetInvoiceLink verifies account ownership of a provider invoice or ledger
// document before invoking the provider's optional invoice URL capability.
func (s *CommerceService) GetInvoiceLink(ctx context.Context, input GetInvoiceLinkInput) (string, error) {
	accountID, err := requireText(input.AccountID, "account ID")
	if err != nil {
		return "", err
	}
	var providerName, documentID string
	switch input.Document.Kind {
	case "provider_invoice":
		state, stateErr := s.requireState()
		if stateErr != nil {
			return "", stateErr
		}
		invoices, listErr := state.ListBillingInvoices(ctx, accountID)
		if listErr != nil {
			return "", listErr
		}
		for _, invoice := range invoices {
			if invoice.Provider == input.Document.Provider && invoice.ID == input.Document.ProviderDocumentID {
				providerName, documentID = invoice.Provider, invoice.ID
				break
			}
		}
	case "ledger_entry":
		entry, entryErr := s.credits.GetLedgerEntry(ctx, accountID, input.Document.LedgerEntryID)
		if entryErr != nil {
			return "", entryErr
		}
		if entry != nil {
			providerName = metadataText(entry.Metadata, "provider")
			documentID = firstNonEmpty(metadataText(entry.Metadata, "provider_document_id"), metadataText(entry.Metadata, "provider_invoice_id"), metadataText(entry.Metadata, "provider_payment_id"))
		}
	default:
		return "", NewError("invoice document kind is invalid", ErrorOptions{Code: ErrorCodeConfig, Category: ErrorCategoryInvalidRequest})
	}
	if strings.TrimSpace(providerName) == "" || strings.TrimSpace(documentID) == "" {
		return "", NewError("invoice was not found", ErrorOptions{Code: ErrorCodeCommerceResourceNotFound, Category: ErrorCategoryNotFound})
	}
	provider, err := s.providers.Get(ctx, providerName)
	if err != nil {
		return "", err
	}
	invoices, ok := provider.(InvoiceProvider)
	if !ok {
		return "", providerCapabilityError(provider.Name(), "invoice_url")
	}
	url, err := invoices.GetInvoiceURL(ctx, documentID)
	if err != nil {
		return "", err
	}
	if strings.TrimSpace(url) == "" {
		return "", NewError("provider returned no invoice URL", ErrorOptions{Code: ErrorCodeCommerceResourceNotFound, Category: ErrorCategoryNotFound})
	}
	return url, nil
}

// GetAccountOverview joins durable credit and commerce projections. It uses
// the same account ID for every read, avoiding unscoped provider lookups.
func (s *CommerceService) GetAccountOverview(ctx context.Context, accountID string) (AccountCommerceOverview, error) {
	accountID, err := requireText(accountID, "account ID")
	if err != nil {
		return AccountCommerceOverview{}, err
	}
	balance, err := s.credits.GetBalance(ctx, accountID)
	if err != nil {
		return AccountCommerceOverview{}, err
	}
	available, err := s.credits.GetAvailable(ctx, accountID)
	if err != nil {
		return AccountCommerceOverview{}, err
	}
	buckets, err := s.credits.GetBucketBalances(ctx, accountID)
	if err != nil {
		return AccountCommerceOverview{}, err
	}
	entitlement, err := s.credits.GetUserPlan(ctx, accountID)
	if err != nil {
		return AccountCommerceOverview{}, err
	}
	allowance, err := s.credits.CheckAllowance(ctx, accountID)
	if err != nil {
		return AccountCommerceOverview{}, err
	}
	summary, err := s.GetAccountSubscriptionSummary(ctx, accountID)
	if err != nil {
		return AccountCommerceOverview{}, err
	}
	state, err := s.requireState()
	if err != nil {
		return AccountCommerceOverview{}, err
	}
	prefs, err := state.GetBillingPreferences(ctx, accountID)
	if err != nil {
		return AccountCommerceOverview{}, err
	}
	invoices, err := state.ListBillingInvoices(ctx, accountID)
	if err != nil {
		return AccountCommerceOverview{}, err
	}
	transactions, err := s.credits.ListLedgerEntries(ctx, accountID, ListLedgerEntriesOptions{Limit: 100})
	if err != nil {
		return AccountCommerceOverview{}, err
	}
	usage, err := s.credits.ListUsageCharges(ctx, accountID, ListUsageChargesOptions{Limit: 100})
	if err != nil {
		return AccountCommerceOverview{}, err
	}
	preferences := defaultBillingPreferences(accountID)
	if prefs != nil {
		preferences = *prefs
		preferences.AccountID = accountID
	}
	return AccountCommerceOverview{AccountID: accountID, Balance: balance, Available: available, Buckets: buckets, Entitlement: entitlement, Allowance: allowance, SubscriptionSummary: summary, Subscription: summary.Subscription, PendingChange: summary.PendingChange, Preferences: preferences, Invoices: invoices, Transactions: transactions, Usage: usage}, nil
}

type commercePlanChangeContext struct {
	subscription   *CommerceSubscription
	provider       PaymentProvider
	offer          *BillingOffer
	targetPlan     string
	targetInterval string
	classification PlanChangeClassification
	policy         SubscriptionChangePolicy
}

func (s *CommerceService) planChangeContext(ctx context.Context, input PreviewPlanChangeInput) (commercePlanChangeContext, error) {
	accountID, err := requireText(input.AccountID, "account ID")
	if err != nil {
		return commercePlanChangeContext{}, err
	}
	offerKey, err := requireText(input.OfferKey, "target offer key")
	if err != nil {
		return commercePlanChangeContext{}, err
	}
	state, err := s.requireState()
	if err != nil {
		return commercePlanChangeContext{}, err
	}
	subscription, err := state.GetBillingSubscription(ctx, accountID, currentSubscriptionStatuses)
	if err != nil {
		return commercePlanChangeContext{}, err
	}
	if subscription == nil || strings.TrimSpace(subscription.ProviderSubscriptionID) == "" {
		return commercePlanChangeContext{}, NewError("no active subscription was found", ErrorOptions{Code: ErrorCodeCommerceResourceNotFound, Category: ErrorCategoryNotFound})
	}
	config, err := s.catalog.GetConfig(ctx)
	if err != nil {
		return commercePlanChangeContext{}, err
	}
	target, found := config.Commerce.Offers[offerKey]
	if !found || target.Type != "subscription" || target.Plan == nil || target.BillingInterval == nil {
		return commercePlanChangeContext{}, NewError("target offer is not a subscription offer", ErrorOptions{Code: ErrorCodeUnknownOffer, Category: ErrorCategoryNotFound, Details: map[string]any{"offer_key": offerKey}})
	}
	provider, err := s.providers.Get(ctx, subscription.Provider)
	if err != nil {
		return commercePlanChangeContext{}, err
	}
	reference, found := target.Providers[provider.Name()]
	if !found {
		return commercePlanChangeContext{}, NewError("target offer is unavailable from the subscription provider", ErrorOptions{Code: ErrorCodeUnknownOffer, Category: ErrorCategoryNotFound, Details: map[string]any{"offer_key": offerKey, "provider": provider.Name()}})
	}
	productID, priceID, lookupKey := providerReferenceIDs(reference)
	offer, err := state.ResolveBillingOffer(ctx, provider.Name(), productID, priceID, lookupKey)
	if err != nil {
		return commercePlanChangeContext{}, err
	}
	if offer == nil {
		return commercePlanChangeContext{}, NewError("target offer is not available in persisted billing state", ErrorOptions{Code: ErrorCodeCommerceResourceNotFound, Category: ErrorCategoryNotFound})
	}
	entitlement, err := s.credits.GetUserPlan(ctx, accountID)
	if err != nil {
		return commercePlanChangeContext{}, err
	}
	currentPlan := firstNonEmpty(entitlement.PlanKey, subscription.PlanKey)
	if currentPlan == "" {
		return commercePlanChangeContext{}, NewError("current subscription plan is unknown", ErrorOptions{Code: ErrorCodeCommerceResourceNotFound, Category: ErrorCategoryNotFound})
	}
	classification, err := classifyPlanChange(config, currentPlan, subscription.Interval, *target.Plan, target.BillingInterval.Unit)
	if err != nil {
		return commercePlanChangeContext{}, err
	}
	policy := SubscriptionChangePolicy{}
	if classification != PlanChangeUnchanged {
		var exists bool
		policy, exists = config.Commerce.SubscriptionChanges[string(classification)]
		if !exists {
			return commercePlanChangeContext{}, NewError("subscription change policy is missing", ErrorOptions{Code: ErrorCodePlanChangePolicyMissing, Category: ErrorCategoryUnavailable, Retryable: true, Details: map[string]any{"classification": classification}})
		}
	}
	return commercePlanChangeContext{subscription: subscription, provider: provider, offer: offer, targetPlan: *target.Plan, targetInterval: target.BillingInterval.Unit, classification: classification, policy: policy}, nil
}

func (c commercePlanChangeContext) providerRequest(idempotencyKey string) ProviderPlanChangeRequest {
	effectiveAt := "immediately"
	if c.policy.Effective == "renewal" {
		effectiveAt = "next_billing_date"
	}
	proration := "prorated_immediately"
	if c.policy.Proration == "none" {
		proration = "do_not_bill"
	}
	return ProviderPlanChangeRequest{ProviderSubscriptionID: c.subscription.ProviderSubscriptionID, ProductID: firstNonEmpty(c.offer.PriceID, c.offer.ProductID, c.offer.LookupKey), EffectiveAt: effectiveAt, ProrationBillingMode: proration, PaymentFailure: c.policy.PaymentFailure, Quantity: 1, Metadata: map[string]string{"bursar_account_id": c.subscription.AccountID, "plan_slug": c.targetPlan, "billing_interval": c.targetInterval}, IdempotencyKey: idempotencyKey}
}

func (s *CommerceService) subscriptionForCommand(ctx context.Context, accountID, subscriptionID string, statuses []string) (*CommerceSubscription, error) {
	state, err := s.requireState()
	if err != nil {
		return nil, err
	}
	if strings.TrimSpace(subscriptionID) == "" {
		return state.GetBillingSubscription(ctx, accountID, statuses)
	}
	subscriptions, err := state.ListBillingSubscriptions(ctx, accountID)
	if err != nil {
		return nil, err
	}
	for index := range subscriptions {
		subscription := &subscriptions[index]
		if (subscription.ID == subscriptionID || subscription.ProviderSubscriptionID == subscriptionID) && (len(statuses) == 0 || containsText(statuses, subscription.Status)) {
			return subscription, nil
		}
	}
	return nil, nil
}

func validateSubscriptionCommand(input SubscriptionCommandInput) (string, string, error) {
	accountID, err := requireText(input.AccountID, "account ID")
	if err != nil {
		return "", "", err
	}
	operationKey, err := requireStableKey(input.OperationKey, "subscription operation key")
	if err != nil {
		return "", "", err
	}
	return accountID, operationKey, nil
}

func buildSubscriptionSummary(accountID string, subscription *CommerceSubscription, entitlement GetUserPlanResult, pending *BillingSubscriptionChange) AccountSubscriptionSummary {
	status := ""
	planKey := entitlement.PlanKey
	if subscription != nil {
		status = subscription.Status
		if planKey == "" {
			planKey = subscription.PlanKey
		}
	}
	isCurrent := containsText(currentSubscriptionStatuses, status)
	isEntitled := entitlement.PlanKey != ""
	inGrace := status == "past_due" && subscription != nil && subscription.GraceEndsAt != nil && subscription.GraceEndsAt.After(time.Now().UTC())
	access := "none"
	if isEntitled {
		if inGrace {
			access = "grace"
		} else {
			access = "entitled"
		}
	} else if status != "" {
		access = "blocked"
	}
	return AccountSubscriptionSummary{AccountID: accountID, PlanKey: planKey, Status: status, LifecycleState: firstNonEmpty(status, "none"), AccessState: access, IsCurrent: isCurrent, IsEntitled: isEntitled, IsBlockingCheckout: containsText(blockingSubscriptionStatuses, status), IsCancellable: containsText(cancellableSubscriptionStates, status) && (subscription == nil || !subscription.CancelAtPeriodEnd), IsTerminal: containsText(terminalSubscriptionStatuses, status), Subscription: subscription, PendingChange: pending}
}

func classifyPlanChange(config *BursarConfig, currentPlan, currentInterval, targetPlan, targetInterval string) (PlanChangeClassification, error) {
	if currentPlan == targetPlan {
		if currentInterval == targetInterval {
			return PlanChangeUnchanged, nil
		}
		return PlanChangeCadenceChange, nil
	}
	current, currentFound := config.Plans[currentPlan]
	target, targetFound := config.Plans[targetPlan]
	if !currentFound || !targetFound {
		return "", NewError("subscription plan is absent from the active catalog", ErrorOptions{Code: ErrorCodeCoreBillingDataUnavailable, Category: ErrorCategoryUnavailable, Retryable: true})
	}
	if target.Rank > current.Rank {
		return PlanChangeUpgrade, nil
	}
	if target.Rank < current.Rank {
		return PlanChangeDowngrade, nil
	}
	return PlanChangeLateral, nil
}

func providerReferenceIDs(reference ProviderReference) (productID, priceID, lookupKey string) {
	if reference.ProductID != nil {
		productID = *reference.ProductID
	}
	if reference.PriceID != nil {
		priceID = *reference.PriceID
	}
	if reference.ExternalID != nil {
		lookupKey = *reference.ExternalID
	}
	return productID, priceID, lookupKey
}

func planChangeQuoteFingerprint(preview PlanChangePreview) (string, error) {
	lines := make([]map[string]any, 0, len(preview.LineItems))
	for _, line := range preview.LineItems {
		lines = append(lines, map[string]any{"productId": line.ProductID, "unitPrice": line.UnitPrice.String(), "quantity": line.Quantity, "prorationFactor": line.ProrationFactor.String(), "currency": strings.ToUpper(line.Currency), "tax": line.Tax.String(), "subtotal": line.Subtotal.String()})
	}
	payload := map[string]any{"totalAmount": preview.TotalAmount.String(), "settlementAmount": preview.SettlementAmount.String(), "currency": strings.ToUpper(preview.Currency), "recurringAmount": amountString(preview.RecurringAmount), "recurringCurrency": strings.ToUpper(preview.RecurringCurrency), "taxAmount": amountString(preview.TaxAmount), "customerCredits": amountString(preview.CustomerCredits), "nextBillingDate": timeString(preview.NextBillingDate), "lineItems": lines}
	encoded, err := json.Marshal(payload)
	if err != nil {
		return "", NewError("serialize plan change quote", ErrorOptions{Code: ErrorCodeConfig, Category: ErrorCategoryInvalidRequest, Cause: err})
	}
	digest := sha256.Sum256(encoded)
	return hex.EncodeToString(digest[:]), nil
}

func commerceScopedKey(operationKey, scope string) (string, error) {
	operationKey, err := requireStableKey(operationKey, "operation key")
	if err != nil {
		return "", err
	}
	digest := sha256.Sum256([]byte(scope))
	return requireStableKey(operationKey+":"+hex.EncodeToString(digest[:8]), "scoped operation key")
}

func providerCapabilityError(provider, capability string) error {
	return NewError("provider does not support "+capability, ErrorOptions{Code: ErrorCodeProviderCapabilityNotSupported, Category: ErrorCategoryInvalidRequest, Details: map[string]any{"provider": provider, "capability": capability}})
}

func defaultBillingPreferences(accountID string) BillingPreferences {
	return BillingPreferences{AccountID: accountID, AutoRecharge: false, OverageProtection: true, EmailNotifications: true, UsageAlerts: true, InvoiceReminders: false}
}

func metadataText(metadata CreditMetadata, key string) string {
	if metadata == nil {
		return ""
	}
	value, _ := metadata[key].(string)
	return strings.TrimSpace(value)
}

func amountString(value *Amount) any {
	if value == nil {
		return nil
	}
	return value.String()
}

func timeString(value *time.Time) any {
	if value == nil {
		return nil
	}
	return value.UTC().Format(time.RFC3339Nano)
}

func stringPointer(value string) *string { return &value }
