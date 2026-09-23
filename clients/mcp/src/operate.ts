/**
 * MCP operate tools (#326 Phase 1; design §6.3). The mutating verb tier: start / status / review /
 * cancel / repin / refresh. Unlike the read tools these are:
 *
 *   - CONFIRMATION-HINTED (§9). The four writes carry `readOnlyHint: false` so the MCP client
 *     enforces human-in-the-loop confirmation before firing; `cancel` is `destructiveHint: true`,
 *     `repin`/`refresh` are `idempotentHint: true`. `get_status` is the one operate-tier tool that
 *     is a pure read — the design itself calls it "an inspect read" (`trace=false`, never writes
 *     audit) — so it stays `readOnlyHint: true`; marking it non-read-only would falsely make the
 *     client gate a read behind a confirmation.
 *   - CAPABILITY-GATED (§9). Each tool declares a `/meta`-capability predicate; the server REMOVES
 *     the tool when the caller doesn't satisfy it, so an agent never offers an action it will be
 *     denied (start→can_start, review→can_review, cancel→can_cancel, refresh→can_refresh_project,
 *     repin→can_refresh_project AND can_resolve — repin's CP handler requires resolvability, so a
 *     resolve-less token would see repin advertised yet always 501). A token that later 403s at call
 *     time still surfaces the honest structured error.
 *
 * POLICY-HASH scope (§9): `start` is PREVIEW-THEN-COMMIT so the human confirms the resolved policy
 * hash under MCP's confirm-before-handler model (see runStart). review/cancel act on an
 * already-running, already-pinned execution, so their §6.3 signatures omit the hash; we mirror that
 * and pass `policy_ids: []`.
 *
 * ENVIRONMENT_ID (contract-vs-design note): the design's abbreviated signatures write
 * `get_status(workflow_id, execution_id)` / `submit_review(workflow_id, execution_id, …)` /
 * `cancel_workflow(workflow_id, execution_id, …)`, but the normative contract REQUIRES
 * `environment_id` on all of them (the API 422s without it and there is no way to derive it from a
 * workflow+execution). Contract-first wins (design §2): `environment_id` is a required input on
 * every operate tool.
 */

import { z } from "zod";
import type { McpServer, RegisteredTool } from "@modelcontextprotocol/sdk/server/mcp.js";

import type { Backend } from "./backend.js";
import type { Capabilities, StartRequest } from "./control-plane/client.js";
import { ApiRequestError, describeError, toToolError } from "./control-plane/errors.js";
import { buildElicitationPlan, type JsonSchema, validateInput } from "./schema-validate.js";

type GetBackend = () => Promise<Backend>;

/**
 * Whether the caller's `/meta` capabilities permit an operate tool (§9). A predicate rather than a
 * single flag because some tools need MORE than one capability: `repin` requires BOTH
 * `can_refresh_project` (the `project.refresh` permission it carries) AND `can_resolve` (the CP's
 * repin handler calls `requireResolvable`, so a `can_refresh_project:true` / `can_resolve:false`
 * token would see repin advertised yet always get a 501 — a capability-gating contract break).
 * `capabilities` is undefined only in the degrade path, where every operate tool is removed anyway.
 */
export type CapabilityGate = (capabilities: Capabilities | undefined) => boolean;

/** A registered operate tool paired with the gate that governs its visibility (§9). */
export interface OperateHandle {
  name: string;
  requires: CapabilityGate;
  tool: RegisteredTool;
}

/** get_status is an inspect read — present whenever the backend is not degraded (handled in prepare). */
const REQUIRES_INSPECT: CapabilityGate = () => true;

const looseObject = z.record(z.string(), z.unknown());

/** A tool result: text JSON + structured content on success, or `isError` + text on failure. */
type ToolResult = {
  content: { type: "text"; text: string }[];
  structuredContent?: Record<string, unknown>;
  isError?: true;
};

function ok(structured: Record<string, unknown>): ToolResult {
  return {
    content: [{ type: "text" as const, text: JSON.stringify(structured, null, 2) }],
    structuredContent: structured,
  };
}

const str = (args: Record<string, unknown>, key: string): string => String(args[key]);
const optStr = (args: Record<string, unknown>, key: string): string | undefined =>
  args[key] === undefined ? undefined : String(args[key]);
const optArr = (args: Record<string, unknown>, key: string): string[] | undefined =>
  Array.isArray(args[key]) ? (args[key] as string[]) : undefined;

/** A structured tool error built from a status + code, mirroring a control-plane failure (§9). */
function structuredFailure(status: number, code: string, message: string) {
  return toToolError(new ApiRequestError(describeError(status, { error: code, message })));
}

const WF_ENV_EXEC = {
  workflow_id: z.string(),
  environment_id: z.string(),
  execution_id: z.string(),
} as const;

/**
 * Resolve the `start` input on the COMMIT path: validate locally and, if a primitive field is
 * missing/mistyped AND the client advertises elicitation, ELICIT it against the (sanitized) schema.
 * Returns the ready input, or a structured tool error the caller returns verbatim.
 *
 * A client with NO elicitation capability (or a non-elicitable / non-object input) gets a structured
 * 422 telling it to pass the full `input` object — NOT an `elicitInput` throw surfaced as a bogus
 * `BackendUnavailable`.
 */
async function resolveCommitInput(
  server: McpServer,
  inputSchema: JsonSchema | undefined,
  rawInput: unknown,
): Promise<{ status: "ok"; input: Record<string, unknown> } | { status: "error"; result: ToolResult }> {
  let input: unknown = rawInput ?? {};
  const validation = validateInput(inputSchema, input);
  if (validation.notAnObject) {
    return { status: "error", result: structuredFailure(422, "InvalidRequest", "input must be a JSON object matching the workflow's input schema") };
  }
  if (validation.ok || inputSchema === undefined) return { status: "ok", input: input as Record<string, unknown> };

  const fields = [...validation.missingRequired, ...validation.typeErrors.map((e) => e.field)];
  const plan = buildElicitationPlan(inputSchema, fields);
  const elicitable = plan.nonElicitable.length === 0 && Object.keys(plan.requestedSchema.properties).length > 0;
  const supportsElicitation = server.server.getClientCapabilities()?.elicitation !== undefined;
  if (!elicitable || !supportsElicitation) {
    const detail = plan.nonElicitable.length > 0 ? plan.nonElicitable.join(", ") : fields.join(", ");
    return {
      status: "error",
      result: structuredFailure(422, "InvalidRequest", `input is incomplete or invalid — pass the full 'input' object (fields: ${detail}).`),
    };
  }

  let elicited;
  try {
    elicited = await server.server.elicitInput({
      message: "Provide the workflow input values requested below before it starts.",
      // The plan's properties are structurally the SDK's flat primitive schema (type + neutral
      // title + optional enum); cast past the SDK's stricter literal typing.
      requestedSchema: plan.requestedSchema as never,
    });
  } catch {
    return {
      status: "error",
      result: structuredFailure(422, "InvalidRequest", "input is incomplete and interactive elicitation is unavailable — pass the full 'input' object."),
    };
  }
  if (elicited.action !== "accept" || elicited.content === undefined) {
    return { status: "error", result: structuredFailure(422, "ElicitationDeclined", "the required workflow input was not provided; start was not dispatched") };
  }
  input = { ...(input as Record<string, unknown>), ...elicited.content };
  const revalidation = validateInput(inputSchema, input);
  if (!revalidation.ok) {
    const problems = [
      ...revalidation.missingRequired.map((f) => `${f} (missing)`),
      ...revalidation.typeErrors.map((e) => `${e.field} (expected ${e.expected})`),
    ];
    return {
      status: "error",
      result: structuredFailure(422, "InvalidRequest", `input is still invalid after elicitation: ${problems.join(", ")}. Pass the full 'input' object.`),
    };
  }
  return { status: "ok", input: input as Record<string, unknown> };
}

/**
 * The `start` flow (design §6.3 / §9) as PREVIEW-THEN-COMMIT — the only shape that gives the human a
 * real confirmation of the policy hash under MCP's confirm-before-handler model (the client confirms
 * the tool ARGS before this handler runs, so the server can't surface a self-resolved hash after the
 * fact; a self-injected hash from the same bundle read it fires with could never mismatch, voiding
 * the drift guard).
 *
 *   - `expected_policy_hash` OMITTED → PREVIEW, no write. Resolve the bundle and RETURN the resolved
 *     policy hash, the applied policy ids, the versioned workflow type it would register under, and a
 *     local validation report for `input`. The agent surfaces the hash to the human, then re-calls
 *     start_workflow WITH `expected_policy_hash` set to it (and the full input) to commit.
 *   - `expected_policy_hash` PRESENT → COMMIT. Validate/elicit input, forward the caller's hash
 *     VERBATIM (never a re-derived one) so if the pinned policy drifted between preview and commit
 *     the control plane rejects it (422/409) before any Temporal connection.
 */
async function runStart(
  server: McpServer,
  backend: Backend,
  args: Record<string, unknown>,
): Promise<ToolResult> {
  const workflowId = str(args, "workflow_id");
  const environmentId = str(args, "environment_id");
  const executionId = str(args, "execution_id");
  const policyIds = optArr(args, "policy_ids");
  const rawInput = args["input"] ?? {};
  const cp = backend.controlPlane;

  // Resolve for the input schema, the resolved policy hash, and the versioned workflow type. A
  // resolution failure (501/404/503) is the honest reason start can't proceed — surface it as-is.
  let inputSchema: JsonSchema | undefined;
  let resolvedHash: string | undefined;
  let workflowType: string | undefined;
  try {
    const bundle = await cp.bundle(workflowId, environmentId, { policyIds });
    inputSchema = (bundle.workflow?.input_schema ?? undefined) as JsonSchema | undefined;
    resolvedHash = bundle.policy?.policy_hash ?? undefined;
    workflowType = bundle.workflow?.workflow_type ?? undefined;
  } catch (error) {
    return toToolError(error);
  }

  const callerHash = optStr(args, "expected_policy_hash");

  // PREVIEW — no start, no elicitation (the preview is a resolve+report the agent shows the human).
  if (callerHash === undefined) {
    const validation = validateInput(inputSchema, rawInput);
    const inputValid = validation.ok && !validation.notAnObject;
    return ok({
      preview: true,
      resolved_policy_hash: resolvedHash ?? null,
      applied_policy_ids: policyIds ?? [],
      workflow_type: workflowType ?? null,
      input: rawInput as Record<string, unknown>,
      input_valid: inputValid,
      validation: inputValid
        ? null
        : { not_an_object: validation.notAnObject, missing_required: validation.missingRequired, type_errors: validation.typeErrors },
      instruction: inputValid
        ? "Confirm resolved_policy_hash with the user, then re-call start_workflow with expected_policy_hash set to it (and this input) to start."
        : "input is incomplete/invalid — provide the full 'input' object, then re-call start_workflow with expected_policy_hash set to resolved_policy_hash to start.",
    });
  }

  // COMMIT — validate/elicit input, then start with the caller's hash forwarded verbatim.
  const resolved = await resolveCommitInput(server, inputSchema, rawInput);
  if (resolved.status === "error") return resolved.result;

  const body: StartRequest = {
    environment_id: environmentId,
    execution_id: executionId,
    input: resolved.input,
    policy_ids: policyIds ?? [], // required by the contract type; [] == the API default
    ...(optStr(args, "task_queue") ? { task_queue: optStr(args, "task_queue")! } : {}),
    expected_policy_hash: callerHash,
  };
  try {
    const receipt = await cp.start(workflowId, body);
    return ok({ started: true, receipt: receipt as unknown as Record<string, unknown>, policy_hash: callerHash });
  } catch (error) {
    return toToolError(error);
  }
}

interface OperateToolDef {
  name: string;
  title: string;
  description: string;
  requires: CapabilityGate;
  inputSchema: z.ZodRawShape;
  outputSchema: z.ZodRawShape;
  annotations: {
    readOnlyHint: boolean;
    destructiveHint?: boolean;
    idempotentHint?: boolean;
    openWorldHint: false;
  };
  /** `server` is passed so `start` can elicit; most tools ignore it. Returns the full tool result. */
  run: (server: McpServer, backend: Backend, args: Record<string, unknown>) => Promise<ToolResult>;
}

const OPERATE_TOOLS: OperateToolDef[] = [
  {
    name: "start_workflow",
    title: "Start workflow",
    description:
      "Start one execution of a resolved workflow version, PREVIEW-THEN-COMMIT. Call WITHOUT expected_policy_hash to PREVIEW: no run starts; you get resolved_policy_hash, applied_policy_ids, the versioned workflow_type, and a validation report for 'input'. Surface resolved_policy_hash to the user, then re-call WITH expected_policy_hash set to it (and the full 'input') to COMMIT: input is validated (missing fields elicited if the client supports it), the hash is forwarded verbatim so a policy that drifted since preview is rejected before any Temporal connection, and the WorkflowStartReceipt is returned. Requires user confirmation.",
    requires: (caps) => caps?.can_start === true,
    inputSchema: {
      workflow_id: z.string(),
      environment_id: z.string(),
      execution_id: z.string(),
      input: looseObject.optional(),
      task_queue: z.string().optional(),
      policy_ids: z.array(z.string()).optional(),
      expected_policy_hash: z.string().optional(),
    },
    outputSchema: {
      preview: z.boolean().optional(),
      started: z.boolean().optional(),
      resolved_policy_hash: z.string().nullable().optional(),
      applied_policy_ids: z.array(z.string()).optional(),
      workflow_type: z.string().nullable().optional(),
      input: looseObject.optional(),
      input_valid: z.boolean().optional(),
      validation: looseObject.nullable().optional(),
      instruction: z.string().optional(),
      receipt: looseObject.optional(),
      policy_hash: z.string().nullable().optional(),
    },
    annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: false, openWorldHint: false },
    run: (server, backend, args) => runStart(server, backend, args),
  },
  {
    name: "get_status",
    title: "Get run status",
    description:
      "Return a running execution's lifecycle snapshot, the review decisions valid for its version, and its runtime pin. A read (trace=false) — it never writes an audit event. Subscribe to typeflux://{project}/workflows/{workflow_id}/status?environment_id=&execution_id= to be notified of changes.",
    requires: REQUIRES_INSPECT,
    inputSchema: { ...WF_ENV_EXEC },
    outputSchema: { status: looseObject },
    annotations: { readOnlyHint: true, openWorldHint: false },
    run: async (_server, backend, args) =>
      ok({
        status: (await backend.controlPlane.status(
          str(args, "workflow_id"),
          str(args, "environment_id"),
          str(args, "execution_id"),
        )) as unknown as Record<string, unknown>,
      }),
  },
  {
    name: "submit_review",
    title: "Submit review decision",
    description:
      "Submit a human review decision for a paused execution. Only decisions valid for that execution's version are accepted (read them from get_status.valid_user_decisions). reviewer/notes ride the review signal and persist in history. Requires user confirmation.",
    requires: (caps) => caps?.can_review === true,
    inputSchema: {
      ...WF_ENV_EXEC,
      user_decision: z.string(),
      reviewer: z.string().optional(),
      notes: z.string().optional(),
      gate: z.string().optional(),
    },
    outputSchema: { submitted: z.boolean() },
    annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: false, openWorldHint: false },
    run: async (_server, backend, args) => {
      await backend.controlPlane.submitReview(str(args, "workflow_id"), {
        environment_id: str(args, "environment_id"),
        execution_id: str(args, "execution_id"),
        policy_ids: [],
        command: {
          user_decision: str(args, "user_decision"),
          ...(optStr(args, "reviewer") ? { reviewer: optStr(args, "reviewer") } : {}),
          ...(optStr(args, "notes") ? { notes: optStr(args, "notes") } : {}),
          ...(optStr(args, "gate") ? { gate: optStr(args, "gate") } : {}),
        },
      });
      return ok({ submitted: true });
    },
  },
  {
    name: "cancel_workflow",
    title: "Cancel workflow",
    description:
      "Request cancellation of a running execution. The reason is echoed to inspect callers via status. Destructive — requires user confirmation.",
    requires: (caps) => caps?.can_cancel === true,
    inputSchema: { ...WF_ENV_EXEC, reason: z.string().optional() },
    outputSchema: { cancelled: z.boolean() },
    annotations: { readOnlyHint: false, destructiveHint: true, idempotentHint: false, openWorldHint: false },
    run: async (_server, backend, args) => {
      await backend.controlPlane.cancel(str(args, "workflow_id"), {
        environment_id: str(args, "environment_id"),
        execution_id: str(args, "execution_id"),
        policy_ids: [],
        ...(optStr(args, "reason") ? { reason: optStr(args, "reason") } : {}),
      });
      return ok({ cancelled: true });
    },
  },
  {
    name: "repin_operations",
    title: "Repin operations runtime",
    description:
      "Drop the pinned operations runtime for a workflow so it re-pins from the current YAML on the next mutating call — no serve restart. Idempotent. Requires user confirmation.",
    requires: (caps) => caps?.can_refresh_project === true && caps?.can_resolve === true,
    inputSchema: { workflow_id: z.string(), environment_id: z.string() },
    outputSchema: { result: looseObject },
    annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: true, openWorldHint: false },
    run: async (_server, backend, args) =>
      ok({
        result: (await backend.controlPlane.repin(
          str(args, "workflow_id"),
          str(args, "environment_id"),
        )) as unknown as Record<string, unknown>,
      }),
  },
  {
    name: "refresh_project",
    title: "Refresh project source",
    description:
      "Re-fetch a Git-sourced project clone (a local project reports refreshed:false). Idempotent. Requires user confirmation.",
    requires: (caps) => caps?.can_refresh_project === true,
    inputSchema: { project: z.string() },
    outputSchema: { result: looseObject },
    annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: true, openWorldHint: false },
    run: async (_server, backend, args) =>
      ok({ result: (await backend.controlPlane.refresh(str(args, "project"))) as unknown as Record<string, unknown> }),
  },
];

/**
 * Register every operate tool. Returns handles paired with the capability that gates each — the
 * server removes a tool whose capability the caller lacks (§9), so tools/list only advertises what
 * the token can actually perform.
 */
export function registerOperateTools(server: McpServer, getBackend: GetBackend): OperateHandle[] {
  return OPERATE_TOOLS.map((def) => {
    const tool = server.registerTool(
      def.name,
      {
        title: def.title,
        description: def.description,
        inputSchema: def.inputSchema,
        outputSchema: def.outputSchema,
        annotations: { title: def.title, ...def.annotations },
      },
      async (args: Record<string, unknown>) => {
        try {
          const backend = await getBackend();
          // Call-time capability re-check (#785): with retry-after-cooldown, this tool can have
          // been advertised BEFORE capabilities were known (prepare bailed on a transient
          // failure), and the resolution that just succeeded may not grant it. The §9 gate has
          // now removed it for future lists; abort this stale in-flight call too rather than
          // relying only on the control plane's own denial.
          if (backend.degraded || !def.requires(backend.capabilities)) {
            // A capability denial, not an outage: use the wire 403 shape so callers see the
            // permission signal instead of diagnosing a BackendUnavailable.
            return toToolError(
              new ApiRequestError({
                status: 403,
                code: "Forbidden",
                message: `${def.name} is not available: the control plane does not grant its required capability`,
              }),
            );
          }
          return await def.run(server, backend, args ?? {});
        } catch (error) {
          return toToolError(error);
        }
      },
    );
    return { name: def.name, requires: def.requires, tool };
  });
}

/** The registered operate-tool names, in registration order (for the coverage conformance test). */
export function operateToolNames(): string[] {
  return OPERATE_TOOLS.map((def) => def.name);
}
