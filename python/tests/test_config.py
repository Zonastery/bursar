"""Tests for pricing config parsing and validation."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from bursar.config import (
    ConfigError,
    PricingConfig,
    load_config_from_dict,
)
from bursar.interface.models import PricingConfigData


class TestConfigValidation:
    """Tests for config loading and validation."""

    def test_valid_full_config(self) -> None:
        """Loading a full config dict populates all sections."""
        config = load_config_from_dict(
            {
                "models": {"gpt-4": "input_tokens * 0.01"},
                "tools": {"_default": "tool_calls * 0.1"},
            }
        )
        assert config.models["gpt-4"] == "input_tokens * 0.01"

    def test_minimal_config(self) -> None:
        """Minimal config with only version and models works."""
        config = load_config_from_dict(
            {
                "models": {"_default": "input_tokens * 0.001"},
            }
        )
        assert config.models["_default"] == "input_tokens * 0.001"

    def test_invalid_expression_raises_error(self) -> None:
        """An expression with disallowed syntax raises ConfigError."""
        with pytest.raises(ConfigError, match="invalid expression"):
            load_config_from_dict(
                {
                    "models": {"gpt-4": "lambda x: x"},
                }
            )

    def test_missing_models_raises_error(self) -> None:
        """Missing models section raises ConfigError."""
        with pytest.raises(ConfigError, match="models"):
            load_config_from_dict({})

    def test_negative_fixed_cost_raises_error(self) -> None:
        """Negative fixed cost values raise pydantic ValidationError."""
        with pytest.raises(ValidationError):
            load_config_from_dict(
                {
                    "models": {"_default": "input_tokens * 1"},
                    "fixed": {"bad_job": -5},
                }
            )

    def test_tool_specific_costs(self) -> None:
        """Tool-specific expression strings are stored correctly."""
        config = load_config_from_dict(
            {
                "models": {"_default": "input_tokens * 1"},
                "tools": {"_default": "tool_calls * 0", "web_search": "web_search_calls * 2"},
            }
        )
        assert config.tools["web_search"] == "web_search_calls * 2"

    # ── WS2: this_tool_calls is valid only inside tools expressions ────────

    def test_this_tool_calls_valid_in_tools_expression(self) -> None:
        """WS2 — this_tool_calls is a known variable inside tools.* expressions."""
        config = load_config_from_dict(
            {
                "models": {"_default": "input_tokens * 1"},
                "tools": {"code_exec": "this_tool_calls * 10 / 1000", "_default": "this_tool_calls * 5 / 1000"},
            }
        )
        assert config.tools["code_exec"] == "this_tool_calls * 10 / 1000"

    def test_this_tool_calls_rejected_in_models_expression(self) -> None:
        """WS2 — this_tool_calls is NOT a global metric; using it in models.* fails."""
        with pytest.raises(ConfigError, match="unknown variable"):
            load_config_from_dict({"models": {"_default": "this_tool_calls * 1"}})

    def test_this_tool_calls_rejected_in_search_expression(self) -> None:
        """WS2 — this_tool_calls is NOT valid in a search expression either."""
        with pytest.raises(ConfigError, match="unknown variable"):
            load_config_from_dict(
                {
                    "models": {"_default": "input_tokens * 1"},
                    "search": "this_tool_calls * 1",
                }
            )

    def test_this_tool_calls_rejected_in_cache_expression(self) -> None:
        """WS2 — this_tool_calls is NOT valid in a cache expression either."""
        with pytest.raises(ConfigError, match="unknown variable"):
            load_config_from_dict(
                {
                    "models": {"_default": "input_tokens * 1"},
                    "cache": "this_tool_calls * 1",
                }
            )

    def test_fixed_costs_are_positive(self) -> None:
        """Positive fixed cost values are accepted."""
        config = load_config_from_dict(
            {
                "models": {"_default": "input_tokens * 1"},
                "fixed": {"batch_job": 20, "slow_job": 10},
            }
        )
        assert config.fixed["batch_job"] == 20

    # ── WS3: fractional (Decimal) fixed-job costs ──────────────────────────

    def test_fractional_fixed_cost_accepted(self) -> None:
        """WS3 — a fractional fixed cost like 2.5 is accepted and stored exactly."""
        config = load_config_from_dict(
            {
                "models": {"_default": "input_tokens * 1"},
                "fixed": {"partial_job": 2.5},
            }
        )
        assert config.fixed["partial_job"] == Decimal("2.5")
        assert isinstance(config.fixed["partial_job"], Decimal)

    def test_fractional_fixed_cost_string_accepted(self) -> None:
        """WS3 — a string fractional value coerces to an exact Decimal."""
        config = load_config_from_dict(
            {
                "models": {"_default": "input_tokens * 1"},
                "fixed": {"partial_job": "0.75"},
            }
        )
        assert config.fixed["partial_job"] == Decimal("0.75")

    def test_min_balance_is_decimal(self) -> None:
        """min_balance is a Decimal money field (contract §1)."""
        config = load_config_from_dict({"models": {"_default": "input_tokens * 1"}, "min_balance": 10})
        assert config.min_balance == Decimal(10)
        assert isinstance(config.min_balance, Decimal)

    def test_min_balance_default_is_decimal(self) -> None:
        """WS6 — min_balance defaults to 0 (was 5)."""
        config = load_config_from_dict({"models": {"_default": "input_tokens * 1"}})
        assert config.min_balance == Decimal(0)
        assert isinstance(config.min_balance, Decimal)

    def test_negative_min_balance_rejected(self) -> None:
        with pytest.raises(ValidationError):
            load_config_from_dict({"models": {"_default": "input_tokens * 1"}, "min_balance": -1})

    def test_fractional_min_balance(self) -> None:
        config = load_config_from_dict({"models": {"_default": "input_tokens * 1"}, "min_balance": "2.5"})
        assert config.min_balance == Decimal("2.5")

    def test_unknown_variable_rejected_at_config_load(self) -> None:
        """A typo'd metric variable fails at config-load, not runtime (M5)."""
        with pytest.raises(ConfigError, match="unknown variable"):
            load_config_from_dict({"models": {"_default": "inputtokens * 0.001"}})

    def test_known_variables_accepted(self) -> None:
        config = load_config_from_dict(
            {
                "models": {"_default": "input_tokens * 0.001 + output_tokens * 0.003"},
                "search": "search_queries * 0.5 + search_results * 0.05",
                "cache": "-cache_read_tokens * 0.0045",
            }
        )
        assert config.models["_default"]

    def test_pow_expression_rejected_at_config_load(self) -> None:
        with pytest.raises(ConfigError, match="invalid expression"):
            load_config_from_dict({"models": {"_default": "input_tokens ** 2"}})

    def test_plan_missing_name_raises_config_error(self) -> None:
        """A plan dict without 'name' raises ConfigError, not KeyError."""
        with pytest.raises(ConfigError, match="missing required 'name'"):
            load_config_from_dict(
                {
                    "models": {"_default": "input_tokens * 1"},
                    "plans": {"pro": {"id": "pro", "free_allowance": 100}},
                }
            )

    def test_duplicate_plan_names_raises(self) -> None:
        with pytest.raises(ConfigError, match="duplicate plan names"):
            load_config_from_dict(
                {
                    "models": {"_default": "input_tokens * 1"},
                    "plans": {
                        "a": {"id": "a", "name": "Pro"},
                        "b": {"id": "b", "name": "Pro"},
                    },
                }
            )

    def test_pricing_config_field_alignment(self) -> None:
        """PricingConfig and PricingConfigData fields stay in sync.

        Prevents silent data loss when fields are added to one model but not the other.
        The validated config (PricingConfig) and raw data model (PricingConfigData)
        must share the same set of fields for reliable round-trip through stores.
        """
        config_fields = set(PricingConfig.model_fields.keys())
        data_fields = set(PricingConfigData.model_fields.keys())
        assert config_fields == data_fields, (
            f"Field drift: PricingConfig has {config_fields - data_fields}, "
            f"PricingConfigData has {data_fields - config_fields}"
        )

    # ── SB1: signup_bonus default and validation ──────────────────────────

    def test_signup_bonus_defaults_to_50(self) -> None:
        """signup_bonus defaults to 50 (millicredits = $0.05)."""
        config = load_config_from_dict({"models": {"_default": "input_tokens * 1"}})
        assert config.signup_bonus == 50

    def test_signup_bonus_custom_value(self) -> None:
        """signup_bonus accepts a custom positive int."""
        config = load_config_from_dict({"models": {"_default": "input_tokens * 1"}, "signup_bonus": 200})
        assert config.signup_bonus == 200

    def test_signup_bonus_negative_rejected(self) -> None:
        """Negative signup_bonus raises ValidationError."""
        with pytest.raises(ValidationError):
            load_config_from_dict({"models": {"_default": "input_tokens * 1"}, "signup_bonus": -1})

    def test_signup_bonus_zero_accepted(self) -> None:
        """signup_bonus of 0 is valid (no signup bonus)."""
        config = load_config_from_dict({"models": {"_default": "input_tokens * 1"}, "signup_bonus": 0})
        assert config.signup_bonus == 0

    # ── CF1: Plan rate_overrides accepted ──────────────────────────────────

    def test_plan_rate_overrides_accepted(self) -> None:
        """CF1 — A plan with rate_overrides is loaded without error."""
        config = load_config_from_dict(
            {
                "models": {"_default": "input_tokens * 1"},
                "plans": {
                    "pro": {
                        "id": "pro",
                        "name": "Pro",
                        "rate_overrides": {"gpt-4": "input_tokens * 0.003"},
                    }
                },
            }
        )
        assert config.plans is not None
        plan = config.plans["pro"]
        assert plan.rate_overrides == {"gpt-4": "input_tokens * 0.003"}

    # ── CF2: Plan free_allowance negative is rejected ──────────────────────

    def test_plan_negative_free_allowance_rejected(self) -> None:
        """CF2 — free_allowance: -10 in a plan raises a validation error."""
        with pytest.raises((ConfigError, ValidationError)):
            load_config_from_dict(
                {
                    "models": {"_default": "input_tokens * 1"},
                    "plans": {
                        "cheap": {
                            "id": "cheap",
                            "name": "Cheap",
                            "free_allowance": -10,
                        }
                    },
                }
            )

    # ── CF3: Version field — only 1 is valid ──────────────────────────────

    def test_version_2_rejected(self) -> None:
        """CF3 — config with version: 2 raises a validation error (Literal[1])."""
        with pytest.raises(ValidationError):
            load_config_from_dict(
                {
                    "version": 2,
                    "models": {"_default": "input_tokens * 1"},
                }
            )

    # ── CF4: Empty sections are allowed ────────────────────────────────────

    def test_empty_sections_allowed(self) -> None:
        """CF4 — Empty tools/fixed and omitted search/cache are valid when models is present.

        search/cache are single expression strings (WS1); their "empty" state is
        the default ``None``, not an empty dict.
        """
        config = load_config_from_dict(
            {
                "models": {"_default": "input_tokens * 1"},
                "tools": {},
                "fixed": {},
            }
        )
        assert config.tools == {}
        assert config.search is None
        assert config.cache is None
        assert config.fixed == {}

    def test_search_and_cache_none_when_omitted(self) -> None:
        """WS1 — search/cache default to None (not an empty dict) when omitted."""
        config = load_config_from_dict({"models": {"_default": "input_tokens * 1"}})
        assert config.search is None
        assert config.cache is None

    def test_search_as_dict_rejected(self) -> None:
        """WS1 — search/cache must be a single expression string, not a dict."""
        with pytest.raises(ValidationError):
            load_config_from_dict(
                {
                    "models": {"_default": "input_tokens * 1"},
                    "search": {"costs": "search_queries * 1"},
                }
            )

    # ── CF5: Plan with features: null ──────────────────────────────────────

    def test_plan_features_null_is_valid(self) -> None:
        """CF5 — A plan with features: null is valid and returns empty features dict."""
        config = load_config_from_dict(
            {
                "models": {"_default": "input_tokens * 1"},
                "plans": {
                    "basic": {
                        "id": "basic",
                        "name": "Basic",
                        "features": None,
                    }
                },
            }
        )
        assert config.plans is not None
        plan = config.plans["basic"]
        assert plan.features is None

        # Verify that get_user_plan returns empty features (not None) for such a plan.
        from bursar import MemoryStore
        from bursar.interface.models import PlanDefinition, PricingConfigData

        store = MemoryStore()
        store.set_active_pricing(
            PricingConfigData(
                models={"_default": "1"},
                plans={"basic": PlanDefinition(id="basic", name="Basic", features=None)},
            )
        )
        store.set_user_plan("user-1", "basic")
        result = store.get_user_plan("user-1")
        assert result.features == {}

    # ── CF6: Duplicate plan names rejected ─────────────────────────────────
    # (already covered by test_duplicate_plan_names_raises above)

    # ── CF7: min_balance string coerces to Decimal ─────────────────────────

    def test_min_balance_string_coerces_to_decimal(self) -> None:
        """CF7 — min_balance: '10' (string) is coerced to Decimal('10') without error."""
        config = load_config_from_dict(
            {
                "models": {"_default": "input_tokens * 1"},
                "min_balance": "10",
            }
        )
        assert config.min_balance == Decimal("10")
        assert isinstance(config.min_balance, Decimal)

    # ── CF8: Empty plans dict ──────────────────────────────────────────────

    def test_empty_plans_dict_is_valid(self) -> None:
        """CF8 — plans: {} (empty dict) is a valid config, not an error."""
        config = load_config_from_dict(
            {
                "version": 1,
                "models": {"_default": "input_tokens * 0"},
                "plans": {},
            }
        )
        assert config.plans == {}

    # ── CF9: Version edge cases ────────────────────────────────────────────

    def test_version_zero_rejected(self) -> None:
        """CF9a — version: 0 must be rejected (only Literal[1] is valid)."""
        with pytest.raises(ValidationError):
            load_config_from_dict({"version": 0, "models": {"_default": "input_tokens * 1"}})

    def test_version_two_rejected(self) -> None:
        """CF9b — version: 2 must be rejected."""
        with pytest.raises(ValidationError):
            load_config_from_dict({"version": 2, "models": {"_default": "input_tokens * 1"}})

    def test_version_string_one_rejected(self) -> None:
        """CF9c — version: '1' (string) must be rejected; Literal[1] is int-only."""
        with pytest.raises(ValidationError):
            load_config_from_dict({"version": "1", "models": {"_default": "input_tokens * 1"}})

    def test_version_none_rejected(self) -> None:
        """CF9d — version: null must be rejected."""
        with pytest.raises(ValidationError):
            load_config_from_dict({"version": None, "models": {"_default": "input_tokens * 1"}})

    # ── CF10: Variable name collision with builtins ────────────────────────

    def test_builtin_name_as_metric_variable_rejected_at_config_load(self) -> None:
        """CF10 — 'ceil' is not a metric variable; using it as one in an expression
        is rejected at config-load time because 'ceil' is not in METRIC_VARIABLES.

        Specifically: 'ceil' is treated as a function reference in _SAFE_NAMES,
        so the expression 'ceil * 0.001' has no user-supplied variable references
        and triggers the 'expression references no variables' guard.
        """
        with pytest.raises(ConfigError, match="invalid expression"):
            load_config_from_dict({"models": {"_default": "ceil * 0.001"}})

    def test_builtin_name_in_call_position_uses_builtin_not_variable(self) -> None:
        """CF10b — 'ceil' in call position always invokes the builtin function.

        When 'ceil(input_tokens * 0.5)' appears in a config expression, the AST
        evaluator resolves 'ceil' to the safe builtin _ceil function, never to
        any hypothetical variable named 'ceil'.  This confirms the function
        namespace is isolated and cannot be shadowed by metric variables.
        """
        # This must load and evaluate cleanly.
        config = load_config_from_dict({"models": {"_default": "ceil(input_tokens * 0.5)"}})
        assert "ceil" in config.models["_default"]

        # Verify at evaluation time the builtin is used correctly.
        from bursar.engine import PricingEngine
        from bursar.metrics import UsageMetrics

        engine = PricingEngine.from_dict({"models": {"_default": "ceil(input_tokens * 0.5)"}})
        result = engine.calculate(UsageMetrics(model="_default", input_tokens=11))
        # ceil(11 * 0.5) = ceil(5.5) = 6
        assert result.total == Decimal("6.0000")

    # ── WS9: PlanDefinition.allowance_period ────────────────────────────────

    def test_plan_allowance_period_calendar_month_loads(self) -> None:
        config = load_config_from_dict(
            {
                "models": {"_default": "input_tokens * 1"},
                "plans": {"pro": {"id": "pro", "name": "Pro", "allowance_period": "calendar_month"}},
            }
        )
        assert config.plans is not None
        assert config.plans["pro"].allowance_period == "calendar_month"

    def test_plan_allowance_period_rolling_30d_loads(self) -> None:
        config = load_config_from_dict(
            {
                "models": {"_default": "input_tokens * 1"},
                "plans": {"pro": {"id": "pro", "name": "Pro", "allowance_period": "rolling_30d"}},
            }
        )
        assert config.plans is not None
        assert config.plans["pro"].allowance_period == "rolling_30d"

    def test_plan_allowance_period_anniversary_loads(self) -> None:
        config = load_config_from_dict(
            {
                "models": {"_default": "input_tokens * 1"},
                "plans": {"pro": {"id": "pro", "name": "Pro", "allowance_period": "anniversary"}},
            }
        )
        assert config.plans is not None
        assert config.plans["pro"].allowance_period == "anniversary"

    def test_plan_allowance_period_invalid_value_rejected(self) -> None:
        with pytest.raises(ValidationError):
            load_config_from_dict(
                {
                    "models": {"_default": "input_tokens * 1"},
                    "plans": {"pro": {"id": "pro", "name": "Pro", "allowance_period": "weekly"}},
                }
            )

    def test_plan_allowance_period_defaults_to_calendar_month(self) -> None:
        config = load_config_from_dict(
            {
                "models": {"_default": "input_tokens * 1"},
                "plans": {"pro": {"id": "pro", "name": "Pro"}},
            }
        )
        assert config.plans is not None
        assert config.plans["pro"].allowance_period == "calendar_month"

    def test_pricing_config_field_alignment_unaffected_by_allowance_period(self) -> None:
        """allowance_period is nested inside PlanDefinition, not a top-level
        PricingConfig/PricingConfigData field, so the field-parity test
        (test_pricing_config_field_alignment) must still pass."""
        config_fields = set(PricingConfig.model_fields.keys())
        data_fields = set(PricingConfigData.model_fields.keys())
        assert config_fields == data_fields
        assert "allowance_period" not in config_fields
        assert "allowance_period" not in data_fields


# ── Credit tiers: TierDefinition / tiers-section Pydantic validation ────────
#
# These are the config.py-level (Pydantic) validation tests, distinct from
# the store-runtime checks in test_tiers.py's TestTierConfigValidation (which
# also documents where PricingConfigData/MemoryStore.set_active_pricing do
# NOT perform this validation themselves).


class TestTierConfigValidation:
    def test_minimal_tier_loads_with_defaults(self) -> None:
        config = load_config_from_dict(
            {
                "models": {"_default": "input_tokens * 1"},
                "tiers": {"gifted": {"name": "Gifted", "priority": 10}},
            }
        )
        assert config.tiers is not None
        tier = config.tiers["gifted"]
        assert tier.name == "Gifted"
        assert tier.priority == 10
        assert tier.expires is False
        assert tier.default_ttl_days is None
        assert tier.allow_overdraft is False
        assert tier.is_default is False

    def test_full_tier_fields_load_correctly(self) -> None:
        config = load_config_from_dict(
            {
                "models": {"_default": "input_tokens * 1"},
                "tiers": {
                    "gifted": {
                        "name": "Gifted Credits",
                        "priority": 10,
                        "expires": True,
                        "default_ttl_days": 30,
                    },
                    "purchased": {
                        "name": "Purchased Credits",
                        "priority": 30,
                        "is_default": True,
                        "allow_overdraft": True,
                    },
                },
            }
        )
        assert config.tiers is not None
        gifted = config.tiers["gifted"]
        assert gifted.expires is True
        assert gifted.default_ttl_days == 30
        purchased = config.tiers["purchased"]
        assert purchased.is_default is True
        assert purchased.allow_overdraft is True

    def test_tier_missing_name_rejected(self) -> None:
        with pytest.raises(ValidationError):
            load_config_from_dict(
                {
                    "models": {"_default": "input_tokens * 1"},
                    "tiers": {"a": {"priority": 1}},
                }
            )

    def test_tier_missing_priority_rejected(self) -> None:
        with pytest.raises(ValidationError):
            load_config_from_dict(
                {
                    "models": {"_default": "input_tokens * 1"},
                    "tiers": {"a": {"name": "A"}},
                }
            )

    def test_tiers_not_a_dict_rejected(self) -> None:
        with pytest.raises(ConfigError, match="tiers must be a dict"):
            load_config_from_dict(
                {
                    "models": {"_default": "input_tokens * 1"},
                    "tiers": ["not", "a", "dict"],
                }
            )

    def test_empty_tiers_dict_rejected(self) -> None:
        """An explicit tiers: {} is ambiguous — omit the key entirely for
        "no tiers configured" instead."""
        with pytest.raises(ConfigError, match="empty dict"):
            load_config_from_dict(
                {
                    "models": {"_default": "input_tokens * 1"},
                    "tiers": {},
                }
            )

    def test_omitted_tiers_is_valid_and_none(self) -> None:
        config = load_config_from_dict({"models": {"_default": "input_tokens * 1"}})
        assert config.tiers is None

    def test_duplicate_allow_overdraft_rejected(self) -> None:
        with pytest.raises(ConfigError, match="allow_overdraft"):
            load_config_from_dict(
                {
                    "models": {"_default": "input_tokens * 1"},
                    "tiers": {
                        "a": {"name": "A", "priority": 1, "allow_overdraft": True},
                        "b": {"name": "B", "priority": 2, "allow_overdraft": True},
                    },
                }
            )

    def test_single_allow_overdraft_accepted(self) -> None:
        config = load_config_from_dict(
            {
                "models": {"_default": "input_tokens * 1"},
                "tiers": {
                    "a": {"name": "A", "priority": 1, "allow_overdraft": True},
                    "b": {"name": "B", "priority": 2},
                },
            }
        )
        assert config.tiers is not None
        assert config.tiers["a"].allow_overdraft is True
        assert config.tiers["b"].allow_overdraft is False

    def test_duplicate_is_default_rejected(self) -> None:
        with pytest.raises(ConfigError, match="is_default"):
            load_config_from_dict(
                {
                    "models": {"_default": "input_tokens * 1"},
                    "tiers": {
                        "a": {"name": "A", "priority": 1, "is_default": True},
                        "b": {"name": "B", "priority": 2, "is_default": True},
                    },
                }
            )

    def test_single_is_default_accepted(self) -> None:
        config = load_config_from_dict(
            {
                "models": {"_default": "input_tokens * 1"},
                "tiers": {
                    "a": {"name": "A", "priority": 1, "is_default": True},
                    "b": {"name": "B", "priority": 2},
                },
            }
        )
        assert config.tiers is not None
        assert config.tiers["a"].is_default is True

    def test_default_ttl_days_zero_rejected(self) -> None:
        with pytest.raises(ConfigError, match="default_ttl_days"):
            load_config_from_dict(
                {
                    "models": {"_default": "input_tokens * 1"},
                    "tiers": {"a": {"name": "A", "priority": 1, "expires": True, "default_ttl_days": 0}},
                }
            )

    def test_default_ttl_days_negative_rejected(self) -> None:
        with pytest.raises(ConfigError, match="default_ttl_days"):
            load_config_from_dict(
                {
                    "models": {"_default": "input_tokens * 1"},
                    "tiers": {"a": {"name": "A", "priority": 1, "expires": True, "default_ttl_days": -1}},
                }
            )

    def test_default_ttl_days_positive_accepted(self) -> None:
        config = load_config_from_dict(
            {
                "models": {"_default": "input_tokens * 1"},
                "tiers": {"a": {"name": "A", "priority": 1, "expires": True, "default_ttl_days": 1}},
            }
        )
        assert config.tiers is not None
        assert config.tiers["a"].default_ttl_days == 1

    def test_default_ttl_days_none_is_valid_even_when_expires_true(self) -> None:
        """A tier may expire with no default_ttl_days — add_credits() must
        then always be called with an explicit expires_at (enforced at the
        store layer, see test_tiers.py)."""
        config = load_config_from_dict(
            {
                "models": {"_default": "input_tokens * 1"},
                "tiers": {"a": {"name": "A", "priority": 1, "expires": True}},
            }
        )
        assert config.tiers is not None
        assert config.tiers["a"].expires is True
        assert config.tiers["a"].default_ttl_days is None

    def test_ties_in_priority_are_not_an_error(self) -> None:
        """Priority ties are broken by key ascending at the store layer, not
        rejected at config-load time."""
        config = load_config_from_dict(
            {
                "models": {"_default": "input_tokens * 1"},
                "tiers": {
                    "a": {"name": "A", "priority": 5},
                    "b": {"name": "B", "priority": 5},
                },
            }
        )
        assert config.tiers is not None
        assert config.tiers["a"].priority == config.tiers["b"].priority == 5

    def test_pricing_config_data_round_trips_tiers(self) -> None:
        """PricingConfigData (the store-facing raw model) carries the same
        tiers shape as the validated PricingConfig."""
        raw = {
            "models": {"_default": "input_tokens * 1"},
            "tiers": {"gifted": {"name": "Gifted", "priority": 10, "expires": True, "default_ttl_days": 7}},
        }
        validated = load_config_from_dict(raw)
        data = PricingConfigData.model_validate(raw)
        assert validated.tiers is not None
        assert data.tiers is not None
        assert validated.tiers["gifted"].model_dump() == data.tiers["gifted"].model_dump()
