/**
 * Wire a validated spec into a runnable worker (parity Epic 5, #452; Python
 * `yaml/runtime.py` `build_runtime` + `TypefluxYamlRuntime.execute_workflow`).
 *
 * `assembleYamlRuntime` is the pure, unit-testable core (activities map + workflow
 * plan, no server); `buildRuntime` adds the live `createTypefluxWorker`. The provider
 * is injected or built from the spec (#449); observers wire via `observerFromSpec`
 * (#451); the artifact admission policy wires from `runtime.artifacts` (#481);
 * `runtime.provider_limits` wires one shared `ProviderRateLimitController` across
 * the assembled activities (#529).
 */

import { requireTemporalEngine, requireTemporalWorkflowGraph } from "@typeflux/yaml/engine";

import { assembleRuntimeServices, enforcePolicyPreflight, type YamlRuntimeOptions } from "@typeflux/yaml/runtime-assembly";

import type { DataConverter } from "@temporalio/common";
import {
  buildTemporalActivities,
  createTypefluxWorker,
  executeWorkflow,
  startWorkflow,
  type TemporalActivities,
  type TypefluxWorkerOptions,
} from "@typeflux/temporal-worker";

import { langfuseObserverFromSpec } from "./langfuse-observer.js";
import { langfuseRegistryTransportFromSpec } from "./langfuse-registry.js";
import { langsmithObserverFromSpec } from "./langsmith-observer.js";
import { langsmithRegistryTransportFromSpec } from "./langsmith-registry.js";
import {
  enforceFrozenWorkflowVersion,
  workflowIdentityMemo,
  workflowPlanDigest,
  type WorkflowListClient,
} from "@typeflux/yaml/frozen-version";
import { specReferencesSubworkflows } from "./project-validation.js";
import {
  buildSubjectAwarePayloadCodec,
  InMemorySubjectKeystore,
  SubjectScopedPayloadCodec,
  type SubjectBindingClient,
  type SubjectKeystore,
} from "./subject-keystore.js";
import { SUBJECT_IDS_SEARCH_ATTRIBUTE, effectiveSubjectIds } from "./subjects.js";
import { providerTransportsFromSpec } from "./provider-transports-from-env.js";
import { type TypefluxYamlSpec } from "@typeflux/yaml/spec";
import {
  type WorkflowPlan,
} from "@typeflux/yaml/workflow-plan";

/** The workflow type the generic interpreter is registered under (see `workflows.ts`). */
export const YAML_WORKFLOW_TYPE = "typefluxYamlWorkflow";

export interface AssembleYamlRuntimeOptions extends YamlRuntimeOptions {
  /** Overrides spec.task_queue for Temporal registration. */
  taskQueue?: string;
}

export interface AssembledYamlRuntime {
  activities: TemporalActivities;
  plan: WorkflowPlan;
  taskQueue: string;
}

export function assembleYamlRuntime(spec: TypefluxYamlSpec, options: AssembleYamlRuntimeOptions): AssembledYamlRuntime {
  requireTemporalEngine(spec);
  const assembled = assembleRuntimeServices(spec, options);
  const activities = buildTemporalActivities(Object.values(assembled.descriptors).map(descriptor => ({
    descriptor, options: assembled.options,
  })));
  return { activities, plan: assembled.plan, taskQueue: options.taskQueue ?? requireTemporalEngine(spec) };
}

export interface BuildRuntimeOptions extends AssembleYamlRuntimeOptions {
  /**
   * Path to the workflow module that registers `typefluxYamlWorkflow`. Defaults to this
   * package's BUILT module (`dist/workflows.js`); override it to point at the TS source
   * (e.g. when running through a bundler/vitest where `dist` is not used).
   */
  workflowsPath?: string;
  /** Passthrough to `createTypefluxWorker` (connection, namespace, …). */
  worker?: Omit<TypefluxWorkerOptions, "taskQueue" | "activities" | "workflowsPath" | "workflowBundle">;
  /**
   * The crypto-shred keystore backend for a spec declaring
   * `payload_codec.subject_scope` (#715 slice 4). Absent ⇒ the process-local in-memory
   * reference backend is built (test/dev ONLY — a starter and worker in different
   * processes would mint different keys for the same subject; see docs/privacy.md).
   */
  subjectKeystore?: SubjectKeystore;
}

export interface TypefluxYamlRuntime {
  worker: Awaited<ReturnType<typeof createTypefluxWorker>>;
  plan: WorkflowPlan;
  /** sha256 over the plan's canonical JSON (#530) — stamped into every start's memo. */
  specDigest: string;
  taskQueue: string;
  /**
   * The payload-codec `dataConverter` this runtime wired into its worker (#188), or
   * `undefined` when the spec declares no `runtime.temporal.payload_codec`. Callers that
   * START workflows against this runtime MUST construct their Temporal `Client` with it —
   * `new Client({ connection, namespace, dataConverter: runtime.dataConverter })` — so a
   * workflow's START payloads (plan + input) are encrypted in history exactly like the
   * results the worker encrypts. `runWorkflow` fail-closes (D188-3) when a codec is declared
   * but the supplied client lacks one. Typed like `@temporalio/client`'s
   * `ClientOptions.dataConverter`.
   */
  dataConverter?: DataConverter;
  /** Start the YAML workflow and await its result. */
  runWorkflow(
    client: Parameters<typeof executeWorkflow>[0],
    input: unknown,
    options: {
      workflowId: string;
      /**
       * Explicit subject id override (#715 slice 1). A NON-EMPTY override wins
       * over the spec `subjects:` extraction; an empty array is treated as "no
       * override" and falls through to extraction (subjects are erasure-critical
       * — there is no "explicitly no subjects" opt-out of a declared block).
       * Stamps the `TypefluxSubjectIds` keyword-list search attribute and fans
       * to the observer / cache carriers.
       */
      subjectIds?: readonly string[];
    },
  ): Promise<unknown>;
  /**
   * Await every trace still in flight from the runtime-built observer
   * (streams per activity; this is the graceful-exit drain for short-lived
   * processes), then shut down its transport. This is terminal: do not reuse
   * the runtime afterward. BEST-EFFORT: a failed tail flush is logged, never thrown —
   * safe to call from a finally. No-op when the spec doesn't trace or the
   * caller supplied their own observer (whose lifecycle they own).
   */
  drainObservability(): Promise<void>;
}

/** The auto-observer the OOTB wiring built (kept for the graceful-exit drain). */
type AutoWiredObserver =
  | NonNullable<Awaited<ReturnType<typeof langfuseObserverFromSpec>>>
  | NonNullable<Awaited<ReturnType<typeof langsmithObserverFromSpec>>>;

/**
 * The ONE out-of-the-box options assembly (Python `build_runtime` parity), shared by
 * {@link buildRuntime} and the `typeflux-yaml-worker` entrypoint's `--preflight` (#687
 * review: preflight MUST compose exactly the wiring the live worker runs with, or the
 * generated command fails supported OOTB deployments before the readiness marker):
 *
 * - Governance pre-flight FIRST, even before the auto-observer: a non-compliant spec
 *   must refuse to build before ANYTHING is constructed — including the langfuse SDK
 *   client — and its error must be the policy violation, never a missing-peer install
 *   hint (finder). Idempotent; assembleYamlRuntime re-runs it as its own fail-closed
 *   contract. Includes the closure admission (#300/#298) — cascade + tree-wide ceilings.
 * - Out-of-the-box langfuse/langsmith OBSERVERS: a spec declaring either tracing backend
 *   traces with NO caller wiring — the official SDK is lazy-loaded and the observer
 *   built from the environment (each helper self-gates on its own type). An explicit
 *   `observer` always wins; `type: none` is unchanged.
 * - Out-of-the-box langfuse/langsmith PROMPT REGISTRIES (Python `_build_registry`):
 *   an explicit `registry`/`registryTransport` wins; `inline` self-builds in
 *   `registryFromSpec`. Unlike the observer, a declared-but-unbuildable registry THROWS
 *   inside the helper — prompts are load-bearing, so there is no untraced-style degrade.
 * - Out-of-the-box vendor PROVIDERS (Python `_build_provider`): openai/anthropic/gemini
 *   execute with NO caller wiring — the official SDK is lazy-loaded and the thin
 *   transport built from the environment. An explicit `provider`/`transports` wins.
 *   Like the registry, a missing credential THROWS: a run cannot degrade past its
 *   provider.
 *
 * Returns the assembled options plus the auto-built observer (for the drain helper).
 */
export async function autoWireRuntimeOptions<T extends AssembleYamlRuntimeOptions>(
  spec: TypefluxYamlSpec,
  options: T,
): Promise<{ options: T; autoObserver: AutoWiredObserver | undefined }> {
  requireTemporalEngine(spec);
  if (options.policy !== undefined) {
    enforcePolicyPreflight(spec, options.policy, options.subworkflows);
  }
  requireTemporalWorkflowGraph(spec, options.subworkflows);
  const autoObserver =
    options.observer === undefined
      ? ((await langfuseObserverFromSpec(spec)) ?? (await langsmithObserverFromSpec(spec)))
      : undefined;
  const autoRegistryTransport =
    options.registry === undefined && options.registryTransport === undefined
      ? ((await langfuseRegistryTransportFromSpec(spec)) ?? (await langsmithRegistryTransportFromSpec(spec)))
      : undefined;
  const autoTransports =
    options.provider === undefined && options.transports === undefined
      ? await providerTransportsFromSpec(spec)
      : undefined;
  return {
    options: {
      ...options,
      ...(autoObserver !== undefined ? { observer: autoObserver } : {}),
      ...(autoRegistryTransport !== undefined ? { registryTransport: autoRegistryTransport } : {}),
      ...(autoTransports !== undefined ? { transports: autoTransports } : {}),
    },
    autoObserver: autoObserver ?? undefined,
  };
}

/**
 * Build a Temporal worker that serves the spec's activities + the generic YAML
 * workflow, plus a `runWorkflow` helper that starts `typefluxYamlWorkflow` with the
 * derived plan. `createTypefluxWorker` connects to a server.
 */
export async function buildRuntime(
  spec: TypefluxYamlSpec,
  options: BuildRuntimeOptions,
): Promise<TypefluxYamlRuntime> {
  // The OOTB auto-wiring + governance-first ordering live in ONE shared assembly (see
  // autoWireRuntimeOptions) so the worker entrypoint's --preflight composes exactly this.
  const { options: assembled, autoObserver } = await autoWireRuntimeOptions(spec, options);
  const { activities, plan, taskQueue } = assembleYamlRuntime(spec, assembled);
  // Build the AES-256-GCM codec ONCE from the resolved spec (#188), fail-closed on a
  // missing/wrong-length key. Absent payload_codec ⇒ no codec ⇒ plaintext (digests
  // unchanged). A declared `subject_scope` (#715 slice 4) wraps it in the
  // subject-scoped codec (per-subject crypto-shred).
  let subjectKeystore = options.subjectKeystore;
  if (spec.runtime.temporal.payload_codec?.subject_scope !== undefined && subjectKeystore === undefined) {
    // SOLE-OWNER justification for the in-memory default (#715 Bugbot): buildRuntime
    // constructs the worker AND the start-path data converter in this ONE process, and
    // runWorkflow fail-closes unless the caller's Client carries that same converter —
    // so every encode and decode shares one codec + keystore instance, making the
    // process-local reference backend coherent HERE (and only here; the live suite
    // proves the round-trip). Split starter/worker deployments (worker-entry) must
    // inject a shared backend — worker-entry enforces that loudly.
    subjectKeystore = new InMemorySubjectKeystore();
  }
  const codec = buildSubjectAwarePayloadCodec(spec.runtime.temporal.payload_codec, subjectKeystore);
  if (codec instanceof SubjectScopedPayloadCodec && specReferencesSubworkflows(spec.workflow.steps)) {
    // #715 slice 4 boundary: a child's start payloads are encoded (with the CHILD's
    // serialization context) before the child exists, so its subject binding cannot be
    // resolved. Reject the combination loudly rather than fail mid-workflow.
    throw new Error(
      "runtime.temporal.payload_codec.subject_scope does not yet support sub-workflow " +
        "composition (#715 slice 4): a child's input is encoded before the child execution " +
        "exists, so its subject binding cannot be resolved. Remove subject_scope or the " +
        "sub-workflow references.",
    );
  }
  // The dataConverter that carries the spec-declared codec (#188). Wired into BOTH the worker
  // (below) AND exposed on the returned runtime so callers build their START-path Client with
  // the SAME converter — otherwise workflow start payloads (plan + input) ride to history as
  // PLAINTEXT while the worker encrypts results (D188-3 fail-closed; Python wires both sites).
  const codecDataConverter: DataConverter | undefined =
    codec !== undefined ? { payloadCodecs: [codec] } : undefined;
  const workerOptions = {
    ...(options.worker ?? {}),
    taskQueue,
    activities,
    // The generic interpreter ships in this package's built workflow module; callers
    // running from source (vitest/bundler) override `workflowsPath` to the `.ts`.
    workflowsPath: options.workflowsPath ?? new URL("./workflows.js", import.meta.url).pathname,
  };
  if (codec !== undefined) {
    // FAIL-CLOSED (D188-3): a declared codec must never be silently dropped in favor of a
    // caller's converter — that would ride PII to Temporal as PLAINTEXT. A caller supplying
    // their own dataConverter alongside a spec-declared codec is ambiguous; reject it rather
    // than guess. (Drop the spec block to bring your own converter.)
    // Detect a caller's converter at EITHER level: the top-level `dataConverter` OR the
    // nested `workerOptions.workerOptions?.dataConverter` passthrough (TypefluxWorkerOptions),
    // which `workerCreateOptions` spreads and the codec would otherwise silently override.
    if (
      workerOptions.dataConverter !== undefined ||
      workerOptions.workerOptions?.dataConverter !== undefined
    ) {
      throw new Error(
        "runtime.temporal.payload_codec is declared but a worker dataConverter was also supplied; " +
          "remove one — a spec-declared codec must not be silently overridden (fail-closed, #188)",
      );
    }
    workerOptions.dataConverter = codecDataConverter;
  }
  const worker = await createTypefluxWorker(workerOptions);
  const specDigest = workflowPlanDigest(plan);
  const identityMemo = workflowIdentityMemo(spec, specDigest);
  // Frozen workflow.version (#530): checked once per runtime instance, before the
  // first start. Python enforces at worker start (its worker can replay an edited
  // graph); TS's plan-as-argument makes in-flight executions immune, so NEW starts
  // are the only thing a frozen label protects — start time is the natural gate.
  let frozenVersionChecked: Promise<void> | undefined;
  return {
    worker,
    plan,
    specDigest,
    taskQueue,
    // exactOptionalPropertyTypes: only present the field when a codec is declared, so a
    // no-codec runtime reports `dataConverter` absent (⇒ runWorkflow performs no check).
    ...(codecDataConverter !== undefined ? { dataConverter: codecDataConverter } : {}),
    drainObservability: async () => {
      try {
        await autoObserver?.shutdown();
      } catch (error) {
        // Graceful-exit helper: a failed tail flush must not abort the
        // caller's finally (connection close) — tracing is observability,
        // not control flow (Bugbot). Strict callers drain the writer they
        // injected themselves.
        console.error("observability drain failed:", error);
      }
    },
    runWorkflow: async (client, input, runOptions) => {
      // #715 slice 1: a NON-EMPTY explicit subjectIds override wins over the
      // declarative `subjects:` extraction; an EMPTY override falls through to
      // extraction (see effectiveSubjectIds — `subjectIds: []` must never
      // silently bypass a declared block, #715 review round 2). A required
      // selector that contributes zero ids throws HERE, at start.
      const subjectIds = effectiveSubjectIds(
        runOptions.subjectIds,
        input,
        (spec.workflow.subjects ?? []).map((s) => ({ fromPath: s.from, required: s.required })),
      );
      if (codec instanceof SubjectScopedPayloadCodec) {
        // #715 slice 4: pin the execution's subject set for the subject-scoped codec
        // BEFORE the start encodes the input (the execution does not exist in
        // visibility yet, so the registry is the only resolvable channel). Registers
        // the SAME ids stamped into TypefluxSubjectIds; the empty set pins "no
        // subjects". Also (re-)bind the caller's client as the visibility fallback
        // for executions other processes started.
        codec.bindings.register(runOptions.workflowId, subjectIds);
        codec.bindings.bindClient(client as unknown as SubjectBindingClient);
      }
      if (codec !== undefined) {
        // FAIL-CLOSED (D188-3): the worker encrypts activity/result payloads, but a workflow's
        // START payloads (plan + input) are serialized by the CALLER-supplied Client. If that
        // Client was not built with the codec, the start rides to Temporal history as PLAINTEXT
        // while the worker encrypts everything else — silently breaking the codec's promise.
        // Codec instances can't be meaningfully deep-compared across the client boundary, so the
        // practical check is PRESENCE of any payloadCodec on the client's loaded dataConverter.
        // `options.loadedDataConverter.payloadCodecs` is the robust surface: the SDK always
        // normalizes it to an array (`[]` when the caller passed no converter, since
        // defaultBaseClientOptions sets `dataConverter: {}`), unlike the raw `options.dataConverter`.
        const clientPayloadCodecs = client.options?.loadedDataConverter?.payloadCodecs;
        if (clientPayloadCodecs === undefined || clientPayloadCodecs.length === 0) {
          throw new Error(
            "runtime.temporal.payload_codec is declared but the Temporal Client used to start the " +
              "workflow was not built with a payload codec — start payloads (plan + input) would be " +
              "persisted in history as PLAINTEXT. Pass `dataConverter: runtime.dataConverter` when " +
              "constructing the Client (fail-closed, #188).",
          );
        }
      }
      if (spec.workflow.version !== undefined) {
        // Cached promise: one visibility query per runtime instance. A digest
        // mismatch rejects — and KEEPS rejecting on retry (the graph didn't
        // change); visibility failures warn-and-resolve inside the check.
        frozenVersionChecked ??= enforceFrozenWorkflowVersion(client as unknown as WorkflowListClient, {
          workflowType: YAML_WORKFLOW_TYPE,
          workflowName: spec.workflow.name,
          project: spec.project,
          versionLabel: spec.workflow.version,
          planDigest: specDigest,
          // Narrows the scan to THIS workflow's starts when the attribute is
          // configured (it is stamped with the logical name on every start).
          ...(spec.runtime.temporal.workflow_search_attribute !== undefined
            ? {
                searchAttribute: {
                  name: spec.runtime.temporal.workflow_search_attribute,
                  value: spec.workflow.name,
                },
              }
            : {}),
        });
        await frozenVersionChecked;
      }
      const handle = await startWorkflow(client, {
        workflowType: YAML_WORKFLOW_TYPE,
        taskQueue,
        workflowId: runOptions.workflowId,
        // The optional third arg is the non-identity start context (#55 §6): the configured
        // search attribute name, so the interpreter can stamp each CHILD's own logical name into
        // it. Passed only when configured, so pre-existing starts stay byte-identical (2-arg).
        args:
          spec.runtime.temporal.workflow_search_attribute !== undefined
            ? [plan, input, { searchAttribute: spec.runtime.temporal.workflow_search_attribute }]
            : [plan, input],
        // Identity memo on EVERY start (Python _workflow_start_memo): the digest is
        // what a future frozen-label check compares against.
        startOptions: { memo: identityMemo },
        // The logical-name keyword attribute (#495): one visibility query across
        // versioned workflow types. Must be REGISTERED in the namespace (as in Python).
        ...(spec.runtime.temporal.workflow_search_attribute !== undefined
          ? {
              keywordSearchAttributes: {
                [spec.runtime.temporal.workflow_search_attribute]: spec.workflow.name,
              },
            }
          : {}),
        // The subject->execution index (#715 slice 1): a fixed KEYWORD_LIST attribute
        // stamped whenever the execution has subjects, independent of the opt-in
        // logical-name attribute. Must be registered on the namespace as a KeywordList.
        ...(subjectIds.length > 0
          ? { keywordListSearchAttributes: { [SUBJECT_IDS_SEARCH_ATTRIBUTE]: subjectIds } }
          : {}),
      });
      // Workflow-level facts on the grouped PARENT trace (Python parity: the
      // workflow span carries the run's input/output + identity metadata).
      // Redacted + best-effort inside the writer; identity = the start memo.
      const runId = handle.firstExecutionRunId;
      await autoObserver?.recordWorkflowRun({
        runId,
        workflowId: runOptions.workflowId,
        input,
        metadata: { ...identityMemo, workflow_id: runOptions.workflowId },
        // #715 slice 1: the primary subject becomes the trace's native userId and
        // every subject a `typeflux.subject:{id}` tag (set inside the observer).
        ...(subjectIds.length > 0 ? { subjectIds } : {}),
      });
      try {
        const result = await handle.result();
        await autoObserver?.recordWorkflowRun({
          runId,
          workflowId: runOptions.workflowId,
          output: result,
          // Re-carried on every record (#715 review, finding 3): a Langfuse upsert
          // replaces the fields it carries, so the completion upsert must not omit
          // the subject identity the start recorded.
          ...(subjectIds.length > 0 ? { subjectIds } : {}),
        });
        return result;
      } catch (error) {
        await autoObserver?.recordWorkflowRun({
          runId,
          workflowId: runOptions.workflowId,
          error: error instanceof Error ? `${error.name}: ${error.message}` : String(error),
          ...(subjectIds.length > 0 ? { subjectIds } : {}),
        });
        throw error;
      }
    },
  };
}
