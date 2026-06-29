"""Tests for store-level pricing operations."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from ducto import ConfigError, CreditManager, MemoryStore
from ducto.interface.models import PlanDefinition, PricingConfigData, SpendCap


def test_get_pricing_when_none() -> None:
    store = MemoryStore()
    result = store.get_active_pricing()
    assert result is None


def test_set_and_get_pricing() -> None:
    store = MemoryStore()
    config = PricingConfigData(
        models={"gpt-4": "input_tokens * 0.01"},
    )
    returned_id = store.set_active_pricing(config, label="v1")
    assert returned_id != ""

    result = store.get_active_pricing()
    assert result is not None
    assert result.config.models == {"gpt-4": "input_tokens * 0.01"}


def test_set_pricing_replaces_active() -> None:
    store = MemoryStore()
    c1 = PricingConfigData(models={"_default": "input_tokens * 1"})
    store.set_active_pricing(c1, label="first")

    c2 = PricingConfigData(models={"_default": "input_tokens * 2"})
    store.set_active_pricing(c2, label="second")

    result = store.get_active_pricing()
    assert result is not None
    assert result.config.models["_default"] == "input_tokens * 2"


def test_pricing_history_returns_all_versions() -> None:
    store = MemoryStore()
    c1 = PricingConfigData(models={"_default": "input_tokens * 1"})
    c2 = PricingConfigData(models={"_default": "input_tokens * 2"})
    c3 = PricingConfigData(models={"_default": "input_tokens * 3"})

    store.set_active_pricing(c1, label="first")
    store.set_active_pricing(c2, label="second")
    store.set_active_pricing(c3, label="third")

    history = store.get_pricing_history()
    assert len(history) == 3
    assert [h.version for h in history] == [3, 2, 1]  # newest first
    assert [h.label for h in history] == ["third", "second", "first"]
    # Only the latest should be active
    assert [h.active for h in history] == [True, False, False]


def test_get_pricing_config_by_version() -> None:
    store = MemoryStore()
    c1 = PricingConfigData(models={"_default": "input_tokens * 1"})
    c2 = PricingConfigData(models={"_default": "input_tokens * 2"})
    store.set_active_pricing(c1, label="v1")
    store.set_active_pricing(c2, label="v2")

    v1 = store.get_pricing_config(1)
    assert v1 is not None
    assert v1.config.models["_default"] == "input_tokens * 1"
    assert v1.version == 1
    assert v1.label == "v1"

    v2 = store.get_pricing_config(2)
    assert v2 is not None
    assert v2.config.models["_default"] == "input_tokens * 2"
    assert v2.version == 2

    # Missing version
    missing = store.get_pricing_config(99)
    assert missing is None


def test_activate_pricing_rollback() -> None:
    store = MemoryStore()
    c1 = PricingConfigData(models={"_default": "input_tokens * 1"})
    c2 = PricingConfigData(models={"_default": "input_tokens * 2"})
    c3 = PricingConfigData(models={"_default": "input_tokens * 3"})

    store.set_active_pricing(c1, label="v1")
    store.set_active_pricing(c2, label="v2")
    store.set_active_pricing(c3, label="v3")

    # Rollback to v1
    store.activate_pricing(1)
    active = store.get_active_pricing()
    assert active is not None
    assert active.config.models["_default"] == "input_tokens * 1"
    assert active.version == 1

    # History should reflect only v1 is active
    history = store.get_pricing_history()
    assert history[2].version == 1
    assert history[2].active is True
    assert history[0].active is False
    assert history[1].active is False


def test_pricing_history_empty_when_no_config() -> None:
    store = MemoryStore()
    assert store.get_pricing_history() == []


def test_activate_pricing_does_not_create_new_version() -> None:
    """Activate switches active version without inserting a new config."""
    store = MemoryStore()
    store.set_active_pricing(PricingConfigData(models={"_default": "input_tokens * 1"}), label="v1")
    store.set_active_pricing(PricingConfigData(models={"_default": "input_tokens * 2"}), label="v2")

    store.activate_pricing(1)
    # Still only 2 versions
    assert len(store.get_pricing_history()) == 2


def test_publish_pricing_from_dict_invalid_data() -> None:
    manager = CreditManager(store=MemoryStore())

    with pytest.raises(ConfigError):
        manager.publish_pricing_from_dict({})


def test_load_pricing_file_yaml(tmp_path) -> None:
    """Load a YAML pricing file via _load_pricing_file."""
    from ducto.__main__ import _load_pricing_file

    f = tmp_path / "pricing.yaml"
    f.write_text("models:\n  _default: input_tokens * 1\n")
    data = _load_pricing_file(str(f))
    assert data["models"]["_default"] == "input_tokens * 1"


# ── Plan management ─────────────────────────────────────────────────────


class TestPlanManagement:
    def test_get_user_plan_no_plan(self) -> None:
        store = MemoryStore()
        result = store.get_user_plan("user-1")
        assert result.plan_id is None
        assert result.plan_name is None
        assert result.free_allowance == 0
        assert result.features == {}

    def test_set_and_get_user_plan(self) -> None:
        store = MemoryStore()
        # Seed plan via v2 config
        v2 = PricingConfigData(
            models={"_default": "1"},
            plans={
                "pro": PlanDefinition(id="pro", name="Pro Plan", free_allowance=Decimal("500")),
            },
        )
        store.set_active_pricing(v2)
        store.set_user_plan("user-1", "pro")

        result = store.get_user_plan("user-1")
        assert result.plan_id == "pro"
        assert result.plan_name == "Pro Plan"
        assert result.free_allowance == 500
        assert result.features == {}

    def test_get_user_plan_features(self) -> None:
        store = MemoryStore()
        v2 = PricingConfigData(
            models={"_default": "1"},
            plans={
                "premium": PlanDefinition(
                    id="premium",
                    name="Premium Plan",
                    free_allowance=Decimal("2000"),
                    features={"ai_chat": True, "max_roadmaps": 20, "export_pdf": True},
                ),
            },
        )
        store.set_active_pricing(v2)
        store.set_user_plan("user-1", "premium")

        result = store.get_user_plan("user-1")
        assert result.plan_id == "premium"
        assert result.features["ai_chat"] is True
        assert result.features["max_roadmaps"] == 20
        assert result.features["export_pdf"] is True

    def test_check_feature(self) -> None:
        store = MemoryStore()
        v2 = PricingConfigData(
            models={"_default": "1"},
            plans={
                "premium": PlanDefinition(
                    id="premium",
                    name="Premium Plan",
                    features={"ai_chat": True, "max_roadmaps": 20},
                ),
                "free": PlanDefinition(
                    id="free",
                    name="Free Plan",
                    features={},
                ),
            },
        )
        store.set_active_pricing(v2)
        store.set_user_plan("user-1", "premium")
        store.set_user_plan("user-2", "free")

        # Premium user has features
        assert store.check_feature("user-1", "ai_chat").has_feature is True
        assert store.check_feature("user-1", "ai_chat").value is True
        assert store.check_feature("user-1", "max_roadmaps").value == 20
        # Premium user missing feature
        assert store.check_feature("user-1", "export_pdf").has_feature is False
        # Free user — no features
        assert store.check_feature("user-2", "ai_chat").has_feature is False
        # No plan user
        assert store.check_feature("nobody", "ai_chat").has_feature is False

    def test_check_allowance_no_plan(self) -> None:
        store = MemoryStore()
        allowance = store.check_allowance("nobody")
        assert allowance.allowance_remaining == 0

    def test_check_allowance_with_allowance(self) -> None:
        store = MemoryStore()
        v2 = PricingConfigData(
            models={"_default": "1"},
            plans={"basic": PlanDefinition(id="basic", name="Basic", free_allowance=Decimal("200"))},
        )
        store.set_active_pricing(v2)
        store.set_user_plan("user-1", "basic")

        allowance = store.check_allowance("user-1")
        assert allowance.allowance_remaining == 200
        assert allowance.plan_id == "basic"

    def test_increment_usage_window_reduces_allowance(self) -> None:
        store = MemoryStore()
        v2 = PricingConfigData(
            models={"_default": "1"},
            plans={"basic": PlanDefinition(id="basic", name="Basic", free_allowance=Decimal("200"))},
        )
        store.set_active_pricing(v2)
        store.set_user_plan("user-1", "basic")

        store.increment_usage_window("user-1", "basic", Decimal("50"))
        assert store.check_allowance("user-1").allowance_remaining == 150

        store.increment_usage_window("user-1", "basic", Decimal("30"))
        assert store.check_allowance("user-1").allowance_remaining == 120


# ── Credit expiry ───────────────────────────────────────────────────────────


class TestCreditExpiry:
    def test_credits_expire_after_ttl(self) -> None:
        store = MemoryStore()
        # tz-aware UTC, one hour in the past → already expired (M9: compare
        # datetimes, not strings; no naive-local clock).
        expires_at = datetime.now(UTC) - timedelta(hours=1)
        store.add_credits("user_1", Decimal("100"), "purchase", expires_at=expires_at)

        result = store.sweep_expired_credits()
        assert result.expired_count == 1
        assert result.expired_amount == 100
        assert result.dry_run is False
        assert store.get_balance("user_1").balance == 0

    def test_dry_run_reports_without_modifying(self) -> None:
        store = MemoryStore()
        expires_at = datetime.now(UTC) - timedelta(hours=1)
        store.add_credits("user_1", Decimal("100"), "purchase", expires_at=expires_at)

        result = store.sweep_expired_credits(dry_run=True)
        assert result.expired_count == 1
        assert result.expired_amount == 100
        assert result.dry_run is True
        assert store.get_balance("user_1").balance == 100  # unchanged

    def test_credits_without_expiry_never_expire(self) -> None:
        store = MemoryStore()
        store.add_credits("user_1", Decimal("100"))

        result = store.sweep_expired_credits()
        assert result.expired_count == 0
        assert result.expired_amount == 0
        assert store.get_balance("user_1").balance == 100

    def test_sweep_with_no_expired_returns_zero(self) -> None:
        store = MemoryStore()
        result = store.sweep_expired_credits()
        assert result.expired_count == 0
        assert result.expired_amount == 0

    def test_partial_expiry_caps_at_balance(self) -> None:
        store = MemoryStore()
        expires_at = datetime.now(UTC) - timedelta(hours=1)
        store.add_credits("user_1", Decimal("50"), "purchase", expires_at=expires_at)
        store.add_credits("user_1", Decimal("30"), "purchase")

        result = store.sweep_expired_credits()
        assert result.expired_amount == 50
        assert store.get_balance("user_1").balance == 30


# ── Refunds ────────────────────────────────────────────────────────────────


class TestRefund:
    def test_full_refund_restores_balance(self) -> None:
        store = MemoryStore()
        store.add_credits("user_1", Decimal("100"), "purchase")
        # Deduct 30
        reserve = store.reserve_credits("user_1", Decimal("30"), "usage")
        deduct = store.deduct_credits("user_1", reserve.reservation_id, Decimal("30"))
        assert store.get_balance("user_1").balance == 70

        refund = store.refund_credits(deduct.transaction_id)
        assert refund.error is None
        assert refund.amount == 30
        assert store.get_balance("user_1").balance == 100

    def test_partial_refund(self) -> None:
        store = MemoryStore()
        store.add_credits("user_1", Decimal("100"))
        reserve = store.reserve_credits("user_1", Decimal("50"), "usage")
        deduct = store.deduct_credits("user_1", reserve.reservation_id, Decimal("50"))

        refund = store.refund_credits(deduct.transaction_id, amount=Decimal("20"))
        assert refund.error is None
        assert refund.amount == 20
        assert store.get_balance("user_1").balance == 70  # 50 + 20

    def test_double_refund_returns_error(self) -> None:
        store = MemoryStore()
        store.add_credits("user_1", Decimal("100"))
        reserve = store.reserve_credits("user_1", Decimal("30"), "usage")
        deduct = store.deduct_credits("user_1", reserve.reservation_id, Decimal("30"))

        r1 = store.refund_credits(deduct.transaction_id)
        assert r1.error is None

        r2 = store.refund_credits(deduct.transaction_id)
        assert r2.error == "already_refunded"

    def test_unknown_transaction_returns_error(self) -> None:
        store = MemoryStore()
        refund = store.refund_credits("non-existent-id")
        # Aligned to the SQL refund error code (was "transaction_not_found").
        assert refund.error == "not_found"


# ── Usage analytics ───────────────────────────────────────────────────────────


class TestUsageAnalytics:
    def test_spend_by_user_returns_correct_totals(self) -> None:
        store = MemoryStore()
        store.add_credits("user_1", Decimal("1000"))
        store.add_credits("user_2", Decimal("2000"))

        r1 = store.reserve_credits("user_1", Decimal("100"), "usage")
        store.deduct_credits("user_1", r1.reservation_id, Decimal("100"))
        r2 = store.reserve_credits("user_1", Decimal("50"), "usage")
        store.deduct_credits("user_1", r2.reservation_id, Decimal("50"))
        r3 = store.reserve_credits("user_2", Decimal("200"), "usage")
        store.deduct_credits("user_2", r3.reservation_id, Decimal("200"))

        now = datetime.now(UTC)
        rows = store.spend_by_user(now - timedelta(seconds=10), now + timedelta(seconds=10))

        assert len(rows) == 2
        u1 = next(r for r in rows if r.user_id == "user_1")
        assert u1.total_spend == 150  # 100 + 50
        assert u1.transaction_count == 2
        u2 = next(r for r in rows if r.user_id == "user_2")
        assert u2.total_spend == 200
        assert u2.transaction_count == 1

    def test_spend_by_model_returns_correct_totals(self) -> None:
        store = MemoryStore()
        store.add_credits("user_1", Decimal("1000"))

        from ducto.interface.models import CreditMetadata

        r1 = store.reserve_credits("user_1", Decimal("100"), "usage")
        store.deduct_credits("user_1", r1.reservation_id, Decimal("100"), metadata=CreditMetadata(model="gpt-4"))
        r2 = store.reserve_credits("user_1", Decimal("50"), "usage")
        store.deduct_credits("user_1", r2.reservation_id, Decimal("50"), metadata=CreditMetadata(model="claude-3"))

        now = datetime.now(UTC)
        rows = store.spend_by_model(now - timedelta(seconds=10), now + timedelta(seconds=10))
        gpt4 = next((r for r in rows if r.model == "gpt-4"), None)
        assert gpt4 is not None
        assert gpt4.total_spend == 100

    def test_empty_time_window_returns_empty(self) -> None:
        store = MemoryStore()
        store.add_credits("user_1", Decimal("100"))
        r = store.reserve_credits("user_1", Decimal("10"), "usage")
        store.deduct_credits("user_1", r.reservation_id, Decimal("10"))

        rows = store.spend_by_user(
            datetime(2020, 1, 1),
            datetime(2020, 1, 2),
        )
        assert len(rows) == 0

    def test_top_users_respects_limit(self) -> None:
        store = MemoryStore()
        store.add_credits("user_1", Decimal("1000"))
        store.add_credits("user_2", Decimal("1000"))
        store.add_credits("user_3", Decimal("1000"))

        for uid, amt in [("user_1", Decimal("300")), ("user_2", Decimal("200")), ("user_3", Decimal("100"))]:
            r = store.reserve_credits(uid, amt, "usage")
            store.deduct_credits(uid, r.reservation_id, amt)

        now = datetime.now(UTC)
        top = store.top_users(2, now - timedelta(seconds=10), now + timedelta(seconds=10))
        assert len(top) == 2
        assert top[0].total_spend >= top[1].total_spend

    def test_aggregate_stats_returns_aggregates(self) -> None:
        store = MemoryStore()
        store.add_credits("user_1", Decimal("1000"))
        store.add_credits("user_2", Decimal("1000"))
        from ducto.interface.models import CreditMetadata

        r1 = store.reserve_credits("user_1", Decimal("50"), "usage")
        store.deduct_credits("user_1", r1.reservation_id, Decimal("50"), metadata=CreditMetadata(model="gpt-4"))
        r2 = store.reserve_credits("user_2", Decimal("30"), "usage")
        store.deduct_credits("user_2", r2.reservation_id, Decimal("30"), metadata=CreditMetadata(model="claude-3"))

        now = datetime.now(UTC)
        stats = store.aggregate_stats(now - timedelta(seconds=10), now + timedelta(seconds=10))
        assert stats.total_credits_consumed == 80
        assert stats.active_users == 2
        assert stats.avg_daily_spend == 80
        assert stats.top_model in ("gpt-4", "claude-3")
        assert stats.top_user in ("user_1", "user_2")

    def test_aggregate_stats_empty_window(self) -> None:
        store = MemoryStore()
        stats = store.aggregate_stats(datetime(2020, 1, 1), datetime(2020, 1, 2))
        assert stats.total_credits_consumed == 0
        assert stats.active_users == 0

    def test_daily_spend_bucketing_correct(self) -> None:
        store = MemoryStore()
        store.add_credits("user_1", Decimal("1000"))
        r = store.reserve_credits("user_1", Decimal("75"), "usage")
        store.deduct_credits("user_1", r.reservation_id, Decimal("75"))

        now = datetime.now(UTC)
        rows = store.daily_spend(now - timedelta(days=1), now + timedelta(days=1))
        assert len(rows) >= 1
        assert rows[0].total_spend == 75
        assert rows[0].transaction_count == 1

    # ── Transaction listing ─────────────────────────────────────────────────

    def test_list_transactions_returns_all_for_user(self) -> None:
        from ducto.interface.models import CreditMetadata

        store = MemoryStore()
        store.add_credits("user_1", Decimal("1000"), "purchase", CreditMetadata(reference_id="purchase-1"))
        store.add_credits("user_1", Decimal("500"), "signup_bonus", CreditMetadata(reference_id="bonus-1"))
        r = store.reserve_credits("user_1", Decimal("200"), "usage")
        store.deduct_credits("user_1", r.reservation_id, Decimal("200"), metadata=CreditMetadata(model="gpt-4"))
        store.add_credits("user_2", Decimal("999"), "purchase")
        result = store.list_user_transactions("user_1")
        assert len(result) == 3
        assert result[0].total_count == 3

    def test_list_transactions_filters_by_type(self) -> None:
        from ducto.interface.models import CreditMetadata

        store = MemoryStore()
        store.add_credits("user_1", Decimal("1000"), "purchase")
        store.add_credits("user_1", Decimal("500"), "signup_bonus")
        r = store.reserve_credits("user_1", Decimal("200"), "usage")
        store.deduct_credits("user_1", r.reservation_id, Decimal("200"), metadata=CreditMetadata(model="gpt-4"))
        result = store.list_user_transactions("user_1", types=["usage"])
        assert len(result) == 1
        assert result[0].type == "usage"
        assert result[0].total_count == 1

    def test_list_transactions_paginates(self) -> None:
        store = MemoryStore()
        for _i in range(5):
            store.add_credits("user_1", Decimal("100"), "purchase")
        page = store.list_user_transactions("user_1", limit=2, offset=0)
        assert len(page) == 2
        assert page[0].total_count == 5

    def test_list_transactions_orders_by_created_at_desc(self) -> None:
        store = MemoryStore()
        store.add_credits("user_1", Decimal("100"), "purchase")
        store.add_credits("user_1", Decimal("200"), "purchase")
        store.add_credits("user_1", Decimal("300"), "purchase")
        result = store.list_user_transactions("user_1")
        for i in range(1, len(result)):
            assert result[i].created_at <= result[i - 1].created_at

    def test_list_transactions_returns_empty_for_no_transactions(self) -> None:
        store = MemoryStore()
        result = store.list_user_transactions("no_such_user")
        assert len(result) == 0


def test_load_pricing_file_json(tmp_path) -> None:
    """Load a JSON pricing file via _load_pricing_file."""
    from ducto.__main__ import _load_pricing_file

    f = tmp_path / "pricing.json"
    f.write_text('{"models": {"_default": "input_tokens * 1"}}')
    data = _load_pricing_file(str(f))
    assert data["models"]["_default"] == "input_tokens * 1"


# ── Team/shared balance pools ─────────────────────────────────────────


class TestTeamBalances:
    def test_create_team_and_get_balance(self) -> None:
        store = MemoryStore()
        result = store.create_team("Engineering")
        assert result.team_id != ""
        assert result.name == "Engineering"

        balance = store.get_team_balance(result.team_id)
        assert balance.name == "Engineering"
        assert balance.balance == 0
        assert balance.member_count == 0

    def test_create_team_with_initial_balance(self) -> None:
        store = MemoryStore()
        result = store.create_team("Pro Team", initial_balance=Decimal("1000"))
        balance = store.get_team_balance(result.team_id)
        assert balance.balance == 1000

    def test_add_team_member_and_track_members(self) -> None:
        store = MemoryStore()
        team = store.create_team("Team A", Decimal("500"))
        store.add_team_member(team.team_id, "user-1", role="admin")
        store.add_team_member(team.team_id, "user-2", role="member")

        balance = store.get_team_balance(team.team_id)
        assert balance.member_count == 2

        members = store.get_team_members(team.team_id)
        assert len(members) == 2
        assert members[0].role == "admin"

    def test_add_team_member_with_spend_cap(self) -> None:
        store = MemoryStore()
        team = store.create_team("Capped Team", Decimal("5000"))
        store.add_team_member(team.team_id, "user-1", spend_cap=Decimal("100"))
        members = store.get_team_members(team.team_id)
        assert members[0].spend_cap == 100

    def test_deduct_team_debits_team_pool_not_user_balance(self) -> None:
        store = MemoryStore()
        store.add_credits("user-1", Decimal("100"))  # user balance
        team = store.create_team("Pool", Decimal("500"))
        store.add_team_member(team.team_id, "user-1")

        result = store.deduct_team(team.team_id, "user-1", Decimal("50"))
        assert result.error is None
        assert result.amount == -50
        assert result.team_balance_after == 450

        # User balance unchanged
        assert store.get_balance("user-1").balance == 100

    def test_deduct_team_insufficient_balance(self) -> None:
        store = MemoryStore()
        team = store.create_team("Poor Team", Decimal("10"))
        store.add_team_member(team.team_id, "user-1")
        result = store.deduct_team(team.team_id, "user-1", Decimal("100"))
        assert result.error == "insufficient_team_balance"

    def test_deduct_team_user_not_in_team(self) -> None:
        store = MemoryStore()
        team = store.create_team("Closed Team", Decimal("500"))
        result = store.deduct_team(team.team_id, "user-1", Decimal("10"))
        assert result.error == "user_not_in_team"

    def test_deduct_team_spend_cap_blocks_overspend(self) -> None:
        store = MemoryStore()
        team = store.create_team("Capped", Decimal("1000"))
        store.add_team_member(team.team_id, "user-1", role="member", spend_cap=Decimal("50"))

        r1 = store.deduct_team(team.team_id, "user-1", Decimal("30"))
        assert r1.error is None
        assert r1.team_balance_after == 970

        r2 = store.deduct_team(team.team_id, "user-1", Decimal("30"))
        assert r2.error == "spend_cap_exceeded"

    def test_deduct_team_nonexistent_team(self) -> None:
        store = MemoryStore()
        result = store.deduct_team("no-such-team", "user-1", Decimal("10"))
        assert result.error == "team_not_found"


# ── Spend caps and rate limiting ──────────────────────────────────────


class TestSpendCaps:
    def test_no_caps_returns_no_limit(self) -> None:
        store = MemoryStore()
        result = store.check_spend_cap("user-1")
        assert not result.capped
        assert result.action is None

    def test_deny_when_exceeds_daily_cap(self) -> None:
        store = MemoryStore()
        store.set_spend_cap(SpendCap(user_id="user-1", type="daily", limit=Decimal("100"), action="deny"))
        result = store.check_spend_cap("user-1", amount=Decimal("101"))
        assert result.capped
        assert result.action == "deny"

    def test_allow_within_daily_cap(self) -> None:
        store = MemoryStore()
        store.set_spend_cap(SpendCap(user_id="user-1", type="daily", limit=Decimal("100"), action="deny"))
        result = store.check_spend_cap("user-1", amount=Decimal("50"))
        assert not result.capped

    def test_warn_action_allows_through(self) -> None:
        store = MemoryStore()
        store.set_spend_cap(SpendCap(user_id="user-1", type="daily", limit=Decimal("100"), action="warn"))
        result = store.check_spend_cap("user-1", amount=Decimal("101"))
        assert not result.capped
        assert result.action == "warn"

    def test_notify_action_allows_through(self) -> None:
        store = MemoryStore()
        store.set_spend_cap(SpendCap(user_id="user-1", type="daily", limit=Decimal("100"), action="notify"))
        result = store.check_spend_cap("user-1", amount=Decimal("101"))
        assert not result.capped
        assert result.action == "notify"

    def test_per_model_cap_independent(self) -> None:
        store = MemoryStore()
        store.set_spend_cap(SpendCap(user_id="user-1", type="daily", limit=Decimal("50"), action="deny", model="gpt-4"))
        store.set_spend_cap(SpendCap(user_id="user-1", type="daily", limit=Decimal("200"), action="deny"))

        assert not store.check_spend_cap("user-1", model="gpt-4", amount=Decimal("30")).capped
        assert store.check_spend_cap("user-1", model="gpt-4", amount=Decimal("60")).capped
        assert not store.check_spend_cap("user-1", model="claude-3", amount=Decimal("150")).capped

    def test_caps_only_apply_to_matching_user(self) -> None:
        store = MemoryStore()
        store.set_spend_cap(SpendCap(user_id="user-1", type="daily", limit=Decimal("100"), action="deny"))
        result = store.check_spend_cap("user-2", amount=Decimal("200"))
        assert not result.capped
