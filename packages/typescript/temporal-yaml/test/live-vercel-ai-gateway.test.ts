/** One opt-in request. Never load dotenv or retry a failure. */
import { expect, it } from "vitest";
import { z } from "zod";
import {
  defineActivity, executeActivity, ProviderAuthError, ProviderConfigError,
  ProviderRateLimitError, ProviderTransientError, type ProviderUsage,
} from "@typeflux/temporal";
import { loadYamlSpec, providerFromSpec, providerTransportsFromSpec } from "../src/index.js";

it.skipIf(process.env["TYPEFLUX_RUN_LIVE"] !== "1" || !process.env["AI_GATEWAY_API_KEY"])(
  "live Vercel AI Gateway bounded structured output", async () => {
    const spec = loadYamlSpec(`
project: smoke
name: smoke
task_queue: smoke
runtime:
  temporal: {}
  registry: {type: inline}
  provider: {type: vercel-ai-gateway, model: anthropic/claude-haiku-4.5}
activities: {}
workflow:
  name: Smoke
  input: schemas:Input
  steps: [{id: smoke, activity: smoke}]
`, { env: {} });
    const activity = defineActivity({ name: "smoke", prompt: { name: "smoke" },
      input: z.object({}), output: z.object({ label: z.literal("ok") }), validationRetries: 0 });
    const usage: ProviderUsage[] = [];
    let result;
    try {
      const transports = await providerTransportsFromSpec(spec);
      result = await executeActivity(activity, {}, {
        provider: providerFromSpec(spec, transports!),
        messages: [{ role: "user", content: 'Return {"label":"ok"}.' }],
        providerParams: { max_tokens: 64, timeout: 30 }, transientRetries: 0,
        cancellationSignal: AbortSignal.timeout(30_000), usageSink: (u) => usage.push(u),
      });
    } catch (error) {
      const category = error instanceof ProviderAuthError ? "authentication"
        : error instanceof ProviderRateLimitError ? "rate-limit"
        : error instanceof ProviderTransientError ? "transient"
        : error instanceof ProviderConfigError ? "configuration" : "unclassified";
      throw new Error(`Vercel AI Gateway live request failed (${category}); no automatic retry was made`);
    }
    console.log("Gateway smoke", { provider: "vercel-ai-gateway", ...usage[0] });
    expect(result).toEqual({ label: "ok" });
    expect(usage[0]?.model).toBe("anthropic/claude-haiku-4.5");
    expect(usage[0]?.inputTokens).toBeTypeOf("number");
    expect(usage[0]?.outputTokens).toBeTypeOf("number");
  }, 35_000,
);
