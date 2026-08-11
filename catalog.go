// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import (
	"context"
	"sync"
)

// CatalogService owns process-local parsing and pricing-engine caching around
// the database-authoritative catalog revision. Catalog writes remain atomic
// database RPCs exposed through CreditStore.
type CatalogService struct {
	store CreditStore

	mu       sync.RWMutex
	revision *CatalogRevision
	config   *BursarConfig
	engine   *PricingEngine
}

// NewCatalogService constructs the catalog capability for a durable store.
func NewCatalogService(store CreditStore) (*CatalogService, error) {
	if store == nil {
		return nil, NewError("catalog requires a credit store", ErrorOptions{Code: ErrorCodeConfig, Category: ErrorCategoryInvalidRequest})
	}
	return &CatalogService{store: store}, nil
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
	return s.install(revision)
}

// Refresh reloads only when the active persisted revision differs from the
// cached revision. Concurrent readers always observe an all-or-nothing engine.
func (s *CatalogService) Refresh(ctx context.Context) error {
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
	loaded := s.revision != nil && s.revision.ID == revision.ID && s.revision.Version == revision.Version
	s.mu.RUnlock()
	if loaded {
		return nil
	}
	return s.install(revision)
}

// Invalidate clears the in-process cache. It never alters the persisted
// catalog; the next Load or Refresh obtains the active revision from storage.
func (s *CatalogService) Invalidate() {
	if s == nil {
		return
	}
	s.mu.Lock()
	s.revision, s.config, s.engine = nil, nil, nil
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
	if err == nil {
		s.Invalidate()
	}
	return result, err
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
	if err == nil {
		s.Invalidate()
	}
	return result, err
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
	s.mu.Unlock()
	return nil
}
