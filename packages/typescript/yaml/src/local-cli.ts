#!/usr/bin/env node
/** Foreground, non-durable YAML execution. No Temporal worker or connection. */
import { readFileSync, realpathSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";
import { buildLocalRuntime, LocalWorkflowInputError, type LocalRuntimeOptions } from "./local.js";
import { validateLocalCapabilities } from "./engine.js";
import { importBindingsModule } from "./bindings-module.js";
import { resolveRuntimeTarget, type RuntimeTargetArgs } from "./runtime-target.js";
import { enforcePolicyPreflight } from "./runtime-assembly.js";
import { autoWireRuntimeOptions } from "./runtime-wiring.js";

const HELP = `Usage: typeflux-local <workflow.yaml> --input <file.json|-> [--bindings module.js]
       typeflux-local --project <manifest.yaml> --workflow W --environment E
         --input <file.json|-> [--bindings module.js] [--policy P]...
         [--expect-policy-hash SHA256]

Executes once in the foreground. runtime.engine must be local.
Bindings use named exports or a default object (TYPEFLUX_WORKER_BINDINGS fallback).
Exit: 0 success, 1 execution/I/O, 2 usage/input, 3 configuration/policy;
      SIGINT 130, SIGTERM 143 after cooperative cancellation and observation drain.
`;

class UsageError extends Error {}

function normalizedHash(value: string | undefined): string | undefined {
  const hash = value?.trim().toLowerCase();
  if (!hash) return undefined;
  if (!/^[0-9a-f]{64}$/.test(hash)) throw new UsageError("expected project policy hash must be a 64-character sha256 hex digest");
  return hash;
}

function expectedHash(cli: string | undefined): string | undefined {
  const arg = normalizedHash(cli);
  const env = normalizedHash(process.env["TYPEFLUX_EXPECTED_POLICY_HASH"]);
  if (arg && env && arg !== env) throw new UsageError("conflicting expected project policy hashes from --expect-policy-hash and TYPEFLUX_EXPECTED_POLICY_HASH");
  return arg ?? env;
}

/** Runs a dedicated foreground command. Serial use only: stdout and environment are scoped. */
export async function main(argv: readonly string[]): Promise<number> {
  let args: ReturnType<typeof argumentsFor>;
  try { args = argumentsFor(argv); }
  catch (error) { console.error(message(error)); return 2; }
  if (args.values.help) { process.stdout.write(HELP); return 0; }
  let pin: string | undefined;
  let input: unknown;
  try {
    pin = expectedHash(args.values["expect-policy-hash"]);
    input = JSON.parse(readFileSync(args.values.input === "-" ? 0 : args.values.input!, "utf8"));
  } catch (error) { console.error(message(error)); return 2; }

  const savedEnvironment = {...process.env};
  const write = process.stdout.write;
  // Extension modules and SDKs may log or write directly. Keep the result channel JSON-only,
  // including during imports, service construction, cancellation and observation shutdown.
  process.stdout.write = process.stderr.write.bind(process.stderr);
  const controller = new AbortController();
  let signalStatus = 0;
  const interrupt = () => { signalStatus ||= 130; controller.abort(new Error("SIGINT")); };
  const terminate = () => { signalStatus ||= 143; controller.abort(new Error("SIGTERM")); };
  process.on("SIGINT", interrupt);
  process.on("SIGTERM", terminate);
  let status = 3;
  let result: string | undefined;
  let wired: Awaited<ReturnType<typeof autoWireRuntimeOptions>> | undefined;
  let injectedObserver: LocalRuntimeOptions["observer"];
  try {
    const targetArgs: RuntimeTargetArgs = {
      policies: args.values.policy ?? [],
      ...(args.values.project === undefined ? {yamlPath: args.positionals[0]!} : {manifest: args.values.project}),
      ...(args.values.workflow === undefined ? {} : {workflow: args.values.workflow}),
      ...(args.values.environment === undefined ? {} : {environment: args.values.environment}),
      ...(pin === undefined ? {} : {expectPolicyHash: pin}),
    };
    const target = resolveRuntimeTarget(targetArgs);
    // Capture the caller's pin before project variables are applied. An environment cannot
    // replace/clear it, and a pin introduced by that environment must also be enforced.
    const effectivePin = expectedHash(args.values["expect-policy-hash"]);
    if (pin !== undefined && effectivePin !== pin) throw new Error("project environment changed the caller's expected policy hash");
    const expected = pin ?? effectivePin;
    if (expected !== undefined && target.policy?.policyHash !== expected) throw new Error("selected project policy hash does not match expected deployment policy hash (or no project policy was selected)");
    if (args.values.project === undefined && (args.values.policy?.length || expected !== undefined)) throw new Error("project policy selection requires --project");
    validateLocalCapabilities(target.spec, target.subworkflows);
    if (target.policy !== undefined) enforcePolicyPreflight(target.spec, target.policy, target.subworkflows);
    // Bindings are executable application code; static graph/policy selection precedes import.
    const bindingsPath = args.values.bindings ?? process.env["TYPEFLUX_WORKER_BINDINGS"];
    const bindings = bindingsPath ? await importBindingsModule(bindingsPath) : {};
    const services = Object.fromEntries([
      "provider", "transports", "registry", "registryTransport", "observer", "hooks",
      "outputChecks", "moderators", "extraActivities", "cacheStore", "deps", "tenant",
    ].filter(key => Object.hasOwn(bindings, key)).map(key => [key, bindings[key]]));
    const options: LocalRuntimeOptions = {
      ...services,
      schemas: (bindings["schemas"] as LocalRuntimeOptions["schemas"] | undefined) ?? {},
      ...(target.policy === undefined ? {} : {policy: target.policy}),
      ...(target.subworkflows === undefined ? {} : {subworkflows: target.subworkflows}),
    };
    injectedObserver = options.observer;
    if (controller.signal.aborted) throw controller.signal.reason;
    wired = await autoWireRuntimeOptions(target.spec, options);
    if (controller.signal.aborted) throw controller.signal.reason;
    const runtime = await buildLocalRuntime(target.spec, wired.options);
    status = 1;
    const output = await runtime.executeWorkflow(input, {signal: controller.signal});
    result = JSON.stringify(output);
    if (result === undefined) throw new Error("workflow result is not JSON serializable");
    status = 0;
  } catch (error) {
    if (error instanceof LocalWorkflowInputError) status = 2;
    console.error(message(error));
  }
  finally {
    try {
      if (wired?.autoObserver !== undefined) await wired.autoObserver.shutdown();
      else await injectedObserver?.flush();
    } catch (error) { console.error("observability drain failed:", message(error)); status = 1; }
    process.off("SIGINT", interrupt);
    process.off("SIGTERM", terminate);
    for (const key of Object.keys(process.env)) if (!Object.hasOwn(savedEnvironment, key)) delete process.env[key];
    Object.assign(process.env, savedEnvironment);
    process.stdout.write = write;
  }
  if (signalStatus) return signalStatus;
  if (status === 0) process.stdout.write(`${result}\n`);
  return status;
}

function message(error: unknown): string { return error instanceof Error ? error.message : String(error); }

function argumentsFor(argv: readonly string[]) {
  const args = parseArgs({args: [...argv], allowPositionals: true, options: {
    project: {type: "string"}, workflow: {type: "string"}, environment: {type: "string"},
    input: {type: "string"}, bindings: {type: "string"}, policy: {type: "string", multiple: true},
    "expect-policy-hash": {type: "string"}, help: {type: "boolean", short: "h"},
  }});
  if (args.values.help) return args;
  if (!args.values.input) throw new UsageError("--input <file.json|-> is required");
  if (args.values.project !== undefined) {
    if (args.positionals.length || !args.values.workflow || !args.values.environment) throw new UsageError("--project requires --workflow and --environment, without a YAML positional");
  } else if (args.positionals.length !== 1 || args.values.workflow || args.values.environment) {
    throw new UsageError("provide one YAML file, or --project with --workflow and --environment");
  }
  return args;
}

if (process.argv[1] !== undefined && realpathSync(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.exitCode = await main(process.argv.slice(2));
}
