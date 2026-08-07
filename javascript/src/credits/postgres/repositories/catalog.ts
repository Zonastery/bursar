import { z } from "zod";
import { StoreError } from "../../../errors.js";
import type { CallProc } from "../../../shared/postgres-types.js";
import { pgBoolean, safeParse } from "../../../shared/postgres-validation.js";

const CatalogRevisionRowSchema = z
  .object({
    id: z.string().min(1),
    config: z.record(z.string(), z.unknown()),
    source_document: z.record(z.string(), z.unknown()).optional(),
    version: z.coerce.number().int().positive(),
    revision_no: z.coerce.number().optional(),
    label: z.string().nullable().optional(),
    active: pgBoolean,
    status: z.string().nullable().optional(),
    created_at: z.union([z.string(), z.date().transform((v) => v.toISOString())]),
  })
  .passthrough();
const PublishedRevisionSchema = z.object({
  revision_id: z.string().min(1),
  revision_no: z.coerce.number().int().positive(),
});
export type CatalogRevisionRow = z.infer<typeof CatalogRevisionRowSchema>;

export class CatalogRepository {
  constructor(private readonly callproc: CallProc) {}

  private parseRevision(row: Record<string, unknown>, context: string): CatalogRevisionRow {
    return safeParse(
      CatalogRevisionRowSchema,
      {
        ...row,
        config: row.source_document,
        version: row.revision_no,
        active: row.status === "active",
      },
      context,
    );
  }

  async getActiveCatalog(): Promise<CatalogRevisionRow | null> {
    const rows = await this.callproc("active_catalog_revision", []);
    if (!rows?.length) return null;
    const row = rows[0] as Record<string, unknown>;
    return this.parseRevision(row, "CatalogRepository.getActiveCatalog");
  }

  async publishAndActivateCatalog(
    config: string,
    label: string | null,
    rollout: Record<string, unknown>,
  ): Promise<CatalogRevisionRow> {
    return this.publishRevision(config, label, true, rollout);
  }

  async publishCatalogDraft(config: string, label: string | null): Promise<CatalogRevisionRow> {
    return this.publishRevision(config, label, false, { plans: {} });
  }

  private async publishRevision(
    config: string,
    label: string | null,
    activate = false,
    rollout: Record<string, unknown> = { plans: {} },
  ): Promise<CatalogRevisionRow> {
    const published = safeParse(
      PublishedRevisionSchema,
      (
        await this.callproc("publish_and_activate_catalog", [
          1,
          JSON.parse(config),
          label,
          activate,
          rollout,
        ])
      )[0],
      "CatalogRepository.publishRevision",
    );
    const revision = await this.getCatalogRevision(published.revision_no);
    if (!revision) {
      throw new StoreError(
        `Catalog revision ${published.revision_no} disappeared after publication`,
      );
    }
    return revision;
  }

  async getCatalogHistory(): Promise<CatalogRevisionRow[]> {
    const rows = await this.callproc("list_catalog_revisions", [500]);
    return (rows ?? []).map((row) =>
      this.parseRevision(row as Record<string, unknown>, "CatalogRepository.getCatalogHistory"),
    );
  }

  async getCatalogRevision(version: number): Promise<CatalogRevisionRow | null> {
    const rows = await this.callproc("catalog_revision_by_number", [version]);
    if (!rows?.length) return null;
    return this.parseRevision(
      rows[0] as Record<string, unknown>,
      "CatalogRepository.getCatalogRevision",
    );
  }

  async activateCatalogRevision(
    version: number,
    rollout: Record<string, unknown>,
  ): Promise<CatalogRevisionRow> {
    const rows = await this.callproc("activate_catalog_revision", [version, rollout]);
    if (!rows?.length) throw new StoreError(`Catalog revision ${version} was not found`);
    return this.parseRevision(
      rows[0] as Record<string, unknown>,
      "CatalogRepository.activateCatalogRevision",
    );
  }
}
