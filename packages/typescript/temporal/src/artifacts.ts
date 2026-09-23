/**
 * The artifact contract (parity epic #481 PR1; Python `core/artifacts.py`). Pure model +
 * validation: sources/refs (what the caller supplies), inputs/attachments/policy (what an
 * activity declares), and the RESOLVED shapes providers consume (`ResolvedArtifact` carries a
 * `local_path`, not bytes — providers read at request time, parity with Python). Field names stay
 * snake_case: refs arrive on the wire (activity input / YAML) and summaries feed cross-SDK
 * observability, so the shapes must match Python byte-for-byte (the `CacheRecord` precedent).
 *
 * The I/O half — resolving inputs into these shapes (dot-path extraction, hashing, media-type
 * guessing, policy enforcement) — is the worker's job (epic PR3), not this module's.
 */

import { z } from "zod";

import type { ChatMessage } from "./manifest-hashing.js";
import type { ContentPart } from "./content-parts.js";
import { ProviderConfigError } from "./execute.js";

export const ARTIFACT_KINDS = [
  "document",
  "image",
  "audio",
  "video",
  "data",
  "archive",
  "provider_file",
  "external_uri",
  "other",
] as const;
export type ArtifactKind = (typeof ARTIFACT_KINDS)[number];

export const ARTIFACT_SOURCE_KINDS = ["local_path", "url", "object_uri", "provider_file"] as const;
export type ArtifactSourceKind = (typeof ARTIFACT_SOURCE_KINDS)[number];

export type ArtifactAttachmentRole = "system" | "user" | "assistant";

/** Per-source-kind required fields and the fields each kind must NOT set (Python parity). */
const SOURCE_FIELDS: Record<ArtifactSourceKind, { required: readonly string[]; forbidden: readonly string[] }> = {
  local_path: { required: ["path"], forbidden: ["url", "uri", "provider", "file_id"] },
  url: { required: ["url"], forbidden: ["path", "uri", "provider", "file_id"] },
  object_uri: { required: ["uri"], forbidden: ["path", "url", "provider", "file_id"] },
  provider_file: { required: ["provider", "file_id"], forbidden: ["path", "url", "uri"] },
};

/**
 * Where an artifact's bytes live. Exactly the fields for its `type` may be set —
 * mutually-exclusive validation mirrors Python's `ArtifactSource` model validator.
 */
export const artifactSourceSchema = z
  .object({
    type: z.enum(ARTIFACT_SOURCE_KINDS),
    path: z.string().nullish(),
    url: z.string().nullish(),
    uri: z.string().nullish(),
    provider: z.string().nullish(),
    file_id: z.string().nullish(),
  })
  .strict()
  .superRefine((source, ctx) => {
    const fields = SOURCE_FIELDS[source.type];
    const missing = fields.required.filter((f) => {
      const v = (source as Record<string, unknown>)[f];
      return v === undefined || v === null || v === "";
    });
    if (missing.length > 0) {
      ctx.addIssue({
        code: "custom",
        message: `artifact source '${source.type}' requires ${[...missing].sort().join(", ")}`,
      });
    }
    const extras = fields.forbidden.filter((f) => {
      const v = (source as Record<string, unknown>)[f];
      return v !== undefined && v !== null;
    });
    if (extras.length > 0) {
      ctx.addIssue({
        code: "custom",
        message: `artifact source '${source.type}' cannot set ${[...extras].sort().join(", ")}`,
      });
    }
  });
export type ArtifactSource = z.infer<typeof artifactSourceSchema>;

/** Coerce a caller value into an {@link ArtifactSource}: a bare string is a `local_path`. */
export function artifactSourceFromValue(value: unknown): ArtifactSource {
  if (typeof value === "string") {
    return artifactSourceSchema.parse({ type: "local_path", path: value });
  }
  return artifactSourceSchema.parse(value);
}

const trimmedNonEmpty = (label: string) =>
  z
    .string()
    .refine((v) => v.length > 0 && v.trim() === v, { message: `${label} must be non-empty and trimmed` });

/**
 * A caller-supplied reference to one artifact (typically an element of the activity input at an
 * `ArtifactInput.from_path`). `source` accepts a bare string (→ `local_path`) or a source object.
 */
export const artifactRefSchema = z
  .object({
    source: z.preprocess(
      (v) => (typeof v === "string" ? { type: "local_path", path: v } : v),
      artifactSourceSchema,
    ),
    kind: z.enum(ARTIFACT_KINDS).nullish(),
    media_type: z.string().nullish(),
    role: trimmedNonEmpty("artifact role").nullish(),
    sha256: z
      .string()
      .regex(/^[0-9a-f]{64}$/, "artifact sha256 must be lowercase hex")
      .nullish(),
    size_bytes: z.number().int().min(0, "artifact size_bytes must be >= 0").nullish(),
    display_name: trimmedNonEmpty("artifact display_name").nullish(),
    metadata: z.record(z.string(), z.union([z.string(), z.number(), z.boolean()])).default({}),
  })
  .strict();
export type ArtifactRef = z.infer<typeof artifactRefSchema>;

/** How an artifact group is presented to the model: an appended message with an optional preamble. */
export interface ArtifactAttachment {
  readonly role: ArtifactAttachmentRole;
  readonly text?: string;
}

/** Validate + freeze an {@link ArtifactAttachment} (Python `ArtifactAttachment.__post_init__`). */
export function artifactAttachment(fields: { role?: ArtifactAttachmentRole; text?: string }): ArtifactAttachment {
  const role = fields.role ?? "user";
  if (role !== "system" && role !== "user" && role !== "assistant") {
    throw new Error("artifact attachment role must be system, user, or assistant");
  }
  if (fields.text !== undefined && fields.text === "") {
    throw new Error("artifact attachment text must be non-empty");
  }
  return Object.freeze({ role, ...(fields.text !== undefined ? { text: fields.text } : {}) });
}

/**
 * An activity's declared artifact group: where in the input the refs live (`from_path`, a
 * dot-path that must start with `input.`), what is allowed (kind / media types / counts / bytes),
 * and how the group attaches to the conversation. `cache_role: "reference"` marks the group as
 * part of the stable session-cached prefix (#478) and requires `attach` — without an attach rule
 * a reference artifact would be presented nowhere and silently vanish.
 */
export interface ArtifactInput {
  readonly name: string;
  readonly from_path: string;
  readonly required: boolean;
  readonly kind?: ArtifactKind;
  readonly media_types: readonly string[];
  readonly max_count?: number;
  readonly max_bytes?: number;
  readonly attach?: ArtifactAttachment;
  readonly cache_role?: "reference";
}

/** Validate + freeze an {@link ArtifactInput} (Python `ArtifactInput.__post_init__`). */
export function artifactInput(fields: {
  name: string;
  from_path: string;
  required?: boolean;
  kind?: ArtifactKind;
  media_types?: readonly string[];
  max_count?: number;
  max_bytes?: number;
  attach?: ArtifactAttachment;
  cache_role?: "reference";
}): ArtifactInput {
  if (fields.name.length === 0 || fields.name.trim() !== fields.name) {
    throw new Error("artifact input name must be non-empty and trimmed");
  }
  if (!fields.from_path.startsWith("input.")) {
    throw new Error("artifact input from_path must start with 'input.'");
  }
  const mediaTypes = fields.media_types ?? [];
  for (const mediaType of mediaTypes) {
    if (mediaType.length === 0 || mediaType.trim() !== mediaType) {
      throw new Error("artifact input media_types must be non-empty and trimmed");
    }
  }
  if (fields.max_count !== undefined && fields.max_count < 1) {
    throw new Error("artifact input max_count must be >= 1");
  }
  if (fields.max_bytes !== undefined && fields.max_bytes < 0) {
    throw new Error("artifact input max_bytes must be >= 0");
  }
  if (fields.cache_role === "reference" && fields.attach === undefined) {
    throw new Error(
      `artifact input ${JSON.stringify(fields.name)} sets cache: reference but has no ` +
        "attach rule; a reference artifact must declare how it attaches",
    );
  }
  // An optional, textless reference artifact emits NO attach message for items where it
  // resolves empty, so the conversation shape (and the prefix-cache breakpoint index derived
  // from the static reference count, #362) would vary across items — breaking the
  // byte-identical-prefix contract. A sometimes-absent message is invalid, not merely risky;
  // reject it at the source (fail-closed, #362 review; Python parity).
  if (
    fields.cache_role === "reference" &&
    fields.required === false &&
    fields.attach !== undefined &&
    fields.attach.text === undefined
  ) {
    throw new Error(
      `artifact input ${JSON.stringify(fields.name)} sets cache: reference with ` +
        "required: false and no attach text; a cache: reference artifact must always " +
        "produce its attach message so the cached prefix is stable across items — " +
        "make it required or give attach a static text",
    );
  }
  // Re-validate the attachment through its factory: ArtifactAttachment is a structural type, so a
  // plain literal (e.g. from parsed YAML in epic PR4) with `text: ""` or an out-of-union role
  // would otherwise be frozen in unvalidated — Python makes an invalid attachment unrepresentable.
  const attach = fields.attach !== undefined ? artifactAttachment(fields.attach) : undefined;
  return Object.freeze({
    name: fields.name,
    from_path: fields.from_path,
    required: fields.required ?? true,
    ...(fields.kind !== undefined ? { kind: fields.kind } : {}),
    media_types: Object.freeze([...mediaTypes]),
    ...(fields.max_count !== undefined ? { max_count: fields.max_count } : {}),
    ...(fields.max_bytes !== undefined ? { max_bytes: fields.max_bytes } : {}),
    ...(attach !== undefined ? { attach } : {}),
    ...(fields.cache_role !== undefined ? { cache_role: fields.cache_role } : {}),
  });
}

/**
 * The runtime's artifact admission policy (Python `ArtifactPolicy`; wired from
 * `runtime.artifacts` in epic PR4). Empty `local_roots` means no local artifacts are allowed;
 * empty `allowed_media_types` means all are. Root RESOLUTION (absolute paths, boundary checks)
 * is the worker resolver's job — this pure module only validates shape.
 */
export interface ArtifactPolicy {
  readonly local_roots: readonly string[];
  readonly allowed_source_kinds: readonly ArtifactSourceKind[];
  readonly allowed_media_types: readonly string[];
  readonly max_bytes?: number;
}

/** Validate + freeze an {@link ArtifactPolicy} (Python `ArtifactPolicy.__post_init__`). */
export function artifactPolicy(fields: {
  local_roots?: readonly string[];
  allowed_source_kinds?: readonly ArtifactSourceKind[];
  allowed_media_types?: readonly string[];
  max_bytes?: number;
}): ArtifactPolicy {
  const sourceKinds = fields.allowed_source_kinds ?? ["local_path"];
  for (const kind of sourceKinds) {
    if (!ARTIFACT_SOURCE_KINDS.includes(kind)) {
      throw new Error(`unsupported artifact source kind: ${kind}`);
    }
  }
  const mediaTypes = fields.allowed_media_types ?? [];
  for (const mediaType of mediaTypes) {
    if (mediaType.length === 0 || mediaType.trim() !== mediaType) {
      throw new Error("artifact policy media types must be non-empty and trimmed");
    }
  }
  if (fields.max_bytes !== undefined && fields.max_bytes < 0) {
    throw new Error("artifact policy max_bytes must be >= 0");
  }
  return Object.freeze({
    local_roots: Object.freeze([...(fields.local_roots ?? [])]),
    allowed_source_kinds: Object.freeze([...sourceKinds]),
    allowed_media_types: Object.freeze([...mediaTypes]),
    ...(fields.max_bytes !== undefined ? { max_bytes: fields.max_bytes } : {}),
  });
}

/**
 * One artifact after resolution — validated against its input + policy, hashed and sized, with
 * a `local_path` for `local_path` sources (providers read the bytes at request time).
 */
export interface ResolvedArtifact {
  readonly group: string;
  readonly index: number;
  readonly ref: ArtifactRef;
  readonly source_kind: ArtifactSourceKind;
  readonly kind?: ArtifactKind;
  readonly media_type?: string;
  readonly role?: string;
  readonly sha256?: string;
  readonly size_bytes?: number;
  readonly local_path?: string;
}

export interface ResolvedArtifactGroup {
  readonly name: string;
  readonly artifacts: readonly ResolvedArtifact[];
}

/** The safe (non-sensitive) summary of a resolved artifact — Python `safe_summary()` key-for-key. */
export function artifactSafeSummary(artifact: ResolvedArtifact): Record<string, unknown> {
  return dropUndefined({
    group: artifact.group,
    index: artifact.index,
    source_kind: artifact.source_kind,
    kind: artifact.kind,
    media_type: artifact.media_type,
    role: artifact.role,
    sha256: artifact.sha256,
    size_bytes: artifact.size_bytes,
  });
}

/** The safe summaries of a group list — Python `artifact_groups_summary()`. */
export function artifactGroupsSummary(groups: readonly ResolvedArtifactGroup[]): Record<string, unknown>[] {
  return groups.map((group) => ({
    name: group.name,
    count: group.artifacts.length,
    artifacts: group.artifacts.map(artifactSafeSummary),
  }));
}

function artifactCacheIdentity(artifact: ResolvedArtifact): Record<string, unknown> {
  const identity = artifactSafeSummary(artifact);
  if (artifact.sha256 === undefined) {
    // No content hash pins the bytes (URL/object_uri/provider_file, or a hand-built
    // local ref): fold the source itself, or swapping the location under the same
    // group/index would serve a stale cached output (#504).
    const source: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(artifact.ref.source)) {
      // `!= null` mirrors Python's model_dump(exclude_none=True): the zod source
      // schema is nullish, so absent fields may be either null or undefined.
      if (value != null) {
        source[key] = value;
      }
    }
    identity["source"] = source;
  }
  return identity;
}

/**
 * The cross-run cache-key fold (#504; Python `artifact_groups_cache_identity`): the safe
 * summary, extended with the artifact SOURCE whenever no `sha256` pins the bytes, minus
 * empty groups (an optional input that resolved to nothing must not change the key).
 *
 * Deliberately distinct from {@link artifactGroupsSummary}: the summary feeds
 * manifests/observability and must stay redacted (no paths/URLs/ids), while this shape
 * is only ever hashed into a cache key.
 */
export function artifactGroupsCacheIdentity(groups: readonly ResolvedArtifactGroup[]): Record<string, unknown>[] {
  return groups
    .filter((group) => group.artifacts.length > 0)
    .map((group) => ({
      name: group.name,
      count: group.artifacts.length,
      artifacts: group.artifacts.map(artifactCacheIdentity),
    }));
}

/**
 * All artifacts of a named group — a provider-side lookup, so a miss is a typed
 * {@link ProviderConfigError} (Python `artifacts_for_group`).
 */
export function artifactsForGroup(
  groups: readonly ResolvedArtifactGroup[],
  name: string,
  provider: string,
): readonly ResolvedArtifact[] {
  for (const group of groups) {
    if (group.name === name) {
      return group.artifacts;
    }
  }
  throw new ProviderConfigError(`unknown artifact group: ${name}`, { provider });
}

/**
 * Resolve an artifact reference: `"group"` for a single-artifact group, `"group[i]"` for an
 * indexed element (Python `artifact_for_name` — same errors, same `group[index]` guidance).
 */
export function artifactForName(
  groups: readonly ResolvedArtifactGroup[],
  name: string,
  provider: string,
): ResolvedArtifact {
  if (name.includes("[") && name.endsWith("]")) {
    const open = name.indexOf("[");
    const groupName = name.slice(0, open);
    // Python-parity index parsing (int(text)): optional surrounding whitespace + sign + decimal
    // digits ONLY — "1.0"/"1e0"/"0x1" are invalid references, not indices (a typo must throw,
    // not silently resolve some artifact).
    const indexText = name.slice(open + 1, -1).trim();
    if (!/^[+-]?\d+$/.test(indexText)) {
      throw new ProviderConfigError(`invalid artifact reference: ${name}`, { provider });
    }
    const index = Number(indexText);
    if (index < 0) {
      throw new ProviderConfigError(`invalid artifact reference: ${name}`, { provider });
    }
    const artifacts = artifactsForGroup(groups, groupName, provider);
    const artifact = artifacts[index];
    if (artifact === undefined) {
      throw new ProviderConfigError(`unknown artifact reference: ${name}`, { provider });
    }
    return artifact;
  }
  const artifacts = artifactsForGroup(groups, name, provider);
  if (artifacts.length !== 1) {
    throw new ProviderConfigError(
      `artifact reference ${JSON.stringify(name)} resolves to ${artifacts.length} artifacts; ` +
        "use group[index] syntax",
      { provider },
    );
  }
  return artifacts[0] as ResolvedArtifact;
}

/**
 * Append each attaching group's message to the conversation (Python
 * `attach_artifact_messages`): the optional preamble text plus an `artifact_group` content part.
 * A group with no resolved artifacts and no preamble contributes nothing.
 */
export function attachArtifactMessages(
  messages: readonly ChatMessage[],
  artifactInputs: readonly ArtifactInput[],
  artifactGroups: readonly ResolvedArtifactGroup[] = [],
): ChatMessage[] {
  const attached = [...messages];
  for (const input of artifactInputs) {
    if (input.attach === undefined) {
      continue;
    }
    const hasArtifacts = artifactGroups.some(
      (group) => group.name === input.name && group.artifacts.length > 0,
    );
    if (!hasArtifacts && input.attach.text === undefined) {
      continue;
    }
    const parts: ContentPart[] = [];
    if (input.attach.text !== undefined) {
      parts.push({ type: "text", text: input.attach.text });
    }
    if (hasArtifacts) {
      parts.push({ type: "artifact_group", group: input.name });
    }
    attached.push({ role: input.attach.role, content: parts });
  }
  return attached;
}

function dropUndefined(obj: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(obj)) {
    if (value !== undefined) {
      out[key] = value;
    }
  }
  return out;
}
