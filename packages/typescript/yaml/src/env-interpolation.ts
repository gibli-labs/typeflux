/**
 * Environment-variable interpolation for YAML specs (parity Epic 5, #452; Python
 * `yaml/loader.py` `_interpolate_env`). Substitutes `${NAME}` / `${NAME:-default}`
 * against the environment, with `$${NAME}` as the escape that renders a literal
 * `${NAME}`. A missing variable with no default throws.
 *
 * Prompt-text paths are skipped: a literal `${NAME}` inside prompt content is
 * model-facing text and must reach the model verbatim, never inject a worker env value.
 */

// Same grammar as the Python loader: optional escaping `$`, a NAME, and an
// optional `:-default`.
const ENV_PATTERN = /\$(\$)?\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}/g;

export type YamlPath = (string | number)[];

export interface InterpolateEnvOptions {
  /** The environment to resolve against (defaults to `process.env`). */
  env?: Record<string, string | undefined>;
  /** A label for error messages (e.g. the spec path). */
  sourceLabel?: string;
}

/** Recursively interpolate env references through a parsed YAML value. */
export function interpolateEnv(value: unknown, options: InterpolateEnvOptions = {}, path: YamlPath = []): unknown {
  const env = options.env ?? process.env;
  if (Array.isArray(value)) {
    return value.map((item, index) => interpolateEnv(item, options, [...path, index]));
  }
  if (value !== null && typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const [key, item] of Object.entries(value)) {
      // defineProperty (not `out[key] = …`) so a `__proto__` key stays an OWN
      // property rather than routing through the prototype setter — else it would be
      // hidden from the strict schema (a validation bypass) and pollute this object's
      // prototype. The strict spec then rejects the unrecognized key (codex).
      Object.defineProperty(out, key, {
        value: interpolateEnv(item, options, [...path, key]),
        enumerable: true,
        writable: true,
        configurable: true,
      });
    }
    return out;
  }
  if (typeof value === "string") {
    if (isPromptTextPath(path)) {
      return value; // model-facing content — leave ${NAME} verbatim
    }
    return value.replace(ENV_PATTERN, (match, escaped: string | undefined, name: string, def: string | undefined) =>
      replaceEnv(match, escaped, name, def, env, path, options.sourceLabel),
    );
  }
  return value;
}

function replaceEnv(
  match: string,
  escaped: string | undefined,
  name: string,
  def: string | undefined,
  env: Record<string, string | undefined>,
  path: YamlPath,
  sourceLabel: string | undefined,
): string {
  if (escaped !== undefined) {
    // `$${NAME}` escapes interpolation and renders a literal `${NAME}`.
    return match.slice(1);
  }
  // Own-property lookup only: a bare `env[name]` would resolve an inherited
  // `Object.prototype` member (`toString`, `constructor`, `__proto__`, …) as if it
  // were a set variable — substituting a function's source instead of throwing the
  // missing-variable error a valid identifier deserves. Object.hasOwn treats those
  // names as unset (works for a plain object AND `process.env`) (codex).
  const value = Object.hasOwn(env, name) ? env[name] : undefined;
  if (value !== undefined) {
    return value;
  }
  if (def !== undefined) {
    return def;
  }
  const at = sourceLabel ? ` in ${sourceLabel}` : "";
  throw new Error(`missing environment variable: ${name}${at} at ${formatYamlPath(path)}`);
}

/**
 * Whether a value path addresses prompt text (which must NOT be interpolated) —
 * a faithful port of Python `_is_prompt_text_path`.
 */
function isPromptTextPath(path: YamlPath): boolean {
  if (path[0] === "runtime" && path[1] === "registry" && path[2] === "prompts" && path.length >= 4) {
    // runtime.registry.prompts.<name> as a plain string prompt body.
    if (path.length === 4) {
      return true;
    }
    const rest = path.slice(4);
    // ...prompts.<name>.messages[i].content  OR  ...content[j].text
    if (rest[0] === "messages" && rest.length >= 3 && rest[2] === "content") {
      return rest.length === 3 || (rest.length === 5 && rest[4] === "text");
    }
    return false;
  }
  // activities.definitions[i].artifacts[j].attach.text is injected into prompt messages.
  return (
    path.length === 7 &&
    path[0] === "activities" &&
    path[1] === "definitions" &&
    path[3] === "artifacts" &&
    path[5] === "attach" &&
    path[6] === "text"
  );
}

function formatYamlPath(path: YamlPath): string {
  let formatted = "$";
  for (const item of path) {
    if (typeof item === "number") {
      formatted += `[${item}]`;
    } else if (/^[A-Za-z_$][\w$]*$/.test(item)) {
      formatted += `.${item}`;
    } else {
      formatted += `[${JSON.stringify(item)}]`;
    }
  }
  return formatted;
}
