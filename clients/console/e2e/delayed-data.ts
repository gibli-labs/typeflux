import { setTimeout as delay } from "node:timers/promises";
import type { Page } from "@playwright/test";

/** Real fixture bodies/statuses, held beyond the former five-second UI deadline. */
export async function delayFixtureData(page: Page): Promise<() => number> {
  let delivered = 0;
  await page.route(/\/api\/.*\/(bundle|policies\/[^/?]+)(\?|$)/, async (route) => {
    const response = await route.fetch();
    if (response.ok()) {
      await delay(6_000);
      delivered += 1;
    }
    await route.fulfill({ response });
  });
  return () => delivered;
}
