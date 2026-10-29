import { expect, type Page, type Request, type Response } from "@playwright/test";

export const API_READINESS_TIMEOUT = 15_000;
type ExpectedApiError = { method: string; path: RegExp; status: number; reason: string };
export type DataRead = { path: string; query?: Record<string, string> };
export interface ApiHealth {
  allow(error: ExpectedApiError): void;
  settle(timeout?: number): Promise<void>;
  waitForData(reads: DataRead[], timeout?: number): Promise<void>;
}

/** Observe this page from fixture setup; never issue requests or retry failures. */
export function observeApiHealth(page: Page): ApiHealth & { dispose(): void } {
  const failures: string[] = [];
  const pending = new Set<Request>();
  const successful = new WeakSet<Request>();
  const completed: URL[] = [];
  const expected: ExpectedApiError[] = [];
  const isApi = (request: Request) => new URL(request.url()).pathname.startsWith("/api/");
  const onRequest = (request: Request) => { if (isApi(request)) pending.add(request); };
  const onResponse = (response: Response) => {
    const request = response.request();
    if (!isApi(request)) return;
    if (response.ok()) {
      successful.add(request);
      return;
    }
    const path = new URL(response.url()).pathname;
    if (!expected.some((error) => error.method === request.method() && error.status === response.status() && error.path.test(path))) {
      failures.push(`${request.method()} ${response.url()} returned ${response.status()}`);
    }
  };
  const onFinished = (request: Request) => {
    pending.delete(request);
    // Headers alone are not completed data. An allowed 4xx/5xx is not success.
    if (successful.has(request) && request.method() === "GET") completed.push(new URL(request.url()));
  };
  const onFailed = (request: Request) => {
    pending.delete(request);
    // Route changes intentionally abort obsolete React Query reads. Aborted
    // requests still cannot satisfy a required data read.
    if (isApi(request) && request.failure()?.errorText !== "net::ERR_ABORTED") {
      failures.push(`${request.method()} ${request.url()}: ${request.failure()?.errorText}`);
    }
  };
  page.on("request", onRequest);
  page.on("response", onResponse);
  page.on("requestfinished", onFinished);
  page.on("requestfailed", onFailed);
  const pendingUrls = () => [...pending].map((request) => `${request.method()} ${request.url()}`);
  return {
    allow: (error) => expected.push(error),
    async settle(timeout = API_READINESS_TIMEOUT) {
      await expect.poll(pendingUrls, { timeout, message: "background API requests settle" }).toEqual([]);
      expect(failures, "unexpected background API failures").toEqual([]);
    },
    async waitForData(reads, timeout = API_READINESS_TIMEOUT) {
      if (reads.length === 0) throw new Error("Data readiness requires at least one named GET request");
      await expect.poll(() => ({
        missing: reads.filter((read) => !completed.some((url) => url.pathname === read.path
          && Object.entries(read.query ?? {}).every(([key, value]) => url.searchParams.get(key) === value))),
        pending: pendingUrls(),
        failures: [...failures],
      }), { timeout, message: "successful fixture data readiness (missing reads, pending requests, unexpected failures)" })
        .toEqual({ missing: [], pending: [], failures: [] });
    },
    dispose() {
      page.off("request", onRequest);
      page.off("response", onResponse);
      page.off("requestfinished", onFinished);
      page.off("requestfailed", onFailed);
    },
  };
}
