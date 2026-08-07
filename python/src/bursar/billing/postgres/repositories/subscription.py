from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from bursar.billing.types import BillingSubscriptionState
from bursar.credits.postgres.repositories._types import DbQuery
from bursar.credits.postgres.repositories._utils import optional_mapping_row, require_identifier_result
from bursar.credits.postgres.repositories.schemas import SubscriptionRow
from bursar.errors import StoreError


def provider_timestamp_sort_key(row: dict[str, Any]) -> float:
    """Return the provider timestamp used for deterministic newest-first ordering."""
    value = row.get("provider_updated_at")
    try:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str):
            parsed = datetime.fromisoformat(value)
        else:
            raise ValueError("provider_updated_at is required")
        if parsed.tzinfo is None:
            raise ValueError("provider_updated_at must include a timezone")
        return parsed.timestamp()
    except (OverflowError, TypeError, ValueError) as error:
        raise StoreError("billing subscription has an invalid provider_updated_at", cause=error) from error


class BillingSubscriptionRepository:
    def __init__(self, execute: DbQuery) -> None:
        self._execute = execute

    def upsert(self, state: BillingSubscriptionState) -> None:
        provider = state.provider
        provider_subscription_id = state.provider_subscription_id
        user_id = state.user_id
        provider_customer_id = state.provider_customer_id
        offer_id = state.offer_id or ""

        existing = self._execute(
            "SELECT * FROM bursar.get_billing_subscription_by_provider(%s,%s)",
            [provider, provider_subscription_id],
        )
        existing_row = optional_mapping_row(existing, "BillingSubscriptionRepository.upsert.existing") or {}
        if not user_id and existing_row.get("subject_id") is not None:
            user_id = str(existing_row["subject_id"])
        if provider_customer_id is None and existing_row.get("provider_customer_id") is not None:
            provider_customer_id = str(existing_row["provider_customer_id"])
        if not offer_id and existing_row.get("offer_id") is not None:
            offer_id = str(existing_row["offer_id"])

        offer_key = state.offer_key or ""
        if not offer_id and offer_key:
            offer_rows = self._execute(
                "SELECT * FROM bursar.resolve_active_catalog_offer(%s)",
                [offer_key],
            )
            offer_row = optional_mapping_row(offer_rows, "BillingSubscriptionRepository.upsert.offer") or {}
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
                state.status,
                state.current_period_start,
                state.current_period_end,
                state.cancel_at_period_end,
                json.dumps(state.metadata or {}),
                state.trial_end,
                state.cancel_at,
                state.ended_at,
                state.provider_updated_at,
                state.grace_ends_at,
            ],
        )
        require_identifier_result(rows, "id", "BillingSubscriptionRepository.upsert")

    def _with_offer_context(self, row: dict[str, Any]) -> dict[str, Any]:
        if row.get("offer_id") is None or row.get("catalog_revision_id") is None:
            raise StoreError("billing subscription is missing its catalog reference")
        context_rows = self._execute(
            "SELECT * FROM bursar.get_catalog_offer_context(%s::uuid,%s::uuid)",
            [row["offer_id"], row["catalog_revision_id"]],
        )
        context = optional_mapping_row(context_rows, "BillingSubscriptionRepository.offer_context")
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
        return self._map(optional_mapping_row(rows, "BillingSubscriptionRepository.get"))

    def get_user_subscription(self, user_id: str, statuses: list[str] | None = None) -> SubscriptionRow | None:
        rows = self._execute("SELECT * FROM bursar.list_billing_subscriptions(%s::uuid)", [user_id])
        allowed = statuses or ["active", "trialing"]
        candidates = [row for row in rows if isinstance(row, dict) and row.get("status") in allowed]
        candidates.sort(key=provider_timestamp_sort_key, reverse=True)
        return self._map(candidates[0]) if candidates else None

    def get_user_subscriptions(self, user_id: str) -> list[SubscriptionRow]:
        rows = self._execute("SELECT * FROM bursar.list_billing_subscriptions(%s::uuid)", [user_id])
        return [mapped for row in rows if isinstance(row, dict) and (mapped := self._map(row)) is not None]
