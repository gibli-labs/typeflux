/**
 * Shared type guards used across the control-plane core and its HTTP adapter. Kept in one place so a
 * predicate the enforcement extractor and the FastAPI-parity body validator both rely on cannot drift
 * between them (the two used to carry byte-identical private copies).
 */

/** A plain (non-array, non-null) object — the JSON-object shape every metadata/body walk narrows to. */
export const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);
