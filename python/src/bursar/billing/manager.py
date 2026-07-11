from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from bursar.billing.models import (
    BillingConfig,
    BillingEvent,
    BillingEventResult,
    BillingOfferResult,
    BillingSubscriptionInfo,
    BillingSubscriptionState,
    BillingTopupResult,
)
from bursar.billing.store import BillingStore
from bursar.events import CreditEventEmitter
from bursar.manager import CreditManager

logger = logging.getLogger(__name__)

ResolveUserFn = Callable[[str, str | None, str | None], str | None]

IGNORED_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "checkout.expired",
        "invoice.upcoming",
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


class BillingManager:
    def __init__(
        self,
        billing_store: BillingStore,
        credit_manager: CreditManager | None = None,
        emitter: CreditEventEmitter | None = None,
        resolve_user: ResolveUserFn | None = None,
        config: BillingConfig | None = None,
        on_trial_will_end: Callable[[BillingEvent], None] | None = None,
        cancel_prior_providers: bool = True,
    ) -> None:
        self._store = billing_store
        self._cm = credit_manager
        self._emitter = emitter
        self._resolve_user = resolve_user
        self._on_trial_will_end = on_trial_will_end
        self._cancel_prior_providers = cancel_prior_providers
        self._handlers = {
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
        if config is not None:
            self._store.sync_billing_from_config(config)

    def get_user_subscription(
        self,
        user_id: str,
    ) -> BillingSubscriptionState | None:
        return self._store.get_user_subscription(user_id)

    def handle_event(self, event: BillingEvent) -> BillingEventResult:
        claim = self._store.claim_billing_event(
            event.provider,
            event.event_id,
            event.event_type,
        )
        if claim.status == "duplicate":
            logger.debug("duplicate billing event %s/%s", event.provider, event.event_id)
            return BillingEventResult(handled=True, action="duplicate")

        if claim.status in ("retry", "max_retries_exceeded"):
            logger.warning(
                "billing event %s/%s %s — skipping",
                event.provider,
                event.event_id,
                claim.status,
            )
            return BillingEventResult(handled=True, action=claim.status)

        try:
            result = self._route_event(event)
            self._store.complete_billing_event(event.provider, event.event_id)
            return result
        except Exception as exc:
            logger.exception("failed to handle billing event %s/%s", event.provider, event.event_id)
            self._store.fail_billing_event(event.provider, event.event_id)
            return BillingEventResult(handled=False, error=str(exc))

    def _route_event(self, event: BillingEvent) -> BillingEventResult:
        handler = self._handlers.get(event.event_type)
        if handler is None:
            if event.event_type in IGNORED_EVENT_TYPES:
                return BillingEventResult(handled=True, action="ignored")
            logger.warning("unhandled billing event type %s (marking as failed)", event.event_type)
            return BillingEventResult(handled=False, error="unhandled_event_type")
        return handler(event)

    @staticmethod
    def _compute_topup_credits(amount_minor: int, topup_config: BillingTopupResult) -> int:
        credits_per = int(topup_config.credits_per_unit or 1000)
        return (amount_minor * credits_per) // 100

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

    def _offer_for_event(self, event: BillingEvent) -> tuple[BillingOfferResult | None, str | None, str | None]:
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
        if not offer and refs.lookup_key:
            offer = self._resolve_offer_by_lookup(event.provider, refs.lookup_key)
        if not offer:
            return None, None, None
        return offer, offer.offer_key, offer.plan

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
    ) -> BillingSubscriptionState:
        if not event.subscription:
            raise ValueError("no_subscription_data")
        sub = event.subscription
        merger = SubscriptionStateMerge(sub, existing)

        _status = status
        if _status is None and sub.status is not None:
            _status = sub.status.value
        if _status is None and existing is not None:
            _status = existing.status
        if _status is None:
            _status = "incomplete"

        return BillingSubscriptionState(
            user_id=uid,
            provider=event.provider,
            provider_subscription_id=sub.provider_subscription_id,
            provider_customer_id=(event.customer.provider_customer_id if event.customer else None)
            or (existing.provider_customer_id if existing else None),
            offer_key=merger.resolve(offer_key, "offer_key"),
            plan=merger.resolve(plan_key, "plan"),
            status=_status,
            current_period_start=sub.period_start or (existing.current_period_start if existing else None),
            current_period_end=sub.period_end or (existing.current_period_end if existing else None),
            cancel_at_period_end=merger.resolve(cancel_at_period_end, "cancel_at_period_end", False),
            interval=merger.resolve(None, "interval"),
            interval_count=merger.resolve(None, "interval_count"),
            metadata=event.metadata or (existing.metadata if existing else None),
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
        uid = self._resolve_user_id(event)
        if not uid:
            return BillingEventResult(handled=False, error="user_not_found")
        if not event.subscription:
            return BillingEventResult(handled=False, error="no_subscription_data")

        existing = self._store.get_billing_subscription(
            event.provider,
            event.subscription.provider_subscription_id,
        )

        offer = None
        offer_key = None
        plan_key = None
        if resolve_offers:
            offer, offer_key, plan_key = self._offer_for_event(event)

        self._store.upsert_billing_subscription(
            self._subscription_state(
                event,
                uid,
                existing,
                status=status,
                cancel_at_period_end=cancel_at_period_end,
                offer_key=offer_key if offer_key is not None else (existing.offer_key if existing else None),
                plan_key=plan_key if plan_key is not None else (existing.plan if existing else None),
            )
        )

        if self._cm and provision_on_positive:
            self._provision_subscription(
                uid,
                offer,
                event,
                _plan_key=existing.plan if existing and existing.plan else None,
            )

        return BillingEventResult(handled=True, action=action)

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
        if event.customer and event.customer.provider_customer_id:
            uid = self._resolve_user_id(event)
            if uid:
                self._store.upsert_billing_customer(
                    event.provider,
                    event.customer.provider_customer_id,
                    uid,
                    event.customer.email,
                )
        return BillingEventResult(handled=True, action="customer_updated")

    def _handle_customer_deleted(self, event: BillingEvent) -> BillingEventResult:
        if event.customer and event.customer.provider_customer_id:
            uid = self._resolve_user_id(event)
            if uid and self._cm:
                self._revoke_subscription(uid)
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
        st = event.subscription.status.value if event.subscription and event.subscription.status else None
        result = self._apply_subscription_event(
            event,
            status=st,
            cancel_at_period_end=event.subscription.cancel_at_period_end if event.subscription else None,
            action="subscription_created",
            provision_on_positive=st in ("active", "trialing") if st else False,
        )
        if result.handled and event.subscription:
            result.subscription_id = event.subscription.provider_subscription_id
        return result

    def _handle_subscription_updated(self, event: BillingEvent) -> BillingEventResult:
        result = self._apply_subscription_event(
            event,
            status=event.subscription.status.value if event.subscription and event.subscription.status else None,
            cancel_at_period_end=event.subscription.cancel_at_period_end if event.subscription else None,
            action="subscription_updated",
            provision_on_positive=False,
        )
        if result.handled:
            uid = self._resolve_user_id(event)
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
        return self._apply_subscription_event(
            event,
            status="active",
            action="subscription_renewed",
            provision_on_positive=True,
        )

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
        if event.subscription:
            cot = (
                event.subscription.cancel_at_period_end if event.subscription.cancel_at_period_end is not None else True
            )
        else:
            cot = None
        result = self._apply_subscription_event(
            event,
            status="canceled",
            cancel_at_period_end=cot,
            resolve_offers=False,
            action="subscription_canceled",
            provision_on_positive=False,
        )
        if result.handled:
            uid = self._resolve_user_id(event)
            if uid and self._cm:
                self._revoke_subscription(uid)
        return result

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
            uid = self._resolve_user_id(event)
            if uid and self._cm:
                self._revoke_subscription(uid)
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
            uid = self._resolve_user_id(event)
            if uid and self._cm:
                self._revoke_subscription(uid)
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
        if self._on_trial_will_end:
            try:
                self._on_trial_will_end(event)
            except Exception:
                logger.exception(
                    "on_trial_will_end callback failed for event %s/%s",
                    event.provider,
                    event.event_id,
                )
        return BillingEventResult(handled=True, action="trial_will_end_notified")

    def _handle_invoice_paid(self, event: BillingEvent) -> BillingEventResult:
        uid = self._resolve_user_id(event)
        if uid and event.invoice:
            self._store.upsert_billing_invoice(
                event.provider,
                event.invoice.provider_invoice_id,
                provider_subscription_id=event.subscription.provider_subscription_id if event.subscription else None,
                user_id=uid,
                status=event.invoice.status if event.invoice else "paid",
                amount_paid_minor=event.invoice.amount_paid_minor if event.invoice else None,
                amount_due_minor=event.invoice.amount_due_minor if event.invoice else None,
                currency=(event.invoice.currency if event.invoice else None) or "USD",
                period_start=event.invoice.period_start if event.invoice else None,
                period_end=event.invoice.period_end if event.invoice else None,
            )
        if event.subscription:
            return self._handle_subscription_renewed(event)
        return BillingEventResult(handled=True, action="invoice_paid")

    def _handle_payment_succeeded(self, event: BillingEvent) -> BillingEventResult:
        if not event.payment:
            return BillingEventResult(handled=False, error="no_payment_data")

        uid = self._resolve_user_id(event)
        if uid:
            payment_metadata: dict | None = None
            if event.payment.purpose == "credit_topup" and event.payment.refs:
                topup_config = self._store.resolve_credit_topup(
                    event.provider,
                    product_id=event.payment.refs.product_id,
                    price_id=event.payment.refs.price_id,
                )
                if topup_config:
                    payment_metadata = {
                        "credits_per_unit": int(topup_config.credits_per_unit or 1000),
                    }
            self._store.upsert_billing_payment(
                event.provider,
                event.payment.provider_payment_id,
                provider_invoice_id=None,
                user_id=uid,
                amount_minor=event.payment.amount_minor,
                tax_minor=event.payment.tax_minor,
                currency=event.payment.currency,
                purpose=event.payment.purpose,
                metadata=payment_metadata,
            )

        refs = event.payment.refs
        topup_config = None
        if refs:
            topup_config = self._store.resolve_credit_topup(
                event.provider,
                product_id=refs.product_id,
                price_id=refs.price_id,
            )

        if topup_config and self._cm and event.payment.purpose == "credit_topup" and uid:
            min_amount = 0
            max_amount = 10**18
            if event.payment.amount_minor < min_amount or event.payment.amount_minor > max_amount:
                return BillingEventResult(handled=True, action="payment_succeeded")
            credits = self._compute_topup_credits(event.payment.amount_minor, topup_config)
            if credits > 0:
                self._cm.add_credits(
                    uid,
                    Decimal(credits),
                    tx_type="purchase",
                    bucket=topup_config.deposit_to,
                )
                logger.info(
                    "granted %d topup credits to user %s (payment %s)",
                    credits,
                    uid,
                    event.payment.provider_payment_id,
                )

        return BillingEventResult(handled=True, action="payment_succeeded")

    def _handle_payment_failed(self, event: BillingEvent) -> BillingEventResult:
        uid = self._resolve_user_id(event)
        if uid and event.payment:
            self._store.upsert_billing_payment(
                event.provider,
                event.payment.provider_payment_id,
                user_id=uid,
                amount_minor=event.payment.amount_minor,
                currency=event.payment.currency,
                purpose=event.payment.purpose,
            )
        if uid and event.subscription and self._cm:
            existing = self._store.get_billing_subscription(event.provider, event.subscription.provider_subscription_id)
            self._store.upsert_billing_subscription(self._subscription_state(event, uid, existing, status="past_due"))
            self._revoke_subscription(uid)
        return BillingEventResult(handled=True, action="payment_failed_recorded")

    def _handle_refund_created(self, event: BillingEvent) -> BillingEventResult:
        uid = self._resolve_user_id(event)
        if uid and event.refund:
            refund = event.refund
            self._store.upsert_billing_refund(
                event.provider,
                refund.provider_refund_id,
                provider_payment_id=refund.provider_payment_id,
                user_id=uid,
                amount_minor=refund.amount_minor,
                currency=refund.currency,
                reason=refund.reason,
            )
            if refund.provider_payment_id and self._cm:
                payment = self._store.get_billing_payment(event.provider, refund.provider_payment_id)
                if payment and payment.get("purpose") == "credit_topup":
                    pay_meta = payment.get("metadata") or {}
                    credits_per_unit = pay_meta.get("credits_per_unit")
                    if credits_per_unit is None:
                        logger.warning(
                            "cannot claw back credits for refund %s: no credits_per_unit in payment metadata",
                            refund.provider_refund_id,
                        )
                        return BillingEventResult(handled=True, action="refund_recorded_no_clawback")
                    credits_per_unit = int(credits_per_unit)
                    credits = self._compute_topup_credits(
                        refund.amount_minor,
                        BillingTopupResult(topup_key="", credits_per_unit=Decimal(credits_per_unit)),
                    )
                    if credits > 0:
                        self._cm.deduct_credits(
                            uid,
                            Decimal(str(credits)),
                            tx_type="refund_clawback",
                            bucket="purchased",
                        )
                        logger.info(
                            "clawed back %d credits from user %s for refund %s",
                            credits,
                            uid,
                            refund.provider_refund_id,
                        )
        return BillingEventResult(handled=True, action="refund_recorded")

    def _handle_dispute_created(self, event: BillingEvent) -> BillingEventResult:
        uid = self._resolve_user_id(event)
        if uid and event.dispute:
            self._store.upsert_billing_dispute(
                event.provider,
                event.dispute.provider_dispute_id,
                provider_payment_id=event.dispute.provider_payment_id,
                user_id=uid,
                status="needs_response",
            )
        return BillingEventResult(handled=True, action="dispute_recorded")

    def _handle_dispute_closed(self, event: BillingEvent) -> BillingEventResult:
        uid = self._resolve_user_id(event)
        if uid and event.dispute:
            self._store.upsert_billing_dispute(
                event.provider,
                event.dispute.provider_dispute_id,
                user_id=uid,
                status="closed",
            )
        return BillingEventResult(handled=True, action="dispute_closed")

    def _provision_subscription(
        self,
        uid: str,
        offer: BillingOfferResult | None,
        event: BillingEvent,
        *,
        _plan_key: str | None = None,
    ) -> None:
        if not self._cm:
            return

        plan_key = _plan_key or (offer.plan if offer else None)
        if not plan_key:
            return

        period_start = None
        if event.subscription:
            ps = event.subscription.period_start
            if ps:
                try:
                    period_start = datetime.fromisoformat(ps)
                except (ValueError, TypeError):
                    logger.warning("invalid period_start timestamp %r for user %s, using now()", ps, uid)
                    period_start = None

        self._cm.set_user_plan(uid, plan_key, plan_assigned_at=period_start)

        if self._cancel_prior_providers and event.provider:
            result = self._store.deactivate_other_provider_subscriptions(uid, event.provider)
            count = result.get("deactivated_count", 0) or 0
            if count:
                logger.info("deactivated %d prior provider subscription(s) for user %s", count, uid)

        g = offer.grant if offer else None
        if g and g.mode == "cycle_grant" and self._cm:
            cycle_credits = g.credits
            if cycle_credits:
                cycle_tier = g.bucket or "purchased"
                replace_prior = g.replace_prior
                if replace_prior:
                    self._cm.revoke_credits_by_tx_type(uid, "cycle_grant")
                self._cm.add_credits(
                    uid,
                    Decimal(str(cycle_credits)),
                    tx_type="cycle_grant",
                    bucket=cycle_tier,
                )
                logger.info("granted %d cycle credits to user %s (tier=%s)", cycle_credits, uid, cycle_tier)

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
                if existing and existing.plan:
                    self._provision_subscription(uid, None, event, _plan_key=existing.plan)
        elif status_value in ("canceled", "expired", "unpaid", "paused", "incomplete_expired"):
            self._revoke_subscription(uid)
