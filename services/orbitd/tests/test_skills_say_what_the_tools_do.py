"""A skill's account of what a tool does, held to the tool.

Two review rounds of one node found the same class of defect: a sentence in a skill that
described a tool's behaviour from memory of the code, and was wrong in a way the reader
could act on (an empty log answer read as idleness or as a store fault, when the tool had
asked the store nothing). The refusal-vocabulary contract beside this file cannot catch
that class: it holds the skills to the words the surface produces, not to what the tools
do. So each row here drives the tool on a fake, writes down what it did, and then reads
the skill for the sentence that says so. A sentence that stops being true fails on the
execution, not on a pattern; a sentence removed from the skill fails on the reading."""
from __future__ import annotations

import json
import pathlib

import pytest
from conftest import mint, sdk_client

from orbitd import source
from orbitd.client import _escape_host_text, _summary
from orbitd.tools_viewer._util import pod_memory_mi
from orbitd.roles import Listing, Role
from orbitd.tools_viewer.deploy_status import _reconcile
from test_tools_operator import ENV, PROJECT, TAG, WORKER, install, seed_world, wait_result
from test_tools_viewer_kube import DEMO, call, labels, seed_catalog
from test_tools_viewer_observability import _loki, invoke

SKILLS = pathlib.Path(__file__).resolve().parents[3] / "skills"


def _says(text: str, phrase: str) -> bool:
    """Whether the skill carries the phrase, wherever its lines wrap."""
    return " ".join(phrase.split()) in " ".join(text.split())


def _skill(name: str) -> str:
    text = (SKILLS / name / "SKILL.md").read_text()
    assert len(text) > 1000, f"{name} is not the shipped skill"
    return text


def test_empty_logs_with_a_component_means_no_pod_of_that_name_exists_now(fake_backends):
    """The component form asks the store only about pods that exist now -- none, and
    it asks nothing; a replacement, and it names that replacement only. A
    predecessor's lines are reachable by the namespace form and by nothing else, and
    the diagnose skill says so."""
    state = fake_backends
    state.backends.loki.responses["/loki/api/v1/query_range"] = _loki(
        ("100", "demo-web-gone-pod", "handled request"),
        ("101", "demo-worker-current", "tick"),
    )
    by_component = invoke(state, "orbit_logs_query", project="demo", env="prod", component="web")
    assert isinstance(by_component, Listing)
    assert by_component.items == []
    assert state.backends.loki.calls == [], "no pod of the component exists, so the store was not asked"

    state.backends.kube.add_pod(
        namespace="demo-prod", name="demo-web-new", component="web",
        labels={"app.kubernetes.io/instance": "demo", "app.kubernetes.io/name": "web"},
    )
    # a component whose name begins with this one's: its pod shares the prefix
    state.backends.kube.add_pod(
        namespace="demo-prod", name="demo-web-api-current", component="web-api",
        labels={"app.kubernetes.io/instance": "demo", "app.kubernetes.io/name": "web-api"},
    )
    invoke(state, "orbit_logs_query", project="demo", env="prod", component="web")
    asked = state.backends.loki.calls[-1]["params"]["query"]
    assert asked == '{namespace="demo-prod", pod=~"demo-web-new"}', asked
    assert "demo-web-gone-pod" not in asked, "the replacement's name is the only one the store is asked about"
    assert "demo-web-api-current" not in asked, "a prefix-sharing component's pod is not this component's"

    state.backends.loki.responses["/loki/api/v1/query_range"] = _loki(
        ("100", "demo-web-gone-pod", "handled request"),
        ("101", "demo-worker-current", "tick"),
        ("102", "demo-web-new", "ready"),
        ("103", "demo-web-api-current", "ready"),
    )
    by_namespace = invoke(state, "orbit_logs_query", project="demo", env="prod")
    assert state.backends.loki.calls[-1]["params"]["query"] == '{namespace="demo-prod"}'
    assert [row["pod"] for row in by_namespace.items] == [
        "demo-web-gone-pod", "demo-worker-current", "demo-web-new", "demo-web-api-current"]

    text = _skill("orbit-diagnose")
    assert _says(text, "none, and it returns empty without asking the store")
    assert _says(text, "asks the store for those names only")
    assert _says(text, "replacement running or not")
    assert _says(text, "this component's current pods included")
    assert _says(text, "the union of step 2's `pods[].pod` over every")
    assert _says(text, "step 2 describes one component at a time")
    assert _says(text, "the name alone does not say which component it belonged to")
    assert _says(text, "`demo-web-` begins `demo-web-api-` too")
    for stale in ("without a question reaching the store", "the lines are a predecessor's",
                  "any other name is another component's", "carries the component's own prefix",
                  "(step 2 lists them)",
                  "Empty logs beside a zero request rate is idleness"):
        assert not _says(text, stale), stale


@pytest.mark.anyio
async def test_the_namespaces_current_pods_are_the_union_of_step_two_over_the_catalog(fake_backends, http):
    """Step 2 describes one component; the namespace's inventory is its pod list taken
    once per catalog component. A line from a pod outside that union came from a pod
    that no longer exists (or from nothing in the catalog)."""
    state = fake_backends
    seed_catalog(state, [DEMO])
    kube = state.backends.kube
    kube.add_pod(namespace="demo-prod", name="demo-web-new", component="web", labels=labels("demo", "web"))
    kube.add_pod(namespace="demo-prod", name="demo-worker-current", component="worker", labels=labels("demo", "worker"))
    kube.add_deployment(namespace="demo-prod", name="demo-web", image="mirror.invalid/app:1")
    kube.add_deployment(namespace="demo-prod", name="demo-worker", image="mirror.invalid/app:1")
    current: set[str] = set()
    for comp in DEMO["components"]:
        r = await call(http, state, "orbit_workload_describe",
                       {"project": "demo", "env": "prod", "component": comp["name"]})
        assert not r.is_error, r.content
        current |= {p["pod"] for p in r.structured_content["pods"]["items"]}
    assert current == {"demo-web-new", "demo-worker-current"}

    # the listing is capped: past the cap the union is incomplete and the reading does not apply
    for i in range(101):
        kube.add_pod(namespace="demo-prod", name=f"demo-web-many-{i}", component="web", labels=labels("demo", "web"))
    r = await call(http, state, "orbit_workload_describe", {"project": "demo", "env": "prod", "component": "web"})
    assert not r.is_error, r.content
    listing = r.structured_content["pods"]
    assert listing["truncated"] is True
    assert len(listing["items"]) == 100
    text = _skill("orbit-diagnose")
    assert _says(text, "lists at most a hundred of its pods")
    assert _says(text, "`pods.truncated: true` means the union is incomplete and this reading does not apply")

    state.backends.loki.responses["/loki/api/v1/query_range"] = _loki(
        ("100", "demo-web-gone-pod", "handled request"),
        ("101", "demo-worker-current", "tick"),
        ("102", "demo-web-new", "ready"),
    )
    by_namespace = invoke(state, "orbit_logs_query", project="demo", env="prod")
    vanished = [row["pod"] for row in by_namespace.items if row["pod"] not in current]
    assert vanished == ["demo-web-gone-pod"]


def test_age_is_since_the_last_recorded_sync_not_since_the_commit_changed(tmp_path):
    """git-sync's record wins, and a sync that fetched nothing resets it: the same
    commit can carry a smaller age a moment later."""
    sidecar = tmp_path / "git-sync.json"
    sidecar.write_text(json.dumps({"synced": 1000}))
    assert source._age_s(None, tmp_path, 1100.0) == 100
    sidecar.write_text(json.dumps({"synced": 1099}))
    assert source._age_s(None, tmp_path, 1100.0) == 1

    text = _skill("orbit-operator")
    assert _says(text, "seconds since the sync time git-sync recorded when its record beside the")
    assert _says(text, "a sync that fetched nothing new resets it too")
    assert not _says(text, "since that checkout last changed")


def test_a_record_that_cannot_be_read_gives_its_own_change_time(tmp_path):
    """Between the parsed record and HEAD's time sits a third case: the record exists
    but is not JSON with a sync key, and then its file time is the age."""
    import os
    sidecar = tmp_path / "git-sync.json"
    sidecar.write_text("not json")
    os.utime(sidecar, (1099, 1099))
    assert source._age_s(None, tmp_path, 1100.0) == 1

    text = _skill("orbit-operator")
    assert _says(text, "when the record is there but cannot be read it is that file's own\n  change time")


class _HeadOnly:
    """An instance repo with no sync record: git answers where HEAD is, nothing else."""

    def __init__(self, head: pathlib.Path) -> None:
        self.head = head

    def run_git(self, args):
        assert args == ["rev-parse", "--git-path", "HEAD"], args
        return str(self.head)


def test_without_a_sync_record_age_is_the_head_files_time_and_a_fetch_does_not_move_it(tmp_path):
    """The fallback is HEAD's file time. A sync that brought nothing new leaves HEAD
    alone, so the age keeps growing; the skill says a large value then means only that
    nothing new was checked out."""
    import os
    head = tmp_path / ".git" / "HEAD"
    head.parent.mkdir()
    head.write_text("ref: refs/heads/main\n")
    os.utime(head, (1000, 1000))
    repo = _HeadOnly(head)
    assert source._age_s(repo, tmp_path, 1100.0) == 100
    # a fetch that found nothing new: nothing on disk changes
    assert source._age_s(repo, tmp_path, 1200.0) == 200

    text = _skill("orbit-operator")
    assert _says(text, "when there is no record it is the time the checkout's HEAD file was last written")
    assert not _says(text, "only a new commit moves")
    assert not _says(text, "a new commit too")
    assert _says(text, "HEAD has not been written for that long")


@pytest.mark.anyio
async def test_the_ship_reports_the_digest_the_registry_gave_the_tag(fake_backends, http):
    """No viewer tool resolves a tag; the ship's registry step and evidence do."""
    rt, _ = install(fake_backends)
    seed_world(fake_backends)
    async with sdk_client(http, mint(fake_backends, Role.operator), confirm=True) as client:
        running = await client.call_tool("orbit_deploy_ship", {
            "project": PROJECT, "env": ENV, "components": [WORKER], "tag": TAG,
        })
        assert not running.is_error, running.content
        body, _ = await wait_result(client, running)
    registry = next(s for s in body["steps"] if s["name"] == "registry")
    assert registry["detail"] == rt.digest.digest
    assert body["evidence"]["digest"] == rt.digest.digest

    text = _skill("orbit-deploy-review")
    assert _says(text, "No read-only tool resolves a tag to its digest")
    assert _says(text, "`evidence.digest`")
    assert not _says(text, "No tool resolves a tag to its digest for you")


class _PerImageDigest:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def lookup(self, image: str, tag: str) -> str:
        self.calls.append((image, tag))
        return "sha256:" + ("11" * 32 if image.endswith("worker") else "22" * 32)


@pytest.mark.anyio
async def test_a_ship_of_several_components_keeps_only_the_last_digest_it_looked_up(fake_backends, http):
    """The registry step asks per component and the record keeps one answer: the
    last. The deploy-review skill qualifies its digest claim to that."""
    digest = _PerImageDigest()
    install(fake_backends, digest=digest)
    seed_world(fake_backends)
    async with sdk_client(http, mint(fake_backends, Role.operator), confirm=True) as client:
        running = await client.call_tool("orbit_deploy_ship", {
            "project": PROJECT, "env": ENV, "components": [WORKER, "web"], "tag": TAG,
        })
        assert not running.is_error, running.content
        body, _ = await wait_result(client, running)
    assert [i for i, _ in digest.calls] == ["mirror.invalid/demo-worker", "mirror.invalid/demo-web"]
    registry = next(s for s in body["steps"] if s["name"] == "registry")
    assert registry["detail"] == "sha256:" + "22" * 32
    assert body["evidence"]["digest"] == "sha256:" + "22" * 32
    assert "sha256:" + "11" * 32 not in json.dumps(body["evidence"]), "the first component's digest is not retained"

    text = _skill("orbit-deploy-review")
    assert _says(text, "for the last component the ship looked up")
    assert _says(text, "a ship of several components keeps only that one's")


@pytest.mark.parametrize("value, shown", [
    ("a,", "a\\,"), ("a;", "a\\;"), ("a:", "a\\:"),
    ("a, b", "a\\, b"), ("a,b", "a,b"), ("http://x", "http://x"),
    ("(x)", "\\(x\\)"),
    ("before code=bad", "before \\code=bad"),
    ("token holds x", "\\token holds x"),
])
def test_the_marks_print_as_the_renderer_prints_them(value, shown):
    assert _escape_host_text(value) == shown


def test_the_operator_skill_describes_the_marks_the_renderer_makes():
    text = _skill("orbit-operator")
    assert _says(text, "or at the end of the value")
    assert _says(text, "wherever it starts inside a value")
    for phrase in ("`code=`", "`what_failed=`", "`token holds `", "`outside typed origin `"):
        assert phrase in text
    assert not _says(text, "`\\\\x0a`"), "the inline example shows two backslashes where the desk prints one"
    assert _says(text, "`\\x0a`")
    assert not _says(text, "a value that begins with one of the desk's reserved words")


def test_reconcile_is_null_when_no_loop_has_touched_the_pair(fake_backends):
    """No reconcile runner on the state, or a runner with no tick for the pair, is `null`:
    not `off (pair not enabled)`, which is a runner's own answer."""
    state = fake_backends
    if hasattr(state, "reconcile"):
        state.reconcile = None
    assert _reconcile(state, "demo", "prod") is None

    class _Runner:
        def latest(self, project, env):
            return None
    state.reconcile = _Runner()
    assert _reconcile(state, "demo", "prod") is None

    text = _skill("orbit-deploy-review")
    assert _says(text, "`reconcile: null` means the loop is off (`ORBITD_APPLY=off`) or has not reached the pair yet")


@pytest.mark.anyio
async def test_a_dry_run_ship_carries_an_empty_evidence_map(fake_backends, http):
    install(fake_backends)
    seed_world(fake_backends)
    async with sdk_client(http, mint(fake_backends, Role.operator)) as client:
        r = await client.call_tool("orbit_deploy_ship", {
            "project": PROJECT, "env": ENV, "components": [WORKER], "tag": TAG, "dry_run": True,
        })
    assert not r.is_error, r.content
    assert r.structured_content["status"] == "dry_run"
    assert r.structured_content["evidence"] == {}
    text = _skill("orbit-ship")
    assert _says(text, "`evidence` is `{}` on a dry run")


def test_the_human_form_is_one_status_line_for_a_status_answer_and_the_document_otherwise():
    assert _summary("orbit_deploy_ship", {"status": "dry_run", "steps": [{"name": "catalog"}], "evidence": {}}) == "orbit_deploy_ship: dry_run"
    assert _summary("orbit_deploy_ship", {"status": "failed", "code": "busy", "what_failed": "held"}) == "orbit_deploy_ship: failed code=busy what_failed=held"
    assert _summary("orbit_project_list", {"items": [{"id": "demo"}], "total": 1}) == 'orbit_project_list: {"items":[{"id":"demo"}],"total":1}'
    text = _skill("orbit-operator")
    assert _says(text, "for an answer that carries a `status` (a change, a dry run, a job) one line, `<tool>: <status>`")
    assert _says(text, "for any other answer `<tool>: <the document, compact JSON>`")
    assert not _says(text, "the document is JSON either way")


def test_a_pods_request_is_the_larger_of_its_app_sum_and_its_biggest_init_container():
    def c(mem):
        return {"resources": {"requests": {"memory": mem}}}
    assert pod_memory_mi({"spec": {"containers": [c("64Mi"), c("64Mi")], "initContainers": [c("512Mi")]}}, "requests") == 512
    assert pod_memory_mi({"spec": {"containers": [c("64Mi"), c("64Mi")], "initContainers": [c("32Mi")]}}, "requests") == 128
    text = _skill("orbit-budget-review")
    assert _says(text, "the larger of its app containers' sum and its biggest init container")
    assert not _says(text, "sums the app containers of the pods active now")
