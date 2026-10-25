/** One opt-in synthetic request; credentials never appear in diagnostics. */
import { expect, it } from "vitest";
import { TypeSafeProvider, type ProviderUsage } from "@typeflux/temporal";
import { loadYamlSpec, providerTransportsFromSpec } from "../src/index.js";

it.skipIf(process.env["TYPEFLUX_RUN_LIVE"] !== "1" || !process.env["TYPESAFE_API_KEY"])(
  "live TypeSafe choice score and noul", async () => {
    const spec = loadYamlSpec(`
project: smoke
name: smoke
task_queue: smoke
runtime:
  temporal: {}
  registry: {type: inline}
  provider: {type: typesafe}
activities: {}
workflow:
  name: Smoke
  input: schemas:Input
  steps: [{id: smoke, activity: smoke}]
`, { env: {} });
    const usage: ProviderUsage[] = [];
    let result: unknown;
    try {
      const transports = await providerTransportsFromSpec(spec);
      const provider = new TypeSafeProvider(transports!.typesafe!);
      result = await provider.structuredCall({
        messages: [{ role: "system", content: JSON.stringify({
          route: { type: "choice", instructions: "Route this ticket", criteria: { billing: "Payments and refunds", technical: "Software bugs" } },
          urgency: { type: "score", instructions: "Urgency", criteria: ["Routine", "Urgent"] },
          refund: { type: "noul", instructions: "Requests a refund" },
        }) }, { role: "user", content: "Please refund my duplicate charge." }],
        outputSchema: {}, providerParams: { timeout: 20 }, signal: AbortSignal.timeout(25_000), usageSink: (value) => usage.push(value),
      });
    } catch {
      throw new Error("TypeSafe live request failed; no retry was made");
    }
    expect(result).toMatchObject({ route: { choice: "billing" }, urgency: { type: "score" }, refund: { type: "noul" } });
    expect(usage[0]?.inputTokens).toBeTypeOf("number");
    console.log("TypeSafe smoke", { provider: "typesafe", ...usage[0] });
  }, 30_000,
);
