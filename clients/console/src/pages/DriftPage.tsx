import { useMemo, useState } from "react";
import { isUnsupportedRuntime } from "../api";

import type { Capabilities, EnvironmentSummary, ProjectSummary, WorkflowSummary } from "../api";
import { Badge, CapabilityAbsentPanel, ErrorPanel, InsightList, Loading, Mono, NoticeRow, Section, ResolutionUnavailable } from "../components";
import {
  deriveDrainDrift,
  deriveEnvironmentDriftFeed,
  derivePinSkewDrift,
  derivePlanDrift,
  derivePromptDrift,
  temporalDriftOutage,
  type PinSkewResolution,
  type TemporalDriftFeed,
} from "../driftFeed";
import { deriveEnvMatrix, type MatrixCell } from "../envMatrix";
import {
  deriveGithubDrift,
  githubCapabilityAbsentCopy,
  type GithubDriftFeed,
} from "../githubDrift";
import type { Insight } from "../insights";
import {
  cellFingerprint,
  errorMessage,
  useBundleMatrix,
  useDeployments,
  useGithubProvenance,
  usePromptStatus,
  useRuntimePinMatrix,
  useVersionsMatrix,
} from "../queries";

/**
 * The Drift page (#583): one project-level answer to "what is drifting right
 * now, why does it matter, and what do I do next" — plan drift, critical
 * cross-environment drift, prompt-registry drift, and the two Temporal-tier
 * classes (version drain and runtime-pin skew, #577 §1), each row deep-linked
 * to the surface that owns the detail. The Temporal-tier sections fan out
 * bounded reads (#586) and degrade LOUDLY when the cluster is unreachable —
 * an explicit "status unknown" state, never an empty all-clear.
 */
export function DriftPage({
  env,
  workflows,
  environments,
  project,
  capabilities,
}: {
  env: string;
  workflows: WorkflowSummary[];
  environments: EnvironmentSummary[];
  project?: ProjectSummary;
  capabilities: Capabilities;
}) {
  const workflowIds = workflows.map((workflow) => workflow.id);
  const environmentIds = environments.map((environment) => environment.id);

  return (
    <>
      <EnvMatrixSection env={env} workflowIds={workflowIds} environmentIds={environmentIds} />
      <GithubDriftSection capabilities={capabilities} project={project} />
      <PlanDriftSection workflowIds={workflowIds} project={project} />
      <EnvironmentDriftSection env={env} workflowIds={workflowIds} environmentIds={environmentIds} />
      <PromptDriftSection env={env} workflowIds={workflowIds} />
      <VersionDrainSection env={env} workflowIds={workflowIds} />
      <PinSkewSection env={env} workflowIds={workflowIds} />
    </>
  );
}

/**
 * GitHub-vs-served drift (#727 §1): compares the served checkout against the tracked branch HEAD on
 * GitHub, read server-side at request time. Capability-gated — absent/false renders the explicit
 * not-supported panel and fetches nothing; degraded reachability (`partial.github`) renders a loud
 * banner; `ahead_of_served: null` renders an explicit unknown, never in-sync.
 */
function GithubDriftSection({
  capabilities,
  project,
}: {
  capabilities: Capabilities;
  project?: ProjectSummary;
}) {
  // Feature-detect per capability (#577 §6): absent (older contract) and false both gate the fetch.
  const supported = capabilities.github_provenance === true;
  const query = useGithubProvenance(supported);

  if (!supported) {
    return (
      <CapabilityAbsentPanel id="github-drift" title="GitHub drift" copy={githubCapabilityAbsentCopy()} />
    );
  }

  return (
    <Section id="github-drift" title="GitHub drift">
      <div className="panel">
        {query.isPending ? (
          <Loading what="github provenance" />
        ) : query.isError ? (
          isUnsupportedRuntime(query.error) ? (
            <ResolutionUnavailable what="GitHub drift" />
          ) : (
            <ErrorPanel
              message={errorMessage(query.error) ?? "github-provenance request failed"}
              hint="Read server-side from the GitHub API at request time — retry once the control plane is reachable."
            />
          )
        ) : (
          <GithubDriftBody feed={deriveGithubDrift(query.data, project)} />
        )}
        <div className="hint">
          Compares the served checkout against the tracked branch HEAD on GitHub — read server-side
          at request time, so undeployed commits become a first-class drift row.
        </div>
      </div>
    </Section>
  );
}

function GithubDriftBody({ feed }: { feed: GithubDriftFeed }) {
  return (
    <>
      {feed.notice ? <NoticeRow notice={feed.notice} /> : null}
      {feed.rows.length > 0 ? (
        <InsightList insights={feed.rows} />
      ) : feed.notice ? null : (
        // rows are non-empty whenever the source was read in full; this covers only the
        // defensive impossible-empty case, never masking a degraded (notice-carrying) read.
        <NoDrift what="No GitHub HEAD-vs-served drift." />
      )}
    </>
  );
}

// Severity matches the pairwise environment-drift feed: version/governance/secret drift is
// CRITICAL there, so a cell must never read as a downgraded (or contradictory) signal (codex).
function matrixBadge(cell: MatrixCell): { kind: "ok" | "critical" | "neutral"; label: string } {
  if (cell.state === "match") return { kind: "ok", label: "match" };
  if (cell.state === "pending") return { kind: "neutral", label: "…" };
  if (cell.state === "unresolvable") return { kind: "neutral", label: "—" };
  const parts = [
    cell.drift?.spec ? "spec" : undefined,
    cell.drift?.policy ? "policy" : undefined,
    cell.drift?.secret ? "secrets" : undefined,
  ].filter((part): part is string => part !== undefined);
  return { kind: "critical", label: parts.join("+") || "drift" };
}

/** The N-way comparison (#611): every workflow vs the BASE environment, one glance. */
function EnvMatrixSection({
  env,
  workflowIds,
  environmentIds,
}: {
  env: string;
  workflowIds: string[];
  environmentIds: string[];
}) {
  const cells = useBundleMatrix(workflowIds, environmentIds);
  const knownEnv = environmentIds.includes(env);
  const otherEnvs = environmentIds.filter((id) => id !== env);
  const fingerprint = cellFingerprint(cells);
  // eslint-disable-next-line react-hooks/exhaustive-deps -- fingerprint stands in for cells
  const matrix = useMemo(() => deriveEnvMatrix(env, cells), [fingerprint, env]);

  if (otherEnvs.length === 0) return null; // single-environment projects have nothing to compare
  if (!knownEnv) {
    // A stale/hand-edited ?env= must not render every workflow as not-comparable against a
    // bogus base (codex) — same message the Environment drift section shows.
    return (
      <Section id="env-matrix" title={`Environment matrix · vs ${env}`}>
        <div className="panel">
          <span className="dim">
            Environment <span className="mono">{env}</span> is not declared in this project — pick
            one from the selector.
          </span>
        </div>
      </Section>
    );
  }

  return (
    <Section id="env-matrix" title={`Environment matrix · vs ${env}`}>
      <div className="panel">
        {!matrix.settled ? <Loading what="environment matrix" /> : null}
        {matrix.settled ? (
          <>
            {matrix.refreshing ? (
              <div className="hint">Comparing environments — states may still change.</div>
            ) : null}
            <table className="grid">
              <thead>
                <tr>
                  <th>Workflow</th>
                  {otherEnvs.map((other) => (
                    <th key={other}>
                      <Mono>{other}</Mono>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {matrix.rows.map((row) => (
                  <tr key={row.workflowId}>
                    <td>
                      {/* #815: the id always links to the workflow detail — a row whose cells
                          are all pending/unresolvable (a new or actively drifting workflow,
                          exactly the one being investigated) must not be a dead end. */}
                      <a
                        href={`#/workflows/${encodeURIComponent(row.workflowId)}?env=${encodeURIComponent(env)}`}
                        title={`${row.workflowId} in ${env}`}
                      >
                        <Mono>{row.workflowId}</Mono>
                      </a>
                    </td>
                    {row.cells.map((cell) => {
                      const badge = matrixBadge(cell);
                      const linkable = row.comparable && cell.state !== "pending" && cell.state !== "unresolvable";
                      return (
                        <td key={cell.env}>
                          {linkable ? (
                            <a
                              href={`#/workflows/${encodeURIComponent(row.workflowId)}/diff?left=${encodeURIComponent(env)}&right=${encodeURIComponent(cell.env)}`}
                              title={`Diff ${env} vs ${cell.env}`}
                            >
                              <Badge kind={badge.kind}>{badge.label}</Badge>
                            </a>
                          ) : (
                            <Badge kind={badge.kind}>{badge.label}</Badge>
                          )}
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
            {matrix.notes.length > 0 ? <div className="hint">Not comparable: {matrix.notes.join("; ")}</div> : null}
          </>
        ) : null}
      </div>
    </Section>
  );
}

function NoDrift({ what }: { what: string }) {
  return (
    <div className="row">
      <Badge kind="ok">no drift</Badge>
      <span className="dim">{what}</span>
    </div>
  );
}

function PlanDriftBody({ rows }: { rows: Insight[] }) {
  return rows.length > 0 ? (
    <InsightList insights={rows} />
  ) : (
    <NoDrift what="Every approved plan matches the current resolution, and every workflow has one." />
  );
}

/** Approved plans vs the current resolution, plus coverage gaps. */
function PlanDriftSection({
  workflowIds,
  project,
}: {
  workflowIds: string[];
  project?: ProjectSummary;
}) {
  const deployments = useDeployments();
  return (
    <Section id="plan-drift" title="Plan drift">
      <div className="panel">
        {deployments.isPending ? (
          <Loading what="deployment plans" />
        ) : deployments.isError ? (
          isUnsupportedRuntime(deployments.error) ? (
            <ResolutionUnavailable what="Plan drift" />
          ) : (
            <ErrorPanel message={errorMessage(deployments.error) ?? "no data"} />
          )
        ) : (
          <PlanDriftBody rows={derivePlanDrift(deployments.data, workflowIds, project)} />
        )}
        <div className="hint">
          From approved deployment plans (live verification against the current resolution).
        </div>
      </div>
    </Section>
  );
}

/**
 * Critical bundle differences between the selected environment and each
 * other environment — version and governance drift only; expected divergence
 * never reaches this feed (the diff engine classifies it below critical).
 */
function EnvironmentDriftSection({
  env,
  workflowIds,
  environmentIds,
}: {
  env: string;
  workflowIds: string[];
  environmentIds: string[];
}) {
  const cells = useBundleMatrix(workflowIds, environmentIds);
  const knownEnv = environmentIds.includes(env);
  const otherEnvs = environmentIds.filter((id) => id !== env);
  const settled = cells.every((cell) => !cell.pending);

  // A fingerprint keyed on fetch state memoizes the O(workflows ×
  // environments) diff pass: it reruns when any cell settles or refetches
  // (dataUpdatedAt moves on every successful fetch, so policy/secret changes
  // invalidate too), not on every unrelated re-render.
  const fingerprint = cellFingerprint(cells);
  const derived = useMemo(() => {
    if (!knownEnv || otherEnvs.length === 0 || !settled) return null;
    // The shared derivation (#721 F5) — the executive persona rollup counts these same criticals.
    return deriveEnvironmentDriftFeed(env, otherEnvs, workflowIds, cells);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fingerprint, env, knownEnv, settled]);

  let body;
  if (!knownEnv) {
    body = (
      <span className="dim">
        Environment <span className="mono">{env}</span> is not declared in this project — pick one
        from the selector.
      </span>
    );
  } else if (otherEnvs.length === 0) {
    body = <span className="dim">Only one environment is declared — nothing to compare.</span>;
  } else if (!settled || !derived) {
    body = <Loading what={`bundles across ${environmentIds.length} environments`} />;
  } else {
    body = (
      <>
        {derived.rows.length > 0 ? (
          <InsightList insights={derived.rows} />
        ) : (
          <NoDrift
            what={`No version or governance drift between ${env} and ${otherEnvs.join(", ")}.`}
          />
        )}
        {derived.notes.length > 0 ? (
          <div className="hint">
            Not comparable: {derived.notes.join(" · ")} — resolve the environment (secrets,
            variables) to include it.
          </div>
        ) : null}
      </>
    );
  }

  return (
    <Section id="environment-drift" title={`Environment drift · vs ${env}`}>
      <div className="panel">
        {body}
        <div className="hint">
          Critical bundle differences only (spec digest, policy hash, secret configuration);
          provider/component divergence stays on each workflow&apos;s pairwise diff.
        </div>
      </div>
    </Section>
  );
}

/**
 * Prompt-registry drift, fanned out per workflow behind one explicit check —
 * the same on-demand cached probe the workflow page uses, so results are
 * shared with it.
 */
function PromptDriftSection({ env, workflowIds }: { env: string; workflowIds: string[] }) {
  const [requestedFor, setRequestedFor] = useState<string | null>(null);
  const requested = requestedFor === env;

  return (
    <Section id="prompt-drift" title="Prompt drift">
      <div className="panel">
        {!requested ? (
          <div className="row">
            <button type="button" onClick={() => setRequestedFor(env)}>
              Check prompt drift
            </button>
            <span className="dim">
              Compares each workflow&apos;s registry versions against its last run&apos;s manifest.
            </span>
          </div>
        ) : (
          workflowIds.map((workflowId) => (
            <PromptDriftRow key={workflowId} workflowId={workflowId} env={env} />
          ))
        )}
      </div>
    </Section>
  );
}

function PromptDriftRow({ workflowId, env }: { workflowId: string; env: string }) {
  const probe = usePromptStatus(workflowId, env, true);
  const rows = probe.data ? derivePromptDrift(workflowId, env, probe.data) : [];
  const prompts = probe.data?.prompts ?? [];
  // "unknown" (no last-run manifest to compare against) is a neutral state,
  // never a green one — only prompts positively in_sync earn "in sync".
  const unknown = prompts.filter(
    (prompt) => prompt.status !== "drift" && prompt.status !== "in_sync",
  ).length;
  return (
    <div style={{ marginBottom: 6 }}>
      <div className="row">
        <Mono>{workflowId}</Mono>
        {probe.isPending ? (
          <span className="faint">checking…</span>
        ) : probe.isError ? (
          <>
            <span className="badge badge-neutral" title={errorMessage(probe.error) ?? ""}>
              unavailable
            </span>
            <button
              type="button"
              className="link-button"
              onClick={() => void probe.refetch()}
              disabled={probe.isFetching}
            >
              retry
            </button>
          </>
        ) : (
          <>
            {rows.length > 0 ? <Badge kind="warning">{`${rows.length} drifting`}</Badge> : null}
            {unknown > 0 ? (
              <span
                className="badge badge-neutral"
                title="No last-run manifest to compare against — run the workflow to establish a baseline."
              >
                {unknown} unknown
              </span>
            ) : null}
            {rows.length === 0 && unknown === 0 ? (
              prompts.length > 0 ? (
                <Badge kind="ok">in sync</Badge>
              ) : (
                <span className="faint">no prompts</span>
              )
            ) : null}
          </>
        )}
      </div>
      {rows.length > 0 ? <InsightList insights={rows} /> : null}
    </div>
  );
}

/**
 * The shared body for the two Temporal-tier drift classes (#577 §1): ranked rows, then the
 * outage-honest resolution via {@link temporalDriftOutage} (pure, unit-tested). An empty class is
 * only good news when EVERY workflow's Temporal read was readable: a TOTAL outage renders the loud
 * "unknown" state, and a PARTIAL outage with zero rows renders a neutral "incomplete" state naming
 * the unreadable workflows — both visually distinct from the green all-clear, which only a fully
 * readable empty feed earns. Mirrors the Runs surface's outage-honest states over the executions
 * fan-out.
 */
function TemporalDriftBody({
  feed,
  total,
  noDrift,
  unknownLabel,
  unknownAll,
  unavailableNote,
}: {
  feed: TemporalDriftFeed;
  total: number;
  noDrift: string;
  unknownLabel: string;
  unknownAll: string;
  unavailableNote: string;
}) {
  const outage = temporalDriftOutage(feed, total);
  return (
    <>
      {feed.rows.length > 0 ? (
        <InsightList insights={feed.rows} />
      ) : outage.kind === "total" ? (
        <div className="row">
          <Badge kind="neutral">{unknownLabel}</Badge>
          <span className="dim">{unknownAll}</span>
        </div>
      ) : outage.kind === "partial" ? (
        <div className="row">
          <Badge kind="neutral">incomplete</Badge>
          <span className="dim">
            No drift among the readable workflows, but drift is unknown for{" "}
            {outage.unavailable.length} of {total} — see below.
          </span>
        </div>
      ) : (
        <NoDrift what={noDrift} />
      )}
      {outage.kind === "partial" || (outage.kind === "total" && feed.rows.length > 0) ? (
        <div className="hint">
          {unavailableNote}: {feed.unavailable.join(" · ")}.
        </div>
      ) : null}
    </>
  );
}

/**
 * Version drain (#577 §1): executions still running on old versioned workflow types, per workflow,
 * over the cross-version drain matrix. Behind an explicit check like the prompt-drift section: the
 * cross-version drain read is Temporal-tier, and a control plane whose Temporal address is
 * routable-but-dead answers each read only at its bound (#581) — firing that fan-out on every page
 * load would occupy the browser's connection budget and starve the other sections' resolution-tier
 * reads. Once checked it degrades LOUDLY when Temporal is unreachable, never an empty all-clear.
 */
function VersionDrainSection({ env, workflowIds }: { env: string; workflowIds: string[] }) {
  const [requestedFor, setRequestedFor] = useState<string | null>(null);
  const requested = requestedFor === env;
  const cells = useVersionsMatrix(workflowIds, env, requested);
  const settled = requested && cells.every((cell) => !cell.pending);
  // The shared fingerprint template (#721 F8) — same memo convention as every other cell matrix.
  const fingerprint = cellFingerprint(cells);
  // eslint-disable-next-line react-hooks/exhaustive-deps -- fingerprint stands in for cells
  const feed = useMemo(() => deriveDrainDrift(env, cells), [fingerprint, env]);

  return (
    <Section id="version-drain" title="Version drain">
      <div className="panel">
        {!requested ? (
          <div className="row">
            <button type="button" onClick={() => setRequestedFor(env)}>
              Check version drain
            </button>
            <span className="dim">
              Reads each workflow&apos;s cross-version drain status from Temporal (a bounded probe,
              #586).
            </span>
          </div>
        ) : !settled ? (
          <Loading what={`cross-version drain across ${workflowIds.length} workflows`} />
        ) : (
          <TemporalDriftBody
            feed={feed}
            total={cells.length}
            noDrift="No executions are running on old versioned types."
            unknownLabel="drain status unknown"
            unknownAll="Temporal was unreachable for every workflow — old-version drain status is unknown in this environment, not clear."
            unavailableNote="Temporal unreachable — drain status unknown for"
          />
        )}
        <div className="hint">
          Runs still executing on old versioned workflow types. Drain an old version before
          decommissioning it.
        </div>
      </div>
    </Section>
  );
}

/**
 * Runtime-pin skew (#577 §1): the provable graph-changed case, per workflow — mutating operations
 * pinned to a spec digest that differs from the current resolution. The pin is read from a
 * representative execution's status (a two-stage Temporal-tier read), so this too is behind an
 * explicit check (see {@link VersionDrainSection}). Reuses the run inspector's #592 skew derivation
 * verbatim, including the caveat that a matching digest is not a freshness verdict. The bundle spec
 * digest comes from the resolved bundle for this environment (resolution-tier, cache-shared with
 * the matrix above).
 */
function PinSkewSection({ env, workflowIds }: { env: string; workflowIds: string[] }) {
  const [requestedFor, setRequestedFor] = useState<string | null>(null);
  const requested = requestedFor === env;
  const pins = useRuntimePinMatrix(workflowIds, env, requested);
  const bundles = useBundleMatrix(workflowIds, [env]);
  // The shared fingerprint template (#721 F8) covers both matrices — the pin cells key on their
  // `unavailable` state, the bundle cells on `error`/`fetching`.
  const pinFingerprint = cellFingerprint(pins);
  const bundleFingerprint = cellFingerprint(bundles);
  const settled =
    requested && pins.every((cell) => !cell.pending) && bundles.every((cell) => !cell.pending);

  // BOTH sides of the comparison feed the derivation: the current spec digest when the bundle
  // resolved, or the bundle read's ERROR — a workflow whose resolution read failed has UNKNOWN
  // skew (it joins the unavailable list), never a silent no-skew.
  const resolutionByWorkflow = useMemo(
    () =>
      new Map<string, PinSkewResolution>(
        bundles.map((cell) => [
          cell.workflowId,
          { specDigest: cell.bundle?.workflow.spec_digest, error: cell.error },
        ]),
      ),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- bundleFingerprint stands in for bundles
    [bundleFingerprint],
  );
  const feed = useMemo(
    () => derivePinSkewDrift(env, pins, resolutionByWorkflow),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- pinFingerprint stands in for pins
    [pinFingerprint, resolutionByWorkflow, env],
  );

  return (
    <Section id="pin-skew" title="Runtime-pin skew">
      <div className="panel">
        {!requested ? (
          <div className="row">
            <button type="button" onClick={() => setRequestedFor(env)}>
              Check runtime-pin skew
            </button>
            <span className="dim">
              Reads each workflow&apos;s runtime pin from a recent execution and compares it to the
              current resolution.
            </span>
          </div>
        ) : !settled ? (
          <Loading what={`runtime pins across ${workflowIds.length} workflows`} />
        ) : (
          <TemporalDriftBody
            feed={feed}
            total={pins.length}
            noDrift="No workflow's runtime pin resolves to an older graph than the current bundle."
            unknownLabel="pin skew unknown"
            unknownAll="Temporal was unreachable for every workflow — runtime-pin skew is unknown in this environment, not clear."
            unavailableNote="Temporal unreachable — pin skew unknown for"
          />
        )}
        <div className="hint">
          Mutating operations are bound to the runtime pinned at first use; a differing spec digest
          proves the workflow graph moved since. A matching digest is not a freshness verdict
          (config/policy edits don&apos;t move it) — repin from the run inspector to re-resolve.
        </div>
      </div>
    </Section>
  );
}
