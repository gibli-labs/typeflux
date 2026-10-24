"""Local append-only task/evidence journal. CI authority comes from CI, not this file."""

from __future__ import annotations

import fcntl
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from .repository import GraphError, Repository, canonical, digest

TRANSITIONS = {
    "planned": {"implementing", "blocked"},
    "implementing": {"validating", "blocked", "failed"},
    "validating": {"implementing", "review-ready", "blocked", "failed"},
    "review-ready": {"implementing", "accepted", "blocked"},
    "blocked": {"implementing", "validating", "failed"},
    "failed": {"implementing"},
    "accepted": set(),
}


class Ledger:
    def __init__(self, root: Path, task: str):
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,99}", task):
            raise GraphError("invalid task ID")
        self.root = root.resolve()
        self.path = self.root / ".project-graph/tasks" / f"{task}.jsonl"
        if not self.path.resolve().is_relative_to(self.root) or self.path.is_symlink():
            raise GraphError("task journal cannot escape the repository")

    @staticmethod
    def decode(content: str) -> list[dict]:
        records = []
        previous = None
        for line in content.splitlines():
            record = json.loads(line)
            checksum = record.pop("digest")
            if (
                record.get("previous") != previous
                or record.get("sequence") != len(records)
                or digest(record) != checksum
            ):
                raise GraphError("task journal integrity failure")
            record["digest"] = checksum
            records.append(record)
            previous = checksum
        return records

    def read(self) -> list[dict]:
        if not self.path.exists():
            return []
        return self.decode(self.path.read_text())

    def append(self, event: str, payload: dict, repo: Repository) -> dict:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Serialize state validation and append; a partial write is detected on resume.
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "r+", encoding="utf-8") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            records = self.decode(stream.read())
            self.validate_event(records, event, payload)
            record = {
                "version": 1,
                "sequence": len(records),
                "previous": records[-1]["digest"] if records else None,
                "event": event,
                "payload": payload,
                "revision": repo.revision,
                "treeDigest": repo.tree_digest,
                "dirty": repo.dirty,
                "recordedAt": datetime.now(UTC).isoformat(),
            }
            record["digest"] = digest(record)
            stream.write(canonical(record).decode() + "\n")
            stream.flush()
            os.fsync(stream.fileno())
            return record

    @staticmethod
    def validate_event(records, event, payload):
        if not records:
            if event != "create" or not payload.get("intent") or not payload.get("acceptance"):
                raise GraphError("create a task with intent and acceptance criteria first")
            return
        if event == "create":
            raise GraphError("task already exists; resume it")
        if event == "transition":
            state = next(
                (r["payload"]["state"] for r in reversed(records) if r["event"] == "transition"),
                "planned",
            )
            if payload.get("state") not in TRANSITIONS[state]:
                raise GraphError(f"invalid task transition from {state}")
        elif event == "effect":
            if payload.get("state") not in {
                "pending",
                "confirmed",
                "not-applied",
            } or not payload.get("key"):
                raise GraphError("effect needs a key and pending/confirmed/not-applied state")
            previous = next(
                (
                    r
                    for r in reversed(records)
                    if r["event"] == "effect" and r["payload"]["key"] == payload["key"]
                ),
                None,
            )
            if (
                payload["state"] == "pending"
                and previous
                and previous["payload"]["state"] != "not-applied"
            ):
                raise GraphError("effect already pending or confirmed; reconcile before retry")
            if payload["state"] != "pending" and (
                not previous
                or previous["payload"]["state"] != "pending"
                or not payload.get("receipt")
            ):
                raise GraphError("effect reconciliation needs a pending attempt and a receipt")
        elif event == "verification":
            if payload.get("authority") != "local-execution" or payload.get("result") not in {
                "passed",
                "failed",
                "timeout",
            }:
                raise GraphError("verification requires an explicit execution result")
        else:
            raise GraphError(f"unknown task event: {event}")

    def resume(self, repo: Repository) -> dict:
        records = self.read()
        if not records:
            raise GraphError("task does not exist")
        effects = {}
        verification = []
        state = "planned"
        for record in records:
            if record["event"] == "effect":
                effects[record["payload"]["key"]] = record["payload"]
            if record["event"] == "verification":
                verification.append(
                    {
                        **record["payload"],
                        "applicable": record["treeDigest"] == repo.tree_digest,
                        "testedRevision": record["revision"],
                    }
                )
            if record["event"] == "transition":
                state = record["payload"]["state"]
        pending = [effect for effect in effects.values() if effect["state"] == "pending"]
        return {
            "task": records[0]["payload"],
            "state": state,
            "revision": repo.revision,
            "inputsChanged": records[-1]["treeDigest"] != repo.tree_digest,
            "verification": verification,
            "pendingEffects": pending,
            "canRetryExternalEffects": not pending,
            "authority": "local journal; not a CI attestation or permission to publish",
        }
