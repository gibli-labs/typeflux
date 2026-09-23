/**
 * Environment overview (#605, #577 §8): the environment-centric slice of the Overview —
 * every workflow resolved in THIS environment with its admission/policy/digest/insight
 * state, the per-workflow profile selections and overrides as structured tables, then the
 * read-only definition detail (source, variables, env files, used-by).
 */

import { useMemo } from "react";

import type { EnvironmentDefinition, ProjectSummary, WorkflowSummary } from "../api";
import { Badge, ErrorPanel, JsonView, KV, Loading, Mono, Section, SeverityBadge, ShortDigest, SourceLinks } from "../components";
import { definitionSourceLinks } from "../links";
import {
  deriveEnvironmentRows,
  deriveProfileSelectionRows,
  flattenOverrides,
  rowsRefreshing,
  rowsSettled,
  type EnvironmentWorkflowRow,
} from "../environmentRows";
import { cellFingerprint, errorMessage, useBundleMatrix, useDefinition } from "../queries";

function StateBadge({ row }: { row: EnvironmentWorkflowRow }) {
  if (row.state === "pending") return <Badge kind="neutral">resolving…</Badge>;
  if (row.state === "unresolvable") return <Badge kind="warning">unresolvable</Badge>;
  if (row.state === "failing") return <Badge kind="critical">failing</Badge>;
  return <Badge kind="ok">ok</Badge>;
}

export function EnvironmentPage({
  environmentId,
  workflows,
  project,
  sourcePath,
}: {
  environmentId: string;
  workflows: WorkflowSummary[];
  project?: ProjectSummary;
  /** The environment's MANIFEST-RELATIVE path (from the shell listing) — the detail DTO's
   * profile_path is the resolved absolute path, which can never build a repo link (codex). */
  sourcePath?: string;
}) {
  const state = useDefinition("environment", environmentId);
  const cells = useBundleMatrix(
    workflows.map((workflow) => workflow.id),
    [environmentId],
  );
  const fingerprint = cellFingerprint(cells);
  // eslint-disable-next-line react-hooks/exhaustive-deps -- fingerprint stands in for cells
  const rows = useMemo(() => deriveEnvironmentRows(environmentId, cells), [fingerprint, environmentId]);
  const refreshing = rowsRefreshing(cells);

  if (state.isPending) return <Loading what={`environment ${environmentId}`} />;
  if (state.isError || !state.data || state.data.type !== "environment") {
    return <ErrorPanel message={errorMessage(state.error) ?? "no data"} />;
  }
  const definition = state.data.data as EnvironmentDefinition;
  const profileRows = deriveProfileSelectionRows(
    definition.workflow_profiles as Record<string, Record<string, string>> | undefined,
    workflows,
  );
  const overrideRows = flattenOverrides(definition.overrides);
  const settled = rowsSettled(rows);

  return (
    <>
      <Section id="workflows" title={`Workflows in ${environmentId}`}>
        <div className="panel">
          {!settled || refreshing ? (
            <div className="hint">Resolving workflows — states may still change.</div>
          ) : null}
          <table className="grid">
            <thead>
              <tr>
                <th>Workflow</th>
                <th>Admission</th>
                <th>Policies</th>
                <th>Spec digest</th>
                <th>Insights</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.workflowId}>
                  <td>
                    <a href={`#/workflows/${encodeURIComponent(row.workflowId)}?env=${encodeURIComponent(environmentId)}`}>
                      <Mono>{row.workflowId}</Mono>
                    </a>
                  </td>
                  <td>
                    <StateBadge row={row} />
                    {row.detail ? <span className="faint"> {row.detail}</span> : null}
                  </td>
                  <td>
                    {row.policies.length > 0 ? <Mono>{row.policies.join(", ")}</Mono> : <span className="faint">—</span>}
                    {row.policyHash ? (
                      <>
                        {" "}
                        <ShortDigest value={row.policyHash} />
                      </>
                    ) : null}
                  </td>
                  <td>{row.specDigest ? <ShortDigest value={row.specDigest} /> : <span className="faint">—</span>}</td>
                  <td>
                    {row.worstSeverity !== undefined && row.insightCount > 0 ? (
                      <>
                        <SeverityBadge severity={row.worstSeverity} /> <span className="faint">×{row.insightCount}</span>
                      </>
                    ) : row.state === "ok" ? (
                      <span className="faint">none</span>
                    ) : (
                      <span className="faint">—</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Section>

      <Section id="profiles" title="Per-workflow profile selections">
        <div className="panel">
          {profileRows.length === 0 ? (
            <div className="hint">No per-workflow selections — workflows use their manifest-level profiles.</div>
          ) : (
            <table className="grid">
              <thead>
                <tr>
                  <th>Workflow</th>
                  <th>Kind</th>
                  <th>Selected profile</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {profileRows.map((row) => (
                  <tr key={`${row.workflowId}:${row.kind}`}>
                    <td>
                      <Mono>{row.workflowId}</Mono>
                    </td>
                    <td>
                      <Mono>{row.kind}</Mono>
                    </td>
                    <td>
                      <Mono>{row.selected}</Mono>
                    </td>
                    <td>
                      {row.differsFromDefault ? (
                        <Badge kind="warning">{`replaces ${row.workflowDefault ?? "default"}`}</Badge>
                      ) : (
                        <span className="faint">—</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </Section>

      <Section id="overrides" title="Environment overrides">
        <div className="panel">
          {overrideRows.length === 0 ? (
            <div className="hint">No overrides — workflows run with their YAML values.</div>
          ) : (
            <KV rows={overrideRows.map(([path, value]) => [path, <Mono key={path}>{value}</Mono>])} />
          )}
        </div>
      </Section>

      <Section id="definition" title="Environment definition">
        <div className="panel">
          <KV
            rows={[
              ["Name", <Mono key="n">{definition.name}</Mono>],
              [
                "Source",
                <span key="s">
                  <Mono>{definition.profile_path}</Mono>{" "}
                  {sourcePath !== undefined ? (
                    <SourceLinks {...definitionSourceLinks(project, sourcePath)} />
                  ) : (
                    // The shell gave us no manifest-relative path for this definition — the
                    // missing ingredient is the PATH, not git provenance; say so (Bugbot).
                    <SourceLinks reason="path not manifest-relative" />
                  )}
                </span>,
              ],
              ["Variables", <Mono key="v">{(definition.variable_names ?? []).join(", ") || "—"}</Mono>],
              [
                "Used by",
                (definition.used_by ?? []).length > 0 ? (
                  // Linked like the old definition view (Bugbot): each reference deep-links
                  // to the workflow pinned to THIS environment.
                  <span key="u">
                    {(definition.used_by ?? []).map((reference, index) => (
                      <span key={reference}>
                        {index > 0 ? ", " : ""}
                        <a href={`#/workflows/${encodeURIComponent(reference.split(" ")[0])}?env=${encodeURIComponent(environmentId)}`}>
                          <Mono>{reference}</Mono>
                        </a>
                      </span>
                    ))}
                  </span>
                ) : (
                  <span className="faint" key="u">
                    —
                  </span>
                ),
              ],
            ]}
          />
          <JsonView label="env files" value={definition.env_files} />
          <div className="hint">Managed in code — variable values never leave the server; names only.</div>
        </div>
      </Section>
    </>
  );
}
