/**
 * Out-of-the-box Langfuse tracing (Python parity): when the spec declares
 * `runtime.observability.type: langfuse`, `buildRuntime` wires an observer
 * over the OFFICIAL `langfuse` SDK — no transport code in the caller, exactly
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

/** A langfuse observation client — spans nest further spans/generations (structural). */
export interface LangfuseObservationClient {
  generation(body: {
    name: string;
    model?: string;
    input?: unknown;
    output?: unknown;
    metadata?: Record<string, unknown>;
    level?: "ERROR";
    statusMessage?: string;
  }): unknown;
  span(body: {
    name: string;
    input?: unknown;
    output?: unknown;
    metadata?: Record<string, unknown>;
    level?: "ERROR";
    statusMessage?: string;
  }): LangfuseObservationClient;
}

/** The slice of the official SDK's client this module drives (structural — tests fake it). */
export interface LangfuseSdkClient {
  trace(body: {
    id?: string;
    name: string;
    input?: unknown;
    output?: unknown;
    metadata?: Record<string, unknown>;
    tags?: string[];
    /** Native Langfuse user id (#715 slice 1): the PRIMARY subject id. */
    userId?: string;
  }): LangfuseObservationClient;
  flushAsync(): Promise<unknown>;
}

/**
 * Adapt one `EmittedTrace` to the official SDK. Activities of the SAME
 * workflow run GROUP under one parent trace (Python parity — its OTel
 * interceptor joins every activity span to one trace): the Temporal run id is
 * the trace id (deterministic, so every activity's trace-create UPSERTS the
 * same shell), named `TypefluxWorkflow:<workflow>` and tagged
 * `typeflux.workflow:<workflow>` — the exact shape both editions' read paths
 * (CP prompt-status/correlation) already reconstruct from. Each activity is a
 * SPAN under that trace, its generations/hooks nested beneath it. Without a
 * run id (standalone `executeActivity`), the activity is its own trace.
 */
export class LangfuseSdkTransport implements TraceTransport, WorkflowRunRecorder {
  /**
   * Subjects remembered per run id (#715 review, finding 3): a Langfuse `trace()`
   * upsert REPLACES the fields it carries, so a completion/error upsert (or a
   * grouped activity's trace-shell upsert) that omitted userId/tags would strip
   * the subject identity the start recorded. Every upsert for a run re-applies
   * the remembered subjects instead. Insertion-order bounded so a long-lived
   * worker process cannot grow it without limit.
   */
  private readonly subjectsByRun = new Map<string, readonly string[]>();
  private static readonly SUBJECTS_BY_RUN_LIMIT = 1024;

  constructor(
    private readonly client: LangfuseSdkClient,
    /** The spec's workflow name — the grouped parent trace's identity. */
    private readonly workflowName?: string,
  ) {}

  private rememberSubjects(runId: string, subjectIds: readonly string[] | undefined): readonly string[] {
    if (subjectIds !== undefined && subjectIds.length > 0) {
      if (!this.subjectsByRun.has(runId) && this.subjectsByRun.size >= LangfuseSdkTransport.SUBJECTS_BY_RUN_LIMIT) {
        const oldest = this.subjectsByRun.keys().next().value;
        if (oldest !== undefined) {
          this.subjectsByRun.delete(oldest);
        }
      }
      this.subjectsByRun.set(runId, [...subjectIds]);
      return subjectIds;
    }
    return this.subjectsByRun.get(runId) ?? [];
  }

  /** The subject identity fields every upsert of a run's trace shell must re-carry (#715). */
  private subjectTraceFields(subjectIds: readonly string[]): { tags: string[]; userId?: string } {
    return {
      tags: subjectIds.map((id) => `typeflux.subject:${id}`),
      ...(subjectIds.length > 0 ? { userId: subjectIds[0]! } : {}),
    };
  }

  /**
   * Enrich the grouped parent trace with workflow-level facts (Python parity:
   * the workflow span carries the run's input/output + identity metadata).
   * Upserts merge field-by-field, so the start's input and the completion's
   * output land on the same shell the activity spans hang from.
   */
  async recordWorkflowRun(record: WorkflowRunRecord): Promise<void> {
    if (this.workflowName === undefined) {
      return;
    }
    // #715 slice 1: the primary subject is the native userId; every subject a
    // portable `typeflux.subject:{id}` tag (native field + tags always agree).
    // Remembered per run and RE-APPLIED on every upsert, so the completion/error
    // upsert cannot strip what the start recorded (finding 3).
    const subjects = this.rememberSubjects(record.runId, record.subjectIds);
    const identity = this.subjectTraceFields(subjects);
    this.client.trace({
      id: record.runId,
      name: `TypefluxWorkflow:${this.workflowName}`,
      tags: [`typeflux.workflow:${this.workflowName}`, ...identity.tags],
      ...(identity.userId !== undefined ? { userId: identity.userId } : {}),
      ...(record.input !== undefined ? { input: record.input } : {}),
      ...(record.output !== undefined ? { output: record.output } : {}),
      ...(record.metadata !== undefined || record.error !== undefined
        ? { metadata: { ...record.metadata, ...(record.error !== undefined ? { error: record.error } : {}) } }
        : {}),
    });
    await this.client.flushAsync();
  }

  async submitTrace(trace: EmittedTrace): Promise<void> {
    const grouped = trace.runId !== undefined && this.workflowName !== undefined;
    const activityMetadata = {
      ...trace.metadata,
      tenant: trace.tenant,
      // The activity-level error ships in metadata plus level/statusMessage
      // on the span (grouped) or an `error` tag (standalone root).
      ...(trace.error !== undefined ? { error: trace.error } : {}),
    };
    // A grouped activity's trace() call UPSERTS the same run-scoped shell the
    // workflow record created — re-carry the remembered subject identity so an
    // activity flush arriving after the start record cannot strip it (#715).
    const groupedSubjects = grouped ? this.subjectTraceFields(this.subjectsByRun.get(trace.runId!) ?? []) : undefined;
    const root = this.client.trace(
      grouped
        ? {
            id: trace.runId!,
            name: `TypefluxWorkflow:${this.workflowName}`,
            tags: [`typeflux.workflow:${this.workflowName}`, ...(groupedSubjects?.tags ?? [])],
            ...(groupedSubjects?.userId !== undefined ? { userId: groupedSubjects.userId } : {}),
          }
        : {
            name: trace.name,
            input: trace.input,
            ...(trace.output !== undefined ? { output: trace.output } : {}),
            metadata: activityMetadata,
            ...(trace.error !== undefined ? { tags: ["error"] } : {}),
          },
    );
    const activity = grouped
      ? root.span({
          name: trace.name,
          input: trace.input,
          ...(trace.output !== undefined ? { output: trace.output } : {}),
          metadata: activityMetadata,
          ...(trace.error !== undefined
            ? { level: "ERROR" as const, statusMessage: String(trace.error) }
            : {}),
        })
      : root;
    for (const observation of trace.observations) {
      const failure =
        observation.error !== undefined
          ? { level: "ERROR" as const, statusMessage: observation.error }
          : {};
      if (observation.type === "generation") {
        activity.generation({
          name: `${trace.name}:generation`,
          ...(observation.model != null ? { model: observation.model } : {}),
          input: observation.input,
          ...(observation.output !== undefined ? { output: observation.output } : {}),
          // The 0-based validation attempt keeps repair retries
          // distinguishable (Python: typeflux.provider.validation_attempt).
          metadata: { ...observation.metadata, validation_attempt: observation.attempt },
          ...failure,
        });
      } else {
        activity.span({
          name: `${trace.name}:hook`,
          input: observation.input,
          ...(observation.output !== undefined ? { output: observation.output } : {}),
          // The pre-hook model output rides in metadata so the span shows
          // what the hook transformed (Python records it as llm_output).
          ...(observation.metadata !== undefined || observation.modelOutput !== undefined
            ? {
                metadata: {
                  ...observation.metadata,
                  ...(observation.modelOutput !== undefined
                    ? { llm_output: observation.modelOutput }
                    : {}),
                },
              }
            : {}),
          ...failure,
        });
      }
    }
    // Prompt egress: the SDK's queue is sent before submitTrace resolves, so
    // short-lived runs (examples, tests, CLIs) need no shutdown ceremony.
    // DELIVERY is SDK-owned: langfuse v3 retries transient failures with
    // backoff internally and then logs-and-drops — flushAsync resolves either
    // way (the Python SDK's background exporter behaves the same), so a
    // Langfuse outage surfaces in logs, not as a rejection here.
    await this.client.flushAsync();
  }
}

/** The resolved Langfuse client credentials, ready to spread into `new Langfuse(...)`. */
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
 * the spec opts in but the optional `langfuse` peer is not installed.
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
  let sdk: { Langfuse: new (options: Record<string, unknown>) => LangfuseSdkClient };
  try {
    sdk = (await import("langfuse")) as unknown as typeof sdk;
  } catch (error) {
    throw new Error(
      "runtime.observability.type is langfuse but the langfuse SDK is not installed — " +
        "add it to your app (npm/pnpm add langfuse). It is an optional peer dependency, " +
        "loaded only when a spec opts into langfuse tracing (Python's [langfuse] extra).",
      { cause: error },
    );
  }
  const client = new sdk.Langfuse({ ...credentials });
  // Redaction defaults ON for a tracing backend (observerFromSpec parity):
  // absent `redaction` block ≠ raw PII egress.
  return new StreamingTraceWriter(
    new LangfuseSdkTransport(client, spec.workflow.name),
    redactionFromSpec(spec) ?? {},
  );
}
