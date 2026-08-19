"""Focused PostgreSQL coverage for public credit-store workflows."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import psycopg2
import pytest

from bursar.bursar import Bursar
from bursar.config.types import CatalogRollout, PlanRollout
from bursar.credits.events import CreditEvent
from bursar.credits.postgres.store import PostgresStore
from bursar.credits.service import CreditsService
from bursar.credits.service_types import (
    CanAffordOptions,
    CreditsServiceOptions,
    LowBalanceConfig,
    PostDeductionContext,
)
from bursar.credits.types import CreditMetadata
from bursar.errors import CapReachedError, InsufficientCreditsError
from bursar.metrics import UsageMetrics
from tests.conftest import TEST_TENANT_ID
from tests.test_store_integration import CONFIG

pytestmark = [pytest.mark.integration]


TEAM_OWNER_ID = "00000000-0000-0000-0000-000000000931"
TEAM_MEMBER_ID = "00000000-0000-0000-0000-000000000932"
TEAM_IDEMPOTENCY_KEY = "credit-confidence:team:create"
LOW_BALANCE_USER_ID = "00000000-0000-0000-0000-000000000933"
AFFORDABILITY_USER_ID = "00000000-0000-0000-0000-000000000934"
LEDGER_USER_ID = "00000000-0000-0000-0000-000000000935"
PLAN_USER_ID = "00000000-0000-0000-0000-000000000936"
ONBOARDING_USER_ID = "00000000-0000-0000-0000-000000000937"


def _usage(input_tokens: int, output_tokens: int = 0, model: str = "standard") -> UsageMetrics:
    return UsageMetrics(
        operation="completion",
        measures={"input_tokens": Decimal(input_tokens), "output_tokens": Decimal(output_tokens)},
        dimensions={"model": model},
    )


def _plan_config() -> dict[str, object]:
    config = deepcopy(CONFIG)
    config["entitlements"] = {
        "features": {
            "priority_support": {"type": "boolean", "default": False},
        }
    }
    config["plans"] = {
        "pro": {
            "display_name": "Pro",
            "rank": 0,
            "rate_card": "standard",
            "allowed_operations": ["completion"],
            "features": {"priority_support": True},
            "quotas": {
                "completion_input": {
                    "operation": "completion",
                    "measure": "input_tokens",
                    "limit": "5",
                    "window": {
                        "type": "calendar",
                        "unit": "month",
                        "count": 1,
                        "timezone": "UTC",
                    },
                    "enforcement": "block",
                }
            },
        },
        "basic": {
            "display_name": "Basic",
            "rank": 1,
            "rate_card": "standard",
            "allowed_operations": ["completion"],
        },
    }
    return config


def test_team_deduction_replay_caps_and_insufficient_balance(pg_store: PostgresStore) -> None:
    store = pg_store
    service = CreditsService(store=store)
    service.publish_and_activate_catalog(deepcopy(CONFIG))
    team = store.create_team(
        TEAM_OWNER_ID,
        "Credit confidence team",
        Decimal("5"),
        idempotency_key=TEAM_IDEMPOTENCY_KEY,
    )
    store.add_team_member(team.team_id, TEAM_MEMBER_ID, spend_cap=Decimal("2"))

    first = service.deduct_team(
        team.team_id,
        TEAM_MEMBER_ID,
        _usage(2),
        idempotency_key="credit-confidence:team:debit",
    )
    replay = service.deduct_team(
        team.team_id,
        TEAM_MEMBER_ID,
        _usage(2),
        idempotency_key="credit-confidence:team:debit",
    )
    assert first.amount == Decimal("2")
    assert first.team_balance_after == Decimal("3")
    assert replay.idempotent is True
    assert replay.entry_id == first.entry_id

    with pytest.raises(CapReachedError):
        service.deduct_team(
            team.team_id,
            TEAM_MEMBER_ID,
            _usage(2),
            idempotency_key="credit-confidence:team:cap",
        )
    with pytest.raises(InsufficientCreditsError):
        service.deduct_team(
            team.team_id,
            TEAM_OWNER_ID,
            _usage(4),
            idempotency_key="credit-confidence:team:insufficient",
        )

    balance = store.get_team_balance(team.team_id)
    assert balance is not None
    assert balance.balance == Decimal("3")
    assert balance.member_count == 2
    assert store.get_team_members(team.team_id)[1].total_spent == Decimal("2")


def test_low_balance_rearms_callback_and_can_afford_reports_policy_outcomes(
    pg_store: PostgresStore,
) -> None:
    store = pg_store
    callback_balances: list[Decimal] = []

    def on_low_balance(event: CreditEvent) -> None:
        data = event.data or {}
        callback_balances.append(data["balance"])
        if len(callback_balances) == 1:
            raise RuntimeError("notification sink unavailable")

    service = CreditsService(
        store=store,
        options=CreditsServiceOptions(
            low_balance=LowBalanceConfig(
                thresholds=["5", "2"],
                on_trigger=on_low_balance,
            )
        ),
    )
    service.publish_and_activate_catalog(deepcopy(CONFIG))
    service.add_credits(
        LOW_BALANCE_USER_ID,
        Decimal("10"),
        entry_type="purchase",
        idempotency_key="credit-confidence:low-balance:grant",
    )

    service.deduct(
        LOW_BALANCE_USER_ID,
        _usage(8),
        idempotency_key="credit-confidence:low-balance:first",
    )
    assert callback_balances == [Decimal("2")]
    service.add_credits(
        LOW_BALANCE_USER_ID,
        Decimal("5"),
        entry_type="purchase",
        idempotency_key="credit-confidence:low-balance:rearm",
    )
    service.deduct(
        LOW_BALANCE_USER_ID,
        _usage(5),
        idempotency_key="credit-confidence:low-balance:second",
    )
    assert callback_balances == [Decimal("2"), Decimal("2")]
    assert service.get_balance(LOW_BALANCE_USER_ID).balance == Decimal("2")

    service.add_credits(
        AFFORDABILITY_USER_ID,
        Decimal("3"),
        entry_type="purchase",
        idempotency_key="credit-confidence:affordability:grant",
    )
    assert service.can_afford(AFFORDABILITY_USER_ID, Decimal("2")).affordable is True
    insufficient = service.can_afford(AFFORDABILITY_USER_ID, Decimal("4"))
    assert insufficient.affordable is False
    assert insufficient.reason == "insufficient_credits"
    feature_denied = service.can_afford(
        AFFORDABILITY_USER_ID,
        Decimal("1"),
        CanAffordOptions(feature="unconfigured_feature"),
    )
    assert feature_denied.affordable is False
    assert feature_denied.reason == "feature_not_entitled"


def test_post_deduction_hooks_are_awaited_isolated_and_removable(pg_store: PostgresStore) -> None:
    observed: list[tuple[str, Decimal]] = []

    def failing_hook(_context: PostDeductionContext) -> None:
        raise RuntimeError("notification backend unavailable")

    async def recording_hook(context: PostDeductionContext) -> None:
        await asyncio.sleep(0)
        observed.append((context.source, context.deduction.amount))

    service = CreditsService(
        store=pg_store,
        options=CreditsServiceOptions(post_deduction=failing_hook),
    )
    service.publish_and_activate_catalog(deepcopy(CONFIG))
    service.add_credits(
        AFFORDABILITY_USER_ID,
        Decimal("5"),
        entry_type="purchase",
        idempotency_key="credit-confidence:hooks:grant",
    )
    remove_recording_hook = service.add_post_deduction_hook(recording_hook)

    service.deduct_credits(
        AFFORDABILITY_USER_ID,
        Decimal("1"),
        idempotency_key="credit-confidence:hooks:sync",
    )

    async def deduct_inside_event_loop() -> None:
        service.deduct_credits(
            AFFORDABILITY_USER_ID,
            Decimal("2"),
            idempotency_key="credit-confidence:hooks:async",
        )

    asyncio.run(deduct_inside_event_loop())
    remove_recording_hook()
    service.deduct_credits(
        AFFORDABILITY_USER_ID,
        Decimal("1"),
        idempotency_key="credit-confidence:hooks:removed",
    )

    assert observed == [("raw", Decimal("1")), ("raw", Decimal("2"))]
    revoked = service.revoke_credits_by_entry_type(AFFORDABILITY_USER_ID, "purchase")
    assert revoked.revoked == Decimal("1")
    assert service.revoke_credits_by_entry_type(AFFORDABILITY_USER_ID, "purchase").revoked == Decimal("0")
    assert service.get_balance(AFFORDABILITY_USER_ID).balance == Decimal("0")


def test_catalog_plan_quota_and_migration_facade(pg_store: PostgresStore) -> None:
    store = pg_store
    service = CreditsService(store=store)
    config = _plan_config()
    service.publish_and_activate_catalog(config, label="initial-credit-confidence")
    service.set_user_plan(PLAN_USER_ID, "pro")
    assert service.set_plan_revision_pin(PLAN_USER_ID, True) is True
    assert service.get_user_plan(PLAN_USER_ID).catalog_revision_pinned is True
    assert service.set_plan_revision_pin(PLAN_USER_ID, False) is True

    feature = service.check_feature(PLAN_USER_ID, "priority_support")
    assert feature.has_feature is True
    quota = service.get_quota_state(PLAN_USER_ID, "completion_input")
    assert len(quota) == 1
    assert quota[0].remaining == Decimal("5")
    assert service.list_quota_events(PLAN_USER_ID) == []

    draft_config = deepcopy(config)
    draft_config["plans"]["basic"]["rank"] = 2  # type: ignore[index]
    draft_id = service.publish_catalog_draft(draft_config, label="draft-credit-confidence")
    history = store.get_catalog_history()
    draft = next(item for item in history if item.id == draft_id)
    assert draft.active is False
    service.activate_catalog_revision(draft.version)
    service.invalidate_catalog()
    service.refresh_catalog_if_stale()
    assert service.pricing_engine is not None
    assert service.get_active_catalog() is not None

    with psycopg2.connect(store.database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT plan.id
            FROM bursar.catalog_plans AS plan
            JOIN bursar.catalog_revisions AS revision
              ON revision.id = plan.catalog_revision_id
            WHERE revision.status = 'active' AND plan.plan_key = 'basic'
            """
        )
        target_row = cursor.fetchone()
    assert target_row is not None

    source_plan_id = service.get_user_plan(PLAN_USER_ID).plan_id
    assert source_plan_id is not None
    migration = service.start_plan_migration(source_plan_id, str(target_row[0]))
    batch = service.migrate_plan_batch(migration.migration_id, batch_size=10)
    assert batch.migrated == 1
    assert batch.done is True
    assert service.get_user_plan(PLAN_USER_ID).plan_key == "basic"


def test_bursar_facade_onboards_an_account_exactly_once(pg_store: PostgresStore) -> None:
    config = deepcopy(CONFIG)
    config["credits"]["grant_programs"] = {  # type: ignore[index]
        "referral": {
            "trigger": "referral_completed",
            "awards": [{"recipient": "subject", "amount": "3", "bucket": "purchased"}],
        },
        "welcome": {
            "trigger": "account_created",
            "awards": [{"recipient": "subject", "amount": "5", "bucket": "purchased"}],
            "max_awards_per_subject": 1,
            "idempotency_scope": "subject",
        },
    }
    config["plans"]["pro"].update(  # type: ignore[index]
        {
            "description": "Production plan",
            "allowed_operations": ["completion"],
            "quotas": {
                "assignment_tokens": {
                    "operation": "completion",
                    "measure": "input_tokens",
                    "limit": "1000",
                    "window": {
                        "type": "plan_assignment",
                        "interval": {"unit": "month", "count": 1},
                        "timezone": "UTC",
                    },
                    "enforcement": "block",
                }
            },
        }
    )
    config["commerce"] = {
        "providers": {"stripe": {"type": "stripe"}},
        "offers": {
            "credits_100": {
                "type": "topup",
                "display_name": "100 credits",
                "description": "Production credit pack",
                "credits_per_unit": "100",
                "quantity": {"minimum": 1, "maximum": 10, "default": 1},
                "bucket": "purchased",
                "price": {"amount_minor": 500, "currency": "USD"},
                "providers": {"stripe": {"type": "stripe_price", "price_id": "price_private"}},
            }
        },
    }
    bursar = Bursar(credit_store=pg_store)
    bursar.catalog.publish_and_activate(
        config,
        label="account-onboarding",
        rollout=CatalogRollout(plans={"pro": PlanRollout(effective="immediate", include_pinned=True)}),
    )
    signup_metadata = CreditMetadata(reference_type="signup")

    created = bursar.accounts.on_account_created(
        ONBOARDING_USER_ID,
        "account-created-937",
        region="us-east",
        metadata=signup_metadata,
    )
    replay = bursar.accounts.on_account_created(
        ONBOARDING_USER_ID,
        "account-created-937",
        region="us-east",
        metadata=signup_metadata,
    )

    assert created.plan_key == "pro"
    assert created.plan_assigned is True
    assert len(created.grants) == 1
    assert replay.plan_assigned is False
    assert bursar.credits.get_balance(ONBOARDING_USER_ID).balance == Decimal("5")
    assert bursar.catalog.get_active() is not None
    public_catalog = bursar.catalog.public_view()
    public_plan = next(plan for plan in public_catalog["plans"] if plan["key"] == "pro")
    assert public_plan.get("description") == "Production plan"
    assert public_plan["quotas"]["assignment_tokens"]["window"] == {
        "type": "plan_assignment",
        "unit": "month",
        "count": 1,
        "timezone": "UTC",
    }
    assert public_catalog["topups"][0].get("description") == "Production credit pack"
    assert public_catalog["topups"][0].get("credits_per_unit") == "100"
    assert "price_private" not in str(public_catalog)
    assert bursar.catalog.is_loaded is True
    assert bursar.catalog.set_revision_pin(ONBOARDING_USER_ID, True) is True

    updated = deepcopy(config)
    updated["plans"]["pro"]["display_name"] = "Pro v2"  # type: ignore[index]
    draft_id = bursar.catalog.publish_draft(updated, label="account-onboarding-v2")
    draft = next(revision for revision in pg_store.get_catalog_history() if revision.id == draft_id)
    bursar.catalog.activate(draft.version)
    bursar.catalog.invalidate()
    bursar.catalog.refresh()
    bursar.catalog.load()
    assert bursar.catalog.get_config().plans["pro"].display_name == "Pro v2"
    assert bursar.catalog.apply_due_changes(limit=5) == 0


def test_dimension_routing_persists_exact_fallback_charges(pg_store: PostgresStore) -> None:
    config = deepcopy(CONFIG)
    completion = config["pricing"]["operations"]["completion"]  # type: ignore[index]
    completion["dimensions"] = {
        "segment": {"type": "string", "required": False},
        "model": {"type": "string", "required": True},
        "seats": {"type": "number", "required": True},
    }
    standard = config["pricing"]["rate_cards"]["standard"]  # type: ignore[index]
    standard["operations"]["completion"] = {  # type: ignore[index]
        "rules": [
            {
                "when": {
                    "segment": {"op": "in", "values": ["pro"]},
                    "model": {"op": "not_in", "values": ["blocked"]},
                    "seats": {"op": "range", "gt": "1", "lt": "10"},
                },
                "charge": {"type": "flat", "amount": "2"},
            },
            {
                "when": {
                    "seats": {"op": "range", "gte": "10", "lte": "20"},
                    "model": {"op": "eq", "value": "enterprise"},
                },
                "charge": {"type": "flat", "amount": "4"},
            },
        ],
        "unmatched": {"action": "charge", "charge": {"type": "flat", "amount": "1"}},
    }
    config["pricing"]["rate_cards"]["enterprise"] = {  # type: ignore[index]
        "extends": "standard",
        "operations": {},
    }
    config["plans"]["pro"]["rate_card"] = "enterprise"  # type: ignore[index]
    config["plans"]["pro"]["allowed_operations"] = ["completion"]  # type: ignore[index]

    service = CreditsService(store=pg_store)
    service.publish_and_activate_catalog(config)
    service.set_user_plan(ONBOARDING_USER_ID, "pro")
    service.add_credits(
        ONBOARDING_USER_ID,
        Decimal("25"),
        entry_type="purchase",
        idempotency_key="dimension-routing:funds",
    )
    scenarios = [
        ({"segment": "pro", "model": "standard", "seats": Decimal("3")}, Decimal("2")),
        ({"segment": "free", "model": "standard", "seats": Decimal("3")}, Decimal("1")),
        ({"segment": "pro", "model": "blocked", "seats": Decimal("3")}, Decimal("1")),
        ({"segment": "pro", "model": "standard", "seats": Decimal("1")}, Decimal("1")),
        ({"segment": "pro", "model": "standard", "seats": Decimal("10")}, Decimal("1")),
        ({"segment": "pro", "model": "enterprise", "seats": Decimal("21")}, Decimal("1")),
        ({"segment": "pro", "model": "enterprise", "seats": Decimal("20")}, Decimal("4")),
        ({"model": "standard", "seats": Decimal("3")}, Decimal("1")),
    ]

    for index, (dimensions, expected) in enumerate(scenarios):
        result = service.deduct(
            ONBOARDING_USER_ID,
            UsageMetrics(
                operation="completion",
                measures={"input_tokens": Decimal(1), "output_tokens": Decimal(0)},
                dimensions=dimensions,
            ),
            idempotency_key=f"dimension-routing:{index}",
        )
        assert result.amount == expected

    assert service.get_balance(ONBOARDING_USER_ID).balance == Decimal("13")
    usage = service.list_usage_charges(ONBOARDING_USER_ID, limit=20)
    assert len(usage.items) == len(scenarios)
    assert sum((item.charged for item in usage.items), start=Decimal(0)) == Decimal("12")


def test_ledger_cursor_analytics_and_expiry_are_persisted(pg_store: PostgresStore) -> None:
    store = pg_store
    service = CreditsService(store=store)
    service.publish_and_activate_catalog(deepcopy(CONFIG))
    service.add_credits(
        LEDGER_USER_ID,
        Decimal("20"),
        entry_type="purchase",
        bucket="purchased",
        idempotency_key="credit-confidence:ledger:grant",
    )
    first_charge = service.deduct(
        LEDGER_USER_ID,
        _usage(2, model="alpha-model"),
        idempotency_key="credit-confidence:ledger:charge-1",
    )
    second_charge = service.deduct(
        LEDGER_USER_ID,
        _usage(3, model="beta-model"),
        idempotency_key="credit-confidence:ledger:charge-2",
    )
    assert first_charge.entry_id is not None

    first_page = service.list_ledger_entries(LEDGER_USER_ID, limit=1)
    assert len(first_page.items) == 1
    assert first_page.next_cursor is not None
    second_page = service.list_ledger_entries(LEDGER_USER_ID, limit=10, cursor=first_page.next_cursor)
    assert len(first_page.items) + len(second_page.items) == 3
    assert service.get_ledger_entry(LEDGER_USER_ID, first_charge.entry_id) is not None
    ledger_items = [*first_page.items, *second_page.items]
    assert sum(item.entry_type == "usage" for item in ledger_items) == 2

    usage_page = service.list_usage_entries(LEDGER_USER_ID, limit=1)
    assert len(usage_page.items) == 1
    assert usage_page.items[0].entry_type == "usage"
    assert usage_page.next_cursor is not None
    usage_tail = service.list_usage_entries(LEDGER_USER_ID, limit=1, cursor=usage_page.next_cursor)
    assert len(usage_tail.items) == 1
    assert usage_tail.items[0].entry_type == "usage"
    assert usage_tail.next_cursor is None

    start = datetime.now(UTC) - timedelta(minutes=5)
    end = datetime.now(UTC) + timedelta(minutes=5)
    by_user = service.spend_by_user(start, end)
    by_model = service.spend_by_model(start, end)
    top_users = service.top_users(10, start, end)
    daily = service.daily_spend(start, end)
    aggregate = service.aggregate_stats(start, end)
    assert any(row.user_id == LEDGER_USER_ID for row in by_user)
    assert {row.model for row in by_model} == {"alpha-model", "beta-model"}
    assert any(row.user_id == LEDGER_USER_ID for row in top_users)
    assert daily
    assert aggregate.total_credits_consumed == Decimal("5")
    assert aggregate.active_users == 1
    assert first_charge.amount + second_charge.amount == Decimal("5")

    expiring = service.add_credits(
        LEDGER_USER_ID,
        Decimal("2"),
        entry_type="adjustment",
        bucket="grant",
        expires_at=datetime.now(UTC) + timedelta(days=1),
        idempotency_key="credit-confidence:ledger:expiry",
    )
    with psycopg2.connect(store.database_url) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT set_config('bursar.mutation_context', 'internal', true)")
        cursor.execute(
            """
            UPDATE bursar.credit_lots
            SET expires_at = now() - interval '1 second'
            WHERE source_entry_id = %s::uuid
            """,
            [expiring.entry_id],
        )
        assert cursor.rowcount == 1

    with psycopg2.connect(store.database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, granted, consumed
            FROM bursar.credit_lots
            WHERE source_entry_id = %s::uuid
            """,
            [expiring.entry_id],
        )
        lot_row = cursor.fetchone()
        assert lot_row is not None
        lot_id, granted, consumed_before = lot_row

    preview = service.sweep_expired_credits(dry_run=True)
    assert preview.expired_count == 1
    with psycopg2.connect(store.database_url) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT consumed FROM bursar.credit_lots WHERE id = %s", [lot_id])
        assert cursor.fetchone() == (consumed_before,)

    swept = service.sweep_expired_credits()
    assert swept.expired_count == 1
    assert swept.expired_amount == Decimal("2")
    with psycopg2.connect(store.database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT granted, consumed FROM bursar.credit_lots WHERE id = %s",
            [lot_id],
        )
        assert cursor.fetchone() == (granted, granted)

    assert service.sweep_expired_credits().expired_count == 0

    with psycopg2.connect(store.database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT count(*)
            FROM bursar.credit_ledger_entries AS entry
            JOIN bursar.credit_accounts AS account ON account.id = entry.account_id
            WHERE account.tenant_id = %s
              AND account.subject_id = %s::uuid
              AND entry.kind = 'expiry'
              AND entry.amount = -2
              AND entry.metadata->>'lot_id' = %s
            """,
            [TEST_TENANT_ID, LEDGER_USER_ID, str(lot_id)],
        )
        assert cursor.fetchone() == (1,)
