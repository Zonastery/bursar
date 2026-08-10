import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["tests/provider-adapters.test.ts"],
    coverage: {
      provider: "v8",
      include: ["src/providers/*/provider.ts"],
      exclude: ["src/providers/index.ts", "src/providers/*/index.ts", "src/providers/types.ts"],
      reporter: ["text", "json"],
      // Vitest 4's V8 remapper changed branch accounting. Preserve the
      // post-upgrade baseline across supported Node versions.
      thresholds: { statements: 50, branches: 38, functions: 64, lines: 51 },
    },
  },
});
