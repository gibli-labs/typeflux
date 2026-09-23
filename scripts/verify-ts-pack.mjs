/**
 * TS-workspace packed-artifact gate (#894). For each @typeflux SDK package:
 *
 * 1. Pack with `pnpm pack` and read the PACKED manifest (what npm serves).
 * 2. No `workspace:`/`file:`/`link:` specifier may survive rewriting.
 * 3. Version-train equality: every package version equals every other's.
 * 4. Metadata contract: license id, repository/homepage/bugs, explicit
 *    files allowlist; LICENSE + NOTICE + README present in the tarball.
 * 5. If the repo carries the private forbidden-terms list
 *    (docs/open-source/forbidden-terms.txt — never part of the public tree),
 *    no packed file's path or text content may match a term. Trees without
 *    the list (the public repository) skip the scan.
 *
 * Run from the repo root: node scripts/verify-ts-pack.mjs [outDir]
 * Packs into outDir (default: a temp dir); prints "name version tarball"
 * lines on stdout for the release workflow to consume.
 */

import { execFileSync } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, readdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = resolve(HERE, "..");
const PKG_ROOT = join(REPO, "packages", "typescript");
// Topological publish order: each member's rewritten deps must already be
// on the registry if its own upload succeeds and a later one fails.
const PACKAGES = ["temporal", "temporal-worker", "temporal-yaml", "temporal-controlplane"];

const outDir = process.argv[2] ? resolve(process.argv[2]) : mkdtempSync(join(tmpdir(), "ts-pack-"));
const failures = [];

const terms = (() => {
  const p = join(REPO, "docs/open-source/forbidden-terms.txt");
  if (!existsSync(p)) return [];
  return readFileSync(p, "utf8")
    .split("\n")
    .map((line) => line.trim().toLowerCase())
    .filter((line) => line && !line.startsWith("#"));
})();

const results = [];
let trainVersion;

for (const name of PACKAGES) {
  const dir = join(PKG_ROOT, name);
  const tarball = execFileSync("pnpm", ["pack", "--pack-destination", outDir], {
    cwd: dir,
    encoding: "utf8",
  })
    .trim()
    .split("\n")
    .pop();

  const extractDir = mkdtempSync(join(tmpdir(), `ts-verify-${name}-`));
  execFileSync("tar", ["xzf", tarball, "-C", extractDir]);
  const root = join(extractDir, "package");
  const manifest = JSON.parse(readFileSync(join(root, "package.json"), "utf8"));

  // The packed name must be exactly the expected scoped name — a drifted
  // name would otherwise be published as-is before any install smoke runs.
  const expectedName = `@typeflux/${name}`;
  if (manifest.name !== expectedName) {
    failures.push(`${name}: packed name ${manifest.name} != expected ${expectedName}`);
  }

  // Version train
  trainVersion ??= manifest.version;
  if (manifest.version !== trainVersion) {
    failures.push(`${manifest.name}: version ${manifest.version} != train ${trainVersion}`);
  }

  // Workspace rewriting
  for (const field of ["dependencies", "devDependencies", "peerDependencies", "optionalDependencies"]) {
    for (const [dep, spec] of Object.entries(manifest[field] ?? {})) {
      if (/^(workspace:|file:|link:)/.test(String(spec))) {
        failures.push(`${manifest.name}: packed ${field}.${dep} kept non-registry specifier ${spec}`);
      }
    }
  }

  // Metadata contract: exact expected values, not mere truthiness — an
  // immutable release must not carry stale/private URLs.
  if (manifest.license !== "Apache-2.0") failures.push(`${manifest.name}: license ${manifest.license}`);
  const PUBLIC_HOME = "https://github.com/gibli-labs/typeflux";
  const expectMeta = {
    "repository.type": manifest.repository?.type === "git",
    "repository.url": manifest.repository?.url === `git+${PUBLIC_HOME}.git`,
    "repository.directory": manifest.repository?.directory === `packages/typescript/${name}`,
    homepage: manifest.homepage === `${PUBLIC_HOME}/tree/main/packages/typescript/${name}#readme`,
    "bugs.url": manifest.bugs?.url === `${PUBLIC_HOME}/issues`,
  };
  for (const [field, ok] of Object.entries(expectMeta)) {
    if (!ok) failures.push(`${manifest.name}: manifest ${field} does not match the public home`);
  }
  if (!Array.isArray(manifest.files) || manifest.files.length === 0) {
    failures.push(`${manifest.name}: no files allowlist`);
  }
  for (const required of ["LICENSE", "NOTICE", "README.md", "dist"]) {
    if (!existsSync(join(root, required))) failures.push(`${manifest.name}: tarball missing ${required}`);
  }

  // Forbidden-terms content scan
  if (terms.length) {
    const stack = [root];
    while (stack.length) {
      const d = stack.pop();
      for (const entry of readdirSync(d, { withFileTypes: true })) {
        const p = join(d, entry.name);
        if (entry.isDirectory()) {
          stack.push(p);
          continue;
        }
        const lowerPath = p.toLowerCase();
        const content = readFileSync(p, "utf8").toLowerCase();
        for (const term of terms) {
          if (lowerPath.includes(term) || content.includes(term)) {
            failures.push(`${manifest.name}: forbidden term in packed file ${p.slice(root.length + 1)}`);
          }
        }
      }
    }
  }

  results.push({ name: manifest.name, version: manifest.version, tarball });
}

if (terms.length) {
  process.stderr.write(`verify-ts-pack: content-scanned ${results.length} tarballs against ${terms.length} terms\n`);
} else {
  process.stderr.write("verify-ts-pack: no forbidden-terms list in this tree; content scan skipped\n");
}

if (failures.length) {
  for (const f of failures) process.stderr.write(`verify-ts-pack: FAIL ${f}\n`);
  process.exit(1);
}
for (const r of results) process.stdout.write(`${r.name} ${r.version} ${r.tarball}\n`);
process.stderr.write(`verify-ts-pack: OK (${results.length} packages, train ${trainVersion})\n`);
