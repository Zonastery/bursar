import { describe, expect, it, vi } from "vitest";
import { normalizeProviderLogger } from "../src/providers/types.js";

describe("provider logger normalization", () => {
  it("accepts null and supplies safe no-op methods", () => {
    const logger = normalizeProviderLogger(null);

    expect(() => {
      logger.debug("debug");
      logger.info("info");
      logger.warn("warn");
      logger.error("error");
    }).not.toThrow();
  });

  it("preserves supplied methods while filling missing methods", () => {
    const debug = vi.fn();
    const logger = normalizeProviderLogger({ debug });

    logger.debug("event", { value: 1 });
    logger.info("ignored");

    expect(debug).toHaveBeenCalledWith("event", { value: 1 });
    expect(() => logger.info("still safe")).not.toThrow();
  });
});
