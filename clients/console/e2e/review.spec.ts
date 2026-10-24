import type { Page } from "@playwright/test";
import { expect, test, type ApiHealth } from "./fixtures";

import { EXPECT } from "./expectations";

// Browser contract tests: real console + real lane's read endpoints, with controlled
// operation responses. Temporal progress is independently verified by the two live
// control-plane harnesses; these fixtures must never be reported as live evidence.
async function reviewFixture(page: Page, apiHealth: ApiHealth, options: { canReview?: boolean; statusError?: number; reviewError?: number } = {}) {
  apiHealth.allow({ method: "GET", path: /\/correlation$/, status: 503, reason: "Synthetic review execution has no Temporal correlation" });
  if (options.statusError) apiHealth.allow({ method: "GET", path: /\/status$/, status: options.statusError, reason: "Deliberate execution binding error" });
  if (options.reviewError) apiHealth.allow({ method: "POST", path: /\/review$/, status: options.reviewError, reason: "Deliberate review rejection or outage" });
  await page.addInitScript((project) => localStorage.setItem("typeflux.project", project), EXPECT.defaultProject);
  let state = "waiting_for_review";
  let reviewError = options.reviewError;
  const submissions: Array<{ url: string; body: Record<string, unknown> }> = [];
  const requests: string[] = [];
  // Scoped requests carry a ReadableStream body after URL rewriting. Chromium's
  // interception API omits streamed postData; inspect a clone at the fetch boundary.
  await page.exposeFunction("recordGraphReview", (submission: typeof submissions[number]) => submissions.push(submission));
  await page.addInitScript(() => {
    const original = window.fetch;
    window.fetch = async (input, init) => {
      const request = input instanceof Request ? input.clone() : new Request(input, init);
      if (new URL(request.url).pathname.endsWith("/review")) {
        await (window as unknown as { recordGraphReview: (value: unknown) => Promise<void> }).recordGraphReview({ url: new URL(request.url).pathname, body: await request.json() });
      }
      return original(input, init);
    };
  });
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    // The addressed execution is synthetic, so its listing is controlled too.
    if (path.endsWith("/executions")) return route.fulfill({ json: {
      logical_workflow: EXPECT.workflowId, current_workflow_type: EXPECT.workflowName, executions: [],
    } });
    if (path.endsWith("/correlation")) return route.fulfill({ status: 503, json: { error: "unavailable", message: "No Temporal correlation for the synthetic review execution" } });
    if (path.endsWith("/meta") && options.canReview === false) {
      const response = await route.fetch();
      const body = await response.json();
      body.capabilities.can_review = false;
      return route.fulfill({ response, json: body });
    }
    if (path.endsWith("/status")) {
      if (options.statusError) return route.fulfill({ status: options.statusError, json: { error: "LifecycleBindingError", message: "execution binding mismatch" } });
      return route.fulfill({ json: {
        workflow_id: "graph-review-001", run_id: "graph-run-001",
        status: { state, current_step: state === "completed" ? "finalize" : "assess", completed_units: state === "completed" ? 2 : 1, total_units: 2, cancellation_requested: false },
        valid_user_decisions: { approve: "finalize" }, recommended_poll_interval_seconds: 1,
      } });
    }
    if (path.endsWith("/review")) {
      requests.push(path);
      if (reviewError) return route.fulfill({ status: reviewError, json: { error: "ReviewError", message: reviewError === 422 ? "invalid decision" : "backend unavailable; reconcile status before retry" } });
      state = "completed";
      return route.fulfill({ status: 204 });
    }
    return route.continue();
  });
  await page.goto(`/#/workflows/${EXPECT.workflowId}/runs?env=${EXPECT.environment}&run=graph-review-001`);
  return { submissions, requests, recover: () => { reviewError = undefined; } };
}

test("review journey submits to the selected execution and observes progress", async ({ page, apiHealth }) => {
  const fixture = await reviewFixture(page, apiHealth);
  await expect(page.locator("#status")).toContainText("waiting_for_review");
  await page.getByRole("button", { name: "approve → finalize", exact: true }).click();
  await expect(page.locator("#status").getByText("completed", { exact: true })).toBeVisible();
  expect(fixture.submissions).toHaveLength(1);
  expect(fixture.submissions[0]!.url).toContain(`/projects/${EXPECT.defaultProject}/workflows/${EXPECT.workflowId}/review`);
  expect(fixture.submissions[0]!.body).toMatchObject({ environment_id: EXPECT.environment, execution_id: "graph-review-001", command: { user_decision: "approve" } });
  await expect(page.getByRole("button", { name: "approve → finalize", exact: true })).toBeDisabled();
});

test("review journey respects permission denial without submitting", async ({ page, apiHealth }) => {
  const fixture = await reviewFixture(page, apiHealth, { canReview: false });
  await expect(page.getByRole("button", { name: "approve → finalize", exact: true })).toBeDisabled();
  expect(fixture.submissions).toHaveLength(0);
});

test("review journey reports binding mismatch without offering a decision", async ({ page, apiHealth }) => {
  const fixture = await reviewFixture(page, apiHealth, { statusError: 409 });
  await expect(page.getByText("execution binding mismatch", { exact: false }).first()).toBeVisible();
  await expect(page.getByRole("button", { name: "approve → finalize", exact: true })).toHaveCount(0);
  expect(fixture.submissions).toHaveLength(0);
});

test("review journey surfaces an invalid decision without claiming completion", async ({ page, apiHealth }) => {
  await reviewFixture(page, apiHealth, { reviewError: 422 });
  await page.getByRole("button", { name: "approve → finalize", exact: true }).click();
  await expect(page.getByText("invalid decision", { exact: false }).first()).toBeVisible();
  await expect(page.locator("#status")).toContainText("waiting_for_review");
});

test("review journey recovers after backend failure without automatic resubmission", async ({ page, apiHealth }) => {
  const fixture = await reviewFixture(page, apiHealth, { reviewError: 503 });
  const decision = page.getByRole("button", { name: "approve → finalize", exact: true });
  await decision.click();
  await expect(page.getByText("backend unavailable", { exact: false }).first()).toBeVisible();
  expect(fixture.submissions).toHaveLength(1);
  fixture.recover();
  // A fresh inspection reconciles the status before an explicit operator retry.
  await apiHealth.settle();
  await page.reload();
  await expect(page.locator("#status")).toContainText("waiting_for_review");
  await decision.click();
  await expect(page.locator("#status").getByText("completed", { exact: true })).toBeVisible();
  expect(fixture.requests).toHaveLength(2);
});
