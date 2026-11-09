import type { Page } from "@playwright/test";

/** Real fixture bodies/statuses, held beyond the former five-second UI deadline. */
export async function delayFixtureData(page: Page, paths = /\/api\/.*\/(bundle|policies\/[^/?]+)(\?|$)/): Promise<() => number> {
  let delivered = 0;
  await page.route(paths, async (route) => {
    const started = performance.now();
    const response = await route.fetch();
    if (response.ok()) {
      // Upstream fetching counts toward the minimum hold. Adding another six
      // seconds to an already slow response needlessly consumes readiness time.
      const remaining = 6_000 - (performance.now() - started);
      if (remaining > 0) await new Promise((resolve) => setTimeout(resolve, remaining));
      delivered += 1;
    }
    await route.fulfill({ response });
  });
  return () => delivered;
}
