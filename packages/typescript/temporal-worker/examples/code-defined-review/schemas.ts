/**
 * Activity I/O schemas for the code-defined review example. These match the
 * structural `ReviewActivities` contract the shipped `reviewWorkflow` proxies
 * (`packages/typescript/temporal-worker/src/workflows.ts`) — the workflow calls
 * `classifyDisclosure` and `substantiateClaim` by name.
 */

import { z } from "zod";

export const DisclosureInput = z.object({
  text: z.string().describe("The disclosure text to classify."),
});
export type DisclosureInput = z.infer<typeof DisclosureInput>;

export const Classification = z.object({
  category: z.string().describe("Disclosure category."),
});
export type Classification = z.infer<typeof Classification>;

export const ClaimInput = z.object({
  claim: z.string().describe("A single claim to substantiate."),
});
export type ClaimInput = z.infer<typeof ClaimInput>;

export const Substantiation = z.object({
  verdict: z.string().describe("Substantiation verdict."),
  supported: z.boolean().describe("Whether the evidence supports the claim."),
});
export type Substantiation = z.infer<typeof Substantiation>;
