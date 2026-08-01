"""PostgreSQL-backed billing store adapter.

Connects directly via psycopg2 to a Postgres database with the billing
schema installed. Wraps all billing repositories under a single store class.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal, cast
from uuid import UUID

import psycopg2
import psycopg2.extras
import psycopg2.pool

from bursar.billing.billing_store import BillingStore
from bursar.billing.contracts import (
    AutoRechargeAttemptClaim,
    AutoRechargeAttemptUpdate,
    AutoRechargeProviderPaymentUpdate,
    BillingCreditGrantCreate,
    BillingDisputeUpsert,
    BillingInvoiceUpsert,
    BillingPaymentUpsert,
    BillingRefundUpsert,
    BillingSubscriptionChangeUpdate,
    BillingSubscriptionConflictCreate,
    CheckoutIntentCreate,
    CheckoutIntentUpdate,
)
from bursar.billing.postgres.repositories.auto_recharge import BillingTopupRepository
from bursar.billing.postgres.repositories.customer import BillingCustomerRepository
from bursar.billing.postgres.repositories.dispute import BillingDisputeRepository
from bursar.billing.postgres.repositories.event import BillingEventRepository
from bursar.billing.postgres.repositories.invoice import BillingInvoiceRepository
from bursar.billing.postgres.repositories.offer import BillingOfferRepository
from bursar.billing.postgres.repositories.payment import BillingPaymentRepository
from bursar.billing.postgres.repositories.preferences import BillingPreferencesRepository
from bursar.billing.postgres.repositories.refund import BillingRefundRepository
from bursar.billing.postgres.repositories.subscription import (
    BillingSubscriptionRepository,
    provider_timestamp_sort_key,
)
from bursar.billing.types import (
    BillingAutoRechargeAttempt,
    BillingAutoRechargeProfile,
    BillingCustomerRecord,
    BillingEventClaim,
    BillingGrantResult,
    BillingInvoiceInfo,
    BillingOfferResult,
    BillingPreferences,
    BillingSubscriptionChange,
    BillingSubscriptionChangeInput,
    BillingSubscriptionChangeState,
    BillingSubscriptionOfferContext,
    BillingSubscriptionProrationBehavior,
    BillingSubscriptionState,
    BillingSubscriptionStatus,
    BillingTopupResult,
    CheckoutIntent,
    CheckoutIntentStatus,
)
from bursar.credits.postgres.repositories.schemas import SubscriptionRow


def _dec_credits(value: str | Decimal | None) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _to_utc_iso(value: str | datetime | None) -> str | None:
    if value is None:
        return None
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("billing timestamp must include timezone")
    return parsed.astimezone(UTC).isoformat()


class PostgresBillingStore(BillingStore):
    """Billing store backed by a raw Postgres connection with pooling.

    Wraps all billing repositories (offer, topup, customer, subscription,
    event, payment, refund, invoice, dispute, config) under a single
    interface. All public methods delegate to the corresponding repository.

    Args:
        database_url: Postgres connection string.
        pool: Optional existing connection pool; created if not provided.
    """

    def __init__(
        self,
        database_url: str,
        *,
        tenant_id: str | UUID,
        pool: psycopg2.pool.ThreadedConnectionPool | None = None,
    ) -> None:
        self._database_url = database_url
        self._tenant_id = str(UUID(str(tenant_id)))
        self._pool = pool or psycopg2.pool.ThreadedConnectionPool(1, 10, database_url)
        self._owns_pool = pool is None

    def close(self) -> None:
        """Close all connections in the pool."""
        if self._pool is None:
            return
        if self._owns_pool:
            self._pool.closeall()
        self._pool = None

    def __enter__(self) -> PostgresBillingStore:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def _execute(self, sql: str, params: list[Any] | None = None) -> list[Any]:
        """Execute raw SQL and return all result rows as dicts via the connection pool."""
        conn = self._pool.getconn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT set_config('bursar.tenant_id', %s, true)",
                    (self._tenant_id,),
                )
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
            subscription_id=str(r.id) if r.id else None,
            user_id=str(r.user_id),
            provider=str(r.provider),
            provider_subscription_id=str(r.provider_subscription_id),
            provider_customer_id=str(r.provider_customer_id) if r.provider_customer_id else None,
            offer_id=str(r.offer_id) if r.offer_id else None,
            offer_key=str(r.offer_key) if r.offer_key else None,
            plan=str(r.plan) if r.plan else None,
            status=BillingSubscriptionStatus(str(r.status)) if r.status else BillingSubscriptionStatus.incomplete,
            current_period_start=_to_utc_iso(r.current_period_start),
            current_period_end=_to_utc_iso(r.current_period_end),
            trial_end=_to_utc_iso(r.trial_end),
            cancel_at=_to_utc_iso(r.cancel_at),
            ended_at=_to_utc_iso(r.ended_at),
            cancel_at_period_end=bool(r.cancel_at_period_end),
            interval=str(r.interval) if r.interval else None,
            interval_count=int(r.interval_count) if r.interval_count is not None else None,
            grace_ends_at=_to_utc_iso(getattr(r, "grace_ends_at", None)),
            grace_expired_at=_to_utc_iso(getattr(r, "grace_expired_at", None)),
            provider_updated_at=_to_utc_iso(getattr(r, "provider_updated_at", None)),
            metadata=r.metadata if isinstance(r.metadata, dict) else None,
        )

    def _subscription_offer_contexts(
        self,
        row: dict[str, Any],
    ) -> tuple[BillingSubscriptionOfferContext, BillingSubscriptionOfferContext]:
        rows = self._execute(
            """SELECT requested.side, requested.offer_id, context.*
               FROM (
                   VALUES
                       ('from', %s::uuid, %s::uuid),
                       ('to', %s::uuid, %s::uuid)
               ) AS requested(side, offer_id, catalog_revision_id)
               CROSS JOIN LATERAL bursar.get_catalog_offer_context(
                   requested.offer_id,
                   requested.catalog_revision_id
               ) AS context""",
            [
                row["from_offer_id"],
                row["from_catalog_revision_id"],
                row["to_offer_id"],
                row["to_catalog_revision_id"],
            ],
        )
        by_side = {str(context["side"]): context for context in rows}

        def map_context(side: str) -> BillingSubscriptionOfferContext:
            context = by_side.get(side)
            if context is None or context.get("offer_key") is None:
                raise RuntimeError(f"subscription change {side}-offer context not found")
            return BillingSubscriptionOfferContext(
                offer_id=str(context["offer_id"]),
                offer_key=str(context["offer_key"]),
                plan_id=str(context["plan_id"]) if context.get("plan_id") else None,
                plan=str(context["plan_key"]) if context.get("plan_key") else None,
                interval=str(context["billing_unit"]) if context.get("billing_unit") else None,
                interval_count=(int(context["billing_count"]) if context.get("billing_count") is not None else None),
            )

        return map_context("from"), map_context("to")

    def _row_to_subscription_change(self, row: dict[str, Any] | None) -> BillingSubscriptionChange | None:
        if not row or row.get("id") is None:
            return None
        from_offer, to_offer = self._subscription_offer_contexts(row)
        return BillingSubscriptionChange(
            id=str(row["id"]),
            subscription_id=str(row["subscription_id"]),
            from_offer_id=str(row["from_offer_id"]),
            to_offer_id=str(row["to_offer_id"]),
            from_offer=from_offer,
            to_offer=to_offer,
            effective_at=_to_utc_iso(row.get("effective_at")),
            effective=cast(Literal["immediate", "renewal"], str(row["effective_behavior"])),
            state=cast(BillingSubscriptionChangeState, str(row["state"])),
            proration_behavior=cast(
                BillingSubscriptionProrationBehavior,
                str(row["proration_behavior"]),
            ),
            idempotency_key=str(row["idempotency_key"]),
            provider_operation_id=(str(row["provider_operation_id"]) if row.get("provider_operation_id") else None),
            error_message=str(row["error_message"]) if row.get("error_message") else None,
        )

    @staticmethod
    def _row_to_offer(result: Any) -> BillingOfferResult | None:
        if result is None:
            return None
        return BillingOfferResult(
            offer_id=str(result.id),
            offer_key=str(result.offer_key),
            plan_id=str(result.plan_id) if result.plan_id else None,
            plan=result.plan,
            interval=result.interval,
            interval_count=result.interval_count,
            grant=BillingGrantResult(
                mode="cycle_grant" if result.grant_mode == "credits" else result.grant_mode,
                credits=result.grant_credits,
                bucket=result.grant_bucket,
                replace_prior=result.grant_replace_prior,
            ),
        )

    @staticmethod
    def _row_to_topup(result: Any) -> BillingTopupResult | None:
        if result is None:
            return None
        amount_minor = int(result.amount_minor) if result.amount_minor is not None else None
        min_quantity = int(result.min_quantity) if result.min_quantity is not None else None
        max_quantity = int(result.max_quantity) if result.max_quantity is not None else None
        credits_per_unit = _dec_credits(
            result.credits_per_unit if result.credits_per_unit is not None else result.credits_per_major_unit
        )
        return BillingTopupResult(
            topup_id=str(result.id),
            topup_key=str(result.topup_key),
            credits_per_unit=credits_per_unit if credits_per_unit is not None else Decimal(1000),
            deposit_to=result.bucket_key or "purchased",
            amount_minor=amount_minor,
            currency=result.currency,
            min_quantity=min_quantity,
            max_quantity=max_quantity,
            default_quantity=int(result.default_quantity) if result.default_quantity is not None else None,
            min_amount_minor=amount_minor * (min_quantity or 1) if amount_minor is not None else None,
            max_amount_minor=amount_minor * (max_quantity or 1) if amount_minor is not None else None,
        )

    # ── Public API ─────────────────────────────────────────────────────

    def get_active_bursar_config(self) -> dict[str, Any] | None:
        rows = self._execute("SELECT * FROM bursar.active_catalog_revision()")
        if not rows:
            return None
        row = rows[0]
        value = row.get("source_document") if isinstance(row, dict) else row[0]
        return value if isinstance(value, dict) else None

    def create_or_get_checkout_intent(
        self,
        input: CheckoutIntentCreate,
    ) -> CheckoutIntent:
        """Create or retrieve an open checkout intent for an actor key.

        Atomically expires any old open intents for the same actor,
        then inserts a new one (or returns the existing one via ON CONFLICT).
        """
        if re.fullmatch(r"[0-9a-fA-F]{64}", input.request_digest) is None:
            raise ValueError("request_digest must be a 32-byte hex string")
        rows = self._execute(
            "SELECT bursar.create_checkout_intent(%s::uuid, %s, %s, %s, decode(%s, 'hex'), %s::timestamptz) AS id",
            [
                input.subject_id,
                input.provider,
                input.checkout_kind,
                input.product_key,
                input.request_digest,
                input.expires_at,
            ],
        )
        intent_id = rows[0].get("id") if rows else None
        if intent_id is None:
            raise RuntimeError("checkout intent creation returned no ID")
        intent = self.get_checkout_intent(str(intent_id), input.subject_id)
        if intent is None:
            raise RuntimeError("checkout intent could not be read after creation")
        return intent

    def update_checkout_intent(
        self,
        id: str,
        update: CheckoutIntentUpdate,
    ) -> None:
        """Update a checkout intent's status and optional session/URL fields."""
        rows = self._execute(
            """SELECT bursar.advance_checkout_intent(
                   %s::uuid,
                   %s,
                   %s,
                   %s
               ) AS advanced""",
            [
                id,
                update.status,
                update.provider_session_id,
                update.checkout_url,
            ],
        )
        if not rows or not rows[0].get("advanced"):
            raise RuntimeError(f"checkout intent update rejected: {id}")

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
        envelope: dict[str, Any] | None = None,
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
            json.dumps(envelope or {"eventType": event_type}),
        )
        if result is None:
            return BillingEventClaim(status="retry")
        raw_status = (result.status or "retry").lower()
        claim_token = getattr(result, "claim_token", None)
        billing_event_id = getattr(result, "event_id", None)
        if raw_status == "claimed" and isinstance(claim_token, str) and billing_event_id is not None:
            return BillingEventClaim(
                status="claimed",
                claim_token=claim_token,
                billing_event_id=str(billing_event_id),
            )
        if raw_status == "duplicate":
            return BillingEventClaim(status="duplicate")
        return BillingEventClaim(status="retry")

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
                "offer_id": state.offer_id,
                "offer_key": state.offer_key,
                "plan": state.plan,
                "status": state.status,
                "current_period_start": _to_utc_iso(state.current_period_start),
                "current_period_end": _to_utc_iso(state.current_period_end),
                "trial_end": _to_utc_iso(state.trial_end),
                "cancel_at": _to_utc_iso(state.cancel_at),
                "ended_at": _to_utc_iso(state.ended_at),
                "provider_updated_at": _to_utc_iso(state.provider_updated_at),
                "cancel_at_period_end": state.cancel_at_period_end,
                "interval": state.interval,
                "interval_count": state.interval_count,
                "grace_ends_at": _to_utc_iso(state.grace_ends_at),
                "metadata": state.metadata,
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
        result = self._subscription_repo.get_user_subscription(user_id, statuses=statuses)
        return self._row_to_subscription_state(result)

    def create_billing_subscription_change(
        self,
        input: BillingSubscriptionChangeInput,
    ) -> BillingSubscriptionChange:
        subscription = self._subscription_repo.get(
            input.provider,
            input.provider_subscription_id,
        )
        if subscription is None or subscription.id is None:
            raise RuntimeError("subscription change requires persisted subscription")

        rows = self._execute(
            """SELECT * FROM bursar.open_subscription_change(
                   %s::uuid,
                   %s::uuid,
                   %s::timestamptz,
                   %s,
                   %s,
                   %s
               )""",
            [
                subscription.id,
                input.to_offer_id,
                input.effective_at,
                input.effective,
                input.idempotency_key,
                input.proration_behavior,
            ],
        )
        result = rows[0] if rows else {}
        if result.get("error_code"):
            raise RuntimeError(f"subscription change: {result['error_code']}")
        change_rows = self._execute(
            "SELECT * FROM bursar.get_billing_subscription_change(%s::bigint)",
            [result.get("change_id")],
        )
        parsed_change = self._row_to_subscription_change(change_rows[0] if change_rows else None)
        if parsed_change is None:
            raise RuntimeError("subscription change creation returned no row")
        return parsed_change

    def get_open_billing_subscription_change(
        self, provider: str, provider_subscription_id: str
    ) -> BillingSubscriptionChange | None:
        rows = self._execute(
            "SELECT * FROM bursar.get_open_billing_subscription_change(%s, %s)",
            [provider, provider_subscription_id],
        )
        return self._row_to_subscription_change(rows[0]) if rows else None

    def update_billing_subscription_change(
        self,
        id: str,
        update: BillingSubscriptionChangeUpdate,
    ) -> None:
        if update.state is None:
            return
        rows = self._execute(
            """SELECT bursar.advance_subscription_change(
                   %s::bigint,
                   %s,
                   %s,
                   %s
               ) AS advanced""",
            [
                id,
                update.state,
                update.provider_operation_id,
                update.error_message,
            ],
        )
        if not rows or not rows[0].get("advanced"):
            raise RuntimeError(f"subscription change transition rejected: {id}")

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
        input: BillingPaymentUpsert,
    ) -> str:
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
            status: The payment status (default "succeeded").
            provider_updated_at: Optional provider timestamp.
            metadata: Optional structured metadata dict.
        """
        return self._payment_repo.upsert(
            input.provider,
            input.provider_payment_id,
            input.provider_invoice_id,
            input.user_id,
            input.amount_minor,
            input.tax_minor,
            input.currency or "USD",
            input.purpose,
            json.dumps(input.metadata) if input.metadata else None,
            input.status,
            input.provider_updated_at,
        )

    def upsert_billing_refund(
        self,
        input: BillingRefundUpsert,
    ) -> str:
        """Insert or update a billing refund record.

        Args:
            provider: The billing provider identifier.
            provider_refund_id: The provider refund ID.
            provider_payment_id: The associated payment ID, or None.
            user_id: The user ID, or None.
            amount_minor: The refund amount in minor currency units.
            currency: The ISO 4217 currency code (default "USD").
            reason: The refund reason, or None.
            status: The refund status (default "pending").
            provider_updated_at: Optional provider timestamp.
            metadata: Optional structured metadata dict.
        """
        return self._refund_repo.upsert(
            input.provider,
            input.provider_refund_id,
            input.provider_payment_id,
            input.user_id,
            input.amount_minor,
            input.currency or "USD",
            input.reason,
            json.dumps(input.metadata) if input.metadata else None,
            input.status,
            input.provider_updated_at,
        )

    def upsert_billing_invoice(
        self,
        input: BillingInvoiceUpsert,
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
        subscription = (
            self._subscription_repo.get(
                input.provider,
                input.provider_subscription_id,
            )
            if input.provider_subscription_id
            else None
        )
        subject_id = input.user_id or (str(subscription.user_id) if subscription and subscription.user_id else None)
        if not subject_id:
            raise ValueError("invoice subject is required")
        self._invoice_repo.upsert(
            subject_id,
            input.provider,
            input.provider_invoice_id,
            str(subscription.id) if subscription and subscription.id else None,
            input.status,
            input.amount_due_minor,
            input.amount_paid_minor,
            input.currency or "USD",
            input.period_start,
            input.period_end,
            json.dumps(input.metadata or {}),
            input.provider_updated_at or datetime.now(UTC).isoformat(),
        )

    def list_billing_invoices(self, user_id: str) -> list[BillingInvoiceInfo]:
        return self._invoice_repo.list_for_user(user_id)

    def upsert_billing_dispute(
        self,
        input: BillingDisputeUpsert,
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
        payment = (
            self._payment_repo.get_for_refund(
                input.provider,
                input.provider_payment_id,
            )
            if input.provider_payment_id
            else None
        )
        if not payment or not payment.id:
            raise ValueError("dispute payment is required")
        self._dispute_repo.upsert(
            input.provider,
            input.provider_dispute_id,
            str(payment.id),
            input.status,
            input.reason,
            json.dumps(input.metadata or {}),
            input.provider_updated_at or datetime.now(UTC).isoformat(),
        )

    def create_billing_credit_grant(
        self,
        input: BillingCreditGrantCreate,
    ) -> str:
        rows = self._execute(
            "SELECT bursar.create_billing_credit_grant(%s::uuid, %s::uuid, %s::uuid, %s, %s, %s::uuid)",
            [
                input.payment_id,
                input.subscription_id,
                input.topup_id,
                str(input.configured_credits),
                input.quantity,
                input.billing_event_id,
            ],
        )
        return str(next(iter(rows[0].values())))

    def grant_billing_credit(self, grant_id: str, idempotency_key: str) -> dict:
        rows = self._execute("SELECT * FROM bursar.grant_billing_credit(%s::uuid, %s)", [grant_id, idempotency_key])
        return rows[0] if rows and isinstance(rows[0], dict) else {}

    def get_billing_credit_grant_by_payment(self, payment_id: str) -> str | None:
        rows = self._execute(
            "SELECT * FROM bursar.get_billing_credit_grant_by_payment(%s::uuid)",
            [payment_id],
        )
        grant_id = rows[0].get("id") if rows else None
        return str(grant_id) if grant_id else None

    def post_billing_refund(self, refund_id: str, grant_id: str, amount_minor: int, idempotency_key: str) -> dict:
        rows = self._execute(
            "SELECT * FROM bursar.post_billing_refund(%s::uuid, %s::uuid, %s, %s)",
            [refund_id, grant_id, amount_minor, idempotency_key],
        )
        return rows[0] if rows and isinstance(rows[0], dict) else {}

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
            user_id=str(row.get("subject_id", "")),
            auto_recharge=bool(row.get("auto_recharge", False)),
            overage_protection=bool(row.get("overage_protection", True)),
            email_notifications=bool(row.get("email_notifications", True)),
            usage_alerts=bool(row.get("usage_alerts", True)),
            invoice_reminders=bool(row.get("invoice_reminders", False)),
        )

    def upsert_billing_preferences(self, prefs: BillingPreferences) -> None:
        """Insert or update billing preferences for a user.

        Args:
            prefs: The billing preferences to persist.
        """
        self._preferences_repo.upsert(prefs.model_dump())

    def get_auto_recharge_profile(self, user_id: str) -> BillingAutoRechargeProfile | None:
        rows = self._execute(
            "SELECT * FROM bursar.get_auto_recharge_profile(%s::uuid)",
            [user_id],
        )
        if not rows:
            return None
        row = rows[0]
        return BillingAutoRechargeProfile(
            user_id=str(row["subject_id"]),
            enabled=bool(row["enabled"]),
            state=cast(Literal["disabled", "active", "paused"], str(row["state"])),
            armed=bool(row.get("armed", True)),
            provider=str(row["provider"]) if row.get("provider") is not None else None,
            topup_id=str(row["topup_id"]) if row.get("topup_id") is not None else None,
            quantity=int(row["quantity"]),
            threshold=Decimal(str(row["threshold"])),
            max_charges_per_window=(
                int(row["max_charges_per_window"]) if row.get("max_charges_per_window") is not None else None
            ),
            window_unit=cast(
                Literal["second", "minute", "hour", "day", "week", "month", "year"],
                str(row["window_unit"]),
            ),
            window_count=int(row["window_count"]),
            window_anchor=cast(
                Literal["calendar", "plan_assignment", "rolling"],
                str(row["window_anchor"]),
            ),
            window_timezone=str(row["window_timezone"]),
            updated_at=_to_utc_iso(row.get("updated_at")),
        )

    def upsert_auto_recharge_profile(self, profile: BillingAutoRechargeProfile) -> None:
        rows = self._execute(
            """SELECT bursar.upsert_auto_recharge_profile(
                   %s::uuid,
                   %s,
                   %s,
                   %s::uuid,
                   %s,
                   %s,
                   %s,
                   %s,
                   %s,
                   %s,
                   %s
               ) AS profile_updated""",
            [
                profile.user_id,
                profile.enabled,
                profile.provider,
                profile.topup_id,
                profile.quantity,
                profile.threshold,
                profile.max_charges_per_window,
                profile.window_unit,
                profile.window_count,
                profile.window_anchor,
                profile.window_timezone,
            ],
        )
        if not rows or not rows[0].get("profile_updated"):
            raise RuntimeError(f"auto-recharge profile update rejected: {profile.user_id}")

    def claim_auto_recharge_attempt(
        self,
        input: AutoRechargeAttemptClaim,
    ) -> BillingAutoRechargeAttempt | None:
        rows = self._execute(
            "SELECT * FROM bursar.claim_auto_recharge_attempt(%s::uuid, %s)",
            [input.user_id, input.idempotency_key],
        )
        if not rows:
            return None
        return self._row_to_auto_recharge_attempt(rows[0])

    @staticmethod
    def _row_to_auto_recharge_attempt(row: dict[str, Any]) -> BillingAutoRechargeAttempt:
        return BillingAutoRechargeAttempt(
            id=str(row["id"]),
            user_id=str(row["subject_id"]),
            provider=str(row["provider"]),
            idempotency_key=str(row["idempotency_key"]),
            provider_attempt_id=(
                str(row["provider_attempt_id"]) if row.get("provider_attempt_id") is not None else None
            ),
            topup_id=str(row["topup_id"]),
            quantity=int(row["quantity"]),
            state=cast(
                Literal[
                    "claimed",
                    "submitted",
                    "processing",
                    "unknown",
                    "succeeded",
                    "failed",
                    "action_required",
                ],
                str(row["state"]),
            ),
            window_start=cast(str, _to_utc_iso(row["window_start"])),
            window_end=cast(str, _to_utc_iso(row["window_end"])),
            quoted_amount_minor=(
                int(row["quoted_amount_minor"]) if row.get("quoted_amount_minor") is not None else None
            ),
            currency=str(row["currency"]) if row.get("currency") is not None else None,
            failure_code=(str(row["failure_code"]) if row.get("failure_code") is not None else None),
            failure_message=(str(row["failure_message"]) if row.get("failure_message") is not None else None),
            metadata=row["metadata"] if isinstance(row.get("metadata"), dict) else {},
            created_at=cast(str, _to_utc_iso(row["created_at"])),
            updated_at=cast(str, _to_utc_iso(row["updated_at"])),
        )

    def _advance_auto_recharge_attempt(
        self,
        attempt_id: str,
        state: str,
        provider_attempt_id: str | None = None,
        failure_code: str | None = None,
        failure_message: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        current_rows = self._execute(
            "SELECT * FROM bursar.get_auto_recharge_attempt(%s::uuid)",
            [attempt_id],
        )
        current = str(current_rows[0].get("state", "")) if current_rows else ""
        transitions: dict[str, dict[str, list[str]]] = {
            "claimed": {
                "submitted": ["submitted"],
                "processing": ["submitted", "processing"],
                "succeeded": ["submitted", "processing", "succeeded"],
                "failed": ["submitted", "processing", "failed"],
                "unknown": ["submitted", "processing", "unknown"],
                "action_required": ["submitted", "action_required"],
            },
            "submitted": {
                "submitted": [],
                "processing": ["processing"],
                "succeeded": ["processing", "succeeded"],
                "failed": ["processing", "failed"],
                "unknown": ["processing", "unknown"],
                "action_required": ["action_required"],
            },
            "processing": {
                "processing": [],
                "succeeded": ["succeeded"],
                "failed": ["failed"],
                "unknown": ["unknown"],
            },
            "unknown": {
                "unknown": [],
                "processing": ["processing"],
                "action_required": ["action_required"],
            },
            "action_required": {
                "action_required": [],
                "processing": ["processing"],
            },
        }
        path = transitions.get(current, {}).get(state)
        if path is None:
            raise RuntimeError(f"auto-recharge attempt transition rejected: {attempt_id}")

        for next_state in path:
            rows = self._execute(
                """SELECT bursar.advance_auto_recharge_attempt(
                       %s::uuid,
                       %s::bursar.recharge_attempt_status,
                       %s,
                       %s,
                       %s,
                       %s::jsonb
                   ) AS advanced""",
                [
                    attempt_id,
                    next_state,
                    provider_attempt_id,
                    failure_code,
                    failure_message,
                    json.dumps(metadata or {}),
                ],
            )
            if not rows or not rows[0].get("advanced"):
                raise RuntimeError(f"auto-recharge attempt transition rejected: {attempt_id}")

    def update_auto_recharge_attempt(
        self,
        input: AutoRechargeAttemptUpdate,
    ) -> None:
        self._advance_auto_recharge_attempt(
            input.id,
            input.state,
            input.provider_attempt_id,
            input.failure_code,
            input.failure_message,
            input.metadata,
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

    def get_checkout_intent(self, id: str, subject_id: str) -> CheckoutIntent | None:
        rows = self._execute(
            "SELECT * FROM bursar.get_checkout_intent(%s::uuid, %s::uuid)",
            [id, subject_id],
        )
        if not rows:
            return None
        row = rows[0]
        return CheckoutIntent(
            id=str(row["id"]),
            subject_id=str(row["subject_id"]),
            provider=str(row["provider"]),
            checkout_kind=row["checkout_kind"],
            product_key=str(row["product_key"]),
            request_digest=bytes(row["request_digest"]).hex(),
            status=CheckoutIntentStatus(str(row["status"])),
            provider_session_id=(str(row["provider_session_id"]) if row.get("provider_session_id") else None),
            checkout_url=(str(row["checkout_url"]) if row.get("checkout_url") else None),
            expires_at=cast(str, _to_utc_iso(row["expires_at"])),
        )

    def list_expired_grace_subscriptions(self, now: datetime, limit: int = 100) -> list[dict]:
        rows = self._execute(
            "SELECT * FROM bursar.list_expired_grace_subscriptions(%s, %s)",
            [now.isoformat(), limit],
        )
        return [dict(r) for r in rows if isinstance(r, dict)]

    def mark_subscription_grace_expired(self, id: str, expected_grace_ends_at: str, expired_at: str) -> bool:
        rows = self._execute(
            "SELECT bursar.mark_subscription_grace_expired(%s::uuid, %s, %s) AS marked",
            [id, expected_grace_ends_at, expired_at],
        )
        return bool(rows[0]["marked"]) if rows else False

    def select_subscription_entitlement_source(
        self, user_id: str, provider: str, subscription_id: str | None = None
    ) -> bool:
        rows = self._execute(
            "SELECT * FROM bursar.list_billing_subscriptions(%s::uuid)",
            [user_id],
        )
        eligible_statuses = {"trialing", "active", "past_due", "paused"}
        candidates = [
            r
            for r in rows
            if str(r.get("provider", "")) == provider
            and str(r.get("status", "")) in eligible_statuses
            and (subscription_id is None or str(r.get("provider_subscription_id", "")) == subscription_id)
        ]
        if not candidates:
            return False
        replacement = max(candidates, key=provider_timestamp_sort_key)
        selected = self._execute(
            "SELECT bursar.select_entitlement_source(%s::uuid, %s::uuid) AS selected",
            [user_id, replacement["id"]],
        )
        if not selected or not selected[0].get("selected"):
            raise RuntimeError("entitlement source selection was rejected")
        return True

    def record_subscription_conflict(
        self,
        input: BillingSubscriptionConflictCreate,
    ) -> None:
        rows = self._execute(
            "SELECT bursar.record_subscription_conflict(%s::uuid, %s, %s, %s, %s, %s::jsonb) AS id",
            [
                input.user_id,
                input.provider,
                input.duplicate_subscription_id,
                input.existing_subscription_id,
                input.event_id,
                json.dumps(input.metadata or {}),
            ],
        )
        conflict_id = rows[0].get("id") if rows and isinstance(rows[0], dict) else None
        if conflict_id is None:
            raise RuntimeError("subscription conflict audit returned no ID")

    def compute_topup_credits(self, amount_minor: int, topup_config: BillingTopupResult) -> Decimal:
        unit_amount = topup_config.amount_minor
        if not unit_amount:
            return Decimal(0)
        credits_per = Decimal(str(topup_config.credits_per_unit or 0))
        if amount_minor % unit_amount != 0:
            return Decimal(0)
        quantity = amount_minor // unit_amount
        min_qty = topup_config.min_quantity or 1
        max_qty = topup_config.max_quantity or 1
        if quantity < min_qty or quantity > max_qty:
            return Decimal(0)
        return Decimal(str(quantity)) * credits_per

    def pseudonymize_financial_subject(self, user_id: str) -> bool:
        rows = self._execute(
            "SELECT bursar.pseudonymize_financial_subject(%s::uuid) AS pseudonymized",
            [user_id],
        )
        return bool(rows[0].get("pseudonymized")) if rows else False

    def update_auto_recharge_attempt_by_provider_payment(
        self,
        input: AutoRechargeProviderPaymentUpdate,
    ) -> None:
        attempts = self._execute(
            "SELECT * FROM bursar.get_auto_recharge_attempt_by_provider(%s, %s)",
            [input.provider, input.provider_payment_id],
        )
        for attempt in attempts:
            attempt_id = attempt.get("id")
            if not attempt_id:
                continue
            self._advance_auto_recharge_attempt(
                str(attempt_id),
                input.state,
                input.provider_payment_id,
                input.failure_code,
                input.failure_message,
            )

    def count_auto_recharge_attempts(
        self,
        user_id: str,
        since: str | datetime,
    ) -> int:
        since_date = since if isinstance(since, datetime) else datetime.fromisoformat(since)
        if since_date.tzinfo is None:
            raise ValueError("auto-recharge attempt window must include timezone")
        rows = self._execute(
            "SELECT bursar.count_auto_recharge_attempts(%s::uuid, %s::timestamptz) AS count",
            [user_id, since_date.astimezone(UTC).isoformat()],
        )
        return int(rows[0]["count"]) if rows else 0
