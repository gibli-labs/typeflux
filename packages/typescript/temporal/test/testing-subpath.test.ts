import { describe, expect, it } from "vitest";

import { FakeProvider, ScriptedProvider } from "../src/testing.js";

describe("@typeflux/temporal/testing (#808)", () => {
  it("ScriptedProvider replays by index without consuming; throws past the end", () => {
    const provider = new ScriptedProvider([{ ok: 1 }]);
    expect(provider.structuredCall({} as never)).toEqual({ ok: 1 });
    expect(provider.calls).toBe(1);
    expect(() => provider.structuredCall({} as never)).toThrow(/exhausted/);
  });

  it("FakeProvider consumes responses and counts every attempt", () => {
    const provider = new FakeProvider([{ ok: 1 }]);
    expect(provider.structuredCall({} as never)).toEqual({ ok: 1 });
    expect(() => provider.structuredCall({} as never)).toThrow(/no responses left/);
    expect(provider.calls).toBe(2);
  });
});
