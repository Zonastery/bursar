package bursar

import (
	"context"
	"time"
)

// CreditMetadata is application-supplied context persisted with a credit
// operation. It intentionally accepts additional fields so integrations can
// attach provider, tracing, and domain-specific identifiers without the SDK
// discarding them.
type CreditMetadata map[string]any

// Clone returns an independent copy suitable for adding SDK-owned fields.
func (m CreditMetadata) Clone() CreditMetadata {
	if m == nil {
		return CreditMetadata{}
	}
	clone := make(CreditMetadata, len(m))
	for key, value := range m {
		clone[key] = value
	}
	return clone
}

// BillingMode controls the balance floor enforced while admitting an
// operation. The database remains the final authority for the policy.
type BillingMode string

const (
	BillingModeStrict    BillingMode = "strict"
	BillingModeOverdraft BillingMode = "overdraft"
)

// BalanceResult is the committed balance and cumulative purchased credits for
// one subject.
type BalanceResult struct {
	UserID            string
	Balance           Amount
	LifetimePurchased Amount
}

// AddCreditsOptions controls an idempotent credit grant or adjustment.
type AddCreditsOptions struct {
	Type           string
	Metadata       CreditMetadata
	ExpiresAt      *time.Time
	Bucket         string
	IdempotencyKey string
}

// AddCreditsResult is the committed outcome of AddCredits.
type AddCreditsResult struct {
	EntryID           string
	UserID            string
	Amount            Amount
	NewBalance        Amount
	LifetimePurchased Amount
	Bucket            string
	Idempotent        bool
}

// AvailableResult is an advisory balance snapshot. It must not be used as an
// admission gate; CreateLease is the atomic admission primitive.
type AvailableResult struct {
	UserID    string
	Balance   Amount
	Reserved  Amount
	Available Amount
}

// DeductionResult is returned by both immediate charges and lease settlement.
// A non-empty ErrorCode is a database business outcome; transport and storage
// failures are returned as Go errors.
type DeductionResult struct {
	EntryID           string
	UsageChargeID     string
	UserID            string
	Amount            Amount
	BalanceAfter      *Amount
	AllowanceConsumed Amount
	Idempotent        bool
	ErrorCode         string
	BucketBreakdown   map[string]Amount
}

// PostDeductionSource identifies the durable operation that committed a
// debit. Hooks run only after a non-idempotent successful commit.
type PostDeductionSource string

const (
	PostDeductionSourceRaw    PostDeductionSource = "raw"
	PostDeductionSourceDeduct PostDeductionSource = "deduct"
	PostDeductionSourceSettle PostDeductionSource = "settle"
)

// PostDeductionContext is delivered to a best-effort hook after a credit
// debit commits. Its deduction is a defensive snapshot; changing it cannot
// alter persisted accounting or the result returned to the caller.
type PostDeductionContext struct {
	UserID    string
	Source    PostDeductionSource
	Deduction DeductionResult
}

// PostDeductionHook is a post-commit extension point for work such as
// auto-recharge evaluation. Returned errors and panics are isolated: a hook
// can never change an already-committed deduction result.
type PostDeductionHook func(context.Context, PostDeductionContext) error

// RefundResult is the result of an idempotent refund request.
type RefundResult struct {
	RefundEntryID   string
	OriginalEntryID string
	UserID          string
	Amount          *Amount
	NewBalance      *Amount
	ErrorCode       string
	BucketBreakdown map[string]Amount
}

// RevokeCreditsResult describes credits removed from lots created by one entry
// type, normally during a subscription-cycle replacement.
type RevokeCreditsResult struct {
	UserID       string
	EntryType    string
	Revoked      Amount
	BalanceAfter Amount
}

// OperationUsageOptions is shared by usage, lease, and settlement operations.
// Measures are exact credit-related quantities. Dimensions are untyped labels
// evaluated by catalog policy and retained on usage receipts.
type OperationUsageOptions struct {
	Feature    string
	Model      string
	Region     string
	Measures   map[string]Amount
	Dimensions map[string]any
}

// DeductWithAllowanceOptions controls a plan-aware immediate usage charge.
type DeductWithAllowanceOptions struct {
	OperationUsageOptions
	IdempotencyKey string
	Operation      string
	Metadata       CreditMetadata
}

// RecordUsageOptions controls a priced usage receipt that does not debit a
// credit balance again.
type RecordUsageOptions struct {
	OperationUsageOptions
	IdempotencyKey string
	Metadata       CreditMetadata
}

// UsageRecordResult is the result of a record-only usage receipt.
type UsageRecordResult struct {
	UsageID    string
	UserID     string
	Requested  Amount
	Idempotent bool
	ErrorCode  string
}

// CreateLeaseOptions controls an atomic credit reservation.
type CreateLeaseOptions struct {
	OperationUsageOptions
	IdempotencyKey string
	BillingMode    BillingMode
	Floor          Amount
	MaxConcurrent  *int
	OverdraftFloor *Amount
	TTL            time.Duration
	Metadata       CreditMetadata
}

// SettleLeaseOptions controls an idempotent settlement of an existing lease.
type SettleLeaseOptions struct {
	OperationUsageOptions
	IdempotencyKey string
	Metadata       CreditMetadata
}

// LeaseResult is the outcome of an admission or renewal request. ErrorCode is
// populated for denied or finalized leases without exposing a mutable lease ID.
type LeaseResult struct {
	LeaseID        string
	UserID         string
	Amount         *Amount
	Available      Amount
	ReservedTotal  Amount
	MinimumBalance *Amount
	BillingMode    BillingMode
	ExpiresAt      *time.Time
	ErrorCode      string
}

// LeasePricingContext is the immutable catalog/plan snapshot captured at
// admission. Metered settlement must price from this context, not a later plan.
type LeasePricingContext struct {
	CatalogVersion int
	PlanID         string
	PlanKey        string
	RateCard       string
}

// ReleaseResult describes the idempotent release of a reservation.
type ReleaseResult struct {
	LeaseID  string
	UserID   string
	Released bool
	Reason   string
}

// BucketBalance is the committed balance in one configured credit bucket.
type BucketBalance struct {
	BucketKey string
	Label     string
	Priority  int
	Expires   bool
	Balance   Amount
}

// BucketBalancesResult provides a stable shape even when no explicit buckets
// are configured (the database returns a synthetic default bucket).
type BucketBalancesResult struct {
	UserID       string
	Buckets      []BucketBalance
	TotalBalance Amount
}

// CanAffordResult is an advisory UI result only. Reserve/CreateLease remains
// the authoritative concurrency-safe admission path.
type CanAffordResult struct {
	Affordable bool
	Spendable  Amount
	WorstCase  Amount
	Reason     string
}

// AllowanceResult describes the database-owned active plan allowance window.
type AllowanceResult struct {
	PlanID             string
	AllowanceRemaining Amount
	PeriodStart        time.Time
	PeriodEnd          time.Time
}

// CatalogRevision is a catalog document and its monotonic version number.
type CatalogRevision struct {
	ID      string
	Config  map[string]any
	Version int
}

// CatalogRevisionSummary is a lightweight catalog-history row.
type CatalogRevisionSummary struct {
	ID        string
	Version   int
	Label     string
	Active    bool
	CreatedAt time.Time
}

// Entitlement preserves the arbitrary catalog value associated with a feature.
type Entitlement struct {
	Value any
}

// PlanAllowancePolicy mirrors a catalog allowance reset policy.
type PlanAllowancePolicy struct {
	Amount        Amount
	Priority      int
	ResetUnit     string
	ResetCount    int
	ResetAnchor   string
	ResetTimezone string
}

// PlanCreditPolicy controls prepaid versus credit-line behavior.
type PlanCreditPolicy struct {
	Type        string
	CreditLimit *Amount
}

// PlanAdmissionOperation configures the in-flight limit for an operation.
type PlanAdmissionOperation struct {
	MaxInFlight *int
}

// PlanAdmissionPolicy configures global and per-operation admission limits.
type PlanAdmissionPolicy struct {
	MaxInFlight *int
	Operations  map[string]PlanAdmissionOperation
}

// GetUserPlanResult is the database projection of the subject's effective
// plan, including catalog version pinning and entitlements.
type GetUserPlanResult struct {
	UserID                string
	PlanID                string
	PlanKey               string
	PlanLabel             string
	Allowance             *PlanAllowancePolicy
	Entitlements          map[string]Entitlement
	RateCard              string
	CreditPolicy          *PlanCreditPolicy
	Admission             *PlanAdmissionPolicy
	AllowedOperations     []string
	PlanAssignedAt        *time.Time
	PlanAssignmentEndsAt  *time.Time
	AssignmentSourceType  string
	AssignmentSourceID    string
	CatalogRevisionPinned bool
	CatalogVersion        *int
}

// CheckFeatureResult is a non-mutating entitlement check. A present numeric
// zero and an empty string are intentionally considered present, like the JS
// and Python SDKs.
type CheckFeatureResult struct {
	UserID     string
	Feature    string
	Value      any
	HasFeature bool
}

// SetUserPlanOptions controls the effective timestamp of a plan assignment.
type SetUserPlanOptions struct {
	PlanAssignedAt *time.Time
}

// SetUserPlanResult is the committed plan assignment.
type SetUserPlanResult struct {
	UserID          string
	PlanID          string
	PlanKey         string
	PlanAssignedAt  time.Time
	AssignmentState string
}

// UnsetUserPlanResult confirms removal of a subject plan assignment.
type UnsetUserPlanResult struct {
	UserID string
}

// PlanMigrationStartResult identifies a resumable catalog migration.
type PlanMigrationStartResult struct {
	MigrationID string
}

// PlanMigrationBatchResult is a bounded migration progress report.
type PlanMigrationBatchResult struct {
	Migrated   int
	Done       bool
	NextCursor string
}

// QuotaState is the current database-owned state for one quota window.
type QuotaState struct {
	UserID        string
	QuotaKey      string
	Operation     string
	Measure       string
	Limit         Amount
	Consumed      Amount
	Reserved      Amount
	Remaining     Amount
	Overage       Amount
	Enforcement   string
	WindowStart   time.Time
	WindowEnd     time.Time
	EmitAtPercent []float64
}

// QuotaEvent is a persisted threshold or blocking event.
type QuotaEvent struct {
	EventID          string
	QuotaKey         string
	Operation        string
	Measure          string
	EventType        string
	ThresholdPercent *float64
	IdempotencyKey   string
	UsageChargeID    string
	CreatedAt        time.Time
}

// ListQuotaEventsOptions supports the stable event cursor accepted by the RPC.
type ListQuotaEventsOptions struct {
	After          *time.Time
	AfterID        string
	Limit          int
	IdempotencyKey string
}

// SweepResult describes a bounded credit-lot expiry pass.
type SweepResult struct {
	ExpiredCount    int
	ExpiredAmount   Amount
	DryRun          bool
	ExpiredByBucket map[string]Amount
}

// ExecuteGrantProgramRequest is a durable grant-program trigger.
type ExecuteGrantProgramRequest struct {
	Trigger           string
	ProgramKey        string
	SubjectID         string
	EventKey          string
	ReferrerSubjectID string
	Region            string
	Metadata          CreditMetadata
}

// GrantProgramAwardResult is one awarded recipient from a grant program.
type GrantProgramAwardResult struct {
	GrantEventID       string
	GrantAwardID       string
	RecipientSubjectID string
	LedgerEntryID      string
	Amount             Amount
	Idempotent         bool
	ErrorCode          string
}

// LedgerEntry is an immutable accounting ledger row.
type LedgerEntry struct {
	EntryID          string
	AccountID        string
	ActorUserID      string
	Amount           Amount
	EntryType        string
	Operation        string
	ReferenceEntryID string
	IdempotencyKey   string
	Metadata         CreditMetadata
	CreatedAt        time.Time
}

// LedgerCursor is the stable composite cursor used to page ledger rows.
type LedgerCursor struct {
	CreatedAt time.Time
	EntryID   string
}

// LedgerPage is a page of immutable credit ledger entries.
type LedgerPage struct {
	Items      []LedgerEntry
	NextCursor *LedgerCursor
}

// ListLedgerEntriesOptions controls a stable ledger page.
type ListLedgerEntriesOptions struct {
	EntryTypes []string
	From       *time.Time
	To         *time.Time
	Limit      int
	Cursor     *LedgerCursor
}

// UsageCharge is the canonical priced usage receipt, including allowance-only
// and record-only charges.
type UsageCharge struct {
	UsageID            string
	AccountID          string
	Operation          string
	Requested          Amount
	Charged            Amount
	AllowanceRequested Amount
	AllowanceCovered   Amount
	BillingDisposition string
	Feature            string
	Model              string
	Region             string
	EventAt            time.Time
	IdempotencyKey     string
	Metadata           CreditMetadata
	CreatedAt          time.Time
}

// UsageChargeCursor is the stable composite cursor used to page usage receipts.
type UsageChargeCursor struct {
	EventAt time.Time
	UsageID string
}

// UsageChargePage is a page of usage receipts.
type UsageChargePage struct {
	Items      []UsageCharge
	NextCursor *UsageChargeCursor
}

// ListUsageChargesOptions controls a stable usage-receipt page.
type ListUsageChargesOptions struct {
	From              *time.Time
	To                *time.Time
	Limit             int
	Cursor            *UsageChargeCursor
	IncludeRecordOnly *bool
}

// SpendByUserRow is a credit-spend aggregate for one subject.
type SpendByUserRow struct {
	UserID     string
	TotalSpend Amount
	EntryCount int
}

// SpendByModelRow is a credit-spend aggregate for one model.
type SpendByModelRow struct {
	Model      string
	TotalSpend Amount
	EntryCount int
}

// TopUserRow is an abbreviated subject-spend aggregate.
type TopUserRow struct {
	UserID     string
	TotalSpend Amount
}

// DailySpendRow is a daily spend aggregate.
type DailySpendRow struct {
	Date       time.Time
	TotalSpend Amount
	EntryCount int
}

// AggregateStats summarizes usage across a time range.
type AggregateStats struct {
	TotalCreditsConsumed Amount
	ActiveUsers          int
	AverageDailySpend    Amount
	TopModel             string
	TopUser              string
}

// TeamRole controls a member's access to a shared credit balance.
type TeamRole string

const (
	TeamRoleOwner  TeamRole = "owner"
	TeamRoleAdmin  TeamRole = "admin"
	TeamRoleMember TeamRole = "member"
)

// CreateTeamOptions controls an idempotent shared-balance creation.
type CreateTeamOptions struct {
	IdempotencyKey string
	InitialBalance Amount
}

// CreateTeamResult identifies a created or replayed team.
type CreateTeamResult struct {
	TeamID     string
	Name       string
	Idempotent bool
}

// TeamBalanceResult is the current shared balance projection.
type TeamBalanceResult struct {
	TeamID      string
	Name        string
	Balance     Amount
	MemberCount int
}

// AddTeamMemberOptions configures a member role and optional spend cap.
type AddTeamMemberOptions struct {
	Role     TeamRole
	SpendCap *Amount
}

// AddTeamMemberResult confirms a membership write.
type AddTeamMemberResult struct {
	TeamID string
	UserID string
	Role   TeamRole
}

// TeamMember is a shared-balance membership projection.
type TeamMember struct {
	UserID     string
	Role       TeamRole
	SpendCap   *Amount
	TotalSpent Amount
}

// TeamDeductionOptions controls a shared-balance usage debit.
type TeamDeductionOptions struct {
	IdempotencyKey string
	Operation      string
	Metadata       CreditMetadata
}

// TeamDeductionResult is a shared-balance debit result.
type TeamDeductionResult struct {
	EntryID          string
	TeamID           string
	UserID           string
	Amount           Amount
	TeamBalanceAfter *Amount
	Idempotent       bool
	ErrorCode        string
}

// CreditPolicyPreset supplies service-level defaults when an operation does
// not explicitly choose a billing mode. The database still enforces catalog
// policy and may return a stricter business denial.
type CreditPolicyPreset string

const (
	CreditPolicyStrictPrepaid CreditPolicyPreset = "strict_prepaid"
	CreditPolicyOverdraft     CreditPolicyPreset = "overdraft"
)

// CreditsServiceOptions controls portable orchestration above CreditStore.
// It deliberately contains no framework hooks or CLI settings.
type CreditsServiceOptions struct {
	Policy          CreditPolicyPreset
	OverdraftFloor  *Amount
	MaxConcurrent   *int
	DefaultLeaseTTL time.Duration
	EventSink       CreditEventSink
	LowBalance      []Amount
	PostDeduction   PostDeductionHook
}

// ReserveOptions controls the service-level reserve shortcut.
type ReserveOptions struct {
	OperationUsageOptions
	IdempotencyKey string
	OperationType  string
	BillingMode    BillingMode
	TTL            time.Duration
	Metadata       CreditMetadata
}

// SettleOptions controls a service-level lease settlement.
type SettleOptions struct {
	OperationUsageOptions
	IdempotencyKey string
	Metadata       CreditMetadata
}

// CanAffordOptions controls the advisory affordability check.
type CanAffordOptions struct {
	Feature       string
	BillingMode   BillingMode
	OperationType string
}

// GrantSubscriptionCycleOptions controls one idempotent cycle credit grant.
type GrantSubscriptionCycleOptions struct {
	Bucket         string
	ExpiresAt      *time.Time
	TTLDays        int
	PlanKey        string
	IdempotencyKey string
	Metadata       CreditMetadata
}

// BilledWork performs application work after a lease has been admitted. It
// returns an application result and the exact amount to settle.
type BilledWork func(context.Context) (result any, actual Amount, err error)

// RunBilledOptions wires reserve -> work -> settle without a framework
// adapter. The operation key scopes both idempotency keys.
type RunBilledOptions struct {
	Estimate           Amount
	DoWork             BilledWork
	OperationKey       string
	OperationType      string
	BillingMode        BillingMode
	TTL                time.Duration
	Feature            string
	Metadata           CreditMetadata
	SettlementAttempts int
}

// RunBilledResult combines an application result with its committed debit.
type RunBilledResult struct {
	Result    any
	Deduction DeductionResult
}

// BeginBilledOperationOptions controls a durable lease handle for a longer
// operation. It may be reconstructed later with ResumeBilledOperation.
type BeginBilledOperationOptions struct {
	Estimate      Amount
	OperationKey  string
	OperationType string
	BillingMode   BillingMode
	TTL           time.Duration
	Feature       string
	Metadata      CreditMetadata
}
