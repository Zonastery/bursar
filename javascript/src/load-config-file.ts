import { readFileSync, statSync } from "fs";
import { extname } from "path";
import { load as parseYaml } from "js-yaml";
import { ConfigError } from "./errors.js";

function parseJsonWithUniqueKeys(content: string): unknown {
  const parsed: unknown = JSON.parse(content);
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
    const code = (cause as NodeJS.ErrnoException).code;
    if (code === "ENOENT") throw new ConfigError(`Config file not found: ${filepath}`);
    if (code === "EACCES") throw new ConfigError(`Permission denied: ${filepath}`);
    throw new ConfigError(`Could not read ${filepath}: ${(cause as Error).message}`, [], { cause });
  }
  if (stat.isDirectory()) {
    throw new ConfigError(`Not a file (is a directory): ${filepath}`);
  }
  try {
    return readFileSync(filepath, "utf-8");
  } catch (cause) {
    throw new ConfigError(`Could not read ${filepath}: ${(cause as Error).message}`, [], { cause });
  }
}

/** Guard against an empty file or a non-object parse result (e.g. an empty YAML document). */
function assertNonEmptyObject(data: unknown, filepath: string): Record<string, unknown> {
  if (data == null) {
    throw new ConfigError(`Bursar config is empty: ${filepath}`);
  }
  if (typeof data !== "object" || Array.isArray(data)) {
    throw new ConfigError(
      `Bursar config must be a JSON/YAML object, got ${typeof data}: ${filepath}`,
    );
  }
  if (Object.keys(data).length === 0) {
    throw new ConfigError(`Bursar config is empty: ${filepath}`);
  }
  return data as Record<string, unknown>;
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
export async function loadConfigFile(filepath: string): Promise<Record<string, unknown>> {
  const extension = extname(filepath).toLowerCase();
  if (!new Set([".json", ".yaml", ".yml"]).has(extension)) {
    throw new ConfigError(
      `Unsupported config file format: ${extension || "<none>"}. Expected .json, .yaml, or .yml`,
    );
  }

  if (extension === ".yaml" || extension === ".yml") {
    const content = readFileClean(filepath);
    let parsed: unknown;
    try {
      parsed = parseYaml(content, { json: false });
    } catch (cause) {
      throw new ConfigError(`Invalid YAML in ${filepath}: ${(cause as Error).message}`, [], {
        cause,
      });
    }
    return assertNonEmptyObject(parsed, filepath);
  }

  const content = readFileClean(filepath);
  let parsed: unknown;
  try {
    parsed = parseJsonWithUniqueKeys(content);
  } catch (cause) {
    throw new ConfigError(`Invalid JSON in ${filepath}: ${(cause as Error).message}`, [], {
      cause,
    });
  }
  return assertNonEmptyObject(parsed, filepath);
}
