import { readFileSync, statSync } from "fs";
import { extname } from "path";
import { load as parseYaml } from "js-yaml";
import { z } from "zod";
import { ConfigError } from "./errors.js";
import { isJsonObject, type JsonObject, type JsonValue } from "./shared/json.js";

function parseJsonWithUniqueKeys(content: string): JsonValue {
  const parsed = z.json().parse(JSON.parse(content));
  // JSON is a strict subset of YAML. js-yaml retains source-level mapping
  // keys and rejects duplicates, including nested and escape-equivalent keys;
  // native JSON.parse above remains the authority for strict JSON syntax and
  // for the returned JavaScript value.
  parseYaml(content, { json: false });
  return parsed;
}

/**
 * Read a file's contents, converting missing-file/directory/permission
 * failures into a clean {@link ConfigError} instead of a raw Node `fs` error
 * (mirrors the Python CLI's `_load_config_file`).
 */
function readFileClean(filepath: string): string {
  let stat: ReturnType<typeof statSync> | undefined;
  try {
    stat = statSync(filepath);
  } catch (cause) {
    const code = z.object({ code: z.string().optional() }).safeParse(cause).data?.code;
    if (code === "ENOENT") throw new ConfigError(`Config file not found: ${filepath}`);
    if (code === "EACCES") throw new ConfigError(`Permission denied: ${filepath}`);
    const message = cause instanceof Error ? cause.message : String(cause);
    throw new ConfigError(`Could not read ${filepath}: ${message}`, [], { cause });
  }
  if (stat.isDirectory()) {
    throw new ConfigError(`Not a file (is a directory): ${filepath}`);
  }
  try {
    return readFileSync(filepath, "utf-8");
  } catch (cause) {
    const message = cause instanceof Error ? cause.message : String(cause);
    throw new ConfigError(`Could not read ${filepath}: ${message}`, [], { cause });
  }
}

/** Guard against an empty file or a non-object parse result (e.g. an empty YAML document). */
function assertNonEmptyObject(data: JsonValue | undefined, filepath: string): JsonObject {
  if (data == null) {
    throw new ConfigError(`Bursar config is empty: ${filepath}`);
  }
  if (!isJsonObject(data)) {
    throw new ConfigError(`Bursar config must be a JSON/YAML object: ${filepath}`);
  }
  if (Object.keys(data).length === 0) {
    throw new ConfigError(`Bursar config is empty: ${filepath}`);
  }
  return data;
}

/**
 * Read a JSON or YAML Bursar config file from disk.
 *
 * Returns the raw parsed dict (suitable for ``loadConfigFromDict`` or
 * ``PricingEngine.fromDict``).
 *
 * JSON syntax is validated by the platform parser and mapping keys are also
 * checked by js-yaml so duplicate config fields cannot silently overwrite.
 */
export async function loadConfigFile(filepath: string): Promise<JsonObject> {
  const extension = extname(filepath).toLowerCase();
  if (!new Set([".json", ".yaml", ".yml"]).has(extension)) {
    throw new ConfigError(
      `Unsupported config file format: ${extension || "<none>"}. Expected .json, .yaml, or .yml`,
    );
  }

  if (extension === ".yaml" || extension === ".yml") {
    const content = readFileClean(filepath);
    let parsed: JsonValue | undefined;
    try {
      const yamlValue = parseYaml(content, { json: false });
      const jsonValue = z.json().safeParse(yamlValue);
      if (!jsonValue.success) throw new ConfigError("YAML document must contain JSON values");
      parsed = jsonValue.data;
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : String(cause);
      throw new ConfigError(`Invalid YAML in ${filepath}: ${message}`, [], { cause });
    }
    return assertNonEmptyObject(parsed, filepath);
  }

  const content = readFileClean(filepath);
  let parsed: JsonValue;
  try {
    parsed = parseJsonWithUniqueKeys(content);
  } catch (cause) {
    const message = cause instanceof Error ? cause.message : String(cause);
    throw new ConfigError(`Invalid JSON in ${filepath}: ${message}`, [], { cause });
  }
  return assertNonEmptyObject(parsed, filepath);
}
