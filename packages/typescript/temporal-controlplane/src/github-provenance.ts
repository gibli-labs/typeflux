/**
 * GitHub provenance reads (#727; Python `project/github_provenance.py`): a normalized,
 * read-at-request surface relating the code the control plane *serves* to the code in GitHub —
 * HEAD-vs-served drift (#577 §1) and plan → approving-PR provenance (#577 §6).
 *
 * This module holds the PURE logic ported from Python — the host gate, served/plan extraction, and
 * the response assembly — plus the reachability vocabulary and the transport's result shape. The one
 * impurity (the GitHub REST read) lives behind the injected seam in `github-transport.ts`, exactly
 * like the Langfuse transport (#573), so tests replace it with a fixture.
 *
 * EDITION-HONEST PARITY (the git-capability decision, #727 slice 2): the TS control plane serves
 * LOCAL project checkouts only — its registry rejects Git `repo:` sources and runs NO clone and NO
 * git subprocess (`http/registry.ts`). Python resolves each plan's `sha` with `git log -1 -- <file>`
 * in the served CLONE and the served-side drift from the registry's recorded git provenance; the TS
 * edition has neither capability, and shelling out to git is NOT an established TS-CP pattern. So the
 * honest port is: no recorded git source ⇒ `served` is null ⇒ the surface reports `not_configured`
 * with NO network call, plans list with `sha`/`pr` null, and the `github_provenance` capability is
 * false (its surface factor — a recorded github repo source — is structurally absent here). The full
 * transport + assembly logic is ported all the same (behavioral parity + cross-referenced with
 * Python), so the surface lights up automatically if the TS registry ever gains git sources, and the
 * unit/adapter tests exercise every hardened path through the injected seam.
 *
 * The host gate {@link parseGithubRepo} is the server-side twin of the console's
 * `validatedGithubRepoUrl` (`clients/console/src/links.ts`) and of Python's `parse_github_repo` —
 * all three anchor the REGISTRABLE domain (`github.com` / `*.github.com`) so `github.com.evil.com`
 * never passes. Keep the three in lockstep; a change to the accepted-host rule travels to all.
 */

import type { components } from "./http/contract.js";

/** The most-recent approved plans whose PR is looked up per request — the fan-out bound (Python
 * `PLAN_PR_LOOKUP_CAP`). Plans are ordered newest-first; the tail is listed WITHOUT a PR lookup
 * (`pr: null`), never dropped. */
export const PLAN_PR_LOOKUP_CAP = 20;
/** Hard cap on the reported `commits_behind` so a long-diverged branch reports a bounded, honest
 * "100+" rather than a huge literal (Python `MAX_COMMITS_BEHIND`). */
export const MAX_COMMITS_BEHIND = 100;
/** Max concurrent plan-PR lookups (Python `PLAN_PR_LOOKUP_CONCURRENCY`): the capped plan set is
 * fanned out through a bounded pool rather than issued serially. */
export const PLAN_PR_LOOKUP_CONCURRENCY = 6;

/**
 * The reachability vocabulary the surface degrades into (`partial.github`; Python `GithubStatus`):
 *  - `not_configured` — no server-side token, OR no recorded GitHub repo source. NO network call.
 *  - `rate_limited`   — GitHub answered a 403 rate-limit signal, or a 429.
 *  - `unreachable`    — any other genuine transport/HTTP failure (incl. a malformed HEAD payload).
 *  - `ok`             — the reads succeeded.
 *
 * ALIASED off the generated contract schema (`GithubPartial.github`), exactly as `enforcement.ts`
 * derives `LangfuseEnforcementStatus` — a contract regen that changes the vocabulary breaks this
 * compile instead of silently drifting the edition off the shared OpenAPI surface.
 */
export type GithubStatus = components["schemas"]["GithubPartial"]["github"];

/** A GitHub repository parsed from a recorded `repo_url` (Python `GithubRepo`). */
export interface GithubRepo {
  owner: string;
  repo: string;
  url: string;
}

/** The served side of the drift comparison (Python `ServedProvenance`): the repo, the served
 * branch/ref (always set), and the checkout `sha` the registry recorded (null when unresolved). */
export interface ServedProvenance {
  repo: GithubRepo;
  branch: string;
  servedSha: string | null;
}

/** One approved plan reduced to what the surface needs (Python `PlanRef`): its id and the commit
 * `sha` of its plan FILE's last commit in the served clone (null when untracked/uncommitted — or,
 * on the TS edition, always null: there is no served clone to run `git log` against). */
export interface PlanRef {
  planId: string;
  sha: string | null;
}

/** The approving PR for a plan's commit — the INTERNAL camelCase shape (Python `PullRequestRef`).
 * Named `PlanPullRequest` here to free the bare `PullRequestRef` name for the generated wire alias
 * below (the #727 F4 name-collision resolution). `mergedAt` is set for a merged PR (the audit case),
 * null for an open/unmerged PR that still touched the commit; {@link buildGithubProvenance}
 * serializes it to the snake_case wire shape (`merged_at`). */
export interface PlanPullRequest {
  number: number;
  url: string;
  mergedAt: string | null;
}

/**
 * The transport seam's result (Python `GithubReadResult`): a reachability `status` (mapped straight
 * into `partial`), the remote HEAD `sha` (null when the HEAD read itself did not succeed; set even
 * under a degraded `status` when HEAD was read before a later failure), the bounded `commitsBehind`
 * (null when not computed), and the resolved PRs keyed by plan commit `sha` (only shas whose PR was
 * found appear).
 */
export interface GithubReadResult {
  status: GithubStatus;
  headSha?: string | null;
  commitsBehind?: number | null;
  planPrs?: Readonly<Record<string, PlanPullRequest>>;
}

// --- Wire shapes: ALIASES of the generated contract schemas (`http/contract.ts`), never hand-
//     restated — so a contract regen that renames/retypes a field breaks THIS compile instead of
//     silently drifting the edition off the shared OpenAPI surface (exactly as `enforcement.ts`
//     aliases its DTOs). The generated optional fields are `T | null`; the assembler still OMITS
//     absent fields (never emits `null`) so `JSON.stringify` reproduces Python's
//     `response_model_exclude_none` — an omitted key satisfies `field?: T | null`. -----------------

/** The `head` block on the wire (`HeadProvenance`); `ahead_of_served`/`commits_behind` omitted when null. */
export type HeadProvenanceWire = components["schemas"]["HeadProvenance"];

/** One plan on the wire (`PlanProvenance`); `sha`/`pr` omitted when null. */
export type PlanProvenanceWire = components["schemas"]["PlanProvenance"];

/** A PR on the wire (the generated `PullRequestRef` — the name freed by renaming the internal
 * camelCase shape to {@link PlanPullRequest}); `merged_at` omitted when null. */
export type PullRequestRef = components["schemas"]["PullRequestRef"];

/** The github-provenance response envelope (`GithubProvenance`); `head` omitted when null. */
export type GithubProvenanceWire = components["schemas"]["GithubProvenance"];

// ---------------------------------------------------------------------------
// Credential scrubbing: strip userinfo before a repo URL enters the surface.
// ---------------------------------------------------------------------------

/**
 * Strip `user:token@` userinfo from a single repo URL, preserving the rest (Python
 * `controlplane/git_source.py` `scrub_git_url`). A registry remote may be a credential-bearing HTTPS
 * URL (`https://user:token@host/repo`); the clone would use it as-is, but it must NEVER reach a
 * control-plane response, diagnostic, or wire field. This is the choke point: {@link parseGithubRepo}
 * scrubs its input HERE before parsing/storing, so every path onto this surface — the host gate
 * itself, {@link servedProvenance}, and the `/meta` `github_provenance` capability check — is covered
 * and the stored {@link GithubRepo.url} is always credential-free. The ssh shorthand
 * `git@host:path` has no scheme (unparseable as a URL) and carries no token, so it is left as-is.
 */
export function scrubGitUrl(url: string): string;
export function scrubGitUrl(url: string | null | undefined): string | null | undefined;
export function scrubGitUrl(url: string | null | undefined): string | null | undefined {
  if (!url) return url;
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return url; // no scheme (e.g. ssh shorthand) → not a parseable URL, no userinfo to strip.
  }
  if (parsed.username === "" && parsed.password === "") return url; // nothing to scrub — return verbatim.
  parsed.password = ""; // clear password FIRST — an empty username with a set password still serializes `:pw@`.
  parsed.username = "";
  return parsed.toString();
}

// ---------------------------------------------------------------------------
// Pure helpers: repo-url host gate, served/plan extraction, feed assembly.
// ---------------------------------------------------------------------------

/**
 * Validate a recorded `repo_url` to a github.com WEB repo and split its `owner`/`repo`, or `null`.
 *
 * Mirrors the console host gate (`validatedGithubRepoUrl`, `clients/console/src/links.ts`) and
 * Python `parse_github_repo`: the host must be exactly `github.com` or a `*.github.com` subdomain
 * (anchoring the REGISTRABLE domain — a prefix match would pass `github.com.evil.com`), the scheme
 * must be http(s) (excludes `git@github.com:...` ssh shorthand and local paths), and the path must
 * carry `owner/repo`. Self-hosted GHE hosts are unverifiable and stay ungated (capability false).
 */
export function parseGithubRepo(repoUrl: string | null | undefined): GithubRepo | null {
  if (!repoUrl) return null;
  // Scrub credential userinfo FIRST (Python scrubs at each call site; the TS gate is the single choke
  // point every path routes through, so scrubbing here covers the host gate, `servedProvenance`, and
  // the `/meta` capability check at once, and guarantees the stored `url` is credential-free).
  const scrubbed = scrubGitUrl(repoUrl);
  if (!scrubbed) return null;
  let parsed: URL;
  try {
    parsed = new URL(scrubbed);
  } catch {
    return null;
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return null;
  const host = parsed.hostname;
  if (host === "" || !(host === "github.com" || host.endsWith(".github.com"))) return null;
  // owner/repo are the first two non-empty path segments; a trailing `.git` (rare on a web url but
  // possible on a recorded remote) is stripped.
  const segments = parsed.pathname.split("/").filter((segment) => segment.length > 0);
  if (segments.length < 2) return null;
  const owner = segments[0]!;
  let repo = segments[1]!;
  if (repo.endsWith(".git")) repo = repo.slice(0, -".git".length);
  if (owner === "" || repo === "") return null;
  return { owner, repo, url: scrubbed };
}

/**
 * Build the served side of the drift comparison, or `null` when the recorded source is not a github
 * repo (Python `served_provenance`) — the caller reports that as `not_configured` with no network
 * call, and the `github_provenance` capability is false for it.
 */
export function servedProvenance(options: {
  repoUrl: string | null | undefined;
  repoRef: string;
  repoSha: string | null;
}): ServedProvenance | null {
  const repo = parseGithubRepo(options.repoUrl);
  if (repo === null) return null;
  return { repo, branch: options.repoRef, servedSha: options.repoSha };
}

/**
 * The plans whose PR is worth looking up (Python `plan_refs`): the input is assumed newest-first
 * (the caller sorts by `generated_at`); the first `cap` with a non-null sha are the lookup set. A
 * pure selector over already-ordered refs — the assembler still lists EVERY plan, only the PR lookup
 * is bounded.
 */
export function planRefs(plans: readonly PlanRef[], cap: number = PLAN_PR_LOOKUP_CAP): PlanRef[] {
  const selected: PlanRef[] = [];
  for (const ref of plans) {
    if (ref.sha === null) continue;
    selected.push(ref);
    if (selected.length >= cap) break;
  }
  return selected;
}

/**
 * Compose the response from the served provenance, the ordered plans, and the reader's result
 * (Python `build_github_provenance`) — the one place the local and remote sources are merged.
 *
 * `head` is populated whenever the remote HEAD was actually READ (a head sha is present) —
 * independent of a later degradation, so a transport failure on a `compare`/plan-PR read AFTER HEAD
 * succeeded keeps the fetched HEAD (the degradation still shows in `partial.github`). It is omitted
 * only when the HEAD read itself did not succeed. `ahead_of_served` is omitted (Python: null) when
 * the served checkout sha is unresolved (drift UNKNOWN, never a false "in sync"). Every plan is
 * listed with its local `sha`; its `pr` comes from `readResult.planPrs` when the sha resolved, else
 * omitted (unknown/unreachable/no-sha — never fabricated). Null fields are OMITTED (Python's
 * `response_model_exclude_none`).
 */
export function buildGithubProvenance(options: {
  served: ServedProvenance | null;
  plans: readonly PlanRef[];
  readResult: GithubReadResult;
}): GithubProvenanceWire {
  const { served, plans, readResult } = options;
  const planPrs = readResult.planPrs ?? {};
  let head: HeadProvenanceWire | undefined;
  if (served !== null && readResult.headSha != null) {
    head = { branch: served.branch, sha: readResult.headSha };
    if (served.servedSha !== null) {
      // A bool (not null): the served checkout sha is known, so drift is determinate.
      head.ahead_of_served = readResult.headSha !== served.servedSha;
    }
    if (readResult.commitsBehind != null) head.commits_behind = readResult.commitsBehind;
  }
  const planWire: PlanProvenanceWire[] = plans.map((ref) => {
    const entry: PlanProvenanceWire = { plan_id: ref.planId };
    if (ref.sha !== null) {
      entry.sha = ref.sha;
      const pr = planPrs[ref.sha];
      if (pr !== undefined) entry.pr = pullRequestRefWire(pr);
    }
    return entry;
  });
  return {
    ...(head !== undefined ? { head } : {}),
    plans: planWire,
    partial: { github: readResult.status },
  };
}

/** Serialize a {@link PlanPullRequest} to its wire shape ({@link PullRequestRef}), omitting `merged_at` when null. */
function pullRequestRefWire(pr: PlanPullRequest): PullRequestRef {
  return { number: pr.number, url: pr.url, ...(pr.mergedAt !== null ? { merged_at: pr.mergedAt } : {}) };
}

// ---------------------------------------------------------------------------
// Payload normalizers shared with the transport (Python `_pull_request_ref` / `_select_pull_request`).
// ---------------------------------------------------------------------------

/**
 * Normalize one GitHub pull-request JSON object into a {@link PlanPullRequest} (Python
 * `_pull_request_ref`). A merged PR (`merged_at` present) is the audit case; an open PR that touched
 * the commit is still linked (`mergedAt: null`). Any shape it doesn't recognize yields `null`.
 */
export function planPullRequest(payload: unknown): PlanPullRequest | null {
  if (typeof payload !== "object" || payload === null) return null;
  const record = payload as Record<string, unknown>;
  const number = record["number"];
  const url = record["html_url"];
  if (typeof number !== "number" || !Number.isInteger(number) || typeof url !== "string") return null;
  const mergedAt = record["merged_at"];
  return { number, url, mergedAt: typeof mergedAt === "string" ? mergedAt : null };
}

/**
 * Choose the approving PR from the `/commits/{sha}/pulls` list (Python `_select_pull_request`):
 * prefer the merged one (the review record), else the first well-formed entry.
 */
export function selectPullRequest(payloads: unknown): PlanPullRequest | null {
  if (!Array.isArray(payloads)) return null;
  const refs = payloads.map((item) => planPullRequest(item)).filter((ref): ref is PlanPullRequest => ref !== null);
  if (refs.length === 0) return null;
  for (const ref of refs) {
    if (ref.mergedAt !== null) return ref;
  }
  return refs[0]!;
}
