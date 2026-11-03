# Offline Python quickstart

This example describes current source/candidate packages. Copy this directory into
an application directory. Python 3.11+ is required. Install the candidate base wheel
built from this checkout into a fresh environment:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install /absolute/path/to/typeflux-VERSION-py3-none-any.whl
typeflux-local workflow.yaml --input input.json
typeflux-local workflow.yaml --input - < input.json
typeflux-local --project typeflux.project.yaml --workflow echo --environment offline --input input.json
python local_sdk.py
```

Each command prints `{"text":"local result"}`. Replace the wheel path with the
actual candidate artifact; after a version containing this feature is published,
`python -m pip install typeflux` installs the base package instead. No provider
credentials, vendor SDK, observability backend, network service or Temporal package
is needed. `local_sdk.py` uses `build_local_runtime`, awaits typed execution and
shuts observations down after the invocation drains. The CLI imports `local_example`
from the YAML/project directory; the SDK script uses its own application directory.

From a repository checkout, first follow [development setup](../../../../docs/development.md),
then run the same example through the candidate editable package:

```sh
uv run --project packages/python typeflux-local packages/python/examples/local_runtime/workflow.yaml --input packages/python/examples/local_runtime/input.json
uv run --project packages/python python packages/python/examples/local_runtime/local_sdk.py
```

## Adopt Temporal

Keep `workflow.yaml`, the models and `OfflineProvider`. The included `temporal.yaml`
environment adds engine/connection/queue placement; the manifest already registers
it. Install the matching candidate wheel with its extra, start a Temporal dev server
in a separate terminal, then run the starter/worker script:

```sh
python -m pip install '/absolute/path/to/typeflux-VERSION-py3-none-any.whl[temporal]'
temporal server start-dev
# In another terminal, with the application environment activated:
python temporal_sdk.py
```

The Temporal CLI is a separate prerequisite only for this optional adoption example;
an existing reachable service can be configured instead. The script resolves the
`temporal` environment, runs a worker while starting a fresh workflow, prints the
same JSON and drains the worker. It does not resume or migrate a local execution.
Repository verification exercises this script on its existing isolated Temporal
server. For deployment, use the existing worker/starter patterns and project policy
admission. See the [engine comparison and migration guide](../../../../docs/local-runtime.md#switch-an-application-to-temporal).

Every local invocation starts at step one. Process loss discards progress and can
prevent cleanup; rerunning may repeat completed side effects. The base-wheel probe
runs the CLI and SDK example with Temporal absent and network connections blocked.
