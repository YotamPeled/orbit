"""Shared clients for observed and desired state.

orbitd holds no cluster state of its own. Observed state is the Kubernetes API plus
Prometheus, Loki, Tempo and Alertmanager; desired state is the instance git repo.
Tools that need a backend that is not configured raise `BackendUnconfigured` (the
actionable failure `backend_unconfigured` naming the variable). HTTP errors raise
`BackendError` (`backend_error`) with the status code and the first 200 characters of
the body — never the full body: observability text is attacker-influenceable.
"""

from __future__ import annotations

import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import yaml

from .config import Config
from .roles import failure

BODY_LIMIT = 200
HTTP_TIMEOUT = 15.0
# The kubernetes client honours `_request_timeout` only as an int or a (connect, read)
# pair; a float is silently no timeout at all. The appliers and the shadow diff pass this
# pair, the job layer passes a pair built the same way (deploy.kube_request_timeout,
# assertions._kube_timeout), and Kube binds it at the request layer for every call that
# names none (the dynamic client's discovery, list_pods, ...), so no request the daemon
# makes is unbounded.
KUBE_TIMEOUT: tuple[float, float] = (HTTP_TIMEOUT, HTTP_TIMEOUT)

CNPG_GROUP = "postgresql.cnpg.io"
CNPG_VERSION = "v1"
CNPG_PLURAL = "clusters"


class BackendUnconfigured(Exception):
    """A tool asked for a backend whose environment variable is unset."""

    def __init__(self, variable: str) -> None:
        self.variable = variable
        super().__init__(variable)

    def as_failure(self):
        return failure(
            "backend_unconfigured",
            f"{self.variable} is not set",
            next_action=f"set {self.variable} on the orbitd process",
            retryable=False,
            variable=self.variable,
        )


class BackendError(Exception):
    """A backend call failed. `body` is already truncated to BODY_LIMIT and scrubbed of
    credential shapes, so it may be relayed to the caller as is."""

    def __init__(self, *, status: int | None, body: str,
                 what_failed: str = "backend request failed",
                 env_var: str | None = None, retryable: bool | None = None) -> None:
        from .deploy import scrub  # deploy imports this module

        self.status = status
        # scrub before the cut: a credential straddling the limit must not leave its prefix
        self.body = scrub(body[:BODY_LIMIT + 4096])[:BODY_LIMIT]
        self.what_failed = what_failed
        self.env_var = env_var
        self.retryable_override = retryable
        super().__init__(what_failed)

    def is_transport(self) -> bool:
        """Retryable transport: connection refused, timeout, or HTTP 5xx."""
        if self.retryable_override is not None:
            return self.retryable_override
        if self.status is not None:
            return self.status >= 500
        return True

    def as_failure(self):
        transport = self.is_transport()
        name = self.env_var or "the configured backend"
        if transport:
            next_action = (
                f"check {name} is reachable from namespace orbit "
                f"and that NetworkPolicy admits it"
            )
        elif self.status == 400:
            next_action = f"check the query syntax sent to {name}"
        else:
            next_action = f"inspect {name}"
        evidence: dict[str, object] = {"status": self.status, "body": self.body}
        if self.env_var:
            evidence["variable"] = self.env_var
        return failure(
            "backend_error",
            self.what_failed,
            next_action=next_action,
            retryable=transport,
            **evidence,
        )


# ------------------------------------------------------------------ HTTP (prom, loki, tempo, alertmanager)

class HttpBackend:
    """Thin httpx GET/POST/DELETE wrapper: JSON in/out, 15 s timeout, truncated errors."""

    def __init__(self, base_url: str | None, env_var: str, *,
                 timeout: float = HTTP_TIMEOUT, transport: httpx.BaseTransport | None = None) -> None:
        self.base_url = base_url
        self.env_var = env_var
        self.timeout = timeout
        self.transport = transport
        self._client: httpx.Client | None = None

    def require(self) -> HttpBackend:
        if not self.base_url:
            raise BackendUnconfigured(self.env_var)
        return self

    def get(self, path: str, *, params: dict[str, Any] | None = None,
            headers: dict[str, str] | None = None) -> Any:
        return self._request("GET", path, params=params, headers=headers)

    def post(self, path: str, *, json: Any = None, params: dict[str, Any] | None = None,
             headers: dict[str, str] | None = None) -> Any:
        return self._request("POST", path, params=params, json=json, headers=headers)

    def delete(self, path: str, *, params: dict[str, Any] | None = None,
               headers: dict[str, str] | None = None) -> Any:
        return self._request("DELETE", path, params=params, headers=headers)

    def _http(self) -> httpx.Client:
        self.require()
        if self._client is None:
            kwargs: dict[str, Any] = {
                "base_url": self.base_url,
                "timeout": self.timeout,
                "headers": {"Accept": "application/json"},
            }
            if self.transport is not None:
                kwargs["transport"] = self.transport
            self._client = httpx.Client(**kwargs)
        return self._client

    def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None,
                 json: Any = None, headers: dict[str, str] | None = None) -> Any:
        try:
            response = self._http().request(method, path, params=params, json=json, headers=headers)
        except httpx.RequestError as e:
            raise BackendError(
                status=None, body=str(e),
                what_failed=f"{self.env_var} request failed",
                env_var=self.env_var,
            ) from e
        text = response.text or ""
        if response.status_code >= 400:
            raise BackendError(
                status=response.status_code, body=text,
                what_failed=f"{self.env_var} returned {response.status_code}",
                env_var=self.env_var,
            )
        if not text:
            return None
        try:
            return response.json()
        except ValueError as e:
            raise BackendError(
                status=response.status_code, body=text,
                what_failed=f"{self.env_var} returned non-JSON",
                env_var=self.env_var, retryable=False,
            ) from e


# ------------------------------------------------------------------ kubernetes

class Kube:
    """Official kubernetes client. Built from a kubeconfig file, or in-cluster when none.

    Construction reads local config only; it does not contact the cluster. API objects
    (`core`, `apps`, `custom`) are created on first access.
    """

    def __init__(self, kubeconfig: str | None = None) -> None:
        self.kubeconfig = kubeconfig
        self._core = None
        self._apps = None
        self._custom = None
        self._dynamic = None
        self._api_client = None
        self._load_lock = threading.Lock()

    def _load(self) -> None:
        if self._core is not None:
            return
        with self._load_lock:
            if self._core is None:
                self._build()

    def _build(self) -> None:
        from kubernetes import client, config as kube_config
        try:
            if self.kubeconfig:
                api_client = kube_config.new_client_from_config(config_file=self.kubeconfig)
            else:
                configuration = client.Configuration()
                kube_config.load_incluster_config(client_configuration=configuration)
                api_client = client.ApiClient(configuration)
        except Exception as e:
            raise BackendError(
                status=None, body=str(e),
                what_failed="kubernetes client construction failed",
                env_var="ORBITD_KUBECONFIG",
            ) from e
        _bind_default_timeout(api_client)
        # published together, under the lock, _core last: a second thread that sees
        # _core set must see the others too
        self._api_client = api_client
        self._apps = client.AppsV1Api(api_client)
        self._custom = client.CustomObjectsApi(api_client)
        self._core = client.CoreV1Api(api_client)

    @property
    def core(self):
        self._load()
        return self._core

    @property
    def apps(self):
        self._load()
        return self._apps

    @property
    def custom(self):
        self._load()
        return self._custom

    @property
    def dynamic(self):
        """Built on first use: DynamicClient discovers the API and would hit the cluster."""
        if self._dynamic is None:
            self._load()
            with self._load_lock:
                if self._dynamic is None:
                    from kubernetes.dynamic import DynamicClient
                    self._dynamic = DynamicClient(self._api_client)
        return self._dynamic


# ------------------------------------------------------------------ instance git repo

def _bind_default_timeout(api_client: Any) -> None:
    """Every request that names no `_request_timeout` gets KUBE_TIMEOUT. The client passes
    `timeout=None` to the pool whenever a request names none, which overrides any pool
    default, and the dynamic client's discovery calls (/version, /api, /apis) name none;
    bound them here, at the request layer, where nothing can override it."""
    rest = api_client.rest_client
    unbounded = rest.request

    def bounded(method: str, url: str, *args: Any, _request_timeout: Any = None, **kwargs: Any) -> Any:
        return unbounded(method, url, *args, _request_timeout=_request_timeout or KUBE_TIMEOUT, **kwargs)

    rest.request = bounded


class InstanceRepo:
    """Desired state: catalog/projects.yaml, catalog/tags.yaml, monitors/*.yaml.

    Two paths. The CHECKOUT is the git-sync working tree, always at main's newest commit;
    git history is read from it. The tree DESIRED STATE is read from (`root`,
    `require()`) is the checkout, unless the commit gate has pinned the last good
    commit's tree in its place because the newest commit was refused (orbitd/gate.py)."""

    def __init__(self, root: str | None) -> None:
        self._checkout = Path(root) if root else None
        self.pinned: Path | None = None
        self.pinned_commit: str | None = None
        self.env_var = "ORBITD_INSTANCE_DIR"

    @property
    def root(self) -> Path | None:
        return self.pinned if self.pinned is not None else self._checkout

    @root.setter
    def root(self, value: Any) -> None:
        self._checkout = Path(value) if value else None

    def pin(self, tree: Path | None, commit: str | None = None) -> None:
        """Read desired state from `tree`, which is the tree of `commit` (None: from the
        checkout again)."""
        self.pinned = Path(tree) if tree is not None else None
        self.pinned_commit = (commit or "") if tree is not None else None

    def checkout(self) -> Path:
        """The git working tree itself, whatever is pinned."""
        if self._checkout is None:
            raise BackendUnconfigured(self.env_var)
        return self._checkout

    def require(self) -> Path:
        root = self.root
        if root is None:
            raise BackendUnconfigured(self.env_var)
        return root

    def projects(self) -> Any:
        return self._yaml("catalog/projects.yaml")

    def tags(self) -> Any:
        return self._yaml("catalog/tags.yaml")

    def monitors(self) -> list[Any]:
        directory = self.require() / "monitors"
        if not directory.is_dir():
            return []
        out = []
        for path in sorted(directory.glob("*.yaml")):
            out.append(self._load(path))
        return out

    def run_git(self, args: list[str], *, missing_ok: bool = False) -> str:
        """Run `git -C <root> ...`. Missing binary and non-zero exit become BackendError."""
        root = self.checkout()      # history lives in the working tree, never in a pinned tree
        command = "git" + (f" {args[0]}" if args else "")
        try:
            proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
        except FileNotFoundError as e:
            raise BackendError(
                status=None, body=str(e), what_failed=command,
                env_var=self.env_var, retryable=False,
            ) from e
        if proc.returncode != 0:
            if missing_ok:
                return ""
            raise BackendError(
                status=None,
                body=(proc.stderr or proc.stdout or f"{command} failed"),
                what_failed=command,
                env_var=self.env_var, retryable=False,
            )
        return proc.stdout

    def show(self, spec: str, *, missing_ok: bool = False) -> str:
        return self.run_git(["show", spec], missing_ok=missing_ok)

    def tags_history(self, limit: int = 50, skip: int = 0) -> list[dict[str, str]]:
        """`git log --format=%H%x09%aI%x09%s -- catalog/tags.yaml`, newest first.

        `skip` pages from the start of that log so callers can walk past a cap.
        """
        text = self.run_git(
            ["log", f"--skip={int(skip)}", f"--max-count={int(limit)}",
             "--format=%H%x09%aI%x09%s", "--", "catalog/tags.yaml"]
        )
        rows: list[dict[str, str]] = []
        for line in text.splitlines():
            if not line.strip():
                continue
            sha, authored, subject = (line.split("\t", 2) + ["", ""])[:3]
            rows.append({"commit": sha, "authored": authored, "subject": subject})
        return rows

    def _yaml(self, rel: str) -> Any:
        return self._load(self.require() / rel)

    def _load(self, path: Path) -> Any:
        try:
            text = path.read_text()
        except OSError as e:
            raise BackendError(status=None, body=str(e),
                               what_failed=f"reading {path.name} failed") from e
        try:
            return yaml.safe_load(text) or {}
        except yaml.YAMLError as e:
            # the position, never the source text: the message would quote the line
            mark = getattr(e, "problem_mark", None)
            where = (f"line {mark.line + 1} column {mark.column + 1}"
                     if mark is not None else "unknown position")
            raise BackendError(status=None, body=f"{e.__class__.__name__} at {where}",
                               what_failed=f"parsing {path.name} failed", retryable=False) from e


# ------------------------------------------------------------------ bundle

@dataclass
class Backends:
    kube: Kube
    prom: HttpBackend
    loki: HttpBackend
    tempo: HttpBackend
    alertmanager: HttpBackend
    instance: InstanceRepo

    @classmethod
    def from_config(cls, cfg: Config) -> Backends:
        return cls(
            kube=Kube(cfg.kubeconfig),
            prom=HttpBackend(cfg.prometheus_url, "ORBITD_PROMETHEUS_URL"),
            loki=HttpBackend(cfg.loki_url, "ORBITD_LOKI_URL"),
            tempo=HttpBackend(cfg.tempo_url, "ORBITD_TEMPO_URL"),
            alertmanager=HttpBackend(cfg.alertmanager_url, "ORBITD_ALERTMANAGER_URL"),
            instance=InstanceRepo(cfg.instance_dir),
        )


__all__ = [
    "BODY_LIMIT", "HTTP_TIMEOUT", "KUBE_TIMEOUT", "CNPG_GROUP", "CNPG_VERSION", "CNPG_PLURAL",
    "BackendUnconfigured", "BackendError", "HttpBackend", "Kube", "InstanceRepo", "Backends",
]
