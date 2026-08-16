import {
  BillingService as BillingEventService,
  type BillingServiceOptions,
} from "./billing/billing-service.js";
import type { BillingStore } from "./billing/billing-store.js";
import {
  CreditsService as CreditsServiceImpl,
  type CreditsServiceOptions,
} from "./credits/service.js";
import type { CreditStore } from "./credits/store.js";
import type { CreditEventEmitter } from "./credits/events.js";
import type { BillingEvent, BillingEventResult } from "./billing/types/index.js";
import type { BillingCapability, BillingEventSink } from "./billing/contracts.js";
import { CommerceService as CommerceServiceImpl } from "./commerce/service.js";
import { CommerceNotConfiguredError } from "./commerce/errors.js";
import type { CommerceOptions } from "./commerce/types.js";
import { loadConfigFromDict } from "./config.js";
import type { BursarConfigData, CatalogRollout, ParsedBursarConfig } from "./config/types.js";
import { CapabilityNotConfiguredError, ConfigError, CatalogNotLoadedError } from "./errors.js";
import { projectPublicCatalog, type PublicCatalog } from "./catalog.js";
import type { CreditMetadata, GrantProgramAwardResult } from "./credits/types/index.js";
import { retryBursarOperation } from "./retry.js";
import { z } from "zod";
export type { BillingCapability, BillingEventSink } from "./billing/contracts.js";
export type { CommerceOptions } from "./commerce/types.js";

/**
 * Stable public credit capability exposed by {@link Bursar}.
 *
 * The explicit method list prevents implementation helpers added to
 * `CreditsServiceImpl` from silently becoming public API.
 */
export type CreditsService = Pick<
  CreditsServiceImpl,
  | "getUserPlan"
  | "checkFeature"
  | "getQuotaState"
  | "listQuotaEvents"
  | "startPlanMigration"
  | "migratePlanBatch"
  | "revokeCreditsByEntryType"
  | "executeGrantProgram"
  | "getLedgerEntry"
  | "getAvailable"
  | "aggregateStats"
  | "spendByUser"
  | "spendByModel"
  | "listLedgerEntries"
  | "listUsageEntries"
  | "listUsageCharges"
  | "topUsers"
  | "dailySpend"
  | "setUserPlan"
  | "unsetUserPlan"
  | "getBalance"
  | "addCredits"
  | "deductCredits"
  | "grantSubscriptionCycle"
  | "reserve"
  | "settle"
  | "release"
  | "renew"
  | "canAfford"
  | "getBucketBalances"
  | "checkAllowance"
  | "runBilled"
  | "beginBilledOperation"
  | "deduct"
  | "recordUsage"
  | "deductFlatJob"
  | "refundCredits"
  | "deductTeam"
  | "sweepExpiredCredits"
>;

type CatalogCreditsService = Pick<
  CreditsServiceImpl,
  | "getActiveCatalog"
  | "loadCatalogFromStore"
  | "refreshCatalogIfStale"
  | "invalidateCatalog"
  | "publishAndActivateCatalog"
  | "publishCatalogDraft"
  | "activateCatalogRevision"
  | "pricingEngine"
  | "setPlanRevisionPin"
  | "applyDuePlanChanges"
>;

interface BaseBursarOptions {
  creditStore: CreditStore;
  creditsOptions?: CreditsServiceOptions;
  emitter?: CreditEventEmitter;
}

/** Credits-only construction rejects billing options that would otherwise be ignored. */
export interface CreditsOnlyBursarOptions extends BaseBursarOptions {
  billingStore?: undefined;
  billingOptions?: never;
  commerceOptions?: never;
}

/** Billing construction owns provisioning and may optionally enable commerce. */
export interface BillingBursarOptions extends BaseBursarOptions {
  billingStore: BillingStore;
  billingOptions?: Omit<BillingServiceOptions, "provisioning">;
  commerceOptions?: CommerceOptions;
}

/** Options for constructing the single application-facing Bursar service. */
export type BursarOptions = CreditsOnlyBursarOptions | BillingBursarOptions;

/** Catalog operations. Configuration writes live here, never in BillingService. */
export class CatalogService {
  constructor(private readonly credits: CatalogCreditsService) {}

  /** Return the active persisted catalog revision, if one exists. */
  getActive() {
    return this.credits.getActiveCatalog();
  }

  /** Whether this process currently has a pricing engine loaded. */
  get isLoaded(): boolean {
    return this.credits.pricingEngine !== null;
  }

  /** Load the active persisted catalog into this process. */
  load(): Promise<void> {
    return this.credits.loadCatalogFromStore();
  }

  /** Block until a stale in-process catalog has been refreshed. */
  refresh(): Promise<void> {
    return this.credits.refreshCatalogIfStale();
  }

  /** Force the next refresh to reload the active catalog. */
  invalidate(): void {
    this.credits.invalidateCatalog();
  }

  async getConfig(): Promise<ParsedBursarConfig> {
    const active = await this.getActive();
    if (!active) throw new CatalogNotLoadedError("No active Bursar catalog is available");
    return loadConfigFromDict(active.config);
  }

  async publicView(): Promise<PublicCatalog> {
    return projectPublicCatalog(await this.getConfig());
  }

  publishDraft(config: BursarConfigData, label?: string | null): Promise<string> {
    return this.credits.publishCatalogDraft(config, label);
  }

  activate(version: number, rollout?: CatalogRollout | null): Promise<string> {
    return this.credits.activateCatalogRevision(version, rollout);
  }

  publishAndActivate(
    config: BursarConfigData,
    label?: string | null,
    rollout?: CatalogRollout | null,
  ): Promise<string> {
    return this.credits.publishAndActivateCatalog(config, label, rollout);
  }

  /** Pin or unpin one current assignment from automatic catalog rollout. */
  setRevisionPin(userId: string, pinned: boolean): Promise<boolean> {
    return this.credits.setPlanRevisionPin(userId, pinned);
  }

  /** Apply one bounded batch of renewal-effective plan changes. */
  applyDueChanges(limit = 100): Promise<number> {
    return this.credits.applyDuePlanChanges(limit);
  }
}

export interface AccountCreatedInput {
  accountId: string;
  eventKey: string;
  region?: string | null;
  metadata?: CreditMetadata | null;
}

export interface AccountCreatedResult {
  accountId: string;
  planKey: string;
  planAssigned: boolean;
  grants: GrantProgramAwardResult[];
}

/** Generic financial lifecycle operations for SaaS accounts. */
export class AccountService {
  constructor(
    private readonly credits: CreditsService,
    private readonly catalog: CatalogService,
  ) {}

  async onAccountCreated(input: AccountCreatedInput): Promise<AccountCreatedResult> {
    if (!z.string().trim().min(1).safeParse(input.accountId).success) {
      throw new TypeError("accountId must be a non-empty string");
    }
    if (!z.string().trim().min(1).safeParse(input.eventKey).success) {
      throw new TypeError("eventKey must be a non-empty string");
    }
    const config = await retryBursarOperation(() => this.catalog.getConfig());
    const planKey = config.catalog.defaultPlan;
    if (!planKey) throw new ConfigError("The active catalog has no default account plan");

    const current = await retryBursarOperation(() => this.credits.getUserPlan(input.accountId));
    const planAssigned = current.planKey == null;
    if (planAssigned) {
      await retryBursarOperation(() => this.credits.setUserPlan(input.accountId, planKey));
    }

    const grants: GrantProgramAwardResult[] = [];
    for (const [programKey, program] of Object.entries(config.credits.grantPrograms).sort(
      ([a], [b]) => a.localeCompare(b),
    )) {
      if (program.trigger !== "account_created") continue;
      grants.push(
        ...(await retryBursarOperation(() =>
          this.credits.executeGrantProgram({
            trigger: "account_created",
            programKey,
            subjectId: input.accountId,
            eventKey: input.eventKey,
            region: input.region,
            metadata: input.metadata,
          }),
        )),
      );
    }
    return {
      accountId: input.accountId,
      planKey: current.planKey ?? planKey,
      planAssigned,
      grants,
    };
  }
}

/**
 * The application-facing Bursar boundary.
 *
 * Credit and billing services are deliberately created together so consumers
 * cannot accidentally construct unrelated services for the same account
 * lifecycle. Provider and application integrations use this facade rather
 * than constructing lifecycle services independently.
 */
export class Bursar implements BillingEventSink {
  readonly credits: CreditsService;
  readonly billing: BillingCapability | null;
  readonly commerce: CommerceServiceImpl | null;
  readonly catalog: CatalogService;
  readonly accounts: AccountService;

  constructor(options: BursarOptions) {
    if (!z.object({}).safeParse(options).success) {
      throw new TypeError("Bursar options are required");
    }
    if (options.billingStore === null) {
      throw new TypeError("billingStore must be a BillingStore when provided");
    }
    for (const optionName of [
      "creditsOptions",
      "billingOptions",
      "commerceOptions",
      "emitter",
    ] as const) {
      if (options[optionName] === null) {
        throw new TypeError(`${optionName} must not be null`);
      }
    }
    if (
      options.billingStore === undefined &&
      (options.billingOptions !== undefined || options.commerceOptions !== undefined)
    ) {
      throw new TypeError("billingOptions and commerceOptions require billingStore");
    }
    if (options.billingOptions && "provisioning" in options.billingOptions) {
      throw new TypeError("billingOptions.provisioning is owned by Bursar");
    }
    const environments = [
      ["creditStore", options.creditStore.providerEnvironment],
      ["billingStore", options.billingStore?.providerEnvironment],
      ["commerceOptions", options.commerceOptions?.providerEnvironment],
    ].filter((entry): entry is [string, NonNullable<(typeof entry)[1]>] => entry[1] !== undefined);
    if (new Set(environments.map(([, environment]) => environment)).size > 1) {
      throw new TypeError(
        `Bursar provider environments must match: ${environments
          .map(([name, environment]) => `${name}=${environment}`)
          .join(", ")}`,
      );
    }
    const credits = new CreditsServiceImpl(
      options.creditStore,
      undefined,
      options.emitter ?? undefined,
      options.creditsOptions ?? undefined,
    );
    this.credits = credits;
    this.catalog = new CatalogService(credits);
    this.accounts = new AccountService(credits, this.catalog);

    this.billing = options.billingStore
      ? new BillingEventService(options.billingStore, {
          ...(options.billingOptions ?? {}),
          provisioning: credits,
        })
      : null;
    this.commerce =
      this.billing && options.commerceOptions
        ? new CommerceServiceImpl(this.billing, credits, this, options.commerceOptions)
        : null;
    if (this.commerce) {
      credits.addPostDeductionHook(async ({ userId }) => {
        await this.commerce!.autoRecharge.processIfNeeded({ accountId: userId });
      });
    }
  }

  /** Load the active catalog into the pricing engine. */
  async loadCatalog(): Promise<void> {
    await this.catalog.load();
  }

  /** Return billing or raise the SDK's typed configuration error. */
  requireBilling(): BillingCapability {
    if (!this.billing) {
      throw new CapabilityNotConfiguredError("billing");
    }
    return this.billing;
  }

  /** Return commerce or raise the SDK's typed configuration error. */
  requireCommerce(): CommerceServiceImpl {
    if (!this.commerce) {
      throw new CommerceNotConfiguredError("Bursar commerce capability is not configured");
    }
    return this.commerce;
  }

  /**
   * Ingest a normalized provider event through the facade-owned billing
   * lifecycle. Providers must not depend on BillingService directly.
   */
  async ingestBillingEvent(event: BillingEvent): Promise<BillingEventResult> {
    return this.requireBilling().ingestBillingEvent(event);
  }
}
