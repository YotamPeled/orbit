"""`orbit features`: the desk commands for the platform's optional features.

Through orbitd (the day-to-day path; one tool call each):
  orbit features list                        every feature and its state
  orbit features enable <feature>            turn one on  (admin; confirmed; a job)
  orbit features disable <feature>           turn one off (admin; confirmed; a job)

On the checkout and the cluster directly (bootstrap and recovery; they work with orbitd
down, and are what orbitd itself runs):
  orbit features status <instance_dir>       the same rows, read from the desk
  orbit features set <instance_dir> <feature> on|off
                                             edit instance/config.yaml (the checkout only)
  orbit features converge <instance_dir>    make the cluster match the checkout
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from . import features as features_module
from . import features_converge as converge
from . import features_edit
from . import orbit_features as rules

EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2


def _kube(args: Any) -> Any:
    if getattr(args, "kube", None) is not None:
        return args.kube
    from .backends import Kube
    from .platform import resolve_kubeconfig
    return Kube(resolve_kubeconfig(getattr(args, "kubeconfig", None)))


def _ops(args: Any, instance_dir: Path) -> Any:
    if getattr(args, "ops", None) is not None:
        return args.ops
    return converge.ClusterOps(instance_dir, _kube(args))


def gate_refusal(args: Any, instance_dir: Path) -> str | None:
    """With --gated: the commit gate's own chain over the checkout, before anything is
    applied from it. The converge Job runs this way: orbitd hands it only a commit it
    accepted, and the Job does not take that on trust."""
    from . import __version__, checks
    from .checks import facts
    core = checks.core_dir()
    if not (core / checks.MANIFEST_REL).is_file():
        return f"this build carries no copy of orbit-core at {core}, so the checkout cannot be checked"
    verdict = checks.run_chain(checks.Context(
        root=instance_dir, core=core, version=__version__, facts=facts.from_kube(_kube(args))))
    bad = verdict.failed
    return None if bad is None else f"the {bad.name} check refuses this checkout: {bad.sentence}"


def cmd_converge(args: Any) -> int:
    instance_dir = Path(args.instance_dir)
    if getattr(args, "gated", False):
        refusal = gate_refusal(args, instance_dir)
        if refusal is not None:
            from .client import write_json_document
            write_json_document({"ok": False, "refused": refusal, "steps": []}, sort_keys=True)
            print(f"orbit features converge: {refusal}", file=sys.stderr)
            return converge.EXIT_REFUSED
    code, report = converge.run(
        instance_dir, _ops(args, instance_dir), dry_run=bool(args.dry_run),
        ready_timeout_s=float(args.ready_timeout), commit=converge.checkout_commit(instance_dir))
    from .client import write_json_document
    write_json_document(report, sort_keys=True)
    if report.get("refused"):
        print(f"orbit features converge: {report['refused']}", file=sys.stderr)
    return code


def collect(args: Any, instance_dir: Path) -> dict[str, Any]:
    """The features status, by the collector orbitd's own tool uses, from the desk: the
    checkout on disk and the cluster behind the kubeconfig. (No Prometheus from here, so
    no memory column; orbit_features_list carries it.)"""
    from types import SimpleNamespace

    from . import features_status
    shim = SimpleNamespace(backends=SimpleNamespace(
        instance=SimpleNamespace(root=str(instance_dir)), kube=_kube(args), prom=None))
    return features_status.collect(shim)


def cmd_status(args: Any) -> int:
    from . import features_status
    instance_dir = Path(args.instance_dir)
    data = collect(args, instance_dir)
    if args.json:
        from .client import write_json_document
        write_json_document(data, sort_keys=True)
    else:
        if data["problem"]:
            print(f"orbit features status: {data['problem']}", file=sys.stderr)
        for row in data["features"]:
            print(features_status.sentence(row))
        if data["converge_job"]:
            print(f"a converge Job is running: {data['converge_job']}")
    wrong = [row for row in data["features"] if not row["always"] and (
        row["pending"] or (row["declared"] and (row["not_ready"] or any(not r["met"] for r in row["requires"]))))]
    return EXIT_OK if not wrong and not data["problem"] else EXIT_FAILED


def tool_call(args: Any) -> tuple[str, list[str]]:
    """The orbitd tool and its arguments for a through-orbitd command."""
    if args.features_command == "list":
        return "orbit_features_list", []
    enabled = "true" if args.features_command == "enable" else "false"
    flags = [f"feature={args.feature}", f"enabled={enabled}"]
    if getattr(args, "dry_run", False):
        flags.append("dry_run=true")
    return "orbit_features_set", flags


THROUGH_ORBITD = ("list", "enable", "disable")


def cmd_set(args: Any) -> int:
    instance_dir = Path(args.instance_dir)
    if args.state not in ("on", "off"):
        print("orbit features set: the state is `on` or `off`", file=sys.stderr)
        return EXIT_USAGE
    config_path = instance_dir / features_module.CONFIG_REL
    try:
        registry = rules.load_registry(instance_dir)
        text, want = features_edit.set_feature(
            config_path.read_text(encoding="utf-8"), registry, args.feature, args.state == "on")
    except OSError as e:
        print(f"orbit features set: {features_module.CONFIG_REL} cannot be read ({e.__class__.__name__})", file=sys.stderr)
        return EXIT_USAGE
    except rules.FeatureError as e:
        print(f"orbit features set: {e}", file=sys.stderr)
        return EXIT_USAGE
    with open(config_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    print(f"{args.feature} is {args.state} in {features_module.CONFIG_REL}: "
          + ", ".join(f"{name}={'on' if on else 'off'}" for name, on in want.items()))
    print("next: `make init` and `make sync` (re-render the tree), commit and merge, then "
          "`orbit features converge` (make the cluster match)")
    return EXIT_OK


def cmd_features(args: Any) -> int:
    command = getattr(args, "features_command", None)
    if command == "converge":
        return cmd_converge(args)
    if command == "status":
        return cmd_status(args)
    if command == "set":
        return cmd_set(args)
    print("orbit features: unknown command", file=sys.stderr)
    return EXIT_USAGE


def add_parser(sub: Any) -> argparse.ArgumentParser:
    feat = sub.add_parser("features", help="the platform's optional features: status, switch, converge")
    fsub = feat.add_subparsers(dest="features_command", required=True)
    ls = fsub.add_parser("list", help="every feature and its state, asked of orbitd (the orbit_features_list tool)")
    ls.add_argument("--url", help="MCP URL; defaults to the stored session")
    ls.add_argument("--json", nargs="?", const=True, default=False, metavar="ARGS", help="print structuredContent as JSON")
    for verb, word in (("enable", "on"), ("disable", "off")):
        sw = fsub.add_parser(verb, help=f"turn one feature {word} through orbitd (the orbit_features_set tool): admin, confirmed, a job")
        sw.add_argument("feature", help="a feature, as `orbit features list` names it")
        sw.add_argument("--url", help="MCP URL; defaults to the stored session")
        sw.add_argument("--dry-run", action="store_true", help="report what would be applied and retired; change nothing")
        sw.add_argument("--json", nargs="?", const=True, default=False, metavar="ARGS", help="print structuredContent as JSON")
        confirm = sw.add_mutually_exclusive_group()
        confirm.add_argument("--yes", action="store_true", help="confirm without prompting")
        confirm.add_argument("--no", action="store_true", help="refuse without prompting")
    st = fsub.add_parser("status", help="one line per feature, read from the desk: declared in the checkout, applied and ready on the cluster, what it needs; exit 0 when nothing is wrong")
    st.add_argument("instance_dir", help="instance checkout (the directory holding instance/config.yaml)")
    st.add_argument("--kubeconfig", help="kubeconfig path; ORBITD_KUBECONFIG, then KUBECONFIG, then in-cluster")
    st.add_argument("--json", action="store_true", help="print the rows as JSON")
    se = fsub.add_parser("set", help="switch one feature in the checkout's instance/config.yaml (nothing is applied)")
    se.add_argument("instance_dir", help="instance checkout (the directory holding instance/config.yaml)")
    se.add_argument("feature", help="a feature of provisioning/features.yaml")
    se.add_argument("state", help="on or off")
    cv = fsub.add_parser("converge", help="apply the Applications of features turned on, retire those of features turned off, re-apply what changes with them")
    cv.add_argument("instance_dir", help="instance checkout (the directory holding instance/config.yaml)")
    cv.add_argument("--kubeconfig", help="kubeconfig path; ORBITD_KUBECONFIG, then KUBECONFIG, then in-cluster")
    cv.add_argument("--dry-run", action="store_true", help="print what would be done; change nothing")
    cv.add_argument("--gated", action="store_true",
                    help="first run the commit gate's checks on the checkout and apply nothing if one fails")
    cv.add_argument("--ready-timeout", default=str(converge.READY_TIMEOUT_S), metavar="SECONDS",
                    help="how long to wait for a turned-on feature's workloads to be ready")
    return feat
