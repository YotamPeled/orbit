"""Make the cluster's optional features match the instance checkout.

`features:` in instance/config.yaml says which features are on; the rendered tree under
clusters/ already follows it (`make init`). This is the step that makes the CLUSTER
follow: a feature turned on has its Applications applied, a feature turned off has them
retired, and the Applications that carry a feature's blocks (its `consumers`: a Grafana
datasource, an alert rule, a network policy) are re-applied with prune either way.

One engine, two launchers: `orbit features converge` on the desk (bootstrap, recovery),
and the Job orbitd starts after `orbit_features_set` and whenever its reconcile loop
finds the cluster behind the checkout. It is safe to run again at any point: every step
is an idempotent apply or a retire of what is still there, and the record of what was
converged (ConfigMap orbit/orbit-features) is written only for features whose every
step succeeded.

Order, in one run:
  1. apply the Applications of every feature being turned on (their CRDs and namespaces
     exist before anything that refers to them);
  2. re-apply, with prune, every consumer of a feature that changed either way;
  3. retire the Applications of every feature being turned off (after the consumers let
     go of what they held: a CRD is not removed while its objects exist);
  4. wait for the workloads of the features turned on to be ready.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from . import features as features_module
from . import orbit_features as rules

RECORD_NS = "orbit"
RECORD_NAME = "orbit-features"
READY_TIMEOUT_S = 900.0
EXIT_OK, EXIT_FAILED, EXIT_REFUSED = 0, 1, 2


class Ops(Protocol):
    """What the engine needs done; the real one drives the platform applier."""

    def applied(self, app: str) -> bool: ...
    def apply(self, app: str, *, prune: bool) -> str | None: ...
    def retire(self, app: str) -> str | None: ...
    def wait_ready(self, app: str, timeout_s: float) -> str | None: ...
    def read_record(self) -> dict[str, Any] | None: ...
    def write_record(self, record: dict[str, Any]) -> None: ...


@dataclass
class Step:
    action: str                      # apply | apply-prune | retire | wait-ready
    app: str
    features: list[str]
    ok: bool | None = None           # None: not run
    error: str | None = None

    def dump(self) -> dict[str, Any]:
        out: dict[str, Any] = {"action": self.action, "app": self.app, "for": self.features}
        if self.ok is not None:
            out["ok"] = self.ok
        if self.error:
            out["error"] = self.error
        return out


@dataclass
class Plan:
    desired: dict[str, bool]
    was: dict[str, bool | None]
    enable: list[str] = field(default_factory=list)
    disable: list[str] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)

    @property
    def changed(self) -> list[str]:
        return self.enable + self.disable


def make_plan(registry: dict[str, Any], on: dict[str, bool], ops: Ops,
              assumed: dict[str, bool] | None = None) -> Plan:
    """What a converge would do, from the registry, the desired states and the cluster.

    `assumed` is what a feature is taken to have been when nothing says: no record names
    it and it owns no Application to look for (Grafana lives inside the metrics stack's
    chart). An instance from before features rendered every feature whose needs were
    met, so that reading of its config is the assumption."""
    record = ops.read_record() or {}
    recorded = record.get("features") if isinstance(record.get("features"), dict) else {}
    optional = [name for name, spec in registry.items() if not spec["always"]]
    plan = Plan(desired={name: bool(on.get(name)) for name in optional}, was={})
    applied: dict[str, bool] = {}
    for name in optional:
        owned = registry[name]["apps"]
        for app in owned:
            applied[app] = ops.applied(app)
        have = [applied[app] for app in owned]
        was = recorded.get(name)
        if not isinstance(was, bool):
            # never converged: an instance from before features. What is applied is what
            # it has; a feature that owns no Application cannot be read off the cluster
            # and is taken to be what such an instance rendered.
            was = all(have) if owned else (assumed or {}).get(name)
        plan.was[name] = was
        if plan.desired[name] and (was is not True or not all(have)):
            plan.enable.append(name)
        elif not plan.desired[name] and (was is not False or any(have)):
            plan.disable.append(name)
    for name in plan.enable:
        for app in registry[name]["apps"]:
            plan.steps.append(Step("apply", app, [name]))
    consumers: dict[str, list[str]] = {}
    for name in plan.changed:
        for app in registry[name].get("consumers") or []:
            consumers.setdefault(app, []).append(name)
    for app, names in consumers.items():
        plan.steps.append(Step("apply-prune", app, names))
    for name in plan.disable:
        for app in reversed(registry[name]["apps"]):
            if applied.get(app):
                plan.steps.append(Step("retire", app, [name]))
    for name in plan.enable:
        for app in registry[name]["apps"]:
            plan.steps.append(Step("wait-ready", app, [name]))
    return plan


def assumed_states(instance_dir: Path, registry: dict[str, Any]) -> dict[str, bool]:
    """The features this instance ran before it ever named them: its config read without
    its `features:` block. Empty when the config cannot be read."""
    import yaml
    try:
        config = yaml.safe_load((Path(instance_dir) / features_module.CONFIG_REL).read_text(encoding="utf-8")) or {}
        legacy = {key: value for key, value in config.items() if key != "features"}
        return rules.states(legacy, registry)
    except (OSError, yaml.YAMLError, rules.FeatureError, AttributeError):
        return {}


def run(instance_dir: Path, ops: Ops, *, dry_run: bool = False,
        ready_timeout_s: float = READY_TIMEOUT_S, commit: str | None = None) -> tuple[int, dict[str, Any]]:
    """Converge; returns (exit code, report). A refused checkout changes nothing."""
    try:
        snap = features_module.read(Path(instance_dir))
    except rules.FeatureError as e:
        return EXIT_REFUSED, {"ok": False, "refused": str(e), "steps": []}
    plan = make_plan(snap.registry, snap.on, ops, assumed=assumed_states(Path(instance_dir), snap.registry))
    report: dict[str, Any] = {
        "ok": True, "dry_run": dry_run, "commit": commit,
        "features": {name: {"desired": plan.desired[name], "was": plan.was[name],
                            "action": ("enable" if name in plan.enable else
                                       "disable" if name in plan.disable else "none")}
                     for name in plan.desired},
        "steps": [],
    }
    failed: set[str] = set()
    if not dry_run:
        for step in plan.steps:
            if failed & set(step.features) and step.action in ("retire", "wait-ready"):
                # a feature whose earlier step failed is not taken further apart or
                # waited on; the steps it shares with other features still run
                continue
            if step.action == "apply":
                step.error = ops.apply(step.app, prune=False)
            elif step.action == "apply-prune":
                step.error = ops.apply(step.app, prune=True)
            elif step.action == "retire":
                step.error = ops.retire(step.app)
            else:
                step.error = ops.wait_ready(step.app, ready_timeout_s)
            step.ok = step.error is None
            if not step.ok:
                failed |= set(step.features)
        previous = ops.read_record() or {}
        recorded = dict(previous.get("features") or {}) if isinstance(previous.get("features"), dict) else {}
        for name in plan.desired:
            if name not in failed:
                recorded[name] = plan.desired[name]
        ops.write_record({"features": recorded, "commit": commit or "",
                          "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    report["steps"] = [step.dump() for step in plan.steps]
    if failed:
        report["ok"] = False
        report["failed"] = sorted(failed)
        for name in failed:
            report["features"][name]["action"] += " (failed)"
    return (EXIT_FAILED if failed else EXIT_OK), report


# ---------------------------------------------------------------- the real operations

class ClusterOps:
    """The engine's operations over the platform applier and the Kubernetes API."""

    def __init__(self, instance_dir: Path, kube: Any, *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep, stderr: Any = None) -> None:
        self.instance_dir = Path(instance_dir)
        self.kube = kube
        self.clock = clock
        self.sleep = sleep
        self.stderr = sys.stderr if stderr is None else stderr

    def _resource(self, api_version: str, kind: str) -> Any:
        return self.kube.dynamic.resources.get(api_version=api_version, kind=kind)

    def _config_map(self, name: str) -> dict[str, Any] | None:
        try:
            return _doc(self._resource("v1", "ConfigMap").get(name=name, namespace=RECORD_NS))
        except Exception as e:
            if getattr(e, "status", None) == 404:
                return None
            raise

    def applied(self, app: str) -> bool:
        """True while the app has something on the cluster: its stored inventory lists an
        object other than a Namespace. A retired app keeps its inventory ConfigMap, and
        keeps in it the namespace a retire never deletes (another app's sealed secret
        may live there); that is an app that is gone, not one that is applied."""
        from .platform_apply import read_app_inventory
        refs, _ = read_app_inventory(self.kube, app)
        return any(ref.kind != "Namespace" for ref in refs)

    def apply(self, app: str, *, prune: bool) -> str | None:
        from . import platform_apply
        from .platform import RenderError
        try:
            code, body = platform_apply.run(
                self.instance_dir, self.kube, app=app, prune=prune,
                stdin=io.StringIO(f"{platform_apply.CONFIRM_PREFIX}{app}\n"), stderr=self.stderr)
        except RenderError as e:
            return f"render failed for {e.app}: {e.message}"
        if code == platform_apply.EXIT_OK:
            return None
        rows = [row for row in (body or {}).get("apps") or [] if not row.get("ok")]
        return "; ".join(str(row.get("error") or "apply failed") for row in rows) or f"apply exited {code}"

    def retire(self, app: str) -> str | None:
        from . import platform_apply
        err = io.StringIO()
        code, body = platform_apply.run_retire(
            self.instance_dir, self.kube, app=app,
            stdin=io.StringIO(f"{platform_apply.RETIRE_CONFIRM_PREFIX}{app}\n"), stderr=err)
        print(err.getvalue(), file=self.stderr, end="")
        if code == platform_apply.EXIT_OK:
            return None
        if isinstance(body, dict) and body.get("error"):
            return str(body["error"])[:400]
        lines = [line for line in err.getvalue().splitlines() if line.strip()]
        return (lines[-1] if lines else f"retire exited {code}")[:400]

    def wait_ready(self, app: str, timeout_s: float) -> str | None:
        from .platform_apply import read_app_inventory
        refs, _ = read_app_inventory(self.kube, app)
        workloads = [r for r in refs if r.kind in ("Deployment", "StatefulSet", "DaemonSet")]
        deadline = self.clock() + timeout_s
        while True:
            waiting = [f"{r.kind}/{r.namespace}/{r.name}" for r in workloads if not self._ready(r)]
            if not waiting:
                return None
            if self.clock() >= deadline:
                return f"not ready after {int(timeout_s)}s: {', '.join(waiting)}"
            self.sleep(5.0)

    def _ready(self, ref: Any) -> bool:
        try:
            doc = _doc(self._resource("apps/v1", ref.kind).get(name=ref.name, namespace=ref.namespace))
        except Exception as e:
            if getattr(e, "status", None) == 404:
                return False
            raise
        spec, status = doc.get("spec") or {}, doc.get("status") or {}
        if (status.get("observedGeneration") or 0) < ((doc.get("metadata") or {}).get("generation") or 0):
            return False
        if ref.kind == "DaemonSet":
            want = status.get("desiredNumberScheduled") or 0
            return (status.get("numberReady") or 0) >= want and (status.get("updatedNumberScheduled") or 0) >= want
        want = spec.get("replicas")
        want = 1 if want is None else want
        return (status.get("readyReplicas") or 0) >= want and (status.get("updatedReplicas") or 0) >= want

    def wanted(self, ref: Any) -> int | None:
        """How many pods a workload asks for right now (None: it cannot be told). A
        Deployment or StatefulSet scaled to 0 is "ready" by the arithmetic above and
        runs nothing: the status says so instead (features_status)."""
        try:
            doc = _doc(self._resource("apps/v1", ref.kind).get(name=ref.name, namespace=ref.namespace))
        except Exception:
            return None
        if ref.kind == "DaemonSet":
            return (doc.get("status") or {}).get("desiredNumberScheduled")
        want = (doc.get("spec") or {}).get("replicas")
        return 1 if want is None else want

    def read_record(self) -> dict[str, Any] | None:
        cm = self._config_map(RECORD_NAME)
        if cm is None:
            return None
        try:
            record = json.loads((cm.get("data") or {}).get("record") or "{}")
        except ValueError:
            return None
        return record if isinstance(record, dict) else None

    def write_record(self, record: dict[str, Any]) -> None:
        from .applier import FIELD_MANAGER
        body = {"apiVersion": "v1", "kind": "ConfigMap",
                "metadata": {"name": RECORD_NAME, "namespace": RECORD_NS,
                             "labels": {"app.kubernetes.io/part-of": "orbit"}},
                "data": {"record": json.dumps(record, sort_keys=True)}}
        self._resource("v1", "ConfigMap").server_side_apply(
            body=body, name=RECORD_NAME, namespace=RECORD_NS, field_manager=FIELD_MANAGER,
            force_conflicts=True)


def _doc(obj: Any) -> dict[str, Any]:
    if isinstance(obj, dict):
        return obj
    to_dict = getattr(obj, "to_dict", None)
    data = to_dict() if callable(to_dict) else None
    return data if isinstance(data, dict) else {}


def checkout_commit(instance_dir: Path) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(instance_dir), "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None
