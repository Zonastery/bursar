import { z } from "zod";
import type { CallProc } from "./types.js";

export const ActivePricingRowSchema = z
  .object({
    id: z.string().optional(),
    config: z.record(z.string(), z.unknown()).optional(),
    version: z.number().optional(),
    label: z.string().nullable().optional(),
    active: z
      .union([z.boolean(), z.string()] as const)
      .nullable()
      .optional(),
    created_at: z.string().optional(),
  })
  .passthrough();

export const PricingHistoryRowSchema = z
  .object({
    id: z.string().optional(),
    version: z.number().optional(),
    label: z.string().nullable().optional(),
    active: z
      .union([z.boolean(), z.string()] as const)
      .nullable()
      .optional(),
    created_at: z.string().optional(),
  })
  .passthrough();

export type ActivePricingRow = z.infer<typeof ActivePricingRowSchema>;
export type PricingHistoryRow = z.infer<typeof PricingHistoryRowSchema>;

export class PricingRepository {
  constructor(private callproc: CallProc) {}

  async getActivePricing(): Promise<ActivePricingRow | null> {
    const rows = await this.callproc("get_active_pricing_config", []);
    if (!rows || rows.length === 0) return null;
    return ActivePricingRowSchema.parse(rows[0]);
  }

  async setActivePricing(config: string, label: string | null): Promise<ActivePricingRow> {
    const rows = await this.callproc("set_active_pricing_config", [config, label]);
    return ActivePricingRowSchema.parse(rows?.[0] ?? {});
  }

  async getPricingHistory(): Promise<unknown[]> {
    const rows = await this.callproc("get_pricing_history", []);
    return rows ?? [];
  }

  async getPricingConfig(version: number): Promise<ActivePricingRow | null> {
    const rows = await this.callproc("get_pricing_config", [version]);
    if (!rows || rows.length === 0) return null;
    return ActivePricingRowSchema.parse(rows[0]);
  }

  async activatePricing(version: number): Promise<ActivePricingRow> {
    const rows = await this.callproc("activate_pricing", [version]);
    return ActivePricingRowSchema.parse(rows?.[0] ?? {});
  }
}
