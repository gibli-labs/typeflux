// Both generated surfaces use this tool's compiler API, independently of tsc 7.
import { spawnSync } from "node:child_process";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const repo = resolve(here, "../..");
const outputs = {
  client: "clients/typescript/src/schema.ts",
  controlplane: "packages/typescript/temporal-controlplane/src/http/contract.ts",
};
const output = outputs[process.argv[2]];
if (!output || process.argv.length !== 3) {
  console.error("Usage: node tools/openapi-generator/generate.mjs client|controlplane");
  process.exit(2);
}
const require = createRequire(import.meta.url);
let cli;
try {
  const manifest = require.resolve("openapi-typescript/package.json");
  cli = resolve(dirname(manifest), require(manifest).bin["openapi-typescript"]);
} catch {
  console.error("Install the isolated generator first: npm ci --prefix tools/openapi-generator");
  process.exit(1);
}
const result = spawnSync(process.execPath, [cli, resolve(repo, "contracts/controlplane/openapi.v1.json"), "-o", resolve(repo, output)], { stdio: "inherit" });
if (result.error) console.error(result.error.message);
process.exit(result.status ?? 1);
