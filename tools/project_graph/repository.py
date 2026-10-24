"""Read revisions as data, never check out or execute code from them."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path, PurePosixPath


class GraphError(ValueError):
    pass


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def git(root: Path, *args: str) -> bytes:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=False)
    if result.returncode:
        raise GraphError(result.stderr.decode(errors="replace").strip())
    return result.stdout


def safe_path(path: str) -> bool:
    p = PurePosixPath(path)
    return (
        bool(path)
        and not p.is_absolute()
        and not any(part in {"..", ".git"} for part in p.parts)
        and "\\" not in path
    )


class Repository:
    def __init__(self, root: Path, revision: str | None = None):
        self.root = root.resolve()
        self.revision = (
            git(
                self.root,
                "rev-parse",
                "--verify",
                "--end-of-options",
                f"{revision or 'HEAD'}^{{commit}}",
            )
            .decode()
            .strip()
        )
        self.files: dict[str, bytes] = {}
        self.omitted: dict[str, str] = {}
        self.modes: dict[str, str] = {}
        self.special_inputs: dict[str, str] = {}
        if revision is not None:
            # archive honors export-ignore; ls-tree + cat-file must include those
            # tracked inputs too. Object IDs (not paths) go to the batch protocol.
            entries = []
            for entry in git(self.root, "ls-tree", "-rz", "--full-tree", self.revision).split(
                b"\0"
            ):
                if not entry:
                    continue
                meta, raw_path = entry.split(b"\t", 1)
                mode, kind, oid = meta.split()
                path = raw_path.decode()
                if not safe_path(path):
                    raise GraphError(f"unsafe repository path: {path}")
                self.modes[path] = mode.decode()
                if mode == b"120000":
                    self.omitted[path] = "symlink"
                    target = git(self.root, "cat-file", "blob", oid.decode())
                    self.special_inputs[path] = digest(
                        {"symlink": hashlib.sha256(target).hexdigest()}
                    )
                elif mode == b"160000":
                    self.omitted[path] = "gitlink"
                    self.special_inputs[path] = digest({"gitlink": oid.decode()})
                elif kind != b"blob" or mode not in {b"100644", b"100755"}:
                    self.omitted[path] = "non-regular file"
                    self.special_inputs[path] = oid.decode()
                else:
                    entries.append((path, oid))
            result = subprocess.run(
                ["git", "-C", str(self.root), "cat-file", "--batch"],
                input=b"".join(oid + b"\n" for _, oid in entries),
                capture_output=True,
                check=True,
            ).stdout
            offset = 0
            for path, oid in entries:
                end = result.index(b"\n", offset)
                actual, kind, size = result[offset:end].split()
                if actual != oid or kind != b"blob":
                    raise GraphError(f"invalid git object: {path}")
                offset = end + 1
                self.files[path] = result[offset : offset + int(size)]
                offset += int(size) + 1
            self.dirty = False
        else:
            gitlinks = {}
            for entry in git(self.root, "ls-files", "--stage", "-z").split(b"\0"):
                if not entry:
                    continue
                meta, raw_path = entry.split(b"\t", 1)
                mode, oid, stage = meta.split()
                if stage != b"0":
                    raise GraphError("resolve unmerged index entries before graph compilation")
                if mode == b"160000":
                    gitlinks[raw_path.decode()] = oid.decode()
            paths = git(
                self.root,
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            )
            for raw in paths.split(b"\0"):
                if not raw:
                    continue
                path = raw.decode()
                if not safe_path(path):
                    raise GraphError(f"unsafe repository path: {path}")
                absolute = self.root / path
                # Do not follow file or directory symlinks outside the checkout.
                if absolute.is_symlink() or not absolute.resolve().is_relative_to(self.root):
                    self.omitted[path] = "symlink"
                    target = (
                        os.fsencode(os.readlink(absolute))
                        if absolute.is_symlink()
                        else b"symlink ancestor"
                    )
                    self.special_inputs[path] = digest(
                        {"symlink": hashlib.sha256(target).hexdigest()}
                    )
                elif path in gitlinks:
                    oid = gitlinks[path]
                    # An uninitialized submodule is still an indexed gitlink.
                    # An initialized one may have moved to a different commit.
                    if (absolute / ".git").exists():
                        oid = git(absolute, "rev-parse", "--verify", "HEAD").decode().strip()
                    self.omitted[path] = "gitlink"
                    self.special_inputs[path] = digest({"gitlink": oid})
                elif absolute.is_file():
                    self.files[path] = absolute.read_bytes()
                    self.modes[path] = "100755" if absolute.stat().st_mode & 0o111 else "100644"
                elif absolute.exists():
                    self.omitted[path] = "non-regular file"
            self.dirty = bool(git(self.root, "status", "--porcelain", "--untracked-files=all"))
        self.inputs = {
            p: digest({"content": hashlib.sha256(b).hexdigest(), "mode": self.modes[p]})
            for p, b in sorted(self.files.items())
        }
        self.inputs.update(
            {p: digest({"omitted": value}) for p, value in self.special_inputs.items()}
        )
        self.tree_digest = digest({"files": self.inputs, "omitted": self.omitted})

    def text(self, path: str) -> str:
        try:
            return self.files[path].decode("utf-8")
        except (KeyError, UnicodeDecodeError) as error:
            raise GraphError(f"missing or non-UTF8 source: {path}") from error
