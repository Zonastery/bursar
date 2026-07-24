/** One provider-neutral billable operation. */
export interface UsageMetrics {
  operation: string;
  measures?: Record<string, number | string>;
  dimensions?: Record<string, string>;
  metadata?: Record<string, unknown>;
}
