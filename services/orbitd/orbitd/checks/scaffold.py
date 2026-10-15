"""The platform tree is what the scaffolder renders from the config.

clusters/ (outside the generator's directories and the sealed secrets) is the output of
`make init`: the staged tree rendered for this instance's config and features. Every
manifest there is one `make init` renders, byte for byte. A commit that edits a rendered
file by hand, changes the config without re-running `make init`, or adds a manifest of
its own would have the desk and the converge Job apply something no config asked for.

Only manifests are held to this (`*.yaml`, `*.yml`): a README or a `.gitkeep` beside
them is applied by nothing.
"""

from __future__ import annotations

from . import Context, files_under, one_sentence, run_scaffolder, work_copy

NAME = "scaffold"
WHAT = "every manifest under clusters/ is exactly what `make init` renders from instance/config.yaml"
# what the scaffolder does not own: the generator's output and the sealed secrets
NOT_SCAFFOLDED = ("apps/", "orbit/", "platform/secrets/")


def rendered_from_nothing(ctx: Context) -> set[str]:
    """The manifests the scaffolder writes under clusters/, found by running it on a second
    work copy whose clusters/ holds no manifest of the scaffolder's at all."""
    import shutil

    from . import run_script
    work = work_copy(ctx)
    bare = ctx.scratch / "bare"          # type: ignore[operator]
    if not bare.exists():
        shutil.copytree(work, bare, symlinks=True)
        for rel, path in files_under(bare / "clusters").items():
            if rel.endswith((".yaml", ".yml")) and not rel.startswith(NOT_SCAFFOLDED):
                path.unlink()
        ctx.memo.pop("init-bare", None)
    import os
    import subprocess

    from . import SCRIPT_TIMEOUT_S
    if "init-bare" not in ctx.memo:
        env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": str(ctx.scratch),
               "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}
        try:
            proc = subprocess.run(["python3", "scripts/instance-init.py"], cwd=bare, env=env,
                                  capture_output=True, text=True, timeout=SCRIPT_TIMEOUT_S)
            ctx.memo["init-bare"] = proc.returncode
        except subprocess.TimeoutExpired:
            ctx.memo["init-bare"] = 124
    del run_script
    return {rel for rel in files_under(bare / "clusters")
            if rel.endswith((".yaml", ".yml")) and not rel.startswith(NOT_SCAFFOLDED)}


def check(ctx: Context) -> str | None:
    rc, said = run_scaffolder(ctx)
    if rc != 0:
        return f"`make init` refuses this commit: {one_sentence(said)}"
    # `rendered` is what the scaffolder leaves in a tree that started EMPTY of manifests:
    # exactly the files it writes. A manifest in the commit that is not among them is
    # either one `make init` removes (a closed gate) or one it never knew.
    want = files_under(work_copy(ctx) / "clusters")
    have = files_under(ctx.root / "clusters")
    rendered = rendered_from_nothing(ctx)
    for rel in sorted(set(want) | set(have)):
        if rel.startswith(NOT_SCAFFOLDED) or not rel.endswith((".yaml", ".yml")):
            continue
        if rel not in want:
            # in the commit, and `make init` deletes it: a file behind a closed gate
            return (f"clusters/{rel} is in the commit and `make init` removes it for this config "
                    f"(its feature is off, or its component was retired): run `make init` and commit")
        if rel in have and rel not in rendered:
            return (f"clusters/{rel} is not a file `make init` renders: a platform manifest comes from "
                    f"the core's staged tree and the config, never from a file added by hand")
        if rel not in have:
            return f"clusters/{rel} is missing: `make init` renders it for this config (run `make init` and commit)"
        if want[rel].read_bytes() != have[rel].read_bytes():
            return (f"clusters/{rel} is not what `make init` renders from instance/config.yaml "
                    f"(run `make init` and commit; rendered files are never edited by hand)")
    return None
