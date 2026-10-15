"""Every project environment renders.

Each generated Application is the service chart plus that pair's values. A values
combination the chart refuses (its schema, a template `fail`) cannot be applied, and
orbitd would find out only at apply time, pair by pair. The Applications the commit
carries are rendered here, with this build's chart, before anything is applied.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from . import Context, one_sentence, work_copy

NAME = "render"
WHAT = "the service chart renders for every project environment"
GENERATED_DIRS = ("apps", "orbit")


def rendered_pairs(ctx: Context) -> tuple[list[tuple[str, list[dict[str, Any]]]], str | None]:
    """([(Application name, its rendered objects)], None), or ([], the failing sentence).
    Rendered once per run; the fit check reads the same objects."""
    if "pairs" in ctx.memo:
        return ctx.memo["pairs"]
    from types import SimpleNamespace

    from ..backends import BackendError
    from ..render import _helm_template    # how orbitd itself renders a pair's Application
    # the chart comes from the work copy (this build's); the values from the commit itself
    work: Path = work_copy(ctx)
    checkout = SimpleNamespace(backends=SimpleNamespace(instance=SimpleNamespace(require=lambda: work)))
    pairs: list[tuple[str, list[dict[str, Any]]]] = []
    failure: str | None = None
    for directory in GENERATED_DIRS:
        for path in sorted((ctx.root / "clusters" / directory).glob("*.yaml")):
            try:
                docs = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
            except yaml.YAMLError:
                failure = failure or f"clusters/{directory}/{path.name} is not YAML"
                continue
            for doc in docs:
                if not isinstance(doc, dict) or doc.get("kind") != "Application":
                    continue
                name = str((doc.get("metadata") or {}).get("name") or path.stem)
                try:
                    pairs.append((name, _helm_template(checkout, doc)))
                except BackendError as e:
                    failure = failure or f"{name} does not render: {one_sentence(e.body or e.what_failed)}"
    out: tuple[list[Any], str | None] = ([], failure) if failure else (pairs, None)
    ctx.memo["pairs"] = out
    return out


def check(ctx: Context) -> str | None:
    _, failure = rendered_pairs(ctx)
    return failure
