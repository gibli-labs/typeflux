import { spawn, spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterAll, describe, expect, it } from "vitest";
import { stringify } from "yaml";
import { composeProjectPolicies } from "../src/policy-composition.js";
import { loadPolicySpec } from "../src/policy.js";

const testDir = dirname(fileURLToPath(import.meta.url));
const cli = join(testDir, "../dist/local-cli.js");
const base = JSON.parse(readFileSync(new URL("../../../../contracts/local-runtime/golden/execution-cases.json", import.meta.url), "utf8")).base;
const directories: string[] = [];
afterAll(() => { for (const directory of directories) rmSync(directory, {recursive: true, force: true}); });
const policyText = "name: strict\nproviders: {allowed: {fake: {models: [offline]}}}\n";
const policyHash = composeProjectPolicies([{id: "strict", spec: loadPolicySpec(policyText)}], ["strict"]).policyHash;
const bindings = `
import { z } from 'zod';
import { NoOpObserver } from '@typeflux/temporal';
import { appendFileSync } from 'node:fs';
const event = text => { if (process.env.EVENTS) appendFileSync(process.env.EVENTS, text+'\\n'); };
event('bindings');
console.log('bindings diagnostic');
export const schemas = {'models:Item': z.object({n: z.number().int()})};
export const observer = new class extends NoOpObserver { async flush() { event('flush'); console.log('flush diagnostic'); } };
export const provider = {providerName: 'fake', async structuredCall(params) {
  event('provider'); console.log('provider diagnostic');
  if (process.env.FAIL_EXECUTION) throw new Error('execution failed');
  const n = Number(params.messages.at(-1).content);
  if (process.env.LOSS_TEST) event('input:'+n);
  if (process.env.WAIT_FOR_SIGNAL || (process.env.WAIT_AFTER_FIRST && n === 2)) {
    event('ready');
    try { await new Promise((resolve, reject) => {
      const timer = setInterval(() => {}, 1000);
      const abort = () => { clearInterval(timer); setTimeout(() => { event('cancelled'); reject(new Error('aborted')); }, 30); };
      if (params.signal?.aborted) abort(); else params.signal?.addEventListener('abort', abort, {once:true});
    }); } finally { event('settled'); }
  }
  if (process.env.LOSS_TEST) event('effect:'+n);
  return {n: n + Number(process.env.INCREMENT || 1)};
}};
`;

function fixture() {
  const dir = mkdtempSync(join(testDir, "tmp-local-cli-")); directories.push(dir);
  const source = structuredClone(base); source.runtime.provider.model = "offline";
  writeFileSync(join(dir, "workflow.yaml"), stringify(source));
  writeFileSync(join(dir, "bindings.mjs"), bindings);
  writeFileSync(join(dir, "input.json"), '{"n":1}');
  writeFileSync(join(dir, "policy.yaml"), policyText);
  writeFileSync(join(dir, "profile.yaml"), "name: offline\nkind: provider\nruntime: {provider: {model: '${MODEL}'}}\n");
  writeFileSync(join(dir, "environment.yaml"), "name: offline\nvariables: {INCREMENT: 2, MODEL: offline}\n");
  writeFileSync(join(dir, "typeflux.project.yaml"), `name: local\nworkflows: [{id: root, path: workflow.yaml, profiles: {provider: offline}}, {id: child, path: child.yaml}]\nenvironments: {offline: environment.yaml}\nprofiles: {provider: {offline: profile.yaml}}\npolicies: {strict: policy.yaml}\nvalidation: {targets: {local: {workflows: [root], environment: offline, policies: [strict]}}}\n`);
  writeFileSync(join(dir, "child.yaml"), stringify({...source, name: "child", workflow: {...source.workflow, name: "Child"}}));
  writeFileSync(join(dir, "guard.mjs"), `
import { registerHooks } from 'node:module';
import net from 'node:net';
registerHooks({resolve(specifier, context, next) {
  if (specifier.startsWith('@temporalio/')) throw new Error('Temporal import forbidden: '+specifier);
  return next(specifier, context);
}});
net.Socket.prototype.connect = function() { throw new Error('Network access forbidden'); };
`);
  return {dir, source};
}

const projectArgs = ["--project", "typeflux.project.yaml", "--workflow", "root", "--environment", "offline"];
function run(dir: string, target = ["workflow.yaml"], options: {input?: string; env?: Record<string, string>} = {}) {
  const env = {...process.env, TYPEFLUX_EXPECTED_POLICY_HASH: "", ...options.env};
  return spawnSync(process.execPath, ["--import", join(dir, "guard.mjs"), cli, ...target, "--bindings", "bindings.mjs", "--input", options.input === undefined ? "input.json" : "-"],
    {cwd: dir, env, input: options.input, encoding: "utf8", timeout: 15000});
}

describe("local foreground CLI", () => {
  it("executes file and stdin without Temporal or network and isolates extension stdout", () => {
    const {dir} = fixture();
    for (const options of [{}, {input: '{"n":4}'}]) {
      const result = run(dir, ["workflow.yaml"], options);
      expect(result.status, result.stderr).toBe(0);
      expect(JSON.parse(result.stdout)).toEqual({n: "input" in options ? 5 : 2});
      expect(result.stderr).toContain("bindings diagnostic");
      expect(result.stderr).toContain("provider diagnostic");
      expect(result.stderr).toContain("flush diagnostic");
    }
  });

  it("resolves profiles, environment, parent policy and nested project execution", () => {
    const {dir, source} = fixture();
    source.workflow.steps = [{id: "nested", workflow: "child"}];
    writeFileSync(join(dir, "workflow.yaml"), stringify(source));
    for (const extra of [[], ["--policy", "strict", "--expect-policy-hash", ` ${policyHash.toUpperCase()} `]]) {
      const result = run(dir, [...projectArgs, ...extra]);
      expect(result.status, result.stderr).toBe(0);
      expect(JSON.parse(result.stdout)).toEqual({n: 3});
    }
  });

  function inputRefusal(kind: string) {
    const {dir} = fixture();
    const result = run(dir, undefined, {input: kind === "malformed" ? "{" : '{"n":"bad"}'});
    expect(result.status).toBe(2); expect(result.stdout).toBe("");
    expect(result.stderr).not.toContain("provider diagnostic");
  }
  it("classifies malformed input and never calls the provider", () => inputRefusal("malformed"));
  it("classifies schema input and never calls the provider", () => inputRefusal("schema"));


  function configurationRefusal(kind: string) {
    const {dir, source} = fixture();
    let target = [...projectArgs]; let env: Record<string, string> = {};
    if (kind === "temporal") { source.runtime.engine = "temporal"; source.task_queue = "q"; target = ["workflow.yaml"]; }
    if (kind.startsWith("skipped")) {
      source.workflow.steps = [{id: "nested", workflow: "child", when: {path: "input.n", eq: -1}}];
      const child = structuredClone(source); child.name = "child"; child.workflow = {...base.workflow, name: "Child"};
      if (kind === "skipped capability") child.workflow.risk_tier = "human_gated";
      else child.runtime.provider.model = "forbidden";
      writeFileSync(join(dir, "child.yaml"), stringify(child));
    }
    if (kind === "hash mismatch" || kind === "standalone pin") {
      env.TYPEFLUX_EXPECTED_POLICY_HASH = "0".repeat(64);
      if (kind === "standalone pin") target = ["workflow.yaml"];
    }
    if (kind === "hash replacement" || kind === "hash clearing") {
      env.TYPEFLUX_EXPECTED_POLICY_HASH = "0".repeat(64);
      writeFileSync(join(dir, "environment.yaml"), `name: offline\nvariables: {MODEL: offline, TYPEFLUX_EXPECTED_POLICY_HASH: '${kind === "hash clearing" ? "" : policyHash}'}\n`);
    }
    if (kind === "hash conflict") { env.TYPEFLUX_EXPECTED_POLICY_HASH = policyHash; target.push("--expect-policy-hash", "0".repeat(64)); }
    if (kind === "malformed hash") target.push("--expect-policy-hash", "bad");
    writeFileSync(join(dir, "workflow.yaml"), stringify(source));
    const result = run(dir, target, {env});
    expect(result.status, result.stderr).toBe(kind === "hash conflict" || kind === "malformed hash" ? 2 : 3);
    expect(result.stdout).toBe(""); expect(result.stderr).not.toContain("bindings diagnostic");
  }
  it("refuses temporal before bindings/service construction", () => configurationRefusal("temporal"));
  it("refuses skipped capability before bindings/service construction", () => configurationRefusal("skipped capability"));
  it("refuses skipped policy before bindings/service construction", () => configurationRefusal("skipped policy"));
  it("refuses hash mismatch before bindings/service construction", () => configurationRefusal("hash mismatch"));
  it("refuses hash replacement before bindings/service construction", () => configurationRefusal("hash replacement"));
  it("refuses hash clearing before bindings/service construction", () => configurationRefusal("hash clearing"));
  it("refuses standalone pin before bindings/service construction", () => configurationRefusal("standalone pin"));
  it("refuses hash conflict before bindings/service construction", () => configurationRefusal("hash conflict"));
  it("refuses malformed hash before bindings/service construction", () => configurationRefusal("malformed hash"));


  it("attributes execution failures and flushes observations before exit", () => {
    const {dir} = fixture();
    const result = run(dir, undefined, {env: {FAIL_EXECUTION: "1"}});
    expect(result.status).toBe(1); expect(result.stdout).toBe("");
    expect(result.stderr).toMatch(/workflow "Root" step "first" failed/);
    expect(result.stderr).toContain("flush diagnostic");
  });

  it("flushes injected observations when preparation fails", () => {
    const {dir, source} = fixture();
    source.runtime.registry.prompts = {};
    writeFileSync(join(dir, "workflow.yaml"), stringify(source));
    const result = run(dir);
    expect(result.status).toBe(3); expect(result.stdout).toBe("");
    expect(result.stderr).toContain("flush diagnostic"); expect(result.stderr).not.toContain("provider diagnostic");
  });

  it("flushes injected observations when automatic provider construction fails", () => {
    const {dir, source} = fixture();
    source.runtime.provider = {type: "openai"};
    writeFileSync(join(dir, "workflow.yaml"), stringify(source));
    writeFileSync(join(dir, "bindings.mjs"), bindings.replace("export const provider", "const provider"));
    const result = run(dir);
    expect(result.status).toBe(3); expect(result.stdout).toBe("");
    expect(result.stderr).toContain("OPENAI_API_KEY"); expect(result.stderr).toContain("flush diagnostic");
    expect(result.stderr).not.toContain("provider diagnostic");
  });

  it("accepts default bindings and the existing worker bindings environment convention", () => {
    const {dir} = fixture();
    writeFileSync(join(dir, "default.mjs"), "import * as bindings from './bindings.mjs'; export default bindings;\n");
    const result = spawnSync(process.execPath, ["--import", join(dir, "guard.mjs"), cli, "workflow.yaml", "--input", "input.json"],
      {cwd: dir, env: {...process.env, TYPEFLUX_EXPECTED_POLICY_HASH: "", TYPEFLUX_WORKER_BINDINGS: "default.mjs"}, encoding: "utf8"});
    expect(result.status, result.stderr).toBe(0); expect(JSON.parse(result.stdout)).toEqual({n: 2});
  });

  async function signalDrain(signal: "SIGINT" | "SIGTERM") {
    const {dir} = fixture(); const events = join(dir, "events");
    writeFileSync(events, "");
    const child = spawn(process.execPath, ["--import", join(dir, "guard.mjs"), cli, "workflow.yaml", "--bindings", "bindings.mjs", "--input", "input.json"],
      {cwd: dir, env: {...process.env, TYPEFLUX_EXPECTED_POLICY_HASH: "", WAIT_FOR_SIGNAL: "1", EVENTS: events}, stdio: ["ignore", "pipe", "pipe"]});
    let stdout = "", stderr = "";
    child.stdout.on("data", data => { stdout += data; }); child.stderr.on("data", data => { stderr += data; });
    const completion = new Promise<number | null>((resolve, reject) => { child.once("error", reject); child.once("close", resolve); });
    try {
      await expect.poll(() => readFileSync(events, "utf8"), {timeout: 7000}).toContain("ready");
      child.kill(signal);
      expect(await completion, stderr).toBe(signal === "SIGINT" ? 130 : 143);
      expect(stdout).toBe("");
      expect(readFileSync(events, "utf8").trim().split("\n")).toEqual(["bindings", "provider", "ready", "cancelled", "settled", "flush"]);
    } finally { if (child.exitCode === null) child.kill("SIGKILL"); }
  }
  it("drains cooperative SIGINT cancellation before observations", () => signalDrain("SIGINT"), 10000);
  it("drains cooperative SIGTERM cancellation before observations", () => signalDrain("SIGTERM"), 10000);

});


describe.skipIf(process.platform === "win32")("local CLI process loss", () => {
  it("keeps completed effects after SIGKILL and repeats from the start on a fresh command", async () => {
    const {dir, source} = fixture();
    source.workflow.steps = ["first", "second", "downstream"].map(id => ({id, activity: "echo"}));
    writeFileSync(join(dir, "workflow.yaml"), stringify(source));
    const events = join(dir, "events");
    writeFileSync(events, "");
    const child = spawn(process.execPath, ["--import", join(dir, "guard.mjs"), cli, "workflow.yaml", "--bindings", "bindings.mjs", "--input", "input.json"],
      {cwd: dir, env: {...process.env, TYPEFLUX_EXPECTED_POLICY_HASH: "", LOSS_TEST: "1", WAIT_AFTER_FIRST: "1", EVENTS: events}, stdio: ["ignore", "pipe", "pipe"]});
    let stdout = "", stderr = "";
    child.stdout.on("data", data => { stdout += data; });
    child.stderr.on("data", data => { stderr += data; });
    const completion = new Promise<{code: number | null; signal: NodeJS.Signals | null}>((resolve, reject) => {
      child.once("error", reject);
      child.once("close", (code, signal) => resolve({code, signal}));
    });
    try {
      await expect.poll(() => readFileSync(events, "utf8"), {timeout: 10000}).toContain("ready");
      expect(child.kill("SIGKILL")).toBe(true);
      expect(await completion, stderr).toEqual({code: null, signal: "SIGKILL"});
      expect(stdout).toBe("");
      const beforeRestart = ["bindings", "provider", "input:1", "effect:1", "provider", "input:2", "ready"];
      expect(readFileSync(events, "utf8").trim().split("\n")).toEqual(beforeRestart);
      const fresh = run(dir, ["workflow.yaml"], {env: {LOSS_TEST: "1", EVENTS: events}});
      expect(fresh.status, fresh.stderr).toBe(0);
      expect(JSON.parse(fresh.stdout)).toEqual({n: 4});
      expect(readFileSync(events, "utf8").trim().split("\n")).toEqual([...beforeRestart,
        "bindings", "provider", "input:1", "effect:1", "provider", "input:2", "effect:2", "provider", "input:3", "effect:3", "flush"]);
    } finally {
      if (child.exitCode === null && child.signalCode === null) child.kill("SIGKILL");
      await completion;
    }
  }, 20000);
});
