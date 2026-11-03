/**
 * Filesystem project loader (governance parity, #454; Python `project/loader.py`
 * `load_project_spec` + `discover_project_workflows` + the reference-file reading in
 * `validate_project`). The thin **Node** helper that turns a project directory into the
 * in-memory {@link ProjectBundleSources} the injection-based validators consume — so
 * `validateProjectBundle` / `resolveEnvironmentWorkflow` are usable end-to-end from a
 * real `typeflux.project.yaml` without every caller wiring up `fs` themselves.
 *
 * This is the ONLY module in the package that reads project files. It stays a separate,
 * clearly Node-only seam: the loaders/validators remain pure + text-only (testable without
 * a filesystem), and this maps paths → text on top of them.
 *
 * Reads the SPEC files (manifest, workflow YAMLs, policy specs, environment specs). A
 * declared file that does not exist is OMITTED from `sources` (not an error) so
 * `validateProjectBundle` can report the gap in-band (`missing_policy_source`,
 * `missing_environment_source`, a per-workflow resolution failure); a malformed spec file
 * throws with its parse error. `.env` file VALUES are NOT read here — consistent with the
 * environment spec's text-only stance — so a project relying on `env_files` supplies them
 * to the validator via `envFileValues` (inline `variables` need no file reading).
 */

import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { isAbsolute, join, resolve } from "node:path";

import { loadEnvironmentSpec, type ProjectEnvironmentSpec } from "./environment-spec.js";
import { loadProfileSpec, PROFILE_KINDS, type ProjectProfileSpec } from "./profile.js";
import { loadPolicySpec, type TypefluxProjectPolicySpec } from "./policy.js";
import type { ProjectBundleSources } from "./project-validation.js";
import { loadProjectSpec, type TypefluxProjectSpec } from "./project-spec.js";

/** A loaded project + the injected sources built from its files, ready for the validators. */
export interface LoadedProjectBundle {
  project: TypefluxProjectSpec;
  sources: ProjectBundleSources;
}

/** Expand a leading `~` / `~/` to the home directory (Python `Path.expanduser`, common cases). */
const expandHome = (path: string): string =>
  path === "~" ? homedir() : path.startsWith("~/") ? join(homedir(), path.slice(2)) : path;

/**
 * Resolve a manifest-relative reference to an absolute path (Python `_resolve_project_path`):
 * expand `~`, then an absolute path is used as-is, otherwise it resolves against the project dir.
 */
const resolveProjectPath = (projectDir: string, rawPath: string): string => {
  const expanded = expandHome(rawPath);
  return isAbsolute(expanded) ? resolve(expanded) : resolve(projectDir, expanded);
};

// A reference resolves to no readable file: absent (ENOENT), routed through a non-directory
// (ENOTDIR — e.g. `policies/x.yaml` where `policies` is a file), pointed at a directory (EISDIR),
// a broken symlink cycle (ELOOP), or an unusable path (ENAMETOOLONG). All are "declared file
// missing" → omit so the validator reports the gap in-band (like Python's `Path.exists()` → False,
// which reports `missing_*_file` and keeps loading the rest); a real IO error (e.g. EACCES)
// still propagates so one bad reference never silently corrupts the bundle.
const MISSING_FILE_CODES = new Set(["ENOENT", "ENOTDIR", "EISDIR", "ELOOP", "ENAMETOOLONG"]);

/** Read a file's text, or `undefined` if no readable file exists at `path` (other errors propagate). */
function readTextIfExists(path: string): string | undefined {
  try {
    return readFileSync(path, "utf-8");
  } catch (error) {
    if (MISSING_FILE_CODES.has((error as NodeJS.ErrnoException).code ?? "")) return undefined;
    throw error;
  }
}

/** The file a project workflow resolves to: its `path`, or `directory` + `defaults.workflow_filename`. */
function workflowFilePath(project: TypefluxProjectSpec, workflow: TypefluxProjectSpec["workflows"][number], projectDir: string): string {
  if (workflow.path !== undefined) return resolveProjectPath(projectDir, workflow.path);
  // The spec guarantees exactly one of path|directory, so `directory` is set here.
  const directory = resolveProjectPath(projectDir, workflow.directory as string);
  return join(directory, project.defaults.workflow_filename);
}

/**
 * Load a project bundle from its manifest file (Python `load_project_spec` +
 * `discover_project_workflows` + reference-file loading). Reads the manifest, then every
 * declared workflow / policy / environment file relative to the manifest's directory, and
 * returns the `project` spec plus the {@link ProjectBundleSources} for the validators.
 *
 * Throws if the manifest itself is missing/unreadable/invalid, or if a declared policy or
 * environment file EXISTS but fails to parse. A merely-absent declared file is omitted so the
 * validator surfaces it (see the module note).
 */
export function loadProjectBundle(manifestPath: string): LoadedProjectBundle {
  const resolvedManifest = resolveProjectPath(process.cwd(), manifestPath);
  const manifestText = readTextIfExists(resolvedManifest);
  if (manifestText === undefined) {
    throw new Error(`project manifest not found: ${resolvedManifest}`);
  }
  const project = loadProjectSpec(manifestText, { sourceLabel: resolvedManifest });
  const projectDir = resolve(resolvedManifest, "..");

  const workflows: Record<string, string> = {};
  for (const workflow of project.workflows) {
    const text = readTextIfExists(workflowFilePath(project, workflow, projectDir));
    if (text !== undefined) workflows[workflow.id] = text;
  }

  const policies: Record<string, TypefluxProjectPolicySpec> = {};
  for (const [policyId, reference] of Object.entries(project.policies)) {
    const path = resolveProjectPath(projectDir, reference);
    const text = readTextIfExists(path);
    if (text !== undefined) policies[policyId] = loadPolicySpec(text, { sourceLabel: path });
  }

  const environments: Record<string, ProjectEnvironmentSpec> = {};
  for (const [environmentId, reference] of Object.entries(project.environments)) {
    const path = resolveProjectPath(projectDir, reference);
    const text = readTextIfExists(path);
    if (text !== undefined) environments[environmentId] = loadEnvironmentSpec(text, { sourceLabel: path });
  }

  const profiles: Record<string, Record<string, ProjectProfileSpec>> = {};
  for (const kind of PROFILE_KINDS) {
    const declared = project.profiles?.[kind] ?? {};
    const loaded: Record<string, ProjectProfileSpec> = {};
    for (const [profileId, reference] of Object.entries(declared)) {
      const path = resolveProjectPath(projectDir, reference);
      const text = readTextIfExists(path);
      if (text !== undefined) loaded[profileId] = loadProfileSpec(text, { sourceLabel: path, declaredKind: kind });
    }
    profiles[kind] = loaded;
  }

  return {
    project,
    sources: {
      policies,
      environments,
      workflows,
      profiles: profiles as ProjectBundleSources["profiles"],
    },
  };
}
