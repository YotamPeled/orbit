#!/usr/bin/env python3
"""Mint a GitHub App installation token, or ask a registry for a manifest digest.

Stdlib only. RS256 for the App JWT is the openssl command on a temporary PEM —
no JWT library, so there is nothing extra to audit.

Default mode prints an installation token to stdout and nothing else. The token is
never written to stderr, never interpolated into an error, never logged. The three
App files are read from $ORBIT_CONFIG_DIR (the caller passes the directory; this
script does not know the instance name and does not default one):

    github-app-id
    github-app-installation-id
    github-app-key.pem

`--digest IMAGE TAG` reuses the registry manifest check below (ghcr.io, anonymous or
ORBIT_REGISTRY_TOKEN_FILE) and prints the Docker-Content-Digest. ship.sh needs the
digest for the Image-Digest trailer and the 200-or-stop check.
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

IMAGE_RE = re.compile(r"[a-z0-9][a-z0-9._/-]{0,255}")
TAG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")

ACCEPT = ",".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])


def _b64url(data: bytes) -> bytes:
    return base64.urlsafe_b64encode(data).rstrip(b"=")


def _read_file(path: str) -> str:
    with open(path) as f:
        return f.read()


def _config_dir() -> str:
    directory = os.environ.get("ORBIT_CONFIG_DIR")
    if not directory:
        sys.exit("ORBIT_CONFIG_DIR is not set")
    return directory


def _secret(name: str) -> str:
    return _read_file(os.path.join(_config_dir(), name)).strip()


def app_jwt() -> str:
    now = int(time.time())
    signing = (
        _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        + b"."
        + _b64url(json.dumps(
            {"iat": now - 60, "exp": now + 540, "iss": _secret("github-app-id")}).encode())
    )
    key = _read_file(os.path.join(_config_dir(), "github-app-key.pem"))
    with tempfile.NamedTemporaryFile("w", suffix=".pem") as pem:
        pem.write(key if key.endswith("\n") else key + "\n")
        pem.flush()
        try:
            sig = subprocess.run(
                ["openssl", "dgst", "-sha256", "-sign", pem.name],
                input=signing, capture_output=True, check=True).stdout
        except subprocess.CalledProcessError:
            sys.exit("jwt sign failed")
    return (signing + b"." + _b64url(sig)).decode()


def installation_token() -> str:
    api = os.environ.get("GITHUB_API", "https://api.github.com").rstrip("/")
    req = urllib.request.Request(
        f"{api}/app/installations/{_secret('github-app-installation-id')}/access_tokens",
        method="POST",
        headers={"Authorization": "Bearer " + app_jwt(),
                 "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            token = json.load(response)["token"]
    except (urllib.error.URLError, TimeoutError, OSError, KeyError, ValueError):
        sys.exit("token request failed")
    if not isinstance(token, str) or not token:
        sys.exit("token request failed")
    return token


def _registry_root() -> str:
    return os.environ.get("ORBIT_REGISTRY_URL", "https://ghcr.io").rstrip("/")


def _pull_token() -> str | None:
    path = os.environ.get("ORBIT_REGISTRY_TOKEN_FILE")
    if not path:
        return None
    try:
        return _read_file(path).strip()
    except FileNotFoundError:
        sys.exit("registry token file is missing")


def manifest_digest(image: str, tag: str) -> str:
    """HEAD the tag's manifest. Returns the digest, or exits on absence/error.

    404 is "not in the registry". Auth failure and outage are distinct failures so
    a broken pull token cannot look like "tag was never pushed".
    """
    if not image.startswith("ghcr.io/") or not IMAGE_RE.fullmatch(image):
        sys.exit("refusing existence check for image")
    if not TAG_RE.fullmatch(tag):
        sys.exit("refusing tag")
    path = image[len("ghcr.io/"):]
    root = _registry_root()
    tok_req = urllib.request.Request(
        f"{root}/token?service=ghcr.io&scope="
        + urllib.parse.quote(f"repository:{path}:pull", safe=":"))
    pull = _pull_token()
    if pull:
        basic = base64.b64encode(f"orbit:{pull}".encode()).decode()
        tok_req.add_header("Authorization", "Basic " + basic)
    # The registry is asked up to three times when it could not be reached at all, or
    # answered that it is busy or broken (429, 5xx): one dropped connection between the
    # cluster and the registry failed a ship outright ("registry unreachable" twice in
    # seven minutes on the rebuilt test box, each time with the registry answering the
    # next request; docs/V1.md §8). An ANSWER about the image or the credential (404, 401,
    # 403) is final on the first ask.
    tries = max(1, int(os.environ.get("ORBIT_REGISTRY_TRIES", "3")))
    gap_s = float(os.environ.get("ORBIT_REGISTRY_RETRY_S", "3"))
    for attempt in range(1, tries + 1):
        try:
            with urllib.request.urlopen(tok_req, timeout=30) as response:
                token = json.load(response)["token"]
            head = urllib.request.Request(
                f"{root}/v2/{path}/manifests/{urllib.parse.quote(tag, safe='')}",
                method="HEAD",
                headers={"Authorization": "Bearer " + token, "Accept": ACCEPT})
            with urllib.request.urlopen(head, timeout=30) as response:
                status = response.getcode() if hasattr(response, "getcode") else response.status
                digest = response.headers.get("Docker-Content-Digest") or ""
            break
        except urllib.error.HTTPError as err:
            if err.code == 404:
                sys.exit("manifest not found")
            if err.code in (401, 403):
                sys.exit("registry auth failed")
            if attempt == tries or not (err.code == 429 or 500 <= err.code <= 599):
                sys.exit("registry error")
        except (urllib.error.URLError, TimeoutError, OSError, KeyError, ValueError):
            if attempt == tries:
                sys.exit("registry unreachable")
        time.sleep(gap_s)
    if status == 404:
        sys.exit("manifest not found")
    if status != 200:
        sys.exit("registry error")
    if not digest:
        sys.exit("manifest digest missing")
    return digest


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if argv == []:
        sys.stdout.write(installation_token() + "\n")
        return
    if len(argv) == 3 and argv[0] == "--digest":
        sys.stdout.write(manifest_digest(argv[1], argv[2]) + "\n")
        return
    sys.exit("usage: github-app-token.py [--digest IMAGE TAG]")


if __name__ == "__main__":
    main()
