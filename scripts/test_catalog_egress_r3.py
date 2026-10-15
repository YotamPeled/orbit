#!/usr/bin/env python3
"""
Per-component egress, round 3 — breadth measured, and the platform's own plumbing kept.

The allowlist inversion held: a review compared the generator against a real API server
over 131 spellings and found nothing the generator accepts that the API server refuses.
This round is about two things the inversion did not cover.

BREADTH IS NOT A PREFIX LENGTH. The generator refuses prefix 0 and 1, which are the two
spellings of "everything" in IPv4 and mean nothing at all in IPv6. The whole usable IPv6
internet is 2000::/3, which the generator accepts — so a component the catalog calls
narrowed can be granted every globally routable IPv6 address. Half of IPv4 can be reached
the same way by writing two quarters. The rule has to be a bound on HOW MUCH ADDRESS SPACE
a single target grants, stated per family with a reason, not a special case for the two
spellings someone happened to think of.

The bound this contract pins, and why: no target may be wider than a /8 in IPv4 or a /32
in IPv6. Those are the largest blocks any single provider is allocated — a regional
registry hands out IPv6 at /32, and no IPv4 holder has more than a /8 — so a legitimate
"the model provider's network" declaration fits underneath, and "the internet" does not.

NARROWING MUST NOT CUT THE PLATFORM'S OWN PLUMBING. Before narrowing, a component's egress
grant covered the cluster's own pod network, so it reached the resolver, its sibling
components and the trace collector without anyone declaring them. After narrowing it gets
only what it named. So a component that declares one external range silently loses name
resolution — and cannot resolve the hostname its external range exists to reach — loses
its siblings, and, if it has a language set, loses the traces the platform injected into
it. The generator already refuses the same shape one case over: a component naming the
database alias with no database is refused because it "would reach nothing at all, not
even DNS". The same reasoning applies here and this round settles it the other way round:
the plumbing is not something an operator must remember to declare. Narrowing restricts
what a component reaches OUTSIDE the cluster; the platform's own paths stay.

MY OWN CONTRACT, TWICE MORE. Round 2's round-trip test asserts nothing on the path its
values actually take — its body runs only when the generator ACCEPTS, and all three of its
values are refused, so it cannot fail for the property its name states. And round 2's
TOO_MUCH list is the same magic-number mistake as the generator's: it enumerates the two
prefix lengths rather than measuring breadth. Both are corrected here.

Run as `python3 scripts/test_catalog_egress_r3.py`. No cluster, no credentials, no network.
"""
import pathlib
import sys
import unittest

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
_BYTECODE, sys.dont_write_bytecode = sys.dont_write_bytecode, True
from test_catalog_egress import Fixture, catalog_with, pod  # noqa: E402
from test_catalog_quota import ROOT  # noqa: E402
sys.dont_write_bytecode = _BYTECODE

EGRESS_LABEL = "orbit.local/egress"

# The trace collector the platform injects into any component with a language set.
TRACE_COLLECTOR_NAMESPACE = "tempo"
TRACE_COLLECTOR_PORT = 4318

# Blocks wider than any single provider holds. Each is a valid, canonical network that the
# API server accepts, so only the generator can refuse it.
TOO_BROAD = [
    "0.0.0.0/0",        # the whole IPv4 internet
    "::/0",             # the whole IPv6 internet
    "0.0.0.0/1",        # half of IPv4
    "128.0.0.0/1",      # the other half
    "0.0.0.0/2",        # a quarter, which the prefix-length rule let through
    "64.0.0.0/2",
    "192.0.0.0/2",
    "2000::/3",         # EVERY globally routable IPv6 address — the review's finding
    "2000::/4",
    "::/3",
    "1.0.0.0/7",        # one step wider than the IPv4 bound
    "2001:db8::/31",    # one step wider than the IPv6 bound
]

# Blocks a real declaration uses. Each must be accepted, or the rule has cut off the use
# the feature exists for.
LEGITIMATE = [
    "13.0.0.0/8",          # the widest IPv4 block the bound admits
    "203.0.113.0/24",      # a provider's published range
    "198.51.100.128/25",
    "2001:db8::/32",       # the widest IPv6 block the bound admits
    "2001:db8:1::/48",
    "2001:db8:1:2::/64",   # a single network segment
]


class _EgressCase(unittest.TestCase):
    def render(self, value: str, egress=None):
        fx = Fixture(catalog_with(egress or {"cidrs": ["s3", "dns"]}),
                     {"egressTargets": {"s3": [value]}})
        self.addCleanup(fx.close)
        return fx

    def assert_clean_refusal(self, fx, value):
        out = fx.output()
        self.assertNotEqual(fx.proc.returncode, 0,
                            f"{value!r} was accepted:\n" + out[-1200:])
        self.assertNotIn("Traceback", out,
                         f"{value!r} crashed the generator instead of being refused:\n"
                         + out[-1200:])
        self.assertIn(str(value), out, "the refusal does not name the value")

    def assert_accepted(self, fx, value):
        self.assertEqual(fx.proc.returncode, 0,
                         f"{value!r} is a legitimate declaration and was refused:\n"
                         + fx.output()[-1200:])


class BreadthTest(_EgressCase):
    def test_a_block_wider_than_any_provider_holds_is_refused(self):
        for value in TOO_BROAD:
            with self.subTest(value=value):
                self.assert_clean_refusal(self.render(value), value)

    def test_a_real_providers_range_is_accepted(self):
        for value in LEGITIMATE:
            with self.subTest(value=value):
                self.assert_accepted(self.render(value), value)

    def test_the_refusal_explains_breadth_rather_than_naming_a_prefix_length(self):
        """An operator who reads 'prefix 3 is refused' learns nothing. One who reads how
        much the block would grant can decide what to write instead."""
        fx = self.render("2000::/3")
        out = fx.output().lower()
        self.assertNotEqual(fx.proc.returncode, 0)
        self.assertTrue(any(w in out for w in ("grants", "wider", "narrow", "address")),
                        "the refusal does not say what is wrong with the range, only "
                        "that it is: " + fx.output()[-600:])

    def test_the_bound_is_the_same_idea_in_both_families(self):
        """Stated as a property rather than as two constants: the widest accepted block
        in each family must be refused one step wider. If the two bounds ever drift apart
        in intent, this is where it shows."""
        for widest, wider in (("13.0.0.0/8", "12.0.0.0/7"),
                              ("2001:db8::/32", "2001:db8::/31")):
            with self.subTest(widest=widest):
                self.assert_accepted(self.render(widest), widest)
                self.assert_clean_refusal(self.render(wider), wider)


class PlumbingTest(_EgressCase):
    """What a narrowed component keeps without declaring it."""

    def policy_for(self, fx, comp="web"):
        for policy in fx.policies("widgets-prod"):
            selector = ((policy.get("spec") or {}).get("podSelector") or {})
            if EGRESS_LABEL in (selector.get("matchLabels") or {}):
                return policy
        self.fail("no narrowing policy was rendered:\n" + fx.output()[-1200:])

    def egress_rules(self, fx):
        return (self.policy_for(fx).get("spec") or {}).get("egress") or []

    def test_a_narrowed_component_still_resolves_names(self):
        """The sharpest case: a component narrowed to one external range cannot resolve
        the hostname that range exists to reach. Declaring an address range and losing
        DNS is not a configuration an operator would choose on purpose."""
        fx = self.render("203.0.113.0/24", egress={"cidrs": ["s3"]})
        self.assert_accepted(fx, "203.0.113.0/24")
        rules = self.egress_rules(fx)
        resolves = any(
            any(str(p.get("port")) == "53" or str(p.get("port")) == "domain"
                for p in (rule.get("ports") or []))
            for rule in rules)
        self.assertTrue(resolves,
                        "a component narrowed without naming the resolver alias got no "
                        "name resolution at all; the rendered rules were: %r" % rules)

    def test_a_narrowed_component_still_reaches_its_siblings(self):
        """The first customer's shape: a web component calling a worker in the same
        namespace. There is no alias that expresses 'my own namespace', so if narrowing
        does not keep it, the call cannot be declared at all."""
        fx = self.render("203.0.113.0/24", egress={"cidrs": ["s3"]})
        self.assert_accepted(fx, "203.0.113.0/24")
        rules = self.egress_rules(fx)
        sibling = pod("widgets", "worker")
        reaches = any(
            any("podSelector" in peer or "namespaceSelector" in peer
                for peer in (rule.get("to") or []))
            for rule in rules)
        self.assertTrue(reaches,
                        "a narrowed component can no longer reach a sibling in its own "
                        "namespace (%r); the rendered rules were: %r" % (sibling, rules))

    def test_a_narrowed_component_with_a_language_still_reports_traces(self):
        """The platform points a component's tracing at a collector in another namespace
        without the component asking. Narrowing must not silently switch that off."""
        fx = self.render("203.0.113.0/24", egress={"cidrs": ["s3"]})
        self.assert_accepted(fx, "203.0.113.0/24")
        rules = self.egress_rules(fx)
        collects = any(
            any(str(p.get("port")) == str(TRACE_COLLECTOR_PORT)
                for p in (rule.get("ports") or []))
            for rule in rules)
        self.assertTrue(collects,
                        "narrowing turned off the traces the platform injected; no rule "
                        "reaches the collector on port %d: %r"
                        % (TRACE_COLLECTOR_PORT, rules))

    def test_the_plumbing_does_not_widen_what_was_declared(self):
        """Keeping the platform's paths must not become a second open grant: the external
        half of the policy is still only what the component named."""
        fx = self.render("203.0.113.0/24", egress={"cidrs": ["s3"]})
        self.assert_accepted(fx, "203.0.113.0/24")
        blocks = sorted({peer["ipBlock"]["cidr"]
                         for rule in self.egress_rules(fx)
                         for peer in (rule.get("to") or []) if "ipBlock" in peer})
        self.assertEqual(blocks, ["203.0.113.0/24"],
                         "narrowing granted address ranges nobody declared — the "
                         "platform's own paths belong in selectors, not in a second "
                         "open block: %r" % blocks)


class WholeTableTest(_EgressCase):
    def test_a_target_under_an_unreferenced_alias_is_still_validated(self):
        """A bad range under an alias no component names sits green until the day someone
        uses it, and the refusal then lands on whoever declared the alias rather than on
        whoever wrote the bad value."""
        fx = Fixture(catalog_with({"cidrs": ["dns"]}),
                     {"egressTargets": {"dns": ["203.0.113.0/24"],
                                        "unused": ["198.51.100.5"]}})
        self.addCleanup(fx.close)
        self.assert_clean_refusal(fx, "198.51.100.5")


class MessageTest(_EgressCase):
    def test_a_native_ipv6_supernet_is_not_called_an_ipv4_mapped_range(self):
        """::/8 is a native IPv6 range with native endpoints. Refusing it is right;
        telling the operator it is a mapped range and to 'write the IPv4 range it maps
        instead' is false on both halves, and there is no such range to write."""
        fx = self.render("::/8")
        self.assertNotEqual(fx.proc.returncode, 0)
        out = fx.output().lower()
        self.assertNotIn("ipv4-mapped", out,
                         "a native IPv6 supernet was diagnosed as an IPv4-mapped range: "
                         + fx.output()[-600:])

    def test_a_leading_zero_is_not_said_to_be_read_as_something(self):
        """The cluster does not read 192.168.0.0/016 as anything — it refuses it. A
        message that says otherwise teaches the operator a false rule."""
        fx = self.render("192.168.0.0/016")
        self.assertNotEqual(fx.proc.returncode, 0)
        out = fx.output().lower()
        self.assertNotIn("reads it as", out,
                         "the refusal claims the cluster reads a value it actually "
                         "refuses: " + fx.output()[-600:])


if __name__ == "__main__":
    unittest.main()
