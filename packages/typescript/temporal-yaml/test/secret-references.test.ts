import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import { loadYamlSpec } from "../src/loader.js";
import { secretReferenceRecords } from "../src/secret-references.js";

const spec = (runtimeTemporal: string, providerApiKey: string) =>
  loadYamlSpec(
    `project: p\nname: n\ntask_queue: q\nruntime:\n  temporal: ${runtimeTemporal}\n` +
      "  registry: { type: inline, prompts: { p/x: hi } }\n" +
      `  provider: { type: openai, api_key: ${providerApiKey} }\n` +
      "activities:\n  definitions:\n    - { name: a, input: schemas:In, output: schemas:Out, prompt: p/x }\n" +
      "workflow:\n  name: W\n  input: schemas:In\n  steps:\n    - { id: s1, activity: a }\n",
    { env: {} },
  );

const ENV_KEY = "TYPEFLUX_TEST_SECRET_REF";
const savedEnv = process.env[ENV_KEY];

afterEach(() => {
  if (savedEnv === undefined) delete process.env[ENV_KEY];
  else process.env[ENV_KEY] = savedEnv;
});

describe("secretReferenceRecords (#575; Python secret_reference_records)", () => {
  it("records a literal by kind only and a set env reference as configured", () => {
    process.env[ENV_KEY] = "shh";
    const records = secretReferenceRecords(
      spec("{ api_key: literal-token }", `{ value_from: { env: ${ENV_KEY} } }`),
    );
    expect(records).toEqual([
      { runtime_path: "runtime.temporal.api_key", source_kind: "literal", source_name: "", configured: true },
      { runtime_path: "runtime.provider.api_key", source_kind: "env", source_name: ENV_KEY, configured: true },
    ]);
  });

  it("treats an unset or whitespace-only env var as not configured", () => {
    process.env[ENV_KEY] = "   ";
    const [record] = secretReferenceRecords(spec("{}", `{ value_from: { env: ${ENV_KEY} } }`));
    expect(record).toEqual({
      runtime_path: "runtime.provider.api_key",
      source_kind: "env",
      source_name: ENV_KEY,
      configured: false,
    });
  });

  it("classifies file references by existence and non-emptiness", () => {
    const dir = mkdtempSync(join(tmpdir(), "typeflux-secret-ref-"));
    const present = join(dir, "token.txt");
    writeFileSync(present, "tok");
    const empty = join(dir, "empty.txt");
    writeFileSync(empty, "");
    const records = secretReferenceRecords(
      spec(`{ api_key: { value_from: { file: '${present}' } } }`, `{ value_from: { file: '${empty}' } }`),
    );
    expect(records).toEqual([
      { runtime_path: "runtime.temporal.api_key", source_kind: "file", source_name: present, configured: true },
      { runtime_path: "runtime.provider.api_key", source_kind: "file", source_name: empty, configured: false },
    ]);
  });

  it("expands custom-extension config map slots keyed by entry (#792)", () => {
    // A full TS spec stub-rejects `config`, so the map walk is exercised structurally —
    // the walker stays total so the exported slot list means the same thing as Python's.
    const structural = {
      runtime: {
        provider: {
          type: "custom",
          config: { endpoint: "https://acme.internal", api_key: { value_from: { env: ENV_KEY } } },
        },
      },
    };
    process.env[ENV_KEY] = "shh";
    const records = secretReferenceRecords(structural as never);
    expect(records).toEqual([
      {
        runtime_path: "runtime.provider.config[endpoint]",
        source_kind: "literal",
        source_name: "",
        configured: true,
      },
      {
        runtime_path: "runtime.provider.config[api_key]",
        source_kind: "env",
        source_name: ENV_KEY,
        configured: true,
      },
    ]);
    expect(JSON.stringify(records)).not.toContain("acme.internal");
  });

  it("walks the tls block's cert slots only when tls is a block (structured spec, #685)", () => {
    process.env[ENV_KEY] = "pem";
    const withBlock = secretReferenceRecords(
      spec(
        `{ tls: { server_root_ca_cert: { value_from: { env: ${ENV_KEY} } }, ` +
          `client_cert: { value_from: { env: ${ENV_KEY} } }, ` +
          `client_private_key: { value_from: { env: ${ENV_KEY} } } } }`,
        "sk-live",
      ),
    );
    expect(withBlock).toEqual([
      {
        runtime_path: "runtime.temporal.tls.server_root_ca_cert",
        source_kind: "env",
        source_name: ENV_KEY,
        configured: true,
      },
      { runtime_path: "runtime.temporal.tls.client_cert", source_kind: "env", source_name: ENV_KEY, configured: true },
      {
        runtime_path: "runtime.temporal.tls.client_private_key",
        source_kind: "env",
        source_name: ENV_KEY,
        configured: true,
      },
      { runtime_path: "runtime.provider.api_key", source_kind: "literal", source_name: "", configured: true },
    ]);
    // A boolean tls has no cert slots to reference.
    expect(secretReferenceRecords(spec("{ tls: true }", "sk-live")).map((r) => r.runtime_path)).toEqual([
      "runtime.provider.api_key",
    ]);
  });
});
