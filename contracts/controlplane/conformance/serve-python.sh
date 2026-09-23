#!/usr/bin/env bash
# Launch the Python control plane serving the canonical conformance fixture
# project under one of the suite's auth profiles (#617 slice 3). The runner
# invokes this via `--server-cmd './serve-python.sh {profile} <port>'`; other
# server editions provide their own launcher mapping the same profile names
# (the canonical grants below are suite definition, mirrored in
# fixtures/_suite.json).
set -euo pipefail

profile="${1:?usage: serve-python.sh <profile> <port>}"
port="${2:?usage: serve-python.sh <profile> <port>}"

here="$(cd "$(dirname "$0")" && pwd)"
suite="$here/fixtures/_suite.json"

cd "$here/../../../packages/python"
registry=../../contracts/controlplane/conformance/project/python/typeflux.projects.yaml
export PYTHONPATH=../../contracts/controlplane/conformance/project/python
export TYPEFLUX_ENV_FILE=/nonexistent

args=()
case "$profile" in
  open) ;;
  token)
    # The canonical grants live in _suite.json (single source of truth,
    # shared with the in-process pytest gate) — derive, don't copy.
    while IFS= read -r grant; do
      args+=(--auth-token "$grant")
    done < <(python3 -c "
import json
for grant in json.load(open('$suite'))['profiles']['token']['grants']:
    print(grant)
")
    ;;
  proxy) args+=(--trust-proxy-auth) ;;
  *) echo "unknown conformance profile: $profile" >&2; exit 2 ;;
esac

# ${args[@]+…}: macOS ships bash 3.2, where an empty array trips `set -u`.
exec uv run --extra api python -m typeflux.controlplane serve \
  --registry "$registry" --port "$port" ${args[@]+"${args[@]}"}
