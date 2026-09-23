/** Prompt-registry drift projection (#639): Python `workflow_prompt_status` parity, arm by arm. */

import { loadYamlSpec } from "@typeflux/temporal-yaml";
import { describe, expect, it } from "vitest";

import { buildWorkflowPromptStatus } from "../src/index.js";

const spec = (registry: string, activities: string) =>
  loadYamlSpec(
    `project: p\nname: n\ntask_queue: q\n` +
      `runtime:\n  temporal: {}\n${registry}\n  provider: { type: openai, model: gpt-4o-mini }\n` +
      `activities:\n  definitions:\n${activities}` +
      "workflow:\n  name: W\n  input: schemas:In\n  steps:\n    - { id: s1, activity: a }\n",
    { env: {} },
  );

describe("buildWorkflowPromptStatus (#639)", () => {
  it("inline: grouped by ref over sorted activities, in_sync, template from the user's YAML", async () => {
    const s = spec(
      "  registry: { type: inline, prompts: { shared: 'assess {{value}}', solo: 'sum {{x}}' } }",
      "    - { name: b, input: schemas:In, output: schemas:In, prompt: shared }\n" +
        "    - { name: a, input: schemas:In, output: schemas:In, prompt: shared }\n" +
        "    - { name: c, input: schemas:In, output: schemas:In, prompt: solo }\n",
    );
    expect(await buildWorkflowPromptStatus(s, "wf", "local")).toEqual({
      workflow_id: "wf",
      environment_id: "local",
      registry_type: "inline",
      prompts: [
        {
          name: "shared",
          mode: "inline",
          selector: "inline",
          status: "in_sync",
          // Sorted activity order (a before b), grouped under one ref.
          used_by_activities: ["a", "b"],
          template: "assess {{value}}",
        },
        {
          name: "solo",
          mode: "inline",
          selector: "inline",
          status: "in_sync",
          used_by_activities: ["c"],
          template: "sum {{x}}",
        },
      ],
    });
  });

  it("inline: a structured template renders as YAML, never a model repr (intended contract, #648)", async () => {
    const s = spec(
      "  registry:\n    type: inline\n    prompts:\n      chat:\n        messages:\n          - role: system\n            content: be brief\n",
      "    - { name: a, input: schemas:In, output: schemas:In, prompt: chat }\n",
    );
    const [prompt] = (await buildWorkflowPromptStatus(s, "wf", "local")).prompts;
    expect(prompt!.template).toContain("role: system");
    expect(prompt!.template).toContain("content: be brief");
    expect(prompt!.template).not.toContain("Object");
  });

  it("inline: a ref with no matching template omits `template` (Python exclude_none)", async () => {
    const s = spec(
      "  registry: { type: inline, prompts: { other: 'x' } }",
      "    - { name: a, input: schemas:In, output: schemas:In, prompt: missing }\n",
    );
    const [prompt] = (await buildWorkflowPromptStatus(s, "wf", "local")).prompts;
    expect(prompt).toEqual({
      name: "missing",
      mode: "inline",
      selector: "inline",
      status: "in_sync",
      used_by_activities: ["a"],
    });
    expect(Object.hasOwn(prompt!, "template")).toBe(false);
  });

  it("custom/langsmith: honest unknown with the requested selector (label WITHOUT @, dash when absent)", async () => {
    const s = spec(
      "  registry: { type: langsmith }",
      "    - { name: a, input: schemas:In, output: schemas:In, prompt: { name: pinned-p, version: 3 } }\n" +
        "    - { name: b, input: schemas:In, output: schemas:In, prompt: { name: labeled-p, label: prod } }\n" +
        "    - { name: c, input: schemas:In, output: schemas:In, prompt: bare-p }\n" +
        "    - { name: d, input: schemas:In, output: schemas:In, prompt: { name: empty-p, label: '' } }\n",
    );
    expect((await buildWorkflowPromptStatus(s, "wf", "local")).prompts).toEqual([
      { name: "pinned-p", mode: "pinned", selector: "v3", status: "unknown", used_by_activities: ["a"] },
      { name: "labeled-p", mode: "label", selector: "prod", status: "unknown", used_by_activities: ["b"] },
      { name: "bare-p", mode: "label", selector: "—", status: "unknown", used_by_activities: ["c"] },
      // Python `ref.label or "—"` is truthiness: a present-but-EMPTY label reads "—" too.
      { name: "empty-p", mode: "label", selector: "—", status: "unknown", used_by_activities: ["d"] },
    ]);
  });

  it("backend registry: pinned refs are static in_sync; label refs are honest unknown with detail", async () => {
    const s = spec(
      "  registry: { type: langfuse, label: staging }",
      "    - { name: a, input: schemas:In, output: schemas:In, prompt: { name: pin, version: 7 } }\n" +
        "    - { name: b, input: schemas:In, output: schemas:In, prompt: { name: lab, label: prod } }\n" +
        "    - { name: c, input: schemas:In, output: schemas:In, prompt: bare }\n",
    );
    const { registry_type, prompts } = await buildWorkflowPromptStatus(s, "wf", "local");
    expect(registry_type).toBe("langfuse");
    expect(prompts[0]).toEqual({
      name: "pin",
      mode: "pinned",
      selector: "v7",
      registry_version: "7",
      status: "in_sync",
      used_by_activities: ["a"],
    });
    expect(prompts[1]).toMatchObject({
      name: "lab",
      mode: "label",
      selector: "@prod",
      status: "unknown",
    });
    expect(prompts[1]!.detail).toMatch(/^registry lookup failed: /);
    // The registry-level default label applies to bare refs (Python `... or "production"`).
    expect(prompts[2]).toMatchObject({ name: "bare", mode: "label", selector: "@staging" });
  });

  it("backend registry: an EMPTY registry label falls through to production (Python truthiness)", async () => {
    const s = spec(
      "  registry: { type: langfuse, label: '' }",
      "    - { name: a, input: schemas:In, output: schemas:In, prompt: bare }\n",
    );
    expect((await buildWorkflowPromptStatus(s, "wf", "local")).prompts[0]).toMatchObject({
      selector: "@production",
    });
  });
});

describe("buildWorkflowPromptStatus — injected langfuse transport (#573)", () => {
  // A langfuse registry AND a langfuse observer so both the label lookup and the last-run
  // reconstruction run. `lab@prod` and a pinned `pin@v7`, plus a bare ref at the default label.
  const langfuseSpec = () =>
    spec(
      "  registry: { type: langfuse, label: staging }\n  observability: { type: langfuse }",
      "    - { name: a, input: schemas:In, output: schemas:In, prompt: { name: pin, version: 7 } }\n" +
        "    - { name: b, input: schemas:In, output: schemas:In, prompt: { name: lab, label: prod } }\n" +
        "    - { name: c, input: schemas:In, output: schemas:In, prompt: bare }\n",
    );

  /** A fake transport with scripted label versions + last-run versions (never touches the network). */
  const fakeTransport = (opts: {
    labelVersions?: Record<string, string>;
    lastRun?: Record<string, string>;
    throwOnLabel?: boolean;
  }) => ({
    ping: async () => undefined,
    promptLabelVersion: async ({ name }: { name: string }) => {
      if (opts.throwOnLabel) {
        // A custom-named error class — its NAME is the only thing a credential-safe detail may echo.
        const error = new Error("connect failed to lf.local:3000");
        error.name = "LangfuseLookupError";
        throw error;
      }
      return opts.labelVersions?.[name];
    },
    lastRunPromptVersions: async () => opts.lastRun ?? {},
    traceSummary: async () => null,
    searchEnforcementTraces: async () => [],
  });

  it("label ref: registry == last-run → in_sync; both versions reported", async () => {
    const status = await buildWorkflowPromptStatus(
      langfuseSpec(),
      "wf",
      "local",
      fakeTransport({ labelVersions: { lab: "5" }, lastRun: { lab: "5", pin: "7" } }),
    );
    const lab = status.prompts.find((p) => p.name === "lab")!;
    expect(lab).toMatchObject({ mode: "label", selector: "@prod", status: "in_sync", registry_version: "5", last_run_version: "5" });
  });

  it("label ref: registry != last-run → drift", async () => {
    const status = await buildWorkflowPromptStatus(
      langfuseSpec(),
      "wf",
      "local",
      fakeTransport({ labelVersions: { lab: "6" }, lastRun: { lab: "5" } }),
    );
    const lab = status.prompts.find((p) => p.name === "lab")!;
    expect(lab).toMatchObject({ status: "drift", registry_version: "6", last_run_version: "5" });
  });

  it("label ref: no last run for the prompt → unknown (one side missing), registry version still reported", async () => {
    const status = await buildWorkflowPromptStatus(
      langfuseSpec(),
      "wf",
      "local",
      fakeTransport({ labelVersions: { lab: "6" }, lastRun: {} }),
    );
    const lab = status.prompts.find((p) => p.name === "lab")!;
    expect(lab).toMatchObject({ status: "unknown", registry_version: "6" });
    expect(Object.hasOwn(lab, "last_run_version")).toBe(false);
  });

  it("label ref: a registry lookup that throws → unknown with a sanitized 'registry lookup failed' detail (no raw error)", async () => {
    const status = await buildWorkflowPromptStatus(
      langfuseSpec(),
      "wf",
      "local",
      fakeTransport({ throwOnLabel: true, lastRun: { lab: "5" } }),
    );
    const lab = status.prompts.find((p) => p.name === "lab")!;
    expect(lab.status).toBe("unknown");
    expect(lab.detail).toMatch(/^registry lookup failed: /);
    // The detail echoes ONLY the error's class NAME (a static identifier), never its message — the
    // fake's message ("connect failed to lf.local:3000") carries a host and must not leak.
    expect(lab.detail).toBe("registry lookup failed: LangfuseLookupError");
    expect(lab.detail).not.toContain("lf.local");
    expect(Object.hasOwn(lab, "registry_version")).toBe(false);
  });

  it("label ref: a plain Error whose message embeds a credential is MASKED, never leaked (#573)", async () => {
    // A plain `Error` (name "Error") carries no safe class identity, so its whole message is dropped —
    // a credential-bearing message with any spacing (`sk_live_…`, a host) never reaches the contract.
    const status = await buildWorkflowPromptStatus(langfuseSpec(), "wf", "local", {
      ping: async () => undefined,
      promptLabelVersion: async () => {
        throw new Error("auth failed for token abc123 at tenant.langfuse.example sk_live_deadbeef");
      },
      lastRunPromptVersions: async () => ({}),
      traceSummary: async () => null,
      searchEnforcementTraces: async () => [],
    });
    const lab = status.prompts.find((p) => p.name === "lab")!;
    expect(lab.status).toBe("unknown");
    expect(lab.detail).toBe("registry lookup failed: lookup error");
    expect(lab.detail).not.toContain("sk_live");
    expect(lab.detail).not.toContain("abc123");
    expect(lab.detail).not.toContain("langfuse.example");
  });

  it("pinned ref: gains last_run_version from the observer (a pre-#573 Python-parity gap), stays in_sync", async () => {
    const status = await buildWorkflowPromptStatus(
      langfuseSpec(),
      "wf",
      "local",
      fakeTransport({ lastRun: { pin: "7" } }),
    );
    const pin = status.prompts.find((p) => p.name === "pin")!;
    expect(pin).toMatchObject({ mode: "pinned", status: "in_sync", registry_version: "7", last_run_version: "7" });
  });

  it("last-run is gated on the OBSERVER being langfuse: a non-langfuse observer skips it (no last_run_version)", async () => {
    // Langfuse registry but observer `none` → the transport's lastRunPromptVersions is never called.
    const s = spec(
      "  registry: { type: langfuse, label: staging }\n  observability: { type: none }",
      "    - { name: a, input: schemas:In, output: schemas:In, prompt: { name: pin, version: 7 } }\n",
    );
    let lastRunCalls = 0;
    const status = await buildWorkflowPromptStatus(s, "wf", "local", {
      ping: async () => undefined,
      promptLabelVersion: async () => undefined,
      lastRunPromptVersions: async () => {
        lastRunCalls += 1;
        return {};
      },
      traceSummary: async () => null,
      searchEnforcementTraces: async () => [],
    });
    expect(lastRunCalls).toBe(0);
    expect(Object.hasOwn(status.prompts[0]!, "last_run_version")).toBe(false);
  });

  it("a prompt named like a prototype key ('toString') with no last run reads undefined, not an inherited value (#573)", async () => {
    // A langfuse registry with a prompt literally named `toString`; last-run is an empty plain object.
    const s = spec(
      "  registry: { type: langfuse, label: prod }\n  observability: { type: langfuse }",
      "    - { name: a, input: schemas:In, output: schemas:In, prompt: { name: toString, label: prod } }\n",
    );
    const status = await buildWorkflowPromptStatus(s, "wf", "local", {
      ping: async () => undefined,
      promptLabelVersion: async () => "5",
      lastRunPromptVersions: async () => ({}), // no `toString` entry — the own-property guard must apply
      traceSummary: async () => null,
      searchEnforcementTraces: async () => [],
    });
    const row = status.prompts.find((p) => p.name === "toString")!;
    // One side (last-run) is genuinely missing → unknown, and NO last_run_version (never a function).
    expect(row.status).toBe("unknown");
    expect(Object.hasOwn(row, "last_run_version")).toBe(false);
    expect(typeof (row as { last_run_version?: unknown }).last_run_version).not.toBe("function");
  });

  it("no transport injected: label refs still degrade honestly (unchanged contract)", async () => {
    const status = await buildWorkflowPromptStatus(langfuseSpec(), "wf", "local");
    const lab = status.prompts.find((p) => p.name === "lab")!;
    expect(lab.status).toBe("unknown");
    expect(lab.detail).toMatch(/^registry lookup failed: /);
  });
});
