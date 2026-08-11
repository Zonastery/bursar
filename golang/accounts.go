// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import (
	"context"
	"sort"
)

// AccountCreatedInput contains the stable application event identity for a
// newly created account. EventKey must remain the same across retries so grant
// programs cannot award credits twice.
type AccountCreatedInput struct {
	AccountID string
	EventKey  string
	Region    string
	Metadata  CreditMetadata
}

// AccountCreatedResult records the default plan assignment and grant awards
// resulting from a durable account-created event.
type AccountCreatedResult struct {
	AccountID    string
	PlanKey      string
	PlanAssigned bool
	Grants       []GrantProgramAwardResult
}

// AccountService performs financial account lifecycle actions which compose
// the active catalog and atomic CreditStore methods.
type AccountService struct {
	credits *CreditsService
	catalog *CatalogService
}

// NewAccountService constructs the account lifecycle capability.
func NewAccountService(credits *CreditsService, catalog *CatalogService) (*AccountService, error) {
	if credits == nil || catalog == nil {
		return nil, NewError("accounts require credits and catalog services", ErrorOptions{Code: ErrorCodeConfig, Category: ErrorCategoryInvalidRequest})
	}
	return &AccountService{credits: credits, catalog: catalog}, nil
}

// OnAccountCreated assigns the active catalog's default plan only when the
// account has no plan, then runs each account_created grant program in stable
// key order. All grants carry the caller's durable event key.
func (s *AccountService) OnAccountCreated(ctx context.Context, input AccountCreatedInput) (AccountCreatedResult, error) {
	if s == nil || s.credits == nil || s.catalog == nil {
		return AccountCreatedResult{}, NewError("account service is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	accountID, err := requireText(input.AccountID, "account ID")
	if err != nil {
		return AccountCreatedResult{}, err
	}
	eventKey, err := requireStableKey(input.EventKey, "account-created event key")
	if err != nil {
		return AccountCreatedResult{}, err
	}
	config, err := s.catalog.GetConfig(ctx)
	if err != nil {
		return AccountCreatedResult{}, err
	}
	if config.Catalog.DefaultPlan == nil || *config.Catalog.DefaultPlan == "" {
		return AccountCreatedResult{}, NewError("active catalog has no default account plan", ErrorOptions{Code: ErrorCodeConfig, Category: ErrorCategoryInvalidRequest})
	}

	store := s.credits.Store()
	current, err := store.GetUserPlan(ctx, accountID)
	if err != nil {
		return AccountCreatedResult{}, err
	}
	planKey := current.PlanKey
	planAssigned := planKey == ""
	if planAssigned {
		if _, err := s.credits.SetUserPlan(ctx, accountID, *config.Catalog.DefaultPlan, SetUserPlanOptions{}); err != nil {
			return AccountCreatedResult{}, err
		}
		planKey = *config.Catalog.DefaultPlan
	}

	programKeys := make([]string, 0, len(config.Credits.GrantPrograms))
	for key := range config.Credits.GrantPrograms {
		programKeys = append(programKeys, key)
	}
	sort.Strings(programKeys)
	grants := make([]GrantProgramAwardResult, 0)
	for _, programKey := range programKeys {
		program := config.Credits.GrantPrograms[programKey]
		if program.Trigger != "account_created" {
			continue
		}
		awards, err := store.ExecuteGrantProgram(ctx, ExecuteGrantProgramRequest{
			Trigger:    "account_created",
			ProgramKey: programKey,
			SubjectID:  accountID,
			EventKey:   eventKey,
			Region:     input.Region,
			Metadata:   input.Metadata.Clone(),
		})
		if err != nil {
			return AccountCreatedResult{}, err
		}
		grants = append(grants, awards...)
	}
	return AccountCreatedResult{AccountID: accountID, PlanKey: planKey, PlanAssigned: planAssigned, Grants: grants}, nil
}
