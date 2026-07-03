"""In-memory credit store for testing and development."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from pydantic import BaseModel

from bursar.allowance import resolve_allowance_window, resolve_calendar_window
from bursar.interface.base import CreditStore, StoreError
from bursar.interface.models import (
    AddCreditsResult,
    AddTeamMemberResult,
    AggregateStatsRow,
    AllowanceResult,
    AvailableResult,
    BalanceResult,
    CapCheckResult,
    CreateTeamResult,
    CreditMetadata,
    DailySpendRow,
    DeductionResult,
    FeatureLimit,
    FeatureLimitResult,
    GetUserPlanResult,
    LeaseResult,
    PlanDefinition,
    PricingConfigData,
    PricingConfigHistoryItem,
    PricingConfigResult,
    RefundResult,
    ReleaseResult,
    SetupResult,
    SetUserPlanResult,
    SpendByModelRow,
    SpendByUserRow,
    SpendCap,
    SweepResult,
    TeamBalanceResult,
    TeamDeductionResult,
    TeamMember,
    TierBalance,
    TierBalancesResult,
    TierDefinition,
    TopUserRow,
    TransactionRow,
)
from bursar.sql import _get_sql_files


def _utcnow() -> datetime:
    """Return a timezone-aware UTC datetime (contract §5, M9)."""
    return datetime.now(UTC)


def _as_decimal(value: Any) -> Decimal:
    """Coerce an incoming money value to ``Decimal`` without binary-float error.

    Accepts ``Decimal``/``int``/``str``; ``float`` is routed through ``str`` so a
    caller that still passes a float does not poison the ledger with IEEE-754
    noise (contract §1).
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    return Decimal(value)


class _TransactionRecord(BaseModel):
    """Internal transaction record for MemoryStore."""

    id: str
    user_id: str
    amount: Decimal
    type: str
    metadata: dict[str, Any] = {}
    reference_type: str | None = None
    reference_id: str | None = None
    expires_at: datetime | None = None
    swept_at: datetime | None = None
    created_at: datetime | None = None


class _ReservationRecord(BaseModel):
    """Internal reservation/lease record for MemoryStore.

    The lease lifecycle (``create_lease``/``settle_lease``/``release_lease``/
    ``renew_lease``) drives ``status`` through ``active → settled | released |
    expired`` and records the resolved ``billing_mode``/``overdraft_floor``
    plus the settling transaction id.
    """

    id: str
    user_id: str
    amount: Decimal
    operation_type: str
    metadata: dict[str, Any] = {}
    expires_at: datetime
    status: str = "active"
    billing_mode: str = "strict"
    overdraft_floor: Decimal | None = None
    settle_tx_id: str | None = None


class _UsageWindowRecord(BaseModel):
    """Internal usage window record for MemoryStore."""

    user_id: str
    plan_id: str
    billing_period: str
    usage: Decimal


class _TeamRecord(BaseModel):
    """Internal team record for MemoryStore."""

    id: str
    name: str
    balance: Decimal
    member_count: int
    created_at: datetime


class _TeamMemberRecord(BaseModel):
    """Internal team member record for MemoryStore."""

    user_id: str
    role: str
    spend_cap: Decimal | None = None
    total_spent: Decimal = Decimal(0)
    joined_at: datetime | None = None


class MemoryStore(CreditStore):
    """Credit store backed by in-memory dicts. Zero dependencies.

    Useful for unit testing and local development without a database.
    All data is lost when the process exits.

    Thread-safety (contract §3, C2): every mutating/reading method takes a single
    re-entrant lock so each emulated "transaction" — most importantly the
    read-modify-write inside :meth:`deduct_with_allowance` — is atomic and cannot
    double-spend under concurrent callers. The lock is re-entrant so helpers can
    be called while held.

    Args:
        clock: Optional injectable clock (a zero-arg callable returning a
            UTC-aware ``datetime``), used everywhere the store would otherwise
            call ``datetime.now(UTC)``. Defaults to the real clock; tests pass
            a fake clock to fast-forward time (e.g. to simulate an allowance
            billing-period rollover) without any wall-clock sleep (WS9f).
    """

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._clock: Callable[[], datetime] = clock if clock is not None else _utcnow
        self._lock = threading.RLock()
        self._balances: dict[str, Decimal] = {}
        self._lifetime: dict[str, Decimal] = {}
        self._transactions: list[_TransactionRecord] = []
        self._reservations: dict[str, _ReservationRecord] = {}
        self._pricing_config: PricingConfigData | None = None
        self._pricing_version: int = 0
        self._pricing_label: str | None = None
        self._pricing_history: list[dict[str, Any]] = []
        # Keyed on the *plan_key* (the dict key in config.plans), matching SQL
        # (L6) so set_user_plan(user, "pro") resolves identically across backends.
        self._plan_definitions: dict[str, PlanDefinition] = {}
        self._user_plan_map: dict[str, str] = {}
        # When each user was (most recently) assigned their current plan —
        # the anchor for rolling_30d/anniversary allowance windows (WS9).
        self._user_plan_assigned_at: dict[str, datetime] = {}
        # Credit tiers (aggregate per-tier balance model): tier definitions loaded
        # from config (parallel to _plan_definitions), and each user's balance
        # per tier. ``self._balances[user_id]`` remains a dual-written aggregate
        # kept equal to SUM of that user's tier balances.
        self._tier_definitions: dict[str, TierDefinition] = {}
        self._tier_balances: dict[tuple[str, str], Decimal] = {}
        self._usage_windows: list[_UsageWindowRecord] = []
        self._teams: dict[str, _TeamRecord] = {}
        self._team_members: dict[str, dict[str, _TeamMemberRecord]] = {}
        self._spend_caps: list[SpendCap] = []

    def _utcnow(self) -> datetime:
        """Return the store's current time (injectable clock, WS9f)."""
        return self._clock()

    # ── Schema management ──────────────────────────────────────────────

    def setup(self, database_url: str | None = None) -> SetupResult:
        # Derive the reported file list from the SQL glob, not a hardcoded list
        # (L5), so it never drifts from the actual bundled migrations.
        return SetupResult(
            tables_created=[f.name for f in _get_sql_files()],
        )

    # ── Runtime operations ─────────────────────────────────────────────

    def get_balance(self, user_id: str) -> BalanceResult:
        with self._lock:
            return BalanceResult(
                user_id=user_id,
                balance=self._balances.get(user_id, Decimal(0)),
                lifetime_purchased=self._lifetime.get(user_id, Decimal(0)),
            )

    def add_credits(
        self,
        user_id: str,
        amount: Decimal,
        type: str = "adjustment",
        metadata: CreditMetadata | None = None,
        expires_at: datetime | None = None,
        tier: str | None = None,
        idempotency_key: str | None = None,
    ) -> AddCreditsResult:
        amount = _as_decimal(amount)

        # Validate (contract §3, M11/L2): purchases (and other credit grants) must
        # be a finite, strictly-positive amount. Negative/zero only via adjustment.
        if not amount.is_finite():
            raise StoreError(f"invalid_amount: {amount}")
        if type != "adjustment" and amount <= 0:
            raise StoreError(f"invalid_amount: {amount} for type {type}")

        with self._lock:
            # Idempotency replay (user-scoped): a retried grant (e.g. a
            # redelivered webhook) with the same key returns the ORIGINAL
            # transaction's result rather than granting a second time — no
            # double-mutation, no second ledger row. Mirrors deduct_team's
            # replay idiom: the CURRENT balance/lifetime is reported (not a
            # frozen point-in-time snapshot like deduct_with_allowance's Fix
            # 8), since a plain credit grant has no floor/cap check tied to
            # the original call the way a debit does.
            if idempotency_key is not None:
                for tx in self._transactions:
                    if tx.user_id == user_id and tx.metadata.get("idempotency_key") == idempotency_key:
                        return AddCreditsResult(
                            transaction_id=tx.id,
                            user_id=user_id,
                            amount=tx.amount,
                            new_balance=self._balances.get(user_id, Decimal(0)),
                            lifetime_purchased=self._lifetime.get(user_id, Decimal(0)),
                            tier=tx.metadata.get("tier", "default"),
                        )

            resolved_tier = self._resolve_add_credits_tier(tier)
            resolved_expires_at = self._reconcile_add_credits_expiry(resolved_tier, expires_at)

            current = self._balances.get(user_id, Decimal(0))
            self._balances[user_id] = current + amount
            self._lifetime[user_id] = self._lifetime.get(user_id, Decimal(0)) + (
                amount if type == "purchase" else Decimal(0)
            )
            tier_balance_key = (user_id, resolved_tier)
            self._tier_balances[tier_balance_key] = self._tier_balances.get(tier_balance_key, Decimal(0)) + amount

            tx_id = str(uuid.uuid4())
            tx_meta: dict[str, Any] = metadata.model_dump() if metadata else {}
            tx_meta["tier"] = resolved_tier
            if idempotency_key is not None:
                tx_meta["idempotency_key"] = idempotency_key
            tx = _TransactionRecord(
                id=tx_id,
                user_id=user_id,
                amount=amount,
                type=type,
                metadata=tx_meta,
                created_at=self._utcnow(),
                # Store tz-aware (naive bounds assumed UTC) so sweep compares
                # datetimes safely without a tz-aware/naive TypeError (M9).
                expires_at=resolved_expires_at,
            )
            self._transactions.append(tx)

            return AddCreditsResult(
                transaction_id=tx_id,
                user_id=user_id,
                amount=amount,
                new_balance=self._balances[user_id],
                lifetime_purchased=self._lifetime[user_id],
                tier=resolved_tier,
            )

    def _resolve_add_credits_tier(self, tier: str | None) -> str:
        """Resolve ``add_credits(tier=...)`` against configured tiers (lock held).

        - No tiers configured: ``tier`` must be ``None`` or ``"default"``.
        - Tiers configured + given: must be a known key, else ``StoreError``
          with error code ``tier_not_found``.
        - Tiers configured + omitted: resolves to the tier with
          ``is_default=True``; if none is marked default, raises
          ``StoreError`` with error code ``tier_required`` (deliberately
          strict — never silently misroute real money).
        """
        if not self._tier_definitions:
            if tier is not None and tier != "default":
                raise StoreError(f"tier_not_found: {tier}")
            return "default"
        if tier is not None:
            if tier not in self._tier_definitions:
                raise StoreError(f"tier_not_found: {tier}")
            return tier
        for tier_key, tdef in self._tier_definitions.items():
            if tdef.is_default:
                return tier_key
        raise StoreError("tier_required")

    def _reconcile_add_credits_expiry(self, resolved_tier: str, expires_at: datetime | None) -> datetime | None:
        """Reconcile ``add_credits(expires_at=...)`` against the resolved tier's
        ``expires`` flag (lock held).

        - No tiers configured (implicit ``"default"`` tier): behaves exactly as
          ``add_credits`` always has — no restriction on ``expires_at``.
        - Non-expiring tier + explicit ``expires_at``: raises ``StoreError``
          with error code ``tier_does_not_expire``.
        - Expiring tier + explicit ``expires_at``: validated to be in the
          future and used as-is.
        - Expiring tier + omitted ``expires_at``: uses ``default_ttl_days``
          when configured, else raises ``StoreError`` with error code
          ``expires_at_required``.
        """
        tier_def = self._tier_definitions.get(resolved_tier)
        if tier_def is None:
            # No tiers configured — unchanged legacy behavior (M-tiers zero
            # behavioral change requirement).
            return self._ensure_aware(expires_at) if expires_at else None
        if not tier_def.expires:
            if expires_at is not None:
                raise StoreError(f"tier_does_not_expire: {resolved_tier}")
            return None
        if expires_at is not None:
            aware = self._ensure_aware(expires_at)
            if aware <= self._utcnow():
                raise StoreError(f"invalid_expires_at: {expires_at}")
            return aware
        if tier_def.default_ttl_days is not None:
            return self._utcnow() + timedelta(days=tier_def.default_ttl_days)
        raise StoreError(f"expires_at_required: {resolved_tier}")

    def _overdraft_sink(self) -> str:
        """Resolve the tier that absorbs overdraft debt (lock held).

        ``allow_overdraft`` tier, else the tier with the highest ``priority``
        number, else ``"default"`` (always resolvable per plan).
        """
        if not self._tier_definitions:
            return "default"
        for tier_key, tdef in self._tier_definitions.items():
            if tdef.allow_overdraft:
                return tier_key
        return max(self._tier_definitions.items(), key=lambda kv: (kv[1].priority, kv[0]))[0]

    def _walk_tiers(self, user_id: str, net: Decimal) -> dict[str, Decimal]:
        """Priority-walk debit of ``net`` across a user's tier balances (lock held).

        Mirrors the deduction algorithm's tier-walk step used by both
        ``deduct_with_allowance`` and ``settle_lease``: drains configured tiers
        in ascending priority order (or the synthetic ``[{"default",
        priority=0, allow_overdraft=True}]`` when no tiers are configured),
        then any tier keys the user holds a nonzero balance in that are no
        longer configured (the "config drift" safety net, appended last so
        money under a removed/renamed tier key never gets stuck).

        If ``net`` still isn't fully covered after exhausting every tier (only
        reachable under a negative floor / overdraft — the floor check that
        ran before this helper guarantees full coverage in strict mode), the
        remainder routes to the overdraft sink and that tier's balance is
        allowed to go negative.

        Uses plain Decimal ``min()`` arithmetic — an exact greedy split, never
        proportional/rounded — so the returned dict's values always sum to
        exactly ``net``.
        """
        if self._tier_definitions:
            walk_order = [
                tier_key
                for tier_key, _ in sorted(self._tier_definitions.items(), key=lambda kv: (kv[1].priority, kv[0]))
            ]
        else:
            walk_order = ["default"]

        configured_keys = set(walk_order)
        drift_keys = sorted(
            tier_key
            for (uid, tier_key), bal in self._tier_balances.items()
            if uid == user_id and tier_key not in configured_keys and bal != 0
        )
        walk_order.extend(drift_keys)

        remaining = net
        tier_breakdown: dict[str, Decimal] = {}
        for tier_key in walk_order:
            if remaining <= 0:
                break
            balance_key = (user_id, tier_key)
            available = self._tier_balances.get(balance_key, Decimal(0))
            take = min(available, remaining)
            if take > 0:
                tier_breakdown[tier_key] = take
                remaining -= take
                self._tier_balances[balance_key] = available - take

        if remaining > 0:
            sink = self._overdraft_sink()
            sink_key = (user_id, sink)
            self._tier_balances[sink_key] = self._tier_balances.get(sink_key, Decimal(0)) - remaining
            tier_breakdown[sink] = tier_breakdown.get(sink, Decimal(0)) + remaining

        return tier_breakdown

    def deduct_with_allowance(
        self,
        user_id: str,
        amount: Decimal,
        *,
        idempotency_key: str | None = None,
        min_balance: Decimal = Decimal(0),
        model: str | None = None,
        metadata: CreditMetadata | None = None,
        skip_allowance: bool = False,
        period_start: date | None = None,
        feature: str | None = None,
        feature_limit: FeatureLimit | None = None,
        feature_period_start: date | None = None,
    ) -> DeductionResult:
        """Atomic calculate-then-charge under the store lock (contract §2).

        Mirrors ``deduct_with_allowance`` in ``009_deduct_and_leases.sql``: the entire
        pipeline (idempotency replay → allowance consume → cap deny on net →
        feature-limit deny → balance floor → debit → ledger insert) runs while
        holding ``self._lock`` so it is all-or-nothing and cannot double-spend
        under threads.

        ``period_start`` (WS9) keys allowance consumption to a specific
        ``rolling_30d``/``anniversary`` window; ``None`` resolves the window
        from the user's plan (falling back to the current UTC calendar month).

        ``feature``/``feature_limit``/``feature_period_start`` enforce a
        per-feature invocation-count limit (see the ``check_feature_limit``
        docstring for the ledger-derived counting model); ``feature_limit=None``
        skips enforcement entirely (the transaction is still tagged with
        ``feature`` when given).
        """
        amount = _as_decimal(amount)
        min_balance = _as_decimal(min_balance)

        if not amount.is_finite() or amount < 0:
            return DeductionResult(
                transaction_id="",
                user_id=user_id,
                amount=Decimal(0),
                balance_after=self._balances.get(user_id, Decimal(0)),
                error="invalid_amount",
            )

        with self._lock:
            balance = self._balances.get(user_id, Decimal(0))

            # (2) Idempotency replay (user-scoped). Use _replay_deduction so that
            # balance_after is read from the original tx metadata (Fix 8), not
            # from the current (potentially diverged) live balance.
            if idempotency_key is not None:
                for tx in self._transactions:
                    if tx.user_id == user_id and tx.metadata.get("idempotency_key") == idempotency_key:
                        return self._replay_deduction(tx, user_id, balance)

            # (3) Allowance: consume as much as the plan's remaining free allowance
            # covers. Net = gross − consumed. Skipped for fixed-cost batch jobs so
            # they don't eat the user's inference allowance (Fix 7 / skip_allowance).
            plan_key = self._user_plan_map.get(user_id)
            consume = Decimal(0)
            if not skip_allowance and plan_key and plan_key in self._plan_definitions:
                remaining = self._allowance_remaining(user_id, plan_key, period_start)
                consume = min(remaining, amount)
            net = amount - consume

            # (4) Spend cap on the NET amount: a deny cap aborts (no allowance
            # consumed); warn/notify just record the strongest signal.
            cap_denied, cap_warning = self._spend_cap_check(user_id, model, net)
            if cap_denied:
                return DeductionResult(
                    transaction_id="",
                    user_id=user_id,
                    amount=Decimal(0),
                    balance_after=balance,
                    error="cap_reached",
                )

            # (4b) Feature limit on the ledger-derived count: a deny breach
            # aborts (no allowance consumed, nothing committed); warn/notify
            # record a signal and continue.
            feature_would_deny, feature_limit_warning = self._feature_limit_check(
                user_id, feature, feature_limit, feature_period_start
            )
            if feature_would_deny:
                return DeductionResult(
                    transaction_id="",
                    user_id=user_id,
                    amount=Decimal(0),
                    balance_after=balance,
                    error="feature_limit_reached",
                )

            # (5) Balance floor on the NET amount.
            if balance - net < min_balance:
                return DeductionResult(
                    transaction_id="",
                    user_id=user_id,
                    amount=Decimal(0),
                    balance_after=balance,
                    error="insufficient_credits",
                )

            # (6) Commit: consume allowance, walk tiers, debit balance, insert one
            # ledger row. The aggregate balance stays a maintained cache, unchanged
            # (still one scalar decrement); the tier walk is the new per-tier commit.
            if consume > 0 and plan_key:
                self._increment_usage_window(user_id, plan_key, consume, period_start)

            tier_breakdown = self._walk_tiers(user_id, net)
            self._balances[user_id] = balance - net
            new_balance = self._balances[user_id]

            tx_id = str(uuid.uuid4())
            tx_meta: dict[str, Any] = metadata.model_dump(exclude_none=True) if metadata else {}
            # System fields last so they win over caller metadata (contract §5).
            if model is not None:
                tx_meta["model"] = model
            # Always tag when `feature` is given, regardless of whether a limit
            # is currently configured, so enabling a limit later still has
            # accurate history within the window.
            if feature is not None:
                tx_meta["feature"] = feature
            if idempotency_key is not None:
                tx_meta["idempotency_key"] = idempotency_key
            # Store balance_after so idempotent replay returns the original value,
            # not the (wrong) current balance at replay time (Fix 8).
            tx_meta["allowance_consumed"] = str(consume)
            tx_meta["balance_after"] = str(new_balance)
            tx_meta["tier_breakdown"] = {k: str(v) for k, v in tier_breakdown.items()}
            self._transactions.append(
                _TransactionRecord(
                    id=tx_id,
                    user_id=user_id,
                    amount=-net,
                    type="usage",
                    metadata=tx_meta,
                    created_at=self._utcnow(),
                )
            )

            return DeductionResult(
                transaction_id=tx_id,
                user_id=user_id,
                amount=net,
                allowance_consumed=consume,
                balance_after=new_balance,
                idempotent=False,
                cap_warning=cap_warning,
                feature_limit_warning=feature_limit_warning,
                tier_breakdown=tier_breakdown,
            )

    # ── Lease lifecycle (atomic admission) ─────────────────────────────

    def _active_leases(self, user_id: str, operation_type: str | None = None) -> list[_ReservationRecord]:
        """Active, unexpired holds for a user (assumes the lock is held)."""
        now = self._utcnow()
        return [
            r
            for r in self._reservations.values()
            if r.user_id == user_id
            and r.status == "active"
            and r.expires_at > now
            and (operation_type is None or r.operation_type == operation_type)
        ]

    def create_lease(
        self,
        user_id: str,
        amount: Decimal,
        operation_type: str,
        *,
        billing_mode: str = "strict",
        floor: Decimal = Decimal(0),
        max_concurrent: int | None = None,
        ttl_seconds: int = 600,
        model: str | None = None,
        overdraft_floor: Decimal | None = None,
        metadata: CreditMetadata | None = None,
        period_start: date | None = None,
        feature: str | None = None,
        feature_limit: FeatureLimit | None = None,
        feature_period_start: date | None = None,
    ) -> LeaseResult:
        amount = _as_decimal(amount)
        floor = _as_decimal(floor)

        if not amount.is_finite() or amount <= 0:
            return LeaseResult(lease_id="", user_id=user_id, amount=Decimal(0), error="invalid_amount")

        with self._lock:
            # Ensure a balance row exists (overdraft admits brand-new users at 0).
            balance = self._balances.setdefault(user_id, Decimal(0))

            # (1A) Allowance headroom: remaining free allowance extends the effective
            #      available so free-tier users aren't falsely rejected at admission
            #      for a worst-case hold they can fully cover with allowance (Fix 1).
            allowance_credit = self._allowance_remaining(user_id, self._user_plan_map.get(user_id) or "", period_start)

            # (2) Concurrency: count active leases for this operation type.
            if max_concurrent is not None and len(self._active_leases(user_id, operation_type)) >= max_concurrent:
                return LeaseResult(
                    lease_id="",
                    user_id=user_id,
                    amount=Decimal(0),
                    billing_mode=billing_mode,  # type: ignore[arg-type]
                    error="concurrency_limit",
                )

            # (3) Deny spend cap at admission: a blocked user can't even start.
            for cap in self._user_caps(user_id, model):
                if cap.action != "deny":
                    continue
                spend = self._cap_window_spend(user_id, cap, model)
                if spend + amount > cap.limit:
                    return LeaseResult(
                        lease_id="",
                        user_id=user_id,
                        amount=Decimal(0),
                        billing_mode=billing_mode,  # type: ignore[arg-type]
                        error="cap_reached",
                    )

            # (3b) Deny-only feature limit at admission — mirrors spend-cap
            # admission: only `deny` is checked here, warn/notify are advisory
            # signals with nothing to warn about yet (no charge has happened).
            feature_would_deny, _ = self._feature_limit_check(user_id, feature, feature_limit, feature_period_start)
            if feature_would_deny:
                return LeaseResult(
                    lease_id="",
                    user_id=user_id,
                    amount=Decimal(0),
                    billing_mode=billing_mode,  # type: ignore[arg-type]
                    error="feature_limit_reached",
                )

            # (4) effective_available = balance − Σ active holds + allowance headroom.
            reserved_total = sum((r.amount for r in self._active_leases(user_id)), Decimal(0))
            available = balance - reserved_total + allowance_credit
            if available - amount < floor:
                return LeaseResult(
                    lease_id="",
                    user_id=user_id,
                    amount=Decimal(0),
                    available=available,
                    reserved_total=reserved_total,
                    billing_mode=billing_mode,  # type: ignore[arg-type]
                    error="insufficient_credits",
                )

            # (5) Insert the active lease.
            lid = str(uuid.uuid4())
            expires_at = self._utcnow() + timedelta(seconds=ttl_seconds)
            self._reservations[lid] = _ReservationRecord(
                id=lid,
                user_id=user_id,
                amount=amount,
                operation_type=operation_type,
                metadata=metadata.model_dump(exclude_none=True) if metadata else {},
                expires_at=expires_at,
                status="active",
                billing_mode=billing_mode,
                overdraft_floor=_as_decimal(overdraft_floor) if overdraft_floor is not None else None,
            )

            return LeaseResult(
                lease_id=lid,
                user_id=user_id,
                amount=amount,
                available=available - amount,
                reserved_total=reserved_total + amount,
                billing_mode=billing_mode,  # type: ignore[arg-type]
                expires_at=expires_at.isoformat(),
            )

    def _replay_deduction(self, tx: _TransactionRecord, user_id: str, balance: Decimal) -> DeductionResult:
        """Build an idempotent-replay ``DeductionResult`` from a ledger row (lock held).

        Uses the ``balance_after`` stored in the transaction's metadata rather than
        the current balance so that multiple replays return a stable result (Fix 8).
        Falls back to ``balance`` (current) for transactions written before this fix.

        Likewise echoes back the ``tier_breakdown`` stored in the original ledger
        row's metadata verbatim — never recomputed — so replay is correct even if
        tiers/balances changed since (idempotent replay must return the exact
        original per-tier breakdown).
        """
        original_balance_after = _as_decimal(tx.metadata.get("balance_after", balance))
        return DeductionResult(
            transaction_id=tx.id,
            user_id=user_id,
            amount=abs(tx.amount),
            allowance_consumed=_as_decimal(tx.metadata.get("allowance_consumed", 0)),
            balance_after=original_balance_after,
            idempotent=True,
            tier_breakdown=self._decimal_breakdown(tx.metadata.get("tier_breakdown")) or None,
        )

    @staticmethod
    def _decimal_breakdown(raw: Any) -> dict[str, Decimal]:
        """Parse a ``dict[str, str]`` tier-breakdown metadata blob into ``Decimal`` values.

        Returns ``{}`` for ``None``/malformed input so callers can treat absence
        uniformly (pre-tiers transactions have no ``tier_breakdown`` key at all).
        """
        if not isinstance(raw, dict):
            return {}
        return {k: _as_decimal(v) for k, v in raw.items()}

    def _settle_lease_state(
        self,
        lease: _ReservationRecord | None,
        user_id: str,
        balance: Decimal,
    ) -> DeductionResult | None:
        """Validate a lease for settle. Returns a short-circuit result, or ``None`` to
        proceed (assumes the lock is held).

        - missing / other-user / released → ``lease_not_found``
        - already settled → idempotent replay of the original charge
        - TTL elapsed → mark ``expired`` and return ``lease_expired``
        """
        now = self._utcnow()
        if lease is None or lease.user_id != user_id or lease.status == "released":
            return DeductionResult(
                transaction_id="", user_id=user_id, amount=Decimal(0), balance_after=balance, error="lease_not_found"
            )
        if lease.status == "settled":
            if lease.settle_tx_id:
                tx = next((t for t in self._transactions if t.id == lease.settle_tx_id), None)
                if tx is not None:
                    return self._replay_deduction(tx, user_id, balance)
            return DeductionResult(
                transaction_id="", user_id=user_id, amount=Decimal(0), balance_after=balance, idempotent=True
            )
        if lease.status == "expired" or lease.expires_at <= now:
            lease.status = "expired"
            return DeductionResult(
                transaction_id="", user_id=user_id, amount=Decimal(0), balance_after=balance, error="lease_expired"
            )
        return None

    def settle_lease(
        self,
        user_id: str,
        lease_id: str,
        amount: Decimal,
        *,
        idempotency_key: str | None = None,
        min_balance: Decimal = Decimal(0),
        model: str | None = None,
        metadata: CreditMetadata | None = None,
        skip_allowance: bool = False,
        period_start: date | None = None,
        feature: str | None = None,
        feature_limit: FeatureLimit | None = None,
        feature_period_start: date | None = None,
    ) -> DeductionResult:
        amount = _as_decimal(amount)

        if not amount.is_finite() or amount < 0:
            return DeductionResult(
                transaction_id="",
                user_id=user_id,
                amount=Decimal(0),
                balance_after=self._balances.get(user_id, Decimal(0)),
                error="invalid_amount",
            )

        with self._lock:
            balance = self._balances.get(user_id, Decimal(0))

            # Idempotency replay (user-scoped).
            if idempotency_key is not None:
                for tx in self._transactions:
                    if tx.user_id == user_id and tx.metadata.get("idempotency_key") == idempotency_key:
                        return self._replay_deduction(tx, user_id, balance)

            lease = self._reservations.get(lease_id)
            precheck = self._settle_lease_state(lease, user_id, balance)
            if precheck is not None:
                return precheck
            assert lease is not None  # _settle_lease_state returns early on None

            # Active & unexpired → settle. De-clamped: charge the ACTUAL cost (D5),
            # never clamp to the lease hold.

            # Zero-cost: release the lease without charging (resolves M3).
            if amount == 0:
                lease.status = "settled"
                return DeductionResult(
                    transaction_id="",
                    user_id=user_id,
                    amount=Decimal(0),
                    balance_after=balance,
                    idempotent=False,
                )

            # Allowance consume on the actual cost.  Skipped for fixed-cost jobs
            # so they don't deplete the inference allowance (Fix 7 / #4).
            plan_key = self._user_plan_map.get(user_id)
            consume = Decimal(0)
            if not skip_allowance and plan_key and plan_key in self._plan_definitions:
                consume = min(self._allowance_remaining(user_id, plan_key, period_start), amount)
            net = amount - consume

            # Floor enforcement (C1): clamp net so balance stays ≥ floor.
            # The floor is derived from the lease's persisted billing_mode and
            # overdraft_floor; min_balance is the engine's strict-mode floor
            # threaded through from the manager.
            if lease.billing_mode in ("strict", "strict_prepaid"):
                settle_floor = min_balance
            else:
                settle_floor = lease.overdraft_floor if lease.overdraft_floor is not None else Decimal(0)
            max_debit = max(Decimal(0), balance - settle_floor)
            net = min(net, max_debit)
            # Re-clamp consume so it never exceeds the actual net charge.
            consume = min(consume, amount - net) if net < amount else consume

            # Spend cap is ADVISORY at settle (work is done): record the strongest
            # breaching action, never block (interface plan §7). 'deny' surfaces as
            # a non-blocking signal the manager re-emits as credits.cap_reached.
            cap_warning: str | None = None
            for cap in self._user_caps(user_id, model):
                spend = self._cap_window_spend(user_id, cap, model)
                if spend + net > cap.limit and (
                    cap_warning is None or (cap_warning != "deny" and cap.action == "deny")
                ):
                    cap_warning = cap.action

            # Feature limit is ADVISORY at settle (work is done): a breach —
            # of any action, including "deny" — only sets feature_limit_warning,
            # never blocks.
            _, feature_limit_warning = self._feature_limit_check(user_id, feature, feature_limit, feature_period_start)

            if consume > 0 and plan_key:
                self._increment_usage_window(user_id, plan_key, consume, period_start)

            tier_breakdown = self._walk_tiers(user_id, net)
            self._balances[user_id] = balance - net
            new_balance = self._balances[user_id]

            tx_id = str(uuid.uuid4())
            tx_meta: dict[str, Any] = metadata.model_dump(exclude_none=True) if metadata else {}
            if model is not None:
                tx_meta["model"] = model
            # Tag the transaction whenever `feature` is given — this is what
            # makes the call countable for future feature-limit checks.
            if feature is not None:
                tx_meta["feature"] = feature
            if idempotency_key is not None:
                tx_meta["idempotency_key"] = idempotency_key
            # Store balance_after so idempotent replay returns the original value,
            # not the (wrong) current balance at replay time (Fix 8).
            tx_meta["allowance_consumed"] = str(consume)
            tx_meta["balance_after"] = str(new_balance)
            tx_meta["tier_breakdown"] = {k: str(v) for k, v in tier_breakdown.items()}
            self._transactions.append(
                _TransactionRecord(
                    id=tx_id,
                    user_id=user_id,
                    amount=-net,
                    type="usage",
                    metadata=tx_meta,
                    created_at=self._utcnow(),
                )
            )

            lease.status = "settled"
            lease.settle_tx_id = tx_id

            return DeductionResult(
                transaction_id=tx_id,
                user_id=user_id,
                amount=net,
                allowance_consumed=consume,
                balance_after=new_balance,
                idempotent=False,
                cap_warning=cap_warning,
                feature_limit_warning=feature_limit_warning,
                tier_breakdown=tier_breakdown,
            )

    def release_lease(self, user_id: str, lease_id: str) -> ReleaseResult:
        with self._lock:
            lease = self._reservations.get(lease_id)
            if lease is None or lease.user_id != user_id:
                return ReleaseResult(lease_id=lease_id, user_id=user_id, released=False, reason="not_found")
            if lease.status == "settled":
                return ReleaseResult(lease_id=lease_id, user_id=user_id, released=False, reason="already_settled")
            if lease.status == "released":
                return ReleaseResult(lease_id=lease_id, user_id=user_id, released=False, reason="already_released")
            lease.status = "released"
            return ReleaseResult(lease_id=lease_id, user_id=user_id, released=True, reason="released")

    def renew_lease(self, user_id: str, lease_id: str, ttl_seconds: int) -> LeaseResult:
        with self._lock:
            now = self._utcnow()
            lease = self._reservations.get(lease_id)
            if lease is None or lease.user_id != user_id or lease.status in ("released", "settled"):
                return LeaseResult(lease_id=lease_id, user_id=user_id, amount=Decimal(0), error="lease_not_found")
            if lease.status == "expired" or lease.expires_at <= now:
                lease.status = "expired"
                return LeaseResult(lease_id=lease_id, user_id=user_id, amount=Decimal(0), error="lease_expired")

            lease.expires_at = now + timedelta(seconds=ttl_seconds)
            reserved_total = sum((r.amount for r in self._active_leases(user_id)), Decimal(0))
            balance = self._balances.get(user_id, Decimal(0))
            # Mirror create_lease: include remaining free allowance in available so the
            # reported headroom is consistent across admission and renewal (#9).
            allowance_credit = self._allowance_remaining(user_id, self._user_plan_map.get(user_id) or "")
            return LeaseResult(
                lease_id=lease_id,
                user_id=user_id,
                amount=lease.amount,
                available=balance - reserved_total + allowance_credit,
                reserved_total=reserved_total,
                billing_mode=lease.billing_mode,  # type: ignore[arg-type]
                expires_at=lease.expires_at.isoformat(),
            )

    def get_available(self, user_id: str) -> AvailableResult:
        with self._lock:
            balance = self._balances.get(user_id, Decimal(0))
            reserved = sum((r.amount for r in self._active_leases(user_id)), Decimal(0))
            return AvailableResult(
                user_id=user_id,
                balance=balance,
                reserved=reserved,
                available=balance - reserved,
            )

    def get_credit_tiers(self, user_id: str) -> TierBalancesResult:
        with self._lock:
            total_balance = self._balances.get(user_id, Decimal(0))

            if not self._tier_definitions:
                # No tiers configured — synthesize a single "default" entry so
                # the API shape is uniform regardless of whether tiers exist.
                return TierBalancesResult(
                    user_id=user_id,
                    tiers=[
                        TierBalance(
                            tier_key="default",
                            name="default",
                            priority=0,
                            expires=False,
                            balance=total_balance,
                        )
                    ],
                    total_balance=total_balance,
                )

            configured = sorted(self._tier_definitions.items(), key=lambda kv: (kv[1].priority, kv[0]))
            tiers = [
                TierBalance(
                    tier_key=tier_key,
                    name=tdef.name,
                    priority=tdef.priority,
                    expires=tdef.expires,
                    balance=self._tier_balances.get((user_id, tier_key), Decimal(0)),
                )
                for tier_key, tdef in configured
            ]
            return TierBalancesResult(user_id=user_id, tiers=tiers, total_balance=total_balance)

    # ── Internal helpers (assume the lock is held) ─────────────────────

    def _purge_expired_reservations(self, user_id: str) -> None:
        now = self._utcnow()
        expired = [rid for rid, r in self._reservations.items() if r.user_id == user_id and r.expires_at <= now]
        for rid in expired:
            del self._reservations[rid]

    def _billing_period(self, period_start: date | None = None) -> str:
        """Key used to bucket allowance usage windows (WS9).

        When ``period_start`` is given (a resolved ``rolling_30d``/
        ``anniversary`` window start), it is formatted directly as the key.
        Otherwise falls back to the current UTC calendar month — the pre-WS9
        behavior, unchanged.
        """
        if period_start is not None:
            return period_start.strftime("%Y-%m-%d")
        return self._utcnow().strftime("%Y-%m-01")

    def _period_start_for_user_plan(self, user_id: str, plan_key: str) -> date | None:
        """Resolve the ``rolling_30d``/``anniversary`` window start for a user's plan.

        Returns ``None`` for ``calendar_month`` (or unknown) plans so callers
        fall back to ``_billing_period()``'s calendar-month default.
        """
        plan_def = self._plan_definitions.get(plan_key)
        if plan_def is None or plan_def.allowance_period == "calendar_month":
            return None
        anchor = self._user_plan_assigned_at.get(user_id)
        period_start, _period_end = resolve_allowance_window(self._utcnow(), plan_def.allowance_period, anchor)
        return period_start

    def _allowance_remaining(self, user_id: str, plan_key: str, period_start: date | None = None) -> Decimal:
        plan_def = self._plan_definitions.get(plan_key)
        if plan_def is None:
            return Decimal(0)
        if period_start is None:
            period_start = self._period_start_for_user_plan(user_id, plan_key)
        period = self._billing_period(period_start)
        usage = sum(
            (
                w.usage
                for w in self._usage_windows
                if w.user_id == user_id and w.plan_id == plan_key and w.billing_period == period
            ),
            Decimal(0),
        )
        return max(plan_def.free_allowance - usage, Decimal(0))

    def _increment_usage_window(
        self, user_id: str, plan_key: str, amount: Decimal, period_start: date | None = None
    ) -> None:
        if period_start is None:
            period_start = self._period_start_for_user_plan(user_id, plan_key)
        period = self._billing_period(period_start)
        for w in self._usage_windows:
            if w.user_id == user_id and w.plan_id == plan_key and w.billing_period == period:
                w.usage += amount
                return
        self._usage_windows.append(
            _UsageWindowRecord(
                user_id=user_id,
                plan_id=plan_key,
                billing_period=period,
                usage=amount,
            )
        )

    def _spend_cap_check(self, user_id: str, model: str | None, net: Decimal) -> tuple[bool, str | None]:
        """Evaluate spend caps against ``net`` (lock held; extracted from
        :meth:`deduct_with_allowance` to keep its cyclomatic complexity down).

        Returns ``(denied, warning)``: ``denied`` is ``True`` on the first
        breached ``deny`` cap (short-circuits); ``warning`` is the strongest
        non-blocking ``warn``/``notify`` signal seen otherwise.
        """
        cap_warning: str | None = None
        for cap in self._user_caps(user_id, model):
            spend = self._cap_window_spend(user_id, cap, model)
            if spend + net > cap.limit:
                if cap.action == "deny":
                    return True, None
                if cap_warning is None:
                    cap_warning = cap.action
        return False, cap_warning

    def _user_caps(self, user_id: str, model: str | None) -> list[SpendCap]:
        """Caps for a user ordered deny-first then by ascending limit (SQL parity)."""
        caps = [c for c in self._spend_caps if c.user_id == user_id and (not c.model or c.model == model)]
        return sorted(caps, key=lambda c: (c.action != "deny", c.limit))

    def _cap_window_spend(self, user_id: str, cap: SpendCap, model: str | None) -> Decimal:
        now = self._utcnow()
        if cap.cap_type == "daily":
            window_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            window_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        spend = Decimal(0)
        for t in self._transactions:
            if t.user_id != user_id:
                continue
            if t.type not in ("usage", "team_usage") or t.amount >= 0:
                continue
            if cap.model is not None and (t.metadata or {}).get("model") != cap.model:
                continue
            if t.created_at is not None and t.created_at >= window_start:
                spend += abs(t.amount)
        return spend

    def _feature_limit_window_end(self, period_start: date, period: str) -> date:
        """Resolve the exclusive window end for a calendar-aligned feature-limit period.

        ``period_start`` is already the aligned window start (resolved by the
        caller via :func:`bursar.allowance.resolve_calendar_window`), so
        re-resolving the window for a ``now`` constructed from that same start
        date returns the exact ``(period_start, period_end)`` pair unchanged —
        this avoids duplicating the daily/weekly/monthly/yearly length logic.
        """
        _, period_end = resolve_calendar_window(datetime.combine(period_start, datetime.min.time(), tzinfo=UTC), period)
        return period_end

    def _feature_limit_check(
        self,
        user_id: str,
        feature: str | None,
        feature_limit: FeatureLimit | None,
        feature_period_start: date | None,
    ) -> tuple[bool, str | None]:
        """Evaluate a feature limit against the ledger-derived count (lock held).

        Returns ``(would_deny, warning_action)``: ``would_deny`` is ``True``
        only when the limit is breached AND ``action == "deny"`` (used by
        ``deduct_with_allowance``/``create_lease`` to abort); ``warning_action``
        is the configured ``action`` whenever the limit is breached, regardless
        of ``action`` (used by ``deduct_with_allowance``/``settle_lease`` for
        the non-blocking ``feature_limit_warning`` signal). ``feature_limit is
        None`` or ``feature_period_start is None`` means enforcement is
        skipped entirely: ``(False, None)``.
        """
        if feature_limit is None or feature_period_start is None:
            return False, None
        feature_period_end = self._feature_limit_window_end(feature_period_start, feature_limit.period)
        count = self._feature_usage_count(user_id, feature or "", feature_period_start, feature_period_end)
        if count >= feature_limit.max_calls:
            return feature_limit.action == "deny", feature_limit.action
        return False, None

    def _feature_usage_count(self, user_id: str, feature: str, period_start: date, period_end: date) -> int:
        """Ledger-derived invocation count for ``feature`` in ``[period_start, period_end)``.

        Mirrors ``_cap_window_spend`` but counts rows instead of summing amounts:
        only committed ``usage`` transactions tagged ``metadata.feature ==
        feature`` count. This is why :meth:`release_lease` never counts (it never
        inserts a ``usage`` row) and :meth:`refund_credits` never frees up quota
        (a refund does not delete the original ``usage`` row).

        Deliberately does NOT filter on ``amount < 0`` (unlike
        ``_cap_window_spend``, which only cares about actual dollars spent): a
        call fully covered by free allowance nets to ``amount == 0`` but is
        still one invocation of the feature and must still count.
        """
        count = 0
        for t in self._transactions:
            if t.user_id != user_id or t.type != "usage":
                continue
            if (t.metadata or {}).get("feature") != feature:
                continue
            if t.created_at is not None and period_start <= t.created_at.date() < period_end:
                count += 1
        return count

    # ── Pricing configuration ──────────────────────────────────────────

    def get_active_pricing(self) -> PricingConfigResult | None:
        with self._lock:
            for h in reversed(self._pricing_history):
                if h["active"]:
                    cfg = h.get("config")
                    if cfg is None:
                        return None
                    return PricingConfigResult(
                        id=h["id"],
                        config=PricingConfigData.model_validate(cfg),
                        version=h["version"],
                        label=h.get("label"),
                    )
            return None

    def set_active_pricing(
        self,
        config: PricingConfigData,
        label: str | None = None,
    ) -> str:
        with self._lock:
            self._pricing_config = config
            self._pricing_version += 1
            self._pricing_label = label
            # Push to history with a snapshot of the config data
            for h in self._pricing_history:
                h["active"] = False
            record_id = str(uuid.uuid4())
            self._pricing_history.append(
                {
                    "id": record_id,
                    "version": self._pricing_version,
                    "label": label,
                    "active": True,
                    "config": config.model_dump(mode="json"),
                    "created_at": self._utcnow().isoformat(),
                }
            )
            # Extract plan definitions from config, keyed on plan_key (L6).
            plans = getattr(config, "plans", None)
            if plans:
                for plan_key, plan in plans.items():
                    self._plan_definitions[plan_key] = plan
            # Extract tier definitions from config, keyed on tier_key (mirrors plans).
            tiers = getattr(config, "tiers", None)
            if tiers:
                for tier_key, tier in tiers.items():
                    self._tier_definitions[tier_key] = tier
            return record_id

    def get_pricing_history(self) -> list[PricingConfigHistoryItem]:
        with self._lock:
            return [PricingConfigHistoryItem.model_validate(h) for h in reversed(self._pricing_history)]

    def get_pricing_config(self, version: int) -> PricingConfigResult | None:
        with self._lock:
            for h in self._pricing_history:
                if h["version"] == version:
                    cfg = h.get("config")
                    if cfg is None and self._pricing_config is not None:
                        cfg = self._pricing_config.model_dump(mode="json")
                    return PricingConfigResult(
                        id=h["id"],
                        config=PricingConfigData.model_validate(cfg),
                        version=version,
                        label=h.get("label"),
                    )
            return None

    def activate_pricing(self, version: int) -> str:
        with self._lock:
            if not any(h["version"] == version for h in self._pricing_history):
                raise StoreError(f"Version {version} not found")
            activated_id = ""
            for h in self._pricing_history:
                h["active"] = False
                if h["version"] == version:
                    h["active"] = True
                    activated_id = h["id"]
                    # Restore the config data from that version
                    cfg_data = h.get("config")
                    if cfg_data:
                        self._pricing_config = PricingConfigData.model_validate(cfg_data)
                        self._pricing_version = version
            return activated_id or str(uuid.uuid4())

    # ── Plan management ────────────────────────────────────────────────

    def get_user_plan(self, user_id: str) -> GetUserPlanResult:
        with self._lock:
            plan_key = self._user_plan_map.get(user_id)
            plan_def = self._plan_definitions.get(plan_key) if plan_key else None
            return GetUserPlanResult(
                user_id=user_id,
                plan_id=plan_key,
                plan_name=plan_def.name if plan_def else None,
                free_allowance=plan_def.free_allowance if plan_def else Decimal(0),
                features=plan_def.features if plan_def and plan_def.features else {},
                feature_limits=plan_def.feature_limits if plan_def and plan_def.feature_limits else {},
                default_billing_mode=plan_def.default_billing_mode if plan_def else "strict",
                per_operation=plan_def.per_operation if plan_def and plan_def.per_operation else {},
                max_concurrent=plan_def.max_concurrent if plan_def else None,
                overdraft_floor=plan_def.overdraft_floor if plan_def else None,
                allowance_period=plan_def.allowance_period if plan_def else "calendar_month",
                plan_assigned_at=self._user_plan_assigned_at.get(user_id) if plan_key else None,
            )

    def set_user_plan(self, user_id: str, plan_id: str) -> SetUserPlanResult:
        # ``plan_id`` is the plan_key (matches SQL set_user_plan(UUID, TEXT); L6).
        # Re-anchors the allowance window (WS9): every (re-)assignment records a
        # fresh plan_assigned_at, using the injectable clock so tests can
        # time-travel (WS9f).
        with self._lock:
            self._user_plan_map[user_id] = plan_id
            self._user_plan_assigned_at[user_id] = self._utcnow()
            return SetUserPlanResult(user_id=user_id, plan_id=plan_id)

    def check_allowance(self, user_id: str, period_start: date | None = None) -> AllowanceResult:
        # period_start is unused here: MemoryStore already has direct access to
        # plan_assigned_at and its own (injectable, test-controllable) clock, so
        # it self-resolves the window rather than trusting a caller-supplied date
        # that may have been computed against the manager's wall clock instead.
        del period_start
        with self._lock:
            plan_key = self._user_plan_map.get(user_id)
            plan_def = self._plan_definitions.get(plan_key) if plan_key else None
            if not plan_key or plan_def is None:
                return AllowanceResult(
                    plan_id="",
                    allowance_remaining=Decimal(0),
                    period_start="",
                    period_end="",
                )
            anchor = self._user_plan_assigned_at.get(user_id)
            period_start, period_end_exclusive = resolve_allowance_window(
                self._utcnow(), plan_def.allowance_period, anchor
            )
            # Display convention (preserved exactly): period_end is the LAST DAY
            # INCLUSIVE, i.e. one day before the resolver's exclusive end.
            period_end_inclusive = period_end_exclusive - timedelta(days=1)
            return AllowanceResult(
                plan_id=plan_key,
                allowance_remaining=self._allowance_remaining(user_id, plan_key, period_start),
                period_start=datetime(period_start.year, period_start.month, period_start.day, tzinfo=UTC).isoformat(),
                period_end=datetime(
                    period_end_inclusive.year,
                    period_end_inclusive.month,
                    period_end_inclusive.day,
                    tzinfo=UTC,
                ).isoformat(),
            )

    def increment_usage_window(self, user_id: str, plan_id: str, amount: Decimal) -> None:
        with self._lock:
            self._increment_usage_window(user_id, plan_id, _as_decimal(amount))

    # ── Refunds ─────────────────────────────────────────────────────────

    def refund_credits(
        self,
        transaction_id: str,
        amount: Decimal | None = None,
        reason: str | None = None,
        metadata: CreditMetadata | None = None,
    ) -> RefundResult:
        with self._lock:
            # Find original transaction
            orig_tx = next((t for t in self._transactions if t.id == transaction_id), None)
            if orig_tx is None:
                return RefundResult(
                    refund_transaction_id="",
                    original_transaction_id=transaction_id,
                    user_id="",
                    amount=Decimal(0),
                    new_balance=Decimal(0),
                    error="not_found",
                )

            current = self._balances.get(orig_tx.user_id, Decimal(0))

            # Only a usage/team_usage debit (negative amount) is refundable. A
            # purchase/refund/adjustment has nothing to give back → over_refund
            # (matches SQL refund semantics in 005).
            if orig_tx.type not in ("usage", "team_usage") or orig_tx.amount >= 0:
                return RefundResult(
                    refund_transaction_id="",
                    original_transaction_id=transaction_id,
                    user_id=orig_tx.user_id,
                    amount=Decimal(0),
                    new_balance=current,
                    error="over_refund",
                )

            original_debit = abs(orig_tx.amount)

            # Back-compat duplicate detection: a prior FULL refund replays as
            # already_refunded (parity with SQL 005 step 3a).
            already_refunded = any(
                t.type == "refund" and t.reference_id == transaction_id and t.amount == original_debit
                for t in self._transactions
            )
            if already_refunded:
                return RefundResult(
                    refund_transaction_id="",
                    original_transaction_id=transaction_id,
                    user_id=orig_tx.user_id,
                    amount=Decimal(0),
                    new_balance=current,
                    error="already_refunded",
                )

            prior_refunded = sum(
                (t.amount for t in self._transactions if t.type == "refund" and t.reference_id == transaction_id),
                Decimal(0),
            )
            remaining = original_debit - prior_refunded
            refund_amount = _as_decimal(amount) if amount is not None else remaining

            # Over-refund rejection: prior + this must not exceed the original debit.
            if refund_amount <= 0 or refund_amount > remaining:
                return RefundResult(
                    refund_transaction_id="",
                    original_transaction_id=transaction_id,
                    user_id=orig_tx.user_id,
                    amount=Decimal(0),
                    new_balance=current,
                    error="over_refund",
                )

            # LIFO tier restoration (usage only — team_usage has no tier concept;
            # tiers apply only to user_credits/user_credit_tiers, not team pools).
            # Re-derives tier_remaining from the ledger each time (sum of ALL prior
            # refunds' own tier_breakdown), never a separate running counter, so
            # repeated partial refunds compose correctly without double-restoring.
            refund_tier_breakdown: dict[str, Decimal] | None = None
            if orig_tx.type == "usage":
                original_breakdown = self._decimal_breakdown(orig_tx.metadata.get("tier_breakdown"))
                if original_breakdown:
                    prior_refund_breakdowns = [
                        self._decimal_breakdown(t.metadata.get("tier_breakdown"))
                        for t in self._transactions
                        if t.type == "refund" and t.reference_id == transaction_id
                    ]
                    tier_remaining: dict[str, Decimal] = {}
                    for tier_key, orig_amt in original_breakdown.items():
                        already = sum((b.get(tier_key, Decimal(0)) for b in prior_refund_breakdowns), Decimal(0))
                        tier_remaining[tier_key] = orig_amt - already

                    to_allocate = refund_amount
                    new_breakdown: dict[str, Decimal] = {}
                    for tier_key in self._reverse_priority_order(list(tier_remaining.keys())):
                        if to_allocate <= 0:
                            break
                        give = min(tier_remaining.get(tier_key, Decimal(0)), to_allocate)
                        if give > 0:
                            new_breakdown[tier_key] = give
                            to_allocate -= give
                            balance_key = (orig_tx.user_id, tier_key)
                            self._tier_balances[balance_key] = self._tier_balances.get(balance_key, Decimal(0)) + give
                    refund_tier_breakdown = new_breakdown

            # Restore balance and append the refund ledger row.
            self._balances[orig_tx.user_id] = current + refund_amount

            tx_id = str(uuid.uuid4())
            tx_meta = metadata.model_dump(exclude_none=True) if metadata else {}
            if reason:
                tx_meta["reason"] = reason
            if refund_tier_breakdown is not None:
                tx_meta["tier_breakdown"] = {k: str(v) for k, v in refund_tier_breakdown.items()}
            self._transactions.append(
                _TransactionRecord(
                    id=tx_id,
                    user_id=orig_tx.user_id,
                    amount=refund_amount,
                    type="refund",
                    reference_type=reason,
                    reference_id=transaction_id,
                    metadata=tx_meta,
                    created_at=self._utcnow(),
                )
            )

            return RefundResult(
                refund_transaction_id=tx_id,
                original_transaction_id=transaction_id,
                user_id=orig_tx.user_id,
                amount=refund_amount,
                new_balance=self._balances[orig_tx.user_id],
                tier_breakdown=refund_tier_breakdown,
            )

    def _reverse_priority_order(self, tier_keys: list[str]) -> list[str]:
        """Order tier keys for LIFO refund restoration (lock held): highest
        ``priority`` number (last-drained) first. Tier keys no longer present in
        config (config drift) sort before all configured tiers — they were
        appended last in the forward deduction walk, so they were drained last
        and are restored first.
        """

        def sort_key(tier_key: str) -> tuple[int, int, str]:
            tdef = self._tier_definitions.get(tier_key)
            if tdef is None:
                return (0, 0, tier_key)
            return (1, -tdef.priority, tier_key)

        return sorted(tier_keys, key=sort_key)

    # ── Credit expiry ─────────────────────────────────────────────────────

    def sweep_expired_credits(self, dry_run: bool = False, user_id: str | None = None) -> SweepResult:
        """Sweep expired credits from all users' balances, or a single user's.

        Swept grants are marked with ``swept_at`` (H4) so a second sweep reports
        zero and never double-debits — parity with the SQL ``expire_credits``.

        Grouping is per ``(user_id, tier_key)`` (not just per-user): each grant's
        tier is read from ``metadata["tier"]`` (defaulting to ``"default"`` for
        pre-existing transactions written before tiers existed) — a tier's
        ``expires`` flag is only consulted at ``add_credits`` time, so a
        transaction's fate is fixed regardless of later config changes. The
        existing clamp (never expire more than what's left) is unchanged, just
        re-scoped to the tier's own balance instead of the user's aggregate.

        ``user_id`` (lazy per-user expiry): when given, the scan is restricted
        to that user's own transactions only — other users' expired grants are
        left completely untouched (they still show up in a later global or
        per-user sweep). ``user_id=None`` (the default) preserves the exact
        prior global-sweep behavior/output shape.
        """
        with self._lock:
            now = self._utcnow()
            expired_by_tier_key: dict[tuple[str, str], Decimal] = {}
            expired_txs: list[_TransactionRecord] = []

            for tx in self._transactions:
                if tx.swept_at is not None:
                    continue
                if user_id is not None and tx.user_id != user_id:
                    continue
                if tx.expires_at and tx.type in ("purchase", "adjustment") and tx.expires_at <= now:
                    tier_key = tx.metadata.get("tier", "default")
                    key = (tx.user_id, tier_key)
                    expired_by_tier_key[key] = expired_by_tier_key.get(key, Decimal(0)) + tx.amount
                    expired_txs.append(tx)

            expired_count = 0
            expired_amount = Decimal(0)
            expired_by_tier: dict[str, Decimal] = {}

            for (grp_user_id, tier_key), total_expired in expired_by_tier_key.items():
                current_tier_balance = self._tier_balances.get((grp_user_id, tier_key), Decimal(0))
                to_expire = min(total_expired, current_tier_balance)

                if to_expire > 0:
                    expired_count += 1
                    expired_amount += to_expire
                    expired_by_tier[tier_key] = expired_by_tier.get(tier_key, Decimal(0)) + to_expire

                    if not dry_run:
                        self._tier_balances[(grp_user_id, tier_key)] = current_tier_balance - to_expire
                        current_balance = self._balances.get(grp_user_id, Decimal(0))
                        self._balances[grp_user_id] = current_balance - to_expire

                        # Mark swept grants so they are not re-swept (H4).
                        for et in expired_txs:
                            if et.user_id == grp_user_id and et.metadata.get("tier", "default") == tier_key:
                                et.swept_at = now

                        tx_id = str(uuid.uuid4())
                        self._transactions.append(
                            _TransactionRecord(
                                id=tx_id,
                                user_id=grp_user_id,
                                amount=-to_expire,
                                type="adjustment",
                                metadata={
                                    "reason": "credit_expired",
                                    "expired_amount": str(to_expire),
                                    "tier": tier_key,
                                },
                                created_at=now,
                            )
                        )

            return SweepResult(
                expired_count=expired_count,
                expired_amount=expired_amount,
                dry_run=dry_run,
                expired_by_tier=expired_by_tier,
            )

    # ── Usage analytics ─────────────────────────────────────────────────

    def _usage_in_window(self, start: datetime, end: datetime) -> list[_TransactionRecord]:
        """Filter transactions to usage records in the time window.

        Compares timezone-aware datetimes (M9), not ISO strings. Naive bounds
        are assumed to be UTC.
        """
        start = self._ensure_aware(start)
        end = self._ensure_aware(end)
        return [
            t
            for t in self._transactions
            if t.type == "usage" and t.amount < 0 and t.created_at is not None and start <= t.created_at <= end
        ]

    @staticmethod
    def _ensure_aware(dt: datetime) -> datetime:
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)

    def spend_by_user(self, start: datetime, end: datetime) -> list[SpendByUserRow]:
        """Aggregate spend by user in a time window."""
        with self._lock:
            usage = self._usage_in_window(start, end)
        by_user: dict[str, dict[str, Any]] = {}
        for t in usage:
            uid = t.user_id
            if uid not in by_user:
                by_user[uid] = {"total": Decimal(0), "count": 0}
            by_user[uid]["total"] += abs(t.amount)
            by_user[uid]["count"] += 1
        return [
            SpendByUserRow(user_id=uid, total_spend=v["total"], transaction_count=v["count"])
            for uid, v in sorted(by_user.items())
        ]

    def spend_by_model(self, start: datetime, end: datetime) -> list[SpendByModelRow]:
        """Aggregate spend by model in a time window."""
        with self._lock:
            usage = self._usage_in_window(start, end)
        by_model: dict[str, dict[str, Any]] = {}
        for t in usage:
            model = t.metadata.get("model", "unknown")
            if model not in by_model:
                by_model[model] = {"total": Decimal(0), "count": 0}
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
        with self._lock:
            usage = self._usage_in_window(start, end)
        by_day: dict[str, dict[str, Any]] = {}
        for t in usage:
            assert t.created_at is not None
            date = t.created_at.strftime("%Y-%m-%d")
            if date not in by_day:
                by_day[date] = {"total": Decimal(0), "count": 0}
            by_day[date]["total"] += abs(t.amount)
            by_day[date]["count"] += 1
        return [
            DailySpendRow(date=date, total_spend=v["total"], transaction_count=v["count"])
            for date, v in sorted(by_day.items())
        ]

    # ── Aggregate stats ──────────────────────────────────────────────────

    def aggregate_stats(self, start: datetime, end: datetime) -> AggregateStatsRow:
        """Aggregate statistics across all users in a time window."""
        with self._lock:
            usage = self._usage_in_window(start, end)
        if not usage:
            return AggregateStatsRow()
        total = sum((abs(t.amount) for t in usage), Decimal(0))
        active = len({t.user_id for t in usage})
        days = len({t.created_at.strftime("%Y-%m-%d") for t in usage if t.created_at is not None})
        # NUMERIC division (not integer division) per contract §1.
        avg = total / Decimal(max(days, 1))
        by_model: dict[str, Decimal] = {}
        by_user: dict[str, Decimal] = {}
        for t in usage:
            model = t.metadata.get("model", "unknown")
            by_model[model] = by_model.get(model, Decimal(0)) + abs(t.amount)
            by_user[t.user_id] = by_user.get(t.user_id, Decimal(0)) + abs(t.amount)
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
        with self._lock:
            self._spend_caps.append(cap)

    def check_spend_cap(
        self,
        user_id: str,
        model: str | None = None,
        amount: Decimal | None = None,
    ) -> CapCheckResult:
        amount_d = _as_decimal(amount) if amount is not None else Decimal(0)
        with self._lock:
            user_caps = [c for c in self._spend_caps if c.user_id == user_id]
            if not user_caps:
                return CapCheckResult(capped=False, current_spend=Decimal(0), cap_limit=Decimal(0), action=None)

            # Check deny caps first — return first deny hit.
            for cap in (c for c in user_caps if c.action == "deny" and (not c.model or c.model == model)):
                spend = self._cap_window_spend(user_id, cap, model)
                if spend + amount_d > cap.limit:
                    return CapCheckResult(
                        capped=True,
                        current_spend=spend,
                        cap_limit=cap.limit,
                        action=cap.action,
                        model=cap.model,
                    )

            # Then warn/notify — return first soft hit.
            for cap in (c for c in user_caps if c.action != "deny" and (not c.model or c.model == model)):
                spend = self._cap_window_spend(user_id, cap, model)
                if spend + amount_d > cap.limit:
                    return CapCheckResult(
                        capped=False,
                        current_spend=spend,
                        cap_limit=cap.limit,
                        action=cap.action,
                        model=cap.model,
                    )

            return CapCheckResult(capped=False, current_spend=Decimal(0), cap_limit=Decimal(0), action=None)

    def check_feature_limit(
        self,
        user_id: str,
        feature: str,
        max_calls: int,
        period_start: date,
        period_end: date,
    ) -> FeatureLimitResult:
        with self._lock:
            count = self._feature_usage_count(user_id, feature, period_start, period_end)
            return FeatureLimitResult(
                user_id=user_id,
                feature=feature,
                limited=True,
                limit=max_calls,
                used=count,
                remaining=max(max_calls - count, 0),
                period_start=str(period_start),
                period_end=str(period_end),
            )

    # ── Transaction listing ─────────────────────────────────────────────────

    def list_user_transactions(
        self,
        user_id: str,
        types: list[str] | None = None,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[TransactionRow]:
        from_aware = self._ensure_aware(from_date) if from_date else None
        to_aware = self._ensure_aware(to_date) if to_date else None
        with self._lock:
            filtered = [
                t
                for t in self._transactions
                if t.user_id == user_id
                and (types is None or t.type in types)
                and (from_aware is None or (t.created_at is not None and t.created_at >= from_aware))
                and (to_aware is None or (t.created_at is not None and t.created_at <= to_aware))
            ]
            filtered.sort(key=lambda t: t.created_at or self._utcnow(), reverse=True)
            total = len(filtered)
            page = filtered[offset : offset + limit]
            return [
                TransactionRow(
                    id=t.id,
                    user_id=t.user_id,
                    amount=t.amount,
                    type=t.type,
                    reference_type=t.reference_type,
                    reference_id=t.reference_id,
                    metadata=t.metadata,
                    created_at=t.created_at.isoformat() if t.created_at else "",
                    total_count=total,
                )
                for t in page
            ]

    # ── Team/shared balance pools ─────────────────────────────────────────

    def create_team(self, name: str, initial_balance: Decimal = Decimal(0)) -> CreateTeamResult:
        with self._lock:
            team_id = str(uuid.uuid4())
            self._teams[team_id] = _TeamRecord(
                id=team_id,
                name=name,
                balance=_as_decimal(initial_balance),
                member_count=0,
                created_at=self._utcnow(),
            )
            self._team_members[team_id] = {}
            return CreateTeamResult(team_id=team_id, name=name)

    def get_team_balance(self, team_id: str) -> TeamBalanceResult:
        with self._lock:
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
        spend_cap: Decimal | None = None,
    ) -> AddTeamMemberResult:
        with self._lock:
            members = self._team_members.get(team_id)
            if members is None:
                return AddTeamMemberResult(team_id=team_id, user_id=user_id, role="")
            members[user_id] = _TeamMemberRecord(
                user_id=user_id,
                role=role,
                spend_cap=_as_decimal(spend_cap) if spend_cap is not None else None,
                total_spent=Decimal(0),
                joined_at=self._utcnow(),
            )
            team = self._teams.get(team_id)
            if team is not None:
                team.member_count = len(members)
            return AddTeamMemberResult(team_id=team_id, user_id=user_id, role=role)

    def get_team_members(self, team_id: str) -> list[TeamMember]:
        """List a team's members.

        ``total_spent`` is the SAME monthly-windowed team_usage spend that
        ``deduct_team`` enforces the per-user cap against (contract §3 / M2):
        a single source of truth, reset monthly, attributed via metadata team_id.
        """
        with self._lock:
            members = self._team_members.get(team_id)
            if not members:
                return []
            return [
                TeamMember(
                    user_id=m.user_id,
                    role=m.role,
                    spend_cap=m.spend_cap,
                    total_spent=self._team_month_spent(team_id, m.user_id),
                )
                for m in members.values()
            ]

    def _team_month_spent(self, team_id: str, user_id: str) -> Decimal:
        window_start = self._utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        spent = Decimal(0)
        for t in self._transactions:
            if (
                t.user_id == user_id
                and t.type == "team_usage"
                and (t.metadata or {}).get("team_id") == team_id
                and t.created_at is not None
                and t.created_at >= window_start
            ):
                spent += abs(t.amount)
        return spent

    def deduct_team(
        self,
        team_id: str,
        user_id: str,
        amount: Decimal,
        metadata: CreditMetadata | None = None,
        idempotency_key: str | None = None,
    ) -> TeamDeductionResult:
        amount = _as_decimal(amount)

        with self._lock:
            team = self._teams.get(team_id)
            if team is None:
                return TeamDeductionResult(
                    transaction_id="",
                    team_id=team_id,
                    user_id=user_id,
                    amount=Decimal(0),
                    team_balance_after=Decimal(0),
                    error="team_not_found",
                )

            # Idempotency replay (user-scoped): return the original team tx (H12).
            if idempotency_key is not None:
                for tx in self._transactions:
                    if (
                        tx.user_id == user_id
                        and tx.type == "team_usage"
                        and tx.metadata.get("idempotency_key") == idempotency_key
                    ):
                        return TeamDeductionResult(
                            transaction_id=tx.id,
                            team_id=team_id,
                            user_id=user_id,
                            amount=tx.amount,
                            team_balance_after=team.balance,
                        )

            members = self._team_members.get(team_id)
            member = members.get(user_id) if members else None
            if member is None:
                return TeamDeductionResult(
                    transaction_id="",
                    team_id=team_id,
                    user_id=user_id,
                    amount=Decimal(0),
                    team_balance_after=team.balance,
                    error="user_not_in_team",
                )

            # Enforce the per-user spend cap against the monthly team-usage window
            # (the same figure get_team_members reports) — not a lifetime counter.
            if member.spend_cap is not None:
                month_spent = self._team_month_spent(team_id, user_id)
                if (month_spent + amount) > member.spend_cap:
                    return TeamDeductionResult(
                        transaction_id="",
                        team_id=team_id,
                        user_id=user_id,
                        amount=Decimal(0),
                        team_balance_after=team.balance,
                        error="spend_cap_exceeded",
                    )

            if team.balance < amount:
                return TeamDeductionResult(
                    transaction_id="",
                    team_id=team_id,
                    user_id=user_id,
                    amount=Decimal(0),
                    team_balance_after=team.balance,
                    error="insufficient_team_balance",
                )

            team.balance -= amount
            member.total_spent += amount

            tx_id = str(uuid.uuid4())
            tx_meta: dict[str, Any] = {"team_id": team_id}
            if metadata:
                tx_meta.update(metadata.model_dump(exclude_none=True))
            if idempotency_key is not None:
                tx_meta["idempotency_key"] = idempotency_key
            self._transactions.append(
                _TransactionRecord(
                    id=tx_id,
                    user_id=user_id,
                    amount=-amount,
                    type="team_usage",
                    metadata=tx_meta,
                    created_at=self._utcnow(),
                )
            )

            return TeamDeductionResult(
                transaction_id=tx_id,
                team_id=team_id,
                user_id=user_id,
                amount=-amount,
                team_balance_after=team.balance,
            )
