import { createHash } from "node:crypto";
import { readFileSync, readdirSync, existsSync } from "node:fs";
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
const sha = (path) => createHash("sha256").update(readFileSync(path)).digest("hex");
const packages = ["@typeflux/temporal", "@typeflux/yaml", "@typeflux/temporal-worker",
  "@typeflux/temporal-yaml", "@typeflux/temporal-controlplane", "@typeflux/control-plane-client",
  "@temporalio/worker", "@temporalio/client", "zod"];
const versions = {};
const entries = {};
for (const name of packages) {
  let entry;
  try { entry = require.resolve(name); } catch { versions[name] = null; continue; }
  if (!entry.startsWith("/app/node_modules/")) throw new Error("repository SDK fallback");
  versions[name] = JSON.parse(readFileSync(`/app/node_modules/${name}/package.json`, "utf8")).version;
  if (name.startsWith("@typeflux/")) entries[name] = {path: entry, sha256: sha(entry)};
}
console.log(JSON.stringify({versions, entries,
  temporalPackages: existsSync("/app/node_modules/@temporalio") ? readdirSync("/app/node_modules/@temporalio") : [],
  candidateFiles: Object.fromEntries(readdirSync("/candidate").map(name => [name, sha(`/candidate/${name}`)])),
  dependencyLocks: {"package-lock.json": sha("/app/package-lock.json")}}));
