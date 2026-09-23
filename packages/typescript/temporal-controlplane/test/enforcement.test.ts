/**
 * Unit tests for the pure enforcement-events normalization helpers (#723 slice 2) — mirroring the
 * Python `test_project_enforcement.py` cases: admission normalization from a validation report,
 * runtime extraction from Langfuse traces, filtering/windowing/ordering, and cursor pagination.
 */

import { applyModeration } from "@typeflux/temporal";
import { describe, expect, it } from "vitest";

import {
  admissionEventsFromReport,
  buildEnforcementFeed,
  decodeCursor,
  DEFAULT_LIMIT,
  DEFAULT_WINDOW_MS,
  EnforcementCursorError,
  encodeCursor,
  filterEvents,
  filterFingerprint,
  isAdmissionEnforcementCode,
  observerFromReport,
  paginate,
  resolveWindow,
  runtimeEventsFromTraces,
  sortEvents,
  type EnforcementEvent,
  type EnforcementTraceRecord,
} from "../src/enforcement.js";
import type {
  ApiProjectValidationReport,
  ApiResolvedWorkflowValidation,
  ApiValidationCheck,
  ApiValidationIssue,
} from "../src/validation-dto.js";

// --- report builders -------------------------------------------------------

function report(options: {
  issues?: ApiValidationIssue[];
  resolved?: ApiResolvedWorkflowValidation[];
} = {}): ApiProjectValidationReport {
  return {
    project_name: "demo",
    manifest_path: "/tmp/typeflux.project.yaml",
    ok: (options.issues ?? []).length === 0,
    issues: options.issues ?? [],
    workflows: [],
    resolved_workflows: options.resolved ?? [],
  };
}

function resolved(options: {
  workflowId?: string;
  environmentId?: string;
  workflowName?: string;
  checks: ApiValidationCheck[];
}): ApiResolvedWorkflowValidation {
  return {
    workflow_id: options.workflowId ?? "workflow",
    environment_id: options.environmentId ?? "prod",
    ok: false,
    ...(options.workflowName !== undefined ? { workflow_name: options.workflowName } : {}),
    checks: options.checks,
  };
}

const check = (code: string, status: ApiValidationCheck["status"], extra: Partial<ApiValidationCheck> = {}): ApiValidationCheck => ({
  code,
  status,
  details: {},
  ...extra,
});

// --- admission normalization -----------------------------------------------

describe("isAdmissionEnforcementCode — explicit verdict sets, never substrings", () => {
  it("accepts genuine policy/admission verdict codes", () => {
    for (const code of ["policy_provider", "policy_risk_tier", "risk_tier_binding", "policy_selection", "admission_unknown_workflow", "admission_policy_selection"]) {
      expect(isAdmissionEnforcementCode(code)).toBe(true);
    }
  });

  it("rejects composition/authoring/config errors even when they contain 'policy'/'admission'", () => {
    for (const code of [
      "policy_composition",
      "invalid_policy_composition",
      "invalid_target_policy_composition",
      "admission_spec_shape",
      "admission_external_modules_forbidden",
      "duplicate_workflow_name",
      "unknown_profile_reference",
      "unknown_validation_policy",
      "policy_enforcement",
      "resolved_policy_composition_failed",
    ]) {
      expect(isAdmissionEnforcementCode(code)).toBe(false);
    }
  });
});

describe("admissionEventsFromReport", () => {
  it("lifts a failed policy check into a rejected admission event with recorded provenance", () => {
    const events = admissionEventsFromReport(
      report({
        resolved: [
          resolved({
            checks: [
              check("environment_workflow_resolution", "passed"),
              check("policy_selection", "passed", { details: { applied_policy_ids: ["base", "regulated"] } }),
              check("policy_provider", "failed", { message: "model claude-x not in allowlist" }),
              check("workflow_graph", "skipped"),
            ],
          }),
        ],
      }),
      { environmentId: "prod" },
    );
    expect(events).toHaveLength(1);
    const event = events[0]!;
    expect(event.source).toBe("admission");
    expect(event.verdict).toBe("rejected");
    expect(event.rule).toBe("policy_provider");
    expect(event.workflow_id).toBe("workflow");
    expect(event.environment_id).toBe("prod");
    // policy_ids is the RECORDED applied provenance, NOT a caller filter.
    expect(event.policy_ids).toEqual(["base", "regulated"]);
    expect(event.detail).toBe("model claude-x not in allowlist");
    expect(event.occurred_at).toBeUndefined(); // admission reflects current state, not a moment.
  });

  it("an EMPTY-string check message takes the fallback detail (Python `or`, not `??`)", () => {
    const events = admissionEventsFromReport(
      report({ resolved: [resolved({ checks: [check("policy_provider", "failed", { message: "" })] })] }),
    );
    // `||` semantics: a blank message must not surface as an empty detail.
    expect(events[0]!.detail).toBe("admission rejected: policy_provider");
  });

  it("drops policy_ids when there is no recorded policy_selection (never a caller filter)", () => {
    const events = admissionEventsFromReport(
      report({ resolved: [resolved({ checks: [check("policy_secrets", "failed", { message: "secret X" })] })] }),
    );
    expect(events[0]!.policy_ids).toEqual([]);
  });

  it("excludes composition errors and NEVER mislabels a policy-id reference as a workflow id", () => {
    const events = admissionEventsFromReport(
      report({
        resolved: [resolved({ checks: [check("policy_secrets", "failed", { message: "secret X" })] })],
        issues: [
          // The lifted resolved_* form is skipped (already normalized from the check above).
          { code: "resolved_policy_secrets_failed", message: "secret X", reference: "prod:workflow" },
          { code: "duplicate_workflow_name", message: "dup" },
          // reference="base" is a POLICY id, not a workflow id — excluded, never surfaced as workflow_id.
          { code: "invalid_policy_composition", message: "base conflicts with regulated", reference: "base" },
        ],
      }),
    );
    expect(events.map((e) => e.rule).sort()).toEqual(["policy_secrets"]);
    expect(events.every((e) => e.workflow_id !== "base")).toBe(true);
  });

  it("normalizes a top-level enforcement issue, splitting `env:workflow` and dropping provenance", () => {
    const events = admissionEventsFromReport(
      report({ issues: [{ code: "admission_unknown_workflow", message: "no such slot", reference: "prod:orphan" }] }),
    );
    expect(events).toHaveLength(1);
    expect(events[0]).toMatchObject({ workflow_id: "orphan", environment_id: "prod", policy_ids: [], rule: "admission_unknown_workflow" });
  });
});

describe("observerFromReport", () => {
  it("reads the observability_config check's type, else undefined", () => {
    expect(
      observerFromReport(report({ resolved: [resolved({ checks: [check("observability_config", "passed", { details: { type: "langfuse" } })] })] })),
    ).toBe("langfuse");
    expect(observerFromReport(report())).toBeUndefined();
  });
});

// --- runtime extraction ----------------------------------------------------

function runtimeTrace(options: {
  traceId: string;
  workflowName?: string;
  environment?: string;
  observations: EnforcementTraceRecord["observations"];
}): EnforcementTraceRecord {
  return {
    traceId: options.traceId,
    timestamp: "2026-07-01T12:00:00+00:00",
    workflowName: options.workflowName ?? "SupportWorkflow",
    workflowId: `${options.traceId}-exec`,
    environment: options.environment ?? "prod",
    appliedPolicyIds: [],
    observations: options.observations,
  };
}

describe("runtimeEventsFromTraces", () => {
  it("turns a moderation block into a blocked runtime event", () => {
    const events = runtimeEventsFromTraces([
      runtimeTrace({
        traceId: "trace-mod",
        observations: [
          { startTime: "2026-07-01T12:00:05+00:00", metadata: { typeflux_moderation: { decision: "block", categories: ["hate", "violence"], max_score: 0.9 } } },
          // A passed moderation verdict is NOT an enforcement event.
          { metadata: { typeflux_moderation: { decision: "allow", categories: [] } } },
        ],
      }),
    ]);
    expect(events).toHaveLength(1);
    const event = events[0]!;
    expect(event.source).toBe("runtime");
    expect(event.verdict).toBe("blocked");
    expect(event.rule).toBe("moderation.on_violation.block");
    expect(event.execution_id).toBe("trace-mod-exec");
    expect(event.workflow_id).toBe("SupportWorkflow");
    expect(event.environment_id).toBe("prod");
    expect(event.evidence.trace_id).toBe("trace-mod");
    expect(event.detail).toContain("hate, violence");
    expect(event.occurred_at).toBe("2026-07-01T12:00:05+00:00");
  });

  it("extracts nothing from an errored lifecycle-signal span (no honest review-rejection marker)", () => {
    const events = runtimeEventsFromTraces([
      runtimeTrace({ traceId: "trace-review", observations: [{ metadata: { level: "ERROR", name: "TypefluxLifecycleSignal:review" } }] }),
    ]);
    expect(events).toEqual([]);
  });

  it("ignores non-enforcement observations", () => {
    const events = runtimeEventsFromTraces([
      runtimeTrace({ traceId: "trace-clean", observations: [{ metadata: {} }, { metadata: { typeflux_moderation: { decision: "flag", categories: ["x"] } } }] }),
    ]);
    expect(events).toEqual([]);
  });

  it("normalizes the trace workflow NAME to the project id, falling back to the raw name", () => {
    const events = runtimeEventsFromTraces(
      [
        runtimeTrace({ traceId: "trace-mapped", workflowName: "SupportWorkflow", observations: [{ metadata: { typeflux_moderation: { decision: "block", categories: ["hate"] } } }] }),
        runtimeTrace({ traceId: "trace-unmapped", workflowName: "RenamedWorkflow", observations: [{ metadata: { typeflux_moderation: { decision: "block", categories: ["hate"] } } }] }),
      ],
      { workflowNameToId: { SupportWorkflow: "support" } },
    );
    const byExec = Object.fromEntries(events.map((e) => [e.execution_id, e.workflow_id]));
    expect(byExec["trace-mapped-exec"]).toBe("support"); // name → project id
    expect(byExec["trace-unmapped-exec"]).toBe("RenamedWorkflow"); // fallback to raw name
  });

  it("normalizes a naive reader timestamp to UTC (never a local-offset drift) and keeps it in-window", () => {
    const events = runtimeEventsFromTraces([
      runtimeTrace({ traceId: "trace-naive", observations: [{ startTime: "2026-07-03T12:00:00", metadata: { typeflux_moderation: { decision: "block", categories: ["hate"] } } }] }),
    ]);
    expect(events[0]!.occurred_at).toBe("2026-07-03T12:00:00+00:00");
    const kept = filterEvents(events, { since: new Date("2026-07-01T00:00:00Z"), until: new Date("2026-07-08T00:00:00Z") });
    expect(kept).toHaveLength(1);
  });

  it("preserves SUB-SECOND precision (a same-second block is not lost, ordering intra-second holds)", () => {
    const events = runtimeEventsFromTraces([
      runtimeTrace({ traceId: "trace-ms", observations: [{ startTime: "2026-07-01T12:00:00.500+00:00", metadata: { typeflux_moderation: { decision: "block", categories: ["x"] } } }] }),
    ]);
    // Millis retained and rendered as Python-isoformat microseconds — NOT truncated to whole seconds.
    expect(events[0]!.occurred_at).toBe("2026-07-01T12:00:00.500000+00:00");
    // since=12:00:00.250 keeps the .500 event; a whole-second strip would have compared 12:00:00 and dropped it.
    expect(filterEvents(events, { since: new Date("2026-07-01T12:00:00.250Z"), until: new Date("2026-07-01T12:00:01Z") })).toHaveLength(1);
    // since=12:00:00.750 drops it — proving the boundary is compared at full precision, not the whole second.
    expect(filterEvents(events, { since: new Date("2026-07-01T12:00:00.750Z"), until: new Date("2026-07-01T12:00:01Z") })).toHaveLength(0);
  });
});

describe("writer → reader round-trip (#723): the reader matches the writer's REAL output", () => {
  it("feeds the actual typeflux_moderation verdict the worker writes through the extractor", async () => {
    // Produce the EXACT verdict shape `execute.ts` (~825) records via
    // `observation.updateMetadata({ typeflux_moderation: verdict })` — capture it from `applyModeration`
    // itself, so the reader is validated against the writer's real output, not a hand-built fixture.
    let recorded: Record<string, unknown> | undefined;
    await expect(
      applyModeration(
        "assess",
        { moderator: async () => ({ flagged: true, categories: ["hate", "violence"], maxScore: 0.91 }), onViolation: "block" },
        "unsafe output",
        undefined,
        (verdict) => {
          recorded = verdict as unknown as Record<string, unknown>;
        },
      ),
    ).rejects.toThrow();
    expect(recorded).toMatchObject({ decision: "block", categories: ["hate", "violence"], max_score: 0.91 });
    const events = runtimeEventsFromTraces([
      {
        traceId: "trace-rt",
        timestamp: "2026-07-01T12:00:00+00:00",
        workflowName: "W",
        workflowId: "exec-1",
        environment: "prod",
        appliedPolicyIds: [],
        observations: [{ startTime: "2026-07-01T12:00:01+00:00", metadata: { typeflux_moderation: recorded! } }],
      },
    ]);
    expect(events).toHaveLength(1);
    expect(events[0]).toMatchObject({
      source: "runtime",
      verdict: "blocked",
      rule: "moderation.on_violation.block",
      execution_id: "exec-1",
    });
    expect(events[0]!.detail).toContain("hate, violence");
  });
});

// --- filtering / ordering / windowing / pagination -------------------------

const event = (overrides: Partial<EnforcementEvent> = {}): EnforcementEvent => ({
  source: "admission",
  rule: "policy_provider",
  verdict: "rejected",
  policy_ids: [],
  evidence: {},
  detail: "d",
  ...overrides,
});

describe("filterEvents", () => {
  it("filters by workflow, environment, and verdict", () => {
    const events = [
      event({ workflow_id: "a", environment_id: "prod", verdict: "rejected" }),
      event({ workflow_id: "b", environment_id: "prod", verdict: "blocked", source: "runtime" }),
      event({ workflow_id: "a", environment_id: "staging", verdict: "rejected" }),
    ];
    expect(filterEvents(events, { workflowIds: ["a"] }).map((e) => e.workflow_id)).toEqual(["a", "a"]);
    expect(filterEvents(events, { environmentId: "prod" }).map((e) => e.environment_id)).toEqual(["prod", "prod"]);
    expect(filterEvents(events, { verdicts: ["blocked"] }).map((e) => e.verdict)).toEqual(["blocked"]);
  });

  it("filters by policy_id via recorded-provenance intersection (admission + runtime alike)", () => {
    const events = [
      event({ workflow_id: "a", policy_ids: ["base"], source: "admission" }),
      event({ workflow_id: "b", policy_ids: ["base", "regulated"], source: "runtime", verdict: "blocked" }),
      event({ workflow_id: "c", policy_ids: ["other"], source: "admission" }),
      event({ workflow_id: "d", policy_ids: [], source: "admission" }),
    ];
    expect(filterEvents(events, { policyIds: ["regulated"] }).map((e) => e.workflow_id)).toEqual(["b"]);
    expect(filterEvents(events, { policyIds: ["base"] }).map((e) => e.workflow_id).sort()).toEqual(["a", "b"]);
  });

  it("drops out-of-window runtime events but always keeps admission events (current state)", () => {
    const since = new Date("2026-07-01T00:00:00Z");
    const until = new Date("2026-07-08T00:00:00Z");
    const inWindow = event({ source: "runtime", verdict: "blocked", occurred_at: "2026-07-03T00:00:00+00:00" });
    const outWindow = event({ source: "runtime", verdict: "blocked", occurred_at: "2026-06-01T00:00:00+00:00" });
    const admission = event(); // no occurred_at → always in-window
    const kept = filterEvents([inWindow, outWindow, admission], { since, until });
    expect(kept).toContain(inWindow);
    expect(kept).toContain(admission);
    expect(kept).not.toContain(outWindow);
  });
});

describe("sortEvents", () => {
  it("orders newest-first with admission (undated) ahead of dated runtime events", () => {
    const older = event({ source: "runtime", verdict: "blocked", occurred_at: "2026-07-01T00:00:00+00:00" });
    const newer = event({ source: "runtime", verdict: "blocked", occurred_at: "2026-07-05T00:00:00+00:00" });
    const admission = event();
    expect(sortEvents([older, admission, newer])).toEqual([admission, newer, older]);
  });
});

describe("resolveWindow", () => {
  it("defaults to the trailing seven days", () => {
    const now = new Date("2026-07-08T00:00:00Z");
    const { since, until } = resolveWindow(undefined, undefined, now);
    expect(until).toEqual(now);
    expect(since).toEqual(new Date(now.getTime() - DEFAULT_WINDOW_MS));
    expect(DEFAULT_WINDOW_MS).toBe(7 * 24 * 60 * 60 * 1000);
  });
});

describe("cursor + fingerprint", () => {
  const fp = (overrides: Parameters<typeof filterFingerprint>[0] = {}) =>
    filterFingerprint({ since: new Date("2026-07-01T00:00:00Z"), until: new Date("2026-07-08T00:00:00Z"), ...overrides });

  it("round-trips and rejects garbage", () => {
    const f = fp();
    expect(decodeCursor(undefined, f)).toBe(0);
    expect(decodeCursor(encodeCursor(40, f), f)).toBe(40);
    expect(() => decodeCursor("!!!not-base64!!!", f)).toThrow(EnforcementCursorError);
  });

  it("binds the cursor to the filter set — a mismatch is rejected, matching filters round-trip", () => {
    const minted = encodeCursor(20, fp({ verdicts: ["blocked"] }));
    expect(() => decodeCursor(minted, fp({ verdicts: ["rejected"] }))).toThrow(EnforcementCursorError);
    expect(decodeCursor(minted, fp({ verdicts: ["blocked"] }))).toBe(20);
  });

  it("fingerprints an unpinned window stably (so pagination survives now() advancing)", () => {
    expect(filterFingerprint({ environmentId: "prod" })).toBe(filterFingerprint({ environmentId: "prod" }));
  });
});

describe("paginate", () => {
  it("emits a next cursor only when more remain", () => {
    const f = filterFingerprint({ environmentId: "prod" });
    const events = Array.from({ length: 5 }, (_, i) => event({ workflow_id: String(i) }));
    const first = paginate(events, { offset: 0, limit: 2, fingerprint: f });
    expect(first.page.map((e) => e.workflow_id)).toEqual(["0", "1"]);
    expect(decodeCursor(first.nextCursor, f)).toBe(2);
    const last = paginate(events, { offset: 4, limit: 2, fingerprint: f });
    expect(last.page.map((e) => e.workflow_id)).toEqual(["4"]);
    expect(last.nextCursor).toBeUndefined();
  });
});

// --- feed composition ------------------------------------------------------

describe("buildEnforcementFeed", () => {
  const admissionReport = report({ resolved: [resolved({ checks: [check("policy_provider", "failed", { message: "blocked" })] })] });

  it("merges both sources and reports the partial marker", () => {
    const feed = buildEnforcementFeed({
      report: admissionReport,
      readResult: {
        status: "ok",
        traces: [runtimeTrace({ traceId: "trace-mod", observations: [{ startTime: "2026-07-03T00:00:00+00:00", metadata: { typeflux_moderation: { decision: "block", categories: ["hate"] } } }] })],
      },
      environmentId: "prod",
      policyIds: [],
      workflowIds: [],
      verdicts: [],
      since: new Date("2026-07-01T00:00:00Z"),
      until: new Date("2026-07-08T00:00:00Z"),
      limit: DEFAULT_LIMIT,
      offset: 0,
      cursorFingerprint: "fp",
    });
    expect(feed.partial.langfuse).toBe("ok");
    expect(new Set(feed.events.map((e) => e.source))).toEqual(new Set(["admission", "runtime"]));
    expect(feed.next_cursor).toBeUndefined();
    // The window echo uses Python's `isoformat` (`+00:00` offset, fractional part only when non-zero),
    // NOT `toISOString()`'s `…Z`/millisecond form — parity with the Python edition's envelope.
    expect(feed.since).toBe("2026-07-01T00:00:00+00:00");
    expect(feed.until).toBe("2026-07-08T00:00:00+00:00");
  });

  it("still serves admission events when the runtime source is not_configured", () => {
    const feed = buildEnforcementFeed({
      report: admissionReport,
      readResult: { status: "not_configured", traces: [] },
      environmentId: "prod",
      policyIds: [],
      workflowIds: [],
      verdicts: [],
      since: new Date("2026-07-01T00:00:00Z"),
      until: new Date("2026-07-08T00:00:00Z"),
      limit: DEFAULT_LIMIT,
      offset: 0,
      cursorFingerprint: "fp",
    });
    expect(feed.partial.langfuse).toBe("not_configured");
    expect(feed.events).toHaveLength(1);
    expect(feed.events[0]!.source).toBe("admission");
  });
});
