import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

describe.skipIf(process.env["TYPEFLUX_LIVE_TEMPORAL"] !== "1")("documented review adoption", () => {
  it("observes the bound gate, selected route and result through the documented SDK caller", async () => {
    const environment: NodeJS.ProcessEnv = {...process.env, TEMPORAL_ADDRESS: "localhost:7233", TEMPORAL_NAMESPACE: "default", LIFECYCLE_REVIEW_DECISION: "send_email"};
    delete environment["LANGFUSE_PUBLIC_KEY"];
    delete environment["LANGFUSE_SECRET_KEY"];
    const cli = fileURLToPath(new URL("../node_modules/tsx/dist/cli.mjs", import.meta.url));
    const script = fileURLToPath(new URL("../examples/lifecycle-review/main.ts", import.meta.url));
    const {stdout} = await promisify(execFile)(process.execPath, [cli, script], {env: environment, timeout: 75000});
    const observation = JSON.parse(stdout.trim().split("\n").at(-1)!);
    expect(observation).toEqual({scenario: "review-adoption", sameRun: true,
      waitingCheckpoint: "package_for_review", reviewRoute: "send_email", state: "completed",
      stepsStarted: ["assess_case", "package_for_review", "send_email"],
      result: {case_id: "CASE-2026-0101", decision: "approved", summary: "Human approval was received and the case can proceed.", approved: true}});
  }, 80000);
  it("preserves a rejected start and submits no review decision", async () => {
    const directory = mkdtempSync(fileURLToPath(new URL("../node_modules/review-rejection-", import.meta.url)));
    const environment: NodeJS.ProcessEnv = {...process.env, TEMPORAL_ADDRESS: "localhost:7233", TEMPORAL_NAMESPACE: "default", LIFECYCLE_REVIEW_DECISION: "send_email"};
    delete environment["LANGFUSE_PUBLIC_KEY"];
    delete environment["LANGFUSE_SECRET_KEY"];
    try {
      const script = fileURLToPath(new URL("../examples/lifecycle-review/main.ts", import.meta.url));
      const harness = `${directory}/reject-start.mjs`;
      writeFileSync(harness, `
import { WorkflowClient } from ${JSON.stringify(import.meta.resolve("@temporalio/client"))};
let starts = 0, signals = 0;
WorkflowClient.prototype.start = async function() {
  starts++;
  throw new Error("owned start rejection");
};
const getHandle = WorkflowClient.prototype.getHandle;
WorkflowClient.prototype.getHandle = function(...args) {
  const handle = getHandle.apply(this, args);
  handle.signal = async () => { signals++; throw new Error("unexpected review signal"); };
  return handle;
};
process.on("beforeExit", () => console.error(JSON.stringify({probe: "start-rejection", starts, signals})));
await import(${JSON.stringify(script)});
`);
      let failure: unknown;
      try {
        await promisify(execFile)(process.execPath, ["--import", "tsx", harness], {env: environment, timeout: 30000, killSignal: "SIGKILL"});
      } catch (error) { failure = error; }
      expect(failure).toMatchObject({code: 1, killed: false});
      const stderr = (failure as {stderr: string}).stderr;
      expect(stderr).toContain("owned start rejection");
      expect(stderr).not.toContain("gate must be observed before any decision");
      const observation = stderr.split("\n").find(line => line.startsWith('{"probe":"start-rejection"'));
      expect(JSON.parse(observation!)).toEqual({probe: "start-rejection", starts: 1, signals: 0});
    } finally { rmSync(directory, {recursive: true, force: true}); }
  }, 35000);

});
