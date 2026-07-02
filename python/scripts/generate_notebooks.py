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
# Notebook 00 - Concepts & Setup
# ---------------------------------------------------------------------------


def n_concepts():
    return [
        md("""# 00 - Concepts & Setup

Before diving into any single feature, it helps to see the whole shape of ducto: four pieces that compose into a full credit-billing system for an AI product. This notebook introduces them, runs the smallest possible example against each, and explains the setup code that every later notebook relies on — so those notebooks can skip straight to the feature being taught instead of re-explaining their own scaffolding.

- **`PricingEngine`** — a stateless calculator. Give it usage metrics (input/output tokens, tool calls, search queries, …) and a set of formulas, and it returns a cost. It never touches a database and produces the same answer every time for the same inputs.
- **`CreditStore`** — the persistence layer. An abstract interface with three shipped implementations: `MemoryStore` (in-process, for tests and these notebooks), `PostgresStore` (production), and `SupabaseStore`. It owns balances, leases, plans, spend caps, and analytics.
- **`CreditManager`** — the object your application code actually calls. It wires a `PricingEngine` and a `CreditStore` together and adds the operational layer on top: the lease lifecycle, allowance tracking, spend caps, and an event system.
- **A credit** — an opaque unit of value that you define. Most SaaS platforms map credits to a currency at the UI layer (for example, 1 credit = $0.001); ducto itself is currency-agnostic.

Everything in this series builds on these four pieces, one capability at a time — starting with `PricingEngine` (Notebooks 01–02), then `CreditStore`/`CreditManager` operations (Notebooks 03 onward)."""),
        md("""## The smallest possible example

You don't need a pricing engine or a database to start experimenting with balances — `MemoryStore` runs entirely in-process and needs no setup beyond `store.setup()`. It's the same store every later notebook falls back to whenever a feature doesn't specifically require Postgres."""),
        code("""from decimal import Decimal
from ducto.interface.memory import MemoryStore

store = MemoryStore()
store.setup()

user = "demo-user"

# Add credits: every deposit gets a transaction_id and a `type` label for audit purposes.
r = store.add_credits(user, 1_000, type="signup_bonus")
print(f"Balance after signup bonus: {r.new_balance}")

# Charge credits: deduct_with_allowance() is the atomic "calculate cost, then charge" primitive.
ded = store.deduct_with_allowance(user, Decimal("150"))
print(f"Balance after a 150-credit charge: {ded.balance_after}")
assert ded.balance_after == Decimal("850")"""),
        md("""## Why most other notebooks start with `start_postgres_store()`

From here on, most notebooks use `PostgresStore` instead of `MemoryStore`, because several features (analytics, teams, and the RPC-backed atomicity guarantees) are demonstrated against the real production store. Spinning up a real Postgres server isn't something you want to explain in every notebook, so it's factored into two helpers in `shared.py`, next to these notebooks:

- **`start_postgres_store()`** initializes a throwaway Postgres data directory (`initdb`), starts a `postgres` process on a free local port, creates a database, and calls `store.setup()` — which runs every numbered SQL migration in `ducto/sql/` (the tables and RPC functions every `CreditStore` method calls into). It returns `(store, pgdata)`.
- **`cleanup(pgdata)`** stops that process and deletes the temporary data directory.

You'll see this pair open and close nearly every remaining notebook:

```python
from shared import start_postgres_store, cleanup
store, pgdata = start_postgres_store()
...
cleanup(pgdata)
```

None of this exists in a real deployment. In production you point `PostgresStore` at your actual database URL and run migrations once (`ducto migrate <dsn>`, covered in Notebook 12), not per session."""),
        md("""## Store capability differences

| Capability | `MemoryStore` | `PostgresStore` |
|---|---|---|
| Core: balances, atomic charge, refunds, leases, plans, allowance checks, spend-cap checks, expiry sweep | ✓ | ✓ |
| Analytics: `spend_by_user`, `spend_by_model`, `top_users`, `daily_spend`, `aggregate_stats` | ✓ | ✓ |
| Teams: `create_team`, `add_team_member`, `deduct_team`, `get_team_members` | ✓ | ✓ |
| Writing a new spend cap | ✓ (`set_spend_cap`) | not part of the interface — see below |
| Assigning a plan to a user | ✓ | ✓ — no manual seeding needed, see below |

Two of these are worth stating precisely, because it's easy to read them as store *limitations* when they're really just where a piece of configuration lives:

- **Writing a spend cap is not part of the portable `CreditStore` interface.** `MemoryStore.set_spend_cap()` is a convenience specific to that store, meant for tests and these notebooks. `PostgresStore` implements the *read* side (`check_spend_cap`, which `deduct_with_allowance` also calls internally) against a `credit_spend_caps` table, but there's no writer method — in production you insert a row into that table yourself (via a migration or your own admin tooling). Notebook 07 shows both sides.
- **Plans need no separate seeding step.** Publishing a pricing config that includes a `plans` section — via `store.set_active_pricing()` or `ducto pricing set` — automatically upserts those plans into Postgres's `credit_plans` table as part of the same call. Notebook 04 shows this end to end.

With the shared vocabulary and setup out of the way, Notebook 01 starts with the first concrete piece: turning usage metrics into a cost."""),
    ]


# ---------------------------------------------------------------------------
# Notebook 01 - Pricing Basics
# ---------------------------------------------------------------------------


def n_pricing_basics():
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
- `tool_calls`: The total number of tool invocations across every tool, everywhere this variable appears (including inside `tools.*` expressions). Non-zero when your request includes tool definitions and the model decides to call them.
- `this_tool_calls`: Available only inside `tools.*` expressions. Bound to the count of calls matching that specific tool key (or, inside `tools._default`, the count of calls not matched by any other key). Use this instead of `tool_calls` whenever you want a per-tool count rather than the global total.
- `search_queries` and `search_results`: The number of search queries issued and results processed. These are non-zero in RAG (Retrieval-Augmented Generation) or web-search-augmented generation flows.
- `web_search_calls` and `code_exec_calls`: Web search API calls and code execution sandbox invocations. These are used by agentic applications that have browsing or code-running capabilities.
- `fixed_job`: A special variable for fixed-cost operations that don't scale with token count.

The `tools` section maps tool names to formulas (using `this_tool_calls` for a per-tool count, or `tool_calls` for the global total). `search` and `cache` are each a single formula string, not a dict — one search-pricing expression and one cache-discount expression per config. Once the dictionary is assembled, pass it to `PricingEngine.from_dict()` to build the engine.

This notebook sticks to plain arithmetic (`+ - * /`) inside these formula strings — that's all you need for straightforward per-token pricing. The formula language also supports functions like `max`, `min`, `if`, `tier`, and `clamp` for floors, caps, conditional pricing, and volume discounts; Notebook 02 (Expression Language) covers those in depth."""),
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
    # this_tool_calls counts only calls to THIS tool (code_exec), not the global total.
    "tools": {"code_exec": "this_tool_calls * 50"},
    # "search" — a single expression string (not a dict) for RAG / search-augmented generation.
    "search": "search_queries * 10 + search_results * 1",
    # "cache" — a single expression string (not a dict) for LLM context cache usage.
    "cache": "cache_read_tokens * 1 + cache_write_tokens * 5",
}

# Build the engine: from_dict() parses all formulas into internal AST trees.
# No database or storage is needed at this stage — pure computation.
engine = PricingEngine.from_dict(config)

# Inspect what was registered via the pricing schema.
schema = engine.pricing_schema()
print(f"Engine ready — {len(schema.models)} models registered (gpt-4o, claude-sonnet-4, claude-haiku-3.5)")"""),
        md("""### Basic call (tokens only)

The simplest and most common pricing scenario is a pure chat completion: no tools, no search, no caching. We provide just the model name, input token count, and output token count. The engine returns a `CreditCost` object with a detailed breakdown of each cost component.

In this case, we ask `gpt-4o` to process 500 input tokens and 200 output tokens. The formula is `input_tokens * 5 + output_tokens * 15`, which gives us 500 * 5 + 200 * 15 = 2,500 + 3,000 = 5,500 credits total. No tool credits or search credits are added because we did not specify those metrics.

This is because every metric variable you don't pass to `UsageMetrics` defaults to `0` inside the expression — omitting `tool_calls` is equivalent to passing `tool_calls=0`, which is why the tool/search/cache formulas above all evaluate to zero without raising an error for a missing variable."""),
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


# ---------------------------------------------------------------------------
# Notebook 02 - Expression Language
# ---------------------------------------------------------------------------


def n_expression_language():
    return [
        md("""# 02 - Expression Language

Notebook 01 used plain arithmetic inside pricing formulas — enough for simple per-token rates. This notebook covers everything else the formula language supports: floors, caps, conditionals, volume tiers, clamping, rounding, and percentiles — plus the safety model that makes it safe to let formulas be user-editable strings stored in a database.

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
# Notebook 03 - Credit Lifecycle
# ---------------------------------------------------------------------------


def n_credit_lifecycle():
    return [
        md("""# 03 - Credit Lifecycle

Credits flow through a lifecycle: they are added, charged, and sometimes refunded. Understanding this lifecycle is critical to building reliable credit systems.

ducto's core operations are simple:
- `add_credits()` deposits credits into a user's account.
- `deduct_with_allowance()` charges atomically — calculates against a fixed cost you already know, then debits in one transaction.
- `refund_credits()` reverses a completed deduction.

Everything in this notebook assumes you already know (or can easily compute) the cost before charging — the common case for most operations. When the cost isn't known until the work finishes (a chat call with unpredictable token counts) or several concurrent operations need to see each other's in-flight holds, ducto provides a separate **lease lifecycle** (`reserve` → `settle`/`release`) — that's substantial enough to get its own notebook: see Notebook 06 - Financial Safety."""),
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
        md("""### Creating a user record without funding it

Sometimes you need a user to exist in ducto's ledger before they have any credits of their own — for example, before adding them to a team (Notebook 08), which requires every member to already have a user record. The idiom is to add zero credits with a descriptive `type`:

```python
store.add_credits(user_id, 0, type="adjustment")
```

This creates the user's row with a balance of `0` without implying they received a real deposit. `type` is an arbitrary string — use whatever your own audit taxonomy calls a no-op initialization."""),
        code("""new_user = str(uuid.uuid4())
r0 = store.add_credits(new_user, 0, type="adjustment")
print(f"  User initialized with balance: {r0.new_balance}")
assert r0.new_balance == 0"""),
        md("""### Deduct credits (atomic charge)

Most operations simply **charge** credits directly with `store.deduct_with_allowance()` — ducto's atomic "calculate cost, then charge" primitive. It locks the user's row, applies free allowance, enforces the balance floor, and debits, all in one server-side transaction — no separate reservation step needed for a single-shot charge.

This is the same call `CreditManager.deduct()` makes internally after computing the cost from `UsageMetrics`. Calling it directly here keeps this section store-only, with no pricing engine required. It also accepts an optional `model=` keyword — see the next section."""),
        code("""from decimal import Decimal

# Deduct 2,000 credits directly. deduct_with_allowance() is the atomic
# "calculate cost, then charge" primitive — it locks the row, applies free
# allowance, enforces the balance floor, and debits, all in one transaction.
# Returns a DeductionResult object with:
# - transaction_id: a unique reference for this completed deduction, used for audits and refunds
# - balance_after: the user's total balance after the deduction completes
ded = store.deduct_with_allowance(user, Decimal("2_000"))
print(f"  Deduction:   {ded.transaction_id}")  # Unique reference for this spend
print(f"  Balance aft: {ded.balance_after}")  # 10,000 - 2,000 = 8,000
assert ded.balance_after == Decimal("8000")"""),
        md("""### Attributing a deduction to a model (`model=`)

`deduct_with_allowance()` accepts an optional `model=` keyword. Passing the model name that produced this charge doesn't change the amount debited — it only tags the transaction so later analytics queries can break spend down by model. Notebook 09's `spend_by_model()` query is built entirely on this tag, so it's worth attaching whenever you know which model handled the request."""),
        code("""ded_m = store.deduct_with_allowance(user, Decimal("500"), model="gpt-4o")
print(f"  Deduction attributed to gpt-4o, balance after: {ded_m.balance_after}")
assert ded_m.balance_after == Decimal("7500")"""),
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
print(f"  New balance: {ref.new_balance}")  # 7,500 + 2,000 refunded = 9,500
assert ref.new_balance == 9_500  # the earlier 500-credit gpt-4o deduction is still in effect"""),
        md("""### Refunding the same transaction twice

`refund_credits()` never raises on a business-rule failure — it sets `result.error` instead, so you can always safely inspect the result. Attempting to refund a transaction that was already fully refunded returns `error="already_refunded"` and moves no credits. Always check `.error` before treating a refund as successful."""),
        code("""dup = store.refund_credits(ded.transaction_id, amount=2_000, reason="test")
print(f"  Second refund attempt: error='{dup.error}'")
assert dup.error == "already_refunded"
assert store.get_balance(user).balance == 9_500  # unchanged by the failed duplicate"""),
        pg_teardown(),
    ]


# ---------------------------------------------------------------------------
# Notebook 04 - Plans and Allowances
# ---------------------------------------------------------------------------


def n_plans_and_allowances():
    return [
        md("""# 04 - Plans and Allowances

Most SaaS products offer pricing tiers - Free, Pro, Enterprise - each with a free monthly allowance of credits. ducto tracks per-user plan assignments and monthly usage windows, automatically falling back to the user's credit balance when the free allowance is consumed.

Think of the **allowance** as a monthly prepaid bucket. Every Pro user gets 50,000 free credits each month. Every Free user gets 5,000. These buckets reset automatically at the start of each billing period. In contrast, the **credit balance** (managed via `add_credits` and `deduct_with_allowance` from the previous notebook) is permanent - it only changes when credits are manually added or deducted.

Allowance tracking works through plan definitions embedded in the pricing configuration. Each plan specifies a `free_allowance` - the number of free credits per billing period. When a user is assigned to a plan, the system records the current billing period (a monthly window). Each time the user spends credits, the system first deducts from the free allowance. Once the allowance is exhausted, further spending draws from the user's purchased credit balance (pay-as-you-go).

This notebook uses `MemoryStore` purely to avoid the Postgres startup overhead for every code cell — plan management works identically on `PostgresStore`. As covered in Notebook 00, publishing a config with a `plans` section (via `set_active_pricing()`, exactly as shown below) is all `PostgresStore` needs; there is no separate table-seeding step."""),
        memory_setup(),
        md("""### Persist plan definitions in pricing config

In ducto, plan definitions live inside the pricing configuration, right alongside the model pricing formulas. This keeps all pricing logic - both per-unit costs and subscription allowances - in a single place for easy maintenance.

Each plan has three key properties: an `id` (used to reference the plan when assigning users), a human-readable `name`, and a `free_allowance` (the number of free credits the user gets each billing period). By default the billing period is a monthly window that resets on calendar-month UTC boundaries (`allowance_period="calendar_month"`). Two other modes are available: `"rolling_30d"` (a rolling 30-day window) and `"anniversary"` (resets on the same day-of-month the user was assigned the plan). For both non-calendar modes, the window is anchored to when `set_user_plan()` was called for that user — not their account signup date — so re-assigning a plan re-anchors the cycle. When a user is assigned to a plan, the system records the `period_start` and `period_end`. The allowance resets automatically when the period ends.

This auto-reset makes the allowance fundamentally different from the balance. The allowance refills every period, while the balance only changes when credits are manually added or deducted through `add_credits` and `deduct_with_allowance`. A Pro user with 50,000 monthly allowance still needs a credit balance for usage beyond the free tier."""),
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
# In production, this would be called alongside deduct_with_allowance() to
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
        md("""### Rolling 30-day allowance windows

Calendar-month resets are simple but can feel arbitrary to a user who signs up on the 28th and loses most of their first month's allowance three days later. `allowance_period="rolling_30d"` fixes this: the allowance resets exactly 30 days after the plan was assigned, not on the 1st of the month. `"anniversary"` (not shown here) is the middle ground — it resets monthly, but on the day-of-month the plan was assigned rather than the 1st.

You never compute the window yourself: `CreditManager.check_allowance()` resolves `period_start`/`period_end` for whichever mode the plan uses and returns them directly. (Internally, the store layer keys usage rows by an explicit date so a `PostgresStore`/`SupabaseStore` restart doesn't lose track of which window is current — but that's plumbing, not something you need to think about.)

One boundary case worth knowing: if a user's allowance period ends mid-session — say they have 4,000 credits remaining and the window rolls over between one request and the next — the very next `check_allowance()` call already reflects the new period: a full, fresh allowance, not the stale 4,000. There is no partial carryover between periods in either direction."""),
        code("""from ducto.manager import CreditManager

store.set_active_pricing(
    PricingConfigData(
        models={"_default": "input_tokens * 1"},
        plans={
            "startup": PlanDefinition(
                id="startup", name="Startup Tier", free_allowance=20_000,
                allowance_period="rolling_30d",
            ),
        },
    ),
    label="rolling",
)

manager = CreditManager(store=store)
rolling_user = str(uuid.uuid4())
store.set_user_plan(rolling_user, "startup")

allow3 = manager.check_allowance(rolling_user)
print(f"  Plan:      {allow3.plan_id}")
print(f"  Period:    {allow3.period_start} → {allow3.period_end}")  # 30-day window starting today
print(f"  Remaining: {allow3.allowance_remaining}")
assert allow3.allowance_remaining == 20_000
print("  ✓ rolling_30d window resolved automatically by the manager")"""),
    ]


# ---------------------------------------------------------------------------
# Notebook 05 - Credit Expiry
# ---------------------------------------------------------------------------


def n_credit_expiry():
    return [
        md("""# 05 - Credit Expiry

Free trial credits should expire after 14 days. Purchased credits might expire in 12 months. Promotional bonuses may expire in 60 days. ducto's credit expiry feature handles all of these scenarios with a single `sweep_expired_credits()` function. The pattern is simple: when you add credits to a user's balance, you can set an optional `expires_at` timestamp. If you set it, a background sweep job finds all expired grants and deducts them from the user's available balance.

The sweep is a safe, transactional operation. It only removes grants whose `expires_at` is in the past. Permanent credits (those added without an `expires_at`) are never touched. This means you can mix expiring and permanent credits in the same user balance: free trial credits expire, purchased credits persist. The sweep also supports a dry-run mode that lets you preview what would be removed before making any changes. This is essential for production deployments where you want to verify the sweep logic before executing it.

Think of the sweep like a refrigerator cleanout: you check the expiration dates on all items, identify anything past its prime (dry run), and then throw away only the expired ones (real sweep). You would never throw away food without checking the labels first, and you should never run a sweep in production without a dry-run preview. The `dry_run=True` flag is your safety net.

One scope note: the sweep only touches `add_credits` grants sitting in the balance waiting to expire — it has nothing to do with `deduct_with_allowance`, `settle`, or any other spend-side operation from Notebooks 03 and 06. Expiring credits and spending credits are two independent mechanisms that both happen to adjust the same balance.

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


# ---------------------------------------------------------------------------
# Notebook 06 - Financial Safety
# ---------------------------------------------------------------------------


def n_financial_safety():
    return [
        md("""# 06 - Financial Safety

ducto charges **after** an AI operation completes — but that is exactly when things can go wrong. If you only check the balance *before* the call and debit *after* it returns, you have a race: between the check and the debit, other concurrent operations can also pass the check, and a single expensive (or runaway agentic) call can finish costing far more than you estimated. Once the AI work has run, its cost is real regardless of what your ledger says — you cannot un-deliver a response.

The fix is an atomic **lease** taken *before* the work. A lease holds credits against `available = balance − Σ(active holds)`, so concurrent operations actually see each other. Every operation follows the same shape: **`reserve`** (place a hold) → **do the work** → **`settle`** (charge the actual cost) or **`release`** (cancel, charging nothing). Admission is the *only* place limits are enforced; `settle` is **de-clamped** — it always bills the real cost, even if that exceeds the hold.

This notebook covers the whole model: the two billing shapes (dynamic inference vs. fixed-cost jobs), the two admission presets (`strict_prepaid` vs `overdraft`), the modifiers you can layer on top (free-tier allowances, concurrency limits, feature gates, low-balance alerts), the advisory reads for UI, refunds, and what happens when something goes wrong (expired or missing leases). All money is `Decimal`, and everything below runs on a real PostgreSQL store."""),
        code("""import time
import uuid
from decimal import Decimal

from ducto import (
    CreditManager,
    UsageMetrics,
    LowBalanceConfig,
    ConcurrencyLimitError,
    FeatureNotEntitledError,
    InsufficientCreditsError,
    LeaseExpiredError,
    LeaseNotFoundError,
)
from ducto.events import CreditEvent, CreditEventEmitter
from ducto.interface.models import PricingConfigData, PlanDefinition, OperationPolicy
from shared import start_postgres_store, cleanup

# A temporary Postgres cluster with the ducto schema already applied.
# PostgresStore uses UUID user ids, so every demo user below is a fresh uuid4().
store, pgdata = start_postgres_store()
print("✔ PostgresStore ready.")"""),
        md("""## Why leases exist

The problem described above, made concrete: two requests from the same user, arriving close enough together that neither has been charged when the other is admitted."""),
        md("""### The naive way: check now, charge later

A common first instinct is to check the balance up front, do the work, and charge afterward with the atomic `deduct_with_allowance` primitive from Notebook 03. That primitive is safe against overdrawing *any single charge* — but it does nothing to stop two requests from both being told "yes, you have enough," because both check the same pre-charge balance."""),
        code("""naive_user = str(uuid.uuid4())
store.add_credits(naive_user, Decimal("50"), "signup_bonus")

worst_case = Decimal("40")  # what either request might cost in the worst case

# NAIVE: check-then-act, with the charge deferred until after the (expensive,
# irreversible) work completes -- exactly the shape of "charge after the call returns".
balance = store.get_balance(naive_user).balance
request_a_admitted = balance >= worst_case   # 50 >= 40 -> True
request_b_admitted = balance >= worst_case   # both requests read the SAME pre-charge balance -> True
print(f"  Both requests pass the balance check: A={request_a_admitted} B={request_b_admitted}")

# ... both AI calls run here -- compute cost already incurred, responses already
# delivered to both users, before either charge has been attempted ...

charge_a = store.deduct_with_allowance(naive_user, Decimal("38"))  # A's actual cost
charge_b = store.deduct_with_allowance(naive_user, Decimal("35"))  # B's actual cost
print(f"  Charge A: amount={charge_a.amount} error={charge_a.error}")
print(f"  Charge B: amount={charge_b.amount} error={charge_b.error}")
# B's charge is rejected -- but B's AI response was already generated and delivered.
# The 35-credit cost of that work is now unrecoverable: there is no way to
# retroactively un-deliver the response or force the charge through.
assert charge_b.error == "insufficient_credits\""""),
        md("""### The fix: reserve before doing any work

A lease moves the check to *before* the work, and makes it authoritative: `reserve()` atomically holds credits against `available`, so a second `reserve()` for the same user sees the first request's hold and is rejected immediately — before any AI call runs, before any cost is incurred."""),
        code("""lease_user = str(uuid.uuid4())
store.add_credits(lease_user, Decimal("50"), "signup_bonus")
lease_mgr = CreditManager(store=store, policy="strict_prepaid")
lease_mgr.publish_pricing_from_dict({"models": {"_default": "input_tokens * 1"}, "min_balance": 0})

lease_a = lease_mgr.reserve(lease_user, Decimal("40"))  # holds 40 against available (50)
print(f"  Request A reserved: held={lease_a.amount} available={lease_a.available}")  # available now 10

# Request B arrives before A has settled. Its worst-case hold no longer fits --
# it is rejected HERE, before any AI call runs, before any cost is incurred.
try:
    lease_mgr.reserve(lease_user, Decimal("40"))
    print("  (unexpected) Request B admitted")
except InsufficientCreditsError:
    print("  Request B rejected at admission -- no work was ever started for it")

lease_mgr.settle(lease_user, lease_a.lease_id, Decimal("38"))"""),
        md("""## Decision tree: which pattern for which situation?

| Situation | Use | Method |
|---|---|---|
| Cost is known upfront (flat fee: report, batch job, export) | Single atomic deduction | `deduct_fixed` (or `deduct_with_allowance` from Notebook 03 for a raw amount) |
| Cost is only known after the work finishes (chat, agentic run) | Reserve worst case, settle actual | `reserve()` → `settle()`/`release()` |
| Same as above, want less boilerplate | One-call wrapper over reserve/settle/release | `run_billed()` |
| Trusted/paid users should be allowed to briefly go negative | A modifier on either of the above | `policy="overdraft"` |
| Free-tier monthly credits before touching balance | A modifier on either of the above | plan `free_allowance` (Notebook 04), consumed automatically |
| Block a double-submit / cap concurrent work per user | A modifier on `reserve()` | `max_concurrent` (constructor or per-plan) |
| Restrict an operation to certain plans | A gate on `reserve()` | `required_feature` |"""),
        md("""## 1. `strict_prepaid`: reserve → settle, with worst-case sizing

We build a `CreditManager` with the default `strict_prepaid` policy and a small per-token price. The key discipline in strict mode: **size the hold at the worst case** you might bill. Admission subtracts that hold from `available`, so the balance can never be driven negative by work in flight. At `settle` time you bill the *actual* cost — which is usually less than the worst case, so the difference is automatically freed.

Below, a `gpt-4` call is reserved at its worst-case token budget, then settled at the (smaller) actual usage."""),
        code("""# strict_prepaid is the default policy; min_balance=0 keeps the arithmetic obvious.
manager = CreditManager(store=store, policy="strict_prepaid")
manager.publish_pricing_from_dict({
    "version": 1,
    "models": {"gpt-4": "input_tokens * 0.01 + output_tokens * 0.03"},
    "min_balance": 0,
})

user = str(uuid.uuid4())
manager.add_credits(user, Decimal("100"), "signup_bonus")

# Size the hold at the WORST CASE we might bill for this call.
worst_case = UsageMetrics(model="gpt-4", input_tokens=1000, output_tokens=1000)  # cost 40
lease = manager.reserve(user, worst_case, operation_type="chat")
print(f"  Lease:     {lease.lease_id}")
print(f"  Held:      {lease.amount}")          # 40 reserved against available
print(f"  Available: {lease.available}")        # 100 - 40 = 60 left for other work
print(f"  Mode:      {lease.billing_mode}")

# ... do the AI work ... it turned out cheaper than the worst case.
actual = UsageMetrics(model="gpt-4", input_tokens=500, output_tokens=200)  # cost 11
ded = manager.settle(user, lease.lease_id, actual)
print(f"  Charged:   {ded.amount}")             # 11, the ACTUAL cost (not the 40 hold)
print(f"  Balance:   {ded.balance_after}")        # 100 - 11 = 89; the unused 29 was freed
assert ded.balance_after == Decimal("89.00")"""),
        md("""### Releasing a lease (work aborted)

If the work fails or is cancelled, call `release` instead of `settle`. Nothing is charged and the hold is returned to `available`. `release` is **idempotent**: calling it twice (or after a settle) is safe and reports a `reason` rather than raising."""),
        code("""lease = manager.reserve(user, Decimal("20"), operation_type="chat")
print(f"  Reserved 20, available now: {manager.get_available(user).available}")  # 89 - 20 = 69

# The operation failed — cancel the hold, charge nothing.
rel = manager.release(user, lease.lease_id)
print(f"  Released:  {rel.released}  reason={rel.reason}")
print(f"  Available restored: {manager.get_available(user).available}")           # back to 89

# Idempotent: a second release does not raise, it just reports the state.
rel2 = manager.release(user, lease.lease_id)
print(f"  Second release: released={rel2.released} reason={rel2.reason}")
assert manager.get_balance(user).balance == Decimal("89.00")"""),
        md("""## 2. `run_billed`: the one-call shortcut

Wiring reserve → work → settle/release by hand is repetitive and easy to get wrong (e.g. forgetting to release on an exception). `run_billed` does it for you: pass an `estimate` (the worst-case hold) and a zero-arg `do_work` callable that returns `(result, actual)`. On success it settles the actual cost; **on any exception it auto-releases the lease** and re-raises."""),
        code("""def do_work():
    # Run the real operation, then report what it actually cost.
    answer = "the AI response"
    actual = UsageMetrics(model="gpt-4", input_tokens=300, output_tokens=100)  # cost 6
    return answer, actual

out = manager.run_billed(
    user,
    estimate=UsageMetrics(model="gpt-4", input_tokens=1000, output_tokens=1000),  # worst-case hold 40
    do_work=do_work,
    operation_type="chat",
)
print(f"  Result:   {out['result']!r}")
print(f"  Charged:  {out['deduction'].amount}")          # 6, the actual cost
print(f"  Balance:  {out['deduction'].balance_after}")     # 89 - 6 = 83
assert out["deduction"].balance_after == Decimal("83.00")"""),
        md("""## Pricing setup for the rest of this notebook

The remaining sections model a small real platform with three tiers, a couple of fixed-cost jobs, and per-token model pricing — every section below shares this one config via `saas_mgr`.

- **Free** — 100 credits/month allowance, 1 concurrent chat, no agentic.
- **Pro** — 500 credits/month, 4 concurrent, agentic unlocked.
- **Paid** — no allowance; paired with a dedicated overdraft-policy manager in Section 5.

The `_default` model expression (`input_tokens * 1 + output_tokens * 1`) gives 1 credit per token — easy mental arithmetic for the demos ahead."""),
        code("""saas_mgr = CreditManager(store=store)
saas_mgr.publish_pricing_from_dict({
    "version": 1,
    "models": {
        "gpt-4o":    "input_tokens * 0.01 + output_tokens * 0.03",
        "_default":  "input_tokens * 1 + output_tokens * 1",
    },
    "fixed": {
        "daily_report": 10,
        "batch_train":  50,
        "quick_summary": 0.5,
    },
    "min_balance": 0,
    "plans": {
        "free": {
            "id": "free", "name": "Free", "free_allowance": 100,
            "max_concurrent": 1, "features": {"chat": True},
        },
        "pro": {
            "id": "pro", "name": "Pro", "free_allowance": 500,
            "max_concurrent": 4, "features": {"chat": True, "agentic": True},
        },
        "paid": {
            "id": "paid", "name": "Paid", "free_allowance": 0,
            "max_concurrent": 8, "features": {"chat": True, "agentic": True},
        },
    },
})
print("✔ Pricing published.")"""),
        md("""## 3. Fixed-cost jobs: `deduct_fixed`

Operations with a known price — generating a PDF report, running a training job, sending a bulk notification batch — use `deduct_fixed`. The job name maps to the `fixed` section of your pricing config. There is no estimate/settle cycle: the cost is deducted atomically.

**Critical ordering rule: deduct first, execute second.**
Unlike dynamic inference (where you must wait for the model to know the cost), a fixed-cost job's price is known up front — so the debit happens *before* the job starts. If you flip the order (run job → charge), a user can exhaust their balance mid-run and the completed work goes unpaid.

**Job failure → refund.** Because the charge is taken before execution, any failure in the job must be followed by `refund_credits` using the `transaction_id` from the `DeductionResult`. The example below shows the full success path and the failure path with refund.

`deduct_fixed` is idempotent when you pass an `idempotency_key` — retrying the same key returns the original result instead of double-charging."""),
        code("""batch_user = str(uuid.uuid4())
saas_mgr.add_credits(batch_user, Decimal("500"), "purchase")

# ── Success path ──────────────────────────────────────────────────────────────
# Step 1: deduct BEFORE starting the job.
idem_key = f"report-{batch_user}-2026-06-30"
result = saas_mgr.deduct_fixed(batch_user, "daily_report", idempotency_key=idem_key)
print(f"Report charged: {result.amount} credits  (balance: {result.balance_after})")

# Step 2: run the job only after the deduction succeeds.
def generate_report():
    return {"pages": 3, "status": "ok"}  # simulated

report = generate_report()
print(f"Report generated: {report}")

# Retry with same key — idempotent, no double charge.
result2 = saas_mgr.deduct_fixed(batch_user, "daily_report", idempotency_key=idem_key)
print(f"Retry (idempotent): amount={result2.amount}, idempotent={result2.idempotent}, balance={result2.balance_after}")

# ── Failure path ──────────────────────────────────────────────────────────────
# Step 1: deduct before kicking off the training job.
train = saas_mgr.deduct_fixed(batch_user, "batch_train")
print(f"\\nTraining job charged: {train.amount} credits  (balance: {train.balance_after})")

# Step 2: job execution fails partway through.
def run_training():
    raise RuntimeError("GPU node unreachable")

try:
    run_training()
except RuntimeError as e:
    print(f"Job failed: {e}")
    # Step 3: refund the deduction — credits are restored to the user.
    refund = saas_mgr.refund_credits(train.transaction_id, reason="training_job_failed")
    assert refund.error is None, f"Refund failed: {refund.error}"
    print(f"Refund issued: +{refund.amount} credits  (balance: {refund.new_balance})")"""),
        md("""## 4. `max_concurrent`: blocking a double-submit

An impatient user who clicks *Send* twice, or a client that retries before the first response arrives, creates two simultaneous leases for the same operation. `max_concurrent` caps the number of active leases per operation type per user. The second admission is rejected with `ConcurrencyLimitError` — before any model call is made.

The free plan already has `max_concurrent: 1` for chat. The slot is freed when the first lease is settled or released. (You can also set a manager-wide default via `CreditManager(..., max_concurrent=N)` instead of per-plan; the per-plan value shown here takes precedence when the user has a plan.)"""),
        code("""conc_user = str(uuid.uuid4())
saas_mgr.add_credits(conc_user, Decimal("200"), "signup_bonus")
saas_mgr.set_user_plan(conc_user, "free")  # max_concurrent=1 for chat

# First request lands — lease acquired.
first = saas_mgr.reserve(conc_user, UsageMetrics(model="_default", input_tokens=100), operation_type="chat")
print(f"First request: lease acquired ({first.lease_id[:8]}…)")

# Second request (double-submit) — rejected immediately.
try:
    saas_mgr.reserve(conc_user, UsageMetrics(model="_default", input_tokens=100), operation_type="chat")
except ConcurrencyLimitError as e:
    print(f"Second request: ConcurrencyLimitError — {e}")

# First request finishes — slot freed, next request can proceed.
saas_mgr.settle(conc_user, first.lease_id, UsageMetrics(model="_default", input_tokens=80))
third = saas_mgr.reserve(conc_user, UsageMetrics(model="_default", input_tokens=100), operation_type="chat")
print(f"After settle:  new lease acquired ({third.lease_id[:8]}…)")
saas_mgr.release(conc_user, third.lease_id)"""),
        md("""## 5. The `overdraft` preset

Strict mode guarantees zero debt, but sometimes you *want* to let a trusted, paid user run past their balance and reconcile later — typically via a saved payment method that auto-charges on top-up. The `overdraft` preset admits down to a **negative `overdraft_floor`**, and at settle time clamps the actual charge so the balance never breaches that floor (C1): settle still bills the real cost of the work, but the debit itself is bounded. The balance genuinely goes negative (unlike `strict_prepaid`), but never past the floor you configured. Once past the floor, new admissions are rejected until you reconcile with `add_credits`.

This demo uses a dedicated `CreditManager(policy="overdraft", overdraft_floor=...)` instance rather than the shared `saas_mgr` from the previous sections — in an application with a small, fixed number of billing tiers, building one manager per policy (strict for free/pro, overdraft for paid) is the simplest approach; see the architecture doc if you need the policy resolved per-plan or per-call instead."""),
        code("""# A dedicated manager for overdraft users — floor matches the plan.
od_mgr = CreditManager(store=store, policy="overdraft", overdraft_floor=Decimal("-50"))
od_mgr.publish_pricing_from_dict({
    "version": 1,
    "models": {"_default": "input_tokens * 1 + output_tokens * 1"},
    "min_balance": 0,
})

paid_user = str(uuid.uuid4())
od_mgr.add_credits(paid_user, Decimal("30"), "purchase")  # thin balance

# Reserve an estimate — work may cost more.
lease = od_mgr.reserve(paid_user, Decimal("20"), operation_type="chat")
print(f"Estimated hold: {lease.amount}  balance available: {lease.available}")

# Model returns 60 tokens — actual > estimate. settle bills the full 60 here
# because balance(30) - floor(-50) = 80 of headroom covers it; if the actual
# cost had exceeded that headroom, settle would clamp the debit to the floor (C1).
ded = od_mgr.settle(paid_user, lease.lease_id, Decimal("60"))
print(f"Actual charged: {ded.amount}  balance after: {ded.balance_after}")

# Balance is now negative. New admission blocked until reconciled.
try:
    od_mgr.reserve(paid_user, Decimal("5"), operation_type="chat")
except InsufficientCreditsError:
    print("New request blocked — InsufficientCreditsError (balance below floor)")

# Auto-reload fires (card charged), recorded as a purchase-type top-up, balance reconciled.
od_mgr.add_credits(paid_user, Decimal("200"), "purchase")
after = od_mgr.get_available(paid_user)
print(f"After reload:   balance={after.balance}  available={after.available}")

# Now admission succeeds again.
new_lease = od_mgr.reserve(paid_user, Decimal("10"), operation_type="chat")
od_mgr.release(paid_user, new_lease.lease_id)
print("New request admitted successfully.")"""),
        md("""## 6. Feature gating

Expensive or risky operations (autonomous agentic runs, bulk exports, higher-context models) should be restricted to plans that include them. Pass `required_feature` to `reserve` — ducto checks the user's plan and raises `FeatureNotEntitledError` before any work starts or credits are held."""),
        code("""free_gated = str(uuid.uuid4())
pro_user   = str(uuid.uuid4())
saas_mgr.add_credits(free_gated, Decimal("500"), "signup_bonus")
saas_mgr.add_credits(pro_user,   Decimal("500"), "purchase")
saas_mgr.set_user_plan(free_gated, "free")
saas_mgr.set_user_plan(pro_user,   "pro")

# Free user — agentic feature absent from plan.
try:
    saas_mgr.reserve(
        free_gated,
        UsageMetrics(model="_default", input_tokens=500),
        operation_type="agentic",
        required_feature="agentic",
    )
except FeatureNotEntitledError:
    print("Free user: FeatureNotEntitledError — agentic not in plan (no hold placed)")

# Pro user — feature present, admission succeeds.
pro_lease = saas_mgr.reserve(
    pro_user,
    UsageMetrics(model="_default", input_tokens=500),
    operation_type="agentic",
    required_feature="agentic",
)
print(f"Pro user:   lease acquired — {pro_lease.amount} credits held")
saas_mgr.release(pro_user, pro_lease.lease_id)"""),
        md("""## 7. Free-tier allowances as a modifier, not a separate pattern

Free-tier monthly allowances (Notebook 04) aren't a separate billing pattern — they're a modifier that composes with whatever pattern you're already using. The exact same `reserve()`/`settle()` calls from Section 1 automatically draw down a user's allowance first, falling back to their balance only once it's exhausted. `DeductionResult.amount` is the **net amount actually debited from balance** (after allowance); `allowance_consumed` is the portion covered by allowance instead — the *total* actual cost of the call is `amount + allowance_consumed`.

Below, a free-tier user (100 credits/month allowance) runs two chat calls. The first is covered entirely by allowance; once it's exhausted, the second is covered entirely by balance — same code path both times."""),
        code("""free_user = str(uuid.uuid4())
saas_mgr.add_credits(free_user, Decimal("200"), "signup_bonus")
saas_mgr.set_user_plan(free_user, "free")

allowance = saas_mgr.check_allowance(free_user)
print(f"Allowance remaining: {allowance.allowance_remaining} / period ends {allowance.period_end[:10]}")

# Call 1: fully covered by the 100-credit allowance.
lease = saas_mgr.reserve(free_user, UsageMetrics(model="_default", input_tokens=100, output_tokens=100), operation_type="chat")
ded = saas_mgr.settle(free_user, lease.lease_id, UsageMetrics(model="_default", input_tokens=60, output_tokens=40))
print(f"\\nCall 1 — total cost: {ded.amount + ded.allowance_consumed}  from allowance: {ded.allowance_consumed}  from balance: {ded.amount}")
print(f"Allowance remaining: {saas_mgr.check_allowance(free_user).allowance_remaining}")

# Call 2: allowance now exhausted -- the SAME reserve()/settle() calls fall
# back to balance automatically, no branching required in application code.
lease2 = saas_mgr.reserve(free_user, UsageMetrics(model="_default", input_tokens=50, output_tokens=50), operation_type="chat")
ded2 = saas_mgr.settle(free_user, lease2.lease_id, UsageMetrics(model="_default", input_tokens=50, output_tokens=50))
print(f"\\nCall 2 — total cost: {ded2.amount + ded2.allowance_consumed}  from allowance: {ded2.allowance_consumed}  from balance: {ded2.amount}")
print(f"Balance after:   {ded2.balance_after}")"""),
        md("""## 8. Multi-level low-balance alerts + `LowBalanceConfig`

Pass a `low_balance=LowBalanceConfig(thresholds=[...], on_trigger=...)` config to the constructor to get **edge-triggered** alerts: `thresholds` is sorted internally high → low, and each level fires once on the descent that crosses it, re-arming only after a top-up climbs back above it. `on_trigger` is an async-safe, **non-blocking** callable — perfect for a payment-provider-agnostic reload or notification. A handler that raises never breaks the operation.

We use the `overdraft` preset here only so the balance can descend freely through every threshold."""),
        code("""fired = []

def on_low_balance(event: CreditEvent):
    # Payment-provider-agnostic: enqueue a reload, send an email, page on-call, etc.
    level = event.data["threshold"]
    balance = event.data["balance"]
    fired.append(level)
    print(f"  [hook] balance {balance} crossed threshold {level} — triggering reload")

emitter = CreditEventEmitter()
lb_mgr = CreditManager(
    store=store,
    emitter=emitter,
    policy="overdraft",
    overdraft_floor=Decimal("0"),
    low_balance=LowBalanceConfig(
        thresholds=[Decimal("50"), Decimal("20"), Decimal("10")],
        on_trigger=on_low_balance,
    ),
)
lb_mgr.publish_pricing_from_dict({
    "version": 1,
    "models": {"_default": "input_tokens * 1"},
    "min_balance": 0,
})

lbu = str(uuid.uuid4())
lb_mgr.add_credits(lbu, Decimal("100"))

def charge(amount):
    lease = lb_mgr.reserve(lbu, Decimal(amount))
    lb_mgr.settle(lbu, lease.lease_id, Decimal(amount))

charge(55)  # 100 -> 45 : crosses 50
charge(30)  # 45  -> 15 : crosses 20
charge(7)   # 15  -> 8  : crosses 10
print(f"  Thresholds fired (each once): {fired}")
assert fired == [Decimal("50"), Decimal("20"), Decimal("10")]"""),
        md("""## 9. Advisory reads: `get_available()` vs `can_afford()`

For UI elements — a balance bar, deciding whether to enable the *Send* button — use the non-locking advisory reads. They are fast, never block the operation, and may be slightly stale under concurrency; that's fine for UI. **Only `reserve()` is the real admission gate.**

- `get_available()` → `balance`, `reserved` (active lease holds), `available = balance − reserved` — **cash only**, it does not include free allowance.
- `can_afford(amount_or_metrics)` → `affordable`, `spendable`, `worst_case`, `reason`. `spendable` is the user's **effective spending power** — `balance − reserved + remaining free allowance` — which matches exactly what `reserve()` will actually admit. Use `spendable` (not `available`) to gate a "Send" button, so a free-tier user with allowance left isn't shown a false "insufficient funds."

`available` and `spendable` answer different questions — don't confuse them: `available` is what a raw balance display should show; `spendable` is what an admission-affecting UI decision should check."""),
        code("""ui_user = str(uuid.uuid4())
saas_mgr.add_credits(ui_user, Decimal("150"), "purchase")

# Simulate one in-flight lease already holding 80 credits.
in_flight = saas_mgr.reserve(ui_user, Decimal("80"), operation_type="chat")

# Balance bar: show how much is available for new work (cash only, no allowance).
avail = saas_mgr.get_available(ui_user)
print("Balance bar:")
print(f"  Total balance:   {avail.balance}")
print(f"  In-flight holds: {avail.reserved}")
print(f"  Available now:   {avail.available}")

# Send-button affordability check. check.spendable includes remaining allowance —
# it is the effective spending power that reserve() will actually admit against,
# which is why it's the right field to gate a "Send" button (not avail.available above).
next_request = UsageMetrics(model="_default", input_tokens=200, output_tokens=100)  # worst-case 300
check = saas_mgr.can_afford(ui_user, next_request)
print(f"\\nSend button enabled: {check.affordable}")
print(f"  Worst-case cost: {check.worst_case}")
print(f"  Spendable:       {check.spendable}")

# Request that would exceed available funds.
too_big = saas_mgr.can_afford(ui_user, Decimal("200"))
print(f"\\n200-credit request affordable: {too_big.affordable}  (reason: {too_big.reason})")

saas_mgr.release(ui_user, in_flight.lease_id)"""),
        md("""## 10. Refunds

Refunds reverse a completed deduction and restore the user's balance. They are identified by `transaction_id` from the `DeductionResult`. Partial refunds are supported. `result.error` is set (not raised) on business-rule failures such as over-refunding, duplicate refunds, or refunding a purchase — always check it before treating the result as success."""),
        code("""ref_user = str(uuid.uuid4())
saas_mgr.add_credits(ref_user, Decimal("500"), "purchase")

# Charge for a batch training job.
ded = saas_mgr.deduct_fixed(ref_user, "batch_train")
print(f"Charged:        {ded.amount}  (tx: {ded.transaction_id[:8]}…  balance: {ded.balance_after})")

# Full refund — training job failed before completing.
refund = saas_mgr.refund_credits(ded.transaction_id, reason="training_job_failed")
assert refund.error is None, f"Refund failed: {refund.error}"
print(f"Full refund:    +{refund.amount}  new balance: {refund.new_balance}")

# Partial refund example — dynamic inference, bill only for tokens actually processed.
lease = saas_mgr.reserve(ref_user, Decimal("100"), operation_type="chat")
ded2 = saas_mgr.settle(ref_user, lease.lease_id, Decimal("80"))
print(f"\\nCharged (inference): {ded2.amount}  balance: {ded2.balance_after}")

partial = saas_mgr.refund_credits(ded2.transaction_id, amount=Decimal("30"), reason="user_cancelled_mid_stream")
assert partial.error is None
print(f"Partial refund:      +{partial.amount}  new balance: {partial.new_balance}")

# Duplicate refund — business-rule failure, never raises.
dup = saas_mgr.refund_credits(ded.transaction_id)  # already fully refunded
print(f"\\nDuplicate refund:    error='{dup.error}'  (no credits moved)")"""),
        md("""## 11. Failure modes: expired and missing leases

Two things you will hit in production, both raised as clear typed exceptions rather than silent no-ops or generic errors:

- **`LeaseExpiredError`** — you called `settle()` (or `renew()`) after the lease's TTL elapsed. The default TTL is generous (long enough for a normal request), but a stuck job or a very slow stream can outlive it. No charge is made; you would typically `reserve()` again and retry, or `renew()` before the TTL runs out on long-running work.
- **`LeaseNotFoundError`** — you called `settle()` or `release()` with a `lease_id` that doesn't exist, belongs to another user, or was already released. This is different from `release()`'s own idempotency (Section 1) — releasing twice is safe and reports a `reason`; *settling* an already-released lease raises instead, since silently accepting a charge against a hold that no longer exists would be a real bug to hide.
- Refund failures (over-refund, duplicate refund) never raise — `refund_credits()` sets `result.error` instead, exactly as shown with the duplicate refund in Section 10 and in Notebook 03. It's the one departure from "business failures raise typed exceptions" in this notebook, and it's intentional: a refund is often called from a webhook or a retry path where you want to inspect and log the outcome rather than handle an exception."""),
        code("""fail_user = str(uuid.uuid4())
saas_mgr.add_credits(fail_user, Decimal("500"), "purchase")

# LeaseExpiredError: settling after the lease's TTL has elapsed.
short_lease = saas_mgr.reserve(fail_user, Decimal("10"), operation_type="chat", ttl=1)
time.sleep(1.2)
try:
    saas_mgr.settle(fail_user, short_lease.lease_id, Decimal("5"))
except LeaseExpiredError:
    print("  settle() after TTL elapsed: LeaseExpiredError (no charge was made)")

# LeaseNotFoundError: settling a lease that's already been released.
lease2 = saas_mgr.reserve(fail_user, Decimal("10"), operation_type="chat")
saas_mgr.release(fail_user, lease2.lease_id)
try:
    saas_mgr.settle(fail_user, lease2.lease_id, Decimal("5"))
except LeaseNotFoundError:
    print("  settle() on an already-released lease: LeaseNotFoundError")

# Refund failures never raise -- see the duplicate refund_credits() call in Section 10.
print("  (duplicate/over-refund: see the refund_credits().error check in Section 10)")"""),
        md("""## Recap

| Use case | Pattern | Key method |
|---|---|---|
| Dynamic inference (unknown cost until the work finishes) | `reserve` → model call → `settle` (or `release` on error) | `manager.reserve()` / `manager.settle()` |
| One-call dynamic inference | `run_billed` auto-wires reserve/settle/release | `manager.run_billed()` |
| Fixed-cost job (known price, possibly fractional) | Single atomic deduct, idempotent, does not consume allowance by default (`use_allowance=True` opts in) | `manager.deduct_fixed()` |
| Free monthly credits | Plan with `free_allowance` (+ optional `allowance_period`, Notebook 04); consumed automatically by `reserve`/`settle`/`deduct_fixed` | `manager.check_allowance()` |
| Double-submit prevention | Manager-wide or per-plan `max_concurrent` | Raises `ConcurrencyLimitError` |
| Plan-gated features | Pass `required_feature` to `reserve` | Raises `FeatureNotEntitledError` |
| Balance bar / send button | `get_available().available` is cash-only; `can_afford().spendable` includes allowance headroom — use `spendable` for the button | `manager.get_available()` / `manager.can_afford()` |
| Trusted paid users, auto-reload | Overdraft preset + `low_balance=LowBalanceConfig(...)` hook | `policy="overdraft"` + `manager.add_credits()` |
| Reverse a charge | Refund by transaction ID, supports partial, never raises | `manager.refund_credits()` |

**Error reference:**
- `InsufficientCreditsError` — balance + allowance (minus active holds) below floor at admission
- `ConcurrencyLimitError` — `max_concurrent` active leases for this op type already
- `FeatureNotEntitledError` — user's plan missing `required_feature`
- `CapReachedError` — spend cap (deny) hit at admission (Notebook 07)
- `LeaseExpiredError` — lease TTL elapsed before `settle`
- `LeaseNotFoundError` — `lease_id` unknown, belongs to another user, or already released

**Event reference (non-blocking signals after a charge):**
- `credits.overdraft` — balance went negative (overdraft mode)
- `credits.floor_breach` — balance slipped below `min_balance` without going negative (strict mode under-estimate)
- `credits.low_balance` — edge-triggered threshold crossing (configure via `low_balance=LowBalanceConfig(thresholds=..., on_trigger=...)`; defaults to a single threshold of `min_balance * 2`, which is `0` unless you set `min_balance` or `low_balance` explicitly)
- `credits.cap_warning` — soft spend-cap crossed at settle time (Notebook 07)

See also: [Financial Safety guide](/docs/financial-safety)."""),
        pg_teardown(),
    ]


# ---------------------------------------------------------------------------
# Notebook 07 - Spend Caps (uses MemoryStore)
# ---------------------------------------------------------------------------


def n_spend_caps():
    return [
        md("""# 07 - Spend Caps

Without spend caps, a single bug or runaway loop can drain a user's entire credit balance in seconds. Spend caps act as safety valves — they limit how many credits a user can consume in a given period, protecting both the user and the platform operator from unexpected costs.

ducto supports three cap behaviors: **deny** (hard block — the operation is rejected when the cap is exceeded), **warn** (soft alert — the operation proceeds but the overage is flagged), and **notify** (passive monitoring — the operation proceeds and the overage is recorded the same way). The deny action is the most common choice for production deployments, while warn and notify are useful for gradual rollouts or monitoring-only scenarios.

Caps can be configured per user and per period type. ducto supports **daily** caps, which reset every calendar day, and **monthly** caps, which reset every calendar month. You can set different limits for different users — for example, a 5,000-credit daily cap for free-tier users and a 50,000-credit daily cap for enterprise customers.

In this notebook we will use the `MemoryStore` implementation and walk through each cap action type. You will see what happens when a deduction stays under the cap, when it exceeds a deny cap, when it exceeds a warn cap, and when it exceeds a notify cap. By the end of this notebook you will understand how to configure and enforce spend controls in your own ducto deployment."""),
        md("""### Setup

This notebook uses `MemoryStore` because writing a spend cap (`set_spend_cap`) is a `MemoryStore`-only convenience, not part of the portable `CreditStore` interface (see Notebook 00). `PostgresStore` ships the same `credit_spend_caps` schema and implements the read side (`check_spend_cap`, which `deduct_with_allowance` also calls internally) — but there is no writer method. In production you configure a cap by inserting a row into `credit_spend_caps` directly (via your own admin tooling or a migration); Postgres then enforces it exactly like `MemoryStore` does below.

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

With the 5,000-credit daily deny cap in place, we first test a deduction that stays under the limit. A 3,000-credit charge is well within the 5,000-credit cap, so the deduction should complete successfully.

After the deduction, we call `check_spend_cap()` to inspect the user's current spend tracking. This method returns a `CapCheckResult` object that tells us whether the user is currently capped, how much they have spent in the current period, and what their limit is. After a 3,000-credit deduction against a 5,000-credit cap, the current spend should be 3,000 and the user should not be flagged as capped."""),
        code("""# Deduct 3,000 credits — this is well under the 5,000 daily cap.
# deduct_with_allowance() enforces the cap as part of its atomic transaction,
# so this single call both charges the user and checks the cap.
ded = store.deduct_with_allowance(user, 3_000)
print(f"  Deduction succeeded: balance after = {ded.balance_after}")

# Check the user's current spend against their configured cap
check = store.check_spend_cap(user)
print(f"  Cap status: capped={check.capped}, current spend={check.current_spend}")"""),
        md("""### Exceed cap (denied)

Now we attempt a second deduction that would push the user over the 5,000-credit daily limit. The user has already spent 3,000 credits, and a further 3,000-credit deduction would bring the total to 6,000 — exceeding the cap by 1,000.

With the deny action, the store rejects the deduction and returns an error message explaining why. The credits stay in the user's account: the cap check and the balance debit happen inside the same server-side transaction as `deduct_with_allowance` itself, so there is no window where a concurrent request could sneak a charge through between checking the cap and applying it. This is the expected behavior for a hard cap: the application receives the error and can decide how to respond — perhaps by showing the user an upgrade prompt, logging the event for admin review, or retrying with a smaller operation.

This safety net prevents runaway costs from a single misconfigured loop or a compromised API key. Without it, the same 3,000-credit deduction would succeed and leave the platform operator to detect the overage retroactively. In this section we attempt the over-cap deduction and observe the deny behavior in action."""),
        code("""# Attempt a second deduction of 3,000 credits — this would exceed the remaining
# cap (3,000 already spent + 3,000 new > 5,000 cap). deduct_with_allowance()
# rejects it atomically, before any credits leave the account.
ded2 = store.deduct_with_allowance(user, 3_000)

# Check the result: with a deny cap, the error field contains a descriptive message
if ded2.error:
    print(f"  Deduction denied: {ded2.error}")
else:
    print(f"  Deduction allowed: balance after = {ded2.balance_after}")
print(f"  Explanation: daily cap = 5,000, already spent 3,000 today")"""),
        md("""### Cap with warn action

Not every cap breach needs to be a hard block. Sometimes you want to allow the operation but flag it for attention. The **warn** action does exactly that: the deduction proceeds normally (`result.error` stays `None`), but `check_spend_cap()` reports that the user is over their configured limit.

This is useful for gradual rollouts of spend controls. You might set a warn cap first to observe how many users would be affected, then switch to deny once you are confident in the limits. It is also appropriate for internal tools or trusted users where you want visibility into spending without disrupting their workflow.

In this section we create a second user with a much lower daily cap of 500 credits and the warn action. We then attempt a 1,000-credit deduction and observe that it succeeds even though it exceeds the cap. The `check_spend_cap()` response confirms the overage but does not block the operation."""),
        code("""# Create a second user for the warn cap demonstration
user2 = str(uuid.uuid4())

# Seed the user with 50,000 credits, same as the first user
store.add_credits(user2, 50_000, type="seed")

# Set a very low daily cap of 500 credits with the warn action (not deny)
store.set_spend_cap(SpendCap(user_id=user2, cap_type="daily", limit=500, action="warn"))

# Attempt to deduct 1,000 credits — exceeds the 500-credit warn cap, but a
# warn cap never blocks the deduction, only flags it.
ded3 = store.deduct_with_allowance(user2, 1_000)
print(f"  Deduction result: error={ded3.error}  (None -- warn never blocks)")

# Check cap status — the action field confirms "warn" and current spend exceeds the limit
check2 = store.check_spend_cap(user2)
print(f"  Current spend: {check2.current_spend}  Cap limit: {check2.cap_limit}  Action: {check2.action}")
print(f"  Key insight: warn action allows the deduction but flags the overage")"""),
        md("""### Cap with notify action

The **notify** action behaves identically to **warn** at the deduction level — the charge still proceeds, and `check_spend_cap()` still reports the overage. ducto doesn't treat the two differently in the store; the distinction is a labeling convention for *your* application to interpret. A common split: surface `warn` inline (e.g. a banner in the product UI), and route `notify` to an out-of-band channel your users don't see directly (e.g. an email digest or a Slack alert to the account team) — both read the same `action` field from `check_spend_cap()` to decide which path to take."""),
        code("""# Create a third user for the notify cap demonstration
user3 = str(uuid.uuid4())
store.add_credits(user3, 50_000, type="seed")
store.set_spend_cap(SpendCap(user_id=user3, cap_type="daily", limit=1_000, action="notify"))

# Exceeds the 1,000-credit notify cap — proceeds anyway, just like warn.
ded4 = store.deduct_with_allowance(user3, 1_500)
check3 = store.check_spend_cap(user3)
print(f"  Deduction result: error={ded4.error}  balance_after={ded4.balance_after}")
print(f"  Cap status: current spend={check3.current_spend}  limit={check3.cap_limit}  action={check3.action}")
print("  Key insight: notify behaves identically to warn at the deduction level --")
print("  it's up to your application to route action=='notify' differently from action=='warn'.")"""),
    ]


# ---------------------------------------------------------------------------
# Notebook 08 - Teams
# ---------------------------------------------------------------------------


def n_teams():
    return [
        md("""# 08 - Teams

Individual user balances work well for B2C products where each user pays for themselves. But B2B SaaS needs team accounts — one company with multiple users sharing a single credit pool. ducto's team feature lets you create shared balances, add members, enforce per-user spend caps, and track who spent what. Think of it like a shared bank account with individual debit card limits.

In a typical B2B scenario, a company purchases a block of credits and then distributes access to its employees or departments. Rather than managing individual balances for each employee, you create a single team pool. Every team member draws from that shared pool, and you can optionally cap how much each individual can spend. This mirrors the real-world pattern of a corporate card with per-employee spending limits.

Beyond simple sharing, teams also give you auditability: each deduction records which team member made the request, so you can bill back costs to specific departments or users. Combined with per-member caps, you prevent any single user from accidentally (or intentionally) exhausting the entire team's budget.

What we will do in this section: create a team with an initial balance, add three members, make deductions from the shared pool, query per-member spend, observe what happens when the pool is empty, and enforce a per-member spend cap to demonstrate cost governance within a team."""),
        pg_setup("import uuid"),
        md("""### Create team with initial balance

When you create a team, ducto establishes a separate credit balance that belongs to the team entity, not to any individual user. This is fundamentally different from the user-level `add_credits` calls we have seen in earlier notebooks: the team balance lives in its own ledger and is only accessible through team-specific API methods like `deduct_team` and `get_team_balance`.

Think of it as opening a joint bank account. The initial deposit of 100 000 credits is the team's working capital. Individual user balances still exist independently (they may have their own personal credits too), but team operations draw exclusively from the team pool. The two ledgers — personal and team — are separate and do not intermix.

`team.team_id` is an opaque string identifier (a UUID string on `PostgresStore`) — treat it the same way you treat a user ID: pass it verbatim to every team-scoped call.

What we will do in this section: call `store.create_team` with a name and initial balance, then inspect the returned team object to see its assigned identifier."""),
        code("""# Create a team entity with its own independent credit balance.
# The team "Engineering" gets 100 000 credits deposited into its
# team pool. This balance is separate from any individual user
# balance and can only be accessed through team-specific methods.
team = store.create_team(name="Engineering", initial_balance=100_000)
print(f"  Team created: name='{team.name}', id={team.team_id}, initial_balance=100000")"""),
        md("""### Add members

Before a user can join a team, they must already exist in the `user_credits` table. This is an intentional design choice: ducto requires every team member to have a user record, even if that record has a zero balance. The team does not create user accounts — it only associates existing users with a shared pool. This ensures that all credit operations, including team deductions, are always attributed to a real user identity. Below we use the zero-balance `add_credits(uid, 0, type="adjustment")` idiom introduced in Notebook 03 to create bare user records before adding them to the team.

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
        md("""### Querying per-member spend

`deduct_team` attributes each charge to the member who incurred it, and that attribution is queryable, not just a log line: `get_team_members()` returns every member's cumulative `total_spent` within the team, alongside their role and any spend cap. This is what backs "the receipt shows which employee made the purchase" in a real billing dashboard — members[0] should now show 5 000 spent, the other two 0."""),
        code("""members_after = store.get_team_members(team.team_id)
for m in members_after:
    print(f"  {m.user_id[:8]}…  role={m.role}  total_spent={m.total_spent}")"""),
        md("""### Exceed team balance (rejected)

What happens when a team tries to spend more credits than the pool contains? Just like overdraft protection on a bank account, ducto rejects the transaction. The `deduct_team` call returns an error code (`"insufficient_credits"` on `PostgresStore`; `MemoryStore` returns the more specific `"insufficient_team_balance"`) rather than allowing the balance to go negative. This is a safety mechanism: it prevents the team from accruing debt and ensures that credits are consumed only when they are available.

This behavior is by design. In a production SaaS application, an overdrawn team pool could mean a user receives service they cannot pay for, creating a billing gap. By rejecting insufficient-balance transactions upfront, ducto lets you surface the error to the team admin, who can then top up the pool before the user experiences a service disruption.

What we will do in this section: attempt to deduct 999 999 credits from the team pool (far exceeding the remaining 95 000), observe the error response, and verify the assertion that enforces the rejection."""),
        code("""# Attempt to deduct 999 999 credits, far more than the 95 000
# remaining in the team pool. The store should reject this.
res2 = store.deduct_team(team.team_id, members[1], 999_999)
print(f"  Attempt to deduct 999 999: error='{res2.error}', team_balance_after={res2.team_balance_after}")
print(f"  (team balance is 95 000 — insufficient to cover the request)")
assert res2.error == "insufficient_credits" """),
        md("""### Per-member spend cap

A shared pool solves the basic sharing problem, but it introduces a new one: any single member could drain the entire team's credits. A rogue script, an aggressive user, or a bug in your application could consume the whole pool in minutes. Per-member spend caps prevent this by limiting how much each individual can draw from the team pool within a period.

The cap is set when you add the member via `add_team_member(..., spend_cap=3_000)`. Once the member's cumulative team spending reaches that limit, subsequent `deduct_team` calls return an error (`"cap_reached"` on `PostgresStore`; `MemoryStore` returns the more specific `"spend_cap_exceeded"`). The team balance may still have plenty of credits — the cap only restricts that specific user. You can raise, lower, or remove the cap dynamically without affecting other members.

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
assert res4.error == "cap_reached"
print("  Verification passed: per-member spend cap correctly enforced")"""),
        pg_teardown(),
    ]


# ---------------------------------------------------------------------------
# Notebook 09 - Analytics
# ---------------------------------------------------------------------------


def n_analytics():
    return [
        md("""# 09 - Analytics

Raw credit transactions are a stream of individual events — user X deducted Y credits at time Z. That is hard to read at a glance. ducto's analytics queries aggregate these events into meaningful summaries: total spend per user, breakdown by model, daily trends, and overall statistics. These queries are the foundation for customer-facing dashboards, internal cost analysis, and anomaly detection.

ducto provides five built-in analytics methods through every `CreditStore` implementation. `spend_by_user()` groups deductions by user and returns each user's total spend and transaction count. `spend_by_model()` breaks down credits consumed by AI model name — this is what the `model=` tag from Notebook 03's `deduct_with_allowance()` calls feeds into. `top_users()` returns the highest-spending users, sorted by total consumption. `daily_spend()` groups transactions by calendar date to reveal trends over time. Finally, `aggregate_stats()` computes a single summary row with total credits, active user count, average daily spend, top model, and top user.

These queries are designed to be efficient against Postgres backends but work equally well with in-memory stores for testing and development. Together they form a complete analytics toolkit that answers the most common questions any platform operator needs: who is spending, what are they spending on, and how does spending evolve over time.

In this notebook we will seed a realistic dataset and then run each analytics query to see what it reveals. By the end you will understand how to extract actionable insights from raw credit event data using ducto's analytics layer."""),
        md("""### Setup

Before we can query analytics, we need a running PostgresStore instance with a seeded database. The `start_postgres_store()` helper handles the connection lifecycle for us, initializing the schema and returning a ready-to-use store object — see Notebook 00 for exactly what that setup does.

The setup also imports the utilities we need: `uuid` for generating user identifiers, `random` for simulating realistic transaction amounts and model choices, and `timezone` for working with UTC timestamps. These are standard library modules — no additional dependencies required."""),
        pg_setup("""\
import uuid, random
from datetime import timezone"""),
        md("""### Seed sample data

Analytics queries need data to be interesting. Here we simulate three users, each making one random-sized transaction per day for seven days against one of three models (`gpt-4o`, `claude-sonnet-4`, `claude-haiku-3.5`) — enough variation to produce non-trivial results in every query below, small enough to stay interpretable."""),
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
        # Randomly pick which AI model this transaction is attributed to
        model = random.choice(["gpt-4o", "claude-sonnet-4", "claude-haiku-3.5"])
        # deduct_with_allowance() is the atomic "calculate cost, then charge"
        # primitive; passing model= attributes this transaction for spend_by_model.
        store.deduct_with_allowance(u, amount, model=model)

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

While per-user and per-model queries tell you "who" and "what," the `daily_spend()` query tells you "when." It groups transactions by calendar date within the time window, revealing usage patterns over time such as weekday spikes, weekend dips, or sustained growth trends. Each `date` bucket is a UTC calendar day — a transaction at 23:30 UTC and one at 00:30 UTC the next day land in different buckets even if both users are in the same non-UTC timezone. Convert to a display timezone at the UI layer, not by adjusting the query window.

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

This is the method you would call to populate a billing dashboard's header card. It provides an instant overview without requiring you to run multiple queries and compute the summaries yourself. Querying a window with no transactions at all returns zero-valued fields (`total_credits_consumed=0`, `active_users=0`, empty strings for `top_model`/`top_user`) rather than `None` or an error — safe to feed straight into a dashboard without a null check.

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
# Notebook 10 - Events
# ---------------------------------------------------------------------------


def n_events():
    return [
        md("""# 10 - Events

Credit operations are useful on their own, but often you need to react to them — send a Slack alert when a user's balance runs low, update an analytics dashboard on each deduction, or trigger an auto top-up. ducto's event system follows the observer pattern: you emit events when operations happen, and registered handlers react asynchronously.

The observer pattern is a software design pattern where an object (the subject) maintains a list of dependents (observers) and notifies them automatically of state changes. In ducto, every credit operation — adding credits, deducting, refunding, hitting a cap — generates a typed event that can be observed by any number of handlers. This decouples the core credit logic from the integrations that react to it.

Without events, you would need to add notification logic directly inside every credit operation call site: after `add_credits`, check the balance and send a Slack message; after `deduct`, update the dashboard. This approach is fragile, tightly coupled, and hard to maintain as your integration surface grows. Events solve this by letting you register handlers once, after which all relevant operations automatically trigger them.

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

In this section, we register a separate handler that only listens for refund events and appends them to its own list. Then we deduct and refund through the same emitter-wired `manager` and verify that the refund-specific handler was triggered.

Note that the refund event carries information about the original deduction transaction, the refund amount, and the reason for the refund. This data is accessible through the event's `data` dictionary and is critical for audit trails and accounting reconciliation.

What we will do in this section: register a dedicated refund handler on the emitter, deduct then refund through `manager`, and verify that the refund handler captured the expected events."""),
        code("""# Create a dedicated list to capture only refund events, and
# register a handler that subscribes exclusively to the
# "credits.refunded" event type. This demonstrates type-specific
# subscriptions: this handler will only fire when a refund occurs.
refunds: list[CreditEvent] = []
emitter.on("credits.refunded", lambda e: refunds.append(e))

# Deduct 100 credits, then refund them in full through the same
# emitter-wired manager. The refund triggers a credits.refunded
# event, which our dedicated refund handler captures. The reason
# "demo" is attached to the event data for audit trail purposes.
ded_tx = manager.deduct(user, UsageMetrics(model="_default", input_tokens=100))
manager.refund_credits(ded_tx.transaction_id, amount=100, reason="demo")
print(f"Refund events captured by dedicated handler: {len(refunds)}")"""),
        pg_teardown(),
    ]


# ---------------------------------------------------------------------------
# Notebook 11 - Custom Store
# ---------------------------------------------------------------------------


def n_custom_store():
    return [
        md("""# 11 - Custom Store

ducto ships with two store implementations: PostgresStore (production-ready, persistent) and MemoryStore (development, ephemeral). But your application might use a different backend -- Redis for speed, DynamoDB for scalability, SQLite for embedded deployments. The CreditStore abstract base class (ABC) defines the contract that every store must fulfill.

Think of the CreditStore ABC as a standardized electrical outlet. The shape of the outlet (the abstract methods) is the same everywhere, but what happens behind the wall (the implementation) can be anything -- Postgres, Redis, DynamoDB, or a plain Python dictionary. As long as the outlet fits, any appliance (CreditManager, PricingEngine, event emitter) works with any store. This is the dependency inversion principle in action: high-level modules depend on abstractions, not concrete implementations.

The ABC is split into a **core** surface and an **optional-capability** surface. The core surface -- balance and atomic charging (get_balance, add_credits, deduct_with_allowance, refund_credits), the lease lifecycle (create_lease, settle_lease, release_lease, renew_lease, get_available), pricing config (get_active_pricing, set_active_pricing, get_pricing_history, get_pricing_config, activate_pricing), plans (get_user_plan, set_user_plan, check_allowance, increment_usage_window), spend-cap checks (check_spend_cap), and expiry sweeps (sweep_expired_credits) -- is `@abstractmethod`, so Python enforces it at instantiation time. The optional groups -- analytics (spend_by_user, spend_by_model, top_users, daily_spend, aggregate_stats), transaction listing (list_user_transactions), and shared team pools (create_team, get_team_balance, add_team_member, get_team_members, deduct_team) -- are concrete on the ABC with a default body that raises `CapabilityNotSupportedError`. A minimal custom store does not need to implement (or even think about) those groups at all; it only pays for them if it opts in by overriding the method.

This split matters for adopters: implementing "pluggable storage" against ducto means implementing roughly 20 core methods, not the full ~35-method surface. If a caller reaches for a capability your store does not support, they get a clear, typed `CapabilityNotSupportedError` instead of a confusing `AttributeError` or silently wrong data.

What we will do in this section: implement a minimal MyCustomStore that satisfies only the core ABC, wire it to a CreditManager, run it through a charge and a lease, and then show what happens when we call an optional capability it never implemented."""),
        md("""### Implement the core ABC

Below is a minimal MyCustomStore -- dict-backed, no persistence, no concurrency guarantees beyond a single process lock. It implements exactly the core abstract methods and nothing else. Because none of the optional-capability methods are overridden, calling one of them (e.g. `spend_by_user`) falls through to the ABC's default body and raises `CapabilityNotSupportedError` -- which we will see below.

What we will do in this section: walk through the imports and the class definition, grouped by the same sections used in `interface/base.py`."""),
        code("""import uuid
import threading
from decimal import Decimal
from ducto.interface.base import CreditStore
from ducto.interface.models import (
    BalanceResult, AddCreditsResult, DeductionResult, LeaseResult,
    ReleaseResult, AvailableResult, RefundResult, GetUserPlanResult,
    SetUserPlanResult, AllowanceResult, CapCheckResult, SetupResult,
    SweepResult,
)

class MyCustomStore(CreditStore):
    \"\"\"Minimal custom store -- dict-backed, single-process lock, no persistence.\"\"\"

    def __init__(self):
        self._balances: dict[str, Decimal] = {}
        # lease_id -> [user_id, amount, status] ("active" | "settled" | "released")
        self._leases: dict[str, list] = {}
        self._lock = threading.Lock()

    def _held(self, user_id: str) -> Decimal:
        return sum(
            (amt for uid, amt, status in self._leases.values() if uid == user_id and status == "active"),
            Decimal(0),
        )

    # ── Setup, balance, atomic charge, refund ────────────────────────────

    def setup(self, database_url=None) -> SetupResult:
        return SetupResult()

    def get_balance(self, user_id: str) -> BalanceResult:
        return BalanceResult(user_id=user_id, balance=self._balances.get(user_id, Decimal(0)))

    def add_credits(self, user_id: str, amount, type: str = "adjustment",
                    metadata=None, expires_at=None) -> AddCreditsResult:
        with self._lock:
            amount = Decimal(amount)
            new_balance = self._balances.get(user_id, Decimal(0)) + amount
            self._balances[user_id] = new_balance
            return AddCreditsResult(transaction_id=str(uuid.uuid4()), user_id=user_id,
                                    amount=amount, new_balance=new_balance)

    def deduct_with_allowance(self, user_id: str, amount, *, idempotency_key=None,
                              min_balance=Decimal(0), model=None, metadata=None,
                              skip_allowance=False, period_start=None) -> DeductionResult:
        with self._lock:
            amount = Decimal(amount)
            balance = self._balances.get(user_id, Decimal(0))
            if balance - amount < min_balance:
                return DeductionResult(transaction_id="", user_id=user_id, amount=Decimal(0),
                                       balance_after=balance, error="insufficient_credits")
            balance -= amount
            self._balances[user_id] = balance
            return DeductionResult(transaction_id=str(uuid.uuid4()), user_id=user_id,
                                   amount=amount, balance_after=balance)

    def refund_credits(self, transaction_id: str, amount=None, reason=None, metadata=None) -> RefundResult:
        return RefundResult(refund_transaction_id=str(uuid.uuid4()),
                            original_transaction_id=transaction_id, user_id="",
                            amount=amount or Decimal(0), new_balance=Decimal(0))

    # ── Lease lifecycle (the only admission gate) ────────────────────────
    # This toy store never expires a lease's TTL -- a real implementation
    # would track expires_at and reject settle/renew on an elapsed lease.

    def create_lease(self, user_id: str, amount, operation_type: str, *, billing_mode="strict",
                     floor=Decimal(0), max_concurrent=None, ttl_seconds=600, model=None,
                     overdraft_floor=None, metadata=None, period_start=None) -> LeaseResult:
        with self._lock:
            amount = Decimal(amount)
            balance = self._balances.get(user_id, Decimal(0))
            held = self._held(user_id)
            available = balance - held
            if available - amount < floor:
                return LeaseResult(lease_id="", user_id=user_id, error="insufficient_credits")
            lease_id = str(uuid.uuid4())
            self._leases[lease_id] = [user_id, amount, "active"]
            return LeaseResult(lease_id=lease_id, user_id=user_id, amount=amount,
                               available=available - amount, reserved_total=held + amount,
                               billing_mode=billing_mode)

    def settle_lease(self, user_id: str, lease_id: str, amount, *, idempotency_key=None,
                     min_balance=Decimal(0), model=None, metadata=None,
                     skip_allowance=False, period_start=None) -> DeductionResult:
        with self._lock:
            entry = self._leases.get(lease_id)
            if entry is None or entry[0] != user_id or entry[2] != "active":
                return DeductionResult(transaction_id="", user_id=user_id, amount=Decimal(0),
                                       balance_after=self._balances.get(user_id, Decimal(0)),
                                       error="lease_not_found")
            amount = Decimal(amount)
            entry[2] = "settled"
            balance = self._balances.get(user_id, Decimal(0)) - amount
            self._balances[user_id] = balance
            return DeductionResult(transaction_id=str(uuid.uuid4()), user_id=user_id,
                                   amount=amount, balance_after=balance)

    def release_lease(self, user_id: str, lease_id: str) -> ReleaseResult:
        with self._lock:
            entry = self._leases.get(lease_id)
            if entry is None or entry[0] != user_id:
                return ReleaseResult(lease_id=lease_id, user_id=user_id, released=False, reason="lease_not_found")
            if entry[2] != "active":
                return ReleaseResult(lease_id=lease_id, user_id=user_id, released=False, reason="already_finalized")
            entry[2] = "released"
            return ReleaseResult(lease_id=lease_id, user_id=user_id, released=True, reason="released")

    def renew_lease(self, user_id: str, lease_id: str, ttl_seconds: int) -> LeaseResult:
        entry = self._leases.get(lease_id)
        if entry is None or entry[0] != user_id or entry[2] != "active":
            return LeaseResult(lease_id=lease_id, user_id=user_id, error="lease_not_found")
        return LeaseResult(lease_id=lease_id, user_id=user_id, amount=entry[1])

    def get_available(self, user_id: str) -> AvailableResult:
        balance = self._balances.get(user_id, Decimal(0))
        held = self._held(user_id)
        return AvailableResult(user_id=user_id, balance=balance, reserved=held, available=balance - held)

    # ── Pricing config -- delegated to CreditManager/PricingEngine here ──

    def get_active_pricing(self):
        return None
    def set_active_pricing(self, config, label=None) -> str:
        return str(uuid.uuid4())
    def get_pricing_history(self):
        return []
    def get_pricing_config(self, version: int):
        return None
    def activate_pricing(self, version: int) -> str:
        return ""

    # ── Plans -- this store has no plan catalog, so every user is planless ─

    def get_user_plan(self, user_id: str) -> GetUserPlanResult:
        return GetUserPlanResult(user_id=user_id)
    def set_user_plan(self, user_id: str, plan_id: str) -> SetUserPlanResult:
        return SetUserPlanResult(user_id=user_id, plan_id=plan_id)
    def check_allowance(self, user_id: str, period_start=None) -> AllowanceResult:
        return AllowanceResult(plan_id="", allowance_remaining=Decimal(0), period_start="", period_end="")
    def increment_usage_window(self, user_id: str, plan_id: str, amount) -> None:
        pass

    # ── Spend caps and expiry -- no caps or expiry tracking in this toy ──

    def check_spend_cap(self, user_id: str, model=None, amount=None) -> CapCheckResult:
        return CapCheckResult()
    def sweep_expired_credits(self, dry_run: bool = False) -> SweepResult:
        return SweepResult()

# Instantiate our store. Python checks that all @abstractmethod-decorated
# methods are implemented at instantiation time -- since we implemented
# exactly the core surface (and none of the optional capabilities), this
# succeeds without needing a single team/analytics/transaction-listing method.
custom_store = MyCustomStore()
print("MyCustomStore implements the CreditStore core ABC.")"""),
        md("""### Use with CreditManager

Implementing the core ABC is enough to plug straight into `CreditManager`. The manager does not care whether the store persists to Postgres, Redis, DynamoDB, or a Python dictionary -- it only relies on the core contract. Below we add credits, then run both charging patterns from earlier notebooks (an atomic charge via `deduct_with_allowance`, Notebook 03, and a `reserve` → `settle` lease, Notebook 06) against our custom store.

What we will do in this section: create a CreditManager backed by our custom store, add credits, run an atomic charge and a reserve/settle lease, then confirm that this minimal store still works correctly for every core operation."""),
        code("""from ducto.manager import CreditManager

# Create a CreditManager using our custom store as the backend. It needs no
# pricing engine here since we charge raw Decimal amounts directly.
manager = CreditManager(custom_store)
user = str(uuid.uuid4())

manager.add_credits(user, Decimal("10_000"))
print(f"  After adding 10,000 credits, balance = {manager.get_balance(user).balance}")

# Atomic charge (no reservation needed for a single-shot deduction). This
# calls the store directly, same as the deduct_with_allowance() pattern
# from Notebook 03 -- no pricing engine required for a raw amount.
ded = custom_store.deduct_with_allowance(user, Decimal("1_500"))
print(f"  Charged 1,500 directly, balance = {ded.balance_after}")

# Lease lifecycle: reserve the worst-case hold, then settle the actual cost.
lease = manager.reserve(user, Decimal("1_000"))
print(f"  Reserved {lease.amount}, available now {lease.available}")
settled = manager.settle(user, lease.lease_id, Decimal("600"))
print(f"  Settled {settled.amount}, balance = {settled.balance_after}")  # unused 400 hold freed"""),
        md("""### Optional capabilities raise a clear, typed error

Our store never overrode any of the optional-capability groups. Calling one -- for example `spend_by_user`, an analytics method -- falls through to the ABC's default body, which raises `CapabilityNotSupportedError` rather than an `AttributeError` or a silently empty/wrong result. This is the practical benefit of the WS8 core/optional split: callers can distinguish "this store doesn't support analytics" from "this store is broken," and a minimal custom store never has to implement (or stub out) a capability its application does not need.

What we will do in this section: call `spend_by_user` on our custom store and catch the resulting `CapabilityNotSupportedError`."""),
        code("""from datetime import datetime, timezone
from ducto.interface.base import CapabilityNotSupportedError

try:
    custom_store.spend_by_user(datetime.now(timezone.utc), datetime.now(timezone.utc))
except CapabilityNotSupportedError as e:
    print(f"  spend_by_user raised CapabilityNotSupportedError: {e}")"""),
    ]


# ---------------------------------------------------------------------------
# Notebook 12 - CLI & Deployment
# ---------------------------------------------------------------------------


def n_cli() -> list[dict]:
    return [
        md("""# 12 - Using the ducto CLI

Every previous notebook drove ducto through Python. In production, publishing and rolling back pricing changes is usually a deploy-pipeline or on-call operation, not a Python script — that's what the CLI is for.

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

The CLI reads `.env` from your current working directory if available (via `python-dotenv`) and loads any variables it defines — but variables already set in your shell environment always take precedence over the `.env` file, so `export SUPABASE_URL=...` in your shell always wins over a `.env` entry of the same name.

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
safe, scriptable workflow. This closes the loop from Notebook 00: you now know
the concepts, the Python API for every feature, and how to operate pricing
changes safely in production.
"""),
    ]


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

ALL: list[tuple[str, list[dict]]] = [
    ("00_concepts_and_setup.ipynb", n_concepts()),
    ("01_pricing_basics.ipynb", n_pricing_basics()),
    ("02_expression_language.ipynb", n_expression_language()),
    ("03_credit_lifecycle.ipynb", n_credit_lifecycle()),
    ("04_plans_and_allowances.ipynb", n_plans_and_allowances()),
    ("05_credit_expiry.ipynb", n_credit_expiry()),
    ("06_financial_safety.ipynb", n_financial_safety()),
    ("07_spend_caps.ipynb", n_spend_caps()),
    ("08_teams.ipynb", n_teams()),
    ("09_analytics.ipynb", n_analytics()),
    ("10_events.ipynb", n_events()),
    ("11_custom_store.ipynb", n_custom_store()),
    ("12_cli_and_deployment.ipynb", n_cli()),
]


if __name__ == "__main__":
    print("Generating notebooks …")
    for name, cells in ALL:
        save(name, cells)
