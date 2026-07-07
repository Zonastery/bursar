from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal

from bursar.billing.models import (
    BillingConfig,
    BillingEvent,
    BillingEventResult,
    BillingSubscriptionState,
)
from bursar.billing.store import BillingStore
from bursar.events import CreditEventEmitter
from bursar.manager import CreditManager

logger = logging.getLogger(__name__)

ResolveUserFn = Callable[[str, str | None, str | None], str | None]


class BillingManager:
    def __init__(
        self,
        billing_store: BillingStore,
        credit_manager: CreditManager | None = None,
        emitter: CreditEventEmitter | None = None,
        resolve_user: ResolveUserFn | None = None,
        config: BillingConfig | None = None,
    ) -> None:
        self._store = billing_store
        self._cm = credit_manager
        self._emitter = emitter
        self._resolve_user = resolve_user
        if config is not None:
            self._store.sync_billing_from_config(config)

    def handle_event(self, event: BillingEvent) -> BillingEventResult:
        claim = self._store.claim_billing_event(
            event.provider,
            event.event_id,
            event.event_type,
        )
        if claim.status == "duplicate":
            logger.debug("duplicate billing event %s/%s", event.provider, event.event_id)
            return BillingEventResult(handled=True, action="duplicate")

        try:
            result = self._route_event(event)
            self._store.complete_billing_event(event.provider, event.event_id)
            return result
        except Exception as exc:
            logger.exception("failed to handle billing event %s/%s", event.provider, event.event_id)
            self._store.fail_billing_event(event.provider, event.event_id)
            return BillingEventResult(handled=False, error=str(exc))

    def _route_event(self, event: BillingEvent) -> BillingEventResult:
        handlers = {
            "customer.created": self._handle_customer_created,
            "customer.updated": self._handle_customer_updated,
            "customer.deleted": self._handle_customer_deleted,
            "checkout.completed": self._handle_checkout_completed,
            "subscription.created": self._handle_subscription_created,
            "subscription.updated": self._handle_subscription_updated,
            "subscription.activated": self._handle_subscription_activated,
            "subscription.renewed": self._handle_subscription_renewed,
            "subscription.plan_changed": self._handle_subscription_plan_changed,
            "subscription.cancellation_scheduled": self._handle_cancellation_scheduled,
            "subscription.cancellation_unscheduled": self._handle_cancellation_unscheduled,
            "subscription.canceled": self._handle_subscription_canceled,
            "subscription.expired": self._handle_subscription_expired,
            "subscription.paused": self._handle_subscription_paused,
            "subscription.resumed": self._handle_subscription_resumed,
            "subscription.trial_will_end": self._handle_trial_will_end,
            "invoice.paid": self._handle_invoice_paid,
            "payment.succeeded": self._handle_payment_succeeded,
            "payment.failed": self._handle_payment_failed,
            "refund.created": self._handle_refund_created,
            "dispute.created": self._handle_dispute_created,
            "dispute.closed": self._handle_dispute_closed,
        }
        handler = handlers.get(event.event_type)
        if handler is None:
            logger.debug("unhandled billing event type %s", event.event_type)
            return BillingEventResult(handled=True, action="ignored")
        return handler(event)

    def _resolve_user_id(self, event: BillingEvent) -> str | None:
        if event.user_id:
            return event.user_id
        if event.customer and event.customer.provider_customer_id:
            uid = self._store.get_billing_customer(
                event.provider,
                event.customer.provider_customer_id,
            )
            if uid:
                return uid
        if self._resolve_user and event.customer:
            return self._resolve_user(
                event.provider,
                event.customer.provider_customer_id,
                event.customer.email,
            )
        return None

    def _offer_for_event(self, event: BillingEvent) -> tuple[dict | None, str | None, str | None]:
        if not event.subscription:
            return None, None, None
        refs = event.subscription.refs
        if not refs:
            return None, None, None
        offer = self._store.resolve_billing_offer(
            event.provider,
            product_id=refs.product_id,
            price_id=refs.price_id,
        )
        if not offer:
            return None, None, None
        return offer, offer.get("offer_key"), offer.get("plan_key")

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
    ) -> BillingSubscriptionState:
        if not event.subscription:
            raise ValueError("no_subscription_data")
        sub = event.subscription
        return BillingSubscriptionState(
            user_id=uid,
            provider=event.provider,
            provider_subscription_id=sub.provider_subscription_id,
            provider_customer_id=(
                event.customer.provider_customer_id
                if event.customer and event.customer.provider_customer_id
                else (existing.provider_customer_id if existing else None)
            ),
            offer_key=offer_key if offer_key is not None else (existing.offer_key if existing else None),
            plan_key=plan_key if plan_key is not None else (existing.plan_key if existing else None),
            status=status or (sub.status.value if sub.status else (existing.status if existing else "incomplete")),
            current_period_start=sub.period_start if sub.period_start is not None else (existing.current_period_start if existing else None),
            current_period_end=sub.period_end if sub.period_end is not None else (existing.current_period_end if existing else None),
            cancel_at_period_end=(
                cancel_at_period_end
                if cancel_at_period_end is not None
                else (
                    sub.cancel_at_period_end
                    if sub.cancel_at_period_end is not None
                    else (existing.cancel_at_period_end if existing else False)
                )
            ),
            interval=sub.interval if sub.interval is not None else (existing.interval if existing else None),
            interval_count=sub.interval_count if sub.interval_count is not None else (existing.interval_count if existing else None),
            metadata=event.metadata if event.metadata is not None else (existing.metadata if existing else None),
        )

    def _handle_customer_created(self, event: BillingEvent) -> BillingEventResult:
        if event.customer and event.customer.provider_customer_id:
            uid = self._resolve_user_id(event)
            if uid:
                self._store.upsert_billing_customer(
                    event.provider,
                    event.customer.provider_customer_id,
                    uid,
                    event.customer.email,
                )
        return BillingEventResult(handled=True, action="customer_created")

    def _handle_customer_updated(self, event: BillingEvent) -> BillingEventResult:
        return BillingEventResult(handled=True, action="customer_updated")

    def _handle_customer_deleted(self, event: BillingEvent) -> BillingEventResult:
        return BillingEventResult(handled=True, action="customer_deleted")

    def _handle_checkout_completed(self, event: BillingEvent) -> BillingEventResult:
        if event.customer and event.customer.provider_customer_id:
            uid = self._resolve_user_id(event)
            if uid:
                self._store.upsert_billing_customer(
                    event.provider,
                    event.customer.provider_customer_id,
                    uid,
                    event.customer.email,
                )
        return BillingEventResult(handled=True, action="checkout_recorded")

    def _handle_subscription_created(self, event: BillingEvent) -> BillingEventResult:
        uid = self._resolve_user_id(event)
        if not uid:
            return BillingEventResult(handled=False, error="user_not_found")
        if not event.subscription:
            return BillingEventResult(handled=False, error="no_subscription_data")

        offer, offer_key, plan_key = self._offer_for_event(event)
        existing = self._store.get_billing_subscription(event.provider, event.subscription.provider_subscription_id)
        self._store.upsert_billing_subscription(
            self._subscription_state(
                event,
                uid,
                existing,
                status=event.subscription.status.value if event.subscription.status else None,
                cancel_at_period_end=event.subscription.cancel_at_period_end,
                offer_key=offer_key,
                plan_key=plan_key,
            )
        )

        if self._cm and event.subscription.status in ("active", "trialing"):
            self._provision_subscription(
                uid,
                offer or ({"plan_key": existing.plan_key} if existing and existing.plan_key else None),
                event,
            )

        return BillingEventResult(
            handled=True,
            action="subscription_created",
            subscription_id=event.subscription.provider_subscription_id,
        )

    def _handle_subscription_updated(self, event: BillingEvent) -> BillingEventResult:
        uid = self._resolve_user_id(event)
        if not uid:
            return BillingEventResult(handled=False, error="user_not_found")
        if not event.subscription:
            return BillingEventResult(handled=False, error="no_subscription_data")

        existing = self._store.get_billing_subscription(event.provider, event.subscription.provider_subscription_id)
        offer, offer_key, plan_key = self._offer_for_event(event)
        self._store.upsert_billing_subscription(
            self._subscription_state(
                event,
                uid,
                existing,
                status=event.subscription.status.value if event.subscription.status else None,
                cancel_at_period_end=event.subscription.cancel_at_period_end,
                offer_key=offer_key if offer_key is not None else (existing.offer_key if existing else None),
                plan_key=plan_key if plan_key is not None else (existing.plan_key if existing else None),
            )
        )

        if self._cm:
            self._re_evaluate_access(uid, event)

        return BillingEventResult(handled=True, action="subscription_updated")

    def _handle_subscription_activated(self, event: BillingEvent) -> BillingEventResult:
        uid = self._resolve_user_id(event)
        if not uid:
            return BillingEventResult(handled=False, error="user_not_found")
        if not event.subscription:
            return BillingEventResult(handled=False, error="no_subscription_data")

        existing = self._store.get_billing_subscription(event.provider, event.subscription.provider_subscription_id)
        offer, offer_key, plan_key = self._offer_for_event(event)
        self._store.upsert_billing_subscription(
            self._subscription_state(
                event,
                uid,
                existing,
                status="active",
                offer_key=offer_key if offer_key is not None else (existing.offer_key if existing else None),
                plan_key=plan_key if plan_key is not None else (existing.plan_key if existing else None),
            )
        )

        if self._cm:
            self._provision_subscription(
                uid,
                offer or ({"plan_key": existing.plan_key} if existing and existing.plan_key else None),
                event,
            )

        return BillingEventResult(handled=True, action="subscription_activated")

    def _handle_subscription_renewed(self, event: BillingEvent) -> BillingEventResult:
        uid = self._resolve_user_id(event)
        if not uid:
            return BillingEventResult(handled=False, error="user_not_found")
        if not event.subscription:
            return BillingEventResult(handled=False, error="no_subscription_data")

        existing = self._store.get_billing_subscription(event.provider, event.subscription.provider_subscription_id)
        offer, offer_key, plan_key = self._offer_for_event(event)
        self._store.upsert_billing_subscription(
            self._subscription_state(
                event,
                uid,
                existing,
                status="active",
                offer_key=offer_key if offer_key is not None else (existing.offer_key if existing else None),
                plan_key=plan_key if plan_key is not None else (existing.plan_key if existing else None),
            )
        )

        resolved_plan_key = plan_key if plan_key is not None else (existing.plan_key if existing else None)
        if self._cm and resolved_plan_key:
            period_start = event.subscription.period_start
            self._cm.set_user_plan(
                uid,
                resolved_plan_key,
                plan_assigned_at=(datetime.fromisoformat(period_start) if period_start else None),
            )
            logger.info("renewed plan %s for user %s (anchored to %s)", resolved_plan_key, uid, period_start)

        return BillingEventResult(handled=True, action="subscription_renewed")

    def _handle_subscription_plan_changed(self, event: BillingEvent) -> BillingEventResult:
        uid = self._resolve_user_id(event)
        if not uid:
            return BillingEventResult(handled=False, error="user_not_found")
        if not event.subscription:
            return BillingEventResult(handled=False, error="no_subscription_data")

        existing = self._store.get_billing_subscription(event.provider, event.subscription.provider_subscription_id)
        offer, offer_key, plan_key = self._offer_for_event(event)
        self._store.upsert_billing_subscription(
            self._subscription_state(
                event,
                uid,
                existing,
                status=event.subscription.status.value if event.subscription.status else "active",
                offer_key=offer_key if offer_key is not None else (existing.offer_key if existing else None),
                plan_key=plan_key if plan_key is not None else (existing.plan_key if existing else None),
            )
        )

        resolved_plan_key = plan_key if plan_key is not None else (existing.plan_key if existing else None)
        if self._cm and resolved_plan_key:
            self._cm.set_user_plan(
                uid,
                resolved_plan_key,
                plan_assigned_at=(
                    datetime.fromisoformat(event.subscription.period_start)
                    if event.subscription.period_start
                    else None
                ),
            )
            logger.info("plan changed to %s for user %s", resolved_plan_key, uid)

        return BillingEventResult(handled=True, action="plan_changed")

    def _handle_cancellation_scheduled(self, event: BillingEvent) -> BillingEventResult:
        uid = self._resolve_user_id(event)
        if not uid:
            return BillingEventResult(handled=False, error="user_not_found")
        if not event.subscription:
            return BillingEventResult(handled=False, error="no_subscription_data")

        existing = self._store.get_billing_subscription(event.provider, event.subscription.provider_subscription_id)
        self._store.upsert_billing_subscription(
            self._subscription_state(
                event,
                uid,
                existing,
                status=existing.status if existing else (event.subscription.status.value if event.subscription.status else "active"),
                cancel_at_period_end=True,
            )
        )
        return BillingEventResult(handled=True, action="cancellation_scheduled")

    def _handle_cancellation_unscheduled(self, event: BillingEvent) -> BillingEventResult:
        uid = self._resolve_user_id(event)
        if not uid:
            return BillingEventResult(handled=False, error="user_not_found")
        if not event.subscription:
            return BillingEventResult(handled=False, error="no_subscription_data")

        existing = self._store.get_billing_subscription(event.provider, event.subscription.provider_subscription_id)
        self._store.upsert_billing_subscription(
            self._subscription_state(
                event,
                uid,
                existing,
                status=existing.status if existing else (event.subscription.status.value if event.subscription.status else "active"),
                cancel_at_period_end=False,
            )
        )
        return BillingEventResult(handled=True, action="cancellation_unscheduled")

    def _handle_subscription_canceled(self, event: BillingEvent) -> BillingEventResult:
        uid = self._resolve_user_id(event)
        if not uid:
            return BillingEventResult(handled=False, error="user_not_found")
        if not event.subscription:
            return BillingEventResult(handled=False, error="no_subscription_data")

        existing = self._store.get_billing_subscription(event.provider, event.subscription.provider_subscription_id)
        self._store.upsert_billing_subscription(
            self._subscription_state(
                event,
                uid,
                existing,
                status="canceled",
                cancel_at_period_end=True if event.subscription.cancel_at_period_end is None else event.subscription.cancel_at_period_end,
            )
        )

        if self._cm:
            self._revoke_subscription(uid)

        return BillingEventResult(handled=True, action="subscription_canceled")

    def _handle_subscription_expired(self, event: BillingEvent) -> BillingEventResult:
        uid = self._resolve_user_id(event)
        if not uid:
            return BillingEventResult(handled=False, error="user_not_found")
        if not event.subscription:
            return BillingEventResult(handled=False, error="no_subscription_data")

        existing = self._store.get_billing_subscription(event.provider, event.subscription.provider_subscription_id)
        self._store.upsert_billing_subscription(
            self._subscription_state(
                event,
                uid,
                existing,
                status="expired",
                cancel_at_period_end=True if event.subscription.cancel_at_period_end is None else event.subscription.cancel_at_period_end,
            )
        )

        if self._cm:
            self._revoke_subscription(uid)

        return BillingEventResult(handled=True, action="subscription_expired")

    def _handle_subscription_paused(self, event: BillingEvent) -> BillingEventResult:
        uid = self._resolve_user_id(event)
        if not uid:
            return BillingEventResult(handled=False, error="user_not_found")
        if not event.subscription:
            return BillingEventResult(handled=False, error="no_subscription_data")

        existing = self._store.get_billing_subscription(event.provider, event.subscription.provider_subscription_id)
        self._store.upsert_billing_subscription(
            self._subscription_state(
                event,
                uid,
                existing,
                status="paused",
                cancel_at_period_end=existing.cancel_at_period_end if existing else False,
            )
        )

        if self._cm:
            self._revoke_subscription(uid)

        return BillingEventResult(handled=True, action="subscription_paused")

    def _handle_subscription_resumed(self, event: BillingEvent) -> BillingEventResult:
        uid = self._resolve_user_id(event)
        if not uid:
            return BillingEventResult(handled=False, error="user_not_found")
        if not event.subscription:
            return BillingEventResult(handled=False, error="no_subscription_data")

        existing = self._store.get_billing_subscription(event.provider, event.subscription.provider_subscription_id)
        offer, offer_key, plan_key = self._offer_for_event(event)
        self._store.upsert_billing_subscription(
            self._subscription_state(
                event,
                uid,
                existing,
                status="active",
                cancel_at_period_end=False,
                offer_key=offer_key if offer_key is not None else (existing.offer_key if existing else None),
                plan_key=plan_key if plan_key is not None else (existing.plan_key if existing else None),
            )
        )

        if self._cm:
            self._re_evaluate_access(uid, event)

        return BillingEventResult(handled=True, action="subscription_resumed")

    def _handle_trial_will_end(self, event: BillingEvent) -> BillingEventResult:
        return BillingEventResult(handled=True, action="trial_will_end_notified")

    def _handle_invoice_paid(self, event: BillingEvent) -> BillingEventResult:
        if event.subscription:
            return self._handle_subscription_renewed(event)
        return BillingEventResult(handled=True, action="invoice_paid")

    def _handle_payment_succeeded(self, event: BillingEvent) -> BillingEventResult:
        if not event.payment:
            return BillingEventResult(handled=False, error="no_payment_data")

        refs = event.payment.refs
        topup_config = None
        if refs:
            topup_config = self._store.resolve_credit_topup(
                event.provider,
                product_id=refs.product_id,
                price_id=refs.price_id,
            )

        if topup_config and self._cm and event.payment.purpose == "credit_topup":
            uid = self._resolve_user_id(event)
            if uid:
                if event.payment.currency.upper() != str(topup_config.get("currency", "USD")).upper():
                    return BillingEventResult(handled=True, action="payment_succeeded")
                min_amount = int(topup_config.get("min_amount_minor", 0))
                max_amount = int(topup_config.get("max_amount_minor", 10**18))
                if event.payment.amount_minor < min_amount or event.payment.amount_minor > max_amount:
                    return BillingEventResult(handled=True, action="payment_succeeded")
                credits = self._store.compute_topup_credits(event.payment.amount_minor, topup_config)
                if credits > 0:
                    self._cm.add_credits(
                        uid,
                        Decimal(credits),
                        tx_type="purchase",
                        tier=topup_config.get("tier", "purchased"),
                    )
                    logger.info(
                        "granted %d topup credits to user %s (payment %s)",
                        credits,
                        uid,
                        event.payment.provider_payment_id,
                    )

        return BillingEventResult(handled=True, action="payment_succeeded")

    def _handle_payment_failed(self, event: BillingEvent) -> BillingEventResult:
        return BillingEventResult(handled=True, action="payment_failed_recorded")

    def _handle_refund_created(self, event: BillingEvent) -> BillingEventResult:
        return BillingEventResult(handled=True, action="refund_recorded")

    def _handle_dispute_created(self, event: BillingEvent) -> BillingEventResult:
        return BillingEventResult(handled=True, action="dispute_recorded")

    def _handle_dispute_closed(self, event: BillingEvent) -> BillingEventResult:
        return BillingEventResult(handled=True, action="dispute_closed")

    def _provision_subscription(
        self,
        uid: str,
        offer: dict | None,
        event: BillingEvent,
    ) -> None:
        if not offer or not self._cm:
            return

        plan_key = offer.get("plan_key")
        if not plan_key:
            return

        period_start = None
        if event.subscription:
            ps = event.subscription.period_start
            period_start = datetime.fromisoformat(ps) if ps else None

        self._cm.set_user_plan(uid, plan_key, plan_assigned_at=period_start)
        logger.info("provisioned plan %s for user %s", plan_key, uid)

    def _revoke_subscription(self, uid: str) -> None:
        if not self._cm:
            return
        self._cm.unset_user_plan(uid)
        logger.info("revoked plan for user %s", uid)

    def _re_evaluate_access(self, uid: str, event: BillingEvent) -> None:
        if not self._cm or not event.subscription:
            return

        status = event.subscription.status
        status_value = status.value if status else None
        if status_value in ("active", "trialing"):
            offer, _, _ = self._offer_for_event(event)
            if offer:
                self._provision_subscription(uid, offer, event)
            else:
                existing = self._store.get_billing_subscription(
                    event.provider,
                    event.subscription.provider_subscription_id,
                )
                if existing and existing.plan_key:
                    self._provision_subscription(uid, {"plan_key": existing.plan_key}, event)
        elif status_value in ("canceled", "expired", "unpaid", "paused", "incomplete_expired"):
            self._revoke_subscription(uid)
