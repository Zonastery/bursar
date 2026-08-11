// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import (
	"context"
	"strings"
)

// CommerceAutoRecharge provides the JavaScript/Python-equivalent
// account-scoped auto-recharge management surface. The lower-level
// AutoRechargeService remains available for integrations that intentionally
// select a provider themselves.
type CommerceAutoRecharge struct {
	commerce *CommerceService
	service  *AutoRechargeService
}

func (a *CommerceAutoRecharge) require() (*CommerceService, *AutoRechargeService, error) {
	if a == nil || a.commerce == nil || a.service == nil {
		return nil, nil, NewError("auto-recharge is not configured", ErrorOptions{Code: ErrorCodeAutoRechargeNotConfigured, Category: ErrorCategoryUnavailable})
	}
	return a.commerce, a.service, nil
}

// GetStatus returns the customer-safe auto-recharge status for an account.
func (a *CommerceAutoRecharge) GetStatus(ctx context.Context, accountID string) (*AutoRechargeStatus, error) {
	commerce, service, err := a.require()
	if err != nil {
		return nil, err
	}
	accountID, err = requireText(accountID, "account ID")
	if err != nil {
		return nil, err
	}
	provider, err := commerce.autoRechargeProvider(ctx, accountID, service)
	if err != nil {
		return nil, err
	}
	return service.GetStatus(ctx, accountID, provider)
}

// Enable validates a saved payment method, persists catalog guardrails, and
// immediately evaluates the account's committed balance.
func (a *CommerceAutoRecharge) Enable(ctx context.Context, input AutoRechargeInput) (*AutoRechargeStatus, error) {
	commerce, service, err := a.require()
	if err != nil {
		return nil, err
	}
	accountID, err := requireText(input.AccountID, "account ID")
	if err != nil {
		return nil, err
	}
	provider, err := commerce.autoRechargeProvider(ctx, accountID, service)
	if err != nil {
		return nil, err
	}
	balance, err := commerce.credits.GetBalance(ctx, accountID)
	if err != nil {
		return nil, err
	}
	return service.Enable(ctx, provider, AutoRechargeProcessInput{UserID: accountID, Balance: balance.Balance, ReturnURL: input.ReturnURL})
}

// Disable is idempotent and does not need a provider lookup.
func (a *CommerceAutoRecharge) Disable(ctx context.Context, accountID string) error {
	_, service, err := a.require()
	if err != nil {
		return err
	}
	return service.Disable(ctx, accountID)
}

// Retry re-arms a paused profile and evaluates the current durable balance.
func (a *CommerceAutoRecharge) Retry(ctx context.Context, input AutoRechargeInput) (AutoRechargeProcessResult, error) {
	commerce, service, err := a.require()
	if err != nil {
		return AutoRechargeProcessResult{}, err
	}
	accountID, err := requireText(input.AccountID, "account ID")
	if err != nil {
		return AutoRechargeProcessResult{}, err
	}
	provider, err := commerce.autoRechargeProvider(ctx, accountID, service)
	if err != nil {
		return AutoRechargeProcessResult{}, err
	}
	balance, err := commerce.credits.GetBalance(ctx, accountID)
	if err != nil {
		return AutoRechargeProcessResult{}, err
	}
	return service.Retry(ctx, provider, AutoRechargeProcessInput{UserID: accountID, Balance: balance.Balance, ReturnURL: input.ReturnURL})
}

// ProcessIfNeeded explicitly evaluates guardrails using the current durable
// balance. Normal credit debits invoke this same path through the facade hook.
func (a *CommerceAutoRecharge) ProcessIfNeeded(ctx context.Context, input AutoRechargeInput) (AutoRechargeProcessResult, error) {
	commerce, service, err := a.require()
	if err != nil {
		return AutoRechargeProcessResult{}, err
	}
	accountID, err := requireText(input.AccountID, "account ID")
	if err != nil {
		return AutoRechargeProcessResult{}, err
	}
	provider, err := commerce.autoRechargeProvider(ctx, accountID, service)
	if err != nil {
		return AutoRechargeProcessResult{}, err
	}
	balance, err := commerce.credits.GetBalance(ctx, accountID)
	if err != nil {
		return AutoRechargeProcessResult{}, err
	}
	return service.ProcessIfNeeded(ctx, provider, AutoRechargeProcessInput{UserID: accountID, Balance: balance.Balance, ReturnURL: input.ReturnURL})
}

func (s *CommerceService) autoRechargeProvider(ctx context.Context, accountID string, autoRecharge *AutoRechargeService) (PaymentProvider, error) {
	if s == nil || s.providers == nil {
		return nil, NewError("commerce providers are not configured", ErrorOptions{Code: ErrorCodeAutoRechargeNotConfigured, Category: ErrorCategoryUnavailable})
	}
	profile, err := autoRecharge.GetProfile(ctx, accountID)
	if err != nil {
		return nil, err
	}
	if profile != nil && strings.TrimSpace(profile.Provider) != "" {
		return s.providers.Get(ctx, profile.Provider)
	}
	if s.state != nil {
		subscription, subscriptionErr := s.state.GetBillingSubscription(ctx, accountID, currentSubscriptionStatuses)
		if subscriptionErr != nil {
			return nil, subscriptionErr
		}
		if subscription != nil && strings.TrimSpace(subscription.Provider) != "" {
			return s.providers.Get(ctx, subscription.Provider)
		}
		customer, customerErr := s.state.GetBillingCustomer(ctx, accountID, "")
		if customerErr != nil {
			return nil, customerErr
		}
		if customer != nil && strings.TrimSpace(customer.Provider) != "" {
			return s.providers.Get(ctx, customer.Provider)
		}
	}
	config, err := s.catalog.GetConfig(ctx)
	if err != nil {
		return nil, err
	}
	if config.Commerce.AutoRecharge == nil || len(config.Commerce.AutoRecharge.EligibleTopups) == 0 {
		return nil, NewError("auto-recharge is not configured for this catalog", ErrorOptions{Code: ErrorCodeAutoRechargeNotConfigured, Category: ErrorCategoryUnavailable})
	}
	offer, found := config.Commerce.Offers[config.Commerce.AutoRecharge.EligibleTopups[0]]
	if !found || offer.Type != "topup" {
		return nil, NewError("auto-recharge has no eligible top-up offer", ErrorOptions{Code: ErrorCodeAutoRechargeNotConfigured, Category: ErrorCategoryUnavailable})
	}
	providers := sortedProviderKeys(offer.Providers)
	if len(providers) == 0 {
		return nil, NewError("auto-recharge offer has no providers", ErrorOptions{Code: ErrorCodeAutoRechargeNotConfigured, Category: ErrorCategoryUnavailable})
	}
	// sortedProviderKeys is deterministic; Select then enforces an explicit
	// default whenever more than one provider could bill the account.
	return s.providers.Select(ctx, "", s.defaultProvider, providers)
}
