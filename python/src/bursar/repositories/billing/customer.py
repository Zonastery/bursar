from __future__ import annotations

from typing import Any

from bursar.repositories._types import DbQuery
from bursar.repositories._utils import validate_non_empty


class BillingCustomerRepository:
    """Repository for billing customer operations."""

    def __init__(self, execute: DbQuery) -> None:
        self._execute = execute

    def upsert(
        self,
        provider: str,
        provider_customer_id: str,
        user_id: str,
        email: str | None,
    ) -> dict[str, Any]:
        """Insert or update a billing customer record via RPC."""
        validate_non_empty(provider, "provider")
        validate_non_empty(provider_customer_id, "provider_customer_id")
        validate_non_empty(user_id, "user_id")
        rows = self._execute(
            "SELECT public.upsert_billing_customer(%s, %s, %s::uuid, %s) AS result",
            [provider, provider_customer_id, user_id, email],
        )
        if not rows:
            return {"status": "ok"}
        row = rows[0]
        result = row.get("result") if isinstance(row, dict) else row[0]
        if isinstance(result, dict):
            return result
        return {"status": "ok"}

    def get(self, provider: str, provider_customer_id: str) -> str | None:
        validate_non_empty(provider, "provider")
        validate_non_empty(provider_customer_id, "provider_customer_id")
        rows = self._execute(
            "SELECT user_id FROM public.billing_customers WHERE provider = %s AND provider_customer_id = %s",
            [provider, provider_customer_id],
        )
        if not rows:
            return None
        row = rows[0]
        if isinstance(row, dict):
            user_id = row.get("user_id")
            return str(user_id) if user_id is not None else None
        return None

    def get_by_user_id(
        self,
        user_id: str,
        provider: str | None = None,
    ) -> dict[str, Any] | None:
        validate_non_empty(user_id, "user_id")
        if provider is not None:
            validate_non_empty(provider, "provider")
            rows = self._execute(
                """SELECT provider, provider_customer_id
                   FROM public.billing_customers
                   WHERE user_id = %s AND provider = %s
                   ORDER BY updated_at DESC LIMIT 1""",
                [user_id, provider],
            )
        else:
            rows = self._execute(
                """SELECT provider, provider_customer_id
                   FROM public.billing_customers
                   WHERE user_id = %s
                   ORDER BY updated_at DESC LIMIT 1""",
                [user_id],
            )
        if not rows:
            return None
        row = rows[0]
        return row if isinstance(row, dict) else None
