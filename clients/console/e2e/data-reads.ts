import type { DataRead } from "./api-health";
import { EXPECT } from "./expectations";

// A fresh browser context uses the server default project (no saved selection).
const prefix = "/api/v1";
// These required successes anchor readiness after the page's matrix fan-out has
// started. The health fixture also waits for every other pending API request and
// rejects unexpected failures; rendered rows/rules/source links are checked next.
export const matrixReads: DataRead[] = [EXPECT.environment, EXPECT.secondEnvironment].map((environment) => ({
  path: `${prefix}/workflows/${EXPECT.workflowId}/bundle`,
  query: { environment_id: environment },
}));
export const governanceReads: DataRead[] = [...matrixReads, { path: `${prefix}/policies/base` }];
