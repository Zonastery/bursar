// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package runtime

import (
	"context"
	"errors"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	bursar "github.com/Zonastery/bursar/golang/v2"
)

type componentStub struct {
	starts   int
	flushes  int
	closes   int
	health   error
	startErr error
}

func (c *componentStub) Start(context.Context) error {
	c.starts++
	return c.startErr
}

func (c *componentStub) Flush(context.Context) error {
	c.flushes++
	return nil
}

func (c *componentStub) Close(context.Context) error {
	c.closes++
	return nil
}

func (c *componentStub) Health(context.Context) error { return c.health }

type blockedStartComponent struct {
	startEntered chan struct{}
	releaseStart chan struct{}
	flushCalled  chan struct{}
	closeCalled  chan struct{}
}

func (c *blockedStartComponent) Start(context.Context) error {
	close(c.startEntered)
	<-c.releaseStart
	return nil
}

func (c *blockedStartComponent) Flush(context.Context) error {
	close(c.flushCalled)
	return nil
}

func (c *blockedStartComponent) Close(context.Context) error {
	close(c.closeCalled)
	return nil
}

func (*blockedStartComponent) Health(context.Context) error { return nil }

type blockedFlushComponent struct {
	flushEntered    chan struct{}
	releaseFlush    chan struct{}
	concurrentFlush chan struct{}
	closeCalled     chan struct{}
	closeWhileFlush chan struct{}
	enteredOnce     sync.Once
	concurrentOnce  sync.Once
	flushActive     atomic.Bool
}

func (c *blockedFlushComponent) Start(context.Context) error { return nil }

func (c *blockedFlushComponent) Flush(context.Context) error {
	if c.flushActive.Load() {
		c.concurrentOnce.Do(func() { close(c.concurrentFlush) })
	}
	c.flushActive.Store(true)
	c.enteredOnce.Do(func() { close(c.flushEntered) })
	<-c.releaseFlush
	c.flushActive.Store(false)
	return nil
}

func (c *blockedFlushComponent) Close(context.Context) error {
	if c.flushActive.Load() {
		close(c.closeWhileFlush)
	}
	close(c.closeCalled)
	return nil
}

func (*blockedFlushComponent) Health(context.Context) error { return nil }

func TestNewBursarRuntimeRejectsInvalidTenantAndConnections(t *testing.T) {
	t.Parallel()

	_, err := NewBursarRuntime(context.Background(), Options{
		TenantID:            "not-a-uuid",
		DatabaseURL:         "postgres://primary",
		OperatorDatabaseURL: "postgres://operator",
	})
	if err == nil {
		t.Fatal("expected invalid tenant ID error")
	}

	_, err = NewBursarRuntime(context.Background(), Options{
		TenantID:            "00000000-0000-0000-0000-000000000001",
		TenantSlug:          "bad slug",
		DatabaseURL:         "postgres://primary",
		OperatorDatabaseURL: "postgres://operator",
	})
	if err == nil {
		t.Fatal("expected invalid tenant slug error")
	}

	_, err = NewBursarRuntime(context.Background(), Options{
		TenantID:            "00000000-0000-0000-0000-000000000001",
		DatabaseURL:         "postgres://same",
		OperatorDatabaseURL: "postgres://same",
	})
	if err == nil || err.Error() != "primary and operator database URLs must be distinct" {
		t.Fatalf("expected distinct database URL error, got %v", err)
	}
}

func TestDependencyRuntimeLifecycleAndOwnership(t *testing.T) {
	t.Parallel()

	component := &componentStub{}
	runtime, err := NewBursarRuntime(context.Background(), Options{Dependencies: &Dependencies{
		Bursar:     &bursar.Bursar{},
		Components: []bursar.RuntimeComponent{component},
	}, TenantID: "00000000-0000-0000-0000-000000000001"})
	if err != nil {
		t.Fatalf("NewBursarRuntime() error = %v", err)
	}
	loadCatalog := false
	if err := runtime.Start(context.Background(), StartOptions{LoadCatalog: &loadCatalog}); err != nil {
		t.Fatalf("Start() error = %v", err)
	}
	if component.starts != 1 {
		t.Fatalf("component starts = %d, want 1", component.starts)
	}
	if err := runtime.Flush(context.Background()); err != nil {
		t.Fatalf("Flush() error = %v", err)
	}
	if component.flushes != 1 {
		t.Fatalf("component flushes = %d, want 1", component.flushes)
	}
	if err := runtime.Close(context.Background()); err != nil {
		t.Fatalf("Close() error = %v", err)
	}
	if component.closes != 0 {
		t.Fatalf("caller-owned component closes = %d, want 0", component.closes)
	}
	if err := runtime.Close(context.Background()); err != nil {
		t.Fatalf("second Close() error = %v", err)
	}
}

func TestRuntimeStartAndCloseAreSerialized(t *testing.T) {
	component := &blockedStartComponent{
		startEntered: make(chan struct{}),
		releaseStart: make(chan struct{}),
		flushCalled:  make(chan struct{}),
		closeCalled:  make(chan struct{}),
	}
	runtime := &Runtime{
		Bursar: &bursar.Bursar{},
		components: []runtimeComponent{{
			component: component,
			owned:     true,
		}},
	}
	loadCatalog := false
	startDone := make(chan error, 1)
	go func() {
		startDone <- runtime.Start(context.Background(), StartOptions{LoadCatalog: &loadCatalog})
	}()
	<-component.startEntered

	closeDone := make(chan error, 1)
	go func() {
		closeDone <- runtime.Close(context.Background())
	}()

	guard := time.NewTimer(100 * time.Millisecond)
	defer guard.Stop()
	select {
	case <-component.flushCalled:
		t.Fatal("Close flushed a component while Start was blocked")
	case <-component.closeCalled:
		t.Fatal("Close closed a component while Start was blocked")
	case <-guard.C:
	}
	if state := runtime.State(); state.Closed {
		t.Fatal("Close marked the runtime closed while Start was blocked")
	}

	close(component.releaseStart)
	if err := <-startDone; err != nil {
		t.Fatalf("Start() error = %v", err)
	}
	if err := <-closeDone; err != nil {
		t.Fatalf("Close() error = %v", err)
	}
	select {
	case <-component.flushCalled:
	case <-time.After(time.Second):
		t.Fatal("Close did not flush the component after Start released")
	}
	select {
	case <-component.closeCalled:
	case <-time.After(time.Second):
		t.Fatal("Close did not close the component after Start released")
	}
}

func TestRuntimeFlushAndCloseAreSerialized(t *testing.T) {
	component := &blockedFlushComponent{
		flushEntered:    make(chan struct{}),
		releaseFlush:    make(chan struct{}),
		concurrentFlush: make(chan struct{}),
		closeCalled:     make(chan struct{}),
		closeWhileFlush: make(chan struct{}),
	}
	runtime := &Runtime{
		Bursar:  &bursar.Bursar{},
		started: true,
		components: []runtimeComponent{{
			component: component,
			owned:     true,
		}},
	}

	flushDone := make(chan error, 1)
	go func() { flushDone <- runtime.Flush(context.Background()) }()
	<-component.flushEntered

	closeDone := make(chan error, 1)
	go func() { closeDone <- runtime.Close(context.Background()) }()

	guard := time.NewTimer(100 * time.Millisecond)
	defer guard.Stop()
	select {
	case <-component.concurrentFlush:
		t.Fatal("Close started a concurrent component Flush")
	case <-component.closeWhileFlush:
		t.Fatal("Close closed a component while Flush was blocked")
	case <-component.closeCalled:
		t.Fatal("Close closed a component before the blocked Flush released")
	case <-guard.C:
	}
	if state := runtime.State(); state.Closed {
		t.Fatal("Close marked the runtime closed while Flush was blocked")
	}

	close(component.releaseFlush)
	if err := <-flushDone; err != nil {
		t.Fatalf("Flush() error = %v", err)
	}
	if err := <-closeDone; err != nil {
		t.Fatalf("Close() error = %v", err)
	}
	select {
	case <-component.concurrentFlush:
		t.Fatal("component Flush calls overlapped")
	default:
	}
	select {
	case <-component.closeWhileFlush:
		t.Fatal("component Close overlapped Flush")
	default:
	}
	select {
	case <-component.closeCalled:
	default:
		t.Fatal("Close did not close the component")
	}
}

func TestRuntimeFlushAndCloseRejectNilContexts(t *testing.T) {
	runtime := &Runtime{}
	var nilContext context.Context
	if err := runtime.Flush(nilContext); err == nil || err.Error() != "runtime context is required" {
		t.Fatalf("Flush(nil) error = %v", err)
	}
	if err := runtime.Close(nilContext); err == nil || err.Error() != "runtime context is required" {
		t.Fatalf("Close(nil) error = %v", err)
	}
}

func TestDependencyRuntimeHealthReportsComponentFailure(t *testing.T) {
	t.Parallel()

	component := &componentStub{health: errors.New("projection unavailable")}
	runtime, err := NewBursarRuntime(context.Background(), Options{Dependencies: &Dependencies{
		Bursar:     &bursar.Bursar{},
		Components: []bursar.RuntimeComponent{component},
	}, TenantID: "00000000-0000-0000-0000-000000000001"})
	if err != nil {
		t.Fatalf("NewBursarRuntime() error = %v", err)
	}
	diagnostics := runtime.CheckDependencies(context.Background())
	if len(diagnostics.Components) != 1 || diagnostics.Components[0].OK {
		t.Fatalf("unexpected component diagnostics: %#v", diagnostics.Components)
	}
	if diagnostics.Components[0].Error != "dependency_check_failed:Error" {
		t.Fatalf("component error = %q", diagnostics.Components[0].Error)
	}
}

func TestRuntimeHealthRequiresSuccessfulStart(t *testing.T) {
	t.Parallel()

	component := &componentStub{}
	runtime, err := NewBursarRuntime(context.Background(), Options{Dependencies: &Dependencies{
		Bursar:     &bursar.Bursar{},
		Components: []bursar.RuntimeComponent{component},
	}, TenantID: "00000000-0000-0000-0000-000000000001"})
	if err != nil {
		t.Fatalf("NewBursarRuntime() error = %v", err)
	}
	if health := runtime.Health(context.Background()); health.Ready || health.FinancialReady || health.ProjectionReady || health.Degraded {
		t.Fatalf("health before Start() = %#v", health)
	}
	if err := runtime.Close(context.Background()); err != nil {
		t.Fatalf("Close() before Start() error = %v", err)
	}
	if component.flushes != 0 || component.closes != 0 {
		t.Fatalf("unstarted caller component flushes/closes = %d/%d, want 0/0", component.flushes, component.closes)
	}
	if health := runtime.Health(context.Background()); !health.Closed || health.ProjectionReady {
		t.Fatalf("health after Close() = %#v", health)
	}
}

func TestNilRuntimeHealthAndDiagnosticsAreSafe(t *testing.T) {
	t.Parallel()

	var runtime *Runtime
	if health := runtime.Health(context.Background()); !health.Closed || health.Ready {
		t.Fatalf("nil runtime health = %#v", health)
	}
	diagnostics := runtime.CheckDependencies(context.Background())
	if diagnostics.Catalog.Error != "catalog is not configured" || len(diagnostics.Components) != 0 {
		t.Fatalf("nil runtime diagnostics = %#v", diagnostics)
	}
}

type closableCreditStoreStub struct {
	bursar.CreditStore
	closes int
}

func (s *closableCreditStoreStub) Close() error {
	s.closes++
	return nil
}

func TestDependencyRuntimeBorrowsFacade(t *testing.T) {
	t.Parallel()

	store := &closableCreditStoreStub{}
	credits, err := bursar.NewCreditsService(store, bursar.CreditsServiceOptions{})
	if err != nil {
		t.Fatalf("NewCreditsService() error = %v", err)
	}
	runtime, err := NewBursarRuntime(context.Background(), Options{Dependencies: &Dependencies{
		Bursar: &bursar.Bursar{Credits: credits, Catalog: credits.Catalog()},
	}, TenantID: "00000000-0000-0000-0000-000000000001"})
	if err != nil {
		t.Fatalf("NewBursarRuntime() error = %v", err)
	}
	if err := runtime.Close(context.Background()); err != nil {
		t.Fatalf("Close() error = %v", err)
	}
	if store.closes != 0 {
		t.Fatalf("borrowed facade store closes = %d, want 0", store.closes)
	}
}

func TestDependencyRuntimeDoesNotRestartAfterPartialStartupFailure(t *testing.T) {
	t.Parallel()

	first := &componentStub{}
	second := &componentStub{startErr: errors.New("start failed")}
	runtime, err := NewBursarRuntime(context.Background(), Options{Dependencies: &Dependencies{
		Bursar:     &bursar.Bursar{},
		Components: []bursar.RuntimeComponent{first, second},
	}, TenantID: "00000000-0000-0000-0000-000000000001"})
	if err != nil {
		t.Fatalf("NewBursarRuntime() error = %v", err)
	}
	loadCatalog := false
	if err := runtime.Start(context.Background(), StartOptions{LoadCatalog: &loadCatalog}); err == nil {
		t.Fatal("Start() succeeded with a failing component")
	}
	if first.starts != 1 || first.flushes != 1 || first.closes != 0 || second.starts != 1 {
		t.Fatalf("partial startup state: first=%+v second=%+v", first, second)
	}
	if err := runtime.Start(context.Background(), StartOptions{LoadCatalog: &loadCatalog}); err == nil {
		t.Fatal("second Start() succeeded after partial startup failure")
	}
	if first.starts != 1 || second.starts != 1 {
		t.Fatalf("components restarted after partial failure: first=%d second=%d", first.starts, second.starts)
	}
}
