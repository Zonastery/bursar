"""Cross-SDK billing parity runner — Python side.

Loads billing_scenarios.json and exercises each scenario against the Python
MemoryStore+CreditManager. Results are saved to parity_results_py.json so the
JS runner can diff them, and the file is also used as a standalone pytest suite.

Run directly:  python tests/parity/run_parity.py
Run via pytest: pytest tests/parity/run_parity.py -v
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

PARITY_DIR = Path(__file__).parent
SCENARIOS_FILE = PARITY_DIR / "billing_scenarios.json"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_scenarios() -> dict:
    with open(SCENARIOS_FILE) as f:
        return json.load(f)


def _make_manager(pricing_cfg: dict, billing_mode: str = "strict", overdraft_floor: Decimal | None = None, min_balance: Decimal | None = None):
    """Build a CreditManager with MemoryStore pre-loaded with pricing."""
    from bursar import CreditManager
    from bursar.interface.memory import MemoryStore

    store = MemoryStore()
    kwargs: dict = {"store": store}
    if billing_mode == "overdraft":
        kwargs["policy"] = "overdraft"
        if overdraft_floor is not None:
            kwargs["overdraft_floor"] = overdraft_floor
    else:
        kwargs["policy"] = "strict_prepaid"

    m = CreditManager(**kwargs)
    m.publish_pricing_from_dict(pricing_cfg)
    return m, store


def _run_setup(store, manager, steps: list[dict]) -> None:
    from bursar.interface.models import CreditMetadata

    for step in steps:
        op = step["op"]
        if op == "add_credits":
            store.add_credits(step["user_id"], Decimal(step["amount"]))
        elif op == "set_user_plan":
            store.set_user_plan(step["user_id"], step["plan_key"])
        elif op == "create_team":
            # MemoryStore generates a UUID for team_id; patch both _teams and
            # _team_members to use the fixture's stable id for predictable tests.
            result = store.create_team(step["team_id"], Decimal(step["initial_balance"]))
            actual_id = result.team_id
            if actual_id != step["team_id"]:
                team_rec = store._teams.pop(actual_id)
                team_rec.id = step["team_id"]
                store._teams[step["team_id"]] = team_rec
                store._team_members[step["team_id"]] = store._team_members.pop(actual_id)
        elif op == "add_team_member":
            cap = Decimal(step["spend_cap"]) if step.get("spend_cap") else None
            store.add_team_member(step["team_id"], step["user_id"], spend_cap=cap)
        else:
            raise ValueError(f"Unknown setup op: {op}")


def _run_action(scenario: dict, pricing_cfg: dict) -> dict:
    """Execute the scenario action and return a normalised result dict."""
    from bursar import CreditManager, UsageMetrics
    from bursar.expr import evaluate_expression
    from bursar.interface.memory import MemoryStore

    action = scenario["action"]
    op = action["op"]

    # Determine billing mode from action or default
    billing_mode = action.get("billing_mode", "strict")
    overdraft_floor_raw = action.get("overdraft_floor")
    overdraft_floor = Decimal(overdraft_floor_raw) if overdraft_floor_raw is not None else None
    min_balance_raw = action.get("min_balance")
    min_balance_override = Decimal(min_balance_raw) if min_balance_raw is not None else None

    manager, store = _make_manager(
        pricing_cfg,
        billing_mode=billing_mode,
        overdraft_floor=overdraft_floor,
    )

    _run_setup(store, manager, scenario.get("setup", []))

    if op == "deduct":
        metrics = UsageMetrics(
            model=action.get("model", "_default"),
            input_tokens=action.get("input_tokens", 0),
            output_tokens=action.get("output_tokens", 0),
        )
        try:
            res = manager.deduct(action["user_id"], metrics)
            return {
                "balance_after": str(res.balance_after),
                "allowance_consumed": str(res.allowance_consumed),
                "plan_covered": getattr(res, "plan_covered", None),
                "error": res.error,
            }
        except Exception as e:
            return {"error": _error_code(e)}

    elif op == "deduct_idempotent":
        metrics = UsageMetrics(
            model=action.get("model", "_default"),
            input_tokens=action.get("input_tokens", 0),
            output_tokens=action.get("output_tokens", 0),
        )
        try:
            res1 = manager.deduct(action["user_id"], metrics, idempotency_key=action["idempotency_key"])
            res2 = manager.deduct(action["user_id"], metrics, idempotency_key=action["idempotency_key"])
            return {
                "balance_after": str(res1.balance_after),
                "idempotent_on_replay": res2.idempotent,
                "error": None,
            }
        except Exception as e:
            return {"error": _error_code(e)}

    elif op == "get_user_plan":
        res = store.get_user_plan(action["user_id"])
        return {
            "plan_id": res.plan_id,
            "billing_mode": res.default_billing_mode,
        }

    elif op == "lease_reserve_settle":
        user_id = action["user_id"]
        reserve_amt = Decimal(action["reserve_amount"])
        settle_amt = Decimal(action["settle_amount"])
        try:
            lease = manager.reserve(user_id, reserve_amt)
            min_bal = min_balance_override or (manager._engine.min_balance if manager._engine else Decimal(0))
            res = manager._store.settle_lease(
                user_id, lease.lease_id, settle_amt,
                min_balance=min_bal,
            )
            return {"balance_after": str(res.balance_after), "error": res.error}
        except Exception as e:
            return {"error": _error_code(e)}

    elif op == "lease_reserve_release":
        user_id = action["user_id"]
        reserve_amt = Decimal(action["reserve_amount"])
        try:
            lease = manager.reserve(user_id, reserve_amt)
            manager.release(user_id, lease.lease_id)
            available = manager.get_available(user_id)
            return {"available_after_release": str(available.available), "error": None}
        except Exception as e:
            return {"error": _error_code(e)}

    elif op == "team_deduct":
        metrics = UsageMetrics(
            model=action.get("model", "_default"),
            input_tokens=action.get("input_tokens", 0),
            output_tokens=action.get("output_tokens", 0),
        )
        try:
            res = manager.deduct_team(action["team_id"], action["user_id"], metrics)
            return {"team_balance_after": str(res.team_balance_after), "error": res.error}
        except Exception as e:
            return {"error": _error_code(e)}

    elif op == "team_deduct_twice":
        metrics1 = UsageMetrics(
            model="_default",
            input_tokens=action.get("input_tokens_1", 0),
            output_tokens=action.get("output_tokens_1", 0),
        )
        metrics2 = UsageMetrics(
            model="_default",
            input_tokens=action.get("input_tokens_2", 0),
            output_tokens=action.get("output_tokens_2", 0),
        )
        try:
            manager.deduct_team(action["team_id"], action["user_id"], metrics1)
        except Exception:
            pass
        try:
            manager.deduct_team(action["team_id"], action["user_id"], metrics2)
            return {"second_deduct_error": None}
        except Exception as e:
            return {"second_deduct_error": _error_code(e)}

    elif op == "deduct_then_refund":
        metrics = UsageMetrics(
            model="_default",
            input_tokens=action.get("input_tokens", 0),
            output_tokens=action.get("output_tokens", 0),
        )
        try:
            ded = manager.deduct(action["user_id"], metrics)
            ref = manager.refund_credits(ded.transaction_id)
            return {"balance_after_refund": str(ref.new_balance), "error": None}
        except Exception as e:
            return {"error": _error_code(e)}

    elif op == "evaluate_expr":
        from bursar.expr import evaluate_expression
        from bursar.engine import _q
        try:
            result = evaluate_expression(action["expr"], action["vars"])
            return {"result": str(_q(result)), "error": None}
        except Exception as e:
            return {"error": _error_code(e)}

    else:
        raise ValueError(f"Unknown action op: {op}")


def _error_code(exc: Exception) -> str:
    """Convert an exception to a short code matching the scenario's expected error."""
    name = type(exc).__name__
    mapping = {
        "InsufficientCreditsError": "insufficient_credits",
        "CapReachedError": "cap_reached",
        "InsufficientTeamBalanceError": "insufficient_team_balance",
        "SpendCapExceededError": "spend_cap_exceeded",
        "LeaseExpiredError": "lease_expired",
        "LeaseNotFoundError": "lease_not_found",
    }
    # Prefer explicit error code attribute.
    code = getattr(exc, "code", None) or getattr(exc, "error", None)
    if code:
        return str(code)
    # Some exceptions embed the specific code in the message (e.g. InsufficientCreditsError
    # raised for team deducts includes "spend_cap_exceeded" in the message text).
    msg = str(exc)
    for known_code in ("spend_cap_exceeded", "cap_reached", "insufficient_team_balance",
                       "lease_expired", "lease_not_found", "invalid_amount"):
        if known_code in msg:
            return known_code
    return mapping.get(name, name)


def _assert_scenario(scenario: dict, result: dict) -> list[str]:
    """Return a list of failure messages (empty = pass)."""
    failures = []
    expected = scenario.get("assert", {})
    for key, exp_val in expected.items():
        got = result.get(key)
        # Normalise Decimal strings for comparison
        if isinstance(exp_val, str) and "." in exp_val:
            try:
                exp_d = Decimal(exp_val)
                got_d = Decimal(str(got)) if got is not None else None
                if got_d is None or exp_d != got_d:
                    failures.append(f"  {key}: expected {exp_val!r}, got {got!r}")
                continue
            except Exception:
                pass
        if exp_val != got:
            failures.append(f"  {key}: expected {exp_val!r}, got {got!r}")
    return failures


# ---------------------------------------------------------------------------
# pytest parametrize
# ---------------------------------------------------------------------------

_DATA = _load_scenarios()
_PRICING = _DATA["pricing_config"]
_SCENARIOS = _DATA["scenarios"]


@pytest.mark.parametrize("scenario", _SCENARIOS, ids=[s["name"] for s in _SCENARIOS])
def test_billing_scenario(scenario: dict) -> None:
    result = _run_action(scenario, _PRICING)
    failures = _assert_scenario(scenario, result)
    if failures:
        pytest.fail(f"Scenario '{scenario['name']}' failed:\n" + "\n".join(failures) + f"\n  result: {result}")


# ---------------------------------------------------------------------------
# CLI entry point — writes parity_results_py.json for cross-runner diffing
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    data = _load_scenarios()
    pricing = data["pricing_config"]
    scenarios = data["scenarios"]

    results = {}
    errors = []
    for sc in scenarios:
        result = _run_action(sc, pricing)
        failures = _assert_scenario(sc, result)
        results[sc["name"]] = result
        if failures:
            errors.append(f"FAIL {sc['name']}:\n" + "\n".join(failures))
        else:
            print(f"  PASS {sc['name']}")

    out_file = PARITY_DIR / "parity_results_py.json"
    with open(out_file, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nResults written to {out_file}")

    if errors:
        print("\nFAILURES:")
        for e in errors:
            print(e)
        sys.exit(1)
    else:
        print(f"\nAll {len(scenarios)} scenarios passed.")
