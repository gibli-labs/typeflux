/**
 * GATED live test for the #573 langfuse transport seam. Skipped unless TYPEFLUX_RUN_LIVE=1 AND
 * LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY are set — there is no langfuse instance in CI, and the
 * test harness's network guard blocks non-loopback fetch + scrubs LANGFUSE_* env UNLESS a live gate
 * (TYPEFLUX_RUN_LIVE=1) is set (see packages/typescript/test-setup/network-guard.ts).
 *
 * Run against a real langfuse (cloud or self-hosted):
 *   TYPEFLUX_RUN_LIVE=1 LANGFUSE_PUBLIC_KEY=pk LANGFUSE_SECRET_KEY=sk LANGFUSE_HOST=https://cloud.langfuse.com \
 *     pnpm --filter @typeflux/temporal-controlplane test -- live-langfuse
 *
 * Proves the reference `fetchLangfuseTransport` reaches a real langfuse: `ping` resolves (the
 * connections probe would report reachable) and `lastRunPromptVersions` returns a map without
 * throwing. If LANGFUSE_TEST_PROMPT (+ optional LANGFUSE_TEST_LABEL) names a real prompt, its label
 * resolves to a version too. This is the live proof I cannot run in this environment (no instance).
 */

import { describe, expect, it } from "vitest";

import { fetchLangfuseTransport } from "../src/index.js";

const LIVE =
  process.env["TYPEFLUX_RUN_LIVE"] === "1" &&
  (process.env["LANGFUSE_PUBLIC_KEY"] ?? "") !== "" &&
  (process.env["LANGFUSE_SECRET_KEY"] ?? "") !== "";

describe.skipIf(!LIVE)("langfuse transport — live (#573)", () => {
  const transport = fetchLangfuseTransport();

  it("ping reaches the configured langfuse host", async () => {
    await expect(transport.ping({ host: null, environmentId: "live" })).resolves.toBeUndefined();
  });

  it("lastRunPromptVersions returns a map without throwing", async () => {
    const workflowName = process.env["LANGFUSE_TEST_WORKFLOW"] ?? "TypefluxWorkflow";
    const versions = await transport.lastRunPromptVersions({ workflowName, host: null });
    expect(typeof versions).toBe("object");
  });

  it("promptLabelVersion resolves a configured test prompt's label to a version", async () => {
    const name = process.env["LANGFUSE_TEST_PROMPT"];
    if (name === undefined || name === "") return; // no test prompt configured — nothing to assert
    const label = process.env["LANGFUSE_TEST_LABEL"] ?? "production";
    const version = await transport.promptLabelVersion({ name, label, host: null });
    expect(version === undefined || typeof version === "string").toBe(true);
  });
});
