import { readdirSync, readFileSync, statSync } from "node:fs";
import { basename, dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const here = dirname(fileURLToPath(import.meta.url));
const contractsDir = resolve(here, "../../../../contracts");

interface Fixture {
  input?: string;
  golden?: string;
}
interface Area {
  name: string;
  fixtures: Fixture[];
  coverage: { python: string; typescript: string };
}
interface InterfaceContract {
  name: string;
  document: string;
  conformance?: string;
}
interface ConformanceIndex {
  contract_version: string;
  areas: Area[];
  interfaces: InterfaceContract[];
}

const index = JSON.parse(
  readFileSync(resolve(contractsDir, "conformance.json"), "utf-8"),
) as ConformanceIndex;

function isDir(path: string): boolean {
  try {
    return statSync(path).isDirectory();
  } catch {
    return false;
  }
}

describe("conformance suite integrity (#392)", () => {
  it("contract_version matches contracts/CONTRACT_VERSION", () => {
    const version = readFileSync(resolve(contractsDir, "CONTRACT_VERSION"), "utf-8").trim();
    expect(index.contract_version).toBe(version);
  });

  it("every referenced fixture exists and parses", () => {
    for (const area of index.areas) {
      for (const fixture of area.fixtures) {
        for (const rel of [fixture.input, fixture.golden]) {
          if (rel === undefined) {
            continue;
          }
          const path = resolve(contractsDir, rel);
          expect(statSync(path).isFile(), `missing ${rel}`).toBe(true);
          JSON.parse(readFileSync(path, "utf-8"));
        }
      }
    }
  });

  it("index partitions the contracts/ directories into areas and interfaces", () => {
    // The index is the classification authority (#616): every contracts/
    // subdirectory is exactly one of an area (golden-reproduction parity) or
    // an interface (normative surface document), so a contract of either
    // class can't ship undocumented or be registered as both. Directory shape
    // does not classify — an interface may later gain fixtures (#617) without
    // becoming an area.
    const areas = new Set(index.areas.map((area) => area.name));
    const interfaces = new Set(index.interfaces.map((iface) => iface.name));
    const onDisk = new Set(
      readdirSync(contractsDir, { withFileTypes: true })
        .filter((entry) => entry.isDirectory())
        .map((entry) => entry.name),
    );
    expect(new Set([...areas, ...interfaces])).toEqual(onDisk);
    expect([...areas].filter((name) => interfaces.has(name))).toEqual([]);
  });

  it("every area has a golden/ directory", () => {
    for (const area of index.areas) {
      expect(isDir(resolve(contractsDir, area.name, "golden")), `area ${area.name}`).toBe(true);
    }
  });

  it("every interface document exists and parses", () => {
    for (const iface of index.interfaces) {
      const path = resolve(contractsDir, iface.document);
      expect(statSync(path).isFile(), `missing document for interface ${iface.name}`).toBe(true);
      JSON.parse(readFileSync(path, "utf-8"));
    }
  });

  it("resolver response schemas exist in the control-plane contract", () => {
    // The resolver interface returns exactly what the API serves (#619): every
    // operation's response_schema must be a component of the control-plane
    // OpenAPI document — no invented shapes, no dangling names after a rename.
    // One sanctioned exception (#642, mirroring the Python gate): an operation
    // whose response is NOT an API DTO (resolve_plan — a binding-profile
    // artifact no route serves) declares `response_schema: "inline …"` and
    // must carry its full inline `response` definition — explicit, never dangling.
    const resolver = JSON.parse(
      readFileSync(resolve(contractsDir, "resolver/resolver.v1.json"), "utf-8"),
    ) as {
      response_schemas_source: string;
      operations: Record<string, { response_schema: string; response?: Record<string, unknown> }>;
    };
    const source = JSON.parse(
      readFileSync(resolve(contractsDir, resolver.response_schemas_source), "utf-8"),
    ) as { components: { schemas: Record<string, unknown> } };
    const components = new Set(Object.keys(source.components.schemas));
    for (const [name, operation] of Object.entries(resolver.operations)) {
      if (operation.response_schema.startsWith("inline")) {
        expect(
          typeof operation.response === "object" && Object.keys(operation.response ?? {}).length > 0,
          `resolver op ${name}: an inline response_schema must define its response fields`,
        ).toBe(true);
        continue;
      }
      expect(components.has(operation.response_schema), `resolver op ${name}`).toBe(true);
    }
  });

  it("interface conformance suites index their fixtures", () => {
    // An interface may carry an HTTP-level conformance suite (#617): the suite
    // index and every case it references must exist and parse, and every case
    // file on disk must be indexed — a case can't ship outside the suite.
    for (const iface of index.interfaces) {
      if (iface.conformance === undefined) {
        continue;
      }
      const suitePath = resolve(contractsDir, iface.conformance);
      expect(statSync(suitePath).isFile(), `missing conformance suite for ${iface.name}`).toBe(
        true,
      );
      const suite = JSON.parse(readFileSync(suitePath, "utf-8")) as { cases: string[] };
      const fixturesDir = dirname(suitePath);
      for (const caseFile of suite.cases) {
        const casePath = resolve(fixturesDir, caseFile);
        expect(statSync(casePath).isFile(), `missing conformance case ${caseFile}`).toBe(true);
        const parsed = JSON.parse(readFileSync(casePath, "utf-8")) as Record<string, unknown>;
        for (const key of ["name", "request", "response"]) {
          expect(parsed, `${caseFile} lacks ${key}`).toHaveProperty(key);
        }
      }
      const onDisk = readdirSync(fixturesDir)
        .filter((file) => file.endsWith(".json"))
        .filter((file) => resolve(fixturesDir, file) !== suitePath);
      expect(new Set(suite.cases)).toEqual(new Set(onDisk));
    }
  });

  it("edition blocks are well-formed and never replace the base golden (#620)", () => {
    // Edition variants/skips are EXPLICIT divergence records: every editions key
    // must be a suite-registered edition; a skip must cite a live issue; a variant
    // must say WHY it diverges; the base golden stays intact.
    for (const iface of index.interfaces) {
      if (iface.conformance === undefined) {
        continue;
      }
      const suitePath = resolve(contractsDir, iface.conformance);
      const suite = JSON.parse(readFileSync(suitePath, "utf-8")) as {
        cases: string[];
        editions?: Record<string, unknown>;
      };
      const knownEditions = new Set(Object.keys(suite.editions ?? {}));
      for (const caseFile of suite.cases) {
        const parsed = JSON.parse(readFileSync(resolve(dirname(suitePath), caseFile), "utf-8")) as {
          response?: unknown;
          editions?: Record<string, { skip?: string; note?: string; response?: Record<string, unknown> }>;
        };
        expect(parsed.response, `${caseFile}: base golden missing`).toBeDefined();
        for (const [edition, variant] of Object.entries(parsed.editions ?? {})) {
          expect(knownEditions.has(edition), `${caseFile}: unknown edition ${edition}`).toBe(true);
          if (variant.skip !== undefined) {
            expect(variant.skip, `${caseFile}: edition skip must cite a live issue`).toMatch(/#\d+/);
            expect(variant.response, `${caseFile}: a skipped edition must not carry a response`).toBeUndefined();
          } else {
            expect(variant.note?.trim(), `${caseFile}: edition variant needs a why-note`).toBeTruthy();
            expect(Object.keys(variant.response ?? {})).toEqual(expect.arrayContaining(["status", "body"]));
          }
        }
      }
    }
  });

  it("every area declares per-SDK coverage", () => {
    for (const area of index.areas) {
      expect(["full", "partial"]).toContain(area.coverage.python);
      expect(["full", "partial"]).toContain(area.coverage.typescript);
    }
  });

  it("references every fixture file in each area's golden dir", () => {
    // A new golden/input added to an existing area must enter the index too.
    for (const area of index.areas) {
      const referenced = new Set<string>();
      for (const fixture of area.fixtures) {
        for (const rel of [fixture.input, fixture.golden]) {
          if (rel !== undefined) {
            referenced.add(basename(rel));
          }
        }
      }
      const goldenDir = resolve(contractsDir, area.name, "golden");
      const onDisk = new Set(readdirSync(goldenDir).filter((file) => file.endsWith(".json")));
      expect(referenced).toEqual(onDisk);
    }
  });
});
