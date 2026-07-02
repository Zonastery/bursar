/**
 * Adversarial / financial-safety tests for credit tiers (MemoryStore).
 *
 * Mirror of `lease-adversarial.test.ts`'s rigor, applied to the tier-priority
 * walk (deduct/settle), LIFO refund restoration, and per-tier expiry sweep
 * introduced by the credit-tiers feature. Attacks the money invariants from
 * every angle: config drift (a tier definition removed while a user still
 * holds balance under the old key), exact-Decimal no-rounding-drift across a
 * multi-tier walk, conservation of money under a sequence of interleaved
 * grants/deducts, team-pool isolation, and composing 3+ partial refunds
 * without ever over-restoring a tier.
 *
 * Concurrency note (repo `CLAUDE.md`): JS is single-threaded and the
 * in-memory store has no `await` inside its critical sections, so
 * `Promise.all([...])` of store calls runs sequentially (no true
 * preemption). Tests below fire many calls "concurrently" and assert
 * invariants (conservation of money, no over/under-restoration) rather than
 * asserting a specific interleaving/race outcome.
 */

import { describe, it, expect } from "vitest";
import Decimal from "decimal.js";
import { MemoryStore } from "../src/stores/memory-store.js";
import type { TierDefinition } from "../src/types.js";

const D = (n: number | string) => new Decimal(n);

/** White-box: reach into MemoryStore's private tier-definition map (mirrors
 * `lease-adversarial.test.ts`'s `expireLease` helper reaching into `reservations`). */
function tierDefinitionsOf(store: MemoryStore): Map<string, TierDefinition> {
  return (store as unknown as { tierDefinitions: Map<string, TierDefinition> }).tierDefinitions;
}

describe("Credit tiers — adversarial", () => {
  // ── 1. Config-drift safety net ─────────────────────────────────────────

  describe("config-drift safety net (orphaned tier keys)", () => {
    it("still fully drains an orphaned tier's balance, appended last, after its definition is removed", async () => {
      const store = new MemoryStore();
      await store.setActivePricing({
        models: { _default: "input_tokens * 1" },
        tiers: {
          gifted: { name: "Gifted", priority: 10, expires: false },
          purchased: { name: "Purchased", priority: 20, expires: false, isDefault: true },
        },
      });
      await store.addCredits("u1", D(10), "adjustment", null, null, "gifted");
      await store.addCredits("u1", D(5), "adjustment", null, null, "purchased");
      expect((await store.getBalance("u1")).balance.toString()).toBe("15");

      // Simulate the tier's config row being removed (e.g. a manual `DELETE
      // FROM credit_tiers` in Postgres). NOTE: republishing a pricing config
      // that simply omits a previously-defined tier does NOT do this —
      // `setActivePricing`/`sync_tiers_from_config` are upsert-only and never
      // delete a stale tier definition (see MemoryStore.setActivePricing and
      // 023_credit_tiers.sql's `sync_tiers_from_config`), so a tier can only
      // become "orphaned" via an out-of-band removal like this one.
      tierDefinitionsOf(store).delete("gifted");

      const r = await store.deductWithAllowance("u1", D(12));
      expect(r.error).toBeUndefined();
      // "purchased" (still configured, priority 20) is walked FIRST; the
      // orphaned "gifted" balance is appended LAST regardless of its
      // original priority (10) and still fully absorbs the remainder.
      expect(r.tierBreakdown?.purchased?.toString()).toBe("5");
      expect(r.tierBreakdown?.gifted?.toString()).toBe("7");

      // getCreditTiers only enumerates currently-CONFIGURED tiers, so the
      // orphaned "gifted" bucket no longer appears in the per-tier list —
      // but the aggregate total still correctly reflects the drain (no money
      // is lost or double-counted).
      const tiers = await store.getCreditTiers("u1");
      expect(tiers.tiers.map((t) => t.tierKey)).toEqual(["purchased"]);
      expect(tiers.tiers[0].balance.toString()).toBe("0");
      expect(tiers.totalBalance.toString()).toBe("3"); // 15 - 12
      expect((await store.getBalance("u1")).balance.toString()).toBe("3");
    });
  });

  // ── 2. No rounding drift across a multi-tier walk ──────────────────────

  describe("no rounding drift", () => {
    it("tierBreakdown values sum EXACTLY to net via Decimal equality, never floating point", async () => {
      const store = new MemoryStore();
      await store.setActivePricing({
        models: { _default: "input_tokens * 1" },
        tiers: {
          a: { name: "A", priority: 10, expires: false },
          b: { name: "B", priority: 20, expires: false },
          c: { name: "C", priority: 30, expires: false },
        },
      });
      await store.addCredits("u1", D("10.3333"), "adjustment", null, null, "a");
      await store.addCredits("u1", D("5.1111"), "adjustment", null, null, "b");
      await store.addCredits("u1", D("0.7778"), "adjustment", null, null, "c");

      const net = D("15.5");
      const r = await store.deductWithAllowance("u1", net);
      expect(r.error).toBeUndefined();

      const breakdown = r.tierBreakdown!;
      const sum = Object.values(breakdown).reduce((acc, v) => acc.plus(v), D(0));
      expect(sum.equals(net)).toBe(true);

      expect(breakdown.a.toString()).toBe("10.3333");
      expect(breakdown.b.toString()).toBe("5.1111");
      expect(breakdown.c.toString()).toBe("0.0556");

      const tiers = await store.getCreditTiers("u1");
      const byKey = Object.fromEntries(tiers.tiers.map((t) => [t.tierKey, t.balance]));
      expect(byKey.a.equals(D(0))).toBe(true);
      expect(byKey.b.equals(D(0))).toBe(true);
      expect(byKey.c.equals(D("0.7222"))).toBe(true);
      expect(tiers.totalBalance.equals(D("0.7222"))).toBe(true);
    });
  });

  // ── 3. Conservation of money under interleaved add/deduct ─────────────

  describe("conservation of money under interleaved operations", () => {
    it("sum of tier balances always equals the aggregate, which equals grants minus consumed", async () => {
      const store = new MemoryStore();
      await store.setActivePricing({
        models: { _default: "input_tokens * 1" },
        tiers: {
          a: { name: "A", priority: 10, expires: false },
          b: { name: "B", priority: 20, expires: false },
          c: { name: "C", priority: 30, expires: false },
        },
      });

      const grants: Array<[string, Decimal]> = [
        ["a", D(50)],
        ["b", D(30)],
        ["c", D(20)],
        ["a", D(10)],
        ["b", D(5)],
      ];
      let totalGranted = D(0);
      for (const [tier, amt] of grants) {
        await store.addCredits("u1", amt, "adjustment", null, null, tier);
        totalGranted = totalGranted.plus(amt);
      }

      // "Interleaved" deducts — fired via Promise.all (sequential in JS; see
      // module doc) so some may be admitted and some rejected depending on
      // the floor, exactly like a real interleaving would.
      const deductAmounts = [D(10), D(40), D(15), D(100), D(5)];
      const results = await Promise.all(
        deductAmounts.map((amt) => store.deductWithAllowance("u1", amt, { minBalance: D(0) })),
      );
      const totalConsumed = results
        .filter((r) => !r.error)
        .reduce((acc, r) => acc.plus(r.amount), D(0));

      const tiers = await store.getCreditTiers("u1");
      const sumTierBalances = tiers.tiers.reduce((acc, t) => acc.plus(t.balance), D(0));

      expect(sumTierBalances.equals(tiers.totalBalance)).toBe(true);
      expect(tiers.totalBalance.equals(totalGranted.minus(totalConsumed))).toBe(true);
      expect(tiers.totalBalance.gte(0)).toBe(true);
    });
  });

  // ── 4. Team pools untouched (out of scope per plan) ────────────────────

  describe("team pools untouched by tier configuration", () => {
    it("deductTeam behaves normally regardless of configured tiers", async () => {
      const store = new MemoryStore();
      await store.setActivePricing({
        models: { _default: "input_tokens * 1" },
        tiers: {
          gifted: { name: "Gifted", priority: 10, expires: false },
          purchased: { name: "Purchased", priority: 20, expires: false, isDefault: true },
        },
      });
      const team = await store.createTeam("Pool", D(500));
      await store.addTeamMember(team.teamId, "u1", "member");

      const result = await store.deductTeam(team.teamId, "u1", D(50));
      expect(result.error).toBeUndefined();
      expect(result.teamBalanceAfter.toString()).toBe("450");

      // deductTeam never touches the user's personal tier balances.
      const tiers = await store.getCreditTiers("u1");
      expect(tiers.totalBalance.toString()).toBe("0");
    });
  });

  // ── 5. Multiple partial refunds across 3+ tiers ────────────────────────

  describe("multiple partial refunds across 3+ tiers", () => {
    it("composes correctly without ever over-restoring any single tier", async () => {
      const store = new MemoryStore();
      await store.setActivePricing({
        models: { _default: "input_tokens * 1" },
        tiers: {
          a: { name: "A", priority: 10, expires: false },
          b: { name: "B", priority: 20, expires: false },
          c: { name: "C", priority: 30, expires: false },
        },
      });
      await store.addCredits("u1", D(30), "adjustment", null, null, "a");
      await store.addCredits("u1", D(20), "adjustment", null, null, "b");
      await store.addCredits("u1", D(10), "adjustment", null, null, "c");

      const deduct = await store.deductWithAllowance("u1", D(45));
      expect(deduct.error).toBeUndefined();
      expect(deduct.tierBreakdown?.a?.toString()).toBe("30");
      expect(deduct.tierBreakdown?.b?.toString()).toBe("15");
      expect(deduct.tierBreakdown?.c).toBeUndefined();

      const r1 = await store.refundCredits(deduct.transactionId, D(10));
      expect(r1.error).toBeUndefined();
      expect(r1.tierBreakdown?.b?.toString()).toBe("10");
      expect(r1.tierBreakdown?.a).toBeUndefined();

      const r2 = await store.refundCredits(deduct.transactionId, D(10));
      expect(r2.error).toBeUndefined();
      expect(r2.tierBreakdown?.b?.toString()).toBe("5");
      expect(r2.tierBreakdown?.a?.toString()).toBe("5");

      const r3 = await store.refundCredits(deduct.transactionId, D(15));
      expect(r3.error).toBeUndefined();
      expect(r3.tierBreakdown?.a?.toString()).toBe("15");
      expect(r3.tierBreakdown?.b).toBeUndefined();
      expect(r3.tierBreakdown?.c).toBeUndefined();

      const tiers = await store.getCreditTiers("u1");
      const byKey = Object.fromEntries(tiers.tiers.map((t) => [t.tierKey, t.balance.toString()]));
      expect(byKey).toEqual({ a: "20", b: "20", c: "10" });
      expect(tiers.totalBalance.toString()).toBe("50");

      // Never over-restored: cumulative per-tier refund never exceeds the
      // original per-tier debit breakdown ({a: 30, b: 15}).
      const totalARefunded = D(0).plus(r2.tierBreakdown!.a!).plus(r3.tierBreakdown!.a!);
      const totalBRefunded = D(0).plus(r1.tierBreakdown!.b!).plus(r2.tierBreakdown!.b!);
      expect(totalARefunded.lte(D(30))).toBe(true);
      expect(totalBRefunded.equals(D(15))).toBe(true); // exactly the original b debit, never more
      // c was never part of the original debit breakdown — never touched by any refund.
      expect(tiers.tiers.find((t) => t.tierKey === "c")?.balance.toString()).toBe("10");
    });
  });
});
