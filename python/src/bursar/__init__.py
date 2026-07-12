"""bursar — declarative credit calculation engine for AI SaaS platforms."""

from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING

try:
    __version__ = version("bursar")
except PackageNotFoundError:  # pragma: no cover - source checkout without install
    __version__ = "0.0.0+unknown"

from bursar.billing import (
    AllowanceGrant,
    BillingConfig,
    BillingCreditTopup,
    BillingCustomerInfo,
    BillingDisputeInfo,
    BillingEvent,
    BillingEventClaim,
    BillingEventResult,
    BillingEventType,
    BillingInvoiceInfo,
    BillingManager,
    BillingOffer,
    BillingOfferInterval,
    BillingPaymentInfo,
    BillingProvider,
    BillingRefundInfo,
    BillingStore,
    BillingSubscriptionInfo,
    BillingSubscriptionState,
    BillingSubscriptionStatus,
    CycleGrant,
    ProviderRef,
)
from bursar.breakdown import CostBreakdown

if TYPE_CHECKING:
    from bursar.billing import PostgresBillingStore
from bursar.config import ConfigError, PricingConfig
from bursar.engine import PricingEngine
from bursar.events import CreditEvent, CreditEventEmitter
from bursar.expr import ExpressionError, evaluate_expression, validate_expression
from bursar.interface.base import (
    CapabilityNotSupportedError,
    CapReachedError,
    FeatureLimitReachedError,
    RefundError,
    StoreError,
)
from bursar.interface.models import (
    AddCreditsResult,
    AddTeamMemberResult,
    AggregateStatsRow,
    AllowanceResult,
    AvailableResult,
    BalanceResult,
    BucketBalance,
    BucketBalancesResult,
    BucketDefinition,
    CanAffordResult,
    CapCheckResult,
    CheckFeatureResult,
    CreateTeamResult,
    CreditMetadata,
    DailySpendRow,
    DeductionResult,
    FeatureLimit,
    FeatureLimitResult,
    GetUserPlanResult,
    LeaseResult,
    OperationPolicy,
    PlanDefinition,
    PricingConfigResult,
    RefundResult,
    ReleaseResult,
    SetupResult,
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
    TransactionRow,
)
from bursar.manager import (
    ConcurrencyLimitError,
    CreditError,
    CreditManager,
    FeatureNotEntitledError,
    InsufficientCreditsError,
    LeaseExpiredError,
    LeaseNotFoundError,
    LowBalanceConfig,
    PricingNotLoadedError,
)
from bursar.metrics import ToolCall, UsageMetrics
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
    """Lazy-import PostgresBillingStore — requires psycopg2."""
    if name == "PostgresBillingStore":
        from bursar.billing import PostgresBillingStore  # pyright: ignore[reportUnsupportedDunderAll]

        return PostgresBillingStore
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)


__all__ = [
    "AddCreditsResult",
    "AddTeamMemberResult",
    "AggregateStatsRow",
    "AllowanceGrant",
    "AllowanceResult",
    "AvailableResult",
    "BalanceResult",
    "BillingConfig",
    "BillingCreditTopup",
    "BillingCustomerInfo",
    "BillingDisputeInfo",
    "BillingEvent",
    "BillingEventClaim",
    "BillingEventResult",
    "BillingEventType",
    "BillingInvoiceInfo",
    "BillingManager",
    "BillingOffer",
    "BillingOfferInterval",
    "BillingPaymentInfo",
    "BillingProvider",
    "BillingRefundInfo",
    "BillingStore",
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
    "CreateCustomerParams",
    "CreateTeamResult",
    "CreditError",
    "CreditEvent",
    "CreditEventEmitter",
    "CreditManager",
    "CreditMetadata",
    "CycleGrant",
    "DailySpendRow",
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
    "LeaseResult",
    "LowBalanceConfig",
    "OperationPolicy",
    "PaymentMethodInfo",
    "PaymentMethodSetupParams",
    "PaymentProvider",
    "PlanDefinition",
    "PortalParams",
    "PostgresBillingStore",
    "PricingConfig",
    "PricingConfigResult",
    "PricingEngine",
    "PricingNotLoadedError",
    "ProviderLogger",
    "ProviderRef",
    "RefundError",
    "RefundResult",
    "ReleaseResult",
    "SetupResult",
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
    "ToolCall",
    "TopUserRow",
    "TransactionRow",
    "UpdatePaymentMethodParams",
    "UsageMetrics",
    "validate_expression",
    "WebhookRequest",
]
