import { loadYamlSpec, type TypefluxYamlSpec } from "@typeflux/temporal-yaml";
import { describe, expect, it } from "vitest";

import { buildWorkflowConnections, type ConnectionProbe, defaultConnectionProbe } from "../src/index.js";

/** A minimal valid spec; `runtimeExtra` injects registry/observability YAML under `runtime:`. */
const specWith = (runtimeExtra: string): TypefluxYamlSpec =>
  loadYamlSpec(
    `project: p\nname: n\ntask_queue: q\n` +
      `runtime:\n  temporal: {}\n  provider: { type: openai, model: gpt-4o-mini }\n${runtimeExtra}` +
      "activities:\n  definitions: [{ name: a, input: schemas:In, output: schemas:Out, prompt: p/x }]\n" +
      "workflow:\n  name: W\n  input: schemas:In\n  steps: [{ id: s, activity: a }]\n",
    { env: {} },
  );

const INLINE = "  registry: { type: inline, prompts: { p/x: hi } }\n";

describe("buildWorkflowConnections — default (network-free) probe (#563 slice 2b)", () => {
  it("an inline registry with no observability block reads as reachable, with manifest+redaction ON", async () => {
    const conns = await buildWorkflowConnections(specWith(INLINE), "review", "prod", defaultConnectionProbe);
    expect(conns).toEqual({
      workflow_id: "review",
      environment_id: "prod",
      // inline registry: nothing to reach → reachable, host/detail omitted (exclude_none).
      registry: { type: "inline", reachable: true },
      // no observability block → observer "none"; execution_manifest + redaction default TRUE (parity).
      observability: { type: "none", reachable: true, execution_manifest: true, redaction_enabled: true },
    });
  });

  it("a langfuse observer reads as un-probed (reachable:false + detail) without a real probe injected", async () => {
    const conns = await buildWorkflowConnections(specWith(`${INLINE}  observability: { type: langfuse }\n`), "review", "prod", defaultConnectionProbe);
    expect(conns.registry).toEqual({ type: "inline", reachable: true });
    expect(conns.observability).toMatchObject({ type: "langfuse", reachable: false, execution_manifest: true, redaction_enabled: true });
    expect(conns.observability.detail).toMatch(/not probed/);
  });

  it("honors explicit execution_manifest:false and redaction.enabled:false (no silent default-ON)", async () => {
    const conns = await buildWorkflowConnections(
      specWith(`${INLINE}  observability: { type: none, execution_manifest: false, redaction: { enabled: false } }\n`),
      "review",
      "prod",
      defaultConnectionProbe,
    );
    expect(conns.observability).toEqual({ type: "none", reachable: true, execution_manifest: false, redaction_enabled: false });
  });

  it("a langfuse registry surfaces its configured host with an un-probed status", async () => {
    const conns = await buildWorkflowConnections(
      specWith("  registry: { type: langfuse, host: https://cloud.langfuse.com }\n"),
      "review",
      "prod",
      defaultConnectionProbe,
    );
    expect(conns.registry).toMatchObject({ type: "langfuse", host: "https://cloud.langfuse.com/", reachable: false });
    expect(conns.registry.detail).toMatch(/not probed/);
  });

  it("strips credentials from a host before returning it (userinfo + query never enter the contract; codex)", async () => {
    const conns = await buildWorkflowConnections(
      specWith('  registry: { type: langfuse, host: "https://user:s3cret@lf.example/api?token=abc" }\n'),
      "review",
      "prod",
      defaultConnectionProbe,
    );
    // userinfo + query stripped → no credentials in the secret-free contract.
    expect(conns.registry.host).toBe("https://lf.example/api");
  });

  it("strips credentials even from a non-absolute host (no-scheme / protocol-relative; codex)", async () => {
    const conns = await buildWorkflowConnections(
      specWith('  registry: { type: langfuse, host: "//user:pass@lf.example?token=abc" }\n'),
      "review",
      "prod",
      defaultConnectionProbe,
    );
    // `new URL` can't parse this, but userinfo + query are still stripped; the `//` prefix is kept.
    expect(conns.registry.host).toBe("//lf.example");
  });

  it("strips a credential even when the userinfo contains an embedded/duplicated `@` (finder)", async () => {
    const conns = await buildWorkflowConnections(
      specWith('  registry: { type: langfuse, host: "https://user:p@ss@lf.example/api" }\n'),
      "review",
      "prod",
      defaultConnectionProbe,
    );
    // Greedy-to-last-`@` strip: no fragment of the `user:p@ss` credential may survive.
    expect(conns.registry.host).toBe("https://lf.example/api");
  });

  it("does not let a smuggled newline hide a second credentialed host segment (finder)", async () => {
    const conns = await buildWorkflowConnections(
      // YAML double-quote turns \n into a real newline in the host value.
      specWith('  registry: { type: langfuse, host: "https://user:pass@lf.example\\nSECRET@evil?token=x" }\n'),
      "review",
      "prod",
      defaultConnectionProbe,
    );
    // Cut at the newline first → only the pre-newline host remains, userinfo stripped.
    expect(conns.registry.host).toBe("https://lf.example/");
  });

  it("strips credentials from a BACKSLASH-delimited authority (WHATWG normalizes `\\`→`/`; finder)", async () => {
    const conns = await buildWorkflowConnections(
      // YAML single-quote keeps the backslashes literal → the host is `https:\\user:pass@lf.example`.
      specWith(String.raw`  registry: { type: langfuse, host: 'https:\\user:pass@lf.example' }` + "\n"),
      "review",
      "prod",
      defaultConnectionProbe,
    );
    expect(conns.registry.host).not.toMatch(/user:pass/); // no credential fragment survives
    expect(conns.registry.host).toContain("lf.example");
  });

  it("does NOT mistake a backslash-delimited PATH `@` for a userinfo boundary (no host corruption; finder)", async () => {
    const conns = await buildWorkflowConnections(
      // `https://host\@evil` — WHATWG: host=`host`, path=`/@evil`; the host must not collapse to `evil`.
      specWith(String.raw`  registry: { type: langfuse, host: 'https://host\@evil' }` + "\n"),
      "review",
      "prod",
      defaultConnectionProbe,
    );
    expect(conns.registry.host).not.toBe("https://evil"); // the real host is never replaced by the trailing label
    expect(conns.registry.host).toContain("host");
  });

  it("strips a basic-auth credential from an OPAQUE host (no `//`, parsed as scheme:path; finder)", async () => {
    const conns = await buildWorkflowConnections(
      // `user:pass@host` with no `//` → WHATWG parses scheme + opaque path, not an authority.
      specWith('  registry: { type: langfuse, host: "pk-lf-abc:sk-lf-xyz@cloud.langfuse.com" }\n'),
      "review",
      "prod",
      defaultConnectionProbe,
    );
    expect(conns.registry.host).not.toMatch(/sk-lf-xyz|pk-lf-abc/); // no credential survives
    expect(conns.registry.host).toContain("cloud.langfuse.com");
  });

  it("treats an empty-string langfuse host as absent (Python `or` truthiness, not `??`)", async () => {
    const conns = await buildWorkflowConnections(
      specWith('  registry: { type: langfuse, host: "" }\n'),
      "review",
      "prod",
      defaultConnectionProbe,
    );
    // Python `configured_host or …` → "" is falsy → host falls through to env/None → omitted, not "".
    expect(conns.registry).not.toHaveProperty("host");
    expect(conns.registry).toMatchObject({ type: "langfuse", reachable: false });
  });
});

describe("buildWorkflowConnections — injected probe (#563 slice 2b)", () => {
  it("uses the injected probe's verdict (a real async langfuse probe reports reachable + host)", async () => {
    const liveProbe: ConnectionProbe = async ({ type, configuredHost }) =>
      type === "langfuse" ? { reachable: true, host: configuredHost ?? "https://cloud.langfuse.com" } : { reachable: true, host: null };
    const conns = await buildWorkflowConnections(specWith(`${INLINE}  observability: { type: langfuse }\n`), "review", "prod", liveProbe);
    expect(conns.observability).toMatchObject({ type: "langfuse", reachable: true, host: "https://cloud.langfuse.com/" });
    expect(conns.observability).not.toHaveProperty("detail"); // reachable → no detail
  });

  it("keeps the configured host when a probe reports only reachability (host omitted, not null; codex)", async () => {
    const reachabilityOnly: ConnectionProbe = ({ type }) => ({ reachable: type === "langfuse" }); // no host reported
    const conns = await buildWorkflowConnections(
      specWith("  registry: { type: langfuse, host: https://lf.example }\n"),
      "review",
      "prod",
      reachabilityOnly,
    );
    // Probe omitted host → fall back to the configured host (endpoint not erased).
    expect(conns.registry).toMatchObject({ type: "langfuse", reachable: true, host: "https://lf.example/" });
    // The observer (type "none", no configured host) stays host-less.
    expect(conns.observability).not.toHaveProperty("host");
  });

  it("passes the selected environmentId to the probe so it can resolve per-environment settings (codex)", async () => {
    const seen: string[] = [];
    const recordingProbe: ConnectionProbe = ({ environmentId }) => {
      seen.push(environmentId);
      return { reachable: true, host: null };
    };
    await buildWorkflowConnections(specWith(`${INLINE}  observability: { type: langfuse }\n`), "review", "prod", recordingProbe);
    expect(seen).toEqual(["prod", "prod"]); // both the registry + observer probes get the environment
  });

  it("degrades a THROWING (rejecting) probe to unreachable without a raw error message or a broken request", async () => {
    const throwingProbe: ConnectionProbe = async ({ type }) => {
      if (type === "langfuse") throw new Error("connect https://user:secret@lf.example failed");
      return { reachable: true, host: null };
    };
    const conns = await buildWorkflowConnections(
      specWith("  registry: { type: langfuse, host: https://lf.example }\n  observability: { type: langfuse }\n"),
      "review",
      "prod",
      throwingProbe,
    );
    // A backend outage → that backend is unreachable with a GENERIC detail (no leaked error text /
    // credentials), and the whole projection still returns. Pin BOTH halves exactly.
    expect(conns.registry).toEqual({ type: "langfuse", host: "https://lf.example/", reachable: false, detail: "probe failed" });
    expect(conns.observability).toEqual({
      type: "langfuse",
      reachable: false,
      detail: "probe failed",
      execution_manifest: true,
      redaction_enabled: true,
    });
  });
});
