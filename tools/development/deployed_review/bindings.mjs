import { appendFileSync } from "node:fs";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";
import { dirname, join } from "node:path";
import { Context } from "@temporalio/activity";
import { ProviderTransientError } from "@typeflux/temporal";

// Installed candidate's canonical fixture schemas, not an approximation or HTTP stub.
const require = createRequire(import.meta.url);
const entry = require.resolve("@typeflux/temporal-controlplane");
export const { CONFORMANCE_SCHEMAS: schemas } = await import(
  pathToFileURL(join(dirname(entry), "http/conformance-schemas.js")).href);
export const provider = {
  providerName: "fake",
  structuredCall() {
    const info = Context.current().info;
    appendFileSync("/data/effects.jsonl", JSON.stringify({activity: info.activityType,
      attempt: info.attempt, runId: info.workflowExecution.runId, timeMs: Date.now()}) + "\n");
    if (process.env.RETRY_FIXTURE === "1" && info.activityType === "assess_item" && info.attempt === 1)
      throw new ProviderTransientError("fixed deployment test fault");
    return {value: "1", reviews: [{value: "1"}]};
  },
};
