"""Roles, scopes and the tool registry.

viewer ⊂ operator ⊂ admin. A role is a set of scopes: an admin token carries all three
`orbit:*` scopes, so "may this principal call this tool" is one membership test on the
tool's declared role. Every tool is registered through `Registry.tool(...)`, which is
where a tool declares its role, its annotations and (by its return annotation) the
result envelope it promises. The role gate reads only this table.
"""

from __future__ import annotations

import enum
import inspect
import logging
import re
import typing
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.server.mcpserver import MCPServer
from mcp_types import CallToolResult, ListToolsResult, TextContent, ToolAnnotations
from pydantic import BaseModel, Field

from .audit import log_audit

log = logging.getLogger("orbitd.audit")

SCOPE_PREFIX = "orbit:"

#: Longest caller-controlled name echoed back in a refusal body. A two-megabyte
#: tool name must not come back in a two-megabyte refusal; the refusal names
#: what was refused, truncated with a marker. The audit record keeps its own
#: bound at the sink, so neither output grows with the caller's input.
MAX_REFUSAL_ECHO_LEN = 256

REFUSAL_TRUNCATED = "...[truncated]"


def refusal_echo(value: Any) -> str:
    """Render one caller-controlled name for a refusal body, bounded."""
    text = value if isinstance(value, str) else str(value)
    if len(text) > MAX_REFUSAL_ECHO_LEN:
        return text[:MAX_REFUSAL_ECHO_LEN] + REFUSAL_TRUNCATED
    return text


class Role(str, enum.Enum):
    viewer = "viewer"
    operator = "operator"
    admin = "admin"

    @property
    def scope(self) -> str:
        return SCOPE_PREFIX + self.value

    @property
    def scopes(self) -> list[str]:
        """Every scope this role carries: its own and every role below it."""
        return [r.scope for r in ORDER[: ORDER.index(self) + 1]]

    @classmethod
    def parse(cls, value: str) -> Role:
        try:
            return cls(value)
        except ValueError:
            raise ValueError(f"unknown role {value!r}; one of {[r.value for r in cls]}") from None


ORDER: list[Role] = [Role.viewer, Role.operator, Role.admin]
ALL_SCOPES: list[str] = [r.scope for r in ORDER]


def role_of_scopes(scopes: list[str]) -> Role | None:
    """The highest role a scope list carries, or None if it carries no orbit role."""
    held = None
    for r in ORDER:
        if r.scope in scopes:
            held = r
    return held


# ------------------------------------------------------------------ result envelopes

class Step(BaseModel):
    name: str
    status: Literal["ok", "failed", "skipped"]
    detail: str | None = None


class CheckoutSource(BaseModel):
    """Freshness of the git-sync checkout a desired-state payload was read from."""

    commit: str
    age_s: int
    lag_note: str


class Mutation(BaseModel):
    """The verdict shape every mutating tool returns (DESIGN-v2 §3.5)."""

    status: Literal["applied", "refused", "failed", "dry_run", "running"]
    steps: list[Step] = Field(default_factory=list)
    commit: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    source: CheckoutSource | None = None


class Listing(BaseModel):
    """The one pagination envelope (DESIGN-v2 §3.4): `cursor` in, `next_cursor` out."""

    items: list[Any]
    total: int | None = None
    returned: int = 0
    truncated: bool = False
    next_cursor: str | None = None
    source: CheckoutSource | None = None

    @classmethod
    def of(cls, items: list[Any], *, total: int | None = None, next_cursor: str | None = None,
           source: CheckoutSource | None = None) -> Listing:
        total = len(items) if total is None else total
        return cls(items=items, total=total, returned=len(items), truncated=next_cursor is not None,
                   next_cursor=next_cursor, source=source)


class ToolFailure(BaseModel):
    """The `isError` payload for anything an operator or a model could act on."""

    code: str
    what_failed: str
    evidence: dict[str, Any] = Field(default_factory=dict)
    next_action: str | None = None
    retryable: bool = False


def failure(code: str, what_failed: str, *, next_action: str | None = None,
            retryable: bool = False, **evidence: Any) -> CallToolResult:
    body = ToolFailure(code=code, what_failed=what_failed, evidence=evidence,
                       next_action=next_action, retryable=retryable)
    return CallToolResult(content=[TextContent(type="text", text=body.model_dump_json())],
                          structured_content=body.model_dump(), is_error=True)


# ------------------------------------------------------------------ registry

NAME_RE = re.compile(r"^orbit_[a-z0-9_]+$")


@dataclass(frozen=True)
class ToolSpec:
    name: str
    fn: Callable[..., Any]
    role: Role
    annotations: ToolAnnotations
    description: str
    title: str | None = None


class Registry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self._resource_tools: dict[str, str] = {}
        self._resource_prefixes: list[tuple[str, str]] = []

    def tool(self, *, role: Role, read_only: bool, destructive: bool = False, idempotent: bool = False,
             name: str | None = None, title: str | None = None) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Declare a tool. `read_only` tools may return any model; a mutating tool must
        promise the `Mutation` envelope in its return annotation, so the contract of
        §3.5 is checked at import time rather than discovered by a caller."""

        def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
            tool_name = name or fn.__name__
            if not NAME_RE.match(tool_name) or len(tool_name) > 64:
                raise ValueError(f"tool name {tool_name!r} must match {NAME_RE.pattern} and be <= 64 chars")
            if tool_name in self._tools:
                raise ValueError(f"tool {tool_name!r} registered twice")
            if not fn.__doc__ or not fn.__doc__.strip():
                raise ValueError(f"tool {tool_name!r} has no description")
            if not read_only and not _returns(fn, Mutation):
                raise TypeError(f"mutating tool {tool_name!r} must return the Mutation envelope")
            if read_only and destructive:
                raise ValueError(f"tool {tool_name!r} cannot be both read-only and destructive")
            annotations = ToolAnnotations(title=title, read_only_hint=read_only, destructive_hint=destructive,
                                          idempotent_hint=idempotent, open_world_hint=False)
            self._tools[tool_name] = ToolSpec(name=tool_name, fn=fn, role=role, annotations=annotations,
                                              description=inspect.cleandoc(fn.__doc__), title=title)
            return fn

        return decorate

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def mount(self, server: MCPServer) -> None:
        for spec in self._tools.values():
            server.add_tool(spec.fn, name=spec.name, title=spec.title, description=spec.description,
                            annotations=spec.annotations)

    def required_role(self, name: str) -> Role | None:
        """The role a call to `name` needs, or None for a name this registry does not
        know (an SDK tool added without a declaration, a typo, a probe).

        An unknown name has no declaration, so there is no role to name: `allowed`
        already refuses it for every role (default deny), and the gates answer with
        the not-offered refusal — which names no role and no scope — instead of
        inventing one no token could ever satisfy."""
        spec = self._tools.get(name)
        return spec.role if spec is not None else None

    def allowed(self, name: str, token: AccessToken | None) -> bool:
        spec = self._tools.get(name)
        if spec is None:
            return False
        return token is not None and spec.role.scope in token.scopes

    def bind_resource(self, uri: str, tool: str) -> None:
        """Map an exact orbit:// URI (or a prefix ending in /) to the tool that backs it."""
        if uri.endswith("/"):
            self._resource_prefixes.append((uri, tool))
        else:
            self._resource_tools[uri] = tool

    def tool_for_resource(self, uri: str) -> str | None:
        text = str(uri or "")
        if text in self._resource_tools:
            return self._resource_tools[text]
        for prefix, tool in self._resource_prefixes:
            if text.startswith(prefix):
                return tool
        return None

    def resource_allowed(self, uri: str, token: AccessToken | None) -> bool:
        tool = self.tool_for_resource(uri)
        if tool is None:
            return False
        return self.allowed(tool, token)


def _returns(fn: Callable[..., Any], model: type) -> bool:
    ann = typing.get_type_hints(fn).get("return")
    if ann is None:
        return False
    candidates = typing.get_args(ann) or (ann,)
    return any(c is model for c in candidates)


# ------------------------------------------------------------------ the gate

def enable_hint(feature: str) -> str:
    from .features import enable_hint as hint   # features imports backends; keep roles light
    return hint(feature)


class RoleGate:
    """Handler-tier gate (mcp `ServerMiddleware`): the control, regardless of transport.

    `tools/list` is filtered to what the token may call (a viewer never sees the write
    tools, per §3.4). `tools/call` on a tool above the token's role returns an
    `isError` result naming the role, so a model can act on it; `tools/call` on a
    name the registry does not declare returns a different `isError` result that
    names no role and no scope — no token could carry it, so none is pointed at —
    and says what the caller can do instead. This gate is the only one that
    answers authenticated calls: the HTTP-tier middleware that used to refuse
    them with a 403 in front of the protocol is gone.
    """

    def __init__(self, registry: Registry, features: Any = None) -> None:
        self.registry = registry
        # orbitd.features.Features, or None (a gate built without one offers every tool)
        self.features = features

    def _feature_off(self, name: str | None) -> str | None:
        """The optional feature that owns this tool and is off, or None."""
        if self.features is None or not name:
            return None
        return self.features.off_owner(name)

    async def __call__(self, ctx: ServerRequestContext[Any, Any], call_next: CallNext) -> HandlerResult:
        token = get_access_token()
        if ctx.method == "tools/list":
            # call_next hands back the wire form (a dict), already shaped by the SDK.
            result = await call_next(ctx)
            if isinstance(result, dict) and isinstance(result.get("tools"), list):
                # by role, then by feature: a tool whose feature is off does not exist here
                result["tools"] = [t for t in result["tools"]
                                   if self.registry.allowed(str(t.get("name")), token)
                                   and self._feature_off(str(t.get("name"))) is None]
            return result
        if ctx.method == "tools/call":
            name = str((ctx.params or {}).get("name", ""))
            actor = token.subject if token else "-"
            held = role_of_scopes(token.scopes) if token else None
            role = held.value if held else "-"
            if not self.registry.allowed(name, token):
                # Default deny: a name without a registry declaration is refused for every
                # role, before dispatch, whether or not the SDK knows a tool by that name.
                needs = self.registry.required_role(name)
                if needs is None:
                    # Not offered: there is no role to name and no scope that would
                    # carry this call, so the refusal names none and points at
                    # tools/list instead. A different refusal from the one above.
                    # The name is caller text: it stays in its own field, where
                    # it may say anything, and the sink renders it; the
                    # refusal body echoes it bounded.
                    log_audit(log, "unknown tool %s", name,
                              actor=actor, role=role, tool=name, verdict="unknown_tool")
                    return failure(
                        "UNKNOWN_TOOL",
                        f"tool {refusal_echo(name)} is not offered by this surface",
                        next_action="list the tools this surface offers with tools/list",
                    ).model_dump(by_alias=True, mode="json", exclude_none=True)
                log_audit(log, "forbidden tool %s needs %s", name, needs.value,
                          actor=actor, role=role, tool=name, verdict="forbidden",
                          needs=needs.value)
                return failure(
                    "FORBIDDEN",
                    # The role this call needed and the role the token holds travel as
                    # the fields below, and the desk writes the sentence from them. Saying
                    # it again here in prose put the desk's own words in text arriving over
                    # a wire, which the desk cannot tell from a host claiming a role: the
                    # commonest refusal an operator meets came back marked as a forgery.
                    f"tool {refusal_echo(name)} is above this token's role",
                    next_action=f"present a token whose scope includes {needs.scope}",
                    required_role=needs.value, required_scope=needs.scope, held_role=role,
                ).model_dump(by_alias=True, mode="json", exclude_none=True)
            feature = self._feature_off(name)
            if feature is not None:
                # The role would carry this call; the platform does not run what it needs.
                # Said after the role check, so a token that could not call the tool
                # anyway learns nothing about which features are on.
                log_audit(log, "tool %s needs feature %s, which is off", name, feature,
                          actor=actor, role=role, tool=name, verdict="feature_off", feature=feature)
                return failure(
                    "FEATURE_OFF",
                    f"tool {name} needs the {feature} feature, which is off on this platform",
                    next_action=enable_hint(feature),
                    feature=feature,
                ).model_dump(by_alias=True, mode="json", exclude_none=True)
            result = await call_next(ctx)
            verdict = "error" if isinstance(result, dict) and result.get("isError") else "ok"
            log_audit(log, "tool %s %s", name, verdict,
                      actor=actor, role=role, tool=name, verdict=verdict)
            return result
        if ctx.method == "resources/list":
            result = await call_next(ctx)
            return self._filter_resources(result, token)
        if ctx.method == "resources/templates/list":
            result = await call_next(ctx)
            return self._filter_templates(result, token)
        if ctx.method == "resources/read":
            uri = str((ctx.params or {}).get("uri", ""))
            name = self.registry.tool_for_resource(uri)
            actor = token.subject if token else "-"
            held = role_of_scopes(token.scopes) if token else None
            role = held.value if held else "-"
            if not self.registry.resource_allowed(uri, token):
                needs = self.registry.required_role(name) if name is not None else None
                if needs is None:
                    # No declaration backs this URI (unmapped, or mapped to a name
                    # the registry does not know): nothing is offered here, so no
                    # role or scope is named.
                    log_audit(log, "unknown resource %s", uri,
                              actor=actor, role=role, resource=uri, verdict="unknown_tool")
                    return failure(
                        "UNKNOWN_TOOL",
                        f"resource {refusal_echo(uri)} is not offered by this surface",
                        next_action="list the resources this surface offers with resources/list",
                    ).model_dump(by_alias=True, mode="json", exclude_none=True)
                assert name is not None
                log_audit(log, "forbidden resource %s needs %s", uri, needs.value,
                          actor=actor, role=role, tool=name, resource=uri,
                          verdict="forbidden", needs=needs.value)
                return failure(
                    "FORBIDDEN",
                    # Same refusal, same reason as above: the roles are fields, not prose.
                    f"tool {name} is above this token's role",
                    next_action=f"present a token whose scope includes {needs.scope}",
                    required_role=needs.value, required_scope=needs.scope, held_role=role,
                ).model_dump(by_alias=True, mode="json", exclude_none=True)
            result = await call_next(ctx)
            verdict = "error" if isinstance(result, dict) and result.get("isError") else "ok"
            log_audit(log, "resource %s %s", uri, verdict,
                      actor=actor, role=role, tool=name, resource=uri, verdict=verdict)
            return result
        return await call_next(ctx)

    def _uri_of(self, item: Any) -> str:
        if isinstance(item, dict):
            return str(item.get("uri") or item.get("uriTemplate") or item.get("uri_template") or "")
        return str(getattr(item, "uri", None)
                   or getattr(item, "uriTemplate", None)
                   or getattr(item, "uri_template", None)
                   or "")

    def _filter_resources(self, result: Any, token: AccessToken | None) -> Any:
        if isinstance(result, dict) and isinstance(result.get("resources"), list):
            result["resources"] = [
                r for r in result["resources"] if self.registry.resource_allowed(self._uri_of(r), token)
            ]
            return result
        resources = getattr(result, "resources", None)
        if isinstance(resources, list):
            result.resources = [
                r for r in resources if self.registry.resource_allowed(self._uri_of(r), token)
            ]
        return result

    def _filter_templates(self, result: Any, token: AccessToken | None) -> Any:
        key = "resourceTemplates" if isinstance(result, dict) and "resourceTemplates" in result else "resource_templates"
        if isinstance(result, dict) and isinstance(result.get(key), list):
            result[key] = [
                r for r in result[key] if self.registry.resource_allowed(self._uri_of(r), token)
            ]
            return result
        templates = getattr(result, "resource_templates", None) or getattr(result, "resourceTemplates", None)
        if isinstance(templates, list):
            filtered = [r for r in templates if self.registry.resource_allowed(self._uri_of(r), token)]
            if hasattr(result, "resource_templates"):
                result.resource_templates = filtered
            elif hasattr(result, "resourceTemplates"):
                result.resourceTemplates = filtered
        return result
