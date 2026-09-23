/**
 * Canonical content digest of a packed npm tarball (#894).
 *
 * `pnpm pack` serializes rewritten workspace dependencies in nondeterministic
 * key order, so raw tarball bytes differ across otherwise-identical packs.
 * This digest is stable: package.json is parsed and stringified with
 * recursively sorted keys; every other member is hashed by raw bytes; members
 * are combined sorted by path. Two packs of identical content agree, and any
 * real content difference does not.
 *
 * Usage: node scripts/ts-tarball-digest.mjs <tarball.tgz>   (prints the digest)
 */

import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { lstatSync, mkdtempSync, readFileSync, readdirSync, readlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, relative, resolve } from "node:path";

const tarball = process.argv[2];
if (!tarball) {
  process.stderr.write("usage: node scripts/ts-tarball-digest.mjs <tarball.tgz>\n");
  process.exit(2);
}

// Only DEPENDENCY MAPS are order-insensitive; the rest of package.json keeps
// its order (conditional `exports` are order-SENSITIVE — `default` must
// follow more specific conditions, so reordering there would let two
// semantically different manifests collide).
const ORDER_INSENSITIVE_KEYS = new Set([
  "dependencies",
  "devDependencies",
  "peerDependencies",
  "optionalDependencies",
]);

function canonicalManifest(manifest) {
  const out = {};
  for (const [key, value] of Object.entries(manifest)) {
    if (ORDER_INSENSITIVE_KEYS.has(key) && value && typeof value === "object" && !Array.isArray(value)) {
      out[key] = Object.fromEntries(Object.keys(value).sort().map((k) => [k, value[k]]));
    } else {
      out[key] = value;
    }
  }
  return JSON.stringify(out);
}

const dir = mkdtempSync(join(tmpdir(), "ts-digest-"));
execFileSync("tar", ["xzf", resolve(tarball), "-C", dir]);
// npm strips the FIRST path component of every archive member on install,
// so a tarball can carry siblings of package/ (e.g. other/dist/x.js) that
// still install. Refuse anything but the single canonical top-level folder
// rather than silently ignoring extra members.
const topLevel = readdirSync(dir);
if (topLevel.length !== 1 || topLevel[0] !== "package") {
  process.stderr.write(
    `ts-tarball-digest: refusing archive with unexpected top-level entries: ${topLevel.join(", ")}\n`,
  );
  process.exit(1);
}
const root = join(dir, "package");

const files = [];
const stack = [root];
while (stack.length) {
  const d = stack.pop();
  for (const entry of readdirSync(d, { withFileTypes: true })) {
    const p = join(d, entry.name);
    if (entry.isDirectory() && !entry.isSymbolicLink()) stack.push(p);
    else files.push(p);
  }
}
files.sort((a, b) => relative(root, a).localeCompare(relative(root, b)));

// Length-prefixed encoding: NUL separators alone are ambiguous when file
// contents contain NUL bytes (two layouts could collide). Each field is
// preceded by its byte length, which makes the framing injective.
const hash = createHash("sha256");
const frame = (buf) => {
  const b = Buffer.isBuffer(buf) ? buf : Buffer.from(buf);
  hash.update(String(b.length));
  hash.update(":");
  hash.update(b);
};
for (const f of files) {
  const rel = relative(root, f);
  const st = lstatSync(f);
  frame(rel);
  // Entry semantics matter: a 0755 vs 0644 dist/cli.js, or a symlink vs a
  // regular file, are materially different packages.
  frame(`mode:${(st.mode & 0o777).toString(8)}`);
  if (st.isSymbolicLink()) {
    frame("type:symlink");
    frame(readlinkSync(f));
  } else {
    frame("type:file");
    if (rel === "package.json") {
      frame(canonicalManifest(JSON.parse(readFileSync(f, "utf8"))));
    } else {
      frame(readFileSync(f));
    }
  }
}
process.stdout.write(`${hash.digest("hex")}\n`);
