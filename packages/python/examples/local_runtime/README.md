# Offline foreground execution

From the repository root, after the maintained development setup:

```sh
uv run --project packages/python typeflux-local packages/python/examples/local_runtime/workflow.yaml --input packages/python/examples/local_runtime/input.json
```

The command returns `{"text":"local result"}`. It uses the existing `FakeProvider`
with a canned response and inline prompts. It requires no credentials, network
service or Temporal installation. The CLI imports `local_example` from this
directory, the application root selected by the YAML path.

The same workflow through the project/environment resolver:

```sh
uv run --project packages/python typeflux-local --project packages/python/examples/local_runtime/typeflux.project.yaml --workflow echo --environment offline --input packages/python/examples/local_runtime/input.json
```

For a base `pip install typeflux` installation, copy this directory to your machine
and run `typeflux-local workflow.yaml --input input.json` from it. The installed
command and `python -m typeflux.yaml.local_cli` use the same implementation.

Each invocation starts at the beginning. Process termination loses progress and
rerunning can repeat side effects. See [the runtime guide](../../../../docs/local-runtime.md)
for exit statuses, policies and cooperative interruption. TypeScript foreground
CLI support and the complete cross-engine/process-loss matrix remain in #89.
