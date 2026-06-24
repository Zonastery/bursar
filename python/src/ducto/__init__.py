"""ducto — declarative credit calculation engine for AI SaaS platforms."""

__version__ = "0.1.2"

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
    CreateTeamResult,
    CreditMetadata,
    DailySpendRow,
    DeductionResult,
    GetUserPlanResult,
    PlanDefinition,
    PricingConfigData,
    PricingConfigResult,
    PricingConfigV2,
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
)
from ducto.manager import CreditManager, InsufficientCreditsError, PricingNotLoadedError
from ducto.metrics import ToolCall, UsageMetrics

__all__ = [
    "AddCreditsResult",
    "AddTeamMemberResult",
    "AllowanceResult",
    "BalanceResult",
    "CapCheckResult",
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
    "PricingConfigV2",
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
    "UsageMetrics",
    "evaluate_expression",
    "validate_expression",
]
