"""scripts/pitr-restore.sh: the load keeps owners and grants, and the restore is only
reported when every relation belongs to the database's owner (docs/V1.md §8: S07's
restore loaded the right rows owned by postgres, and the application's own role lost
access to its tables until they were fixed by hand)."""
from __future__ import annotations

import pathlib
import re
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent / "pitr-restore.sh"


class PitrRestoreOwnershipTest(unittest.TestCase):
    def setUp(self):
        self.text = SCRIPT.read_text(encoding="utf-8")
        start = self.text.index('echo "== loading the restored data into the live primary')
        self.load = self.text[start:]

    def test_the_load_does_not_strip_owners_or_grants(self):
        pipeline = re.search(r"pg_dump -d \"\$DBNAME\"[^\n]*", self.load).group(0)
        self.assertNotIn("--no-owner", pipeline)
        self.assertNotIn("--no-privileges", pipeline)
        self.assertIn("--clean --if-exists", pipeline)

    def test_ownership_is_verified_before_restored_is_reported(self):
        verify = self.load.index("pg_get_userbyid(c.relowner) <> :'owner'")
        refused = self.load.index("LOADED, BUT NOT OWNED BY")
        restored = self.load.index('echo "RESTORED:')
        self.assertLess(verify, refused)
        self.assertLess(refused, restored)
        self.assertIn(".spec.bootstrap.initdb.owner", self.load)
        # the refusal exits non-zero: no RESTORED line, no success for a caller
        block = self.load[refused:restored]
        self.assertIn("exit 1", block)


    def test_a_failed_runs_scratch_namespace_is_taken_away_by_the_next_run(self):
        """One failed comparison blocked every later one with "inspect it, then: kubectl
        delete ns", addressed to an agent that has no kubectl (docs/V1.md §8). A failed run
        marks its namespace; the next run removes a marked or half-hour-old one and stops
        only for one that is neither."""
        text = SCRIPT.read_text()
        self.assertIn('kubectl annotate ns "$NS" orbit.local/pitr-run=failed --overwrite', text)
        # ...and since v0.34.27 the failed run itself says what its scratch copy looked like
        # and takes it away: kept, it was a failed Job and a database with no healthy
        # instance, three alerts and a red verify on a platform with nothing wrong
        failed_branch = text[text.index("restore did not finish; what the scratch copy"):text.index("trap finish EXIT")]
        self.assertIn('get clusters.postgresql.cnpg.io', failed_branch)
        self.assertIn('kubectl delete ns "$NS" --wait=false', failed_branch)
        self.assertNotIn("kept for inspection", text)
        # the drill, the same rule
        drill = (SCRIPT.parent / "pitr-drill.sh").read_text()
        self.assertNotIn("kept for inspection", drill)
        self.assertNotIn("clean up with: kubectl delete ns", drill)
        self.assertIn('if [ "$AGE" -ge 1800 ]; then', drill)
        failed = drill[drill.index("drill FAILED; what its copy"):drill.index("trap finish EXIT")]
        self.assertIn('kubectl delete ns "$NS" --wait=false', failed)
        self.assertIn('if [ "$STATE" = failed ] || [ "$AGE" -ge 1800 ]; then', text)
        self.assertIn('kubectl delete ns "$NS" --wait=true --timeout=300s', text)
        self.assertIn("another restore is running", text)
        self.assertNotIn("inspect it, then: kubectl delete ns $NS\"; exit 2", text)
        marked, removed, stop = text.index("pitr-run=failed --overwrite"), text.index('--wait=true --timeout=300s'), text.index("another restore is running")
        self.assertLess(removed, stop)
        self.assertLess(stop, marked, "the mark is written by the exit trap, defined after the start-up check")

if __name__ == "__main__":
    unittest.main(verbosity=2)
