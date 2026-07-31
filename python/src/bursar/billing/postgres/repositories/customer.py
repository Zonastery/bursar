from __future__ import annotations

from typing import Any

from bursar.credits.postgres.repositories._types import DbQuery
from bursar.credits.postgres.repositories._utils import validate_non_empty


class BillingCustomerRepository:
    def __init__(self, execute: DbQuery) -> None:
        self._execute = execute

    def upsert(self, provider: str, provider_customer_id: str, user_id: str, email: str | None) -> dict[str, Any]:
        validate_non_empty(provider, "provider")
        validate_non_empty(provider_customer_id, "provider_customer_id")
        self._execute(
            "SELECT bursar.upsert_billing_customer(%s::uuid, %s, %s, %s)",
            [user_id, provider, provider_customer_id, email],
        )
        return {"status": "ok"}

    def get(self, provider: str, provider_customer_id: str) -> str | None:
        rows = self._execute(
            "SELECT * FROM bursar.get_billing_customer_by_provider(%s, %s)",
            [provider, provider_customer_id],
        )
        row = rows[0] if rows else None
        return str(row["subject_id"]) if isinstance(row, dict) and row.get("subject_id") is not None else None

    def get_by_user_id(self, user_id: str, provider: str | None = None) -> dict[str, Any] | None:
        rows = self._execute("SELECT * FROM bursar.get_billing_customer(%s::uuid, %s)", [user_id, provider])
        row = rows[0] if rows else None
        if not isinstance(row, dict):
            return None
        return {"provider": str(row["provider"]), "provider_customer_id": str(row["provider_customer_id"])}
