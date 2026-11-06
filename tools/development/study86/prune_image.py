"""Leave only frozen dependencies in the verifier; remove the setup checkout's SDK solutions."""

from pathlib import Path

root = Path("/environment/repository")
site = next((root / "packages/python/.venv/lib").glob("python*/site-packages"))
rewritten = 0
for file in site.glob("*.pth"):
    text = file.read_text()
    if "/environment/repository/packages/python/src" in text:
        file.write_text(
            text.replace("/environment/repository/packages/python/src", "/work/packages/python/src")
        )
        rewritten += 1
assert rewritten, "expected editable SDK path must be rebound to each historical candidate"


def prune(directory):
    for child in directory.iterdir():
        if child.name in {"node_modules", ".venv"}:
            continue
        if child.is_symlink() or child.is_file():
            child.unlink()
        elif child.is_dir():
            prune(child)
            if not any(child.iterdir()):
                child.rmdir()


prune(root)
assert not (root / "packages/typescript/temporal/dist").exists()
assert not (root / "packages/python/src").exists()
