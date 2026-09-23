/**
 * The policy violations & enforcement feed (#723 slice 3 / #577 §2): the real
 * queryable surface that replaced the governance persona's planned panel —
 * one shared component, mounted on the Governance persona view AND the
 * Governance page (the coverage-sharing pattern). All verdicts, notices, and
 * row shapes come from the pure `enforcementFeed.ts` engine; this component
 * renders them and owns only the filter state.
 *
 * Honest states, in order of precedence:
 * - capability absent/false → an explicit "not supported by this control
 *   plane" panel (nothing is fetched), never an error and never an empty feed;
 * - 501 UnsupportedRuntime → the shared honest-unavailable prose;
 * - `partial.langfuse` unreachable/not_configured → a LOUD banner above the
 *   rows (runtime events may be missing / are not available);
 * - empty feed → an explicit confirmation scoped to what was actually read
 *   (all-clear only when `partial` is ok).
 */

import { useMemo, useState } from "react";
import { Link } from "@tanstack/react-router";

import { isUnsupportedRuntime, type Capabilities, type EnforcementVerdict } from "../api";
import {
  Badge,
  CapabilityAbsentPanel,
  ErrorPanel,
  Loading,
  Mono,
  NoticeRow,
  ResolutionUnavailable,
  Section,
  TraceLink,
} from "../components";
import {
  capabilityAbsentCopy,
  combinePartial,
  deriveFeedRows,
  emptyFeedNotice,
  partialNotice,
  sourceBadgeTitle,
  verdictBadgeTitle,
  WINDOW_PRESETS,
  type EnforcementRow,
  type WindowPresetId,
} from "../enforcementFeed";
import { errorMessage, useEnforcementEvents } from "../queries";

function EvidenceCell({ row, env }: { row: EnforcementRow; env: string }) {
  if (row.evidence.kind === "trace") return <TraceLink state={row.evidence.state} />;
  if (row.evidence.kind === "validation") {
    return (
      <Link
        to="/workflows/$workflowId"
        params={{ workflowId: row.evidence.workflowId }}
        search={{ env, section: row.evidence.section }}
        title="Admission evidence: the workflow's current validation report."
      >
        Validation
      </Link>
    );
  }
  return <span className="faint">—</span>;
}

export function EnforcementFeedSection({
  env,
  workflowIds,
  capabilities,
  langfuseBase,
}: {
  env: string;
  workflowIds: string[];
  capabilities: Capabilities;
  /** The env's Langfuse project base for trace evidence links (null → loud degradation). */
  langfuseBase: string | null;
}) {
  const [workflowId, setWorkflowId] = useState("");
  const [verdict, setVerdict] = useState<"" | EnforcementVerdict>("");
  const [windowId, setWindowId] = useState<WindowPresetId>("7d");
  // Feature-detect per capability (#577 §6): absent (older contract) and false both
  // gate the fetch — the honest not-supported panel, never an error or empty feed.
  const supported = capabilities.enforcement_events === true;
  const query = useEnforcementEvents(
    env,
    { workflowId: workflowId || null, verdict: verdict || null, window: windowId },
    supported,
  );

  const pages = query.data?.pages;
  // The banner is the loudest-wins combined partial; the ROWS are derived per page
  // with that page's own partial (#723 F1) so a later unreachable page can't
  // retroactively mask an earlier page's working trace links.
  const partial = useMemo(() => (pages && pages.length > 0 ? combinePartial(pages) : null), [pages]);
  const rows = useMemo(
    () => (pages ? deriveFeedRows(pages, { langfuseBase }) : []),
    [pages, langfuseBase],
  );

  if (!supported) {
    return (
      <CapabilityAbsentPanel
        id="violations"
        title="Policy violations & enforcement"
        copy={capabilityAbsentCopy()}
      />
    );
  }

  const notice = partial === null ? null : partialNotice(partial);

  return (
    <Section id="violations" title="Policy violations & enforcement">
      <div className="panel">
        <div className="row" style={{ marginBottom: 10, flexWrap: "wrap" }}>
          <label className="field">
            workflow{" "}
            <select value={workflowId} onChange={(event) => setWorkflowId(event.target.value)}>
              <option value="">all</option>
              {workflowIds.map((id) => (
                <option key={id} value={id}>
                  {id}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            verdict{" "}
            <select
              value={verdict}
              onChange={(event) => setVerdict(event.target.value as "" | EnforcementVerdict)}
            >
              <option value="">all</option>
              <option value="blocked">blocked</option>
              <option value="rejected">rejected</option>
            </select>
          </label>
          <label className="field">
            window{" "}
            <select
              value={windowId}
              onChange={(event) => setWindowId(event.target.value as WindowPresetId)}
            >
              {WINDOW_PRESETS.map((preset) => (
                <option key={preset.id} value={preset.id}>
                  {preset.label}
                </option>
              ))}
            </select>
          </label>
        </div>

        {query.isPending ? (
          <Loading what="enforcement events" />
        ) : query.isError ? (
          isUnsupportedRuntime(query.error) ? (
            <ResolutionUnavailable what="The enforcement feed" />
          ) : (
            <ErrorPanel
              message={errorMessage(query.error) ?? "enforcement-events request failed"}
              hint="The feed is read at request time from the validation surface and Langfuse — retry once the control plane is reachable."
            />
          )
        ) : (
          <>
            {notice ? <NoticeRow notice={notice} /> : null}
            {rows.length === 0 ? (
              <EmptyFeed partial={partial ?? "ok"} />
            ) : (
              <table className="grid">
                <thead>
                  <tr>
                    <th>Occurred</th>
                    <th>Source</th>
                    <th>Verdict</th>
                    <th>Rule</th>
                    <th>Workflow</th>
                    <th>Policies</th>
                    <th>Detail</th>
                    <th>Evidence</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row) => (
                    <tr key={row.key}>
                      <td>
                        {row.occurredAt ? (
                          <Mono>{row.occurredAt}</Mono>
                        ) : (
                          <span
                            className="faint"
                            title="Admission events reflect current admission state (point-in-time now), not a recorded past instant."
                          >
                            —
                          </span>
                        )}
                      </td>
                      <td>
                        <Badge kind="neutral" title={sourceBadgeTitle(row.source)}>
                          {row.source}
                        </Badge>
                      </td>
                      <td>
                        <Badge kind="critical" title={verdictBadgeTitle(row.verdict)}>
                          {row.verdict}
                        </Badge>
                      </td>
                      <td>
                        <Mono>{row.rule}</Mono>
                      </td>
                      <td>
                        {row.workflowId ? (
                          <Link
                            to="/workflows/$workflowId"
                            params={{ workflowId: row.workflowId }}
                            search={{ env, section: "policy" }}
                          >
                            <Mono>{row.workflowId}</Mono>
                          </Link>
                        ) : (
                          <span className="faint">—</span>
                        )}
                      </td>
                      <td>
                        {row.policyIds.length > 0 ? (
                          row.policyIds.map((policyId, index) => (
                            <span key={policyId}>
                              {index > 0 ? ", " : null}
                              <Link
                                to="/policies/$policyId"
                                params={{ policyId }}
                                search={{ env }}
                              >
                                <Mono>{policyId}</Mono>
                              </Link>
                            </span>
                          ))
                        ) : (
                          <span className="faint">—</span>
                        )}
                      </td>
                      <td>{row.detail}</td>
                      <td>
                        <EvidenceCell row={row} env={env} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            {query.hasNextPage ? (
              <div className="row" style={{ marginTop: 8 }}>
                <button
                  type="button"
                  disabled={query.isFetchingNextPage}
                  onClick={() => void query.fetchNextPage()}
                >
                  {query.isFetchingNextPage ? "Loading…" : "Load more"}
                </button>
                <span className="dim">More events remain in this window.</span>
              </div>
            ) : null}
          </>
        )}
        <div className="hint">
          Read at request time (#577 §2): admission verdicts are normalized from the same
          validation surface each workflow page renders; runtime verdicts (moderation blocks) are
          read from Langfuse traces in the selected window. Nothing is persisted server-side, and
          the window is bounded — widen it with the window filter.
        </div>
      </div>
    </Section>
  );
}

function EmptyFeed({ partial }: { partial: "ok" | "unreachable" | "not_configured" }) {
  const notice = emptyFeedNotice(partial);
  return (
    <div className="row">
      <Badge kind={notice.badgeKind}>{notice.badge}</Badge>
      <span className="dim">{notice.message}</span>
    </div>
  );
}
