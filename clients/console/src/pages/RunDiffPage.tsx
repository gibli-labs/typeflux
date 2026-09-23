import { useNavigate } from "@tanstack/react-router";

import type { ExecutionList } from "../api";
import { Badge, ErrorPanel, ExtLink, Loading, Mono, Section, TraceLink } from "../components";
import { temporalExecutionUrl, temporalNamespaceOf, traceLinkState } from "../links";
import { errorMessage, useBundle, useCorrelation, useExecutions } from "../queries";
import { diffTraceSummaries, type TraceSummary } from "../runDiff";

/**
 * Run-to-run manifest diff (#289): compare two executions of the same logical
 * workflow by their recorded manifest identity (spec/contract hash, code sha,
 * policy hash, models, prompt refs…), classified by severity. Contract/manifest
 * focused — never output-quality analytics. Reuses the per-execution trace
 * summary from `/correlation`.
 */
export function RunDiffPage({
  workflowId,
  env,
  left,
  right,
}: {
  workflowId: string;
  env: string;
  left: string | null;
  right: string | null;
}) {
  const navigate = useNavigate();
  const bundle = useBundle(workflowId, env);
  const executions = useExecutions(workflowId, env);

  const records = executions.data?.executions ?? [];
  // Default each side to the most recent execution (and a different one for the
  // other side) without navigating: the URL only changes on an explicit pick,
  // so there's no default-selection race and a half-specified URL still fills
  // the missing side.
  const effectiveLeft = left || records[0]?.execution_id || null;
  const effectiveRight =
    right || records.find((r) => r.execution_id !== effectiveLeft)?.execution_id || null;

  const leftCorr = useCorrelationState(workflowId, env, effectiveLeft);
  const rightCorr = useCorrelationState(workflowId, env, effectiveRight);

  function pick(side: "left" | "right", id: string): void {
    // Preserve the other side at its effective value so picking one never
    // drops the other.
    void navigate({
      to: "/workflows/$workflowId/run-diff",
      params: { workflowId },
      search: {
        env,
        left: side === "left" ? id : (effectiveLeft ?? ""),
        right: side === "right" ? id : (effectiveRight ?? ""),
      },
    });
  }

  if (executions.isError) return <ErrorPanel message={errorMessage(executions.error) ?? "no data"} />;
  if (executions.isPending) return <Loading what="executions" />;
  if (records.length < 2) {
    return (
      <Section id="run-diff" title="Run diff">
        <div className="panel">
          <span className="dim">
            Need at least two recorded executions of this logical workflow to compare.
          </span>
        </div>
      </Section>
    );
  }

  const links = bundle.data?.links;
  const namespace = temporalNamespaceOf(bundle.data);

  return (
    <Section id="run-diff" title="Run diff">
      <div className="panel">
        <div className="run-diff-pickers">
          <RunPicker
            label="A"
            value={effectiveLeft}
            records={records}
            onPick={(id) => pick("left", id)}
          />
          <RunPicker
            label="B"
            value={effectiveRight}
            records={records}
            onPick={(id) => pick("right", id)}
          />
        </div>
        <div className="run-diff-links">
          <RunLinks corr={leftCorr} links={links} namespace={namespace} side="A" />
          <RunLinks corr={rightCorr} links={links} namespace={namespace} side="B" />
        </div>
        <DiffBody left={leftCorr} right={rightCorr} records={records} />
      </div>
    </Section>
  );
}

function useCorrelationState(workflowId: string, env: string, id: string | null) {
  // The query key carries the full fetch identity (workflow + env + id), so a
  // stale response can never surface under a newer selection — the guard the
  // old hand-rolled hook implemented with a key tag.
  const query = useCorrelation(workflowId, env, id);
  return { id, corr: query.data ?? null, error: errorMessage(query.error) ?? null };
}

type CorrState = ReturnType<typeof useCorrelationState>;

type ExecutionRecord = ExecutionList["executions"][number];

function RunPicker({
  label,
  value,
  records,
  onPick,
}: {
  label: string;
  value: string | null;
  records: ExecutionRecord[];
  onPick: (id: string) => void;
}) {
  // A deep-linked / aged-out selection may not be in the recent-records window;
  // surface it as its own option so the select still reflects it.
  const ids = new Set(records.map((r) => r.execution_id));
  const stale = value && !ids.has(value) ? value : null;
  return (
    <label className="run-diff-picker">
      <span className="faint">{label}</span>
      <select value={value ?? ""} onChange={(e) => onPick(e.target.value)}>
        <option value="" disabled>
          select execution…
        </option>
        {stale ? <option value={stale}>{stale} (not in recent runs)</option> : null}
        {records.map((r) => (
          <option key={r.execution_id} value={r.execution_id}>
            {r.execution_id} — {r.workflow_type}
            {r.current_version ? "" : " (old version)"}
          </option>
        ))}
      </select>
    </label>
  );
}

function RunLinks({
  corr,
  links,
  namespace,
  side,
}: {
  corr: CorrState;
  links: { langfuse_project?: string | null; temporal_ui?: string | null } | null | undefined;
  namespace: string | null;
  side: string;
}) {
  if (!corr.id) return <span />;
  const trace = corr.corr?.trace as TraceSummary | undefined;
  const traceId = typeof trace?.["trace_id"] === "string" ? (trace["trace_id"] as string) : null;
  const runId =
    typeof trace?.["temporal_run_id"] === "string" ? (trace["temporal_run_id"] as string) : null;
  return (
    <span className="run-diff-side-links">
      <span className="faint">{side}</span> <Mono>{corr.id}</Mono>
      {links?.temporal_ui ? (
        <ExtLink href={temporalExecutionUrl(links.temporal_ui, namespace, corr.id, runId)}>
          Temporal
        </ExtLink>
      ) : null}
      <TraceLink
        state={traceLinkState({
          langfuseBase: links?.langfuse_project,
          // corr.corr carries the observer verdict; while correlation is still PENDING fall
          // back to a search link on the execution id (same as the run inspector) instead of a
          // premature "no trace yet" — once loaded, the verdict below is authoritative (Bugbot).
          observer: corr.corr?.observer,
          reachable: corr.corr?.reachable,
          traceId,
          searchTerm: corr.corr ? null : corr.id,
        })}
        label="Trace"
      />
    </span>
  );
}

function DiffBody({
  left,
  right,
  records,
}: {
  left: CorrState;
  right: CorrState;
  records: ExecutionRecord[];
}) {
  if (!left.id || !right.id) {
    return <div className="hint">Select two executions to compare.</div>;
  }
  if (left.id === right.id) {
    return <div className="hint">Select two different executions to compare.</div>;
  }
  if (left.error || right.error) {
    return <ErrorPanel message={left.error ?? right.error ?? "correlation failed"} />;
  }
  if (!left.corr || !right.corr) return <Loading what="run correlation" />;

  // Comparing across versioned workflow types is meaningful but expected to
  // differ on the identity hashes — surface that so version drift doesn't read
  // as a surprise.
  const leftType = records.find((r) => r.execution_id === left.id)?.workflow_type;
  const rightType = records.find((r) => r.execution_id === right.id)?.workflow_type;
  const crossVersion = leftType && rightType && leftType !== rightType;

  const observerMissing = left.corr.observer === "none" || right.corr.observer === "none";
  const traceMissing = !left.corr.trace || !right.corr.trace;
  if (observerMissing || traceMissing) {
    return (
      <div className="hint">
        {observerMissing
          ? "This workflow's observer is 'none', so no per-run manifest was recorded to diff. Configure an observer (e.g. Langfuse) to compare runs."
          : "A trace is missing for one of these executions — the manifest diff needs both. Check the run's observer connection."}
      </div>
    );
  }

  const rows = diffTraceSummaries(
    left.corr.trace as TraceSummary,
    right.corr.trace as TraceSummary,
  );
  if (rows.length === 0) {
    return (
      <div className="row">
        <Badge kind="ok">identical</Badge>
        <span className="dim">The two runs share the same manifest identity.</span>
      </div>
    );
  }

  return (
    <>
      {crossVersion ? (
        <div className="hint">
          These runs executed different versioned workflow types — identity
          differences below are expected version drift, not a same-version
          regression.
        </div>
      ) : null}
      <table className="grid">
        <thead>
        <tr>
          <th>Field</th>
          <th>Severity</th>
          <th>A</th>
          <th>B</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((rowItem) => (
          <tr key={rowItem.field}>
            <td>{rowItem.label}</td>
            <td>
              <Badge kind={rowItem.severity}>{rowItem.severity}</Badge>
            </td>
            <td>
              <Mono>{rowItem.left}</Mono>
            </td>
            <td>
              <Mono>{rowItem.right}</Mono>
            </td>
          </tr>
        ))}
        </tbody>
      </table>
    </>
  );
}
