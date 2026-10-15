#!/usr/bin/env python3
"""
Per-component egress, round 2 — a different validator, not another special case. Run as
`python3 scripts/test_catalog_egress_r2.py`. No cluster, no credentials, no network.

WHY THIS IS A REDESIGN AND NOT A THIRD PATCH. Two reviews in a row found the same defect
in a different costume. Round 1: targets were parsed leniently and rendered verbatim, so
`198.51.100.5` and `198.51.100.5/24` passed every local check and were refused by the API
server. Round 2, after strict parsing closed exactly those two: an IPv4-mapped IPv6
network passes `ipaddress.ip_network(..., strict=True)` and the API server refuses every
one of them, and the mapped spelling of the node's metadata range walks through the
link-local guard on the way.

The shapes differ; the mistake is one mistake. The generator was asking "does Python's
address library accept this?" when the thing that decides is the Kubernetes API server,
and those are two different validators that happen to agree on the common cases. Adding a
third refusal for mapped addresses would leave the fourth shape for the next review.

SO THE RULE INVERTS. Stop enumerating what is refused. Define the set that is ACCEPTED —
an IPv4 network, or a native IPv6 network, canonical, with an explicit prefix — and refuse
everything else by not being in it. A shape nobody thought of is then refused by default
rather than admitted by default, which is the only version of this that a review cannot
keep finding holes in.

THE SAME MISTAKE, SOMEWHERE ELSE. The label the two halves of the narrowing agree on is
recovered by a regular expression over the chart's SOURCE TEXT. YAML quoting is invisible
to it: an ordinary `orbit.local/egress: "declared"` — a spelling a formatter introduces
unasked — makes the generator match on a value with the quote characters in it, and the
pod is then selected by no policy at all, unrestricted in both directions. Scraping text
to recover meaning is the same error as validating with the wrong library. The agreement
has to come from something both sides PARSE.

TWO DEFECTS IN MY OWN ROUND 1 CONTRACT, fixed here. The ingress-copy tests were pinned to
a production namespace, whose ingress rule set has exactly one element, so they compared a
one-element list against itself and the non-production rule the spec asked about was never
in the comparison. And the host-bits test asserted only a non-zero exit with the value in
the output, which an unhandled Python traceback satisfies — it could not tell a refusal
from a crash.
"""
import ipaddress
import pathlib
import re
import sys
import unittest

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
_BYTECODE, sys.dont_write_bytecode = sys.dont_write_bytecode, True
from test_catalog_egress import Fixture, catalog_with, pod  # noqa: E402
from test_catalog_egress_r import admits, database_pod  # noqa: E402
from test_catalog_quota import ROOT, Sandbox  # noqa: E402
sys.dont_write_bytecode = _BYTECODE

CHART = ROOT / "charts" / "service"
EGRESS_LABEL = "orbit.local/egress"

# Everything the API server's ipBlock.cidr field refuses, in one place, so a reader can
# see the class rather than a list. Each is a valid network to Python's address library.
REFUSED_BY_THE_APISERVER = [
    "::ffff:198.51.100.0/120",      # IPv4-mapped IPv6, the shape round 2 found
    "::ffff:c633:6400/120",         # the same range, hexadecimal
    "::ffff:169.254.0.0/112",       # the node's metadata range, mapped
    "198.51.100.5",                 # no prefix at all
    "198.51.100.5/24",              # host bits set
]

# Ranges that are a valid CIDR and still a refusal, because of WHAT they reach rather
# than how they are spelled.
TOO_MUCH = [
    "0.0.0.0/0", "::/0",            # the whole internet, both families
    "0.0.0.0/1", "::/1",            # half of it, which is not narrowing either
    "169.254.0.0/16", "fe80::/10",  # the node's metadata service
]


class AcceptedSetTest(unittest.TestCase):
    """The validator is an allowlist of two shapes, so an unimagined shape is refused."""

    def render(self, value: str):
        fx = Fixture(catalog_with({"cidrs": ["s3", "dns"]}),
                     {"egressTargets": {"s3": [value]}})
        self.addCleanup(fx.close)
        return fx

    def assert_clean_refusal(self, fx, value):
        """A refusal, not a crash. A traceback is also non-zero and also prints the
        value, which is how my round 1 contract could not tell them apart."""
        out = fx.output()
        self.assertNotEqual(fx.proc.returncode, 0,
                            f"{value!r} was accepted:\n" + out[-1200:])
        self.assertNotIn("Traceback", out,
                         f"{value!r} produced an unhandled exception rather than a "
                         "refusal; an operator cannot tell a rejected value from a "
                         "broken generator:\n" + out[-1200:])
        self.assertIn(str(value), out, "the refusal does not name the value")

    def test_every_shape_the_api_server_refuses_is_refused_here(self):
        for value in REFUSED_BY_THE_APISERVER:
            with self.subTest(value=value):
                self.assert_clean_refusal(self.render(value), value)

    def test_a_range_that_grants_too_much_is_refused(self):
        for value in TOO_MUCH:
            with self.subTest(value=value):
                self.assert_clean_refusal(self.render(value), value)

    def test_the_two_accepted_shapes_still_render(self):
        for value in ("198.51.100.0/24", "2001:db8::/32"):
            with self.subTest(value=value):
                fx = self.render(value)
                self.assertEqual(fx.proc.returncode, 0, fx.output()[-1200:])

    def test_a_target_that_does_not_survive_a_round_trip_is_refused(self):
        """The general form of the rule: if writing the parsed network back out does not
        give the operator's own string, the two disagree about what was asked for, and
        the generator must not pick a side."""
        for value in ("198.51.100.00/24", "2001:0db8:0000::/32", "::FFFF:0/96"):
            with self.subTest(value=value):
                fx = self.render(value)
                # All three take the refusal path. The old body asserted only on the
                # ACCEPTED path, so it could not fail for the property it names. A
                # refusal renders nothing, so the refusal itself is the assertion.
                self.assert_clean_refusal(fx, value)

    def test_whatever_is_rendered_is_canonical_and_unmapped(self):
        fx = self.render("198.51.100.0/24")
        self.assertEqual(fx.proc.returncode, 0, fx.output()[-1200:])
        for policy in fx.policies("widgets-prod"):
            for rule in fx.egress_rules(policy):
                for peer in rule.get("to") or []:
                    cidr = (peer.get("ipBlock") or {}).get("cidr")
                    if not cidr:
                        continue
                    net = ipaddress.ip_network(cidr, strict=True)
                    self.assertEqual(str(net), cidr, f"{cidr} is not canonical")
                    if net.version == 6:
                        self.assertIsNone(net[0].ipv4_mapped,
                                          f"{cidr} is an IPv4-mapped IPv6 range, which "
                                          "the API server refuses outright")


class LabelAgreementTest(unittest.TestCase):
    """The two halves agree through something both of them parse."""

    def stamped_value(self):
        """What the chart declares, read the way a YAML reader reads it — not scraped
        out of a template's source with a regular expression."""
        values = yaml.safe_load((CHART / "values.yaml").read_text()) or {}
        return values.get("egressLabelValue")

    def test_the_chart_declares_the_value_somewhere_yaml_can_read_it(self):
        self.assertIsNotNone(
            self.stamped_value(),
            "the chart does not declare the egress label's value in values.yaml; while "
            "it lives only inside a template, the generator can only recover it by "
            "scraping text, and an ordinary respelling silently desynchronises the two "
            "halves of the narrowing")

    def test_the_template_uses_that_declaration_rather_than_a_literal(self):
        text = (CHART / "templates" / "workloads.yaml").read_text()
        literal = re.findall(rf"^\s*{re.escape(EGRESS_LABEL)}:\s*(\S+)\s*$", text, re.M)
        for value in literal:
            self.assertIn("Values", value,
                          f"the template stamps the literal {value!r}; it must render "
                          "the declared value so the two cannot drift")

    def test_the_policy_matches_exactly_what_the_chart_declares(self):
        fx = Fixture(catalog_with({"cidrs": ["dns"]}))
        self.addCleanup(fx.close)
        self.assertEqual(fx.proc.returncode, 0, fx.output()[-1200:])
        matched = {labels[EGRESS_LABEL]
                   for policy in fx.policies("widgets-prod")
                   for labels in [((policy.get("spec") or {}).get("podSelector") or {}
                                   ).get("matchLabels") or {}]
                   if EGRESS_LABEL in labels}
        self.assertEqual(matched, {self.stamped_value()},
                         f"the policy matches {sorted(matched)} and the chart declares "
                         f"{self.stamped_value()!r}")

    def test_a_quoted_declaration_does_not_reach_the_policy_quoted(self):
        """The concrete regression: YAML quoting is invisible to a text scrape, and a
        label value carrying quote characters is refused by the API server."""
        fx = Fixture(catalog_with({"cidrs": ["dns"]}))
        self.addCleanup(fx.close)
        for policy in fx.policies("widgets-prod"):
            labels = ((policy.get("spec") or {}).get("podSelector") or {}
                      ).get("matchLabels") or {}
            value = labels.get(EGRESS_LABEL)
            if value is None:
                continue
            self.assertRegex(value, r"^[A-Za-z0-9]([A-Za-z0-9_.-]{0,61}[A-Za-z0-9])?$",
                             f"the policy matches {value!r}, which is not a label value "
                             "the API server accepts")


class IngressCopyTest(unittest.TestCase):
    """My round 1 contract compared production against production. The rule set that
    differs is the non-production one, and it was never in the comparison."""

    def build(self, env: str):
        fx = Fixture(catalog_with({"cidrs": ["dns"]}))
        self.addCleanup(fx.close)
        self.assertEqual(fx.proc.returncode, 0, fx.output()[-1200:])
        fx.policies_all = lambda e=env: fx.policies(f"widgets-{e}")
        return fx

    def test_the_non_production_admit_list_is_copied_whole(self):
        """A non-production namespace admits one thing production does not: the control
        plane, to the chart's web ports. A copy that drops it leaves the narrowed
        component unreachable by the check that says a ship worked."""
        fx = self.build("dev")
        narrowed = admits(fx, pod("widgets", "web", narrowed=True))
        plain = admits(fx, pod("widgets", "importer"))
        self.assertNotEqual(narrowed, ["NOT ISOLATED"],
                            "the narrowed component is not ingress-isolated in dev")
        self.assertEqual(narrowed, plain,
                         "the narrowed component's dev admit list differs from its "
                         "neighbour's; the extra non-production rule was dropped by the "
                         "copy")

    def test_the_non_production_rule_is_actually_in_the_comparison(self):
        """The guard on the test above. If dev and prod have identical rule sets the
        fixture has stopped exercising the case, and the test passes for the wrong
        reason — which is exactly what happened the first time."""
        dev = admits(self.build("dev"), pod("widgets", "importer"))
        prod = admits(self.build("prod"), pod("widgets", "importer"))
        self.assertNotEqual(dev, prod,
                            "dev and prod admit the same set, so nothing here tests the "
                            "rule that only non-production has")

    def test_production_is_still_isolated_and_identical(self):
        fx = self.build("prod")
        self.assertEqual(admits(fx, pod("widgets", "web", narrowed=True)),
                         admits(fx, pod("widgets", "importer")))

    def test_the_database_keeps_the_namespace_default_in_both(self):
        for env in ("dev", "prod"):
            fx = self.build(env)
            self.assertNotEqual(admits(fx, database_pod("widgets")), ["NOT ISOLATED"],
                                f"the database lost ingress isolation in {env}")


class UnchangedTest(unittest.TestCase):
    def test_a_catalog_that_narrows_nothing_renders_byte_identical(self):
        box = Sandbox()
        self.addCleanup(box.close)
        proc = box.generate()
        self.assertEqual(proc.returncode, 0, (proc.stdout + proc.stderr)[-1500:])
        expected = (ROOT / "scripts" / "testdata" / "catalog-quota"
                    / "expected" / "namespaces.yaml").read_text()
        self.assertEqual(box.namespaces_text(), expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
