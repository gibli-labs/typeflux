/** Dedicated OTel provider per integration: never replace application-global tracing. */
import { createHash, randomBytes } from "node:crypto";
import type { LangfuseEnvCredentials } from "./langfuse-observer.js";

export interface ObservationAttributes {
  input?: unknown;
  output?: unknown;
  metadata?: Record<string, unknown>;
  model?: string;
  level?: "ERROR";
  statusMessage?: string;
}
export interface LangfuseObservationClient {
  update(attributes: ObservationAttributes): void;
  updateIdentity(tags: string[], userId?: string): void;
  end(): void;
  startObservation(name: string, attributes: ObservationAttributes, type?: "span" | "generation"): LangfuseObservationClient;
}
export interface RootObservationAttributes extends ObservationAttributes {
  /** Temporal run ID used as a deterministic trace seed, never a raw OTel trace ID. */
  id?: string;
  name: string;
  tags?: string[];
  userId?: string;
}
export interface LangfuseSdkClient {
  startRoot(attributes: RootObservationAttributes): LangfuseObservationClient;
  forceFlush(): Promise<unknown>;
  shutdown(): Promise<unknown>;
}

export async function createLangfuseOtelClient(
  credentials: LangfuseEnvCredentials,
  /** Test seam: an in-memory OTel exporter; production uses Langfuse's OTLP exporter. */
  exporter?: unknown,
): Promise<LangfuseSdkClient> {
  const [sdk, otel, api, base] = await Promise.all([
    import("@langfuse/tracing"), import("@langfuse/otel"),
    import("@opentelemetry/api"), import("@opentelemetry/sdk-trace"),
  ]);
  let rootTraceId: string | undefined;
  const provider = new base.TracerProvider({
    idGenerator: {
      generateTraceId: () => rootTraceId ?? randomBytes(16).toString("hex"),
      generateSpanId: () => randomBytes(8).toString("hex"),
    },
    spanProcessors: [new otel.LangfuseSpanProcessor({
      ...credentials,
      ...(exporter === undefined ? {} : { exporter: exporter as import("@opentelemetry/sdk-trace").SpanExporter }),
    })],
  });
  const tracer = provider.getTracer("langfuse-sdk", "6.0.1");
  const attrs = sdk.LangfuseOtelSpanAttributes;
  const wrap = (
    span: import("@opentelemetry/api").Span,
    body: ObservationAttributes,
    type: "span" | "generation",
    identity: Record<string, string | string[]>,
  ): LangfuseObservationClient => {
    span.setAttributes(identity);
    const observation = type === "generation"
      ? new sdk.LangfuseGeneration({ otelSpan: span, attributes: body })
      : new sdk.LangfuseSpan({ otelSpan: span, attributes: body });
    return {
      update: (update) => { observation.update(update); },
      updateIdentity: (tags, userId) => {
        identity[attrs.TRACE_TAGS] = [...new Set([...(identity[attrs.TRACE_TAGS] as string[] ?? []), ...tags])];
        if (userId !== undefined) identity[attrs.TRACE_USER_ID] ??= userId;
        span.setAttributes(identity);
      },
      end: () => observation.end(),
      startObservation: (name, data, childType = "span") => wrap(
        tracer.startSpan(name, {}, api.trace.setSpan(api.ROOT_CONTEXT, span)), data, childType, identity,
      ),
    };
  };
  return {
    startRoot: (body) => {
      const identity: Record<string, string | string[]> = {
        [attrs.TRACE_NAME]: body.name,
        [attrs.TRACE_TAGS]: body.tags ?? [],
        ...(body.userId === undefined ? {} : { [attrs.TRACE_USER_ID]: body.userId }),
      };
      // startSpan invokes the generator synchronously; the override is cleared
      // before any async work and cannot leak to a concurrent integration.
      rootTraceId = body.id === undefined ? undefined : createHash("sha256").update(body.id).digest("hex").slice(0, 32);
      try {
        return wrap(tracer.startSpan(body.name, {}, api.ROOT_CONTEXT), body, "span", identity);
      } finally {
        rootTraceId = undefined;
      }
    },
    forceFlush: () => provider.forceFlush(),
    shutdown: () => provider.shutdown(),
  };
}
