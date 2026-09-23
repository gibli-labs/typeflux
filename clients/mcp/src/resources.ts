/**
 * MCP resource registration (#326 Phase 0). Two families:
 *
 *   STATIC docs (no backend) — always available, even in the degraded (403) state:
 *     typeflux://docs/{slug}              the design-listed docs, bundled
 *     typeflux://docs/typescript/{slug}   the TS-edition doc mirror (#862)
 *     typeflux://examples/{name}          a distilled example project (Python edition)
 *     typeflux://examples/typescript/{name}  a distilled TS-edition example (#863)
 *     typeflux://guide/authoring-checklist  curated how-to for writing a correct activity
 *     typeflux://guide/project-layout       project setup: modes, layout, manifest, engine.lock (#865)
 *     typeflux://schema/{typeflux-yaml|project}  generated JSON Schema
 *
 *   LIVE control-plane reads — the secret-free JSON the contract returns, application/json. Every
 *     live URI carries the design's `{project}` dimension (§3.3/§5.2): the reserved token `default`
 *     (or an empty value) uses the default project (unprefixed routes); any other value scopes to
 *     that project id. The registered URI templates match the coverage ledger 1:1 (asserted by the
 *     conformance test), so the surface can't drift from the contract.
 *
 * A failed live read is returned AS application/json `{ error: { code, status, message } }` (design
 * §9: structured, never flattened) so one 501/503 never takes the whole resource surface down.
 */

import { ResourceTemplate } from "@modelcontextprotocol/sdk/server/mcp.js";
import type {
  McpServer,
  RegisteredResource,
  RegisteredResourceTemplate,
} from "@modelcontextprotocol/sdk/server/mcp.js";
import type { Variables } from "@modelcontextprotocol/sdk/shared/uriTemplate.js";

import type { Backend } from "./backend.js";
import type { Completers } from "./completions.js";
import type { TypefluxControlPlane } from "./control-plane/client.js";
import { ApiRequestError, describeError, STATUS_CODES } from "./control-plane/errors.js";
import {
  listDocs,
  listExamples,
  listTypescriptDocs,
  listTypescriptExamples,
  readAuthoringChecklist,
  readProjectLayoutGuide,
  readDoc,
  readExample,
  readSchema,
  readTypescriptDoc,
  readTypescriptExample,
  SCHEMA_FILES,
} from "./content.js";

type GetBackend = () => Promise<Backend>;

function textContents(uri: string, text: string, mimeType: string) {
  return { contents: [{ uri, mimeType, text }] };
}

function jsonContents(uri: string, data: unknown) {
  return textContents(uri, JSON.stringify(data, null, 2), "application/json");
}

/** Render a thrown error as an application/json error resource body (design §9). */
function errorContents(uri: string, error: unknown) {
  if (error instanceof ApiRequestError) {
    return jsonContents(uri, {
      error: {
        code: error.code ?? STATUS_CODES[error.status] ?? "RequestFailed",
        status: error.status,
        message: error.message,
      },
    });
  }
  return jsonContents(uri, {
    error: {
      code: "BackendUnavailable",
      status: 0,
      message: error instanceof Error ? error.message : String(error),
    },
  });
}

function one(value: string | string[] | undefined): string | undefined {
  return Array.isArray(value) ? value[0] : value;
}

/** Require a path/query variable, throwing the contract's 422 shape when absent. */
function required(variables: Variables, name: string): string {
  const value = one(variables[name] as string | string[] | undefined);
  if (value === undefined || value === "") {
    throw new ApiRequestError(
      describeError(422, { error: "InvalidRequest", message: `missing required parameter: ${name}` }),
    );
  }
  return value;
}

/**
 * One markdown resource family = one URI prefix + a {list, read} loader pair (content.ts). The
 * static docs/examples surface is this table — editions are rows, not copy-pasted registration
 * blocks (#862/#863), mirroring the LIVE_RESOURCES data-table idiom below.
 */
const MARKDOWN_FAMILIES: ReadonlyArray<{
  id: string;
  /** URI prefix after `typeflux://`, e.g. `docs` or `docs/typescript`. */
  prefix: string;
  variable: "slug" | "name";
  title: string;
  description: string;
  list: () => string[];
  read: (slug: string) => string | undefined;
  /** The 404 message, e.g. `unknown doc` — completed with the failing slug. */
  notFound: string;
}> = [
  {
    id: "typeflux-docs",
    prefix: "docs",
    variable: "slug",
    title: "Typeflux documentation",
    description: "Core Typeflux docs (concepts, YAML, control plane, ...).",
    list: listDocs,
    read: readDoc,
    notFound: "unknown doc",
  },
  {
    id: "typeflux-docs-typescript",
    prefix: "docs/typescript",
    variable: "slug",
    title: "Typeflux documentation (TypeScript edition)",
    description: "The TS-edition doc mirror (tutorial, YAML, code-defined workflows, ...).",
    list: listTypescriptDocs,
    read: readTypescriptDoc,
    notFound: "unknown TypeScript doc",
  },
  {
    id: "typeflux-examples",
    prefix: "examples",
    variable: "name",
    title: "Typeflux examples",
    description: "Distilled example projects (YAML + code shape).",
    list: listExamples,
    read: readExample,
    notFound: "unknown example",
  },
  {
    id: "typeflux-examples-typescript",
    prefix: "examples/typescript",
    variable: "name",
    title: "Typeflux examples (TypeScript edition)",
    description: "Distilled TS-edition example projects (YAML + zod/defineActivity code shape).",
    list: listTypescriptExamples,
    read: readTypescriptExample,
    notFound: "unknown TypeScript example",
  },
];

/**
 * Register the static documentation resources (no backend needed). The generic `{slug}`/`{name}`
 * variables reject "/" (content.ts traversal guard), so an edition-scoped prefix like
 * `docs/typescript` can never be shadowed by its parent family.
 */
export function registerStaticResources(server: McpServer): void {
  for (const family of MARKDOWN_FAMILIES) {
    server.registerResource(
      family.id,
      new ResourceTemplate(`typeflux://${family.prefix}/{${family.variable}}`, {
        list: () => ({
          resources: family.list().map((slug) => ({
            uri: `typeflux://${family.prefix}/${slug}`,
            name: `${family.prefix}/${slug}`,
            mimeType: "text/markdown",
          })),
        }),
      }),
      { title: family.title, description: family.description },
      (uri, variables) => {
        const slug = one(variables[family.variable] as string | string[]) ?? "";
        const text = family.read(slug);
        if (text === undefined) {
          return errorContents(uri.href, new ApiRequestError(describeError(404, { error: "NotFound", message: `${family.notFound}: ${slug}` })));
        }
        return textContents(uri.href, text, "text/markdown");
      },
    );
  }

  server.registerResource(
    "typeflux-schema",
    new ResourceTemplate("typeflux://schema/{name}", {
      list: () => ({
        resources: Object.keys(SCHEMA_FILES).map((name) => ({
          uri: `typeflux://schema/${name}`,
          name: `schema/${name}`,
          mimeType: "application/schema+json",
        })),
      }),
    }),
    {
      title: "Typeflux JSON Schemas",
      description: "Generated JSON Schema for the workflow YAML and the project manifest.",
    },
    (uri, variables) => {
      const name = one(variables.name as string | string[]) ?? "";
      const schema = readSchema(name);
      if (schema === undefined) {
        return errorContents(uri.href, new ApiRequestError(describeError(404, { error: "NotFound", message: `unknown schema: ${name}` })));
      }
      return textContents(uri.href, schema, "application/schema+json");
    },
  );

  server.registerResource(
    "typeflux-authoring-checklist",
    "typeflux://guide/authoring-checklist",
    {
      title: "Authoring checklist",
      description: "How to write a correct Typeflux AI activity/workflow.",
      mimeType: "text/markdown",
    },
    (uri) => {
      const guide = readAuthoringChecklist();
      if (guide === undefined) {
        return errorContents(uri.href, new ApiRequestError(describeError(404, { error: "NotFound", message: "authoring checklist not bundled" })));
      }
      return textContents(uri.href, guide, "text/markdown");
    },
  );

  server.registerResource(
    "typeflux-project-layout",
    "typeflux://guide/project-layout",
    {
      title: "Project layout & adoption guide",
      description: "How to set up a Typeflux project: authoring modes, file layout, manifest growth, engine.lock pinning.",
      mimeType: "text/markdown",
    },
    (uri) => {
      const guide = readProjectLayoutGuide();
      if (guide === undefined) {
        return errorContents(uri.href, new ApiRequestError(describeError(404, { error: "NotFound", message: "project-layout guide not bundled" })));
      }
      return textContents(uri.href, guide, "text/markdown");
    },
  );
}

/**
 * One live control-plane resource. `ledgerUri` is the path form recorded in the coverage ledger;
 * `template` is the RFC6570 URI (may add a `{?query}` suffix). `read` receives a project-scoped
 * control plane (per the `{project}` dimension) and the filled variables.
 *
 * NOTE on query params: the MCP SDK's `UriTemplate` matcher treats every declared query var as
 * REQUIRED and ORDERED (`^...$`-anchored), so it cannot express optional query params on a resource
 * URI. Templates therefore declare only the REQUIRED params (environment_id, execution_id). The
 * richer, optional scoping the contract supports (policy_id[], deployment_image, task_queue, limit,
 * candidate workflows) lives on the corresponding TOOLS — which use JSON input schemas and have no
 * such limitation — exactly the design's "call vs subscribe" split (§6.1).
 */
interface LiveResource {
  name: string;
  description: string;
  ledgerUri: string;
  template: string;
  read: (cp: TypefluxControlPlane, variables: Variables) => Promise<unknown>;
}

const LIVE_RESOURCES: LiveResource[] = [
  { name: "meta", description: "Control-plane meta + capabilities.", ledgerUri: "typeflux://{project}/meta", template: "typeflux://{project}/meta", read: (cp) => cp.meta() },
  { name: "workflows", description: "Workflows in the project.", ledgerUri: "typeflux://{project}/workflows", template: "typeflux://{project}/workflows", read: (cp) => cp.workflows() },
  { name: "environments", description: "Declared environments.", ledgerUri: "typeflux://{project}/environments", template: "typeflux://{project}/environments", read: (cp) => cp.environments() },
  { name: "policies", description: "Declared policies.", ledgerUri: "typeflux://{project}/policies", template: "typeflux://{project}/policies", read: (cp) => cp.policies() },
  { name: "profiles", description: "Declared component profiles.", ledgerUri: "typeflux://{project}/profiles", template: "typeflux://{project}/profiles", read: (cp) => cp.profiles() },
  { name: "annotations", description: "In-repo insight acknowledgements.", ledgerUri: "typeflux://{project}/annotations", template: "typeflux://{project}/annotations", read: (cp) => cp.annotations() },
  { name: "deployments", description: "Deployment plans.", ledgerUri: "typeflux://{project}/deployments", template: "typeflux://{project}/deployments", read: (cp) => cp.deployments() },
  {
    name: "environment",
    description: "One environment definition.",
    ledgerUri: "typeflux://{project}/environments/{environment_id}",
    template: "typeflux://{project}/environments/{environment_id}",
    read: (cp, v) => cp.environment(required(v, "environment_id")),
  },
  {
    name: "policy",
    description: "One policy definition.",
    ledgerUri: "typeflux://{project}/policies/{policy_id}",
    template: "typeflux://{project}/policies/{policy_id}",
    read: (cp, v) => cp.policy(required(v, "policy_id")),
  },
  {
    name: "profile",
    description: "One component profile.",
    ledgerUri: "typeflux://{project}/profiles/{kind}/{profile_id}",
    template: "typeflux://{project}/profiles/{kind}/{profile_id}",
    read: (cp, v) => cp.profile(required(v, "kind"), required(v, "profile_id")),
  },
  {
    // The unscoped whole-project report; candidate workflow/policy scoping is on `validate_project`
    // (the SDK's URI-template query matching can't express optional params — see NOTE below).
    name: "validate",
    description: "Whole-project validation report. For candidate workflow/policy scoping, use the validate_project tool.",
    ledgerUri: "typeflux://{project}/validate",
    template: "typeflux://{project}/validate",
    read: (cp) => cp.validate(),
  },
  {
    // Candidate-policy / deployment-image preview is on the get_bundle tool (see NOTE below).
    name: "bundle",
    description: "Fully-resolved workflow bundle. For candidate-policy / deployment-image preview, use the get_bundle tool.",
    ledgerUri: "typeflux://{project}/workflows/{workflow_id}/bundle",
    template: "typeflux://{project}/workflows/{workflow_id}/bundle{?environment_id}",
    read: (cp, v) => cp.bundle(required(v, "workflow_id"), required(v, "environment_id")),
  },
  {
    name: "topology",
    description: "Workflow topology (projected from bundle.topology).",
    ledgerUri: "typeflux://{project}/workflows/{workflow_id}/topology",
    template: "typeflux://{project}/workflows/{workflow_id}/topology{?environment_id}",
    read: (cp, v) => cp.topology(required(v, "workflow_id"), required(v, "environment_id")),
  },
  {
    name: "catalog",
    description: "Resolved activity catalog.",
    ledgerUri: "typeflux://{project}/workflows/{workflow_id}/catalog",
    template: "typeflux://{project}/workflows/{workflow_id}/catalog{?environment_id}",
    read: (cp, v) => cp.catalog(required(v, "workflow_id"), required(v, "environment_id")),
  },
  {
    name: "connections",
    description: "External connection reachability.",
    ledgerUri: "typeflux://{project}/workflows/{workflow_id}/connections",
    template: "typeflux://{project}/workflows/{workflow_id}/connections{?environment_id}",
    read: (cp, v) => cp.connections(required(v, "workflow_id"), required(v, "environment_id")),
  },
  {
    name: "prompt-status",
    description: "Prompt registry drift status.",
    ledgerUri: "typeflux://{project}/workflows/{workflow_id}/prompt-status",
    template: "typeflux://{project}/workflows/{workflow_id}/prompt-status{?environment_id}",
    read: (cp, v) => cp.promptStatus(required(v, "workflow_id"), required(v, "environment_id")),
  },
  {
    name: "versions",
    description: "Workflow drain/version status.",
    ledgerUri: "typeflux://{project}/workflows/{workflow_id}/versions",
    template: "typeflux://{project}/workflows/{workflow_id}/versions{?environment_id}",
    read: (cp, v) => cp.versions(required(v, "workflow_id"), required(v, "environment_id")),
  },
  {
    name: "workers",
    description: "Task-queue worker reachability. To filter by task_queue, use the get_workers tool.",
    ledgerUri: "typeflux://{project}/workflows/{workflow_id}/workers",
    template: "typeflux://{project}/workflows/{workflow_id}/workers{?environment_id}",
    read: (cp, v) => cp.workers(required(v, "workflow_id"), required(v, "environment_id")),
  },
  {
    name: "executions",
    description: "Recent executions of the workflow. To bound by limit, use the list_executions tool.",
    ledgerUri: "typeflux://{project}/workflows/{workflow_id}/executions",
    template: "typeflux://{project}/workflows/{workflow_id}/executions{?environment_id}",
    read: (cp, v) => cp.executions(required(v, "workflow_id"), required(v, "environment_id")),
  },
  {
    name: "status",
    description: "Live status of one execution.",
    ledgerUri: "typeflux://{project}/workflows/{workflow_id}/status",
    template: "typeflux://{project}/workflows/{workflow_id}/status{?environment_id,execution_id}",
    read: (cp, v) => cp.status(required(v, "workflow_id"), required(v, "environment_id"), required(v, "execution_id")),
  },
  {
    name: "correlation",
    description: "Run correlation (parent/child + observability ids).",
    ledgerUri: "typeflux://{project}/workflows/{workflow_id}/correlation",
    template: "typeflux://{project}/workflows/{workflow_id}/correlation{?environment_id,execution_id}",
    read: (cp, v) => cp.correlation(required(v, "workflow_id"), required(v, "environment_id"), required(v, "execution_id")),
  },
  {
    name: "deployment",
    description: "One deployment plan.",
    ledgerUri: "typeflux://{project}/deployments/{plan_id}",
    template: "typeflux://{project}/deployments/{plan_id}",
    read: (cp, v) => cp.deployment(required(v, "plan_id")),
  },
];

/**
 * Extract the RFC6570 variable names declared in a URI template, e.g.
 * `typeflux://{project}/workflows/{workflow_id}/status{?environment_id,execution_id}` →
 * [project, workflow_id, environment_id, execution_id]. Used to attach an ID completer to each
 * template variable the design §4 completions cover.
 */
function templateVariables(template: string): string[] {
  const names: string[] = [];
  for (const match of template.matchAll(/\{[?&#/.;]?([^}]+)\}/g)) {
    for (const name of match[1]!.split(",")) names.push(name.trim());
  }
  return names;
}

/**
 * Build the `complete` map for a resource template: for every template variable the design §4
 * completions back (workflow_id / environment_id / policy_id / project / plan_id), wire the matching
 * ID completer so the client can autocomplete that URI segment (§4 "wire completion for resource-
 * template variables"). Variables with no completer (execution_id, kind, profile_id) are left out.
 */
function completeMapFor(
  template: string,
  completers: Completers,
): Record<string, Completers["workflow_id"]> | undefined {
  const map: Record<string, Completers["workflow_id"]> = {};
  for (const variable of templateVariables(template)) {
    const completer = completers.forVariable(variable);
    if (completer) map[variable] = completer;
  }
  return Object.keys(map).length > 0 ? map : undefined;
}

/** The project registry listing — project-agnostic, so a concrete (non-templated) live resource. */
const PROJECTS_URI = "typeflux://projects";

/** The registered live resource ledger URIs, for the conformance test (ledger vs registration). */
export function liveResourceLedgerUris(): string[] {
  return [PROJECTS_URI, ...LIVE_RESOURCES.map((r) => r.ledgerUri)];
}

/**
 * Register the live control-plane read resources against the lazy backend. Returns the handles so
 * the server can disable them when the caller lacks `inspect` (§9 degrade).
 */
export function registerLiveResources(
  server: McpServer,
  getBackend: GetBackend,
  completers: Completers,
): Array<RegisteredResource | RegisteredResourceTemplate> {
  const handles: Array<RegisteredResource | RegisteredResourceTemplate> = [];

  handles.push(
    server.registerResource(
      "typeflux-projects",
      PROJECTS_URI,
      { title: "projects", description: "The project registry.", mimeType: "application/json" },
      async (uri) => {
        try {
          return jsonContents(uri.href, await (await getBackend()).controlPlane.projects());
        } catch (error) {
          return errorContents(uri.href, error);
        }
      },
    ),
  );

  for (const resource of LIVE_RESOURCES) {
    // §4: attach ID completers to the template's completable variables (project, workflow_id, ...).
    const complete = completeMapFor(resource.template, completers);
    handles.push(
      server.registerResource(
        `typeflux-${resource.name}`,
        new ResourceTemplate(resource.template, { list: undefined, ...(complete ? { complete } : {}) }),
        { title: resource.name, description: resource.description, mimeType: "application/json" },
        async (uri, variables) => {
          try {
            const backend = await getBackend();
            const project = one(variables.project as string | string[]) ?? "";
            const cp = backend.scopedControlPlane(project);
            return jsonContents(uri.href, await resource.read(cp, variables));
          } catch (error) {
            return errorContents(uri.href, error);
          }
        },
      ),
    );
  }

  return handles;
}
