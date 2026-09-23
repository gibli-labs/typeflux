/**
 * Produce a self-contained bin (#326 items 1+2) with esbuild CODE-SPLITTING.
 *
 * typeflux-mcp publishes to public npmjs.org, but two of its deps are unpublishable from there:
 * `@typeflux/control-plane-client` (GitHub Packages) and `@typeflux/temporal-controlplane`
 * (a `workspace:*` package). esbuild INLINES those (and all other pure-JS deps) so the packed
 * artifact carries no file:/workspace deps.
 *
 * Splitting is the key to item 2: managed-local.ts reaches the in-process control plane ONLY via a
 * dynamic `import("@typeflux/temporal-controlplane")`, so esbuild emits it as a SEPARATE chunk. The
 * bin (dist/index.js) never statically references it, so ATTACH mode (TYPEFLUX_CP_URL) never loads
 * the chunk — and never touches the heavy native machinery the chunk pulls in. Only managed-local
 * loads the chunk.
 *
 * The chunk transitively needs the Temporal worker (native @swc/core + core-bridge) at load — the
 * TS control plane's read tier imports it through @typeflux/temporal-worker — so `@temporalio/worker`
 * is a real (public-npm) dependency, installed by `npx`. Native `.node` binaries can't be inlined,
 * and the operate-tier client + provider SDKs are dynamic and never on the read path, so all of
 * these stay EXTERNAL. `@typeflux/*` packages are inlined via the alias + pnpm store.
 */

import { build } from "esbuild";
import { chmodSync, existsSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const PKG_ROOT = resolve(HERE, "..");
const OUT_DIR = resolve(PKG_ROOT, "dist");
const CP_DIST = resolve(PKG_ROOT, "../../packages/typescript/temporal-controlplane/dist/index.js");
const CLIENT_DIST = resolve(PKG_ROOT, "../typescript/dist/index.js");

for (const [label, path, hint] of [
  ["@typeflux/temporal-controlplane", CP_DIST, "pnpm -r --filter ./packages/typescript/** build"],
  ["@typeflux/control-plane-client", CLIENT_DIST, "(cd clients/typescript && npm install && npm run build)"],
]) {
  if (!existsSync(path)) {
    process.stderr.write(`bundle: ${label} build not found at ${path}\nRun: ${hint}\n`);
    process.exit(1);
  }
}

// External: heavy/native + dynamic-only public deps the read tier does not statically need. Node
// resolves these from node_modules at runtime (installed via `dependencies` for managed-local, or
// simply never loaded in attach mode).
const EXTERNAL = [
  "@temporalio/worker",
  "@temporalio/core-bridge",
  "@temporalio/client",
  "@swc/core",
  "@swc/wasm",
  "openai",
  "@anthropic-ai/sdk",
  "@google/genai",
  "langfuse",
  "langsmith",
];

// Clean any prior output (stale per-file emit, old chunks) so `files: ["dist"]` ships only the bundle.
rmSync(OUT_DIR, { recursive: true, force: true });

const result = await build({
  metafile: true,
  entryPoints: [resolve(PKG_ROOT, "src/index.ts")],
  outdir: OUT_DIR,
  bundle: true,
  splitting: true,
  platform: "node",
  format: "esm",
  target: "node22",
  packages: "bundle",
  external: EXTERNAL,
  alias: {
    "@typeflux/temporal-controlplane": CP_DIST,
    "@typeflux/control-plane-client": CLIENT_DIST,
  },
  // The require shim goes into EVERY output chunk (bundled CJS deps expect `require`/`__dirname`).
  banner: {
    js: [
      'import { createRequire as __cr } from "module";',
      'import { fileURLToPath as __fu } from "url";',
      'import { dirname as __dn } from "path";',
      "const require = __cr(import.meta.url);",
      "const __filename = __fu(import.meta.url);",
      "const __dirname = __dn(__filename);",
    ].join("\n"),
  },
  logLevel: "info",
});

// The shebang belongs on the bin only (a chunk with a shebang would be odd); prepend post-build.
const BIN = resolve(OUT_DIR, "index.js");
writeFileSync(BIN, `#!/usr/bin/env node\n${readFileSync(BIN, "utf8")}`);
chmodSync(BIN, 0o755);

// ---------------------------------------------------------------------------
// Third-party attribution (#898): the bundle INLINES dependencies, and MIT/BSD
// require their notices to accompany substantial copies. Derive the exact set
// of bundled packages from the esbuild metafile (inputs under node_modules),
// then reproduce each package's license text in THIRD-PARTY-NOTICES.md,
// shipped in the tarball via the `files` allowlist.
// ---------------------------------------------------------------------------

/** node_modules input path -> the package's root directory (handles scopes + pnpm store). */
function packageRootOf(inputPath) {
  const parts = inputPath.split(sep);
  // Use the LAST node_modules segment (pnpm stores nest: .pnpm/x@v/node_modules/x/...).
  const i = parts.lastIndexOf("node_modules");
  if (i === -1) return undefined;
  const name = parts[i + 1];
  if (!name) return undefined;
  const rootParts = name.startsWith("@") ? parts.slice(0, i + 3) : parts.slice(0, i + 2);
  return rootParts.join(sep);
}

const pkgRoots = new Set();
for (const inputPath of Object.keys(result.metafile.inputs)) {
  const root = packageRootOf(resolve(PKG_ROOT, inputPath));
  if (root) pkgRoots.add(root);
}
// The aliased first-party builds are inlined too but resolve OUTSIDE node_modules
// (dist paths) — they are this repository's own Apache-2.0 code, covered by
// LICENSE/NOTICE, so they are intentionally not third-party entries.

function licenseTextOf(root) {
  for (const candidate of readdirSync(root)) {
    if (/^(licen[cs]e|copying|notice)(\.|$)/i.test(candidate)) {
      return { file: candidate, text: readFileSync(join(root, candidate), "utf8") };
    }
  }
  return undefined;
}

const entries = [];
for (const root of pkgRoots) {
  const metaPath = join(root, "package.json");
  if (!existsSync(metaPath)) continue;
  const meta = JSON.parse(readFileSync(metaPath, "utf8"));
  if (!meta.name || meta.name === "typeflux-mcp") continue;
  entries.push({ name: meta.name, version: meta.version ?? "?", license: meta.license ?? "UNKNOWN", root });
}
entries.sort((a, b) => a.name.localeCompare(b.name) || a.version.localeCompare(b.version));

// Refuse to ship un-attributable code: every bundled package must declare a
// license AND carry a license text we can reproduce.
const missing = [];
const sections = [];
const seen = new Set();
for (const e of entries) {
  const key = `${e.name}@${e.version}`;
  if (seen.has(key)) continue;
  seen.add(key);
  const lic = licenseTextOf(e.root);
  if (e.license === "UNKNOWN" || !lic) {
    missing.push(key);
    continue;
  }
  sections.push(`## ${key}\n\nLicense: ${e.license} (from ${lic.file})\n\n\`\`\`\n${lic.text.trimEnd()}\n\`\`\``);
}
if (missing.length) {
  process.stderr.write(`bundle: bundled packages lack attributable license text: ${missing.join(", ")}\n`);
  process.exit(1);
}

const header = [
  "# Third-party notices",
  "",
  "typeflux-mcp's published bundle (dist/) inlines the packages below.",
  "Each package's license text is reproduced verbatim. This file is",
  "generated by scripts/bundle.mjs from the esbuild metafile — do not edit.",
  "",
].join("\n");
writeFileSync(resolve(PKG_ROOT, "THIRD-PARTY-NOTICES.md"), `${header}\n${sections.join("\n\n")}\n`);

process.stderr.write(
  `bundle: wrote split, self-contained dist/ (bin: index.js); attributed ${seen.size} bundled packages in THIRD-PARTY-NOTICES.md\n`,
);
