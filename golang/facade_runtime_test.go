package bursar

import (
	"context"
	"errors"
	"testing"
	"time"
)

// facadeStoreStub embeds the broad production contract but implements only the
// read/lifecycle calls exercised by facade and runtime tests.
type facadeStoreStub struct {
	CreditStore
	environment ProviderEnvironment
	active      *CatalogRevision
	closed      bool
}

func (s *facadeStoreStub) ProviderEnvironment() ProviderEnvironment { return s.environment }

func (s *facadeStoreStub) GetActiveCatalog(context.Context) (*CatalogRevision, error) {
	return s.active, nil
}

func (s *facadeStoreStub) Close() error {
	s.closed = true
	return nil
}

type facadeBillingStoreStub struct {
	environment ProviderEnvironment
	completed   bool
}

func (s *facadeBillingStoreStub) ProviderEnvironment() ProviderEnvironment { return s.environment }

func (*facadeBillingStoreStub) ClaimBillingEvent(context.Context, BillingEvent, map[string]any) (BillingEventClaim, error) {
	return BillingEventClaim{State: BillingEventClaimed, ClaimToken: "claim-1"}, nil
}

func (s *facadeBillingStoreStub) CompleteBillingEvent(context.Context, string, string, string) (bool, error) {
	s.completed = true
	return true, nil
}

func (*facadeBillingStoreStub) FailBillingEvent(context.Context, string, string, string, string) (bool, error) {
	return true, nil
}

type runtimeComponentStub struct {
	started bool
	flushed bool
	closed  bool
}

func (s *runtimeComponentStub) Start(context.Context) error { s.started = true; return nil }
func (s *runtimeComponentStub) Flush(context.Context) error { s.flushed = true; return nil }
func (s *runtimeComponentStub) Close(context.Context) error { s.closed = true; return nil }
func (*runtimeComponentStub) Health(context.Context) error  { return nil }

func TestBursarFacadeRejectsMismatchedProviderEnvironments(t *testing.T) {
	creditStore := &facadeStoreStub{environment: ProviderEnvironmentTest}
	billingStore := &facadeBillingStoreStub{environment: ProviderEnvironmentLive}
	_, err := New(Options{CreditStore: creditStore, BillingStore: billingStore})
	if err == nil {
		t.Fatal("New() accepted mismatched provider environments")
	}
	classified, ok := AsBursarError(err)
	if !ok || classified.Code != ErrorCodeConfig {
		t.Fatalf("New() error = %#v, want CONFIG_ERROR", err)
	}
}

func TestBursarFacadeOwnsBillingIngestion(t *testing.T) {
	creditStore := &facadeStoreStub{environment: ProviderEnvironmentTest}
	billingStore := &facadeBillingStoreStub{environment: ProviderEnvironmentTest}
	sdk, err := New(Options{
		CreditStore:  creditStore,
		BillingStore: billingStore,
		BillingOptions: &BillingServiceOptions{DefaultHandler: func(context.Context, BillingEvent, string) error {
			return nil
		}},
	})
	if err != nil {
		t.Fatalf("New() error = %v", err)
	}
	if !sdk.Billing.HasProvisioning() {
		t.Fatal("facade billing did not receive the shared CreditsService provisioning port")
	}
	result, err := sdk.IngestBillingEvent(context.Background(), BillingEvent{
		ID:         "event-1",
		Provider:   "stripe",
		Type:       BillingEventPaymentSucceeded,
		OccurredAt: time.Now().UTC(),
		Payment:    &BillingPayment{ProviderPaymentID: "pay-1", Provider: "stripe", Purpose: "subscription", Status: "succeeded", Currency: "USD"},
	})
	if err != nil {
		t.Fatalf("IngestBillingEvent() error = %v", err)
	}
	if !result.Handled || !billingStore.completed {
		t.Fatalf("billing result = %#v, completed = %v; want completed event", result, billingStore.completed)
	}
}

func TestRuntimeLoadsCatalogAndClosesInOrder(t *testing.T) {
	creditStore := &facadeStoreStub{
		environment: ProviderEnvironmentTest,
		active: &CatalogRevision{ID: "catalog-1", Version: 1, Config: map[string]any{
			"version": 1,
			"credits": map[string]any{},
		}},
	}
	sdk, err := New(Options{CreditStore: creditStore})
	if err != nil {
		t.Fatalf("New() error = %v", err)
	}
	component := &runtimeComponentStub{}
	runtime, err := NewBursarRuntime(BursarRuntimeOptions{Bursar: sdk, Components: []RuntimeComponent{component}})
	if err != nil {
		t.Fatalf("NewBursarRuntime() error = %v", err)
	}
	if err := runtime.Start(context.Background()); err != nil {
		t.Fatalf("Start() error = %v", err)
	}
	health := runtime.Health()
	if !health.Ready || !health.CatalogLoaded || !component.started {
		t.Fatalf("Health() = %#v, component started = %v", health, component.started)
	}
	if err := runtime.Close(context.Background()); err != nil {
		t.Fatalf("Close() error = %v", err)
	}
	if !component.flushed || !component.closed || !creditStore.closed {
		t.Fatalf("close state = flush:%v component:%v store:%v", component.flushed, component.closed, creditStore.closed)
	}
}

func TestRuntimeRollsBackStartedComponents(t *testing.T) {
	creditStore := &facadeStoreStub{
		environment: ProviderEnvironmentTest,
		active:      &CatalogRevision{ID: "catalog-1", Version: 1, Config: map[string]any{"version": 1, "credits": map[string]any{}}},
	}
	sdk, err := New(Options{CreditStore: creditStore})
	if err != nil {
		t.Fatalf("New() error = %v", err)
	}
	first := &runtimeComponentStub{}
	second := runtimeComponentFailingStub{runtimeComponentStub: runtimeComponentStub{}, err: errors.New("start failed")}
	runtime, err := NewBursarRuntime(BursarRuntimeOptions{Bursar: sdk, Components: []RuntimeComponent{first, &second}})
	if err != nil {
		t.Fatalf("NewBursarRuntime() error = %v", err)
	}
	if err := runtime.Start(context.Background()); !errors.Is(err, second.err) {
		t.Fatalf("Start() error = %v, want component error", err)
	}
	if !first.closed {
		t.Fatal("first component was not rolled back")
	}
}

type runtimeComponentFailingStub struct {
	runtimeComponentStub
	err error
}

func (s *runtimeComponentFailingStub) Start(context.Context) error { return s.err }
