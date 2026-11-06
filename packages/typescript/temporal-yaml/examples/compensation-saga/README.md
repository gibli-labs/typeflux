# Temporal YAML compensation adoption

This is the YAML counterpart of the existing
[Python booking/charge saga](../../../../python/examples/compensation_saga/README.md).
`typeflux.yaml` declares book → charge → fulfill, with `cancel_room` and
`refund_card` compensations. `bindings.mjs` supplies Zod schemas and fixed fake
responses/counters. No external booking, payment or refund is made.

From a checkout after [development setup](../../../../../docs/development.md),
start a separate development Temporal server, then run from the repository root:

```sh
node packages/typescript/temporal-yaml/examples/compensation-saga/adoption-demo.mjs
```

The script injects intentional finalizer and compensation faults using the existing
SDK retry policies. It observes complete and partial compensation, then a pending
refund retry completed by a replacement worker on the same run. It asserts actual
action/attempt order, original failure, lifecycle compensation events, native
terminal status and fake forward-write counts before printing three JSON records.
It queries lifecycle before shutting down the serving worker; service history and
describe reads remain separate observations.

Queues/IDs are unique and waits, attempts and owned cleanup are bounded. Use a
non-production namespace. The required isolated Temporal lane executes this exact
script; no model credentials or external service are needed besides that server.
The same command requires the matching built candidate SDKs and explicit Temporal
client/worker/protobuf dependencies; these are optional adoption dependencies,
not dependencies added to the Local quickstart.

See the [compensation adoption guide](../../../../../docs/temporal-compensation-adoption.md)
for expected complete/partial/replacement outcomes, idempotency and partial-effect
limits. Fake counters do not establish transactional undo or persistent deduplication.
Local rejects configured compensation, retains completed effects and restarts a
new invocation from step one. Actual deployed recovery remains #87.
