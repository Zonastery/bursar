import { z } from "zod";
import { StoreError } from "../../../errors.js";
import type { CallProc } from "../../../shared/postgres-types.js";
import {
  optionalRecordRow,
  pgBoolean,
  requireRecordRow,
  safeParse,
} from "../../../shared/postgres-validation.js";

const CatalogRevisionRowSchema = z
  .object({
    id: z.string().min(1),
    config: z.record(z.string(), z.unknown()),
    version: z.coerce.number().int().positive(),
    label: z.string().nullable(),
    active: pgBoolean,
    status: z.enum(["draft", "published", "active", "retired"]),
    created_at: z.union([z.string(), z.date().transform((v) => v.toISOString())]),
  })
  .strict();
const PublishedRevisionSchema = z
  .object({
    revision_id: z.string().min(1),
    revision_no: z.coerce.number().int().positive(),
    status: z.enum(["published", "active"]),
  })
  .strict();
export type CatalogRevisionRow = z.infer<typeof CatalogRevisionRowSchema>;

export class CatalogRepository {
  constructor(private readonly callproc: CallProc) {}

  private parseRevision(row: Record<string, unknown>, context: string): CatalogRevisionRow {
    return safeParse(
      CatalogRevisionRowSchema,
      {
        id: row.id,
        config: row.source_document,
        version: row.revision_no,
        label: row.label,
        active: row.status === "active",
        status: row.status,
        created_at: row.created_at,
      },
      context,
    );
  }

  async getActiveCatalog(): Promise<CatalogRevisionRow | null> {
    const rows = await this.callproc("active_catalog_revision", []);
    const row = optionalRecordRow(rows, "CatalogRepository.getActiveCatalog");
    if (row === null || Object.values(row).every((value) => value === null)) return null;
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
      requireRecordRow(
        await this.callproc("publish_and_activate_catalog", [
          1,
          JSON.parse(config),
          label,
          activate,
          rollout,
        ]),
        "CatalogRepository.publishRevision",
      ),
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
    return (rows ?? []).map((row) => {
      if (typeof row !== "object" || row === null || Array.isArray(row)) {
        throw new StoreError("CatalogRepository.getCatalogHistory returned a non-object row");
      }
      return this.parseRevision(
        row as Record<string, unknown>,
        "CatalogRepository.getCatalogHistory",
      );
    });
  }

  async getCatalogRevision(version: number): Promise<CatalogRevisionRow | null> {
    const rows = await this.callproc("catalog_revision_by_number", [version]);
    const row = optionalRecordRow(rows, "CatalogRepository.getCatalogRevision");
    if (row === null || Object.values(row).every((value) => value === null)) return null;
    return this.parseRevision(row, "CatalogRepository.getCatalogRevision");
  }

  async activateCatalogRevision(
    version: number,
    rollout: Record<string, unknown>,
  ): Promise<CatalogRevisionRow> {
    const rows = await this.callproc("activate_catalog_revision", [version, rollout]);
    const row = optionalRecordRow(rows, "CatalogRepository.activateCatalogRevision");
    if (row === null || Object.values(row).every((value) => value === null)) {
      throw new StoreError(`Catalog revision ${version} was not found`);
    }
    return this.parseRevision(row, "CatalogRepository.activateCatalogRevision");
  }
}
