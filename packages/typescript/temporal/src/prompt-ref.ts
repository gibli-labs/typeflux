/**
 * Prompt-ref contract (#390): the reference an AI activity uses to resolve a
 * prompt from a registry, mirroring the Python `PromptRef`.
 *
 * `version` pins an immutable registry version; `label` selects a mutable label
 * (e.g. `production`, `canary`) — the two are mutually exclusive. `promptType`
 * is a resolve-time hint (text vs chat) and is NOT part of the serialized
 * contract: `promptRefToDict` emits only `name`/`version`/`label`.
 */

export type PromptType = "auto" | "text" | "chat";

export interface PromptRef {
  name: string;
  version?: number | null;
  label?: string | null;
  /** Resolve-time hint; applied at resolution, never serialized. */
  promptType?: PromptType;
}

export interface PromptRefDict {
  name: string;
  version: number | null;
  label: string | null;
}

/** Serialize a `PromptRef` to its wire contract (`name`/`version`/`label`). */
export function promptRefToDict(ref: PromptRef): PromptRefDict {
  if (ref.version != null && ref.label != null) {
    throw new Error(
      "PromptRef.version and PromptRef.label are mutually exclusive; " +
        "pin an immutable registry version or select a mutable label, not both",
    );
  }
  return { name: ref.name, version: ref.version ?? null, label: ref.label ?? null };
}
