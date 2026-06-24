"""In-memory credit store for testing and development."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel

from ducto.interface.base import CreditStore
from ducto.interface.models import (
    AddCreditsResult,
    AddTeamMemberResult,
    AggregateStatsRow,
    AllowanceResult,
    BalanceResult,
    CapCheckResult,
    CreateTeamResult,
    CreditMetadata,
    DailySpendRow,
    DeductionResult,
    GetUserPlanResult,
    PlanDefinition,
    PricingConfigData,
    PricingConfigResult,
    RefundResult,
    ReserveResult,
    SetupResult,
    SetUserPlanResult,
    SpendByModelRow,
    SpendByUserRow,
    SpendCap,
    SweepResult,
    TeamBalanceResult,
    TeamDeductionResult,
    TeamMember,
    TopUserRow,
)


class _TransactionRecord(BaseModel):
    """Internal transaction record for MemoryStore."""

    id: str
    user_id: str
    amount: int
    type: str
    metadata: dict[str, Any] = {}
    reference_type: str | None = None
    reference_id: str | None = None
    expires_at: str | None = None
    created_at: str = ""


class _ReservationRecord(BaseModel):
    """Internal reservation record for MemoryStore."""

    id: str
    user_id: str
    amount: int
    operation_type: str
    metadata: dict[str, Any] = {}


class _UsageWindowRecord(BaseModel):
    """Internal usage window record for MemoryStore."""

    user_id: str
    plan_id: str
    billing_period: str
    usage: int


class _TeamRecord(BaseModel):
    """Internal team record for MemoryStore."""

    id: str
    name: str
    balance: int
    member_count: int
    created_at: str


class _TeamMemberRecord(BaseModel):
    """Internal team member record for MemoryStore."""

    user_id: str
    role: str
    spend_cap: int | None = None
    total_spent: int = 0


class MemoryStore(CreditStore):
    """Credit store backed by in-memory dicts. Zero dependencies.

    Useful for unit testing and local development without a database.
    All data is lost when the process exits.
    """

    def __init__(self) -> None:
        self._balances: dict[str, int] = {}
        self._lifetime: dict[str, int] = {}
        self._transactions: list[_TransactionRecord] = []
        self._reservations: dict[str, _ReservationRecord] = {}
        self._pricing_config: PricingConfigData | None = None
        self._pricing_version: int = 0
        self._pricing_label: str | None = None
        self._plan_definitions: dict[str, PlanDefinition] = {}
        self._user_plan_map: dict[str, str] = {}
        self._usage_windows: list[_UsageWindowRecord] = []
        self._teams: dict[str, _TeamRecord] = {}
        self._team_members: dict[str, dict[str, _TeamMemberRecord]] = {}
        self._spend_caps: list[SpendCap] = []

    # ── Schema management ──────────────────────────────────────────────

    def setup(self, database_url: str | None = None) -> SetupResult:
        return SetupResult(
            tables_created=[
                "001_credit_tables.sql",
                "002_credit_rpcs.sql",
                "003_pricing_config.sql",
                "004_user_plans.sql",
                "005_credit_refunds.sql",
                "006_credit_expiry.sql",
                "007_usage_analytics.sql",
                "008_team_balances.sql",
                "009_spend_caps.sql",
                "010_aggregate_stats.sql",
            ],
        )

    # ── Runtime operations ─────────────────────────────────────────────

    def get_balance(self, user_id: str) -> BalanceResult:
        return BalanceResult(
            user_id=user_id,
            balance=self._balances.get(user_id, 0),
            lifetime_purchased=self._lifetime.get(user_id, 0),
        )

    def add_credits(
        self,
        user_id: str,
        amount: int,
        type: str = "adjustment",
        metadata: CreditMetadata | None = None,
        expires_at: datetime | None = None,
    ) -> AddCreditsResult:
        current = self._balances.get(user_id, 0)
        self._balances[user_id] = current + amount
        self._lifetime[user_id] = self._lifetime.get(user_id, 0) + (amount if type == "purchase" else 0)

        tx_id = str(uuid.uuid4())
        tx = _TransactionRecord(
            id=tx_id,
            user_id=user_id,
            amount=amount,
            type=type,
            metadata=metadata.model_dump() if metadata else {},
            created_at=datetime.now().isoformat(),
        )
        if expires_at:
            tx.expires_at = expires_at.isoformat()
        self._transactions.append(tx)

        return AddCreditsResult(
            transaction_id=tx_id,
            user_id=user_id,
            amount=amount,
            new_balance=self._balances[user_id],
            lifetime_purchased=self._lifetime[user_id],
        )

    def reserve_credits(
        self,
        user_id: str,
        amount: int,
        operation_type: str,
        metadata: CreditMetadata | None = None,
        min_balance: int = 5,
    ) -> ReserveResult:
        balance = self._balances.get(user_id, 0)
        reserved_total = sum(r.amount for r in self._reservations.values() if r.user_id == user_id)
        available = balance - reserved_total

        if available - amount < min_balance:
            return ReserveResult(
                reservation_id="",
                user_id=user_id,
                amount=0,
                error="insufficient_credits",
            )

        rid = str(uuid.uuid4())
        self._reservations[rid] = _ReservationRecord(
            id=rid,
            user_id=user_id,
            amount=amount,
            operation_type=operation_type,
            metadata=metadata.model_dump() if metadata else {},
        )

        return ReserveResult(
            reservation_id=rid,
            user_id=user_id,
            amount=amount,
            balance=balance,
            reserved_total=reserved_total + amount,
        )

    def deduct_credits(
        self,
        user_id: str,
        reservation_id: str,
        amount: int,
        idempotency_key: str | None = None,
        metadata: CreditMetadata | None = None,
    ) -> DeductionResult:
        # Idempotency check
        if idempotency_key:
            for tx in self._transactions:
                if tx.metadata.get("idempotency_key") == idempotency_key:
                    return DeductionResult(
                        transaction_id=tx.id,
                        user_id=user_id,
                        amount=-amount,
                        balance_after=self._balances.get(user_id, 0),
                        idempotent=True,
                    )

        current = self._balances.get(user_id, 0)
        if current < amount:
            return DeductionResult(
                transaction_id="",
                user_id=user_id,
                amount=-amount,
                balance_after=current,
                error="insufficient_credits",
            )

        self._balances[user_id] = current - amount
        if reservation_id in self._reservations:
            del self._reservations[reservation_id]

        tx_id = str(uuid.uuid4())
        tx_meta = metadata.model_dump() if metadata else {}
        if idempotency_key:
            tx_meta["idempotency_key"] = idempotency_key
        self._transactions.append(
            _TransactionRecord(
                id=tx_id,
                user_id=user_id,
                amount=-amount,
                type="usage",
                metadata=tx_meta,
                created_at=datetime.now().isoformat(),
            )
        )

        return DeductionResult(
            transaction_id=tx_id,
            user_id=user_id,
            amount=-amount,
            balance_after=self._balances[user_id],
            idempotent=False,
        )

    # ── Pricing configuration ──────────────────────────────────────────

    def get_active_pricing(self) -> PricingConfigResult | None:
        if self._pricing_config is None:
            return None
        return PricingConfigResult(
            id=str(uuid.uuid4()),
            config=self._pricing_config,
            version=self._pricing_version,
        )

    def set_active_pricing(
        self,
        config: PricingConfigData,
        label: str | None = None,
    ) -> str:
        self._pricing_config = config
        self._pricing_version += 1
        self._pricing_label = label
        # Extract plan definitions from v2 config
        plans = getattr(config, "plans", None)
        if plans:
            for plan in plans.values():
                self._plan_definitions[plan.id] = plan
        return str(uuid.uuid4())

    # ── Plan management ────────────────────────────────────────────────

    def get_user_plan(self, user_id: str) -> GetUserPlanResult:
        plan_id = self._user_plan_map.get(user_id)
        plan_def = self._plan_definitions.get(plan_id) if plan_id else None
        return GetUserPlanResult(
            user_id=user_id,
            plan_id=plan_id,
            plan_name=plan_def.name if plan_def else None,
            free_allowance=plan_def.free_allowance if plan_def else 0,
        )

    def set_user_plan(self, user_id: str, plan_id: str) -> SetUserPlanResult:
        self._user_plan_map[user_id] = plan_id
        return SetUserPlanResult(user_id=user_id, plan_id=plan_id)

    def check_allowance(self, user_id: str) -> AllowanceResult:
        plan_id = self._user_plan_map.get(user_id)
        if not plan_id or plan_id not in self._plan_definitions:
            return AllowanceResult(
                plan_id="",
                allowance_remaining=0,
                period_start="",
                period_end="",
            )
        plan_def = self._plan_definitions[plan_id]
        now = datetime.now()
        period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if now.month == 12:
            period_end = now.replace(year=now.year + 1, month=1, day=1) - timedelta(days=1)
        else:
            period_end = now.replace(month=now.month + 1, day=1) - timedelta(days=1)
        period_end = period_end.replace(hour=0, minute=0, second=0, microsecond=0)
        billing_period = period_start.strftime("%Y-%m-%d")
        usage = sum(
            w.usage
            for w in self._usage_windows
            if w.user_id == user_id and w.plan_id == plan_id and w.billing_period == billing_period
        )
        return AllowanceResult(
            plan_id=plan_id,
            allowance_remaining=max(plan_def.free_allowance - usage, 0),
            period_start=period_start.isoformat(),
            period_end=period_end.isoformat(),
        )

    def increment_usage_window(self, user_id: str, plan_id: str, amount: int) -> None:
        now = datetime.now()
        billing_period = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%d")
        for w in self._usage_windows:
            if w.user_id == user_id and w.plan_id == plan_id and w.billing_period == billing_period:
                w.usage += amount
                return
        self._usage_windows.append(
            _UsageWindowRecord(
                user_id=user_id,
                plan_id=plan_id,
                billing_period=billing_period,
                usage=amount,
            )
        )

    # ── Refunds ─────────────────────────────────────────────────────────

    def refund_credits(
        self,
        transaction_id: str,
        amount: int | None = None,
        reason: str | None = None,
        metadata: CreditMetadata | None = None,
    ) -> RefundResult:
        # Find original transaction
        orig_tx = next((t for t in self._transactions if t.id == transaction_id), None)
        if orig_tx is None:
            return RefundResult(
                refund_transaction_id="",
                original_transaction_id=transaction_id,
                user_id="",
                amount=0,
                new_balance=0,
                error="transaction_not_found",
            )

        # Check for duplicate refund
        is_refunded = any(t.type == "refund" and t.reference_id == transaction_id for t in self._transactions)
        if is_refunded:
            return RefundResult(
                refund_transaction_id="",
                original_transaction_id=transaction_id,
                user_id=orig_tx.user_id,
                amount=0,
                new_balance=self._balances.get(orig_tx.user_id, 0),
                error="already_refunded",
            )

        refund_amount = amount if amount is not None else abs(orig_tx.amount)
        max_refund = abs(orig_tx.amount)
        actual_refund = min(refund_amount, max_refund)

        # Restore balance
        current = self._balances.get(orig_tx.user_id, 0)
        self._balances[orig_tx.user_id] = current + actual_refund

        tx_id = str(uuid.uuid4())
        tx_meta = metadata.model_dump() if metadata else {}
        if reason:
            tx_meta["reason"] = reason
        self._transactions.append(
            _TransactionRecord(
                id=tx_id,
                user_id=orig_tx.user_id,
                amount=actual_refund,
                type="refund",
                reference_type=reason,
                reference_id=transaction_id,
                metadata=tx_meta,
                created_at=datetime.now().isoformat(),
            )
        )

        return RefundResult(
            refund_transaction_id=tx_id,
            original_transaction_id=transaction_id,
            user_id=orig_tx.user_id,
            amount=actual_refund,
            new_balance=self._balances[orig_tx.user_id],
        )

    # ── Credit expiry ─────────────────────────────────────────────────────

    def sweep_expired_credits(self, dry_run: bool = False) -> SweepResult:
        """Sweep expired credits from all users' balances."""
        now = datetime.now()
        expired_by_user: dict[str, int] = {}
        expired_txs: list[_TransactionRecord] = []

        for tx in self._transactions:
            if tx.expires_at and tx.type in ("purchase", "adjustment"):
                try:
                    expires_dt = datetime.fromisoformat(tx.expires_at)
                except ValueError:
                    continue
                if expires_dt <= now:
                    expired_by_user[tx.user_id] = expired_by_user.get(tx.user_id, 0) + tx.amount
                    expired_txs.append(tx)

        expired_count = 0
        expired_amount = 0

        for user_id, total_expired in expired_by_user.items():
            current_balance = self._balances.get(user_id, 0)
            to_expire = min(total_expired, current_balance)

            if to_expire > 0:
                expired_count += 1
                expired_amount += to_expire

                if not dry_run:
                    self._balances[user_id] = current_balance - to_expire

                    # Null out expires_at on swept grants to prevent re-sweeping
                    for et in expired_txs:
                        if et.user_id == user_id:
                            et.expires_at = None

                    tx_id = str(uuid.uuid4())
                    self._transactions.append(
                        _TransactionRecord(
                            id=tx_id,
                            user_id=user_id,
                            amount=-to_expire,
                            type="adjustment",
                            metadata={"reason": "credit_expired", "expired_amount": to_expire},
                            created_at=datetime.now().isoformat(),
                        )
                    )

        return SweepResult(
            expired_count=expired_count,
            expired_amount=expired_amount,
            dry_run=dry_run,
        )

    # ── Usage analytics ─────────────────────────────────────────────────

    def _usage_in_window(self, start: datetime, end: datetime) -> list[_TransactionRecord]:
        """Filter transactions to usage records in the time window."""
        return [
            t
            for t in self._transactions
            if t.type == "usage"
            and t.amount < 0
            and t.created_at
            and start.isoformat() <= t.created_at <= end.isoformat()
        ]

    def spend_by_user(self, start: datetime, end: datetime) -> list[SpendByUserRow]:
        """Aggregate spend by user in a time window."""
        usage = self._usage_in_window(start, end)
        by_user: dict[str, dict[str, int]] = {}
        for t in usage:
            uid = t.user_id
            if uid not in by_user:
                by_user[uid] = {"total": 0, "count": 0}
            by_user[uid]["total"] += abs(t.amount)
            by_user[uid]["count"] += 1
        return [
            SpendByUserRow(user_id=uid, total_spend=v["total"], transaction_count=v["count"])
            for uid, v in sorted(by_user.items())
        ]

    def spend_by_model(self, start: datetime, end: datetime) -> list[SpendByModelRow]:
        """Aggregate spend by model in a time window."""
        usage = self._usage_in_window(start, end)
        by_model: dict[str, dict[str, int]] = {}
        for t in usage:
            model = t.metadata.get("model", "unknown")
            if model not in by_model:
                by_model[model] = {"total": 0, "count": 0}
            by_model[model]["total"] += abs(t.amount)
            by_model[model]["count"] += 1
        return [
            SpendByModelRow(model=model, total_spend=v["total"], transaction_count=v["count"])
            for model, v in sorted(by_model.items())
        ]

    def top_users(self, limit: int, start: datetime, end: datetime) -> list[TopUserRow]:
        """Top users by spend in a time window."""
        rows = self.spend_by_user(start, end)
        rows.sort(key=lambda r: r.total_spend, reverse=True)
        return [TopUserRow(user_id=r.user_id, total_spend=r.total_spend) for r in rows[:limit]]

    def daily_spend(self, start: datetime, end: datetime) -> list[DailySpendRow]:
        """Daily spend aggregation in a time window."""
        usage = self._usage_in_window(start, end)
        by_day: dict[str, dict[str, int]] = {}
        for t in usage:
            date = t.created_at[:10]  # YYYY-MM-DD
            if date not in by_day:
                by_day[date] = {"total": 0, "count": 0}
            by_day[date]["total"] += abs(t.amount)
            by_day[date]["count"] += 1
        return [
            DailySpendRow(date=date, total_spend=v["total"], transaction_count=v["count"])
            for date, v in sorted(by_day.items())
        ]

    # ── Aggregate stats ──────────────────────────────────────────────────

    def aggregate_stats(self, start: datetime, end: datetime) -> AggregateStatsRow:
        """Aggregate statistics across all users in a time window."""
        usage = self._usage_in_window(start, end)
        if not usage:
            return AggregateStatsRow()
        total = sum(abs(t.amount) for t in usage)
        active = len({t.user_id for t in usage})
        days = len({t.created_at[:10] for t in usage})
        avg = total // max(days, 1)
        by_model: dict[str, int] = {}
        by_user: dict[str, int] = {}
        for t in usage:
            model = t.metadata.get("model", "unknown")
            by_model[model] = by_model.get(model, 0) + abs(t.amount)
            by_user[t.user_id] = by_user.get(t.user_id, 0) + abs(t.amount)
        top_model = max(by_model, key=lambda k: by_model[k]) if by_model else ""
        top_user = max(by_user, key=lambda k: by_user[k]) if by_user else ""
        return AggregateStatsRow(
            total_credits_consumed=total,
            active_users=active,
            avg_daily_spend=avg,
            top_model=top_model,
            top_user=top_user,
        )

    # ── Spend caps and rate limiting ─────────────────────────────────────

    def set_spend_cap(self, cap: SpendCap) -> None:
        """Configure a spend cap (MemoryStore-only helper for testing)."""
        self._spend_caps.append(cap)

    def _exceeds_cap(self, user_id: str, cap: SpendCap, model: str | None, amount: int | None) -> CapCheckResult | None:
        """Evaluate single cap. Return CapCheckResult if exceeded, None otherwise."""
        if cap.model and cap.model != model:
            return None
        now = datetime.now()
        if cap.cap_type == "daily":
            window_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            window_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        current = 0
        for t in self._transactions:
            if t.user_id != user_id:
                continue
            if t.type not in ("usage", "team_usage") or t.amount >= 0:
                continue
            if cap.model is not None and (t.metadata or {}).get("model") != cap.model:
                continue
            if t.created_at and t.created_at >= window_start.isoformat():
                current += abs(t.amount)
        if current + (amount or 0) > cap.limit:
            return CapCheckResult(
                capped=cap.action == "deny",
                current_spend=current,
                cap_limit=cap.limit,
                action=cap.action,
                model=cap.model,
            )
        return None

    def check_spend_cap(
        self,
        user_id: str,
        model: str | None = None,
        amount: int | None = None,
    ) -> CapCheckResult:
        user_caps = [c for c in self._spend_caps if c.user_id == user_id]
        if not user_caps:
            return CapCheckResult(capped=False, current_spend=0, cap_limit=0, action=None)

        # Check deny caps first — return first deny hit
        deny_caps = [c for c in user_caps if c.action == "deny" and (not c.model or c.model == model)]
        for cap in deny_caps:
            result = self._exceeds_cap(user_id, cap, model, amount)
            if result is not None:
                return result

        # Then warn/notify — return first soft hit
        soft_caps = [c for c in user_caps if c.action != "deny" and (not c.model or c.model == model)]
        for cap in soft_caps:
            result = self._exceeds_cap(user_id, cap, model, amount)
            if result is not None:
                return CapCheckResult(
                    capped=False,
                    current_spend=result.current_spend,
                    cap_limit=result.cap_limit,
                    action=cap.action,
                    model=cap.model,
                )

        return CapCheckResult(capped=False, current_spend=0, cap_limit=0, action=None)

    # ── Team/shared balance pools ─────────────────────────────────────────

    def create_team(self, name: str, initial_balance: int = 0) -> CreateTeamResult:
        team_id = str(uuid.uuid4())
        self._teams[team_id] = _TeamRecord(
            id=team_id,
            name=name,
            balance=initial_balance,
            member_count=0,
            created_at=datetime.now().isoformat(),
        )
        self._team_members[team_id] = {}
        return CreateTeamResult(team_id=team_id, name=name)

    def get_team_balance(self, team_id: str) -> TeamBalanceResult:
        team = self._teams.get(team_id)
        if team is None:
            return TeamBalanceResult(team_id=team_id)
        return TeamBalanceResult(
            team_id=team.id,
            name=team.name,
            balance=team.balance,
            member_count=team.member_count,
        )

    def add_team_member(
        self,
        team_id: str,
        user_id: str,
        role: str = "member",
        spend_cap: int | None = None,
    ) -> AddTeamMemberResult:
        members = self._team_members.get(team_id)
        if members is None:
            return AddTeamMemberResult(team_id=team_id, user_id=user_id, role="")
        members[user_id] = _TeamMemberRecord(
            user_id=user_id,
            role=role,
            spend_cap=spend_cap,
            total_spent=0,
        )
        team = self._teams.get(team_id)
        if team is not None:
            team.member_count = len(members)
        return AddTeamMemberResult(team_id=team_id, user_id=user_id, role=role)

    def get_team_members(self, team_id: str) -> list[TeamMember]:
        members = self._team_members.get(team_id)
        if not members:
            return []
        return [
            TeamMember(
                user_id=m.user_id,
                role=m.role,
                spend_cap=m.spend_cap,
                total_spent=m.total_spent,
            )
            for m in members.values()
        ]

    def deduct_team(
        self,
        team_id: str,
        user_id: str,
        amount: int,
        metadata: CreditMetadata | None = None,
    ) -> TeamDeductionResult:
        team = self._teams.get(team_id)
        if team is None:
            return TeamDeductionResult(
                transaction_id="",
                team_id=team_id,
                user_id=user_id,
                amount=0,
                team_balance_after=0,
                error="team_not_found",
            )

        members = self._team_members.get(team_id)
        member = members.get(user_id) if members else None
        if member is None:
            return TeamDeductionResult(
                transaction_id="",
                team_id=team_id,
                user_id=user_id,
                amount=0,
                team_balance_after=team.balance,
                error="user_not_in_team",
            )

        # Enforce spend cap
        if member.spend_cap is not None and (member.total_spent + amount) > member.spend_cap:
            return TeamDeductionResult(
                transaction_id="",
                team_id=team_id,
                user_id=user_id,
                amount=0,
                team_balance_after=team.balance,
                error="spend_cap_exceeded",
            )

        if team.balance < amount:
            return TeamDeductionResult(
                transaction_id="",
                team_id=team_id,
                user_id=user_id,
                amount=0,
                team_balance_after=team.balance,
                error="insufficient_team_balance",
            )

        team.balance -= amount
        member.total_spent += amount

        tx_id = str(uuid.uuid4())
        tx_meta = {
            "team_id": team_id,
        }
        if metadata:
            tx_meta.update(metadata.model_dump(exclude_none=True))
        self._transactions.append(
            _TransactionRecord(
                id=tx_id,
                user_id=user_id,
                amount=-amount,
                type="team_usage",
                metadata=tx_meta,
                created_at=datetime.now().isoformat(),
            )
        )

        return TeamDeductionResult(
            transaction_id=tx_id,
            team_id=team_id,
            user_id=user_id,
            amount=-amount,
            team_balance_after=team.balance,
        )
