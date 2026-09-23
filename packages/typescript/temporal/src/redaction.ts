/**
 * Metadata redaction (parity Epic 4, #451) — a port of Python
 * `observability/redaction.py` `RegexPIIRedactor`. `redactMetadata` recursively
 * replaces PII in the string values of a metadata structure (emails / SSNs /
 * Luhn-valid card numbers / phone numbers), skipping configured `excludePaths`
 * (fnmatch-style globs). It is idempotent: a replacement like `[REDACTED_EMAIL]`
 * matches no rule, so re-redacting is a no-op.
 *
 * The redaction is applied to trace metadata by a `TraceWriter` (and exposed here
 * for redacting any free-form metadata before emit).
 */

/** Internal Typeflux/Temporal metadata paths preserved when `preserveTypefluxMetadata` is on. */
export const DEFAULT_EXCLUDED_PATHS: readonly string[] = [
  "temporal.workflow_id",
  "temporal.run_id",
  "temporal.activity_id",
  "temporal.workflow_type",
  "temporal.activity_type",
  "typeflux.workflow.workflow_id",
  "typeflux.workflow.workflow_name",
  "typeflux.runtime_placement.*",
  "typeflux.execution_manifest.*",
  "typeflux.activity.*",
  "typeflux.activity_execution_manifest.*",
  "typeflux.yaml.map_steps.*",
  "typeflux.map.*",
  "typeflux.join.*",
  "typeflux.lifecycle.state",
  "typeflux.lifecycle.current_step",
  "typeflux.lifecycle.completed_units",
  "typeflux.lifecycle.total_units",
  "typeflux.lifecycle.cancellation_requested",
  "typeflux.lifecycle.waiting_checkpoint",
  "typeflux.lifecycle.terminal_status",
  "typeflux.lifecycle.status_event_limit",
  "typeflux.lifecycle.review_after_step",
  "typeflux.provider_controls.*",
  "typeflux.manifest_hash",
  "typeflux.input_schema_hash",
  "typeflux.output_schema_hash",
  "typeflux.activity_execution_manifest_hash",
  // Moderation verdict is a classification (never raw output), preserved as audit evidence.
  "typeflux_moderation.*",
  "langfuse.prompt_config.*",
];

export interface RedactionConfig {
  /** Master switch — `false` returns the value unchanged (default `true`). */
  enabled?: boolean;
  emails?: boolean;
  phones?: boolean;
  ssn?: boolean;
  creditCards?: boolean;
  /** Preserve internal Typeflux/Temporal metadata paths (default `true`). */
  preserveTypefluxMetadata?: boolean;
  /**
   * Extra fnmatch-style glob paths to skip. Paths are relative to the redacted value's
   * ROOT — for trace metadata that is the metadata dict's keys (e.g. `"ticket_id"` or
   * `"temporal.workflow_id"`, NOT `"metadata.ticket_id"`), matching the Python convention
   * and the `DEFAULT_EXCLUDED_PATHS` form.
   */
  excludePaths?: readonly string[];
  /**
   * Custom rules (#188 D188-4) appended AFTER the built-in catalog: the built-ins redact
   * known PII first, then these jurisdiction/domain rules run over the result. `pattern`
   * is the JS `RegExp` dialect (per-edition — the same YAML gives Python `re` on the other
   * side; see docs/privacy.md). Excluded paths still win, checked per node before any rule.
   */
  customRules?: readonly CustomRedactionRule[];
}

/** A custom redaction rule declared in YAML (`observability.redaction.custom_rules`). */
export interface CustomRedactionRule {
  name: string;
  /** JS `RegExp` source; compiled with the global flag so every match is replaced. */
  pattern: string;
  replacement: string;
}

interface RedactionRule {
  pattern: RegExp;
  replacement: string;
  validator?: (candidate: string) => boolean;
}

/** Luhn check used to redact only valid-looking card numbers (matches the Python validator). */
function isLuhnValid(candidate: string): boolean {
  const digits = candidate.replace(/[ -]/g, "");
  if (!/^\d+$/.test(digits) || digits.length < 13 || digits.length > 19) {
    return false;
  }
  let total = 0;
  const reversed = digits.split("").reverse();
  for (let index = 0; index < reversed.length; index += 1) {
    let value = Number(reversed[index]);
    if (index % 2 === 1) {
      value *= 2;
      if (value > 9) {
        value -= 9;
      }
    }
    total += value;
  }
  return total % 10 === 0;
}

function buildRules(config: RedactionConfig): RedactionRule[] {
  const rules: RedactionRule[] = [];
  if (config.emails !== false) {
    rules.push({ pattern: /\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b/g, replacement: "[REDACTED_EMAIL]" });
  }
  if (config.ssn !== false) {
    rules.push({ pattern: /\b\d{3}-\d{2}-\d{4}\b/g, replacement: "[REDACTED_SSN]" });
  }
  if (config.creditCards !== false) {
    rules.push({
      pattern: /(?<!\d)\d(?:[ -]?\d){12,18}(?!\d)/g,
      replacement: "[REDACTED_CARD]",
      validator: isLuhnValid,
    });
  }
  if (config.phones !== false) {
    rules.push({
      pattern: /(?<!\d)(?:\+1[-.\s]?)?(?:\(\d{3}\)|\d{3}[-.\s])\d{3}[-.\s]\d{4}(?!\d)/g,
      replacement: "[REDACTED_PHONE]",
    });
  }
  for (const rule of config.customRules ?? []) {
    // Global flag so `applyRules`' String.replace replaces every occurrence, matching
    // Python's `pattern.sub`. Compiling here throws SyntaxError on an invalid pattern —
    // the spec/config layer validates first (fail-closed) so this stays a belt-and-braces
    // guard rather than the primary error surface.
    rules.push({ pattern: new RegExp(rule.pattern, "g"), replacement: rule.replacement });
  }
  return rules;
}

/**
 * fnmatchcase-style glob match — `*` matches any run (including dots), `?` one char,
 * everything else literal. A linear two-pointer scan (no regex), so an adversarial
 * `excludePaths` glob can't trigger catastrophic backtracking (parity with Python's
 * `fnmatch`, which compiles to a non-backtracking matcher).
 */
function globMatch(text: string, pattern: string): boolean {
  let t = 0;
  let p = 0;
  let starP = -1;
  let starT = 0;
  while (t < text.length) {
    if (p < pattern.length && (pattern[p] === "?" || pattern[p] === text[t])) {
      t += 1;
      p += 1;
    } else if (p < pattern.length && pattern[p] === "*") {
      starP = p;
      starT = t;
      p += 1;
    } else if (starP !== -1) {
      // Backtrack: let the last `*` absorb one more character.
      p = starP + 1;
      starT += 1;
      t = starT;
    } else {
      return false;
    }
  }
  while (p < pattern.length && pattern[p] === "*") {
    p += 1;
  }
  return p === pattern.length;
}

/**
 * Compiled-rule cache keyed on `RedactionConfig` object IDENTITY. `redactMetadata` is called
 * per trace by the TraceWriter — a single trace fans out to input/output/error/metadata plus
 * one call per observation (~6+5M times for a large trace), and `buildRules` recompiles every
 * custom `RegExp` each time. Identity-keying is sound because configs come from parsed spec
 * objects that are not mutated after construction; a distinct config object (or `undefined`,
 * which we don't cache) rebuilds. A `WeakMap` lets a config be GC'd with no manual eviction.
 * The cached rules are safe to SHARE across calls because `applyRules` only ever calls
 * `String.prototype.replace` (which resets `lastIndex` on the shared /g regexes each time) —
 * it never uses `.test`/`.exec`, so there is no stateful `lastIndex` to leak between calls.
 */
const compiledRulesCache = new WeakMap<RedactionConfig, RedactionRule[]>();

function getRules(config: RedactionConfig | undefined): RedactionRule[] {
  if (config === undefined) {
    return buildRules({});
  }
  const cached = compiledRulesCache.get(config);
  if (cached !== undefined) {
    return cached;
  }
  const rules = buildRules(config);
  compiledRulesCache.set(config, rules);
  return rules;
}

function applyRules(text: string, rules: RedactionRule[]): string {
  let result = text;
  for (const rule of rules) {
    result = result.replace(rule.pattern, (match) =>
      rule.validator === undefined || rule.validator(match) ? rule.replacement : match,
    );
  }
  return result;
}

function redactValue(value: unknown, path: string[], rules: RedactionRule[], excludePaths: readonly string[]): unknown {
  if (path.length > 0) {
    const dotted = path.join(".");
    if (excludePaths.some((pattern) => globMatch(dotted, pattern))) {
      return value;
    }
  }
  if (typeof value === "string") {
    return applyRules(value, rules);
  }
  if (Array.isArray(value)) {
    // Array item paths stay index-less (matches Python — for wildcard excludes like `…activities.*`).
    return value.map((item) => redactValue(item, path, rules, excludePaths));
  }
  if (isPlainObject(value)) {
    const out: Record<string, unknown> = {};
    for (const [key, item] of Object.entries(value)) {
      out[key] = redactValue(item, [...path, key], rules, excludePaths);
    }
    return out;
  }
  // Non-plain objects (Date, Map, Set, URL, class instances) pass through unchanged —
  // rebuilding them from `Object.entries` would lose their type / drop non-enumerable state.
  return value;
}

/** A `{}`-literal / `Object.create(null)` record — NOT a Date/Map/Set/class instance. */
function isPlainObject(value: unknown): value is Record<string, unknown> {
  if (value === null || typeof value !== "object") {
    return false;
  }
  const proto = Object.getPrototypeOf(value);
  return proto === Object.prototype || proto === null;
}

/**
 * Recursively redact PII from the string values of `value`. Returns a redacted copy;
 * the input is not mutated. Disabled (`enabled: false`) returns the value unchanged.
 */
export function redactMetadata<T>(value: T, config?: RedactionConfig): T {
  if (config?.enabled === false) {
    return value;
  }
  // `getRules` memoizes per config-object identity (see `compiledRulesCache`); an omitted
  // config rebuilds without caching a throwaway `{}` on every call.
  const rules = getRules(config);
  const excludePaths =
    config?.preserveTypefluxMetadata !== false
      ? [...DEFAULT_EXCLUDED_PATHS, ...(config?.excludePaths ?? [])]
      : [...(config?.excludePaths ?? [])];
  return redactValue(value, [], rules, excludePaths) as T;
}
