#!/usr/bin/env python3
"""
The extraction oracle: the core carries no instance strings.

The whole point of instance/config.yaml is that everything an instance VENDORS is CORE —
it must work unchanged for any org, so any value read out of the instance file that
appears in one of those files is a leak. The scope is therefore core-manifest.txt itself
(see core_scope below), not a second list beside it. Run from the repo root (core or
instance, either works):

    scripts/verify/no-instance-strings.py            # 0 clean, 1 leaks, 2 could not run
    scripts/verify/no-instance-strings.py --print-scope

Extracted from scripts/verify/stage3.sh on 2026-09-07, when `.claude/skills` joined the
scope. It is a file rather than a heredoc for the reason no-credentials.py and
verify/am-routes.py already are: ONE implementation with two callers — the live suite and
a core CI test that plants a string and proves the check goes red — because a check whose
only copy runs on a cluster is a check nobody has ever watched fail. The scope
interpolation this replaces had a bug of exactly that family (an `ls` newline landing
inside a Python string literal made the check pass vacuously from the day it was written,
found 2026-08-28), which is also why the matching is done here in Python instead of by
shelling out to grep with an interpolated path list.

FAIL CLOSED, in four places, because "no output" must never be able to mean "nothing
ran": a config that cannot be read, a missing core-manifest.txt (which would leave the
scope empty), a scope path that does not exist, and a file that cannot be read all exit 2
and say so, rather than reporting a clean tree.
"""
import os
import pathlib
import shown as display   # the shared display rule (scripts/verify/shown.py)
import re
import sys

import yaml

# The scope is core-manifest.txt — the list of what an instance VENDORS — plus the two
# core files that reach an instance by other means (`Makefile` comes with template/, and
# the tofu module is enumerated file by file in the manifest but globbed here so a new
# .tf cannot arrive unchecked). Deriving it rather than repeating it is the fix for a
# real hole found in review on 2026-09-07: the hand-kept list named the generator, the
# chart and the skills, and left `pitr-drill.sh`, `core-sync.sh` and the three verify
# stages — all vendored, all shipped to every org — outside the check, while stage3
# reported "core carries no instance strings" over them.
#
# So there is now ONE list, and adding a path to core-manifest.txt is what puts it in
# scope. scripts/test_no_instance_strings.py asserts the two agree as full sets, so the
# derivation cannot quietly stop deriving.
#
# tofu STATE is instance data by nature (it records this org's real server) and is not in
# the manifest; the .tf CODE is, and stays in scope.
#
# An instance repo keeps its OWN tenant skills under .claude/skills beside the vendored
# ones, and those legitimately name the org's projects and
# hostnames. That is why the scope is the manifest and not the directory: only what core
# ships is core.
EXTRA_SCOPE = ["Makefile", "tofu/*.tf"]


def core_scope(root: pathlib.Path) -> list[str]:
    """Every path core-manifest.txt vendors, resolved for THIS tree, plus EXTRA_SCOPE."""
    manifest = root / "core-manifest.txt"
    if not manifest.is_file():
        die(f"no core-manifest.txt at {root / 'core-manifest.txt'} — the scope is "
            "derived from it, so without it nothing would be checked")
    scope = []
    for line in manifest.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # `<core path> -> <instance path>`: the one manifest form whose path differs by
        # side (core keeps the artifact under template/, because that is how a NEW org
        # gets it; an instance has it at the top). Either spelling names the same file,
        # so the scope takes whichever spelling THIS tree actually has — and falls back
        # to the instance path so that a genuinely missing file is named the way the
        # reader will look for it.
        src, _, dst = (x.strip() for x in line.partition(" -> "))
        dst = dst or src
        # a manifest entry names a path INSIDE the instance: an absolute path or one that
        # climbs out (`../x`, or a symlink resolving outside) would put a file of this
        # machine's into the scope and its report (round 16, breaker); the entry is not
        # repeated
        for cand in (src, dst):
            if pathlib.PurePosixPath(cand).is_absolute() or ".." in pathlib.PurePosixPath(cand).parts:
                die("core-manifest.txt has an entry that leaves the instance root (an absolute path or a `..` component)")
        scope.append(dst if (root / dst).exists() else
                     (src if (root / src).exists() else dst))
    scope.extend(EXTRA_SCOPE)
    return list(dict.fromkeys(scope))


def shown(rel: pathlib.Path) -> str:
    """A path in a report, component by component: a name not of a file-name shape is
    replaced, never repeated (round 19: a scope file can be named after a token)."""
    return display.shown_parts("/".join(rel.parts))


def die(msg: str) -> None:
    """Exit 2 — the check did not run. Never 0, never 1."""
    print(f"the instance-string leak check did not run: {msg}", file=sys.stderr)
    sys.exit(2)


def walk(top: pathlib.Path) -> list[pathlib.Path]:
    """Every file under a scope directory. A symlinked directory is not descended (the
    generator writes none, and a walk that followed one could leave the tree) and it is
    not skipped either: a directory the check would not read is a scope that shrank, so it
    is refused (round 21)."""
    out = []
    def unlisted(e):
        # os.walk swallows a listing error by default; a directory the check could not
        # read is a scope that shrank (round 22) — its class, never its path
        die(f"a scope directory could not be listed ({type(e).__name__}; the path is not repeated)")
    for dirpath, dirnames, filenames in os.walk(top, followlinks=False, onerror=unlisted):
        for d in dirnames:
            if os.path.islink(os.path.join(dirpath, d)):
                die("a scope directory contains a symlinked directory (not repeated) — its files would go unchecked")
        out.extend(pathlib.Path(dirpath) / n for n in filenames)
    return sorted(out)


def scope_files(root: pathlib.Path) -> list[pathlib.Path]:
    files = []
    for ei, entry in enumerate(core_scope(root), 1):
        if "*" in entry:
            # A glob that matches nothing is a scope that silently shrank. The entry is
            # manifest text: named by position, never repeated (round 20)
            matched = sorted(root.glob(entry))
            if not matched:
                die(f"scope entry #{ei} (a glob pattern; not repeated) matched no file under the instance root")
            targets = matched
        else:
            targets = [root / entry]
        for t in targets:
            if not t.exists():
                die(f"scope path {shown(t.relative_to(root))} does not exist")
            try:
                inside = t.resolve().is_relative_to(root.resolve())
            except OSError:
                inside = False
            if not inside:
                die("a scope path resolves outside the instance root (a symlink pointing out, or a `..` component)")
            files.extend(walk(t) if t.is_dir() else [t])
    for f in files:
        try:
            inside = f.resolve().is_relative_to(root.resolve())
        except OSError:
            inside = False
        if not inside:
            die("a scope path resolves outside the instance root (a symlink pointing out, or a `..` component)")
    return files


def instance_values(config: pathlib.Path) -> tuple[list[str], str]:
    if not config.is_file():
        die(f"no instance config at {config}")
    try:
        c = yaml.safe_load(config.read_text())
        # instance.name joined this list on 2026-09-07, found by review: it is the value
        # the most core paths are DERIVED from — `$ORBIT_CONFIG_DIR` is
        # `~/.config/<name>-platform`, the kubeconfig is `~/.kube/config-<name>-platform`,
        # the buckets carry it as a prefix — so it is exactly the string a skill or a
        # script hardcodes when it stops reading lib-instance.sh, and it was the one
        # identity field nothing checked.
        # each value in the CANONICAL form the loader exports it in (surrounding whitespace
        # dropped; a DNS name's trailing dot dropped and lowercased) — the spelling a
        # vendored file would carry; round 15: a padded config value was searched verbatim
        # and matched nothing
        def dns(v): return str(v).strip().rstrip(".").lower()
        values = [("instance.name", str(c["instance"]["name"]).strip()),
                  ("domains.primary", dns(c["domains"]["primary"])), ("domains.dev", dns(c["domains"]["dev"])),
                  ("registry", str(c["registry"]).strip()),
                  ("git.github_org", str(c["git"]["github_org"]).strip()), ("git.repoUrl", str(c["git"]["repoUrl"]).strip())]
        # every DNS zone is an identity too (the loader exports them all)
        values += [(f"domains.dnsZones[{i}]", dns(z)) for i, z in enumerate(c["domains"].get("dnsZones") or [])]
        values += [(f"nodes[{i}].ip", str(n["ip"]).strip()) for i, n in enumerate(c.get("nodes") or [])]
        values += [(f"nodes[{i}].name", str(n["name"]).strip()) for i, n in enumerate(c.get("nodes") or [])]
        primary = dns(c["domains"]["primary"])
    except Exception as e:                                    # noqa: BLE001
        # the class only: a YAML error's text quotes the line around the error, and a
        # config may hold a pasted value there
        die(f"{config} is not a readable instance config ({type(e).__name__})")
    # (field, value) pairs, one per distinct value: a report names the FIELD, never the
    # value — a config value (a repo URL with userinfo pasted in) may be a credential
    first = {}
    for field, v in values:
        if v:                      # an empty value would match every line
            first.setdefault(str(v), field)
    return [(field, v) for v, field in first.items()], str(primary)


def main() -> int:
    args = sys.argv[1:]
    root = pathlib.Path.cwd()
    config = pathlib.Path(next((a.split("=", 1)[1] for a in args
                                if a.startswith("--config=")),
                               root / "instance" / "config.yaml"))
    if "--print-scope" in args:
        print("\n".join(core_scope(root)))
        return 0

    values, _primary = instance_values(config)
    # There are NO exemptions, and there used to be exactly one: the `<primary domain>/`
    # label-key prefix, waved through because the chart, the namespaces and the
    # NetworkPolicies shared it as an API schema. It exempted the whole LINE, so
    # `curl https://<primary>/health` in a skill was certified clean — found by review on
    # 2026-09-07, and reproduced as test_a_planted_url_in_a_skill_fails. The prefix itself
    # is now the constant `orbit.local/`, so there is nothing left to exempt: an exemption
    # that has to guess whether a match is a label key or a URL is a hole either way.

    leaks = []
    for f in scope_files(root):
        try:
            text = f.read_text(errors="replace")
        except OSError as e:
            # the name is tree text and the OS message repeats it: the class alone (round 21)
            die(f"cannot read {shown(f.relative_to(root))} ({type(e).__name__})")
        for field, v in values:
            # The value as an IDENTITY: embedded in a path or a flag (`config-<name>-platform`,
            # `kubeconfig-<name>`) it is a leak; as the inside of a longer word it is not
            # (round 2, 2026-09-13: a four-letter instance name matched an ordinary English
            # word in a vendored stage). Letters and digits on either side make it part of
            # another word.
            pat = re.compile(r"(?<![a-z0-9])" + re.escape(v.lower()) + r"(?![a-z0-9])")
            if not pat.search(text.lower()):
                continue
            for n, line in enumerate(text.splitlines(), 1):
                if not pat.search(line.lower()):
                    continue
                leaks.append(f"the value of {field} in {shown(f.relative_to(root))}:{n}")
    if leaks:
        print("\n".join(leaks))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
