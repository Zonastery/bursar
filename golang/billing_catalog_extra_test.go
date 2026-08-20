package bursar

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"
)

func TestBillingEventValidationRejectsMalformedFinancialPayloads(t *testing.T) {
	cases := []struct {
		name   string
		mutate func(*BillingEvent)
	}{
		{"missing event id", func(e *BillingEvent) { e.ID = "" }},
		{"missing provider", func(e *BillingEvent) { e.Provider = "" }},
		{"unsupported type", func(e *BillingEvent) { e.Type = "unsupported.event" }},
		{"missing occurred at", func(e *BillingEvent) { e.OccurredAt = time.Time{} }},
		{"missing subscription", func(e *BillingEvent) { e.Subscription = nil }},
		{"missing customer", func(e *BillingEvent) { e.Customer = nil }},
		{"bad subscription status", func(e *BillingEvent) { e.Subscription.Status = "bad" }},
		{"bad subscription interval", func(e *BillingEvent) { e.Subscription.Interval = "quarter" }},
		{"bad subscription count", func(e *BillingEvent) { e.Subscription.Interval = "month"; e.Subscription.IntervalCount = 0 }},
		{"missing invoice id", func(e *BillingEvent) { e.Invoice.ProviderInvoiceID = "" }},
		{"bad invoice amount", func(e *BillingEvent) { e.Invoice.AmountDueMinor = -1 }},
		{"bad invoice currency", func(e *BillingEvent) { e.Invoice.Currency = "usd" }},
		{"missing payment id", func(e *BillingEvent) { e.Payment.ProviderPaymentID = "" }},
		{"bad payment purpose", func(e *BillingEvent) { e.Payment.Purpose = "other" }},
		{"bad payment status", func(e *BillingEvent) { e.Payment.Status = "other" }},
		{"missing refund ids", func(e *BillingEvent) { e.Refund.ProviderRefundID = "" }},
		{"bad refund amount", func(e *BillingEvent) { e.Refund.AmountMinor = 0 }},
		{"missing dispute ids", func(e *BillingEvent) { e.Dispute.ProviderDisputeID = "" }},
		{"bad dispute status", func(e *BillingEvent) { e.Dispute.Status = "other" }},
	}
	validCustomer := lifecycleEvent(BillingEventCustomerCreated)
	validCustomer.Customer.Email = "customer@example.test"
	if err := validCustomer.Validate(); err != nil {
		t.Fatalf("customer email event rejected: %v", err)
	}
	validSubscription := lifecycleEvent(BillingEventSubscriptionUpdated)
	validSubscription.Subscription.Interval = "month"
	validSubscription.Subscription.IntervalCount = 1
	validSubscription.Subscription.Refs = &ProviderRef{LookupKey: "starter"}
	if err := validSubscription.Validate(); err != nil {
		t.Fatalf("subscription event rejected: %v", err)
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var event BillingEvent
			switch {
			case strings.Contains(tc.name, "customer"):
				event = lifecycleEvent(BillingEventCustomerCreated)
			case strings.Contains(tc.name, "subscription"):
				event = lifecycleEvent(BillingEventSubscriptionUpdated)
			case strings.Contains(tc.name, "invoice"):
				event = lifecycleEvent(BillingEventInvoicePaid)
			case strings.Contains(tc.name, "payment"):
				event = lifecycleEvent(BillingEventPaymentSucceeded)
			case strings.Contains(tc.name, "refund"):
				event = lifecycleEvent(BillingEventRefundCreated)
			case strings.Contains(tc.name, "dispute"):
				event = lifecycleEvent(BillingEventDisputeCreated)
			default:
				event = lifecycleEvent(BillingEventSubscriptionUpdated)
			}
			tc.mutate(&event)
			if err := event.Validate(); err == nil {
				t.Fatal("malformed event was accepted")
			}
		})
	}
	if err := validateProviderRef(&ProviderRef{}); err == nil {
		t.Fatal("empty provider reference accepted")
	}
	if err := validateBillingInstants(&time.Time{}); err == nil {
		t.Fatal("zero instant accepted")
	}
	if billingCurrency("USD") != true || billingCurrency("usd") || billingCurrency("US") {
		t.Fatal("currency validation mismatch")
	}
}

func TestBillingIngestClaimAndFailureOutcomes(t *testing.T) {
	base := lifecycleEvent(BillingEventPaymentSucceeded)
	for _, tc := range []struct {
		name     string
		claim    BillingEventClaim
		claimErr error
		want     func(BillingEventResult, error) bool
	}{
		{"duplicate", BillingEventClaim{State: BillingEventDuplicate}, nil, func(r BillingEventResult, err error) bool { return err == nil && r.Duplicate }},
		{"busy", BillingEventClaim{State: BillingEventBusy}, nil, func(r BillingEventResult, err error) bool { return err == nil && r.Retryable }},
		{"rejected", BillingEventClaim{State: BillingEventRejected, Reason: "bad signature"}, nil, func(_ BillingEventResult, err error) bool {
			return err != nil && strings.Contains(err.Error(), "rejected")
		}},
		{"unknown", BillingEventClaim{State: "unknown"}, nil, func(_ BillingEventResult, err error) bool { return err != nil }},
		{"claim error", BillingEventClaim{}, errors.New("database unavailable"), func(_ BillingEventResult, err error) bool {
			return errors.Is(err, errors.New("database unavailable")) || strings.Contains(err.Error(), "database unavailable")
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			store := &billingOutcomeStore{claim: tc.claim, claimErr: tc.claimErr}
			service, err := NewBillingService(store)
			if err != nil {
				t.Fatal(err)
			}
			result, ingestErr := service.Ingest(context.Background(), base)
			if !tc.want(result, ingestErr) {
				t.Fatalf("result=%#v err=%v", result, ingestErr)
			}
		})
	}
	store := &billingOutcomeStore{claim: BillingEventClaim{State: BillingEventClaimed}}
	service, _ := NewBillingService(store)
	if _, err := service.Ingest(context.Background(), base); err == nil {
		t.Fatal("claimed event without token accepted")
	}
	store = &billingOutcomeStore{claim: BillingEventClaim{State: BillingEventClaimed, ClaimToken: "token"}, complete: false}
	service, _ = NewBillingService(store)
	_ = service.On(BillingEventPaymentSucceeded, func(context.Context, BillingEvent, string) error { return nil })
	if _, err := service.Ingest(context.Background(), base); err == nil || !strings.Contains(err.Error(), "completion claim was lost") {
		t.Fatalf("completion loss error = %v", err)
	}
	store = &billingOutcomeStore{claim: BillingEventClaim{State: BillingEventClaimed, ClaimToken: "token"}, fail: false, failSet: true}
	service, _ = NewBillingService(store)
	_ = service.On(BillingEventPaymentSucceeded, func(context.Context, BillingEvent, string) error { return errors.New("handler") })
	if _, err := service.Ingest(context.Background(), base); err == nil || !strings.Contains(err.Error(), "failure claim was lost") {
		t.Fatalf("failure claim loss error = %v", err)
	}
	store = &billingOutcomeStore{claim: BillingEventClaim{State: BillingEventClaimed, ClaimToken: "token"}, failErr: errors.New("fail write")}
	service, _ = NewBillingService(store)
	_ = service.On(BillingEventPaymentSucceeded, func(context.Context, BillingEvent, string) error { return errors.New("handler") })
	if _, err := service.Ingest(context.Background(), base); err == nil || !strings.Contains(err.Error(), "fail billing event") {
		t.Fatalf("fail persistence error = %v", err)
	}
	store = &billingOutcomeStore{claim: BillingEventClaim{State: BillingEventClaimed, ClaimToken: "token"}, completeErr: errors.New("complete write")}
	service, _ = NewBillingService(store)
	_ = service.On(BillingEventPaymentSucceeded, func(context.Context, BillingEvent, string) error { return nil })
	if _, err := service.Ingest(context.Background(), base); err == nil || !strings.Contains(err.Error(), "complete billing event") {
		t.Fatalf("complete persistence error = %v", err)
	}
	store = &billingOutcomeStore{claim: BillingEventClaim{State: BillingEventClaimed, ClaimToken: "token"}, resolveErr: errors.New("resolve account")}
	service, _ = NewBillingService(store)
	if _, err := service.Ingest(context.Background(), base); err == nil || !strings.Contains(err.Error(), "resolve account") {
		t.Fatalf("account resolver error = %v", err)
	}
	unhandled := &billingUnhandledStore{billingOutcomeStore: &billingOutcomeStore{claim: BillingEventClaim{State: BillingEventClaimed, ClaimToken: "token"}}}
	service, _ = NewBillingService(unhandled)
	if _, err := service.Ingest(context.Background(), lifecycleEvent(BillingEventSubscriptionCreated)); err == nil || !strings.Contains(err.Error(), "did not handle") {
		t.Fatalf("unhandled lifecycle error = %v", err)
	}
	failed := errors.New("handler failed")
	store = &billingOutcomeStore{claim: BillingEventClaim{State: BillingEventClaimed, ClaimToken: "token"}}
	service, _ = NewBillingService(store)
	_ = service.On(BillingEventPaymentSucceeded, func(context.Context, BillingEvent, string) error { return failed })
	if _, err := service.Ingest(context.Background(), base); !errors.Is(err, failed) || store.failed != 1 {
		t.Fatalf("handler failure = %v failed=%d", err, store.failed)
	}
}

func TestBillingRegistrationAndCanonicalFallbacks(t *testing.T) {
	store := &billingOutcomeStore{}
	if _, err := NewBillingService(nil); err == nil {
		t.Fatal("nil billing store accepted")
	}
	service, err := NewBillingService(store)
	if err != nil {
		t.Fatal(err)
	}
	if service.On("", nil) == nil || service.On(BillingEventPaymentSucceeded, nil) == nil {
		t.Fatal("invalid handler registration accepted")
	}
	if service.OnEvent("", nil) == nil || service.OnEvent(BillingEventPaymentSucceeded, nil) == nil {
		t.Fatal("invalid callback registration accepted")
	}
	if err := service.On(BillingEventPaymentSucceeded, func(context.Context, BillingEvent, string) error { return nil }); err != nil {
		t.Fatal(err)
	}
	if err := service.OnEvent(BillingEventPaymentSucceeded, func(context.Context, BillingEvent, string) {}); err != nil {
		t.Fatal(err)
	}
	service.SetDefaultHandler(nil)
	if _, err := NewBillingService(store, BillingServiceOptions{}, BillingServiceOptions{}); err == nil {
		t.Fatal("multiple billing options accepted")
	}
	grace := -time.Second
	if _, err := NewBillingService(store, BillingServiceOptions{PastDueGracePeriod: &grace}); err == nil {
		t.Fatal("negative grace period accepted")
	}
	if (BillingEvent{ID: "id"}).canonicalEventID() != "id" {
		t.Fatal("event ID fallback failed")
	}
	if (BillingCustomer{ID: "customer"}).canonicalProviderCustomerID() != "customer" {
		t.Fatal("customer ID fallback failed")
	}
	if (BillingSubscription{ID: "subscription"}).canonicalProviderSubscriptionID() != "subscription" {
		t.Fatal("subscription ID fallback failed")
	}
	if (BillingRefund{PaymentID: "payment"}).canonicalProviderPaymentID() != "payment" {
		t.Fatal("refund payment fallback failed")
	}
	if (BillingDispute{PaymentID: "payment"}).canonicalProviderPaymentID() != "payment" {
		t.Fatal("dispute payment fallback failed")
	}
	claims := billingEventClaimEnvelope(lifecycleEvent(BillingEventPaymentSucceeded))
	if billingMetadataString(nil, "x") != "" || claims["eventId"] != "event-1" {
		t.Fatalf("claim helpers = %#v", claims)
	}
	target := map[string]any{}
	setBillingEnvelopeTime(target, "time", nil)
	if len(target) != 0 {
		t.Fatalf("nil time added to envelope: %#v", target)
	}
}

type billingOutcomeStore struct {
	environment ProviderEnvironment
	claim       BillingEventClaim
	claimErr    error
	complete    bool
	fail        bool
	failSet     bool
	failed      int
	completeErr error
	failErr     error
	resolveErr  error
}

func (s *billingOutcomeStore) ProviderEnvironment() ProviderEnvironment {
	if s.environment == "" {
		return ProviderEnvironmentTest
	}
	return s.environment
}
func (s *billingOutcomeStore) ClaimBillingEvent(context.Context, BillingEvent, map[string]any) (BillingEventClaim, error) {
	return s.claim, s.claimErr
}

func (s *billingOutcomeStore) CompleteBillingEvent(context.Context, string, string, string) (bool, error) {
	if s.completeErr != nil {
		return false, s.completeErr
	}
	return s.complete, nil
}
func (s *billingOutcomeStore) FailBillingEvent(context.Context, string, string, string, string) (bool, error) {
	s.failed++
	if s.failErr != nil {
		return false, s.failErr
	}
	if !s.failSet {
		return true, nil
	}
	return s.fail, nil
}
func (s *billingOutcomeStore) ResolveBillingEventAccount(context.Context, BillingEvent) (string, error) {
	return "account-1", s.resolveErr
}

type billingUnhandledStore struct{ *billingOutcomeStore }

func (s *billingUnhandledStore) ProcessBillingEvent(context.Context, BillingEvent, string) (BillingEventResult, error) {
	return BillingEventResult{}, nil
}

func TestCatalogServiceLoadsRefreshesAndProjectsVersionedCatalog(t *testing.T) {
	config := checkoutTestConfig(t)
	store := &catalogWaveStore{active: &CatalogRevision{ID: "catalog-1", Version: 1, Config: config}}
	now := time.Date(2026, time.August, 19, 12, 0, 0, 0, time.UTC)
	service, err := NewCatalogServiceWithOptions(store, CatalogServiceOptions{CacheTTL: time.Hour})
	if err != nil {
		t.Fatal(err)
	}
	service.now = func() time.Time { return now }
	if service.IsLoaded() {
		t.Fatal("new catalog reported loaded")
	}
	if err := service.Load(context.Background()); err != nil {
		t.Fatal(err)
	}
	if !service.IsLoaded() {
		t.Fatal("loaded catalog reported unloaded")
	}
	if _, err := service.Engine(); err != nil {
		t.Fatal(err)
	}
	if err := service.RefreshIfStale(context.Background()); err != nil || store.activeReads != 1 {
		t.Fatalf("fresh refresh = %v reads=%d", err, store.activeReads)
	}
	service.Invalidate()
	store.active = &CatalogRevision{ID: "catalog-2", Version: 2, Config: config}
	if err := service.Refresh(context.Background()); err != nil {
		t.Fatal(err)
	}
	if store.activeReads != 2 {
		t.Fatalf("stale refresh reads=%d, want 2", store.activeReads)
	}
	if got, err := service.GetConfig(context.Background()); err != nil || got == nil {
		t.Fatalf("GetConfig = %#v, %v", got, err)
	}
	view, err := service.PublicView(context.Background())
	if err != nil || len(view.Plans) == 0 {
		t.Fatalf("PublicView = %#v, %v", view, err)
	}
	service.Invalidate()
	if service.IsLoaded() == false {
		t.Fatal("Invalidate discarded usable engine")
	}
	if _, err := (&CatalogService{}).Engine(); err == nil {
		t.Fatal("unloaded engine accepted")
	}
	if _, err := service.engineForVersion(context.Background(), nil); err != nil {
		t.Fatal(err)
	}
	if _, err := service.engineForVersion(context.Background(), intPointerForCatalog(99)); err == nil {
		t.Fatal("missing pinned revision accepted")
	}
	_, _ = service.CalculateForUser(context.Background(), "user-1", UsageMetrics{})
	_, _ = service.CalculateForLease(context.Background(), "user-1", "lease-1", UsageMetrics{})
	if got, err := service.PublishDraft(context.Background(), config, "draft"); err != nil || got != "draft-id" {
		t.Fatalf("PublishDraft = %q, %v", got, err)
	}
	if got, err := service.Activate(context.Background(), 2, CatalogRollout{}); err != nil || got != "activate-id" {
		t.Fatalf("Activate = %q, %v", got, err)
	}
	if got, err := service.PublishAndActivate(context.Background(), config, "active", CatalogRollout{}); err != nil || got != "publish-active-id" {
		t.Fatalf("PublishAndActivate = %q, %v", got, err)
	}
	if pinned, err := service.SetRevisionPin(context.Background(), "user-1", true); err != nil || !pinned {
		t.Fatalf("SetRevisionPin = %v, %v", pinned, err)
	}
	if count, err := service.ApplyDueChanges(context.Background(), 10); err != nil || count != 2 {
		t.Fatalf("ApplyDueChanges = %d, %v", count, err)
	}
	if _, err := service.ApplyDueChanges(context.Background(), -1); err == nil {
		t.Fatal("negative maintenance limit accepted")
	}
}

func TestCatalogServiceRejectsInvalidPortsAndBounds(t *testing.T) {
	if _, err := NewCatalogService(nil); err == nil {
		t.Fatal("nil catalog store accepted")
	}
	if _, err := NewCatalogServiceWithOptions(&catalogWaveStore{}, CatalogServiceOptions{CacheTTL: -time.Second}); err == nil {
		t.Fatal("negative cache TTL accepted")
	}
	var service *CatalogService
	if service.IsLoaded() {
		t.Fatal("nil catalog reported loaded")
	}
	if err := service.Refresh(context.Background()); err == nil {
		t.Fatal("nil catalog refreshed")
	}
	if err := (&CatalogService{store: &catalogWaveStore{}}).Load(context.Background()); err == nil {
		t.Fatal("missing active revision accepted")
	}
	empty := &CatalogService{}
	if _, err := empty.GetActive(context.Background()); err == nil {
		t.Fatal("empty GetActive accepted")
	}
	if _, err := empty.GetConfig(context.Background()); err == nil {
		t.Fatal("empty GetConfig accepted")
	}
	if _, err := empty.PublicView(context.Background()); err == nil {
		t.Fatal("empty PublicView accepted")
	}
	if _, err := empty.CalculateForUser(context.Background(), "u", UsageMetrics{}); err == nil {
		t.Fatal("empty CalculateForUser accepted")
	}
	if _, err := empty.CalculateForLease(context.Background(), "u", "lease", UsageMetrics{}); err == nil {
		t.Fatal("empty CalculateForLease accepted")
	}
	if _, err := empty.PublishDraft(context.Background(), nil, "draft"); err == nil {
		t.Fatal("empty PublishDraft accepted")
	}
	if _, err := empty.Activate(context.Background(), 1, CatalogRollout{}); err == nil {
		t.Fatal("empty Activate accepted")
	}
	if _, err := empty.PublishAndActivate(context.Background(), nil, "active", CatalogRollout{}); err == nil {
		t.Fatal("empty PublishAndActivate accepted")
	}
	if _, err := empty.SetRevisionPin(context.Background(), "u", true); err == nil {
		t.Fatal("empty SetRevisionPin accepted")
	}
	if _, err := empty.ApplyDueChanges(context.Background(), 1); err == nil {
		t.Fatal("empty ApplyDueChanges accepted")
	}
}

type catalogWaveStore struct {
	CreditStore
	active      *CatalogRevision
	activeReads int
	revisions   map[int]*CatalogRevision
}

func intPointerForCatalog(value int) *int { return &value }

func (s *catalogWaveStore) GetActiveCatalog(context.Context) (*CatalogRevision, error) {
	s.activeReads++
	return s.active, nil
}
func (s *catalogWaveStore) GetCatalogRevision(_ context.Context, version int) (*CatalogRevision, error) {
	if s.revisions == nil {
		return nil, nil
	}
	return s.revisions[version], nil
}

func (s *catalogWaveStore) GetUserPlan(context.Context, string) (GetUserPlanResult, error) {
	return GetUserPlanResult{}, nil
}
func (s *catalogWaveStore) GetLeasePricingContext(context.Context, string, string) (*LeasePricingContext, error) {
	version := 1
	return &LeasePricingContext{CatalogVersion: version}, nil
}

func (s *catalogWaveStore) PublishCatalogDraft(context.Context, map[string]any, string) (string, error) {
	return "draft-id", nil
}
func (s *catalogWaveStore) ActivateCatalogRevision(context.Context, int, CatalogRollout) (string, error) {
	return "activate-id", nil
}
func (s *catalogWaveStore) PublishAndActivateCatalog(context.Context, map[string]any, string, CatalogRollout) (string, error) {
	return "publish-active-id", nil
}
func (s *catalogWaveStore) SetPlanRevisionPin(context.Context, string, bool) (bool, error) {
	return true, nil
}
func (s *catalogWaveStore) ApplyDuePlanChanges(context.Context, int) (int, error) { return 2, nil }

func TestProjectPublicCatalogWindowAndCloneBoundaries(t *testing.T) {
	for _, tc := range []struct {
		name     string
		window   Window
		unit     string
		count    int
		timezone bool
	}{
		{"calendar", Window{Type: "calendar", Unit: "month", Count: 2, Timezone: "Asia/Kolkata"}, "month", 2, true},
		{"plan assignment", Window{Type: "plan_assignment", Interval: &BillingInterval{Unit: "year", Count: 1}, Timezone: "UTC"}, "year", 1, true},
		{"rolling", Window{Type: "rolling", Duration: &Duration{Unit: "day", Count: 3}}, "day", 3, false},
		{"empty", Window{Type: "other"}, "", 0, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got := projectPublicWindow(tc.window)
			if got.Unit != tc.unit || got.Count != tc.count || (got.Timezone != nil) != tc.timezone {
				t.Fatalf("window = %#v", got)
			}
		})
	}
	if got := ProjectPublicCatalog(nil); got.Version != 1 || got.Plans == nil || got.Topups == nil {
		t.Fatalf("nil projection = %#v", got)
	}
	text := "description"
	offer := projectPublicOffer("sub", CommerceOffer{Type: "subscription", DisplayName: "Sub", Description: &text, BillingInterval: &BillingInterval{Unit: "month"}, Providers: map[string]ProviderReference{"secret": {}}})
	if offer.BillingInterval == nil || offer.Description == nil {
		t.Fatalf("subscription projection = %#v", offer)
	}
	text = "changed"
	if *offer.Description != "description" {
		t.Fatal("description pointer was not cloned")
	}
	copy := cloneStringPointer(nil)
	if copy != nil {
		t.Fatal("nil string pointer cloned")
	}
}
