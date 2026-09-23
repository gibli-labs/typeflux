import { useState } from "react";

import type { BundleTopology } from "./api";
import { NODE_HEIGHT, NODE_WIDTH, layoutTopology } from "./topology";

/** Approximate mono-text width at the 10px topology size — sizes the hover panel's box. */
const CHAR_WIDTH = 6.2;

/**
 * Read-only workflow structure as a ranked DAG (#55): sequential steps on the
 * spine, `parallel` branches fanning out into stacked rows and collecting back,
 * `when`-gated `conditional`/`branch` edges carrying a `when` pill that reveals
 * the predicate in an instant hover panel, and one labeled swim-lane per routed
 * review decision below the deepest row. Review decisions are entry points into
 * the downstream chain — the checkpoint has no unconditional outgoing arrow, and
 * the spine continues after each entry point. With `activeStep` set (run
 * inspection) the live node is highlighted — this is monitoring, never authoring.
 */
export function TopologyView({
  topology,
  activeStep,
  env,
}: {
  topology: BundleTopology;
  activeStep?: string | null;
  /** The page's selected environment — carried on sub-workflow links so the child
   * opens against the SAME environment instead of the first-configured fallback. */
  env?: string;
}) {
  // The hovered/focused `when` pill's edge index. React state (not a native <title>)
  // because the reveal must be INSTANT and the panel must paint ABOVE the node layer —
  // SVG paints in document order, so the panel renders in a dedicated last layer.
  const [hoveredWhen, setHoveredWhen] = useState<number | null>(null);
  // Reset on a topology swap DURING render (the sanctioned adjust-state-on-prop-change
  // pattern): the index is only meaningful against one layout, and no mouseleave fires
  // when this same instance re-renders for a different workflow — without this, a stale
  // index would ghost the overlay onto whatever edge the new graph has at that position.
  const [prevTopology, setPrevTopology] = useState(topology);
  if (prevTopology !== topology) {
    setPrevTopology(topology);
    setHoveredWhen(null);
  }
  const layout = layoutTopology(topology);
  if (layout.nodes.length === 0) {
    return <div className="dim">No steps.</div>;
  }
  return (
    <svg
      className="topology-svg"
      viewBox={`0 0 ${layout.width} ${layout.height}`}
      width={layout.width}
      height={layout.height}
      role="img"
      aria-label="Workflow topology"
    >
      <defs>
        <marker id="arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto">
          <path d="M 0 0 L 8 4 L 0 8 z" fill="var(--border-strong)" />
        </marker>
        <marker id="arrow-review" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto">
          <path d="M 0 0 L 8 4 L 0 8 z" fill="var(--warn)" />
        </marker>
        {/* Muted head for `collect` fan-in — it merges rather than advances flow. */}
        <marker id="arrow-muted" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto">
          <path d="M 0 0 L 8 4 L 0 8 z" fill="var(--border)" />
        </marker>
      </defs>
      {layout.lanes.map((lane, index) => (
        <g key={`lane:${lane.condition ?? index}`}>
          <line className="topo-lane" x1={12} y1={lane.y} x2={layout.width - 12} y2={lane.y} />
          <text className="topo-edge-label" x={14} y={lane.y - 5}>
            {lane.condition}
          </text>
        </g>
      ))}
      {layout.edges.map((edge, index) => (
        <g key={`${edge.kind}:${edge.source}:${edge.target}:${index}`}>
          <path
            className={`topo-edge ${edge.kind}`}
            d={edge.d}
            markerEnd={
              edge.kind === "review"
                ? "url(#arrow-review)"
                : edge.kind === "collect"
                  ? "url(#arrow-muted)"
                  : "url(#arrow)"
            }
          />
          {/* branch/conditional predicates fold into a `when` pill — full predicates as
              inline text crowded the diagram. Hover reveals the predicate instantly in
              the overlay layer below (React state, not the delayed native tooltip). */}
          {edge.condition && edge.labelX !== null && edge.labelY !== null ? (
            <g
              className="topo-when"
              data-condition={edge.condition}
              tabIndex={0}
              role="img"
              aria-label={`when ${edge.condition}`}
              onMouseEnter={() => setHoveredWhen(index)}
              onMouseLeave={() => setHoveredWhen((current) => (current === index ? null : current))}
              onFocus={() => setHoveredWhen(index)}
              onBlur={() => setHoveredWhen((current) => (current === index ? null : current))}
            >
              <rect
                className="topo-when-pill"
                x={edge.labelX - 22}
                y={edge.labelY - 13}
                width={44}
                height={15}
                rx={7.5}
              />
              <text className="topo-when-label" x={edge.labelX} y={edge.labelY - 2} textAnchor="middle">
                when
              </text>
            </g>
          ) : null}
        </g>
      ))}
      {layout.nodes.map((node) => {
        // Map AND parallel share the stacked-card treatment — one visual language for
        // "runs concurrently" (map: N copies of one activity; parallel: N branches).
        const stacked = node.kind === "map" || node.kind === "parallel";
        const body = (
          <>
            {stacked ? (
              <>
                <rect
                  className="topo-stack"
                  x={node.x + 8}
                  y={node.y - 8}
                  width={NODE_WIDTH}
                  height={NODE_HEIGHT}
                  rx={5}
                />
                <rect
                  className="topo-stack"
                  x={node.x + 4}
                  y={node.y - 4}
                  width={NODE_WIDTH}
                  height={NODE_HEIGHT}
                  rx={5}
                />
              </>
            ) : null}
            <rect x={node.x} y={node.y} width={NODE_WIDTH} height={NODE_HEIGHT} rx={5} />
            <text x={node.x + 10} y={node.y + 19}>
              {node.id}
            </text>
            <text className="activity" x={node.x + 10} y={node.y + 35}>
              {/* A sub-workflow node calls no activity — its secondary label is the child
                  workflow's manifest id (#55 slice 3). */}
              {node.activity ?? node.workflow}
            </text>
            <text className="topo-kind" x={node.x + NODE_WIDTH - 8} y={node.y + 13} textAnchor="end">
              {node.kind === "map"
                ? "MAP"
                : node.kind === "workflow"
                  ? "SUB"
                  : node.kind === "parallel"
                    ? "PARALLEL"
                    : ""}
            </text>
            {node.checkpoint ? (
              <text
                className="topo-review-tag"
                x={node.x + NODE_WIDTH}
                y={node.y - 6}
                textAnchor="end"
              >
                ⏸ REVIEW
              </text>
            ) : null}
          </>
        );
        const className = `topo-node ${node.kind} ${node.id === activeStep ? "active" : ""}`;
        // A sub-workflow node deep-links to the child workflow's page (same hash-href
        // convention as insight rows).
        return node.kind === "workflow" && node.workflow ? (
          <a
            key={node.id}
            href={`#/workflows/${node.workflow}${env ? `?env=${encodeURIComponent(env)}` : ""}`}
            className="topo-node-link"
          >
            <g className={className}>
              <title>{`open ${node.workflow}`}</title>
              {body}
            </g>
          </a>
        ) : (
          <g key={node.id} className={className}>
            {body}
          </g>
        );
      })}
      {/* Hover overlay LAST so it paints above every node — SVG has no z-index. */}
      {layout.edges.map((edge, index) => {
        if (index !== hoveredWhen || !edge.condition || edge.labelX === null || edge.labelY === null)
          return null;
        const width = edge.condition.length * CHAR_WIDTH + 18;
        const x = Math.max(4, Math.min(edge.labelX - width / 2, layout.width - width - 4));
        const y = edge.labelY + 6;
        return (
          <g key={`when-detail:${index}`} className="topo-when-detail">
            <rect x={x} y={y} width={width} height={20} rx={4} />
            <text x={x + 9} y={y + 14}>
              {edge.condition}
            </text>
          </g>
        );
      })}
    </svg>
  );
}
