import { describe, expect, it } from "vitest";

import type { EnforcementEvent } from "./api";
import {
  capabilityAbsentCopy,
  combinePartial,
  deriveEnforcementRows,
  deriveFeedRows,
  emptyFeedNotice,
  enforcementPageParams,
  enforcementQueryKey,
  langfuseBaseFromCells,
  partialNotice,
  resolvedWorkflowIdsInEnv,
  sourceBadgeTitle,
  verdictBadgeTitle,
  WINDOW_PRESETS,
  windowSince,
  type EnforcementFilters,
} from "./enforcementFeed";
import type { BundleCell } from "./queries";

function event(overrides: Partial<EnforcementEvent>): EnforcementEvent {
  return {
    source: "admission",
    rule: "policy_provider",
    verdict: "rejected",
    detail: "provider 'anthropic' is not allowed by selected project policy",
    policy_ids: ["base"],
    evidence: {},
    ...overrides,
  } as EnforcementEvent;
}

describe("windowSince", () => {
  it("computes the ISO bound for each preset from a fixed now", () => {
    const now = new Date("2026-07-18T12:00:00.000Z");
    expect(windowSince("24h", now)).toBe("2026-07-17T12:00:00.000Z");
    expect(windowSince("7d", now)).toBe("2026-07-11T12:00:00.000Z");
    expect(windowSince("30d", now)).toBe("2026-06-18T12:00:00.000Z");
  });

  it("covers every declared preset (a new preset must not silently fall back)", () => {
    const now = new Date("2026-07-18T12:00:00.000Z");
    for (const preset of WINDOW_PRESETS) {
      expect(windowSince(preset.id, now)).toBe(
        new Date(now.getTime() - preset.hours * 3_600_000).toISOString(),
      );
    }
  });
});

describe("combinePartial", () => {
  const page = (langfuse: "ok" | "unreachable" | "not_configured") => ({ partial: { langfuse } });

  it("is ok only when every page was ok", () => {
    expect(combinePartial([page("ok"), page("ok")])).toBe("ok");
  });

  it("is loudest-wins: any unreachable page marks the whole feed unreachable", () => {
    expect(combinePartial([page("ok"), page("unreachable"), page("not_configured")])).toBe(
      "unreachable",
    );
  });

  it("any not_configured page (without unreachable) marks the feed not_configured", () => {
    expect(combinePartial([page("ok"), page("not_configured")])).toBe("not_configured");
  });
});

describe("partialNotice", () => {
  it("is silent (null) when both sources were read in full", () => {
    expect(partialNotice("ok")).toBeNull();
  });

  it("warns loudly that runtime events may be MISSING when the observer is unreachable", () => {
    const notice = partialNotice("unreachable");
    expect(notice?.badgeKind).toBe("warning");
    expect(notice?.message).toMatch(/may be missing/);
    expect(notice?.message).toMatch(/Admission events are complete/);
  });

  it("states the runtime portion is not available when the observer is not langfuse", () => {
    const notice = partialNotice("not_configured");
    expect(notice?.badgeKind).toBe("neutral");
    expect(notice?.message).toMatch(/not available/);
    expect(notice?.message).toMatch(/admission verdicts only/);
  });
});

describe("emptyFeedNotice", () => {
  it("is a green all-clear ONLY when partial is ok", () => {
    const notice = emptyFeedNotice("ok");
    expect(notice.badgeKind).toBe("ok");
    expect(notice.message).toMatch(/No enforcement events in this window/);
  });

  it("scopes the confirmation to admission events when the runtime portion was partial", () => {
    for (const partial of ["unreachable", "not_configured"] as const) {
      const notice = emptyFeedNotice(partial);
      expect(notice.badgeKind).toBe("neutral");
      expect(notice.message).toMatch(/No admission enforcement events/);
    }
  });
});

describe("deriveEnforcementRows", () => {
  const input = { langfuseBase: "https://lf.example/project/p1", partialLangfuse: "ok" as const };

  it("renders an admission event with a null occurredAt and structured validation evidence", () => {
    const rows = deriveEnforcementRows([event({ workflow_id: "wf_a", environment_id: "local" })], input);
    expect(rows).toHaveLength(1);
    expect(rows[0].occurredAt).toBeNull();
    expect(rows[0].source).toBe("admission");
    expect(rows[0].verdict).toBe("rejected");
    expect(rows[0].rule).toBe("policy_provider");
    expect(rows[0].policyIds).toEqual(["base"]);
    expect(rows[0].workflowId).toBe("wf_a");
    // Structured link data only (#723 F3): the JSX builds the TanStack <Link> that
    // encodes workflow id + env — no hand-built href string lives on the row.
    expect(rows[0].evidence).toEqual({
      kind: "validation",
      workflowId: "wf_a",
      section: "validation",
    });
  });

  it("degrades an admission event without a workflow id to no evidence", () => {
    const rows = deriveEnforcementRows([event({ workflow_id: null })], input);
    expect(rows[0].workflowId).toBeNull();
    expect(rows[0].evidence).toEqual({ kind: "none" });
  });

  it("links a runtime event's trace when a trace id and langfuse base are known", () => {
    const rows = deriveEnforcementRows(
      [
        event({
          source: "runtime",
          verdict: "blocked",
          rule: "moderation.on_violation",
          occurred_at: "2026-07-17T10:00:00Z",
          workflow_id: "wf_a",
          execution_id: "run-1",
          evidence: { trace_id: "trace-9" },
        }),
      ],
      input,
    );
    expect(rows[0].occurredAt).toBe("2026-07-17T10:00:00Z");
    expect(rows[0].evidence).toEqual({
      kind: "trace",
      state: { kind: "link", href: "https://lf.example/project/p1/traces/trace-9" },
    });
  });

  it("degrades a runtime event without a trace id through the shared three-state (no-trace)", () => {
    const rows = deriveEnforcementRows(
      [event({ source: "runtime", occurred_at: "2026-07-17T10:00:00Z", evidence: {} })],
      input,
    );
    expect(rows[0].evidence).toEqual({ kind: "trace", state: { kind: "no-trace" } });
  });

  it("marks runtime evidence unreachable when the feed's partial marker says so", () => {
    const rows = deriveEnforcementRows(
      [event({ source: "runtime", occurred_at: "t", evidence: { trace_id: "trace-9" } })],
      { ...input, partialLangfuse: "unreachable" },
    );
    expect(rows[0].evidence).toEqual({ kind: "trace", state: { kind: "unreachable" } });
  });

  it("renders runtime evidence as not-traced when no langfuse base is known", () => {
    const rows = deriveEnforcementRows(
      [event({ source: "runtime", occurred_at: "t", evidence: { trace_id: "trace-9" } })],
      { ...input, langfuseBase: null },
    );
    expect(rows[0].evidence.kind).toBe("trace");
    expect(rows[0].evidence.kind === "trace" && rows[0].evidence.state.kind).toBe("none");
  });

  it("keys rows uniquely even for identical events (served order preserved)", () => {
    const rows = deriveEnforcementRows([event({ workflow_id: "wf_a" }), event({ workflow_id: "wf_a" })], input);
    expect(rows[0].key).not.toBe(rows[1].key);
  });
});

describe("deriveFeedRows", () => {
  const runtimeEvent = (overrides: Partial<EnforcementEvent> = {}) =>
    event({
      source: "runtime",
      verdict: "blocked",
      rule: "moderation.on_violation",
      occurred_at: "2026-07-17T10:00:00Z",
      workflow_id: "wf_a",
      evidence: { trace_id: "trace-9" },
      ...overrides,
    });

  it("derives each page with THAT page's partial: a later unreachable page never masks page-1 evidence", () => {
    const page1 = { events: [runtimeEvent()], partial: { langfuse: "ok" as const } };
    const page2 = { events: [runtimeEvent({ evidence: { trace_id: "trace-10" } })], partial: { langfuse: "unreachable" as const } };

    const rows = deriveFeedRows([page1, page2], { langfuseBase: "https://lf.example/project/p1" });

    // Page-1 runtime row KEEPS its working trace link (derived under its own ok partial)…
    expect(rows[0].evidence).toEqual({
      kind: "trace",
      state: { kind: "link", href: "https://lf.example/project/p1/traces/trace-9" },
    });
    // …while page-2's row degrades to the unreachable placeholder from its own partial.
    expect(rows[1].evidence).toEqual({ kind: "trace", state: { kind: "unreachable" } });
    // The feed-wide banner (loudest-wins) still reports unreachable for the whole feed.
    expect(combinePartial([page1, page2])).toBe("unreachable");
  });

  it("namespaces row keys per page so identical events across pages never collide", () => {
    const page = { events: [runtimeEvent()], partial: { langfuse: "ok" as const } };
    const rows = deriveFeedRows([page, { ...page }], { langfuseBase: null });
    expect(rows).toHaveLength(2);
    expect(rows[0].key).not.toBe(rows[1].key);
  });
});

describe("enforcement infinite-query identity", () => {
  const filters: EnforcementFilters = { workflowId: "wf_a", verdict: "blocked", window: "7d" };
  const since = "2026-07-11T12:00:00.000Z";

  // Mirrors filter_fingerprint in packages/python/.../project/enforcement.py, which fingerprints
  // {w: workflow_ids, e: environment_id, v: verdicts, p: policy_ids, s: since, u: until}. This
  // client varies only env/workflowId/verdict/since (never sends policy_ids; never sends until).
  it("keys on EXACTLY the varying fingerprinted fields: resource, env, workflow, verdict, since", () => {
    expect(enforcementQueryKey("local", filters, since)).toEqual([
      "enforcement-events",
      "local",
      "wf_a",
      "blocked",
      since,
    ]);
  });

  it("collapses null workflow/verdict to the empty-string the fingerprint's absent value maps to", () => {
    expect(enforcementQueryKey("local", { workflowId: null, verdict: null, window: "7d" }, since)).toEqual([
      "enforcement-events",
      "local",
      "",
      "",
      since,
    ]);
  });

  it("(load more) two sequential pages share identical since/workflow/verdict — only cursor varies", () => {
    const page1 = enforcementPageParams(filters, since, null);
    const page2 = enforcementPageParams(filters, since, "cursor-2");
    expect(page1).toEqual({ workflowId: "wf_a", verdict: "blocked", since, cursor: null });
    expect(page2).toEqual({ workflowId: "wf_a", verdict: "blocked", since, cursor: "cursor-2" });
    const { cursor: _c1, ...frozen1 } = page1;
    const { cursor: _c2, ...frozen2 } = page2;
    expect(frozen1).toEqual(frozen2);
  });

  it("(filter change) a changed filter + fresh since mints a NEW key — a fresh page stack", () => {
    const now1 = new Date("2026-07-18T12:00:00.000Z");
    const now2 = new Date("2026-07-18T12:05:00.000Z");
    // Re-picking any filter re-anchors `now` in the hook, so the frozen `since` moves…
    const sinceA = windowSince("7d", now1);
    const sinceB = windowSince("7d", now2);
    expect(sinceA).not.toBe(sinceB);
    // …and a changed verdict alone already produces a different key.
    const keyA = enforcementQueryKey("local", filters, sinceA);
    const keyB = enforcementQueryKey("local", { ...filters, verdict: "rejected" }, sinceB);
    expect(keyA).not.toEqual(keyB);
  });
});

describe("badge + panel copy", () => {
  it("computes the source badge tooltip (admission vs runtime)", () => {
    expect(sourceBadgeTitle("admission")).toMatch(/admission-time verdict/);
    expect(sourceBadgeTitle("runtime")).toMatch(/read from the Langfuse trace/);
  });

  it("computes the verdict badge tooltip (blocked vs rejected)", () => {
    expect(verdictBadgeTitle("blocked")).toMatch(/blocked at runtime/);
    expect(verdictBadgeTitle("rejected")).toMatch(/rejected at admission/);
  });

  it("computes the capability-absent panel copy around the enforcement_events token", () => {
    const copy = capabilityAbsentCopy();
    expect(copy.badge).toBe("not supported");
    expect(copy.capability).toBe("enforcement_events");
    expect(copy.before).toMatch(/not supported by this control plane/);
    expect(copy.after).toMatch(/validation issues on each/);
  });
});

describe("langfuseBaseFromCells", () => {
  const cell = (env: string, base: string | null, resolved = true): BundleCell =>
    ({
      workflowId: "wf",
      env,
      bundle: resolved ? ({ links: base ? { langfuse_project: base } : {} } as never) : undefined,
      pending: false,
      fetching: false,
      updatedAt: 0,
    }) as BundleCell;

  it("takes the first resolved bundle's base in the SELECTED environment only", () => {
    const cells = [cell("other", "https://wrong.example"), cell("local", "https://lf.example")];
    expect(langfuseBaseFromCells(cells, "local")).toBe("https://lf.example");
  });

  it("skips unresolved and base-less cells, and is null when nothing configures a base", () => {
    expect(
      langfuseBaseFromCells([cell("local", null, false), cell("local", null)], "local"),
    ).toBeNull();
  });
});

describe("resolvedWorkflowIdsInEnv", () => {
  const cell = (workflowId: string, env: string, resolved: boolean): BundleCell =>
    ({
      workflowId,
      env,
      bundle: resolved ? ({ links: {} } as never) : undefined,
      pending: false,
      fetching: false,
      updatedAt: 0,
    }) as BundleCell;

  it("keeps only workflows that resolve in the selected env, de-duplicated in matrix order", () => {
    const cells = [
      cell("wf_a", "local", true),
      cell("wf_a", "other", true),
      cell("wf_b", "local", false), // does not resolve in local → excluded
      cell("wf_c", "local", true),
    ];
    expect(resolvedWorkflowIdsInEnv(cells, "local")).toEqual(["wf_a", "wf_c"]);
  });

  it("is empty when nothing resolves in the env (e.g. still pending)", () => {
    expect(resolvedWorkflowIdsInEnv([cell("wf_a", "local", false)], "local")).toEqual([]);
  });
});
