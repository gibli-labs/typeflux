# TypeSafe / Jev decisions

Use `TypeSafeProvider` in Python or TypeScript, or select
`runtime.provider.type: typesafe` in YAML. This integration calls TypeSafe's native
System One API. It supports **Choice**, **Score**, and **Noul** questions and returns
the complete answer map, including probabilities and confidence where provided.
It does not generate arbitrary JSON or prose.

The default model is pinned to `jev-1.14.0`. You can explicitly choose another
version or an alias such as `jev-latest`; aliases can change behavior without a
configuration change. The requested model controls policy and cache identity. The
response's resolved model is recorded separately in existing usage observations.

## Prompts and outputs

The **first system message** must contain a JSON object mapping question names to
native Jev questions. At least one following message must be a user message.
With one following message, its text becomes `state`. With multiple following
messages, `state` is an array of `{role, content}` objects preserving their order,
including any validation-repair instructions. Text content parts are joined.
Questions are evaluated independently against this shared state.

```json
{
  "route": {
    "type": "choice",
    "instructions": "Which team should handle this ticket?",
    "criteria": {
      "billing": "Invoices, refunds and payments",
      "technical": "Bugs and integration problems",
      "human_review": "Ambiguous requests or cases needing human judgment"
    }
  },
  "urgency": {
    "type": "score",
    "instructions": "How urgent is this ticket?",
    "criteria": ["Routine", "Urgent", "Critical"]
  },
  "refund": {"type": "noul", "instructions": "Does the customer request a refund?"}
}
```

Your Pydantic or Zod output schema describes **the answer map**, without an
`answers` wrapper. A Choice field has `type`, `choice`, `probabilities`, and
`confidence`; a Score field has `type`, `score`, `probabilities`, `legend`, and
`confidence`; a Noul field has `type` and `noul`. A score is a fractional expected
level, not an integer label. Score probability and legend keys are strings (`"0"`,
`"1"`, …) in both editions. Noul is a probability, not an automatic Boolean decision.

Declare explicit probability and legend fields in activity schemas, matching the
known criteria. Typeflux's portable schema checks reject open maps such as
`z.record(...)`. Provider responses are checked for matching question names,
answer types and valid ranges, then validated against the activity output schema
before they can be accepted or cached. No confidence threshold is applied inside
the provider. Prefer `validation_retries: 0` for a fixed native answer contract;
repair prompts cannot change the shape Jev's API returns.

## Python

Install `typeflux[typesafe]` and set `TYPESAFE_API_KEY` through your secret manager.
The constructor also accepts an explicit `api_key`. No other vendor key is used.

```python
import json
from pydantic import BaseModel, Field
from typing import Literal
from typeflux import ChatMessage, TypeSafeProvider

class RefundAnswer(BaseModel):
    type: Literal["noul"]
    noul: float = Field(ge=0, le=1)

class Decision(BaseModel):
    refund: RefundAnswer

provider = TypeSafeProvider()
result = provider.structured_call(
    messages=[
        ChatMessage(role="system", content=json.dumps({
            "refund": {"type": "noul", "instructions": "Requests a refund"}
        })),
        ChatMessage(role="user", content="Please refund my duplicate charge."),
    ],
    output_schema=Decision,
)
```

`async_structured_call` uses the official asynchronous client and propagates
cancellation. For controlled transports, inject `typesafe_client` and/or
`async_typesafe_client`. Typeflux closes clients it constructs; injected clients
remain application-owned. SDK retries must be disabled on injected clients too.
The [Python example](../packages/python/examples/typesafe_decisions/main.py)
shows a Choice result with deterministic routing and a review fallback.

## TypeScript

The core package takes an injected transport. Install `@typesafe-ai/sdk` in your
application to use the official client. The YAML runtime constructs this adapter
automatically when the optional SDK is installed.

```ts
import { TypeSafeClient } from "@typesafe-ai/sdk";
import { TypeSafeProvider } from "@typeflux/temporal";

const client = new TypeSafeClient({
  apiKey: process.env.TYPESAFE_API_KEY,
  retry: { maxRetries: 0 },
  logLevel: "off",
});
const provider = new TypeSafeProvider({
  systemOne: (request, options) => client.systemOne(request, {
    ...(options?.signal ? { signal: options.signal } : {}),
    ...(options?.timeoutMs ? { timeout: options.timeoutMs } : {}),
  }),
});
```

Use this provider with `executeActivity` and an explicit Zod answer schema.
See the [complete YAML activity example](../packages/typescript/temporal-yaml/examples/typesafe-decisions/main.ts).

## YAML

```yaml
runtime:
  provider:
    type: typesafe
    model: jev-1.14.0
    api_key: {value_from: {env: TYPESAFE_API_KEY}}
    params: {timeout: 30}
  registry:
    type: inline
    prompts:
      decide:
        messages:
          - role: system
            content: '{"refund":{"type":"noul","instructions":"Requests a refund"}}'
          - role: user
            content: '{{ text }}'
```

The `api_key` field is optional when `TYPESAFE_API_KEY` is present. Explicit secret
references take precedence. Only `model` and `timeout` provider parameters are
supported. Sampling parameters, artifacts, image/Box questions, and explicit
provider-side session caching are unsupported. The built-in client uses
`https://api.typesafe.ai`; alternate endpoints require an injected client or
transport. Ordinary Typeflux result caching remains available.

## Routing, parallel paths, and review

Execute the Jev request **inside an activity**. Workflow code consumes its recorded
result and applies deterministic rules, so Temporal replay does not call the model.
For example, route to the selected team when `route.confidence >= 0.8`, and otherwise
to `human_review`. Tune thresholds against representative examples for your use case.
A confidence score does not replace human approval.

Choice answers select one route. Independent Noul/Score questions can enable
multiple branches. Existing YAML `when` predicates can inspect paths such as
`decide.route.choice`, `decide.route.confidence`, or `decide.refund.noul`; use an
existing `parallel` block when multiple conditional paths should run. A false
**top-level** `when` ends its sequence, so mutually exclusive paths should not be
written as consecutive gated top-level steps. See [conditional steps](yaml.md#conditional-steps-when).

Send uncertain cases to a workflow with an explicit lifecycle review gate, and
keep human approval before side effects where that is your workflow policy. Jev's
assessment is decision evidence; it does not submit a human review decision.
No workflow manifest fields or review control-plane operations are added by this
provider. Existing metadata contributors handle provider identity and usage.

## Failures and verification

Authentication failures and invalid requests are terminal provider errors. HTTP
429 is rate limited; overload (529), other server failures, timeouts, and connection
errors use Typeflux's existing transient retry policy. Built-in SDK retries are
disabled. Error messages do not copy SDK request bodies, credentials, or response
text. Model usage is reported through the existing usage sink.

Offline tests cover both editions, YAML activity execution, the official Python
SDK wire format, request/answer validation, policy/cache identity, retry behavior,
and cancellation. Live checks are opt-in and send synthetic ticket text only:

```sh
TYPEFLUX_RUN_LIVE=1 uv run --project packages/python --no-sync pytest -m live packages/python/tests/test_live_typesafe.py -q
TYPEFLUX_RUN_LIVE=1 pnpm --filter @typeflux/temporal-yaml exec vitest run test/live-typesafe.test.ts
```

The key must be supplied separately as `TYPESAFE_API_KEY`. Live availability and
model behavior are separate from offline transport checks.

API references: [System One](https://docs.typesafe.ai/api/system-one),
[models](https://docs.typesafe.ai/models), and
[errors](https://docs.typesafe.ai/api/errors).
