/** The `runtime.temporal.tls` → connection TLS options mapping (#685; Python `yaml/tls.py`). */

import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, describe, expect, it, vi } from "vitest";

import { loadYamlSpec } from "../src/loader.js";
import { temporalConnectionOptions, type TemporalTlsOptions, temporalTlsOptions } from "../src/temporal-tls.js";

// `~` expansion is part of the mapping contract, so `homedir()` must be steerable:
// tests point it at a scratch dir instead of writing into the real home.
const mocks = vi.hoisted(() => ({ home: "" }));
vi.mock("node:os", async (importOriginal) => {
  const actual = await importOriginal<typeof import("node:os")>();
  return { ...actual, homedir: () => (mocks.home === "" ? actual.homedir() : mocks.home) };
});

const ENV_KEY = "TYPEFLUX_TEST_TLS_SECRET";
const savedEnv = process.env[ENV_KEY];

afterEach(() => {
  mocks.home = "";
  if (savedEnv === undefined) delete process.env[ENV_KEY];
  else process.env[ENV_KEY] = savedEnv;
});

const scratch = () => mkdtempSync(join(tmpdir(), "typeflux-tls-"));

describe("temporalTlsOptions (#685; Python build_temporal_tls_config)", () => {
  it("passes booleans through and materializes Python's tls=False default for undefined", () => {
    expect(temporalTlsOptions(true)).toBe(true);
    expect(temporalTlsOptions(false)).toBe(false);
    expect(temporalTlsOptions(undefined)).toBe(false);
  });

  it("maps file paths to the exact file bytes and domain to serverNameOverride", () => {
    const dir = scratch();
    writeFileSync(join(dir, "ca.pem"), "root-ca-bytes");
    writeFileSync(join(dir, "client.pem"), "client-cert-bytes");
    writeFileSync(join(dir, "client.key"), "client-key-bytes");
    const options = temporalTlsOptions({
      server_root_ca_cert_file: join(dir, "ca.pem"),
      domain: "temporal.internal",
      client_cert_file: join(dir, "client.pem"),
      client_private_key_file: join(dir, "client.key"),
    }) as TemporalTlsOptions;
    expect(options.serverNameOverride).toBe("temporal.internal");
    expect(options.serverRootCACertificate).toEqual(Buffer.from("root-ca-bytes"));
    expect(options.clientCertPair).toEqual({
      crt: Buffer.from("client-cert-bytes"),
      key: Buffer.from("client-key-bytes"),
    });
  });

  it("expands ~ in cert file paths against the home directory", () => {
    const home = scratch();
    mocks.home = home;
    mkdirSync(join(home, "certs"));
    writeFileSync(join(home, "certs", "ca.pem"), "home-ca");
    const options = temporalTlsOptions({ server_root_ca_cert_file: "~/certs/ca.pem" }) as TemporalTlsOptions;
    expect(options.serverRootCACertificate).toEqual(Buffer.from("home-ca"));
    expect(options.clientCertPair).toBeUndefined();
  });

  it("fails loudly on a missing cert file (never a silent server-trust downgrade)", () => {
    expect(() => temporalTlsOptions({ server_root_ca_cert_file: "/nope/absent-ca.pem" })).toThrow(
      /could not read runtime\.temporal\.tls\.server_root_ca_cert_file: \/nope\/absent-ca\.pem/,
    );
  });

  it("resolves inline value_from env secrets to trimmed utf-8 bytes", () => {
    process.env[ENV_KEY] = "  pem-material \n";
    const options = temporalTlsOptions({
      server_root_ca_cert: { value_from: { env: ENV_KEY } },
    }) as TemporalTlsOptions;
    expect(options.serverRootCACertificate).toEqual(Buffer.from("pem-material"));
  });

  it("fails loudly on a required-but-unset env secret", () => {
    delete process.env[ENV_KEY];
    expect(() => temporalTlsOptions({ client_cert: { value_from: { env: ENV_KEY } }, client_private_key: { value_from: { env: ENV_KEY } } })).toThrow(
      new RegExp(`missing required secret for runtime\\.temporal\\.tls\\.client_cert: env ${ENV_KEY} is not set`),
    );
  });

  it("refuses a half-resolved mTLS pair (optional key unset must not weaken to server-auth TLS)", () => {
    const dir = scratch();
    writeFileSync(join(dir, "client.pem"), "client-cert-bytes");
    expect(() =>
      temporalTlsOptions({
        client_cert_file: join(dir, "client.pem"),
        client_private_key: { value_from: { env: ENV_KEY, required: false } },
      }),
    ).toThrow(/mTLS pair must resolve together/);
  });

  it("re-validates a raw block: unknown keys and an incomplete declared pair fail loudly", () => {
    expect(() => temporalTlsOptions({ ca: "x" } as never)).toThrow(/invalid runtime\.temporal\.tls block/);
    expect(() => temporalTlsOptions({ client_cert_file: "/tmp/c.pem" } as never)).toThrow(
      /must be configured together/,
    );
  });
});

describe("spec-level structured tls (#685; Python TemporalTLSConfigSpec)", () => {
  const spec = (tlsYaml: string) =>
    loadYamlSpec(
      `project: p\nname: n\ntask_queue: q\nruntime:\n  temporal:\n    tls:\n${tlsYaml}` +
        "  registry: { type: inline, prompts: { x: hi } }\n  provider: { type: openai, model: gpt-4o-mini }\n" +
        "activities:\n  definitions:\n    - { name: a, input: schemas:In, output: schemas:Out, prompt: x }\n" +
        "workflow:\n  name: W\n  input: schemas:In\n  steps:\n    - { id: s1, activity: a }\n",
      { env: {} },
    );

  it("parses the full structured block and normalizes blank strings to absent", () => {
    const parsed = spec(
      "      server_root_ca_cert_file: /secrets/ca.pem\n" +
        "      domain: ''\n" +
        "      client_cert:\n        value_from: { env: CLIENT_PEM }\n" +
        "      client_private_key:\n        value_from: { env: CLIENT_KEY }\n",
    );
    const tls = parsed.runtime.temporal.tls;
    expect(tls).toMatchObject({
      server_root_ca_cert_file: "/secrets/ca.pem",
      client_cert: { value_from: { env: "CLIENT_PEM" } },
    });
    expect((tls as { domain?: string }).domain).toBeUndefined();
  });

  it("rejects a slot configured both inline and as a file (Python parity)", () => {
    expect(() =>
      spec(
        "      server_root_ca_cert_file: /secrets/ca.pem\n" +
          "      server_root_ca_cert:\n        value_from: { env: CA_PEM }\n",
      ),
    ).toThrow(/server_root_ca_cert_file and runtime\.temporal\.tls\.server_root_ca_cert cannot both/);
  });

  it("rejects a client cert without its private key (and vice versa)", () => {
    expect(() => spec("      client_cert_file: /secrets/client.pem\n")).toThrow(
      /must be configured together/,
    );
  });

  it("rejects unknown keys in the tls block (strict, no silent misconfiguration)", () => {
    // The boolean|block union collapses member errors, so the message is the
    // union's, pointing at the tls path — still a loud load-time failure.
    expect(() => spec("      ca_file: /secrets/ca.pem\n")).toThrow(/at runtime\.temporal\.tls/);
  });
});

describe("temporalConnectionOptions (#687 review — the worker entrypoint's fail-closed dial)", () => {
  it("carries tls:true AND the env-resolved api key together (the runbook's Temporal Cloud pattern)", () => {
    process.env[ENV_KEY] = "cloud-api-key";
    const options = temporalConnectionOptions(
      {
        address: "your-namespace.a1b2c.tmprl.cloud:7233",
        namespace: "your-namespace.a1b2c",
        tls: true,
        api_key: { value_from: { env: ENV_KEY } },
      },
      process.env,
    );
    expect(options).toEqual({
      address: "your-namespace.a1b2c.tmprl.cloud:7233",
      namespace: "your-namespace.a1b2c",
      tls: true,
      apiKey: "cloud-api-key",
    });
  });

  it("resolves a structured mTLS block to real cert bytes (the #685 machinery, not a re-derivation)", () => {
    const dir = scratch();
    writeFileSync(join(dir, "ca.pem"), "root-ca-bytes");
    writeFileSync(join(dir, "client.pem"), "client-cert-bytes");
    writeFileSync(join(dir, "client.key"), "client-key-bytes");
    const options = temporalConnectionOptions({
      address: "temporal.example.com:7233",
      tls: {
        domain: "temporal.example.com",
        server_root_ca_cert: { value_from: { file: join(dir, "ca.pem") } },
        client_cert: { value_from: { file: join(dir, "client.pem") } },
        client_private_key: { value_from: { file: join(dir, "client.key") } },
      },
    });
    const tls = options.tls as TemporalTlsOptions;
    expect(tls.serverNameOverride).toBe("temporal.example.com");
    expect(tls.serverRootCACertificate?.toString()).toBe("root-ca-bytes");
    expect(tls.clientCertPair?.crt.toString()).toBe("client-cert-bytes");
    expect(tls.clientCertPair?.key.toString()).toBe("client-key-bytes");
  });

  it("fails CLOSED on a required-but-unset api key env — never an anonymous connection", () => {
    delete process.env[ENV_KEY];
    expect(() =>
      temporalConnectionOptions({ tls: true, api_key: { value_from: { env: ENV_KEY } } }, process.env),
    ).toThrow(new RegExp(`${ENV_KEY}.*required.*not set`));
    // `required: false` relaxes to no key, but the TLS posture is untouched.
    const relaxed = temporalConnectionOptions(
      { tls: true, api_key: { value_from: { env: ENV_KEY, required: false } } },
      process.env,
    );
    expect(relaxed.apiKey).toBeUndefined();
    expect(relaxed.tls).toBe(true);
  });

  it("reads a file-sourced api key (trimmed) and defaults address/namespace/tls like the spec", () => {
    const dir = scratch();
    writeFileSync(join(dir, "api-key"), "  file-api-key\n");
    const options = temporalConnectionOptions({ api_key: { value_from: { file: join(dir, "api-key") } } });
    expect(options).toEqual({
      address: "localhost:7233",
      namespace: "default",
      tls: false, // Python TemporalSpec default: tls = False
      apiKey: "file-api-key",
    });
    expect(() => temporalConnectionOptions({ api_key: { value_from: { file: join(dir, "missing") } } })).toThrow(
      /required by runtime\.temporal\.api_key does not exist/,
    );
  });
});
