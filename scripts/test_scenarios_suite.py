"""The suite runner runs every scenario there is.

`scenarios/run-all.sh` runs the directories its ORDER names. S17b, S19 and S20 were added
(docs/V1.md M2c) without being added to that list, so "the whole suite" would have passed
without them. A scenario directory that is not in ORDER, or a name in ORDER that is not a
scenario, fails here."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SCENARIOS = Path(__file__).resolve().parent.parent / "scenarios"

pytestmark = pytest.mark.skipif(not SCENARIOS.is_dir(), reason="no scenarios directory beside these scripts")


def order() -> list[str]:
    text = (SCENARIOS / "run-all.sh").read_text(encoding="utf-8")
    listed = re.search(r"^ORDER=\((.*?)\)", text, re.S | re.M)
    assert listed, "run-all.sh has no ORDER=( ... ) list"
    return listed.group(1).split()


def scenario_dirs() -> list[str]:
    return sorted(p.name for p in SCENARIOS.iterdir() if p.is_dir() and (p / "check.sh").is_file())


def test_every_scenario_is_in_the_suite_once():
    names = order()
    assert sorted(names) == scenario_dirs()
    assert len(names) == len(set(names))


def test_the_reboot_runs_last_and_a_removal_that_must_refuse_follows_one_that_works():
    names = order()
    assert names[-1] == "S16-resilience"
    assert names.index("S17b-remove-unprovable-backup") == names.index("S17-remove-project") + 1
