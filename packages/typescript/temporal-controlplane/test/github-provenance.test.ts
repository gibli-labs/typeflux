/**
 * Unit tests for the github-provenance surface helpers (#727; mirrors the Python
 * `test_project_github_provenance.py` cases): the repo-url host gate, served/plan extraction, the
 * pure feed assembler (wire shape), and the default GitHub transport seam (with `fetch` injected — no
 * network). Every hardened behavior the Python edition pins is covered here.
 */

import { describe, expect, it } from "vitest";

import {
  buildGithubProvenance,
  type GithubReadResult,
  type GithubRepo,
  MAX_COMMITS_BEHIND,
  parseGithubRepo,
  type PlanPullRequest,
  type PlanRef,
  planRefs,
  scrubGitUrl,
  servedProvenance,
} from "../src/github-provenance.js";
import { fetchGithubTransport } from "../src/github-transport.js";

// ---------------------------------------------------------------------------
// parseGithubRepo — the host gate (mirrors the console #718 gate + Python parse_github_repo).
// ---------------------------------------------------------------------------

describe("parseGithubRepo", () => {
  it("accepts github https and splits owner/repo", () => {
    expect(parseGithubRepo("https://github.com/acme/flows")).toEqual({
      owner: "acme",
      repo: "flows",
      url: "https://github.com/acme/flows",
    });
  });

  it("strips a trailing .git and takes owner/repo from the first two segments", () => {
    expect(parseGithubRepo("https://github.com/acme/flows.git")).toEqual({
      owner: "acme",
      repo: "flows",
      url: "https://github.com/acme/flows.git",
    });
    expect(parseGithubRepo("https://github.com/acme/flows/tree/main")).toMatchObject({
      owner: "acme",
      repo: "flows",
    });
  });

  it("allows a github subdomain", () => {
    expect(parseGithubRepo("https://www.github.com/acme/flows")).toMatchObject({ owner: "acme", repo: "flows" });
  });

  it.each([
    null,
    undefined,
    "",
    "git@github.com:acme/flows.git", // ssh shorthand: no http(s) scheme
    "https://github.com.evil.com/acme/flows", // prefix-bypass: registrable domain anchored
    "https://github.enterprise.example/acme/flows", // self-hosted GHE: unverifiable
    "https://gitlab.com/acme/flows", // non-github host
    "https://github.com/acme", // missing repo segment
    "https://github.com/", // no owner/repo
  ])("rejects non-github or incomplete: %s", (repoUrl) => {
    expect(parseGithubRepo(repoUrl)).toBeNull();
  });

  it("scrubs credential userinfo from the stored url (never leaks a token)", () => {
    const repo = parseGithubRepo("https://alice:ghp_secrettoken@github.com/acme/flows.git");
    expect(repo).toEqual({ owner: "acme", repo: "flows", url: "https://github.com/acme/flows.git" });
    // The credential appears nowhere in any field of the parsed result.
    expect(JSON.stringify(repo)).not.toContain("ghp_secrettoken");
    expect(JSON.stringify(repo)).not.toContain("alice");
  });
});

// ---------------------------------------------------------------------------
// scrubGitUrl — credential userinfo stripping (mirrors Python scrub_git_url).
// ---------------------------------------------------------------------------

describe("scrubGitUrl", () => {
  it("strips user:token@ userinfo, preserving the rest", () => {
    expect(scrubGitUrl("https://user:token@github.com/acme/flows")).toBe("https://github.com/acme/flows");
    expect(scrubGitUrl("https://user:token@github.com/acme/flows.git")).toBe("https://github.com/acme/flows.git");
    // A username-only userinfo is stripped too.
    expect(scrubGitUrl("https://token@github.com/acme/flows")).toBe("https://github.com/acme/flows");
  });

  it("leaves credential-free urls and ssh shorthand untouched", () => {
    expect(scrubGitUrl("https://github.com/acme/flows")).toBe("https://github.com/acme/flows");
    expect(scrubGitUrl("git@github.com:acme/flows.git")).toBe("git@github.com:acme/flows.git"); // no scheme
    expect(scrubGitUrl(null)).toBeNull();
    expect(scrubGitUrl(undefined)).toBeUndefined();
    expect(scrubGitUrl("")).toBe("");
  });
});

// ---------------------------------------------------------------------------
// servedProvenance / planRefs — pure selectors.
// ---------------------------------------------------------------------------

describe("servedProvenance / planRefs", () => {
  it("is present for a github source", () => {
    const served = servedProvenance({ repoUrl: "https://github.com/acme/flows", repoRef: "main", repoSha: "abc123" });
    expect(served).not.toBeNull();
    expect(served!.repo).toEqual({ owner: "acme", repo: "flows", url: "https://github.com/acme/flows" });
    expect(served!.branch).toBe("main");
    expect(served!.servedSha).toBe("abc123");
  });

  it("is null for a non-github source", () => {
    expect(servedProvenance({ repoUrl: null, repoRef: "main", repoSha: null })).toBeNull();
    expect(servedProvenance({ repoUrl: "https://gitlab.com/a/b", repoRef: "main", repoSha: null })).toBeNull();
  });

  it("scrubs credential userinfo from the served repo url", () => {
    const served = servedProvenance({
      repoUrl: "https://user:ghp_secrettoken@github.com/acme/flows",
      repoRef: "main",
      repoSha: "abc123",
    });
    expect(served!.repo.url).toBe("https://github.com/acme/flows");
    expect(JSON.stringify(served)).not.toContain("ghp_secrettoken");
  });

  it("caps and skips sha-less plans", () => {
    const refs: PlanRef[] = [
      { planId: "p0", sha: "s0" },
      { planId: "p1", sha: null }, // no provenance → skipped from PR lookup
      { planId: "p2", sha: "s2" },
      { planId: "p3", sha: "s3" },
    ];
    expect(planRefs(refs, 2)).toEqual([
      { planId: "p0", sha: "s0" },
      { planId: "p2", sha: "s2" },
    ]);
  });
});

// ---------------------------------------------------------------------------
// buildGithubProvenance — the pure assembler (wire shape, nulls omitted).
// ---------------------------------------------------------------------------

const SERVED = servedProvenance({ repoUrl: "https://github.com/acme/flows", repoRef: "main", repoSha: "served-sha" })!;

describe("buildGithubProvenance", () => {
  it("yields no head with no served provenance, still lists plans", () => {
    const result = buildGithubProvenance({
      served: null,
      plans: [{ planId: "p0", sha: "s0" }],
      readResult: { status: "not_configured" },
    });
    expect(result.head).toBeUndefined();
    expect(result.partial.github).toBe("not_configured");
    expect(result.plans).toEqual([{ plan_id: "p0", sha: "s0" }]); // pr omitted (no network)
  });

  it("marks the head behind when the remote differs", () => {
    const result = buildGithubProvenance({
      served: SERVED,
      plans: [],
      readResult: { status: "ok", headSha: "remote-sha", commitsBehind: 3 },
    });
    expect(result.head).toEqual({ branch: "main", sha: "remote-sha", ahead_of_served: true, commits_behind: 3 });
  });

  it("marks the head in sync when the remote equals served (commits_behind omitted)", () => {
    const result = buildGithubProvenance({
      served: SERVED,
      plans: [],
      readResult: { status: "ok", headSha: "served-sha", commitsBehind: null },
    });
    expect(result.head).toEqual({ branch: "main", sha: "served-sha", ahead_of_served: false });
    expect(result.head!.commits_behind).toBeUndefined();
  });

  it("leaves ahead_of_served unknown (omitted) when the served sha is unresolved", () => {
    const servedNoSha = servedProvenance({ repoUrl: "https://github.com/acme/flows", repoRef: "main", repoSha: null })!;
    const result = buildGithubProvenance({
      served: servedNoSha,
      plans: [],
      readResult: { status: "ok", headSha: "remote-sha" },
    });
    expect(result.head).toEqual({ branch: "main", sha: "remote-sha" });
    expect(result.head!.ahead_of_served).toBeUndefined();
  });

  it("yields a null head when a degradation never fetched the head", () => {
    for (const status of ["unreachable", "rate_limited", "not_configured"] as const) {
      const result = buildGithubProvenance({ served: SERVED, plans: [], readResult: { status } });
      expect(result.head, status).toBeUndefined();
      expect(result.partial.github).toBe(status);
    }
  });

  it("keeps the head once fetched even under a later degradation", () => {
    const result = buildGithubProvenance({
      served: SERVED,
      plans: [],
      readResult: { status: "unreachable", headSha: "remote-sha" },
    });
    expect(result.head).toEqual({ branch: "main", sha: "remote-sha", ahead_of_served: true });
    expect(result.partial.github).toBe("unreachable");
  });

  it("links a plan's PR when resolved, else omits it", () => {
    const pr: PlanPullRequest = { number: 7, url: "https://github.com/acme/flows/pull/7", mergedAt: "2026-07-01T00:00:00Z" };
    const result = buildGithubProvenance({
      served: SERVED,
      plans: [
        { planId: "p-has-pr", sha: "s1" },
        { planId: "p-no-pr", sha: "s2" },
        { planId: "p-no-sha", sha: null },
      ],
      readResult: { status: "ok", headSha: "served-sha", planPrs: { s1: pr } },
    });
    const byId = Object.fromEntries(result.plans.map((plan) => [plan.plan_id, plan]));
    expect(byId["p-has-pr"]).toEqual({
      plan_id: "p-has-pr",
      sha: "s1",
      pr: { number: 7, url: "https://github.com/acme/flows/pull/7", merged_at: "2026-07-01T00:00:00Z" },
    });
    expect(byId["p-no-pr"]).toEqual({ plan_id: "p-no-pr", sha: "s2" });
    expect(byId["p-no-sha"]).toEqual({ plan_id: "p-no-sha" });
  });
});

// ---------------------------------------------------------------------------
// fetchGithubTransport — the transport seam, `fetch` injected (no network).
// ---------------------------------------------------------------------------

const REPO: GithubRepo = { owner: "acme", repo: "flows", url: "https://github.com/acme/flows" };
const NO_TOKENS: Record<string, string | undefined> = { TYPEFLUX_GITHUB_TOKEN: undefined, GITHUB_TOKEN: undefined };
const TOKEN_ENV: Record<string, string | undefined> = { TYPEFLUX_GITHUB_TOKEN: "tok" };

/** A GitHub HTTP error marker for a route (a non-ok Response with the given status + headers). */
type HttpError = { httpErrorStatus: number; headers?: Record<string, string> };
const httpError = (status: number, headers: Record<string, string> = {}): HttpError => ({ httpErrorStatus: status, headers });

/**
 * A `fetch` stub that matches a request URL against route FRAGMENTS (first substring match wins,
 * mirroring the Python `_routed_urlopen`). A JSON payload → 200; an {@link HttpError} → a non-ok
 * Response; the special `"__reject"` marker → a rejected promise (a network/abort failure).
 *
 * TODO(#727 F6c): this `routedFetch` and `langfuse-transport.test.ts`'s `fakeFetch` overlap but are
 * NOT trivially unifiable — `fakeFetch` records the calls and matches an ordered array of
 * `{match,status?,json?}`, while `routedFetch` supports header-bearing HTTP errors + a `__reject`
 * marker and matches a fragment record. Unify into a shared test-support helper only if a third
 * transport test needs one (until then the extra abstraction would obscure both).
 */
function routedFetch(routes: Record<string, unknown>): typeof fetch {
  const impl = async (input: unknown): Promise<Response> => {
    const url = String(input);
    for (const [fragment, payload] of Object.entries(routes)) {
      if (!url.includes(fragment)) continue;
      if (payload === "__reject") return Promise.reject(new Error("network down"));
      if (payload !== null && typeof payload === "object" && "httpErrorStatus" in (payload as object)) {
        const err = payload as HttpError;
        return { ok: false, status: err.httpErrorStatus, headers: new Headers(err.headers), json: async () => ({}) } as Response;
      }
      return { ok: true, status: 200, headers: new Headers(), json: async () => payload } as Response;
    }
    throw new Error(`unexpected GitHub call: ${url}`);
  };
  return impl as unknown as typeof fetch;
}

const readerFor = (routes: Record<string, unknown>, env: Record<string, string | undefined> = TOKEN_ENV): Promise<GithubReadResult> =>
  fetchGithubTransport({ env, fetch: routedFetch(routes) }).read({
    repo: REPO,
    branch: "main",
    servedSha: "served",
    planShas: ["s1"],
  });

describe("fetchGithubTransport", () => {
  it("is not_configured without a network call when there is no token", async () => {
    const boom = (() => {
      throw new Error("no network call may be made when unconfigured");
    }) as unknown as typeof fetch;
    const result = await fetchGithubTransport({ env: NO_TOKENS, fetch: boom }).read({
      repo: REPO,
      branch: "main",
      servedSha: "served",
      planShas: ["s1"],
    });
    expect(result).toEqual({ status: "not_configured" });
  });

  it("falls through an empty-string primary to a valid fallback token (the ??-vs-|| trap)", async () => {
    // TYPEFLUX_GITHUB_TOKEN is "" (blank primary); GITHUB_TOKEN is the real token. A `??` chain would
    // stop at the empty string and report not_configured; the trimmed-truthy `||` chain falls through.
    const result = await fetchGithubTransport({
      env: { TYPEFLUX_GITHUB_TOKEN: "", GITHUB_TOKEN: "real-token" },
      fetch: routedFetch({ "/commits/main": { sha: "served" }, "/commits/s1/pulls": [] }),
    }).read({ repo: REPO, branch: "main", servedSha: "served", planShas: ["s1"] });
    expect(result.status).toBe("ok"); // configured: the fallback token was used
    expect(result.headSha).toBe("served");
  });

  it("treats a whitespace-only token as absent (no network)", async () => {
    const boom = (() => {
      throw new Error("no network call");
    }) as unknown as typeof fetch;
    const result = await fetchGithubTransport({ env: { GITHUB_TOKEN: "   " }, fetch: boom }).read({
      repo: REPO,
      branch: "main",
      servedSha: "served",
      planShas: [],
    });
    expect(result).toEqual({ status: "not_configured" });
  });

  it("reads head, compare, and the merged PR (dedupes shas)", async () => {
    const result = await fetchGithubTransport({ env: TOKEN_ENV, fetch: routedFetch({
      "/commits/main": { sha: "remote-sha" },
      "/compare/served...remote-sha": { ahead_by: 4 },
      "/commits/s1/pulls": [
        { number: 3, html_url: "https://github.com/acme/flows/pull/3", merged_at: null },
        { number: 4, html_url: "https://github.com/acme/flows/pull/4", merged_at: "2026-07-02T00:00:00Z" },
      ],
    }) }).read({ repo: REPO, branch: "main", servedSha: "served", planShas: ["s1", "s1"] });
    expect(result.status).toBe("ok");
    expect(result.headSha).toBe("remote-sha");
    expect(result.commitsBehind).toBe(4);
    // The merged PR wins over the open one.
    expect(result.planPrs!["s1"]).toEqual({ number: 4, url: "https://github.com/acme/flows/pull/4", mergedAt: "2026-07-02T00:00:00Z" });
  });

  it("skips the compare when head equals served", async () => {
    // No /compare route registered: if the reader called it, routedFetch would throw.
    const result = await readerFor({ "/commits/main": { sha: "served" }, "/commits/s1/pulls": [] });
    expect(result.status).toBe("ok");
    expect(result.headSha).toBe("served");
    expect(result.commitsBehind).toBeNull();
    expect(result.planPrs).toEqual({});
  });

  it("bounds commits_behind at the ceiling", async () => {
    const result = await fetchGithubTransport({ env: TOKEN_ENV, fetch: routedFetch({
      "/commits/main": { sha: "remote-sha" },
      "/compare/served...remote-sha": { ahead_by: 10_000 },
    }) }).read({ repo: REPO, branch: "main", servedSha: "served", planShas: [] });
    expect(result.commitsBehind).toBe(MAX_COMMITS_BEHIND);
  });

  it("is rate_limited on a 403 with the ratelimit header", async () => {
    expect(await readerFor({ "/commits/main": httpError(403, { "x-ratelimit-remaining": "0" }) })).toEqual({ status: "rate_limited" });
  });

  it("is rate_limited on a secondary retry-after 403", async () => {
    expect(await readerFor({ "/commits/main": httpError(403, { "retry-after": "60" }) })).toEqual({ status: "rate_limited" });
  });

  it("is rate_limited on a 429 even without a ratelimit header", async () => {
    expect(await readerFor({ "/commits/main": httpError(429) })).toEqual({ status: "rate_limited" });
  });

  it("is unreachable on a 404 head (repo/branch gone), never a silent ok", async () => {
    expect(await readerFor({ "/commits/main": httpError(404) })).toEqual({ status: "unreachable" });
  });

  it("is unreachable on a 403 without a ratelimit signal (a permission error)", async () => {
    expect(await readerFor({ "/commits/main": httpError(403) })).toEqual({ status: "unreachable" });
  });

  it("is unreachable on a malformed head payload (no usable sha)", async () => {
    expect(await readerFor({ "/commits/main": { not_sha: true } })).toEqual({ status: "unreachable" });
  });

  it("is unreachable on a rejected (network) head fetch", async () => {
    expect(await readerFor({ "/commits/main": "__reject" })).toEqual({ status: "unreachable" });
  });

  it("isolates one bad plan sha: keeps the head and the other PRs, status still ok", async () => {
    const result = await fetchGithubTransport({ env: TOKEN_ENV, fetch: routedFetch({
      "/commits/main": { sha: "served" }, // HEAD == served → no compare
      "/commits/good/pulls": [{ number: 5, html_url: "https://github.com/acme/flows/pull/5", merged_at: null }],
      "/commits/bad/pulls": httpError(404), // commit not found → isolated, pr null
    }) }).read({ repo: REPO, branch: "main", servedSha: "served", planShas: ["good", "bad"] });
    expect(result.status).toBe("ok"); // a 404 on one plan is routine, not a degradation
    expect(result.headSha).toBe("served");
    expect(result.planPrs!["good"]).toMatchObject({ number: 5 });
    expect(result.planPrs!["bad"]).toBeUndefined();
  });

  it("keeps the head with null behind when the compare fails (force-push / no common ancestor)", async () => {
    const result = await fetchGithubTransport({ env: TOKEN_ENV, fetch: routedFetch({
      "/commits/main": { sha: "remote-sha" },
      "/compare/served...remote-sha": httpError(404),
    }) }).read({ repo: REPO, branch: "main", servedSha: "served", planShas: [] });
    expect(result.status).toBe("ok");
    expect(result.headSha).toBe("remote-sha");
    expect(result.commitsBehind).toBeNull();
  });

  it("keeps the head but degrades to unreachable on a transport failure after head", async () => {
    const result = await fetchGithubTransport({ env: TOKEN_ENV, fetch: routedFetch({
      "/commits/main": { sha: "served" }, // HEAD == served → no compare
      "/commits/s1/pulls": httpError(500), // a real transport failure, not a 404/422
    }) }).read({ repo: REPO, branch: "main", servedSha: "served", planShas: ["s1"] });
    expect(result.status).toBe("unreachable");
    expect(result.headSha).toBe("served"); // the primary signal survives the later failure
    expect(result.planPrs).toEqual({});
  });

  it("lets rate_limited win over unreachable across steps", async () => {
    // A compare transport failure (unreachable) then a plan-PR rate limit → rate_limited wins.
    const result = await fetchGithubTransport({ env: TOKEN_ENV, fetch: routedFetch({
      "/commits/main": { sha: "remote-sha" },
      "/compare/served...remote-sha": httpError(500),
      "/commits/s1/pulls": httpError(429),
    }) }).read({ repo: REPO, branch: "main", servedSha: "served", planShas: ["s1"] });
    expect(result.status).toBe("rate_limited");
    expect(result.headSha).toBe("remote-sha"); // head still survives
  });
});
