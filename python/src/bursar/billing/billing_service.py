from __future__ import annotations

import asyncio
import inspect
import math
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from bursar.billing.auto_recharge_service import AutoRechargeService
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
from bursar.billing.service_types import (
    BillingProvisioningPort,
    BillingServiceOptions,
)
from bursar.billing.types import (
    BillingAutoRechargeAttempt,
    BillingAutoRechargeProfile,
    BillingCustomerRecord,
    BillingEvent,
    BillingEventHandler,
    BillingEventResult,
    BillingEventType,
    BillingInvoiceRecord,
    BillingOfferResult,
    BillingPreferences,
    BillingSubscriptionChange,
    BillingSubscriptionChangeInput,
    BillingSubscriptionInfo,
    BillingSubscriptionState,
    BillingSubscriptionStatus,
    BillingTopupResult,
    CheckoutIntent,
)
from bursar.errors import StoreError
from bursar.shared.diagnostics import bounded_diagnostic_message
from bursar.shared.logger import NormalizedLogger, normalize_logger


def _wait_for_handler(awaitable: Any) -> None:
    async def wait() -> None:
        await awaitable

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(wait())
        return

    errors: list[BaseException] = []

    def runner() -> None:
        try:
            asyncio.run(wait())
        except BaseException as exc:  # pragma: no cover - re-raised below
            errors.append(exc)

    worker = threading.Thread(target=runner, daemon=True)
    worker.start()
    worker.join()
    if errors:
        raise errors[0]


def _camelize_model_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key.split("_", 1)[0] + "".join(part.capitalize() for part in key.split("_")[1:])
            if "_" in key
            else key: _camelize_model_keys(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_camelize_model_keys(item) for item in value]
    return value


def _billing_event_claim_envelope(event: BillingEvent) -> dict[str, Any]:
    envelope = _camelize_model_keys(
        event.model_dump(
            mode="json",
            exclude={"occurred_at", "raw", "billing_event_id", "metadata"},
            exclude_none=True,
        )
    )
    if event.metadata is not None:
        envelope["metadata"] = event.metadata
    return envelope


IGNORED_EVENT_TYPES: frozenset[BillingEventType] = frozenset(
    {
        BillingEventType.checkout_expired,
        BillingEventType.invoice_upcoming,
    }
)


@dataclass
class SubscriptionStateMerge:
    event_data: BillingSubscriptionInfo | None
    existing: BillingSubscriptionState | None

    def resolve(self, handler_override: Any, event_field: str, default: Any = None) -> Any:
        """Precedence: handler_override → event_data → existing → default."""
        if handler_override is not None:
            return handler_override
        if self.event_data is not None:
            event_val = getattr(self.event_data, event_field, None)
            if event_val is not None:
                return event_val
        if self.existing is not None:
            return getattr(self.existing, event_field, default)
        return default


class BillingService:
    auto_recharge: AutoRechargeService

    def __init__(
        self,
        billing_store: BillingStore,
        options: BillingServiceOptions | None = None,
        *,
        event_handlers: dict[BillingEventType, BillingEventHandler] | None = None,
        auto_select_entitlement_source: bool | None = None,
        provisioning: BillingProvisioningPort | None = None,
        terminal_plan_key: str | None = None,
        past_due_grace_period_ms: float | None = None,
    ) -> None:
        options = options or BillingServiceOptions()
        event_handlers = event_handlers if event_handlers is not None else options.event_handlers
        auto_select_entitlement_source = (
            auto_select_entitlement_source
            if auto_select_entitlement_source is not None
            else options.auto_select_entitlement_source
        )
        provisioning = provisioning if provisioning is not None else options.provisioning
        terminal_plan_key = terminal_plan_key if terminal_plan_key is not None else options.terminal_plan_key
        past_due_grace_period_ms = (
            past_due_grace_period_ms if past_due_grace_period_ms is not None else options.past_due_grace_period_ms
        )
        if not math.isfinite(past_due_grace_period_ms) or past_due_grace_period_ms < 0:
            raise ValueError("past_due_grace_period_ms must be a finite non-negative number")
        self._store = billing_store
        self._logger: NormalizedLogger = normalize_logger(options.logger)
        self._provisioning = provisioning
        self._event_handlers = event_handlers or {}
        self._auto_select_entitlement_source = auto_select_entitlement_source
        self._terminal_plan_key = terminal_plan_key
        self._past_due_grace_period_ms = past_due_grace_period_ms
        self.auto_recharge = AutoRechargeService(self)
        self._handlers = {
            BillingEventType.customer_created: self._handle_customer_upserted,
            BillingEventType.customer_updated: self._handle_customer_upserted,
            BillingEventType.customer_deleted: self._handle_customer_deleted,
            BillingEventType.checkout_completed: self._handle_checkout_completed,
            BillingEventType.subscription_created: self._handle_subscription_created,
            BillingEventType.subscription_updated: self._handle_subscription_updated,
            BillingEventType.subscription_activated: self._handle_subscription_activated,
            BillingEventType.subscription_renewed: self._handle_subscription_renewed,
            BillingEventType.subscription_plan_changed: self._handle_subscription_plan_changed,
            BillingEventType.subscription_cancellation_scheduled: self._handle_cancellation_scheduled,
            BillingEventType.subscription_cancellation_unscheduled: self._handle_cancellation_unscheduled,
            BillingEventType.subscription_canceled: self._handle_subscription_canceled,
            BillingEventType.subscription_expired: self._handle_subscription_expired,
            BillingEventType.subscription_paused: self._handle_subscription_paused,
            BillingEventType.subscription_resumed: self._handle_subscription_resumed,
            BillingEventType.subscription_trial_will_end: self._handle_trial_will_end,
            BillingEventType.invoice_paid: self._handle_invoice_paid,
            BillingEventType.payment_succeeded: self._handle_payment_succeeded,
            BillingEventType.payment_failed: self._handle_payment_failed,
            BillingEventType.refund_created: self._handle_refund_created,
            BillingEventType.refund_updated: self._handle_refund_created,
            BillingEventType.refund_failed: self._handle_refund_created,
            BillingEventType.dispute_created: self._handle_dispute_created,
            BillingEventType.dispute_closed: self._handle_dispute_closed,
        }

    @property
    def has_provisioning(self) -> bool:
        return self._provisioning is not None

    def get_user_subscription(
        self,
        user_id: str,
    ) -> BillingSubscriptionState | None:
        return self._expire_grace_if_needed(
            self._store.get_user_subscription(
                user_id,
                statuses=["active", "trialing", "canceled", "past_due", "incomplete"],
            )
        )

    def get_active_subscription(
        self,
        user_id: str,
    ) -> BillingSubscriptionState | None:
        return self._store.get_user_subscription(
            user_id,
            statuses=["active", "trialing"],
        )

    def list_cancellable_provider_subscription_ids(self, user_id: str) -> list[str]:
        return [subscription.provider_subscription_id for subscription in self.list_cancellable_subscriptions(user_id)]

    def list_cancellable_subscriptions(self, user_id: str) -> list[BillingSubscriptionState]:
        cancellable_statuses = {
            BillingSubscriptionStatus.active,
            BillingSubscriptionStatus.trialing,
            BillingSubscriptionStatus.past_due,
            BillingSubscriptionStatus.incomplete,
            BillingSubscriptionStatus.unpaid,
            BillingSubscriptionStatus.paused,
        }
        return [
            subscription
            for subscription in self._store.get_user_subscriptions(user_id)
            if subscription.status in cancellable_statuses and subscription.provider_subscription_id
        ]

    def get_blocking_subscription(
        self,
        user_id: str,
    ) -> BillingSubscriptionState | None:
        return self._expire_grace_if_needed(
            self._store.get_user_subscription(
                user_id,
                statuses=["active", "trialing", "past_due", "incomplete"],
            )
        )

    def create_billing_subscription_change(
        self,
        input: BillingSubscriptionChangeInput,
    ) -> BillingSubscriptionChange:
        return self._store.create_billing_subscription_change(input)

    def get_open_billing_subscription_change(
        self, provider: str, provider_subscription_id: str
    ) -> BillingSubscriptionChange | None:
        return self._store.get_open_billing_subscription_change(provider, provider_subscription_id)

    def update_billing_subscription_change(
        self,
        id: str,
        update: BillingSubscriptionChangeUpdate,
    ) -> None:
        self._store.update_billing_subscription_change(id, update)

    def _expire_grace_if_needed(self, subscription: BillingSubscriptionState | None) -> BillingSubscriptionState | None:
        if (
            not subscription
            or self._provisioning is None
            or subscription.status != BillingSubscriptionStatus.past_due
            or subscription.grace_expired_at
            or not subscription.grace_ends_at
            or not subscription.subscription_id
        ):
            return subscription
        try:
            expired = datetime.fromisoformat(subscription.grace_ends_at).timestamp() <= datetime.now(UTC).timestamp()
        except (TypeError, ValueError):
            expired = False
        if not expired:
            return subscription
        self._revoke_if_current_subscription(
            subscription.user_id,
            subscription.provider_subscription_id,
        )
        expired_at = datetime.now(UTC).isoformat()
        marked = self._store.mark_subscription_grace_expired(
            subscription.subscription_id,
            subscription.grace_ends_at,
            expired_at,
        )
        return subscription.model_copy(update={"grace_expired_at": expired_at}) if marked else subscription

    def expire_past_due_grace_periods(
        self,
        now: datetime | None = None,
    ) -> int:
        """Revoke and mark every still-current past-due grace period."""
        if self._provisioning is None:
            return 0
        effective_now = now or datetime.now(UTC)
        as_of = effective_now.isoformat()
        expired_count = 0
        for candidate in self._store.list_expired_grace_subscriptions(effective_now):
            if candidate.subscription_id is None or candidate.grace_ends_at is None:
                continue
            current = self._store.get_billing_subscription(
                candidate.provider,
                candidate.provider_subscription_id,
            )
            if (
                current is None
                or current.status != BillingSubscriptionStatus.past_due
                or current.grace_ends_at != candidate.grace_ends_at
                or current.grace_expired_at
            ):
                continue
            self._revoke_if_current_subscription(
                candidate.user_id,
                candidate.provider_subscription_id,
            )
            if self._store.mark_subscription_grace_expired(
                candidate.subscription_id,
                candidate.grace_ends_at,
                as_of,
            ):
                expired_count += 1
        return expired_count

    def invalidate_offer_cache(self) -> None:
        """Invalidate offer resolution state.

        The Python store performs uncached lookups, so there is no local cache
        to clear. The method is retained to mirror the JavaScript capability.
        """

    def create_or_get_checkout_intent(
        self,
        input: CheckoutIntentCreate,
    ) -> CheckoutIntent:
        return self._store.create_or_get_checkout_intent(input)

    def update_checkout_intent(
        self,
        id: str,
        update: CheckoutIntentUpdate,
    ) -> None:
        self._store.update_checkout_intent(id, update)

    def get_checkout_intent(self, id: str, subject_id: str) -> CheckoutIntent | None:
        return self._store.get_checkout_intent(id, subject_id)

    def get_user_preferences(self, user_id: str) -> BillingPreferences | None:
        """Get billing preferences for a user.

        Returns None if no preferences row exists.
        """
        return self._store.get_billing_preferences(user_id)

    def update_user_preferences(self, prefs: BillingPreferences) -> None:
        """Insert or update billing preferences for a user."""
        self._store.upsert_billing_preferences(prefs)

    def list_billing_invoices(self, user_id: str) -> list[BillingInvoiceRecord]:
        return self._store.list_billing_invoices(user_id)

    def upsert_billing_subscription(self, state: BillingSubscriptionState) -> None:
        self._store.upsert_billing_subscription(state)

    def get_customer_by_user_id(
        self,
        user_id: str,
        provider: str | None = None,
    ) -> BillingCustomerRecord | None:
        """Reverse lookup: find a user's billing customer record.

        Args:
            user_id: The user ID.
            provider: Optional provider filter. If None, returns the most recently updated.

        Returns:
            BillingCustomerRecord if found, None otherwise.
        """
        return self._store.get_billing_customer_by_user_id(user_id, provider)

    def resolve_offer(
        self,
        provider: str,
        product_id: str | None = None,
        price_id: str | None = None,
    ) -> BillingOfferResult | None:
        """Resolve a billing offer by provider and product/price IDs.

        This is a convenience wrapper around the store's resolveBillingOffer,
        so callers don't need to access the store directly.
        """
        return self._store.resolve_billing_offer(provider, product_id, price_id)

    def resolve_offer_by_lookup(
        self,
        provider: str,
        lookup_key: str,
    ) -> BillingOfferResult | None:
        return self._resolve_offer_by_lookup(provider, lookup_key)

    def resolve_topup(
        self,
        provider: str,
        product_id: str | None = None,
        price_id: str | None = None,
    ) -> BillingTopupResult | None:
        """Resolve a credit topup by provider and product/price IDs.

        This is a convenience wrapper around the store's resolveCreditTopup,
        so callers don't need to access the store directly.
        """
        return self._store.resolve_credit_topup(provider, product_id, price_id)

    def resolve_topup_by_lookup(
        self,
        provider: str,
        lookup_key: str,
    ) -> BillingTopupResult | None:
        return self._store.resolve_credit_topup_by_lookup(provider, lookup_key)

    def upsert_customer(
        self,
        provider: str,
        provider_customer_id: str,
        user_id: str,
        email: str | None = None,
    ) -> None:
        """Insert or update a billing customer record.

        This is a convenience wrapper around the store's upsert_billing_customer,
        so callers don't need to access the store directly.
        """
        self._store.upsert_billing_customer(provider, provider_customer_id, user_id, email)

    def get_auto_recharge_profile(self, user_id: str) -> BillingAutoRechargeProfile | None:
        return self._store.get_auto_recharge_profile(user_id)

    def get_active_catalog_document(self) -> dict[str, Any] | None:
        """Return the source document for the active catalog revision."""
        return self._store.get_active_catalog_document()

    def upsert_auto_recharge_profile(
        self,
        profile: BillingAutoRechargeProfile,
        *,
        reset_cooldown: bool = False,
    ) -> None:
        self._store.upsert_auto_recharge_profile(profile, reset_cooldown=reset_cooldown)

    def claim_auto_recharge_attempt(
        self,
        input: AutoRechargeAttemptClaim,
    ) -> BillingAutoRechargeAttempt | None:
        return self._store.claim_auto_recharge_attempt(input)

    def update_auto_recharge_attempt(
        self,
        input: AutoRechargeAttemptUpdate,
    ) -> None:
        self._store.update_auto_recharge_attempt(input)

    def update_auto_recharge_attempt_by_provider_payment(
        self,
        input: AutoRechargeProviderPaymentUpdate,
    ) -> None:
        self._store.update_auto_recharge_attempt_by_provider_payment(input)

    def count_auto_recharge_attempts(
        self,
        user_id: str,
        since: str | datetime,
    ) -> int:
        return self._store.count_auto_recharge_attempts(user_id, since)

    def pseudonymize_financial_subject(self, user_id: str) -> None:
        self._store.pseudonymize_financial_subject(user_id)

    def record_subscription_conflict(
        self,
        input: BillingSubscriptionConflictCreate,
    ) -> None:
        self._store.record_subscription_conflict(input)

    def ingest_billing_event(self, event: BillingEvent) -> BillingEventResult:
        claim_envelope = _billing_event_claim_envelope(event)
        claim = self._store.claim_billing_event(
            event.provider,
            event.event_id,
            event.event_type,
            claim_envelope,
        )
        if claim.status == "duplicate":
            self._logger.debug(
                "duplicate billing event",
                {"provider": event.provider, "event_id": event.event_id},
            )
            return BillingEventResult(handled=True, action="duplicate")

        if claim.status == "busy":
            self._logger.debug(
                "billing event is already processing",
                {"provider": event.provider, "event_id": event.event_id},
            )
            return BillingEventResult(handled=False, error="claim_busy")

        if claim.status == "retry":
            self._logger.warn(
                "billing event retry — skipping",
                {"provider": event.provider, "event_id": event.event_id},
            )
            return BillingEventResult(handled=False, error="claim_failed_retry")
        if not claim.claim_token:
            return BillingEventResult(handled=False, error="claim_token_missing")

        try:
            event.billing_event_id = claim.billing_event_id
            result = self._route_event(event)
        except Exception as exc:
            return self._record_billing_event_failure(event, claim.claim_token, exc)

        if not result.handled:
            message = bounded_diagnostic_message(
                result.error,
                "billing_event_not_handled",
            )
            self._record_billing_event_failure(
                event,
                claim.claim_token,
                message,
                log_as_error=False,
            )
            return result.model_copy(update={"error": message})

        try:
            completed = self._store.complete_billing_event(
                event.provider,
                event.event_id,
                claim.claim_token,
            )
        except Exception as exc:
            return self._record_billing_event_failure(event, claim.claim_token, exc)

        if not completed:
            return self._record_billing_event_failure(
                event,
                claim.claim_token,
                "billing_event_completion_rejected",
            )
        return result

    def _record_billing_event_failure(
        self,
        event: BillingEvent,
        claim_token: str,
        error: object | None,
        *,
        log_as_error: bool = True,
    ) -> BillingEventResult:
        message = bounded_diagnostic_message(error, "billing_event_processing_failed")
        log = self._logger.error if log_as_error else self._logger.warn
        log(
            "failed to handle billing event",
            {
                "provider": event.provider,
                "event_id": event.event_id,
                "error": message,
            },
        )
        failed = self._store.fail_billing_event(
            event.provider,
            event.event_id,
            claim_token,
            message,
        )
        if not failed:
            self._logger.warn(
                "billing event failure was not persisted",
                {
                    "provider": event.provider,
                    "event_id": event.event_id,
                },
            )
        return BillingEventResult(handled=False, error=message)

    def _route_event(self, event: BillingEvent) -> BillingEventResult:
        handler = self._handlers.get(event.event_type)
        if handler is None:
            if event.event_type in IGNORED_EVENT_TYPES:
                if event.event_type == BillingEventType.checkout_expired:
                    self._update_checkout_intent_from_event(event, "expired")
                return BillingEventResult(handled=True, action="ignored")
            event_type_name = (
                event.event_type.value if isinstance(event.event_type, BillingEventType) else str(event.event_type)
            )
            self._logger.warn(
                "unhandled billing event type (marking as failed)",
                {"event_type": event_type_name},
            )
            return BillingEventResult(handled=False, error="unhandled_event_type")
        result = handler(event)
        if result.handled:
            self._fire_event_handlers(event, event.account_id)
        return result

    def _update_checkout_intent_from_event(
        self,
        event: BillingEvent,
        status: Literal["completed", "failed", "expired"],
    ) -> None:
        intent_id = event.metadata.get("checkout_intent_id") if event.metadata else None
        if isinstance(intent_id, str) and intent_id:
            self._store.update_checkout_intent(
                intent_id,
                CheckoutIntentUpdate(status=status),
            )

    def _fire_event_handlers(self, event: BillingEvent, account_id: str | None) -> None:
        if not account_id:
            return
        handler = self._event_handlers.get(event.event_type)
        if handler is None:
            return
        try:
            result = handler(event, account_id)
            if inspect.isawaitable(result):
                _wait_for_handler(result)
        except Exception as exc:
            self._logger.error(
                "event handler failed",
                {
                    "provider": event.provider,
                    "event_id": event.event_id,
                    "error": str(exc),
                },
            )

    def _resolve_account_id(self, event: BillingEvent) -> str | None:
        if event.account_id:
            return event.account_id
        if event.customer and event.customer.provider_customer_id:
            uid = self._store.get_billing_customer(
                event.provider,
                event.customer.provider_customer_id,
            )
            if uid:
                event.account_id = uid
                return uid
        if event.subscription and event.subscription.provider_subscription_id:
            existing = self._store.get_billing_subscription(
                event.provider,
                event.subscription.provider_subscription_id,
            )
            if existing and existing.user_id:
                event.account_id = existing.user_id
                return existing.user_id
        # Refund/dispute events (e.g. a Stripe dashboard refund) carry no customer
        # or subscription — the only link to the user is the stored payment row.
        # Without this tier a refund's credit clawback is silently skipped (parity
        # with the JS resolveAccountId payment fallback).
        provider_payment_id = (
            (event.payment.provider_payment_id if event.payment else None)
            or (event.refund.provider_payment_id if event.refund else None)
            or (event.dispute.provider_payment_id if event.dispute else None)
        )
        if provider_payment_id:
            payment = self._store.get_billing_payment(event.provider, provider_payment_id)
            uid = payment.get("user_id") if isinstance(payment, dict) else None
            if isinstance(uid, str) and uid:
                event.account_id = uid
                return uid
        return None

    def _offer_for_event(
        self, event: BillingEvent
    ) -> tuple[BillingOfferResult | None, str | None, str | None, str | None]:
        if not event.subscription:
            return None, None, None, None
        refs = event.subscription.refs
        if not refs:
            return None, None, None, None
        offer = self._store.resolve_billing_offer(
            event.provider,
            product_id=refs.product_id,
            price_id=refs.price_id,
        )
        if not offer and refs.lookup_key:
            offer = self._resolve_offer_by_lookup(event.provider, refs.lookup_key)
        if not offer:
            return None, None, None, None
        return offer, offer.offer_key, offer.plan, offer.offer_id

    def _resolve_offer_by_lookup(self, provider: str, lookup_key: str) -> BillingOfferResult | None:
        result = self._store.resolve_billing_offer_by_lookup(provider, lookup_key)
        if result and result.offer_key and result.plan:
            return result
        return None

    def _subscription_state(
        self,
        event: BillingEvent,
        uid: str,
        existing: BillingSubscriptionState | None = None,
        *,
        status: str | None = None,
        cancel_at_period_end: bool | None = None,
        offer_key: str | None = None,
        plan_key: str | None = None,
        offer_id: str | None = None,
        interval: str | None = None,
        interval_count: int | None = None,
        metadata: dict[str, Any] | None = None,
        grace_ends_at: str | None = None,
        grace_expired_at: str | None = None,
    ) -> BillingSubscriptionState:
        if not event.subscription:
            raise TypeError("billing subscription event requires subscription data")
        sub = event.subscription
        merger = SubscriptionStateMerge(sub, existing)

        _status = status
        if _status is None and sub.status is not None:
            _status = sub.status.value
        if _status is None and existing is not None:
            _status = existing.status.value if existing.status else None
        if _status is None:
            _status = "incomplete"

        return BillingSubscriptionState(
            user_id=uid,
            provider=event.provider,
            provider_subscription_id=sub.provider_subscription_id,
            provider_customer_id=(event.customer.provider_customer_id if event.customer else None)
            or (existing.provider_customer_id if existing else None),
            offer_key=merger.resolve(offer_key, "offer_key"),
            offer_id=offer_id if offer_id is not None else (existing.offer_id if existing else None),
            plan=merger.resolve(plan_key, "plan"),
            status=BillingSubscriptionStatus(_status),
            current_period_start=sub.period_start or (existing.current_period_start if existing else None),
            current_period_end=sub.period_end or (existing.current_period_end if existing else None),
            trial_end=sub.trial_end or (existing.trial_end if existing else None),
            cancel_at=sub.cancel_at or (existing.cancel_at if existing else None),
            ended_at=sub.ended_at or (existing.ended_at if existing else None),
            grace_ends_at=(
                grace_ends_at or (existing.grace_ends_at if existing else None)
                if _status == BillingSubscriptionStatus.past_due.value
                else None
            ),
            grace_expired_at=(
                grace_expired_at or (existing.grace_expired_at if existing else None)
                if _status == BillingSubscriptionStatus.past_due.value
                else None
            ),
            provider_updated_at=event.occurred_at,
            cancel_at_period_end=merger.resolve(cancel_at_period_end, "cancel_at_period_end", False),
            interval=merger.resolve(interval, "interval"),
            interval_count=merger.resolve(interval_count, "interval_count"),
            metadata=(
                metadata
                if metadata is not None
                else (
                    {
                        **(existing.metadata or {} if existing else {}),
                        **(event.metadata or {}),
                    }
                    if event.metadata or (existing and existing.metadata)
                    else None
                )
            ),
        )

    def _apply_subscription_event(
        self,
        event: BillingEvent,
        *,
        status: str | None = None,
        cancel_at_period_end: bool | None = None,
        resolve_offers: bool = True,
        action: str = "",
        provision_on_positive: bool = True,
    ) -> BillingEventResult:
        """Common path for all subscription event handlers."""
        uid = self._resolve_account_id(event)
        if not uid:
            return BillingEventResult(handled=False, error="account_not_found")
        if not event.subscription:
            return BillingEventResult(handled=False, error="no_subscription_data")

        existing = self._store.get_billing_subscription(
            event.provider,
            event.subscription.provider_subscription_id,
        )

        offer = None
        offer_key = None
        plan_key = None
        offer_id = None
        if resolve_offers:
            offer, offer_key, plan_key, offer_id = self._offer_for_event(event)

        preserved_allowance_anchor = None
        if action == "plan_changed" and self._provisioning:
            preserved_allowance_anchor = self._provisioning.get_user_plan(uid).plan_assigned_at

        pending = None
        if action == "plan_changed":
            pending = self._store.get_open_billing_subscription_change(
                event.provider,
                event.subscription.provider_subscription_id,
            )
            if pending:
                self._store.update_billing_subscription_change(
                    pending.id,
                    BillingSubscriptionChangeUpdate(state="applied"),
                )

        state_metadata = None
        if action == "plan_changed":
            state_metadata = {
                **(existing.metadata or {} if existing else {}),
                **(event.metadata or {}),
                "pendingPlanChange": None,
            }
        self._store.upsert_billing_subscription(
            self._subscription_state(
                event,
                uid,
                existing,
                status=status,
                cancel_at_period_end=cancel_at_period_end,
                offer_key=offer_key if offer_key is not None else (existing.offer_key if existing else None),
                plan_key=plan_key if plan_key is not None else (existing.plan if existing else None),
                offer_id=offer_id if offer_id is not None else (existing.offer_id if existing else None),
                interval=offer.interval if offer else None,
                interval_count=offer.interval_count if offer else None,
                metadata=state_metadata,
            )
        )

        if self._provisioning and provision_on_positive:
            self._provision_subscription(
                uid,
                offer,
                event,
                plan_key_override=plan_key if plan_key is not None else (existing.plan if existing else None),
                preserve_allowance_anchor=action == "plan_changed",
                preserved_allowance_anchor=preserved_allowance_anchor,
            )

        return BillingEventResult(handled=True, action=action)

    def _handle_customer_upserted(self, event: BillingEvent) -> BillingEventResult:
        if event.customer and event.customer.provider_customer_id:
            uid = self._resolve_account_id(event)
            if uid:
                self._store.upsert_billing_customer(
                    event.provider,
                    event.customer.provider_customer_id,
                    uid,
                    event.customer.email,
                )
        action = "customer_created" if event.event_type == BillingEventType.customer_created else "customer_updated"
        return BillingEventResult(handled=True, action=action)

    def _handle_customer_deleted(self, event: BillingEvent) -> BillingEventResult:
        if event.customer and event.customer.provider_customer_id:
            uid = self._resolve_account_id(event)
            if uid and self._provisioning:
                self._revoke_subscription(uid)
        return BillingEventResult(handled=True, action="customer_deleted")

    def _handle_checkout_completed(self, event: BillingEvent) -> BillingEventResult:
        if event.customer and event.customer.provider_customer_id:
            uid = self._resolve_account_id(event)
            if uid:
                self._store.upsert_billing_customer(
                    event.provider,
                    event.customer.provider_customer_id,
                    uid,
                    event.customer.email,
                )
        if event.subscription:
            return self._handle_subscription_created(event)
        self._update_checkout_intent_from_event(event, "completed")
        return BillingEventResult(handled=True, action="checkout_completed")

    def _handle_subscription_created(self, event: BillingEvent) -> BillingEventResult:
        uid = self._resolve_account_id(event)
        if not uid:
            return BillingEventResult(handled=False, error="account_not_found")
        if event.customer and event.customer.provider_customer_id:
            self._store.upsert_billing_customer(
                event.provider,
                event.customer.provider_customer_id,
                uid,
                event.customer.email,
            )
        if not event.subscription or not event.subscription.provider_subscription_id:
            return BillingEventResult(handled=False, error="no_subscription_data")

        sub_id = event.subscription.provider_subscription_id
        existing = self._store.get_billing_subscription(event.provider, sub_id)

        blocking_statuses = {"active", "trialing", "past_due", "incomplete"}
        all_user_subs = self._store.get_user_subscriptions(uid)
        existing_for_provider = next(
            (
                s
                for s in all_user_subs
                if s.provider == event.provider
                and s.provider_subscription_id != sub_id
                and s.status in blocking_statuses
            ),
            None,
        )
        if existing_for_provider is not None:
            self._store.record_subscription_conflict(
                BillingSubscriptionConflictCreate(
                    user_id=uid,
                    provider=event.provider,
                    duplicate_subscription_id=sub_id,
                    existing_subscription_id=(existing_for_provider.provider_subscription_id),
                    event_id=event.event_id,
                    metadata=event.metadata or {},
                )
            )
            return BillingEventResult(handled=False, error="subscription_conflict")

        offer, offer_key, plan_key, offer_id = self._offer_for_event(event)
        st = event.subscription.status.value if event.subscription.status else None
        subscription_state = self._subscription_state(
            event,
            uid,
            existing,
            status=st,
            cancel_at_period_end=event.subscription.cancel_at_period_end,
            offer_key=offer_key if offer_key is not None else (existing.offer_key if existing else None),
            offer_id=offer_id if offer_id is not None else (existing.offer_id if existing else None),
            plan_key=plan_key if plan_key is not None else (existing.plan if existing else None),
        )
        try:
            self._store.upsert_billing_subscription(subscription_state)
        except Exception as exc:
            pgcode = getattr(exc, "pgcode", None)
            if pgcode != "23505":
                raise
            concurrent = next(
                (
                    s
                    for s in self._store.get_user_subscriptions(uid)
                    if s.provider == event.provider
                    and s.provider_subscription_id != sub_id
                    and s.status in blocking_statuses
                ),
                None,
            )
            if concurrent is None:
                raise
            self._store.record_subscription_conflict(
                BillingSubscriptionConflictCreate(
                    user_id=uid,
                    provider=event.provider,
                    duplicate_subscription_id=sub_id,
                    existing_subscription_id=(concurrent.provider_subscription_id),
                    event_id=event.event_id,
                    metadata=event.metadata or {},
                )
            )
            return BillingEventResult(handled=False, error="subscription_conflict")

        if self._provisioning and st and st in ("active", "trialing"):
            self._provision_subscription(
                uid,
                offer,
                event,
            )
        if st in ("active", "trialing"):
            self._update_checkout_intent_from_event(event, "completed")

        return BillingEventResult(
            handled=True,
            action="subscription_created",
            subscription_id=sub_id,
        )

    def _handle_subscription_updated(self, event: BillingEvent) -> BillingEventResult:
        result = self._apply_subscription_event(
            event,
            status=event.subscription.status.value if event.subscription and event.subscription.status else None,
            cancel_at_period_end=event.subscription.cancel_at_period_end if event.subscription else None,
            action="subscription_updated",
            provision_on_positive=False,
        )
        if result.handled:
            uid = self._resolve_account_id(event)
            if uid:
                self._re_evaluate_access(uid, event)
        return result

    def _handle_subscription_activated(self, event: BillingEvent) -> BillingEventResult:
        return self._apply_subscription_event(
            event,
            status="active",
            action="subscription_activated",
            provision_on_positive=True,
        )

    def _handle_subscription_renewed(self, event: BillingEvent) -> BillingEventResult:
        result = self._apply_subscription_event(
            event,
            status="active",
            action="subscription_renewed",
            provision_on_positive=True,
        )
        if result.handled:
            offer, _, _, _ = self._offer_for_event(event)
            self._grant_subscription_cycle(event, offer)
        return result

    def _handle_subscription_plan_changed(self, event: BillingEvent) -> BillingEventResult:
        st = event.subscription.status.value if event.subscription and event.subscription.status else "active"
        return self._apply_subscription_event(
            event,
            status=st,
            action="plan_changed",
            provision_on_positive=True,
        )

    def _handle_cancellation_scheduled(self, event: BillingEvent) -> BillingEventResult:
        return self._apply_subscription_event(
            event,
            cancel_at_period_end=True,
            resolve_offers=False,
            action="cancellation_scheduled",
            provision_on_positive=False,
        )

    def _handle_cancellation_unscheduled(self, event: BillingEvent) -> BillingEventResult:
        return self._apply_subscription_event(
            event,
            cancel_at_period_end=False,
            resolve_offers=False,
            action="cancellation_unscheduled",
            provision_on_positive=False,
        )

    def _handle_subscription_canceled(self, event: BillingEvent) -> BillingEventResult:
        uid = self._resolve_account_id(event)
        if not uid:
            return BillingEventResult(handled=False, error="account_not_found")
        if not event.subscription or not event.subscription.provider_subscription_id:
            return BillingEventResult(handled=False, error="no_subscription_data")

        sub_id = event.subscription.provider_subscription_id
        existing = self._store.get_billing_subscription(event.provider, sub_id)
        offer = None
        offer_key = None
        plan_key = None
        offer_id = None
        if existing is None:
            offer, offer_key, plan_key, offer_id = self._offer_for_event(event)
            if not offer_id:
                raise StoreError(
                    "cannot persist cancellation for unknown subscription "
                    f"{event.provider}/{sub_id}: offer could not be resolved",
                    retryable=True,
                    details={"provider": event.provider, "provider_subscription_id": sub_id},
                )

        self._store.upsert_billing_subscription(
            self._subscription_state(
                event,
                uid,
                existing,
                status="canceled",
                cancel_at_period_end=(
                    event.subscription.cancel_at_period_end
                    if event.subscription.cancel_at_period_end is not None
                    else True
                ),
                offer_key=offer_key if offer_key is not None else (existing.offer_key if existing else None),
                offer_id=offer_id if offer_id is not None else (existing.offer_id if existing else None),
                plan_key=plan_key if plan_key is not None else (existing.plan if existing else None),
                interval=offer.interval if offer is not None else None,
                interval_count=offer.interval_count if offer is not None else None,
            )
        )
        if self._provisioning:
            self._revoke_if_current_subscription(uid, sub_id)
        return BillingEventResult(handled=True, action="subscription_canceled")

    def _handle_subscription_expired(self, event: BillingEvent) -> BillingEventResult:
        if event.subscription:
            cot = (
                event.subscription.cancel_at_period_end if event.subscription.cancel_at_period_end is not None else True
            )
        else:
            cot = None
        result = self._apply_subscription_event(
            event,
            status="expired",
            cancel_at_period_end=cot,
            resolve_offers=False,
            action="subscription_expired",
            provision_on_positive=False,
        )
        if result.handled:
            uid = self._resolve_account_id(event)
            if uid and self._provisioning and event.subscription:
                self._revoke_if_current_subscription(uid, event.subscription.provider_subscription_id)
        return result

    def _handle_subscription_paused(self, event: BillingEvent) -> BillingEventResult:
        result = self._apply_subscription_event(
            event,
            status="paused",
            resolve_offers=False,
            action="subscription_paused",
            provision_on_positive=False,
        )
        if result.handled:
            uid = self._resolve_account_id(event)
            if uid and self._provisioning and event.subscription:
                self._revoke_if_current_subscription(uid, event.subscription.provider_subscription_id)
        return result

    def _handle_subscription_resumed(self, event: BillingEvent) -> BillingEventResult:
        return self._apply_subscription_event(
            event,
            status="active",
            cancel_at_period_end=False,
            action="subscription_resumed",
            provision_on_positive=True,
        )

    def _handle_trial_will_end(self, event: BillingEvent) -> BillingEventResult:
        self._resolve_account_id(event)
        return BillingEventResult(handled=True, action="trial_will_end_notified")

    def _handle_invoice_paid(self, event: BillingEvent) -> BillingEventResult:
        if event.invoice is None:
            return BillingEventResult(handled=False, error="no_invoice_data")
        renewal_result = (
            self._handle_subscription_renewed(event)
            if event.subscription
            else BillingEventResult(handled=True, action="invoice_paid")
        )
        if not renewal_result.handled:
            return renewal_result
        uid = self._resolve_account_id(event)
        if uid:
            self._store.upsert_billing_invoice(
                BillingInvoiceUpsert(
                    provider=event.provider,
                    provider_invoice_id=event.invoice.provider_invoice_id,
                    provider_subscription_id=(
                        event.subscription.provider_subscription_id if event.subscription else None
                    ),
                    user_id=uid,
                    status=event.invoice.status,
                    amount_paid_minor=event.invoice.amount_paid_minor,
                    amount_due_minor=event.invoice.amount_due_minor,
                    currency=event.invoice.currency,
                    period_start=event.invoice.period_start,
                    period_end=event.invoice.period_end,
                    provider_updated_at=event.occurred_at,
                    metadata=event.metadata,
                )
            )
        return renewal_result

    def _handle_payment_succeeded(self, event: BillingEvent) -> BillingEventResult:
        if not event.payment:
            return BillingEventResult(handled=False, error="no_payment_data")

        uid = self._resolve_account_id(event)

        topup_config = None
        if event.payment.purpose == "credit_topup" and event.payment.refs:
            topup_config = self._store.resolve_credit_topup(
                event.provider,
                product_id=event.payment.refs.product_id,
                price_id=event.payment.refs.price_id,
            )

        payment_id: str | None = None
        if uid:
            payment_metadata: dict | None = None
            if topup_config and event.payment.purpose == "credit_topup":
                payment_metadata = {
                    "credits_per_unit": str(topup_config.credits_per_unit),
                }
            payment_id = self._store.upsert_billing_payment(
                BillingPaymentUpsert(
                    provider=event.provider,
                    provider_payment_id=event.payment.provider_payment_id,
                    provider_invoice_id=None,
                    user_id=uid,
                    amount_minor=event.payment.amount_minor,
                    tax_minor=event.payment.tax_minor,
                    currency=event.payment.currency,
                    purpose=event.payment.purpose,
                    status=event.payment.status,
                    provider_updated_at=event.occurred_at,
                    metadata=payment_metadata,
                )
            )

        if topup_config and event.payment.purpose == "credit_topup" and uid:
            amt = event.payment.amount_minor
            below_minimum = amt < topup_config.min_amount_minor
            above_maximum = amt > topup_config.max_amount_minor
            if below_minimum or above_maximum:
                self._logger.warn(
                    "topup amount outside configured bounds",
                    {
                        "amount_minor": amt,
                        "min_amount_minor": topup_config.min_amount_minor,
                        "max_amount_minor": topup_config.max_amount_minor,
                    },
                )
                return BillingEventResult(handled=True, action="payment_succeeded_out_of_bounds")
            credits = self._store.compute_topup_credits(amt, topup_config)
            unit_amount = topup_config.amount_minor
            quantity = amt // unit_amount if unit_amount and amt % unit_amount == 0 else 0
            if credits > 0 and payment_id and quantity > 0:
                grant_id = self._store.create_billing_credit_grant(
                    BillingCreditGrantCreate(
                        payment_id=payment_id,
                        topup_id=topup_config.topup_id,
                        configured_credits=topup_config.credits_per_unit,
                        quantity=quantity,
                    )
                )
                self._store.grant_billing_credit(grant_id, f"billing:{event.event_id}:topup")
                self._logger.info(
                    "granted topup credits",
                    {
                        "credits": credits,
                        "user_id": uid,
                        "provider_payment_id": event.payment.provider_payment_id,
                    },
                )
            self._store.update_auto_recharge_attempt_by_provider_payment(
                AutoRechargeProviderPaymentUpdate(
                    provider=event.provider,
                    provider_payment_id=event.payment.provider_payment_id,
                    state="succeeded",
                )
            )

        if event.payment.purpose == "subscription" and event.subscription and uid:
            renewal_result = self._handle_subscription_renewed(event)
            if not renewal_result.handled:
                return renewal_result
            self._store.upsert_billing_invoice(
                BillingInvoiceUpsert(
                    provider=event.provider,
                    provider_invoice_id=event.payment.provider_payment_id,
                    provider_subscription_id=(event.subscription.provider_subscription_id),
                    user_id=uid,
                    status="paid",
                    amount_paid_minor=event.payment.amount_minor,
                    amount_due_minor=event.payment.amount_minor,
                    currency=event.payment.currency,
                    period_start=event.subscription.period_start,
                    period_end=event.subscription.period_end,
                    provider_updated_at=event.occurred_at,
                    metadata=event.metadata,
                )
            )

        intent_id = event.metadata.get("checkout_intent_id") if event.metadata else None
        if intent_id:
            self._store.update_checkout_intent(
                intent_id,
                CheckoutIntentUpdate(status="completed"),
            )

        return BillingEventResult(handled=True, action="payment_succeeded")

    def _handle_payment_failed(self, event: BillingEvent) -> BillingEventResult:
        if event.payment is None:
            return BillingEventResult(handled=False, error="no_payment_data")
        uid = self._resolve_account_id(event)
        if uid:
            self._store.upsert_billing_payment(
                BillingPaymentUpsert(
                    provider=event.provider,
                    provider_payment_id=event.payment.provider_payment_id,
                    user_id=uid,
                    amount_minor=event.payment.amount_minor,
                    tax_minor=event.payment.tax_minor,
                    currency=event.payment.currency,
                    purpose=event.payment.purpose,
                    status=event.payment.status,
                    provider_updated_at=event.occurred_at,
                )
            )
        self._store.update_auto_recharge_attempt_by_provider_payment(
            AutoRechargeProviderPaymentUpdate(
                provider=event.provider,
                provider_payment_id=event.payment.provider_payment_id,
                state="failed",
                failure_code="provider_payment_failed",
            )
        )
        if uid and event.subscription:
            existing = self._store.get_billing_subscription(event.provider, event.subscription.provider_subscription_id)
            grace_base = datetime.fromisoformat(event.occurred_at)
            grace_ends_at = (grace_base + timedelta(milliseconds=self._past_due_grace_period_ms)).isoformat()
            self._store.upsert_billing_subscription(
                self._subscription_state(
                    event,
                    uid,
                    existing,
                    status="past_due",
                    grace_ends_at=grace_ends_at,
                ).model_copy(update={"grace_expired_at": None})
            )
        intent_id = event.metadata.get("checkout_intent_id") if event.metadata else None
        if intent_id:
            self._store.update_checkout_intent(
                intent_id,
                CheckoutIntentUpdate(status="failed"),
            )
        return BillingEventResult(handled=True, action="payment_failed_recorded")

    def _handle_refund_created(self, event: BillingEvent) -> BillingEventResult:
        if event.refund is None:
            return BillingEventResult(handled=False, error="no_refund_data")
        uid = self._resolve_account_id(event)
        if uid:
            refund = event.refund
            refund_id = self._store.upsert_billing_refund(
                BillingRefundUpsert(
                    provider=event.provider,
                    provider_refund_id=refund.provider_refund_id,
                    provider_payment_id=refund.provider_payment_id,
                    user_id=uid,
                    amount_minor=refund.amount_minor,
                    currency=refund.currency,
                    reason=refund.reason,
                    status=refund.status,
                    provider_updated_at=event.occurred_at,
                )
            )
            if refund.status == "succeeded":
                payment = self._store.get_billing_payment(event.provider, refund.provider_payment_id)
                if payment and payment.purpose == "credit_topup":
                    grant_id = self._store.get_billing_credit_grant_by_payment(payment.id)
                    if grant_id:
                        result = self._store.post_billing_refund(
                            refund_id,
                            grant_id,
                            refund.amount_minor,
                            f"billing:{event.event_id}:refund",
                        )
                        if not result.error_code:
                            return BillingEventResult(handled=True, action="refund_clawback")
        return BillingEventResult(handled=True, action="refund_recorded")

    def _handle_dispute_created(self, event: BillingEvent) -> BillingEventResult:
        if event.dispute is None:
            return BillingEventResult(handled=False, error="no_dispute_data")
        self._store.upsert_billing_dispute(
            BillingDisputeUpsert(
                provider=event.provider,
                provider_dispute_id=event.dispute.provider_dispute_id,
                provider_payment_id=event.dispute.provider_payment_id,
                status=event.dispute.status,
                reason=event.dispute.reason,
                provider_updated_at=event.occurred_at,
                metadata=event.metadata,
            )
        )
        return BillingEventResult(handled=True, action="dispute_recorded")

    def _handle_dispute_closed(self, event: BillingEvent) -> BillingEventResult:
        if event.dispute is None:
            return BillingEventResult(handled=False, error="no_dispute_data")
        self._store.upsert_billing_dispute(
            BillingDisputeUpsert(
                provider=event.provider,
                provider_dispute_id=event.dispute.provider_dispute_id,
                provider_payment_id=event.dispute.provider_payment_id,
                status=event.dispute.status,
                reason=event.dispute.reason,
                provider_updated_at=event.occurred_at,
                metadata=event.metadata,
            )
        )
        return BillingEventResult(handled=True, action="dispute_closed")

    def _provision_subscription(
        self,
        uid: str,
        offer: BillingOfferResult | None,
        event: BillingEvent,
        *,
        plan_key_override: str | None = None,
        preserve_allowance_anchor: bool = False,
        preserved_allowance_anchor: datetime | str | None = None,
    ) -> None:
        if not self._provisioning:
            return

        plan_key = plan_key_override or (offer.plan if offer else None)
        if not plan_key:
            return

        period_start: datetime | None = None
        if preserve_allowance_anchor:
            if preserved_allowance_anchor:
                try:
                    anchor = (
                        preserved_allowance_anchor
                        if isinstance(preserved_allowance_anchor, datetime)
                        else datetime.fromisoformat(preserved_allowance_anchor.replace("Z", "+00:00"))
                    )
                    if anchor.tzinfo is None:
                        anchor = anchor.replace(tzinfo=UTC)
                    period_start = datetime.now(UTC) if anchor > datetime.now(UTC) else anchor
                except (ValueError, TypeError):
                    period_start = None
        elif event.subscription:
            ps = event.subscription.period_start
            if ps:
                try:
                    period_start = datetime.fromisoformat(ps)
                except (ValueError, TypeError):
                    self._logger.warn(
                        "invalid period_start timestamp; using now()",
                        {"period_start": ps, "user_id": uid},
                    )
                    period_start = None

        self._provisioning.set_user_plan(uid, plan_key, plan_assigned_at=period_start)

        if self._auto_select_entitlement_source and event.provider:
            selected = self._store.select_subscription_entitlement_source(
                uid,
                event.provider,
                event.subscription.provider_subscription_id if event.subscription else None,
            )
            if selected:
                self._logger.info(
                    "selected subscription as entitlement source",
                    {
                        "provider": event.provider,
                        "provider_subscription_id": (
                            event.subscription.provider_subscription_id if event.subscription else None
                        ),
                        "user_id": uid,
                    },
                )

        self._logger.info(
            "provisioned plan",
            {"plan_key": plan_key, "user_id": uid},
        )

    def _grant_subscription_cycle(self, event: BillingEvent, offer: BillingOfferResult | None) -> None:
        grant = offer.grant if offer else None
        credits = grant.credits if grant and grant.mode == "cycle_grant" else None
        if not credits or Decimal(str(credits)) <= 0 or not event.billing_event_id or not event.subscription:
            return

        subscription = self._store.get_billing_subscription(
            event.provider,
            event.subscription.provider_subscription_id,
        )
        if not subscription or not subscription.subscription_id:
            raise StoreError(
                "subscription cycle grant requires a persisted subscription",
                indeterminate=True,
                details={
                    "provider": event.provider,
                    "provider_subscription_id": event.subscription.provider_subscription_id,
                },
            )

        payment = (
            self._store.get_billing_payment(event.provider, event.payment.provider_payment_id)
            if event.payment
            else None
        )
        payment_id = payment.id if payment else None
        grant_id = self._store.create_billing_credit_grant(
            BillingCreditGrantCreate(
                payment_id=payment_id,
                subscription_id=subscription.subscription_id,
                configured_credits=Decimal(str(credits)),
                quantity=1,
                billing_event_id=event.billing_event_id,
            )
        )
        self._store.grant_billing_credit(
            grant_id,
            f"billing:{event.event_id}:subscription-cycle",
        )

    def _revoke_subscription(self, uid: str) -> None:
        if not self._provisioning:
            return
        if self._terminal_plan_key:
            self._provisioning.set_user_plan(uid, self._terminal_plan_key)
        else:
            self._provisioning.unset_user_plan(uid)
        self._logger.info("revoked plan", {"user_id": uid})

    def _revoke_if_current_subscription(self, uid: str, subscription_id: str) -> None:
        current = self._store.get_user_subscription(uid, statuses=["active", "trialing"])
        if not current or current.provider_subscription_id == subscription_id:
            self._revoke_subscription(uid)

    def _re_evaluate_access(self, uid: str, event: BillingEvent) -> None:
        if not self._provisioning or not event.subscription:
            return

        status = event.subscription.status
        status_value = status.value if status else None
        if status_value in ("active", "trialing"):
            offer, _, _, _ = self._offer_for_event(event)
            if offer:
                self._provision_subscription(uid, offer, event)
            else:
                existing = self._store.get_billing_subscription(
                    event.provider,
                    event.subscription.provider_subscription_id,
                )
                if existing and existing.plan:
                    self._provision_subscription(
                        uid,
                        None,
                        event,
                        plan_key_override=existing.plan,
                    )
        elif status_value in ("canceled", "expired", "unpaid", "paused", "incomplete_expired"):
            self._revoke_if_current_subscription(uid, event.subscription.provider_subscription_id)
