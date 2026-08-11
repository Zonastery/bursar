import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const packageJson = require("@zonastery/bursar/package.json");
const optionalPeers = Object.entries(packageJson.peerDependenciesMeta ?? {})
  .filter(([, metadata]) => metadata.optional === true)
  .map(([packageName]) => packageName);

assert.ok(
  optionalPeers.length > 0,
  "the packed package must declare its optional peers",
);
for (const packageName of optionalPeers) {
  assert.throws(
    () => require.resolve(packageName),
    (error) => error?.code === "MODULE_NOT_FOUND",
    `${packageName} unexpectedly resolved in the peer-free consumer`,
  );
}

const [sdk, nodeSdk, providers] = await Promise.all([
  import("@zonastery/bursar"),
  import("@zonastery/bursar/node"),
  import("@zonastery/bursar/providers"),
]);

assert.equal(typeof sdk.Bursar, "function");
assert.equal(typeof sdk.PricingEngine, "function");
assert.equal(typeof nodeSdk.BursarRuntime, "function");
assert.equal(typeof nodeSdk.S3BillingArchive, "function");
assert.ok(Array.isArray(providers.PROVIDER_ENVIRONMENTS));
assert.equal(typeof providers.noopLogger, "object");
