/**
 * Secret-reference enumeration (#575; Python `yaml/secrets.py` `secret_reference_records`):
 * walk the fixed allowlist of runtime secret slots and record WHERE each credential comes
 * from (`literal` / `env` / `file`) and whether it is configured RIGHT NOW — never the value.
 * Control-plane bundles and manifests surface these records so operators can see an unset
 * credential before a worker fails on it.
 *
 * `configured` is a live check (env non-empty after trim; file exists and non-empty), so the
 * caller must run this under the environment overlay it wants the answer for
 * (`withEnvironmentContext`), exactly like `secretValueConfigured`.
 */

import { existsSync, readFileSync, statSync } from "node:fs";

import type { TypefluxYamlSpec } from "./spec.js";

/** One secret slot's provenance (Python `SecretReferenceRecord`). */
export interface SecretReferenceRecord {
  /** The runtime path of the slot, e.g. `runtime.provider.api_key`. */
  runtime_path: string;
  source_kind: "literal" | "env" | "file";
  /** The env-var name or file path; empty for a literal (the value itself is never recorded). */
  source_name: string;
  configured: boolean;
}

/** A structural `value_from` read — the walker also runs over raw profile FRAGMENTS
 * (untyped dicts, #570), so candidate values arrive untyped; a shape that isn't a secret
 * reference yields nothing (exactly like Python's `isinstance(value, SecretValueSpec)` gate). */
function valueFromOf(value: unknown): { env?: string; file?: string } | undefined {
  if (typeof value !== "object" || value === null || !Object.hasOwn(value, "value_from")) {
    return undefined;
  }
  const source = (value as { value_from: unknown }).value_from;
  if (typeof source !== "object" || source === null) return undefined;
  const env = (source as { env?: unknown }).env;
  const file = (source as { file?: unknown }).file;
  return {
    ...(typeof env === "string" && env !== "" ? { env } : {}),
    ...(typeof file === "string" && file !== "" ? { file } : {}),
  };
}

/** Python `_secret_source_configured`: env non-empty after trim; file exists with size > 0. */
function sourceConfigured(source: { env?: string; file?: string }): boolean {
  if (source.env !== undefined) {
    const value = Object.hasOwn(process.env, source.env) ? process.env[source.env] : undefined;
    return value !== undefined && value.trim() !== "";
  }
  if (source.file !== undefined) {
    try {
      const stat = statSync(source.file);
      return stat.isFile() && stat.size > 0;
    } catch {
      return false;
    }
  }
  return false;
}

function appendSecretReference(records: SecretReferenceRecord[], runtimePath: string, value: unknown): void {
  if (typeof value === "string" && value !== "") {
    // Literal credentials are recorded by kind only — never the value.
    records.push({ runtime_path: runtimePath, source_kind: "literal", source_name: "", configured: true });
    return;
  }
  const source = valueFromOf(value);
  if (source === undefined) return;
  if (source.env !== undefined) {
    records.push({
      runtime_path: runtimePath,
      source_kind: "env",
      source_name: source.env,
      configured: sourceConfigured(source),
    });
    return;
  }
  if (source.file !== undefined) {
    records.push({
      runtime_path: runtimePath,
      source_kind: "file",
      source_name: source.file,
      configured: sourceConfigured(source),
    });
  }
}

/**
 * Enumerate the spec's secret references over the fixed slot allowlist (Python parity):
 * `runtime.temporal.api_key`, the three `runtime.temporal.tls.*` cert slots (only when `tls`
 * is a block, not a boolean), and `runtime.provider.api_key`.
 */
/**
 * The fixed secret-slot allowlist, as dotted paths from the spec root — the ONE list the
 * reference walker and the control plane's literal-credential redaction both consume, so a
 * slot cannot be reported by one and missed by the other. A boolean `tls` simply has no
 * leaf at the tls paths, matching the walker's old explicit block check.
 */
export const SECRET_SLOT_PATHS = [
  "runtime.temporal.api_key",
  "runtime.temporal.tls.server_root_ca_cert",
  "runtime.temporal.tls.client_cert",
  "runtime.temporal.tls.client_private_key",
  // AES-256-GCM payload codec key material (#188). A wildcard slot: one record per declared
  // key, keyed by the key id, so bundles/plans mask the key VALUE to source_kind/source_name.
  "runtime.temporal.payload_codec.keys[*].value_from",
  "runtime.provider.api_key",
  // Custom-extension config maps (#792). Map wildcards: one record per config entry, keyed by
  // the entry's key. The TS SDK stub-rejects `config` at full spec load (spec.ts `unsupported`),
  // but the control plane's profile-definition surface redacts RAW runtime fragments that never
  // pass full validation — these slots keep that surface (and list parity with Python) honest.
  "runtime.provider.config{*}",
  "runtime.registry.config{*}",
  "runtime.observability.config{*}",
  // Spec-declared observability credentials (#793): the audit's "one credential contract"
  // gap — the observers' env-var reads were invisible to this inventory.
  "runtime.observability.langfuse.public_key",
  "runtime.observability.langfuse.secret_key",
  "runtime.observability.langsmith.api_key",
] as const;

/** Marker for a map-wildcard slot: the path prefix names a flat string map whose every entry
 * is a secret slot (the custom-extension `config` blocks, #792). */
export const MAP_WILDCARD = "{*}";

const WILDCARD = "[*]";

/** Walk a full dotted slot path (starting at `runtime`) from the SPEC root; undefined when any
 * segment is absent/non-object. */
export function secretSlotValue(root: unknown, path: string): unknown {
  let node: unknown = root;
  for (const segment of path.split(".")) {
    if (typeof node !== "object" || node === null || Array.isArray(node) || !Object.hasOwn(node, segment)) {
      return undefined;
    }
    node = (node as Record<string, unknown>)[segment];
  }
  return node;
}

export function secretReferenceRecords(spec: TypefluxYamlSpec): SecretReferenceRecord[] {
  // A structural walk over the slot paths (tolerant of absent subtrees, like Python's
  // getattr(..., None) chain): a full spec always carries temporal/provider, but a profile
  // FRAGMENT (a bare runtime subtree, #570) may not.
  const records: SecretReferenceRecord[] = [];
  for (const path of SECRET_SLOT_PATHS) {
    if (path.includes(MAP_WILDCARD)) {
      appendMapReferences(records, path, spec);
    } else if (path.includes(WILDCARD)) {
      appendWildcardReferences(records, path, spec);
    } else {
      appendSecretReference(records, path, secretSlotValue(spec, path));
    }
  }
  return records;
}

/** Expand a `prefix{*}` slot over the flat map at `prefix` (a custom-extension `config`
 * block, #792): one record per entry, keyed by the entry's key. Values may be literal
 * strings or `value_from` references — either way only kind/name is recorded, never the
 * value. Unreachable from a fully-validated TS spec (spec.ts rejects `config`), but the
 * walker stays total so the exported slot list means the same thing in both editions. */
function appendMapReferences(records: SecretReferenceRecord[], path: string, root: unknown): void {
  const prefix = path.slice(0, -MAP_WILDCARD.length).replace(/\.$/, "");
  const mapping = secretSlotValue(root, prefix);
  if (typeof mapping !== "object" || mapping === null || Array.isArray(mapping)) return;
  for (const [key, value] of Object.entries(mapping as Record<string, unknown>)) {
    appendSecretReference(records, `${prefix}[${key}]`, value);
  }
}

/** Expand a `prefix[*].leaf` slot over the array at `prefix` (the payload-codec keys):
 * one record per element, its `runtime_path` keyed by the element's `id`. The `leaf`
 * (`value_from`) is itself the `{env|file}` source, so it is wrapped as a reference for the
 * shared appender — the value is never recorded. */
function appendWildcardReferences(records: SecretReferenceRecord[], path: string, root: unknown): void {
  const wildcardIndex = path.indexOf(WILDCARD);
  const prefix = path.slice(0, wildcardIndex).replace(/\.$/, "");
  const leaf = path.slice(wildcardIndex + WILDCARD.length).replace(/^\./, "");
  const array = secretSlotValue(root, prefix);
  if (!Array.isArray(array)) return;
  array.forEach((item, index) => {
    if (typeof item !== "object" || item === null) return;
    const record = item as Record<string, unknown>;
    const source = record[leaf];
    const keyId = typeof record.id === "string" && record.id !== "" ? record.id : String(index);
    appendSecretReference(records, `${prefix}[${keyId}].${leaf}`, { value_from: source });
  });
}

/**
 * THE one `string | value_from` text resolution (#793; Python
 * `resolve_optional_secret_text` parity): a literal passes through un-trimmed ("" means
 * unset); `value_from.env` reads the environment and trims; `value_from.file` reads the
 * file and trims. A `required` reference (the default) that resolves to nothing THROWS —
 * a declared credential slot must not silently fall through. Extracted from
 * `resolveProviderApiKey` so every spec credential slot shares one behavior.
 */
export function resolveOptionalSecretText(
  value:
    | string
    | { value_from: { env?: string | undefined; file?: string | undefined; required?: boolean | undefined } }
    | undefined,
  runtimePath: string,
  environment: Record<string, string | undefined> = process.env,
): string | undefined {
  if (value === undefined) return undefined;
  if (typeof value === "string") {
    // Python: `value if value != "" else None` — literals are NOT trimmed.
    return value !== "" ? value : undefined;
  }
  const { env, file, required } = value.value_from;
  if (env !== undefined) {
    const raw = environment[env];
    if (raw === undefined) {
      if (required ?? true) {
        throw new Error(`missing required secret for ${runtimePath}: env ${env} is not set`);
      }
      return undefined;
    }
    const normalized = raw.trim();
    if (normalized === "") {
      if (required ?? true) {
        throw new Error(`secret for ${runtimePath} from env ${env} is empty`);
      }
      return undefined;
    }
    return normalized;
  }
  if (file !== undefined) {
    // Python `Path(file)`: no ~ expansion (mirrors yaml/secrets.py rules).
    if (!existsSync(file)) {
      if (required ?? true) {
        throw new Error(`missing required secret for ${runtimePath}: file ${file} does not exist`);
      }
      return undefined;
    }
    let raw: string;
    try {
      raw = readFileSync(file, "utf-8");
    } catch (error) {
      throw new Error(`could not read secret for ${runtimePath}: file ${file}`, { cause: error });
    }
    const normalized = raw.trim();
    if (normalized === "") {
      if (required ?? true) {
        throw new Error(`secret for ${runtimePath} from file ${file} is empty`);
      }
      return undefined;
    }
    return normalized;
  }
  throw new Error(`secret value_from for ${runtimePath} must configure env or file`);
}
