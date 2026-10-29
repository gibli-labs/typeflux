import { test as base } from "@playwright/test";

import { observeApiHealth, type ApiHealth } from "./api-health";
import { LANE } from "./expectations";
export type { ApiHealth } from "./api-health";

export const test = base.extend<{ apiHealth: ApiHealth }>({
  apiHealth: [async ({ page }, use) => {
    const health = observeApiHealth(page);
    // Both fixtures deliberately run without Temporal. These bounded runtime
    // reads exercise unavailable-state rendering; bundle/configuration reads must succeed.
    health.allow({ method: "GET", path: /\/workflows\/[^/]+\/executions$/, status: 503, reason: "No Temporal in the read-only fixture" });
    if (LANE === "ts-cp") {
      health.allow({
        method: "GET", path: /\/workflows\/broken\/(bundle|catalog|prompt-status|versions|executions)$/, status: 422,
        reason: "Conformance fixture deliberately references an undeclared profile; its resolution-backed reads reject it",
      });
    }
    try {
      await use(health);
      // Retain teardown coverage for unexpected errors in other background reads.
      await health.settle();
    } finally {
      health.dispose();
    }
  }, { auto: true }],
});

export { expect } from "@playwright/test";
