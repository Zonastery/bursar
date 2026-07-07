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

    async def handle_event(self, event: BillingEvent) -> BillingEventResult:
        claim = await self._store.claim_billing_event(
            event.provider,
            event.event_id,
            event.event_type,
        )
        if claim.status in ("duplicate",):
            logger.debug("duplicate billing event %s/%s", event.provider, event.event_id)
            return BillingEventResult(handled=True, action="duplicate")

        try:
            result = await self._route_event(event)
            await self._store.complete_billing_event(event.provider, event.event_id)
            return result
        except Exception as exc:
            logger.exception("failed to handle billing event %s/%s", event.provider, event.event_id)
            await self._store.fail_billing_event(event.provider, event.event_id)
            return BillingEventResult(handled=False, error=str(exc))

    async def _route_event(self, event: BillingEvent) -> BillingEventResult:
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
        return await handler(event)

    async def _resolve_user_id(self, event: BillingEvent) -> str | None:
        if event.user_id:
            return event.user_id
        if event.customer and event.customer.provider_customer_id:
            uid = await self._store.get_billing_customer(
                event.provider,
                event.customer.provider_customer_id,
            )
            if uid:
                return uid
        if self._resolve_user and event.customer:
            cid = event.customer.provider_customer_id
            return self._resolve_user(event.provider, cid, event.customer.email)
        return None

    async def _handle_customer_created(self, event: BillingEvent) -> BillingEventResult:
        if event.customer and event.customer.provider_customer_id:
            await self._store.upsert_billing_customer(
                event.provider,
                event.customer.provider_customer_id,
                event.user_id or "",
                event.customer.email,
            )
        return BillingEventResult(handled=True, action="customer_created")

    async def _handle_customer_updated(self, event: BillingEvent) -> BillingEventResult:
        return BillingEventResult(handled=True, action="customer_updated")

    async def _handle_customer_deleted(self, event: BillingEvent) -> BillingEventResult:
        return BillingEventResult(handled=True, action="customer_deleted")

    async def _handle_checkout_completed(self, event: BillingEvent) -> BillingEventResult:
        if event.customer and event.customer.provider_customer_id and event.user_id:
            await self._store.upsert_billing_customer(
                event.provider,
                event.customer.provider_customer_id,
                event.user_id,
                event.customer.email,
            )
        return BillingEventResult(handled=True, action="checkout_recorded")

    async def _handle_subscription_created(self, event: BillingEvent) -> BillingEventResult:
        uid = await self._resolve_user_id(event)
        if not uid:
            return BillingEventResult(handled=False, error="user_not_found")
        if not event.subscription:
            return BillingEventResult(handled=False, error="no_subscription_data")

        refs = event.subscription.refs
        offer = None
        if refs:
            offer = await self._store.resolve_billing_offer(
                event.provider,
                product_id=refs.product_id,
                price_id=refs.price_id,
            )

        await self._store.upsert_billing_subscription(
            BillingSubscriptionState(
                user_id=uid,
                provider=event.provider,
                provider_subscription_id=event.subscription.provider_subscription_id,
                provider_customer_id=(event.customer.provider_customer_id if event.customer else None),
                offer_key=(offer.get("offer_key") if offer else None),
                plan_key=(offer.get("plan_key") if offer else None),
                status=event.subscription.status.value if event.subscription.status else "incomplete",
                current_period_start=event.subscription.period_start,
                current_period_end=event.subscription.period_end,
                cancel_at_period_end=event.subscription.cancel_at_period_end or False,
                interval=event.subscription.interval,
                interval_count=event.subscription.interval_count,
                metadata=event.metadata,
            )
        )

        if self._cm and event.subscription.status in ("active", "trialing"):
            await self._provision_subscription(uid, offer, event)

        return BillingEventResult(
            handled=True,
            action="subscription_created",
            subscription_id=event.subscription.provider_subscription_id,
        )

    async def _handle_subscription_updated(self, event: BillingEvent) -> BillingEventResult:
        uid = await self._resolve_user_id(event)
        if not uid:
            return BillingEventResult(handled=False, error="user_not_found")
        if not event.subscription:
            return BillingEventResult(handled=False, error="no_subscription_data")

        await self._store.upsert_billing_subscription(
            BillingSubscriptionState(
                user_id=uid,
                provider=event.provider,
                provider_subscription_id=event.subscription.provider_subscription_id,
                status=event.subscription.status.value if event.subscription.status else "incomplete",
                cancel_at_period_end=event.subscription.cancel_at_period_end or False,
                current_period_start=event.subscription.period_start,
                current_period_end=event.subscription.period_end,
                metadata=event.metadata,
            )
        )

        if self._cm:
            await self._re_evaluate_access(uid, event)

        return BillingEventResult(handled=True, action="subscription_updated")

    async def _handle_subscription_activated(self, event: BillingEvent) -> BillingEventResult:
        uid = await self._resolve_user_id(event)
        if not uid:
            return BillingEventResult(handled=False, error="user_not_found")
        if not event.subscription:
            return BillingEventResult(handled=False, error="no_subscription_data")

        refs = event.subscription.refs
        offer = None
        if refs:
            offer = await self._store.resolve_billing_offer(
                event.provider,
                product_id=refs.product_id,
                price_id=refs.price_id,
            )

        await self._store.upsert_billing_subscription(
            BillingSubscriptionState(
                user_id=uid,
                provider=event.provider,
                provider_subscription_id=event.subscription.provider_subscription_id,
                offer_key=(offer.get("offer_key") if offer else None),
                plan_key=(offer.get("plan_key") if offer else None),
                status="active",
                current_period_start=event.subscription.period_start,
                current_period_end=event.subscription.period_end,
                interval=event.subscription.interval,
                interval_count=event.subscription.interval_count,
                metadata=event.metadata,
            )
        )

        if self._cm:
            await self._provision_subscription(uid, offer, event)

        return BillingEventResult(handled=True, action="subscription_activated")

    async def _handle_subscription_renewed(self, event: BillingEvent) -> BillingEventResult:
        uid = await self._resolve_user_id(event)
        if not uid:
            return BillingEventResult(handled=False, error="user_not_found")
        if not event.subscription:
            return BillingEventResult(handled=False, error="no_subscription_data")

        refs = event.subscription.refs
        offer = None
        if refs:
            offer = await self._store.resolve_billing_offer(
                event.provider,
                product_id=refs.product_id,
                price_id=refs.price_id,
            )

        await self._store.upsert_billing_subscription(
            BillingSubscriptionState(
                user_id=uid,
                provider=event.provider,
                provider_subscription_id=event.subscription.provider_subscription_id,
                offer_key=(offer.get("offer_key") if offer else None),
                plan_key=(offer.get("plan_key") if offer else None),
                status="active",
                current_period_start=event.subscription.period_start,
                current_period_end=event.subscription.period_end,
                metadata=event.metadata,
            )
        )

        if self._cm:
            plan_key = offer.get("plan_key") if offer else None
            period_start = event.subscription.period_start
            if plan_key:
                await self._cm.set_user_plan(
                    uid,
                    plan_key,
                    plan_assigned_at=(datetime.fromisoformat(period_start) if period_start else None),
                )
                logger.info("renewed plan %s for user %s (anchored to %s)", plan_key, uid, period_start)

        return BillingEventResult(handled=True, action="subscription_renewed")

    async def _handle_subscription_plan_changed(self, event: BillingEvent) -> BillingEventResult:
        uid = await self._resolve_user_id(event)
        if not uid:
            return BillingEventResult(handled=False, error="user_not_found")
        if not event.subscription:
            return BillingEventResult(handled=False, error="no_subscription_data")

        refs = event.subscription.refs
        offer = None
        if refs:
            offer = await self._store.resolve_billing_offer(
                event.provider,
                product_id=refs.product_id,
                price_id=refs.price_id,
            )

        await self._store.upsert_billing_subscription(
            BillingSubscriptionState(
                user_id=uid,
                provider=event.provider,
                provider_subscription_id=event.subscription.provider_subscription_id,
                offer_key=(offer.get("offer_key") if offer else None),
                plan_key=(offer.get("plan_key") if offer else None),
                status=event.subscription.status.value if event.subscription.status else "active",
                metadata=event.metadata,
            )
        )

        if self._cm:
            plan_key = offer.get("plan_key") if offer else None
            if plan_key:
                await self._cm.set_user_plan(
                    uid,
                    plan_key,
                    plan_assigned_at=(
                        datetime.fromisoformat(event.subscription.period_start)
                        if event.subscription and event.subscription.period_start
                        else None
                    ),
                )
                logger.info("plan changed to %s for user %s", plan_key, uid)

        return BillingEventResult(handled=True, action="plan_changed")

    async def _handle_cancellation_scheduled(self, event: BillingEvent) -> BillingEventResult:
        uid = await self._resolve_user_id(event)
        if not uid:
            return BillingEventResult(handled=False, error="user_not_found")
        if not event.subscription:
            return BillingEventResult(handled=False, error="no_subscription_data")

        await self._store.upsert_billing_subscription(
            BillingSubscriptionState(
                user_id=uid,
                provider=event.provider,
                provider_subscription_id=event.subscription.provider_subscription_id,
                cancel_at_period_end=True,
                metadata=event.metadata,
            )
        )
        return BillingEventResult(handled=True, action="cancellation_scheduled")

    async def _handle_cancellation_unscheduled(self, event: BillingEvent) -> BillingEventResult:
        uid = await self._resolve_user_id(event)
        if not uid:
            return BillingEventResult(handled=False, error="user_not_found")
        if not event.subscription:
            return BillingEventResult(handled=False, error="no_subscription_data")

        await self._store.upsert_billing_subscription(
            BillingSubscriptionState(
                user_id=uid,
                provider=event.provider,
                provider_subscription_id=event.subscription.provider_subscription_id,
                cancel_at_period_end=False,
                metadata=event.metadata,
            )
        )
        return BillingEventResult(handled=True, action="cancellation_unscheduled")

    async def _handle_subscription_canceled(self, event: BillingEvent) -> BillingEventResult:
        uid = await self._resolve_user_id(event)
        if not uid:
            return BillingEventResult(handled=False, error="user_not_found")
        if not event.subscription:
            return BillingEventResult(handled=False, error="no_subscription_data")

        await self._store.upsert_billing_subscription(
            BillingSubscriptionState(
                user_id=uid,
                provider=event.provider,
                provider_subscription_id=event.subscription.provider_subscription_id,
                status="canceled",
                metadata=event.metadata,
            )
        )

        if self._cm:
            await self._revoke_subscription(uid)

        return BillingEventResult(handled=True, action="subscription_canceled")

    async def _handle_subscription_expired(self, event: BillingEvent) -> BillingEventResult:
        uid = await self._resolve_user_id(event)
        if not uid:
            return BillingEventResult(handled=False, error="user_not_found")
        if not event.subscription:
            return BillingEventResult(handled=False, error="no_subscription_data")

        await self._store.upsert_billing_subscription(
            BillingSubscriptionState(
                user_id=uid,
                provider=event.provider,
                provider_subscription_id=event.subscription.provider_subscription_id,
                status="expired",
                metadata=event.metadata,
            )
        )

        if self._cm:
            await self._revoke_subscription(uid)

        return BillingEventResult(handled=True, action="subscription_expired")

    async def _handle_subscription_paused(self, event: BillingEvent) -> BillingEventResult:
        uid = await self._resolve_user_id(event)
        if not uid:
            return BillingEventResult(handled=False, error="user_not_found")
        if not event.subscription:
            return BillingEventResult(handled=False, error="no_subscription_data")

        await self._store.upsert_billing_subscription(
            BillingSubscriptionState(
                user_id=uid,
                provider=event.provider,
                provider_subscription_id=event.subscription.provider_subscription_id,
                status="paused",
                metadata=event.metadata,
            )
        )

        if self._cm:
            await self._revoke_subscription(uid)

        return BillingEventResult(handled=True, action="subscription_paused")

    async def _handle_subscription_resumed(self, event: BillingEvent) -> BillingEventResult:
        uid = await self._resolve_user_id(event)
        if not uid:
            return BillingEventResult(handled=False, error="user_not_found")
        if not event.subscription:
            return BillingEventResult(handled=False, error="no_subscription_data")

        await self._store.upsert_billing_subscription(
            BillingSubscriptionState(
                user_id=uid,
                provider=event.provider,
                provider_subscription_id=event.subscription.provider_subscription_id,
                status="active",
                metadata=event.metadata,
            )
        )

        if self._cm:
            await self._re_evaluate_access(uid, event)

        return BillingEventResult(handled=True, action="subscription_resumed")

    async def _handle_trial_will_end(self, event: BillingEvent) -> BillingEventResult:
        return BillingEventResult(handled=True, action="trial_will_end_notified")

    async def _handle_invoice_paid(self, event: BillingEvent) -> BillingEventResult:
        if event.subscription:
            return await self._handle_subscription_renewed(event)
        return BillingEventResult(handled=True, action="invoice_paid")

    async def _handle_payment_succeeded(self, event: BillingEvent) -> BillingEventResult:
        if not event.payment:
            return BillingEventResult(handled=False, error="no_payment_data")

        refs = event.payment.refs
        topup_config = None
        if refs:
            topup_config = await self._store.resolve_credit_topup(
                event.provider,
                product_id=refs.product_id,
                price_id=refs.price_id,
            )

        if topup_config and self._cm:
            uid = await self._resolve_user_id(event)
            if uid:
                credits = await self._store.compute_topup_credits(
                    event.payment.amount_minor,
                    topup_config,
                )
                if credits > 0:
                    await self._cm.add_credits(
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

    async def _handle_payment_failed(self, event: BillingEvent) -> BillingEventResult:
        return BillingEventResult(handled=True, action="payment_failed_recorded")

    async def _handle_refund_created(self, event: BillingEvent) -> BillingEventResult:
        return BillingEventResult(handled=True, action="refund_recorded")

    async def _handle_dispute_created(self, event: BillingEvent) -> BillingEventResult:
        return BillingEventResult(handled=True, action="dispute_recorded")

    async def _handle_dispute_closed(self, event: BillingEvent) -> BillingEventResult:
        return BillingEventResult(handled=True, action="dispute_closed")

    async def _provision_subscription(
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

        await self._cm.set_user_plan(uid, plan_key, plan_assigned_at=period_start)
        logger.info("provisioned plan %s for user %s", plan_key, uid)

    async def _revoke_subscription(self, uid: str) -> None:
        if not self._cm:
            return
        await self._cm.unset_user_plan(uid)
        logger.info("revoked plan for user %s", uid)

    async def _re_evaluate_access(self, uid: str, event: BillingEvent) -> None:
        if not self._cm or not event.subscription:
            return

        status = event.subscription.status
        if status in ("active", "trialing"):
            refs = event.subscription.refs
            offer = None
            if refs:
                offer = await self._store.resolve_billing_offer(
                    event.provider,
                    product_id=refs.product_id,
                    price_id=refs.price_id,
                )
            if offer:
                await self._provision_subscription(uid, offer, event)
        elif status in ("canceled", "expired", "unpaid", "paused", "incomplete_expired"):
            await self._revoke_subscription(uid)
