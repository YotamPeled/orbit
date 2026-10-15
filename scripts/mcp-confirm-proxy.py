#!/usr/bin/env python3
"""
A stdio MCP server that forwards every tools/list and tools/call to one upstream MCP
server and answers the upstream's confirmation round when the host cannot.

    mcp-confirm-proxy.py --config <mcp.json> --server <name> --record <file> [--auto-confirm]

orbitd's mutating tools confirm through the 2026-07-28 input-required round: the first
call returns an `InputRequiredResult` carrying one elicitation under the key `confirm`
and a sealed `requestState`; the client answers `{confirm: true}` and retries the same
call with `inputResponses` and the state. A host that does not implement that round
(Claude Code 2.1 headless reports "Handler returned an invalid result") cannot ship
through orbitd at all. This proxy sits between such a host and orbitd: tools and their
schemas pass through unchanged, results pass through unchanged (content, structured
content, isError), and only an `InputRequiredResult` is handled here:

- with `--auto-confirm`: answered once with `confirm: true`, the retry's result is
  returned to the host, and one line is appended to `--record` naming the tool, the
  arguments, the elicitation message and the answer. The host never sees the round.
- without it: an error result saying the confirmation could not be answered, and the
  same record line with `answered: false`. Nothing was written upstream.

`--never-confirm KEY=VALUE` (repeatable) is the harness's safety rail: a round whose
arguments carry that key with that value is refused instead of answered, whatever the
flag says, and recorded with `denied: true`. A call that already carries
`inputResponses` (a host that does implement the round) is forwarded as it is and never
auto-answered. The upstream is read from `--config`, a
host-style MCP config (`mcpServers.<name>` of type `http` with `url` and `headers`, or
`stdio` with `command`, `args`, `env`); the config is a credential and nothing from it
is ever printed or recorded. Needs the pinned SDK (services/orbitd's environment).
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import threading
import time
from typing import Any

import anyio
import httpx2
from mcp import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import MCPError
from mcp_types import (
    CallToolRequestParams, CallToolResult, ElicitResult, InputRequiredResult,
    ListToolsResult, PaginatedRequestParams, TextContent,
)

CONFIRM_KEY = "confirm"
CONNECTION_CLOSED = -32000        # the SDK's code for a request whose connection went away


class Lost(Exception):
    """The upstream's answer did not arrive: no answer inside the ceiling, or the
    connection broke. The message is short and names no value."""


def _root(e: BaseException) -> BaseException:
    """The one exception inside a task group's wrapping, when there is exactly one."""
    while isinstance(e, BaseExceptionGroup) and len(e.exceptions) == 1:
        e = e.exceptions[0]
    return e


class HttpUpstream:
    """An HTTP upstream (orbitd), reached with a FRESH client for every call, each in a
    thread of its own with its own event loop.

    Measured on the test box (docs/V1.md §8): when the connection dies under a call
    (the ingress restarts whenever the traces feature is switched), the SDK's HTTP
    client either tears its whole session down or stops answering, and in the second
    case nothing on its event loop runs again: a ceiling set on that loop never fires.
    orbitd is stateless, so a call needs no session before it; a thread that stops
    answering is left behind (a daemon), the host is told the answer was lost, and the
    next call starts clean."""

    def __init__(self, url: str, headers: dict[str, str], ceiling: float) -> None:
        self.url, self.headers, self.ceiling = url, headers, ceiling

    async def _call(self, work: Any) -> Any:
        box: dict[str, Any] = {}
        done = threading.Event()

        def worker() -> None:
            async def go() -> Any:
                async with httpx2.AsyncClient(headers=dict(self.headers), timeout=180.0) as http:
                    async with Client(streamable_http_client(self.url, http_client=http)) as upstream:
                        return await work(upstream.session)
            try:
                box["result"] = anyio.run(go)
            except BaseException as e:  # reported by the caller; never the config
                box["error"] = e
            finally:
                done.set()

        threading.Thread(target=worker, daemon=True, name="upstream-call").start()
        deadline = time.monotonic() + self.ceiling
        while not done.is_set():
            if time.monotonic() >= deadline:
                raise Lost(f"none within {self.ceiling:g} s")
            await anyio.sleep(0.05)
        if "error" in box:
            e = _root(box["error"])
            if isinstance(e, MCPError) and getattr(getattr(e, "error", None), "code", None) != CONNECTION_CLOSED:
                raise e                 # the server's own refusal: the host's to see as it is
            raise Lost(type(e).__name__)
        return box["result"]

    async def list_tools(self, params: Any) -> Any:
        return await self._call(lambda session: session.list_tools(params=params))

    async def call_tool(self, name: str, arguments: dict[str, Any], **kw: Any) -> Any:
        return await self._call(lambda session: session.call_tool(name, arguments, **kw))


class StdioUpstream:
    """A stdio upstream: one process, one session for the proxy's life. A call past the
    ceiling is abandoned in a task of its own; the host is told the answer was lost."""

    def __init__(self, session: Any, tasks: Any, ceiling: float) -> None:
        self.session, self.tasks, self.ceiling = session, tasks, ceiling

    async def _call(self, work: Any) -> Any:
        box: dict[str, Any] = {}
        done = anyio.Event()

        async def run() -> None:
            try:
                box["result"] = await work(self.session)
            except Exception as e:
                box["error"] = e
            finally:
                done.set()

        self.tasks.start_soon(run)
        with anyio.move_on_after(self.ceiling):
            await done.wait()
        if not done.is_set():
            raise Lost(f"none within {self.ceiling:g} s")
        if "error" in box:
            e = box["error"]
            if isinstance(e, (anyio.BrokenResourceError, anyio.ClosedResourceError, anyio.EndOfStream, TimeoutError)):
                raise Lost(type(e).__name__)
            raise e
        return box["result"]

    async def list_tools(self, params: Any) -> Any:
        return await self._call(lambda session: session.list_tools(params=params))

    async def call_tool(self, name: str, arguments: dict[str, Any], **kw: Any) -> Any:
        return await self._call(lambda session: session.call_tool(name, arguments, **kw))


def _spec(config_path: str, name: str) -> tuple[str, dict[str, Any]]:
    with open(config_path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    servers = cfg.get("mcpServers") or {}
    if name not in servers:
        raise SystemExit(f"mcp-confirm-proxy: no server {name!r} in the config")
    spec = servers[name]
    kind = spec.get("type") or ("stdio" if "command" in spec else "http")
    if kind in ("http", "streamable-http", "streamable_http"):
        return "http", spec
    if kind == "stdio":
        return "stdio", spec
    raise SystemExit(f"mcp-confirm-proxy: unsupported server type {kind!r}")


def _record(path: str, **fields: Any) -> None:
    fields["at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(fields, sort_keys=True) + "\n")


def _messages(result: InputRequiredResult) -> list[str]:
    out = []
    for req in (result.input_requests or {}).values():
        params = getattr(req, "params", None)
        message = getattr(params, "message", None)
        if message:
            out.append(str(message))
    return out


def _error(text: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)], is_error=True,
                          structured_content={"code": "confirmation_unanswered", "what_failed": text,
                                              "evidence": {}, "retryable": False,
                                              "next_action": "run the harness with --auto-confirm, "
                                                             "or answer the confirmation from a host that can"})


def _lost(name: str, why: str) -> CallToolResult:
    """The upstream's answer never arrived. Said as a tool error the agent can act on,
    at once: a host left waiting on a dead connection waits out its own timeout (half an
    hour, measured: docs/V1.md §8), and the call it was waiting on may well have run."""
    text = (f"{name}: no answer reached this host ({why}). The connection to the server was lost or "
            f"went quiet; the call may or may not have run")
    return CallToolResult(content=[TextContent(type="text", text=text)], is_error=True,
                          structured_content={"code": "answer_lost", "what_failed": text, "evidence": {},
                                              "retryable": True,
                                              "next_action": "do not assume either way: read the state again "
                                                             "(a job: orbit_job_status with its job_id), then "
                                                             "call the tool again if it is still needed"})


async def serve(args: argparse.Namespace) -> None:
    kind, spec = _spec(args.config, args.server)
    async with contextlib.AsyncExitStack() as stack:
        if kind == "http":
            upstream: Any = HttpUpstream(str(spec["url"]), dict(spec.get("headers") or {}), args.call_timeout)
        else:
            params = StdioServerParameters(command=spec["command"], args=list(spec.get("args") or []),
                                           env=dict(spec.get("env") or {}) or None)
            client = await stack.enter_async_context(Client(stdio_client(params)))
            tasks = await stack.enter_async_context(anyio.create_task_group())
            stack.callback(tasks.cancel_scope.cancel)      # the host is gone: so are the calls it abandoned
            upstream = StdioUpstream(client.session, tasks, args.call_timeout)

        async def on_list_tools(_ctx: Any, params: PaginatedRequestParams | None) -> ListToolsResult:
            listed = await upstream.list_tools(params)
            return ListToolsResult(tools=listed.tools, next_cursor=listed.next_cursor)

        async def on_call_tool(_ctx: Any, params: CallToolRequestParams) -> CallToolResult:
            # Every upstream call has a ceiling, above the longest wait a tool itself
            # offers (orbit_job_wait and orbit_deploy_wait: 120 s). Past it, or when the
            # connection breaks, the host is told so instead of being left to wait.
            try:
                return await forward(params)
            except Lost as e:
                _record(args.record, tool=params.name, lost=str(e))
                return _lost(params.name, str(e))

        async def forward(params: CallToolRequestParams) -> CallToolResult:
            name, arguments = params.name, dict(params.arguments or {})
            if params.input_responses:
                # The host answers the round itself: forward as is, never auto-confirm.
                return await upstream.call_tool(name, arguments, input_responses=params.input_responses,
                                                request_state=params.request_state)
            result = await upstream.call_tool(name, arguments, allow_input_required=True)
            if not isinstance(result, InputRequiredResult):
                return result
            messages = _messages(result)
            denied = [rule for rule in args.never_confirm
                      if str(arguments.get(rule[0])) == rule[1]]
            if denied:
                key, value = denied[0]
                _record(args.record, tool=name, arguments=arguments, messages=messages, answered=False,
                        denied=f"{key}={value}")
                return _error(f"{name} asked for confirmation ({'; '.join(messages)}) and the harness "
                              f"refuses to answer it for {key}={value}; nothing was written")
            if not args.auto_confirm:
                _record(args.record, tool=name, arguments=arguments, messages=messages, answered=False)
                return _error(f"{name} asked for confirmation ({'; '.join(messages)}) and this host "
                              "cannot answer it; nothing was written")
            _record(args.record, tool=name, arguments=arguments, messages=messages, answered=True,
                    answer={CONFIRM_KEY: True})
            responses = {CONFIRM_KEY: ElicitResult(action="accept", content={CONFIRM_KEY: True})}
            final = await upstream.call_tool(name, arguments, input_responses=responses,
                                             request_state=result.request_state,
                                             allow_input_required=True)
            if isinstance(final, InputRequiredResult):
                return _error(f"{name} asked for confirmation again after it was answered once; "
                              "the proxy answers once per call")
            return final

        server: Server = Server(args.server, on_list_tools=on_list_tools, on_call_tool=on_call_tool)
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="mcp-confirm-proxy", description=__doc__.split("\n\n")[1])
    p.add_argument("--config", required=True, help="host-style MCP config naming the upstream (a credential)")
    p.add_argument("--server", required=True, help="the server name in the config; also this proxy's name")
    p.add_argument("--record", required=True, help="JSONL file; one line per confirmation round met")
    p.add_argument("--auto-confirm", action="store_true", help="answer each round once with confirm: true")
    p.add_argument("--call-timeout", type=float, default=float(os.environ.get("MCP_CONFIRM_PROXY_CALL_TIMEOUT") or 200),
                   metavar="SECONDS", help="ceiling for one upstream call; past it the host gets an "
                                           "`answer_lost` tool error (default 200)")
    p.add_argument("--never-confirm", action="append", default=[], metavar="KEY=VALUE",
                   help="refuse (never answer) a round whose arguments carry KEY equal to VALUE")
    args = p.parse_args(argv)
    rules = []
    for raw in args.never_confirm:
        key, sep, value = raw.partition("=")
        if not sep or not key:
            p.error(f"--never-confirm wants KEY=VALUE, got {raw!r}")
        rules.append((key, value))
    args.never_confirm = rules
    try:
        anyio.run(serve, args)
    except KeyboardInterrupt:
        pass
    except Exception as e:  # never the config; the exception type and message only
        _record(args.record, error=f"{type(e).__name__}: {e}")
        print(f"mcp-confirm-proxy: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
