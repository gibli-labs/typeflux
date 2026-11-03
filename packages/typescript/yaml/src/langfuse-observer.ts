/**
 * Out-of-the-box Langfuse tracing (Python parity): when the spec declares
 * `runtime.observability.type: langfuse`, `buildRuntime` wires an observer
 * over the scoped Langfuse 6 SDKs and OpenTelemetry — no transport code in the caller, exactly
 * like Python's `_build_observability` constructing its Langfuse backend from
 * the environment. The SDK is an OPTIONAL peer dependency loaded lazily (the
 * TS mirror of Python's `[langfuse]` extra): specs that never opt in never
 * load it; specs that do opt in fail loudly when it is missing.
 *
 * Credentials come from the standard environment — `LANGFUSE_PUBLIC_KEY`,
 * `LANGFUSE_SECRET_KEY`, and `LANGFUSE_HOST`/`LANGFUSE_BASEURL` for
 * self-hosted. Absent keys degrade to an untraced run with one warning
 * (Python's `from_env` posture: the local dev loop keeps working offline).
 *
 * LangSmith mirrors this in `langsmith-observer.ts`. Custom backends keep
 * the injected-transport seam: pass your own `observer`
 * (`observerFromSpec(spec, transport)`) and this module never runs.
 */

import { resolveOptionalSecretText } from "./secret-references.js";
import type { EmittedTrace, TraceTransport } from "@typeflux/temporal";

import { redactionFromSpec } from "./observer-from-spec.js";
import {
  StreamingTraceWriter,
  type WorkflowRunRecord,
  type WorkflowRunRecorder,
} from "./streaming-trace-writer.js";
import type { TypefluxYamlSpec } from "./spec.js";

import { createLangfuseOtelClient, type LangfuseObservationClient, type LangfuseSdkClient } from "./langfuse-otel.js";
export type { LangfuseObservationClient, LangfuseSdkClient } from "./langfuse-otel.js";

/** One real root observation per active run, with deterministic cross-process trace identity. */
export class LangfuseSdkTransport implements TraceTransport, WorkflowRunRecorder {
  private readonly runs = new Map<string, LangfuseObservationClient>();
  private static readonly MAX_ACTIVE_RUNS = 1024;

  constructor(private readonly client: LangfuseSdkClient, private readonly workflowName?: string) {}

  private root(runId: string, subjectIds: readonly string[] = []): LangfuseObservationClient {
    const existing = this.runs.get(runId);
    if (existing !== undefined) {
      existing.updateIdentity(subjectIds.map((id) => `typeflux.subject:${id}`), subjectIds[0]);
      return existing;
    }
    if (this.runs.size >= LangfuseSdkTransport.MAX_ACTIVE_RUNS) {
      const oldest = this.runs.keys().next().value!;
      this.runs.get(oldest)!.end();
      this.runs.delete(oldest);
    }
    const root = this.client.startRoot({
      id: runId, name: `TypefluxWorkflow:${this.workflowName}`,
      tags: [`typeflux.workflow:${this.workflowName}`, ...subjectIds.map((id) => `typeflux.subject:${id}`)],
      ...(subjectIds.length > 0 ? { userId: subjectIds[0]! } : {}),
    });
    this.runs.set(runId, root);
    return root;
  }

  async recordWorkflowRun(record: WorkflowRunRecord): Promise<void> {
    if (this.workflowName === undefined) return;
    const root = this.root(record.runId, record.subjectIds);
    root.update({
      ...(record.input !== undefined ? { input: record.input } : {}),
      ...(record.output !== undefined ? { output: record.output } : {}),
      ...(record.metadata !== undefined ? { metadata: record.metadata } : {}),
      ...(record.error !== undefined ? { level: "ERROR", statusMessage: record.error, metadata: { ...record.metadata, error: record.error } } : {}),
    });
    if ("output" in record || "error" in record) {
      root.end();
      this.runs.delete(record.runId);
    }
    await this.client.forceFlush();
  }

  async submitTrace(trace: EmittedTrace): Promise<void> {
    const grouped = trace.runId !== undefined && this.workflowName !== undefined;
    const body = {
      input: trace.input,
      ...(trace.output !== undefined ? { output: trace.output } : {}),
      metadata: { ...trace.metadata, tenant: trace.tenant, ...(trace.error !== undefined ? { error: trace.error } : {}) },
      ...(trace.error !== undefined ? { level: "ERROR" as const, statusMessage: String(trace.error) } : {}),
    };
    const activity = grouped
      ? this.root(trace.runId!).startObservation(trace.name, body)
      : this.client.startRoot({ name: trace.name, ...body, ...(trace.error !== undefined ? { tags: ["error"] } : {}) });
    try {
      for (const observation of trace.observations) {
        const child = activity.startObservation(`${trace.name}:${observation.type === "generation" ? "generation" : "hook"}`, {
          input: observation.input,
          ...(observation.output !== undefined ? { output: observation.output } : {}),
          ...(observation.error !== undefined ? { level: "ERROR", statusMessage: observation.error } : {}),
          ...(observation.type === "generation" ? {
            ...(observation.model != null ? { model: observation.model } : {}),
            metadata: { ...observation.metadata, validation_attempt: observation.attempt },
          } : {
            metadata: { ...observation.metadata, ...(observation.modelOutput !== undefined ? { llm_output: observation.modelOutput } : {}) },
          }),
        }, observation.type === "generation" ? "generation" : "span");
        child.end();
      }
    } finally {
      activity.end();
    }
    await this.client.forceFlush();
  }

  async shutdown(): Promise<void> {
    for (const root of this.runs.values()) root.end();
    this.runs.clear();
    await this.client.shutdown();
  }
}

/** The resolved Langfuse client credentials, shared by the scoped client and OTel processor. */
export interface LangfuseEnvCredentials {
  readonly publicKey: string;
  readonly secretKey: string;
  readonly baseUrl?: string;
}

/**
 * THE one Langfuse configured-detection (Python
 * `LangfuseObservabilityBackend.is_configured`/`from_env` parity; #715 Bugbot):
 * resolves the credential pair (+ optional host) from the environment with values
 * STRIPPED — a whitespace-only key is not a credential (it would build a client that
 * fails at runtime instead of the surface being honestly skipped). Returns
 * `undefined` when either key is absent/blank. Callers construct the client from
 * the RETURNED values, so what was checked is exactly what is used.
 */
export function langfuseCredentialsFromEnv(
  environment: Record<string, string | undefined> = process.env,
): LangfuseEnvCredentials | undefined {
  const publicKey = environment["LANGFUSE_PUBLIC_KEY"]?.trim();
  const secretKey = environment["LANGFUSE_SECRET_KEY"]?.trim();
  if (!publicKey || !secretKey) return undefined;
  const baseUrl = (environment["LANGFUSE_HOST"] ?? environment["LANGFUSE_BASEURL"])?.trim();
  return { publicKey, secretKey, ...(baseUrl ? { baseUrl } : {}) };
}

/**
 * Spec-aware credential resolution (#793): `runtime.observability.langfuse.*` (literal or
 * `value_from`, resolved per slot) wins field-by-field; unset fields fall back to the
 * standard env vars, so a declared block is additive. Returns undefined when no complete
 * pair resolves from either source.
 */
export function langfuseCredentialsFromSpec(
  spec: TypefluxYamlSpec,
  environment: Record<string, string | undefined> = process.env,
): LangfuseEnvCredentials | undefined {
  const declared = spec.runtime.observability?.langfuse;
  // Per-FIELD env fallback (not the whole-pair detection): a declared secret_key with only
  // LANGFUSE_HOST in the environment must still pick the host up.
  const publicKey =
    resolveOptionalSecretText(declared?.public_key, "runtime.observability.langfuse.public_key", environment) ??
    environment["LANGFUSE_PUBLIC_KEY"]?.trim();
  const secretKey =
    resolveOptionalSecretText(declared?.secret_key, "runtime.observability.langfuse.secret_key", environment) ??
    environment["LANGFUSE_SECRET_KEY"]?.trim();
  if (!publicKey || !secretKey) return undefined;
  const baseUrl =
    resolveOptionalSecretText(declared?.host, "runtime.observability.langfuse.host", environment) ??
    (environment["LANGFUSE_HOST"] ?? environment["LANGFUSE_BASEURL"])?.trim();
  return { publicKey, secretKey, ...(baseUrl ? { baseUrl } : {}) };
}

/**
 * Build the out-of-the-box observer for a `type: langfuse` spec. Returns
 * undefined for every other type (none/unset → no tracing; langsmith has its
 * own module; custom brings a transport via `observerFromSpec`), and
 * undefined with one
 * warning when the credentials are absent. Throws with an install hint when
 * the spec opts in but the optional scoped Langfuse peers is not installed.
 */
export async function langfuseObserverFromSpec(
  spec: TypefluxYamlSpec,
  environment: Record<string, string | undefined> = process.env,
): Promise<StreamingTraceWriter | undefined> {
  if (spec.runtime.observability?.type !== "langfuse") {
    return undefined;
  }
  const credentials = langfuseCredentialsFromSpec(spec, environment);
  if (credentials === undefined) {
    console.warn(
      "runtime.observability.type is langfuse but no credentials resolve — running untraced " +
        "(declare runtime.observability.langfuse.public_key/secret_key or export " +
        "LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY, plus LANGFUSE_HOST for self-hosted)",
    );
    return undefined;
  }
  let client: LangfuseSdkClient;
  try {
    client = await createLangfuseOtelClient(credentials);
  } catch (error) {
    throw new Error(
      "Langfuse tracing requires @langfuse/tracing@6, @langfuse/otel@6, " +
      "@opentelemetry/api and @opentelemetry/sdk-trace; install the optional integration peers.",
      { cause: error },
    );
  }
  // Redaction defaults ON for a tracing backend (observerFromSpec parity):
  // absent `redaction` block ≠ raw PII egress.
  return new StreamingTraceWriter(
    new LangfuseSdkTransport(client, spec.workflow.name),
    redactionFromSpec(spec) ?? {},
  );
}
