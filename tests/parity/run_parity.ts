/**
 * Cross-SDK billing parity runner — JavaScript/TypeScript side.
 *
 * Loads billing_scenarios.json and exercises each scenario against the JS
 * MemoryStore + CreditManager. Results are compared field-by-field against the
 * expected values in the JSON. Any field-level divergence is a test failure.
 *
 * Run via: cd javascript && npx vitest run ../tests/parity/run_parity.ts
 */
import { describe, it, expect } from "vitest";
import { readFileSync } from "fs";
import { resolve, dirname } from "path";
import { fileURLToPath } from "url";
import { CreditManager } from "../../javascript/src/manager.js";
import { MemoryStore } from "../../javascript/src/stores/memory-store.js";
import { evaluateExpression } from "../../javascript/src/expr.js";
import Decimal from "decimal.js";
import {
  InsufficientCreditsError,
  CapReachedError,
  LeaseExpiredError,
  LeaseNotFoundError,
} from "../../javascript/src/errors.js";
import type {
  PricingConfigData,
} from "../../javascript/src/types.js";

// ---------------------------------------------------------------------------
// Load scenario fixture
// ---------------------------------------------------------------------------

const __filename = fileURLToPath(import.meta.url);
const __dir = dirname(__filename);
const SCENARIOS_FILE = resolve(__dir, "billing_scenarios.json");
const _data = JSON.parse(readFileSync(SCENARIOS_FILE, "utf-8"));
const PRICING: PricingConfigData = _data.pricing_config;
const SCENARIOS: Array<Record<string, unknown>> = _data.scenarios;

const D = (v: string | number) => new Decimal(v);

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function q(v: Decimal): string {
  return v.toDecimalPlaces(4, Decimal.ROUND_HALF_UP).toFixed(4);
}

function errorCode(err: unknown): string {
  if (err instanceof Error) {
    // Check if the message embeds a known code.
    const knownCodes = [
      "spend_cap_exceeded",
      "cap_reached",
      "insufficient_team_balance",
      "lease_expired",
      "lease_not_found",
      "invalid_amount",
    ];
    for (const code of knownCodes) {
      if (err.message.includes(code)) return code;
    }
    if (err instanceof InsufficientCreditsError) return "insufficient_credits";
    if (err instanceof CapReachedError) return "cap_reached";
    if (err instanceof LeaseExpiredError) return "lease_expired";
    if (err instanceof LeaseNotFoundError) return "lease_not_found";
    return err.constructor.name;
  }
  return String(err);
}

function assertFields(
  scenarioName: string,
  expected: Record<string, unknown>,
  got: Record<string, unknown>,
): void {
  const failures: string[] = [];
  for (const [key, expVal] of Object.entries(expected)) {
    const gotVal = got[key];
    // Numeric string comparison: normalise both sides to 4 d.p.
    if (typeof expVal === "string" && expVal.includes(".")) {
      try {
        const expD = D(expVal);
        const gotD = gotVal != null ? D(String(gotVal)) : null;
        if (gotD === null || !expD.eq(gotD)) {
          failures.push(`  ${key}: expected '${expVal}', got '${String(gotVal)}'`);
        }
        continue;
      } catch {
        // fall through to strict equality
      }
    }
    if (expVal !== gotVal) {
      failures.push(`  ${key}: expected ${JSON.stringify(expVal)}, got ${JSON.stringify(gotVal)}`);
    }
  }
  if (failures.length > 0) {
    throw new Error(
      `Scenario '${scenarioName}' failed:\n${failures.join("\n")}\n  result: ${JSON.stringify(got)}`,
    );
  }
}

// ---------------------------------------------------------------------------
// Setup helpers
// ---------------------------------------------------------------------------

async function runSetup(
  store: MemoryStore,
  steps: Array<Record<string, unknown>>,
): Promise<void> {
  for (const step of steps) {
    const op = step["op"] as string;
    if (op === "add_credits") {
      const amt = D(step["amount"] as string);
      // Skip zero-amount adds (just ensure the user has a balance entry of 0).
      if (amt.isZero()) {
        // Force a balance entry to exist so the user is "known" but has 0 balance.
        // addCredits would reject 0; for zero-balance overdraft tests we just skip.
        return;
      }
      await store.addCredits(step["user_id"] as string, amt);
    } else if (op === "set_user_plan") {
      await store.setUserPlan(step["user_id"] as string, step["plan_key"] as string);
    } else if (op === "create_team") {
      // MemoryStore generates a UUID; patch internal maps to stable fixture id.
      // We use `as any` because `teams` / `teamMembers` are private.
      const result = await store.createTeam(
        step["team_id"] as string,
        D(step["initial_balance"] as string),
      );
      const actualId = result.teamId;
      const fixtureId = step["team_id"] as string;
      if (actualId !== fixtureId) {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const s = store as any;
        const teamRec = s.teams.get(actualId)!;
        s.teams.delete(actualId);
        teamRec.id = fixtureId;
        s.teams.set(fixtureId, teamRec);
        const memberMap = s.teamMembers.get(actualId)!;
        s.teamMembers.delete(actualId);
        s.teamMembers.set(fixtureId, memberMap);
      }
    } else if (op === "add_team_member") {
      const cap = step["spend_cap"] ? D(step["spend_cap"] as string) : undefined;
      await store.addTeamMember(
        step["team_id"] as string,
        step["user_id"] as string,
        "member",
        cap,
      );
    } else {
      throw new Error(`Unknown setup op: ${op}`);
    }
  }
}

// ---------------------------------------------------------------------------
// Action runner
// ---------------------------------------------------------------------------

async function runAction(
  scenario: Record<string, unknown>,
  pricing: PricingConfigData,
): Promise<Record<string, unknown>> {
  const action = scenario["action"] as Record<string, unknown>;
  const op = action["op"] as string;

  // Mirror Python runner: create manager with billing mode/floor from action.
  const actionBillingMode = (action["billing_mode"] as string | undefined) ?? "strict";
  const actionOverdraftFloorRaw = action["overdraft_floor"] as string | number | undefined;
  // Pass overdraftFloor as a number to avoid Decimal instanceof cross-module issues.
  const actionOverdraftFloorNum = actionOverdraftFloorRaw != null
    ? parseFloat(String(actionOverdraftFloorRaw))
    : undefined;
  const policy = actionBillingMode === "overdraft" ? "overdraft" : "strict_prepaid";

  const store = new MemoryStore();
  const manager = new CreditManager(store, undefined, undefined, {
    policy,
    ...(actionOverdraftFloorNum !== undefined ? { overdraftFloor: actionOverdraftFloorNum } : {}),
  });
  await manager.publishPricingFromDict(pricing);

  await runSetup(store, (scenario["setup"] as Array<Record<string, unknown>>) ?? []);

  if (op === "deduct" || op === "deduct_idempotent") {
    const metrics = {
      model: (action["model"] as string | undefined) ?? "_default",
      inputTokens: (action["input_tokens"] as number | undefined) ?? 0,
      outputTokens: (action["output_tokens"] as number | undefined) ?? 0,
    };
    try {
      if (op === "deduct_idempotent") {
        const ikey = action["idempotency_key"] as string;
        const r1 = await manager.deduct(action["user_id"] as string, metrics, ikey);
        const r2 = await manager.deduct(action["user_id"] as string, metrics, ikey);
        return {
          balance_after: q(r1.balanceAfter),
          idempotent_on_replay: r2.idempotent,
          error: null,
        };
      } else {
        const r = await manager.deduct(action["user_id"] as string, metrics);
        return {
          balance_after: q(r.balanceAfter),
          allowance_consumed: r.allowanceConsumed.toFixed(),
          plan_covered: r.planCovered ?? null,
          error: r.error ?? null,
        };
      }
    } catch (e) {
      if (process.env["PARITY_DEBUG"]) {
        console.error(`[${op}] error:`, e);
        if (e instanceof Error) console.error("stack:", e.stack?.split("\n").slice(0,5).join("\n"));
      }
      return { error: errorCode(e) };
    }
  }

  if (op === "get_user_plan") {
    const r = await store.getUserPlan(action["user_id"] as string);
    return {
      plan_id: r.planId,
      billing_mode: r.defaultBillingMode ?? "strict",
    };
  }

  if (op === "insufficient_credits_strict") {
    // handled via deduct path — but this scenario uses "deduct" op
    return {};
  }

  if (op === "lease_reserve_settle") {
    const userId = action["user_id"] as string;
    // Use numbers to avoid cross-module Decimal instanceof issues with isAmount().
    const reserveAmt = parseFloat(action["reserve_amount"] as string);
    const settleAmtDecimal = D(action["settle_amount"] as string);
    const minBalanceDecimal = action["min_balance"]
      ? D(action["min_balance"] as string)
      : undefined;
    try {
      if (process.env["PARITY_DEBUG"]) {
        const bal = await store.getBalance(userId);
        console.log(`[lease] userId=${userId} balance=${bal.balance} reserveAmt=${reserveAmt} policy=${policy} floor=${actionOverdraftFloorNum}`);
      }
      const lease = await manager.reserve(userId, reserveAmt);
      const r = await store.settleLease(userId, lease.leaseId, settleAmtDecimal, {
        minBalance: minBalanceDecimal ?? new Decimal(0),
      });
      return {
        balance_after: q(r.balanceAfter),
        error: r.error ?? null,
      };
    } catch (e) {
      if (process.env["PARITY_DEBUG"]) console.error(`[lease_reserve_settle] error:`, e);
      return { error: errorCode(e) };
    }
  }

  if (op === "lease_reserve_release") {
    const userId = action["user_id"] as string;
    const reserveAmt = parseFloat(action["reserve_amount"] as string);
    try {
      const lease = await manager.reserve(userId, reserveAmt);
      await manager.release(userId, lease.leaseId);
      const avail = await manager.getAvailable(userId);
      return {
        available_after_release: q(avail.available),
        error: null,
      };
    } catch (e) {
      return { error: errorCode(e) };
    }
  }

  if (op === "team_deduct") {
    const metrics = {
      model: (action["model"] as string | undefined) ?? "_default",
      inputTokens: (action["input_tokens"] as number | undefined) ?? 0,
      outputTokens: (action["output_tokens"] as number | undefined) ?? 0,
    };
    try {
      const r = await manager.deductTeam(
        action["team_id"] as string,
        action["user_id"] as string,
        metrics,
      );
      return {
        team_balance_after: q(r.teamBalanceAfter),
        error: r.error ?? null,
      };
    } catch (e) {
      return { error: errorCode(e) };
    }
  }

  if (op === "team_deduct_twice") {
    const metrics1 = {
      model: "_default",
      inputTokens: (action["input_tokens_1"] as number | undefined) ?? 0,
      outputTokens: (action["output_tokens_1"] as number | undefined) ?? 0,
    };
    const metrics2 = {
      model: "_default",
      inputTokens: (action["input_tokens_2"] as number | undefined) ?? 0,
      outputTokens: (action["output_tokens_2"] as number | undefined) ?? 0,
    };
    try {
      await manager.deductTeam(action["team_id"] as string, action["user_id"] as string, metrics1);
    } catch {
      // ignore first deduct failure
    }
    try {
      await manager.deductTeam(action["team_id"] as string, action["user_id"] as string, metrics2);
      return { second_deduct_error: null };
    } catch (e) {
      return { second_deduct_error: errorCode(e) };
    }
  }

  if (op === "deduct_then_refund") {
    const metrics = {
      model: "_default",
      inputTokens: (action["input_tokens"] as number | undefined) ?? 0,
      outputTokens: (action["output_tokens"] as number | undefined) ?? 0,
    };
    try {
      const ded = await manager.deduct(action["user_id"] as string, metrics);
      const ref = await manager.refundCredits(ded.transactionId);
      return {
        balance_after_refund: q(ref.newBalance),
        error: null,
      };
    } catch (e) {
      return { error: errorCode(e) };
    }
  }

  if (op === "evaluate_expr") {
    const vars = action["vars"] as Record<string, number>;
    const decVars: Record<string, Decimal> = {};
    for (const [k, v] of Object.entries(vars)) {
      decVars[k] = new Decimal(v);
    }
    try {
      const result = evaluateExpression(action["expr"] as string, decVars);
      return { result: q(result), error: null };
    } catch (e) {
      return { error: errorCode(e) };
    }
  }

  throw new Error(`Unknown action op: ${op}`);
}

// ---------------------------------------------------------------------------
// Vitest test suite
// ---------------------------------------------------------------------------

describe("Cross-SDK billing parity (JS side)", () => {
  for (const scenario of SCENARIOS) {
    const name = scenario["name"] as string;
    it(name, async () => {
      const result = await runAction(scenario, PRICING);
      const expected = scenario["assert"] as Record<string, unknown>;
      assertFields(name, expected, result);
    });
  }
});
