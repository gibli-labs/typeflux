/**
 * Packed-content gate for @typeflux/control-plane-client (#893). Mirrors the
 * SDK-train gate: pack, unpack, then assert the license surface, metadata
 * contract, files allowlist, and (private tree only) the forbidden-terms
 * scan. Finally import-smokes the packed entrypoint.
 *
 * Run from clients/typescript: node ../../scripts/verify-client-pack.mjs
 */

import { execFileSync } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, readdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = resolve(HERE, "..");
const PKG = resolve(REPO, "clients", "typescript");
const PUBLIC_HOME = "https://github.com/gibli-labs/typeflux";

const outDir = mkdtempSync(join(tmpdir(), "client-pack-"));
const packJson = JSON.parse(
  execFileSync("npm", ["pack", "--json", "--pack-destination", outDir], { cwd: PKG, encoding: "utf8" }),
);
const tarball = join(outDir, packJson[0].filename);
const extract = mkdtempSync(join(tmpdir(), "client-verify-"));
execFileSync("tar", ["xzf", tarball, "-C", extract]);
const root = join(extract, "package");
const manifest = JSON.parse(readFileSync(join(root, "package.json"), "utf8"));

const failures = [];
if (manifest.name !== "@typeflux/control-plane-client") failures.push(`name ${manifest.name}`);
if (manifest.license !== "Apache-2.0") failures.push(`license ${manifest.license}`);
if (manifest.repository?.url !== `git+${PUBLIC_HOME}.git`) failures.push(`repository.url ${manifest.repository?.url}`);
if (manifest.bugs?.url !== `${PUBLIC_HOME}/issues`) failures.push(`bugs.url ${manifest.bugs?.url}`);
if (!manifest.homepage?.startsWith(`${PUBLIC_HOME}/`)) failures.push(`homepage ${manifest.homepage}`);
if (!Array.isArray(manifest.files) || manifest.files.length === 0) failures.push("no files allowlist");
if (manifest.engines?.node !== "^22") failures.push(`engines.node ${manifest.engines?.node}`);
for (const required of ["LICENSE", "NOTICE", "README.md", "dist/index.js", "dist/index.d.ts"]) {
  if (!existsSync(join(root, required))) failures.push(`tarball missing ${required}`);
}

// Forbidden-terms scan (private tree only)
const termsPath = join(REPO, "docs/open-source/forbidden-terms.txt");
if (existsSync(termsPath)) {
  const terms = readFileSync(termsPath, "utf8")
    .split("\n")
    .map((l) => l.trim().toLowerCase())
    .filter((l) => l && !l.startsWith("#"));
  const stack = [root];
  while (stack.length) {
    const d = stack.pop();
    for (const entry of readdirSync(d, { withFileTypes: true })) {
      const p = join(d, entry.name);
      if (entry.isDirectory()) { stack.push(p); continue; }
      const content = readFileSync(p, "utf8").toLowerCase();
      for (const term of terms) {
        if (p.toLowerCase().includes(term) || content.includes(term)) {
          failures.push(`forbidden term in packed file ${p.slice(root.length + 1)}`);
        }
      }
    }
  }
  process.stderr.write(`verify-client-pack: content-scanned against ${terms.length} terms\n`);
} else {
  process.stderr.write("verify-client-pack: no forbidden-terms list; scan skipped\n");
}

if (failures.length) {
  for (const f of failures) process.stderr.write(`verify-client-pack: FAIL ${f}\n`);
  process.exit(1);
}

// Install smoke: npm install the TARBALL into a scratch project so its
// declared dependencies must resolve from the registry (an undeclared
// runtime dep — the hoisting-masked class — fails here), then import it
// by name.
const smoke = mkdtempSync(join(tmpdir(), "client-smoke-"));
execFileSync("npm", ["init", "-y"], { cwd: smoke, stdio: "ignore" });
execFileSync("npm", ["install", tarball, "--no-audit", "--no-fund"], {
  cwd: smoke,
  stdio: "inherit",
  env: { ...process.env, NPM_CONFIG_USERCONFIG: "/dev/null" },
});
// Import by the PUBLIC package specifier from inside the scratch project so
// Node exercises real package-name resolution and the exports map — a broken
// exports["."] fails here exactly as it would for a consumer.
execFileSync("node", ["--input-type=module", "-e",
  `import * as m from "@typeflux/control-plane-client";
   if (Object.keys(m).length === 0) { console.error("no exports"); process.exit(1); }
   console.log(Object.keys(m).slice(0, 5).join(","));`,
], { cwd: smoke, stdio: ["ignore", "pipe", "inherit"] });
process.stderr.write(`verify-client-pack: OK (${packJson[0].filename}; name-specifier import resolved)\n`);
