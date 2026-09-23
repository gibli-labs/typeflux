// Per-subject crypto-shred keystore + subject-scoped codec seam (#715 slice 4).
//
// Keystore lifecycle (mint-on-first-use, destroy, destroy-then-encode fail-closed,
// destroy-then-decode DISTINCT shred error, unknown subject, defensive copies), the
// kid scheme (canonical), the combine, the codec driven through the SDK's REAL
// serialization-context argument, the binding registry + visibility fallback, the
// WIRED build helper (`buildSubjectAwarePayloadCodec` + spec `subject_scope`), and the
// shared conformance vectors (incl. the no-subject fallback byte proof).

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import type { SerializationContext } from "@temporalio/common";
import proto from "@temporalio/proto";
import { describe, expect, it } from "vitest";

import {
  buildSubjectAwarePayloadCodec,
  ENCRYPTED_ENCODING,
  KEY_ID_METADATA_KEY,
  KEY_LEN,
  openPayloadBytes,
  PayloadCodecError,
  payloadCodecKeySpec,
  payloadCodecSpec,
  RESERVED_KID_PREFIX,
  sealPayloadBytes,
  TypefluxAesGcmPayloadCodec,
} from "../src/index.js";
import {
  combineSubjectKey,
  InMemorySubjectKeystore,
  isSubjectKeystore,
  parseSubjectKid,
  SubjectKeyBindings,
  SubjectKeyShreddedError,
  subjectKeyState,
  SubjectKeyUnknownError,
  SubjectScopedPayloadCodec,
  subjectKid,
  type SubjectBindingClient,
  type SubjectKeyDestructionResult,
  type SubjectKeystore,
} from "../src/subject-keystore.js";

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
  }[];
  subject_scoped: {
    nonce_hex: string;
    plaintext_hex: string;
    vectors: {
      name: string;
      subject_ids: string[];
      subject_record_keys_hex: Record<string, string>;
      kid: string;
      combined_sha256_hex: string;
      data_hex: string;
    }[];
    decode_after_shred: { vector_name: string; destroy_subject_id: string };
  };
};

const SHARED_KEY = Buffer.alloc(KEY_LEN, 0x11);
// A textual, obviously-synthetic 32-byte codec key for the wired-build tests
// (secret-scanner friendly: English-like low entropy, the live tests' convention).
const WIRED_TEST_KEY = "typeflux-test-subject-key-32byte";
const base = (): TypefluxAesGcmPayloadCodec =>
  new TypefluxAesGcmPayloadCodec({ currentKid: "k1", keys: new Map([["k1", SHARED_KEY]]) });
const sample = (data = '{"claim":"redact-me"}') => ({
  metadata: { encoding: Buffer.from("json/plain") },
  data: Buffer.from(data),
});

const workflowContext = (workflowId: string): SerializationContext => ({
  type: "workflow",
  namespace: "default",
  workflowId,
});

/** A codec bound the way the SDK binds it: registry entry + a context on encode. */
function boundCodec(ks: InMemorySubjectKeystore, subjects: string[], workflowId = "wf-1") {
  const codec = new SubjectScopedPayloadCodec(base(), ks);
  codec.bindings.register(workflowId, subjects);
  return {
    codec,
    encode: (payloads: Parameters<SubjectScopedPayloadCodec["encode"]>[0]) =>
      codec.encode(payloads, workflowContext(workflowId)),
  };
}

describe("subject keystore lifecycle", () => {
  it("mints on first use, stable, 32 bytes", () => {
    const ks = new InMemorySubjectKeystore();
    const key = ks.dataKey("subject-0001", { create: true });
    expect(key.length).toBe(KEY_LEN);
    expect(ks.dataKey("subject-0001", { create: true }).equals(key)).toBe(true);
    expect(ks.dataKey("subject-0002", { create: true }).equals(key)).toBe(false);
  });

  it("returns defensive copies — caller mutation cannot corrupt the stored record", () => {
    const ks = new InMemorySubjectKeystore();
    const first = ks.dataKey("subject-0001", { create: true });
    const original = Buffer.from(first);
    first.fill(0); // hostile caller zeroes the returned buffer
    const second = ks.dataKey("subject-0001", { create: false });
    expect(second.equals(original)).toBe(true);
    // And mutating the second copy does not affect a third read.
    second.fill(0xff);
    expect(ks.dataKey("subject-0001", { create: false }).equals(original)).toBe(true);
  });

  it("decode path throws unknown for a missing subject", () => {
    expect(() => new InMemorySubjectKeystore().dataKey("never", { create: false })).toThrow(SubjectKeyUnknownError);
  });

  it("destroy then encode fails closed (never re-mints)", () => {
    const ks = new InMemorySubjectKeystore();
    ks.dataKey("subject-0001", { create: true });
    const result = ks.destroySubjectKey("subject-0001");
    expect(result.keyExisted).toBe(true);
    expect(result.alreadyDestroyed).toBe(false);
    expect(() => ks.dataKey("subject-0001", { create: true })).toThrow(SubjectKeyShreddedError);
  });

  it("destroy before any key still tombstones permanently", () => {
    const ks = new InMemorySubjectKeystore();
    expect(ks.destroySubjectKey("subject-0001").keyExisted).toBe(false);
    expect(() => ks.dataKey("subject-0001", { create: true })).toThrow(SubjectKeyShreddedError);
  });

  it("destroy is idempotent", () => {
    const ks = new InMemorySubjectKeystore();
    ks.dataKey("subject-0001", { create: true });
    const first = ks.destroySubjectKey("subject-0001");
    const second = ks.destroySubjectKey("subject-0001");
    expect(first).toMatchObject({ keyExisted: true, alreadyDestroyed: false });
    expect(second).toMatchObject({ keyExisted: false, alreadyDestroyed: true });
  });

  it("destroyed key is unrecoverable through the api (both create modes throw)", () => {
    const ks = new InMemorySubjectKeystore();
    ks.dataKey("subject-0001", { create: true });
    ks.destroySubjectKey("subject-0001");
    expect(() => ks.dataKey("subject-0001", { create: false })).toThrow(SubjectKeyShreddedError);
    expect(() => ks.dataKey("subject-0001", { create: true })).toThrow(SubjectKeyShreddedError);
  });

  it("empty subject id fails closed", () => {
    const ks = new InMemorySubjectKeystore();
    expect(() => ks.dataKey("", { create: true })).toThrow(/non-empty/);
    expect(() => ks.destroySubjectKey("")).toThrow(/non-empty/);
  });

  it("isSubjectKeystore checks every method", () => {
    expect(isSubjectKeystore(new InMemorySubjectKeystore())).toBe(true);
    expect(isSubjectKeystore({ dataKey: () => Buffer.alloc(0) })).toBe(false); // missing destroy
    expect(isSubjectKeystore(null)).toBe(false);
  });
});

describe("subject key state probe (#715 slice 5)", () => {
  it("is non-minting and non-destroying across all three states", () => {
    const ks = new InMemorySubjectKeystore();
    expect(subjectKeyState(ks, "subject-0001")).toBe("absent");
    expect(subjectKeyState(ks, "subject-0001")).toBe("absent"); // still absent: no mint
    const key = ks.dataKey("subject-0001", { create: true });
    expect(subjectKeyState(ks, "subject-0001")).toBe("live");
    // Probing did not rotate/destroy: the same key comes back.
    expect(ks.dataKey("subject-0001", { create: false }).equals(key)).toBe(true);
    ks.destroySubjectKey("subject-0001");
    expect(subjectKeyState(ks, "subject-0001")).toBe("destroyed");
    expect(() => ks.subjectKeyState("")).toThrow(/non-empty/);
  });

  it("falls back to a read-only probe for a backend without the optional method", () => {
    const inner = new InMemorySubjectKeystore();
    const createCalls: boolean[] = [];
    const minimal: SubjectKeystore = {
      dataKey(subjectId: string, options: { create: boolean }): Buffer {
        createCalls.push(options.create);
        return inner.dataKey(subjectId, options);
      },
      destroySubjectKey(subjectId: string): SubjectKeyDestructionResult {
        return inner.destroySubjectKey(subjectId);
      },
    };
    expect(subjectKeyState(minimal, "subject-0001")).toBe("absent");
    inner.dataKey("subject-0001", { create: true });
    expect(subjectKeyState(minimal, "subject-0001")).toBe("live");
    minimal.destroySubjectKey("subject-0001");
    expect(subjectKeyState(minimal, "subject-0001")).toBe("destroyed");
    // Every probe went through the read-only path.
    expect(new Set(createCalls)).toEqual(new Set([false]));
  });
});

describe("subject kid scheme + combine", () => {
  it("round-trips kid through parse (incl. delimiter-heavy ids)", () => {
    for (const ids of [["subject-0001"], ["subject-0001", "subject-0002"], ["a:b,c=d weird"]]) {
      const kid = subjectKid(ids);
      expect(kid.startsWith("tfsubj1:")).toBe(true);
      expect(parseSubjectKid(kid)).toEqual(ids);
    }
  });

  it("rejects empty set and malformed kids", () => {
    expect(() => subjectKid([])).toThrow();
    expect(() => parseSubjectKid("key-2026")).toThrow();
    expect(() => parseSubjectKid("tfsubj1:")).toThrow();
    expect(() => parseSubjectKid("tfsubj1:!!!not-base64!!!")).toThrow();
  });

  it("rejects non-canonical segments (padded / standard alphabet) — Python parity", () => {
    expect(() => parseSubjectKid("tfsubj1:c3ViamVjdA==")).toThrow(PayloadCodecError);
    const standard = Buffer.from("subject\xfb\xff", "binary").toString("base64").replace(/=+$/, "");
    expect(standard.includes("+") || standard.includes("/")).toBe(true);
    expect(() => parseSubjectKid(`tfsubj1:${standard}`)).toThrow(PayloadCodecError);
  });

  it("combine is order-sensitive and 32 bytes", () => {
    const a = Buffer.alloc(KEY_LEN, 0x10);
    const b = Buffer.alloc(KEY_LEN, 0x20);
    const combined = combineSubjectKey([a, b]);
    expect(combined.length).toBe(KEY_LEN);
    expect(combineSubjectKey([b, a]).equals(combined)).toBe(false);
    expect(() => combineSubjectKey([])).toThrow();
    expect(() => combineSubjectKey([Buffer.from("short")])).toThrow();
  });
});

describe("reserved-prefix guard (one source of truth)", () => {
  it("spec guard and codec scheme agree on the SAME constant", () => {
    // The zod refine and the codec both consume RESERVED_KID_PREFIX — a violating id
    // is rejected by BOTH, proving the guard and the wire scheme cannot drift.
    const violating = `${RESERVED_KID_PREFIX}foo`;
    expect(payloadCodecKeySpec.safeParse({ id: violating, value_from: { env: "X" } }).success).toBe(false);
    expect(
      () => new TypefluxAesGcmPayloadCodec({ currentKid: violating, keys: new Map([[violating, SHARED_KEY]]) }),
    ).toThrow(/reserved/);
  });
});

describe("subject-scoped codec through the SDK context seam", () => {
  it("no-subject execution uses the shared-key path (byte-compatible)", async () => {
    const { encode } = boundCodec(new InMemorySubjectKeystore(), []);
    const enc = (await encode([sample()]))[0]!;
    expect(Buffer.from(enc.metadata![KEY_ID_METADATA_KEY]!).toString("utf8")).toBe("k1");
    expect(Buffer.from(enc.metadata!.encoding!).toString("utf8")).toBe(ENCRYPTED_ENCODING);
    // A plain shared-key codec decodes it (proves the wire is the shared format).
    expect(Buffer.from((await base().decode([enc]))[0]!.data!).toString("utf8")).toBe('{"claim":"redact-me"}');
  });

  it("single-subject round-trip stamps the subject kid", async () => {
    const { codec, encode } = boundCodec(new InMemorySubjectKeystore(), ["subject-0001"]);
    const enc = (await encode([sample()]))[0]!;
    expect(Buffer.from(enc.metadata![KEY_ID_METADATA_KEY]!).toString("utf8")).toBe(subjectKid(["subject-0001"]));
    const dec = (await codec.decode([enc]))[0]!;
    expect(Buffer.from(dec.data!).toString("utf8")).toBe('{"claim":"redact-me"}');
  });

  it("multi-subject round-trip; destroying either member shreds the payload", async () => {
    const ks = new InMemorySubjectKeystore();
    const { codec, encode } = boundCodec(ks, ["subject-0001", "subject-0002"]);
    const enc = await encode([sample()]);
    expect(Buffer.from((await codec.decode(enc))[0]!.data!).toString("utf8")).toBe('{"claim":"redact-me"}');
    ks.destroySubjectKey("subject-0002");
    await expect(codec.decode(enc)).rejects.toThrow(SubjectKeyShreddedError);
  });

  it("decode after shred throws the distinct error — which extends PayloadCodecError", async () => {
    const ks = new InMemorySubjectKeystore();
    const { codec, encode } = boundCodec(ks, ["subject-0001"]);
    const enc = await encode([sample()]);
    ks.destroySubjectKey("subject-0001");
    const failure = await codec.decode(enc).then(
      () => undefined,
      (error: unknown) => error,
    );
    expect(failure).toBeInstanceOf(SubjectKeyShreddedError);
    // Broad fail-closed handlers that catch the codec base type still catch the shred.
    expect(failure).toBeInstanceOf(PayloadCodecError);
  });

  it("decode mixes subject-scoped, shared, and passthrough — and needs no binding", async () => {
    const ks = new InMemorySubjectKeystore();
    const { encode } = boundCodec(ks, ["subject-0001"]);
    const subjectPayload = (await encode([sample("subj")]))[0]!;
    const sharedPayload = (await base().encode([sample("shared")]))[0]!;
    const plainPayload = sample("plain"); // no encrypted marker → passthrough
    // A FRESH unbound codec over the same keystore decodes all three (kid-driven).
    const reader = new SubjectScopedPayloadCodec(base(), ks);
    const out = await reader.decode([subjectPayload, sharedPayload, plainPayload]);
    expect(out.map((p) => Buffer.from(p.data!).toString("utf8"))).toEqual(["subj", "shared", "plain"]);
  });

  it("encode without a serialization context fails closed", async () => {
    const codec = new SubjectScopedPayloadCodec(base(), new InMemorySubjectKeystore());
    await expect(codec.encode([sample()])).rejects.toThrow(/serialization context/);
  });

  it("malformed registered subjects fail closed", () => {
    const codec = new SubjectScopedPayloadCodec(base(), new InMemorySubjectKeystore());
    expect(() => codec.bindings.register("wf-1", ["", "ok"])).toThrow(/non-empty/);
  });
});

describe("subject bindings registry + visibility fallback", () => {
  const fakeClient = (subjects: Record<string, string[] | undefined>, fail = false) => {
    const describes: string[] = [];
    const client: SubjectBindingClient = {
      workflow: {
        getHandle: (workflowId: string) => ({
          describe: async () => {
            describes.push(workflowId);
            if (fail) throw new Error("visibility down");
            if (!(workflowId in subjects)) throw new Error("workflow not found");
            const value = subjects[workflowId];
            return {
              typedSearchAttributes: {
                get: (key: unknown) =>
                  (key as { name?: string })?.name === "TypefluxSubjectIds" ? value : undefined,
              },
            };
          },
        }),
      },
    };
    return { client, describes };
  };

  it("registry wins without a describe", async () => {
    const bindings = new SubjectKeyBindings();
    bindings.register("wf-1", ["subject-0001"]);
    expect(await bindings.resolve("wf-1")).toEqual(["subject-0001"]);
  });

  it("visibility fallback reads TypefluxSubjectIds and caches", async () => {
    const bindings = new SubjectKeyBindings();
    const { client, describes } = fakeClient({ "wf-9": ["subject-0001", "subject-0002"] });
    bindings.bindClient(client);
    expect(await bindings.resolve("wf-9")).toEqual(["subject-0001", "subject-0002"]);
    expect(await bindings.resolve("wf-9")).toEqual(["subject-0001", "subject-0002"]);
    expect(describes).toEqual(["wf-9"]);
  });

  it("a describe without the attribute pins no subjects", async () => {
    const bindings = new SubjectKeyBindings();
    bindings.bindClient(fakeClient({ "wf-plain": undefined }).client);
    expect(await bindings.resolve("wf-plain")).toEqual([]);
  });

  it("a miss without a client fails closed", async () => {
    await expect(new SubjectKeyBindings().resolve("wf-unknown")).rejects.toThrow(/fails closed/);
  });

  it("a describe failure fails closed with the child-workflow hint", async () => {
    const bindings = new SubjectKeyBindings();
    bindings.bindClient(fakeClient({}, true).client);
    await expect(bindings.resolve("wf-child")).rejects.toThrow(/sub-workflow composition/);
  });
});

describe("wired build helper (spec subject_scope → subject-scoped codec)", () => {
  const specWith = (subjectScope: boolean) =>
    payloadCodecSpec.parse({
      type: "aes",
      current: "k1",
      keys: [{ id: "k1", value_from: { env: "TF_SUBJ_CODEC_KEY" } }],
      ...(subjectScope ? { subject_scope: {} } : {}),
    });

  it("spec materializes the in_memory default (zod-default parity rule)", () => {
    const spec = specWith(true);
    expect(spec.subject_scope).toEqual({ keystore: "in_memory" });
  });

  it("builds the wrapped codec with an injected keystore, plain otherwise", () => {
    process.env.TF_SUBJ_CODEC_KEY = WIRED_TEST_KEY;
    try {
      expect(buildSubjectAwarePayloadCodec(specWith(true), new InMemorySubjectKeystore())).toBeInstanceOf(
        SubjectScopedPayloadCodec,
      );
      const plain = buildSubjectAwarePayloadCodec(specWith(false));
      expect(plain).toBeInstanceOf(TypefluxAesGcmPayloadCodec);
      expect(plain).not.toBeInstanceOf(SubjectScopedPayloadCodec);
      expect(buildSubjectAwarePayloadCodec(undefined)).toBeUndefined();
    } finally {
      delete process.env.TF_SUBJ_CODEC_KEY;
    }
  });

  it("fails closed when subject_scope is declared with NO keystore (#715 Bugbot)", () => {
    // Never a silently-minted process-local keystore: a caller that is not the sole
    // owner of subject key records would seal payloads no worker can decode.
    process.env.TF_SUBJ_CODEC_KEY = WIRED_TEST_KEY;
    try {
      expect(() => buildSubjectAwarePayloadCodec(specWith(true))).toThrow(/SHARED keystore/);
    } finally {
      delete process.env.TF_SUBJ_CODEC_KEY;
    }
  });

  it("e2e: wired codec + registered start binding seals, shreds, and stays byte-compatible for no-subject", async () => {
    process.env.TF_SUBJ_CODEC_KEY = WIRED_TEST_KEY;
    try {
      const ks = new InMemorySubjectKeystore();
      const codec = buildSubjectAwarePayloadCodec(specWith(true), ks) as SubjectScopedPayloadCodec;
      // The start path's registration (runWorkflow does exactly this).
      codec.bindings.register("wf-subj", ["subject-0001"]);
      codec.bindings.register("wf-plain", []);
      // Subject execution: sealed under the subject kid; destroy → distinct shred.
      const enc = await codec.encode([sample()], workflowContext("wf-subj"));
      expect(Buffer.from(enc[0]!.metadata![KEY_ID_METADATA_KEY]!).toString("utf8")).toBe(
        subjectKid(["subject-0001"]),
      );
      expect(Buffer.from((await codec.decode(enc))[0]!.data!).toString("utf8")).toBe('{"claim":"redact-me"}');
      ks.destroySubjectKey("subject-0001");
      await expect(codec.decode(enc)).rejects.toThrow(SubjectKeyShreddedError);
      await expect(codec.encode([sample()], workflowContext("wf-subj"))).rejects.toThrow(SubjectKeyShreddedError);
      // No-subject execution through the SAME wired codec: shared-key wire the
      // UNWRAPPED codec opens.
      const plainEnc = await codec.encode([sample()], workflowContext("wf-plain"));
      expect(Buffer.from(plainEnc[0]!.metadata![KEY_ID_METADATA_KEY]!).toString("utf8")).toBe("k1");
      const unwrapped = new TypefluxAesGcmPayloadCodec({
        currentKid: "k1",
        keys: new Map([["k1", Buffer.from(WIRED_TEST_KEY, "utf8")]]),
      });
      expect(Buffer.from((await unwrapped.decode(plainEnc))[0]!.data!).toString("utf8")).toBe(
        '{"claim":"redact-me"}',
      );
    } finally {
      delete process.env.TF_SUBJ_CODEC_KEY;
    }
  });
});

describe("subject-scoped conformance vectors (byte-pinned, shared vector)", () => {
  it("reproduces the pinned combined key + ciphertext and opens it back", () => {
    const ss = vectors.subject_scoped;
    const nonce = Buffer.from(ss.nonce_hex, "hex");
    const plaintext = Buffer.from(ss.plaintext_hex, "hex");
    for (const vector of ss.vectors) {
      const recordKeys = vector.subject_ids.map((id) => Buffer.from(vector.subject_record_keys_hex[id]!, "hex"));
      const combined = combineSubjectKey(recordKeys);
      expect(combined.toString("hex")).toBe(vector.combined_sha256_hex);
      expect(subjectKid(vector.subject_ids)).toBe(vector.kid);
      const sealed = sealPayloadBytes({ key: combined, kid: vector.kid, nonce, plaintext });
      expect(Buffer.from(sealed.data!).toString("hex")).toBe(vector.data_hex);
      expect(openPayloadBytes({ key: combined, data: Buffer.from(vector.data_hex, "hex") }).toString("hex")).toBe(
        ss.plaintext_hex,
      );
    }
  });

  it("routes a destroyed subject to the distinct shred error (never plaintext)", async () => {
    const ss = vectors.subject_scoped;
    const target = ss.vectors.find((v) => v.name === ss.decode_after_shred.vector_name)!;
    const ks = new InMemorySubjectKeystore();
    for (const [subjectId, keyHex] of Object.entries(target.subject_record_keys_hex)) {
      ks.seedRecordForTest(subjectId, Buffer.from(keyHex, "hex"));
    }
    const codec = new SubjectScopedPayloadCodec(base(), ks);
    const sealedPayload = {
      metadata: {
        encoding: Buffer.from(ENCRYPTED_ENCODING, "utf8"),
        [KEY_ID_METADATA_KEY]: Buffer.from(target.kid, "utf8"),
      },
      data: Buffer.from(target.data_hex, "hex"),
    };
    ks.destroySubjectKey(ss.decode_after_shred.destroy_subject_id);
    await expect(codec.decode([sealedPayload])).rejects.toThrow(SubjectKeyShreddedError);
  });

  it("the pinned SHARED vectors run through the subject-scoped wrapper's no-subject fallback", async () => {
    // The binding note's byte-for-byte fallback claim, proven with the pinned bytes:
    // decode of the shared vectors' wire bytes THROUGH the wrapper opens to the pinned
    // plaintext, and encode through the wrapper's fallback stamps the pinned kid and
    // reopens under the pinned key. Any drift in the fallback path fails CI.
    for (const vector of vectors.vectors) {
      const key = Buffer.from(vector.key_hex, "hex");
      const plaintext = Buffer.from(vector.plaintext_hex, "hex");
      const sharedBase = new TypefluxAesGcmPayloadCodec({
        currentKid: vector.kid,
        keys: new Map([[vector.kid, key]]),
      });
      const wrapper = new SubjectScopedPayloadCodec(sharedBase, new InMemorySubjectKeystore());
      wrapper.bindings.register("wf-no-subjects", []);
      // DECODE of the pinned wire bytes through the wrapper (byte proof).
      const pinned = {
        metadata: {
          encoding: Buffer.from(ENCRYPTED_ENCODING, "utf8"),
          [KEY_ID_METADATA_KEY]: Buffer.from(vector.kid, "utf8"),
        },
        data: Buffer.from(vector.data_hex, "hex"),
      };
      const opened = (await wrapper.decode([pinned]))[0]!;
      expect(Buffer.from(PayloadProto.encode(opened).finish()).toString("hex")).toBe(vector.plaintext_hex);
      // ENCODE through the wrapper's fallback: shared kid stamped, opens under the
      // pinned key back to the pinned plaintext (nonce is random, so prove via open).
      const inner = PayloadProto.decode(plaintext);
      const sealed = (await wrapper.encode([inner], workflowContext("wf-no-subjects")))[0]!;
      expect(Buffer.from(sealed.metadata![KEY_ID_METADATA_KEY]!).toString("utf8")).toBe(vector.kid);
      expect(openPayloadBytes({ key, data: Buffer.from(sealed.data!) }).toString("hex")).toBe(
        vector.plaintext_hex,
      );
    }
  });
});
