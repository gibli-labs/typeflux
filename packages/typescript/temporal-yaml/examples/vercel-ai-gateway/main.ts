import { readFile } from "node:fs/promises";
import { z } from "zod";
import { assembleYamlRuntime, loadYamlSpec, providerTransportsFromSpec } from "../../src/index.js";

// Install the optional `openai` SDK in your application and supply the key
// through your secret manager. This example makes one activity invocation.
const spec = loadYamlSpec(await readFile(new URL("./typeflux.yaml", import.meta.url), "utf8"));
const transports = await providerTransportsFromSpec(spec);
const runtime = assembleYamlRuntime(spec, {
  ...(transports !== undefined ? { transports } : {}),
  schemas: {
    "schemas:Input": z.object({ text: z.string() }),
    "schemas:Output": z.object({ label: z.string() }),
  },
});
console.log(await runtime.activities["classify"]!({ text: "I need a copy of my invoice." }));
