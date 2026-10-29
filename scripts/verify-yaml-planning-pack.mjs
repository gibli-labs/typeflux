/** Install candidate tarballs outside the workspace and reject any Temporal SDK dependency. */
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { copyFileSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const directory = mkdtempSync(join(tmpdir(), "typeflux-yaml-install-"));
const run = (command, args, cwd = directory) => execFileSync(command, args, { cwd, encoding: "utf8", stdio: ["ignore", "pipe", "inherit"] });
try {
  const tarballs = ["temporal", "yaml"].map((name) =>
    run("pnpm", ["pack", "--pack-destination", directory], join(root, "packages/typescript", name)).trim().split("\n").at(-1),
  );
  writeFileSync(join(directory, "package.json"), JSON.stringify({ private: true, type: "module" }));
  run("npm", ["install", "--ignore-scripts", "--no-audit", "--fund=false", ...tarballs]);
  const tree = JSON.parse(run("npm", ["ls", "--all", "--json"]));
  function inspect(node) {
    for (const [name, dependency] of Object.entries(node.dependencies ?? {})) {
      assert.ok(!name.startsWith("@temporalio/"), `Unexpected Temporal SDK dependency: ${name}`);
      inspect(dependency);
    }
  }
  inspect(tree);
  copyFileSync(join(root, "scripts/probe-yaml-planning.mjs"), join(directory, "probe.mjs"));
  process.stdout.write(run(process.execPath, ["probe.mjs"]));
} finally {
  rmSync(directory, { recursive: true, force: true });
}
