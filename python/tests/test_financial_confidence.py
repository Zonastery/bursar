"""Production-boundary checks for financially sensitive Python SDK behavior.

The Postgres integration suite owns SQL atomicity and concurrency coverage.  This
module covers the high-level service contract that must remain true regardless of
the backing store: exact money values, policy resolution, replay-key scoping, and
failure handling around the reserve/settle lifecycle.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from hypothesis import given
from hypothesis import strategies as st

from bursar.credits.events import CreditEventEmitter
from bursar.credits.service import CreditsService
from bursar.credits.service_types import CanAffordOptions, CreditsServiceOptions, ReserveOptions, RunBilledOptions
from bursar.credits.types import (
    AddCreditsResult,
    AllowanceResult,
    AvailableResult,
    DeductionResult,
    LeaseResult,
    RefundResult,
    ReleaseResult,
)
from bursar.errors import RefundError

USER_ID = "user-production"


def _successful_lease(amount: Decimal = Decimal("3.000001")) -> LeaseResult:
    return LeaseResult(
        lease_id="lease-1",
        user_id=USER_ID,
        amount=amount,
        available=Decimal("20.000000"),
        reserved_total=amount,
        minimum_balance=Decimal("0"),
        billing_mode="strict",
        expires_at="2030-01-01T00:00:00+00:00",
    )


def _successful_deduction(amount: Decimal = Decimal("2.123456")) -> DeductionResult:
    return DeductionResult(
        entry_id="usage-1",
        user_id=USER_ID,
        amount=amount,
        balance_after=Decimal("17.876544"),
        allowance_consumed=Decimal("0"),
        idempotent=False,
    )


def _planless_store(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "get_user_plan": MagicMock(return_value=SimpleNamespace(plan_id=None)),
        # Reserve emits any persisted quota notifications after admission;
        # an in-memory test double has no quota events unless a test opts in.
        "list_quota_events": MagicMock(return_value=[]),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_add_credits_preserves_six_decimal_places_without_float_coercion() -> None:
    store = _planless_store(
        add_credits=MagicMock(
            return_value=AddCreditsResult(
                entry_id="grant-1",
                user_id=USER_ID,
                amount=Decimal("0.000001"),
                new_balance=Decimal("0.000001"),
                lifetime_purchased=Decimal("0.000001"),
                bucket=None,
            )
        )
    )
    service = CreditsService(store=cast(Any, store))

    service.add_credits(USER_ID, Decimal("0.000001"), idempotency_key="grant:precise")

    amount = store.add_credits.call_args.args[1]
    assert amount == Decimal("0.000001")
    assert amount.as_tuple().exponent == -6
    assert not isinstance(amount, float)


@given(st.decimals(min_value="0.000001", max_value="1000000", places=6))
def test_finite_six_place_decimal_grants_are_forwarded_unchanged(amount: Decimal) -> None:
    """No representable money value is silently rounded in the Python facade."""
    store = _planless_store(
        add_credits=MagicMock(
            return_value=AddCreditsResult(
                entry_id="grant-property",
                user_id=USER_ID,
                amount=amount,
                new_balance=amount,
                lifetime_purchased=amount,
                bucket=None,
            )
        )
    )
    service = CreditsService(store=cast(Any, store))

    service.add_credits(USER_ID, amount, idempotency_key="grant:property")

    assert store.add_credits.call_args.args[1] == amount


@pytest.mark.parametrize("amount", [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity"), 0.1])
def test_public_credit_amounts_reject_non_finite_or_binary_float_values(amount: Any) -> None:
    store = _planless_store(add_credits=MagicMock())
    service = CreditsService(store=cast(Any, store))

    with pytest.raises(ValueError, match=r"amount must be|amount must be finite"):
        service.add_credits(USER_ID, amount, idempotency_key="grant:invalid")

    store.add_credits.assert_not_called()


def test_plan_credit_line_and_operation_admission_policy_reach_atomic_reservation() -> None:
    plan = SimpleNamespace(
        plan_id="plan-pro",
        credit_policy=SimpleNamespace(type="credit_line", credit_limit=Decimal("7.50")),
        admission=SimpleNamespace(
            max_in_flight=4,
            operations={"completion": SimpleNamespace(max_in_flight=2)},
        ),
    )
    store = SimpleNamespace(
        get_user_plan=MagicMock(return_value=plan),
        create_lease=MagicMock(return_value=_successful_lease(Decimal("1.25"))),
        list_quota_events=MagicMock(return_value=[]),
    )
    service = CreditsService(store=cast(Any, store))

    service.reserve(
        USER_ID,
        Decimal("1.25"),
        ReserveOptions(operation_type="completion", idempotency_key="reserve:policy"),
    )

    options = store.create_lease.call_args.args[3]
    assert options.billing_mode == "overdraft"
    assert options.overdraft_floor == Decimal("-7.50")
    assert options.floor == Decimal("-7.50")
    assert options.max_concurrent == 2


def test_can_afford_includes_allowance_headroom_for_send_button_consistency() -> None:
    store = _planless_store(
        get_available=MagicMock(
            return_value=AvailableResult(
                user_id=USER_ID,
                balance=Decimal("2"),
                reserved=Decimal("0"),
                available=Decimal("2"),
            )
        ),
        check_allowance=MagicMock(
            return_value=AllowanceResult(
                plan_id="plan-free",
                allowance_remaining=Decimal("3"),
                period_start="2030-01-01T00:00:00+00:00",
                period_end="2030-02-01T00:00:00+00:00",
            )
        ),
    )
    service = CreditsService(store=cast(Any, store))

    result = service.can_afford(USER_ID, Decimal("5"), CanAffordOptions())

    assert result.affordable is True
    assert result.spendable == Decimal("5")
    assert result.worst_case == Decimal("5")


def test_run_billed_scopes_replay_keys_and_settles_actual_decimal_cost() -> None:
    store = _planless_store(
        create_lease=MagicMock(return_value=_successful_lease()),
        settle_lease=MagicMock(return_value=_successful_deduction()),
    )
    service = CreditsService(store=cast(Any, store))
    actual = Decimal("2.123456")

    result = service.run_billed(
        USER_ID,
        RunBilledOptions(
            estimate=Decimal("3.000001"),
            do_work=lambda: ("completed", actual),
            operation_key="checkout:42",
        ),
    )

    assert result.result == "completed"
    assert result.deduction.amount == actual
    create_options = store.create_lease.call_args.args[3]
    settle_options = store.settle_lease.call_args.args[3]
    assert create_options.idempotency_key == "checkout:42:reserve"
    assert settle_options.idempotency_key == "checkout:42:settle"
    assert store.settle_lease.call_args.args[2] == actual


def test_run_billed_releases_reservation_when_work_fails_before_settlement() -> None:
    store = _planless_store(
        create_lease=MagicMock(return_value=_successful_lease()),
        settle_lease=MagicMock(),
        release_lease=MagicMock(
            return_value=ReleaseResult(
                lease_id="lease-1",
                user_id=USER_ID,
                released=True,
                reason="work_failed",
            )
        ),
    )
    service = CreditsService(store=cast(Any, store))

    def fail() -> tuple[Any, Decimal]:
        raise RuntimeError("provider timeout")

    with pytest.raises(RuntimeError, match="provider timeout"):
        service.run_billed(
            USER_ID,
            RunBilledOptions(estimate=Decimal("3"), do_work=fail, operation_key="job:timeout"),
        )

    store.release_lease.assert_called_once_with(USER_ID, "lease-1")
    store.settle_lease.assert_not_called()


def test_failed_refund_emits_failure_only_and_never_a_success_event() -> None:
    store = _planless_store(
        refund_credits=MagicMock(
            return_value=RefundResult(
                refund_entry_id=None,
                original_entry_id="usage-1",
                user_id=USER_ID,
                amount=None,
                new_balance=None,
                error="already_refunded",
            )
        )
    )
    emitter = CreditEventEmitter()
    events: list[str] = []
    emitter.on("credits.refund_failed", lambda event: events.append(event.type))
    emitter.on("credits.refunded", lambda event: events.append(event.type))
    service = CreditsService(store=cast(Any, store), emitter=emitter)

    with pytest.raises(RefundError, match="already_refunded"):
        service.refund_credits("usage-1", idempotency_key="refund:replay")

    assert events == ["credits.refund_failed"]
    store.refund_credits.assert_called_once_with(
        "usage-1",
        idempotency_key="refund:replay",
        amount=None,
        reason=None,
        metadata=None,
    )


def test_per_call_strict_override_cannot_inherit_credit_line_floor() -> None:
    plan = SimpleNamespace(
        plan_id="plan-pro",
        credit_policy=SimpleNamespace(type="credit_line", credit_limit=Decimal("7.50")),
        admission=None,
    )
    store = SimpleNamespace(
        get_user_plan=MagicMock(return_value=plan),
        create_lease=MagicMock(return_value=_successful_lease(Decimal("1"))),
        list_quota_events=MagicMock(return_value=[]),
    )
    service = CreditsService(store=cast(Any, store), options=CreditsServiceOptions(policy="overdraft"))

    service.reserve(
        USER_ID,
        Decimal("1"),
        ReserveOptions(billing_mode="strict", idempotency_key="reserve:strict"),
    )

    options = store.create_lease.call_args.args[3]
    assert options.billing_mode == "strict"
    assert options.floor == Decimal("0")
    assert options.overdraft_floor == Decimal("-7.50")
