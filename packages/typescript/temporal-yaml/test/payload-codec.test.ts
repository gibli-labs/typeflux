// AES-256-GCM Temporal payload codec (#188 slice 1).
//
// Round-trip + fail-closed unit tests, spec validation, the SECRET_SLOT_PATHS masking proof,
// and the CROSS-EDITION conformance vector (the load-bearing proof that the TS and Python
// codecs share a byte-identical wire format — the SAME vector file the Python suite asserts).

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import proto from "@temporalio/proto";
import { describe, expect, it } from "vitest";

import {
  buildPayloadCodec,
  ENCRYPTED_ENCODING,
  KEY_ID_METADATA_KEY,
  NONCE_LEN,
  openPayloadBytes,
  payloadCodecSpec,
  sealPayloadBytes,
  TAG_LEN,
  TypefluxAesGcmPayloadCodec,
} from "../src/index.js";
import { loadYamlSpec } from "../src/loader.js";
import { secretReferenceRecords } from "../src/secret-references.js";

const PayloadProto = proto.temporal.api.common.v1.Payload;
const here = dirname(fileURLToPath(import.meta.url));
const vectors = JSON.parse(
  readFileSync(resolve(here, "../../../../contracts/temporal-binding/payload-codec-vectors.json"), "utf-8"),
) as {
  vectors: {
    name: string;
    key_hex: string;
    kid: string;
    nonce_hex: string;
    plaintext_hex: string;
    data_hex: string;
    inner_payload: { metadata: Record<string, string>; data_utf8: string };
  }[];
};

const KEY = Buffer.concat([Buffer.alloc(16, 0), Buffer.alloc(16, 0x11)]); // 32 bytes
const codec = (): TypefluxAesGcmPayloadCodec =>
  new TypefluxAesGcmPayloadCodec({ currentKid: "k1", keys: new Map([["k1", KEY]]) });
const sample = () => ({ metadata: { encoding: Buffer.from("json/plain") }, data: Buffer.from('{"claim":"redact-me"}') });

describe("AES-256-GCM payload codec round trip", () => {
  it("encrypts and decrypts back to the original", async () => {
    const c = codec();
    const original = sample();
    const encrypted = (await c.encode([original]))[0]!;
    expect(Buffer.from(encrypted.metadata!.encoding!).toString("utf8")).toBe(ENCRYPTED_ENCODING);
    expect(Buffer.from(encrypted.metadata![KEY_ID_METADATA_KEY]!).toString("utf8")).toBe("k1");
    const decrypted = (await c.decode([encrypted]))[0]!;
    expect(Buffer.from(decrypted.data!).toString("utf8")).toBe('{"claim":"redact-me"}');
    expect(Buffer.from(decrypted.metadata!.encoding!).toString("utf8")).toBe("json/plain");
  });

  it("uses a fresh nonce per encrypt (same plaintext ⇒ different ciphertext)", async () => {
    const c = codec();
    const first = (await c.encode([sample()]))[0]!;
    const second = (await c.encode([sample()]))[0]!;
    expect(Buffer.from(first.data!).equals(Buffer.from(second.data!))).toBe(false);
  });

  it("passes a non-encrypted payload through decode untouched", async () => {
    const plain = sample();
    const out = (await codec().decode([plain]))[0]!;
    expect(Buffer.from(out.data!).toString("utf8")).toBe('{"claim":"redact-me"}');
    expect(Buffer.from(out.metadata!.encoding!).toString("utf8")).toBe("json/plain");
  });

  it("decrypts old-key history after a current-key rotation", async () => {
    const old = Buffer.alloc(32, 0x61);
    const fresh = Buffer.alloc(32, 0x62);
    const encrypting = new TypefluxAesGcmPayloadCodec({ currentKid: "old", keys: new Map([["old", old]]) });
    const encrypted = (await encrypting.encode([sample()]))[0]!;
    const rotated = new TypefluxAesGcmPayloadCodec({
      currentKid: "new",
      keys: new Map([
        ["new", fresh],
        ["old", old],
      ]),
    });
    const decrypted = (await rotated.decode([encrypted]))[0]!;
    expect(Buffer.from(decrypted.data!).toString("utf8")).toBe('{"claim":"redact-me"}');
  });
});

describe("AES-256-GCM payload codec fail-closed", () => {
  it("fails closed on an unknown key id", async () => {
    const encrypted = sealPayloadBytes({
      key: Buffer.alloc(32, 0x7a),
      kid: "unregistered",
      nonce: Buffer.alloc(NONCE_LEN, 1),
      plaintext: Buffer.from("x"),
    });
    await expect(codec().decode([encrypted])).rejects.toThrow(/no key registered/);
  });

  it("fails authentication on tampered ciphertext", async () => {
    const encrypted = (await codec().encode([sample()]))[0]!;
    const data = Buffer.from(encrypted.data!);
    data[NONCE_LEN + 2] = data[NONCE_LEN + 2]! ^ 0x01;
    await expect(codec().decode([{ ...encrypted, data }])).rejects.toThrow(/authentication/);
  });

  it("rejects a wrong-length key at construction", () => {
    expect(() => new TypefluxAesGcmPayloadCodec({ currentKid: "k1", keys: new Map([["k1", Buffer.alloc(8)]]) })).toThrow(
      /32 bytes/,
    );
  });

  it("rejects a current id that is not among the keys", () => {
    expect(() => new TypefluxAesGcmPayloadCodec({ currentKid: "missing", keys: new Map([["k1", KEY]]) })).toThrow(
      /current key id/,
    );
  });
});

describe("payload codec spec + build", () => {
  it("builds from an env-sourced key", async () => {
    const spec = payloadCodecSpec.parse({
      type: "aes",
      current: "k1",
      keys: [{ id: "k1", value_from: { env: "TF_CODEC_KEY" } }],
    });
    const built = buildPayloadCodec(spec, { TF_CODEC_KEY: ("01234567" + "89abcdef").repeat(2) });
    expect(built).toBeDefined();
    const decrypted = (await built!.decode(await built!.encode([sample()])))[0]!;
    expect(Buffer.from(decrypted.data!).toString("utf8")).toBe('{"claim":"redact-me"}');
  });

  it("fails closed when a required key is unresolved", () => {
    const spec = payloadCodecSpec.parse({
      type: "aes",
      current: "k1",
      keys: [{ id: "k1", value_from: { env: "TF_CODEC_MISSING" } }],
    });
    expect(() => buildPayloadCodec(spec, {})).toThrow(/is not set/);
  });

  it("fails closed on a wrong-length env key", () => {
    const spec = payloadCodecSpec.parse({
      type: "aes",
      current: "k1",
      keys: [{ id: "k1", value_from: { env: "TF_CODEC_SHORT" } }],
    });
    expect(() => buildPayloadCodec(spec, { TF_CODEC_SHORT: "too-short" })).toThrow(/32 bytes/);
  });

  it("returns undefined when the block is absent", () => {
    expect(buildPayloadCodec(undefined)).toBeUndefined();
  });

  it("rejects current not naming a key, empty keys, and duplicate ids", () => {
    expect(payloadCodecSpec.safeParse({ type: "aes", current: "x", keys: [{ id: "k1", value_from: { env: "A" } }] }).success).toBe(
      false,
    );
    expect(payloadCodecSpec.safeParse({ type: "aes", current: "k1", keys: [] }).success).toBe(false);
    expect(
      payloadCodecSpec.safeParse({
        type: "aes",
        current: "k1",
        keys: [
          { id: "k1", value_from: { env: "A" } },
          { id: "k1", value_from: { env: "B" } },
        ],
      }).success,
    ).toBe(false);
  });
});

describe("SECRET_SLOT_PATHS masking", () => {
  it("records a codec key as source_kind/source_name, never the value", () => {
    const spec = loadYamlSpec(
      "project: p\nname: n\ntask_queue: q\nruntime:\n" +
        "  temporal:\n    address: localhost:7233\n    payload_codec:\n      type: aes\n      current: main\n" +
        "      keys:\n        - { id: main, value_from: { env: TF_CODEC_KEY } }\n" +
        "  registry: { type: inline, prompts: { p/x: hi } }\n" +
        "  provider: { type: fake }\n" +
        "activities:\n  definitions:\n    - { name: a, input: schemas:In, output: schemas:Out, prompt: p/x }\n" +
        "workflow:\n  name: W\n  input: schemas:In\n  steps:\n    - { id: s1, activity: a }\n",
      { env: { TF_CODEC_KEY: ("01234567" + "89abcdef").repeat(2) } },
    );
    const records = secretReferenceRecords(spec);
    const codecRecords = records.filter((r) => r.runtime_path.includes("payload_codec"));
    expect(codecRecords).toHaveLength(1);
    expect(codecRecords[0]!).toMatchObject({
      runtime_path: "runtime.temporal.payload_codec.keys[main].value_from",
      source_kind: "env",
      source_name: "TF_CODEC_KEY",
    });
  });
});

describe("cross-edition conformance (byte-pinned, shared vector)", () => {
  it("reproduces the pinned ciphertext and opens the Python-sealed bytes", () => {
    for (const vector of vectors.vectors) {
      const key = Buffer.from(vector.key_hex, "hex");
      const nonce = Buffer.from(vector.nonce_hex, "hex");
      const plaintext = Buffer.from(vector.plaintext_hex, "hex");
      const sealed = sealPayloadBytes({ key, kid: vector.kid, nonce, plaintext });
      // Byte-identical to the pinned wire bytes (the Python edition asserts the SAME).
      expect(Buffer.from(sealed.data!).toString("hex")).toBe(vector.data_hex);
      expect(Buffer.from(sealed.metadata!.encoding!).toString("utf8")).toBe(ENCRYPTED_ENCODING);
      expect(Buffer.from(sealed.metadata![KEY_ID_METADATA_KEY]!).toString("utf8")).toBe(vector.kid);
      // And the pinned ciphertext opens back to the exact plaintext (decrypt interop).
      expect(openPayloadBytes({ key, data: Buffer.from(vector.data_hex, "hex") }).toString("hex")).toBe(
        vector.plaintext_hex,
      );
      // The inner proto parses to the described Payload.
      const inner = PayloadProto.decode(plaintext);
      expect(Buffer.from(inner.metadata!.encoding!).toString("utf8")).toBe(vector.inner_payload.metadata.encoding);
      expect(Buffer.from(inner.data!).toString("utf8")).toBe(vector.inner_payload.data_utf8);
    }
  });

  it("agrees on the nonce and tag lengths", () => {
    expect(NONCE_LEN).toBe(12);
    expect(TAG_LEN).toBe(16);
  });
});
