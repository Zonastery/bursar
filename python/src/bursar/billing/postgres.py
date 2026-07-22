"""PostgreSQL-backed billing store adapter.

Connects directly via psycopg2 to a Postgres database with the billing
schema installed. Wraps all billing repositories under a single store class.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Any

import psycopg2
import psycopg2.extras
import psycopg2.pool

from bursar.billing.models import (
    BillingAutoRechargeAttempt,
    BillingAutoRechargeProfile,
    BillingConfig,
    BillingCustomerRecord,
    BillingEventClaim,
    BillingGrantResult,
    BillingOfferResult,
    BillingPreferences,
    BillingSubscriptionChange,
    BillingSubscriptionState,
    BillingSubscriptionStatus,
    BillingTopupResult,
    CheckoutIntent,
    CheckoutIntentStatus,
)
from bursar.billing.store import BillingStore
from bursar.repositories.billing.config import BillingConfigRepository
from bursar.repositories.billing.customer import BillingCustomerRepository
from bursar.repositories.billing.dispute import BillingDisputeRepository
from bursar.repositories.billing.event import BillingEventRepository
from bursar.repositories.billing.invoice import BillingInvoiceRepository
from bursar.repositories.billing.offer import BillingOfferRepository
from bursar.repositories.billing.payment import BillingPaymentRepository
from bursar.repositories.billing.preferences import BillingPreferencesRepository
from bursar.repositories.billing.refund import BillingRefundRepository
from bursar.repositories.billing.subscription import BillingSubscriptionRepository
from bursar.repositories.billing.topup import BillingTopupRepository
from bursar.repositories.schemas import SubscriptionRow


def _dec_credits(value: str | Decimal | None) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _to_utc_iso(dt_str: str | None) -> str | None:
    if dt_str is None:
        return None
    try:
        return datetime.fromisoformat(dt_str).isoformat()
    except (ValueError, TypeError):
        return dt_str


class PostgresBillingStore(BillingStore):
    """Billing store backed by a raw Postgres connection with pooling.

    Wraps all billing repositories (offer, topup, customer, subscription,
    event, payment, refund, invoice, dispute, config) under a single
    interface. All public methods delegate to the corresponding repository.

    Args:
        database_url: Postgres connection string.
        pool: Optional existing connection pool; created if not provided.
    """

    def __init__(self, database_url: str, pool: psycopg2.pool.ThreadedConnectionPool | None = None) -> None:
        self._database_url = database_url
        self._pool = pool or psycopg2.pool.ThreadedConnectionPool(1, 10, database_url)

    def close(self) -> None:
        """Close all connections in the pool."""
        self._pool.closeall()

    def __enter__(self) -> PostgresBillingStore:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def _execute(self, sql: str, params: list[Any] | None = None) -> list[Any]:
        """Execute raw SQL and return all result rows as dicts via the connection pool."""
        conn = self._pool.getconn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, params or [])
                try:
                    rows = cur.fetchall()
                except psycopg2.ProgrammingError:
                    rows = []
            conn.commit()
            return rows
        except BaseException:
            # Pool corruption — rollback and re-raise to protect connection state
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    @property
    def _offer_repo(self) -> BillingOfferRepository:
        if not hasattr(self, "_offer_repo_cache"):
            self._offer_repo_cache = BillingOfferRepository(self._execute)
        return self._offer_repo_cache

    @property
    def _topup_repo(self) -> BillingTopupRepository:
        if not hasattr(self, "_topup_repo_cache"):
            self._topup_repo_cache = BillingTopupRepository(self._execute)
        return self._topup_repo_cache

    @property
    def _customer_repo(self) -> BillingCustomerRepository:
        if not hasattr(self, "_customer_repo_cache"):
            self._customer_repo_cache = BillingCustomerRepository(self._execute)
        return self._customer_repo_cache

    @property
    def _subscription_repo(self) -> BillingSubscriptionRepository:
        if not hasattr(self, "_subscription_repo_cache"):
            self._subscription_repo_cache = BillingSubscriptionRepository(self._execute)
        return self._subscription_repo_cache

    @property
    def _event_repo(self) -> BillingEventRepository:
        if not hasattr(self, "_event_repo_cache"):
            self._event_repo_cache = BillingEventRepository(self._execute)
        return self._event_repo_cache

    @property
    def _payment_repo(self) -> BillingPaymentRepository:
        if not hasattr(self, "_payment_repo_cache"):
            self._payment_repo_cache = BillingPaymentRepository(self._execute)
        return self._payment_repo_cache

    @property
    def _refund_repo(self) -> BillingRefundRepository:
        if not hasattr(self, "_refund_repo_cache"):
            self._refund_repo_cache = BillingRefundRepository(self._execute)
        return self._refund_repo_cache

    @property
    def _invoice_repo(self) -> BillingInvoiceRepository:
        if not hasattr(self, "_invoice_repo_cache"):
            self._invoice_repo_cache = BillingInvoiceRepository(self._execute)
        return self._invoice_repo_cache

    @property
    def _dispute_repo(self) -> BillingDisputeRepository:
        if not hasattr(self, "_dispute_repo_cache"):
            self._dispute_repo_cache = BillingDisputeRepository(self._execute)
        return self._dispute_repo_cache

    @property
    def _config_repo(self) -> BillingConfigRepository:
        if not hasattr(self, "_config_repo_cache"):
            self._config_repo_cache = BillingConfigRepository(self._execute)
        return self._config_repo_cache

    @property
    def _preferences_repo(self) -> BillingPreferencesRepository:
        if not hasattr(self, "_preferences_repo_cache"):
            self._preferences_repo_cache = BillingPreferencesRepository(self._execute)
        return self._preferences_repo_cache

    # ── Helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _row_to_subscription_state(r: SubscriptionRow | None) -> BillingSubscriptionState | None:
        if r is None:
            return None
        return BillingSubscriptionState(
            user_id=str(r.user_id),
            provider=str(r.provider),
            provider_subscription_id=str(r.provider_subscription_id),
            provider_customer_id=str(r.provider_customer_id) if r.provider_customer_id else None,
            offer_key=str(r.offer_key) if r.offer_key else None,
            plan=str(r.plan) if r.plan else None,
            status=BillingSubscriptionStatus(str(r.status)) if r.status else BillingSubscriptionStatus.incomplete,
            current_period_start=str(r.current_period_start) if r.current_period_start else None,
            current_period_end=str(r.current_period_end) if r.current_period_end else None,
            cancel_at_period_end=bool(r.cancel_at_period_end),
            interval=str(r.interval) if r.interval else None,
            interval_count=int(r.interval_count) if r.interval_count is not None else None,
            grace_ends_at=str(r.grace_ends_at) if getattr(r, "grace_ends_at", None) else None,
            metadata=r.metadata if isinstance(r.metadata, dict) else None,
            catalog_version=int(cv) if (cv := getattr(r, "catalog_version", None)) is not None else None,
            plan_version_id=str(pv) if (pv := getattr(r, "plan_version_id", None)) else None,
        )

    @staticmethod
    def _row_to_subscription_change(row: dict[str, Any] | None) -> BillingSubscriptionChange | None:
        if not row:
            return None
        return BillingSubscriptionChange(
            id=str(row["id"]),
            user_id=str(row["user_id"]),
            provider=str(row["provider"]),
            provider_subscription_id=str(row["provider_subscription_id"]),
            from_plan=row.get("from_plan"),
            from_interval=row.get("from_interval"),
            to_plan=str(row["to_plan"]),
            to_interval=str(row["to_interval"]),
            effective_at=str(row["effective_at"]),
            state=str(row["state"]),
            proration_billing_mode=str(row["proration_billing_mode"]),
            quote=row.get("quote") or {},
            quote_hash=str(row["quote_hash"]),
            provider_operation_id=row.get("provider_operation_id"),
            effective_date=str(row["effective_date"]) if row.get("effective_date") else None,
            expires_at=str(row["expires_at"]) if row.get("expires_at") else None,
        )

    @staticmethod
    def _row_to_offer(result: Any) -> BillingOfferResult | None:
        if result is None:
            return None
        return BillingOfferResult(
            offer_key=str(result.offer_key),
            plan=result.plan,
            interval=result.interval,
            interval_count=result.interval_count,
            grant=BillingGrantResult(
                mode=result.grant_mode,
                credits=result.grant_credits,
                bucket=result.grant_bucket,
                replace_prior=result.grant_replace_prior,
            ),
        )

    @staticmethod
    def _row_to_topup(result: Any) -> BillingTopupResult | None:
        if result is None:
            return None
        min_amt = (
            int(result.min_amount_minor)
            if hasattr(result, "min_amount_minor") and result.min_amount_minor is not None
            else 500
        )
        max_amt = (
            int(result.max_amount_minor)
            if hasattr(result, "max_amount_minor") and result.max_amount_minor is not None
            else 500000
        )
        return BillingTopupResult(
            topup_key=str(result.topup_key),
            credits_per_unit=_dec_credits(result.credits_per_unit),
            deposit_to=result.deposit_to or "purchased",
            min_amount_minor=min_amt,
            max_amount_minor=max_amt,
        )

    # ── Public API ─────────────────────────────────────────────────────

    def sync_billing_from_config(self, config: BillingConfig) -> None:
        """Sync the full billing configuration from a BillingConfig object.

        Args:
            config: The billing configuration to sync.
        """
        raw = config.model_dump()
        config_json = json.dumps(raw, default=str)
        self._config_repo.sync_from_config(config_json)

    def create_or_get_checkout_intent(
        self,
        actor_key: str,
        provider: str,
        type: str,
        product_id: str,
        request_fingerprint: str,
        expires_at: str,
    ) -> CheckoutIntent:
        """Create or retrieve an open checkout intent for an actor key.

        Atomically expires any old open intents for the same actor,
        then inserts a new one (or returns the existing one via ON CONFLICT).
        """
        self._execute(
            "UPDATE bursar.billing_checkout_intents"
            " SET status = 'expired', updated_at = now()"
            " WHERE actor_key = %s AND status = 'open' AND expires_at <= now()"
            " RETURNING id",
            [actor_key],
        )
        rows = self._execute(
            "INSERT INTO bursar.billing_checkout_intents"
            " (actor_key, provider, type, product_id, request_fingerprint, expires_at)"
            " VALUES (%s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (actor_key) WHERE status = 'open'"
            " DO UPDATE SET updated_at = now()"
            " RETURNING id, actor_key, provider, type, product_id, request_fingerprint, status,"
            "           provider_session_id, checkout_url, expires_at",
            [actor_key, provider, type, product_id, request_fingerprint, expires_at],
        )
        r = rows[0]
        return CheckoutIntent(
            id=str(r["id"]),
            actor_key=str(r["actor_key"]),
            provider=str(r["provider"]),
            type=r["type"],
            product_id=str(r["product_id"]),
            request_fingerprint=str(r["request_fingerprint"]),
            status=CheckoutIntentStatus(str(r["status"])),
            provider_session_id=str(r["provider_session_id"]) if r.get("provider_session_id") else None,
            checkout_url=str(r["checkout_url"]) if r.get("checkout_url") else None,
            expires_at=str(r["expires_at"]),
        )

    def update_checkout_intent(
        self,
        id: str,
        status: str | None = None,
        provider_session_id: str | None = None,
        checkout_url: str | None = None,
    ) -> None:
        """Update a checkout intent's status and optional session/URL fields."""
        self._execute(
            "UPDATE bursar.billing_checkout_intents"
            " SET status = COALESCE(%s, status),"
            "     provider_session_id = COALESCE(%s, provider_session_id),"
            "     checkout_url = COALESCE(%s, checkout_url),"
            "     updated_at = now()"
            " WHERE id = %s",
            [status, provider_session_id, checkout_url, id],
        )

    def resolve_billing_offer(
        self,
        provider: str,
        product_id: str | None = None,
        price_id: str | None = None,
    ) -> BillingOfferResult | None:
        """Resolve a billing offer by provider and product/price IDs.

        Args:
            provider: The billing provider identifier.
            product_id: The provider product ID, or None.
            price_id: The provider price ID, or None.

        Returns:
            BillingOfferResult if found, None otherwise.
        """
        result = self._offer_repo.resolve_by_price(provider, price_id, product_id)
        return self._row_to_offer(result)

    def claim_billing_event(
        self,
        provider: str,
        event_id: str,
        event_type: str,
    ) -> BillingEventClaim:
        """Claim a billing event for processing (idempotent).

        Args:
            provider: The billing provider identifier.
            event_id: The provider event ID.
            event_type: The event type string.

        Returns:
            BillingEventClaim with status ("ok", "retry", etc.).
        """
        result = self._event_repo.claim(
            provider,
            event_id,
            event_type,
            json.dumps({"event_type": event_type}),
        )
        if result is None:
            return BillingEventClaim(status="retry")
        raw_status = (result.status or "retry").lower()
        if raw_status not in ("claimed", "duplicate", "retry"):
            raw_status = "retry"
        return BillingEventClaim(status=raw_status, claim_token=getattr(result, "claim_token", None))

    def complete_billing_event(self, provider: str, event_id: str, claim_token: str) -> None:
        """Mark a billing event as completed.

        Args:
            provider: The billing provider identifier.
            event_id: The provider event ID.
        """
        self._event_repo.complete(provider, event_id, claim_token)

    def fail_billing_event(self, provider: str, event_id: str, claim_token: str, error: str | None = None) -> None:
        """Mark a billing event as failed.

        Args:
            provider: The billing provider identifier.
            event_id: The provider event ID.
        """
        self._event_repo.fail(provider, event_id, claim_token, error)

    def upsert_billing_customer(
        self,
        provider: str,
        provider_customer_id: str,
        user_id: str,
        email: str | None = None,
    ) -> dict[str, Any]:
        """Insert or update a billing customer record."""
        return self._customer_repo.upsert(provider, provider_customer_id, user_id, email)

    def upsert_billing_subscription(self, state: BillingSubscriptionState) -> None:
        """Insert or update a billing subscription record.

        Args:
            state: The subscription state to persist.
        """
        self._subscription_repo.upsert(
            {
                "user_id": state.user_id,
                "provider": state.provider,
                "provider_subscription_id": state.provider_subscription_id,
                "provider_customer_id": state.provider_customer_id,
                "offer_key": state.offer_key,
                "plan": state.plan,
                "status": state.status,
                "current_period_start": _to_utc_iso(state.current_period_start),
                "current_period_end": _to_utc_iso(state.current_period_end),
                "cancel_at_period_end": state.cancel_at_period_end,
                "interval": state.interval,
                "interval_count": state.interval_count,
                "grace_ends_at": _to_utc_iso(state.grace_ends_at),
                "metadata": state.metadata,
                "catalog_version": state.catalog_version,
                "plan_version_id": state.plan_version_id,
            }
        )

    def get_billing_customer(
        self,
        provider: str,
        provider_customer_id: str,
    ) -> str | None:
        """Get the user ID associated with a provider customer.

        Args:
            provider: The billing provider identifier.
            provider_customer_id: The provider customer ID.

        Returns:
            The user ID string if found, None otherwise.
        """
        return self._customer_repo.get(provider, provider_customer_id)

    def get_billing_subscription(
        self,
        provider: str,
        provider_subscription_id: str,
    ) -> BillingSubscriptionState | None:
        """Get a subscription by provider and provider subscription ID.

        Args:
            provider: The billing provider identifier.
            provider_subscription_id: The provider subscription ID.

        Returns:
            BillingSubscriptionState if found, None otherwise.
        """
        result = self._subscription_repo.get(provider, provider_subscription_id)
        return self._row_to_subscription_state(result)

    def get_user_subscription(
        self,
        user_id: str,
        statuses: list[str] | None = None,
    ) -> BillingSubscriptionState | None:
        """Get the most recent subscription for a user, filtered by status.

        Args:
            user_id: The user ID.
            statuses: Optional list of statuses to filter by.
                      Defaults to (active, trialing).

        Returns:
            BillingSubscriptionState if found, None otherwise.
        """
        result = self._subscription_repo.get_user_subscription(user_id, status=tuple(statuses) if statuses else None)
        return self._row_to_subscription_state(result)

    def create_billing_subscription_change(self, change: BillingSubscriptionChange) -> BillingSubscriptionChange:
        rows = self._execute(
            """INSERT INTO bursar.billing_subscription_changes
            (id, user_id, provider, provider_subscription_id, from_plan, from_interval, to_plan, to_interval,
             effective_at, state, proration_billing_mode, quote, quote_hash, provider_operation_id,
             effective_date, expires_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s) RETURNING *""",
            [
                change.id,
                change.user_id,
                change.provider,
                change.provider_subscription_id,
                change.from_plan,
                change.from_interval,
                change.to_plan,
                change.to_interval,
                change.effective_at,
                change.state,
                change.proration_billing_mode,
                json.dumps(change.quote),
                change.quote_hash,
                change.provider_operation_id,
                change.effective_date,
                change.expires_at,
            ],
        )
        return self._row_to_subscription_change(rows[0])

    def get_open_billing_subscription_change(
        self, provider: str, provider_subscription_id: str
    ) -> BillingSubscriptionChange | None:
        rows = self._execute(
            "SELECT * FROM bursar.billing_subscription_changes "
            "WHERE provider=%s AND provider_subscription_id=%s "
            "AND state IN ('awaiting_payment','scheduled') "
            "ORDER BY created_at DESC LIMIT 1",
            [provider, provider_subscription_id],
        )
        return self._row_to_subscription_change(rows[0]) if rows else None

    def update_billing_subscription_change(self, id: str, **updates: Any) -> None:
        allowed = {"state", "provider_operation_id", "effective_date"}
        fields = [(key, value) for key, value in updates.items() if key in allowed]
        if not fields:
            return
        assignments = ", ".join(f"{key} = %s" for key, _ in fields)
        self._execute(
            f"UPDATE bursar.billing_subscription_changes SET {assignments}, updated_at=now() WHERE id=%s",
            [*(value for _, value in fields), id],
        )

    def resolve_credit_topup(
        self,
        provider: str,
        product_id: str | None = None,
        price_id: str | None = None,
    ) -> BillingTopupResult | None:
        """Resolve a credit topup by provider and product/price IDs.

        Args:
            provider: The billing provider identifier.
            product_id: The provider product ID, or None.
            price_id: The provider price ID, or None.

        Returns:
            BillingTopupResult if found, None otherwise.
        """
        result = self._topup_repo.resolve_by_price(provider, price_id, product_id)
        return self._row_to_topup(result)

    def resolve_billing_offer_by_lookup(
        self,
        provider: str,
        lookup_key: str,
    ) -> BillingOfferResult | None:
        """Resolve a billing offer by provider and lookup key.

        Args:
            provider: The billing provider identifier.
            lookup_key: The offer lookup key.

        Returns:
            BillingOfferResult if found, None otherwise.
        """
        result = self._offer_repo.resolve_by_lookup(provider, lookup_key)
        return self._row_to_offer(result)

    def resolve_credit_topup_by_lookup(
        self,
        provider: str,
        lookup_key: str,
    ) -> BillingTopupResult | None:
        """Resolve a credit topup by provider and lookup key.

        Args:
            provider: The billing provider identifier.
            lookup_key: The topup lookup key.

        Returns:
            BillingTopupResult if found, None otherwise.
        """
        result = self._topup_repo.resolve_by_lookup(provider, lookup_key)
        return self._row_to_topup(result)

    def upsert_billing_payment(
        self,
        *,
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
        """Insert or update a billing payment record.

        Args:
            provider: The billing provider identifier.
            provider_payment_id: The provider payment ID.
            provider_invoice_id: The associated invoice ID, or None.
            user_id: The user ID, or None.
            amount_minor: The payment amount in minor currency units.
            tax_minor: The tax amount in minor currency units, or None.
            currency: The ISO 4217 currency code (default "USD").
            purpose: The payment purpose (default "unknown").
            metadata: Optional structured metadata dict.
        """
        self._payment_repo.upsert(
            provider,
            provider_payment_id,
            provider_invoice_id,
            user_id,
            amount_minor,
            tax_minor,
            currency,
            purpose,
            json.dumps(metadata) if metadata else None,
        )

    def upsert_billing_refund(
        self,
        *,
        provider: str,
        provider_refund_id: str,
        provider_payment_id: str | None = None,
        user_id: str | None = None,
        amount_minor: int = 0,
        currency: str = "USD",
        reason: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        """Insert or update a billing refund record.

        Args:
            provider: The billing provider identifier.
            provider_refund_id: The provider refund ID.
            provider_payment_id: The associated payment ID, or None.
            user_id: The user ID, or None.
            amount_minor: The refund amount in minor currency units.
            currency: The ISO 4217 currency code (default "USD").
            reason: The refund reason, or None.
            metadata: Optional structured metadata dict.
        """
        self._refund_repo.upsert(
            provider,
            provider_refund_id,
            provider_payment_id,
            user_id,
            amount_minor,
            currency,
            reason,
            json.dumps(metadata) if metadata else None,
        )

    def upsert_billing_invoice(
        self,
        *,
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
        """Insert or update a billing invoice record.

        Args:
            provider: The billing provider identifier.
            provider_invoice_id: The provider invoice ID.
            provider_subscription_id: The associated subscription ID, or None.
            user_id: The user ID, or None.
            status: The invoice status, or None.
            amount_paid_minor: Amount paid in minor currency units, or None.
            amount_due_minor: Amount due in minor currency units, or None.
            currency: The ISO 4217 currency code (default "USD").
            period_start: The billing period start, or None.
            period_end: The billing period end, or None.
            metadata: Optional structured metadata dict.
        """
        self._invoice_repo.upsert(
            provider,
            provider_invoice_id,
            provider_subscription_id,
            user_id,
            status,
            amount_paid_minor,
            amount_due_minor,
            currency,
            period_start,
            period_end,
            json.dumps(metadata) if metadata else None,
        )

    def upsert_billing_dispute(
        self,
        *,
        provider: str,
        provider_dispute_id: str,
        provider_payment_id: str | None = None,
        user_id: str | None = None,
        status: str = "needs_response",
        reason: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        """Insert or update a billing dispute record.

        Args:
            provider: The billing provider identifier.
            provider_dispute_id: The provider dispute ID.
            provider_payment_id: The associated payment ID, or None.
            user_id: The user ID, or None.
            status: The dispute status (default "needs_response").
            reason: The dispute reason, or None.
            metadata: Optional structured metadata dict.
        """
        self._dispute_repo.upsert(
            provider,
            provider_dispute_id,
            provider_payment_id,
            user_id,
            status,
            reason,
            json.dumps(metadata) if metadata else None,
        )

    def get_billing_payment(
        self,
        provider: str,
        provider_payment_id: str,
    ) -> dict | None:
        """Get payment details for refund processing.

        Args:
            provider: The billing provider identifier.
            provider_payment_id: The provider payment ID.

        Returns:
            Payment details dict if found, None otherwise.
        """
        result = self._payment_repo.get_for_refund(provider, provider_payment_id)
        return result.model_dump(exclude_none=True) if result else None

    def get_user_subscriptions(self, user_id: str) -> list[BillingSubscriptionState]:
        """Get all subscriptions for a user.

        Args:
            user_id: The user ID.

        Returns:
            List of BillingSubscriptionState (may be empty).
        """
        rows = self._subscription_repo.get_user_subscriptions(user_id)
        return [s for r in rows if (s := self._row_to_subscription_state(r)) is not None]

    def deactivate_other_provider_subscriptions(
        self,
        user_id: str,
        keep_provider: str,
    ) -> dict[str, Any]:
        """Cancel all active/trialing subscriptions for a user except the given provider.

        Args:
            user_id: The user ID.
            keep_provider: The provider whose subscriptions should be preserved.

        Returns:
            Dict with deactivated_count and deactivated_ids.
        """
        ids = self._subscription_repo.deactivate_other_provider_subscriptions(user_id, keep_provider)
        return {"deactivated_count": len(ids), "deactivated_ids": ids}

    def record_subscription_conflict(
        self,
        user_id: str | None = None,
        provider: str = "",
        duplicate_subscription_id: str = "",
        existing_subscription_id: str | None = None,
        event_id: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        """Record a subscription conflict for manual review.

        Uses ON CONFLICT DO NOTHING for idempotent insertion.
        """
        self._execute(
            "INSERT INTO bursar.billing_subscription_conflicts"
            " (user_id, provider, duplicate_subscription_id, existing_subscription_id, event_id, metadata)"
            " VALUES (%s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (provider, duplicate_subscription_id) DO NOTHING",
            [
                user_id,
                provider,
                duplicate_subscription_id,
                existing_subscription_id,
                event_id,
                json.dumps(metadata) if metadata else "{}",
            ],
        )

    def get_billing_preferences(self, user_id: str) -> BillingPreferences | None:
        """Get billing preferences for a user.

        Args:
            user_id: The user ID.

        Returns:
            BillingPreferences if found, None otherwise.
        """
        row = self._preferences_repo.get(user_id)
        if row is None:
            return None
        return BillingPreferences(
            user_id=str(row.get("user_id", "")),
            auto_recharge=bool(row.get("auto_recharge", False)),
            overage_protection=bool(row.get("overage_protection", True)),
            email_notifications=bool(row.get("email_notifications", True)),
            usage_alerts=bool(row.get("usage_alerts", True)),
            invoice_reminders=bool(row.get("invoice_reminders", False)),
            usage_limit_alerts=bool(row.get("usage_limit_alerts", True)),
        )

    def upsert_billing_preferences(self, prefs: BillingPreferences) -> None:
        """Insert or update billing preferences for a user.

        Args:
            prefs: The billing preferences to persist.
        """
        self._preferences_repo.upsert(prefs.model_dump())

    def get_auto_recharge_profile(self, user_id: str) -> BillingAutoRechargeProfile | None:
        rows = self._execute("SELECT * FROM bursar.billing_auto_recharge_profiles WHERE user_id=%s", [user_id])
        if not rows:
            return None
        row = rows[0]
        return BillingAutoRechargeProfile(
            user_id=str(row["user_id"]),
            enabled=bool(row["enabled"]),
            state=str(row["state"]),
            provider=row.get("provider"),
            provider_customer_id=row.get("provider_customer_id"),
            payment_method_id=row.get("payment_method_id"),
            suspended_reason=row.get("suspended_reason"),
            consented_at=str(row["consented_at"]) if row.get("consented_at") else None,
            consent_reference=row.get("consent_reference"),
            consent_metadata=row.get("consent_metadata"),
            policy_override=row.get("policy_override"),
            policy_snapshot=row.get("policy_snapshot"),
            policy_hash=row.get("policy_hash"),
            quote_snapshot=row.get("quote_snapshot"),
            armed=bool(row.get("armed", True)),
        )

    def upsert_auto_recharge_profile(self, profile: BillingAutoRechargeProfile) -> None:
        self._execute(
            """INSERT INTO bursar.billing_auto_recharge_profiles
              (user_id,enabled,state,provider,provider_customer_id,payment_method_id,suspended_reason,consented_at)
              VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
              ON CONFLICT (user_id) DO UPDATE SET enabled=EXCLUDED.enabled,state=EXCLUDED.state,
              provider=EXCLUDED.provider,provider_customer_id=EXCLUDED.provider_customer_id,
              payment_method_id=EXCLUDED.payment_method_id,suspended_reason=EXCLUDED.suspended_reason,
              consented_at=EXCLUDED.consented_at,consent_reference=EXCLUDED.consent_reference,
                 consent_metadata=EXCLUDED.consent_metadata,policy_override=EXCLUDED.policy_override,
                 policy_snapshot=EXCLUDED.policy_snapshot,policy_hash=EXCLUDED.policy_hash,
                 quote_snapshot=EXCLUDED.quote_snapshot,armed=EXCLUDED.armed,updated_at=now()""",
            [
                profile.user_id,
                profile.enabled,
                profile.state,
                profile.provider,
                profile.provider_customer_id,
                profile.payment_method_id,
                profile.suspended_reason,
                profile.consented_at,
                profile.consent_reference,
                json.dumps(profile.consent_metadata) if profile.consent_metadata is not None else None,
                json.dumps(profile.policy_override) if profile.policy_override is not None else None,
                json.dumps(profile.policy_snapshot) if profile.policy_snapshot is not None else None,
                profile.policy_hash,
                json.dumps(profile.quote_snapshot) if profile.quote_snapshot is not None else None,
                profile.armed,
            ],
        )

    def claim_auto_recharge_attempt(
        self, user_id: str, provider: str, topup_key: str, quantity: int, max_recharges: int, window_days: int
    ) -> BillingAutoRechargeAttempt | None:
        rows = self._execute(
            "SELECT * FROM bursar.claim_auto_recharge_attempt(%s,%s,%s,%s,%s,%s)",
            [user_id, provider, topup_key, quantity, max_recharges, window_days],
        )
        if not rows:
            return None
        row = rows[0]
        return BillingAutoRechargeAttempt(
            id=str(row["id"]),
            user_id=str(row["user_id"]),
            provider=str(row["provider"]),
            idempotency_key=str(row["idempotency_key"]),
            provider_payment_id=row.get("provider_payment_id"),
            topup_key=str(row["topup_key"]),
            quantity=int(row["quantity"]),
            state=str(row["state"]),
            credits=row.get("credits"),
            failure_code=row.get("failure_code"),
            action_url=row.get("action_url"),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def update_auto_recharge_attempt(
        self,
        attempt_id: str,
        state: str,
        provider_payment_id: str | None = None,
        failure_code: str | None = None,
        action_url: str | None = None,
    ) -> None:
        self._execute(
            "UPDATE bursar.billing_auto_recharge_attempts "
            "SET state=%s, provider_payment_id=COALESCE(%s,provider_payment_id), "
            "failure_code=%s, action_url=%s, updated_at=now() WHERE id=%s",
            [state, provider_payment_id, failure_code, action_url, attempt_id],
        )

    def get_billing_customer_by_user_id(
        self,
        user_id: str,
        provider: str | None = None,
    ) -> BillingCustomerRecord | None:
        """Reverse lookup: find a customer record by user ID.

        Args:
            user_id: The user ID.
            provider: Optional provider filter.

        Returns:
            BillingCustomerRecord if found, None otherwise.
        """
        row = self._customer_repo.get_by_user_id(user_id, provider)
        if row is None:
            return None
        return BillingCustomerRecord(
            provider=str(row.get("provider", "")),
            provider_customer_id=str(row.get("provider_customer_id", "")),
        )
