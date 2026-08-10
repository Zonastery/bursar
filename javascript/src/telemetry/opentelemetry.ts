import {
  metrics,
  SpanStatusCode,
  trace,
  type Attributes,
  type Counter,
  type Histogram,
  type Meter,
  type Span,
  type Tracer,
} from "@opentelemetry/api";

import {
  BURSAR_INSTRUMENTATION_SCOPE,
  BURSAR_INSTRUMENTATION_VERSION,
  setDefaultInstrumentation,
  telemetryErrorAttributes,
  telemetryOperationAttributes,
  type Instrumentation,
  type TelemetryAttributes,
  type TelemetryAttributeValue,
} from "./index.js";

export interface OpenTelemetryInstrumentationOptions {
  /** Override the API tracer, primarily for host-level composition or tests. */
  tracer?: Tracer;
  /** Override the API meter, primarily for host-level composition or tests. */
  meter?: Meter;
}

function otelAttributes(attributes: Readonly<Record<string, TelemetryAttributeValue>>): Attributes {
  return attributes;
}

/** OpenTelemetry API-only adapter. It never creates an SDK or exporter. */
export class OpenTelemetryInstrumentation implements Instrumentation {
  private readonly tracer: Tracer;
  private readonly operationCounter: Counter;
  private readonly operationDuration: Histogram;

  constructor(options: OpenTelemetryInstrumentationOptions = {}) {
    this.tracer =
      options.tracer ??
      trace.getTracer(BURSAR_INSTRUMENTATION_SCOPE, BURSAR_INSTRUMENTATION_VERSION);
    const meter =
      options.meter ??
      metrics.getMeter(BURSAR_INSTRUMENTATION_SCOPE, BURSAR_INSTRUMENTATION_VERSION);
    this.operationCounter = meter.createCounter("bursar.operation.count", {
      description: "Completed Bursar operations",
      unit: "{operation}",
    });
    this.operationDuration = meter.createHistogram("bursar.operation.duration", {
      description: "Bursar operation duration",
      unit: "s",
    });
  }

  run<T>(
    operation: string,
    attributes: TelemetryAttributes | undefined,
    callback: () => Promise<T>,
  ): Promise<T> {
    const baseAttributes = telemetryOperationAttributes(operation, attributes);
    const spanName = `bursar.${baseAttributes["bursar.operation"]}`;
    return this.tracer.startActiveSpan(
      spanName,
      { attributes: otelAttributes(baseAttributes) },
      async (span) => this.runInSpan(span, baseAttributes, callback),
    );
  }

  private async runInSpan<T>(
    span: Span,
    baseAttributes: Record<string, TelemetryAttributeValue>,
    callback: () => Promise<T>,
  ): Promise<T> {
    const startedAt = performance.now();
    try {
      const result = await callback();
      const completed = { ...baseAttributes, "bursar.outcome": "success" };
      span.setAttributes(otelAttributes(completed));
      span.setStatus({ code: SpanStatusCode.OK });
      this.recordMetrics(startedAt, completed);
      return result;
    } catch (error) {
      const completed = {
        ...baseAttributes,
        "bursar.outcome": "error",
        ...telemetryErrorAttributes(error),
      };
      span.setAttributes(otelAttributes(completed));
      // Deliberately omit a description and exception event: both may include
      // the raw error message, SQL text, identifiers, or provider payloads.
      span.setStatus({ code: SpanStatusCode.ERROR });
      this.recordMetrics(startedAt, completed);
      throw error;
    } finally {
      span.end();
    }
  }

  private recordMetrics(
    startedAt: number,
    attributes: Record<string, TelemetryAttributeValue>,
  ): void {
    const safeAttributes = otelAttributes(attributes);
    this.operationCounter.add(1, safeAttributes);
    this.operationDuration.record(
      Math.max(0, performance.now() - startedAt) / 1_000,
      safeAttributes,
    );
  }
}

export function createOpenTelemetryInstrumentation(
  options: OpenTelemetryInstrumentationOptions = {},
): Instrumentation {
  return new OpenTelemetryInstrumentation(options);
}

/**
 * Enable the API-only adapter for subsequently constructed Bursar services.
 * The embedding application remains responsible for the OTel SDK and export.
 */
export function enableOpenTelemetry(options: OpenTelemetryInstrumentationOptions = {}): () => void {
  return setDefaultInstrumentation(createOpenTelemetryInstrumentation(options));
}
