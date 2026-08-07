"""Database security smoke test for the redesigned configuration catalog."""

import pytest

from bursar.credits.postgres.store import PostgresStore
from tests.conftest import TEST_TENANT_ID

pytestmark = [pytest.mark.integration]


def test_config_catalog_is_available_after_migrations(pg_database_url: str) -> None:
    store = PostgresStore(pg_database_url, tenant_id=TEST_TENANT_ID)
    # The catalog is intentionally private to Bursar's server-side store API;
    # this call proves the security-definer RPC path remains usable.
    active_pricing = store.get_active_catalog()
    assert active_pricing is None or active_pricing.config is not None
