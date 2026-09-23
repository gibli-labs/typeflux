/**
 * Enforcement-events feed (#723 slice 2; Python `project/enforcement.py`): a normalized,
 * read-at-request surface over the two enforcement sources the control plane already computes.
 * Pure functions over data — the only impurity (the Langfuse trace read) lives behind the injected
 * transport seam (#573, `langfuse-transport.ts`), so tests replace it with a fixture.
 *
 * The control plane persists nothing here — every request re-reads both sources (the stateless
 * re-read-per-request design of #577 stands):
 *
 *  - **Admission events** are NORMALIZED from the read tier's existing validation report — the
 *    policy/admission-gate verdicts it already derives — never re-computed. A plain authoring/config
 *    error (`duplicate_workflow_name`, a broken-policy-YAML `policy_composition` conflict) is NOT an
 *    enforcement verdict and stays in the validation surface. Which codes count as verdicts is an
 *    EXPLICIT allow-set ({@link ADMISSION_ENFORCEMENT_CODES}), never a substring heuristic — a
 *    substring match on `"policy"` wrongly swept in config errors whose `reference` is a policy id,
 *    mislabeling it as a workflow id.
 *  - **Runtime events** are extracted from Langfuse traces read through the transport seam. The ONLY
 *    confirmed runtime marker this slice is a moderation `on_violation=block` verdict (recorded as
 *    `typeflux_moderation.decision === "block"` on the activity span, #158). The query is BOUNDED —
 *    an explicit time window (default 7d) and a row cap — never an unbounded scan.
 *
 * There is deliberately NO runtime review-rejection source this slice (see the Python module's long
 * rationale): the only client-side signal is an errored `TypefluxLifecycleSignal:*` span, and no
 * honest heuristic can be built on it (it also wraps cancel + marks any raised exception, while a
 * real review verdict is decided server-side and never errors this span). A faithful review-rejection
 * event needs a dedicated writer-side marker recorded by the engine (slice 2+, #723).
 *
 * Reachability of the runtime source DEGRADES LOUDLY: an unconfigured observer reports
 * `langfuse: "not_configured"` and a transport failure reports `langfuse: "unreachable"` in the
 * response's `partial` marker — the admission events still serve, and the runtime portion is NEVER a
 * silent empty list.
 */

import { createHash } from "node:crypto";

import { isRecord } from "./guards.js";
import type { components } from "./http/contract.js";
import type { ApiProjectValidationReport, ApiResolvedWorkflowValidation, ApiValidationCheck } from "./validation-dto.js";

/** Default read window (ms) when the caller pins neither `since` nor `until` — 7 days (#723: an explicit, bounded window). */
export const DEFAULT_WINDOW_MS = 7 * 24 * 60 * 60 * 1000;
/** Default page size for pagination and the bounded trace fetch. */
export const DEFAULT_LIMIT = 50;
/** Hard cap on the page size and the bounded trace fetch. */
export const MAX_LIMIT = 200;

// The wire DTOs are ALIASES of the generated contract schemas (`http/contract.ts`), never
// hand-restated — so a contract regen that renames/retypes a field breaks this compile instead of
// silently drifting the edition off the shared OpenAPI surface. The generated optional fields are
// `T | null`; this module still OMITS absent fields (never emits `null`) so `JSON.stringify`
// reproduces Python's `response_model_exclude_none` — an omitted key satisfies `field?: T | null`.
/** One normalized enforcement decision (#723). See the contract schema for field semantics. */
export type EnforcementEvent = components["schemas"]["EnforcementEvent"];
/** The enforcement-events response envelope (#723). `next_cursor` is present only when more events remain. */
export type EnforcementEventList = components["schemas"]["EnforcementEventList"];
/** Where to see the evidence for one enforcement event (the console derives a Langfuse deep link from `trace_id`). */
export type EnforcementEvidence = components["schemas"]["EnforcementEvidence"];
/** Loud-degradation marker (#723): the reachability of the best-effort runtime source. */
export type EnforcementPartial = components["schemas"]["EnforcementPartial"];
/** The reachability of the runtime (Langfuse) source, mapped straight into the `partial` marker. */
export type LangfuseEnforcementStatus = EnforcementPartial["langfuse"];
/**
 * Runtime moderation blocks produce `blocked`; admission policy verdicts produce `rejected`. There
 * is no `warned` producer this slice, so the vocabulary is closed to the two emitted verdicts.
 */
export type EnforcementVerdict = EnforcementEvent["verdict"];
export type EnforcementSource = EnforcementEvent["source"];

/** The transport seam's result: a reachability `status` (mapped into `partial`) + the bounded traces it read. */
export interface EnforcementReadResult {
  status: LangfuseEnforcementStatus;
  traces: readonly EnforcementTraceRecord[];
}

/** One observation (activity span) on a Langfuse trace, as the transport surfaces it for enforcement. */
export interface EnforcementTraceObservation {
  /** The span start time (ISO string). A naive (offset-less) value is normalized to UTC at ingestion. */
  startTime?: string | null;
  /** The span metadata bag — the moderation marker lives here as a FLAT `typeflux_moderation` key. */
  metadata?: Record<string, unknown> | null;
}

/**
 * One Langfuse trace, as the transport surfaces it for enforcement extraction (Python's `TraceRecord`
 * + the trace-level fields `TraceSummaryView` derives). The transport parses the vendor payload into
 * this edition-native shape so the pure extractor stays fixture-injectable and vendor-free.
 */
export interface EnforcementTraceRecord {
  traceId: string;
  /** The trace timestamp (ISO string), used as the event `occurred_at` fallback when a span has none. */
  timestamp?: string | null;
  /** The workflow TYPE name recorded on the trace (normalized to the project id downstream). */
  workflowName?: string | null;
  /** The Temporal workflow (execution) id — surfaced as the event `execution_id`. */
  workflowId?: string | null;
  /** The environment id recorded on the trace, if any. */
  environment?: string | null;
  /** The trace's recorded applied-policy provenance. */
  appliedPolicyIds?: readonly string[];
  observations: readonly EnforcementTraceObservation[];
}

// ---------------------------------------------------------------------------
// Admission events — normalized from the read tier's validation report.
// ---------------------------------------------------------------------------

/**
 * Policy-verdict CHECK codes: a resolved workflow that FAILS one of these was rejected by its
 * composed project policy (#300/#454/#298). Each is a governance verdict against a workflow, so it
 * becomes an admission enforcement event. Mirrors Python `_POLICY_VERDICT_CODES` exactly — the TS
 * validation surface emits every one of these check codes (`policy-enforcement.ts`).
 */
export const POLICY_VERDICT_CODES: ReadonlySet<string> = new Set([
  "policy_allowlists", // composed allowlist gate
  "policy_artifacts", // artifact policy gate
  "policy_composition_ceilings", // #298 admission ceilings
  "policy_floor", // policy floor gate
  "policy_imports", // import allowlist gate
  "policy_observability", // observability policy gate
  "policy_provider", // provider/model allowlist gate
  "policy_provider_limits", // provider limit gate
  "policy_provider_retry", // provider retry gate
  "policy_registry", // prompt-registry gate
  "policy_review", // review-route gate
  "policy_risk_tier", // #300 risk-tier gate
  "risk_tier_binding", // #788 elevated-tier-must-be-enforced gate
  "policy_secrets", // secret-reference gate
  "policy_selection", // policy-selection gate
  "policy_semantics", // moderation/semantics gate
  "policy_subworkflow_closure", // #55 transitive-closure gate
  "policy_temporal", // Temporal-config gate
]);

/**
 * Admission-gate verdict codes (`admission.ts` `admitSpec`, #298): genuine admission decisions
 * against a submitted spec. Mirrors Python `_ADMISSION_VERDICT_CODES`. The TS surface additionally
 * emits `admission_spec_shape` and `admission_external_modules_forbidden` — both DELIBERATELY
 * EXCLUDED (see below), the TS counterparts of Python's excluded `admission_spec_shape` /
 * `provider_import_policy` authoring errors.
 */
export const ADMISSION_VERDICT_CODES: ReadonlySet<string> = new Set([
  "admission_unknown_workflow", // an explicit slot the manifest doesn't declare
  "admission_policy_selection", // external-origin spec with no governing policy
]);

/**
 * Every code that is a genuine admission/policy ENFORCEMENT VERDICT. Membership is EXACT — NOT a
 * substring test. DELIBERATELY EXCLUDED (authoring/config errors that merely mention
 * "policy"/"admission" — not verdicts against a workflow, and whose `reference` is a policy/target
 * id, never a workflow id):
 *   - `policy_composition` / `invalid_policy_composition` / `invalid_target_policy_composition` —
 *     broken policy YAML (a composition conflict), reference is a policy/target id.
 *   - `admission_spec_shape` / `admission_external_modules_forbidden` — the submitted spec failed to
 *     validate / declared a forbidden external module (the TS analogue of Python's
 *     `admission_spec_shape` / `provider_import_policy` authoring checks).
 *   - `unknown_validation_policy` / `unknown_target_policy` — config reference errors naming a policy/target.
 *   - `policy_enforcement` — the "no policy selected" SKIP marker (never fails).
 */
export const ADMISSION_ENFORCEMENT_CODES: ReadonlySet<string> = new Set([
  ...POLICY_VERDICT_CODES,
  ...ADMISSION_VERDICT_CODES,
]);

/**
 * Whether a validation issue/check `code` is a POLICY or admission-gate ENFORCEMENT VERDICT (as
 * opposed to a plain authoring/config error). Exact membership in {@link ADMISSION_ENFORCEMENT_CODES}
 * — a broken-policy-YAML composition error or a spec-shape authoring error is NOT a verdict against a
 * workflow and stays in the validation surface.
 */
export function isAdmissionEnforcementCode(code: string): boolean {
  return ADMISSION_ENFORCEMENT_CODES.has(code);
}

/**
 * Best-effort split of a validation issue `reference` into `[workflowId, environmentId]`. Only
 * reached for the enforcement-verdict codes above, whose lift uses `"{environment}:{workflow}"`; a
 * bare reference is a workflow id. (Config-error references — a bare policy/target id — never reach
 * here because their codes are excluded from the verdict set.)
 */
function splitIssueReference(reference: string | undefined): { workflowId?: string; environmentId?: string } {
  if (reference === undefined || reference === "") return {};
  const index = reference.indexOf(":");
  if (index >= 0) {
    const environment = reference.slice(0, index);
    const workflow = reference.slice(index + 1);
    return {
      ...(workflow !== "" ? { workflowId: workflow } : {}),
      ...(environment !== "" ? { environmentId: environment } : {}),
    };
  }
  return { workflowId: reference };
}

/**
 * The composed policy this resolved workflow was evaluated under, read from the `policy_selection`
 * check's `applied_policy_ids` detail — the genuine recorded provenance (never the caller's filter).
 */
function appliedPolicyIds(resolved: ApiResolvedWorkflowValidation): string[] {
  for (const check of resolved.checks) {
    if (check.code === "policy_selection") {
      const applied = check.details["applied_policy_ids"];
      if (Array.isArray(applied)) return applied.map((id) => String(id));
    }
  }
  return [];
}

/**
 * Normalize the enforcement verdicts out of a validation report. Per-workflow failed policy/admission
 * checks become the primary events; a top-level enforcement issue with no resolved-workflow context is
 * added too. The read tier LIFTS each failed resolved check into a top-level `resolved_*` issue —
 * those are skipped here so they are not double-counted. `policy_ids` on each event is the RECORDED
 * applied policy provenance (from the workflow's `policy_selection` check), not the caller's filter.
 */
export function admissionEventsFromReport(
  report: ApiProjectValidationReport,
  options: { environmentId?: string } = {},
): EnforcementEvent[] {
  const events: EnforcementEvent[] = [];
  for (const resolved of report.resolved_workflows) {
    const applied = appliedPolicyIds(resolved);
    for (const check of resolved.checks) {
      if (check.status !== "failed" || !isAdmissionEnforcementCode(check.code)) continue;
      const environmentId = resolved.environment_id || options.environmentId;
      events.push({
        source: "admission",
        verdict: "rejected",
        rule: check.code,
        policy_ids: applied,
        workflow_id: resolved.workflow_id,
        ...(environmentId !== undefined ? { environment_id: environmentId } : {}),
        evidence: {},
        // `||` (Python `or`): an EMPTY-string message must take the fallback, not surface as a blank
        // detail (`??` would keep `""`). The documented truthiness porting trap.
        detail: check.message || `admission rejected: ${check.code}`,
      });
    }
  }
  for (const issue of report.issues) {
    if (issue.code.startsWith("resolved_")) continue; // already normalized from the resolved-workflow check above.
    if (!isAdmissionEnforcementCode(issue.code)) continue;
    const { workflowId, environmentId } = splitIssueReference(issue.reference);
    const env = environmentId ?? options.environmentId;
    events.push({
      source: "admission",
      verdict: "rejected",
      rule: issue.code,
      // A top-level issue has no resolved-workflow context, so no recorded applied-policy provenance
      // — the field is dropped (empty), never the caller's filter masquerading as provenance.
      policy_ids: [],
      ...(workflowId !== undefined ? { workflow_id: workflowId } : {}),
      ...(env !== undefined ? { environment_id: env } : {}),
      evidence: {},
      detail: issue.message,
    });
  }
  return events;
}

/**
 * The resolved observer type (`langfuse`/`none`/…) for the report's environment, read from the
 * `observability_config` check the read tier emits — `undefined` when nothing resolved (so the
 * runtime source is reported `not_configured`).
 */
export function observerFromReport(report: ApiProjectValidationReport): string | undefined {
  for (const resolved of report.resolved_workflows) {
    for (const check of resolved.checks) {
      if (check.code === "observability_config") {
        const observer = check.details["type"];
        if (typeof observer === "string") return observer;
      }
    }
  }
  return undefined;
}

// ---------------------------------------------------------------------------
// Runtime events — extracted from Langfuse traces (through the #573 seam).
// ---------------------------------------------------------------------------

/**
 * Parse an ISO-8601 timestamp to an aware-UTC `Date`. A trailing `Z` is honored; a NAIVE
 * (offset-less) value is treated as UTC — so a naive timestamp from an injected reader can never
 * later compare naive-vs-local and drift the window (the Python `_as_utc_aware` normalization). An
 * unparseable value is `null`. FULL precision is retained (milliseconds are NOT stripped) so window
 * filtering and ordering stay correct at sub-second boundaries.
 */
function toUtcDate(value: string | Date | null | undefined): Date | null {
  if (value === null || value === undefined) return null;
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value;
  const trimmed = value.trim();
  if (trimmed === "") return null;
  // No timezone designator (`Z` or ±hh:mm after the time) ⇒ treat as UTC, not local.
  const hasZone = /[zZ]$/.test(trimmed) || /[+-]\d{2}:?\d{2}$/.test(trimmed);
  const date = new Date(hasZone ? trimmed : `${trimmed}Z`);
  return Number.isNaN(date.getTime()) ? null : date;
}

/**
 * ISO-8601 (aware-UTC, `+00:00` offset) of a timestamp, or `null`. Mirrors Python's
 * `datetime.isoformat()` fractional-second behavior: NO fractional part when the millisecond
 * component is zero, else six-digit microseconds (JS carries millisecond resolution, so the three
 * millisecond digits are zero-padded to microseconds — `12:00:00.500` → `.500000`). Full precision
 * is preserved (never truncated to whole seconds) so a same-second block is not lost.
 */
function isoUtc(value: string | Date | null | undefined): string | null {
  const date = toUtcDate(value);
  if (date === null) return null;
  const iso = date.toISOString(); // always `YYYY-MM-DDTHH:MM:SS.mmmZ`
  return date.getUTCMilliseconds() === 0
    ? iso.replace(/\.000Z$/, "+00:00")
    : iso.replace(/\.(\d{3})Z$/, ".$1000+00:00");
}

/**
 * Recognize an enforcement verdict on one trace observation, or `undefined`. The only confirmed
 * runtime marker this slice is `typeflux_moderation.decision === "block"` — a moderation
 * `on_violation=block` verdict (#158), recorded by the engine on the activity span (never re-derived).
 */
function eventFromObservation(
  observation: EnforcementTraceObservation,
  context: {
    traceId: string;
    workflowId?: string;
    workflowName?: string;
    environmentId?: string;
    traceTimestamp?: string | null;
    policyIds: readonly string[];
  },
): EnforcementEvent | undefined {
  const metadata = isRecord(observation.metadata) ? observation.metadata : {};
  const moderation = metadata["typeflux_moderation"];
  if (isRecord(moderation) && moderation["decision"] === "block") {
    const categories = moderation["categories"];
    const listed = Array.isArray(categories) ? categories.map((c) => String(c)).join(", ") : "";
    const occurredAt = isoUtc(observation.startTime) ?? isoUtc(context.traceTimestamp);
    return {
      ...(occurredAt !== null ? { occurred_at: occurredAt } : {}),
      source: "runtime",
      verdict: "blocked",
      rule: "moderation.on_violation.block",
      policy_ids: [...context.policyIds],
      ...(context.workflowName !== undefined ? { workflow_id: context.workflowName } : {}),
      ...(context.environmentId !== undefined ? { environment_id: context.environmentId } : {}),
      ...(context.workflowId !== undefined ? { execution_id: context.workflowId } : {}),
      evidence: { trace_id: context.traceId },
      detail: `moderation blocked activity output: ${listed || "unspecified"}`,
    };
  }
  return undefined;
}

/**
 * Extract every runtime enforcement event from a set of Langfuse traces. A trace records the workflow
 * TYPE name (the Temporal/YAML name), not the PROJECT workflow id the API filters/validates against;
 * `workflowNameToId` (built from the validation report's resolved workflows) normalizes each event's
 * `workflow_id` to the project id. An unmapped name falls back to the raw name (a trace for a workflow
 * not in the resolved set — renamed/removed — still surfaces, keyed by its raw name).
 */
export function runtimeEventsFromTraces(
  traces: Iterable<EnforcementTraceRecord>,
  options: { environmentId?: string; workflowNameToId?: Readonly<Record<string, string>> } = {},
): EnforcementEvent[] {
  const nameToId = options.workflowNameToId ?? {};
  const events: EnforcementEvent[] = [];
  for (const trace of traces) {
    const env = (trace.environment ?? undefined) || options.environmentId;
    const workflowName = trace.workflowName ?? undefined;
    const workflowId = trace.workflowId ?? undefined;
    // Resolve the trace's workflow TYPE name to the PROJECT id UP FRONT (an unmapped name falls back
    // to the raw name), so the event is constructed with its final `workflow_id` rather than built
    // and then patched. `eventFromObservation` writes `context.workflowName` into `event.workflow_id`.
    const resolvedWorkflowId =
      workflowName !== undefined && Object.hasOwn(nameToId, workflowName) ? nameToId[workflowName]! : workflowName;
    for (const observation of trace.observations) {
      const event = eventFromObservation(observation, {
        traceId: trace.traceId,
        ...(workflowId !== undefined ? { workflowId } : {}),
        ...(resolvedWorkflowId !== undefined ? { workflowName: resolvedWorkflowId } : {}),
        ...(env !== undefined ? { environmentId: env } : {}),
        traceTimestamp: trace.timestamp ?? null,
        policyIds: trace.appliedPolicyIds ?? [],
      });
      if (event === undefined) continue;
      events.push(event);
    }
  }
  return events;
}

// ---------------------------------------------------------------------------
// Window / filtering / ordering / pagination — pure helpers.
// ---------------------------------------------------------------------------

/** Resolve the bounded read window, defaulting to the last {@link DEFAULT_WINDOW_MS}. */
export function resolveWindow(
  since: Date | undefined,
  until: Date | undefined,
  now: Date = new Date(),
): { since: Date; until: Date } {
  const untilDt = until ?? now;
  const sinceDt = since ?? new Date(untilDt.getTime() - DEFAULT_WINDOW_MS);
  return { since: sinceDt, until: untilDt };
}

/**
 * Apply the request filters to the MERGED feed. A runtime event outside the `[since, until]` window is
 * dropped; an admission event (no `occurred_at`) reflects current state and is always in-window. The
 * `policyIds` filter keeps an event whose RECORDED applied policy ids intersect the request set —
 * applied consistently to admission and runtime events.
 */
export function filterEvents(
  events: Iterable<EnforcementEvent>,
  options: {
    workflowIds?: readonly string[];
    environmentId?: string;
    verdicts?: readonly string[];
    policyIds?: readonly string[];
    since?: Date;
    until?: Date;
  } = {},
): EnforcementEvent[] {
  const workflowSet = new Set(options.workflowIds ?? []);
  const verdictSet = new Set(options.verdicts ?? []);
  const policySet = new Set(options.policyIds ?? []);
  const kept: EnforcementEvent[] = [];
  for (const event of events) {
    // `== null` (not `=== undefined`): the aliased contract type admits `null`, though this module
    // only ever omits an absent field — the loose check treats both the same.
    if (workflowSet.size > 0 && (event.workflow_id == null || !workflowSet.has(event.workflow_id))) continue;
    if (
      options.environmentId !== undefined &&
      event.environment_id != null &&
      event.environment_id !== options.environmentId
    ) {
      continue;
    }
    if (verdictSet.size > 0 && !verdictSet.has(event.verdict)) continue;
    if (policySet.size > 0 && !event.policy_ids.some((id) => policySet.has(id))) continue;
    const occurred = toUtcDate(event.occurred_at);
    if (occurred !== null) {
      if (options.since !== undefined && occurred.getTime() < options.since.getTime()) continue;
      if (options.until !== undefined && occurred.getTime() > options.until.getTime()) continue;
    }
    kept.push(event);
  }
  return kept;
}

/**
 * Newest first; an admission event (no timestamp) reflects current state and sorts ahead of dated
 * runtime events. Every remaining key is ascending and total, so ordering is fully deterministic
 * (conformance depends on it).
 */
function compareEvents(a: EnforcementEvent, b: EnforcementEvent): number {
  const at = toUtcDate(a.occurred_at);
  const bt = toUtcDate(b.occurred_at);
  // `-Infinity` for a missing timestamp so admission events sort first (newest-first on the negated ts).
  const ap = at === null ? -Infinity : -at.getTime();
  const bp = bt === null ? -Infinity : -bt.getTime();
  if (ap !== bp) return ap < bp ? -1 : 1;
  const keys: Array<(e: EnforcementEvent) => string> = [
    (e) => e.source,
    (e) => e.rule,
    (e) => e.workflow_id ?? "",
    (e) => e.environment_id ?? "",
    (e) => e.execution_id ?? "",
    (e) => e.detail,
  ];
  for (const key of keys) {
    const ak = key(a);
    const bk = key(b);
    if (ak !== bk) return ak < bk ? -1 : 1;
  }
  return 0;
}

export function sortEvents(events: Iterable<EnforcementEvent>): EnforcementEvent[] {
  return [...events].sort(compareEvents);
}

/**
 * A short, stable fingerprint of the filter set a cursor was minted under. A cursor is a bare offset
 * into a filtered+ordered list; reusing it after the filters change would silently skip or duplicate
 * events, so the offset is bound to this fingerprint and a mismatch is rejected (422).
 *
 * `since`/`until` are the caller's RAW (unresolved) window bounds — `undefined` when unpinned — NOT
 * the resolved window: an unpinned `until` resolves to `now()` afresh each request, so fingerprinting
 * the resolved value would reject every paginated call.
 */
export function filterFingerprint(options: {
  workflowIds?: readonly string[];
  environmentId?: string;
  verdicts?: readonly string[];
  policyIds?: readonly string[];
  since?: Date;
  until?: Date;
}): string {
  const payload = JSON.stringify({
    e: options.environmentId ?? null,
    p: [...(options.policyIds ?? [])].sort(),
    s: options.since !== undefined ? options.since.toISOString() : null,
    u: options.until !== undefined ? options.until.toISOString() : null,
    v: [...(options.verdicts ?? [])].sort(),
    w: [...(options.workflowIds ?? [])].sort(),
  });
  return createHash("sha256").update(payload, "utf8").digest("hex").slice(0, 12);
}

/** A malformed/mismatched pagination cursor — the HTTP adapter maps it to a 422. */
export class EnforcementCursorError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "EnforcementCursorError";
  }
}

/**
 * Encode an opaque pagination cursor: the offset bound to the filter `fingerprint`. Base64url-opaque
 * by design (an operator must not hand-craft an offset) — never a bare stringified int.
 */
export function encodeCursor(offset: number, fingerprint: string): string {
  return Buffer.from(JSON.stringify({ f: fingerprint, o: offset }), "utf8").toString("base64url");
}

/**
 * Decode an opaque pagination cursor to an offset, verifying it was minted under the current filters.
 * A malformed cursor or a filter mismatch is a bad request — throw {@link EnforcementCursorError}
 * (the adapter maps it to 422).
 */
export function decodeCursor(cursor: string | undefined, fingerprint: string): number {
  if (cursor === undefined || cursor === "") return 0;
  let payload: unknown;
  try {
    payload = JSON.parse(Buffer.from(cursor, "base64url").toString("utf8"));
  } catch {
    throw new EnforcementCursorError(`malformed pagination cursor: ${JSON.stringify(cursor)}`);
  }
  if (!isRecord(payload) || typeof payload["o"] !== "number" || !Number.isInteger(payload["o"])) {
    throw new EnforcementCursorError(`malformed pagination cursor: ${JSON.stringify(cursor)}`);
  }
  const bound = payload["f"];
  if (typeof bound !== "string" || bound !== fingerprint) {
    throw new EnforcementCursorError(
      "pagination cursor does not match the current filters; omit the cursor to restart from the " +
        "first page when any filter changes",
    );
  }
  const offset = payload["o"];
  if (offset < 0) throw new EnforcementCursorError(`pagination cursor offset must be >= 0: ${offset}`);
  return offset;
}

/** Slice one page out of the ordered events and mint a `next_cursor` only when more remain. */
export function paginate(
  events: readonly EnforcementEvent[],
  options: { offset: number; limit: number; fingerprint: string },
): { page: EnforcementEvent[]; nextCursor?: string } {
  const page = events.slice(options.offset, options.offset + options.limit);
  const nextOffset = options.offset + options.limit;
  if (nextOffset < events.length) {
    return { page, nextCursor: encodeCursor(nextOffset, options.fingerprint) };
  }
  return { page };
}

/**
 * Compose the enforcement feed from both sources: normalize, filter, order, and paginate — the one
 * place the two sources are merged. `policyIds` here is the caller's FILTER (applied in
 * {@link filterEvents}); each event carries its own recorded applied-policy provenance.
 * `cursorFingerprint` (computed by the caller from the RAW window intent) binds the emitted
 * `next_cursor` to this filter set.
 */
export function buildEnforcementFeed(options: {
  report: ApiProjectValidationReport | undefined;
  readResult: EnforcementReadResult;
  environmentId?: string;
  policyIds: readonly string[];
  workflowIds: readonly string[];
  verdicts: readonly string[];
  since: Date;
  until: Date;
  limit: number;
  offset: number;
  cursorFingerprint: string;
}): EnforcementEventList {
  const events: EnforcementEvent[] = [];
  const nameToId: Record<string, string> = {};
  if (options.report !== undefined) {
    events.push(
      ...admissionEventsFromReport(options.report, {
        ...(options.environmentId !== undefined ? { environmentId: options.environmentId } : {}),
      }),
    );
    // workflow NAME → PROJECT id, so runtime events (keyed by trace workflow name) normalize onto the
    // ids the API filters/validates against.
    for (const resolved of options.report.resolved_workflows) {
      if (resolved.workflow_name !== undefined && !Object.hasOwn(nameToId, resolved.workflow_name)) {
        nameToId[resolved.workflow_name] = resolved.workflow_id;
      }
    }
  }
  events.push(
    ...runtimeEventsFromTraces(options.readResult.traces, {
      ...(options.environmentId !== undefined ? { environmentId: options.environmentId } : {}),
      workflowNameToId: nameToId,
    }),
  );
  const filtered = filterEvents(events, {
    workflowIds: options.workflowIds,
    ...(options.environmentId !== undefined ? { environmentId: options.environmentId } : {}),
    verdicts: options.verdicts,
    policyIds: options.policyIds,
    since: options.since,
    until: options.until,
  });
  const ordered = sortEvents(filtered);
  const { page, nextCursor } = paginate(ordered, {
    offset: options.offset,
    limit: options.limit,
    fingerprint: options.cursorFingerprint,
  });
  return {
    events: page,
    partial: { langfuse: options.readResult.status },
    // `isoUtc` (Python `datetime.isoformat()`) — a `+00:00` offset and microsecond fractional part,
    // NOT `toISOString()`'s `…Z`/millisecond form, so the window echo matches the Python edition.
    since: isoUtc(options.since)!,
    until: isoUtc(options.until)!,
    ...(nextCursor !== undefined ? { next_cursor: nextCursor } : {}),
  };
}

/**
 * Whether the leading `YYYY-MM-DD` of an ISO string names a real calendar day. JS `Date` SILENTLY
 * normalizes an out-of-range day (`2026-02-30` → Mar 2) instead of failing, so a strict feed would
 * accept and echo a shifted window; Python's `datetime.fromisoformat("2026-02-30")` raises
 * `ValueError` → 422. (Out-of-range month/hour/minute already yield an Invalid Date in JS, so only
 * the day-of-month needs this guard.)
 */
function isoDateComponentsValid(value: string): boolean {
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(value);
  if (match === null) return true; // not a `YYYY-MM-DD…` shape — leave it to `toUtcDate`.
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  if (month < 1 || month > 12 || day < 1) return false;
  // `Date.UTC(year, month, 0)` (month 1-indexed here → 0-indexed next month, day 0) = last day of `month`.
  const daysInMonth = new Date(Date.UTC(year, month, 0)).getUTCDate();
  return day <= daysInMonth;
}

/**
 * Parse a caller-supplied window bound (`since`/`until` query) to an aware-UTC `Date`, or `null` for
 * an absent/empty/unparseable value. A NAIVE value is treated as UTC (the `_as_utc_aware` posture),
 * so a bare `2026-07-01` never drifts by the server's local offset. A rolled-over calendar date
 * (`2026-02-30`) is REJECTED (`null`) rather than silently shifted, matching Python's
 * `fromisoformat` `ValueError` → 422.
 */
export function parseWindowBound(value: string | null | undefined): Date | null {
  if (typeof value === "string" && !isoDateComponentsValid(value.trim())) return null;
  return toUtcDate(value);
}

// Re-export the check DTO type so callers importing the report shape find it alongside these helpers.
export type { ApiValidationCheck };
