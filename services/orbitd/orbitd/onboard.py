"""`orbit onboard status` -- where an instance's onboarding stands, and the one next step.

The desk's half of the orbit-onboard skill. An agent onboarding an organization runs this
after every step instead of guessing where it is: each step of the runbook
(docs/ONBOARDING.md, sections 1 to 8) is a line saying done, todo, blocked or unmeasured,
with the single command that moves it. Everything here is read-only and reads no value:
a credential file is checked for existence, mode and the KEY NAMES it carries; a live
object is checked for existence by name; nothing under `data` is ever touched; and no
error from a file, the cluster client or the kubernetes loader reaches the report or the
error stream with anything but its type and status -- every cluster call is caught here,
and the loader's own logging is silenced while it runs, because a credential plugin's
stderr is exactly what that logging would print.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import ipaddress
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

import yaml

from .platform import load_applications, render_only, resolve_kubeconfig
from .shadow import pair_ok, renders_pair

EXIT_OK = 0
EXIT_TOOL = 1
EXIT_USAGE = 2

DONE, TODO, BLOCKED, UNMEASURED = "done", "todo", "blocked", "unmeasured"

CONFIG_REL = "instance/config.yaml"
APPS_TREE_REL = "clusters/apps"
LOCK_REL = "orbit-core.lock"
APPS_REL = "clusters/platform/apps"
SEALED_REL = "clusters/platform/secrets"
PAIRS_REL = "clusters/orbit"
CATALOG_REL = "catalog/projects.yaml"
TFVARS_REL = "tofu/instance.auto.tfvars"
SOPS_CONFIG = ".sops.yaml"

# The credentials directory (runbook section 2): file name -> (must be private, what it is).
CREDENTIAL_FILES = {
    "env": (True, "the cloud tokens, as `export HCLOUD_TOKEN=...` and `export CLOUDFLARE_API_TOKEN=...` lines"),
    "node-key": (True, "a fresh ssh private key for the node (`ssh-keygen -t ed25519 -f <dir>/node-key -N ''`)"),
    "node-key.pub": (False, "its public half"),
    "ssh-key-path": (False, "one line: the path to node-key"),
    "age.key": (True, "the sops private key (`age-keygen -o <dir>/age.key`), backed up outside this machine"),
}
ENV_KEYS = ("HCLOUD_TOKEN", "CLOUDFLARE_API_TOKEN")
DASHBOARD_PASSWORD_FILE = "grafana-admin-password"
DASHBOARD_SECRET = ("monitoring", "grafana-admin")
SOPS_NAMESPACE, SOPS_KEY_SECRET = "sops", "sops-age"
INVENTORY_NAMESPACE = "orbit"
PLATFORM_INVENTORY_PREFIX, TENANT_INVENTORY_PREFIX = "orbit-platform-inventory-", "orbit-inventory-"
SOPSSECRET_API, SOPSSECRET_KIND = "isindir.github.com/v1alpha3", "SopsSecret"
APPLY_ON = "on"
# What `make verify` inherits from the caller's environment: an allow-list, because the
# alternative (a deny-list of make's, bash's and the loader's control variables) is a
# list that is never complete -- MAKEFLAGS=n, MAKEFILES, BASH_ENV, ENV, LD_PRELOAD each
# made a suite that ran nothing report a pass.
_ACCEPTANCE_ENV_KEEP = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE",
                        "TERM", "TMPDIR", "TZ", "SSH_AUTH_SOCK", "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME",
                        "XDG_CACHE_HOME", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy",
                        "no_proxy", "SOPS_AGE_KEY_FILE", "ORBIT_INSTANCE_NAME",
                        # the node key as THIS process can read it: in a desk the pointer file names
                        # the key's path on the operator machine, which is no path in the container,
                        # and the desk says where its copy is. Without it every node row of verify
                        # failed under `status --accept` and the run's acceptance while `make verify`
                        # typed in the same desk passed (docs/V1.md §8).
                        "ORBIT_SSH_KEY",
                        # the operator's GitHub token, as the desk hands it to every command typed
                        # in it: the row "the vendored core matches the pin" fetches the pinned core,
                        # and a core repository that is private cannot be fetched without it. Dropped,
                        # that one row failed under every acceptance and passed when typed (§8).
                        "GH_TOKEN")

# The init script's own token shape (a dotted config path in double braces); a dashboard's
# `{{pod}}` legend or an alert template's `{{ $labels.instance }}` is not one.
_TOKEN = re.compile(r"(?<!\$)\{\{([a-z][a-zA-Z]*(?:\.[a-zA-Z]+)+)\}\}")
_AGE_RECIPIENT = re.compile(r"\bage1[0-9a-z]{20,}\b")
_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=")

# How a rendered object names a Secret it needs: the keys below carry a name directly, the
# suffix carries a `{name: ...}` reference, the selector keys carry `{name, key}` (the
# Prometheus operator's additional-config fields), imagePullSecrets is a list of `{name}`,
# and `secrets` on the operator's kinds is a list of names. A payload (`data`,
# `stringData`, `binaryData`) is never walked: a Secret's value is not a reference,
# whatever its key is called.
_SECRET_NAME_KEYS = ("secretName", "existingSecret", "existingSecretName", "configSecret", "certSecret")
_SECRET_REF_SUFFIX = "SecretRef"
_SECRET_REF_KEYS = ("secretRef", "secretKeyRef", "keySecret", "additionalScrapeConfigs",
                    "additionalAlertManagerConfigs", "additionalAlertRelabelConfigs",
                    "accessKeyId", "secretAccessKey", "sessionToken", "accessKey", "secretKey", "username", "password",
                    "credentials", "clientSecret", "bearerTokenSecret")
# `secret:` is two shapes: a name (Traefik's basicAuth/digestAuth/forwardAuth) or the
# Prometheus operator's `{name, key}` selector (tlsConfig.ca/cert); a Pod volume's
# `secret: {secretName}` has no `name` and is found by recursion.
_SECRET_EITHER_KEY = "secret"
_SECRET_LIST_KINDS = ("Alertmanager", "Prometheus", "ThanosRuler")
_PAYLOAD_KEYS = ("data", "stringData", "binaryData")
# Kinds that make the Secrets they name (a Certificate writes spec.secretName; an ACME issuer
# writes its account key; a SopsSecret renders its templates) -- never something to seal.
_MAKES_SECRETS = {"Certificate": ("spec", "secretName"),
                  "ClusterIssuer": ("spec", "acme", "privateKeySecretRef", "name"),
                  "Issuer": ("spec", "acme", "privateKeySecretRef", "name")}
# The one bootstrap Secret, made by hand in its own step.
_BOOTSTRAP = {(SOPS_NAMESPACE, SOPS_KEY_SECRET)}
# A Secret an operator writes for itself at start: demanded by nothing but that operator's
# own Deployment, so the exception holds only when the referrer IS that Deployment (a tree
# with a consumer and no operator still demands it).
_SELF_MADE_BY = {("cnpg-system", "cnpg-webhook-cert"): ("Deployment", "cloudnative-pg")}


@dataclass
class Step:
    name: str
    state: str
    detail: str
    next: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"step": self.name, "state": self.state, "detail": self.detail, "next": self.next}


@dataclass
class Report:
    instance: str
    instance_dir: str
    config_dir: str
    steps: list[Step] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return all(s.state == DONE for s in self.steps)

    @property
    def next(self) -> str:
        for s in self.steps:
            if s.state != DONE:
                return s.next or s.detail
        return "every step is in place and the acceptance suite passed"

    def as_dict(self) -> dict[str, Any]:
        return {"instance": self.instance, "instance_dir": self.instance_dir,
                "config_dir": self.config_dir, "complete": self.complete,
                "next": self.next, "steps": [s.as_dict() for s in self.steps]}


# ----------------------------------------------------------------------------- helpers

@contextlib.contextmanager
def _quiet() -> Iterator[None]:
    """Nothing reaches the error stream while the cluster is asked: not a logger (the
    kubernetes loader logs a credential plugin's stderr at ERROR on the root logger) and
    not the stream itself (on a terminal the loader hands the plugin the process's own
    stderr descriptor). Logging is disabled and descriptor 2 points at /dev/null, at the
    descriptor level so a child process inherits the silence; both are restored after."""
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    for stream in (sys.stderr, sys.__stderr__):
        with contextlib.suppress(Exception):
            stream.flush()
    saved = os.dup(2)
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 2)
    try:
        with open(os.devnull, "w") as sink, contextlib.redirect_stderr(sink):
            yield
    finally:
        os.dup2(saved, 2)
        os.close(saved)
        os.close(devnull)
        logging.disable(previous)


def node_key_file(config_dir: Path) -> Path | None:
    """The node's ssh key as a file this process can read, or None. The pointer
    (`ssh-key-path`) names it on the operator machine. Run there, that is the file. Run in a
    desk, the credentials directory is a mount (a key inside it is the same file under the
    mount) and a key from elsewhere on the machine is the copy the desk made (ORBIT_SSH_KEY)."""
    try:
        named = (config_dir / "ssh-key-path").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not named:
        return None
    here = Path(named).expanduser()
    if here.is_file():
        return here
    host_dir = (os.environ.get("ORBIT_HOST_CONFIG_DIR") or "").rstrip("/")
    if host_dir and named.startswith(host_dir + "/"):
        mounted = config_dir / named[len(host_dir) + 1:]
        if mounted.is_file():
            return mounted
    desk_copy = os.environ.get("ORBIT_SSH_KEY") or ""
    if host_dir and desk_copy and Path(desk_copy).is_file():
        return Path(desk_copy)
    return None


def _private(path: Path) -> bool:
    try:
        return stat.S_IMODE(path.stat().st_mode) & 0o077 == 0
    except OSError:
        return False


def _env_key_names(path: Path) -> set[str]:
    """The KEY names an env file exports. Values are never read past the `=`."""
    names: set[str] = set()
    try:
        for line in path.read_text(errors="replace").splitlines():
            m = _ENV_LINE.match(line)
            if m:
                names.add(m.group(1))
    except OSError:
        pass
    return names


def _load_config(instance_dir: Path) -> tuple[dict[str, Any] | None, str]:
    path = instance_dir / CONFIG_REL
    if not path.is_file():
        return None, f"{CONFIG_REL} is missing"
    try:
        doc = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        return None, f"{CONFIG_REL} does not parse: {type(e).__name__}"
    if not isinstance(doc, dict):
        return None, f"{CONFIG_REL} is not a mapping"
    return doc, ""


def _get(doc: Any, *keys: str) -> Any:
    for k in keys:
        if not isinstance(doc, dict):
            return None
        doc = doc.get(k)
    return doc


# Address blocks reserved for documentation (RFC 5737, RFC 3849): the template ships one of
# these as the node address, so an instance still carrying one has not written its server's
# address yet, whatever tofu says.
_DOCUMENTATION_NETS = tuple(ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "2001:db8::/32"))


def _example(value: Any) -> bool:
    text = str(value or "").strip().lower()
    if not text or "example" in text:
        return True
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        return False
    return any(addr in net for net in _DOCUMENTATION_NETS)


def _git_origin(instance_dir: Path) -> bool:
    if not (instance_dir / ".git").exists() or shutil.which("git") is None:
        return False
    try:
        proc = subprocess.run(["git", "-C", str(instance_dir), "remote", "get-url", "origin"],
                              capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and bool(proc.stdout.strip())


def tofu_state_list(tofu_dir: Path) -> list[str] | None:
    """Resource addresses in the instance's tofu state, or None when it cannot be asked here
    (no tofu on this machine, or the directory was never initialised)."""
    if shutil.which("tofu") is None or not (tofu_dir / ".terraform").is_dir():
        return None
    try:
        proc = subprocess.run(["tofu", f"-chdir={tofu_dir}", "state", "list"],
                              capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


ACCEPTANCE_LOG = "acceptance.log"


def run_acceptance(instance_dir: Path, *, kubeconfig: str | None = None,
                   config_dir: str | None = None) -> tuple[int | None, int]:
    """`make verify` in the instance checkout, against the same cluster and credentials
    directory the report measured (the stages read KUBECONFIG and ORBIT_CONFIG_DIR):
    (exit code or None when it could not run, count of FAIL lines). The report carries
    none of its output (the stages print names, not values, but the report has no business
    carrying any of it). With a credentials directory, the output of this one run is kept
    there as `acceptance.log`, private: a run that fails here and passes when typed in the
    same desk (M4, docs/V1.md §8) could not be read at all, and "run it directly" then
    shows a different run."""
    if shutil.which("make") is None:
        return None, 0
    # Nothing inherited may change what make or the stages do, so the environment is
    # rebuilt from the allow-list; the paths are made absolute before make changes
    # directory, so a relative --kubeconfig names the same file the report measured.
    env = {k: v for k, v in os.environ.items() if k in _ACCEPTANCE_ENV_KEEP}
    env["NO_COLOR"] = "1"
    if kubeconfig:
        env["KUBECONFIG"] = str(Path(kubeconfig).expanduser().resolve())
    if config_dir:
        env["ORBIT_CONFIG_DIR"] = str(Path(config_dir).expanduser().resolve())
    try:
        proc = subprocess.run(["make", "verify"], cwd=str(Path(instance_dir).resolve()), capture_output=True,
                              text=True, timeout=1800, env=env)
    except (OSError, subprocess.SubprocessError):
        return None, 0
    fails = sum(1 for line in proc.stdout.splitlines() if line.strip().startswith("FAIL"))
    if config_dir:
        try:
            log = Path(config_dir).expanduser().resolve() / ACCEPTANCE_LOG
            fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", errors="replace", newline="\n") as fh:
                fh.write(proc.stdout + ("\n--- stderr ---\n" + proc.stderr if proc.stderr else ""))
        except OSError:
            pass                        # a log that cannot be kept changes no verdict
    return proc.returncode, fails


class ClusterAnswer(Exception):
    """The cluster answered something other than yes or no: a status, or a transport
    failure. Carries the type and status only; never a body."""

    def __init__(self, what: str) -> None:
        super().__init__(what)
        self.what = what


def _exists(kube: Any, api_version: str, kind: str, name: str, namespace: str | None = None) -> bool | None:
    """True/False for a named object; None when the kind itself is not served (its CRD is
    not applied yet). Only the object's existence is observed; any other answer is a
    ClusterAnswer naming its status, and nothing of a response body survives."""
    from kubernetes.client.exceptions import ApiException
    try:
        from kubernetes.dynamic.exceptions import ResourceNotFoundError
    except ImportError:  # pragma: no cover - older client
        ResourceNotFoundError = ()  # type: ignore[assignment]
    with _quiet():
        try:
            resource = kube.dynamic.resources.get(api_version=api_version, kind=kind)
            resource.get(name=name, namespace=namespace, _request_timeout=REQUEST_TIMEOUT)
            return True
        except ApiException as e:
            status = getattr(e, "status", None)
            if status == 404:
                return False
            raise ClusterAnswer(f"the API answered {status} for {kind} {name}") from None
        except ResourceNotFoundError:
            return None
        except Exception as e:  # noqa: BLE001 - a transport failure, reported by type only
            raise ClusterAnswer(f"the API did not answer for {kind} {name} ({type(e).__name__})") from None


def _fetch(kube: Any, api_version: str, kind: str, name: str, namespace: str | None = None) -> dict[str, Any] | None:
    """One named object as a dict, or None when it is absent; the same answers as
    `_exists` otherwise. Used only for kinds that carry no value (an inventory ConfigMap,
    a Deployment), and the caller keeps names and counts from it."""
    from kubernetes.client.exceptions import ApiException
    with _quiet():
        try:
            got = kube.dynamic.resources.get(api_version=api_version, kind=kind).get(name=name, namespace=namespace,
                                                                                    _request_timeout=REQUEST_TIMEOUT)
        except ApiException as e:
            status = getattr(e, "status", None)
            if status == 404:
                return None
            raise ClusterAnswer(f"the API answered {status} for {kind} {name}") from None
        except Exception as e:  # noqa: BLE001
            raise ClusterAnswer(f"the API did not answer for {kind} {name} ({type(e).__name__})") from None
    if isinstance(got, dict):
        return got
    if hasattr(got, "to_dict"):
        return got.to_dict()
    return None


WORKLOAD_KINDS = ("Deployment", "StatefulSet", "DaemonSet", "CronJob", "Job", "Cluster")
CNPG_HEALTHY = "Cluster in healthy state"


def inventory_workloads(inventory: dict[str, Any]) -> list[tuple[str, str, str, str]]:
    """(api_version, kind, namespace, name) of every workload a tenant inventory says the
    daemon applied: one `<group>/<version>/<kind>/<namespace>/<name>` line per object."""
    out: list[tuple[str, str, str, str]] = []
    for line in str(_get(inventory, "data", "objects") or "").splitlines():
        parts = line.strip().split("/")
        if len(parts) == 5 and parts[2] in WORKLOAD_KINDS:
            out.append((f"{parts[0]}/{parts[1]}" if parts[0] else parts[1], parts[2], parts[3], parts[4]))
    return out


def workload_state(kind: str, obj: dict[str, Any]) -> str:
    """Empty when the workload is serving as applied, else why not, as kubectl's rollout
    status would say it: the controller has seen the applied generation, every replica is
    updated, no old replica lingers, every replica is available. A CronJob serves when it
    is not suspended."""
    generation = int(_get(obj, "metadata", "generation") or 0)
    observed = int(_get(obj, "status", "observedGeneration") or 0)
    if kind == "CronJob":
        return "suspended" if _get(obj, "spec", "suspend") else ""
    if kind == "Job":
        want = int(_get(obj, "spec", "completions") or 1)
        done = int(_get(obj, "status", "succeeded") or 0)
        if done >= want:
            return ""
        if int(_get(obj, "status", "failed") or 0):
            return f"failed ({_get(obj, 'status', 'failed')} attempt(s))"
        return f"running ({done}/{want} complete)"
    if kind == "Cluster":
        # The phase is compared, never repeated: an API string is not the report's to carry.
        want = int(_get(obj, "spec", "instances") or 1)
        ready = int(_get(obj, "status", "readyInstances") or 0)
        if ready < want:
            return f"ready instances {ready}/{want}"
        return "" if str(_get(obj, "status", "phase") or "") == CNPG_HEALTHY else "not in the healthy phase"
    if observed < generation:
        return f"generation {generation} not yet observed ({observed})"
    if kind == "DaemonSet":
        want = int(_get(obj, "status", "desiredNumberScheduled") or 0)
        updated = int(_get(obj, "status", "updatedNumberScheduled") or 0)
        available = int(_get(obj, "status", "numberAvailable") or 0)
        if updated < want:
            return f"updated {updated}/{want}"
        return "" if available >= want else f"available {available}/{want}"
    want = int(_get(obj, "spec", "replicas") if _get(obj, "spec", "replicas") is not None else 1)
    updated = int(_get(obj, "status", "updatedReplicas") or 0)
    total = int(_get(obj, "status", "replicas") or 0)
    available = int((_get(obj, "status", "availableReplicas") if kind == "Deployment" else _get(obj, "status", "readyReplicas")) or 0)
    if updated < want:
        return f"updated {updated}/{want}"
    if total > updated:
        return f"{total - updated} old replica(s) still running"
    return "" if available >= want else f"available {available}/{want}"


def _reachable(kube: Any) -> tuple[bool, str]:
    """A cluster answers (any answer, a 404 included) or it does not (the tunnel is closed,
    the kubeconfig is stale). The loader is silenced: its logging is where a credential
    plugin's stderr would go."""
    from kubernetes.client.exceptions import ApiException
    with _quiet():
        try:
            kube.dynamic.resources.get(api_version="v1", kind="Namespace").get(name="kube-system", _request_timeout=REQUEST_TIMEOUT)
        except ApiException:
            return True, ""
        except Exception as e:  # noqa: BLE001 - any transport failure means unreachable
            return False, type(e).__name__
    return True, ""


def _sealed_files(instance_dir: Path) -> list[tuple[str, str, list[str]]]:
    """(namespace, SopsSecret name, Secret names its templates render) per sealed file.
    A sealed file is ciphertext plus clear metadata; only names are read."""
    out: list[tuple[str, str, list[str]]] = []
    root = instance_dir / SEALED_REL
    if not root.is_dir():
        return out
    # the same files the secrets tooling reads: any depth, .yaml or .yml
    for path in sorted(p for p in root.rglob("*") if p.is_file() and p.suffix in (".yaml", ".yml")):
        try:
            doc = yaml.safe_load(path.read_text())
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(doc, dict) or doc.get("kind") != SOPSSECRET_KIND:
            continue
        name = str(_get(doc, "metadata", "name") or path.stem)
        templates = _get(doc, "spec", "secretTemplates") or []
        secrets = [str(t.get("name")) for t in templates if isinstance(t, dict) and t.get("name")]
        out.append((path.parent.name, name, secrets))
    return out


def _secret_references(node: Any, found: set[str]) -> None:
    """Every Secret name a rendered object refers to, whatever the field."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in _PAYLOAD_KEYS:
                continue
            if key in _SECRET_NAME_KEYS and isinstance(value, str) and value:
                found.add(value)
            elif key == _SECRET_EITHER_KEY and isinstance(value, str) and value:
                found.add(value)
            elif key == _SECRET_EITHER_KEY and isinstance(value, dict) and isinstance(value.get("name"), str) and value["name"]:
                found.add(value["name"])
            elif (key in _SECRET_REF_KEYS or key.endswith(_SECRET_REF_SUFFIX)) and isinstance(value, dict):
                name = value.get("name")
                if isinstance(name, str) and name:
                    found.add(name)
            elif key == "imagePullSecrets" and isinstance(value, list):
                for entry in value:
                    if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                        found.add(entry["name"])
            elif key == "proxyConnectHeader" and isinstance(value, dict):
                # header name -> a list of Secret key selectors (Prometheus remoteWrite/scrape)
                for selectors in value.values():
                    for entry in (selectors if isinstance(selectors, list) else []):
                        if isinstance(entry, dict) and isinstance(entry.get("name"), str) and entry["name"]:
                            found.add(entry["name"])
            _secret_references(value, found)
    elif isinstance(node, list):
        for item in node:
            _secret_references(item, found)


def required_secrets(rendered: list[tuple[str, list[dict[str, Any]]]],
                     app_namespaces: dict[str, str]) -> set[tuple[str, str]]:
    """(namespace, name) of every Secret the rendered platform tree asks for that nothing in
    the tree makes: not a rendered Secret, not a Certificate's target, not a sealed file's
    template. What is left is what a human must seal (or mint) -- derived from the tree,
    never from the sealed files that happen to exist."""
    asked: set[tuple[str, str]] = set()
    made: set[tuple[str, str]] = set()
    for app, objects in rendered:
        for obj in objects:
            if not isinstance(obj, dict):
                continue
            meta = obj.get("metadata") or {}
            namespace = str(meta.get("namespace") or app_namespaces.get(app) or "")
            kind = str(obj.get("kind") or "")
            if kind == "Secret" and meta.get("name"):
                made.add((namespace, str(meta["name"])))
            elif kind == SOPSSECRET_KIND:
                for t in _get(obj, "spec", "secretTemplates") or []:
                    if isinstance(t, dict) and t.get("name"):
                        made.add((namespace, str(t["name"])))
            elif kind in _MAKES_SECRETS:
                target = _get(obj, *_MAKES_SECRETS[kind])
                # an ACME issuer told not to generate its account key needs one supplied
                supplied = kind in ("Issuer", "ClusterIssuer") and bool(_get(obj, "spec", "acme", "disableAccountKeyGeneration"))
                if isinstance(target, str) and target and not supplied:
                    made.add((namespace, target))
            refs: set[str] = set()
            if kind != "Secret":
                # everything but the identity and the payload: a ServiceAccount's
                # imagePullSecrets and an object's spec alike
                _secret_references({k: v for k, v in obj.items() if k not in ("metadata", "status", "apiVersion", "kind", *_PAYLOAD_KEYS)}, refs)
            if kind in _SECRET_LIST_KINDS:
                refs.update(str(n) for n in (_get(obj, "spec", "secrets") or []) if isinstance(n, str) and n)
            for name in refs:
                if _SELF_MADE_BY.get((namespace, name)) == (kind, str(meta.get("name") or "")):
                    continue
                asked.add((namespace, name))
    return {ref for ref in asked if ref not in made and ref not in _BOOTSTRAP and ref[0]}


def catalog_pairs(projects: list[Any]) -> list[tuple[str, str]]:
    """The (project, env) pairs the generator renders, by its own rules: a project hosted
    elsewhere or managed elsewhere renders nothing; every declared environment counts,
    except a non-prod one without a truthy `enabled`; nothing is invented."""
    pairs: list[tuple[str, str]] = []
    for p in projects:
        if not isinstance(p, dict) or not p.get("id"):
            continue
        envs = p.get("environments") if isinstance(p.get("environments"), dict) else {}
        for env in sorted(envs):
            if not pair_ok(p["id"], env) or not renders_pair(p, env):
                continue
            pairs.append((str(p["id"]), str(env)))
    return pairs


_COMMIT = re.compile(r"[0-9a-f]{7,64}")


def _holds_probe_for(text: str, pair: tuple[str, str]) -> bool:
    try:
        docs = list(yaml.safe_load_all(text))
    except yaml.YAMLError:
        return False
    for d in docs:
        if isinstance(d, dict) and d.get("kind") == "Probe":
            labels = _get(d, "metadata", "labels") or {}
            if str(labels.get("orbit.local/project")) == pair[0] and str(labels.get("orbit.local/env")) == pair[1]:
                return True
    return False


def _probe_files_for(instance_dir: Path, pair: tuple[str, str], at_commit: str | None = None) -> list[str]:
    """Every clusters/apps file holding a Probe labelled for the pair, as the daemon
    selects them (any file, not a name pattern): in the working tree, or, given a commit,
    in that commit's tree, so a Probe deleted or relabelled since the daemon applied it
    still counts as part of the declaration that changed."""
    out: list[str] = []
    if at_commit is None:
        apps = Path(instance_dir) / APPS_TREE_REL
        if not apps.is_dir():
            return out
        for path in sorted(apps.glob("*.yaml")):
            try:
                text = path.read_text()
            except OSError:
                continue
            if _holds_probe_for(text, pair):
                out.append(f"{APPS_TREE_REL}/{path.name}")
        return out
    git = ["git", "-C", str(instance_dir)]
    listed = subprocess.run([*git, "ls-tree", "-z", "--name-only", at_commit, "--", f"{APPS_TREE_REL}/"],
                            capture_output=True, text=True, timeout=30)
    for rel in sorted(p for p in listed.stdout.split("\0") if p):
        if not rel.endswith(".yaml"):
            continue
        shown = subprocess.run([*git, "show", f"{at_commit}:{rel}"], capture_output=True, text=True, timeout=30)
        if shown.returncode == 0 and _holds_probe_for(shown.stdout, pair):
            out.append(rel)
    return out


def pair_file_rel(instance_dir: Path, pair: tuple[str, str]) -> str:
    """Where the generator wrote the pair's Application, as the applier resolves it:
    under clusters/orbit when the pair is listed in orbitd.applyPairs, else under
    clusters/apps (an empty list means every pair, from there)."""
    name = f"{pair[0]}-{pair[1]}.yaml"
    if (Path(instance_dir) / PAIRS_REL / name).is_file():
        return f"{PAIRS_REL}/{name}"
    return f"{APPS_TREE_REL}/{name}"


def declaration_landed(instance_dir: Path, pair: tuple[str, str], applied_commit: str) -> str:
    """Empty when the inventory's commit carries the pair's current declaration, else why
    not. The daemon applies a commit; the inventory records it. The declaration is the
    catalog entry and the rendered pair file: their last change must be committed, and
    that commit must be the applied one or an ancestor of it."""
    # Everything the daemon renders for a pair (render.py): the pair file wherever the
    # generator put it, the catalog, the service chart, the namespaces file, the pair's
    # dashboard and the tenant Probes.
    paths = [pair_file_rel(instance_dir, pair), CATALOG_REL, "charts/service",
             f"{APPS_TREE_REL}/namespaces.yaml", f"{APPS_TREE_REL}/dashboard-{pair[0]}-{pair[1]}.yaml",
             *_probe_files_for(instance_dir, pair)]
    if not _COMMIT.fullmatch(applied_commit or ""):
        return "the inventory carries no commit hash" if not applied_commit else "the inventory's commit is not a hash"
    if shutil.which("git") is None:
        return "git is not on this machine, so whether the declaration is committed cannot be told"

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", "-C", str(instance_dir), *args], capture_output=True, text=True, timeout=30)
    try:
        dirty = git("status", "--porcelain", "--", *paths)
        if dirty.returncode != 0:
            return "the checkout is not a git repository"
        if dirty.stdout.strip():
            return "the declaration has uncommitted changes (commit and push, then wait one reconcile)"
        last = git("log", "-1", "--format=%H", "--", *paths)
        declared = last.stdout.strip()
        if not declared:
            return "the declaration is not in the git history"
        known = git("cat-file", "-e", f"{applied_commit}^{{commit}}")
        if known.returncode != 0:
            return f"the applied commit {applied_commit[:12]} is not in the local history (git fetch)"
        # The Probe files the applied tree held for the pair count too: one deleted or
        # relabelled since is a change the daemon has not applied.
        gone = [p for p in _probe_files_for(instance_dir, pair, applied_commit) if p not in paths]
        if gone:
            dirty = git("status", "--porcelain", "--", *gone)
            if dirty.stdout.strip():
                return "the declaration has uncommitted changes (commit and push, then wait one reconcile)"
            since = git("log", "-1", "--format=%H", "--", *gone)
            if since.stdout.strip():
                paths.extend(gone)
                last = git("log", "-1", "--format=%H", "--", *paths)
                declared = last.stdout.strip() or declared
        anc = git("merge-base", "--is-ancestor", declared, applied_commit)
        if anc.returncode == 0:
            return ""
        return f"the applied commit {applied_commit[:12]} predates the declaration's last change {declared[:12]} (push, then wait one reconcile)"
    except (OSError, subprocess.SubprocessError) as e:
        return f"git could not be asked ({type(e).__name__})"


# ------------------------------------------------------------------------------- steps

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102 - the handler's own name
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)


_HOSTNAME_RE = re.compile(r"(?=.{1,253}\Z)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+")


def dashboard_sign_in(domain: str, password_file: Path) -> str:
    """Whether the dashboard accepts the workstation password: "accepted", "refused", or
    "unanswered (<why>)". The value is read in this process and sent as the request's
    Authorization header; nothing returned or raised carries it. The domain comes from
    the instance config and is required to be a hostname before it names the origin
    that receives the header."""
    if not _HOSTNAME_RE.fullmatch(domain or ""):
        return "unanswered (domains.primary is not a hostname shape)"
    try:
        password = Path(password_file).read_text().rstrip("\n")
    except OSError as e:
        return f"unanswered ({type(e).__name__})"
    token = base64.b64encode(f"admin:{password}".encode()).decode()
    req = urllib.request.Request(f"https://grafana.{domain}/api/org", headers={"Authorization": f"Basic {token}"})
    # The header goes to that one origin and nowhere else: no redirect is followed (a
    # redirect would carry it to whatever host answered, over whatever scheme) and no
    # proxy from the environment is consulted.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(req, timeout=20) as resp:
            code = resp.status
    except urllib.error.HTTPError as e:
        code = e.code
    except Exception as e:  # noqa: BLE001 - DNS, TLS, refused: the reason is its type, never its text
        return f"unanswered ({type(e).__name__})"
    if code == 200:
        return "accepted"
    if code in (401, 403):
        return "refused"
    return f"unanswered (HTTP {code})"


def _feature_declared(instance_dir: Path, config: Any, name: str) -> bool:
    """Whether the instance's config has an optional feature on. A tree without the
    features registry, or a block that cannot be read, counts as on: that is what such a
    tree renders, and the `features` step is what reports a block that cannot be read."""
    from . import orbit_features as rules
    try:
        return bool(rules.states(config or {}, rules.load_registry(instance_dir)).get(name, True))
    except rules.FeatureError:
        return True


def status(instance_dir: Path, *, config_dir: Path | None = None, kubeconfig: str | None = None,
           home: Path | None = None, kube_factory: Callable[[str], Any] | None = None,
           tofu_state: Callable[[Path], list[str] | None] = tofu_state_list,
           render: Callable[[Path], list[tuple[str, list[dict[str, Any]]]]] = render_only,
           accept: bool = False,
           acceptance: Callable[..., tuple[int | None, int]] = run_acceptance,
           landed: Callable[[Path, tuple[str, str], str], str] = declaration_landed,
           sign_in: Callable[[str, Path], str] = dashboard_sign_in) -> Report:
    instance_dir = Path(instance_dir)
    home = Path(home) if home is not None else Path.home()
    config, why = _load_config(instance_dir)
    name = str(_get(config, "instance", "name") or "") if config else ""
    if config_dir is None:
        config_dir = home / ".config" / f"{name or '<name>'}-platform"
    report = Report(instance=name, instance_dir=str(instance_dir), config_dir=str(config_dir))
    steps = report.steps
    tofu_env = f"( set -a; . {config_dir}/env; set +a; tofu -chdir=tofu init && tofu -chdir=tofu plan && tofu -chdir=tofu apply )"

    # 1. the instance repo, from the template
    missing = []
    if config is None:
        steps.append(Step("instance-repo", TODO, why,
                          "copy instance/config.yaml.example to instance/config.yaml and fill every field, then `make init`"))
    else:
        for key in (("instance", "name"), ("domains", "primary"), ("git", "repoUrl"), ("git", "github_org")):
            if _example(_get(config, *key)):
                missing.append(".".join(key))
        rendered = (instance_dir / APPS_REL).is_dir() and any((instance_dir / APPS_REL).glob("*.yaml"))
        tokens_left = any(_TOKEN.search(p.read_text(errors="replace"))
                          for p in (instance_dir / "clusters").rglob("*.yaml")) if rendered else False
        if missing:
            steps.append(Step("instance-repo", TODO, f"{CONFIG_REL} still carries the example value for: " + ", ".join(missing),
                              "fill those fields in instance/config.yaml, then `make init`"))
        elif not (instance_dir / LOCK_REL).is_file():
            steps.append(Step("instance-repo", TODO, f"{LOCK_REL} is missing: the core is not pinned", "`make init`"))
        elif not rendered or tokens_left:
            steps.append(Step("instance-repo", TODO,
                              "clusters/ is not rendered" if not rendered else "clusters/ still holds unfilled {{tokens}}",
                              "`make init` (re-runnable; it renders clusters/ from provisioning/ and the config)"))
        elif not _git_origin(instance_dir):
            steps.append(Step("instance-repo", TODO, "the checkout has no `origin` remote",
                              "`git add -A && git commit -m 'instance from template' && gh repo create <org>/<name>-platform --private --source . --push`"))
        else:
            steps.append(Step("instance-repo", DONE, "config filled, core pinned, clusters rendered, origin set"))

    # 2. the credentials directory (existence, mode and key names only)
    absent, open_mode, env_missing = [], [], []
    if not config_dir.is_dir():
        steps.append(Step("credentials", TODO, f"{config_dir} does not exist",
                          f"`mkdir -p -m 700 {config_dir}`, then create each file there yourself, in your own editor: "
                          + "; ".join(f"{n}: {what}" for n, (_, what) in CREDENTIAL_FILES.items())
                          + ". Never paste a token into this conversation."))
    else:
        dir_open = not _private(config_dir)
        for fname, (private, _what) in CREDENTIAL_FILES.items():
            p = config_dir / fname
            if not p.is_file() or p.stat().st_size == 0:
                absent.append(fname)
            elif private and not _private(p):
                open_mode.append(fname)
        if "env" not in absent:
            env_missing = [k for k in ENV_KEYS if k not in _env_key_names(config_dir / "env")]
        ssh_path_ok = True
        if "ssh-key-path" not in absent:
            ssh_path_ok = node_key_file(config_dir) is not None
        recipient = False
        sops_cfg = instance_dir / SOPS_CONFIG
        if sops_cfg.is_file():
            recipient = bool(_AGE_RECIPIENT.search(sops_cfg.read_text(errors="replace")))
        problems = []
        if absent:
            problems.append("missing: " + " ".join(absent))
        if open_mode:
            problems.append("readable by others (chmod 600): " + " ".join(open_mode))
        if dir_open:
            # a group- or world-accessible directory lets another user replace a private file
            # (the env file is sourced as shell), whatever the file's own mode
            problems.append(f"the directory itself is open to others (chmod 700 {config_dir})")
        if env_missing:
            problems.append("env lacks the keys: " + " ".join(env_missing))
        if not ssh_path_ok:
            problems.append("ssh-key-path does not point at a file")
        if not recipient:
            problems.append(f"{SOPS_CONFIG} names no age recipient")
        if problems:
            steps.append(Step("credentials", TODO, "; ".join(problems),
                              f"in your own editor, fix each item under {config_dir} "
                              "(" + "; ".join(f"{n}: {what}" for n, (_, what) in CREDENTIAL_FILES.items() if n in absent or n in open_mode)
                              + (f"; put the age public key from `age-keygen -y {config_dir}/age.key` in {SOPS_CONFIG}" if not recipient else "")
                              + "). Never paste a token into this conversation."))
        else:
            steps.append(Step("credentials", DONE, "env (both keys named), node-key, node-key.pub, ssh-key-path, age.key present and private; age recipient in .sops.yaml"))

    # 3. infrastructure: tofu created the server, the address is in the config
    tfvars = instance_dir / TFVARS_REL
    nodes = _get(config, "nodes") if config else None
    ip = str((nodes or [{}])[0].get("ip") or "") if isinstance(nodes, list) and nodes and isinstance(nodes[0], dict) else ""
    state = tofu_state(instance_dir / "tofu")
    tfvars_missing = []
    if tfvars.is_file():
        text = tfvars.read_text(errors="replace")
        tfvars_missing = [k for k in ("server_name", "ssh_key_name", "zone_ids") if k not in text]
    if not tfvars.is_file() or tfvars_missing:
        steps.append(Step("infrastructure", TODO,
                          f"{TFVARS_REL} is missing" if not tfvars.is_file() else f"{TFVARS_REL} lacks: " + " ".join(tfvars_missing),
                          f"write {TFVARS_REL} (server_name, ssh_key_name, the two key paths, zone_ids), then, in a subshell so the tokens die with it: `{tofu_env}`"))
    elif state is not None and not any(a.startswith("hcloud_server.") for a in state):
        steps.append(Step("infrastructure", TODO, "tofu state holds no server",
                          f"in a subshell so the tokens die with it: `{tofu_env}` (expect adds only, 0 destroy)"))
    elif _example(ip):
        steps.append(Step("infrastructure", TODO, "nodes[0].ip in instance/config.yaml is not the created server's address",
                          "`tofu -chdir=tofu output server_ipv4`, write it as nodes[0].ip in instance/config.yaml, `make init`, commit and push"))
    else:
        how = "tofu state holds the server" if state is not None else "tofu state not asked here (tofu absent or directory not initialised)"
        steps.append(Step("infrastructure", DONE, f"server address {ip} in the config; {how}"))

    # 4. the node: bootstrap ran and the API answers through the tunnel
    kube_path = kubeconfig or str(home / ".kube" / f"config-{name or '<name>'}-platform")
    kube = None
    cluster_note = ""
    if not Path(kube_path).is_file() or Path(kube_path).stat().st_size == 0:
        steps.append(Step("node", TODO, f"no kubeconfig at {kube_path}: the node bootstrap has not run",
                          f"`ORBIT_CONFIG_DIR={config_dir} scripts/bootstrap-node.sh` (idempotent; it writes that kubeconfig and prints the tunnel command)"))
        cluster_note = "the node step is not done"
    else:
        factory = kube_factory or _default_kube
        with _quiet():
            try:
                kube = factory(kube_path)
                ok, err = _reachable(kube)
            except Exception as e:  # noqa: BLE001 - a kubeconfig that will not load is unreachable
                ok, err = False, type(e).__name__
        if ok:
            steps.append(Step("node", DONE, f"kubeconfig at {kube_path}; the API answers"))
        else:
            kube = None
            cluster_note = "the cluster is unreachable"
            steps.append(Step("node", BLOCKED, f"kubeconfig at {kube_path} but the API does not answer ({err})",
                              f"open the tunnel in another terminal: `ssh -i {config_dir}/node-key -N -L 6443:127.0.0.1:6443 root@{ip or '<node ip>'}`, then run this again"))

    def unmeasured(step: str, hint: str) -> Step:
        return Step(step, UNMEASURED, f"not measured: {cluster_note}", hint)

    def measured(step: str, measure: Callable[[], Step], hint: str) -> None:
        """A cluster step: unmeasured without a cluster, blocked on any answer that is not
        yes or no, and never a traceback."""
        if kube is None:
            steps.append(unmeasured(step, hint))
            return
        try:
            steps.append(measure())
        except ClusterAnswer as e:
            steps.append(Step(step, BLOCKED, e.what, "run this again once the API answers; if it keeps answering that way, report the status"))

    # 5. the one bootstrap secret: the sops key
    def _sops_key() -> Step:
        ns = _exists(kube, "v1", "Namespace", SOPS_NAMESPACE)
        key = _exists(kube, "v1", "Secret", SOPS_KEY_SECRET, SOPS_NAMESPACE) if ns else False
        if ns and key:
            return Step("sops-key", DONE, f"namespace {SOPS_NAMESPACE} and Secret {SOPS_KEY_SECRET} exist")
        return Step("sops-key", TODO,
                    f"namespace {SOPS_NAMESPACE} is missing" if not ns else f"Secret {SOPS_NAMESPACE}/{SOPS_KEY_SECRET} is missing",
                    f"`kubectl create namespace {SOPS_NAMESPACE}` then "
                    f"`kubectl -n {SOPS_NAMESPACE} create secret generic {SOPS_KEY_SECRET} --from-file=age.agekey={config_dir}/age.key` "
                    "(the file key must be age.agekey)")
    measured("sops-key", _sops_key, "after the node answers, this step checks the sops namespace and its key Secret")

    # 6. the platform tree, applied app by app
    try:
        applications = load_applications(instance_dir)
    except Exception:  # noqa: BLE001 - an unreadable tree is reported, not raised
        applications = []
    apps = [str(a["metadata"]["name"]) for a in applications]
    app_namespaces = {str(a["metadata"]["name"]): str(_get(a, "spec", "destination", "namespace") or "") for a in applications}

    def _platform() -> Step:
        if not apps:
            return Step("platform", TODO, f"no Application under {APPS_REL}", "`make init` renders the platform tree")
        applied = [a for a in apps if _exists(kube, "v1", "ConfigMap", PLATFORM_INVENTORY_PREFIX + a, INVENTORY_NAMESPACE)]
        pending = [a for a in apps if a not in applied]
        if pending:
            return Step("platform", TODO, f"{len(applied)}/{len(apps)} apps applied; not yet: " + " ".join(pending),
                        "`orbit platform plan .` then `orbit platform apply .` from the instance checkout; "
                        "let the early apps settle (sops, cert-manager, traefik) and re-apply once if an admission hook failed")
        return Step("platform", DONE, f"all {len(apps)} apps carry a stored inventory")
    measured("platform", _platform, "after the node answers, this step checks every app's stored inventory")

    # 7. the sealed values: what the rendered tree asks for, and what is live
    sealed = _sealed_files(instance_dir)

    def _secrets() -> Step:
        try:
            rendered = render(instance_dir)
        except Exception as e:  # noqa: BLE001 - a render that fails is a todo, by type
            return Step("secrets", TODO, f"the platform tree does not render ({type(e).__name__})",
                        "`orbit platform plan .` names the app and the reason")
        wanted = required_secrets(rendered, app_namespaces)
        have = {(ns, s) for ns, _name, secrets in sealed for s in secrets}
        no_file = sorted(f"{ns}/{s}" for ns, s in wanted - have)
        unrendered, absent_secrets, unserved = [], [], False
        for ns, sname, secrets in sealed:
            live = _exists(kube, SOPSSECRET_API, SOPSSECRET_KIND, sname, ns)
            if live is None:
                unserved = True
                break
            if not live:
                unrendered.append(f"{ns}/{sname}")
                continue
            for s in secrets:
                if not _exists(kube, "v1", "Secret", s, ns):
                    absent_secrets.append(f"{ns}/{s}")
        if unserved:
            return Step("secrets", BLOCKED, "the sealed-secret kind is not served yet: the sops controller is not applied",
                        "apply the platform tree first")
        detail = []
        if no_file:
            detail.append("the rendered tree asks for Secrets no sealed file renders: " + " ".join(no_file))
        if unrendered:
            detail.append("sealed files not applied: " + " ".join(unrendered))
        if absent_secrets:
            detail.append("Secrets not rendered from their sealed file: " + " ".join(absent_secrets))
        if detail:
            return Step("secrets", TODO, "; ".join(detail),
                        "mint each generated value (`orbit secrets mint . --name <secret> --namespace <ns>`), have the human seal each supplied one, "
                        "commit, push, `orbit platform apply .`; then `SOPS_AGE_KEY_FILE=<dir>/age.key orbit secrets verify .` to all-match")
        return Step("secrets", DONE, f"{len(sealed)} sealed files applied, every Secret they name exists, "
                                      "and the rendered tree asks for nothing more")
    measured("secrets", _secrets, "after the node answers, this step renders the tree (about a minute) and checks each Secret it asks for")

    # 7b. the optional features: what the config declares is applied, ready, and has what it needs
    def _features() -> Step:
        from types import SimpleNamespace

        from . import features_status
        shim = SimpleNamespace(backends=SimpleNamespace(
            instance=SimpleNamespace(root=str(instance_dir)), kube=kube, prom=None))
        data = features_status.collect(shim)
        rows = [r for r in data["features"] if not r["always"]]
        if data["problem"] and not rows:
            if "predates features" in data["problem"]:
                return Step("features", DONE, "this core predates features: everything it renders runs")
            return Step("features", TODO, data["problem"], "`make init` says what is wrong with the features block")
        if data["problem"]:
            return Step("features", BLOCKED, data["problem"], "run this again once the API answers")
        wrong = [r for r in rows if r["pending"] or (r["declared"] and (
            r["not_ready"] or any(not q["met"] for q in r["requires"])))]
        said = "; ".join(features_status.sentence(r) for r in (wrong or rows))
        if wrong:
            return Step("features", TODO, said, "`orbit features converge .` (then this again)")
        return Step("features", DONE, said or "no optional feature is declared")
    measured("features", _features, "after the node answers, this step compares the declared features with what is applied")

    # 8. dashboard sign-in: the one value with two homes
    pw = config_dir / DASHBOARD_PASSWORD_FILE
    sealed_admin = instance_dir / SEALED_REL / DASHBOARD_SECRET[0] / f"{DASHBOARD_SECRET[1]}.yaml"
    if not _feature_declared(instance_dir, config, "dashboards"):
        steps.append(Step("dashboard-sign-in", DONE, "the dashboards feature is off: there is no dashboard to sign in to"))
    elif not pw.is_file() or pw.stat().st_size == 0 or not _private(pw):
        steps.append(Step("dashboard-sign-in", TODO,
                          f"{DASHBOARD_PASSWORD_FILE} is missing or readable by others under {config_dir}",
                          f"`openssl rand -base64 32 > {pw} && chmod 600 {pw}`, then seal the same value as "
                          f"{SEALED_REL}/{DASHBOARD_SECRET[0]}/{DASHBOARD_SECRET[1]}.yaml (keys admin-user, admin-password), encrypt in place, commit"))
    elif not sealed_admin.is_file():
        steps.append(Step("dashboard-sign-in", TODO, f"{sealed_admin.relative_to(instance_dir)} is missing",
                          "seal the workstation value into that file (keys admin-user, admin-password), encrypt in place with the org's age recipient, commit, push, apply"))
    else:
        def _dashboard() -> Step:
            if not _exists(kube, "v1", "Secret", DASHBOARD_SECRET[1], DASHBOARD_SECRET[0]):
                return Step("dashboard-sign-in", TODO, f"Secret {DASHBOARD_SECRET[0]}/{DASHBOARD_SECRET[1]} is not rendered live",
                            "commit and push the sealed file, then `orbit platform apply .`")
            # The one measurement that proves the two homes hold one value: sign in with
            # the workstation file. A Secret that exists says nothing about its content.
            domain = str(_get(config, "domains", "primary") or "")
            answer = sign_in(domain, pw)
            if answer == "accepted":
                return Step("dashboard-sign-in", DONE, "workstation file private, sealed file present, live Secret rendered, "
                                                       "the dashboard accepts the workstation password")
            if answer == "refused":
                return Step("dashboard-sign-in", TODO, "the dashboard refuses the workstation password: the file and the sealed "
                                                       "value differ, or the sealed admin-user is not `admin`",
                            f"redo step 7 with one value: regenerate {pw}, reseal {SEALED_REL}/{DASHBOARD_SECRET[0]}/{DASHBOARD_SECRET[1]}.yaml "
                            "(admin-user: admin, admin-password: that value), commit, push, `orbit platform apply --app secrets .`, "
                            f"then restart the dashboard so it reads the new value (`kubectl -n {DASHBOARD_SECRET[0]} rollout restart "
                            "deployment -l app.kubernetes.io/name=grafana`; nothing restarts it for you), wait for the rollout, then run this again")
            return Step("dashboard-sign-in", BLOCKED, f"the dashboard at https://grafana.{domain} did not answer the sign-in ({answer})",
                        "check that the name resolves and the platform's ingress serves it (the `platform` step), then run this again")
        measured("dashboard-sign-in", _dashboard, "after the node answers, this step checks the live Secret and signs in with the workstation file")

    # 9. acceptance: the verify suite, run only when asked (minutes)
    if accept:
        code, fails = acceptance(instance_dir, kubeconfig=kube_path, config_dir=str(config_dir))
        if code is None:
            steps.append(Step("acceptance", TODO, "`make verify` could not run here", "run `make verify` in the instance checkout and read its output"))
        elif code == 0:
            steps.append(Step("acceptance", DONE, "`make verify`: every stage passed"))
        else:
            steps.append(Step("acceptance", TODO, f"`make verify` failed ({fails} failing rows)",
                              f"read the failing rows of THIS run in {config_dir / ACCEPTANCE_LOG} (`grep FAIL`); each names what it "
                              "checked. `make verify` in the instance checkout runs it again"))
    else:
        steps.append(Step("acceptance", UNMEASURED, "not measured: the verify suite runs only when asked",
                          "`orbit onboard status . --accept` (runs `make verify`, several minutes)"))

    # 10. the first project: declared, rendered, applied, serving
    catalog = instance_dir / CATALOG_REL
    projects: list[Any] = []
    if catalog.is_file():
        try:
            projects = list((yaml.safe_load(catalog.read_text()) or {}).get("projects") or [])
        except yaml.YAMLError:
            projects = []
    pairs = catalog_pairs(projects)
    mode = _get(config, "orbitd", "applyMode") if config else None
    mode = APPLY_ON if mode is True else ("off" if mode is False else str(mode or "off"))
    listed = _get(config, "orbitd", "applyPairs") if config else None
    listed = [str(x) for x in listed] if isinstance(listed, list) else []
    if not pairs:
        steps.append(Step("first-project", TODO, f"{CATALOG_REL} declares no project",
                          "add one entry (the worked example at the top of catalog/projects.yaml shows every field), `make sync`, commit, push"))
    elif mode != APPLY_ON:
        steps.append(Step("first-project", TODO, f"orbitd.applyMode is {mode!r}: the daemon applies nothing",
                          "set `orbitd.applyMode: \"on\"` (quoted) in instance/config.yaml, `make init`, commit, push, `orbit platform apply --app platform-config .`"))
    else:
        not_rendered = [f"{p}/{e}" for p, e in pairs if not (instance_dir / pair_file_rel(instance_dir, (p, e))).is_file()]
        not_listed = [f"{p}/{e}" for p, e in pairs if listed and f"{p}/{e}" not in listed]
        if not_rendered:
            steps.append(Step("first-project", TODO, "pairs not rendered (no Application under clusters/orbit or clusters/apps): " + " ".join(not_rendered),
                              "`make sync`, commit, push"))
        elif not_listed:
            steps.append(Step("first-project", TODO, "pairs missing from orbitd.applyPairs: " + " ".join(not_listed),
                              "add them to orbitd.applyPairs (or empty the list: every pair), `make init`, commit, push, `orbit platform apply --app platform-config .`"))
        else:
            def _project() -> Step:
                waiting, unready = [], []
                for p, e in pairs:
                    ns = f"{p}-{e}"
                    inventory = _fetch(kube, "v1", "ConfigMap", f"{TENANT_INVENTORY_PREFIX}{ns}", INVENTORY_NAMESPACE)
                    if not _exists(kube, "v1", "Namespace", ns) or inventory is None:
                        waiting.append(f"{p}/{e}")
                        continue
                    behind = landed(instance_dir, (p, e), str(_get(inventory, "data", "commit") or ""))
                    if behind:
                        unready.append(f"{p}/{e}: {behind}")
                        continue
                    expected = inventory_workloads(inventory)
                    if not expected:
                        unready.append(f"{p}/{e} (the inventory lists no workload)")
                    for api, kind, wns, wname in expected:
                        w = _fetch(kube, api, kind, wname, wns)
                        if w is None:
                            unready.append(f"{kind} {wns}/{wname} missing")
                            continue
                        why = workload_state(kind, w)
                        if why:
                            unready.append(f"{kind} {wns}/{wname} {why}")
                if waiting:
                    return Step("first-project", TODO, "pairs not yet applied by orbitd (no namespace or inventory): " + " ".join(waiting),
                                "wait one reconcile period (orbitd.reconcilePeriodS) after the push; if it stays, read the daemon's log for the pair")
                if unready:
                    return Step("first-project", TODO, "workloads not as applied: " + "; ".join(unready),
                                "the orbit-diagnose skill, on that component")
                return Step("first-project", DONE, "applied and serving: " + " ".join(f"{p}/{e}" for p, e in pairs))
            measured("first-project", _project, "after the node answers, this step checks each pair's namespace, inventory and deployments")
    return report


# Seconds a cluster request may take. An endpoint that accepts the connection and then
# says nothing would otherwise hold the verb forever, and the promised `blocked` line
# would never print. The pool timeout covers the dynamic client's discovery calls, which
# take no per-request timeout; every resource request also passes one explicitly.
REQUEST_TIMEOUT = 20


def _default_kube(kubeconfig: str) -> Any:
    from .backends import Kube
    kube = Kube(kubeconfig)
    kube._load()
    rest = kube._api_client.rest_client
    unbounded = rest.request

    # The client passes `timeout=None` to the pool whenever a request names no timeout, which
    # overrides any pool default: discovery calls (/version, /api, /apis) name none. Bound
    # them at the request layer, where nothing can override it.
    def bounded(method: str, url: str, *args: Any, _request_timeout: Any = None, **kwargs: Any) -> Any:
        return unbounded(method, url, *args, _request_timeout=_request_timeout or (10, REQUEST_TIMEOUT), **kwargs)

    rest.request = bounded
    return kube


# ----------------------------------------------------------------------------- command

def render(report: Report) -> str:
    lines = [f"onboard status: {report.instance or '(no instance name yet)'} at {report.instance_dir}",
             f"credentials directory: {report.config_dir}"]
    width = max(len(s.name) for s in report.steps)
    for s in report.steps:
        lines.append(f"  {s.state:10} {s.name:{width}}  {s.detail}")
        if s.state != DONE and s.next:
            lines.append(f"  {'':10} {'':{width}}  next: {s.next}")
    lines.append(("complete: " if report.complete else "next: ") + report.next)
    return "\n".join(lines)


def cmd_status(args: argparse.Namespace) -> int:
    instance_dir = Path(args.instance_dir)
    if not instance_dir.is_dir():
        print(f"orbit onboard status: {instance_dir} is not a directory", file=sys.stderr)
        return EXIT_USAGE
    try:
        report = status(
            instance_dir,
            config_dir=Path(args.config_dir).expanduser() if getattr(args, "config_dir", None) else None,
            kubeconfig=resolve_kubeconfig(getattr(args, "kubeconfig", None)) if getattr(args, "kubeconfig", None) else None,
            home=Path(args.home) if getattr(args, "home", None) else None,
            kube_factory=getattr(args, "kube_factory", None),
            tofu_state=getattr(args, "tofu_state", None) or tofu_state_list,
            render=getattr(args, "render", None) or render_only,
            accept=bool(getattr(args, "accept", False)),
            acceptance=getattr(args, "acceptance", None) or run_acceptance,
            landed=getattr(args, "landed", None) or declaration_landed,
            sign_in=getattr(args, "sign_in", None) or dashboard_sign_in,
        )
    except Exception as e:  # noqa: BLE001 - the report never carries a message it did not write
        print(f"orbit onboard status: could not measure ({type(e).__name__})", file=sys.stderr)
        return EXIT_TOOL
    if getattr(args, "json", False):
        from .client import write_json_document
        write_json_document(report.as_dict(), sort_keys=True)
    else:
        # One print per line: the desk renders every line it writes as its own, and a
        # single multi-line write would reach the terminal with its breaks escaped.
        for line in render(report).splitlines():
            print(line)
    return EXIT_OK if report.complete else EXIT_TOOL


def cmd_onboard(args: argparse.Namespace) -> int:
    if getattr(args, "onboard_command", None) == "status":
        return cmd_status(args)
    if getattr(args, "onboard_command", None) == "run":
        from .onboard_run import cmd_run
        return cmd_run(args)
    print("orbit onboard: unknown command", file=sys.stderr)
    return EXIT_USAGE


def add_parser(sub: Any) -> argparse.ArgumentParser:
    onboard = sub.add_parser("onboard", help="where an instance's onboarding stands")
    osub = onboard.add_subparsers(dest="onboard_command", required=True)
    st = osub.add_parser("status", help="one line per onboarding step, and the single next command; exit 0 when every step is done")
    st.add_argument("instance_dir", help="instance checkout (the directory holding instance/config.yaml)")
    st.add_argument("--config-dir", help="credentials directory; default ~/.config/<instance name>-platform")
    st.add_argument("--kubeconfig", help="kubeconfig to reach the node; default ~/.kube/config-<instance name>-platform")
    st.add_argument("--accept", action="store_true", help="also run `make verify` in the instance checkout (several minutes)")
    st.add_argument("--json", action="store_true", help="print the report as JSON")
    rn = osub.add_parser("run", help="run every onboarding step after the form, in order, resuming where a previous run stopped")
    rn.add_argument("instance_dir", help="instance checkout (the directory holding instance/config.yaml)")
    rn.add_argument("--form", required=True, help="the onboarding form (YAML, mode 600, outside every repository)")
    rn.add_argument("--config-dir", help="credentials directory; default ~/.config/<instance name>-platform")
    rn.add_argument("--kubeconfig", help="kubeconfig the node bootstrap writes; default ~/.kube/config-<instance name>-platform")
    rn.add_argument("--only", help="run this one step (one of the step names the run prints)")
    rn.add_argument("--port", type=int, default=8765, help="local port of the GitHub App flow (the desk publishes it with ORBIT_DESK_PORTS)")
    return onboard


__all__ = ["ClusterAnswer", "Report", "Step", "add_parser", "cmd_onboard", "cmd_status", "render",
           "catalog_pairs", "declaration_landed", "inventory_workloads", "pair_file_rel", "required_secrets", "run_acceptance",
           "status", "tofu_state_list", "workload_state"]
