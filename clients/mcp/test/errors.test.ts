import { describe, expect, it } from "vitest";

import {
  ApiRequestError,
  describeError,
  isUnsupportedRuntime,
  STATUS_CODES,
  toToolError,
} from "../src/control-plane/errors.js";

describe("error taxonomy (design §9)", () => {
  it("maps each taxonomy status to its canonical code", () => {
    expect(STATUS_CODES[403]).toBe("Unauthorized");
    expect(STATUS_CODES[404]).toBe("NotFound");
    expect(STATUS_CODES[409]).toBe("LifecycleBindingError");
    expect(STATUS_CODES[422]).toBe("InvalidRequest");
    expect(STATUS_CODES[501]).toBe("UnsupportedRuntime");
    expect(STATUS_CODES[503]).toBe("TemporalUnavailable");
  });

  it("describeError prefers the wire {error,message} and prefixes the code", () => {
    const failure = describeError(501, { error: "UnsupportedRuntime", message: "cannot resolve python" });
    expect(failure.status).toBe(501);
    expect(failure.code).toBe("UnsupportedRuntime");
    expect(failure.message).toBe("UnsupportedRuntime: cannot resolve python");
  });

  it("describeError falls back to the canonical code when the body has no error", () => {
    const failure = describeError(404, {});
    expect(failure.code).toBe("NotFound");
    expect(failure.message).toContain("NotFound");
  });

  it("ApiRequestError preserves status and code", () => {
    const error = new ApiRequestError(describeError(409, { error: "LifecycleBindingError", message: "conflict" }));
    expect(error.status).toBe(409);
    expect(error.code).toBe("LifecycleBindingError");
    expect(error.message).toContain("conflict");
  });

  it("isUnsupportedRuntime is true only for the 501 / UnsupportedRuntime case", () => {
    expect(isUnsupportedRuntime(new ApiRequestError(describeError(501, { error: "UnsupportedRuntime", message: "x" })))).toBe(true);
    expect(isUnsupportedRuntime(new ApiRequestError(describeError(404, { error: "NotFound", message: "x" })))).toBe(false);
    expect(isUnsupportedRuntime(new Error("nope"))).toBe(false);
  });

  it("toToolError renders a structured error in content, with NO structuredContent (item 3)", () => {
    const result = toToolError(new ApiRequestError(describeError(422, { error: "InvalidRequest", message: "bad" })));
    expect(result.isError).toBe(true);
    // The success outputSchema would reject a {error} structuredContent (MCP -32602), so the
    // structured error rides in `content` as JSON text and structuredContent is left unset.
    expect("structuredContent" in result).toBe(false);
    const error = JSON.parse(result.content[0]!.text) as { code: string; status: number; message: string };
    expect(error.code).toBe("InvalidRequest");
    expect(error.status).toBe(422);
    expect(error.message).toContain("bad");
  });

  it("toToolError classifies a non-API throw as BackendUnavailable", () => {
    const result = toToolError(new Error("control plane did not come up"));
    const error = JSON.parse(result.content[0]!.text) as { code: string; status: number };
    expect(error.code).toBe("BackendUnavailable");
    expect(error.status).toBe(0);
  });
});
