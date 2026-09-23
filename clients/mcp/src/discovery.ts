/**
 * Manifest discovery over MCP roots (#326 Phase 0; ground truth: roots/list → glob
 * `typeflux.project.yaml`). The MCP client advertises the workspace directories it trusts as
 * "roots"; the managed-local backend walks each root (shallowly) for a `typeflux.project.yaml`
 * manifest and drives the in-process control plane against the first one found.
 *
 * A `file://` root URI maps to a directory; we look for the manifest at the root and one level
 * down (a manifest at the repo root, or in an obvious sub-package), which keeps discovery cheap
 * and predictable — no unbounded tree walk. An explicit `TYPEFLUX_MANIFEST` bypasses all of this.
 */

import { existsSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const MANIFEST_NAME = "typeflux.project.yaml";

/** A discovered project manifest and the root it came from. */
export interface DiscoveredManifest {
  manifestPath: string;
  rootUri: string;
}

/** Map a `file://` root URI to a filesystem directory path; undefined for non-file roots. */
function rootToDir(uri: string): string | undefined {
  if (!uri.startsWith("file://")) return undefined;
  try {
    return fileURLToPath(uri);
  } catch {
    return undefined;
  }
}

/** Whether a directory holds a manifest; returns its path or undefined. */
function manifestIn(dir: string): string | undefined {
  const candidate = join(dir, MANIFEST_NAME);
  return existsSync(candidate) ? candidate : undefined;
}

/**
 * Find the first `typeflux.project.yaml` across the given root URIs: at each root directory, then
 * one level down (immediate subdirectories). Returns undefined when no manifest is found.
 */
export function discoverManifest(rootUris: readonly string[]): DiscoveredManifest | undefined {
  for (const uri of rootUris) {
    const dir = rootToDir(uri);
    if (dir === undefined || !existsSync(dir)) continue;

    const atRoot = manifestIn(dir);
    if (atRoot !== undefined) return { manifestPath: atRoot, rootUri: uri };

    let children: string[];
    try {
      children = readdirSync(dir);
    } catch {
      continue;
    }
    for (const child of children.sort()) {
      const childDir = join(dir, child);
      let isDir = false;
      try {
        isDir = statSync(childDir).isDirectory();
      } catch {
        isDir = false;
      }
      if (!isDir) continue;
      const nested = manifestIn(childDir);
      if (nested !== undefined) return { manifestPath: nested, rootUri: uri };
    }
  }
  return undefined;
}
