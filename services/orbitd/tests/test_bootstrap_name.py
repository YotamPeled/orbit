"""The first identity must be one the surface can name.

The verb that takes a machine identity out of service accepts names matching the pattern
every identity tool declares. Bootstrap, which creates the FIRST admin, validates nothing.
So `bootstrap --name Ops` succeeds and produces an admin principal that no tool can ever
name: not to retire it, not to reason about it. It is permanent, and it is the identity
with the most power on the surface.

The check below does not repeat the pattern. It reads the pattern off the registered tool's
own schema and requires bootstrap to accept exactly what that accepts, so the two cannot
drift apart again.

Run under pytest from services/orbitd.
"""

from __future__ import annotations

import hashlib
import json
import re

import pytest

from orbitd import cli as cli_mod
from orbitd.auth import Store
from orbitd.roles import Role

CANDIDATES = [
    "admin",            # the default
    "ci-deployer",
    "a",
    "0ps",
    "Ops",              # a capital: the review's own example
    "ops team",         # a space
    "ops.team",         # a dot
    "-leading",         # a leading dash
    "",                 # nothing at all
    "x" * 200,          # far too long
    "ops\nactor=admin",  # a separator, which belongs nowhere near an identity name
]


def tool_name_pattern(app_state) -> re.Pattern:
    """The pattern the identity tools declare, read off the registered schema rather than
    copied here, so bootstrap is held to whatever the tools actually accept."""
    from orbitd.server import build_server

    _, registry = build_server(app_state)
    spec = registry.get("orbit_auth_retire")
    assert spec is not None, "the retirement verb is not registered; this check is empty"
    import typing

    hints = typing.get_type_hints(spec.fn, include_extras=True)
    field = hints["name"].__metadata__[0]
    patterns = [m.pattern for m in getattr(field, "metadata", [])
                if getattr(m, "pattern", None)]
    patterns += [p for p in [getattr(field, "pattern", None)] if p]
    assert patterns, f"the retirement verb declares no pattern for a name: {field!r}"
    return re.compile(patterns[0])


def _dirname(name: str) -> str:
    """A short directory per case: a candidate may be longer than a path component."""
    return hashlib.sha256(name.encode()).hexdigest()[:16]


def run_bootstrap(monkeypatch, tmp_path, name: str) -> tuple[int, Store]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ORBITD_SIGNING_KEY", str(tmp_path / "signing.pem"))
    monkeypatch.setenv("ORBITD_PRINCIPALS", str(tmp_path / "principals.json"))
    monkeypatch.setenv("ORBITD_REVOCATIONS", str(tmp_path / "revoked.txt"))
    monkeypatch.setenv("ORBITD_MCP_URL", "https://mcp.fixture.invalid/mcp")
    monkeypatch.setenv("ORBITD_ISSUER_URL", "https://mcp.fixture.invalid")
    code = cli_mod.main(["bootstrap", "--name", name])
    return code, Store(str(tmp_path / "principals.json"))


def test_the_pattern_is_really_declared_and_really_refuses_something(state):
    """Anti-vacuity: a pattern that accepted everything would make the check below empty."""
    pattern = tool_name_pattern(state)
    assert pattern.fullmatch("admin")
    assert not pattern.fullmatch("Ops")


@pytest.mark.parametrize("name", CANDIDATES)
def test_bootstrap_accepts_exactly_the_names_the_tools_accept(state, monkeypatch, tmp_path, name):
    acceptable = bool(tool_name_pattern(state).fullmatch(name))
    code, store = run_bootstrap(monkeypatch, tmp_path / _dirname(name), name)
    if acceptable:
        assert code == 0, f"bootstrap refused {name!r}, which every identity tool accepts"
        assert store.principal(name) is not None
    else:
        assert code != 0, (
            f"bootstrap created the first admin as {name!r}, a name no tool can ever say, "
            "so it can never be retired")
        assert store.principal(name) is None, "a refused bootstrap wrote a principal anyway"
        assert store.admins() == [], "a refused bootstrap left an administrator behind"


@pytest.mark.parametrize("name", ["Ops", "ops team", ""])
def test_a_refused_bootstrap_says_what_is_wrong_with_the_name(state, monkeypatch, tmp_path,
                                                              capsys, name):
    run_bootstrap(monkeypatch, tmp_path / "x", name)
    out, err = capsys.readouterr()
    assert name in err or "name" in err.lower(), (
        "a person given a name they cannot use is told nothing about it: " + err)


def test_the_credential_is_still_printed_once_for_an_acceptable_name(state, monkeypatch,
                                                                     tmp_path, capsys):
    """The guard: a validation that refused everything would pass every check above."""
    code, _ = run_bootstrap(monkeypatch, tmp_path / "ok", "ci-admin")
    out, _ = capsys.readouterr()
    assert code == 0
    body = json.loads(out)
    assert body["client_id"] == "ci-admin" and body["role"] == Role.admin.value
    assert body["client_secret"]
