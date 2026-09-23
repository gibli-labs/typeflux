/**
 * The injected GitHub transport seam (#727; Python `github_provenance.py` `default_github_reader` +
 * the `github_reader` Callable the control plane injects). A NEW seam — parallel to
 * {@link ../langfuse-transport.js#LangfuseControlPlaneTransport}, NOT a method on it — because GitHub
 * is an entirely different backend (its own host, auth, and REST surface): Python keeps
 * `github_reader` distinct from `enforcement_reader` for the same reason, and folding GitHub reads
 * into the Langfuse transport would conflate two unrelated vendors. The pure control plane never
 * constructs one on the served path (the TS registry records no git source, so `served` is null and
 * the reader is never reached); a deployment or a test injects a transport, and {@link
 * fetchGithubTransport} is the dependency-free reference over GitHub's REST API.
 *
 * One method mirrors Python's `github_reader` seam (a single call returning a `GithubReadResult`):
 * given the served repo/branch/sha and the capped set of plan commit shas, it returns the
 * reachability status + remote HEAD + behind-count + resolved PRs, degrading LOUDLY and per step.
 */

import { fanOut } from "@typeflux/temporal";

import { fetchJson } from "./deadline-fetch.js";
import {
  type GithubReadResult,
  type GithubRepo,
  type GithubStatus,
  MAX_COMMITS_BEHIND,
  type PlanPullRequest,
  PLAN_PR_LOOKUP_CONCURRENCY,
  selectPullRequest,
} from "./github-provenance.js";

/** The GitHub reader the control plane needs (Python `GithubReader = Callable[..., GithubReadResult]`).
 * A single bounded read; it NEVER rejects — it degrades into the returned `status` (the seam contract:
 * provenance must never 500 the surface). */
export interface GithubProvenanceTransport {
  read(options: {
    repo: GithubRepo;
    branch: string;
    servedSha: string | null;
    planShas: readonly string[];
  }): Promise<GithubReadResult>;
}

/** Options for {@link fetchGithubTransport} — a token override + test seams for `fetch`/`env`/timeout. */
export interface FetchGithubTransportOptions {
  /** GitHub token override; else `TYPEFLUX_GITHUB_TOKEN` then `GITHUB_TOKEN` from {@link env}. */
  token?: string;
  /** Env source (defaults to `process.env`) — the token fallbacks read from here. */
  env?: Record<string, string | undefined>;
  /** `fetch` override (defaults to the global) — injected in unit tests. */
  fetch?: typeof fetch;
  /** Per-request deadline in ms (default 10000; Python `_GITHUB_TIMEOUT_SECONDS`). */
  timeoutMs?: number;
}

const GITHUB_API_BASE = "https://api.github.com";

/** GitHub answered with a rate-limit signal (a 403 primary/secondary limit, or a 429) — Python `_RateLimited`. */
class GithubRateLimited extends Error {
  constructor() {
    super("github rate limit");
    this.name = "GithubRateLimited";
  }
}

/** A non-rate-limit HTTP error carrying the status `code` so the caller can classify a 404/422 as a
 * routine per-resource miss (Python surfaces this via `urllib.error.HTTPError.code`). */
class GithubHttpError extends Error {
  constructor(readonly code: number) {
    super(`github request failed (status ${code})`);
    this.name = "GithubHttpError";
  }
}

/** A 404/422 — a specific resource is absent/unprocessable (a commit with no PR, a `compare` with no
 * common ancestor / a gone sha): routine drift, NOT a reachability outage (Python `_is_absent`). */
function isAbsent(error: unknown): boolean {
  return error instanceof GithubHttpError && (error.code === 404 || error.code === 422);
}

/**
 * Primary limit: `x-ratelimit-remaining: 0`. Secondary/abuse limit: a `retry-after` header. Either
 * is the rate-limited class (a 403 without them is a real auth/perm error → unreachable). Python
 * `_is_rate_limited`.
 */
function isRateLimited(headers: Headers): boolean {
  if (headers.get("retry-after") !== null) return true;
  return headers.get("x-ratelimit-remaining") === "0";
}

/**
 * A dependency-free reference {@link GithubProvenanceTransport} over GitHub's REST API (`fetch` +
 * bearer auth). Ports Python `default_github_reader`: a bounded, authenticated set of reads degrading
 * LOUDLY and PER STEP. No token → `not_configured` with NO network call (the Langfuse-seam posture).
 *
 * Bounds: one HEAD lookup, at most one `compare` for the behind-count (only when the served sha
 * differs from HEAD), and one PR lookup per plan sha (the caller has already capped the plan set),
 * fanned out through a bounded pool ({@link PLAN_PR_LOOKUP_CONCURRENCY}) rather than issued serially.
 *
 * Per-step isolation: the HEAD read is the primary signal, so a failure THERE is the only path that
 * yields no head at all. Once HEAD is fetched it is always returned — a `compare` miss keeps the head
 * with `commitsBehind` null (a 404/422 is a force-push / no-common-ancestor drift; a genuine
 * transport failure additionally degrades `partial` to `unreachable` but still keeps the head), and a
 * per-plan PR miss (404/422) leaves that plan's `pr` null without touching the others. Rate-limit
 * anywhere wins the status (the actionable signal), transport failure otherwise.
 */
export function fetchGithubTransport(options: FetchGithubTransportOptions = {}): GithubProvenanceTransport {
  const env = options.env ?? process.env;
  const doFetch = options.fetch ?? fetch;
  const timeoutMs = options.timeoutMs ?? 10_000;
  // Python `os.getenv(TYPEFLUX_GITHUB_TOKEN) or os.getenv(GITHUB_TOKEN)` then `.strip()`: `or`
  // semantics, so a blank/whitespace-only primary must NOT block the valid fallback. A trimmed-truthy
  // chain reproduces that (the repo's `??`-vs-`||` porting trap — an empty-string primary short-
  // circuits `??` and swallows the fallback; `||` on the trimmed value falls through as Python does).
  const token =
    options.token?.trim() || env["TYPEFLUX_GITHUB_TOKEN"]?.trim() || env["GITHUB_TOKEN"]?.trim() || null;

  const get = async (path: string): Promise<unknown> =>
    // The shared deadline-bound JSON wrapper (#727 F7) owns the per-request timeout + the non-ok gate
    // + `.json()`; GitHub's classifier separates a rate-limit signal from a routine HTTP error.
    fetchJson({
      fetch: doFetch,
      url: `${GITHUB_API_BASE}${path}`,
      headers: {
        authorization: `Bearer ${token ?? ""}`,
        accept: "application/vnd.github+json",
        "x-github-api-version": "2022-11-28",
        "user-agent": "typeflux-control-plane",
      },
      timeoutMs,
      onErrorResponse: (response) => {
        // A 429 is always a (secondary) rate limit; a 403 is a rate limit only WITH the primary/
        // secondary signal (a bare 403 is a real auth/perm error → unreachable).
        if (response.status === 429 || (response.status === 403 && isRateLimited(response.headers))) {
          throw new GithubRateLimited();
        }
        throw new GithubHttpError(response.status);
      },
    });

  return {
    async read({ repo, branch, servedSha, planShas }): Promise<GithubReadResult> {
      if (token === null) return { status: "not_configured" };

      const owner = encodeURIComponent(repo.owner);
      const name = encodeURIComponent(repo.repo);

      // Step 1 — the HEAD lookup (the primary signal). A failure here yields no head at all.
      let head: unknown;
      try {
        head = await get(`/repos/${owner}/${name}/commits/${encodeURIComponent(branch)}`);
      } catch (error) {
        return { status: error instanceof GithubRateLimited ? "rate_limited" : "unreachable" };
      }
      const headSha =
        typeof head === "object" && head !== null && typeof (head as Record<string, unknown>)["sha"] === "string"
          ? ((head as Record<string, unknown>)["sha"] as string)
          : null;
      if (headSha === null) {
        // Reached GitHub but the HEAD payload was malformed — the primary signal could not be read,
        // so this is NOT a healthy read: report `unreachable` rather than an `ok` with no head (a
        // client treating `ok` as healthy would get a false positive).
        return { status: "unreachable" };
      }

      // From here the HEAD is fetched and is always returned; later failures only mark `partial`
      // (rate_limited wins over unreachable — the more actionable signal).
      let degraded: GithubStatus | null = null;
      const degrade = (status: GithubStatus): void => {
        if (status === "rate_limited" || degraded === null) degraded = status;
      };

      // Step 2 and Step 3 are INDEPENDENT once HEAD resolves (the compare needs only headSha/servedSha;
      // the plan-PR lookups need only the plan shas). Python issues them serially; this async edition
      // runs them CONCURRENTLY (`Promise.all`) to halve the tail latency. Each step still owns its own
      // isolation internally and reports its degradation locally; the two are merged through `degrade`
      // in a FIXED order (compare, then plans) below, so the rate_limited-wins ordering is unchanged —
      // and because rate_limited always overrides, the merged status is deterministic regardless.
      const [{ commitsBehind, degrade: compareDegrade }, { planPrs, degrades: planDegrades }] =
        await Promise.all([
          computeCommitsBehind(get, owner, name, servedSha, headSha),
          resolvePlanPrs(get, owner, name, planShas),
        ]);
      if (compareDegrade !== null) degrade(compareDegrade);
      for (const status of planDegrades) degrade(status);

      return { status: degraded ?? "ok", headSha, commitsBehind, planPrs };
    },
  };
}

/**
 * Step 2 — the behind-count (Python's `compare` arm), isolated so it can run CONCURRENTLY with the
 * plan-PR fan-out (#727 F5). Only calls `compare` when there is a divergence to measure (a call saved
 * when served == HEAD, the common healthy case). A 404/422 is a force-push / no-common-ancestor —
 * exactly when drift matters: keep the head, leave `commitsBehind` null, and do NOT degrade. A
 * rate-limit or genuine transport failure reports its degradation (`compare` never throws out).
 */
async function computeCommitsBehind(
  get: (path: string) => Promise<unknown>,
  owner: string,
  name: string,
  servedSha: string | null,
  headSha: string,
): Promise<{ commitsBehind: number | null; degrade: GithubStatus | null }> {
  if (servedSha === null || headSha === servedSha) return { commitsBehind: null, degrade: null };
  try {
    const compare = await get(
      `/repos/${owner}/${name}/compare/${encodeURIComponent(servedSha)}...${encodeURIComponent(headSha)}`,
    );
    if (typeof compare === "object" && compare !== null) {
      const aheadBy = (compare as Record<string, unknown>)["ahead_by"];
      if (typeof aheadBy === "number" && Number.isInteger(aheadBy)) {
        // ahead_by = commits HEAD is ahead of the served base = how far served is behind. Bound it so
        // a long-diverged branch reports an honest ceiling.
        return { commitsBehind: Math.min(aheadBy, MAX_COMMITS_BEHIND), degrade: null };
      }
    }
    return { commitsBehind: null, degrade: null };
  } catch (error) {
    if (error instanceof GithubRateLimited) return { commitsBehind: null, degrade: "rate_limited" };
    return { commitsBehind: null, degrade: isAbsent(error) ? null : "unreachable" };
  }
}

/**
 * Step 3 — the plan-PR lookups, fanned out through a bounded pool; each future is isolated (a 404/422
 * → that plan's pr null; a rate-limit/transport failure → a degradation). The per-plan fn NEVER
 * rejects, so `fanOut` settles every lookup and returns them all (no early propagation). Runs
 * CONCURRENTLY with Step 2 (#727 F5); its degradations are returned in sha order for the caller to
 * merge under the rate_limited-wins rule.
 */
async function resolvePlanPrs(
  get: (path: string) => Promise<unknown>,
  owner: string,
  name: string,
  planShas: readonly string[],
): Promise<{ planPrs: Record<string, PlanPullRequest>; degrades: GithubStatus[] }> {
  const planPrs: Record<string, PlanPullRequest> = {};
  const degrades: GithubStatus[] = [];
  const uniqueShas = [...new Set(planShas)]; // de-dupe, preserve order
  if (uniqueShas.length > 0) {
    const results = await fanOut(uniqueShas, async (sha) => lookupPlanPr(get, owner, name, sha), {
      concurrency: Math.min(PLAN_PR_LOOKUP_CONCURRENCY, uniqueShas.length),
    });
    for (let i = 0; i < uniqueShas.length; i += 1) {
      const { status, pr } = results[i]!;
      if (status !== null) degrades.push(status);
      else if (pr !== null) planPrs[uniqueShas[i]!] = pr;
    }
  }
  return { planPrs, degrades };
}

/**
 * One plan-PR read, isolated for the fan-out (Python `_lookup_plan_pr`): returns `{status: null, pr}`
 * on success (`pr` may be null when the commit has no PR), or `{status, pr: null}` when the read
 * failed — `rate_limited` for a rate limit, `unreachable` for a genuine transport failure, and
 * `{status: null, pr: null}` for a routine 404/422 (a commit with no PR / an unknown sha) so it does
 * NOT degrade the whole response. It never throws.
 */
async function lookupPlanPr(
  get: (path: string) => Promise<unknown>,
  owner: string,
  name: string,
  sha: string,
): Promise<{ status: GithubStatus | null; pr: PlanPullRequest | null }> {
  try {
    const pulls = await get(`/repos/${owner}/${name}/commits/${encodeURIComponent(sha)}/pulls`);
    return { status: null, pr: selectPullRequest(pulls) };
  } catch (error) {
    if (error instanceof GithubRateLimited) return { status: "rate_limited", pr: null };
    return { status: isAbsent(error) ? null : "unreachable", pr: null };
  }
}
