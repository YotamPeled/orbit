#!/usr/bin/env python3
"""
Per-component egress, round 1 repairs — run as
`python3 scripts/test_catalog_egress_r.py`. No cluster, no credentials, no network.

WHY. The node that narrowed a component's outbound reach passed its own contract and was
then reviewed. The first finding is the one that matters, and my contract had no test
that could have caught it because every test I wrote looked at egress.

  * NARROWING A COMPONENT'S EGRESS SILENTLY REMOVED ITS INGRESS ISOLATION. A tenant
    namespace has exactly one NetworkPolicy carrying `Ingress` in its policyTypes, and
    the narrowing works by removing the narrowed pod from that policy's podSelector. Under
    Kubernetes semantics a pod is ingress-isolated only while SOME policy with `Ingress`
    in policyTypes selects it; a pod selected by none accepts connections from everywhere.
    So the component holding the model provider's key — the one component anybody would
    narrow first — stopped being protected from every other pod in the cluster the moment
    it was narrowed. A security feature that opens a hole is worse than no feature.

The rest are ways a green render is refused or useless at apply time:

  * A target is parsed leniently and then rendered VERBATIM, so `198.51.100.5` and
    `198.51.100.5/24` both pass every local check and are both rejected by the API server.
    The generator's job is to fail on the workstation, not in the cluster.
  * The `postgres` alias renders a selector for a database Cluster the project may not
    have, so a component whose only alias is `postgres` in a database-less project leaves
    the open grant and reaches NOTHING — not even DNS. That is precisely the
    deny-everything outcome the alias validation was written to prevent.
  * The whole-internet and link-local refusals compare against IPv4 constants, so the
    IPv6 spellings of both walk through.
  * The label that ties the two halves together is written as a literal in two files with
    no test comparing them. If they ever drift, the pod carries the KEY (so the open grant
    excludes it) and fails the per-component policy's VALUE match — selected by no policy
    at all, which is unrestricted in both directions.
  * An instance that declares an address range under a name the core builds in is ignored
    without a word.
"""
import ipaddress
import pathlib
import re
import sys
import unittest

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
_BYTECODE, sys.dont_write_bytecode = sys.dont_write_bytecode, True
from test_catalog_egress import (EGRESS_LABEL, Fixture, catalog_with,  # noqa: E402
                                 pod)
from test_catalog_quota import ROOT, Sandbox  # noqa: E402
sys.dont_write_bytecode = _BYTECODE

CHART = ROOT / "charts" / "service"
WORKLOADS = CHART / "templates" / "workloads.yaml"

# The database Cluster the chart creates for a project, by the label its pods carry.
CNPG_LABEL = "cnpg.io/cluster"


def database_pod(project: str) -> dict:
    """A CloudNativePG instance pod of this project's database, as the operator labels
    it. The Cluster is named <project>-db (charts/service/templates/database.yaml)."""
    return {CNPG_LABEL: f"{project}-db", "cnpg.io/podRole": "instance"}


def admits(fx, labels: dict) -> list:
    """Every ingress `from` peer that reaches a pod carrying `labels`, flattened, with
    the policies that grant it. A pod selected by NO ingress-carrying policy is not
    isolated at all, which this returns as the sentinel below."""
    isolated = False
    peers = []
    for policy in fx.policies_all():
        if "Ingress" not in ((policy.get("spec") or {}).get("policyTypes") or []):
            continue
        if not fx.selects(policy, labels):
            continue
        isolated = True
        for rule in (policy["spec"].get("ingress") or []):
            for peer in rule.get("from") or []:
                peers.append(yaml.safe_dump(peer, sort_keys=True).strip())
    return sorted(peers) if isolated else ["NOT ISOLATED"]


class _Ingress(unittest.TestCase):
    """The fixture: one narrowed component beside one that declares nothing."""

    def setUp(self):
        self.fx = Fixture(catalog_with({"cidrs": ["dns"]}))
        self.addCleanup(self.fx.close)
        self.assertEqual(self.fx.proc.returncode, 0, self.fx.output()[-2000:])
        self.fx.policies_all = lambda: self.fx.policies("widgets-prod")


class IngressIsolationTest(_Ingress):
    """THE finding. Narrowing outbound reach must not widen inbound reach."""

    def test_a_narrowed_component_is_still_ingress_isolated(self):
        narrowed = pod("widgets", "web", narrowed=True)
        self.assertNotEqual(
            admits(self.fx, narrowed), ["NOT ISOLATED"],
            "no policy carrying Ingress selects the narrowed component, so it accepts "
            "connections from every pod in the cluster — narrowing its egress removed "
            "its inbound protection")

    def test_it_admits_exactly_what_an_unnarrowed_component_admits(self):
        narrowed = admits(self.fx, pod("widgets", "web", narrowed=True))
        plain = admits(self.fx, pod("widgets", "importer"))
        self.assertEqual(narrowed, plain,
                         "the narrowed component's admit list differs from its "
                         "neighbour's; declaring an outbound allowlist is not a request "
                         "to change who may call in")

    def test_the_database_is_still_ingress_isolated(self):
        self.assertNotEqual(admits(self.fx, database_pod("widgets")), ["NOT ISOLATED"],
                            "the database pods lost their ingress isolation")

    def test_a_namespace_with_nothing_narrowed_is_unchanged(self):
        box = Sandbox()
        self.addCleanup(box.close)
        proc = box.generate()
        self.assertEqual(proc.returncode, 0, (proc.stdout + proc.stderr)[-2000:])
        expected = (ROOT / "scripts" / "testdata" / "catalog-quota"
                    / "expected" / "namespaces.yaml").read_text()
        self.assertEqual(box.namespaces_text(), expected,
                         "repairing the narrowed case changed the render of a catalog "
                         "that narrows nothing")


class RenderedRangeTest(unittest.TestCase):
    """What reaches `ipBlock.cidr` is what the API server accepts, or the generator
    refuses it on the workstation."""

    def render(self, value: str):
        fx = Fixture(catalog_with({"cidrs": ["s3", "dns"]}),
                     {"egressTargets": {"s3": [value]}})
        self.addCleanup(fx.close)
        return fx

    def cidrs(self, fx) -> list:
        return [(peer.get("ipBlock") or {}).get("cidr")
                for policy in fx.policies("widgets-prod")
                for rule in fx.egress_rules(policy)
                for peer in rule.get("to") or []
                if (peer.get("ipBlock") or {}).get("cidr")]

    def test_a_bare_address_does_not_reach_the_policy_unchanged(self):
        """`198.51.100.5` reads as a /32 to a lenient parser and is refused outright by
        the API server's cidr field."""
        fx = self.render("198.51.100.5")
        if fx.proc.returncode == 0:
            self.assertNotIn("198.51.100.5", self.cidrs(fx),
                             "a bare address was rendered as a policy cidr; the API "
                             "server rejects it and every local check was green")
        else:
            self.assertIn("198.51.100.5", fx.output(),
                          "the refusal does not name the value it refused")

    def test_an_address_with_host_bits_set_is_refused(self):
        fx = self.render("198.51.100.5/24")
        self.assertNotEqual(fx.proc.returncode, 0,
                            "198.51.100.5/24 was accepted; a lenient parser normalises "
                            "the host bits away and the API server rejects the original "
                            "string:\n" + fx.output()[-1500:])
        self.assertIn("198.51.100.5/24", fx.output(),
                      "the refusal does not name the value it refused")

    def test_every_rendered_range_is_a_strict_network(self):
        fx = self.render("198.51.100.0/24")
        self.assertEqual(fx.proc.returncode, 0, fx.output()[-1500:])
        for cidr in self.cidrs(fx):
            try:
                ipaddress.ip_network(cidr, strict=True)
            except ValueError as e:
                self.fail(f"the policy carries {cidr!r}, which the API server refuses: {e}")


class BothFamiliesTest(unittest.TestCase):
    """A refusal that only knows one address family is not a refusal."""

    def refuse(self, value: str):
        fx = Fixture(catalog_with({"cidrs": ["s3"]}),
                     {"egressTargets": {"s3": [value]}})
        self.addCleanup(fx.close)
        self.assertNotEqual(fx.proc.returncode, 0,
                            f"{value} was accepted as an egress target:\n"
                            + fx.output()[-1500:])
        self.assertIn(value, fx.output(), "the refusal does not name the range")

    def test_the_whole_internet_is_refused_in_either_family(self):
        for value in ("0.0.0.0/0", "::/0"):
            self.refuse(value)

    def test_link_local_is_refused_in_either_family(self):
        """The node's metadata service is the IPv4 one, but this node carries a public
        IPv6 address of its own and nothing else stands between a pod and it."""
        for value in ("169.254.0.0/16", "fe80::/10"):
            self.refuse(value)


class DatabaseAliasTest(unittest.TestCase):
    """The postgres alias must name a database that exists."""

    def no_database(self, cidrs):
        box = Sandbox()
        self.addCleanup(box.close)
        cat = box.root / "catalog" / "projects.yaml"
        doc = yaml.safe_load(cat.read_text())
        doc["projects"] = [{
            "id": "nodb", "name": "No DB", "hosted": "self",
            "environments": {"prod": {"hostnames": ["nodb.example.org"]}},
            "components": [{"name": "web", "kind": "web", "image": "example/x",
                            "port": 8080, "path": "/", "egress": {"cidrs": cidrs}}],
        }]
        cat.write_text(yaml.safe_dump(doc, sort_keys=False))
        proc = box.generate()
        return box, proc

    def test_the_postgres_alias_is_refused_when_the_project_has_no_database(self):
        _, proc = self.no_database(["postgres", "dns"])
        out = (proc.stdout or "") + (proc.stderr or "")
        self.assertNotEqual(proc.returncode, 0,
                            "a component asked for the database of a project that has "
                            "none; it leaves the open grant and reaches nothing at "
                            "all, not even DNS:\n" + out[-1500:])
        self.assertIn("postgres", out, "the refusal does not name the alias")

    def test_a_database_less_project_can_still_narrow(self):
        box, proc = self.no_database(["dns"])
        self.assertEqual(proc.returncode, 0,
                         "narrowing became impossible for a project with no database:\n"
                         + ((proc.stdout or "") + (proc.stderr or ""))[-1500:])

    def test_the_postgres_selector_names_the_cluster_the_chart_creates(self):
        fx = Fixture(catalog_with({"cidrs": ["postgres", "dns"]}))
        self.addCleanup(fx.close)
        self.assertEqual(fx.proc.returncode, 0, fx.output()[-1500:])
        wanted = f"widgets-db"
        found = set()
        for policy in fx.policies("widgets-prod"):
            if not fx.selects(policy, pod("widgets", "web", narrowed=True)):
                continue
            for rule in fx.egress_rules(policy):
                for peer in rule.get("to") or []:
                    labels = (peer.get("podSelector") or {}).get("matchLabels") or {}
                    if CNPG_LABEL in labels:
                        found.add(labels[CNPG_LABEL])
        self.assertEqual(found, {wanted},
                         f"the postgres alias selects {sorted(found)}; the chart names "
                         f"the Cluster {wanted!r}, so the grant reaches no pod")


# LabelAgreementTest lived here. It compared the policy's matched value against a value
# scraped out of the chart template's source with a regular expression — which only works
# while the template stamps a literal. The redesign (test_catalog_egress_r2.py) makes the
# template render a declared value instead, precisely so the two halves cannot drift, and
# carries a LabelAgreementTest that reads that declaration the way a YAML reader reads it.
# Keeping this one would pin the shape the redesign exists to remove.


class AliasShadowTest(unittest.TestCase):
    """What an operator writes down is honoured or refused, never ignored."""

    def test_an_instance_target_named_after_a_builtin_alias_is_refused(self):
        for builtin in ("dns", "postgres"):
            fx = Fixture(catalog_with({"cidrs": [builtin]}),
                         {"egressTargets": {builtin: ["198.51.100.0/24"]}})
            self.addCleanup(fx.close)
            self.assertNotEqual(fx.proc.returncode, 0,
                                f"an instance declared an address range under {builtin!r} "
                                "and it was silently ignored in favour of the builtin:\n"
                                + fx.output()[-1500:])
            self.assertIn(builtin, fx.output(), "the refusal does not name the alias")


if __name__ == "__main__":
    unittest.main(verbosity=2)
