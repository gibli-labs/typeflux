import { expect, test } from "./fixtures";
import { EXPECT } from "./expectations";

// Real HTTP/1 transport through Vite and each real control-plane edition.
// The command is deliberately malformed; no operation response is fulfilled or workflow dispatched.
test("scoped review POST reaches the real API and retains its validation error", async ({ page, apiHealth }) => {
  apiHealth.allow({ method: "POST", path: /\/review$/, status: 422,
    reason: "Deliberately malformed user_decision; actual API validation response" });
  await page.addInitScript((project) => localStorage.setItem("typeflux.project", project), EXPECT.defaultProject);
  await page.goto("/");
  const responses: Array<Promise<{path: string; status: number; body: unknown}>> = [];
  page.on("response", (response) => {
    if (response.request().method() === "POST" && new URL(response.url()).pathname.endsWith("/review")) {
      const path = new URL(response.url()).pathname;
      const status = response.status();
      responses.push(response.json().then(body => ({path, status, body}),
        () => ({path, status, body: {bodyUnavailable: true}})));
    }
  });
  const outcome = await page.evaluate(async ({ workflow, environment }) => {
    const modulePath = "/src/api.ts";
    const api = await import(/* @vite-ignore */ modulePath);
    try {
      await api.submitReview(workflow, { environment_id: environment,
        execution_id: "real-transport-invalid-command", command: { user_decision: [] } });
      return { name: "unexpected-success", status: 0 };
    } catch (error) {
      const failure = error as { name: string; status?: number };
      return { name: failure.name, status: failure.status ?? 0 };
    }
  }, { workflow: EXPECT.workflowId, environment: EXPECT.environment });
  expect(outcome).toEqual({ name: "ApiRequestError", status: 422 });
  const wire = await Promise.all(responses);
  expect(wire).toHaveLength(1);
  expect(wire[0]!.path).toBe(`/api/v1/projects/${EXPECT.defaultProject}/workflows/${EXPECT.workflowId}/review`);
  expect(wire[0]!.status).toBe(422);
  expect(JSON.stringify(wire[0]!.body)).toContain("user_decision");
});
