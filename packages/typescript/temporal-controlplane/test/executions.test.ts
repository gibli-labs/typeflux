/** The executions listing for the ts-plan-argument binding (#620): memo-filtered bounded scan. */

import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { loadYamlSpec, workflowPlanDigest, workflowPlanFromSpec } from "@typeflux/temporal-yaml";
import { describe, expect, it } from "vitest";

import {
  type ExecutionsVisibilityClient,
  listWorkflowExecutions,
  ProjectControlPlaneError,
  temporalConnectionOptions,
  type VisibilityExecutionInfo,
} from "../src/index.js";

const SPEC = loadYamlSpec(
  "project: p\nname: n\ntask_queue: q\n" +
    "runtime:\n  temporal: { address: 'localhost:7233' }\n" +
    "  registry: { type: inline, prompts: { x: hi } }\n  provider: { type: openai, model: gpt-4o-mini }\n" +
    "activities:\n  definitions:\n    - { name: a, input: schemas:In, output: schemas:In, prompt: x }\n" +
    "workflow:\n  name: W\n  input: schemas:In\n  steps:\n    - { id: s1, activity: a }\n",
  { env: {} },
);
const CURRENT_DIGEST = workflowPlanDigest(workflowPlanFromSpec(SPEC));

const row = (overrides: Partial<VisibilityExecutionInfo>): VisibilityExecutionInfo => ({
  workflowId: "wf-1",
  runId: "run-1",
  type: "typefluxYamlWorkflow",
  status: { name: "RUNNING" },
  startTime: new Date("2026-07-09T10:00:00Z"),
  memo: { typeflux_workflow: "W", typeflux_project: "p", typeflux_spec_digest: CURRENT_DIGEST },
  ...overrides,
});

const fakeClient = (rows: VisibilityExecutionInfo[], seen: string[] = []): ExecutionsVisibilityClient => ({
  // eslint-disable-next-line @typescript-eslint/require-await
  async *list(query: string) {
    seen.push(query);
    yield* rows;
  },
  close: async () => undefined,
} as unknown as ExecutionsVisibilityClient);

describe("listWorkflowExecutions (#620)", () => {
  it("filters by identity memo, flags current_version by digest, and maps the record shape", async () => {
    const rows = [
      row({}),
      // Foreign memo: another project's execution under the same generic type — excluded.
      row({ workflowId: "foreign", memo: { typeflux_workflow: "W", typeflux_project: "other" } }),
      // Same identity, stale digest: listed with current_version false; closed with times.
      row({
        workflowId: "wf-old",
        runId: undefined,
        status: { name: "COMPLETED" },
        closeTime: new Date("2026-07-09T11:00:00Z"),
        memo: { typeflux_workflow: "W", typeflux_project: "p", typeflux_spec_digest: "stale" },
      }),
    ];
    const queries: string[] = [];
    const list = await listWorkflowExecutions(SPEC, {
      workflowId: "wf",
      environmentId: "local",
      clientFactory: async () => fakeClient(rows, queries),
    });
    expect(queries).toEqual(["WorkflowType = 'typefluxYamlWorkflow'"]);
    expect(list.logical_workflow).toBe("W");
    expect(list.current_workflow_type).toBe("typefluxYamlWorkflow");
    expect(list.executions).toEqual([
      {
        execution_id: "wf-1",
        run_id: "run-1",
        workflow_type: "typefluxYamlWorkflow",
        current_version: true,
        status: "RUNNING",
        start_time: "2026-07-09T10:00:00.000Z",
        close_time: null,
      },
      {
        execution_id: "wf-old",
        run_id: null,
        workflow_type: "typefluxYamlWorkflow",
        current_version: false,
        status: "COMPLETED",
        start_time: "2026-07-09T10:00:00.000Z",
        close_time: "2026-07-09T11:00:00.000Z",
      },
    ]);
  });

  it("narrows the query with the configured search attribute and clamps the limit", async () => {
    const attrSpec = loadYamlSpec(
      "project: p\nname: n\ntask_queue: q\n" +
        "runtime:\n  temporal: { address: 'localhost:7233', workflow_search_attribute: TypefluxWorkflow }\n" +
        "  registry: { type: inline, prompts: { x: hi } }\n  provider: { type: openai, model: gpt-4o-mini }\n" +
        "activities:\n  definitions:\n    - { name: a, input: schemas:In, output: schemas:In, prompt: x }\n" +
        "workflow:\n  name: W\n  input: schemas:In\n  steps:\n    - { id: s1, activity: a }\n",
      { env: {} },
    );
    const rows = Array.from({ length: 10 }, (_, i) => row({ workflowId: `wf-${i}` }));
    const queries: string[] = [];
    const list = await listWorkflowExecutions(attrSpec, {
      workflowId: "wf",
      environmentId: "local",
      limit: -5, // clamps to 1
      clientFactory: async () => fakeClient(rows, queries),
    });
    expect(queries).toEqual(["WorkflowType = 'typefluxYamlWorkflow' AND TypefluxWorkflow = 'W'"]);
    expect(list.executions).toHaveLength(1);
  });

  it("maps a failed connect to 503 TemporalUnavailable, never a 500", async () => {
    let caught: ProjectControlPlaneError | undefined;
    try {
      await listWorkflowExecutions(SPEC, {
        workflowId: "wf",
        environmentId: "local",
        clientFactory: async () => {
          throw new Error("Failed client connect: connection refused");
        },
      });
    } catch (error) {
      caught = error as ProjectControlPlaneError;
    }
    expect(caught?.status).toBe(503);
    expect(caught?.errorName).toBe("TemporalUnavailable");
    expect(caught?.message).toMatch(/^listing executions failed: /);
  });
});

describe("executions route limit validation", () => {
  it("rejects empty-string and float-notation limits like FastAPI int coercion", async () => {
    const { buildRoutes, loadProjectRegistry } = await import("../src/index.js");
    const { CONFORMANCE_SCHEMAS } = await import("../src/http/conformance-schemas.js");
    const { fileURLToPath } = await import("node:url");
    const { dirname, resolve } = await import("node:path");
    const registryPath = resolve(
      dirname(fileURLToPath(import.meta.url)),
      "../../../../contracts/controlplane/conformance/project/typescript/typeflux.projects.yaml",
    );
    const routes = buildRoutes({
      registry: loadProjectRegistry(registryPath),
      schemasFor: () => CONFORMANCE_SCHEMAS,
    });
    const route = routes.find(
      (r) => r.method === "GET" && r.segments.join("/") === "api/v1/workflows/{workflow_id}/executions",
    )!;
    // `Number("")` is 0 and `Number("1e2")` is 100 — FastAPI rejects both as invalid integers.
    for (const bad of ["", "1e2", " 7 ", "2.5"]) {
      await expect(
        route.handler({
          params: { workflow_id: "workflow" },
          query: { environment_id: "local", limit: bad },
          body: () => undefined,
          headers: {},
        }),
      ).rejects.toThrow(/query\.limit: Input should be a valid integer/);
    }
  });
});

describe("temporalConnectionOptions (ts binding mapping)", () => {
  it("maps address/namespace/tls/api_key with Python-parity defaults", () => {
    expect(temporalConnectionOptions(SPEC)).toEqual({
      address: "localhost:7233",
      namespace: "default",
      tls: false,
      apiKey: undefined,
    });
  });

  it("maps a structured TLS block to real cert bytes (#685)", () => {
    const dir = mkdtempSync(join(tmpdir(), "typeflux-cp-tls-"));
    writeFileSync(join(dir, "ca.pem"), "root-ca-bytes");
    writeFileSync(join(dir, "client.pem"), "client-cert-bytes");
    writeFileSync(join(dir, "client.key"), "client-key-bytes");
    const spec = {
      ...SPEC,
      runtime: {
        ...SPEC.runtime,
        temporal: {
          ...SPEC.runtime.temporal,
          tls: {
            server_root_ca_cert_file: join(dir, "ca.pem"),
            domain: "temporal.internal",
            client_cert_file: join(dir, "client.pem"),
            client_private_key_file: join(dir, "client.key"),
          },
        },
      },
    };
    expect(temporalConnectionOptions(spec as typeof SPEC).tls).toEqual({
      serverNameOverride: "temporal.internal",
      serverRootCACertificate: Buffer.from("root-ca-bytes"),
      clientCertPair: { crt: Buffer.from("client-cert-bytes"), key: Buffer.from("client-key-bytes") },
    });
  });

  it("fails closed (422 TsBindingConfigError) on an invalid structured TLS block", () => {
    const spec = { ...SPEC, runtime: { ...SPEC.runtime, temporal: { ...SPEC.runtime.temporal, tls: { ca: "x" } } } };
    try {
      temporalConnectionOptions(spec as unknown as typeof SPEC);
      expect.unreachable("an unknown tls key must fail closed");
    } catch (error) {
      expect(error).toBeInstanceOf(ProjectControlPlaneError);
      expect((error as ProjectControlPlaneError).message).toMatch(/invalid runtime\.temporal\.tls block/);
      expect((error as { status?: number }).status).toBe(422);
    }
  });

  it("fails closed on an unreadable cert file (never a silent server-trust downgrade)", () => {
    const spec = {
      ...SPEC,
      runtime: {
        ...SPEC.runtime,
        temporal: { ...SPEC.runtime.temporal, tls: { server_root_ca_cert_file: "/nope/absent-ca.pem" } },
      },
    };
    expect(() => temporalConnectionOptions(spec as unknown as typeof SPEC)).toThrow(
      /could not read runtime\.temporal\.tls\.server_root_ca_cert_file/,
    );
  });

  it("fails closed on a required-but-unset api_key env reference", () => {
    const spec = {
      ...SPEC,
      runtime: {
        ...SPEC.runtime,
        temporal: { ...SPEC.runtime.temporal, api_key: { value_from: { env: "MISSING_KEY" } } },
      },
    };
    expect(() => temporalConnectionOptions(spec as typeof SPEC, {})).toThrow(
      /'MISSING_KEY' is required by the project's temporal config/,
    );
  });

  // #188: the CP must build the spec's payload codec so its own clients (operate START,
  // history reads) round-trip encrypted payloads — mirroring Python binding_ts._connect.
  const codecSpec = (env: string) =>
    ({
      ...SPEC,
      runtime: {
        ...SPEC.runtime,
        temporal: {
          ...SPEC.runtime.temporal,
          payload_codec: {
            type: "aes",
            current: "k1",
            keys: [{ id: "k1", value_from: { env } }],
          },
        },
      },
    }) as unknown as typeof SPEC;

  it("resolves runtime.temporal.payload_codec into a working codec from the passed env", async () => {
    const key = ("01234567" + "89abcdef").repeat(2); // exactly 32 bytes (AES-256)
    const opts = temporalConnectionOptions(codecSpec("TF_CP_CODEC_KEY"), { TF_CP_CODEC_KEY: key });
    expect(opts.payloadCodec).toBeDefined();
    // Prove it is a real, round-tripping codec (encode → ciphertext marker → decode → plaintext).
    const sample = { metadata: { encoding: Buffer.from("json/plain") }, data: Buffer.from('{"x":1}') };
    const [sealed] = await opts.payloadCodec!.encode([sample]);
    expect(Buffer.from(sealed!.metadata!.encoding!).toString("utf8")).toBe("binary/encrypted");
    const [opened] = await opts.payloadCodec!.decode([sealed!]);
    expect(Buffer.from(opened!.data!).toString("utf8")).toBe('{"x":1}');
  });

  it("leaves payloadCodec undefined when the project declares no codec", () => {
    expect(temporalConnectionOptions(SPEC).payloadCodec).toBeUndefined();
  });

  it("rejects payload_codec.subject_scope fail-closed (#715 slice 4 — no CP keystore seam)", () => {
    // A subject-scoped project's lifecycle ops need the deployment's SHARED
    // SubjectKeystore; with no keystore seam here, silently using only the shared
    // codec would seal subject-execution signals OUTSIDE their shred domain.
    const key = ("01234567" + "89abcdef").repeat(2); // exactly 32 bytes (AES-256)
    const base = codecSpec("TF_CP_CODEC_KEY") as unknown as {
      runtime: { temporal: { payload_codec: Record<string, unknown> } };
    };
    const spec = {
      ...base,
      runtime: {
        ...base.runtime,
        temporal: {
          ...base.runtime.temporal,
          payload_codec: { ...base.runtime.temporal.payload_codec, subject_scope: {} },
        },
      },
    };
    expect(() => temporalConnectionOptions(spec as unknown as typeof SPEC, { TF_CP_CODEC_KEY: key })).toThrow(
      /subject_scope is not supported by this control plane/,
    );
  });

  it("fails closed on a declared codec whose key env is unset (never a plaintext client)", () => {
    expect(() => temporalConnectionOptions(codecSpec("TF_CP_CODEC_MISSING"), {})).toThrow(
      /env TF_CP_CODEC_MISSING is not set/,
    );
  });
});
