#!/usr/bin/env python3
"""
Refuse to let a credential into the generated manifests.

Generated Applications are committed in plaintext, so anything that reaches their values
is in git forever. This is the check that keeps that from happening.

It looks at VALUES, not at key names. The earlier version was a grep for /password:/ and
it fired on the first component that referenced a database password correctly:

    PGPASSWORD:
      secret: example-db-app
      key: password

which contains no credential at all — it is a pointer to a Secret that CloudNativePG
generates in the cluster. A check that goes red on the correct pattern is worse than no
check, because the only way to get a green build is to stop believing it.

So: a two-key mapping of exactly {secret, key} is a pointer and is skipped wholesale.
Anything else with a credential-shaped name and a literal value is a finding, as is any
value carrying a recognisable credential shape regardless of where it sits.

Used by both scripts/verify/stage3.sh and the catalog-check workflow, from one file, so
the two cannot drift apart.
"""
from __future__ import annotations

import os
import pathlib
import re
import sys

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "verify"))
import shown as display   # the shared display rule (scripts/verify/shown.py)

ROOT = pathlib.Path(__file__).resolve().parent.parent
APPS = ROOT / "clusters" / "apps"

# Names whose literal value would be a credential.
SECRET_NAME = re.compile(r"(pass(word|wd)?|token|secret|apikey|api_key|credential)", re.I)

# ...except these, which by Kubernetes convention hold the NAME of an object rather than
# anything inside it. `secretName: widgets-prod-dns-tls` on a Certificate is the same kind
# of finding as the PGPASSWORD pointer that got the first version of this file rewritten:
# a reference, red on the correct pattern. Excluded from the NAME rule only — the value is
# still matched against every SECRET_SHAPE below, so a credential parked in one of these
# fields is still caught. That is asserted by a test, not assumed.
REFERENCE_NAME = re.compile(r"^(secretName|existingSecret|secretRef|imagePullSecret|secretKey|"
                            r"[a-zA-Z]*SecretName|[a-zA-Z]*SecretRef)$")

# Shapes that are a credential wherever they appear, whatever the key is called.
SECRET_SHAPE = [
    (re.compile(r"\$apr1\$"), "an htpasswd MD5 hash"),
    (re.compile(r"\$2[aby]\$"), "a bcrypt hash"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "a private key"),
    (re.compile(r"(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{16,}"), "a GitHub token"),
    # The agent plane's own credential. `claude setup-token` emits sk-ant-oat01-…, the
    # API-key form is sk-ant-api03-…, admin keys sk-ant-admin…. This is THE token the
    # agent-runtime holds, and a skill document or an agent prompt renders verbatim into
    # a committed manifest — so a copy pasted into either has to turn the tree red.
    (re.compile(r"\bsk-ant-(api|oat|admin)\d{2}-[A-Za-z0-9_-]{20,}"),
     "an Anthropic API/OAuth key (sk-ant-…)"),
    (re.compile(r"\bey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\."), "a JWT"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "an AWS access key id"),
    (re.compile(r"basicAuthUsers|htpasswd", re.I), "an htpasswd reference"),
]


def walk(node, path, findings):
    if isinstance(node, dict):
        # A pointer to a Kubernetes Secret: names a Secret and a key inside it, carries no
        # value, and is exactly how a component is supposed to consume a credential.
        # ...so the credential-shaped-NAME rule does not apply to it; its two values are
        # still scanned for every credential SHAPE, because a token pasted where the
        # Secret's name belongs is exactly the paste this scanner exists to catch
        if set(node) == {"secret", "key"}:
            for ki, (k, v) in enumerate(node.items()):
                walk(v, f"{path}.{k}" if path else k, findings)
            return
        for ki, (k, v) in enumerate(node.items()):
            # the key is file text (a credential pasted INTO a key name is the very thing
            # this scanner exists to catch): shown only with a key's shape, else by position
            ks = str(k) if display.fits(str(k), "key") and len(str(k)) <= 40 and not any(rx.search(str(k)) for rx, _ in SECRET_SHAPE) \
                else f"#{ki}" + display.described("key", "key")
            here = f"{path}.{ks}" if path else ks
            if isinstance(v, (str, int, float)) and not isinstance(v, bool):
                if SECRET_NAME.search(str(k)) and not REFERENCE_NAME.match(str(k)):
                    findings.append(f"{here}: a literal value under a credential-shaped name")
            walk(v, here, findings)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            walk(v, f"{path}[{i}]", findings)
    elif isinstance(node, str):
        for rx, what in SECRET_SHAPE:
            if rx.search(node):
                findings.append(f"{path}: {what}")


def main() -> int:
    files = sorted(APPS.glob("*.yaml"))
    if not files:
        # A glob that matches nothing must never read as "nothing found".
        print("no generated manifests found — the check itself is broken")
        return 2
    findings: list[str] = []
    for f in files:
        for doc in yaml.safe_load_all(f.read_text()):
            if doc:
                walk(doc, f.name, findings)
    if findings:
        for x in findings:
            print(f"credential in a generated manifest: {x}")
        return 1
    print(f"no credentials in {len(files)} generated manifests")
    return 0


if __name__ == "__main__":
    sys.exit(main())
