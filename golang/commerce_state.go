// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import (
	"context"
	"time"
)

// BillingCustomerRecord is the persisted provider customer associated with a
// Bursar financial account. Provider customer identifiers are never accepted
// as an authorization boundary; Commerce always starts from AccountID.
type BillingCustomerRecord struct {
	Provider           string
	ProviderCustomerID string
	AccountID          string
	Email              string
}

// CommerceSubscription is the customer-owned subscription projection needed
// for self-service commerce. ProviderSubscriptionID is the opaque provider
// identifier used only after the account-scoped store lookup succeeds.
type CommerceSubscription struct {
	ID                     string
	CatalogRevisionID      string
	Provider               string
	ProviderSubscriptionID string
	AccountID              string
	ProviderCustomerID     string
	OfferID                string
	OfferKey               string
	PlanID                 string
	PlanKey                string
	Status                 string
	Interval               string
	IntervalCount          int
	CurrentPeriodStart     *time.Time
	CurrentPeriodEnd       *time.Time
	TrialEnd               *time.Time
	CancelAt               *time.Time
	EndedAt                *time.Time
	CancelAtPeriodEnd      bool
	GraceEndsAt            *time.Time
	GraceExpiredAt         *time.Time
	ProviderUpdatedAt      time.Time
	Metadata               map[string]any
}

// BillingOffer is the persisted catalog/provider mapping used to ensure a
// provider operation only targets a current Bursar offer.
type BillingOffer struct {
	ID          string
	Provider    string
	OfferKey    string
	PlanID      string
	PlanKey     string
	ProductID   string
	PriceID     string
	LookupKey   string
	Interval    string
	IntervalCnt int
	Grant       *BillingGrantResult
}

// BillingSubscriptionChange records a provider-backed plan transition. A
// quote is confirmed only after its fingerprint has been revalidated.
type BillingSubscriptionChange struct {
	ID                  string
	Provider            string
	SubscriptionID      string
	ToOfferID           string
	ToOfferKey          string
	ToPlanKey           string
	ToInterval          string
	Effective           string
	EffectiveAt         time.Time
	ProrationBehavior   string
	State               string
	ProviderOperationID string
	ErrorMessage        string
	CreatedAt           time.Time
	UpdatedAt           time.Time
}

// BillingSubscriptionChangeCreate is the durable, idempotent request made
// before a provider plan-change call. OperationKey must remain stable when a
// caller retries an indeterminate request.
type BillingSubscriptionChangeCreate struct {
	Provider               string
	ProviderSubscriptionID string
	ToOfferID              string
	ToOfferKey             string
	ToPlanKey              string
	ToInterval             string
	Effective              string
	EffectiveAt            time.Time
	ProrationBehavior      string
	OperationKey           string
}

// BillingSubscriptionChangeUpdate contains only provider/result state that
// can change after a durable change row has been created.
type BillingSubscriptionChangeUpdate struct {
	State               *string
	ProviderOperationID *string
	ErrorMessage        *string
}

// BillingPreferences is a durable account-owned preference projection.
type BillingPreferences struct {
	AccountID          string
	AutoRecharge       bool
	OverageProtection  bool
	EmailNotifications bool
	UsageAlerts        bool
	InvoiceReminders   bool
}

// PreferencePatch changes only explicitly provided preference fields. Pointer
// booleans distinguish a requested false value from an omitted field.
type PreferencePatch struct {
	AutoRecharge       *bool
	OverageProtection  *bool
	EmailNotifications *bool
	UsageAlerts        *bool
	InvoiceReminders   *bool
}

// CommerceStateStore supplies the persisted customer, subscription, invoice,
// preference, and plan-change projections required by the full self-service
// commerce surface. It is intentionally separate from CommerceStore: a
// checkout-only integration can still use hosted checkout without pretending
// that subscription management is available.
type CommerceStateStore interface {
	GetBillingCustomer(context.Context, string, string) (*BillingCustomerRecord, error)
	GetBillingSubscription(context.Context, string, []string) (*CommerceSubscription, error)
	ListBillingSubscriptions(context.Context, string) ([]CommerceSubscription, error)
	ResolveBillingOffer(context.Context, string, string, string, string) (*BillingOffer, error)
	CreateBillingSubscriptionChange(context.Context, BillingSubscriptionChangeCreate) (BillingSubscriptionChange, error)
	GetOpenBillingSubscriptionChange(context.Context, string, string) (*BillingSubscriptionChange, error)
	UpdateBillingSubscriptionChange(context.Context, string, BillingSubscriptionChangeUpdate) error
	GetBillingPreferences(context.Context, string) (*BillingPreferences, error)
	UpsertBillingPreferences(context.Context, BillingPreferences) error
	ListBillingInvoices(context.Context, string) ([]BillingInvoice, error)
}

// CheckoutStatus is a compact provider-neutral checkout state.
type CheckoutStatus string

const (
	CheckoutStatusPending   CheckoutStatus = "pending"
	CheckoutStatusSucceeded CheckoutStatus = "succeeded"
	CheckoutStatusFailed    CheckoutStatus = "failed"
	CheckoutStatusExpired   CheckoutStatus = "expired"
)

// CheckoutStatusResult is intentionally scoped to the durable intent, not a
// provider session ID that a caller could guess.
type CheckoutStatusResult struct {
	IntentID string
	Status   CheckoutStatus
}

// SubscriptionCommandInput is shared by cancellation and reactivation. An
// optional SubscriptionID lets an account with multiple historical provider
// subscriptions select one explicitly; it is still resolved account-scoped.
type SubscriptionCommandInput struct {
	AccountID      string
	SubscriptionID string
	OperationKey   string
}

// SubscriptionCommandResult reports that the provider action was accepted.
// Pending means the final state remains webhook-owned.
type SubscriptionCommandResult struct {
	OK      bool
	Pending bool
}

// SubscriptionCancellationResult captures the per-subscription outcome of a
// bounded account cancellation fan-out.
type SubscriptionCancellationResult struct {
	Provider               string
	ProviderSubscriptionID string
	Canceled               bool
	Error                  string
}

// CancelAllSubscriptionsResult is returned only when every listed current
// subscription cancellation succeeded. Individual results remain useful for
// application diagnostics if an aggregate error is returned.
type CancelAllSubscriptionsResult struct {
	AccountID     string
	CanceledCount int
	Subscriptions []SubscriptionCancellationResult
}

// AccountSubscriptionSummary is a stable, UI-friendly summary derived from
// persisted subscription state and the database-owned plan entitlement.
type AccountSubscriptionSummary struct {
	AccountID          string
	PlanKey            string
	Status             string
	LifecycleState     string
	AccessState        string
	IsCurrent          bool
	IsEntitled         bool
	IsBlockingCheckout bool
	IsCancellable      bool
	IsTerminal         bool
	Subscription       *CommerceSubscription
	PendingChange      *BillingSubscriptionChange
}

// PlanChangeClassification describes the policy branch selected from the
// active catalog's subscription_changes section.
type PlanChangeClassification string

const (
	PlanChangeUnchanged     PlanChangeClassification = "unchanged"
	PlanChangeUpgrade       PlanChangeClassification = "upgrade"
	PlanChangeDowngrade     PlanChangeClassification = "downgrade"
	PlanChangeLateral       PlanChangeClassification = "lateral"
	PlanChangeCadenceChange PlanChangeClassification = "cadence_change"
)

// PlanChangeLineItem is an exact provider financial quote line. Amounts use
// Amount rather than float64 so quotes never lose money precision in transit.
type PlanChangeLineItem struct {
	ProductID       string
	Name            string
	UnitPrice       Amount
	Quantity        int64
	ProrationFactor Amount
	Currency        string
	Tax             Amount
	Subtotal        Amount
}

// PlanChangePreview is the provider quote that is hashed before confirmation.
type PlanChangePreview struct {
	TotalAmount       Amount
	SettlementAmount  Amount
	Currency          string
	LineItems         []PlanChangeLineItem
	EffectiveAt       time.Time
	RecurringAmount   *Amount
	RecurringCurrency string
	NextBillingDate   *time.Time
	TaxAmount         *Amount
	CustomerCredits   *Amount
}

// PreviewPlanChangeInput resolves a subscription offer for an account-owned
// current subscription.
type PreviewPlanChangeInput struct {
	AccountID string
	OfferKey  string
}

// PlanChangePreviewResult is discriminated by Unchanged. A confirmation is
// only valid for a non-unchanged preview with its quote fingerprint.
type PlanChangePreviewResult struct {
	Unchanged        bool
	Classification   PlanChangeClassification
	Scheduled        bool
	PlanKey          string
	Interval         string
	Preview          *PlanChangePreview
	QuoteFingerprint string
}

// ConfirmPlanChangeInput binds an operation key and the quote fingerprint
// returned from PreviewPlanChange.
type ConfirmPlanChangeInput struct {
	AccountID        string
	OfferKey         string
	QuoteFingerprint string
	OperationKey     string
}

// ConfirmPlanChangeResult distinguishes an unchanged selection from a
// accepted provider operation whose final state remains webhook-owned.
type ConfirmPlanChangeResult struct {
	Success     bool
	Unchanged   bool
	Pending     bool
	Scheduled   bool
	EffectiveAt *time.Time
	PlanKey     string
	Interval    string
}

// ProviderPlanChangeRequest is the provider-neutral operation request. It is
// built solely from a persisted subscription and catalog-resolved offer.
type ProviderPlanChangeRequest struct {
	ProviderSubscriptionID string
	ProductID              string
	EffectiveAt            string
	ProrationBillingMode   string
	PaymentFailure         string
	Quantity               int64
	Metadata               map[string]string
	IdempotencyKey         string
}

// ProviderPlanChangeResult supplies a provider operation identifier when a
// scheduled change needs later cancellation.
type ProviderPlanChangeResult struct {
	ProviderOperationID string
}

// PlanChangePreviewProvider, PlanChangeProvider, and
// ScheduledPlanChangeCancellationProvider are optional provider capabilities.
// Commerce returns a typed capability error when a configured provider does
// not implement the relevant operation.
type PlanChangePreviewProvider interface {
	PreviewPlanChange(context.Context, ProviderPlanChangeRequest) (PlanChangePreview, error)
}

type PlanChangeProvider interface {
	ChangePlan(context.Context, ProviderPlanChangeRequest) (ProviderPlanChangeResult, error)
}

type ScheduledPlanChangeCancellationProvider interface {
	CancelScheduledPlanChange(context.Context, string, string, string) error
}

// PortalPurpose selects a hosted billing-management or payment-method route.
type PortalPurpose string

const (
	PortalPurposeBilling       PortalPurpose = "billing"
	PortalPurposePaymentMethod PortalPurpose = "payment_method"
)

// PortalSessionInput is account-scoped before Commerce obtains a provider
// customer ID. The provider receives only the resolved trusted identifiers.
type PortalSessionInput struct {
	AccountID string
	Purpose   PortalPurpose
	ReturnURL string
	CancelURL string
}

// InvoiceDocumentLocator describes one account-owned provider invoice or an
// account-owned ledger entry that references a provider document.
type InvoiceDocumentLocator struct {
	Kind               string
	Provider           string
	ProviderDocumentID string
	LedgerEntryID      string
}

// GetInvoiceLinkInput requires the financial account alongside an opaque
// document ID so Bursar never turns arbitrary provider invoices into URLs.
type GetInvoiceLinkInput struct {
	AccountID string
	Document  InvoiceDocumentLocator
}

// AutoRechargeInput is the account-scoped management request used by
// Commerce.AutoRecharge. ReturnURL is passed only to provider operations that
// require customer action; it is never treated as an authorization boundary.
type AutoRechargeInput struct {
	AccountID string
	ReturnURL string
}

// CommerceSectionAvailability reports whether an ancillary overview section
// was read successfully. Core credit and entitlement failures still fail the
// overview; provider and historical sections degrade independently.
type CommerceSectionAvailability struct {
	PaymentMethods   bool
	Documents        bool
	ProviderInvoices bool
	Transactions     bool
	Usage            bool
	AutoRecharge     bool
}

// AccountAllowanceOverview is the account-facing allowance projection used by
// the commerce overview. The database-owned allowance window may be absent.
type AccountAllowanceOverview struct {
	Remaining   Amount
	Limit       *Amount
	PeriodStart *time.Time
	PeriodEnd   *time.Time
}

// CreditSpendSource identifies one deterministic source in the account's
// allowance/bucket spend order.
type CreditSpendSource struct {
	Type     string
	Key      string
	Label    string
	Priority int
}

// AccountCreditDisplay contains the catalog-defined display conversion. It is
// informational only; all accounting remains in exact credit units.
type AccountCreditDisplay struct {
	Currency      string
	UnitsPerMajor Amount
}

// AccountCreditOverview combines exact balances, allowance state, bucket
// projections, and deterministic spend ordering.
type AccountCreditOverview struct {
	LedgerBalance             Amount
	EffectiveSpendableBalance Amount
	LifetimePurchases         Amount
	Allowance                 AccountAllowanceOverview
	Buckets                   []BucketBalance
	BucketsByKey              map[string]Amount
	SpendOrder                []CreditSpendSource
	Display                   *AccountCreditDisplay
}

// BillingDocumentRef is a customer-safe provider-invoice or ledger-document
// projection. It contains no provider credentials or raw payment payloads.
type BillingDocumentRef struct {
	Kind               string
	Provider           string
	ProviderDocumentID string
	LedgerEntryID      string
	Status             string
	AmountPaidMinor    int64
	AmountDueMinor     int64
	Currency           string
	PeriodStart        *time.Time
	PeriodEnd          *time.Time
	CreatedAt          time.Time
	EntryType          string
	Amount             Amount
}

// AccountCommerceOverview combines durable financial and entitlement
// projections. Ancillary provider/history sections remain usable when one
// optional read is unavailable, matching the Python facade behavior.
type AccountCommerceOverview struct {
	AccountID           string
	Balance             BalanceResult
	Available           AvailableResult
	Buckets             BucketBalancesResult
	Credits             AccountCreditOverview
	Entitlement         GetUserPlanResult
	Allowance           *AllowanceResult
	SubscriptionSummary AccountSubscriptionSummary
	Subscription        *CommerceSubscription
	PendingChange       *BillingSubscriptionChange
	Preferences         BillingPreferences
	Invoices            []BillingInvoice
	Transactions        LedgerPage
	Usage               UsageChargePage
	PaymentMethods      []PaymentMethodInfo
	Documents           []BillingDocumentRef
	ProviderInvoices    []BillingInvoice
	AutoRecharge        *AutoRechargeStatus
	Availability        CommerceSectionAvailability
}
