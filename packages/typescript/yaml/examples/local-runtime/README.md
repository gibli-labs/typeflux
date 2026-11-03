# Offline foreground execution

From this directory, with `@typeflux/yaml` installed in your application:

```sh
typeflux-local workflow.yaml --bindings bindings.mjs --input input.json
typeflux-local --project typeflux.project.yaml --workflow echo --environment offline \
  --bindings bindings.mjs --input input.json
cat input.json | typeflux-local workflow.yaml --bindings bindings.mjs --input -
```

Each prints `{"text":"local result"}`. The fake provider needs no credentials,
network, Temporal SDK, worker or service. Bindings paths resolve from the working
directory; `TYPEFLUX_WORKER_BINDINGS` is the fallback for `--bindings`. Named exports
and a default object both work. Compile TypeScript bindings to JavaScript first.

Use a new command for a fresh execution. Interruptions discard process state;
durability, schedules and review require Temporal. See [the runtime guide](../../../../../docs/local-runtime.md).
