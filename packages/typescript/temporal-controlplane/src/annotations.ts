/**
 * Insight acknowledgement annotations (#733 §1; Python `project/annotations.py`): the read side of
 * the in-repo insight-ack ledger. Parses `.typeflux/annotations.yaml` (beside the project manifest)
 * into the contract-shaped projection the read tier serves, and reports a parse error so the
 * validation surface can raise an AUTHORING issue for a malformed file.
 *
 * The maintainer decision (#577) keeps ack/suppression state IN THE REPO — PR-reviewed, git as the
 * single source of truth, no console-side or control-plane-side mutable state. So this is a
 * pure-YAML, project-level read with NO seam and NO resolution dependency: always servable, even for
 * a project this server cannot resolve.
 *
 * Parsing is FAIL-CLOSED, byte-for-byte with the Python edition (behavioral parity):
 *  - absent file        → an EMPTY projection, NOT an error (the common case).
 *  - empty/comments-only → an EMPTY projection, NOT an error.
 *  - valid file          → the parsed entries, in file order.
 *  - malformed/invalid   → an EMPTY projection AND a parse-error message (never a partial parse);
 *                          the error surfaces as a project validation issue, not here.
 *
 * An expired entry is SERVED with its expiry (the "stale ack" rendering is the console's job, slice
 * 3) — expiry never mutates or hides an entry.
 */

import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";

import { parseDocument } from "yaml";
import { z } from "zod";

import type { components } from "./http/contract.js";

/** The served projection envelope (Python `ProjectAnnotations`), aliased off the generated contract
 * so a schema regen that changes the shape breaks this compile instead of drifting the edition. */
export type ApiProjectAnnotations = components["schemas"]["ProjectAnnotations"];
/** One acknowledgement entry (Python `InsightAnnotation`). */
export type ApiInsightAnnotation = components["schemas"]["InsightAnnotation"];

/** The annotations file location relative to the manifest: `<manifestDir>/.typeflux/annotations.yaml`
 * (Python `annotations_path`). */
export const ANNOTATIONS_DIRNAME = ".typeflux";
export const ANNOTATIONS_FILENAME = "annotations.yaml";

const nonEmptyTrimmed = (value: string): boolean => value.length > 0 && value.trim() === value;

/** SHAPE-only URL validation (Python `_validate_tracked_in`): an http(s) URL with a host. The read
 * tier NEVER fetches it. */
function isHttpUrl(value: string): boolean {
  if (!nonEmptyTrimmed(value)) return false;
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    return false;
  }
  return (parsed.protocol === "http:" || parsed.protocol === "https:") && parsed.host.length > 0;
}

/** `expires` is an ISO calendar date (`YYYY-MM-DD`) — matching Python's `datetime.date` acceptance.
 * The YAML core schema leaves the scalar a string (no timestamp tag), so validate the string shape
 * and that it names a real date. */
function isIsoDate(value: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const [year, month, day] = value.split("-").map((part) => Number(part));
  const date = new Date(Date.UTC(year!, month! - 1, day!));
  return (
    date.getUTCFullYear() === year && date.getUTCMonth() === month! - 1 && date.getUTCDate() === day
  );
}

const insightAnnotationSchema = z
  .object({
    insight_id_pattern: z
      .string()
      .refine(nonEmptyTrimmed, "insight_id_pattern must be a non-empty, trimmed string"),
    reason: z.string().refine(nonEmptyTrimmed, "reason must be a non-empty, trimmed string"),
    // `.nullish()` + null→undefined: YAML authors write `tracked_in:` / explicit nulls, and
    // Python's pydantic optionals treat explicit null as absent — the editions must accept the
    // same files (codex: a null here invalidated the WHOLE file on this edition only).
    tracked_in: z
      .string()
      .refine(isHttpUrl, "tracked_in must be an http(s) URL citing the tracking issue")
      .nullish()
      .transform((value) => value ?? undefined),
    expires: z
      .string()
      .refine(isIsoDate, "expires must be an ISO date (YYYY-MM-DD)")
      .nullish()
      .transform((value) => value ?? undefined),
  })
  .strict();

const annotationsFileSchema = z
  .object({
    annotations: z.array(insightAnnotationSchema).default([]),
  })
  .strict();

/** The single parse both consumers (the projection + the validation issue) derive from, so they can
 * never disagree. `error` is `undefined` on success/absence, a human-readable message when the file
 * exists but is malformed. */
export interface AnnotationsReadResult {
  annotations: ApiProjectAnnotations;
  error?: string;
  path: string;
}

/** The annotations file path for a manifest (Python `annotations_path`). */
export function annotationsPath(manifestPath: string): string {
  return join(dirname(manifestPath), ANNOTATIONS_DIRNAME, ANNOTATIONS_FILENAME);
}

/** Parse the project's annotations file, fail-closed (Python `read_project_annotations`). */
export function readProjectAnnotations(manifestPath: string): AnnotationsReadResult {
  const path = annotationsPath(manifestPath);
  if (!existsSync(path)) {
    return { annotations: { annotations: [] }, path };
  }
  let raw: unknown;
  try {
    // `merge: true` expands YAML `<<` merge keys, matching Python's strict_safe_load and the
    // temporal-yaml loader — without it a merge key reaches the strict schema and invalidates
    // the whole file on this edition only (codex).
    const doc = parseDocument(readFileSync(path, "utf-8"), { uniqueKeys: true, merge: true });
    const fatal = [...doc.errors, ...doc.warnings.filter((w) => w.code === "DUPLICATE_KEY")];
    if (fatal.length > 0) {
      return {
        annotations: { annotations: [] },
        error: `annotations file is not valid YAML: ${fatal[0]?.message}`,
        path,
      };
    }
    raw = doc.toJS();
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    return { annotations: { annotations: [] }, error: `annotations file is not valid YAML: ${message}`, path };
  }
  if (raw === null || raw === undefined) {
    // Empty or comments-only file: an empty ledger, not a malformed one.
    return { annotations: { annotations: [] }, path };
  }
  const parsed = annotationsFileSchema.safeParse(raw);
  if (!parsed.success) {
    return {
      annotations: { annotations: [] },
      error: `annotations file does not match the schema: ${parsed.error.message}`,
      path,
    };
  }
  // Rebuild each entry so absent optionals are OMITTED (never `undefined`-valued keys) — parity with
  // Python's `exclude_none` serialization, so the wire body matches byte-for-byte.
  const annotations: ApiInsightAnnotation[] = parsed.data.annotations.map((entry) => {
    const out: ApiInsightAnnotation = {
      insight_id_pattern: entry.insight_id_pattern,
      reason: entry.reason,
    };
    if (entry.tracked_in !== undefined) out.tracked_in = entry.tracked_in;
    if (entry.expires !== undefined) out.expires = entry.expires;
    return out;
  });
  return { annotations: { annotations }, path };
}

/** The annotations projection the read tier serves (Python `load_project_annotations`): the parsed
 * entries, or an empty projection when the file is absent or malformed (fail-closed). */
export function loadProjectAnnotations(manifestPath: string): ApiProjectAnnotations {
  return readProjectAnnotations(manifestPath).annotations;
}
