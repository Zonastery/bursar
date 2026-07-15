"""Validate documentation example configs against PricingConfig."""

from __future__ import annotations

import pytest

from bursar.config import load_config_from_dict

# Canonical examples mirrored from docs/docs/configuration.mdx — must stay in sync.
MINIMAL_CONFIG = {
    "version": 1,
    "metering": {
        "models": {
            "gpt-4": "input_tokens * 0.01 + output_tokens * 0.03",
            "*": "input_tokens * 0.001 + output_tokens * 0.003",
        },
        "tools": {"*": "calls * 5 / 1000", "code_exec": "calls * 10 / 1000"},
        "search": "search_queries * 0.5 + search_results * 0.05",
        "cache_discount": "cache_read_tokens * 0.0045",
        "flat_jobs": {"batch_train": 100, "quick_summary": "0.5"},
    },
    "ledger": {
        "min_balance": 0,
        "signup_grant": {"amount": 50, "bucket": "gifted"},
        "buckets": {
            "gifted": {"priority": 10, "ttl_days": 30},
            "monthly": {"priority": 20, "ttl_days": 30},
            "purchased": {"priority": 30, "default": True, "allow_overdraft": True},
        },
    },
}

FULL_CONFIG = {
    "version": 1,
    "metering": {
        "models": {
            "claude-sonnet-4-6": "input_tokens * 3 + output_tokens * 10",
            "*": "input_tokens * 10 + output_tokens * 30",
        },
        "tools": {"*": "calls * 0", "code_exec": "calls * 50"},
        "search": "search_queries * 10 + search_results * 1",
        "cache_discount": "cache_read_tokens * 0.5",
        "flat_jobs": {"roadmap_gen": 5000},
    },
    "ledger": {
        "min_balance": 0,
        "signup_grant": {"amount": 50, "bucket": "gifted"},
        "buckets": {
            "gifted": {"priority": 10, "ttl_days": 7},
            "monthly": {"priority": 20, "ttl_days": 30},
            "purchased": {"priority": 30, "default": True, "allow_overdraft": True},
        },
    },
    "plans": {
        "seeker": {
            "label": "Seeker",
            "allowance": {"amount": 5000, "period": "calendar_month"},
            "rate_overrides": {
                "claude-sonnet-4-6": "min(input_tokens * 3 + output_tokens * 9, 50)",
            },
            "safety": {
                "billing_mode": "strict",
                "max_concurrent": 1,
                "overdraft_floor": 0,
                "per_operation": {
                    "roadmap_gen": {"billing_mode": "strict", "max_concurrent": 1},
                },
            },
            "entitlements": {
                "roadmap_gen": {
                    "value": True,
                    "max_calls": 1,
                    "period": "daily",
                    "on_exceed": "deny",
                },
                "chat": {"value": True},
            },
        },
        "sage": {
            "label": "Sage",
            "allowance": {"amount": 50000, "period": "calendar_month"},
            "safety": {"billing_mode": "strict", "max_concurrent": 4},
            "entitlements": {
                "chat": {"value": True},
                "agentic": {"value": True},
            },
        },
    },
    "billing": {
        "currency": "USD",
        "subscriptions": {
            "seeker-monthly": {
                "plan": "seeker",
                "interval": "month",
                "interval_count": 1,
                "grant": {"mode": "allowance"},
                "providers": {"stripe": {"price_id": "price_monthly_seeker"}},
            },
            "sage-annual": {
                "plan": "sage",
                "interval": "year",
                "grant": {
                    "mode": "cycle_grant",
                    "credits": 50000,
                    "bucket": "purchased",
                    "replace_prior": True,
                },
                "providers": {"stripe": {"price_id": "price_annual_sage"}},
            },
        },
        "topups": {
            "small-pack": {
                "credits_per_unit": 1000,
                "min_amount_minor": 500,
                "max_amount_minor": 50000,
                "tax_behavior": "exclude_tax",
                "deposit_to": "purchased",
                "providers": {"stripe": {"price_id": "price_credits_small"}},
            },
        },
    },
}

DOC_EXAMPLES = [
    pytest.param(MINIMAL_CONFIG, id="minimal"),
    pytest.param(FULL_CONFIG, id="full"),
]


@pytest.mark.parametrize("config", DOC_EXAMPLES)
def test_configuration_mdx_examples_validate(config: dict) -> None:
    """Each documented example config loads without validation errors."""
    loaded = load_config_from_dict(config)
    assert loaded.version == 1
    assert loaded.metering.models


def test_scalar_entitlement_shorthand_rejected() -> None:
    """Entitlements must be objects — plain scalars like chat: true are invalid."""
    with pytest.raises(ValueError):
        load_config_from_dict(
            {
                "version": 1,
                "metering": {"models": {"*": "input_tokens * 1"}},
                "plans": {
                    "free": {
                        "label": "Free",
                        "entitlements": {"chat": True},
                    },
                },
            }
        )
