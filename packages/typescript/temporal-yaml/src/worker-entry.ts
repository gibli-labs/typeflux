#!/usr/bin/env node
/**
 * `typeflux-yaml-worker` — the reference worker entrypoint the rendered Kubernetes Deployment
 * invokes (deployment tier, #687 slice 2; the TS analog of Python's `python -m
 * typeflux.project run`). It composes {@link assembleYamlRuntime} + `createTypefluxWorker`
 * (via {@link buildRuntime}) for a resolved YAML spec and polls its task queue.
 *
 *   typeflux-yaml-worker [<manifest>] [--workflow W] [--environment E] [--policy P]...
 *       [--expect-policy-hash H] [--preflight]
 *
 * TWO MODES:
 *   - PROJECT mode (a `<manifest>` positional, the rendered container command): resolve the named
 *     workflow under its environment, compose + hash-verify its policy, and serve it.
 *   - SINGLE-SPEC mode (no manifest, `TYPEFLUX_YAML_PATH`, the reference image's default CMD): load
 *     one spec file directly — mirrors the Python reference image's `yaml.run`.
 *
 * `--preflight` assembles the runtime (governance + activity/plan build, NO server) and exits — the
 * rendered command runs it, writes the `/tmp/typeflux-preflight-ok` marker, then re-execs WITHOUT
 * it to poll. Preflight and the live worker compose ONE options assembly
 * ({@link autoWireRuntimeOptions} — the OOTB env-keyed providers/registries/observability included),
 * so a preflight pass means the live build gets exactly the same wiring (#687 review).
 *
 * CONNECTION (fail-closed, #687 review): the live dial resolves `runtime.temporal` through
 * {@link temporalConnectionOptions} — the #685 structured-TLS machinery plus the api-key secret
 * reference — so a Temporal Cloud/mTLS deployment connects with the TLS + credential its manifest
 * declares, and a missing required secret is a loud config error, never a silent plaintext downgrade.
 *
 * CODE BINDINGS (the TS↔Python divergence): Python resolves a spec's `schemas:`/activity refs via
 * importlib at runtime; TS needs COMPILED code. This entrypoint dynamic-imports a bindings module
 * (`TYPEFLUX_WORKER_BINDINGS`) that exports `{ schemas, provider?|transports?, hooks?, outputChecks?, moderators?,
 * extraActivities? }`. The module is OPTIONAL: providers/registries/observability auto-wire from the
 * spec + environment, so bindings are only required for what MUST be code — the activity schema
 * resolvers (and hooks/moderators/extra activities). The reference image points it at a bundled
 * example; a production image forks it to its project's bindings.
 */

import { readFileSync, realpathSync } from "node:fs";
import { fileURLToPath } from "node:url";

import {
  buildSubjectAwarePayloadCodec,
  SubjectScopedPayloadCodec,
  type SubjectBindingClient,
} from "./subject-keystore.js";
import { assembleYamlRuntime, autoWireRuntimeOptions, buildRuntime } from "./runtime.js";
import { importBindingsModule } from "./bindings-module.js";
import { applyEnvironmentVariablesToProcessEnv } from "./environment-overlay.js";
import { loadYamlSpec } from "./loader.js";
import { loadProjectBundle } from "./project-fs-loader.js";
import { resolveProjectWorkflow } from "./project-run.js";
import { projectSubworkflowResolver, type SubworkflowSpecResolver } from "./build-workflow.js";
import { composeProjectPolicyIds } from "./project-resolve.js";
import {
  assertRiskTierEnforced, selectProjectPolicyIdsForWorkflow } from "./project-enforcement.js";
import { temporalConnectionOptions } from "./temporal-tls.js";
import type { ComposedProjectPolicy } from "./policy-composition.js";
import type { BuildRuntimeOptions } from "./runtime.js";
import type { TypefluxYamlSpec } from "./spec.js";

interface WorkerArgs {
  manifest?: string;
  workflow?: string;
  environment?: string;
  policies: string[];
  expectPolicyHash?: string;
  preflight: boolean;
}

/** The code-defined pieces a bindings module supplies (dynamic-import, loosely typed). */
interface WorkerBindings {
  schemas?: BuildRuntimeOptions["schemas"];
  provider?: BuildRuntimeOptions["provider"];
  transports?: BuildRuntimeOptions["transports"];
  hooks?: BuildRuntimeOptions["hooks"];
  outputChecks?: BuildRuntimeOptions["outputChecks"];
  moderators?: BuildRuntimeOptions["moderators"];
  extraActivities?: BuildRuntimeOptions["extraActivities"];
  /**
   * A cross-run activity output cache store (#398/#753): a code-defined piece (e.g.
   * `new InMemoryCacheStore()` or a durable store), so a deployed YAML worker that declares
   * `cross_run_cache:` on an activity actually memoizes. Absent ⇒ cross-run caching stays inert.
   */
  cacheStore?: BuildRuntimeOptions["cacheStore"];
  /**
   * The SHARED crypto-shred keystore backend (#715 slice 4), REQUIRED when the spec
   * declares `runtime.temporal.payload_codec.subject_scope`: this entrypoint is by
   * design a split-process worker (something else starts the workflows), so a
   * process-local in-memory keystore could never hold the records the starter minted —
   * the worker refuses to start without an injected shared backend (fail-closed).
   */
  subjectKeystore?: BuildRuntimeOptions["subjectKeystore"];
}

function parseArgs(argv: readonly string[]): WorkerArgs {
  const args: WorkerArgs = { policies: [], preflight: false };
  const take = (flag: string, i: number): string => {
    const value = argv[i + 1];
    if (value === undefined) throw new Error(`${flag} requires a value`);
    return value;
  };
  for (let i = 0; i < argv.length; i += 1) {
    const token = argv[i]!;
    switch (token) {
      case "--workflow":
        args.workflow = take(token, i);
        i += 1;
        break;
      case "--environment":
        args.environment = take(token, i);
        i += 1;
        break;
      case "--policy":
        args.policies.push(take(token, i));
        i += 1;
        break;
      case "--expect-policy-hash":
        args.expectPolicyHash = take(token, i);
        i += 1;
        break;
      case "--preflight":
        args.preflight = true;
        break;
      default:
        if (token.startsWith("-")) throw new Error(`unknown flag: ${token}`);
        if (args.manifest !== undefined) throw new Error(`unexpected argument: ${token}`);
        args.manifest = token;
    }
  }
  return args;
}

/**
 * Dynamic-import the OPTIONAL code bindings module. Absent (`TYPEFLUX_WORKER_BINDINGS` unset),
 * the worker runs on the OOTB auto-wiring alone with no schema resolvers — every activity
 * schema ref then fails at assembly with the precise "provide it in resolver.schemas" error,
 * never silently.
 */
async function loadBindings(): Promise<WorkerBindings> {
  const modulePath = process.env["TYPEFLUX_WORKER_BINDINGS"];
  if (modulePath === undefined || modulePath === "") {
    return {};
  }
  // The SHARED loader (named-or-default unwrap) — the erase CLI reads the same
  // module through the same helper, so the two can never disagree on shape.
  return (await importBindingsModule(modulePath)) as WorkerBindings;
}

/** Resolve the target spec (+ optional composed policy and sub-workflow resolver) for either mode. */
function resolveTarget(args: WorkerArgs): {
  spec: TypefluxYamlSpec;
  policy?: ComposedProjectPolicy;
  subworkflows?: SubworkflowSpecResolver;
} {
  if (args.manifest === undefined) {
    const specPath = process.env["TYPEFLUX_YAML_PATH"];
    if (specPath === undefined || specPath === "") {
      throw new Error("no project manifest argument and no TYPEFLUX_YAML_PATH set; nothing to run");
    }
    const spec = loadYamlSpec(readFileSync(specPath, "utf-8"), { sourceLabel: specPath });
    return { spec };
  }
  if (args.workflow === undefined || args.environment === undefined) {
    throw new Error("project mode requires --workflow and --environment");
  }
  const { project, sources } = loadProjectBundle(args.manifest);
  // The target environment's variable overlay applies to the WHOLE worker lifetime
  // (Bugbot #706): deploy admission and Python's project `run` evaluate policy —
  // including secret/env-backed checks — under this overlay, and the rendered pod
  // gets the same values via its ConfigMap. A dedicated worker process adopts them
  // persistently (overlay wins, matching withEnvironmentContext precedence) so the
  // policy hash it verifies and the runtime it assembles agree with the plan it
  // was deployed with.
  const environmentSpec = Object.hasOwn(sources.environments, args.environment)
    ? sources.environments[args.environment]
    : undefined;
  if (environmentSpec !== undefined) {
    // Shared applier (the restore function is deliberately dropped: the worker
    // adopts the overlay for its whole lifetime, per the comment above).
    applyEnvironmentVariablesToProcessEnv(environmentSpec.variables);
  }
  const resolved = resolveProjectWorkflow(project, sources, args.workflow, args.environment);
  if (resolved === undefined) {
    throw new Error(`cannot resolve workflow '${args.workflow}' under environment '${args.environment}'`);
  }
  const selected = selectProjectPolicyIdsForWorkflow(project, {
    environmentId: args.environment,
    workflowId: args.workflow,
    explicitPolicyIds: args.policies,
  });
  const policy = selected.length > 0 ? composeProjectPolicyIds(project, sources, selected) : undefined;
  if (args.expectPolicyHash !== undefined) {
    if (policy === undefined) {
      throw new Error("expected project policy hash was provided, but no project policy was selected");
    }
    if (policy.policyHash !== args.expectPolicyHash) {
      throw new Error(
        `selected project policy hash does not match expected deployment policy hash ` +
          `(expected=${args.expectPolicyHash}, actual=${policy.policyHash})`,
      );
    }
  }
  const subworkflows = projectSubworkflowResolver(
    args.workflow,
    (siblingId) => resolveProjectWorkflow(project, sources, siblingId, args.environment!)?.spec,
  );
  // #788 (review M3): the worker's project mode composes its own policy object and
  // previously bypassed the risk-tier binding check entirely — an elevated declared
  // tier (parent or closure) must fail closed here exactly like the Python worker.
  assertRiskTierEnforced(
    resolved.spec,
    selected,
    policy?.payload,
    (siblingId) => resolveProjectWorkflow(project, sources, siblingId, args.environment!)?.spec,
    args.workflow,
  );
  return policy !== undefined ? { spec: resolved.spec, policy, subworkflows } : { spec: resolved.spec, subworkflows };
}

function runtimeOptions(bindings: WorkerBindings, target: ReturnType<typeof resolveTarget>): BuildRuntimeOptions {
  const options: BuildRuntimeOptions = { schemas: bindings.schemas ?? {} };
  if (bindings.provider !== undefined) options.provider = bindings.provider;
  if (bindings.transports !== undefined) options.transports = bindings.transports;
  if (bindings.hooks !== undefined) options.hooks = bindings.hooks;
  if (bindings.outputChecks !== undefined) options.outputChecks = bindings.outputChecks;
  if (bindings.moderators !== undefined) options.moderators = bindings.moderators;
  if (bindings.extraActivities !== undefined) options.extraActivities = bindings.extraActivities;
  if (bindings.cacheStore !== undefined) options.cacheStore = bindings.cacheStore;
  if (bindings.subjectKeystore !== undefined) options.subjectKeystore = bindings.subjectKeystore;
  if (target.policy !== undefined) options.policy = target.policy;
  if (target.subworkflows !== undefined) options.subworkflows = target.subworkflows;
  return options;
}

/** Run the entrypoint over `argv` (everything after the program name). Returns an exit code. */
export async function main(argv: readonly string[]): Promise<number> {
  const args = parseArgs(argv);
  const bindings = await loadBindings();
  const target = resolveTarget(args);
  const options = runtimeOptions(bindings, target);

  // #715 slice 4 (Bugbot): this entrypoint is BY DESIGN a split-process worker —
  // something else starts the workflows — so buildRuntime's sole-owner in-memory
  // keystore default can never hold the records the starter minted. subject_scope
  // therefore REQUIRES an injected shared backend, checked up front for BOTH
  // preflight and live (preflight must certify the exact wiring the worker polls
  // with).
  if (
    target.spec.runtime.temporal.payload_codec?.subject_scope !== undefined &&
    options.subjectKeystore === undefined
  ) {
    throw new Error(
      "runtime.temporal.payload_codec.subject_scope requires a SHARED SubjectKeystore " +
        "backend for a deployed worker (#715 slice 4): a process-local in-memory keystore " +
        "cannot hold the key records the starter minted, so subject payloads would be " +
        "undecodable. Export TYPEFLUX_WORKER_BINDINGS with a module providing " +
        "`subjectKeystore` (see docs/typescript/privacy.md 'Keystore backends').",
    );
  }

  if (args.preflight) {
    // Pure assembly (no server), through the SAME options assembly the live build runs
    // (autoWireRuntimeOptions: governance-first + the OOTB env-keyed provider/registry/observer
    // wiring) — a preflight pass must certify the exact wiring the worker will poll with, and a
    // missing OOTB credential must fail HERE, before the readiness marker. Success ⇒ the rendered
    // command writes the marker; a throw fails the container's preflight before it polls.
    const { options: wired } = await autoWireRuntimeOptions(target.spec, options);
    assembleYamlRuntime(target.spec, wired);
    // Fail-closed codec validation (#188): a declared payload_codec whose key can't resolve
    // (or is the wrong length) must fail HERE, before the readiness marker — the live build
    // (buildRuntime) constructs the same codec (incl. the #715 subject_scope wrapping with
    // the bindings-supplied keystore, required above), so this certifies the wiring the
    // worker polls with. Resolves keys only; never logs the value.
    buildSubjectAwarePayloadCodec(target.spec.runtime.temporal.payload_codec, options.subjectKeystore);
    process.stdout.write("typeflux worker preflight ok\n");
    return 0;
  }

  // The live dial: `runtime.temporal` resolved fail-closed (#685 TLS + api-key secret refs; env
  // interpolation already stamped the pod's TEMPORAL_* ConfigMap values into the loaded spec).
  // `@temporalio/worker` (a declared dependency) is imported lazily so `--preflight` stays
  // native-module-free.
  const connectionOptions = temporalConnectionOptions(target.spec.runtime.temporal);
  const { NativeConnection } = (await import("@temporalio/worker")) as typeof import("@temporalio/worker");
  const connection = await NativeConnection.connect({
    address: connectionOptions.address,
    // Boolean or the resolved structured options — `NativeConnection.connect` takes both (#685).
    tls: connectionOptions.tls,
    ...(connectionOptions.apiKey !== undefined ? { apiKey: connectionOptions.apiKey } : {}),
  });
  let subjectScopeConnection: { close(): Promise<void> } | undefined;
  try {
    const runtime = await buildRuntime(target.spec, {
      ...options,
      worker: { connection, namespace: connectionOptions.namespace },
    });
    // #715 slice 4: a subject-scoped codec on the WORKER must resolve the subjects of
    // executions this process did not start. Bind a visibility client (built with the
    // SAME data converter) as the bindings' fallback — it describes the execution and
    // reads its TypefluxSubjectIds. `@temporalio/client` resolves via
    // `@temporalio/worker`'s own dependency tree, so the lazy import is deployment-safe.
    const workerCodec = runtime.dataConverter?.payloadCodecs?.[0];
    if (workerCodec instanceof SubjectScopedPayloadCodec) {
      const { Client, Connection } = (await import(
        "@temporalio/client"
      )) as typeof import("@temporalio/client");
      const clientConnection = await Connection.connect({
        address: connectionOptions.address,
        tls: connectionOptions.tls,
        ...(connectionOptions.apiKey !== undefined ? { apiKey: connectionOptions.apiKey } : {}),
      });
      subjectScopeConnection = clientConnection;
      const visibilityClient = new Client({
        connection: clientConnection,
        namespace: connectionOptions.namespace,
        ...(runtime.dataConverter !== undefined ? { dataConverter: runtime.dataConverter } : {}),
      });
      workerCodec.bindings.bindClient(visibilityClient as unknown as SubjectBindingClient);
    }
    process.stdout.write(
      `typeflux worker polling task queue '${runtime.taskQueue}' at ${connectionOptions.address}\n`,
    );
    try {
      await runtime.worker.run();
    } finally {
      await runtime.drainObservability();
    }
    return 0;
  } finally {
    await subjectScopeConnection?.close();
    await connection.close();
  }
}

/**
 * Whether this module is the process entrypoint (#757 item 4). Realpath both `process.argv[1]`
 * (the npm `.bin` shim is a SYMLINK) and this module's own path so the symlinked bin invocation is
 * detected — a naive `import.meta.url === file://${argv[1]}` never matches through the shim and the
 * worker would silently no-op with exit 0.
 */
function runningAsMain(): boolean {
  const entry = process.argv[1];
  if (entry === undefined) return false;
  try {
    return realpathSync(entry) === realpathSync(fileURLToPath(import.meta.url));
  } catch {
    return false;
  }
}

// Executed directly as the `typeflux-yaml-worker` bin (not when imported by a test).
if (runningAsMain()) {
  main(process.argv.slice(2))
    .then((code) => {
      process.exitCode = code;
    })
    .catch((error) => {
      process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
      process.exitCode = 1;
    });
}
