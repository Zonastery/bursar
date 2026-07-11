from __future__ import annotations

from collections.abc import Callable
from typing import Any

QueryFn = Callable[[str, list[Any]], list[Any]]


class BillingCustomerRepository:
    def __init__(self, execute: QueryFn) -> None:
        self._execute = execute

    def upsert(
        self,
        provider: str,
        provider_customer_id: str,
        user_id: str,
        email: str | None,
    ) -> None:
        self._execute(
            """INSERT INTO public.billing_customers (provider, provider_customer_id, user_id, email)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (provider, provider_customer_id) DO UPDATE SET
                 user_id = EXCLUDED.user_id,
                 email = COALESCE(EXCLUDED.email, billing_customers.email),
                 updated_at = now()""",
            [provider, provider_customer_id, user_id, email],
        )

    def get(self, provider: str, provider_customer_id: str) -> str | None:
        rows = self._execute(
            "SELECT user_id FROM public.billing_customers WHERE provider = %s AND provider_customer_id = %s",
            [provider, provider_customer_id],
        )
        if not rows:
            return None
        row = rows[0]
        return str(row[0]) if isinstance(row, (list, tuple)) else str(row.get("user_id", "")) if row else None
