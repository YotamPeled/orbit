#!/usr/bin/env python3
"""
Point every environment that claims this branch at a newly built image tag.

Runs in the platform repo from a repository_dispatch. The branch->environment mapping
lives in the catalog, so retargeting an environment at a different branch is a catalog
commit and the app repo's workflow never changes.
"""
import os
import pathlib
import re
import subprocess
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
CATALOG = ROOT / "catalog" / "projects.yaml"
TAGS = ROOT / "catalog" / "tags.yaml"

DEFAULT_BRANCH = {"prod": "main", "dev": "dev"}

TAGS_HEADER = """\
# Image tags, written by CI — never by hand.
#
# Split from projects.yaml deliberately: that file is hand-edited and its diffs are the
# review surface, so a bot rewriting it would make every deploy look like a config change.
# Here a deploy is one obvious line, and `git log catalog/tags.yaml` is the deploy history.
#
# Key is <project>/<env>/<component>. A missing key means the component's catalog default
# tag is used.
"""

# The dispatch payload arrives from an app repo's CI and is untrusted input: anyone
# holding the dispatch token can shape it. Validate before it reaches a file that Argo
# will apply to the cluster.
# fullmatch, not match: Python's `$` also matches before a trailing newline, so a tag of
# "v1\n" passed validation and then wrote a multi-line YAML scalar that broke the chart.
TAG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
IMAGE_RE = re.compile(r"[a-z0-9][a-z0-9._/-]{0,255}")
BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,200}")
REPO_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")
ID_RE = re.compile(r"[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?")
# a component name is a DNS label: the chart truncates <project>-<component> to 63, the
# catalog does not hold the name to the (shorter) project-id class
LABEL_RE = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")


def shown_key(pid, comp) -> str:
    """project/component for a note or refusal: catalog text, printed only when it has the
    shape a catalog id has; anything else is described, never repeated (round 2)."""
    pid = str(pid) if ID_RE.fullmatch(str(pid)) else "(a project id not of the catalog shape)"
    comp = str(comp) if LABEL_RE.fullmatch(str(comp)) else "(a component name not of the label shape)"
    return f"{pid}/{comp}"
# The operator ship path (scripts/ship.sh) names ONE project/env/component and the human
# confirms exactly that; the dispatch path names none and fans out to every environment
# that claims the branch. When these are set, hits are restricted to the named target and a
# target that the branch does not reach is a refusal, never a silent no-op.
SHIP_KEYS = ("ORBIT_SHIP_PROJECT", "ORBIT_SHIP_ENV", "ORBIT_SHIP_COMPONENT")


def normalise(ref: str) -> str:
    """Compare image references without being fooled by an implicit registry."""
    ref = ref.split("@")[0]                       # strip any digest
    head, _, tail = ref.rpartition(":")           # strip a tag, but not a port
    if head and "/" not in tail:
        ref = head
    if ref.startswith("ghcr.io/"):
        ref = ref[len("ghcr.io/"):]
    return ref.strip("/")


def main() -> int:
    try:
        image = os.environ["IMAGE"]  # e.g. <registry-org>/<service>-api
        tag = os.environ["TAG"]      # e.g. main-a1b2c3d
        branch = os.environ["BRANCH"]
        repo = os.environ.get("REPO", "")
    except KeyError as e:
        sys.exit(f"missing dispatch field {e}")

    # A dispatch value is untrusted text: a refusal names the field, never the value.
    if not TAG_RE.fullmatch(tag):
        sys.exit("refusing tag: not a valid image tag (the value is not repeated)")
    if not IMAGE_RE.fullmatch(image):
        sys.exit("refusing image: not a valid image reference (the value is not repeated)")
    if not BRANCH_RE.fullmatch(branch):
        sys.exit("refusing branch: not a valid branch name (the value is not repeated)")
    if repo and not REPO_RE.fullmatch(repo):
        sys.exit("refusing repo: not an owner/name (the value is not repeated)")
    target = tuple(os.environ.get(k, "") for k in SHIP_KEYS)
    if any(target):
        if not all(target) or not ID_RE.fullmatch(target[0]) or not ID_RE.fullmatch(target[1]) \
                or not LABEL_RE.fullmatch(target[2]):
            sys.exit("refusing ship target: ORBIT_SHIP_PROJECT/ENV/COMPONENT must all be set and be catalog ids (the values are not repeated)")
    else:
        target = None
    if "@" in image:
        sys.exit("refusing a digest-pinned image: a tag cannot be appended to a digest")

    # The durable receiver asks for this validated, canonical effect key before it
    # recomputes any catalog output. Legacy invocations take the unchanged path below.
    if len(sys.argv) > 1:
        if sys.argv[1:] != ["--receipt-key"]:
            sys.exit("usage: bump-tag.py [--receipt-key]")
        print(f"{image}:{tag}")
        return 0
    want = normalise(image)

    cat = yaml.safe_load(CATALOG.read_text())
    doc = yaml.safe_load(TAGS.read_text()) or {}
    tags = doc.get("tags") or {}
    doc_tags_before = dict(tags)

    hits = []
    refused = []
    owned_any = False
    for p in cat["projects"]:
        for comp in p.get("components") or []:
            # Match on the FULL image reference, registry and org included. Matching on
            # the basename alone means a build of one project rewrites the deployed tag
            # of every component whose image happens to end in the same word — "api",
            # "web" and "app" collide across orgs immediately.
            if normalise(comp.get("image", "")) != want:
                continue
            # A dispatch says which image it built; it does not get to say which project
            # that image belongs to. Without this, any holder of the dispatch token can
            # retag any project's production environment.
            # Both sides are mandatory. The earlier `if repo and owner and ...` form failed
            # open twice over: a dispatch that simply omitted `repo` interpolated to an
            # empty string and skipped the comparison entirely, and any catalog project
            # with no `repo:` key was deployable by anyone. An authorization check that
            # is skipped when its input is missing is not an authorization check.
            # A project may be assembled from more than one repository: an engine built
            # from one repo and the front end from a website repo is one project with two
            # sources. Keying ownership on the project alone
            # refused the front end's own deploy — correctly, given what the catalog said,
            # which is what made the gap visible rather than exploitable.
            owner = (comp.get("repo") or p.get("repo") or "").lower()
            if not repo:
                sys.exit("refusing: dispatch carries no repo, so ownership cannot be checked")
            if not owner:
                # Undeployable by dispatch, but skipped rather than fatal for the same
                # reason as the mismatch below: one unowned project sharing a base image
                # must not break a different project's legitimate deploy.
                refused.append(shown_key(p["id"], comp["name"]) + " (no repo: in the catalog)")
                continue
            owned_any = owned_any or (repo.lower() == owner)
            if repo.lower() != owner:
                # Skip this project, do NOT abort the run. Two projects may legitimately
                # share a base image — the catalog already deploys stock
                # nginx-unprivileged — and aborting meant the second project's existence
                # silently broke the first project's deploys. Not being the owner of THIS
                # project is the normal case, not an attack.
                # the owner is catalog text of no validated shape: not repeated
                refused.append(shown_key(p["id"], comp["name"]) + " (built from another repo)")
                continue
            for env, envcfg in (p.get("environments") or {}).items():
                envcfg = envcfg or {}
                if env != "prod" and not envcfg.get("enabled"):
                    continue
                # A component may exist in only some environments. Writing a tag for an
                # environment it does not render in leaves a key in tags.yaml that nothing
                # consumes, and a reader of the deploy history would conclude the component
                # is deployed there.
                comp_envs = comp.get("environments")
                if comp_envs is not None and env not in comp_envs:
                    continue
                if envcfg.get("branch", DEFAULT_BRANCH.get(env)) == branch:
                    if target is not None and (p["id"], env, comp["name"]) != target:
                        continue          # the operator confirmed one target; siblings on the branch are theirs to ship
                    hits.append(f"{p['id']}/{env}/{comp['name']}")

    if target is not None and not hits:
        # the ship path resolved the branch FROM this env, so a miss means the catalog the
        # bump runs on (the clone of main) is not the one the operator was shown
        sys.exit("refusing: the named project/env/component does not claim this branch in the catalog on main")
    if not hits:
        # Distinguish "nothing claims this branch" from "everything that matched was
        # refused". The second is an authorization event and must not read as a no-op.
        # Only an authorization failure when the caller owned NOTHING that matched. If it
        # owned a project and that project simply has no environment on this branch, that
        # is an ordinary "built, deployed nowhere" — exiting non-zero there failed the CI
        # job of the legitimate owner because some OTHER project shared the base image.
        if refused and not owned_any:
            sys.exit("refusing: the dispatching repo owns none of the projects using this image: "
                     + ", ".join(refused))
        print(f"no environment claims branch {branch!r} — image built, deployed nowhere")
        return 0
    if refused:
        print(f"note: skipped {len(refused)} project(s) not owned by the dispatching repo: " + ", ".join(refused))

    for key in hits:
        print(f"{key}: {tags.get(key, '(unset)')} -> {tag}")
        tags[key] = tag
    if all(doc_tags_before.get(key) == tag for key in hits):
        # a re-ship of the deployed tag, or a rollback to it: a decision, not a missing claim
        print("already current: " + ", ".join(hits) + " — nothing to commit")

    # Rebuild from a constant header rather than splitting the existing file on "tags:" —
    # the split form silently truncates the header the day any comment contains that word.
    TAGS.write_text(TAGS_HEADER + yaml.safe_dump({"tags": dict(sorted(tags.items()))}, sort_keys=True))
    subprocess.run(["python3", str(ROOT / "scripts" / "catalog-sync.py")], check=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
