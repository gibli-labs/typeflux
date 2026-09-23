/**
 * Lane expectations for the e2e smoke (#621 slice 2): the suite runs against BOTH
 * control-plane editions, and specs assert structure through this module instead of
 * hardcoding Python-example strings.
 *
 * - `python-cp` (default): today's lane — the examples project served by the Python CP.
 * - `ts-cp`: the TypeScript CP (#620) serving the canonical conformance fixture
 *   project's TS binding under the same two registry ids.
 *
 * A capability the ts-cp lane does not serve yet is a SKIP with an issue pointer —
 * the same edition discipline as the conformance suite; a console page that ERRORS
 * (instead of degrading) on one lane only is a finding, not a skip.
 */

export type Lane = "python-cp" | "ts-cp";

/** Escape a fixture value for interpolation into a RegExp — ids are word-chars today,
 * but a future value with a metacharacter must not silently change match semantics. */
export const rx = (value: string): string => value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

const raw = process.env.E2E_SERVER ?? "python-cp";
if (raw !== "python-cp" && raw !== "ts-cp") {
  throw new Error(`unknown E2E_SERVER '${raw}' (lanes: python-cp, ts-cp)`);
}
export const LANE: Lane = raw;

interface LaneExpectations {
  /** The default registry project id and the second id the switcher targets. */
  defaultProject: string;
  secondProject: string;
  /** The primary workflow: manifest id, rendered (YAML) workflow name. */
  workflowId: string;
  workflowName: string;
  /**
   * A #55 composition workflow whose resolved bundle projects the graph-shaped
   * topology — a `parallel` block with `branch`/`collect` edges and a `when`-gated
   * (labeled) edge. Both lanes ship a byte-identical composition fixture, so the
   * console's DAG rendering is exercised end-to-end on each edition. `parallel` is
   * the node the layout ranks as the fan-out root; `condition` is a predicate the
   * fixture renders on a branch/conditional edge label.
   */
  compositionWorkflowId: string;
  /**
   * A `when` predicate the composition fixture renders verbatim on a branch/conditional
   * edge label. Asserting this exact string (rather than any `.topo-edge-label`) targets
   * the composition edge label specifically — review-lane labels carry decision NAMES,
   * never predicates, so a matching predicate proves the when-gate still rides its edge.
   */
  compositionCondition: string;
  /** A workflow whose topology contains a `workflow` sub-workflow node (may be the
   * composition workflow itself, python-cp) — its SUB nodes deep-link to the child. */
  subWorkflowParentId: string;
  /** The child workflow id that parent's SUB node (and the panel listing) links to. */
  subWorkflowId: string;
  /** The Overview renders the human workflow_name (#621 slice 3) on every lane. */
  overviewWorkflowText: string;
  /** A registry project this lane's server cannot resolve (foreign runtime). */
  foreignProject: string;
  /** A second sidebar workflow for navigation flows. */
  secondWorkflowId: string;
  /** The environment every ?env= flow uses, and the fixture's other environment. */
  environment: string;
  secondEnvironment: string;
  /** Governance content: a text the policy chain/list renders, and one rule path. */
  policyChainText: string;
  policyRuleText: string;
  /**
   * The enforcement feed's fixture behavior (#723 slice 3), probed live against each
   * lane's server: `rows` when the fixture project yields admission events in `local`
   * (the smoke asserts a real row), `empty` when it admits cleanly (the smoke asserts
   * the honest scoped-empty confirmation). Both lanes run without a Langfuse observer,
   * so the loud `not_configured` partial banner is asserted lane-agnostically.
   */
  enforcementFeed:
    | { kind: "rows"; ruleText: string }
    | { kind: "empty" };
  /**
   * The GitHub-provenance surface's fixture behavior (#727 slice 3), probed live against each
   * lane's server. Both lanes register the fixture project as a LOCAL manifest mount (no Git
   * source, no server-side GitHub token), so both serve `github_provenance: false` — the honest
   * console rendering is the explicit "not supported" panel on the Drift page's GitHub section
   * (and no PR row on plan cards). `supported: true` would flip the assertion to the live
   * drift/partial rendering; kept lane-explicit so a future Git-sourced fixture is a one-line
   * change here, not a spec rewrite.
   */
  githubProvenance: { supported: boolean };
  /**
   * The insight-acknowledgement annotations surface (#733 slice 3). The read is served on BOTH
   * lanes (pure-YAML, project-level, no capability gate). `count` is how many entries the lane's
   * fixture project ships in `.typeflux/annotations.yaml`: the examples project (python-cp) ships
   * NONE (an empty projection — the common case), while the conformance fixture (ts-cp) ships two,
   * one of them already EXPIRED. `staleAckPattern`, when set, is the expired entry's pattern the
   * console derives a stale-ack WARNING for on the project feed.
   */
  annotations: { count: number; staleAckPattern?: string };
}

export const EXPECT: LaneExpectations = {
  "python-cp": {
    defaultProject: "examples",
    secondProject: "examples_b",
    workflowId: "insurance_claim_review",
    workflowName: "InsuranceClaimReviewWorkflow",
    overviewWorkflowText: "InsuranceClaimReviewWorkflow",
    // The pure-YAML #55 showcase parent: a `parallel` screen block (two when-gated
    // branches → labeled branch edges + collect joins), a map.workflow + sub-workflow,
    // and two review gates — the fullest composition topology the examples ship.
    compositionWorkflowId: "claims_review_pure",
    // The fast_track branch's when-gate, rendered on its branch edge.
    compositionCondition: 'input.priority == "low"',
    // claims_review_pure's parallel block runs the claim_triage child — the composition
    // page itself carries SUB nodes on this lane.
    subWorkflowParentId: "claims_review_pure",
    subWorkflowId: "claim_triage",
    foreignProject: "foreign",
    secondWorkflowId: "lifecycle_review",
    environment: "local",
    secondEnvironment: "temporal_cloud_dev",
    // The regulated example policy extends base — the chain renders with provenance.
    policyChainText: "base → regulated",
    policyRuleText: "allowed.openai.models",
    // The examples project's base policy allows only openai, so its anthropic/custom
    // workflows are REJECTED at admission — the feed serves real admission events.
    enforcementFeed: {
      kind: "rows",
      ruleText: "policy_provider",
    },
    // The examples project is a local manifest mount — no Git source, no server GitHub token —
    // so the Python CP serves github_provenance: false.
    githubProvenance: { supported: false },
    // The examples project ships no .typeflux/annotations.yaml → the served projection is empty
    // (the common case), and no stale-ack warning is derived.
    annotations: { count: 0 },
  },
  "ts-cp": {
    defaultProject: "examples",
    secondProject: "examples_b",
    workflowId: "workflow",
    workflowName: "ConformanceDemoWorkflow",
    overviewWorkflowText: "ConformanceDemoWorkflow",
    // The conformance composition fixture: a `parallel` reviews block (a map branch +
    // a when-gated branch → labeled branch edge, collect joins) and a when-gated
    // `escalate` tail → a labeled conditional edge. Byte-identical to the Python binding.
    compositionWorkflowId: "composition",
    // The deep-review when-gate, rendered on both the `summary` branch edge and the
    // `escalate` conditional tail.
    compositionCondition: "classify.deep_review == true",
    // The conformance `composition` workflow has no sub-workflow step; the dedicated
    // `subworkflow` fixture workflow carries the SUB node on this lane (why-note: the
    // fixtures split composition and sub-workflow coverage into separate workflows).
    subWorkflowParentId: "subworkflow",
    subWorkflowId: "child_assessment",
    foreignProject: "foreign",
    // The conformance fixture's second workflow — deliberately broken (its bundle
    // 422s on an undeclared profile), which the sidebar navigation tolerates.
    secondWorkflowId: "broken",
    environment: "local",
    secondEnvironment: "unreachable",
    // The fixture ships a single base policy (no extends chain) allowing anthropic.
    policyChainText: "base",
    policyRuleText: "allowed.anthropic.models",
    // The conformance fixture's resolvable workflow admits cleanly under its policy,
    // so the feed's honest state is the scoped empty confirmation (runtime portion
    // not_configured — no Langfuse observer in the fixture).
    enforcementFeed: { kind: "empty" },
    // The conformance fixture is likewise a local manifest mount, so the TS CP serves
    // github_provenance: false — same honest not-supported rendering as the python-cp lane.
    githubProvenance: { supported: false },
    // The conformance fixture ships two ack entries; `runtime.pin.*` is already EXPIRED
    // (expires 2026-01-01), so the console derives its stale-ack warning on the project feed.
    annotations: { count: 2, staleAckPattern: "runtime.pin.*" },
  },
}[LANE];
