import { useMemo, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";

import type { Capabilities, EnvironmentSummary, ProjectSummary, WorkflowSummary } from "../api";
import { activeProject, currentProject, fetchProjects, refreshProject, isUnsupportedRuntime } from "../api";
import { Badge, ErrorPanel, InsightList, Loading, Mono, Section, SeverityBadge, ShortDigest, ResolutionUnavailable, SourceLinks } from "../components";
import { staleAckInsights } from "../annotationsFeed";
import { deriveCrossEnvInsights } from "../crossEnv";
import { githubChangeInsights } from "../githubDrift";
import { manifestSourceLinks } from "../links";
import { deriveBundleInsights, issueSeverity, sortInsights, validationIssueSourceLinks } from "../insights";
import { accessDeniedHint } from "../ops";
import { cellFingerprint, errorMessage, useAnnotations, useBundleMatrix, useGithubProvenance, useValidation, useWorkflowBundles } from "../queries";

export function OverviewPage({
  env,
  workflows,
  environments,
  projects = [],
  onSwitchProject,
  canRefreshProject = true,
  project,
  capabilities,
}: {
  env: string;
  workflows: WorkflowSummary[];
  environments: EnvironmentSummary[];
  projects?: ProjectSummary[];
  onSwitchProject?: (projectId: string) => void;
  canRefreshProject?: boolean;
  /** The active project's git provenance — anchors the change-correlated GitHub insight's compare link. */
  project?: ProjectSummary;
  capabilities?: Capabilities;
}) {
  const queryClient = useQueryClient();
  const [refreshing, setRefreshing] = useState<string | null>(null);
  // Local copy so a refresh updates provenance in place — no full page reload.
  const [projectList, setProjectList] = useState<ProjectSummary[]>(projects);

  async function refresh(projectId: string): Promise<void> {
    setRefreshing(projectId);
    let refreshed = false;
    try {
      // The endpoint returns success or a recorded failure (never throws on a
      // git error); re-fetch the listing to show the new sha / failure inline.
      await refreshProject(projectId);
      refreshed = true;
      setProjectList(await fetchProjects());
    } catch {
      // A transport error — leave the prior listing shown.
    } finally {
      if (refreshed) {
        // A server-side refresh re-clones the project, so the served checkout sha — and thus the
        // HEAD-vs-served drift and every plan→PR link — has moved. The github-provenance query has
        // a deliberately generous 5-min staleTime, so without an explicit invalidation the Drift,
        // Overview, and Deployments surfaces would keep serving the PRE-refresh drift + compare
        // links (the very thing the operator refreshed to fix). Invalidated in `finally`, gated
        // only on the REFRESH having succeeded — a failed LISTING refetch must not skip it
        // (Bugbot): the server checkout already moved either way. Same queryClient pattern as
        // the start-workflow panel (panels/runs.tsx).
        void queryClient.invalidateQueries({ queryKey: ["github-provenance"] });
        // The annotations file is part of the re-cloned checkout too, so its cache (a generous
        // 5-min staleTime) must invalidate on the same refresh — else the acknowledged-collapse and
        // stale-ack warnings keep serving the pre-refresh ledger.
        void queryClient.invalidateQueries({ queryKey: ["annotations"] });
      }
      setRefreshing(null);
    }
  }

  const navigate = useNavigate();
  const report = useValidation(env);
  const workflowBundles = useWorkflowBundles(workflows, env);
  // Cross-environment splits (#604): the full workflow x environment matrix, cache-shared
  // with the Drift page and the selected-env fan-out above via bundleKey. The engine
  // derives nothing until the matrix settles, so a slow environment cannot flash a
  // false split; its unresolvable-env notes render as a hint below the feed.
  const matrixCells = useBundleMatrix(
    workflows.map((workflow) => workflow.id),
    environments.map((environment) => environment.id),
  );
  // Memoized on the shared cell fingerprint (#721 F8): unrelated local state changes must not
  // re-run the O(workflows x environments) derivation.
  const matrixFingerprint = cellFingerprint(matrixCells);
  // eslint-disable-next-line react-hooks/exhaustive-deps -- fingerprint stands in for cells
  const crossEnv = useMemo(() => deriveCrossEnvInsights(matrixCells), [matrixFingerprint]);

  // Change-correlated GitHub drift (#727 §7): one project-level provenance read, capability-gated,
  // feeding the "changed in GitHub but the served checkout hasn't picked it up" insight into the
  // Overview feed. Shares its cache entry with the Drift page and Deployments (same query key).
  const githubProvenance = useGithubProvenance(capabilities?.github_provenance === true);

  // The insight-acknowledgement annotations (#733): the same cached read InsightList uses for the
  // acknowledged-collapse, consumed here for the ONE project-level surface that owns stale-ack
  // warnings — so an expired ack raises its warning once (this feed), not once per InsightList mount
  // across the app.
  const annotations = useAnnotations();
  // Memoized per annotations payload AND per UTC calendar day: expiry is a day-granular verdict, so
  // the day string is the complete time dependency. Without it, a render after UTC midnight would
  // un-collapse matches in InsightList (which reads the clock each render) while this memo still
  // held yesterday's warnings until the annotations query refetched (Bugbot).
  const utcDay = new Date().toISOString().slice(0, 10);
  // eslint-disable-next-line react-hooks/exhaustive-deps -- utcDay stands in for the clock read
  const staleAcks = useMemo(
    () => staleAckInsights(annotations.data?.annotations ?? [], new Date()),
    [annotations.data, utcDay],
  );

  if (report.isPending || workflowBundles.loading) return <Loading what="project overview" />;
  // An unresolvable-runtime project keeps its PAGE: workflows/projects/environments are
  // pure-YAML reads the server still answers, and the per-row bundle badges already say
  // "unresolvable". Only the resolution-backed validation section degrades below (#621).
  const validationUnsupported = report.isError && isUnsupportedRuntime(report.error);
  if ((report.isError || !report.data) && !validationUnsupported) {
    return (
      <ErrorPanel
        message={errorMessage(report.error) ?? "no data"}
        hint="Is the control-plane API running?"
      />
    );
  }

  const bundles = workflowBundles.entries;
  const projectIssues = report.data;
  const issues = projectIssues?.issues ?? [];
  // The change-correlated GitHub insight's compare link is anchored to the active project's served
  // sha. Derive it from the LOCAL projectList (which a refresh updates in place, F2) rather than the
  // `project` prop snapshot the route passed at mount, so a post-refresh compare link bases on the
  // freshly-refreshed served checkout, not the stale one. Falls back to the prop pre-refresh (both
  // resolve to the same active project on first render).
  const activeGitProject = activeProject(projectList) ?? project;
  const insights = sortInsights([
    // Skip the GitHub insight while the provenance query is REFETCHING (post-refresh
    // invalidation): the refreshed projectList and the still-cached pre-refresh provenance are
    // different generations, and pairing a new served sha with an old remote HEAD builds a wrong
    // compare URL / lingering drift row (Bugbot). Both sides settle together on the next render.
    ...(githubProvenance.data && !githubProvenance.isFetching
      ? githubChangeInsights(githubProvenance.data, activeGitProject)
      : []),
    // Stale-ack warnings (#733): an expired acknowledgement surfaces as its own warning-severity
    // finding here — the single project-level feed — so it is loud once, not repeated across every
    // InsightList mount. The acknowledged-collapse itself is applied inside InsightList for free.
    ...staleAcks,
    ...crossEnv.insights,
    ...bundles.flatMap(({ workflow, bundle }) =>
      bundle ? deriveBundleInsights(workflow.id, env, bundle) : [],
    ),
  ]);

  return (
    <>
      {projectList.length > 1 ? (
        <Section id="projects" title="Projects">
          <div className="panel">
            <table className="grid">
              <thead>
                <tr>
                  <th>Project</th>
                  <th>Source</th>
                  <th>Refreshed</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {projectList.map((project) => {
                  const active =
                    currentProject() === project.id ||
                    (currentProject() === null && project.default);
                  return (
                    <tr key={project.id}>
                      <td>
                        <Mono>{project.name}</Mono>
                        {project.default ? <span className="faint"> (default)</span> : null}
                        <span className="faint"> · {project.runtime}</span>
                        {!project.resolvable ? (
                          <>
                            {" "}
                            <Badge kind="warning">not resolvable here</Badge>
                          </>
                        ) : null}
                        {project.available === false ? (
                          // A degraded registry entry (unloadable manifest / unreachable Git
                          // source) must never render like a healthy project (#621 slice 3).
                          <>
                            {" "}
                            <span className="badge badge-critical" title={project.detail ?? undefined}>
                              unavailable
                            </span>
                          </>
                        ) : null}
                      </td>
                      <td>
                        {project.source === "git" ? (
                          <span title={project.manifest_path}>
                            <Mono>{project.repo_url}</Mono>
                            <span className="faint"> @ {project.repo_ref}</span>
                            {project.repo_sha ? (
                              <>
                                {" "}
                                <ShortDigest value={project.repo_sha} />
                              </>
                            ) : null}{" "}
                            {/* The manifest itself is a source-of-truth file — link it at the
                                resolved sha (#718 §A); a git project without a resolvable sha/path
                                degrades loudly rather than dropping the affordance. */}
                            <SourceLinks {...manifestSourceLinks(project)} />
                          </span>
                        ) : (
                          <Mono>{project.manifest_path}</Mono>
                        )}
                      </td>
                      <td>
                        {project.source !== "git" ? (
                          <span className="faint">—</span>
                        ) : project.last_refresh ? (
                          <span
                            title={project.last_refresh.detail ?? undefined}
                            className="row"
                          >
                            <span
                              className={`badge ${
                                project.last_refresh.refreshed ? "badge-ok" : "badge-warning"
                              }`}
                              title={
                                project.last_refresh.refreshed
                                  ? "The last refresh of this Git-sourced project succeeded; the sha shown is current."
                                  : (project.last_refresh.detail ??
                                    "The last refresh failed; the previous good clone is still served.")
                              }
                            >
                              {project.last_refresh.refreshed ? "ok" : "failed"}
                            </span>
                            <span className="faint">{formatRefreshedAt(project.last_refresh.refreshed_at)}</span>
                          </span>
                        ) : (
                          <span className="faint">not yet</span>
                        )}
                      </td>
                      <td className="project-actions">
                        {project.source === "git" ? (
                          <button
                            type="button"
                            className="link-button"
                            onClick={() => refresh(project.id)}
                            disabled={refreshing === project.id || !canRefreshProject}
                            title={
                              canRefreshProject ? undefined : accessDeniedHint("refreshing projects")
                            }
                          >
                            {refreshing === project.id ? "refreshing…" : "refresh"}
                          </button>
                        ) : null}
                        {active ? (
                          <Badge kind="ok">active</Badge>
                        ) : (
                          <button
                            type="button"
                            className="link-button"
                            onClick={() => onSwitchProject?.(project.id)}
                          >
                            switch →
                          </button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            <div className="hint">
              Switching project reloads the console; every view rescopes to that project.
              Refreshing a Git-sourced project re-fetches its repository on the server.
            </div>
          </div>
        </Section>
      ) : null}
      <Section id="workflows" title={`Workflows · ${env}`}>
        <div className="panel">
          <table className="grid">
            <thead>
              <tr>
                <th>Workflow</th>
                <th>Versioned type</th>
                <th>Spec digest</th>
                <th>Policy</th>
                <th>Validation</th>
              </tr>
            </thead>
            <tbody>
              {bundles.map(({ workflow, bundle, error }) => (
                <tr
                  key={workflow.id}
                  className="clickable"
                  onClick={() =>
                    void navigate({
                      to: "/workflows/$workflowId",
                      params: { workflowId: workflow.id },
                      search: { env },
                    })
                  }
                >
                  <td>
                    <span className="mono">{workflow.id}</span>
                  </td>
                  <td>
                    {bundle ? (
                      // The human identity is workflow_name — under the ts-plan-argument
                      // binding the TYPE is the constant generic workflow and carries no
                      // identity (contracts/temporal-binding; #621 slice 3). Type on hover.
                      <span className="mono" title={bundle.workflow.workflow_type}>
                        {bundle.workflow.workflow_name}
                      </span>
                    ) : (
                      "—"
                    )}
                  </td>
                  <td>{bundle ? <ShortDigest value={bundle.workflow.spec_digest} /> : "—"}</td>
                  <td>
                    {bundle?.policy ? (
                      <ShortDigest value={bundle.policy.policy_hash || "(composition failed)"} />
                    ) : (
                      <Badge kind="neutral">none</Badge>
                    )}
                  </td>
                  <td>
                    {error ? (
                      <span className="badge badge-critical" title={error}>
                        unresolvable
                      </span>
                    ) : bundle?.validation.ok ? (
                      <Badge kind="ok" title="This workflow resolves and validates cleanly in this environment.">
                        ok
                      </Badge>
                    ) : (
                      <span
                        className="badge badge-warning"
                        title="Validation found issues for this workflow in this environment; open the workflow for detail."
                      >
                        {bundle?.validation.issues?.length ?? 0} issues
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Section>

      <Section id="insights" title="Insights">
        <div className="panel">
          {insights.length === 0 && crossEnv.pending ? (
            // Never show the ok/No-findings state while the matrix is in flight — an
            // empty cross-environment result is not a verdict yet (codex).
            <Loading what="cross-environment comparison" />
          ) : (
            <InsightList insights={insights} />
          )}
          {insights.length > 0 && crossEnv.pending ? (
            <div className="hint">Comparing environments — cross-environment findings may still appear.</div>
          ) : null}
          {crossEnv.notes.length > 0 ? (
            <div className="hint">Not comparable: {crossEnv.notes.join("; ")}</div>
          ) : null}
        </div>
      </Section>

      {validationUnsupported ? (
        <Section id="project-issues" title="Project validation issues">
          <ResolutionUnavailable what="The validation report" />
        </Section>
      ) : null}
      {issues.length > 0 ? (
        <Section id="project-issues" title="Project validation issues">
          <div className="panel">
            <table className="grid">
              <thead>
                <tr>
                  <th>Severity</th>
                  <th>Code</th>
                  <th>Message</th>
                  <th>Reference</th>
                  <th>Source</th>
                </tr>
              </thead>
              <tbody>
                {issues.map((issue, index) => {
                  // File-class failures (missing/unloadable/malformed workflow YAML) get the same
                  // GitHub source-link affordance every other feed carries (#577 §10): the manifest
                  // entry that declares the workflow and the expected workflow YAML. Per the
                  // app-wide SourceLinks convention the affordance degrades LOUDLY — a file-class
                  // issue without repo provenance renders the explicit "source link unavailable"
                  // note (via the empty pair), while a non-file-class issue (which never carries
                  // source links) renders a plain dash, so the two blanks are distinguishable.
                  const source = validationIssueSourceLinks(issue, activeGitProject, workflows);
                  return (
                    <tr key={`${issue.code}:${index}`}>
                      <td>
                        <SeverityBadge severity={issueSeverity(issue)} />
                      </td>
                      <td>
                        <span className="mono">{issue.code}</span>
                      </td>
                      <td>{issue.message}</td>
                      <td>
                        <span className="mono dim">{issue.reference ?? ""}</span>
                      </td>
                      <td>
                        {source === undefined ? (
                          <span className="dim">—</span>
                        ) : (
                          <>
                            {source.workflowFile?.blob !== undefined ? (
                              <div className="row">
                                <span className="dim">workflow</span>
                                <SourceLinks {...source.workflowFile} />
                              </div>
                            ) : null}
                            <div className="row">
                              <span className="dim">manifest</span>
                              <SourceLinks {...source.manifest} />
                            </div>
                          </>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </Section>
      ) : null}
    </>
  );
}

/** Compact local time for a refresh timestamp; degrades to the raw value. */
function formatRefreshedAt(iso: string | null | undefined): string {
  if (!iso) return "";
  const parsed = new Date(iso);
  return Number.isNaN(parsed.getTime()) ? iso : parsed.toLocaleString();
}
