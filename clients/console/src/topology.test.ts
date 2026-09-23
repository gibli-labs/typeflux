import { describe, expect, it } from "vitest";

import type { BundleTopology } from "./api";
import { NODE_HEIGHT, NODE_WIDTH, layoutTopology } from "./topology";

const TOPOLOGY: BundleTopology = {
  nodes: [
    { id: "assess_items", kind: "map", activity: "assess_item" },
    { id: "summarize", kind: "activity", activity: "summarize" },
    { id: "decide", kind: "activity", activity: "decide" },
  ],
  edges: [
    { source: "assess_items", target: "summarize", kind: "sequential" },
    { source: "summarize", target: "decide", kind: "sequential" },
    { source: "assess_items", target: "summarize", kind: "review", condition: "approve" },
    { source: "assess_items", target: "decide", kind: "review", condition: "fast_track" },
  ],
};

describe("layoutTopology — sequential spine (regression: coordinates must not shift)", () => {
  it("positions nodes left-to-right in declared order on one row", () => {
    const layout = layoutTopology(TOPOLOGY);

    expect(layout.nodes.map((node) => node.id)).toEqual([
      "assess_items",
      "summarize",
      "decide",
    ]);
    const xs = layout.nodes.map((node) => node.x);
    expect(xs[0]).toBeLessThan(xs[1]);
    expect(xs[1]).toBeLessThan(xs[2]);
    // Every node is on the spine row (row 0) at the same y — a purely sequential
    // workflow ranks 0,1,2 and never stacks.
    expect(layout.nodes.map((node) => [node.rank, node.row])).toEqual([
      [0, 0],
      [1, 0],
      [2, 0],
    ]);
    expect(layout.nodes.every((node) => node.y === layout.nodes[0].y)).toBe(true);
    expect(layout.width).toBeGreaterThan(xs[2] + NODE_WIDTH);
  });

  it("pins the exact x/y and straight spine-arrow path for a purely sequential workflow", () => {
    const layout = layoutTopology({
      nodes: TOPOLOGY.nodes,
      edges: [
        { source: "assess_items", target: "summarize", kind: "sequential" },
        { source: "summarize", target: "decide", kind: "sequential" },
      ],
    });
    // PADDING 24, NODE_WIDTH 168, GAP 56, NODE_Y 24 → x = 24 + rank*224, y = 24.
    expect(layout.nodes.map((node) => [node.x, node.y])).toEqual([
      [24, 24],
      [248, 24],
      [472, 24],
    ]);
    // Straight arrow at the spine center y (24 + 46/2 = 47), source-right → target-left.
    expect(layout.edges.map((edge) => edge.d)).toEqual([
      "M 192 47 L 248 47",
      "M 416 47 L 472 47",
    ]);
    // No lanes, no collect band → height/width unchanged from the old linear layout.
    expect(layout.width).toBe(24 * 2 + 3 * 168 + 2 * 56);
    expect(layout.height).toBe(24 + 46 + 24);
  });

  it("marks checkpoints and suppresses their unconditional outgoing arrow", () => {
    const layout = layoutTopology(TOPOLOGY);

    expect(layout.nodes.find((node) => node.id === "assess_items")?.checkpoint).toBe(true);
    expect(layout.nodes.find((node) => node.id === "summarize")?.checkpoint).toBe(false);

    const sequential = layout.edges.filter((edge) => edge.kind === "sequential");
    expect(sequential.map((edge) => [edge.source, edge.target])).toEqual([
      ["summarize", "decide"],
    ]);
    expect(sequential[0].d).toMatch(/^M .+ L .+$/);
    expect(layout.edges.some((edge) => edge.kind === "fallthrough")).toBe(false);
  });

  it("renders the warn-mode fall-through dotted only when no decision covers the next step", () => {
    const layout = layoutTopology({
      nodes: TOPOLOGY.nodes,
      edges: [
        { source: "assess_items", target: "summarize", kind: "sequential" },
        { source: "summarize", target: "decide", kind: "sequential" },
        { source: "assess_items", target: "decide", kind: "review", condition: "fast_track" },
      ],
    });

    const fallthrough = layout.edges.filter((edge) => edge.kind === "fallthrough");
    expect(fallthrough.map((edge) => [edge.source, edge.target])).toEqual([
      ["assess_items", "summarize"],
    ]);
  });

  it("gives each review decision its own labeled swim-lane below the spine", () => {
    const layout = layoutTopology(TOPOLOGY);

    expect(layout.lanes.map((lane) => lane.condition)).toEqual(["approve", "fast_track"]);
    const nodeBottom = layout.nodes[0].y + NODE_HEIGHT;
    expect(layout.lanes[0].y).toBeGreaterThan(nodeBottom);
    expect(layout.lanes[1].y).toBeGreaterThan(layout.lanes[0].y);

    const review = layout.edges.filter((edge) => edge.kind === "review");
    expect(review).toHaveLength(2);
    expect(review.every((edge) => /^M .+ V .+ H .+ V .+$/.test(edge.d))).toBe(true);
    expect(review.map((edge) => edge.condition)).toEqual(["approve", "fast_track"]);
  });

  it("grows the canvas with the lane count and shrinks without lanes", () => {
    const withLanes = layoutTopology(TOPOLOGY);
    const withoutLanes = layoutTopology({
      nodes: TOPOLOGY.nodes,
      edges: (TOPOLOGY.edges ?? []).filter((edge) => edge.kind === "sequential"),
    });

    expect(withoutLanes.lanes).toEqual([]);
    expect(withLanes.height).toBeGreaterThan(withoutLanes.height);
  });

  it("skips edges that reference unknown nodes and handles empty topologies", () => {
    const layout = layoutTopology({
      nodes: [{ id: "only", kind: "activity", activity: "only" }],
      edges: [{ source: "only", target: "missing", kind: "sequential" }],
    });
    expect(layout.edges).toEqual([]);
    expect(layout.lanes).toEqual([]);

    const empty = layoutTopology({ nodes: [], edges: [] });
    expect(empty.nodes).toEqual([]);
    expect(empty.width).toBe(0);
    expect(empty.height).toBe(0);
  });
});

describe("layoutTopology — #55 composition DAG", () => {
  // A parallel block: `fork` fans out to three branches, each collects back, and
  // `join` runs after the block. Byte-for-byte this mirrors the emission order the
  // Python/TS generators produce (parallel node, then per-branch edges/nodes).
  const parallel = {
    nodes: [
      { id: "fork", kind: "parallel" },
      { id: "a", kind: "activity", activity: "assess_a" },
      { id: "b", kind: "activity", activity: "assess_b" },
      { id: "c", kind: "map", activity: "assess_c" },
      { id: "join", kind: "activity", activity: "merge" },
    ],
    edges: [
      { source: "fork", target: "a", kind: "branch" },
      { source: "a", target: "fork", kind: "collect" },
      { source: "fork", target: "b", kind: "branch", condition: "priority == high" },
      { source: "b", target: "fork", kind: "collect" },
      { source: "fork", target: "c", kind: "branch" },
      { source: "c", target: "fork", kind: "collect" },
      { source: "fork", target: "join", kind: "sequential" },
    ],
  } as unknown as BundleTopology;

  it("ranks the fork before its branches and pushes the join past them (collect advances rank)", () => {
    const layout = layoutTopology(parallel);
    const rank = (id: string) => layout.nodes.find((node) => node.id === id)!.rank;

    expect(rank("fork")).toBe(0);
    expect(rank("a")).toBe(1);
    expect(rank("b")).toBe(1);
    expect(rank("c")).toBe(1);
    // The join is a direct sequential successor of the fork, but the collect
    // edges advance it past the deepest branch — rank 2, not 1.
    expect(rank("join")).toBe(2);
    // x tracks rank; branches share the fork's successor column.
    expect(layout.nodes.find((n) => n.id === "a")!.x).toBe(
      layout.nodes.find((n) => n.id === "b")!.x,
    );
  });

  it("stacks the branch nodes in rows by branch-edge appearance, then id", () => {
    const layout = layoutTopology(parallel);
    const row = (id: string) => layout.nodes.find((node) => node.id === id)!.row;

    // fork/join are alone in their rank → the spine row.
    expect(row("fork")).toBe(0);
    expect(row("join")).toBe(0);
    // Branches stack in declared fan-out order: a (first branch edge), then b, then c.
    expect([row("a"), row("b"), row("c")]).toEqual([0, 1, 2]);
    // Stacked rows get distinct, increasing y.
    const ya = layout.nodes.find((n) => n.id === "a")!.y;
    const yb = layout.nodes.find((n) => n.id === "b")!.y;
    const yc = layout.nodes.find((n) => n.id === "c")!.y;
    expect(ya).toBeLessThan(yb);
    expect(yb).toBeLessThan(yc);
  });

  it("routes branch/collect edges orthogonally and keeps each kind on the path", () => {
    const layout = layoutTopology(parallel);

    const branchToB = layout.edges.find(
      (edge) => edge.kind === "branch" && edge.target === "b",
    )!;
    // Cross-row branch → orthogonal H-V-H, never a straight L.
    expect(branchToB.d).toMatch(/^M .+ H .+ V .+ H .+$/);

    // Same-row adjacent branch (fork → a) stays a straight spine arrow but keeps its kind.
    const branchToA = layout.edges.find(
      (edge) => edge.kind === "branch" && edge.target === "a",
    )!;
    expect(branchToA.d).toMatch(/^M .+ L .+$/);

    // Every collect edge points back to the fork and routes below the deepest row.
    const collects = layout.edges.filter((edge) => edge.kind === "collect");
    expect(collects.map((edge) => edge.target)).toEqual(["fork", "fork", "fork"]);
    expect(collects.every((edge) => /^M .+ V .+ H .+ V .+$/.test(edge.d))).toBe(true);

    // The when-gated branch renders its predicate as a label; an ungated one does not.
    expect(branchToB.condition).toBe("priority == high");
    expect(branchToB.labelX).not.toBeNull();
    expect(branchToA.labelX).toBeNull();
  });

  it("keeps every step of a MULTI-step branch in its own row (lane propagation, not id sort)", () => {
    // Two branches of two steps each. The second-rank steps (`z_a2`, `a_b2`)
    // carry no branch edge of their own — a naive id sort would put `a_b2`
    // (branch 2) above `z_a2` (branch 1), crossing the lanes. Lane propagation
    // must keep each branch's chain in the row its first step established.
    const layout = layoutTopology({
      nodes: [
        { id: "fork", kind: "parallel" },
        { id: "z_a1", kind: "activity", activity: "a1" },
        { id: "z_a2", kind: "activity", activity: "a2" },
        { id: "a_b1", kind: "activity", activity: "b1" },
        { id: "a_b2", kind: "activity", activity: "b2" },
        { id: "join", kind: "activity", activity: "merge" },
      ],
      edges: [
        { source: "fork", target: "z_a1", kind: "branch" },
        { source: "z_a1", target: "z_a2", kind: "sequential" },
        { source: "z_a2", target: "fork", kind: "collect" },
        { source: "fork", target: "a_b1", kind: "branch" },
        { source: "a_b1", target: "a_b2", kind: "sequential" },
        { source: "a_b2", target: "fork", kind: "collect" },
        { source: "fork", target: "join", kind: "sequential" },
      ],
    });
    const row = (id: string) => layout.nodes.find((node) => node.id === id)!.row;
    // Branch 1 (first fan-out edge) owns row 0 across both of its ranks.
    expect([row("z_a1"), row("z_a2")]).toEqual([0, 0]);
    // Branch 2 stays in row 1 across both of its ranks — even though `a_b2`
    // sorts before `z_a2` by id.
    expect([row("a_b1"), row("a_b2")]).toEqual([1, 1]);
  });

  it("labels a conditional (when-gated) edge with its predicate", () => {
    const layout = layoutTopology({
      nodes: [
        { id: "merge", kind: "activity", activity: "merge" },
        { id: "escalate", kind: "activity", activity: "escalate" },
      ],
      edges: [
        {
          source: "merge",
          target: "escalate",
          kind: "conditional",
          condition: "classify.deep_review == true",
        },
      ],
    });
    const edge = layout.edges.find((e) => e.kind === "conditional")!;
    expect(edge.condition).toBe("classify.deep_review == true");
    expect(edge.labelX).not.toBeNull();
    expect(edge.labelY).not.toBeNull();
  });

  it("renders review swim-lanes below the DEEPEST row of a multi-row graph", () => {
    const layout = layoutTopology({
      nodes: [
        { id: "fork", kind: "parallel" },
        { id: "a", kind: "activity", activity: "a" },
        { id: "b", kind: "activity", activity: "b" },
        { id: "gate", kind: "activity", activity: "gate" },
        { id: "done", kind: "activity", activity: "done" },
      ],
      edges: [
        { source: "fork", target: "a", kind: "branch" },
        { source: "a", target: "fork", kind: "collect" },
        { source: "fork", target: "b", kind: "branch" },
        { source: "b", target: "fork", kind: "collect" },
        { source: "fork", target: "gate", kind: "sequential" },
        { source: "gate", target: "done", kind: "sequential" },
        { source: "gate", target: "done", kind: "review", condition: "approve" },
      ],
    });
    expect(layout.lanes).toHaveLength(1);
    expect(layout.nodes.find((n) => n.id === "gate")!.checkpoint).toBe(true);
    // The lane sits below the bottom of every node — including the stacked branch row.
    const deepestNodeBottom = Math.max(...layout.nodes.map((n) => n.y + NODE_HEIGHT));
    expect(layout.lanes[0].y).toBeGreaterThan(deepestNodeBottom);
  });

  it("degrades an unknown FUTURE edge kind to a plain spine arrow carrying its own kind", () => {
    const future = {
      nodes: [
        { id: "one", kind: "activity", activity: "one" },
        { id: "two", kind: "activity", activity: "two" },
      ],
      // A kind this layout has never heard of (a newer control plane): it must
      // still advance rank and render as a plain arrow — never a review lane.
      edges: [{ source: "one", target: "two", kind: "telemetry" }],
    } as unknown as BundleTopology;
    const layout = layoutTopology(future);

    expect(layout.lanes).toEqual([]);
    expect(layout.nodes.every((node) => !node.checkpoint)).toBe(true);
    const edge = layout.edges[0];
    expect(edge.kind).toBe("telemetry");
    expect(edge.d).toMatch(/^M .+ L .+$/);
  });

  it("sub-workflow nodes carry the child manifest id and render inside a parallel branch", () => {
    const layout = layoutTopology({
      nodes: [
        { id: "fork", kind: "parallel" },
        { id: "child_call", kind: "workflow", workflow: "child_assessment" },
        { id: "join", kind: "activity", activity: "merge" },
      ],
      edges: [
        { source: "fork", target: "child_call", kind: "branch" },
        { source: "child_call", target: "fork", kind: "collect" },
        { source: "fork", target: "join", kind: "sequential" },
      ],
    });
    const child = layout.nodes.find((n) => n.id === "child_call")!;
    expect(child.kind).toBe("workflow");
    expect(child.workflow).toBe("child_assessment");
    expect(child.activity).toBeNull();
    // It lives one rank right of the fork (inside the branch), not orphaned.
    expect(child.rank).toBe(1);
  });

  it("guards against a malformed cycle instead of hanging", () => {
    const layout = layoutTopology({
      nodes: [
        { id: "a", kind: "activity", activity: "a" },
        { id: "b", kind: "activity", activity: "b" },
        { id: "c", kind: "activity", activity: "c" },
      ],
      // A sequential cycle a → b → c → a (malformed; not a review rework loop).
      edges: [
        { source: "a", target: "b", kind: "sequential" },
        { source: "b", target: "c", kind: "sequential" },
        { source: "c", target: "a", kind: "sequential" },
      ],
    });
    // Terminates with every node placed at a finite coordinate.
    expect(layout.nodes).toHaveLength(3);
    expect(layout.nodes.every((node) => Number.isFinite(node.x) && Number.isFinite(node.y))).toBe(
      true,
    );
    expect(Number.isFinite(layout.width)).toBe(true);
    expect(Number.isFinite(layout.height)).toBe(true);
  });
});

describe("sub-workflow nodes (#55 slice 3)", () => {
  it("carries the child workflow manifest id on kind === 'workflow' nodes", () => {
    const topology: BundleTopology = {
      nodes: [
        { id: "classify", kind: "activity", activity: "classify" },
        { id: "assess_all", kind: "workflow", workflow: "child_assessment" },
      ],
      edges: [{ source: "classify", target: "assess_all", kind: "sequential" }],
    };
    const layout = layoutTopology(topology);
    const node = layout.nodes.find((entry) => entry.id === "assess_all");
    expect(node?.kind).toBe("workflow");
    expect(node?.workflow).toBe("child_assessment");
    expect(node?.activity).toBeNull();
    expect(layout.nodes.find((entry) => entry.id === "classify")?.workflow).toBeNull();
  });
});

describe("nested parallel blocks (codex review)", () => {
  it("ranks the outer continuation AFTER the inner branches, not beside them", () => {
    // Outer parallel O: branch A (one step) + branch ending in INNER parallel Q,
    // whose branches are C and D. The continuation Z runs after the outer collect.
    // The outer collect's source is Q itself; rank-forwarding must expand Q to its
    // FINISH nodes (C, D), or Z lands in the same column as the inner branches.
    const topology: BundleTopology = {
      nodes: [
        { id: "O", kind: "parallel", activity: null },
        { id: "A", kind: "activity", activity: "a" },
        { id: "Q", kind: "parallel", activity: null },
        { id: "C", kind: "activity", activity: "c" },
        { id: "D", kind: "activity", activity: "d" },
        { id: "Z", kind: "activity", activity: "z" },
      ],
      edges: [
        { source: "O", target: "A", kind: "branch", condition: null },
        { source: "O", target: "Q", kind: "branch", condition: null },
        { source: "A", target: "O", kind: "collect", condition: null },
        { source: "Q", target: "C", kind: "branch", condition: null },
        { source: "Q", target: "D", kind: "branch", condition: null },
        { source: "C", target: "Q", kind: "collect", condition: null },
        { source: "D", target: "Q", kind: "collect", condition: null },
        { source: "Q", target: "O", kind: "collect", condition: null },
        { source: "O", target: "Z", kind: "sequential", condition: null },
      ],
    };
    const layout = layoutTopology(topology);
    const x = (id: string) => layout.nodes.find((node) => node.id === id)?.x ?? -1;
    // Inner branches sit one rank past the inner parallel node…
    expect(x("C")).toBeGreaterThan(x("Q"));
    expect(x("D")).toBeGreaterThan(x("Q"));
    // …and the outer continuation sits past the inner branch ENDS.
    expect(x("Z")).toBeGreaterThan(x("C"));
    expect(x("Z")).toBeGreaterThan(x("D"));
  });
});
