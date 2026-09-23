import { describe, expect, it } from "vitest";

import type { InsightAnnotation, ProjectAnnotations } from "./api";
import {
  ANNOTATIONS_FILE_PATH,
  STALE_ACK_ID_PREFIX,
  acknowledgeSnippet,
  annotateInsights,
  isExpired,
  matchesInsightId,
  matchingAnnotation,
  partitionInsights,
  staleAckInsights,
  trackedInHref,
} from "./annotationsFeed";
import type { Insight, Severity } from "./insights";

function insight(id: string, severity: Severity = "warning"): Insight {
  return { id, severity, title: `title ${id}`, detail: `detail ${id}`, link: `#/${id}` };
}

function annotation(overrides: Partial<InsightAnnotation> & { insight_id_pattern: string }): InsightAnnotation {
  return { reason: "because", ...overrides };
}

describe("matchesInsightId", () => {
  it("matches an exact pattern only against the whole id", () => {
    expect(matchesInsightId("policy.drift.base", "policy.drift.base")).toBe(true);
    // Anchored: a pattern must cover the whole id, never a substring.
    expect(matchesInsightId("policy.drift", "policy.drift.base")).toBe(false);
    expect(matchesInsightId("drift.base", "policy.drift.base")).toBe(false);
  });

  it("treats `*` as any run of characters, including empty and dots", () => {
    expect(matchesInsightId("runtime.pin.*", "runtime.pin.exact")).toBe(true);
    expect(matchesInsightId("runtime.pin.*", "runtime.pin.a.b.c")).toBe(true);
    // `*` matches empty — the prefix alone satisfies the trailing wildcard.
    expect(matchesInsightId("runtime.pin.*", "runtime.pin.")).toBe(true);
    expect(matchesInsightId("*", "anything:at:all")).toBe(true);
    expect(matchesInsightId("wf:*:warn-mode", "wf:gate-a:warn-mode")).toBe(true);
    expect(matchesInsightId("wf:*:warn-mode", "wf:gate-a:other")).toBe(false);
  });

  it("keeps every other regex metacharacter literal", () => {
    // The dots are literal separators, not regex `any-char` — a different char must not match.
    expect(matchesInsightId("policy.drift.base", "policyXdriftXbase")).toBe(false);
    // `?` is NOT a wildcard (the backend documents `*` only) — it matches itself.
    expect(matchesInsightId("a?b", "a?b")).toBe(true);
    expect(matchesInsightId("a?b", "axb")).toBe(false);
  });
});

describe("matchingAnnotation", () => {
  it("returns the FIRST matching annotation in file order", () => {
    const annotations = [
      annotation({ insight_id_pattern: "runtime.pin.*", reason: "first" }),
      annotation({ insight_id_pattern: "runtime.pin.exact", reason: "second" }),
    ];
    expect(matchingAnnotation("runtime.pin.exact", annotations)?.reason).toBe("first");
  });

  it("returns undefined when nothing matches", () => {
    expect(matchingAnnotation("x", [annotation({ insight_id_pattern: "y" })])).toBeUndefined();
  });
});

describe("partitionInsights", () => {
  const now = new Date("2026-07-19T12:00:00Z");

  it("splits acknowledged out of active, preserving input order", () => {
    const insights = [insight("a", "critical"), insight("policy.drift.base"), insight("c", "info")];
    const annotations = [annotation({ insight_id_pattern: "policy.drift.*" })];
    const { active, acknowledged } = partitionInsights(insights, annotations, now);
    expect(active.map((i) => i.id)).toEqual(["a", "c"]);
    expect(acknowledged.map((a) => a.insight.id)).toEqual(["policy.drift.base"]);
    expect(acknowledged[0]?.annotation.insight_id_pattern).toBe("policy.drift.*");
  });

  it("does NOT collapse under an EXPIRED entry — its matches return to active", () => {
    const insights = [insight("runtime.pin.exact")];
    const annotations = [annotation({ insight_id_pattern: "runtime.pin.*", expires: "2000-01-01" })];
    const { active, acknowledged } = partitionInsights(insights, annotations, now);
    expect(active.map((i) => i.id)).toEqual(["runtime.pin.exact"]);
    expect(acknowledged).toHaveLength(0);
  });

  it("dual match (expired entry + live renewal) collapses under the LIVE entry's reason", () => {
    const insights = [insight("runtime.pin.exact")];
    const annotations = [
      annotation({ insight_id_pattern: "runtime.pin.*", reason: "old window", expires: "2000-01-01" }),
      annotation({ insight_id_pattern: "runtime.pin.*", reason: "renewed", expires: "2099-01-01" }),
    ];
    const { active, acknowledged } = partitionInsights(insights, annotations, now);
    expect(active).toHaveLength(0);
    // File order would pick the expired entry first; collapse-eligibility must skip it.
    expect(acknowledged[0]?.annotation.reason).toBe("renewed");
  });

  it("a broad live `*` pattern never collapses a derived stale-ack warning (meta-suppression)", () => {
    const staleWarning = insight(`${STALE_ACK_ID_PREFIX}other.pattern`);
    const insights = [staleWarning, insight("wf:policy:none")];
    const annotations = [annotation({ insight_id_pattern: "*", reason: "sweep" })];
    const { active, acknowledged } = partitionInsights(insights, annotations, now);
    // The ordinary insight collapses; the expiry signal itself must stay loud.
    expect(active.map((i) => i.id)).toEqual([staleWarning.id]);
    expect(acknowledged.map((a) => a.insight.id)).toEqual(["wf:policy:none"]);
  });

  it("leaves everything active with no annotations", () => {
    const insights = [insight("a"), insight("b")];
    expect(partitionInsights(insights, [], now).active).toHaveLength(2);
    expect(partitionInsights(insights, [], now).acknowledged).toHaveLength(0);
  });
});

describe("isExpired", () => {
  const now = new Date("2026-07-19T12:00:00Z");
  it("is true strictly before today (UTC calendar day)", () => {
    expect(isExpired("2026-01-01", now)).toBe(true);
    expect(isExpired("2026-07-18", now)).toBe(true);
  });
  it("is false on the expiry day itself and in the future", () => {
    // Valid THROUGH the expiry date — stale only the day after.
    expect(isExpired("2026-07-19", now)).toBe(false);
    expect(isExpired("2026-12-31", now)).toBe(false);
  });
  it("is false (never crashes) for a malformed date", () => {
    expect(isExpired("not-a-date", now)).toBe(false);
  });
});

describe("staleAckInsights", () => {
  const now = new Date("2026-07-19T12:00:00Z");

  it("derives a warning naming the entry + reason + file for each expired ack", () => {
    const annotations = [
      annotation({ insight_id_pattern: "runtime.pin.*", reason: "migration window", expires: "2026-01-01" }),
      annotation({ insight_id_pattern: "policy.drift.base", reason: "open", tracked_in: "https://x/1" }),
      annotation({ insight_id_pattern: "future.*", reason: "later", expires: "2027-01-01" }),
    ];
    const stale = staleAckInsights(annotations, now);
    expect(stale).toHaveLength(1);
    const [only] = stale;
    expect(only?.id).toBe(`${STALE_ACK_ID_PREFIX}runtime.pin.*`);
    expect(only?.severity).toBe("warning");
    expect(only?.title).toContain("runtime.pin.*");
    expect(only?.detail).toContain("2026-01-01");
    expect(only?.detail).toContain("migration window");
    expect(only?.detail).toContain(ANNOTATIONS_FILE_PATH);
    // The copy must state the corrected semantics: expiry un-collapses.
    expect(only?.detail).toContain("ACTIVE again");
  });

  it("dedups duplicate expired entries by pattern (no duplicate insight ids / React keys)", () => {
    const annotations = [
      annotation({ insight_id_pattern: "runtime.pin.*", reason: "first", expires: "2026-01-01" }),
      annotation({ insight_id_pattern: "runtime.pin.*", reason: "copy-paste slip", expires: "2025-06-01" }),
    ];
    const stale = staleAckInsights(annotations, now);
    expect(stale).toHaveLength(1);
    // File order wins: the first expired entry supplies the rendered reason/date.
    expect(stale[0]?.detail).toContain("first");
  });

  it("derives nothing when no ack has expired", () => {
    expect(staleAckInsights([annotation({ insight_id_pattern: "x" })], now)).toHaveLength(0);
  });
});

describe("acknowledgeSnippet", () => {
  it("builds a FULL valid file shape (top-level `annotations:` key) with a reason placeholder", () => {
    const snippet = acknowledgeSnippet("wf:policy:none");
    // A bare list item is malformed as a new file — the parser requires the mapping wrapper.
    expect(snippet).toContain("annotations:\n  - insight_id_pattern: 'wf:policy:none'\n    reason: <fill in>");
    expect(snippet).toContain(ANNOTATIONS_FILE_PATH);
  });

  it("single-quotes the id so YAML-special characters survive as scalars", () => {
    // `: ` would otherwise parse as a nested mapping; `'` must be escaped by doubling.
    expect(acknowledgeSnippet("a: b")).toContain("insight_id_pattern: 'a: b'");
    expect(acknowledgeSnippet("it's")).toContain("insight_id_pattern: 'it''s'");
    expect(acknowledgeSnippet("runtime.pin.*")).toContain("insight_id_pattern: 'runtime.pin.*'");
  });
});

describe("annotateInsights", () => {
  const now = new Date("2026-07-19T12:00:00Z");
  function projection(entries: InsightAnnotation[]): ProjectAnnotations {
    return { annotations: entries };
  }

  it("offers the acknowledge snippet on active findings, not on ok/derived rows", () => {
    const insights = [
      insight("wf:policy:none", "warning"),
      insight("wf:drain:ok", "ok"),
      insight(`${STALE_ACK_ID_PREFIX}runtime.pin.*`, "warning"),
    ];
    const feed = annotateInsights(insights, projection([]), now);
    const byId = Object.fromEntries(feed.active.map((row) => [row.insight.id, row.acknowledgeSnippet]));
    expect(byId["wf:policy:none"]).toBe(acknowledgeSnippet("wf:policy:none"));
    // No affordance for an all-clear (nothing to suppress) or for a stale-ack (acking an ack).
    expect(byId["wf:drain:ok"]).toBeUndefined();
    expect(byId[`${STALE_ACK_ID_PREFIX}runtime.pin.*`]).toBeUndefined();
    expect(feed.filePath).toBe(ANNOTATIONS_FILE_PATH);
  });

  it("partitions acknowledged out and pairs the annotation", () => {
    const insights = [insight("wf:policy:none"), insight("policy.drift.base")];
    const feed = annotateInsights(
      insights,
      projection([annotation({ insight_id_pattern: "policy.drift.*", reason: "tracked" })]),
      now,
    );
    expect(feed.active.map((r) => r.insight.id)).toEqual(["wf:policy:none"]);
    expect(feed.acknowledged).toHaveLength(1);
    expect(feed.acknowledged[0]?.annotation.reason).toBe("tracked");
  });

  it("degrades to all-active when annotations are absent (loading/error)", () => {
    const insights = [insight("a"), insight("b")];
    const feed = annotateInsights(insights, undefined, now);
    expect(feed.active).toHaveLength(2);
    expect(feed.acknowledged).toHaveLength(0);
    // The acknowledge affordance is still offered — it is how the first ack is authored.
    expect(feed.active.every((row) => row.acknowledgeSnippet !== undefined)).toBe(true);
  });
});

describe("trackedInHref", () => {
  it("passes http(s) tracker URLs through (any host)", () => {
    expect(trackedInHref("https://github.com/acme/x/issues/1")).toBe("https://github.com/acme/x/issues/1");
    expect(trackedInHref("http://jira.internal/browse/AB-1")).toBe("http://jira.internal/browse/AB-1");
  });
  it("rejects non-http schemes and malformed values (inert, never a live link)", () => {
    expect(trackedInHref("javascript:alert(1)")).toBeUndefined();
    expect(trackedInHref("ftp://x/y")).toBeUndefined();
    expect(trackedInHref("not a url")).toBeUndefined();
    expect(trackedInHref(null)).toBeUndefined();
    expect(trackedInHref(undefined)).toBeUndefined();
  });
});
