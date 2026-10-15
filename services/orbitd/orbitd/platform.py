"""Render the platform layer from ArgoCD Application files and diff it against live.

`orbit platform plan <instance-dir>` never writes to the cluster. Chart sources run
`helm template` (`--repo` for HTTP repos, `oci://` for OCI, a local path when the
chart is on disk) with `--no-hooks`: Helm hook resources are not part of the desired
set. Directory sources walk YAML under `spec.source.path`. Compare is the shadow
compare: generated Secrets (TLS-shaped, or `orbit.local/generated-secrets` on the
Application) are listed next to hooks and owned, and do not keep `empty` false.

Helm output and directory documents are loaded with a SafeLoader that constructs the
YAML 1.1 `=` scalar (`tag:yaml.org,2002:value`) as the string `"="`.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml

from .backends import Kube
from .helm import (
    HELM, HelmFailure, chart_name, dns_label, documents_bounded, remote_repo, revision,
    run_helm, tree_under_root, under_root,
)
from .shadow import (
    LABEL_GENERATED_SECRETS, PLATFORM_LIVE_GVKS, _apply_namespace, _canonicalise_secret,
    _fingerprint, compare_objects, read_live,
)

_FINGERPRINT_RE = re.compile(r"sha256:[0-9a-f]{16}")
PLATFORM_APP = "platform"
APPS_REL = "clusters/platform/apps"
KIND_APPLICATION = "Application"
KIND_NAMESPACE = "Namespace"
CREATE_NAMESPACE = "CreateNamespace=true"
YAML_VALUE_TAG = "tag:yaml.org,2002:value"

EXIT_EMPTY = 0
EXIT_DRIFT = 1
EXIT_RENDER = 2


class _ValueLoader(yaml.SafeLoader):
    """SafeLoader plus YAML 1.1 `=` as the string `"="`."""


def _construct_yaml_value(_loader: Any, _node: Any) -> str:
    return "="


_ValueLoader.add_constructor(YAML_VALUE_TAG, _construct_yaml_value)


def load_yaml_documents(text: str) -> list[dict[str, Any]]:
    """Parse Helm or directory YAML; `=` scalars become the string `"="`."""
    return [d for d in yaml.load_all(text, Loader=_ValueLoader) if isinstance(d, dict)]


def resolve_kubeconfig(flag: str | None) -> str | None:
    """`--kubeconfig`, else `ORBITD_KUBECONFIG`, else `KUBECONFIG`, else in-cluster."""
    text = (flag or "").strip()
    if text:
        return text
    for key in ("ORBITD_KUBECONFIG", "KUBECONFIG"):
        value = (os.environ.get(key) or "").strip()
        if value:
            return value
    return None


class RenderError(Exception):
    """Helm or directory render failed for one Application."""

    def __init__(self, app: str, message: str) -> None:
        self.app = app
        self.message = message
        super().__init__(f"{app}: {message}")


class RenderReadError(RenderError):
    """The render could not be READ: the apps directory is missing, a file
    could not be read, or a file could not be parsed. This is never "the app
    is not rendered": callers that treat "not rendered" as permission must
    refuse on this error instead. A subclass of RenderError, so existing
    render-failure handling is unchanged."""


def load_applications(instance_dir: Path, name: str | None = None) -> list[dict[str, Any]]:
    """ArgoCD Application documents under clusters/platform/apps/*.yaml, name-sorted."""
    root = Path(instance_dir)
    apps_dir = root / APPS_REL
    if not apps_dir.is_dir():
        # Never an empty plan: a missing tree is unreadable, and an unreadable tree
        # must not read as "nothing is rendered" (retire would take that as leave to
        # delete every platform object).
        raise RenderReadError(name or PLATFORM_APP, f"{APPS_REL} is not a directory")
    found: list[dict[str, Any]] = []
    for path in sorted({*apps_dir.glob("*.yaml"), *apps_dir.glob("*.yml")}):
        for doc in _load_yaml_docs(path):
            if doc.get("kind") != KIND_APPLICATION:
                continue
            app_name = str((doc.get("metadata") or {}).get("name") or "")
            if not app_name:
                continue
            if name is not None and app_name != name:
                continue
            found.append(doc)
    # glob("*.yaml")+glob("*.yml") can list the same path twice on case-folding fs
    by_name: dict[str, dict[str, Any]] = {}
    for doc in found:
        by_name[str((doc.get("metadata") or {}).get("name") or "")] = doc
    apps = [by_name[k] for k in sorted(by_name)]
    if name is not None and name not in by_name:
        raise RenderError(name, "application not found")
    if not apps:
        raise RenderReadError(PLATFORM_APP, f"no Application under {APPS_REL}")
    return apps


def render_application(instance_dir: Path, application: dict[str, Any]) -> list[dict[str, Any]]:
    """Desired objects for one platform Application. No kube."""
    root = Path(instance_dir)
    app_name = str((application.get("metadata") or {}).get("name") or "")
    if not app_name:
        raise RenderError("", "Application is missing metadata.name")
    if not dns_label(app_name):
        # The name becomes a helm release, a label selector and a report key; a
        # value of another shape is refused before any of those, and not repeated.
        raise RenderError("<not a DNS label>", "Application metadata.name is not a DNS label")
    spec = application.get("spec") or {}
    source = spec.get("source") or {}
    dest_ns = str((spec.get("destination") or {}).get("namespace") or "").strip()
    if dest_ns and not dns_label(dest_ns):
        raise RenderError(app_name, "Application spec.destination.namespace is not a DNS label")
    try:
        if source.get("chart"):
            objects = _render_chart(root, application, app_name, source, dest_ns)
        elif source.get("path"):
            objects = _render_directory(root, app_name, source)
        else:
            raise RenderError(app_name, "Application spec.source has neither chart nor path")
    except RenderError:
        raise
    except Exception as e:
        raise RenderError(app_name, str(e)) from e
    if _create_namespace(spec) and dest_ns:
        objects = [_namespace_object(dest_ns), *objects]
    by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for obj in objects:
        obj = _with_namespace(obj, dest_ns)
        by_key[_obj_key(obj)] = obj
    return [by_key[k] for k in sorted(by_key)]


def _own_client(kube: Any) -> Any:
    """A separate cluster client built the way `kube` was (same kubeconfig, or in-cluster);
    `kube` itself when it is not ours to rebuild (a stand-in in a test, no client at all)."""
    from . import backends
    if type(kube) is backends.Kube:
        return backends.Kube(kube.kubeconfig)
    return kube


# How many apps `plan` works on at once (ORBIT_PLAN_WORKERS; 1 = one after another).
PLAN_WORKERS = max(1, int(os.environ.get("ORBIT_PLAN_WORKERS") or 6))


def plan(
    instance_dir: Path,
    kube: Any,
    *,
    app: str | None = None,
    live_by_app: dict[str, list[dict[str, Any]]] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Render every selected Application, compare to live, return (exit, report)."""
    applications = load_applications(instance_dir, app)
    state = SimpleNamespace(backends=SimpleNamespace(kube=kube))

    def one(application: dict[str, Any], state: Any = state) -> dict[str, Any]:
        name = str((application.get("metadata") or {}).get("name") or "")
        objects = render_application(instance_dir, application)
        if live_by_app is not None:
            # A saved plan carries Secret values only as fingerprints, so the replay
            # compares fingerprints on both sides: drift is still named per key.
            live = secrets_fingerprinted(list(live_by_app.get(name) or []))
            objects = secrets_fingerprinted(objects)
        else:
            live = read_live(state, objects, name, "", kinds=PLATFORM_LIVE_GVKS)
        report = compare_objects(
            objects, live, project=name,
            generated_secret_names=_generated_secret_names(application),
        )
        return _app_payload(name, report, live=secrets_fingerprinted(live))

    # The apps are planned side by side and reported in the order they are declared.
    # One after another, fifteen apps took 845 s from the operator machine (docs/V1.md §8,
    # 2026-10-06): longer than the ten minutes an agent's command may run, so the agent
    # upgrading core never saw its plan. An app's plan reads only its own render and its
    # own live objects; the first failure, in declared order, is the one raised.
    workers = max(1, min(PLAN_WORKERS, len(applications)))
    if workers == 1:
        apps = [one(application) for application in applications]
    else:
        import queue

        from .audit import guarded_thread
        todo: queue.SimpleQueue[int] = queue.SimpleQueue()
        for index in range(len(applications)):
            todo.put(index)
        done: list[Any] = [None] * len(applications)
        failed: dict[int, BaseException] = {}

        def work() -> None:
            # A client of its own for each worker. One client shared by six threads broke
            # on the minimal profile (docs/V1.md §8): its discovery cache is rebuilt when a
            # kind is not found (a retired feature's kinds are not), and a rebuild under
            # another thread's lookup raised KeyError('api') out of the library.
            own = SimpleNamespace(backends=SimpleNamespace(kube=_own_client(kube)))
            while True:
                try:
                    index = todo.get_nowait()
                except queue.Empty:
                    return
                try:
                    done[index] = one(applications[index], own)
                except BaseException as e:  # noqa: BLE001 - raised below, by the caller, in declared order
                    failed[index] = e

        threads = [guarded_thread(target=work, name=f"plan-{n}") for n in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        if failed:
            raise failed[min(failed)]
        apps = done
    body = {
        "empty": bool(apps) and all(row["empty"] for row in apps),
        "apps": apps,
    }
    if not apps:
        body["empty"] = True
    return (EXIT_EMPTY if body["empty"] else EXIT_DRIFT), body


def render_only(instance_dir: Path, *, app: str | None = None) -> list[tuple[str, list[dict[str, Any]]]]:
    """Render every selected Application. No kube. Returns (name, objects) per app."""
    applications = load_applications(instance_dir, app)
    rows: list[tuple[str, list[dict[str, Any]]]] = []
    for application in applications:
        name = str((application.get("metadata") or {}).get("name") or "")
        rows.append((name, render_application(instance_dir, application)))
    return rows


def cmd_plan(args: argparse.Namespace) -> int:
    instance_dir = Path(args.instance_dir)
    if not instance_dir.is_dir():
        print(f"orbit platform plan: {instance_dir} is not a directory", file=sys.stderr)
        return EXIT_RENDER
    render_path = getattr(args, "render_only", None)
    if render_path:
        try:
            rows = render_only(instance_dir, app=args.app)
        except RenderError as e:
            print(f"orbit platform plan: render failed for {e.app}: {e.message}", file=sys.stderr)
            return EXIT_RENDER
        objects = [obj for _, objs in rows for obj in objs]
        Path(render_path).write_text(
            json.dumps(objects, indent=2, sort_keys=True, default=str) + "\n",
        )
        if args.summary:
            for name, objs in rows:
                print(f"{name} rendered={len(objs)}", file=sys.stderr)
        return EXIT_EMPTY
    live_from = getattr(args, "live_from", None)
    live_by_app: dict[str, list[dict[str, Any]]] | None = None
    if live_from:
        try:
            live_by_app = load_live_from(Path(live_from))
        except (OSError, ValueError) as e:
            print(f"orbit platform plan: --live-from: {e}", file=sys.stderr)
            return EXIT_RENDER
        kube = None
    else:
        kube = (
            args.kube if getattr(args, "kube", None) is not None
            else Kube(resolve_kubeconfig(getattr(args, "kubeconfig", None)))
        )
    try:
        code, body = plan(
            instance_dir, kube, app=args.app, live_by_app=live_by_app,
        )
    except RenderError as e:
        print(f"orbit platform plan: render failed for {e.app}: {e.message}", file=sys.stderr)
        return EXIT_RENDER
    except Exception as e:  # noqa: BLE001 - one scrubbed line, never a traceback
        # A live read that fails (403, a timeout, an object too deep to compare) is a
        # render-side failure with its own exit code, never exit 1 = "drift found",
        # and never a traceback that quotes an API body.
        from .applier import _reason
        print(f"orbit platform plan: failed: {_reason(e)}", file=sys.stderr)
        return EXIT_RENDER
    from .client import write_json_document
    write_json_document(body, sort_keys=True)
    if args.summary:
        for row in body["apps"]:
            print(
                f"{row['name']} empty={str(row['empty']).lower()} "
                f"rendered={len(row['rendered'])} matched={len(row['matched'])} "
                f"changed={len(row['changed'])} missing={len(row['missing'])} "
                f"extra={len(row['extra'])} "
                f"hooks={len(row.get('hooks') or [])} "
                f"owned={len(row.get('owned') or [])} "
                f"generated={len(row.get('generated') or [])}",
                file=sys.stderr,
            )
    return code


def _add_plan_arguments(plan_p: argparse.ArgumentParser) -> None:
    plan_p.add_argument("instance_dir", help="instance checkout containing clusters/platform/apps")
    plan_p.add_argument("--app", metavar="NAME", help="only this Application metadata.name")
    plan_p.add_argument(
        "--kubeconfig",
        help="kubeconfig path; ORBITD_KUBECONFIG, then KUBECONFIG, then in-cluster",
    )
    plan_p.add_argument("--summary", action="store_true", help="one-line-per-app summary on stderr")
    plan_p.add_argument(
        "--render-only", metavar="OUT.json",
        help="render Applications to a JSON list; no cluster access",
    )
    plan_p.add_argument(
        "--live-from", metavar="PLAN.json", dest="live_from",
        help="replay live objects from a saved plan JSON; no cluster access",
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="orbit platform", description="desk commands for the platform layer")
    sub = p.add_subparsers(dest="platform_command", required=True)
    plan_p = sub.add_parser("plan", help="render platform Applications and diff against live")
    _add_plan_arguments(plan_p)
    apply_p = sub.add_parser("apply", help="apply platform Applications through the applier library")
    from .platform_apply import add_apply_arguments
    add_apply_arguments(apply_p)
    release_p = sub.add_parser(
        "release-owners", help="drop retired field managers from live platform objects",
    )
    from .platform_apply import add_release_owners_arguments
    add_release_owners_arguments(release_p)
    retire_p = sub.add_parser(
        "retire",
        help="delete what a platform app applied once the instance no longer renders it",
    )
    from .platform_apply import add_retire_arguments
    add_retire_arguments(retire_p)
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return 0 if e.code is None else int(e.code)
    if args.platform_command == "plan":
        return cmd_plan(args)
    if args.platform_command == "apply":
        from .platform_apply import cmd_apply
        return cmd_apply(args)
    if args.platform_command == "release-owners":
        from .platform_apply import cmd_release_owners
        return cmd_release_owners(args)
    if args.platform_command == "retire":
        from .platform_apply import cmd_retire
        return cmd_retire(args)
    print("orbit platform: unknown command", file=sys.stderr)
    return EXIT_RENDER


def helm_argv(
    release: str,
    source: dict[str, Any],
    *,
    root: Path,
    values_path: Path,
    namespace: str,
) -> list[str]:
    """`helm template` argv for one chart source. Local charts skip `--repo`."""
    chart = str(source.get("chart") or "").strip()
    repo = str(source.get("repoURL") or "").strip()
    version = str(source.get("targetRevision") or "").strip()
    # Every field is checked for shape before it is an argv word: helm reads a
    # leading dash as a flag, and a repository is a URL with a host, nothing else.
    if not dns_label(release):
        raise ValueError("helm release name is not a DNS label")
    if namespace and not dns_label(namespace):
        raise ValueError("destination namespace is not a DNS label")
    if not chart_name(chart):
        raise ValueError("Application spec.source.chart is not a chart name")
    if version and not revision(version):
        raise ValueError("Application spec.source.targetRevision is not a revision shape")
    args = [HELM, "template", release]
    local = _local_chart(root, repo, chart)
    if local is not None:
        args.append(str(local))
    elif repo.startswith(("oci://", "http://", "https://")):
        if not remote_repo(repo):
            raise ValueError("Application spec.source.repoURL is not a repository URL")
        if repo.startswith("oci://"):
            args.append(f"{repo.rstrip('/')}/{chart}")
        else:
            args.append(chart)
            args.extend(["--repo", repo])
    elif repo:
        oci = f"oci://{repo.rstrip('/')}"
        if not remote_repo(oci):
            raise ValueError("Application spec.source.repoURL is not a repository URL")
        args.append(f"{oci}/{chart}")
    else:
        raise ValueError("Application spec.source.chart is not a local chart and has no repoURL")
    if version:
        args.extend(["--version", version])
    if namespace:
        args.extend(["--namespace", namespace])
    helm = source.get("helm") or {}
    # spec.source.helm.skipCrds: true means ArgoCD did not apply chart CRDs.
    if not helm.get("skipCrds"):
        args.append("--include-crds")
    args.extend(["--no-hooks", "-f", str(values_path)])
    return args


def _render_chart(
    root: Path,
    application: dict[str, Any],
    app_name: str,
    source: dict[str, Any],
    dest_ns: str,
) -> list[dict[str, Any]]:
    helm = source.get("helm") or {}
    release = str(helm.get("releaseName") or "").strip() or app_name
    values = _helm_values(helm)
    with tempfile.TemporaryDirectory(prefix="orbitd-platform-helm-") as tmp:
        tmp_path = Path(tmp)
        values_path = tmp_path / "values.yaml"
        values_path.write_text(values)
        env = os.environ.copy()
        env.setdefault("HELM_CACHE_HOME", str(tmp_path / "cache"))
        env.setdefault("HELM_CONFIG_HOME", str(tmp_path / "config"))
        env.setdefault("HELM_DATA_HOME", str(tmp_path / "data"))
        argv = helm_argv(
            release, source, root=root, values_path=values_path, namespace=dest_ns,
        )
        local = _local_chart(root, str(source.get("repoURL") or ""), str(source.get("chart") or ""))
        if local is not None and not tree_under_root(root, local):
            # helm follows a symlink inside a chart; a chart on disk is one whose every
            # file is inside the checkout
            raise RenderError(app_name, "a file under the local chart resolves outside the instance checkout")
        for extra in _value_file_paths(root, local, helm, app_name):
            argv.extend(["-f", str(extra)])
        try:
            stdout = run_helm(argv, cwd=root, env=env)
        except HelmFailure as e:
            raise RenderError(app_name, str(e)) from e
    docs = load_yaml_documents(stdout)
    documents_bounded(len(docs))
    return docs


def _value_file_paths(
    root: Path, chart: Path | None, helm: dict[str, Any], app_name: str = "",
) -> list[Path]:
    """Resolve spec.source.helm.valueFiles relative to the chart, then the instance root."""
    files = helm.get("valueFiles") or []
    if not isinstance(files, list):
        files = [files]
    out: list[Path] = []
    for item in files:
        rel = str(item or "").strip()
        if not rel:
            continue
        candidates: list[Path] = []
        if chart is not None:
            candidates.append(chart / rel)
        candidates.append(root / rel)
        found = next((p for p in candidates if p.is_file()), None)
        if found is None:
            raise RenderError(
                app_name, "a helm valueFile was not found relative to the chart",
            )
        if not under_root(root, found):
            raise RenderError(app_name, "a helm valueFile resolves outside the instance checkout")
        out.append(found.resolve())
    return out


def _helm_values(helm: dict[str, Any]) -> str:
    values_object = helm.get("valuesObject")
    if isinstance(values_object, dict):
        return yaml.safe_dump(values_object, sort_keys=True)
    values = helm.get("values")
    if isinstance(values, str):
        return values
    if isinstance(values, dict):
        return yaml.safe_dump(values, sort_keys=True)
    return "{}\n"


def _local_chart(root: Path, repo: str, chart: str) -> Path | None:
    if not chart:
        return None
    if repo.startswith(("http://", "https://", "oci://")):
        return None
    if not chart_name(chart):
        return None
    candidates: list[Path] = []
    repo_path: Path | None = None
    if repo:
        text = repo[7:] if repo.startswith("file://") else repo
        repo_path = Path(text)
        if not repo_path.is_absolute():
            repo_path = root / repo_path
        candidates.append(repo_path / chart)
        candidates.append(repo_path)
    candidates.append(root / chart)
    candidates.append(root / "charts" / chart)
    for path in candidates:
        # A chart on disk is one inside the checkout: a repoURL naming another
        # directory (or a symlink out of the tree) is not a local chart.
        if (path / "Chart.yaml").is_file() and under_root(root, path):
            return path.resolve()
    return None


def _render_directory(
    root: Path,
    app_name: str,
    source: dict[str, Any],
) -> list[dict[str, Any]]:
    rel = str(source.get("path") or "").strip().lstrip("/")
    if not rel:
        raise RenderError(app_name, "Application spec.source.path is empty")
    base = root / rel
    if not under_root(root, base):
        raise RenderError(app_name, "Application spec.source.path resolves outside the instance checkout")
    if not base.is_dir():
        raise RenderError(app_name, "Application spec.source.path is not a directory")
    directory = source.get("directory") or {}
    recurse = bool(directory.get("recurse"))
    exclude = directory.get("exclude") or ""
    objects: list[dict[str, Any]] = []
    for path in _manifest_files(base, root=root, recurse=recurse, exclude=exclude):
        for doc in _load_yaml_docs(path):
            if doc.get("kind") == KIND_APPLICATION:
                continue
            objects.append(doc)
    return objects


def _manifest_files(base: Path, *, root: Path | None = None, recurse: bool, exclude: Any) -> list[Path]:
    if recurse:
        paths = sorted(
            p for p in base.rglob("*")
            if p.is_file() and p.suffix in {".yaml", ".yml"}
        )
    else:
        paths = sorted(
            p for p in base.iterdir()
            if p.is_file() and p.suffix in {".yaml", ".yml"}
        )
    wanted = [p for p in paths if not _excluded(p.relative_to(base).as_posix(), exclude)]
    # is_file() follows symlinks: a link committed under the source directory must
    # not read a file outside the checkout into the render (a link to another file
    # of the checkout is a shared manifest, and renders).
    if any(not under_root(root if root is not None else base, p) for p in wanted):
        raise ValueError("a manifest under spec.source.path resolves outside the instance checkout")
    return wanted


def _excluded(rel: str, exclude: Any) -> bool:
    patterns = _exclude_patterns(exclude)
    if not patterns:
        return False
    rel = rel.replace("\\", "/").lstrip("./")
    return any(_glob_match(rel, pat) for pat in patterns)


def _exclude_patterns(exclude: Any) -> list[str]:
    if exclude is None or exclude is False:
        return []
    if isinstance(exclude, list):
        parts = [str(p) for p in exclude]
    else:
        parts = [str(exclude)]
    out: list[str] = []
    for part in parts:
        text = part.strip().strip("'\"")
        if text.startswith("{") and text.endswith("}"):
            out.extend(p.strip() for p in text[1:-1].split(",") if p.strip())
        else:
            for piece in text.split(","):
                piece = piece.strip()
                if piece:
                    out.append(piece)
    return out


def _glob_match(rel: str, pattern: str) -> bool:
    """ArgoCD doublestar against the path relative to spec.source.path."""
    if pattern.endswith("/**"):
        prefix = pattern[:-3]
        return rel == prefix or rel.startswith(prefix + "/")
    if fnmatch.fnmatch(rel, pattern):
        return True
    return fnmatch.fnmatch(Path(rel).name, pattern)


def _create_namespace(spec: dict[str, Any]) -> bool:
    options = ((spec.get("syncPolicy") or {}).get("syncOptions") or [])
    return CREATE_NAMESPACE in options


def _namespace_object(name: str) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": KIND_NAMESPACE,
        "metadata": {"name": name},
    }


def _with_namespace(obj: dict[str, Any], dest_ns: str) -> dict[str, Any]:
    return _apply_namespace(obj, dest_ns)


def _obj_key(obj: dict[str, Any]) -> tuple[str, str, str]:
    meta = obj.get("metadata") or {}
    return (
        str(obj.get("kind") or ""),
        str(meta.get("namespace") or ""),
        str(meta.get("name") or ""),
    )


def _load_yaml_docs(path: Path) -> list[dict[str, Any]]:
    try:
        text = path.read_text()
    except OSError as e:
        raise RenderReadError(path.name, str(e)) from e
    try:
        return load_yaml_documents(text)
    except yaml.YAMLError as e:
        raise RenderReadError(path.name, str(e)) from e


def _app_payload(
    name: str, report: Any, *, live: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    def refs(items: list[Any]) -> list[dict[str, Any]]:
        return [
            {"kind": r.kind, "namespace": r.namespace, "name": r.name}
            for r in items
        ]

    payload = {
        "name": name,
        "empty": bool(report.empty),
        "rendered": refs(report.rendered),
        "matched": refs(report.live_matched),
        "changed": [
            {
                "kind": c.kind,
                "namespace": c.namespace,
                "name": c.name,
                "paths": list(c.paths),
            }
            for c in report.changed
        ],
        "missing": refs(report.missing),
        "extra": refs(report.extra),
        "hooks": refs(getattr(report, "hooks", []) or []),
        "generated": [
            {
                "kind": g.kind,
                "namespace": g.namespace,
                "name": g.name,
                "keys": list(g.keys),
            }
            for g in (getattr(report, "generated", []) or [])
        ],
        "owned": [
            {
                "kind": o.kind,
                "namespace": o.namespace,
                "name": o.name,
                "owner": {
                    "kind": o.owner.kind,
                    "namespace": o.owner.namespace,
                    "name": o.owner.name,
                },
            }
            for o in (getattr(report, "owned", []) or [])
        ],
    }
    if live is not None:
        payload["live"] = live
    return payload


def secrets_fingerprinted(objects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The same objects with every Secret reduced to its identity and fingerprints:
    apiVersion, kind, type, metadata name/namespace/labels, and data with every
    value replaced by the shadow report's `sha256:` digest (after stringData is
    folded into data the way the API server does). Nothing else of a Secret is
    kept: an annotation such as kubectl's last-applied-configuration carries the
    whole body in clear, and a live object's other metadata names nothing the plan
    needs. The plan JSON is printed, saved and replayed: a Secret's body must not
    be in it, and a fingerprint still names drift per key. A value already of
    fingerprint shape stays as it is, so the pass is idempotent."""
    out: list[dict[str, Any]] = []
    for obj in objects:
        if not isinstance(obj, dict) or obj.get("kind") != "Secret":
            out.append(obj)
            continue
        obj = _canonicalise_secret(obj)
        meta = obj.get("metadata") if isinstance(obj.get("metadata"), dict) else {}
        kept_meta: dict[str, Any] = {}
        # ownerReferences carry kinds, names and uids, no value: the replay needs
        # them to tell a controller-owned Secret from an extra one
        for key in ("name", "namespace", "labels", "ownerReferences"):
            if meta.get(key) is not None:
                kept_meta[key] = meta[key]
        reduced: dict[str, Any] = {"apiVersion": obj.get("apiVersion"), "kind": "Secret",
                                   "metadata": kept_meta}
        if obj.get("type") is not None:
            reduced["type"] = obj["type"]
        data = obj.get("data")
        if isinstance(data, dict):
            reduced["data"] = {
                str(k): (v if isinstance(v, str) and _FINGERPRINT_RE.fullmatch(v) else _fingerprint(v))
                for k, v in data.items()
            }
        out.append(reduced)
    return out


def _generated_secret_names(application: dict[str, Any]) -> frozenset[str]:
    """Names listed on the Application as orbit.local/generated-secrets."""
    ann = (application.get("metadata") or {}).get("annotations") or {}
    raw = ann.get(LABEL_GENERATED_SECRETS) or ""
    return frozenset(part.strip() for part in str(raw).split(",") if part.strip())


def load_live_from(path: Path) -> dict[str, list[dict[str, Any]]]:
    """Per-app live objects from a saved plan JSON. Full objects, not refs."""
    data = json.loads(Path(path).read_text())
    if not isinstance(data, dict) or not isinstance(data.get("apps"), list):
        raise ValueError(f"{path} is not a plan JSON with apps")
    out: dict[str, list[dict[str, Any]]] = {}
    for app in data["apps"]:
        if not isinstance(app, dict):
            continue
        name = str(app.get("name") or "")
        live = app.get("live")
        if not name or not isinstance(live, list):
            continue
        objects = secrets_fingerprinted([
            obj for obj in live
            if isinstance(obj, dict) and obj.get("apiVersion") and obj.get("kind")
        ])
        if objects or live == []:
            out[name] = objects
    if not out:
        raise ValueError(f"{path} has no per-app live objects")
    return out


def add_parser(sub: Any) -> argparse.ArgumentParser:
    """Register `platform` on the orbit CLI subparsers."""
    plat = sub.add_parser("platform", help="desk commands for the platform layer")
    plat_sub = plat.add_subparsers(dest="platform_command", required=True)
    plan_p = plat_sub.add_parser(
        "plan", help="render platform Applications and diff against live",
    )
    _add_plan_arguments(plan_p)
    apply_p = plat_sub.add_parser(
        "apply", help="apply platform Applications through the applier library",
    )
    from .platform_apply import add_apply_arguments
    add_apply_arguments(apply_p)
    release_p = plat_sub.add_parser(
        "release-owners", help="drop retired field managers from live platform objects",
    )
    from .platform_apply import add_release_owners_arguments
    add_release_owners_arguments(release_p)
    retire_p = plat_sub.add_parser(
        "retire",
        help="delete what a platform app applied once the instance no longer renders it",
    )
    from .platform_apply import add_retire_arguments
    add_retire_arguments(retire_p)
    return plat


__all__ = [
    "EXIT_EMPTY", "EXIT_DRIFT", "EXIT_RENDER", "RenderError",
    "load_applications", "load_yaml_documents", "render_application",
    "render_only", "resolve_kubeconfig", "plan", "cmd_plan",
    "helm_argv", "load_live_from", "main", "add_parser",
]
