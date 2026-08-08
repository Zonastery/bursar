from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from bursar.credits.service import CreditsService
from bursar.credits.service_types import GrantSubscriptionCycleOptions
from bursar.credits.types import SetUserPlanResult
from bursar.engine import PricingEngine
from bursar.metrics import UsageMetrics

CONFIG = {
    "version": 1,
    "pricing": {
        "operations": {
            "completion": {
                "measures": {"tokens": {"unit": "token"}},
                "dimensions": {},
            }
        },
        "rate_cards": {
            "standard": {
                "operations": {
                    "completion": {
                        "rules": [],
                        "unmatched": {
                            "action": "charge",
                            "charge": {"type": "flat", "amount": "1"},
                        },
                    }
                }
            }
        },
    },
    "credits": {
        "accounting": {"unit": "credit", "scale": 6, "rounding": "half_up"},
        "buckets": {"default": {"priority": 1, "expiry": {"type": "never"}}},
        "default_bucket": "default",
    },
}


def _service(store: object) -> CreditsService:
    return CreditsService(store=cast(Any, store))


def test_catalog_is_installed_only_after_publication_succeeds() -> None:
    original = PricingEngine.from_dict(CONFIG)
    store = SimpleNamespace(publish_and_activate_catalog=MagicMock(return_value="revision-2"))
    credits = CreditsService(store=cast(Any, store), engine=original)

    assert credits.publish_and_activate_catalog(CONFIG, "release-2") == "revision-2"
    assert credits.pricing_engine is not original


def test_catalog_publication_failure_keeps_the_committed_engine() -> None:
    original = PricingEngine.from_dict(CONFIG)
    store = SimpleNamespace(publish_and_activate_catalog=MagicMock(side_effect=RuntimeError("write failed")))
    credits = CreditsService(store=cast(Any, store), engine=original)

    with pytest.raises(RuntimeError, match="write failed"):
        credits.publish_and_activate_catalog(CONFIG)

    assert credits.pricing_engine is original


@pytest.mark.parametrize(
    "invoke",
    [
        lambda credits: credits.add_credits("user-1", -1),
        lambda credits: credits.deduct_credits("user-1", -1),
        lambda credits: credits.grant_subscription_cycle(
            "user-1",
            0,
            GrantSubscriptionCycleOptions(),
        ),
        lambda credits: credits.refund_credits("entry-1", amount=0),
    ],
)
def test_invalid_public_amounts_are_rejected_before_store_call(invoke: Any) -> None:
    store = SimpleNamespace(add_credits=MagicMock(), refund_credits=MagicMock())
    credits = _service(store)

    with pytest.raises(ValueError, match="finite and greater than zero"):
        invoke(credits)

    store.add_credits.assert_not_called()
    store.refund_credits.assert_not_called()


def test_negative_raw_lease_amount_is_rejected_before_admission() -> None:
    store = SimpleNamespace(
        get_user_plan=MagicMock(return_value=SimpleNamespace(plan_id=None)),
        create_lease=MagicMock(),
    )
    credits = _service(store)

    with pytest.raises(ValueError, match="finite and non-negative"):
        credits.reserve("user-1", Decimal("-1"))

    store.create_lease.assert_not_called()


def test_boolean_amount_is_not_treated_as_one_credit() -> None:
    store = SimpleNamespace(add_credits=MagicMock())
    credits = _service(store)

    with pytest.raises(ValueError, match="Decimal or integer"):
        credits.add_credits("user-1", True)

    store.add_credits.assert_not_called()


def test_integer_usage_inputs_are_normalized_to_decimal() -> None:
    metrics = UsageMetrics(
        operation="completion",
        measures={"calls": 1, "tokens": 42},
        dimensions={"max_results": 12},
    )

    assert metrics.measures == {"calls": Decimal(1), "tokens": Decimal(42)}
    assert metrics.dimensions == {"max_results": Decimal(12)}


def test_cycle_replay_does_not_reanchor_an_existing_plan() -> None:
    store = SimpleNamespace(
        add_credits=MagicMock(
            return_value=SimpleNamespace(
                entry_id="entry-1",
                amount=Decimal(10),
                new_balance=Decimal(10),
                idempotent=True,
            )
        ),
        get_user_plan=MagicMock(return_value=SimpleNamespace(plan_key="pro")),
        set_user_plan=MagicMock(),
    )
    credits = _service(store)

    credits.grant_subscription_cycle(
        "user-1",
        10,
        GrantSubscriptionCycleOptions(
            plan_key="pro",
            idempotency_key="invoice-1",
        ),
    )

    store.set_user_plan.assert_not_called()


def test_cycle_replay_repairs_an_interrupted_plan_assignment() -> None:
    store = SimpleNamespace(
        add_credits=MagicMock(
            return_value=SimpleNamespace(
                entry_id="entry-1",
                amount=Decimal(10),
                new_balance=Decimal(10),
                idempotent=True,
            )
        ),
        get_user_plan=MagicMock(return_value=SimpleNamespace(plan_key="free")),
        set_user_plan=MagicMock(
            return_value=SetUserPlanResult(
                user_id="user-1",
                plan_id="plan-pro",
                plan_key="pro",
                plan_assigned_at=datetime(2026, 1, 1, tzinfo=UTC),
                assignment_state="applied",
            )
        ),
    )
    credits = _service(store)

    credits.grant_subscription_cycle(
        "user-1",
        10,
        GrantSubscriptionCycleOptions(
            plan_key="pro",
            idempotency_key="invoice-1",
        ),
    )

    store.set_user_plan.assert_called_once_with("user-1", "pro", plan_assigned_at=None)
