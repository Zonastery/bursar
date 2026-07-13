from __future__ import annotations

from typing import Any

from bursar.repositories._types import DbQuery
from bursar.repositories._utils import validate_non_empty


class BillingCustomerRepository:
    """Repository for billing customer operations.

    All methods call Postgres via raw SQL queries through the query function.
    Returns None when the query returns no rows.
    """

    def __init__(self, execute: DbQuery) -> None:
        self._execute = execute

    def upsert(
        self,
        provider: str,
        provider_customer_id: str,
        user_id: str,
        email: str | None,
    ) -> None:
        """Insert or update a billing customer record.

        Args:
            provider: The billing provider identifier.
            provider_customer_id: The provider customer ID.
            user_id: The internal user ID.
            email: The customer email address, or None.
        """
        validate_non_empty(provider, "provider")
        validate_non_empty(provider_customer_id, "provider_customer_id")
        validate_non_empty(user_id, "user_id")
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
        """Get the user ID associated with a provider customer.

        Args:
            provider: The billing provider identifier.
            provider_customer_id: The provider customer ID.

        Returns:
            The user ID string if found, None otherwise.
        """
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
        """Reverse lookup: find a customer record by user_id.

        Args:
            user_id: The internal user ID.
            provider: Optional provider filter.

        Returns:
            Dict with 'provider' and 'provider_customer_id' if found, None otherwise.
        """
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
