#!/usr/bin/env python3
"""Is a checkout's vendored copy of orbit-core byte for byte this core's?

    core-same.py <core dir> <checkout>

Exit 0 when every path core-manifest.txt lists is the same in both; exit 1 with ONE line
naming the first path that differs, is missing, is extra inside a vendored directory, or
is a symlink. The ship path asks this before it runs anything out of a fresh clone of the
instance repository: orbitd holds the GitHub App key, the registry token and the token
signing key, so a script it runs must be this build's own, and the cheapest proof is that
the clone's copy IS this build's copy. (orbitd's commit gate asks the same of every commit
in Python, `orbitd/checks/core.py`; this is that question for a shell script, with no
import of orbitd.)
"""
import pathlib
import sys

SKIP = {"__pycache__"}
# What Python would import from a directory in place of the module a script asked for.
IMPORTABLE = (".py", ".pyc", ".pyo", ".so", ".pyd", ".pth")


def entries(root: pathlib.Path) -> dict:
    """{relative posix path: bytes} for a file or a directory tree; a symlink is an entry
    whose value is None (never followed)."""
    if root.is_symlink():
        return {"": None}
    if root.is_file():
        return {"": root.read_bytes()}
    out = {}
    for path in sorted(root.rglob("*")):
        if SKIP & set(path.relative_to(root).parts):
            continue
        if path.is_symlink():
            out[path.relative_to(root).as_posix()] = None
        elif path.is_file():
            out[path.relative_to(root).as_posix()] = path.read_bytes()
    return out


def shadow(checkout: pathlib.Path, vendored: set) -> str | None:
    """A file that sits beside core's Python scripts and that Python would import in their
    place. A script run as `python3 scripts/x.py` looks in scripts/ first: a `yaml.py`
    there is the `yaml` it gets. Such a directory holds core's files and nothing else
    that can be imported."""
    beside = {pathlib.PurePosixPath(p).parent.as_posix() for p in vendored if p.endswith(".py")}
    for rel_dir in sorted(beside):
        directory = checkout / rel_dir
        if not directory.is_dir():
            continue
        for entry in sorted(directory.iterdir()):
            rel = f"{rel_dir}/{entry.name}" if rel_dir != "." else entry.name
            if entry.name in SKIP or rel in vendored or any(v.startswith(rel + "/") for v in vendored):
                continue
            if entry.is_dir() and not entry.is_symlink():
                if (entry / "__init__.py").exists():
                    return f"{rel} is a Python package beside core's scripts and not a file of this core"
            elif entry.name.endswith(IMPORTABLE):
                return f"{rel} is not a file of this core"
    return None


def first_difference(core: pathlib.Path, checkout: pathlib.Path):
    vendored = set()
    for line in (core / "core-manifest.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        src, _, dst = line.partition(" -> ")
        src, dst = src.strip(), (dst or src).strip()
        source, target = core / src, checkout / dst
        if not source.exists():
            continue
        vendored.add(dst)
        if not target.exists() and not target.is_symlink():
            return f"{dst} is missing"
        want, have = entries(source), entries(target)
        for rel in sorted(set(want) | set(have)):
            name = f"{dst}/{rel}" if rel else dst
            if rel not in have:
                return f"{name} is missing"
            if have[rel] is None:
                return f"{name} is a symlink"
            if rel not in want:
                return f"{name} is not a file of this core"
            if want[rel] != have[rel]:
                return f"{name} differs"
    return shadow(checkout, vendored)


def main(argv: list) -> int:
    if len(argv) != 3:
        print("usage: core-same.py <core dir> <checkout>", file=sys.stderr)
        return 2
    try:
        said = first_difference(pathlib.Path(argv[1]), pathlib.Path(argv[2]))
    except OSError as e:
        print(f"the two copies could not be compared ({e.__class__.__name__})")
        return 1
    if said:
        print(said)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
