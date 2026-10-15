"""The generated manifests are what the generator writes for this catalog.

clusters/apps and clusters/orbit are the generator's output (`make sync`). orbitd applies
what is committed there, so a catalog change committed without its regenerated manifests
(or a generated file edited by hand) would run something the catalog does not describe.
"""

from __future__ import annotations

from . import Context, first_difference, one_sentence, run_generator, work_copy

NAME = "generated"
WHAT = "clusters/apps and clusters/orbit are exactly what `make sync` writes for the catalog"
GENERATED_DIRS = ("apps", "orbit")


def check(ctx: Context) -> str | None:
    rc, said = run_generator(ctx)
    if rc != 0:
        return f"the generator did not run on this commit: {one_sentence(said)}"
    for directory in GENERATED_DIRS:
        diff = first_difference(work_copy(ctx) / "clusters" / directory, ctx.root / "clusters" / directory)
        if diff is None:
            continue
        rel, how = diff
        path = f"clusters/{directory}/{rel}"
        if how == "extra":
            return f"{path} is not a file the generator writes for this catalog (run `make sync` and commit)"
        if how == "missing":
            return f"{path} is missing: the generator writes it for this catalog (run `make sync` and commit)"
        return (f"{path} is not what the generator writes for catalog/projects.yaml and catalog/tags.yaml "
                f"(run `make sync` and commit; generated files are never edited by hand)")
    return None
