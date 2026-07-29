"""Provider-neutral auto-recharge orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal
from uuid import uuid4

from bursar.billing.policy_window import resolve_auto_recharge_window
from bursar.billing.types import (
    BillingAutoRechargeProfile,
    BillingAutoRechargeStatus,
    BillingTopupResult,
)
from bursar.config import (
    CustomObjectReference,
    DodoProductReference,
    StripePriceReference,
    TopupOffer,
    load_config_from_dict,
)
from bursar.providers.types import (
    PaymentMethodInfo,
    PaymentProvider,
    SavedPaymentChargeParams,
    SavedPaymentChargeQuote,
    SavedPaymentChargeResult,
)

AutoRechargeOutcome = Literal[
    "not_configured",
    "disabled",
    "above_threshold",
    "already_processing",
    "limit_reached",
    "submitted",
    "action_required",
    "failed",
]


@dataclass(frozen=True, slots=True)
class AutoRechargeProcessResult:
    outcome: AutoRechargeOutcome
    charge: SavedPaymentChargeResult | None = None


@dataclass(frozen=True, slots=True)
class _ResolvedAutoRechargePolicy:
    threshold: Decimal
    topup_key: str
    topup_id: str
    quantity: int
    max_charges_per_window: int
    window_unit: Literal["second", "minute", "hour", "day", "week", "month", "year"]
    window_count: int
    window_anchor: Literal["calendar", "rolling"]
    window_timezone: str
    window_start: str
    window_end: str
    window_days: float
    product_id: str


class AutoRechargeService:
    def __init__(self, billing: Any) -> None:
        self._billing = billing

    def _policy(self, provider: PaymentProvider) -> _ResolvedAutoRechargePolicy | None:
        raw = self._billing.get_active_bursar_config()
        if raw is None:
            return None
        config = load_config_from_dict(raw)
        policy = config.commerce.auto_recharge
        if policy is None:
            return None

        topup_key = policy.eligible_topups[0]
        topup = config.commerce.offers.get(topup_key)
        if not isinstance(topup, TopupOffer):
            return None
        reference = topup.providers.get(provider.provider)
        if reference is None:
            return None

        resolved_topup: BillingTopupResult | None
        if isinstance(reference, StripePriceReference):
            product_id = reference.price_id
            resolved_topup = self._billing.resolve_topup(provider.provider, None, product_id)
        elif isinstance(reference, DodoProductReference):
            product_id = reference.product_id
            resolved_topup = self._billing.resolve_topup(provider.provider, product_id, None)
        elif isinstance(reference, CustomObjectReference):
            product_id = reference.external_id
            resolved_topup = self._billing.resolve_topup_by_lookup(provider.provider, product_id)
        else:
            return None
        if resolved_topup is None:
            return None

        period = resolve_auto_recharge_window(policy.limits.window)
        return _ResolvedAutoRechargePolicy(
            threshold=Decimal(policy.balance_below.default),
            topup_key=topup_key,
            topup_id=resolved_topup.topup_id,
            quantity=policy.quantity.default,
            max_charges_per_window=policy.limits.max_purchases,
            window_unit=period.unit,
            window_count=period.count,
            window_anchor=period.anchor,
            window_timezone=period.timezone,
            window_start=period.start,
            window_end=period.end,
            window_days=period.duration_days,
            product_id=product_id,
        )

    async def _payment_method(
        self,
        user_id: str,
        provider: PaymentProvider,
    ) -> tuple[str, PaymentMethodInfo] | None:
        customer = self._billing.get_customer_by_user_id(user_id, provider.provider)
        if customer is None:
            return None
        methods = await provider.list_payment_methods(customer.provider_customer_id)
        method = await provider.get_default_payment_method(customer.provider_customer_id)
        if method is None:
            method = next((candidate for candidate in methods if candidate.is_default), None)
        if method is None and len(methods) == 1:
            method = methods[0]
        return (customer.provider_customer_id, method) if method is not None else None

    async def quote(
        self,
        user_id: str,
        provider: PaymentProvider,
    ) -> SavedPaymentChargeQuote | None:
        policy = self._policy(provider)
        if policy is None:
            return None
        payment = await self._payment_method(user_id, provider)
        if payment is None:
            return None
        customer_id, method = payment
        try:
            return await provider.preview_saved_payment_charge(
                SavedPaymentChargeParams(
                    customer_id=customer_id,
                    payment_method_id=method.id,
                    product_id=policy.product_id,
                    quantity=policy.quantity,
                    metadata={},
                    idempotency_key="auto-recharge-preview",
                )
            )
        except NotImplementedError:
            return None

    async def get_status(
        self,
        user_id: str,
        provider: PaymentProvider,
    ) -> BillingAutoRechargeStatus | None:
        policy = self._policy(provider)
        if policy is None:
            return None
        profile = self._billing.get_auto_recharge_profile(user_id)
        payment = await self._payment_method(user_id, provider) if profile is not None and profile.enabled else None
        quote = await self.quote(user_id, provider)
        method = payment[1] if payment is not None else None
        return BillingAutoRechargeStatus(
            enabled=bool(profile and profile.enabled),
            state=profile.state if profile and profile.enabled else "disabled",
            threshold_credits=policy.threshold,
            topup_key=policy.topup_key,
            quantity=policy.quantity,
            max_recharges=policy.max_charges_per_window,
            window_days=policy.window_days,
            window_start=policy.window_start,
            window_end=policy.window_end,
            recharges_in_window=self._billing.count_auto_recharge_attempts(
                user_id,
                policy.window_start,
            ),
            payment_method_id=method.id if method is not None else None,
            payment_method_last4=method.last4 if method is not None else None,
            payment_method_brand=method.brand if method is not None else None,
            suspended_reason=("auto_recharge_paused" if profile and profile.state == "paused" else None),
            pending_attempt_id=None,
            quote_amount_minor=quote.amount_minor if quote is not None else None,
            quote_currency=quote.currency if quote is not None else None,
        )

    async def enable(
        self,
        user_id: str,
        provider: PaymentProvider,
        *,
        balance: Decimal | int,
        return_url: str,
        consent_reference: str | None = None,
    ) -> BillingAutoRechargeStatus | None:
        policy = self._policy(provider)
        if policy is None:
            raise ValueError("auto_recharge_not_configured")
        payment = await self._payment_method(user_id, provider)
        if payment is None:
            raise ValueError("payment_method_required")

        self._billing.upsert_auto_recharge_profile(
            BillingAutoRechargeProfile(
                user_id=user_id,
                enabled=True,
                state="active",
                armed=True,
                provider=provider.provider,
                topup_id=policy.topup_id,
                quantity=policy.quantity,
                threshold=policy.threshold,
                max_charges_per_window=policy.max_charges_per_window,
                window_unit=policy.window_unit,
                window_count=policy.window_count,
                window_anchor=policy.window_anchor,
                window_timezone=policy.window_timezone,
            )
        )
        await self.process_if_needed(
            user_id,
            provider,
            balance=balance,
            return_url=return_url,
        )
        return await self.get_status(user_id, provider)

    def disable(self, user_id: str) -> None:
        profile = self._billing.get_auto_recharge_profile(user_id)
        if profile is None:
            return
        self._billing.upsert_auto_recharge_profile(
            profile.model_copy(
                update={
                    "enabled": False,
                    "state": "disabled",
                    "armed": True,
                }
            )
        )

    async def retry(
        self,
        user_id: str,
        provider: PaymentProvider,
        *,
        balance: Decimal | int,
        return_url: str,
    ) -> AutoRechargeProcessResult:
        profile = self._billing.get_auto_recharge_profile(user_id)
        if profile is None or not profile.enabled:
            raise ValueError("auto_recharge_disabled")
        self._billing.upsert_auto_recharge_profile(profile.model_copy(update={"state": "active", "armed": True}))
        return await self.process_if_needed(
            user_id,
            provider,
            balance=balance,
            return_url=return_url,
        )

    async def process_if_needed(
        self,
        user_id: str,
        provider: PaymentProvider,
        *,
        balance: Decimal | int,
        return_url: str,
    ) -> AutoRechargeProcessResult:
        policy = self._policy(provider)
        if policy is None:
            return AutoRechargeProcessResult(outcome="not_configured")
        profile = self._billing.get_auto_recharge_profile(user_id)
        if profile is None or not profile.enabled or profile.state != "active":
            return AutoRechargeProcessResult(outcome="disabled")
        if Decimal(balance) >= policy.threshold:
            if not profile.armed:
                self._billing.upsert_auto_recharge_profile(profile.model_copy(update={"armed": True}))
            return AutoRechargeProcessResult(outcome="above_threshold")

        payment = await self._payment_method(user_id, provider)
        if payment is None:
            return AutoRechargeProcessResult(outcome="failed")
        customer_id, method = payment
        attempt = self._billing.claim_auto_recharge_attempt(
            user_id,
            f"auto-recharge:{user_id}:{uuid4()}",
        )
        if attempt is None:
            return AutoRechargeProcessResult(outcome="limit_reached")

        charge = await provider.charge_saved_payment_method(
            SavedPaymentChargeParams(
                customer_id=customer_id,
                payment_method_id=method.id,
                product_id=policy.product_id,
                quantity=policy.quantity,
                return_url=return_url,
                idempotency_key=attempt.idempotency_key,
                metadata={
                    "auto_recharge_attempt_id": attempt.id,
                    "purpose": "credit_topup",
                    "userId": user_id,
                },
            )
        )
        if charge.status == "requires_customer_action":
            self._billing.update_auto_recharge_attempt(
                attempt.id,
                "submitted",
                charge.provider_payment_id,
            )
            self._billing.upsert_auto_recharge_profile(profile.model_copy(update={"state": "paused"}))
            return AutoRechargeProcessResult(outcome="action_required", charge=charge)

        if charge.status in {"succeeded", "processing"}:
            self._billing.update_auto_recharge_attempt(
                attempt.id,
                "processing",
                charge.provider_payment_id,
            )
            return AutoRechargeProcessResult(outcome="submitted", charge=charge)

        self._billing.update_auto_recharge_attempt(
            attempt.id,
            "failed",
            charge.provider_payment_id,
            "payment_failed",
        )
        self._billing.upsert_auto_recharge_profile(profile.model_copy(update={"state": "paused"}))
        return AutoRechargeProcessResult(outcome="failed", charge=charge)
