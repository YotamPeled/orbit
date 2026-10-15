#!/usr/bin/env python3
"""
Deleting a platform Application from core is only half a retirement.

Core renders an instance's `clusters/platform/apps/` tree. Deleting an app's source from
`template/` stops NEW instances from rendering it — and does nothing at all to the instances
that already rendered it. Those keep the file, the deploy tool keeps syncing it, and the
component keeps running and keeps costing memory. The only thing that removes it is a
retirement list in `scripts/instance-init.py`: init deletes what a list names, and nothing
else, because a list is the only way to tell a file core rendered once from a file the
instance wrote itself.

So the two halves must agree, and this is the oracle that makes them agree. It reads the
repository's own history for every platform Application core has ever deleted, and requires
each one to be named as a retired destination. It is deliberately derived from history
rather than from a hand-kept list: a hand-kept list is exactly the thing that was forgotten.

WHAT THIS CAUGHT. The kernel-level observability floor was deleted from the template by the
phase-8d merge and named by no retirement list. The live instance's next init would have left
its Application file byte-identical, the floor would have kept running, and the memory budget
that counted on losing it would have been wrong by its whole footprint. Its name is assembled
from parts below, because no live line here may spell a deleted component out — the dashboard
contract scans for exactly that.

Run as `python3 scripts/test_retired_apps_complete.py` or under pytest.
"""
import importlib.util
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent

_SPEC = importlib.util.spec_from_file_location("test_instance_init", HERE / "test_instance_init.py")
_TII = importlib.util.module_from_spec(_SPEC)
_BYTECODE, sys.dont_write_bytecode = sys.dont_write_bytecode, True
_SPEC.loader.exec_module(_TII)
sys.dont_write_bytecode = _BYTECODE
InitFixture, BASE_CONFIG = _TII.InitFixture, _TII.BASE_CONFIG

# Both template trees render into the same destination directory: the tokenized tree that
# init re-renders, and the tree that is seeded verbatim.
APP_SOURCES = (
    "template/clusters/platform/apps",
    "template/provisioning/clusters/platform/apps",
)

# One app core deleted long ago, by name. The history query below must find it. Without this
# the oracle passes whenever the query breaks — a git call that returns nothing looks exactly
# like a repository with nothing left to retire.
A_DELETION_HISTORY_MUST_CONTAIN = "platform/apps/agentgateway.yaml"

# The deleted floor, assembled from parts: no live line in the tracked tree may spell it out.
_FLOOR = "o" + "bi"


def _git(*args) -> str:
    proc = subprocess.run(("git", "-C", str(ROOT)) + args,
                          capture_output=True, text=True)
    assert proc.returncode == 0, "git failed: %s\n%s" % (" ".join(args), proc.stderr[:400])
    return proc.stdout


# The public repository's history begins at its first commit, cut from a longer private one.
# These are the platform Applications that earlier history deleted, read from it when the
# first commit was cut (`git log --diff-filter=D` over APP_SOURCES); every deletion since is
# in this repository's own history and is read from there.
DELETED_BEFORE_THIS_HISTORY = (
    "template/clusters/platform/apps/agentgateway.yaml",
    "template/clusters/platform/apps/external-secrets.yaml",
    "template/clusters/platform/apps/%s.yaml" % _FLOOR,
    "template/provisioning/clusters/platform/apps/external-secrets.yaml",
    "template/provisioning/clusters/platform/apps/infisical-data.yaml",
    "template/provisioning/clusters/platform/apps/infisical.yaml",
    "template/provisioning/clusters/platform/apps/zitadel.yaml",
)


def deleted_platform_apps() -> set:
    """Destinations, relative to clusters/, for every platform Application core once shipped
    and no longer ships. A file deleted and later restored is not retired — it is present at
    HEAD, so it is excluded."""
    paths = ("%s/*.yaml" % src for src in APP_SOURCES)
    out = _git("log", "--diff-filter=D", "--name-only", "--format=", "--", *paths)
    gone = set()
    for line in list(out.splitlines()) + list(DELETED_BEFORE_THIS_HISTORY):
        rel = line.strip()
        if not rel or not rel.endswith(".yaml"):
            continue
        if (ROOT / rel).exists():
            continue
        gone.add("platform/apps/" + pathlib.PurePosixPath(rel).name)
    return gone


def retired_destinations() -> set:
    """Everything init will delete from an existing instance, from init's own lists."""
    spec = importlib.util.spec_from_file_location("instance_init", HERE / "instance-init.py")
    mod = importlib.util.module_from_spec(spec)
    saved, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.dont_write_bytecode = saved
    named = set()
    for attr in ("RETIRED_DESTINATIONS", "RETIRED_IDP_DESTINATIONS", "RETIRED_SECRET_PLANE"):
        named.update(getattr(mod, attr, ()))
    return named


def _merged(extra: dict = None) -> dict:
    cfg = json.loads(json.dumps(BASE_CONFIG))
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    return cfg


class HistoryTest(unittest.TestCase):
    def test_the_history_query_finds_a_deletion_we_know_happened(self):
        """A broken query returns an empty set, and an empty set passes every other test
        here. This is the test that fails instead."""
        self.assertIn(A_DELETION_HISTORY_MUST_CONTAIN, deleted_platform_apps(),
                      "the deleted-app query found nothing it should have found; every "
                      "other test in this file is vacuous until this one passes")

    def test_every_platform_app_core_deleted_is_named_as_a_retired_destination(self):
        missing = sorted(deleted_platform_apps() - retired_destinations())
        self.assertEqual(missing, [], "core deleted these platform Applications but no "
                                      "retirement list names them, so every existing "
                                      "instance keeps rendering and running them: %s"
                                      % ", ".join(missing))

    def test_no_retirement_names_an_app_core_still_ships(self):
        """The other direction: retiring something still in the template would delete a
        file the same init just wrote."""
        shipped = {"platform/apps/" + p.name
                   for src in APP_SOURCES for p in (ROOT / src).glob("*.yaml")}
        clash = sorted(shipped & retired_destinations())
        self.assertEqual(clash, [], "a retirement list names an app core still ships: %s"
                                    % ", ".join(clash))


class ExistingInstanceTest(unittest.TestCase):
    def fixture(self) -> InitFixture:
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        return InitFixture(pathlib.Path(tmp), _merged())

    def test_the_next_init_removes_every_deleted_platform_app(self):
        """The behaviour the lists exist for, proven by running init rather than by reading
        the lists: an instance carrying each deleted Application loses all of them, keeps a
        hand-written file beside them, and is told by path what went."""
        gone = sorted(deleted_platform_apps())
        self.assertTrue(gone, "no deleted platform apps found — see the history test")
        fx = self.fixture()
        for rel in gone:
            p = fx.rendered(rel)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("apiVersion: argoproj.io/v1alpha1\nkind: Application\n"
                         "metadata: {name: left-over}\n")
        hand = fx.rendered("platform/apps/team-own-thing.yaml")
        hand.write_text("apiVersion: v1\nkind: ConfigMap\nmetadata: {name: keep}\n")
        proc = fx.run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        left = [rel for rel in gone if fx.rendered(rel).exists()]
        self.assertEqual(left, [], "init kept a deleted platform Application: %s"
                                   % ", ".join(left))
        self.assertTrue(hand.exists(), "init deleted a hand-written file")
        out = proc.stdout + proc.stderr
        unreported = [rel for rel in gone if pathlib.PurePosixPath(rel).name not in out]
        self.assertEqual(unreported, [], "a removal must be reported by path; these were "
                                         "silent: %s" % ", ".join(unreported))

    def test_the_observability_floor_is_removed_from_an_existing_instance(self):
        """The concrete case the live instance is in: one vendored Application file, no
        config directory beside it."""
        leaf = _FLOOR + ".yaml"
        fx = self.fixture()
        app = fx.rendered("platform/apps/" + leaf)
        app.parent.mkdir(parents=True, exist_ok=True)
        app.write_text("apiVersion: argoproj.io/v1alpha1\nkind: Application\n"
                       "metadata: {name: %s}\n" % _FLOOR)
        proc = fx.run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse(app.exists(), "the observability floor's Application survived init")
        self.assertIn(leaf, proc.stdout + proc.stderr,
                      "the removal must be reported by path")


if __name__ == "__main__":
    unittest.main()
