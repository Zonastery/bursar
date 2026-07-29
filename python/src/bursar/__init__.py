"""bursar — declarative credit calculation engine for AI SaaS platforms."""

from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING

from bursar.billing.auto_recharge_service import AutoRechargeService

try:
    __version__ = version("bursar")
except PackageNotFoundError:  # pragma: no cover - source checkout without install
    __version__ = "0.0.0+unknown"

from bursar.allowance import resolve_allowance_window, resolve_calendar_window
from bursar.billing import (
    AllowanceGrant,
    BillingAutoRechargeAttempt,
    BillingAutoRechargeProfile,
    BillingCreditTopup,
    BillingCustomerInfo,
    BillingCustomerRecord,
    BillingDisputeInfo,
    BillingEvent,
    BillingEventClaim,
    BillingEventResult,
    BillingEventType,
    BillingInvoiceInfo,
    BillingOffer,
    BillingOfferInterval,
    BillingPaymentInfo,
    BillingPreferences,
    BillingProvider,
    BillingRefundInfo,
    BillingStore,
    BillingSubscriptionInfo,
    BillingSubscriptionState,
    BillingSubscriptionStatus,
    CycleGrant,
    ProviderRef,
)
from bursar.billing.billing_service import BillingProvisioningPort
from bursar.breakdown import CostBreakdown, make_cost_breakdown
from bursar.bursar import BillingEventSink, BillingService, Bursar, CatalogService, CreditsService

if TYPE_CHECKING:
    from bursar.billing import PostgresBillingStore
    from bursar.credits.postgres.store import PostgresStore
from bursar.config import BursarConfig, ConfigError
from bursar.credits.events import CreditEvent, CreditEventEmitter
from bursar.credits.service import (
    ConcurrencyLimitError,
    CreditError,
    FeatureNotEntitledError,
    InsufficientCreditsError,
    LeaseExpiredError,
    LeaseNotFoundError,
    OperationNotAllowedError,
    PricingNotLoadedError,
    QuotaExceededError,
)
from bursar.credits.store import (
    CapabilityNotSupportedError,
    CapReachedError,
    FeatureLimitReachedError,
    RefundError,
    StoreError,
)
from bursar.credits.types import (
    AddCreditsResult,
    AddTeamMemberResult,
    AggregateStatsRow,
    AllowanceResult,
    AvailableResult,
    BalanceResult,
    BucketBalance,
    BucketBalancesResult,
    BucketDefinition,
    BursarConfigResult,
    CanAffordResult,
    CapCheckResult,
    CheckFeatureResult,
    CreateTeamResult,
    CreditMetadata,
    DailySpendRow,
    DeductionResult,
    DeductWithAllowanceOptions,
    FeatureLimit,
    FeatureLimitResult,
    GetUserPlanResult,
    LeasePricingContext,
    LeaseResult,
    LedgerCursor,
    LedgerEntry,
    LedgerPage,
    ListLedgerEntriesOptions,
    ListQuotaEventsOptions,
    ListUsageEntriesOptions,
    OperationPolicy,
    PlanDefinition,
    PlanMigrationBatchResult,
    PlanMigrationStartResult,
    QuotaEvent,
    QuotaState,
    RefundResult,
    ReleaseResult,
    SetUserPlanResult,
    SpendByModelRow,
    SpendByUserRow,
    SpendCap,
    SweepResult,
    Team,
    TeamBalanceResult,
    TeamDeductionResult,
    TeamMember,
    TopUserRow,
)
from bursar.engine import PricingEngine
from bursar.expr import ExpressionError, evaluate_expression, quantize_money, validate_expression
from bursar.metrics import UsageMetrics
from bursar.providers.types import (
    CheckoutParams,
    CreateCustomerParams,
    PaymentMethodInfo,
    PaymentMethodSetupParams,
    PaymentProvider,
    PortalParams,
    ProviderLogger,
    UpdatePaymentMethodParams,
    WebhookRequest,
)


def __getattr__(name: str):
    """Lazy-import Postgres stores — they require the optional psycopg2 extra."""
    if name == "PostgresBillingStore":
        from bursar.billing import PostgresBillingStore  # pyright: ignore[reportUnsupportedDunderAll]

        return PostgresBillingStore
    if name == "PostgresStore":
        from bursar.credits.postgres.store import PostgresStore

        return PostgresStore
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)


__all__ = [
    "BillingAutoRechargeAttempt",
    "BillingAutoRechargeProfile",
    "AutoRechargeService",
    "AddCreditsResult",
    "AddTeamMemberResult",
    "AggregateStatsRow",
    "AllowanceGrant",
    "AllowanceResult",
    "AvailableResult",
    "BalanceResult",
    "BillingCreditTopup",
    "BillingCustomerInfo",
    "BillingCustomerRecord",
    "BillingDisputeInfo",
    "BillingEvent",
    "BillingEventClaim",
    "BillingEventResult",
    "BillingEventType",
    "BillingInvoiceInfo",
    "BillingOffer",
    "BillingOfferInterval",
    "BillingPaymentInfo",
    "BillingPreferences",
    "BillingProvider",
    "BillingRefundInfo",
    "BillingStore",
    "BillingProvisioningPort",
    "Bursar",
    "BillingEventSink",
    "BillingService",
    "CatalogService",
    "BillingSubscriptionInfo",
    "BillingSubscriptionState",
    "BillingSubscriptionStatus",
    "BucketBalance",
    "BucketBalancesResult",
    "BucketDefinition",
    "CanAffordResult",
    "CapCheckResult",
    "CapReachedError",
    "CapabilityNotSupportedError",
    "CheckFeatureResult",
    "CheckoutParams",
    "ConcurrencyLimitError",
    "ConfigError",
    "CostBreakdown",
    "CreditsService",
    "CreateCustomerParams",
    "CreateTeamResult",
    "CreditError",
    "CreditEvent",
    "CreditEventEmitter",
    "CreditMetadata",
    "CycleGrant",
    "DailySpendRow",
    "DeductWithAllowanceOptions",
    "DeductionResult",
    "evaluate_expression",
    "ExpressionError",
    "FeatureLimit",
    "FeatureLimitReachedError",
    "FeatureLimitResult",
    "FeatureNotEntitledError",
    "GetUserPlanResult",
    "InsufficientCreditsError",
    "LeaseExpiredError",
    "LeaseNotFoundError",
    "LeasePricingContext",
    "LeaseResult",
    "LedgerCursor",
    "LedgerEntry",
    "LedgerPage",
    "ListLedgerEntriesOptions",
    "ListQuotaEventsOptions",
    "ListUsageEntriesOptions",
    "make_cost_breakdown",
    "OperationNotAllowedError",
    "OperationPolicy",
    "PlanMigrationBatchResult",
    "PlanMigrationStartResult",
    "PaymentMethodInfo",
    "PaymentMethodSetupParams",
    "PaymentProvider",
    "PlanDefinition",
    "PortalParams",
    "PostgresBillingStore",
    "PostgresStore",
    "BursarConfig",
    "BursarConfigResult",
    "PricingEngine",
    "PricingNotLoadedError",
    "quantize_money",
    "QuotaEvent",
    "QuotaExceededError",
    "QuotaState",
    "ProviderLogger",
    "ProviderRef",
    "RefundError",
    "RefundResult",
    "ReleaseResult",
    "resolve_allowance_window",
    "resolve_calendar_window",
    "SetUserPlanResult",
    "SpendByModelRow",
    "SpendByUserRow",
    "SpendCap",
    "StoreError",
    "SweepResult",
    "Team",
    "TeamBalanceResult",
    "TeamDeductionResult",
    "TeamMember",
    "TopUserRow",
    "UpdatePaymentMethodParams",
    "UsageMetrics",
    "validate_expression",
    "WebhookRequest",
]
