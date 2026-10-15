"""Who applies the change, when this orbitd does not.

The apply step of a ship can be skipped two ways: the switch is off, or the pair is not
in the list this orbitd applies. On both routes the surface tells the operator that the
sign-in-and-apply plane will apply it instead. That plane was retired: nothing applies it,
the ship still reports `applied`, and an agent following the ship walk is told its change
is live when no object was written.

These cases are driven over the protocol, because what a machine caller is handed is the
thing that is wrong -- the step's own text reads the same either way.
"""

from __future__ import annotations

import ast
import json
import pathlib
import re

import pytest
from conftest import mint, sdk_client

from orbitd.roles import Role

from test_apply_pairs import set_pairs
from orbitd import deploy

from test_reconcile import COMMIT as SHIPPED, ENV, OLD, PROJECT, FakeSource, rendered, set_mode, use_render
from test_tools_operator import TAG, WORKER, install, seed_world, wait_result

pytestmark = pytest.mark.anyio


# The plane phase 5 retired: its namespace was deleted and nothing regenerates it. Its
# Application documents stay in the instance tree as the platform declaration the desk
# reads, and objects it once owned still carry its annotations -- so naming it is not the
# defect. Saying it will act is: there is no controller left to act.
RETIRED = re.compile(r"argo ?cd", re.I)
ACTS = re.compile(
    r"\b(applies|applied|applying|apply|syncs|synced|deploys|deployed|reconciles|owns|owned)\b", re.I)
NEAR = 60

# Every documented outcome of a mutation, from the operator skill. A ship that applied
# nothing is one of these, and it is not the first.
OUTCOMES = ("applied", "refused", "failed", "dry_run", "running")


def _skip_because_the_switch_is_off(state):
    set_mode(state, "off")


def _skip_because_the_pair_is_not_enabled(state):
    set_mode(state, "on")
    set_pairs(state, "demo/dev")


SKIPPED = (
    ("the switch is off", _skip_because_the_switch_is_off),
    ("the pair is not enabled", _skip_because_the_pair_is_not_enabled),
)


async def _ship(state, http):
    async with sdk_client(http, mint(state, Role.operator), confirm=True) as client:
        running = await client.call_tool("orbit_deploy_ship", {
            "project": PROJECT, "env": ENV, "components": [WORKER], "tag": TAG,
        })
        result, _ = await wait_result(client, running)
    return result


def _promises_a_retired_plane_will_act(text: str) -> str | None:
    """The sentence this guards against: the retired plane named as the actor. A mention
    with no verb of acting near it is a file format, a namespace or an annotation key, all
    of which survived the retirement and are named correctly."""
    for hit in RETIRED.finditer(text):
        window = text[max(0, hit.start() - NEAR):hit.end() + NEAR]
        acting = ACTS.search(window)
        if acting:
            return window.strip()
    return None


class TestAShipThatAppliedNothingDoesNotReportApplied:
    """The thing an agent reads first. `applied` is the word the ship walk waits for, and
    on both skip routes it arrives having written nothing at all."""

    @pytest.mark.parametrize("why,arrange", SKIPPED)
    async def test_nothing_was_written(self, fake_backends, http, why, arrange):
        """Anti-vacuity for the pair below: these routes really do write no object, so a
        ship that reports success on them is reporting something that did not happen."""
        install(fake_backends)
        kube = seed_world(fake_backends)
        arrange(fake_backends)
        await _ship(fake_backends, http)
        assert kube.apply_calls == [], (
            f"{why}: this route was supposed to write nothing and wrote "
            f"{len(kube.apply_calls)} objects, so this file is testing the wrong routes")

    @pytest.mark.parametrize("why,arrange", SKIPPED)
    async def test_the_outcome_does_not_say_applied(self, fake_backends, http, why, arrange):
        install(fake_backends)
        seed_world(fake_backends)
        arrange(fake_backends)
        result = await _ship(fake_backends, http)
        assert result["status"] in OUTCOMES, (
            f"{why}: the ship answered with an outcome the skills do not describe: "
            + repr(result["status"]))
        assert result["status"] != "applied", (
            f"{why}: the ship wrote no object and reported applied, so an agent following "
            "the ship walk is told its change is live when nothing applied it")

    async def test_a_real_apply_still_says_applied(self, fake_backends, http, monkeypatch):
        """Anti-vacuity: a ship that never reported applied would pass every case above
        and destroy the walk it exists to serve."""
        install(fake_backends)
        kube = seed_world(fake_backends)
        set_mode(fake_backends, "on")
        use_render(monkeypatch, rendered())
        monkeypatch.setattr(deploy, "read_source", FakeSource(OLD, SHIPPED, stale=0))
        result = await _ship(fake_backends, http)
        assert result["status"] == "applied", result
        assert kube.apply_calls, "the anti-vacuity route wrote nothing either"


class TestTheOperatorIsToldWhatActuallyHappens:
    """Saying nothing is not the repair. A skipped apply must say that this orbitd did not
    apply it and name what decides that, so the operator can act instead of waiting."""

    @pytest.mark.parametrize("why,arrange", SKIPPED)
    async def test_it_names_no_plane_the_release_does_not_run(
            self, fake_backends, http, why, arrange):
        install(fake_backends)
        seed_world(fake_backends)
        arrange(fake_backends)
        result = await _ship(fake_backends, http)
        document = json.dumps(result)
        said = _promises_a_retired_plane_will_act(document)
        assert said is None, (
            f"{why}: what the operator is handed promises a plane this release retired "
            "will apply the change, and there is no controller left to do it: " + repr(said))

    @pytest.mark.parametrize("why,arrange", SKIPPED)
    async def test_it_names_what_decides_the_skip(self, fake_backends, http, why, arrange):
        install(fake_backends)
        seed_world(fake_backends)
        arrange(fake_backends)
        result = await _ship(fake_backends, http)
        document = json.dumps(result)
        assert "ORBITD_APPLY" in document, (
            f"{why}: the apply was skipped and nothing in the answer says what decides "
            "that, so the operator cannot tell a skip from a success: " + document[:400])


# The class that stood here scanned every string in the package and every line of the
# shipped skills for one proper noun near a verb of acting. A review put the false promise
# back past it twice with the suite green: once wrapped the way markdown wraps, so the noun
# ended one line and the verb began the next, and once crediting the apply to something
# that was never in any noun list because it does not exist. That is the third round of
# this shape on this front, and the rule here forbids repairing it a fourth time.
#
# Its replacement is in test_what_the_ship_really_did.py. It names no plane, so not naming
# one cannot defeat it, and it reads paragraphs rather than lines. It drives every route
# the ship has, keeps the words the ship uses to say how a thing ended, and requires every
# document that tells a reader a change gets applied to anchor that claim in one of them.
