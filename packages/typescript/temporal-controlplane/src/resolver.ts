/**
 * The Resolver seam, TypeScript binding (#619 slice 4).
 *
 * `contracts/resolver/resolver.v1.json` names resolution — turning a project
 * manifest into contract DTOs — as the one language-bound control-plane
 * layer. This module binds it in-process for the TypeScript runtime by
 * composing `loadProjectBundle` (the fs tree read) with the pure
 * `ProjectControlPlane` projection core.
 *
 * Honest coverage, fail closed everywhere:
 * - `validateProject` binds fully.
 * - `resolveBundle`/`resolveCatalog` bind with constructor-injected Zod
 *   schemas (the TS edition has no manifest-path-only schema discovery —
 *   its server, #620, supplies the schemas); without them the projection
 *   core's structured 422 stands. Never stub garbage.
 * - `promptStatus` binds the drift projection (#639); the backend-registry
 *   live-lookup tier reports honest unknowns (no registry client here).
 *
 * The operations are async on purpose: a future schema-discovery /
 * dynamic-import story (and the subprocess transport of the epic's slice 5)
 * both want the async surface from day one.
 */

import { homedir } from "node:os";
import { join, resolve } from "node:path";

import type * as z from "zod";

import { loadProjectBundle } from "@typeflux/temporal-yaml";

import type {
  ApiActivityCatalog,
  ApiProjectValidationReport,
  ApiResolvedPlan,
  ApiResolvedWorkflowBundle,
  ApiWorkflowPromptStatus,
} from "./index.js";
import { ProjectControlPlane } from "./project-control-plane.js";

/** The contract's resolution operations (resolver.v1.json): the four reads + `resolve_plan` (#642). */
export interface TypefluxResolver {
  readonly runtime: string;

  resolveBundle(
    manifestPath: string,
    options: {
      workflowId: string;
      environmentId: string;
      policyIds?: readonly string[];
      deploymentImage?: string | null;
    },
  ): Promise<ApiResolvedWorkflowBundle>;

  resolveCatalog(
    manifestPath: string,
    options: { workflowId: string; environmentId: string },
  ): Promise<ApiActivityCatalog>;

  validateProject(
    manifestPath: string,
    options?: {
      environmentId?: string | null;
      workflowIds?: readonly string[];
      policyIds?: readonly string[];
    },
  ): Promise<ApiProjectValidationReport>;

  promptStatus(
    manifestPath: string,
    options: { workflowId: string; environmentId: string },
  ): Promise<ApiWorkflowPromptStatus>;

  /**
   * The raw plan + start identity for a plan-as-argument dispatch (#642). BINDING-PROFILE-SCOPED:
   * meaningful for runtimes whose temporal-binding profile passes the plan as a start argument
   * (`ts-plan-argument`); a resolver for a runtime whose profile registers the plan into a
   * versioned type instead answers with a structured 422 rejection (see the contract's
   * profile_note) — never a fabricated plan.
   */
  resolvePlan(
    manifestPath: string,
    options: { workflowId: string; environmentId: string },
  ): Promise<ApiResolvedPlan>;
}

export interface InProcessTypescriptResolverOptions {
  /**
   * Zod schemas by type-ref name, required by bundle/catalog resolution.
   * The TS edition derives schemas from registered project code, not from
   * the manifest — the embedding server supplies them (#620).
   */
  schemas?: Record<string, z.ZodType>;
}

/**
 * Absolute, home-expanded manifest path — the DTO identity must be stable
 * regardless of caller cwd and must honor `~/…` exactly like the fs loader
 * (and Python's `expanduser().resolve()`).
 */
export function normalizeManifestPath(manifestPath: string): string {
  if (manifestPath === "~") {
    return homedir();
  }
  if (manifestPath.startsWith("~/")) {
    return resolve(join(homedir(), manifestPath.slice(2)));
  }
  return resolve(manifestPath);
}

/** The TypeScript runtime's in-process resolver. */
export class InProcessTypescriptResolver implements TypefluxResolver {
  readonly runtime = "typescript";
  private readonly schemas: Record<string, z.ZodType> | undefined;

  constructor(options: InProcessTypescriptResolverOptions = {}) {
    this.schemas = options.schemas;
  }

  private controlPlane(manifestPath: string): ProjectControlPlane {
    // A black box over manifestPath, like the Python resolver: every call
    // re-reads the project tree, preserving the per-request freshness
    // contract. Normalized to an absolute path so the DTOs' manifest_path
    // is a stable identity regardless of caller cwd — matching Python's
    // load_project_spec resolution.
    const absolute = normalizeManifestPath(manifestPath);
    return new ProjectControlPlane(loadProjectBundle(absolute), {
      manifestPath: absolute,
      ...(this.schemas !== undefined ? { schemas: this.schemas } : {}),
    });
  }

  async resolveBundle(
    manifestPath: string,
    options: {
      workflowId: string;
      environmentId: string;
      policyIds?: readonly string[];
      deploymentImage?: string | null;
    },
  ): Promise<ApiResolvedWorkflowBundle> {
    // deploymentImage is real now (#687): a supplied image yields the secret-free
    // `deployment_preview`; its absence yields `deployment_preview_reference`.
    return this.controlPlane(manifestPath).bundle(
      options.workflowId,
      options.environmentId,
      options.policyIds ?? [],
      options.deploymentImage ?? undefined,
    );
  }

  async resolveCatalog(
    manifestPath: string,
    options: { workflowId: string; environmentId: string },
  ): Promise<ApiActivityCatalog> {
    return this.controlPlane(manifestPath).activityCatalog(
      options.workflowId,
      options.environmentId,
    );
  }

  async validateProject(
    manifestPath: string,
    options: {
      environmentId?: string | null;
      workflowIds?: readonly string[];
      policyIds?: readonly string[];
    } = {},
  ): Promise<ApiProjectValidationReport> {
    return this.controlPlane(manifestPath).validate({
      ...(options.environmentId != null ? { environmentId: options.environmentId } : {}),
      ...(options.workflowIds !== undefined ? { workflowIds: options.workflowIds } : {}),
      ...(options.policyIds !== undefined ? { policyIds: options.policyIds } : {}),
    });
  }

  async promptStatus(
    manifestPath: string,
    options: { workflowId: string; environmentId: string },
  ): Promise<ApiWorkflowPromptStatus> {
    return this.controlPlane(manifestPath).promptStatus(options.workflowId, options.environmentId);
  }

  async resolvePlan(
    manifestPath: string,
    options: { workflowId: string; environmentId: string },
  ): Promise<ApiResolvedPlan> {
    // The SAME derivation the TS operate tier dispatches (`workflowPlanFromSpec` over the resolved,
    // profile-composed spec) — a foreign control plane starting via this DTO starts the identical
    // plan a TS-edition start would. Needs no schemas: the plan is structural (steps/routing).
    return this.controlPlane(manifestPath).resolvedPlan(options.workflowId, options.environmentId);
  }
}
