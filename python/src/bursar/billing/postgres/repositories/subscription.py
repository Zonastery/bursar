from __future__ import annotations

import json
from typing import Any

from bursar.credits.postgres.repositories._types import DbQuery
from bursar.credits.postgres.repositories.schemas import SubscriptionRow


def _provider_lookup_type(reference: dict[str, Any]) -> str:
    return {
        "stripe_price": "price_id",
        "dodo_product": "product_id",
        "custom_object": str(reference.get("object_kind", "object")),
    }.get(str(reference.get("type")), "")


def _provider_lookup_value(reference: dict[str, Any]) -> str:
    return str(reference.get("price_id") or reference.get("product_id") or reference.get("external_id") or "")


class BillingSubscriptionRepository:
    def __init__(self, execute: DbQuery) -> None:
        self._execute = execute

    def _offer_id(self, state: dict[str, Any]) -> str:
        offer_id = str(state.get("offer_id") or state.get("offerId") or "")
        if offer_id:
            return str(offer_id)
        offer_key = state.get("offer_key")
        if not offer_key:
            existing = self._execute(
                "SELECT * FROM bursar.get_billing_subscription_by_provider(%s,%s)",
                [
                    state.get("provider"),
                    state.get("provider_subscription_id"),
                ],
            )
            if existing and isinstance(existing[0], dict):
                return str(existing[0].get("offer_id") or "")
            return ""
        active = self._execute("SELECT * FROM bursar.active_catalog_revision()", [])
        row = active[0] if active and isinstance(active[0], dict) else {}
        doc = row.get("source_document", {})
        offer = doc.get("commerce", {}).get("offers", {}).get(str(offer_key), {})
        ref = offer.get("providers", {}).get(str(state["provider"]), {})
        if not _provider_lookup_value(ref):
            return ""
        resolved = self._execute(
            "SELECT * FROM bursar.resolve_catalog_offer(%s, %s, %s)",
            [state["provider"], _provider_lookup_type(ref), _provider_lookup_value(ref)],
        )
        return (
            str(resolved[0].get("id")) if resolved and isinstance(resolved[0], dict) and resolved[0].get("id") else ""
        )

    def upsert(self, state: dict[str, Any]) -> None:
        subject_id = state.get("subject_id") or state.get("user_id")
        offer_id = self._offer_id(state)
        required = (subject_id, state.get("provider"), state.get("provider_subscription_id"), offer_id)
        if not all(required):
            raise ValueError("subscription.upsert: subject, provider, subscription, and offer are required")
        self._execute(
            "SELECT bursar.upsert_billing_subscription("
            "%s::uuid,%s,%s,%s,%s::uuid,%s::bursar.billing_subscription_status,%s,%s,%s,%s::jsonb)",
            [
                subject_id,
                state["provider"],
                state["provider_subscription_id"],
                state.get("provider_customer_id"),
                offer_id,
                state.get("status", "incomplete"),
                state.get("current_period_start"),
                state.get("current_period_end"),
                state.get("cancel_at_period_end", False),
                json.dumps(state.get("metadata") or {}),
            ],
        )

    def _map(self, row: dict[str, Any] | None) -> SubscriptionRow | None:
        if not row or (row.get("subject_id") is None and row.get("provider") is None):
            return None

        mapped = {**row, "user_id": row.get("subject_id")}
        if mapped.get("offer_key") and mapped.get("interval"):
            return SubscriptionRow.model_validate(mapped)

        active = self._execute(
            "SELECT * FROM bursar.active_catalog_revision()",
            [],
        )
        document = active[0].get("source_document", {}) if active and isinstance(active[0], dict) else {}
        offers = document.get("commerce", {}).get("offers", {})
        provider = str(row.get("provider") or "")

        for offer_key, offer in offers.items():
            if offer.get("type") != "subscription":
                continue
            reference = offer.get("providers", {}).get(provider)
            if not isinstance(reference, dict):
                continue
            lookup_type = _provider_lookup_type(reference)
            lookup_value = _provider_lookup_value(reference)
            if not lookup_type or not lookup_value:
                continue

            resolved = self._execute(
                "SELECT * FROM bursar.resolve_catalog_offer(%s,%s,%s)",
                [provider, lookup_type, lookup_value],
            )
            if resolved and isinstance(resolved[0], dict) and str(resolved[0].get("id")) == str(row.get("offer_id")):
                interval = offer.get("billing_interval", {})
                mapped.update(
                    {
                        "offer_key": offer_key,
                        "plan": offer.get("plan"),
                        "interval": interval.get("unit"),
                        "interval_count": interval.get("count"),
                    }
                )
                break

        return SubscriptionRow.model_validate(mapped)

    def get(self, provider: str, provider_subscription_id: str) -> SubscriptionRow | None:
        rows = self._execute(
            "SELECT * FROM bursar.get_billing_subscription_by_provider(%s,%s)", [provider, provider_subscription_id]
        )
        return self._map(rows[0] if rows and isinstance(rows[0], dict) else None)

    def get_user_subscription(self, user_id: str, statuses: list[str] | None = None) -> SubscriptionRow | None:
        rows = self._execute("SELECT * FROM bursar.list_billing_subscriptions(%s::uuid)", [user_id])
        allowed = statuses or ["active", "trialing"]
        for row in rows:
            if isinstance(row, dict) and row.get("status") in allowed:
                return self._map(row)
        return None

    def get_user_subscriptions(self, user_id: str) -> list[SubscriptionRow]:
        rows = self._execute("SELECT * FROM bursar.list_billing_subscriptions(%s::uuid)", [user_id])
        return [mapped for row in rows if isinstance(row, dict) and (mapped := self._map(row)) is not None]
