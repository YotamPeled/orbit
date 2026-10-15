"""The commit gate's checks (docs/V1.md M2c), each on a passing fixture and on a failing one
per mistake it exists for. The fixture is a real instance: the template copied, this
core vendored into it, a config, a catalog with one project, then `make init` and `make
sync` as an operator runs them. Every failing case is that instance with one thing wrong.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from orbitd import checks
from orbitd.checks import Context, Facts, FactsUnavailable, no_facts, run_chain

REPO = Path(__file__).resolve().parents[3]
VERSION = "v9.9.9"
MI = 1024 * 1024
PROJECT = {
    "id": "demo", "name": "Demo", "blurb": "A fixture.", "repo": "fixture-org/demo", "hosted": "self",
    "environments": {"prod": {"branch": "main"}},
    "components": [{"name": "web", "kind": "web", "class": "interactive", "image": "ghcr.io/fixture-org/demo-web",
                    "port": 8080, "path": "/", "memory": "256Mi", "probePath": "/healthz"}],
}


def _fixture_values() -> dict:
    """The config values scripts/test_verify_platform.py scaffolds its fixture org with."""
    sys.path.insert(0, str(REPO / "scripts" / "verify"))
    sys.path.insert(0, str(REPO / "scripts"))
    spec = importlib.util.spec_from_file_location("tvp_fixture", REPO / "scripts" / "test_verify_platform.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return dict(module.FIXTURE)


def run(root: Path, *argv: str) -> None:
    proc = subprocess.run(list(argv), cwd=root, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def build_instance(dest: Path) -> Path:
    shutil.copytree(REPO / "template", dest)
    for src, dst in checks.vendored_paths(REPO):
        source, target = REPO / src, dest / dst
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(source, target)
    config = yaml.safe_load((dest / "instance" / "config.yaml.example").read_text())
    for path, value in _fixture_values().items():
        *parents, leaf = path.split(".")
        node = config
        for key in parents:
            node = node[key]
        node[leaf] = value
    for node in config["nodes"]:
        node["ip"] = "198.51.100.10"
    config["secrets"]["provider"] = "sops"
    (dest / "instance" / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
    (dest / "orbit-core.lock").write_text(f"url: https://example.invalid/orbit-core.git\nref: {VERSION}\n")
    (dest / "catalog" / "projects.yaml").write_text(yaml.safe_dump({"version": 3, "projects": [PROJECT]}, sort_keys=False))
    # shipped once: a component with no tag yet runs zero replicas and reserves nothing
    (dest / "catalog" / "tags.yaml").write_text(yaml.safe_dump({"tags": {"demo/prod/web": "main-abc1234"}}))
    run(dest, sys.executable, "scripts/instance-init.py")
    run(dest, sys.executable, "scripts/catalog-sync.py")
    for junk in dest.rglob("__pycache__"):
        shutil.rmtree(junk, ignore_errors=True)
    return dest


@pytest.fixture(scope="session")
def good(tmp_path_factory) -> Path:
    return build_instance(tmp_path_factory.mktemp("gate") / "instance")


@pytest.fixture
def instance(good, tmp_path) -> Path:
    """A private copy of the good instance for one test to break."""
    return Path(shutil.copytree(good, tmp_path / "instance", symlinks=True))


def facts(node_mi: int = 8000, platform_mi: int = 2000) -> Facts:
    return Facts(node_allocatable_bytes=lambda: node_mi * MI, platform_requests_bytes=lambda: platform_mi * MI)


def verdict(root: Path, only=None, **kw):
    return run_chain(Context(root=root, core=REPO, version=VERSION, facts=kw.pop("facts", facts()), **kw),
                     only=only)


def refusal(root: Path, name: str, **kw) -> str:
    """The sentence check `name` fails this tree with, running that check alone."""
    v = verdict(root, only=[name], **kw)
    assert not v.ok, f"the {name} check passed a tree it must refuse"
    assert v.failed.name == name
    return v.failed.sentence


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text, f"{old!r} is not in {path.name}"
    path.write_text(text.replace(old, new, 1))


# ---------------------------------------------------------------- the chain

def test_a_scaffolded_instance_passes_every_check(good):
    v = verdict(good)
    assert v.ok, v.dump()
    assert [r.name for r in v.results] == checks.names()
    assert all(r.ok and r.sentence is None for r in v.results)


def test_each_check_passes_alone_on_the_good_instance(good):
    for name in checks.names():
        v = verdict(good, only=[name])
        assert v.ok and [r.name for r in v.results] == [name], (name, v.dump())


def test_the_chain_never_writes_into_the_checkout(good):
    before = {rel: f.read_bytes() for rel, f in checks.files_under(good).items()}
    verdict(good)
    assert {rel: f.read_bytes() for rel, f in checks.files_under(good).items()} == before


def test_the_chain_stops_at_the_first_failure_and_reports_that_checks_sentence(instance):
    edit(instance / "clusters" / "platform" / "apps" / "traefik.yaml", "replicas: 1", "replicas: 3")   # scaffold
    (instance / "clusters" / "apps" / "extra.yaml").write_text("kind: ConfigMap\n")                        # generated
    v = verdict(instance)
    assert not v.ok
    assert [r.name for r in v.results] == ["core", "scaffold"], "nothing after the first failure runs"
    assert v.failed.name == "scaffold" and "traefik.yaml" in v.failed.sentence
    assert v.dump()["check"] == "scaffold" and v.dump()["sentence"] == v.failed.sentence


def test_a_check_that_crashes_is_a_refusal_never_a_pass(good, monkeypatch):
    from orbitd.checks import catalog

    def boom(ctx):
        raise RuntimeError("secret text that must not travel")

    monkeypatch.setattr(catalog, "check", boom)
    v = verdict(good)
    assert not v.ok and v.failed.name == "catalog"
    assert v.failed.sentence == "the catalog check could not run (RuntimeError)"
    assert [r.name for r in v.results][-1] == "catalog"


def test_every_check_in_the_chain_has_a_failing_fixture_here():
    """A check added to the chain without a case below is a check nobody proved bites."""
    source = Path(__file__).read_text()
    for name in checks.names():
        assert f'refusal(instance, "{name}"' in source, f"no failing fixture for the {name} check"
    for module in checks.chain():
        assert module.NAME and module.WHAT and callable(module.check)
    assert len(set(checks.names())) == len(checks.names())


# ---------------------------------------------------------------- core

def test_core_a_vendored_file_edited_added_or_deleted_is_refused(instance):
    edit(instance / "scripts" / "catalog-sync.py", "import yaml", "import yaml  # edited here")
    assert "scripts/catalog-sync.py differs from orbit-core v9.9.9" in refusal(instance, "core")


def test_core_a_file_added_to_a_vendored_directory_is_refused(instance):
    (instance / "charts" / "service" / "templates" / "extra.yaml").write_text("kind: ConfigMap\n")
    said = refusal(instance, "core")
    assert "charts/service/templates/extra.yaml is not part of orbit-core" in said


def test_core_a_module_planted_beside_cores_scripts_is_refused(instance):
    """`python3 scripts/catalog-sync.py` looks in scripts/ first: a yaml.py there would be
    the yaml the generator imports. A shell script or a note beside them is not."""
    for rel in ("scripts/yaml.py", "scripts/verify/shown2.py", "scripts/json.pth"):
        planted = instance / rel
        planted.write_text("raise SystemExit('ran')\n")
        said = refusal(instance, "core")
        assert said.startswith(f"{rel} is not part of orbit-core") and "would import it" in said, said
        planted.unlink()
    package = instance / "scripts" / "yaml"
    package.mkdir()
    (package / "__init__.py").write_text("")
    assert refusal(instance, "core").startswith("scripts/yaml is not part of orbit-core")
    shutil.rmtree(package)
    (instance / "scripts" / "my-own-helper.sh").write_text("#!/bin/sh\n")
    (instance / "scripts" / "NOTES.md").write_text("ours\n")
    assert verdict(instance, only=["core"]).ok, "an instance's own shell script or note beside core's files is its own"


def test_core_a_vendored_file_missing_or_linked_is_refused(instance):
    (instance / "scripts" / "ship.sh").unlink()
    assert "scripts/ship.sh is missing" in refusal(instance, "core")
    (instance / "scripts" / "ship.sh").symlink_to("/bin/sh")
    assert "scripts/ship.sh is a symlink" in refusal(instance, "core")


def test_core_a_commit_at_another_core_says_how_to_go_on(instance):
    (instance / "orbit-core.lock").write_text("url: x\nref: v9.9.10\n")
    said = refusal(instance, "core")
    assert "pins orbit-core v9.9.10" in said and "checked by v9.9.9" in said and "orbit platform apply" in said
    v = verdict(instance, only=["core"])
    assert v.failed.upgrade and not v.transient and v.dump()["checks"][0]["upgrade"] is True
    (instance / "orbit-core.lock").unlink()
    assert "orbit-core.lock is missing" in refusal(instance, "core")


def test_a_tampered_script_is_never_run(instance):
    """The scaffolder in the commit is replaced by one that would leave a mark; the checks
    run this build's scaffolder, so the mark never appears and the edit is what is refused."""
    mark = instance.parent / "mark"
    (instance / "scripts" / "instance-init.py").write_text(
        f"import pathlib\npathlib.Path({str(mark)!r}).write_text('ran')\n")
    v = verdict(instance)
    assert v.failed.name == "core" and "scripts/instance-init.py differs" in v.failed.sentence
    for name in checks.names():
        verdict(instance, only=[name])
    assert not mark.exists(), "a check ran the commit's own script"


# ---------------------------------------------------------------- scaffold

def test_scaffold_a_rendered_file_edited_by_hand_is_refused(instance):
    edit(instance / "clusters" / "platform" / "apps" / "traefik.yaml", "replicas: 1", "replicas: 3")
    said = refusal(instance, "scaffold")
    assert "clusters/platform/apps/traefik.yaml is not what `make init` renders" in said


def test_scaffold_a_config_change_without_make_init_is_refused(instance):
    config = yaml.safe_load((instance / "instance" / "config.yaml").read_text())
    original = yaml.safe_dump(config, sort_keys=False)
    config["features"] = {"logs": True}                 # traces off, tree still has it
    (instance / "instance" / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
    said = refusal(instance, "scaffold")
    assert "`make init`" in said and "clusters/platform/" in said
    (instance / "instance" / "config.yaml").write_text(original)
    (instance / "clusters" / "platform" / "config" / "orbitd" / "netpol.yaml").unlink()
    assert "clusters/platform/config/orbitd/netpol.yaml is missing" in refusal(instance, "scaffold")


def test_scaffold_a_manifest_behind_a_closed_gate_or_added_by_hand_is_refused(instance):
    """The intake's public door is rendered only when the config asks for it; restored by
    hand, `make init` would remove it. And a manifest nobody's config asked for at all."""
    staged = instance / "provisioning" / "clusters" / "platform" / "config" / "orbit-intake" / "netpol.yaml"
    stale = instance / "clusters" / "platform" / "config" / "orbit-intake" / "netpol.yaml"
    stale.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(staged, stale)
    said = refusal(instance, "scaffold")
    assert "clusters/platform/config/orbit-intake/netpol.yaml is in the commit and `make init` removes it" in said
    shutil.rmtree(stale.parent)
    extra = instance / "clusters" / "platform" / "config" / "orbitd" / "extra.yaml"
    extra.write_text(yaml.safe_dump({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "x"}, "data": {"k": "dg=="}}))
    said = refusal(instance, "scaffold")
    assert "clusters/platform/config/orbitd/extra.yaml is not a file `make init` renders" in said
    extra.unlink()
    (instance / "clusters" / "platform" / "config" / "orbitd" / "NOTES.md").write_text("a note is applied by nothing\n")
    assert verdict(instance, only=["scaffold"]).ok


def test_scaffold_a_config_make_init_refuses_is_refused_with_its_sentence(instance):
    config = yaml.safe_load((instance / "instance" / "config.yaml").read_text())
    config["features"] = {"no-such-feature": True}
    (instance / "instance" / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
    said = refusal(instance, "scaffold")
    assert said.startswith("`make init` refuses this commit:") and "no-such-feature" in said


# ---------------------------------------------------------------- catalog, generated

def test_catalog_one_the_generator_refuses_is_refused_with_its_sentence(instance):
    catalog = yaml.safe_load((instance / "catalog" / "projects.yaml").read_text())
    catalog["projects"][0]["components"][0]["memory"] = "lots"
    (instance / "catalog" / "projects.yaml").write_text(yaml.safe_dump(catalog, sort_keys=False))
    said = refusal(instance, "catalog")
    assert said.startswith("the generator refuses the catalog:") and "memory" in said


def test_generated_a_catalog_change_without_make_sync_is_refused(instance):
    catalog = yaml.safe_load((instance / "catalog" / "projects.yaml").read_text())
    catalog["projects"][0]["components"][0]["memory"] = "512Mi"
    (instance / "catalog" / "projects.yaml").write_text(yaml.safe_dump(catalog, sort_keys=False))
    said = refusal(instance, "generated")
    assert "is not what the generator writes" in said and "make sync" in said


def test_generated_a_generated_file_edited_added_or_removed_is_refused(instance):
    apps = instance / "clusters" / "apps"
    target = next(p for p in sorted(apps.glob("*.yaml")) if "kind: Application" in p.read_text())
    original = target.read_text()
    target.write_text(original.replace("256Mi", "999Mi"))
    assert f"clusters/apps/{target.name} is not what the generator writes" in refusal(instance, "generated")
    target.write_text(original)
    (apps / "extra.yaml").write_text("kind: ConfigMap\n")
    assert "clusters/apps/extra.yaml is not a file the generator writes" in refusal(instance, "generated")
    (apps / "extra.yaml").unlink()
    (apps / "namespaces.yaml").unlink()
    assert "clusters/apps/namespaces.yaml is missing" in refusal(instance, "generated")


# ---------------------------------------------------------------- render

def test_render_values_the_chart_refuses_are_refused_by_application_name(instance):
    apps = instance / "clusters" / "apps"
    target = next(p for p in sorted(apps.glob("*.yaml")) if "kind: Application" in p.read_text())
    docs = list(yaml.safe_load_all(target.read_text()))
    app = next(d for d in docs if isinstance(d, dict) and d.get("kind") == "Application")
    app["spec"]["source"]["helm"]["valuesObject"]["components"][0]["kind"] = "no-such-kind"
    target.write_text(yaml.safe_dump_all(docs, sort_keys=False))
    said = refusal(instance, "render")
    assert said.startswith(f"{app['metadata']['name']} does not render:")


# ---------------------------------------------------------------- sealed

SEALED = {"apiVersion": "isindir.github.com/v1alpha3", "kind": "SopsSecret",
          "metadata": {"name": "orbit-s3", "namespace": "loki"},
          "spec": {"secretTemplates": [{"name": "orbit-s3", "stringData": {
              "access-key": "ENC[AES256_GCM,data:abc,iv:def,tag:ghi,type:str]",
              "secret-key": "ENC[AES256_GCM,data:jkl,iv:mno,tag:pqr,type:str]"}}]},
          "sops": {"mac": "ENC[AES256_GCM,data:m,iv:i,tag:t,type:str]", "age": []}}


def write_secret(instance: Path, doc: dict, name: str = "orbit-s3") -> Path:
    path = instance / "clusters" / "platform" / "secrets" / "loki" / f"{name}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    return path


def test_sealed_a_properly_sealed_secret_passes(instance):
    write_secret(instance, SEALED)
    assert verdict(instance, only=["sealed"]).ok


def test_sealed_a_plain_secret_is_refused_wherever_it_sits(instance):
    write_secret(instance, {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "x"}, "stringData": {"k": "v"}})
    assert "holds a plain Secret" in refusal(instance, "sealed")
    (instance / "clusters" / "platform" / "secrets" / "loki" / "orbit-s3.yaml").unlink()
    (instance / "clusters" / "apps" / "extra-secret.yaml").write_text(
        yaml.safe_dump({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "x"}, "data": {"k": "dg=="}}))
    said = refusal(instance, "sealed")
    assert "clusters/apps/extra-secret.yaml holds a plain Secret" in said


def test_sealed_a_secret_with_one_value_left_open_or_never_sealed_is_refused_without_the_value(instance):
    import copy
    leaky = copy.deepcopy(SEALED)
    leaky["spec"]["secretTemplates"][0]["stringData"]["secret-key"] = "PLAINTEXT-CANARY-VALUE"
    write_secret(instance, leaky)
    said = refusal(instance, "sealed")
    assert "not ciphertext at spec.secretTemplates[0].stringData.secret-key" in said
    assert "PLAINTEXT-CANARY-VALUE" not in said, "a refusal must never repeat the value"
    unsealed = copy.deepcopy(SEALED)
    unsealed.pop("sops")
    write_secret(instance, unsealed)
    assert "was never sealed" in refusal(instance, "sealed")
    write_secret(instance, {"kind": "ConfigMap", "metadata": {"name": "x"}})
    assert "is not a SopsSecret" in refusal(instance, "sealed")


def _declare(instance: Path, name: str, keys: list[str]) -> str:
    """Make the first generated Application declare Secret `name` with `keys`; its namespace."""
    apps = instance / "clusters" / "apps"
    target = next(p for p in sorted(apps.glob("*.yaml")) if "kind: Application" in p.read_text())
    docs = list(yaml.safe_load_all(target.read_text()))
    app = next(d for d in docs if isinstance(d, dict) and d.get("kind") == "Application")
    app["spec"]["source"]["helm"]["valuesObject"]["secrets"] = [{"from": "external", "name": name, "keys": keys}]
    target.write_text(yaml.safe_dump_all(docs, sort_keys=False))
    return app["spec"]["destination"]["namespace"]


def _seal_for(instance: Path, namespace: str, name: str, keys: list[str]) -> None:
    import copy
    doc = copy.deepcopy(SEALED)
    doc["metadata"] = {"name": name, "namespace": namespace}
    doc["spec"]["secretTemplates"] = [{"name": name, "data": {k: "ENC[AES256_GCM,data:abc,iv:def,tag:ghi,type:str]" for k in keys}}]
    path = instance / "clusters" / "platform" / "secrets" / namespace / f"{name}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(doc, sort_keys=False))


def test_sealed_a_secret_sealed_without_the_keys_its_project_declares_is_refused_by_name(instance):
    """The rebuilt test box (docs/V1.md §8): `orbit-s3` sealed for a new project under the
    platform copy's key names; every check passed and the database could not archive."""
    ns = _declare(instance, "orbit-s3", ["HETZNER_S3_ACCESS_KEY", "HETZNER_S3_SECRET_KEY"])
    _seal_for(instance, ns, "orbit-s3", ["access-key", "secret-key"])
    said = refusal(instance, "sealed")
    assert f"clusters/platform/secrets/{ns}/orbit-s3.yaml seals {ns}/orbit-s3 without the key(s) HETZNER_S3_ACCESS_KEY, HETZNER_S3_SECRET_KEY" in said
    assert "it has: access-key, secret-key" in said and "ENC[" not in said
    _seal_for(instance, ns, "orbit-s3", ["HETZNER_S3_ACCESS_KEY", "HETZNER_S3_SECRET_KEY", "an-extra-one"])
    assert verdict(instance, only=["sealed"]).ok


def test_sealed_a_declared_secret_nobody_sealed_yet_or_another_namespaces_file_is_not_this_checks_finding(instance):
    ns = _declare(instance, "orbit-s3", ["HETZNER_S3_ACCESS_KEY"])
    assert verdict(instance, only=["sealed"]).ok                      # not sealed yet: the workload says so
    _seal_for(instance, "some-other-namespace", "orbit-s3", ["access-key"])
    assert verdict(instance, only=["sealed"]).ok                      # the same name elsewhere is another Secret
    write_secret(instance, SEALED)                                    # the platform's own loki/orbit-s3, its own key names
    assert verdict(instance, only=["sealed"]).ok


def test_sealed_a_credential_in_a_generated_manifest_is_refused(instance):
    apps = instance / "clusters" / "apps"
    target = next(p for p in sorted(apps.glob("*.yaml")) if "kind: Application" in p.read_text())
    docs = list(yaml.safe_load_all(target.read_text()))
    app = next(d for d in docs if isinstance(d, dict) and d.get("kind") == "Application")
    component = app["spec"]["source"]["helm"]["valuesObject"]["components"][0]
    component.setdefault("env", {})["API_TOKEN"] = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    target.write_text(yaml.safe_dump_all(docs, sort_keys=False))
    said = refusal(instance, "sealed")
    assert said.startswith("a generated manifest carries a credential:")
    assert "A1b2C3d4E5f6" not in said


# ---------------------------------------------------------------- fit

def reserved_mi(root: Path) -> int:
    """What the instance's rendered workloads reserve, by the check's own sum."""
    import tempfile

    from orbitd.checks import fit, render
    scratch = Path(tempfile.mkdtemp())
    try:
        pairs, failure = render.rendered_pairs(Context(root=root, core=REPO, version=VERSION, scratch=scratch))
        assert failure is None, failure
        return sum(fit.reserved_bytes(objects) for _, objects in pairs) // MI
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def test_fit_more_than_the_node_has_is_refused_with_the_three_numbers(instance):
    projects = reserved_mi(instance)
    assert projects > 0, "the fixture's project reserves nothing, so this test proves nothing"
    said = refusal(instance, "fit", facts=facts(node_mi=2000 + projects - 1, platform_mi=2000))
    assert f"reserves {projects} Mi for projects" in said and "platform holds 2000 Mi" in said
    assert f"{2000 + projects} Mi in all" in said and f"node has {2000 + projects - 1} Mi" in said
    assert verdict(instance, only=["fit"], facts=facts(node_mi=2000 + projects, platform_mi=2000)).ok, "exactly fitting fits"


def test_fit_a_node_that_cannot_be_read_is_a_failure_marked_as_not_the_commits(instance):
    v = verdict(instance, only=["fit"], facts=no_facts("the cluster did not answer"))
    assert not v.ok and v.transient
    assert v.failed.sentence == "fit could not be checked: the cluster did not answer"
    assert not verdict(instance, only=["scaffold"]).transient


def test_fit_counts_replicas_databases_and_the_larger_of_init_and_app_containers():
    from orbitd.checks.fit import reserved_bytes
    objects = [
        {"kind": "Deployment", "spec": {"replicas": 3, "template": {"spec": {
            "initContainers": [{"resources": {"requests": {"memory": "64Mi"}}}],
            "containers": [{"resources": {"requests": {"memory": "100Mi"}}}, {"resources": {"requests": {"memory": "28Mi"}}}]}}}},
        {"kind": "StatefulSet", "spec": {"template": {"spec": {"containers": [{"resources": {"requests": {"memory": "1Gi"}}}]}}}},
        {"apiVersion": "postgresql.cnpg.io/v1", "kind": "Cluster", "spec": {"instances": 2, "resources": {"requests": {"memory": "256Mi"}}}},
        {"kind": "CronJob", "spec": {"jobTemplate": {"spec": {"template": {"spec": {"containers": [{"resources": {"requests": {"memory": "512Mi"}}}]}}}}}},
        {"kind": "Deployment", "spec": {"replicas": 0, "template": {"spec": {"containers": [{"resources": {"requests": {"memory": "4Gi"}}}]}}}},
    ]
    assert reserved_bytes(objects) == (3 * 128 + 1024 + 2 * 256) * MI


def test_facts_unavailable_is_its_own_error():
    with pytest.raises(FactsUnavailable):
        no_facts().node_allocatable_bytes()
