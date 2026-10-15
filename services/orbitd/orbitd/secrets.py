"""Mint platform credentials and export, verify and adopt cluster Secrets as sops ciphertext.

`orbit secrets adopt` releases each verified Secret from its ExternalSecret (an
Orphan delete) and marks it for the sops-secrets-operator; it never deletes a Secret
and never sends `data`.

`orbit secrets export`, `verify` and `adopt` never write plaintext, never put a
secret value in argv, the environment, a log line or an error message. sops is a
subprocess that reads plaintext on stdin and writes ciphertext on stdout.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
import re
import secrets as _stdlib_secrets
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping

import yaml
from kubernetes.client.exceptions import ApiException
from kubernetes.dynamic.exceptions import ResourceNotFoundError

from .backends import Kube
from .platform import resolve_kubeconfig

EXIT_OK = 0
EXIT_DIFF = 1
EXIT_USAGE = 2

ES_API = "external-secrets.io/v1"
ES_KIND = "ExternalSecret"
SECRET_API = "v1"
SECRET_KIND = "Secret"
SOPS_API = "isindir.github.com/v1alpha3"
SOPS_KIND = "SopsSecret"
SKIP_NS = "infisical"
SECRETS_REL = Path("clusters/platform/secrets")
SOPS_YAML_NAME = ".sops.yaml"
PATH_REGEX = r"clusters/platform/secrets/.*\.yaml"
ENCRYPTED_REGEX = r"^(data|stringData)$"
DEFAULT_TYPE = "Opaque"

SopsRun = Callable[..., subprocess.CompletedProcess[str]]


class SecretsError(Exception):
    """Operator-facing failure. The message names a Secret and key only."""


def run_sops(
    argv: list[str],
    *,
    stdin: str,
    env: Mapping[str, str] | None = None,
    cwd: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Call sops. `stdin` is the only place plaintext (or ciphertext) is passed."""
    try:
        return subprocess.run(
            argv,
            input=stdin,
            capture_output=True,
            text=True,
            env=env,
            cwd=cwd,
        )
    except FileNotFoundError:
        return subprocess.CompletedProcess(argv, 127, "", "sops is not on PATH")


def _as_mapping(obj: Any) -> dict[str, Any]:
    if isinstance(obj, dict):
        return obj
    to_dict = getattr(obj, "to_dict", None)
    if callable(to_dict):
        raw = to_dict()
        if isinstance(raw, dict):
            return raw
    return {}


def _items_of(result: Any) -> list[Any]:
    if result is None:
        return []
    if isinstance(result, dict):
        items = result.get("items")
        return list(items) if isinstance(items, list) else []
    items = getattr(result, "items", None)
    if isinstance(items, list):
        return items
    return []


def _meta(obj: Mapping[str, Any]) -> tuple[str, str]:
    meta = obj.get("metadata") or {}
    if not isinstance(meta, dict):
        return "", ""
    return str(meta.get("namespace") or ""), str(meta.get("name") or "")


def _ok_name(value: str) -> bool:
    return bool(value) and "/" not in value and "\\" not in value and value not in {".", ".."} and "\n" not in value


def _data_of(secret: Mapping[str, Any]) -> dict[str, str]:
    raw = secret.get("data")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        if isinstance(value, bytes):
            out[str(key)] = value.decode("ascii")
        elif value is None:
            out[str(key)] = ""
        else:
            out[str(key)] = str(value)
    return out


def _type_of(secret: Mapping[str, Any]) -> str:
    text = str(secret.get("type") or "").strip()
    return text or DEFAULT_TYPE


def _resource(kube: Any, api_version: str, kind: str) -> Any:
    try:
        return kube.dynamic.resources.get(api_version=api_version, kind=kind)
    except SecretsError:
        raise
    except Exception as e:
        raise SecretsError(f"cannot discover {kind}") from e


def list_external_secrets(kube: Any) -> list[dict[str, Any]]:
    listed = _resource(kube, ES_API, ES_KIND).get()
    found: list[dict[str, Any]] = []
    for item in _items_of(listed):
        doc = _as_mapping(item)
        namespace, name = _meta(doc)
        if namespace == SKIP_NS:
            continue
        if not _ok_name(namespace) or not _ok_name(name):
            continue
        found.append(doc)
    found.sort(key=lambda d: _meta(d))
    return found


def _target_name(es: Mapping[str, Any]) -> str:
    _, es_name = _meta(es)
    spec = es.get("spec") or {}
    target = spec.get("target") if isinstance(spec, dict) else None
    if isinstance(target, dict) and target.get("name"):
        return str(target["name"])
    return es_name


def read_secret(kube: Any, namespace: str, name: str) -> dict[str, Any] | None:
    try:
        obj = _resource(kube, SECRET_API, SECRET_KIND).get(name=name, namespace=namespace)
    except ApiException as e:
        if e.status == 404:
            return None
        raise SecretsError(f"reading Secret {namespace}/{name} failed") from e
    except SecretsError:
        raise
    except Exception as e:
        raise SecretsError(f"reading Secret {namespace}/{name} failed") from e
    return _as_mapping(obj)


def _relpath(namespace: str, name: str) -> str:
    return f"clusters/platform/secrets/{namespace}/{name}.yaml"


def _file_path(instance_dir: Path, namespace: str, name: str) -> Path:
    return instance_dir / SECRETS_REL / namespace / f"{name}.yaml"


def _plaintext_doc(namespace: str, name: str, secret_type: str, data: dict[str, str]) -> dict[str, Any]:
    ordered = {key: data[key] for key in sorted(data)}
    return {
        "apiVersion": SOPS_API,
        "kind": SOPS_KIND,
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "secretTemplates": [
                {
                    "name": name,
                    "type": secret_type,
                    "data": ordered,
                }
            ]
        },
    }


class _QuotedDumper(yaml.SafeDumper):
    """Every string scalar double-quoted: sops reads YAML 1.2, so an unquoted
    `1e10` or `0o17` would be encrypted as a number and decrypt differently."""


def _represent_quoted(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style='"')


_QuotedDumper.add_representer(str, _represent_quoted)


def _dump(doc: dict[str, Any]) -> str:
    text = yaml.dump(
        doc, Dumper=_QuotedDumper, sort_keys=False, default_flow_style=False, width=1 << 30,
    )
    return text if text.endswith("\n") else text + "\n"


def _template_of(doc: Mapping[str, Any]) -> dict[str, Any]:
    spec = doc.get("spec") or {}
    templates = spec.get("secretTemplates") if isinstance(spec, dict) else None
    if not isinstance(templates, list) or not templates:
        return {}
    first = templates[0]
    return first if isinstance(first, dict) else {}


def _string_mapping(value: Any) -> bool:
    return isinstance(value, dict) and all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items())


def _supported_shape(doc: Mapping[str, Any], namespace: str, name: str) -> bool:
    """True for the two shapes a sealed file is written in: one template named as its
    path, holding `data` (base64 values: what export, mint and set write) and/or
    `stringData` (plain values: what the onboarding documents' own `sops --encrypt`
    command seals, its regex being `^(data|stringData)$`). A file of the second shape
    was "unsupported", so `orbit secrets verify`, the documented tripwire, was red for
    good on any instance holding one (QA of v1.0.0, F34). Each is a mapping of string
    to string, never normalised; `stringData` on the spec itself is no field of a
    SopsSecret and stays refused."""
    if doc.get("apiVersion") != SOPS_API:
        return False
    if doc.get("kind") != SOPS_KIND:
        return False
    spec = doc.get("spec")
    if not isinstance(spec, dict) or "stringData" in spec:
        return False
    templates = spec.get("secretTemplates")
    if not isinstance(templates, list) or len(templates) != 1:
        return False
    template = templates[0]
    if not isinstance(template, dict):
        return False
    if str(template.get("name") or name) != name:
        return False
    if "data" not in template and "stringData" not in template:
        return False
    for field in ("data", "stringData"):
        if field in template and not _string_mapping(template[field]):
            return False
    return _meta(doc) == (namespace, name)


def _template_data(template: Mapping[str, Any]) -> dict[str, str]:
    """A template's content as the Secret it becomes holds it: `data` as written, each
    `stringData` value base64-encoded over it (stringData wins, as in Kubernetes)."""
    out = _data_of(template)
    plain = template.get("stringData")
    if isinstance(plain, dict):
        for key, value in plain.items():
            text = "" if value is None else str(value)
            out[str(key)] = base64.b64encode(text.encode("utf-8")).decode("ascii")
    return out


def sops_content(doc: Mapping[str, Any]) -> tuple[str, str, str, dict[str, str]]:
    namespace, name = _meta(doc)
    template = _template_of(doc)
    templ_name = str(template.get("name") or name)
    return namespace, templ_name, _type_of(template), _template_data(template)


def _same_content(
    secret_type: str,
    data: dict[str, str],
    other_type: str,
    other_data: dict[str, str],
) -> bool:
    return secret_type == other_type and data == other_data


def _diff_keys(file_data: dict[str, str], live_data: dict[str, str]) -> list[str]:
    """Describe file-vs-live key differences. Names and reasons only."""
    reasons: list[str] = []
    file_keys = set(file_data)
    live_keys = set(live_data)
    for key in sorted(live_keys - file_keys):
        reasons.append(f"{key}: missing")
    for key in sorted(file_keys - live_keys):
        reasons.append(f"{key}: extra")
    for key in sorted(file_keys & live_keys):
        if file_data[key] != live_data[key]:
            reasons.append(f"{key}: value differs")
    return reasons


def _age_public_line(text: str) -> str | None:
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("AGE-SECRET-KEY-"):
            continue
        if line.lower().startswith("# public key:"):
            key = line.split(":", 1)[1].strip()
            if key.startswith("age1"):
                return key.split()[0]
        if line.startswith("age1"):
            return line.split()[0]
    return None


def load_recipient_file(path: Path) -> str:
    try:
        text = path.read_text()
    except OSError as e:
        raise SecretsError(f"cannot read recipient file {path.name}") from e
    key = _age_public_line(text)
    if not key:
        raise SecretsError(f"no age public key in {path.name}")
    return key


def load_recipient_from_sops_yaml(path: Path) -> str:
    try:
        loaded = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError) as e:
        raise SecretsError("cannot read instance .sops.yaml") from e
    if not isinstance(loaded, dict):
        raise SecretsError("instance .sops.yaml has no age recipient")
    for rule in loaded.get("creation_rules") or []:
        if not isinstance(rule, dict):
            continue
        age = rule.get("age")
        if age:
            parts = [p.strip() for p in str(age).split(",")]
            parts = [p for p in parts if p]
            if parts:
                return ",".join(parts)
    raise SecretsError("instance .sops.yaml has no age recipient")


def write_sops_yaml(path: Path, recipient: str) -> None:
    doc = {
        "creation_rules": [
            {
                "path_regex": PATH_REGEX,
                "encrypted_regex": ENCRYPTED_REGEX,
                "age": recipient,
            }
        ]
    }
    path.write_text(_dump(doc))


def resolve_recipient(instance_dir: Path, recipient_file: str | None) -> str:
    sops_yaml = instance_dir / SOPS_YAML_NAME
    if recipient_file:
        recipient = load_recipient_file(Path(recipient_file))
        if sops_yaml.is_file() and load_recipient_from_sops_yaml(sops_yaml) != recipient:
            raise SecretsError(
                f"recipient in {Path(recipient_file).name} differs from instance {SOPS_YAML_NAME}"
            )
    elif sops_yaml.is_file():
        recipient = load_recipient_from_sops_yaml(sops_yaml)
    else:
        raise SecretsError("need --recipient-file or instance .sops.yaml")
    if not sops_yaml.is_file():
        write_sops_yaml(sops_yaml, recipient)
    return recipient


def _sops_argv(*args: str) -> list[str]:
    return ["sops", "--config", os.devnull, *args]


def encrypt_stdin(plaintext: str, *, recipient: str, relpath: str, sops_run: SopsRun) -> str:
    argv = _sops_argv(
        "encrypt",
        "--age", recipient,
        "--encrypted-regex", ENCRYPTED_REGEX,
        "--input-type", "yaml",
        "--output-type", "yaml",
        "--filename-override", relpath,
    )
    proc = sops_run(argv, stdin=plaintext)
    if proc.returncode != 0:
        raise SecretsError(f"sops encrypt failed for {relpath}")
    return proc.stdout


def decrypt_stdin(ciphertext: str, *, relpath: str, sops_run: SopsRun) -> dict[str, Any]:
    argv = _sops_argv(
        "decrypt",
        "--input-type", "yaml",
        "--output-type", "yaml",
        "--filename-override", relpath,
    )
    proc = sops_run(argv, stdin=ciphertext)
    if proc.returncode != 0:
        raise SecretsError(f"sops decrypt failed for {relpath}")
    try:
        # safe_load keeps YAML types: export double-quotes every string it writes
        # (and sops preserves the quoting), so a genuine file still parses to
        # strings while a hand-written number, list or mapping survives as one
        # for _supported_shape to refuse instead of being normalised to a string.
        loaded = yaml.safe_load(proc.stdout or "") or {}
    except yaml.YAMLError as e:
        raise SecretsError(f"sops decrypt failed for {relpath}") from e
    if not isinstance(loaded, dict):
        raise SecretsError(f"sops decrypt failed for {relpath}")
    return loaded


def _stage_ciphertext(path: Path, ciphertext: str) -> Path:
    """Write ciphertext to a unique temp beside its target; the caller places it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(ciphertext)
    except BaseException:
        os.unlink(tmp)
        raise
    return Path(tmp)


def _write_ciphertext(path: Path, ciphertext: str) -> None:
    os.replace(_stage_ciphertext(path, ciphertext), path)


def _print_export_line(namespace: str, name: str, secret_type: str, keys: list[str], status: str) -> None:
    key_field = ",".join(keys) if keys else "-"
    print(f"{namespace}/{name} {secret_type} {key_field} {status}")


def _discovery_failure(exc: BaseException) -> bool:
    """True when a discovery error carries an underlying API error.

    The client raises bare ResourceNotFoundError when the kind is not served,
    and the same type when a group's endpoint does not answer (a 503 on the
    group reads as "no matches"). A bare error means "not served"; one with
    an API error underneath means discovery did not answer."""
    seen: set[int] = set()
    node: BaseException | None = exc
    while node is not None and id(node) not in seen:
        seen.add(id(node))
        if isinstance(node, ApiException):
            return True
        node = node.__cause__ if node.__cause__ is not None else node.__context__
    return False


def _discovery_healthy(kube: Any) -> bool:
    """True when API discovery answers at all, proved on a kind every cluster
    serves. A bare "not found" for ExternalSecret beside a working core
    discovery means the kind is gone; beside a broken one it means nothing."""
    try:
        kube.dynamic.resources.get(api_version=SECRET_API, kind=SECRET_KIND)
    except Exception:
        return False
    return True


ES_CRD_API = "apiextensions.k8s.io/v1"
ES_CRD_KIND = "CustomResourceDefinition"
ES_CRD_NAME = "externalsecrets.external-secrets.io"


def _es_crd_present(kube: Any) -> bool | None:
    """Whether the ExternalSecret CRD exists: True, False when gone, None when
    that cannot be told. Discovery swallows a 503 on the ExternalSecret
    group's endpoint and reports bare "no matches", exactly like an unserved
    kind — but the CRD lives in another group, so a present CRD beside a bare
    "not found" means discovery lied."""
    try:
        resource = kube.dynamic.resources.get(api_version=ES_CRD_API, kind=ES_CRD_KIND)
    except Exception:
        return None
    try:
        resource.get(name=ES_CRD_NAME)
        return True
    except Exception as e:
        if getattr(e, "status", None) == 404:
            return False
        if e.__class__.__name__ == "NotFoundError":
            return False
        return None


def _targets(kube: Any) -> tuple[str, list[tuple[str, str]]]:
    """(status, unique (namespace, secret-name)) for every non-infisical
    ExternalSecret. Three states, said out loud by the caller:

    * "ok" — the kind is served; the list may still be empty.
    * "unserved" — the kind is not served (ESO deleted): no targets.
    * raise — discovery did not answer, so an empty list would be a lie.
      A 503 on the group's endpoint reads as bare "no matches", exactly like
      an unserved kind (discovery swallows ServiceUnavailable), so a bare
      "not found" is unserved only when the ExternalSecret CRD is gone beside
      a working core discovery; a present CRD means discovery lied."""
    try:
        externos = list_external_secrets(kube)
    except SecretsError as e:
        cause = e.__cause__
        if (isinstance(cause, ResourceNotFoundError)
                and not _discovery_failure(cause)):
            if _es_crd_present(kube) is False and _discovery_healthy(kube):
                return "unserved", []
        raise SecretsError(f"cannot discover {ES_KIND}: API discovery did not answer") from cause
    except ApiException as e:
        raise SecretsError(f"cannot discover {ES_KIND}: API discovery did not answer") from e
    seen: dict[tuple[str, str], None] = {}
    for es in externos:
        namespace, _ = _meta(es)
        name = _target_name(es)
        if not _ok_name(namespace) or not _ok_name(name):
            continue
        seen.setdefault((namespace, name), None)
    return "ok", list(seen)


def export_secrets(
    instance_dir: Path,
    kube: Any,
    *,
    recipient_file: str | None = None,
    sops_run: SopsRun | None = None,
) -> int:
    runner = sops_run or run_sops
    try:
        recipient = resolve_recipient(instance_dir, recipient_file)
    except SecretsError as e:
        print(f"orbit secrets export: {e}", file=sys.stderr)
        return EXIT_USAGE
    failed = False
    try:
        status, targets = _targets(kube)
    except SecretsError as e:
        print(f"orbit secrets export: {e}", file=sys.stderr)
        return EXIT_DIFF
    except Exception as e:
        print(f"orbit secrets export: {type(e).__name__}", file=sys.stderr)
        return EXIT_DIFF
    if status == "unserved":
        print(f"orbit secrets export: refusing: {ES_KIND} is not served by this "
              f"cluster, so export cannot prove there is nothing to export",
              file=sys.stderr)
        return EXIT_DIFF
    for namespace, name in targets:
        relpath = _relpath(namespace, name)
        path = _file_path(instance_dir, namespace, name)
        try:
            live = read_secret(kube, namespace, name)
            if live is None:
                print(f"{namespace}/{name} missing-secret", file=sys.stderr)
                failed = True
                continue
            secret_type = _type_of(live)
            data = _data_of(live)
            keys = sorted(data)
            if path.is_file():
                try:
                    existing = decrypt_stdin(path.read_text(), relpath=relpath, sops_run=runner)
                    _, _, existing_type, existing_data = sops_content(existing)
                    if _same_content(secret_type, data, existing_type, existing_data):
                        try:
                            raw = yaml.safe_load(path.read_text()) or {}
                            sops_block = raw.get("sops") if isinstance(raw, dict) else None
                            age_entries = sops_block.get("age") if isinstance(sops_block, dict) else None
                            file_recipients: set[str] = set()
                            if isinstance(age_entries, list):
                                for entry in age_entries:
                                    if isinstance(entry, dict):
                                        value = entry.get("recipient")
                                    elif isinstance(entry, str):
                                        value = entry
                                    else:
                                        continue
                                    for part in str(value or "").split(","):
                                        part = part.strip()
                                        if part:
                                            file_recipients.add(part)
                            wanted = {p.strip() for p in recipient.split(",") if p.strip()}
                            if file_recipients == wanted:
                                _print_export_line(namespace, name, secret_type, keys, "unchanged")
                                continue
                        except yaml.YAMLError:
                            pass
                except SecretsError as e:
                    print(f"orbit secrets export: {e}", file=sys.stderr)
                    failed = True
                    continue
            plaintext = _dump(_plaintext_doc(namespace, name, secret_type, data))
            ciphertext = encrypt_stdin(
                plaintext, recipient=recipient, relpath=relpath, sops_run=runner,
            )
            _write_ciphertext(path, ciphertext)
            _print_export_line(namespace, name, secret_type, keys, "written")
        except SecretsError as e:
            print(f"orbit secrets export: {e}", file=sys.stderr)
            failed = True
        except Exception as e:
            print(f"orbit secrets export: {namespace}/{name} {type(e).__name__}", file=sys.stderr)
            failed = True
    return EXIT_DIFF if failed else EXIT_OK


# ------------------------------------------------------------------ mint

_DNS_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_KEY_RE = re.compile(r"[-._a-zA-Z0-9]+")


def _symlink_on_mint_path(instance_dir: Path, path: Path) -> Path | None:
    """First existing symlink from <instance>/clusters down to `path`, if any.

    Each component is checked with `is_symlink()` (lstat, no following), so a
    crafted checkout cannot redirect a write outside the instance. Missing
    components are skipped: they are created later as plain directories.
    """
    base = Path(instance_dir)
    stop = base / "clusters"
    if path != stop and stop not in path.parents:
        return None
    chain: list[Path] = []
    current = path
    for _ in range(64):
        chain.append(current)
        if current == stop:
            break
        current = current.parent
    else:
        return None
    for component in reversed(chain):
        if component.is_symlink():
            return component
    return None


def mint_secret(
    instance_dir: Path,
    *,
    name: str,
    namespaces: list[str],
    key: str = "token",
    recipient_file: str | None = None,
    rotate: bool = False,
    sops_run: SopsRun = run_sops,
    value_factory: Callable[[], str] | None = None,
) -> int:
    """Seal one fresh random value as a SopsSecret file per namespace.

    Every file holds the same value under `key`; the value travels only in
    memory and on sops's stdin, never in argv, the environment, output or an
    error message."""
    if not isinstance(name, str) or _DNS_LABEL_RE.fullmatch(name) is None:
        print(f"orbit secrets mint: invalid --name {name!r}", file=sys.stderr)
        return EXIT_USAGE
    if (
        not namespaces
        or any(not isinstance(ns, str) or _DNS_LABEL_RE.fullmatch(ns) is None for ns in namespaces)
    ):
        print("orbit secrets mint: need at least one --namespace, each a DNS label", file=sys.stderr)
        return EXIT_USAGE
    if not isinstance(key, str) or _KEY_RE.fullmatch(key) is None:
        print(f"orbit secrets mint: invalid --key {key!r}", file=sys.stderr)
        return EXIT_USAGE
    paths = [(ns, _file_path(Path(instance_dir), ns, name)) for ns in namespaces]
    missing = [ns for ns, path in paths if not path.is_file()]
    if not missing and not rotate:
        for ns, _ in paths:
            print(f"{ns}/{name} {key} present")
        return EXIT_OK
    if missing and len(missing) != len(paths) and not rotate:
        refs = ", ".join(f"{ns}/{name}" for ns in missing)
        print(f"orbit secrets mint: missing {refs}; pass --rotate to mint", file=sys.stderr)
        return EXIT_USAGE
    base = Path(instance_dir)
    for _, path in paths:
        offender = _symlink_on_mint_path(base, path)
        if offender is not None:
            try:
                rel = offender.relative_to(base).as_posix()
            except ValueError:
                rel = offender.as_posix()
            print(f"orbit secrets mint: refusing symlink on path {rel}", file=sys.stderr)
            return EXIT_USAGE
    runner = sops_run or run_sops
    try:
        recipient = resolve_recipient(Path(instance_dir), recipient_file)
    except SecretsError as e:
        print(f"orbit secrets mint: {e}", file=sys.stderr)
        return EXIT_USAGE
    value = value_factory() if value_factory is not None else _stdlib_secrets.token_urlsafe(48)
    encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
    # Every namespace is sealed before any file is placed: a sops failure on the
    # second namespace must not leave the first rotated and the rest on the old value.
    sealed: list[tuple[str, Path, str]] = []
    for ns, path in paths:
        relpath = _relpath(ns, name)
        try:
            plaintext = _dump(_plaintext_doc(ns, name, DEFAULT_TYPE, {key: encoded}))
            ciphertext = encrypt_stdin(
                plaintext, recipient=recipient, relpath=relpath, sops_run=runner,
            )
        except SecretsError as e:
            print(f"orbit secrets mint: {e}", file=sys.stderr)
            return EXIT_DIFF
        except Exception as e:
            print(f"orbit secrets mint: {ns}/{name} {type(e).__name__}", file=sys.stderr)
            return EXIT_DIFF
        sealed.append((ns, path, ciphertext))
    # ...and staged beside its target before any is placed, so a write failure on one
    # namespace leaves every file as it was (and no temp behind).
    staged: list[tuple[str, Path, Path]] = []
    try:
        for ns, path, ciphertext in sealed:
            if path.exists() and not path.is_file():
                # a directory (or a socket, a device) at the target would fail the
                # rename after earlier namespaces were already placed
                print(f"orbit secrets mint: {ns}/{name} target is not a regular file",
                      file=sys.stderr)
                return EXIT_DIFF
            try:
                staged.append((ns, path, _stage_ciphertext(path, ciphertext)))
            except Exception as e:
                print(f"orbit secrets mint: {ns}/{name} {type(e).__name__}", file=sys.stderr)
                return EXIT_DIFF
        placed = 0
        for ns, path, tmp in staged:
            try:
                os.replace(tmp, path)
            except Exception as e:
                # a target that stopped being a file between staging and placing
                print(f"orbit secrets mint: {ns}/{name} {type(e).__name__}"
                      f"{'; the ' + str(placed) + ' namespace(s) before it were rotated' if placed else ''}",
                      file=sys.stderr)
                return EXIT_DIFF
            placed += 1
            print(f"{ns}/{name} {key} minted")
    finally:
        for _, _, tmp in staged:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
    return EXIT_OK


# ------------------------------------------------------------------ copy

COPY_HELP = "place an existing sealed file in a second namespace, re-signed by sops"


def copy_secret(
    instance_dir: Path,
    *,
    source: str | Path | None = None,
    namespace: str | None = None,
    sops_run: SopsRun | None = None,
) -> int:
    """Place an existing sealed file in a second namespace, re-signed by sops.

    Only `metadata.namespace` is rewritten, through `sops set`, which recomputes
    the file's signature over the edited document: sops signs the whole document,
    so rewriting the namespace by hand breaks the seal. The encrypted values
    travel across byte-identical and are never opened outside the tool — no value
    reaches a log line, an error message, a command line or a file. The key is
    the one the tool already holds in its environment for every other sealing
    operation. Prints the copy's path, which the caller applies and cleans up."""
    runner = sops_run or run_sops
    if not _age_key_ok():
        print("orbit secrets copy: SOPS_AGE_KEY_FILE is not set or not a file", file=sys.stderr)
        return EXIT_USAGE
    if not isinstance(namespace, str) or _DNS_LABEL_RE.fullmatch(namespace) is None:
        print(f"orbit secrets copy: invalid --namespace {namespace!r}", file=sys.stderr)
        return EXIT_USAGE
    if source is None or (isinstance(source, str) and not source):
        print("orbit secrets copy: need --file <sealed file>", file=sys.stderr)
        return EXIT_USAGE
    src = Path(source)
    try:
        text = src.read_text()
    except OSError:
        print(f"orbit secrets copy: cannot read {src}", file=sys.stderr)
        return EXIT_USAGE
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError:
        print(f"orbit secrets copy: {src} is not a sealed SopsSecret file", file=sys.stderr)
        return EXIT_USAGE
    if (
        not isinstance(raw, dict)
        or raw.get("apiVersion") != SOPS_API
        or raw.get("kind") != SOPS_KIND
    ):
        print(f"orbit secrets copy: {src} is not a sealed SopsSecret file", file=sys.stderr)
        return EXIT_USAGE
    sops_block = raw.get("sops")
    if (
        not isinstance(sops_block, dict)
        or not sops_block.get("age")
        or not sops_block.get("mac")
    ):
        print(f"orbit secrets copy: {src} is not sealed; refusing to copy cleartext",
              file=sys.stderr)
        return EXIT_USAGE
    meta = raw.get("metadata")
    if not isinstance(meta, dict):
        meta = {}
        raw["metadata"] = meta
    src_ns = str(meta.get("namespace") or "")
    name = str(meta.get("name") or src.stem)
    if _DNS_LABEL_RE.fullmatch(name) is None:
        print(f"orbit secrets copy: sealed file {src} names {name!r}, not a Secret name",
              file=sys.stderr)
        return EXIT_USAGE
    if namespace == src_ns:
        print(f"orbit secrets copy: {src} is already in namespace {namespace}", file=sys.stderr)
        return EXIT_USAGE
    base = Path(instance_dir)
    target = _file_path(base, namespace, name)
    if target.is_file() or target.is_symlink():
        print(f"orbit secrets copy: refusing to overwrite {_relpath(namespace, name)}",
              file=sys.stderr)
        return EXIT_USAGE
    offender = _symlink_on_mint_path(base, target)
    if offender is not None:
        try:
            rel = offender.relative_to(base).as_posix()
        except ValueError:
            rel = offender.as_posix()
        print(f"orbit secrets copy: refusing symlink on path {rel}", file=sys.stderr)
        return EXIT_USAGE
    relpath = _relpath(namespace, name)
    # Crash safety: the finished file is built under a unique temporary name and
    # placed in one step, so an interrupt never leaves the target path naming the
    # source namespace (which every later run would then refuse, wedging the
    # drill). mkstemp creates with O_EXCL and os.link places atomically, refusing
    # a taken target instead of racing it. Only ciphertext ever touches the
    # temporary file. It keeps the .yaml suffix because `sops set` reads the
    # format off the file's extension.
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(target.parent), prefix=target.stem + ".copy-", suffix=".yaml")
    except OSError as e:
        print(f"orbit secrets copy: cannot write {relpath}: {type(e).__name__}",
              file=sys.stderr)
        return EXIT_DIFF
    tmp = Path(tmp_name)
    try:
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(text if text.endswith("\n") else text + "\n")
        except OSError as e:
            print(f"orbit secrets copy: cannot write {relpath}: {type(e).__name__}",
                  file=sys.stderr)
            return EXIT_DIFF
        # The namespace is a validated DNS label, so quoting it is its JSON encoding.
        try:
            proc = runner(
                _sops_argv("set", str(tmp), '["metadata"]["namespace"]', f'"{namespace}"'),
                stdin="",
            )
        except Exception:
            proc = None
        if proc is None or proc.returncode != 0:
            print(f"orbit secrets copy: sops cannot open {src}; refusing to leave "
                  f"a copy nobody can open",
                  file=sys.stderr)
            return EXIT_DIFF
        try:
            os.link(tmp, target)
        except FileExistsError:
            print(f"orbit secrets copy: refusing to overwrite {relpath}",
                  file=sys.stderr)
            return EXIT_USAGE
        except OSError as e:
            print(f"orbit secrets copy: cannot write {relpath}: {type(e).__name__}",
                  file=sys.stderr)
            return EXIT_DIFF
        print(relpath)
        return EXIT_OK
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


# ------------------------------------------------------------------ set

SET_HELP = "rotate one key of a sealed file, re-sealed by sops"


_SET_LOCKS: dict[str, threading.Lock] = {}
_SET_LOCKS_GUARD = threading.Lock()


def _set_lock_for(path: Path) -> threading.Lock:
    """The in-process mutex for one sealed file. Held for the whole rotation;
    a second rotation takes it non-blocking and refuses instead of waiting."""
    ident = os.path.abspath(path)
    with _SET_LOCKS_GUARD:
        lock = _SET_LOCKS.get(ident)
        if lock is None:
            lock = threading.Lock()
            _SET_LOCKS[ident] = lock
        return lock


def _set_targets(raw: Any, key: str) -> list[tuple[int, str]] | None:
    """(template position, field) pairs holding `key`, or None when the text
    is not a sealed SopsSecret at all. Key names are cleartext in a sealed
    file, so finding them needs no opening."""
    if not isinstance(raw, dict):
        return None
    if raw.get("apiVersion") != SOPS_API or raw.get("kind") != SOPS_KIND:
        return None
    sops_block = raw.get("sops")
    if (
        not isinstance(sops_block, dict)
        or not sops_block.get("age")
        or not sops_block.get("mac")
    ):
        return None
    spec = raw.get("spec")
    templates = spec.get("secretTemplates") if isinstance(spec, dict) else None
    if not isinstance(templates, list):
        return []
    found: list[tuple[int, str]] = []
    for position, entry in enumerate(templates):
        if not isinstance(entry, dict):
            continue
        for field in ("data", "stringData"):
            values = entry.get(field)
            if isinstance(values, dict) and key in values:
                found.append((position, field))
    return found


def _landed_value(
    doc: Any,
    key: str,
    targets: list[tuple[int, str]],
    stored: dict[str, str],
) -> bool:
    """True when every targeted template of the finished document holds the
    value it was asked to."""
    if not isinstance(doc, dict):
        return False
    spec = doc.get("spec")
    templates = spec.get("secretTemplates") if isinstance(spec, dict) else None
    if not isinstance(templates, list):
        return False
    for position, field in targets:
        if position >= len(templates):
            return False
        entry = templates[position]
        values = entry.get(field) if isinstance(entry, dict) else None
        if not isinstance(values, dict) or values.get(key) != stored[field]:
            return False
    return True


def _cleartext_forms(value: str) -> list[str]:
    """Every form the value could take in a file that claims to have sealed it."""
    padded = base64.b64encode(value.encode("utf-8")).decode("ascii")
    return [value, padded, padded.rstrip("=")]


def _credential_shape_error(value: Any) -> str | None:
    """Why a candidate credential is not a single printable line, or None.

    A value of the accepted shape has one serialisation in every file shape
    this verb accepts, so the outcome check's three forms are the whole list
    rather than a guess at it. The reason names the shape only, never the
    value."""
    if not isinstance(value, str):
        return "is not a string"
    if value == "":
        return "is empty"
    if "\n" in value:
        return "contains a newline"
    if "\r" in value:
        return "contains a carriage return"
    if not value.isprintable():
        return "contains unprintable characters"
    return None


def _rotate_file(
    path: Path,
    relpath: str,
    ciphertext: str,
    namespace: str,
    name: str,
    key: str,
    targets: list[tuple[int, str]],
    runner: SopsRun,
    value_factory: Callable[[], str] | None,
) -> int:
    """The rotation itself, run holding both locks. A copy of the sealed file
    is made beside the original and `sops set --value-stdin` replaces the one
    value inside the copy — nothing is reassembled, so the recipients, the
    sealed-field pattern, the authentication code and every field this code
    never heard of survive. The copy is proved sealed holding the new value,
    then moved into place in one step. The value travels only in memory and
    on sops's standard input. Every refusal leaves the original byte-for-byte
    as it was. The instance's declared recipient is never consulted: the file
    keeps the recipients it was sealed for, so a declaration that has drifted
    is no reason to refuse."""
    plain = sorted({field for _, field in targets if field != "data"})
    if plain:
        fields = ", ".join(repr(f) for f in plain)
        print(f"orbit secrets set: refusing to rotate {namespace}/{name} {key} "
              f"({relpath}): key is in field {fields}, which stores values in "
              f"clear; the emitter chooses its serialisation there, so the seal "
              f"cannot be proved; leaving the file alone", file=sys.stderr)
        return EXIT_DIFF
    value = value_factory() if value_factory is not None else _stdlib_secrets.token_urlsafe(48)
    shape = _credential_shape_error(value)
    if shape is not None:
        print(f"orbit secrets set: refusing to rotate {namespace}/{name} {key} "
              f"({relpath}): value {shape}; leaving the file alone", file=sys.stderr)
        return EXIT_DIFF
    stored = {
        "data": base64.b64encode(value.encode("utf-8")).decode("ascii"),
        "stringData": value,
    }
    # Crash safety: the finished file is built under a unique temporary name
    # beside the target and moved into place in one step, so an interrupt
    # before the move leaves the original byte-for-byte as it was. mkstemp
    # creates with O_EXCL; only ciphertext ever touches the temporary file.
    # The suffix matches the module's own crash-safe writer, which the
    # applier's glob ignores, so a kill that runs no cleanup leaves no second
    # manifest beside the original.
    try:
        mode = path.stat().st_mode & 0o777
    except OSError as e:
        print(f"orbit secrets set: cannot write {relpath}: {type(e).__name__}",
              file=sys.stderr)
        return EXIT_DIFF
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=path.stem + ".set-", suffix=".tmp")
    except OSError as e:
        print(f"orbit secrets set: cannot write {relpath}: {type(e).__name__}",
              file=sys.stderr)
        return EXIT_DIFF
    tmp = Path(tmp_name)
    try:
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(ciphertext if ciphertext.endswith("\n") else ciphertext + "\n")
        except OSError as e:
            print(f"orbit secrets set: cannot write {relpath}: {type(e).__name__}",
                  file=sys.stderr)
            return EXIT_DIFF
        try:
            os.chmod(tmp, mode)
        except OSError as e:
            print(f"orbit secrets set: cannot write {relpath}: {type(e).__name__}",
                  file=sys.stderr)
            return EXIT_DIFF
        for position, field in targets:
            index = f'["spec"]["secretTemplates"][{position}]["{field}"]["{key}"]'
            try:
                proc = runner(
                    _sops_argv(
                        "set", "--input-type", "yaml", "--output-type", "yaml",
                        str(tmp), index, "--value-stdin",
                    ),
                    stdin=json.dumps(stored[field]),
                )
            except Exception:
                proc = None
            if proc is None or proc.returncode != 0:
                print(f"orbit secrets set: sops cannot set {key} in {namespace}/{name} "
                      f"({relpath}); leaving the file alone", file=sys.stderr)
                return EXIT_DIFF
        # Prove the result before moving it into place: the new value must be
        # absent from the bytes about to land, as written and base64-encoded.
        # The file names its own sealed pattern and this code does not know
        # it, so a value the tool left unsealed — a pattern narrower than the
        # fields this code sets, a suffix rule, or any shape nobody has
        # thought of — refuses here instead of landing in clear beside an
        # intact envelope. Raw bytes only; the value stays in memory, never
        # in argv, output or an error message.
        try:
            staged = tmp.read_text()
        except OSError as e:
            print(f"orbit secrets set: cannot write {relpath}: {type(e).__name__}",
                  file=sys.stderr)
            return EXIT_DIFF
        for form in _cleartext_forms(value):
            if form and form in staged:
                print(f"orbit secrets set: refusing to leave {namespace}/{name} {key} "
                      f"unsealed ({relpath}); leaving the file alone", file=sys.stderr)
                return EXIT_DIFF
        # Prove the finished file opens with the desk's key holding the new
        # value everywhere it was asked to, while the original is still in
        # place. A failure is a refusal that leaves the original untouched.
        try:
            landed = decrypt_stdin(tmp.read_text(), relpath=relpath, sops_run=runner)
        except SecretsError:
            print(f"orbit secrets set: sops cannot reopen {namespace}/{name} "
                  f"({relpath}); refusing to leave a file nobody can open",
                  file=sys.stderr)
            return EXIT_DIFF
        except Exception as e:
            print(f"orbit secrets set: {namespace}/{name} {type(e).__name__}", file=sys.stderr)
            return EXIT_DIFF
        if not _landed_value(landed, key, targets, stored):
            print(f"orbit secrets set: sops cannot set {key} in {namespace}/{name} "
                  f"({relpath}); leaving the file alone", file=sys.stderr)
            return EXIT_DIFF
        try:
            os.replace(tmp, path)
        except OSError as e:
            print(f"orbit secrets set: cannot write {relpath}: {type(e).__name__}",
                  file=sys.stderr)
            return EXIT_DIFF
        print(f"{namespace}/{name} {key} rotated")
        return EXIT_OK
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def set_secret(
    instance_dir: Path,
    *,
    namespace: str | None,
    name: str | None,
    key: str | None,
    recipient_file: str | None = None,
    sops_run: SopsRun | None = None,
    value_factory: Callable[[], str] | None = None,
) -> int:
    """Rotate one key of an existing sealed file, keeping everything else.

    The sealed document is never reassembled here; that design lost
    something it had never read in three successive rounds. Which templates
    carry the key is read off the file's cleartext key names, and the change
    itself is `sops set --value-stdin` inside a copy beside the original, so
    the recipients, the sealed-field pattern and every untouched field
    survive. Before the copy moves into place its bytes must not contain the
    new value as written or base64-encoded, so a file whose own sealed
    pattern leaves the set field unsealed refuses instead of leaking. The
    value travels only in memory and on sops's standard input, never in
    argv, the environment, output or an error message. Prints
    `namespace/name key` on success. Every refusal leaves the file
    byte-for-byte as it was. `recipient_file` is accepted for interface
    stability and ignored: the file keeps the recipients it was sealed for
    and the instance's declared recipient is never consulted."""
    if not isinstance(namespace, str) or _DNS_LABEL_RE.fullmatch(namespace) is None:
        print(f"orbit secrets set: invalid --namespace {namespace!r}", file=sys.stderr)
        return EXIT_USAGE
    if not isinstance(name, str) or _DNS_LABEL_RE.fullmatch(name) is None:
        print(f"orbit secrets set: invalid --name {name!r}", file=sys.stderr)
        return EXIT_USAGE
    if not isinstance(key, str) or _KEY_RE.fullmatch(key) is None:
        print(f"orbit secrets set: invalid --key {key!r}", file=sys.stderr)
        return EXIT_USAGE
    runner = sops_run or run_sops
    if not _age_key_ok():
        print("orbit secrets set: SOPS_AGE_KEY_FILE is not set or not a file", file=sys.stderr)
        return EXIT_USAGE
    base = Path(instance_dir)
    path = _file_path(base, namespace, name)
    relpath = _relpath(namespace, name)
    if not path.is_file():
        print(f"orbit secrets set: no file {relpath}", file=sys.stderr)
        return EXIT_USAGE
    offender = _symlink_on_mint_path(base, path)
    if offender is not None:
        try:
            rel = offender.relative_to(base).as_posix()
        except ValueError:
            rel = offender.as_posix()
        print(f"orbit secrets set: refusing symlink on path {rel}", file=sys.stderr)
        return EXIT_USAGE
    # Preliminary read before taking the lock: early refusal without contention
    # for a missing file, an unsealed document or a key the file does not hold.
    # Nothing decided here is used after the lock is taken; the snapshot the
    # rotation acts on is re-read under the lock below.
    try:
        ciphertext = path.read_text()
    except OSError:
        print(f"orbit secrets set: cannot read {relpath}", file=sys.stderr)
        return EXIT_USAGE
    try:
        raw = yaml.safe_load(ciphertext)
    except yaml.YAMLError:
        print(f"orbit secrets set: {relpath} is not a sealed SopsSecret file", file=sys.stderr)
        return EXIT_USAGE
    targets = _set_targets(raw, key)
    if targets is None:
        print(f"orbit secrets set: {relpath} is not sealed; refusing to write cleartext",
              file=sys.stderr)
        return EXIT_USAGE
    if not targets:
        print(f"orbit secrets set: {namespace}/{name} has no key {key}", file=sys.stderr)
        return EXIT_USAGE
    # Serialize with any other rotation of this file. The in-process mutex
    # decides between threads, the file lock between processes; both are
    # non-blocking, so exactly one of two concurrent rotations reports
    # success and the file holds that one's value.
    mutex = _set_lock_for(path)
    if not mutex.acquire(blocking=False):
        print(f"orbit secrets set: {namespace}/{name} {key} is already being rotated",
              file=sys.stderr)
        return EXIT_DIFF
    try:
        try:
            lock_fd = os.open(path, os.O_RDONLY)
        except OSError:
            print(f"orbit secrets set: cannot read {relpath}", file=sys.stderr)
            return EXIT_DIFF
        try:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                print(f"orbit secrets set: {namespace}/{name} {key} is already being rotated",
                      file=sys.stderr)
                return EXIT_DIFF
            # The snapshot the rotation acts on: re-read while the lock is held.
            # A pre-lock read that straddled another rotation would write back a
            # document predating it, silently reverting a reported success.
            try:
                ciphertext = path.read_text()
            except OSError:
                print(f"orbit secrets set: cannot read {relpath}", file=sys.stderr)
                return EXIT_USAGE
            try:
                raw = yaml.safe_load(ciphertext)
            except yaml.YAMLError:
                print(f"orbit secrets set: {relpath} is not a sealed SopsSecret file",
                      file=sys.stderr)
                return EXIT_USAGE
            targets = _set_targets(raw, key)
            if targets is None:
                print(f"orbit secrets set: {relpath} is not sealed; refusing to write cleartext",
                      file=sys.stderr)
                return EXIT_USAGE
            if not targets:
                print(f"orbit secrets set: {namespace}/{name} has no key {key}",
                      file=sys.stderr)
                return EXIT_USAGE
            return _rotate_file(
                path, relpath, ciphertext, namespace, name, key,
                targets, runner, value_factory,
            )
        finally:
            try:
                os.close(lock_fd)
            except OSError:
                pass
    finally:
        mutex.release()


def _secret_files(instance_dir: Path) -> list[Path]:
    root = instance_dir / SECRETS_REL
    if not root.is_dir():
        return []
    return [p for p in sorted(root.rglob("*")) if p.is_file() and p.suffix in {".yaml", ".yml"}]


def _ns_name_from_path(instance_dir: Path, path: Path) -> tuple[str, str]:
    try:
        rel = path.resolve().relative_to((instance_dir / SECRETS_REL).resolve())
    except ValueError:
        return path.parent.name, path.stem
    parts = rel.parts
    if len(parts) >= 2:
        return parts[0], Path(parts[-1]).stem
    return "", path.stem


STATUS_MATCH = "match"
STATUS_MISMATCH = "mismatch"
STATUS_DECRYPT_FAILED = "decrypt-failed"
STATUS_ERROR = "error"


def _age_key_ok() -> bool:
    key_file = (os.environ.get("SOPS_AGE_KEY_FILE") or "").strip()
    return bool(key_file) and Path(key_file).is_file()


def _verify_file(
    instance_dir: Path,
    path: Path,
    kube: Any,
    runner: SopsRun,
    command: str,
) -> tuple[str, str, str, dict[str, Any], str, dict[str, str]]:
    """Compare one SopsSecret file with its live Secret and print verify's line.

    Returns (namespace, name, status, live metadata, live type, live data). The
    metadata, type and data are those of the same read the comparison used; the
    metadata never carries `data`. The data travels only in memory, for the
    adopt plan's fingerprint."""
    ns_from_path, name_from_path = _ns_name_from_path(instance_dir, path)
    try:
        relpath = path.relative_to(instance_dir).as_posix()
    except ValueError:
        relpath = _relpath(ns_from_path, name_from_path)
    try:
        doc = decrypt_stdin(path.read_text(), relpath=relpath, sops_run=runner)
    except SecretsError as e:
        print(f"{ns_from_path}/{name_from_path} decrypt-failed")
        print(f"orbit secrets {command}: {e}", file=sys.stderr)
        return ns_from_path, name_from_path, STATUS_DECRYPT_FAILED, {}, "", {}
    if not _supported_shape(doc, ns_from_path, name_from_path):
        print(f"{ns_from_path}/{name_from_path} mismatch unsupported shape")
        return ns_from_path, name_from_path, STATUS_MISMATCH, {}, "", {}
    namespace, name, secret_type, data = sops_content(doc)
    namespace = namespace or ns_from_path
    name = name or name_from_path
    try:
        live = read_secret(kube, namespace, name)
    except SecretsError as e:
        print(f"orbit secrets {command}: {e}", file=sys.stderr)
        return namespace, name, STATUS_ERROR, {}, "", {}
    if live is None:
        print(f"{namespace}/{name} mismatch secret missing")
        return namespace, name, STATUS_MISMATCH, {}, "", {}
    live_type = _type_of(live)
    live_data = _data_of(live)
    reasons: list[str] = []
    if secret_type != live_type:
        reasons.append("type differs")
    reasons.extend(_diff_keys(data, live_data))
    meta = live.get("metadata") if isinstance(live.get("metadata"), dict) else {}
    if reasons:
        print(f"{namespace}/{name} mismatch {'; '.join(reasons)}")
        return namespace, name, STATUS_MISMATCH, meta, live_type, live_data
    print(f"{namespace}/{name} match")
    return namespace, name, STATUS_MATCH, meta, live_type, live_data


def verify_secrets(
    instance_dir: Path,
    kube: Any,
    *,
    sops_run: SopsRun | None = None,
) -> int:
    runner = sops_run or run_sops
    if not _age_key_ok():
        print("orbit secrets verify: SOPS_AGE_KEY_FILE is not set or not a file", file=sys.stderr)
        return EXIT_USAGE
    failed = False
    seen_files: set[tuple[str, str]] = set()
    try:
        status, found = _targets(kube)
    except SecretsError as e:
        print(f"orbit secrets verify: {e}", file=sys.stderr)
        return EXIT_DIFF
    except Exception as e:
        print(f"orbit secrets verify: {type(e).__name__}", file=sys.stderr)
        return EXIT_DIFF
    targets = set(found)
    if status == "unserved":
        print(f"{ES_KIND} is not served by this cluster; no targets")
    for path in _secret_files(instance_dir):
        namespace, name, status, _, _, _ = _verify_file(instance_dir, path, kube, runner, "verify")
        if status == STATUS_DECRYPT_FAILED:
            failed = True
            continue
        seen_files.add((namespace, name))
        if status != STATUS_MATCH:
            failed = True
    for namespace, name in sorted(targets - seen_files):
        print(f"{namespace}/{name} no-file")
        failed = True
    return EXIT_DIFF if failed else EXIT_OK


# ------------------------------------------------------------------ adopt

MANAGED_ANNOTATION = "sopssecret/managed"
MANAGED_VALUE = "true"
PROPAGATION_ORPHAN = "Orphan"
MERGE_PATCH = "application/merge-patch+json"
ORPHAN_WAIT_TRIES = 30
ORPHAN_WAIT_SECONDS = 1.0


def _delete_options(uid: str, resource_version: str) -> dict[str, Any]:
    """DeleteOptions for every delete adopt sends. They travel in the body: the API
    server reads DeleteOptions from a present body and ignores query parameters beside
    it. Orphan keeps the Secret when its owning ExternalSecret goes; the uid and
    resourceVersion preconditions refuse the delete if the ExternalSecret was replaced
    or changed since it was proved Retain."""
    return {
        "apiVersion": "v1",
        "kind": "DeleteOptions",
        "propagationPolicy": PROPAGATION_ORPHAN,
        "preconditions": {"uid": uid, "resourceVersion": str(resource_version)},
    }


def _api_reason(e: BaseException) -> str:
    """Never the exception text: an API error body can echo the object, data included."""
    if isinstance(e, ApiException):
        return f"HTTP {e.status}"
    return type(e).__name__


def _es_owner_refs(meta: Mapping[str, Any]) -> list[dict[str, Any]]:
    refs = meta.get("ownerReferences") or []
    if not isinstance(refs, list):
        return []
    return [r for r in refs if isinstance(r, dict) and r.get("kind") == ES_KIND]


def _is_managed(meta: Mapping[str, Any]) -> bool:
    annotations = meta.get("annotations") or {}
    return isinstance(annotations, dict) and annotations.get(MANAGED_ANNOTATION) == MANAGED_VALUE


def _owning_external_secrets(kube: Any, namespace: str, name: str) -> list[tuple[str, str]]:
    """(name, uid) of every ExternalSecret in `namespace` whose target is Secret `name`.
    A cluster that no longer serves the ExternalSecret kind has none. A bare
    "not found" with the CRD still present means discovery lied (503), not
    that there are no owners."""
    try:
        resource = kube.dynamic.resources.get(api_version=ES_API, kind=ES_KIND)
    except ResourceNotFoundError as e:
        if _discovery_failure(e):
            raise SecretsError(f"cannot discover {ES_KIND}: API discovery did not answer") from e
        if _es_crd_present(kube) is False and _discovery_healthy(kube):
            return []
        raise SecretsError(f"cannot discover {ES_KIND}: API discovery did not answer") from e
    except Exception as e:
        raise SecretsError(f"cannot discover {ES_KIND}") from e
    try:
        listed = resource.get(namespace=namespace)
    except Exception as e:
        raise SecretsError(f"listing ExternalSecrets in {namespace} failed: {_api_reason(e)}") from e
    found: list[tuple[str, str]] = []
    for item in _items_of(listed):
        doc = _as_mapping(item)
        es_namespace, es_name = _meta(doc)
        if es_namespace != namespace or not _ok_name(es_name) or _target_name(doc) != name:
            continue
        uid = str((doc.get("metadata") or {}).get("uid") or "")
        if not uid:
            raise SecretsError(f"ExternalSecret {namespace}/{es_name} has no uid")
        found.append((es_name, uid))
    found.sort()
    return found


def _fingerprint(data: dict[str, str]) -> dict[str, str]:
    """A sha256 per data key, keyed by key name. Hashes only: a plan holding this
    never holds a value, so it is safe to keep, compare and (in tests) inspect."""
    return {key: hashlib.sha256(value.encode("utf-8")).hexdigest() for key, value in data.items()}


def _rv_of(obj: Any) -> str:
    """The resourceVersion of a read or patch response. Never a value."""
    meta = _as_mapping(obj).get("metadata")
    return str(meta.get("resourceVersion") or "") if isinstance(meta, dict) else ""


class _AdoptPlan:
    """One Secret to release, as read by the verify comparison. Holds no data:
    the Secret's content survives only as a sha256 per key plus its type."""

    def __init__(self, namespace: str, name: str, uid: str,
                 external_secrets: list[tuple[str, str]], managed: bool,
                 key_hashes: dict[str, str], secret_type: str) -> None:
        self.namespace = namespace
        self.name = name
        self.uid = uid
        self.external_secrets = external_secrets
        self.managed = managed
        self.key_hashes = dict(key_hashes)
        self.secret_type = secret_type

    @property
    def ref(self) -> str:
        return f"{self.namespace}/{self.name}"

    @property
    def released(self) -> bool:
        return not self.external_secrets and self.managed


def _read_external_secret(kube: Any, namespace: str, name: str) -> dict[str, Any] | None:
    """The live ExternalSecret, or None when it is gone. Failures name HTTP status only.
    A bare "not found" for the kind with the CRD still present means discovery
    lied (503), not that the object is gone."""
    try:
        resource = kube.dynamic.resources.get(api_version=ES_API, kind=ES_KIND)
    except ResourceNotFoundError as e:
        if _discovery_failure(e):
            raise SecretsError(f"reading ExternalSecret {namespace}/{name} failed: API discovery did not answer") from e
        if _es_crd_present(kube) is False and _discovery_healthy(kube):
            return None
        raise SecretsError(f"reading ExternalSecret {namespace}/{name} failed: API discovery did not answer") from e
    except Exception as e:
        raise SecretsError(f"reading ExternalSecret {namespace}/{name} failed: {_api_reason(e)}") from e
    try:
        return _as_mapping(resource.get(name=name, namespace=namespace))
    except ApiException as e:
        if e.status == 404:
            return None
        raise SecretsError(
            f"reading ExternalSecret {namespace}/{name} failed: {_api_reason(e)}") from e
    except SecretsError:
        raise
    except Exception as e:
        raise SecretsError(
            f"reading ExternalSecret {namespace}/{name} failed: {_api_reason(e)}") from e


def _plan_adopt(instance_dir: Path, path: Path, kube: Any, runner: SopsRun) -> _AdoptPlan:
    namespace, name, status, meta, live_type, live_data = _verify_file(
        instance_dir, path, kube, runner, "adopt")
    if status != STATUS_MATCH:
        raise SecretsError(f"{namespace}/{name} is not a match")
    uid = str(meta.get("uid") or "")
    if not uid:
        raise SecretsError(f"Secret {namespace}/{name} has no uid")
    external_secrets = _owning_external_secrets(kube, namespace, name)
    if not external_secrets and _es_owner_refs(meta):
        raise SecretsError(
            f"Secret {namespace}/{name} is owned by an ExternalSecret that targets another name")
    targeting_uids = {es_uid for _, es_uid in external_secrets}
    for ref in _es_owner_refs(meta):
        ref_uid = str(ref.get("uid") or "")
        if ref_uid and ref_uid not in targeting_uids:
            live = _read_external_secret(kube, namespace, str(ref.get("name") or ""))
            if live is not None and str((live.get("metadata") or {}).get("uid") or "") == ref_uid:
                raise SecretsError(
                    f"Secret {namespace}/{name} is owned by an ExternalSecret that does not target it")
    for es_name, _ in external_secrets:
        live = _read_external_secret(kube, namespace, es_name)
        if live is None:
            continue
        refs = (live.get("metadata") or {}).get("ownerReferences") or []
        if isinstance(refs, list) and any(
                isinstance(r, dict) and r.get("kind") == "ClusterExternalSecret" for r in refs):
            raise SecretsError(
                f"ExternalSecret {namespace}/{es_name} is owned by a ClusterExternalSecret")
    return _AdoptPlan(namespace, name, uid, external_secrets, _is_managed(meta),
                      _fingerprint(live_data), live_type)


def _await_orphaned(kube: Any, plan: _AdoptPlan, sleep: Callable[[float], None]) -> dict[str, Any]:
    """Re-read the Secret until the garbage collector has stripped its ExternalSecret
    ownerReferences. It must exist with the uid it had when verified throughout.
    Returns the last Secret read: the annotate's resourceVersion comes from it, and
    its content is compared against the verify fingerprint before anything is marked."""
    for attempt in range(ORPHAN_WAIT_TRIES):
        live = read_secret(kube, plan.namespace, plan.name)
        if live is None:
            raise SecretsError(f"Secret {plan.ref} is gone")
        meta = live.get("metadata") if isinstance(live.get("metadata"), dict) else {}
        if str(meta.get("uid") or "") != plan.uid:
            raise SecretsError(f"Secret {plan.ref} was replaced (uid changed)")
        if not _es_owner_refs(meta):
            return live
        if attempt + 1 < ORPHAN_WAIT_TRIES:
            sleep(ORPHAN_WAIT_SECONDS)
    raise SecretsError(f"Secret {plan.ref} still has an ExternalSecret ownerReference")


def _guarded_patch(
    resource: Any,
    *,
    namespace: str,
    name: str,
    uid: str,
    resource_version: str,
    patch: Mapping[str, Any],
) -> Any:
    """The only way adopt sends a patch. The uid and resourceVersion adopt last
    read travel in the patch body's metadata: the API server refuses a patch
    whose resourceVersion is stale, and one whose uid differs from the stored
    object, so a patch can only land on the object it was planned against."""
    body = {key: value for key, value in dict(patch).items()}
    meta = body.get("metadata")
    if not isinstance(meta, dict):
        meta = {}
        body["metadata"] = meta
    meta["uid"] = uid
    meta["resourceVersion"] = str(resource_version)
    return resource.patch(
        name=name, namespace=namespace, body=body, content_type=MERGE_PATCH,
    )


def _ensure_retain(kube: Any, es_resource: Any, namespace: str, name: str,
                   expected_uid: str) -> str | None:
    """Switch an ExternalSecret to deletionPolicy Retain before its delete. ESO's
    cleanup reads that field at delete time: Delete removes the Secret and Merge
    empties its keys, whatever propagation policy the delete carries. The
    ExternalSecret is re-read first: a uid other than the planned one means it
    was replaced since planning, and nothing may touch the replacement.
    Returns the resourceVersion of the version proved Retain (the read one when
    already Retain, the patch response's when patched), for the delete
    precondition; None when the ExternalSecret is already gone and there is
    nothing to delete."""
    try:
        live = es_resource.get(name=name, namespace=namespace)
    except ApiException as e:
        if e.status == 404:
            return None
        raise SecretsError(
            f"reading ExternalSecret {namespace}/{name} failed: {_api_reason(e)}") from e
    except Exception as e:
        raise SecretsError(
            f"reading ExternalSecret {namespace}/{name} failed: {_api_reason(e)}") from e
    doc = _as_mapping(live)
    live_meta = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
    live_uid = str(live_meta.get("uid") or "")
    if live_uid != expected_uid:
        raise SecretsError(f"ExternalSecret {namespace}/{name} was replaced (uid changed)")
    spec = doc.get("spec")
    target = spec.get("target") if isinstance(spec, dict) else None
    policy = target.get("deletionPolicy") if isinstance(target, dict) else None
    if policy is None or policy == "Retain":
        return str(live_meta.get("resourceVersion") or "")
    try:
        patched = _guarded_patch(
            es_resource,
            namespace=namespace, name=name,
            uid=live_uid, resource_version=str(live_meta.get("resourceVersion") or ""),
            patch={"spec": {"target": {"deletionPolicy": "Retain"}}},
        )
    except ApiException as e:
        if e.status == 404:
            return None
        raise SecretsError(
            f"patching ExternalSecret {namespace}/{name} failed: {_api_reason(e)}") from e
    except Exception as e:
        raise SecretsError(
            f"patching ExternalSecret {namespace}/{name} failed: {_api_reason(e)}") from e
    return _rv_of(patched)


def _release(kube: Any, plan: _AdoptPlan, sleep: Callable[[float], None]) -> None:
    es_resource = _resource(kube, ES_API, ES_KIND) if plan.external_secrets else None
    for es_name, es_uid in plan.external_secrets:
        retain_rv = _ensure_retain(kube, es_resource, plan.namespace, es_name, es_uid)
        if retain_rv is None:
            continue
        try:
            es_resource.delete(
                name=es_name, namespace=plan.namespace,
                body=_delete_options(es_uid, retain_rv))
        except ApiException as e:
            if e.status == 404:
                continue
            if e.status == 409:
                raise SecretsError(
                    f"ExternalSecret {plan.namespace}/{es_name} changed since it was "
                    "checked; run adopt again"
                ) from e
            raise SecretsError(
                f"deleting ExternalSecret {plan.namespace}/{es_name} failed: {_api_reason(e)}"
            ) from e
        except Exception as e:
            raise SecretsError(
                f"deleting ExternalSecret {plan.namespace}/{es_name} failed: {_api_reason(e)}"
            ) from e
    live = _await_orphaned(kube, plan, sleep)
    meta = live.get("metadata") if isinstance(live.get("metadata"), dict) else {}
    if _type_of(live) != plan.secret_type or _fingerprint(_data_of(live)) != plan.key_hashes:
        raise SecretsError(f"Secret {plan.ref} changed since verify; run export and verify again")
    if _is_managed(meta):
        return
    try:
        _guarded_patch(
            _resource(kube, SECRET_API, SECRET_KIND),
            namespace=plan.namespace, name=plan.name,
            uid=plan.uid, resource_version=str(meta.get("resourceVersion") or ""),
            patch={"metadata": {"annotations": {MANAGED_ANNOTATION: MANAGED_VALUE}}},
        )
    except Exception as e:
        raise SecretsError(f"annotating Secret {plan.ref} failed: {_api_reason(e)}") from e


def _print_dry(plan: _AdoptPlan) -> None:
    if plan.released:
        print(f"{plan.ref} already released")
        return
    steps = [f"delete ExternalSecret {plan.namespace}/{es_name} (Orphan)"
             for es_name, _ in plan.external_secrets]
    if not plan.managed:
        steps.append(f"annotate {MANAGED_ANNOTATION}={MANAGED_VALUE}")
    print(f"{plan.ref} would release: {', '.join(steps)}")


def _adopt_files(instance_dir: Path, namespace: str | None, name: str | None) -> list[Path]:
    if bool(namespace) != bool(name):
        raise SecretsError("--namespace and --name go together")
    if namespace and name:
        if not _ok_name(namespace) or not _ok_name(name):
            raise SecretsError("invalid --namespace or --name")
        path = _file_path(instance_dir, namespace, name)
        if not path.is_file():
            raise SecretsError(f"no file {_relpath(namespace, name)}")
        return [path]
    files = _secret_files(instance_dir)
    if not files:
        raise SecretsError("no SopsSecret files under clusters/platform/secrets")
    return files


def adopt_secrets(
    instance_dir: Path,
    kube: Any,
    *,
    namespace: str | None = None,
    name: str | None = None,
    yes: bool = False,
    sops_run: SopsRun | None = None,
    sleep: Callable[[float], None] | None = None,
) -> int:
    """Release each verified Secret from its ExternalSecret without deleting it.

    Every selected file is verified before anything changes; one non-match stops the
    run with no mutation. Without `yes` nothing is mutated at all."""
    runner = sops_run or run_sops
    pause = sleep or time.sleep
    if not _age_key_ok():
        print("orbit secrets adopt: SOPS_AGE_KEY_FILE is not set or not a file", file=sys.stderr)
        return EXIT_USAGE
    try:
        files = _adopt_files(instance_dir, namespace, name)
    except SecretsError as e:
        print(f"orbit secrets adopt: {e}", file=sys.stderr)
        return EXIT_USAGE
    plans: list[_AdoptPlan] = []
    failed = False
    for path in files:
        try:
            plans.append(_plan_adopt(instance_dir, path, kube, runner))
        except SecretsError as e:
            print(f"orbit secrets adopt: {e}", file=sys.stderr)
            failed = True
        except Exception as e:
            print(f"orbit secrets adopt: {path.stem} {_api_reason(e)}", file=sys.stderr)
            failed = True
    if failed:
        print("orbit secrets adopt: stopped; nothing changed", file=sys.stderr)
        return EXIT_DIFF
    if not yes:
        for plan in plans:
            _print_dry(plan)
        print("dry run: nothing changed; pass --yes to release")
        return EXIT_OK
    for plan in plans:
        if plan.released:
            print(f"{plan.ref} already released")
            continue
        try:
            _release(kube, plan, pause)
        except SecretsError as e:
            print(f"orbit secrets adopt: {e}; stopped", file=sys.stderr)
            return EXIT_DIFF
        except Exception as e:
            print(f"orbit secrets adopt: {plan.ref} {_api_reason(e)}; stopped", file=sys.stderr)
            return EXIT_DIFF
        print(f"{plan.ref} released")
    return EXIT_OK


def _require_instance_dir(raw: str, command: str) -> Path | None:
    path = Path(raw)
    if not path.is_dir():
        print(f"orbit secrets {command}: {path} is not a directory", file=sys.stderr)
        return None
    return path


def _kube_from_args(args: argparse.Namespace) -> Any:
    if getattr(args, "kube", None) is not None:
        return args.kube
    return Kube(resolve_kubeconfig(getattr(args, "kubeconfig", None)))


def _sops_from_args(args: argparse.Namespace) -> SopsRun:
    return getattr(args, "sops_run", None) or run_sops


def cmd_export(args: argparse.Namespace) -> int:
    instance_dir = _require_instance_dir(args.instance_dir, "export")
    if instance_dir is None:
        return EXIT_USAGE
    try:
        kube = _kube_from_args(args)
    except Exception as e:
        print(f"orbit secrets export: {type(e).__name__}", file=sys.stderr)
        return EXIT_USAGE
    return export_secrets(
        instance_dir, kube,
        recipient_file=getattr(args, "recipient_file", None),
        sops_run=_sops_from_args(args),
    )


def cmd_verify(args: argparse.Namespace) -> int:
    instance_dir = _require_instance_dir(args.instance_dir, "verify")
    if instance_dir is None:
        return EXIT_USAGE
    try:
        kube = _kube_from_args(args)
    except Exception as e:
        print(f"orbit secrets verify: {type(e).__name__}", file=sys.stderr)
        return EXIT_USAGE
    return verify_secrets(instance_dir, kube, sops_run=_sops_from_args(args))


def cmd_adopt(args: argparse.Namespace) -> int:
    instance_dir = _require_instance_dir(args.instance_dir, "adopt")
    if instance_dir is None:
        return EXIT_USAGE
    try:
        kube = _kube_from_args(args)
    except Exception as e:
        print(f"orbit secrets adopt: {type(e).__name__}", file=sys.stderr)
        return EXIT_USAGE
    return adopt_secrets(
        instance_dir, kube,
        namespace=getattr(args, "namespace", None),
        name=getattr(args, "name", None),
        yes=bool(getattr(args, "yes", False)),
        sops_run=_sops_from_args(args),
        sleep=getattr(args, "sleep", None),
    )


def cmd_mint(args: argparse.Namespace) -> int:
    instance_dir = _require_instance_dir(args.instance_dir, "mint")
    if instance_dir is None:
        return EXIT_USAGE
    key = getattr(args, "key", None)
    return mint_secret(
        instance_dir,
        name=getattr(args, "name", None),
        namespaces=list(getattr(args, "namespace", None) or []),
        key=key if key is not None else "token",
        recipient_file=getattr(args, "recipient_file", None),
        rotate=bool(getattr(args, "rotate", False)),
        sops_run=_sops_from_args(args),
    )


def cmd_copy(args: argparse.Namespace) -> int:
    instance_dir = _require_instance_dir(args.instance_dir, "copy")
    if instance_dir is None:
        return EXIT_USAGE
    return copy_secret(
        instance_dir,
        source=getattr(args, "file", None),
        namespace=getattr(args, "namespace", None),
        sops_run=_sops_from_args(args),
    )


def cmd_set(args: argparse.Namespace) -> int:
    instance_dir = _require_instance_dir(args.instance_dir, "set")
    if instance_dir is None:
        return EXIT_USAGE
    return set_secret(
        instance_dir,
        namespace=getattr(args, "namespace", None),
        name=getattr(args, "name", None),
        key=getattr(args, "key", None),
        recipient_file=getattr(args, "recipient_file", None),
        sops_run=_sops_from_args(args),
    )


def cmd_secrets(args: argparse.Namespace) -> int:
    command = getattr(args, "secrets_command", None)
    if command == "export":
        return cmd_export(args)
    if command == "verify":
        return cmd_verify(args)
    if command == "adopt":
        return cmd_adopt(args)
    if command == "mint":
        return cmd_mint(args)
    if command == "copy":
        return cmd_copy(args)
    if command == "set":
        return cmd_set(args)
    print("orbit secrets: unknown command", file=sys.stderr)
    return EXIT_USAGE


def _add_export_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("instance_dir", help="instance checkout; files land under clusters/platform/secrets")
    parser.add_argument(
        "--kubeconfig",
        help="kubeconfig path; ORBITD_KUBECONFIG, then KUBECONFIG, then in-cluster",
    )
    parser.add_argument(
        "--recipient-file", dest="recipient_file",
        help="age public key file; otherwise the instance .sops.yaml",
    )


def _add_verify_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("instance_dir", help="instance checkout containing clusters/platform/secrets")
    parser.add_argument(
        "--kubeconfig",
        help="kubeconfig path; ORBITD_KUBECONFIG, then KUBECONFIG, then in-cluster",
    )


def _add_adopt_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("instance_dir", help="instance checkout containing clusters/platform/secrets")
    parser.add_argument(
        "--kubeconfig",
        help="kubeconfig path; ORBITD_KUBECONFIG, then KUBECONFIG, then in-cluster",
    )
    parser.add_argument("--namespace", help="adopt only this Secret's namespace (with --name)")
    parser.add_argument("--name", help="adopt only this Secret (with --namespace)")
    parser.add_argument(
        "--yes", action="store_true",
        help="make the changes; without it adopt prints what it would do and changes nothing",
    )


def _add_mint_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("instance_dir", help="instance checkout; files land under clusters/platform/secrets")
    parser.add_argument("--name", help="Secret name to mint (a DNS label)")
    parser.add_argument(
        "--namespace", action="append", default=None,
        help="namespace holding a copy of the value (repeatable)",
    )
    parser.add_argument("--key", default="token", help="data key for the minted value")
    parser.add_argument(
        "--recipient-file", dest="recipient_file",
        help="age public key file; otherwise the instance .sops.yaml",
    )
    parser.add_argument(
        "--rotate", action="store_true",
        help="overwrite every file; without it mint refuses a partial set",
    )


def _add_copy_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("instance_dir", help="instance checkout; the copy lands under clusters/platform/secrets")
    parser.add_argument("--file", help="sealed file to place in a second namespace")
    parser.add_argument("--namespace", help="namespace to place the sealed file in")


def _add_set_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("instance_dir", help="instance checkout containing clusters/platform/secrets")
    parser.add_argument("--namespace", help="namespace of the sealed file")
    parser.add_argument("--name", help="Secret name of the sealed file")
    parser.add_argument("--key", help="data key to rotate")
    parser.add_argument(
        "--recipient-file", dest="recipient_file",
        help="age public key file; otherwise the instance .sops.yaml",
    )


ADOPT_HELP = "release verified Secrets from their ExternalSecrets (Orphan delete) and mark them sops-managed"
MINT_HELP = "mint a random value sealed as a SopsSecret file per namespace"


def add_parser(sub: Any) -> argparse.ArgumentParser:
    """Register `secrets` on the orbit CLI subparsers."""
    sec = sub.add_parser("secrets", help="export, verify and adopt cluster Secrets as sops ciphertext")
    sec_sub = sec.add_subparsers(dest="secrets_command", required=True)
    exp = sec_sub.add_parser(
        "export",
        help="write SopsSecret files from live ExternalSecret targets",
    )
    _add_export_arguments(exp)
    ver = sec_sub.add_parser(
        "verify",
        help="compare clusters/platform/secrets files to live Secrets",
    )
    _add_verify_arguments(ver)
    _add_adopt_arguments(sec_sub.add_parser("adopt", help=ADOPT_HELP))
    _add_mint_arguments(sec_sub.add_parser("mint", help=MINT_HELP))
    _add_copy_arguments(sec_sub.add_parser("copy", help=COPY_HELP))
    _add_set_arguments(sec_sub.add_parser("set", help=SET_HELP))
    return sec


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="orbit secrets",
        description="export and verify cluster Secrets as sops ciphertext",
    )
    sub = p.add_subparsers(dest="secrets_command", required=True)
    exp = sub.add_parser("export", help="write SopsSecret files from live ExternalSecret targets")
    _add_export_arguments(exp)
    ver = sub.add_parser("verify", help="compare sops files to live Secrets")
    _add_verify_arguments(ver)
    _add_adopt_arguments(sub.add_parser("adopt", help=ADOPT_HELP))
    _add_mint_arguments(sub.add_parser("mint", help=MINT_HELP))
    _add_copy_arguments(sub.add_parser("copy", help=COPY_HELP))
    _add_set_arguments(sub.add_parser("set", help=SET_HELP))
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return 0 if e.code is None else int(e.code)
    return cmd_secrets(args)


__all__ = [
    "EXIT_OK", "EXIT_DIFF", "EXIT_USAGE", "SecretsError",
    "adopt_secrets", "add_parser", "build_parser", "cmd_adopt", "cmd_copy", "cmd_export", "cmd_mint",
    "cmd_secrets", "cmd_set", "cmd_verify", "copy_secret", "decrypt_stdin", "encrypt_stdin",
    "export_secrets",
    "list_external_secrets", "load_recipient_file", "main", "mint_secret", "read_secret",
    "resolve_recipient", "run_sops", "set_secret", "verify_secrets", "write_sops_yaml",
]


if __name__ == "__main__":
    sys.exit(main())
