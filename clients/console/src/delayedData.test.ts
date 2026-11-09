import type { Page, Route } from "@playwright/test";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { delayFixtureData } from "../e2e/delayed-data";

beforeEach(() => { vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] }); });
afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers(); });

async function fixture(ok: boolean, upstreamMs: number) {
  let elapsed = 0;
  vi.spyOn(performance, "now").mockImplementation(() => elapsed);
  let handle!: (route: Route) => Promise<void>;
  const page = { route: vi.fn(async (_paths, handler) => { handle = handler; }) } as unknown as Page;
  const response = { ok: () => ok };
  const fulfill = vi.fn();
  const route = {
    fetch: vi.fn(async () => { elapsed = upstreamMs; return response; }),
    fulfill,
  } as unknown as Route;
  const delivered = await delayFixtureData(page);
  return { run: () => handle(route), fulfill, response, delivered };
}

it("holds a fast successful upstream response before delivering the original response", async () => {
  const f = await fixture(true, 1_000);
  const pending = f.run();
  await Promise.resolve();
  await vi.advanceTimersByTimeAsync(4_999);
  expect(f.fulfill).not.toHaveBeenCalled();
  expect(f.delivered()).toBe(0);
  await vi.advanceTimersByTimeAsync(1);
  await pending;
  expect(f.fulfill).toHaveBeenCalledExactlyOnceWith({ response: f.response });
  expect(f.delivered()).toBe(1);
});

it("delivers an already slow successful upstream response without an additional hold", async () => {
  const f = await fixture(true, 8_000);
  await f.run();
  expect(vi.getTimerCount()).toBe(0);
  expect(f.fulfill).toHaveBeenCalledExactlyOnceWith({ response: f.response });
  expect(f.delivered()).toBe(1);
});

it("delivers an unsuccessful upstream response unchanged without holding or counting it", async () => {
  const f = await fixture(false, 100);
  await f.run();
  expect(vi.getTimerCount()).toBe(0);
  expect(f.fulfill).toHaveBeenCalledExactlyOnceWith({ response: f.response });
  expect(f.delivered()).toBe(0);
});
