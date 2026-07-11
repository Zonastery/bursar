/**
 * Node.js-only bursar exports.
 *
 * These modules depend on Node built-ins (`crypto`, `fs`) and are not
 * compatible with Edge Runtime environments.  Import them from the
 * ``@zonastery/bursar/node`` subpath when you need Node-specific behaviour:
 *
 * ```ts
 * import { loadPricingFile } from "@zonastery/bursar/node";
 * ```
 */

export { loadPricingFile } from "./load-pricing-file.js";
