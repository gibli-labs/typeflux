/**
 * Project registry for the TS control-plane HTTP server (#620; Python
 * `controlplane/registry.py` `load_project_registry` / `ProjectRegistry` / `ProjectSummary`).
 * Reads a `typeflux.projects.yaml` and serves one or several projects; the single-manifest server
 * is the degenerate case (a registry of one). This slice is LOCAL checkout paths only — no Git
 * source (`repo:` entries are rejected), so every summary is `source: "local"` with null repo_*.
 *
 * Freshness contract (Python parity): each request re-loads the routed project's bundle from disk
 * (`resolveProject`) so a control plane reflects the YAML on disk. `runtime` declares the project's
 * language runtime — a bad/empty runtime FAILS CLOSED at load (never silently the default). THIS
 * server resolves TypeScript, so `resolvable = runtime === "typescript"`; a python-runtime project
 * stays inspectable (pure-YAML reads) but its resolution-dependent routes answer 501.
 */

import { readFileSync } from "node:fs";
import { dirname, isAbsolute, resolve } from "node:path";

import {
  type LoadedProjectBundle,
  loadProjectBundle,
  MAX_YAML_ALIASES,
  MAX_YAML_BYTES,
} from "@typeflux/temporal-yaml";
import type { ProjectRuntime } from "../project-control-plane.js";
import { parse as parseYaml } from "yaml";

import { ProjectControlPlaneError } from "../errors.js";

/** Runtimes this control plane can RESOLVE in-process (#619). The TS server resolves TypeScript. */
export const SUPPORTED_RESOLVER_RUNTIMES: readonly ProjectRuntime[] = ["typescript"];

const RUNTIMES: readonly ProjectRuntime[] = ["python", "typescript"];

/** One registered project: a stable id, a local manifest path, and a declared runtime (default python). */
export interface ProjectRegistryEntry {
  id: string;
  manifestPath: string;
  runtime: ProjectRuntime;
}

/** Read projection of a registered project for the console switcher (Python `ProjectSummary`). */
export interface ProjectSummary {
  id: string;
  name: string;
  manifest_path: string;
  default: boolean;
  runtime: ProjectRuntime;
  /** Whether THIS server can resolve the project (its runtime is a supported resolver runtime). */
  resolvable: boolean;
  /** Local-only in this slice; Git sourcing is a later slice. */
  source: "local" | "git";
  repo_url: string | null;
  repo_ref: string | null;
  repo_sha: string | null;
  manifest_repo_path: string | null;
  last_refresh: null;
  /** False when the manifest failed to load; `detail` carries the error, the listing degrades per-project. */
  available: boolean;
  detail: string | null;
}

/**
 * The result of a project refresh (Python `ProjectRefreshResult`, `controlplane/git_source.py`).
 * Local-only in this slice, so `source` is always `"local"` and `refreshed` false; the git
 * provenance fields (`ref`/`sha`) are null. No `exclude_none` on the refresh route, so every field
 * serializes (nulls included), matching Python's wire shape.
 */
export interface ProjectRefreshResult {
  id: string;
  source: "local" | "git";
  refreshed: boolean;
  ref: string | null;
  sha: string | null;
  refreshed_at: string | null;
  detail: string | null;
}

const isResolvable = (runtime: ProjectRuntime): boolean => SUPPORTED_RESOLVER_RUNTIMES.includes(runtime);

/** An ordered set of registered projects with a default alias (Python `ProjectRegistry`). */
export class ProjectRegistry {
  private readonly byId: Map<string, ProjectRegistryEntry>;
  readonly defaultId: string;

  constructor(
    readonly entries: readonly ProjectRegistryEntry[],
    defaultId: string,
  ) {
    if (entries.length === 0) {
      throw new ProjectControlPlaneError("project registry must list at least one project", 422);
    }
    const ids = entries.map((entry) => entry.id);
    const duplicates = [...new Set(ids.filter((id, index) => ids.indexOf(id) !== index))].sort();
    if (duplicates.length > 0) {
      throw new ProjectControlPlaneError(`project registry has duplicate ids: ${duplicates.join(", ")}`, 422);
    }
    if (!ids.includes(defaultId)) {
      throw new ProjectControlPlaneError(`default project '${defaultId}' is not a registered id`, 422);
    }
    this.byId = new Map(entries.map((entry) => [entry.id, entry]));
    this.defaultId = defaultId;
  }

  /** The entry for `projectId`; the default when `projectId` is undefined. 404 (NotFound) when unknown. */
  entry(projectId: string | undefined): ProjectRegistryEntry {
    const target = projectId ?? this.defaultId;
    const entry = this.byId.get(target);
    if (entry === undefined) {
      throw new ProjectControlPlaneError(`unknown project: ${target}`, 404);
    }
    return entry;
  }

  /** Whether the serving resolver covers the routed project's declared runtime (Python parity). */
  entryRuntimeResolvable(projectId: string | undefined): boolean {
    return isResolvable(this.entry(projectId).runtime);
  }

  /**
   * Whether the routed project is OPERABLE — its lifecycle binding driver can run here (Python
   * `_entry_operable`, #618). A TypeScript project is plan-less (always operable); every other
   * runtime needs this server's resolver to cover it, so on a TS-resolving server it reduces to
   * `resolvable`. Kept as its own method (not a literal) so a second resolver runtime tracks both.
   */
  entryOperable(projectId: string | undefined): boolean {
    const runtime = this.entry(projectId).runtime;
    return runtime === "typescript" || isResolvable(runtime);
  }

  /**
   * Fail closed before a lifecycle operation (status/review/cancel) that needs a binding DRIVER,
   * not resolution (#618/#563). An unknown project id is 404 (via `entry`); a runtime whose binding
   * driver this server cannot run is 501 `UnsupportedRuntime`. On a TS-resolving server this
   * reduces to `resolvable` for non-TS runtimes (the TS driver is always operable). Mirrors
   * Python's `_require_operable`.
   */
  requireOperable(projectId: string | undefined): void {
    const entry = this.entry(projectId);
    if (!this.entryOperable(projectId)) {
      const supported = [...SUPPORTED_RESOLVER_RUNTIMES].sort().join(", ");
      throw new ProjectControlPlaneError(
        `project '${entry.id}' declares runtime '${entry.runtime}', whose binding driver requires ` +
          `resolution this server does not provide (supported: ${supported})`,
        501,
        "UnsupportedRuntime",
      );
    }
  }

  /**
   * Fail closed before a resolution-dependent operation (#619). An unknown project id is 404
   * (checked by `entry`); an unresolvable runtime is 501 `UnsupportedRuntime` with the message
   * template that names the project's runtime + this server's supported set.
   */
  requireResolvable(projectId: string | undefined): void {
    const entry = this.entry(projectId);
    if (!isResolvable(entry.runtime)) {
      const supported = [...SUPPORTED_RESOLVER_RUNTIMES].sort().join(", ");
      throw new ProjectControlPlaneError(
        `project '${entry.id}' declares runtime '${entry.runtime}', which this server cannot resolve ` +
          `(supported: ${supported}); resolution-dependent operations are unavailable for it`,
        501,
        "UnsupportedRuntime",
      );
    }
  }

  /**
   * The declared GitHub repo source for the project — the served side of the github-provenance drift
   * comparison (#727; Python `ProjectRegistry.repo_source` → `repo_url`/`ref`). ALWAYS `null` on this
   * edition: the TS control plane serves LOCAL checkouts only (Git `repo:` sources are rejected at
   * load, see {@link loadProjectRegistry}), so no repo provenance is recorded and the surface reports
   * `not_configured` with no network call. Kept as an overridable method (not an inline `null`) so it
   * is the single git-provenance seam a future Git-source slice — or a test — replaces, matching
   * Python's registry accessors the endpoint reads.
   */
  repoSource(_projectId: string | undefined): { url: string; ref: string } | null {
    return null;
  }

  /**
   * The current clone HEAD sha for a Git-sourced project — the served checkout sha the surface
   * compares against the remote branch HEAD (#727; Python `ProjectRegistry.repo_head_sha`). ALWAYS
   * `null` here: no clone exists (local checkouts only), so there is no served sha to resolve.
   */
  repoHeadSha(_projectId: string | undefined): string | null {
    return null;
  }

  /**
   * The last commit that touched a deployment plan FILE in the project's clone — the commit the
   * github-provenance surface attributes the plan's approving PR to (#727; Python
   * `ProjectRegistry.plan_file_sha`, `git log -1 -- <file>`). ALWAYS `null` here: the TS control
   * plane runs NO clone and NO git subprocess, and shelling out to git is not an established TS-CP
   * pattern — so every plan's `sha` (and therefore its `pr`) is an honest null, never fabricated.
   */
  planFileSha(_projectId: string | undefined, _manifestRelativePath: string): string | null {
    return null;
  }

  /** Re-load the routed project's bundle from disk (per-request freshness). Throws its load error verbatim. */
  resolveProject(projectId: string | undefined): LoadedProjectBundle {
    return loadProjectBundle(this.entry(projectId).manifestPath);
  }

  /**
   * Re-fetch a project's source (Python `ProjectRegistry.refresh` → `ProjectRefreshResult`). This
   * slice is LOCAL checkouts only (Git `repo:` entries are rejected at load), so refresh is always
   * a no-op that returns the local-checkout result verbatim — the exact shape + `detail` string
   * Python emits for a local entry, so a client sees identical JSON. An unknown project id 404s
   * (via `entry`). `refreshed_at` stamps the attempt time (ISO-8601 UTC).
   */
  refresh(projectId: string): ProjectRefreshResult {
    const entry = this.entry(projectId); // 404 on an unknown id
    return {
      id: entry.id,
      source: "local",
      refreshed: false,
      ref: null,
      sha: null,
      refreshed_at: new Date().toISOString(),
      detail: "local checkout — nothing to refresh",
    };
  }

  /**
   * Every registered project as a summary (Python `ProjectRegistry.summaries`). One unloadable
   * manifest degrades to `available: false` with the error `detail` so a single bad entry never
   * fails the whole switcher; `name` falls back to the id, `manifest_path` to "".
   */
  summaries(): ProjectSummary[] {
    return this.entries.map((entry) => {
      const base = {
        id: entry.id,
        default: entry.id === this.defaultId,
        runtime: entry.runtime,
        resolvable: isResolvable(entry.runtime),
        source: "local" as const,
        repo_url: null,
        repo_ref: null,
        repo_sha: null,
        manifest_repo_path: null,
        last_refresh: null,
      };
      try {
        const bundle = loadProjectBundle(entry.manifestPath);
        return {
          ...base,
          name: bundle.project.name,
          manifest_path: entry.manifestPath,
          available: true,
          detail: null,
        };
      } catch (error) {
        return {
          ...base,
          name: entry.id,
          manifest_path: "",
          available: false,
          detail: error instanceof Error ? error.message : String(error),
        };
      }
    });
  }

  /** A registry of one — the single-manifest server as the degenerate case (Python `ProjectRegistry.single`). */
  static single(manifestPath: string, projectId = "default", runtime: ProjectRuntime = "typescript"): ProjectRegistry {
    return new ProjectRegistry([{ id: projectId, manifestPath: resolve(manifestPath), runtime }], projectId);
  }
}

/**
 * Load a `typeflux.projects.yaml` registry file (Python `load_project_registry`). Shape:
 *
 *   version: "1"
 *   default: <project-id>     # optional; first entry otherwise
 *   projects:
 *     - id: <project-id>
 *       manifest: <path to typeflux.project.yaml>   # relative to this file
 *       runtime: python | typescript                # optional; default python
 *
 * Each entry's `manifest` resolves relative to the registry file. A bad/empty `runtime` — or any
 * value other than python/typescript — fails closed (never silently the default). Git `repo:`
 * sources are not supported in this slice and are rejected.
 */
export function loadProjectRegistry(path: string): ProjectRegistry {
  const registryPath = resolve(path);
  let raw: unknown;
  try {
    const text = readFileSync(registryPath, "utf8");
    // The same strictness bounds every other YAML entry point applies (temporal-yaml's loader):
    // a size cap and an alias-expansion cap (billion-laughs guard) — the registry file must not
    // be the one unbounded parse in the server.
    if (Buffer.byteLength(text, "utf8") > MAX_YAML_BYTES) {
      throw new Error(`registry exceeds the ${MAX_YAML_BYTES} byte YAML limit`);
    }
    raw = parseYaml(text, { uniqueKeys: true, maxAliasCount: MAX_YAML_ALIASES });
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    const missing = (error as NodeJS.ErrnoException | undefined)?.code === "ENOENT";
    throw new ProjectControlPlaneError(
      missing ? `project registry not found: ${registryPath}` : `project registry ${registryPath} failed to parse: ${detail}`,
      422,
    );
  }
  if (raw === null || typeof raw !== "object" || Array.isArray(raw)) {
    throw new ProjectControlPlaneError(`project registry ${registryPath} is not a YAML mapping`, 422);
  }
  const record = raw as Record<string, unknown>;
  const projects = record["projects"];
  if (!Array.isArray(projects) || projects.length === 0) {
    throw new ProjectControlPlaneError(`project registry ${registryPath} must list a non-empty 'projects'`, 422);
  }

  const base = dirname(registryPath);
  const entries: ProjectRegistryEntry[] = projects.map((item, index) => {
    if (item === null || typeof item !== "object" || Array.isArray(item)) {
      throw new ProjectControlPlaneError(`project registry entry #${index} is not a mapping`, 422);
    }
    const entry = item as Record<string, unknown>;
    const id = entry["id"];
    if (typeof id !== "string" || id.length === 0) {
      throw new ProjectControlPlaneError(`project registry entry #${index} is missing a string 'id'`, 422);
    }
    if (entry["repo"] !== undefined) {
      throw new ProjectControlPlaneError(
        `project registry entry '${id}' uses a Git 'repo' source, which this server does not support yet`,
        422,
      );
    }
    const manifest = entry["manifest"];
    if (typeof manifest !== "string" || manifest.length === 0) {
      throw new ProjectControlPlaneError(`project registry entry '${id}' 'manifest' must be a non-empty string`, 422);
    }
    // Fail closed on anything but the two known runtimes — INCLUDING the empty string and a
    // present-but-null key (`runtime:`): a blank runtime must never silently mean the default
    // (Python parity, registry.py — only an ABSENT key defaults).
    const runtime = Object.hasOwn(entry, "runtime") ? entry["runtime"] : "python";
    if (typeof runtime !== "string" || !RUNTIMES.includes(runtime as ProjectRuntime)) {
      throw new ProjectControlPlaneError(
        `project registry entry '${id}' 'runtime' must be 'python' or 'typescript', got ${JSON.stringify(runtime)}`,
        422,
      );
    }
    const manifestPath = isAbsolute(manifest) ? resolve(manifest) : resolve(base, manifest);
    return { id, manifestPath, runtime: runtime as ProjectRuntime };
  });

  const declaredDefault = record["default"];
  if (declaredDefault !== undefined && typeof declaredDefault !== "string") {
    throw new ProjectControlPlaneError(`project registry ${registryPath} 'default' must be a string id`, 422);
  }
  const defaultId = (declaredDefault as string | undefined) ?? entries[0]!.id;
  return new ProjectRegistry(entries, defaultId);
}
