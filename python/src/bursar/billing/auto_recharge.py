"""Provider-neutral auto-recharge orchestration.

Applications invoke this service after a credit deduction.  It owns policy
resolution, consent snapshots, idempotency claims, and payment-provider calls;
applications do not access Bursar auto-recharge tables directly.
"""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any

from bursar.billing.models import (
    BillingAutoRechargeConfig,
    BillingAutoRechargePolicy,
    BillingAutoRechargeProfile,
    BillingConfig,
)
from bursar.providers.types import PaymentProvider, SavedPaymentChargeParams


class AutoRechargeService:
    def __init__(self, billing: Any, config: BillingConfig) -> None:
        self._billing = billing
        self._config = config

    def _policy(self) -> BillingAutoRechargePolicy | None:
        config: BillingAutoRechargeConfig | None = self._config.auto_recharge
        if config is None or not config.enabled:
            return None
        return config.default_policy

    def _policy_snapshot(self, policy: BillingAutoRechargePolicy) -> tuple[dict[str, Any], str]:
        snapshot = policy.model_dump(mode="json", exclude_none=True)
        encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
        return snapshot, sha256(encoded.encode()).hexdigest()

    def _topup_product(self, policy: BillingAutoRechargePolicy, provider: PaymentProvider) -> str:
        topup = self._config.topups.get(policy.topup.key)
        if topup is None:
            raise ValueError("auto-recharge top-up is not configured")
        provider_ref = topup.providers.get(provider.provider)
        if provider_ref is None or not provider_ref.product_id:
            raise ValueError("auto-recharge top-up is unavailable for this provider")
        return provider_ref.product_id

    async def quote(self, user_id: str, provider: PaymentProvider) -> dict[str, Any] | None:
        policy = self._policy()
        profile = self._billing.get_auto_recharge_profile(user_id)
        if policy is None or profile is None or not profile.enabled:
            return None
        if not profile.provider_customer_id or not profile.payment_method_id:
            return None
        quote = await provider.preview_saved_payment_charge(
            SavedPaymentChargeParams(
                customer_id=profile.provider_customer_id,
                payment_method_id=profile.payment_method_id,
                product_id=self._topup_product(policy, provider),
                quantity=policy.topup.quantity,
                idempotency_key=f"auto-recharge-quote:{user_id}",
            )
        )
        return {"amount_minor": quote.amount_minor, "currency": quote.currency}

    async def enable(
        self,
        user_id: str,
        provider: PaymentProvider,
        *,
        consent_reference: str,
        consent_metadata: dict[str, Any] | None = None,
    ) -> BillingAutoRechargeProfile:
        policy = self._policy()
        if policy is None:
            raise ValueError("auto-recharge is not configured")
        customer = self._billing.get_customer_by_user_id(user_id, provider.provider)
        if customer is None:
            raise ValueError("a saved payment method is required")
        method = await provider.get_default_payment_method(customer.provider_customer_id)
        if method is None:
            raise ValueError("select a saved payment method before enabling auto-recharge")
        snapshot, policy_hash = self._policy_snapshot(policy)
        quote = await provider.preview_saved_payment_charge(
            SavedPaymentChargeParams(
                customer_id=customer.provider_customer_id,
                payment_method_id=method.id,
                product_id=self._topup_product(policy, provider),
                quantity=policy.topup.quantity,
                idempotency_key=f"auto-recharge-quote:{user_id}",
            )
        )
        profile = BillingAutoRechargeProfile(
            user_id=user_id,
            enabled=True,
            state="active",
            provider=provider.provider,
            provider_customer_id=customer.provider_customer_id,
            payment_method_id=method.id,
            consent_reference=consent_reference,
            consent_metadata=consent_metadata,
            policy_snapshot=snapshot,
            policy_hash=policy_hash,
            quote_snapshot={"amount_minor": quote.amount_minor, "currency": quote.currency},
            armed=True,
        )
        self._billing.upsert_auto_recharge_profile(profile)
        return profile

    def disable(self, user_id: str) -> None:
        profile = self._billing.get_auto_recharge_profile(user_id)
        if profile is None:
            return
        profile.enabled = False
        profile.state = "disabled"
        self._billing.upsert_auto_recharge_profile(profile)

    async def process_if_needed(self, user_id: str, balance: int, provider: PaymentProvider) -> str:
        policy = self._policy()
        profile = self._billing.get_auto_recharge_profile(user_id)
        if policy is None or profile is None or not profile.enabled:
            return "disabled"
        if balance >= policy.trigger.threshold_credits:
            if not profile.armed:
                profile.armed = True
                self._billing.upsert_auto_recharge_profile(profile)
            return "above_threshold"
        if not profile.armed or not profile.provider_customer_id or not profile.payment_method_id:
            return "not_armed"
        window_days = 0 if policy.limit.period == "calendar_month" else policy.limit.rolling_days or 30
        attempt = self._billing.claim_auto_recharge_attempt(
            user_id,
            provider.provider,
            policy.topup.key,
            policy.topup.quantity,
            policy.limit.max_charges,
            window_days,
        )
        if attempt is None:
            return "limit_reached"
        profile.armed = False
        self._billing.upsert_auto_recharge_profile(profile)
        try:
            charge = await provider.charge_saved_payment_method(
                SavedPaymentChargeParams(
                    customer_id=profile.provider_customer_id,
                    payment_method_id=profile.payment_method_id,
                    product_id=self._topup_product(policy, provider),
                    quantity=policy.topup.quantity,
                    idempotency_key=attempt.idempotency_key,
                    metadata={"user_id": user_id, "auto_recharge_attempt_id": attempt.id},
                )
            )
            self._billing.update_auto_recharge_attempt(
                attempt.id,
                charge.status,
                charge.provider_payment_id,
                getattr(charge, "failure_code", None),
                charge.action_url,
            )
            return charge.status
        except Exception as error:
            self._billing.update_auto_recharge_attempt(attempt.id, "failed", failure_code=str(error))
            profile.state = "suspended"
            profile.suspended_reason = "payment_failed"
            self._billing.upsert_auto_recharge_profile(profile)
            return "failed"
