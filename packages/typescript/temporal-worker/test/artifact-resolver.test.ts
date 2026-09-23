import { createHash } from "node:crypto";
import { mkdtemp, realpath, symlink, writeFile } from "node:fs/promises";
import { homedir, tmpdir } from "node:os";
import { join } from "node:path";

import { beforeAll, describe, expect, it } from "vitest";

import { artifactInput, artifactPolicy } from "@typeflux/temporal";

import { resolveArtifactInputs, valueAtPath } from "../src/index.js";

let root: string;
let pngPath: string;
const PNG_BYTES = Buffer.from([0x89, 0x50, 0x4e, 0x47]);
const PNG_SHA = createHash("sha256").update(PNG_BYTES).digest("hex");

beforeAll(async () => {
  root = await mkdtemp(join(tmpdir(), "tf-resolver-"));
  pngPath = join(root, "logo.png");
  await writeFile(pngPath, PNG_BYTES);
  await writeFile(join(root, "notes.txt"), "hello notes");
});

describe("valueAtPath (#481)", () => {
  it("walks input dot-paths, skipping the leading `input` segment", () => {
    expect(valueAtPath({ docs: { main: "a.pdf" } }, "input.docs.main")).toBe("a.pdf");
    expect(valueAtPath({ docs: null }, "input.docs.main")).toBeNull();
    expect(valueAtPath("scalar", "input.docs")).toBeUndefined();
  });

  it("expands ~ to the home directory (path.join normalizes the kept separator)", async () => {
    // Bugbot flagged slice(1) keeping the leading "/" as a bug; path.join('/home/u', '/x.png')
    // normalizes to '/home/u/x.png' (segment-reset is path.resolve behavior, not join's). Pin it:
    // a ~-rooted path resolves under the real home, not to filesystem root.
    await expect(
      resolveArtifactInputs(
        { d: "~/tf-definitely-missing-artifact.png" },
        [artifactInput({ name: "d", from_path: "input.d" })],
        artifactPolicy({ local_roots: [homedir()] }),
      ),
    ).rejects.toThrow(/does not exist or is not a file: ~\/tf-definitely-missing-artifact\.png/);
  });

  it("does not index into arrays (Python parity: a list is not a Mapping)", () => {
    // JS bracket access would resolve arr["0"]; Python's getattr(list, "0") does not — a spec
    // relying on `input.docs.0` must fail identically on both SDKs.
    expect(valueAtPath({ docs: ["a.pdf"] }, "input.docs.0")).toBeUndefined();
  });
});

describe("resolveArtifactInputs (#481)", () => {
  const policyFor = () => artifactPolicy({ local_roots: [root] });

  it("resolves a relative local path against the roots, hashing and sizing the file", async () => {
    const groups = await resolveArtifactInputs(
      { logo: "logo.png" },
      [artifactInput({ name: "logo", from_path: "input.logo" })],
      policyFor(),
    );
    expect(groups).toHaveLength(1);
    const artifact = groups[0]?.artifacts[0];
    expect(artifact).toMatchObject({
      group: "logo",
      index: 0,
      source_kind: "local_path",
      kind: "image",
      media_type: "image/png",
      role: "logo",
      sha256: PNG_SHA,
      size_bytes: PNG_BYTES.length,
      // The resolver returns the REAL path (symlinks followed, Python Path.resolve parity) —
      // on macOS the mkdtemp root under /var realpaths to /private/var.
      local_path: await realpath(pngPath),
    });
  });

  it("coerces list values, guesses kinds from media types, and honors ref overrides", async () => {
    const groups = await resolveArtifactInputs(
      { docs: ["notes.txt", { source: "logo.png", role: "brand" }] },
      [artifactInput({ name: "docs", from_path: "input.docs" })],
      policyFor(),
    );
    const [notes, logo] = groups[0]?.artifacts ?? [];
    expect(notes).toMatchObject({ kind: "data", media_type: "text/plain", role: "docs" }); // text -> data (Python table)
    expect(logo).toMatchObject({ kind: "image", role: "brand" });
  });

  it("enforces required, max_count, kind mismatch, and hash mismatch", async () => {
    const policy = policyFor();
    await expect(
      resolveArtifactInputs({}, [artifactInput({ name: "d", from_path: "input.d" })], policy),
    ).rejects.toThrow(/required artifact input resolved to null/);
    await expect(
      resolveArtifactInputs({ d: [] }, [artifactInput({ name: "d", from_path: "input.d" })], policy),
    ).rejects.toThrow(/resolved to an empty list/);
    await expect(
      resolveArtifactInputs(
        { d: ["logo.png", "notes.txt"] },
        [artifactInput({ name: "d", from_path: "input.d", max_count: 1 })],
        policy,
      ),
    ).rejects.toThrow(/allows at most 1 artifact\(s\)/);
    await expect(
      resolveArtifactInputs(
        { d: { source: "logo.png", kind: "image" } },
        [artifactInput({ name: "d", from_path: "input.d", kind: "document" })],
        policy,
      ),
    ).rejects.toThrow(/expected kind "document", got "image"/);
    await expect(
      resolveArtifactInputs(
        { d: { source: "logo.png", sha256: "0".repeat(64) } },
        [artifactInput({ name: "d", from_path: "input.d" })],
        policy,
      ),
    ).rejects.toThrow(/hash mismatch/);
    // A required=false missing input yields an EMPTY group (not an error).
    const optional = await resolveArtifactInputs(
      {},
      [artifactInput({ name: "d", from_path: "input.d", required: false })],
      policy,
    );
    expect(optional[0]?.artifacts).toHaveLength(0);
  });

  it("enforces the policy: source kinds, media types, size, and root boundaries", async () => {
    await expect(
      resolveArtifactInputs(
        { d: { source: { type: "url", url: "https://x/a.png" } } },
        [artifactInput({ name: "d", from_path: "input.d" })],
        artifactPolicy({ local_roots: [root] }), // default allowed_source_kinds = local_path only
      ),
    ).rejects.toThrow(/source "url" is not allowed/);
    await expect(
      resolveArtifactInputs(
        { d: "notes.txt" },
        [artifactInput({ name: "d", from_path: "input.d", media_types: ["image/*"] })],
        artifactPolicy({ local_roots: [root] }),
      ),
    ).rejects.toThrow(/media type "text\/plain" is not allowed/);
    await expect(
      resolveArtifactInputs(
        { d: "logo.png" },
        [artifactInput({ name: "d", from_path: "input.d" })],
        artifactPolicy({ local_roots: [root], max_bytes: 1 }),
      ),
    ).rejects.toThrow(/exceeds max_bytes \(4 > 1\)/);
    // Escape attempts resolve outside the root -> rejected (or nonexistent).
    await expect(
      resolveArtifactInputs(
        { d: "../../etc/passwd" },
        [artifactInput({ name: "d", from_path: "input.d" })],
        artifactPolicy({ local_roots: [root] }),
      ),
    ).rejects.toThrow(/does not exist or is not a file|outside configured artifact local_roots/);
    // No roots configured -> local sources are rejected outright.
    await expect(
      resolveArtifactInputs({ d: "logo.png" }, [artifactInput({ name: "d", from_path: "input.d" })]),
    ).rejects.toThrow(/require at least one configured artifact local_root/);
  });

  it("wildcard media patterns match and URL sources resolve without hashing", async () => {
    const groups = await resolveArtifactInputs(
      { d: { source: { type: "url", url: "https://x/photo.jpg" }, media_type: "image/jpeg" } },
      [artifactInput({ name: "d", from_path: "input.d", media_types: ["image/*"] })],
      artifactPolicy({ local_roots: [root], allowed_source_kinds: ["local_path", "url"] }),
    );
    expect(groups[0]?.artifacts[0]).toMatchObject({ source_kind: "url", kind: "image", media_type: "image/jpeg" });
    expect(groups[0]?.artifacts[0]?.sha256).toBeUndefined();
    expect(groups[0]?.artifacts[0]?.local_path).toBeUndefined();
  });
});

describe("symlink boundary semantics (#481 PR3 review)", () => {
  it("rejects a symlink inside the root that points outside (realpath, not lexical)", async () => {
    const outside = await mkdtemp(join(tmpdir(), "tf-outside-"));
    const secretPath = join(outside, "secret.txt");
    await writeFile(secretPath, "outside bytes");
    await symlink(secretPath, join(root, "escape.txt"));
    await expect(
      resolveArtifactInputs(
        { d: "escape.txt" },
        [artifactInput({ name: "d", from_path: "input.d" })],
        artifactPolicy({ local_roots: [root] }),
      ),
    ).rejects.toThrow(/outside configured artifact local_roots/);
  });

  it("accepts a real absolute path when the configured root is itself a symlink", async () => {
    const linkRoot = join(await mkdtemp(join(tmpdir(), "tf-link-")), "rootlink");
    await symlink(root, linkRoot);
    const realPng = await realpath(pngPath); // e.g. /private/tmp/... on macOS
    const groups = await resolveArtifactInputs(
      { d: realPng },
      [artifactInput({ name: "d", from_path: "input.d" })],
      artifactPolicy({ local_roots: [linkRoot] }),
    );
    expect(groups[0]?.artifacts[0]?.local_path).toBe(realPng);
  });
});
