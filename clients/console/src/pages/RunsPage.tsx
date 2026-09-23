import { Link } from "@tanstack/react-router";

import type { WorkflowSummary } from "../api";
import { Badge, Loading, Mono, Section } from "../components";
import { statusSeverity, triageOrder, type TriageRow } from "../runsFeed";
import { useExecutionsMatrix } from "../queries";

/**
 * The cross-workflow Runs surface (#589): every workflow's recent executions
 * in one failure-first table — "what is failing right now" without opening
 * each workflow. Rows address runs via the shareable `?run=` URLs (#582);
 * the per-run drill-down stays with the run inspector.
 */
// Deliberately a sibling of panels/runs.tsx's ExecutionsPanel, not a merge:
// the per-workflow panel drills down in place (row click inspects) while this
// cross-workflow table navigates (rows are ?run= links) and adds the
// workflow column + severity badge. Unify only if the UX difference goes.
export function RunsPage({ env, workflows }: { env: string; workflows: WorkflowSummary[] }) {
  const workflowIds = workflows.map((workflow) => workflow.id);
  const cells = useExecutionsMatrix(workflowIds, env);
  // Render progressively: each workflow's row set appears as its query
  // settles — a nine-way fan-out against a slow/dead Temporal must not hold
  // the whole surface behind the slowest workflow.
  const pendingCells = cells.filter((cell) => cell.pending);
  const settled = pendingCells.length === 0;

  const rows: TriageRow[] = triageOrder(
    cells.flatMap((cell) =>
      cell.executions.map((record) => ({ workflowId: cell.workflowId, record })),
    ),
  );
  const unavailable = cells.filter((cell) => cell.error);

  return (
    <Section id="runs" title={`Recent executions · ${env}`}>
      <div className="panel">
        {!settled ? (
          <div className="row" style={{ marginBottom: 8 }}>
            <Loading
              what={`executions for ${pendingCells.length} of ${workflowIds.length} workflows`}
            />
          </div>
        ) : null}
        {rows.length === 0 && !settled ? null : rows.length === 0 ? (
          <div className="row">
            {/* An empty list is only good news when the workflows were
                actually readable — never show a green all-clear over an
                outage. */}
            {unavailable.length === 0 ? (
              <>
                <Badge kind="ok">no recent executions</Badge>
                <span className="dim">Nothing has run recently in this environment.</span>
              </>
            ) : unavailable.length === cells.length ? (
              <>
                <span className="badge badge-neutral">no executions readable</span>
                <span className="dim">
                  Every workflow's executions were unreadable — is Temporal reachable in this
                  environment?
                </span>
              </>
            ) : (
              <>
                <span className="badge badge-neutral">no recent executions readable</span>
                <span className="dim">
                  Nothing recent among the readable workflows; see the unavailable ones below.
                </span>
              </>
            )}
          </div>
        ) : (
          <table className="grid">
            <thead>
              <tr>
                <th>Status</th>
                <th>Workflow</th>
                <th>Execution</th>
                <th>Versioned type</th>
                <th>Started</th>
              </tr>
            </thead>
            <tbody>
              {rows.map(({ workflowId, record }) => (
                <tr key={`${workflowId}:${record.execution_id}:${record.run_id ?? ""}`}>
                  <td>
                    <Badge kind={statusSeverity(record.status)}>{record.status}</Badge>
                  </td>
                  <td>
                    <Mono>{workflowId}</Mono>
                  </td>
                  <td>
                    <Link
                      to="/workflows/$workflowId/runs"
                      params={{ workflowId }}
                      search={{ env, run: record.execution_id }}
                    >
                      <Mono>{record.execution_id}</Mono>
                    </Link>
                  </td>
                  <td>
                    <Mono>{record.workflow_type}</Mono>{" "}
                    {record.current_version ? null : <Badge kind="warning">old version</Badge>}
                  </td>
                  <td>
                    <Mono>{record.start_time ?? "—"}</Mono>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {unavailable.length > 0 ? (
          <div className="hint">
            Executions unavailable:{" "}
            {unavailable.map((cell) => cell.workflowId).join(" · ")} — check that Temporal is
            reachable in this environment (each returns a bounded error, never a stall).
          </div>
        ) : null}
        <div className="hint">
          Failure-first: failed / terminated / timed-out / canceled executions rank above running,
          unknown statuses above completed. Click an execution to open its addressable run view.
        </div>
      </div>
    </Section>
  );
}
