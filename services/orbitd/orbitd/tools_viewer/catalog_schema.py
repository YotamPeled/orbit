"""orbit_catalog_schema — the vendored chart JSON Schema."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from mcp.server.mcpserver import Context

from ..backends import BackendError, BackendUnconfigured
from ..roles import Registry, Role, failure
from ..state import AppState

SCHEMA_REL = "charts/service/values.schema.json"


# The shape of one entry of catalog/projects.yaml: what orbit_catalog_get returns and
# orbit_catalog_set takes. The chart's schema below describes the VALUES the generator
# renders (project, env, hostnames...), which is not how an entry is written (id,
# environments, components...): an author had to map one onto the other by hand (QA of
# v1.0.0, F7). The key sets are the generator's own (scripts/catalog-sync.py:
# PROJECT_KEYS, ENV_KEYS, COMPONENT_OVERRIDE_KEYS, GENERATOR_COMPONENT_KEYS); a test
# holds the two together.
ENTRY_SHAPE: dict[str, Any] = {
    "id": "the project id: a DNS label; each environment's namespace is <id>-<env>. Cannot change",
    "name": "display name",
    "blurb": "one line about the project",
    "repo": "<owner>/<repository> its images are built from",
    "tags": "a list of words",
    "hosted": "`self` (Orbit runs it; the default) or another word for a project that only reserves hostnames",
    "managed_elsewhere": "true for a project hosted somewhere else",
    "showcase": "true to list it on the portfolio page",
    "internal": "true to keep it off the portfolio page",
    "url": "an externally hosted project's address",
    "database": "{size, memory, ...}: a Postgres per environment, described by `chart.properties.database`; "
                "its connection values are the Secret <id>-db-app (keys host, port, username, password, dbname)",
    "secrets": "[{name, keys: [...]}]: every Secret a component's env points into, by name and key; "
               "values never appear in the catalog",
    "environments": {
        "<env>": {
            "enabled": "true to run an environment other than prod (prod always runs)",
            "branch": "the git branch its images are built from",
            "hostnames": "names under the instance's own domains; left out, <id>.<primary domain> for prod "
                         "and <id>.<dev domain> otherwise; `[]` for no public door",
            "customDomains": "[{host}]: a domain of the project's own",
            "expose": "false for no public door at all",
            "components": {"<component name>": "this environment's own cpu, drain, env, memory, memoryLimit, "
                                               "replicas (env merges key by key; a null value removes a key)"},
        },
    },
    "components": "a list; each item is `chart.properties.components.items` (name, kind web|worker|cron, image, "
                  "port, path, memory, env, schedule, ...) plus three keys the generator reads itself: `repo` "
                  "(the repository that builds it), `environments` (the environments it runs in; all when left "
                  "out) and `hosts` (the hostnames a web component answers on; all when left out). No `tag`: "
                  "orbit_deploy_ship writes tags",
}
ENTRY_KEYS = frozenset(ENTRY_SHAPE)
ENVIRONMENT_KEYS = frozenset(ENTRY_SHAPE["environments"]["<env>"])
COMPONENT_OVERRIDE_KEYS = frozenset({"cpu", "drain", "env", "memory", "memoryLimit", "replicas"})
GENERATOR_COMPONENT_KEYS = frozenset({"repo", "environments", "hosts"})


class CatalogSchema(BaseModel):
    path: str
    body: str = Field(description="the chart's JSON Schema, as text: `properties.components.items` is one "
                                  "component, `properties.database` the database block")
    entry: dict[str, Any] = Field(
        default_factory=dict,
        description="the keys of one catalog entry (what orbit_catalog_set takes), each with what it holds")


def read_schema(state: AppState) -> str:
    root = state.backends.instance.require()
    path = root / SCHEMA_REL
    try:
        return path.read_text()
    except OSError as e:
        raise BackendError(status=None, body=str(e),
                           what_failed=f"reading {SCHEMA_REL} failed") from e


def register(registry: Registry, state: AppState) -> None:

    @registry.tool(role=Role.viewer, read_only=True, idempotent=True)
    def orbit_catalog_schema(ctx: Context) -> CatalogSchema:
        """What a catalog entry may hold. `entry` is the shape of one entry of
        catalog/projects.yaml, the thing orbit_catalog_get returns and orbit_catalog_set
        takes: its keys (id, database, secrets, environments, components...), each with
        what it holds, and under `environments.<env>` the keys an environment takes.
        `body` is the JSON Schema the generator validates a component and the database
        against (`charts/service/values.schema.json` in the instance checkout): one item
        of `components` is `properties.components.items` there, `database` is
        `properties.database`. The schema's top-level keys (project, env, hostnames...)
        are the values the generator renders, not keys of an entry. Example:
        orbit_catalog_schema() -> {path: "charts/service/values.schema.json",
        body: "{\\"$schema\\": ...}", entry: {id: "...", environments: {...}}}."""
        try:
            text = read_schema(state)
        except BackendUnconfigured as e:
            return e.as_failure()  # type: ignore[return-value]
        except BackendError as e:
            if "failed" in e.what_failed:
                return failure(
                    "not_found",
                    f"{SCHEMA_REL} is not in the instance dir",
                    next_action="vendor the service chart into the instance checkout",
                    path=SCHEMA_REL,
                )
            return e.as_failure()  # type: ignore[return-value]
        return CatalogSchema(path=SCHEMA_REL, body=text, entry=ENTRY_SHAPE)
