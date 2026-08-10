import Decimal from "decimal.js";
import { LRUCache } from "lru-cache";
import {
  canonicalBursarConfigDict,
  type BursarConfigData,
  type CatalogRollout,
} from "../config.js";
import type { PricingEngine } from "../engine.js";
import { PricingEngine as PricingEngineClass } from "../engine.js";
import { LeaseNotFoundError, CatalogNotLoadedError } from "../errors.js";
import type { NormalizedLogger } from "../shared/logger.js";
import type { CreditStore } from "./store.js";
import type { ExactAmount, MetricsOrAmount } from "./service-types.js";
import { isAmount, rejectNativeCreditAmount, toDecimal } from "./amount.js";

function toNonNegativeAmount(value: ExactAmount): Decimal {
  const amount = toDecimal(value);
  if (!amount.isFinite() || amount.isNegative()) {
    throw new RangeError("amount must be finite and non-negative");
  }
  return amount;
}

/** Owns catalog publication, cache refresh, and revision-aware pricing engines. */
export class CatalogRuntime {
  private engine: PricingEngine | null;
  private readonly cache: LRUCache<string, PricingEngine>;
  private readonly versionEngines = new Map<number, PricingEngine>();

  constructor(
    private readonly store: CreditStore,
    engine: PricingEngine | null,
    private readonly logger: NormalizedLogger,
    private readonly cacheTtlMs: number,
  ) {
    this.engine = engine;
    this.cache = new LRUCache<string, PricingEngine>({
      max: 1,
      ttl: cacheTtlMs,
      // Consumers must never proceed against a known-stale catalog. The cache
      // still deduplicates concurrent refreshes, but every caller awaits the
      // same fresh value just like the Python SDK's refresh lock.
      allowStale: false,
      fetchMethod: async () => {
        const engine = await this.fetchEngineFromStore();
        // `lru-cache` installs the returned value when the fetch completes.
        // Calling cache.set() from inside fetchMethod aborts the in-flight
        // refresh with a "replaced" error.
        this.engine = engine;
        this.versionEngines.clear();
        return engine;
      },
    });
    if (engine) this.cache.set("catalog", engine);
  }

  get currentEngine(): PricingEngine | null {
    return this.engine;
  }

  async loadFromStore(): Promise<void> {
    this.setEngine(await this.fetchEngineFromStore());
  }

  private async fetchEngineFromStore(): Promise<PricingEngine> {
    this.logger.info("[CatalogService] loading active catalog");
    const active = await this.store.getActiveCatalog();
    if (!active) {
      this.logger.warn("[CatalogService] no active catalog revision in store");
      throw new CatalogNotLoadedError("No active catalog revision is available");
    }
    return PricingEngineClass.fromDict(active.config as Record<string, unknown>);
  }

  async refreshIfStale(): Promise<void> {
    if (this.cacheTtlMs === 0) return;
    await this.cache.fetch("catalog");
  }

  invalidate(): void {
    this.cache.delete("catalog");
  }

  async publishAndActivate(
    config: BursarConfigData,
    label?: string | null,
    rollout?: CatalogRollout | null,
  ): Promise<string> {
    this.logger.info("[CatalogService] publishing and activating catalog", { label });
    const canonical = canonicalBursarConfigDict(config);
    const nextEngine = PricingEngineClass.fromDict(canonical);
    const revisionId = await this.store.publishAndActivateCatalog(canonical, label, rollout);
    this.setEngine(nextEngine);
    return revisionId;
  }

  async publishDraft(config: BursarConfigData, label?: string | null): Promise<string> {
    return this.store.publishCatalogDraft(canonicalBursarConfigDict(config), label);
  }

  async activateRevision(version: number, rollout?: CatalogRollout | null): Promise<string> {
    const id = await this.store.activateCatalogRevision(version, rollout);
    await this.loadFromStore();
    return id;
  }

  async costOf(
    metricsOrAmount: MetricsOrAmount,
    userId?: string | null,
    leaseId?: string | null,
  ): Promise<{ amount: Decimal; model: string | null }> {
    rejectNativeCreditAmount(metricsOrAmount);
    if (isAmount(metricsOrAmount)) {
      return { amount: toNonNegativeAmount(metricsOrAmount), model: null };
    }
    let engine: PricingEngine;
    let rateCard: string | undefined;
    if (leaseId != null) {
      if (userId == null) throw new TypeError("userId is required when pricing a lease");
      const context = await this.store.getLeasePricingContext(userId, leaseId);
      if (!context) throw new LeaseNotFoundError(`Lease not found. User=${userId}`);
      engine = await this.engineForCatalogVersion(context.catalogVersion);
      rateCard = context.rateCard ?? engine.getRateCardForPlan(context.planKey);
    } else {
      engine = await this.engineForUser(userId ?? null);
      const plan = userId == null ? null : await this.store.getUserPlan(userId);
      rateCard = plan?.rateCard ?? undefined;
    }
    const breakdown = engine.calculate(metricsOrAmount, {
      rateCard,
    });
    return {
      amount: breakdown.total,
      model:
        typeof metricsOrAmount.dimensions?.model === "string"
          ? metricsOrAmount.dimensions.model
          : null,
    };
  }

  private setEngine(engine: PricingEngine): void {
    this.engine = engine;
    this.versionEngines.clear();
    this.cache.set("catalog", engine);
  }

  async engineForUser(userId: string | null): Promise<PricingEngine> {
    if (userId == null) return this.requireEngine();

    const plan = await this.store.getUserPlan(userId);
    const catalogVersion = plan.catalogVersion ?? null;
    if (catalogVersion == null) {
      await this.refreshIfStale();
      return this.requireEngine();
    }

    return this.engineForCatalogVersion(catalogVersion);
  }

  private async engineForCatalogVersion(catalogVersion: number): Promise<PricingEngine> {
    const cached = this.versionEngines.get(catalogVersion);
    if (cached) return cached;

    const config = await this.store.getCatalogRevision(catalogVersion);
    if (!config?.config) {
      throw new CatalogNotLoadedError(
        `Catalog revision ${catalogVersion} is unavailable for the pinned plan`,
      );
    }

    const engine = PricingEngineClass.fromDict(config.config as Record<string, unknown>);
    this.versionEngines.set(catalogVersion, engine);
    return engine;
  }

  private requireEngine(): PricingEngine {
    if (!this.engine) {
      throw new CatalogNotLoadedError(
        "Catalog is not loaded; call catalog.load() or catalog.publishAndActivate() first",
      );
    }
    return this.engine;
  }
}
