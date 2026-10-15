"""`orbit onboard run` -- the unattended half of onboarding (docs/V1.md M3).

The person fills ONE form (docs/V1.md §7) into a private file and makes the two GitHub App
clicks; this runs every other step of the runbook, in order, each one idempotent, using
`orbit onboard status` to decide what is already done. It is the agent's way to onboard
without ever seeing a value: the form's tokens move file to file -- into the credentials
directory, into sealed files, into a subprocess's environment -- and never onto a command
line, into the report, or onto stdout. The report names steps, files and key names only.

    orbit onboard run <instance-dir> --form <form.yaml> [--config-dir D] [--only STEP]

The form (YAML, mode 0600, outside every repository):

    domain: acme.example            # the Cloudflare zone; the instance's primary domain
    github: {owner: acme-org}       # org or user; the operator's `gh` must be its admin
    hetzner: {token: ..., server_type: cx43, location: fsn1}
    cloudflare: {token: ..., zone: acme.example}
    acme_email: ops@acme.example
    # optional: which optional features run (provisioning/features.yaml names them and
    # what each needs). One left out is off; no `features:` at all is the smallest
    # platform: core alone.
    features: {logs: true, dashboards: true, databases: true}
    # optional: needed by the features that need a bucket (logs, traces, backups)
    object_storage: {access_key: ..., secret_key: ..., location: fsn1}
    # optional: needed by the alerts-telegram feature
    telegram: {bot_token: ..., chat_id: "123"}
    # optional: instance: {name: acme}   (default: the domain's first label)
    # one of:     hetzner.server_id: 123 + hetzner.rebuild: true
    #               (adopt an existing server: its disk is wiped and reinstalled in place,
    #               same address, and what its earlier life left in DNS and Hetzner is
    #               taken over, not created again)
    #             hetzner.create_server: true  (buy a new one: it costs money)
"""

from __future__ import annotations

import os
import re
import secrets
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

FORM_MODE_MASK = 0o077
NAME_RE = re.compile(r"[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?")
DOMAIN_RE = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+")
LOCATION_RE = re.compile(r"[a-z]{3}[0-9]")
SERVER_TYPE_RE = re.compile(r"c[apcx]x?[0-9]{2,3}")
CHAT_ID_RE = re.compile(r"-?[0-9]{3,20}")
EMAIL_RE = re.compile(r"[^@\s]+@[a-z0-9.-]+\.[a-z]{2,}")
TOKEN_RE = re.compile(r"[A-Za-z0-9_:.\-/+=]{16,512}")


class FormError(ValueError):
    """A form field is missing or not of its shape. The message names the FIELD, never
    the value: a token pasted into the wrong field must not be echoed."""


@dataclass(frozen=True)
class Form:
    name: str
    domain: str
    github_owner: str
    hetzner_token: str = field(repr=False)
    server_type: str
    location: str
    cloudflare_token: str = field(repr=False)
    zone: str
    acme_email: str
    # object storage and Telegram are optional: only the features that need them do
    s3_access_key: str | None = field(default=None, repr=False)
    s3_secret_key: str | None = field(default=None, repr=False)
    s3_location: str | None = None
    telegram_bot_token: str | None = field(default=None, repr=False)
    telegram_chat_id: str | None = None
    # the optional features asked for; one left out is off (no entry at all: core alone)
    features: tuple[tuple[str, bool], ...] = ()
    server_id: str | None = None      # adopt this existing server (rebuilt in place)
    create_server: bool = False       # a new server costs money: only on an explicit yes
    rebuild: bool = False             # an adopted server's disk is wiped: only on an explicit yes

    @property
    def has_bucket(self) -> bool:
        return self.s3_access_key is not None

    @property
    def has_telegram(self) -> bool:
        return self.telegram_bot_token is not None

    @property
    def s3_endpoint(self) -> str:
        return f"https://{self.s3_location}.your-objectstorage.com"

    def wants(self, feature: str) -> bool:
        return dict(self.features).get(feature, False)


def _field(doc: dict[str, Any], path: str, rx: re.Pattern[str]) -> str:
    cur: Any = doc
    for part in path.split("."):
        cur = cur.get(part) if isinstance(cur, dict) else None
    if cur is None or str(cur).strip() == "":
        raise FormError(f"the form has no `{path}`")
    value = str(cur).strip()
    if not rx.fullmatch(value):
        raise FormError(f"the form's `{path}` is not of its shape (it is not repeated)")
    return value


def make_private(path: Path, mode: int = 0o600) -> bool:
    """Narrow a file (or directory) others can reach to its owner alone; True when it had
    to. The owner's credentials directory on a Windows machine reaches the desk as a mount
    where everything reads as mode 0777 until it is narrowed FROM INSIDE the desk: a chmod
    typed on the Windows side changes nothing the desk sees, so "chmod 600 it first" was an
    instruction nobody on that machine could follow (docs/V1.md §8). What cannot be narrowed
    (someone else's file, a read-only mount) stays as it is, and the caller judges it."""
    if os.name == "nt":
        return False
    try:
        if not path.stat().st_mode & FORM_MODE_MASK:
            return False
        os.chmod(path, mode)
        return not path.stat().st_mode & FORM_MODE_MASK
    except OSError:
        return False


def load_form(path: Path) -> Form:
    """Read and validate the form. A file others can read is made private first (it holds
    every token); one that cannot be made private is refused."""
    make_private(path)
    st = path.stat()
    if os.name != "nt" and st.st_mode & FORM_MODE_MASK:
        raise FormError(f"{path} is readable by others (mode {stat.S_IMODE(st.st_mode):o}) and could not be made "
                        f"private; chmod 600 it first (on a Windows machine through the desk: "
                        f"`scripts/desk.sh chmod 600 /root/.config/<name>-platform/{path.name}`)")
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        raise FormError(f"{path} is not YAML (the parser's message is not repeated: it can quote a value)") from None
    if not isinstance(doc, dict):
        raise FormError(f"{path} is not a mapping")
    domain = _field(doc, "domain", DOMAIN_RE)
    name = str((doc.get("instance") or {}).get("name") or domain.split(".")[0])
    if not NAME_RE.fullmatch(name):
        raise FormError("the instance name (`instance.name`, or the domain's first label) is not a short DNS label")
    zone = _field(doc, "cloudflare.zone", DOMAIN_RE)
    if domain != zone and not domain.endswith("." + zone):
        raise FormError("`domain` is not inside `cloudflare.zone`: the certificates are issued through that zone")
    return Form(
        name=name, domain=domain, zone=zone,
        github_owner=_field(doc, "github.owner", re.compile(r"[A-Za-z0-9]([A-Za-z0-9-]{0,38})")),
        hetzner_token=_field(doc, "hetzner.token", TOKEN_RE),
        server_type=_field(doc, "hetzner.server_type", SERVER_TYPE_RE),
        location=_field(doc, "hetzner.location", LOCATION_RE),
        cloudflare_token=_field(doc, "cloudflare.token", TOKEN_RE),
        acme_email=_field(doc, "acme_email", EMAIL_RE),
        # a block that is there must be whole; a block that is absent is a choice
        **({"s3_access_key": _field(doc, "object_storage.access_key", TOKEN_RE),
            "s3_secret_key": _field(doc, "object_storage.secret_key", TOKEN_RE),
            "s3_location": _field(doc, "object_storage.location", LOCATION_RE)}
           if doc.get("object_storage") is not None else {}),
        **({"telegram_bot_token": _field(doc, "telegram.bot_token", TOKEN_RE),
            "telegram_chat_id": _field(doc, "telegram.chat_id", CHAT_ID_RE)}
           if doc.get("telegram") is not None else {}),
        features=_features(doc),
        server_id=_optional_id(doc),
        create_server=bool((doc.get("hetzner") or {}).get("create_server") is True),
        rebuild=bool((doc.get("hetzner") or {}).get("rebuild") is True),
    )


FEATURE_RE = re.compile(r"[a-z][a-z0-9-]{0,39}")


def _features(doc: dict[str, Any]) -> tuple[tuple[str, bool], ...]:
    """The form's `features:` as (name, on) pairs. Names are checked against the registry
    later (check_features), once the instance's copy of it is at hand."""
    raw = doc.get("features")
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise FormError("the form's `features` is not a mapping of feature name to true or false")
    pairs = []
    for name, value in raw.items():
        if not isinstance(name, str) or not FEATURE_RE.fullmatch(name):
            raise FormError("the form's `features` has a key that is not a feature name (it is not repeated)")
        if not isinstance(value, bool):
            raise FormError(f"the form's `features.{name}` is not true or false")
        pairs.append((name, value))
    return tuple(pairs)


def check_features(form: Form, registry: dict[str, Any]) -> dict[str, bool]:
    """The optional features this form turns on and off, checked against the registry:
    every name is a feature, and every feature that is on has what it needs in the form.
    Refused here, before anything is written or bought, naming the form field to add."""
    from . import orbit_features as rules
    optional = [name for name, spec in registry.items() if not spec["always"]]
    for name, _ in form.features:
        if name not in optional:
            raise FormError(f"the form's `features.{name}` is not a feature of this core; the features are: "
                            + ", ".join(optional))
    want = {name: form.wants(name) for name in optional}
    for name in optional:
        if want[name] and "bucket" in registry[name]["requires"] and not form.has_bucket:
            raise FormError(f"the form turns `features.{name}` on and has no `object_storage`: {name} keeps its "
                            f"data in a bucket. Add object_storage (access_key, secret_key, location), or leave {name} off")
    if want.get("alerts-telegram") and not form.has_telegram:
        raise FormError("the form turns `features.alerts-telegram` on and has no `telegram`: add telegram "
                        "(bot_token, chat_id), or leave alerts-telegram off")
    config = {"features": dict(want)}
    if form.has_bucket:
        config["objectStorage"] = {"endpoint": form.s3_endpoint, "buckets": {"platform": f"{form.name}-orbit-platform"}}
    try:
        rules.states(config, registry)
    except rules.FeatureError as e:
        raise FormError(f"the form's `features` cannot be: {e}") from None
    return want


def _optional_id(doc: dict[str, Any]) -> str | None:
    raw = (doc.get("hetzner") or {}).get("server_id")
    if raw in (None, ""):
        return None
    if not re.fullmatch(r"[0-9]{1,12}", str(raw)):
        raise FormError("the form's `hetzner.server_id` is not a server id (digits)")
    return str(raw)


# ----------------------------------------------------------------------------- steps

def write_private(path: Path, text: str) -> None:
    """Create or replace a file readable by its owner alone, never via a world-readable
    temporary: the descriptor is opened 0600 before a byte is written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    if os.name != "nt":
        os.chmod(path, 0o600)


def _drop_block(text: str, key: str) -> str:
    """`text` without the top-level block `key:` (its own lines; the comments above it
    stay). Proven: the result parses to the old document minus that key, or nothing changes."""
    lines = text.split("\n")
    start = next((i for i, line in enumerate(lines) if line.startswith(f"{key}:")), None)
    if start is None:
        return text
    end = start + 1
    while end < len(lines) and (lines[end][:1] in (" ", "\t") or not lines[end].strip()):
        end += 1
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1                    # the blank line that separated it from what follows stays
    out = "\n".join(lines[:start] + lines[end:])
    before, after = yaml.safe_load(text) or {}, yaml.safe_load(out) or {}
    if after != {k: v for k, v in before.items() if k != key}:
        raise FormError(f"instance/config.yaml's `{key}` block is not written in a form this run can remove")
    return out


def core_registry_owner(instance_dir: Path) -> str | None:
    """Where this instance's core publishes its images: the container registry of the
    repository orbit-core.lock pins (github.com/<owner>/... -> ghcr.io/<owner>). None when
    the lock is not there or names another kind of host: the config then keeps its
    example value and `make init` asks for it by name."""
    try:
        text = (instance_dir / "orbit-core.lock").read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.search(r"^url:\s*https://github\.com/([A-Za-z0-9][A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]+?(?:\.git)?\s*$", text, re.M)
    return f"ghcr.io/{m.group(1).lower()}" if m else None


def fill_config(instance_dir: Path, form: Form, registry: dict[str, Any] | None = None) -> list[str]:
    """Write the form's non-secret answers into instance/config.yaml, in place, keeping
    every comment: each key is replaced only where it still holds the template's example
    value, so a re-run (or a hand edit) is never overwritten. Returns the keys written.

    With the features registry at hand it also declares the form's features (a
    `features:` block naming every optional feature, written once), drops the example
    `objectStorage` block when the form has no bucket, and turns `contacts.telegram` off
    when the form has no Telegram."""
    path = instance_dir / "instance" / "config.yaml"
    text = path.read_text(encoding="utf-8")
    owner = form.github_owner
    want = check_features(form, registry) if registry is not None else None
    core_owner = core_registry_owner(instance_dir)
    plan = [
        ("instance.name", r"(\n\s*name:\s*)example(\s)", rf"\g<1>{form.name}\2"),
        ("git.repoUrl", r"(repoUrl:\s*)git@github\.com:example-org/example-instance\.git",
         rf"\g<1>git@github.com:{owner}/{form.name}-platform.git"),
        ("git.github_org", r"(github_org:\s*)example-org", rf"\g<1>{owner}"),
        ("domains.primary", r"(primary:\s*)example\.org", rf"\g<1>{form.domain}"),
        ("domains.dev", r"(\n\s*dev:\s*)dev\.example\.org", rf"\g<1>dev.{form.domain}"),
        ("domains.dnsZones", r"(dnsZones:\s*)\[example\.org\]", rf"\g<1>[{form.zone}]"),
        ("registry", r"(\nregistry:\s*)ghcr\.io/example-org", rf"\g<1>ghcr.io/{owner.lower()}"),
        # orbitd and the desk are pulled from the registry of the core this instance pins
        *([("coreRegistry.owner", r"(\n\s*owner:\s*)ghcr\.io/example-core-org", rf"\g<1>{core_owner}")] if core_owner else []),
        ("nodes[0].name", r"(- name:\s*)org-node-1", rf"\g<1>{form.name}-node-1"),
        ("contacts.email", r"(email:\s*)ops@example\.org", rf"\g<1>{form.acme_email}"),
    ]
    if form.has_bucket:
        plan += [
            ("objectStorage.endpoint", r"(endpoint:\s*)https://fsn1\.your-objectstorage\.com", rf"\g<1>{form.s3_endpoint}"),
            ("objectStorage.buckets.platform", r"(platform:\s*)example-orbit-platform", rf"\g<1>{form.name}-orbit-platform"),
            ("objectStorage.buckets.appData", r"(appData:\s*)example-orbit-apps", rf"\g<1>{form.name}-orbit-apps"),
        ]
    if form.has_telegram:
        plan.append(("contacts.telegramChatId", r'(telegramChatId:\s*)"111111111"', rf'\g<1>"{form.telegram_chat_id}"'))
    written = []
    for key, rx, repl in plan:
        new, n = re.subn(rx, repl, text, count=1)
        if n and new != text:               # an example value equal to the form's is not a write
            text, _ = new, written.append(key)
    # Example blocks an instance born here does not use: no agent plane yet (its two
    # images), and the admin's display name, which nothing reads. `make init` refuses an
    # example value left in place and accepts a block that is absent.
    for block, marker in (("agentRuntime", r"image:\s*ghcr\.io/example-core-org/orbit-agent-runtime"),
                          ("intakeRuntime", r"image:\s*ghcr\.io/example-core-org/orbit-intake")):
        if re.search(marker, text):
            text = _drop_block(text, block)
            written.append(f"{block} (removed: no agent plane)")
    new, n = re.subn(r"\n[ \t]*#[^\n]*admin's display name[^\n]*(?=\n[ \t]*adminFirstName:)", "", text, count=1)
    new, a = re.subn(r"\n[ \t]*adminFirstName:[ \t]*Ada[ \t]*(?=\n)", "", new, count=1)
    new, b = re.subn(r"\n[ \t]*adminLastName:[ \t]*Admin[ \t]*(?=\n)", "", new, count=1)
    if a or b:
        text = new
        written.append("contacts.adminFirstName, contacts.adminLastName (removed: unused)")
    if not form.has_bucket and re.search(r"platform:\s*example-orbit-platform", text):
        # the example block, untouched: this instance has no bucket, so it has no block
        text = _drop_block(text, "objectStorage")
        written.append("objectStorage (removed: no bucket)")
    if not form.has_telegram:
        new, n = re.subn(r"(\n\s*telegram:\s*)true\b", r"\g<1>false", text, count=1)
        new, m = re.subn(r'\n\s*telegramChatId:\s*"111111111"[^\n]*', "", new, count=1)
        if n or m:
            text = new
            written.append("contacts.telegram (off: no Telegram)")
    if want is not None and "features" not in (yaml.safe_load(text) or {}):
        from . import orbit_features as rules
        try:
            text, _ = rules.declare(text, registry, want)
        except rules.FeatureError as e:
            raise FormError(f"the form's `features` cannot be written: {e}") from None
        written.append("features (" + (", ".join(n for n, on in want.items() if on) or "core alone") + ")")
    path.write_text(text, encoding="utf-8", newline="\n")
    return written


def make_credentials(config_dir: Path, form: Form, run: Any = subprocess.run) -> list[str]:
    """The credentials directory of runbook section 2, generated: the node's ssh key, the
    sops age key, the `env` file with the two cloud tokens, `ssh-key-path`. A file that
    already exists is kept (a re-run never rotates a key). Returns the files created."""
    created = []
    config_dir.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(config_dir, 0o700)
    key = config_dir / "node-key"
    if not key.exists():
        run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"{form.name}-node", "-f", str(key)],
            check=True, capture_output=True)
        created += ["node-key", "node-key.pub"]
    if not (config_dir / "ssh-key-path").exists():
        # The pointer is read on the operator machine (scripts/desk.sh mounts the key it
        # names). Run inside the desk, this directory is a mount: its path here
        # (/root/.config/...) names nothing out there, and every desk command after the
        # run then stopped at "the node ssh key named by ssh-key-path is not readable".
        # The desk says where the directory really is (ORBIT_HOST_CONFIG_DIR).
        host_dir = os.environ.get("ORBIT_HOST_CONFIG_DIR") or ""
        pointer = f"{host_dir.rstrip('/')}/node-key" if host_dir.startswith("/") else str(key)
        (config_dir / "ssh-key-path").write_text(pointer + "\n", encoding="utf-8")
        created.append("ssh-key-path")
    age = config_dir / "age.key"
    if not age.exists():
        out = run(["age-keygen"], check=True, capture_output=True, text=True).stdout
        write_private(age, out)
        created.append("age.key")
    env = config_dir / "env"
    if not env.exists():
        write_private(env, f"export HCLOUD_TOKEN={form.hetzner_token}\n"
                           f"export CLOUDFLARE_API_TOKEN={form.cloudflare_token}\n")
        created.append("env")
    return created


def private_key(config_dir: Path) -> str:
    """The node key at a path ssh will accept. A credentials directory mounted from a
    Windows machine shows every file as mode 0777, and ssh refuses a private key others can
    read ("UNPROTECTED PRIVATE KEY FILE"): the rebuilt server would never "accept the node
    key". The desk's entrypoint copies a key that exists when it starts; this run makes the
    key itself, so it takes the same copy (~/.ssh/node-key, 0600) when the file is not
    private where it lies. ORBIT_SSH_KEY, when set, is the key to use as it is."""
    given = os.environ.get("ORBIT_SSH_KEY")
    if given:
        return given
    key = config_dir / "node-key"
    try:
        loose = bool(key.stat().st_mode & 0o077)
    except OSError:
        return str(key)
    if not loose or os.name == "nt":
        return str(key)
    copy = Path.home() / ".ssh" / "node-key"
    copy.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_private(copy, key.read_text(encoding="utf-8"))
    return str(copy)


def age_recipient(config_dir: Path) -> str:
    """The public half of age.key (its `# public key:` line)."""
    for line in (config_dir / "age.key").read_text(encoding="utf-8").splitlines():
        if line.startswith("# public key: "):
            return line.split(": ", 1)[1].strip()
    raise FormError("age.key carries no `# public key:` line")


def random_password(n: int = 32) -> str:
    return secrets.token_urlsafe(n)[:n]


# ----------------------------------------------------------------------------- sealing

def seal(instance_dir: Path, namespace: str, name: str, data: dict[str, str], *,
         secret_type: str = "Opaque", replace: bool = False, sops_run: Any = None) -> bool:
    """Seal one SopsSecret file (clusters/platform/secrets/<namespace>/<name>.yaml) with the
    instance's age recipient. Values travel in memory and on sops's stdin only. An existing
    file is kept unless `replace` (a re-run never rotates). Returns whether it was written."""
    import base64

    from . import secrets as sx
    path = sx._file_path(instance_dir, namespace, name)
    if path.is_file() and not replace:
        return False
    recipient = sx.resolve_recipient(instance_dir, None)
    encoded = {k: base64.b64encode(v.encode("utf-8")).decode("ascii") for k, v in data.items()}
    plaintext = sx._dump(sx._plaintext_doc(namespace, name, secret_type, encoded))
    ciphertext = sx.encrypt_stdin(plaintext, recipient=recipient, relpath=sx._relpath(namespace, name),
                                  sops_run=sops_run or sx.run_sops)
    sx._write_ciphertext(path, ciphertext)
    return True


def htpasswd_apr1(user: str, password: str) -> str:
    """An htpasswd line Traefik's basic-auth accepts (Apache MD5, `$apr1$`), from openssl —
    the password on openssl's stdin, never its command line."""
    salt = secrets.token_hex(4)
    out = subprocess.run(["openssl", "passwd", "-apr1", "-salt", salt, "-stdin"], input=password + "\n",
                         capture_output=True, text=True, check=True).stdout.strip()
    return f"{user}:{out}"


def seal_platform_secrets(instance_dir: Path, config_dir: Path, form: Form, *, ghcr_token: str,
                          app: dict[str, str] | None, sops_run: Any = None) -> list[str]:
    """Every sealed Secret the platform tree refers to, from the form, the operator's
    registry token, the GitHub App's credentials and freshly generated values. Generated
    passwords are kept in the credentials directory (0600) before they are sealed, so the
    Recovery Kit can carry them. Returns the files written (namespace/name)."""
    import base64
    import json

    def kept(file: str, make: Any) -> str:
        p = config_dir / file
        if not p.exists():
            write_private(p, make() + "\n")
        return p.read_text(encoding="utf-8").strip()

    dev_pw = kept("dev-basic-auth-password", lambda: random_password(24))
    webhook = kept("orbit-alert-webhook-token", lambda: random_password(48))
    # Alertmanager mounts this whether or not an agent ever declares a trigger: without the
    # Secret its pod does not start. The agent plane's intake verifies the same value.
    intake = kept("alertmanager-intake-token", lambda: random_password(48))
    dockercfg = json.dumps({"auths": {"ghcr.io": {"auth": base64.b64encode(
        f"{form.github_owner}:{ghcr_token}".encode()).decode()}}})
    # What every platform has...
    plan: list[tuple[str, str, dict[str, str], str]] = [
        ("cert-manager", "cloudflare-api-token", {"api-token": form.cloudflare_token}, "Opaque"),
        ("monitoring", "orbit-alert-webhook", {"token": webhook}, "Opaque"),
        ("monitoring", "alertmanager-intake", {"token": intake}, "Opaque"),
        ("orbit", "orbit-alert-webhook", {"token": webhook}, "Opaque"),
        ("orbit", "ghcr-pull", {".dockerconfigjson": dockercfg}, "kubernetes.io/dockerconfigjson"),
        # not used in `orbit` itself: the copy a project's non-prod namespace takes (`orbit
        # secrets copy`), so the dev door's users line never has to be made by hand
        ("orbit", "dev-basic-auth", {"users": htpasswd_apr1("orbit", dev_pw)}, "Opaque"),
        ("orbit", "orbitd-registry", {"ghcr-pull-token": ghcr_token}, "Opaque"),
    ]
    # ...and what a feature the form turned on needs (provisioning/features.yaml `secrets`).
    # A generated password is made only for a feature that is on: the Recovery Kit carries
    # what exists.
    if form.wants("alerts-telegram") and form.has_telegram:
        plan.append(("monitoring", "alertmanager-telegram", {"bot-token": str(form.telegram_bot_token)}, "Opaque"))
    if form.wants("dashboards"):
        grafana_pw = kept("grafana-admin-password", lambda: random_password(32))
        renderer = kept("grafana-image-renderer-token", lambda: random_password(48))
        plan += [("monitoring", "grafana-admin", {"admin-user": "admin", "admin-password": grafana_pw}, "Opaque"),
                 ("monitoring", "grafana-image-renderer-token", {"token": renderer}, "Opaque")]
    if form.has_bucket:
        access, secret = str(form.s3_access_key), str(form.s3_secret_key)
        if form.wants("logs"):
            plan.append(("loki", "orbit-s3", {"access-key": access, "secret-key": secret}, "Opaque"))
        if form.wants("traces"):
            plan.append(("tempo", "orbit-s3", {"S3_ACCESS_KEY": access, "S3_SECRET_KEY": secret}, "Opaque"))
        # the backup Jobs orbitd starts copy each database dump to the bucket with these
        plan.append(("orbit", "orbit-s3", {"access-key": access, "secret-key": secret}, "Opaque"))
    if app:
        pem = app["pem"]
        plan.append(("orbit", "orbitd-github-app", {
            "app-id": app["id"], "github-app-id": app["id"],
            "installation-id": app["installation_id"], "github-app-installation-id": app["installation_id"],
            "private-key.pem": pem, "github-app-key.pem": pem}, "Opaque"))
    written = []
    for ns, name, data, typ in plan:
        if seal(instance_dir, ns, name, data, secret_type=typ, sops_run=sops_run):
            written.append(f"{ns}/{name}")
    return written


def dev_basic_auth_users(config_dir: Path) -> str:
    """The htpasswd `users` line for the dev doors (user `orbit`), from the password the
    run generated; a project's dev namespace gets it sealed as `dev-basic-auth`."""
    return htpasswd_apr1("orbit", (config_dir / "dev-basic-auth-password").read_text(encoding="utf-8").strip())


# ----------------------------------------------------------------------------- GitHub App

APP_FILE = "github-app.json"
APP_PERMISSIONS = {"contents": "write", "metadata": "read", "pull_requests": "write"}


def app_manifest(form: Form, port: int, suffix: str) -> dict[str, Any]:
    """The manifest GitHub turns into the instance's App: private, the permissions orbitd
    uses (commit tags and monitors, open pull requests, read the repo), no webhook, and both
    redirects back to this machine so the two clicks need nothing typed."""
    return {
        "name": f"{form.name}-orbit-{suffix}",
        "url": f"https://orbit.{form.domain}",
        "redirect_url": f"http://127.0.0.1:{port}/created",
        "setup_url": f"http://127.0.0.1:{port}/installed",
        "public": False,
        "default_permissions": APP_PERMISSIONS,
        "default_events": [],
        "hook_attributes": {"url": f"https://orbit.{form.domain}/v1/github", "active": False},
    }


def github_app_flow(config_dir: Path, form: Form, *, owner_is_org: bool, owner_id: int, repo_id: int,
                    port: int = 8765, bind: str = "0.0.0.0", timeout_s: float = 1800,
                    convert: Any = None, announce: Any = print) -> dict[str, str]:
    """The GitHub App, by the manifest flow: two clicks and nothing typed.

    Serves http://127.0.0.1:<port>/ (the desk publishes the port). Its page posts the
    manifest to GitHub; click one ("Create GitHub App") sends GitHub back to /created with
    a one-time code, exchanged here for the App's id and private key; the browser is sent
    on to the App's install page with the instance repository preselected; click two
    ("Install") sends it back to /installed with the installation id. The credentials are
    written to <config dir>/github-app.json (0600) and returned; nothing is printed but the
    local URL. A finished flow (the file complete) is not run again."""
    import html
    import json
    import threading
    import urllib.parse
    import urllib.request
    from http.server import BaseHTTPRequestHandler, HTTPServer

    path = config_dir / APP_FILE
    if path.exists():
        got = json.loads(path.read_text(encoding="utf-8"))
        if got.get("id") and got.get("pem") and got.get("installation_id"):
            return got
    state = secrets.token_urlsafe(24)
    manifest = app_manifest(form, port, secrets.token_hex(2))
    target = (f"https://github.com/organizations/{form.github_owner}/settings/apps/new" if owner_is_org
              else "https://github.com/settings/apps/new") + "?state=" + urllib.parse.quote(state)

    def default_convert(code: str) -> dict[str, Any]:
        req = urllib.request.Request(f"https://api.github.com/app-manifests/{urllib.parse.quote(code)}/conversions",
                                     method="POST", headers={"Accept": "application/vnd.github+json"})
        return json.load(urllib.request.urlopen(req, timeout=60))

    convert = convert or default_convert
    result: dict[str, str] = {}
    done = threading.Event()

    class Flow(BaseHTTPRequestHandler):
        def _page(self, code: int, body: str, location: str | None = None) -> None:
            self.send_response(code)
            if location:
                self.send_header("Location", location)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))

        def do_GET(self) -> None:  # noqa: N802 - http.server's name
            url = urllib.parse.urlparse(self.path)
            q = dict(urllib.parse.parse_qsl(url.query))
            if url.path == "/":
                self._page(200, f"""<!doctype html><meta charset="utf-8"><title>Orbit: create the GitHub App</title>
<form method="post" action="{html.escape(target)}"><input type="hidden" name="manifest" value="{html.escape(json.dumps(manifest))}">
<p>Orbit needs one GitHub App for {html.escape(form.github_owner)}. Click, then click <b>Create GitHub App</b> on GitHub.</p>
<button type="submit">Continue to GitHub</button></form>""")
            elif url.path == "/created" and q.get("state") == state and q.get("code"):
                app = convert(q["code"])
                result.update({"id": str(app["id"]), "slug": str(app["slug"]), "pem": str(app["pem"]),
                               "client_id": str(app.get("client_id", "")), "client_secret": str(app.get("client_secret", ""))})
                write_private(path, json.dumps(result) + "\n")
                install = (f"https://github.com/apps/{urllib.parse.quote(result['slug'])}/installations/new/permissions"
                           f"?suggested_target_id={owner_id}&repository_ids[]={repo_id}")
                self._page(302, "", location=install)
            elif url.path == "/installed" and q.get("installation_id", "").isdigit() and result:
                result["installation_id"] = q["installation_id"]
                write_private(path, json.dumps(result) + "\n")
                self._page(200, "<!doctype html><meta charset=utf-8><p>Done. The App is installed; return to the terminal.</p>")
                done.set()
            else:
                self._page(400, "<!doctype html><meta charset=utf-8><p>Unexpected request.</p>")

        def log_message(self, *args: Any) -> None:  # the query carries a one-time code
            pass

    server = HTTPServer((bind, port), Flow)
    from .audit import guarded_thread
    thread = guarded_thread(target=server.serve_forever, name="github-app-flow")
    thread.start()
    announce(f"Open http://127.0.0.1:{port}/ in a browser on this machine: two clicks on GitHub create and install the App.")
    try:
        if not done.wait(timeout_s):
            raise FormError(f"the GitHub App flow did not finish within {int(timeout_s)} s (re-run to start again)")
    finally:
        server.shutdown()
        server.server_close()
    return result


# ----------------------------------------------------------------------------- GitHub, git

GIT_CRED_HELPER = "!f(){ echo username=x-access-token; echo \"password=$GH_TOKEN\"; }; f"


def github_api(method: str, path: str, body: dict[str, Any] | None = None, *, token: str | None = None) -> Any:
    """One GitHub REST call with the operator's token (GH_TOKEN, from the desk). A refusal
    names the call and the status, never the response body."""
    import json
    import urllib.error
    import urllib.request
    token = token or os.environ.get("GH_TOKEN", "")
    req = urllib.request.Request("https://api.github.com" + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}",
                                          "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raise FormError(f"GitHub {method} {path.split('?')[0]} answered {e.code}") from None


def git(instance_dir: Path, *args: str, run: Any = subprocess.run) -> subprocess.CompletedProcess:
    """git in the instance checkout, authenticating with GH_TOKEN through a credential
    helper that reads the variable itself: the token is never an argument."""
    return run(["git", "-C", str(instance_dir), "-c", f"credential.helper={GIT_CRED_HELPER}", *args],
               capture_output=True, text=True)


def ensure_repo(instance_dir: Path, form: Form, *, api: Any = github_api, run: Any = subprocess.run) -> dict[str, Any]:
    """The instance repository on GitHub (private, `<owner>/<name>-platform`), created if
    missing and set as `origin`; returns its API record (its id is the App's install target)."""
    name = f"{form.name}-platform"
    try:
        repo = api("GET", f"/repos/{form.github_owner}/{name}")
    except FormError:
        owner = api("GET", f"/users/{form.github_owner}")
        create = f"/orgs/{form.github_owner}/repos" if owner.get("type") == "Organization" else "/user/repos"
        repo = api("POST", create, {"name": name, "private": True, "description": f"Orbit instance for {form.domain}"})
    if git(instance_dir, "remote", "get-url", "origin", run=run).returncode != 0:
        git(instance_dir, "remote", "add", "origin", repo["clone_url"], run=run)
    return repo


_REMOTE_RE = re.compile(r"github\.com[:/]([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?\s*$")


def _through_a_pull_request(instance_dir: Path, message: str, *, run: Any, api: Any) -> None:
    """main takes changes through pull requests only (a rule the organization put on its
    repository): the commit goes up as a branch, a pull request carries it into main, and
    the checkout follows. The rule is kept, not worked around: this is the path it asks for.
    A rule that also wants a review stops here in one sentence."""
    url = git(instance_dir, "remote", "get-url", "origin", run=run).stdout or ""
    m = _REMOTE_RE.search(url.strip())
    if not m:
        raise FormError("git push to the instance repository failed (main takes pull requests only, and origin is not a GitHub repository this run can open one on)")
    repo = f"{m.group(1)}/{m.group(2)}"
    sha = (git(instance_dir, "rev-parse", "--short=12", "HEAD", run=run).stdout or "").strip()
    if not re.fullmatch(r"[0-9a-f]{7,40}", sha):
        raise FormError("git push to the instance repository failed")
    branch = f"orbit-onboard/{sha}"
    if git(instance_dir, "push", "-q", "origin", f"HEAD:refs/heads/{branch}", run=run).returncode != 0:
        raise FormError(f"main of {repo} takes pull requests only, and the branch for one could not be pushed")
    pr = api("POST", f"/repos/{repo}/pulls", {"title": message, "head": branch, "base": "main",
                                               "body": "Made by `orbit onboard run`: main takes changes through pull requests."})
    try:
        api("PUT", f"/repos/{repo}/pulls/{int(pr['number'])}/merge", {"merge_method": "merge"})
    except FormError:
        raise FormError(f"main of {repo} takes pull requests only, and pull request #{pr['number']} could not be merged "
                        f"(a rule there asks for more than a pull request: a review or a check). Merge it, then run again") from None
    try:
        api("DELETE", f"/repos/{repo}/git/refs/heads/{branch}")
    except FormError:
        pass                                    # the branch staying is no harm
    git(instance_dir, "fetch", "-q", "origin", "main", run=run)
    if git(instance_dir, "merge", "-q", "--ff-only", "FETCH_HEAD", run=run).returncode != 0:
        raise FormError(f"pull request #{pr['number']} is merged into main of {repo}, and the checkout could not follow it (git pull, then run again)")


ACCEPTANCE_SETTLE_S = 300
PLATFORM_APPLY_TRIES = 5
PLATFORM_APPLY_GAP_S = 60
PLATFORM_APPLY_LOG = "platform-apply.log"


def apply_until_it_holds(apply: Any, out: Any, config_dir: Path, *, sleep: Any = None,
                         tries: int = PLATFORM_APPLY_TRIES, gap_s: int = PLATFORM_APPLY_GAP_S) -> int:
    """`orbit platform apply` on a cluster that is minutes old, until one pass applies
    everything. Apps that own admission webhooks (cert-manager, then the Prometheus
    operator, which waits for a certificate cert-manager issues) refuse the objects of the
    next app until they are serving, and the apply stops an app at its first refused
    object: the first pass on a new cluster can fail, and so can the second. The apply is
    idempotent, so it is run again, `gap_s` apart; M4's eighth run stopped here twice and
    a third pass by hand applied everything (docs/V1.md §8). The output of the last pass
    is kept privately beside the credentials (object names and reasons, no value), so a
    stop can be read. Returns the number of passes it took."""
    import time
    proc = None
    for attempt in range(1, tries + 1):
        proc = apply()
        try:
            write_private(config_dir / PLATFORM_APPLY_LOG, (proc.stdout or "") + (("\n--- stderr ---\n" + proc.stderr) if proc.stderr else ""))
        except OSError:
            pass
        if proc.returncode == 0:
            return attempt
        if attempt < tries:
            out(f"platform: pass {attempt} did not apply everything (a new cluster's admission webhooks are still coming up); again in {gap_s} s")
            (sleep or time.sleep)(gap_s)
    raise FormError(f"`orbit platform apply` failed {tries} times (exit {proc.returncode}); the last pass's output is in "
                    f"{PLATFORM_APPLY_LOG} in the credentials directory (search it for \"reason\")")


def accept_with_patience(verify: Any, out: Any, *, sleep: Any = None, settle_s: int = ACCEPTANCE_SETTLE_S) -> str:
    """The acceptance of a platform that is minutes old. It is still settling then
    (certificates being issued, the first scrapes, operators reconciling): rows that are
    green ten minutes later were red in M4's first acceptance (docs/V1.md §8). The suite
    is not touched: it is run again, up to three times, `settle_s` apart, and only a run
    with every row green passes. `verify()` is one run: (exit code or None, FAIL lines)."""
    import time
    fails = 0
    for attempt in (1, 2, 3):
        rc, fails = verify()
        if rc is None:
            raise FormError("make is not installed here: run the step from the desk")
        if rc == 0:
            return "make verify: every stage passed" + ("" if attempt == 1 else f" (on run {attempt}; the platform was still settling before)")
        if attempt < 3:
            out(f"acceptance: {fails} row(s) not passing on run {attempt}; a platform minutes old is still settling, again in {settle_s // 60} minutes")
            (sleep or time.sleep)(settle_s)
    raise FormError(f"make verify did not pass ({fails} FAIL line(s), three runs); the rows of the last run are in "
                    f"acceptance.log in the credentials directory (`grep FAIL`)")

PUSH_BLOCKING_RULES = frozenset({"pull_request", "update", "required_status_checks"})


def let_the_app_write(repo: str, app_id: int, *, api: Any = None) -> list[str]:
    """Orbit writes to main of the instance repository as its GitHub App (the catalog, the
    features, every deploy). A repository rule that keeps direct pushes off main keeps the
    App off too, unless the App is on the rule's bypass list: the platform would come up
    and then fail at its first write. For each active branch ruleset of the repository that
    covers main and blocks a push, the App is added to the bypass list (nothing else in the
    rule changes, nobody is removed). Returns the names of the rulesets it changed. A rule
    that lives on the organization is not this run's to change: one sentence says so."""
    api = api or github_api
    try:
        listed = api("GET", f"/repos/{repo}/rulesets?includes_parents=true")
    except FormError:
        return []                               # no rulesets on this plan, or none to read: nothing to do
    changed = []
    for item in listed if isinstance(listed, list) else []:
        rs = api("GET", f"/repos/{repo}/rulesets/{int(item['id'])}?includes_parents=true")
        if rs.get("target") != "branch" or rs.get("enforcement") != "active":
            continue
        include = set(((rs.get("conditions") or {}).get("ref_name") or {}).get("include") or [])
        if not include & {"refs/heads/main", "~DEFAULT_BRANCH", "~ALL"}:
            continue
        if not PUSH_BLOCKING_RULES & {r.get("type") for r in rs.get("rules") or []}:
            continue
        actors = list(rs.get("bypass_actors") or [])
        if any(a.get("actor_type") == "Integration" and a.get("actor_id") == app_id for a in actors):
            continue
        if rs.get("source_type") != "Repository":
            raise FormError(f"the ruleset \"{rs.get('name')}\" on the organization keeps direct pushes off main of {repo}, "
                            f"and Orbit writes there as its GitHub App: add the App to that ruleset's bypass list "
                            f"(organization settings, Rules), then run again")
        actors.append({"actor_id": app_id, "actor_type": "Integration", "bypass_mode": "always"})
        api("PUT", f"/repos/{repo}/rulesets/{int(rs['id'])}",
            {"name": rs["name"], "target": rs["target"], "enforcement": rs["enforcement"],
             "conditions": rs.get("conditions") or {}, "rules": rs.get("rules") or [], "bypass_actors": actors})
        changed.append(str(rs["name"]))
    return changed


def commit_and_push(instance_dir: Path, message: str, run: Any = subprocess.run, api: Any = None) -> bool:
    """Commit every change in the checkout as one commit on main and push it; where main
    takes pull requests only, through one. Returns whether there was anything to commit."""
    git(instance_dir, "add", "-A", run=run)
    if git(instance_dir, "diff", "--cached", "--quiet", run=run).returncode == 0:
        return False
    # A checkout made a minute ago on a machine (or in a desk container) with no git identity
    # cannot commit at all; the run then commits as itself rather than stopping on it.
    who = [] if git(instance_dir, "config", "user.email", run=run).returncode == 0 else         ["-c", "user.name=orbit onboard", "-c", "user.email=orbit-onboard@invalid"]
    proc = git(instance_dir, *who, "commit", "-q", "-m", message, run=run)
    if proc.returncode != 0:
        raise FormError("git commit failed in the instance checkout")
    pushed = git(instance_dir, "push", "-q", "-u", "origin", "HEAD:main", run=run)
    if pushed.returncode != 0:
        # GitHub's refusal for a rule ("GH013 ... Changes must be made through a pull request")
        if "pull request" not in (pushed.stderr or "").lower():
            raise FormError("git push to the instance repository failed")
        _through_a_pull_request(instance_dir, message, run=run, api=api or github_api)
    return True


# ----------------------------------------------------------------------------- infrastructure

PLACEHOLDER_IP = "203.0.113.10"


def _cloud_env(config_dir: Path) -> dict[str, str]:
    """The env file's two tokens as a subprocess environment — never as arguments."""
    env = dict(os.environ)
    for line in (config_dir / "env").read_text(encoding="utf-8").splitlines():
        m = re.fullmatch(r"\s*export\s+([A-Z_]+)=(.*)", line)
        if m:
            env[m.group(1)] = m.group(2).strip().strip('"')
    return env


def write_tfvars(instance_dir: Path, config_dir: Path, form: Form, *, server_id: str | None, zone_id: str) -> Path:
    """tofu/instance.auto.tfvars from the form, and tofu/import.tf when an existing server
    is adopted (the test box is rebuilt in place: same server, same address, no new spend,
    no DNS change). An existing tfvars is kept."""
    tofu = instance_dir / "tofu"
    tfvars = tofu / "instance.auto.tfvars"
    if not tfvars.exists():
        tofu.mkdir(parents=True, exist_ok=True)
        tfvars.write_text(
            "# Written by `orbit onboard run` from the onboarding form.\n"
            f'server_id            = "{server_id or ""}"\n'
            f'server_name          = "{form.name}-node-1"\n'
            f'ssh_key_name         = "{form.name}-operator"\n'
            f'ssh_public_key_path  = "{config_dir / "node-key.pub"}"\n'
            f'ssh_private_key_path = "{config_dir / "node-key"}"\n'
            "enable_backups       = false\n"
            f'zone_ids = {{\n  "{form.zone}" = "{zone_id}"\n}}\n', encoding="utf-8")
    if server_id and not (tofu / "import.tf").exists():
        (tofu / "import.tf").write_text(
            "# The node is an existing server, adopted (written by `orbit onboard run`).\n"
            "import {\n  to = hcloud_server.prod\n  id = var.server_id\n}\n", encoding="utf-8")
    return tfvars


def cloudflare_zone_id(form: Form) -> str:
    import json
    import urllib.parse
    import urllib.request
    req = urllib.request.Request("https://api.cloudflare.com/client/v4/zones?name=" + urllib.parse.quote(form.zone),
                                 headers={"Authorization": f"Bearer {form.cloudflare_token}"})
    try:
        zones = json.load(urllib.request.urlopen(req, timeout=60)).get("result") or []
    except Exception:
        raise FormError("Cloudflare did not answer the zone lookup with the form's token") from None
    if len(zones) != 1:
        raise FormError(f"Cloudflare holds {len(zones)} zone(s) named {form.zone} for that token, not one")
    return str(zones[0]["id"])


NODE_IMAGE = "ubuntu-26.04"     # tofu/server.tf's image: a rebuild lands where a create would
REBUILT_MARKER = "rebuilt-server-{id}"


def _api(base: str, token: str, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    """One JSON call with a bearer token in a header (never in the URL, never printed).
    A failure names the method and path only: an API's error text can quote the request."""
    import json
    import urllib.request
    req = urllib.request.Request(base + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
    except Exception as e:  # urllib.error.HTTPError carries the response body: not repeated
        code = getattr(e, "code", None)
        raise FormError(f"{method} {path} failed" + (f" (HTTP {code})" if code else "")) from None
    return json.loads(raw) if raw else {}


def hetzner_api(form: Form, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    return _api("https://api.hetzner.cloud/v1", form.hetzner_token, method, path, body)


def cloudflare_api(form: Form, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    return _api("https://api.cloudflare.com/client/v4", form.cloudflare_token, method, path, body)


def adopt_imports(instance_dir: Path, form: Form, *, zone_id: str, hz: Any = None, cf: Any = None) -> list[str]:
    """tofu/adopt.tf: an `import` block for each resource tofu/ declares that already exists
    (an adopted node's earlier life left its DNS wildcards, firewall and ssh key behind, and
    no tofu state). Without them `tofu apply` tries to create each again and fails; with
    them the apply takes them over and changes nothing in DNS. Returns the addresses
    imported. Looked up by the names tofu/ gives them, so a re-run finds the same ones."""
    import urllib.parse
    hz = hz or (lambda m, p, b=None: hetzner_api(form, m, p, b))
    cf = cf or (lambda m, p, b=None: cloudflare_api(form, m, p, b))
    cfg = yaml.safe_load((instance_dir / "instance" / "config.yaml").read_text(encoding="utf-8")) or {}
    name = ((cfg.get("instance") or {}).get("name")) or form.name
    domains = cfg.get("domains") or {}
    blocks: list[tuple[str, str]] = []
    for addr, host in (("cloudflare_dns_record.wildcard_primary", domains.get("primary")),
                       ("cloudflare_dns_record.wildcard_dev", domains.get("dev"))):
        if not host:
            continue
        found = cf("GET", f"/zones/{zone_id}/dns_records?type=A&name={urllib.parse.quote('*.' + host)}").get("result") or []
        if len(found) > 1:
            raise FormError(f"Cloudflare holds {len(found)} A records for *.{host}; tofu manages one")
        if found:
            blocks.append((addr, f"{zone_id}/{found[0]['id']}"))
    fws = hz("GET", f"/firewalls?name={urllib.parse.quote(name + '-platform-prod')}").get("firewalls") or []
    if fws:
        blocks.append(("hcloud_firewall.prod", str(fws[0]["id"])))
        blocks.append(("hcloud_firewall_attachment.prod", str(fws[0]["id"])))
    keys = hz("GET", f"/ssh_keys?name={urllib.parse.quote(name + '-operator')}").get("ssh_keys") or []
    if keys:
        blocks.append(("hcloud_ssh_key.operator", str(keys[0]["id"])))
    path = instance_dir / "tofu" / "adopt.tf"
    if blocks:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Resources the adopted node's earlier life left behind, taken over rather than\n"
                        "# created again (written by `orbit onboard run`).\n"
                        + "".join(f'import {{\n  to = {a}\n  id = "{i}"\n}}\n' for a, i in blocks), encoding="utf-8")
    return [a for a, _ in blocks]


def rebuild_user_data(pub: str) -> str:
    """cloud-init for a rebuilt server: the run's node key, and a root account that key can
    use. A rebuild has no ssh key of the provider's attached, so the provider sets a root
    password and marks it expired ("you are required to change your password immediately"):
    the key is accepted and then every command without a terminal is refused, which read
    here as "did not accept the node key" until the wait ran out (docs/V1.md §8). Nobody
    signs in with that password (the node step turns password sign-in off), so it is never
    marked expired in the first place (`chpasswd.expire`), and marked changed today at the
    end of the first boot in case the provider's own data marked it after all (`chage`)."""
    return ("#cloud-config\n"
            "ssh_authorized_keys:\n"
            f"  - {pub}\n"
            "chpasswd:\n"
            "  expire: false\n"
            "runcmd:\n"
            # this list REPLACES the provider's own (read on the rebuilt server: one command,
            # which makes attached volumes appear); it is kept as the first entry
            "  - udevadm trigger -c add -s block -p ID_VENDOR=HC --verbose -p ID_MODEL=Volume\n"
            "  - chage -d \"$(date +%F)\" -M -1 root\n")


def rebuild_server(config_dir: Path, form: Form, server_id: str, *, hz: Any = None, run: Any = subprocess.run,
                   sleep: Any = None, timeout_s: float = 900) -> bool:
    """Wipe the adopted server and reinstall it from NODE_IMAGE (Hetzner's rebuild: same
    server, same address, no spend), with cloud-init authorizing the run's own node key so
    the node step can reach it. Done once: a marker in the credentials directory makes a
    resumed run skip it (a second rebuild would destroy what the first run built). Returns
    whether it rebuilt now."""
    import time
    marker = config_dir / REBUILT_MARKER.format(id=server_id)
    if marker.exists():
        return False
    hz = hz or (lambda m, p, b=None: hetzner_api(form, m, p, b))
    sleep = sleep or time.sleep
    pub = (config_dir / "node-key.pub").read_text(encoding="utf-8").strip()
    action = hz("POST", f"/servers/{server_id}/actions/rebuild", {"image": NODE_IMAGE, "user_data": rebuild_user_data(pub)})["action"]
    deadline = time.monotonic() + timeout_s
    while action.get("status") == "running":
        if time.monotonic() > deadline:
            raise FormError(f"the rebuild of server {server_id} was still running after {int(timeout_s)} s")
        sleep(10)
        action = hz("GET", f"/actions/{action['id']}")["action"]
    if action.get("status") != "success":
        raise FormError(f"Hetzner reported the rebuild of server {server_id} as {action.get('status')}")
    ip = hz("GET", f"/servers/{server_id}")["server"]["public_net"]["ipv4"]["ip"]
    probe = ["ssh", "-i", private_key(config_dir), "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
             "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", f"root@{ip}", "true"]
    while run(probe, capture_output=True, text=True).returncode != 0:
        if time.monotonic() > deadline:
            raise FormError(f"the rebuilt server at {ip} did not accept the node key within {int(timeout_s)} s")
        sleep(10)
    write_private(marker, f"rebuilt from {NODE_IMAGE}\n")
    return True


def tofu_apply(instance_dir: Path, config_dir: Path, run: Any = subprocess.run, sleep: Any = None) -> str:
    """`tofu init` + `tofu apply` with the cloud tokens in the environment; returns the
    server's address (`tofu output server_ipv4`). `init` only asks the provider registry
    and changes nothing: it is tried three times, because one dropped connection on the
    operator machine's link stopped a resumed run here (docs/V1.md §8). `apply` is tried once."""
    import time
    env = _cloud_env(config_dir)
    init = ["tofu", "-chdir=tofu", "init", "-input=false"]
    for attempt in (1, 2, 3):
        proc = run(init, cwd=str(instance_dir), env=env, capture_output=True, text=True)
        if proc.returncode == 0:
            break
        if attempt < 3:
            (sleep or time.sleep)(20)
    else:
        raise FormError(f"`tofu -chdir=tofu init` failed three times (exit {proc.returncode}); "
                        f"run `scripts/desk.sh tofu -chdir=tofu init` to read why (it prints no secret)")
    for argv in (["tofu", "-chdir=tofu", "apply", "-input=false", "-auto-approve"],):
        proc = run(argv, cwd=str(instance_dir), env=env, capture_output=True, text=True)
        if proc.returncode != 0:
            raise FormError(f"`{' '.join(argv[:3])}` failed (exit {proc.returncode}); its output may name a value and is not repeated here")
    out = run(["tofu", "-chdir=tofu", "output", "-raw", "server_ipv4"], cwd=str(instance_dir), env=env,
              capture_output=True, text=True).stdout.strip()
    import ipaddress
    try:
        ipaddress.IPv4Address(out)
    except ValueError:
        raise FormError("tofu did not output the server's IPv4 address") from None
    return out


def write_ip(instance_dir: Path, ip: str) -> bool:
    """nodes[0].ip into instance/config.yaml — the step a person used to copy by hand. Only
    the placeholder is replaced; a different real address already there is a refusal."""
    path = instance_dir / "instance" / "config.yaml"
    text = path.read_text(encoding="utf-8")
    if f"ip: {ip}" in text:
        return False
    new, n = re.subn(rf"(\n\s*ip:\s*){re.escape(PLACEHOLDER_IP)}", rf"\g<1>{ip}", text, count=1)
    if not n:
        raise FormError("instance/config.yaml's nodes[0].ip is neither the placeholder nor the server's address")
    path.write_text(new, encoding="utf-8", newline="\n")
    return True


def create_buckets(form: Form, names: list[str], run: Any = subprocess.run) -> list[str]:
    """The platform buckets, through the S3 API (SigV4, curl), the keys on curl's stdin as a
    config document, never its command line. An existing bucket of ours is fine."""
    made = []
    for name in names:
        cfg = f'user = "{form.s3_access_key}:{form.s3_secret_key}"\n'
        proc = run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-X", "PUT", "-K", "-",
                    "--aws-sigv4", f"aws:amz:{form.s3_location}:s3", f"{form.s3_endpoint}/{name}"],
                   input=cfg, capture_output=True, text=True)
        code = proc.stdout.strip()
        if code in ("200", "201"):
            made.append(name)
        elif code != "409":
            raise FormError(f"creating bucket {name} answered {code or 'nothing'}")
    return made


def sops_key_secret(config_dir: Path, run: Any = subprocess.run) -> bool:
    """The one bootstrap Secret: the age key as sops/sops-age (key `age.agekey`), the only
    value that cannot itself arrive sealed."""
    if run(["kubectl", "-n", "sops", "get", "secret", "sops-age"], capture_output=True).returncode == 0:
        return False
    run(["kubectl", "create", "namespace", "sops"], capture_output=True)
    proc = run(["kubectl", "-n", "sops", "create", "secret", "generic", "sops-age",
                f"--from-file=age.agekey={config_dir / 'age.key'}"], capture_output=True, text=True)
    if proc.returncode != 0:
        raise FormError("creating sops/sops-age failed")
    return True


# ----------------------------------------------------------------------------- the API tunnel

API_PORT = 6443


def _port_open(port: int = API_PORT) -> bool:
    import socket
    with socket.socket() as s:
        s.settimeout(2)
        return s.connect_ex(("127.0.0.1", port)) == 0


def ensure_tunnel(instance_dir: Path, config_dir: Path, kubeconfig: str, *, run: Any = subprocess.run,
                  port_open: Any = _port_open, sleep: Any = None) -> bool:
    """The ssh tunnel to the node's API, opened when the kubeconfig names the loopback and
    nothing listens there. The API port is closed to the internet, so kubectl reaches it
    through ssh only; the desk opens the tunnel when it starts, but a run that starts
    before the node exists starts without one, and its first step that asks the cluster
    (the sops key) found nobody at 127.0.0.1:6443. Returns whether it opened one."""
    import time
    try:
        text = Path(kubeconfig).read_text(encoding="utf-8")
    except OSError:
        raise FormError("there is no kubeconfig yet: the node step writes it") from None
    if f"127.0.0.1:{API_PORT}" not in text and f"localhost:{API_PORT}" not in text:
        return False
    if port_open():
        return False
    cfg = yaml.safe_load((instance_dir / "instance" / "config.yaml").read_text(encoding="utf-8")) or {}
    ip = str(((cfg.get("nodes") or [{}])[0]).get("ip") or "")
    if not re.fullmatch(r"[0-9a-fA-F.:]{3,45}", ip):
        raise FormError("instance/config.yaml names no node address to open the API tunnel to")
    key = private_key(config_dir)
    run(["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
         "-o", f"UserKnownHostsFile={config_dir / 'known_hosts'}", "-o", "ExitOnForwardFailure=yes",
         "-o", "ServerAliveInterval=30", "-i", key, "-fN",
         "-L", f"127.0.0.1:{API_PORT}:127.0.0.1:{API_PORT}", f"root@{ip}"], capture_output=True)
    for _ in range(20):
        if port_open():
            return True
        (sleep or time.sleep)(1)
    raise FormError("the ssh tunnel to the node's API did not open (ssh to the node with the node key, then run again)")


# ----------------------------------------------------------------------------- orbitd + kit

def capture_orbitd_admin(config_dir: Path, run: Any = subprocess.run) -> bool:
    """`orbitd bootstrap` inside the running orbitd, once: its one-time admin credential
    goes straight to <config dir>/orbitd-admin.json (0600) and is never printed. A second
    bootstrap refuses (an admin already exists), so an existing file means done."""
    target = config_dir / "orbitd-admin.json"
    if target.exists():
        return False
    proc = run(["kubectl", "-n", "orbit", "exec", "deploy/orbitd", "-c", "orbitd", "--", "orbitd", "bootstrap"],
               capture_output=True, text=True)
    out = proc.stdout or ""
    if proc.returncode != 0 or "client_secret" not in out:
        raise FormError("orbitd bootstrap did not hand back a credential (an admin may already exist: "
                        "then the credential is wherever the first bootstrap put it)")
    write_private(target, out[out.index("{"):].strip() + "\n")
    return True


RECOVERY_KIT = "recovery-kit.txt"


def write_recovery_kit(config_dir: Path, form: Form) -> Path:
    """The Recovery Kit: everything that cannot be re-derived if this machine is lost —
    the sops age private key (every sealed file opens with it), the Grafana admin
    password, the first orbitd admin credential — in one private file, with what to do
    with it. The run prints its path, never its content."""
    def read(name: str) -> str:
        p = config_dir / name
        return p.read_text(encoding="utf-8").strip() if p.exists() else "(not created yet)"
    text = f"""ORBIT RECOVERY KIT — {form.name} ({form.domain})
Store this file in a password manager, then delete it from this machine.
Anyone holding it can decrypt every sealed Secret and administer orbitd.

1. sops age private key — decrypts every file under clusters/platform/secrets/.
   Restore it to <config dir>/age.key (mode 600) on a new operator machine, and as the
   cluster's sops/sops-age Secret on a rebuilt node.
{read("age.key")}

2. orbitd's first admin credential — exchange it for a token with `orbit login`.
{read("orbitd-admin.json")}
"""
    if (config_dir / "grafana-admin-password").exists():      # the dashboards feature is on
        text += f"""
3. Grafana admin password (user: admin) — https://grafana.{form.domain}
{read("grafana-admin-password")}
"""
    path = config_dir / RECOVERY_KIT
    write_private(path, text)
    return path


# ----------------------------------------------------------------------------- the run

STEPS = ("config", "credentials", "repository", "github-app", "infrastructure", "node", "buckets",
         "sops-key", "secrets", "platform", "orbitd-credential", "acceptance", "recovery-kit")


def run(instance_dir: Path, form_path: Path, *, config_dir: Path | None = None, kubeconfig: str | None = None,
        only: str | None = None, port: int = 8765, out: Any = print, sub: Any = subprocess.run) -> int:
    """Every onboarding step after the form, in order; each is idempotent, so a run that
    stopped resumes where it stopped. Prints one line per step (names, files, key names —
    never a value) and stops at the first step that cannot finish, saying why."""
    import json

    from . import secrets as sx
    from .onboard import run_acceptance
    form = load_form(form_path)
    # the features are checked before the first step, whichever step is asked for: a form
    # that cannot be is refused before anything is written, created or bought
    from . import orbit_features as _rules
    try:
        check_features(form, _rules.load_registry(instance_dir))
    except _rules.FeatureError:
        pass          # no registry here yet: the config step says so
    config_dir = config_dir or Path(os.environ.get("ORBIT_CONFIG_DIR") or Path.home() / ".config" / f"{form.name}-platform")
    kubeconfig = kubeconfig or os.environ.get("KUBECONFIG") or str(Path.home() / ".kube" / f"config-{form.name}-platform")
    state: dict[str, Any] = {}
    # what the owner put there before the run (the directory itself, the App file from the
    # two clicks) is narrowed to its owner like the form: the acceptance at the end refuses
    # a credentials directory or a credential others can reach
    narrowed = [p.name for p, mode in ((config_dir, 0o700), (config_dir / APP_FILE, 0o600)) if make_private(p, mode)]
    if narrowed:
        out("private: made readable by its owner alone: " + ", ".join(narrowed))

    def s_config() -> str:
        from . import orbit_features as rules
        try:
            registry = rules.load_registry(instance_dir)
        except rules.FeatureError as e:
            raise FormError(f"the instance directory is not a copy of core's template ({e})") from None
        wrote = fill_config(instance_dir, form, registry)
        return ("wrote " + ", ".join(wrote)) if wrote else "already filled"

    def use_node_key() -> None:
        # the scripts this run starts read the key by ORBIT_SSH_KEY; inside the desk the
        # pointer file names the operator machine's path, which is not a path here
        key = config_dir / "node-key"
        if key.exists() and not os.environ.get("ORBIT_SSH_KEY"):
            os.environ["ORBIT_SSH_KEY"] = private_key(config_dir)

    def api() -> None:
        use_node_key()
        ensure_tunnel(instance_dir, config_dir, kubeconfig, run=sub)

    def s_credentials() -> str:
        made = make_credentials(config_dir, form, run=sub)
        use_node_key()
        if not (instance_dir / ".sops.yaml").exists():
            sx.write_sops_yaml(instance_dir / ".sops.yaml", age_recipient(config_dir))
            made.append(".sops.yaml")
        return ("created " + ", ".join(made)) if made else "present"

    def s_repository() -> str:
        repo = ensure_repo(instance_dir, form, run=sub)
        state["repo_id"] = int(repo["id"])
        pushed = commit_and_push(instance_dir, "onboard: the instance, from the onboarding form", run=sub)
        return f"{repo['full_name']} ({'pushed' if pushed else 'up to date'})"

    def s_github_app() -> str:
        owner = github_api("GET", f"/users/{form.github_owner}")
        if "repo_id" not in state:
            state["repo_id"] = int(github_api("GET", f"/repos/{form.github_owner}/{form.name}-platform")["id"])
        app = github_app_flow(config_dir, form, owner_is_org=owner.get("type") == "Organization",
                              owner_id=int(owner["id"]), repo_id=state["repo_id"], port=port, announce=out)
        ruled = let_the_app_write(f"{form.github_owner}/{form.name}-platform", int(app["id"]))
        return (f"App {app['slug']} installed (installation {app['installation_id']})"
                + (f"; added to the bypass list of the ruleset {', '.join(repr(n) for n in ruled)} (Orbit writes to main as the App)" if ruled else ""))

    def s_infrastructure() -> str:
        if not form.server_id and not form.create_server:
            raise FormError("no server to adopt and no yes to buying one: set hetzner.server_id (adopt), or "
                            "hetzner.create_server: true (a new server costs money)")
        if form.server_id and not form.rebuild:
            raise FormError("an adopted server is rebuilt (its disk wiped) so the run owns it and its key: "
                            "set hetzner.rebuild: true to say yes to that")
        zone_id = cloudflare_zone_id(form)
        write_tfvars(instance_dir, config_dir, form, server_id=form.server_id, zone_id=zone_id)
        taken = adopt_imports(instance_dir, form, zone_id=zone_id) if form.server_id else []
        ip = tofu_apply(instance_dir, config_dir, run=sub)
        rebuilt = rebuild_server(config_dir, form, form.server_id, run=sub) if form.server_id else False
        wrote = write_ip(instance_dir, ip)
        init = sub(["python3", "scripts/instance-init.py"], cwd=str(instance_dir), capture_output=True, text=True)
        if init.returncode != 0:
            raise FormError("scripts/instance-init.py failed after the address was written")
        commit_and_push(instance_dir, "onboard: the node's address, the rendered platform tree", run=sub)
        return (f"node at {ip}" + (" (written into instance/config.yaml)" if wrote else "")
                + (f"; took over {', '.join(taken)}" if taken else "")
                + ("; rebuilt from " + NODE_IMAGE if rebuilt else ""))

    def s_node() -> str:
        use_node_key()
        proc = sub(["bash", "scripts/bootstrap-node.sh"], cwd=str(instance_dir), capture_output=True, text=True)
        if proc.returncode != 0:
            raise FormError("scripts/bootstrap-node.sh failed (re-run it alone to read its output)")
        return "k3s up, kubeconfig written"

    def s_buckets() -> str:
        if not form.has_bucket:
            return "none: this platform has no object storage"
        cfg = yaml.safe_load((instance_dir / "instance" / "config.yaml").read_text(encoding="utf-8")) or {}
        names = sorted(set(((cfg.get("objectStorage") or {}).get("buckets") or {}).values()))
        made = create_buckets(form, names, run=sub)
        return ("created " + ", ".join(made)) if made else "present: " + ", ".join(names)

    def s_sops_key() -> str:
        api()
        return "created sops/sops-age" if sops_key_secret(config_dir, run=sub) else "present"

    def s_secrets() -> str:
        app_file = config_dir / APP_FILE
        app = json.loads(app_file.read_text(encoding="utf-8")) if app_file.exists() else None
        ghcr = os.environ.get("GH_TOKEN", "")
        if not ghcr:
            raise FormError("GH_TOKEN is not set: the registry pull secret is the operator's own token")
        written = seal_platform_secrets(instance_dir, config_dir, form, ghcr_token=ghcr, app=app)
        commit_and_push(instance_dir, "onboard: the platform's sealed Secrets", run=sub)
        return ("sealed " + ", ".join(written)) if written else "present"

    def s_platform() -> str:
        api()
        apply_until_it_holds(lambda: sub(["orbit", "platform", "apply", str(instance_dir), "--kubeconfig", kubeconfig],
                                         capture_output=True, text=True), out, config_dir)
        wait = sub(["kubectl", "-n", "orbit", "rollout", "status", "deploy/orbitd", "--timeout=600s"], capture_output=True, text=True)
        if wait.returncode != 0:
            raise FormError("orbitd did not become ready within 10 minutes of the platform apply")
        return "every platform app applied; orbitd ready"

    def s_orbitd_admin() -> str:
        api()
        return "orbitd-admin.json written" if capture_orbitd_admin(config_dir, run=sub) else "present"

    def s_acceptance() -> str:
        api()
        return accept_with_patience(lambda: run_acceptance(instance_dir, kubeconfig=kubeconfig, config_dir=str(config_dir)), out)

    def s_recovery_kit() -> str:
        return f"written to {write_recovery_kit(config_dir, form)} — store it in a password manager, then delete it here"

    table = {"config": s_config, "credentials": s_credentials, "repository": s_repository, "github-app": s_github_app,
             "infrastructure": s_infrastructure, "node": s_node, "buckets": s_buckets, "sops-key": s_sops_key,
             "secrets": s_secrets, "platform": s_platform, "orbitd-credential": s_orbitd_admin,
             "acceptance": s_acceptance, "recovery-kit": s_recovery_kit}
    for name in STEPS:
        if only and name != only:
            continue
        try:
            out(f"{name}: {table[name]()}")
        except FormError as e:
            out(f"{name}: STOPPED — {e}")
            return 1
    return 0


def cmd_run(args: Any) -> int:
    # one line per step, the moment the step ends: written to a file (the skill starts the
    # run to a log and reads the log) the lines sat in a buffer until the run ended, and the
    # agent watching an hour-long run read an empty file (docs/V1.md §8)
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass
    try:
        return run(Path(args.instance_dir), Path(args.form),
                   config_dir=Path(args.config_dir) if args.config_dir else None,
                   kubeconfig=args.kubeconfig, only=args.only, port=args.port)
    except FormError as e:
        print(f"orbit onboard run: {e}")
        return 2


__all__ = ["Form", "FormError", "RECOVERY_KIT", "STEPS", "cmd_run", "run", "age_recipient", "capture_orbitd_admin",
           "dev_basic_auth_users", "check_features", "fill_config", "htpasswd_apr1", "load_form", "make_credentials",
           "random_password", "seal", "seal_platform_secrets", "write_private", "write_recovery_kit"]
