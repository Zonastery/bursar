from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID

from bursar.billing.postgres.repositories.schemas import (
    CatalogOfferContextRow,
    PersistedSubscriptionRow,
    SubscriptionRow,
)
from bursar.billing.types import BillingSubscriptionState
from bursar.credits.postgres.repositories._types import DbQuery
from bursar.credits.postgres.repositories._utils import (
    optional_mapping_row,
    require_boolean_result,
    require_identifier_result,
    validate_row,
)
from bursar.errors import StoreError


class BillingSubscriptionRepository:
    def __init__(self, execute: DbQuery) -> None:
        self._execute = execute

    def upsert(self, state: BillingSubscriptionState) -> None:
        provider = state.provider
        provider_subscription_id = state.provider_subscription_id
        user_id = state.user_id
        provider_customer_id = state.provider_customer_id
        offer_id = state.offer_id

        existing = self._execute(
            "SELECT * FROM bursar.get_billing_subscription_by_provider(%s,%s)",
            [provider, provider_subscription_id],
        )
        existing_row = optional_mapping_row(existing, "BillingSubscriptionRepository.upsert.existing")
        existing_subscription = (
            self._persisted(existing_row, "BillingSubscriptionRepository.upsert.existing")
            if existing_row is not None
            else None
        )
        if provider_customer_id is None and existing_subscription is not None:
            provider_customer_id = existing_subscription.provider_customer_id
        if offer_id is None and existing_subscription is not None:
            offer_id = str(existing_subscription.offer_id)

        offer_key = state.offer_key
        if offer_id is None and offer_key is not None:
            offer_rows = self._execute(
                "SELECT * FROM bursar.resolve_active_catalog_offer(%s)",
                [offer_key],
            )
            offer_row = optional_mapping_row(offer_rows, "BillingSubscriptionRepository.upsert.offer")
            if offer_row is not None:
                try:
                    offer_id = str(UUID(str(offer_row.get("id"))))
                except (AttributeError, TypeError, ValueError) as error:
                    raise StoreError(
                        "BillingSubscriptionRepository.upsert.offer: malformed offer identifier",
                        cause=error,
                    ) from error

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

    @staticmethod
    def _persisted(row: dict[str, Any], context: str) -> PersistedSubscriptionRow:
        return validate_row(
            PersistedSubscriptionRow,
            {
                "id": row.get("id"),
                "subject_id": row.get("subject_id"),
                "provider": row.get("provider"),
                "provider_subscription_id": row.get("provider_subscription_id"),
                "provider_customer_id": row.get("provider_customer_id"),
                "offer_id": row.get("offer_id"),
                "catalog_revision_id": row.get("catalog_revision_id"),
                "status": row.get("status"),
                "current_period_start": row.get("current_period_start"),
                "current_period_end": row.get("current_period_end"),
                "trial_end": row.get("trial_end"),
                "cancel_at": row.get("cancel_at"),
                "ended_at": row.get("ended_at"),
                "cancel_at_period_end": row.get("cancel_at_period_end"),
                "grace_ends_at": row.get("grace_ends_at"),
                "grace_expired_at": row.get("grace_expired_at"),
                "provider_updated_at": row.get("provider_updated_at"),
                "metadata": row.get("metadata"),
            },
            context,
        )

    def _offer_context(self, row: PersistedSubscriptionRow) -> CatalogOfferContextRow:
        context_rows = self._execute(
            "SELECT * FROM bursar.get_catalog_offer_context(%s::uuid,%s::uuid)",
            [row.offer_id, row.catalog_revision_id],
        )
        context = optional_mapping_row(context_rows, "BillingSubscriptionRepository.offer_context")
        if context is None:
            raise StoreError(
                "billing subscription offer context is missing",
                details={
                    "offer_id": str(row.offer_id),
                    "catalog_revision_id": str(row.catalog_revision_id),
                },
            )
        return validate_row(
            CatalogOfferContextRow,
            {
                "offer_key": context.get("offer_key"),
                "plan_id": context.get("plan_id"),
                "plan_key": context.get("plan_key"),
                "billing_unit": context.get("billing_unit"),
                "billing_count": context.get("billing_count"),
            },
            "BillingSubscriptionRepository.offer_context",
        )

    def _map(self, row: PersistedSubscriptionRow) -> SubscriptionRow:
        context = self._offer_context(row)
        return validate_row(
            SubscriptionRow,
            {
                "id": row.id,
                "user_id": row.subject_id,
                "provider": row.provider,
                "provider_subscription_id": row.provider_subscription_id,
                "provider_customer_id": row.provider_customer_id,
                "offer_id": row.offer_id,
                "offer_key": context.offer_key,
                "plan_id": context.plan_id,
                "plan": context.plan_key,
                "status": row.status,
                "current_period_start": row.current_period_start,
                "current_period_end": row.current_period_end,
                "trial_end": row.trial_end,
                "cancel_at": row.cancel_at,
                "ended_at": row.ended_at,
                "cancel_at_period_end": row.cancel_at_period_end,
                "interval": context.billing_unit,
                "interval_count": context.billing_count,
                "grace_ends_at": row.grace_ends_at,
                "grace_expired_at": row.grace_expired_at,
                "provider_updated_at": row.provider_updated_at,
                "metadata": row.metadata,
            },
            "BillingSubscriptionRepository",
        )

    def get(self, provider: str, provider_subscription_id: str) -> SubscriptionRow | None:
        rows = self._execute(
            "SELECT * FROM bursar.get_billing_subscription_by_provider(%s,%s)", [provider, provider_subscription_id]
        )
        row = optional_mapping_row(rows, "BillingSubscriptionRepository.get")
        return None if row is None else self._map(self._persisted(row, "BillingSubscriptionRepository.get"))

    def get_user_subscription(self, user_id: str, statuses: list[str] | None = None) -> SubscriptionRow | None:
        allowed = set(statuses or ["active", "trialing"])
        rows = self._execute("SELECT * FROM bursar.list_billing_subscriptions(%s::uuid)", [user_id])
        if any(not isinstance(row, dict) for row in rows):
            raise StoreError("BillingSubscriptionRepository.get_user_subscription: expected object rows")
        candidates = [
            persisted
            for row in rows
            if isinstance(row, dict)
            and (persisted := self._persisted(row, "BillingSubscriptionRepository.get_user_subscription")).status
            in allowed
        ]
        selected = max(candidates, key=lambda row: row.provider_updated_at) if candidates else None
        return self._map(selected) if selected is not None else None

    def get_user_subscriptions(self, user_id: str) -> list[SubscriptionRow]:
        rows = self._execute("SELECT * FROM bursar.list_billing_subscriptions(%s::uuid)", [user_id])
        if any(not isinstance(row, dict) for row in rows):
            raise StoreError("BillingSubscriptionRepository.get_user_subscriptions: expected object rows")
        return [
            self._map(self._persisted(row, "BillingSubscriptionRepository.get_user_subscriptions"))
            for row in rows
            if isinstance(row, dict)
        ]

    def list_expired_grace_subscriptions(self, now: datetime, limit: int = 100) -> list[SubscriptionRow]:
        rows = self._execute(
            "SELECT * FROM bursar.list_expired_grace_subscriptions(%s::timestamptz,%s)",
            [now, limit],
        )
        if any(not isinstance(row, dict) for row in rows):
            raise StoreError("BillingSubscriptionRepository.list_expired_grace_subscriptions: expected object rows")
        return [
            self._map(self._persisted(row, "BillingSubscriptionRepository.list_expired_grace_subscriptions"))
            for row in rows
            if isinstance(row, dict)
        ]

    def mark_grace_expired(
        self,
        subscription_id: str,
        expected_grace_ends_at: str,
        expired_at: str,
    ) -> bool:
        rows = self._execute(
            "SELECT bursar.mark_subscription_grace_expired(%s::uuid,%s::timestamptz,%s::timestamptz) AS marked",
            [subscription_id, expected_grace_ends_at, expired_at],
        )
        return require_boolean_result(rows, "marked", "BillingSubscriptionRepository.mark_grace_expired")

    def select_entitlement_source(
        self,
        user_id: str,
        provider: str,
        provider_subscription_id: str | None = None,
    ) -> bool:
        eligible_statuses = {"trialing", "active", "past_due", "paused"}
        rows = self._execute("SELECT * FROM bursar.list_billing_subscriptions(%s::uuid)", [user_id])
        if any(not isinstance(row, dict) for row in rows):
            raise StoreError("BillingSubscriptionRepository.select_entitlement_source: expected object rows")
        candidates = [
            persisted
            for row in rows
            if isinstance(row, dict)
            and (persisted := self._persisted(row, "BillingSubscriptionRepository.select_entitlement_source")).provider
            == provider
            and persisted.status in eligible_statuses
            and (provider_subscription_id is None or persisted.provider_subscription_id == provider_subscription_id)
        ]
        if not candidates:
            return False
        replacement = max(candidates, key=lambda row: row.provider_updated_at)
        rows = self._execute(
            "SELECT bursar.select_entitlement_source(%s::uuid,%s::uuid) AS selected",
            [user_id, replacement.id],
        )
        if not require_boolean_result(rows, "selected", "BillingSubscriptionRepository.select_entitlement_source"):
            raise StoreError("subscription entitlement source selection was rejected")
        return True
