from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from bursar.credits.postgres.repositories._types import DbQuery
from bursar.credits.postgres.repositories._utils import require_identifier_result
from bursar.credits.postgres.repositories.schemas import SubscriptionRow
from bursar.errors import StoreError


def provider_timestamp_sort_key(row: dict[str, Any]) -> tuple[bool, float]:
    """Rank valid provider timestamps above invalid values, with newer timestamps greater."""
    value = row.get("provider_updated_at")
    try:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str):
            parsed = datetime.fromisoformat(value)
        else:
            return False, float("-inf")
        if parsed.tzinfo is None:
            return False, float("-inf")
        return True, parsed.timestamp()
    except (OverflowError, TypeError, ValueError):
        return False, float("-inf")


class BillingSubscriptionRepository:
    def __init__(self, execute: DbQuery) -> None:
        self._execute = execute

    def upsert(self, state: dict[str, Any]) -> None:
        provider = str(state.get("provider") or "")
        provider_subscription_id = str(state.get("provider_subscription_id") or "")
        user_id = str(state.get("user_id") or state.get("subject_id") or "")
        provider_customer_id = state.get("provider_customer_id")
        offer_id = str(state.get("offer_id") or "")

        if not provider or not provider_subscription_id:
            raise ValueError("subscription.upsert: subject, provider, subscription, and offer are required")

        existing = self._execute(
            "SELECT * FROM bursar.get_billing_subscription_by_provider(%s,%s)",
            [provider, provider_subscription_id],
        )
        existing_row = existing[0] if existing and isinstance(existing[0], dict) else {}
        if not user_id and existing_row.get("subject_id") is not None:
            user_id = str(existing_row["subject_id"])
        if provider_customer_id is None and existing_row.get("provider_customer_id") is not None:
            provider_customer_id = str(existing_row["provider_customer_id"])
        if not offer_id and existing_row.get("offer_id") is not None:
            offer_id = str(existing_row["offer_id"])

        offer_key = str(state.get("offer_key") or "")
        if not offer_id and offer_key:
            offer_rows = self._execute(
                "SELECT * FROM bursar.resolve_active_catalog_offer(%s)",
                [offer_key],
            )
            offer_row = offer_rows[0] if offer_rows and isinstance(offer_rows[0], dict) else {}
            if offer_row.get("id") is not None:
                offer_id = str(offer_row["id"])

        if not user_id or not offer_id:
            raise ValueError("subscription.upsert: subject, provider, subscription, and offer are required")

        rows = self._execute(
            "SELECT bursar.upsert_billing_subscription("
            "%s::uuid,%s,%s,%s,%s::uuid,%s::bursar.billing_subscription_status,"
            "%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s) AS id",
            [
                user_id,
                provider,
                provider_subscription_id,
                provider_customer_id,
                offer_id,
                state.get("status", "incomplete"),
                state.get("current_period_start"),
                state.get("current_period_end"),
                state.get("cancel_at_period_end", False),
                json.dumps(state.get("metadata") or {}),
                state.get("trial_end"),
                state.get("cancel_at"),
                state.get("ended_at"),
                state.get("provider_updated_at") or datetime.now(UTC).isoformat(),
                state.get("grace_ends_at"),
            ],
        )
        require_identifier_result(rows, "id", "BillingSubscriptionRepository.upsert")

    def _with_offer_context(self, row: dict[str, Any]) -> dict[str, Any]:
        if row.get("offer_id") is None or row.get("catalog_revision_id") is None:
            return row
        context_rows = self._execute(
            "SELECT * FROM bursar.get_catalog_offer_context(%s::uuid,%s::uuid)",
            [row["offer_id"], row["catalog_revision_id"]],
        )
        context = context_rows[0] if context_rows and isinstance(context_rows[0], dict) else None
        if context is None or context.get("offer_key") is None:
            raise StoreError(
                "billing subscription offer context is missing",
                details={
                    "offer_id": row["offer_id"],
                    "catalog_revision_id": row["catalog_revision_id"],
                },
            )
        return {
            **row,
            **context,
            "plan": context.get("plan_key"),
            "interval": context.get("billing_unit"),
            "interval_count": context.get("billing_count"),
        }

    def _map(self, row: dict[str, Any] | None) -> SubscriptionRow | None:
        if not row or (row.get("subject_id") is None and row.get("provider") is None):
            return None
        mapped = {
            **self._with_offer_context(row),
            "user_id": row.get("subject_id"),
        }
        try:
            return SubscriptionRow.model_validate(mapped)
        except ValueError as exc:
            raise StoreError(
                "BillingSubscriptionRepository: subscription row validation failed",
                cause=exc,
            ) from exc

    def get(self, provider: str, provider_subscription_id: str) -> SubscriptionRow | None:
        rows = self._execute(
            "SELECT * FROM bursar.get_billing_subscription_by_provider(%s,%s)", [provider, provider_subscription_id]
        )
        return self._map(rows[0] if rows and isinstance(rows[0], dict) else None)

    def get_user_subscription(self, user_id: str, statuses: list[str] | None = None) -> SubscriptionRow | None:
        rows = self._execute("SELECT * FROM bursar.list_billing_subscriptions(%s::uuid)", [user_id])
        allowed = statuses or ["active", "trialing"]
        candidates = [row for row in rows if isinstance(row, dict) and row.get("status") in allowed]
        candidates.sort(key=provider_timestamp_sort_key, reverse=True)
        return self._map(candidates[0]) if candidates else None

    def get_user_subscriptions(self, user_id: str) -> list[SubscriptionRow]:
        rows = self._execute("SELECT * FROM bursar.list_billing_subscriptions(%s::uuid)", [user_id])
        return [mapped for row in rows if isinstance(row, dict) and (mapped := self._map(row)) is not None]
