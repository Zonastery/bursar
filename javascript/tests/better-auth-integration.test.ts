import { describe, expect, it, vi } from "vitest";

import type { Bursar } from "../src/bursar.js";
import { bursarBetterAuth, type BetterAuthBursarUser } from "../src/integrations/better-auth.js";
import { dodoBetterAuthCustomer } from "../src/providers/dodo/better-auth.js";

type BursarFacade = Pick<Bursar, "accounts" | "requireBilling">;

interface InitializedBursarPlugin {
  options: {
    databaseHooks: {
      user: {
        create: { after(user: BetterAuthBursarUser): Promise<void> };
        update: { after(user: BetterAuthBursarUser): Promise<void> };
      };
    };
  };
}

function betterAuthUser(
  overrides: Partial<Pick<BetterAuthBursarUser, "dodoCustomerId">> = {},
): BetterAuthBursarUser {
  return {
    id: "user-1",
    name: "Ada Lovelace",
    email: "ada@example.com",
    emailVerified: true,
    createdAt: new Date("2026-01-01T00:00:00.000Z"),
    updatedAt: new Date("2026-01-01T00:00:00.000Z"),
    ...overrides,
  };
}

function testBursarFacade<TFacade>(facade: TFacade): BursarFacade {
  // SAFETY: This fixture implements the accounts and billing methods exercised by the hook test.
  return facade as BursarFacade;
}

function initializedBursarPlugin<TValue>(value: TValue): InitializedBursarPlugin {
  // SAFETY: The Bursar plugin returns the database hook shape asserted by this test.
  return value as InitializedBursarPlugin;
}

describe("bursarBetterAuth", () => {
  it("provisions the Bursar account and synchronizes an official Dodo customer field", async () => {
    const onAccountCreated = vi.fn().mockResolvedValue({
      accountId: "user-1",
      planKey: "seeker",
      planAssigned: true,
      grants: [],
    });
    const upsertCustomer = vi.fn().mockResolvedValue(undefined);
    const plugin = bursarBetterAuth({
      getBursar: async () =>
        testBursarFacade({
          accounts: { onAccountCreated },
          requireBilling: () => ({ upsertCustomer }),
        }),
      resolveProviderCustomer: dodoBetterAuthCustomer,
    });
    // SAFETY: This isolated test does not execute the Better Auth context; the Bursar plugin ignores it.
    const initialized = initializedBursarPlugin(await plugin.init?.({} as never));
    const user = betterAuthUser({ dodoCustomerId: "cus_official_adapter" });

    await initialized.options.databaseHooks.user.create.after(user);

    expect(onAccountCreated).toHaveBeenCalledWith({
      accountId: "user-1",
      eventKey: "better-auth:user.created:user-1",
    });
    expect(upsertCustomer).toHaveBeenCalledWith(
      "dodo",
      "cus_official_adapter",
      "user-1",
      "ada@example.com",
    );

    await initialized.options.databaseHooks.user.update.after(
      betterAuthUser({ dodoCustomerId: "cus_updated" }),
    );
    expect(onAccountCreated).toHaveBeenCalledTimes(1);
    expect(upsertCustomer).toHaveBeenLastCalledWith(
      "dodo",
      "cus_updated",
      "user-1",
      "ada@example.com",
    );
  });

  it("does not create a provider mapping until the auth adapter exposes one", async () => {
    expect(dodoBetterAuthCustomer(betterAuthUser())).toBeNull();
  });
});
