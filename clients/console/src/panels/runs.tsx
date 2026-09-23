/**
 * Runs panels (#580): the operational sections of Runs & operations as
 * independent components. `RunInspector` is the reusable unit — "show me this
 * execution" — owning its own status/correlation queries and review/cancel
 * state so a future incidents/Drift surface can embed it for any execution id.
 * Panels never import from pages/.
 */

import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";

import type { Bundle, Capabilities, StartReceipt, TaskQueueWorkers } from "../api";
import { fetchWorkers, postRepin, requestCancel, startWorkflow, submitReview } from "../api";
import { createStaleGuard } from "../staleGuard";
import {
  Badge,
  CopyBlock,
  ErrorPanel,
  ExtLink,
  InsightList,
  JsonView,
  KV,
  Loading,
  Mono,
  Section,
  ShortDigest,
  TraceLink,
} from "../components";
import {
  langfuseTraceUrl,
  temporalExecutionUrl,
  temporalNamespaceOf,
  traceLinkState,
} from "../links";
import {
  accessDeniedHint,
  canSubmitReview,
  generateExecutionId,
  isTerminal,
  parseWorkflowInput,
  reviewerAttribution,
  traceQuerySnippet,
} from "../ops";
import {
  errorMessage,
  executionsKey,
  useCorrelation,
  useExecutions,
  useOperationStatus,
} from "../queries";
import { derivePinSkew } from "../runsFeed";
import { assemble, planFields, validate } from "../schemaForm";
import { SchemaForm } from "../SchemaFormFields";
import { TopologyView } from "../TopologyView";

/**
 * Start form + start receipt. Owns the whole start interaction (input form,
 * worker probe, receipt with trace lookup); reports a started execution up
 * via `onInspect` so the page can make it the addressed run.
 */
export function StartExecutionPanel({
  workflowId,
  env,
  bundle,
  capabilities,
  onInspect,
}: {
  workflowId: string;
  env: string;
  bundle: Bundle | undefined;
  capabilities: Capabilities;
  onInspect: (executionId: string) => void;
}) {
  const queryClient = useQueryClient();
  const [startId, setStartId] = useState("");
  const [inputJson, setInputJson] = useState("");
  const [formState, setFormState] = useState<Record<string, string>>({});
  const [rawMode, setRawMode] = useState(false);
  const [taskQueue, setTaskQueue] = useState("");
  const [startBusy, setStartBusy] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);
  const [receipt, setReceipt] = useState<StartReceipt | null>(null);
  const [workers, setWorkers] = useState<TaskQueueWorkers | "loading" | null>(null);
  // Manual fetch outside TanStack Query, so it needs its own out-of-order discard (#786):
  // without the guard, a response for the PREVIOUS request identity that resolves after the
  // selection changes overwrites the reset below, mislabeled as the current identity's result.
  // The guard instance MUST stay reference-stable across renders (it is in the effect deps;
  // a per-render instance would silently invalidate every in-flight check).
  const workersGuardRef = useRef<ReturnType<typeof createStaleGuard> | null>(null);
  workersGuardRef.current ??= createStaleGuard();
  const workersGuard = workersGuardRef.current;
  useEffect(() => {
    workersGuard.invalidate();
    setWorkers(null);
    // Unmount also drops any in-flight response.
    return () => workersGuard.invalidate();
    // taskQueue is part of the request identity (fetchWorkers sends it): editing the queue
    // field resets the badge so a result for queue A is never read as a verdict on queue B.
  }, [workflowId, env, taskQueue, workersGuard]);

  const identity = bundle?.workflow;
  const links = bundle?.links;
  const namespace = temporalNamespaceOf(bundle);
  const inputSchema =
    (bundle?.workflow.input_schema?.["json_schema"] as Record<string, unknown> | undefined) ??
    null;
  const inputPlan = planFields(inputSchema);

  const checkWorkers = () => {
    const isCurrent = workersGuard.next();
    setWorkers("loading");
    fetchWorkers(workflowId, env, taskQueue.trim() || undefined).then(
      (result) => {
        if (isCurrent()) setWorkers(result);
      },
      () => {
        if (isCurrent()) setWorkers(null);
      },
    );
  };

  const handleStart = async () => {
    let input: Record<string, unknown>;
    if (inputPlan.structured && !rawMode) {
      input = assemble(inputPlan, formState);
      const errors = inputSchema ? validate(inputSchema, input) : [];
      if (errors.length > 0) {
        setStartError(errors.join("; "));
        return;
      }
    } else {
      const parsed = parseWorkflowInput(inputJson);
      if (!parsed.ok) {
        setStartError(parsed.error);
        return;
      }
      input = parsed.value;
    }
    const id = startId.trim();
    if (!id) {
      setStartError("execution id is required");
      return;
    }
    setStartBusy(true);
    setStartError(null);
    // Capture the worker-check identity at start entry (#786, Bugbot): the trailing
    // auto-refresh below runs from THIS render's closure, so it must be skipped when the
    // workflow/env/queue changed — or a newer manual check ran — while the start awaited.
    const workersIdentityUnchanged = workersGuard.observe();
    try {
      const next = await startWorkflow(workflowId, {
        environment_id: env,
        execution_id: id,
        input,
        task_queue: taskQueue.trim() || null,
        policy_ids: [],
      });
      setReceipt(next);
      onInspect(id);
      // The new run should appear in the executions list without a reload.
      void queryClient.invalidateQueries({ queryKey: executionsKey(workflowId, env) });
      if (workersIdentityUnchanged()) checkWorkers();
    } catch (caught) {
      setStartError(errorMessage(caught) ?? "start failed");
    } finally {
      setStartBusy(false);
    }
  };

  return (
    <>
      <Section id="start" title="Start execution">
        <div className="panel">
          {identity ? (
            <div className="row" style={{ marginBottom: 10 }}>
              <span className="dim">registers as</span>
              <Mono>{identity.workflow_type}</Mono>
              <span className="dim">· queue</span>
              <Mono>{identity.task_queue}</Mono>
              {workers === null ? (
                <button type="button" onClick={checkWorkers}>
                  Check workers
                </button>
              ) : workers === "loading" ? (
                <span className="faint">checking…</span>
              ) : !workers.reachable ? (
                <span className="badge badge-neutral" title={workers.detail ?? ""}>
                  temporal unreachable
                </span>
              ) : workers.workers_polling > 0 ? (
                <span className="badge badge-ok">{workers.workers_polling} workers polling</span>
              ) : (
                <Badge kind="warning">no workers polling</Badge>
              )}
            </div>
          ) : null}
          <form
            onSubmit={(event) => {
              event.preventDefault();
              void handleStart();
            }}
          >
            <div className="row" style={{ marginBottom: 8 }}>
              <label className="field">
                execution id
                <input
                  type="text"
                  value={startId}
                  onChange={(event) => setStartId(event.target.value)}
                  placeholder="e.g. claim-2026-0612-001"
                  size={32}
                />
              </label>
              <button
                type="button"
                onClick={() => setStartId(generateExecutionId(workflowId, Date.now()))}
              >
                Generate
              </button>
              <label className="field">
                task queue (optional)
                <input
                  type="text"
                  value={taskQueue}
                  onChange={(event) => setTaskQueue(event.target.value)}
                  size={24}
                />
              </label>
            </div>
            {inputPlan.structured ? (
              <div className="row" style={{ marginBottom: 6 }}>
                <label className="field">
                  <input
                    type="checkbox"
                    checked={rawMode}
                    onChange={(event) => setRawMode(event.target.checked)}
                  />
                  edit as raw JSON
                </label>
              </div>
            ) : null}
            {inputPlan.structured && !rawMode ? (
              <SchemaForm
                plan={inputPlan}
                state={formState}
                onChange={(key: string, value: string) =>
                  setFormState((previous) => ({ ...previous, [key]: value }))
                }
              />
            ) : (
              <textarea
                className="json-input"
                value={inputJson}
                onChange={(event) => setInputJson(event.target.value)}
                placeholder={'workflow input as a JSON object, e.g. {"case_id": "c-1"}'}
                rows={5}
                spellCheck={false}
              />
            )}
            <div className="row" style={{ marginTop: 8 }}>
              <button type="submit" disabled={startBusy || !capabilities.can_start}>
                {startBusy ? "Starting…" : "Start workflow"}
              </button>
              {capabilities.can_start ? null : (
                <span className="dim">{accessDeniedHint("starting executions")}</span>
              )}
              {startError ? <span className="op-error mono">{startError}</span> : null}
            </div>
          </form>
        </div>
      </Section>

      {receipt ? (
        <Section id="receipt" title="Start receipt">
          <div className="panel">
            <KV
              rows={[
                ["Execution", <Mono key="e">{receipt.workflow_id}</Mono>],
                ["Run id", <Mono key="r">{receipt.run_id ?? "—"}</Mono>],
                ["Versioned type", <Mono key="t">{receipt.workflow_type}</Mono>],
                ["Spec digest", <ShortDigest key="d" value={receipt.spec_digest} />],
                ["Task queue", <Mono key="q">{receipt.task_queue}</Mono>],
              ]}
            />
            <div className="row" style={{ margin: "8px 0" }}>
              {links?.temporal_ui ? (
                <ExtLink
                  href={temporalExecutionUrl(
                    links.temporal_ui,
                    namespace,
                    receipt.workflow_id,
                    receipt.run_id,
                  )}
                >
                  Open in Temporal
                </ExtLink>
              ) : null}
              {/* searchTerm-only by design: a StartReceipt carries no observer/reachable and no
                  trace exists yet at start, so a workflow trace-SEARCH link is the best available
                  hand-off (no per-run correlation fetch on this surface). */}
              <TraceLink
                state={traceLinkState({
                  langfuseBase: links?.langfuse_project,
                  searchTerm: receipt.workflow_id,
                })}
                label="Langfuse trace"
              />
            </div>
            {workers !== null && workers !== "loading" && workers.reachable && workers.workers_polling === 0 ? (
              <div className="error-panel" style={{ marginBottom: 8 }}>
                Submitted to <span className="mono">{workers.task_queue}</span>, but{" "}
                <strong>no workers are polling it</strong> — the run will not progress until a
                worker starts on that task queue.
              </div>
            ) : null}
            <div className="hint" style={{ marginBottom: 6 }}>
              Trace lookup for this execution:
            </div>
            <CopyBlock command={traceQuerySnippet(receipt.trace_query_hint)} />
          </div>
        </Section>
      ) : null}
    </>
  );
}

/** Recent executions of the logical workflow; a row click addresses the run. */
export function ExecutionsPanel({
  workflowId,
  env,
  bundle,
  onInspect,
}: {
  workflowId: string;
  env: string;
  bundle: Bundle | undefined;
  onInspect: (executionId: string) => void;
}) {
  const executionsState = useExecutions(workflowId, env);
  const links = bundle?.links;
  const namespace = temporalNamespaceOf(bundle);

  return (
    <Section id="executions" title="Executions">
      <div className="panel">
        {(executionsState.data?.executions ?? []).length >= 2 ? (
          <div className="page-actions">
            <Link
              className="crumb"
              to="/workflows/$workflowId/run-diff"
              params={{ workflowId }}
              search={{ env }}
            >
              Compare runs →
            </Link>
          </div>
        ) : null}
        {executionsState.isPending ? (
          <Loading what="executions" />
        ) : executionsState.isError ? (
          <span className="dim">
            Executions unavailable: {errorMessage(executionsState.error)}
          </span>
        ) : (executionsState.data?.executions ?? []).length === 0 ? (
          <span className="dim">No executions recorded for this logical workflow.</span>
        ) : (
          <table className="grid">
            <thead>
              <tr>
                <th>Execution</th>
                <th>Versioned type</th>
                <th>Status</th>
                <th>Started</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {(executionsState.data?.executions ?? []).map((record) => (
                <tr
                  key={`${record.execution_id}:${record.run_id ?? ""}`}
                  className="clickable"
                  onClick={() => onInspect(record.execution_id)}
                >
                  <td>
                    <Mono>{record.execution_id}</Mono>
                  </td>
                  <td>
                    <Mono>{record.workflow_type}</Mono>{" "}
                    {record.current_version ? null : <Badge kind="warning">old version</Badge>}
                  </td>
                  <td>
                    <Mono>{record.status}</Mono>
                  </td>
                  <td>
                    <Mono>{record.start_time ?? "—"}</Mono>
                  </td>
                  <td onClick={(event) => event.stopPropagation()}>
                    {links?.temporal_ui ? (
                      <ExtLink
                        href={temporalExecutionUrl(
                          links.temporal_ui,
                          namespace,
                          record.execution_id,
                          record.run_id,
                        )}
                      >
                        Temporal
                      </ExtLink>
                    ) : null}{" "}
                    {/* searchTerm-only by design: execution records carry no observer/reachable,
                        and a precise per-row correlation fetch would be N requests for the table —
                        a trace-SEARCH link is the best available hand-off here. */}
                    <TraceLink
                      state={traceLinkState({
                        langfuseBase: links?.langfuse_project,
                        searchTerm: record.execution_id,
                      })}
                      label="traces"
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </Section>
  );
}

/** Free-form execution-id lookup; submitting addresses the run. */
export function RunLookupPanel({
  addressedRun,
  syncSignal,
  onInspect,
}: {
  addressedRun: string | null;
  /** Bumped on same-run re-inspection so the input re-syncs even then. */
  syncSignal: number;
  onInspect: (executionId: string) => void;
}) {
  const [executionId, setExecutionId] = useState(addressedRun ?? "");
  // Addressing a run elsewhere (row click, start, deep link) reflects here —
  // including re-addressing the same run, which changes only the signal.
  useEffect(() => {
    if (addressedRun) setExecutionId(addressedRun);
  }, [addressedRun, syncSignal]);

  return (
    <Section id="lookup" title="Run lookup">
      <div className="panel">
        <form
          className="row"
          onSubmit={(event) => {
            event.preventDefault();
            const trimmed = executionId.trim();
            if (trimmed) onInspect(trimmed);
          }}
        >
          <label className="field">
            execution id
            <input
              type="text"
              value={executionId}
              onChange={(event) => setExecutionId(event.target.value)}
              placeholder="e.g. claim-2026-0612-001"
              size={36}
            />
          </label>
          <button type="submit" disabled={!executionId.trim()}>
            Inspect
          </button>
        </form>
        <div className="hint">
          Untraced status query — inspection does not write to the audit trail. The inspected
          run lives in the URL (<span className="mono">?run=…</span>), so the view is shareable.
        </div>
      </div>
    </Section>
  );
}

/**
 * Everything about one execution: live topology position, lifecycle status,
 * manifest correlation, version-valid review decisions, cancellation. Owns
 * its status/correlation queries and operation state; remount (key change)
 * is a fresh fetch since status queries retain nothing (gcTime 0).
 */
export function RunInspector({
  workflowId,
  env,
  executionId,
  refreshSignal = 0,
  bundle,
  bundlePending = false,
  capabilities,
  callerIdentity = null,
}: {
  workflowId: string;
  env: string;
  executionId: string;
  /** Bumped by the page to request a manual refetch without a remount. */
  refreshSignal?: number;
  bundle: Bundle | undefined;
  bundlePending?: boolean;
  capabilities: Capabilities;
  /** The trusted proxy-auth principal (`/meta.caller_identity`, #577 §5). When present it IS the
   * reviewer on submitted decisions — the free-text field renders as a read-only attribution. */
  callerIdentity?: string | null;
}) {
  const [polling, setPolling] = useState(false);
  const [reviewer, setReviewer] = useState("");
  const [notes, setNotes] = useState("");
  const [reason, setReason] = useState("");
  const [opBusy, setOpBusy] = useState<string | null>(null);
  const [opError, setOpError] = useState<string | null>(null);

  const statusQuery = useOperationStatus(workflowId, env, executionId, polling);
  const correlationQuery = useCorrelation(workflowId, env, executionId);
  const correlation = correlationQuery.data ?? null;
  // On error the snapshot is cleared (never render a stale run state) and
  // polling stops — the same fail-quiet behavior as before.
  const status = statusQuery.isError ? null : (statusQuery.data ?? null);
  const error = errorMessage(statusQuery.error) ?? null;
  const loading = statusQuery.isFetching;
  useEffect(() => {
    if (statusQuery.isError) setPolling(false);
  }, [statusQuery.isError]);

  const refreshStatus = () => statusQuery.refetch();
  // A bumped signal is the page saying "the operator re-inspected this run":
  // refetch in place, keeping reviewer/notes/polling state. Only bumps
  // *after* mount count — the page's counter is not per-run, so a fresh
  // inspector (new run, new key) must ignore whatever value it mounts with;
  // its queries are already fetching.
  const mountSignal = useRef(refreshSignal);
  const refetchStatus = statusQuery.refetch;
  const refetchCorrelation = correlationQuery.refetch;
  useEffect(() => {
    if (refreshSignal > mountSignal.current) {
      mountSignal.current = refreshSignal;
      void refetchStatus();
      void refetchCorrelation();
    }
  }, [refreshSignal, refetchStatus, refetchCorrelation]);

  const handleReview = async (decision: string) => {
    setOpBusy(`review:${decision}`);
    setOpError(null);
    try {
      await submitReview(workflowId, {
        environment_id: env,
        execution_id: executionId,
        policy_ids: [],
        command: {
          user_decision: decision,
          reviewer: reviewerAttribution(callerIdentity, reviewer).value,
          notes: notes.trim() || null,
        },
      });
      // Stay busy until the refreshed snapshot arrives — closes the
      // double-submit window.
      await refreshStatus();
    } catch (caught) {
      setOpError(errorMessage(caught) ?? "operation failed");
    } finally {
      setOpBusy(null);
    }
  };

  const handleCancel = async () => {
    if (!window.confirm(`Request cancellation of ${executionId}?`)) return;
    setOpBusy("cancel");
    setOpError(null);
    try {
      await requestCancel(workflowId, {
        environment_id: env,
        execution_id: executionId,
        reason: reason.trim() || null,
        policy_ids: [],
      });
      await refreshStatus();
    } catch (caught) {
      setOpError(errorMessage(caught) ?? "operation failed");
    } finally {
      setOpBusy(null);
    }
  };

  const [repinBusy, setRepinBusy] = useState(false);
  const [repinError, setRepinError] = useState<string | null>(null);
  const pinSkew = derivePinSkew(
    workflowId,
    env,
    status?.runtime_pin,
    bundle?.workflow.spec_digest,
  );
  const handleRepin = async () => {
    setRepinBusy(true);
    setRepinError(null);
    try {
      await postRepin(workflowId, env);
      // The next status read re-pins against the current YAML; refresh so the
      // banner reflects the fresh pin.
      await statusQuery.refetch();
    } catch (caught) {
      setRepinError(errorMessage(caught) ?? "repin failed");
    } finally {
      setRepinBusy(false);
    }
  };

  const lifecycleStatus = status?.status as Record<string, unknown> | undefined;
  const currentStep = (lifecycleStatus?.["current_step"] as string | undefined) ?? null;
  const lifecycleState = (lifecycleStatus?.["state"] as string | undefined) ?? null;
  const reviewOpen = canSubmitReview(lifecycleState);
  const terminal = isTerminal(lifecycleState);
  const links = bundle?.links;
  const namespace = temporalNamespaceOf(bundle);

  return (
    <>
      <div className="panel row" style={{ marginBottom: 16 }}>
        <span className="dim">inspecting</span>
        <Mono>{executionId}</Mono>
        <label className="field">
          <input
            type="checkbox"
            checked={polling}
            onChange={(event) => setPolling(event.target.checked)}
          />
          auto-refresh
          {status ? (
            <span className="faint">(every {status.recommended_poll_interval_seconds}s)</span>
          ) : null}
        </label>
      </div>

      {status?.runtime_pin ? (
        <div className="panel" style={{ marginBottom: 16 }}>
          {pinSkew ? <InsightList insights={[pinSkew]} /> : null}
          <div className="row" style={{ marginTop: pinSkew ? 6 : 0 }}>
            <span className="dim">runtime pin</span>
            <Mono>
              {status.runtime_pin.spec_digest?.slice(0, 12) ?? "?"} · {status.runtime_pin.pinned_at ?? "?"}
            </Mono>
            <button
              type="button"
              disabled={repinBusy || !capabilities.can_refresh_project}
              onClick={() => void handleRepin()}
            >
              {repinBusy ? "Repinning…" : "Repin (drop the pinned runtime)"}
            </button>
            {capabilities.can_refresh_project ? null : (
              <span className="dim">{accessDeniedHint("repinning the runtime")}</span>
            )}
            {repinError ? <span className="op-error mono">{repinError}</span> : null}
          </div>
          <div className="hint">
            Mutating operations reuse the runtime pinned at first use. The digest covers the
            workflow graph only — runtime-config or policy edits don&apos;t move it — so a matching
            digest is not a freshness verdict; repin to make the next mutating call re-resolve
            the current YAML (fail-closed on policy drift, as always).
          </div>
        </div>
      ) : null}

      {error ? (
        <ErrorPanel
          message={error}
          hint="Status queries reach Temporal through the control-plane server; check the execution id and that Temporal is reachable."
        />
      ) : null}
      {opError ? <ErrorPanel message={opError} /> : null}

      {loading && !status ? <Loading what="run status" /> : null}

      {status && lifecycleStatus ? (
        <>
          <Section id="live-topology" title="Live position">
            <div className="panel">
              {bundle ? (
                <TopologyView
                  topology={bundle.topology ?? { nodes: [], edges: [] }}
                  activeStep={currentStep}
                  env={env}
                />
              ) : bundlePending ? (
                <Loading what="topology" />
              ) : (
                <span className="dim">Topology unavailable.</span>
              )}
            </div>
          </Section>

          <Section id="status" title="Lifecycle status">
            <div className="panel">
              <KV
                rows={[
                  ["Execution", <Mono key="e">{status.workflow_id}</Mono>],
                  ["State", <Mono key="s">{String(lifecycleStatus["state"] ?? "?")}</Mono>],
                  ["Current step", currentStep ? <Mono key="c">{currentStep}</Mono> : <span className="faint">—</span>],
                  [
                    "Progress",
                    `${String(lifecycleStatus["completed_units"] ?? 0)} / ${String(lifecycleStatus["total_units"] ?? 0)} units`,
                  ],
                  [
                    "Cancellation requested",
                    lifecycleStatus["cancellation_requested"] ? "yes" : "no",
                  ],
                ]}
              />
              <div className="row" style={{ marginTop: 8 }}>
                {links?.temporal_ui ? (
                  <ExtLink
                    href={temporalExecutionUrl(
                      links.temporal_ui,
                      namespace,
                      executionId,
                      status.run_id,
                    )}
                  >
                    Open in Temporal
                  </ExtLink>
                ) : null}
                <TraceLink
                  state={traceLinkState({
                    langfuseBase: links?.langfuse_project,
                    observer: correlation?.observer,
                    reachable: correlation?.reachable,
                    traceId:
                      typeof correlation?.trace?.["trace_id"] === "string"
                        ? (correlation.trace["trace_id"] as string)
                        : null,
                    // Fall back to a workflow SEARCH link only while correlation is still loading;
                    // once loaded, its verdict (none/unreachable/precise trace-id link/no-trace) is
                    // authoritative and AGREES with the manifest-correlation panel below for the
                    // same run — a specific trace id is preferred over the search fallback.
                    searchTerm: correlation ? null : executionId,
                  })}
                  label="Langfuse trace"
                />
              </div>
              <JsonView label="raw status" value={status.status} />
            </div>
          </Section>

          <Section id="manifest" title="Manifest correlation">
            <div className="panel">
              {correlation === null ? (
                // Pending is a data-loading state, not a trace verdict — kept here rather than
                // invented as a TraceLinkState kind.
                <span className="dim">Correlation pending…</span>
              ) : !(correlation.observer === "langfuse" && correlation.reachable && correlation.trace) ? (
                // Every non-link degradation (none / unreachable / no-trace) renders through the
                // ONE TraceLink component so the copy has a single source of truth (#718 §A). No
                // traceId/searchTerm is passed: this branch is only reached when there is no usable
                // trace, so the verdict must be the honest degradation, never a search link.
                <TraceLink
                  state={traceLinkState({
                    langfuseBase: links?.langfuse_project,
                    observer: correlation.observer,
                    reachable: correlation.reachable,
                  })}
                  variant="descriptive"
                />
              ) : (
                <>
                  <KV
                    rows={[
                      ["Trace", <Mono key="t">{String(correlation.trace["trace_id"])}</Mono>],
                      ["Manifest hash", <Mono key="m">{String(correlation.trace["manifest_hash"] ?? "—")}</Mono>],
                      ["Git sha", <Mono key="g">{String(correlation.trace["git_sha"] ?? "—")}</Mono>],
                      ["Prompts", <Mono key="p">{(correlation.trace["prompt_refs"] as string[] | undefined)?.join(", ") ?? "—"}</Mono>],
                      ["Models", <Mono key="o">{(correlation.trace["provider_models"] as string[] | undefined)?.join(", ") ?? "—"}</Mono>],
                    ]}
                  />
                  {links?.langfuse_project ? (
                    <div className="row" style={{ marginTop: 8 }}>
                      <ExtLink
                        href={langfuseTraceUrl(
                          links.langfuse_project,
                          String(correlation.trace["trace_id"]),
                        )}
                      >
                        Open trace in Langfuse
                      </ExtLink>
                    </div>
                  ) : null}
                  <JsonView label="trace summary" value={correlation.trace} />
                </>
              )}
            </div>
          </Section>

          <Section id="decisions" title="Review decisions (this version)">
            <div className="panel">
              {Object.keys(status.valid_user_decisions).length > 0 ? (
                <>
                  <div className="row" style={{ marginBottom: 8 }}>
                    {reviewerAttribution(callerIdentity, reviewer).fromIdentity ? (
                      // The control plane vouches for this principal (trusted proxy auth) — review
                      // decisions are attributed to it, not to free text (#577 §5).
                      <span className="field">
                        reviewer
                        <span>
                          <Mono>{callerIdentity}</Mono>{" "}
                          <span className="dim">(from your authenticated identity)</span>
                        </span>
                      </span>
                    ) : (
                      <label className="field">
                        reviewer (optional)
                        <input
                          type="text"
                          value={reviewer}
                          onChange={(event) => setReviewer(event.target.value)}
                          size={22}
                        />
                      </label>
                    )}
                    <label className="field">
                      notes (optional)
                      <input
                        type="text"
                        value={notes}
                        onChange={(event) => setNotes(event.target.value)}
                        size={32}
                      />
                    </label>
                  </div>
                  <div className="row">
                    {Object.entries(status.valid_user_decisions).map(([decision, target]) => (
                      <button
                        key={decision}
                        type="button"
                        disabled={opBusy !== null || !reviewOpen || !capabilities.can_review}
                        title={`routes to ${target}`}
                        onClick={() => void handleReview(decision)}
                      >
                        {opBusy === `review:${decision}` ? "Submitting…" : `${decision} → ${target}`}
                      </button>
                    ))}
                  </div>
                  <div className="hint">
                    {capabilities.can_review
                      ? null
                      : `${accessDeniedHint("submitting review decisions")} `}
                    {reviewOpen
                      ? null
                      : `Decisions apply while the run is waiting for review (current state: ${lifecycleState ?? "unknown"}). `}
                    Only the decisions valid for this execution's resolved version are offered.
                    Reviewer and notes are sent with the review signal and persist in Temporal
                    workflow history (they are kept out of `typeflux.*` metadata and trace links,
                    but are not private — avoid sensitive free text).
                  </div>
                </>
              ) : (
                <span className="dim">No review gate on this workflow version.</span>
              )}
            </div>
          </Section>

          <Section id="cancel" title="Cancellation">
            <div className="panel">
              <div className="row">
                <label className="field">
                  reason (optional)
                  <input
                    type="text"
                    value={reason}
                    onChange={(event) => setReason(event.target.value)}
                    size={36}
                  />
                </label>
                <button
                  type="button"
                  className="danger"
                  disabled={opBusy !== null || terminal || !capabilities.can_cancel}
                  onClick={() => void handleCancel()}
                >
                  {opBusy === "cancel" ? "Requesting…" : "Request cancel"}
                </button>
              </div>
              <div className="hint">
                {capabilities.can_cancel
                  ? null
                  : `${accessDeniedHint("requesting cancellation")} `}
                Cooperative cancellation through the lifecycle signal; the reason persists in
                Temporal workflow history and is returned to anyone who can read run status.
              </div>
            </div>
          </Section>
        </>
      ) : null}
    </>
  );
}
