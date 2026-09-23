import { describe, expect, it } from "vitest";

import { loadYamlSpec, workflowPlanFromSpec } from "../src/index.js";
// Interpreter internals — deliberately not part of the package surface (#493).
import {
  activityProxyOptions,
  CACHE_PREP_ACTIVITY_SUFFIX,
  CACHE_RELEASE_ACTIVITY_SUFFIX,
  cacheActivityProxyOptions,
  resolveContextPath,
  utf8ByteLength,
} from "../src/workflow-plan.js";
import {
  CACHE_PREP_ACTIVITY_SUFFIX as canonicalPrep,
  CACHE_RELEASE_ACTIVITY_SUFFIX as canonicalRelease,
} from "@typeflux/temporal";
import type { WorkflowPlan } from "../src/index.js";

const SPEC = `
project: p
name: n
task_queue: q
runtime:
  temporal: {}
  registry: { type: inline }
  provider: { type: openai }
activities: {}
workflow:
  name: W
  input: schemas:In
  steps:
    - id: review_evidence
      map:
        activity: review_evidence_item
        over: input.evidence
        concurrency: 3
        collect:
          output: schemas:Batch
          field: reviews
    - id: consolidate
      activity: consolidate_claim_review
`;

describe("workflowPlanFromSpec (#452)", () => {
  it("derives an activity + map step plan from the spec", () => {
    const plan = workflowPlanFromSpec(loadYamlSpec(SPEC));
    expect(plan.steps).toEqual([
      {
        kind: "map",
        id: "review_evidence",
        activity: "review_evidence_item",
        over: "input.evidence",
        concurrency: 3,
        collectField: "reviews",
        collectMaxBytes: 1_500_000, // the Python default guard (#495 PR-B)
      },
      { kind: "activity", id: "consolidate", activity: "consolidate_claim_review" },
    ]);
  });

  it("throws on a step with neither activity nor map", () => {
    // A bare `- id: consolidate` step passes the (strict) schema — activity/map are optional —
    // so the exactly-one check in workflowPlanFromSpec must catch it.
    const bad = SPEC.replace("    - id: consolidate\n      activity: consolidate_claim_review", "    - id: consolidate");
    expect(() => workflowPlanFromSpec(loadYamlSpec(bad))).toThrow(/exactly one of/);
  });

  it("rejects an unknown step key at load time (strict spec, #490)", () => {
    const bad = SPEC.replace("      activity: consolidate_claim_review", "      note: nothing");
    expect(() => loadYamlSpec(bad)).toThrow(/invalid Typeflux spec/);
  });

  it("throws on the reserved step id `input`", () => {
    const reserved = SPEC.replace("    - id: consolidate", "    - id: input");
    expect(() => workflowPlanFromSpec(loadYamlSpec(reserved))).toThrow(/reserved/);
  });

  it("throws on a duplicate step id", () => {
    const dup = SPEC.replace("    - id: consolidate", "    - id: review_evidence");
    expect(() => workflowPlanFromSpec(loadYamlSpec(dup))).toThrow(/duplicate workflow step id/);
  });

  it("throws on a step with both activity and map", () => {
    const both = SPEC.replace(
      "    - id: consolidate\n      activity: consolidate_claim_review",
      "    - id: consolidate\n      activity: consolidate_claim_review\n      map:\n        activity: x\n        over: input.y",
    );
    expect(() => workflowPlanFromSpec(loadYamlSpec(both))).toThrow(/exactly one of/);
  });
});

const TIMEOUT_SPEC = `
project: p
name: n
task_queue: q
runtime:
  temporal: {}
  registry: { type: inline }
  provider: { type: openai }
activities:
  definitions:
    - name: a_fast
      input: schemas:In
      output: schemas:Out
      prompt: p/x
      start_to_close_timeout_seconds: 30
      heartbeat_timeout_seconds: 5
    - name: a_default
      input: schemas:In
      output: schemas:Out
      prompt: p/x
workflow:
  name: W
  input: schemas:In
  steps:
    - id: s1
      activity: a_fast
    - id: s2
      activity: a_default
`;

// The @temporalio RetryPolicy shape activityProxyOptions emits for the bounded default (#486).
const DEFAULT_PROXY_RETRY = { maximumAttempts: 5, initialInterval: 1_000, maximumInterval: 60_000, backoffCoefficient: 2 };

describe("workflowPlanFromSpec activity timeouts (#479)", () => {
  it("maps a definition's start_to_close + heartbeat timeouts to per-activity millisecond options", () => {
    const plan = workflowPlanFromSpec(loadYamlSpec(TIMEOUT_SPEC));
    expect(plan.activityOptions?.["a_fast"]).toEqual({ startToCloseTimeoutMs: 30_000, heartbeatTimeoutMs: 5_000 });
    // a_default sets no timeout -> no entry (the interpreter applies the 2-minute default).
    expect(Object.keys(plan.activityOptions ?? {})).toEqual(["a_fast"]);
  });

  it("omits activityOptions entirely when no definition sets a timeout", () => {
    const noTimeouts = TIMEOUT_SPEC.replace("      start_to_close_timeout_seconds: 30\n", "").replace(
      "      heartbeat_timeout_seconds: 5\n",
      "",
    );
    expect(workflowPlanFromSpec(loadYamlSpec(noTimeouts)).activityOptions).toBeUndefined();
  });

  it("wires a heartbeat-only definition (the worker heartbeats the activity, #484)", () => {
    const heartbeatOnly = TIMEOUT_SPEC.replace("      start_to_close_timeout_seconds: 30\n", "");
    expect(workflowPlanFromSpec(loadYamlSpec(heartbeatOnly)).activityOptions?.["a_fast"]).toEqual({
      heartbeatTimeoutMs: 5_000,
    });
  });

  it("rounds a fractional-second timeout to milliseconds", () => {
    const frac = TIMEOUT_SPEC.replace("start_to_close_timeout_seconds: 30", "start_to_close_timeout_seconds: 1.5");
    expect(workflowPlanFromSpec(loadYamlSpec(frac)).activityOptions?.["a_fast"]?.startToCloseTimeoutMs).toBe(1_500);
  });

  it("clamps a sub-millisecond timeout to >=1ms so Temporal never drops the bound", () => {
    // 0.0004s rounds to 0ms; a 0 startToCloseTimeout is silently treated as "no timeout".
    const tiny = TIMEOUT_SPEC.replace("start_to_close_timeout_seconds: 30", "start_to_close_timeout_seconds: 0.0004");
    expect(workflowPlanFromSpec(loadYamlSpec(tiny)).activityOptions?.["a_fast"]?.startToCloseTimeoutMs).toBe(1);
  });

  it("throws on a timeout so large it overflows the millisecond range", () => {
    const huge = TIMEOUT_SPEC.replace("start_to_close_timeout_seconds: 30", "start_to_close_timeout_seconds: 1e308");
    expect(() => workflowPlanFromSpec(loadYamlSpec(huge))).toThrow(/too large to express as a millisecond duration/);
  });

  it("resolves a timeout for an activity literally named __proto__ across the JSON boundary", () => {
    // The plan crosses Temporal's data converter (JSON) before the interpreter reads it. A
    // __proto__-named activity must survive: JSON.parse materializes "__proto__" as a real own
    // property (no prototype pollution), so the Object.hasOwn lookup still resolves the override.
    const plan = workflowPlanFromSpec(loadYamlSpec(TIMEOUT_SPEC.replace(/a_fast/g, "__proto__")));
    const roundTripped = JSON.parse(JSON.stringify(plan)) as WorkflowPlan;
    expect(activityProxyOptions(roundTripped, "__proto__")).toEqual({
      startToCloseTimeout: 30_000,
      heartbeatTimeout: 5_000,
      retry: DEFAULT_PROXY_RETRY,
    });
  });
});

describe("activityProxyOptions (#479, #486)", () => {
  it("applies a configured start-to-close and heartbeat, with the default bounded retry", () => {
    const plan = workflowPlanFromSpec(loadYamlSpec(TIMEOUT_SPEC));
    expect(activityProxyOptions(plan, "a_fast")).toEqual({
      startToCloseTimeout: 30_000,
      heartbeatTimeout: 5_000,
      retry: DEFAULT_PROXY_RETRY,
    });
  });

  it("defaults start-to-close even when only a heartbeat is configured (parity with Python)", () => {
    const heartbeatOnly = TIMEOUT_SPEC.replace("      start_to_close_timeout_seconds: 30\n", "");
    const plan = workflowPlanFromSpec(loadYamlSpec(heartbeatOnly));
    expect(activityProxyOptions(plan, "a_fast")).toEqual({
      startToCloseTimeout: 120_000,
      heartbeatTimeout: 5_000,
      retry: DEFAULT_PROXY_RETRY,
    });
  });

  it("falls back to the 2-minute default + bounded retry for an activity with no override", () => {
    const plan = workflowPlanFromSpec(loadYamlSpec(TIMEOUT_SPEC));
    expect(activityProxyOptions(plan, "a_default")).toEqual({ startToCloseTimeout: 120_000, retry: DEFAULT_PROXY_RETRY });
  });

  it("returns the default for an activity absent from the override map, including inherited names", () => {
    // After Temporal's JSON round-trip the plan arrives with a normal-prototype activityOptions, so a
    // bare bracket lookup of an inherited name (`toString`) would read Object.prototype; the Object.hasOwn
    // guard must return the default instead. (A plain object literal models the post-serialization shape.)
    const plan: WorkflowPlan = { steps: [], activityOptions: { a_fast: { startToCloseTimeoutMs: 1_000 } } };
    expect(activityProxyOptions(plan, "toString")).toEqual({ startToCloseTimeout: 120_000, retry: DEFAULT_PROXY_RETRY });
    expect(activityProxyOptions(plan, "unknown")).toEqual({ startToCloseTimeout: 120_000, retry: DEFAULT_PROXY_RETRY });
  });
});

const RETRY_SPEC = `
project: p
name: n
task_queue: q
runtime:
  temporal: {}
  registry: { type: inline }
  provider: { type: openai }
  activity_retry:
    maximum_attempts: 3
    initial_interval_seconds: 2
    maximum_interval_seconds: 30
    backoff_coefficient: 1.5
activities:
  definitions:
    - name: a_spec
      input: schemas:In
      output: schemas:Out
      prompt: p/x
    - name: a_own
      input: schemas:In
      output: schemas:Out
      prompt: p/x
      retry: { maximum_attempts: 0, maximum_interval_seconds: null }
workflow:
  name: W
  input: schemas:In
  steps:
    - id: s1
      activity: a_spec
    - id: s2
      activity: a_own
`;

describe("activity retry policy (#486)", () => {
  it("applies the bounded default when no retry is configured anywhere", () => {
    const plan = workflowPlanFromSpec(loadYamlSpec(TIMEOUT_SPEC));
    expect(plan.retryPolicy).toBeUndefined(); // no runtime.activity_retry -> default applied at proxy time
    expect(activityProxyOptions(plan, "a_default").retry).toEqual(DEFAULT_PROXY_RETRY);
  });

  it("uses runtime.activity_retry for an activity without its own retry", () => {
    const plan = workflowPlanFromSpec(loadYamlSpec(RETRY_SPEC));
    expect(plan.retryPolicy).toEqual({
      maximumAttempts: 3,
      initialIntervalMs: 2_000,
      maximumIntervalMs: 30_000,
      backoffCoefficient: 1.5,
    });
    expect(activityProxyOptions(plan, "a_spec").retry).toEqual({
      maximumAttempts: 3,
      initialInterval: 2_000,
      maximumInterval: 30_000,
      backoffCoefficient: 1.5,
    });
  });

  it("a definition's own retry overrides runtime.activity_retry; unlimited sentinel + null max-interval map to omitted fields", () => {
    const plan = workflowPlanFromSpec(loadYamlSpec(RETRY_SPEC));
    // a_own sets maximum_attempts: 0 (unlimited) + null max-interval; other fields default. The plan
    // keeps the 0 sentinel, but the proxy form OMITS maximumAttempts (Temporal TS rejects <= 0 and
    // treats absent as unlimited) and omits maximumInterval (no cap).
    expect(plan.activityOptions?.["a_own"]?.retry?.maximumAttempts).toBe(0);
    expect(activityProxyOptions(plan, "a_own").retry).toEqual({
      initialInterval: 1_000, // default 1s
      backoffCoefficient: 2, // default
      // no maximumAttempts (unlimited), no maximumInterval (no cap)
    });
  });
});

describe("resolveContextPath (#452)", () => {
  const context = { input: { evidence: [1, 2, 3] }, review_evidence: { reviews: ["a", "b"] } };

  it("resolves a nested input path", () => {
    expect(resolveContextPath(context, "input.evidence")).toEqual([1, 2, 3]);
  });

  it("resolves a top-level context key", () => {
    expect(resolveContextPath(context, "input")).toEqual({ evidence: [1, 2, 3] });
  });

  it("resolves a prior step's collected field", () => {
    expect(resolveContextPath(context, "review_evidence.reviews")).toEqual(["a", "b"]);
  });

  it("throws when an intermediate value is not traversable", () => {
    expect(() => resolveContextPath(context, "input.evidence.missing.deep")).toThrow(/cannot resolve workflow path/);
  });

  it("throws on a missing key instead of returning undefined (Python KeyError parity)", () => {
    expect(() => resolveContextPath(context, "nope")).toThrow(/cannot resolve workflow path/);
    expect(() => resolveContextPath(context, "input.missing")).toThrow(/cannot resolve workflow path/);
  });

  it("does not resolve inherited prototype members", () => {
    expect(() => resolveContextPath(context, "input.toString")).toThrow(/cannot resolve workflow path/);
    expect(() => resolveContextPath(context, "input.constructor")).toThrow(/cannot resolve workflow path/);
  });
});

describe("session-cache plan wiring (#478 PR6)", () => {
  it("cacheActivityProxyOptions reuses timeout+retry but NEVER the heartbeat", () => {
    const plan: WorkflowPlan = {
      steps: [{ kind: "map", id: "s", activity: "a", over: "input.items" }],
      activityOptions: {
        a: { startToCloseTimeoutMs: 30_000, heartbeatTimeoutMs: 5_000, retry: { maximumAttempts: 2, initialIntervalMs: 100, backoffCoefficient: 2 } },
      },
    };
    const options = cacheActivityProxyOptions(plan, "a");
    expect(options.startToCloseTimeout).toBe(30_000);
    expect(options.retry.maximumAttempts).toBe(2);
    expect("heartbeatTimeout" in options).toBe(false);
  });

  it("pins the cache-activity suffixes to the canonical @typeflux/temporal strings", () => {
    // workflow-plan.ts duplicates the constants (the canonical module imports node:crypto,
    // which the workflow sandbox cannot load) — this test is the drift guard.
    expect(CACHE_PREP_ACTIVITY_SUFFIX).toBe(canonicalPrep);
    expect(CACHE_RELEASE_ACTIVITY_SUFFIX).toBe(canonicalRelease);
  });
});

describe("map-step sessionCache from the definition (#478 PR6)", () => {
  it("carries the activity's cache config on the map plan step (replay-visible)", () => {
    const spec = `
project: p
name: n
task_queue: q
runtime:
  temporal: {}
  registry: { type: inline }
  provider: { type: openai }
activities:
  definitions:
    - name: review_evidence_item
      input: schemas:In
      output: schemas:Out
      prompt: p/x
      cache: { enabled: true, ttl_seconds: 600 }
workflow:
  name: W
  input: schemas:In
  steps:
    - id: review_evidence
      map:
        activity: review_evidence_item
        over: input.evidence
`;
    const plan = workflowPlanFromSpec(loadYamlSpec(spec));
    expect(plan.steps[0]).toMatchObject({
      kind: "map",
      sessionCache: { enabled: true, ttlSeconds: 600 },
    });
    // No cache block -> no plan field (the interpreter skips prep entirely).
    const uncached = workflowPlanFromSpec(loadYamlSpec(spec.replace("      cache: { enabled: true, ttl_seconds: 600 }\n", "")));
    expect("sessionCache" in (uncached.steps[0] as object)).toBe(false);
  });
});

describe("collect.max_bytes on the plan (#495 PR-B)", () => {
  const specWith = (maxBytes: string) => `
project: p
name: n
task_queue: q
runtime:
  temporal: {}
  registry: { type: inline }
  provider: { type: openai }
activities: {}
workflow:
  name: W
  input: schemas:In
  steps:
    - id: s
      map:
        activity: a
        over: input.items
        collect: { output: schemas:Out, field: r${maxBytes} }
`;

  it("carries max_bytes; ABSENT defaults the guard ON at 1.5MB; 0 disables (Python parity)", () => {
    const bounded = workflowPlanFromSpec(loadYamlSpec(specWith(", max_bytes: 500000")));
    expect(bounded.steps[0]).toMatchObject({ kind: "map", collectMaxBytes: 500000 });
    const absent = workflowPlanFromSpec(loadYamlSpec(specWith("")));
    expect(absent.steps[0]).toMatchObject({ collectMaxBytes: 1_500_000 });
    const disabled = workflowPlanFromSpec(loadYamlSpec(specWith(", max_bytes: 0")));
    expect("collectMaxBytes" in (disabled.steps[0] as object)).toBe(false);
  });

  it("utf8ByteLength matches real UTF-8 byte counts incl. surrogate pairs", () => {
    expect(utf8ByteLength("abc")).toBe(3);
    expect(utf8ByteLength("é")).toBe(2);
    expect(utf8ByteLength("€")).toBe(3);
    expect(utf8ByteLength("👍")).toBe(4); // surrogate pair
    expect(utf8ByteLength('{"r":["ok","👍é€"]}')).toBe(Buffer.byteLength('{"r":["ok","👍é€"]}', "utf-8"));
  });
});
