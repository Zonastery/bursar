// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import (
	"context"
	"errors"
	"testing"
)

type catalogBoundaryStore struct {
	CreditStore
	active      *CatalogRevision
	activeErr   error
	plan        GetUserPlanResult
	planErr     error
	lease       *LeasePricingContext
	leaseErr    error
	revision    *CatalogRevision
	revisionErr error
	publishErr  error
	activateErr error
	publishedID string
	activatedID string
}

func (s *catalogBoundaryStore) GetActiveCatalog(context.Context) (*CatalogRevision, error) {
	return s.active, s.activeErr
}

func (s *catalogBoundaryStore) GetUserPlan(context.Context, string) (GetUserPlanResult, error) {
	return s.plan, s.planErr
}

func (s *catalogBoundaryStore) GetLeasePricingContext(context.Context, string, string) (*LeasePricingContext, error) {
	return s.lease, s.leaseErr
}

func (s *catalogBoundaryStore) GetCatalogRevision(context.Context, int) (*CatalogRevision, error) {
	return s.revision, s.revisionErr
}

func (s *catalogBoundaryStore) PublishCatalogDraft(context.Context, map[string]any, string) (string, error) {
	return s.publishedID, s.publishErr
}

func (s *catalogBoundaryStore) PublishAndActivateCatalog(context.Context, map[string]any, string, CatalogRollout) (string, error) {
	return s.publishedID, s.publishErr
}

func (s *catalogBoundaryStore) ActivateCatalogRevision(context.Context, int, CatalogRollout) (string, error) {
	return s.activatedID, s.activateErr
}

func TestCatalogBoundaryErrorsNeverUseStaleFinancialState(t *testing.T) {
	ctx := context.Background()
	storeError := errors.New("catalog store unavailable")
	store := &catalogBoundaryStore{activeErr: storeError}
	service, err := NewCatalogServiceWithOptions(store, CatalogServiceOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if err := service.RefreshIfStale(ctx); err != nil {
		t.Fatalf("zero-TTL refresh should remain disabled: %v", err)
	}
	if err := service.Load(ctx); !errors.Is(err, storeError) {
		t.Fatalf("load error = %v", err)
	}

	store.activeErr = nil
	if _, err := service.GetConfig(ctx); err == nil {
		t.Fatal("missing active catalog was accepted")
	}
	store.active = &CatalogRevision{ID: "invalid", Version: 1, Config: map[string]any{"version": 1}}
	if err := service.Load(ctx); err == nil {
		t.Fatal("invalid active catalog was installed")
	}

	valid := checkoutTestConfig(t)
	store.active = &CatalogRevision{ID: "active", Version: 2, Config: valid}
	if err := service.Load(ctx); err != nil {
		t.Fatal(err)
	}
	if err := service.Load(ctx); err != nil {
		t.Fatalf("same revision reload failed: %v", err)
	}

	store.planErr = storeError
	if _, err := service.CalculateForUser(ctx, "account-1", UsageMetrics{}); !errors.Is(err, storeError) {
		t.Fatalf("plan lookup error = %v", err)
	}
	store.planErr = nil
	store.leaseErr = storeError
	if _, err := service.CalculateForLease(ctx, "account-1", "lease-1", UsageMetrics{}); !errors.Is(err, storeError) {
		t.Fatalf("lease lookup error = %v", err)
	}
	store.leaseErr = nil
	store.lease = nil
	if _, err := service.CalculateForLease(ctx, "account-1", "lease-1", UsageMetrics{}); err == nil {
		t.Fatal("missing lease pricing snapshot was accepted")
	}

	version := 9
	store.revisionErr = storeError
	if _, err := service.engineForVersion(ctx, &version); !errors.Is(err, storeError) {
		t.Fatalf("historical catalog error = %v", err)
	}
	store.revisionErr = nil
	store.revision = &CatalogRevision{Version: version, Config: map[string]any{"version": 1}}
	if _, err := service.engineForVersion(ctx, &version); err == nil {
		t.Fatal("invalid historical catalog was cached")
	}

	if _, err := service.PublishDraft(ctx, map[string]any{"version": 1}, "invalid"); err == nil {
		t.Fatal("invalid draft reached storage")
	}
	store.publishErr = storeError
	if _, err := service.PublishDraft(ctx, valid, "draft"); !errors.Is(err, storeError) {
		t.Fatalf("draft storage error = %v", err)
	}
	if _, err := service.PublishAndActivate(ctx, valid, "active", CatalogRollout{}); !errors.Is(err, storeError) {
		t.Fatalf("publish storage error = %v", err)
	}
	store.publishErr = nil
	store.activateErr = storeError
	if _, err := service.Activate(ctx, 2, CatalogRollout{}); !errors.Is(err, storeError) {
		t.Fatalf("activate storage error = %v", err)
	}
}
