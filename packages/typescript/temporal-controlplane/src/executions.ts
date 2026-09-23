/**
 * Execution listing for the ts-plan-argument binding (#620; Python `project/runs.py`
 * `workflow_executions`). Python lists by versioned-TYPE prefix (`{name}.` — identity lives in
 * the type name); this profile's identity lives in the MEMO (contracts/temporal-binding), so the
 * listing is a bounded visibility scan over the constant generic type, optionally narrowed by
 * the configured search attribute, filtered client-side on `typeflux_workflow` +
 * `typeflux_project`. `current_version` compares the execution's `typeflux_spec_digest` memo
 * against the currently-resolved plan digest (profile `version_identity: memo`; Python compares
 * type names).
 *
 * Every Temporal-tier call is BOUNDED (Python `_temporal_bounded`, #581): a connect failure or
 * timeout is 503 `TemporalUnavailable` — cluster unavailability, never a 500.
 */

import { existsSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

import {
  buildPayloadCodec,
  type SubworkflowSpecResolver,
  type TemporalTlsOptions,
  temporalTlsOptions,
  type TypefluxAesGcmPayloadCodec,
  type TypefluxYamlSpec,
  workflowPlanDigest,
  workflowPlanFromSpec,
  YAML_WORKFLOW_TYPE,
} from "@typeflux/temporal-yaml";

import { ProjectControlPlaneError } from "./errors.js";

/** One listed execution (Python `WorkflowExecutionRecord`; nulls serialize — no exclude_none here). */
export interface ApiWorkflowExecutionRecord {
  execution_id: string;
  run_id: string | null;
  workflow_type: string;
  /** True when the execution runs the currently-resolved spec version (memo digest match). */
  current_version: boolean;
  status: string;
  start_time: string | null;
  close_time: string | null;
}

/** The listing (Python `WorkflowExecutionList`). */
export interface ApiWorkflowExecutionList {
  logical_workflow: string;
  current_workflow_type: string;
  executions: ApiWorkflowExecutionRecord[];
}

/** One visibility row — the `@temporalio/client` `WorkflowExecutionInfo` slice this module reads. */
export interface VisibilityExecutionInfo {
  workflowId: string;
  runId?: string | undefined;
  type: string;
  status?: { name?: string | undefined } | undefined;
  startTime?: Date | undefined;
  closeTime?: Date | undefined;
  memo?: Record<string, unknown> | undefined;
}

/**
 * The Temporal-tier seam: an async listing plus a close handle. The default implementation
 * connects via `@temporalio/client`; tests (and future transports) inject their own.
 */
export interface ExecutionsVisibilityClient {
  list(query: string): AsyncIterable<VisibilityExecutionInfo>;
  close(): Promise<void>;
}

export type VisibilityClientFactory = (
  options: TemporalConnectionOptions,
) => Promise<ExecutionsVisibilityClient>;

/** Spec-derived connection options (the ts-plan-argument driver's mapping, `binding_ts.py`). */
export interface TemporalConnectionOptions {
  address: string;
  namespace: string;
  /** A boolean, or the resolved structured TLS options (custom CA / mTLS certs, #685). */
  tls: boolean | TemporalTlsOptions;
  apiKey?: string | undefined;
  /**
   * The AES-256-GCM codec built from `runtime.temporal.payload_codec` (#188), or `undefined`
   * when the project declares none. Client factories MUST wire it onto the client's
   * `dataConverter` (Python `binding_ts._connect`): un-wired, a CP-issued START would persist
   * payloads as PLAINTEXT and a history read could not DECODE a codec-encrypted body. Fail-closed
   * — a declared-but-unresolvable/wrong-length key throws when this is resolved (config time).
   */
  payloadCodec?: TypefluxAesGcmPayloadCodec | undefined;
}

/**
 * Map `runtime.temporal` to connection options, fail closed on anything this tier cannot honor
 * (Python `TsBindingTarget`): a structured TLS block resolves to REAL cert bytes via the
 * canonical mapping (#685) — an invalid block or unreadable cert file is a config error, never
 * a silently-weakened `tls: true` — and a `value_from.env` api_key that is required-but-unset
 * is a config error, never an anonymous connection.
 */
export function temporalConnectionOptions(
  spec: TypefluxYamlSpec,
  env: Record<string, string | undefined> = process.env,
): TemporalConnectionOptions {
  const temporal = spec.runtime.temporal;
  let tls: boolean | TemporalTlsOptions;
  try {
    tls = temporalTlsOptions(temporal.tls, env);
  } catch (error) {
    throw new ProjectControlPlaneError(
      error instanceof Error ? error.message : String(error),
      422,
      "TsBindingConfigError",
    );
  }
  let apiKey: string | undefined;
  const rawKey = temporal.api_key;
  if (typeof rawKey === "string") {
    apiKey = rawKey;
  } else if (rawKey !== undefined) {
    // `value_from: {env | file, required?}` — the ts-driver's secret rules (binding_ts.py):
    // env reads the PROCESS environment (a deployment secret; environment-file values arrive
    // via ${VAR} interpolation at spec load); file reads + trims, with ~ expansion.
    const { env: envName, file: fileName, required } = rawKey.value_from;
    if (envName !== undefined) {
      const resolved = env[envName];
      if (resolved === undefined && (required ?? true)) {
        throw new ProjectControlPlaneError(
          `environment variable '${envName}' is required by the project's temporal config and is not set`,
          422,
          "TsBindingConfigError",
        );
      }
      apiKey = resolved;
    } else if (fileName !== undefined) {
      const secretFile = fileName.startsWith("~")
        ? join(homedir(), fileName.slice(1).replace(/^\//, ""))
        : fileName;
      if (!existsSync(secretFile)) {
        if (required ?? true) {
          throw new ProjectControlPlaneError(
            `secret file '${secretFile}' required by the project's temporal config does not exist`,
            422,
            "TsBindingConfigError",
          );
        }
      } else {
        apiKey = readFileSync(secretFile, "utf8").trim();
      }
    }
  }
  // Build the codec from the SAME resolved-env layer the api_key uses (parity with Python's
  // binding_ts._resolve_payload_codec); fail-closed on a missing/wrong-length key at config time.
  // subject_scope (#715 slice 4) is REJECTED here fail-closed (parity with Python's
  // binding_ts): this control plane has no SubjectKeystore seam yet, so it can neither
  // decode a subject execution's history nor seal signals under the workers' key
  // records — and silently using only the shared codec would seal subject-execution
  // signals OUTSIDE their shred domain. Re-open trigger: a CP keystore-injection seam.
  if (temporal.payload_codec?.subject_scope !== undefined) {
    throw new ProjectControlPlaneError(
      "runtime.temporal.payload_codec.subject_scope is not supported by this control plane " +
        "(#715 slice 4): lifecycle operations on a subject-scoped project require the " +
        "deployment's SHARED SubjectKeystore, and no keystore seam exists here yet. Drive " +
        "subject-scoped projects through the runtime APIs (see docs/typescript/privacy.md).",
      422,
      "TsBindingConfigError",
    );
  }
  const payloadCodec = buildPayloadCodec(temporal.payload_codec, env);
  return {
    address: temporal.address ?? "localhost:7233",
    namespace: temporal.namespace ?? "default",
    tls,
    apiKey,
    payloadCodec,
  };
}

/** The default client: a real `@temporalio/client` connection (lazily imported — test seams never load it). */
export const defaultVisibilityClient: VisibilityClientFactory = async (options) => {
  const { Client, Connection } = await import("@temporalio/client");
  const connection = await Connection.connect({
    address: options.address,
    // Boolean or the resolved structured options — `Connection.connect` takes both (#685).
    tls: options.tls,
    ...(options.apiKey !== undefined ? { apiKey: options.apiKey } : {}),
  });
  const client = new Client({
    connection,
    namespace: options.namespace,
    // Codec-aware read (#188): wire the spec's codec so history payloads a codec-enabled worker
    // sealed DECODE under the SAME wire format (Python binding_ts._connect). Absent ⇒ omitted,
    // and the codec's decode passes any unmarked payload through untouched regardless.
    ...(options.payloadCodec !== undefined
      ? { dataConverter: { payloadCodecs: [options.payloadCodec] } }
      : {}),
  });
  return {
    list: (query: string) => client.workflow.list({ query }) as AsyncIterable<VisibilityExecutionInfo>,
    close: () => connection.close(),
  };
};

const TEMPORAL_TIER_TIMEOUT_ENV = "TYPEFLUX_CONTROLPLANE_TEMPORAL_TIMEOUT_SECONDS";
const TEMPORAL_TIER_TIMEOUT_DEFAULT_SECONDS = 10;

export const temporalTierTimeoutSeconds = (): number => {
  const raw = process.env[TEMPORAL_TIER_TIMEOUT_ENV];
  const parsed = raw !== undefined ? Number(raw) : Number.NaN;
  return Number.isFinite(parsed) && parsed > 0 ? parsed : TEMPORAL_TIER_TIMEOUT_DEFAULT_SECONDS;
};

/**
 * Bound one Temporal-tier operation (Python `_temporal_bounded`, #581): the whole call races the
 * configured timeout, and a timeout or any non-`ProjectControlPlaneError` failure is cluster
 * unavailability — 503 `TemporalUnavailable`, never a 500. `what` names the operation in both
 * messages exactly like Python's (`"{what} did not complete within …"` / `"{what} failed: …"`).
 * Shared by the executions listing, the drain view (`drain.ts`), and the task-queue workers
 * describe (`workers.ts`) so the three visibility routes cannot drift on the bound contract.
 */
export async function boundedTemporalTier<T>(run: () => Promise<T>, what: string): Promise<T> {
  const timeoutSeconds = temporalTierTimeoutSeconds();
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => {
      reject(
        new ProjectControlPlaneError(
          `${what} did not complete within ${timeoutSeconds}s; the call was ` +
            "cancelled so it cannot stall other requests. Check that the Temporal cluster in " +
            "this environment's runtime config is reachable.",
          503,
        ),
      );
    }, timeoutSeconds * 1000);
  });
  try {
    // NOTE: race losers do NOT leak unhandled rejections — Promise.race attaches a reaction to
    // every input, which satisfies Node's rejection tracking even after the race settles
    // (verified live). The explicit guard below is documentation-by-construction: two
    // independent reviews flagged this line, so make the handled-ness visible.
    const running = run();
    running.catch(() => undefined);
    return await Promise.race([running, timeout]);
  } catch (error) {
    if (error instanceof ProjectControlPlaneError) throw error;
    throw new ProjectControlPlaneError(
      `${what} failed: ${error instanceof Error ? error.message : String(error)}`,
      503,
    );
  } finally {
    if (timer !== undefined) clearTimeout(timer);
  }
}

/** How many visibility rows the memo-filtered scan reads before giving up (bounded best-effort). */
export const EXECUTIONS_SCAN_LIMIT = 1000;

export interface ListWorkflowExecutionsOptions {
  workflowId: string;
  environmentId: string;
  /** Clamped to [1, 100] like Python's route. */
  limit?: number;
  /** Injectable Temporal seam; defaults to a real `@temporalio/client` connection. */
  clientFactory?: VisibilityClientFactory;
  /** Resolves `workflow:` sub-workflow refs so the current-digest derivation embeds children (#55). */
  subworkflows?: SubworkflowSpecResolver;
}

/**
 * List this workflow's executions, newest first (Temporal visibility default ordering), every
 * status. The scan is DOUBLY bounded: the whole call races the Temporal-tier timeout (#581),
 * and the memo-filtered iteration stops after {@link EXECUTIONS_SCAN_LIMIT} rows.
 */
export async function listWorkflowExecutions(
  spec: TypefluxYamlSpec,
  options: ListWorkflowExecutionsOptions,
): Promise<ApiWorkflowExecutionList> {
  const logical = spec.workflow.name;
  // A bad graph after a clean resolution (duplicate/reserved step ids, invalid review route)
  // is a CONFIG failure — 422 like the bundle/catalog projections, never the generic 500.
  let currentDigest: string;
  try {
    currentDigest = workflowPlanDigest(
      workflowPlanFromSpec(spec, options.subworkflows !== undefined ? { subworkflows: options.subworkflows } : {}),
    );
  } catch (error) {
    throw new ProjectControlPlaneError(
      `failed to derive the workflow plan: ${error instanceof Error ? error.message : String(error)}`,
      422,
    );
  }
  const limit = Math.max(1, Math.min(options.limit ?? 20, 100));
  const factory = options.clientFactory ?? defaultVisibilityClient;
  const query = memoIdentityVisibilityQuery(spec);

  const run = async (): Promise<ApiWorkflowExecutionList> => {
    const client = await factory(temporalConnectionOptions(spec));
    try {
      const records: ApiWorkflowExecutionRecord[] = [];
      let scanned = 0;
      for await (const execution of client.list(query)) {
        scanned += 1;
        const memo = execution.memo ?? {};
        if (memo["typeflux_workflow"] === logical && memo["typeflux_project"] === spec.project) {
          records.push({
            execution_id: execution.workflowId,
            run_id: execution.runId ?? null,
            workflow_type: execution.type,
            current_version: memo["typeflux_spec_digest"] === currentDigest,
            status: execution.status?.name ?? "UNKNOWN",
            start_time: execution.startTime?.toISOString() ?? null,
            close_time: execution.closeTime?.toISOString() ?? null,
          });
          if (records.length >= limit) break;
        }
        if (scanned >= EXECUTIONS_SCAN_LIMIT) break;
      }
      return {
        logical_workflow: logical,
        current_workflow_type: YAML_WORKFLOW_TYPE,
        executions: records,
      };
    } finally {
      await client.close().catch(() => undefined);
    }
  };

  // Python `_temporal_bounded` (#581): the bound is part of the operation. A timeout or a
  // failed connect is cluster unavailability — 503 TemporalUnavailable, never a 500.
  return boundedTemporalTier(run, "listing executions");
}

/**
 * The ts-plan-argument profile's visibility query (Python `TsPlanArgumentDriver._visibility_query`):
 * the constant generic type, narrowed by the configured search attribute when the spec opts in
 * (the NAME is spec-validated to [A-Za-z][A-Za-z0-9_]*, the VALUE is quote-escaped —
 * injection-safe, same as frozen-version enforcement). Shared by the executions listing and the
 * drain view (`drain.ts`), which appends its running-status clause.
 */
export function memoIdentityVisibilityQuery(spec: TypefluxYamlSpec): string {
  const logical = spec.workflow.name;
  const attribute = spec.runtime.temporal.workflow_search_attribute;
  const attributeClause =
    attribute !== undefined ? ` AND ${attribute} = '${logical.replace(/'/g, "''")}'` : "";
  return `WorkflowType = '${YAML_WORKFLOW_TYPE}'${attributeClause}`;
}
