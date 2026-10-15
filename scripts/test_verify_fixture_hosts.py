#!/usr/bin/env python3
"""The catalog fixtures `make verify` generates from name hostnames the generator accepts.

stage3 writes small catalogs into a scaffold of the fixture organization (primary domain
example.org) and runs the generator over them, live, on the instance's desk. When the
generator began refusing a host outside the instance's domains under `hostnames` (QA of
v1.0.0, F23), one of those fixtures still named `dup.example.com` there: the stage's
"repeated hostnames deduplicated" row turned red on the test box, and no offline test
had run the fixture. This reads every `hostnames: [...]` a verify stage writes into a
catalog and holds each host to the fixture organization's domain, so the next rule the
generator learns about hostnames is met here first.
"""
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
STAGES = sorted((ROOT / "scripts" / "verify").glob("stage*.sh"))
# a fixture host that is MEANT to be refused: not a DNS name
MEANT_TO_FAIL = {"bad_host.example.org"}
FIXTURE_DOMAIN = "example.org"


class VerifyFixtureHosts(unittest.TestCase):
    def test_every_hostname_a_stage_writes_into_a_catalog_is_under_the_fixture_domain(self):
        found, outside = 0, []
        for stage in STAGES:
            for number, line in enumerate(stage.read_text(encoding="utf-8").splitlines(), 1):
                if "projects:" not in line and "{id:" not in line:
                    continue                      # chart values fixtures are not catalogs
                for hosts in re.findall(r"hostnames: \[([^\]]*)\]", line):
                    for host in [h.strip().strip('"\'') for h in hosts.split(",") if h.strip()]:
                        found += 1
                        normal = host.lower().rstrip(".")
                        if normal in MEANT_TO_FAIL:
                            continue
                        if not (normal == FIXTURE_DOMAIN or normal.endswith("." + FIXTURE_DOMAIN)):
                            outside.append(f"{stage.name}:{number}: {host}")
        self.assertGreater(found, 3, "no catalog fixture was found: the stages changed shape, and so must this test")
        self.assertEqual(outside, [], "a verify fixture names a host the generator refuses under `hostnames` "
                                      "(a domain of the project's own is a customDomains entry)")


if __name__ == "__main__":
    unittest.main()
