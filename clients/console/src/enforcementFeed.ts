/**
 * Enforcement-feed engine (#723 slice 3): pure derivation of the policy
 * violations & enforcement feed from the contract's `EnforcementEventList` —
 * the `insights.ts` contract: every verdict, badge, and notice the feed
 * renders is computed here (testable in isolation), never buried in JSX.
 *
 * Honesty rules this module encodes:
 * - `partial.langfuse` degrades LOUDLY: `unreachable` warns that runtime
 *   events may be missing; `not_configured` says the runtime portion is not
 *   available at all. Neither is ever silent.
 * - An empty feed is only an all-clear when `partial` is `ok`; otherwise the
 *   confirmation covers admission events alone.
 * - An admission event has no `occurred_at` by design (it reflects current
 *   admission state, not a recorded past instant) — rendered as an explicit
 *   "—", never a fabricated timestamp.
 * - Events whose environment came from the server's fallback attribution are
 *   indistinguishable client-side (#723 design note) — the feed renders what
 *   the API serves, no invented markers.
 */

import type {
  EnforcementEvent,
  EnforcementEventList,
  EnforcementPartial,
  EnforcementVerdict,
} from "./api";
import { langfuseBaseOf, traceLinkState, type TraceLinkState } from "./links";
import type { BundleCell } from "./queries";

// ── time-window presets ───────────────────────────────────────────────────────

export type WindowPresetId = "24h" | "7d" | "30d";

export const WINDOW_PRESETS: ReadonlyArray<{ id: WindowPresetId; label: string; hours: number }> = [
  { id: "24h", label: "last 24 hours", hours: 24 },
  { id: "7d", label: "last 7 days", hours: 24 * 7 },
  { id: "30d", label: "last 30 days", hours: 24 * 30 },
];

/**
 * The ISO `since` bound for a window preset. Callers must FREEZE the result
 * per filter set (not recompute per fetch): the server binds a pagination
 * cursor to a fingerprint of the raw filters it was minted under, so every
 * load-more request must repeat the exact `since` of page 1 (422 otherwise).
 */
export function windowSince(preset: WindowPresetId, now: Date = new Date()): string {
  const hours = WINDOW_PRESETS.find((window) => window.id === preset)?.hours ?? 24 * 7;
  return new Date(now.getTime() - hours * 3_600_000).toISOString();
}

/** The feed's client-side filter set; `null` means unfiltered. The verdict union
 * aliases the generated contract type rather than restating it by hand (#723 F6c),
 * so a new verdict on the wire can't silently diverge from the filter. */
export interface EnforcementFilters {
  workflowId: string | null;
  verdict: EnforcementVerdict | null;
  window: WindowPresetId;
}

// ── infinite-query identity (the frozen-filter-set contract) ─────────────────

/**
 * The cursor-feed's query key. It covers EXACTLY the raw-filter fields the server
 * binds each page cursor to — see `filter_fingerprint` in
 * `packages/python/src/typeflux/project/enforcement.py`, which
 * fingerprints: `w` workflow_ids, `e` environment_id, `v` verdicts, `p`
 * policy_ids, `s` since (raw), `u` until (raw). This client varies only
 * env / workflowId / verdict / since; it never sends `policy_ids` and
 * deliberately never sends `until` (the server resolves it to `now()` per
 * request and fingerprints the raw absent value), so those two stay constant and
 * out of the key. Holding this key aligned to the fingerprint is what lets
 * "load more" reuse the frozen `since` (same key, new cursor) instead of drawing
 * a 422 on fingerprint drift.
 */
export function enforcementQueryKey(env: string, filters: EnforcementFilters, since: string) {
  return [
    "enforcement-events",
    env,
    filters.workflowId ?? "",
    filters.verdict ?? "",
    since,
  ] as const;
}

/**
 * The per-page fetch params for one cursor within a frozen filter set: every
 * "load more" repeats the SAME since / workflowId / verdict and advances only
 * `cursor`, because the server 422s if the fingerprinted filters shift between a
 * cursor's mint and its use.
 */
export function enforcementPageParams(
  filters: EnforcementFilters,
  since: string,
  cursor: string | null,
): { workflowId: string | null; verdict: EnforcementVerdict | null; since: string; cursor: string | null } {
  return { workflowId: filters.workflowId, verdict: filters.verdict, since, cursor };
}

// ── partial-result (loud degradation) ────────────────────────────────────────

export type PartialLangfuse = EnforcementPartial["langfuse"];

/**
 * Loudest-wins combination of per-page partial markers: with "load more"
 * pagination each page reports its own reachability, and a feed that was
 * partial on ANY page must not read as complete because the last page was ok.
 */
export function combinePartial(pages: Array<Pick<EnforcementEventList, "partial">>): PartialLangfuse {
  const states = pages.map((page) => page.partial.langfuse);
  if (states.includes("unreachable")) return "unreachable";
  if (states.includes("not_configured")) return "not_configured";
  return "ok";
}

export interface FeedNotice {
  badgeKind: "ok" | "warning" | "neutral";
  badge: string;
  message: string;
}

/**
 * The loud partial-result banner, or null when both sources were read in full.
 * `unreachable` is a warning (data may be MISSING); `not_configured` is a
 * neutral-but-explicit scope statement (the runtime portion does not exist
 * for this observer configuration).
 */
export function partialNotice(partial: PartialLangfuse): FeedNotice | null {
  if (partial === "unreachable") {
    return {
      badgeKind: "warning",
      badge: "observer unreachable",
      message:
        "The Langfuse backend could not be reached — runtime enforcement events " +
        "(moderation blocks) may be missing from this feed. Admission events are complete.",
    };
  }
  if (partial === "not_configured") {
    return {
      badgeKind: "neutral",
      badge: "runtime not traced",
      message:
        "The resolved observer is not Langfuse, so runtime enforcement events are not " +
        "available — this feed covers admission verdicts only.",
    };
  }
  return null;
}

/**
 * The explicit empty-feed confirmation: a green all-clear ONLY when both
 * sources were read in full (`partial` ok); otherwise the confirmation is
 * scoped to admission events, with the partial banner carrying the caveat.
 */
export function emptyFeedNotice(partial: PartialLangfuse): FeedNotice {
  if (partial === "ok") {
    return {
      badgeKind: "ok",
      badge: "no enforcement events",
      message: "No enforcement events in this window — admission and runtime sources were both read.",
    };
  }
  return {
    badgeKind: "neutral",
    badge: "no admission events",
    message:
      "No admission enforcement events in this window. The runtime portion could not be " +
      "read in full — see the note above.",
  };
}

// ── rendered copy (all feed prose is computed here) ──────────────────────────

/**
 * The `source` badge tooltip: an admission verdict is normalized from the
 * validation surface (a current would-be-rejected state); a runtime verdict is
 * read from a Langfuse trace (an event that happened during execution).
 */
export function sourceBadgeTitle(source: EnforcementEvent["source"]): string {
  return source === "admission"
    ? "An admission-time verdict, normalized from the validation surface: this workflow would be rejected now."
    : "A runtime verdict read from the Langfuse trace (e.g. a moderation block during execution).";
}

/**
 * The `verdict` badge tooltip: `blocked` is a runtime moderation block;
 * `rejected` is an admission-time fail-closed under the applied policy.
 */
export function verdictBadgeTitle(verdict: EnforcementVerdict): string {
  return verdict === "blocked"
    ? "The action was blocked at runtime by policy (moderation on_violation=block)."
    : "The workflow is rejected at admission under the applied policy — starts fail closed.";
}

/**
 * The capability-absent panel copy (#723 F4): when the control plane does not
 * advertise `enforcement_events`, the feed fetches nothing and says why. Split
 * around the capability token the panel renders as `<Mono>`, so the module still
 * owns every rendered word (its header contract) while the JSX keeps the code
 * styling.
 */
export interface CapabilityAbsentCopy {
  badge: string;
  before: string;
  capability: string;
  after: string;
}

export function capabilityAbsentCopy(): CapabilityAbsentCopy {
  return {
    badge: "not supported",
    before:
      "Enforcement events are not supported by this control plane — it does not advertise the ",
    capability: "enforcement_events",
    after:
      " capability for this project. Admission verdicts still appear as validation issues on each " +
      "workflow page; runtime blocks live in the Langfuse traces.",
  };
}

// ── row derivation ────────────────────────────────────────────────────────────

/**
 * Where one event's evidence lives: a runtime event hands off to its Langfuse
 * trace (the three-state loud degradation of `traceLinkState`); an admission
 * event is a *current* verdict whose evidence is the workflow's validation
 * section. `none` when an admission event names no workflow to link.
 */
export type EnforcementEvidenceLink =
  | { kind: "trace"; state: TraceLinkState }
  | { kind: "validation"; workflowId: string; section: "validation" }
  | { kind: "none" };

export interface EnforcementRow {
  key: string;
  /** Trace timestamp for runtime events; null for admission events (point-in-time now). */
  occurredAt: string | null;
  source: EnforcementEvent["source"];
  verdict: EnforcementVerdict;
  rule: string;
  workflowId: string | null;
  policyIds: string[];
  detail: string;
  evidence: EnforcementEvidenceLink;
}

function evidenceOf(
  event: EnforcementEvent,
  langfuseBase: string | null,
  partialLangfuse: PartialLangfuse,
): EnforcementEvidenceLink {
  if (event.source === "runtime") {
    // A runtime event only exists because a trace was read; still, degrade through
    // the shared three-state helper so the copy matches every other trace surface.
    if (partialLangfuse === "not_configured") return { kind: "trace", state: { kind: "none" } };
    return {
      kind: "trace",
      state: traceLinkState({
        langfuseBase,
        reachable: partialLangfuse === "unreachable" ? false : undefined,
        traceId: event.evidence?.trace_id ?? null,
      }),
    };
  }
  // Admission evidence is the workflow's current validation report. The row carries
  // STRUCTURED link data (#723 F3); the JSX renders a TanStack <Link> that encodes
  // workflow id + env itself, so no hand-built href with unencoded values exists.
  if (event.workflow_id) {
    return { kind: "validation", workflowId: event.workflow_id, section: "validation" };
  }
  return { kind: "none" };
}

/**
 * One renderable row per served event, in served order (the server sorts
 * admission-first, then runtime by recency). No client-side re-derivation of
 * verdicts — `policy_ids` is the RECORDED applied-policy provenance, and an
 * environment attributed by the server's fallback is rendered as served.
 */
export function deriveEnforcementRows(
  events: EnforcementEvent[],
  input: { langfuseBase: string | null; partialLangfuse: PartialLangfuse },
): EnforcementRow[] {
  return events.map((event, index) => ({
    key: `${event.source}:${event.rule}:${event.workflow_id ?? ""}:${event.occurred_at ?? ""}:${index}`,
    occurredAt: event.occurred_at ?? null,
    source: event.source,
    verdict: event.verdict,
    rule: event.rule,
    workflowId: event.workflow_id ?? null,
    policyIds: event.policy_ids ?? [],
    detail: event.detail,
    evidence: evidenceOf(event, input.langfuseBase, input.partialLangfuse),
  }));
}

/**
 * All feed rows across the loaded pages, each page's rows derived with THAT
 * page's own partial marker (#723 F1). Load-more pagination means a later page
 * can go `unreachable` while earlier pages were read in full; deriving from the
 * FLATTENED events under one combined partial would retroactively downgrade an
 * earlier page's WORKING trace links to unreachable placeholders — masking real
 * evidence behind an unrelated later failure. Per-page derivation keeps each
 * page's evidence honest; the feed-wide banner still uses {@link combinePartial}
 * (loudest-wins) so the caveat is shown ONCE, not smeared onto every earlier row.
 *
 * Page index prefixes the row key so two identical events on different pages
 * cannot collide (the per-page key index resets each page).
 */
export function deriveFeedRows(
  pages: Array<Pick<EnforcementEventList, "events" | "partial">>,
  input: { langfuseBase: string | null },
): EnforcementRow[] {
  return pages.flatMap((page, pageIndex) =>
    deriveEnforcementRows(page.events, {
      langfuseBase: input.langfuseBase,
      partialLangfuse: page.partial.langfuse,
    }).map((row) => ({ ...row, key: `${pageIndex}:${row.key}` })),
  );
}

/**
 * The Langfuse project base for the feed's evidence links, from the resolved
 * bundle matrix: the first resolved bundle in the SELECTED environment that
 * configures one (`links.langfuse_project` is per-environment operator
 * config, so any resolved bundle in the env carries the same base when set).
 * Null when nothing resolved or no bundle configures a base — the trace
 * evidence then degrades through `traceLinkState`'s explicit states.
 */
export function langfuseBaseFromCells(cells: BundleCell[], env: string): string | null {
  for (const cell of cells) {
    if (cell.env !== env) continue;
    const base = langfuseBaseOf(cell.bundle);
    if (base) return base;
  }
  return null;
}

/**
 * The workflow ids that RESOLVE in the selected environment (a bundle came back),
 * for scoping the feed's workflow filter (#723 F6d): a workflow that does not
 * resolve here can never yield an enforcement event in this env, so offering it
 * as a filter option is noise. One filter over the bundle matrix both mounts
 * already hold, in the matrix's order and de-duplicated.
 */
export function resolvedWorkflowIdsInEnv(cells: BundleCell[], env: string): string[] {
  const ids: string[] = [];
  for (const cell of cells) {
    if (cell.env === env && cell.bundle && !ids.includes(cell.workflowId)) {
      ids.push(cell.workflowId);
    }
  }
  return ids;
}
