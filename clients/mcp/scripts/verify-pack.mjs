/**
 * Packed-artifact content gate (#898). Runs after `npm run build`:
 *
 * 1. The tarball must carry the license surface: LICENSE, NOTICE,
 *    THIRD-PARTY-NOTICES.md, and the bin.
 * 2. No packed path may reference an excluded example
 *    (scripts/generate-assets.mjs EXCLUDED_EXAMPLES).
 * 3. If the repo carries a private forbidden-terms list
 *    (docs/open-source/forbidden-terms.txt — never part of the public tree),
 *    no packed file's CONTENT may match a term, case-insensitively. In trees
 *    without the list (e.g. the public repository) this scan is skipped.
 */

import { execFileSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { EXCLUDED_EXAMPLES } from "./generate-assets.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const PKG_ROOT = resolve(HERE, "..");
const REPO_ROOT = resolve(PKG_ROOT, "../..");

const packJson = JSON.parse(
  execFileSync("npm", ["pack", "--dry-run", "--json"], { cwd: PKG_ROOT, encoding: "utf8" }),
);
const files = packJson[0].files.map((f) => f.path);

const failures = [];

for (const required of ["LICENSE", "NOTICE", "THIRD-PARTY-NOTICES.md", "dist/index.js"]) {
  if (!files.includes(required)) failures.push(`missing required packed file: ${required}`);
}

// Release-policy metadata contract: the published manifest must carry the
// license id and the pointers a registry page needs.
const manifest = JSON.parse(readFileSync(join(PKG_ROOT, "package.json"), "utf8"));
if (manifest.license !== "Apache-2.0") failures.push(`manifest license must be Apache-2.0, got: ${manifest.license}`);
for (const field of ["repository", "homepage", "bugs"]) {
  if (!manifest[field]) failures.push(`manifest missing required field: ${field}`);
}
if (!Array.isArray(manifest.files) || manifest.files.length === 0) {
  failures.push("manifest must declare an explicit files allowlist");
}

for (const path of files) {
  for (const excluded of EXCLUDED_EXAMPLES) {
    if (path.includes(excluded)) failures.push(`packed path references excluded example: ${path}`);
  }
}

const termsPath = join(REPO_ROOT, "docs/open-source/forbidden-terms.txt");
if (existsSync(termsPath)) {
  const terms = readFileSync(termsPath, "utf8")
    .split("\n")
    .map((line) => line.trim().toLowerCase())
    .filter((line) => line && !line.startsWith("#"));
  for (const path of files) {
    const abs = join(PKG_ROOT, path);
    if (!existsSync(abs)) continue; // defensive: pack listing should mirror the tree
    const content = readFileSync(abs, "utf8").toLowerCase();
    for (const term of terms) {
      if (path.toLowerCase().includes(term) || content.includes(term)) {
        failures.push(`forbidden term in packed file: ${path}`);
      }
    }
  }
  process.stderr.write(`verify-pack: content-scanned ${files.length} packed files against ${terms.length} terms\n`);
} else {
  process.stderr.write("verify-pack: no forbidden-terms list in this tree; content scan skipped\n");
}

if (failures.length) {
  for (const f of failures) process.stderr.write(`verify-pack: FAIL ${f}\n`);
  process.exit(1);
}
process.stderr.write(`verify-pack: OK (${files.length} files)\n`);
