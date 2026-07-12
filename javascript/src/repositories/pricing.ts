import { z } from "zod";
import type { CallProc } from "./types.js";
import { pgBoolean, safeParse } from "./_shared.js";

export const ActivePricingRowSchema = z
  .object({
    id: z.string().optional(),
    config: z.record(z.string(), z.unknown()).optional(),
    version: z.number().optional(),
    label: z.string().nullable().optional(),
    active: pgBoolean.nullable().optional(),
    created_at: z.string().optional(),
  })
  .passthrough();

export const PricingHistoryRowSchema = z
  .object({
    id: z.string().optional(),
    version: z.number().optional(),
    label: z.string().nullable().optional(),
    active: pgBoolean.nullable().optional(),
    created_at: z.string().optional(),
  })
  .passthrough();

export type ActivePricingRow = z.infer<typeof ActivePricingRowSchema>;
export type PricingHistoryRow = z.infer<typeof PricingHistoryRowSchema>;

/** Repository for pricing configuration operations. */
export class PricingRepository {
  constructor(private callproc: CallProc) {}

  /** Fetch the currently active pricing configuration. */
  async getActivePricing(): Promise<ActivePricingRow | null> {
    const rows = await this.callproc("get_active_pricing_config", []);
    if (!rows || rows.length === 0) return null;
    return safeParse(ActivePricingRowSchema, rows[0], "PricingRepository.getActivePricing");
  }

  /** Set the active pricing configuration. */
  async setActivePricing(config: string, label: string | null): Promise<ActivePricingRow> {
    const rows = await this.callproc("set_active_pricing_config", [config, label]);
    return safeParse(ActivePricingRowSchema, rows?.[0] ?? {}, "PricingRepository.setActivePricing");
  }

  /** Fetch pricing configuration history. */
  async getPricingHistory(): Promise<PricingHistoryRow[]> {
    const rows = await this.callproc("get_pricing_configs", []);
    return (rows ?? []).map((r) =>
      safeParse(PricingHistoryRowSchema, r, "PricingRepository.getPricingHistory"),
    );
  }

  /** Fetch a specific pricing config version. */
  async getPricingConfig(version: number): Promise<ActivePricingRow | null> {
    const rows = await this.callproc("get_pricing_config", [version]);
    if (!rows || rows.length === 0) return null;
    return safeParse(ActivePricingRowSchema, rows[0], "PricingRepository.getPricingConfig");
  }

  /** Activate a pricing config version. */
  async activatePricing(version: number): Promise<ActivePricingRow> {
    const rows = await this.callproc("activate_pricing_config", [version]);
    return safeParse(ActivePricingRowSchema, rows?.[0] ?? {}, "PricingRepository.activatePricing");
  }
}
