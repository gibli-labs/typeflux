/**
 * Subprocess resolver entry (#642): the TypeScript edition of the resolver contract
 * (`contracts/resolver/resolver.v1.json`) over newline-delimited JSON on stdio, so a FOREIGN
 * control plane (the Python CP) resolves `runtime: typescript` projects by spawning this process.
 *
 * Protocol (the contract's `subprocess_framing`):
 *   - one boot readiness line: {"event": "ready", "resolver_version": "1", "runtime": "typescript"}
 *   - one request per stdin line:  {"id"?: string, "operation": <name>, "params": {...}}
 *   - one response per stdout line, in order: {"id"?, "ok": true, "result": ...}
 *                                           | {"id"?, "ok": false, "status": <int>, "error": {"error", "message"}}
 *   - diagnostics to stderr, never stdout.
 *
 * Long-lived; every request resolves with per-request freshness (the in-process resolver rebuilds
 * the control plane from the manifest on every call). Failures are the same ApiError (+ the HTTP
 * status) the TS server would answer with — the foreign CP forwards them instead of translating.
 *
 * Flags:
 *   --schemas <module.js>     dynamic-import a module whose default export is the Zod schema map
 *                             (Record<string, z.ZodType>, keyed by spec ref e.g. "schemas:ClaimItem")
 *                             — the injected-schemas seam (#620); bundle/catalog 422 honestly without it
 *   --conformance-schemas     use the built-in conformance fixture project's schemas instead
 *
 * BUILD PREREQUISITE: runs the compiled dist — (cd packages/typescript && pnpm -r build).
 */

import { createInterface } from "node:readline";
import { pathToFileURL } from "node:url";

import type { z } from "zod";

import { defaultErrorName, ProjectControlPlaneError } from "./errors.js";
import { CONFORMANCE_SCHEMAS } from "./http/conformance-schemas.js";
import { InProcessTypescriptResolver } from "./resolver.js";

interface ParsedArgs {
  schemasModule: string | undefined;
  conformanceSchemas: boolean;
}

function parseArgs(argv: readonly string[]): ParsedArgs {
  const parsed: ParsedArgs = { schemasModule: undefined, conformanceSchemas: false };
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index]!;
    switch (arg) {
      case "--schemas":
        parsed.schemasModule = argv[(index += 1)];
        if (parsed.schemasModule === undefined) throw new Error("--schemas requires a module path");
        break;
      case "--conformance-schemas":
        parsed.conformanceSchemas = true;
        break;
      default:
        throw new Error(`unknown argument: ${arg}`);
    }
  }
  if (parsed.schemasModule !== undefined && parsed.conformanceSchemas) {
    throw new Error("--schemas and --conformance-schemas are mutually exclusive");
  }
  return parsed;
}

/** Load the injected schema map from a caller-supplied module (its default export). */
async function loadSchemasModule(modulePath: string): Promise<Record<string, z.ZodType>> {
  const imported = (await import(pathToFileURL(modulePath).href)) as { default?: unknown };
  const schemas = imported.default;
  if (typeof schemas !== "object" || schemas === null) {
    throw new Error(`--schemas module ${modulePath} must default-export a Record<string, ZodType>`);
  }
  return schemas as Record<string, z.ZodType>;
}

type WireRequest = { id?: unknown; operation?: unknown; params?: unknown };

/** Map one wire request to the resolver call. Unknown operations are a 422 — the caller can fix and retry. */
async function dispatch(resolver: InProcessTypescriptResolver, request: WireRequest): Promise<unknown> {
  const params = (typeof request.params === "object" && request.params !== null ? request.params : {}) as Record<
    string,
    unknown
  >;
  const manifestPath = params["manifest_path"];
  if (typeof manifestPath !== "string" || manifestPath.length === 0) {
    throw new ProjectControlPlaneError("manifest_path is required", 422, "InvalidRequest");
  }
  const str = (key: string): string => {
    const value = params[key];
    if (typeof value !== "string" || value.length === 0) {
      throw new ProjectControlPlaneError(`${key} is required`, 422, "InvalidRequest");
    }
    return value;
  };
  switch (request.operation) {
    case "resolve_bundle":
      return resolver.resolveBundle(manifestPath, {
        workflowId: str("workflow_id"),
        environmentId: str("environment_id"),
        policyIds: Array.isArray(params["policy_ids"]) ? (params["policy_ids"] as string[]) : [],
        deploymentImage: (params["deployment_image"] as string | null | undefined) ?? null,
      });
    case "resolve_catalog":
      return resolver.resolveCatalog(manifestPath, {
        workflowId: str("workflow_id"),
        environmentId: str("environment_id"),
      });
    case "validate_project":
      return resolver.validateProject(manifestPath, {
        environmentId: (params["environment_id"] as string | null | undefined) ?? null,
        ...(Array.isArray(params["workflow_ids"]) ? { workflowIds: params["workflow_ids"] as string[] } : {}),
        ...(Array.isArray(params["policy_ids"]) ? { policyIds: params["policy_ids"] as string[] } : {}),
      });
    case "prompt_status":
      return resolver.promptStatus(manifestPath, {
        workflowId: str("workflow_id"),
        environmentId: str("environment_id"),
      });
    case "resolve_plan":
      return resolver.resolvePlan(manifestPath, {
        workflowId: str("workflow_id"),
        environmentId: str("environment_id"),
      });
    default:
      throw new ProjectControlPlaneError(
        `unknown resolver operation: ${JSON.stringify(request.operation)}`,
        422,
        "InvalidRequest",
      );
  }
}

/** One wire failure envelope: the ApiError (+ status) the TS server would answer with. */
function failureEnvelope(id: unknown, error: unknown): Record<string, unknown> {
  if (error instanceof ProjectControlPlaneError) {
    return {
      ...(id !== undefined ? { id } : {}),
      ok: false,
      status: error.status,
      error: { error: error.errorName ?? defaultErrorName(error.status), message: error.message },
    };
  }
  // A loader/spec error from temporal-yaml is a CONFIG error (Python maps TypefluxError/ValueError
  // to 422); anything else would be a genuine bug — still answer structured, never crash the stream.
  const message = error instanceof Error ? error.message : String(error);
  const name = error instanceof Error && error.name !== "Error" ? error.name : "ProjectError";
  return { ...(id !== undefined ? { id } : {}), ok: false, status: 422, error: { error: name, message } };
}

async function main(): Promise<void> {
  const args = parseArgs(process.argv.slice(2));
  const schemas = args.conformanceSchemas
    ? CONFORMANCE_SCHEMAS
    : args.schemasModule !== undefined
      ? await loadSchemasModule(args.schemasModule)
      : undefined;
  const resolver = new InProcessTypescriptResolver({ ...(schemas !== undefined ? { schemas } : {}) });

  process.stdout.write(JSON.stringify({ event: "ready", resolver_version: "1", runtime: "typescript" }) + "\n");

  // Serialize responses in request order: the reader awaits the previous dispatch before starting
  // the next, so responses can never interleave (the Python client pairs strictly in order).
  const lines = createInterface({ input: process.stdin, crlfDelay: Infinity });
  for await (const line of lines) {
    const trimmed = line.trim();
    if (trimmed.length === 0) continue;
    let request: WireRequest;
    try {
      request = JSON.parse(trimmed) as WireRequest;
    } catch {
      process.stdout.write(
        JSON.stringify({ ok: false, status: 422, error: { error: "InvalidRequest", message: "request is not valid JSON" } }) +
          "\n",
      );
      continue;
    }
    let response: Record<string, unknown>;
    try {
      const result = await dispatch(resolver, request);
      response = { ...(request.id !== undefined ? { id: request.id } : {}), ok: true, result };
    } catch (error) {
      response = failureEnvelope(request.id, error);
    }
    process.stdout.write(JSON.stringify(response) + "\n");
  }
}

main().catch((error: unknown) => {
  process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
  process.exit(2);
});
