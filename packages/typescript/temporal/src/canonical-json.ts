/**
 * Canonical JSON serialization, byte-compatible with the Python SDK's
 * `manifests._common.canonical_json` (#391), so hashes reproduce across SDKs.
 *
 * Rules (matching Python's `json.dumps(..., sort_keys=True,
 * separators=(",", ":"))` with the default `ensure_ascii=True`):
 *  - object keys are sorted recursively, by Unicode **code point** (Python's
 *    order), not JS's default UTF-16 code-unit order;
 *  - compact separators (no spaces);
 *  - non-ASCII characters are escaped to lowercase `\uXXXX`.
 *
 * Numbers (#420): rendering is NOT delegated to `JSON.stringify` (whose
 * exponential-notation thresholds differ from Python's `repr`); `formatNumber`
 * reproduces Python's `json.dumps(_neutralize_numbers(x))` byte-for-byte across
 * every magnitude - integral floats collapse to their exact integer via `BigInt`
 * (`n.toString()` would diverge past 2^53 and go exponential past `1e21`), and
 * non-integral floats follow Python's fixed-vs-scientific rule with a signed,
 * >=2-digit zero-padded exponent
 * (`1e-6 -> "1e-06"`, `1.5e-5 -> "1.5e-05"`). The `canonical-json` conformance
 * area pins this against the Python output.
 */

/** Compare two strings by Unicode code point, matching Python's string order. */
function compareByCodePoint(a: string, b: string): number {
  const ca = Array.from(a);
  const cb = Array.from(b);
  const shared = Math.min(ca.length, cb.length);
  for (let i = 0; i < shared; i += 1) {
    const delta = (ca[i] as string).codePointAt(0)! - (cb[i] as string).codePointAt(0)!;
    if (delta !== 0) {
      return delta;
    }
  }
  return ca.length - cb.length;
}

/**
 * Render a number exactly as Python's `json.dumps(_neutralize_numbers(x))`:
 *  - non-finite -> `NaN`/`Infinity`/`-Infinity` (Python emits these literally);
 *  - integral, finite -> its EXACT integer via `BigInt` (`_neutralize_numbers` does
 *    `int(float)`; `n.toString()` would diverge past 2^53), and `-0 -> 0`;
 *  - non-integral -> Python `repr`: the shortest round-trip mantissa, scientific
 *    when the decimal exponent `e < -4 || e >= 16` (signed, >=2-digit zero-padded),
 *    else fixed (where JS `toString` already agrees with Python).
 */
function formatNumber(n: number): string {
  if (Number.isNaN(n)) {
    return "NaN";
  }
  if (n === Infinity) {
    return "Infinity";
  }
  if (n === -Infinity) {
    return "-Infinity";
  }
  if (Number.isInteger(n)) {
    if (Object.is(n, -0)) {
      return "0";
    }
    // BigInt of an integral double is its EXACT integer value, matching Python's
    // `int(float)`. `n.toString()` would instead give the shortest round-tripping
    // decimal, which diverges past 2^53 (e.g. 8.236641532529827e16 -> JS
    // "82366415325298270" vs Python's exact "82366415325298272"), and uses
    // exponential past 1e21.
    return BigInt(n).toString();
  }
  // Non-integral finite float. `toExponential()` (no args) is the shortest
  // round-trip mantissa + exponent, e.g. "1.5e-5" -> mantissa "1.5", e = -5.
  const exponential = n.toExponential();
  const eIndex = exponential.indexOf("e");
  const mantissa = exponential.slice(0, eIndex);
  const e = Number.parseInt(exponential.slice(eIndex + 1), 10);
  if (e < -4 || e >= 16) {
    const sign = e < 0 ? "-" : "+";
    const digits = Math.abs(e).toString().padStart(2, "0");
    return `${mantissa}e${sign}${digits}`;
  }
  return n.toString();
}

/**
 * Emit `value` as canonical JSON (numbers via `formatNumber`, keys sorted by code
 * point). Returns `undefined` for `undefined`/function/symbol so callers can drop
 * the key (object) or substitute `null` (array) - exactly as `JSON.stringify` does.
 */
function emit(value: unknown): string | undefined {
  // Mirror JSON.stringify's toJSON hook so values like Date serialize as before.
  if (
    value !== null &&
    typeof value === "object" &&
    typeof (value as { toJSON?: unknown }).toJSON === "function"
  ) {
    value = (value as { toJSON: () => unknown }).toJSON();
  }
  if (value === null) {
    return "null";
  }
  switch (typeof value) {
    case "number":
      return formatNumber(value);
    case "boolean":
      return value ? "true" : "false";
    case "string":
      return JSON.stringify(value);
    case "bigint":
      // JSON.stringify throws on bigint; Python ints are unbounded - emit decimal.
      return value.toString();
    case "object": {
      if (Array.isArray(value)) {
        return "[" + value.map((item) => emit(item) ?? "null").join(",") + "]";
      }
      const source = value as Record<string, unknown>;
      const parts: string[] = [];
      for (const key of Object.keys(source).sort(compareByCodePoint)) {
        const rendered = emit(source[key]);
        if (rendered === undefined) {
          continue; // undefined/function/symbol values: drop the key
        }
        parts.push(JSON.stringify(key) + ":" + rendered);
      }
      return "{" + parts.join(",") + "}";
    }
    default:
      return undefined; // undefined / function / symbol
  }
}

function escapeNonAscii(json: string): string {
  // Python's default ensure_ascii=True escapes every code unit > 0x7F as a
  // lowercase \uXXXX sequence; JSON.stringify emits them raw. Surrogate halves
  // are escaped individually, matching Python's output for astral characters.
  let out = "";
  for (let i = 0; i < json.length; i += 1) {
    const code = json.charCodeAt(i);
    out += code > 0x7f ? "\\u" + code.toString(16).padStart(4, "0") : json.charAt(i);
  }
  return out;
}

/** Serialize `value` to the cross-SDK canonical JSON string. */
export function canonicalJson(value: unknown): string {
  return escapeNonAscii(emit(value) as string);
}
