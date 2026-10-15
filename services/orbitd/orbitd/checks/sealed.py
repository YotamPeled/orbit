"""Every secret in the repository is sealed, and nothing generated carries a credential.

The instance repository is plaintext to everyone who can read it. A secret belongs there
only as a SopsSecret whose every value is ciphertext; a plain Secret, or a sealed file
with one value left open, is a credential published. The generated manifests are held to
the same rule by the generator's own credential check.
"""

from __future__ import annotations

from typing import Any

import yaml

import os
import shutil
import subprocess

from . import SCRIPT_TIMEOUT_S, Context, one_sentence

NAME = "sealed"
WHAT = "every secret in the repository is sealed, and no generated manifest carries a credential"
SECRETS_REL = "clusters/platform/secrets"
VALUE_KEYS = ("stringData", "data")
GENERATED = ("clusters/apps/", "clusters/orbit/")


def open_value(doc: dict[str, Any]) -> str | None:
    """The first value of a SopsSecret that is not ciphertext, as a path; None when sealed."""
    templates = ((doc.get("spec") or {}).get("secretTemplates")) or []
    if not isinstance(templates, list) or not templates:
        return "spec.secretTemplates (there are none)"
    for index, template in enumerate(templates):
        if not isinstance(template, dict):
            return f"spec.secretTemplates[{index}]"
        for key in VALUE_KEYS:
            values = template.get(key)
            if values is None:
                continue
            if not isinstance(values, dict):
                return f"spec.secretTemplates[{index}].{key}"
            for name, value in values.items():
                if not (isinstance(value, str) and value.startswith("ENC[") and value.endswith("]")):
                    return f"spec.secretTemplates[{index}].{key}.{name}"
    return None


def check(ctx: Context) -> str | None:
    clusters = ctx.root / "clusters"
    files = sorted([*clusters.rglob("*.yaml"), *clusters.rglob("*.yml")]) if clusters.is_dir() else []
    for path in files:
        rel = path.relative_to(ctx.root).as_posix()
        under_secrets = rel.startswith(SECRETS_REL + "/")
        if path.is_symlink():
            return f"{rel} is a symlink"
        try:
            docs = [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if isinstance(d, dict)]
        except (yaml.YAMLError, UnicodeDecodeError):
            if under_secrets:
                return f"{rel} is not YAML: a sealed secret that cannot be read cannot be shown to be sealed"
            continue           # the scaffold and generated checks own a broken manifest
        for doc in docs:
            kind = doc.get("kind")
            # Where a hand can put one: the sealed-secrets directory and the generator's
            # output. (The scaffolder's own manifests are held byte for byte by the scaffold
            # check, and an upstream bundle among them may carry a Secret of settings.)
            if kind == "Secret" and (under_secrets or rel.startswith(GENERATED)):
                return (f"{rel} holds a plain Secret: a secret is committed only sealed "
                        f"(`orbit secrets set`), never as a Secret")
            if not under_secrets:
                continue
            if kind != "SopsSecret":
                return f"{rel} is under {SECRETS_REL}/ and is not a SopsSecret"
            if not isinstance(doc.get("sops"), dict) or not doc["sops"].get("mac"):
                return f"{rel} is a SopsSecret that was never sealed (it carries no sops block)"
            where = open_value(doc)
            if where is not None:
                return f"{rel} has a value that is not ciphertext at {where}: seal it with `orbit secrets set`"
    return declared_keys_missing(ctx, files) or credential_in_generated(ctx)


def sealed_key_names(ctx: Context, files: list[Any]) -> dict[tuple[str, str], tuple[str, set[str]]]:
    """(namespace, Secret name) -> (the sealed file, the key NAMES it seals). A sealed file's
    key names are clear text (only the values are ciphertext); nothing else is read."""
    out: dict[tuple[str, str], tuple[str, set[str]]] = {}
    for path in files:
        rel = path.relative_to(ctx.root).as_posix()
        if not rel.startswith(SECRETS_REL + "/"):
            continue
        try:
            docs = [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if isinstance(d, dict)]
        except (yaml.YAMLError, UnicodeDecodeError, OSError):
            continue
        for doc in docs:
            namespace = str((doc.get("metadata") or {}).get("namespace") or path.parent.name)
            for template in ((doc.get("spec") or {}).get("secretTemplates")) or []:
                if not isinstance(template, dict) or not template.get("name"):
                    continue
                names: set[str] = set()
                for key in VALUE_KEYS:
                    if isinstance(template.get(key), dict):
                        names.update(str(k) for k in template[key])
                out[(namespace, str(template["name"]))] = (rel, names)
    return out


def declared_keys_missing(ctx: Context, files: list[Any]) -> str | None:
    """A Secret a project declares WITH its keys (the bucket credential a production
    database archives with, a token a component reads) must be sealed under those key
    names. On the rebuilt test box an agent sealed `orbit-s3` for a new project under the
    key names the platform's own copy uses; every check passed, the database came up, and
    its write-ahead log could not be archived ("missing key HETZNER_S3_ACCESS_KEY, inside
    secret orbit-s3") until verify said so an hour later (docs/V1.md §8). A declared
    Secret that no file seals yet is not this check's finding: its workload says so."""
    apps = ctx.root / "clusters" / "apps"
    if not apps.is_dir():
        return None
    sealed = sealed_key_names(ctx, files)
    for path in sorted(apps.glob("*.yaml")):
        try:
            docs = [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if isinstance(d, dict)]
        except (yaml.YAMLError, UnicodeDecodeError, OSError):
            continue               # the generated check owns a manifest that does not parse
        for doc in docs:
            if doc.get("kind") != "Application":
                continue
            spec = doc.get("spec") or {}
            namespace = str((spec.get("destination") or {}).get("namespace") or "")
            values = (((spec.get("source") or {}).get("helm") or {}).get("valuesObject")) or {}
            for declared in values.get("secrets") or [] if isinstance(values, dict) else []:
                if not isinstance(declared, dict) or not declared.get("name"):
                    continue
                want = {str(k) for k in declared.get("keys") or [] if isinstance(k, str)}
                found = sealed.get((namespace, str(declared["name"])))
                if not want or found is None:
                    continue
                rel, have = found
                missing = sorted(want - have)
                if missing:
                    return (f"{rel} seals {namespace}/{declared['name']} without the key(s) {', '.join(missing)} "
                            f"that {namespace} declares for it (it has: {', '.join(sorted(have)) or 'none'}): "
                            f"write the file again with those key names and seal it (`sops --encrypt`, as the "
                            f"onboarding skill's Secrets section shows; `orbit secrets set` rotates a key that exists)")
    return None


def credential_in_generated(ctx: Context) -> str | None:
    """This build's credential scan (scripts/no-credentials.py) over the generated
    manifests the commit carries, run in a directory that holds nothing else."""
    apps = ctx.root / "clusters" / "apps"
    if not apps.is_dir() or not any(apps.glob("*.yaml")):
        return None            # no generated manifest at all: nothing to carry a credential
    assert ctx.scratch is not None
    box = ctx.scratch / "credentials"
    if box.exists():
        shutil.rmtree(box)
    (box / "scripts" / "verify").mkdir(parents=True)
    (box / "clusters").mkdir()
    shutil.copy2(ctx.core / "scripts" / "no-credentials.py", box / "scripts" / "no-credentials.py")
    shutil.copy2(ctx.core / "scripts" / "verify" / "shown.py", box / "scripts" / "verify" / "shown.py")
    shutil.copytree(apps, box / "clusters" / "apps", symlinks=True)
    env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": str(ctx.scratch),
           "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        proc = subprocess.run(["python3", "scripts/no-credentials.py"], cwd=box, env=env,
                              capture_output=True, text=True, timeout=SCRIPT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return "the credential scan of the generated manifests did not finish"
    if proc.returncode == 0:
        return None
    lines = [line for line in (proc.stdout + "\n" + proc.stderr).splitlines() if line.strip()]
    said = next((line for line in lines if line.startswith("credential in a generated manifest")), lines[-1] if lines else "")
    return f"a generated manifest carries a credential: {one_sentence(said)}"
