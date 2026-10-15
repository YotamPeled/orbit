"""The desk Job: how orbitd runs one of core's own scripts on the cluster.

Backing up a database, restoring one and removing a project are desk scripts (kubectl,
psql through `exec`, the platform applier). An agent used to run them from a laptop,
around orbitd. They run here instead, where the role gate, the confirmation and the
audit line are: a Kubernetes Job in orbitd's namespace, from the desk image of this
same core release, under the `orbit-platform` ServiceAccount, with the instance repo
checked out at the commit orbitd is applying.

What a caller chooses is small and checked: which script (one of SCRIPTS, never a path)
and its arguments (plain words, passed as an argv list; no shell reads them). The
script is the image's own copy under /opt/orbit-core, never the instance checkout's.

Backups are written to a volume that outlives the Job and any project namespace: the
`orbit-backups` claim in orbitd's namespace, made on first use.
"""

from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from . import features_job
from .features_converge import RECORD_NS
from .features_job import CHECKOUT, HARDENED, SERVICE_ACCOUNT, _doc, _finished, _jobs

LABEL = features_job.LABEL
LABEL_VALUE = "desk"
KIND_LABEL = "orbit.local/desk-job"
CORE_SCRIPTS = "/opt/orbit-core/scripts"
SCRIPTS = frozenset({"db-backup.sh", "db-restore.sh", "remove-project.sh"})
CONTAINER = "desk"
BACKUPS_CLAIM = "orbit-backups"
BACKUPS_MOUNT = "/var/lib/orbit-backups"
BACKUPS_SIZE_DEFAULT = "5Gi"
S3_SECRET = "orbit-s3"
TTL_AFTER_FINISHED_S = 86400
POLL_S = 5.0
LOG_BYTES = 256 * 1024
_SHA = re.compile(r"^[0-9a-f]{40}$")
_WORD = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$|^--[a-z][a-z-]{0,20}$")
_KIND = re.compile(r"^[a-z][a-z0-9-]{0,20}$")
_SIZE = re.compile(r"^[1-9][0-9]{0,3}(Mi|Gi)$")


class DeskJobUnavailable(Exception):
    """The Job cannot be built or started; the message is one plain sentence."""


@dataclass(frozen=True)
class DeskSettings:
    """What the Job is built from; every value is orbitd's own deployment's."""

    image: str               # the desk image of this core release
    git_sync_image: str
    repo: str                # https URL of the instance repo
    backups_size: str = BACKUPS_SIZE_DEFAULT
    pull_secret: str = "ghcr-pull"
    github_app_secret: str = "orbitd-github-app"

    @classmethod
    def from_env(cls, env: Any = None) -> DeskSettings | None:
        env = os.environ if env is None else env
        image = (env.get("ORBITD_DESK_IMAGE") or "").strip()
        sync = (env.get("ORBITD_GIT_SYNC_IMAGE") or "").strip()
        repo = (env.get("ORBITD_INSTANCE_REPO") or "").strip()
        if not (image and sync and repo):
            return None
        size = (env.get("ORBITD_BACKUPS_SIZE") or "").strip() or BACKUPS_SIZE_DEFAULT
        return cls(image=image, git_sync_image=sync, repo=repo,
                   backups_size=size if _SIZE.match(size) else BACKUPS_SIZE_DEFAULT)


UNCONFIGURED = ("this orbitd is not told the desk image and its instance repo (ORBITD_DESK_IMAGE, "
                "ORBITD_GIT_SYNC_IMAGE, ORBITD_INSTANCE_REPO), so it cannot run a desk Job")


def manifest(settings: DeskSettings, commit: str, *, kind: str, script: str, args: list[str],
             deadline_s: int, name: str | None = None) -> dict[str, Any]:
    """The Job that runs one core script at one commit of the instance repo."""
    if not _SHA.match(commit or ""):
        raise DeskJobUnavailable("the commit to run at is not a full commit id")
    if script not in SCRIPTS:
        raise DeskJobUnavailable("that is not a script a desk Job runs")
    if not _KIND.match(kind or ""):
        raise DeskJobUnavailable("the job's kind is not a plain word")
    for arg in args:
        if not isinstance(arg, str) or not _WORD.match(arg):
            raise DeskJobUnavailable("an argument of the script is not a plain word")
    name = name or f"orbit-desk-{kind}-{uuid.uuid4().hex[:8]}"
    labels = {"app.kubernetes.io/part-of": "orbit", LABEL: LABEL_VALUE, KIND_LABEL: kind,
              features_job.COMMIT_LABEL: commit}
    key = lambda k: {"secretKeyRef": {"name": S3_SECRET, "key": k, "optional": True}}  # noqa: E731
    return {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": name, "namespace": RECORD_NS, "labels": labels},
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": int(deadline_s),
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
                        "name": CONTAINER,
                        "image": settings.image,
                        # the image's entrypoint prepares a laptop (ssh key, tunnel); a
                        # Job on the cluster needs none of it
                        "command": ["bash", f"{CORE_SCRIPTS}/{script}", *args],
                        "workingDir": CHECKOUT,
                        "env": [{"name": "HOME", "value": "/tmp"},
                                {"name": "TMPDIR", "value": "/tmp"},
                                {"name": "ORBIT_IN_CLUSTER", "value": "1"},
                                {"name": "ORBIT_BACKUP_DIR", "value": BACKUPS_MOUNT},
                                # the bucket keys, when this platform has a bucket: read
                                # by the script from its environment, never by orbitd
                                {"name": "ORBIT_S3_ACCESS_KEY", "valueFrom": key("access-key")},
                                {"name": "ORBIT_S3_SECRET_KEY", "valueFrom": key("secret-key")},
                                {"name": "HELM_CACHE_HOME", "value": "/tmp/helm-cache"},
                                {"name": "HELM_CONFIG_HOME", "value": "/tmp/helm-config"},
                                {"name": "HELM_DATA_HOME", "value": "/tmp/helm-data"}],
                        "volumeMounts": [{"name": "instance", "mountPath": "/var/lib/instance", "readOnly": True},
                                         {"name": "tmp", "mountPath": "/tmp"},
                                         {"name": "backups", "mountPath": BACKUPS_MOUNT}],
                        "resources": {"requests": {"cpu": "50m", "memory": "128Mi"}, "limits": {"memory": "768Mi"}},
                        "securityContext": dict(HARDENED),
                    }],
                    "volumes": [{"name": "instance", "emptyDir": {}},
                                {"name": "tmp", "emptyDir": {"sizeLimit": "2Gi"}},
                                {"name": "backups", "persistentVolumeClaim": {"claimName": BACKUPS_CLAIM}},
                                {"name": "github-app", "secret": {"secretName": settings.github_app_secret}}],
                },
            },
        },
    }


def ensure_backups_claim(kube: Any, settings: DeskSettings) -> bool:
    """Make the backups claim if it is not there; True when this call made it."""
    claims = kube.dynamic.resources.get(api_version="v1", kind="PersistentVolumeClaim")
    try:
        claims.get(name=BACKUPS_CLAIM, namespace=RECORD_NS)
        return False
    except Exception as e:
        if getattr(e, "status", None) != 404:
            raise
    claims.create(namespace=RECORD_NS, body={
        "apiVersion": "v1", "kind": "PersistentVolumeClaim",
        "metadata": {"name": BACKUPS_CLAIM, "namespace": RECORD_NS,
                     "labels": {"app.kubernetes.io/part-of": "orbit"}},
        "spec": {"accessModes": ["ReadWriteOnce"],
                 "resources": {"requests": {"storage": settings.backups_size}}}})
    return True


def active(kube: Any) -> dict[str, Any] | None:
    """A desk Job that is running now, if any."""
    listed = _jobs(kube).get(namespace=RECORD_NS, label_selector=f"{LABEL}={LABEL_VALUE}")
    for item in _doc(listed).get("items") or []:
        job = _doc(item)
        if _finished(job) is None:
            return job
    return None


def start(kube: Any, settings: DeskSettings | None, commit: str, *, kind: str, script: str,
          args: list[str], deadline_s: int) -> str:
    """Create the Job (and the backups claim on first use); returns the Job's name."""
    if settings is None:
        raise DeskJobUnavailable(UNCONFIGURED)
    body = manifest(settings, commit, kind=kind, script=script, args=args, deadline_s=deadline_s)
    ensure_backups_claim(kube, settings)
    _jobs(kube).create(body=body, namespace=RECORD_NS)
    return body["metadata"]["name"]


def wait(kube: Any, name: str, *, clock: Callable[[], float], sleep: Callable[[float], None],
         timeout_s: float) -> tuple[str, str]:
    """(outcome, log): outcome is succeeded | failed | timeout | gone; the log is what
    the script printed, as far as the pod still has it."""
    deadline = clock() + timeout_s
    while True:
        try:
            job = _doc(_jobs(kube).get(name=name, namespace=RECORD_NS))
        except Exception as e:
            if getattr(e, "status", None) == 404:
                return "gone", ""
            raise
        outcome = _finished(job)
        if outcome is not None:
            return outcome, log_of(kube, name)
        if clock() >= deadline:
            return "timeout", log_of(kube, name)
        sleep(POLL_S)


def log_of(kube: Any, name: str) -> str:
    try:
        pods = kube.core.list_namespaced_pod(RECORD_NS, label_selector=f"job-name={name}")
        for pod in list(getattr(pods, "items", None) or []):
            pod_name = (_doc(pod).get("metadata") or {}).get("name")
            if pod_name:
                from .node_logs import read_text
                return read_text(kube, pod_name, RECORD_NS, container=CONTAINER,
                                 tail_lines=2000, limit_bytes=LOG_BYTES)
    except Exception:
        return ""
    return ""
