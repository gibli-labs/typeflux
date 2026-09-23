/**
 * MCP read tools (#326 Phase 0; design §6.1/6.2 — read/inspect only). Every tool:
 *   - is marked `readOnlyHint: true`, `openWorldHint: false` (a bounded, non-mutating read);
 *   - declares a structured `outputSchema` (a loose wrapper over the contract's response shape, so
 *     real payloads never fail structured-content validation);
 *   - maps a control-plane failure to a STRUCTURED tool error (design §9), preserving status+code.
 *
 * Phase 0 exposes NO operate tools (start/review/cancel/repin/migrate/refresh — Phase 1), no
 * scaffold, and no prompts/completions/elicitation. The wrapped operations are the ledger's tool
 * entries (coverage.ts); the conformance test asserts the surface matches the contract.
 */

import { z } from "zod";
import type { McpServer, RegisteredTool } from "@modelcontextprotocol/sdk/server/mcp.js";

import type { Backend } from "./backend.js";
import { toToolError } from "./control-plane/errors.js";

type GetBackend = () => Promise<Backend>;

const READ_ANNOTATIONS = { readOnlyHint: true, openWorldHint: false } as const;

/** A loose record — the API's JSON, unconstrained so a real payload always validates. */
const looseObject = z.record(z.string(), z.unknown());
const looseArray = z.array(z.unknown());

/** Build a successful tool result: text JSON (for plain clients) + structured content. */
function ok(structured: Record<string, unknown>) {
  return {
    content: [{ type: "text" as const, text: JSON.stringify(structured, null, 2) }],
    structuredContent: structured,
  };
}

interface ReadTool {
  name: string;
  title: string;
  description: string;
  inputSchema: z.ZodRawShape;
  outputSchema: z.ZodRawShape;
  run: (backend: Backend, args: Record<string, unknown>) => Promise<Record<string, unknown>>;
}

const str = (args: Record<string, unknown>, key: string): string => String(args[key]);
const optStr = (args: Record<string, unknown>, key: string): string | undefined =>
  args[key] === undefined ? undefined : String(args[key]);
const optArr = (args: Record<string, unknown>, key: string): string[] | undefined =>
  Array.isArray(args[key]) ? (args[key] as string[]) : undefined;
const optNum = (args: Record<string, unknown>, key: string): number | undefined =>
  args[key] === undefined ? undefined : Number(args[key]);

const WORKFLOW_ENV = { workflow_id: z.string(), environment_id: z.string() } as const;

const READ_TOOLS: ReadTool[] = [
  {
    name: "list_workflows",
    title: "List workflows",
    description: "List the workflows declared in the project.",
    inputSchema: {},
    outputSchema: { workflows: looseArray },
    run: async (b) => ({ workflows: await b.controlPlane.workflows() }),
  },
  {
    name: "list_environments",
    title: "List environments",
    description: "List the project's declared environments.",
    inputSchema: {},
    outputSchema: { environments: looseArray },
    run: async (b) => ({ environments: await b.controlPlane.environments() }),
  },
  {
    name: "list_policies",
    title: "List policies",
    description: "List the project's declared policies.",
    inputSchema: {},
    outputSchema: { policies: looseArray },
    run: async (b) => ({ policies: await b.controlPlane.policies() }),
  },
  {
    name: "list_annotations",
    title: "List insight annotations",
    description:
      "List the project's in-repo insight acknowledgements (`.typeflux/annotations.yaml`): each " +
      "acknowledged/suppressed console insight with its reason, optional tracking issue, and optional " +
      "expiry. Authored in the repo and PR-reviewed — read-only here. Empty when no file is present; a " +
      "malformed file is reported as a validation issue by validate_project instead (#733).",
    inputSchema: {},
    outputSchema: { annotations: looseArray },
    run: async (b) => ({ annotations: await b.controlPlane.annotations() }),
  },
  {
    name: "list_projects",
    title: "List projects",
    description: "List the projects in the control plane's registry.",
    inputSchema: {},
    outputSchema: { projects: looseArray },
    run: async (b) => ({ projects: await b.controlPlane.projects() }),
  },
  {
    name: "validate_project",
    title: "Validate project",
    description:
      "Run whole-project validation and return the structured issue report. Optionally scope to an environment, to candidate workflows, and/or to candidate policies (preview 'is this valid under policy X').",
    inputSchema: {
      environment_id: z.string().optional(),
      workflow_id: z.array(z.string()).optional(),
      policy_id: z.array(z.string()).optional(),
    },
    outputSchema: { report: looseObject },
    run: async (b, a) => ({
      report: await b.controlPlane.validate(optStr(a, "environment_id"), {
        workflowIds: optArr(a, "workflow_id"),
        policyIds: optArr(a, "policy_id"),
      }),
    }),
  },
  {
    name: "get_bundle",
    title: "Get resolved bundle",
    description:
      "Resolve a workflow into its full bundle (graph, runtime, policy, secrets, risk tier). Optionally preview under candidate policies and/or a specific deployment image.",
    inputSchema: {
      ...WORKFLOW_ENV,
      policy_id: z.array(z.string()).optional(),
      deployment_image: z.string().optional(),
    },
    outputSchema: { bundle: looseObject },
    run: async (b, a) => ({
      bundle: await b.controlPlane.bundle(str(a, "workflow_id"), str(a, "environment_id"), {
        policyIds: optArr(a, "policy_id"),
        deploymentImage: optStr(a, "deployment_image"),
      }),
    }),
  },
  {
    name: "get_catalog",
    title: "Get activity catalog",
    description: "Return the resolved activity catalog (activities and their IO schemas) for a workflow.",
    inputSchema: { ...WORKFLOW_ENV },
    outputSchema: { catalog: looseObject },
    run: async (b, a) => ({ catalog: await b.controlPlane.catalog(str(a, "workflow_id"), str(a, "environment_id")) }),
  },
  {
    name: "get_prompt_status",
    title: "Get prompt status",
    description: "Return prompt-registry drift status for a workflow.",
    inputSchema: { ...WORKFLOW_ENV },
    outputSchema: { prompt_status: looseObject },
    run: async (b, a) => ({ prompt_status: await b.controlPlane.promptStatus(str(a, "workflow_id"), str(a, "environment_id")) }),
  },
  {
    name: "get_connections",
    title: "Get connections",
    description: "Return external-connection reachability for a workflow (Temporal, registry, ...).",
    inputSchema: { ...WORKFLOW_ENV },
    outputSchema: { connections: looseObject },
    run: async (b, a) => ({ connections: await b.controlPlane.connections(str(a, "workflow_id"), str(a, "environment_id")) }),
  },
  {
    name: "get_workers",
    title: "Get workers",
    description: "Return task-queue worker reachability for a workflow.",
    inputSchema: { ...WORKFLOW_ENV, task_queue: z.string().optional() },
    outputSchema: { workers: looseObject },
    run: async (b, a) => ({
      workers: await b.controlPlane.workers(str(a, "workflow_id"), str(a, "environment_id"), optStr(a, "task_queue")),
    }),
  },
  {
    name: "list_executions",
    title: "List executions",
    description: "List recent executions of a workflow (optionally bounded by limit).",
    inputSchema: { ...WORKFLOW_ENV, limit: z.number().int().positive().optional() },
    outputSchema: { executions: looseObject },
    run: async (b, a) => ({
      executions: await b.controlPlane.executions(str(a, "workflow_id"), str(a, "environment_id"), optNum(a, "limit")),
    }),
  },
  {
    name: "get_correlation",
    title: "Get run correlation",
    description: "Return the run correlation (parent/child + observability ids) for one execution.",
    inputSchema: { ...WORKFLOW_ENV, execution_id: z.string() },
    outputSchema: { correlation: looseObject },
    run: async (b, a) => ({
      correlation: await b.controlPlane.correlation(str(a, "workflow_id"), str(a, "environment_id"), str(a, "execution_id")),
    }),
  },
  {
    name: "get_versions",
    title: "Get versions",
    description: "Return workflow drain/version status.",
    inputSchema: { ...WORKFLOW_ENV },
    outputSchema: { versions: looseObject },
    run: async (b, a) => ({ versions: await b.controlPlane.versions(str(a, "workflow_id"), str(a, "environment_id")) }),
  },
  {
    name: "list_deployments",
    title: "List deployments",
    description: "List the control plane's deployment plans.",
    inputSchema: {},
    outputSchema: { deployments: looseArray },
    run: async (b) => ({ deployments: await b.controlPlane.deployments() }),
  },
  {
    name: "list_enforcement_events",
    title: "List enforcement events",
    description:
      "List policy enforcement events (admission rejections + runtime moderation blocks) for an " +
      "environment, read at request time — check `partial.langfuse` for degraded runtime reads (#723).",
    inputSchema: {
      environment_id: z.string(),
      workflow_id: z.array(z.string()).optional(),
      policy_id: z.array(z.string()).optional(),
      verdict: z.string().optional(),
      since: z.string().optional(),
      until: z.string().optional(),
      limit: z.number().int().positive().optional(),
      cursor: z.string().optional(),
    },
    outputSchema: { enforcement_events: looseObject },
    run: async (b, a) => ({
      enforcement_events: await b.controlPlane.enforcementEvents(str(a, "environment_id"), {
        workflowIds: a.workflow_id as string[] | undefined,
        policyIds: a.policy_id as string[] | undefined,
        verdict: optStr(a, "verdict"),
        since: optStr(a, "since"),
        until: optStr(a, "until"),
        limit: optNum(a, "limit"),
        cursor: optStr(a, "cursor"),
      }),
    }),
  },
  {
    name: "get_github_provenance",
    title: "Get GitHub provenance",
    description:
      "Show how the code the control plane serves relates to GitHub: HEAD-vs-served drift " +
      "(the served project changed in GitHub but the CP hasn't picked it up) and each approved " +
      "deployment plan's approving pull request. Read at request time — check `partial.github` " +
      "(`ok`/`unreachable`/`not_configured`/`rate_limited`) for degraded or unconfigured GitHub reads (#727).",
    inputSchema: {},
    outputSchema: { github_provenance: looseObject },
    run: async (b) => ({ github_provenance: await b.controlPlane.githubProvenance() }),
  },
  {
    name: "get_deployment",
    title: "Get deployment",
    description: "Return one deployment plan by id.",
    inputSchema: { plan_id: z.string() },
    outputSchema: { deployment: looseObject },
    run: async (b, a) => ({ deployment: await b.controlPlane.deployment(str(a, "plan_id")) }),
  },
];

/**
 * Register every Phase-0 read tool against the lazy backend. Returns the handles so the server can
 * disable them when the caller lacks `inspect` (§9 degrade) — every Phase-0 tool is a live read.
 */
export function registerReadTools(server: McpServer, getBackend: GetBackend): RegisteredTool[] {
  return READ_TOOLS.map((tool) =>
    server.registerTool(
      tool.name,
      {
        title: tool.title,
        description: tool.description,
        inputSchema: tool.inputSchema,
        outputSchema: tool.outputSchema,
        annotations: { title: tool.title, ...READ_ANNOTATIONS },
      },
      async (args: Record<string, unknown>) => {
        try {
          const backend = await getBackend();
          return ok(await tool.run(backend, args ?? {}));
        } catch (error) {
          return toToolError(error);
        }
      },
    ),
  );
}

/** The registered read-tool names (for tests + the coverage check). */
export function readToolNames(): string[] {
  return READ_TOOLS.map((tool) => tool.name);
}
