/**
 * Prompt message rendering (parity Epic 1, #448) — a faithful port of the Python
 * `core/render`. Substitutes validated `{{var}}` placeholders (dot-path
 * identifiers, no mustache sections or partials) from the activity input.
 *
 * Substitution is plain text: user content round-trips byte-for-byte. A missing
 * field throws (matching Python's `KeyError`); `null`/`undefined` renders as "".
 */

import { renderContentParts } from "./content-parts.js";
import type { ChatMessage } from "./manifest-hashing.js";

const MUSTACHE_VARIABLE = /\{\{\s*([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s*\}\}/g;
// A non-global twin for detection: `.test()` on a /g regex is stateful (lastIndex).
const MUSTACHE_VARIABLE_DETECT = new RegExp(MUSTACHE_VARIABLE.source);

/**
 * True if `template` contains at least one `{{var}}` placeholder (Python
 * `has_template_variables`). Used to reject per-item template variables in a
 * session-cached system prefix (#60): a prefix that varies per item cannot be
 * the stable cached content.
 */
export function hasTemplateVariables(template: string): boolean {
  return MUSTACHE_VARIABLE_DETECT.test(template);
}

function resolvePath(data: Record<string, unknown>, path: string): unknown {
  let current: unknown = data;
  for (const part of path.split(".")) {
    // `Object.hasOwn` (not `in`) so a prototype member like `{{ toString }}` /
    // `{{ constructor }}` throws "missing prompt field" instead of resolving to an
    // inherited value — only own input fields are addressable.
    if (
      current === null ||
      typeof current !== "object" ||
      Array.isArray(current) ||
      !Object.hasOwn(current, part)
    ) {
      throw new Error(`missing prompt field: ${path}`);
    }
    current = (current as Record<string, unknown>)[part];
  }
  return current;
}

function formatValue(value: unknown): string {
  if (value === null || value === undefined) {
    return "";
  }
  // Match Python `str(bool)` (`True`/`False`) so a `{{ flag }}` placeholder renders
  // the same prompt text cross-SDK.
  if (typeof value === "boolean") {
    return value ? "True" : "False";
  }
  // A Date renders as bare ISO text — JSON.stringify would add quote-noise
  // around it (Temporal payloads pre-stringify Dates, but direct in-process
  // callers can bind one).
  if (value instanceof Date) {
    return value.toISOString();
  }
  // Python renders objects/arrays via str() — a content-bearing repr. JS
  // String() would emit "[object Object]": content-FREE text silently sent to
  // the model (caught by the insurance example port, #455). JSON is the
  // behavioral-parity equivalent — not byte-identical to Python's repr, but
  // structured text a model can actually read.
  if (typeof value === "object") {
    return JSON.stringify(value);
  }
  return String(value);
}

/** Substitute `{{var}}` / `{{a.b}}` placeholders in `template` from `input`. */
export function renderTemplate(template: string, input: Record<string, unknown>): string {
  return template.replace(MUSTACHE_VARIABLE, (_match, path: string) =>
    formatValue(resolvePath(input, path)),
  );
}

/** Render each message's content against the activity input (string or content parts). */
export function renderMessages(messages: ChatMessage[], input: Record<string, unknown>): ChatMessage[] {
  return messages.map((message) => ({
    ...message,
    content: renderContentParts(message.content, (text) => renderTemplate(text, input)),
  }));
}
