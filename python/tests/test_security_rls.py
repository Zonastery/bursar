"""Database security coverage for the configuration catalog RPC boundary."""

import psycopg2
import pytest

from bursar.credits.postgres.store import PostgresStore
from tests.conftest import TEST_TENANT_ID
from tests.test_store_integration import CONFIG

pytestmark = [pytest.mark.integration, pytest.mark.security]


def test_catalog_rpc_exposes_tenant_state_without_table_access(pg_database_url: str) -> None:
    with PostgresStore(
        pg_database_url,
        tenant_id=TEST_TENANT_ID,
        provider_environment="test",
    ) as store:
        revision_id = store.publish_and_activate_catalog(CONFIG, "security-rpc-smoke")
        active_catalog = store.get_active_catalog()

        assert active_catalog is not None
        assert active_catalog.id == revision_id
        assert active_catalog.version == 1
        assert active_catalog.config["catalog"]["default_plan"] == "pro"
        assert active_catalog.config["plans"]["pro"]["display_name"] == "Pro"

    # The public store works through the restricted role and its SECURITY
    # DEFINER RPCs. Verify that boundary exposes the exact tenant state while
    # the same role remains unable to read the private catalog table directly.
    with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
        cursor.execute("SET LOCAL ROLE bursar_client")
        cursor.execute(
            "SELECT set_config('bursar.tenant_id', %s, true)",
            (TEST_TENANT_ID,),
        )
        cursor.execute("SELECT set_config('bursar.provider_environment', 'test', true)")
        cursor.execute(
            """
            SELECT
                (revision).tenant_id::text,
                (revision).id::text,
                (revision).revision_no,
                (revision).status::text,
                (revision).label
            FROM (
                SELECT bursar.active_catalog_revision() AS revision
            ) AS active
            """
        )
        assert cursor.fetchone() == (
            TEST_TENANT_ID,
            revision_id,
            1,
            "active",
            "security-rpc-smoke",
        )

        with pytest.raises(psycopg2.errors.InsufficientPrivilege) as exc_info:
            cursor.execute("SELECT count(*) FROM bursar.catalog_revisions")
        assert exc_info.value.pgcode == "42501"
        connection.rollback()
