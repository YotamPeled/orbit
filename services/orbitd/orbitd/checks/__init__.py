"""The commit gate's checks: a chain of small, independent checks over one checkout of the
instance repository (docs/V1.md M2c).

orbitd runs the chain on every new commit before it applies anything from it; a commit
that fails is refused and the last good commit stays applied. The desk runs the SAME
chain (`orbit check all`, or one check by name) so an agent can know before merging, and
the box and the laptop cannot disagree: one list, here.

A check is one module with one shape:

    NAME = "sealed"                       # how `orbit check <name>` and a refusal name it
    WHAT = "every sealed secret holds ciphertext"      # one line for `orbit check --list`
    def check(ctx: Context) -> str | None  # None: passes. A str: fails, one plain sentence.

Adding a check is one module and one line in CHAIN below. The chain runs in order and
stops at the first failure. A check that raises counts as a failure, never as a pass.

What the checks may rely on: `ctx.root` is the checkout (read-only: nothing is ever
written into it); `ctx.core` is THIS BUILD's copy of orbit-core (the image carries it);
`work_copy(ctx)` is a private copy of the checkout with this build's core laid over the
vendored paths, so the only scripts a check ever runs are this build's own, whatever the
commit carries under scripts/.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

CORE_DIR_ENV = "ORBIT_CORE_DIR"
DEFAULT_CORE_DIR = "/opt/orbit-core"
LOCK_REL = "orbit-core.lock"
MANIFEST_REL = "core-manifest.txt"
SCRIPT_TIMEOUT_S = 180
SENTENCE_MAX = 400


class FactsUnavailable(Exception):
    """A fact about the cluster a check needs could not be read here; str() says which."""


class AnotherCore(Exception):
    """The commit pins another orbit-core than this build's, so this build cannot check
    it: an upgrade in progress. A refusal all the same (nothing from the commit is
    applied), but an expected one for the minutes an upgrade takes; str() is the sentence."""


@dataclass
class Facts:
    """What a check may know about the cluster, read lazily. Both are bytes of memory."""

    node_allocatable_bytes: Callable[[], int]
    platform_requests_bytes: Callable[[], int]


def no_facts(why: str = "no cluster was given") -> Facts:
    def unavailable() -> int:
        raise FactsUnavailable(why)
    return Facts(node_allocatable_bytes=unavailable, platform_requests_bytes=unavailable)


@dataclass
class Context:
    root: Path
    core: Path
    version: str
    facts: Facts = field(default_factory=no_facts)
    scratch: Path | None = None
    memo: dict[str, Any] = field(default_factory=dict)


@dataclass
class Result:
    name: str
    ok: bool
    sentence: str | None
    seconds: float
    transient: bool = False    # failed for a reason that is not the commit's (a fact unread)
    upgrade: bool = False      # refused because the commit pins another core than this build's

    def dump(self) -> dict[str, Any]:
        out: dict[str, Any] = {"check": self.name, "ok": self.ok, "seconds": round(self.seconds, 2)}
        if self.sentence:
            out["sentence"] = self.sentence
        if self.transient:
            out["transient"] = True
        if self.upgrade:
            out["upgrade"] = True
        return out


@dataclass
class Verdict:
    ok: bool
    results: list[Result]

    @property
    def failed(self) -> Result | None:
        return next((r for r in self.results if not r.ok), None)

    @property
    def transient(self) -> bool:
        bad = self.failed
        return bool(bad and bad.transient)

    def dump(self) -> dict[str, Any]:
        bad = self.failed
        out: dict[str, Any] = {"ok": self.ok, "checks": [r.dump() for r in self.results]}
        if bad is not None:
            out["check"], out["sentence"] = bad.name, bad.sentence
        return out


def _chain() -> list[Any]:
    from . import catalog, core, fit, generated, render, scaffold, sealed
    # The order is the order they run in. `core` is first: every later check trusts that
    # the vendored core is this build's.
    return [core, scaffold, catalog, generated, render, sealed, fit]


def chain() -> list[Any]:
    return _chain()


def names() -> list[str]:
    return [module.NAME for module in _chain()]


def one_sentence(text: str) -> str:
    """Whatever a check or a script said, as one bounded line."""
    line = " ".join(str(text or "").split())
    return line[:SENTENCE_MAX] + ("…" if len(line) > SENTENCE_MAX else "")


def run_one(module: Any, ctx: Context) -> Result:
    started = time.monotonic()
    transient = upgrade = False
    try:
        sentence = module.check(ctx)
    except FactsUnavailable as e:
        sentence, transient = f"{module.NAME} could not be checked: {e}", True
    except AnotherCore as e:
        sentence, upgrade = str(e), True
    except Exception as e:  # a check that crashes is a refusal, never a pass
        sentence = f"the {module.NAME} check could not run ({e.__class__.__name__})"
    if sentence is not None:
        sentence = one_sentence(sentence) or f"the {module.NAME} check failed"
    return Result(name=module.NAME, ok=sentence is None, sentence=sentence,
                  seconds=time.monotonic() - started, transient=transient, upgrade=upgrade)


def run_chain(ctx: Context, only: list[str] | None = None) -> Verdict:
    """Run the chain (or the named checks, in chain order); stop at the first failure."""
    modules = [m for m in _chain() if only is None or m.NAME in only]
    results: list[Result] = []
    owns_scratch = ctx.scratch is None
    if owns_scratch:
        ctx.scratch = Path(tempfile.mkdtemp(prefix="orbit-check-"))
    try:
        for module in modules:
            result = run_one(module, ctx)
            results.append(result)
            if not result.ok:
                break
    finally:
        if owns_scratch and ctx.scratch is not None:
            shutil.rmtree(ctx.scratch, ignore_errors=True)
            ctx.scratch = None
            ctx.memo.clear()
    return Verdict(ok=all(r.ok for r in results), results=results)


# ---------------------------------------------------------------- shared by the checks

def core_dir(env: Any = None) -> Path:
    env = os.environ if env is None else env
    return Path(env.get(CORE_DIR_ENV) or DEFAULT_CORE_DIR)


def lock_ref(root: Path) -> str | None:
    """The core ref the checkout pins (`ref:` in orbit-core.lock), or None."""
    try:
        for line in (root / LOCK_REL).read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition(":")
            if key.strip() == "ref":
                return value.strip() or None
    except OSError:
        return None
    return None


def vendored_paths(core: Path) -> list[tuple[str, str]]:
    """(path in core, path in an instance) for every line of this build's core-manifest."""
    pairs = []
    for line in (core / MANIFEST_REL).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        src, _, dst = line.partition(" -> ")
        pairs.append((src.strip(), (dst or src).strip()))
    return pairs


def files_under(path: Path) -> dict[str, Path]:
    """{posix path relative to `path`: file} for every regular file under it (a file: {"": it})."""
    if path.is_file():
        return {"": path}
    out: dict[str, Path] = {}
    if path.is_dir():
        for f in sorted(path.rglob("*")):
            if f.is_file() and "__pycache__" not in f.parts:
                out[f.relative_to(path).as_posix()] = f
    return out


def work_copy(ctx: Context) -> Path:
    """A private copy of the checkout with this build's core laid over every vendored
    path. Built once per run. A symlink in the checkout is copied as a link and never
    followed out of the tree."""
    if "work" in ctx.memo:
        return ctx.memo["work"]
    assert ctx.scratch is not None, "run_chain owns the scratch directory"
    work = ctx.scratch / "tree"
    copy_with_core(ctx.root, work, ctx.core)
    ctx.memo["work"] = work
    return work


def copy_with_core(root: Path, dest: Path, core: Path) -> Path:
    """Copy the checkout `root` to `dest` and lay this build's core over every vendored
    path of it: the scripts and the chart that then run from `dest` are this build's,
    whatever the checkout carries. A symlink is copied as a link, never followed."""
    shutil.copytree(root, dest, symlinks=True,
                    ignore=shutil.ignore_patterns(".git", "__pycache__", "node_modules"))
    for src, dst in vendored_paths(core):
        source, target = core / src, dest / dst
        if not source.exists():
            continue
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(source, target)
    return dest


def script_env(scratch: Path) -> dict[str, str]:
    """The environment one of this build's scripts runs in: nothing of the caller's (no
    token, no kubeconfig, no config path)."""
    return {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": str(scratch),
            "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
            "HELM_CACHE_HOME": str(scratch / "helm-cache"),
            "HELM_CONFIG_HOME": str(scratch / "helm-config"),
            "HELM_DATA_HOME": str(scratch / "helm-data")}


def run_script(ctx: Context, key: str, argv: list[str]) -> tuple[int, str]:
    """Run one of this build's scripts in the work copy, once per run; (exit code, the
    last line it wrote). The environment carries nothing of the caller's: no token, no
    kubeconfig, no config path."""
    if key in ctx.memo:
        return ctx.memo[key]
    work = work_copy(ctx)
    env = script_env(ctx.scratch)
    try:
        proc = subprocess.run(argv, cwd=work, env=env, capture_output=True, text=True,
                              timeout=SCRIPT_TIMEOUT_S)
        lines = [line for line in (proc.stdout + "\n" + proc.stderr).splitlines() if line.strip()]
        outcome = (proc.returncode, lines[-1] if lines else "")
    except subprocess.TimeoutExpired:
        outcome = (124, f"it did not finish in {SCRIPT_TIMEOUT_S}s")
    ctx.memo[key] = outcome
    return outcome


def run_scaffolder(ctx: Context) -> tuple[int, str]:
    """This build's scaffolder over the work copy (`make init`)."""
    return run_script(ctx, "init", ["python3", "scripts/instance-init.py"])


def run_generator(ctx: Context) -> tuple[int, str]:
    """This build's generator over the scaffolded work copy (`make sync`)."""
    rc, said = run_scaffolder(ctx)
    if rc != 0:
        return rc, said
    return run_script(ctx, "sync", ["python3", "scripts/catalog-sync.py"])


def first_difference(want: Path, have: Path, *, skip: tuple[str, ...] = ()) -> tuple[str, str] | None:
    """(path, how) for the first file that differs between two trees: `missing` (in want,
    not in have), `extra` (in have only) or `differs`. Paths under any `skip` prefix are
    not compared."""
    def kept(files: dict[str, Path]) -> dict[str, Path]:
        return {rel: f for rel, f in files.items()
                if not any(rel == s or rel.startswith(s.rstrip("/") + "/") for s in skip)}
    a, b = kept(files_under(want)), kept(files_under(have))
    for rel in sorted(set(a) | set(b)):
        if rel not in b:
            return rel, "missing"
        if rel not in a:
            return rel, "extra"
        if a[rel].read_bytes() != b[rel].read_bytes():
            return rel, "differs"
    return None
