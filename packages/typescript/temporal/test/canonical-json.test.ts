import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { canonicalJson } from "../src/index.js";

const here = dirname(fileURLToPath(import.meta.url));
const contractsDir = resolve(here, "../../../../contracts");

interface NumberPair {
  value: unknown;
  canonical: string;
}

const pairs = JSON.parse(
  readFileSync(resolve(contractsDir, "canonical-json/golden/numbers.json"), "utf-8"),
) as NumberPair[];

describe("canonical_json number formatting parity (#420)", () => {
  it("reproduces every Python canonical string byte-for-byte", () => {
    expect(pairs.length).toBeGreaterThan(0);
    for (const pair of pairs) {
      expect(canonicalJson(pair.value)).toBe(pair.canonical);
    }
  });

  it("matches Python repr across the fixed/scientific threshold", () => {
    // The exponent boundary: e >= -4 is fixed, e <= -5 is scientific (padded).
    expect(canonicalJson(1e-4)).toBe("0.0001");
    expect(canonicalJson(1e-5)).toBe("1e-05");
    expect(canonicalJson(1e-6)).toBe("1e-06");
    expect(canonicalJson(1.5e-7)).toBe("1.5e-07");
    // Integral past 1e21 stays a full integer (JSON.stringify would give 1e+21).
    expect(canonicalJson(1e21)).toBe("1000000000000000000000");
    // Integral floats below that, and -0, collapse to plain ints.
    expect(canonicalJson(1e16)).toBe("10000000000000000");
    expect(canonicalJson(-0)).toBe("0");
    // Unsafe integral doubles (> 2^53): the EXACT integer value, not the shorter
    // round-tripping decimal `n.toString()` would give (== Python int(float)).
    expect(canonicalJson(8.236641532529827e16)).toBe("82366415325298272");
    expect(canonicalJson(816023253420088576)).toBe("816023253420088576");
  });
});
