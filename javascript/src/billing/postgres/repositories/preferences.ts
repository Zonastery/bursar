import type { QueryFn } from "../../../shared/postgres-types.js";

export class BillingPreferencesRepository {
  constructor(private query: QueryFn) {}

  async get(userId: string): Promise<Record<string, unknown> | null> {
    const rows = await this.query("SELECT * FROM bursar.get_billing_preferences($1::uuid)", [
      userId,
    ]);
    return (rows[0] as Record<string, unknown> | undefined) ?? null;
  }

  async upsert(prefs: {
    userId: string;
    autoRecharge?: boolean;
    overageProtection?: boolean;
    emailNotifications?: boolean;
    usageAlerts?: boolean;
    invoiceReminders?: boolean;
  }): Promise<void> {
    await this.query("SELECT bursar.upsert_billing_preferences($1::uuid, $2, $3, $4, $5, $6)", [
      prefs.userId,
      prefs.autoRecharge ?? false,
      prefs.overageProtection ?? true,
      prefs.emailNotifications ?? true,
      prefs.usageAlerts ?? true,
      prefs.invoiceReminders ?? false,
    ]);
  }
}
