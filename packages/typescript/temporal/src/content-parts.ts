/**
 * Multimodal message content parts (parity Epic 6, #453) — the content-part
 * representation from Python `core/artifacts.py`, the part that touches
 * `ChatMessage`. A message's content is either a plain string or a sequence of
 * tagged parts (text + artifact references). The heavier artifact-resolution
 * subsystem (sources/kinds/media/sha256) belongs to the worker and is not here.
 *
 * `contentPartPayload(string) === string`, so widening message content keeps
 * string-content hashing byte-identical (the cross-SDK message hash is unaffected).
 */

export interface TextPart {
  type: "text";
  text: string;
}

export interface ArtifactPart {
  type: "artifact";
  artifact: string;
  text?: string;
}

export interface ArtifactGroupPart {
  type: "artifact_group";
  group: string;
  text?: string;
}

export interface ProviderExtensionPart {
  type: "provider_extension";
  provider: string;
  payload: Record<string, unknown>;
}

export type ContentPart = TextPart | ArtifactPart | ArtifactGroupPart | ProviderExtensionPart;

/** A message's content: a plain string, or a sequence of content parts. */
export type ChatContent = string | ContentPart[];

/**
 * Serialize content for the message hash, matching Python `content_part_payload`:
 * a string stays a string (so string-content hashing is unchanged); parts become an
 * array of plain objects with `text` dropped when absent.
 */
export function contentPartPayload(content: ChatContent): unknown {
  if (typeof content === "string") {
    return content;
  }
  return content.map((part): Record<string, unknown> => {
    switch (part.type) {
      case "text":
        return { type: part.type, text: part.text };
      case "artifact":
        return dropUndefined({ type: part.type, artifact: part.artifact, text: part.text });
      case "artifact_group":
        return dropUndefined({ type: part.type, group: part.group, text: part.text });
      case "provider_extension":
        return { type: part.type, provider: part.provider, payload: { ...part.payload } };
      default: {
        // Exhaustiveness: a new ContentPart type must be handled here, not silently
        // serialized as a provider extension.
        const unreachable: never = part;
        return unreachable;
      }
    }
  });
}

/**
 * Apply `renderText` to the text of a message's content (parity with Python
 * `render_content_parts`): a string is rendered directly; in a parts list, only the
 * `text` of text / artifact / artifact-group parts is rendered — other parts pass
 * through unchanged.
 */
export function renderContentParts(
  content: ChatContent,
  renderText: (text: string) => string,
): ChatContent {
  if (typeof content === "string") {
    return renderText(content);
  }
  return content.map((part): ContentPart => {
    if (part.type === "text") {
      return { type: "text", text: renderText(part.text) };
    }
    if (part.type === "artifact" && part.text !== undefined) {
      return { ...part, text: renderText(part.text) };
    }
    if (part.type === "artifact_group" && part.text !== undefined) {
      return { ...part, text: renderText(part.text) };
    }
    return part;
  });
}

function dropUndefined(obj: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(obj)) {
    if (value !== undefined) {
      out[key] = value;
    }
  }
  return out;
}
