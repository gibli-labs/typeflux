/**
 * Deterministic RANK/ROW layout for the bundle's nodes+edges topology projection.
 *
 * The #55 composition vocabulary is graph-shaped, not a single linear spine:
 * a `parallel` node fans out to each branch's chain (`branch` edges) and the
 * branches `collect` back where the merged value materializes; `when`-gated
 * steps carry their predicate on a `conditional` edge. This layout places every
 * node by:
 *
 *   * RANK (x): the longest path from the roots over rank-advancing edges. A
 *     `collect` edge points back to its parallel node (a structural back-edge),
 *     so it is not laid down as-is — it advances the rank of the block's
 *     CONTINUATION instead (the branch's last step must precede whatever runs
 *     after the parallel block). Review edges never advance rank. A visited
 *     guard breaks any malformed cycle so the layout can't hang; a node a cycle
 *     prevents ranking falls back to its input index.
 *   * ROW (y): nodes sharing a rank stack downward, ordered by first branch-edge
 *     appearance then node id. A purely sequential workflow has one node per
 *     rank, so its top row stays exactly where the old linear spine placed it —
 *     coordinates are byte-identical (regression-pinned).
 *
 * Same-row adjacent edges render as a straight horizontal spine arrow. Cross-row
 * and rank-skipping/back edges route orthogonally (H-V-H) in per-edge channels so
 * parallel fan-out/fan-in arrows don't overlap. Each edge keeps its `kind` on the
 * rendered path (CSS hooks); `branch`/`conditional` edges carry their condition as
 * a rendered label. Review decisions stay their own labeled swim-lanes below the
 * DEEPEST row (unchanged semantics). Any kind this layout doesn't model — a future
 * composition shape — still degrades to a plain spine-style arrow carrying its own
 * kind, so a newer control plane never corrupts the view.
 */

import type { BundleTopology } from "./api";

export const NODE_WIDTH = 168;
export const NODE_HEIGHT = 46;
export const LANE_HEIGHT = 30;
const GAP = 56;
const NODE_Y = 24;
const ROW_GAP = 24;
const ROW_PITCH = NODE_HEIGHT + ROW_GAP;
const LANE_GAP = 30;
const PADDING = 24;
// Below the deepest row, `collect` (and any back-edge) fan-in arrows run in their
// own channel band; review swim-lanes sit below that band.
const COLLECT_CHANNEL_GAP = 8;
const COLLECT_BAND_PAD = 12;

export interface PositionedNode {
  id: string;
  kind: string;
  /** Absent for nodes that call no activity (a #55 `parallel` block) — renders empty. */
  activity: string | null;
  /** The child workflow's manifest id on `kind === "workflow"` sub-workflow nodes (#55 slice 3). */
  workflow: string | null;
  /** Longest-path rank (column). */
  rank: number;
  /** Row within the rank (0 = the spine row). */
  row: number;
  x: number;
  y: number;
  /** True when review decision routes leave this node. */
  checkpoint: boolean;
}

export interface PositionedEdge {
  source: string;
  target: string;
  /** "sequential" | "review" | "branch" | "collect" | "conditional" | "fallthrough" | (future kinds). */
  kind: string;
  condition: string | null;
  /** SVG path data. */
  d: string;
  /** Anchor for a rendered condition label (branch/conditional edges only). */
  labelX: number | null;
  labelY: number | null;
}

export interface ReviewLane {
  condition: string | null;
  y: number;
}

export interface TopologyLayout {
  nodes: PositionedNode[];
  edges: PositionedEdge[];
  /** One swim-lane per routed review decision, top-down. */
  lanes: ReviewLane[];
  width: number;
  height: number;
}

export function layoutTopology(topology: BundleTopology): TopologyLayout {
  const rawNodes = topology.nodes ?? [];
  const rawEdges = topology.edges ?? [];
  const indexById = new Map(rawNodes.map((node, index) => [node.id, index]));
  const known = (id: string) => indexById.has(id);
  const validEdges = rawEdges.filter((edge) => known(edge.source) && known(edge.target));

  // Review lanes key on kind === "review" EXPLICITLY: only review decisions get
  // swim-lanes and checkpoint marking. Every other kind is graph structure.
  const reviewEdges = validEdges.filter((edge) => edge.kind === "review");
  const reviewSources = new Set(reviewEdges.map((edge) => edge.source));

  // --- RANK ---------------------------------------------------------------
  // Rank-advancing edges: every non-review edge EXCEPT `collect`, which is a
  // structural back-edge to the parallel/join node. A collect edge instead
  // advances the rank of the block's CONTINUATION — the sequential/conditional
  // steps that run AFTER the parallel node — so post-block steps land to the
  // right of the branches rather than colliding with them. Unknown future
  // kinds advance rank like any forward edge (and render as a plain arrow).
  const continuations = new Map<string, string[]>();
  for (const edge of validEdges) {
    if (edge.kind === "sequential" || edge.kind === "conditional") {
      const list = continuations.get(edge.source);
      if (list) list.push(edge.target);
      else continuations.set(edge.source, [edge.target]);
    }
  }
  const predecessors = new Map<string, string[]>();
  const addRankEdge = (from: string, to: string) => {
    const list = predecessors.get(to);
    if (list) list.push(from);
    else predecessors.set(to, [from]);
  };
  // A collect source that is ITSELF a join (an inner `parallel` node ending an outer
  // branch — nested parallels are spec-supported) must forward its FINISH nodes, not
  // itself: the outer continuation belongs after the inner branch ENDS, which rank
  // deeper than the inner parallel node's own start. Resolved recursively with a
  // seen-guard so a malformed collect cycle degrades instead of recursing forever.
  const collectSourcesByTarget = new Map<string, string[]>();
  for (const edge of validEdges) {
    if (edge.kind !== "collect") continue;
    const list = collectSourcesByTarget.get(edge.target);
    if (list) list.push(edge.source);
    else collectSourcesByTarget.set(edge.target, [edge.source]);
  }
  const finishNodes = (id: string, seen: Set<string>): string[] => {
    if (seen.has(id)) return [id];
    seen.add(id);
    const sources = collectSourcesByTarget.get(id);
    if (!sources || sources.length === 0) return [id];
    return sources.flatMap((source) => finishNodes(source, seen));
  };
  for (const edge of validEdges) {
    if (edge.kind === "review") continue;
    if (edge.kind === "collect") {
      for (const cont of continuations.get(edge.target) ?? []) {
        for (const finish of finishNodes(edge.source, new Set())) addRankEdge(finish, cont);
      }
      continue;
    }
    addRankEdge(edge.source, edge.target);
  }

  const rankMemo = new Map<string, number>();
  const stack = new Set<string>();
  const cyclic = new Set<string>();
  const rankOf = (id: string): number => {
    const memo = rankMemo.get(id);
    if (memo !== undefined) return memo;
    if (stack.has(id)) {
      // A cycle closes on `id`: this predecessor path is invalid. Flag the node
      // so it can fall back to input order, and contribute nothing (-1) here.
      cyclic.add(id);
      return -1;
    }
    stack.add(id);
    let best = 0;
    for (const pred of predecessors.get(id) ?? []) {
      const r = rankOf(pred);
      if (r >= 0) best = Math.max(best, r + 1);
    }
    stack.delete(id);
    rankMemo.set(id, best);
    return best;
  };
  const ranks = new Map<string, number>();
  for (const node of rawNodes) ranks.set(node.id, rankOf(node.id));
  // Malformed cycle fallback: a node a cycle prevented from ranking uses its
  // input index, so a bad graph degrades to declared order instead of hanging.
  for (const id of cyclic) ranks.set(id, indexById.get(id) ?? 0);

  // --- ROW ----------------------------------------------------------------
  // Deterministic within a rank: order by first branch-edge appearance (branch
  // fan-out order), then node id. A rank with one node keeps row 0 (the spine).
  // The "lane" of a node is the fan-out order of the branch it belongs to. A
  // branch edge seeds its target with the edge's appearance index; the lane then
  // propagates FORWARD through the branch's internal chain (sequential/conditional
  // steps), so a MULTI-step branch keeps every step in the same row — not just its
  // first. Collect/review edges are excluded from the forward walk, so propagation
  // stops at the branch's last step (its collect returns to the parallel node) and
  // never leaks into the post-block continuation.
  const forwardAdj = new Map<string, string[]>();
  const firstBranchIndex = new Map<string, number>();
  validEdges.forEach((edge, index) => {
    if (edge.kind === "review" || edge.kind === "collect") return;
    const list = forwardAdj.get(edge.source);
    if (list) list.push(edge.target);
    else forwardAdj.set(edge.source, [edge.target]);
    if (edge.kind === "branch" && !firstBranchIndex.has(edge.target)) {
      firstBranchIndex.set(edge.target, index);
    }
  });
  const laneOf = new Map<string, number>();
  const queue: string[] = [];
  for (const [target, index] of firstBranchIndex) {
    laneOf.set(target, index);
    queue.push(target);
  }
  while (queue.length > 0) {
    const cur = queue.shift() as string;
    const lane = laneOf.get(cur) as number;
    for (const next of forwardAdj.get(cur) ?? []) {
      const existing = laneOf.get(next);
      // Keep the earliest branch lane; only lowering it can re-enqueue, so the
      // walk terminates even on a malformed graph.
      if (existing === undefined || lane < existing) {
        laneOf.set(next, lane);
        queue.push(next);
      }
    }
  }

  const rows = new Map<string, number>();
  const byRank = new Map<number, string[]>();
  for (const node of rawNodes) {
    const rank = ranks.get(node.id) ?? 0;
    const list = byRank.get(rank);
    if (list) list.push(node.id);
    else byRank.set(rank, [node.id]);
  }
  for (const members of byRank.values()) {
    members
      .slice()
      .sort((a, b) => {
        const la = laneOf.get(a) ?? Number.POSITIVE_INFINITY;
        const lb = laneOf.get(b) ?? Number.POSITIVE_INFINITY;
        if (la !== lb) return la - lb;
        return a.localeCompare(b);
      })
      .forEach((id, row) => rows.set(id, row));
  }

  const rowY = (row: number) => NODE_Y + row * ROW_PITCH;
  const centerY = (row: number) => rowY(row) + NODE_HEIGHT / 2;

  const nodes: PositionedNode[] = rawNodes.map((node) => {
    const rank = ranks.get(node.id) ?? 0;
    const row = rows.get(node.id) ?? 0;
    return {
      id: node.id,
      kind: node.kind,
      activity: node.activity ?? null,
      // The 1.5.0 contract types `workflow` on sub-workflow nodes; read it typed.
      workflow: node.workflow ?? null,
      rank,
      row,
      x: PADDING + rank * (NODE_WIDTH + GAP),
      y: rowY(row),
      checkpoint: reviewSources.has(node.id),
    };
  });
  const byId = new Map(nodes.map((node) => [node.id, node]));

  const maxRank = nodes.reduce((max, node) => Math.max(max, node.rank), 0);
  const maxRow = nodes.reduce((max, node) => Math.max(max, node.row), 0);
  const deepestBottom = rowY(maxRow) + NODE_HEIGHT;

  // Back-edge (collect) channels live in a band below the deepest row; size the
  // band up front so review lanes sit beneath it.
  const backEdgeCount = validEdges.filter(
    (edge) => edge.kind !== "review" && (byId.get(edge.target)?.x ?? 0) < (byId.get(edge.source)?.x ?? 0),
  ).length;
  const collectBand = backEdgeCount > 0 ? COLLECT_BAND_PAD + backEdgeCount * COLLECT_CHANNEL_GAP : 0;

  const edges: PositionedEdge[] = [];

  // --- REVIEW SWIM-LANES (unchanged behavior) -----------------------------
  // Shorter routes take the lanes nearest the spine; ties break on the decision
  // name. "Span" is measured in RANKS now, so a multi-row graph orders lanes the
  // same way the old index-based spine did.
  const ordered = [...reviewEdges].sort((a, b) => {
    const spanA = Math.abs((ranks.get(a.target) ?? 0) - (ranks.get(a.source) ?? 0));
    const spanB = Math.abs((ranks.get(b.target) ?? 0) - (ranks.get(b.source) ?? 0));
    if (spanA !== spanB) return spanA - spanB;
    return (a.condition ?? "").localeCompare(b.condition ?? "");
  });
  const laneBase = deepestBottom + collectBand + LANE_GAP;
  const lanes: ReviewLane[] = [];
  ordered.forEach((edge, laneIndex) => {
    const source = byId.get(edge.source);
    const target = byId.get(edge.target);
    if (!source || !target) return;
    const laneY = laneBase + laneIndex * LANE_HEIGHT;
    // Per-lane offsets keep vertical drops/rises from overlapping when several
    // routes share a checkpoint or a target.
    const exitX = source.x + NODE_WIDTH / 2 + 12 + laneIndex * 9;
    const entryX = target.x + NODE_WIDTH / 2 - 12 - laneIndex * 9;
    const sourceBottom = source.y + NODE_HEIGHT;
    const targetBottom = target.y + NODE_HEIGHT;
    lanes.push({ condition: edge.condition ?? null, y: laneY });
    edges.push({
      source: edge.source,
      target: edge.target,
      kind: edge.kind,
      condition: edge.condition ?? null,
      d: `M ${exitX} ${sourceBottom} V ${laneY} H ${entryX} V ${targetBottom + 4}`,
      labelX: null,
      labelY: null,
    });
  });

  // --- STRUCTURE EDGES ----------------------------------------------------
  // Straight spine arrow for same-row adjacent forward edges (a purely sequential
  // workflow is all of these — byte-identical to the old layout). Everything else
  // routes orthogonally. The warn-mode fall-through substitution is unchanged: a
  // checkpoint's UNCONDITIONAL sequential arrow is suppressed when a decision
  // already covers it, else it renders dotted.
  const forwardOffset = new Map<string, number>();
  let backChannel = 0;
  for (const edge of validEdges) {
    if (edge.kind === "review") continue;
    const source = byId.get(edge.source);
    const target = byId.get(edge.target);
    if (!source || !target) continue;

    let kind: string = edge.kind;
    if (source.checkpoint && edge.kind === "sequential") {
      const covered = reviewEdges.some(
        (review) => review.source === edge.source && review.target === edge.target,
      );
      if (covered) continue;
      kind = "fallthrough";
    }

    const cyS = centerY(source.row);
    const cyT = centerY(target.row);
    const rightS = source.x + NODE_WIDTH;
    const leftT = target.x;
    const sameRow = source.row === target.row;
    const adjacent = target.rank === source.rank + 1;
    const forward = target.x > source.x;

    let d: string;
    let labelX: number | null = null;
    let labelY: number | null = null;

    if (sameRow && adjacent && forward) {
      // The spine: straight horizontal arrow (old coordinates, exactly).
      d = `M ${rightS} ${cyS} L ${leftT} ${cyT}`;
      labelX = (rightS + leftT) / 2;
      labelY = cyS - 6;
    } else if (forward) {
      const n = forwardOffset.get(edge.source) ?? 0;
      forwardOffset.set(edge.source, n + 1);
      if (sameRow) {
        // Rank-skip on the same row (a step pushed past a parallel block): a
        // straight line would cut through the intervening node, so detour up
        // through the top-padding channel and drop into the target.
        const channelY = Math.max(4, NODE_Y - 10 - n * 6);
        const sx = source.x + NODE_WIDTH / 2;
        const tx = target.x + NODE_WIDTH / 2;
        d = `M ${sx} ${source.y} V ${channelY} H ${tx} V ${target.y}`;
        labelX = (sx + tx) / 2;
        labelY = channelY - 4;
      } else {
        // Cross-row fan-out/continuation: exit right, vertical in a channel just
        // left of the target, enter the target's left edge.
        const midX = (rightS + leftT) / 2 + n * 8;
        d = `M ${rightS} ${cyS} H ${midX} V ${cyT} H ${leftT}`;
        labelX = midX;
        labelY = (cyS + cyT) / 2 - 6;
      }
    } else {
      // Back-edge (collect returning to its parallel/join node): drop below the
      // deepest row into the fan-in band, run back, rise into the target.
      const channelY = deepestBottom + COLLECT_BAND_PAD / 2 + backChannel * COLLECT_CHANNEL_GAP;
      backChannel += 1;
      const sx = source.x + NODE_WIDTH / 2;
      const tx = target.x + NODE_WIDTH / 2;
      d = `M ${sx} ${source.y + NODE_HEIGHT} V ${channelY} H ${tx} V ${target.y + NODE_HEIGHT}`;
    }

    edges.push({
      source: edge.source,
      target: edge.target,
      kind,
      condition: edge.condition ?? null,
      // Only branch/conditional edges render their predicate; a bare label anchor
      // on a plain edge would be noise.
      d,
      labelX: edge.condition && (kind === "branch" || kind === "conditional") ? labelX : null,
      labelY: edge.condition && (kind === "branch" || kind === "conditional") ? labelY : null,
    });
  }

  const width =
    nodes.length > 0
      ? PADDING * 2 + (maxRank + 1) * NODE_WIDTH + maxRank * GAP
      : 0;
  const contentBottom =
    lanes.length > 0 ? laneBase + lanes.length * LANE_HEIGHT : deepestBottom + collectBand;
  const height = nodes.length > 0 ? contentBottom + PADDING : 0;
  return { nodes, edges, lanes, width, height };
}
