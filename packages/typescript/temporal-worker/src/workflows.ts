/**
 * Example code-defined Typeflux workflows (parity Epic 3, #450; Python #396
 * child-workflow conventions). This module is the worker's `workflowsPath` entry —
 * it runs in the Temporal **workflow sandbox**, so it must contain only
 * deterministic, sandbox-safe code: it calls activities via `proxyActivities` and
 * uses the import-pure composition primitive `fanOut` (from the core's
 * `@typeflux/temporal/composition` subpath — NOT the barrel, which pulls `node:crypto`).
 *
 * It is intentionally NOT re-exported from the package index: that barrel imports
 * `@temporalio/worker` + node APIs, which the workflow bundler must not pull in.
 */

import { executeChild, proxyActivities, workflowInfo } from "@temporalio/workflow";

import { fanOut } from "@typeflux/temporal/composition";

/**
 * The activity contract this workflow calls — the names match what a worker
 * registers via `buildTemporalActivities`. (A workflow may only `import type` the
 * concrete schemas; these structural shapes keep the sandbox bundle pure.)
 */
export interface ReviewActivities {
  classifyDisclosure(input: { text: string }): Promise<{ category: string }>;
  substantiateClaim(input: { claim: string }): Promise<{ verdict: string; supported: boolean }>;
}

const { classifyDisclosure, substantiateClaim } = proxyActivities<ReviewActivities>({
  startToCloseTimeout: "1 minute",
});

export interface ReviewWorkflowInput {
  text: string;
  claims: string[];
}

export interface ReviewWorkflowResult {
  category: string;
  verdicts: { claim: string; verdict: string; supported: boolean }[];
}

/**
 * A durable review workflow: classify the disclosure, then fan out claim
 * substantiation with bounded concurrency. `fanOut` is determinism-safe in a
 * workflow — it is pure Promise orchestration over the proxied activity calls (no
 * `Date`/`Math.random`/IO), so its scheduling replays identically.
 */
export async function reviewWorkflow(input: ReviewWorkflowInput): Promise<ReviewWorkflowResult> {
  const { category } = await classifyDisclosure({ text: input.text });
  const verdicts = await fanOut(
    input.claims,
    async (claim) => {
      const result = await substantiateClaim({ claim });
      return { claim, verdict: result.verdict, supported: result.supported };
    },
    { concurrency: 5 },
  );
  return { category, verdicts };
}

/**
 * Child-workflow composition: run each review as a durable child workflow, bounded
 * by `fanOut`. Child workflow ids are derived deterministically from the parent id +
 * index so replay is stable.
 */
export async function batchReviewWorkflow(inputs: ReviewWorkflowInput[]): Promise<ReviewWorkflowResult[]> {
  const parentId = workflowInfo().workflowId;
  const tagged = inputs.map((input, index) => ({ input, workflowId: `${parentId}/review-${index}` }));
  return fanOut(
    tagged,
    ({ input, workflowId }) => executeChild(reviewWorkflow, { args: [input], workflowId }),
    { concurrency: 3 },
  );
}
