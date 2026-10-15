"""orbit_render_get — generated Application for one project/env pair."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, Field

from mcp.server.mcpserver import Context

from ..backends import BackendError, BackendUnconfigured
from ..roles import CheckoutSource, Registry, Role, failure
from ..source import read_source
from ..state import AppState
from ._util import ID_RE

ORBIT_REL = "clusters/orbit/{project}-{env}.yaml"
APPS_REL = "clusters/apps/{project}-{env}.yaml"
RENDER_REL = APPS_REL


class RenderConflict(Exception):
    """The pair's Application is in both clusters/orbit and clusters/apps."""

    def __init__(self, orbit: str, apps: str) -> None:
        self.orbit = orbit
        self.apps = apps
        super().__init__(f"pair is present in both {orbit} and {apps}")


class RenderDoc(BaseModel):
    project: str
    env: str
    path: str
    yaml: str
    source: CheckoutSource


def render_path(project: str, env: str, root: Path | None = None) -> str:
    """Relative Application path: clusters/orbit first, then clusters/apps.

    A pair present in both is an error naming both. With no `root`, returns the
    apps path (the historical location) so callers that only need a name still work.
    """
    orbit = ORBIT_REL.format(project=project, env=env)
    apps = APPS_REL.format(project=project, env=env)
    if root is None:
        return apps
    orbit_exists = (root / orbit).is_file()
    apps_exists = (root / apps).is_file()
    if orbit_exists and apps_exists:
        raise RenderConflict(orbit, apps)
    if orbit_exists:
        return orbit
    return apps


def read_render(state: AppState, project: str, env: str) -> str:
    root = state.backends.instance.require()
    rel = render_path(project, env, root)
    path = root / rel
    try:
        return path.read_text()
    except OSError as e:
        raise BackendError(status=None, body=str(e),
                           what_failed=f"reading {path.name} failed") from e


def register(registry: Registry, state: AppState) -> None:

    @registry.tool(role=Role.viewer, read_only=True, idempotent=True)
    def orbit_render_get(
        project: Annotated[str, Field(pattern=ID_RE.pattern,
                                      description="Catalog project id; the generated file is project-env.yaml.")],
        env: Annotated[str, Field(pattern=ID_RE.pattern,
                                  description="Environment id (prod, dev, ...).")],
        ctx: Context = None,  # type: ignore[assignment]
    ) -> RenderDoc:
        """Return the generated Application manifest for one project/environment
        pair, read from `clusters/orbit/<project>-<env>.yaml` if present, else
        `clusters/apps/<project>-<env>.yaml` in the instance checkout. Fails
        `not_found` when that file has not been generated, and `conflict` when
        both locations exist. `source` is {commit, age_s, lag_note} for the
        git-sync checkout this call read.
        Example: orbit_render_get(project="demo", env="prod") -> {path:
        "clusters/apps/demo-prod.yaml", yaml: "apiVersion: ..."}."""
        try:
            root = state.backends.instance.require()
            rel = render_path(project, env, root)
            text = read_render(state, project, env)
        except BackendUnconfigured as e:
            return e.as_failure()  # type: ignore[return-value]
        except RenderConflict as e:
            return failure(
                "conflict",
                str(e),
                next_action="leave the Application in exactly one of clusters/orbit or clusters/apps",
                project=project, env=env, path=e.orbit, other=e.apps,
            )
        except BackendError:
            rel = render_path(project, env)
            return failure(
                "not_found",
                f"{rel} is not in the instance dir",
                next_action="generate the pair with catalog-sync, then retry",
                project=project, env=env, path=rel,
            )
        return RenderDoc(project=project, env=env, path=rel, yaml=text,
                         source=read_source(state))
