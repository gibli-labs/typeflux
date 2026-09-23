import { describe, expect, it } from "vitest";

import html from "../index.html?raw";

const bootstrapMatch = html.match(/<script id="theme-bootstrap">([\s\S]*?)<\/script>/);

if (bootstrapMatch == null) {
  throw new Error("index.html must contain the synchronous theme bootstrap");
}

const bootstrap = bootstrapMatch[1];

function executeBootstrap(stored: string | null, systemDark: boolean, storageBlocked = false) {
  const documentElement = { dataset: {} as Record<string, string> };
  let themeColor = "#fafafa";
  const documentStub = {
    documentElement,
    querySelector(selector: string) {
      expect(selector).toBe('meta[name="theme-color"]');
      return {
        setAttribute(name: string, value: string) {
          expect(name).toBe("content");
          themeColor = value;
        },
      };
    },
  };
  const storageStub = {
    getItem(key: string) {
      expect(key).toBe("typeflux.theme");
      if (storageBlocked) throw new Error("storage blocked");
      return stored;
    },
  };
  const windowStub = {
    matchMedia(query: string) {
      expect(query).toBe("(prefers-color-scheme: dark)");
      return { matches: systemDark };
    },
  };

  new Function("document", "localStorage", "window", bootstrap)(
    documentStub,
    storageStub,
    windowStub,
  );

  return { theme: documentElement.dataset.theme, themeColor };
}

describe("theme bootstrap", () => {
  it("runs before the application module", () => {
    expect(html.indexOf('id="theme-bootstrap"')).toBeGreaterThan(-1);
    expect(html.indexOf('id="theme-bootstrap"')).toBeLessThan(
      html.indexOf('src="/src/main.tsx"'),
    );
  });

  it("applies a stored dark preference before React mounts", () => {
    expect(executeBootstrap("dark", false)).toEqual({
      theme: "dark",
      themeColor: "#101012",
    });
  });

  it("falls back to the system dark preference", () => {
    expect(executeBootstrap(null, true)).toEqual({
      theme: "dark",
      themeColor: "#101012",
    });
  });

  it("still uses the system preference when storage is blocked", () => {
    expect(executeBootstrap(null, true, true)).toEqual({
      theme: "dark",
      themeColor: "#101012",
    });
  });
});
