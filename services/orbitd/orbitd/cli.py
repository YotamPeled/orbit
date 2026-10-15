"""`orbitd serve` and `orbitd bootstrap`. Not the operator CLI (that is an MCP client and
another job); this is the process entrypoint and the one-time first-admin ceremony."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

from .auth import BootstrapDone, PrincipalExists, Store, generate_key
from .config import from_env
from .roles import Role

# The creation rule for a principal name (tools.py: PRINCIPAL_NAME_PATTERN,
# enforced on orbit_auth_issue and advertised on orbit_auth_retire, which
# itself accepts any name so a pre-rule identity can still be retired).
# Bootstrap must accept exactly what that rule accepts, or the first admin is
# a principal no creation path can ever name again. Kept as a literal here so
# this module stays importable without the SDK; test_bootstrap_name.py reads
# the pattern off the registered verb's Python type metadata and fails if the
# two drift apart.
BOOTSTRAP_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def bootstrap(args: argparse.Namespace) -> int:
    if not BOOTSTRAP_NAME_RE.fullmatch(args.name):
        print(f"orbitd: invalid --name {args.name!r}: a principal name starts with a "
              f"lowercase letter or digit, holds lowercase letters, digits and dashes, "
              f"and is at most 63 characters ({BOOTSTRAP_NAME_RE.pattern})",
              file=sys.stderr)
        return 2
    cfg = from_env()
    if not os.path.exists(cfg.signing_key_path):
        generate_key(cfg.signing_key_path)
        print(f"generated signing key {cfg.signing_key_path}", file=sys.stderr)
    store = Store(cfg.principals_path)
    try:
        # One check-and-write under the store lock: two bootstraps racing leave one admin.
        secret = store.add_first_admin(args.name, cfg.token_ttl)
    except BootstrapDone as done:
        # One-time, regardless of --name: a second admin only comes from orbit_auth_issue.
        print(f"orbitd: bootstrap already done; admin principal(s) {done.admins} exist in {cfg.principals_path}. "
              "A further admin is issued through the orbit_auth_issue tool (with confirmation), "
              "never by bootstrap.", file=sys.stderr)
        return 2
    except PrincipalExists:
        print(f"orbitd: principal {args.name!r} already exists; choose another --name", file=sys.stderr)
        return 2
    except (OSError, ValueError) as e:
        # an unreadable store (a truncated principals file) is named as such, never
        # as a name collision
        print(f"orbitd: cannot read {cfg.principals_path}: {e.__class__.__name__}", file=sys.stderr)
        return 2
    # Printed exactly once. The store keeps only the hash.
    print(json.dumps({"client_id": args.name, "client_secret": secret, "role": Role.admin.value,
                      "token_endpoint": cfg.issuer_url + "/token", "resource": cfg.mcp_url,
                      "grant_type": "client_credentials"}, indent=1))
    return 0


def serve(_: argparse.Namespace) -> int:
    from .server import serve as run
    run(from_env())
    return 0


def _argv_with_name_value(argv: list[str]) -> list[str]:
    """Spell `--name X` as `--name=X` when X starts with a dash.

    argparse reads `-leading` as a flag, so `bootstrap --name -leading` dies in
    the parser with a usage error (raising SystemExit) instead of reaching the
    name validation, which must refuse it itself with its own message. The
    respelling changes nothing for any other value."""
    out: list[str] = []
    i = 0
    while i < len(argv):
        if argv[i] == "--name" and i + 1 < len(argv) and argv[i + 1].startswith("-"):
            out.append(f"--name={argv[i + 1]}")
            i += 2
        else:
            out.append(argv[i])
            i += 1
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="orbitd")
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("bootstrap", help="create the signing key if absent and print one admin credential once")
    b.add_argument("--name", default="admin", help="principal name for the first admin (default: admin)")
    b.set_defaults(fn=bootstrap)
    s = sub.add_parser("serve", help="run the server")
    s.set_defaults(fn=serve)
    args = parser.parse_args(_argv_with_name_value(list(argv) if argv is not None else sys.argv[1:]))
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
