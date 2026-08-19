// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import (
	"context"
	"errors"
	"sync/atomic"
	"testing"
)

type accountCreatedStoreStub struct {
	CreditStore
	active       *CatalogRevision
	activeErrors []error
	plan         GetUserPlanResult
	planErrors   []error
	setErrors    []error
	grantErrors  []error
	activeCalls  atomic.Int32
	planCalls    atomic.Int32
	setCalls     atomic.Int32
	grantCalls   atomic.Int32
	grantRequest []ExecuteGrantProgramRequest
}

func (s *accountCreatedStoreStub) GetActiveCatalog(context.Context) (*CatalogRevision, error) {
	s.activeCalls.Add(1)
	if len(s.activeErrors) > 0 {
		err := s.activeErrors[0]
		s.activeErrors = s.activeErrors[1:]
		return nil, err
	}
	return s.active, nil
}

func (s *accountCreatedStoreStub) GetUserPlan(context.Context, string) (GetUserPlanResult, error) {
	s.planCalls.Add(1)
	if len(s.planErrors) > 0 {
		err := s.planErrors[0]
		s.planErrors = s.planErrors[1:]
		return GetUserPlanResult{}, err
	}
	return s.plan, nil
}

func (s *accountCreatedStoreStub) SetUserPlan(_ context.Context, userID, planKey string, _ SetUserPlanOptions) (SetUserPlanResult, error) {
	s.setCalls.Add(1)
	if len(s.setErrors) > 0 {
		err := s.setErrors[0]
		s.setErrors = s.setErrors[1:]
		return SetUserPlanResult{}, err
	}
	s.plan = GetUserPlanResult{UserID: userID, PlanKey: planKey}
	return SetUserPlanResult{UserID: userID, PlanKey: planKey}, nil
}

func (s *accountCreatedStoreStub) ExecuteGrantProgram(_ context.Context, request ExecuteGrantProgramRequest) ([]GrantProgramAwardResult, error) {
	s.grantCalls.Add(1)
	s.grantRequest = append(s.grantRequest, request)
	if len(s.grantErrors) > 0 {
		err := s.grantErrors[0]
		s.grantErrors = s.grantErrors[1:]
		return nil, err
	}
	return []GrantProgramAwardResult{{GrantAwardID: request.ProgramKey + "-award", Amount: MustAmount("5")}}, nil
}

func accountCreatedCatalog(t *testing.T) *CatalogRevision {
	t.Helper()
	defaultPlan := "starter"
	defaultBucket := "promotional"
	never := ExpiryPolicy{Type: "never"}
	config := &BursarConfig{
		Version: 1,
		Catalog: CatalogConfig{DefaultPlan: &defaultPlan},
		Credits: CreditsConfig{
			Buckets:       map[string]BucketDefinition{"promotional": {Priority: 1, Expiry: never}},
			DefaultBucket: &defaultBucket,
			Policies:      map[string]CreditPolicy{},
			GrantPrograms: map[string]GrantProgram{"welcome": {
				Trigger: "account_created", Awards: []GrantAward{{Recipient: "subject", Amount: MustAmount("5"), Bucket: "promotional"}},
				MaxAwardsPerSubject: 1, IdempotencyScope: "subject",
			}},
		},
		Entitlements: EntitlementsConfig{Features: map[string]FeatureDefinition{}},
		Admission:    AdmissionConfig{Policies: map[string]AdmissionPolicy{}},
		Plans:        map[string]PlanDefinition{"starter": {DisplayName: "Starter", Evolution: PlanEvolution{DefaultRollout: "immediate"}, Features: map[string]any{}, Quotas: map[string]QuotaDefinition{}}},
		Commerce:     CommerceConfig{Providers: map[string]ProviderDefinition{}, Offers: map[string]CommerceOffer{}, SubscriptionChanges: map[string]SubscriptionChangePolicy{}},
	}
	raw := CanonicalParsedBursarConfigDict(config)
	if _, err := LoadConfigFromMap(raw); err != nil {
		t.Fatalf("account test catalog is invalid: %v", err)
	}
	return &CatalogRevision{ID: "catalog-1", Version: 1, Config: raw}
}

func accountCreatedServices(t *testing.T, store *accountCreatedStoreStub) *AccountService {
	t.Helper()
	credits, err := NewCreditsService(store, CreditsServiceOptions{})
	if err != nil {
		t.Fatalf("NewCreditsService() error = %v", err)
	}
	catalog, err := NewCatalogService(store)
	if err != nil {
		t.Fatalf("NewCatalogService() error = %v", err)
	}
	accounts, err := NewAccountService(credits, catalog)
	if err != nil {
		t.Fatalf("NewAccountService() error = %v", err)
	}
	return accounts
}

func TestOnAccountCreatedRetriesReplaySafeSteps(t *testing.T) {
	retryable := NewStoreUnavailableError("temporary database failure", errors.New("transport"))
	store := &accountCreatedStoreStub{
		active:       accountCreatedCatalog(t),
		activeErrors: []error{retryable},
		planErrors:   []error{retryable},
		setErrors:    []error{retryable},
		grantErrors:  []error{retryable},
	}
	accounts := accountCreatedServices(t, store)

	result, err := accounts.OnAccountCreated(context.Background(), AccountCreatedInput{
		AccountID: "account-1", EventKey: "signup-event-1", Region: "US", Metadata: CreditMetadata{"source": "signup"},
	})
	if err != nil {
		t.Fatalf("OnAccountCreated() error = %v", err)
	}
	if result.PlanKey != "starter" || !result.PlanAssigned || len(result.Grants) != 1 {
		t.Fatalf("result = %#v", result)
	}
	if store.setCalls.Load() != 2 {
		t.Fatalf("set plan calls = %d, want 2", store.setCalls.Load())
	}
	if store.activeCalls.Load() != 2 || store.planCalls.Load() != 2 {
		t.Fatalf("read calls = active %d plan %d, want 2 each", store.activeCalls.Load(), store.planCalls.Load())
	}
	if store.grantCalls.Load() != 2 {
		t.Fatalf("grant calls = %d, want 2", store.grantCalls.Load())
	}
	if len(store.grantRequest) != 2 || store.grantRequest[0].EventKey != "signup-event-1" || store.grantRequest[1].EventKey != "signup-event-1" {
		t.Fatalf("grant requests did not preserve event key: %#v", store.grantRequest)
	}
}

func TestOnAccountCreatedDoesNotRetryNonRetryableCatalogRead(t *testing.T) {
	operationErr := NewError("catalog conflict", ErrorOptions{Code: ErrorCodeCheckoutConflict, Category: ErrorCategoryConflict})
	store := &accountCreatedStoreStub{activeErrors: []error{operationErr}}
	accounts := accountCreatedServices(t, store)

	_, err := accounts.OnAccountCreated(context.Background(), AccountCreatedInput{AccountID: "account-1", EventKey: "event-1"})
	if err != operationErr {
		t.Fatalf("error = %v, want original non-retryable error", err)
	}
	if store.activeCalls.Load() != 1 {
		t.Fatalf("catalog attempts = %d, want 1", store.activeCalls.Load())
	}
	if store.setCalls.Load() != 0 || store.grantCalls.Load() != 0 {
		t.Fatalf("mutations reached after catalog failure: set=%d grants=%d", store.setCalls.Load(), store.grantCalls.Load())
	}
}

func TestOnAccountCreatedLeavesExistingPlanUnchanged(t *testing.T) {
	store := &accountCreatedStoreStub{
		active: accountCreatedCatalog(t),
		plan:   GetUserPlanResult{UserID: "account-1", PlanKey: "existing"},
	}
	accounts := accountCreatedServices(t, store)

	result, err := accounts.OnAccountCreated(context.Background(), AccountCreatedInput{AccountID: "account-1", EventKey: "event-1"})
	if err != nil {
		t.Fatalf("OnAccountCreated() error = %v", err)
	}
	if result.PlanKey != "existing" || result.PlanAssigned {
		t.Fatalf("existing plan changed: %#v", result)
	}
	if store.setCalls.Load() != 0 {
		t.Fatalf("set plan calls = %d, want 0", store.setCalls.Load())
	}
}

func TestNewAccountServiceRejectsMissingDependencies(t *testing.T) {
	if service, err := NewAccountService(nil, nil); service != nil || err == nil {
		t.Fatalf("NewAccountService(nil, nil) = service:%v error:%v, want typed error", service, err)
	}
	if service, err := NewAccountService(&CreditsService{}, nil); service != nil || err == nil {
		t.Fatalf("NewAccountService(credits, nil) = service:%v error:%v, want typed error", service, err)
	}
}

func TestOnAccountCreatedRejectsNilServiceAndInvalidIdentity(t *testing.T) {
	var service *AccountService
	if _, err := service.OnAccountCreated(context.Background(), AccountCreatedInput{}); err == nil {
		t.Fatal("nil AccountService accepted an account-created event")
	}

	store := &accountCreatedStoreStub{active: accountCreatedCatalog(t)}
	accounts := accountCreatedServices(t, store)
	if _, err := accounts.OnAccountCreated(context.Background(), AccountCreatedInput{EventKey: "event-1"}); err == nil {
		t.Fatal("empty account ID was accepted")
	}
	if _, err := accounts.OnAccountCreated(context.Background(), AccountCreatedInput{AccountID: "account-1"}); err == nil {
		t.Fatal("empty event key was accepted")
	}
	if store.activeCalls.Load() != 0 {
		t.Fatalf("catalog read calls = %d, want no calls after identity validation", store.activeCalls.Load())
	}
}

func TestOnAccountCreatedRequiresCatalogDefaultPlan(t *testing.T) {
	catalog := accountCreatedCatalog(t)
	delete(catalog.Config["catalog"].(map[string]any), "default_plan")
	catalog.Config["plans"] = map[string]any{}
	store := &accountCreatedStoreStub{active: catalog}
	accounts := accountCreatedServices(t, store)
	if _, err := accounts.OnAccountCreated(context.Background(), AccountCreatedInput{AccountID: "account-1", EventKey: "event-1"}); err == nil {
		t.Fatal("account-created flow accepted a catalog without a default plan")
	}
}
