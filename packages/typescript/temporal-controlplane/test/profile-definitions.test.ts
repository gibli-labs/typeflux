import {
  emptyProfileSources,
  loadEnvironmentSpec,
  loadProfileSpec,
  loadProjectSpec,
  type ProjectBundleSources,
} from "@typeflux/temporal-yaml";
import { describe, expect, it } from "vitest";

import { buildProfileDefinition, buildProfileSummaries, ProjectControlPlaneError } from "../src/index.js";

const PROJECT = loadProjectSpec(`
version: "1"
name: acme
workflows:
  - { id: review, path: review.yaml, profiles: { provider: anthropic-prod } }
  - { id: intake, path: intake.yaml }
profiles:
  provider:
    anthropic-prod: profiles/anthropic.yaml
  runtime:
    hardened: profiles/hardened.yaml
environments:
  prod: envs/prod.yaml
`);

const PROVIDER_PROFILE =
  "name: anthropic-prod\nkind: provider\nruntime:\n  provider:\n    type: anthropic\n" +
  "    api_key: { value_from: { env: ANTHROPIC_API_KEY } }\n";

const sources: ProjectBundleSources = {
  policies: {},
  // prod selects the runtime profile for `intake` via workflow_profiles — the "id (env)" case.
  environments: {
    prod: loadEnvironmentSpec("name: prod\nworkflows:\n  intake:\n    profiles: { runtime: hardened }\n"),
  },
  workflows: {},
  profiles: {
    provider: { "anthropic-prod": loadProfileSpec(PROVIDER_PROFILE) },
    registry: {},
    runtime: {
      hardened: loadProfileSpec("name: hardened\nkind: runtime\nruntime:\n  temporal: { namespace: prod }\n"),
    },
  },
};

const expectStatus = (fn: () => unknown, status: number, message: RegExp): void => {
  let caught: unknown;
  try {
    fn();
  } catch (error) {
    caught = error;
  }
  expect(caught).toBeInstanceOf(ProjectControlPlaneError);
  expect((caught as ProjectControlPlaneError).status).toBe(status);
  expect((caught as Error).message).toMatch(message);
};

describe("buildProfileSummaries (#570)", () => {
  it("lists declared profiles in fixed kind order with ids sorted within each kind", () => {
    const summaries = buildProfileSummaries(PROJECT, sources);
    expect(summaries.map((entry) => [entry.kind, entry.id])).toEqual([
      ["provider", "anthropic-prod"],
      ["runtime", "hardened"],
    ]);
    const [provider] = summaries;
    expect(provider?.name).toBe("anthropic-prod");
    expect(provider?.content_hash).toMatch(/^[0-9a-f]{64}$/);
    expect(provider?.path).toBe("profiles/anthropic.yaml");
  });

  it("returns [] when the manifest declares no profiles", () => {
    const bare = loadProjectSpec('version: "1"\nname: acme\nworkflows:\n  - { id: w, path: w.yaml }\n');
    expect(buildProfileSummaries(bare, { ...sources, profiles: emptyProfileSources() })).toEqual([]);
  });

  it("422s a declared profile with no supplied source", () => {
    expectStatus(
      () => buildProfileSummaries(PROJECT, { ...sources, profiles: emptyProfileSources() }),
      422,
      /profile source not provided for declared profile: provider\/anthropic-prod/,
    );
  });
});

describe("buildProfileDefinition (#570)", () => {
  it("projects content + used_by mixing bare workflow ids and environment-suffixed selections", () => {
    const provider = buildProfileDefinition(PROJECT, sources, "provider", "anthropic-prod");
    expect(provider.used_by).toEqual(["review"]);
    expect(provider.runtime["provider"]).toMatchObject({ type: "anthropic" });

    const runtime = buildProfileDefinition(PROJECT, sources, "runtime", "hardened");
    expect(runtime.used_by).toEqual(["intake (prod)"]);
    expect(runtime.path).toBe("profiles/hardened.yaml");
  });

  it("serializes secrets as value_from references only — never a raw credential", () => {
    const payload = JSON.stringify(buildProfileDefinition(PROJECT, sources, "provider", "anthropic-prod"));
    expect(payload).toContain("ANTHROPIC_API_KEY");
    expect(payload).not.toMatch(/sk-/);
  });

  it("redacts a LITERAL credential in the profile runtime (codex; divergence from Python, flagged)", () => {
    const leaky: ProjectBundleSources = {
      ...sources,
      profiles: {
        ...sources.profiles,
        provider: {
          "anthropic-prod": loadProfileSpec(
            "name: anthropic-prod\nkind: provider\nruntime:\n  provider:\n    type: anthropic\n" +
              "    api_key: sk-live-supersecret\n",
          ),
        },
      },
    };
    const detail = buildProfileDefinition(PROJECT, leaky, "provider", "anthropic-prod");
    expect((detail.runtime["provider"] as Record<string, unknown>)["api_key"]).toBe("***");
    expect(JSON.stringify(detail)).not.toContain("sk-live-supersecret");
  });

  it("redacts custom-extension config map entries in a raw profile fragment (#792)", () => {
    // Profiles never pass full spec validation (which stub-rejects `config` in TS), so a
    // fragment CAN carry a config map — literal entries mask whole, references stay.
    const leaky: ProjectBundleSources = {
      ...sources,
      profiles: {
        ...sources.profiles,
        provider: {
          "anthropic-prod": loadProfileSpec(
            "name: anthropic-prod\nkind: provider\nruntime:\n  provider:\n    type: custom\n" +
              "    class: acme.providers:AcmeProvider\n" +
              "    config:\n" +
              "      endpoint: https://acme.internal\n" +
              "      api_key: sk-live-smuggled-in-config\n" +
              "      token: { value_from: { env: ACME_TOKEN } }\n",
          ),
        },
      },
    };
    const detail = buildProfileDefinition(PROJECT, leaky, "provider", "anthropic-prod");
    const config = (detail.runtime["provider"] as Record<string, unknown>)["config"] as Record<string, unknown>;
    expect(config["endpoint"]).toBe("***");
    expect(config["api_key"]).toBe("***");
    expect(config["token"]).toEqual({ value_from: { env: "ACME_TOKEN" } });
    expect(JSON.stringify(detail)).not.toContain("sk-live-smuggled-in-config");
  });

  it("masks a NON-MAP value occupying the config slot whole (codex on #792)", () => {
    const leaky: ProjectBundleSources = {
      ...sources,
      profiles: {
        ...sources.profiles,
        provider: {
          "anthropic-prod": loadProfileSpec(
            "name: anthropic-prod\nkind: provider\nruntime:\n  provider:\n    type: custom\n" +
              "    class: acme.providers:AcmeProvider\n" +
              "    config: sk-live-bare-string\n",
          ),
        },
      },
    };
    const detail = buildProfileDefinition(PROJECT, leaky, "provider", "anthropic-prod");
    expect((detail.runtime["provider"] as Record<string, unknown>)["config"]).toBe("***");
    expect(JSON.stringify(detail)).not.toContain("sk-live-bare-string");
  });

  it("redacts a NON-STRING value occupying a secret slot (codex: numeric api_key)", () => {
    const leaky: ProjectBundleSources = {
      ...sources,
      profiles: {
        ...sources.profiles,
        provider: {
          "anthropic-prod": loadProfileSpec(
            "name: anthropic-prod\nkind: provider\nruntime:\n  provider:\n    type: anthropic\n" +
              "    api_key: 123456\n",
          ),
        },
      },
    };
    const detail = buildProfileDefinition(PROJECT, leaky, "provider", "anthropic-prod");
    expect((detail.runtime["provider"] as Record<string, unknown>)["api_key"]).toBe("***");
    expect(JSON.stringify(detail)).not.toContain("123456");
  });

  it("sanitizes a credentialed registry host URL in the profile runtime (codex)", () => {
    const leaky: ProjectBundleSources = {
      ...sources,
      profiles: {
        ...sources.profiles,
        registry: {
          langfuse: loadProfileSpec(
            "name: langfuse\nkind: registry\nruntime:\n  registry:\n    type: langfuse\n" +
              "    host: https://user:sk-token@lf.example/api\n",
          ),
        },
      },
    };
    const project = loadProjectSpec(
      'version: "1"\nname: acme\nworkflows:\n  - { id: w, path: w.yaml }\n' +
        "profiles:\n  registry:\n    langfuse: profiles/langfuse.yaml\n",
    );
    const detail = buildProfileDefinition(project, leaky, "registry", "langfuse");
    const host = (detail.runtime["registry"] as Record<string, unknown>)["host"] as string;
    expect(host).not.toContain("sk-token");
    expect(host).toContain("lf.example");
  });

  it("redacts a MALFORMED reference shape wholesale (codex: credential smuggled beside value_from)", () => {
    const leaky: ProjectBundleSources = {
      ...sources,
      profiles: {
        ...sources.profiles,
        provider: {
          "anthropic-prod": loadProfileSpec(
            "name: anthropic-prod\nkind: provider\nruntime:\n  provider:\n    type: anthropic\n" +
              "    api_key: { value_from: { env: KEY }, fallback: sk-live-smuggled }\n",
          ),
        },
      },
    };
    const detail = buildProfileDefinition(PROJECT, leaky, "provider", "anthropic-prod");
    expect((detail.runtime["provider"] as Record<string, unknown>)["api_key"]).toBe("***");
    expect(JSON.stringify(detail)).not.toContain("sk-live-smuggled");
  });

  it("redacts literal temporal api_key and tls cert material too (all allowlist slots)", () => {
    const leaky: ProjectBundleSources = {
      ...sources,
      profiles: {
        ...sources.profiles,
        runtime: {
          hardened: loadProfileSpec(
            "name: hardened\nkind: runtime\nruntime:\n  temporal:\n    api_key: tmprl-secret\n" +
              "    tls:\n      client_private_key: PEM-PRIVATE-KEY\n" +
              "      server_root_ca_cert: { value_from: { env: CA_CERT_PEM } }\n",
          ),
        },
      },
    };
    const detail = buildProfileDefinition(PROJECT, leaky, "runtime", "hardened");
    const temporal = detail.runtime["temporal"] as Record<string, unknown>;
    expect(temporal["api_key"]).toBe("***");
    expect((temporal["tls"] as Record<string, unknown>)["client_private_key"]).toBe("***");
    // A value_from reference is provenance, not a secret — rendered as-is.
    expect((temporal["tls"] as Record<string, unknown>)["server_root_ca_cert"]).toEqual({
      value_from: { env: "CA_CERT_PEM" },
    });
    expect(JSON.stringify(detail)).not.toMatch(/tmprl-secret|PEM-PRIVATE-KEY/);
  });

  it("422s an environment referencing an undeclared workflow instead of fabricating used_by (parity)", () => {
    const ghost: ProjectBundleSources = {
      ...sources,
      environments: {
        prod: loadEnvironmentSpec(
          "name: prod\nworkflows:\n  ghost-workflow:\n    profiles: { runtime: hardened }\n",
        ),
      },
    };
    expectStatus(
      () => buildProfileDefinition(PROJECT, ghost, "runtime", "hardened"),
      422,
      /environment 'prod' references unknown workflow: ghost-workflow/,
    );
  });

  it("422s an injected source whose self-declared kind mismatches its key", () => {
    const misKeyed: ProjectBundleSources = {
      ...sources,
      profiles: {
        ...sources.profiles,
        provider: { "anthropic-prod": loadProfileSpec("name: anthropic-prod\nkind: runtime\n") },
      },
    };
    expectStatus(
      () => buildProfileDefinition(PROJECT, misKeyed, "provider", "anthropic-prod"),
      422,
      /referenced under profiles\.provider but declares kind: runtime/,
    );
  });

  it("422s when a declared environment has no source — used_by must not silently understate", () => {
    expectStatus(
      () => buildProfileDefinition(PROJECT, { ...sources, environments: {} }, "provider", "anthropic-prod"),
      422,
      /environment source not provided for declared environment: prod/,
    );
  });

  it("deep-copies runtime — mutating the response cannot corrupt the loaded spec", () => {
    const def = buildProfileDefinition(PROJECT, sources, "runtime", "hardened");
    (def.runtime["temporal"] as Record<string, unknown>)["namespace"] = "MUTATED";
    expect(
      (buildProfileDefinition(PROJECT, sources, "runtime", "hardened").runtime["temporal"] as Record<string, unknown>)[
        "namespace"
      ],
    ).toBe("prod");
  });
});
