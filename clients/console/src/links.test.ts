import { describe, expect, it } from "vitest";

import type { Bundle } from "./api";
import {
  bundleSourceLinks,
  definitionSourceLinks,
  repoRelativePath, langfusePromptUrl, langfuseTracesUrl, temporalExecutionUrl } from "./links";

describe("temporalExecutionUrl", () => {
  it("builds the execution page, trimming trailing slashes and defaulting the namespace", () => {
    expect(temporalExecutionUrl("http://localhost:8233/", null, "case-1")).toBe(
      "http://localhost:8233/namespaces/default/workflows/case-1",
    );
  });

  it("links the run history when a run id is known and encodes components", () => {
    expect(
      temporalExecutionUrl("https://web.temporal.io", "prod ns", "case/1", "run-9"),
    ).toBe("https://web.temporal.io/namespaces/prod%20ns/workflows/case%2F1/run-9/history");
  });
});

describe("langfuse links", () => {
  it("builds project-scoped prompt and trace-search URLs", () => {
    const base = "https://cloud.langfuse.com/project/p-123/";
    expect(langfusePromptUrl(base, "support/classify")).toBe(
      "https://cloud.langfuse.com/project/p-123/prompts/support%2Fclassify",
    );
    expect(langfuseTracesUrl(base, "case-1")).toBe(
      "https://cloud.langfuse.com/project/p-123/traces?search=case-1",
    );
  });
});

describe("langfuseTraceUrl", () => {
  it("links directly to a trace id", async () => {
    const { langfuseTraceUrl } = await import("./links");
    expect(langfuseTraceUrl("https://lf.example/project/p/", "tr-1")).toBe(
      "https://lf.example/project/p/traces/tr-1",
    );
  });
});

describe("repoRelativePath / definitionSourceLinks (#606)", () => {
  it("joins a manifest-relative reference onto the manifest's repo directory", () => {
    expect(repoRelativePath("acme/typeflux.project.yaml", "policies/base.yaml")).toBe(
      "acme/policies/base.yaml",
    );
    expect(repoRelativePath("typeflux.project.yaml", "envs/prod.yaml")).toBe("envs/prod.yaml");
    expect(repoRelativePath("a/b/typeflux.project.yaml", "../shared/policy.yaml")).toBe(
      "a/shared/policy.yaml",
    );
  });

  it("returns undefined rather than a wrong link — absolute, ~, traversal escaping the root, no manifest", () => {
    expect(repoRelativePath("acme/typeflux.project.yaml", "/etc/policy.yaml")).toBeUndefined();
    expect(repoRelativePath("acme/typeflux.project.yaml", "~/policy.yaml")).toBeUndefined();
    expect(repoRelativePath("typeflux.project.yaml", "../../outside.yaml")).toBeUndefined();
    expect(repoRelativePath(undefined, "policies/base.yaml")).toBeUndefined();
    expect(repoRelativePath(null, "policies/base.yaml")).toBeUndefined();
    // Windows-style references resolve differently server-side — no link (codex).
    expect(repoRelativePath("typeflux.project.yaml", "C:\\policies\\base.yaml")).toBeUndefined();
    expect(repoRelativePath("typeflux.project.yaml", "..\\shared\\policy.yaml")).toBeUndefined();
  });

  it("builds blob+history links from git project provenance and degrades to {} without it", () => {
    const project = {
      repo_url: "https://github.com/acme/flows",
      repo_sha: "abc123",
      manifest_repo_path: "proj/typeflux.project.yaml",
    };
    expect(definitionSourceLinks(project, "policies/base.yaml")).toEqual({
      blob: "https://github.com/acme/flows/blob/abc123/proj/policies/base.yaml",
      history: "https://github.com/acme/flows/commits/abc123/proj/policies/base.yaml",
    });
    expect(definitionSourceLinks({ ...project, repo_sha: null }, "policies/base.yaml")).toEqual({});
    expect(definitionSourceLinks(undefined, "policies/base.yaml")).toEqual({});
    // ssh/local clone URLs cannot serve /blob/ pages — no link (codex).
    expect(
      definitionSourceLinks({ ...project, repo_url: "git@github.com:acme/flows.git" }, "p.yaml"),
    ).toEqual({});
    expect(definitionSourceLinks({ ...project, repo_url: "/srv/git/flows.git" }, "p.yaml")).toEqual({});
    // Non-GitHub https hosts use different blob path shapes (GitLab /-/blob/, Bitbucket /src/).
    expect(
      definitionSourceLinks({ ...project, repo_url: "https://gitlab.com/acme/flows" }, "p.yaml"),
    ).toEqual({});
    // A standard https CLONE url (.git suffix) still links to the web repo root (codex).
    expect(
      definitionSourceLinks({ ...project, repo_url: "https://github.com/acme/flows.git" }, "p.yaml").blob,
    ).toBe("https://github.com/acme/flows/blob/abc123/proj/p.yaml");
    // The host anchors the registrable domain — a github.-PREFIXED attacker host must not
    // pass (the classic allowlist bypass), and self-hosted GHE domains stay unlinked.
    expect(
      definitionSourceLinks({ ...project, repo_url: "https://github.com.evil.com/acme/flows" }, "p.yaml"),
    ).toEqual({});
    expect(
      definitionSourceLinks({ ...project, repo_url: "https://github.enterprise.example/acme/flows" }, "p.yaml"),
    ).toEqual({});
  });
});

describe("validatedGithubRepoUrl (#718 host gate)", () => {
  it("accepts github.com https and rejects ssh / GHE / prefix-bypass / non-github hosts", async () => {
    const { validatedGithubRepoUrl } = await import("./links");
    expect(validatedGithubRepoUrl("https://github.com/acme/flows")).toBe(
      "https://github.com/acme/flows",
    );
    // The host anchors the registrable domain — the classic `github.com.evil.com` bypass fails.
    expect(validatedGithubRepoUrl("https://github.com.evil.com/acme/flows")).toBeUndefined();
    // ssh clone URLs have no web pages; GHE hosts aren't verifiable client-side; other hosts differ.
    expect(validatedGithubRepoUrl("git@github.com:acme/flows.git")).toBeUndefined();
    expect(validatedGithubRepoUrl("https://github.enterprise.example/acme/flows")).toBeUndefined();
    expect(validatedGithubRepoUrl("https://gitlab.com/acme/flows")).toBeUndefined();
    expect(validatedGithubRepoUrl(null)).toBeUndefined();
    expect(validatedGithubRepoUrl(undefined)).toBeUndefined();
  });
});

describe("github links", () => {
  it("builds sha-pinned blob and commit URLs", async () => {
    const { githubBlobUrl, githubCommitUrl } = await import("./links");
    expect(githubBlobUrl("https://github.com/acme/demo/", "abc123", "examples/a b/typeflux.yaml")).toBe(
      "https://github.com/acme/demo/blob/abc123/examples/a%20b/typeflux.yaml",
    );
    expect(githubCommitUrl("https://github.com/acme/demo", "abc123")).toBe(
      "https://github.com/acme/demo/commit/abc123",
    );
  });

  it("builds a sha-pinned commit-HISTORY URL for any path, encoding segments (#718)", async () => {
    const { githubCommitsUrl } = await import("./links");
    expect(githubCommitsUrl("https://github.com/acme/demo.git/", "abc123", "policies/a b.yaml")).toBe(
      "https://github.com/acme/demo/commits/abc123/policies/a%20b.yaml",
    );
  });
});

describe("definitionSourceLinks / manifest links (#718)", () => {
  const project = {
    repo_url: "https://github.com/acme/flows",
    repo_sha: "abc123",
    manifest_repo_path: "proj/typeflux.project.yaml",
  };

  it("builds blob + commit-history for a manifest-relative reference and degrades to {}", () => {
    expect(definitionSourceLinks(project, "policies/base.yaml")).toEqual({
      blob: "https://github.com/acme/flows/blob/abc123/proj/policies/base.yaml",
      history: "https://github.com/acme/flows/commits/abc123/proj/policies/base.yaml",
    });
    // Degrades as a pair — no sha, non-GitHub, or unresolvable path → {} (both links absent).
    expect(definitionSourceLinks({ ...project, repo_sha: null }, "policies/base.yaml")).toEqual({});
    expect(
      definitionSourceLinks({ ...project, repo_url: "https://gitlab.com/acme/flows" }, "p.yaml"),
    ).toEqual({});
    expect(definitionSourceLinks(project, "/etc/policy.yaml")).toEqual({});
  });

  it("links the manifest itself by its own repo-relative path", async () => {
    const { manifestSourceLinks } = await import("./links");
    expect(manifestSourceLinks(project)).toEqual({
      blob: "https://github.com/acme/flows/blob/abc123/proj/typeflux.project.yaml",
      history: "https://github.com/acme/flows/commits/abc123/proj/typeflux.project.yaml",
    });
    // No provenance, or an absolute (non-repo-relative) manifest path → {} (no link).
    expect(manifestSourceLinks({ ...project, repo_sha: null })).toEqual({});
    expect(manifestSourceLinks({ ...project, manifest_repo_path: "/abs/typeflux.project.yaml" })).toEqual({});
    expect(manifestSourceLinks({ ...project, manifest_repo_path: null })).toEqual({});
    expect(manifestSourceLinks(undefined)).toEqual({});
  });

  it("suppresses the manifest link for Windows paths and `..` traversal (#718 path parity)", async () => {
    // The SAME rejections repoRelativePath applies to references, now applied to the manifest's
    // own path — a malformed path suppresses the link rather than pointing at the wrong repo file.
    const { manifestSourceLinks } = await import("./links");
    expect(manifestSourceLinks({ ...project, manifest_repo_path: "proj\\typeflux.project.yaml" })).toEqual({});
    expect(manifestSourceLinks({ ...project, manifest_repo_path: "C:/proj/typeflux.project.yaml" })).toEqual({});
    expect(manifestSourceLinks({ ...project, manifest_repo_path: "a/../b.yaml" })).toEqual({});
  });
});

describe("bundleSourceLinks (#721 F1)", () => {
  it("links only from code.workflow_path — never the raw resolved filesystem workflow.path", () => {
    const linked = {
      code: {
        repo_url: "https://github.com/acme/flows",
        sha: "abc123",
        workflow_path: "flows/wf.yaml",
        dirty: false,
      },
      workflow: { path: "/Users/dev/project/flows/wf.yaml" },
    } as unknown as Bundle;
    expect(bundleSourceLinks(linked)).toEqual({
      blob: "https://github.com/acme/flows/blob/abc123/flows/wf.yaml",
      history: "https://github.com/acme/flows/commits/abc123/flows/wf.yaml",
    });
  });

  it("degrades to {} when code.workflow_path is null — the absolute workflow.path must not leak into a blob/<sha>/Users/... URL", () => {
    const noRepoPath = {
      code: {
        repo_url: "https://github.com/acme/flows",
        sha: "abc123",
        workflow_path: null,
        dirty: false,
      },
      workflow: { path: "/Users/dev/project/flows/wf.yaml" },
    } as unknown as Bundle;
    expect(bundleSourceLinks(noRepoPath)).toEqual({});
    // No git provenance at all → {} too.
    expect(
      bundleSourceLinks({ workflow: { path: "/Users/dev/wf.yaml" } } as unknown as Bundle),
    ).toEqual({});
  });
});

describe("traceLinkState (#718 §A)", () => {
  const base = "https://cloud.langfuse.com/project/p-1";

  it("links to a specific trace when a trace id is known", async () => {
    const { traceLinkState } = await import("./links");
    expect(traceLinkState({ langfuseBase: base, observer: "langfuse", reachable: true, traceId: "tr-1" })).toEqual({
      kind: "link",
      href: "https://cloud.langfuse.com/project/p-1/traces/tr-1",
    });
  });

  it("falls back to a trace-search link when only a search term is known (executions table)", async () => {
    const { traceLinkState } = await import("./links");
    expect(traceLinkState({ langfuseBase: base, searchTerm: "exec-1" })).toEqual({
      kind: "link",
      href: "https://cloud.langfuse.com/project/p-1/traces?search=exec-1",
    });
  });

  it("prefers a precise trace-id link over the search-term fallback when both are known", async () => {
    const { traceLinkState } = await import("./links");
    expect(
      traceLinkState({
        langfuseBase: base,
        observer: "langfuse",
        reachable: true,
        traceId: "tr-1",
        searchTerm: "exec-1",
      }),
    ).toEqual({ kind: "link", href: "https://cloud.langfuse.com/project/p-1/traces/tr-1" });
  });

  it("distinguishes none / unreachable / no-trace with the same inputs the correlation contract carries", async () => {
    const { traceLinkState } = await import("./links");
    // No configured base, a non-Langfuse observer, or explicit `none` → not traced; the observer
    // name rides along so the descriptive rendering can say WHICH observer.
    expect(traceLinkState({ langfuseBase: null, searchTerm: "exec-1" })).toEqual({ kind: "none" });
    expect(traceLinkState({ langfuseBase: base, observer: "none", traceId: "tr-1" })).toEqual({
      kind: "none",
      observer: "none",
    });
    expect(traceLinkState({ langfuseBase: base, observer: "otel", traceId: "tr-1" })).toEqual({
      kind: "none",
      observer: "otel",
    });
    // Configured but unreachable outranks any trace id.
    expect(
      traceLinkState({ langfuseBase: base, observer: "langfuse", reachable: false, traceId: "tr-1" }),
    ).toEqual({ kind: "unreachable" });
    // Reachable Langfuse with nothing to point at yet.
    expect(traceLinkState({ langfuseBase: base, observer: "langfuse", reachable: true })).toEqual({
      kind: "no-trace",
    });
  });
});
