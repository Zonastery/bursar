"""Tests for store-level pricing operations."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from bursar import ConfigError, CreditManager, MemoryStore
from bursar.allowance import resolve_calendar_window
from bursar.interface.base import CapabilityNotSupportedError, CreditStore, StoreError
from bursar.interface.models import (
    AddCreditsResult,
    AllowanceResult,
    AvailableResult,
    BalanceResult,
    BucketBalancesResult,
    CapCheckResult,
    DeductionResult,
    FeatureLimit,
    FeatureLimitResult,
    GetUserPlanResult,
    LeaseResult,
    RefundResult,
    ReleaseResult,
    SetupResult,
    SetUserPlanResult,
    SpendCap,
    SweepResult,
)


def test_get_pricing_when_none() -> None:
    store = MemoryStore()
    result = store.get_active_pricing()
    assert result is None


def test_set_and_get_pricing() -> None:
    store = MemoryStore()
    config = {
        "version": 1,
        "metering": {"models": {"gpt-4": "input_tokens * 0.01"}},
    }
    returned_id = store.set_active_pricing(config, label="v1")
    assert returned_id != ""

    result = store.get_active_pricing()
    assert result is not None
    assert result.config["metering"]["models"] == {"gpt-4": "input_tokens * 0.01"}


def test_set_pricing_replaces_active() -> None:
    store = MemoryStore()
    c1 = {"version": 1, "metering": {"models": {"*": "input_tokens * 1"}}}
    store.set_active_pricing(c1, label="first")

    c2 = {"version": 1, "metering": {"models": {"*": "input_tokens * 2"}}}
    store.set_active_pricing(c2, label="second")

    result = store.get_active_pricing()
    assert result is not None
    assert result.config["metering"]["models"]["*"] == "input_tokens * 2"


def test_pricing_history_returns_all_versions() -> None:
    store = MemoryStore()
    c1 = {"version": 1, "metering": {"models": {"*": "input_tokens * 1"}}}
    c2 = {"version": 2, "metering": {"models": {"*": "input_tokens * 2"}}}
    c3 = {"version": 3, "metering": {"models": {"*": "input_tokens * 3"}}}

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
    c1 = {"version": 1, "metering": {"models": {"*": "input_tokens * 1"}}}
    c2 = {"version": 2, "metering": {"models": {"*": "input_tokens * 2"}}}
    store.set_active_pricing(c1, label="v1")
    store.set_active_pricing(c2, label="v2")

    v1 = store.get_pricing_config(1)
    assert v1 is not None
    assert v1.config["metering"]["models"]["*"] == "input_tokens * 1"
    assert v1.version == 1
    assert v1.label == "v1"

    v2 = store.get_pricing_config(2)
    assert v2 is not None
    assert v2.config["metering"]["models"]["*"] == "input_tokens * 2"
    assert v2.version == 2

    # Missing version
    missing = store.get_pricing_config(99)
    assert missing is None


def test_activate_pricing_rollback() -> None:
    store = MemoryStore()
    c1 = {"version": 1, "metering": {"models": {"*": "input_tokens * 1"}}}
    c2 = {"version": 2, "metering": {"models": {"*": "input_tokens * 2"}}}
    c3 = {"version": 3, "metering": {"models": {"*": "input_tokens * 3"}}}

    store.set_active_pricing(c1, label="v1")
    store.set_active_pricing(c2, label="v2")
    store.set_active_pricing(c3, label="v3")

    # Rollback to v1
    store.activate_pricing(1)
    active = store.get_active_pricing()
    assert active is not None
    assert active.config["metering"]["models"]["*"] == "input_tokens * 1"
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
    store.set_active_pricing({"version": 1, "metering": {"models": {"*": "input_tokens * 1"}}}, label="v1")
    store.set_active_pricing({"version": 2, "metering": {"models": {"*": "input_tokens * 2"}}}, label="v2")

    store.activate_pricing(1)
    # Still only 2 versions
    assert len(store.get_pricing_history()) == 2


def test_publish_pricing_from_dict_invalid_data() -> None:
    manager = CreditManager(store=MemoryStore())

    with pytest.raises(ConfigError):
        manager.publish_pricing_from_dict({})


def test_load_pricing_file_yaml(tmp_path) -> None:
    """Load a YAML pricing file via _load_pricing_file."""
    from bursar.__main__ import _load_pricing_file

    f = tmp_path / "pricing.yaml"
    f.write_text("version: 1\nmetering:\n  models:\n    '*': input_tokens * 1\n")
    data = _load_pricing_file(str(f))
    assert data["metering"]["models"]["*"] == "input_tokens * 1"


# ── Plan management ─────────────────────────────────────────────────────


class TestPlanManagement:
    def test_get_user_plan_no_plan(self) -> None:
        store = MemoryStore()
        result = store.get_user_plan("user-1")
        assert result.plan_id is None
        assert result.plan_label is None
        assert result.allowance_amount == 0
        assert result.entitlements == {}

    def test_set_and_get_user_plan(self) -> None:
        store = MemoryStore()
        # Seed plan via v2 config
        v2 = {
            "version": 1,
            "metering": {"models": {"*": "1"}},
            "plans": {
                "pro": {"label": "Pro Plan", "allowance": {"amount": 500}},
            },
        }
        store.set_active_pricing(v2)
        store.set_user_plan("user-1", "pro")

        result = store.get_user_plan("user-1")
        assert result.plan_id == "pro"
        assert result.plan_label == "Pro Plan"
        assert result.allowance_amount == 500
        assert result.entitlements == {}

    def test_get_user_plan_features(self) -> None:
        store = MemoryStore()
        v2 = {
            "version": 1,
            "metering": {"models": {"*": "1"}},
            "plans": {
                "premium": {
                    "label": "Premium Plan",
                    "allowance": {"amount": 2000},
                    "entitlements": {
                        "ai_chat": {"value": True},
                        "max_roadmaps": {"value": 20},
                        "export_pdf": {"value": True},
                    },
                },
            },
        }
        store.set_active_pricing(v2)
        store.set_user_plan("user-1", "premium")

        result = store.get_user_plan("user-1")
        assert result.plan_id == "premium"
        assert result.entitlements["ai_chat"].value is True
        assert result.entitlements["max_roadmaps"].value == 20
        assert result.entitlements["export_pdf"].value is True

    def test_check_feature(self) -> None:
        store = MemoryStore()
        v2 = {
            "version": 1,
            "metering": {"models": {"*": "1"}},
            "plans": {
                "premium": {
                    "label": "Premium Plan",
                    "entitlements": {
                        "ai_chat": {"value": True},
                        "max_roadmaps": {"value": 20},
                    },
                },
                "free": {
                    "label": "Free Plan",
                    "entitlements": {},
                },
            },
        }
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
        v2 = {
            "version": 1,
            "metering": {"models": {"*": "1"}},
            "plans": {"basic": {"label": "Basic", "allowance": {"amount": 200}}},
        }
        store.set_active_pricing(v2)
        store.set_user_plan("user-1", "basic")

        allowance = store.check_allowance("user-1")
        assert allowance.allowance_remaining == 200
        assert allowance.plan_id == "basic"

    def test_unset_user_plan_clears_plan_and_assigned_at(self) -> None:
        store = MemoryStore()
        store.set_active_pricing(
            {
                "version": 1,
                "metering": {"models": {"*": "1"}},
                "plans": {
                    "basic": {"label": "Basic", "allowance": {"amount": 100}},
                },
            }
        )
        store.set_user_plan("u", "basic")
        plan = store.get_user_plan("u")
        assert plan.plan_id == "basic"
        assert plan.plan_assigned_at is not None

        store.unset_user_plan("u")
        plan = store.get_user_plan("u")
        assert plan.plan_id is None
        assert plan.plan_assigned_at is None

    def test_unset_user_plan_idempotent_for_planless_user(self) -> None:
        store = MemoryStore()
        result = store.unset_user_plan("no-plan-user")
        assert result == {"user_id": "no-plan-user"}
        plan = store.get_user_plan("no-plan-user")
        assert plan.plan_id is None

    def test_increment_usage_window_reduces_allowance(self) -> None:
        store = MemoryStore()
        v2 = {
            "version": 1,
            "metering": {"models": {"*": "1"}},
            "plans": {"basic": {"label": "Basic", "allowance": {"amount": 200}}},
        }
        store.set_active_pricing(v2)
        store.set_user_plan("user-1", "basic")

        store.increment_usage_window("user-1", "basic", Decimal("50"))
        assert store.check_allowance("user-1").allowance_remaining == 150

        store.increment_usage_window("user-1", "basic", Decimal("30"))
        assert store.check_allowance("user-1").allowance_remaining == 120

    def test_deduct_with_allowance_skip_allowance_bypasses_free_credits(self) -> None:
        """skip_allowance=True must charge the full amount from balance, not the allowance pool (Fix 7)."""
        store = MemoryStore()
        v2 = {
            "version": 1,
            "metering": {"models": {"*": "1"}},
            "plans": {"free": {"label": "Free", "allowance": {"amount": 100}}},
            "ledger": {"min_balance": 0},
        }
        store.set_active_pricing(v2)
        store.set_user_plan("user-1", "free")
        store.add_credits("user-1", Decimal("50"))

        result = store.deduct_with_allowance(
            "user-1",
            Decimal("20"),
            skip_allowance=True,
        )
        # Full charge from balance, none from allowance pool.
        assert result.amount == Decimal("20")
        assert result.allowance_consumed == Decimal(0)
        assert store.get_balance("user-1").balance == Decimal("30")
        # Allowance pool entirely intact.
        assert store.check_allowance("user-1").allowance_remaining == Decimal("100")

    def test_deduct_with_allowance_default_consumes_allowance(self) -> None:
        """skip_allowance defaults to False — free allowance is consumed first."""
        store = MemoryStore()
        v2 = {
            "version": 1,
            "metering": {"models": {"*": "1"}},
            "plans": {"free": {"label": "Free", "allowance": {"amount": 100}}},
            "ledger": {"min_balance": 0},
        }
        store.set_active_pricing(v2)
        store.set_user_plan("user-1", "free")
        store.add_credits("user-1", Decimal("50"))

        result = store.deduct_with_allowance("user-1", Decimal("20"))
        assert result.amount == Decimal(0)  # fully covered by allowance
        assert result.allowance_consumed == Decimal("20")
        assert store.get_balance("user-1").balance == Decimal("50")  # balance untouched
        assert store.check_allowance("user-1").allowance_remaining == Decimal("80")


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

    # ── Lazy per-user scoped sweep ───────────────────────────────────────────

    def test_scoped_sweep_only_expires_target_user(self) -> None:
        """sweep_expired_credits(user_id=X) only touches X's expired grants."""
        store = MemoryStore()
        expires_at = datetime.now(UTC) - timedelta(hours=1)
        store.add_credits("user_1", Decimal("100"), "purchase", expires_at=expires_at)
        store.add_credits("user_2", Decimal("200"), "purchase", expires_at=expires_at)

        result = store.sweep_expired_credits(user_id="user_1")
        assert result.expired_count == 1
        assert result.expired_amount == Decimal("100")
        assert store.get_balance("user_1").balance == Decimal("0")
        # user_2's expired grant is left completely untouched.
        assert store.get_balance("user_2").balance == Decimal("200")

        # A later global sweep still catches user_2's untouched expired grant.
        global_result = store.sweep_expired_credits()
        assert global_result.expired_count == 1
        assert global_result.expired_amount == Decimal("200")
        assert store.get_balance("user_2").balance == Decimal("0")

    def test_scoped_sweep_is_idempotent(self) -> None:
        """Repeated calls to the scoped sweep never double-expire (swept_at parity)."""
        store = MemoryStore()
        expires_at = datetime.now(UTC) - timedelta(hours=1)
        store.add_credits("user_1", Decimal("40"), "purchase", expires_at=expires_at)

        first = store.sweep_expired_credits(user_id="user_1")
        assert first.expired_count == 1
        assert first.expired_amount == Decimal("40")
        assert store.get_balance("user_1").balance == Decimal("0")

        second = store.sweep_expired_credits(user_id="user_1")
        assert second.expired_count == 0
        assert second.expired_amount == Decimal("0")
        assert store.get_balance("user_1").balance == Decimal("0")

    def test_scoped_sweep_on_user_with_no_expired_grants_is_a_noop(self) -> None:
        store = MemoryStore()
        expires_at = datetime.now(UTC) - timedelta(hours=1)
        store.add_credits("user_1", Decimal("40"), "purchase", expires_at=expires_at)
        store.add_credits("user_2", Decimal("999"))  # never expires

        result = store.sweep_expired_credits(user_id="user_2")
        assert result.expired_count == 0
        assert result.expired_amount == Decimal("0")
        # user_1's expired grant is untouched by user_2's scoped sweep.
        assert store.get_balance("user_1").balance == Decimal("40")


# ── add_credits idempotency ──────────────────────────────────────────────────


class TestAddCreditsIdempotency:
    def test_replayed_idempotency_key_does_not_double_grant(self) -> None:
        store = MemoryStore()
        first = store.add_credits("user_1", Decimal("100"), "purchase", idempotency_key="grant-1")
        second = store.add_credits("user_1", Decimal("100"), "purchase", idempotency_key="grant-1")

        assert second.transaction_id == first.transaction_id
        assert second.amount == first.amount
        assert second.new_balance == first.new_balance
        # Balance reflects exactly ONE grant, not two.
        assert store.get_balance("user_1").balance == Decimal("100")

    def test_replayed_idempotency_key_creates_no_second_transaction(self) -> None:
        store = MemoryStore()
        store.add_credits("user_1", Decimal("50"), "purchase", idempotency_key="grant-1")
        store.add_credits("user_1", Decimal("50"), "purchase", idempotency_key="grant-1")
        store.add_credits("user_1", Decimal("50"), "purchase", idempotency_key="grant-1")

        transactions = store.list_user_transactions("user_1")
        assert len(transactions) == 1
        assert store.get_balance("user_1").balance == Decimal("50")

    def test_different_idempotency_keys_both_grant(self) -> None:
        store = MemoryStore()
        store.add_credits("user_1", Decimal("50"), "purchase", idempotency_key="grant-1")
        store.add_credits("user_1", Decimal("50"), "purchase", idempotency_key="grant-2")

        assert store.get_balance("user_1").balance == Decimal("100")
        assert len(store.list_user_transactions("user_1")) == 2

    def test_idempotency_key_is_user_scoped(self) -> None:
        """The same key for a different user grants independently (no cross-user collision)."""
        store = MemoryStore()
        store.add_credits("user_1", Decimal("50"), "purchase", idempotency_key="shared-key")
        store.add_credits("user_2", Decimal("75"), "purchase", idempotency_key="shared-key")

        assert store.get_balance("user_1").balance == Decimal("50")
        assert store.get_balance("user_2").balance == Decimal("75")

    def test_add_credits_without_idempotency_key_behaves_as_before(self) -> None:
        """Regression check: omitting idempotency_key still allows repeated identical grants."""
        store = MemoryStore()
        store.add_credits("user_1", Decimal("10"))
        store.add_credits("user_1", Decimal("10"))

        assert store.get_balance("user_1").balance == Decimal("20")
        assert len(store.list_user_transactions("user_1")) == 2


# ── Refunds ────────────────────────────────────────────────────────────────


class TestRefund:
    def test_full_refund_restores_balance(self) -> None:
        store = MemoryStore()
        store.add_credits("user_1", Decimal("100"), "purchase")
        # Deduct 30
        deduct = store.deduct_with_allowance("user_1", Decimal("30"))
        assert store.get_balance("user_1").balance == 70

        refund = store.refund_credits(deduct.transaction_id)
        assert refund.error is None
        assert refund.amount == 30
        assert store.get_balance("user_1").balance == 100

    def test_partial_refund(self) -> None:
        store = MemoryStore()
        store.add_credits("user_1", Decimal("100"))
        deduct = store.deduct_with_allowance("user_1", Decimal("50"))

        refund = store.refund_credits(deduct.transaction_id, amount=Decimal("20"))
        assert refund.error is None
        assert refund.amount == 20
        assert store.get_balance("user_1").balance == 70  # 50 + 20

    def test_double_refund_returns_error(self) -> None:
        store = MemoryStore()
        store.add_credits("user_1", Decimal("100"))
        deduct = store.deduct_with_allowance("user_1", Decimal("30"))

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

        store.deduct_with_allowance("user_1", Decimal("100"))
        store.deduct_with_allowance("user_1", Decimal("50"))
        store.deduct_with_allowance("user_2", Decimal("200"))

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

        store.deduct_with_allowance("user_1", Decimal("100"), model="gpt-4")
        store.deduct_with_allowance("user_1", Decimal("50"), model="claude-3")

        now = datetime.now(UTC)
        rows = store.spend_by_model(now - timedelta(seconds=10), now + timedelta(seconds=10))
        gpt4 = next((r for r in rows if r.model == "gpt-4"), None)
        assert gpt4 is not None
        assert gpt4.total_spend == 100

    def test_empty_time_window_returns_empty(self) -> None:
        store = MemoryStore()
        store.add_credits("user_1", Decimal("100"))
        store.deduct_with_allowance("user_1", Decimal("10"))

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
            store.deduct_with_allowance(uid, amt)

        now = datetime.now(UTC)
        top = store.top_users(2, now - timedelta(seconds=10), now + timedelta(seconds=10))
        assert len(top) == 2
        assert top[0].total_spend >= top[1].total_spend

    def test_aggregate_stats_returns_aggregates(self) -> None:
        store = MemoryStore()
        store.add_credits("user_1", Decimal("1000"))
        store.add_credits("user_2", Decimal("1000"))

        store.deduct_with_allowance("user_1", Decimal("50"), model="gpt-4")
        store.deduct_with_allowance("user_2", Decimal("30"), model="claude-3")

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
        store.deduct_with_allowance("user_1", Decimal("75"))

        now = datetime.now(UTC)
        rows = store.daily_spend(now - timedelta(days=1), now + timedelta(days=1))
        assert len(rows) >= 1
        assert rows[0].total_spend == 75
        assert rows[0].transaction_count == 1

    # ── Transaction listing ─────────────────────────────────────────────────

    def test_list_transactions_returns_all_for_user(self) -> None:
        from bursar.interface.models import CreditMetadata

        store = MemoryStore()
        store.add_credits("user_1", Decimal("1000"), "purchase", CreditMetadata(reference_id="purchase-1"))
        store.add_credits("user_1", Decimal("500"), "signup_bonus", CreditMetadata(reference_id="bonus-1"))
        store.deduct_with_allowance("user_1", Decimal("200"), model="gpt-4")
        store.add_credits("user_2", Decimal("999"), "purchase")
        result = store.list_user_transactions("user_1")
        assert len(result) == 3
        assert result[0].total_count == 3

    def test_list_transactions_filters_by_type(self) -> None:

        store = MemoryStore()
        store.add_credits("user_1", Decimal("1000"), "purchase")
        store.add_credits("user_1", Decimal("500"), "signup_bonus")
        store.deduct_with_allowance("user_1", Decimal("200"), model="gpt-4")
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
    from bursar.__main__ import _load_pricing_file

    f = tmp_path / "pricing.json"
    f.write_text('{"version": 1, "metering": {"models": {"*": "input_tokens * 1"}}}')
    data = _load_pricing_file(str(f))
    assert data["metering"]["models"]["*"] == "input_tokens * 1"


# ── M1 — Allowance monthly window reset ──────────────────────────────────────


def test_allowance_resets_across_billing_periods() -> None:
    """M1 / WS9f — allowance window is fresh in each calendar month.

    Uses MemoryStore's injectable clock (WS9f) to fast-forward past the month
    boundary without any wall-clock sleep.
    """
    clock_box = {"now": datetime(2024, 1, 15, tzinfo=UTC)}
    store = MemoryStore(clock=lambda: clock_box["now"])
    store.set_active_pricing(
        {
            "version": 1,
            "metering": {"models": {"*": "1"}},
            "plans": {"basic": {"label": "Basic", "allowance": {"amount": 5}}},
        }
    )
    store.set_user_plan("u", "basic")
    store.add_credits("u", Decimal("100"))

    # Period 1: consume 4 of the 5 allowance
    r1 = store.deduct_with_allowance("u", Decimal("4"))
    assert r1.error is None
    assert r1.allowance_consumed == Decimal("4")
    assert store.check_allowance("u").allowance_remaining == Decimal("1")

    # Simulate month rollover via the injectable clock.
    clock_box["now"] = datetime(2024, 2, 1, tzinfo=UTC)

    # Period 2: allowance should reset to 5 (fresh calendar month).
    assert store.check_allowance("u").allowance_remaining == Decimal("5")
    r2 = store.deduct_with_allowance("u", Decimal("4"))
    assert r2.error is None
    assert r2.allowance_consumed == Decimal("4")


# ── M2 — Spend cap accumulates across deductions then blocks ──────────────────


def test_spend_cap_accumulates_across_deductions_then_blocks() -> None:
    """M2 — cap usage accumulates; the deduction that would push over the cap is denied."""
    store = MemoryStore()
    store.add_credits("u", Decimal("1000"))
    store.set_spend_cap(SpendCap(user_id="u", type="daily", limit=Decimal("10"), action="deny"))

    r1 = store.deduct_with_allowance("u", Decimal("4"))
    assert r1.error is None

    r2 = store.deduct_with_allowance("u", Decimal("4"))
    assert r2.error is None

    # Third deduction: prior spend = 8, adding 4 would give 12 > 10 → denied
    r3 = store.deduct_with_allowance("u", Decimal("4"))
    assert r3.error == "cap_reached"

    # Only two successful deductions; balance = 1000 - 4 - 4 = 992
    assert store.get_balance("u").balance == Decimal("992")


# ── M3 — Partial expiry: one grant expires, others don't ─────────────────────


def test_partial_credit_expiry() -> None:
    """M3 — sweep removes expired grants; permanent grants are unaffected."""
    store = MemoryStore()

    # Add 10 credits that expire in the past
    expired_at = datetime.now(UTC) - timedelta(days=1)
    store.add_credits("u", Decimal("10"), "purchase", expires_at=expired_at)

    # Add 5 credits with no expiry
    store.add_credits("u", Decimal("5"), "purchase")

    first = store.sweep_expired_credits()
    assert first.expired_count == 1
    assert first.expired_amount == Decimal("10")
    assert store.get_balance("u").balance == Decimal("5")

    # Second sweep: nothing left to expire — idempotent
    second = store.sweep_expired_credits()
    assert second.expired_count == 0
    assert second.expired_amount == Decimal("0")
    assert store.get_balance("u").balance == Decimal("5")


# ── M14 — Transaction listing pagination ─────────────────────────────────────


def test_list_user_transactions_pagination() -> None:
    """M14 — list_user_transactions pages correctly across 15 transactions."""
    store = MemoryStore()

    # Create exactly 15 purchase transactions for a dedicated user
    for _i in range(15):
        store.add_credits("user-pg", Decimal("1"), "purchase")

    page1 = store.list_user_transactions("user-pg", limit=5, offset=0)
    assert len(page1) == 5
    assert page1[0].total_count == 15

    page2 = store.list_user_transactions("user-pg", limit=5, offset=5)
    assert len(page2) == 5
    assert page2[0].total_count == 15

    page3 = store.list_user_transactions("user-pg", limit=5, offset=10)
    assert len(page3) == 5
    assert page3[0].total_count == 15

    # Beyond end — empty, no error
    page4 = store.list_user_transactions("user-pg", limit=5, offset=15)
    assert len(page4) == 0

    # Verify no duplicates across pages
    ids_p1 = {r.id for r in page1}
    ids_p2 = {r.id for r in page2}
    ids_p3 = {r.id for r in page3}
    assert len(ids_p1 & ids_p2) == 0, "Pages 1 and 2 overlap"
    assert len(ids_p2 & ids_p3) == 0, "Pages 2 and 3 overlap"
    assert len(ids_p1 & ids_p3) == 0, "Pages 1 and 3 overlap"


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


# ── Feature limits (per-feature invocation-count limits) ───────────────────
#
# Counting is ledger-derived, exactly like spend caps: a feature invocation is
# counted by counting already-committed `usage` transactions tagged
# `metadata.feature == feature` within `[period_start, period_end)`. There is
# no separate counter to decrement/restore, so `release_lease`/`refund_credits`
# never free up quota (see the dedicated tests at the end of this class).


class TestFeatureLimits:
    def test_no_limit_configured_deduct_never_blocks(self) -> None:
        store = MemoryStore()
        store.add_credits("u", Decimal("1000"))
        for _ in range(10):
            r = store.deduct_with_allowance("u", Decimal("1"), feature="export")
            assert r.error is None
            assert r.feature_limit_warning is None

    def test_no_limit_configured_create_lease_never_blocks(self) -> None:
        store = MemoryStore()
        store.add_credits("u", Decimal("1000"))
        lease = store.create_lease("u", Decimal("1"), "op", feature="export")
        assert lease.error is None

    def test_no_limit_configured_settle_lease_never_blocks(self) -> None:
        store = MemoryStore()
        store.add_credits("u", Decimal("1000"))
        lease = store.create_lease("u", Decimal("1"), "op")
        r = store.settle_lease("u", lease.lease_id, Decimal("1"), feature="export")
        assert r.error is None
        assert r.feature_limit_warning is None

    def test_feature_tagged_on_transaction_even_without_limit_configured(self) -> None:
        """A `feature` name is always tagged, regardless of whether a limit is
        configured, so enabling a limit later still has accurate history."""
        store = MemoryStore()
        store.add_credits("u", Decimal("1000"))
        store.deduct_with_allowance("u", Decimal("1"), feature="export")
        result = store.check_feature_limit("u", "export", 100, date(2000, 1, 1), date(2100, 1, 1))
        assert result.used == 1

    def test_deny_action_allows_calls_under_limit(self) -> None:
        store = MemoryStore(clock=lambda: datetime(2024, 6, 15, tzinfo=UTC))
        store.add_credits("u", Decimal("1000"))
        limit = FeatureLimit(max_calls=3, period="monthly", action="deny")
        period_start = date(2024, 6, 1)
        for i in range(3):
            r = store.deduct_with_allowance(
                "u", Decimal("1"), feature="export", feature_limit=limit, feature_period_start=period_start
            )
            assert r.error is None, f"call {i} should be under the limit"

    def test_deny_action_blocks_call_that_would_exceed_limit(self) -> None:
        store = MemoryStore(clock=lambda: datetime(2024, 6, 15, tzinfo=UTC))
        store.add_credits("u", Decimal("1000"))
        limit = FeatureLimit(max_calls=3, period="monthly", action="deny")
        period_start = date(2024, 6, 1)
        for _ in range(3):
            store.deduct_with_allowance(
                "u", Decimal("1"), feature="export", feature_limit=limit, feature_period_start=period_start
            )
        r = store.deduct_with_allowance(
            "u", Decimal("1"), feature="export", feature_limit=limit, feature_period_start=period_start
        )
        assert r.error == "feature_limit_reached"
        # Nothing committed on the denied call: balance only reflects 3 debits.
        assert store.get_balance("u").balance == Decimal("997")

    def test_deny_action_keeps_blocking_over_limit(self) -> None:
        store = MemoryStore(clock=lambda: datetime(2024, 6, 15, tzinfo=UTC))
        store.add_credits("u", Decimal("1000"))
        limit = FeatureLimit(max_calls=1, period="monthly", action="deny")
        period_start = date(2024, 6, 1)
        store.deduct_with_allowance(
            "u", Decimal("1"), feature="export", feature_limit=limit, feature_period_start=period_start
        )
        for _ in range(3):
            r = store.deduct_with_allowance(
                "u", Decimal("1"), feature="export", feature_limit=limit, feature_period_start=period_start
            )
            assert r.error == "feature_limit_reached"

    def test_warn_action_allows_through_and_signals_after_limit_reached(self) -> None:
        store = MemoryStore(clock=lambda: datetime(2024, 6, 15, tzinfo=UTC))
        store.add_credits("u", Decimal("1000"))
        limit = FeatureLimit(max_calls=1, period="monthly", action="warn")
        period_start = date(2024, 6, 1)
        r1 = store.deduct_with_allowance(
            "u", Decimal("1"), feature="export", feature_limit=limit, feature_period_start=period_start
        )
        assert r1.error is None
        assert r1.feature_limit_warning is None

        r2 = store.deduct_with_allowance(
            "u", Decimal("1"), feature="export", feature_limit=limit, feature_period_start=period_start
        )
        assert r2.error is None
        assert r2.feature_limit_warning == "warn"

    def test_notify_action_allows_through_and_signals_after_limit_reached(self) -> None:
        store = MemoryStore(clock=lambda: datetime(2024, 6, 15, tzinfo=UTC))
        store.add_credits("u", Decimal("1000"))
        limit = FeatureLimit(max_calls=1, period="monthly", action="notify")
        period_start = date(2024, 6, 1)
        store.deduct_with_allowance(
            "u", Decimal("1"), feature="export", feature_limit=limit, feature_period_start=period_start
        )
        r2 = store.deduct_with_allowance(
            "u", Decimal("1"), feature="export", feature_limit=limit, feature_period_start=period_start
        )
        assert r2.error is None
        assert r2.feature_limit_warning == "notify"

    def test_per_feature_isolation(self) -> None:
        store = MemoryStore(clock=lambda: datetime(2024, 6, 15, tzinfo=UTC))
        store.add_credits("u", Decimal("1000"))
        limit = FeatureLimit(max_calls=1, period="monthly", action="deny")
        period_start = date(2024, 6, 1)
        store.deduct_with_allowance(
            "u", Decimal("1"), feature="export", feature_limit=limit, feature_period_start=period_start
        )
        blocked = store.deduct_with_allowance(
            "u", Decimal("1"), feature="export", feature_limit=limit, feature_period_start=period_start
        )
        assert blocked.error == "feature_limit_reached"

        # A different feature name has its own independent count.
        other = store.deduct_with_allowance(
            "u", Decimal("1"), feature="import", feature_limit=limit, feature_period_start=period_start
        )
        assert other.error is None

    def test_per_user_isolation(self) -> None:
        store = MemoryStore(clock=lambda: datetime(2024, 6, 15, tzinfo=UTC))
        store.add_credits("u1", Decimal("1000"))
        store.add_credits("u2", Decimal("1000"))
        limit = FeatureLimit(max_calls=1, period="monthly", action="deny")
        period_start = date(2024, 6, 1)
        store.deduct_with_allowance(
            "u1", Decimal("1"), feature="export", feature_limit=limit, feature_period_start=period_start
        )
        blocked = store.deduct_with_allowance(
            "u1", Decimal("1"), feature="export", feature_limit=limit, feature_period_start=period_start
        )
        assert blocked.error == "feature_limit_reached"

        # A different user's count is independent.
        other = store.deduct_with_allowance(
            "u2", Decimal("1"), feature="export", feature_limit=limit, feature_period_start=period_start
        )
        assert other.error is None

    def test_accumulation_then_block_across_n_deducts(self) -> None:
        store = MemoryStore(clock=lambda: datetime(2024, 6, 15, tzinfo=UTC))
        store.add_credits("u", Decimal("1000"))
        limit = FeatureLimit(max_calls=5, period="monthly", action="deny")
        period_start = date(2024, 6, 1)
        for i in range(5):
            r = store.deduct_with_allowance(
                "u", Decimal("1"), feature="export", feature_limit=limit, feature_period_start=period_start
            )
            assert r.error is None, f"call {i} should succeed"

        blocked = store.deduct_with_allowance(
            "u", Decimal("1"), feature="export", feature_limit=limit, feature_period_start=period_start
        )
        assert blocked.error == "feature_limit_reached"

        check = store.check_feature_limit("u", "export", 5, period_start, date(2024, 7, 1))
        assert check.used == 5
        assert check.remaining == 0

    # ── create_lease: deny-only at admission ────────────────────────────

    def test_create_lease_deny_action_blocks_at_admission(self) -> None:
        store = MemoryStore(clock=lambda: datetime(2024, 6, 15, tzinfo=UTC))
        store.add_credits("u", Decimal("1000"))
        limit = FeatureLimit(max_calls=1, period="monthly", action="deny")
        period_start = date(2024, 6, 1)
        store.deduct_with_allowance(
            "u", Decimal("1"), feature="export", feature_limit=limit, feature_period_start=period_start
        )
        lease = store.create_lease(
            "u", Decimal("1"), "op", feature="export", feature_limit=limit, feature_period_start=period_start
        )
        assert lease.error == "feature_limit_reached"

    def test_create_lease_warn_action_not_enforced_at_admission(self) -> None:
        store = MemoryStore(clock=lambda: datetime(2024, 6, 15, tzinfo=UTC))
        store.add_credits("u", Decimal("1000"))
        limit = FeatureLimit(max_calls=1, period="monthly", action="warn")
        period_start = date(2024, 6, 1)
        store.deduct_with_allowance(
            "u", Decimal("1"), feature="export", feature_limit=limit, feature_period_start=period_start
        )
        # Quota is already at/over the limit, but warn/notify are advisory-only
        # signals with nothing to warn about at admission -- never enforced here.
        lease = store.create_lease(
            "u", Decimal("1"), "op", feature="export", feature_limit=limit, feature_period_start=period_start
        )
        assert lease.error is None

    # ── settle_lease: advisory-only, never blocks ───────────────────────

    def test_settle_lease_deny_action_never_blocks_but_warns(self) -> None:
        store = MemoryStore(clock=lambda: datetime(2024, 6, 15, tzinfo=UTC))
        store.add_credits("u", Decimal("1000"))
        limit = FeatureLimit(max_calls=1, period="monthly", action="deny")
        period_start = date(2024, 6, 1)
        store.deduct_with_allowance(
            "u", Decimal("1"), feature="export", feature_limit=limit, feature_period_start=period_start
        )
        lease = store.create_lease("u", Decimal("1"), "op")
        r = store.settle_lease(
            "u", lease.lease_id, Decimal("1"), feature="export", feature_limit=limit, feature_period_start=period_start
        )
        assert r.error is None
        assert r.feature_limit_warning == "deny"

    def test_settle_lease_warn_action_signals(self) -> None:
        store = MemoryStore(clock=lambda: datetime(2024, 6, 15, tzinfo=UTC))
        store.add_credits("u", Decimal("1000"))
        limit = FeatureLimit(max_calls=1, period="monthly", action="warn")
        period_start = date(2024, 6, 1)
        store.deduct_with_allowance(
            "u", Decimal("1"), feature="export", feature_limit=limit, feature_period_start=period_start
        )
        lease = store.create_lease("u", Decimal("1"), "op")
        r = store.settle_lease(
            "u", lease.lease_id, Decimal("1"), feature="export", feature_limit=limit, feature_period_start=period_start
        )
        assert r.error is None
        assert r.feature_limit_warning == "warn"

    # ── release_lease / refund_credits do NOT restore quota ─────────────

    def test_release_lease_does_not_restore_quota(self) -> None:
        store = MemoryStore(clock=lambda: datetime(2024, 6, 15, tzinfo=UTC))
        store.add_credits("u", Decimal("1000"))
        period_start = date(2024, 6, 1)
        warn_limit = FeatureLimit(max_calls=1, period="monthly", action="warn")
        deny_limit = FeatureLimit(max_calls=1, period="monthly", action="deny")

        # Exhaust the quota via one committed deduct.
        r1 = store.deduct_with_allowance(
            "u", Decimal("1"), feature="export", feature_limit=warn_limit, feature_period_start=period_start
        )
        assert r1.error is None

        # Reserve (admission never checks warn, so this succeeds) then release
        # without settling -- a released lease never inserted a usage row.
        lease = store.create_lease(
            "u", Decimal("1"), "op", feature="export", feature_limit=warn_limit, feature_period_start=period_start
        )
        assert lease.error is None
        released = store.release_lease("u", lease.lease_id)
        assert released.released is True

        # If release had freed up quota, this deny check would now pass
        # (count back at 0). It must still see the single committed usage.
        r2 = store.deduct_with_allowance(
            "u", Decimal("1"), feature="export", feature_limit=deny_limit, feature_period_start=period_start
        )
        assert r2.error == "feature_limit_reached"

    def test_refund_credits_does_not_restore_quota(self) -> None:
        store = MemoryStore(clock=lambda: datetime(2024, 6, 15, tzinfo=UTC))
        store.add_credits("u", Decimal("1000"))
        period_start = date(2024, 6, 1)
        deny_limit = FeatureLimit(max_calls=1, period="monthly", action="deny")

        d = store.deduct_with_allowance(
            "u", Decimal("1"), feature="export", feature_limit=deny_limit, feature_period_start=period_start
        )
        assert d.error is None

        refund = store.refund_credits(d.transaction_id)
        assert refund.error is None

        # Refunding does not delete the original usage row -- quota unaffected.
        r2 = store.deduct_with_allowance(
            "u", Decimal("1"), feature="export", feature_limit=deny_limit, feature_period_start=period_start
        )
        assert r2.error == "feature_limit_reached"

    # ── Window rollover across all four cadences ────────────────────────

    def _rollover_check(self, period: str, before: datetime, after: datetime) -> None:
        clock_box = {"now": before}
        store = MemoryStore(clock=lambda: clock_box["now"])
        store.add_credits("u", Decimal("1000"))
        limit = FeatureLimit(max_calls=1, period=period, action="deny")

        period_start, _ = resolve_calendar_window(clock_box["now"], period)
        r1 = store.deduct_with_allowance(
            "u", Decimal("1"), feature="export", feature_limit=limit, feature_period_start=period_start
        )
        assert r1.error is None

        blocked = store.deduct_with_allowance(
            "u", Decimal("1"), feature="export", feature_limit=limit, feature_period_start=period_start
        )
        assert blocked.error == "feature_limit_reached"

        # Advance the clock past the window boundary; a fresh window resolves
        # a new period_start, and the counter is reset (ledger-derived: only
        # transactions inside the new window are counted).
        clock_box["now"] = after
        new_period_start, _ = resolve_calendar_window(clock_box["now"], period)
        r2 = store.deduct_with_allowance(
            "u", Decimal("1"), feature="export", feature_limit=limit, feature_period_start=new_period_start
        )
        assert r2.error is None

    def test_window_rollover_daily(self) -> None:
        self._rollover_check("daily", datetime(2024, 6, 15, 12, 0, tzinfo=UTC), datetime(2024, 6, 16, 0, 0, tzinfo=UTC))

    def test_window_rollover_weekly(self) -> None:
        # 2024-06-13 is a Thursday (week of Mon 6/10); 6/17 is the next Monday.
        self._rollover_check("weekly", datetime(2024, 6, 13, tzinfo=UTC), datetime(2024, 6, 17, tzinfo=UTC))

    def test_window_rollover_monthly(self) -> None:
        self._rollover_check("monthly", datetime(2024, 1, 15, tzinfo=UTC), datetime(2024, 2, 1, tzinfo=UTC))

    def test_window_rollover_yearly(self) -> None:
        self._rollover_check("yearly", datetime(2024, 6, 15, tzinfo=UTC), datetime(2025, 1, 1, tzinfo=UTC))

    # ── check_feature_limit: advisory read, no side effects ─────────────

    def test_check_feature_limit_reports_used_and_remaining(self) -> None:
        store = MemoryStore(clock=lambda: datetime(2024, 6, 15, tzinfo=UTC))
        store.add_credits("u", Decimal("1000"))
        period_start = date(2024, 6, 1)
        period_end = date(2024, 7, 1)
        store.deduct_with_allowance("u", Decimal("1"), feature="export")
        store.deduct_with_allowance("u", Decimal("1"), feature="export")

        result = store.check_feature_limit("u", "export", 5, period_start, period_end)
        assert result.limited is True
        assert result.limit == 5
        assert result.used == 2
        assert result.remaining == 3
        assert result.period_start == str(period_start)
        assert result.period_end == str(period_end)

    def test_check_feature_limit_is_side_effect_free(self) -> None:
        store = MemoryStore()
        store.add_credits("u", Decimal("1000"))
        period_start = date(2024, 6, 1)
        period_end = date(2024, 7, 1)
        for _ in range(3):
            store.check_feature_limit("u", "export", 1, period_start, period_end)

        result = store.check_feature_limit("u", "export", 1, period_start, period_end)
        assert result.used == 0


# ── ST2: Concurrent refund race on same transaction ────────────────────────────


class TestConcurrentRefund:
    def test_concurrent_partial_refunds_never_exceed_original(self) -> None:
        """ST2 — Two concurrent partial refunds on the same transaction: combined refund never exceeds original."""
        import concurrent.futures

        store = MemoryStore()
        store.add_credits("user-1", Decimal("100"), "purchase")

        deduct = store.deduct_with_allowance("user-1", Decimal("40"))
        tx_id = deduct.transaction_id

        results = []

        def do_refund() -> None:
            r = store.refund_credits(tx_id, amount=Decimal("30"))
            results.append(r)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            futures = [ex.submit(do_refund), ex.submit(do_refund)]
            concurrent.futures.wait(futures)

        successes = [r for r in results if r.error is None]
        errors = [r for r in results if r.error is not None]

        # Exactly one should succeed; the other should fail
        assert len(successes) == 1
        assert len(errors) == 1
        assert errors[0].error in ("over_refund", "already_refunded")

        # Final balance must not exceed original 100
        final_balance = store.get_balance("user-1").balance
        # After 40 deducted and 30 refunded: 60 + 30 = 90
        assert final_balance == Decimal("90")


# ── ST3: Sweep when balance < total expired amount ──────────────────────────────


class TestSweepBalanceCap:
    def test_sweep_caps_at_actual_balance(self) -> None:
        """ST3 — Sweep debits at most the actual balance, not the full expired amount."""
        store = MemoryStore()

        # Add 100 expiring credits and 50 non-expiring credits
        expires_at = datetime.now(UTC) - timedelta(hours=1)
        store.add_credits("user-1", Decimal("100"), "purchase", expires_at=expires_at)
        store.add_credits("user-1", Decimal("50"), "purchase")

        # Deduct 80 so balance = 70 (100 + 50 - 80)
        store.deduct_with_allowance("user-1", Decimal("80"), min_balance=Decimal("0"))
        assert store.get_balance("user-1").balance == Decimal("70")

        # Sweep: expired amount is 100 but balance is only 70 — should debit 70
        result = store.sweep_expired_credits()
        assert result.expired_amount == Decimal("70")
        assert store.get_balance("user-1").balance == Decimal("0")


# ── ST4: Team member per-user spend cap accumulation ──────────────────────────


class TestTeamMemberSpendCap:
    def test_per_user_spend_cap_accumulates(self) -> None:
        """ST4 — Per-user team spend cap blocks cumulative overspend."""
        store = MemoryStore()
        team = store.create_team("Alpha", initial_balance=Decimal("1000"))

        store.add_team_member(team.team_id, "u1", spend_cap=Decimal("200"))
        store.add_team_member(team.team_id, "u2", spend_cap=Decimal("150"))

        # u1: 150 deduction — under 200 cap
        r1 = store.deduct_team(team.team_id, "u1", Decimal("150"))
        assert r1.error is None

        # u1: 100 more deduction — 150+100=250 > 200 cap
        r2 = store.deduct_team(team.team_id, "u1", Decimal("100"))
        assert r2.error == "spend_cap_exceeded"

        # u2: 149 deduction — under 150 cap
        r3 = store.deduct_team(team.team_id, "u2", Decimal("149"))
        assert r3.error is None

        # u2: 10 more deduction — 149+10=159 > 150 cap
        r4 = store.deduct_team(team.team_id, "u2", Decimal("10"))
        assert r4.error == "spend_cap_exceeded"


# ── ST5: listUserTransactions type filter ──────────────────────────────────────


class TestListUserTransactionsTypeFilter:
    def test_type_filter_usage_only(self) -> None:
        """ST5 — list_user_transactions with types=['usage'] returns only usage transactions."""
        store = MemoryStore()
        # Add a purchase transaction
        store.add_credits("user-1", Decimal("100"), "purchase")
        # Add a usage (debit) transaction
        store.deduct_with_allowance("user-1", Decimal("20"))

        result = store.list_user_transactions("user-1", types=["usage"])
        assert len(result) == 1
        assert result[0].type == "usage"

    def test_type_filter_purchase_only(self) -> None:
        """ST5 — list_user_transactions with types=['purchase'] returns only purchase transactions."""
        store = MemoryStore()
        store.add_credits("user-1", Decimal("100"), "purchase")
        store.deduct_with_allowance("user-1", Decimal("20"))

        result = store.list_user_transactions("user-1", types=["purchase"])
        assert len(result) == 1
        assert result[0].type == "purchase"


# ── ST6: listUserTransactions pagination ──────────────────────────────────────


class TestListUserTransactionsPagination:
    def test_pagination_first_page(self) -> None:
        """ST6 — limit=2 offset=0 returns 2 results from 5 transactions."""
        store = MemoryStore()
        for _ in range(5):
            store.add_credits("user-1", Decimal("10"), "purchase")

        page = store.list_user_transactions("user-1", limit=2, offset=0)
        assert len(page) == 2
        assert page[0].total_count == 5

    def test_pagination_last_page(self) -> None:
        """ST6 — limit=2 offset=4 returns 1 result (last item of 5)."""
        store = MemoryStore()
        for _ in range(5):
            store.add_credits("user-1", Decimal("10"), "purchase")

        page = store.list_user_transactions("user-1", limit=2, offset=4)
        assert len(page) == 1
        assert page[0].total_count == 5

    def test_pagination_beyond_end(self) -> None:
        """ST6 — limit=2 offset=5 returns 0 results (no error)."""
        store = MemoryStore()
        for _ in range(5):
            store.add_credits("user-1", Decimal("10"), "purchase")

        page = store.list_user_transactions("user-1", limit=2, offset=5)
        assert len(page) == 0


# ── ST7: check_feature with numeric 0, Decimal("0"), and False ─────────────────


class TestCheckFeatureZeroValues:
    def _make_store_with_features(self, features: dict) -> MemoryStore:
        store = MemoryStore()
        store.set_active_pricing(
            {
                "version": 1,
                "metering": {"models": {"*": "1"}},
                "plans": {
                    "p": {
                        "label": "P",
                        "entitlements": {k: {"value": v} for k, v in features.items()},
                    }
                },
            }
        )
        store.set_user_plan("user-1", "p")
        return store

    def test_float_zero_is_present(self) -> None:
        """ST7 — feature value float(0.0) is treated as present (has_feature=True)."""
        store = self._make_store_with_features({"quota": 0.0})
        result = store.check_feature("user-1", "quota")
        assert result.has_feature is True
        assert result.value == 0.0

    def test_decimal_zero_is_present(self) -> None:
        """ST7 — feature value Decimal('0') is treated as present (has_feature=True)."""
        from decimal import Decimal as D

        store = self._make_store_with_features({"quota": D("0")})
        result = store.check_feature("user-1", "quota")
        assert result.has_feature is True

    def test_false_is_absent(self) -> None:
        """ST7 — feature value False is treated as absent (has_feature=False)."""
        store = self._make_store_with_features({"disabled_feature": False})
        result = store.check_feature("user-1", "disabled_feature")
        assert result.has_feature is False


# ── WS8: CreditStore ABC split into core (required) + optional capabilities ──


class _MinimalCoreStore(CreditStore):
    """A store implementing ONLY the core abstract methods of CreditStore.

    Exercises WS8: analytics/transaction-listing/teams are optional
    capabilities with a default raise, so a minimal custom store subclass
    does not need to implement all ~35 methods — only the core ones.
    """

    def setup(self, database_url: str | None = None):
        return SetupResult()

    def get_balance(self, user_id: str):
        return BalanceResult(user_id=user_id)

    def add_credits(self, user_id, amount, type="adjustment", metadata=None, expires_at=None, tier=None):
        return AddCreditsResult(transaction_id="tx", user_id=user_id, amount=amount, new_balance=amount)

    def get_bucket_balances(self, user_id: str):
        return BucketBalancesResult(user_id=user_id, buckets=[], total_balance=Decimal(0))

    def deduct_with_allowance(
        self,
        user_id,
        amount,
        *,
        idempotency_key=None,
        min_balance=Decimal(0),
        model=None,
        metadata=None,
        skip_allowance=False,
    ):
        return DeductionResult(transaction_id="tx", user_id=user_id, amount=amount, balance_after=Decimal(0))

    def create_lease(
        self,
        user_id,
        amount,
        operation_type,
        *,
        billing_mode="strict",
        floor=Decimal(0),
        max_concurrent=None,
        ttl_seconds=600,
        model=None,
        overdraft_floor=None,
        metadata=None,
    ):
        return LeaseResult(lease_id="lease", user_id=user_id, amount=amount)

    def settle_lease(
        self,
        user_id,
        lease_id,
        amount,
        *,
        idempotency_key=None,
        min_balance=Decimal(0),
        model=None,
        metadata=None,
        skip_allowance=False,
    ):
        return DeductionResult(transaction_id="tx", user_id=user_id, amount=amount, balance_after=Decimal(0))

    def release_lease(self, user_id, lease_id):
        return ReleaseResult(lease_id=lease_id, user_id=user_id, released=True, reason="released")

    def renew_lease(self, user_id, lease_id, ttl_seconds):
        return LeaseResult(lease_id=lease_id, user_id=user_id)

    def get_available(self, user_id: str):
        return AvailableResult(user_id=user_id)

    def get_active_pricing(self):
        return None

    def set_active_pricing(self, config, label=None):
        return "id"

    def get_pricing_history(self):
        return []

    def get_pricing_config(self, version: int):
        return None

    def activate_pricing(self, version: int):
        return "id"

    def get_user_plan(self, user_id: str):
        return GetUserPlanResult(user_id=user_id)

    def set_user_plan(self, user_id: str, plan_id: str):
        return SetUserPlanResult(user_id=user_id, plan_id=plan_id)

    def unset_user_plan(self, user_id: str) -> dict:
        return {"user_id": user_id}

    def check_allowance(self, user_id: str):
        return AllowanceResult(plan_id="", allowance_remaining=Decimal(0), period_start="", period_end="")

    def increment_usage_window(self, user_id: str, plan_id: str, amount: Decimal) -> None:
        return None

    def check_spend_cap(self, user_id: str, model=None, amount=None):
        return CapCheckResult()

    def check_feature_limit(self, user_id: str, feature: str, max_calls: int, period_start, period_end):
        return FeatureLimitResult(user_id=user_id, feature=feature)

    def refund_credits(self, transaction_id, amount=None, reason=None, metadata=None):
        return RefundResult(refund_transaction_id="", original_transaction_id=transaction_id, user_id="")

    def sweep_expired_credits(self, dry_run: bool = False):
        return SweepResult(dry_run=dry_run)

    def revoke_credits_by_tx_type(self, user_id: str, tx_type: str) -> dict:
        return {"user_id": user_id, "amount": 0, "new_balance": "0", "tier": None}


class TestOptionalCapabilities:
    """WS8 — analytics/transaction-listing/teams raise CapabilityNotSupportedError
    by default on a store that only implements the core abstract methods."""

    def test_minimal_store_instantiates(self) -> None:
        # No TypeError for missing abstract methods — the optional-capability
        # group is now concrete with a default raise, not abstract.
        store = _MinimalCoreStore()
        assert store.get_balance("u1").user_id == "u1"

    def test_minimal_store_implements_get_bucket_balances(self) -> None:
        """get_bucket_balances is a CORE abstract method (credit buckets), not an
        optional capability — _MinimalCoreStore already implements it (see its
        class body above), so this must succeed with no
        CapabilityNotSupportedError."""
        store = _MinimalCoreStore()
        result = store.get_bucket_balances("u1")
        assert result.user_id == "u1"

    def test_get_bucket_balances_is_a_required_core_abstract_method(self) -> None:
        """get_bucket_balances is required (unlike the optional-capability group
        below, which has a default raise): it's part of
        CreditStore.__abstractmethods__, so a store subclass implementing
        every OTHER core method but omitting it cannot be instantiated at all
        (TypeError) — confirming it's core to the ABC contract added for
        buckets, not an optional capability with a default implementation."""
        assert "get_bucket_balances" in CreditStore.__abstractmethods__

        # Build a store with every _MinimalCoreStore method EXCEPT
        # get_bucket_balances, based directly on CreditStore (not
        # _MinimalCoreStore, whose inherited implementation would otherwise
        # satisfy the ABC).
        namespace = {k: v for k, v in _MinimalCoreStore.__dict__.items() if k != "get_bucket_balances"}
        missing_buckets_store_cls = type("_MissingBucketsStore", (CreditStore,), namespace)
        with pytest.raises(TypeError, match="get_bucket_balances"):
            missing_buckets_store_cls()

    def test_create_team_raises_capability_not_supported(self) -> None:
        store = _MinimalCoreStore()
        with pytest.raises(CapabilityNotSupportedError):
            store.create_team("acme")

    def test_spend_by_user_raises_capability_not_supported(self) -> None:
        store = _MinimalCoreStore()
        with pytest.raises(CapabilityNotSupportedError):
            store.spend_by_user(datetime.now(UTC), datetime.now(UTC))

    def test_list_user_transactions_raises_capability_not_supported(self) -> None:
        store = _MinimalCoreStore()
        with pytest.raises(CapabilityNotSupportedError):
            store.list_user_transactions("u1")

    def test_capability_not_supported_is_a_store_error(self) -> None:
        """CapabilityNotSupportedError is a StoreError subclass (contract §4 style)."""
        assert issubclass(CapabilityNotSupportedError, StoreError)


# ── WS9f: configurable allowance-period rollover (MemoryStore, injectable clock) ──


class TestAllowancePeriodRollover:
    """Allowance rollover across all three allowance_period modes.

    Uses MemoryStore's injectable clock (WS9f) to fast-forward past window
    boundaries with no wall-clock sleep. Each mode is tested through BOTH the
    deduct_with_allowance path and the settle_lease (lease) path, since they
    independently key allowance consumption.
    """

    def _store_with_plan(self, period: str, allowance: Decimal, now: datetime) -> tuple[MemoryStore, dict]:
        clock_box = {"now": now}
        store = MemoryStore(clock=lambda: clock_box["now"])
        store.set_active_pricing(
            {
                "version": 1,
                "metering": {"models": {"*": "1"}},
                "plans": {
                    "basic": {"label": "Basic", "allowance": {"amount": allowance, "period": period}},
                },
                "ledger": {"min_balance": 0},
            }
        )
        store.set_user_plan("u", "basic")
        store.add_credits("u", Decimal("1000"))
        return store, clock_box

    # ── calendar_month ──────────────────────────────────────────────────

    def test_calendar_month_resets_via_deduct(self) -> None:
        store, clock = self._store_with_plan("calendar_month", Decimal("5"), datetime(2024, 1, 15, tzinfo=UTC))
        store.deduct_with_allowance("u", Decimal("4"))
        assert store.check_allowance("u").allowance_remaining == Decimal("1")
        clock["now"] = datetime(2024, 2, 1, tzinfo=UTC)
        assert store.check_allowance("u").allowance_remaining == Decimal("5")

    def test_calendar_month_resets_via_settle(self) -> None:
        store, clock = self._store_with_plan("calendar_month", Decimal("5"), datetime(2024, 1, 15, tzinfo=UTC))
        lease = store.create_lease("u", Decimal("4"), "usage", floor=Decimal(0))
        store.settle_lease("u", lease.lease_id, Decimal("4"))
        assert store.check_allowance("u").allowance_remaining == Decimal("1")
        clock["now"] = datetime(2024, 2, 1, tzinfo=UTC)
        assert store.check_allowance("u").allowance_remaining == Decimal("5")

    # ── rolling_30d ──────────────────────────────────────────────────────

    def test_rolling_30d_resets_via_deduct(self) -> None:
        store, clock = self._store_with_plan("rolling_30d", Decimal("5"), datetime(2024, 1, 1, tzinfo=UTC))
        store.deduct_with_allowance("u", Decimal("4"))
        assert store.check_allowance("u").allowance_remaining == Decimal("1")
        # Still within the first 30-day window (29 days elapsed).
        clock["now"] = datetime(2024, 1, 30, tzinfo=UTC)
        assert store.check_allowance("u").allowance_remaining == Decimal("1")
        # 30 days elapsed -> rolls into a fresh window.
        clock["now"] = datetime(2024, 1, 31, tzinfo=UTC)
        assert store.check_allowance("u").allowance_remaining == Decimal("5")

    def test_rolling_30d_resets_via_settle(self) -> None:
        store, clock = self._store_with_plan("rolling_30d", Decimal("5"), datetime(2024, 1, 1, tzinfo=UTC))
        lease = store.create_lease("u", Decimal("4"), "usage", floor=Decimal(0))
        store.settle_lease("u", lease.lease_id, Decimal("4"))
        assert store.check_allowance("u").allowance_remaining == Decimal("1")
        clock["now"] = datetime(2024, 1, 31, tzinfo=UTC)
        assert store.check_allowance("u").allowance_remaining == Decimal("5")

    # ── anniversary ──────────────────────────────────────────────────────

    def test_anniversary_resets_via_deduct(self) -> None:
        store, clock = self._store_with_plan("anniversary", Decimal("5"), datetime(2024, 1, 15, tzinfo=UTC))
        store.deduct_with_allowance("u", Decimal("4"))
        assert store.check_allowance("u").allowance_remaining == Decimal("1")
        # Before the 15th next month: still in the same window.
        clock["now"] = datetime(2024, 2, 10, tzinfo=UTC)
        assert store.check_allowance("u").allowance_remaining == Decimal("1")
        # On/after the 15th next month: fresh window.
        clock["now"] = datetime(2024, 2, 15, tzinfo=UTC)
        assert store.check_allowance("u").allowance_remaining == Decimal("5")

    def test_anniversary_resets_via_settle(self) -> None:
        store, clock = self._store_with_plan("anniversary", Decimal("5"), datetime(2024, 1, 15, tzinfo=UTC))
        lease = store.create_lease("u", Decimal("4"), "usage", floor=Decimal(0))
        store.settle_lease("u", lease.lease_id, Decimal("4"))
        assert store.check_allowance("u").allowance_remaining == Decimal("1")
        clock["now"] = datetime(2024, 2, 15, tzinfo=UTC)
        assert store.check_allowance("u").allowance_remaining == Decimal("5")

    # ── Plan switch mid-window re-anchors ────────────────────────────────

    def test_switching_plan_mid_window_updates_plan_assigned_at(self) -> None:
        clock_box = {"now": datetime(2024, 1, 15, tzinfo=UTC)}
        store = MemoryStore(clock=lambda: clock_box["now"])
        store.set_active_pricing(
            {
                "version": 1,
                "metering": {"models": {"*": "1"}},
                "plans": {
                    "basic": {
                        "label": "Basic",
                        "allowance": {"amount": 5, "period": "anniversary"},
                    },
                    "pro": {
                        "label": "Pro",
                        "allowance": {"amount": 50, "period": "anniversary"},
                    },
                },
                "ledger": {"min_balance": 0},
            }
        )
        store.set_user_plan("u", "basic")
        first_assigned_at = store.get_user_plan("u").plan_assigned_at
        assert first_assigned_at == datetime(2024, 1, 15, tzinfo=UTC)

        # Re-assign (even to a different plan) later re-anchors plan_assigned_at.
        clock_box["now"] = datetime(2024, 3, 20, tzinfo=UTC)
        store.set_user_plan("u", "pro")
        second_assigned_at = store.get_user_plan("u").plan_assigned_at
        assert second_assigned_at == datetime(2024, 3, 20, tzinfo=UTC)
        assert second_assigned_at != first_assigned_at

        # Future windows anchor off the NEW assignment time (day-of-month 20).
        allowance = store.check_allowance("u")
        assert allowance.plan_id == "pro"
        assert allowance.allowance_remaining == Decimal("50")

    def test_get_user_plan_reports_allowance_period(self) -> None:
        store = MemoryStore()
        store.set_active_pricing(
            {
                "version": 1,
                "metering": {"models": {"*": "1"}},
                "plans": {"pro": {"label": "Pro", "allowance": {"period": "rolling_30d"}}},
            }
        )
        store.set_user_plan("u", "pro")
        result = store.get_user_plan("u")
        assert result.allowance_period == "rolling_30d"
        assert result.plan_assigned_at is not None

    def test_get_user_plan_defaults_to_calendar_month(self) -> None:
        store = MemoryStore()
        store.set_active_pricing(
            {
                "version": 1,
                "metering": {"models": {"*": "1"}},
                "plans": {"pro": {"label": "Pro"}},
            }
        )
        store.set_user_plan("u", "pro")
        result = store.get_user_plan("u")
        assert result.allowance_period == "calendar_month"

    def test_no_plan_reports_calendar_month_and_no_assigned_at(self) -> None:
        store = MemoryStore()
        result = store.get_user_plan("nobody")
        assert result.allowance_period == "calendar_month"
        assert result.plan_assigned_at is None
