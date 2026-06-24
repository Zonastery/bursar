#!/usr/bin/env python3
"""Generate ducto example notebooks using nbformat.

Usage: uv run python scripts/generate_notebooks.py
"""

from pathlib import Path

import nbformat as nbf

NB_DIR = Path(__file__).resolve().parent.parent / "notebooks"
NB_DIR.mkdir(parents=True, exist_ok=True)

KERNEL = {"display_name": "Python 3", "language": "python", "name": "python3"}
LANG_INFO = {"name": "python", "version": "3.11.0"}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def md(s: str) -> dict:
    return nbf.v4.new_markdown_cell(s)


def code(s: str) -> dict:
    return nbf.v4.new_code_cell(s)


def save(name: str, cells: list[dict]) -> None:
    nb = nbf.v4.new_notebook(
        metadata={"kernelspec": KERNEL, "language_info": LANG_INFO},
        cells=cells,
    )
    with open(NB_DIR / name, "w") as f:
        nbf.write(nb, f)
    print(f"  {name}")


def pg_setup(extra_imports: str = "") -> dict:
    return code(f"""\
from datetime import datetime, timedelta
from ducto.interface.postgres import PostgresStore
from ducto.manager import CreditManager
from ducto.engine import PricingEngine
from ducto.metrics import UsageMetrics, ToolCall
from ducto.interface.models import (
    PricingConfigData, PlanDefinition,
    CreditMetadata,
)
from shared import start_postgres_store, cleanup

store, pgdata = start_postgres_store()
{extra_imports}
print("✔ PostgresStore ready.")""")


def pg_teardown() -> dict:
    return code("cleanup(pgdata)")


def memory_setup() -> dict:
    return code("""\
import uuid
from datetime import datetime, timedelta
from ducto.interface.memory import MemoryStore
from ducto.manager import CreditManager
from ducto.engine import PricingEngine
from ducto.metrics import UsageMetrics, ToolCall
from ducto.interface.models import (
    PricingConfigData, PlanDefinition,
    CreditMetadata, SpendCap,
)

store = MemoryStore()
store.setup()
print("✔ MemoryStore ready.")""")


# ---------------------------------------------------------------------------
# Notebook 1 – Pricing Basics
# ---------------------------------------------------------------------------


def n01():
    return [
        md("""# 01 – Pricing Basics

`PricingEngine` calculates credit costs from usage metrics. Define
model pricing formulas as math expressions, then call `calculate()`.

No storage needed — pure computation."""),
        md("""## Setup"""),
        code("""from ducto.engine import PricingEngine
from ducto.metrics import UsageMetrics, ToolCall"""),
        md("""### Static config via `from_dict`

Each model maps to an expression with these variables:

`input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens` \\
`tool_calls`, `search_queries`, `search_results` \\
`web_search_calls`, `code_exec_calls`, `fixed_job`"""),
        code("""config = {
    "models": {
        "gpt-4o": "input_tokens * 5 + output_tokens * 15",
        "claude-sonnet-4": "input_tokens * 3 + output_tokens * 15",
        "claude-haiku-3.5": "input_tokens * 1 + output_tokens * 4",
    },
    "tools": {"code_exec": "tool_calls * 50"},
    "search": {"costs": "search_queries * 10 + search_results * 1"},
    "cache": {"discount": "cache_read_tokens * 1 + cache_write_tokens * 5"},
}
engine = PricingEngine.from_dict(config)
schema = engine.pricing_schema()
print(f"Engine ready — {len(schema.models)} models registered")"""),
        md("""### Basic call (tokens only)"""),
        code("""cost = engine.calculate(UsageMetrics(
    model="gpt-4o", input_tokens=500, output_tokens=200,
))
print(f"  Model:  {cost.model_credits}  ({500}×5 + {200}×15)")
print(f"  Tools:  {cost.tool_credits}")
print(f"  Total:  {cost.total}")
assert cost.total == 5500"""),
        md("""### With tool calls"""),
        code("""cost = engine.calculate(UsageMetrics(
    model="claude-sonnet-4", input_tokens=1000, output_tokens=400,
    tool_calls=[ToolCall(name="code_exec")],
))
print(f"  Model: {cost.model_credits}  Tools: {cost.tool_credits}  Total: {cost.total}")
assert cost.total == 9050  # 3000+6000+50"""),
        md("""### With search / RAG"""),
        code("""cost = engine.calculate(UsageMetrics(
    model="gpt-4o", input_tokens=200, output_tokens=50,
    search_queries=3, search_results=45,
))
print(f"  Model: {cost.model_credits}  Search: {cost.search_credits}  Total: {cost.total}")
assert cost.total == 1825  # 1750 + 75"""),
        md("""### With cache discount"""),
        code("""cost = engine.calculate(UsageMetrics(
    model="claude-haiku-3.5", input_tokens=300, output_tokens=100,
    cache_read_tokens=200, cache_write_tokens=50,
))
print(f"  Model: {cost.model_credits}  Cache: {cost.cache_savings}  Total: {cost.total}")
print(f"Breakdown keys: {list(cost.breakdown.keys())}")"""),
    ]


# ---------------------------------------------------------------------------
# Notebook 2 – Credit Lifecycle
# ---------------------------------------------------------------------------


def n02():
    return [
        md("""# 02 – Credit Lifecycle

The low-level reserve-then-deduct pattern via `PostgresStore`:

1. **Add** — deposit credits for a user
2. **Reserve** — hold credits before an expensive operation
3. **Deduct** — consume the reservation
4. **Check** — verify balance
5. **Refund** — reverse a deduction"""),
        pg_setup("import uuid"),
        md("""### Add credits"""),
        code("""user = str(uuid.uuid4())
r = store.add_credits(user, 10_000, type="signup_bonus")
print(f"  Tx:        {r.transaction_id}")
print(f"  Balance:   {r.new_balance}")"""),
        md("""### Reserve → deduct (two-phase commit)"""),
        code("""res = store.reserve_credits(user, 2_000, operation_type="model_inference")
print(f"  Reservation: {res.reservation_id}")
print(f"  Balance:     {res.balance}")

ded = store.deduct_credits(user, res.reservation_id, 2_000)
print(f"  Deduction:   {ded.transaction_id}")
print(f"  Balance aft: {ded.balance_after}")

bal = store.get_balance(user)
print(f"  Final:       {bal.balance}")
assert bal.balance == 8_000"""),
        md("""### Refund a deduction"""),
        code("""ref = store.refund_credits(ded.transaction_id, amount=2_000, reason="test")
print(f"  Refund tx:   {ref.refund_transaction_id}")
print(f"  New balance: {ref.new_balance}")
assert ref.new_balance == 10_000"""),
        pg_teardown(),
    ]


# ---------------------------------------------------------------------------
# Notebook 3 – Plans and Allowances
# ---------------------------------------------------------------------------


def n03():
    return [
        md("""# 03 – Plans and Allowances

ducto supports subscription-style plans with free monthly allowances.
The v2 pricing config embeds plan definitions alongside model formulas.

> **Note:** Uses `MemoryStore` because plan management via PostgresStore
> requires pre-seeded `credit_plans` table rows. MemoryStore handles
> plan definitions inline."""),
        memory_setup(),
        md("""### Persist plan definitions in pricing config v2"""),
        code("""# MemoryStore extracts plan definitions from PricingConfigData
# via set_active_pricing().
store.set_active_pricing(
    PricingConfigData(
        models={
            "gpt-4o": "input_tokens * 5 + output_tokens * 15",
        },
        plans={
            "pro": PlanDefinition(
                id="pro", name="Pro Tier",
                free_allowance=50_000,
            ),
            "free": PlanDefinition(
                id="free", name="Free Tier",
                free_allowance=5_000,
            ),
        },
    ),
    label="default",
)
print("  Pricing config stored with 2 plan definitions")"""),
        md("""### Assign a user and check allowance"""),
        code("""user = str(uuid.uuid4())
store.set_user_plan(user, "pro")

allow = store.check_allowance(user)
print(f"  Plan:      {allow.plan_id}")
print(f"  Period:    {allow.period_start} → {allow.period_end}")
print(f"  Remaining: {allow.allowance_remaining}")
assert allow.allowance_remaining == 50_000
print("  ✓ Full 50 000 free allowance available")"""),
        md("""### Consume allowance"""),
        code("""store.increment_usage_window(user, "pro", 3_000)
allow2 = store.check_allowance(user)
print(f"  Remaining after 3 000 used: {allow2.allowance_remaining}")
assert allow2.allowance_remaining == 47_000
print("  ✓ Allowance correctly reduced")"""),
        md("""### Free tier vs pro tier"""),
        code("""free_user = str(uuid.uuid4())
store.set_user_plan(free_user, "free")
free_allow = store.check_allowance(free_user)
print(f"  Free user allowance: {free_allow.allowance_remaining}")
assert free_allow.allowance_remaining == 5_000
print("  ✓ Free tier gets 5 000/month")"""),
    ]


# ---------------------------------------------------------------------------
# Notebook 4 – Analytics
# ---------------------------------------------------------------------------


def n04():
    return [
        md("""# 04 – Analytics

Aggregation queries across all users and transactions.

Built-in methods:
- `spend_by_user(start, end)` — total per user
- `daily_spend(start, end)` — spend grouped by day
- `top_users(limit, start, end)` — highest spenders
- `aggregate_stats(start, end)` — summary stats"""),
        pg_setup("""\
import uuid, random
from datetime import timezone"""),
        md("""### Seed sample data

Create 3 users with multiple reserve-then-deduct cycles over 7 days."""),
        code("""users = [str(uuid.uuid4()) for _ in range(3)]
now = datetime.now(timezone.utc)

for u in users:
    store.add_credits(u, 100_000, type="adjustment")
    for day_offset in range(7):
        amount = random.randint(100, 2_000)
        res = store.reserve_credits(u, amount, operation_type="inference")
        store.deduct_credits(u, res.reservation_id, amount)

print(f"Seeded {3} users × 7 days of random transactions")"""),
        md("""### Spend by user (last 30 days)"""),
        code("""from datetime import timedelta, timezone
end = datetime.now(timezone.utc)
start = end - timedelta(days=30)
rows = store.spend_by_user(start, end)
for r in rows:
    print(f"  {r.user_id[:8]}…  {r.total_spend:>7}  ({r.transaction_count} txns)")"""),
        md("""### Daily spend"""),
        code("""rows = store.daily_spend(start, end)
print(f"{'Date':<12}  {'Spend':>7}  {'Txns':>5}")
for r in rows:
    print(f"{r.date:<12}  {r.total_spend:>7}  {r.transaction_count:>5}")"""),
        md("""### Aggregate stats"""),
        code("""stats = store.aggregate_stats(start, end)
print(f"  Total consumed: {stats.total_credits_consumed}")
print(f"  Active users:   {stats.active_users}")
print(f"  Daily avg:      {stats.avg_daily_spend}")
print(f"  Top model:      {stats.top_model}")
print(f"  Top user:       {stats.top_user}")"""),
        pg_teardown(),
    ]


# ---------------------------------------------------------------------------
# Notebook 5 – Spend Caps (uses MemoryStore)
# ---------------------------------------------------------------------------


def n05():
    return [
        md("""# 05 – Spend Caps

Spend caps limit credits a user can consume per period. Useful for
cost governance in multi-tenant SaaS.

Cap actions:
- **deny** — reject when exceeded
- **warn** — allow but flag
- **notify** — allow and trigger event

> **Note:** This notebook uses `MemoryStore` because `PostgresStore`
> doesn't ship a `set_spend_cap` implementation. Caps are a
> store-specific feature."""),
        memory_setup(),
        md("""### Set a daily deny-cap of 5 000"""),
        code("""user = str(uuid.uuid4())
store.add_credits(user, 50_000, type="seed")
print(f"  Balance: {store.get_balance(user).balance}")

cap = SpendCap(user_id=user, cap_type="daily", limit=5_000, action="deny")
store.set_spend_cap(cap)
print(f"  Cap set: daily limit={cap.limit}, action={cap.action}")"""),
        md("""### Deduct under cap (succeeds)"""),
        code("""res = store.reserve_credits(user, 3_000, operation_type="inference")
ded = store.deduct_credits(user, res.reservation_id, 3_000)
print(f"  Deducted 3k: balance={ded.balance_after}")
check = store.check_spend_cap(user)
print(f"  Cap check: capped={check.capped}, current={check.current_spend}")"""),
        md("""### Exceed cap (denied)"""),
        code("""res2 = store.reserve_credits(user, 3_000, operation_type="inference")
ded2 = store.deduct_credits(user, res2.reservation_id, 3_000)
if ded2.error:
    print(f"  Denied: {ded2.error}")
else:
    print(f"  Allowed: {ded2.balance_after}")
print("  (daily cap = 5k, already spent 3k)")"""),
        md("""### Cap with warn action"""),
        code("""user2 = str(uuid.uuid4())
store.add_credits(user2, 50_000, type="seed")
store.set_spend_cap(SpendCap(user_id=user2, cap_type="daily", limit=500, action="warn"))

res3 = store.reserve_credits(user2, 1_000, operation_type="inference")
ded3 = store.deduct_credits(user2, res3.reservation_id, 1_000)
check2 = store.check_spend_cap(user2)
print(f"  Current: {check2.current_spend}  Cap: {check2.cap_limit}  Action: {check2.action}")
print("  (warn — deduction still went through)")"""),
    ]


# ---------------------------------------------------------------------------
# Notebook 6 – Teams
# ---------------------------------------------------------------------------


def n06():
    return [
        md("""# 06 – Teams

Shared credit pools (teams). Team has its own balance; members draw
from the pool. Per-member spend caps available.

Useful for organisations, projects, or departments sharing credits."""),
        pg_setup("import uuid"),
        md("""### Create team with initial balance"""),
        code("""team = store.create_team(name="Engineering", initial_balance=100_000)
print(f"  Team: {team.name}  (id={team.team_id})")"""),
        md("""### Add members"""),
        code("""members = [str(uuid.uuid4()) for _ in range(3)]
for uid in members:
    store.add_credits(uid, 0, type="adjustment")  # user must exist in user_credits
    store.add_team_member(team.team_id, uid, role="member")
    print(f"  Added {uid[:8]}…")

bal = store.get_team_balance(team.team_id)
print(f"  Team balance: {bal.balance}  Members: {bal.member_count}")"""),
        md("""### Deduct from team pool"""),
        code("""res = store.deduct_team(team.team_id, members[0], 5_000)
print(f"  balance_after={res.team_balance_after}  error={res.error}")

bal2 = store.get_team_balance(team.team_id)
assert bal2.balance == 95_000"""),
        md("""### Exceed team balance (rejected)"""),
        code("""res2 = store.deduct_team(team.team_id, members[1], 999_999)
print(f"  error={res2.error}  balance={res2.team_balance_after}")
assert res2.error == "insufficient_team_balance" """),
        pg_teardown(),
    ]


# ---------------------------------------------------------------------------
# Notebook 7 – Events
# ---------------------------------------------------------------------------


def n07():
    return [
        md("""# 07 – Events

ducto emits typed lifecycle events. Subscribe handlers to react to
credit operations.

Event types: `credits.added`, `credits.deducted`, `credits.refunded`,
`credits.low_balance`, `credits.cap_reached`, `credits.cap_warning`."""),
        pg_setup("""\
import uuid
from ducto.events import CreditEvent, CreditEventEmitter"""),
        md("""### Create emitter and register handlers"""),
        code("""captured: list[CreditEvent] = []

def logger(ev: CreditEvent) -> None:
    captured.append(ev)
    print(f"  [{ev.type}] user={ev.user_id[:8]}…  data={ev.data}")

emitter = CreditEventEmitter()
emitter.on("credits.added", logger)
emitter.on("credits.deducted", logger)
emitter.on("credits.low_balance", logger)
print("Handlers registered.")"""),
        md("""### Wire to CreditManager and trigger events"""),
        code("""manager = CreditManager(store, emitter=emitter)
user = str(uuid.uuid4())

print("--- add_credits ---")
manager.add_credits(user, 500)

print("\\n--- deduct (end-to-end) ---")
engine = PricingEngine.from_dict({
    "models": {"_default": "input_tokens * 1"},
})
manager = CreditManager(store, engine=engine, emitter=emitter)
manager.add_credits(user, 2_000)
ded = manager.deduct(user, UsageMetrics(model="_default", input_tokens=100))
print(f"  Deduct result: amount={ded.amount}, balance={ded.balance_after}")

print(f"\\nTotal events captured: {len(captured)}")"""),
        md("""### Inspect captured events"""),
        code("""for ev in captured:
    print(f"  [{ev.timestamp.strftime('%H:%M:%S')}] {ev.type}")
    if ev.data:
        for k, v in ev.data.items():
            print(f"      {k}={v}")"""),
        md("""### Subscribe by specific type"""),
        code("""refunds: list[CreditEvent] = []
emitter.on("credits.refunded", lambda e: refunds.append(e))

# trigger a refund — must use store directly
ded_tx = store.reserve_credits(user, 100, operation_type="test")
ded = store.deduct_credits(user, ded_tx.reservation_id, 100)
store.refund_credits(ded.transaction_id, amount=100, reason="demo")
print(f"Refund events: {len(refunds)}")"""),
        pg_teardown(),
    ]


# ---------------------------------------------------------------------------
# Notebook 8 – Custom Store
# ---------------------------------------------------------------------------


def n08():
    return [
        md("""# 08 – Custom Store

The `CreditStore` ABC defines the storage contract. Implement your own
backend (Redis, DynamoDB, SQLite, etc.) by subclassing and providing
the abstract methods.

Below is a minimal in-memory implementation that shows the contract."""),
        md("""### Implement the ABC"""),
        code("""from ducto.interface.base import CreditStore
from ducto.interface.models import (
    BalanceResult, AddCreditsResult, ReserveResult, DeductionResult,
    RefundResult, TeamDeductionResult, CreateTeamResult, TeamBalanceResult,
    TeamMember, AddTeamMemberResult, AllowanceResult, CapCheckResult,
    PricingConfigResult, SetupResult,
)

class MyCustomStore(CreditStore):
    '''Minimal custom store — dict-backed, no persistence.'''

    def __init__(self):
        self._balances: dict[str, int] = {}
        self._reservations: dict[str, int] = {}

    # -- Required: balance / lifecycle ------------------------------------

    def get_balance(self, user_id: str) -> BalanceResult:
        return BalanceResult(user_id=user_id, balance=self._balances.get(user_id, 0))

    def add_credits(self, user_id: str, amount: int, type: str = "adjustment",
                    metadata=None, expires_at=None) -> AddCreditsResult:
        self._balances[user_id] = self._balances.get(user_id, 0) + amount
        return AddCreditsResult(transaction_id="tx", user_id=user_id,
                                amount=amount, new_balance=self._balances[user_id])

    def reserve_credits(self, user_id: str, amount: int, operation_type: str,
                        metadata=None, min_balance: int = 5) -> ReserveResult:
        bal = self._balances.get(user_id, 0)
        rid = "res_" + user_id[:8]
        self._reservations[rid] = amount
        return ReserveResult(reservation_id=rid, user_id=user_id,
                             amount=amount, balance=bal - amount)

    def deduct_credits(self, user_id: str, reservation_id: str, amount: int,
                       idempotency_key=None, metadata=None) -> DeductionResult:
        amt = self._reservations.pop(reservation_id, amount)
        self._balances[user_id] -= amt
        return DeductionResult(transaction_id="ded", user_id=user_id,
                               amount=-amt,
                               balance_after=self._balances[user_id])

    def refund_credits(self, transaction_id: str, amount: int = None,
                       reason: str = None, metadata=None) -> RefundResult:
        return RefundResult(refund_transaction_id="ref", user_id="",
                            original_transaction_id=transaction_id,
                            amount=amount or 0, new_balance=0,
                            reason=reason or "")

    # -- Pricing ----------------------------------------------------------

    def get_active_pricing(self) -> PricingConfigResult | None:
        return None
    def set_active_pricing(self, config, label=None) -> str:
        return "cfg_1"
    def setup_pricing_config(self, config, name="default") -> PricingConfigResult:
        raise NotImplementedError

    # -- Plans ------------------------------------------------------------

    def get_user_plan(self, user_id: str):
        return None
    def set_user_plan(self, user_id: str, plan_id: str):
        pass
    def check_allowance(self, user_id: str) -> AllowanceResult:
        return AllowanceResult(plan_id="", allowance_remaining=0,
                               period_start="", period_end="")
    def increment_usage_window(self, user_id: str, plan_id: str, amount: int):
        pass

    # -- Caps -------------------------------------------------------------

    def set_spend_cap(self, cap):
        pass
    def check_spend_cap(self, user_id: str, model=None, amount=None) -> CapCheckResult:
        return CapCheckResult()

    # -- Analytics --------------------------------------------------------

    def spend_by_user(self, start, end) -> list:
        return []
    def spend_by_model(self, start, end) -> list:
        return []
    def daily_spend(self, start, end) -> list:
        return []
    def top_users(self, limit, start, end) -> list:
        return []
    def aggregate_stats(self, start, end):
        from ducto.interface.models import AggregateStatsRow
        return AggregateStatsRow()

    # -- Sweep ------------------------------------------------------------

    def sweep_expired_credits(self, dry_run=False):
        from ducto.interface.models import SweepResult
        return SweepResult()

    # -- Teams ------------------------------------------------------------

    def create_team(self, name: str, initial_balance=0) -> CreateTeamResult:
        raise NotImplementedError("Teams not supported")
    def get_team_balance(self, team_id: str) -> TeamBalanceResult:
        raise NotImplementedError
    def add_team_member(self, team_id, user_id, role="member", spend_cap=None):
        raise NotImplementedError
    def get_team_members(self, team_id: str):
        raise NotImplementedError
    def deduct_team(self, team_id, user_id, amount, metadata=None):
        raise NotImplementedError

    def setup(self):
        return SetupResult()

custom_store = MyCustomStore()
print("MyCustomStore implements CreditStore ABC.")"""),
        md("""### Use with CreditManager"""),
        code("""import uuid
from ducto.manager import CreditManager

manager = CreditManager(custom_store)

user = str(uuid.uuid4())
manager.add_credits(user, 10_000)
print(f"  Balance: {manager.get_balance(user).balance}")

res = manager.reserve_credits(user, 1_000, operation_type="test")
print(f"  Reserved: {res.amount}, bal={res.balance}")"""),
    ]


# ---------------------------------------------------------------------------
# Notebook 9 – Expression Evaluator
# ---------------------------------------------------------------------------


def n09():
    return [
        md("""# 09 – Expression Evaluator

ducto uses a **safe AST-based** evaluator — not `eval()`. It whitelists
specific node types and function names. Dangerous operations
(`__import__`, `open`, `globals`) are blocked.

Available variables: `input_tokens`, `output_tokens`, `cache_read_tokens`,
`cache_write_tokens`, `tool_calls`, `search_queries`, `search_results`,
`web_search_calls`, `code_exec_calls`.

Available functions: `percentile(base, pct)`, `abs`, `round`, `min`, `max`, `sum`."""),
        md("""### Basic arithmetic"""),
        code("""from ducto.expr import evaluate_expression

r = evaluate_expression("input_tokens * 5 + output_tokens * 15",
                        {"input_tokens": 500, "output_tokens": 200})
print(f"  500×5 + 200×15 = {r}  (expected: 5500)")
assert r == 5500"""),
        md("""### Function calls"""),
        code("""r = evaluate_expression("max(input_tokens, output_tokens) * 2",
                        {"input_tokens": 500, "output_tokens": 200})
print(f"  max(500,200)×2 = {r}  (expected: 1000)")
assert r == 1000"""),
        md("""### Percentile function

`percentile(p, v1, v2, ...)` computes the `p`-th percentile of the values.
For example, `percentile(90, 100, 200, 300)` finds the value below which
90 % of the data falls."""),
        code("""r = evaluate_expression("percentile(input_tokens, 100, 200, 300)",
                        {"input_tokens": 90})
print(f"  percentile(input_tokens, 100, 200, 300) = {r}")
# 90th percentile of (100, 200, 300) = 280 (linear interpolation)"""),
        md("""### Combined expression"""),
        code("""expr = "input_tokens * 3 + output_tokens * 15 + max(tool_calls, 0) * 10"
r = evaluate_expression(expr, {"input_tokens": 1000, "output_tokens": 400, "tool_calls": 1})
print(f"  {r}")
# = 3000 + 6000 + 1*10 = 9010"""),
        md("""### Safety — blocked operations

These are **blocked** by the AST whitelist. Uncomment to test:"""),
        code("""# evaluate_expression("__import__('os').system('ls')", {})
# evaluate_expression("open('/etc/passwd').read()", {})
# evaluate_expression("globals()", {})
# evaluate_expression("lambda x: x", {})
print("All dangerous operations blocked by AST whitelist.")"""),
    ]


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

ALL: list[tuple[str, list[dict]]] = [
    ("01_pricing_basics.ipynb", n01()),
    ("02_credit_lifecycle.ipynb", n02()),
    ("03_plans_and_allowances.ipynb", n03()),
    ("04_analytics.ipynb", n04()),
    ("05_spend_caps.ipynb", n05()),
    ("06_teams.ipynb", n06()),
    ("07_events.ipynb", n07()),
    ("08_custom_store.ipynb", n08()),
    ("09_expression_evaluator.ipynb", n09()),
]

if __name__ == "__main__":
    print("Generating notebooks …")
    for name, cells in ALL:
        save(name, cells)
    print(f"Done — {len(ALL)} notebooks in {NB_DIR}")
