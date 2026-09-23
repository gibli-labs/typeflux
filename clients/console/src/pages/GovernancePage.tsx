import type { Capabilities, EnvironmentSummary, PolicySummary, WorkflowSummary } from "../api";
import { useMemo } from "react";

import { Badge, InsightList, Loading, Mono, Section, ShortDigest } from "../components";
import { langfuseBaseFromCells, resolvedWorkflowIdsInEnv } from "../enforcementFeed";
import type { Insight } from "../insights";
import {
  compositionChain,
  deriveGovernanceCoverage,
  deriveGovernanceGaps,
  extendsProvenance,
  hasAssessableCoverage,
} from "../governance";
import { EnforcementFeedSection } from "../panels/enforcement";
import {
  CompositionChainPanel,
  CoverageMatrixPanel,
  PolicyRulesPanel,
} from "../panels/governance";
import { derivePolicyMatrix, type PolicyCell } from "../policyMatrix";
import { type BundleCell, cellFingerprint, useBundleMatrix, usePolicyDefinitionMap } from "../queries";

/**
 * The Governance page (#587): where policy applies (coverage from resolved
 * bundles), where it doesn't (gaps feed), what each policy says (structured
 * rules + composition chain), and where policy actually FIRED — the
 * enforcement-events feed (#723 / #577 §2), shared with the governance
 * persona view (one component, two mounts — the coverage pattern).
 */
function GovernanceGapsBody({ gaps, assessable }: { gaps: Insight[]; assessable: boolean }) {
  if (gaps.length > 0) return <InsightList insights={gaps} />;
  if (!assessable) {
    // An empty gaps list means nothing when nothing resolved — never show an
    // all-clear over an entirely unresolvable matrix.
    return (
      <span className="dim">
        No (workflow, environment) pair resolves, so policy coverage could not be assessed — see
        the matrix below.
      </span>
    );
  }
  return (
    <span className="dim">
      Every resolvable (workflow, environment) pair has an applied policy.
    </span>
  );
}

export function GovernancePage({
  env,
  workflows,
  environments,
  policies,
  capabilities,
}: {
  env: string;
  workflows: WorkflowSummary[];
  environments: EnvironmentSummary[];
  policies: PolicySummary[];
  capabilities: Capabilities;
}) {
  const workflowIds = workflows.map((workflow) => workflow.id);
  const environmentIds = environments.map((environment) => environment.id);
  const cells = useBundleMatrix(workflowIds, environmentIds);
  const settled = cells.every((cell) => !cell.pending);
  // The feed's env-scoped inputs, memoized behind the matrix fingerprint (#723 F6):
  // the Langfuse base and the workflow-filter options scoped to workflows that
  // resolve in this env. Same convention as PolicyMatrixSection's fingerprint memo.
  const fingerprint = cellFingerprint(cells);
  const feed = useMemo(
    () => ({
      langfuseBase: langfuseBaseFromCells(cells, env),
      workflowIds: resolvedWorkflowIdsInEnv(cells, env),
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- fingerprint stands in for cells
    [fingerprint, env],
  );
  // One coverage derivation feeds both the gaps feed and the matrix (cheap
  // dict-building — no memo needed, unlike the Drift page's bundle diffing).
  const coverage = settled ? deriveGovernanceCoverage(cells) : null;
  const definitions = usePolicyDefinitionMap(policies.map((policy) => policy.id));
  const definitionsSettled = definitions.every((cell) => !cell.pending);
  const policyMap = new Map(
    definitions.flatMap((cell) => (cell.definition ? [[cell.policyId, cell.definition] as const] : [])),
  );

  return (
    <>
      <PolicyMatrixSection cells={cells} environmentIds={environmentIds} env={env} />
      <Section id="gaps" title="Governance gaps">
        <div className="panel">
          {!coverage ? (
            <Loading what={`policy coverage across ${environmentIds.length} environments`} />
          ) : (
            <GovernanceGapsBody
              gaps={deriveGovernanceGaps(coverage)}
              assessable={hasAssessableCoverage(coverage)}
            />
          )}
        </div>
      </Section>

      {coverage ? (
        <CoverageMatrixPanel rows={coverage} environmentIds={environmentIds} env={env} />
      ) : null}

      <Section id="policies" title="Policies">
        {policies.length === 0 ? (
          <div className="panel">
            <span className="dim">This project declares no policies.</span>
          </div>
        ) : !definitionsSettled ? (
          <div className="panel">
            <Loading what="policy definitions" />
          </div>
        ) : (
          policies.map((policy) => {
            const definition = policyMap.get(policy.id);
            if (!definition) {
              return (
                <div key={policy.id} className="panel" style={{ marginBottom: 12 }}>
                  <Mono>{policy.id}</Mono>{" "}
                  <span className="dim">definition unavailable.</span>
                </div>
              );
            }
            const chain = compositionChain(policyMap, policy.id);
            return (
              <div key={policy.id} className="panel" style={{ marginBottom: 14 }}>
                <div className="row" style={{ marginBottom: 8 }}>
                  <Mono>{definition.name}</Mono>
                  {definition.policy_hash ? (
                    <ShortDigest value={definition.policy_hash} />
                  ) : (
                    <span className="badge badge-critical">composition failed</span>
                  )}
                  {definition.description ? (
                    <span className="dim">{definition.description}</span>
                  ) : null}
                </div>
                {chain.length > 1 ? (
                  <div style={{ marginBottom: 10 }}>
                    <CompositionChainPanel chain={chain} provenance={extendsProvenance(chain)} />
                  </div>
                ) : null}
                <PolicyRulesPanel rules={definition.rules} />
              </div>
            );
          })
        )}
      </Section>

      <EnforcementFeedSection
        env={env}
        workflowIds={feed.workflowIds}
        capabilities={capabilities}
        langfuseBase={feed.langfuseBase}
      />
    </>
  );
}

function policyCellBadge(cell: PolicyCell): { kind: "ok" | "warning" | "critical" | "neutral"; label: string } {
  if (cell.state === "governed") return { kind: "ok", label: cell.ids.join(", ") };
  if (cell.state === "ungoverned") return { kind: "warning", label: "none" };
  // Selected but unenforced — the attempted ids stay visible (codex).
  if (cell.state === "composition-failed") return { kind: "critical", label: `${cell.ids.join(", ")} (failed)` };
  if (cell.state === "pending") return { kind: "neutral", label: "…" };
  return { kind: "neutral", label: "—" };
}

/** The coverage grid (#612): who governs what, where — with ungoverned and split rows first. */
function PolicyMatrixSection({
  cells,
  environmentIds,
  env,
}: {
  cells: BundleCell[];
  environmentIds: string[];
  env: string;
}) {
  const fingerprint = cellFingerprint(cells);
  // eslint-disable-next-line react-hooks/exhaustive-deps -- fingerprint stands in for cells
  const matrix = useMemo(() => derivePolicyMatrix(cells), [fingerprint]);

  return (
    <Section id="policy-matrix" title="Policy coverage matrix">
      <div className="panel">
        {!matrix.settled ? (
          <Loading what="policy coverage matrix" />
        ) : (
          <>
            {matrix.refreshing ? (
              <div className="hint">Refreshing coverage — states may still change.</div>
            ) : null}
            <table className="grid">
              <thead>
                <tr>
                  <th>Workflow</th>
                  {environmentIds.map((environmentId) => (
                    <th key={environmentId}>
                      <Mono>{environmentId}</Mono>
                    </th>
                  ))}
                  <th />
                </tr>
              </thead>
              <tbody>
                {matrix.rows.map((row) => (
                  <tr key={row.workflowId}>
                    <td>
                      {/* #815: the id always links to the workflow's policy section — a row
                          whose cells are all pending/unresolvable must not be a dead end. */}
                      <a
                        href={`#/workflows/${encodeURIComponent(row.workflowId)}?env=${encodeURIComponent(env)}&section=policy`}
                        title={`${row.workflowId} policy in ${env}`}
                      >
                        <Mono>{row.workflowId}</Mono>
                      </a>
                    </td>
                    {row.cells.map((cell) => {
                      const badge = policyCellBadge(cell);
                      const linkable = cell.state !== "pending" && cell.state !== "unresolvable";
                      return (
                        <td key={cell.env}>
                          {linkable ? (
                            <a
                              href={`#/workflows/${encodeURIComponent(row.workflowId)}?env=${encodeURIComponent(cell.env)}&section=policy`}
                              title={`${row.workflowId} policy in ${cell.env}`}
                            >
                              <Badge kind={badge.kind}>{badge.label}</Badge>
                            </a>
                          ) : (
                            <Badge kind={badge.kind}>{badge.label}</Badge>
                          )}
                        </td>
                      );
                    })}
                    <td>
                      {row.split ? (
                        <Badge kind="warning">environments disagree</Badge>
                      ) : (
                        <span className="faint">—</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}
      </div>
    </Section>
  );
}
