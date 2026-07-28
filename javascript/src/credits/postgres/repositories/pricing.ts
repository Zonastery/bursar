import { z } from "zod";
import type { CallProc } from "../../../shared/postgres-types.js";
import { pgBoolean, safeParse } from "../../../shared/postgres-validation.js";

const ActivePricingRowSchema = z
  .object({
    id: z.string().optional(),
    config: z.record(z.string(), z.unknown()).optional(),
    source_document: z.record(z.string(), z.unknown()).optional(),
    version: z.coerce.number().optional(),
    revision_no: z.coerce.number().optional(),
    label: z.string().nullable().optional(),
    active: pgBoolean.nullable().optional(),
    status: z.string().nullable().optional(),
    created_at: z.union([z.string(), z.date().transform((v) => v.toISOString())]).optional(),
  })
  .passthrough();
export type ActivePricingRow = z.infer<typeof ActivePricingRowSchema>;
export type PricingHistoryRow = ActivePricingRow;

export class PricingRepository {
  constructor(private readonly callproc: CallProc) {}

  private parseRevision(row: Record<string, unknown>, context: string): ActivePricingRow {
    return safeParse(
      ActivePricingRowSchema,
      {
        ...row,
        config: row.source_document,
        version: row.revision_no,
        active: row.status === "active",
      },
      context,
    );
  }

  async getActivePricing(): Promise<ActivePricingRow | null> {
    const rows = await this.callproc("active_catalog_revision", []);
    if (!rows?.length) return null;
    const row = rows[0] as Record<string, unknown>;
    return this.parseRevision(row, "PricingRepository.getActivePricing");
  }

  async setActivePricing(config: string, label: string | null): Promise<ActivePricingRow> {
    return this.publishPricing(config, label, true);
  }

  async publishPricing(
    config: string,
    label: string | null,
    activate = false,
  ): Promise<ActivePricingRow> {
    const published = (
      await this.callproc("publish_and_activate_catalog", [1, JSON.parse(config), label, activate])
    )[0] as Record<string, unknown> | undefined;
    if (published?.revision_no == null) {
      return safeParse(ActivePricingRowSchema, {}, "PricingRepository.publishPricing");
    }
    const revision = await this.getBursarConfig(Number(published.revision_no));
    return (
      revision ??
      safeParse(
        ActivePricingRowSchema,
        {
          id: published.revision_id,
          version: published.revision_no,
          status: published.status,
          active: published.status === "active",
        },
        "PricingRepository.publishPricing",
      )
    );
  }

  async getPricingHistory(): Promise<PricingHistoryRow[]> {
    const rows = await this.callproc("list_catalog_revisions", [500]);
    return (rows ?? []).map((row) =>
      this.parseRevision(row as Record<string, unknown>, "PricingRepository.getPricingHistory"),
    );
  }

  async getBursarConfig(version: number): Promise<ActivePricingRow | null> {
    const rows = await this.callproc("catalog_revision_by_number", [version]);
    if (!rows?.length) return null;
    return this.parseRevision(
      rows[0] as Record<string, unknown>,
      "PricingRepository.getBursarConfig",
    );
  }

  async activatePricing(version: number): Promise<ActivePricingRow> {
    const rows = await this.callproc("activate_catalog_revision", [version]);
    if (!rows?.length) throw new Error(`Catalog revision ${version} was not found`);
    return this.parseRevision(
      rows[0] as Record<string, unknown>,
      "PricingRepository.activatePricing",
    );
  }
}
