from copy import deepcopy

import pytest

from bursar.config import ConfigError, canonical_bursar_config_dict

STARTER = {
    "version": 1,
    "catalog": {"default_plan": "free"},
    "credits": {"on_signup": "20"},
    "pricing": {"operations": {"export_report": {"charge": {"type": "flat", "amount": "2"}}}},
    "plans": {"free": {"display_name": "Free", "allowed_operations": ["export_report"]}},
}


def test_starter_compiles_to_durable_catalog_without_mutating_input() -> None:
    source = deepcopy(STARTER)
    result = canonical_bursar_config_dict(source)
    assert source == STARTER
    assert result["version"] == 1
    assert result["pricing"]["operations"]["export_report"]["measures"] == {}
    assert result["credits"]["grant_programs"]["signup"]["idempotency_scope"] == "subject"
    assert result["plans"]["free"]["allowed_operations"] == ["export_report"]
    assert result["plans"]["free"]["tier"] == 0


@pytest.mark.parametrize("access", [[], None, "unexpected"])
def test_invalid_access_rejected(access: object) -> None:
    source = deepcopy(STARTER)
    source["plans"]["free"]["allowed_operations"] = access
    with pytest.raises(ConfigError):
        canonical_bursar_config_dict(source)


def test_default_access_is_none_and_conflict_is_rejected() -> None:
    source = deepcopy(STARTER)
    del source["plans"]["free"]["allowed_operations"]
    assert canonical_bursar_config_dict(source)["plans"]["free"]["allowed_operations"] == "none"
    source["plans"]["free"]["operation_access"] = "all"
    with pytest.raises(ConfigError):
        canonical_bursar_config_dict(source)


def test_v2_metered_measure_defaults_required_but_can_be_optional() -> None:
    from decimal import Decimal

    from bursar.engine import PricingEngine
    from bursar.metrics import UsageMetrics

    source = deepcopy(STARTER)
    source["pricing"]["operations"]["export_report"] = {
        "measures": {"items": {"unit": "item"}},
        "charge": {"type": "per_unit", "measure": "items", "rate": "2"},
    }
    engine = PricingEngine.from_dict(source)
    with pytest.raises(ConfigError, match="requires usage measure"):
        engine.calculate(UsageMetrics(operation="export_report"))
    assert engine.calculate(UsageMetrics(operation="export_report", measures={"items": Decimal(0)})).total == 0
    source["pricing"]["operations"]["export_report"]["measures"]["items"]["required"] = False
    engine = PricingEngine.from_dict(source)
    assert engine.calculate(UsageMetrics(operation="export_report")).total == 0


def test_provider_bindings_are_lossless_and_resolved_at_use() -> None:
    from bursar.config import resolve_provider_id
    from bursar.config.authoring import normalize_config
    from bursar.config.types import ProviderId

    mapping: ProviderId = {"test": "price_test", "live": "price_live"}
    assert resolve_provider_id(mapping, "test") == "price_test"
    assert resolve_provider_id(mapping, "live") == "price_live"
    with pytest.raises(ConfigError, match="sandbox"):
        resolve_provider_id(mapping, "sandbox")
    assert normalize_config({"version": 1}, "test")["version"] == 1


def test_model_and_mapping_follow_same_public_contract() -> None:
    from bursar.config import BursarConfig

    parsed = BursarConfig.model_validate(deepcopy(STARTER))
    expected = canonical_bursar_config_dict(STARTER)
    assert canonical_bursar_config_dict(parsed) == expected
    assert canonical_bursar_config_dict(expected) == expected
