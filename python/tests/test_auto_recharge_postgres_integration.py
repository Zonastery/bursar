"""Focused PostgreSQL integration coverage for auto-recharge recovery."""

from __future__ import annotations

import psycopg2
import pytest

from bursar.billing.contracts import AutoRechargeProviderPaymentUpdate
from bursar.billing.postgres.store import PostgresBillingStore
from bursar.bursar import Bursar
from bursar.commerce import AutoRechargeInput, CommerceOptions
from bursar.providers.types import SavedPaymentChargeParams, SavedPaymentChargeResult
from tests.conftest import TEST_TENANT_ID
from tests.test_commerce_integration import CONFIG, USER_ID, IntegrationMockProvider

pytestmark = [pytest.mark.integration]


class FailsOnceProvider(IntegrationMockProvider):
    """A provider transport that fails once, then returns a pending charge."""

    def __init__(self) -> None:
        super().__init__(event_sink=None)  # type: ignore[arg-type]
        self.fail_next_charge = True

    async def charge_saved_payment_method(self, params: SavedPaymentChargeParams) -> SavedPaymentChargeResult:
        if self.fail_next_charge:
            self.fail_next_charge = False
            raise RuntimeError("provider transport timed out")
        return SavedPaymentChargeResult(
            provider_payment_id="auto-retry-payment",
            status="processing",
            amount_minor=500,
            currency="USD",
        )


def _bursar_with_provider(
    pg_database_url: str,
    pg_store: object,
) -> tuple[Bursar, PostgresBillingStore, FailsOnceProvider]:
    billing_store = PostgresBillingStore(pg_database_url, tenant_id=TEST_TENANT_ID, provider_environment="test")
    provider = FailsOnceProvider()
    bursar = Bursar(
        credit_store=pg_store,  # type: ignore[arg-type]
        billing_store=billing_store,
        commerce_options=CommerceOptions(
            provider_environment="test",
            providers={"stripe": lambda _context: provider},
        ),
    )
    bursar.catalog.publish_and_activate(CONFIG)
    billing_store.upsert_billing_customer("stripe", "cus_auto_recharge", USER_ID, "auto@example.com")
    return bursar, billing_store, provider


@pytest.mark.asyncio
async def test_provider_timeout_is_unknown_then_retry_resumes_same_attempt(
    pg_database_url: str,
    pg_store: object,
) -> None:
    bursar, billing_store, provider = _bursar_with_provider(pg_database_url, pg_store)
    try:
        assert bursar.commerce is not None
        with pytest.raises(RuntimeError, match="provider transport timed out"):
            await bursar.commerce.auto_recharge.enable(
                AutoRechargeInput(account_id=USER_ID, return_url="https://app.example/retry")
            )

        with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT state, idempotency_key, failure_code, provider_attempt_id
                FROM bursar.billing_auto_recharge_attempts
                WHERE subject_id = %s::uuid
                """,
                (USER_ID,),
            )
            first = cursor.fetchone()
        assert first is not None
        assert first[0] == "unknown"
        assert first[2] == "provider_request_failed"
        assert first[3] is None

        status = await bursar.commerce.auto_recharge.retry(
            AutoRechargeInput(account_id=USER_ID, return_url="https://app.example/retry")
        )
        assert status is not None
        assert status.state == "active"

        with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT state, idempotency_key, provider_attempt_id
                FROM bursar.billing_auto_recharge_attempts
                WHERE subject_id = %s::uuid
                """,
                (USER_ID,),
            )
            resumed = cursor.fetchone()
        assert resumed == ("processing", first[1], "auto-retry-payment")
        assert provider.fail_next_charge is False

        billing_store.update_auto_recharge_attempt_by_provider_payment(
            AutoRechargeProviderPaymentUpdate(
                provider="stripe",
                provider_payment_id="auto-retry-payment",
                state="succeeded",
            )
        )
        with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT state FROM bursar.billing_auto_recharge_attempts WHERE subject_id = %s::uuid",
                (USER_ID,),
            )
            assert cursor.fetchone() == ("succeeded",)
    finally:
        billing_store.close()


@pytest.mark.parametrize(
    ("change", "admitted"),
    [
        ("unrelated", True),
        ("restricted", False),
        ("expired", False),
        ("future", False),
        ("removed_policy", False),
        ("changed_product", False),
        ("changed_terms", False),
        ("reordered", True),
        ("changed_default_quantity", True),
        ("reduced_max_charge", False),
        ("threshold_outside_range", False),
        ("wrong_product", False),
    ],
)
def test_claim_reconciles_saved_topup_against_active_catalog(
    pg_database_url: str, pg_store: object, change: str, admitted: bool
) -> None:
    from copy import deepcopy
    from decimal import Decimal

    from bursar.billing.contracts import AutoRechargeAttemptClaim
    from bursar.billing.types import BillingAutoRechargeProfile

    bursar, billing_store, _provider = _bursar_with_provider(pg_database_url, pg_store)
    try:
        topup = bursar.billing.resolve_topup("stripe", None, "price_topup_500")
        assert topup is not None
        billing_store.upsert_auto_recharge_profile(
            BillingAutoRechargeProfile(
                user_id=USER_ID,
                enabled=True,
                state="active",
                provider="stripe",
                topup_id=topup.topup_id,
                quantity=1,
                threshold=Decimal("1000"),
                max_charges_per_window=3,
                window_unit="day",
                window_count=30,
                window_anchor="rolling",
                window_timezone="UTC",
            )
        )
        updated = deepcopy(CONFIG)
        offers = updated["commerce"]["offers"]
        offer = offers["standard_topup"]
        if change in {"unrelated", "wrong_product"}:
            updated["plans"]["starter"]["display_name"] = "Starter revised"
        elif change == "restricted":
            offer["availability"] = {"regions": ["IN"]}
        elif change == "expired":
            offer["availability"] = {"ends_at": "2000-01-01T00:00:00Z"}
        elif change == "future":
            offer["availability"] = {"starts_at": "2099-01-01T00:00:00Z"}
        elif change == "removed_policy":
            updated["commerce"].pop("auto_recharge")
        elif change == "changed_default_quantity":
            updated["commerce"]["auto_recharge"]["quantity"]["default"] = 2
        elif change == "reduced_max_charge":
            updated["commerce"]["auto_recharge"]["limits"]["max_charge_minor"] = 100
        elif change == "threshold_outside_range":
            updated["commerce"]["auto_recharge"]["balance_below"]["maximum"] = "500"
            updated["commerce"]["auto_recharge"]["balance_below"]["default"] = "500"
        elif change == "changed_terms":
            offer["price"]["amount_minor"] = 600
        elif change == "changed_product":
            offer["providers"]["stripe"]["price_id"] = "changed_price"
        elif change == "reordered":
            offers["other_topup"] = deepcopy(offer)
            offers["other_topup"]["providers"]["stripe"]["price_id"] = "other_price"
            updated["commerce"]["auto_recharge"]["eligible_topups"] = ["other_topup", "standard_topup"]
        bursar.catalog.publish_and_activate(updated)
        attempt = billing_store.claim_auto_recharge_attempt(
            AutoRechargeAttemptClaim(
                user_id=USER_ID,
                idempotency_key="claim-test",
                expected_product_id=("wrong_price" if change == "wrong_product" else "price_topup_500"),
                expected_quantity=1,
                expected_topup_key="standard_topup",
            )
        )
        assert (attempt is not None) is admitted
    finally:
        billing_store.close()


@pytest.mark.parametrize(("changed_terms", "retry_allowed"), [(False, True), (True, False)])
def test_unknown_attempt_keeps_its_key_across_catalog_changes(
    pg_database_url: str, pg_store: object, changed_terms: bool, retry_allowed: bool
) -> None:
    from copy import deepcopy
    from decimal import Decimal

    from bursar.billing.contracts import AutoRechargeAttemptClaim, AutoRechargeAttemptUpdate
    from bursar.billing.types import BillingAutoRechargeProfile

    bursar, billing_store, _provider = _bursar_with_provider(pg_database_url, pg_store)
    try:
        topup = bursar.billing.resolve_topup("stripe", None, "price_topup_500")
        assert topup is not None
        profile = BillingAutoRechargeProfile(
            user_id=USER_ID,
            enabled=True,
            state="active",
            provider="stripe",
            topup_id=topup.topup_id,
            quantity=1,
            threshold=Decimal("1000"),
            max_charges_per_window=3,
            window_unit="day",
            window_count=30,
            window_anchor="rolling",
            window_timezone="UTC",
        )
        billing_store.upsert_auto_recharge_profile(profile)
        claim = AutoRechargeAttemptClaim(
            user_id=USER_ID,
            idempotency_key="original-attempt",
            expected_product_id="price_topup_500",
            expected_quantity=1,
            expected_topup_key="standard_topup",
        )
        original = billing_store.claim_auto_recharge_attempt(claim)
        assert original is not None
        billing_store.update_auto_recharge_attempt(AutoRechargeAttemptUpdate(id=original.id, state="unknown"))
        billing_store.upsert_auto_recharge_profile(profile, reset_cooldown=True)
        updated = deepcopy(CONFIG)
        if changed_terms:
            updated["commerce"]["offers"]["standard_topup"]["price"]["amount_minor"] = 600
        else:
            updated["plans"]["starter"]["display_name"] = "Starter revised"
        bursar.catalog.publish_and_activate(updated)
        retry = billing_store.claim_auto_recharge_attempt(claim.model_copy(update={"idempotency_key": "retry-attempt"}))
        assert (retry is not None) is retry_allowed
        if retry is not None:
            assert retry.idempotency_key == "original-attempt"
        exact = billing_store.claim_auto_recharge_attempt(claim)
        assert exact is not None and exact.idempotency_key == "original-attempt"
    finally:
        billing_store.close()
