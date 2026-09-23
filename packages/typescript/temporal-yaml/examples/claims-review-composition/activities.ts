/**
 * Code-injected activities (the YAML+code authoring mode). `finalize` is a
 * code-DEFINED activity descriptor passed to `buildRuntime` via `extraActivities`
 * instead of declared inline in the YAML — the TS analogue of Python's
 * `activities.modules`. The referenced sub-workflows and the parent's other two
 * activities stay pure-YAML — both authoring modes composed in one project.
 */

import { defineActivity } from "@typeflux/temporal";
import { z } from "zod";

export const finalize = defineActivity({
  name: "finalize",
  prompt: { name: "finalize-review" },
  input: z.object({ summary: z.string(), escalate: z.boolean() }),
  output: z.object({ decision: z.string() }),
});

export const extraActivities = { finalize };
