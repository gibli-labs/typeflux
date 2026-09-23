import { useNavigate } from "@tanstack/react-router";

import type { EnvironmentSummary } from "../api";
import { Badge, ErrorPanel, Loading, Section, SeverityBadge } from "../components";
import { diffBundles, summarizeDiff } from "../diff";
import { errorMessage, useBundle } from "../queries";

function renderValue(value: unknown): string {
  if (value === undefined) return "—";
  return typeof value === "string" ? value : JSON.stringify(value);
}

export function DiffPage({
  workflowId,
  environments,
  left,
  right,
}: {
  workflowId: string;
  environments: EnvironmentSummary[];
  left: string;
  right: string;
}) {
  const navigate = useNavigate();
  const leftState = useBundle(workflowId, left);
  const rightState = useBundle(workflowId, right);
  const loading = leftState.isPending || rightState.isPending;
  const error = errorMessage(leftState.error) ?? errorMessage(rightState.error);

  const pick = (side: "left" | "right", env: string) => {
    void navigate({
      to: "/workflows/$workflowId/diff",
      params: { workflowId },
      search: (previous) => ({
        ...previous,
        left: side === "left" ? env : left,
        right: side === "right" ? env : right,
      }),
    });
  };

  const selector = (
    <div className="row" style={{ marginBottom: 14 }}>
      <label className="field">
        left
        <select value={left} onChange={(event) => pick("left", event.target.value)}>
          {environments.map((environment) => (
            <option key={environment.id} value={environment.id}>
              {environment.id}
            </option>
          ))}
        </select>
      </label>
      <span className="faint">vs</span>
      <label className="field">
        right
        <select value={right} onChange={(event) => pick("right", event.target.value)}>
          {environments.map((environment) => (
            <option key={environment.id} value={environment.id}>
              {environment.id}
            </option>
          ))}
        </select>
      </label>
    </div>
  );

  if (loading) {
    return (
      <>
        {selector}
        <Loading what={`bundles for ${left} and ${right}`} />
      </>
    );
  }
  if (error || !leftState.data || !rightState.data) {
    return (
      <>
        {selector}
        <ErrorPanel message={error ?? "no data"} />
      </>
    );
  }

  const [leftBundle, rightBundle] = [leftState.data, rightState.data];
  const entries = diffBundles(leftBundle, rightBundle);
  const summary = summarizeDiff(entries);
  const sections = [...new Set(entries.map((entry) => entry.section))];

  return (
    <>
      {selector}
      <Section id="summary" title="Diff summary">
        <div className="panel row">
          {entries.length === 0 ? (
            <>
              <Badge kind="ok">identical</Badge>
              <span className="dim">
                The resolved bundles are identical between {left} and {right} (per-checkout paths
                excluded).
              </span>
            </>
          ) : (
            <>
              <span className="badge badge-critical">{summary.critical} critical</span>
              <span className="badge badge-warning">{summary.warning} warning</span>
              <span className="badge badge-info">{summary.info} info</span>
              <span className="dim">
                critical = version/governance drift · warning = behavior may differ · info =
                expected divergence
              </span>
            </>
          )}
        </div>
      </Section>
      {sections.map((section) => (
        <Section key={section} id={`diff-${section}`} title={section}>
          <div className="panel">
            <table className="grid">
              <thead>
                <tr>
                  <th style={{ width: 90 }}>Severity</th>
                  <th>Path</th>
                  <th>{left}</th>
                  <th>{right}</th>
                </tr>
              </thead>
              <tbody>
                {entries
                  .filter((entry) => entry.section === section)
                  .map((entry) => (
                    <tr key={entry.path}>
                      <td>
                        <SeverityBadge severity={entry.severity} />
                      </td>
                      <td>
                        <span className="diff-path">{entry.path}</span>
                      </td>
                      <td>
                        <span className="diff-cell left">{renderValue(entry.left)}</span>
                      </td>
                      <td>
                        <span className="diff-cell right">{renderValue(entry.right)}</span>
                      </td>
                    </tr>
                  ))}
              </tbody>
            </table>
          </div>
        </Section>
      ))}
    </>
  );
}
