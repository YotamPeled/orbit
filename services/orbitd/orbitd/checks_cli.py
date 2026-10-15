"""`orbit check`: the commit gate's checks, from the desk.

  orbit check --list                         the checks, in the order the gate runs them
  orbit check all <instance_dir>             the whole chain; stops at the first failure
  orbit check <name> <instance_dir>          one check

The same chain orbitd runs on every commit before it applies anything from it
(orbitd/checks). Run it on a branch before merging: what passes here passes there. Exit 0
when every check ran passed, 1 when one failed (its sentence on stderr), 2 on a usage
error.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from . import __version__, checks

EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2


def _facts(args: Any) -> checks.Facts:
    from .checks import facts
    node, platform = getattr(args, "node_allocatable_mi", None), getattr(args, "platform_requests_mi", None)
    if node is not None or platform is not None:
        if node is None or platform is None:
            raise ValueError("--node-allocatable-mi and --platform-requests-mi are given together")
        return facts.fixed(int(node), int(platform))
    if getattr(args, "kube", None) is not None:
        return facts.from_kube(args.kube)
    from .backends import Kube
    from .platform import resolve_kubeconfig
    return facts.from_kube(Kube(resolve_kubeconfig(getattr(args, "kubeconfig", None))))


def cmd_check(args: Any) -> int:
    if getattr(args, "list", False):
        for module in checks.chain():
            print(f"{module.NAME:<10} {module.WHAT}")
        return EXIT_OK
    name, instance_dir = getattr(args, "name", None), getattr(args, "instance_dir", None)
    if not name or not instance_dir:
        print("usage: orbit check <name|all> <instance_dir>   (orbit check --list names the checks)", file=sys.stderr)
        return EXIT_USAGE
    if name != "all" and name not in checks.names():
        print(f"orbit check: {name!r} is not a check; the checks are: {', '.join(checks.names())}", file=sys.stderr)
        return EXIT_USAGE
    root = Path(instance_dir)
    if not root.is_dir():
        print(f"orbit check: {root} is not a directory", file=sys.stderr)
        return EXIT_USAGE
    core = Path(getattr(args, "core", None) or checks.core_dir())
    if not (core / checks.MANIFEST_REL).is_file():
        print(f"orbit check: this build's copy of orbit-core is not at {core} (set {checks.CORE_DIR_ENV})", file=sys.stderr)
        return EXIT_USAGE
    try:
        facts = _facts(args)
    except ValueError as e:
        print(f"orbit check: {e}", file=sys.stderr)
        return EXIT_USAGE
    ctx = checks.Context(root=root, core=core, version=getattr(args, "as_version", None) or __version__, facts=facts)
    verdict = checks.run_chain(ctx, only=None if name == "all" else [name])
    if getattr(args, "json", False):
        from .client import write_json_document
        write_json_document(verdict.dump(), sort_keys=True)
    else:
        for result in verdict.results:
            print(f"{'ok  ' if result.ok else 'FAIL'}  {result.name:<10} {result.seconds:5.1f}s")
    bad = verdict.failed
    if bad is not None:
        print(f"orbit check: {bad.name}: {bad.sentence}", file=sys.stderr)
        return EXIT_FAILED
    return EXIT_OK


def add_parser(sub: Any) -> argparse.ArgumentParser:
    chk = sub.add_parser("check", help="run the commit gate's checks on an instance checkout (all, or one by name)")
    chk.add_argument("name", nargs="?", help="`all`, or one check (see --list)")
    chk.add_argument("instance_dir", nargs="?", help="instance checkout (the directory holding instance/config.yaml)")
    chk.add_argument("--list", action="store_true", help="name the checks in the order the gate runs them")
    chk.add_argument("--kubeconfig", help="kubeconfig for the checks that read the node; ORBITD_KUBECONFIG, then KUBECONFIG, then in-cluster")
    chk.add_argument("--node-allocatable-mi", type=int, metavar="MI", help="the node's allocatable memory, instead of asking a cluster")
    chk.add_argument("--platform-requests-mi", type=int, metavar="MI", help="the memory the platform reserves, instead of asking a cluster")
    chk.add_argument("--core", metavar="DIR", help="this build's copy of orbit-core (default: the image's)")
    chk.add_argument("--as-version", metavar="REF", help="check as this core ref (default: this build's)")
    chk.add_argument("--json", action="store_true", help="print the verdict as JSON")
    return chk
