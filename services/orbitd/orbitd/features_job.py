"""The converge Job: orbitd's way of making the cluster's features match a commit.

orbitd's own ServiceAccount cannot apply platform Applications (it holds the tenant
kinds and nothing more), and rendering a platform chart does not belong inside a
long-lived control surface with a small memory limit. So the work runs in a Kubernetes
Job in orbitd's namespace: a one-time git-sync of the instance repo at one commit, then
`orbit features converge` from orbitd's own image, under the `orbit-platform`
ServiceAccount. orbitd may create and read such Jobs and nothing else of the platform's.

One converge at a time: a Job is not started while another is active. The Job outlives
an orbitd restart, and what it could not finish the next one picks up (the engine is
idempotent and records only what it converged).
"""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from .features_converge import RECORD_NS

LABEL = "orbit.local/job"
LABEL_VALUE = "features"
COMMIT_LABEL = "orbit.local/commit"
SERVICE_ACCOUNT = "orbit-platform"
ACTIVE_DEADLINE_S = 1800
TTL_AFTER_FINISHED_S = 86400
POLL_S = 5.0
_SHA = re.compile(r"^[0-9a-f]{40}$")
CHECKOUT = "/var/lib/instance/current"


@dataclass(frozen=True)
class JobSettings:
    """What the Job is built from; every value is orbitd's own deployment's."""

    image: str               # orbitd's image: the Job runs the same build
    git_sync_image: str
    repo: str                # https URL of the instance repo
    pull_secret: str = "ghcr-pull"
    github_app_secret: str = "orbitd-github-app"

    @classmethod
    def from_env(cls, env: Any = None) -> JobSettings | None:
        env = os.environ if env is None else env
        image = (env.get("ORBITD_IMAGE") or "").strip()
        sync = (env.get("ORBITD_GIT_SYNC_IMAGE") or "").strip()
        repo = (env.get("ORBITD_INSTANCE_REPO") or "").strip()
        if not (image and sync and repo):
            return None
        return cls(image=image, git_sync_image=sync, repo=repo)


class ConvergeUnavailable(Exception):
    """This orbitd cannot start a converge Job; str() says why."""


HARDENED = {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001,
            "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]}}


def manifest(settings: JobSettings, commit: str, *, name: str | None = None) -> dict[str, Any]:
    """The Job for one commit. The commit is the only caller-chosen value in it, and it
    must be a full sha: it becomes the git ref the checkout is pinned to."""
    if not _SHA.match(commit or ""):
        raise ConvergeUnavailable("the commit to converge on is not a full commit id")
    name = name or f"orbit-features-{commit[:10]}-{uuid.uuid4().hex[:5]}"
    labels = {"app.kubernetes.io/part-of": "orbit", LABEL: LABEL_VALUE, COMMIT_LABEL: commit}
    return {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": name, "namespace": RECORD_NS, "labels": labels},
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": ACTIVE_DEADLINE_S,
            "ttlSecondsAfterFinished": TTL_AFTER_FINISHED_S,
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "serviceAccountName": SERVICE_ACCOUNT,
                    "automountServiceAccountToken": True,
                    "restartPolicy": "Never",
                    "securityContext": {"fsGroup": 10001},
                    "imagePullSecrets": [{"name": settings.pull_secret}],
                    "initContainers": [{
                        "name": "checkout",
                        "image": settings.git_sync_image,
                        "args": [f"--repo={settings.repo}", f"--ref={commit}", "--one-time", "--depth=1",
                                 "--root=/var/lib/instance", "--link=current",
                                 "--github-app-private-key-file=/etc/github-app/private-key.pem"],
                        "env": [
                            {"name": "GITSYNC_GITHUB_APP_APPLICATION_ID",
                             "valueFrom": {"secretKeyRef": {"name": settings.github_app_secret, "key": "app-id"}}},
                            {"name": "GITSYNC_GITHUB_APP_INSTALLATION_ID",
                             "valueFrom": {"secretKeyRef": {"name": settings.github_app_secret, "key": "installation-id"}}},
                        ],
                        "volumeMounts": [{"name": "instance", "mountPath": "/var/lib/instance"},
                                         {"name": "github-app", "mountPath": "/etc/github-app", "readOnly": True},
                                         {"name": "tmp", "mountPath": "/tmp"}],
                        "resources": {"requests": {"cpu": "10m", "memory": "32Mi"}, "limits": {"memory": "128Mi"}},
                        "securityContext": dict(HARDENED),
                    }],
                    "containers": [{
                        "name": "converge",
                        "image": settings.image,
                        "command": ["orbit", "features", "converge", CHECKOUT, "--gated"],
                        "env": [{"name": "HOME", "value": "/tmp"},
                                {"name": "TMPDIR", "value": "/tmp"},
                                {"name": "HELM_CACHE_HOME", "value": "/tmp/helm-cache"},
                                {"name": "HELM_CONFIG_HOME", "value": "/tmp/helm-config"},
                                {"name": "HELM_DATA_HOME", "value": "/tmp/helm-data"}],
                        "volumeMounts": [{"name": "instance", "mountPath": "/var/lib/instance", "readOnly": True},
                                         {"name": "tmp", "mountPath": "/tmp"}],
                        # a platform chart is rendered here (helm template of the metrics
                        # stack is the largest); measured on the test box, docs/V1.md M2c
                        "resources": {"requests": {"cpu": "50m", "memory": "128Mi"}, "limits": {"memory": "768Mi"}},
                        "securityContext": dict(HARDENED),
                    }],
                    "volumes": [{"name": "instance", "emptyDir": {}},
                                {"name": "tmp", "emptyDir": {"sizeLimit": "512Mi"}},
                                {"name": "github-app", "secret": {"secretName": settings.github_app_secret}}],
                },
            },
        },
    }


def _jobs(kube: Any) -> Any:
    return kube.dynamic.resources.get(api_version="batch/v1", kind="Job")


def _doc(obj: Any) -> dict[str, Any]:
    if isinstance(obj, dict):
        return obj
    to_dict = getattr(obj, "to_dict", None)
    return to_dict() if callable(to_dict) else {}


def _finished(job: dict[str, Any]) -> str | None:
    """"succeeded", "failed", or None while it runs."""
    status = job.get("status") or {}
    for cond in status.get("conditions") or []:
        if str(cond.get("status")) == "True" and cond.get("type") in ("Complete", "Failed"):
            return "succeeded" if cond.get("type") == "Complete" else "failed"
    if (status.get("succeeded") or 0) > 0:
        return "succeeded"
    return None


def list_jobs(kube: Any) -> list[dict[str, Any]]:
    listed = _jobs(kube).get(namespace=RECORD_NS, label_selector=f"{LABEL}={LABEL_VALUE}")
    items = _doc(listed).get("items") or []
    return [_doc(item) for item in items]


def active(kube: Any) -> dict[str, Any] | None:
    """The converge Job that is running now, if any."""
    for job in list_jobs(kube):
        if _finished(job) is None:
            return job
    return None


def start(kube: Any, settings: JobSettings | None, commit: str) -> str:
    """Create the Job; returns its name. Raises ConvergeUnavailable."""
    if settings is None:
        raise ConvergeUnavailable("this orbitd is not told its own image and instance repo "
                                  "(ORBITD_IMAGE, ORBITD_GIT_SYNC_IMAGE, ORBITD_INSTANCE_REPO), "
                                  "so it cannot start a converge Job")
    body = manifest(settings, commit)
    _jobs(kube).create(body=body, namespace=RECORD_NS)
    return body["metadata"]["name"]


def delete(kube: Any, name: str) -> None:
    """Delete a finished Job and its pod; a Job that is already gone is fine."""
    if not name:
        return
    try:
        _jobs(kube).delete(name=name, namespace=RECORD_NS, body={"propagationPolicy": "Background"})
    except Exception as e:
        if getattr(e, "status", None) != 404:
            raise


def wait(kube: Any, name: str, *, clock: Callable[[], float], sleep: Callable[[float], None],
         timeout_s: float) -> tuple[str, dict[str, Any] | None]:
    """(outcome, report): outcome is succeeded | failed | timeout | gone; the report is the
    JSON document the engine printed, when the pod's log still has it."""
    deadline = clock() + timeout_s
    while True:
        try:
            job = _doc(_jobs(kube).get(name=name, namespace=RECORD_NS))
        except Exception as e:
            if getattr(e, "status", None) == 404:
                return "gone", None
            raise
        outcome = _finished(job)
        if outcome is not None:
            return outcome, report_of(kube, name)
        if clock() >= deadline:
            return "timeout", None
        sleep(POLL_S)


def report_of(kube: Any, name: str) -> dict[str, Any] | None:
    """The engine's report from the Job's pod log: the last JSON object it printed."""
    try:
        pods = kube.core.list_namespaced_pod(RECORD_NS, label_selector=f"job-name={name}")
        items = list(getattr(pods, "items", None) or [])
        for pod in items:
            pod_name = (_doc(pod).get("metadata") or {}).get("name")
            if not pod_name:
                continue
            from .node_logs import read_text
            report = last_json_object(read_text(kube, pod_name, RECORD_NS, container="converge",
                                                tail_lines=4000, limit_bytes=1024 * 1024))
            if report is not None:
                return report
    except Exception:
        return None
    return None


def last_json_object(text: str) -> dict[str, Any] | None:
    """The last top-level JSON object in a log: the engine prints one, after whatever
    the applier wrote on the way."""
    decoder = json.JSONDecoder()
    found = None
    i = text.find("{")
    while i != -1:
        try:
            value, end = decoder.raw_decode(text, i)
        except ValueError:
            i = text.find("{", i + 1)
            continue
        if isinstance(value, dict) and "steps" in value:
            found = value
        i = text.find("{", end)
    return found
