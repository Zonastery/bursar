"""PostgreSQL-backed billing store adapter.

Connects directly via psycopg2 to a Postgres database with the billing
schema installed. Wraps all billing repositories under a single store class.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from functools import cached_property
from typing import Any, Literal, cast
from uuid import UUID

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
from bursar.billing.postgres.repositories.auto_recharge import BillingAutoRechargeRepository
from bursar.billing.postgres.repositories.customer import BillingCustomerRepository
from bursar.billing.postgres.repositories.dispute import BillingDisputeRepository
from bursar.billing.postgres.repositories.event import BillingEventRepository
from bursar.billing.postgres.repositories.invoice import BillingInvoiceRepository
from bursar.billing.postgres.repositories.offer import BillingOfferRepository
from bursar.billing.postgres.repositories.payment import BillingPaymentRepository
from bursar.billing.postgres.repositories.preferences import BillingPreferencesRepository
from bursar.billing.postgres.repositories.refund import BillingRefundRepository
from bursar.billing.postgres.repositories.schemas import (
    BillingOfferRow,
    BillingTopupRow,
    SubscriptionRow,
)
from bursar.billing.postgres.repositories.subscription import (
    BillingSubscriptionRepository,
)
from bursar.billing.postgres.repositories.subscription_change import BillingSubscriptionChangeRepository
from bursar.billing.postgres.repositories.topup import BillingTopupRepository
from bursar.billing.types import (
    BillingAutoRechargeAttempt,
    BillingAutoRechargeProfile,
    BillingCreditPostingResult,
    BillingCustomerRecord,
    BillingEventClaim,
    BillingGrantResult,
    BillingInvoiceRecord,
    BillingOfferResult,
    BillingPaymentRecord,
    BillingPreferences,
    BillingSubscriptionChange,
    BillingSubscriptionChangeInput,
    BillingSubscriptionState,
    BillingSubscriptionStatus,
    BillingTopupResult,
    CheckoutIntent,
    CheckoutIntentStatus,
)
from bursar.credits.postgres.repositories._utils import (
    optional_mapping_row,
    require_boolean_result,
    require_identifier_result,
    require_mapping_row,
)
from bursar.errors import BursarError, StoreError
from bursar.providers.types import ProviderEnvironment
from bursar.shared.diagnostics import optional_bounded_diagnostic_message
from bursar.shared.postgres_client import PostgresClient, PostgresConnectionOptions, PostgresPool


def _to_utc_iso(value: str | datetime | None) -> str | None:
    if value is None:
        return None
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("billing timestamp must include timezone")
    return parsed.astimezone(UTC).isoformat()


def _required_utc_iso(value: str | datetime, context: str) -> str:
    result = _to_utc_iso(value)
    if result is None:  # pragma: no cover - excluded by the non-null input type
        raise StoreError(f"{context}: timestamp is required")
    return result


def _billing_credit_posting_result(
    rows: list[Any] | None,
    context: str,
) -> BillingCreditPostingResult:
    row = require_mapping_row(rows, context)
    try:
        return BillingCreditPostingResult.model_validate(row)
    except ValueError as exc:
        raise StoreError(
            f"{context}: result validation failed",
            cause=exc,
            indeterminate=True,
            details={"context": context},
        ) from exc


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
        database_url: str | None = None,
        *,
        tenant_id: str | UUID,
        provider_environment: ProviderEnvironment,
        pool: PostgresPool | None = None,
        billing_payload_backend: Literal["postgres", "s3"] = "postgres",
        connection_timeout_seconds: float = 10.0,
        statement_timeout_ms: int = 30_000,
        idle_transaction_timeout_ms: int = 30_000,
        application_name: str = "bursar-python",
        on_pool_error: Callable[[BursarError], None] | None = None,
        postgres_options: PostgresConnectionOptions | None = None,
    ) -> None:
        if pool is None and (not isinstance(database_url, str) or not database_url.strip()):
            raise ValueError("database_url is required when pool is not provided")
        if pool is not None and database_url is not None:
            raise ValueError("provide either database_url or pool, not both")
        if pool is None:
            assert database_url is not None
        self._database_url = database_url
        self._tenant_id = str(UUID(str(tenant_id)))
        self._provider_environment: ProviderEnvironment = provider_environment
        self._billing_payload_backend = billing_payload_backend
        self._client = (
            PostgresClient.from_pool(
                pool,
                tenant_id=self._tenant_id,
                billing_payload_backend=billing_payload_backend,
                provider_environment=provider_environment,
                connection_timeout_seconds=connection_timeout_seconds,
                statement_timeout_ms=statement_timeout_ms,
                idle_transaction_timeout_ms=idle_transaction_timeout_ms,
                application_name=application_name,
                on_pool_error=on_pool_error,
                postgres_options=postgres_options,
            )
            if pool is not None
            else PostgresClient(
                cast(str, database_url),
                tenant_id=self._tenant_id,
                billing_payload_backend=billing_payload_backend,
                provider_environment=provider_environment,
                connection_timeout_seconds=connection_timeout_seconds,
                statement_timeout_ms=statement_timeout_ms,
                idle_transaction_timeout_ms=idle_transaction_timeout_ms,
                application_name=application_name,
                on_pool_error=on_pool_error,
                postgres_options=postgres_options,
            )
        )

    @property
    def provider_environment(self) -> ProviderEnvironment:
        """Provider namespace used by every billing transaction."""
        return self._provider_environment

    def close(self) -> None:
        """Close all connections in the pool."""
        self._client.close()

    def __enter__(self) -> PostgresBillingStore:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def _execute(self, sql: str, params: list[Any] | None = None) -> list[Any]:
        """Execute raw SQL and return all result rows as dicts via the connection pool."""
        return self._client.query(sql, params)

    @cached_property
    def _offer_repo(self) -> BillingOfferRepository:
        return BillingOfferRepository(self._execute)

    @cached_property
    def _topup_repo(self) -> BillingTopupRepository:
        return BillingTopupRepository(self._execute)

    @cached_property
    def _customer_repo(self) -> BillingCustomerRepository:
        return BillingCustomerRepository(self._execute)

    @cached_property
    def _subscription_repo(self) -> BillingSubscriptionRepository:
        return BillingSubscriptionRepository(self._execute)

    @cached_property
    def _event_repo(self) -> BillingEventRepository:
        return BillingEventRepository(self._execute)

    @cached_property
    def _payment_repo(self) -> BillingPaymentRepository:
        return BillingPaymentRepository(self._execute)

    @cached_property
    def _refund_repo(self) -> BillingRefundRepository:
        return BillingRefundRepository(self._execute)

    @cached_property
    def _invoice_repo(self) -> BillingInvoiceRepository:
        return BillingInvoiceRepository(self._execute)

    @cached_property
    def _dispute_repo(self) -> BillingDisputeRepository:
        return BillingDisputeRepository(self._execute)

    @cached_property
    def _preferences_repo(self) -> BillingPreferencesRepository:
        return BillingPreferencesRepository(self._execute)

    @cached_property
    def _auto_recharge_repo(self) -> BillingAutoRechargeRepository:
        return BillingAutoRechargeRepository(self._execute)

    @cached_property
    def _subscription_change_repo(self) -> BillingSubscriptionChangeRepository:
        return BillingSubscriptionChangeRepository(self._execute)

    # ── Helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _row_to_subscription_state(r: SubscriptionRow | None) -> BillingSubscriptionState | None:
        if r is None:
            return None
        return BillingSubscriptionState(
            subscription_id=str(r.id),
            user_id=str(r.user_id),
            provider=r.provider,
            provider_subscription_id=r.provider_subscription_id,
            provider_customer_id=str(r.provider_customer_id) if r.provider_customer_id else None,
            offer_id=str(r.offer_id),
            offer_key=r.offer_key,
            plan_id=str(r.plan_id),
            plan=r.plan,
            status=BillingSubscriptionStatus(r.status),
            current_period_start=_to_utc_iso(r.current_period_start),
            current_period_end=_to_utc_iso(r.current_period_end),
            trial_end=_to_utc_iso(r.trial_end),
            cancel_at=_to_utc_iso(r.cancel_at),
            ended_at=_to_utc_iso(r.ended_at),
            cancel_at_period_end=r.cancel_at_period_end,
            interval=r.interval,
            interval_count=r.interval_count,
            grace_ends_at=_to_utc_iso(r.grace_ends_at),
            grace_expired_at=_to_utc_iso(r.grace_expired_at),
            provider_updated_at=_required_utc_iso(r.provider_updated_at, "billing subscription"),
            metadata=r.metadata,
        )

    @staticmethod
    def _row_to_offer(result: BillingOfferRow | None) -> BillingOfferResult | None:
        if result is None:
            return None
        grant = None
        if result.grant_mode is not None:
            if result.grant_credits is None or result.grant_bucket is None:
                raise StoreError(
                    "resolved billing offer has an incomplete cycle grant",
                    details={"offer_id": result.id, "offer_key": result.offer_key},
                )
            grant = BillingGrantResult(
                mode="cycle_grant",
                credits=result.grant_credits,
                bucket=result.grant_bucket,
                replace_prior=result.grant_replace_prior,
            )
        return BillingOfferResult(
            offer_id=str(result.id),
            offer_key=str(result.offer_key),
            plan_id=str(result.plan_id),
            plan=result.plan,
            interval=result.interval,
            interval_count=result.interval_count,
            grant=grant,
        )

    @staticmethod
    def _row_to_topup(result: BillingTopupRow | None) -> BillingTopupResult | None:
        if result is None:
            return None
        return BillingTopupResult(
            topup_id=str(result.id),
            topup_key=str(result.topup_key),
            credits_per_unit=result.credits_per_unit,
            deposit_to=result.bucket_key,
            amount_minor=result.amount_minor,
            currency=result.currency,
            min_quantity=result.min_quantity,
            max_quantity=result.max_quantity,
            default_quantity=result.default_quantity,
            min_amount_minor=result.amount_minor * result.min_quantity,
            max_amount_minor=result.amount_minor * result.max_quantity,
        )

    # ── Public API ─────────────────────────────────────────────────────

    def get_active_catalog_document(self) -> dict[str, Any] | None:
        rows = self._execute("SELECT * FROM bursar.active_catalog_revision()")
        if not rows:
            return None
        if len(rows) != 1:
            raise StoreError(
                "active catalog revision returned multiple rows",
                details={"row_count": len(rows)},
            )
        row = rows[0]
        if not isinstance(row, dict) or not isinstance(row.get("source_document"), dict):
            raise StoreError("active catalog revision returned a malformed source document")
        return row["source_document"]

    def create_or_get_checkout_intent(
        self,
        input: CheckoutIntentCreate,
    ) -> CheckoutIntent:
        """Create or retrieve the checkout intent bound to an operation key."""
        if re.fullmatch(r"[0-9a-fA-F]{64}", input.request_digest) is None:
            raise ValueError("request_digest must be a 32-byte hex string")
        rows = self._execute(
            """SELECT bursar.create_checkout_intent(
                   %s::uuid, %s, %s, %s, %s, decode(%s, 'hex'), %s::timestamptz
               ) AS id""",
            [
                input.subject_id,
                input.provider,
                input.operation_key,
                input.checkout_kind,
                input.product_key,
                input.request_digest,
                input.expires_at,
            ],
        )
        intent_id = require_identifier_result(
            rows,
            "id",
            "PostgresBillingStore.create_or_get_checkout_intent",
        )
        intent = self.get_checkout_intent(intent_id, input.subject_id)
        if intent is None:
            raise StoreError(
                "checkout intent could not be read after creation",
                indeterminate=True,
                details={"checkout_intent_id": intent_id},
            )
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
        if not require_boolean_result(rows, "advanced", "PostgresBillingStore.update_checkout_intent"):
            raise StoreError(
                f"checkout intent update rejected: {id}",
                details={"checkout_intent_id": id},
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
        envelope: dict[str, Any] | None = None,
    ) -> BillingEventClaim:
        """Claim a billing event for processing (idempotent).

        Args:
            provider: The billing provider identifier.
            event_id: The provider event ID.
            event_type: The event type string.

        Returns:
            BillingEventClaim with the explicit database lifecycle status.
        """
        result = self._event_repo.claim(
            provider,
            event_id,
            event_type,
            json.dumps(envelope or {"eventType": event_type}),
        )
        raw_status = result.status
        claim_token = result.claim_token
        billing_event_id = result.event_id
        if raw_status == "claimed":
            if not claim_token or not billing_event_id:
                raise StoreError(
                    "billing event claim returned no claim identifiers",
                    details={"provider": provider, "event_id": event_id},
                )
            return BillingEventClaim(
                status="claimed",
                claim_token=str(claim_token),
                billing_event_id=str(billing_event_id),
            )
        if raw_status == "duplicate":
            return BillingEventClaim(status="duplicate")
        if raw_status == "busy":
            return BillingEventClaim(status="busy")
        if raw_status == "invalid_request":
            return BillingEventClaim(status="invalid_request")
        if raw_status in {"idempotency_conflict", "max_retries_exceeded"}:
            if billing_event_id is None:
                raise StoreError(
                    "billing event terminal claim returned no event identifier",
                    details={"provider": provider, "event_id": event_id, "status": raw_status},
                )
            return BillingEventClaim(
                status=raw_status,
                billing_event_id=str(billing_event_id),
            )
        raise StoreError(
            "billing event claim returned an unsupported status",
            details={"provider": provider, "event_id": event_id, "status": raw_status},
        )

    def complete_billing_event(self, provider: str, event_id: str, claim_token: str) -> bool:
        """Mark a billing event as completed.

        Args:
            provider: The billing provider identifier.
            event_id: The provider event ID.
        """
        return self._event_repo.complete(provider, event_id, claim_token)

    def fail_billing_event(self, provider: str, event_id: str, claim_token: str, error: str | None = None) -> bool:
        """Mark a billing event as failed.

        Args:
            provider: The billing provider identifier.
            event_id: The provider event ID.
        """
        return self._event_repo.fail(
            provider,
            event_id,
            claim_token,
            optional_bounded_diagnostic_message(error),
        )

    def upsert_billing_customer(
        self,
        provider: str,
        provider_customer_id: str,
        user_id: str,
        email: str | None = None,
    ) -> None:
        """Insert or update a billing customer record."""
        self._customer_repo.upsert(provider, provider_customer_id, user_id, email)

    def upsert_billing_subscription(self, state: BillingSubscriptionState) -> None:
        """Insert or update a billing subscription record.

        Args:
            state: The subscription state to persist.
        """
        self._subscription_repo.upsert(state)

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
            raise StoreError(
                "subscription change requires persisted subscription",
                retryable=True,
                details={
                    "provider": input.provider,
                    "provider_subscription_id": input.provider_subscription_id,
                },
            )

        return self._subscription_change_repo.create(str(subscription.id), input)

    def get_open_billing_subscription_change(
        self, provider: str, provider_subscription_id: str
    ) -> BillingSubscriptionChange | None:
        return self._subscription_change_repo.get_open(provider, provider_subscription_id)

    def update_billing_subscription_change(
        self,
        id: str,
        update: BillingSubscriptionChangeUpdate,
    ) -> None:
        self._subscription_change_repo.update(id, update)

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
            input.currency,
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
            input.currency,
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
        if input.provider_subscription_id and subscription is None:
            raise StoreError(
                "invoice subscription is not available",
                retryable=True,
                details={
                    "provider": input.provider,
                    "provider_invoice_id": input.provider_invoice_id,
                    "provider_subscription_id": input.provider_subscription_id,
                },
            )
        self._invoice_repo.upsert(
            input.user_id,
            input.provider,
            input.provider_invoice_id,
            str(subscription.id) if subscription and subscription.id else None,
            input.status,
            input.amount_due_minor,
            input.amount_paid_minor,
            input.currency,
            input.period_start,
            input.period_end,
            json.dumps(input.metadata or {}),
            input.provider_updated_at,
        )

    def list_billing_invoices(self, user_id: str) -> list[BillingInvoiceRecord]:
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
        payment = self._payment_repo.get_for_refund(
            input.provider,
            input.provider_payment_id,
        )
        if not payment or not payment.id:
            raise StoreError(
                "dispute payment is required",
                retryable=True,
                details={
                    "provider": input.provider,
                    "provider_dispute_id": input.provider_dispute_id,
                    "provider_payment_id": input.provider_payment_id,
                },
            )
        self._dispute_repo.upsert(
            input.provider,
            input.provider_dispute_id,
            str(payment.id),
            input.status,
            input.reason,
            json.dumps(input.metadata or {}),
            input.provider_updated_at,
        )

    def create_billing_credit_grant(
        self,
        input: BillingCreditGrantCreate,
    ) -> str:
        rows = self._execute(
            "SELECT bursar.create_billing_credit_grant(%s::uuid, %s::uuid, %s::uuid, %s, %s, %s::uuid) AS id",
            [
                input.payment_id,
                input.subscription_id,
                input.topup_id,
                str(input.configured_credits),
                input.quantity,
                input.billing_event_id,
            ],
        )
        return require_identifier_result(rows, "id", "PostgresBillingStore.create_billing_credit_grant")

    def grant_billing_credit(
        self,
        grant_id: str,
        idempotency_key: str,
    ) -> BillingCreditPostingResult:
        rows = self._execute("SELECT * FROM bursar.grant_billing_credit(%s::uuid, %s)", [grant_id, idempotency_key])
        return _billing_credit_posting_result(rows, "PostgresBillingStore.grant_billing_credit")

    def get_billing_credit_grant_by_payment(self, payment_id: str) -> str | None:
        rows = self._execute(
            "SELECT * FROM bursar.get_billing_credit_grant_by_payment(%s::uuid)",
            [payment_id],
        )
        row = optional_mapping_row(rows, "PostgresBillingStore.get_billing_credit_grant_by_payment")
        if row is None:
            return None
        try:
            return str(UUID(str(row.get("id"))))
        except (AttributeError, TypeError, ValueError) as error:
            raise StoreError(
                "PostgresBillingStore.get_billing_credit_grant_by_payment: malformed identifier"
            ) from error

    def post_billing_refund(
        self,
        refund_id: str,
        grant_id: str,
        amount_minor: int,
        idempotency_key: str,
    ) -> BillingCreditPostingResult:
        rows = self._execute(
            "SELECT * FROM bursar.post_billing_refund(%s::uuid, %s::uuid, %s, %s)",
            [refund_id, grant_id, amount_minor, idempotency_key],
        )
        return _billing_credit_posting_result(rows, "PostgresBillingStore.post_billing_refund")

    def get_billing_payment(
        self,
        provider: str,
        provider_payment_id: str,
    ) -> BillingPaymentRecord | None:
        """Get persisted payment state for refund processing.

        Args:
            provider: The billing provider identifier.
            provider_payment_id: The provider payment ID.

        Returns:
            Persisted payment state if found, None otherwise.
        """
        result = self._payment_repo.get_for_refund(provider, provider_payment_id)
        if result is None:
            return None
        provider_updated_at = _to_utc_iso(result.provider_updated_at)
        if provider_updated_at is None:
            raise StoreError("PostgresBillingStore.get_billing_payment: missing provider timestamp")
        return BillingPaymentRecord(
            id=str(result.id),
            provider=result.provider,
            provider_payment_id=result.provider_payment_id,
            provider_invoice_id=result.provider_invoice_id,
            user_id=str(result.subject_id),
            amount_minor=result.amount_minor,
            tax_minor=result.tax_minor,
            currency=result.currency,
            purpose=result.purpose,
            status=result.status,
            provider_updated_at=provider_updated_at,
            metadata=result.metadata,
        )

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
        return self._preferences_repo.get(user_id)

    def upsert_billing_preferences(self, prefs: BillingPreferences) -> None:
        """Insert or update billing preferences for a user.

        Args:
            prefs: The billing preferences to persist.
        """
        self._preferences_repo.upsert(prefs)

    def get_auto_recharge_profile(self, user_id: str) -> BillingAutoRechargeProfile | None:
        return self._auto_recharge_repo.get_profile(user_id)

    def upsert_auto_recharge_profile(
        self,
        profile: BillingAutoRechargeProfile,
        *,
        reset_cooldown: bool = False,
    ) -> None:
        self._auto_recharge_repo.upsert_profile(profile, reset_cooldown=reset_cooldown)

    def claim_auto_recharge_attempt(
        self,
        input: AutoRechargeAttemptClaim,
    ) -> BillingAutoRechargeAttempt | None:
        return self._auto_recharge_repo.claim_attempt(input)

    def update_auto_recharge_attempt(
        self,
        input: AutoRechargeAttemptUpdate,
    ) -> None:
        self._auto_recharge_repo.update_attempt(input)

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
        return self._customer_repo.get_by_user_id(user_id, provider)

    def get_checkout_intent(self, id: str, subject_id: str) -> CheckoutIntent | None:
        rows = self._execute(
            "SELECT * FROM bursar.get_checkout_intent(%s::uuid, %s::uuid)",
            [id, subject_id],
        )
        if not rows:
            return None
        if len(rows) != 1 or not isinstance(rows[0], dict):
            raise StoreError(
                "PostgresBillingStore.get_checkout_intent: malformed result envelope",
                details={"row_count": len(rows), "checkout_intent_id": id},
            )
        row = rows[0]
        try:
            request_digest = bytes(row["request_digest"])
            if len(request_digest) != 32:
                raise ValueError("request digest must contain 32 bytes")
            expires_at = _to_utc_iso(row["expires_at"])
            if expires_at is None:
                raise ValueError("expires_at is required")
            return CheckoutIntent(
                id=str(UUID(str(row["id"]))),
                subject_id=str(UUID(str(row["subject_id"]))),
                provider=str(row["provider"]),
                checkout_kind=row["checkout_kind"],
                product_key=str(row["product_key"]),
                request_digest=request_digest.hex(),
                status=CheckoutIntentStatus(str(row["status"])),
                provider_session_id=(str(row["provider_session_id"]) if row.get("provider_session_id") else None),
                checkout_url=(str(row["checkout_url"]) if row.get("checkout_url") else None),
                expires_at=expires_at,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise StoreError(
                "PostgresBillingStore.get_checkout_intent: row validation failed",
                cause=exc,
                details={"checkout_intent_id": id},
            ) from exc

    def list_expired_grace_subscriptions(
        self,
        now: datetime,
        limit: int = 100,
    ) -> list[BillingSubscriptionState]:
        result: list[BillingSubscriptionState] = []
        for row in self._subscription_repo.list_expired_grace_subscriptions(now, limit):
            mapped = self._row_to_subscription_state(row)
            if mapped is None:  # pragma: no cover - repository rows are non-null
                raise StoreError("expired grace subscription could not be mapped")
            result.append(mapped)
        return result

    def mark_subscription_grace_expired(self, id: str, expected_grace_ends_at: str, expired_at: str) -> bool:
        return self._subscription_repo.mark_grace_expired(id, expected_grace_ends_at, expired_at)

    def select_subscription_entitlement_source(
        self, user_id: str, provider: str, subscription_id: str | None = None
    ) -> bool:
        return self._subscription_repo.select_entitlement_source(user_id, provider, subscription_id)

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
        require_identifier_result(rows, "id", "PostgresBillingStore.record_subscription_conflict")

    def compute_topup_credits(self, amount_minor: int, topup_config: BillingTopupResult) -> Decimal:
        if isinstance(amount_minor, bool) or not isinstance(amount_minor, int):
            raise TypeError("amount_minor must be an integer")
        if amount_minor < 0:
            raise ValueError("amount_minor must be non-negative")
        unit_amount = topup_config.amount_minor
        if not unit_amount:
            return Decimal(0)
        if amount_minor % unit_amount != 0:
            return Decimal(0)
        quantity = amount_minor // unit_amount
        if quantity < topup_config.min_quantity or quantity > topup_config.max_quantity:
            return Decimal(0)
        return Decimal(quantity) * topup_config.credits_per_unit

    def pseudonymize_financial_subject(self, user_id: str) -> bool:
        rows = self._execute(
            "SELECT bursar.pseudonymize_financial_subject(%s::uuid) AS pseudonymized",
            [user_id],
        )
        return require_boolean_result(
            rows,
            "pseudonymized",
            "PostgresBillingStore.pseudonymize_financial_subject",
        )

    def update_auto_recharge_attempt_by_provider_payment(
        self,
        input: AutoRechargeProviderPaymentUpdate,
    ) -> None:
        self._auto_recharge_repo.update_attempt_by_provider_payment(input)

    def count_auto_recharge_attempts(
        self,
        user_id: str,
        since: str | datetime,
    ) -> int:
        return self._auto_recharge_repo.count_attempts(user_id, since)
