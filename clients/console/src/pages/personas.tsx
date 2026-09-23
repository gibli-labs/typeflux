/**
 * Persona landing views (#721 / #577 §5): four read-only surfaces that answer one role's
 * questions from EXISTING read-tier data — governance (compliance owner), security posture
 * (security reviewer), operations (SRE), executive (leadership). They compose the Phase-0 query
 * cache (bundle matrix, policy definitions, cross-workflow executions) and reuse the Phase-3
 * `SourceLinks` hand-off everywhere; all rollup arithmetic lives in `personas.ts` (pure, tested).
 *
 * These wrap — never duplicate — the operator surfaces: the Governance view leads with the
 * coverage rollup and links into the full `#/governance` matrix + composition chains; the
 * Operations view leads with a failure-first rollup over the same executions the `#/runs` surface
 * renders. Remediation stays read-only: a copyable pointer or a link, never a write.
 */

import { useMemo, type ReactNode } from "react";
import { Link } from "@tanstack/react-router";

import type {
  Capabilities,
  EnvironmentSummary,
  PolicySummary,
  ProjectSummary,
  WorkflowSummary,
} from "../api";
import {
  Badge,
  Loading,
  Mono,
  ResolutionUnavailable,
  Section,
  ShortDigest,
  SourceLinks,
} from "../components";
import { langfuseBaseFromCells, resolvedWorkflowIdsInEnv } from "../enforcementFeed";
import { EnforcementFeedSection } from "../panels/enforcement";
import { deriveGovernanceCoverage } from "../governance";
import { deriveCrossEnvInsights } from "../crossEnv";
import { deriveEnvironmentDriftFeed, derivePlanDrift } from "../driftFeed";
import { definitionSourceLinks } from "../links";
import {
  deriveCriticalDrift,
  deriveExecutiveRollup,
  deriveGovernanceRollup,
  deriveOperationsSummary,
  derivePolicySecurityControl,
  deriveSecurityPosture,
  deriveSecurityRollup,
  type GovernanceStatus,
  type PostureState,
  type SecurityRow,
} from "../personas";
import {
  useBundleCoverage,
  useDeployments,
  useExecutionsMatrix,
  usePolicyDefinitions,
} from "../queries";
import { statusSeverity, triageOrder, type TriageRow } from "../runsFeed";

// ── shared bits ───────────────────────────────────────────────────────────────

/** The one nav group (#721): the four persona views cross-link so they read as a set, and each
 * URL (`#/personas/<view>`) is shareable. Exported as the SINGLE source of the persona nav so the
 * Shell sidebar maps the same list rather than keeping its own inline copy (#721 F11); `slug` is
 * the URL segment the sidebar uses to mark the active item. */
export const PERSONA_LINKS = [
  { to: "/personas/governance", slug: "governance", label: "Governance" },
  { to: "/personas/security", slug: "security", label: "Security posture" },
  { to: "/personas/operations", slug: "operations", label: "Operations" },
  { to: "/personas/executive", slug: "executive", label: "Executive" },
] as const;

function PersonaNav({ active, env }: { active: string; env: string }) {
  return (
    <div className="page-actions" style={{ marginBottom: 12 }}>
      {PERSONA_LINKS.map((link) => (
        <Link
          key={link.to}
          to={link.to}
          search={{ env }}
          className="crumb"
          style={{ marginRight: 12, fontWeight: link.to === active ? 700 : 400 }}
        >
          {link.label}
        </Link>
      ))}
    </div>
  );
}

/** A compact rollup number with a label — the "read the headline first" tiles. */
function Stat({
  value,
  label,
  kind,
}: {
  value: string | number;
  label: string;
  kind?: "ok" | "warning" | "critical" | "neutral";
}) {
  // theme.css defines --crit / --warn / --ok (not --critical / --warning); use the real vars so
  // the palette wins over the hex fallback in both light and dark (#721 F6).
  const color =
    kind === "critical"
      ? "var(--crit, #d33)"
      : kind === "warning"
        ? "var(--warn, #c80)"
        : kind === "ok"
          ? "var(--ok, #2a2)"
          : "inherit";
  return (
    <div style={{ minWidth: 120, marginRight: 24, marginBottom: 8 }}>
      <div style={{ fontSize: 26, fontWeight: 700, color, lineHeight: 1.1 }}>{value}</div>
      <div className="dim" style={{ fontSize: 12 }}>
        {label}
      </div>
    </div>
  );
}

function StatRow({ children }: { children: ReactNode }) {
  return <div className="row" style={{ flexWrap: "wrap", alignItems: "flex-start" }}>{children}</div>;
}

// ── Governance persona ──────────────────────────────────────────────────────────

const GOVERNANCE_BADGE: Record<GovernanceStatus, { kind: "ok" | "warning" | "critical" | "neutral"; label: string }> = {
  governed: { kind: "ok", label: "governed" },
  partial: { kind: "warning", label: "partial" },
  ungoverned: { kind: "warning", label: "ungoverned" },
  failed: { kind: "critical", label: "composition failed" },
  unassessable: { kind: "neutral", label: "unresolvable" },
};

export function PersonaGovernanceView({
  env,
  workflows,
  environments,
  policies,
  project,
  capabilities,
}: {
  env: string;
  workflows: WorkflowSummary[];
  environments: EnvironmentSummary[];
  policies: PolicySummary[];
  project?: ProjectSummary;
  capabilities: Capabilities;
}) {
  const environmentIds = environments.map((environment) => environment.id);
  const { cells, settled, fingerprint } = useBundleCoverage(workflows, environments);
  // eslint-disable-next-line react-hooks/exhaustive-deps -- fingerprint stands in for cells
  const rollup = useMemo(() => deriveGovernanceRollup(deriveGovernanceCoverage(cells)), [fingerprint]);
  // The feed's env-scoped inputs, memoized behind the matrix fingerprint (#723 F6) —
  // the file's convention for O(cells) derivations. The workflow filter is scoped to
  // the workflows that resolve in this env (an option that can't yield an event is noise).
  const feed = useMemo(
    () => ({
      langfuseBase: langfuseBaseFromCells(cells, env),
      workflowIds: resolvedWorkflowIdsInEnv(cells, env),
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- fingerprint stands in for cells
    [fingerprint, env],
  );

  const { definitions, settled: definitionsSettled, pathById } = usePolicyDefinitions(policies);

  return (
    <>
      <PersonaNav active="/personas/governance" env={env} />
      <Section id="coverage-rollup" title="Governance coverage">
        {settled && rollup.assessedCount === 0 ? (
          // Nothing resolved → the loud honest-unavailable panel (its own .panel), never a green
          // rollup over zero data (#721 F12). It carries its own panel, so it is NOT nested here.
          <ResolutionUnavailable what="Policy coverage" />
        ) : (
          <div className="panel">
          {!settled ? (
            <Loading what={`policy coverage across ${environmentIds.length} environments`} />
          ) : (
            <>
              <StatRow>
                <Stat
                  value={rollup.coveredPct === null ? "—" : `${rollup.coveredPct}%`}
                  label="under policy (all envs)"
                  kind={rollup.coveredPct === 100 ? "ok" : rollup.coveredPct === 0 ? "critical" : "warning"}
                />
                <Stat value={rollup.governedCount} label="governed" kind="ok" />
                <Stat value={rollup.partialCount} label="partial" kind={rollup.partialCount > 0 ? "warning" : "neutral"} />
                <Stat value={rollup.ungovernedCount} label="ungoverned" kind={rollup.ungovernedCount > 0 ? "warning" : "neutral"} />
                <Stat value={rollup.failedCount} label="composition failed" kind={rollup.failedCount > 0 ? "critical" : "neutral"} />
              </StatRow>
              <div className="hint">
                {rollup.governedCount} of {rollup.assessedCount} resolvable workflows apply a policy
                in every environment they resolve in. The full coverage grid, per-environment cells,
                and composition chains live on the{" "}
                <Link to="/governance" search={{ env }}>
                  Governance surface
                </Link>
                .
              </div>
            </>
          )}
          </div>
        )}
      </Section>

      {settled && rollup.assessedCount > 0 ? (
        <Section id="workflow-coverage" title="Per-workflow coverage">
          <div className="panel">
            <table className="grid">
              <thead>
                <tr>
                  <th>Workflow</th>
                  <th>Status</th>
                  <th>Environments without policy</th>
                </tr>
              </thead>
              <tbody>
                {rollup.rows.map((row) => {
                  const badge = GOVERNANCE_BADGE[row.status];
                  const gaps = [
                    ...row.failedEnvs.map((e) => `${e} (failed)`),
                    ...row.ungovernedEnvs,
                  ];
                  return (
                    <tr key={row.workflowId}>
                      <td>
                        <Link to="/workflows/$workflowId" params={{ workflowId: row.workflowId }} search={{ env, section: "policy" }}>
                          <Mono>{row.workflowId}</Mono>
                        </Link>
                      </td>
                      <td>
                        <Badge kind={badge.kind}>{badge.label}</Badge>
                      </td>
                      <td>
                        {gaps.length > 0 ? <Mono>{gaps.join(", ")}</Mono> : <span className="faint">—</span>}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </Section>
      ) : null}

      <Section id="policy-hashes" title="Policies & effective hashes">
        <div className="panel">
          {policies.length === 0 ? (
            <span className="dim">This project declares no policies.</span>
          ) : !definitionsSettled ? (
            <Loading what="policy definitions" />
          ) : (
            <table className="grid">
              <thead>
                <tr>
                  <th>Policy</th>
                  <th>Effective hash</th>
                  <th>Source</th>
                </tr>
              </thead>
              <tbody>
                {definitions.map((cell) => {
                  const definition = cell.definition;
                  return (
                    <tr key={cell.policyId}>
                      <td>
                        <Link to="/policies/$policyId" params={{ policyId: cell.policyId }} search={{ env }}>
                          <Mono>{definition?.name ?? cell.policyId}</Mono>
                        </Link>
                      </td>
                      <td>
                        {!definition ? (
                          <span className="faint">definition unavailable</span>
                        ) : definition.policy_hash ? (
                          <ShortDigest value={definition.policy_hash} />
                        ) : (
                          <Badge kind="critical">composition failed</Badge>
                        )}
                      </td>
                      <td>
                        <SourceLinks {...definitionSourceLinks(project, pathById.get(cell.policyId) ?? "")} />
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
          <div className="hint">
            The policy hash is the composed identity a deployment fails closed against; it is what a
            start/review is checked with (<Mono>expected_policy_hash</Mono>).
          </div>
        </div>
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

// ── Security posture persona ─────────────────────────────────────────────────────

const POSTURE_BADGE: Record<PostureState, { kind: "ok" | "warning" | "neutral"; label: string; title: string }> = {
  on: { kind: "ok", label: "on", title: "The control is present and enabled." },
  off: { kind: "warning", label: "off", title: "The control is present but disabled or weak — review it." },
  na: { kind: "neutral", label: "n/a", title: "The control does not apply to this resolved bundle." },
  unknown: {
    kind: "neutral",
    label: "not reported",
    title: "This control plane does not report this control for the resolved bundle.",
  },
};

const POSTURE_DIMENSIONS: Array<{ key: string; header: string }> = [
  { key: "redaction", header: "Redaction" },
  { key: "manifest", header: "Exec manifest" },
  { key: "tls", header: "Temporal TLS" },
  { key: "provider_key", header: "Provider key" },
  { key: "secrets", header: "Secrets" },
  { key: "image_pin", header: "Image pin" },
];

function PostureCell({ row, dimensionKey }: { row: SecurityRow; dimensionKey: string }) {
  const dimension = row.dimensions.find((d) => d.key === dimensionKey);
  if (!dimension) return <span className="faint">—</span>;
  const badge = POSTURE_BADGE[dimension.state];
  return (
    <span title={dimension.detail || badge.title}>
      <Badge kind={badge.kind}>{badge.label}</Badge>
    </span>
  );
}

export function PersonaSecurityView({
  env,
  workflows,
  environments,
  policies,
  project,
}: {
  env: string;
  workflows: WorkflowSummary[];
  environments: EnvironmentSummary[];
  policies: PolicySummary[];
  project?: ProjectSummary;
}) {
  const environmentIds = environments.map((environment) => environment.id);
  const { cells, settled, fingerprint } = useBundleCoverage(workflows, environments);
  // Fold the rollup into the SAME memo as the rows (#721 F13): both derive from the cells and are
  // keyed on the one fingerprint, so the headline counts and the matrix can never disagree.
  // eslint-disable-next-line react-hooks/exhaustive-deps -- fingerprint stands in for cells
  const { rows, rollup } = useMemo(() => {
    const rows = deriveSecurityPosture(cells);
    return { rows, rollup: deriveSecurityRollup(rows) };
  }, [fingerprint]);

  const { definitions, settled: definitionsSettled, pathById } = usePolicyDefinitions(policies);

  return (
    <>
      <PersonaNav active="/personas/security" env={env} />
      {/* Titled distinctly from the page's own "Security posture" heading — a duplicate
          heading name is ambiguous for accessibility/tests (strict-mode violation). */}
      <Section id="security-rollup" title="Posture rollup">
        {settled && rollup.resolvedRows === 0 ? (
          <ResolutionUnavailable what="Security posture" />
        ) : (
          <div className="panel">
          {!settled ? (
            <Loading what={`security posture across ${environmentIds.length} environments`} />
          ) : (
            <>
              <StatRow>
                <Stat value={rollup.resolvedRows} label="resolved bundles" kind="neutral" />
                <Stat value={rollup.redactionOff} label="redaction off" kind={rollup.redactionOff > 0 ? "warning" : "ok"} />
                <Stat value={rollup.tlsOff} label="TLS off" kind={rollup.tlsOff > 0 ? "warning" : "ok"} />
                <Stat value={rollup.missingSecrets} label="missing secrets" kind={rollup.missingSecrets > 0 ? "critical" : "ok"} />
                <Stat value={rollup.unpinnedImages} label="mutable images" kind={rollup.unpinnedImages > 0 ? "warning" : "ok"} />
                <Stat value={rollup.providerKeyMissing} label="provider key unset" kind={rollup.providerKeyMissing > 0 ? "warning" : "ok"} />
                {rollup.unresolvedRows > 0 ? (
                  <Stat value={rollup.unresolvedRows} label="unresolvable" kind="neutral" />
                ) : null}
              </StatRow>
              <div className="hint">
                Each control is read defensively from the resolved bundle — a control this control
                plane does not report shows as <Badge kind="neutral">not reported</Badge> rather than
                a false pass. Values never leave the server; only configured state.
              </div>
            </>
          )}
          </div>
        )}
      </Section>

      {settled && rollup.resolvedRows > 0 ? (
        <Section id="posture-matrix" title="Runtime controls · per workflow × environment">
          <div className="panel">
            <table className="grid">
              <thead>
                <tr>
                  <th>Workflow</th>
                  <th>Environment</th>
                  {POSTURE_DIMENSIONS.map((dimension) => (
                    <th key={dimension.key}>{dimension.header}</th>
                  ))}
                  <th>Source</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={`${row.workflowId}:${row.env}`}>
                    <td>
                      <Link to="/workflows/$workflowId" params={{ workflowId: row.workflowId }} search={{ env: row.env, section: "secrets" }}>
                        <Mono>{row.workflowId}</Mono>
                      </Link>
                    </td>
                    <td>
                      <Mono>{row.env}</Mono>
                    </td>
                    {row.resolved ? (
                      POSTURE_DIMENSIONS.map((dimension) => (
                        <td key={dimension.key}>
                          <PostureCell row={row} dimensionKey={dimension.key} />
                        </td>
                      ))
                    ) : (
                      <td colSpan={POSTURE_DIMENSIONS.length}>
                        <span className="badge badge-neutral" title="The workflow does not resolve in this environment; its security posture is unknown.">
                          unresolvable
                        </span>
                      </td>
                    )}
                    <td>
                      {row.resolved ? <SourceLinks {...row.sourceLinks} /> : <span className="faint">—</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Section>
      ) : null}

      <Section id="policy-controls" title="Policy-enforced controls">
        <div className="panel">
          {policies.length === 0 ? (
            <span className="dim">
              This project declares no policies — redaction requirements, module allowlists, and
              artifact-source allowlists are unenforced.
            </span>
          ) : !definitionsSettled ? (
            <Loading what="policy definitions" />
          ) : (
            <table className="grid">
              <thead>
                <tr>
                  <th>Policy</th>
                  <th>Redaction required</th>
                  <th>Module allowlist</th>
                  <th>Artifact allowlist</th>
                  <th>Source</th>
                </tr>
              </thead>
              <tbody>
                {definitions.map((cell) => {
                  if (!cell.definition) {
                    return (
                      <tr key={cell.policyId}>
                        <td>
                          <Mono>{cell.policyId}</Mono>
                        </td>
                        <td colSpan={4}>
                          <span className="faint">definition unavailable</span>
                        </td>
                      </tr>
                    );
                  }
                  const control = derivePolicySecurityControl(
                    cell.definition,
                    definitionSourceLinks(project, pathById.get(cell.policyId) ?? ""),
                  );
                  const yesNo = (value: boolean) =>
                    value ? <Badge kind="ok">yes</Badge> : <span className="faint">no</span>;
                  // Redaction is three-state (#721 F4): a direct requirement reads "required",
                  // a risk-tier-gated one "required (risk-tier)" (it applies only to workflows
                  // at/above the tier), and neither reads "no".
                  const redactionCell = !control.redactionRequired ? (
                    <span className="faint">no</span>
                  ) : control.redactionTierGated ? (
                    <Badge
                      kind="ok"
                      title="Required by a risk tier (risk_tiers.<tier>.require_redaction) — enforced for workflows at or above that tier; #300 fails closed on it."
                    >
                      required (risk-tier)
                    </Badge>
                  ) : (
                    <Badge kind="ok">required</Badge>
                  );
                  return (
                    <tr key={cell.policyId}>
                      <td>
                        <Link to="/policies/$policyId" params={{ policyId: cell.policyId }} search={{ env }}>
                          <Mono>{control.policyName}</Mono>
                        </Link>
                      </td>
                      <td>{redactionCell}</td>
                      <td>{yesNo(control.hasImportAllowlist)}</td>
                      <td>{yesNo(control.hasArtifactAllowlist)}</td>
                      <td>
                        <SourceLinks {...control.sourceLinks} />
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
          <div className="hint">
            Presence of a control in the policy YAML (not its merged values, which the backend
            composes). A policy tightens what every governed workflow may import, redact, and read
            as artifacts.
          </div>
        </div>
      </Section>
    </>
  );
}

// ── Operations persona ──────────────────────────────────────────────────────────

export function PersonaOperationsView({
  env,
  workflows,
}: {
  env: string;
  workflows: WorkflowSummary[];
}) {
  const workflowIds = workflows.map((workflow) => workflow.id);
  const cells = useExecutionsMatrix(workflowIds, env);
  const pendingCells = cells.filter((cell) => cell.pending);
  const settled = pendingCells.length === 0;
  const summary = deriveOperationsSummary(cells);
  const rows: TriageRow[] = triageOrder(
    cells.flatMap((cell) => cell.executions.map((record) => ({ workflowId: cell.workflowId, record }))),
  );

  return (
    <>
      <PersonaNav active="/personas/operations" env={env} />
      <Section id="ops-rollup" title={`Operational health · ${env}`}>
        <div className="panel">
          {!settled ? (
            // Gate the StatRow behind settle (#721 F2): rendering it while cells pend folds the
            // still-loading zeros into confident "0 failing" tiles beside the loading banner — a
            // false all-clear. Match the other persona views: loading, THEN the rollup.
            <div className="row" style={{ marginBottom: 8 }}>
              <Loading what={`executions for ${pendingCells.length} of ${workflowIds.length} workflows`} />
            </div>
          ) : (
            <>
              <StatRow>
                <Stat value={summary.failing} label="failing" kind={summary.failing > 0 ? "critical" : "ok"} />
                <Stat value={summary.running} label="running" kind="neutral" />
                <Stat value={summary.oldVersionRuns} label="on old versions" kind={summary.oldVersionRuns > 0 ? "warning" : "ok"} />
                <Stat value={summary.completed} label="completed" kind="neutral" />
                {summary.unavailableWorkflows.length > 0 ? (
                  <Stat value={summary.unavailableWorkflows.length} label="unreadable" kind="warning" />
                ) : null}
              </StatRow>
              <div className="hint">
                Failure-first over every workflow&apos;s recent executions (the same cross-workflow
                feed as{" "}
                <Link to="/runs" search={{ env }}>
                  Runs
                </Link>
                ). Executions on a workflow type other than the current spec digest are the drain
                signal — old versions are not safe to decommission until they finish; per-workflow
                drain and worker polling live on each workflow&apos;s versions / runs pages.
              </div>
            </>
          )}
        </div>
      </Section>

      <Section id="ops-triage" title="Failure-first triage">
        <div className="panel">
          {settled && rows.length === 0 ? (
            <div className="row">
              {summary.unavailableWorkflows.length === 0 ? (
                <>
                  <Badge kind="ok">no recent executions</Badge>
                  <span className="dim">Nothing has run recently in this environment.</span>
                </>
              ) : summary.unavailableWorkflows.length === cells.length ? (
                <>
                  <span className="badge badge-neutral">no executions readable</span>
                  <span className="dim">
                    Every workflow&apos;s executions were unreadable — is Temporal reachable in this
                    environment?
                  </span>
                </>
              ) : (
                <>
                  <span className="badge badge-neutral">no recent executions readable</span>
                  <span className="dim">Nothing recent among the readable workflows.</span>
                </>
              )}
            </div>
          ) : rows.length > 0 ? (
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
                      <Link to="/workflows/$workflowId/runs" params={{ workflowId }} search={{ env, run: record.execution_id }}>
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
          ) : null}
          {summary.unavailableWorkflows.length > 0 ? (
            <div className="hint">
              Executions unavailable: {summary.unavailableWorkflows.join(" · ")} — check that Temporal
              is reachable in this environment (each returns a bounded error, never a stall).
            </div>
          ) : null}
          <div className="hint">
            Click an execution to open its addressable run view — Temporal history, the Langfuse trace,
            the executed source at sha, and the applied policy are the per-run hand-offs there.
          </div>
        </div>
      </Section>
    </>
  );
}

// ── Executive persona ───────────────────────────────────────────────────────────

export function PersonaExecutiveView({
  env,
  workflows,
  environments,
  policies,
}: {
  env: string;
  workflows: WorkflowSummary[];
  environments: EnvironmentSummary[];
  policies: PolicySummary[];
}) {
  const workflowIds = workflows.map((workflow) => workflow.id);
  const environmentIds = environments.map((environment) => environment.id);
  const { cells, settled, fingerprint } = useBundleCoverage(workflows, environments);
  // Plan drift feeds the "critical drift" headline (#721 F5). This shares the SAME cached
  // deployments query the Deployments/Drift pages use — not a new fetch pattern.
  const deployments = useDeployments();
  const deploymentsSettled = !deployments.isPending;
  const rollup = useMemo(() => {
    const governance = deriveGovernanceRollup(deriveGovernanceCoverage(cells));
    // Critical drift = the SAME three critical-drift derivations the Drift page renders:
    // cross-env admission splits + version/governance environment drift + drifted approved plans.
    const crossEnv = deriveCrossEnvInsights(cells);
    const otherEnvs = environmentIds.filter((id) => id !== env);
    const environmentDrift = environmentIds.includes(env)
      ? deriveEnvironmentDriftFeed(env, otherEnvs, workflowIds, cells)
      : { rows: [], notes: [] };
    // An errored deployments query contributes NO plan criticals — TanStack keeps the last
    // successful payload on a refetch error, so `deployments.data` would silently count STALE
    // plan drift (Bugbot). The stat still renders the cross-env + environment criticals, and
    // the panel shows an explicit plan-drift-unavailable hint below.
    const planDrift = deployments.isError ? [] : derivePlanDrift(deployments.data ?? [], workflowIds);
    const driftCount = deriveCriticalDrift({
      crossEnv: crossEnv.insights,
      environmentDrift: environmentDrift.rows,
      planDrift,
    });
    return deriveExecutiveRollup({
      workflowCount: workflowIds.length,
      environmentCount: environmentIds.length,
      policyCount: policies.length,
      governance,
      driftCount,
      selectedEnvCells: cells.filter((cell) => cell.env === env),
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps -- fingerprint stands in for cells
  }, [fingerprint, env, workflowIds.length, environmentIds.length, policies.length, deployments.dataUpdatedAt, deployments.isError]);

  // Gate on BOTH the bundle matrix and the deployments query (F2 discipline): the drift stat is
  // derived from unsettled deployments otherwise, and a pending zero reads as a false all-clear.
  const ready = settled && deploymentsSettled;

  return (
    <>
      <PersonaNav active="/personas/executive" env={env} />
      <Section id="executive-rollup" title="Governance at a glance">
        <div className="panel">
          {!ready ? (
            <Loading what="project rollup" />
          ) : (
            <>
              <StatRow>
                <Stat value={rollup.workflowCount} label="AI workflows" kind="neutral" />
                <Stat
                  value={rollup.underPolicyPct === null ? "—" : `${rollup.underPolicyPct}%`}
                  label="under policy"
                  kind={rollup.underPolicyPct === 100 ? "ok" : rollup.underPolicyPct === 0 ? "critical" : "warning"}
                />
                <Stat value={rollup.validatingWorkflows} label="validating cleanly" kind="ok" />
                <Stat value={rollup.workflowsWithIssues} label="with issues" kind={rollup.workflowsWithIssues > 0 ? "warning" : "ok"} />
                <Link
                  to="/drift"
                  search={{ env }}
                  style={{ color: "inherit", textDecoration: "none" }}
                  title="Critical drift across environments, approved plans, and admission — open the Drift page"
                >
                  <Stat value={rollup.driftCount} label="critical drift" kind={rollup.driftCount > 0 ? "critical" : "ok"} />
                </Link>
                <Stat value={rollup.reviewGateWorkflows} label="human review gates" kind="neutral" />
              </StatRow>
              <ExecutiveNarrative rollup={rollup} env={env} />
              {deployments.isError ? (
                // Loud degradation: the drift tile above excludes plan drift in this state —
                // say so rather than letting the number quietly narrow its meaning.
                <div className="hint">
                  Plan drift is unavailable (the deployment-plans query failed) — the critical-drift
                  count covers admission and environment drift only.
                </div>
              ) : null}
            </>
          )}
        </div>
      </Section>
    </>
  );
}

function ExecutiveNarrative({
  rollup,
  env,
}: {
  rollup: ReturnType<typeof deriveExecutiveRollup>;
  env: string;
}) {
  const pct = rollup.underPolicyPct;
  return (
    <>
      <p style={{ marginBottom: 6 }}>
        This project runs <strong>{rollup.workflowCount}</strong> AI{" "}
        {rollup.workflowCount === 1 ? "workflow" : "workflows"} across{" "}
        <strong>{rollup.environmentCount}</strong>{" "}
        {rollup.environmentCount === 1 ? "environment" : "environments"}, governed by{" "}
        <strong>{rollup.policyCount}</strong> {rollup.policyCount === 1 ? "policy" : "policies"}.
      </p>
      <p style={{ marginBottom: 6 }}>
        {pct === null
          ? "No workflow resolves against this control plane yet, so governance coverage cannot be reported."
          : pct === 100
            ? "Every workflow that resolves applies a policy in each of its environments."
            : `${rollup.governedWorkflows} of the resolvable workflows are fully under policy (${pct}%); ${rollup.ungovernedWorkflows} run with a gap in at least one environment.`}
      </p>
      <p style={{ marginBottom: 6 }}>
        In the <Mono>{env}</Mono> environment, <strong>{rollup.validatingWorkflows}</strong>{" "}
        {rollup.validatingWorkflows === 1 ? "workflow validates" : "workflows validate"} cleanly and{" "}
        <strong>{rollup.workflowsWithIssues}</strong>{" "}
        {rollup.workflowsWithIssues === 1 ? "has" : "have"} open issues.
        {rollup.unresolvableWorkflows > 0
          ? ` ${rollup.unresolvableWorkflows} could not be resolved here.`
          : ""}{" "}
        <strong>{rollup.reviewGateWorkflows}</strong>{" "}
        {rollup.reviewGateWorkflows === 1 ? "workflow requires" : "workflows require"} human review
        before acting.
      </p>
      <p style={{ marginBottom: 6 }}>
        {rollup.driftCount === 0 ? (
          <>
            No <strong>critical drift</strong> across environments, approved plans, or admission.
          </>
        ) : (
          <>
            <strong>{rollup.driftCount}</strong> critical drift{" "}
            {rollup.driftCount === 1 ? "finding" : "findings"} across environments, approved plans,
            and admission would break a promote.
          </>
        )}{" "}
        See the{" "}
        <Link to="/drift" search={{ env }}>
          Drift page
        </Link>{" "}
        for the detail, including prompt/registry drift (tracked there, not counted here).
      </p>
      <div className="hint">
        Individual policy violations live on the{" "}
        <Link to="/personas/governance" search={{ env }}>
          Governance persona view
        </Link>
        &apos;s enforcement feed (#723); a violation/incident TREND over time is still open — the
        feed is read-at-request, so trending needs an aggregation surface. Counts are read live
        from resolved source of truth; no digests to interpret.
      </div>
    </>
  );
}
