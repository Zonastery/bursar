from __future__ import annotations

import json
from datetime import datetime

import psycopg2
import psycopg2.extras
import psycopg2.pool

from bursar.billing.models import (
    BillingConfig,
    BillingEventClaim,
    BillingSubscriptionState,
)
from bursar.billing.store import BillingStore


def _to_utc_iso(dt_str: str | None) -> str | None:
    if dt_str is None:
        return None
    if isinstance(dt_str, datetime):
        return dt_str.isoformat()
    return dt_str


class PostgresBillingStore(BillingStore):
    def __init__(self, database_url: str, pool: psycopg2.pool.ThreadedConnectionPool | None = None) -> None:
        self._database_url = database_url
        self._pool = pool or psycopg2.pool.ThreadedConnectionPool(1, 10, database_url)

    # ── Synchronous helpers ────────────────────────────────────────────

    def _call_rpc_json_sync(self, rpc_name: str, params: list) -> dict | None:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.callproc(rpc_name, params)
                row = cur.fetchone()
            conn.commit()
        except psycopg2.Error:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)
        if row and isinstance(row[0], dict):
            return row[0]
        return None

    def _call_rpc_void_sync(self, rpc_name: str, params: list) -> None:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.callproc(rpc_name, params)
            conn.commit()
        except psycopg2.Error:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    # ── Async public API ────────────────────────────────────────────────

    def sync_billing_from_config(self, config: BillingConfig) -> None:
        raw = config.model_dump()
        config_json = json.dumps(raw, default=str)
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT public.sync_billing_from_config(%s::jsonb)", [config_json])
            conn.commit()
        except psycopg2.Error:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    def resolve_billing_offer(
        self,
        provider: str,
        product_id: str | None = None,
        price_id: str | None = None,
    ) -> dict | None:
        result = self._call_rpc_json_sync(
            "public.resolve_billing_offer_by_price",
            [
                provider,
                price_id,
                product_id,
            ],
        )
        if result and "offer_key" in result:
            return result
        return None

    def claim_billing_event(
        self,
        provider: str,
        event_id: str,
        event_type: str,
    ) -> BillingEventClaim:
        result = self._call_rpc_json_sync(
            "public.claim_billing_event",
            [
                provider,
                event_id,
                event_type,
                json.dumps({"event_type": event_type}),
            ],
        )
        if result is None:
            return BillingEventClaim(status="retry")
        status = result.get("status", "retry")
        return BillingEventClaim(status=status)

    def complete_billing_event(self, provider: str, event_id: str) -> None:
        self._call_rpc_void_sync("public.complete_billing_event", [provider, event_id])

    def fail_billing_event(self, provider: str, event_id: str) -> None:
        self._call_rpc_void_sync("public.fail_billing_event", [provider, event_id])

    def _upsert_billing_customer_sync(
        self,
        provider: str,
        provider_customer_id: str,
        user_id: str,
        email: str | None = None,
    ) -> None:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.billing_customers (provider, provider_customer_id, user_id, email)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (provider, provider_customer_id) DO UPDATE SET
                        user_id = EXCLUDED.user_id,
                        email = COALESCE(EXCLUDED.email, billing_customers.email),
                        updated_at = now()
                    """,
                    [provider, provider_customer_id, user_id, email],
                )
            conn.commit()
        except psycopg2.Error:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    def upsert_billing_customer(
        self,
        provider: str,
        provider_customer_id: str,
        user_id: str,
        email: str | None = None,
    ) -> None:
        self._upsert_billing_customer_sync(provider, provider_customer_id, user_id, email)

    def _upsert_billing_subscription_sync(self, state: BillingSubscriptionState) -> None:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.billing_subscriptions (
                        user_id, provider, provider_subscription_id, provider_customer_id,
                        offer_key, plan_key, status, current_period_start,
                        current_period_end, cancel_at_period_end, interval, interval_count, metadata
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (provider, provider_subscription_id) DO UPDATE SET
                        user_id = EXCLUDED.user_id,
                        provider_customer_id = COALESCE(
                            EXCLUDED.provider_customer_id, billing_subscriptions.provider_customer_id
                        ),
                        offer_key = COALESCE(EXCLUDED.offer_key, billing_subscriptions.offer_key),
                        plan_key = COALESCE(EXCLUDED.plan_key, billing_subscriptions.plan_key),
                        status = EXCLUDED.status,
                        current_period_start = COALESCE(
                            EXCLUDED.current_period_start, billing_subscriptions.current_period_start
                        ),
                        current_period_end = COALESCE(
                            EXCLUDED.current_period_end, billing_subscriptions.current_period_end
                        ),
                        cancel_at_period_end = EXCLUDED.cancel_at_period_end,
                        interval = COALESCE(EXCLUDED.interval, billing_subscriptions.interval),
                        interval_count = COALESCE(EXCLUDED.interval_count, billing_subscriptions.interval_count),
                        metadata = CASE WHEN EXCLUDED.metadata IS NOT NULL
                            THEN EXCLUDED.metadata ELSE billing_subscriptions.metadata END,
                        updated_at = now()
                    """,
                    [
                        state.user_id,
                        state.provider,
                        state.provider_subscription_id,
                        state.provider_customer_id,
                        state.offer_key,
                        state.plan_key,
                        state.status,
                        _to_utc_iso(state.current_period_start),
                        _to_utc_iso(state.current_period_end),
                        state.cancel_at_period_end,
                        state.interval,
                        state.interval_count,
                        json.dumps(state.metadata) if state.metadata else None,
                    ],
                )
            conn.commit()
        except psycopg2.Error:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    def upsert_billing_subscription(self, state: BillingSubscriptionState) -> None:
        self._upsert_billing_subscription_sync(state)

    def _get_billing_customer_sync(
        self,
        provider: str,
        provider_customer_id: str,
    ) -> str | None:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT user_id FROM public.billing_customers WHERE provider = %s AND provider_customer_id = %s",
                    [provider, provider_customer_id],
                )
                row = cur.fetchone()
            return str(row[0]) if row else None
        finally:
            self._pool.putconn(conn)

    def get_billing_customer(
        self,
        provider: str,
        provider_customer_id: str,
    ) -> str | None:
        return self._get_billing_customer_sync(provider, provider_customer_id)

    def _get_billing_subscription_sync(
        self,
        provider: str,
        provider_subscription_id: str,
    ) -> BillingSubscriptionState | None:
        conn = self._pool.getconn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT user_id, provider, provider_subscription_id, provider_customer_id,
                           offer_key, plan_key, status, current_period_start,
                           current_period_end, cancel_at_period_end, interval, interval_count, metadata
                    FROM public.billing_subscriptions
                    WHERE provider = %s AND provider_subscription_id = %s
                    """,
                    [provider, provider_subscription_id],
                )
                row = cur.fetchone()
            if not row:
                return None

            meta = row["metadata"]
            return BillingSubscriptionState(
                user_id=str(row["user_id"]),
                provider=str(row["provider"]),
                provider_subscription_id=str(row["provider_subscription_id"]),
                provider_customer_id=str(row["provider_customer_id"]) if row["provider_customer_id"] else None,
                offer_key=str(row["offer_key"]) if row["offer_key"] else None,
                plan_key=str(row["plan_key"]) if row["plan_key"] else None,
                status=str(row["status"]) if row["status"] else "incomplete",
                current_period_start=str(row["current_period_start"]) if row["current_period_start"] else None,
                current_period_end=str(row["current_period_end"]) if row["current_period_end"] else None,
                cancel_at_period_end=bool(row["cancel_at_period_end"]),
                interval=str(row["interval"]) if row["interval"] else None,
                interval_count=int(row["interval_count"]) if row["interval_count"] else None,
                metadata=meta if isinstance(meta, dict) else None,
            )
        finally:
            self._pool.putconn(conn)

    def get_billing_subscription(
        self,
        provider: str,
        provider_subscription_id: str,
    ) -> BillingSubscriptionState | None:
        return self._get_billing_subscription_sync(provider, provider_subscription_id)

    def resolve_credit_topup(
        self,
        provider: str,
        product_id: str | None = None,
        price_id: str | None = None,
    ) -> dict | None:
        result = self._call_rpc_json_sync(
            "public.resolve_credit_topup_by_price",
            [
                provider,
                price_id,
                product_id,
            ],
        )
        if result and "topup_key" in result:
            return result
        return None

    def compute_topup_credits(self, amount_minor: int, topup_config: dict) -> int:
        credits_per = topup_config.get("credits_per_major_unit", 1000)
        return (amount_minor * credits_per) // 100
