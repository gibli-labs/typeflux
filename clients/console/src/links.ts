/**
 * External deep-link builders (#250). The console correlates code, prompts,
 * and runs — Temporal Web and Langfuse own the deep detail, so wherever
 * they do, we link out instead of redrawing. Base URLs come from the
 * bundle's `links` section (operator-configured per environment); callers
 * render no affordance when a base is absent.
 */

import type { Bundle } from "./api";

function trimBase(base: string): string {
  return base.replace(/\/+$/, "");
}

/** The operator-configured Langfuse project base for a resolved bundle. */
export function langfuseBaseOf(bundle: Bundle | undefined | null): string | null {
  return bundle?.links?.langfuse_project ?? null;
}

/** The Temporal namespace the bundle resolves to (for execution deep links). */
export function temporalNamespaceOf(bundle: Bundle | undefined | null): string | null {
  return (
    ((bundle?.runtime?.["temporal"] as Record<string, unknown> | undefined)?.["namespace"] as
      | string
      | undefined) ?? null
  );
}

export function temporalExecutionUrl(
  base: string,
  namespace: string | null | undefined,
  workflowId: string,
  runId?: string | null,
): string {
  const ns = encodeURIComponent(namespace || "default");
  const path = `${trimBase(base)}/namespaces/${ns}/workflows/${encodeURIComponent(workflowId)}`;
  return runId ? `${path}/${encodeURIComponent(runId)}/history` : path;
}

export function langfusePromptUrl(base: string, promptName: string): string {
  return `${trimBase(base)}/prompts/${encodeURIComponent(promptName)}`;
}

export function langfuseTracesUrl(base: string, workflowId: string): string {
  return `${trimBase(base)}/traces?search=${encodeURIComponent(workflowId)}`;
}

export function langfuseTraceUrl(base: string, traceId: string): string {
  return `${trimBase(base)}/traces/${encodeURIComponent(traceId)}`;
}

/** Web repo base from a clone URL: GitHub pages are rooted WITHOUT the `.git` suffix (codex). */
const githubBase = (repoUrl: string): string => trimBase(repoUrl).replace(/\.git$/, "");

const encodePath = (path: string): string => path.split("/").map(encodeURIComponent).join("/");

export function githubBlobUrl(repoUrl: string, sha: string, path: string): string {
  return `${githubBase(repoUrl)}/blob/${sha}/${encodePath(path)}`;
}

export function githubCommitUrl(repoUrl: string, sha: string): string {
  return `${githubBase(repoUrl)}/commit/${sha}`;
}

/** Commit HISTORY of one path at a sha (`/commits/<sha>/<path>`) — the "what changed" hand-off
 * that sits next to the blob link. Pure URL construction, no API calls (#718 §A). */
export function githubCommitsUrl(repoUrl: string, sha: string, path: string): string {
  return `${githubBase(repoUrl)}/commits/${sha}/${encodePath(path)}`;
}

/**
 * GitHub's three-dot compare view (`/compare/<base>...<head>`) — the commits reachable from
 * `head` but not `base`, i.e. exactly "what the served checkout hasn't picked up" (#727 §7). Pure
 * URL construction, no API calls; host-gating stays with the caller via {@link
 * validatedGithubRepoUrl} (the same discipline every other GitHub builder here follows).
 */
export function githubCompareUrl(repoUrl: string, base: string, head: string): string {
  return `${githubBase(repoUrl)}/compare/${encodeURIComponent(base)}...${encodeURIComponent(head)}`;
}

/**
 * Repo-path references that resolve differently server-side, or aren't repo-relative at all, and
 * so must suppress the link rather than emit a wrong repo path (codex P3): POSIX-absolute paths,
 * `~` home expansion, Windows drive letters, and backslash separators. Shared by the
 * manifest-relative-reference resolver and the manifest-path validator so both reject the same
 * shapes from one place.
 */
function isUnsafeRepoPath(path: string): boolean {
  return (
    path.startsWith("/") || path.startsWith("~") || path.includes("\\") || /^[a-zA-Z]:/.test(path)
  );
}

/**
 * Resolve a MANIFEST-relative reference to its repo-relative path (#606): join it onto the
 * manifest's directory within the repo and normalize `.`/`..`. Returns undefined — no link,
 * never a wrong link — for absolute paths, `~` expansion, Windows paths, or traversal escaping
 * the repo root.
 */
export function repoRelativePath(
  manifestRepoPath: string | null | undefined,
  reference: string,
): string | undefined {
  if (!manifestRepoPath || isUnsafeRepoPath(reference)) return undefined;
  const manifestDir = manifestRepoPath.includes("/")
    ? manifestRepoPath.slice(0, manifestRepoPath.lastIndexOf("/"))
    : "";
  const segments: string[] = [];
  for (const segment of `${manifestDir}/${reference}`.split("/")) {
    if (segment === "" || segment === ".") continue;
    if (segment === "..") {
      if (segments.length === 0) return undefined; // escapes the repo root
      segments.pop();
      continue;
    }
    segments.push(segment);
  }
  return segments.length > 0 ? segments.join("/") : undefined;
}

/**
 * The manifest's OWN repo-relative path, validated: undefined for absolute, `~`, Windows, or any
 * `..`-traversal path — the same rejections {@link repoRelativePath} applies to manifest-relative
 * references, so a malformed manifest path suppresses the link rather than pointing at the wrong
 * repo file. Unlike a reference it has no base to join onto, so `..` is rejected outright rather
 * than normalized.
 */
function validManifestRepoPath(path: string | null | undefined): string | undefined {
  if (!path || isUnsafeRepoPath(path)) return undefined;
  if (path.split("/").includes("..")) return undefined;
  return path;
}

/** Git provenance a page carries for a source-of-truth file: the project's repo + resolved sha
 * plus the manifest's repo-relative path (which anchors relative references). */
export interface RepoProvenance {
  repo_url?: string | null;
  repo_sha?: string | null;
  manifest_repo_path?: string | null;
}

/**
 * A free-form repo URL validated to a GitHub WEB host we can build correct `/blob/`|`/commits/`|
 * `/commit/` urls for, else undefined. The registry also clones ssh/local-path URLs (no web pages
 * at all) and non-GitHub https hosts (GitLab is `/-/blob/`, Bitbucket `/src/` — GitHub's shape
 * would 404 there). The host check anchors the REGISTRABLE domain — a prefix match would pass
 * `github.com.evil.com`, the classic allowlist bypass (finder). Self-hosted GHE domains aren't
 * verifiable client-side and stay unlinked for now. This is the single host gate every GitHub
 * link builder routes through, including callers that hold a raw `repo_url` (bundle code).
 *
 * PORT PARITY: both control planes re-implement this exact host gate — Python
 * `parse_github_repo` in `packages/python/src/typeflux/project/github_provenance.py`
 * and TS `parseGithubRepo` in
 * `packages/typescript/temporal-controlplane/src/github-provenance.ts` (#727) — the server-side
 * github-provenance surface must gate REST calls to the same hosts this builds links for. Keep all
 * three in lockstep: a change to the accepted-host rule here (or a new bypass to reject) must
 * travel to both functions too.
 */
export function validatedGithubRepoUrl(repoUrl: string | null | undefined): string | undefined {
  if (!repoUrl) return undefined;
  let host: string;
  try {
    host = new URL(repoUrl).hostname;
  } catch {
    return undefined;
  }
  const githubHost = host === "github.com" || host.endsWith(".github.com");
  if (!/^https?:\/\//.test(repoUrl) || !githubHost) return undefined;
  return repoUrl;
}

/**
 * The validated {repoUrl, sha} for a project we can build correct GitHub WEB urls for, else
 * undefined — the host allowlist plus a resolved sha.
 */
function githubProjectRef(
  project: RepoProvenance | undefined,
): { repoUrl: string; sha: string } | undefined {
  if (!project?.repo_sha) return undefined;
  const repoUrl = validatedGithubRepoUrl(project.repo_url);
  return repoUrl === undefined ? undefined : { repoUrl, sha: project.repo_sha };
}

/**
 * A source-of-truth file's external hand-off links: the blob at the resolved sha plus its commit
 * history (#718 §A). Both undefined when the project carries no usable git provenance or the path
 * can't be resolved — the {@link SourceLinks} component renders that as an explicit
 * "unavailable" note rather than a silently absent affordance.
 */
export interface SourceLinkPair {
  blob?: string;
  history?: string;
}

/** Build the {blob, history} pair for a resolved ref + repo-relative path, or `{}` when either is
 * missing — the one place blob-vs-commits URL construction lives. */
function sourceLinksAt(
  ref: { repoUrl: string; sha: string } | undefined,
  path: string | undefined,
): SourceLinkPair {
  if (ref === undefined || path === undefined) return {};
  return {
    blob: githubBlobUrl(ref.repoUrl, ref.sha, path),
    history: githubCommitsUrl(ref.repoUrl, ref.sha, path),
  };
}

/** The {blob, history} links for a manifest-relative definition reference, from project
 * provenance; `{}` when the project is not git-sourced or the path can't be resolved. */
export function definitionSourceLinks(
  project: RepoProvenance | undefined,
  reference: string,
): SourceLinkPair {
  return sourceLinksAt(
    githubProjectRef(project),
    repoRelativePath(project?.manifest_repo_path, reference),
  );
}

/** The {blob, history} links for the project MANIFEST itself (its own repo-relative path, not a
 * manifest-relative reference); `{}` without git provenance or a valid relative manifest path. */
export function manifestSourceLinks(project: RepoProvenance | undefined): SourceLinkPair {
  return sourceLinksAt(githubProjectRef(project), validManifestRepoPath(project?.manifest_repo_path));
}

/**
 * The {blob, history} links for a resolved bundle's OWN workflow source file (#721): the code
 * banner's `repo_url`/`sha`/`workflow_path`, routed through the same GitHub host allowlist every
 * other builder uses. `{}` (the loud "unavailable" note) when the bundle carries no usable git
 * provenance — a `dirty` checkout still links its committed sha, matching `CodeProvenanceBanner`.
 * The single place bundle-code source links are built, so the persona views and the workflow
 * banner never drift apart.
 *
 * Only `code.workflow_path` is link-safe: it is the REPO-relative path the server derives for a
 * Git-sourced checkout. There is deliberately NO fallback to `bundle.workflow.path` — that field
 * is the raw resolved FILESYSTEM path (`str(resolved.workflow_path)`), which becomes the chosen
 * path exactly when `code.workflow_path` is legitimately null and would leak host paths into
 * `blob/<sha>/Users/...` URLs. A null `code.workflow_path` therefore degrades LOUDLY to `{}`.
 */
export function bundleSourceLinks(bundle: Bundle | undefined | null): SourceLinkPair {
  const code = bundle?.code ?? null;
  if (!code) return {};
  const repoUrl = validatedGithubRepoUrl(code.repo_url);
  const path = code.workflow_path;
  if (repoUrl === undefined || !path) return {};
  return { blob: githubBlobUrl(repoUrl, code.sha, path), history: githubCommitsUrl(repoUrl, code.sha, path) };
}

/**
 * Trace-link three-state standardization (#718 §A / #577 §6): a Langfuse trace link degrades
 * loudly with distinct copy, never silently vanishing. The correlation contract already
 * distinguishes observer `none` / unreachable / no-trace; this collapses the same inputs to a
 * single verdict every trace-link surface reuses.
 *
 * - `link`     — a resolvable Langfuse URL (a specific trace when a `traceId` is known, else a
 *                workflow/execution `search` link).
 * - `none`     — no observer is configured (observer `none`, a non-Langfuse observer, or no
 *                `langfuseBase`): runs are not traced.
 * - `unreachable` — the observer is configured but the backend could not be reached.
 * - `no-trace` — reachable, but no trace has been recorded for this execution yet.
 */
export type TraceLinkState =
  | { kind: "link"; href: string }
  | { kind: "none"; observer?: string | null }
  | { kind: "unreachable" }
  | { kind: "no-trace" };

export function traceLinkState(input: {
  langfuseBase: string | null | undefined;
  /** The correlation observer, when known (`none`/`langfuse`/…); omit when un-probed. */
  observer?: string | null;
  /** Whether the observer backend was reachable, when known. */
  reachable?: boolean | null;
  /** A specific trace id → a direct trace link (preferred). */
  traceId?: string | null;
  /** A workflow/execution term → a trace-SEARCH link when no specific trace id is available. */
  searchTerm?: string | null;
}): TraceLinkState {
  const { langfuseBase, observer, reachable, traceId, searchTerm } = input;
  // A known non-Langfuse observer (incl. explicit `none`) means these runs aren't Langfuse-traced.
  // Carry the observer name so the descriptive rendering can say WHICH observer (panel copy).
  if ((observer !== undefined && observer !== null && observer !== "langfuse") || !langfuseBase) {
    return { kind: "none", observer };
  }
  if (reachable === false) return { kind: "unreachable" };
  if (traceId) return { kind: "link", href: langfuseTraceUrl(langfuseBase, traceId) };
  if (searchTerm) return { kind: "link", href: langfuseTracesUrl(langfuseBase, searchTerm) };
  return { kind: "no-trace" };
}
