import { readFile } from "node:fs/promises";
import { z } from "zod";
import { assembleYamlRuntime, loadYamlSpec, providerTransportsFromSpec } from "../../src/index.js";

const spec = loadYamlSpec(await readFile(new URL("./typeflux.yaml", import.meta.url), "utf8"));
const transports = await providerTransportsFromSpec(spec);
const output = z.object({ route: z.object({
  type: z.literal("choice"), choice: z.enum(["billing", "technical", "human_review"]),
  confidence: z.number().min(0).max(1), probabilities: z.object({ billing: z.number(), technical: z.number(), human_review: z.number() }),
}) });
const runtime = assembleYamlRuntime(spec, {
  ...(transports !== undefined ? { transports } : {}),
  schemas: { "schemas:Input": z.object({ text: z.string() }), "schemas:Output": output },
});
const result = output.parse(await runtime.activities["decide"]!({ text: "I was charged twice. Please refund the duplicate." }));
// Use the same deterministic rule in workflow code after executing the activity.
const path = result.route.confidence >= 0.8 ? result.route.choice : "human_review";
console.log({ path, ...result });
