import { expect, test, type Page } from "@playwright/test";
import { observeApiHealth } from "./api-health";

const data = { path: "/api/readiness-data", query: { environment_id: "local" } };
const dataUrl = `${data.path}?environment_id=local`;

async function probe(page: Page) {
  // A tiny document isolates the observer from the console's own fan-out. The
  // main smoke specs below exercise real fixture data and the rendered console.
  await page.route("**/readiness-probe", (route) => route.fulfill({ contentType: "text/html", body: "<main>probe</main>" }));
  await page.goto("/readiness-probe");
  return observeApiHealth(page);
}

async function fetchData(page: Page, url = dataUrl) {
  await page.evaluate(async (path) => { await fetch(path).then((response) => response.text()); }, url);
}

test("data readiness requires the named successful GET and preserves missing-content failures", async ({ page }) => {
  const health = await probe(page);
  try {
    await page.route("**/api/readiness-data?*", (route) => route.fulfill({ json: { ready: true } }));
    await fetchData(page, `${data.path}?environment_id=other`);
    await expect(health.waitForData([data], 150)).rejects.toThrow(/environment_id/);
    await fetchData(page);
    await health.waitForData([data]);
    await expect(expect(page.getByText("absent fixture content")).toBeVisible({ timeout: 150 })).rejects.toThrow();
    await health.settle();
  } finally { health.dispose(); }
});

test("data readiness rejects unexpected API errors even after a successful retry", async ({ page }) => {
  const health = await probe(page);
  let status = 500;
  try {
    await page.route("**/api/readiness-data?*", (route) => route.fulfill({ status, json: { ready: status === 200 } }));
    await fetchData(page);
    await expect(health.waitForData([data], 150)).rejects.toThrow(/returned 500/);
    status = 200;
    await fetchData(page);
    await expect(health.waitForData([data], 150)).rejects.toThrow(/returned 500/);
    await expect(health.settle()).rejects.toThrow(/returned 500/);
  } finally { health.dispose(); }
});

test("allowed unavailable responses settle but cannot satisfy successful data readiness", async ({ page }) => {
  const health = await probe(page);
  try {
    health.allow({ method: "GET", path: /^\/api\/readiness-data$/, status: 503, reason: "deliberate outage" });
    await page.route("**/api/readiness-data?*", (route) => route.fulfill({ status: 503, json: {} }));
    await fetchData(page);
    await health.settle();
    await expect(health.waitForData([data], 150)).rejects.toThrow(/readiness-data/);
  } finally { health.dispose(); }
});

test("permanently pending API reads fail within the readiness budget with their URL", async ({ page }) => {
  const health = await probe(page);
  let release!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  let started!: () => void;
  const issued = new Promise<void>((resolve) => { started = resolve; });
  await page.route("**/api/readiness-data?*", async (route) => {
    started();
    await gate;
    await route.fulfill({ json: {} });
  });
  const request = fetchData(page);
  try {
    await issued;
    await expect(health.waitForData([data], 150)).rejects.toThrow(/pending[\s\S]*readiness-data/);
    await expect(health.settle(150)).rejects.toThrow(/readiness-data/);
  } finally {
    release();
    await request;
    health.dispose();
  }
});
