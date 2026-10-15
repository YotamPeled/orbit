#!/usr/bin/env python3
"""orbitd's guardrails probed live with real tokens (docs/V1.md §5 S11; verify stage7).

    guardrails.py <mcp-dir> <project> <env> <component> <tag>

<mcp-dir> holds scenario-{viewer,operator,admin}.mcp.json (bearer configs); <tag> should
be the one the component already runs (the ship the probe starts is never confirmed, so
nothing changes either way). Prints one
`ok`/`FAIL` line per guard and exits 1 if any failed. Nothing is written to the platform:
the one ship it starts is never confirmed, every confirmed retry it sends is one orbitd
must refuse, and the two throwaway principals it issues are retired at the end.
Needs the pinned SDK (services/orbitd's environment). No credential is ever printed.
"""
from __future__ import annotations

import asyncio
import base64
import json
import sys
import time
import urllib.parse
import urllib.request
from typing import Any

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp_types import ElicitResult, InputRequiredResult

FAILS: list[str] = []


def ok(msg: str) -> None:
    print(f"  ok    {msg}", flush=True)


def bad(msg: str) -> None:
    print(f"  FAIL  {msg}", flush=True)
    FAILS.append(msg)


def check(cond: bool, good: str, broke: str) -> None:
    ok(good) if cond else bad(broke)


async def attempt(url: str, headers: dict[str, str], tool: str, args: dict[str, Any], **kw: Any) -> str:
    """The call's status, or `refused:<why>` when orbitd answers with a protocol error
    (an invalid requestState is refused that way) or the transport is refused (401)."""
    try:
        return status_of(await session_call(url, headers, tool, args, **kw))
    except BaseException as e:  # noqa: BLE001 - anyio wraps the MCP error in a group
        inner = e
        while isinstance(inner, BaseExceptionGroup) and inner.exceptions:
            inner = inner.exceptions[0]
        return "refused:" + str(getattr(inner, "message", None) or type(inner).__name__)[:80]


def load(path: str) -> tuple[str, dict[str, str]]:
    spec = json.load(open(path, encoding="utf-8"))["mcpServers"]["orbit"]
    return spec["url"], dict(spec.get("headers") or {})


async def session_call(url: str, headers: dict[str, str], tool: str | None, args: dict[str, Any] | None = None,
                       **kw: Any) -> Any:
    """tools/list when tool is None, else one tools/call; exceptions propagate."""
    async with httpx2.AsyncClient(headers=headers, timeout=60.0) as http:
        async with Client(streamable_http_client(url, http_client=http)) as c:
            if tool is None:
                return [t.name for t in (await c.session.list_tools()).tools]
            return await c.session.call_tool(tool, args or {}, allow_input_required=True, **kw)


def status_of(result: Any) -> str:
    if isinstance(result, InputRequiredResult):
        return "input_required"
    sc = getattr(result, "structured_content", None) or {}
    if getattr(result, "is_error", False):
        return "error:" + str(sc.get("code") or "")
    return str(sc.get("status") or "")


ACCEPT = {"confirm": ElicitResult(action="accept", content={"confirm": True})}


async def confirmed(url: str, headers: dict[str, str], tool: str, args: dict[str, Any]) -> Any:
    first = await session_call(url, headers, tool, args)
    if not isinstance(first, InputRequiredResult):
        return first
    return await session_call(url, headers, tool, args, input_responses=ACCEPT, request_state=first.request_state)


def token_for(evidence: dict[str, Any], resource: str) -> str:
    body = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": evidence["client_id"],
                                   "client_secret": evidence["client_secret"], "resource": resource}).encode()
    req = urllib.request.Request(evidence["token_endpoint"], data=body, method="POST")
    return json.load(urllib.request.urlopen(req))["access_token"]


def jti_of(token: str) -> str:
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["jti"]


async def refused_listing(url: str, token: str) -> bool:
    try:
        await session_call(url, {"Authorization": "Bearer " + token}, None)
    except BaseException:  # 401 surfaces as a transport error inside an exception group
        return True
    return False


async def main(mcp_dir: str, project: str, env: str, component: str, tag: str) -> int:
    v_url, v_h = load(f"{mcp_dir}/scenario-viewer.mcp.json")
    o_url, o_h = load(f"{mcp_dir}/scenario-operator.mcp.json")
    a_url, a_h = load(f"{mcp_dir}/scenario-admin.mcp.json")
    ship = {"project": project, "env": env, "components": [component], "tag": tag}

    tools = await session_call(v_url, v_h, None)
    check("orbit_deploy_ship" not in tools and "orbit_deploy_rollback" not in tools,
          "a viewer token is not offered orbit_deploy_ship or orbit_deploy_rollback",
          "a viewer token is offered a ship or rollback tool")
    st = await attempt(v_url, v_h, "orbit_deploy_ship", ship)
    check(st.startswith(("error", "refused")), f"a viewer calling orbit_deploy_ship is refused ({st})",
          f"a viewer calling orbit_deploy_ship got {st}")

    first = await session_call(o_url, o_h, "orbit_deploy_ship", ship)
    check(isinstance(first, InputRequiredResult), "an operator's ship asks for confirmation before anything is written",
          f"an operator's ship did not ask for confirmation ({status_of(first)})")
    if isinstance(first, InputRequiredResult):
        def held(s: str) -> bool:
            return s == "refused" or s.startswith(("error", "refused:")) or s == "input_required"
        swapped = dict(ship, tag="main-0000000")
        st = await attempt(o_url, o_h, "orbit_deploy_ship", swapped, input_responses=ACCEPT,
                           request_state=first.request_state)
        check(held(st), f"the confirmation replayed with swapped arguments is refused ({st})",
              f"the confirmation replayed with swapped arguments got {st}")
        st = await attempt(a_url, a_h, "orbit_deploy_ship", ship, input_responses=ACCEPT,
                           request_state=first.request_state)
        check(held(st), f"the confirmation replayed by another token is refused ({st})",
              f"the confirmation replayed by another token got {st}")

    stamp = time.strftime("%H%M%S", time.gmtime())
    resource = o_url
    temps = []
    for name, ttl in ((f"s11-revoke-{stamp}", 3600), (f"s11-expire-{stamp}", 60)):
        r = await confirmed(a_url, a_h, "orbit_auth_issue", {"name": name, "role": "viewer", "ttl": ttl})
        ev = (getattr(r, "structured_content", None) or {}).get("evidence") or {}
        if "client_secret" not in ev:
            bad(f"could not issue the throwaway principal {name} ({status_of(r)})"); continue
        temps.append(name)
        tok = token_for(ev, resource)
        live = not await refused_listing(o_url, tok)
        if name.startswith("s11-revoke"):
            r = await confirmed(a_url, a_h, "orbit_auth_revoke", {"jti": jti_of(tok)})
            gone = await refused_listing(o_url, tok)
            check(live and gone, "a token that worked is refused once revoked",
                  f"revocation: worked before {live}, refused after {gone} ({status_of(r)})")
        else:
            await asyncio.sleep(75)
            gone = await refused_listing(o_url, tok)
            check(live and gone, "a token that worked is refused once its 60 s lifetime is over",
                  f"expiry: worked before {live}, refused after {gone}")
    for name in temps:
        await confirmed(a_url, a_h, "orbit_auth_retire", {"name": name})
    return 1 if FAILS else 0


def issued_dir(tmp: str, stamp: str, names: list[str]) -> None:
    """`--issue` mode (verify stage7): issue throwaway viewer, operator and admin
    principals through the desk's own `orbit` session (an admin), exchange each for a
    token and write scenario-<role>.mcp.json into <tmp> (0600). Every credential stays in
    this process and those files; each name is appended to <names> the moment it exists,
    so the caller retires every one even when a later step fails."""
    import os
    import subprocess
    for role in ("viewer", "operator", "admin"):
        name = f"verify-guard-{role}-{stamp}"
        out = subprocess.run(["orbit", "call", "orbit_auth_issue", "--yes", "--json",
                              json.dumps({"name": name, "role": role, "ttl": 900})],
                             capture_output=True, text=True, timeout=120).stdout
        try:
            ev = json.loads(out[out.index("{"):]).get("evidence") or {}
        except ValueError:
            ev = {}
        if "client_secret" not in ev:
            raise SystemExit(f"could not issue the throwaway {role} principal through the desk's orbit session")
        names.append(name)
        base = ev["token_endpoint"].rsplit("/token", 1)[0]
        tok = token_for(ev, base + "/mcp")
        path = os.path.join(tmp, f"scenario-{role}.mcp.json")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump({"mcpServers": {"orbit": {"type": "http", "url": base + "/mcp",
                                                "headers": {"Authorization": "Bearer " + tok}}}}, fh)


def retire(names: list[str]) -> None:
    import subprocess
    for name in names:
        subprocess.run(["orbit", "call", "orbit_auth_retire", "--yes", "--json", json.dumps({"name": name})],
                       capture_output=True, text=True, timeout=120)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--issue"]:
        import tempfile
        stamp = time.strftime("%H%M%S", time.gmtime())
        with tempfile.TemporaryDirectory() as tmp:
            issued: list[str] = []
            try:
                issued_dir(tmp, stamp, issued)
                rc = asyncio.run(main(tmp, *sys.argv[2:6]))
            finally:
                retire(issued)
        sys.exit(rc)
    sys.exit(asyncio.run(main(*sys.argv[1:6])))
