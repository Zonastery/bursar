"""ducto — declarative credit calculation engine for AI SaaS platforms."""

__version__ = "0.1.0"

from ducto.breakdown import CostBreakdown
from ducto.config import ConfigError, PricingConfig
from ducto.engine import PricingEngine
from ducto.expr import ExpressionError, evaluate_expression, validate_expression
from ducto.interface.memory import MemoryStore
from ducto.interface.models import (
    AddCreditsResult,
    BalanceResult,
    CreditMetadata,
    DeductionResult,
    PricingConfigData,
    PricingConfigResult,
    ReserveResult,
    SetupResult,
)
from ducto.manager import CreditManager, InsufficientCreditsError, PricingNotLoadedError
from ducto.metrics import ToolCall, UsageMetrics

__all__ = [
    "PricingEngine",
    "CostBreakdown",
    "UsageMetrics",
    "ToolCall",
    "PricingConfig",
    "ConfigError",
    "ExpressionError",
    "evaluate_expression",
    "validate_expression",
    "CreditManager",
    "InsufficientCreditsError",
    "PricingNotLoadedError",
    "CreditMetadata",
    "PricingConfigData",
    "BalanceResult",
    "AddCreditsResult",
    "ReserveResult",
    "DeductionResult",
    "PricingConfigResult",
    "SetupResult",
    "MemoryStore",
]
