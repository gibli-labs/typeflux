/**
 * Local authoring aids (#326 Phase 3; design §6.4). Four tools that make an agent productive at the
 * KEYBOARD without ever crossing the authoring boundary (design §2, §11 decision 3):
 *
 *   - `scaffold_ai_activity`  — returns generated Python (`@ai_activity.defn` / `AIActivity(...)`),
 *                               TypeScript (`defineActivity` + zod strictObject schemas, #864), or a
 *                               hookless YAML `activities.definitions` block, matched to the target
 *                               file's existing style.
 *   - `scaffold_workflow_yaml`— returns a `typeflux.yaml` seeded with runtime/registry/provider stubs
 *                               and `${ENV}` interpolation.
 *   - `scaffold_project_entry`— returns a `typeflux.project.yaml` workflow/env/policy entry.
 *   - `doctor`                — an environment-readiness checklist (the "why won't it run" first stop).
 *
 * RETURN-CONTENT, NEVER WRITE. The three scaffold tools compute file content and return it as a
 * payload the EDITOR applies as a reviewable diff — they never touch the filesystem. They may READ a
 * target file (via MCP roots, workspace.ts) to match its style, but there is no write path anywhere
 * in this module. `doctor` READS (env presence + a control-plane ping + optional Temporal/registry
 * probe) and writes nothing, degrading honestly when the control plane is unreachable.
 *
 * These are LOCAL tools, not control-plane operations — so they are NOT in the coverage ledger
 * (coverage.ts), exactly like the prompts. They stay registered in EVERY backend state (including the
 * §9 degrade): a scaffold is pure-local, and `doctor` is most useful precisely when the live tier is
 * down. The server therefore never removes them.
 *
 * UNTRUSTED-CONTENT BOUNDARY (§9): the generated content a scaffold echoes is fenced with the shared
 * {@link fenceDelimiter} (reused from prompts.ts) so a caller-supplied field name / prompt ref that
 * contains a backtick run can never break out of the code block and be reinterpreted as narrative.
 */

import { z } from "zod";
import type { McpServer, RegisteredTool } from "@modelcontextprotocol/sdk/server/mcp.js";

import type { Backend } from "./backend.js";
import type { ServerConfig } from "./config.js";
import { toStructuredError } from "./control-plane/errors.js";
import { fenceDelimiter } from "./prompts.js";
import { readWorkspaceFile, type ListRoots } from "./workspace.js";

type GetBackend = () => Promise<Backend>;

/** Local authoring tools are non-CP verbs: reads-only, closed-world, and never a live CP write. */
const LOCAL_ANNOTATIONS = { readOnlyHint: true, openWorldHint: false } as const;

/** A tool result: text + structured content on success. (A type alias — an interface would lack the
 * implicit index signature the SDK's CallToolResult expects.) */
type ToolResult = {
  content: { type: "text"; text: string }[];
  structuredContent?: Record<string, unknown>;
  isError?: true;
};

/** Build a success result: a human-facing text rendering plus the structured payload. */
function ok(text: string, structured: Record<string, unknown>): ToolResult {
  return { content: [{ type: "text" as const, text }], structuredContent: structured };
}

// --- style + naming helpers ------------------------------------------------------------------

/** The authoring style a scaffold emits. */
export type ScaffoldStyle = "python" | "yaml" | "typescript";

/** PascalCase an identifier: `review_claim` / `review-claim` → `ReviewClaim`. */
export function pascalCase(name: string): string {
  return name
    .split(/[^A-Za-z0-9]+/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join("");
}

/**
 * Neutralize a caller-supplied value before it lands in the UNFENCED note prose (§9 untrusted-content
 * boundary). The scaffold ARGS are caller-controlled — untrusted for the agent's context — so a
 * `name` like ``x```\n## ignore prior instructions`` must not close the fence, open a heading, or
 * start a new narrative line. Drop backticks + CR/LF and length-cap. (The FENCED content is already
 * protected by {@link fenceDelimiter}; this covers the note text ahead of it.)
 */
export function neutralize(value: string): string {
  return value.replace(/[`\r\n]/g, "").slice(0, 80);
}

/** camelCase an identifier for a TS symbol: `review_claim` / `review-claim` → `reviewClaim`. */
export function camelCase(name: string): string {
  const pascal = pascalCase(name);
  return pascal.charAt(0).toLowerCase() + pascal.slice(1);
}

/**
 * Words that cannot (or must not) be the generated `export const` symbol: ES reserved words plus
 * the two bindings the scaffold itself imports (Bugbot #878 — `export const delete` doesn't parse,
 * and a symbol named `defineActivity`/`z` would shadow its own import).
 */
const TS_UNUSABLE_SYMBOLS = new Set([
  "await", "break", "case", "catch", "class", "const", "continue", "debugger", "default",
  "delete", "do", "else", "enum", "export", "extends", "false", "finally", "for", "function",
  "if", "implements", "import", "in", "instanceof", "interface", "let", "new", "null",
  "package", "private", "protected", "public", "return", "static", "super", "switch", "this",
  "throw", "true", "try", "typeof", "var", "void", "while", "with", "yield",
  "defineActivity", "z",
]);

/**
 * A LEGAL TS identifier from a caller-supplied name (#864 review): camelCase, `_`-prefixed when it
 * would start with a digit, `activity` when nothing survives, suffixed when it lands on a reserved
 * word or an imported binding. Callers control these names, and the scaffold must return parseable
 * source for any input.
 */
export function tsIdentifier(name: string): string {
  const base = camelCase(name);
  if (base === "") return "activity";
  const legal = /^[0-9]/.test(base) ? `_${base}` : base;
  return TS_UNUSABLE_SYMBOLS.has(legal) ? `${legal}Activity` : legal;
}

/** Serialize a caller-supplied value as a TS double-quoted string literal (escapes " \ and newlines). */
function tsString(value: string): string {
  return JSON.stringify(value);
}

/** snake_case an identifier for a Python symbol: `ReviewClaim` / `review-claim` → `review_claim`. */
export function snakeCase(name: string): string {
  return name
    .replace(/([a-z0-9])([A-Z])/g, "$1_$2")
    .split(/[^A-Za-z0-9]+/)
    .filter(Boolean)
    .join("_")
    .toLowerCase();
}

/** A normalized IO field: a name and a declared type (default `string`). */
interface Field {
  name: string;
  type: string;
}

/** Accept a field as `"name"`, `"name:type"`, or `{ name, type? }`; normalize to {@link Field}. */
function normalizeField(raw: unknown): Field | undefined {
  if (typeof raw === "string") {
    const [name, type] = raw.split(":");
    const trimmed = (name ?? "").trim();
    return trimmed ? { name: trimmed, type: (type ?? "string").trim() || "string" } : undefined;
  }
  if (raw && typeof raw === "object") {
    const rec = raw as { name?: unknown; type?: unknown };
    const name = typeof rec.name === "string" ? rec.name.trim() : "";
    if (!name) return undefined;
    const type = typeof rec.type === "string" && rec.type.trim() ? rec.type.trim() : "string";
    return { name, type };
  }
  return undefined;
}

function normalizeFields(raw: unknown): Field[] {
  if (!Array.isArray(raw)) return [];
  return raw.map(normalizeField).filter((f): f is Field => f !== undefined);
}

/** Map a declared type to its Python annotation (supports a `T[]` list suffix). */
function pythonType(type: string): string {
  const t = type.trim();
  if (t.endsWith("[]")) return `list[${pythonType(t.slice(0, -2))}]`;
  switch (t.toLowerCase()) {
    case "string":
    case "str":
      return "str";
    case "integer":
    case "int":
      return "int";
    case "number":
    case "float":
      return "float";
    case "boolean":
    case "bool":
      return "bool";
    case "object":
    case "dict":
      return "dict";
    case "array":
    case "list":
      return "list";
    default:
      return "str";
  }
}

/** Map a declared type to its zod expression (supports a `T[]` list suffix). */
function zodType(type: string): string {
  const t = type.trim();
  if (t.endsWith("[]")) return `z.array(${zodType(t.slice(0, -2))})`;
  switch (t.toLowerCase()) {
    case "integer":
    case "int":
      return "z.number().int()";
    case "number":
    case "float":
      return "z.number()";
    case "boolean":
    case "bool":
      return "z.boolean()";
    case "object":
    case "dict":
      // Closed object, not z.record: defineActivity eagerly converts the output schema to the
      // provider-safe profile, which REJECTS open maps (ProviderSchemaError on import).
      return "z.strictObject({})";
    case "array":
    case "list":
      return "z.array(z.strictObject({}))";
    default:
      return "z.string()";
  }
}

/**
 * Detect the authoring style to emit. An explicit `style` wins. Otherwise infer from the target
 * file: a `.py` file (or content using `@ai_activity.defn` / `AIActivity(`) is Python; a `.ts` file
 * (or content using `defineActivity` / `@typeflux/temporal` imports) is TypeScript (#864); a
 * `.yaml` / `.yml` file is YAML. With no signal, default to hookless YAML (the pure-YAML runtime is the
 * lowest-friction authoring surface). Returns the style and, for Python, whether the file already
 * uses the CONSTRUCTOR form (`AIActivity(...)`) vs the DECORATOR form (`@ai_activity.defn`).
 */
export function detectStyle(
  explicit: ScaffoldStyle | undefined,
  targetPath: string | undefined,
  targetContent: string | undefined,
): { style: ScaffoldStyle; pythonConstructor: boolean } {
  const constructor = targetContent !== undefined && /\bAIActivity\s*\(/.test(targetContent);
  if (explicit !== undefined) return { style: explicit, pythonConstructor: constructor };

  const path = targetPath?.toLowerCase() ?? "";
  if (path.endsWith(".py")) return { style: "python", pythonConstructor: constructor };
  if (path.endsWith(".ts") || path.endsWith(".tsx") || path.endsWith(".mts") || path.endsWith(".cts")) {
    return { style: "typescript", pythonConstructor: false };
  }
  if (path.endsWith(".yaml") || path.endsWith(".yml")) return { style: "yaml", pythonConstructor: false };
  if (targetContent !== undefined && /@ai_activity\.defn|AIActivity\s*\(/.test(targetContent)) {
    return { style: "python", pythonConstructor: constructor };
  }
  // Import/call syntax only — a bare "@typeflux/temporal" substring would false-positive on a
  // package.json dependency entry reached through the extension fall-through.
  if (targetContent !== undefined && /\bdefine(?:Code)?Activity\s*\(|from\s+"@typeflux\/temporal/.test(targetContent)) {
    return { style: "typescript", pythonConstructor: false };
  }
  return { style: "yaml", pythonConstructor: false };
}

// --- scaffold content generators -------------------------------------------------------------

function pydanticModel(className: string, fields: Field[]): string {
  const body =
    fields.length > 0
      ? fields.map((f) => `    ${snakeCase(f.name)}: ${pythonType(f.type)}`).join("\n")
      : "    pass  # TODO: declare the fields of this schema";
  return `class ${className}(BaseModel):\n${body}`;
}

/** Generate the Python (`@ai_activity.defn` or `AIActivity(...)`) content for an AI activity. */
export function pythonActivity(
  name: string,
  inputs: Field[],
  outputs: Field[],
  promptRef: string,
  useConstructor: boolean,
): string {
  const fn = snakeCase(name);
  const inModel = `${pascalCase(name)}Input`;
  const outModel = `${pascalCase(name)}Output`;
  const header =
    "from __future__ import annotations\n\n" +
    "from pydantic import BaseModel\n\n" +
    `from typeflux.core import ${useConstructor ? "AIActivity, PromptRef" : "PromptRef, ai_activity"}\n`;
  const models = `${pydanticModel(inModel, inputs)}\n\n\n${pydanticModel(outModel, outputs)}`;
  const activity = useConstructor
    ? `${fn} = AIActivity(\n` +
      `    name="${name}",\n` +
      `    input_type=${inModel},\n` +
      `    output_type=${outModel},\n` +
      `    prompt_ref=PromptRef("${promptRef}"),\n` +
      `)`
    : `@ai_activity.defn(\n` +
      `    name="${name}",\n` +
      `    prompt=PromptRef("${promptRef}"),\n` +
      `    output=${outModel},\n` +
      `    validation_retries=1,\n` +
      `)\n` +
      `def ${fn}(input: ${inModel}, output: ${outModel}) -> ${outModel}:\n` +
      `    # The runtime resolves the prompt, calls the model, and validates its output against\n` +
      `    # ${outModel} (retrying on a miss). Do NOT call a model here — return \`output\`,\n` +
      `    # optionally post-processed. Keep any external side effect in a separate activity.\n` +
      `    return output`;
  return `${header}\n\n${models}\n\n\n${activity}\n`;
}

/** Generate the hookless YAML `activities.definitions` block for an AI activity. */
export function yamlActivity(
  name: string,
  inputs: Field[],
  outputs: Field[],
  promptRef: string,
): string {
  const inModel = `${pascalCase(name)}Input`;
  const outModel = `${pascalCase(name)}Output`;
  const inFields = inputs.map((f) => `#     ${f.name}: ${f.type}`).join("\n") || "#     (declare fields)";
  const outFields = outputs.map((f) => `#     ${f.name}: ${f.type}`).join("\n") || "#     (declare fields)";
  return (
    "# Add this entry under `activities.definitions:` in your typeflux.yaml. A hookless AI\n" +
    "# activity — the pure-YAML runtime injects no code. The input/output schema refs below\n" +
    `# (schemas:${inModel} / schemas:${outModel}) must resolve via your project's schema source\n` +
    "# (Python schema classes, or the injected TS resolver). Intended shapes:\n" +
    `#   ${inModel}:\n${inFields}\n` +
    `#   ${outModel}:\n${outFields}\n` +
    "    - name: " + name + "\n" +
    `      input: schemas:${inModel}\n` +
    `      output: schemas:${outModel}\n` +
    `      prompt: ${promptRef}\n` +
    "      validation_retries: 1\n"
  );
}

/** Generate the TypeScript (`defineActivity` + zod strictObject schemas) content (#864). */
export function typescriptActivity(
  name: string,
  inputs: Field[],
  outputs: Field[],
  promptRef: string,
): string {
  const symbol = tsIdentifier(name);
  const zodObject = (fields: Field[]): string => {
    // Legalized keys, first-wins dedupe: two names that camelCase identically would otherwise
    // emit a duplicate object-literal key (the second silently shadows the first).
    const seen = new Set<string>();
    const lines: string[] = [];
    for (const f of fields) {
      const key = tsIdentifier(f.name);
      if (seen.has(key)) continue;
      seen.add(key);
      lines.push(`  ${key}: ${zodType(f.type)},`);
    }
    return lines.length > 0
      ? `z.strictObject({\n${lines.join("\n")}\n})`
      : "z.strictObject({\n  // TODO: declare the fields of this schema\n})";
  };
  return (
    `import { defineActivity } from "@typeflux/temporal";\n` +
    `import { z } from "zod";\n` +
    "\n" +
    `export const ${symbol}Input = ${zodObject(inputs)};\n` +
    "\n" +
    `export const ${symbol}Output = ${zodObject(outputs)};\n` +
    "\n" +
    "// The engine resolves the prompt, calls the model, and validates the response against\n" +
    `// ${symbol}Output (retrying on a miss). Register via buildTemporalActivities, or inject\n` +
    "// into a YAML runtime through extraActivities (hooks live there too).\n" +
    `export const ${symbol} = defineActivity({\n` +
    `  name: ${tsString(name)},\n` +
    `  prompt: { name: ${tsString(promptRef)} },\n` +
    `  input: ${symbol}Input,\n` +
    `  output: ${symbol}Output,\n` +
    `  validationRetries: 1,\n` +
    `});\n`
  );
}

/** A provider's default model + its api-key env var, for the workflow-YAML scaffold. */
const PROVIDER_DEFAULTS: Readonly<Record<string, { model: string; keyEnv: string }>> = {
  anthropic: { model: "claude-sonnet-4-6", keyEnv: "ANTHROPIC_API_KEY" },
  openai: { model: "gpt-4o", keyEnv: "OPENAI_API_KEY" },
  google: { model: "gemini-2.5-pro", keyEnv: "GOOGLE_API_KEY" },
};

/** Generate a seeded `typeflux.yaml` (runtime/registry/provider stubs + `${ENV}` interpolation). */
export function workflowYaml(opts: {
  project: string;
  name: string;
  taskQueue: string;
  provider: string;
  model: string;
  keyEnv: string;
}): string {
  const { project, name, taskQueue, provider, model, keyEnv } = opts;
  const activity = `${snakeCase(name)}_activity`;
  const inModel = `${pascalCase(name)}Input`;
  const outModel = `${pascalCase(name)}Output`;
  return (
    `project: ${project}\n` +
    `name: ${name}\n` +
    `task_queue: ${taskQueue}\n` +
    "runtime:\n" +
    "  temporal:\n" +
    "    # ${ENV:-default} interpolation: resolved from the environment at load, else the default.\n" +
    "    # (A bare ${VAR} with no default THROWS when unset — always give locally-defaulting vars a `:-`.)\n" +
    "    address: ${TEMPORAL_ADDRESS:-localhost:7233}\n" +
    "  registry:\n" +
    "    type: inline\n" +
    "    prompts:\n" +
    `      ${name}: "TODO: describe the task for ${name}. Reference input fields with {{field}}."\n` +
    "  provider:\n" +
    `    type: ${provider}\n` +
    `    model: ${model}\n` +
    "    api_key:\n" +
    "      # Secret REFERENCE, never a literal (authoring checklist rule 4).\n" +
    "      value_from:\n" +
    `        env: ${keyEnv}\n` +
    "  observability:\n" +
    "    type: none\n" +
    "activities:\n" +
    "  definitions:\n" +
    `    - name: ${activity}\n` +
    `      input: schemas:${inModel}\n` +
    `      output: schemas:${outModel}\n` +
    `      prompt: ${name}\n` +
    "      validation_retries: 1\n" +
    "workflow:\n" +
    `  name: ${pascalCase(name)}Workflow\n` +
    `  input: schemas:${inModel}\n` +
    `  output: schemas:${outModel}\n` +
    "  # Declare the real blast radius: safe | policy_gated | human_gated | prohibited.\n" +
    "  risk_tier: safe\n" +
    "  steps:\n" +
    `    - id: ${activity}_step\n` +
    `      activity: ${activity}\n`
  );
}

/** Generate a `typeflux.project.yaml` workflow/env/policy entry snippet. */
export function projectEntry(opts: {
  name: string;
  path: string;
  environmentId?: string;
  policyId?: string;
}): string {
  const { name, path, environmentId, policyId } = opts;
  const parts = [
    "# Merge these into typeflux.project.yaml. Add the workflow under `workflows:`",
    "workflows:",
    `  - id: ${name}`,
    `    path: ${path}`,
  ];
  if (environmentId) {
    parts.push(
      "# ...and register the environment under `environments:` (path relative to the manifest):",
      "environments:",
      `  ${environmentId}: environments/${environmentId}.yaml`,
    );
  }
  if (policyId) {
    parts.push(
      "# ...and the policy under `policies:`:",
      "policies:",
      `  ${policyId}: policies/${policyId}.yaml`,
    );
  }
  return `${parts.join("\n")}\n`;
}

// --- doctor -----------------------------------------------------------------------------------

/** One readiness check in the doctor checklist. */
export interface DoctorCheck {
  id: string;
  label: string;
  status: "ok" | "warn" | "fail" | "unknown";
  detail: string;
}

/** Provider api-key env vars doctor probes for presence (names only — values are NEVER read out). */
const PROVIDER_KEY_ENVS = ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY"];
/** Optional registry / observability keys doctor probes for presence. */
const REGISTRY_KEY_ENVS = ["LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGSMITH_API_KEY"];
/** Temporal / control-plane env keys doctor may probe. */
const INFRA_KEY_ENVS = [
  "TEMPORAL_ADDRESS",
  "TYPEFLUX_CP_URL",
  "TYPEFLUX_CP_TOKEN",
  "TYPEFLUX_MANIFEST",
  "TYPEFLUX_PROJECT",
  "TYPEFLUX_RUNTIME",
];

/**
 * The ALLOWLIST of env names `doctor` may probe for presence (§9): the documented provider/registry/
 * infra keys, plus anything in Typeflux's own `TYPEFLUX_*` namespace. This bounds `required_env` so it
 * can't be turned into an arbitrary env-presence oracle (e.g. probing `AWS_SECRET_ACCESS_KEY`).
 */
const PROBEABLE_ENV = new Set([...PROVIDER_KEY_ENVS, ...REGISTRY_KEY_ENVS, ...INFRA_KEY_ENVS]);

/** Whether `doctor` may probe an env NAME (documented key, or the Typeflux namespace). */
function isProbeable(name: string): boolean {
  return PROBEABLE_ENV.has(name) || /^TYPEFLUX_[A-Z0-9_]*$/.test(name);
}

/** True iff the named env var is set to a non-empty value. Reports PRESENCE only — never the value. */
function envPresent(env: NodeJS.ProcessEnv, key: string): boolean {
  const value = env[key];
  return typeof value === "string" && value.trim() !== "";
}

/**
 * Run the readiness checklist (design §6.4 "why won't it run"). READS env presence (never values),
 * pings the control plane, and — when a workflow+environment is given — probes Temporal/registry
 * reachability. Every branch degrades to an honest `warn`/`fail`/`unknown` rather than throwing.
 */
async function runDoctor(
  getBackend: GetBackend,
  config: ServerConfig,
  args: Record<string, unknown>,
  env: NodeJS.ProcessEnv,
): Promise<ToolResult> {
  const checks: DoctorCheck[] = [];

  // 1. Backend mode / how the CP is reached.
  checks.push({
    id: "backend_mode",
    label: "Control-plane backend mode",
    status: "ok",
    detail:
      config.mode === "attach"
        ? "attach: talking to the control plane at TYPEFLUX_CP_URL."
        : "managed-local: the TS control plane runs in-process from the discovered manifest.",
  });

  // 2. Required env keys (caller-supplied or mode defaults). Presence only — never the value. A
  // caller-supplied name is probed ONLY if it is allowlisted (§9): a Typeflux/provider/registry/infra
  // key, never an arbitrary name — so doctor can't be turned into a general env-presence oracle.
  const requestedEnv =
    Array.isArray(args.required_env) && args.required_env.length > 0
      ? (args.required_env as unknown[]).map(String)
      : config.mode === "attach"
        ? ["TYPEFLUX_CP_URL"]
        : [];
  const rejectedEnv: string[] = [];
  for (const key of requestedEnv) {
    if (!isProbeable(key)) {
      rejectedEnv.push(key);
      continue;
    }
    const present = envPresent(env, key);
    checks.push({
      id: `env:${key}`,
      label: `Required env: ${key}`,
      status: present ? "ok" : "fail",
      detail: present ? "set" : "MISSING — set it before running.",
    });
  }
  if (rejectedEnv.length > 0) {
    checks.push({
      id: "env_rejected",
      label: "Env names not probed",
      status: "unknown",
      detail:
        `doctor only probes Typeflux/provider/registry/infra env keys — not probed: ${rejectedEnv.join(", ")}.`,
    });
  }
  if (config.mode === "attach" && !envPresent(env, "TYPEFLUX_CP_TOKEN")) {
    checks.push({
      id: "env:TYPEFLUX_CP_TOKEN",
      label: "Bearer token: TYPEFLUX_CP_TOKEN",
      status: "warn",
      detail: "unset — a token-protected control plane will degrade to read-only discovery (no operate).",
    });
  }

  // 3. Provider / registry keys (presence, names only).
  const providersPresent = PROVIDER_KEY_ENVS.filter((k) => envPresent(env, k));
  checks.push({
    id: "provider_keys",
    label: "Provider API keys",
    status: providersPresent.length > 0 ? "ok" : "warn",
    detail:
      providersPresent.length > 0
        ? `present: ${providersPresent.join(", ")} (values not shown).`
        : `none of ${PROVIDER_KEY_ENVS.join(", ")} is set — a model call will fail. Set the one your provider needs.`,
  });
  const registryPresent = REGISTRY_KEY_ENVS.filter((k) => envPresent(env, k));
  checks.push({
    id: "registry_keys",
    label: "Registry / observability keys",
    status: "ok",
    detail:
      registryPresent.length > 0
        ? `present: ${registryPresent.join(", ")} (values not shown).`
        : "none set (fine unless you use a hosted prompt registry or trace backend).",
  });

  // 4. Temporal address hint.
  checks.push({
    id: "env:TEMPORAL_ADDRESS",
    label: "Temporal address: TEMPORAL_ADDRESS",
    status: envPresent(env, "TEMPORAL_ADDRESS") ? "ok" : "warn",
    detail: envPresent(env, "TEMPORAL_ADDRESS")
      ? "set (values not shown)."
      : "unset — the runtime defaults to localhost:7233. Set it for a remote cluster.",
  });

  // 5. Control-plane reachability (the ping). Degrades honestly when unreachable.
  let backend: Backend | undefined;
  try {
    backend = await getBackend();
    if (backend.degraded) {
      checks.push({
        id: "control_plane",
        label: "Control plane reachable",
        status: "warn",
        detail:
          "reachable but the token lacks `inspect` (403 on /meta) — only static discovery is available. " +
          "Provide a token with inspect to enable the live reads.",
      });
    } else {
      const caps = backend.capabilities;
      const canResolve = caps?.can_resolve === true;
      checks.push({
        id: "control_plane",
        label: "Control plane reachable",
        status: canResolve ? "ok" : "warn",
        detail: canResolve
          ? `up (project=${backend.meta?.project ?? "?"}, runtime=${backend.meta?.runtime ?? "?"}).`
          : `up but cannot resolve this project's runtime (project=${backend.meta?.project ?? "?"}) — ` +
            "pure-YAML reads work; attach a control plane that can resolve it for bundle/catalog/start.",
      });
    }
  } catch (error) {
    const structured = toStructuredError(error).error;
    checks.push({
      id: "control_plane",
      label: "Control plane reachable",
      status: "fail",
      detail: `unreachable: ${structured.code} (${structured.status}) — ${structured.message}`,
    });
  }

  // 6. Temporal / connection reachability — only probeable against a concrete workflow+environment.
  const workflowId = typeof args.workflow_id === "string" ? args.workflow_id : undefined;
  const environmentId = typeof args.environment_id === "string" ? args.environment_id : undefined;
  const project = typeof args.project === "string" ? args.project : undefined;
  if (backend !== undefined && !backend.degraded && workflowId && environmentId) {
    const cp =
      project && project !== "" && project !== "default"
        ? backend.scopedControlPlane(project)
        : backend.controlPlane;
    try {
      const connections = (await cp.connections(workflowId, environmentId)) as Record<string, unknown>;
      checks.push({
        id: "connections",
        label: `Connections for ${workflowId} @ ${environmentId}`,
        status: "ok",
        detail: `reachability probed: ${JSON.stringify(connections)}`,
      });
    } catch (error) {
      const structured = toStructuredError(error).error;
      checks.push({
        id: "connections",
        label: `Connections for ${workflowId} @ ${environmentId}`,
        status: structured.status === 503 ? "fail" : "warn",
        detail: `${structured.code} (${structured.status}) — ${structured.message}`,
      });
    }
  } else {
    checks.push({
      id: "connections",
      label: "Temporal / registry reachability",
      status: "unknown",
      detail: "not probed — call doctor with workflow_id + environment_id to check reachability for a run.",
    });
  }

  const ready = checks.every((c) => c.status === "ok" || c.status === "warn" || c.status === "unknown");
  const structured = { ready, mode: config.mode, checks };
  const summary =
    `# Environment readiness (doctor)\n\n` +
    `Overall: ${ready ? "no blocking failures" : "BLOCKED — see the fail entries"} (mode: ${config.mode}).\n\n` +
    checks
      .map((c) => `- [${c.status.toUpperCase()}] ${c.label} — ${c.detail}`)
      .join("\n") +
    "\n\n(Structured checklist in structuredContent. Env values are never shown — only presence.)";
  return ok(summary, structured);
}

// --- registration -----------------------------------------------------------------------------

const FIELD_ARG = z
  .array(z.union([z.string(), z.object({ name: z.string(), type: z.string().optional() })]))
  .optional();

/** Wrap generated file content as a fenced payload the editor applies (never written by the server). */
function scaffoldResult(opts: {
  style: ScaffoldStyle;
  language: string;
  targetFile: string | undefined;
  matchedStyle: boolean;
  content: string;
  note: string;
}): ToolResult {
  const { style, language, targetFile, matchedStyle, content, note } = opts;
  const fence = fenceDelimiter(content);
  const text =
    `${note}\n\n` +
    "This server does NOT write files — apply the content below as a reviewable diff, then run " +
    "validate_project.\n\n" +
    `${fence}${language}\n${content}\n${fence}`;
  return ok(text, {
    style,
    language,
    ...(targetFile ? { target_file: targetFile } : {}),
    matched_target_style: matchedStyle,
    content,
    wrote_file: false,
  });
}

/**
 * Register the Phase-3 local authoring tools. They are LOCAL (not CP-gated): the server keeps them
 * registered in every backend state, so they are returned as plain handles (no capability gate).
 * `listRoots` is the read-only workspace seam (workspace.ts); `config` gives `doctor` the backend
 * mode; `env` is injectable for tests (defaults to the process environment — presence only).
 */
export function registerAuthoringTools(
  server: McpServer,
  getBackend: GetBackend,
  config: ServerConfig,
  listRoots: ListRoots,
  env: NodeJS.ProcessEnv = process.env,
): RegisteredTool[] {
  const handles: RegisteredTool[] = [];

  handles.push(
    server.registerTool(
      "scaffold_ai_activity",
      {
        title: "Scaffold an AI activity",
        description:
          "Return generated content for a typed AI activity — Python (@ai_activity.defn or AIActivity(...)), TypeScript (defineActivity + zod strictObject schemas), or a hookless YAML activities.definitions block — matched to the target file's existing style. RETURNS CONTENT ONLY for you to apply as a reviewable diff; it never writes files. Pass target_file to match the file's style (read via workspace roots), or set style explicitly.",
        inputSchema: {
          name: z.string(),
          input_fields: FIELD_ARG,
          output_fields: FIELD_ARG,
          prompt_ref: z.string().optional(),
          style: z.enum(["python", "yaml", "typescript"]).optional(),
          target_file: z.string().optional(),
        },
        outputSchema: {
          style: z.string(),
          language: z.string(),
          target_file: z.string().optional(),
          matched_target_style: z.boolean(),
          content: z.string(),
          wrote_file: z.boolean(),
        },
        annotations: { title: "Scaffold an AI activity", ...LOCAL_ANNOTATIONS },
      },
      async (args: Record<string, unknown>): Promise<ToolResult> => {
        try {
          const name = String(args.name);
          const inputs = normalizeFields(args.input_fields);
          const outputs = normalizeFields(args.output_fields);
          const promptRef = typeof args.prompt_ref === "string" && args.prompt_ref ? args.prompt_ref : name;
          const targetFile = typeof args.target_file === "string" ? args.target_file : undefined;
          const explicit = args.style as ScaffoldStyle | undefined;
          const file = targetFile ? await readWorkspaceFile(listRoots, targetFile) : undefined;
          const { style, pythonConstructor } = detectStyle(explicit, targetFile, file?.content);
          const matchedStyle = explicit === undefined && file !== undefined;
          if (style === "python") {
            const content = pythonActivity(name, inputs, outputs, promptRef, pythonConstructor);
            return scaffoldResult({
              style,
              language: "python",
              targetFile,
              matchedStyle,
              content,
              note: `# Scaffolded Python AI activity \`${neutralize(name)}\` (${pythonConstructor ? "AIActivity(...) constructor" : "@ai_activity.defn decorator"} style).`,
            });
          }
          if (style === "typescript") {
            const content = typescriptActivity(name, inputs, outputs, promptRef);
            return scaffoldResult({
              style,
              language: "typescript",
              targetFile,
              matchedStyle,
              content,
              note: `# Scaffolded TypeScript AI activity \`${neutralize(name)}\` (defineActivity + zod).`,
            });
          }
          const content = yamlActivity(name, inputs, outputs, promptRef);
          return scaffoldResult({
            style,
            language: "yaml",
            targetFile,
            matchedStyle,
            content,
            note: `# Scaffolded hookless YAML activity \`${neutralize(name)}\`.`,
          });
        } catch (error) {
          return { content: [{ type: "text", text: JSON.stringify(toStructuredError(error).error, null, 2) }], isError: true };
        }
      },
    ),
  );

  handles.push(
    server.registerTool(
      "scaffold_workflow_yaml",
      {
        title: "Scaffold a workflow YAML",
        description:
          "Return a seeded typeflux.yaml (runtime/registry/provider stubs with ${ENV} interpolation and a secret-reference api_key) for a new workflow. RETURNS CONTENT ONLY for you to apply as a reviewable diff; it never writes files.",
        inputSchema: {
          project: z.string(),
          name: z.string(),
          task_queue: z.string().optional(),
          provider: z.string().optional(),
          model: z.string().optional(),
        },
        outputSchema: {
          style: z.string(),
          language: z.string(),
          matched_target_style: z.boolean(),
          content: z.string(),
          wrote_file: z.boolean(),
        },
        annotations: { title: "Scaffold a workflow YAML", ...LOCAL_ANNOTATIONS },
      },
      async (args: Record<string, unknown>): Promise<ToolResult> => {
        try {
          const project = String(args.project);
          const name = String(args.name);
          const providerRaw = typeof args.provider === "string" && args.provider ? args.provider.toLowerCase() : "anthropic";
          const defaults = PROVIDER_DEFAULTS[providerRaw] ?? PROVIDER_DEFAULTS.anthropic!;
          const provider = PROVIDER_DEFAULTS[providerRaw] ? providerRaw : "anthropic";
          const model = typeof args.model === "string" && args.model ? args.model : defaults.model;
          const taskQueue = typeof args.task_queue === "string" && args.task_queue ? args.task_queue : `${name}-queue`;
          const content = workflowYaml({ project, name, taskQueue, provider, model, keyEnv: defaults.keyEnv });
          return scaffoldResult({
            style: "yaml",
            language: "yaml",
            targetFile: undefined,
            matchedStyle: false,
            content,
            note: `# Scaffolded typeflux.yaml for workflow \`${neutralize(name)}\` (project \`${neutralize(project)}\`, provider \`${provider}\`).`,
          });
        } catch (error) {
          return { content: [{ type: "text", text: JSON.stringify(toStructuredError(error).error, null, 2) }], isError: true };
        }
      },
    ),
  );

  handles.push(
    server.registerTool(
      "scaffold_project_entry",
      {
        title: "Scaffold a project manifest entry",
        description:
          "Return a typeflux.project.yaml entry — a workflow record (and optionally an environment / policy binding) — to merge into the manifest. RETURNS CONTENT ONLY for you to apply as a reviewable diff; it never writes files.",
        inputSchema: {
          name: z.string(),
          path: z.string().optional(),
          environment_id: z.string().optional(),
          policy_id: z.string().optional(),
        },
        outputSchema: {
          style: z.string(),
          language: z.string(),
          matched_target_style: z.boolean(),
          content: z.string(),
          wrote_file: z.boolean(),
        },
        annotations: { title: "Scaffold a project manifest entry", ...LOCAL_ANNOTATIONS },
      },
      async (args: Record<string, unknown>): Promise<ToolResult> => {
        try {
          const name = String(args.name);
          const path = typeof args.path === "string" && args.path ? args.path : `${name}.yaml`;
          const content = projectEntry({
            name,
            path,
            environmentId: typeof args.environment_id === "string" ? args.environment_id : undefined,
            policyId: typeof args.policy_id === "string" ? args.policy_id : undefined,
          });
          return scaffoldResult({
            style: "yaml",
            language: "yaml",
            targetFile: undefined,
            matchedStyle: false,
            content,
            note: `# Scaffolded typeflux.project.yaml entry for workflow \`${neutralize(name)}\`.`,
          });
        } catch (error) {
          return { content: [{ type: "text", text: JSON.stringify(toStructuredError(error).error, null, 2) }], isError: true };
        }
      },
    ),
  );

  handles.push(
    server.registerTool(
      "doctor",
      {
        title: "Doctor — environment readiness",
        description:
          "Run an environment-readiness checklist: required env keys present (presence only — values are never shown), the control plane reachable, provider/registry keys set, and (with workflow_id + environment_id) Temporal/registry reachability. The 'why won't it run' first stop. READS only — writes nothing.",
        inputSchema: {
          required_env: z.array(z.string()).optional(),
          workflow_id: z.string().optional(),
          environment_id: z.string().optional(),
          project: z.string().optional(),
        },
        outputSchema: {
          ready: z.boolean(),
          mode: z.string(),
          checks: z.array(
            z.object({
              id: z.string(),
              label: z.string(),
              status: z.string(),
              detail: z.string(),
            }),
          ),
        },
        annotations: { title: "Doctor — environment readiness", ...LOCAL_ANNOTATIONS },
      },
      async (args: Record<string, unknown>): Promise<ToolResult> => {
        try {
          return await runDoctor(getBackend, config, args ?? {}, env);
        } catch (error) {
          return { content: [{ type: "text", text: JSON.stringify(toStructuredError(error).error, null, 2) }], isError: true };
        }
      },
    ),
  );

  return handles;
}

/** The registered Phase-3 authoring tool names (tests + README inventory). LOCAL — not ledger ops. */
export function authoringToolNames(): string[] {
  return ["scaffold_ai_activity", "scaffold_workflow_yaml", "scaffold_project_entry", "doctor"];
}
