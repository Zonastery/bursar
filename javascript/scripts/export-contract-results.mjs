import { readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { evaluateExpression } from "../dist/expr.js";
import { loadConfigFromDict } from "../dist/config.js";
import { resolveAllowanceWindow, resolveCalendarWindow } from "../dist/allowance.js";

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
const windows = {};
for (const c of JSON.parse(readFileSync(resolve(root, "tests/parity/allowance_cases.json")))) {
  const now = new Date(`${c.now}T12:00:00Z`);
  const result = c.feature
    ? resolveCalendarWindow(now, c.period)
    : resolveAllowanceWindow(now, c.period, c.anchor ? new Date(`${c.anchor}T12:00:00Z`) : null);
  windows[c.name] = {
    start: result.start.toISOString().slice(0, 10),
    end: result.end.toISOString().slice(0, 10),
  };
}
writeFileSync(process.argv[2], `${JSON.stringify({ expressions, configs, windows }, null, 2)}\n`);
