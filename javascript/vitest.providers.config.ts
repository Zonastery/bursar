import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["tests/provider-adapters.test.ts"],
    coverage: {
      provider: "v8",
      all: true,
      include: ["src/providers/*/provider.ts"],
      exclude: ["src/providers/index.ts", "src/providers/*/index.ts", "src/providers/types.ts"],
      reporter: ["text", "json"],
      // Keep several points of headroom below the current 53/51/69/53 result
      // while preventing the adapter contract suite from regressing to smoke-only coverage.
      thresholds: { statements: 48, branches: 45, functions: 60, lines: 48 },
    },
  },
});
