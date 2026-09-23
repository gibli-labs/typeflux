/**
 * READ-ONLY workspace access over MCP roots (#326 Phase 3; design §6.4). The local authoring aids
 * (`scaffold_*`) read a target file to MATCH its existing style, then RETURN generated content for
 * the editor to apply as a reviewable diff — they never write. This module is the read half of that
 * boundary: it resolves a caller-supplied target path against the MCP client's advertised roots and
 * reads it. There is deliberately NO write path here — the authoring boundary (design §2, §11
 * decision 3) is that neither the control plane nor this server ever mutates the repo.
 *
 * SECURITY: a target path is caller-controlled, so we (a) map only `file://` roots to directories,
 * and (b) refuse any resolved path that escapes every advertised root (a `../../etc/passwd`
 * traversal reads nothing). A read outside the workspace roots returns `undefined`, never throws.
 */

import { closeSync, constants as fsConstants, fstatSync, openSync, readSync, realpathSync } from "node:fs";
import { isAbsolute, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

/** The most a style-match read ever touches — a starter's style is decided in the first few KB. */
const MAX_READ_BYTES = 64 * 1024;

/** List the MCP client's roots as URIs (the same seam the managed-local backend discovers over). */
export type ListRoots = () => Promise<string[]>;

/** Map `file://` root URIs to absolute directory paths; non-file roots are dropped. */
export function rootDirs(roots: readonly string[]): string[] {
  const dirs: string[] = [];
  for (const uri of roots) {
    if (!uri.startsWith("file://")) continue;
    try {
      dirs.push(resolve(fileURLToPath(uri)));
    } catch {
      /* not a usable file URI */
    }
  }
  return dirs;
}

/** Whether `candidate` (absolute) is contained within `root` (absolute) — the traversal guard. */
function within(root: string, candidate: string): boolean {
  const rel = relative(root, candidate);
  return rel === "" || (!rel.startsWith("..") && !isAbsolute(rel));
}

/** The canonicalized (realpath'd) roots, for containment comparison against a canonical target. */
function realRootDirs(roots: readonly string[], realpath: RealpathFn): string[] {
  return roots.map((root) => {
    try {
      return realpath(root);
    } catch {
      return root; // unresolvable root — compare against the literal path
    }
  });
}

/** The `realpathSync` shape — injectable so the containment logic is unit-testable without symlinks. */
export type RealpathFn = (path: string) => string;

/**
 * Whether `candidate`, FULLY canonicalized (all symlinks resolved — leaf included), lands inside a
 * canonicalized root. Realpathing the WHOLE candidate (not just its parent) is the cross-platform
 * guard: a leaf symlink pointing outside the roots resolves outside and is rejected here on EVERY OS,
 * including Windows where `O_NOFOLLOW` is a no-op. Returns undefined when the candidate can't be
 * resolved (ENOENT / a broken or escaping symlink) so the caller treats it as "not found".
 */
export function canonicalWithinRoots(
  roots: readonly string[],
  candidate: string,
  realpath: RealpathFn = realpathSync,
): string | undefined {
  let resolved: string;
  try {
    resolved = realpath(candidate); // resolves the leaf too — a symlink'd target canonicalizes away
  } catch {
    return undefined; // missing / broken / escaping — not a readable in-workspace file
  }
  const realRoots = realRootDirs(roots, realpath);
  return realRoots.some((root) => within(root, resolved)) ? resolved : undefined;
}

/** A file read from the workspace for style matching: its resolved path and text content. */
export interface WorkspaceFile {
  path: string;
  content: string;
}

/**
 * Open a candidate WITHIN the roots and read it (returns undefined on any refusal).
 *
 * Cross-platform containment + POSIX race hardening:
 *   1. {@link canonicalWithinRoots} realpaths the FULL candidate (leaf included) and requires the
 *      resolved path inside a realpath'd root — so a leaf/intermediate symlink escaping the workspace
 *      is rejected on every OS (this is the authoritative guard; it does not rely on O_NOFOLLOW).
 *   2. Open the already-canonical path with `O_NOFOLLOW` (a no-op on Windows, honored on POSIX): on
 *      POSIX this closes the small window where the resolved leaf is swapped to a symlink between
 *      realpath and open (ELOOP). It is defense-in-depth on top of (1), not the correctness guarantee.
 *   3. `fstat` the OPEN fd (not a path) to confirm a regular file, then read from that same fd.
 *
 * OUT OF SCOPE: a racer with local WRITE access swapping an intermediate directory of the canonical
 * path between realpath and open. That attacker can already read/replace the files in question
 * directly, so it grants nothing new; defending it would need per-component openat, which Node does
 * not expose.
 */
function readWithinRoots(roots: readonly string[], candidate: string): string | undefined {
  const canonical = canonicalWithinRoots(roots, candidate);
  if (canonical === undefined) return undefined;

  let fd: number;
  try {
    // O_NOFOLLOW is honored on POSIX (ELOOP on a swapped-to-symlink leaf) and undefined on Windows
    // (`?? 0` → plain O_RDONLY); Windows correctness already comes from the realpath containment above.
    fd = openSync(canonical, fsConstants.O_RDONLY | (fsConstants.O_NOFOLLOW ?? 0));
  } catch {
    return undefined;
  }
  try {
    if (!fstatSync(fd).isFile()) return undefined; // fstat the OPEN fd, not a path
    const buffer = Buffer.allocUnsafe(MAX_READ_BYTES);
    const read = readSync(fd, buffer, 0, MAX_READ_BYTES, 0);
    return buffer.subarray(0, read).toString("utf8");
  } finally {
    closeSync(fd);
  }
}

/**
 * Read `targetPath` from within the client's roots, for STYLE MATCHING ONLY (never writes). An
 * absolute target must be contained in a root; a relative target is resolved against each root, the
 * first existing match wins. Anything that resolves outside every root, is a symlink escaping the
 * roots, or does not exist yields `undefined` — the scaffold tool then falls back to the caller's
 * declared style with no file read. A missing/oversized/unreadable file is undefined, never an error.
 */
export async function readWorkspaceFile(
  listRoots: ListRoots,
  targetPath: string,
): Promise<WorkspaceFile | undefined> {
  if (targetPath === "") return undefined;
  let roots: string[];
  try {
    roots = rootDirs(await listRoots());
  } catch {
    return undefined;
  }
  if (roots.length === 0) return undefined;

  // Cheap string-level prefilter (a `../` that never even names a root is dropped without a syscall);
  // the authoritative symlink-aware guard is readWithinRoots → canonicalWithinRoots.
  const candidates: string[] = [];
  if (isAbsolute(targetPath)) {
    const abs = resolve(targetPath);
    if (roots.some((root) => within(root, abs))) candidates.push(abs);
  } else {
    for (const root of roots) {
      const abs = resolve(root, targetPath);
      if (within(root, abs)) candidates.push(abs);
    }
  }

  for (const abs of candidates) {
    const content = readWithinRoots(roots, abs);
    if (content !== undefined) return { path: abs, content };
  }
  return undefined;
}
