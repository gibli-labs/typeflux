/**
 * The artifact resolution pipeline (parity #481 PR3; Python `core/artifacts.py`
 * `resolve_artifact_inputs` + helpers). Worker-side because it does I/O: local files are
 * existence-checked against the policy's `local_roots`, sha256-hashed and sized in 1MB chunks.
 * The pure contract (`ArtifactInput`/`ArtifactPolicy`/`ResolvedArtifact`) lives in the core;
 * `artifactInputResolver(policy)` adapts this pipeline to `executeActivity`'s injected
 * `artifactResolver` option.
 */

import { createHash } from "node:crypto";
import { createReadStream } from "node:fs";
import { realpath, stat } from "node:fs/promises";
import { homedir } from "node:os";
import { isAbsolute, join, relative, resolve, sep } from "node:path";

import {
  type ArtifactInput,
  type ArtifactKind,
  type ArtifactPolicy,
  artifactPolicy,
  type ArtifactRef,
  artifactRefSchema,
  type ArtifactSource,
  type ResolvedArtifact,
  type ResolvedArtifactGroup,
} from "@typeflux/temporal";

/**
 * Resolve an activity's declared artifact inputs against its input value: extract the refs at
 * each `from_path`, coerce them, enforce the input's and policy's constraints, hash local files,
 * and produce the frozen groups providers consume. Python `resolve_artifact_inputs`, same errors.
 */
export async function resolveArtifactInputs(
  inputValue: unknown,
  artifactInputs: readonly ArtifactInput[],
  policy?: ArtifactPolicy,
): Promise<ResolvedArtifactGroup[]> {
  if (artifactInputs.length === 0) {
    return [];
  }
  const resolvedPolicy = policy ?? artifactPolicy({});
  const groups: ResolvedArtifactGroup[] = [];
  for (const input of artifactInputs) {
    const rawValue = valueAtPath(inputValue, input.from_path);
    const refs = coerceArtifactRefs(rawValue, input.required);
    if (input.max_count !== undefined && refs.length > input.max_count) {
      throw new Error(
        `artifact input ${JSON.stringify(input.name)} allows at most ${input.max_count} artifact(s)`,
      );
    }
    const artifacts: ResolvedArtifact[] = [];
    for (const [index, ref] of refs.entries()) {
      artifacts.push(await resolveArtifact(input, ref, index, resolvedPolicy));
    }
    groups.push({ name: input.name, artifacts });
  }
  return groups;
}

/**
 * The `executeActivity` `artifactResolver` injection bound to a policy — wire it into the
 * activity's execution options so a descriptor's declared `artifacts` resolve per call:
 *
 *     executeActivity(descriptor, input, { ..., artifactResolver: artifactInputResolver(policy) })
 */
export function artifactInputResolver(
  policy?: ArtifactPolicy,
): (inputValue: unknown, artifactInputs: readonly ArtifactInput[]) => Promise<ResolvedArtifactGroup[]> {
  return (inputValue, artifactInputs) => resolveArtifactInputs(inputValue, artifactInputs, policy);
}

async function resolveArtifact(
  input: ArtifactInput,
  ref: ArtifactRef,
  index: number,
  policy: ArtifactPolicy,
): Promise<ResolvedArtifact> {
  const source = ref.source;
  if (!policy.allowed_source_kinds.includes(source.type)) {
    throw new Error(`artifact input ${JSON.stringify(input.name)} source ${JSON.stringify(source.type)} is not allowed`);
  }
  // Python truthiness (`ref.media_type or guess`): "" counts as missing.
  const mediaType = (ref.media_type != null && ref.media_type !== "" ? ref.media_type : undefined) ?? guessMediaType(source);
  if (ref.kind != null && input.kind !== undefined && ref.kind !== input.kind) {
    throw new Error(
      `artifact input ${JSON.stringify(input.name)} expected kind ${JSON.stringify(input.kind)}, ` +
        `got ${JSON.stringify(ref.kind)}`,
    );
  }
  const kind: ArtifactKind = ref.kind ?? input.kind ?? guessKind(mediaType, source);
  validateMediaType(input, mediaType, policy);
  const localPath = source.type === "local_path" ? await resolveLocalPath(source, policy) : undefined;
  let sha256 = ref.sha256 ?? undefined;
  let sizeBytes = ref.size_bytes ?? undefined;
  if (localPath !== undefined) {
    const [digest, size] = await hashFile(localPath);
    if (sha256 !== undefined && sha256 !== digest) {
      throw new Error(`artifact input ${JSON.stringify(input.name)} hash mismatch for local artifact`);
    }
    sha256 = digest;
    sizeBytes = size;
  }
  const maxBytes = input.max_bytes ?? policy.max_bytes;
  if (maxBytes !== undefined && sizeBytes !== undefined && sizeBytes > maxBytes) {
    throw new Error(
      `artifact input ${JSON.stringify(input.name)} exceeds max_bytes (${sizeBytes} > ${maxBytes})`,
    );
  }
  return {
    group: input.name,
    index,
    ref,
    source_kind: source.type,
    kind,
    ...(mediaType !== undefined ? { media_type: mediaType } : {}),
    role: ref.role ?? input.name,
    ...(sha256 !== undefined ? { sha256 } : {}),
    ...(sizeBytes !== undefined ? { size_bytes: sizeBytes } : {}),
    ...(localPath !== undefined ? { local_path: localPath } : {}),
  };
}

/**
 * Locate a local artifact: an absolute path stands alone; a relative one is tried against each
 * root in order. The winner must be an existing FILE inside one of the configured roots (the
 * boundary check runs on the resolved path, so `..` segments cannot escape). Python
 * `_resolve_local_path`, same errors.
 */
async function resolveLocalPath(source: ArtifactSource, policy: ArtifactPolicy): Promise<string> {
  if (policy.local_roots.length === 0) {
    throw new Error("local artifact sources require at least one configured artifact local_root");
  }
  const raw = expandUser(source.path ?? "");
  // realpath (not the lexical path.resolve) mirrors Python's Path.resolve(), which follows
  // symlinks: a symlink INSIDE a root pointing outside must fail the boundary check (a lexical
  // check is a sandbox escape), and a root that is itself a symlink (macOS /tmp -> /private/tmp)
  // must still admit files addressed by their real absolute path.
  const roots = await Promise.all(policy.local_roots.map((root) => realpathOrLexical(expandUser(root))));
  const candidates = isAbsolute(raw) ? [resolve(raw)] : roots.map((root) => resolve(join(root, raw)));
  let path: string | undefined;
  for (const candidate of candidates) {
    if (await isFile(candidate)) {
      path = await realpath(candidate);
      break;
    }
  }
  if (path === undefined) {
    throw new Error(`artifact local_path does not exist or is not a file: ${source.path}`);
  }
  for (const root of roots) {
    if (path === root || isInside(root, path)) {
      return path;
    }
  }
  throw new Error("artifact local_path is outside configured artifact local_roots");
}

/** A root's real path when it exists (symlinks followed), else its lexical resolution. */
async function realpathOrLexical(path: string): Promise<string> {
  try {
    return await realpath(path);
  } catch {
    return resolve(path);
  }
}

function expandUser(path: string): string {
  return path === "~" || path.startsWith(`~${sep}`) || path.startsWith("~/")
    ? join(homedir(), path.slice(1))
    : path;
}

function isInside(root: string, path: string): boolean {
  const rel = relative(root, path);
  return rel !== "" && !rel.startsWith("..") && !isAbsolute(rel);
}

async function isFile(path: string): Promise<boolean> {
  try {
    return (await stat(path)).isFile();
  } catch {
    return false;
  }
}

function validateMediaType(input: ArtifactInput, mediaType: string | undefined, policy: ArtifactPolicy): void {
  const allowed = input.media_types.length > 0 ? input.media_types : policy.allowed_media_types;
  if (allowed.length === 0) {
    return;
  }
  if (mediaType === undefined) {
    throw new Error(
      `artifact input ${JSON.stringify(input.name)} has unknown media type ` +
        "and cannot be checked against allowed media types",
    );
  }
  for (const pattern of allowed) {
    if (mediaTypeMatches(mediaType, pattern)) {
      return;
    }
  }
  throw new Error(`artifact input ${JSON.stringify(input.name)} media type ${JSON.stringify(mediaType)} is not allowed`);
}

function mediaTypeMatches(mediaType: string, pattern: string): boolean {
  if (pattern === mediaType) {
    return true;
  }
  if (pattern.endsWith("/*")) {
    return mediaType.startsWith(pattern.slice(0, -1));
  }
  return false;
}

/** sha256 + byte count of a file, streamed in 1MB chunks (Python `_hash_file`). */
async function hashFile(path: string): Promise<[string, number]> {
  const digest = createHash("sha256");
  let size = 0;
  const stream = createReadStream(path, { highWaterMark: 1024 * 1024 });
  for await (const chunk of stream) {
    const buffer = chunk as Buffer;
    size += buffer.length;
    digest.update(buffer);
  }
  return [digest.digest("hex"), size];
}

/**
 * Extension → media type for the common artifact formats (Python leans on the stdlib
 * `mimetypes` table; Node has none, so this covers the SDK's supported formats — an unknown
 * extension resolves to undefined exactly like Python's miss).
 */
const MEDIA_TYPES_BY_EXTENSION: Record<string, string> = {
  png: "image/png",
  jpg: "image/jpeg",
  jpeg: "image/jpeg",
  gif: "image/gif",
  webp: "image/webp",
  pdf: "application/pdf",
  txt: "text/plain",
  md: "text/markdown",
  csv: "text/csv",
  json: "application/json",
  xml: "application/xml",
  html: "text/html",
  htm: "text/html",
  tsv: "text/tab-separated-values",
  mp3: "audio/mpeg",
  wav: "audio/wav",
  m4a: "audio/mp4",
  mp4: "video/mp4",
  mov: "video/quicktime",
  webm: "video/webm",
  zip: "application/zip",
  gz: "application/gzip",
};

function guessMediaType(source: ArtifactSource): string | undefined {
  const name = source.path ?? source.url ?? source.uri;
  if (name == null) {
    return undefined;
  }
  // Strip URL query/fragment before reading the extension (mimetypes.guess_type parity).
  const clean = name.split(/[?#]/, 1)[0] ?? name;
  const dot = clean.lastIndexOf(".");
  if (dot === -1 || dot === clean.length - 1) {
    return undefined;
  }
  return MEDIA_TYPES_BY_EXTENSION[clean.slice(dot + 1).toLowerCase()];
}

/** Python `_guess_kind`, table-for-table (order matters: text-like is "data", pdf "document"). */
function guessKind(mediaType: string | undefined, source: ArtifactSource): ArtifactKind {
  if (source.type === "provider_file") {
    return "provider_file";
  }
  if ((source.type === "url" || source.type === "object_uri") && mediaType === undefined) {
    return "external_uri";
  }
  if (mediaType === undefined) {
    return "other";
  }
  if (mediaType.startsWith("image/")) {
    return "image";
  }
  if (mediaType.startsWith("audio/")) {
    return "audio";
  }
  if (mediaType.startsWith("video/")) {
    return "video";
  }
  if (mediaType === "application/json" || mediaType === "text/csv" || mediaType.startsWith("text/")) {
    return "data";
  }
  if (mediaType === "application/pdf") {
    return "document";
  }
  if (mediaType === "application/zip" || mediaType === "application/gzip") {
    return "archive";
  }
  return "document";
}

/** Python `_coerce_artifact_refs`: null honors `required`; string/object → one ref; array → each. */
function coerceArtifactRefs(value: unknown, required: boolean): ArtifactRef[] {
  if (value == null) {
    if (required) {
      throw new Error("required artifact input resolved to null");
    }
    return [];
  }
  if (typeof value === "string" || (typeof value === "object" && !Array.isArray(value))) {
    return [coerceSingleArtifactRef(value)];
  }
  if (Array.isArray(value)) {
    const refs = value.map(coerceSingleArtifactRef);
    if (required && refs.length === 0) {
      throw new Error("required artifact input resolved to an empty list");
    }
    return refs;
  }
  throw new TypeError("artifact input values must be a string path, mapping, or list");
}

function coerceSingleArtifactRef(value: unknown): ArtifactRef {
  if (typeof value === "string") {
    return artifactRefSchema.parse({ source: value });
  }
  if (typeof value === "object" && value !== null && !Array.isArray(value)) {
    if ("source" in value) {
      return artifactRefSchema.parse(value);
    }
    // A bare source mapping ({ type: "url", url: ... }) is a ref with only a source.
    return artifactRefSchema.parse({ source: value });
  }
  throw new TypeError("artifact list entries must be a string path or mapping");
}

/**
 * Walk a `from_path` dot-path against the activity input (Python `_value_at_path`): the leading
 * `input` segment names the input itself; missing keys resolve to undefined (→ `required` rules).
 */
export function valueAtPath(value: unknown, path: string): unknown {
  const parts = path.split(".");
  let current: unknown = value;
  for (const part of parts.slice(1)) {
    if (current !== null && typeof current === "object" && !Array.isArray(current)) {
      current = (current as Record<string, unknown>)[part];
    } else {
      // Python parity: a list is not a Mapping, so `input.docs.0` does NOT index into it —
      // JS bracket access would, and a spec relying on that would silently break under Python.
      current = undefined;
    }
    if (current == null) {
      return current;
    }
  }
  return current;
}
