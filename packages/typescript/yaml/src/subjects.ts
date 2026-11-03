/**
 * Subject-identity extraction (#715 slice 1) — behavioral parity with the Python
 * `typeflux.core.subjects` module.
 *
 * A first-class `subjectIds` concept that enters at workflow submit and fans to
 * the `TypefluxSubjectIds` keyword-list search attribute (the subject->execution
 * index), the Langfuse observer (native `userId` + `typeflux.subject:{id}` tags),
 * and the cross-run cache record. The spine of the erasure epic (#715).
 *
 * Two entry points mirror the `artifacts: { from: input.X }` shape:
 *  - Declarative — a `subjects:` block on the workflow spec, each item a
 *    `{ from: input.<path> }` selector pulled off the validated input at start.
 *  - Explicit — a `subjectIds` override on the runtime start options, which wins.
 *
 * Extraction is loud: a required selector whose path is missing/empty throws at
 * start rather than silently starting an un-indexed (erasure-invisible) execution.
 */

/**
 * The Temporal keyword-LIST search attribute that indexes an execution by the
 * subject id(s) it processes. A fixed, unversioned name (unlike the opt-in
 * `runtime.temporal.workflow_search_attribute`): it is THE erasure index, always
 * stamped when an execution has subjects. Must be registered on the namespace as
 * a `KeywordList` before use (a deploy-time step; see docs/yaml.md, binding.v1.json).
 */
export const SUBJECT_IDS_SEARCH_ATTRIBUTE = "TypefluxSubjectIds";

/** A declarative subject selector: pull id(s) from `fromPath` on the input. */
export interface SubjectInput {
  /** A dotted `input.<path>` selector (same grammar as an artifact input's `from`). */
  readonly fromPath: string;
  /** Default true — a missing/empty resolution is a hard error at start. */
  readonly required: boolean;
}

function valueAtPath(input: unknown, path: string): unknown {
  const parts = path.split(".");
  let current: unknown = input;
  // Skip the leading `input` segment (Python `_value_at_path` parity).
  for (const part of parts.slice(1)) {
    if (current === null || current === undefined || typeof current !== "object") {
      return undefined;
    }
    current = (current as Record<string, unknown>)[part];
    if (current === null || current === undefined) {
      return undefined;
    }
  }
  return current;
}

function coerceSubjectValues(raw: unknown, fromPath: string): string[] {
  const values: unknown[] = Array.isArray(raw) ? raw : [raw];
  if (typeof raw !== "string" && !Array.isArray(raw)) {
    throw new Error(
      `subject selector '${fromPath}' must resolve to a string or an array of strings`,
    );
  }
  const coerced: string[] = [];
  for (const value of values) {
    if (typeof value !== "string" || value.trim().length === 0) {
      throw new Error(
        `subject selector '${fromPath}' resolved to a non-empty-string value (${JSON.stringify(
          value,
        )}); subject ids must be non-empty strings`,
      );
    }
    coerced.push(value);
  }
  return coerced;
}

/**
 * Extract subject id(s) from the validated workflow input. Order-preserving,
 * de-duplicated (first occurrence wins → the primary subject is stable). A
 * required selector that resolves to nothing throws; an optional one contributes
 * nothing.
 */
export function resolveSubjectIds(input: unknown, subjectInputs: readonly SubjectInput[]): string[] {
  const resolved: string[] = [];
  const seen = new Set<string>();
  for (const subject of subjectInputs) {
    const raw = valueAtPath(input, subject.fromPath);
    // A required selector must CONTRIBUTE at least one id: a missing path AND an
    // empty list both yield zero ids, and either would silently start an
    // un-indexed (erasure-invisible) execution — the exact outcome `required`
    // exists to prevent (#715 review round, finding 2).
    const values = raw === undefined || raw === null ? [] : coerceSubjectValues(raw, subject.fromPath);
    if (values.length === 0) {
      if (subject.required) {
        throw new Error(
          `subject selector '${subject.fromPath}' resolved to no value; a required subject ` +
            `must be present at start (an un-indexed execution is invisible to erasure) — ` +
            `fix the input or mark the selector optional`,
        );
      }
      continue;
    }
    for (const value of values) {
      if (!seen.has(value)) {
        seen.add(value);
        resolved.push(value);
      }
    }
  }
  return resolved;
}

/** Validate + de-duplicate an EXPLICIT `subjectIds` override (mirrors the declarative checks). */
export function normalizeSubjectIds(subjectIds: readonly string[]): string[] {
  const resolved: string[] = [];
  const seen = new Set<string>();
  for (const value of subjectIds) {
    if (typeof value !== "string" || value.trim().length === 0) {
      throw new Error(`subjectIds must be non-empty strings, got ${JSON.stringify(value)}`);
    }
    if (!seen.has(value)) {
      seen.add(value);
      resolved.push(value);
    }
  }
  return resolved;
}

/**
 * Resolve the subject ids for a start: a NON-EMPTY explicit override wins over
 * the declarative selectors; an EMPTY override is treated as "no override
 * provided" and falls through to extraction. Subjects are erasure-critical, so
 * there is no legitimate "explicitly no subjects" opt-out when the spec declares
 * required subjects — `subjectIds: []` must never silently bypass them (#715
 * review round 2). Both paths validate non-empty strings and de-dup; a required
 * selector that contributes zero ids throws here, at start.
 */
export function effectiveSubjectIds(
  explicit: readonly string[] | undefined,
  input: unknown,
  selectors: readonly SubjectInput[],
): string[] {
  if (explicit !== undefined && explicit.length > 0) {
    return normalizeSubjectIds(explicit);
  }
  return resolveSubjectIds(input, selectors);
}

/**
 * The Langfuse native `userId`: the PRIMARY (first) subject id, or undefined.
 * Native field + portable `typeflux.subject:{id}` tags always agree (#715 ratified).
 */
export function subjectUserId(subjectIds: readonly string[]): string | undefined {
  return subjectIds.length > 0 ? subjectIds[0] : undefined;
}

/** The portable per-subject trace tags: `typeflux.subject:{id}` per id. */
export function subjectTraceTags(subjectIds: readonly string[]): string[] {
  return subjectIds.map((id) => `typeflux.subject:${id}`);
}

/**
 * The visibility query enumerating every execution touching a subject. A
 * keyword-LIST attribute matches when the list CONTAINS the value.
 */
export function subjectIndexQuery(subjectId: string): string {
  // Quote-escape by DOUBLING single quotes — the Temporal visibility SQL
  // convention the existing search-attribute queries use (frozen-version.ts).
  const escaped = subjectId.replace(/'/g, "''");
  return `${SUBJECT_IDS_SEARCH_ATTRIBUTE} = '${escaped}'`;
}
