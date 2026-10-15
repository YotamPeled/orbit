"""OAuth 2.1 resource server plus the co-hosted minimal authorization server.

Two metadata documents (RFC 9728, RFC 8414), one grant (`client_credentials`), one
token format (an Ed25519-signed JWT, `aud` = the canonical MCP URL, `sub` = the
principal, `scope` = the role's scopes, `jti` for revocation). Verification runs on
every MCP request through the SDK's bearer middleware; this module supplies the
`TokenVerifier`, the token endpoint and the documents. Refusals are answered in
the protocol by `roles.RoleGate`; nothing here decides them.

The principal store and the revocation list are files. A principal is a name, a role,
and the SHA-256 of a high-entropy client secret (32 random bytes: a slow KDF buys
nothing against a secret nobody can guess). The revocation list is one `jti` per line,
the shape a ConfigMap mount will have later; it is re-read on every verification so a
revoke needs no restart.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import fcntl
import tempfile
import threading
import logging
import os
import secrets
import time
from datetime import datetime, timezone
from typing import Any, Callable

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.routes import build_resource_metadata_url
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .config import Config
from .roles import ALL_SCOPES, Role, role_of_scopes

log = logging.getLogger("orbitd.auth")

ALG = "EdDSA"
CLOCK_LEEWAY = 30
MIN_TTL = 60
TOKEN_PATH = "/token"
JWKS_PATH = "/.well-known/jwks.json"
AS_METADATA_PATH = "/.well-known/oauth-authorization-server"
RS_METADATA_ROOT = "/.well-known/oauth-protected-resource"


def _now() -> int:
    return int(time.time())


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------ signing key

def generate_key(path: str) -> None:
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(pem)


class SigningKey:
    def __init__(self, path: str) -> None:
        with open(path, "rb") as f:
            self.pem = f.read()
        key = serialization.load_pem_private_key(self.pem, password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise SystemExit(f"orbitd: {path} is not an Ed25519 private key")
        self.private = key
        self.public_pem = key.public_key().public_bytes(serialization.Encoding.PEM,
                                                        serialization.PublicFormat.SubjectPublicKeyInfo)
        raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.kid = hashlib.sha256(raw).hexdigest()[:16]
        self.jwk = {"kty": "OKP", "crv": "Ed25519", "kid": self.kid, "alg": ALG, "use": "sig",
                    "x": base64.urlsafe_b64encode(raw).decode().rstrip("=")}

    @property
    def request_state_secret(self) -> bytes:
        """Seeds the SDK's request-state sealing so a confirmation survives a restart."""
        return hashlib.sha256(b"orbitd/request-state/" + self.pem).digest()


# ------------------------------------------------------------------ principal store

# The longest name the creation path will make. Enforced only where names are
# created (Store.add_principal): the retirement verb carries no bound, because
# loading applies no rule to what the store file already holds, so no number is
# safe against a file an older build wrote. 4096 covers the longest name the
# store has ever held; the creation rule still holds new names to 63 via its
# pattern.
PRINCIPAL_NAME_MAX_LENGTH = 4096


_STORE_LOCKS: dict[str, threading.Lock] = {}
_STORE_LOCKS_GUARD = threading.Lock()


def _store_lock_for(path: str) -> threading.Lock:
    with _STORE_LOCKS_GUARD:
        lock = _STORE_LOCKS.get(path)
        if lock is None:
            lock = threading.Lock()
            _STORE_LOCKS[path] = lock
        return lock


class PrincipalExists(ValueError):
    """The name is already registered (the store itself was readable)."""


class BootstrapDone(ValueError):
    """An admin already exists: bootstrap is one-time."""

    def __init__(self, admins: list[str]) -> None:
        self.admins = admins
        super().__init__(f"bootstrap already done; admin principal(s) {admins} exist")


class Store:
    """`{"principals": {name: {...}}, "tokens": {jti: {...}}}` in one JSON file, rewritten
    atomically. Small by construction (three roles, machine principals).

    Every change is a load-mutate-save under one exclusive lock: an in-process mutex
    (the token endpoint runs on the event loop while the auth tools run on the SDK's
    worker threads) plus `flock` on a sidecar (`orbitd bootstrap` is a second process on
    the same file). Unserialised, a mint could write back a snapshot taken before a
    retire and put the retired principal's secret back; two writers sharing one temp
    name could leave the file unparseable. The temp file is unique per writer."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = _store_lock_for(os.path.abspath(path))

    def _mutate(self, change: Callable[[dict[str, Any]], Any]) -> Any:
        """Apply `change` to the current document and save it, serialised against every
        other writer of this path (thread or process). `change` may raise to abort
        without saving."""
        lock_path = f"{self.path}.lock"
        with self._lock:
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                data = self.load()
                result = change(data)
                self.save(data)
            finally:
                os.close(fd)
        return result

    def load(self) -> dict[str, Any]:
        try:
            with open(self.path) as f:
                data = json.load(f)
        except FileNotFoundError:
            data = {}
        data.setdefault("principals", {})
        data.setdefault("tokens", {})
        return data

    def save(self, data: dict[str, Any]) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        fd, tmp = tempfile.mkstemp(prefix=os.path.basename(self.path) + ".", suffix=".tmp",
                                   dir=directory)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=1, sort_keys=True)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def principal(self, name: str) -> dict[str, Any] | None:
        return self.load()["principals"].get(name)

    def admins(self) -> list[str]:
        """Names of every admin principal; non-empty means bootstrap has happened."""
        # Layer note (shared with tools.orbit_auth_list / orbit_auth_retire): the
        # repair for rows that are not shaped like a principal lives here, in the
        # store's row readers, not in each verb's body -- every verb that reads a
        # row would otherwise need the same guard, and the next verb would miss
        # it. A row that is not a dict carries no role, so it is never an admin.
        return sorted(n for n, p in self.load()["principals"].items()
                      if isinstance(p, dict) and p.get("role") == Role.admin.value)

    def add_principal(self, name: str, role: Role, ttl: int) -> str:
        """Register `name` and return its client secret, the only time it is readable."""
        if len(name) > PRINCIPAL_NAME_MAX_LENGTH:
            raise ValueError(f"principal name is {len(name)} characters; "
                             f"at most {PRINCIPAL_NAME_MAX_LENGTH}")
        secret = secrets.token_urlsafe(32)

        def change(data: dict[str, Any]) -> None:
            if name in data["principals"]:
                raise ValueError(f"principal {name!r} already exists")
            data["principals"][name] = {"role": role.value, "secret_sha256": _sha256(secret),
                                        "ttl": ttl, "created": _iso(_now())}

        self._mutate(change)
        return secret

    def add_first_admin(self, name: str, ttl: int) -> str:
        """Bootstrap: register `name` as the first admin and return its client secret.
        One read-check-write under the store lock: two bootstraps racing cannot both
        pass an "is there an admin yet" check, so at most one ever succeeds."""
        if len(name) > PRINCIPAL_NAME_MAX_LENGTH:
            raise ValueError(f"principal name is {len(name)} characters; "
                             f"at most {PRINCIPAL_NAME_MAX_LENGTH}")
        secret = secrets.token_urlsafe(32)

        def change(data: dict[str, Any]) -> None:
            admins = sorted(n for n, p in data["principals"].items()
                            if isinstance(p, dict) and p.get("role") == Role.admin.value)
            if admins:
                raise BootstrapDone(admins)
            if name in data["principals"]:
                raise PrincipalExists(f"principal {name!r} already exists")
            data["principals"][name] = {"role": Role.admin.value, "secret_sha256": _sha256(secret),
                                        "ttl": ttl, "created": _iso(_now())}

        self._mutate(change)
        return secret

    def remove_principal(self, name: str) -> dict[str, Any] | None:
        """Delete `name` so its secret mints nothing more. Returns the removed entry,
        or None when the name was not there (idempotent: that changes nothing). The
        tokens it minted stay in the ledger under its name, so the caller can revoke
        each by `jti`; retirement ends the credential, not the name, and a later
        principal registered under the same name starts with a new secret and no live
        tokens of its own."""
        return self._mutate(lambda data: data["principals"].pop(name, None))

    def check_secret(self, name: str, secret: str) -> dict[str, Any] | None:
        p = self.principal(name)
        # Same layer note as admins(): a row that is not shaped like a principal
        # holds no credential, so it authenticates nothing (401 downstream, never
        # a 500 from indexing into a string or list).
        if not isinstance(p, dict) or not isinstance(p.get("secret_sha256"), str):
            return None
        if not hmac.compare_digest(p["secret_sha256"], _sha256(secret)):
            return None
        return p

    def record_token(self, jti: str, sub: str, role: Role, iat: int, exp: int) -> None:
        self._mutate(lambda data: data["tokens"].__setitem__(
            jti, {"sub": sub, "role": role.value, "iat": _iso(iat), "exp": _iso(exp)}))


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


class Revocations:
    def __init__(self, path: str) -> None:
        self.path = path

    def read(self) -> set[str]:
        try:
            with open(self.path) as f:
                return {ln.strip() for ln in f if ln.strip() and not ln.startswith("#")}
        except FileNotFoundError:
            return set()

    def add(self, jti: str) -> None:
        with open(self.path, "a") as f:
            f.write(jti + "\n")


# ------------------------------------------------------------------ tokens

class Tokens:
    def __init__(self, cfg: Config, key: SigningKey, store: Store, revocations: Revocations) -> None:
        self.cfg, self.key, self.store, self.revocations = cfg, key, store, revocations

    def mint(self, sub: str, role: Role, ttl: int | None = None, *, scopes: list[str] | None = None,
             record: bool = True, **override: Any) -> tuple[str, dict[str, Any]]:
        """Sign a token. `override` exists for tests that need a wrong `aud` or a past `exp`."""
        ttl = self.cfg.token_ttl if ttl is None else ttl
        iat = _now()
        claims: dict[str, Any] = {
            "iss": self.cfg.issuer_url, "aud": self.cfg.mcp_url, "sub": sub, "iat": iat,
            "exp": iat + ttl, "jti": secrets.token_urlsafe(16), "role": role.value,
            "scope": " ".join(scopes if scopes is not None else role.scopes),
        }
        claims.update(override)
        token = jwt.encode(claims, self.key.pem, algorithm=ALG, headers={"kid": self.key.kid})
        if record:
            self.store.record_token(claims["jti"], sub, role, iat, claims["exp"])
        return token, claims

    def verify(self, token: str) -> AccessToken | None:
        """The `TokenVerifier` contract, synchronous core. None means 401."""
        try:
            claims = jwt.decode(token, self.key.public_pem, algorithms=[ALG], audience=self.cfg.mcp_url,
                                issuer=self.cfg.issuer_url, leeway=CLOCK_LEEWAY,
                                options={"require": ["exp", "iat", "sub", "jti", "aud", "iss", "scope"]})
        except jwt.PyJWTError as e:
            log.info("token rejected: %s", type(e).__name__)
            return None
        if claims["jti"] in self.revocations.read():
            log.info("token rejected: revoked jti=%s", claims["jti"])
            return None
        scopes = [s for s in str(claims["scope"]).split() if s in ALL_SCOPES]
        if role_of_scopes(scopes) is None:
            return None
        return AccessToken(token=token, client_id=claims["sub"], scopes=scopes, expires_at=int(claims["exp"]),
                           resource=self.cfg.mcp_url, subject=claims["sub"],
                           claims={"iss": claims["iss"], "jti": claims["jti"], "role": claims.get("role")})

    async def verify_token(self, token: str) -> AccessToken | None:
        return self.verify(token)


# ------------------------------------------------------------------ the AS: /token

def _client_credentials(request: Request, form: dict[str, str]) -> tuple[str, str] | None:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("basic "):
        try:
            user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
            return user, pw
        except (ValueError, UnicodeDecodeError):
            return None
    if form.get("client_id") and form.get("client_secret"):
        return form["client_id"], form["client_secret"]
    return None


def _oauth_error(status: int, error: str, description: str, **headers: str) -> JSONResponse:
    return JSONResponse({"error": error, "error_description": description}, status_code=status,
                        headers={"Cache-Control": "no-store", **headers})


def token_endpoint(tokens: Tokens):
    cfg = tokens.cfg

    async def handle(request: Request) -> Response:
        if request.headers.get("content-type", "").split(";")[0].strip() != "application/x-www-form-urlencoded":
            return _oauth_error(400, "invalid_request", "body must be application/x-www-form-urlencoded")
        form = {k: str(v) for k, v in (await request.form()).items()}
        if form.get("grant_type") != "client_credentials":
            return _oauth_error(400, "unsupported_grant_type", "only client_credentials is supported")
        creds = _client_credentials(request, form)
        if creds is None:
            return _oauth_error(401, "invalid_client", "client authentication required",
                                **{"WWW-Authenticate": "Basic"})
        name, secret = creds
        principal = tokens.store.check_secret(name, secret)
        if principal is None:
            return _oauth_error(401, "invalid_client", "unknown client or bad secret",
                                **{"WWW-Authenticate": "Basic"})
        role = Role.parse(principal["role"])
        resource = form.get("resource")
        if resource is not None and resource.rstrip("/") != cfg.mcp_url:
            return _oauth_error(400, "invalid_target", f"this server issues tokens only for {cfg.mcp_url}")
        scopes = role.scopes
        if form.get("scope"):
            asked = form["scope"].split()
            if not set(asked) <= set(role.scopes):
                return _oauth_error(400, "invalid_scope", f"principal {name} holds role {role.value}")
            scopes = [s for s in role.scopes if s in asked]
        token, claims = tokens.mint(name, role, int(principal.get("ttl") or cfg.token_ttl), scopes=scopes)
        return JSONResponse({"access_token": token, "token_type": "Bearer",
                             "expires_in": claims["exp"] - claims["iat"], "scope": claims["scope"]},
                            headers={"Cache-Control": "no-store", "Pragma": "no-cache"})

    return handle


# ------------------------------------------------------------------ metadata documents

def as_metadata(cfg: Config) -> dict[str, Any]:
    """RFC 8414. No authorization endpoint: the only grant never visits one, and RFC 8414
    §2 makes `authorization_endpoint` optional in exactly that case."""
    return {
        "issuer": cfg.issuer_url,
        "token_endpoint": cfg.issuer_url + TOKEN_PATH,
        "jwks_uri": cfg.issuer_url + JWKS_PATH,
        "grant_types_supported": ["client_credentials"],
        "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
        "scopes_supported": ALL_SCOPES,
        "response_types_supported": [],
    }


def rs_metadata(cfg: Config) -> dict[str, Any]:
    """RFC 9728: who may issue tokens for this resource, and what scopes exist."""
    return {
        "resource": cfg.mcp_url,
        "authorization_servers": [cfg.issuer_url],
        "jwks_uri": cfg.issuer_url + JWKS_PATH,
        "scopes_supported": ALL_SCOPES,
        "bearer_methods_supported": ["header"],
        "resource_signing_alg_values_supported": [ALG],
    }


def rs_metadata_url(cfg: Config) -> str:
    return str(build_resource_metadata_url(AnyHttpUrl(cfg.mcp_url)))


def rs_metadata_paths(cfg: Config) -> list[str]:
    """RFC 9728 §3.1 inserts the well-known segment before the resource path; the root
    form is served too, so a client that probed the origin finds the same document."""
    path_form = rs_metadata_url(cfg).split("/", 3)[3]
    return sorted({RS_METADATA_ROOT, "/" + path_form})


def jwks(key: SigningKey) -> dict[str, Any]:
    return {"keys": [key.jwk]}


# Refusals were once answered here, in front of the protocol, with an HTTP 403;
# that tier tore the session down and is gone. `roles.RoleGate` answers them now,
# in the protocol, so this module keeps no middleware of its own.

