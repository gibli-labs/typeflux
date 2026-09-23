/**
 * Workflow panels (#580): the sections of the workflow detail page as
 * independent, presentational components. Each renders its own <Section>
 * (so `?section=` anchors keep working) and takes its data slice via props;
 * the two probe panels own their on-demand cached queries because the data
 * is theirs alone. Pages — and the Phase 2 question-oriented surfaces —
 * compose these; panels never import from pages/.
 */

import { type ReactNode, useState } from "react";
import type { UseQueryResult } from "@tanstack/react-query";

import type { Bundle, Catalog } from "../api";
import {
  Badge,
  CopyBlock,
  ExtLink,
  JsonView,
  KV,
  Loading,
  Mono,
  Section,
  SeverityBadge,
  ShortDigest,
  SourceLinks,
} from "../components";
import { issueSeverity } from "../insights";
import {
  bundleSourceLinks,
  githubCommitUrl,
  langfuseBaseOf,
  langfusePromptUrl,
  langfuseTracesUrl,
  validatedGithubRepoUrl,
} from "../links";
import { useConnections, usePromptStatus, useShellData } from "../queries";
import { TopologyView } from "../TopologyView";

export function CodeProvenanceBanner({ bundle }: { bundle: Bundle }) {
  const code = bundle.code ?? null;
  if (!code) return null;
  // `code.repo_url` is an unconstrained contract string — route it through the SAME host
  // allowlist every GitHub link builder uses (github.com only, no `github.com.evil.com` bypass)
  // before building the commit / blob / history links. An invalid host degrades loudly: plain sha
  // text for the commit, the "no repo provenance" note for source links.
  const repoUrl = validatedGithubRepoUrl(code.repo_url);
  return (
    <div className="panel row" style={{ marginBottom: 16 }}>
      <span className="dim">defined in code</span>
      <Mono>{code.workflow_path ?? bundle.workflow.path}</Mono>
      <span className="dim">·</span>
      <Mono>
        {code.branch ?? "detached"} @{" "}
        {repoUrl ? (
          <a href={githubCommitUrl(repoUrl, code.sha)} target="_blank" rel="noreferrer noopener">
            {code.sha.slice(0, 7)}
          </a>
        ) : (
          code.sha.slice(0, 7)
        )}
      </Mono>
      {code.dirty ? <Badge kind="warning">dirty checkout</Badge> : null}
      <SourceLinks {...bundleSourceLinks(bundle)} />
    </div>
  );
}

export function IdentityPanel({ bundle }: { bundle: Bundle }) {
  return (
    <Section id="identity" title="Identity">
      <div className="panel">
        <KV
          rows={[
            ["Workflow", <Mono key="w">{bundle.workflow.workflow_name}</Mono>],
            ["Versioned type", <Mono key="t">{bundle.workflow.workflow_type}</Mono>],
            [
              "Spec digest",
              <Mono key="d">
                {bundle.workflow.spec_digest} <span className="faint">({bundle.workflow.spec_digest_algorithm})</span>
              </Mono>,
            ],
            ["Version label", bundle.workflow.version_label ?? <span className="faint">none</span>],
            ["Task queue", <Mono key="q">{bundle.workflow.task_queue}</Mono>],
            ["Trace name", <Mono key="tn">{bundle.workflow.observability_trace_name}</Mono>],
            ["YAML", <Mono key="y">{bundle.workflow.path}</Mono>],
            ["Environment", <Mono key="e">{bundle.environment.id} · {bundle.environment.profile_path}</Mono>],
          ]}
        />
      </div>
    </Section>
  );
}

export function TopologyPanel({ bundle, env }: { bundle: Bundle; env?: string }) {
  const topology = bundle.topology ?? { nodes: [], edges: [] };
  return (
    <Section id="topology" title="Topology">
      <div className="panel">
        <TopologyView topology={topology} env={env} />
        <div className="hint">
          Read-only projection: a ranked DAG of declared steps. Sequential steps run along the
          spine; a <em>parallel</em> block fans out to each branch (branches stack in rows and
          <em>collect</em> back where the merged value materializes), and a <em>when</em>-gate
          rides its edge as a <span className="mono">when</span> pill — hover it to see the
          condition. Sub-workflow nodes (<span className="mono">SUB</span>) link to the child
          workflow's page. Each review decision is an <em>entry point</em>
          into the downstream chain, shown as its own labeled swim-lane below the deepest row: a
          checkpoint has no unconditional outgoing arrow — its decision lanes are the flow, and
          execution continues from wherever a decision enters.
        </div>
      </div>
    </Section>
  );
}

/**
 * The child workflows this graph calls (`workflow` sub-workflow nodes, #55 slice 3) as their
 * own section after Activities — one row per child, its calling steps grouped, enriched from
 * the cached workflow summaries (source path) with the child id linked. Renders nothing for a
 * workflow without sub-workflows (most of them) — no empty section noise.
 */
export function SubWorkflowsPanel({ bundle, env }: { bundle: Bundle; env?: string }) {
  // The shell's cached workflow listing (one query app-wide) supplies the summary columns;
  // a child missing from the listing (e.g. cross-project) degrades to its id alone.
  const shell = useShellData();
  const summaries = new Map((shell.data?.workflows ?? []).map((w) => [w.id, w]));
  const calls = (bundle.topology?.nodes ?? [])
    .filter((node) => node.kind === "workflow")
    .map((node) => ({ step: node.id, workflow: node.workflow }))
    .filter((entry): entry is { step: string; workflow: string } =>
      typeof entry.workflow === "string" && entry.workflow.length > 0,
    );
  if (calls.length === 0) return null;
  const byChild = new Map<string, string[]>();
  for (const { step, workflow } of calls) {
    byChild.set(workflow, [...(byChild.get(workflow) ?? []), step]);
  }
  return (
    <Section id="sub-workflows" title="Sub-workflows">
      <div className="panel">
        <table className="grid">
          <thead>
            <tr>
              <th>Workflow</th>
              <th>Called from step</th>
              <th>Source</th>
            </tr>
          </thead>
          <tbody>
            {[...byChild.entries()].map(([child, steps]) => (
              <tr key={child}>
                <td>
                  <a
                    href={`#/workflows/${child}${env ? `?env=${encodeURIComponent(env)}` : ""}`}
                    className="mono"
                  >
                    {child}
                  </a>
                </td>
                <td>
                  <Mono>{steps.join(", ")}</Mono>
                </td>
                <td>
                  {summaries.get(child)?.path ? (
                    <Mono>{summaries.get(child)?.path}</Mono>
                  ) : (
                    <span className="dim">not in this project's listing</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="hint">
          Each child is a full workflow with its own page (topology, activities, runs); the SUB
          nodes in the topology above link to the same place.
        </div>
      </div>
    </Section>
  );
}

export function ActivitiesPanel({ bundle, catalog }: { bundle: Bundle; catalog?: Catalog }) {
  const catalogByName = new Map((catalog?.activities ?? []).map((entry) => [entry.name, entry]));
  const langfuseBase = langfuseBaseOf(bundle);
  const registryType =
    ((bundle.runtime?.["registry"] as Record<string, unknown> | undefined)?.["type"] as
      | string
      | undefined) ?? "inline";
  const langfuseRegistry = registryType === "langfuse";
  return (
    <Section id="activities" title="Activities">
      <div className="panel">
        <table className="grid">
          <thead>
            <tr>
              <th>Name</th>
              <th>Kind</th>
              <th>Input → Output</th>
              <th>Prompt</th>
              <th>Used by</th>
              <th>Schemas</th>
            </tr>
          </thead>
          <tbody>
            {(bundle.activities ?? []).map((activity) => {
              const entry = catalogByName.get(activity.name);
              return (
                <tr key={activity.name}>
                  <td>
                    <Mono>{activity.name}</Mono>
                  </td>
                  <td>
                    <span className={`badge ${activity.kind === "ai" ? "badge-info" : "badge-neutral"}`}>
                      {activity.kind}
                    </span>
                  </td>
                  <td>
                    <Mono>
                      {String(activity.input_schema["name"] ?? "?")} → {String(activity.output_schema["name"] ?? "?")}
                    </Mono>
                  </td>
                  <td>
                    {activity.prompt_ref ? (
                      <>
                        <Mono>
                          {String(activity.prompt_ref["name"])}
                          {activity.prompt_ref["version"] != null ? ` @v${activity.prompt_ref["version"]}` : ""}
                          {activity.prompt_ref["label"] != null ? ` @${activity.prompt_ref["label"]}` : ""}
                        </Mono>
                        {langfuseBase && langfuseRegistry ? (
                          <>
                            {" "}
                            <ExtLink
                              href={langfusePromptUrl(
                                langfuseBase,
                                String(activity.prompt_ref["name"]),
                              )}
                            >
                              Langfuse
                            </ExtLink>
                          </>
                        ) : null}
                      </>
                    ) : (
                      <span className="faint">—</span>
                    )}
                  </td>
                  <td>
                    <Mono>{(activity.used_by_steps ?? []).join(", ")}</Mono>
                  </td>
                  <td>
                    {entry ? (
                      <>
                        <JsonView label="input json-schema" value={entry.input_schema.json_schema} />
                        <JsonView label="output json-schema" value={entry.output_schema.json_schema} />
                      </>
                    ) : (
                      <span className="faint">catalog unavailable</span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </Section>
  );
}

export function LifecyclePanel({ bundle }: { bundle: Bundle }) {
  return (
    <Section id="lifecycle" title="Lifecycle">
      <div className="panel">
        {bundle.lifecycle ? (
          <>
            <KV
              rows={[
                ["Progress", bundle.lifecycle.progress ? "enabled" : "disabled"],
                ["Cancellation", bundle.lifecycle.cancellation ? "enabled" : "disabled"],
                ["Status event limit", String(bundle.lifecycle.status_event_limit)],
                [
                  "Review gate",
                  bundle.lifecycle.review ? (
                    <Mono key="r">
                      after {bundle.lifecycle.review.after_step} · invalid decision:{" "}
                      {bundle.lifecycle.review.invalid_user_decision}
                    </Mono>
                  ) : (
                    <span className="faint">none</span>
                  ),
                ],
              ]}
            />
            {bundle.lifecycle.review ? (
              <table className="grid" style={{ marginTop: 8 }}>
                <thead>
                  <tr>
                    <th>Decision</th>
                    <th>Routes to</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(bundle.lifecycle.review.user_decisions).map(([decision, target]) => (
                    <tr key={decision}>
                      <td>
                        <Mono>{decision}</Mono>
                      </td>
                      <td>
                        <Mono>{target}</Mono>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : null}
          </>
        ) : (
          <span className="dim">Lifecycle disabled — no review or cancellation surface.</span>
        )}
      </div>
    </Section>
  );
}

export function PolicyPanel({ bundle }: { bundle: Bundle }) {
  // Risk tiers (#300): the additive `risk_tier` posture — read defensively (the pinned
  // client types may predate the field). Tier + control NAMES only; never prompt/secret.
  const riskTier = (
    bundle as {
      risk_tier?: {
        declared?: string;
        effective?: string;
        floor_source?: string;
        requirements?: { name?: string; satisfied?: boolean }[];
        cascade?: {
          lifted_by?: string;
          effective?: string;
          requirements?: { name?: string; satisfied?: boolean }[];
        };
      };
    }
  ).risk_tier;
  return (
    <Section id="policy" title="Policy">
      <div className="panel">
        {bundle.policy ? (
          <KV
            rows={[
              ["Selected", <Mono key="s">{bundle.policy.selected_policy_ids.join(", ")}</Mono>],
              ["Applied", <Mono key="a">{bundle.policy.applied_policy_ids.join(", ") || "(composition failed)"}</Mono>],
              ["Policy hash", <ShortDigest key="h" value={bundle.policy.policy_hash || "—"} />],
            ]}
          />
        ) : (
          <span className="dim">No policy bundle applied in this environment.</span>
        )}
        {riskTier && typeof riskTier.effective === "string"
          ? (() => {
              const controlBadges = (reqs: { name?: string; satisfied?: boolean }[]) => (
                <span>
                  {reqs.map((req, index) => (
                    <span key={req?.name ?? index} style={{ marginRight: 6 }}>
                      <Badge kind={req?.satisfied ? "ok" : "warning"}>{req?.name ?? "?"}</Badge>
                    </span>
                  ))}
                </span>
              );
              const rows: Array<[string, ReactNode]> = [
                [
                  "Risk tier",
                  <span key="rt">
                    <Badge kind={riskTier.effective === "prohibited" ? "critical" : "neutral"}>
                      {riskTier.effective}
                    </Badge>{" "}
                    {riskTier.declared && riskTier.declared !== riskTier.effective ? (
                      <span className="faint">
                        declared {riskTier.declared},{" "}
                        {typeof riskTier.floor_source === "string" &&
                        riskTier.floor_source.startsWith("cascade:")
                          ? `lifted by sub-workflow ${riskTier.floor_source.slice("cascade:".length)}`
                          : "raised by policy floor"}
                      </span>
                    ) : null}
                  </span>,
                ],
              ];
              if (Array.isArray(riskTier.requirements) && riskTier.requirements.length > 0) {
                rows.push(["Controls", controlBadges(riskTier.requirements)]);
              }
              // The sub-workflow closure lift (#300 D300-3): a higher-tier child raised the
              // effective tier — show what lifted it and the re-expanded controls.
              if (riskTier.cascade && typeof riskTier.cascade.lifted_by === "string") {
                rows.push([
                  "Cascade",
                  <span key="cx">
                    <span className="faint">
                      lifted to {riskTier.cascade.effective ?? riskTier.effective} by sub-workflow{" "}
                      <Mono>{riskTier.cascade.lifted_by}</Mono>
                    </span>{" "}
                    {Array.isArray(riskTier.cascade.requirements) && riskTier.cascade.requirements.length > 0
                      ? controlBadges(riskTier.cascade.requirements)
                      : null}
                  </span>,
                ]);
              }
              return (
                <div style={{ marginTop: 8 }}>
                  <KV rows={rows} />
                </div>
              );
            })()
          : null}
      </div>
    </Section>
  );
}

export function ComponentsPanel({ bundle }: { bundle: Bundle }) {
  return (
    <Section id="components" title="Components">
      <div className="panel">
        {(bundle.components ?? []).length > 0 ? (
          <table className="grid">
            <thead>
              <tr>
                <th>Kind</th>
                <th>Id</th>
                <th>Content hash</th>
                <th>Source</th>
              </tr>
            </thead>
            <tbody>
              {(bundle.components ?? []).map((component, index) => {
                const record = component as Record<string, unknown>;
                return (
                  <tr key={index}>
                    <td>
                      <span className="badge badge-neutral">{String(record["kind"] ?? "?")}</span>
                    </td>
                    <td>
                      <Mono>{String(record["id"] ?? "?")}</Mono>
                    </td>
                    <td>
                      <ShortDigest value={String(record["content_hash"] ?? "")} />
                    </td>
                    <td>
                      <Mono>{String(record["source_path"] ?? "")}</Mono>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        ) : (
          <span className="dim">No component profiles applied.</span>
        )}
      </div>
    </Section>
  );
}

export function SecretsPanel({ bundle }: { bundle: Bundle }) {
  return (
    <Section id="secrets" title="Secret references">
      <div className="panel">
        {(bundle.secret_references ?? []).length > 0 ? (
          <table className="grid">
            <thead>
              <tr>
                <th>Runtime path</th>
                <th>Source</th>
                <th>Configured</th>
              </tr>
            </thead>
            <tbody>
              {(bundle.secret_references ?? []).map((reference, index) => {
                const record = reference as Record<string, unknown>;
                return (
                  <tr key={index}>
                    <td>
                      <Mono>{String(record["runtime_path"] ?? "")}</Mono>
                    </td>
                    <td>
                      <Mono>
                        {String(record["source_kind"] ?? "")}:{String(record["source_name"] ?? "")}
                      </Mono>
                    </td>
                    <td>
                      {record["configured"] ? (
                        <Badge kind="ok">configured</Badge>
                      ) : (
                        <Badge kind="critical">missing</Badge>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        ) : (
          <span className="dim">No secret references declared.</span>
        )}
        <div className="hint">Values never leave the server; only references and configured state.</div>
      </div>
    </Section>
  );
}

/** "checked 12:04:31 · refresh" affordance for an on-demand cached probe. */
function ProbeFreshness({
  dataUpdatedAt,
  isFetching,
  onRefresh,
}: {
  dataUpdatedAt: number;
  isFetching: boolean;
  onRefresh: () => void;
}) {
  return (
    <div className="hint">
      checked {new Date(dataUpdatedAt).toLocaleTimeString()} ·{" "}
      <button type="button" className="link-button" onClick={onRefresh} disabled={isFetching}>
        {isFetching ? "refreshing…" : "refresh"}
      </button>
    </div>
  );
}

/**
 * The on-demand probe mechanism, shared by every probe panel: nothing fetches
 * until the operator checks *this* workflow/environment. The request state is
 * identity-keyed rather than a reset-by-effect boolean, so switching
 * workflows renders unrequested on the very first render — a probe can never
 * fire for a workflow the operator didn't ask about, and there is no reset
 * effect for future panel state to miss.
 */
function useOnDemandProbe<T>(
  useProbeQuery: (workflowId: string, env: string, requested: boolean) => UseQueryResult<T>,
  workflowId: string,
  env: string,
) {
  const [requestedFor, setRequestedFor] = useState<string | null>(null);
  const identity = `${workflowId} ${env}`;
  const requested = requestedFor === identity;
  const probe = useProbeQuery(workflowId, env, requested);
  const check = () => {
    if (requested) void probe.refetch();
    else setRequestedFor(identity);
  };
  /** Idle = show the check button (never requested here, or the probe failed). */
  const idle = !requested || (probe.isError && !probe.isFetching);
  return { probe, check, idle, failed: requested && probe.isError };
}

export function ConnectionsPanel({
  workflowId,
  env,
  bundle,
}: {
  workflowId: string;
  env: string;
  bundle: Bundle;
}) {
  const langfuseBase = langfuseBaseOf(bundle);
  const { probe, check, idle, failed } = useOnDemandProbe(useConnections, workflowId, env);
  const connections = probe.data;

  return (
    <Section id="connections" title="Connections">
      <div className="panel">
        {idle ? (
          <div className="row">
            <button type="button" onClick={check}>
              Check connections
            </button>
            <span className="dim">
              {failed
                ? "The probe failed — try again."
                : "Probes the YAML-configured registry and observer once."}
            </span>
          </div>
        ) : !connections ? (
          <Loading what="connections" />
        ) : (
          <>
            <div className="row" style={{ marginBottom: 6 }}>
              <span className="dim">registry</span>
              <Mono>
                {connections.registry.type}
                {connections.registry.host ? ` @ ${connections.registry.host}` : ""}
              </Mono>
              {connections.registry.reachable ? (
                <Badge kind="ok">connected</Badge>
              ) : (
                <span className="badge badge-critical" title={connections.registry.detail ?? ""}>
                  unreachable
                </span>
              )}
            </div>
            <div className="row">
              <span className="dim">observer</span>
              {connections.observability.type === "none" ? (
                <span className="dim">none — runs are not traced</span>
              ) : (
                <>
                  <Mono>
                    {connections.observability.type}
                    {connections.observability.host ? ` @ ${connections.observability.host}` : ""}
                  </Mono>
                  {connections.observability.reachable ? (
                    <Badge kind="ok">connected</Badge>
                  ) : (
                    <span
                      className="badge badge-critical"
                      title={connections.observability.detail ?? ""}
                    >
                      unreachable
                    </span>
                  )}
                  <span className="badge badge-neutral">
                    manifest {connections.observability.execution_manifest ? "on" : "off"}
                  </span>
                  <span className="badge badge-neutral">
                    redaction {connections.observability.redaction_enabled ? "on" : "off"}
                  </span>
                  {connections.observability.type === "langfuse" && langfuseBase ? (
                    <ExtLink href={langfuseTracesUrl(langfuseBase, bundle.workflow.observability_trace_name)}>
                      this workflow&apos;s traces
                    </ExtLink>
                  ) : null}
                </>
              )}
            </div>
            <ProbeFreshness
              dataUpdatedAt={probe.dataUpdatedAt}
              isFetching={probe.isFetching}
              onRefresh={() => void probe.refetch()}
            />
          </>
        )}
      </div>
    </Section>
  );
}

export function PromptRegistryPanel({
  workflowId,
  env,
  bundle,
}: {
  workflowId: string;
  env: string;
  bundle: Bundle;
}) {
  const langfuseBase = langfuseBaseOf(bundle);
  const { probe, check, idle, failed } = useOnDemandProbe(usePromptStatus, workflowId, env);
  const promptStatus = probe.data;

  return (
    <Section id="prompt-registry" title="Prompt registry">
      <div className="panel">
        {idle ? (
          <div className="row">
            <button type="button" onClick={check}>
              Check prompt drift
            </button>
            <span className="dim">
              {failed
                ? "The check failed — try again."
                : "Compares each label's current registry version against the last run's manifest."}
            </span>
          </div>
        ) : !promptStatus ? (
          <Loading what="prompt status" />
        ) : (
          <>
            <table className="grid">
              <thead>
                <tr>
                  <th>Prompt</th>
                  <th>Selector</th>
                  <th>Registry</th>
                  <th>Last run</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {(promptStatus.prompts ?? []).map((entry) => (
                  <tr key={entry.name}>
                    <td>
                      <Mono>{entry.name}</Mono>{" "}
                      {langfuseBase && entry.mode !== "inline" ? (
                        <ExtLink href={langfusePromptUrl(langfuseBase, entry.name)}>Langfuse</ExtLink>
                      ) : null}
                      {entry.mode === "inline" && entry.template ? (
                        <details className="json">
                          <summary>preview</summary>
                          <pre>{entry.template}</pre>
                        </details>
                      ) : null}
                    </td>
                    <td>
                      <Mono>{entry.selector}</Mono>
                    </td>
                    <td>
                      <Mono>{entry.registry_version ?? "—"}</Mono>
                    </td>
                    <td>
                      <Mono>{entry.last_run_version ?? "—"}</Mono>
                    </td>
                    <td>
                      <span
                        className={`badge ${entry.status === "drift" ? "badge-warning" : entry.status === "in_sync" ? "badge-ok" : "badge-neutral"}`}
                        title={entry.detail ?? ""}
                      >
                        {entry.status}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <ProbeFreshness
              dataUpdatedAt={probe.dataUpdatedAt}
              isFetching={probe.isFetching}
              onRefresh={() => void probe.refetch()}
            />
          </>
        )}
      </div>
    </Section>
  );
}

export function RuntimePanel({ bundle }: { bundle: Bundle }) {
  return (
    <Section id="runtime" title="Runtime">
      <div className="panel">
        {(bundle.runtime_effective ?? []).length > 0 ? (
          <table className="grid" style={{ marginBottom: 10 }}>
            <thead>
              <tr>
                <th>Effective setting</th>
                <th>Value</th>
                <th>Source</th>
              </tr>
            </thead>
            <tbody>
              {(bundle.runtime_effective ?? []).map((entry) => (
                <tr key={entry.path}>
                  <td>
                    <Mono>{entry.path}</Mono>
                  </td>
                  <td>
                    <Mono>{JSON.stringify(entry.value)}</Mono>
                  </td>
                  <td>
                    <span
                      className={`badge ${entry.source === "configured" ? "badge-info" : "badge-neutral"}`}
                    >
                      {entry.source === "engine_default"
                        ? "default"
                        : entry.source === "project_default"
                          ? "project default"
                          : "configured"}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
        {Object.entries(bundle.runtime ?? {}).map(([key, value]) => (
          <JsonView key={key} label={`runtime.${key}`} value={value} />
        ))}
      </div>
    </Section>
  );
}

export function ValidationPanel({ bundle }: { bundle: Bundle }) {
  return (
    <Section id="validation" title="Validation">
      <div className="panel">
        {bundle.validation.ok ? (
          <div className="row">
            <Badge kind="ok">ok</Badge>
            <span className="dim">All checks passed.</span>
          </div>
        ) : (
          <table className="grid">
            <thead>
              <tr>
                <th>Severity</th>
                <th>Code</th>
                <th>Message</th>
              </tr>
            </thead>
            <tbody>
              {(bundle.validation.issues ?? []).map((issue, index) => (
                <tr key={index}>
                  <td>
                    <SeverityBadge severity={issueSeverity(issue)} />
                  </td>
                  <td>
                    <Mono>{issue.code}</Mono>
                  </td>
                  <td>{issue.message}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {(bundle.validation.checks ?? []).length > 0 ? (
          <JsonView label="resolved checks" value={bundle.validation.checks} />
        ) : null}
      </div>
    </Section>
  );
}

export function DeploymentPanel({ bundle }: { bundle: Bundle }) {
  return (
    <Section id="deployment" title="Deployment">
      <div className="panel">
        {bundle.deployment_preview ? (
          <JsonView label="deployment preview" value={bundle.deployment_preview} />
        ) : bundle.deployment_preview_reference ? (
          <>
            <div className="hint" style={{ marginTop: 0, marginBottom: 6 }}>
              Bundle this declarative definition into a deployment:
            </div>
            <CopyBlock command={bundle.deployment_preview_reference} />
          </>
        ) : (
          <span className="dim">No deployment preview.</span>
        )}
      </div>
    </Section>
  );
}
