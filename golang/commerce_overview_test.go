package bursar

import (
	"context"
	"errors"
	"testing"
	"time"
)

type overviewStoreStub struct {
	CreditStore
	CommerceStore
	CommerceStateStore
	config                 map[string]any
	balance                BalanceResult
	available              AvailableResult
	buckets                BucketBalancesResult
	plan                   GetUserPlanResult
	allowance              *AllowanceResult
	ledger                 LedgerPage
	usage                  UsageChargePage
	ledgerErr              error
	usageErr               error
	invoicesErr            error
	usageIncludeRecordOnly *bool
	preferences            *BillingPreferences
	subscription           *CommerceSubscription
	customer               *BillingCustomerRecord
	customerProvider       string
	invoices               []BillingInvoice
}

func (s *overviewStoreStub) GetActiveCatalog(context.Context) (*CatalogRevision, error) {
	return &CatalogRevision{ID: "catalog-overview", Version: 1, Config: s.config}, nil
}

func (s *overviewStoreStub) GetBalance(context.Context, string) (BalanceResult, error) {
	return s.balance, nil
}

func (s *overviewStoreStub) GetAvailable(context.Context, string) (AvailableResult, error) {
	return s.available, nil
}

func (s *overviewStoreStub) GetBucketBalances(context.Context, string) (BucketBalancesResult, error) {
	return s.buckets, nil
}

func (s *overviewStoreStub) GetUserPlan(context.Context, string) (GetUserPlanResult, error) {
	return s.plan, nil
}

func (s *overviewStoreStub) CheckAllowance(context.Context, string) (*AllowanceResult, error) {
	return s.allowance, nil
}

func (s *overviewStoreStub) ListLedgerEntries(context.Context, string, ListLedgerEntriesOptions) (LedgerPage, error) {
	if s.ledgerErr != nil {
		return LedgerPage{}, s.ledgerErr
	}
	return s.ledger, nil
}

func (s *overviewStoreStub) ListUsageCharges(_ context.Context, _ string, options ListUsageChargesOptions) (UsageChargePage, error) {
	if options.IncludeRecordOnly != nil {
		value := *options.IncludeRecordOnly
		s.usageIncludeRecordOnly = &value
	}
	if s.usageErr != nil {
		return UsageChargePage{}, s.usageErr
	}
	return s.usage, nil
}

func (s *overviewStoreStub) GetBillingCustomer(_ context.Context, _ string, provider string) (*BillingCustomerRecord, error) {
	s.customerProvider = provider
	return s.customer, nil
}

func (s *overviewStoreStub) GetBillingSubscription(context.Context, string, []string) (*CommerceSubscription, error) {
	return s.subscription, nil
}

func (s *overviewStoreStub) ListBillingSubscriptions(context.Context, string) ([]CommerceSubscription, error) {
	return nil, nil
}

func (s *overviewStoreStub) GetOpenBillingSubscriptionChange(context.Context, string, string) (*BillingSubscriptionChange, error) {
	return nil, nil
}

func (s *overviewStoreStub) GetBillingPreferences(context.Context, string) (*BillingPreferences, error) {
	return s.preferences, nil
}

func (s *overviewStoreStub) UpsertBillingPreferences(context.Context, BillingPreferences) error {
	return nil
}

func (s *overviewStoreStub) ListBillingInvoices(context.Context, string) ([]BillingInvoice, error) {
	if s.invoicesErr != nil {
		return nil, s.invoicesErr
	}
	return s.invoices, nil
}

type overviewProviderStub struct {
	name       string
	methods    []PaymentMethodInfo
	methodsErr error
}

func (p *overviewProviderStub) Name() string { return p.name }

func (*overviewProviderStub) CreateCheckoutSession(context.Context, CheckoutSessionRequest) (CheckoutSession, error) {
	return CheckoutSession{}, errors.New("not used")
}

func (p *overviewProviderStub) HandleWebhook(context.Context, WebhookRequest) (WebhookResult, error) {
	return WebhookResult{}, errors.New("not used")
}

func (p *overviewProviderStub) ListPaymentMethods(context.Context, string) ([]PaymentMethodInfo, error) {
	return p.methods, p.methodsErr
}

func newOverviewService(t *testing.T, store *overviewStoreStub, provider *overviewProviderStub) *CommerceService {
	t.Helper()
	registry, err := NewProviderRegistry(ProviderFactoryContext{ProviderEnvironment: ProviderEnvironmentTest}, map[string]ProviderFactory{
		provider.name: func(context.Context, ProviderFactoryContext) (PaymentProvider, error) { return provider, nil },
	})
	if err != nil {
		t.Fatal(err)
	}
	service, err := NewCommerceService(
		&BillingService{store: checkoutBillingStoreStub{}},
		&CatalogService{store: store},
		&CreditsService{store: store, usageStore: store, analytics: store},
		CommerceOptions{Store: store, StateStore: store, Providers: registry},
	)
	if err != nil {
		t.Fatal(err)
	}
	return service
}

func TestGetAccountOverviewAggregatesCreditAndProviderSections(t *testing.T) {
	periodStart := time.Now().UTC().Add(-time.Hour)
	periodEnd := time.Now().UTC().Add(time.Hour)
	config := checkoutTestConfig(t)
	config["credits"].(map[string]any)["display"] = map[string]any{"currency": "USD", "units_per_major": "100"}
	store := &overviewStoreStub{
		config:    config,
		balance:   BalanceResult{UserID: "account-1", Balance: MustAmount("10"), LifetimePurchased: MustAmount("42")},
		available: AvailableResult{UserID: "account-1", Available: MustAmount("7")},
		buckets: BucketBalancesResult{UserID: "account-1", Buckets: []BucketBalance{
			{BucketKey: "general", Label: "General", Priority: 4, Balance: MustAmount("7")},
		}},
		plan:         GetUserPlanResult{UserID: "account-1", Allowance: &PlanAllowancePolicy{Amount: MustAmount("3"), Priority: 1}, CreditPolicy: &PlanCreditPolicy{Type: "prepaid"}},
		allowance:    &AllowanceResult{AllowanceRemaining: MustAmount("2"), PeriodStart: periodStart, PeriodEnd: periodEnd},
		preferences:  &BillingPreferences{AccountID: "account-1", OverageProtection: true},
		subscription: &CommerceSubscription{Provider: "stripe", ProviderSubscriptionID: "sub-1", Status: "active", PlanKey: "starter"},
		customer:     &BillingCustomerRecord{Provider: "stripe", ProviderCustomerID: "cus-1", AccountID: "account-1"},
		ledger: LedgerPage{Items: []LedgerEntry{
			{EntryID: "usage-ledger", EntryType: "usage", Amount: MustAmount("1")},
			{EntryID: "invoice-ledger", EntryType: "purchase", Amount: MustAmount("5"), CreatedAt: periodStart, Metadata: CreditMetadata{"provider": "stripe", "provider_invoice_id": "inv-1"}},
		}},
		usage:    UsageChargePage{Items: []UsageCharge{{UsageID: "usage-1", BillingDisposition: "charged"}}},
		invoices: []BillingInvoice{{Provider: "stripe", ProviderInvoiceID: "inv-1", Status: "paid", Currency: "USD"}},
	}
	provider := &overviewProviderStub{name: "stripe", methods: []PaymentMethodInfo{{ID: "pm-1", Brand: "visa", Last4: "4242", IsDefault: true}}}
	overview := newOverviewService(t, store, provider)

	result, err := overview.GetAccountOverview(context.Background(), "account-1")
	if err != nil {
		t.Fatal(err)
	}
	if !result.Credits.LedgerBalance.Equal(MustAmount("10")) || !result.Credits.EffectiveSpendableBalance.Equal(MustAmount("9")) {
		t.Fatalf("credit overview = %+v", result.Credits)
	}
	if result.Credits.Display == nil || result.Credits.Display.Currency != "USD" {
		t.Fatalf("credit display = %+v", result.Credits.Display)
	}
	if len(result.Credits.SpendOrder) != 2 || result.Credits.SpendOrder[0].Type != "allowance" {
		t.Fatalf("spend order = %+v", result.Credits.SpendOrder)
	}
	if len(result.Transactions.Items) != 1 || result.Transactions.Items[0].EntryID != "invoice-ledger" {
		t.Fatalf("transactions = %+v", result.Transactions.Items)
	}
	if store.usageIncludeRecordOnly == nil || *store.usageIncludeRecordOnly {
		t.Fatalf("include_record_only = %v, want false", store.usageIncludeRecordOnly)
	}
	if len(result.PaymentMethods) != 1 || result.PaymentMethods[0].ID != "pm-1" {
		t.Fatalf("payment methods = %+v", result.PaymentMethods)
	}
	if store.customerProvider != "stripe" {
		t.Fatalf("customer provider = %q, want persisted subscription provider", store.customerProvider)
	}
	if len(result.Documents) != 2 || len(result.ProviderInvoices) != 1 {
		t.Fatalf("documents = %+v, provider invoices = %+v", result.Documents, result.ProviderInvoices)
	}
	if !result.Availability.PaymentMethods || !result.Availability.Documents || !result.Availability.Transactions || !result.Availability.Usage {
		t.Fatalf("availability = %+v", result.Availability)
	}
}

func TestGetAccountOverviewDegradesAncillaryFailures(t *testing.T) {
	config := checkoutTestConfig(t)
	store := &overviewStoreStub{
		config:      config,
		balance:     BalanceResult{UserID: "account-1", Balance: MustAmount("1")},
		available:   AvailableResult{UserID: "account-1", Available: MustAmount("1")},
		buckets:     BucketBalancesResult{UserID: "account-1"},
		plan:        GetUserPlanResult{UserID: "account-1"},
		ledgerErr:   errors.New("ledger unavailable"),
		usageErr:    errors.New("usage unavailable"),
		invoicesErr: errors.New("invoices unavailable"),
		customer:    &BillingCustomerRecord{Provider: "stripe", ProviderCustomerID: "cus-1", AccountID: "account-1"},
	}
	provider := &overviewProviderStub{name: "stripe", methodsErr: errors.New("provider unavailable")}
	overview := newOverviewService(t, store, provider)

	result, err := overview.GetAccountOverview(context.Background(), "account-1")
	if err != nil {
		t.Fatal(err)
	}
	if result.Availability.PaymentMethods || result.Availability.Documents || result.Availability.ProviderInvoices || result.Availability.Transactions || result.Availability.Usage || result.Availability.AutoRecharge {
		t.Fatalf("availability = %+v, want all ancillary sections degraded", result.Availability)
	}
	if !result.Credits.LedgerBalance.Equal(MustAmount("1")) {
		t.Fatalf("core credit state lost during ancillary failure: %+v", result.Credits)
	}
}
