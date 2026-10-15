"""orbit_db_backup, orbit_db_restore and orbit_project_remove (docs/V1.md M2c), offline:
who may call each, what is refused before anything runs, the confirmation round, the one
audit line, the Job each starts, and what the tool says for every way the script can end.
The scripts themselves are proven on a real cluster (docs/V1.md, scenarios S07 and S17)."""

from __future__ import annotations

import pytest
from mcp.shared.exceptions import MCPError
from mcp_types import ElicitResult, InputRequiredResult

from dataclasses import replace

from orbitd import deploy as deploy_module
from orbitd import desk_job
from orbitd import source as source_module
from orbitd.features_converge import RECORD_NS
from orbitd.roles import CheckoutSource, Role
from orbitd.testing import FakeRunner

from conftest import mint, sdk_client
from test_features_gate import OPTIONAL, write_features
from test_tools_operator import install, wait_result
from test_tools_viewer_kube import seed_catalog

pytestmark = pytest.mark.anyio

COMMIT = "0123456789abcdef0123456789abcdef01234567"
SETTINGS = {"ORBITD_DESK_IMAGE": "registry.invalid/orbit-desk:v9", "ORBITD_GIT_SYNC_IMAGE": "registry.invalid/git-sync:v4",
            "ORBITD_INSTANCE_REPO": "https://github.com/fixture-org/fixture-instance.git"}
DEMO = {"id": "demo", "database": {"size": "1Gi"}, "environments": {"prod": {}, "dev": {"enabled": True}},
        "components": [{"name": "web", "kind": "web", "image": "mirror.invalid/demo-web"}]}
PLAIN = {"id": "plain", "environments": {"prod": {}},
         "components": [{"name": "web", "kind": "web", "image": "mirror.invalid/plain-web"}]}
BACKUP_FILE = "demo-db-20261006T101500Z.sql.gz"
BACKUP_LOG = f"""== dumping demo-prod/demo-db (database demo) from demo-db-1
== restoring the dump into a scratch database on the same server
table | rows in the database | rows restored from the dump
  public.games 207 207
  public.winners 3 3
verified: 2 table(s), 210 row(s), 4096 bytes, sha256 0123456789abcdef; kept in /var/lib/orbit-backups/demo-prod/{BACKUP_FILE}
BACKUP OK: /var/lib/orbit-backups/demo-prod/{BACKUP_FILE}
"""
REFUSED_LOG = """== dumping demo-prod/demo-db (database demo) from demo-db-1
== restoring the dump into a scratch database on the same server
BACKUP REFUSED: the dump of demo-prod/demo-db does not restore ( ERROR:  new row for relation "games" violates check constraint "ok") — it is not a backup (kept as demo-db-20261006T101500Z.sql.gz.refused)
"""
TOOLS = {"orbit_db_backup": ({"project": "demo", "env": "prod"}, Role.operator),
         "orbit_db_restore": ({"project": "demo", "env": "prod", "backup": "latest"}, Role.admin),
         "orbit_project_remove": ({"project": "gone"}, Role.admin)}
YES = {"confirm": ElicitResult(action="accept", content={"confirm": True})}


def world(state, monkeypatch, *, settings=True, runner=None):
    """A catalog with a project that has a database and one that has none, a project
    ("gone") that left two namespaces and a database behind, orbitd told its desk image,
    and a checkout at COMMIT."""
    seed_catalog(state, [DEMO, PLAIN])
    kube = state.backends.kube
    kube.namespaces += [
        {"metadata": {"name": "gone-prod", "labels": {"orbit.local/project": "gone", "orbit.local/env": "prod"}}},
        {"metadata": {"name": "gone-dev", "labels": {"orbit.local/project": "gone", "orbit.local/env": "dev"}}},
        {"metadata": {"name": "demo-prod", "labels": {"orbit.local/project": "demo", "orbit.local/env": "prod"}}},
    ]
    kube.add_cluster(namespace="gone-prod", name="gone-db")
    kube.add_cluster(namespace="demo-prod", name="demo-db")
    for key, value in SETTINGS.items():
        if settings:
            monkeypatch.setenv(key, value)
        else:
            monkeypatch.delenv(key, raising=False)
    at_commit = lambda state, *, now=None: CheckoutSource(commit=COMMIT, age_s=0, lag_note="test")  # noqa: E731
    monkeypatch.setattr(deploy_module, "read_source", at_commit)
    from orbitd.tools_operator import guardrails
    monkeypatch.setattr(guardrails, "read_source", at_commit)
    monkeypatch.setattr(source_module, "read_head", at_commit)
    rt, clock = install(state, runner=runner)
    state.cfg = replace(state.cfg, apply_mode="on")
    return kube, rt, clock


def jobs(kube) -> list[dict]:
    return [o for o in kube.objects if o.get("kind") == "Job"]


def claims(kube) -> list[dict]:
    return [o for o in kube.objects if o.get("kind") == "PersistentVolumeClaim"]


def finish(kube, clock, log: str, *, outcome="Complete"):
    """Make the fake cluster run a desk Job: on the first wait it ends, and its pod's log
    is what the script printed."""
    real_sleep = clock.sleep

    def sleep(seconds: float) -> None:
        real_sleep(seconds)
        for job in jobs(kube):
            if job.get("status"):
                continue
            name = job["metadata"]["name"]
            job["status"] = {"conditions": [{"type": outcome, "status": "True"}]}
            kube.add_pod(namespace=RECORD_NS, name=f"{name}-pod", labels={"job-name": name})
            kube.pod_logs[(RECORD_NS, f"{name}-pod")] = log

    return sleep


def audit_lines(caplog, tool: str) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name.startswith("orbitd") and f" {tool} " in r.getMessage()]


# ---------------------------------------------------------------- who may call, and the confirmation

@pytest.mark.parametrize("tool", sorted(TOOLS))
async def test_a_lower_role_is_refused_and_nothing_starts(fake_backends, http, monkeypatch, tool):
    kube, _, _ = world(fake_backends, monkeypatch)
    args, needs = TOOLS[tool]
    lower = [Role.viewer] if needs == Role.operator else [Role.viewer, Role.operator]
    for role in lower:
        async with sdk_client(http, mint(fake_backends, role), confirm=True) as client:
            r = await client.call_tool(tool, args)
        assert r.is_error and r.structured_content["code"] == "FORBIDDEN", (role, r.structured_content)
        assert r.structured_content["evidence"]["required_role"] == needs.value
    assert jobs(kube) == [] and claims(kube) == []


@pytest.mark.parametrize("tool", sorted(TOOLS))
async def test_without_the_confirmation_round_nothing_starts(fake_backends, http, monkeypatch, tool):
    kube, _, _ = world(fake_backends, monkeypatch)
    args, _ = TOOLS[tool]
    async with sdk_client(http, mint(fake_backends, Role.admin)) as client:
        first = await client.session.call_tool(tool, args, allow_input_required=True)
    assert isinstance(first, InputRequiredResult)
    message = first.input_requests["confirm"].params.message
    assert ("gone" if tool == "orbit_project_remove" else "demo/prod") in message
    assert jobs(kube) == []
    async with sdk_client(http, mint(fake_backends, Role.admin), confirm=None) as client:
        r = await client.call_tool(tool, args)
    assert r.structured_content["status"] == "refused"
    assert jobs(kube) == [] and claims(kube) == []


@pytest.mark.parametrize("tool", sorted(TOOLS))
async def test_a_confirmation_cannot_be_replayed_for_other_arguments_or_by_another_caller(fake_backends, http, monkeypatch, tool):
    kube, _, _ = world(fake_backends, monkeypatch)
    args, _ = TOOLS[tool]
    other = dict(args, project="plain") if tool == "orbit_project_remove" else dict(args, env="dev")
    async with sdk_client(http, mint(fake_backends, Role.admin, "first")) as client:
        first = await client.session.call_tool(tool, args, allow_input_required=True)
        with pytest.raises(MCPError):
            await client.session.call_tool(tool, other, input_responses=YES, request_state=first.request_state)
    async with sdk_client(http, mint(fake_backends, Role.admin, "second")) as client:
        with pytest.raises(MCPError):
            await client.session.call_tool(tool, args, input_responses=YES, request_state=first.request_state)
        r = await client.session.call_tool(tool, args, input_responses=YES)
    assert r.structured_content["status"] == "refused"
    assert jobs(kube) == []


async def test_the_removal_confirmation_names_every_namespace_and_database(fake_backends, http, monkeypatch):
    world(fake_backends, monkeypatch)
    async with sdk_client(http, mint(fake_backends, Role.admin)) as client:
        first = await client.session.call_tool("orbit_project_remove", {"project": "gone"}, allow_input_required=True)
    message = first.input_requests["confirm"].params.message
    for name in ("gone-dev", "gone-prod", "gone-prod/gone-db"):
        assert name in message
    assert "demo-prod" not in message, "another project's namespace is not this removal's"
    assert "nothing is deleted" in message


# ---------------------------------------------------------------- refused before anything runs

async def test_a_pair_without_a_database_has_nothing_to_back_up_or_restore(fake_backends, http, monkeypatch):
    kube, _, _ = world(fake_backends, monkeypatch)
    async with sdk_client(http, mint(fake_backends, Role.admin), confirm=True) as client:
        for tool, args in (("orbit_db_backup", {"project": "plain", "env": "prod"}),
                           ("orbit_db_restore", {"project": "plain", "env": "prod", "backup": "latest"}),
                           ("orbit_db_backup", {"project": "nope", "env": "prod"}),
                           ("orbit_db_backup", {"project": "demo", "env": "staging"})):
            r = await client.call_tool(tool, args)
            assert r.is_error and r.structured_content["code"] == "not_found", (tool, args, r.structured_content)
    assert jobs(kube) == []


async def test_a_restore_takes_exactly_one_of_a_backup_and_a_moment(fake_backends, http, monkeypatch):
    kube, _, _ = world(fake_backends, monkeypatch)
    async with sdk_client(http, mint(fake_backends, Role.admin), confirm=True) as client:
        neither = await client.call_tool("orbit_db_restore", {"project": "demo", "env": "prod"})
        both = await client.call_tool("orbit_db_restore", {"project": "demo", "env": "prod", "backup": "latest",
                                                            "to": "2026-10-04T15:00:00Z"})
        dev = await client.call_tool("orbit_db_restore", {"project": "demo", "env": "dev", "to": "2026-10-04T15:00:00Z"})
    for r in (neither, both):
        assert r.is_error and r.structured_content["code"] == "refused"
        assert "exactly one" in r.structured_content["what_failed"]
    assert dev.structured_content["code"] == "refused" and "only for a prod database" in dev.structured_content["what_failed"]
    assert jobs(kube) == []


async def test_a_point_in_time_needs_the_backups_feature(fake_backends, http, monkeypatch):
    kube, _, _ = world(fake_backends, monkeypatch)
    write_features(fake_backends, {name: name != "backups" for name in OPTIONAL})
    seed_catalog(fake_backends, [DEMO, PLAIN])
    async with sdk_client(http, mint(fake_backends, Role.admin), confirm=True) as client:
        r = await client.call_tool("orbit_db_restore", {"project": "demo", "env": "prod", "to": "2026-10-04T15:00:00Z"})
    body = r.structured_content
    assert r.is_error and body["code"] == "refused", body
    assert "needs the backups feature, which is off" in body["what_failed"]
    assert "orbit features enable backups" in body["next_action"]
    assert jobs(kube) == []


@pytest.mark.parametrize("backup", ["../../etc/passwd", "demo-db-20261006T101500Z.sql.gz; rm -rf /", "other.sql.gz", "--yes"])
async def test_a_backup_name_is_a_backup_name_or_the_call_never_runs(fake_backends, http, monkeypatch, backup):
    kube, _, _ = world(fake_backends, monkeypatch)
    async with sdk_client(http, mint(fake_backends, Role.admin), confirm=True) as client:
        r = await client.call_tool("orbit_db_restore", {"project": "demo", "env": "prod", "backup": backup})
    assert r.is_error
    assert jobs(kube) == []


async def test_a_project_that_is_nowhere_has_nothing_to_remove(fake_backends, http, monkeypatch):
    kube, rt, _ = world(fake_backends, monkeypatch)
    async with sdk_client(http, mint(fake_backends, Role.admin), confirm=True) as client:
        r = await client.call_tool("orbit_project_remove", {"project": "ghost"})
    assert r.is_error and r.structured_content["code"] == "not_found"
    assert jobs(kube) == [] and rt.runner.calls == []


async def test_an_orbitd_not_told_its_desk_image_says_so(fake_backends, http, monkeypatch):
    kube, _, _ = world(fake_backends, monkeypatch, settings=False)
    async with sdk_client(http, mint(fake_backends, Role.admin), confirm=True) as client:
        r = await client.call_tool("orbit_db_backup", {"project": "demo", "env": "prod"})
    assert r.is_error and r.structured_content["code"] == "backend_unconfigured"
    assert "ORBITD_DESK_IMAGE" in r.structured_content["what_failed"]
    assert jobs(kube) == []


async def test_a_removal_dry_run_lists_what_would_go_and_starts_nothing(fake_backends, http, monkeypatch):
    kube, _, _ = world(fake_backends, monkeypatch)
    async with sdk_client(http, mint(fake_backends, Role.admin)) as client:
        r = await client.call_tool("orbit_project_remove", {"project": "gone", "dry_run": True})
    body = r.structured_content
    assert body["status"] == "dry_run"
    assert body["evidence"]["namespaces"] == ["gone-dev", "gone-prod"]
    assert body["evidence"]["databases"] == ["gone-prod/gone-db"]
    assert jobs(kube) == [] and claims(kube) == []


# ---------------------------------------------------------------- the Job each starts

async def test_a_backup_runs_cores_own_script_in_a_job_and_reports_the_proof(fake_backends, http, monkeypatch, caplog):
    kube, rt, clock = world(fake_backends, monkeypatch)
    rt.sleeper = finish(kube, clock, BACKUP_LOG)
    with caplog.at_level("INFO"):
        async with sdk_client(http, mint(fake_backends, Role.operator, "ada"), confirm=True) as client:
            running = await client.call_tool("orbit_db_backup", {"project": "demo", "env": "prod"})
            result, _ = await wait_result(client, running)
    assert result["status"] == "applied", result
    assert [(s["name"], s["status"]) for s in result["steps"]] == [("confirm", "ok"), ("backup", "ok")]
    assert result["evidence"]["backup"] == BACKUP_FILE
    assert result["evidence"]["verified"].startswith("verified: 2 table(s), 210 row(s)")
    assert "  public.games 207 207" in result["evidence"]["output"]
    (job,) = jobs(kube)
    spec = job["spec"]["template"]["spec"]
    main = spec["containers"][0]
    assert main["image"] == SETTINGS["ORBITD_DESK_IMAGE"]
    assert main["command"] == ["bash", "/opt/orbit-core/scripts/db-backup.sh", "demo-prod", "demo-db"], (
        "the image's own copy of the script, with the arguments as an argv list")
    assert spec["serviceAccountName"] == "orbit-platform"
    assert f"--ref={COMMIT}" in spec["initContainers"][0]["args"]
    assert job["spec"]["backoffLimit"] == 0, "a backup is not retried behind the caller's back"
    env = {e["name"]: e for e in main["env"]}
    assert env["ORBIT_BACKUP_DIR"]["value"] == desk_job.BACKUPS_MOUNT
    for name in ("ORBIT_S3_ACCESS_KEY", "ORBIT_S3_SECRET_KEY"):
        ref = env[name]["valueFrom"]["secretKeyRef"]
        assert ref["name"] == "orbit-s3" and ref["optional"] is True, "the bucket keys are referenced, never read"
    assert {"name": "backups", "persistentVolumeClaim": {"claimName": "orbit-backups"}} in spec["volumes"]
    assert main["securityContext"]["runAsNonRoot"] is True and main["securityContext"]["readOnlyRootFilesystem"] is True
    # the volume the backups outlive a namespace on is made on first use, once
    (claim,) = claims(kube)
    assert (claim["metadata"]["name"], claim["metadata"]["namespace"]) == ("orbit-backups", RECORD_NS)
    assert claim["spec"]["resources"]["requests"]["storage"] == "5Gi"
    # one audit line for the accepted change, with who asked
    applied = [line for line in audit_lines(caplog, "orbit_db_backup") if line.endswith(": applied")]
    assert len(applied) == 1, audit_lines(caplog, "orbit_db_backup")


async def test_a_second_backup_reuses_the_volume(fake_backends, http, monkeypatch):
    kube, rt, clock = world(fake_backends, monkeypatch)
    rt.sleeper = finish(kube, clock, BACKUP_LOG)
    async with sdk_client(http, mint(fake_backends, Role.admin), confirm=True) as client:
        for env in ("prod", "dev"):
            running = await client.call_tool("orbit_db_backup", {"project": "demo", "env": env})
            await wait_result(client, running)
    assert len(jobs(kube)) == 2 and len(claims(kube)) == 1


async def test_a_backup_that_does_not_restore_is_a_failure_with_the_scripts_sentence(fake_backends, http, monkeypatch, caplog):
    kube, rt, clock = world(fake_backends, monkeypatch)
    rt.sleeper = finish(kube, clock, REFUSED_LOG, outcome="Failed")
    with caplog.at_level("INFO"):
        async with sdk_client(http, mint(fake_backends, Role.admin), confirm=True) as client:
            running = await client.call_tool("orbit_db_backup", {"project": "demo", "env": "prod"})
            result, _ = await wait_result(client, running)
    assert result["status"] == "failed"
    step = result["steps"][-1]
    assert step["name"] == "backup" and step["status"] == "failed"
    assert step["detail"].startswith("BACKUP REFUSED: the dump of demo-prod/demo-db does not restore")
    assert "backup" not in result["evidence"]
    assert not [line for line in audit_lines(caplog, "orbit_db_backup") if line.endswith(": applied")]


async def test_a_job_that_says_ok_without_succeeding_is_not_believed(fake_backends, http, monkeypatch):
    """The verdict needs both: the Job succeeded AND the script's own last word."""
    kube, rt, clock = world(fake_backends, monkeypatch)
    rt.sleeper = finish(kube, clock, BACKUP_LOG, outcome="Failed")
    async with sdk_client(http, mint(fake_backends, Role.admin), confirm=True) as client:
        running = await client.call_tool("orbit_db_backup", {"project": "demo", "env": "prod"})
        result, _ = await wait_result(client, running)
    assert result["status"] == "failed"
    kube2, rt2, clock2 = kube, rt, clock
    rt2.sleeper = finish(kube2, clock2, "kubectl: connection refused\n")
    async with sdk_client(http, mint(fake_backends, Role.admin), confirm=True) as client:
        running = await client.call_tool("orbit_db_backup", {"project": "demo", "env": "dev"})
        result, _ = await wait_result(client, running)
    assert result["status"] == "failed", "a Job that succeeded without `BACKUP OK:` made no backup"


# ---------------------------------------------------------------- restore

RESTORE_LOG = "== first: a backup of the live database as it is now\n" + "\n".join(BACKUP_LOG.splitlines()[-2:]) + """
== loading demo-db-20261005T000000Z.sql.gz into a scratch database
table | rows live now | rows in demo-db-20261005T000000Z.sql.gz
  public.games 1 207
== replacing the live data (one transaction)
table | rows live after the restore
  public.games 207
every relation is owned by demo; the state before the restore is kept as demo-db-20261006T101500Z.sql.gz
RESTORED: demo-prod/demo-db holds the data of demo-db-20261005T000000Z.sql.gz
"""


async def test_a_restore_backs_up_first_then_restores_and_names_the_way_back(fake_backends, http, monkeypatch, caplog):
    kube, rt, clock = world(fake_backends, monkeypatch)
    rt.sleeper = finish(kube, clock, RESTORE_LOG)
    with caplog.at_level("INFO"):
        async with sdk_client(http, mint(fake_backends, Role.admin), confirm=True) as client:
            running = await client.call_tool("orbit_db_restore", {"project": "demo", "env": "prod",
                                                                  "backup": "demo-db-20261005T000000Z.sql.gz"})
            result, _ = await wait_result(client, running)
    assert result["status"] == "applied", result
    assert [(s["name"], s["status"]) for s in result["steps"]] == [("confirm", "ok"), ("backup", "ok"), ("restore", "ok")]
    assert result["evidence"]["pre_restore_backup"] == BACKUP_FILE
    (job,) = jobs(kube)
    assert job["spec"]["template"]["spec"]["containers"][0]["command"] == [
        "bash", "/opt/orbit-core/scripts/db-restore.sh", "demo", "prod", "--from", "demo-db-20261005T000000Z.sql.gz", "--yes"]
    assert len([line for line in audit_lines(caplog, "orbit_db_restore") if line.endswith(": applied")]) == 1


COMPARE_LOG = """== loading demo-db-20261005T000000Z.sql.gz into a scratch database
table | rows live now | rows in demo-db-20261005T000000Z.sql.gz
  public.games 0 207
nothing changed on the live database. With --yes its data is replaced by demo-db-20261005T000000Z.sql.gz (what was written since is lost; a backup of the live state is taken first).
"""


async def test_a_restore_dry_run_compares_and_changes_nothing(fake_backends, http, monkeypatch):
    """No confirmation, no --yes, one step, and the table an agent reads before restoring."""
    kube, rt, clock = world(fake_backends, monkeypatch)
    rt.sleeper = finish(kube, clock, COMPARE_LOG)
    async with sdk_client(http, mint(fake_backends, Role.admin)) as client:     # no confirmation is answered
        running = await client.call_tool("orbit_db_restore", {"project": "demo", "env": "prod",
                                                              "backup": "demo-db-20261005T000000Z.sql.gz", "dry_run": True})
        result, _ = await wait_result(client, running)
        status = await client.call_tool("orbit_job_status", {"job_id": running.structured_content["evidence"]["job_id"]})
    assert status.structured_content["status"] == "completed", "a comparison that ran is a job that completed"
    assert result["status"] == "dry_run", result
    assert [(s["name"], s["status"]) for s in result["steps"]] == [("compare", "ok")]
    assert "  public.games 0 207" in result["evidence"]["output"]
    (job,) = jobs(kube)
    command = job["spec"]["template"]["spec"]["containers"][0]["command"]
    assert command == ["bash", "/opt/orbit-core/scripts/db-restore.sh", "demo", "prod", "--from", "demo-db-20261005T000000Z.sql.gz"]
    assert "--yes" not in command


async def test_a_dry_run_that_cannot_compare_says_why(fake_backends, http, monkeypatch):
    kube, rt, clock = world(fake_backends, monkeypatch)
    rt.sleeper = finish(kube, clock, "RESTORE REFUSED: there is no backup nope of demo-prod/demo-db on this platform\n", outcome="Failed")
    async with sdk_client(http, mint(fake_backends, Role.admin)) as client:
        running = await client.call_tool("orbit_db_restore", {"project": "demo", "env": "prod", "backup": "latest", "dry_run": True})
        result, _ = await wait_result(client, running)
    assert result["status"] == "failed"
    assert result["steps"][-1]["name"] == "compare" and result["steps"][-1]["detail"].startswith("RESTORE REFUSED")


async def test_a_point_in_time_restore_passes_the_moment(fake_backends, http, monkeypatch):
    kube, rt, clock = world(fake_backends, monkeypatch)
    rt.sleeper = finish(kube, clock, BACKUP_LOG + "RESTORED: demo-prod/demo-db data is as of 2026-10-04T15:00:00Z\n")
    async with sdk_client(http, mint(fake_backends, Role.admin), confirm=True) as client:
        running = await client.call_tool("orbit_db_restore", {"project": "demo", "env": "prod", "to": "2026-10-04T15:00:00Z"})
        result, _ = await wait_result(client, running)
    assert result["status"] == "applied", result
    (job,) = jobs(kube)
    assert job["spec"]["template"]["spec"]["containers"][0]["command"][2:] == ["demo", "prod", "--to", "2026-10-04T15:00:00Z", "--yes"]


async def test_a_restore_whose_backup_is_refused_changes_nothing(fake_backends, http, monkeypatch):
    kube, rt, clock = world(fake_backends, monkeypatch)
    log = ("== first: a backup of the live database as it is now\n" + REFUSED_LOG
           + "RESTORE REFUSED: the live database could not be backed up first, so it is not changed\n")
    rt.sleeper = finish(kube, clock, log, outcome="Failed")
    async with sdk_client(http, mint(fake_backends, Role.admin), confirm=True) as client:
        running = await client.call_tool("orbit_db_restore", {"project": "demo", "env": "prod", "backup": "latest"})
        result, _ = await wait_result(client, running)
    assert result["status"] == "failed"
    steps = {s["name"]: s for s in result["steps"]}
    assert steps["backup"]["status"] == "failed"
    assert steps["restore"]["status"] == "skipped" and "not changed" in steps["restore"]["detail"]
    assert "pre_restore_backup" not in result["evidence"]


async def test_a_restore_that_fails_after_its_backup_names_the_backup(fake_backends, http, monkeypatch):
    kube, rt, clock = world(fake_backends, monkeypatch)
    log = BACKUP_LOG + "RESTORE NOT PROVEN: after the load these tables do not hold the backup's rows: public.games\n"
    rt.sleeper = finish(kube, clock, log, outcome="Failed")
    async with sdk_client(http, mint(fake_backends, Role.admin), confirm=True) as client:
        running = await client.call_tool("orbit_db_restore", {"project": "demo", "env": "prod", "backup": "latest"})
        result, _ = await wait_result(client, running)
    assert result["status"] == "failed"
    steps = {s["name"]: s for s in result["steps"]}
    assert steps["backup"]["status"] == "ok" and steps["restore"]["status"] == "failed"
    assert steps["restore"]["detail"].startswith("RESTORE NOT PROVEN")
    assert result["evidence"]["pre_restore_backup"] == BACKUP_FILE


# ---------------------------------------------------------------- remove

GONE_BACKUP = BACKUP_LOG.replace("demo-prod", "gone-prod").replace("demo-db", "gone-db")


async def test_a_removal_backs_up_every_database_then_deletes(fake_backends, http, monkeypatch, caplog):
    kube, rt, clock = world(fake_backends, monkeypatch)
    rt.sleeper = finish(kube, clock, "== what gone left behind\n" + GONE_BACKUP
                        + "== pruning the services app\nleft: namespaces [] monitoring []\nservices plan: empty=true\n"
                          "REMOVED: gone (backups in /var/lib/orbit-backups)\n")
    with caplog.at_level("INFO"):
        async with sdk_client(http, mint(fake_backends, Role.admin), confirm=True) as client:
            running = await client.call_tool("orbit_project_remove", {"project": "gone"})
            result, _ = await wait_result(client, running)
    assert result["status"] == "applied", result
    assert [(s["name"], s["status"]) for s in result["steps"]] == [
        ("confirm", "ok"), ("commit", "skipped"), ("checkout", "skipped"), ("monitors", "ok"),
        ("backup", "ok"), ("remove", "ok")]
    assert result["evidence"]["backups"] == ["gone-db-20261006T101500Z.sql.gz"]
    (job,) = jobs(kube)
    assert job["spec"]["template"]["spec"]["containers"][0]["command"] == [
        "bash", "/opt/orbit-core/scripts/remove-project.sh", "gone", "--yes"]
    assert len([line for line in audit_lines(caplog, "orbit_project_remove") if line.endswith(": applied")]) == 1


DEMO_BACKUP = BACKUP_LOG


async def test_removing_a_project_still_in_the_catalog_deletes_its_entry_first(fake_backends, http, monkeypatch):
    """One call removes all of it: the catalog entry (one commit by orbitd, accepted by
    the gate), then the backups, then the cluster."""
    kube, rt, clock = world(fake_backends, monkeypatch)
    rt.sleeper = finish(kube, clock, "== what demo left behind\n" + DEMO_BACKUP
                        + "REMOVED: demo (backups in /var/lib/orbit-backups)\n")
    async with sdk_client(http, mint(fake_backends, Role.admin, "ada")) as client:
        first = await client.session.call_tool("orbit_project_remove", {"project": "demo"}, allow_input_required=True)
    message = first.input_requests["confirm"].params.message
    assert "Deletes its entry from the catalog" in message and "demo-prod/demo-db" in message
    assert rt.runner.calls == [], "nothing is committed before the confirmation"
    async with sdk_client(http, mint(fake_backends, Role.admin, "ada"), confirm=True) as client:
        running = await client.call_tool("orbit_project_remove", {"project": "demo"})
        result, _ = await wait_result(client, running)
    assert result["status"] == "applied", result
    assert [(s["name"], s["status"]) for s in result["steps"]] == [
        ("confirm", "ok"), ("commit", "ok"), ("checkout", "ok"), ("monitors", "ok"), ("backup", "ok"), ("remove", "ok")]
    (call,) = rt.runner.calls
    assert call["argv"] == [rt.catalog_script, "remove", "demo"]
    assert call["env"] == {"ORBIT_ACTION": "catalog-remove", "ORBIT_ACTOR": "ada", "ORBIT_ROLE": "admin"}
    (job,) = jobs(kube)
    assert f"--ref={COMMIT}" in job["spec"]["template"]["spec"]["initContainers"][0]["args"], (
        "the Job runs at the commit that removed the entry")
    assert result["evidence"]["commit"] == COMMIT


async def test_a_catalog_commit_that_fails_deletes_nothing(fake_backends, http, monkeypatch):
    kube, rt, _ = world(fake_backends, monkeypatch, runner=FakeRunner(
        returncode=1, stdout="STEP args OK\nSTEP edit FAIL\nproject demo's entry is written on one line ({...}); rewrite it as a block first\n"))
    async with sdk_client(http, mint(fake_backends, Role.admin), confirm=True) as client:
        running = await client.call_tool("orbit_project_remove", {"project": "demo"})
        result, _ = await wait_result(client, running)
    assert result["status"] == "failed"
    steps = {s["name"]: s for s in result["steps"]}
    assert steps["commit"]["status"] == "failed" and "written on one line" in steps["commit"]["detail"]
    assert [steps[n]["status"] for n in ("checkout", "backup", "remove")] == ["skipped"] * 3
    assert jobs(kube) == [], "no Job ran: nothing on the cluster was touched"


async def test_a_removal_whose_backup_is_refused_deletes_nothing(fake_backends, http, monkeypatch):
    kube, rt, clock = world(fake_backends, monkeypatch)
    rt.sleeper = finish(kube, clock, "== what gone left behind\n" + REFUSED_LOG.replace("demo", "gone")
                        + "REFUSED: gone-prod/gone-db has no verified backup — nothing deleted\n", outcome="Failed")
    async with sdk_client(http, mint(fake_backends, Role.admin), confirm=True) as client:
        running = await client.call_tool("orbit_project_remove", {"project": "gone"})
        result, _ = await wait_result(client, running)
    assert result["status"] == "failed"
    steps = {s["name"]: s for s in result["steps"]}
    assert steps["backup"]["status"] == "failed" and "no verified backup" in steps["backup"]["detail"]
    assert steps["remove"]["status"] == "skipped" and "nothing was deleted" in steps["remove"]["detail"]


async def test_a_failed_job_that_was_read_is_logged_and_gone(fake_backends, http, monkeypatch, caplog):
    """A refusal is the tool working: the caller has the sentence, orbitd's log has the
    script's last lines, and no failed Job is left to keep `KubeJobFailed` firing."""
    kube, rt, clock = world(fake_backends, monkeypatch)
    rt.sleeper = finish(kube, clock, REFUSED_LOG, outcome="Failed")
    with caplog.at_level("INFO"):
        async with sdk_client(http, mint(fake_backends, Role.admin), confirm=True) as client:
            running = await client.call_tool("orbit_db_backup", {"project": "demo", "env": "prod"})
            result, _ = await wait_result(client, running)
    assert result["status"] == "failed"
    assert jobs(kube) == []
    kept = [r.getMessage() for r in caplog.records if r.name == "orbitd.desk"]
    assert len(kept) == 3 and all(line.startswith("desk job orbit-desk-db-backup-") for line in kept)
    assert "BACKUP REFUSED: the dump of demo-prod/demo-db does not restore" in kept[-1]


async def test_a_job_that_succeeded_is_kept_for_its_day(fake_backends, http, monkeypatch):
    kube, rt, clock = world(fake_backends, monkeypatch)
    rt.sleeper = finish(kube, clock, BACKUP_LOG)
    async with sdk_client(http, mint(fake_backends, Role.admin), confirm=True) as client:
        running = await client.call_tool("orbit_db_backup", {"project": "demo", "env": "prod"})
        result, _ = await wait_result(client, running)
    assert result["status"] == "applied"
    assert len(jobs(kube)) == 1 and jobs(kube)[0]["spec"]["ttlSecondsAfterFinished"] == 86400


async def test_a_removal_that_stops_before_every_database_is_backed_up_is_not_called_removed(fake_backends, http, monkeypatch):
    kube, rt, clock = world(fake_backends, monkeypatch)
    rt.sleeper = finish(kube, clock, "== what gone left behind\nerror: the server is currently unable to handle the request\n",
                        outcome="Failed")
    async with sdk_client(http, mint(fake_backends, Role.admin), confirm=True) as client:
        running = await client.call_tool("orbit_project_remove", {"project": "gone"})
        result, _ = await wait_result(client, running)
    assert result["status"] == "failed"
    steps = {s["name"]: s for s in result["steps"]}
    assert steps["backup"]["status"] == "failed" and steps["remove"]["status"] == "skipped"


# ---------------------------------------------------------------- the Job's own rules

def test_only_cores_own_scripts_and_plain_words_reach_a_job():
    settings = desk_job.DeskSettings.from_env(SETTINGS)
    ok = desk_job.manifest(settings, COMMIT, kind="db-backup", script="db-backup.sh", args=["demo-prod", "demo-db"], deadline_s=60)
    assert ok["spec"]["activeDeadlineSeconds"] == 60
    for script in ("../../bin/sh", "ship.sh", "/instance/scripts/db-backup.sh", ""):
        with pytest.raises(desk_job.DeskJobUnavailable):
            desk_job.manifest(settings, COMMIT, kind="x", script=script, args=[], deadline_s=60)
    for arg in ("a b", "$(id)", "a;b", "-rf", "", "a\nb", "`id`", "x" * 200):
        with pytest.raises(desk_job.DeskJobUnavailable):
            desk_job.manifest(settings, COMMIT, kind="x", script="db-backup.sh", args=[arg], deadline_s=60)
    with pytest.raises(desk_job.DeskJobUnavailable):
        desk_job.manifest(settings, "main", kind="x", script="db-backup.sh", args=[], deadline_s=60)


def test_the_settings_need_the_desk_image():
    assert desk_job.DeskSettings.from_env({}) is None
    assert desk_job.DeskSettings.from_env({k: v for k, v in SETTINGS.items() if k != "ORBITD_DESK_IMAGE"}) is None
    assert desk_job.DeskSettings.from_env({**SETTINGS, "ORBITD_BACKUPS_SIZE": "20Gi"}).backups_size == "20Gi"
    assert desk_job.DeskSettings.from_env({**SETTINGS, "ORBITD_BACKUPS_SIZE": "lots"}).backups_size == "5Gi"


# ---------------------------------------------------------------- a moment the archive cannot give

def _archive(kube, first="2026-10-09T03:30:06Z", last="2026-10-12T03:30:06Z"):
    kube.objectstores.append({"metadata": {"name": "demo-db", "namespace": "demo-prod"},
                              "status": {"serverRecoveryWindow": {"demo-db": {
                                  "firstRecoverabilityPoint": first, "lastSuccessfulBackupTime": last}}}})


async def test_a_moment_in_the_future_or_before_the_first_base_backup_is_refused_at_once(fake_backends, http, monkeypatch):
    """QA of v1.0.0, F32: asked for a moment before the first backup, and for one in the
    future, each restore ran ten and a half minutes in a scratch namespace and failed
    with "did not become healthy" and raw pod logs. Both are known before anything runs."""
    kube, rt, _ = world(fake_backends, monkeypatch)
    _archive(kube)
    async with sdk_client(http, mint(fake_backends, Role.admin), confirm=True) as client:
        future = await client.call_tool("orbit_db_restore", {"project": "demo", "env": "prod", "to": "2099-01-01T00:00:00Z"})
        early = await client.call_tool("orbit_db_restore", {"project": "demo", "env": "prod", "to": "2026-10-08T00:00:00Z"})
        early_dry = await client.call_tool("orbit_db_restore", {"project": "demo", "env": "prod",
                                                                "to": "2026-10-08T00:00:00Z", "dry_run": True})
    assert future.is_error and future.structured_content["code"] == "refused"
    assert "is in the future" in future.structured_content["what_failed"]
    for r in (early, early_dry):
        body = r.structured_content
        assert r.is_error and body["code"] == "refused"
        assert "before the earliest moment" in body["what_failed"] and "2026-10-09T03:30:06Z" in body["what_failed"]
        assert body["evidence"]["first_recoverability_point"] == "2026-10-09T03:30:06Z"
        assert "orbit_db_status" in body["next_action"]
    assert jobs(kube) == [], "a restore that cannot succeed started a Job"


async def test_a_moment_inside_the_window_and_one_with_no_window_known_still_run(fake_backends, http, monkeypatch):
    kube, rt, clock = world(fake_backends, monkeypatch)
    rt.sleeper = finish(kube, clock, "nothing changed on the live database\n")
    async with sdk_client(http, mint(fake_backends, Role.admin), confirm=True) as client:
        # no ObjectStore records a window (a database whose first base backup is not taken
        # yet, or a store this orbitd cannot read): not a reason to refuse
        unknown = await client.call_tool("orbit_db_restore", {"project": "demo", "env": "prod",
                                                              "to": "2026-10-08T00:00:00Z", "dry_run": True})
        await wait_result(client, unknown)
        _archive(kube)
        inside = await client.call_tool("orbit_db_restore", {"project": "demo", "env": "prod",
                                                             "to": "2026-10-10T12:00:00Z", "dry_run": True})
        await wait_result(client, inside)
    assert len(jobs(kube)) == 2


async def test_a_backup_made_through_the_tool_is_what_db_status_shows(fake_backends, http, monkeypatch):
    """QA of v1.0.0, F30 and F35: orbit_db_status showed no backup for any database, the
    one just backed up included; called a dev database archived; and said "Cluster in
    healthy state" with `ready` null for a database that was switched off."""
    kube, rt, clock = world(fake_backends, monkeypatch)
    rt.sleeper = finish(kube, clock, BACKUP_LOG)
    prod = next(c for c in kube.clusters if c["metadata"]["namespace"] == "demo-prod")
    prod["spec"] = {"plugins": [{"name": "barman-cloud.cloudnative-pg.io", "isWALArchiver": True}]}
    prod["status"]["conditions"] = [{"type": "ContinuousArchiving", "status": "True"}]
    _archive(kube)
    dev = kube.add_cluster(namespace="demo-dev", name="demo-db")
    dev["status"]["conditions"] = [{"type": "ContinuousArchiving", "status": "True"}]
    async with sdk_client(http, mint(fake_backends, Role.operator), confirm=True) as client:
        before = (await client.call_tool("orbit_db_status", {"project": "demo", "env": "prod"})).structured_content
        await wait_result(client, await client.call_tool("orbit_db_backup", {"project": "demo", "env": "prod"}))
        after = (await client.call_tool("orbit_db_status", {"project": "demo", "env": "prod"})).structured_content
        dev_row = (await client.call_tool("orbit_db_status", {"project": "demo", "env": "dev"})).structured_content
        prod["metadata"].setdefault("annotations", {})["cnpg.io/hibernation"] = "on"
        prod["status"]["readyInstances"] = None
        off = (await client.call_tool("orbit_db_status", {"project": "demo", "env": "prod"})).structured_content
        prod["metadata"]["annotations"].pop("cnpg.io/hibernation")
        down = (await client.call_tool("orbit_db_status", {"project": "demo", "env": "prod"})).structured_content
    assert before["state"] == "ready" and before["archiving"] is True and before["wal_archive"] == "True"
    assert before["first_recoverability_point"] == "2026-10-09T03:30:06Z"
    assert before["last_successful_backup"] == "2026-10-12T03:30:06Z"
    assert before["last_file_backup"] is None
    assert after["last_file_backup"]["name"] == BACKUP_FILE and after["last_file_backup"]["at"].endswith("Z")
    assert dev_row["archiving"] is False and dev_row["wal_archive"] is None, "a dev database archives nothing"
    assert dev_row["first_recoverability_point"] is None and dev_row["last_file_backup"] is None
    assert off["state"] == "hibernated" and off["ready"] == 0
    assert down["state"] == "not ready" and down["ready"] == 0 and down["phase"] == "Cluster in healthy state"


async def test_a_removed_projects_backup_note_goes_with_it(fake_backends, http, monkeypatch):
    """On the test box (v1.0.1-rc3) a project made again under a removed project's name
    showed the removed database's backup as its own last backup. The files stay (the
    removal names them); the note in orbit_db_status is the database's, and it is gone."""
    from orbitd.tools_viewer import db_status
    kube, rt, clock = world(fake_backends, monkeypatch)
    db_status.record_file_backup(fake_backends, "gone-prod", "gone-db", "gone-db-20261006T101500Z.sql.gz")
    db_status.record_file_backup(fake_backends, "demo-prod", "demo-db", BACKUP_FILE)
    rt.sleeper = finish(kube, clock, "== what gone left behind\n" + GONE_BACKUP
                        + "REMOVED: gone (backups in /var/lib/orbit-backups)\n")
    async with sdk_client(http, mint(fake_backends, Role.admin), confirm=True) as client:
        result, _ = await wait_result(client, await client.call_tool("orbit_project_remove", {"project": "gone"}))
    assert result["status"] == "applied", result
    assert result["evidence"]["backups"] == ["gone-db-20261006T101500Z.sql.gz"], "the removal still names the files"
    assert db_status.last_file_backup(fake_backends, "gone-prod", "gone-db") is None
    assert db_status.last_file_backup(fake_backends, "demo-prod", "demo-db").name == BACKUP_FILE, "another project's note went too"
