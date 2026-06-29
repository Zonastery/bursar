"""Tests for the credit calculation engine.

Tests loading config, calculating costs across all pricing dimensions,
Decimal money math (no truncation), clamping, batch operations, schema
introspection, and the cross-SDK parity fixture (pricing_cases).
"""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from ducto.engine import PricingEngine
from ducto.metrics import ToolCall, UsageMetrics

FULL_PRICING = {
    "models": {
        "claude-opus-4": "input_tokens * 0.005 + output_tokens * 0.015",
        "claude-sonnet-4": "input_tokens * 0.003 + output_tokens * 0.009",
        "gemini-2.5-pro": "input_tokens * 0.0025 + output_tokens * 0.0075",
        "gemini-2.5-flash": "input_tokens * 0.0005 + output_tokens * 0.0015",
        "_default": "input_tokens * 0.001 + output_tokens * 0.003",
    },
    "tools": {
        "_default": "tool_calls * 0",
        "web_search": "web_search_calls * 0.5",
        "code_exec": "code_exec_calls * 0.3",
    },
    "search": {
        "costs": "search_queries * 0.5 + search_results * 0.05",
    },
    "cache": {
        "discount": "-cache_read_tokens * 0.0045",
    },
    "min_balance": 5,
    "fixed": {
        "batch_job": 20,
        "slow_job": 10,
    },
}

MINIMAL_PRICING = {
    "models": {
        "_default": "input_tokens * 0.001 + output_tokens * 0.003",
    },
}

# ── Parity fixture (pricing_cases) ──────────────────────────────────────────

_PARITY_PATH = Path(__file__).parent / "../../tests/parity/expression_cases.json"


def _load_parity() -> dict:
    with _PARITY_PATH.open() as f:
        return json.load(f)


_PRICING_CASES = _load_parity()["pricing_cases"]


@pytest.mark.parametrize("case", _PRICING_CASES, ids=[c["name"] for c in _PRICING_CASES])
def test_parity_pricing_cases(case: dict) -> None:
    """Each fixture pricing case: engine.calculate(...).total == expected_total."""
    engine = PricingEngine.from_dict(case["config"])
    breakdown = engine.calculate(UsageMetrics(**case["metrics"]))
    assert breakdown.total == Decimal(case["expected_total"])
    assert str(breakdown.total) == case["expected_total"]


class TestPricingEngineLoading:
    """PricingEngine construction from dict sources."""

    def test_from_dict(self) -> None:
        engine = PricingEngine.from_dict({"models": {"_default": "input_tokens * 1"}})
        assert engine is not None

    def test_from_dict_with_full_config(self) -> None:
        engine = PricingEngine.from_dict(FULL_PRICING)
        assert engine is not None

    def test_from_dict_with_minimal_config(self) -> None:
        engine = PricingEngine.from_dict(MINIMAL_PRICING)
        assert engine is not None


class TestPricingEngineCalculate:
    """Single-request cost calculations. All money is exact Decimal."""

    def test_model_cost_only(self) -> None:
        engine = PricingEngine.from_dict(FULL_PRICING)
        result = engine.calculate(UsageMetrics(model="claude-opus-4", input_tokens=1000, output_tokens=2000))
        # 1000*0.005 + 2000*0.015 = 5 + 30 = 35
        assert result.model_credits == Decimal("35.0000")
        assert result.total == Decimal("35.0000")

    def test_fallback_to_default_model(self) -> None:
        engine = PricingEngine.from_dict(FULL_PRICING)
        result = engine.calculate(UsageMetrics(model="unknown-model", input_tokens=1000, output_tokens=1000))
        # _default: 1000*0.001 + 1000*0.003 = 1 + 3 = 4
        assert result.model_credits == Decimal("4.0000")
        assert result.total == Decimal("4.0000")

    def test_full_calculation_all_dimensions(self) -> None:
        engine = PricingEngine.from_dict(FULL_PRICING)
        result = engine.calculate(
            UsageMetrics(
                model="gemini-2.5-flash",
                input_tokens=500,
                output_tokens=1000,
                tool_calls=[ToolCall(name="web_search")],
                web_search_calls=1,
                search_queries=2,
                search_results=10,
                cache_read_tokens=200,
            )
        )
        # model: 500*0.0005 + 1000*0.0015 = 0.25 + 1.5 = 1.75
        # tools: web_search override -> 1*0.5 = 0.5
        # search: 2*0.5 + 10*0.05 = 1 + 0.5 = 1.5
        # cache: -200*0.0045 = -0.9
        # total: 1.75 + 0.5 + 1.5 - 0.9 = 2.85
        assert result.model_credits == Decimal("1.7500")
        assert result.tool_credits == Decimal("0.5000")
        assert result.search_credits == Decimal("1.5000")
        assert result.cache_savings == Decimal("-0.9000")
        assert result.total == Decimal("2.8500")

    def test_sub_one_credit_not_truncated(self) -> None:
        # A 0.4-credit op must charge 0.4, not 0 (revenue-leak guard, H1).
        engine = PricingEngine.from_dict({"models": {"_default": "input_tokens * 0.0004"}})
        result = engine.calculate(UsageMetrics(model="x", input_tokens=1000))
        assert result.total == Decimal("0.4000")

    def test_decimal_precision_no_float_artifacts(self) -> None:
        engine = PricingEngine.from_dict({"models": {"_default": "input_tokens * 0.1 + output_tokens * 0.2"}})
        result = engine.calculate(UsageMetrics(model="x", input_tokens=1, output_tokens=1))
        assert result.total == Decimal("0.3000")

    def test_fixed_cost_job(self) -> None:
        engine = PricingEngine.from_dict(FULL_PRICING)
        result = engine.calculate(UsageMetrics(model="none", fixed_job="batch_job"))
        assert result.fixed_credits == Decimal("20.0000")
        assert result.total == Decimal("20.0000")

    def test_total_clamped_to_zero(self) -> None:
        engine = PricingEngine.from_dict(FULL_PRICING)
        result = engine.calculate(
            UsageMetrics(model="claude-opus-4", input_tokens=0, output_tokens=0, cache_read_tokens=100000)
        )
        # model: 0, cache: -100000*0.0045 = -450 -> total clamped to 0
        assert result.total == Decimal("0.0000")
        # But the cache_savings component retains its (negative) value.
        assert result.cache_savings == Decimal("-450.0000")

    def test_zero_metrics_returns_zero(self) -> None:
        engine = PricingEngine.from_dict(MINIMAL_PRICING)
        result = engine.calculate(UsageMetrics(model="unknown"))
        assert result.total == Decimal("0.0000")

    def test_model_not_found_and_no_default_raises_error(self) -> None:
        engine = PricingEngine.from_dict({"models": {"gpt-4": "input_tokens * 1"}})
        with pytest.raises(ValueError, match="no model match for 'unknown' and no _default in config"):
            engine.calculate(UsageMetrics(model="unknown"))

    def test_tool_specific_override_used(self) -> None:
        engine = PricingEngine.from_dict(FULL_PRICING)
        result = engine.calculate(
            UsageMetrics(
                model="claude-opus-4",
                input_tokens=0,
                output_tokens=0,
                tool_calls=[ToolCall(name="web_search"), ToolCall(name="web_search")],
                web_search_calls=2,
            )
        )
        # web_search override is 0.5 per call -> 1.0
        assert result.tool_credits == Decimal("1.0000")

    def test_batch_calculation(self) -> None:
        engine = PricingEngine.from_dict(FULL_PRICING)
        results = engine.calculate_batch(
            [
                UsageMetrics(model="claude-opus-4", input_tokens=1000, output_tokens=2000),
                UsageMetrics(model="gemini-2.5-flash", input_tokens=500, output_tokens=1000),
            ]
        )
        assert len(results) == 2
        assert results[0].total == Decimal("35.0000")
        assert results[1].total == Decimal("1.7500")

    def test_pricing_schema_returns_pydantic_model(self) -> None:
        engine = PricingEngine.from_dict(FULL_PRICING)
        schema = engine.pricing_schema()
        assert schema.models
        assert "claude-opus-4" in schema.models
        assert isinstance(schema.models["claude-opus-4"], str)
        assert schema.models["claude-opus-4"] == "input_tokens * 0.005 + output_tokens * 0.015"


class TestEngineMinBalance:
    """min_balance is exposed as a Decimal (contract §1)."""

    def test_min_balance_is_decimal(self) -> None:
        engine = PricingEngine.from_dict(FULL_PRICING)
        assert engine.min_balance == Decimal(5)
        assert isinstance(engine.min_balance, Decimal)

    def test_default_min_balance(self) -> None:
        engine = PricingEngine.from_dict(MINIMAL_PRICING)
        assert engine.min_balance == Decimal(5)


class TestEngineFixedJob:
    """Fixed-cost job calculations and get_fixed_cost contract."""

    def test_fixed_job_batch(self) -> None:
        engine = PricingEngine.from_dict(FULL_PRICING)
        result = engine.calculate(UsageMetrics(model=None, fixed_job="batch_job"))
        assert result.fixed_credits == Decimal("20.0000")
        assert result.total == Decimal("20.0000")

    def test_get_fixed_cost_known_returns_decimal(self) -> None:
        engine = PricingEngine.from_dict(FULL_PRICING)
        cost = engine.get_fixed_cost("batch_job")
        assert cost == Decimal("20.0000")
        assert isinstance(cost, Decimal)

    def test_get_fixed_cost_unknown_returns_none(self) -> None:
        # Unknown / typo'd job -> None so the manager can reject it (L1).
        engine = PricingEngine.from_dict(FULL_PRICING)
        assert engine.get_fixed_cost("does_not_exist") is None

    def test_get_fixed_cost_no_fixed_section(self) -> None:
        engine = PricingEngine.from_dict(MINIMAL_PRICING)
        assert engine.get_fixed_cost("anything") is None
