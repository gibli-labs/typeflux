// Real production console + real HTTP API. No request/response interception.
import { createRequire } from "node:module";
const [moduleRoot, base, executionId, runId, mode = "approve"] = process.argv.slice(2);
const require = createRequire(`${moduleRoot}/package.json`);
const { chromium } = require("@playwright/test");
const browser = await chromium.launch({headless: true});
const errors = [];
const submissions = [];
const observedRuns = [];
const observedStates = [];
const responseReads = [];
try {
  const page = await browser.newPage();
  page.on("requestfailed", (request) => {
    if (new URL(request.url()).pathname.startsWith("/api/"))
      errors.push({method: request.method(), path: new URL(request.url()).pathname,
        transport: request.failure()?.errorText ?? "unknown"});
  });
  page.on("response", (response) => {
    if (new URL(response.url()).pathname.startsWith("/api/") && response.status() >= 400)
      errors.push({method: response.request().method(), status: response.status(),
        path: new URL(response.url()).pathname});
    if (new URL(response.url()).pathname.endsWith("/status") && response.status() === 200)
      responseReads.push(response.json().then((body) => {
        observedRuns.push(body.run_id);
        observedStates.push(body.status.state);
      }, () => errors.push({error: "status-body-unavailable"})));
  });
  await page.exposeFunction("captureDeployedDecision", (body) => {
      // The released console addresses the current run by execution ID. The API
      // also supports an explicit run_id; the separate HTTP probes exercise that.
      if (body.execution_id !== executionId || (body.run_id && body.run_id !== runId))
        errors.push({error: "browser decision addressed another execution/run"});
      submissions.push({executionId: body.execution_id, runId: body.run_id,
        decision: body.command.user_decision});
  });
  await page.addInitScript(() => {
    localStorage.setItem("typeflux.project", "deployment");
    localStorage.setItem("typeflux.apiToken", "test-operator");
    // Read a clone at the fetch boundary and forward the original unchanged.
    // This also observes a streamed request that Chromium's event body omits.
    const original = window.fetch;
    window.fetch = async (input, init) => {
      const request = input instanceof Request ? input.clone() : new Request(input, init);
      if (new URL(request.url).pathname.endsWith("/review") && request.method === "POST")
        await window.captureDeployedDecision(await request.json());
      return original(input, init);
    };
  });
  await page.goto(`${base}/#/workflows/workflow/runs?env=local&run=${executionId}`);
  await page.locator("#status").getByText("waiting_for_review", {exact: true}).waitFor({timeout: 30000});
  const decision = page.getByRole("button", {name: "approve → summarize", exact: true});
  if (mode === "approve") {
    await page.getByRole("checkbox", {name: /auto-refresh/}).check();
    await decision.click();
    await page.locator("#status").getByText("completed", {exact: true}).waitFor({timeout: 30000});
    if (submissions.length !== 1) throw new Error("expected one browser decision");
  }
  await Promise.all(responseReads);
  if (!observedRuns.length || observedRuns.some((id) => id != null && id !== runId))
    throw new Error("browser observed another run or no successful status");
  if (errors.length) throw new Error(`unexpected real API errors: ${JSON.stringify(errors)}`);
  console.log(JSON.stringify({passed: true, mode, realApi: true, interceptedResponses: 0,
    observed: mode === "approve" ? "completed" : "waiting_for_review", submissions,
    addressing: "console current-run execution ID; nullable status run ID; native history reconciled separately",
    observedRuns, observedStates}));
} catch (error) {
  await Promise.allSettled(responseReads);
  console.log(JSON.stringify({passed: false, mode, errorClass: error.name,
    errors, submissions, observedRuns, observedStates}));
  throw error;
} finally { await browser.close(); }
