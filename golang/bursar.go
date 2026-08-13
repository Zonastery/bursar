// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import (
	"context"
	"fmt"
	"sort"
	"time"
)

// BillingServiceOptions configures application-owned handlers around Bursar's
// durable billing event lifecycle. Financial provisioning belongs in these
// handlers or a BillingStore implementation; it is never inferred from an
// unverified provider webhook.
type BillingServiceOptions struct {
	Handlers                    map[BillingEventType]BillingEventHandler
	DefaultHandler              BillingEventHandler
	Provisioning                BillingProvisioningPort
	AutoSelectEntitlementSource *bool
	PastDueGracePeriod          *time.Duration
	TerminalPlanKey             string
}

// Options constructs Bursar's single application-facing facade. CreditStore
// is always required. Billing and commerce are optional capabilities, but
// commerce requires billing and a durable checkout-intent store.
type Options struct {
	CreditStore     CreditStore
	CreditsOptions  CreditsServiceOptions
	Emitter         CreditEventSink
	BillingStore    BillingStore
	BillingOptions  *BillingServiceOptions
	CommerceOptions *CommerceOptions
}

// Bursar groups a tenant's catalog, credits, account lifecycle, billing, and
// optional commerce capabilities. Applications should construct this facade
// once per tenant-bound store instead of wiring unrelated service instances.
type Bursar struct {
	Credits  *CreditsService
	Catalog  *CatalogService
	Accounts *AccountService
	Billing  *BillingService
	Commerce *CommerceService
}

// New constructs the application-facing Bursar facade.
func New(options Options) (*Bursar, error) {
	if options.CreditStore == nil {
		return nil, NewError("Bursar requires a credit store", ErrorOptions{Code: ErrorCodeConfig, Category: ErrorCategoryInvalidRequest})
	}
	if options.BillingStore == nil && (options.BillingOptions != nil || options.CommerceOptions != nil) {
		return nil, NewError("billing and commerce options require a billing store", ErrorOptions{Code: ErrorCodeConfig, Category: ErrorCategoryInvalidRequest})
	}
	if options.Emitter != nil && options.CreditsOptions.EventSink != nil {
		return nil, NewError("configure a credit event sink with either Emitter or CreditsOptions.EventSink", ErrorOptions{Code: ErrorCodeConfig, Category: ErrorCategoryInvalidRequest})
	}
	if err := validateFacadeEnvironments(options); err != nil {
		return nil, err
	}

	creditsOptions := options.CreditsOptions
	if options.Emitter != nil {
		creditsOptions.EventSink = options.Emitter
	}
	credits, err := NewCreditsService(options.CreditStore, creditsOptions)
	if err != nil {
		return nil, err
	}
	catalog := credits.Catalog()
	accounts, err := NewAccountService(credits, catalog)
	if err != nil {
		return nil, err
	}
	sdk := &Bursar{Credits: credits, Catalog: catalog, Accounts: accounts}
	if options.BillingStore == nil {
		return sdk, nil
	}
	var billing *BillingService
	if options.BillingOptions == nil {
		billing, err = NewBillingService(options.BillingStore)
	} else {
		billing, err = NewBillingService(options.BillingStore, *options.BillingOptions)
	}
	if err != nil {
		return nil, err
	}
	if billing.provisioning == nil {
		billing.provisioning = credits
	}
	autoRechargeStore, _ := options.BillingStore.(AutoRechargeStore)
	autoRechargeOptions := AutoRechargeServiceOptions{}
	if options.CommerceOptions != nil {
		if options.CommerceOptions.AutoRechargeStore != nil {
			autoRechargeStore = options.CommerceOptions.AutoRechargeStore
		}
		autoRechargeOptions = options.CommerceOptions.AutoRechargeOptions
	}
	if autoRechargeStore != nil {
		billing.AutoRecharge, err = NewAutoRechargeService(catalog, autoRechargeStore, autoRechargeOptions)
		if err != nil {
			return nil, err
		}
	}
	sdk.Billing = billing
	if options.CommerceOptions == nil {
		return sdk, nil
	}
	commerceOptions := *options.CommerceOptions
	if commerceOptions.Store == nil {
		if store, ok := options.BillingStore.(CommerceStore); ok {
			commerceOptions.Store = store
		}
	}
	if commerceOptions.StateStore == nil {
		if store, ok := options.BillingStore.(CommerceStateStore); ok {
			commerceOptions.StateStore = store
		}
	}
	if commerceOptions.AutoRechargeStore == nil {
		if store, ok := options.BillingStore.(AutoRechargeStore); ok {
			commerceOptions.AutoRechargeStore = store
		}
	}
	commerce, err := NewCommerceService(billing, catalog, credits, commerceOptions)
	if err != nil {
		return nil, err
	}
	sdk.Commerce = commerce
	return sdk, nil
}

// LoadCatalog loads the active persisted catalog into this process's exact
// pricing engine. It must succeed before callers use Catalog.Engine.
func (b *Bursar) LoadCatalog(ctx context.Context) error {
	if b == nil || b.Catalog == nil {
		return NewError("Bursar catalog capability is not configured", ErrorOptions{Code: ErrorCodeCatalogNotLoaded, Category: ErrorCategoryUnavailable})
	}
	return b.Catalog.Load(ctx)
}

// RequireBilling returns billing or a typed capability error.
func (b *Bursar) RequireBilling() (*BillingService, error) {
	if b == nil || b.Billing == nil {
		return nil, NewError("Bursar billing capability is not configured", ErrorOptions{Code: ErrorCodeCapabilityNotConfigured, Category: ErrorCategoryInvalidRequest})
	}
	return b.Billing, nil
}

// RequireCommerce returns commerce or a typed capability error.
func (b *Bursar) RequireCommerce() (*CommerceService, error) {
	if b == nil || b.Commerce == nil {
		return nil, NewError("Bursar commerce capability is not configured", ErrorOptions{Code: ErrorCodeCommerceNotConfigured, Category: ErrorCategoryInvalidRequest})
	}
	return b.Commerce, nil
}

// IngestBillingEvent feeds a previously verified, normalized provider event
// through facade-owned billing event claiming and completion.
func (b *Bursar) IngestBillingEvent(ctx context.Context, event BillingEvent) (BillingEventResult, error) {
	billing, err := b.RequireBilling()
	if err != nil {
		return BillingEventResult{}, err
	}
	return billing.Ingest(ctx, event)
}

// Close releases credit-store resources. Billing stores that own separate
// resources should expose and close their own lifecycle in application setup.
func (b *Bursar) Close() error {
	if b == nil {
		return nil
	}
	if b.Commerce != nil {
		b.Commerce.Close()
	}
	if b.Credits == nil {
		return nil
	}
	return b.Credits.Close()
}

type providerEnvironmentSource interface {
	ProviderEnvironment() ProviderEnvironment
}

func validateFacadeEnvironments(options Options) error {
	environments := map[string]ProviderEnvironment{}
	if source, ok := options.CreditStore.(providerEnvironmentSource); ok {
		if environment := source.ProviderEnvironment(); environment != "" {
			environments["credit_store"] = environment
		}
	}
	if options.BillingStore != nil {
		environments["billing_store"] = options.BillingStore.ProviderEnvironment()
	}
	if options.CommerceOptions != nil && options.CommerceOptions.Providers != nil {
		if environment := options.CommerceOptions.Providers.Environment(); environment != "" {
			environments["commerce_providers"] = environment
		}
	}
	if len(environments) < 2 {
		return nil
	}
	values := make([]ProviderEnvironment, 0, len(environments))
	for _, environment := range environments {
		if err := environment.Validate(); err != nil {
			return err
		}
		values = append(values, environment)
	}
	for _, environment := range values[1:] {
		if environment != values[0] {
			keys := make([]string, 0, len(environments))
			for key := range environments {
				keys = append(keys, key)
			}
			sort.Strings(keys)
			parts := make([]string, 0, len(keys))
			for _, key := range keys {
				parts = append(parts, fmt.Sprintf("%s=%s", key, environments[key]))
			}
			return NewError("Bursar provider environments must match: "+joinStrings(parts, ", "), ErrorOptions{Code: ErrorCodeConfig, Category: ErrorCategoryInvalidRequest})
		}
	}
	return nil
}

func applyBillingOptions(service *BillingService, options *BillingServiceOptions) error {
	if options == nil {
		return nil
	}
	keys := make([]string, 0, len(options.Handlers))
	for eventType := range options.Handlers {
		keys = append(keys, string(eventType))
	}
	sort.Strings(keys)
	for _, key := range keys {
		eventType := BillingEventType(key)
		if err := service.On(eventType, options.Handlers[eventType]); err != nil {
			return err
		}
	}
	service.SetDefaultHandler(options.DefaultHandler)
	if options.Provisioning != nil {
		service.provisioning = options.Provisioning
	}
	service.autoSelectEntitlementSource = true
	if options.AutoSelectEntitlementSource != nil {
		service.autoSelectEntitlementSource = *options.AutoSelectEntitlementSource
	}
	if options.PastDueGracePeriod != nil && *options.PastDueGracePeriod < 0 {
		return NewError("billing past-due grace period must be non-negative", ErrorOptions{Code: ErrorCodeConfig, Category: ErrorCategoryInvalidRequest})
	}
	if options.PastDueGracePeriod != nil {
		service.pastDueGracePeriod = *options.PastDueGracePeriod
	}
	service.terminalPlanKey = options.TerminalPlanKey
	return nil
}

func joinStrings(values []string, separator string) string {
	if len(values) == 0 {
		return ""
	}
	result := values[0]
	for _, value := range values[1:] {
		result += separator + value
	}
	return result
}
