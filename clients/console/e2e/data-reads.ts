import type { DataRead } from "./api-health";
import { EXPECT } from "./expectations";

// A fresh browser context uses the server default project (no saved selection).
// Named bundle successes anchor readiness after overview/matrix fan-out starts.
// The health fixture also waits for every other pending API request and rejects
// unexpected failures; rendered content is checked after the reads settle.
export const bundleReads = (project?: string): DataRead[] => {
  const prefix = project ? `/api/v1/projects/${project}` : "/api/v1";
  return [EXPECT.environment, EXPECT.secondEnvironment].map((environment) => ({
    path: `${prefix}/workflows/${EXPECT.workflowId}/bundle`,
    query: { environment_id: environment },
  }));
};
export const matrixReads = bundleReads();
export const governanceReads: DataRead[] = [...matrixReads, { path: "/api/v1/policies/base" }];
