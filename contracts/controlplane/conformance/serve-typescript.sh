#!/usr/bin/env bash
# Launch the TypeScript control plane serving the canonical conformance fixture
# project (its TS binding) under one of the suite's auth profiles (#620 slice 2).
# The runner invokes this via `--server-cmd './serve-typescript.sh {profile}
# <port>'`, mirroring serve-python.sh's shape so both editions share the harness.
#
# BUILD PREREQUISITE: this runs the compiled server at
# packages/typescript/temporal-controlplane/dist/http/serve.js. Build first:
#   (cd packages/typescript && pnpm -r build)
#
set -euo pipefail

profile="${1:?usage: serve-typescript.sh <profile> <port>}"
port="${2:?usage: serve-typescript.sh <profile> <port>}"

here="$(cd "$(dirname "$0")" && pwd)"
package="$here/../../../packages/typescript/temporal-controlplane"
serve="$package/dist/http/serve.js"
registry="$here/project/typescript/typeflux.projects.yaml"
suite="$here/fixtures/_suite.json"

args=()
case "$profile" in
  open) ;;
  token)
    # The canonical grants live in _suite.json (single source of truth,
    # shared with serve-python.sh) — derive, don't copy.
    while IFS= read -r grant; do
      args+=(--auth-token "$grant")
    done < <(python3 -c "
import json
for grant in json.load(open('$suite'))['profiles']['token']['grants']:
    print(grant)
")
    ;;
  proxy) args+=(--trust-proxy-auth) ;;
  *)
    echo "unknown conformance profile: $profile" >&2
    exit 2
    ;;
esac

if [[ ! -f "$serve" ]]; then
  echo "serve-typescript.sh: $serve not found — build first: (cd packages/typescript && pnpm -r build)" >&2
  exit 2
fi

# --conformance-schemas injects the fixture project's activity IO schemas (the
# embedding server supplies schemas; #620 injected-schemas decision).
# ${args[@]+…}: macOS ships bash 3.2, where an empty array trips `set -u`.
# The `serve` subcommand is the bin's canonical spelling (#807: `typeflux-controlplane serve`);
# invoking the dist file via node keeps the script build-output-relative (no install step).
exec node "$serve" serve --registry "$registry" --port "$port" --conformance-schemas \
  ${args[@]+"${args[@]}"}
