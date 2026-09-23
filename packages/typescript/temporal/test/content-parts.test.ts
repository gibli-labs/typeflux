import { describe, expect, it } from "vitest";

import type { ChatMessage, ContentPart } from "../src/index.js";
import { contentPartPayload, messagesHash, renderContentParts, renderMessages } from "../src/index.js";

describe("content parts hashing (#453)", () => {
  it("keeps string-content message hashing byte-identical (cross-SDK pinned)", () => {
    // This exact hash is pinned in contracts/manifest/golden/activity_execution.json
    // (prompt_messages_hash). Widening content to parts must NOT change it.
    const hash = messagesHash([{ role: "user", content: "Classify: {{ text }}" }]);
    expect(hash).toBe("9010add6d5d83db49e562a713eff3255a10a91ad76c552b5d70d21fe7ff64164");
  });

  it("serializes content parts deterministically (Python content_part_payload shape)", () => {
    const parts: ContentPart[] = [
      { type: "text", text: "Describe this:" },
      { type: "artifact", artifact: "img-1" }, // no text -> dropped
      { type: "artifact", artifact: "img-2", text: "Figure 2" },
      { type: "artifact_group", group: "exhibits" },
      { type: "provider_extension", provider: "gemini", payload: { cachedContent: "cc/1" } },
    ];
    expect(contentPartPayload(parts)).toEqual([
      { type: "text", text: "Describe this:" },
      { type: "artifact", artifact: "img-1" },
      { type: "artifact", artifact: "img-2", text: "Figure 2" },
      { type: "artifact_group", group: "exhibits" },
      { type: "provider_extension", provider: "gemini", payload: { cachedContent: "cc/1" } },
    ]);
    // A parts message hashes deterministically (and differently from a string).
    const a = messagesHash([{ role: "user", content: parts }]);
    expect(a).toBe(messagesHash([{ role: "user", content: parts }]));
    expect(a).not.toBe(messagesHash([{ role: "user", content: "Describe this:" }]));
  });

  it("string content serializes to the string (unchanged)", () => {
    expect(contentPartPayload("hello")).toBe("hello");
  });
});

describe("content parts rendering (#453)", () => {
  it("renders text/artifact text parts and leaves others intact", () => {
    const parts: ContentPart[] = [
      { type: "text", text: "Claim {{ id }}" },
      { type: "artifact", artifact: "doc-1", text: "see {{ id }}" },
      { type: "provider_extension", provider: "p", payload: { k: "{{ id }}" } }, // NOT rendered
    ];
    const rendered = renderContentParts(parts, (t) => t.replace("{{ id }}", "CLM-1")) as ContentPart[];
    expect(rendered).toEqual([
      { type: "text", text: "Claim CLM-1" },
      { type: "artifact", artifact: "doc-1", text: "see CLM-1" },
      { type: "provider_extension", provider: "p", payload: { k: "{{ id }}" } },
    ]);
  });

  it("renderMessages substitutes vars in the text parts of a parts message", () => {
    const messages: ChatMessage[] = [
      { role: "system", content: "no vars" },
      { role: "user", content: [{ type: "text", text: "Ticket {{ id }}" }, { type: "artifact", artifact: "a1" }] },
    ];
    const out = renderMessages(messages, { id: "T-9" });
    expect(out[0]?.content).toBe("no vars");
    expect(out[1]?.content).toEqual([
      { type: "text", text: "Ticket T-9" },
      { type: "artifact", artifact: "a1" },
    ]);
  });
});
