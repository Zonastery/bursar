package bursar

import (
	"context"
	"errors"
	"strings"
	"sync"
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

func TestBursarConstructorValidatesRequiredOptions(t *testing.T) {
	if sdk, err := New(Options{}); sdk != nil || err == nil {
		t.Fatalf("New(Options{}) = sdk:%v error:%v, want typed error", sdk, err)
	}
	store := &facadeStoreStub{environment: ProviderEnvironmentTest}
	if sdk, err := New(Options{CreditStore: store, BillingOptions: &BillingServiceOptions{}}); sdk != nil || err == nil {
		t.Fatalf("billing options without store = sdk:%v error:%v, want typed error", sdk, err)
	}
	emitter := NewCreditEventEmitter()
	if sdk, err := New(Options{CreditStore: store, Emitter: emitter, CreditsOptions: CreditsServiceOptions{EventSink: emitter}}); sdk != nil || err == nil {
		t.Fatalf("duplicate event sinks = sdk:%v error:%v, want typed error", sdk, err)
	}
	if sdk, err := New(Options{CreditStore: store, BillingStore: &facadeBillingStoreStub{environment: "invalid"}}); sdk != nil || err == nil {
		t.Fatalf("invalid billing environment = sdk:%v error:%v, want typed error", sdk, err)
	}
	sdk, err := New(Options{CreditStore: store, Emitter: emitter})
	if err != nil || sdk == nil || sdk.Credits.events != emitter {
		t.Fatalf("valid emitter = sdk:%v error:%v, want emitter snapshot", sdk, err)
	}
	if err := sdk.Close(); err != nil {
		t.Fatalf("emitter facade Close() error = %v", err)
	}
	if sdk, err := New(Options{CreditStore: store, BillingStore: &facadeBillingStoreStub{environment: ProviderEnvironmentTest}}); err != nil || sdk == nil || sdk.Billing == nil {
		t.Fatalf("billing without options = sdk:%v error:%v, want billing capability", sdk, err)
	}
}

func TestBursarAppliesBillingOptionsAcrossHandlerSurfaces(t *testing.T) {
	creditStore := &facadeStoreStub{environment: ProviderEnvironmentTest}
	billingStore := &facadeBillingStoreStub{environment: ProviderEnvironmentTest}
	provisioning := &billingProvisioningStub{}
	grace := time.Hour
	autoSelect := false
	sdk, err := New(Options{
		CreditStore:  creditStore,
		BillingStore: billingStore,
		BillingOptions: &BillingServiceOptions{
			Handlers: map[BillingEventType]BillingEventHandler{
				BillingEventPaymentSucceeded: func(context.Context, BillingEvent, string) error { return nil },
			},
			DefaultHandler: func(context.Context, BillingEvent, string) error { return nil },
			EventHandlers: map[BillingEventType]BillingEventCallback{
				BillingEventPaymentSucceeded: func(context.Context, BillingEvent, string) {},
			},
			Provisioning:                provisioning,
			AutoSelectEntitlementSource: &autoSelect,
			PastDueGracePeriod:          &grace,
			TerminalPlanKey:             "terminal",
		},
	})
	if err != nil {
		t.Fatalf("New() error = %v", err)
	}
	if sdk.Billing == nil || sdk.Billing.defaultHandler == nil || sdk.Billing.provisioning != provisioning || sdk.Billing.autoSelectEntitlementSource || sdk.Billing.pastDueGracePeriod != grace || sdk.Billing.terminalPlanKey != "terminal" {
		t.Fatalf("billing options were not applied: %#v", sdk.Billing)
	}
	if len(sdk.Billing.handlers) != 1 || len(sdk.Billing.eventHandlers) != 1 {
		t.Fatalf("billing handler maps = handlers:%d events:%d", len(sdk.Billing.handlers), len(sdk.Billing.eventHandlers))
	}
	if err := sdk.Close(); err != nil {
		t.Fatalf("Bursar.Close() error = %v", err)
	}
}

func TestBursarRejectsNegativeBillingGracePeriod(t *testing.T) {
	grace := -time.Second
	_, err := New(Options{
		CreditStore:  &facadeStoreStub{environment: ProviderEnvironmentTest},
		BillingStore: &facadeBillingStoreStub{environment: ProviderEnvironmentTest},
		BillingOptions: &BillingServiceOptions{
			PastDueGracePeriod: &grace,
		},
	})
	if err == nil {
		t.Fatal("negative billing grace period was accepted")
	}
}

func TestBursarComposesCommerceStoreCapabilities(t *testing.T) {
	store := &billingManagementStore{
		billingLifecycleStoreStub: &billingLifecycleStoreStub{environment: ProviderEnvironmentTest},
		checkoutStoreStub:         &checkoutStoreStub{},
		autoRechargeStoreStub:     &autoRechargeStoreStub{},
		activeCatalog:             &CatalogRevision{Config: map[string]any{"version": 1}},
	}
	sdk, err := New(Options{
		CreditStore:  &facadeStoreStub{environment: ProviderEnvironmentTest},
		BillingStore: store,
		CommerceOptions: &CommerceOptions{
			Providers: &ProviderRegistry{},
		},
	})
	if err != nil {
		t.Fatalf("New() commerce error = %v", err)
	}
	if sdk.Commerce == nil || sdk.Billing == nil || sdk.Billing.AutoRecharge == nil {
		t.Fatalf("composed capabilities = billing:%v autoRecharge:%v commerce:%v", sdk.Billing != nil, sdk.Billing != nil && sdk.Billing.AutoRecharge != nil, sdk.Commerce != nil)
	}
	if err := sdk.Close(); err != nil {
		t.Fatalf("commerce facade Close() error = %v", err)
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

type runtimeErrorComponent struct {
	startErr  error
	flushErr  error
	closeErr  error
	healthErr error
}

func (s *runtimeErrorComponent) Start(context.Context) error  { return s.startErr }
func (s *runtimeErrorComponent) Flush(context.Context) error  { return s.flushErr }
func (s *runtimeErrorComponent) Close(context.Context) error  { return s.closeErr }
func (s *runtimeErrorComponent) Health(context.Context) error { return s.healthErr }

func TestNewBursarRuntimeRejectsMissingDependencies(t *testing.T) {
	if runtime, err := NewBursarRuntime(BursarRuntimeOptions{}); runtime != nil || err == nil {
		t.Fatalf("NewBursarRuntime() = runtime:%v error:%v, want typed error", runtime, err)
	}
	if runtime, err := NewBursarRuntime(BursarRuntimeOptions{Bursar: &Bursar{}, Components: []RuntimeComponent{nil}}); runtime != nil || err == nil {
		t.Fatalf("nil runtime component = runtime:%v error:%v, want typed error", runtime, err)
	}
}

func TestRuntimeLifecycleErrorsAndNilReceiverAreFailClosed(t *testing.T) {
	var runtime *BursarRuntime
	if err := runtime.Start(context.Background()); err == nil {
		t.Fatal("nil runtime Start() succeeded")
	}
	if err := runtime.Flush(context.Background()); err != nil {
		t.Fatalf("nil runtime Flush() error = %v", err)
	}
	if err := runtime.Close(context.Background()); err != nil {
		t.Fatalf("nil runtime Close() error = %v", err)
	}

	component := &runtimeErrorComponent{flushErr: errors.New("flush failed"), closeErr: errors.New("close failed"), healthErr: errors.New("health failed")}
	sdk, err := New(Options{CreditStore: &facadeStoreStub{
		environment: ProviderEnvironmentTest,
		active:      &CatalogRevision{Config: map[string]any{"version": 1}},
	}})
	if err != nil {
		t.Fatalf("New() error = %v", err)
	}
	runtime, err = NewBursarRuntime(BursarRuntimeOptions{Bursar: sdk, Components: []RuntimeComponent{component}})
	if err != nil {
		t.Fatalf("NewBursarRuntime() error = %v", err)
	}
	loadCatalog := false
	if err := runtime.StartWithOptions(context.Background(), BursarRuntimeStartOptions{LoadCatalog: &loadCatalog}); err != nil {
		t.Fatalf("StartWithOptions() error = %v", err)
	}
	diagnostics := runtime.CheckDependencies(context.Background())
	if diagnostics.Catalog.Skipped || !diagnostics.Catalog.OK || len(diagnostics.Components) != 1 || diagnostics.Components[0].OK || diagnostics.Components[0].Error == "" {
		t.Fatalf("dependency diagnostics = %#v", diagnostics)
	}
	if err := runtime.Flush(context.Background()); err == nil || !strings.Contains(err.Error(), "flush failed") {
		t.Fatalf("Flush() error = %v, want component failure", err)
	}
	if err := runtime.Close(context.Background()); err == nil || !strings.Contains(err.Error(), "close failed") {
		t.Fatalf("Close() error = %v, want component failure", err)
	}
	if err := runtime.Close(context.Background()); err != nil {
		t.Fatalf("repeated Close() error = %v", err)
	}
}

func TestRuntimeStartOptionsAndClosedState(t *testing.T) {
	loadCatalog := false
	runtime, err := NewBursarRuntime(BursarRuntimeOptions{Bursar: &Bursar{}})
	if err != nil {
		t.Fatalf("NewBursarRuntime() error = %v", err)
	}
	if err := runtime.StartWithOptions(context.Background(), BursarRuntimeStartOptions{LoadCatalog: &loadCatalog}); err != nil {
		t.Fatalf("StartWithOptions() error = %v", err)
	}
	if err := runtime.Start(context.Background()); err != nil {
		t.Fatalf("idempotent Start() error = %v", err)
	}
	if err := runtime.Close(context.Background()); err != nil {
		t.Fatalf("Close() error = %v", err)
	}
	if err := runtime.Start(context.Background()); err == nil {
		t.Fatal("closed runtime restarted successfully")
	}
}

type serializedRuntimeComponent struct {
	startEntered chan struct{}
	releaseStart chan struct{}
	flushEntered chan struct{}
	releaseFlush chan struct{}
	closeCalled  chan struct{}
	startOnce    sync.Once
	flushOnce    sync.Once
	closeOnce    sync.Once
}

func (s *serializedRuntimeComponent) Start(context.Context) error {
	if s.startEntered != nil {
		s.startOnce.Do(func() { close(s.startEntered) })
		<-s.releaseStart
	}
	return nil
}

func (s *serializedRuntimeComponent) Flush(context.Context) error {
	if s.flushEntered != nil {
		s.flushOnce.Do(func() { close(s.flushEntered) })
		<-s.releaseFlush
	}
	return nil
}

func (s *serializedRuntimeComponent) Close(context.Context) error {
	s.closeOnce.Do(func() { close(s.closeCalled) })
	return nil
}

func TestRuntimeStartAndCloseAreSerialized(t *testing.T) {
	component := &serializedRuntimeComponent{
		startEntered: make(chan struct{}),
		releaseStart: make(chan struct{}),
		closeCalled:  make(chan struct{}),
	}
	runtime, err := NewBursarRuntime(BursarRuntimeOptions{
		Bursar:     &Bursar{},
		Components: []RuntimeComponent{component},
	})
	if err != nil {
		t.Fatalf("NewBursarRuntime() error = %v", err)
	}
	loadCatalog := false
	startDone := make(chan error, 1)
	go func() {
		startDone <- runtime.StartWithOptions(context.Background(), BursarRuntimeStartOptions{LoadCatalog: &loadCatalog})
	}()
	<-component.startEntered

	closeDone := make(chan error, 1)
	go func() { closeDone <- runtime.Close(context.Background()) }()
	select {
	case <-component.closeCalled:
		t.Fatal("Close closed a component while Start was blocked")
	case <-time.After(50 * time.Millisecond):
	}
	if runtime.Health().Closed {
		t.Fatal("Close marked runtime closed while Start was blocked")
	}

	close(component.releaseStart)
	if err := <-startDone; err != nil {
		t.Fatalf("Start() error = %v", err)
	}
	if err := <-closeDone; err != nil {
		t.Fatalf("Close() error = %v", err)
	}
	select {
	case <-component.closeCalled:
	case <-time.After(time.Second):
		t.Fatal("Close did not close component after Start completed")
	}
}

func TestRuntimeFlushAndCloseAreSerialized(t *testing.T) {
	component := &serializedRuntimeComponent{
		flushEntered: make(chan struct{}),
		releaseFlush: make(chan struct{}),
		closeCalled:  make(chan struct{}),
	}
	runtime, err := NewBursarRuntime(BursarRuntimeOptions{Bursar: &Bursar{}, Components: []RuntimeComponent{component}})
	if err != nil {
		t.Fatalf("NewBursarRuntime() error = %v", err)
	}
	loadCatalog := false
	if err := runtime.StartWithOptions(context.Background(), BursarRuntimeStartOptions{LoadCatalog: &loadCatalog}); err != nil {
		t.Fatalf("Start() error = %v", err)
	}

	flushDone := make(chan error, 1)
	go func() { flushDone <- runtime.Flush(context.Background()) }()
	<-component.flushEntered
	closeDone := make(chan error, 1)
	go func() { closeDone <- runtime.Close(context.Background()) }()
	select {
	case <-component.closeCalled:
		t.Fatal("Close closed a component while Flush was blocked")
	case <-time.After(50 * time.Millisecond):
	}

	close(component.releaseFlush)
	if err := <-flushDone; err != nil {
		t.Fatalf("Flush() error = %v", err)
	}
	if err := <-closeDone; err != nil {
		t.Fatalf("Close() error = %v", err)
	}
	select {
	case <-component.closeCalled:
	case <-time.After(time.Second):
		t.Fatal("Close did not close component after Flush completed")
	}
}

func TestRuntimeRejectsNilLifecycleContexts(t *testing.T) {
	runtime, err := NewBursarRuntime(BursarRuntimeOptions{Bursar: &Bursar{}})
	if err != nil {
		t.Fatalf("NewBursarRuntime() error = %v", err)
	}
	var nilContext context.Context
	if err := runtime.Start(nilContext); err == nil {
		t.Fatal("Start(nil) succeeded")
	}
	if err := runtime.Flush(nilContext); err == nil {
		t.Fatal("Flush(nil) succeeded")
	}
	if err := runtime.Close(nilContext); err == nil {
		t.Fatal("Close(nil) succeeded")
	}
}
