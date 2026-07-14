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
    "current_period_end, cancel_at_period_end, interval, interval_count, metadata, "
    "catalog_version, plan_version_id"
)


class DecimalEncoder(json.JSONEncoder):
    """Custom JSON encoder that converts ``Decimal`` to a string for JSONB storage."""

    def default(self, o: object) -> object:
        if isinstance(o, Decimal):
            return str(o)
        return super().default(o)


class BillingSubscriptionRepository:
    """Repository for billing subscription operations."""

    def __init__(self, execute: DbQuery) -> None:
        self._execute = execute

    def upsert(self, state: dict[str, Any]) -> None:
        """Insert or update a billing subscription via upsert_billing_subscription RPC."""
        required = ["user_id", "provider", "provider_subscription_id"]
        for key in required:
            if key not in state or not state[key]:
                raise ValueError(f"subscription.upsert: {key} is required")

        payload = {
            "user_id": state["user_id"],
            "provider": state["provider"],
            "provider_subscription_id": state["provider_subscription_id"],
            "provider_customer_id": state.get("provider_customer_id"),
            "offer_key": state.get("offer_key"),
            "plan": state.get("plan"),
            "status": state.get("status", SUBSCRIPTION_STATUS_INCOMPLETE),
            "current_period_start": state.get("current_period_start"),
            "current_period_end": state.get("current_period_end"),
            "cancel_at_period_end": state.get("cancel_at_period_end", False),
            "interval": state.get("interval"),
            "interval_count": state.get("interval_count"),
            "metadata": state.get("metadata"),
            "catalog_version": state.get("catalog_version"),
            "plan_version_id": state.get("plan_version_id"),
        }
        rows = self._execute(
            "SELECT public.upsert_billing_subscription(%s::jsonb) AS result",
            [json.dumps(payload, cls=DecimalEncoder)],
        )
        if rows and isinstance(rows[0], dict) and rows[0].get("result", {}).get("error"):
            err = rows[0]["result"]
            raise ValueError(err.get("message") or err.get("error"))

    def get(self, provider: str, provider_subscription_id: str) -> SubscriptionRow | None:
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
        if status is None:
            status = (SUBSCRIPTION_STATUS_ACTIVE, SUBSCRIPTION_STATUS_TRIALING)
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
