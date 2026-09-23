/**
 * Insight-acknowledgement engine (#733 slice 3, #577): the PURE layer that turns the served
 * `.typeflux/annotations.yaml` projection into a rendered insight feed — matching insight ids
 * against the ack patterns, partitioning a feed into active vs acknowledged, deriving the "stale
 * ack" warning for an expired entry, and building the copyable authoring snippet. All copy and all
 * logic live here (the tested layer); {@link InsightList} is a thin renderer over
 * {@link annotateInsights}.
 *
 * The maintainer decision (#577) keeps ack state IN THE REPO — PR-reviewed, git the single source of
 * truth. The console never mutates it: the acknowledge affordance shows the file path + a snippet an
 * author commits. Expiry un-collapses: once an entry's `expires` date passes, its matches return to
 * the active feed AND a loud stale-ack warning names the entry — an ack cannot rot silently, and an
 * expired one cannot keep suppressing (review-hardened, #733).
 */

import type { InsightAnnotation, ProjectAnnotations } from "./api";
import type { Insight } from "./insights";

/** The annotations file's project-root-relative path (Python `annotations_path`: the `.typeflux/`
 * dir beside the manifest). The console never resolves the manifest's directory, so it shows this
 * stable relative location in the acknowledge affordance — the file an author edits. */
export const ANNOTATIONS_FILE_PATH = ".typeflux/annotations.yaml";

/** Stable id prefix for the derived "stale ack" insights, so the acknowledge affordance recognizes
 * them and suppresses itself — you never acknowledge an acknowledgement. */
export const STALE_ACK_ID_PREFIX = "annotation:stale:";

/**
 * Whether an insight id matches an `insight_id_pattern` — the semantics the backend documents
 * (`project/annotations.py`): an EXACT id, or a glob whose only wildcard is `*`, matching any run of
 * characters (including empty and `.`); every other character is literal. `?` is NOT a wildcard
 * (the backend documents `*` only), so it matches itself. Anchored full-string — a pattern must
 * cover the whole id, never a substring.
 */
export function matchesInsightId(pattern: string, insightId: string): boolean {
  // Escape every regex metacharacter, then re-open `*` as `.*` — so the pattern's literal text
  // stays literal and only `*` is a wildcard.
  const escaped = pattern.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp(`^${escaped.replace(/\\\*/g, ".*")}$`).test(insightId);
}

/** The FIRST annotation (file order) whose pattern matches this insight id, or undefined. File
 * order is the tiebreak the projection preserves, so the reason/tracking shown is deterministic. */
export function matchingAnnotation(
  insightId: string,
  annotations: readonly InsightAnnotation[],
): InsightAnnotation | undefined {
  return annotations.find((annotation) => matchesInsightId(annotation.insight_id_pattern, insightId));
}

/** One acknowledged insight paired with the annotation that acknowledged it (its reason + tracking
 * drive the collapsed-disclosure rendering). */
export interface AcknowledgedInsight {
  insight: Insight;
  annotation: InsightAnnotation;
}

/**
 * Split a feed into the insights that stay active and those an annotation acknowledges — in input
 * order (the caller has already severity-sorted). Two review-hardened invariants (#733):
 *
 * - Only a NON-EXPIRED entry collapses. An expired ack's window has closed — its matches come
 *   BACK into the active feed (the whole point of `expires`), and {@link staleAckInsights}
 *   explains why. A dual match (an old expired entry + its live renewal) therefore always shows
 *   the live entry's reason, never the stale one's.
 * - A derived stale-ack warning (id `annotation:stale:…`) is NEVER collapsible: a broad live
 *   pattern like `*` must not suppress the very signal that an ack expired (meta-suppression).
 */
export function partitionInsights(
  insights: readonly Insight[],
  annotations: readonly InsightAnnotation[],
  now: Date,
): { active: Insight[]; acknowledged: AcknowledgedInsight[] } {
  const live = annotations.filter(
    (annotation) => !annotation.expires || !isExpired(annotation.expires, now),
  );
  const active: Insight[] = [];
  const acknowledged: AcknowledgedInsight[] = [];
  for (const insight of insights) {
    const annotation = insight.id.startsWith(STALE_ACK_ID_PREFIX)
      ? undefined
      : matchingAnnotation(insight.id, live);
    if (annotation) acknowledged.push({ insight, annotation });
    else active.push(insight);
  }
  return { active, acknowledged };
}

/**
 * Whether an `expires` date (a plain ISO `YYYY-MM-DD` string the CP serves) has passed as of `now`
 * — a calendar-day comparison at UTC midnight, so the verdict doesn't wobble with the viewer's
 * timezone or clock time. Strictly before today: an ack valid THROUGH its expiry date is stale only
 * the day after. Defensive against a malformed date (returns false — the CP shape-validates it, but
 * the client never trusts blindly).
 */
export function isExpired(expires: string, now: Date): boolean {
  const expiry = Date.parse(`${expires}T00:00:00Z`);
  if (Number.isNaN(expiry)) return false;
  return expiry < Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate());
}

/**
 * The warning-severity insights for every EXPIRED annotation (#733): a stale ack renders as its own
 * finding naming the entry, its reason, and the file to edit — so an ack whose window has closed is
 * loud, not silently still-suppressing. Joins the active feed (never collapsed). Ordered by input
 * (file order). `now` is injected for testability.
 */
export function staleAckInsights(annotations: readonly InsightAnnotation[], now: Date): Insight[] {
  const stale: Insight[] = [];
  const seen = new Set<string>();
  for (const annotation of annotations) {
    if (!annotation.expires || !isExpired(annotation.expires, now)) continue;
    // Dedup by pattern: duplicate expired entries (a copy-paste renewal slip) must not mint
    // duplicate insight ids (React key collisions, non-deterministic rendering).
    if (seen.has(annotation.insight_id_pattern)) continue;
    seen.add(annotation.insight_id_pattern);
    stale.push({
      id: `${STALE_ACK_ID_PREFIX}${annotation.insight_id_pattern}`,
      severity: "warning",
      title: `Acknowledgement expired: ${annotation.insight_id_pattern}`,
      detail:
        `This insight acknowledgement expired on ${annotation.expires} — its reason was ` +
        `"${annotation.reason}". Its matches are ACTIVE again (an expired ack no longer ` +
        `collapses anything). Renew the 'expires' date or remove the entry in ` +
        `${ANNOTATIONS_FILE_PATH}.`,
      link: "#",
    });
  }
  return stale;
}

/** The copyable YAML an author pastes into the annotations file to acknowledge an insight (#733):
 * the FULL valid file shape (the parser requires a top-level `annotations:` mapping — a bare list
 * item pasted into a NEW file is malformed, codex), with a note for the append-to-existing case.
 * The id is single-quoted (with '' escaping) so ids carrying YAML-special characters (`: `, `*`,
 * `#`, `&`) survive as scalars. The console never writes the file — show, don't do. */
export function acknowledgeSnippet(insightId: string): string {
  const quoted = `'${insightId.replaceAll("'", "''")}'`;
  return (
    `# ${ANNOTATIONS_FILE_PATH} — append the list item under the existing 'annotations:' key,\n` +
    `# or start the file with exactly this content:\n` +
    `annotations:\n  - insight_id_pattern: ${quoted}\n    reason: <fill in>`
  );
}

/** Whether an active insight should offer the "acknowledge…" affordance: NOT an `ok` insight (there
 * is nothing to suppress in an all-clear), and NOT a derived stale-ack insight (acknowledging an
 * acknowledgement is nonsensical). */
function isAcknowledgeable(insight: Insight): boolean {
  return insight.severity !== "ok" && !insight.id.startsWith(STALE_ACK_ID_PREFIX);
}

/** One row of the active feed: the insight plus, when offering one makes sense, the copyable
 * acknowledge snippet. */
export interface ActiveInsightRow {
  insight: Insight;
  acknowledgeSnippet?: string;
}

/** The rendered feed: active rows (with their acknowledge affordance), the acknowledged entries
 * (collapsed under a disclosure), and the file path the affordance cites. */
export interface AnnotatedFeed {
  active: ActiveInsightRow[];
  acknowledged: AcknowledgedInsight[];
  filePath: string;
}

/**
 * Turn a severity-sorted insight feed + the served annotations into the rendered feed: acknowledged
 * insights partitioned out (collapsed, never hidden), each remaining active insight carrying its
 * acknowledge snippet. `undefined`/absent annotations (loading, error, or a project with no file)
 * degrade to all-active — the acknowledge affordance still shows (it is how you CREATE the first
 * ack), so a feed never depends on the annotations read having landed.
 */
export function annotateInsights(
  insights: readonly Insight[],
  annotations: ProjectAnnotations | undefined,
  now: Date,
): AnnotatedFeed {
  const entries = annotations?.annotations ?? [];
  const { active, acknowledged } = partitionInsights(insights, entries, now);
  return {
    active: active.map((insight) =>
      isAcknowledgeable(insight)
        ? { insight, acknowledgeSnippet: acknowledgeSnippet(insight.id) }
        : { insight },
    ),
    acknowledged,
    filePath: ANNOTATIONS_FILE_PATH,
  };
}

/**
 * A `tracked_in` URL safe to render as an external link: an http(s) URL. The CP already shape-
 * validates this, but the console re-gates DEFENSIVELY on the scheme — a non-http value (e.g. a
 * `javascript:` URL) that somehow reached the client renders as inert text, never a live link. Any
 * tracker host is allowed (GitHub/GitLab/Jira/…); unlike the GitHub link builders this is a
 * user-cited issue URL, so the external-link convention gates the SCHEME, not the host.
 */
export function trackedInHref(url: string | null | undefined): string | undefined {
  if (!url) return undefined;
  try {
    const { protocol } = new URL(url);
    return protocol === "http:" || protocol === "https:" ? url : undefined;
  } catch {
    return undefined;
  }
}
