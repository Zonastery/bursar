from __future__ import annotations

import json
from typing import Any

from bursar.billing.models import (
    BillingConfig,
    BillingEventClaim,
    BillingSubscriptionState,
)
from bursar.billing.store import BillingStore


class SupabaseBillingStore(BillingStore):
    def __init__(self, supabase: Any) -> None:
        self._supabase = supabase

    def sync_billing_from_config(self, config: BillingConfig) -> None:
        raw = json.loads(json.dumps(config.model_dump(), default=str))
        result = self._supabase.rpc("sync_billing_from_config", {"p_config": raw})
        if "error" in result and result["error"]:
            raise RuntimeError(result["error"])

    def resolve_billing_offer(
        self,
        provider: str,
        product_id: str | None = None,
        price_id: str | None = None,
    ) -> dict | None:
        result = self._supabase.rpc(
            "resolve_billing_offer_by_price",
            {
                "p_provider": provider,
                "p_price_id": price_id,
                "p_product_id": product_id,
            },
        )
        if "error" in result and result["error"]:
            raise RuntimeError(result["error"])
        data = result.get("data")
        return data if data else None

    def claim_billing_event(
        self,
        provider: str,
        event_id: str,
        event_type: str,
    ) -> BillingEventClaim:
        result = self._supabase.rpc(
            "claim_billing_event",
            {
                "p_provider": provider,
                "p_event_id": event_id,
                "p_event_type": event_type,
                "p_payload": {"eventType": event_type},
            },
        )
        if "error" in result and result["error"]:
            return BillingEventClaim(status="retry")
        data = result.get("data")
        if not data:
            return BillingEventClaim(status="retry")
        s = data.get("status", "")
        if s == "claimed":
            return BillingEventClaim(status="claimed")
        if s == "duplicate":
            return BillingEventClaim(status="duplicate")
        return BillingEventClaim(status="retry")

    def complete_billing_event(self, provider: str, event_id: str) -> None:
        result = self._supabase.rpc(
            "complete_billing_event",
            {
                "p_provider": provider,
                "p_event_id": event_id,
            },
        )
        if "error" in result and result["error"]:
            raise RuntimeError(result["error"])

    def fail_billing_event(self, provider: str, event_id: str) -> None:
        result = self._supabase.rpc(
            "fail_billing_event",
            {
                "p_provider": provider,
                "p_event_id": event_id,
            },
        )
        if "error" in result and result["error"]:
            raise RuntimeError(result["error"])

    def upsert_billing_customer(
        self,
        provider: str,
        provider_customer_id: str,
        user_id: str,
        email: str | None = None,
    ) -> None:
        result = self._supabase.rpc(
            "upsert_billing_customer",
            {
                "p_provider": provider,
                "p_provider_customer_id": provider_customer_id,
                "p_user_id": user_id,
                "p_email": email,
            },
        )
        if "error" in result and result["error"]:
            raise RuntimeError(result["error"])

    def get_billing_customer(
        self,
        provider: str,
        provider_customer_id: str,
    ) -> str | None:
        result = self._supabase.rpc(
            "get_billing_customer",
            {
                "p_provider": provider,
                "p_provider_customer_id": provider_customer_id,
            },
        )
        if "error" in result and result["error"]:
            raise RuntimeError(result["error"])
        data = result.get("data")
        if not data:
            return None
        return str(data.get("user_id", ""))

    def upsert_billing_subscription(self, state: BillingSubscriptionState) -> None:
        result = self._supabase.rpc(
            "upsert_billing_subscription",
            {
                "p_state": state.model_dump(mode="json"),
            },
        )
        if "error" in result and result["error"]:
            raise RuntimeError(result["error"])

    def get_billing_subscription(
        self,
        provider: str,
        provider_subscription_id: str,
    ) -> BillingSubscriptionState | None:
        result = self._supabase.rpc(
            "get_billing_subscription",
            {
                "p_provider": provider,
                "p_provider_subscription_id": provider_subscription_id,
            },
        )
        if "error" in result and result["error"]:
            raise RuntimeError(result["error"])
        data = result.get("data")
        if not data:
            return None
        return self._row_to_subscription_state(data)

    def get_user_subscription(
        self,
        user_id: str,
    ) -> BillingSubscriptionState | None:
        result = self._supabase.rpc(
            "get_user_billing_subscription",
            {
                "p_user_id": user_id,
            },
        )
        if "error" in result and result["error"]:
            raise RuntimeError(result["error"])
        data = result.get("data")
        if not data:
            return None
        return self._row_to_subscription_state(data)

    def _row_to_subscription_state(self, r: dict) -> BillingSubscriptionState:
        return BillingSubscriptionState(
            user_id=str(r.get("user_id", "")),
            provider=str(r.get("provider", "")),
            provider_subscription_id=str(r.get("provider_subscription_id", "")),
            provider_customer_id=str(r.get("provider_customer_id")) if r.get("provider_customer_id") else None,
            offer_key=str(r.get("offer_key")) if r.get("offer_key") else None,
            plan_key=str(r.get("plan_key")) if r.get("plan_key") else None,
            status=str(r.get("status", "incomplete")),
            current_period_start=str(r.get("current_period_start")) if r.get("current_period_start") else None,
            current_period_end=str(r.get("current_period_end")) if r.get("current_period_end") else None,
            cancel_at_period_end=bool(r.get("cancel_at_period_end", False)),
            interval=str(r.get("interval")) if r.get("interval") else None,
            interval_count=int(r["interval_count"]) if r.get("interval_count") else None,
            metadata=r.get("metadata") if isinstance(r.get("metadata"), dict) else None,
        )

    def resolve_credit_topup(
        self,
        provider: str,
        product_id: str | None = None,
        price_id: str | None = None,
    ) -> dict | None:
        result = self._supabase.rpc(
            "resolve_credit_topup_by_price",
            {
                "p_provider": provider,
                "p_price_id": price_id,
                "p_product_id": product_id,
            },
        )
        if "error" in result and result["error"]:
            raise RuntimeError(result["error"])
        data = result.get("data")
        return data if data else None

    def upsert_billing_payment(
        self,
        provider: str,
        provider_payment_id: str,
        provider_invoice_id: str | None = None,
        user_id: str | None = None,
        amount_minor: int = 0,
        tax_minor: int | None = None,
        currency: str = "USD",
        purpose: str = "unknown",
        metadata: dict | None = None,
    ) -> None:
        result = self._supabase.rpc(
            "upsert_billing_payment",
            {
                "p_provider": provider,
                "p_provider_payment_id": provider_payment_id,
                "p_provider_invoice_id": provider_invoice_id,
                "p_user_id": user_id,
                "p_amount_minor": amount_minor,
                "p_tax_minor": tax_minor,
                "p_currency": currency,
                "p_purpose": purpose,
                "p_metadata": metadata,
            },
        )
        if "error" in result and result["error"]:
            raise RuntimeError(result["error"])

    def upsert_billing_refund(
        self,
        provider: str,
        provider_refund_id: str,
        provider_payment_id: str | None = None,
        user_id: str | None = None,
        amount_minor: int = 0,
        currency: str = "USD",
        reason: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        result = self._supabase.rpc(
            "upsert_billing_refund",
            {
                "p_provider": provider,
                "p_provider_refund_id": provider_refund_id,
                "p_provider_payment_id": provider_payment_id,
                "p_user_id": user_id,
                "p_amount_minor": amount_minor,
                "p_currency": currency,
                "p_reason": reason,
                "p_metadata": metadata,
            },
        )
        if "error" in result and result["error"]:
            raise RuntimeError(result["error"])

    def upsert_billing_invoice(
        self,
        provider: str,
        provider_invoice_id: str,
        provider_subscription_id: str | None = None,
        user_id: str | None = None,
        status: str | None = None,
        amount_paid_minor: int | None = None,
        amount_due_minor: int | None = None,
        currency: str = "USD",
        period_start: str | None = None,
        period_end: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        result = self._supabase.rpc(
            "upsert_billing_invoice",
            {
                "p_provider": provider,
                "p_provider_invoice_id": provider_invoice_id,
                "p_provider_subscription_id": provider_subscription_id,
                "p_user_id": user_id,
                "p_status": status,
                "p_amount_paid_minor": amount_paid_minor,
                "p_amount_due_minor": amount_due_minor,
                "p_currency": currency,
                "p_period_start": period_start,
                "p_period_end": period_end,
                "p_metadata": metadata,
            },
        )
        if "error" in result and result["error"]:
            raise RuntimeError(result["error"])

    def upsert_billing_dispute(
        self,
        provider: str,
        provider_dispute_id: str,
        provider_payment_id: str | None = None,
        user_id: str | None = None,
        status: str = "needs_response",
        reason: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        result = self._supabase.rpc(
            "upsert_billing_dispute",
            {
                "p_provider": provider,
                "p_provider_dispute_id": provider_dispute_id,
                "p_provider_payment_id": provider_payment_id,
                "p_user_id": user_id,
                "p_status": status,
                "p_reason": reason,
                "p_metadata": metadata,
            },
        )
        if "error" in result and result["error"]:
            raise RuntimeError(result["error"])

    def get_billing_payment(
        self,
        provider: str,
        provider_payment_id: str,
    ) -> dict | None:
        result = self._supabase.rpc(
            "get_billing_payment_for_refund",
            {
                "p_provider": provider,
                "p_provider_payment_id": provider_payment_id,
            },
        )
        if "error" in result and result["error"]:
            raise RuntimeError(result["error"])
        data = result.get("data")
        return data if data else None
