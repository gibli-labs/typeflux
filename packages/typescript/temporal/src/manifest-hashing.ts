/**
 * Manifest hash primitives (#390), mirroring the Python `manifests/hashing.py`.
 *
 * These are the leaf, content-addressed hashes the cross-SDK manifest is
 * anchored on — and the same values the cache key (#391) consumes as
 * `input_schema_hash` / `rendered_messages_hash`. Both are
 * `sha256(canonicalJson(...))`, so they reproduce the Python values
 * byte-for-byte for fixed inputs.
 *
 * The composite manifest hashes (`activity_manifest_hash`, the execution
 * `manifest_hash`, `workflow_contract_hash`) are assembled from the full
 * manifest builder and are tracked separately.
 */

import { createHash } from "node:crypto";

import { canonicalJson } from "./canonical-json.js";
import { type ChatContent, contentPartPayload } from "./content-parts.js";

export interface ChatMessage {
  role: string;
  /** A plain string, or a sequence of multimodal content parts (#453). */
  content: ChatContent;
  name?: string;
}

function sha256Hex(value: string): string {
  return createHash("sha256").update(value, "utf-8").digest("hex");
}

/** `sha256` over the canonical JSON of a JSON Schema (mirrors `schema_hash`). */
export function schemaHash(schema: Record<string, unknown>): string {
  return sha256Hex(canonicalJson(schema));
}

function messagePayload(message: ChatMessage): Record<string, unknown> {
  // `contentPartPayload` returns a string unchanged for string content, so the
  // message hash is byte-identical to the pre-multimodal serialization.
  const payload: Record<string, unknown> = {
    role: message.role,
    content: contentPartPayload(message.content),
  };
  if (message.name != null) {
    payload["name"] = message.name;
  }
  return payload;
}

/** `sha256` over the canonical JSON of the message payloads (mirrors `messages_hash`). */
export function messagesHash(messages: ChatMessage[]): string {
  return sha256Hex(canonicalJson(messages.map(messagePayload)));
}
