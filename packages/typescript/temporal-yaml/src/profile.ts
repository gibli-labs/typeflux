/**
 * Component PROFILE content loading (#570; Python `project/profiles.py` `ProjectProfileSpec` +
 * `load_project_profile`). A profile is a named, kind-scoped `runtime.*` fragment a workflow or
 * environment selects by id; kinds own DISJOINT runtime subtrees by construction, so no
 * cross-profile conflict is structurally possible. Profile fragments ride the same override
 * allowlist environment overrides use, at lower precedence: workflow YAML < profiles <
 * environment overrides.
 *
 * `profileContentHash` is BYTE-IDENTICAL to Python's `ProjectProfileSpec.content_hash` — the
 * shared cross-SDK `canonicalJson` (#391) over the same `{version, name, kind, runtime}`
 * payload — so the two control planes report the same hash for the same profile file (unlike
 * `spec_digest`, there is no per-interpreter identity here to justify divergence). Caveat
 * (SDK-wide, not profile-specific): the TS loaders parse YAML 1.2 while PyYAML parses 1.1,
 * so a 1.1-only scalar spelling (`on`/`yes` for booleans, sexagesimal ints) reads as a
 * different VALUE on each side and therefore hashes differently — spell scalars in the
 * JSON-compatible core forms (`true`/`false`, plain numbers) that both parse identically.
 */

import { createHash } from "node:crypto";

import { canonicalJson } from "@typeflux/temporal";
import { parseDocument } from "yaml";
import { z } from "zod";

import { assertSafeKeys, validateYamlOverrides, yamlOverridePaths } from "./overrides.js";

export const PROFILE_KINDS = ["provider", "registry", "runtime"] as const;
export type ProfileKind = (typeof PROFILE_KINDS)[number];

/** The `runtime.*` keys each kind may set (Python `_OWNED_RUNTIME_KEYS`) — disjoint by design. */
const OWNED_RUNTIME_KEYS: Record<ProfileKind, readonly string[]> = {
  provider: ["provider"],
  registry: ["registry"],
  runtime: ["temporal", "observability", "provider_retry", "provider_limits"],
};

const trimmedNonEmpty = (label: string) =>
  z.string().refine((value) => value.length > 0 && value.trim() === value, {
    message: `${label} must be non-empty and trimmed`,
  });

/** A component profile document (Python `ProjectProfileSpec`, minus the fs-only `profile_path`). */
export const projectProfileSpec = z
  .object({
    version: z.literal("1").default("1"),
    name: trimmedNonEmpty("profile name"),
    kind: z.enum(PROFILE_KINDS),
    runtime: z.record(z.string(), z.unknown()).default({}),
  })
  .strict();

export type ProjectProfileSpec = z.infer<typeof projectProfileSpec>;

/** Python `ProjectProfileSpec.content_hash`: sha256 over the canonical JSON of the identity
 * payload. Cross-SDK stable — the same YAML hashes identically on both control planes. */
export function profileContentHash(spec: ProjectProfileSpec): string {
  const payload = { version: spec.version, name: spec.name, kind: spec.kind, runtime: spec.runtime };
  return createHash("sha256").update(canonicalJson(payload), "utf-8").digest("hex");
}

/** An empty per-kind profile map — the no-profiles `sources.profiles` for hand-built bundles. */
export const emptyProfileSources = (): Record<ProfileKind, Record<string, ProjectProfileSpec>> => ({
  provider: {},
  registry: {},
  runtime: {},
});

/** Bytes/alias bounds for operator-trusted profile files (parity with the policy loader). */
const MAX_PROFILE_BYTES = 1024 * 1024;
const MAX_PROFILE_ALIASES = 1000;

export interface LoadProfileSpecOptions {
  /** Where the text came from, for error messages (a path or logical name). */
  sourceLabel?: string;
  /** The manifest section (`profiles.<kind>`) the file was referenced under; when given, a
   * profile self-declaring a different kind is rejected (Python `load_project_profile`). */
  declaredKind?: ProfileKind;
}

/**
 * Parse + validate one profile document (Python `load_project_profile`'s content half; path
 * resolution stays with the caller/fs loader). Enforces, beyond the schema: the declared-kind
 * match (when `declaredKind` is supplied) and the owned-subtree constraint — keys outside the
 * kind's subtree are rejected, and the fragment re-runs the override allowlist
 * (`validateYamlOverrides`) exactly like Python `_validate_profile_subtree`.
 */
export function loadProfileSpec(text: string, options: LoadProfileSpecOptions = {}): ProjectProfileSpec {
  const sourceLabel = options.sourceLabel ?? "profile";
  if (Buffer.byteLength(text, "utf-8") > MAX_PROFILE_BYTES) {
    throw new Error(`profile document exceeds the ${MAX_PROFILE_BYTES} byte limit (${sourceLabel})`);
  }
  const doc = parseDocument(text, { uniqueKeys: true, merge: true });
  const fatal = [...doc.errors, ...doc.warnings.filter((warning) => warning.code === "DUPLICATE_KEY")];
  if (fatal.length > 0) {
    throw new Error(`invalid profile YAML (${sourceLabel}): ${fatal[0]?.message ?? "parse error"}`);
  }
  const raw: unknown = doc.toJS({ maxAliasCount: MAX_PROFILE_ALIASES });
  if (raw === null || raw === undefined) {
    throw new Error(`empty component profile: ${sourceLabel}`);
  }
  if (typeof raw !== "object" || Array.isArray(raw)) {
    throw new TypeError(`component profile must be a YAML mapping (${sourceLabel})`);
  }
  assertSafeKeys(raw, sourceLabel);
  const result = projectProfileSpec.safeParse(raw);
  if (!result.success) {
    throw new Error(`invalid component profile (${sourceLabel}):\n${z.prettifyError(result.error)}`);
  }
  const profile = result.data;
  if (options.declaredKind !== undefined && profile.kind !== options.declaredKind) {
    throw new Error(
      `profile ${sourceLabel} is referenced under profiles.${options.declaredKind} ` +
        `but declares kind: ${profile.kind}`,
    );
  }
  validateProfileSubtree(profile);
  return profile;
}

/** Python `_validate_profile_subtree`: owned-keys check + the shared override allowlist. */
function validateProfileSubtree(profile: ProjectProfileSpec): void {
  const owned = OWNED_RUNTIME_KEYS[profile.kind];
  const outside = Object.keys(profile.runtime)
    .filter((key) => !owned.includes(key))
    .sort();
  if (outside.length > 0) {
    throw new Error(
      `profile '${profile.name}' (kind: ${profile.kind}) sets runtime keys outside ` +
        `its owned subtree: ${outside.join(", ")}; owned keys: ${[...owned].sort().join(", ")}`,
    );
  }
  if (Object.keys(profile.runtime).length > 0) {
    validateYamlOverrides({ runtime: profile.runtime }, `profiles.${profile.kind}.${profile.name}`);
  }
}

/**
 * Raised when a component profile cannot be resolved or applied safely (Python
 * `ProjectProfileError`). The control plane maps it to a 422 with the `ProjectProfileError`
 * discriminant, so an undeclared-profile selection fails the same way on both editions.
 */
export class ProjectProfileError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ProjectProfileError";
  }
}

/**
 * The `runtime.*` override fragment a profile contributes (Python `ProjectProfileSpec.overrides`):
 * `{ runtime: {...owned subtree...} }`, or `{}` when the profile sets nothing.
 */
export function profileOverrides(spec: ProjectProfileSpec): Record<string, unknown> {
  return Object.keys(spec.runtime).length > 0 ? { runtime: { ...spec.runtime } } : {};
}

/** Safe provenance for one applied component profile (Python `AppliedComponentProfile`). */
export interface AppliedComponentProfile {
  kind: ProfileKind;
  id: string;
  name: string;
  content_hash: string;
  source_path: string;
  override_paths: string[];
}

/** A profile selection: at most one profile id per kind (Python `dict[str, str]`). */
export type ProfileSelection = Readonly<Record<string, string>>;

/**
 * Python `validate_profile_selection`: every selected KIND must be a known profile kind.
 * A typo'd kind fails closed with `ProjectProfileError` (→ 422) rather than being silently
 * dropped. Runs BEFORE id resolution, per kind, so the message names the offending context.
 */
export function validateProfileSelection(selection: ProfileSelection, context: string): void {
  for (const kind of Object.keys(selection)) {
    if (!(PROFILE_KINDS as readonly string[]).includes(kind)) {
      throw new ProjectProfileError(
        `${context} selects unknown profile kind '${kind}'; valid kinds: ${PROFILE_KINDS.join(", ")}`,
      );
    }
  }
}

/**
 * A profile id + its loaded spec + the source path (for provenance) it resolved from — one entry
 * per selected kind. Mirrors the tuple Python `resolve_selected_profiles` returns, with the source
 * path threaded so {@link profileOverridesAndProvenance} can record it (the loaded spec drops it).
 */
export interface SelectedProfile {
  id: string;
  spec: ProjectProfileSpec;
  sourcePath: string;
}

/**
 * The loaded profiles + declared reference paths a composition draws from — the injected
 * counterpart of Python's `project.profiles` map + `_resolve_project_path`. `specs` is the
 * `sources.profiles` channel; `paths` is the manifest's per-id reference (for `source_path`).
 */
export interface ProfileSourceIndex {
  specs: Readonly<Record<ProfileKind, Readonly<Record<string, ProjectProfileSpec>>>>;
  paths: Readonly<Record<ProfileKind, Readonly<Record<string, string>>>>;
}

/**
 * Resolve the effective profile per kind from a workflow + environment selection (Python
 * `resolve_selected_profiles`). The environment selection REPLACES the workflow selection per
 * kind (whole-reference replacement — no partial mixing). An id the sources don't carry is a
 * `ProjectProfileError` (Python's `load_project_profile` raises the same "unknown project {kind}
 * profile: {id}"). Returns the selected profiles in fixed `PROFILE_KINDS` order.
 */
export function resolveSelectedProfiles(
  index: ProfileSourceIndex,
  workflowSelection: ProfileSelection,
  environmentSelection: ProfileSelection,
): SelectedProfile[] {
  const effective: Record<string, string> = { ...workflowSelection, ...environmentSelection };
  const selected: SelectedProfile[] = [];
  for (const kind of PROFILE_KINDS) {
    const profileId = effective[kind];
    if (profileId === undefined) continue;
    // Resolution goes through the MANIFEST DECLARATION first (Python `load_project_profile`):
    // an injected source for an undeclared id must never silently apply (codex) — the manifest
    // is the authority on what a selection may reference.
    const declared = index.paths[kind] ?? {};
    if (!Object.hasOwn(declared, profileId)) {
      throw new ProjectProfileError(`unknown project ${kind} profile: ${profileId}`);
    }
    const specsForKind = index.specs[kind] ?? {};
    if (!Object.hasOwn(specsForKind, profileId)) {
      // The injection analogue of Python's file-load failure: declared, but no source supplied.
      throw new ProjectProfileError(
        `${kind} profile '${profileId}' is declared but no source was provided`,
      );
    }
    const spec = specsForKind[profileId] as ProjectProfileSpec;
    // A source whose self-declared kind mismatches its section must not apply under the wrong
    // owned subtree (Python raises at load; injected sources bypass the fs loader).
    if (spec.kind !== kind) {
      throw new ProjectProfileError(
        `profile '${profileId}' is referenced under profiles.${kind} but declares kind: ${spec.kind}`,
      );
    }
    validateProfileSubtree(spec);
    selected.push({
      id: profileId,
      spec,
      sourcePath: declared[profileId] ?? "",
    });
  }
  return selected;
}

/**
 * Merge selected profiles into one override payload + provenance (Python
 * `profile_overrides_and_provenance`). Kinds own DISJOINT `runtime.*` subtrees, so this merge can
 * never conflict — it assembles the per-kind fragments into one `runtime` mapping. Provenance is
 * one {@link AppliedComponentProfile} per selected profile, in the input (kind) order.
 */
export function profileOverridesAndProvenance(
  selected: readonly SelectedProfile[],
): { overrides: Record<string, unknown>; provenance: AppliedComponentProfile[] } {
  const overrides: Record<string, unknown> = {};
  const provenance: AppliedComponentProfile[] = [];
  for (const { id, spec, sourcePath } of selected) {
    const fragment = profileOverrides(spec);
    if (Object.keys(fragment).length > 0) {
      const runtime = (overrides.runtime ??= {}) as Record<string, unknown>;
      Object.assign(runtime, fragment.runtime as Record<string, unknown>);
    }
    provenance.push({
      kind: spec.kind,
      id,
      name: spec.name,
      content_hash: profileContentHash(spec),
      source_path: sourcePath,
      override_paths: yamlOverridePaths(fragment),
    });
  }
  return { overrides, provenance };
}

/**
 * The composed profile overrides + provenance for a workflow/environment selection (Python
 * `_resolved_profile_overrides`): validate each selection's kinds (workflow first, then
 * environment — a typo'd kind is a `ProjectProfileError`), then resolve + merge. When neither side
 * selects anything, returns empty overrides + no provenance without touching the sources.
 */
export function composeProfileOverrides(
  index: ProfileSourceIndex,
  options: {
    workflowSelection: ProfileSelection;
    environmentSelection: ProfileSelection;
    workflowContext: string;
    environmentContext: string;
  },
): { overrides: Record<string, unknown>; provenance: AppliedComponentProfile[] } {
  const { workflowSelection, environmentSelection, workflowContext, environmentContext } = options;
  validateProfileSelection(workflowSelection, workflowContext);
  validateProfileSelection(environmentSelection, environmentContext);
  if (Object.keys(workflowSelection).length === 0 && Object.keys(environmentSelection).length === 0) {
    return { overrides: {}, provenance: [] };
  }
  return profileOverridesAndProvenance(
    resolveSelectedProfiles(index, workflowSelection, environmentSelection),
  );
}
