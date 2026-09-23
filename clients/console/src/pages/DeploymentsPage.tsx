import type { ReactNode } from "react";
import { useState } from "react";
import { useNavigate } from "@tanstack/react-router";

import type {
  Capabilities, ProjectSummary, DeploymentEntry } from "../api";
import { isUnsupportedRuntime } from "../api";
import { Badge, CopyBlock, ErrorPanel, ExtLink, KV, Loading, Mono, Section, ResolutionUnavailable, ShortDigest, SourceLinks } from "../components";
import { latestPlanCoverage, workflowsWithoutPlan } from "../deploymentCoverage";
import { middleTruncate } from "../format";
import {
  derivePlanPr,
  planProvenanceById,
  PR_NOT_MERGED_TOOLTIP,
  PR_URL_NOT_LINKED_TOOLTIP,
  type PlanPrLink,
} from "../githubDrift";
import { definitionSourceLinks, githubCommitUrl } from "../links";
import { isDigestPinned, planCommand } from "../planCommand";
import { errorMessage, useDeployments, useGithubProvenance } from "../queries";

/**
 * Read-only deployment plans (#253): the in-repo, content-hashed pins that a
 * merged GitHub PR approves. Promotion stays a CLI step the operator runs;
 * the console never mutates a plan or emits artifacts — it shows what is
 * approved, whether it still matches the resolved project, and the exact
 * command to promote it.
 */
export function DeploymentsPage({
  workflowIds = [],
  environmentIds = [],
  policyIds = [],
  manifestPath = "typeflux.project.yaml",
  project,
  capabilities,
}: {
  workflowIds?: string[];
  environmentIds?: string[];
  policyIds?: string[];
  manifestPath?: string;
  project?: ProjectSummary;
  capabilities?: Capabilities;
}) {
  const state = useDeployments();
  // Plan → approving PR (#727 §6): ONE project-level provenance read serves every plan card's PR
  // link (no per-card N+1). Capability-gated — when the control plane doesn't advertise
  // `github_provenance`, nothing is fetched and the cards render without a PR row (the surface is
  // simply absent, honestly, rather than a fabricated "unknown" per card).
  const prSupported = capabilities?.github_provenance === true;
  const provenance = useGithubProvenance(prSupported);
  const prByPlanId = planProvenanceById(provenance.data);
  // F1: the PR cell's verdict is only trustworthy once the provenance query has SETTLED. While it
  // is pending (or errored) `derivePlanPr(undefined)` would render a confident "no PR provenance"
  // that flips once the read lands — a false negative. Gate on the query state (settled-discipline,
  // cf. #722 F2): pending → a dim placeholder, error → an honest unavailable note, and only a
  // read-in-full success renders `derivePlanPr` verdicts (threaded with the envelope's `partial`
  // so a degraded/capped pr-null is not mislabelled — F3).
  const prCellFor = (planId: string): PlanPrCellState => {
    if (provenance.isPending) return { kind: "pending" };
    if (provenance.isError) return { kind: "error" };
    return {
      kind: "ready",
      link: derivePlanPr(prByPlanId.get(planId), provenance.data?.partial.github ?? "ok"),
    };
  };

  if (state.isPending) return <Loading what="deployment plans" />;
  if (state.isError || !state.data) {
    if (isUnsupportedRuntime(state.error)) {
      return <ResolutionUnavailable what="The deployments surface" />;
    }
    return <ErrorPanel message={errorMessage(state.error) ?? "no data"} />;
  }
  const entries = state.data;

  const generator = (
    <PlanGenerator
      workflowIds={workflowIds}
      environmentIds={environmentIds}
      policyIds={policyIds}
      manifestPath={manifestPath}
    />
  );

  if (entries.length === 0) {
    return (
      <>
        <Section id="deployments" title="Deployment plans">
          <div className="panel">
            <span className="dim">No deployment plans yet.</span>
            <div className="hint">
              Write one below, then open a PR — a merged review is the approval.
            </div>
            {workflowIds.length > 0 ? (
              <div className="hint">
                No approved plan for: <Mono>{workflowIds.join(", ")}</Mono>.
              </div>
            ) : null}
          </div>
        </Section>
        {generator}
      </>
    );
  }

  // Group by workflow so an operator scans one workflow's plans together.
  const byWorkflow = new Map<string, DeploymentEntry[]>();
  for (const entry of entries) {
    const key = entry.plan.identity.workflow_id;
    (byWorkflow.get(key) ?? byWorkflow.set(key, []).get(key)!).push(entry);
  }

  return (
    <>
      <PlanCoverage entries={entries} workflowIds={workflowIds} />
      {generator}
      {[...byWorkflow.entries()].map(([workflowId, group]) => (
        <Section key={workflowId} id={`deployments-${workflowId}`} title={`${workflowId} — plans`}>
          <div className="panel">
            {group.map((entry) => (
              <PlanCard
                key={entry.plan_id}
                entry={entry}
                project={project}
                prSupported={prSupported}
                prState={prCellFor(entry.plan_id)}
              />
            ))}
          </div>
        </Section>
      ))}
    </>
  );
}

/**
 * The workflow-centric coverage view (#294): the latest approved plan per
 * (workflow, environment) — ready vs drifted from the current resolution —
 * plus the workflows with no approved plan at all.
 */
function PlanCoverage({
  entries,
  workflowIds,
}: {
  entries: DeploymentEntry[];
  workflowIds: string[];
}) {
  const navigate = useNavigate();
  const rows = latestPlanCoverage(entries);
  const uncovered = workflowsWithoutPlan(entries, workflowIds);

  return (
    <Section id="plan-coverage" title="Plan coverage">
      <div className="panel">
        <table className="grid">
          <thead>
            <tr>
              <th>Workflow</th>
              <th>Environment</th>
              <th>Versioned type</th>
              <th>State</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr
                key={row.planId}
                className="clickable"
                onClick={() =>
                  void navigate({
                    to: "/workflows/$workflowId",
                    params: { workflowId: row.workflowId },
                    search: { env: row.environmentId },
                  })
                }
              >
                <td>
                  <Mono>{row.workflowId}</Mono>
                </td>
                <td className="faint">{row.environmentId}</td>
                <td>
                  <ShortDigest value={row.workflowType} />
                </td>
                <td>
                  {row.state === "ready" ? (
                    <Badge kind="ok">ready</Badge>
                  ) : (
                    <span
                      className="badge badge-warning"
                      title={row.mismatches
                        .map((m) => `${m.path}: ${format(m.plan_value)} → ${format(m.current_value)}`)
                        .join("\n")}
                    >
                      drifted
                    </span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {uncovered.length > 0 ? (
          <div className="hint">
            No approved plan: <Mono>{uncovered.join(", ")}</Mono>
          </div>
        ) : null}
        <div className="hint">
          A drifted plan no longer matches the current resolution — promotion fails closed until a
          fresh plan is approved.
        </div>
      </div>
    </Section>
  );
}

/** The PR cell's state, gated on the ONE provenance query so a pending/errored read never renders a
 * confident (and later-flipping) "no PR provenance" verdict (F1). `ready` carries the settled,
 * partial-aware {@link PlanPrLink}. */
type PlanPrCellState =
  | { kind: "pending" }
  | { kind: "error" }
  | { kind: "ready"; link: PlanPrLink };

function PlanCard({
  entry,
  project,
  prSupported = false,
  prState,
}: {
  entry: DeploymentEntry;
  project?: ProjectSummary;
  /** Whether the control plane advertises github_provenance — gates the PR row entirely. */
  prSupported?: boolean;
  /** This plan's PR cell state from the ONE provenance query (pending/error/settled), so the row
   * stays honest while the read is in flight (F1). */
  prState?: PlanPrCellState;
}) {
  const { plan, verification, promote_command } = entry;
  const { identity, policy, deployment } = plan;
  const drifted = !verification.ok;
  const code = identity.code;

  return (
    <div className="plan-card">
      <div className="row plan-card-head">
        <span className="mono">{identity.workflow_type}</span>
        <span className="faint">{identity.environment_id}</span>
        <SourceLinks {...definitionSourceLinks(project, entry.path)} />
        {drifted ? (
          <span className="badge badge-warning" title="Resolution has moved since this plan was approved">
            drifted
          </span>
        ) : (
          <span className="badge badge-ok" title="Matches the current resolution">
            ready to promote
          </span>
        )}
        {deployment.preflight.ok ? (
          <Badge kind="ok">preflight ok</Badge>
        ) : (
          <Badge kind="warning">preflight failed</Badge>
        )}
        {deployment.image_digest_pinned ? (
          <Badge kind="ok">digest-pinned</Badge>
        ) : (
          <Badge kind="warning">mutable image</Badge>
        )}
      </div>

      <KV
        rows={[
          ["Plan", <ShortDigest key="p" value={plan.plan_hash} />],
          ["Spec digest", <ShortDigest key="s" value={identity.spec_digest} />],
          [
            "Policy",
            <span key="pol">
              <Mono>{policy.applied_policy_ids.join(", ") || "—"}</Mono>{" "}
              <span className="faint">
                (<ShortDigest value={policy.policy_hash} />)
              </span>
            </span>,
          ],
          ["Image", <Mono key="i">{deployment.image}</Mono>],
          [
            "Code",
            code ? (
              code.repo_url ? (
                <ExtLink key="c" href={githubCommitUrl(code.repo_url, code.sha)}>
                  <ShortDigest value={code.sha} />
                </ExtLink>
              ) : (
                <ShortDigest key="c" value={code.sha} />
              )
            ) : (
              <span className="faint">not a checkout</span>
            ),
          ],
          // Approving PR (#727 §6): the review record every approved plan is one click from, from
          // the single provenance read. Only when the control plane offers the surface — otherwise
          // the row is absent (no fabricated "unknown"), matching the capability gate on the Drift
          // page's GitHub section.
          ...(prSupported && prState
            ? ([
                ["Approving PR", <PlanPrCell key="pr" state={prState} />],
              ] as Array<[string, ReactNode]>)
            : []),
        ]}
      />

      {drifted ? (
        <div className="plan-drift">
          <div className="hint">
            This plan no longer matches the resolved project — promotion fails closed until a fresh
            plan is approved.
          </div>
          <ul className="insight-list">
            {verification.mismatches.map((mismatch) => (
              <li key={mismatch.path}>
                <span>
                  <Badge kind="warning">drift</Badge>
                </span>
                <span>
                  <Mono>{mismatch.path}</Mono>
                  <div className="insight-detail">
                    plan <Mono>{format(mismatch.plan_value)}</Mono> → current{" "}
                    <Mono>{format(mismatch.current_value)}</Mono>
                  </div>
                </span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      <div className="plan-promote">
        <a
          className="crumb"
          href={`#/workflows/${identity.workflow_id}?env=${identity.environment_id}`}
        >
          open workflow ↗
        </a>
        <div className="hint">Approve by merging the plan file's PR; then promote:</div>
        <CopyBlock command={promote_command} />
      </div>
    </div>
  );
}

/**
 * Console-assisted plan generation (#290): guide the operator from a
 * workflow/environment/image/policy selection to the exact `deploy --plan-out`
 * command. The console writes nothing — it only assembles the command; the
 * operator runs it and approval stays the merged plan-file PR.
 */
function PlanGenerator({
  workflowIds,
  environmentIds,
  policyIds,
  manifestPath,
}: {
  workflowIds: string[];
  environmentIds: string[];
  policyIds: string[];
  manifestPath: string;
}) {
  const [workflow, setWorkflow] = useState(workflowIds[0] ?? "");
  const [environment, setEnvironment] = useState(environmentIds[0] ?? "");
  const [policies, setPolicies] = useState<string[]>([]);
  const [image, setImage] = useState("");

  if (workflowIds.length === 0 || environmentIds.length === 0) return null;

  // Clamp selections to the current project's options: a stale workflow/env id
  // (kept in state after the props change) must never reach the command.
  const selectedWorkflow = workflowIds.includes(workflow) ? workflow : workflowIds[0];
  const selectedEnvironment = environmentIds.includes(environment)
    ? environment
    : environmentIds[0];
  const selectedPolicies = policies.filter((p) => policyIds.includes(p));

  const mutable = image.trim() !== "" && !isDigestPinned(image);
  const command = planCommand({
    manifest: manifestPath,
    workflow: selectedWorkflow,
    environment: selectedEnvironment,
    policies: selectedPolicies,
    image,
  });

  return (
    <Section id="plan-generator" title="Generate a plan">
      <div className="panel">
        <div className="plan-gen-fields">
          <label>
            <span className="faint">Workflow</span>
            <select value={selectedWorkflow} onChange={(e) => setWorkflow(e.target.value)}>
              {workflowIds.map((id) => (
                <option key={id} value={id}>
                  {id}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span className="faint">Environment</span>
            <select value={selectedEnvironment} onChange={(e) => setEnvironment(e.target.value)}>
              {environmentIds.map((id) => (
                <option key={id} value={id}>
                  {id}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span className="faint">Image</span>
            <input
              type="text"
              value={image}
              placeholder="ghcr.io/org/worker@sha256:…"
              onChange={(e) => setImage(e.target.value)}
            />
          </label>
        </div>
        {policyIds.length > 0 ? (
          <div className="plan-gen-policies">
            <span className="faint">Policies</span>
            {policyIds.map((id) => (
              <label key={id} className="plan-gen-policy">
                <input
                  type="checkbox"
                  checked={policies.includes(id)}
                  onChange={(e) =>
                    setPolicies((prev) =>
                      e.target.checked ? [...prev, id] : prev.filter((p) => p !== id),
                    )
                  }
                />
                <Mono>{id}</Mono>
              </label>
            ))}
          </div>
        ) : null}
        {mutable ? (
          <div className="row">
            <Badge kind="warning">mutable image</Badge>
            <span className="dim">
              Not pinned to a digest — the deployment won't be reproducible. Pin to{" "}
              <Mono>…@sha256:&lt;digest&gt;</Mono>.
            </span>
          </div>
        ) : null}
        <CopyBlock command={command} />
        <div className="hint">
          Run this to write the plan file, then open a PR — a merged review is the approval. The
          plan only writes if the workflow resolves and admits in this environment; it records the
          digest-pin and policy hash. The console writes nothing.
        </div>
      </div>
    </Section>
  );
}

function format(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value !== "string") return String(value);
  return middleTruncate(value);
}

/**
 * The approving-PR hand-off for one plan card (#727 §6): the PR number linked to its GitHub URL
 * plus its merged date when the provenance read resolved one; a dim placeholder while the ONE
 * provenance query is still in flight and an honest note when it errored (F1 — never a confident
 * "no PR provenance" verdict that flips once the read lands); otherwise the explicit reason from
 * `derivePlanPr` (untracked plan file, degraded/capped lookup, or provenance unavailable). Never a
 * fabricated link, and every tooltip comes from the tested `githubDrift` layer.
 */
function PlanPrCell({ state }: { state: PlanPrCellState }) {
  if (state.kind === "pending") {
    return <span className="faint">resolving PR…</span>;
  }
  if (state.kind === "error") {
    return (
      <span
        className="faint"
        title="The GitHub-provenance read failed, so the approving PR could not be resolved this cycle — retry once the control plane is reachable."
      >
        PR provenance unavailable
      </span>
    );
  }
  const link = state.link;
  if (link.kind === "pr") {
    return (
      <span>
        {link.url ? (
          <ExtLink href={link.url}>#{link.number}</ExtLink>
        ) : (
          <span className="mono" title={PR_URL_NOT_LINKED_TOOLTIP}>
            #{link.number}
          </span>
        )}
        {link.mergedAt ? (
          <span className="faint"> · merged {formatMergedAt(link.mergedAt)}</span>
        ) : (
          <span className="faint" title={PR_NOT_MERGED_TOOLTIP}>
            {" "}
            · not merged
          </span>
        )}
      </span>
    );
  }
  return (
    <span className="faint" title={link.tooltip}>
      no PR provenance ({link.reason})
    </span>
  );
}

/** A merged-at timestamp as a compact local date; degrades to the raw ISO value. */
function formatMergedAt(iso: string): string {
  const parsed = new Date(iso);
  return Number.isNaN(parsed.getTime()) ? iso : parsed.toLocaleDateString();
}
