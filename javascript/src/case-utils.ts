function snakeToCamel(str: string): string {
  return str.replace(/_([a-z])/g, (_, letter) => letter.toUpperCase());
}

function camelToSnake(str: string): string {
  return str
    .replace(/([a-z0-9])([A-Z])/g, "$1_$2")
    .replace(/([A-Z]+)([A-Z][a-z])/g, "$1_$2")
    .toLowerCase();
}

export function snakeToCamelKeys<T>(obj: T): T {
  if (Array.isArray(obj)) return obj.map(snakeToCamelKeys) as unknown as T;
  if (obj && typeof obj === "object" && obj !== null) {
    const converted: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(obj as Record<string, unknown>)) {
      converted[snakeToCamel(key)] = snakeToCamelKeys(value);
    }
    return converted as T;
  }
  return obj;
}

export function camelToSnakeKeys<T>(obj: T): T {
  if (Array.isArray(obj)) return obj.map(camelToSnakeKeys) as unknown as T;
  if (obj && typeof obj === "object" && obj !== null) {
    if ("constructor" in obj && obj.constructor?.name === "Decimal") {
      // Preserve Decimal instances as-is — they have their own JSON serialization.
      return obj;
    }
    const converted: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(obj as Record<string, unknown>)) {
      converted[camelToSnake(key)] = camelToSnakeKeys(value);
    }
    return converted as T;
  }
  return obj;
}
