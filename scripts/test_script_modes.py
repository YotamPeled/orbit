"""Every tracked shell script is executable in git's index (docs/V1.md §8): scripts added
from a Windows checkout (core.filemode=false) were recorded 100644, core-sync copied that
mode into the instance, and the desk refused `scripts/desk.sh scripts/remove-project.sh`
with 'Permission denied' — an agent removing a project (S17) stopped there."""
from __future__ import annotations

import pathlib
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent


class ScriptModesTest(unittest.TestCase):
    def test_every_tracked_shell_script_is_executable(self):
        if not (ROOT / ".git").exists():
            self.skipTest("needs the git index")
        out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-s", "--", "*.sh"],
                             capture_output=True, text=True, check=True).stdout
        bad = [line.split("\t", 1)[1] for line in out.splitlines() if line.startswith("100644 ")]
        self.assertEqual(bad, [], "shell scripts tracked without the executable bit "
                                  "(fix: git update-index --chmod=+x <file>)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
