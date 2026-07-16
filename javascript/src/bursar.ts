import {
  BillingService as BillingServiceImpl,
  type BillingServiceOptions,
} from "./billing/billing-service.js";
import type { BillingStore } from "./billing/billing-store.js";
import {
  CreditsService as CreditsServiceImpl,
  type CreditsServiceOptions,
} from "./credits-service.js";
import type { CreditStore } from "./stores/credit-store.js";
import type { CreditEventEmitter } from "./stores/events.js";
import type {
  BillingEvent,
  BillingEventResult,
  BillingPreferences,
  BillingOfferResult,
  BillingTopupResult,
  BillingCustomerRecord,
  BillingSubscriptionState,
} from "./billing/billing-types.js";

/** Boundary used by payment providers to submit normalized lifecycle events. */
export interface BillingEventSink {
  ingestBillingEvent(event: BillingEvent): Promise<BillingEventResult>;
}

/** Public billing capability exposed by the Bursar facade. */
export interface BillingService extends BillingEventSink {
  getUserSubscription(userId: string): Promise<BillingSubscriptionState | null>;
  getUserPreferences(userId: string): Promise<BillingPreferences | null>;
  updateUserPreferences(prefs: BillingPreferences): Promise<void>;
  getCustomerByUserId(
    userId: string,
    provider?: string | null,
  ): Promise<BillingCustomerRecord | null>;
  resolveOffer(
    provider: string,
    productId?: string | null,
    priceId?: string | null,
  ): Promise<BillingOfferResult | null>;
  resolveTopup(
    provider: string,
    productId?: string | null,
    priceId?: string | null,
  ): Promise<BillingTopupResult | null>;
  upsertCustomer(
    provider: string,
    providerCustomerId: string,
    userId: string,
    email?: string | null,
  ): Promise<void>;
}

/** Public credit capability. The implementation remains package-private. */
export type CreditsService = CreditsServiceImpl;

/** Options for constructing the single application-facing Bursar service. */
export interface BursarOptions {
  creditStore: CreditStore;
  billingStore?: BillingStore | null;
  credits?: CreditsService | null;
  creditsOptions?: CreditsServiceOptions | null;
  billingOptions?: BillingServiceOptions | null;
  emitter?: CreditEventEmitter | null;
}

/** Catalog operations. Configuration writes live here, never in BillingService. */
export class CatalogService {
  constructor(private readonly credits: CreditsService) {}

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
 * cannot accidentally construct unrelated services for the same account
 * lifecycle. Provider and application integrations use this facade rather
 * than constructing lifecycle services independently.
 */
export class Bursar implements BillingEventSink {
  readonly credits: CreditsService;
  readonly billing: BillingService | null;
  readonly catalog: CatalogService;

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

    this.billing = options.billingStore
      ? new BillingServiceImpl(options.billingStore, {
          ...(options.billingOptions ?? {}),
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

  /**
   * Ingest a normalized provider event through the facade-owned billing
   * lifecycle. Providers must not depend on BillingService directly.
   */
  async ingestBillingEvent(event: BillingEvent): Promise<BillingEventResult> {
    if (!this.billing) throw new Error("Bursar billing capability is not configured");
    return this.billing.ingestBillingEvent(event);
  }
}
