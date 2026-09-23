/**
 * The two shipped JSON Schemas parse and stay FRESH against their zod source (#326). Freshness is
 * checked by regenerating from the monorepo temporal-yaml dist in memory and diffing against the
 * committed src/schema/*.json — a spec change that skips `npm run generate:schemas` fails here.
 * When the monorepo dist is absent (a published install) the freshness check is skipped; the
 * committed JSON is authoritative there.
 */

import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { z } from "zod";

import { readSchema, readSchemaJson, SCHEMA_FILES } from "../src/content.js";

const YAML_DIST = fileURLToPath(
  new URL("../../../packages/typescript/temporal-yaml/dist/index.js", import.meta.url),
);
const PROJECT_DIST = fileURLToPath(
  new URL("../../../packages/typescript/temporal-yaml/dist/project-spec.js", import.meta.url),
);

describe("shipped JSON schemas", () => {
  it("both schema slugs are shipped and parse as JSON objects", () => {
    for (const name of Object.keys(SCHEMA_FILES)) {
      const parsed = readSchemaJson(name) as Record<string, unknown>;
      expect(parsed).toBeTypeOf("object");
      expect(parsed.type).toBe("object");
      expect(parsed.properties).toBeTypeOf("object");
    }
  });

  it("the workflow-yaml schema describes the top-level spec shape", () => {
    const parsed = readSchemaJson("typeflux-yaml") as { properties: Record<string, unknown> };
    for (const field of ["project", "name", "task_queue", "runtime", "activities", "workflow"]) {
      expect(parsed.properties).toHaveProperty(field);
    }
  });

  it("the project schema describes the manifest shape", () => {
    const parsed = readSchemaJson("project") as { properties: Record<string, unknown> };
    expect(parsed.properties).toHaveProperty("workflows");
    expect(parsed.properties).toHaveProperty("environments");
  });

  it.skipIf(!existsSync(YAML_DIST))(
    "the committed schemas match a fresh generation from the zod specs",
    async () => {
      const { typefluxYamlSpec } = await import(YAML_DIST);
      const { typefluxProjectSpec } = await import(PROJECT_DIST);
      const opts = { unrepresentable: "any", io: "input" } as const;

      const cases: [string, unknown][] = [
        ["typeflux-yaml", typefluxYamlSpec],
        ["project", typefluxProjectSpec],
      ];
      for (const [name, spec] of cases) {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const fresh = z.toJSONSchema(spec as any, opts);
        const committed = readSchemaJson(name) as Record<string, unknown>;
        // The committed file wraps the generated schema with a header; compare the shared keys.
        expect(committed.properties).toEqual((fresh as Record<string, unknown>).properties);
        expect(committed.required).toEqual((fresh as Record<string, unknown>).required);
        expect(committed.$defs).toEqual((fresh as Record<string, unknown>).$defs);
      }
    },
  );

  it("readSchema returns undefined for an unknown slug (no traversal)", () => {
    expect(readSchema("../secret")).toBeUndefined();
    expect(readSchema("nope")).toBeUndefined();
  });
});
