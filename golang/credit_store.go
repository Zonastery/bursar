package bursar

import (
	"context"
	"strings"
	"time"
)

const (
	defaultLeaseTTL         = 10 * time.Minute
	defaultPageSize         = 50
	maxPageSize             = 500
	maxLedgerPageSize       = 200
	maxMaintenanceBatchSize = 1000
	maxIdempotencyKeyLength = 255
)

// CreditStore is the portable persistence contract used by CreditsService.
//
// Implementations must make every balance-changing operation atomic and
// idempotent at the storage boundary. PostgresStore is the production
// implementation; custom stores should preserve the same error/result
// semantics rather than emulating accounting state in process memory.
type CreditStore interface {
	Close() error

	// Runtime accounting.
	GetBalance(ctx context.Context, userID string) (BalanceResult, error)
	AddCredits(ctx context.Context, userID string, amount Amount, options AddCreditsOptions) (AddCreditsResult, error)
	DeductWithAllowance(ctx context.Context, userID string, amount Amount, options DeductWithAllowanceOptions) (DeductionResult, error)
	RecordUsage(ctx context.Context, userID, operation string, requested Amount, options RecordUsageOptions) (UsageRecordResult, error)

	// Atomic admission / lease lifecycle.
	CreateLease(ctx context.Context, userID string, amount Amount, operationType string, options CreateLeaseOptions) (LeaseResult, error)
	SettleLease(ctx context.Context, userID, leaseID string, amount Amount, options SettleLeaseOptions) (DeductionResult, error)
	GetLeasePricingContext(ctx context.Context, userID, leaseID string) (*LeasePricingContext, error)
	ReleaseLease(ctx context.Context, userID, leaseID string) (ReleaseResult, error)
	RenewLease(ctx context.Context, userID, leaseID string, ttl time.Duration) (LeaseResult, error)
	ExpireLeases(ctx context.Context, limit int) (int, error)
	GetAvailable(ctx context.Context, userID string) (AvailableResult, error)
	GetBucketBalances(ctx context.Context, userID string) (BucketBalancesResult, error)
	ExecuteGrantProgram(ctx context.Context, request ExecuteGrantProgramRequest) ([]GrantProgramAwardResult, error)

	// Catalog management.
	GetActiveCatalog(ctx context.Context) (*CatalogRevision, error)
	PublishAndActivateCatalog(ctx context.Context, config map[string]any, label string, rollout CatalogRollout) (string, error)
	GetCatalogHistory(ctx context.Context) ([]CatalogRevisionSummary, error)
	GetCatalogRevision(ctx context.Context, version int) (*CatalogRevision, error)
	ActivateCatalogRevision(ctx context.Context, version int, rollout CatalogRollout) (string, error)
	PublishCatalogDraft(ctx context.Context, config map[string]any, label string) (string, error)

	// Plans, entitlements, and quotas.
	GetUserPlan(ctx context.Context, userID string) (GetUserPlanResult, error)
	CheckFeature(ctx context.Context, userID, feature string) (CheckFeatureResult, error)
	SetUserPlan(ctx context.Context, userID, planKey string, options SetUserPlanOptions) (SetUserPlanResult, error)
	UnsetUserPlan(ctx context.Context, userID string) (UnsetUserPlanResult, error)
	SetPlanRevisionPin(ctx context.Context, userID string, pinned bool) (bool, error)
	ApplyDuePlanChanges(ctx context.Context, limit int) (int, error)
	StartPlanMigration(ctx context.Context, fromPlanID, toPlanID string) (PlanMigrationStartResult, error)
	MigratePlanBatch(ctx context.Context, migrationID string, batchSize int) (PlanMigrationBatchResult, error)
	GetQuotaState(ctx context.Context, userID, quotaKey string) ([]QuotaState, error)
	CheckAllowance(ctx context.Context, userID string) (*AllowanceResult, error)
	ListQuotaEvents(ctx context.Context, userID string, options ListQuotaEventsOptions) ([]QuotaEvent, error)

	// Credit correction and expiry.
	RefundCredits(ctx context.Context, entryID string, amount *Amount, reason string, metadata CreditMetadata, idempotencyKey string) (RefundResult, error)
	SweepExpiredCredits(ctx context.Context, dryRun bool, userID string, limit int) (SweepResult, error)
	RevokeCreditsByEntryType(ctx context.Context, userID, entryType string) (RevokeCreditsResult, error)

	// Read-only reporting.
	SpendByUser(ctx context.Context, start, end time.Time) ([]SpendByUserRow, error)
	SpendByModel(ctx context.Context, start, end time.Time) ([]SpendByModelRow, error)
	TopUsers(ctx context.Context, limit int, start, end time.Time) ([]TopUserRow, error)
	DailySpend(ctx context.Context, start, end time.Time) ([]DailySpendRow, error)
	AggregateStats(ctx context.Context, start, end time.Time) (AggregateStats, error)
	ListLedgerEntries(ctx context.Context, userID string, options ListLedgerEntriesOptions) (LedgerPage, error)
	ListUsageEntries(ctx context.Context, userID string, options ListLedgerEntriesOptions) (LedgerPage, error)
	ListUsageCharges(ctx context.Context, userID string, options ListUsageChargesOptions) (UsageChargePage, error)
	GetLedgerEntry(ctx context.Context, userID, entryID string) (*LedgerEntry, error)

	// Shared team balance pools.
	CreateTeam(ctx context.Context, ownerUserID, name string, options CreateTeamOptions) (CreateTeamResult, error)
	GetTeamBalance(ctx context.Context, teamID string) (*TeamBalanceResult, error)
	AddTeamMember(ctx context.Context, teamID, userID string, options AddTeamMemberOptions) (AddTeamMemberResult, error)
	GetTeamMembers(ctx context.Context, teamID string) ([]TeamMember, error)
	RemoveTeamMember(ctx context.Context, teamID, userID string) (bool, error)
	DeductTeam(ctx context.Context, teamID, userID string, amount Amount, options TeamDeductionOptions) (TeamDeductionResult, error)
}

func requireText(value, field string) (string, error) {
	trimmed := strings.TrimSpace(value)
	if trimmed == "" {
		return "", errorf(ErrorCodeConfig, ErrorCategoryInvalidRequest, "%s must not be empty", field)
	}
	return trimmed, nil
}

func requireStableKey(value, field string) (string, error) {
	trimmed, err := requireText(value, field)
	if err != nil {
		return "", err
	}
	if len(trimmed) > maxIdempotencyKeyLength {
		return "", errorf(
			ErrorCodeConfig,
			ErrorCategoryInvalidRequest,
			"%s must be at most %d characters",
			field,
			maxIdempotencyKeyLength,
		)
	}
	return trimmed, nil
}

func requirePositiveDuration(value time.Duration, field string) (time.Duration, error) {
	if value <= 0 {
		return 0, errorf(ErrorCodeConfig, ErrorCategoryInvalidRequest, "%s must be positive", field)
	}
	if value%time.Second != 0 {
		return 0, errorf(ErrorCodeConfig, ErrorCategoryInvalidRequest, "%s must be an exact number of seconds", field)
	}
	return value, nil
}

func requireBoundedLimit(value, fallback, maximum int, field string) (int, error) {
	if value == 0 {
		return fallback, nil
	}
	if value < 1 || value > maximum {
		return 0, errorf(ErrorCodeConfig, ErrorCategoryInvalidRequest, "%s must be between 1 and %d", field, maximum)
	}
	return value, nil
}

func requireBillingMode(mode BillingMode) (BillingMode, error) {
	if mode == "" {
		return BillingModeStrict, nil
	}
	if mode != BillingModeStrict && mode != BillingModeOverdraft {
		return "", errorf(ErrorCodeConfig, ErrorCategoryInvalidRequest, "billing mode must be %q or %q", BillingModeStrict, BillingModeOverdraft)
	}
	return mode, nil
}

func timePointer(value time.Time) *time.Time {
	return &value
}

func creditAmountPointer(value Amount) *Amount {
	return &value
}
