/**
 * The ts-plan-argument OPERATE tier (#563; Python `project/binding_ts.py` +
 * `project/operations.py` + `controlplane/api.py`). Operates a TS-edition execution over
 * `@temporalio/client`: memo-verified bound handles, plan-as-argument start, and the shared
 * lifecycle query/signals from the temporal-binding contract (`typefluxYamlWorkflow`,
 * `typeflux_lifecycle_status`, `typeflux_request_cancel`, `typeflux_submit_review`).
 *
 * BINDING VERIFICATION (the operator obligation, contracts/temporal-binding/binding.v1.json):
 * before any lifecycle signal/query, `boundHandle` does `describe()` → verifies the constant
 * generic type + the memo (`typeflux_project`, `typeflux_workflow`, and
 * `typeflux_workflow_version` when the spec declares a label) → RE-PINS by the described run id
 * (a run-id-less handle targets the LATEST run at dispatch, so id reuse could swap the target
 * between verification and the signal). A mismatch is a 409 `LifecycleBindingError`, fail closed,
 * no memo-less fallback — mirroring `TsPlanArgumentDriver._bound_handle` message-for-message.
 *
 * BOUNDED (Python `_temporal_bounded`, #581): every Temporal-tier call races the configured
 * timeout and maps a timeout / connect failure / gRPC UNAVAILABLE|DEADLINE_EXCEEDED to 503
 * `TemporalUnavailable`. Any OTHER Temporal error (a NOT_FOUND describe of a missing execution,
 * an already-started start conflict) propagates — Python does NOT special-case these, so they
 * surface as the generic 500 the HTTP adapter renders, NOT a 404/409. Parity, not a new mapping.
 *
 * INJECTABLE CLIENT SEAM (same style as executions.ts): the default factory connects a real
 * `@temporalio/client`; tests inject a fake so no Temporal server is required.
 */

import {
  type ReviewCommand,
  type SubworkflowSpecResolver,
  type TypefluxYamlSpec,
  type WorkflowLifecycleStatus,
  type WorkflowPlan,
  workflowIdentityMemo,
  workflowPlanDigest,
  workflowPlanFromSpec,
  YAML_WORKFLOW_TYPE,
  enforceFrozenWorkflowVersion,
} from "@typeflux/temporal-yaml";

import { versionIdentityKey } from "./drain.js";
import { ProjectControlPlaneError } from "./errors.js";
import { temporalConnectionOptions, type TemporalConnectionOptions } from "./executions.js";

/**
 * The `WorkflowExecutionDescription` slice `boundHandle` reads (the `@temporalio/client` shape):
 * the workflow type, the pinned run id, and the identity memo.
 */
export interface WorkflowDescription {
  type: string;
  runId?: string | undefined;
  memo?: Record<string, unknown> | undefined;
}

/**
 * The injectable operations client: a handle factory plus a close handle. The default connects
 * `@temporalio/client`; tests (and future transports) inject their own. `getHandle` addresses an
 * execution by id (and optionally a pinned run id); `start` dispatches a new execution.
 */
export interface OperationsClient {
  /**
   * Bounded visibility listing for the frozen-version start check (#662). Optional: a client
   * without it degrades to the SDK's best-effort warn-and-skip, never a hard failure.
   */
  list?(query: string): AsyncIterable<{ memo?: Record<string, unknown> | undefined }>;
  /**
   * Scope `fn`'s Temporal RPCs under a gRPC deadline so a timed-out MUTATION is actually
   * cancelled at the transport, not merely abandoned client-side (codex P1: a slow-but-
   * eventually-successful start/signal must not land after the caller saw a 503). Optional:
   * fakes may omit it; the bounded race stays as the outer backstop.
   */
  withDeadline?<T>(deadlineMs: number, fn: () => Promise<T>): Promise<T>;
  getHandle(workflowId: string, runId?: string): OperationsHandle;
  start(options: StartHandleOptions): Promise<{ runId: string | undefined }>;
  /**
   * Live WORKFLOW-task poller count for a queue (Python `_poller_count`), for the migrate
   * fail-closed workers gate (#204). Optional so existing fakes need not implement it; the
   * migrate path refuses (rather than silently skips) when a client cannot report it.
   */
  pollerCount?(taskQueue: string): Promise<number>;
  close(): Promise<void>;
}

/** One execution handle — the `@temporalio/client` `WorkflowHandle` slice this module needs. */
export interface OperationsHandle {
  describe(): Promise<WorkflowDescription>;
  query<Ret>(queryType: string): Promise<Ret>;
  signal(signalType: string, ...args: unknown[]): Promise<void>;
  /**
   * Terminate the execution (migrate #204). Optional so lifecycle-only fakes need not implement
   * it; the migrate path requires it and refuses otherwise.
   */
  terminate?(reason?: string): Promise<void>;
  /**
   * The original input the execution was started with, decoded from its `WorkflowExecutionStarted`
   * event — input carry-over for migrate (#204). For the ts-plan-argument profile the start args
   * are `[plan, input]`, so this returns the SECOND argument (index 1). Optional (see terminate).
   */
  fetchStartInput?(): Promise<unknown>;
}

/** The plan-as-argument start dispatch (`workflow.start` inputs the operate tier fixes). */
export interface StartHandleOptions {
  workflowType: string;
  taskQueue: string;
  workflowId: string;
  /** Exactly `[plan, input]` — the fixed ts-plan-argument order (contracts/temporal-binding). */
  args: readonly unknown[];
  memo: Record<string, string>;
  /** The configured keyword search attribute (name → this workflow's logical name); absent = none. */
  searchAttribute?: { name: string; value: string } | undefined;
}

export type OperationsClientFactory = (
  options: TemporalConnectionOptions,
) => Promise<OperationsClient>;

/** Standard gRPC status codes (the wire spec — stable). UNAVAILABLE / DEADLINE_EXCEEDED are cluster
 * unavailability → 503, mirroring Python's `RPCStatusCode.UNAVAILABLE|DEADLINE_EXCEEDED` mapping. */
const GRPC_UNAVAILABLE = 14;
const GRPC_DEADLINE_EXCEEDED = 4;

const TEMPORAL_TIER_TIMEOUT_ENV = "TYPEFLUX_CONTROLPLANE_TEMPORAL_TIMEOUT_SECONDS";
const TEMPORAL_TIER_TIMEOUT_DEFAULT_SECONDS = 10;

const temporalTierTimeoutSeconds = (): number => {
  const raw = process.env[TEMPORAL_TIER_TIMEOUT_ENV];
  const parsed = raw !== undefined ? Number(raw) : Number.NaN;
  return Number.isFinite(parsed) && parsed > 0 ? parsed : TEMPORAL_TIER_TIMEOUT_DEFAULT_SECONDS;
};

/**
 * True when `error` is cluster unavailability — a connect failure or a gRPC
 * UNAVAILABLE / DEADLINE_EXCEEDED (Python maps exactly these to 503; every other Temporal error
 * keeps its own mapping, i.e. propagates to the generic 500). Recognizes the `@temporalio/client`
 * connect-refused signature AND the gRPC status code — which for a MID-LIFE outage (the client
 * already connected, then the cluster drops) is on the RAW grpc error `@temporalio/client` wraps in
 * `ServiceError(msg, { cause })`, i.e. on `error.cause.code`, NOT the top-level `.code`. Walk the
 * bounded `cause` chain so a wrapped UNAVAILABLE/DEADLINE_EXCEEDED still maps to 503, not 500.
 */
function isTemporalUnavailable(error: unknown): boolean {
  if (!(error instanceof Error)) return false;
  // temporalio surfaces a refused/failed connect as a message containing "client connect"
  // (Python matches the same substring on its bare RuntimeError) or "Failed to connect".
  if (error.message.includes("client connect") || error.message.includes("Failed to connect")) {
    return true;
  }
  // Scan the error and its `cause` chain (bounded to guard against a self-referential cause) for a
  // gRPC status code: the ServiceError wrapper carries the real grpc error as its cause.
  let current: unknown = error;
  for (let depth = 0; depth < 5 && current instanceof Error; depth += 1) {
    const code = (current as { code?: unknown }).code;
    if (code === GRPC_UNAVAILABLE || code === GRPC_DEADLINE_EXCEEDED) return true;
    current = (current as { cause?: unknown }).cause;
  }
  return false;
}

/**
 * Bound one Temporal-tier awaitable (Python `_temporal_bounded`, #581): race the configured
 * timeout, and map a timeout / connect failure / gRPC UNAVAILABLE|DEADLINE_EXCEEDED to a 503
 * `ProjectControlPlaneError`. Any OTHER error is re-thrown verbatim (NOT_FOUND, already-started,
 * config errors) so it keeps its own HTTP mapping — parity with Python, which does NOT convert
 * them to 503 and lets them reach the generic 500.
 */
async function temporalBounded<T>(
  run: () => Promise<T>,
  what: string,
  client?: OperationsClient,
): Promise<T> {
  const timeoutSeconds = temporalTierTimeoutSeconds();
  // Prefer the TRANSPORT deadline when the client provides one: the gRPC layer cancels the RPC
  // at the bound, so a slow mutation cannot land after the caller received the 503. The race
  // below stays as the backstop for hangs outside the RPC (and for fakes without deadlines).
  const bounded = client?.withDeadline !== undefined
    ? () => client.withDeadline!(timeoutSeconds * 1000, run)
    : run;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => {
      reject(
        new ProjectControlPlaneError(
          `${what} did not complete within ${timeoutSeconds}s; the call was cancelled so it ` +
            "cannot stall other requests. Check that the Temporal cluster in this environment's " +
            "runtime config is reachable.",
          503,
        ),
      );
    }, timeoutSeconds * 1000);
  });
  try {
    const running = bounded();
    // Race losers do not leak unhandled rejections — Promise.race attaches a reaction to every
    // input (same rationale as executions.ts); the explicit catch documents handled-ness.
    running.catch(() => undefined);
    return await Promise.race([running, timeout]);
  } catch (error) {
    if (error instanceof ProjectControlPlaneError) throw error;
    if (isTemporalUnavailable(error)) {
      throw new ProjectControlPlaneError(
        `${what} failed: ${error instanceof Error ? error.message : String(error)}`,
        503,
      );
    }
    // Parity: every other Temporal error propagates (the HTTP adapter renders it as 500), exactly
    // as Python's `_temporal_bounded` re-raises non-connect/non-UNAVAILABLE exceptions.
    throw error;
  } finally {
    if (timer !== undefined) clearTimeout(timer);
  }
}

/** The default operations client: a real `@temporalio/client` connection (lazily imported — test
 * seams never load it, and this module carries no `@temporalio/common` dependency). */
const defaultOperationsClient: OperationsClientFactory = async (options) => {
  const { Client, Connection, defaultPayloadConverter } = await import("@temporalio/client");
  const connection = await Connection.connect({
    address: options.address,
    // Boolean or the resolved structured options — `Connection.connect` takes both (#685).
    tls: options.tls,
    // The handshake carries its own transport bound — a slow connect must not depend on the
    // caller's race alone (codex P1's connect-phase sibling).
    connectTimeout: `${temporalTierTimeoutSeconds()}s`,
    ...(options.apiKey !== undefined ? { apiKey: options.apiKey } : {}),
  });
  const client = new Client({
    connection,
    namespace: options.namespace,
    // Codec-wire the operate client (#188, Python binding_ts._connect): the START dispatch below
    // must ENCODE its `[plan, input]` args, or a CP-issued start persists them as PLAINTEXT while
    // the worker encrypts everything else (the Bugbot fail-closed scenario, CP-side). Absent ⇒
    // omitted (codec-free, unchanged).
    ...(options.payloadCodec !== undefined
      ? { dataConverter: { payloadCodecs: [options.payloadCodec] } }
      : {}),
  });
  return {
    withDeadline: (deadlineMs, fn) => connection.withDeadline(Date.now() + deadlineMs, fn),
    list: (query) => client.workflow.list({ query }) as AsyncIterable<{ memo?: Record<string, unknown> }>,
    getHandle: (workflowId, runId) => {
      const handle = client.workflow.getHandle(workflowId, runId);
      return {
        describe: () => handle.describe() as unknown as Promise<WorkflowDescription>,
        query: <Ret>(queryType: string) => handle.query<Ret, []>(queryType),
        signal: (signalType, ...args) => handle.signal(signalType, ...args),
        terminate: (reason) => handle.terminate(reason).then(() => undefined),
        // Input carry-over (#204): read the start event and decode the SECOND positional argument
        // (args are `[plan, input]`). When the project declares a codec (#188), the start args are
        // ENCRYPTED in history — decode through the codec FIRST (fail-closed on an unknown/wrong
        // key), then the default payload converter. A codec-free project decodes exactly as before,
        // and even with a codec present an unmarked (pre-codec) payload passes through untouched.
        fetchStartInput: async () => {
          const history = await handle.fetchHistory();
          const started = (history.events ?? []).find(
            (event) => event.workflowExecutionStartedEventAttributes != null,
          );
          const payloads =
            started?.workflowExecutionStartedEventAttributes?.input?.payloads ?? [];
          if (payloads.length <= 1) return undefined;
          const decoded =
            options.payloadCodec !== undefined
              ? await options.payloadCodec.decode(payloads as never)
              : payloads;
          return defaultPayloadConverter.fromPayload(decoded[1]!);
        },
      };
    },
    pollerCount: async (taskQueue: string) => {
      const response = await connection.workflowService.describeTaskQueue({
        namespace: options.namespace,
        taskQueue: { name: taskQueue },
        // TASK_QUEUE_TYPE_WORKFLOW (temporal.api.enums.v1.TaskQueueType) — the workflow pollers,
        // matching workers.ts / Python's `TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW`.
        taskQueueType: 1,
      });
      return response.pollers?.length ?? 0;
    },
    start: async (opts) => {
      // The configured keyword search attribute is authoritative (contracts/temporal-binding
      // conflict_rule); here the operate tier sets only the one Typeflux key. Set via the string
      // `searchAttributes` map (a keyword's value is a single-element array) to avoid a
      // `@temporalio/common` dependency for the `TypedSearchAttributes` helpers.
      const startOptions: Record<string, unknown> = {
        taskQueue: opts.taskQueue,
        workflowId: opts.workflowId,
        args: [...opts.args],
        memo: opts.memo,
      };
      if (opts.searchAttribute !== undefined) {
        startOptions["searchAttributes"] = {
          [opts.searchAttribute.name]: [opts.searchAttribute.value],
        };
      }
      const started = await client.workflow.start(opts.workflowType, startOptions as never);
      return { runId: started.firstExecutionRunId };
    },
    close: () => connection.close(),
  };
};

/** The operate target derived from a resolved spec (Python `TsBindingTarget` + start identity). */
export interface OperateTarget {
  connection: TemporalConnectionOptions;
  projectName: string;
  workflowName: string;
  /** Present only when the spec declares `workflow.version` (the memo carries the label). */
  versionLabel: string | undefined;
  taskQueue: string;
  /** decision → route target step, sorted (Python `valid_user_decisions`). */
  validUserDecisions: Record<string, string>;
  /** The configured `workflow_search_attribute` name (opt-in; absent = none). */
  searchAttributeName: string | undefined;
}

/**
 * Derive the operate target from a resolved workflow spec. Mirrors the ts-plan-argument driver's
 * YAML reads (`binding_ts.py`): connection via `temporalConnectionOptions`, identity from the
 * spec, the review decision → route map (sorted), and the opt-in search-attribute name.
 */
export function operateTargetFromSpec(spec: TypefluxYamlSpec): OperateTarget {
  // The resolved-spec fallback: a single `review`, or the union over all `gates` (#55 slice 4).
  // Used only when the running execution reports no waiting gate; otherwise the execution's own
  // waiting_gates are preferred (§6 drift caveat, applied in `status()`).
  const lifecycle = spec.workflow.lifecycle;
  const merged: Record<string, string> = {};
  const gateSources = [
    ...(lifecycle?.review !== undefined ? [lifecycle.review] : []),
    ...(lifecycle?.gates ?? []),
  ];
  for (const gate of gateSources) {
    for (const [decision, route] of Object.entries(gate.user_decisions)) {
      merged[decision] = route.route;
    }
  }
  // Sorted, like Python's `sorted(user_decisions.items())` — stable wire order.
  const validUserDecisions: Record<string, string> = {};
  for (const decision of Object.keys(merged).sort()) {
    validUserDecisions[decision] = merged[decision]!;
  }
  return {
    connection: temporalConnectionOptions(spec),
    projectName: spec.project,
    workflowName: spec.workflow.name,
    versionLabel: spec.workflow.version,
    taskQueue: spec.task_queue,
    validUserDecisions,
    searchAttributeName: spec.runtime.temporal.workflow_search_attribute,
  };
}

/** What a control-plane start returns (Python `WorkflowStartReceipt`). snake_case wire DTO. */
export interface ApiWorkflowStartReceipt {
  workflow_id: string;
  run_id: string | null;
  workflow_name: string;
  workflow_type: string;
  spec_digest: string;
  task_queue: string;
  trace_query_hint: Record<string, unknown>;
}

/** Identity returned by a control-plane migrate (Python `WorkflowMigrateResult`). snake_case wire. */
export interface ApiWorkflowMigrateResult {
  execution_id: string;
  old_run_id: string;
  new_run_id: string | null;
  old_version_key: string;
  new_version_key: string;
  abandoned_gate_ids: string[];
  /** True for a preview (#791): every preflight ran, nothing was terminated or started. */
  dry_run: boolean;
}

/** Which pinned runtime the mutating ops are bound to (Python `RuntimePinInfo`). */
export interface ApiRuntimePinInfo {
  spec_digest: string | null;
  pinned_at: string | null;
}

/** A lifecycle snapshot plus the review decisions valid for this version (Python `WorkflowOperationStatus`). */
export interface ApiWorkflowOperationStatus {
  workflow_id: string;
  run_id: string | null;
  status: WorkflowLifecycleStatus;
  valid_user_decisions: Record<string, string>;
  recommended_poll_interval_seconds: number;
  runtime_pin: ApiRuntimePinInfo | null;
}

/**
 * The review decisions the control plane offers for a status snapshot (#55 §6). Prefer the
 * EXECUTION-reported set — the running workflow's own `waiting_gates[].valid_user_decisions` —
 * over the resolved-spec set: a drifted execution pins the plan it started with. When the
 * execution reports >=1 waiting gate, union their decisions (sorted); otherwise fall back to
 * the resolved spec. For a single-gate workflow the two coincide, so the wire is byte-identical.
 * `resolved` is a THUNK so the fallback is computed only when actually needed — never on the
 * waiting-gate hot path (Python `effective_valid_user_decisions`).
 */
export function effectiveValidUserDecisions(
  status: WorkflowLifecycleStatus,
  resolved: () => Record<string, string>,
): Record<string, string> {
  if (status.waiting_gates !== undefined && status.waiting_gates.length > 0) {
    const merged: Record<string, string> = {};
    for (const gate of status.waiting_gates) {
      Object.assign(merged, gate.valid_user_decisions);
    }
    const sorted: Record<string, string> = {};
    for (const decision of Object.keys(merged).sort()) {
      sorted[decision] = merged[decision]!;
    }
    return sorted;
  }
  return resolved();
}

/** Python `RECOMMENDED_STATUS_POLL_INTERVAL_SECONDS`. */
export const RECOMMENDED_STATUS_POLL_INTERVAL_SECONDS = 1.0;

/** The shared wire names (contracts/temporal-binding/binding.v1.json). */
export const LIFECYCLE_STATUS_QUERY = "typeflux_lifecycle_status";
export const REQUEST_CANCEL_SIGNAL = "typeflux_request_cancel";
export const SUBMIT_REVIEW_SIGNAL = "typeflux_submit_review";

/** Canonical prefix for the migrate termination reason (Python `MIGRATE_TERMINATION_REASON_PREFIX`);
 * the live proof and audit tooling key on it. An operator note is appended after a colon. */
export const MIGRATE_TERMINATION_REASON_PREFIX = "typeflux migrate to";

/** The reason recorded when the old run is terminated (Python `migrate_termination_reason`). */
export function migrateTerminationReason(newVersionKey: string, reason: string | null): string {
  const text = `${MIGRATE_TERMINATION_REASON_PREFIX} ${newVersionKey}`;
  const note = (reason ?? "").trim();
  return note.length > 0 ? `${text}: ${note}` : text;
}

/** Standard gRPC NOT_FOUND — Temporal's answer to a terminate against a closed execution. */
const GRPC_NOT_FOUND = 5;

/**
 * True when a terminate failed because the execution is already closed (Python
 * `is_execution_closed_error`): `@temporalio/client` throws `WorkflowNotFoundError` when the
 * mutable-state lookup misses (completed/terminated), and the raw gRPC NOT_FOUND may sit on the
 * wrapped `cause` (the `ServiceError` pattern `isTemporalUnavailable` also walks). Both shapes
 * classify as the 409 race, never an opaque 500.
 */
function isExecutionClosedError(error: unknown): boolean {
  if (!(error instanceof Error)) return false;
  let current: unknown = error;
  for (let depth = 0; depth < 5 && current instanceof Error; depth += 1) {
    if (current.name === "WorkflowNotFoundError") return true;
    if ((current as { code?: unknown }).code === GRPC_NOT_FOUND) return true;
    const message = current.message.toLowerCase();
    if (message.includes("already completed") || message.includes("workflow not found")) {
      return true;
    }
    current = (current as { cause?: unknown }).cause;
  }
  return false;
}

export interface WorkflowOperationsOptions {
  /** Injectable Temporal seam; defaults to a real `@temporalio/client` connection. */
  clientFactory?: OperationsClientFactory;
  /**
   * Resolves `workflow:` sub-workflow references so the started plan embeds child plans+identity
   * (#55 §3.4). Absent ⇒ the spec is planned standalone and a sub-workflow step rejects (422). The
   * CP builds one scoped to this workflow's environment.
   */
  subworkflows?: SubworkflowSpecResolver;
}

/**
 * The operate-tier facade for one resolved workflow (Python `WorkflowOperations` over the
 * `TsPlanArgumentDriver`). Holds the resolved spec + target; connects lazily on the first
 * operation and reuses the client until `shutdown`.
 */
export class WorkflowOperations {
  private readonly spec: TypefluxYamlSpec;
  private readonly target: OperateTarget;
  private readonly clientFactory: OperationsClientFactory;
  private readonly subworkflows: SubworkflowSpecResolver | undefined;
  private client: OperationsClient | undefined;
  /** Set by shutdown(): an in-flight connect must self-close instead of leaking. */
  private abandoned = false;
  /** Cached once per instance — the derived plan + its digest (shared by start + memo identity). */
  private cachedPlan: WorkflowPlan | undefined;
  private planDigest: string | undefined;

  constructor(spec: TypefluxYamlSpec, options: WorkflowOperationsOptions = {}) {
    this.spec = spec;
    this.target = operateTargetFromSpec(spec);
    this.clientFactory = options.clientFactory ?? defaultOperationsClient;
    this.subworkflows = options.subworkflows;
  }

  /** decision → route target for the resolved version (Python `valid_user_decisions`). */
  validUserDecisions(): Record<string, string> {
    return { ...this.target.validUserDecisions };
  }

  private async connect(): Promise<OperationsClient> {
    if (this.client === undefined) {
      const pending = this.clientFactory(this.target.connection);
      // A bounded race can abandon this connect mid-handshake: `shutdown()` cannot close a
      // client that was never assigned, so the race-loser closes ITSELF when it eventually
      // resolves (the executions.ts pattern, hoisted to the connect phase).
      pending
        .then((client) => {
          if (this.abandoned) void client.close().catch(() => undefined);
        })
        .catch(() => undefined);
      this.client = await pending;
      if (this.abandoned) {
        // shutdown() raced ahead of the assignment — hand the close back to it.
        this.shutdown();
        throw new Error("operations client shut down during connect");
      }
    }
    return this.client;
  }

  /**
   * The derived plan, built ONCE per instance. A bad graph after a clean resolution (reserved/
   * duplicate step id, invalid review route) is a CONFIG failure — 422, like the executions
   * projection, never a 500.
   */
  private plan(): WorkflowPlan {
    if (this.cachedPlan === undefined) {
      try {
        this.cachedPlan = workflowPlanFromSpec(
          this.spec,
          this.subworkflows !== undefined ? { subworkflows: this.subworkflows } : {},
        );
      } catch (error) {
        throw new ProjectControlPlaneError(
          `failed to derive the workflow plan: ${error instanceof Error ? error.message : String(error)}`,
          422,
        );
      }
    }
    return this.cachedPlan;
  }

  /** The derived plan's digest, computed once from the cached plan (never rebuilds the plan). */
  private digest(): string {
    if (this.planDigest === undefined) {
      this.planDigest = workflowPlanDigest(this.plan());
    }
    return this.planDigest;
  }

  /**
   * Memo-verify + re-pin a handle for `workflowId` (Python `_bound_handle`). describe() → verify
   * the constant generic type + the memo (project/workflow/version) → re-pin by the described run
   * id → return the handle. A mismatch is a 409 `LifecycleBindingError`, fail closed. The caller
   * wraps this in `temporalBounded`, so a connect/timeout during describe is a 503.
   */
  private async boundHandle(client: OperationsClient, workflowId: string, runId: string | undefined): Promise<OperationsHandle> {
    let handle = client.getHandle(workflowId, runId);
    const description = await handle.describe();
    const actualType = description.type;
    if (actualType !== YAML_WORKFLOW_TYPE) {
      throw new ProjectControlPlaneError(
        `lifecycle operation refused: execution '${workflowId}' has workflow type ` +
          `'${actualType}', not the ts-plan-argument generic type '${YAML_WORKFLOW_TYPE}'`,
        409,
        "LifecycleBindingError",
      );
    }
    const memo = description.memo ?? {};
    const actualProject = memo["typeflux_project"];
    if (actualProject !== this.target.projectName) {
      throw new ProjectControlPlaneError(
        `lifecycle operation refused: execution '${workflowId}' belongs to project ` +
          `'${String(actualProject)}', not the bound project '${this.target.projectName}'`,
        409,
        "LifecycleBindingError",
      );
    }
    const actualWorkflow = memo["typeflux_workflow"];
    if (actualWorkflow !== this.target.workflowName) {
      throw new ProjectControlPlaneError(
        `lifecycle operation refused: execution '${workflowId}' is workflow ` +
          `'${String(actualWorkflow)}', not the bound workflow '${this.target.workflowName}'`,
        409,
        "LifecycleBindingError",
      );
    }
    if (this.target.versionLabel !== undefined) {
      // The ts profile freezes the version in the memo — routing to a versioned workflow must
      // verify it (binding contract binding_verification_extra).
      const actualVersion = memo["typeflux_workflow_version"];
      if (actualVersion !== this.target.versionLabel) {
        throw new ProjectControlPlaneError(
          `lifecycle operation refused: execution '${workflowId}' carries version ` +
            `'${String(actualVersion)}', not the routed version '${this.target.versionLabel}'`,
          409,
          "LifecycleBindingError",
        );
      }
    }
    if (runId === undefined) {
      // Pin to the run describe() verified: an id-only handle targets the LATEST run at dispatch,
      // so workflow-id reuse could swap the target between verification and the query/signal.
      const boundRunId = description.runId;
      if (typeof boundRunId === "string" && boundRunId.length > 0) {
        handle = client.getHandle(workflowId, boundRunId);
      }
    }
    return handle;
  }

  /**
   * Start one execution (Python `TsPlanArgumentDriver.start` via the resolved runtime): derive the
   * plan + digest ONCE, stamp the identity memo, dispatch with the fixed `[plan, input]` argument
   * order + the spec task queue + the configured search attribute. Bounded → 503.
   *
   * `taskQueue` follows Python's `task_queue or self.spec.task_queue` (TRUTHINESS, not `??`): an
   * empty-string override falls back to the spec's task queue, never dispatches to `""`.
   *
   * FROZEN VERSION (#530): the binding contract's ts profile enforces a `workflow.version` label at
   * start via a bounded visibility scan (`enforceFrozenWorkflowVersion`). This operate-tier client
   * seam does not expose visibility (`workflow.list`), so start-time frozen-version enforcement is
   * DEFERRED here — the memo still stamps the digest + label (so a driver WITH visibility can
   * enforce), and lifecycle ops still memo-verify the routed label. Wiring the visibility scan into
   * the client seam is a follow-up (#530 operate-tier enforcement); it is intentionally NOT claimed
   * to run, rather than silently skipped behind a comment.
   */
  /**
   * The frozen `workflow.version` gate (#662), shared by the start leg and the migrate
   * PREFLIGHT (#204 review: it must run BEFORE the terminate, while the old run is alive).
   * A frozen label is a pointer to ONE graph (Python raises a ValueError → 422 from its
   * runtime build). The scan is bounded like every Temporal call and degrades best-effort
   * (warn + skip) when the client exposes no visibility. No-op without a version label.
   */
  private async enforceFrozenVersion(
    client: OperationsClient,
    digest: string,
    what: string,
  ): Promise<void> {
    if (this.target.versionLabel === undefined) return;
    const searchAttribute =
      this.target.searchAttributeName !== undefined
        ? { name: this.target.searchAttributeName, value: this.target.workflowName }
        : undefined;
    const listClient = {
      workflow: {
        list:
          client.list !== undefined
            ? (options: { query: string }) => client.list!(options.query)
            : (undefined as never),
      },
    };
    try {
      await temporalBounded(
        () =>
          enforceFrozenWorkflowVersion(listClient as Parameters<typeof enforceFrozenWorkflowVersion>[0], {
            workflowType: YAML_WORKFLOW_TYPE,
            workflowName: this.target.workflowName,
            project: this.target.projectName,
            versionLabel: this.target.versionLabel!, // guarded above
            planDigest: digest,
            ...(searchAttribute !== undefined ? { searchAttribute } : {}),
          }),
        what,
        client,
      );
    } catch (error) {
      if (error instanceof Error && error.message.includes("is frozen to spec digest")) {
        // Reusing a frozen label for a changed graph is a CONFIG mistake — 422, like Python.
        throw new ProjectControlPlaneError(error.message, 422);
      }
      throw error;
    }
  }

  async start(
    inputValue: unknown,
    options: { workflowId: string; taskQueue?: string | null; extraMemo?: Record<string, string> },
  ): Promise<ApiWorkflowStartReceipt> {
    // Build the plan ONCE — its digest and the start argument are the same plan.
    const plan = this.plan();
    const digest = this.digest();
    // Additive provenance (#204 migrate): merged UNDER the identity keys so it can never overwrite
    // them (the drift-guard set stays authoritative). Absent ⇒ byte-identical to a normal start.
    const memo = { ...(options.extraMemo ?? {}), ...workflowIdentityMemo(this.spec, digest) };
    // Python: `task_queue or self.runtime.spec.task_queue` — empty string falls through.
    const taskQueue = options.taskQueue ? options.taskQueue : this.target.taskQueue;
    const searchAttribute =
      this.target.searchAttributeName !== undefined
        ? { name: this.target.searchAttributeName, value: this.target.workflowName }
        : undefined;
    const client = await temporalBounded(() => this.connect(), "starting the execution");
    await this.enforceFrozenVersion(client, digest, "starting the execution");
    return temporalBounded(async () => {
      const started = await client.start({
        workflowType: YAML_WORKFLOW_TYPE,
        taskQueue,
        workflowId: options.workflowId,
        // The optional third arg is the non-identity start context (#55 §6): the search-attribute
        // name, so the interpreter can stamp each CHILD's own logical name into it. Passed only
        // when configured, so starts with no search attribute stay byte-identical (2-arg).
        args:
          this.target.searchAttributeName !== undefined
            ? [plan, inputValue, { searchAttribute: this.target.searchAttributeName }]
            : [plan, inputValue],
        memo,
        searchAttribute,
      });
      return {
        workflow_id: options.workflowId,
        run_id: typeof started.runId === "string" ? started.runId : null,
        workflow_name: this.target.workflowName,
        workflow_type: YAML_WORKFLOW_TYPE,
        spec_digest: digest,
        task_queue: taskQueue,
        trace_query_hint: { workflow_id: options.workflowId, limit: 1 },
      };
    }, "starting the execution", client);
  }

  /** Query lifecycle status + the valid review decisions (Python `status`). Bounded → 503. */
  async status(
    workflowId: string,
    options: { runId?: string | null } = {},
  ): Promise<ApiWorkflowOperationStatus> {
    const runId = options.runId ?? undefined;
    const client = await temporalBounded(() => this.connect(), "querying execution status");
    return temporalBounded(async () => {
      const handle = await this.boundHandle(client, workflowId, runId);
      const status = await handle.query<WorkflowLifecycleStatus>(LIFECYCLE_STATUS_QUERY);
      // A run started on a pre-slice-4 worker reports no `waiting_gates`; the generated client
      // types promise the always-present-[] status convention, so normalize before returning
      // (Python normalizes via the pydantic default on model_validate).
      status.waiting_gates ??= [];
      return {
        workflow_id: workflowId,
        run_id: runId ?? null,
        status,
        // Prefer the running execution's own waiting-gate decisions over the resolved spec
        // (#55 §6 drift caveat); identical for single-gate workflows. Lazy fallback: the
        // resolved-spec set is only computed when no gate is waiting.
        valid_user_decisions: effectiveValidUserDecisions(status, () => this.validUserDecisions()),
        recommended_poll_interval_seconds: RECOMMENDED_STATUS_POLL_INTERVAL_SECONDS,
        runtime_pin: null,
      };
    }, "querying execution status", client);
  }

  /** Route a waiting review checkpoint (Python `submit_review`). Bounded → 503. */
  async submitReview(
    workflowId: string,
    command: ReviewCommand,
    options: { runId?: string | null } = {},
  ): Promise<void> {
    const runId = options.runId ?? undefined;
    const client = await temporalBounded(() => this.connect(), "submitting the review decision");
    await temporalBounded(async () => {
      const handle = await this.boundHandle(client, workflowId, runId);
      await handle.signal(SUBMIT_REVIEW_SIGNAL, command);
    }, "submitting the review decision", client);
  }

  /** Request graceful cancellation (Python `request_cancel`). Bounded → 503. */
  async requestCancel(
    workflowId: string,
    reason: string | null | undefined,
    options: { runId?: string | null } = {},
  ): Promise<void> {
    const runId = options.runId ?? undefined;
    const client = await temporalBounded(() => this.connect(), "requesting cancellation");
    await temporalBounded(async () => {
      const handle = await this.boundHandle(client, workflowId, runId);
      // The cancel signal's arg is `reason: string | null` (contracts/temporal-binding).
      await handle.signal(REQUEST_CANCEL_SIGNAL, reason ?? null);
    }, "requesting cancellation", client);
  }

  /**
   * Terminate a running execution and resubmit it against THIS version (Python
   * `TsPlanArgumentDriver.migrate`, #204). The long-drain primitive: terminate-and-resubmit with
   * input carry-over. Every version shares the generic type and version identity lives in the memo,
   * so — unlike `boundHandle` — the old execution is verified for project + logical workflow only
   * and its version identity is REQUIRED to differ (the same-version guard).
   *
   * PREFLIGHT-BEFORE-TERMINATE (#204 review): every start-leg precondition runs while the old run
   * is alive — same-version (422), no serving workers (422), open gate without `abandonGates`
   * (422), carried-input validation via `validateCarriedInput` (the CP passes the current
   * version's input schema; 422 on mismatch), and the frozen-version gate (422). After the
   * terminate, only a genuine transport/race error remains: a terminate against an
   * already-closed execution is a 409 `MigrateExecutionClosedError`, and a failed replacement
   * start is a DISTINGUISHED 500 `MigratePartialError` naming the terminated run. The start leg
   * reuses `start` (identity memo, search attribute) with additive `typeflux_migrated_from`
   * provenance. Bounded → 503.
   */
  async migrate(
    executionId: string,
    options: {
      runId?: string | null;
      abandonGates?: boolean;
      reason?: string | null;
      /** Preview (#791): run every preflight — binding, same-version, pollers, gates,
       * input read/validate, frozen version — and return without terminating. */
      dryRun?: boolean;
      /**
       * Validate/coerce the carried-over input against the CURRENT version's input schema,
       * throwing a 422 `ProjectControlPlaneError` on mismatch — runs pre-terminate. The control
       * plane supplies this from its injected zod schema; absent, the input is carried verbatim.
       */
      validateCarriedInput?: (input: unknown) => unknown;
    } = {},
  ): Promise<ApiWorkflowMigrateResult> {
    const runId = options.runId ?? undefined;
    const abandonGates = options.abandonGates ?? false;
    const digest = this.digest();
    const newVersionKey = versionIdentityKey(
      this.target.workflowName,
      this.target.versionLabel,
      digest,
    );
    const client = await temporalBounded(() => this.connect(), "migrating the execution");
    const { inputValue, oldRunId, oldVersionKey, abandonedGateIds, dryRun } = await temporalBounded(
      async () => {
        let handle = client.getHandle(executionId, runId);
        const description = await handle.describe();
        if (description.type !== YAML_WORKFLOW_TYPE) {
          throw new ProjectControlPlaneError(
            `migrate refused: execution '${executionId}' has workflow type ` +
              `'${description.type}', not the ts-plan-argument generic type '${YAML_WORKFLOW_TYPE}'`,
            409,
            "LifecycleBindingError",
          );
        }
        const memo = description.memo ?? {};
        if (memo["typeflux_project"] !== this.target.projectName) {
          throw new ProjectControlPlaneError(
            `migrate refused: execution '${executionId}' belongs to project ` +
              `'${String(memo["typeflux_project"])}', not the bound project '${this.target.projectName}'`,
            409,
            "LifecycleBindingError",
          );
        }
        if (memo["typeflux_workflow"] !== this.target.workflowName) {
          throw new ProjectControlPlaneError(
            `migrate refused: execution '${executionId}' is workflow ` +
              `'${String(memo["typeflux_workflow"])}', not the bound workflow '${this.target.workflowName}'`,
            409,
            "LifecycleBindingError",
          );
        }
        const describedRunId = description.runId;
        if (runId === undefined && typeof describedRunId === "string" && describedRunId.length > 0) {
          handle = client.getHandle(executionId, describedRunId);
        }
        const resolvedOldRunId =
          typeof describedRunId === "string" && describedRunId.length > 0 ? describedRunId : (runId ?? "");
        const versionMemo = memo["typeflux_workflow_version"];
        const digestMemo = memo["typeflux_spec_digest"];
        const oldKey = versionIdentityKey(
          this.target.workflowName,
          typeof versionMemo === "string" ? versionMemo : undefined,
          typeof digestMemo === "string" ? digestMemo : undefined,
        );
        if (oldKey === newVersionKey) {
          throw new ProjectControlPlaneError(
            `migrate refused: execution '${executionId}' already runs the current version ` +
              `'${newVersionKey}'; migrating onto the same graph version is a no-op — nothing to migrate to`,
            422,
          );
        }
        // Fail-closed BEFORE terminating: the new run targets this version's task queue.
        if (client.pollerCount === undefined) {
          throw new ProjectControlPlaneError(
            "migrate refused: this control plane cannot verify serving workers on the target task " +
              "queue (the client seam reports no poller count); refusing rather than risk a pending run",
            422,
          );
        }
        const pollers = await client.pollerCount(this.target.taskQueue);
        if (pollers <= 0) {
          throw new ProjectControlPlaneError(
            `migrate refused: no workers are polling the target task queue '${this.target.taskQueue}' ` +
              `for version '${newVersionKey}'; deploy the new version's workers before migrating ` +
              "(the new run would otherwise sit pending forever)",
            422,
          );
        }
        const status = await handle.query<WorkflowLifecycleStatus>(LIFECYCLE_STATUS_QUERY);
        status.waiting_gates ??= [];
        let abandoned: string[] = [];
        if (status.waiting_gates.length > 0) {
          const gateIds = status.waiting_gates.map((gate) => gate.gate_id);
          if (!abandonGates) {
            throw new ProjectControlPlaneError(
              `migrate refused: execution '${executionId}' is waiting at review gate(s) ` +
                `${gateIds.join(", ")}; deciding the gate first preserves the human decision, or pass ` +
                "abandon_gates to acknowledge that terminate-and-resubmit discards the pending review",
              422,
            );
          }
          abandoned = gateIds;
        }
        if (handle.fetchStartInput === undefined || handle.terminate === undefined) {
          throw new ProjectControlPlaneError(
            "migrate refused: this control plane's client seam cannot read the original input or " +
              "terminate the execution (migrate is unavailable on this transport)",
            422,
          );
        }
        // PREFLIGHT-BEFORE-TERMINATE (#204 review): every start-leg precondition runs while the
        // old execution is alive, so a static failure never leaves it dead with no replacement.
        // 1. Input carry-over: read AND validate against the CURRENT version's input schema.
        const rawInput = await handle.fetchStartInput();
        const carriedInput =
          options.validateCarriedInput !== undefined ? options.validateCarriedInput(rawInput) : rawInput;
        // 2. Frozen version: the same gate the start leg runs, as a read-only preflight.
        await this.enforceFrozenVersion(client, digest, "migrating the execution");
        if (options.dryRun === true) {
          // Preview (#791): every preflight above ran; stop before the mutation.
          return {
            inputValue: carriedInput,
            oldRunId: resolvedOldRunId,
            oldVersionKey: oldKey,
            abandonedGateIds: abandoned,
            dryRun: true,
          };
        }
        try {
          await handle.terminate(migrateTerminationReason(newVersionKey, options.reason ?? null));
        } catch (error) {
          if (isExecutionClosedError(error)) {
            // The race, not a fault: the execution closed (completed, or a concurrent migrate
            // terminated it) between preflight and terminate. Nothing was started.
            throw new ProjectControlPlaneError(
              `migrate conflict: execution '${executionId}' (run '${resolvedOldRunId || "<unknown>"}') ` +
                "is already closed — it may have completed or been migrated concurrently; nothing " +
                "was terminated or started",
              409,
              "MigrateExecutionClosedError",
            );
          }
          throw error;
        }
        return {
          inputValue: carriedInput,
          oldRunId: resolvedOldRunId,
          oldVersionKey: oldKey,
          abandonedGateIds: abandoned,
          dryRun: false,
        };
      },
      "migrating the execution",
      client,
    );

    if (dryRun) {
      return {
        execution_id: executionId,
        old_run_id: oldRunId,
        new_run_id: null,
        old_version_key: oldVersionKey,
        new_version_key: newVersionKey,
        abandoned_gate_ids: abandonedGateIds,
        dry_run: true,
      };
    }
    const provenance: Record<string, string> = { typeflux_migrated_from_version: oldVersionKey };
    if (oldRunId.length > 0) provenance["typeflux_migrated_from"] = oldRunId;
    let receipt: ApiWorkflowStartReceipt;
    try {
      receipt = await this.start(inputValue, {
        workflowId: executionId,
        extraMemo: provenance,
      });
    } catch (error) {
      // The DISTINGUISHED partial-failure shape (#204 review): the old run is gone, the
      // replacement did not start — never the refusal 422 shape. Every static precondition was
      // preflighted above, so only a genuine transport/race error reaches this.
      const cause = error instanceof Error ? error.message : String(error);
      throw new ProjectControlPlaneError(
        `migrate partially completed: old run ${oldRunId || "<unknown>"} of execution ` +
          `'${executionId}' was already terminated; the replacement start failed: ${cause}. ` +
          "The carried input is intact in the terminated run's history — resubmit via a normal start.",
        500,
        "MigratePartialError",
      );
    }
    return {
      execution_id: executionId,
      old_run_id: oldRunId,
      new_run_id: receipt.run_id,
      old_version_key: oldVersionKey,
      new_version_key: newVersionKey,
      abandoned_gate_ids: abandonedGateIds,
      dry_run: false,
    };
  }

  /**
   * Drop the reusable client (Python `shutdown`). Idempotent. Also marks the instance
   * abandoned so a connect still in flight (a bounded-race loser) closes itself on arrival —
   * without the flag, a slow handshake that lost the timeout race would leak its connection.
   */
  shutdown(): void {
    this.abandoned = true;
    const client = this.client;
    this.client = undefined;
    if (client !== undefined) {
      client.close().catch(() => undefined);
    }
  }
}
