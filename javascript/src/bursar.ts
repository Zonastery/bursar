import {
  BillingService as BillingServiceImpl,
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
import type { BillingCapability as BillingService, BillingEventSink } from "./billing/contracts.js";
import { CommerceService as CommerceServiceImpl } from "./commerce/service.js";
import { CommerceNotConfiguredError } from "./commerce/errors.js";
import type { CommerceOptions } from "./commerce/types.js";
import { loadConfigFromDict } from "./config.js";
import type { ParsedBursarConfig } from "./config/types.js";
import { ConfigError, PricingNotLoadedError } from "./errors.js";
import { projectPublicCatalog, type PublicCatalog } from "./catalog.js";
import type { CreditMetadata, GrantProgramAwardResult } from "./credits/types/index.js";
import { retryBursarOperation } from "./retry.js";
export type { BillingCapability as BillingService, BillingEventSink } from "./billing/contracts.js";
export type { CommerceOptions } from "./commerce/types.js";

/** Public credit capability. The implementation remains package-private. */
export type CreditsService = Pick<CreditsServiceImpl, keyof CreditsServiceImpl>;

/** Options for constructing the single application-facing Bursar service. */
export interface BursarOptions {
  creditStore: CreditStore;
  billingStore?: BillingStore | null;
  credits?: CreditsService | null;
  creditsOptions?: CreditsServiceOptions | null;
  billingOptions?: BillingServiceOptions | null;
  commerceOptions?: CommerceOptions | null;
  emitter?: CreditEventEmitter | null;
}

/** Catalog operations. Configuration writes live here, never in BillingService. */
export class CatalogService {
  constructor(private readonly credits: CreditsService) {}

  get active() {
    return this.credits.getActivePricing();
  }

  async getConfig(): Promise<ParsedBursarConfig> {
    const active = await this.active;
    if (!active) throw new PricingNotLoadedError("No active Bursar catalog is available");
    return loadConfigFromDict(active.config);
  }

  async publicView(): Promise<PublicCatalog> {
    return projectPublicCatalog(await this.getConfig());
  }

  publishDraft(config: Record<string, unknown>, label?: string | null): Promise<string> {
    return this.credits.publishPricingDraft(config, label);
  }

  activate(version: number): Promise<string> {
    return this.credits.activatePricing(version);
  }

  publishAndActivate(config: Record<string, unknown>, label?: string | null): Promise<void> {
    return this.credits.publishPricing(config, label);
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
    if (!input.eventKey.trim()) throw new ConfigError("eventKey must not be empty");
    const config = await retryBursarOperation(() => this.catalog.getConfig());
    const fallback = Object.entries(config.plans).sort(
      ([aKey, a], [bKey, b]) => a.rank - b.rank || aKey.localeCompare(bKey),
    )[0]?.[0];
    const planKey = config.catalog.defaultPlan ?? fallback;
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
  readonly billing: BillingService | null;
  readonly commerce: CommerceServiceImpl | null;
  readonly catalog: CatalogService;
  readonly accounts: AccountService;

  constructor(options: BursarOptions) {
    this.credits =
      options.credits ??
      new CreditsServiceImpl(
        options.creditStore,
        undefined,
        options.emitter ?? undefined,
        options.creditsOptions ?? undefined,
      );
    this.catalog = new CatalogService(this.credits);
    this.accounts = new AccountService(this.credits, this.catalog);

    this.billing = options.billingStore
      ? new BillingServiceImpl(options.billingStore, {
          ...(options.billingOptions ?? {}),
          provisioning: this.credits,
        })
      : null;
    this.commerce =
      this.billing && options.commerceOptions
        ? new CommerceServiceImpl(this.billing, this.credits, this, options.commerceOptions)
        : null;
    if (this.commerce) {
      this.credits.addPostDeductionHook(async ({ userId }) => {
        await this.commerce!.autoRecharge.processIfNeeded({ accountId: userId });
      });
    }
  }

  /** Load the active catalog into the pricing engine. */
  async loadCatalog(): Promise<void> {
    await this.credits.loadPricingFromStore();
  }

  /**
   * Ingest a normalized provider event through the facade-owned billing
   * lifecycle. Providers must not depend on BillingService directly.
   */
  async ingestBillingEvent(event: BillingEvent): Promise<BillingEventResult> {
    if (!this.billing)
      throw new CommerceNotConfiguredError("Bursar billing capability is not configured");
    return this.billing.ingestBillingEvent(event);
  }
}
