from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from bursar.repositories.schemas import SubscriptionRow

QueryFn = Callable[[str, list[Any]], list[Any]]

SUBSCRIPTION_COLS = (
    "user_id, provider, provider_subscription_id, provider_customer_id, "
    "offer_key, plan, status, current_period_start, "
    "current_period_end, cancel_at_period_end, interval, interval_count, metadata"
)


class BillingSubscriptionRepository:
    def __init__(self, execute: QueryFn) -> None:
        self._execute = execute

    def upsert(self, state: dict[str, Any]) -> None:
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
                state.get("status", "incomplete"),
                state.get("current_period_start"),
                state.get("current_period_end"),
                state.get("cancel_at_period_end", False),
                state.get("interval"),
                state.get("interval_count"),
                json.dumps(state["metadata"]) if state.get("metadata") else None,
            ],
        )

    def get(self, provider: str, provider_subscription_id: str) -> SubscriptionRow | None:
        rows = self._execute(
            f"SELECT {SUBSCRIPTION_COLS} FROM public.billing_subscriptions"
            " WHERE provider = %s AND provider_subscription_id = %s",
            [provider, provider_subscription_id],
        )
        if not rows:
            return None
        return SubscriptionRow.model_validate(rows[0]) if isinstance(rows[0], dict) else None

    def get_user_subscription(self, user_id: str) -> SubscriptionRow | None:
        rows = self._execute(
            f"SELECT {SUBSCRIPTION_COLS} FROM public.billing_subscriptions"
            " WHERE user_id = %s"
            " ORDER BY current_period_start DESC NULLS LAST, created_at DESC"
            " LIMIT 1",
            [user_id],
        )
        if not rows:
            return None
        return SubscriptionRow.model_validate(rows[0]) if isinstance(rows[0], dict) else None

    def get_user_subscriptions(self, user_id: str) -> list[SubscriptionRow]:
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
    ) -> int:
        rows = self._execute(
            "UPDATE public.billing_subscriptions"
            " SET status = 'canceled', cancel_at_period_end = true, updated_at = now()"
            " WHERE user_id = %s AND provider != %s AND status IN ('active', 'trialing')"
            " RETURNING 1",
            [user_id, keep_provider],
        )
        return len(rows)
