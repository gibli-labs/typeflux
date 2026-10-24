# Vercel AI Gateway

Select `VercelAIGatewayProvider` in Python or TypeScript, or use
`runtime.provider.type: vercel-ai-gateway` in YAML. An explicit `author/model`
ID is required; Typeflux never chooses a gateway model for you.

This adapter uses Vercel's [Chat Completions API](https://vercel.com/docs/ai-gateway/sdks-and-apis/openai-chat-completions)
and [JSON Schema output](https://vercel.com/docs/ai-gateway/sdks-and-apis/openai-chat-completions/structured-outputs),
verified against the official documentation on 2026-10-24. Choose a model that
supports this API and structured output. A gateway model prefix identifies the
model's author, not necessarily the infrastructure provider serving it.

## Authentication and configuration

Install `typeflux[openai]` for Python. The OpenAI SDK is optional and imported
only when a gateway client is needed. In TypeScript, install `openai` in the
application when using the YAML environment adapter; the core provider requires
only an injected `OpenAIChatTransport` and has no vendor SDK dependency.

Supply `AI_GATEWAY_API_KEY` through your secret mechanism. An explicit `api_key`
or YAML `value_from` reference takes precedence. The adapter does not fall back
to `OPENAI_API_KEY`, `OPENAI_BASE_URL`, or automatic OIDC discovery. An OIDC bearer
token can be supplied explicitly through the same secret slot, with refresh
managed by the caller. See [Vercel authentication](https://vercel.com/docs/ai-gateway/authentication-and-byok).
The built-in client always targets `https://ai-gateway.vercel.sh/v1`; Python YAML
rejects `base_url`, and TypeScript retains its injected-transport endpoint seam.

```yaml
runtime:
  provider:
    type: vercel-ai-gateway
    model: openai/gpt-4o-mini
    api_key: {value_from: {env: AI_GATEWAY_API_KEY}}
    params: {max_tokens: 128, timeout: 30}
```

Complete runnable YAML examples:
[Python](../packages/python/examples/vercel_ai_gateway/typeflux.yaml) and
[TypeScript](../packages/typescript/temporal-yaml/examples/vercel-ai-gateway/main.ts).
The Python YAML runtime uses the normal `typeflux worker` / `typeflux run` commands
and needs Temporal for workflow execution. The TypeScript example directly invokes
its assembled activity without a Temporal server.

## Programmatic calls

```python
from pydantic import BaseModel
from typeflux import ChatMessage, ProviderParams, VercelAIGatewayProvider

class Classification(BaseModel):
    label: str

provider = VercelAIGatewayProvider(default_model="openai/gpt-4o-mini")
usage = []
result = provider.structured_call(
    messages=[ChatMessage(role="user", content="Classify: I need an invoice.")],
    output_schema=Classification,
    provider_params=ProviderParams(max_tokens=128, timeout=30),
    usage_sink=usage.append,
)
# result is validated. The same provider works with execute_ai_activity and YAML.
```

```typescript
import OpenAI from "openai";
import { z } from "zod";
import {
  defineActivity, executeActivity, VercelAIGatewayProvider,
  VERCEL_AI_GATEWAY_BASE_URL,
} from "@typeflux/temporal";

const client = new OpenAI({
  apiKey: process.env.AI_GATEWAY_API_KEY,
  baseURL: VERCEL_AI_GATEWAY_BASE_URL,
  maxRetries: 0,
});
const provider = new VercelAIGatewayProvider({ chat: { completions: {
  create: (request, opts) => client.chat.completions.create(request as never, {
    signal: opts?.signal, timeout: opts?.timeoutMs,
  }),
} } }, { model: "openai/gpt-4o-mini" });
const activity = defineActivity({
  name: "classify", prompt: { name: "classify" },
  input: z.object({ text: z.string() }), output: z.object({ label: z.string() }),
});
const result = await executeActivity(activity, { text: "I need an invoice." }, {
  provider, messages: [{ role: "user", content: "Classify: I need an invoice." }],
  providerParams: { max_tokens: 128, timeout: 30 },
});
```

Python returns Pydantic-validated output; TypeScript's `structuredCall` returns
parsed JSON and `executeActivity` validates it with the activity's Zod schema.
Existing activity validation-repair and provider retry policies still apply.
SDK retries are disabled in built-in clients; injected clients should also disable
them so attempts are controlled by Typeflux. Async Python cancellation propagates;
TypeScript forwards the abort signal and timeout to its transport. Timeouts in
provider params are seconds, converted to milliseconds for TypeScript transports.

## Supported scope and inspection

The adapter accepts text `system`, `user`, and `assistant` messages, JSON Schema
output, and `model`, `temperature`, `max_tokens`, `top_p`, `stop`, and `timeout`.
These are mapped API parameters, not a guarantee that every model accepts every
parameter. Model-specific rejections surface as provider errors. Other options,
artifacts, content extensions, tools, streaming, routing/fallback configuration,
reasoning controls, and explicit provider-side session caches are unsupported.
The existing session-cache preparation path falls back to full-context calls.
Gateway-side automatic caching may still report cached input tokens.

Provider identity remains `vercel-ai-gateway` in policy, cache partitioning,
rate limits, and metadata. The requested model remains the effective policy/cache
model, including when selected by a programmatic constructor. Provider policy
applies to the gateway, not to the upstream host; this adapter does not enforce
an upstream-host allowlist. Configure that separately in Vercel if required.

Usage reports input, output, total, and cached-input token counts when supplied.
`model` is the requested ID. Optional `reported_model` / `reportedModel` and
`upstream_provider` / `upstreamProvider` describe the response without overriding
that identity. The upstream field is read only from
`provider_metadata.gateway.routing.finalProvider` when present; absent fields
stay unknown. No provider is inferred from the model prefix, affinity pin, or
routing attempts. Raw gateway metadata and SDK error bodies are not retained.
Python observers expose the optional response identity as redacted
`provider_response` metadata; TypeScript observers include it in generation usage.
No new redaction exclusions or high-cardinality tags are added.

Existing typed activity/YAML metadata contributors carry provider/model identity.
The execution-manifest schema is unchanged: its existing provider/model fields
already represent the planned execution contract. Response identity and token
usage describe an observed call and belong on the generation observation.

Authentication failures are terminal (`ProviderAuthError`); rate limits preserve
`Retry-After`; timeouts, connection errors, 408/409, and 5xx use transient retry
semantics. Unsupported requests and truncated/refused output fail clearly.
No partial or invalid output is accepted into the activity cache.

## Opt-in live smoke

Offline tests use mocked transports and scrub gateway credentials. Live checks
are separate and require the key in the test process environment. Each command
makes at most one request, with 64 output tokens, a 30-second timeout, disabled
SDK/client retries, and no activity repair retries. Run only the selected file:

```sh
TYPEFLUX_RUN_LIVE=1 uv run --project packages/python --no-sync pytest -m live packages/python/tests/test_live_vercel_ai_gateway.py -q -s
TYPEFLUX_RUN_LIVE=1 pnpm --filter @typeflux/temporal-yaml exec vitest run test/live-vercel-ai-gateway.test.ts
```

The Python smoke selects `openai/gpt-4o-mini`; the TypeScript smoke selects
`anthropic/claude-haiku-4.5` to exercise a second model family. These client bounds
do not disable Vercel's internal routing or retry behavior. A missing key skips
the smoke and is not live verification. Never place a key in YAML, a checked-in
file, a fixture, command arguments, or a task journal.
