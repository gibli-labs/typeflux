import { describe, expect, it } from "vitest";

import type { GithubProvenance, HeadProvenance, PlanProvenance } from "./api";
import {
  commitsBehindLabel,
  deriveGithubDrift,
  derivePlanPr,
  githubCapabilityAbsentCopy,
  githubChangeInsights,
  githubCompareHref,
  githubPartialNotice,
  MAX_COMMITS_BEHIND,
  planProvenanceById,
  PR_NOT_MERGED_TOOLTIP,
  PR_URL_NOT_LINKED_TOOLTIP,
  PROJECT_REFRESH_ENDPOINT,
} from "./githubDrift";
import type { RepoProvenance } from "./links";

const GH_PROJECT: RepoProvenance = {
  repo_url: "https://github.com/acme/widgets",
  repo_sha: "servedsha0000000000000000000000000000000",
  manifest_repo_path: "typeflux.project.yaml",
};

function provenance(overrides: Partial<GithubProvenance>): GithubProvenance {
  return {
    partial: { github: "ok" },
    plans: [],
    head: null,
    ...overrides,
  } as GithubProvenance;
}

describe("commitsBehindLabel", () => {
  it("shows an exact, correctly-pluralized count", () => {
    expect(commitsBehindLabel(1)).toBe("1 commit");
    expect(commitsBehindLabel(3)).toBe("3 commits");
  });

  it("shows an honest N+ at the server cap, never a precise-looking number", () => {
    expect(commitsBehindLabel(MAX_COMMITS_BEHIND)).toBe(`${MAX_COMMITS_BEHIND}+ commits`);
    expect(commitsBehindLabel(MAX_COMMITS_BEHIND + 50)).toBe(`${MAX_COMMITS_BEHIND}+ commits`);
  });

  it("falls back to 'some commits' when ahead but the count is unavailable", () => {
    expect(commitsBehindLabel(null)).toBe("some commits");
    expect(commitsBehindLabel(undefined)).toBe("some commits");
  });
});

describe("githubPartialNotice", () => {
  it("returns null when the source was read in full", () => {
    expect(githubPartialNotice("ok")).toBeNull();
  });

  it("explains not_configured with the token/repo-provenance posture (neutral, not a warning)", () => {
    const notice = githubPartialNotice("not_configured")!;
    expect(notice.badgeKind).toBe("neutral");
    expect(notice.message).toMatch(/TYPEFLUX_GITHUB_TOKEN/);
    expect(notice.message).toMatch(/GitHub source/);
  });

  it("warns loudly on rate_limited and unreachable (data may be missing)", () => {
    expect(githubPartialNotice("rate_limited")!.badgeKind).toBe("warning");
    expect(githubPartialNotice("unreachable")!.badgeKind).toBe("warning");
  });
});

describe("githubCompareHref", () => {
  it("builds a host-gated three-dot compare of served sha → remote HEAD", () => {
    const href = githubCompareHref(GH_PROJECT, {
      branch: "main",
      sha: "headsha1111111111111111111111111111111111",
      ahead_of_served: true,
    });
    expect(href).toBe(
      "https://github.com/acme/widgets/compare/servedsha0000000000000000000000000000000...headsha1111111111111111111111111111111111",
    );
  });

  it("suppresses the link for a non-GitHub host (never a wrong/hand-rolled href)", () => {
    expect(
      githubCompareHref(
        { ...GH_PROJECT, repo_url: "https://gitlab.com/acme/widgets" },
        { branch: "main", sha: "h", ahead_of_served: true },
      ),
    ).toBeUndefined();
  });

  it("suppresses the link when the served sha or remote head is missing", () => {
    expect(
      githubCompareHref({ ...GH_PROJECT, repo_sha: null }, { branch: "main", sha: "h", ahead_of_served: true }),
    ).toBeUndefined();
    expect(githubCompareHref(GH_PROJECT, null)).toBeUndefined();
  });
});

describe("deriveGithubDrift — HEAD-vs-served (§1)", () => {
  it("emits a warning row with the compare-evidence link when ahead_of_served is true", () => {
    const feed = deriveGithubDrift(
      provenance({
        head: { branch: "main", sha: "headsha1111111111111111111111111111111111", ahead_of_served: true, commits_behind: 4 },
      }),
      GH_PROJECT,
    );
    expect(feed.notice).toBeNull();
    expect(feed.rows).toHaveLength(1);
    const row = feed.rows[0];
    expect(row.severity).toBe("warning");
    expect(row.title).toBe("Served checkout is 4 commits behind main HEAD");
    expect(row.evidence?.href).toContain("/compare/");
    expect(row.detail).toMatch(/refresh the project/i);
  });

  it("renders the honest N+ count at the cap", () => {
    const feed = deriveGithubDrift(
      provenance({
        head: { branch: "main", sha: "h", ahead_of_served: true, commits_behind: MAX_COMMITS_BEHIND },
      }),
      GH_PROJECT,
    );
    expect(feed.rows[0].title).toBe(`Served checkout is ${MAX_COMMITS_BEHIND}+ commits behind main HEAD`);
  });

  it("treats ahead_of_served=false as a first-class in-sync confirmation (ok)", () => {
    const feed = deriveGithubDrift(
      provenance({ head: { branch: "main", sha: "h", ahead_of_served: false } }),
      GH_PROJECT,
    );
    expect(feed.rows).toHaveLength(1);
    expect(feed.rows[0].severity).toBe("ok");
    expect(feed.rows[0].title).toMatch(/matches main HEAD/);
  });

  it("renders ahead_of_served=null as an explicit unknown, NEVER in-sync", () => {
    const feed = deriveGithubDrift(
      provenance({ head: { branch: "main", sha: "h", ahead_of_served: null } }),
      GH_PROJECT,
    );
    expect(feed.rows).toHaveLength(1);
    expect(feed.rows[0].severity).toBe("info");
    expect(feed.rows[0].id).toBe("github:unknown");
    expect(feed.rows[0].title).not.toMatch(/matches/);
    expect(feed.rows[0].detail).toMatch(/never reported as in sync/i);
  });

  it("renders an OMITTED ahead_of_served (key absent, not null) as unknown too, never in-sync (F5e)", () => {
    // The field is optional on the wire — absence must degrade to the same explicit unknown as an
    // explicit null, never fall through to a false in-sync.
    const feed = deriveGithubDrift(
      provenance({ head: { branch: "main", sha: "h" } as HeadProvenance }),
      GH_PROJECT,
    );
    expect(feed.rows).toHaveLength(1);
    expect(feed.rows[0].id).toBe("github:unknown");
    expect(feed.rows[0].severity).toBe("info");
    expect(feed.rows[0].title).not.toMatch(/matches/);
  });

  it("head-drift remediation names the real refresh endpoint + the multi-project-only UI control (F4)", () => {
    // The Overview Projects "refresh" button only renders on multi-project consoles, so the copy
    // must give the API path too — never point every operator at a control half of them can't see.
    const feed = deriveGithubDrift(
      provenance({ head: { branch: "main", sha: "h", ahead_of_served: true, commits_behind: 2 } }),
      GH_PROJECT,
    );
    const detail = feed.rows[0].detail;
    expect(detail).toContain(PROJECT_REFRESH_ENDPOINT);
    expect(detail).toMatch(/multi-project/i);
    expect(detail).toMatch(/project\.refresh/);
  });
});

describe("deriveGithubDrift — degraded reachability", () => {
  it("carries a loud notice and no head row when not_configured", () => {
    const feed = deriveGithubDrift(provenance({ partial: { github: "not_configured" }, head: null }));
    expect(feed.notice?.badge).toBe("github not configured");
    expect(feed.rows).toHaveLength(0);
  });

  it("emits a neutral info row when the source is ok but the remote HEAD was unreadable", () => {
    const feed = deriveGithubDrift(provenance({ partial: { github: "ok" }, head: null }));
    expect(feed.notice).toBeNull();
    expect(feed.rows).toHaveLength(1);
    expect(feed.rows[0].id).toBe("github:head-unavailable");
    expect(feed.rows[0].severity).toBe("info");
  });
});

describe("githubChangeInsights (Overview feed, §7)", () => {
  it("includes the actionable head-drift warning but drops the ok in-sync confirmation", () => {
    const ahead = githubChangeInsights(
      provenance({ head: { branch: "main", sha: "h", ahead_of_served: true, commits_behind: 2 } }),
      GH_PROJECT,
    );
    expect(ahead.map((row) => row.id)).toEqual(["github:head-drift"]);

    const synced = githubChangeInsights(
      provenance({ head: { branch: "main", sha: "h", ahead_of_served: false } }),
      GH_PROJECT,
    );
    expect(synced).toHaveLength(0);
  });
});

describe("derivePlanPr (§6)", () => {
  const plan = (overrides: Partial<PlanProvenance>): PlanProvenance =>
    ({ plan_id: "p1", sha: "commitsha", pr: null, ...overrides }) as PlanProvenance;

  it("links a resolved, merged PR by number with its merged timestamp", () => {
    const link = derivePlanPr(
      plan({ pr: { number: 42, url: "https://github.com/acme/widgets/pull/42", merged_at: "2026-07-01T12:00:00Z" } }),
    );
    expect(link).toEqual({
      kind: "pr",
      number: 42,
      url: "https://github.com/acme/widgets/pull/42",
      mergedAt: "2026-07-01T12:00:00Z",
    });
  });

  it("host-gates the PR URL: a non-GitHub URL yields the number without a link", () => {
    const link = derivePlanPr(plan({ pr: { number: 7, url: "https://evil.example/pull/7", merged_at: null } }));
    expect(link).toMatchObject({ kind: "pr", number: 7, url: undefined, mergedAt: null });
  });

  it("degrades LOUDLY: sha-null is an untracked plan file, never a fabricated link", () => {
    const link = derivePlanPr(plan({ sha: null }));
    expect(link.kind).toBe("none");
    expect(link).toMatchObject({ reason: expect.stringMatching(/untracked/i) });
  });

  it("degrades LOUDLY: sha present but no PR found (partial ok — the read-in-full default)", () => {
    const link = derivePlanPr(plan({ sha: "abc", pr: null }));
    expect(link).toMatchObject({ kind: "none", reason: expect.stringMatching(/no approving PR/i) });
  });

  it("degrades LOUDLY when the plan is absent from the provenance feed entirely", () => {
    expect(derivePlanPr(undefined)).toMatchObject({ kind: "none", reason: expect.stringMatching(/unavailable/i) });
  });

  it("every none-case carries a tooltip (the copy lives in this tested layer, F5c)", () => {
    for (const link of [
      derivePlanPr(undefined),
      derivePlanPr(plan({ sha: null })),
      derivePlanPr(plan({ sha: "abc", pr: null })),
    ]) {
      expect(link.kind).toBe("none");
      if (link.kind === "none") expect(link.tooltip.length).toBeGreaterThan(0);
    }
  });

  it("exports the two resolved-PR tooltip strings for the Deployments cell (F5c)", () => {
    expect(PR_URL_NOT_LINKED_TOOLTIP).toMatch(/not linked|isn't linked/i);
    expect(PR_NOT_MERGED_TOOLTIP).toMatch(/not marked merged/i);
  });
});

describe("derivePlanPr — pr-null honesty under degraded / capped lookups (F3)", () => {
  const plan = (overrides: Partial<PlanProvenance>): PlanProvenance =>
    ({ plan_id: "p1", sha: "commitsha", pr: null, ...overrides }) as PlanProvenance;

  // The wire (PlanProvenance = plan_id/sha/pr only) cannot distinguish a plan the server looked up
  // and found no PR for from one it never looked up (beyond PLAN_PR_LOOKUP_CAP, or a degraded
  // source). So pr-null must never read as a confident "no approving PR found" when the lookup may
  // not have completed.

  it("partial=ok keeps the confident-but-cap-aware copy: inline 'no approving PR found', tooltip discloses the lookup cap", () => {
    const link = derivePlanPr(plan({}), "ok");
    expect(link).toMatchObject({ kind: "none", reason: expect.stringMatching(/no approving PR found/i) });
    if (link.kind === "none") expect(link.tooltip).toMatch(/lookup cap|beyond that cap|most-recent/i);
  });

  it("not_configured never asserts 'no PR found' — the lookup never ran", () => {
    const link = derivePlanPr(plan({}), "not_configured");
    expect(link.kind).toBe("none");
    if (link.kind === "none") {
      expect(link.reason).not.toMatch(/no approving PR found/i);
      expect(link.reason).toMatch(/not configured/i);
      expect(link.tooltip).toMatch(/never looked up|not a confirmed/i);
    }
  });

  it("rate_limited reports an incomplete lookup, not an absent PR", () => {
    const link = derivePlanPr(plan({}), "rate_limited");
    expect(link.kind).toBe("none");
    if (link.kind === "none") {
      expect(link.reason).not.toMatch(/no approving PR found/i);
      expect(link.reason).toMatch(/rate limited/i);
      expect(link.tooltip).toMatch(/not a confirmed/i);
    }
  });

  it("unreachable reports an incomplete lookup, not an absent PR", () => {
    const link = derivePlanPr(plan({}), "unreachable");
    expect(link.kind).toBe("none");
    if (link.kind === "none") {
      expect(link.reason).not.toMatch(/no approving PR found/i);
      expect(link.reason).toMatch(/unreachable/i);
      expect(link.tooltip).toMatch(/not a confirmed/i);
    }
  });

  it("a resolved PR is unaffected by a degraded partial (HEAD read failed AFTER the PR was read)", () => {
    const link = derivePlanPr(
      plan({ pr: { number: 9, url: "https://github.com/acme/widgets/pull/9", merged_at: null } }),
      "rate_limited",
    );
    expect(link).toMatchObject({ kind: "pr", number: 9 });
  });

  it("sha-null stays 'untracked' even under a degraded partial (a local fact, not a lookup)", () => {
    const link = derivePlanPr(plan({ sha: null }), "unreachable");
    expect(link).toMatchObject({ kind: "none", reason: expect.stringMatching(/untracked/i) });
  });
});

describe("planProvenanceById", () => {
  it("indexes plans by plan_id so cards resolve from one fetch (no N+1)", () => {
    const map = planProvenanceById(
      provenance({
        plans: [
          { plan_id: "a", sha: "sa", pr: null },
          { plan_id: "b", sha: null, pr: null },
        ] as PlanProvenance[],
      }),
    );
    expect(map.get("a")?.sha).toBe("sa");
    expect(map.get("b")?.sha).toBeNull();
    expect(map.get("missing")).toBeUndefined();
  });

  it("is empty for an undefined provenance read", () => {
    expect(planProvenanceById(undefined).size).toBe(0);
  });
});

describe("githubCapabilityAbsentCopy", () => {
  it("names the exact capability token and the enablement conditions", () => {
    const copy = githubCapabilityAbsentCopy();
    expect(copy.capability).toBe("github_provenance");
    expect(copy.badge).toBe("not supported");
    expect(copy.after).toMatch(/GitHub token/);
  });
});
