/**
 * Component-profile projections for the control-plane explorer (#570; Python
 * `project/definitions.py` `profile_definitions` / `profile_definition` + `ProfileSummary` /
 * `ProfileDefinition`). Profiles are config fragments; secrets appear only as `value_from`
 * references, so `runtime` renders in full. `content_hash` is the cross-SDK hash — the same
 * profile file reports the same digest from either control plane.
 *
 * Unlike policies, Python enforces NO name-must-match-id rule for profiles — none is added
 * here (parity, not hardening).
 */

import {
  PROFILE_KINDS,
  payloadCodecKeySpec,
  profileContentHash,
  type ProfileKind,
  type ProjectBundleSources,
  type ProjectProfileSpec,
  SECRET_SLOT_PATHS,
  secretValueSpec,
  type TypefluxProjectSpec,
} from "@typeflux/temporal-yaml";

import { sanitizeHost } from "./connections.js";
import { ProjectControlPlaneError } from "./errors.js";

const asc = (a: string, b: string): number => (a < b ? -1 : a > b ? 1 : 0);

/**
 * Redact LITERAL credentials from a profile's runtime before it leaves the API (codex): a
 * `value_from` reference is provenance and renders as-is, but an inline literal in a shared,
 * control-plane-published profile file must never be echoed back. The literal is replaced
 * with `"***"`, keeping the key visible so operators can see the slot is set; Python's
 * `profile_definition` applies the same masking (#600 — parity both ways).
 *
 * The slots come from `SECRET_SLOT_PATHS` — the ONE fixed allowlist `secretReferenceRecords`
 * walks — so a future secret slot cannot be reported there yet leak here.
 */
function redactLiteralSecrets(runtime: Record<string, unknown>): Record<string, unknown> {
  for (const slotPath of SECRET_SLOT_PATHS) {
    if (slotPath.includes("{*}")) {
      redactMapSlot(runtime, slotPath);
      continue;
    }
    if (slotPath.includes("[*]")) {
      redactWildcardSlot(runtime, slotPath);
      continue;
    }
    const segments = slotPath.split(".").slice(1); // strip the "runtime." root
    let node: unknown = runtime;
    for (const segment of segments.slice(0, -1)) {
      node =
        typeof node === "object" && node !== null && Object.hasOwn(node, segment)
          ? (node as Record<string, unknown>)[segment]
          : undefined;
    }
    const leaf = segments[segments.length - 1];
    if (typeof node !== "object" || node === null || leaf === undefined || !Object.hasOwn(node, leaf)) {
      continue;
    }
    const record = node as Record<string, unknown>;
    const value = record[leaf];
    // Only an EXACT secret reference is provenance (the strict `secretValueSpec` shape —
    // exactly one of env/file, no extra keys); anything else occupying a secret slot —
    // string, number, a malformed { value_from: "sk-..." }, or a reference with credential
    // material smuggled beside it — is treated as credential material (codex).
    const isReference = secretValueSpec.safeParse(value).success;
    if (value !== undefined && value !== null && !isReference) record[leaf] = "***";
  }
  // A registry profile's `host` can be URL-shaped with embedded userinfo/query tokens — run it
  // through the same sanitizer every other control-plane projection applies to hosts (codex).
  const registry = Object.hasOwn(runtime, "registry") ? runtime["registry"] : undefined;
  if (typeof registry === "object" && registry !== null && Object.hasOwn(registry, "host")) {
    const record = registry as Record<string, unknown>;
    if (typeof record["host"] === "string") record["host"] = sanitizeHost(record["host"]);
  }
  return runtime;
}

/** Redact a `prefix{*}` map slot (a custom-extension `config` block, #792) in a profile
 * runtime fragment: an exact `value_from` reference stays — it names a source, not a
 * credential — and every other entry value is masked whole, mirroring the flat-slot rule so
 * a fragment cannot smuggle a raw value (profiles never pass full spec validation, so the
 * TS spec-level `config` rejection does not protect this surface). */
function redactMapSlot(runtime: Record<string, unknown>, slotPath: string): void {
  const segments = slotPath
    .slice(0, -"{*}".length)
    .replace(/\.$/, "")
    .split(".")
    .slice(1); // strip the "runtime." root
  let node: unknown = runtime;
  for (const segment of segments.slice(0, -1)) {
    node =
      typeof node === "object" && node !== null && Object.hasOwn(node, segment)
        ? (node as Record<string, unknown>)[segment]
        : undefined;
  }
  const leaf = segments[segments.length - 1];
  if (typeof node !== "object" || node === null || leaf === undefined || !Object.hasOwn(node, leaf)) return;
  const parent = node as Record<string, unknown>;
  const target = parent[leaf];
  if (typeof target !== "object" || target === null || Array.isArray(target)) {
    // A raw fragment can put ANYTHING at the map slot (`config: sk-live`, a list); mask the
    // whole value rather than letting a non-map literal through (codex).
    if (target !== undefined && target !== null) parent[leaf] = "***";
    return;
  }
  const record = target as Record<string, unknown>;
  for (const [key, value] of Object.entries(record)) {
    if (value !== undefined && value !== null && !secretValueSpec.safeParse(value).success) {
      record[key] = "***";
    }
  }
}

/** Redact a `prefix[*].leaf` slot (the payload-codec keys, #188) in a profile runtime: a key
 * whose whole shape is a valid `payloadCodecKeySpec` is provenance (its `value_from` renders
 * as-is); anything else in that slot is masked, mirroring the flat-slot rule so a malformed
 * profile fragment cannot smuggle raw key material. */
function redactWildcardSlot(runtime: Record<string, unknown>, slotPath: string): void {
  const wildcardIndex = slotPath.indexOf("[*]");
  const prefix = slotPath.slice(0, wildcardIndex).replace(/\.$/, "");
  const leaf = slotPath.slice(wildcardIndex + "[*]".length).replace(/^\./, "");
  const segments = prefix.split(".").slice(1); // strip the "runtime." root
  let node: unknown = runtime;
  for (const segment of segments) {
    node =
      typeof node === "object" && node !== null && Object.hasOwn(node, segment)
        ? (node as Record<string, unknown>)[segment]
        : undefined;
  }
  if (!Array.isArray(node)) return;
  for (const item of node) {
    if (typeof item !== "object" || item === null || !Object.hasOwn(item, leaf)) continue;
    const record = item as Record<string, unknown>;
    const value = record[leaf];
    if (value !== undefined && value !== null && !payloadCodecKeySpec.safeParse(item).success) {
      record[leaf] = "***";
    }
  }
}

/** A profile list entry (Python `ProfileSummary`) — no nullable fields; always fully populated. */
export interface ApiProfileSummary {
  kind: string;
  id: string;
  name: string;
  content_hash: string;
  path: string;
}

/** A profile's full definition (Python `ProfileDefinition`). `runtime`/`used_by` default
 * present-but-empty — the detail route has no exclude_none (nothing None-typed to drop). */
export interface ApiProfileDefinition {
  kind: string;
  id: string;
  name: string;
  content_hash: string;
  path: string;
  runtime: Record<string, unknown>;
  used_by: string[];
}

/** The loaded spec for a DECLARED profile; declared-but-unsourced (or kind-mismatched — the fs
 * loader rejects that at load, but injected sources may not) is a 422 config error, mirroring
 * Python's `ProjectProfileError` → 422 mapping. */
function requireSourcedProfile(
  sources: ProjectBundleSources,
  kind: ProfileKind,
  profileId: string,
): ProjectProfileSpec {
  const ofKind = sources.profiles[kind];
  const spec = Object.hasOwn(ofKind, profileId) ? ofKind[profileId] : undefined;
  if (spec === undefined) {
    throw new ProjectControlPlaneError(`profile source not provided for declared profile: ${kind}/${profileId}`, 422);
  }
  if (spec.kind !== kind) {
    throw new ProjectControlPlaneError(
      `profile '${profileId}' is referenced under profiles.${kind} but declares kind: ${spec.kind}`,
      422,
    );
  }
  return spec;
}

/**
 * `GET /profiles` (Python `profile_definitions`): every declared profile as a summary — kinds in
 * fixed provider → registry → runtime order, ids sorted within each kind. Bare array, no envelope.
 */
export function buildProfileSummaries(
  project: TypefluxProjectSpec,
  sources: ProjectBundleSources,
): ApiProfileSummary[] {
  const declared = project.profiles;
  if (declared === undefined) return [];
  const summaries: ApiProfileSummary[] = [];
  for (const kind of PROFILE_KINDS) {
    for (const [profileId, path] of Object.entries(declared[kind]).sort(([a], [b]) => asc(a, b))) {
      const spec = requireSourcedProfile(sources, kind, profileId);
      summaries.push({
        kind,
        id: profileId,
        name: spec.name,
        content_hash: profileContentHash(spec),
        path,
      });
    }
  }
  return summaries;
}

/**
 * `GET /profiles/{kind}/{profile_id}` (Python `profile_definition`): the loaded content plus the
 * `used_by` reverse index — a workflow-level selection (`workflow.profiles[kind] == id`) adds the
 * bare workflow id; a per-environment `workflow_profiles` selection adds `"workflowId (envId)"`.
 * The caller (the control plane) has already 404'd an unknown kind/undeclared id.
 */
export function buildProfileDefinition(
  project: TypefluxProjectSpec,
  sources: ProjectBundleSources,
  kind: ProfileKind,
  profileId: string,
): ApiProfileDefinition {
  const spec = requireSourcedProfile(sources, kind, profileId);
  const used = new Set<string>();
  for (const workflow of project.workflows) {
    if (Object.hasOwn(workflow.profiles, kind) && workflow.profiles[kind] === profileId) {
      used.add(workflow.id);
    }
  }
  // Python loads EVERY declared environment here and fails on an unsourced one — the reverse
  // index must not silently understate usage because an environment file is missing (422).
  for (const environmentId of Object.keys(project.environments)) {
    const environment = Object.hasOwn(sources.environments, environmentId)
      ? sources.environments[environmentId]
      : undefined;
    if (environment === undefined) {
      throw new ProjectControlPlaneError(
        `environment source not provided for declared environment: ${environmentId}`,
        422,
      );
    }
    for (const [workflowId, workflowSpec] of Object.entries(environment.workflows)) {
      // Python's load_project_environment validates env workflow ids against the manifest and
      // raises (→ 422) on an unknown one — never fabricate a used_by entry for a ghost id.
      if (!project.workflows.some((workflow) => workflow.id === workflowId)) {
        throw new ProjectControlPlaneError(
          `environment '${environment.name}' references unknown workflow: ${workflowId}`,
          422,
        );
      }
      if (Object.hasOwn(workflowSpec.profiles, kind) && workflowSpec.profiles[kind] === profileId) {
        used.add(`${workflowId} (${environmentId})`);
      }
    }
  }
  return {
    kind,
    id: profileId,
    name: spec.name,
    content_hash: profileContentHash(spec),
    path: project.profiles?.[kind][profileId] ?? "",
    // Deep-copied so a caller mutating the response can't reach into the loaded spec; literal
    // credentials are redacted from the copy (value_from references render as-is).
    runtime: redactLiteralSecrets(structuredClone(spec.runtime) as Record<string, unknown>),
    used_by: [...used].sort(asc),
  };
}
