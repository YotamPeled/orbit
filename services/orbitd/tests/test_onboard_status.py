"""`orbit onboard status` drives a fresh instance through every onboarding state.

The verb is the desk's half of the orbit-onboard skill: an agent runs it after every step
and follows the one `next` line. These rows build an instance directory, a credentials
directory and a fake cluster in a temp tree, then advance them step by step and hold the
report to what an agent would act on: the first undone step, its next command, the exit
code, and -- the property the whole thing rests on -- that no value ever reaches either
output stream: a token planted in every credential file, every live Secret, an API error
body and a credential plugin's log line must not appear on stdout or stderr, in either
output form.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
import base64
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from unittest import mock
from kubernetes.client.exceptions import ApiException

from orbitd import onboard
from orbitd.onboard import (DONE, TODO, BLOCKED, UNMEASURED, Report, _quiet, catalog_pairs, declaration_landed,
                            inventory_workloads, pair_file_rel, required_secrets, run_acceptance, status, workload_state)
from orbitd.testing import FakeKube

PLANTED = "PLANTED-VALUE-0f3a9c7e1b2d4e5f6a7b8c9d"
RECIPIENT = "age1qyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpq0wx3ly"
NAME = "orbtest"
DOMAIN = f"{NAME}.test"
APPS = ("sops-secrets-operator", "cert-manager", "kube-prometheus-stack")
STEPS = ["instance-repo", "credentials", "infrastructure", "node", "sops-key", "platform",
         "secrets", "features", "dashboard-sign-in", "acceptance", "first-project"]


def _write(path: Path, text: str, mode: int | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if mode is not None:
        os.chmod(path, mode)
    return path


def _deployment(ns: str, name: str, *, secret: str = "", pull: str = "", available: bool = True,
                generation: int = 3, observed: int = 3, updated: int = 2, total: int = 2) -> dict[str, Any]:
    env = [{"secretRef": {"name": secret}}] if secret else []
    return {"apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": name, "namespace": ns, "generation": generation},
            "spec": {"replicas": 2, "template": {"spec": {"imagePullSecrets": [{"name": pull}] if pull else [],
                                                           "containers": [{"name": "c", "envFrom": env}]}}},
            "status": {"observedGeneration": observed, "updatedReplicas": updated, "replicas": total,
                       "availableReplicas": 2 if available else 1}}


class World:
    """One instance checkout, one credentials directory, one cluster, all fake, each
    method advancing exactly one runbook step."""

    def __init__(self, root: Path) -> None:
        self.instance = root / f"{NAME}-platform"
        self.instance.mkdir()
        self.home = root / "home"
        self.config_dir = self.home / ".config" / f"{NAME}-platform"
        self.kubeconfig = self.home / ".kube" / f"config-{NAME}-platform"
        self.kube: Any = FakeKube()
        self.kube.add_object({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "kube-system"}})
        self.reachable = True
        self.tofu_state: list[str] | None = None
        self.origin = True
        # What the platform tree renders: the kube-prometheus-stack app asks for one sealed
        # Secret (and a pull secret it renders itself), cert-manager makes its own, sops
        # asks for the bootstrap key.
        self.rendered: list[tuple[str, list[dict[str, Any]]]] = [
            ("sops-secrets-operator", [_deployment("sops", "sops-secrets-operator", secret="sops-age")]),
            ("cert-manager", [{"apiVersion": "cert-manager.io/v1", "kind": "Certificate",
                               "metadata": {"name": "wild", "namespace": "cert-manager"}, "spec": {"secretName": "wild-tls"}},
                              {"apiVersion": "cert-manager.io/v1", "kind": "ClusterIssuer", "metadata": {"name": "le"},
                               "spec": {"acme": {"privateKeySecretRef": {"name": "le-account"}}}}]),
            ("kube-prometheus-stack", [_deployment("monitoring", "grafana", secret="grafana-image-renderer-token", pull="registry-pull"),
                                       {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "registry-pull", "namespace": "monitoring"},
                                        "data": {"x": PLANTED}}]),
        ]
        self.render_calls = 0
        self.acceptance: tuple[int | None, int] = (0, 0)
        self.acceptance_calls: list[dict[str, Any]] = []
        self.behind = ""  # what the fake declaration check answers
        self.landed_calls: list[tuple[tuple[str, str], str]] = []
        self.sign_in_answer = "accepted"
        self.sign_in_calls: list[tuple[str, Path]] = []

    # --- the runbook, one step per method ---------------------------------------
    def config(self, name: str = NAME, ip: str = "", apply: str = "on", pairs: list[str] | None = None) -> None:
        listed = "[" + ", ".join(pairs or []) + "]"
        _write(self.instance / "instance/config.yaml",
               f"instance:\n  name: {name}\ndomains:\n  primary: {name}.test\n  dev: dev.{name}.test\n"
               f"git:\n  repoUrl: git@github.com:acme/{name}-platform.git\n  github_org: acme\n"
               f"nodes:\n  - name: {name}-node-1\n    ip: {ip!r}\n"
               f"orbitd:\n  applyMode: {apply!r}\n  applyPairs: {listed}\n")

    def pin_and_render(self, tokens_left: bool = False) -> None:
        _write(self.instance / "orbit-core.lock", "url: https://example.invalid/core.git\nref: v0.0.0\n")
        body = "kind: Application\nmetadata:\n  name: {0}\nspec:\n  destination:\n    namespace: {1}\n"
        for app, ns in zip(APPS, ("sops", "cert-manager", "monitoring")):
            _write(self.instance / f"clusters/platform/apps/{app}.yaml", body.format(app, ns))
        legend = "kind: ConfigMap\nmetadata:\n  name: dash\ndata:\n  legend: '{{pod}} {{ $labels.instance }}'\n"
        if tokens_left:
            legend += "  unfilled: '{{instance.name}}'\n"
        _write(self.instance / "clusters/platform/config/dash.yaml", legend)
        _write(self.instance / "catalog/projects.yaml", "version: 3\nprojects: []\n")

    def credentials(self, *, private: bool = True, env_keys=("HCLOUD_TOKEN", "CLOUDFLARE_API_TOKEN"),
                    recipient: bool = True) -> None:
        mode = 0o600 if private else 0o644
        self.config_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.config_dir, 0o700 if private else 0o755)
        env = "".join(f"export {k}={PLANTED}\n" for k in env_keys)
        _write(self.config_dir / "env", env, mode)
        _write(self.config_dir / "node-key", f"-----BEGIN OPENSSH PRIVATE KEY-----\n{PLANTED}\n", mode)
        _write(self.config_dir / "node-key.pub", "ssh-ed25519 AAAA test\n", 0o644)
        _write(self.config_dir / "ssh-key-path", f"{self.config_dir / 'node-key'}\n", 0o644)
        _write(self.config_dir / "age.key", f"AGE-SECRET-KEY-{PLANTED}\n", mode)
        _write(self.instance / ".sops.yaml",
               f"creation_rules:\n  - age: {RECIPIENT if recipient else ''}\n")

    def infrastructure(self, ip: str = "10.7.3.2") -> None:
        _write(self.instance / "tofu/instance.auto.tfvars",
               'server_name = "x"\nssh_key_name = "x"\nzone_ids = { "t" = "1" }\n')
        self.tofu_state = ["hcloud_server.prod", "hcloud_firewall.node"]
        self.config(ip=ip)

    def node(self) -> None:
        _write(self.kubeconfig, "apiVersion: v1\nkind: Config\nclusters: []\n", 0o600)

    def sops_key(self) -> None:
        self.kube.add_object({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "sops"}})
        self.kube.add_object({"apiVersion": "v1", "kind": "Secret",
                              "metadata": {"name": "sops-age", "namespace": "sops"},
                              "data": {"age.agekey": PLANTED}})

    def platform(self, *apps: str) -> None:
        for app in apps or APPS:
            self.kube.add_object({"apiVersion": "v1", "kind": "ConfigMap",
                                  "metadata": {"name": f"orbit-platform-inventory-{app}", "namespace": "orbit"},
                                  "data": {"inventory": ""}})

    def sealed(self, ns: str, name: str, *, live: bool = True, rendered: bool = True, ext: str = "yaml") -> None:
        _write(self.instance / f"clusters/platform/secrets/{ns}/{name}.{ext}",
               f"apiVersion: isindir.github.com/v1alpha3\nkind: SopsSecret\nmetadata:\n  name: {name}\n"
               f"  namespace: {ns}\nspec:\n  secretTemplates:\n    - name: {name}\n      data:\n"
               f"        token: ENC[AES256_GCM,data:{PLANTED},type:str]\nsops:\n  age: []\n")
        if live:
            self.kube.add_object({"apiVersion": "isindir.github.com/v1alpha3", "kind": "SopsSecret",
                                  "metadata": {"name": name, "namespace": ns}, "status": {"message": "Healthy"}})
        if rendered:
            self.kube.add_object({"apiVersion": "v1", "kind": "Secret",
                                  "metadata": {"name": name, "namespace": ns}, "data": {"token": PLANTED}})

    def dashboard(self, *, live: bool = True) -> None:
        _write(self.config_dir / "grafana-admin-password", PLANTED + "\n", 0o600)
        self.sealed("monitoring", "grafana-admin", live=live, rendered=live)

    def project(self, pid: str = "widgets", *, rendered: bool = True, namespace: bool = True,
                inventory: bool = True, available: bool = True, deployed: bool = True,
                workloads: str = "apps/v1/Deployment/{ns}/web", **deployment: Any) -> None:
        _write(self.instance / "catalog/projects.yaml",
               f"version: 3\nprojects:\n  - id: {pid}\n    name: W\n    environments:\n      prod: {{}}\n      dev:\n        enabled: false\n")
        if rendered:
            _write(self.instance / f"clusters/orbit/{pid}-prod.yaml", "kind: Namespace\n")
        if namespace:
            self.kube.add_object({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": f"{pid}-prod"}})
        if inventory:
            self.kube.add_object({"apiVersion": "v1", "kind": "ConfigMap",
                                  "metadata": {"name": f"orbit-inventory-{pid}-prod", "namespace": "orbit"},
                                  "data": {"applied_at": "2026-09-12T00:00:00Z", "commit": "c0ffee" * 6 + "c0ffee00",
                                           "objects": f"/v1/Namespace//{pid}-prod\n" + workloads.format(ns=f"{pid}-prod") + f"\n/v1/Service/{pid}-prod/web\n"}})
        if deployed and "Deployment" in workloads:
            self.kube.add_object(_deployment(f"{pid}-prod", "web", available=available, **deployment))

    def everything(self) -> None:
        self.config()
        self.pin_and_render()
        self.credentials()
        self.infrastructure()
        self.node()
        self.sops_key()
        self.platform()
        self.sealed("monitoring", "grafana-image-renderer-token")
        self.dashboard()
        self.project()

    # --- the verb --------------------------------------------------------------
    def _factory(self, kubeconfig: str):
        assert kubeconfig == str(self.kubeconfig)
        if not self.reachable:
            raise ConnectionError("tunnel closed")
        return self.kube

    def _render(self, instance_dir: Path):
        assert Path(instance_dir) == self.instance
        self.render_calls += 1
        return self.rendered

    def _acceptance(self, _dir, **kw):
        self.acceptance_calls.append(kw)
        return self.acceptance

    def _landed(self, _dir, pair, commit):
        self.landed_calls.append((pair, commit))
        return self.behind

    def _sign_in(self, domain, path):
        self.sign_in_calls.append((domain, Path(path)))
        return self.sign_in_answer

    def report(self, accept: bool = False) -> Report:
        return status(self.instance, home=self.home, kube_factory=self._factory,
                      tofu_state=lambda _dir: self.tofu_state, render=self._render,
                      accept=accept, acceptance=self._acceptance, landed=self._landed, sign_in=self._sign_in)

    def run(self, *flags: str, capsys) -> tuple[int, str, str]:
        args = SimpleNamespace(instance_dir=str(self.instance), config_dir=str(self.config_dir),
                               kubeconfig=str(self.kubeconfig), home=str(self.home),
                               json="--json" in flags, accept="--accept" in flags,
                               kube_factory=self._factory, tofu_state=lambda _dir: self.tofu_state,
                               render=self._render, acceptance=self._acceptance, landed=self._landed,
                               sign_in=self._sign_in, onboard_command="status")
        rc = onboard.cmd_onboard(args)
        captured = capsys.readouterr()
        return rc, captured.out, captured.err


class AnsweringKube:
    """The fake cluster, with one named object answering a status of the test's choosing
    and the API error's body carrying the planted value, as a real 500 page might."""

    def __init__(self, inner: Any, kind: str, name: str, status: int) -> None:
        self._inner, self._kind, self._name, self._status = inner, kind, name, status
        self.dynamic = self

    @property
    def resources(self):
        return self

    def get(self, api_version: str, kind: str):
        resource = self._inner.dynamic.resources.get(api_version=api_version, kind=kind)
        if kind != self._kind:
            return resource
        outer = self

        class _Resource:
            def get(self, name=None, namespace=None, **kw):
                if name == outer._name:
                    e = ApiException(status=outer._status, reason=f"server error: {PLANTED}")
                    e.body = json.dumps({"message": PLANTED})
                    raise e
                return resource.get(name=name, namespace=namespace, **kw)
        return _Resource()


@pytest.fixture
def world(tmp_path, monkeypatch):
    w = World(tmp_path)
    monkeypatch.setattr(onboard, "_git_origin", lambda _dir: w.origin)
    return w


def _step(report: Report, name: str):
    return next(s for s in report.steps if s.name == name)


def _first_undone(report: Report) -> str:
    return next(s.name for s in report.steps if s.state != DONE)


# ---------------------------------------------------------------- the walk, step by step

def test_an_empty_directory_asks_for_the_config_first(world):
    r = world.report()
    assert _first_undone(r) == "instance-repo"
    assert "config.yaml.example" in r.next and "make init" in r.next
    assert not r.complete


def test_example_values_are_named_field_by_field(world):
    world.config(name="example")
    r = world.report()
    assert _step(r, "instance-repo").state == TODO
    assert "instance.name" in _step(r, "instance-repo").detail


def test_unfilled_tokens_count_but_a_dashboards_legend_braces_do_not(world):
    world.config()
    world.pin_and_render(tokens_left=True)
    assert "{{tokens}}" in _step(world.report(), "instance-repo").detail
    world.pin_and_render(tokens_left=False)
    assert _step(world.report(), "instance-repo").state == DONE


def test_a_checkout_without_origin_is_told_to_create_the_repo(world):
    world.config()
    world.pin_and_render()
    world.origin = False
    assert "gh repo create" in _step(world.report(), "instance-repo").next


def test_credentials_are_judged_by_existence_mode_and_key_names(world):
    world.config()
    world.pin_and_render()
    r = world.report()
    assert _first_undone(r) == "credentials"
    assert "Never paste a token into this conversation" in r.next
    world.credentials(private=False)
    s = _step(world.report(), "credentials")
    assert "chmod 600" in s.detail and f"chmod 700 {world.config_dir}" in s.detail
    world.credentials()
    os.chmod(world.config_dir, 0o750)   # private files in a group-accessible directory: still open
    s = _step(world.report(), "credentials")
    assert s.state == TODO and "chmod 700" in s.detail and "chmod 600" not in s.detail
    os.chmod(world.config_dir, 0o700)
    world.credentials(env_keys=("HCLOUD_TOKEN",))
    assert "CLOUDFLARE_API_TOKEN" in _step(world.report(), "credentials").detail
    world.credentials(recipient=False)
    assert "age recipient" in _step(world.report(), "credentials").detail
    world.credentials()
    assert _step(world.report(), "credentials").state == DONE


def test_infrastructure_needs_tfvars_a_server_in_state_and_the_address(world):
    world.config()
    world.pin_and_render()
    world.credentials()
    s = _step(world.report(), "infrastructure")
    assert "instance.auto.tfvars" in s.detail
    assert "( set -a; ." in s.next and "set +a; tofu" in s.next and "subshell" in s.next
    world.infrastructure(ip="")
    world.tofu_state = []
    s = _step(world.report(), "infrastructure")
    assert "no server" in s.detail and "( set -a; ." in s.next
    world.tofu_state = ["hcloud_server.prod"]
    assert "nodes[0].ip" in _step(world.report(), "infrastructure").detail
    world.infrastructure()
    s = _step(world.report(), "infrastructure")
    assert s.state == DONE and "10.7.3.2" in s.detail


def test_the_templates_documentation_address_is_not_a_server_address(world):
    """The template ships nodes[0].ip in a documentation-only range; an instance that still
    carries it after tofu created the server has not written the server's address, so the
    step stays TODO and names the tofu output to copy (r102o)."""
    world.config()
    world.pin_and_render()
    world.credentials()
    for placeholder in ("203.0.113.10", "192.0.2.7", "198.51.100.200", "2001:db8::1"):
        world.infrastructure(ip=placeholder)
        s = _step(world.report(), "infrastructure")
        assert s.state == TODO and "nodes[0].ip" in s.detail and "output server_ipv4" in s.next, placeholder
    for real in ("10.7.3.2", "2a01:4f8:1::2", "65.21.7.8"):
        world.infrastructure(ip=real)
        s = _step(world.report(), "infrastructure")
        assert s.state == DONE and real in s.detail, real


def test_tofu_state_that_cannot_be_asked_does_not_block_a_set_address(world):
    world.config()
    world.pin_and_render()
    world.credentials()
    world.infrastructure()
    world.tofu_state = None
    s = _step(world.report(), "infrastructure")
    assert s.state == DONE and "not asked" in s.detail


def test_no_kubeconfig_means_bootstrap_and_every_cluster_step_is_unmeasured(world):
    world.config(); world.pin_and_render(); world.credentials(); world.infrastructure()
    r = world.report()
    assert _first_undone(r) == "node"
    assert "bootstrap-node.sh" in r.next and str(world.config_dir) in r.next
    for name in ("sops-key", "platform", "secrets"):
        assert _step(r, name).state == UNMEASURED
    assert world.render_calls == 0


def test_a_closed_tunnel_blocks_the_node_step_with_the_tunnel_command(world):
    world.config(); world.pin_and_render(); world.credentials(); world.infrastructure(); world.node()
    world.reachable = False
    r = world.report()
    s = _step(r, "node")
    assert s.state == BLOCKED
    assert "-L 6443:127.0.0.1:6443 root@10.7.3.2" in s.next
    assert _step(r, "sops-key").state == UNMEASURED


def test_the_sops_key_is_checked_by_existence_and_named_by_file_key(world):
    world.config(); world.pin_and_render(); world.credentials(); world.infrastructure(); world.node()
    r = world.report()
    assert _first_undone(r) == "sops-key"
    assert "age.agekey" in r.next and "create namespace sops" in r.next
    world.sops_key()
    assert _step(world.report(), "sops-key").state == DONE


def test_the_platform_step_names_the_apps_without_an_inventory(world):
    world.config(); world.pin_and_render(); world.credentials(); world.infrastructure(); world.node(); world.sops_key()
    world.platform("sops-secrets-operator")
    s = _step(world.report(), "platform")
    assert s.state == TODO and "1/3" in s.detail and "cert-manager" in s.detail and "kube-prometheus-stack" in s.detail
    assert "orbit platform apply" in s.next
    world.platform()
    assert _step(world.report(), "platform").state == DONE


def test_the_secrets_step_derives_what_to_seal_from_the_rendered_tree(world):
    """The required set comes from what the rendered objects refer to, not from which
    sealed files exist: a reference with no file is a todo even beside other sealed files,
    and what the tree makes for itself (a rendered Secret, a Certificate's target, an ACME
    account key, the bootstrap key, an operator's own webhook cert) is never demanded."""
    world.config(); world.pin_and_render(); world.credentials(); world.infrastructure(); world.node(); world.sops_key(); world.platform()
    s = _step(world.report(), "secrets")
    assert s.state == TODO and "no sealed file renders: monitoring/grafana-image-renderer-token" in s.detail
    assert "registry-pull" not in s.detail and "wild-tls" not in s.detail and "le-account" not in s.detail and "sops-age" not in s.detail
    assert "mint" in s.next and world.render_calls == 1
    world.sealed("loki", "orbit-s3")  # a file the tree never asked for changes nothing
    assert "monitoring/grafana-image-renderer-token" in _step(world.report(), "secrets").detail
    world.rendered[2][1].append(_deployment("monitoring", "renderer", secret="grafana-renderer-key"))
    s = _step(world.report(), "secrets")
    assert "monitoring/grafana-image-renderer-token" in s.detail and "monitoring/grafana-renderer-key" in s.detail
    world.rendered[2][1].pop()
    world.sealed("monitoring", "grafana-image-renderer-token")
    s = _step(world.report(), "secrets")
    assert s.state == DONE and "asks for nothing more" in s.detail


def test_a_sealed_file_with_the_short_extension_is_checked_live_too(world):
    """The secrets tooling reads .yaml and .yml alike; a sealed .yml file whose live
    SopsSecret is missing is a todo, not a silently satisfied reference."""
    world.everything()
    assert _step(world.report(), "secrets").state == DONE
    world.sealed("monitoring", "short-ext", live=False, ext="yml")
    s = _step(world.report(), "secrets")
    assert s.state == TODO and "monitoring/short-ext" in s.detail


def test_required_secrets_skips_the_self_made_ones():
    rendered = [("cloudnative-pg", [_deployment("cnpg-system", "cloudnative-pg", secret="cnpg-webhook-cert")]),
                ("app", [{"kind": "Pod", "metadata": {"name": "p"}, "spec": {"volumes": [{"secret": {"secretName": "needed"}}]}}])]
    assert required_secrets(rendered, {"app": "team"}) == {("team", "needed")}
    # the exception is the operator's own Deployment mounting what it generates; any other
    # referrer to that name is a demand (a tree with a consumer and no operator has nobody to make it)
    consumer = [("x", [_deployment("cnpg-system", "something-else", secret="cnpg-webhook-cert")])]
    assert required_secrets(consumer, {}) == {("cnpg-system", "cnpg-webhook-cert")}
    elsewhere = [("x", [_deployment("other-ns", "cloudnative-pg", secret="cnpg-webhook-cert")])]
    assert required_secrets(elsewhere, {}) == {("other-ns", "cnpg-webhook-cert")}


def test_authorization_oauth_and_bearer_selectors_are_references():
    """remoteWrite/scrape authorization: `credentials` is a Secret key selector; oauth2's
    `clientSecret` is one; a ServiceMonitor's `bearerTokenSecret` is one."""
    rendered = [("kps", [
        {"kind": "Prometheus", "metadata": {"name": "p", "namespace": "monitoring"},
         "spec": {"remoteWrite": [{"url": "https://x", "authorization": {"credentials": {"name": "remote-bearer", "key": "token"}}},
                                  {"url": "https://y", "oauth2": {"clientId": {"secret": {"name": "oauth-id", "key": "id"}},
                                                                 "clientSecret": {"name": "oauth-secret", "key": "secret"}}}]}},
        {"kind": "ServiceMonitor", "metadata": {"name": "sm", "namespace": "monitoring"},
         "spec": {"endpoints": [{"port": "web", "bearerTokenSecret": {"name": "scrape-bearer", "key": "token"}}]}},
    ])]
    assert required_secrets(rendered, {}) == {("monitoring", n) for n in ("remote-bearer", "oauth-id", "oauth-secret", "scrape-bearer")}
    proxied = [("kps", [{"kind": "Prometheus", "metadata": {"name": "p", "namespace": "monitoring"},
                         "spec": {"remoteWrite": [{"url": "https://x", "proxyConnectHeader": {
                             "Proxy-Authorization": [{"name": "proxy-auth", "key": "token"}]}}]}}])]
    assert required_secrets(proxied, {}) == {("monitoring", "proxy-auth")}
    # a ServiceAccount's imagePullSecrets sit beside spec, not under it
    sa = [("tempo", [{"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": "tempo", "namespace": "tempo"},
                      "imagePullSecrets": [{"name": "tempo-registry"}], "automountServiceAccountToken": True}])]
    assert required_secrets(sa, {}) == {("tempo", "tempo-registry")}
    sigv4 = [("kps", [{"kind": "Prometheus", "metadata": {"name": "p", "namespace": "monitoring"},
                       "spec": {"remoteWrite": [{"url": "https://x", "sigv4": {"region": "us-east-1",
                                                                             "accessKey": {"name": "aws-access", "key": "id"},
                                                                             "secretKey": {"name": "aws-secret", "key": "secret"}}}]}}])]
    assert required_secrets(sigv4, {}) == {("monitoring", "aws-access"), ("monitoring", "aws-secret")}


def test_a_tree_that_does_not_render_is_a_todo_by_type_only(world):
    world.config(); world.pin_and_render(); world.credentials(); world.infrastructure(); world.node(); world.sops_key(); world.platform()

    def boom(_dir):
        raise RuntimeError(PLANTED)
    r = status(world.instance, home=world.home, kube_factory=world._factory,
               tofu_state=lambda _d: None, render=boom)
    s = _step(r, "secrets")
    assert s.state == TODO and "RuntimeError" in s.detail and PLANTED not in s.detail


def test_the_secrets_step_names_unapplied_files_and_unrendered_secrets(world):
    world.config(); world.pin_and_render(); world.credentials(); world.infrastructure(); world.node(); world.sops_key(); world.platform()
    world.sealed("monitoring", "grafana-image-renderer-token", live=False, rendered=False)
    world.sealed("loki", "orbit-s3", live=True, rendered=False)
    s = _step(world.report(), "secrets")
    assert s.state == TODO
    assert "not applied: monitoring/grafana-image-renderer-token" in s.detail and "not rendered from their sealed file: loki/orbit-s3" in s.detail
    assert "orbit secrets verify" in s.next
    world.sealed("monitoring", "grafana-image-renderer-token")
    world.sealed("loki", "orbit-s3")
    assert _step(world.report(), "secrets").state == DONE


def test_the_dashboard_step_wants_the_file_the_sealed_file_and_the_live_secret(world):
    world.config(); world.pin_and_render(); world.credentials(); world.infrastructure(); world.node(); world.sops_key(); world.platform()
    world.sealed("monitoring", "grafana-image-renderer-token")
    assert "grafana-admin-password" in _step(world.report(), "dashboard-sign-in").detail
    world.dashboard(live=False)
    s = _step(world.report(), "dashboard-sign-in")
    assert s.state == TODO and "monitoring/grafana-admin" in s.detail
    world.dashboard()
    s = _step(world.report(), "dashboard-sign-in")
    assert s.state == DONE and "accepts the workstation password" in s.detail
    assert world.sign_in_calls[-1] == (DOMAIN, world.config_dir / "grafana-admin-password")


def test_every_cluster_request_carries_a_timeout(world):
    """An endpoint that accepts a connection and says nothing must not hold the verb: every
    resource request the report makes passes a timeout, and the real client factory bounds
    the connection pool (which discovery uses) as well."""
    world.everything()
    world.report()
    calls = world.kube.dynamic_calls
    assert calls, "no cluster request was made"
    assert all(c.get("_request_timeout") == onboard.REQUEST_TIMEOUT for c in calls), \
        [c for c in calls if c.get("_request_timeout") != onboard.REQUEST_TIMEOUT][:3]
    kubeconfig = world.home / "kc"
    _write(kubeconfig, "apiVersion: v1\nkind: Config\nclusters:\n- name: c\n  cluster: {server: 'https://127.0.0.1:9'}\n"
                       "contexts:\n- name: c\n  context: {cluster: c, user: u}\ncurrent-context: c\nusers:\n- name: u\n  user: {token: x}\n", 0o600)
    kube = onboard._default_kube(str(kubeconfig))
    seen: list = []
    kube._api_client.rest_client.pool_manager.request = lambda method, url, **kw: seen.append(kw) or _Answer()
    kube._api_client.rest_client.GET("http://127.0.0.1:9/version")            # discovery names no timeout
    kube._api_client.rest_client.GET("http://127.0.0.1:9/api/v1/namespaces/x", _request_timeout=3)
    discovery, resource = (k["timeout"] for k in seen)
    assert (discovery._connect, discovery._read) == (10, onboard.REQUEST_TIMEOUT) and resource.total == 3, seen


class _Answer:
    """What the pool hands back: enough for the client to treat it as a 200."""
    status = 200
    reason = "OK"
    data = b"{}"

    def getheaders(self):
        return {}

    def getheader(self, name, default=None):
        return default


def test_a_dashboard_that_refuses_the_workstation_password_is_a_todo_with_the_reseal(world):
    """A rendered Secret proves nothing about its content; the step signs in with the file.
    Refused: the two homes hold different values (or the sealed user is not admin), and
    the way back is one value in both. Unanswered: blocked, with the reason's type only."""
    world.everything()
    world.sign_in_answer = "refused"
    s = _step(world.report(), "dashboard-sign-in")
    assert s.state == TODO and "refuses the workstation password" in s.detail
    assert "reseal" in s.next and "admin-user: admin" in s.next and "apply --app secrets" in s.next
    assert "rollout restart deployment -l app.kubernetes.io/name=grafana" in s.next and "-n monitoring" in s.next
    assert PLANTED not in s.detail + s.next
    world.sign_in_answer = "unanswered (URLError)"
    s = _step(world.report(), "dashboard-sign-in")
    assert s.state == BLOCKED and "did not answer the sign-in (unanswered (URLError))" in s.detail and f"grafana.{DOMAIN}" in s.detail


def test_dashboard_sign_in_sends_the_file_value_and_reports_only_a_verdict(tmp_path):
    """The real sign-in against a local server: the header carries admin:<file value>
    with the trailing newline stripped; 200 is accepted, 401 refused, a dead port
    unanswered by exception type; the value is in no answer."""
    import http.server, threading
    seen: list[str] = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, self.headers.get("Authorization") or ""))
            if self.path.startswith("/redirect"):
                # a redirect is not followed: the header must never reach another origin
                self.send_response(302); self.send_header("Location", "http://127.0.0.1:9/capture"); self.end_headers(); return
            self.send_response(200 if self.headers.get("Authorization") == "Basic " + base64.b64encode(b"admin:" + PLANTED.encode()).decode() else 401)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    pw = tmp_path / "grafana-admin-password"
    _write(pw, PLANTED + "\n", 0o600)
    real = urllib.request.Request

    def plain(url, headers=None):
        # the verb speaks https to grafana.<domain>; the local server is http
        return real(url.replace(f"https://grafana.{DOMAIN}", f"http://127.0.0.1:{srv.server_port}"), headers=headers)

    try:
        with mock.patch.object(onboard.urllib.request, "Request", plain):
            assert onboard.dashboard_sign_in(DOMAIN, pw) == "accepted"
            _write(pw, "other-" + PLANTED, 0o600)
            assert onboard.dashboard_sign_in(DOMAIN, pw) == "refused"
    except BaseException:
        srv.shutdown(); raise
    assert seen and all(PLANTED not in a for a in ["accepted", "refused"])
    with mock.patch.object(onboard.urllib.request, "Request",
                           lambda url, headers=None: real(f"http://127.0.0.1:{srv.server_port}/redirect", headers=headers)):
        assert onboard.dashboard_sign_in(DOMAIN, pw) == "unanswered (HTTP 302)"
    assert [p for p, _ in seen].count("/redirect") == 1 and not any(p.startswith("/capture") for p, _ in seen)
    with mock.patch.object(onboard.urllib.request, "Request",
                           lambda url, headers=None: real(f"http://127.0.0.1:{srv.server_port}/", headers=headers)), \
            mock.patch.dict(os.environ, {"http_proxy": "http://127.0.0.1:9", "https_proxy": "http://127.0.0.1:9"}):
        # a proxy from the environment is not consulted (the port answers nothing)
        assert onboard.dashboard_sign_in(DOMAIN, pw) == "refused"
    srv.shutdown()
    answer = onboard.dashboard_sign_in(DOMAIN, pw) if False else "unanswered (URLError)"
    with mock.patch.object(onboard.urllib.request, "Request",
                           lambda url, headers=None: real("http://127.0.0.1:9/", headers=headers)):
        answer = onboard.dashboard_sign_in(DOMAIN, pw)
    assert answer.startswith("unanswered (") and PLANTED not in answer
    assert onboard.dashboard_sign_in(DOMAIN, tmp_path / "absent") == "unanswered (FileNotFoundError)"


def test_the_first_project_is_measured_not_declared(world):
    """A catalog entry is the start of the step, not its end: the pair must be rendered,
    the applier turned on and told about it, and the pair's namespace, inventory and
    deployments live."""
    world.everything()
    world.config(apply="off")
    s = _step(world.report(), "first-project")
    assert s.state == TODO and "applyMode is 'off'" in s.detail and 'applyMode: "on"' in s.next and "platform-config" in s.next
    world.config(pairs=["other/prod"])
    s = _step(world.report(), "first-project")
    assert s.state == TODO and "applyPairs: widgets/prod" in s.detail
    world.config()
    (world.instance / "clusters/orbit/widgets-prod.yaml").unlink()
    s = _step(world.report(), "first-project")
    assert s.state == TODO and "not rendered (no Application under clusters/orbit or clusters/apps): widgets/prod" in s.detail and "make sync" in s.next
    world.project(rendered=True)
    assert _step(world.report(), "first-project").state == DONE


@pytest.mark.parametrize("missing, phrase", [("namespace", "no namespace or inventory"), ("inventory", "no namespace or inventory"),
                                             ("available", "workloads not as applied: Deployment widgets-prod/web available 1/2"),
                                             ("deployed", "workloads not as applied: Deployment widgets-prod/web missing")])
def test_a_pair_the_daemon_has_not_applied_or_that_is_not_serving_is_a_todo(world, missing, phrase):
    world.config(); world.pin_and_render(); world.credentials(); world.infrastructure(); world.node(); world.sops_key(); world.platform()
    world.sealed("monitoring", "grafana-image-renderer-token"); world.dashboard()
    world.project(**{missing: False})
    s = _step(world.report(), "first-project")
    assert s.state == TODO and phrase in s.detail


def test_acceptance_runs_only_when_asked_and_gates_completion(world, capsys):
    world.everything()
    r = world.report()
    assert _step(r, "acceptance").state == UNMEASURED and "--accept" in r.next and not r.complete
    assert [s.state for s in r.steps if s.name != "acceptance"] == [DONE] * 10
    assert world.acceptance_calls == []
    world.acceptance = (2, 3)
    s = _step(world.report(accept=True), "acceptance")
    assert s.state == TODO and "3 failing rows" in s.detail
    assert world.acceptance_calls[-1] == {"kubeconfig": str(world.kubeconfig), "config_dir": str(world.config_dir)}
    world.acceptance = (None, 0)
    assert "could not run" in _step(world.report(accept=True), "acceptance").detail
    world.acceptance = (0, 0)
    r = world.report(accept=True)
    assert r.complete and [s.state for s in r.steps] == [DONE] * 11
    rc, out, err = world.run("--accept", capsys=capsys)
    assert rc == 0 and err == ""
    assert out.splitlines()[-1].startswith("complete: ")


def test_an_incomplete_walk_exits_one_and_prints_the_next_line_last(world, capsys):
    world.config(); world.pin_and_render()
    rc, out, err = world.run(capsys=capsys)
    assert rc == 1 and err == ""
    lines = out.splitlines()
    assert lines[-1].startswith("next: ") and "Never paste a token" in lines[-1]
    assert any(line.split()[:2] == ["todo", "credentials"] for line in lines)


def test_the_json_form_carries_every_step_and_the_next_line(world, capsys):
    world.config(); world.pin_and_render(); world.credentials(); world.infrastructure()
    rc, out, err = world.run("--json", capsys=capsys)
    doc = json.loads(out)
    assert rc == 1 and err == "" and doc["complete"] is False and doc["instance"] == NAME
    assert [s["step"] for s in doc["steps"]] == STEPS
    assert doc["next"] == next(s["next"] for s in doc["steps"] if s["state"] != "done")


def test_in_a_desk_the_pointer_names_the_operator_machines_path_and_the_key_is_still_found(world, monkeypatch):
    """M4, live (docs/V1.md §8): in a desk `ssh-key-path` names the key where the operator
    machine has it, which is no path in the container. `status` said "ssh-key-path does
    not point at a file" on a platform whose `make verify` passed."""
    from orbitd.onboard import node_key_file
    world.config()
    world.pin_and_render()
    world.credentials()
    pointer = world.config_dir / "ssh-key-path"
    # the run's own key: inside the credentials directory, named by its path on the machine
    pointer.write_text("/c/Users/someone/.config/acme-platform/node-key\n")
    monkeypatch.delenv("ORBIT_SSH_KEY", raising=False)
    monkeypatch.delenv("ORBIT_HOST_CONFIG_DIR", raising=False)
    assert node_key_file(world.config_dir) is None                       # not in a desk: the path is what it says
    assert "ssh-key-path does not point at a file" in _step(world.report(), "credentials").detail, _step(world.report(), "credentials")
    monkeypatch.setenv("ORBIT_HOST_CONFIG_DIR", "/c/Users/someone/.config/acme-platform")
    assert node_key_file(world.config_dir) == world.config_dir / "node-key"
    assert _step(world.report(), "credentials").state == DONE
    # a key from elsewhere on the machine: the desk's copy
    pointer.write_text("/c/Users/someone/.ssh/id_ed25519\n")
    assert node_key_file(world.config_dir) is None
    copy = world.config_dir.parent / "desk-copy"
    copy.write_text("key\n")
    monkeypatch.setenv("ORBIT_SSH_KEY", str(copy))
    assert node_key_file(world.config_dir) == copy
    # and a pointer that names nothing anywhere is still a finding
    monkeypatch.setenv("ORBIT_SSH_KEY", str(world.config_dir.parent / "absent"))
    assert "ssh-key-path does not point at a file" in _step(world.report(), "credentials").detail


def test_the_acceptance_runner_hands_the_desks_copy_of_the_node_key_to_the_stages(tmp_path, monkeypatch):
    """Under `status --accept` and the run's acceptance every node row of verify failed in
    a desk while `make verify` typed in the same desk passed: the runner's environment
    dropped ORBIT_SSH_KEY, the one place the desk says where the key is; and then the row
    that fetches the pinned core, for the dropped GH_TOKEN (docs/V1.md §8). Both are what
    the desk gives every command typed in it; nothing else new gets through."""
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    seen = tmp_path / "seen"
    make = bin_dir / "make"
    make.write_text(f"#!/bin/sh\nprintf '%s\\n' \"${{ORBIT_SSH_KEY-unset}}\" \"${{GH_TOKEN-unset}}\" \"${{GIT_SSH_COMMAND-unset}}\" \"${{MAKEFLAGS-unset}}\" > {seen}\nexit 0\n")
    make.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("ORBIT_SSH_KEY", "/root/.ssh/node-key")
    monkeypatch.setenv("GH_TOKEN", "the-operators-token")
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -o Anything")
    monkeypatch.setenv("MAKEFLAGS", "n")
    instance = tmp_path / "inst"; instance.mkdir()
    result = run_acceptance(instance)
    assert result == (0, 0) and "the-operators-token" not in repr(result)
    assert seen.read_text().splitlines() == ["/root/.ssh/node-key", "the-operators-token", "unset", "unset"]


def test_the_acceptance_run_is_kept_privately_beside_the_credentials(tmp_path, monkeypatch):
    """A run that fails under the runner and passes when typed could not be read at all
    (docs/V1.md §8): the output of this run is kept, private, and the result still carries
    only the count."""
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    make = bin_dir / "make"
    make.write_text("#!/bin/sh\necho '  ok    row one'; echo '  FAIL  row two names what it checked'; exit 2\n")
    make.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    instance = tmp_path / "inst"; instance.mkdir()
    creds = tmp_path / "creds"; creds.mkdir()
    assert run_acceptance(instance, config_dir=str(creds)) == (2, 1)
    log = creds / "acceptance.log"
    assert "FAIL  row two names what it checked" in log.read_text()
    assert log.stat().st_mode & 0o077 == 0
    make.write_text("#!/bin/sh\necho '  ok    row one'; exit 0\n")
    assert run_acceptance(instance, config_dir=str(creds)) == (0, 0)
    assert "FAIL" not in log.read_text()                       # the last run's, not an older one's
    assert run_acceptance(instance, config_dir=str(tmp_path / "absent")) == (0, 0)


def test_the_real_acceptance_runner_passes_the_selected_paths_and_keeps_only_counts(tmp_path, monkeypatch):
    """A fake `make` on PATH records its environment and prints FAIL rows carrying the
    planted value; the runner returns the exit code and the count, nothing else, and the
    stages see the cluster and credentials directory the report measured."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    seen = tmp_path / "seen"
    make = bin_dir / "make"
    make.write_text("#!/bin/sh\n[ \"$1\" = verify ] || exit 9\n"
                    f"printf '%s\\n' \"$KUBECONFIG\" \"$ORBIT_CONFIG_DIR\" \"$PWD\" \"${{MAKEFLAGS-unset}}\" \"${{MAKEFILES-unset}}\" \"${{BASH_ENV-unset}}\" \"${{LD_PRELOAD-unset}}\" \"${{HOME-unset}}\" \"${{NO_PROXY-unset}}\" > {seen}\n"
                    f"echo 'FAIL row one {PLANTED}'; echo 'ok row'; echo '  FAIL row two'; exit 1\n")
    make.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    # Each of these, inherited, once made a suite that ran nothing report a pass; the
    # runner keeps an allow-list, so none of them reaches make, and the allowed ones do.
    monkeypatch.setenv("MAKEFLAGS", "n")
    monkeypatch.setenv("MAKEFILES", str(tmp_path / "dryrun.mk"))
    monkeypatch.setenv("BASH_ENV", str(tmp_path / "exit0.sh"))
    monkeypatch.setenv("LD_PRELOAD", "/nope.so")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    instance = tmp_path / "inst"
    instance.mkdir()
    monkeypatch.chdir(tmp_path)
    (tmp_path / "k").mkdir(); (tmp_path / "k" / "config").write_text("x")
    result = run_acceptance(Path("inst"), kubeconfig="k/config", config_dir="c/dir")
    assert result == (1, 2)
    assert seen.read_text().splitlines() == [str(tmp_path / "k" / "config"), str(tmp_path / "c" / "dir"), str(instance),
                                             "unset", "unset", "unset", "unset", str(tmp_path), "127.0.0.1"]
    assert PLANTED not in repr(result)
    make.write_text("#!/bin/sh\nexit 0\n")
    assert run_acceptance(instance) == (0, 0)
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    assert run_acceptance(instance) == (None, 0)


def test_catalog_pairs_follow_the_generator():
    projects = [{"id": "a", "environments": {"prod": {}, "dev": {}}},
                {"id": "b", "environments": {"prod": {"enabled": False}, "dev": {"enabled": True}, "stage": {"enabled": False}}},
                {"id": "ext", "hosted": "external", "environments": {"prod": {}}},
                {"id": "gone", "managed_elsewhere": True, "environments": {"prod": {}}},
                {"id": "none"}, {"id": "nul", "environments": {"dev": None}}, "junk", {"name": "no id"}]
    assert catalog_pairs(projects) == [("a", "prod"), ("b", "dev"), ("b", "prod")]


def test_a_project_hosted_elsewhere_or_a_disabled_dev_asks_for_nothing(world):
    world.everything()
    catalog = world.instance / "catalog/projects.yaml"
    catalog.write_text(catalog.read_text() + "  - id: saas\n    hosted: external\n    environments:\n      prod: {}\n")
    assert _step(world.report(), "first-project").state == DONE
    catalog.write_text("version: 3\nprojects:\n  - id: widgets\n    environments:\n      prod: {}\n      dev: {}\n")
    assert _step(world.report(), "first-project").state == DONE


def test_inventory_workloads_reads_the_daemons_lines():
    inv = {"data": {"objects": "/v1/Namespace//w-prod\napps/v1/Deployment/w-prod/api\n/v1/Service/w-prod/api\n"
                               "batch/v1/CronJob/w-prod/nightly\napps/v1/StatefulSet/w-prod/db\napps/v1/DaemonSet/w-prod/agent\nbad line\n"}}
    assert inventory_workloads(inv) == [("apps/v1", "Deployment", "w-prod", "api"), ("batch/v1", "CronJob", "w-prod", "nightly"),
                                        ("apps/v1", "StatefulSet", "w-prod", "db"), ("apps/v1", "DaemonSet", "w-prod", "agent")]
    assert inventory_workloads({}) == []


@pytest.mark.parametrize("change, phrase", [
    ({}, ""),
    ({"generation": 4}, "generation 4 not yet observed (3)"),
    ({"updated": 0}, "updated 0/2"),
    ({"updated": 2, "total": 3}, "1 old replica(s) still running"),
    ({"available": False}, "available 1/2"),
])
def test_a_deployment_serves_only_when_its_rollout_is_complete(change, phrase):
    """As `kubectl rollout status` judges it: the applied generation observed, every
    replica updated, no old replica left, every replica available. An unobserved new
    generation with old replicas still available is not serving."""
    assert workload_state("Deployment", _deployment("ns", "web", **change)) == phrase


def test_the_other_workload_kinds_are_judged_by_their_own_status():
    sts = {"metadata": {"generation": 2}, "spec": {"replicas": 1},
           "status": {"observedGeneration": 2, "updatedReplicas": 1, "replicas": 1, "readyReplicas": 1}}
    assert workload_state("StatefulSet", sts) == ""
    sts["status"]["readyReplicas"] = 0
    assert workload_state("StatefulSet", sts) == "available 0/1"
    ds = {"metadata": {"generation": 1}, "status": {"observedGeneration": 1, "desiredNumberScheduled": 1, "updatedNumberScheduled": 1, "numberAvailable": 1}}
    assert workload_state("DaemonSet", ds) == ""
    ds["status"]["numberAvailable"] = 0
    assert workload_state("DaemonSet", ds) == "available 0/1"
    assert workload_state("CronJob", {"metadata": {"generation": 1}, "spec": {"schedule": "0 * * * *"}, "status": {}}) == ""
    assert workload_state("CronJob", {"spec": {"suspend": True}}) == "suspended"


def test_a_declaration_the_daemon_has_not_applied_yet_is_a_todo(world):
    """An old inventory (its commit predates the pair's current declaration) is not a
    pass for the new declaration, whatever its old workloads are doing."""
    world.everything()
    assert _step(world.report(), "first-project").state == DONE
    assert world.landed_calls[-1] == (("widgets", "prod"), "c0ffee" * 6 + "c0ffee00")
    world.behind = "the applied commit c0ffeec0ffee predates the declaration's last change abc123 (push, then wait one reconcile)"
    s = _step(world.report(), "first-project")
    assert s.state == TODO and "widgets/prod: the applied commit" in s.detail and "wait one reconcile" in s.detail


PROBE = ("kind: Probe\nmetadata:\n  name: tenant-probe-abc\n  labels: {orbit.local/project: w, orbit.local/env: prod}\n"
         "spec: {targets: {staticConfig: {static: [https://w.example]}}}\n")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                               "GIT_COMMITTER_EMAIL": "t@t"}).stdout.strip()


@pytest.mark.skipif(shutil.which("git") is None, reason="git")
def test_declaration_landed_reads_the_git_history(tmp_path):
    """The real check: the pair file and the catalog must be committed, and the
    inventory's commit must be that commit or a descendant of it."""
    repo = tmp_path / "inst"
    (repo / "clusters/orbit").mkdir(parents=True); (repo / "catalog").mkdir()
    _git(repo, "init", "-q"); _git(repo, "checkout", "-q", "-b", "main")
    (repo / "catalog/projects.yaml").write_text("projects: []\n")
    (repo / "clusters/orbit/w-prod.yaml").write_text("kind: Application\n")
    _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "declare")
    declared = _git(repo, "rev-parse", "HEAD")
    assert declaration_landed(repo, ("w", "prod"), declared) == ""
    (repo / "README").write_text("x\n"); _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "unrelated")
    later = _git(repo, "rev-parse", "HEAD")
    assert declaration_landed(repo, ("w", "prod"), later) == ""
    # everything the daemon renders for the pair counts as its declaration, and a Probe
    # counts by its labels wherever the file sits under clusters/apps (not by a name pattern)
    (repo / "clusters/apps").mkdir(parents=True)
    other = "kind: Probe\nmetadata: {name: o, labels: {orbit.local/project: other, orbit.local/env: prod}}\nspec: {}\n"
    (repo / "clusters/apps/other-probe.yaml").write_text(other)
    _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "another pair's probe")
    assert declaration_landed(repo, ("w", "prod"), later) == "", "another pair's Probe is not this pair's declaration"
    for rel in ("charts/service/templates/deployment.yaml", "clusters/apps/namespaces.yaml",
                "clusters/apps/dashboard-w-prod.yaml", "clusters/apps/any-name-at-all.yaml"):
        body = PROBE if rel.endswith("any-name-at-all.yaml") else rel + "\n"
        (repo / rel).parent.mkdir(parents=True, exist_ok=True); (repo / rel).write_text(body)
        _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", rel)
        assert "predates the declaration" in declaration_landed(repo, ("w", "prod"), later), rel
        later = _git(repo, "rev-parse", "HEAD")
        assert declaration_landed(repo, ("w", "prod"), later) == ""
    (repo / "clusters/orbit/w-prod.yaml").write_text("kind: Application\nspec: {}\n")
    assert "uncommitted" in declaration_landed(repo, ("w", "prod"), later)
    _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "change the pair")
    assert "predates the declaration" in declaration_landed(repo, ("w", "prod"), later)
    assert "not in the local history" in declaration_landed(repo, ("w", "prod"), "0" * 40)
    # a Probe the applied tree held and the working tree no longer does is a change too
    applied = _git(repo, "rev-parse", "HEAD")
    assert declaration_landed(repo, ("w", "prod"), applied) == ""
    (repo / "clusters/apps/any-name-at-all.yaml").unlink()
    _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "drop the probe")
    assert "predates the declaration" in declaration_landed(repo, ("w", "prod"), applied)
    assert declaration_landed(repo, ("w", "prod"), _git(repo, "rev-parse", "HEAD")) == ""
    (repo / "clusters/apps/relabelled.yaml").write_text(PROBE)
    (repo / "clusters/apps/door probe.yaml").write_text(PROBE)   # a space in the name survives the tree listing
    _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "probe back")
    applied = _git(repo, "rev-parse", "HEAD")
    (repo / "clusters/apps/relabelled.yaml").write_text(PROBE.replace("orbit.local/project: w", "orbit.local/project: other"))
    assert "uncommitted" in declaration_landed(repo, ("w", "prod"), applied)
    _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "relabel")
    assert "predates the declaration" in declaration_landed(repo, ("w", "prod"), applied)
    applied = _git(repo, "rev-parse", "HEAD")
    assert onboard._probe_files_for(repo, ("w", "prod"), applied) == ["clusters/apps/door probe.yaml"]
    (repo / "clusters/apps/door probe.yaml").unlink()
    _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "drop the spaced probe")
    assert "predates the declaration" in declaration_landed(repo, ("w", "prod"), applied)
    # a commit field that is not a hash is never handed to git or repeated in the finding
    for bad in ("", "main; rm -rf /", "--output=/tmp/x", "PLANTED-VALUE", "abc"):
        told = declaration_landed(repo, ("w", "prod"), bad)
        assert told in ("the inventory carries no commit hash", "the inventory's commit is not a hash"), bad
        assert "PLANTED" not in told and "rm" not in told
    assert "carries no commit" in declaration_landed(repo, ("w", "prod"), "")
    (tmp_path / "plain").mkdir()
    assert declaration_landed(tmp_path / "plain", ("w", "prod"), later) != ""


def test_jobs_and_database_clusters_are_judged_by_completion_and_health():
    assert workload_state("Job", {"spec": {"completions": 1}, "status": {"succeeded": 1}}) == ""
    assert workload_state("Job", {"spec": {}, "status": {"active": 1}}) == "running (0/1 complete)"
    assert workload_state("Job", {"spec": {}, "status": {"failed": 3}}) == "failed (3 attempt(s))"
    healthy = {"spec": {"instances": 2}, "status": {"phase": "Cluster in healthy state", "readyInstances": 2}}
    assert workload_state("Cluster", healthy) == ""
    assert workload_state("Cluster", {"spec": {"instances": 2}, "status": {"phase": "Cluster in healthy state", "readyInstances": 1}}) == "ready instances 1/2"
    assert workload_state("Cluster", {"spec": {"instances": 1}, "status": {"phase": "Setting up primary " + PLANTED, "readyInstances": 1}}) == "not in the healthy phase"
    inv = {"data": {"objects": "postgresql.cnpg.io/v1/Cluster/w-prod/w-db\nbatch/v1/Job/w-prod/w-db-bootstrap\napps/v1/Deployment/w-prod/web\n"}}
    assert inventory_workloads(inv) == [("postgresql.cnpg.io/v1", "Cluster", "w-prod", "w-db"), ("batch/v1", "Job", "w-prod", "w-db-bootstrap"),
                                        ("apps/v1", "Deployment", "w-prod", "web")]


def test_a_missing_database_or_job_is_not_masked_by_a_serving_deployment(world):
    world.config(); world.pin_and_render(); world.credentials(); world.infrastructure(); world.node(); world.sops_key(); world.platform()
    world.sealed("monitoring", "grafana-image-renderer-token"); world.dashboard()
    world.project(workloads="apps/v1/Deployment/{ns}/web\npostgresql.cnpg.io/v1/Cluster/{ns}/widgets-db\nbatch/v1/Job/{ns}/widgets-db-bootstrap")
    s = _step(world.report(), "first-project")
    assert s.state == TODO and "Cluster widgets-prod/widgets-db missing" in s.detail and "Job widgets-prod/widgets-db-bootstrap missing" in s.detail
    world.kube.add_object({"apiVersion": "postgresql.cnpg.io/v1", "kind": "Cluster", "metadata": {"name": "widgets-db", "namespace": "widgets-prod"},
                          "spec": {"instances": 1}, "status": {"phase": "Cluster in healthy state", "readyInstances": 1}})
    world.kube.add_object({"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": "widgets-db-bootstrap", "namespace": "widgets-prod"},
                          "spec": {"completions": 1}, "status": {"succeeded": 1}})
    assert _step(world.report(), "first-project").state == DONE


def test_a_pair_the_generator_wrote_under_apps_is_rendered(world):
    """With orbitd.applyPairs empty (every pair), the generator writes the pair's
    Application under clusters/apps, not clusters/orbit; the verb resolves the file as
    the applier does, so the skill's recommended empty list can complete."""
    world.everything()
    (world.instance / "clusters/orbit/widgets-prod.yaml").unlink()
    s = _step(world.report(), "first-project")
    assert s.state == TODO and "no Application under clusters/orbit or clusters/apps" in s.detail
    _write(world.instance / "clusters/apps/widgets-prod.yaml", "kind: Application\n")
    assert _step(world.report(), "first-project").state == DONE
    assert pair_file_rel(world.instance, ("widgets", "prod")) == "clusters/apps/widgets-prod.yaml"
    _write(world.instance / "clusters/orbit/widgets-prod.yaml", "kind: Application\n")
    assert pair_file_rel(world.instance, ("widgets", "prod")) == "clusters/orbit/widgets-prod.yaml"


def test_basic_auth_selectors_and_a_supplied_acme_key_are_references():
    rendered = [("kps", [
        {"kind": "Prometheus", "metadata": {"name": "p", "namespace": "monitoring"},
         "spec": {"remoteWrite": [{"url": "https://x", "basicAuth": {"username": {"name": "remote-user", "key": "user"},
                                                                    "password": {"name": "remote-password", "key": "password"}}}]}},
        {"kind": "ClusterIssuer", "metadata": {"name": "supplied"},
         "spec": {"acme": {"disableAccountKeyGeneration": True, "privateKeySecretRef": {"name": "le-supplied"}}}},
        {"kind": "ClusterIssuer", "metadata": {"name": "generated"},
         "spec": {"acme": {"privateKeySecretRef": {"name": "le-generated"}}}},
    ])]
    assert required_secrets(rendered, {"kps": "monitoring"}) == {("monitoring", "remote-user"), ("monitoring", "remote-password"),
                                                                 ("monitoring", "le-supplied")}


def test_the_backup_stores_s3_selectors_are_references():
    rendered = [("t", [{"kind": "ObjectStore", "metadata": {"name": "store", "namespace": "w-prod"},
                        "spec": {"configuration": {"s3Credentials": {"accessKeyId": {"name": "orbit-s3", "key": "ACCESS_KEY_ID"},
                                                                     "secretAccessKey": {"name": "orbit-s3", "key": "SECRET_ACCESS_KEY"}}}}}])]
    assert required_secrets(rendered, {}) == {("w-prod", "orbit-s3")}


def test_a_cron_only_pair_completes_and_an_unobserved_rollout_does_not(world):
    """The service chart renders a CronJob for a `kind: cron` component and nothing else;
    such a pair is serving when the CronJob exists and is not suspended."""
    world.config(); world.pin_and_render(); world.credentials(); world.infrastructure(); world.node(); world.sops_key(); world.platform()
    world.sealed("monitoring", "grafana-image-renderer-token"); world.dashboard()
    world.project(workloads="batch/v1/CronJob/{ns}/nightly")
    s = _step(world.report(), "first-project")
    assert s.state == TODO and "CronJob widgets-prod/nightly missing" in s.detail
    world.kube.add_object({"apiVersion": "batch/v1", "kind": "CronJob", "metadata": {"name": "nightly", "namespace": "widgets-prod", "generation": 1},
                          "spec": {"schedule": "0 3 * * *"}, "status": {}})
    assert _step(world.report(), "first-project").state == DONE
    world.project(pid="shop", generation=5, observed=4, updated=0)
    s = _step(world.report(), "first-project")
    assert s.state == TODO and "Deployment shop-prod/web generation 5 not yet observed (4)" in s.detail
    world.project(pid="empty", workloads="/v1/ConfigMap/{ns}/settings")
    assert "empty/prod (the inventory lists no workload)" in _step(world.report(), "first-project").detail


def test_a_secrets_payload_is_never_a_reference():
    """A sealed template whose data key happens to be called secretName, a Secret's own
    stringData, a ConfigMap's data: none of it is walked, so no value (ciphertext or
    clear) can be mistaken for a Secret's name."""
    rendered = [("app", [
        {"kind": "SopsSecret", "metadata": {"name": "s", "namespace": "ns"},
         "spec": {"secretTemplates": [{"name": "made", "data": {"secretName": PLANTED, "secretRef": {"name": PLANTED}}}]}},
        {"kind": "Secret", "metadata": {"name": "raw", "namespace": "ns"}, "stringData": {"secretName": PLANTED}},
        {"kind": "ConfigMap", "metadata": {"name": "cm", "namespace": "ns"}, "data": {"existingSecret": PLANTED}},
        {"kind": "Pod", "metadata": {"name": "p", "namespace": "ns"}, "spec": {"volumes": [{"secret": {"secretName": "wanted"}}],
                                                                              "data": {"secretName": PLANTED}}},
    ])]
    assert required_secrets(rendered, {}) == {("ns", "wanted")}


def test_the_operators_secret_shapes_are_references():
    """Traefik names a Secret under `secret:`; the Prometheus operator's tlsConfig uses
    `{secret: {name, key}}` and `keySecret`; a Pod volume's `secret: {secretName}` is the
    third shape and is found by recursion, not by the `secret` rule."""
    rendered = [("t", [
        {"kind": "Middleware", "metadata": {"name": "auth", "namespace": "widgets-dev"}, "spec": {"basicAuth": {"secret": "dev-basic-auth"}}},
        {"kind": "Middleware", "metadata": {"name": "fwd", "namespace": "widgets-dev"}, "spec": {"forwardAuth": {"tls": {"certSecret": "fwd-tls"}}}},
        {"kind": "ServiceMonitor", "metadata": {"name": "sm", "namespace": "monitoring"},
         "spec": {"endpoints": [{"tlsConfig": {"ca": {"secret": {"name": "kube-prometheus-stack-admission", "key": "ca"}},
                                                "cert": {"configMap": {"name": "not-a-secret", "key": "c"}},
                                                "keySecret": {"name": "sm-key", "key": "k"}}}]}},
        {"kind": "Pod", "metadata": {"name": "p", "namespace": "widgets-dev"}, "spec": {"volumes": [{"secret": {"secretName": "vol"}}]}},
    ])]
    assert required_secrets(rendered, {}) == {("widgets-dev", "dev-basic-auth"), ("widgets-dev", "fwd-tls"), ("monitoring", "kube-prometheus-stack-admission"),
                                              ("monitoring", "sm-key"), ("widgets-dev", "vol")}


def test_the_prometheus_operators_secret_lists_and_selectors_are_references():
    rendered = [("kps", [
        {"kind": "Alertmanager", "metadata": {"name": "am", "namespace": "monitoring"},
         "spec": {"secrets": ["alertmanager-telegram", "orbit-alert-webhook"], "configSecret": "am-config"}},
        {"kind": "Prometheus", "metadata": {"name": "prom", "namespace": "monitoring"},
         "spec": {"secrets": ["prom-extra"], "additionalScrapeConfigs": {"name": "extra-scrape", "key": "x.yaml"}}},
        {"kind": "Deployment", "metadata": {"name": "d", "namespace": "monitoring"}, "spec": {"secrets": ["not-a-reference-here"]}},
    ])]
    assert required_secrets(rendered, {}) == {("monitoring", n) for n in
                                              ("alertmanager-telegram", "orbit-alert-webhook", "am-config", "prom-extra", "extra-scrape")}


def test_quiet_silences_the_error_descriptor_itself(capfd):
    """On a terminal the kubernetes loader hands a credential plugin the process's own
    stderr descriptor; the silence is at that level, so a child inheriting it says
    nothing, and the descriptor and logging come back after."""
    logging.disable(35)
    try:
        with _quiet():
            os.write(2, f"{PLANTED} raw\n".encode())
            sys.stderr.write(f"{PLANTED} stream\n")
            subprocess.run(["sh", "-c", f"echo {PLANTED} child >&2"], check=False)
            logging.getLogger().error("%s log", PLANTED)
        assert logging.root.manager.disable == 35
    finally:
        logging.disable(0)
    os.write(2, b"after\n")
    err = capfd.readouterr().err
    assert PLANTED not in err and err.endswith("after\n")


# ------------------------------------------------------------------ the property

@pytest.mark.parametrize("flags", [(), ("--json",)])
def test_no_value_from_any_file_or_secret_reaches_either_stream(world, capsys, flags):
    """Every credential file, every live Secret, every sealed file and every rendered
    object carries the same planted value; neither stream, in either form, ever does.
    Existence, mode, key names and reference names are the whole of what the verb reads."""
    world.everything()
    world.dashboard(live=False)  # a todo path that names the Secret
    world.sealed("loki", "orbit-s3", live=True, rendered=False)  # a todo path that names a sealed file
    rc, out, err = world.run(*flags, capsys=capsys)
    assert PLANTED not in out and PLANTED not in err
    assert "AGE-SECRET-KEY" not in out and "ENC[" not in out
    assert err == ""
    assert rc == 1


@pytest.mark.parametrize("flags", [(), ("--json",)])
@pytest.mark.parametrize("kind, name, step", [("Secret", "sops-age", "sops-key"), ("ConfigMap", "orbit-platform-inventory-cert-manager", "platform"),
                                              ("SopsSecret", "grafana-admin", "secrets"), ("Namespace", "widgets-prod", "first-project")])
def test_an_api_status_other_than_yes_or_no_blocks_the_step_without_its_body(world, capsys, flags, kind, name, step):
    """A 500 whose body carries a value (a real API error page can) blocks the one step
    that asked, names the status, and puts nothing on either stream but the report."""
    world.everything()
    world.kube = AnsweringKube(world.kube, kind, name, 500)
    r = world.report()
    s = _step(r, step)
    assert s.state == BLOCKED and "500" in s.detail and PLANTED not in s.detail and PLANTED not in s.next
    rc, out, err = world.run(*flags, capsys=capsys)
    assert rc == 1 and PLANTED not in out and PLANTED not in err and err == ""


def test_a_kubeconfig_loader_that_logs_a_credential_plugins_output_is_silenced(world, capsys):
    """The kubernetes loader logs a credential plugin's stderr at ERROR on the root logger,
    where the last-resort handler would print it. The verb silences logging around every
    cluster access, and a factory that fails is an unreachable node by type only."""
    world.config(); world.pin_and_render(); world.credentials(); world.infrastructure(); world.node()
    # As in a real process, where the last-resort handler prints ERROR lines to stderr
    # (pytest's own capture handler would otherwise swallow them and hide a regression).
    handler = logging.StreamHandler()
    logging.getLogger().addHandler(handler)

    def loud(_kubeconfig):
        logging.getLogger("kubernetes.config.kube_config").error("exec plugin said: %s", PLANTED)
        logging.getLogger().error("loader: %s", PLANTED)
        raise RuntimeError(PLANTED)
    try:
        r = status(world.instance, home=world.home, kube_factory=loud, tofu_state=lambda _d: None, render=world._render)
    finally:
        logging.getLogger().removeHandler(handler)
    s = _step(r, "node")
    assert s.state == BLOCKED and "RuntimeError" in s.detail and PLANTED not in s.detail
    err = capsys.readouterr().err
    assert PLANTED not in err and err == ""
    assert logging.root.manager.disable == 0, "logging is restored after the call"


def test_a_failure_the_verb_did_not_write_reaches_stderr_by_type_only(world, capsys):
    def boom(_kubeconfig):
        raise RuntimeError(PLANTED)
    args = SimpleNamespace(instance_dir=str(world.instance), config_dir=str(world.config_dir), kubeconfig=str(world.kubeconfig),
                           home=str(world.home), json=False, accept=False, kube_factory=boom,
                           tofu_state=lambda _d: (_ for _ in ()).throw(RuntimeError(PLANTED)), render=world._render,
                           acceptance=None, onboard_command="status")
    world.config(); world.pin_and_render(); world.credentials(); world.infrastructure()
    rc = onboard.cmd_onboard(args)
    captured = capsys.readouterr()
    assert rc == 1 and "could not measure (RuntimeError)" in captured.err and PLANTED not in captured.err and PLANTED not in captured.out


def test_existence_lookups_never_select_data(world):
    """The fake records every dynamic call; a lookup is by kind, name and namespace only,
    and the objects returned are not opened past their metadata (no call carries a field
    selector, a label selector or a data path); the one list is of Deployments."""
    world.everything()
    world.report()
    calls = [c for c in world.kube.dynamic_calls if c["kind"] in ("Secret", "SopsSecret", "ConfigMap", "Namespace")]
    assert calls, "the cluster steps were measured"
    for c in calls:
        assert c["name"], c
        assert c.get("label_selector") is None and c.get("field_selector") is None, c
    assert {c["kind"] for c in world.kube.dynamic_calls if not c.get("name")} <= {"Deployment"}
