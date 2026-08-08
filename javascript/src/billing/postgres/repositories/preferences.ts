import { z } from "zod";
import type { QueryFn } from "../../../shared/postgres-types.js";
import { StoreError } from "../../../errors.js";
import type { BillingPreferences } from "../../types/index.js";
import {
  optionalRecordRow,
  pgBoolean,
  postgresUuid,
  requireResultField,
  safeParse,
} from "../../../shared/postgres-validation.js";

const BillingPreferencesRowSchema = z
  .object({
    subject_id: postgresUuid,
    auto_recharge: pgBoolean,
    overage_protection: pgBoolean,
    email_notifications: pgBoolean,
    usage_alerts: pgBoolean,
    invoice_reminders: pgBoolean,
  })
  .strict();

export class BillingPreferencesRepository {
  constructor(private query: QueryFn) {}

  async get(userId: string): Promise<BillingPreferences | null> {
    const rows = await this.query("SELECT * FROM bursar.get_billing_preferences($1::uuid)", [
      userId,
    ]);
    const row = optionalRecordRow(rows, "BillingPreferencesRepository.get");
    if (row === null) {
      return null;
    }
    const parsed = safeParse(
      BillingPreferencesRowSchema,
      {
        subject_id: row.subject_id,
        auto_recharge: row.auto_recharge,
        overage_protection: row.overage_protection,
        email_notifications: row.email_notifications,
        usage_alerts: row.usage_alerts,
        invoice_reminders: row.invoice_reminders,
      },
      "BillingPreferencesRepository.get",
    );
    return {
      userId: parsed.subject_id,
      autoRecharge: parsed.auto_recharge,
      overageProtection: parsed.overage_protection,
      emailNotifications: parsed.email_notifications,
      usageAlerts: parsed.usage_alerts,
      invoiceReminders: parsed.invoice_reminders,
    };
  }

  async upsert(prefs: BillingPreferences): Promise<void> {
    const rows = await this.query(
      "SELECT bursar.upsert_billing_preferences($1::uuid, $2, $3, $4, $5, $6) AS updated",
      [
        prefs.userId,
        prefs.autoRecharge,
        prefs.overageProtection,
        prefs.emailNotifications,
        prefs.usageAlerts,
        prefs.invoiceReminders,
      ],
    );
    const updated = requireResultField(
      rows,
      "updated",
      pgBoolean,
      "BillingPreferencesRepository.upsert",
    );
    if (!updated) {
      throw new StoreError("BillingPreferencesRepository.upsert: update was rejected", {
        indeterminate: true,
      });
    }
  }
}
