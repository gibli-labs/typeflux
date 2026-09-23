/**
 * Loader for the STATIC documentation resources (#326 Phase 0): the bundled docs and examples
 * (src/generated/, produced by scripts/generate-assets.mjs), the generated JSON Schemas
 * (src/schema/), and the hand-authored guide (src/guide/). All content is shipped inside the
 * package so `npx typeflux-mcp` needs no monorepo checkout.
 *
 * Paths resolve relative to the PACKAGE ROOT, derived from `import.meta.url`, so the same code
 * works whether running from `src/` (tests) or the built `dist/`.
 */

import { existsSync, readFileSync, readdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const PKG_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const GENERATED = join(PKG_ROOT, "src/generated");
const SCHEMA_DIR = join(PKG_ROOT, "src/schema");
const GUIDE_DIR = join(PKG_ROOT, "src/guide");

/** The two shipped JSON Schemas, keyed by their `typeflux://schema/{name}` slug. */
export const SCHEMA_FILES: Readonly<Record<string, string>> = {
  "typeflux-yaml": "typeflux-yaml.schema.json",
  project: "project.schema.json",
};

function readText(path: string): string | undefined {
  return existsSync(path) ? readFileSync(path, "utf8") : undefined;
}

/**
 * A markdown resource family: one src/generated/ subdirectory enumerated and read by slug. Docs
 * and examples, in both editions, share this shape — the factory keeps the traversal guard and
 * the .md strip in exactly one place (a fix applied here applies to every family).
 */
function markdownFamily(guard: RegExp, ...segments: string[]) {
  const dir = join(GENERATED, ...segments);
  return {
    list(): string[] {
      if (!existsSync(dir)) return [];
      return readdirSync(dir)
        .filter((f) => f.endsWith(".md"))
        .map((f) => f.replace(/\.md$/, ""))
        .sort();
    },
    read(slug: string): string | undefined {
      if (!guard.test(slug)) return undefined; // no traversal
      return readText(join(dir, `${slug}.md`));
    },
  };
}

const DOC_SLUG = /^[a-z0-9-]+$/;
const EXAMPLE_NAME = /^[a-z0-9_-]+$/i;

const docsFamily = markdownFamily(DOC_SLUG, "docs");
const docsTypescriptFamily = markdownFamily(DOC_SLUG, "docs", "typescript");
const examplesFamily = markdownFamily(EXAMPLE_NAME, "examples");
const examplesTypescriptFamily = markdownFamily(EXAMPLE_NAME, "examples", "typescript");

/** The doc slugs available as `typeflux://docs/{slug}` (bundled at build). */
export const listDocs = docsFamily.list;
/** The markdown of `typeflux://docs/{slug}`, or undefined when unknown. */
export const readDoc = docsFamily.read;
/** The TS-edition doc slugs available as `typeflux://docs/typescript/{slug}` (#862). */
export const listTypescriptDocs = docsTypescriptFamily.list;
/** The markdown of `typeflux://docs/typescript/{slug}`, or undefined when unknown. */
export const readTypescriptDoc = docsTypescriptFamily.read;
/** The example names available as `typeflux://examples/{name}`. */
export const listExamples = examplesFamily.list;
/** The distilled markdown of `typeflux://examples/{name}`, or undefined when unknown. */
export const readExample = examplesFamily.read;
/** The TS-edition example names available as `typeflux://examples/typescript/{name}` (#863). */
export const listTypescriptExamples = examplesTypescriptFamily.list;
/** The distilled markdown of `typeflux://examples/typescript/{name}`, or undefined when unknown. */
export const readTypescriptExample = examplesTypescriptFamily.read;

/** The raw JSON text of a `typeflux://schema/{name}` resource, or undefined when unknown. */
export function readSchema(name: string): string | undefined {
  const file = SCHEMA_FILES[name];
  if (file === undefined) return undefined;
  return readText(join(SCHEMA_DIR, file));
}

/** The parsed JSON Schema for `{name}` (throws only on a genuinely malformed shipped file). */
export function readSchemaJson(name: string): unknown | undefined {
  const text = readSchema(name);
  return text === undefined ? undefined : JSON.parse(text);
}

/** The hand-authored authoring checklist (`typeflux://guide/authoring-checklist`). */
export function readAuthoringChecklist(): string | undefined {
  return readText(join(GUIDE_DIR, "authoring-checklist.md"));
}

/** The hand-authored project-layout & adoption guide (`typeflux://guide/project-layout`, #865). */
export function readProjectLayoutGuide(): string | undefined {
  return readText(join(GUIDE_DIR, "project-layout.md"));
}
