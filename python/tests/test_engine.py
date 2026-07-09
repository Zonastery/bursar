"""Tests for the credit calculation engine.

Tests loading config, calculating costs across all pricing dimensions,
Decimal money math (no truncation), clamping, batch operations, schema
introspection, and the cross-SDK parity fixture (pricing_cases).
"""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from bursar.engine import PricingEngine
from bursar.metrics import ToolCall, UsageMetrics

FULL_PRICING = {
    "version": 1,
    "metering": {
        "models": {
            "claude-opus-4": "input_tokens * 0.005 + output_tokens * 0.015",
            "claude-sonnet-4": "input_tokens * 0.003 + output_tokens * 0.009",
            "gemini-2.5-pro": "input_tokens * 0.0025 + output_tokens * 0.0075",
            "gemini-2.5-flash": "input_tokens * 0.0005 + output_tokens * 0.0015",
            "*": "input_tokens * 0.001 + output_tokens * 0.003",
        },
        "tools": {
            "*": "calls * 0",
            "web_search": "web_search_calls * 0.5",
            "code_exec": "code_exec_calls * 0.3",
        },
        "search": "search_queries * 0.5 + search_results * 0.05",
        "cache_discount": "cache_read_tokens * 0.0045",
        "flat_jobs": {
            "batch_job": 20,
            "slow_job": 10,
        },
    },
    "ledger": {
        "min_balance": 5,
    },
}

MINIMAL_PRICING = {
    "version": 1,
    "metering": {
        "models": {
            "*": "input_tokens * 0.001 + output_tokens * 0.003",
        },
    },
    "ledger": {},
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
        engine = PricingEngine.from_dict(
            {"version": 1, "metering": {"models": {"*": "input_tokens * 1"}}, "ledger": {}}
        )
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
        engine = PricingEngine.from_dict(
            {"version": 1, "metering": {"models": {"*": "input_tokens * 0.0004"}}, "ledger": {}}
        )
        result = engine.calculate(UsageMetrics(model="x", input_tokens=1000))
        assert result.total == Decimal("0.4000")

    def test_decimal_precision_no_float_artifacts(self) -> None:
        engine = PricingEngine.from_dict(
            {"version": 1, "metering": {"models": {"*": "input_tokens * 0.1 + output_tokens * 0.2"}}, "ledger": {}}
        )
        result = engine.calculate(UsageMetrics(model="x", input_tokens=1, output_tokens=1))
        assert result.total == Decimal("0.3000")

    def test_fixed_cost_job(self) -> None:
        engine = PricingEngine.from_dict(FULL_PRICING)
        result = engine.calculate(UsageMetrics(model="none", flat_job="batch_job"))
        assert result.flat_job_credits == Decimal("20.0000")
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
        engine = PricingEngine.from_dict(
            {"version": 1, "metering": {"models": {"gpt-4": "input_tokens * 1"}}, "ledger": {}}
        )
        with pytest.raises(ValueError, match="no model match for 'unknown' and no '\\*' in config"):
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
        assert schema["metering"]["models"]
        assert "claude-opus-4" in schema["metering"]["models"]
        assert isinstance(schema["metering"]["models"]["claude-opus-4"], str)
        assert schema["metering"]["models"]["claude-opus-4"] == "input_tokens * 0.005 + output_tokens * 0.015"


class TestEngineMinBalance:
    """min_balance is exposed as a Decimal (contract §1)."""

    def test_min_balance_is_decimal(self) -> None:
        engine = PricingEngine.from_dict(FULL_PRICING)
        assert engine.min_balance == Decimal(5)
        assert isinstance(engine.min_balance, Decimal)

    def test_default_min_balance(self) -> None:
        """WS6 — min_balance defaults to 0 (was 5)."""
        engine = PricingEngine.from_dict(MINIMAL_PRICING)
        assert engine.min_balance == Decimal(0)


class TestEngineFixedJob:
    """Fixed-cost job calculations and get_flat_job_cost contract."""

    def test_fixed_job_batch(self) -> None:
        engine = PricingEngine.from_dict(FULL_PRICING)
        result = engine.calculate(UsageMetrics(model=None, flat_job="batch_job"))
        assert result.flat_job_credits == Decimal("20.0000")
        assert result.total == Decimal("20.0000")

    def test_get_flat_job_cost_known_returns_decimal(self) -> None:
        engine = PricingEngine.from_dict(FULL_PRICING)
        cost = engine.get_flat_job_cost("batch_job")
        assert cost == Decimal("20.0000")
        assert isinstance(cost, Decimal)

    def test_get_flat_job_cost_unknown_returns_none(self) -> None:
        # Unknown / typo'd job -> None so the manager can reject it (L1).
        engine = PricingEngine.from_dict(FULL_PRICING)
        assert engine.get_flat_job_cost("does_not_exist") is None

    def test_get_flat_job_cost_no_fixed_section(self) -> None:
        engine = PricingEngine.from_dict(MINIMAL_PRICING)
        assert engine.get_flat_job_cost("anything") is None


# ── EN1: Tool calls with duplicate names ─────────────────────────────────────


class TestDuplicateToolCalls:
    """EN1 — duplicate ToolCall names are deduplicated by name-set logic."""

    def test_duplicate_web_search_tool_calls(self) -> None:
        # Two ToolCall(name="web_search") — the tool-set is {web_search}, so the
        # formula runs once with the variables dict.  web_search_calls=2 drives
        # the formula result: 2 * 0.5 = 1.0.
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
        assert result.tool_credits == Decimal("1.0000")

    def test_single_web_search_tool_call(self) -> None:
        # Baseline: one ToolCall, web_search_calls=1 -> 1 * 0.5 = 0.5
        engine = PricingEngine.from_dict(FULL_PRICING)
        result = engine.calculate(
            UsageMetrics(
                model="claude-opus-4",
                input_tokens=0,
                output_tokens=0,
                tool_calls=[ToolCall(name="web_search")],
                web_search_calls=1,
            )
        )
        assert result.tool_credits == Decimal("0.5000")


# ── EN2: Empty tool_calls with no tools config ───────────────────────────────


class TestEmptyToolCallsNoToolsConfig:
    """EN2 — empty tool_calls=[] with a config that has no 'tools' section."""

    def test_empty_tool_calls_zero_cost(self) -> None:
        # Config has no 'tools' key; PricingConfig defaults to {"_default": "tool_calls * 0"}.
        # tool_calls=[] -> tool_count=0, formula=0*0=0 -> tool_credits=0.
        engine = PricingEngine.from_dict(MINIMAL_PRICING)
        result = engine.calculate(UsageMetrics(model="_default", tool_calls=[]))
        assert result.tool_credits == Decimal("0.0000")
        assert result.total == Decimal("0.0000")


# ── EN3: Search section absent ───────────────────────────────────────────────


class TestSearchSectionAbsent:
    """EN3 — config with no 'search' key -> search cost = 0."""

    def test_no_search_section_returns_zero(self) -> None:
        engine = PricingEngine.from_dict(MINIMAL_PRICING)
        result = engine.calculate(UsageMetrics(model="_default", search_queries=10, search_results=5))
        assert result.search_credits == Decimal("0.0000")


# ── EN4: Cache section absent ────────────────────────────────────────────────


class TestCacheSectionAbsent:
    """EN4 — config with no 'cache' key -> cache cost = 0."""

    def test_no_cache_section_returns_zero(self) -> None:
        engine = PricingEngine.from_dict(MINIMAL_PRICING)
        result = engine.calculate(UsageMetrics(model="_default", cache_read_tokens=5000))
        assert result.cache_savings == Decimal("0.0000")


# ── EN5: Fixed cost for unknown job ──────────────────────────────────────────


class TestFixedCostUnknownJob:
    """EN5 — get_flat_job_cost for a nonexistent job returns None."""

    def test_nonexistent_job_returns_none(self) -> None:
        engine = PricingEngine.from_dict(FULL_PRICING)
        assert engine.get_flat_job_cost("nonexistent_job") is None


# ── EN6: Model resolution — exact takes priority over prefix ─────────────────


class TestModelResolutionExactBeforePrefix:
    """EN6 — exact match wins over prefix match in resolve_model."""

    def test_exact_match_over_prefix(self) -> None:
        config = {
            "version": 1,
            "metering": {
                "models": {
                    "gpt-4": "input_tokens * 1",
                    "gpt-4-turbo": "input_tokens * 2",
                }
            },
            "ledger": {},
        }
        engine = PricingEngine.from_dict(config)
        resolved = engine.resolve_model("gpt-4-turbo")
        assert resolved == "gpt-4-turbo"

    def test_prefix_match_used_when_no_exact(self) -> None:
        config = {
            "version": 1,
            "metering": {
                "models": {
                    "gpt-4": "input_tokens * 1",
                }
            },
            "ledger": {},
        }
        engine = PricingEngine.from_dict(config)
        # "gpt-4-20240601" has no exact match; "gpt-4" is a prefix
        resolved = engine.resolve_model("gpt-4-20240601")
        assert resolved == "gpt-4"

    def test_calculate_uses_exact_model_formula(self) -> None:
        # Verify calculate() itself picks the right formula (exact match).
        config = {
            "version": 1,
            "metering": {
                "models": {
                    "gpt-4": "input_tokens * 1",
                    "gpt-4-turbo": "input_tokens * 2",
                }
            },
            "ledger": {},
        }
        engine = PricingEngine.from_dict(config)
        result = engine.calculate(UsageMetrics(model="gpt-4-turbo", input_tokens=100))
        # gpt-4-turbo formula: 100 * 2 = 200
        assert result.model_credits == Decimal("200.0000")


# ── EN7: Quantization of summed components ───────────────────────────────────


class TestQuantizationSummedComponents:
    """EN7 — total is quantized to 4dp ROUND_HALF_UP after summing."""

    def test_repeating_decimal_quantized(self) -> None:
        # input_tokens * (1/3) — but (1/3) is exact division in Decimal,
        # producing a long repeating decimal. Quantized to 4dp.
        # 1 * Decimal(1) / Decimal(3) = 0.3333... -> quantized = 0.3333
        engine = PricingEngine.from_dict(
            {"version": 1, "metering": {"models": {"*": "input_tokens * 1 / output_tokens"}}, "ledger": {}}
        )
        # input_tokens=1, output_tokens=3 -> 1/3 -> 0.3333...
        result = engine.calculate(UsageMetrics(model="_default", input_tokens=1, output_tokens=3))
        assert result.total == Decimal("0.3333")
        assert str(result.total) == "0.3333"


# ── EN8: Total clamped at zero when cache savings exceed model cost ───────────


class TestTotalClampedAtZero:
    """EN8 — total is >= 0 even when cache discount exceeds model cost."""

    def test_cache_discount_exceeds_model_cost(self) -> None:
        config = {
            "version": 1,
            "metering": {
                "models": {"*": "input_tokens * 0.001"},
                "cache_discount": "cache_read_tokens * 0.01",
            },
            "ledger": {},
        }
        engine = PricingEngine.from_dict(config)
        # model cost: 10 * 0.001 = 0.01
        # cache discount: -(1000 * 0.01) = -10.0
        # raw_total = 0.01 - 10.0 = -9.99 -> clamped to 0
        result = engine.calculate(UsageMetrics(model="_default", input_tokens=10, cache_read_tokens=1000))
        assert result.total == Decimal("0.0000")
        assert result.cache_savings == Decimal("-10.0000")
        assert result.model_credits == Decimal("0.0100")


# ── EN9: calculateBatch with empty list ──────────────────────────────────────


class TestCalculateBatchEmpty:
    """EN9 — calculate_batch([]) returns an empty list without error."""

    def test_empty_batch_returns_empty_list(self) -> None:
        engine = PricingEngine.from_dict(FULL_PRICING)
        results = engine.calculate_batch([])
        assert results == []
        assert isinstance(results, list)


# ── EN10: Model prefix ambiguity in calculate() ──────────────────────────────


class TestModelPrefixAmbiguity:
    """EN10 — calculate() uses exact match then _default; resolve_model() does prefix.

    _calc_model performs: (1) exact match, (2) _default fallback.
    It does NOT do prefix matching — that is only available via resolve_model().
    This test documents the boundary precisely so future refactors don't
    accidentally change the contract.
    """

    _CONFIG = {
        "version": 1,
        "metering": {
            "models": {
                "gpt-4": "input_tokens * 0.002",
                "gpt-4-turbo": "input_tokens * 0.003",
                "*": "input_tokens * 0.001",
            },
        },
        "ledger": {},
    }

    def test_exact_match_gpt4_turbo(self) -> None:
        # "gpt-4-turbo" has an exact entry -> 1000 * 0.003 = 3.0000
        engine = PricingEngine.from_dict(self._CONFIG)
        result = engine.calculate(UsageMetrics(model="gpt-4-turbo", input_tokens=1000))
        assert result.total == Decimal("3.0000")

    def test_no_exact_match_falls_to_default_not_prefix(self) -> None:
        # "gpt-4-0613" has no exact entry; _calc_model skips prefix matching
        # and falls through to _default -> 1000 * 0.001 = 1.0000, NOT 2.0000.
        engine = PricingEngine.from_dict(self._CONFIG)
        result = engine.calculate(UsageMetrics(model="gpt-4-0613", input_tokens=1000))
        assert result.total == Decimal("1.0000")

    def test_resolve_model_does_prefix_match(self) -> None:
        # resolve_model() (the public helper) DOES do prefix matching.
        engine = PricingEngine.from_dict(self._CONFIG)
        assert engine.resolve_model("gpt-4-0613") == "gpt-4"

    def test_unknown_model_falls_to_default(self) -> None:
        # "claude-3" has neither exact nor prefix match -> _default -> 1.0000
        engine = PricingEngine.from_dict(self._CONFIG)
        result = engine.calculate(UsageMetrics(model="claude-3", input_tokens=1000))
        assert result.total == Decimal("1.0000")


# ── EN11: calculate_batch preserves input order ──────────────────────────────


class TestCalculateBatchOrdering:
    """EN11 — calculate_batch returns results in the same order as inputs."""

    def test_batch_order_preserved(self) -> None:
        engine = PricingEngine.from_dict(FULL_PRICING)
        m1 = UsageMetrics(model="claude-opus-4", input_tokens=1000, output_tokens=0)
        m2 = UsageMetrics(model="gemini-2.5-flash", input_tokens=500, output_tokens=0)
        m3 = UsageMetrics(model="claude-sonnet-4", input_tokens=200, output_tokens=0)

        # Expected totals for each individually:
        # m1: 1000 * 0.005 = 5.0000
        # m2: 500 * 0.0005 = 0.2500
        # m3: 200 * 0.003 = 0.6000
        individual = [engine.calculate(m) for m in [m1, m2, m3]]
        batch = engine.calculate_batch([m1, m2, m3])

        assert len(batch) == 3
        for i, (ind, bat) in enumerate(zip(individual, batch, strict=True)):
            assert bat.total == ind.total, f"index {i}: batch={bat.total}, individual={ind.total}"

    def test_batch_order_with_varied_models(self) -> None:
        # Use a config where each model produces a clearly distinct total.
        config = {
            "version": 1,
            "metering": {
                "models": {
                    "m1": "input_tokens * 1",
                    "m2": "input_tokens * 2",
                    "m3": "input_tokens * 3",
                }
            },
            "ledger": {},
        }
        engine = PricingEngine.from_dict(config)
        metrics = [
            UsageMetrics(model="m3", input_tokens=10),  # 30
            UsageMetrics(model="m1", input_tokens=10),  # 10
            UsageMetrics(model="m2", input_tokens=10),  # 20
        ]
        results = engine.calculate_batch(metrics)
        assert results[0].total == Decimal("30.0000")
        assert results[1].total == Decimal("10.0000")
        assert results[2].total == Decimal("20.0000")


# ── EN12: Cache discount clamped at zero ─────────────────────────────────────


class TestCacheDiscountClampedAtZero:
    """EN12 — total is never negative even when cache savings exceed model cost."""

    def test_large_cache_savings_clamped_to_zero(self) -> None:
        # Model cost: 10 * 0.001 = 0.01
        # Cache savings: -5000 * 0.01 = -50.0
        # raw total = 0.01 - 50.0 = -49.99 -> clamped to 0.0000
        config = {
            "version": 1,
            "metering": {
                "models": {"*": "input_tokens * 0.001"},
                "cache_discount": "cache_read_tokens * 0.01",
            },
            "ledger": {},
        }
        engine = PricingEngine.from_dict(config)
        result = engine.calculate(UsageMetrics(model="_default", input_tokens=10, cache_read_tokens=5000))
        assert result.total == Decimal("0.0000"), f"total should be clamped to 0, got {result.total}"
        # Component breakdown values are preserved (not clamped).
        assert result.cache_savings == Decimal("-50.0000")
        assert result.model_credits == Decimal("0.0100")

    def test_total_never_negative_regardless_of_magnitude(self) -> None:
        # Extreme scenario: effectively unlimited cache discount.
        config = {
            "version": 1,
            "metering": {
                "models": {"*": "input_tokens * 0.001"},
                "cache_discount": "cache_read_tokens * 1000",
            },
            "ledger": {},
        }
        engine = PricingEngine.from_dict(config)
        result = engine.calculate(UsageMetrics(model="_default", input_tokens=1, cache_read_tokens=999999))
        assert result.total == Decimal("0.0000")


# ── WS2: this_tool_calls is scoped per-tool, not the global total ────────────


class TestThisToolCallsPerTool:
    """WS2 — each tool formula sees ITS OWN call count via this_tool_calls.

    Previously the engine bound the shared ``tool_calls`` variable to the
    unknown-call count when evaluating the ``_default`` formula (and left it
    as the global total otherwise) — a known tool's formula always saw the
    GLOBAL total, never its own count. Now ``this_tool_calls`` carries the
    per-branch count and ``tool_calls`` always stays the global total in
    every branch.
    """

    _CONFIG = {
        "version": 1,
        "metering": {
            "models": {"*": "input_tokens * 0"},
            "tools": {
                "code_exec": "calls * 10 / 1000",
                "*": "calls * 5 / 1000",
            },
        },
        "ledger": {},
    }

    def test_known_tool_priced_on_its_own_count_not_total(self) -> None:
        # 2 code_exec calls + 3 unconfigured calls = 5 total tool_calls.
        # code_exec must be priced on exactly 2 (this_tool_calls), not 5.
        engine = PricingEngine.from_dict(self._CONFIG)
        result = engine.calculate(
            UsageMetrics(
                model="_default",
                tool_calls=[
                    ToolCall(name="code_exec"),
                    ToolCall(name="code_exec"),
                    ToolCall(name="other_a"),
                    ToolCall(name="other_b"),
                    ToolCall(name="other_c"),
                ],
            )
        )
        # code_exec: 2 * 10 / 1000 = 0.02
        # _default (3 unknown calls): 3 * 5 / 1000 = 0.015
        # total tool_credits = 0.035
        assert result.tool_credits == Decimal("0.0350")

    def test_default_branch_priced_on_unknown_count_not_total(self) -> None:
        # Only unconfigured tool calls: _default must see exactly 3 (this_tool_calls),
        # which happens to equal the total here too — verified against the mixed case above.
        engine = PricingEngine.from_dict(self._CONFIG)
        result = engine.calculate(
            UsageMetrics(
                model="_default",
                tool_calls=[ToolCall(name="other_a"), ToolCall(name="other_b"), ToolCall(name="other_c")],
            )
        )
        # _default: 3 * 5 / 1000 = 0.015
        assert result.tool_credits == Decimal("0.0150")

    def test_tool_calls_variable_stays_global_total_in_tools_expression(self) -> None:
        # A tool formula that references the GLOBAL tool_calls (not this_tool_calls)
        # must see the total across ALL tools, not just its own count.
        config = {
            "version": 1,
            "metering": {
                "models": {"*": "input_tokens * 0"},
                "tools": {
                    "code_exec": "tool_calls * 1",
                    "*": "tool_calls * 0",
                },
            },
            "ledger": {},
        }
        engine = PricingEngine.from_dict(config)
        result = engine.calculate(
            UsageMetrics(
                model="_default",
                tool_calls=[ToolCall(name="code_exec"), ToolCall(name="other_a"), ToolCall(name="other_b")],
            )
        )
        # code_exec formula uses global tool_calls=3 (not this_tool_calls=1) -> 3 * 1 = 3
        assert result.tool_credits == Decimal("3.0000")
