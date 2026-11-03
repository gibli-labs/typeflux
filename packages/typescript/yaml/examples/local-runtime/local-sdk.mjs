import { readFileSync } from "node:fs";
import { buildLocalRuntime, loadYamlSpec } from "@typeflux/yaml";
import { schemas, provider } from "./bindings.mjs";

const spec = loadYamlSpec(readFileSync(new URL("workflow.yaml", import.meta.url), "utf8"));
const runtime = await buildLocalRuntime(spec, { schemas, provider });
console.log(JSON.stringify(await runtime.executeWorkflow(
  JSON.parse(readFileSync(new URL("input.json", import.meta.url), "utf8")),
)));
