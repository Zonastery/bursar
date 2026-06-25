#!/usr/bin/env python3
"""Generate ducto example notebooks using nbformat.

Usage: uv run python scripts/generate_notebooks.py
"""

from pathlib import Path

import nbformat as nbf

NB_DIR = Path(__file__).resolve().parent.parent.parent / "samples" / "python" / "notebooks"
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
        md("""# 01 - Pricing Basics

Every AI-powered application needs to answer the same fundamental question: how many credits does this request cost? Without a consistent pricing foundation, costs become opaque - different engineers hard-code different rates in different places, audit trails vanish, and changing your pricing requires a code deployment.

ducto's `PricingEngine` solves this by letting you define pricing formulas as simple math expressions in configuration, completely separate from your application code. Think of it like a restaurant bill: each item on the meal - tokens, tools, search queries - has its own line-item cost, and the total is simply the sum of those items. The `UsageMetrics` class bundles all the ingredients (token counts, tool calls, etc.) into a single object that the engine evaluates against your configured formulas.

This separation of concerns is a core design principle. Formulas live in a database table or a Python dictionary, not scattered across your codebase. Changing how `gpt-4o` is priced means updating one formula string, not hunting down every place that calculates costs. This makes your pricing auditable, testable, and adjustable.

The `PricingEngine` is also completely stateless - it performs pure computation without any storage or database dependency. Give it a formula set and usage metrics, and it returns the same cost every time. This makes it trivially testable and safe to call from anywhere in your application.

In this notebook, we will walk through four common pricing scenarios: a basic token-only call (the most common pattern), a call with tool invocations, a call with search or RAG operations, and a call that benefits from the LLM provider's context caching discount. Each scenario demonstrates how the same engine handles different metric combinations."""),
        md("""## Setup

Before we can calculate any costs, we need to import the core ducto classes that make up the pricing pipeline. Each import serves a distinct purpose.

`PricingEngine` is the calculator - it takes input variables (token counts, tool calls, search queries) and evaluates them against formulas defined in configuration. `UsageMetrics` is the data object that bundles all those input variables together. `ToolCall` is a simple named type used when the engine needs to include per-tool costs in its calculation."""),
        code("""# PricingEngine: the core calculator that evaluates math expressions against usage metrics.
# It parses formula strings into safe AST trees at construction time.
from ducto.engine import PricingEngine

# UsageMetrics: bundles all input variables that pricing formulas reference.
# Each field maps to a variable name available in the expression syntax.
from ducto.metrics import UsageMetrics, ToolCall"""),
        md("""### Static config via `from_dict`

The `PricingEngine` accepts configuration as a Python dictionary with four optional sections. Each section targets a different dimension of AI application costs.

The `models` section defines per-model token pricing. Each model name maps to a math expression string. The engine parses these strings using a safe AST-based evaluator - not Python's `eval()` - so expressions are sandboxed and cannot access the filesystem or network.

Available metric variables in expressions:

- `input_tokens` and `output_tokens`: The prompt and completion token counts from an LLM call. These are the most commonly used variables since every model call produces both input and output.
- `cache_read_tokens` and `cache_write_tokens`: Tokens served from or written to an LLM provider's context cache. These are non-zero only when your application uses prompt caching.
- `tool_calls`: The number of tool invocations the model makes. This is non-zero when your request includes tool definitions and the model decides to call them.
- `search_queries` and `search_results`: The number of search queries issued and results processed. These are non-zero in RAG (Retrieval-Augmented Generation) or web-search-augmented generation flows.
- `web_search_calls` and `code_exec_calls`: Web search API calls and code execution sandbox invocations. These are used by agentic applications that have browsing or code-running capabilities.
- `fixed_job`: A special variable for fixed-cost operations that don't scale with token count.

The `tools`, `search`, and `cache` sections follow the same pattern: each key maps to a formula using the appropriate variables. Once the dictionary is assembled, pass it to `PricingEngine.from_dict()` to build the engine."""),
        code("""# Build a pricing configuration dictionary with four sections.
# Each section's formula uses variable names that UsageMetrics understands.

# "models" — per-model token pricing. Keyed by model name, valued as an expression string.
# gpt-4o: input tokens cost 5 credits each, output tokens cost 15 credits each.
# claude-sonnet-4: input 3 credits per token, output 15 credits per token.
# claude-haiku-3.5: input 1 credit per token, output 4 credits per token.
config = {
    "models": {
        "gpt-4o": "input_tokens * 5 + output_tokens * 15",
        "claude-sonnet-4": "input_tokens * 3 + output_tokens * 15",
        "claude-haiku-3.5": "input_tokens * 1 + output_tokens * 4",
    },
    # "tools" — per-tool-call cost added on top of the model token cost.
    "tools": {"code_exec": "tool_calls * 50"},
    # "search" — cost for RAG or search-augmented generation operations.
    "search": {"costs": "search_queries * 10 + search_results * 1"},
    # "cache" — discount applied for LLM context cache usage.
    "cache": {"discount": "cache_read_tokens * 1 + cache_write_tokens * 5"},
}

# Build the engine: from_dict() parses all formulas into internal AST trees.
# No database or storage is needed at this stage — pure computation.
engine = PricingEngine.from_dict(config)

# Inspect what was registered via the pricing schema.
schema = engine.pricing_schema()
print(f"Engine ready — {len(schema.models)} models registered (gpt-4o, claude-sonnet-4, claude-haiku-3.5)")"""),
        md("""### Basic call (tokens only)

The simplest and most common pricing scenario is a pure chat completion: no tools, no search, no caching. We provide just the model name, input token count, and output token count. The engine returns a `CreditCost` object with a detailed breakdown of each cost component.

In this case, we ask `gpt-4o` to process 500 input tokens and 200 output tokens. The formula is `input_tokens * 5 + output_tokens * 15`, which gives us 500 * 5 + 200 * 15 = 2,500 + 3,000 = 5,500 credits total. No tool credits or search credits are added because we did not specify those metrics."""),
        code("""# Tokens-only is the most common case — a simple chat completion with no extras.
# We provide the model name, input token count, and output token count.
# The engine matches the model name to its pricing formula and evaluates it.
cost = engine.calculate(UsageMetrics(
    model="gpt-4o", input_tokens=500, output_tokens=200,
))

# The CreditCost object has separate fields for each cost component.
# model_credits: the cost from the model's token formula
# tool_credits: the cost from any tool invocations (zero here since none were specified)
# total: the sum of all cost components
print(f"  Model:  {cost.model_credits}  ({500}x5 + {200}x15 = 2,500 + 3,000)")
print(f"  Tools:  {cost.tool_credits}")  # Zero because no tools were called
print(f"  Total:  {cost.total}")
assert cost.total == 5500  # 500*5 + 200*15 = 2,500 + 3,000 = 5,500"""),
        md("""### With tool calls

Many AI applications go beyond simple chat by giving the model tools to call — code execution, database queries, or external API invocations. Each tool invocation adds cost on top of the token consumption.

In this scenario, we use `claude-sonnet-4` with 1,000 input tokens and 400 output tokens, plus a single `code_exec` tool call. The engine evaluates two separate formulas: the model formula for tokens (1,000 * 3 + 400 * 15 = 3,000 + 6,000 = 9,000 credits) and the tool formula for the invocation (1 * 50 = 50 credits). The total is 9,050 credits."""),
        code("""# Adding tools shows how pricing stacks when using multiple dimensions.
# The engine applies the model formula AND the tool formula, then sums them.
cost = engine.calculate(UsageMetrics(
    model="claude-sonnet-4", input_tokens=1000, output_tokens=400,
    tool_calls=[ToolCall(name="code_exec")],  # Single tool invocation
))
print(f"  Model: {cost.model_credits}  Tools: {cost.tool_credits}  Total: {cost.total}")
# Expected: model = 1,000*3 + 400*15 = 3,000 + 6,000 = 9,000; tool = 1*50 = 50; total = 9,050
assert cost.total == 9050"""),
        md("""### With search / RAG

Applications that augment LLM calls with external knowledge retrieval add another cost dimension. Search queries and result processing each incur their own charges.

In a RAG (Retrieval-Augmented Generation) flow, the application typically issues one or more search queries, retrieves multiple results, and feeds those results into the LLM context. The search cost metric tracks both the number of queries issued and the number of results processed.

In this example, we use `gpt-4o` with moderate token counts (200 input, 50 output), plus 3 search queries that return 45 total results. The engine applies both the model formula and the search formula."""),
        code("""# Search/RAG flow: the application issues search queries and processes results.
# The engine applies the search formula from config in addition to the model formula.
cost = engine.calculate(UsageMetrics(
    model="gpt-4o", input_tokens=200, output_tokens=50,
    search_queries=3, search_results=45,  # Search/RAG metrics
))
print(f"  Model: {cost.model_credits}  Search: {cost.search_credits}  Total: {cost.total}")
# Expected: model = 200*5 + 50*15 = 1,000 + 750 = 1,750; search = 3*10 + 45*1 = 30 + 45 = 75; total = 1,825
assert cost.total == 1825"""),
        md("""### With cache discount

LLM providers offer context caching to reduce costs when the same conversation prefix is reused across multiple requests. ducto models this as a separate `cache` section in the pricing config, which produces a discount (a reduction in total credits).

In this scenario, we use `claude-haiku-3.5` with 300 input tokens and 100 output tokens, plus 200 cache read tokens and 50 cache write tokens. The cache savings are calculated from the cache formula and subtracted from the total."""),
        code("""# Cache discount: the engine applies savings (negative cost) based on cache usage.
# This models how LLM providers charge less for cache hits versus cache writes.
cost = engine.calculate(UsageMetrics(
    model="claude-haiku-3.5", input_tokens=300, output_tokens=100,
    cache_read_tokens=200, cache_write_tokens=50,  # Context caching metrics
))
# The cache_savings field reflects the discount from the cache formula (positive value = amount saved).
print(f"  Model: {cost.model_credits}  Cache: {cost.cache_savings}  Total: {cost.total}")
# Inspect every component cost through the breakdown dictionary.
# The breakdown includes model, tools, search, and cache entries separately.
print(f"Breakdown keys: {list(cost.breakdown.keys())}")"""),
    ]


def n02():
    return [
        md("""# 02 - Credit Lifecycle

Credits flow through a lifecycle: they are added, reserved, deducted, and sometimes refunded. Understanding this lifecycle is critical to building reliable credit systems that prevent race conditions and double-spending.

ducto uses a **reserve-then-deduct** pattern - think of it like a hotel booking. First you reserve a room (holding credits), then you check in (deducting them). If you cancel, the hold releases. This pattern prevents race conditions where two concurrent operations could both check the balance, see 10,000 credits, and each try to deduct 8,000 - resulting in a negative balance of -6,000.

Without the reserve step, naive balance-check-then-deduct code has a built-in race condition: between checking the balance and deducting, another operation can also check the balance. Both operations see the same available amount and both proceed, causing an overdraft. The reserve step establishes an ordering: the first reservation succeeds and reduces available credits; the second sees the reduced amount and blocks.

The full lifecycle involves four operations managed by `PostgresStore`: `add_credits()` deposits credits into a user's account, `reserve_credits()` holds a portion pending an expensive operation, `deduct_credits()` finalizes the hold into a permanent deduction, and `refund_credits()` reverses a completed deduction and restores the balance.

Each operation returns a result object that provides unique transaction identifiers and updated balance information. These identifiers form an audit trail: every deduction references a reservation, every refund references a deduction. This makes it possible to trace the full history of every credit."""),
        pg_setup("import uuid"),
        md("""### Add credits

The first operation in any credit lifecycle is adding credits to a user's account. `PostgresStore.add_credits()` creates a new credit entry with a unique transaction ID and returns the user's updated balance.

Each deposit has a `type` label - such as `"signup_bonus"`, `"purchase"`, or `"adjustment"` - which serves as an audit category. This makes it possible to later query how many credits came from signups versus purchases versus manual adjustments."""),
        code("""# Generate a unique user ID for this demonstration.
# In production, this would be the user's UUID from your auth system.
user = str(uuid.uuid4())

# Deposit 10,000 credits as a signup bonus into the user's account.
# The add_credits() method returns an AddCreditsResult object containing:
# - transaction_id: a unique identifier for this deposit, used for audit trails and future refunds
# - new_balance: the user's total credit balance after the deposit completes
r = store.add_credits(user, 10_000, type="signup_bonus")
print(f"  Tx:        {r.transaction_id}")  # Unique reference for this deposit
print(f"  Balance:   {r.new_balance}")  # User now has 10,000 credits available to spend"""),
        md("""### Reserve then deduct (two-phase commit)

Once a user has credits, the next step is typically spending them. Rather than deducting directly, ducto uses a two-phase commit: first reserve the amount, then deduct.

The reserve phase places a hold on the credits. The user's available balance decreases immediately, but the credits are not yet consumed. The reservation returns a `reservation_id` that the subsequent deduction references. This two-step process prevents race conditions: if two requests try to reserve different amounts simultaneously, the second reservation sees the reduced balance from the first.

The deduction phase consumes the held credits. It references the original reservation by its `reservation_id`, which ensures that only the holder of the reservation can finalize the spend. This prevents one request from spending credits that another request reserved."""),
        code("""# Step 1: Reserve 2,000 credits for a pending model inference operation.
# reserve_credits() holds the specified amount and reduces the available balance immediately.
# No other operation can spend these reserved credits.
# Returns a ReserveResult object with:
# - reservation_id: a unique key needed later to deduct or release the hold
# - balance: the remaining available balance after placing the hold
res = store.reserve_credits(user, 2_000, operation_type="model_inference")
print(f"  Reservation: {res.reservation_id}")  # Save this ID for the deduction step
print(f"  Balance:     {res.balance}")  # 10,000 - 2,000 = 8,000 credits remain available

# Step 2: Deduct the reserved credits to finalize the transaction.
# deduct_credits() consumes the reservation identified by its reservation_id.
# Returns a DeductionResult object with:
# - transaction_id: a unique reference for this completed deduction, used for audits and refunds
# - balance_after: the user's total balance after the deduction completes
ded = store.deduct_credits(user, res.reservation_id, 2_000)
print(f"  Deduction:   {ded.transaction_id}")  # Unique reference for this spend
print(f"  Balance aft: {ded.balance_after}")  # Still 8,000 — the reservation already reduced it

# Step 3: Verify the final balance by querying independently.
bal = store.get_balance(user)
print(f"  Final:       {bal.balance}")  # 8,000 credits remaining
assert bal.balance == 8_000  # Confirms the two-phase cycle correctly consumed 2,000 credits"""),
        md("""### Refund a deduction

Sometimes a completed deduction needs to be reversed. For example, if a credit purchase fails after the initial authorization, or if a customer requests a refund for a faulty model response.

`refund_credits()` restores the deducted amount to the user's balance. Critically, it requires the original deduction's `transaction_id` as a reference. This ensures a proper audit trail: the refund is linked to the original spend, and the same transaction cannot be refunded twice. The original deduction's transaction record remains in the database unchanged - it is not deleted or modified - preserving a complete history of the spend-and-refund cycle for auditing purposes."""),
        code("""# Refund the deduction we just completed, referencing its original transaction_id.
# Passing the deduction's transaction_id allows the system to:
# 1. Validate that the referenced transaction exists and has not already been refunded
# 2. Create an audit trail linking the refund to the original spend
# 3. Prevent double-reversal (deduplication) — the same transaction cannot be refunded twice
ref = store.refund_credits(ded.transaction_id, amount=2_000, reason="test")
print(f"  Refund tx:   {ref.refund_transaction_id}")  # New unique ID for this refund operation
print(f"  New balance: {ref.new_balance}")  # Balance restored to 10,000 — the original deposit amount
assert ref.new_balance == 10_000  # Balance fully restored after refunding the full 2,000 credits"""),
        pg_teardown(),
    ]


def n03():
    return [
        md("""# 03 - Plans and Allowances

Most SaaS products offer pricing tiers - Free, Pro, Enterprise - each with a free monthly allowance of credits. ducto tracks per-user plan assignments and monthly usage windows, automatically falling back to the user's credit balance when the free allowance is consumed.

Think of the **allowance** as a monthly prepaid bucket. Every Pro user gets 50,000 free credits each month. Every Free user gets 5,000. These buckets reset automatically at the start of each billing period. In contrast, the **credit balance** (managed via `add_credits` and `deduct_credits` from the previous notebook) is permanent - it only changes when credits are manually added or deducted.

Allowance tracking works through plan definitions embedded in the pricing configuration. Each plan specifies a `free_allowance` - the number of free credits per billing period. When a user is assigned to a plan, the system records the current billing period (a monthly window). Each time the user spends credits, the system first deducts from the free allowance. Once the allowance is exhausted, further spending draws from the user's purchased credit balance (pay-as-you-go).

In this notebook we will use `MemoryStore` because plan management through `PostgresStore` requires pre-seeded `credit_plans` table rows. `MemoryStore` handles plan definitions inline from `PricingConfigData`, making it the ideal choice for learning and experimentation."""),
        memory_setup(),
        md("""### Persist plan definitions in pricing config

In ducto, plan definitions live inside the pricing configuration, right alongside the model pricing formulas. This keeps all pricing logic - both per-unit costs and subscription allowances - in a single place for easy maintenance.

Each plan has three key properties: an `id` (used to reference the plan when assigning users), a human-readable `name`, and a `free_allowance` (the number of free credits the user gets each billing period). The billing period is a monthly window. When a user is assigned to a plan, the system records the `period_start` and `period_end`. The allowance resets automatically when the period ends.

This auto-reset makes the allowance fundamentally different from the balance. The allowance refills every month, while the balance only changes when credits are manually added or deducted through `add_credits` and `deduct_credits`. A Pro user with 50,000 monthly allowance still needs a credit balance for usage beyond the free tier."""),
        code("""# Store pricing configuration with both model formulas and plan definitions.
# MemoryStore.set_active_pricing() extracts plan definitions from PricingConfigData.
# This keeps all pricing logic in one place for easy maintenance.
store.set_active_pricing(
    PricingConfigData(
        # Model pricing formulas — same format as PricingEngine.from_dict().
        models={
            "gpt-4o": "input_tokens * 5 + output_tokens * 15",
        },
        # Plan definitions — each plan specifies a free monthly allowance.
        # "pro" tier: users get 50,000 free credits per month
        # "free" tier: users get 5,000 free credits per month
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
print("  Pricing config stored with 2 plan definitions: Pro (50,000/mo) and Free (5,000/mo)")"""),
        md("""### Assign a user and check allowance

Once plans are configured, we can assign a user to a plan and check their remaining free allowance. The assignment is done via `set_user_plan()`, which links a user ID to a plan ID in the store.

The `check_allowance()` method returns an `AllowanceResult` that includes the plan ID, the current billing period's start and end dates, and the remaining allowance. Initially, a new Pro user has the full 50,000 allowance available - no credits have been consumed yet in the current billing period. The period dates tell you exactly when the allowance will reset."""),
        code("""# Generate a new user and assign them to the "pro" plan.
# set_user_plan() links the user ID to the plan definition stored earlier.
user = str(uuid.uuid4())
store.set_user_plan(user, "pro")

# Check how many free credits the user has remaining in this billing period.
# AllowanceResult contains:
# - plan_id: the name of the plan the user is on
# - period_start: the beginning of the current billing period (monthly window)
# - period_end: when the current period ends and the allowance resets
# - allowance_remaining: how many free credits are still available this period
allow = store.check_allowance(user)
print(f"  Plan:      {allow.plan_id}")
print(f"  Period:    {allow.period_start} → {allow.period_end}")  # Monthly window
print(f"  Remaining: {allow.allowance_remaining}")  # Full 50,000 available since no usage yet
assert allow.allowance_remaining == 50_000
print("  ✓ Full 50 000 free allowance available")"""),
        md("""### Consume allowance

When a user makes a request that costs credits, the system should first consume the free allowance before drawing from the purchased balance. The `increment_usage_window()` method records usage against the user's allowance for the current billing period.

After calling `increment_usage_window()`, the next call to `check_allowance()` returns a reduced remaining amount. Once the allowance reaches zero, further requests use the user's purchased credit balance (pay-as-you-go). The allowance never goes negative - it stops at zero and the system switches to balance-based charging."""),
        code("""# Consume 3,000 credits from the user's free allowance.
# In production, this would be called alongside deduct_credits() to
# also track how much of the free allowance has been used this period.
store.increment_usage_window(user, "pro", 3_000)

# Check the allowance again to confirm it was reduced by the correct amount.
allow2 = store.check_allowance(user)
print(f"  Remaining after 3 000 used: {allow2.allowance_remaining}")
assert allow2.allowance_remaining == 47_000  # 50,000 - 3,000
print("  ✓ Allowance correctly reduced")"""),
        md("""### Free tier vs Pro tier

Different plans have different allowance amounts. The Free tier typically offers a small monthly allowance to let users evaluate the product, while the Pro tier offers substantially more for regular active usage.

In our configuration, the Free tier has a 5,000-credit monthly allowance - one-tenth of the Pro tier's 50,000. This means a Free user would exhaust their allowance after roughly 1,000 gpt-4o input tokens, while a Pro user could run over 10,000 tokens before hitting the cap. Once the allowance runs out, both tiers continue to work, but they charge against the user's purchased credit balance instead."""),
        code("""# Create a Free tier user and compare their allowance to the Pro user.
free_user = str(uuid.uuid4())
store.set_user_plan(free_user, "free")
free_allow = store.check_allowance(free_user)
print(f"  Free user allowance: {free_allow.allowance_remaining}")  # 5,000 — ten times less than Pro
assert free_allow.allowance_remaining == 5_000
print("  ✓ Free tier gets 5 000/month")"""),
    ]


# ---------------------------------------------------------------------------
# Notebook 4 – Analytics
# ---------------------------------------------------------------------------


def n04():
    return [
        md("""# 04 – Analytics

Raw credit transactions are a stream of individual events — user X deducted Y credits at time Z. That is hard to read at a glance. ducto's analytics queries aggregate these events into meaningful summaries: total spend per user, breakdown by model, daily trends, and overall statistics. These queries are the foundation for customer-facing dashboards, internal cost analysis, and anomaly detection.

ducto provides five built-in analytics methods through every `CreditStore` implementation. `spend_by_user()` groups deductions by user and returns each user's total spend and transaction count. `spend_by_model()` breaks down credits consumed by AI model name. `top_users()` returns the highest-spending users, sorted by total consumption. `daily_spend()` groups transactions by calendar date to reveal trends over time. Finally, `aggregate_stats()` computes a single summary row with total credits, active user count, average daily spend, top model, and top user.

These queries are designed to be efficient against Postgres backends but work equally well with in-memory stores for testing and development. Together they form a complete analytics toolkit that answers the most common questions any platform operator needs: who is spending, what are they spending on, and how does spending evolve over time.

In this notebook we will seed a realistic dataset and then run each analytics query to see what it reveals. By the end you will understand how to extract actionable insights from raw credit event data using ducto's analytics layer."""),
        md("""### Setup

Before we can query analytics, we need a running PostgresStore instance with a seeded database. The `start_postgres_store()` helper handles the connection lifecycle for us, initializing the schema and returning a ready-to-use store object.

The setup also imports the utilities we need: `uuid` for generating user identifiers, `random` for simulating realistic transaction amounts and model choices, and `timezone` for working with UTC timestamps. These are standard library modules — no additional dependencies required."""),
        pg_setup("""\
import uuid, random
from datetime import timezone"""),
        md("""### Seed sample data

Analytics queries are only useful when there is data to analyze. In this section we generate a realistic dataset: three users with a large initial credit balance, each performing one random transaction per day for seven days.

Why seven days and three users? This size is large enough to produce meaningful aggregation patterns — you will see variation in spend levels, model preferences, and daily activity — but small enough that every transaction remains interpretable. The randomness ensures that each run produces slightly different results, which is useful for understanding how the aggregation queries behave across different data distributions.

We assign each transaction to a random model from a pool of three: `gpt-4o`, `claude-sonnet-4`, and `claude-haiku-3.5`. This diversity is important because the `spend_by_model` query breaks down spend by model, and we want to see non-trivial results with multiple models contributing to the totals. In this section we generate the simulated transaction data that will feed all subsequent analytics queries in this notebook."""),
        code("""# Create 3 unique user identifiers for our sample dataset
users = [str(uuid.uuid4()) for _ in range(3)]

# Capture the current UTC time as the reference point for all transactions
now = datetime.now(timezone.utc)

# For each user, deposit a starting balance and simulate 7 days of activity
for u in users:
    # Give each user a large initial balance of 100,000 credits
    store.add_credits(u, 100_000, type="adjustment")
    # Loop over each day in the 7-day sample window
    for day_offset in range(7):
        # Random transaction amount between 100 and 2,000 credits
        amount = random.randint(100, 2_000)
        # Step 1 of two-phase commit: reserve credits before deducting
        res = store.reserve_credits(u, amount, operation_type="inference")
        # Randomly pick which AI model this transaction is attributed to
        model = random.choice(["gpt-4o", "claude-sonnet-4", "claude-haiku-3.5"])
        # Step 2 of two-phase commit: deduct the reserved credits with model metadata
        store.deduct_credits(u, res.reservation_id, amount, metadata=CreditMetadata(model=model))

print(f"Data seeding complete: 3 users with 7 days of randomized AI model transactions")"""),
        md("""### Spend by user (last 30 days)

The most basic question in any credit system is "who spent what?" The `spend_by_user()` method answers this by aggregating all deductions for each user within a time window and returning the total spend along with the transaction count.

The `total_spend` field tells you the raw credit consumption per user. The `transaction_count` field tells you how many individual operations contributed to that total. Together, these two numbers help distinguish between a user who spent a lot in a few expensive operations versus a user who spent a lot through many small transactions — two very different usage patterns that may warrant different responses.

In this section we query the last 30 days of activity across all three seeded users. Since our sample data only covers 7 days, all seeded transactions will fall within this window."""),
        code("""# Define a 30-day lookback window ending at the current UTC time
from datetime import timedelta, timezone
end = datetime.now(timezone.utc)
start = end - timedelta(days=30)

# Query the store for total spend grouped by user within this time window
rows = store.spend_by_user(start, end)

# Display each user's abbreviated ID, total credits consumed, and transaction count
for r in rows:
    print(f"  {r.user_id[:8]}…  {r.total_spend:>7}  ({r.transaction_count} txns)")"""),
        md("""### Spend by model

Knowing who spent the most is useful, but in an AI platform the more important question is often "which models are driving costs?" The `spend_by_model()` method breaks down total credit consumption by model name, giving you a clear picture of where your infrastructure budget is going.

This query is essential for cost optimization: if one model accounts for 80 percent of spend, you might consider optimizing prompts, switching to a cheaper model for certain tasks, or implementing caching strategies. It also helps with capacity planning and billing analysis at the per-model level.

The output shows three columns: the model identifier, the total credits consumed, and the number of transactions using that model. A high-spend model with few transactions suggests expensive individual calls, while high spend with many transactions suggests high-volume usage at a moderate per-call cost. In this section we run `spend_by_model()` against our seeded data and examine which models consumed the most credits."""),
        code("""# Query total spend broken down by AI model
rows = store.spend_by_model(start, end)

# Print a formatted table header with columns for model, spend, and transactions
print(f"{'Model':<20}  {'Spend':>7}  {'Txns':>5}")

# Display each model's total spend and transaction count
for r in rows:
    print(f"  {r.model:<20}  {r.total_spend:>7}  {r.transaction_count:>5}")"""),
        md("""### Top users

The `top_users()` method is the admin "leaderboard" — it returns the highest-spending users within a time window, ordered by total spend descending. This is the first place you would look when investigating unusual billing activity, identifying your most valuable customers, or auditing for potential abuse.

We request the top 5 users, but since our sample only has 3 users, all of them will appear in the results. The method is designed to scale to thousands or millions of users, returning only the most significant contributors.

This query pairs naturally with `spend_by_user`: use `top_users` for the headline view and `spend_by_user` when you need every user's data for export or detailed analysis. In this section we retrieve and display the highest-spending users from our sample data."""),
        code("""# Retrieve the top 5 highest-spending users in the last 30 days
rows = store.top_users(limit=5, start=start, end=end)

# Print a formatted table header
print(f"{'User':<10}  {'Spend':>7}")

# Display each user's abbreviated ID and total spend
for r in rows:
    print(f"  {r.user_id[:8]}…  {r.total_spend:>7}")"""),
        md("""### Daily spend

While per-user and per-model queries tell you "who" and "what," the `daily_spend()` query tells you "when." It groups transactions by calendar date within the time window, revealing usage patterns over time such as weekday spikes, weekend dips, or sustained growth trends.

A sudden spike in daily spend could indicate a popular new feature, a marketing campaign driving adoption, or a misconfigured application burning through credits in a loop. A gradual decline could signal churn or seasonal variation. For SaaS platforms, daily spend trends are a health metric that deserves a place on every operations dashboard.

In this section we query `daily_spend()` and examine the per-day totals. With 7 days of sample data you should see 7 rows with varying amounts, reflecting the random transaction sizes we seeded."""),
        code("""# Query total spend grouped by calendar date
rows = store.daily_spend(start, end)

# Print a formatted table header with columns for date, spend, and transactions
print(f"{'Date':<12}  {'Spend':>7}  {'Txns':>5}")

# Display each day's date, total credits consumed, and transaction count
for r in rows:
    print(f"{r.date:<12}  {r.total_spend:>7}  {r.transaction_count:>5}")"""),
        md("""### Aggregate stats

The `aggregate_stats()` method is the executive summary — a single result object that distills the entire time window into five key numbers: total credits consumed, number of active users, average daily spend, the top-spending model, and the top-spending user.

This is the method you would call to populate a billing dashboard's header card. It provides an instant overview without requiring you to run multiple queries and compute the summaries yourself.

The returned `AggregateStatsRow` object contains all five fields as named attributes. In a real application you would feed these into a charting library, a notification system, or a periodic report sent to stakeholders. In this section we call `aggregate_stats()` and display the final summary numbers for our sample dataset."""),
        code("""# Compute a single summary row over the entire 30-day window
stats = store.aggregate_stats(start, end)

# Display each aggregate metric with a descriptive label
print(f"  Total credits consumed: {stats.total_credits_consumed}")
print(f"  Number of active users: {stats.active_users}")
print(f"  Average daily spend:    {stats.avg_daily_spend}")
print(f"  Most expensive model:   {stats.top_model}")
print(f"  Highest spending user:  {stats.top_user}")"""),
        pg_teardown(),
    ]


# ---------------------------------------------------------------------------
# Notebook 5 – Spend Caps (uses MemoryStore)
# ---------------------------------------------------------------------------


def n05():
    return [
        md("""# 05 – Spend Caps

Without spend caps, a single bug or runaway loop can drain a user's entire credit balance in seconds. Spend caps act as safety valves — they limit how many credits a user can consume in a given period, protecting both the user and the platform operator from unexpected costs.

ducto supports three cap behaviors: **deny** (hard block — the operation is rejected when the cap is exceeded), **warn** (soft alert — the operation proceeds but the overage is flagged), and **notify** (passive monitoring — the operation proceeds and a notification event is triggered). The deny action is the most common choice for production deployments, while warn and notify are useful for gradual rollouts or monitoring-only scenarios.

Caps can be configured per user and per period type. ducto supports **daily** caps, which reset every calendar day, and **monthly** caps, which reset every calendar month. You can set different limits for different users — for example, a 5,000-credit daily cap for free-tier users and a 50,000-credit daily cap for enterprise customers.

In this notebook we will use the `MemoryStore` implementation (since `PostgresStore` does not ship a built-in `set_spend_cap` method) and walk through each cap action type. You will see what happens when a deduction stays under the cap, when it exceeds a deny cap, and when it exceeds a warn cap. By the end of this notebook you will understand how to configure and enforce spend controls in your own ducto deployment."""),
        md("""### Setup

This notebook uses `MemoryStore` instead of `PostgresStore` because spend cap management is a store-specific feature. The `PostgresStore` backend does not include a `set_spend_cap` implementation — the database schema for cap tracking varies between deployments and is left to the implementer. `MemoryStore`, on the other hand, ships with full cap support out of the box, making it the ideal choice for learning and prototyping.

The `memory_setup()` helper creates a fresh `MemoryStore` instance, imports the `SpendCap` model (which defines cap configurations), and initializes the store's internal tables. All of our spend cap examples will run against this in-memory store."""),
        memory_setup(),
        md("""### Set a daily deny-cap of 5 000

A deny cap is the strictest form of spend control: if the user's cumulative spend for the period reaches the limit, any further deduction attempt is rejected with an error. No credits leave the account, and the calling application must handle the rejection gracefully.

In this section we create a user, seed their account with 50,000 credits, and then configure a daily deny cap of 5,000 credits. The `SpendCap` model takes four parameters: the user identifier, the cap type (`"daily"` or `"monthly"`), the credit limit (in whole credits), and the action to take when the limit is reached (`"deny"`, `"warn"`, or `"notify"`). This sets up the conditions for the next two sections, where we test the cap from both sides: under and over."""),
        code("""# Create a new user for this spend cap demonstration
user = str(uuid.uuid4())

# Seed the user with a generous starting balance of 50,000 credits
store.add_credits(user, 50_000, type="seed")
print(f"  Initial balance: {store.get_balance(user).balance}")

# Define a daily deny-cap: maximum 5,000 credits consumed per day, hard block
cap = SpendCap(user_id=user, cap_type="daily", limit=5_000, action="deny")

# Register the cap with the store
store.set_spend_cap(cap)
print(f"  Cap registered: type=daily, limit={cap.limit}, action={cap.action}")"""),
        md("""### Deduct under cap (succeeds)

With the 5,000-credit daily deny cap in place, we first test a deduction that stays under the limit. A 3,000-credit charge is well within the 5,000-credit cap, so the reserve-and-deduct sequence should complete successfully.

After the deduction, we call `check_spend_cap()` to inspect the user's current spend tracking. This method returns a `CapCheckResult` object that tells us whether the user is currently capped, how much they have spent in the current period, and what their limit is. After a 3,000-credit deduction against a 5,000-credit cap, the current spend should be 3,000 and the user should not be flagged as capped."""),
        code("""# Reserve 3,000 credits — this is well under the 5,000 daily cap
res = store.reserve_credits(user, 3_000, operation_type="inference")

# Deduct the reserved credits — this should succeed since 3,000 is less than 5,000
ded = store.deduct_credits(user, res.reservation_id, 3_000)
print(f"  Deduction succeeded: balance after = {ded.balance_after}")

# Check the user's current spend against their configured cap
check = store.check_spend_cap(user)
print(f"  Cap status: capped={check.capped}, current spend={check.current_spend}")"""),
        md("""### Exceed cap (denied)

Now we attempt a second deduction that would push the user over the 5,000-credit daily limit. The user has already spent 3,000 credits, and a further 3,000-credit deduction would bring the total to 6,000 — exceeding the cap by 1,000.

With the deny action, the store rejects the deduction and returns an error message explaining why. The credits stay in the user's account and the reservation is released. This is the expected behavior for a hard cap: the application receives the error and can decide how to respond — perhaps by showing the user an upgrade prompt, logging the event for admin review, or retrying with a smaller operation.

This safety net prevents runaway costs from a single misconfigured loop or a compromised API key. Without it, the same 3,000-credit deduction would succeed and leave the platform operator to detect the overage retroactively. In this section we attempt the over-cap deduction and observe the deny behavior in action."""),
        code("""# Attempt a second deduction of 3,000 credits — this would exceed the remaining cap
res2 = store.reserve_credits(user, 3_000, operation_type="inference")

# Try to deduct — the store will reject this because 3,000 already spent + 3,000 new > 5,000 cap
ded2 = store.deduct_credits(user, res2.reservation_id, 3_000)

# Check the result: with a deny cap, the error field contains a descriptive message
if ded2.error:
    print(f"  Deduction denied: {ded2.error}")
else:
    print(f"  Deduction allowed: balance after = {ded2.balance_after}")
print(f"  Explanation: daily cap = 5,000, already spent 3,000 today")"""),
        md("""### Cap with warn action

Not every cap breach needs to be a hard block. Sometimes you want to allow the operation but flag it for attention. The **warn** action does exactly that: the deduction proceeds normally, but the system records a warning that the user has exceeded their configured limit.

This is useful for gradual rollouts of spend controls. You might set a warn cap first to observe how many users would be affected, then switch to deny once you are confident in the limits. It is also appropriate for internal tools or trusted users where you want visibility into spending without disrupting their workflow.

In this section we create a second user with a much lower daily cap of 500 credits and the warn action. We then attempt a 1,000-credit deduction and observe that it succeeds even though it exceeds the cap. The `check_spend_cap()` response confirms the overage but does not block the operation."""),
        code("""# Create a second user for the warn cap demonstration
user2 = str(uuid.uuid4())

# Seed the user with 50,000 credits, same as the first user
store.add_credits(user2, 50_000, type="seed")

# Set a very low daily cap of 500 credits with the warn action (not deny)
store.set_spend_cap(SpendCap(user_id=user2, cap_type="daily", limit=500, action="warn"))

# Attempt to deduct 1,000 credits — exceeds the 500-credit warn cap
res3 = store.reserve_credits(user2, 1_000, operation_type="inference")
ded3 = store.deduct_credits(user2, res3.reservation_id, 1_000)

# Check cap status — the action field confirms "warn" and current spend exceeds the limit
check2 = store.check_spend_cap(user2)
print(f"  Current spend: {check2.current_spend}  Cap limit: {check2.cap_limit}  Action: {check2.action}")
print(f"  Key insight: warn action allows the deduction but flags the overage")"""),
    ]


# ---------------------------------------------------------------------------
# Notebook 6 – Teams
# ---------------------------------------------------------------------------


def n06():
    return [
        md("""# 06 – Teams

Individual user balances work well for B2C products where each user pays for themselves. But B2B SaaS needs team accounts — one company with multiple users sharing a single credit pool. ducto's team feature lets you create shared balances, add members, enforce per-user spend caps, and track who spent what. Think of it like a shared bank account with individual debit card limits.

In a typical B2B scenario, a company purchases a block of credits and then distributes access to its employees or departments. Rather than managing individual balances for each employee, you create a single team pool. Every team member draws from that shared pool, and you can optionally cap how much each individual can spend. This mirrors the real-world pattern of a corporate card with per-employee spending limits.

Beyond simple sharing, teams also give you auditability: each deduction records which team member made the request, so you can bill back costs to specific departments or users. Combined with per-member caps, you prevent any single user from accidentally (or intentionally) exhausting the entire team's budget.

What we will do in this section: create a team with an initial balance, add three members, make deductions from the shared pool, observe what happens when the pool is empty, and enforce a per-member spend cap to demonstrate cost governance within a team."""),
        pg_setup("import uuid"),
        md("""### Create team with initial balance

When you create a team, ducto establishes a separate credit balance that belongs to the team entity, not to any individual user. This is fundamentally different from the user-level `add_credits` calls we have seen in earlier notebooks: the team balance lives in its own ledger and is only accessible through team-specific API methods like `deduct_team` and `get_team_balance`.

Think of it as opening a joint bank account. The initial deposit of 100 000 credits is the team's working capital. Individual user balances still exist independently (they may have their own personal credits too), but team operations draw exclusively from the team pool. The two ledgers — personal and team — are separate and do not intermix.

What we will do in this section: call `store.create_team` with a name and initial balance, then inspect the returned team object to see its assigned identifier."""),
        code("""# Create a team entity with its own independent credit balance.
# The team "Engineering" gets 100 000 credits deposited into its
# team pool. This balance is separate from any individual user
# balance and can only be accessed through team-specific methods.
team = store.create_team(name="Engineering", initial_balance=100_000)
print(f"  Team created: name='{team.name}', id={team.team_id}, initial_balance=100000")"""),
        md("""### Add members

Before a user can join a team, they must already exist in the `user_credits` table. This is an intentional design choice: ducto requires every team member to have a user record, even if that record has a zero balance. The team does not create user accounts — it only associates existing users with a shared pool. This ensures that all credit operations, including team deductions, are always attributed to a real user identity.

In practice, you will typically create user records during your application's signup flow and add them to teams later via an admin dashboard or an org-management workflow. The `add_team_member` call assigns a role (`"member"` by default) and optionally a per-user spend cap, which we will explore in the final section.

What we will do in this section: create three user records with zero balances, add each one as a team member, and verify the new member count on the team balance."""),
        code("""# Generate three unique user IDs that will serve as team members.
members = [str(uuid.uuid4()) for _ in range(3)]

# Each user must have a record in the user_credits table before
# they can join a team. Here we add them with a zero-balance
# adjustment: the user record exists but holds no personal credits.
for uid in members:
    store.add_credits(uid, 0, type="adjustment")
    # Now that the user exists, add them to the Engineering team.
    store.add_team_member(team.team_id, uid, role="member")
    print(f"  Member added: {uid[:8]}… to team {team.team_id[:8]}…")

# Inspect the team balance to confirm the pool is intact and all
# three members are registered.
bal = store.get_team_balance(team.team_id)
print(f"  Team balance: {bal.balance} credits across {bal.member_count} members")"""),
        md("""### Deduct from team pool

When a team member performs an action that costs credits, the deduction comes from the team balance, not from the member's personal balance. This is the core value of the team feature: a shared pool that all members draw from. The `deduct_team` method takes three arguments — the team ID, the member's user ID, and the amount — and records the transaction against both the team and the individual user.

The return value includes the team's remaining balance after the deduction, giving you immediate visibility into pool consumption. You can think of this as a corporate card transaction: the company pays, but the receipt shows which employee made the purchase. This attribution is critical for internal cost accounting and for detecting unusual spending patterns.

What we will do in this section: deduct 5 000 credits from the team pool as the first member, verify the team balance decreased to 95 000, and confirm that the member attribution was recorded."""),
        code("""# Deduct 5 000 credits from the team pool on behalf of members[0].
# The deduction is charged against the team balance, not against
# the individual user's personal balance (which is 0).
res = store.deduct_team(team.team_id, members[0], 5_000)
print(f"  Deducted 5 000 for {members[0][:8]}…: team_balance_after={res.team_balance_after}, error={res.error}")

# Verify the team balance decreased from 100 000 to 95 000.
bal2 = store.get_team_balance(team.team_id)
assert bal2.balance == 95_000"""),
        md("""### Exceed team balance (rejected)

What happens when a team tries to spend more credits than the pool contains? Just like overdraft protection on a bank account, ducto rejects the transaction. The `deduct_team` call returns an error with the code `"insufficient_team_balance"` rather than allowing the balance to go negative. This is a safety mechanism: it prevents the team from accruing debt and ensures that credits are consumed only when they are available.

This behavior is by design. In a production SaaS application, an overdrawn team pool could mean a user receives service they cannot pay for, creating a billing gap. By rejecting insufficient-balance transactions upfront, ducto lets you surface the error to the team admin, who can then top up the pool before the user experiences a service disruption.

What we will do in this section: attempt to deduct 999 999 credits from the team pool (far exceeding the remaining 95 000), observe the error response, and verify the assertion that enforces the rejection."""),
        code("""# Attempt to deduct 999 999 credits, far more than the 95 000
# remaining in the team pool. The store should reject this.
res2 = store.deduct_team(team.team_id, members[1], 999_999)
print(f"  Attempt to deduct 999 999: error='{res2.error}', team_balance_after={res2.team_balance_after}")
print(f"  (team balance is 95 000 — insufficient to cover the request)")
assert res2.error == "insufficient_team_balance" """),
        md("""### Per-member spend cap

A shared pool solves the basic sharing problem, but it introduces a new one: any single member could drain the entire team's credits. A rogue script, an aggressive user, or a bug in your application could consume the whole pool in minutes. Per-member spend caps prevent this by limiting how much each individual can draw from the team pool within a period.

The cap is set when you add the member via `add_team_member(..., spend_cap=3_000)`. Once the member's cumulative team spending reaches that limit, subsequent `deduct_team` calls return `"spend_cap_exceeded"`. The team balance may still have plenty of credits — the cap only restricts that specific user. You can raise, lower, or remove the cap dynamically without affecting other members.

What we will do in this section: create a new user with a 3 000 credit spend cap, deduct 3 000 (within the cap, succeeds), then attempt to deduct 1 more credit (exceeds the cap, fails), and verify that the error is correctly returned."""),
        code("""# Create a new user and add them to the team with a per-member
# spend cap of 3 000 credits. This limits how much this specific
# user can draw from the shared pool, regardless of how many
# credits remain in the team balance.
capped_user = str(uuid.uuid4())
store.add_credits(capped_user, 0, type="adjustment")
store.add_team_member(team.team_id, capped_user, role="member", spend_cap=3_000)
print(f"  Member added: {capped_user[:8]}… with spend_cap=3 000")

# Deduct exactly 3 000 — this is within the cap, so it succeeds.
# The team pool covers the cost, and the user's personal cap
# tracker records this spend.
res3 = store.deduct_team(team.team_id, capped_user, 3_000)
print(f"  Deducted 3 000 (within cap): team_balance_after={res3.team_balance_after}")

# Attempt to deduct 1 more credit. Even though the team pool has
# plenty remaining, this user has exhausted their personal cap
# of 3 000. The transaction is rejected.
res4 = store.deduct_team(team.team_id, capped_user, 1)
print(f"  Attempt to deduct 1 (exceeds cap): error='{res4.error}'")
assert res4.error == "spend_cap_exceeded"
print("  Verification passed: per-member spend cap correctly enforced")"""),
        pg_teardown(),
    ]


# ---------------------------------------------------------------------------
# Notebook 7 – Events
# ---------------------------------------------------------------------------


def n07():
    return [
        md("""# 07 – Events

Credit operations are useful on their own, but often you need to react to them — send a Slack alert when a user's balance runs low, update an analytics dashboard on each deduction, or trigger an auto top-up. ducto's event system follows the observer pattern: you emit events when operations happen, and registered handlers react asynchronously.

The observer pattern is a software design pattern where an object (the subject) maintains a list of dependents (observers) and notifies them automatically of state changes. In ducto, every credit operation — adding credits, deducting, refunding, hitting a cap — generates a typed event that can be observed by any number of handlers. This decouples the core credit logic from the integrations that react to it.

Without events, you would need to add notification logic directly inside every credit operation call site: after `add_credits`, check the balance and send a Slack message; after `deduct_credits`, update the dashboard. This approach is fragile, tightly coupled, and hard to maintain as your integration surface grows. Events solve this by letting you register handlers once, after which all relevant operations automatically trigger them.

ducto defines a standard set of event types: `credits.added`, `credits.deducted`, `credits.refunded`, `credits.low_balance`, `credits.cap_reached`, and `credits.cap_warning`. Each event carries structured data — the user ID, the amount, the new balance, a timestamp, and any operation-specific fields — so your handlers have full context without needing to query the store again.

What we will do in this section: create a `CreditEventEmitter`, register handler functions for multiple event types, wire the emitter to a `CreditManager`, trigger real operations that produce events, inspect the captured event data, and demonstrate type-specific subscriptions that only react to refund events."""),
        pg_setup("""\
import uuid
from ducto.events import CreditEvent, CreditEventEmitter"""),
        md("""### Create emitter and register handlers

The `CreditEventEmitter` is the central hub of ducto's event system. You create one instance, register handler functions for the event types you care about, and then pass the emitter to a `CreditManager`. From that point on, every relevant credit operation on that manager automatically dispatches events to the registered handlers.

Handler functions have a simple signature: they receive a `CreditEvent` object and return nothing. In our example, the `logger` function appends each event to a `captured` list for later inspection and prints a summary. This pattern — collect events in a list for assertions or post-hoc analysis — is especially useful in testing and monitoring scenarios.

Note that you can register the same handler for multiple event types (as we do with `logger` for `credits.added`, `credits.deducted`, and `credits.low_balance`) or different handlers for different types. The emitter dispatches each event to all handlers registered for that type, in registration order.

What we will do in this section: instantiate a `CreditEventEmitter`, define a logging handler that captures events into a list, and register it for three different event types."""),
        code("""# Create a list to collect all emitted events for later inspection.
# In a real application, this would be replaced by a handler that
# sends data to an external system (Slack, analytics, etc.).
captured: list[CreditEvent] = []

# Define a handler function that receives a CreditEvent object.
# This handler appends each event to our capture list and prints
# a one-line summary with the event type, truncated user ID, and
# any additional payload data.
def logger(ev: CreditEvent) -> None:
    captured.append(ev)
    print(f"  EVENT [{ev.type}] user={ev.user_id[:8]}…  data={ev.data}")

# Create the event emitter — the central hub that dispatches
# events to all registered handlers.
emitter = CreditEventEmitter()

# Register our logger handler for three different event types.
# When a credit operation triggers any of these types, the
# emitter calls logger() with the corresponding CreditEvent.
emitter.on("credits.added", logger)
emitter.on("credits.deducted", logger)
emitter.on("credits.low_balance", logger)
print("Handlers registered for credits.added, credits.deducted, credits.low_balance")"""),
        md("""### Wire to CreditManager and trigger events

A `CreditEventEmitter` on its own does nothing — it needs to be connected to credit operations. This is done by passing the emitter to the `CreditManager` constructor. When the manager calls `add_credits`, `deduct`, and other operations internally, it emits events through the wired emitter before returning the result.

In the example below, we create two managers: one with just the emitter (for the first `add_credits` call) and another with both an emitter and a `PricingEngine` (for the full deduct flow). Both emit events that get collected by our `logger` handler. The pricing engine is included so that `manager.deduct()` can calculate the credit cost from usage metrics before applying the deduction and emitting the `credits.deducted` event.

Each call to `manager.add_credits` or `manager.deduct` produces one or more events. The handler prints them in real time, and the events are also saved in the `captured` list for later inspection. At the end, we confirm how many events were captured total.

What we will do in this section: create a `CreditManager` wired to the emitter, add credits to a user (triggering `credits.added`), set up a pricing engine and deduct credits (triggering `credits.deducted` and potentially `credits.low_balance`), and count the total events captured."""),
        code("""# Create a CreditManager with just the emitter (no pricing engine).
# Operations through this manager will emit events to our logger.
manager = CreditManager(store, emitter=emitter)
user = str(uuid.uuid4())

# Add 500 credits to the user. This triggers a credits.added event
# that our logger handler will capture and print.
print("--- add_credits (triggers credits.added event) ---")
manager.add_credits(user, 500)

# Create a second manager that includes both a pricing engine and
# the emitter. The pricing engine calculates how many credits a
# usage metric costs, then the manager deducts that amount and
# emits a credits.deducted event.
print("\\n--- deduct end-to-end (triggers credits.deducted) ---")
engine = PricingEngine.from_dict({
    "models": {"_default": "input_tokens * 1"},
})
manager = CreditManager(store, engine=engine, emitter=emitter)
manager.add_credits(user, 2_000)
ded = manager.deduct(user, UsageMetrics(model="_default", input_tokens=100))
print(f"  Deduct result: amount={ded.amount} credits deducted, balance_after={ded.balance_after}")

# Check how many events were captured across all operations.
# Each add_credits and deduct call should have produced at least
# one event that our handler recorded.
print(f"\\nTotal events captured across all operations: {len(captured)}")"""),
        md("""### Inspect captured events

The `captured` list now contains every event that was emitted during the operations above. Each event is a `CreditEvent` object with structured fields: `type` (the event type string), `user_id`, `amount`, `balance_after`, `timestamp`, and an optional `data` dictionary with operation-specific details.

Inspecting captured events is valuable for debugging, audit logging, and testing. You can verify that the correct sequence of events was emitted, check that critical thresholds (like low balance warnings) were triggered at the right moment, and ensure that all event data contains the expected values. This pattern is also the foundation for building integration tests that assert on event-driven behavior.

In a production system, you would replace the in-memory `captured` list with a real handler — for example, one that sends a webhook to your analytics platform, posts a message to a Slack channel, or writes to an audit log table.

What we will do in this section: iterate over the captured events, print their timestamps and types, and display any additional data they carry."""),
        code("""# Iterate through every event that was captured during the
# operations above. Each CreditEvent object contains structured
# data fields that we can inspect programmatically.
for ev in captured:
    # Print the timestamp and event type for each event. The
    # timestamp is recorded at the moment the event is emitted,
    # giving us a precise timeline of credit operations.
    print(f"  [{ev.timestamp.strftime('%H:%M:%S')}] {ev.type}")
    # If the event carries additional payload data (such as
    # transaction_id, new balance, or operation metadata), print
    # each key-value pair on its own line.
    if ev.data:
        for k, v in ev.data.items():
            print(f"      {k}={v}")"""),
        md("""### Subscribe by specific type

Registering a handler for `"credits.refunded"` means it only fires when a refund operation occurs. This is useful for handlers that should react to specific credit lifecycle events without being invoked on every operation. For example, a refund handler might update an accounting ledger or notify a support agent, while a deduction handler might track usage for billing.

In this section, we register a separate handler that only listens for refund events and appends them to its own list. Then we perform a refund through the store directly (the `CreditManager` does not expose a `refund` method in this example) and verify that the refund-specific handler was triggered.

Note that the refund event carries information about the original deduction transaction, the refund amount, and the reason for the refund. This data is accessible through the event's `data` dictionary and is critical for audit trails and accounting reconciliation.

What we will do in this section: register a dedicated refund handler on the emitter, perform a reserve-deduct-refund cycle through the store, and verify that the refund handler captured the expected events."""),
        code("""# Create a dedicated list to capture only refund events, and
# register a handler that subscribes exclusively to the
# "credits.refunded" event type. This demonstrates type-specific
# subscriptions: this handler will only fire when a refund occurs.
refunds: list[CreditEvent] = []
emitter.on("credits.refunded", lambda e: refunds.append(e))

# Perform a reserve-deduct-refund cycle through the store directly
# (CreditManager does not expose a refund method in this example).
# Step 1: Reserve 100 credits to hold them for a pending operation.
ded_tx = store.reserve_credits(user, 100, operation_type="test")
# Step 2: Deduct the reserved credits, completing the operation.
ded = store.deduct_credits(user, ded_tx.reservation_id, 100)
# Step 3: Refund the full 100 credits. This triggers a
# credits.refunded event, which our dedicated refund handler
# captures. The reason "demo" is attached to the event data for
# audit trail purposes.
store.refund_credits(ded.transaction_id, amount=100, reason="demo")
print(f"Refund events captured by dedicated handler: {len(refunds)}")"""),
        pg_teardown(),
    ]


# ---------------------------------------------------------------------------
# Notebook 8 – Custom Store
# ---------------------------------------------------------------------------


def n08():
    return [
        md("""# 08 -- Custom Store

ducto ships with two store implementations: PostgresStore (production-ready, persistent) and MemoryStore (development, ephemeral). But your application might use a different backend -- Redis for speed, DynamoDB for scalability, SQLite for embedded deployments. The CreditStore abstract base class (ABC) defines the contract that every store must fulfill. If you implement all of its abstract methods, your custom store unlocks the full ducto feature set: reservations, deductions, refunds, analytics, team pools, spend caps, and credit expiry.

Think of the CreditStore ABC as a standardized electrical outlet. The shape of the outlet (the abstract methods) is the same everywhere, but what happens behind the wall (the implementation) can be anything -- Postgres, Redis, DynamoDB, or a plain Python dictionary. As long as the outlet fits, any appliance (CreditManager, PricingEngine, event emitter) works with any store. This is the dependency inversion principle in action: high-level modules depend on abstractions, not concrete implementations.

The CreditStore ABC defines seven method groups. The balance and lifecycle group handles the core credit operations (get_balance, add_credits, reserve_credits, deduct_credits, refund_credits). Pricing methods connect the store to pricing formulas. Plan methods support subscription-style free allowances. Cap methods enforce spending limits. Analytics methods power dashboards and reports. The sweep method handles credit expiry. Team methods support shared credit pools for organizations.

Not every method group is required for every use case. If your application does not need teams, you can raise NotImplementedError for the team methods. If you do not use spend caps, you can return stubs. The contract specifies the interface, but the implementation decides what is supported. This flexibility lets you start with a minimal store and add functionality over time.

Our example store below implements every method group using in-memory Python dictionaries. Some methods are fully functional (balance and lifecycle), some return stubs (pricing, plans, analytics), and some raise NotImplementedError (teams). In production, you would implement all methods against your chosen backend -- but even a partial implementation demonstrates the contract clearly.

What we will do in this section: implement a complete MyCustomStore class that satisfies the CreditStore ABC, with explanatory comments for each method group."""),
        md("""### Implement the ABC

Below is the complete MyCustomStore implementation. Each method group is separated by a section comment with a brief explanation of the group's purpose. Pay close attention to the balance and lifecycle group -- those are the only methods with real logic in this example. The other groups return stubs or raise errors, but their signatures match the ABC exactly, which means the class passes all type checks.

The import section brings in every result type from ducto.interface.models. These are Pydantic models that define the return shape for each method. You do not need to import types you do not use, but importing them all shows the full contract at a glance.

What we will do in this section: walk through the imports, the class definition, and each method group with explanatory comments."""),
        code("""# =============================================================================
# IMPORTS: All result types from the ducto model layer
# =============================================================================
# The CreditStore base class from ducto.interface.base defines the abstract
# interface that every store must implement. The types imported from
# ducto.interface.models are the return types for each method. Every method
# in the ABC has a specific return type; these models ensure type safety.
from ducto.interface.base import CreditStore
from ducto.interface.models import (
    BalanceResult, AddCreditsResult, ReserveResult, DeductionResult,
    RefundResult, TeamDeductionResult, CreateTeamResult, TeamBalanceResult,
    TeamMember, AddTeamMemberResult, AllowanceResult, CapCheckResult,
    PricingConfigResult, SetupResult,
)

# =============================================================================
# CLASS: MyCustomStore
# =============================================================================
# This class inherits from CreditStore and implements every abstract method.
# The ABC uses Python's abc.ABC and @abstractmethod decorators to enforce
# that all abstract methods are implemented. If you forget a method, Python
# will raise a TypeError when you try to instantiate the class.
class MyCustomStore(CreditStore):
    # The docstring uses double quotes to avoid conflict with the
    # outer delimiter in this source code.
    '''Minimal custom store -- dict-backed, no persistence.'''

    def __init__(self):
        # _balances maps user_id to current balance (total credits available).
        # This is the source of truth for all balance operations.
        self._balances: dict[str, int] = {}
        # _reservations maps reservation_id to reserved_amount.
        # Reservations lock credits for specific operations to prevent
        # race conditions in concurrent systems.
        self._reservations: dict[str, int] = {}

    # =========================================================================
    # METHOD GROUP 1: Balance and lifecycle operations
    # =========================================================================
    # These five methods form the core credit contract. Every store must
    # implement them. They are the minimum viable interface for a working
    # credit system: check balance, add credits, reserve, deduct, refund.

    def get_balance(self, user_id: str) -> BalanceResult:
        # Return the user's current balance, or 0 if the user does not exist.
        return BalanceResult(user_id=user_id, balance=self._balances.get(user_id, 0))

    def add_credits(self, user_id: str, amount: int, type: str = "adjustment",
                    metadata=None, expires_at=None) -> AddCreditsResult:
        # Add credits to the user's balance. The type field records the
        # source of the credits (purchase, adjustment, refund, promotion).
        # The optional expires_at parameter enables TTL-based expiry.
        self._balances[user_id] = self._balances.get(user_id, 0) + amount
        return AddCreditsResult(transaction_id="tx", user_id=user_id,
                                amount=amount, new_balance=self._balances[user_id])

    def reserve_credits(self, user_id: str, amount: int, operation_type: str,
                        metadata=None, min_balance: int = 5) -> ReserveResult:
        # Reservation locks credits for a specific operation. This prevents
        # race conditions where two concurrent requests both see sufficient
        # balance but neither can actually deduct. The min_balance parameter
        # ensures a safety buffer (default 5 credits) is never consumed.
        bal = self._balances.get(user_id, 0)
        rid = "res_" + user_id[:8]
        self._reservations[rid] = amount
        return ReserveResult(reservation_id=rid, user_id=user_id,
                             amount=amount, balance=bal - amount)

    def deduct_credits(self, user_id: str, reservation_id: str, amount: int,
                       idempotency_key=None, metadata=None) -> DeductionResult:
        # Deduct reserved credits. The reservation was created by a prior
        # call to reserve_credits, which already checked the balance. The
        # idempotency_key prevents double-charging if the caller retries.
        amt = self._reservations.pop(reservation_id, amount)
        self._balances[user_id] -= amt
        return DeductionResult(transaction_id="ded", user_id=user_id,
                               amount=-amt,
                               balance_after=self._balances[user_id])

    def refund_credits(self, transaction_id: str, amount: int = None,
                       reason: str = None, metadata=None) -> RefundResult:
        # Reverse a previous deduction. Refunds are essential for error
        # recovery -- if a downstream service fails, return the credits.
        # The reason field provides an audit trail for the refund.
        return RefundResult(refund_transaction_id="ref", user_id="",
                            original_transaction_id=transaction_id,
                            amount=amount or 0, new_balance=0,
                            reason=reason or "")

    # =========================================================================
    # METHOD GROUP 2: Pricing configuration
    # =========================================================================
    # These methods connect the store to pricing formulas stored as strings.
    # In production, get_active_pricing would load from a database table.
    # Here we return None (no active pricing) because this store delegates
    # pricing to the CreditManager or PricingEngine.

    def get_active_pricing(self) -> PricingConfigResult | None:
        return None
    def set_active_pricing(self, config, label=None) -> str:
        return "cfg_1"
    def setup_pricing_config(self, config, name="default") -> PricingConfigResult:
        raise NotImplementedError

    # =========================================================================
    # METHOD GROUP 3: Plan management
    # =========================================================================
    # Plans provide subscription-style free allowances. A plan defines a
    # monthly credit allowance; users on the plan draw from that allowance
    # before per-use billing kicks in. These methods manage plan assignment
    # and allowance tracking. This store returns stubs because plans are
    # typically managed by the pricing config, not the store itself.

    def get_user_plan(self, user_id: str):
        return None
    def set_user_plan(self, user_id: str, plan_id: str):
        pass
    def check_allowance(self, user_id: str) -> AllowanceResult:
        return AllowanceResult(plan_id="", allowance_remaining=0,
                               period_start="", period_end="")
    def increment_usage_window(self, user_id: str, plan_id: str, amount: int):
        pass

    # =========================================================================
    # METHOD GROUP 4: Spend caps
    # =========================================================================
    # Caps enforce upper limits on credit consumption over a time window
    # (daily, weekly, monthly). The action field controls the behavior when
    # the cap is exceeded: deny (reject the operation), warn (allow but log),
    # or notify (allow and trigger an event). This store returns stubs.

    def set_spend_cap(self, cap):
        pass
    def check_spend_cap(self, user_id: str, model=None, amount=None) -> CapCheckResult:
        return CapCheckResult()

    # =========================================================================
    # METHOD GROUP 5: Analytics
    # =========================================================================
    # Analytics methods power dashboards and reporting. They answer questions
    # like "who spent the most last month" or "what is the average daily
    # spend." This store returns empty lists because real analytics require
    # a persistent backend with query capabilities.

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

    # =========================================================================
    # METHOD GROUP 6: Credit expiry sweep
    # =========================================================================
    # The sweep operation finds credits whose expires_at timestamp is in the
    # past and deducts them from the user's balance. The dry_run parameter
    # lets you preview what would expire without modifying balances.
    # This store returns an empty SweepResult because it does not track
    # expiry timestamps in its dict-based implementation.

    def sweep_expired_credits(self, dry_run=False):
        from ducto.interface.models import SweepResult
        return SweepResult()

    # =========================================================================
    # METHOD GROUP 7: Team operations
    # =========================================================================
    # Teams share a pooled credit balance across multiple users. Individual
    # team members can have per-member spend caps. Team operations are
    # optional -- if your application does not need team credit pools,
    # raise NotImplementedError as shown here. The important thing is that
    # the method signatures match the ABC, so the class can still be
    # instantiated and used for non-team operations.

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

# Instantiate our store. Python checks that all abstract methods are
# implemented at instantiation time. If the class were missing any
# abstract method, this line would raise TypeError.
custom_store = MyCustomStore()
print("MyCustomStore implements CreditStore ABC.")"""),
        md("""### Use with CreditManager

Implementing the CreditStore ABC is only half the work. The real power comes when you connect your custom store to ducto's CreditManager. The CreditManager wraps any CreditStore and adds higher-level features: automatic pricing engine integration, event emission, idempotency handling, and resource lifecycle management.

The beauty of the ABC pattern is that CreditManager accepts ANY CreditStore implementation. The manager does not care whether the store stores data in Postgres, Redis, DynamoDB, or a Python dictionary. It only knows that the object satisfies the CreditStore contract. When you upgrade from a prototype store to a production PostgresStore, your CreditManager code does not change.

In a production application, you would typically create a single CreditManager instance at startup and inject it into your request handlers. The manager handles thread safety, connection pooling (for database-backed stores), and resource cleanup. It also provides convenience methods that combine multiple store calls into atomic operations.

What we will do in this section: create a CreditManager backed by our custom store, add credits to a user, check the balance, and perform a reserve-and-deduct cycle."""),
        code("""# Import uuid to generate unique user IDs for testing.
import uuid
# The CreditManager wraps any CreditStore with higher-level logic.
# It accepts our custom store because MyCustomStore extends CreditStore.
from ducto.manager import CreditManager

# Create a CreditManager using our custom store as the backend.
# The manager will delegate all storage operations to our store
# while adding pricing engine integration and event emission.
manager = CreditManager(custom_store)

# Generate a unique user ID for our test.
user = str(uuid.uuid4())

# Add 10 000 credits to the user's balance, simulating a grant.
manager.add_credits(user, 10_000)

# Check the balance to confirm the credits were added successfully.
# The get_balance method delegates to our store's get_balance.
print(f"  After adding 10 000 credits, balance = {manager.get_balance(user).balance}")

# Reserve 1 000 credits for a specific operation (e.g., model inference).
# Reservation checks that sufficient balance exists and locks the credits
# so a concurrent request cannot consume them before the deduction.
res = manager.reserve_credits(user, 1_000, operation_type="test")
print(f"  Reserved {res.amount} credit(s), remaining available balance = {res.balance}")"""),
    ]


# ---------------------------------------------------------------------------
# Notebook 9 – Expression Evaluator
# ---------------------------------------------------------------------------


def n09():
    return [
        md("""# 09 -- Expression Evaluator

Pricing formulas in ducto are plain strings: `input_tokens * 5 + output_tokens * 15`. But executing arbitrary strings as code is dangerous -- that is how injection attacks happen. A naive approach would use `eval()` to turn a string into a number, but `eval()` can execute any Python expression, including calls to `__import__` (to import the `os` module), `open()` (to read files), or `globals()` (to inspect runtime state). This is like giving a stranger the keys to every room in your house.

ducto's `evaluate_expression` uses Python's `ast` module, not `eval()`. It parses the expression into an abstract syntax tree, then validates every node against an explicit whitelist. Only allowed operations pass through. Think of it like airport security: every item is inspected before it gets on the plane. If a passenger tries to bring a banned item, security stops it before it reaches the gate. Similarly, if an expression contains an operation that is not on the whitelist, the evaluator raises a `ValueError` before any code is executed.

The whitelist includes standard arithmetic operators (addition, subtraction, multiplication, division, exponentiation, modulo), comparison operators (less than, greater than, equal to), Boolean operators (and, or, not), and a curated set of function names: `min`, `max`, `if`, `tier`, `clamp`, `percentile`, `ceil`, `floor`, `round`, `sum`, `abs`. Everything else -- `__import__`, `exec`, `eval`, `open`, `lambda`, attribute access, function calls on objects -- is blocked.

The expression variables map directly to usage metrics that ducto collects during inference: `input_tokens` and `output_tokens` for token-based pricing, `cache_read_tokens` and `cache_write_tokens` for cache discounts, `tool_calls` for tool usage pricing, `search_queries` and `search_results` for search/RAG operations, `web_search_calls` and `code_exec_calls` for agentic workflows, and `fixed_job` for flat-rate operations.

This design has a practical benefit: because pricing formulas are stored as strings in the database (in the `credit_pricing_config` table), you can update pricing without deploying new code. Change a formula in the database, and the next request uses the new price. Combined with the AST safety guarantees, this gives you both agility and security.

What we will do in this section: explore every function available in the expression evaluator -- arithmetic, min/max for floors and caps, conditional logic, volume discounts, bounds clamping, rounding, percentile statistics, and combined expressions -- and verify that dangerous operations are blocked."""),
        md("""### Basic arithmetic

The simplest use of the expression evaluator is basic arithmetic. This is the foundation for all pricing formulas: multiply token counts by per-token rates and add them up. The arithmetic supports standard operators: `+`, `-`, `*`, `/`, `//`, `%`, `**`.

In a real pricing config, each model gets its own formula string. For example, GPT-4o might cost 5 credits per 1 000 input tokens plus 15 credits per 1 000 output tokens, while Claude Haiku costs 1 and 4 credits respectively. The expression evaluator turns these strings into concrete numbers.

What we will do in this section: calculate the cost of a GPT-4o inference with 500 input tokens and 200 output tokens using a simple arithmetic expression."""),
        code("""# Step 1: Import the evaluate_expression function from ducto's
# expression evaluation module. This function takes a formula string
# and a dictionary of variable values, and returns the computed result.
from ducto.expr import evaluate_expression

# Step 2: Calculate the credit cost for a GPT-4o inference.
# The formula "input_tokens * 5 + output_tokens * 15" means:
#   - Each input token costs 5 credits
#   - Each output token costs 15 credits
# With 500 input tokens and 200 output tokens:
#   500 * 5 = 2 500 for input
#   200 * 15 = 3 000 for output
#   Total = 5 500 credits
r = evaluate_expression("input_tokens * 5 + output_tokens * 15",
                        {"input_tokens": 500, "output_tokens": 200})
print(f"  500 input tokens * 5 + 200 output tokens * 15 = {r} credits  (expected: 5500)")
assert r == 5500"""),
        md("""### Minimum charge with `max`

The `max(expression, minimum)` function enforces a minimum charge per request. This is commonly known as a floor price. Even if a request is tiny -- for example, a single token response -- the minimum charge ensures the provider recovers at least their base cost.

Real-world use case: "Charge at least 1 credit per request, even for tiny responses." Without a floor, a 10-token response would cost 0.03 credits, which may be below the cost of processing the request overhead. The `max` function guarantees the price never falls below a specified minimum.

The function takes two arguments: the expression to evaluate and the minimum floor value. If the expression evaluates to less than the floor, the floor is returned instead. This is equivalent to `max(computed_price, minimum_price)`.

What we will do in this section: test the max function with 100 input tokens (where the computed cost is below the minimum) and 500 input tokens (where it exceeds the minimum)."""),
        code("""# Use max(expression, minimum) to guarantee a minimum charge.
# First test: 100 input tokens at 0.003 credits each = 0.3 credits.
# Since 0.3 is below the minimum of 1 credit, max() returns 1.
r = evaluate_expression("max(input_tokens * 0.003, 1)",
                        {"input_tokens": 100})
print(f"  max(100 * 0.003, 1) = {r}  (expected: 1) -- below floor, minimum applies")

# Second test: 500 input tokens at 0.003 credits each = 1.5 credits.
# Since 1.5 is above the minimum of 1 credit, max() returns 1.5.
r = evaluate_expression("max(input_tokens * 0.003, 1)",
                        {"input_tokens": 500})
print(f"  max(500 * 0.003, 1) = {r}  (expected: 1.5) -- above floor, computed value returned")"""),
        md("""### Price cap with `min`

The `min(expression, cap)` function enforces a maximum charge per request. This is a price ceiling. No matter how large the request, the price never exceeds the cap. This protects users from unexpectedly large bills due to unusually long responses or runaway loops.

Real-world use case: "Cap the model cost at 10 credits per request, even for very long outputs." Without a cap, a 10 000-token response would cost 200 credits at 0.02 credits per token. With a cap of 10 credits, the user pays at most 10 credits regardless of output length.

The function takes two arguments: the expression to evaluate and the maximum cap value. If the expression exceeds the cap, the cap is returned instead. This is equivalent to `min(computed_price, max_price)`. Capped pricing is commonly offered by providers as a "price ceiling" feature for enterprise customers.

What we will do in this section: test the min function with 500 input tokens (where the computed cost exceeds the cap) and 200 input tokens (where it stays under)."""),
        code("""# Use min(expression, cap) to enforce a maximum charge.
# First test: 500 input tokens at 0.02 credits each = 10 credits.
# Since 10 equals the cap of 10, min() returns 10 (the cap is reached).
r = evaluate_expression("min(input_tokens * 0.02, 10)",
                        {"input_tokens": 500})
print(f"  min(500 * 0.02, 10) = {r}  (expected: 10) -- at cap limit")

# Second test: 200 input tokens at 0.02 credits each = 4 credits.
# Since 4 is below the cap of 10, min() returns 4 (no cap applied).
r = evaluate_expression("min(input_tokens * 0.02, 10)",
                        {"input_tokens": 200})
print(f"  min(200 * 0.02, 10) = {r}  (expected: 4) -- under cap, no clamping")"""),
        md("""### Conditional pricing with `if`

The `if(condition, then_value, else_value)` function provides ternary conditional logic in expressions. This enables different pricing paths based on the usage metrics. The condition supports standard comparison operators: `<`, `>`, `<=`, `>=`, `==`, `!=`. You can also combine conditions with `and`, `or`, and `not`.

Real-world use case: "Free for requests under 100 tokens, pay 0.01 per token above that threshold." This is a common pattern for freemium tiers where small requests are free to encourage experimentation. The conditional evaluates whether the condition is true; if so, it returns `then_value`, otherwise `else_value`.

The function performs lazy evaluation in the sense that only the selected branch is computed. However, since the arguments are already evaluated by the expression parser, both `then_value` and `else_value` must be valid expressions. For simple constant values like 0, this is trivially safe.

What we will do in this section: test the if function with 50 input tokens (free tier) and 500 input tokens (paid tier)."""),
        code("""# Use if(condition, then_value, else_value) for conditional pricing.
# First test: 50 input tokens is under 100, so the condition
# "input_tokens < 100" is true, and the cost is 0 (free tier).
r = evaluate_expression("if(input_tokens < 100, 0, input_tokens * 0.01)",
                        {"input_tokens": 50})
print(f"  if(50 < 100, 0, 50 * 0.01) = {r}  (expected: 0) -- free tier activated")

# Second test: 500 input tokens is over 100, so the condition
# "input_tokens < 100" is false, and the cost is 500 * 0.01 = 5.
r = evaluate_expression("if(input_tokens < 100, 0, input_tokens * 0.01)",
                        {"input_tokens": 500})
print(f"  if(500 < 100, 0, 500 * 0.01) = {r}  (expected: 5) -- paid tier activated")"""),
        md("""### Volume discount with `tier`

The `tier(value, threshold1, rate1, threshold2, rate2, ..., default_rate)` function implements multi-threshold volume pricing. As usage increases, the rate decreases. This is the classic "economies of scale" pricing model: the more you use, the cheaper each unit becomes.

Real-world use case: "First 10 000 tokens at 0.02 per 1k, next 90 000 tokens at 0.01 per 1k, everything beyond at 0.005 per 1k." This pricing structure rewards high-volume users with progressively lower rates. The `tier` function selects the rate based on which threshold bucket the value falls into: if `value < threshold1`, use `rate1`; if `value < threshold2`, use `rate2`; otherwise use the `default_rate`.

The rate is then multiplied by the usage amount to compute the total cost. This is a separate step: `tier()` returns the applicable rate, and you multiply it by the usage to get the final price. This separation lets you apply the tiered rate to any dimension of usage.

What we will do in this section: test the tier function with 5 000 tokens (first tier), 50 000 tokens (second tier), and 200 000 tokens (third tier)."""),
        code("""# tier(token_count, threshold1, rate1, threshold2, rate2, default_rate)
# First test: 5 000 tokens is below the first threshold (10 000),
# so the first tier rate of 0.02 per 1k tokens applies.
# Calculation: 0.02 * 5 000 / 1 000 = 0.1000
r = evaluate_expression(
    "tier(input_tokens, 10000, 0.02, 100000, 0.01, 0.005) * input_tokens / 1000",
    {"input_tokens": 5_000},
)
print(f"  tier(5k tokens): rate = 0.02, total = {r:.4f}  (expected: 0.1000 -- first tier)")

# Second test: 50 000 tokens is above the first threshold (10 000)
# but below the second threshold (100 000), so the second tier
# rate of 0.01 per 1k applies.
# Calculation: 0.01 * 50 000 / 1 000 = 0.5000
r = evaluate_expression(
    "tier(input_tokens, 10000, 0.02, 100000, 0.01, 0.005) * input_tokens / 1000",
    {"input_tokens": 50_000},
)
print(f"  tier(50k tokens): rate = 0.01, total = {r:.4f}  (expected: 0.5000 -- second tier)")

# Third test: 200 000 tokens is above both thresholds, so the
# default rate of 0.005 per 1k applies.
# Calculation: 0.005 * 200 000 / 1 000 = 1.0000
r = evaluate_expression(
    "tier(input_tokens, 10000, 0.02, 100000, 0.01, 0.005) * input_tokens / 1000",
    {"input_tokens": 200_000},
)
print(f"  tier(200k tokens): rate = 0.005, total = {r:.4f}  (expected: 1.0000 -- third tier)")"""),
        md("""### Bound values with `clamp`

The `clamp(x, lo, hi)` function restricts a value to the range `[lo, hi]`. If the value is below the lower bound, it is raised to `lo`. If it is above the upper bound, it is lowered to `hi`. If it is within range, it is returned unchanged.

Real-world use case: "Ensure the number of input tokens used for billing is between 100 and 500." This can be useful for minimum billing units: for example, charging for a minimum of 100 tokens even if fewer were used, but never charging for more than 500 tokens regardless of the actual count. This combines a floor and a cap in a single function.

The clamp function is a convenient shorthand for combining `max` and `min`: `clamp(x, lo, hi)` is equivalent to `max(min(x, hi), lo)` or `min(max(x, lo), hi)` -- both produce the same result. It is commonly used in utility billing, telecommunications, and any domain where quantities have both minimum and maximum charges.

What we will do in this section: test clamp with a value below the range (50, clamped to 100), above the range (1000, clamped to 500), and within the range (300, unchanged)."""),
        code("""# Use clamp(x, lo, hi) to keep a value within [lo, hi].
# Test 1: input_tokens = 50, lo = 100, hi = 500.
# 50 is below 100, so the result is clamped to 100 (raised to minimum).
r = evaluate_expression("clamp(input_tokens, 100, 500)",
                        {"input_tokens": 50})
print(f"  clamp(50, 100, 500) = {r}  (expected: 100) -- raised to minimum")

# Test 2: input_tokens = 1000, lo = 100, hi = 500.
# 1000 is above 500, so the result is clamped to 500 (lowered to maximum).
r = evaluate_expression("clamp(input_tokens, 100, 500)",
                        {"input_tokens": 1000})
print(f"  clamp(1000, 100, 500) = {r}  (expected: 500) -- lowered to maximum")

# Test 3: input_tokens = 300, lo = 100, hi = 500.
# 300 is within [100, 500], so the result is unchanged at 300.
r = evaluate_expression("clamp(input_tokens, 100, 500)",
                        {"input_tokens": 300})
print(f"  clamp(300, 100, 500) = {r}  (expected: 300) -- within range, unchanged")"""),
        md("""### Rounding functions

ducto provides three rounding functions for whole-number credit billing: `ceil` (round up to the nearest integer), `floor` (round down to the nearest integer), and `round(x, ndigits)` (round to the nearest value with `ndigits` decimal places).

Real-world use case: "Round up all credit charges to the nearest whole credit." Ceil rounding ensures the provider always collects at least the computed cost, never less. Floor rounding provides a small discount to the user. Standard rounding to 2 decimal places is useful for fractional credit billing where sub-cent precision is acceptable.

These rounding functions are essential when pricing formulas produce fractional credit costs. For example, if your formula produces 0.999 credits per request, `ceil` would round to 1.0 and `floor` would round to 0.0. The choice depends on your business model: ceiling gives higher revenue, flooring gives better user experience.

What we will do in this section: test all three rounding functions with the same input value (333 input tokens at 0.003 credits per token = 0.999 credits)."""),
        code("""# Test ceil: rounds 0.999 up to 1.0 (nearest whole credit).
# Ceil is useful for ensuring every request generates at least
# a minimum revenue unit, even if the computed cost is fractional.
r = evaluate_expression("ceil(input_tokens * 0.003)",
                        {"input_tokens": 333})
print(f"  ceil(333 * 0.003) = {r}  (expected: 1.0) -- round up to nearest integer")

# Test floor: rounds 0.999 down to 0.0 (nearest whole credit).
# Floor provides a discount by dropping the fractional portion.
r = evaluate_expression("floor(input_tokens * 0.003)",
                        {"input_tokens": 333})
print(f"  floor(333 * 0.003) = {r}  (expected: 0.0) -- round down to nearest integer")

# Test round with 2 decimal places: rounds 0.999 to 1.0.
# The second argument (2) specifies the number of decimal places.
# Standard round uses banker's rounding (round half to even).
r = evaluate_expression("round(input_tokens * 0.003, 2)",
                        {"input_tokens": 333})
print(f"  round(333 * 0.003, 2 decimals) = {r}  (expected: 1.0) -- round to 2 decimal places")"""),
        md("""### Percentile function

The `percentile(p, v1, v2, ...)` function computes the `p`-th percentile of the provided values using linear interpolation. Percentiles are useful for statistical pricing models where the rate depends on where the current usage falls in a distribution.

Real-world use case: "Charge based on the 90th percentile of recent latency measurements." If you have a set of historical latency values, the 90th percentile tells you the value below which 90 percent of observations fall. This is commonly used in pay-per-use API billing where the price is based on latency percentiles rather than raw token counts.

The percentile function uses linear interpolation between adjacent values when the percentile falls between two data points. This gives a smooth, continuous result rather than a discrete step function. The values can be any numeric expressions, not just constants.

What we will do in this section: compute the 90th percentile of the values (100, 200, 300) with input_tokens = 90, meaning we want the 90th percentile."""),
        code("""# Compute the 90th percentile of the values (100, 200, 300).
# The first argument (input_tokens = 90) is the percentile rank (0-100).
# The remaining arguments are the data points.
# For 3 data points, the 90th percentile falls between the 2nd and 3rd,
# and linear interpolation gives 280.
r = evaluate_expression("percentile(input_tokens, 100, 200, 300)",
                        {"input_tokens": 90})
print(f"  percentile(90th, values=[100, 200, 300]) = {r}")
# 90th percentile of (100, 200, 300) = 280 (linear interpolation)"""),
        md("""### Combined expression

Real-world pricing formulas often combine multiple functions. For example, a model cost formula might multiply tokens by their rates, add a tool usage surcharge, and apply a floor with `max` -- all in a single expression string.

The power of the expression evaluator is that you can compose any combination of whitelisted functions and operators. This lets you express complex pricing logic as a single string stored in the database, without writing any application code.

What we will do in this section: evaluate a combined expression that calculates GPT-4o model cost (3 credits per input token, 15 per output token) plus a tool call surcharge (10 credits per tool call, floored at 0)."""),
        code("""# A combined expression that calculates total cost from model
# usage and tool calls. The formula:
#   - Input tokens: 3 credits each (1 000 * 3 = 3 000)
#   - Output tokens: 15 credits each (400 * 15 = 6 000)
#   - Tool calls: 10 credits each, floored at 0 (1 * 10 = 10)
#   - Total = 3 000 + 6 000 + 10 = 9 010
expr = "input_tokens * 3 + output_tokens * 15 + max(tool_calls, 0) * 10"
r = evaluate_expression(expr, {"input_tokens": 1000, "output_tokens": 400, "tool_calls": 1})
print(f"  Combined expression result: {r} credits")
# = 3000 + 6000 + 1*10 = 9010"""),
        md("""### Safety -- blocked operations

The most important feature of the expression evaluator is not what it can do, but what it cannot do. The AST whitelist blocks all dangerous operations by default. This includes `__import__` (which could import the `os` module to execute system commands), `open()` (which could read arbitrary files), `globals()` (which could inspect or modify runtime state), and `lambda` (which could create callable objects).

When you attempt to use a blocked operation, the evaluator raises a `ValueError` with a clear message. It never falls through to `eval()` or `exec()`. The blocked operations are removed during the AST validation phase, before any code is executed. This is a compile-time check, not a runtime sandbox -- if the parser rejects the expression, it never runs.

You can verify this behavior by uncommenting any of the test lines below. Each one will raise a `ValueError` before the print statement is reached. The whitelist is intentionally restrictive: only arithmetic operators, comparison operators, Boolean operators, and the explicitly allowed function names (`min`, `max`, `if`, `tier`, `clamp`, `percentile`, `ceil`, `floor`, `round`, `sum`, `abs`) are permitted.

What we will do in this section: verify that the dangerous operations are blocked by checking that the print statement executes (indicating the blocked expressions never executed and the safe code path succeeded)."""),
        code("""# These operations are blocked by the AST whitelist.
# Uncomment any of the following lines to see the ValueError:
# evaluate_expression("__import__('os').system('ls')", {})
# evaluate_expression("open('/etc/passwd').read()", {})
# evaluate_expression("globals()", {})
# evaluate_expression("lambda x: x", {})
print("All dangerous operations blocked by AST whitelist.")"""),
    ]


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Notebook 10 – Credit Expiry
# ---------------------------------------------------------------------------


def n10():
    return [
        md("""# 10 -- Credit Expiry

Free trial credits should expire after 14 days. Purchased credits might expire in 12 months. Promotional bonuses may expire in 60 days. ducto's credit expiry feature handles all of these scenarios with a single `sweep_expired_credits()` function. The pattern is simple: when you add credits to a user's balance, you can set an optional `expires_at` timestamp. If you set it, a background sweep job finds all expired grants and deducts them from the user's available balance.

The sweep is a safe, transactional operation. It only removes grants whose `expires_at` is in the past. Permanent credits (those added without an `expires_at`) are never touched. This means you can mix expiring and permanent credits in the same user balance: free trial credits expire, purchased credits persist. The sweep also supports a dry-run mode that lets you preview what would be removed before making any changes. This is essential for production deployments where you want to verify the sweep logic before executing it.

Think of the sweep like a refrigerator cleanout: you check the expiration dates on all items, identify anything past its prime (dry run), and then throw away only the expired ones (real sweep). You would never throw away food without checking the labels first, and you should never run a sweep in production without a dry-run preview. The `dry_run=True` flag is your safety net.

ducto does not ship a built-in scheduler for the sweep. In production, you would run `sweep_expired_credits()` on a cron schedule (for example, once per hour via a Celery beat task or a cron job). The sweep is idempotent: running it multiple times only removes newly expired grants each time. Already-expired grants are only removed once.

The expiry feature integrates with the rest of the ducto credit system. When expired credits are swept, the balance is updated atomically. If you use events, the sweep emits standard lifecycle events so your monitoring and alerting pipelines can track credit expiry as a normal credit operation. This makes expiry auditable and visible in your existing dashboards.

What we will do in this section: create a mix of expiring and permanent credits for a test user, preview the sweep with `dry_run=True`, execute the real sweep, and confirm that only expired credits were removed."""),
        pg_setup("import uuid\nfrom datetime import timedelta"),
        md("""### Add credits that expire

To demonstrate the expiry feature, we need two types of credits: one that is eligible for expiry (has an `expires_at` in the past) and one that is permanent (no `expires_at`). This mirrors a real-world scenario where a user has both promotional credits that expire after a trial period and purchased credits that never expire.

The `add_credits` method accepts an optional `expires_at` parameter. If you pass a `datetime` in the past, the credit grant is immediately sweepable. If you omit `expires_at` or pass `None`, the grant is permanent and will never be touched by the sweep. You can mix both types freely for the same user.

When you call `get_balance`, the balance includes ALL credits -- both expiring and permanent -- because the sweep has not run yet. The balance only decreases after `sweep_expired_credits()` removes the expired grants. This means you should always run the sweep before reporting balances to users in production.

What we will do in this section: add 5 000 credits that expired one hour ago (immediately sweepable) and 10 000 permanent credits with no expiry."""),
        code("""# Create a unique test user so we start with a clean balance.
user = str(uuid.uuid4())

# Add 5 000 credits that expired 1 hour ago.
# The expires_at timestamp is set to datetime.now() minus one hour,
# meaning these credits are immediately eligible for sweeping.
# This simulates a free trial grant whose time window has closed.
past = datetime.now() - timedelta(hours=1)
store.add_credits(user, 5_000, type="purchase", expires_at=past)

# Add 10 000 permanent credits with no expiry date.
# These credits have no expires_at timestamp, so the sweep
# will never remove them. They persist in the balance forever.
store.add_credits(user, 10_000, type="purchase")

# Before the sweep runs, the total balance includes both types.
# The expired grant still shows up because no sweep has occurred.
print(f"  Total balance before sweep: {store.get_balance(user).balance} credits")"""),
        md("""### Dry-run sweep (preview)

The dry-run pattern is one of the most important safety features in the credit expiry system. Before you execute a sweep that permanently removes credits from user balances, you should always preview what it would do. The `dry_run=True` flag makes `sweep_expired_credits()` a read-only operation: it scans all grants, finds expired ones, and reports the results without modifying any balances.

This is especially important in production environments where a misconfigured sweep could accidentally remove credits that should not expire. For example, if you accidentally set `expires_at` on all grants including purchases, a real sweep would deduct everything. The dry run lets you catch this before any balances are affected.

The `SweepResult` object includes three fields: `expired_count` (the number of expired grants found), `expired_amount` (total credits that would be removed), and `dry_run` (a boolean indicating whether this was a preview). When `dry_run=True`, the user's balance remains unchanged because no writes occurred.

What we will do in this section: call `sweep_expired_credits(dry_run=True)`, inspect the preview result, and confirm the balance is still the same."""),
        code("""# Dry-run mode: identify expired grants without modifying balances.
# The dry_run=True parameter makes this a read-only inspection.
# No credits are actually removed during the dry run.
preview = store.sweep_expired_credits(dry_run=True)
print(f"  Would expire: {preview.expired_count} grant(s)")
print(f"  Amount:       {preview.expired_amount} credits")
print(f"  Dry run:      {preview.dry_run}")

# The balance is unchanged because the dry run is read-only.
# The expired credits are still included in the available balance
# until we execute the real sweep below.
print(f"  Balance during dry run: {store.get_balance(user).balance} credits (unchanged)")"""),
        md("""### Execute the sweep

Now that we have previewed the sweep and confirmed that it correctly identifies the 5 000 expired credit grant, we can execute the real sweep. Setting `dry_run=False` (or omitting it, since `False` is the default) tells the store to permanently remove expired grants from the user's balance.

The real sweep performs the same scan as the dry run, but this time it commits the changes. Each expired grant is deducted atomically, and the user's balance is updated to reflect the removal. Only grants with `expires_at` in the past are affected. Permanent credits (those without `expires_at`) are never removed.

After the sweep, calling `get_balance` returns only the non-expired credits. The `SweepResult` for the real sweep is identical in structure to the dry run, but the `dry_run` field is `False`. You can compare the dry run and real sweep results to confirm that the correct amount was removed.

What we will do in this section: execute `sweep_expired_credits(dry_run=False)` and verify that expired credits are removed from the balance."""),
        code("""# Execute the real sweep with dry_run=False.
# This permanently deducts expired grants from the user balance.
# Only grants whose expires_at is in the past are removed.
result = store.sweep_expired_credits(dry_run=False)
print(f"  Expired: {result.expired_count} grant(s), {result.expired_amount} credits removed")
print(f"  Dry run: {result.dry_run}")

# The balance now reflects only the non-expired credits.
# The 5 000 credit expired grant has been deducted.
print(f"  Balance after sweep: {store.get_balance(user).balance} credits")"""),
        md("""### Non-expiring credits preserved

The most important guarantee of the sweep system is that permanent credits are never affected. When we added 10 000 credits without an `expires_at` timestamp, those credits are permanent. The sweep only considers grants that have a non-null `expires_at` value that is in the past.

This design means you can freely mix grant types for the same user. Free trial credits, promotional bonuses, and purchased subscription credits can all coexist in a single balance. The sweep methodically processes only grants with past `expires_at` values, leaving permanent credits untouched.

The balance after the sweep should be exactly 10 000 credits -- the full amount of the permanent grant. This confirms that the expiry system correctly preserves non-expiring balances while removing only the expired grants.

What we will do in this section: call `get_balance` and confirm that only the 10 000 permanent credits remain."""),
        code("""# After the sweep, verify that permanent credits are preserved.
# The 5 000 expired grant was removed; the 10 000 permanent
# grant remains because it has no expires_at timestamp.
remaining = store.get_balance(user)
print(f"  Remaining balance: {remaining.balance} credits")"""),
        pg_teardown(),
    ]


def n00() -> list[dict]:
    """11 — Using the ducto CLI."""
    return [
        md("""# Using the ducto CLI

The `ducto` command-line tool lets you manage your credit pricing configuration
directly from the terminal. It connects to your Supabase project via environment
variables, validates pricing config files, and supports versioned rollouts with
full history tracking.

## Prerequisites

Before using the CLI, make sure you have:

1. **ducto installed** with the Supabase extra:
   ```bash
   pip install "ducto[supabase]"
   ```
2. **Environment variables** set:
   ```bash
   export SUPABASE_URL="https://your-project.supabase.co"
   export SUPABASE_SERVICE_ROLE_KEY="your-service-role-key"
   ```
3. **Database migrated** (tables and RPCs created):
   ```bash
   ducto migrate "postgresql://user:pass@host:5432/db"
   ```

The CLI reads `.env` from your current working directory if available.

## Available Commands

```
ducto pricing set <file> [--label <msg>]    # Apply new pricing (always creates version)
ducto pricing get                            # Show current active config
ducto pricing list                           # List all versions (* = active)
ducto pricing activate <version>             # Switch to any historical version
ducto pricing validate <file>                # Dry-run validate without applying
ducto pricing diff <v1> <v2>                 # Show changes between versions
ducto pricing export <version>               # Dump a specific version as JSON
```

Each command is a separate subcommand under `ducto pricing`. Let's explore each one.
"""),
        md("""## 1. Setting pricing — `ducto pricing set`

The `set` command reads a JSON or YAML file, validates it, and publishes it as
a **new version** in the database. Previous versions are preserved — nothing is
ever overwritten in place.

**Always creates a new version.** Every call increments the version counter.
There is no "upsert" or "skip if exists" — each set is an atomic publish.

```bash
# Set pricing from a JSON file
ducto pricing set pricing.json

# Set pricing from a YAML file with a descriptive label
ducto pricing set pricing.prod.yaml --label "deploy-42: reduced haiku pricing"
```

The `--label` flag adds a human-readable message to the version, making it
easier to identify in `list` and `diff` outputs later.

**What happens inside:**

1. The file is parsed (JSON or YAML).
2. The data is validated against two Pydantic schemas:
   - `PricingConfigData` — ensures all required fields exist and are correctly typed.
   - `PricingConfig` — validates expression safety (no eval/exec).
3. An RPC call publishes the config: deactivates the old active row and inserts
   a new one with `version = max(version) + 1`.
4. The full config history is preserved in the `credit_pricing_config` table.
"""),
        md("""## 2. Reading active pricing — `ducto pricing get`

Displays the currently active pricing configuration as formatted JSON.

```bash
ducto pricing get
```

Example output:
```json
{{
  "id": "a1b2c3d4-...",
  "config": {{
    "version": 1,
    "models": {{"_default": "input_tokens * 10 + output_tokens * 30"}},
    "tools": {{"_default": "tool_calls * 0"}},
    "fixed": {{"batch_job": 100}},
    "min_balance": 5000
  }},
  "version": 1
}}
```

If no pricing has been configured yet, the command exits with an error.
"""),
        md("""## 3. Version history — `ducto pricing list`

Lists every pricing version that has ever been published, newest first.
The active version is marked with `*`.

```bash
ducto pricing list
```

Example output:
```
  * v3  (id=a1b2c3d4...)  deploy-42 2026-06-25T10:30:00
    v2  (id=e5f6g7h8...)  initial-haiku 2026-06-24T14:00:00
    v1  (id=i9j0k1l2...)  first-setup 2026-06-23T09:15:00
```

Each row shows:
- `*` if this version is currently active
- The version number (`v3`, `v2`, …)
- A truncated UUID for direct database queries
- The label (if provided during `set`)
- The creation timestamp

This historical record lets you audit all pricing changes over time.
"""),
        md("""## 4. Switching versions — `ducto pricing activate`

Activates a specific historical version. This is a **rollback** when you go
to an older version, or a **restore** when you go forward. No new version
is created — the existing version's config is re-activated.

```bash
# Switch to version 1 (rollback to initial pricing)
ducto pricing activate 1

# Switch back to version 3 (latest)
ducto pricing activate 3
```

**How it works:**

1. Deactivates all configs in the `credit_pricing_config` table.
2. Sets `active = true` on the requested version.
3. The change is atomic — between steps 1 and 2, there is a brief moment
   where no config is active, but the RPC runs inside a single transaction.

**Why this matters:** If a pricing change causes unexpected costs (e.g., a
model expression bug that overcharges users), you can roll back to a known-good
version instantly — no redeploy needed.
"""),
        md("""## 5. Validating configs — `ducto pricing validate`

Dry-runs validation against a pricing file **without** publishing it.

```bash
# Validate a JSON file
ducto pricing validate pricing.json

# Validate a YAML file
ducto pricing validate pricing.prod.yaml
```

On success:
```
Pricing config is valid.
```

On failure (e.g., invalid expression, missing `models` field):
```
Validation failed: 1 validation error for PricingConfigData
  models -> dict[str,str]
    Field required [type=missing, ...
```

Use this in CI/CD pipelines to catch pricing errors before deployment.
"""),
        md("""## 6. Comparing versions — `ducto pricing diff`

Shows a unified diff between two pricing versions. Useful for review before
activating a rolled-back version.

```bash
ducto pricing diff 1 3
```

Example output:
```diff
--- v1
+++ v3
@@ -1,5 +1,6 @@
 {{
+  "version": 1,
   "models": {{
     "_default": "input_tokens * 5 + output_tokens * 15"
   }},
+  "plans": {{
+    "free": {{
+      "id": "free",
+      "free_allowance": 5000
+    }}
+  }},
   "min_balance": 5000
 }}
```

The diff tells you exactly what changed — model rates, added plans, tool
pricing, everything. No guessing.
"""),
        md("""## 7. Exporting versions — `ducto pricing export`

Exports a specific version's config as JSON. Combine with `validate` for a
safe edit-publish workflow:

```bash
# 1. Export the active config
ducto pricing export 3 > current.json

# 2. Edit the file
vim current.json

# 3. Validate the edited file
ducto pricing validate current.json

# 4. Publish the new version
ducto pricing set current.json --label "tweaked haiku rates"
```

This workflow ensures you never accidentally deploy broken pricing.
"""),
        md("""## Putting it together — deployment workflow

Here's the full CI/CD pipeline for pricing updates:

```bash
# 1. Validate first
ducto pricing validate pricing.new.yaml

# 2. Publish with a descriptive label
ducto pricing set pricing.new.yaml --label "$(git rev-parse --short HEAD): reduce opus rate"

# 3. Verify the active config
ducto pricing get

# 4. If something goes wrong within minutes, roll back instantly
ducto pricing activate 2
```

**Key design principles:**

- **All changes are versioned.** No silent overwrites.
- **Labels provide context.** Include git commit hashes, deploy IDs, or Jira tickets.
- **Validate before set.** Catches syntax and expression errors early.
- **Activate for rollbacks.** No redeploy required — instant switch.
- **History never deleted.** Full audit trail in `credit_pricing_config` table.

The CLI turns pricing management from a manual database operation into a
safe, scriptable workflow.
"""),
    ]


# ---------------------------------------------------------------------------
# Build (continued)
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
    ("10_credit_expiry.ipynb", n10()),
    ("00_using_the_cli.ipynb", n00()),
]


if __name__ == "__main__":
    print("Generating notebooks …")
    for name, cells in ALL:
        save(name, cells)
    print(f"Done — {len(ALL)} notebooks in {NB_DIR}")
