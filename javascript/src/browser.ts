/** Browser-safe bursar exports.
 *
 * This entrypoint intentionally contains no database stores or Node-only
 * dependencies, so it can be imported by Client Components.
 */
export { AUTO_RECHARGE_STATES } from "./billing/types/index.js";
export type { BillingAutoRechargeStatus } from "./billing/types/index.js";
