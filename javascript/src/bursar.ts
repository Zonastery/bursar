import { BillingManager, type BillingManagerOptions } from "./billing/billing-manager.js";
import type { BillingStore } from "./billing/billing-store.js";
import { CreditManager, type CreditManagerOptions } from "./manager.js";
import type { CreditStore } from "./stores/credit-store.js";
import type { CreditEventEmitter } from "./stores/events.js";

/** Options for constructing the single application-facing Bursar service. */
export interface BursarOptions {
  creditStore: CreditStore;
  billingStore?: BillingStore | null;
  creditManager?: CreditManager | null;
  creditManagerOptions?: CreditManagerOptions | null;
  billingManagerOptions?: BillingManagerOptions | null;
  emitter?: CreditEventEmitter | null;
}

/** Catalog operations. Configuration writes live here, never in BillingManager. */
export class CatalogService {
  constructor(private readonly credits: CreditManager) {}

  get active() {
    return this.credits.getActivePricing();
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

/**
 * The application-facing Bursar boundary.
 *
 * Credit and billing services are deliberately created together so consumers
 * cannot accidentally construct unrelated managers for the same account
 * lifecycle. Existing managers remain available as implementation details
 * while callers migrate to this facade.
 */
export class Bursar {
  readonly credits: CreditManager;
  readonly billing: BillingManager | null;
  readonly catalog: CatalogService;

  constructor(options: BursarOptions) {
    this.credits =
      options.creditManager ??
      new CreditManager(
        options.creditStore,
        undefined,
        options.emitter ?? undefined,
        options.creditManagerOptions ?? undefined,
      );
    this.catalog = new CatalogService(this.credits);

    this.billing = options.billingStore
      ? new BillingManager(options.billingStore, {
          ...(options.billingManagerOptions ?? {}),
          provisioning: this.credits,
        })
      : null;
  }

  /** Run core database setup through the credit store. */
  async setup(): Promise<Awaited<ReturnType<CreditStore["setup"]>>> {
    return this.credits.setup();
  }

  /** Load the active catalog into the metering engine. */
  async loadCatalog(): Promise<void> {
    await this.credits.loadPricingFromStore();
  }
}
