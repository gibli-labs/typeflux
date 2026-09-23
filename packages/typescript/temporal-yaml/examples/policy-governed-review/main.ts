/**
 * Runnable entry point for the policy-governed review example (#454). It:
 *   1. composes an org + tenant project policy (most-restrictive merge);
 *   2. runs a COMPLIANT workflow live on a local Temporal dev server, with the
 *      composed policy enforced as a fail-closed pre-flight in `buildRuntime`;
 *   3. shows a NON-COMPLIANT workflow being REFUSED by the same pre-flight
 *      (it never connects or starts).
 *
 * Offline — the scripted provider needs no API key. See README.md for commands.
 */

import { readFileSync } from "node:fs";

import { Client, Connection } from "@temporalio/client";
import { NativeConnection } from "@temporalio/worker";
import { CollectingObserver } from "@typeflux/temporal";

import {
  buildRuntime,
  composeProjectPolicies,
  loadPolicySpec,
  loadYamlSpec,
  ProjectPolicyEnforcementError,
  validatePolicyCompliance,
  type PolicyValidationCheck,
} from "../../src/index.js";
import { contentSafetyModerator, ScriptedReviewProvider } from "./fakes.js";
import { sampleContent } from "./sample-input.js";
import { schemas } from "./schemas.js";

const policyPath = (file: string) => new URL(`./policies/${file}`, import.meta.url);
const specPath = (file: string) => new URL(`./${file}`, import.meta.url);

const loadPolicy = (file: string) =>
  loadPolicySpec(readFileSync(policyPath(file), "utf-8"), {
    sourceLabel: `examples/policy-governed-review/policies/${file}`,
  });
const loadSpec = (file: string) =>
  loadYamlSpec(readFileSync(specPath(file), "utf-8"), {
    sourceLabel: `examples/policy-governed-review/${file}`,
  });

function printChecks(checks: PolicyValidationCheck[]): void {
  for (const check of checks) {
    const mark = check.status === "passed" ? "✓" : check.status === "failed" ? "✗" : "·";
    const detail = check.message !== undefined ? ` — ${check.message}` : "";
    console.log(`    ${mark} ${check.code.padEnd(24)} ${check.status}${detail}`);
  }
}

/** Walk the `.cause` chain to the deepest message — a workflow failure nests the
 * activity failure. Depth-bounded so a cyclic cause chain can't loop forever. */
function rootCauseMessage(error: unknown): string {
  let current = error;
  let message = current instanceof Error ? current.message : String(current);
  for (let depth = 0; depth < 16 && current instanceof Error && current.cause !== undefined; depth += 1) {
    current = current.cause;
    if (current instanceof Error && current.message) {
      message = current.message;
    }
  }
  return message;
}

async function main(): Promise<void> {
  // 1. Compose the org baseline with the tenant overlay (org ∩ tenant).
  const org = { id: "acme-org", spec: loadPolicy("org.yaml") };
  const tenant = { id: "acme-eu-tenant", spec: loadPolicy("tenant.yaml") };
  const policy = composeProjectPolicies([org, tenant], [org.id, tenant.id]);
  const allowed = (policy.payload as { providers?: { allowed?: unknown } }).providers?.allowed;
  console.log(`Composed policy: ${policy.appliedPolicyIds.join(" + ")}`);
  console.log(`  hash:            ${policy.policyHash.slice(0, 16)}…`);
  console.log(`  allowed models:  ${JSON.stringify(allowed)}`);

  // 2. The compliant workflow — report the per-dimension checks, then run it live.
  const compliant = loadSpec("typeflux.yaml");
  console.log("\nCompliant workflow — policy report:");
  printChecks(validatePolicyCompliance(compliant, policy));

  const address = process.env["TEMPORAL_ADDRESS"] ?? "localhost:7233";
  const workerConnection = await NativeConnection.connect({ address });
  // The starter client must target the SAME server as the worker (a bare
  // `new Client()` would default to localhost:7233 regardless of TEMPORAL_ADDRESS).
  const clientConnection = await Connection.connect({ address });
  try {
    const runtime = await buildRuntime(compliant, {
      provider: new ScriptedReviewProvider(),
      schemas,
      // The governance pre-flight: a non-compliant spec throws before any worker
      // or provider is built. The compliant spec sails through.
      policy,
      // The org policy REQUIRES observability, and the runtime fail-closes when no observer
      // resolves (#756) — this offline example satisfies it with an injected observer (the
      // explicit-observer rule). A production run exports LANGFUSE_PUBLIC_KEY/SECRET_KEY and
      // lets the auto-wiring build the real langfuse observer instead.
      observer: new CollectingObserver(),
      worker: { connection: workerConnection },
      // Run the interpreter from source (swap for the dist path in a built app).
      workflowsPath: new URL("../../src/workflows.ts", import.meta.url).pathname,
    });
    const client = new Client({ connection: clientConnection });
    const assessment = await runtime.worker.runUntil(
      runtime.runWorkflow(client, sampleContent(), {
        workflowId: `policy-governed-review-${Date.now()}`,
      }),
    );
    console.log("\n✓ Compliant workflow ran under governance:");
    console.log(JSON.stringify(assessment, null, 2));

    // 3. The rogue workflow — the SAME pre-flight refuses it before it can start.
    const rogue = loadSpec("typeflux.rogue.yaml");
    console.log("\nRogue workflow (gpt-4o + redaction disabled) — policy report:");
    printChecks(validatePolicyCompliance(rogue, policy));
    try {
      await buildRuntime(rogue, {
        provider: new ScriptedReviewProvider(),
        schemas,
        policy,
        worker: { connection: workerConnection },
        workflowsPath: new URL("../../src/workflows.ts", import.meta.url).pathname,
      });
      throw new Error("expected the rogue workflow to be refused by the policy pre-flight");
    } catch (error) {
      if (!(error instanceof ProjectPolicyEnforcementError)) {
        throw error;
      }
      console.log("\n✗ Rogue workflow REFUSED by the pre-flight (never started):");
      console.log(`    ${error.message}`);
    }

    // 4. A moderated workflow — PASSES admission (semantics.categories is a runtime
    //    control), then is blocked DURING execution by the per-call moderation guard
    //    when the moderator reports a policy-forbidden category (slice 3).
    const moderated = loadSpec("typeflux.moderated.yaml");
    console.log("\nModerated workflow — policy report (semantics enforced at the runtime checkpoint):");
    printChecks(validatePolicyCompliance(moderated, policy));
    // An in-memory observer to show the moderation VERDICT the runtime records on the
    // activity trace — it lands even for a blocked output (redaction-exempt audit).
    const verdicts = new CollectingObserver();
    try {
      const runtime = await buildRuntime(moderated, {
        provider: new ScriptedReviewProvider(),
        schemas,
        policy,
        observer: verdicts,
        // Moderators are injected by ACTIVITY NAME; the spec's
        // `moderation.moderator` only names intent (use an injected moderator).
        moderators: { assess_content: contentSafetyModerator },
        worker: { connection: workerConnection },
        workflowsPath: new URL("../../src/workflows.ts", import.meta.url).pathname,
      });
      await runtime.worker.runUntil(
        runtime.runWorkflow(client, sampleContent(), {
          workflowId: `policy-governed-review-moderated-${Date.now()}`,
        }),
      );
      throw new Error("expected the moderated workflow to be blocked at the runtime checkpoint");
    } catch (error) {
      console.log("\n✗ Moderated workflow started, then BLOCKED at the runtime moderation checkpoint:");
      console.log(`    ${rootCauseMessage(error)}`);
      const verdict = verdicts.activities.find((a) => a.metadata["typeflux_moderation"] !== undefined)?.metadata[
        "typeflux_moderation"
      ];
      if (verdict !== undefined) {
        console.log(`  audit verdict recorded on the trace (survives the block): ${JSON.stringify(verdict)}`);
      }
    }
  } finally {
    await clientConnection.close();
    await workerConnection.close();
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
