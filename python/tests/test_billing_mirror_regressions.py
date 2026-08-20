from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from bursar.billing.billing_service import BillingService
from bursar.billing.contracts import (
    AutoRechargeAttemptUpdate,
    BillingSubscriptionChangeUpdate,
    CheckoutIntentUpdate,
)
from bursar.billing.postgres.repositories.subscription import BillingSubscriptionRepository
from bursar.billing.postgres.store import PostgresBillingStore
from bursar.billing.service_types import BillingProvisioningPort, BillingServiceOptions
from bursar.billing.types import (
    BillingAutoRechargeProfile,
    BillingCustomerInfo,
    BillingEvent,
    BillingEventClaim,
    BillingEventResult,
    BillingEventType,
    BillingInvoiceInfo,
    BillingOfferResult,
    BillingSubscriptionChangeInput,
    BillingSubscriptionInfo,
    BillingSubscriptionState,
    BillingSubscriptionStatus,
    ProviderRef,
)
from bursar.errors import StoreError


def test_billing_service_uses_provisioning_from_options() -> None:
    provisioning = MagicMock(spec=BillingProvisioningPort)
    service = BillingService(
        MagicMock(),
        BillingServiceOptions(provisioning=provisioning),
    )

    assert service._provisioning is provisioning


def _subscription(status: BillingSubscriptionStatus, subscription_id: str) -> BillingSubscriptionState:
    return BillingSubscriptionState(
        user_id="00000000-0000-0000-0000-000000000001",
        provider="stripe",
        provider_subscription_id=subscription_id,
        status=status,
        provider_updated_at="2026-07-29T00:00:00Z",
        cancel_at_period_end=False,
    )


def _persisted_subscription_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": "00000000-0000-0000-0000-000000000011",
        "subject_id": "00000000-0000-0000-0000-000000000001",
        "provider": "stripe",
        "provider_subscription_id": "sub_1",
        "provider_customer_id": "cus_1",
        "offer_id": "00000000-0000-0000-0000-000000000021",
        "catalog_revision_id": "00000000-0000-0000-0000-000000000022",
        "status": "active",
        "current_period_start": None,
        "current_period_end": None,
        "trial_end": None,
        "cancel_at": None,
        "ended_at": None,
        "cancel_at_period_end": False,
        "grace_ends_at": None,
        "grace_expired_at": None,
        "provider_updated_at": datetime(2026, 7, 29, tzinfo=UTC),
        "metadata": {},
    }
    row.update(overrides)
    return row


def _cancellation_event(*, event_id: str, refs: ProviderRef | None) -> BillingEvent:
    return BillingEvent(
        provider="stripe",
        event_id=event_id,
        event_type=BillingEventType.subscription_canceled,
        occurred_at="2026-07-29T00:00:00Z",
        account_id="00000000-0000-0000-0000-000000000001",
        subscription=BillingSubscriptionInfo(
            provider_subscription_id="sub_unknown",
            refs=refs,
        ),
    )


def test_list_cancellable_subscriptions_matches_javascript_statuses() -> None:
    store = MagicMock()
    subscriptions = [_subscription(status, f"sub_{status.value}") for status in BillingSubscriptionStatus]
    store.get_user_subscriptions.return_value = subscriptions
    service = BillingService(store)

    result = service.list_cancellable_subscriptions("00000000-0000-0000-0000-000000000001")

    assert {subscription.status for subscription in result} == {
        BillingSubscriptionStatus.active,
        BillingSubscriptionStatus.trialing,
        BillingSubscriptionStatus.past_due,
        BillingSubscriptionStatus.incomplete,
        BillingSubscriptionStatus.unpaid,
        BillingSubscriptionStatus.paused,
    }
    assert service.list_cancellable_provider_subscription_ids("00000000-0000-0000-0000-000000000001") == [
        subscription.provider_subscription_id for subscription in result
    ]


def test_expire_past_due_grace_periods_matches_javascript_cas_flow() -> None:
    user_id = "00000000-0000-0000-0000-000000000001"
    subscription_id = "00000000-0000-0000-0000-000000000002"
    store = MagicMock()
    provisioning = MagicMock()
    store.list_expired_grace_subscriptions.return_value = [
        BillingSubscriptionState(
            subscription_id=subscription_id,
            user_id=user_id,
            provider="stripe",
            provider_subscription_id="sub_grace",
            status=BillingSubscriptionStatus.past_due,
            grace_ends_at="2026-07-29T00:00:00+00:00",
            provider_updated_at="2026-07-29T00:00:00+00:00",
            cancel_at_period_end=False,
        )
    ]
    store.get_billing_subscription.return_value = BillingSubscriptionState(
        subscription_id=subscription_id,
        user_id=user_id,
        provider="stripe",
        provider_subscription_id="sub_grace",
        status=BillingSubscriptionStatus.past_due,
        grace_ends_at="2026-07-29T00:00:00+00:00",
        provider_updated_at="2026-07-29T00:00:00Z",
        cancel_at_period_end=False,
    )
    store.get_user_subscription.return_value = store.get_billing_subscription.return_value
    store.expire_subscription_grace_period.return_value = True
    service = BillingService(store, provisioning=provisioning)

    assert service.expire_past_due_grace_periods(datetime(2026, 7, 30, tzinfo=UTC)) == 1
    provisioning.unset_user_plan.assert_not_called()
    store.expire_subscription_grace_period.assert_called_once_with(
        user_id,
        subscription_id,
        "2026-07-29T00:00:00+00:00",
        "2026-07-30T00:00:00+00:00",
        None,
    )


def test_grace_expiry_is_disabled_without_plan_provisioning() -> None:
    store = MagicMock()
    service = BillingService(store)

    assert service.expire_past_due_grace_periods(datetime(2026, 7, 30, tzinfo=UTC)) == 0
    store.list_expired_grace_subscriptions.assert_not_called()
    store.expire_subscription_grace_period.assert_not_called()


def test_terminal_subscription_replacement_is_source_aware() -> None:
    user_id = "00000000-0000-0000-0000-000000000001"
    subscription_id = "00000000-0000-0000-0000-000000000002"
    store = MagicMock()
    store.get_billing_subscription.return_value = BillingSubscriptionState(
        subscription_id=subscription_id,
        user_id=user_id,
        provider="stripe",
        provider_subscription_id="sub_paused",
        status=BillingSubscriptionStatus.active,
        provider_updated_at="2026-07-29T00:00:00+00:00",
        cancel_at_period_end=False,
    )
    provisioning = MagicMock()
    service = BillingService(store, provisioning=provisioning, terminal_plan_key="free")
    event = BillingEvent(
        provider="stripe",
        event_id="evt_paused_source",
        event_type=BillingEventType.subscription_paused,
        occurred_at="2026-07-30T00:00:00+00:00",
        account_id=user_id,
        subscription=BillingSubscriptionInfo(
            provider_subscription_id="sub_paused",
            status=BillingSubscriptionStatus.paused,
        ),
        billing_event_id="00000000-0000-0000-0000-000000000099",
    )
    store.reconcile_subscription_entitlement.return_value = "revoked"

    result = service._handle_subscription_paused(event)

    assert result.handled is True
    store.reconcile_subscription_entitlement.assert_called_once_with(
        user_id,
        subscription_id,
        "00000000-0000-0000-0000-000000000099",
        BillingSubscriptionStatus.paused,
        "2026-07-30T00:00:00+00:00",
        None,
        True,
        "free",
        "subscription_paused",
    )
    provisioning.unset_user_plan.assert_not_called()


def test_async_billing_event_handler_is_awaited() -> None:
    called: list[str] = []

    async def handler(event: BillingEvent, user_id: str) -> None:
        await asyncio.sleep(0)
        called.append(f"{event.event_id}:{user_id}")

    service = BillingService(
        MagicMock(),
        event_handlers={BillingEventType.customer_created: handler},
    )
    event = BillingEvent(
        provider="stripe",
        event_id="evt_async",
        event_type=BillingEventType.customer_created,
        occurred_at="2026-07-29T00:00:00Z",
        customer=BillingCustomerInfo(provider_customer_id="cus_async"),
    )

    service._fire_event_handlers(
        event,
        "00000000-0000-0000-0000-000000000001",
    )

    assert called == ["evt_async:00000000-0000-0000-0000-000000000001"]


def test_unknown_cancellation_with_offer_refs_persists_tombstone() -> None:
    store = MagicMock()
    store.claim_billing_event.return_value = BillingEventClaim(
        status="claimed",
        claim_token="claim",
        billing_event_id="00000000-0000-0000-0000-000000000099",
    )
    persisted = BillingSubscriptionState(
        subscription_id="00000000-0000-0000-0000-000000000011",
        user_id="00000000-0000-0000-0000-000000000001",
        provider="stripe",
        provider_subscription_id="sub_unknown",
        offer_id="00000000-0000-0000-0000-000000000010",
        offer_key="pro_monthly",
        plan="pro",
        status=BillingSubscriptionStatus.canceled,
        provider_updated_at="2026-07-29T00:00:00Z",
        cancel_at_period_end=True,
    )
    store.get_billing_subscription.side_effect = [None, persisted]
    store.reconcile_subscription_entitlement.return_value = "preserved"
    store.resolve_billing_offer.return_value = BillingOfferResult(
        offer_id="00000000-0000-0000-0000-000000000010",
        offer_key="pro_monthly",
        plan_id="00000000-0000-0000-0000-000000000020",
        plan="pro",
        interval="month",
        interval_count=1,
        grant=None,
    )
    service = BillingService(store)

    result = service.ingest_billing_event(
        _cancellation_event(event_id="evt_cancel_resolved", refs=ProviderRef(price_id="price_pro"))
    )

    assert result.handled is True
    state = store.upsert_billing_subscription.call_args.args[0]
    assert state.status == BillingSubscriptionStatus.canceled
    assert state.offer_key == "pro_monthly"
    assert state.offer_id == "00000000-0000-0000-0000-000000000010"
    assert state.plan == "pro"
    assert state.interval == "month"
    assert state.interval_count == 1
    store.complete_billing_event.assert_called_once()
    store.fail_billing_event.assert_not_called()


def test_unknown_cancellation_without_offer_refs_is_failed_for_retry() -> None:
    store = MagicMock()
    store.claim_billing_event.return_value = BillingEventClaim(
        status="claimed",
        claim_token="claim",
        billing_event_id="00000000-0000-0000-0000-000000000099",
    )
    store.get_billing_subscription.return_value = None
    service = BillingService(store)

    result = service.ingest_billing_event(_cancellation_event(event_id="evt_cancel_unresolved", refs=None))

    assert result.handled is False
    assert result.error == "billing_event_processing_failed:STORE_ERROR"
    store.upsert_billing_subscription.assert_not_called()
    store.complete_billing_event.assert_not_called()
    store.fail_billing_event.assert_called_once()


def test_subscription_entitlement_uses_atomic_store_boundary() -> None:
    provisioning = MagicMock()
    store = MagicMock()
    persisted = BillingSubscriptionState(
        subscription_id="00000000-0000-0000-0000-000000000011",
        user_id="00000000-0000-0000-0000-000000000001",
        provider="stripe",
        provider_subscription_id="sub_provision",
        offer_id="00000000-0000-0000-0000-000000000010",
        plan="pro",
        status=BillingSubscriptionStatus.active,
        provider_updated_at="2026-07-29T00:00:00Z",
        cancel_at_period_end=False,
    )
    store.get_billing_subscription.return_value = persisted
    store.reconcile_subscription_entitlement.return_value = "applied"
    service = BillingService(store, provisioning=provisioning)
    user_id = "00000000-0000-0000-0000-000000000001"
    event = BillingEvent(
        provider="stripe",
        event_id="evt_provision",
        event_type=BillingEventType.subscription_activated,
        occurred_at="2026-07-29T00:00:00Z",
        account_id=user_id,
        subscription=BillingSubscriptionInfo(provider_subscription_id="sub_provision"),
        billing_event_id="00000000-0000-0000-0000-000000000099",
    )
    offer = BillingOfferResult(
        offer_id="00000000-0000-0000-0000-000000000010",
        offer_key="pro_monthly",
        plan_id="00000000-0000-0000-0000-000000000020",
        plan="pro",
        interval="month",
        interval_count=1,
        grant=None,
    )

    expected = BillingSubscriptionState(
        user_id=user_id,
        provider="stripe",
        provider_subscription_id="sub_provision",
        offer_id=offer.offer_id,
        plan=offer.plan,
        status=BillingSubscriptionStatus.active,
        provider_updated_at="2026-07-29T00:00:00Z",
        cancel_at_period_end=False,
    )
    assert service._reconcile_subscription_event(user_id, event, expected) == "applied"

    store.reconcile_subscription_entitlement.assert_called_once_with(
        user_id,
        persisted.subscription_id,
        "00000000-0000-0000-0000-000000000099",
        BillingSubscriptionStatus.active,
        "2026-07-29T00:00:00+00:00",
        None,
        True,
        None,
        "subscription_active",
    )
    provisioning.set_user_plan.assert_not_called()


def test_stale_renewal_suppresses_cycle_grant_and_callback() -> None:
    user_id = "00000000-0000-0000-0000-000000000001"
    subscription_id = "00000000-0000-0000-0000-000000000011"
    callback = MagicMock()
    store = MagicMock()
    store.get_billing_subscription.return_value = BillingSubscriptionState(
        subscription_id=subscription_id,
        user_id=user_id,
        provider="stripe",
        provider_subscription_id="sub_stale",
        offer_id="00000000-0000-0000-0000-000000000010",
        plan="pro",
        status=BillingSubscriptionStatus.active,
        provider_updated_at="2026-07-30T00:00:00Z",
        cancel_at_period_end=False,
    )
    store.resolve_billing_offer.return_value = BillingOfferResult(
        offer_id="00000000-0000-0000-0000-000000000010",
        offer_key="pro_monthly",
        plan_id="00000000-0000-0000-0000-000000000020",
        plan="pro",
        interval="month",
        interval_count=1,
        grant=None,
    )
    store.reconcile_subscription_entitlement.return_value = "stale"
    service = BillingService(
        store,
        provisioning=MagicMock(spec=BillingProvisioningPort),
        event_handlers={BillingEventType.subscription_renewed: callback},
    )
    event = BillingEvent(
        provider="stripe",
        event_id="evt_stale_renewal",
        event_type=BillingEventType.subscription_renewed,
        occurred_at="2026-07-29T00:00:00Z",
        account_id=user_id,
        subscription=BillingSubscriptionInfo(
            provider_subscription_id="sub_stale",
            status=BillingSubscriptionStatus.active,
            refs=ProviderRef(price_id="price_pro"),
        ),
        billing_event_id="00000000-0000-0000-0000-000000000099",
    )

    result = service._route_event(event)

    assert result == BillingEventResult(handled=True, action="stale_subscription_event")
    store.create_billing_credit_grant.assert_not_called()
    callback.assert_not_called()


def test_entitlement_opt_out_still_uses_atomic_version_fence() -> None:
    user_id = "00000000-0000-0000-0000-000000000001"
    subscription_id = "00000000-0000-0000-0000-000000000011"
    store = MagicMock()
    store.get_billing_subscription.return_value = BillingSubscriptionState(
        subscription_id=subscription_id,
        user_id=user_id,
        provider="stripe",
        provider_subscription_id="sub_opt_out",
        status=BillingSubscriptionStatus.active,
        provider_updated_at="2026-07-29T00:00:00Z",
        cancel_at_period_end=False,
    )
    store.reconcile_subscription_entitlement.return_value = "preserved"
    service = BillingService(
        store,
        BillingServiceOptions(auto_select_entitlement_source=False),
    )
    event = BillingEvent(
        provider="stripe",
        event_id="evt_opt_out",
        event_type=BillingEventType.subscription_activated,
        occurred_at="2026-07-29T00:00:00Z",
        account_id=user_id,
        subscription=BillingSubscriptionInfo(
            provider_subscription_id="sub_opt_out",
            status=BillingSubscriptionStatus.active,
        ),
        billing_event_id="00000000-0000-0000-0000-000000000099",
    )
    expected = store.get_billing_subscription.return_value

    assert service._reconcile_subscription_event(user_id, event, expected) == "preserved"
    assert store.reconcile_subscription_entitlement.call_args.args[6] is False


def test_falsey_provisioning_adapter_still_enables_atomic_entitlement() -> None:
    user_id = "00000000-0000-0000-0000-000000000001"
    store = MagicMock()
    persisted = BillingSubscriptionState(
        subscription_id="00000000-0000-0000-0000-000000000011",
        user_id=user_id,
        provider="stripe",
        provider_subscription_id="sub_falsey",
        status=BillingSubscriptionStatus.active,
        provider_updated_at="2026-07-29T00:00:00Z",
        cancel_at_period_end=False,
    )
    store.get_billing_subscription.return_value = persisted
    store.reconcile_subscription_entitlement.return_value = "applied"
    provisioning = MagicMock()
    provisioning.__bool__.return_value = False
    service = BillingService(store, provisioning=provisioning)
    event = BillingEvent(
        provider="stripe",
        event_id="evt_falsey",
        event_type=BillingEventType.subscription_activated,
        occurred_at="2026-07-29T00:00:00Z",
        account_id=user_id,
        subscription=BillingSubscriptionInfo(
            provider_subscription_id="sub_falsey",
            status=BillingSubscriptionStatus.active,
        ),
        billing_event_id="00000000-0000-0000-0000-000000000099",
    )

    assert service._reconcile_subscription_event(user_id, event, persisted) == "applied"
    assert store.reconcile_subscription_entitlement.call_args.args[6] is True


def test_get_user_subscription_uses_subsecond_timestamps() -> None:
    rows = [
        _persisted_subscription_row(
            id="00000000-0000-0000-0000-000000000011",
            provider_subscription_id="sub_older",
            provider_updated_at=datetime(2026, 7, 29, 12, 0, 0, 100_000, tzinfo=UTC),
        ),
        _persisted_subscription_row(
            id="00000000-0000-0000-0000-000000000012",
            provider_subscription_id="sub_newer",
            provider_updated_at=datetime(2026, 7, 29, 12, 0, 0, 900_000, tzinfo=UTC),
        ),
    ]
    repository = BillingSubscriptionRepository(
        MagicMock(
            side_effect=[
                rows,
                [
                    {
                        "offer_key": "pro_monthly",
                        "plan_id": "00000000-0000-0000-0000-000000000023",
                        "plan_key": "pro",
                        "billing_unit": "month",
                        "billing_count": 1,
                    }
                ],
            ]
        )
    )

    result = repository.get_user_subscription(
        "00000000-0000-0000-0000-000000000001",
        ["active"],
    )

    assert result is not None
    assert result.provider_subscription_id == "sub_newer"


def test_get_user_subscription_rejects_invalid_provider_timestamp() -> None:
    repository = BillingSubscriptionRepository(
        MagicMock(
            return_value=[
                _persisted_subscription_row(
                    provider_subscription_id="sub_invalid",
                    provider_updated_at="not-a-date",
                )
            ]
        )
    )

    with pytest.raises(StoreError, match="row validation failed"):
        repository.get_user_subscription(
            "00000000-0000-0000-0000-000000000001",
            ["active"],
        )


def test_reconcile_entitlement_validates_the_scalar_outcome() -> None:
    execute = MagicMock(return_value=[{"outcome": "applied"}])
    repository = BillingSubscriptionRepository(execute)

    result = repository.reconcile_entitlement(
        "00000000-0000-0000-0000-000000000001",
        "00000000-0000-0000-0000-000000000012",
        "00000000-0000-0000-0000-000000000099",
        BillingSubscriptionStatus.active,
        "2026-07-29T12:00:00.900000+00:00",
        None,
        True,
        None,
        "subscription_active",
    )

    assert result == "applied"
    assert "reconcile_subscription_entitlement" in execute.call_args.args[0]


def test_subscription_change_uses_current_rpc_and_offer_context_shape() -> None:
    store = object.__new__(PostgresBillingStore)
    subscription_repo = MagicMock()
    subscription_repo.get.return_value = SimpleNamespace(id="00000000-0000-0000-0000-000000000011")
    vars(store)["_subscription_repo"] = subscription_repo
    change_id = "12"
    from_offer_id = "00000000-0000-0000-0000-000000000013"
    to_offer_id = "00000000-0000-0000-0000-000000000014"
    from_revision_id = "00000000-0000-0000-0000-000000000015"
    to_revision_id = "00000000-0000-0000-0000-000000000016"
    store._execute = MagicMock(
        side_effect=[
            [{"change_id": change_id, "state": "scheduled", "error_code": None}],
            [
                {
                    "id": change_id,
                    "subscription_id": "00000000-0000-0000-0000-000000000011",
                    "from_offer_id": from_offer_id,
                    "from_catalog_revision_id": from_revision_id,
                    "to_offer_id": to_offer_id,
                    "to_catalog_revision_id": to_revision_id,
                    "effective_at": datetime(2026, 8, 1, tzinfo=UTC),
                    "effective_behavior": "renewal",
                    "state": "scheduled",
                    "proration_behavior": "provider_default",
                    "idempotency_key": "change-1",
                    "provider_operation_id": None,
                    "error_message": None,
                }
            ],
            [
                {
                    "side": "from",
                    "offer_id": from_offer_id,
                    "offer_key": "monk_monthly",
                    "plan_id": "00000000-0000-0000-0000-000000000021",
                    "plan_key": "monk",
                    "billing_unit": "month",
                    "billing_count": 1,
                },
                {
                    "side": "to",
                    "offer_id": to_offer_id,
                    "offer_key": "sage_monthly",
                    "plan_id": "00000000-0000-0000-0000-000000000022",
                    "plan_key": "sage",
                    "billing_unit": "month",
                    "billing_count": 1,
                },
            ],
        ]
    )

    change = store.create_billing_subscription_change(
        BillingSubscriptionChangeInput(
            provider="stripe",
            provider_subscription_id="sub_1",
            to_offer_id=to_offer_id,
            effective_at="2026-08-01T00:00:00+00:00",
            effective="renewal",
            idempotency_key="change-1",
        )
    )

    assert change.subscription_id == "00000000-0000-0000-0000-000000000011"
    assert change.from_offer.offer_key == "monk_monthly"
    assert change.to_offer.plan == "sage"
    assert change.effective == "renewal"
    assert store._execute.call_args_list[0].args[1][3] == "renewal"
    sql_calls = [call.args[0] for call in store._execute.call_args_list]
    assert "bursar.open_subscription_change" in sql_calls[0]
    assert "bursar.get_billing_subscription_change" in sql_calls[1]
    assert "::bigint" in sql_calls[1]
    assert "bursar.get_catalog_offer_context" in sql_calls[2]
    assert all("INSERT INTO bursar.billing_subscription_changes" not in sql for sql in sql_calls)


def test_subscription_change_transition_uses_bigint_identifier() -> None:
    store = object.__new__(PostgresBillingStore)
    store._execute = MagicMock(return_value=[{"advanced": True}])

    store.update_billing_subscription_change(
        "12",
        BillingSubscriptionChangeUpdate(state="applied"),
    )

    sql, params = store._execute.call_args.args
    assert "bursar.advance_subscription_change" in sql
    assert "%s::bigint" in sql
    assert params[0] == "12"


def test_checkout_intent_updates_only_through_transition_rpc() -> None:
    store = object.__new__(PostgresBillingStore)
    store._execute = MagicMock(return_value=[{"advanced": True}])

    store.update_checkout_intent(
        "00000000-0000-0000-0000-000000000011",
        CheckoutIntentUpdate(
            status="completed",
            provider_session_id="session_1",
        ),
    )

    sql = store._execute.call_args.args[0]
    assert "bursar.advance_checkout_intent" in sql
    assert "UPDATE bursar.billing_checkout_intents" not in sql

    store._execute.return_value = [{"advanced": False}]
    with pytest.raises(StoreError, match="checkout intent update rejected"):
        store.update_checkout_intent(
            "00000000-0000-0000-0000-000000000011",
            CheckoutIntentUpdate(status="failed"),
        )


def test_auto_recharge_profile_and_attempt_use_current_rpcs() -> None:
    store = object.__new__(PostgresBillingStore)
    execute = MagicMock(return_value=[{"updated": True}])
    store._execute = execute
    profile = BillingAutoRechargeProfile(
        user_id="00000000-0000-0000-0000-000000000001",
        enabled=True,
        state="active",
        provider="stripe",
        topup_id="00000000-0000-0000-0000-000000000002",
        quantity=2,
        threshold=Decimal("10"),
        max_charges_per_window=3,
        window_unit="month",
        window_count=1,
        window_anchor="calendar",
        window_timezone="UTC",
    )

    store.upsert_auto_recharge_profile(profile)

    assert "bursar.upsert_auto_recharge_profile" in execute.call_args.args[0]
    assert execute.call_args.args[1][-3:] == [True, "active", False]
    attempt_row = {
        "id": "00000000-0000-0000-0000-000000000003",
        "subject_id": "00000000-0000-0000-0000-000000000001",
        "provider": "stripe",
        "idempotency_key": "auto-recharge:test",
        "provider_attempt_id": None,
        "topup_id": "00000000-0000-0000-0000-000000000002",
        "quantity": 2,
        "window_start": datetime(2025, 1, 1, tzinfo=UTC),
        "window_end": datetime(2025, 2, 1, tzinfo=UTC),
        "quoted_amount_minor": None,
        "currency": None,
        "failure_code": None,
        "failure_message": None,
        "metadata": {},
        "created_at": datetime(2025, 1, 1, tzinfo=UTC),
        "updated_at": datetime(2025, 1, 1, tzinfo=UTC),
    }
    execute.reset_mock()
    execute.side_effect = [
        [{**attempt_row, "state": "claimed"}],
        [{"advanced": True}],
        [{"advanced": True}],
    ]
    store.update_auto_recharge_attempt(
        AutoRechargeAttemptUpdate(
            id="00000000-0000-0000-0000-000000000003",
            state="processing",
            provider_attempt_id="pay_1",
        )
    )
    assert "bursar.get_auto_recharge_attempt" in execute.call_args_list[0].args[0]
    assert all("bursar.advance_auto_recharge_attempt" in call.args[0] for call in execute.call_args_list[1:])

    execute.reset_mock()
    execute.side_effect = [[{**attempt_row, "state": "action_required"}], [{"advanced": True}]]
    store.update_auto_recharge_attempt(
        AutoRechargeAttemptUpdate(
            id="00000000-0000-0000-0000-000000000003",
            state="succeeded",
            provider_attempt_id="pay_1",
        )
    )
    assert len(execute.call_args_list) == 2


def test_plan_change_advances_only_after_entitlement_version_fence() -> None:
    store = MagicMock()
    store.get_billing_subscription.return_value = BillingSubscriptionState(
        subscription_id="00000000-0000-0000-0000-000000000011",
        user_id="00000000-0000-0000-0000-000000000001",
        provider="dodo",
        provider_subscription_id="sub_1",
        offer_id="00000000-0000-0000-0000-000000000012",
        offer_key="monk_monthly",
        plan="monk",
        status=BillingSubscriptionStatus.active,
        metadata={"pendingPlanChange": {"to": "sage"}},
        provider_updated_at="2026-07-29T00:00:00Z",
        cancel_at_period_end=False,
    )
    store.resolve_billing_offer.return_value = BillingOfferResult(
        offer_id="00000000-0000-0000-0000-000000000013",
        offer_key="sage_monthly",
        plan_id="00000000-0000-0000-0000-000000000014",
        plan="sage",
        interval="month",
        interval_count=1,
        grant=None,
    )
    store.get_open_billing_subscription_change.return_value = SimpleNamespace(id="change_1")
    store.reconcile_subscription_entitlement.return_value = "preserved"
    service = BillingService(store)
    event = BillingEvent(
        provider="dodo",
        event_id="evt_plan_change",
        event_type=BillingEventType.subscription_plan_changed,
        occurred_at="2026-07-29T00:00:00Z",
        account_id="00000000-0000-0000-0000-000000000001",
        subscription=BillingSubscriptionInfo(
            provider_subscription_id="sub_1",
            status=BillingSubscriptionStatus.active,
            refs=ProviderRef(product_id="prod_sage"),
        ),
        billing_event_id="00000000-0000-0000-0000-000000000099",
    )

    result = service._handle_subscription_plan_changed(event)

    assert result.handled
    method_names = [call[0] for call in store.method_calls]
    assert method_names.index("upsert_billing_subscription") < method_names.index("reconcile_subscription_entitlement")
    assert method_names.index("reconcile_subscription_entitlement") < method_names.index(
        "update_billing_subscription_change"
    )
    state = store.upsert_billing_subscription.call_args.args[0]
    assert state.metadata == {"pendingPlanChange": None}


def test_plan_change_captures_allowance_anchor_before_advancing() -> None:
    anchor = datetime(2026, 7, 1, tzinfo=UTC)
    provisioning = MagicMock()
    provisioning.get_user_plan.return_value = SimpleNamespace(plan_assigned_at=anchor)
    store = MagicMock()
    store.get_billing_subscription.return_value = BillingSubscriptionState(
        subscription_id="00000000-0000-0000-0000-000000000011",
        user_id="00000000-0000-0000-0000-000000000001",
        provider="dodo",
        provider_subscription_id="sub_1",
        offer_id="00000000-0000-0000-0000-000000000012",
        offer_key="monk_monthly",
        plan="monk",
        status=BillingSubscriptionStatus.active,
        provider_updated_at="2026-07-29T00:00:00Z",
        cancel_at_period_end=False,
    )
    store.resolve_billing_offer.return_value = BillingOfferResult(
        offer_id="00000000-0000-0000-0000-000000000013",
        offer_key="sage_monthly",
        plan_id="00000000-0000-0000-0000-000000000014",
        plan="sage",
        interval="month",
        interval_count=1,
        grant=None,
    )
    store.get_open_billing_subscription_change.return_value = SimpleNamespace(id="12")
    store.reconcile_subscription_entitlement.return_value = "applied"
    service = BillingService(store, provisioning=provisioning)
    event = BillingEvent(
        provider="dodo",
        event_id="evt_plan_change_anchor",
        event_type=BillingEventType.subscription_plan_changed,
        occurred_at="2026-07-29T00:00:00Z",
        account_id="00000000-0000-0000-0000-000000000001",
        subscription=BillingSubscriptionInfo(
            provider_subscription_id="sub_1",
            status=BillingSubscriptionStatus.active,
            refs=ProviderRef(product_id="prod_sage"),
        ),
        billing_event_id="00000000-0000-0000-0000-000000000099",
    )

    result = service._handle_subscription_plan_changed(event)

    assert result.handled
    assert provisioning.method_calls[0].args == (event.account_id,)
    store.reconcile_subscription_entitlement.assert_called_once_with(
        event.account_id,
        "00000000-0000-0000-0000-000000000011",
        "00000000-0000-0000-0000-000000000099",
        BillingSubscriptionStatus.active,
        "2026-07-29T00:00:00+00:00",
        anchor,
        True,
        None,
        "subscription_active",
    )
    provisioning.set_user_plan.assert_not_called()
    assert provisioning.get_user_plan.call_count == 1


def test_event_claim_envelope_matches_javascript_shape() -> None:
    store = MagicMock()
    store.claim_billing_event.return_value = BillingEventClaim(status="duplicate")
    service = BillingService(store)
    event = BillingEvent(
        provider="stripe",
        event_id="evt_shape",
        event_type=BillingEventType.subscription_updated,
        occurred_at="2026-07-29T00:00:00Z",
        account_id="00000000-0000-0000-0000-000000000001",
        subscription=BillingSubscriptionInfo(
            provider_subscription_id="sub_1",
            cancel_at_period_end=True,
            refs=ProviderRef(price_id="price_1"),
        ),
        metadata={"provider_key": "must-not-be-renamed"},
    )

    service.ingest_billing_event(event)

    envelope = store.claim_billing_event.call_args.args[3]
    assert envelope["eventId"] == "evt_shape"
    assert envelope["eventType"] == "subscription.updated"
    assert envelope["accountId"] == "00000000-0000-0000-0000-000000000001"
    assert envelope["subscription"]["providerSubscriptionId"] == "sub_1"
    assert envelope["subscription"]["cancelAtPeriodEnd"] is True
    assert envelope["subscription"]["refs"] == {"priceId": "price_1"}
    assert envelope["metadata"] == {"provider_key": "must-not-be-renamed"}
    assert "occurredAt" not in envelope


def test_busy_billing_claim_is_retryable_by_provider_adapter() -> None:
    store = MagicMock()
    store.claim_billing_event.return_value = BillingEventClaim(status="busy")
    service = BillingService(store)
    event = BillingEvent(
        provider="stripe",
        event_id="evt_busy",
        event_type=BillingEventType.invoice_paid,
        occurred_at="2026-07-29T00:00:00Z",
        invoice=BillingInvoiceInfo(
            provider_invoice_id="in_busy",
            status="paid",
            amount_paid_minor=0,
            amount_due_minor=0,
            currency="USD",
        ),
    )

    result = service.ingest_billing_event(event)

    assert result.handled is False
    assert result.error == "claim_busy"


def test_subscription_repository_uses_current_catalog_and_lifecycle_rpc_shape() -> None:
    execute = MagicMock(
        side_effect=[
            [],
            [{"id": "00000000-0000-0000-0000-000000000002"}],
            [{"id": "00000000-0000-0000-0000-000000000003"}],
        ]
    )
    repository = BillingSubscriptionRepository(execute)

    repository.upsert(
        BillingSubscriptionState(
            user_id="00000000-0000-0000-0000-000000000001",
            provider="stripe",
            provider_subscription_id="sub_1",
            provider_customer_id="cus_1",
            offer_key="monk_monthly",
            status=BillingSubscriptionStatus.active,
            trial_end="2026-08-01T00:00:00+00:00",
            cancel_at="2026-09-01T00:00:00+00:00",
            provider_updated_at="2026-07-29T00:00:00+00:00",
            cancel_at_period_end=False,
        )
    )

    assert "get_billing_subscription_by_provider" in execute.call_args_list[0].args[0]
    assert "resolve_active_catalog_offer" in execute.call_args_list[1].args[0]
    upsert_sql, upsert_params = execute.call_args_list[2].args
    assert "bursar.upsert_billing_subscription" in upsert_sql
    assert len(upsert_params) == 15
    assert upsert_params[0] == "00000000-0000-0000-0000-000000000001"
    assert upsert_params[3] == "cus_1"
    assert upsert_params[4] == "00000000-0000-0000-0000-000000000002"
    assert upsert_params[10:15] == [
        "2026-08-01T00:00:00+00:00",
        "2026-09-01T00:00:00+00:00",
        None,
        "2026-07-29T00:00:00+00:00",
        None,
    ]
    assert all("active_catalog_revision" not in call.args[0] for call in execute.call_args_list)
