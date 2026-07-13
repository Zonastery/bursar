import type { QueryFn } from "../types.js";

/** Repository for billing preferences operations. */
export class BillingPreferencesRepository {
  constructor(private query: QueryFn) {}

  /** Get billing preferences for a user. Returns null if not found. */
  async get(userId: string): Promise<Record<string, unknown> | null> {
    const rows = await this.query(
      `SELECT user_id, auto_recharge, overage_protection,
              email_notifications, usage_alerts, invoice_reminders, usage_limit_alerts
       FROM public.billing_preferences WHERE user_id = $1`,
      [userId],
    );
    if (rows.length === 0) return null;
    return rows[0] as Record<string, unknown>;
  }

  /** Insert or update billing preferences. */
  async upsert(prefs: {
    userId: string;
    autoRecharge?: boolean;
    overageProtection?: boolean;
    emailNotifications?: boolean;
    usageAlerts?: boolean;
    invoiceReminders?: boolean;
    usageLimitAlerts?: boolean;
  }): Promise<void> {
    await this.query(
      `INSERT INTO public.billing_preferences (
           user_id, auto_recharge, overage_protection,
           email_notifications, usage_alerts, invoice_reminders, usage_limit_alerts
       )
       VALUES ($1, $2, $3, $4, $5, $6, $7)
       ON CONFLICT (user_id) DO UPDATE SET
           auto_recharge = COALESCE(EXCLUDED.auto_recharge,
               billing_preferences.auto_recharge),
           overage_protection = COALESCE(EXCLUDED.overage_protection,
               billing_preferences.overage_protection),
           email_notifications = COALESCE(EXCLUDED.email_notifications,
               billing_preferences.email_notifications),
           usage_alerts = COALESCE(EXCLUDED.usage_alerts,
               billing_preferences.usage_alerts),
           invoice_reminders = COALESCE(EXCLUDED.invoice_reminders,
               billing_preferences.invoice_reminders),
           usage_limit_alerts = COALESCE(EXCLUDED.usage_limit_alerts,
               billing_preferences.usage_limit_alerts),
           updated_at = now()`,
      [
        prefs.userId,
        prefs.autoRecharge ?? false,
        prefs.overageProtection ?? true,
        prefs.emailNotifications ?? true,
        prefs.usageAlerts ?? true,
        prefs.invoiceReminders ?? false,
        prefs.usageLimitAlerts ?? true,
      ],
    );
  }
}
