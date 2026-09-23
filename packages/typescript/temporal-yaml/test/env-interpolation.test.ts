import { describe, expect, it } from "vitest";

import { interpolateEnv } from "../src/index.js";

describe("interpolateEnv (#452)", () => {
  it("substitutes ${VAR} from the environment", () => {
    expect(interpolateEnv("addr=${HOST}", { env: { HOST: "localhost" } })).toBe("addr=localhost");
  });

  it("uses ${VAR:-default} when the variable is unset, the value when set", () => {
    expect(interpolateEnv("${Q:-the-default}", { env: {} })).toBe("the-default");
    expect(interpolateEnv("${Q:-the-default}", { env: { Q: "set" } })).toBe("set");
  });

  it("escapes $${VAR} to a literal ${VAR}", () => {
    expect(interpolateEnv("$${NAME}", { env: { NAME: "x" } })).toBe("${NAME}");
  });

  it("throws on a missing variable with no default", () => {
    expect(() => interpolateEnv("${MISSING}", { env: {} })).toThrow(/missing environment variable: MISSING/);
  });

  it("treats an inherited Object.prototype name as UNSET, not a resolved value (codex)", () => {
    // `${toString}` / `${constructor}` are valid identifiers but not env vars — a
    // prototype-chain lookup would substitute a function's source; own-key semantics
    // make them miss (throw), and honor an explicit default.
    expect(() => interpolateEnv("${toString}", { env: {} })).toThrow(/missing environment variable: toString/);
    expect(() => interpolateEnv("${constructor}", { env: {} })).toThrow(/missing environment variable: constructor/);
    expect(interpolateEnv("${toString:-tq}", { env: {} })).toBe("tq");
    // An env var legitimately named `toString` (an own key) still resolves.
    expect(interpolateEnv("${toString}", { env: { toString: "real" } })).toBe("real");
  });

  it("recurses through nested maps and arrays", () => {
    const out = interpolateEnv(
      { a: ["${X}", { b: "${Y:-dy}" }], n: 5, flag: true },
      { env: { X: "vx" } },
    );
    expect(out).toEqual({ a: ["vx", { b: "dy" }], n: 5, flag: true });
  });

  it("leaves prompt text verbatim (a ${VAR} in a prompt body must reach the model)", () => {
    const out = interpolateEnv(
      {
        task_queue: "${Q:-tq}",
        runtime: { registry: { prompts: { greeting: "Hello ${NAME}, your code is ${CODE}" } } },
      },
      { env: { Q: "real", NAME: "leak" } },
    );
    expect(out).toEqual({
      task_queue: "real", // config: interpolated
      runtime: { registry: { prompts: { greeting: "Hello ${NAME}, your code is ${CODE}" } } }, // verbatim
    });
  });

  it("passes non-string scalars through unchanged", () => {
    expect(interpolateEnv(42, { env: {} })).toBe(42);
    expect(interpolateEnv(null, { env: {} })).toBe(null);
  });
});
