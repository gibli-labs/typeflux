#!/usr/bin/env node
/**
 * `typeflux-project` — the deployment-tier CLI (deployment tier, #687; Python
 * `project/__main__.py` `deploy` handler) plus the `erase` verb (#715 slice 5;
 * Python `project/__main__.py` `erase` handler):
 *
 *   typeflux-project erase <manifest> --workflow W --environment E --subject S...
 *       [--surface temporal,langfuse,cache] [--since ISO] [--until ISO]
 *       [--dry-run|--execute] [--acknowledge-irreversible] [--actor A]
 *       [--bindings MODULE] [--limit N] [--json]
 *
 * `erase` fans subject deletion across the surfaces Typeflux controls (Temporal
 * crypto-shred + execution deletion, Langfuse traces, the cross-run cache) and emits
 * the `ErasureReceipt` audit record. DRY-RUN BY DEFAULT; `--execute` mutates, and the
 * temporal surface additionally requires `--acknowledge-irreversible` (destroyed key
 * records and deleted histories are unrecoverable). `--bindings` names a module
 * exporting `{ subjectKeystore?, cacheStore? }` (the `TYPEFLUX_WORKER_BINDINGS`
 * convention; the env var is the fallback) — the process-local reference backends
 * cannot hold another process's records, so an un-injected surface is honestly
 * reported skipped. The deploy verb:
 *
 *   typeflux-project deploy <manifest> --environment E [--workflow W]... [--policy P]...
 *       --image I [--allow-mutable-image] [--allow-shared-task-queue]
 *       [--project-path-in-image P] [--config-env N]... [--plan-out DIR] [--plan FILE]
 *       [--output DIR] [--json]
 *
 * `--plan-out` writes immutable, content-hashed plan files under a project directory (approval
 * is the PR review that merges them). `--apply` applies (promotes) an approved plan: it verifies it against the current
 * resolution and fails closed (exit 1 + named mismatches) on drift — the plan file, not the
 * flags, is then authoritative. `--output` renders the Kubernetes artifacts (ConfigMap +
 * Deployment + Secret scaffold + secrets.env.example) for the plan. `--project-path-in-image`
 * overrides the in-image project manifest path the rendered worker command targets.
 *
 * PORTABLE ARTIFACTS (#757 item 1): by default the recorded `project_manifest_path` (surfaced in
 * render annotations + deployment-plan.json) is normalized RELATIVE to the manifest's own
 * directory, so committed artifacts are machine-independent. `--project-manifest-path` records an
 * explicit (repo-relative) value; `--absolute-manifest-path` restores the operator's resolved
 * absolute path. This path is provenance only — never part of the plan hash or drift detection.
 */

import { realpathSync } from "node:fs";
import { userInfo } from "node:os";
import { basename, dirname, isAbsolute, resolve as resolvePath } from "node:path";
import { fileURLToPath } from "node:url";

import {
  buildProjectDeploymentPlan,
  ProjectDeploymentError,
  type DeploymentWorkflowResolver,
  type ProjectDeploymentPlan,
} from "./deployment.js";
import {
  verifyPlanMergedToDefaultBranch, loadDeploymentPlan, PLACEHOLDER_IMAGE_MISMATCH_PATH, verifyDeploymentPlan, writeDeploymentPlan } from "./deployment-plans.js";
import { renderProjectDeploymentPlan, type ProjectDeploymentRenderResult } from "./deployment-render.js";
import {
  ERASURE_SURFACES,
  eraseSubject,
  erasureFailed,
  type EraseSubjectDeps,
  type ErasureReceipt,
} from "./erase.js";
import { importBindingsModule } from "./bindings-module.js";
import { applyEnvironmentVariablesToProcessEnv } from "./environment-overlay.js";
import { langfuseCredentialsFromSpec } from "./langfuse-observer.js";
import { planResolverFor, resolveProjectWorkflow } from "./project-run.js";
import { loadProjectBundle, type LoadedProjectBundle } from "./project-fs-loader.js";

/** Exit-code contract (#818), shared with the Python project CLI: 0 = success; 1 =
 * operational failure; 2 = usage; 3 = validation/drift verdict. The TS CLI currently
 * emits 3 for the plan-drift verdict; invalid-manifest classification (Python: 3)
 * needs typed loader errors and is tracked as a parity note in docs/typescript/yaml.md. */
const EXIT_VALIDATION = 3;
import { isSubjectKeystore, type SubjectKeystore } from "./subject-keystore.js";
import type { SubjectDeletionClient } from "./subject-execution-deletion.js";
import type { LangfuseTraceApiClient } from "./subject-trace-deletion.js";
import { temporalConnectionOptions } from "./temporal-tls.js";

/** A tiny IO seam so the CLI is testable in-process (Python `capsys`). */
export interface CliIo {
  out: (line: string) => void;
  err: (line: string) => void;
}

const defaultIo: CliIo = {
  out: (line) => process.stdout.write(line + "\n"),
  err: (line) => process.stderr.write(line + "\n"),
};

interface DeployArgs {
  manifest?: string;
  environment?: string;
  workflows: string[];
  policies: string[];
  target: "kubernetes";
  image?: string;
  allowMutableImage: boolean;
  allowPlaceholderImage: boolean;
  allowSharedTaskQueue: boolean;
  projectPathInImage?: string;
  projectManifestPath?: string;
  absoluteManifestPath: boolean;
  configEnv: string[];
  planOut?: string;
  plan?: string;
  output?: string;
  json: boolean;
  requireMergedPlan?: boolean;
}

class UsageError extends Error {}

function parseDeployArgs(argv: readonly string[]): DeployArgs {
  const args: DeployArgs = {
    workflows: [],
    policies: [],
    target: "kubernetes",
    allowMutableImage: false,
    allowPlaceholderImage: false,
    allowSharedTaskQueue: false,
    absoluteManifestPath: false,
    configEnv: [],
    json: false,
  };
  const takeValue = (flag: string, index: number): string => {
    const value = argv[index + 1];
    if (value === undefined) throw new UsageError(`${flag} requires a value`);
    return value;
  };
  for (let i = 0; i < argv.length; i += 1) {
    const token = argv[i]!;
    switch (token) {
      case "--environment":
        args.environment = takeValue(token, i);
        i += 1;
        break;
      case "--workflow":
        args.workflows.push(takeValue(token, i));
        i += 1;
        break;
      case "--policy":
        args.policies.push(takeValue(token, i));
        i += 1;
        break;
      case "--target": {
        const value = takeValue(token, i);
        if (value !== "kubernetes") throw new UsageError(`--target must be 'kubernetes' (got '${value}')`);
        args.target = value;
        i += 1;
        break;
      }
      case "--image":
        args.image = takeValue(token, i);
        i += 1;
        break;
      case "--allow-mutable-image":
        args.allowMutableImage = true;
        break;
      case "--allow-placeholder-image":
        args.allowPlaceholderImage = true;
        break;
      case "--allow-shared-task-queue":
        args.allowSharedTaskQueue = true;
        break;
      case "--project-path-in-image":
        args.projectPathInImage = takeValue(token, i);
        i += 1;
        break;
      case "--project-manifest-path":
        args.projectManifestPath = takeValue(token, i);
        i += 1;
        break;
      case "--absolute-manifest-path":
        args.absoluteManifestPath = true;
        break;
      case "--config-env":
        args.configEnv.push(takeValue(token, i));
        i += 1;
        break;
      case "--plan-out":
        args.planOut = takeValue(token, i);
        i += 1;
        break;
      case "--apply":
        args.plan = takeValue(token, i);
        i += 1;
        break;
      case "--require-merged-plan":
        args.requireMergedPlan = true;
        break;
      case "--output":
        args.output = takeValue(token, i);
        i += 1;
        break;
      case "--json":
        args.json = true;
        break;
      default:
        if (token.startsWith("-")) throw new UsageError(`unknown flag: ${token}`);
        if (args.manifest !== undefined) throw new UsageError(`unexpected argument: ${token}`);
        args.manifest = token;
    }
  }
  return args;
}

function printPlan(plan: ProjectDeploymentPlan, args: DeployArgs, io: CliIo): void {
  if (args.json) {
    io.out(JSON.stringify(plan, null, 2));
    return;
  }
  io.out(`deployment plan (${plan.target}) for project '${plan.project_name}' environment '${plan.environment_id}'`);
  io.out(`  image: ${plan.image}${plan.image_digest_pinned ? " (digest-pinned)" : " (MUTABLE)"}`);
  for (const worker of plan.workers) {
    io.out(`  worker ${worker.name}: workflow='${worker.workflow_id}' task_queue='${worker.task_queue}' policy=${worker.policy.policy_hash.slice(0, 12)}`);
  }
}

function runDeploy(args: DeployArgs, io: CliIo): number {
  if (args.manifest === undefined) {
    io.err("deploy requires a project manifest path");
    return 2;
  }

  let bundle: LoadedProjectBundle;
  try {
    bundle = loadProjectBundle(args.manifest);
  } catch (error) {
    io.err(error instanceof Error ? error.message : String(error));
    return 1;
  }
  const { project, sources } = bundle;
  const resolvedManifest = resolvePath(args.manifest);
  const manifestDir = dirname(resolvedManifest);
  const planResolver = planResolverFor(project, sources);

  // PORTABLE ARTIFACTS (#757 item 1): the value recorded in `project_manifest_path` (render
  // annotations + deployment-plan.json). Default = the manifest RELATIVE to its own directory
  // (machine- and cwd-independent); `--project-manifest-path` records an explicit value verbatim;
  // `--absolute-manifest-path` restores the old absolute path. Provenance only — not hashed.
  const recordedManifestPath = args.projectManifestPath ?? (args.absoluteManifestPath ? resolvedManifest : basename(resolvedManifest));

  // Promotion path: the approved plan file is authoritative. Verify it against the current
  // resolution and fail closed on drift before building anything from the flags.
  let environmentId = args.environment;
  let workflowIds: string[] = args.workflows;
  let policyIds: string[] = args.policies;
  let image = args.image;
  if (args.plan !== undefined) {
    const planPath = isAbsolute(args.plan) ? args.plan : resolvePath(manifestDir, args.plan);
    // The #790 merged-plan gate runs FIRST: an unmerged plan refuses before any
    // resolution work (exit 3 — a governance verdict under the #818 contract).
    if (args.requireMergedPlan === true || process.env["TYPEFLUX_REQUIRE_MERGED_PLAN"] === "1") {
      const mergedGap = verifyPlanMergedToDefaultBranch(planPath, manifestDir);
      if (mergedGap !== undefined) {
        io.err(mergedGap);
        return EXIT_VALIDATION;
      }
    }
    let verification;
    let planFile;
    try {
      planFile = loadDeploymentPlan(planPath);
      verification = verifyDeploymentPlan(project, sources, planResolver, planFile, {
        allowPlaceholderImage: args.allowPlaceholderImage,
      });
    } catch (error) {
      io.err(error instanceof Error ? error.message : String(error));
      return 1;
    }
    if (!verification.ok) {
      io.err("deployment plan drifted from current resolution:");
      for (const mismatch of verification.mismatches) {
        // Synthetic explanation entries (#757 item 5) carry the offending literal in
        // `plan_value` and the human explanation in `current_value` — render them as an
        // explanation, not a value diff.
        if (mismatch.path === PLACEHOLDER_IMAGE_MISMATCH_PATH) {
          io.err(`  ${mismatch.path}: ${String(mismatch.plan_value)}: ${String(mismatch.current_value)}`);
          continue;
        }
        io.err(`  ${mismatch.path}: plan=${JSON.stringify(mismatch.plan_value)} current=${JSON.stringify(mismatch.current_value)}`);
      }
      return EXIT_VALIDATION;
    }
    environmentId = planFile.identity.environment_id;
    workflowIds = [planFile.identity.workflow_id];
    policyIds = [...planFile.policy.selected_policy_ids];
    image = planFile.deployment.image;
    // The approved plan is authoritative for image mutability too: a plan reviewed and
    // merged with a mutable image promotes without re-passing --allow-mutable-image (Bugbot).
    if (planFile.deployment.image_digest_pinned === false) {
      args.allowMutableImage = true;
    }
  } else {
    const missing: string[] = [];
    if (environmentId === undefined) missing.push("--environment");
    if (image === undefined) missing.push("--image");
    if (missing.length > 0) {
      io.err(`deploy requires ${missing.join(" and ")} unless --apply is given`);
      return 2;
    }
  }

  const deploymentResolver: DeploymentWorkflowResolver = (workflowId) =>
    resolveProjectWorkflow(project, sources, workflowId, environmentId!);

  let plan: ProjectDeploymentPlan;
  let renderResult: ProjectDeploymentRenderResult | undefined;
  try {
    plan = buildProjectDeploymentPlan(
      project,
      sources,
      {
        environmentId: environmentId!,
        workflowIds,
        policyIds,
        image: image!,
        allowMutableImage: args.allowMutableImage,
        allowPlaceholderImage: args.allowPlaceholderImage,
        allowSharedTaskQueue: args.allowSharedTaskQueue,
        configEnvNames: args.configEnv,
        manifestPath: resolvedManifest,
        projectManifestPath: recordedManifestPath,
        ...(args.projectPathInImage !== undefined ? { projectPathInImage: args.projectPathInImage } : {}),
        target: args.target,
      },
      deploymentResolver,
    );
    if (args.output !== undefined) {
      renderResult = renderProjectDeploymentPlan(plan, resolvePath(manifestDir, args.output));
    }
    if (args.planOut !== undefined) {
      const planDir = resolvePath(manifestDir, args.planOut);
      // Plan-out is per-workflow: a deploy call naming multiple workflows writes one plan each.
      for (const worker of plan.workers) {
        writeDeploymentPlan(project, sources, planResolver, {
          workflowId: worker.workflow_id,
          environmentId: environmentId!,
          image: image!,
          policyIds,
          allowMutableImage: args.allowMutableImage,
          allowPlaceholderImage: args.allowPlaceholderImage,
          planDir,
        });
      }
    }
  } catch (error) {
    io.err(error instanceof Error ? error.message : String(error));
    return 1;
  }

  if (!args.json && renderResult !== undefined) {
    printRenderResult(renderResult, io);
  } else {
    printPlan(plan, args, io);
  }
  if (args.planOut !== undefined && !args.json) {
    for (const worker of plan.workers) {
      io.out(`  wrote plan for '${worker.workflow_id}' under ${args.planOut}/`);
    }
  }
  return 0;
}

/** Print the rendered artifact digests (Python `_print_deployment_render_result`). */
function printRenderResult(result: ProjectDeploymentRenderResult, io: CliIo): void {
  io.out(`Wrote deployment artifacts to ${result.output_dir}:`);
  for (const file of result.files) {
    io.out(`- ${file.kind}: ${file.path} sha256=${file.sha256}`);
  }
}

// --- The `erase` verb (#715 slice 5) --------------------------------------------------

interface EraseArgs {
  manifest?: string;
  workflow?: string;
  environment?: string;
  subjects: string[];
  surfaces: string[];
  since?: string;
  until?: string;
  dryRun: boolean;
  execute: boolean;
  acknowledgeIrreversible: boolean;
  actor?: string;
  bindings?: string;
  limit?: number;
  json: boolean;
}

function parseEraseArgs(argv: readonly string[]): EraseArgs {
  const args: EraseArgs = {
    subjects: [],
    surfaces: [],
    dryRun: false,
    execute: false,
    acknowledgeIrreversible: false,
    json: false,
  };
  const takeValue = (flag: string, index: number): string => {
    const value = argv[index + 1];
    if (value === undefined) throw new UsageError(`${flag} requires a value`);
    return value;
  };
  for (let i = 0; i < argv.length; i += 1) {
    const token = argv[i]!;
    switch (token) {
      case "--workflow":
        args.workflow = takeValue(token, i);
        i += 1;
        break;
      case "--environment":
        args.environment = takeValue(token, i);
        i += 1;
        break;
      case "--subject":
        args.subjects.push(takeValue(token, i));
        i += 1;
        break;
      case "--surface":
        args.surfaces.push(takeValue(token, i));
        i += 1;
        break;
      case "--since":
        args.since = takeValue(token, i);
        i += 1;
        break;
      case "--until":
        args.until = takeValue(token, i);
        i += 1;
        break;
      case "--dry-run":
        args.dryRun = true;
        break;
      case "--execute":
        args.execute = true;
        break;
      case "--acknowledge-irreversible":
        args.acknowledgeIrreversible = true;
        break;
      case "--actor":
        args.actor = takeValue(token, i);
        i += 1;
        break;
      case "--bindings":
        args.bindings = takeValue(token, i);
        i += 1;
        break;
      case "--limit": {
        const raw = takeValue(token, i);
        const parsed = Number.parseInt(raw, 10);
        if (!Number.isFinite(parsed) || parsed <= 0 || String(parsed) !== raw) {
          throw new UsageError(`--limit must be a positive integer (got '${raw}')`);
        }
        args.limit = parsed;
        i += 1;
        break;
      }
      case "--json":
        args.json = true;
        break;
      default:
        if (token.startsWith("-")) throw new UsageError(`unknown flag: ${token}`);
        if (args.manifest !== undefined) throw new UsageError(`unexpected argument: ${token}`);
        args.manifest = token;
    }
  }
  return args;
}

/** Normalize repeatable/comma-separated `--surface` values; default = all three. */
function parseEraseSurfaces(raw: readonly string[]): string[] {
  if (raw.length === 0) return [...ERASURE_SURFACES];
  const names = raw
    .flatMap((chunk) => chunk.split(","))
    .map((name) => name.trim())
    .filter((name) => name.length > 0);
  if (names.length === 0) {
    throw new UsageError(`--surface named no surfaces; valid surfaces are ${ERASURE_SURFACES.join(", ")}`);
  }
  const unknown = [...new Set(names.filter((name) => !(ERASURE_SURFACES as readonly string[]).includes(name)))].sort();
  if (unknown.length > 0) {
    throw new UsageError(`unknown surface(s) ${unknown.join(", ")}; valid surfaces are ${ERASURE_SURFACES.join(", ")}`);
  }
  const selected = new Set(names);
  return ERASURE_SURFACES.filter((name) => selected.has(name));
}

/** ISO-8601 window bounds must parse — a silently unparseable bound is a wrong scan. */
function validateEraseTimestamp(flag: string, raw: string | undefined): void {
  if (raw === undefined) return;
  if (Number.isNaN(Date.parse(raw))) {
    throw new UsageError(`${flag} must be an ISO-8601 timestamp (got '${raw}')`);
  }
}

/** The pieces an erase bindings module may export (the worker-entry convention). */
interface EraseBindings {
  subjectKeystore?: SubjectKeystore;
  cacheStore?: object;
}

/** Dynamic-import the OPTIONAL bindings module (`--bindings`, falling back to the
 * `TYPEFLUX_WORKER_BINDINGS` env var — the same module a deployed worker uses),
 * through the SHARED loader so default-vs-named export shapes can never diverge from
 * what the worker actually consumes (a divergence would make the receipt report a
 * surface "skipped" while the deployment uses that backend — an under-erasure). */
async function loadEraseBindings(explicit: string | undefined): Promise<EraseBindings> {
  const modulePath = explicit ?? process.env["TYPEFLUX_WORKER_BINDINGS"];
  if (modulePath === undefined || modulePath === "") return {};
  const moduleExports = await importBindingsModule(modulePath);
  const bindings: EraseBindings = {};
  if (moduleExports["subjectKeystore"] !== undefined) {
    if (!isSubjectKeystore(moduleExports["subjectKeystore"])) {
      throw new Error(
        `bindings module '${modulePath}' exports a subjectKeystore that is not a ` +
          "SubjectKeystore (it must expose dataKey(subjectId, { create }) and " +
          "destroySubjectKey(subjectId))",
      );
    }
    bindings.subjectKeystore = moduleExports["subjectKeystore"];
  }
  const cacheStore = moduleExports["cacheStore"];
  if (cacheStore !== undefined) {
    if (typeof cacheStore !== "object" || cacheStore === null) {
      throw new Error(`bindings module '${modulePath}' exports a non-object cacheStore`);
    }
    bindings.cacheStore = cacheStore;
  }
  return bindings;
}

async function runErase(args: EraseArgs, io: CliIo): Promise<number> {
  if (args.manifest === undefined) {
    io.err("erase requires a project manifest path");
    return 2;
  }
  if (args.workflow === undefined || args.environment === undefined) {
    io.err("erase requires --workflow and --environment");
    return 2;
  }
  if (args.subjects.length === 0) {
    io.err("erase requires at least one --subject");
    return 2;
  }
  if (args.dryRun && args.execute) {
    io.err("--dry-run and --execute are mutually exclusive");
    return 2;
  }
  let surfaces: string[];
  try {
    surfaces = parseEraseSurfaces(args.surfaces);
    validateEraseTimestamp("--since", args.since);
    validateEraseTimestamp("--until", args.until);
  } catch (error) {
    if (error instanceof UsageError) {
      io.err(error.message);
      return 2;
    }
    throw error;
  }
  const dryRun = !args.execute;
  if (!dryRun && surfaces.includes("temporal") && !args.acknowledgeIrreversible) {
    // The typed confirmation gate: executing the temporal surface destroys key
    // records and deletes histories PERMANENTLY, so the loss must be named, not
    // implied by --execute alone.
    io.err(
      "erase --execute on the temporal surface is IRREVERSIBLE (key records are " +
        "destroyed and workflow histories deleted permanently). Re-run with " +
        "--acknowledge-irreversible to confirm, or drop the temporal surface " +
        "(--surface langfuse,cache).",
    );
    return 2;
  }

  let receipt: ErasureReceipt;
  let closeConnection: (() => Promise<void>) | undefined;
  let restoreEnvironment: (() => void) | undefined;
  try {
    const { project, sources } = loadProjectBundle(args.manifest);
    // The target environment's variable overlay applies for the erase run and is
    // RESTORED in finally (Python `project_environment_context` parity): unlike the
    // worker entrypoint's whole-lifetime adoption, an in-process caller (tests, an
    // embedder) may run several erases against different environments, and leftover
    // LANGFUSE_*/DSN values would silently drive the NEXT run's backends.
    const environmentSpec = Object.hasOwn(sources.environments, args.environment)
      ? sources.environments[args.environment]
      : undefined;
    if (environmentSpec !== undefined) {
      restoreEnvironment = applyEnvironmentVariablesToProcessEnv(environmentSpec.variables);
    }
    const resolved = resolveProjectWorkflow(project, sources, args.workflow, args.environment);
    if (resolved === undefined) {
      io.err(`cannot resolve workflow '${args.workflow}' under environment '${args.environment}'`);
      return 1;
    }
    const bindings = await loadEraseBindings(args.bindings);
    const deps: EraseSubjectDeps & { temporalClient?: SubjectDeletionClient } = {
      ...(bindings.subjectKeystore !== undefined ? { subjectKeystore: bindings.subjectKeystore } : {}),
      ...(bindings.cacheStore !== undefined ? { cacheStore: bindings.cacheStore } : {}),
    };
    if (surfaces.includes("temporal")) {
      // Erase never encodes/decodes payloads (visibility + DeleteWorkflowExecution
      // only), so the client needs no payload codec — but it honors the manifest's
      // TLS/api-key connection config exactly like the worker entrypoint.
      const connectionOptions = temporalConnectionOptions(resolved.spec.runtime.temporal);
      const { Client, Connection } = (await import("@temporalio/client")) as typeof import("@temporalio/client");
      const connection = await Connection.connect({
        address: connectionOptions.address,
        tls: connectionOptions.tls,
        ...(connectionOptions.apiKey !== undefined ? { apiKey: connectionOptions.apiKey } : {}),
      });
      closeConnection = () => connection.close();
      const client = new Client({ connection, namespace: connectionOptions.namespace });
      (deps as { temporalClient?: SubjectDeletionClient }).temporalClient =
        client as unknown as SubjectDeletionClient;
      (deps as { temporalNamespace?: string }).temporalNamespace = connectionOptions.namespace;
    }
    if (surfaces.includes("langfuse") && resolved.spec.runtime.observability?.type === "langfuse") {
      // Spec-declared credentials (#793) count exactly like env ones — the workflow's
      // OWN spec is in hand here (spec wins field-by-field, env fallback), so a
      // workflow whose credentials come only from runtime.observability.langfuse.*
      // is not skipped as unconfigured. The client is built from the RETURNED
      // values, so check and use can never diverge.
      const credentials = langfuseCredentialsFromSpec(resolved.spec);
      if (credentials !== undefined) {
        const sdk = (await import("langfuse")) as unknown as {
          Langfuse: new (options: Record<string, unknown>) => unknown;
        };
        const langfuseClient = new sdk.Langfuse({ ...credentials });
        (deps as { langfuseClient?: LangfuseTraceApiClient }).langfuseClient =
          langfuseClient as LangfuseTraceApiClient;
      }
    }
    receipt = await eraseSubject(args.subjects, deps, {
      actor: args.actor ?? userInfo().username,
      dryRun,
      surfaces,
      requireTargetedCache: resolved.spec.runtime.cache_erasure === "targeted",
      ...(args.since !== undefined ? { since: args.since } : {}),
      ...(args.until !== undefined ? { until: args.until } : {}),
      ...(args.limit !== undefined ? { executionLimit: args.limit } : {}),
    });
  } catch (error) {
    io.err(`Project subject erase failed: ${error instanceof Error ? error.message : String(error)}`);
    return 1;
  } finally {
    await closeConnection?.();
    restoreEnvironment?.();
  }
  if (args.json) {
    io.out(JSON.stringify(receipt, null, 2));
  } else {
    printErasureReceipt(receipt, io);
  }
  // A FAILED surface must be unmistakable: non-zero exit whenever any surface failed
  // (dry-run included — a failed dry run is not a reviewable plan).
  return erasureFailed(receipt) ? 1 : 0;
}

function printErasureReceipt(receipt: ErasureReceipt, io: CliIo): void {
  io.out(`Erasure ${receipt.dryRun ? "DRY RUN (no mutation)" : "EXECUTED"}`);
  io.out(`Subjects: ${receipt.subjectIds.join(", ")}`);
  io.out(`Actor: ${receipt.actor}`);
  io.out(`At: ${receipt.executedAt}`);
  const { temporal, langfuse, cache } = receipt.surfaces;
  io.out(`temporal [${temporal.status}]`);
  if (temporal.skipReason !== undefined) {
    io.out(`  skipped: ${temporal.skipReason}`);
  } else {
    const keystore = temporal.keystore!;
    if (keystore.skipReason !== undefined) {
      io.out(`  keystore [skipped]: ${keystore.skipReason}`);
    } else {
      io.out(
        `  keystore [${keystore.status}]: shreddable=${keystore.shreddableKeyRecords} ` +
          `shredded=${keystore.shreddedKeyRecords}`,
      );
      for (const entry of keystore.entries ?? []) {
        io.out(
          `    - ${entry.subjectId}: state_before=${entry.stateBefore} ` +
            `would_shred=${entry.wouldShred} shredded=${entry.shredded}`,
        );
      }
      for (const failure of keystore.failures ?? []) {
        io.out(`    ! ${failure.subjectId}: ${failure.error}`);
      }
    }
    const executions = temporal.executions!;
    if (executions.skipReason !== undefined) {
      io.out(`  executions [skipped]: ${executions.skipReason}`);
    } else {
      io.out(`  executions [${executions.status}]:`);
      for (const report of executions.reports ?? []) {
        io.out(
          `    - ${report.subjectId}: matched=${report.executionsMatched} ` +
            `deletable=${report.deletable.length} deleted=${report.deletedCount} ` +
            `conflicted=${report.conflicted.length} running=${report.stillRunning.length} ` +
            `failed=${report.failures.length}`,
        );
      }
      for (const failure of executions.failures ?? []) {
        io.out(`    ! ${failure.subjectId}: ${failure.error}`);
      }
    }
  }
  io.out(`langfuse [${langfuse.status}]`);
  if (langfuse.skipReason !== undefined) {
    io.out(`  skipped: ${langfuse.skipReason}`);
  } else {
    for (const report of langfuse.reports ?? []) {
      io.out(
        `  - ${report.subjectId}: deletable=${report.traceIds.length} ` +
          `deleted=${report.deletedCount} conflicted=${report.conflicted.length} ` +
          `failed=${report.failures.length}`,
      );
    }
    for (const failure of langfuse.failures ?? []) {
      io.out(`  ! ${failure.subjectId}: ${failure.error}`);
    }
  }
  io.out(`cache [${cache.status}]`);
  if (cache.skipReason !== undefined) {
    io.out(`  skipped: ${cache.skipReason}`);
  } else {
    for (const report of cache.reports ?? []) {
      const supported = report.supported ? "" : " (NOT SUPPORTED: full flush required)";
      io.out(`  - ${report.subjectId}: found=${report.keysFound} deleted=${report.keysDeleted}${supported}`);
    }
    for (const failure of cache.failures ?? []) {
      io.out(`  ! ${failure.subjectId}: ${failure.error}`);
    }
  }
  io.out("Unreachable surfaces (document-only):");
  for (const note of receipt.unreachable) {
    io.out(`  - ${note.surface}: ${note.note}`);
  }
  if (receipt.warnings.length > 0) {
    io.out("Warnings:");
    for (const warning of receipt.warnings) {
      io.out(`  - ${warning}`);
    }
  }
  io.out("The receipt is the proof of erasure — persist it (use --json) OUTSIDE the erased surfaces.");
}

/**
 * Run the CLI over `argv` (everything after the program name). Uniformly async
 * (#715 fix round, item 10): every verb resolves to a process exit code — the sync
 * `deploy` work simply resolves immediately.
 */
export async function main(argv: readonly string[], io: CliIo = defaultIo): Promise<number> {
  const [command, ...rest] = argv;
  if (command === undefined || command === "--help" || command === "-h") {
    io.err("usage: typeflux-project <deploy|erase> <manifest> [options]");
    return command === undefined ? 2 : 0;
  }
  if (command === "erase") {
    let eraseArgs: EraseArgs;
    try {
      eraseArgs = parseEraseArgs(rest);
    } catch (error) {
      io.err(error instanceof UsageError ? error.message : String(error));
      return 2;
    }
    try {
      return await runErase(eraseArgs, io);
    } catch (error) {
      io.err(error instanceof Error ? error.message : String(error));
      return 1;
    }
  }
  if (command !== "deploy") {
    io.err(`unknown command: ${command} (supported: deploy, erase)`);
    return 2;
  }
  let args: DeployArgs;
  try {
    args = parseDeployArgs(rest);
  } catch (error) {
    io.err(error instanceof UsageError ? error.message : String(error));
    return 2;
  }
  try {
    return runDeploy(args, io);
  } catch (error) {
    // A ProjectDeploymentError that escaped the inner guards, or any unexpected failure.
    io.err(error instanceof ProjectDeploymentError ? error.message : error instanceof Error ? error.message : String(error));
    return 1;
  }
}

/**
 * Whether this module is the process entrypoint (#757 item 4). `process.argv[1]` is the invoked
 * path — through the npm `.bin` shim it is the SYMLINK (`node_modules/.bin/typeflux-project`),
 * while `import.meta.url` is the module's REAL path (`.../dist/cli.js`), so a naive
 * `import.meta.url === file://${argv[1]}` (or `endsWith`) NEVER matches and the bin silently
 * no-ops with exit 0. Realpath BOTH sides so the symlinked bin invocation is detected.
 */
function runningAsMain(): boolean {
  const entry = process.argv[1];
  if (entry === undefined) return false;
  try {
    return realpathSync(entry) === realpathSync(fileURLToPath(import.meta.url));
  } catch {
    // A non-file entry (e.g. an eval/REPL argv[1] that does not exist on disk) is never main.
    return false;
  }
}

// Executed directly as the `typeflux-project` bin (not when imported by a test).
if (runningAsMain()) {
  void main(process.argv.slice(2)).then((code) => process.exit(code));
}
