/**
 * Exercises the code-defined review example on every CI run: the real example
 * activities + scripted provider run through `executeActivity` (no server), and
 * the shipped workflow module is asserted to export the workflows the example
 * drives. The workflow ORCHESTRATION (fan-out + child workflows) needs a server
 * and is covered by `main.ts`.
 */

import { describe, expect, it } from "vitest";

import { executeActivity, InlinePromptRegistry } from "@typeflux/temporal";

import { classifyDisclosure, substantiateClaim } from "../examples/code-defined-review/activities.js";
import { ScriptedReviewProvider } from "../examples/code-defined-review/fakes.js";

const registry = new InlinePromptRegistry({
  "review/classify": [{ role: "user", content: "Classify this disclosure:\n{{text}}" }],
  "review/substantiate": [{ role: "user", content: "Substantiate this claim: {{claim}}" }],
});

describe("code-defined-review example (#455)", () => {
  it("runs the two activities against the scripted provider", async () => {
    const provider = new ScriptedReviewProvider();

    const classification = await executeActivity(classifyDisclosure, { text: "Annual disclosure." }, { provider, registry });
    expect(classification).toEqual({ category: "financial-disclosure" });

    // The substantiator keys on the rendered claim text: a supported claim…
    const supported = await executeActivity(substantiateClaim, { claim: "Revenue grew 12%." }, { provider, registry });
    expect(supported).toEqual({ verdict: "substantiated", supported: true });

    // …and an "unverified" one comes back unsupported — the mixed verdict a
    // fan-out would surface.
    const unsupported = await executeActivity(
      substantiateClaim,
      { claim: "An unverified partnership doubled reach." },
      { provider, registry },
    );
    expect(unsupported).toEqual({ verdict: "insufficient_evidence", supported: false });
  });

  it("the shipped workflow module exports the workflows the example drives", async () => {
    const workflows = await import("../src/workflows.js");
    expect(typeof workflows.reviewWorkflow).toBe("function");
    expect(typeof workflows.batchReviewWorkflow).toBe("function");
  });
});
