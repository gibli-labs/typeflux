import { expect, test as base, type Request } from "@playwright/test";

import { LANE } from "./expectations";

type ExpectedApiError = { method: string; path: RegExp; status: number; reason: string };
export interface ApiHealth {
  allow(error: ExpectedApiError): void;
  settle(): Promise<void>;
}

export const test = base.extend<{ apiHealth: ApiHealth }>({
  apiHealth: [async ({ page }, use) => {
    const failures: string[] = [];
    const pending = new Set<Request>();
    const expected: ExpectedApiError[] = [];
    // Both fixtures deliberately run without Temporal. These bounded runtime
    // reads exercise unavailable-state rendering; bundle/configuration reads must succeed.
    expected.push({ method: "GET", path: /\/workflows\/[^/]+\/executions$/, status: 503, reason: "No Temporal in the read-only fixture" });
    if (LANE === "ts-cp") {
      expected.push({
        method: "GET", path: /\/workflows\/broken\/(bundle|catalog|prompt-status|versions|executions)$/, status: 422,
        reason: "Conformance fixture deliberately references an undeclared profile; its resolution-backed reads reject it",
      });
    }
    const isApi = (request: Request) => new URL(request.url()).pathname.startsWith("/api/");
    page.on("request", (request) => { if (isApi(request)) pending.add(request); });
    page.on("response", (response) => {
      const request = response.request();
      if (!isApi(request) || response.ok()) return;
      const path = new URL(response.url()).pathname;
      if (!expected.some((error) => error.method === request.method() && error.status === response.status() && error.path.test(path))) {
        failures.push(`${request.method()} ${response.url()} returned ${response.status()}`);
      }
    });
    page.on("requestfinished", (request) => pending.delete(request));
    page.on("requestfailed", (request) => {
      pending.delete(request);
      // Route changes intentionally abort obsolete React Query reads.
      if (isApi(request) && request.failure()?.errorText !== "net::ERR_ABORTED") {
        failures.push(`${request.method()} ${request.url()}: ${request.failure()?.errorText}`);
      }
    });
    // Let already-issued background reads finish before asserting. Checking only
    // pageerror or the first rendered row misses fetch errors in other bundles.
    const settle = async () => {
      await expect.poll(() => [...pending].map((request) => request.url()), { timeout: 15_000, message: "background API requests settle" }).toEqual([]);
      expect(failures, "unexpected background API failures").toEqual([]);
    };
    await use({ allow: (error) => expected.push(error), settle });
    await settle();
  }, { auto: true }],
});

export { expect } from "@playwright/test";
