from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from bursar.repositories._types import DbQuery
from bursar.repositories._utils import validate_non_empty
from bursar.repositories.schemas import SubscriptionRow

SUBSCRIPTION_STATUS_ACTIVE = "active"
SUBSCRIPTION_STATUS_TRIALING = "trialing"
SUBSCRIPTION_STATUS_CANCELED = "canceled"
SUBSCRIPTION_STATUS_INCOMPLETE = "incomplete"

SUBSCRIPTION_COLS = (
    "user_id, provider, provider_subscription_id, provider_customer_id, "
    "offer_key, plan, status, current_period_start, "
    "current_period_end, cancel_at_period_end, interval, interval_count, metadata"
)


class DecimalEncoder(json.JSONEncoder):
    """Custom JSON encoder that converts ``Decimal`` to a string for JSONB storage."""

    def default(self, o: object) -> object:
        if isinstance(o, Decimal):
            return str(o)
        return super().default(o)


class BillingSubscriptionRepository:
    """Repository for billing subscription operations.

    All methods call Postgres via raw SQL queries through the query function.
    Returns None when the query returns no rows.
    """

    def __init__(self, execute: DbQuery) -> None:
        self._execute = execute

    def upsert(self, state: dict[str, Any]) -> None:
        """Insert or update a billing subscription record.

        Args:
            state: Dict with keys including user_id, provider,
                provider_subscription_id, status, and optional fields.

        Raises:
            ValueError: If user_id, provider, or provider_subscription_id
                is missing or empty.
        """
        required = ["user_id", "provider", "provider_subscription_id"]
        for key in required:
            if key not in state or not state[key]:
                raise ValueError(f"subscription.upsert: {key} is required")
        self._execute(
            """INSERT INTO public.billing_subscriptions (
                 user_id, provider, provider_subscription_id, provider_customer_id,
                 offer_key, plan, status, current_period_start,
                 current_period_end, cancel_at_period_end, interval, interval_count, metadata
               )
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (provider, provider_subscription_id) DO UPDATE SET
                 user_id = EXCLUDED.user_id,
                 provider_customer_id = COALESCE(EXCLUDED.provider_customer_id,
                   billing_subscriptions.provider_customer_id),
                 offer_key = COALESCE(EXCLUDED.offer_key, billing_subscriptions.offer_key),
                 plan = COALESCE(EXCLUDED.plan, billing_subscriptions.plan),
                 status = EXCLUDED.status,
                 current_period_start = COALESCE(EXCLUDED.current_period_start,
                   billing_subscriptions.current_period_start),
                 current_period_end = COALESCE(EXCLUDED.current_period_end,
                   billing_subscriptions.current_period_end),
                 cancel_at_period_end = EXCLUDED.cancel_at_period_end,
                 interval = COALESCE(EXCLUDED.interval, billing_subscriptions.interval),
                 interval_count = COALESCE(EXCLUDED.interval_count,
                   billing_subscriptions.interval_count),
                 metadata = CASE
                   WHEN EXCLUDED.metadata IS NOT NULL
                   THEN EXCLUDED.metadata
                   ELSE billing_subscriptions.metadata
                 END,
                 updated_at = now()""",
            [
                state["user_id"],
                state["provider"],
                state["provider_subscription_id"],
                state.get("provider_customer_id"),
                state.get("offer_key"),
                state.get("plan"),
                state.get("status", SUBSCRIPTION_STATUS_INCOMPLETE),
                state.get("current_period_start"),
                state.get("current_period_end"),
                state.get("cancel_at_period_end", False),
                state.get("interval"),
                state.get("interval_count"),
                json.dumps(md, cls=DecimalEncoder) if (md := state.get("metadata")) is not None else None,
            ],
        )

    def get(self, provider: str, provider_subscription_id: str) -> SubscriptionRow | None:
        """Get a subscription by provider and provider subscription ID.

        Args:
            provider: The billing provider identifier.
            provider_subscription_id: The provider subscription ID.

        Returns:
            SubscriptionRow if found, None otherwise.
        """
        validate_non_empty(provider, "provider")
        validate_non_empty(provider_subscription_id, "provider_subscription_id")
        rows = self._execute(
            f"SELECT {SUBSCRIPTION_COLS} FROM public.billing_subscriptions"
            " WHERE provider = %s AND provider_subscription_id = %s",
            [provider, provider_subscription_id],
        )
        if not rows:
            return None
        return SubscriptionRow.model_validate(rows[0]) if isinstance(rows[0], dict) else None

    def get_user_subscription(
        self,
        user_id: str,
        status: tuple[str, ...] | None = None,
    ) -> SubscriptionRow | None:
        """Get the most recent subscription for a user matching the given statuses.

        Args:
            user_id: The user ID.
            status: Tuple of allowed status values. Defaults to active and trialing.

        Returns:
            SubscriptionRow if found, None otherwise.
        """
        if status is None:
            status = (SUBSCRIPTION_STATUS_ACTIVE, SUBSCRIPTION_STATUS_TRIALING)
        """Get the most recent subscription for a user matching the given statuses.

        Args:
            user_id: The user ID.
            status: Tuple of allowed status values. Defaults to active and trialing.

        Returns:
            SubscriptionRow if found, None otherwise.
        """
        validate_non_empty(user_id, "user_id")
        rows = self._execute(
            f"SELECT {SUBSCRIPTION_COLS} FROM public.billing_subscriptions"
            " WHERE user_id = %s AND status = ANY(%s)"
            " ORDER BY current_period_start DESC NULLS LAST, created_at DESC"
            " LIMIT 1",
            [user_id, list(status)],
        )
        if not rows:
            return None
        return SubscriptionRow.model_validate(rows[0]) if isinstance(rows[0], dict) else None

    def get_user_subscriptions(self, user_id: str) -> list[SubscriptionRow]:
        """Get all subscriptions for a user, ordered by period start descending.

        Args:
            user_id: The user ID.

        Returns:
            List of SubscriptionRow objects (may be empty).
        """
        validate_non_empty(user_id, "user_id")
        rows = self._execute(
            f"SELECT {SUBSCRIPTION_COLS} FROM public.billing_subscriptions"
            " WHERE user_id = %s"
            " ORDER BY current_period_start DESC NULLS LAST",
            [user_id],
        )
        return [SubscriptionRow.model_validate(r) for r in rows if isinstance(r, dict)]

    def deactivate_other_provider_subscriptions(
        self,
        user_id: str,
        keep_provider: str,
    ) -> list[str]:
        """Cancel all active/trialing subscriptions for a user except the given provider.

        Args:
            user_id: The user ID.
            keep_provider: The provider whose subscriptions should be preserved.

        Returns:
            List of deactivated provider subscription IDs.
        """
        validate_non_empty(user_id, "user_id")
        validate_non_empty(keep_provider, "keep_provider")
        rows = self._execute(
            "UPDATE public.billing_subscriptions"
            " SET status = 'canceled', cancel_at_period_end = true, updated_at = now()"
            " WHERE user_id = %s AND provider != %s AND status = ANY(%s)"
            " RETURNING provider_subscription_id",
            [user_id, keep_provider, [SUBSCRIPTION_STATUS_ACTIVE, SUBSCRIPTION_STATUS_TRIALING]],
        )
        return [str(r["provider_subscription_id"]) for r in rows if isinstance(r, dict)]
