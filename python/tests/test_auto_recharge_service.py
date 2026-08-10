"""Focused recovery tests for provider-side auto-recharge outcomes."""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from bursar.billing.auto_recharge_service import AutoRechargeService, _ResolvedAutoRechargePolicy
from bursar.billing.types import BillingAutoRechargeAttempt, BillingAutoRechargeProfile
from bursar.providers.types import PaymentMethodInfo, SavedPaymentChargeParams, SavedPaymentChargeResult


@pytest.mark.asyncio
async def test_unknown_attempt_resumes_with_its_original_provider_idempotency_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    billing = MagicMock()
    billing.get_auto_recharge_profile.return_value = BillingAutoRechargeProfile(
        user_id="user-1",
        enabled=True,
        state="active",
        provider="stripe",
        topup_id="topup-1",
        quantity=1,
        threshold=Decimal("10"),
        max_charges_per_window=3,
        window_unit="day",
        window_count=1,
        window_anchor="rolling",
        window_timezone="UTC",
    )
    existing = BillingAutoRechargeAttempt(
        id="attempt-1",
        user_id="user-1",
        provider="stripe",
        idempotency_key="auto-recharge:user-1:stable-attempt",
        provider_attempt_id=None,
        topup_id="topup-1",
        quantity=1,
        state="unknown",
        window_start="2026-08-01T00:00:00+00:00",
        window_end="2026-08-02T00:00:00+00:00",
        quoted_amount_minor=None,
        currency=None,
        failure_code="provider_request_failed",
        failure_message="connection lost",
        metadata={},
        created_at="2026-08-01T00:00:00+00:00",
        updated_at="2026-08-01T00:00:01+00:00",
    )
    billing.claim_auto_recharge_attempt.return_value = existing
    service = AutoRechargeService(billing)
    policy = _ResolvedAutoRechargePolicy(
        threshold=Decimal("10"),
        topup_key="small_pack",
        topup_id="topup-1",
        quantity=1,
        max_charges_per_window=3,
        window_unit="day",
        window_count=1,
        window_anchor="rolling",
        window_timezone="UTC",
        window_start="2026-08-01T00:00:00+00:00",
        window_end="2026-08-02T00:00:00+00:00",
        product_id="price-1",
    )
    monkeypatch.setattr(service, "_policy", lambda _provider: policy)
    monkeypatch.setattr(
        service,
        "_payment_method",
        AsyncMock(
            return_value=(
                "customer-1",
                PaymentMethodInfo(
                    id="payment-method-1",
                    last4="4242",
                    brand="visa",
                    expiry_month=12,
                    expiry_year=2030,
                ),
            )
        ),
    )

    class Provider:
        provider = "stripe"

        def __init__(self) -> None:
            self.keys: list[str] = []
            self.metadata: list[dict[str, str]] = []

        async def charge_saved_payment_method(self, params: SavedPaymentChargeParams) -> SavedPaymentChargeResult:
            self.keys.append(params.idempotency_key)
            self.metadata.append(params.metadata)
            return SavedPaymentChargeResult(
                provider_payment_id="payment-1",
                status="processing",
                amount_minor=500,
                currency="USD",
            )

    provider = Provider()
    result = await service.process_if_needed(
        "user-1",
        provider,  # type: ignore[arg-type]
        balance=Decimal("5"),
        return_url="https://example.test/return",
    )

    assert result.outcome == "submitted"
    assert provider.keys == [existing.idempotency_key]
    assert provider.metadata == [
        {
            "auto_recharge_attempt_id": existing.id,
            "bursar_account_id": "user-1",
            "purpose": "credit_topup",
        }
    ]
    assert billing.update_auto_recharge_attempt.call_args.args[0].state == "processing"
