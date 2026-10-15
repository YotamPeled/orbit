#!/usr/bin/env python3
"""
Per-component egress, the CIDR half — run as `python3 scripts/test_catalog_egress.py`.
No cluster, no credentials, no network.

WHY IT EXISTS. Every tenant namespace today carries one NetworkPolicy that denies
ingress and grants egress to `0.0.0.0/0` minus the node's metadata address. So the
engine pod, which holds the model provider's key and processes a stranger's repository,
can open a connection to any address on the internet. The product's promise is the
opposite. Phase 9 narrows that grant per component: a component declares the
infrastructure it needs by name (`egress: {cidrs: [postgres, s3, dns]}`) and reaches
nothing else.

THE ONE THING THAT MAKES THIS HARD, and the reason most of these tests exist:
NetworkPolicies are additive. Two policies selecting the same pod produce the UNION of
their rules, never the intersection. So a per-component policy that grants three
addresses, added beside a namespace-wide policy that grants the whole internet, changes
nothing at all — the pod still reaches everything, the repository reads as if it were
locked down, and the only way to find out is to try. The narrowing is therefore not
"add a policy"; it is "stop the open policy from selecting this pod", and that is what
`test_the_open_grant_no_longer_selects_a_narrowed_component` pins.

The second trap is the other direction. The open grant must keep selecting everything
that is NOT a narrowed component — the database pods CloudNativePG creates, the
generated probe and backup Jobs, anything a future chart adds. An exclusion written as
"every pod whose component name is not in this list" silently drops every pod that
carries no component name at all, which is the database. So the exclusion is keyed on a
label that only a narrowed pod carries, and absence means "not narrowed".

The third is the alias table. `postgres` and `dns` are addresses inside the cluster and
the core can render them for any org. `s3` is an address on the internet that differs
per org, and the core carries no org's addresses — so an alias the core does not build
in must be declared by the instance, and an alias nobody declared is a refusal, not an
empty rule set. An empty rule set reads as "deny everything" and the component simply
stops working on the next apply, in production, with no message.

`egress.hosts` — the hostname half — is refused here by design. The shared proxy that
serves it is the next node; until it exists, a component declaring `hosts:` must be told
so at generation rather than silently rendered with unrestricted internet access.
"""
import ipaddress
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
CHART = ROOT / "charts" / "service"
HELM = shutil.which("helm")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
_BYTECODE, sys.dont_write_bytecode = sys.dont_write_bytecode, True
from test_catalog_quota import Sandbox  # noqa: E402
sys.dont_write_bytecode = _BYTECODE

# The label a narrowed pod carries. Its ABSENCE is what keeps every other pod on the
# open grant, so the open policy selects on `DoesNotExist` and never on a name list.
EGRESS_LABEL = "orbit.local/egress"
LINK_LOCAL = ipaddress.ip_network("169.254.0.0/16")


def catalog_with(egress: dict, comp: str = "web") -> str:
    """The quota fixture's catalog with one component given an `egress:` block."""
    text = (ROOT / "scripts" / "testdata" / "catalog-quota"
            / "catalog" / "projects.yaml").read_text()
    block = "egress: " + yaml.safe_dump(egress, default_flow_style=True).strip()
    needle = f"      - name: {comp}\n"
    assert needle in text, f"fixture has no component {comp!r}"
    return text.replace(needle, needle + f"        {block}\n", 1)


class Fixture:
    """A sandbox whose catalog and instance config this test controls."""

    def __init__(self, catalog_text: str, instance_extra: dict | None = None):
        self.box = Sandbox()
        (self.box.root / "catalog" / "projects.yaml").write_text(catalog_text)
        if instance_extra:
            path = self.box.root / "instance" / "config.yaml"
            conf = yaml.safe_load(path.read_text()) or {}
            conf.update(instance_extra)
            path.write_text(yaml.safe_dump(conf, sort_keys=True))
        self.proc = self.box.generate()

    def close(self):
        self.box.close()

    def output(self) -> str:
        return (self.proc.stdout or "") + (self.proc.stderr or "")

    def policies(self, namespace: str) -> list:
        return [d for d in self.box.docs("namespaces.yaml")
                if d.get("kind") == "NetworkPolicy"
                and d["metadata"]["namespace"] == namespace]

    def egress_rules(self, policy: dict) -> list:
        return (policy.get("spec") or {}).get("egress") or []

    def selects(self, policy: dict, labels: dict) -> bool:
        """Does this policy's podSelector select a pod carrying `labels`?

        The real API-server semantics, not a shape check: matchLabels are exact and
        matchExpressions honour Exists / DoesNotExist / In / NotIn on an ABSENT key.
        A pod with no such label does not match In, and does match DoesNotExist.
        """
        sel = (policy.get("spec") or {}).get("podSelector")
        if sel is None:
            return False
        for key, value in (sel.get("matchLabels") or {}).items():
            if labels.get(key) != value:
                return False
        for expr in sel.get("matchExpressions") or []:
            key, op = expr["key"], expr["operator"]
            present = key in labels
            values = expr.get("values") or []
            if op == "Exists" and not present:
                return False
            if op == "DoesNotExist" and present:
                return False
            if op == "In" and (not present or labels[key] not in values):
                return False
            if op == "NotIn" and present and labels[key] in values:
                return False
        return True


def pod(project: str, component: str, narrowed: bool = False) -> dict:
    """A component's pod as the chart labels it."""
    labels = {"app.kubernetes.io/name": component,
              "app.kubernetes.io/instance": project}
    if narrowed:
        labels[EGRESS_LABEL] = "declared"
    return labels


# The chart names a project's database Cluster <project>-db, so its instance pods
# carry that and NOT the namespace name. The first spelling here was wrong and no
# test noticed, because every test using it keys on the ABSENCE of a label.
DATABASE_POD = {"cnpg.io/cluster": "widgets-db", "cnpg.io/podRole": "instance"}


class NarrowedComponentTest(unittest.TestCase):
    """A component that declares `egress.cidrs` gets its own policy and nothing more."""

    def setUp(self):
        self.fx = Fixture(catalog_with({"cidrs": ["postgres", "dns"]}))
        self.addCleanup(self.fx.close)
        self.assertEqual(self.fx.proc.returncode, 0, self.fx.output()[-2000:])

    def own(self) -> dict:
        mine = [p for p in self.fx.policies("widgets-prod")
                if self.fx.selects(p, pod("widgets", "web", narrowed=True))
                and not self.fx.selects(p, pod("widgets", "importer"))]
        self.assertEqual(len(mine), 1,
                         "expected exactly one policy selecting the narrowed component "
                         f"alone, found {len(mine)}: "
                         + ", ".join(p['metadata']['name'] for p in mine))
        return mine[0]

    def test_the_component_gets_its_own_egress_policy(self):
        policy = self.own()
        self.assertIn("Egress", (policy["spec"].get("policyTypes") or []))
        self.assertTrue(self.fx.egress_rules(policy),
                        "the narrowed component's policy grants nothing at all")

    def test_the_dns_alias_reaches_the_cluster_resolver(self):
        rules = self.fx.egress_rules(self.own())
        ports = [p for rule in rules for p in (rule.get("ports") or [])
                 if int(p.get("port", 0)) == 53]
        self.assertTrue(ports, "a narrowed component cannot resolve a name:\n"
                               + yaml.safe_dump(rules))
        self.assertEqual({p.get("protocol") for p in ports}, {"TCP", "UDP"},
                         "DNS needs both protocols; a TCP-only grant fails on a large "
                         "answer and looks like an intermittent resolver")

    def test_the_postgres_alias_reaches_this_tenants_own_database(self):
        rules = self.fx.egress_rules(self.own())
        hits = [rule for rule in rules
                for p in (rule.get("ports") or []) if int(p.get("port", 0)) == 5432]
        self.assertTrue(hits, "the postgres alias granted no database port:\n"
                              + yaml.safe_dump(rules))
        for rule in hits:
            for peer in rule.get("to") or []:
                self.assertNotIn("ipBlock", peer,
                                 "the tenant's own database is a pod in its own "
                                 "namespace, not an address range")

    def test_a_component_that_declares_nothing_keeps_no_policy_of_its_own(self):
        mine = [p for p in self.fx.policies("widgets-prod")
                if self.fx.selects(p, pod("widgets", "importer"))
                and not self.fx.selects(p, DATABASE_POD)]
        self.assertEqual(mine, [], "a component that declared no egress was narrowed "
                                   "anyway: " + ", ".join(
                                       p["metadata"]["name"] for p in mine))


class AdditiveTest(unittest.TestCase):
    """The union rule: a policy that still grants the internet undoes the narrowing."""

    def setUp(self):
        self.fx = Fixture(catalog_with({"cidrs": ["dns"]}))
        self.addCleanup(self.fx.close)
        self.assertEqual(self.fx.proc.returncode, 0, self.fx.output()[-2000:])

    def open_grants(self, labels: dict) -> list:
        """Every rule selecting this pod that reaches an address outside the cluster."""
        out = []
        for policy in self.fx.policies("widgets-prod"):
            if not self.fx.selects(policy, labels):
                continue
            for rule in self.fx.egress_rules(policy):
                for peer in rule.get("to") or []:
                    cidr = (peer.get("ipBlock") or {}).get("cidr")
                    if cidr and ipaddress.ip_network(cidr).num_addresses > 65536:
                        out.append(f"{policy['metadata']['name']}: {cidr}")
        return out

    def test_the_open_grant_no_longer_selects_a_narrowed_component(self):
        """THE test. Policies are additive, so narrowing is removing the pod from the
        open policy — adding a second policy beside it changes nothing."""
        leaks = self.open_grants(pod("widgets", "web", narrowed=True))
        self.assertEqual(leaks, [], "the narrowed component is still selected by a "
                                    "policy granting the internet, so its own policy "
                                    "buys nothing:\n" + "\n".join(leaks))

    def test_a_component_that_declared_nothing_keeps_the_open_grant(self):
        self.assertTrue(self.open_grants(pod("widgets", "importer")),
                        "narrowing one component cut off every other workload in the "
                        "namespace, which is an outage, not a policy")

    def test_the_database_pods_keep_the_open_grant(self):
        """The exclusion must key on a label only a narrowed pod carries. Written as
        'component name not in [web]' it also drops every pod with no component name —
        the database, the backup Job — and the tenant loses its backups."""
        self.assertTrue(self.open_grants(DATABASE_POD),
                        "the database pods lost their egress: the exclusion is keyed "
                        "on something the database does not carry")

    def test_the_open_grant_still_withholds_the_node_metadata_address(self):
        for policy in self.fx.policies("widgets-prod"):
            for rule in self.fx.egress_rules(policy):
                for peer in rule.get("to") or []:
                    block = peer.get("ipBlock") or {}
                    net = block.get("cidr")
                    if not net or not ipaddress.ip_network(net).overlaps(LINK_LOCAL):
                        continue
                    excepted = [ipaddress.ip_network(e) for e in block.get("except") or []]
                    self.assertTrue(
                        any(LINK_LOCAL.subnet_of(e) for e in excepted),
                        f"{policy['metadata']['name']} reaches the node's metadata "
                        "service, which serves the node's root password to any pod")

    def test_a_narrowed_component_cannot_reach_the_metadata_address_either(self):
        for policy in self.fx.policies("widgets-prod"):
            if not self.fx.selects(policy, pod("widgets", "web", narrowed=True)):
                continue
            for rule in self.fx.egress_rules(policy):
                for peer in rule.get("to") or []:
                    block = peer.get("ipBlock") or {}
                    net = block.get("cidr")
                    if not net or not ipaddress.ip_network(net).overlaps(LINK_LOCAL):
                        continue
                    excepted = [ipaddress.ip_network(e) for e in block.get("except") or []]
                    self.assertTrue(
                        any(LINK_LOCAL.subnet_of(e) for e in excepted),
                        "a narrowed component reaches the node's metadata service")


class AliasTableTest(unittest.TestCase):
    """An address the core cannot know comes from the instance, and an alias nobody
    declared is refused rather than rendered empty."""

    S3 = {"egressTargets": {"s3": ["198.51.100.0/24"]}}

    def test_an_instance_declared_alias_renders_its_addresses(self):
        fx = Fixture(catalog_with({"cidrs": ["s3", "dns"]}), self.S3)
        self.addCleanup(fx.close)
        self.assertEqual(fx.proc.returncode, 0, fx.output()[-2000:])
        seen = {(peer.get("ipBlock") or {}).get("cidr")
                for policy in fx.policies("widgets-prod")
                if fx.selects(policy, pod("widgets", "web", narrowed=True))
                for rule in fx.egress_rules(policy)
                for peer in rule.get("to") or []}
        self.assertIn("198.51.100.0/24", seen,
                      "the instance's own object-store range never reached the policy")

    def test_an_alias_nobody_declared_is_refused(self):
        fx = Fixture(catalog_with({"cidrs": ["s3"]}))
        self.addCleanup(fx.close)
        self.assertNotEqual(fx.proc.returncode, 0,
                            "an undeclared alias generated silently, so the component "
                            "ships with an empty grant and stops working on apply:\n"
                            + fx.output()[-2000:])
        self.assertIn("s3", fx.output(), "the refusal does not name the alias")

    def test_the_refusal_says_what_is_accepted(self):
        fx = Fixture(catalog_with({"cidrs": ["postgress"]}))
        self.addCleanup(fx.close)
        self.assertNotEqual(fx.proc.returncode, 0, fx.output()[-2000:])
        self.assertIn("postgres", fx.output(),
                      "a one-letter typo must be answerable from the message alone")

    def test_an_instance_alias_covering_the_metadata_address_is_refused(self):
        """An org that writes `s3: [0.0.0.0/0]` has not narrowed anything, and one that
        writes a range containing the node's link-local address has handed out the
        node's root password."""
        for bad in ("0.0.0.0/0", "169.254.0.0/16", "169.254.169.254/32"):
            fx = Fixture(catalog_with({"cidrs": ["s3"]}),
                         {"egressTargets": {"s3": [bad]}})
            self.addCleanup(fx.close)
            self.assertNotEqual(fx.proc.returncode, 0,
                                f"an egress target of {bad} was accepted:\n"
                                + fx.output()[-1500:])
            self.assertIn(bad, fx.output(), "the refusal does not name the range")

    def test_a_target_that_is_not_a_cidr_is_refused(self):
        fx = Fixture(catalog_with({"cidrs": ["s3"]}),
                     {"egressTargets": {"s3": ["fsn1.your-objectstorage.com"]}})
        self.addCleanup(fx.close)
        self.assertNotEqual(fx.proc.returncode, 0,
                            "a hostname was accepted as an address range; a policy "
                            "cannot express a name and would grant nothing:\n"
                            + fx.output()[-1500:])


class HostsTest(unittest.TestCase):
    """The hostname half is the next node. Until its proxy exists, declaring a host
    must be refused — an accepted-and-ignored `hosts:` leaves the component on the
    open grant while the catalog reads as if it were restricted."""

    def test_a_component_declaring_hosts_is_refused(self):
        fx = Fixture(catalog_with({"cidrs": ["dns"], "hosts": ["api.example.org"]}))
        self.addCleanup(fx.close)
        self.assertNotEqual(fx.proc.returncode, 0,
                            "a hostname allowlist was accepted with no proxy to serve "
                            "it:\n" + fx.output()[-2000:])
        self.assertIn("hosts", fx.output(), "the refusal does not name the key")

    def test_an_unknown_egress_key_is_refused(self):
        fx = Fixture(catalog_with({"cidr": ["dns"]}))
        self.addCleanup(fx.close)
        self.assertNotEqual(fx.proc.returncode, 0,
                            "`cidr:` for `cidrs:` generated silently, and a typo that "
                            "reads as a lockdown is an open pod:\n"
                            + fx.output()[-2000:])


class UnchangedTest(unittest.TestCase):
    """A catalog that declares no egress at all renders exactly what it rendered
    before this phase — the narrowing is opt-in, and nothing regresses for an org that
    has not adopted it."""

    def test_the_fixture_catalog_renders_byte_identical_namespaces(self):
        expected = (ROOT / "scripts" / "testdata" / "catalog-quota"
                    / "expected" / "namespaces.yaml").read_text()
        box = Sandbox()
        self.addCleanup(box.close)
        proc = box.generate()
        self.assertEqual(proc.returncode, 0, (proc.stdout + proc.stderr)[-2000:])
        self.assertEqual(box.namespaces_text(), expected,
                         "adding per-component egress changed the render of a catalog "
                         "that declares none")


@unittest.skipUnless(HELM, "helm is not on PATH")
class ChartLabelTest(unittest.TestCase):
    """The label the open policy keys on is stamped by the chart, on exactly the
    components that declare egress."""

    VALUES = """\
project: charttest
env: prod
components:
  - name: narrowed
    kind: worker
    image: example/narrowed
    egress: {cidrs: [dns]}
  - name: plain
    kind: worker
    image: example/plain
"""

    def render(self, values: str) -> list:
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="orbit-egress-chart-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "values.yaml").write_text(values)
        proc = subprocess.run(
            [HELM, "template", "t", str(CHART), "-f", str(tmp / "values.yaml")],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        return [d for d in yaml.safe_load_all(proc.stdout) if d]

    def pod_labels(self, docs, name):
        for doc in docs:
            if doc.get("kind") in ("Deployment", "StatefulSet") \
                    and doc["metadata"]["name"].endswith(name):
                return doc["spec"]["template"]["metadata"]["labels"]
        raise AssertionError(f"no workload rendered for {name!r}")

    def test_a_component_that_declares_egress_is_labelled(self):
        docs = self.render(self.VALUES)
        self.assertEqual(self.pod_labels(docs, "narrowed").get(EGRESS_LABEL),
                         "declared",
                         "the narrowed pod carries no label, so the open policy still "
                         "selects it and the narrowing is a no-op")

    def test_a_component_that_declares_none_is_not_labelled(self):
        docs = self.render(self.VALUES)
        self.assertNotIn(EGRESS_LABEL, self.pod_labels(docs, "plain"),
                         "an unlabelled component was labelled anyway, so it lost the "
                         "open grant without asking for it")

    def test_the_label_is_not_a_selector_label(self):
        """Selector labels are immutable on a Deployment. Adding egress to a live
        component would then need the workload deleted and recreated, which is an
        outage for a change that is supposed to be a policy edit."""
        docs = self.render(self.VALUES)
        for doc in docs:
            if doc.get("kind") in ("Deployment", "StatefulSet"):
                selector = doc["spec"]["selector"].get("matchLabels") or {}
                self.assertNotIn(EGRESS_LABEL, selector,
                                 f"{doc['metadata']['name']} matches on the egress "
                                 "label, so declaring egress needs a delete and "
                                 "recreate")

    def test_the_schema_names_the_key(self):
        """A schema that does not know the key lets `egres:` through to a render that
        silently drops it."""
        import json
        schema = json.loads((CHART / "values.schema.json").read_text())
        comp = schema["properties"]["components"]["items"]["properties"]
        self.assertIn("egress", comp,
                      "the chart's values schema does not declare `egress`; it knows "
                      + ", ".join(sorted(comp)))
        self.assertEqual(comp["egress"].get("additionalProperties"), False,
                         "the egress block accepts unknown keys, so `cidr:` for "
                         "`cidrs:` renders as no egress at all")


if __name__ == "__main__":
    unittest.main(verbosity=2)
