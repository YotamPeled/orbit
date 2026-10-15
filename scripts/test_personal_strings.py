#!/usr/bin/env python3
"""The repository carries no personal string and no secret, and the guard that keeps it so
works (scripts/personal_strings.py, the hooks in .githooks).

Every planted value below is assembled at run time, so this file itself stays clean.

Run as `python3 scripts/test_personal_strings.py` or under pytest.
"""
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import personal_strings as ps  # noqa: E402


def j(*parts: str) -> str:
    return "".join(parts)


class TheTreeIsClean(unittest.TestCase):
    def test_no_file_carries_a_personal_string_or_a_secret(self):
        hits = ps.scan_files(ps.working_tree())
        self.assertEqual(hits, [], "\n".join(hits[:40]))

    def test_the_list_and_the_author_are_set(self):
        self.assertGreaterEqual(len(ps.HASHED), 10, "the hashed list of personal strings is empty")
        self.assertTrue(ps.AUTHOR, "the repository's one author is not set")


class TheGuardCatchesEachKind(unittest.TestCase):
    def hits(self, line: str) -> list[str]:
        return [why for _, why in ps.scan_text(line)]

    def test_a_hashed_string_inside_a_word_and_in_any_case(self):
        word = j("zq", "xw", "vu", "ts")
        saved = ps.HASHED
        try:
            ps.HASHED = frozenset(saved | {f"{len(word)}:{ps._h(word)}"})
            ps._SEEN.clear()
            for line in (word, word.upper(), f"https://{word}-platform.example/x", f"a.{word}2.b"):
                self.assertIn("a personal string (one of the hashed list)", self.hits(line), line)
            self.assertEqual(self.hits("nothing of it here"), [])
        finally:
            ps.HASHED = saved
            ps._SEEN.clear()

    def test_the_repositorys_own_home_is_allowed_and_nothing_next_to_it(self):
        acct = j("Yot", "am", "Pel", "ed")
        for ok in (f"ghcr.io/{acct.lower()}/orbitd:v1", f"ghcr.io/{acct.lower()}/orbit-desk:latest",
                   f"https://github.com/{acct}/orbit.git"):
            self.assertEqual(self.hits(ok), [], ok)
        for bad in (f"https://github.com/{acct}/orbit-core.git", f"ghcr.io/{acct.lower()}/other", f"by {acct}"):
            self.assertIn("a personal string (one of the hashed list)", self.hits(bad), bad)

    def test_mail_addresses_paths_and_addresses(self):
        cases = {
            j("someone.real", "@", "gmail", ".com"): "a personal mail address",
            j("1234+someone", "@users", ".noreply.github.com"): "a GitHub account's no-reply address",
            j("cd /c/Us", "ers/alice/src"): "a home-directory path that names a person's account",
            j("C:", "\\", "Users", "\\", "alice", "\\", "x"): "a home-directory path that names a person's account",
            j("ssh root@", "5.6", ".7.8"): "a public IPv4 address (allow a no-one's address in ALLOWED_PUBLIC_IPV4)",
        }
        for line, why in cases.items():
            self.assertIn(why, self.hits(line), line)
        for clean in ("ops@example.com", "/home/orbit/.kube", "ip 203.0.113.65", "10.0.0.5", "version 1.36.5"):
            self.assertEqual(self.hits(clean), [], clean)

    def test_credentials_by_shape_and_placeholders_pass(self):
        body = "M" + "IIEow" * 16
        cases = {
            j("AK", "IA", "Z" * 16): "an AWS access key",
            j("gh", "p_", "a1B2" * 9): "a GitHub token",
            j("AGE-SECRET", "-KEY-1", "Q" * 58): "an age secret key",
            j("123456789", ":AA", "b" * 33): "a Telegram bot token",
            j("sk-", "ant-api03-", "x9" * 20): "an Anthropic or OpenAI key",
            j("-----BEGIN ", "PRIVATE KEY-----\n", body, "\n"): "a private key with a body",
        }
        for text, why in cases.items():
            self.assertIn(why, self.hits(text), why)
        self.assertEqual(self.hits(j("gh", "p_", "PLANTED", "x" * 30)), [])

    def test_a_co_author_trailer_and_another_author_are_refused(self):
        msg = j("Change\n\nCo-", "Authored-By: someone <a@example.com>\n")
        self.assertTrue(any("Co-authored-by" in p for p in ps.message_problems(msg)))
        self.assertEqual(ps.message_problems("Change\n\nWhy it changed.\n"), [])
        self.assertIsNotNone(ps.author_problem("someone-else@example.com"))


class TheHooksAreWired(unittest.TestCase):
    def test_each_hook_runs_the_guard_and_is_executable(self):
        index = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-s", ".githooks"],
                               capture_output=True, text=True, check=True).stdout
        for hook, arg in (("pre-commit", "--staged"), ("commit-msg", "--message"), ("pre-push", "--push")):
            text = (ROOT / ".githooks" / hook).read_text()
            self.assertIn("scripts/personal_strings.py", text, hook)
            self.assertIn(arg, text, hook)
            row = next((l for l in index.splitlines() if l.endswith(f".githooks/{hook}")), "")
            self.assertTrue(row.startswith("100755"), f".githooks/{hook} is not executable in the index")

    def test_the_local_tests_set_the_hooks_path(self):
        self.assertIn("git config core.hooksPath .githooks", (ROOT / "scripts" / "local-tests.sh").read_text())

    def test_the_hook_stops_a_commit_that_carries_a_secret(self):
        with tempfile.TemporaryDirectory() as d:
            repo = pathlib.Path(d)
            env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
                       GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
            git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True, env=env)
            git("init", "-q")
            (repo / "scripts").mkdir()
            src = (HERE / "personal_strings.py").read_text(encoding="utf-8")
            (repo / "scripts" / "personal_strings.py").write_text(src.replace(f'AUTHOR = "{ps.AUTHOR}"', 'AUTHOR = ""'), encoding="utf-8")
            (repo / "leak.txt").write_text(j("key AK", "IA", "Q" * 16, "\n"))
            git("add", "-A")
            out = subprocess.run([sys.executable, str(repo / "scripts" / "personal_strings.py"), "--staged"],
                                 capture_output=True, text=True, env=env)
            self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
            self.assertIn("leak.txt:1: an AWS access key", out.stdout)


if __name__ == "__main__":
    unittest.main()
