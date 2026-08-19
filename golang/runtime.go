// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import (
	"context"
	"errors"
	"strconv"
	"sync"
	"time"
)

// RuntimeComponent is an optional lifecycle participant such as an outbox
// projector or metrics exporter. The SDK deliberately does not prescribe a
// queue, object store, or web framework.
type RuntimeComponent interface {
	Start(context.Context) error
	Flush(context.Context) error
	Close(context.Context) error
}

// RuntimeHealthChecker is optionally implemented by a RuntimeComponent when
// it can perform a dependency check beyond lifecycle state.
type RuntimeHealthChecker interface {
	Health(context.Context) error
}

// BursarRuntimeOptions composes a Bursar facade with optional background
// components. Bursar remains the PostgreSQL accounting authority.
type BursarRuntimeOptions struct {
	Bursar     *Bursar
	Components []RuntimeComponent
}

// BursarRuntimeStartOptions controls startup catalog behavior. Nil
// LoadCatalog defaults to true, matching the Python and TypeScript runtimes.
type BursarRuntimeStartOptions struct {
	LoadCatalog *bool
}

// BursarRuntimeHealth is a compact readiness snapshot suitable for a service
// readiness endpoint. It intentionally contains no credentials or SQL text.
type BursarRuntimeHealth struct {
	Ready           bool
	FinancialReady  bool
	ProjectionReady bool
	Degraded        bool
	Started         bool
	Closed          bool
	CatalogLoaded   bool
}

// DependencyCheck records one best-effort runtime dependency check.
type DependencyCheck struct {
	Name    string
	OK      bool
	Latency time.Duration
	Error   string
	Skipped bool
}

// BursarRuntimeDiagnostics combines readiness with safe dependency outcomes.
type BursarRuntimeDiagnostics struct {
	CheckedAt  time.Time
	Health     BursarRuntimeHealth
	Catalog    DependencyCheck
	Components []DependencyCheck
}

// BursarRuntime coordinates startup, health, flush, and close for a Bursar
// application process. It owns no migrations or administrative CLI behavior.
type BursarRuntime struct {
	Bursar     *Bursar
	components []RuntimeComponent

	mu            sync.Mutex
	startMu       sync.Mutex
	started       bool
	closed        bool
	catalogLoaded bool
	componentsUp  int
}

// NewBursarRuntime creates a framework-neutral runtime around an already
// configured Bursar facade.
func NewBursarRuntime(options BursarRuntimeOptions) (*BursarRuntime, error) {
	if options.Bursar == nil {
		return nil, NewError("runtime requires Bursar", ErrorOptions{Code: ErrorCodeConfig, Category: ErrorCategoryInvalidRequest})
	}
	components := append([]RuntimeComponent(nil), options.Components...)
	for index, component := range components {
		if component == nil {
			return nil, NewError("runtime component must not be nil", ErrorOptions{Code: ErrorCodeConfig, Category: ErrorCategoryInvalidRequest, Details: map[string]any{"index": index}})
		}
	}
	return &BursarRuntime{Bursar: options.Bursar, components: components}, nil
}

// Start starts the runtime and loads the active catalog by default.
func (r *BursarRuntime) Start(ctx context.Context) error {
	return r.StartWithOptions(ctx, BursarRuntimeStartOptions{})
}

// StartWithOptions starts components only after an optional active-catalog
// load has succeeded. A failed component startup is unwound in reverse order.
func (r *BursarRuntime) StartWithOptions(ctx context.Context, options BursarRuntimeStartOptions) error {
	if r == nil || r.Bursar == nil {
		return NewError("runtime is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	if ctx == nil {
		return NewError("runtime context is required", ErrorOptions{Code: ErrorCodeConfig, Category: ErrorCategoryInvalidRequest})
	}
	loadCatalog := true
	if options.LoadCatalog != nil {
		loadCatalog = *options.LoadCatalog
	}
	r.startMu.Lock()
	defer r.startMu.Unlock()
	r.mu.Lock()
	if r.closed {
		r.mu.Unlock()
		return NewError("runtime is closed", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	if r.started {
		r.mu.Unlock()
		return nil
	}
	r.mu.Unlock()

	if loadCatalog {
		if err := r.Bursar.LoadCatalog(ctx); err != nil {
			return err
		}
	}
	started := 0
	for index, component := range r.components {
		if err := component.Start(ctx); err != nil {
			for rollback := index - 1; rollback >= 0; rollback-- {
				_ = r.components[rollback].Close(context.Background())
			}
			return err
		}
		started++
	}
	r.mu.Lock()
	if r.closed {
		r.mu.Unlock()
		for rollback := started - 1; rollback >= 0; rollback-- {
			_ = r.components[rollback].Close(context.Background())
		}
		return NewError("runtime closed during startup", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	r.started = true
	r.componentsUp = started
	r.catalogLoaded = loadCatalog && r.Bursar.Catalog != nil && r.Bursar.Catalog.IsLoaded()
	r.mu.Unlock()
	return nil
}

// Health returns the runtime's local readiness state without initiating I/O.
func (r *BursarRuntime) Health() BursarRuntimeHealth {
	if r == nil {
		return BursarRuntimeHealth{Closed: true}
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	financialReady := r.started && !r.closed && r.catalogLoaded
	projectionReady := r.started && !r.closed && r.componentsUp == len(r.components)
	return BursarRuntimeHealth{
		Ready:           financialReady && projectionReady,
		FinancialReady:  financialReady,
		ProjectionReady: projectionReady,
		Degraded:        financialReady && !projectionReady,
		Started:         r.started,
		Closed:          r.closed,
		CatalogLoaded:   r.catalogLoaded,
	}
}

// CheckDependencies performs non-mutating catalog and optional component
// checks, returning diagnostics rather than exposing transport-specific state.
func (r *BursarRuntime) CheckDependencies(ctx context.Context) BursarRuntimeDiagnostics {
	diagnostics := BursarRuntimeDiagnostics{CheckedAt: time.Now().UTC(), Health: r.Health()}
	if r == nil || r.Bursar == nil || r.Bursar.Catalog == nil {
		diagnostics.Catalog = DependencyCheck{Name: "catalog", Skipped: true, Error: "runtime is not initialized"}
		return diagnostics
	}
	if diagnostics.Health.Closed {
		diagnostics.Catalog = DependencyCheck{Name: "catalog", Skipped: true, Error: "runtime is closed"}
		return diagnostics
	}
	started := time.Now()
	_, err := r.Bursar.Catalog.GetActive(ctx)
	diagnostics.Catalog = dependencyOutcome("catalog", started, err)
	diagnostics.Components = make([]DependencyCheck, 0, len(r.components))
	for index, component := range r.components {
		checker, ok := component.(RuntimeHealthChecker)
		if !ok {
			diagnostics.Components = append(diagnostics.Components, DependencyCheck{Name: runtimeComponentName(index), Skipped: true})
			continue
		}
		started := time.Now()
		diagnostics.Components = append(diagnostics.Components, dependencyOutcome(runtimeComponentName(index), started, checker.Health(ctx)))
	}
	return diagnostics
}

// Flush asks started components to make best effort to persist buffered
// projections. It never alters Bursar's ledger state itself.
func (r *BursarRuntime) Flush(ctx context.Context) error {
	if r == nil {
		return nil
	}
	if ctx == nil {
		return NewError("runtime context is required", ErrorOptions{Code: ErrorCodeConfig, Category: ErrorCategoryInvalidRequest})
	}
	r.startMu.Lock()
	defer r.startMu.Unlock()
	r.mu.Lock()
	if r.closed {
		r.mu.Unlock()
		return NewError("runtime is closed", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	count := r.componentsUp
	components := append([]RuntimeComponent(nil), r.components[:count]...)
	r.mu.Unlock()
	var failures []error
	for index := len(components) - 1; index >= 0; index-- {
		if err := components[index].Flush(ctx); err != nil {
			failures = append(failures, err)
		}
	}
	return errors.Join(failures...)
}

// Close flushes and closes components in reverse startup order, then releases
// the Bursar credit store. It is safe to call repeatedly.
func (r *BursarRuntime) Close(ctx context.Context) error {
	if r == nil {
		return nil
	}
	if ctx == nil {
		return NewError("runtime context is required", ErrorOptions{Code: ErrorCodeConfig, Category: ErrorCategoryInvalidRequest})
	}
	r.startMu.Lock()
	defer r.startMu.Unlock()
	r.mu.Lock()
	if r.closed {
		r.mu.Unlock()
		return nil
	}
	r.closed = true
	count := r.componentsUp
	components := append([]RuntimeComponent(nil), r.components[:count]...)
	bursar := r.Bursar
	r.mu.Unlock()
	var failures []error
	for index := len(components) - 1; index >= 0; index-- {
		if err := components[index].Flush(ctx); err != nil {
			failures = append(failures, err)
		}
		if err := components[index].Close(ctx); err != nil {
			failures = append(failures, err)
		}
	}
	if bursar != nil {
		if err := bursar.Close(); err != nil {
			failures = append(failures, err)
		}
	}
	return errors.Join(failures...)
}

func dependencyOutcome(name string, started time.Time, err error) DependencyCheck {
	result := DependencyCheck{Name: name, OK: err == nil, Latency: time.Since(started)}
	if err != nil {
		result.Error = PublicErrorMessage(err)
	}
	return result
}

func runtimeComponentName(index int) string {
	return "component_" + strconv.Itoa(index+1)
}
