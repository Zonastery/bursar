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
      thresholds: { statements: 40, branches: 35, functions: 40, lines: 40 },
    },
  },
});
