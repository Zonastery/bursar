"""Tests for pricing config parsing and validation."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from bursar.config import (
    BursarConfig,
    ConfigError,
    load_config_from_dict,
)


class TestConfigValidation:
    """Tests for config loading and validation."""

    def test_valid_full_config(self) -> None:
        """Loading a full config dict populates all sections."""
        config = load_config_from_dict(
            {
                "version": 1,
                "metering": {
                    "models": {"gpt-4": "input_tokens * 0.01"},
                    "tools": {"*": "tool_calls * 0.1"},
                },
                "ledger": {"min_balance": "0"},
            }
        )
        assert config.metering.models["gpt-4"] == "input_tokens * 0.01"

    def test_minimal_config(self) -> None:
        """Minimal config with only version, metering, and ledger works."""
        config = load_config_from_dict(
            {
                "version": 1,
                "metering": {
                    "models": {"*": "input_tokens * 0.001"},
                },
                "ledger": {"min_balance": "0"},
            }
        )
        assert config.metering.models["*"] == "input_tokens * 0.001"

    def test_invalid_expression_raises_error(self) -> None:
        """An expression with disallowed syntax raises ConfigError."""
        with pytest.raises(ConfigError, match="invalid expression"):
            load_config_from_dict(
                {
                    "version": 1,
                    "metering": {
                        "models": {"gpt-4": "lambda x: x"},
                    },
                    "ledger": {"min_balance": "0"},
                }
            )

    def test_missing_metering_raises_error(self) -> None:
        """Missing metering section raises ConfigError."""
        with pytest.raises(ConfigError, match="metering"):
            load_config_from_dict({"version": 1, "ledger": {"min_balance": "0"}})

    def test_negative_flat_job_raises_error(self) -> None:
        """Negative flat_jobs values raise pydantic ValidationError."""
        with pytest.raises((ConfigError, ValidationError)):
            load_config_from_dict(
                {
                    "version": 1,
                    "metering": {
                        "models": {"*": "input_tokens * 1"},
                        "flat_jobs": {"bad_job": -5},
                    },
                    "ledger": {"min_balance": "0"},
                }
            )

    def test_tool_specific_costs(self) -> None:
        """Tool-specific expression strings are stored correctly."""
        config = load_config_from_dict(
            {
                "version": 1,
                "metering": {
                    "models": {"*": "input_tokens * 1"},
                    "tools": {"*": "tool_calls * 0", "web_search": "web_search_calls * 2"},
                },
                "ledger": {"min_balance": "0"},
            }
        )
        assert config.metering.tools["web_search"] == "web_search_calls * 2"

    # ── WS2: this_tool_calls is valid only inside tools expressions ────────

    def test_calls_variable_in_tools_expression(self) -> None:
        """WS2 — calls is a per-tool count variable inside tools expressions."""
        config = load_config_from_dict(
            {
                "version": 1,
                "metering": {
                    "models": {"*": "input_tokens * 1"},
                    "tools": {"code_exec": "calls * 10 / 1000", "*": "calls * 5 / 1000"},
                },
                "ledger": {"min_balance": "0"},
            }
        )
        assert config.metering.tools["code_exec"] == "calls * 10 / 1000"

    def test_calls_rejected_in_models_expression(self) -> None:
        """WS2 — calls is NOT a global metric; using it in models.* fails."""
        with pytest.raises(ConfigError, match="unknown variable"):
            load_config_from_dict(
                {
                    "version": 1,
                    "metering": {"models": {"*": "calls * 1"}},
                    "ledger": {"min_balance": "0"},
                }
            )

    def test_calls_rejected_in_search_expression(self) -> None:
        """WS2 — calls is NOT valid in a search expression either."""
        with pytest.raises(ConfigError, match="unknown variable"):
            load_config_from_dict(
                {
                    "version": 1,
                    "metering": {
                        "models": {"*": "input_tokens * 1"},
                        "search": "calls * 1",
                    },
                    "ledger": {"min_balance": "0"},
                }
            )

    def test_calls_rejected_in_cache_expression(self) -> None:
        """WS2 — calls is NOT valid in a cache expression either."""
        with pytest.raises(ConfigError, match="unknown variable"):
            load_config_from_dict(
                {
                    "version": 1,
                    "metering": {
                        "models": {"*": "input_tokens * 1"},
                        "cache_discount": "calls * 1",
                    },
                    "ledger": {"min_balance": "0"},
                }
            )

    def test_flat_jobs_are_positive(self) -> None:
        """Positive flat_jobs values are accepted."""
        config = load_config_from_dict(
            {
                "version": 1,
                "metering": {
                    "models": {"*": "input_tokens * 1"},
                    "flat_jobs": {"batch_job": 20, "slow_job": 10},
                },
                "ledger": {"min_balance": "0"},
            }
        )
        assert config.metering.flat_jobs["batch_job"] == 20

    # ── WS3: fractional (Decimal) flat-job costs ──────────────────────────

    def test_fractional_flat_job_cost_accepted(self) -> None:
        """WS3 — a fractional flat_job cost like 2.5 is accepted and stored exactly."""
        config = load_config_from_dict(
            {
                "version": 1,
                "metering": {
                    "models": {"*": "input_tokens * 1"},
                    "flat_jobs": {"partial_job": 2.5},
                },
                "ledger": {"min_balance": "0"},
            }
        )
        assert config.metering.flat_jobs["partial_job"] == Decimal("2.5")
        assert isinstance(config.metering.flat_jobs["partial_job"], Decimal)

    def test_fractional_flat_job_cost_string_accepted(self) -> None:
        """WS3 — a string fractional value coerces to an exact Decimal."""
        config = load_config_from_dict(
            {
                "version": 1,
                "metering": {
                    "models": {"*": "input_tokens * 1"},
                    "flat_jobs": {"partial_job": "0.75"},
                },
                "ledger": {"min_balance": "0"},
            }
        )
        assert config.metering.flat_jobs["partial_job"] == Decimal("0.75")

    def test_min_balance_is_decimal(self) -> None:
        """min_balance is a Decimal money field (contract §1)."""
        config = load_config_from_dict(
            {
                "version": 1,
                "metering": {"models": {"*": "input_tokens * 1"}},
                "ledger": {"min_balance": 10},
            }
        )
        assert config.ledger.min_balance == Decimal(10)
        assert isinstance(config.ledger.min_balance, Decimal)

    def test_min_balance_default_is_decimal(self) -> None:
        """WS6 — min_balance defaults to 0."""
        config = load_config_from_dict(
            {
                "version": 1,
                "metering": {"models": {"*": "input_tokens * 1"}},
                "ledger": {},
            }
        )
        assert config.ledger.min_balance == Decimal(0)
        assert isinstance(config.ledger.min_balance, Decimal)

    def test_negative_min_balance_rejected(self) -> None:
        with pytest.raises(ValidationError):
            load_config_from_dict(
                {
                    "version": 1,
                    "metering": {"models": {"*": "input_tokens * 1"}},
                    "ledger": {"min_balance": -1},
                }
            )

    def test_fractional_min_balance(self) -> None:
        config = load_config_from_dict(
            {
                "version": 1,
                "metering": {"models": {"*": "input_tokens * 1"}},
                "ledger": {"min_balance": "2.5"},
            }
        )
        assert config.ledger.min_balance == Decimal("2.5")

    def test_unknown_variable_rejected_at_config_load(self) -> None:
        """A typo'd metric variable fails at config-load, not runtime (M5)."""
        with pytest.raises(ConfigError, match="unknown variable"):
            load_config_from_dict(
                {
                    "version": 1,
                    "metering": {"models": {"*": "inputtokens * 0.001"}},
                    "ledger": {"min_balance": "0"},
                }
            )

    def test_known_variables_accepted(self) -> None:
        config = load_config_from_dict(
            {
                "version": 1,
                "metering": {
                    "models": {"*": "input_tokens * 0.001 + output_tokens * 0.003"},
                    "search": "search_queries * 0.5 + search_results * 0.05",
                    "cache_discount": "-cache_read_tokens * 0.0045",
                },
                "ledger": {"min_balance": "0"},
            }
        )
        assert config.metering.models["*"]

    def test_pow_expression_rejected_at_config_load(self) -> None:
        with pytest.raises(ConfigError, match="invalid expression"):
            load_config_from_dict(
                {
                    "version": 1,
                    "metering": {"models": {"*": "input_tokens ** 2"}},
                    "ledger": {"min_balance": "0"},
                }
            )

    def test_plan_missing_label_raises_config_error(self) -> None:
        """A plan dict without 'label' raises ConfigError."""
        with pytest.raises(ConfigError, match="missing required 'label'"):
            load_config_from_dict(
                {
                    "version": 1,
                    "metering": {"models": {"*": "input_tokens * 1"}},
                    "ledger": {"min_balance": "0"},
                    "plans": {"pro": {"id": "pro", "label": None}},
                }
            )

    def test_duplicate_plan_labels_raises(self) -> None:
        with pytest.raises(ConfigError, match="duplicate plan labels"):
            load_config_from_dict(
                {
                    "version": 1,
                    "metering": {"models": {"*": "input_tokens * 1"}},
                    "ledger": {"min_balance": "0"},
                    "plans": {
                        "a": {"label": "Pro"},
                        "b": {"label": "Pro"},
                    },
                }
            )

    def test_bursar_config_field_alignment(self) -> None:
        """BursarConfig fields match the expected top-level schema keys.

        Prevents silent drift when fields are added to the model.
        """
        expected_top_level = {"version", "metering", "ledger", "plans", "billing"}
        config_fields = set(BursarConfig.model_fields.keys())
        assert config_fields == expected_top_level, (
            f"Field drift: BursarConfig has {config_fields - expected_top_level}, "
            f"expected {expected_top_level - config_fields}"
        )

    # ── SB1: signup_bonus default and validation ──────────────────────────

    def test_signup_grant_defaults_to_none(self) -> None:
        """Omitted signup_grant disables signup bonuses."""
        config = load_config_from_dict(
            {
                "version": 1,
                "metering": {"models": {"*": "input_tokens * 1"}},
                "ledger": {},
            }
        )
        assert config.ledger.signup_grant is None

    def test_signup_grant_object_accepted(self) -> None:
        config = load_config_from_dict(
            {
                "version": 1,
                "metering": {"models": {"*": "input_tokens * 1"}},
                "ledger": {
                    "signup_grant": {"amount": 200, "bucket": "gifted"},
                    "buckets": {
                        "gifted": {"label": "Gifted", "priority": 10, "expires": True, "ttl_days": 7},
                        "purchased": {"label": "Purchased", "priority": 20, "default": True},
                    },
                },
            }
        )
        assert config.ledger.signup_grant is not None
        assert config.ledger.signup_grant.amount == 200
        assert config.ledger.signup_grant.bucket == "gifted"

    def test_signup_grant_scalar_rejected(self) -> None:
        with pytest.raises(ConfigError, match="object"):
            load_config_from_dict(
                {
                    "version": 1,
                    "metering": {"models": {"*": "input_tokens * 1"}},
                    "ledger": {"signup_grant": 200},
                }
            )

    def test_signup_grant_negative_amount_rejected(self) -> None:
        with pytest.raises(ValidationError):
            load_config_from_dict(
                {
                    "version": 1,
                    "metering": {"models": {"*": "input_tokens * 1"}},
                    "ledger": {
                        "signup_grant": {"amount": -1, "bucket": "gifted"},
                        "buckets": {"gifted": {"label": "Gifted", "priority": 10}},
                    },
                }
            )

    def test_signup_grant_unknown_bucket_rejected(self) -> None:
        with pytest.raises(ConfigError, match="unknown bucket"):
            load_config_from_dict(
                {
                    "version": 1,
                    "metering": {"models": {"*": "input_tokens * 1"}},
                    "ledger": {
                        "signup_grant": {"amount": 50, "bucket": "missing"},
                        "buckets": {"gifted": {"label": "Gifted", "priority": 10}},
                    },
                }
            )

    # ── CF1: Plan rate_overrides accepted ──────────────────────────────────

    def test_plan_rate_overrides_accepted(self) -> None:
        """CF1 — A plan with rate_overrides is loaded without error."""
        config = load_config_from_dict(
            {
                "version": 1,
                "metering": {"models": {"*": "input_tokens * 1", "gpt-4": "input_tokens * 0.01"}},
                "ledger": {"min_balance": "0"},
                "plans": {
                    "pro": {
                        "label": "Pro",
                        "rate_overrides": {"gpt-4": "input_tokens * 0.003"},
                    }
                },
            }
        )
        assert config.plans is not None
        plan = config.plans["pro"]
        assert plan.rate_overrides == {"gpt-4": "input_tokens * 0.003"}

    # ── CF2: Plan free_allowance negative is rejected ──────────────────────

    def test_plan_negative_allowance_rejected(self) -> None:
        """CF2 — allowance.amount: -10 in a plan raises a validation error."""
        with pytest.raises((ConfigError, ValidationError)):
            load_config_from_dict(
                {
                    "version": 1,
                    "metering": {"models": {"*": "input_tokens * 1"}},
                    "ledger": {"min_balance": "0"},
                    "plans": {
                        "cheap": {
                            "label": "Cheap",
                            "allowance": {"amount": -10, "period": "calendar_month"},
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
                    "metering": {"models": {"*": "input_tokens * 1"}},
                    "ledger": {"min_balance": "0"},
                }
            )

    # ── CF4: Empty sections are allowed ────────────────────────────────────

    def test_empty_sections_allowed(self) -> None:
        """CF4 — Empty tools/flat_jobs and omitted search/cache are valid when models is present.

        search/cache_discount are single expression strings; their "empty" state is
        the default ``None``, not an empty dict. tools defaults to {"*": "calls * 0"}.
        """
        config = load_config_from_dict(
            {
                "version": 1,
                "metering": {
                    "models": {"*": "input_tokens * 1"},
                    "tools": {},
                    "flat_jobs": {},
                },
                "ledger": {"min_balance": "0"},
            }
        )
        assert config.metering.tools == {}
        assert config.metering.search is None
        assert config.metering.cache_discount is None
        assert config.metering.flat_jobs == {}

    def test_search_and_cache_none_when_omitted(self) -> None:
        """search/cache_discount default to None when omitted."""
        config = load_config_from_dict(
            {
                "version": 1,
                "metering": {"models": {"*": "input_tokens * 1"}},
                "ledger": {"min_balance": "0"},
            }
        )
        assert config.metering.search is None
        assert config.metering.cache_discount is None

    def test_search_as_dict_rejected(self) -> None:
        """search/cache_discount must be a single expression string, not a dict."""
        with pytest.raises(ValidationError):
            load_config_from_dict(
                {
                    "version": 1,
                    "metering": {
                        "models": {"*": "input_tokens * 1"},
                        "search": {"costs": "search_queries * 1"},
                    },
                    "ledger": {"min_balance": "0"},
                }
            )

    # ── CF5: Plan with entitlements: null ──────────────────────────────────

    def test_plan_entitlements_null_is_valid(self) -> None:
        """CF5 — A plan with entitlements: null is valid and returns empty entitlements dict."""
        config = load_config_from_dict(
            {
                "version": 1,
                "metering": {"models": {"*": "input_tokens * 1"}},
                "ledger": {"min_balance": "0"},
                "plans": {
                    "basic": {
                        "label": "Basic",
                        "entitlements": None,
                    }
                },
            }
        )
        assert config.plans is not None
        plan = config.plans["basic"]
        assert plan.entitlements is None

        # Verify that get_user_plan returns empty entitlements (not None) for such a plan.

    # ── CF6: Duplicate plan labels rejected ─────────────────────────────────
    # (already covered by test_duplicate_plan_labels_raises above)

    # ── CF7: min_balance string coerces to Decimal ─────────────────────────

    def test_min_balance_string_coerces_to_decimal(self) -> None:
        """CF7 — min_balance: '10' (string) is coerced to Decimal('10') without error."""
        config = load_config_from_dict(
            {
                "version": 1,
                "metering": {"models": {"*": "input_tokens * 1"}},
                "ledger": {"min_balance": "10"},
            }
        )
        assert config.ledger.min_balance == Decimal("10")
        assert isinstance(config.ledger.min_balance, Decimal)

    # ── CF8: Empty plans dict ──────────────────────────────────────────────

    def test_empty_plans_dict_is_valid(self) -> None:
        """CF8 — plans: {} (empty dict) is a valid config, not an error."""
        config = load_config_from_dict(
            {
                "version": 1,
                "metering": {"models": {"*": "input_tokens * 0"}},
                "ledger": {"min_balance": "0"},
                "plans": {},
            }
        )
        assert config.plans == {}

    # ── CF9: Version edge cases ────────────────────────────────────────────

    def test_version_zero_rejected(self) -> None:
        """CF9a — version: 0 must be rejected (only Literal[1] is valid)."""
        with pytest.raises(ValidationError):
            load_config_from_dict(
                {
                    "version": 0,
                    "metering": {"models": {"*": "input_tokens * 1"}},
                    "ledger": {"min_balance": "0"},
                }
            )

    def test_version_two_rejected(self) -> None:
        """CF9b — version: 2 must be rejected."""
        with pytest.raises(ValidationError):
            load_config_from_dict(
                {
                    "version": 2,
                    "metering": {"models": {"*": "input_tokens * 1"}},
                    "ledger": {"min_balance": "0"},
                }
            )

    def test_version_string_one_rejected(self) -> None:
        """CF9c — version: '1' (string) must be rejected; Literal[1] is int-only."""
        with pytest.raises(ValidationError):
            load_config_from_dict(
                {
                    "version": "1",
                    "metering": {"models": {"*": "input_tokens * 1"}},
                    "ledger": {"min_balance": "0"},
                }
            )

    def test_version_none_rejected(self) -> None:
        """CF9d — version: null must be rejected."""
        with pytest.raises(ValidationError):
            load_config_from_dict(
                {
                    "version": None,
                    "metering": {"models": {"*": "input_tokens * 1"}},
                    "ledger": {"min_balance": "0"},
                }
            )

    # ── CF10: Variable name collision with builtins ────────────────────────

    def test_builtin_name_as_metric_variable_rejected_at_config_load(self) -> None:
        """CF10 — 'ceil' is not a metric variable; using it as one in an expression
        is rejected at config-load time because 'ceil' is not in METRIC_VARIABLES.

        Specifically: 'ceil' is treated as a function reference in _SAFE_NAMES,
        so the expression 'ceil * 0.001' has no user-supplied variable references
        and triggers the 'expression references no variables' guard.
        """
        with pytest.raises(ConfigError, match="invalid expression"):
            load_config_from_dict(
                {
                    "version": 1,
                    "metering": {"models": {"*": "ceil * 0.001"}},
                    "ledger": {"min_balance": "0"},
                }
            )

    def test_builtin_name_in_call_position_uses_builtin_not_variable(self) -> None:
        """CF10b — 'ceil' in call position always invokes the builtin function.

        When 'ceil(input_tokens * 0.5)' appears in a config expression, the AST
        evaluator resolves 'ceil' to the safe builtin _ceil function, never to
        any hypothetical variable named 'ceil'.  This confirms the function
        namespace is isolated and cannot be shadowed by metric variables.
        """
        config = load_config_from_dict(
            {
                "version": 1,
                "metering": {"models": {"*": "ceil(input_tokens * 0.5)"}},
                "ledger": {"min_balance": "0"},
            }
        )
        assert "ceil" in config.metering.models["*"]

        # Verify at evaluation time the builtin is used correctly.
        from bursar.engine import PricingEngine
        from bursar.metrics import UsageMetrics

        engine = PricingEngine.from_dict(
            {
                "version": 1,
                "metering": {"models": {"*": "ceil(input_tokens * 0.5)"}},
                "ledger": {"min_balance": "0"},
            }
        )
        result = engine.calculate(UsageMetrics(model="*", input_tokens=11))
        # ceil(11 * 0.5) = ceil(5.5) = 6
        assert result.total == Decimal("6.0000")

    # ── WS9: PlanDefinition.allowance_period ────────────────────────────────

    def test_plan_allowance_period_calendar_month_loads(self) -> None:
        config = load_config_from_dict(
            {
                "version": 1,
                "metering": {"models": {"*": "input_tokens * 1"}},
                "ledger": {"min_balance": "0"},
                "plans": {"pro": {"label": "Pro", "allowance": {"amount": 0, "period": "calendar_month"}}},
            }
        )
        assert config.plans is not None
        assert config.plans["pro"].allowance is not None
        assert config.plans["pro"].allowance.period == "calendar_month"

    def test_plan_allowance_period_rolling_30d_loads(self) -> None:
        config = load_config_from_dict(
            {
                "version": 1,
                "metering": {"models": {"*": "input_tokens * 1"}},
                "ledger": {"min_balance": "0"},
                "plans": {"pro": {"label": "Pro", "allowance": {"amount": 0, "period": "rolling_30d"}}},
            }
        )
        assert config.plans is not None
        assert config.plans["pro"].allowance is not None
        assert config.plans["pro"].allowance.period == "rolling_30d"

    def test_plan_allowance_period_anniversary_loads(self) -> None:
        config = load_config_from_dict(
            {
                "version": 1,
                "metering": {"models": {"*": "input_tokens * 1"}},
                "ledger": {"min_balance": "0"},
                "plans": {"pro": {"label": "Pro", "allowance": {"amount": 0, "period": "anniversary"}}},
            }
        )
        assert config.plans is not None
        assert config.plans["pro"].allowance is not None
        assert config.plans["pro"].allowance.period == "anniversary"

    def test_plan_allowance_period_invalid_value_rejected(self) -> None:
        with pytest.raises(ValidationError):
            load_config_from_dict(
                {
                    "version": 1,
                    "metering": {"models": {"*": "input_tokens * 1"}},
                    "ledger": {"min_balance": "0"},
                    "plans": {"pro": {"label": "Pro", "allowance": {"amount": 0, "period": "weekly"}}},
                }
            )

    def test_plan_allowance_period_defaults_to_calendar_month(self) -> None:
        config = load_config_from_dict(
            {
                "version": 1,
                "metering": {"models": {"*": "input_tokens * 1"}},
                "ledger": {"min_balance": "0"},
                "plans": {"pro": {"label": "Pro"}},
            }
        )
        assert config.plans is not None
        assert config.plans["pro"].allowance is None

    def test_bursar_config_field_alignment_unaffected_by_allowance_period(self) -> None:
        """allowance_period is nested inside PlanDefinition, not a top-level
        BursarConfig field, so the field-parity test must still pass."""
        expected = {"version", "metering", "ledger", "plans", "billing"}
        config_fields = set(BursarConfig.model_fields.keys())
        assert config_fields == expected
        assert "allowance_period" not in config_fields


# ── Credit buckets: BucketDefinition / buckets-section Pydantic validation ───
#
# These are the config.py-level (Pydantic) validation tests, distinct from
# the store-runtime checks in test_tiers.py's TestTierConfigValidation (which
# also documents where MemoryStore.set_active_pricing do NOT perform this
# validation themselves).


class TestBucketConfigValidation:
    def test_minimal_bucket_loads_with_defaults(self) -> None:
        config = load_config_from_dict(
            {
                "version": 1,
                "metering": {"models": {"*": "input_tokens * 1"}},
                "ledger": {
                    "min_balance": "0",
                    "buckets": {"gifted": {"label": "Gifted", "priority": 10}},
                },
            }
        )
        assert config.ledger.buckets is not None
        bucket = config.ledger.buckets["gifted"]
        assert bucket.label == "Gifted"
        assert bucket.priority == 10
        assert bucket.expires is False
        assert bucket.ttl_days is None
        assert bucket.allow_overdraft is False
        assert bucket.default is False

    def test_full_bucket_fields_load_correctly(self) -> None:
        config = load_config_from_dict(
            {
                "version": 1,
                "metering": {"models": {"*": "input_tokens * 1"}},
                "ledger": {
                    "min_balance": "0",
                    "buckets": {
                        "gifted": {
                            "label": "Gifted Credits",
                            "priority": 10,
                            "expires": True,
                            "ttl_days": 30,
                        },
                        "purchased": {
                            "label": "Purchased Credits",
                            "priority": 30,
                            "default": True,
                            "allow_overdraft": True,
                        },
                    },
                },
            }
        )
        assert config.ledger.buckets is not None
        gifted = config.ledger.buckets["gifted"]
        assert gifted.expires is True
        assert gifted.ttl_days == 30
        purchased = config.ledger.buckets["purchased"]
        assert purchased.default is True
        assert purchased.allow_overdraft is True

    def test_bucket_minimal_with_defaults(self) -> None:
        """A bucket with only minimal fields is valid (label defaults to "",
        priority defaults to 0)."""
        config = load_config_from_dict(
            {
                "version": 1,
                "metering": {"models": {"*": "input_tokens * 1"}},
                "ledger": {
                    "min_balance": "0",
                    "buckets": {"a": {"label": "A"}},
                },
            }
        )
        assert config.ledger.buckets is not None
        assert config.ledger.buckets["a"].label == "A"
        assert config.ledger.buckets["a"].priority == 0

    def test_buckets_not_a_dict_rejected(self) -> None:
        with pytest.raises(ConfigError, match="buckets must be a dict"):
            load_config_from_dict(
                {
                    "version": 1,
                    "metering": {"models": {"*": "input_tokens * 1"}},
                    "ledger": {
                        "min_balance": "0",
                        "buckets": ["not", "a", "dict"],
                    },
                }
            )

    def test_empty_buckets_dict_rejected(self) -> None:
        """An explicit buckets: {} is ambiguous — omit the key entirely for
        "no buckets configured" instead."""
        with pytest.raises(ConfigError, match="empty dict"):
            load_config_from_dict(
                {
                    "version": 1,
                    "metering": {"models": {"*": "input_tokens * 1"}},
                    "ledger": {
                        "min_balance": "0",
                        "buckets": {},
                    },
                }
            )

    def test_omitted_buckets_is_valid_and_none(self) -> None:
        config = load_config_from_dict(
            {
                "version": 1,
                "metering": {"models": {"*": "input_tokens * 1"}},
                "ledger": {"min_balance": "0"},
            }
        )
        assert config.ledger.buckets is None

    def test_duplicate_allow_overdraft_rejected(self) -> None:
        with pytest.raises(ConfigError, match="allow_overdraft"):
            load_config_from_dict(
                {
                    "version": 1,
                    "metering": {"models": {"*": "input_tokens * 1"}},
                    "ledger": {
                        "min_balance": "0",
                        "buckets": {
                            "a": {"label": "A", "priority": 1, "allow_overdraft": True},
                            "b": {"label": "B", "priority": 2, "allow_overdraft": True},
                        },
                    },
                }
            )

    def test_single_allow_overdraft_accepted(self) -> None:
        config = load_config_from_dict(
            {
                "version": 1,
                "metering": {"models": {"*": "input_tokens * 1"}},
                "ledger": {
                    "min_balance": "0",
                    "buckets": {
                        "a": {"label": "A", "priority": 1, "allow_overdraft": True},
                        "b": {"label": "B", "priority": 2},
                    },
                },
            }
        )
        assert config.ledger.buckets is not None
        assert config.ledger.buckets["a"].allow_overdraft is True
        assert config.ledger.buckets["b"].allow_overdraft is False

    def test_duplicate_default_rejected(self) -> None:
        with pytest.raises(ConfigError, match="default=True"):
            load_config_from_dict(
                {
                    "version": 1,
                    "metering": {"models": {"*": "input_tokens * 1"}},
                    "ledger": {
                        "min_balance": "0",
                        "buckets": {
                            "a": {"label": "A", "priority": 1, "default": True},
                            "b": {"label": "B", "priority": 2, "default": True},
                        },
                    },
                }
            )

    def test_single_default_accepted(self) -> None:
        config = load_config_from_dict(
            {
                "version": 1,
                "metering": {"models": {"*": "input_tokens * 1"}},
                "ledger": {
                    "min_balance": "0",
                    "buckets": {
                        "a": {"label": "A", "priority": 1, "default": True},
                        "b": {"label": "B", "priority": 2},
                    },
                },
            }
        )
        assert config.ledger.buckets is not None
        assert config.ledger.buckets["a"].default is True

    def test_ttl_days_zero_rejected(self) -> None:
        with pytest.raises(ConfigError, match="ttl_days"):
            load_config_from_dict(
                {
                    "version": 1,
                    "metering": {"models": {"*": "input_tokens * 1"}},
                    "ledger": {
                        "min_balance": "0",
                        "buckets": {"a": {"label": "A", "priority": 1, "expires": True, "ttl_days": 0}},
                    },
                }
            )

    def test_ttl_days_negative_rejected(self) -> None:
        with pytest.raises(ConfigError, match="ttl_days"):
            load_config_from_dict(
                {
                    "version": 1,
                    "metering": {"models": {"*": "input_tokens * 1"}},
                    "ledger": {
                        "min_balance": "0",
                        "buckets": {"a": {"label": "A", "priority": 1, "expires": True, "ttl_days": -1}},
                    },
                }
            )

    def test_ttl_days_positive_accepted(self) -> None:
        config = load_config_from_dict(
            {
                "version": 1,
                "metering": {"models": {"*": "input_tokens * 1"}},
                "ledger": {
                    "min_balance": "0",
                    "buckets": {"a": {"label": "A", "priority": 1, "expires": True, "ttl_days": 1}},
                },
            }
        )
        assert config.ledger.buckets is not None
        assert config.ledger.buckets["a"].ttl_days == 1

    def test_ttl_days_none_is_valid_even_when_expires_true(self) -> None:
        """A bucket may expire with no ttl_days — add_credits() must
        then always be called with an explicit expires_at (enforced at the
        store layer, see test_tiers.py)."""
        config = load_config_from_dict(
            {
                "version": 1,
                "metering": {"models": {"*": "input_tokens * 1"}},
                "ledger": {
                    "min_balance": "0",
                    "buckets": {"a": {"label": "A", "priority": 1, "expires": True}},
                },
            }
        )
        assert config.ledger.buckets is not None
        assert config.ledger.buckets["a"].expires is True
        assert config.ledger.buckets["a"].ttl_days is None

    def test_ties_in_priority_are_not_an_error(self) -> None:
        """Priority ties are broken by key ascending at the store layer, not
        rejected at config-load time."""
        config = load_config_from_dict(
            {
                "version": 1,
                "metering": {"models": {"*": "input_tokens * 1"}},
                "ledger": {
                    "min_balance": "0",
                    "buckets": {
                        "a": {"label": "A", "priority": 5},
                        "b": {"label": "B", "priority": 5},
                    },
                },
            }
        )
        assert config.ledger.buckets is not None
        assert config.ledger.buckets["a"].priority == config.ledger.buckets["b"].priority == 5

    def test_bursar_config_round_trips_buckets(self) -> None:
        """BursarConfig (the validated model) carries the same
        buckets shape as the raw config dict."""
        raw = {
            "version": 1,
            "metering": {"models": {"*": "input_tokens * 1"}},
            "ledger": {
                "min_balance": "0",
                "buckets": {"gifted": {"label": "Gifted", "priority": 10, "expires": True, "ttl_days": 7}},
            },
        }
        validated = load_config_from_dict(raw)
        data = BursarConfig.model_validate(raw)
        assert validated.ledger.buckets is not None
        assert data.ledger.buckets is not None
        assert validated.ledger.buckets["gifted"].model_dump() == data.ledger.buckets["gifted"].model_dump()
