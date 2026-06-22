"""Tests for the credit calculation engine.

Tests loading config (YAML/dict), calculating costs across all pricing
dimensions, clamping, batch operations, and schema introspection.
"""

import pytest

from ducto.config import ConfigError
from ducto.engine import PricingEngine
from ducto.metrics import ToolCall, UsageMetrics


class TestPricingEngineLoading:
    """PricingEngine construction from YAML and dict sources."""

    def test_from_yaml_with_full_config(self) -> None:
        engine = PricingEngine.from_yaml("tests/fixtures/pricing_full.yaml")
        assert engine is not None

    def test_from_yaml_with_minimal_config(self) -> None:
        engine = PricingEngine.from_yaml("tests/fixtures/pricing_minimal.yaml")
        assert engine is not None

    def test_from_dict(self) -> None:
        engine = PricingEngine.from_dict(
            {
                "version": 1,
                "models": {"_default": "input_tokens * 1"},
            }
        )
        assert engine is not None

    def test_invalid_yaml_path_raises_error(self) -> None:
        with pytest.raises(ConfigError, match="config file not found"):
            PricingEngine.from_yaml("tests/fixtures/nonexistent.yaml")


class TestPricingEngineCalculate:
    """Single-request cost calculations."""

    def test_model_cost_only(self) -> None:
        engine = PricingEngine.from_yaml("tests/fixtures/pricing_full.yaml")
        result = engine.calculate(
            UsageMetrics(
                model="claude-opus-4",
                input_tokens=1000,
                output_tokens=2000,
            )
        )
        # 1000*0.005 + 2000*0.015 = 5 + 30 = 35
        assert result.model_credits == pytest.approx(35.0, rel=1e-3)
        assert result.total == pytest.approx(35.0, rel=1e-3)

    def test_fallback_to_default_model(self) -> None:
        engine = PricingEngine.from_yaml("tests/fixtures/pricing_full.yaml")
        result = engine.calculate(
            UsageMetrics(
                model="unknown-model",
                input_tokens=1000,
                output_tokens=1000,
            )
        )
        # _default: 1000*0.001 + 1000*0.003 = 1 + 3 = 4
        assert result.model_credits == pytest.approx(4.0, rel=1e-3)
        assert result.total == pytest.approx(4.0, rel=1e-3)

    def test_full_calculation_all_dimensions(self) -> None:
        engine = PricingEngine.from_yaml("tests/fixtures/pricing_full.yaml")
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
        # total: 1.75 + 0.5 + 1.5 + (-0.9) = 2.85
        assert result.model_credits == pytest.approx(1.75, rel=1e-3)
        assert result.tool_credits == pytest.approx(0.5, rel=1e-3)
        assert result.search_credits == pytest.approx(1.5, rel=1e-3)
        assert result.cache_savings == pytest.approx(-0.9, rel=1e-3)
        assert result.total == pytest.approx(2.85, rel=1e-3)

    def test_fixed_cost_job(self) -> None:
        engine = PricingEngine.from_yaml("tests/fixtures/pricing_full.yaml")
        result = engine.calculate(
            UsageMetrics(
                model="none",  # _default: 0*0.001 + 0*0.003 = 0
                fixed_job="roadmap_gen",
            )
        )
        assert result.fixed_credits == 20.0
        assert result.total == 20.0

    def test_total_clamped_to_zero(self) -> None:
        engine = PricingEngine.from_yaml("tests/fixtures/pricing_full.yaml")
        result = engine.calculate(
            UsageMetrics(
                model="claude-opus-4",
                input_tokens=0,
                output_tokens=0,
                cache_read_tokens=100000,
            )
        )
        # model: 0, cache: -100000*0.0045 = -450
        # total clamped to 0
        assert result.total == 0.0

    def test_zero_metrics_returns_zero(self) -> None:
        engine = PricingEngine.from_yaml("tests/fixtures/pricing_minimal.yaml")
        result = engine.calculate(UsageMetrics(model="unknown"))
        assert result.total == 0.0

    def test_model_not_found_and_no_default_raises_error(self) -> None:
        engine = PricingEngine.from_dict(
            {
                "version": 1,
                "models": {"gpt-4": "input_tokens * 1"},
            }
        )
        with pytest.raises(ValueError, match="no model match for 'unknown' and no _default in config"):
            engine.calculate(UsageMetrics(model="unknown"))

    def test_tool_specific_override_used(self) -> None:
        engine = PricingEngine.from_yaml("tests/fixtures/pricing_full.yaml")
        result = engine.calculate(
            UsageMetrics(
                model="claude-opus-4",
                input_tokens=0,
                output_tokens=0,
                tool_calls=[ToolCall(name="web_search"), ToolCall(name="web_search")],
                web_search_calls=2,
            )
        )
        # _default is 0, web_search override is 0.5 per call
        assert result.tool_credits == pytest.approx(1.0, rel=1e-3)

    def test_batch_calculation(self) -> None:
        engine = PricingEngine.from_yaml("tests/fixtures/pricing_full.yaml")
        results = engine.calculate_batch(
            [
                UsageMetrics(model="claude-opus-4", input_tokens=1000, output_tokens=2000),
                UsageMetrics(model="gemini-2.5-flash", input_tokens=500, output_tokens=1000),
            ]
        )
        assert len(results) == 2
        assert results[0].total == pytest.approx(35.0, rel=1e-3)
        assert results[1].total == pytest.approx(1.75, rel=1e-3)

    def test_pricing_schema_returns_pydantic_model(self) -> None:
        engine = PricingEngine.from_yaml("tests/fixtures/pricing_full.yaml")
        schema = engine.pricing_schema()
        assert schema.models
        assert "claude-opus-4" in schema.models
        assert isinstance(schema.models["claude-opus-4"], str)
        assert schema.models["claude-opus-4"] == "input_tokens * 0.005 + output_tokens * 0.015"


class TestEngineFixedJob:
    """Fixed-cost job calculations."""

    def test_fixed_job_roadmap_gen(self) -> None:
        engine = PricingEngine.from_yaml("tests/fixtures/pricing_full.yaml")
        result = engine.calculate(
            UsageMetrics(
                model=None,
                fixed_job="roadmap_gen",
            )
        )
        assert result.fixed_credits == 20.0
        assert result.total == 20.0
