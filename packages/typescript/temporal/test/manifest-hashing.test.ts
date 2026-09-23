import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { messagesHash, schemaHash } from "../src/index.js";

const here = dirname(fileURLToPath(import.meta.url));
const contractsDir = resolve(here, "../../../../contracts");

function golden<T = unknown>(rel: string): T {
  return JSON.parse(readFileSync(resolve(contractsDir, rel), "utf-8")) as T;
}

interface SchemaIdentity {
  hash: string;
}
interface ActivityManifest {
  input_schema: SchemaIdentity;
  output_schema: SchemaIdentity;
  prompt_messages_hash: string;
  rendered_messages_hash: string;
}

const activity = golden<ActivityManifest>("manifest/golden/activity_execution.json");
const inputSchema = golden<Record<string, unknown>>("manifest/golden/input_schema.json");
const outputSchema = golden<Record<string, unknown>>("manifest/golden/output_schema.json");

describe("manifest hash primitives (#390)", () => {
  it("schemaHash reproduces the golden input/output schema hashes", () => {
    expect(schemaHash(inputSchema)).toBe(activity.input_schema.hash);
    expect(schemaHash(outputSchema)).toBe(activity.output_schema.hash);
  });

  it("matches the workflow manifest's leaf schema hash too", () => {
    const wf = golden<{ activities: { input_schema: SchemaIdentity }[] }>(
      "manifest/golden/workflow_execution.json",
    );
    expect(schemaHash(inputSchema)).toBe(wf.activities[0]?.input_schema.hash);
  });

  it("messagesHash reproduces prompt_messages_hash and rendered_messages_hash", () => {
    // The exact messages the Python baseline hashed
    // (tests/test_contracts_shapes.py::build_goldens).
    expect(messagesHash([{ role: "user", content: "Classify: {{ text }}" }])).toBe(
      activity.prompt_messages_hash,
    );
    expect(messagesHash([{ role: "user", content: "Classify: hello" }])).toBe(
      activity.rendered_messages_hash,
    );
  });

  it("includes an optional message name in the hash payload", () => {
    expect(messagesHash([{ role: "user", content: "x", name: "alice" }])).not.toBe(
      messagesHash([{ role: "user", content: "x" }]),
    );
  });
});
