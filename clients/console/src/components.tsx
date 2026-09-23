import type { ReactNode } from "react";

import type { AcknowledgedInsight, ActiveInsightRow } from "./annotationsFeed";
import { annotateInsights, trackedInHref } from "./annotationsFeed";
import type { CapabilityAbsentCopy, FeedNotice } from "./enforcementFeed";
import type { Insight, Severity } from "./insights";
import type { SourceLinkPair, TraceLinkState } from "./links";
import { useAnnotations } from "./queries";

/**
 * Plain-language tooltip for each status badge, keyed by its label. The point
 * is consequences, not restatement — what the badge means for the operator and
 * what to do about it. A badge with a known label gets this automatically; an
 * explicit `title` on <Badge> overrides it.
 */
export const BADGE_HELP: Record<string, string> = {
  // Code provenance (#252)
  "dirty checkout":
    "The resolved code has uncommitted changes, so this bundle maps to no committed commit — it is not reproducible. Resolve from a clean, committed checkout (or a Git-sourced project) before relying on it.",
  // Versioning / drain (#191)
  current: "This versioned workflow type matches the current resolution.",
  "old version":
    "This execution runs an older versioned workflow type than the current resolution. Let it drain before decommissioning the worker that serves the old type.",
  // Worker presence (#278)
  "no workers polling":
    "No worker is polling this task queue, so a run started now will sit pending until a worker comes up.",
  // Secret references — values never leave the server, only configured state
  configured: "This secret reference resolves from an environment variable that is set.",
  missing:
    "This secret reference's environment variable is not set in the resolved environment; the run may fail when it needs the value.",
  // Connections (#258)
  connected: "The control plane reached this backend with the resolved configuration.",
  // Deployment plans (#253) and plan coverage (#294)
  ready: "The latest approved plan matches the current resolution and can be promoted as-is.",
  "ready to promote": "This plan matches the current resolution and can be promoted as-is.",
  drifted:
    "The approved plan no longer matches the current resolution; promotion fails closed until a fresh plan is approved.",
  drift: "This field differs between the approved plan and the current resolution.",
  "preflight ok": "The deployment preflight checks passed for this plan.",
  "preflight failed":
    "A deployment preflight check failed for this plan; it should not be promoted until resolved.",
  "digest-pinned":
    "The worker image is pinned to a content digest, so the deployment is reproducible.",
  "mutable image":
    "The worker image uses a mutable tag, so the deployment is not reproducible. Pin it to a digest.",
  // Multi-project (#256) and refresh provenance (#296)
  active: "The console is currently scoped to this project; its routes back every view.",
  // Environment diff (#243)
  identical: "The two environments resolve to byte-identical bundles.",
};

/**
 * A status badge. With a string label that matches BADGE_HELP it carries an
 * explanatory tooltip automatically; pass `title` to override or for dynamic
 * labels.
 */
export function Badge({
  kind,
  title,
  children,
}: {
  kind: Severity | "neutral";
  title?: string;
  children: ReactNode;
}) {
  const help = title ?? (typeof children === "string" ? BADGE_HELP[children] : undefined);
  return (
    <span className={`badge badge-${kind}`} title={help}>
      {children}
    </span>
  );
}

/** What each severity level means for the operator. */
const SEVERITY_HELP: Record<Severity, string> = {
  critical:
    "Version or governance drift — treat as blocking; the resolved state is not safe to rely on until addressed.",
  warning: "Behavior may differ — review before promoting or starting runs.",
  info: "Expected divergence — informational, no action required.",
  ok: "No issue found.",
};

export function SeverityBadge({ severity }: { severity: Severity }) {
  return (
    <span className={`badge badge-${severity}`} title={SEVERITY_HELP[severity]}>
      {severity}
    </span>
  );
}

export function Section({ id, title, children }: { id: string; title: string; children: ReactNode }) {
  return (
    <section className="section" id={id}>
      <h2>{title}</h2>
      {children}
    </section>
  );
}

/**
 * An honestly-labeled "planned — needs backend" section (#721 F12): a `planned` badge with the
 * one-line `what` the surface will show once the backend exists, and the `why` it is deliberately
 * a placeholder rather than an empty (falsely all-clear) feed. One place to render the pattern —
 * reused by the governance persona's violations panel today, GitHub-drift / acks panels later.
 * `id` keeps the section addressable (anchor links, tests); `title` is the section heading.
 */
export function PlannedPanel({
  id,
  title,
  what,
  why,
}: {
  id: string;
  title: string;
  what: ReactNode;
  why: ReactNode;
}) {
  return (
    <Section id={id} title={title}>
      <div className="panel">
        <div className="row">
          <Badge kind="neutral">planned</Badge>
          <span className="dim">{what}</span>
        </div>
        <div className="hint">{why}</div>
      </div>
    </Section>
  );
}

/**
 * The capability-absent panel (#577 §6): when the control plane does not advertise a surface's
 * capability, nothing is fetched and the panel says why. One shared renderer for the byte-identical
 * JSX the enforcement feed and the GitHub-drift section both used — the module owns every rendered
 * word (via {@link CapabilityAbsentCopy}) while this keeps the code styling on the capability token.
 */
export function CapabilityAbsentPanel({
  id,
  title,
  copy,
}: {
  id: string;
  title: string;
  copy: CapabilityAbsentCopy;
}) {
  return (
    <Section id={id} title={title}>
      <div className="panel">
        <div className="row">
          <Badge kind="neutral">{copy.badge}</Badge>
          <span className="dim">
            {copy.before}
            <Mono>{copy.capability}</Mono>
            {copy.after}
          </span>
        </div>
      </div>
    </Section>
  );
}

/**
 * The loud partial-result banner row shared by every degradable feed (#723 / #727): one
 * badge-plus-message row from a {@link FeedNotice}, so the enforcement feed and the GitHub-drift
 * section render their degradation identically instead of each open-coding the same JSX.
 */
export function NoticeRow({ notice }: { notice: FeedNotice }) {
  return (
    <div className="row" style={{ marginBottom: 8 }}>
      <Badge kind={notice.badgeKind}>{notice.badge}</Badge>
      <span className="dim">{notice.message}</span>
    </div>
  );
}

export function KV({ rows }: { rows: Array<[string, ReactNode]> }) {
  return (
    <table className="kv">
      <tbody>
        {rows.map(([key, value]) => (
          <tr key={key}>
            <td className="k">{key}</td>
            <td>{value}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function Mono({ children }: { children: ReactNode }) {
  return <span className="mono">{children}</span>;
}

export function ShortDigest({ value }: { value: string }) {
  return (
    <span className="mono" title={value}>
      {value.length > 14 ? `${value.slice(0, 12)}…` : value}
    </span>
  );
}

export function JsonView({ label, value }: { label: string; value: unknown }) {
  return (
    <details className="json">
      <summary>{label}</summary>
      <pre>{JSON.stringify(value, null, 2)}</pre>
    </details>
  );
}

/**
 * The shared insight feed — the SINGLE interception point for insight acknowledgements (#733). Every
 * feed (Overview, Drift sections, persona views) renders through here, so acking is applied "for
 * free" everywhere without per-page wiring: {@link useAnnotations} is the one cached
 * `.typeflux/annotations.yaml` read, and {@link annotateInsights} (the tested pure layer) partitions
 * acknowledged insights out of the active list. Acknowledged insights COLLAPSE under a disclosure
 * (reason + tracking) — never hidden outright (audit honesty); each active insight offers a copyable
 * acknowledge snippet. The derived stale-ack WARNINGS are injected upstream (OverviewPage's project
 * feed) rather than here, so a page with several InsightList sections doesn't repeat them.
 */
export function InsightList({ insights }: { insights: Insight[] }) {
  // Always-enabled, capability-free read (project-level pure YAML). While it is loading/errored the
  // feed degrades to all-active — the acknowledge affordance still shows (it is how the first ack is
  // authored), so rendering never blocks on this read.
  const { data } = useAnnotations();
  // `now` gates collapse-eligibility (an expired ack no longer collapses); render-time is correct —
  // the verdict is a calendar-day comparison, so it's stable within any one day's renders.
  const feed = annotateInsights(insights, data, new Date());
  if (feed.active.length === 0 && feed.acknowledged.length === 0) {
    return (
      <div className="row">
        <span className="badge badge-ok">ok</span>
        <span className="dim">No findings.</span>
      </div>
    );
  }
  return (
    <>
      {feed.active.length > 0 ? (
        <ul className="insight-list">
          {feed.active.map((row) => (
            <InsightRow key={row.insight.id} row={row} filePath={feed.filePath} />
          ))}
        </ul>
      ) : null}
      {feed.acknowledged.length > 0 ? <AcknowledgedInsights items={feed.acknowledged} /> : null}
    </>
  );
}

/** One active insight row, with the copyable "acknowledge…" affordance when it makes sense (the
 * engine decides via {@link ActiveInsightRow.acknowledgeSnippet}). */
function InsightRow({ row, filePath }: { row: ActiveInsightRow; filePath: string }) {
  const { insight } = row;
  return (
    <li>
      <span>
        <SeverityBadge severity={insight.severity} />
      </span>
      <span>
        <a className="insight-title" href={insight.link}>
          {insight.title}
        </a>
        <div className="insight-detail">{insight.detail}</div>
        {insight.sourceLinks ? (
          <div className="insight-evidence">
            <SourceLinks {...insight.sourceLinks} />
          </div>
        ) : null}
        {insight.evidence ? (
          <div className="insight-evidence">
            <ExtLink href={insight.evidence.href}>{insight.evidence.label}</ExtLink>
          </div>
        ) : null}
        {row.acknowledgeSnippet ? (
          <details className="ack-affordance">
            <summary>acknowledge…</summary>
            <div className="hint">
              Acknowledge this insight by committing an entry to <Mono>{filePath}</Mono> — the
              read-only console never writes it; the ack is PR-reviewed like everything else.
            </div>
            <CopyBlock command={row.acknowledgeSnippet} />
          </details>
        ) : null}
      </span>
    </li>
  );
}

/**
 * The acknowledged insights, COLLAPSED under an "acknowledged (N)" disclosure (#733) — visible on
 * demand, never hidden outright (audit honesty). Each shows its original severity, the ack reason,
 * and the tracking issue as a host-gated external link (an inert non-http `tracked_in` degrades to
 * plain text rather than a live link).
 */
function AcknowledgedInsights({ items }: { items: AcknowledgedInsight[] }) {
  return (
    <details className="acknowledged">
      <summary>acknowledged ({items.length})</summary>
      <ul className="insight-list acknowledged-list">
        {items.map(({ insight, annotation }) => {
          const href = trackedInHref(annotation.tracked_in);
          return (
            <li key={insight.id}>
              <span>
                <SeverityBadge severity={insight.severity} />
              </span>
              <span>
                <a className="insight-title" href={insight.link}>
                  {insight.title}
                </a>
                <div className="insight-detail">
                  Acknowledged: {annotation.reason}
                </div>
                {href ? (
                  <div className="insight-evidence">
                    <ExtLink href={href}>tracked issue</ExtLink>
                  </div>
                ) : annotation.tracked_in ? (
                  <div className="insight-evidence">
                    <span className="faint">tracked in {annotation.tracked_in}</span>
                  </div>
                ) : null}
              </span>
            </li>
          );
        })}
      </ul>
    </details>
  );
}

/**
 * Honest unavailable state for a by-construction limitation (#621 slice 3): the serving
 * control plane cannot resolve this project's declared runtime (#619, 501
 * `UnsupportedRuntime`) — the contract behaving as written, never a failure. Pure-YAML
 * inspection stays available; resolution/operation surfaces live on the project's own
 * edition's server.
 */
export function ResolutionUnavailable({ what }: { what: string }) {
  return (
    <div className="panel">
      <span className="dim">
        {what} is unavailable here: this control plane cannot resolve the project&apos;s
        declared runtime. Inspection (workflows, environments, policies, profiles) stays
        available — resolve and operate this project from its own edition&apos;s server.
      </span>
    </div>
  );
}

export function ErrorPanel({ message, hint }: { message: string; hint?: string }) {
  return (
    <div className="error-panel">
      <div className="mono">{message}</div>
      {hint ? <div className="hint">{hint}</div> : null}
    </div>
  );
}

export function Loading({ what }: { what: string }) {
  return <div className="loading">loading {what}…</div>;
}

export function CopyBlock({ command }: { command: string }) {
  return (
    <div className="copy-block">
      <code>{command}</code>
      <button type="button" onClick={() => void navigator.clipboard.writeText(command)}>
        Copy
      </button>
    </div>
  );
}

/** External hand-off: Temporal Web / Langfuse own the deep detail. */
export function ExtLink({ href, children }: { href: string; children: ReactNode }) {
  return (
    <a className="ext-link" href={href} target="_blank" rel="noreferrer noopener">
      {children} ↗
    </a>
  );
}

/**
 * The source-of-truth hand-off (#718 §A / #577 §6): a "View source" blob link plus a "History"
 * commit-log link for any named file, at the resolved sha. When neither URL resolves the
 * affordance degrades LOUDLY — an explicit "source link unavailable (no repo provenance)" note,
 * never a silently absent link — so an operator can tell a missing link from an un-provenanced
 * project. Callers compute the URLs (project- or bundle-code-based); this stays presentational.
 */
export function SourceLinks({ blob, history, reason }: SourceLinkPair & { reason?: string }) {
  if (blob === undefined && history === undefined) {
    return (
      <span
        className="faint"
        title="No repository provenance for this project (not a clean GitHub checkout, or the sha/path is unknown), so the source file can't be linked. Serve the project from a Git source to enable source links."
      >
        {/* `reason` lets a caller name the ACTUAL missing ingredient (e.g. a path the shell
            didn't provide) instead of blaming provenance — same loud affordance, honest copy. */}
        source link unavailable ({reason ?? "no repo provenance"})
      </span>
    );
  }
  return (
    <>
      {blob !== undefined ? <ExtLink href={blob}>View source</ExtLink> : null}
      {blob !== undefined && history !== undefined ? " " : null}
      {history !== undefined ? <ExtLink href={history}>History</ExtLink> : null}
    </>
  );
}

/**
 * A Langfuse trace link with the three-state loud degradation (#718 §A): a resolvable link, or a
 * distinct-copy note for each un-linkable reason (no observer / unreachable / no trace yet). The
 * verdict comes from `traceLinkState`; this component is the SINGLE source of truth for the copy
 * every trace-link surface reads — including the run inspector's manifest-correlation panel, which
 * renders its degradation branches through this same component (`variant="descriptive"`). The
 * `descriptive` variant carries the full-sentence panel copy; `compact` (the default) is the terse
 * form for table cells and tight inline link rows.
 */
export function TraceLink({
  state,
  label = "Trace",
  variant = "compact",
}: {
  state: TraceLinkState;
  label?: string;
  variant?: "compact" | "descriptive";
}) {
  const descriptive = variant === "descriptive";
  switch (state.kind) {
    case "link":
      return <ExtLink href={state.href}>{label}</ExtLink>;
    case "unreachable":
      return (
        <span className="faint" title="The observability backend could not be reached; the trace link is unavailable until it recovers.">
          {descriptive ? "Observability backend unreachable." : "observer unreachable"}
        </span>
      );
    case "no-trace":
      return (
        <span className="faint" title="No trace has been recorded for this execution yet — it may not have started, or the manifest hasn't been written.">
          {descriptive ? "No trace recorded yet for this execution." : "no trace yet"}
        </span>
      );
    case "none":
      return (
        <span className="faint" title="No Langfuse observer is configured, so runs are not traced. Configure an observer to get trace links.">
          {descriptive
            ? state.observer
              ? `Observer: ${state.observer} — runs are not traced.`
              : "No observer configured — runs are not traced."
            : "not traced"}
        </span>
      );
  }
}
