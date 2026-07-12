from __future__ import annotations

from bursar.repositories._types import DbQuery


class BillingConfigRepository:
    """Repository for billing configuration sync operations.

    All methods call Postgres via raw SQL queries through the query function.
    """

    def __init__(self, execute: DbQuery) -> None:
        self._execute = execute

    def sync_from_config(self, config_json: str) -> None:
        """Sync billing configuration from a JSON string into the database.

        Args:
            config_json: JSON string containing the full billing configuration.
        """
        self._execute(
            "SELECT public.sync_billing_from_config(%s::jsonb)",
            [config_json],
        )
