/**
 * GitHub-provenance derivation (#727 §1/§6/§7): pure derivation of the console's GitHub-vs-served
 * drift class, the plan→approving-PR link, and the change-correlated insight from the contract's
 * `GithubProvenance` envelope. Same contract as `driftFeed.ts` / `enforcementFeed.ts` — every row,
 * notice, badge, and prose the surfaces render is computed here (testable in isolation), never
 * buried in JSX. The Drift page section, the Overview insight feed, and the Deployments plan cards
 * all read from this one module so they can never disagree on a verdict.
 *
 * Honesty rules this module encodes:
 * - `head.ahead_of_served === null` is UNKNOWN (the served checkout sha is unresolved), rendered as
 *   an explicit unknown row — never a false "in sync".
 * - `partial.github` degrades LOUDLY: `not_configured` explains the token/repo-provenance posture,
 *   `rate_limited` / `unreachable` warn that HEAD drift and PR links couldn't be read this cycle.
 *   Plans still list from local plan files even when the network portion is degraded.
 * - A plan's PR link is only shown when the server RESOLVED one; `sha`-null (untracked plan file)
 *   and `pr`-null (no approving PR found) both degrade to an explicit "no PR provenance" note,
 *   never a fabricated link.
 * - The evidence hand-off is built ONLY through `links.ts`'s host-gated builders (#719/#722) — a PR
 *   or compare URL that fails the GitHub host gate degrades to no link, never a hand-rolled href.
 */

import type { GithubPartialState, GithubProvenance, HeadProvenance, PlanProvenance } from "./api";
import type { CapabilityAbsentCopy, FeedNotice } from "./enforcementFeed";
import type { Insight } from "./insights";
import { sortInsights } from "./insights";
import { githubCompareUrl, validatedGithubRepoUrl, type RepoProvenance } from "./links";

/** Server-side cap on the reported commits-behind count (mirror of `MAX_COMMITS_BEHIND` in
 * `packages/python/src/typeflux/project/github_provenance.py`): at the cap the count is
 * shown as an honest "N+", never a precise-looking number the server never computed. */
export const MAX_COMMITS_BEHIND = 100;

// ── loud degradation: the partial-result banner ──────────────────────────────

/**
 * The loud partial-result banner for the GitHub source's reachability, or null when it was read in
 * full (`ok`). `not_configured` is a neutral-but-explicit scope statement (the surface is off by
 * configuration); `rate_limited` / `unreachable` are warnings (the data may be MISSING this cycle).
 * Shares the {@link FeedNotice} shape with the enforcement feed so both surfaces render through the
 * one shared `NoticeRow`.
 */
export function githubPartialNotice(state: GithubPartialState): FeedNotice | null {
  switch (state) {
    case "not_configured":
      return {
        badgeKind: "neutral",
        badge: "github not configured",
        message:
          "No GitHub token or repository provenance is configured server-side, so HEAD-vs-served " +
          "drift and plan→PR links can't be read. Set TYPEFLUX_GITHUB_TOKEN (or GITHUB_TOKEN) on " +
          "the control plane and serve the project from a GitHub source to enable this surface.",
      };
    case "rate_limited":
      return {
        badgeKind: "warning",
        badge: "github rate limited",
        message:
          "The GitHub API rate limit was reached — HEAD-vs-served drift and PR links could not be " +
          "read this cycle. They refresh automatically once the limit resets.",
      };
    case "unreachable":
      return {
        badgeKind: "warning",
        badge: "github unreachable",
        message:
          "GitHub could not be reached, so HEAD-vs-served drift and PR provenance could not be " +
          "read. Approved plans still list below from the local plan files.",
      };
    case "ok":
      return null;
  }
}

/**
 * The capability-absent panel copy (#577 §6): when the control plane does not advertise
 * `github_provenance`, nothing is fetched and the panel says why. Same split-around-the-token shape
 * the enforcement feed uses, so the module owns every rendered word while the JSX keeps the code
 * styling on the capability token.
 */
export function githubCapabilityAbsentCopy(): CapabilityAbsentCopy {
  return {
    badge: "not supported",
    before:
      "GitHub provenance is not supported by this control plane — it does not advertise the ",
    capability: "github_provenance",
    after:
      " capability for this project. It appears once the project is served from a GitHub source " +
      "and the control plane has a GitHub token (TYPEFLUX_GITHUB_TOKEN or GITHUB_TOKEN).",
  };
}

// ── commits-behind display (honest at the cap) ───────────────────────────────

/** The behind-count phrase for a HEAD-drift row: an exact count, an honest "N+" at the server cap,
 * or "some commits" when the server reports drift but not a count (compare not attempted). */
export function commitsBehindLabel(commitsBehind: number | null | undefined): string {
  if (commitsBehind == null) return "some commits";
  if (commitsBehind >= MAX_COMMITS_BEHIND) return `${MAX_COMMITS_BEHIND}+ commits`;
  return `${commitsBehind} commit${commitsBehind === 1 ? "" : "s"}`;
}

// ── change-correlated evidence link (§7) ─────────────────────────────────────

/**
 * The GitHub compare URL for the drift — `served_sha...remote_head` (the commits the served
 * checkout hasn't picked up, #727 §7) — or undefined when the project carries no host-gated GitHub
 * repo, no served sha, or the remote HEAD sha is unknown. Built ONLY through the shared host gate,
 * never a hand-rolled href.
 */
export function githubCompareHref(
  project: RepoProvenance | undefined,
  head: HeadProvenance | null | undefined,
): string | undefined {
  const repoUrl = validatedGithubRepoUrl(project?.repo_url);
  const base = project?.repo_sha;
  const target = head?.sha;
  if (repoUrl === undefined || !base || !target) return undefined;
  return githubCompareUrl(repoUrl, base, target);
}

// ── the GitHub-vs-served drift feed (§1) ─────────────────────────────────────

export interface GithubDriftFeed {
  /** Severity-ranked drift rows: the HEAD-drift warning (§1/§7), the unknown-state row, or the
   * in-sync confirmation. Empty when the network portion is degraded (the notice carries it). */
  rows: Insight[];
  /** The loud partial banner, or null when the GitHub source was read in full. */
  notice: FeedNotice | null;
}

// The remediation surface: the Overview page's Projects table carries a per-project "refresh" action
// that re-fetches a Git-sourced project's repository server-side. Every HEAD-drift row links to the
// Overview page (matching how plan drift links to Deployments).
const REFRESH_LINK = "#/";

/**
 * The real server-side project-refresh invocation (#256), documented at
 * `docs/control-plane.md` (`POST /api/v1/projects/{project}/refresh` — re-clones a Git-sourced
 * project and re-answers provenance; requires the `project.refresh` permission). Named in the
 * remediation copy because the console's per-project "refresh" BUTTON only exists on MULTI-project
 * consoles — the Overview Projects table is gated on more than one registered project — so a
 * single-project operator has no UI control and needs the API/CLI path. Kept in this tested layer
 * so the remediation never again points every operator at a control half of them can't see.
 */
export const PROJECT_REFRESH_ENDPOINT = "POST /api/v1/projects/{project}/refresh";

/**
 * The GitHub-vs-served drift rows from one provenance read. When `head` is present:
 * `ahead_of_served === true` is a warning (undeployed changes, with a compare-view evidence link);
 * `false` is an ok confirmation; `null` is an explicit UNKNOWN (never in-sync). When `head` is null
 * but the source was read in full, the remote HEAD was unreadable — a neutral info row; when the
 * source is degraded, the notice carries it and no row is emitted (the notice is the message).
 */
export function deriveGithubDrift(
  provenance: GithubProvenance,
  project?: RepoProvenance,
): GithubDriftFeed {
  const notice = githubPartialNotice(provenance.partial.github);
  const head = provenance.head ?? null;
  const rows: Insight[] = [];

  if (head) {
    const branch = head.branch;
    if (head.ahead_of_served === true) {
      const compare = githubCompareHref(project, head);
      rows.push({
        id: "github:head-drift",
        severity: "warning",
        title: `Served checkout is ${commitsBehindLabel(head.commits_behind)} behind ${branch} HEAD`,
        detail:
          "The project changed in GitHub but the served checkout hasn't picked it up. Refresh the " +
          "project so the control plane re-clones it: on a multi-project console the Overview " +
          "Projects panel has a per-project refresh action; on any console call " +
          `${PROJECT_REFRESH_ENDPOINT} (needs the project.refresh permission) or pull the checkout ` +
          "on the worker. Then regenerate and re-approve any plans that depend on the new code.",
        link: REFRESH_LINK,
        ...(compare ? { evidence: { href: compare, label: "Compare on GitHub" } } : {}),
      });
    } else if (head.ahead_of_served === false) {
      rows.push({
        id: "github:in-sync",
        severity: "ok",
        title: `Served checkout matches ${branch} HEAD`,
        detail: "The control plane is serving the latest commit on the tracked branch — no undeployed changes.",
        link: REFRESH_LINK,
      });
    } else {
      rows.push({
        id: "github:unknown",
        severity: "info",
        title: `GitHub drift unknown for ${branch}`,
        detail:
          "The served checkout sha is unresolved (the source has not been cloned yet), so drift " +
          "against the remote HEAD cannot be computed. This is never reported as in sync — refresh " +
          "the project once it resolves to establish a baseline.",
        link: REFRESH_LINK,
      });
    }
  } else if (provenance.partial.github === "ok") {
    // Read in full, but no HEAD to compare: the remote HEAD could not be read or there is no served
    // GitHub provenance. Neutral-but-explicit — never a silent absence.
    rows.push({
      id: "github:head-unavailable",
      severity: "info",
      title: "Remote HEAD unavailable",
      detail:
        "The remote branch HEAD could not be read for this project, so HEAD-vs-served drift is " +
        "unavailable this cycle. Approved plans below still carry their PR provenance where known.",
      link: REFRESH_LINK,
    });
  }

  return { rows: sortInsights(rows), notice };
}

/**
 * The change-correlated GitHub insights for a shared feed (the Overview insight list, §7): only the
 * ACTIONABLE rows (the HEAD-drift warning and the unknown-state info), never the ok "in sync"
 * confirmation, which would be noise in a findings feed. Empty when the source is degraded (the
 * loud partial banner lives on the Drift page's dedicated section, not the Overview feed).
 */
export function githubChangeInsights(
  provenance: GithubProvenance,
  project?: RepoProvenance,
): Insight[] {
  return deriveGithubDrift(provenance, project).rows.filter((row) => row.severity !== "ok");
}

// ── plan → approving PR (§6) ─────────────────────────────────────────────────

export type PlanPrLink =
  | { kind: "pr"; number: number; url: string | undefined; mergedAt: string | null }
  | { kind: "none"; reason: string; tooltip: string };

/** The tooltip for a resolved PR whose recorded URL failed the GitHub host gate (#719/#722): the
 * number renders as text, never an unsafe link. Owned here (the module's tested-layer promise) so
 * the Deployments cell renders no hand-written copy. */
export const PR_URL_NOT_LINKED_TOOLTIP =
  "The recorded PR URL is not a recognized GitHub web URL, so it isn't linked.";

/** The tooltip for a resolved-but-unmerged approving PR (an open PR that touched the commit). */
export const PR_NOT_MERGED_TOOLTIP =
  "The approving PR is recorded but not marked merged (an open PR that touched this commit).";

/**
 * The pr-null branch (#727 §6, F3): a plan with a resolved `sha` but no `pr` on the wire. The wire
 * CANNOT distinguish a plan the server looked up and found no PR for from one that fell outside the
 * server's `PLAN_PR_LOOKUP_CAP` (tail plans are listed WITHOUT a PR lookup) or one whose lookup
 * never ran because the GitHub source was degraded — `PlanProvenance` carries only
 * `plan_id`/`sha`/`pr`, with no "looked-up" marker. So the copy must never assert a confident "no
 * approving PR found" when the lookup may not have completed:
 * - `partial != ok` → honest neutral: the PR lookup could not complete this cycle (degraded
 *   source), NEVER a false "none found";
 * - `partial == ok` → the residual ambiguity is the lookup cap (a tail plan beyond the newest
 *   `PLAN_PR_LOOKUP_CAP` is listed without a lookup), so the inline copy keeps "no approving PR
 *   found" while the tooltip discloses the cap caveat.
 */
function planPrAbsent(partial: GithubPartialState): PlanPrLink {
  switch (partial) {
    case "not_configured":
      return {
        kind: "none",
        reason: "PR lookup unavailable — GitHub not configured",
        tooltip:
          "No GitHub token or repository provenance is configured server-side, so the approving PR " +
          "for this commit was never looked up — this is not a confirmed 'no PR'.",
      };
    case "rate_limited":
      return {
        kind: "none",
        reason: "PR lookup didn't complete — GitHub rate limited",
        tooltip:
          "The GitHub API rate limit was reached, so the approving PR for this commit could not be " +
          "read this cycle — this is not a confirmed 'no PR'. It resolves once the limit resets.",
      };
    case "unreachable":
      return {
        kind: "none",
        reason: "PR lookup didn't complete — GitHub unreachable",
        tooltip:
          "GitHub could not be reached, so the approving PR for this commit could not be read this " +
          "cycle — this is not a confirmed 'no PR'. Retry once the control plane can reach GitHub.",
      };
    case "ok":
      return {
        kind: "none",
        reason: "no approving PR found for this commit",
        tooltip:
          "No approving PR was found for this plan's commit. The server looks up PRs only for the " +
          "most-recent plans (a lookup cap), so an older plan beyond that cap is listed without a " +
          "PR lookup rather than confirmed to have none.",
      };
  }
}

/**
 * The approving-PR hand-off for one deployment plan (#727 §6). `sha`-null means the plan file is
 * untracked/uncommitted (no commit to resolve); `pr`-null degrades through {@link planPrAbsent},
 * which threads the envelope's `partial` so a lookup that never completed (degraded source) — or an
 * older plan beyond the server's PR-lookup cap — is never mislabelled a confident "no PR found"
 * (F3). Both none-cases carry an explicit reason + tooltip, never a fabricated link. When a PR is
 * present, its URL is host-gated (#719/#722): a URL that fails the GitHub host gate yields
 * `url: undefined`, so the number renders as text without an unsafe link.
 *
 * `partial` defaults to `ok` for callers that have already gated on a settled, read-in-full query.
 */
export function derivePlanPr(
  entry: PlanProvenance | undefined,
  partial: GithubPartialState = "ok",
): PlanPrLink {
  if (!entry) {
    return {
      kind: "none",
      reason: "provenance unavailable for this plan",
      tooltip:
        "This plan was not present in the GitHub-provenance read, so no approving PR could be " +
        "resolved for it.",
    };
  }
  if (entry.sha == null) {
    return {
      kind: "none",
      reason: "untracked plan file — no commit to resolve to a PR",
      tooltip:
        "The plan file is untracked/uncommitted in the served checkout, so there is no commit to " +
        "resolve to an approving PR.",
    };
  }
  const pr = entry.pr ?? null;
  if (!pr) return planPrAbsent(partial);
  return {
    kind: "pr",
    number: pr.number,
    url: validatedGithubRepoUrl(pr.url),
    mergedAt: pr.merged_at ?? null,
  };
}

/** A `plan_id → PlanProvenance` lookup from one provenance read, so the Deployments page resolves
 * every plan card's PR link from a SINGLE fetch (no per-card N+1). */
export function planProvenanceById(
  provenance: GithubProvenance | undefined,
): Map<string, PlanProvenance> {
  return new Map((provenance?.plans ?? []).map((plan) => [plan.plan_id, plan] as const));
}
