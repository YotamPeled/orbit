"""The vendored core is this build's core.

An instance repository carries a copy of orbit-core at one pinned ref (orbit-core.lock):
the scaffolder, the generator, the service chart, the staged platform tree, the verify
stages, the skills. Everything the platform runs is rendered by those files, so a commit
that changes one of them by hand changes what every later check, and the cluster, would
trust. Each vendored path must be byte-for-byte what this build carries.

A commit that pins another core ref than this build's cannot be checked by this build at
all (its files are another version's): it is refused with the way forward, which during
an upgrade is simply to apply the platform so the matching orbitd takes over.
"""

from __future__ import annotations

from . import AnotherCore, Context, files_under, lock_ref, vendored_paths

NAME = "core"
WHAT = "the vendored orbit-core files are exactly the pinned core's"
# What Python would import from a directory in place of the module a script asked for.
IMPORTABLE = (".py", ".pyc", ".pyo", ".so", ".pyd", ".pth")


def _shadow(ctx: Context, vendored: set[str]) -> str | None:
    """A file beside core's Python scripts that Python would import in their place. A
    script run as `python3 scripts/x.py` looks in scripts/ first, so a `yaml.py` there is
    the `yaml` it gets: such a directory holds core's files and nothing else importable."""
    beside = {dst.rsplit("/", 1)[0] for dst in vendored if dst.endswith(".py") and "/" in dst}
    for rel_dir in sorted(beside):
        directory = ctx.root / rel_dir
        if not directory.is_dir():
            continue
        for entry in sorted(directory.iterdir()):
            rel = f"{rel_dir}/{entry.name}"
            if entry.name == "__pycache__" or rel in vendored or any(v.startswith(rel + "/") for v in vendored):
                continue
            package = entry.is_dir() and not entry.is_symlink() and (entry / "__init__.py").exists()
            if package or (not entry.is_dir() and entry.name.endswith(IMPORTABLE)):
                return (f"{rel} is not part of orbit-core {ctx.version} and sits beside its scripts, where Python "
                        f"would import it in place of the module a core script asks for: remove it")
    return None


def check(ctx: Context) -> str | None:
    ref = lock_ref(ctx.root)
    if ref is None:
        return "orbit-core.lock is missing or names no ref: the instance does not say which core it is built from"
    if ref != ctx.version:
        raise AnotherCore(
            f"this commit pins orbit-core {ref} and it is being checked by {ctx.version}: apply the "
            f"platform so the matching orbitd takes over (`orbit platform apply --app platform-config`), "
            f"or check it from a desk at {ref}")
    vendored: set[str] = set()
    for src, dst in vendored_paths(ctx.core):
        vendored.add(dst)
        if (ctx.root / dst).is_symlink():
            return f"{dst} is a symlink; a vendored core path is a real file or directory"
        want, have = files_under(ctx.core / src), files_under(ctx.root / dst)
        for rel in sorted(set(want) | set(have)):
            path = f"{dst}/{rel}" if rel else dst
            if rel not in have:
                return f"{path} is missing: it is part of orbit-core {ref} (run `make core-sync`)"
            if rel not in want:
                return f"{path} is not part of orbit-core {ref}: a vendored directory holds only the core's files"
            if have[rel].is_symlink() or want[rel].read_bytes() != have[rel].read_bytes():
                return (f"{path} differs from orbit-core {ref}: vendored files are never edited in an "
                        f"instance (run `make core-sync`; change core itself to change them)")
    return _shadow(ctx, vendored)
