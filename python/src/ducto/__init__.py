"""ducto — declarative credit calculation engine for AI SaaS platforms."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("ducto")
except PackageNotFoundError:  # pragma: no cover - source checkout without install
    __version__ = "0.0.0+unknown"

from ducto.breakdown import CostBreakdown
from ducto.config import ConfigError, PricingConfig
from ducto.engine import PricingEngine
from ducto.events import CreditEvent, CreditEventEmitter
from ducto.expr import ExpressionError, evaluate_expression, validate_expression
from ducto.interface.memory import MemoryStore
from ducto.interface.models import (
    AddCreditsResult,
    AddTeamMemberResult,
    AllowanceResult,
    BalanceResult,
    CapCheckResult,
    CheckFeatureResult,
    CreateTeamResult,
    CreditMetadata,
    DailySpendRow,
    DeductionResult,
    GetUserPlanResult,
    PlanDefinition,
    PricingConfigData,
    PricingConfigResult,
    RefundResult,
    ReserveResult,
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
from ducto.manager import CreditManager, InsufficientCreditsError, PricingNotLoadedError
from ducto.metrics import ToolCall, UsageMetrics

__all__ = [
    "AddCreditsResult",
    "AddTeamMemberResult",
    "AllowanceResult",
    "BalanceResult",
    "CapCheckResult",
    "CheckFeatureResult",
    "ConfigError",
    "CostBreakdown",
    "CreateTeamResult",
    "CreditEvent",
    "CreditEventEmitter",
    "CreditManager",
    "CreditMetadata",
    "DailySpendRow",
    "DeductionResult",
    "ExpressionError",
    "GetUserPlanResult",
    "InsufficientCreditsError",
    "MemoryStore",
    "PlanDefinition",
    "PricingConfig",
    "PricingConfigData",
    "PricingConfigResult",
    "PricingEngine",
    "PricingNotLoadedError",
    "RefundResult",
    "ReserveResult",
    "SetupResult",
    "SetUserPlanResult",
    "SpendByModelRow",
    "SpendByUserRow",
    "SpendCap",
    "SweepResult",
    "Team",
    "TeamBalanceResult",
    "TeamDeductionResult",
    "TeamMember",
    "ToolCall",
    "TopUserRow",
    "TransactionRow",
    "UsageMetrics",
    "evaluate_expression",
    "validate_expression",
]
