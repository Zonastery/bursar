import { readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { evaluateExpression } from "../dist/expr.js";
import { loadConfigFromDict } from "../dist/config.js";

const root = resolve(fileURLToPath(new URL(".", import.meta.url)), "../../");
const expressions = {};
for (const c of JSON.parse(readFileSync(resolve(root, "tests/parity/expression_cases.json")))
  .expression_cases) {
  try {
    expressions[c.name] = evaluateExpression(c.expr, c.vars ?? {}).toFixed(4);
  } catch {
    expressions[c.name] = "error";
  }
}
const configs = {};
for (const c of JSON.parse(readFileSync(resolve(root, "tests/parity/config_validation_cases.json")))
  .cases) {
  try {
    loadConfigFromDict(c.config);
    configs[c.name] = "accept";
  } catch {
    configs[c.name] = "reject";
  }
}
writeFileSync(process.argv[2], `${JSON.stringify({ expressions, configs }, null, 2)}\n`);
