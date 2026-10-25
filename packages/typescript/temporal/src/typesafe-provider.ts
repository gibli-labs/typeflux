/** Native Jev questions in the system prompt; user text supplies their shared state. */
import {
  callOptions, mergeProviderParams, ProviderAuthError, ProviderConfigError,
  ProviderRateLimitError, ProviderTransientError, retryAfterSecondsFrom,
  type ModelProvider, type StructuredCallParams, type TransportCallOptions,
} from "./execute.js";

export const TYPESAFE_BASE_URL = "https://api.typesafe.ai";
export const DEFAULT_TYPESAFE_MODEL = "jev-1.14.0";
export const TYPESAFE_PARAMS: ReadonlySet<string> = new Set(["model", "timeout"]);
export type TypeSafeJsonValue = string | number | boolean | null | TypeSafeJsonValue[] | { [key: string]: TypeSafeJsonValue };
export type TypeSafeContent = string | { [key: string]: TypeSafeJsonValue } | TypeSafeJsonValue[];
export type TypeSafeQuestion =
  | { type: "choice"; instructions: TypeSafeContent; criteria: Record<string, TypeSafeContent | null> }
  | { type: "score"; instructions: TypeSafeContent; criteria: [TypeSafeContent, TypeSafeContent, ...TypeSafeContent[]] }
  | { type: "noul"; instructions: TypeSafeContent; criteria?: { true: TypeSafeContent; false: TypeSafeContent } | null };
export interface TypeSafeRequest {
  model: string;
  state: string | { role: string; content: string }[];
  questions: Record<string, TypeSafeQuestion>;
}
export interface TypeSafeTransport {
  systemOne(request: TypeSafeRequest, options?: TransportCallOptions): Promise<unknown>;
}

function config(reason: string): ProviderConfigError {
  return new ProviderConfigError(`TypeSafe ${reason}`, { provider: "typesafe" });
}
function record(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
function content(value: unknown): boolean {
  return typeof value === "string" || record(value) || Array.isArray(value);
}
function modelId(value: unknown): string {
  if (typeof value !== "string" || !/^[A-Za-z0-9_.:-]{1,200}$/.test(value)) {
    throw config("requires a nonempty model identifier");
  }
  return value;
}
function finiteJson(value: unknown): boolean {
  if (typeof value === "number") return Number.isFinite(value);
  if (Array.isArray(value)) return value.every(finiteJson);
  if (record(value)) return Object.values(value).every(finiteJson);
  return true;
}
function questionsFromMessages(params: StructuredCallParams): Pick<TypeSafeRequest, "state" | "questions"> {
  if (params.messages.length < 2 || params.messages[0]?.role !== "system") {
    throw config("requires one system message containing JSON questions followed by user state");
  }
  const texts = params.messages.map((message) => {
    if (!["system", "user", "assistant"].includes(message.role)) {
      throw config("supports only system/user/assistant text messages");
    }
    if (typeof message.content === "string") return message.content;
    return message.content.map((part) => {
      if (part.type !== "text") throw config("supports only text content");
      return part.text;
    }).join("");
  });
  let questions: unknown;
  try { questions = JSON.parse(texts[0]!); }
  catch { throw config("system message must be a JSON question map"); }
  if (!record(questions) || Object.keys(questions).length === 0 || !finiteJson(questions)) {
    throw config("requires a nonempty question map with finite values");
  }
  for (const [name, question] of Object.entries(questions)) {
    if (!name || !record(question) || Object.keys(question).some((key) => !["type", "instructions", "criteria"].includes(key))) {
      throw config("received an invalid question");
    }
    if (!content(question["instructions"])) throw config("question instructions must be text, an object or an array");
    const criteria = question["criteria"];
    switch (question["type"]) {
      case "choice":
        if (!record(criteria) || Object.keys(criteria).length < 1 || Object.keys(criteria).length > 255 ||
            Object.entries(criteria).some(([key, value]) => !key || (value !== null && !content(value)))) {
          throw config("choice criteria require 1 to 255 named descriptions");
        }
        break;
      case "score":
        if (!Array.isArray(criteria) || criteria.length < 2 || criteria.length > 10 || !criteria.every(content)) {
          throw config("score criteria require 2 to 10 ordered descriptions");
        }
        break;
      case "noul":
        if (criteria != null && (!record(criteria) || Object.keys(criteria).sort().join(",") !== "false,true" || !Object.values(criteria).every(content))) {
          throw config("noul criteria require true and false descriptions");
        }
        break;
      default: throw config("supports choice, score and noul questions");
    }
  }
  if (!params.messages.slice(1).some((message) => message.role === "user")) throw config("requires user state after the system question map");
  return { questions: questions as Record<string, TypeSafeQuestion>, state: texts.length === 2 ? texts[1]! : params.messages.slice(1).map((message, i) => ({ role: message.role, content: texts[i + 1]! })) };
}
function probability(value: unknown, max = 1): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= max;
}
function sameKeys(a: Record<string, unknown>, b: Record<string, unknown>): boolean {
  return Object.keys(a).length === Object.keys(b).length && Object.keys(a).every((key) => Object.hasOwn(b, key));
}
function validateResponse(response: unknown, questions: Record<string, TypeSafeQuestion>): Record<string, unknown> {
  if (!record(response) || !record(response["answers"]) || !sameKeys(response["answers"], questions)) {
    throw config("returned answers that do not match the questions");
  }
  const answers = response["answers"];
  for (const [name, question] of Object.entries(questions)) {
    const answer = answers[name];
    if (!record(answer) || answer["type"] !== question.type) throw config("returned an invalid answer type");
    if (question.type === "noul") {
      if (!probability(answer["noul"])) throw config("returned an invalid noul probability");
      continue;
    }
    const criteria = question.type === "choice" ? question.criteria : Object.fromEntries(question.criteria.map((v, i) => [String(i), v]));
    const probabilities = answer["probabilities"];
    if (!record(probabilities) || !sameKeys(probabilities, criteria) || !Object.values(probabilities).every((v) => probability(v)) || !probability(answer["confidence"])) {
      throw config("returned invalid probabilities or confidence");
    }
    if (question.type === "choice" && (typeof answer["choice"] !== "string" || !Object.hasOwn(criteria, answer["choice"]))) {
      throw config("returned an unknown choice");
    }
    if (question.type === "score" && (!probability(answer["score"], question.criteria.length - 1) ||
        !record(answer["legend"]) || !sameKeys(answer["legend"], criteria))) {
      throw config("returned an invalid score or legend");
    }
  }
  return response;
}

export class TypeSafeProvider implements ModelProvider {
  readonly providerName = "typesafe";
  readonly supportsSessionCache = false;
  readonly defaultProviderParams: Readonly<Record<string, unknown>>;
  constructor(private readonly transport: TypeSafeTransport, options: { model?: string } = {}) {
    this.defaultProviderParams = Object.freeze({ model: modelId(options.model ?? DEFAULT_TYPESAFE_MODEL) });
  }
  async structuredCall(params: StructuredCallParams): Promise<unknown> {
    const behavior = mergeProviderParams(this.defaultProviderParams, params.providerParams);
    for (const [key, value] of Object.entries(behavior)) {
      if (value == null) continue;
      if (!TYPESAFE_PARAMS.has(key)) throw config(`does not support provider param ${key}`);
      if (key === "timeout" && (typeof value !== "number" || !Number.isFinite(value) || value <= 0)) throw config("requires a positive timeout");
    }
    if ((params.artifacts?.length ?? 0) > 0 || params.cachedSession?.supported === true) {
      throw config("does not support artifacts or explicit session caches");
    }
    const model = modelId(params.model ?? behavior["model"]);
    const request = { ...questionsFromMessages(params), model };
    params.signal?.throwIfAborted();
    let raw: unknown;
    try { raw = await this.transport.systemOne(request, callOptions(params)); }
    catch (error) {
      params.signal?.throwIfAborted();
      throw transportError(error);
    }
    params.signal?.throwIfAborted();
    const response = validateResponse(raw, request.questions);
    const usage = record(response["usage"]) ? response["usage"] : {};
    const input = usage["input_tokens"], output = usage["output_tokens"], reported = response["model"];
    params.usageSink?.({
      model,
      ...(typeof input === "number" && Number.isSafeInteger(input) && input >= 0 ? { inputTokens: input } : {}),
      ...(typeof output === "number" && Number.isSafeInteger(output) && output >= 0 ? { outputTokens: output } : {}),
      ...(typeof reported === "string" && /^[A-Za-z0-9_.:-]{1,200}$/.test(reported) ? { reportedModel: reported } : {}),
    });
    return response["answers"];
  }
}
function transportError(error: unknown): Error {
  const info = error as { status?: unknown; statusCode?: unknown; name?: string; code?: string } | null;
  const status = info?.status ?? info?.statusCode;
  const hint = retryAfterSecondsFrom(error);
  const options = hint === undefined ? undefined : { retryAfterSeconds: hint };
  if (status === 401 || status === 403) return new ProviderAuthError("TypeSafe authentication failed", { provider: "typesafe" });
  if (status === 429) return new ProviderRateLimitError("TypeSafe rate limit", options);
  if (status === 408 || status === 409 || (typeof status === "number" && status >= 500) ||
      ["APIConnectionError", "APITimeoutError", "TimeoutError"].includes(info?.name ?? "") ||
      ["ECONNRESET", "ECONNREFUSED", "ETIMEDOUT", "EAI_AGAIN"].includes(info?.code ?? "")) {
    return new ProviderTransientError("TypeSafe transient transport failure", options);
  }
  return config("rejected the decision request");
}
