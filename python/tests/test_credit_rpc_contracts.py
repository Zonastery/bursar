from datetime import UTC, datetime
from decimal import Decimal

import pytest

from bursar import (
    ConcurrencyLimitError,
    FeatureNotEntitledError,
    InsufficientCreditsError,
    LeaseExpiredError,
    LeaseNotFoundError,
    OperationNotAllowedError,
    QuotaExceededError,
)
from bursar.credits.postgres.repositories.analytics import AnalyticsRepository
from bursar.credits.postgres.repositories.balance import BalanceRepository
from bursar.credits.postgres.repositories.deduction import DeductionRepository
from bursar.credits.postgres.repositories.lease import LeaseRepository
from bursar.credits.postgres.repositories.plan import PlanRepository
from bursar.credits.postgres.repositories.pricing import PricingRepository
from bursar.credits.postgres.repositories.schemas import (
    CreateLeaseParams,
    DeductParams,
    SettleLeaseParams,
)
from bursar.credits.postgres.repositories.team import TeamRepository
from bursar.credits.postgres.store import PostgresStore
from bursar.credits.service import CreditsService

USER_ID = "00000000-0000-0000-0000-000000000901"
LEASE_ID = "00000000-0000-0000-0000-000000000902"
ENTRY_ID = "00000000-0000-0000-0000-000000000903"


def test_analytics_repository_mirrors_canonical_rpc_shapes_and_aliases() -> None:
    calls: list[tuple[str, list[object]]] = []

    def callproc(name: str, params: list[object]) -> list[object]:
        calls.append((name, params))
        if name == "aggregate_usage_stats":
            return [("12.5", 3, "4.1", "gpt-5", USER_ID)]
        if name == "spend_by_user":
            return [(USER_ID, "8.5", 2)]
        if name == "list_ledger":
            return [
                (
                    ENTRY_ID,
                    USER_ID,
                    None,
                    "-8.5",
                    "usage",
                    None,
                    "usage-1",
                    {"operation": "completion"},
                    "2030-01-01T00:00:00+00:00",
                )
            ]
        raise AssertionError(f"unexpected RPC: {name}")

    repository = AnalyticsRepository(callproc)
    aggregate = repository.aggregate_stats("2029-01-01", "2030-01-01")
    top = repository.top_users(1, "2029-01-01", "2030-01-01")
    ledger = repository.list_ledger_entries(
        USER_ID,
        ["usage"],
        "2029-01-01",
        "2030-01-01",
        25,
        "2030-01-01T00:00:00+00:00",
        ENTRY_ID,
        usage_only=True,
    )

    assert aggregate.active_users == 3
    assert top[0].user_id == USER_ID
    assert top[0].total_spend == "8.5"
    assert ledger[0].entry_id == ENTRY_ID
    assert calls[-1] == (
        "list_ledger",
        [
            USER_ID,
            "2030-01-01T00:00:00+00:00",
            ENTRY_ID,
            25,
            ["usage"],
            "2029-01-01",
            "2030-01-01",
            True,
        ],
    )


def test_lease_repository_uses_revamped_create_and_settle_rpc_shapes() -> None:
    calls: list[tuple[str, list[object]]] = []
    expires_at = datetime(2030, 1, 1, tzinfo=UTC)

    def callproc(name: str, params: list[object]) -> list[object]:
        calls.append((name, params))
        if name == "create_lease_for_operation":
            return [{"lease_id": LEASE_ID, "reserved_amount": "12", "error_code": None}]
        if name == "get_credit_lease":
            return [{"expires_at": expires_at, "minimum_balance": "0"}]
        if name == "settle_lease":
            return [{"ledger_entry_id": ENTRY_ID, "settled_amount": "8", "replayed": False, "error_code": None}]
        if name == "get_credit_operation_details":
            return [{"balance_after": "92", "allowance_covered": "2", "bucket_breakdown": {"purchased": "6"}}]
        raise AssertionError(f"unexpected RPC: {name}")

    repository = LeaseRepository(callproc)
    created = repository.create_lease(
        CreateLeaseParams(
            user_id=USER_ID,
            amount="12",
            operation_type="completion",
            idempotency_key="chat:reserve",
            ttl_seconds=600,
            metadata='{"reference_type":"chat"}',
            feature="tutor_chat",
            measures='{"input_tokens":"10"}',
            dimensions='{"model":"test-model"}',
            minimum_balance="0",
            max_concurrent=1,
        )
    )
    settled = repository.settle_lease(
        SettleLeaseParams(
            user_id=USER_ID,
            lease_id=LEASE_ID,
            amount="8",
            idempotency_key="chat:settle",
            feature="tutor_chat",
            model="test-model",
            measures='{"input_tokens":"6"}',
            dimensions='{"model":"test-model"}',
            metadata='{"reference_type":"chat"}',
        )
    )

    assert calls[0] == (
        "create_lease_for_operation",
        [
            USER_ID,
            "completion",
            "12",
            "chat:reserve",
            "600 seconds",
            '{"reference_type":"chat"}',
            "tutor_chat",
            '{"input_tokens":"10"}',
            '{"model":"test-model"}',
            "0",
            1,
        ],
    )
    assert calls[2] == (
        "settle_lease",
        [
            USER_ID,
            LEASE_ID,
            "8",
            "chat:settle",
            "tutor_chat",
            "test-model",
            None,
            '{"input_tokens":"6"}',
            '{"model":"test-model"}',
            '{"reference_type":"chat"}',
        ],
    )
    assert created is not None
    assert created.expires_at == expires_at
    assert settled is not None
    assert settled.balance_after == "92"
    assert settled.allowance_consumed == "2"
    assert settled.bucket_breakdown == {"purchased": "6"}


def test_lease_repository_renews_through_official_rpc() -> None:
    calls: list[tuple[str, list[object]]] = []
    expires_at = datetime(2030, 1, 1, tzinfo=UTC)

    def callproc(name: str, params: list[object]) -> list[object]:
        calls.append((name, params))
        if name == "renew_lease":
            return [{"lease_id": LEASE_ID, "reserved_amount": "12", "error_code": None}]
        if name == "get_credit_lease":
            return [{"expires_at": expires_at, "minimum_balance": "0"}]
        raise AssertionError(f"unexpected RPC: {name}")

    renewed = LeaseRepository(callproc).renew_lease(USER_ID, LEASE_ID, 300)

    assert calls[0] == ("renew_lease", [USER_ID, LEASE_ID, "300 seconds"])
    assert renewed is not None
    assert renewed.expires_at == expires_at


def test_lease_repository_expires_a_bounded_batch() -> None:
    calls: list[tuple[str, list[object]]] = []

    def callproc(name: str, params: list[object]) -> list[object]:
        calls.append((name, params))
        return [3]

    expired = LeaseRepository(callproc).expire_leases(25)

    assert expired == 3
    assert calls == [("expire_leases", [25])]


def test_balance_repository_executes_every_grant_program_award() -> None:
    calls: list[tuple[str, list[object]]] = []

    def callproc(name: str, params: list[object]) -> list[object]:
        calls.append((name, params))
        return [
            {
                "grant_event_id": "00000000-0000-0000-0000-000000000910",
                "grant_award_id": "00000000-0000-0000-0000-000000000911",
                "recipient_subject_id": USER_ID,
                "ledger_entry_id": ENTRY_ID,
                "amount": "12.5",
                "replayed": False,
                "error_code": None,
            }
        ]

    awards = BalanceRepository(callproc).execute_grant_program(
        "manual",
        "welcome_bonus",
        USER_ID,
        "event-42",
        None,
        "US",
        '{"campaign":"summer"}',
    )

    assert calls == [
        (
            "execute_grant_program",
            ["manual", "welcome_bonus", USER_ID, "event-42", None, "US", '{"campaign":"summer"}'],
        )
    ]
    assert len(awards) == 1
    assert awards[0].amount == "12.5"
    assert awards[0].recipient_subject_id == USER_ID


def test_team_repository_removes_member_through_official_rpc() -> None:
    calls: list[tuple[str, list[object]]] = []

    def callproc(name: str, params: list[object]) -> list[object]:
        calls.append((name, params))
        return [True]

    removed = TeamRepository(callproc).remove_team_member("team-1", USER_ID)

    assert removed is True
    assert calls == [("remove_team_member", ["team-1", USER_ID])]


def test_lease_repository_reads_pinned_pricing_context_through_official_rpc() -> None:
    calls: list[tuple[str, list[object]]] = []

    def callproc(name: str, params: list[object]) -> list[object]:
        calls.append((name, params))
        return [
            {
                "catalog_revision_no": 3,
                "plan_id": "00000000-0000-0000-0000-000000000904",
                "plan_key": "pro",
                "rate_card": "pro-rates",
            }
        ]

    context = LeaseRepository(callproc).get_pricing_context(USER_ID, LEASE_ID)

    assert calls == [("get_credit_lease_pricing_context", [USER_ID, LEASE_ID])]
    assert context is not None
    assert context.catalog_revision_no == 3
    assert context.plan_key == "pro"
    assert context.rate_card == "pro-rates"


def test_pricing_repository_fetches_historical_revision_instead_of_active_revision() -> None:
    calls: list[tuple[str, list[object]]] = []

    def callproc(name: str, params: list[object]) -> list[object]:
        calls.append((name, params))
        assert name == "catalog_revision_by_number"
        return [
            {
                "id": "00000000-0000-0000-0000-000000000905",
                "revision_no": 2,
                "status": "superseded",
                "source_document": {"version": 1},
            }
        ]

    revision = PricingRepository(callproc).get_bursar_config(2)

    assert calls == [("catalog_revision_by_number", [2])]
    assert revision is not None
    assert revision.version == 2
    assert revision.active is False
    assert revision.config == {"version": 1}


def test_plan_repository_uses_public_subject_plan_projection() -> None:
    calls: list[tuple[str, list[object]]] = []

    def callproc(name: str, params: list[object]) -> list[object]:
        calls.append((name, params))
        return [
            {
                "user_id": USER_ID,
                "plan_id": "00000000-0000-0000-0000-000000000906",
                "plan_key": "pro",
                "plan_label": "Pro",
                "credit_allowance_amount": "100",
                "credit_policy_type": "credit_line",
                "credit_limit": "20",
                "admission_max_in_flight": 3,
                "operation_admission": {"completion": {"max_in_flight": 1}},
                "catalog_revision_no": 4,
                "entitlements": {"tutor_chat": {"value": True}},
            }
        ]

    repository = PlanRepository(callproc, lambda _query, _params: [])
    plan = repository.get_user_plan(USER_ID)

    assert calls == [("get_subject_plan", [USER_ID])]
    assert plan is not None
    assert plan.plan_key == "pro"
    assert plan.catalog_revision_no == 4
    assert plan.credit_policy_type == "credit_line"
    assert plan.credit_limit == "20"
    assert plan.operation_admission == {"completion": {"max_in_flight": 1}}

    store = object.__new__(PostgresStore)
    store._plan_repo_cache = repository
    public_plan = store.get_user_plan(USER_ID)

    assert public_plan.catalog_version == 4
    assert public_plan.billing_mode == "overdraft"
    assert public_plan.overdraft_floor == Decimal("-20")
    assert public_plan.credit_policy is not None
    assert public_plan.credit_policy.credit_limit == Decimal("20")
    assert public_plan.admission is not None
    assert public_plan.admission.operations["completion"].max_in_flight == 1
    assert public_plan.per_operation["completion"].max_concurrent == 1


def test_refund_repository_uses_entry_scoped_idempotent_rpc() -> None:
    calls: list[tuple[str, list[object]]] = []

    def callproc(name: str, params: list[object]) -> list[object]:
        calls.append((name, params))
        return [
            {
                "entry_id": ENTRY_ID,
                "subject_id": USER_ID,
                "amount": "8",
                "balance_after": "100",
                "replayed": False,
                "error_code": None,
            }
        ]

    result = DeductionRepository(callproc, callproc).refund_credits(
        ENTRY_ID,
        None,
        "curriculum:job-1:refund",
        "curriculum_pipeline_failed",
        '{"job_id":"job-1"}',
    )

    assert calls == [
        (
            "refund_credit_by_entry",
            [
                ENTRY_ID,
                None,
                "curriculum:job-1:refund",
                "curriculum_pipeline_failed",
                '{"job_id":"job-1"}',
            ],
        )
    ]
    assert result is not None
    assert result.user_id == USER_ID
    assert result.amount == "8"


def test_deduction_repository_preserves_operation_usage_dimensions() -> None:
    calls: list[tuple[str, list[object]]] = []

    def callproc(name: str, params: list[object]) -> list[object]:
        calls.append((name, params))
        if name == "charge_usage_for_operation":
            return [
                {
                    "ledger_entry_id": ENTRY_ID,
                    "charged": "8",
                    "allowance_covered": "2",
                    "replayed": False,
                    "error_code": None,
                }
            ]
        if name == "get_credit_operation_details":
            return [
                {
                    "balance_after": "90",
                    "bucket_breakdown": {"purchased": "8"},
                }
            ]
        raise AssertionError(f"unexpected RPC: {name}")

    result = DeductionRepository(callproc, callproc).deduct_with_allowance(
        DeductParams(
            user_id=USER_ID,
            operation="completion",
            amount="10",
            idempotency_key="usage-1",
            feature="chat",
            model="gpt-5",
            region="in-west",
            measures='{"tokens":100}',
            dimensions='{"model":"gpt-5","region":"in-west"}',
            metadata='{"request":"one"}',
        )
    )

    assert calls[0] == (
        "charge_usage_for_operation",
        [
            USER_ID,
            "completion",
            "10",
            "usage-1",
            "chat",
            "gpt-5",
            "in-west",
            '{"request":"one"}',
            '{"tokens":100}',
            '{"model":"gpt-5","region":"in-west"}',
        ],
    )
    assert result is not None
    assert result.balance_after == "90"
    assert result.bucket_breakdown == {"purchased": "8"}


@pytest.mark.parametrize(
    ("error", "exception"),
    [
        ("max_concurrent_reached", ConcurrencyLimitError),
        ("quota_exceeded", QuotaExceededError),
        ("feature_not_entitled", FeatureNotEntitledError),
        ("operation_not_allowed", OperationNotAllowedError),
        ("insufficient_headroom", InsufficientCreditsError),
        ("expired_lease", LeaseExpiredError),
        ("missing_lease", LeaseNotFoundError),
        ("released_lease", LeaseNotFoundError),
    ],
)
def test_revamped_lease_error_codes_map_to_public_domain_errors(
    error: str,
    exception: type[Exception],
) -> None:
    service = object.__new__(CreditsService)

    with pytest.raises(exception):
        service._raise_lease_error(error, USER_ID, Decimal("1"))
