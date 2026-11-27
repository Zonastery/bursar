#!/usr/bin/env node
import { readFileSync, writeFileSync } from "node:fs";
import { parseArgs } from "node:util";
import { z } from "zod";
import { loadConfigFromDict } from "./config.js";
import { PricingEngine } from "./engine.js";
import { ConfigError } from "./errors.js";
import { loadRawConfigFile } from "./load-config-file.js";
import { normalizeProviderEnvironment } from "./providers/environment.js";
import type { ProviderEnvironment } from "./providers/environment.js";
import { isJsonObject } from "./shared/json.js";
import type { JsonObject } from "./shared/json.js";

const help = `Usage: bursar-config <command> [options]

  init [path]                    Create a starter (default: bursar.yaml)
  validate <path>                Validate a configuration
  quote <path> --operation NAME  Quote usage offline

Options:
  --environment test|live|sandbox  Select provider environment
  --measure NAME=VALUE              Usage measure (repeatable)
  --dimension NAME=VALUE            Dimension, optionally JSON (repeatable)
  --rate-card NAME                  Select rate card
  --help                            Show this help
`;

function pairs(items: string[], jsonValues: boolean): Record<string, string | number | boolean> {
  const result: Record<string, string | number | boolean> = Object.create(null);
  for (const item of items) {
    const index = item.indexOf("=");
    const key = item.slice(0, index);
    if (index < 1 || Object.hasOwn(result, key))
      throw new Error(`expected unique NAME=VALUE, got '${item}'`);
    const value = item.slice(index + 1);
    let parsed: unknown = value;
    if (jsonValues) {
      try {
        parsed = JSON.parse(value);
      } catch {
        /* Unquoted strings are accepted. */
      }
    }
    const scalar = z.union([z.string(), z.number(), z.boolean()]).safeParse(parsed);
    if (!scalar.success) throw new Error(`dimension '${key}' must be a string, number, or boolean`);
    result[key] = scalar.data;
  }
  return result;
}

function environments(data: JsonObject): ProviderEnvironment[] {
  const found = new Set<ProviderEnvironment>();
  if (!isJsonObject(data.commerce) || !isJsonObject(data.commerce.offers)) return [];
  for (const offer of Object.values(data.commerce.offers)) {
    if (!isJsonObject(offer) || !isJsonObject(offer.providers)) continue;
    for (const reference of Object.values(offer.providers)) {
      if (!isJsonObject(reference)) continue;
      for (const field of ["price_id", "product_id", "external_id"]) {
        const mapping = reference[field];
        if (isJsonObject(mapping))
          for (const key of Object.keys(mapping)) found.add(normalizeProviderEnvironment(key));
      }
    }
  }
  return [...found].sort();
}

async function main(args: string[]): Promise<void> {
  const command = args.shift();
  if (!command || command === "--help" || command === "-h") {
    process.stdout.write(help);
    return;
  }
  if (!["init", "validate", "quote"].includes(command))
    throw new Error(`unknown command '${command}'`);
  const { values, positionals } = parseArgs({
    args,
    options: {
      help: { type: "boolean" },
      environment: { type: "string" },
      operation: { type: "string" },
      measure: { type: "string", multiple: true },
      dimension: { type: "string", multiple: true },
      "rate-card": { type: "string" },
    },
    allowPositionals: true,
    strict: true,
  });
  if (command === "init" && values.environment !== undefined)
    throw new Error("--environment is not valid for init");
  if (
    command !== "quote" &&
    (values.operation !== undefined ||
      values.measure !== undefined ||
      values.dimension !== undefined ||
      values["rate-card"] !== undefined)
  )
    throw new Error(`quote options are not valid for ${command}`);
  if (values.help) {
    process.stdout.write(help);
    return;
  }
  if (positionals.length > 1 || (command !== "init" && positionals.length !== 1))
    throw new Error(`${command} expects ${command === "init" ? "at most one path" : "one path"}`);
  const path = positionals[0] ?? "bursar.yaml";
  if (command === "init") {
    writeFileSync(path, readFileSync(new URL("./starter.yaml", import.meta.url)), { flag: "wx" });
    console.log(JSON.stringify({ path, created: true }));
    return;
  }
  const environment =
    values.environment === undefined ? undefined : normalizeProviderEnvironment(values.environment);
  const data = await loadRawConfigFile(path);
  if (command === "validate") {
    const selected = environment === undefined ? environments(data) : [environment];
    if (selected.length) for (const item of selected) loadConfigFromDict(data, item);
    else loadConfigFromDict(data);
    console.log(JSON.stringify({ valid: true, errors: [] }));
    return;
  }
  if (!values.operation) throw new Error("quote requires --operation NAME");
  const config = loadConfigFromDict(data, environment);
  const measures = pairs(values.measure ?? [], false);
  const result = new PricingEngine(config).calculate(
    {
      operation: values.operation,
      measures: z.record(z.string(), z.string()).parse(measures),
      dimensions: pairs(values.dimension ?? [], true),
    },
    { rateCard: values["rate-card"] },
  );
  console.log(
    JSON.stringify(
      {
        model_credits: result.modelCredits.toFixed(6),
        tool_credits: result.toolCredits.toFixed(6),
        search_credits: result.searchCredits.toFixed(6),
        cache_savings: result.cacheSavings.toFixed(6),
        fixed_credits: result.fixedCredits.toFixed(6),
        operation_credits: result.operationCredits.toFixed(6),
        total: result.total.toFixed(6),
        breakdown: result.breakdown,
      },
      null,
      2,
    ),
  );
}

main(process.argv.slice(2)).catch((error: unknown) => {
  const message = error instanceof Error ? error.message : String(error);
  const errors = error instanceof ConfigError ? error.validationErrors : [];
  console.error(
    JSON.stringify({
      error: {
        code: error instanceof ConfigError ? error.code : "CLI_ERROR",
        message,
        details: errors,
      },
    }),
  );
  process.exitCode = 1;
});
