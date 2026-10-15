"""S14 (docs/V1.md §5, §8): a hand edit server-side apply cannot undo. A field a person
added with kubectl is owned by kubectl's field manager, so orbit's apply never removed it
and the reconcile loop reported the same drift for ever. After each apply the applier
removes the fields a hand-edit manager owns alone, and says which; a dry run names them."""
from __future__ import annotations

from copy import deepcopy

from test_applier import ENV, NS, PROJECT, add_live, rendered_set, set_mode, use_render
from test_applier import world  # noqa: F401  (the pytest fixture, reused)

from orbitd import applier
from orbitd.applier import apply, hand_edits, plan

ORBIT_FIELDS = {"f:spec": {"f:template": {"f:spec": {"f:containers": {
    'k:{"name":"web"}': {".": {}, "f:name": {}, "f:image": {},
                         "f:env": {'k:{"name":"A"}': {".": {}, "f:name": {}, "f:value": {}}}}}}}}}
HAND_FIELDS = {"f:spec": {"f:template": {"f:spec": {"f:containers": {
    'k:{"name":"web"}': {"f:env": {'k:{"name":"HAND_EDIT"}': {".": {}, "f:name": {}, "f:value": {}}}}}}}}}


def deployment(env: list[dict], managed: list[dict], annotations: dict | None = None) -> dict:
    tmeta = {"annotations": annotations} if annotations else {}
    return {"apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": "demo-web", "namespace": NS, "resourceVersion": "9", "managedFields": managed},
            "spec": {"template": {"metadata": tmeta, "spec": {"containers": [
                {"name": "web", "image": "example/web:1", "env": env}]}}}}


def entry(manager: str, fields: dict, operation: str = "Update", **extra) -> dict:
    return {"manager": manager, "operation": operation, "fieldsV1": fields, **extra}


def test_a_field_kubectl_owns_alone_is_found_by_its_keys_never_its_value():
    live = deployment([{"name": "A", "value": "1"}, {"name": "HAND_EDIT", "value": "s3cr3t"}],
                      [entry("orbit", ORBIT_FIELDS, "Apply"), entry("kubectl-set", HAND_FIELDS)])
    found = hand_edits(live)
    assert [(steps, shown, m) for steps, shown, m in found] == [
        (["spec", "template", "spec", "containers", 0, "env", 1],
         "spec.template.spec.containers[name=web].env[name=HAND_EDIT]", "kubectl-set")]
    assert "s3cr3t" not in repr(found)


def test_what_orbit_or_a_controller_also_owns_is_not_a_hand_edit():
    # kubectl set env A=2 on orbit's own variable: orbit co-owns the item (force re-asserts it)
    co_owned = {"f:spec": {"f:template": {"f:spec": {"f:containers": {
        'k:{"name":"web"}': {"f:env": {'k:{"name":"A"}': {".": {}, "f:value": {}}}}}}}}}
    live = deployment([{"name": "A", "value": "1"}],
                      [entry("orbit", ORBIT_FIELDS, "Apply"), entry("kubectl-set", co_owned)])
    assert hand_edits(live) == []
    # a controller's Update (not a hand tool) is never matched
    ctrl = deployment([{"name": "A", "value": "1"}, {"name": "HAND_EDIT", "value": "x"}],
                      [entry("orbit", ORBIT_FIELDS, "Apply"), entry("kube-controller-manager", HAND_FIELDS)])
    assert hand_edits(ctrl) == []
    # a hand tool writing a subresource (kubectl scale) is not a spec hand edit
    sub = deployment([{"name": "A", "value": "1"}, {"name": "HAND_EDIT", "value": "x"}],
                     [entry("orbit", ORBIT_FIELDS, "Apply"), entry("kubectl", HAND_FIELDS, subresource="scale")])
    assert hand_edits(sub) == []


def test_a_restart_stamp_is_not_a_hand_edit():
    stamp = {"f:spec": {"f:template": {"f:metadata": {"f:annotations": {"f:kubectl.kubernetes.io/restartedAt": {}}}}}}
    live = deployment([{"name": "A", "value": "1"}],
                      [entry("orbit", ORBIT_FIELDS, "Apply"), entry("kubectl-rollout", stamp)],
                      annotations={"kubectl.kubernetes.io/restartedAt": "2026-10-05T00:00:00Z"})
    assert hand_edits(live) == []


def test_a_field_that_is_already_gone_is_not_reported():
    live = deployment([{"name": "A", "value": "1"}],
                      [entry("orbit", ORBIT_FIELDS, "Apply"), entry("kubectl-set", HAND_FIELDS)])
    assert hand_edits(live) == []


def _apply_keeping_the_hand_edit(world, monkeypatch, *, dry: bool):
    """The real API server keeps a field another manager owns through orbit's apply; the
    fake replaces the object, so the stored copy is re-dressed with the hand edit after it."""
    set_mode(world, "on")
    kube = world.backends.kube
    objects = rendered_set()
    use_render(monkeypatch, objects)
    p = plan(world, PROJECT, ENV)
    real = applier._server_side_apply

    def ssa(dyn, obj, ref, *, dry):
        out = real(dyn, obj, ref, dry=dry)
        if ref.kind != "Deployment":
            return out
        stored = next(o for o in kube.objects if o["kind"] == "Deployment" and o["metadata"]["name"] == "demo-web") \
            if not dry else deepcopy(out)
        c = stored["spec"]["template"]["spec"]["containers"][0]
        c.setdefault("env", []).append({"name": "HAND_EDIT", "value": "by-hand"})
        stored["metadata"]["managedFields"] = [entry("orbit", ORBIT_FIELDS, "Apply"), entry("kubectl-set", HAND_FIELDS)]
        stored["metadata"].setdefault("resourceVersion", "5")
        if dry:
            add_live(kube, deepcopy(stored))
        return stored

    monkeypatch.setattr(applier, "_server_side_apply", ssa)
    return kube, apply(world, p, dry_run=dry)


def test_apply_removes_the_hand_edit_and_says_so(world, monkeypatch):
    kube, result = _apply_keeping_the_hand_edit(world, monkeypatch, dry=False)
    assert result.ok, result
    dep = next(r for r in result.results if r.ref.kind == "Deployment")
    assert dep.result == "applied"
    assert dep.reason == ("reverted a hand edit: spec.template.spec.containers[name=web].env[name=HAND_EDIT] "
                          "(by kubectl-set)")
    stored = next(o for o in kube.objects if o["kind"] == "Deployment")
    assert [e["name"] for e in stored["spec"]["template"]["spec"]["containers"][0]["env"]] == []
    patch = [m for m in kube.mutations if m["verb"] == "patch" and m["kind"] == "Deployment"][-1]
    assert patch["content_type"] == "application/json-patch+json"
    assert patch["body"][0]["op"] == "test" and patch["body"][0]["path"] == "/metadata/resourceVersion"
    assert patch["body"][1] == {"op": "remove", "path": "/spec/template/spec/containers/0/env/0"}
    assert patch["field_manager"] == "orbit"


def test_a_dry_run_names_the_hand_edit_and_changes_nothing(world, monkeypatch):
    kube, result = _apply_keeping_the_hand_edit(world, monkeypatch, dry=True)
    dep = next(r for r in result.results if r.ref.kind == "Deployment")
    assert dep.reason.startswith("would revert a hand edit: spec.template.spec.containers[name=web].env[name=HAND_EDIT]")
    patch = [m for m in kube.mutations if m["verb"] == "patch" and m["kind"] == "Deployment"][-1]
    assert patch["dry_run"] == "All"
    stored = next(o for o in kube.objects if o["kind"] == "Deployment")
    assert any(e["name"] == "HAND_EDIT" for e in stored["spec"]["template"]["spec"]["containers"][0]["env"])


def test_the_reconcile_loop_keeps_a_reverted_hand_edit_for_deploy_status(fake_backends, monkeypatch):
    """S14 (docs/V1.md §8): the loop reverts a kubectl change within one period, so by the
    time anyone asks the live object shows nothing. The tick records what it reverted and
    orbit_deploy_status still names it, several ticks later."""
    from types import SimpleNamespace

    from orbitd import reconcile
    from orbitd.applier import ApplyResult, ObjectRef, ObjectResult
    from orbitd.tools_viewer import deploy_status

    ref = ObjectRef(api_version="apps/v1", kind="Deployment", namespace=NS, name="demo-web")
    edit = "reverted a hand edit: spec.template.spec.containers[name=web].env[name=HAND_EDIT] (by kubectl-set)"
    results = [[ObjectResult(ref=ref, result="applied", reason=edit)], [ObjectResult(ref=ref, result="unchanged")]]
    monkeypatch.setattr(reconcile.applier, "plan", lambda state, p, e: SimpleNamespace(prune=[], render_sha="r" * 64, source=None))
    calls = iter([results[0], results[1], results[1]])
    monkeypatch.setattr(reconcile.applier, "apply", lambda state, plan, dry_run: ApplyResult(dry_run=False, ok=True, results=next(calls)))
    runner = reconcile.ReconcileRunner(fake_backends, dry_run=False)
    for _ in range(3):
        runner._record(PROJECT, ENV, runner._reconcile(PROJECT, ENV))
    first, last = runner.history(PROJECT, ENV)[0], runner.latest(PROJECT, ENV)
    assert first.hand_edits == [f"Deployment {NS}/demo-web: {edit}"]
    assert last.hand_edits == []
    fake_backends.reconcile = runner
    status = deploy_status._reconcile(fake_backends, PROJECT, ENV)
    assert status.hand_edits == []
    assert status.recent_hand_edits == [f"{first.last_run} Deployment {NS}/demo-web: {edit}"]
    assert "HAND_EDIT" in status.recent_hand_edits[0] and "kubectl-set" in status.recent_hand_edits[0]


def test_a_removed_pairs_hand_edits_are_not_the_next_project_of_that_names(fake_backends, monkeypatch):
    """On the test box (v1.0.3) a project made again under a removed project's name showed,
    as its own recent hand edits, the ones undone for the removed project an hour before
    it existed. The loop forgets a pair the catalog no longer carries; the units it also
    ticks (monitors, features) are not pairs and are kept."""
    from types import SimpleNamespace

    from orbitd import reconcile
    from orbitd.applier import ApplyResult, ObjectRef, ObjectResult

    ref = ObjectRef(api_version="apps/v1", kind="Deployment", namespace=NS, name="demo-web")
    edit = "reverted a hand edit: spec.replicas (by kubectl-scale)"
    monkeypatch.setattr(reconcile.applier, "plan", lambda state, p, e: SimpleNamespace(prune=[], render_sha="r" * 64, source=None))
    monkeypatch.setattr(reconcile.applier, "apply", lambda state, plan, dry_run: ApplyResult(
        dry_run=False, ok=True, results=[ObjectResult(ref=ref, result="applied", reason=edit)]))
    monkeypatch.setattr(reconcile.applier, "pair_enabled", lambda state, p, e: True)
    import orbitd.gate
    monkeypatch.setattr(orbitd.gate, "gate_of", lambda state: SimpleNamespace(evaluate=lambda: SimpleNamespace(blocks=False, good="g")))
    pairs = [(PROJECT, ENV), ("other", "prod")]
    monkeypatch.setattr(reconcile, "catalog_pairs", lambda state, rendered=True: list(pairs))
    runner = reconcile.ReconcileRunner(fake_backends, dry_run=False)
    monkeypatch.setattr(runner, "tick_monitors", lambda: runner._record("monitors", "", reconcile.ReconcileTick(last_run="t")))
    monkeypatch.setattr(runner, "tick_features", lambda: None)
    runner.run_once()
    assert runner.history(PROJECT, ENV)[0].hand_edits, "the edit was recorded while the pair was in the catalog"
    pairs.remove((PROJECT, ENV))
    runner.run_once()
    assert runner.history(PROJECT, ENV) == [] and runner.latest(PROJECT, ENV) is None
    assert len(runner.history("other", "prod")) == 2, "another pair's ticks went with it"
    assert runner.latest("monitors", "") is not None, "the monitors unit is not a pair and is kept"
    pairs.append((PROJECT, ENV))
    monkeypatch.setattr(reconcile.applier, "apply", lambda state, plan, dry_run: ApplyResult(
        dry_run=False, ok=True, results=[ObjectResult(ref=ref, result="applied")]))
    runner.run_once()
    assert [t.hand_edits for t in runner.history(PROJECT, ENV)] == [[]], "the project made again starts with none"


# ---------------------------------------------------------------- what the apply itself put back

SCALE_FIELDS = {"f:spec": {"f:replicas": {}}}


def _world_applied_once(world, monkeypatch):
    """The pair as orbit left it: every rendered object live, and in the inventory."""
    from test_applier import seed_inventory, seed_live
    from orbitd.applier import ObjectRef
    set_mode(world, "on")
    kube = world.backends.kube
    objects = rendered_set()
    use_render(monkeypatch, objects)
    seed_live(kube, objects)
    # the namespace was made before the inventory was last written (2026-01-01): the
    # inventory is this namespace's own
    next(o for o in kube.objects if o["kind"] == "Namespace")["metadata"]["creationTimestamp"] = "2025-12-01T00:00:00Z"
    seed_inventory(kube, [ObjectRef.of(o) for o in objects])
    return kube, objects


def test_a_scale_by_hand_is_named_when_the_apply_puts_it_back(world, monkeypatch):
    """QA of v1.0.0, F33: `kubectl scale` writes a field orbit owns, so the apply puts it
    back by itself and nothing was ever "owned alone" to report: three replicas became
    one again and no tool said a hand had been there."""
    kube, _ = _world_applied_once(world, monkeypatch)
    dep = next(o for o in kube.objects if o["kind"] == "Deployment")
    dep["spec"]["replicas"] = 3
    dep["metadata"]["managedFields"] = [entry("orbit", ORBIT_FIELDS, "Apply"),
                                        entry("kubectl", SCALE_FIELDS, subresource="scale")]
    result = apply(world, plan(world, PROJECT, ENV), dry_run=False)
    assert result.ok, result
    row = next(r for r in result.results if r.ref.kind == "Deployment")
    assert row.result == "applied" and row.reason == "reverted a hand edit: spec.replicas (by kubectl)"
    assert "3" not in row.reason
    others = [r for r in result.results if r.ref.kind != "Deployment"]
    assert all(r.reason is None for r in others), "an object nobody touched was called a hand edit"


def test_an_object_deleted_by_hand_is_named_when_the_apply_restores_it(world, monkeypatch):
    kube, _ = _world_applied_once(world, monkeypatch)
    for kind in ("Service", "NetworkPolicy"):
        kube.objects[:] = [o for o in kube.objects if o["kind"] != kind]
    result = apply(world, plan(world, PROJECT, ENV), dry_run=False)
    assert result.ok, result
    by_kind = {r.ref.kind: r for r in result.results}
    for kind in ("Service", "NetworkPolicy"):
        assert by_kind[kind].result == "applied"
        assert by_kind[kind].reason == ("reverted a hand edit: the object had been deleted from the cluster "
                                        "and is restored"), kind
    assert any(o["kind"] == "Service" for o in kube.objects)


def test_an_inventory_older_than_the_namespace_is_not_evidence_of_a_deletion(world, monkeypatch):
    """A project made again under a removed project's name: the removed one's inventory is
    still there, the namespace is new, and every object is missing because nothing was
    applied here yet. On the test box (v1.0.1-rc1) that first tick listed ten objects as
    "deleted from the cluster and restored"."""
    kube, _ = _world_applied_once(world, monkeypatch)
    namespace = next(o for o in kube.objects if o["kind"] == "Namespace")
    namespace["metadata"]["creationTimestamp"] = "2026-06-01T00:00:00Z"      # after the inventory (2026-01-01)
    for kind in ("Service", "Deployment", "NetworkPolicy"):
        kube.objects[:] = [o for o in kube.objects if o["kind"] != kind]
    result = apply(world, plan(world, PROJECT, ENV), dry_run=False)
    assert result.ok, result
    assert all(r.reason is None for r in result.results), [r.reason for r in result.results if r.reason]
    # and a namespace whose age cannot be read claims nothing either
    kube2, _ = kube, None
    namespace["metadata"].pop("creationTimestamp")
    kube.objects[:] = [o for o in kube.objects if o["kind"] != "Service"]
    result = apply(world, plan(world, PROJECT, ENV), dry_run=False)
    assert all(r.reason is None for r in result.results)


def test_an_object_that_is_new_in_this_commit_is_not_a_hand_edit(world, monkeypatch):
    """Missing from the cluster and never applied before: the commit added it."""
    set_mode(world, "on")
    use_render(monkeypatch, rendered_set())
    result = apply(world, plan(world, PROJECT, ENV), dry_run=False)
    assert result.ok, result
    assert all(r.reason is None for r in result.results)


def test_a_change_that_comes_from_the_commit_is_not_blamed_on_a_hand(world, monkeypatch):
    """The catalog asks for two replicas now. kubectl once touched another field of the
    object; the replicas change is the commit's and is not called a hand edit."""
    kube, objects = _world_applied_once(world, monkeypatch)
    dep = next(o for o in kube.objects if o["kind"] == "Deployment")
    dep["metadata"]["managedFields"] = [entry("orbit", ORBIT_FIELDS, "Apply"),
                                        entry("kubectl-annotate", {"f:metadata": {"f:annotations": {"f:note": {}}}})]
    wanted = deepcopy(objects)
    next(o for o in wanted if o["kind"] == "Deployment")["spec"]["replicas"] = 2
    use_render(monkeypatch, wanted)
    result = apply(world, plan(world, PROJECT, ENV), dry_run=False)
    row = next(r for r in result.results if r.ref.kind == "Deployment")
    assert row.result == "applied" and row.reason is None


def test_owning_a_field_is_read_through_lists_and_whole_parents():
    tree = {"f:spec": {"f:replicas": {}, "f:template": {"f:spec": {"f:containers": {
        'k:{"name":"web"}': {"f:image": {}}}}}}, "f:data": {}}
    assert applier._owns(tree, "/spec/replicas")
    assert applier._owns(tree, "/spec/template/spec/containers/0/image")
    assert not applier._owns(tree, "/spec/template/spec/containers/0/env")
    assert not applier._owns(tree, "/spec/strategy")
    assert applier._owns(tree, "/data/anything/under/it")
