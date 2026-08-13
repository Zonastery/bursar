from datetime import UTC, datetime
from decimal import Decimal

import pytest

from bursar import (
    ConcurrencyLimitError,
    ConfigError,
    FeatureNotEntitledError,
    InsufficientCreditsError,
    LeaseExpiredError,
    LeaseNotFoundError,
    OperationNotAllowedError,
    QuotaExceededError,
    StoreError,
)
from bursar.credits.postgres.repositories.analytics import AnalyticsRepository
from bursar.credits.postgres.repositories.balance import BalanceRepository
from bursar.credits.postgres.repositories.catalog import CatalogRepository
from bursar.credits.postgres.repositories.deduction import DeductionRepository
from bursar.credits.postgres.repositories.lease import LeaseRepository
from bursar.credits.postgres.repositories.plan import PlanRepository
from bursar.credits.postgres.repositories.schemas import (
    CreateLeaseParams,
    DeductParams,
    SettleLeaseParams,
)
from bursar.credits.postgres.repositories.team import TeamRepository
from bursar.credits.postgres.store import PostgresStore
from bursar.credits.service import CreditsService
from bursar.metrics import UsageMetrics

USER_ID = "00000000-0000-0000-0000-000000000901"
LEASE_ID = "00000000-0000-0000-0000-000000000902"
ENTRY_ID = "00000000-0000-0000-0000-000000000903"
USAGE_ID = "00000000-0000-0000-0000-000000000904"
TEAM_ID = "00000000-0000-0000-0000-000000000905"


def test_usage_metadata_preserves_typed_dimensions() -> None:
    service = object.__new__(CreditsService)
    metadata = service._build_tx_metadata(
        UsageMetrics(
            operation="search",
            measures={"queries": 1},
            dimensions={"provider": "linkup", "max_results": 12, "cached": False},
        ),
        Decimal("0"),
        "search-1",
        None,
    )

    assert metadata.dimensions == {
        "provider": "linkup",
        "max_results": Decimal("12"),
        "cached": False,
    }
    assert metadata.model_dump(mode="json")["dimensions"] == {
        "provider": "linkup",
        "max_results": "12",
        "cached": False,
    }


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
                    "completion",
                    None,
                    "usage-1",
                    {"operation": "completion"},
                    "2030-01-01T00:00:00+00:00",
                )
            ]
        if name == "list_usage_charges":
            return [
                (
                    USAGE_ID,
                    USER_ID,
                    "completion",
                    "10",
                    "8.5",
                    "10",
                    "1.5",
                    "billable",
                    "chat",
                    "gpt-5",
                    "in-west",
                    "2030-01-01T00:00:00+00:00",
                    "usage-1",
                    {"request": "one"},
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
    usage = repository.list_usage_charges(
        USER_ID,
        "2029-01-01",
        "2030-01-01",
        201,
        "2030-01-01T00:00:00+00:00",
        USAGE_ID,
        False,
    )

    assert aggregate.active_users == 3
    assert top[0].user_id == USER_ID
    assert top[0].total_spend == "8.5"
    assert ledger[0].entry_id == ENTRY_ID
    assert ledger[0].operation == "completion"
    assert calls[-2] == (
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
    assert usage[0].usage_id == USAGE_ID
    assert usage[0].allowance_covered == "1.5"
    assert usage[0].billing_disposition == "billable"
    assert calls[-1] == (
        "list_usage_charges",
        [
            USER_ID,
            "2030-01-01T00:00:00+00:00",
            USAGE_ID,
            201,
            "2029-01-01",
            "2030-01-01",
            False,
        ],
    )


def test_lease_repository_uses_revamped_create_and_settle_rpc_shapes() -> None:
    calls: list[tuple[str, list[object]]] = []
    expires_at = datetime(2030, 1, 1, tzinfo=UTC)

    def callproc(name: str, params: list[object]) -> list[object]:
        calls.append((name, params))
        if name == "create_lease_for_operation":
            return [{"lease_id": LEASE_ID, "status": "active", "reserved_amount": "12", "error_code": None}]
        if name == "get_credit_lease":
            return [{"expires_at": expires_at, "minimum_balance": "0"}]
        if name == "settle_lease":
            return [
                {
                    "charge_id": USAGE_ID,
                    "ledger_entry_id": ENTRY_ID,
                    "settled_amount": "8",
                    "replayed": False,
                    "error_code": None,
                }
            ]
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
    assert str(settled.charge_id) == USAGE_ID
    assert settled.balance_after == "92"
    assert settled.allowance_consumed == "2"
    assert settled.bucket_breakdown == {"purchased": "6"}


def test_credit_repository_postconditions_use_canonical_six_place_amounts() -> None:
    def lease_callproc(name: str, _params: list[object]) -> list[object]:
        if name == "settle_lease":
            return [
                {
                    "charge_id": USAGE_ID,
                    "ledger_entry_id": ENTRY_ID,
                    "settled_amount": "8.123457",
                    "replayed": False,
                    "error_code": None,
                }
            ]
        if name == "get_credit_operation_details":
            return [{"balance_after": "91.876543", "allowance_covered": "0", "bucket_breakdown": {}}]
        raise AssertionError(f"unexpected RPC: {name}")

    settled = LeaseRepository(lease_callproc).settle_lease(
        SettleLeaseParams(
            user_id=USER_ID,
            lease_id=LEASE_ID,
            amount="8.1234565",
            idempotency_key="chat:canonical-settle",
            feature=None,
            model=None,
            measures="{}",
            dimensions="{}",
            metadata="{}",
        )
    )

    def team_callproc(name: str, _params: list[object]) -> list[object]:
        assert name == "deduct_team"
        return [
            {
                "entry_id": ENTRY_ID,
                "team_id": TEAM_ID,
                "subject_id": USER_ID,
                "amount": "1.000001",
                "balance_after": "98.999999",
                "replayed": False,
                "error_code": None,
            }
        ]

    team_charge = TeamRepository(team_callproc).deduct_team(
        TEAM_ID,
        USER_ID,
        "1.0000005",
        "team:canonical-charge",
        "team_usage",
        "{}",
    )

    assert Decimal(str(settled.amount)) == Decimal("8.123457")
    assert Decimal(str(team_charge.amount)) == Decimal("1.000001")


def test_lease_repository_renews_through_official_rpc() -> None:
    calls: list[tuple[str, list[object]]] = []
    expires_at = datetime(2030, 1, 1, tzinfo=UTC)

    def callproc(name: str, params: list[object]) -> list[object]:
        calls.append((name, params))
        if name == "renew_lease":
            return [{"lease_id": LEASE_ID, "status": "active", "reserved_amount": "12", "error_code": None}]
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


def test_balance_repository_hydrates_scalar_destination_bucket() -> None:
    calls: list[tuple[str, list[object]]] = []

    def callproc(name: str, params: list[object]) -> list[object]:
        calls.append((name, params))
        if name == "post_credit":
            return [
                {
                    "entry_id": ENTRY_ID,
                    "balance_after": "10",
                    "replayed": False,
                    "error_code": None,
                }
            ]
        if name == "get_credit_state":
            return [{"lifetime_purchased": "10"}]
        if name == "get_credit_grant_details":
            return ["default"]
        raise AssertionError(f"unexpected RPC: {name}")

    result = BalanceRepository(callproc).add_credits(
        USER_ID,
        "10",
        "purchase",
        "{}",
        None,
        None,
        "purchase-1",
    )

    assert result.bucket == "default"
    assert [name for name, _params in calls] == [
        "post_credit",
        "get_credit_state",
        "get_credit_grant_details",
    ]


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
    assert str(awards[0].recipient_subject_id) == USER_ID


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


def test_catalog_repository_fetches_historical_revision_instead_of_active_revision() -> None:
    calls: list[tuple[str, list[object]]] = []

    def callproc(name: str, params: list[object]) -> list[object]:
        calls.append((name, params))
        assert name == "catalog_revision_by_number"
        return [
            {
                "id": "00000000-0000-0000-0000-000000000905",
                "revision_no": 2,
                "status": "retired",
                "source_document": {"version": 1},
                "label": None,
                "created_at": "2030-01-01T00:00:00Z",
            }
        ]

    revision = CatalogRepository(callproc).get_catalog_revision(2)

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
                "plan_assigned_at": datetime(2026, 1, 1, tzinfo=UTC),
                "plan_assignment_ends_at": None,
                "assignment_source_type": "manual",
                "assignment_source_id": None,
                "catalog_revision_pinned": False,
                "rate_card": "standard",
                "allowed_operations": ["completion"],
                "credit_allowance_amount": "100",
                "credit_allowance_priority": 15,
                "credit_allowance_reset_unit": "month",
                "credit_allowance_reset_count": 1,
                "credit_allowance_reset_anchor": "calendar",
                "credit_allowance_reset_timezone": "UTC",
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
    assert plan.operation_admission["completion"].max_in_flight == 1

    store = object.__new__(PostgresStore)
    vars(store)["_plan_repo"] = repository
    public_plan = store.get_user_plan(USER_ID)

    assert public_plan.catalog_version == 4
    assert public_plan.allowance is not None
    assert public_plan.allowance.priority == 15
    assert public_plan.credit_policy is not None
    assert public_plan.credit_policy.type == "credit_line"
    assert public_plan.credit_policy.credit_limit == Decimal("20")
    assert public_plan.admission is not None
    assert public_plan.admission.operations["completion"].max_in_flight == 1


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

    result = DeductionRepository(callproc).refund_credits(
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
    assert str(result.user_id) == USER_ID
    assert result.amount == "8"


def test_deduction_repository_preserves_operation_usage_dimensions() -> None:
    calls: list[tuple[str, list[object]]] = []

    def callproc(name: str, params: list[object]) -> list[object]:
        calls.append((name, params))
        if name == "charge_usage_for_operation":
            return [
                {
                    "charge_id": USAGE_ID,
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

    result = DeductionRepository(callproc).deduct_with_allowance(
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
    assert str(result.user_id) == USER_ID
    assert result.balance_after == "90"
    assert result.bucket_breakdown == {"purchased": "8"}


def test_deduction_repository_records_child_usage_without_a_ledger_debit() -> None:
    calls: list[tuple[str, list[object]]] = []

    def callproc(name: str, params: list[object]) -> list[object]:
        calls.append((name, params))
        assert name == "record_usage"
        return [
            {
                "charge_id": USAGE_ID,
                "requested": "12",
                "ledger_entry_id": None,
                "charged": "0",
                "allowance_covered": "0",
                "replayed": False,
                "error_code": None,
            }
        ]

    result = DeductionRepository(callproc).record_usage(
        DeductParams(
            user_id=USER_ID,
            operation="roadmap_gen",
            amount="12",
            idempotency_key="roadmap-1:usage:outline",
            feature=None,
            model=None,
            region=None,
            metadata='{"usage_kind":"job_event"}',
            measures='{"jobs":0}',
            dimensions='{"model":"linkup"}',
        )
    )

    assert result is not None
    assert str(result.charge_id) == USAGE_ID
    assert result.requested == "12"
    assert calls[0] == (
        "record_usage",
        [
            USER_ID,
            "roadmap_gen",
            "12",
            "roadmap-1:usage:outline",
            None,
            None,
            None,
            '{"usage_kind":"job_event"}',
            '{"jobs":0}',
            '{"model":"linkup"}',
        ],
    )


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


@pytest.mark.parametrize(
    ("error", "exception"),
    [
        ("feature_not_entitled", FeatureNotEntitledError),
        ("operation_not_allowed", OperationNotAllowedError),
        ("missing_quota_measure", ConfigError),
        ("insufficient_headroom", InsufficientCreditsError),
        ("idempotency_conflict", StoreError),
    ],
)
def test_deduction_error_codes_map_to_public_domain_errors(
    error: str,
    exception: type[Exception],
) -> None:
    service = object.__new__(CreditsService)
    service._emitter = None
    metrics = UsageMetrics(operation="completion", measures={}, dimensions={})

    with pytest.raises(exception):
        service._raise_deduct_error(error, USER_ID, Decimal("1"), metrics)
