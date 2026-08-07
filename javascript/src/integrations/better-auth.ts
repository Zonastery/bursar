import type { BetterAuthPlugin, User } from "better-auth";

import type { AccountCreatedInput, Bursar } from "../bursar.js";

export type BetterAuthBursarUser = User & Record<string, unknown>;

export interface BetterAuthProviderCustomer {
  provider: string;
  customerId: string;
}

type BetterAuthBursarFacade = Pick<Bursar, "accounts" | "requireBilling">;

export interface BursarBetterAuthOptions {
  getBursar: () => BetterAuthBursarFacade | Promise<BetterAuthBursarFacade>;
  accountEventKey?: (user: BetterAuthBursarUser) => string;
  getRegion?: (user: BetterAuthBursarUser) => string | null | undefined;
  getAccountMetadata?: (
    user: BetterAuthBursarUser,
  ) => AccountCreatedInput["metadata"] | Promise<AccountCreatedInput["metadata"]>;
  resolveProviderCustomer?: (
    user: BetterAuthBursarUser,
  ) =>
    | BetterAuthProviderCustomer
    | null
    | undefined
    | Promise<BetterAuthProviderCustomer | null | undefined>;
}

/**
 * Better Auth lifecycle plugin for Bursar account provisioning and optional
 * provider-customer synchronization. It composes alongside provider plugins
 * such as `@dodopayments/better-auth` without owning their schema or routes.
 */
export function bursarBetterAuth(options: BursarBetterAuthOptions): BetterAuthPlugin {
  const syncProviderCustomer = async (user: BetterAuthBursarUser): Promise<void> => {
    const mapping = await options.resolveProviderCustomer?.(user);
    if (!mapping) return;
    const provider = mapping.provider.trim();
    const customerId = mapping.customerId.trim();
    if (!provider || !customerId) {
      throw new TypeError("Better Auth provider customer mapping must not be empty");
    }
    const bursar = await options.getBursar();
    await bursar.requireBilling().upsertCustomer(provider, customerId, user.id, user.email);
  };

  return {
    id: "bursar",
    init() {
      return {
        options: {
          databaseHooks: {
            user: {
              create: {
                after: async (user) => {
                  const bursarUser = user as BetterAuthBursarUser;
                  const bursar = await options.getBursar();
                  const region = options.getRegion?.(bursarUser);
                  const metadata = await options.getAccountMetadata?.(bursarUser);
                  await bursar.accounts.onAccountCreated({
                    accountId: bursarUser.id,
                    eventKey:
                      options.accountEventKey?.(bursarUser) ??
                      `better-auth:user.created:${bursarUser.id}`,
                    ...(region == null ? {} : { region }),
                    ...(metadata == null ? {} : { metadata }),
                  });
                  await syncProviderCustomer(bursarUser);
                },
              },
              update: {
                after: async (user) => {
                  await syncProviderCustomer(user as BetterAuthBursarUser);
                },
              },
            },
          },
        },
      };
    },
  };
}
