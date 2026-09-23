import type { ModelProvider, StructuredCallParams } from "./index.js";

/**
 * First-party test providers (#808; Python `typeflux.testing.FakeProvider` parity),
 * published as the `@typeflux/temporal/testing` subpath so downstream workflow authors can
 * test their own workflows without hand-rolling a provider. Promoted verbatim from the
 * package's own test helper — the package's tests consume THIS module, so the public
 * surface is exactly what the engine's suite exercises.
 */

/**
 * A provider that replays queued responses by index (the queue is not
 * consumed). `calls` counts successful calls only; a call past the end of the
 * script throws.
 */
export class ScriptedProvider implements ModelProvider {
  calls = 0;
  constructor(private readonly responses: unknown[]) {}
  structuredCall(_params: StructuredCallParams): unknown {
    if (this.calls >= this.responses.length) {
      throw new Error("ScriptedProvider exhausted");
    }
    return this.responses[this.calls++];
  }
}

/**
 * A provider that consumes (shifts) queued responses. Unlike
 * `ScriptedProvider`, `calls` counts every attempt — including one that finds
 * the queue empty and throws.
 */
export class FakeProvider implements ModelProvider {
  calls = 0;
  constructor(private readonly responses: unknown[]) {}
  structuredCall(_params: StructuredCallParams): unknown {
    this.calls += 1;
    if (this.responses.length === 0) {
      throw new Error("FakeProvider has no responses left");
    }
    return this.responses.shift();
  }
}
