"""Provider bindings resolve independently in separate tenant environments."""

from copy import deepcopy
from typing import Any

import psycopg2
import pytest

from bursar.billing.postgres.store import PostgresBillingStore
from bursar.config import ConfigError, canonical_bursar_config_dict, load_config_from_dict, resolve_provider_id
from bursar.config.types import StripePriceReference
from bursar.credits.postgres.store import PostgresStore
from bursar.errors import StoreError
from tests.conftest import TEST_TENANT_ID

CONFIG: dict[str, Any] = {
    "version": 1,
    "catalog": {"default_plan": "free"},
    "plans": {"free": {"display_name": "Free"}, "pro": {"display_name": "Pro"}},
    "commerce": {
        "providers": {"stripe": {"type": "stripe"}},
        "offers": {
            "monthly": {
                "type": "subscription",
                "display_name": "Monthly",
                "plan": "pro",
                "billing_interval": {"unit": "month"},
                "price": {"amount_minor": 100, "currency": "USD"},
                "providers": {"stripe": {"price_id": {"test": "price_test", "live": "price_live"}}},
            },
            "pack": {
                "type": "topup",
                "display_name": "Pack",
                "credits_per_unit": "10",
                "bucket": "default",
                "price": {"amount_minor": 100, "currency": "USD"},
                "providers": {"stripe": {"price_id": {"test": "pack_test", "live": "pack_live"}}},
            },
        },
    },
}


def test_provider_binding_parse_and_scalar_provenance():
    parsed = load_config_from_dict(CONFIG)
    reference = parsed.commerce.offers["monthly"].providers["stripe"]
    assert isinstance(reference, StripePriceReference)
    value = reference.price_id
    assert resolve_provider_id(value, "live") == "price_live"
    assert resolve_provider_id(value, "test") == "price_test"
    with pytest.raises(ConfigError, match="sandbox"):
        resolve_provider_id(value, "sandbox")
    source = deepcopy(CONFIG)
    source["commerce"]["offers"]["monthly"]["providers"]["stripe"]["price_id"] = "scalar_test"
    compiled = canonical_bursar_config_dict(source, "test")
    compiled = canonical_bursar_config_dict(compiled, "live")
    value = compiled["commerce"]["offers"]["monthly"]["providers"]["stripe"]["price_id"]
    assert value == {"test": "scalar_test"}
    with pytest.raises(ConfigError, match="live"):
        resolve_provider_id(value, "live")
    duplicate = deepcopy(CONFIG)
    duplicate["commerce"]["offers"]["pack"]["providers"]["stripe"]["price_id"]["live"] = "price_live"
    with pytest.raises(ConfigError, match="duplicate provider"):
        load_config_from_dict(duplicate, "test")


@pytest.mark.integration
def test_tenant_catalog_projects_both_namespaces(pg_database_url):
    live_tenant = "00000000-0000-0000-0000-000000008888"
    with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
        cursor.execute("SET LOCAL ROLE bursar_operator")
        cursor.execute("SELECT bursar.create_tenant(%s,'live-fixture','Live fixture','live')", (live_tenant,))
    with PostgresStore(pg_database_url, tenant_id=TEST_TENANT_ID, provider_environment="test") as test:
        with PostgresStore(pg_database_url, tenant_id=live_tenant, provider_environment="live") as live:
            with PostgresBillingStore(pg_database_url, tenant_id=TEST_TENANT_ID, provider_environment="test") as bt:
                with PostgresBillingStore(pg_database_url, tenant_id=live_tenant, provider_environment="live") as bl:
                    compiled = canonical_bursar_config_dict(CONFIG, "test")
                    revision = live.publish_and_activate_catalog(compiled)
                    assert test.publish_and_activate_catalog(compiled) != revision
                    for billing, suffix in ((bt, "test"), (bl, "live")):
                        assert billing.resolve_billing_offer("stripe", None, f"price_{suffix}") is not None
                        assert billing.resolve_credit_topup("stripe", None, f"pack_{suffix}") is not None
                    assert bt.resolve_billing_offer("stripe", None, "price_live") is None
                    assert bl.resolve_credit_topup("stripe", None, "pack_test") is None


@pytest.mark.integration
def test_direct_sql_infers_builtin_reference_types(pg_database_url):
    import json

    with PostgresStore(pg_database_url, tenant_id=TEST_TENANT_ID, provider_environment="test") as store:
        with PostgresBillingStore(pg_database_url, tenant_id=TEST_TENANT_ID, provider_environment="test") as billing:
            document = canonical_bursar_config_dict(CONFIG)
            for offer in document["commerce"]["offers"].values():
                del offer["providers"]["stripe"]["type"]
            rows = store._callproc("publish_and_activate_catalog", [1, json.dumps(document), None, True, "{}"])
            assert rows
            assert billing.resolve_billing_offer("stripe", None, "price_test") is not None
            assert billing.resolve_credit_topup("stripe", None, "pack_test") is not None

            for invalid in ({}, {"preview": "price_preview"}, {"test": "ok", "preview": "wrong"}):
                malformed = deepcopy(document)
                malformed["commerce"]["offers"]["monthly"]["providers"]["stripe"]["price_id"] = invalid
                with pytest.raises(StoreError) as error:
                    store._callproc("publish_and_activate_catalog", [1, json.dumps(malformed), None, True, "{}"])
                assert isinstance(error.value.__cause__, psycopg2.errors.InvalidParameterValue)
                assert billing.resolve_billing_offer("stripe", None, "price_test") is not None
