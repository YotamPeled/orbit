"""The catalog is one the generator accepts.

catalog/projects.yaml is hand-written; the generator validates it against the service
chart's schema and its own rules (names, hostnames, sizes, egress, environments) before it
writes anything. A catalog it refuses renders nothing, and the sentence it gives is the
one to act on.
"""

from __future__ import annotations

from . import Context, one_sentence, run_generator, run_scaffolder

NAME = "catalog"
WHAT = "the generator accepts catalog/projects.yaml"


def check(ctx: Context) -> str | None:
    rc, said = run_scaffolder(ctx)
    if rc != 0:
        return f"`make init` refuses this commit, so the catalog was never read: {one_sentence(said)}"
    rc, said = run_generator(ctx)
    if rc != 0:
        return f"the generator refuses the catalog: {one_sentence(said)}"
    return None
