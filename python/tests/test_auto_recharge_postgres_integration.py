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
