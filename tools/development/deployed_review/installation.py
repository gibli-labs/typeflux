"""Inspect the installed consumer image without a network or Temporal connection."""

import hashlib
import importlib.util
import json
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import typeflux


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


versions = {}
for package in ["typeflux", "pydantic", "temporalio", "fastapi", "uvicorn"]:
    try:
        versions[package] = version(package)
    except PackageNotFoundError:
        versions[package] = None
location = Path(typeflux.__file__)
if "/site-packages/typeflux/" not in str(location):
    raise RuntimeError("repository/editable SDK fallback")
print(
    json.dumps(
        {
            "versions": versions,
            "temporalImportable": importlib.util.find_spec("temporalio") is not None,
            "sdkLocation": str(location),
            "sdkEntrySha256": sha(location),
            "candidateFiles": {
                p.name: sha(p) for p in sorted(Path("/candidate").iterdir()) if p.is_file()
            },
            "dependencyLocks": {"uv.lock": sha(Path("/app/uv.lock"))},
        }
    )
)
