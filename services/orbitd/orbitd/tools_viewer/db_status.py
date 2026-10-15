"""orbit_db_status — CloudNativePG cluster health for a project environment."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, Field

from orbitd.backends import CNPG_GROUP, CNPG_PLURAL, CNPG_VERSION
from orbitd.roles import Registry, Role
from orbitd.state import AppState

from .util import Ident
from .common import as_dict, find_project, guarded, not_found, ns, pick, relayed, require_env


OBJECTSTORE_GROUP, OBJECTSTORE_VERSION, OBJECTSTORE_PLURAL = "barmancloud.cnpg.io", "v1", "objectstores"
HIBERNATION = "cnpg.io/hibernation"
FILE_BACKUPS = "db-file-backups.json"
log = logging.getLogger("orbitd.db")


class FileBackup(BaseModel):
    name: str = Field(description="the file's name: what orbit_db_restore takes as `backup`")
    at: str = Field(description="RFC 3339 time this orbitd saw it made and proven")


class DbStatus(BaseModel):
    project: str
    env: str
    cluster: str
    state: str = Field(
        default="unknown",
        description="`ready` (every instance is up), `not ready` (an instance is down or starting), or "
                    "`hibernated` (switched off on purpose: no pod runs). Read this, not `phase`")
    phase: str | None = Field(default=None, description="the operator's own word; it can lag a database that is down")
    instances: int | None = None
    ready: int = Field(default=0, description="instances that are up; 0 while the database is down")
    primary: str | None = None
    archiving: bool = Field(
        default=False,
        description="whether this database's write-ahead log is archived to the bucket: true for a prod "
                    "database with the backups feature on. Only then is there a point in time to restore to")
    wal_archive: str | None = Field(
        default=None,
        description="the archive's health (`True`: working) when `archiving`; null when nothing is archived")
    first_recoverability_point: str | None = Field(
        default=None,
        description="the earliest moment orbit_db_restore(to=...) can restore to; null when nothing is "
                    "archived, or before the first base backup has been taken")
    last_successful_backup: str | None = Field(
        default=None, description="the newest base backup in the bucket; null when nothing is archived")
    last_file_backup: FileBackup | None = Field(
        default=None,
        description="the newest backup FILE made through orbit_db_backup (or before a restore) that this "
                    "orbitd has seen; null when it has seen none. Files made by the nightly dump of a "
                    "database without an archive are not listed here")


def register(registry: Registry, state: AppState) -> None:

    @registry.tool(role=Role.viewer, read_only=True, idempotent=True)
    def orbit_db_status(
        project: Annotated[Ident, Field(description="Project id from orbit_project_list.")],
        env: Annotated[Ident, Field(description="Environment name, e.g. prod or dev.")],
    ) -> DbStatus:
        """Read the CloudNativePG Cluster `<project>-db` in namespace `<project>-<env>`.
        `state` is the answer to "is it up": `ready`, `not ready` or `hibernated`, from
        the instances that are up (`ready` of `instances`), not from the operator's
        `phase`, which can still read healthy for a database that is down. `archiving`
        says whether its write-ahead log goes to the bucket (a prod database with the
        backups feature on); when it does, `wal_archive` is the archive's health,
        `first_recoverability_point` the earliest moment orbit_db_restore(to=...) can
        restore to and `last_successful_backup` the newest base backup, both read from
        the archive's own record (the barman ObjectStore's recovery window).
        `last_file_backup` is the newest backup file made through orbit_db_backup.
        Fails `not_found` when the catalog entry declares no database.
        Example: orbit_db_status(project="demo", env="prod")."""
        return guarded(lambda: _status(state, project, env))


def _status(state: AppState, project: str, env: str) -> DbStatus:
    entry = find_project(state, project)
    if entry is None:
        return not_found(f"project {project!r} is not in the catalog", project=project)
    if (missing := require_env(entry, project, env)) is not None:
        return missing
    if not entry.get("database"):
        return not_found(
            f"project {project!r} declares no database",
            project=project, env=env,
        )
    name = f"{project}-db"
    namespace = ns(project, env)
    cluster = state.backends.kube.custom.get_namespaced_custom_object(
        CNPG_GROUP, CNPG_VERSION, namespace, CNPG_PLURAL, name,
    )
    data = as_dict(cluster)
    status = as_dict(pick(data, "status") or {})
    instances = _as_int(pick(status, "instances"))
    ready = _as_int(pick(status, "readyInstances", "ready_instances")) or 0
    annotations = as_dict(pick(as_dict(pick(data, "metadata") or {}), "annotations") or {})
    if str(annotations.get(HIBERNATION) or "").lower() == "on":
        up = "hibernated"
    elif instances and ready >= instances:
        up = "ready"
    else:
        up = "not ready"
    # The operator keeps the recovery window on the Cluster only for its built-in
    # backups. With the barman plugin (what the chart renders) it is on the ObjectStore,
    # so both fields were null for every database, also one with three base backups
    # (QA of v1.0.0, F30).
    first = pick(status, "firstRecoverabilityPoint", "first_recoverability_point")
    last = pick(status, "lastSuccessfulBackup", "last_successful_backup",
                "lastSuccessfulBackupTime", "last_successful_backup_time")
    # archived: the spec says so, or the operator itself records a window (it does only
    # for a database it archives)
    archiving = archives(data) or bool(first or last)
    if archiving and not (first and last):
        window = recovery_window(state.backends.kube, namespace, name)
        first, last = first or window[0], last or window[1]
    return DbStatus(
        project=project,
        env=env,
        cluster=name,
        state=up,
        phase=_text(pick(status, "phase")),
        instances=instances,
        ready=ready,
        primary=_text(pick(status, "currentPrimary", "current_primary", "targetPrimary",
                           "target_primary")),
        archiving=archiving,
        first_recoverability_point=_text(first) if archiving else None,
        last_successful_backup=_text(last) if archiving else None,
        # a database that archives nothing has no archive to be healthy: the operator's
        # condition reads True there too, which looked like a dev database being archived
        wal_archive=_text(_wal(status)) if archiving else None,
        last_file_backup=last_file_backup(state, namespace, name),
    )


def archives(cluster: dict[str, Any]) -> bool:
    """Whether the Cluster's write-ahead log is archived: a plugin that is its WAL
    archiver, or the operator's own barman backup."""
    spec = as_dict(pick(cluster, "spec") or {})
    for raw in pick(spec, "plugins") or []:
        plugin = as_dict(raw)
        if pick(plugin, "isWALArchiver", "is_wal_archiver") and pick(plugin, "enabled", default=True) is not False:
            return True
    return bool(pick(as_dict(pick(spec, "backup") or {}), "barmanObjectStore", "barman_object_store"))


def recovery_window(kube: Any, namespace: str, cluster: str) -> tuple[str | None, str | None]:
    """(earliest restorable moment, newest base backup) of `cluster`, from the barman
    ObjectStores of its namespace; (None, None) when none records one or they cannot be
    read."""
    try:
        listed = kube.custom.list_namespaced_custom_object(
            OBJECTSTORE_GROUP, OBJECTSTORE_VERSION, namespace, OBJECTSTORE_PLURAL)
    except Exception:
        return None, None
    for store in (listed or {}).get("items") or []:
        windows = as_dict(pick(as_dict(pick(as_dict(store), "status") or {}),
                               "serverRecoveryWindow", "server_recovery_window") or {})
        window = as_dict(windows.get(cluster) or {})
        first = pick(window, "firstRecoverabilityPoint", "first_recoverability_point")
        last = pick(window, "lastSuccessfulBackupTime", "last_successful_backup_time")
        if first or last:
            return (str(first) if first else None), (str(last) if last else None)
    return None, None


def _file_backups_path(state: AppState) -> Path:
    from ..shadow import state_dir_of
    return Path(state_dir_of(state)) / FILE_BACKUPS


def record_file_backup(state: AppState, namespace: str, cluster: str, name: str) -> None:
    """Remember the newest backup file of one database (orbit_db_backup, and the backup a
    restore takes first). Kept in orbitd's state directory; losing it loses no backup."""
    try:
        path = _file_backups_path(state)
        try:
            known = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            known = {}
        if not isinstance(known, dict):
            known = {}
        known[f"{namespace}/{cluster}"] = {
            "name": name, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(known, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
    except Exception as e:      # a note for orbit_db_status; never a reason to fail a backup
        log.warning("the backup file of %s/%s could not be noted (%s)", namespace, cluster, e.__class__.__name__)


def forget_file_backups(state: AppState, namespaces: list[str]) -> None:
    """Drop the note of every database in `namespaces`: they were removed with their
    project. The files stay on the backups volume (the removal names them); a project
    made again under the same name is another database and starts with no backup of its
    own (v1.0.1-rc3 on the test box: it showed the removed one's)."""
    try:
        path = _file_backups_path(state)
        known = json.loads(path.read_text(encoding="utf-8"))
        kept = {k: v for k, v in known.items() if k.split("/", 1)[0] not in set(namespaces)}
        if kept != known:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(kept, sort_keys=True), encoding="utf-8")
            tmp.replace(path)
    except Exception:
        pass


def last_file_backup(state: AppState, namespace: str, cluster: str) -> FileBackup | None:
    try:
        row = json.loads(_file_backups_path(state).read_text(encoding="utf-8")).get(f"{namespace}/{cluster}")
        return FileBackup(name=str(row["name"]), at=str(row["at"]))
    except Exception:
        return None


def _text(value: Any) -> str | None:
    return None if value is None else relayed(value, limit=200)


def _wal(status: dict[str, Any]) -> str | None:
    for cond in pick(status, "conditions") or []:
        item = as_dict(cond)
        if pick(item, "type") == "ContinuousArchiving":
            return pick(item, "status")
    return pick(status, "walArchiveStatus", "wal_archive_status")


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
