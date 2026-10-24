"""Collection-only pytest reporter; executed by the explicit collect command."""

import json
import os
from pathlib import Path


def pytest_collection_finish(session):
    root = Path(os.environ["TYPEFLUX_GRAPH_ROOT"]).resolve()
    records = [
        {
            "path": str(item.path.resolve().relative_to(root)),
            "name": item.originalname or item.name,
            "id": item.nodeid,
        }
        for item in session.items
    ]
    Path(os.environ["TYPEFLUX_GRAPH_INVENTORY"]).write_text(json.dumps(records))
