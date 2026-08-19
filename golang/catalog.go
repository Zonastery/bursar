// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import (
	"context"
	"sync"
	"time"
)

const defaultCatalogCacheTTL = 5 * time.Minute

// CatalogServiceOptions controls automatic active-catalog refresh. A zero TTL
// disables automatic refresh; callers can still force a reload with Load.
type CatalogServiceOptions struct {
	CacheTTL time.Duration
}

// CatalogService owns process-local parsing and pricing-engine caching around
// the database-authoritative catalog revision. Catalog writes remain atomic
// database RPCs exposed through CreditStore.
type CatalogService struct {
	store CreditStore

	mu             sync.RWMutex
	refreshMu      sync.Mutex
	revision       *CatalogRevision
	config         *BursarConfig
	engine         *PricingEngine
	versionEngines map[int]*PricingEngine
	cacheTTL       time.Duration
	loadedAt       time.Time
	now            func() time.Time
}

// NewCatalogService constructs the catalog capability for a durable store.
func NewCatalogService(store CreditStore) (*CatalogService, error) {
	return NewCatalogServiceWithOptions(store, CatalogServiceOptions{CacheTTL: defaultCatalogCacheTTL})
}

// NewCatalogServiceWithOptions constructs a catalog capability with an
// explicit refresh TTL.
func NewCatalogServiceWithOptions(store CreditStore, options CatalogServiceOptions) (*CatalogService, error) {
	if store == nil {
		return nil, NewError("catalog requires a credit store", ErrorOptions{Code: ErrorCodeConfig, Category: ErrorCategoryInvalidRequest})
	}
	if options.CacheTTL < 0 {
		return nil, NewError("catalog cache TTL must not be negative", ErrorOptions{Code: ErrorCodeConfig, Category: ErrorCategoryInvalidRequest})
	}
	return &CatalogService{
		store: store, versionEngines: make(map[int]*PricingEngine),
		cacheTTL: options.CacheTTL, now: time.Now,
	}, nil
}

// GetActive returns the currently active persisted catalog revision, if any.
func (s *CatalogService) GetActive(ctx context.Context) (*CatalogRevision, error) {
	if s == nil || s.store == nil {
		return nil, NewError("catalog service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	return s.store.GetActiveCatalog(ctx)
}

// IsLoaded reports whether this process currently has a parsed active catalog
// and pricing engine. It does not query PostgreSQL.
func (s *CatalogService) IsLoaded() bool {
	if s == nil {
		return false
	}
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.engine != nil && s.config != nil && s.revision != nil
}

// Load reads the current active persisted revision, validates it using the
// shared pricing schema and semantics, and installs an exact PricingEngine.
func (s *CatalogService) Load(ctx context.Context) error {
	return s.refreshNow(ctx)
}

func (s *CatalogService) refreshNow(ctx context.Context) error {
	if s == nil || s.store == nil {
		return NewError("catalog service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	revision, err := s.store.GetActiveCatalog(ctx)
	if err != nil {
		return err
	}
	if revision == nil {
		return NewError("no active Bursar catalog is available", ErrorOptions{Code: ErrorCodeCatalogNotLoaded, Category: ErrorCategoryNotFound})
	}
	s.mu.RLock()
	loaded := s.revision != nil && s.revision.ID == revision.ID && s.revision.Version == revision.Version && s.engine != nil
	s.mu.RUnlock()
	if loaded {
		s.mu.Lock()
		s.loadedAt = s.now().UTC()
		s.mu.Unlock()
		return nil
	}
	return s.install(revision)
}

// Refresh reloads after the configured cache TTL. A zero TTL disables
// automatic refresh, matching the Python and TypeScript SDKs. Concurrent
// stale readers coalesce into one PostgreSQL read.
func (s *CatalogService) Refresh(ctx context.Context) error {
	return s.RefreshIfStale(ctx)
}

// RefreshIfStale is the explicit name for Refresh's TTL-aware behavior.
func (s *CatalogService) RefreshIfStale(ctx context.Context) error {
	if s == nil || s.store == nil {
		return NewError("catalog service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	if s.cacheTTL == 0 {
		return nil
	}
	if s.catalogFresh() {
		return nil
	}
	s.refreshMu.Lock()
	defer s.refreshMu.Unlock()
	if s.catalogFresh() {
		return nil
	}
	return s.refreshNow(ctx)
}

func (s *CatalogService) catalogFresh() bool {
	s.mu.RLock()
	loadedAt := s.loadedAt
	cacheTTL := s.cacheTTL
	now := s.now
	s.mu.RUnlock()
	return !loadedAt.IsZero() && now().Sub(loadedAt) < cacheTTL
}

// Invalidate marks the in-process catalog stale without discarding the last
// valid engine. The next TTL-aware Refresh or unpinned pricing call reloads it.
func (s *CatalogService) Invalidate() {
	if s == nil {
		return
	}
	s.mu.Lock()
	s.loadedAt = time.Time{}
	s.mu.Unlock()
}

// GetConfig reads and validates the active persisted catalog. It does not
// require an explicit prior Load, which is useful for control-plane workflows.
func (s *CatalogService) GetConfig(ctx context.Context) (*BursarConfig, error) {
	revision, err := s.GetActive(ctx)
	if err != nil {
		return nil, err
	}
	if revision == nil {
		return nil, NewError("no active Bursar catalog is available", ErrorOptions{Code: ErrorCodeCatalogNotLoaded, Category: ErrorCategoryNotFound})
	}
	return LoadConfigFromMap(revision.Config)
}

// PublicView projects the active catalog into a provider-safe product surface.
func (s *CatalogService) PublicView(ctx context.Context) (PublicCatalog, error) {
	config, err := s.GetConfig(ctx)
	if err != nil {
		return PublicCatalog{}, err
	}
	return ProjectPublicCatalog(config), nil
}

// Engine returns the current exact pricing engine after Load or Refresh.
func (s *CatalogService) Engine() (*PricingEngine, error) {
	if s == nil {
		return nil, NewError("catalog service is not initialized", ErrorOptions{Code: ErrorCodeCatalogNotLoaded, Category: ErrorCategoryUnavailable})
	}
	s.mu.RLock()
	engine := s.engine
	s.mu.RUnlock()
	if engine == nil {
		return nil, NewError("catalog is not loaded", ErrorOptions{Code: ErrorCodeCatalogNotLoaded, Category: ErrorCategoryConflict})
	}
	return engine, nil
}

// CalculateForUser prices metrics using the subject's effective catalog
// revision and rate card. A pinned assignment is always evaluated against its
// historical immutable revision rather than the process's active catalog.
func (s *CatalogService) CalculateForUser(ctx context.Context, userID string, metrics UsageMetrics) (CostBreakdown, error) {
	if s == nil || s.store == nil {
		return CostBreakdown{}, NewError("catalog service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	plan, err := s.store.GetUserPlan(ctx, userID)
	if err != nil {
		return CostBreakdown{}, err
	}
	if plan.CatalogVersion == nil {
		if err := s.RefreshIfStale(ctx); err != nil {
			return CostBreakdown{}, err
		}
	}
	engine, err := s.engineForVersion(ctx, plan.CatalogVersion)
	if err != nil {
		return CostBreakdown{}, err
	}
	if plan.RateCard == "" {
		return engine.Calculate(metrics)
	}
	return engine.Calculate(metrics, PricingOptions{RateCard: plan.RateCard})
}

// CalculateForLease prices settlement metrics from the catalog/plan snapshot
// captured when the lease was admitted. This prevents a mid-flight catalog or
// plan change from changing the final price.
func (s *CatalogService) CalculateForLease(ctx context.Context, userID, leaseID string, metrics UsageMetrics) (CostBreakdown, error) {
	if s == nil || s.store == nil {
		return CostBreakdown{}, NewError("catalog service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	pricingContext, err := s.store.GetLeasePricingContext(ctx, userID, leaseID)
	if err != nil {
		return CostBreakdown{}, err
	}
	if pricingContext == nil {
		return CostBreakdown{}, NewError("lease pricing context was not found", ErrorOptions{Code: ErrorCodeLeaseNotFound, Category: ErrorCategoryNotFound})
	}
	version := pricingContext.CatalogVersion
	engine, err := s.engineForVersion(ctx, &version)
	if err != nil {
		return CostBreakdown{}, err
	}
	rateCard := pricingContext.RateCard
	if rateCard == "" {
		rateCard, _ = engine.GetRateCardForPlan(pricingContext.PlanKey)
	}
	if rateCard == "" {
		return engine.Calculate(metrics)
	}
	return engine.Calculate(metrics, PricingOptions{RateCard: rateCard})
}

func (s *CatalogService) engineForVersion(ctx context.Context, version *int) (*PricingEngine, error) {
	if version == nil {
		return s.Engine()
	}
	s.mu.RLock()
	engine := s.versionEngines[*version]
	s.mu.RUnlock()
	if engine != nil {
		return engine, nil
	}
	revision, err := s.store.GetCatalogRevision(ctx, *version)
	if err != nil {
		return nil, err
	}
	if revision == nil {
		return nil, NewError("catalog revision is unavailable for the pinned plan", ErrorOptions{
			Code:     ErrorCodeCatalogNotLoaded,
			Category: ErrorCategoryNotFound,
			Details:  map[string]any{"catalog_version": *version},
		})
	}
	config, err := LoadConfigFromMap(revision.Config)
	if err != nil {
		return nil, err
	}
	engine, err = NewPricingEngine(config)
	if err != nil {
		return nil, err
	}
	s.mu.Lock()
	if cached := s.versionEngines[*version]; cached != nil {
		engine = cached
	} else {
		s.versionEngines[*version] = engine
	}
	s.mu.Unlock()
	return engine, nil
}

// PublishDraft validates config before atomically creating an inactive catalog
// revision in the shared PostgreSQL contract.
func (s *CatalogService) PublishDraft(ctx context.Context, config map[string]any, label string) (string, error) {
	if s == nil || s.store == nil {
		return "", NewError("catalog service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	if _, err := LoadConfigFromMap(config); err != nil {
		return "", err
	}
	return s.store.PublishCatalogDraft(ctx, config, label)
}

// Activate makes one existing revision active with the catalog-defined rollout
// policy, then invalidates the local cache.
func (s *CatalogService) Activate(ctx context.Context, version int, rollout CatalogRollout) (string, error) {
	if s == nil || s.store == nil {
		return "", NewError("catalog service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	result, err := s.store.ActivateCatalogRevision(ctx, version, rollout)
	if err != nil {
		return result, err
	}
	if err := s.Load(ctx); err != nil {
		return result, err
	}
	return result, nil
}

// PublishAndActivate validates and atomically publishes the next revision and
// rollout. It invalidates the cache only after a successful mutation.
func (s *CatalogService) PublishAndActivate(ctx context.Context, config map[string]any, label string, rollout CatalogRollout) (string, error) {
	if s == nil || s.store == nil {
		return "", NewError("catalog service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	if _, err := LoadConfigFromMap(config); err != nil {
		return "", err
	}
	result, err := s.store.PublishAndActivateCatalog(ctx, config, label, rollout)
	if err != nil {
		return result, err
	}
	if err := s.Load(ctx); err != nil {
		return result, err
	}
	return result, nil
}

// SetRevisionPin pins or unpins an existing subject assignment from automatic
// catalog rollout.
func (s *CatalogService) SetRevisionPin(ctx context.Context, userID string, pinned bool) (bool, error) {
	if s == nil || s.store == nil {
		return false, NewError("catalog service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	return s.store.SetPlanRevisionPin(ctx, userID, pinned)
}

// ApplyDueChanges applies a bounded batch of renewal-effective plan changes.
func (s *CatalogService) ApplyDueChanges(ctx context.Context, limit int) (int, error) {
	if s == nil || s.store == nil {
		return 0, NewError("catalog service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	limit, err := requireBoundedLimit(limit, defaultPageSize, maxMaintenanceBatchSize, "catalog change limit")
	if err != nil {
		return 0, err
	}
	return s.store.ApplyDuePlanChanges(ctx, limit)
}

func (s *CatalogService) install(revision *CatalogRevision) error {
	config, err := LoadConfigFromMap(revision.Config)
	if err != nil {
		return err
	}
	engine, err := NewPricingEngine(config)
	if err != nil {
		return err
	}
	s.mu.Lock()
	s.revision = revision
	s.config = config
	s.engine = engine
	s.versionEngines = make(map[int]*PricingEngine)
	s.versionEngines[revision.Version] = engine
	s.loadedAt = s.now().UTC()
	s.mu.Unlock()
	return nil
}
