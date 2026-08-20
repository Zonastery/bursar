import { Decimal } from "decimal.js";
import { describe, expect, it, vi } from "vitest";

import { CreditsService } from "../src/credits/service.js";
import type { CreditStore } from "../src/credits/store.js";

type Mutation = { amount: Decimal; type: string };

function testStore(store: Partial<CreditStore>): CreditStore {
  // SAFETY: This fixture implements the addCredits method exercised below.
  return store as CreditStore;
}

function seededSixPlaceAmounts(seed: number, count: number): string[] {
  let state = seed >>> 0;
  return Array.from({ length: count }, () => {
    // The generator is deliberately deterministic; randomness never decides
    // whether a financial assertion runs or gets skipped.
    state = (Math.imul(state, 1_664_525) + 1_013_904_223) >>> 0;
    return new Decimal((state % 50_000_000) + 1).div(1_000_000).toFixed(6);
  });
}

describe("financial amount invariants", () => {
  it("conserves exact six-place amounts across paired grants and debits", async () => {
    const mutations: Mutation[] = [];
    const addCredits = vi.fn(
      async (userId: string, amount: Decimal, options: { type?: string }) => {
        mutations.push({ amount, type: options.type ?? "adjustment" });
        return {
          entryId: `entry-${mutations.length}`,
          userId,
          amount,
          newBalance: new Decimal(1_000).plus(amount),
          lifetimePurchased: new Decimal(1_000),
          bucket: "purchased",
          idempotent: false,
        };
      },
    );
    const credits = new CreditsService(testStore({ addCredits }));
    const amounts = seededSixPlaceAmounts(0xc0ffee, 64);

    for (const [index, value] of amounts.entries()) {
      await credits.addCredits("user-1", value, {
        type: "purchase",
        idempotencyKey: `grant-${index}`,
      });
      await credits.deductCredits("user-1", value, {
        entryType: "refund_clawback",
        idempotencyKey: `debit-${index}`,
      });
    }

    expect(mutations).toHaveLength(amounts.length * 2);
    for (const [index, value] of amounts.entries()) {
      const grant = mutations[index * 2];
      const debit = mutations[index * 2 + 1];
      if (!grant || !debit) throw new Error(`missing mutation pair ${index}`);

      expect(grant.type).toBe("purchase");
      expect(debit.type).toBe("refund_clawback");
      expect(grant.amount.toFixed(6)).toBe(value);
      expect(debit.amount.toFixed(6)).toBe(`-${value}`);
      expect(grant.amount.plus(debit.amount).isZero()).toBe(true);
    }
  });
});
