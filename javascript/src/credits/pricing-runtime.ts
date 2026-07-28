import Decimal from "decimal.js";
import { LRUCache } from "lru-cache";
import { canonicalBursarConfigDict } from "../config.js";
import type { PricingEngine } from "../engine.js";
import { PricingEngine as PricingEngineClass } from "../engine.js";
import { PricingNotLoadedError } from "../errors.js";
import type { NormalizedLogger } from "../shared/logger.js";
import type { CreditStore } from "./store.js";
import type { MetricsOrAmount } from "./service-types.js";

export function toDecimal(value: Decimal | number): Decimal {
  return value instanceof Decimal ? value : new Decimal(value);
}

export function isAmount(value: MetricsOrAmount): value is Decimal | number {
  return value instanceof Decimal || typeof value === "number";
}

/** Owns pricing publication, cache refresh, and catalog-version pinning. */
export class PricingRuntime {
  private engine: PricingEngine | null;
  private readonly cache: LRUCache<string, PricingEngine>;
  private readonly versionEngines = new Map<number, PricingEngine>();

  constructor(
    private readonly store: CreditStore,
    engine: PricingEngine | null,
    private readonly logger: NormalizedLogger,
    private readonly ttl: number,
  ) {
    this.engine = engine;
    this.cache = new LRUCache<string, PricingEngine>({
      max: 1,
      ttl,
      allowStale: true,
      fetchMethod: async () => {
        await this.loadFromStore();
        return this.engine!;
      },
    });
    if (engine) this.cache.set("pricing", engine);
  }

  get currentEngine(): PricingEngine | null {
    return this.engine;
  }

  async publishFromDict(data: Record<string, unknown>): Promise<void> {
    const canonical = canonicalBursarConfigDict(data);
    this.setEngine(PricingEngineClass.fromDict(canonical));
    await this.store.setActivePricing(canonical);
  }

  async loadFromStore(): Promise<void> {
    this.logger.info("[CreditsService] loading pricing from store");
    const active = await this.store.getActivePricing();
    if (!active) {
      this.logger.warn("[CreditsService] no active pricing config in store");
      throw new PricingNotLoadedError("no active pricing config in store");
    }
    this.setEngine(PricingEngineClass.fromDict(active.config as Record<string, unknown>));
  }

  async refreshIfStale(): Promise<void> {
    if (this.ttl === 0) return;
    await this.cache.fetch("pricing");
  }

  invalidate(): void {
    this.cache.delete("pricing");
  }

  async publish(config: Record<string, unknown>, label?: string | null): Promise<void> {
    this.logger.info("[CreditsService] publishPricing", { label });
    const canonical = canonicalBursarConfigDict(config);
    this.setEngine(PricingEngineClass.fromDict(canonical));
    await this.store.setActivePricing(canonical, label);
  }

  async publishDraft(config: Record<string, unknown>, label?: string | null): Promise<string> {
    return this.store.publishPricing(canonicalBursarConfigDict(config), label);
  }

  async activate(version: number): Promise<string> {
    const id = await this.store.activatePricing(version);
    await this.loadFromStore();
    return id;
  }

  async costOf(
    metricsOrAmount: MetricsOrAmount,
    userId?: string | null,
  ): Promise<{ amount: Decimal; model: string | null }> {
    if (isAmount(metricsOrAmount)) {
      return { amount: toDecimal(metricsOrAmount), model: null };
    }
    const engine = await this.engineForUser(userId ?? null);
    const plan = userId == null ? null : await this.store.getUserPlan(userId);
    const breakdown = engine.calculate(metricsOrAmount, {
      rateCard: plan?.rateCard ?? undefined,
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
    this.cache.set("pricing", engine);
  }

  async engineForUser(userId: string | null): Promise<PricingEngine> {
    if (userId == null) return this.requireEngine();

    const plan = await this.store.getUserPlan(userId);
    const catalogVersion = plan.catalogVersion ?? plan.configVersion ?? null;
    if (catalogVersion == null) {
      await this.refreshIfStale();
      return this.requireEngine();
    }

    const cached = this.versionEngines.get(catalogVersion);
    if (cached) return cached;

    const config = await this.store.getBursarConfig(catalogVersion);
    if (!config?.config) {
      throw new PricingNotLoadedError(
        `no pricing config for pinned catalog version ${catalogVersion}`,
      );
    }

    const engine = PricingEngineClass.fromDict(config.config as Record<string, unknown>);
    this.versionEngines.set(catalogVersion, engine);
    return engine;
  }

  private requireEngine(): PricingEngine {
    if (!this.engine) {
      throw new PricingNotLoadedError(
        "pricing not loaded: call loadPricingFromStore or publishPricing first",
      );
    }
    return this.engine;
  }
}
