import { describe, expect, it } from "vitest";

import { validateMigrationFiles } from "./bootstrap.js";

describe("integration migration bootstrap", () => {
  it("accepts one contiguous baseline and rejects gaps or malformed names", () => {
    expect(validateMigrationFiles(["002_tables.sql", "001_schema.sql"])).toEqual([
      "001_schema.sql",
      "002_tables.sql",
    ]);
    expect(() => validateMigrationFiles(["001_schema.sql", "003_rpc.sql"])).toThrow(
      /expected 002, found 003_rpc\.sql/,
    );
    for (const malformed of ["schema.sql", "001_schema_.sql", "001_schema__types.sql"]) {
      expect(() => validateMigrationFiles([malformed])).toThrow(
        new RegExp(`expected 001, found ${malformed.replace(".", "\\.")}`),
      );
    }
    expect(() => validateMigrationFiles([])).toThrow(/contains no SQL migrations/);
  });
});
