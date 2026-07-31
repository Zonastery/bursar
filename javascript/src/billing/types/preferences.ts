export interface BillingPreferences {
  userId: string;
  autoRecharge: boolean;
  overageProtection: boolean;
  emailNotifications: boolean;
  usageAlerts: boolean;
  invoiceReminders: boolean;
}

export interface BillingCustomerRecord {
  provider: string;
  providerCustomerId: string;
}
