#!/usr/bin/env python3
"""
Guard tests for scripts/verify/no-instance-strings.py — the oracle that keeps core
portable. Run as `python3 scripts/test_no_instance_strings.py`.

The check is vendored and normally runs inside a live instance repo from stage3, which is
exactly why it needs tests HERE: a leak oracle nobody has watched go red is
indistinguishable from one that does nothing, and this one has been that once already (an
interpolated scope list spliced a newline into a Python string literal, so it passed
vacuously from the day it was written until 2026-08-28).

Each test builds a FAITHFUL instance repo in a tmpdir — `template/` copied over it, then
every path in core-manifest.txt vendored out of this checkout, which is what
scripts/core-sync.sh does with a git clone in between — and runs the real checker over it
with the fixture org's identity. So the tree under test has the shape a scaffolded org
actually has: vendored core skills beside a Makefile and a chart, not a hand-built stub.
"""
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
CHECKER = "scripts/verify/no-instance-strings.py"
MANIFEST = ROOT / "core-manifest.txt"

# The same fixture org test_instance_init.py uses. EVERY value here has to be a string
# that appears nowhere in this repository, or the clean-tree assertion is not an assertion:
# a value already present makes it fail for reasons that have nothing to do with a leak,
# and a value the checker does not read makes it vacuous. The instance NAME is why this
# note is here: it was `fixture` until 2026-09-07, when `instance.name` joined the values
# the checker reads (review found it was the one identity field nothing checked) and the
# clean tree lit up with 60-odd hits on the ordinary English word in comments like
# "the disabled-agent fixture failed to generate". `fixtureinst` is the same fixture with
# a name that is a token rather than a word.
FIXTURE = {
    "instance": {"name": "fixtureinst"},
    "git": {"repoUrl": "git@github.com:fixture-org/fixture-instance.git",
            "branch": "main", "github_org": "fixture-org"},
    "domains": {"primary": "fixture.org", "dev": "dev.fixture.org",
                "dnsZones": ["fixture.org"]},
    "registry": "ghcr.io/fixture-org",
    "nodes": [{"name": "fixture-node-1", "ip": "198.51.100.10", "role": "control-plane"}],
}


def manifest_paths() -> list[tuple[str, str]]:
    """(core path, instance path) per manifest line. They differ only for the `src -> dst`
    form, which exists for an artifact core keeps under template/ and an instance has at
    the top of its tree."""
    out = []
    for ln in MANIFEST.read_text().splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        src, _, dst = (x.strip() for x in ln.partition(" -> "))
        out.append((src, dst or src))
    return out


class LeakCheckTest(unittest.TestCase):
    def vendored_instance(self) -> pathlib.Path:
        """template/ + the manifest's files, i.e. what core-sync.sh leaves behind."""
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        shutil.copytree(ROOT / "template", tmp, dirs_exist_ok=True)
        for rel, into in manifest_paths():
            src, dst = ROOT / rel, tmp / into
            self.assertTrue(src.exists(), f"core-manifest.txt names a missing path: {rel}")
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy(src, dst)
        (tmp / "instance" / "config.yaml").write_text(yaml.safe_dump(FIXTURE))
        return tmp

    def check(self, root: pathlib.Path) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, CHECKER],
                              cwd=root, capture_output=True, text=True)

    def skill(self, root: pathlib.Path, name: str) -> pathlib.Path:
        return root / ".claude" / "skills" / name / "SKILL.md"

    def test_a_report_names_the_field_never_the_value(self):
        """B5 (round 8): the report printed the matched value; a config value (a repo
        URL with userinfo pasted in) can be a credential. The FIELD is named now."""
        root = self.vendored_instance()
        cfg = yaml.safe_load((root / "instance" / "config.yaml").read_text())
        cfg["git"]["repoUrl"] = "https://operator:PLANTED-REPO-CREDENTIAL@github.com/fixture-org/repo"
        (root / "instance" / "config.yaml").write_text(yaml.safe_dump(cfg))
        target = self.skill(root, "orbit-develop")
        target.write_text(target.read_text() + "\ngit clone https://operator:PLANTED-REPO-CREDENTIAL@github.com/fixture-org/repo\n")
        proc = self.check(root)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("the value of git.repoUrl in .claude/skills/orbit-develop/SKILL.md", proc.stdout)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_an_unmatched_manifest_glob_is_named_by_position(self):
        """Round 20 (breaker B3): the refusal repeated the manifest entry's text."""
        root = self.vendored_instance()
        with (root / "core-manifest.txt").open("a") as fh:
            fh.write("token=PLANTED_MANIFEST_CREDENTIAL*\n")
        proc = self.check(root)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertRegex(proc.stderr, r"scope entry #[0-9]+ \(a glob pattern; not repeated\) matched no file under the instance root")
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_a_report_shows_a_file_name_only_in_file_name_shape(self):
        """Round 19 (breaker B2): the leak report repeated the leaking file's path — a
        scope file can be named after a token."""
        root = self.vendored_instance()
        odd = root / ".claude" / "skills" / "orbit-develop" / "Bearer PLANTED_FILE_CREDENTIAL.md"
        odd.write_text("see fixture.org\n")
        proc = self.check(root)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("in .claude/skills/orbit-develop/(a name not of a file-name shape, or of a credential's shape):1", proc.stdout)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_a_padded_or_uppercased_domain_is_searched_in_its_canonical_form(self):
        """Round 15 (breaker B6): the loader exports a domain stripped, lowercased and
        without its trailing dot; the scanner searched the config's spelling verbatim, so
        a padded value matched nothing and the vendored file was certified clean."""
        root = self.vendored_instance()
        cfg = root / "instance" / "config.yaml"
        c = yaml.safe_load(cfg.read_text()); c["domains"]["primary"] = "  Fixture.ORG. "
        cfg.write_text(yaml.safe_dump(c))
        target = self.skill(root, "orbit-observe")
        target.write_text(target.read_text() + "\nAsk https://grafana.fixture.org for the dashboard.\n")
        proc = self.check(root)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("the value of domains.primary in", proc.stdout); self.assertNotIn("fixture.org", proc.stdout.lower().replace("the value of", ""))

    def test_every_dns_zone_is_an_identity(self):
        """Round 15 (breaker B7): a secondary DNS zone (exported by the loader) was not
        among the values searched."""
        root = self.vendored_instance()
        cfg = root / "instance" / "config.yaml"
        c = yaml.safe_load(cfg.read_text()); c["domains"]["dnsZones"] = ["fixture.org", "Zone-Fixture.NET."]
        cfg.write_text(yaml.safe_dump(c))
        target = self.skill(root, "orbit-observe")
        target.write_text(target.read_text() + "\nThe zone zone-fixture.net is delegated here.\n")
        proc = self.check(root)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("the value of domains.dnsZones[1] in", proc.stdout); self.assertNotIn("zone-fixture", proc.stdout)

    def test_a_manifest_entry_cannot_leave_the_instance_root(self):
        """Round 16 (breaker B3): `../outside.txt` in the manifest put a file of this
        machine's into the scope and its report."""
        root = self.vendored_instance()
        (root.parent / "outside-scope.txt").write_text("grafana.fixture.org\n")
        for entry in ("../outside-scope.txt", "/etc/hostname", "scripts/../../outside-scope.txt"):
            with self.subTest(entry=entry):
                m = root / "core-manifest.txt"; body = m.read_text()
                m.write_text(body + entry + "\n")
                proc = self.check(root); m.write_text(body)
                self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
                self.assertIn("leaves the instance root", proc.stderr); self.assertNotIn("outside-scope", proc.stdout + proc.stderr)
        # a scope file replaced by a symlink that resolves outside the tree
        link = root / next(into for _, into in manifest_paths() if (root / into).is_file() and into != "core-manifest.txt")
        link.unlink(); link.symlink_to(root.parent / "outside-scope.txt")
        proc = self.check(root)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr); self.assertIn("resolves outside the instance root", proc.stderr)
        self.assertNotIn("outside-scope", proc.stdout + proc.stderr)
        # and one nested under a directory entry of the scope (found by the walk, not named by the manifest)
        link.unlink(); link.write_text("# restored\n")
        nested = self.skill(root, "orbit-observe").parent / "linked.md"; nested.symlink_to(root.parent / "outside-scope.txt")
        proc = self.check(root)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr); self.assertIn("resolves outside the instance root", proc.stderr)
        self.assertNotIn("outside-scope", proc.stdout + proc.stderr)

    def test_the_scaffold_carries_the_front_door_skill(self):
        """orbit-start is only useful if it is actually vendored to an org."""
        root = self.vendored_instance()
        self.assertTrue(self.skill(root, "orbit-start").is_file(),
                        "a vendored instance has no .claude/skills/orbit-start/SKILL.md")

    def test_a_clean_tree_passes(self):
        proc = self.check(self.vendored_instance())
        self.assertEqual(proc.returncode, 0,
                         "the clean vendored tree reported leaks:\n"
                         + proc.stdout + proc.stderr)

    def test_a_planted_domain_in_a_skill_fails(self):
        """THE regression. Before 2026-09-07 the scope stopped at the toolchain, so a
        skill could name one org's Grafana and every check in the repo stayed green."""
        root = self.vendored_instance()
        target = self.skill(root, "orbit-observe")
        target.write_text(target.read_text()
                          + "\nAsk https://grafana.fixture.org for the dashboard.\n")
        proc = self.check(root)
        self.assertEqual(proc.returncode, 1,
                         "a skill naming the instance's own domain was reported clean:\n"
                         + proc.stdout + proc.stderr)
        self.assertIn(".claude/skills/orbit-observe/SKILL.md", proc.stdout)
        self.assertIn("the value of domains.primary in", proc.stdout); self.assertNotIn("fixture.org", proc.stdout)

    def test_the_scope_is_exactly_the_manifest(self):
        """THE structural assertion, and it is a FULL-SET one on purpose. Until 2026-09-07
        the scope was a second list kept by hand beside core-manifest.txt, and the two had
        already drifted: pitr-drill.sh, core-sync.sh, am-routes.py and all three verify
        stages were vendored to every org and checked by nothing, while stage3 printed
        "core carries no instance strings" over them. A subset assertion would not have
        caught that — only equality does."""
        scope = subprocess.run([sys.executable, CHECKER, "--print-scope"],
                               cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(scope.returncode, 0, scope.stderr)
        in_scope = set(scope.stdout.split())
        # Resolved the way the checker resolves it: whichever spelling THIS tree has.
        expected = {dst if (ROOT / dst).exists() else src
                    for src, dst in manifest_paths()}
        expected |= {"Makefile", "tofu/*.tf"}
        self.assertEqual(in_scope, expected,
                         "the leak check's scope and core-manifest.txt disagree about "
                         "what core ships — a vendored file nothing checks")

    def test_the_swarm_skill_is_neither_vendored_nor_checked(self):
        """It is the manual for building core, not for running an instance (PRODUCT.md).
        Both lists are one list now, so this is really an assertion that dropping a path
        from the manifest drops it from the scope too — the same edit, not two."""
        vendored = {dst for _, dst in manifest_paths()}
        self.assertNotIn(".claude/skills/orbit-swarm", vendored)
        scope = subprocess.run([sys.executable, CHECKER, "--print-scope"],
                               cwd=ROOT, capture_output=True, text=True)
        self.assertNotIn("orbit-swarm", scope.stdout)

    def test_the_toolchain_is_still_covered(self):
        """The scope GREW; it must not have moved. A leak in the generator still fails."""
        root = self.vendored_instance()
        gen = root / "scripts" / "catalog-sync.py"
        gen.write_text(gen.read_text() + "\n# deployed at ghcr.io/fixture-org\n")
        proc = self.check(root)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("scripts/catalog-sync.py", proc.stdout)

    def test_the_scaffold_carries_the_operator_skills(self):
        """The agent-facing skills (docs/SKILLS.md) reach an org only if `skills` is in the
        manifest; that line is also what puts them in the leak oracle's scope."""
        root = self.vendored_instance()
        self.assertTrue((root / "skills" / "orbit-operator" / "SKILL.md").is_file(),
                        "a vendored instance has no skills/orbit-operator/SKILL.md")

    def test_a_planted_domain_in_an_operator_skill_fails(self):
        """The operator skills carry no fact about any installation: every id and host
        comes from a tool at run time. A domain planted in one must be reported."""
        root = self.vendored_instance()
        target = root / "skills" / "orbit-diagnose" / "SKILL.md"
        target.write_text(target.read_text()
                          + "\nQuery https://prometheus.fixture.org directly instead.\n")
        proc = self.check(root)
        self.assertEqual(proc.returncode, 1,
                         "an operator skill naming the instance's domain was reported clean:\n"
                         + proc.stdout + proc.stderr)
        self.assertIn("skills/orbit-diagnose/SKILL.md", proc.stdout)
        self.assertIn("the value of domains.primary in", proc.stdout); self.assertNotIn("fixture.org", proc.stdout)

    def test_a_planted_url_in_a_skill_fails(self):
        """THE second regression, and the reason the label-key exemption is gone. That
        exemption skipped any LINE containing `<primary>/`, which a URL also contains — so
        review planted `curl https://fixture.org/` in a vendored skill on 2026-09-07 and
        the checker returned 0 with no output. The prefix is the constant `orbit.local/`
        now, so nothing needs exempting and a literal instance URL cannot hide behind a
        slash."""
        root = self.vendored_instance()
        target = self.skill(root, "orbit-develop")
        target.write_text(target.read_text()
                          + "\nRun `curl https://fixture.org/healthz` to check it.\n")
        proc = self.check(root)
        self.assertEqual(proc.returncode, 1,
                         "a skill carrying the instance's own URL was reported clean:\n"
                         + proc.stdout + proc.stderr)
        self.assertIn(".claude/skills/orbit-develop/SKILL.md", proc.stdout)

    def test_a_planted_instance_name_in_a_skill_fails(self):
        """THE third regression. `instance.name` was not among the values the checker read
        until 2026-09-07, so a skill could name the org outright and stay green — and the
        name is the field core paths are DERIVED from, which makes it the likeliest thing
        for a skill to hardcode."""
        root = self.vendored_instance()
        target = self.skill(root, "orbit-ship")
        target.write_text(target.read_text()
                          + "\nThe cluster is the fixtureinst instance.\n")
        proc = self.check(root)
        self.assertEqual(proc.returncode, 1,
                         "a skill naming the instance was reported clean:\n"
                         + proc.stdout + proc.stderr)
        self.assertIn(".claude/skills/orbit-ship/SKILL.md", proc.stdout)
        self.assertIn("the value of instance.name in", proc.stdout); self.assertNotIn("fixtureinst", proc.stdout)

    def test_an_unreadable_config_is_refused_without_the_parsers_excerpt(self):
        """Round 3 (breaker + auditor): a YAML error's text quotes the source around the
        error; the refusal names the class only."""
        root = self.vendored_instance()
        (root / "instance" / "config.yaml").write_text("instance: [PLANTED-CREDENTIAL-MARKER\n")
        proc = self.check(root)
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)
        self.assertIn("not a readable instance config", proc.stdout + proc.stderr)

    def test_the_name_inside_another_word_is_not_a_leak(self):
        """Round 2 (2026-09-13, auditor): an instance named `west` failed the gate on the
        word `newest` in a vendored stage. A value is a leak as an IDENTITY — embedded in a
        path or a flag — never as the inside of another word."""
        root = self.vendored_instance()
        cfg = yaml.safe_load((root / "instance" / "config.yaml").read_text())
        cfg["instance"]["name"] = "west"
        (root / "instance" / "config.yaml").write_text(yaml.safe_dump(cfg))
        target = self.skill(root, "orbit-develop")
        original = target.read_text()
        target.write_text(original + "\nthe newest line, westward, the southwest\n")
        proc = self.check(root)
        self.assertEqual(proc.returncode, 0, "a word containing the name was reported as a leak:\n" + proc.stdout + proc.stderr)
        for planted in ("kubeconfig-west", "~/.config/west-platform/x", "the west instance", "west.example"):
            target.write_text(original + f"\n{planted}\n")
            proc = self.check(root)
            self.assertEqual(proc.returncode, 1, f"{planted!r} was reported clean:\n" + proc.stdout + proc.stderr)
            self.assertIn("the value of instance.name in .claude/skills/orbit-develop/SKILL.md", proc.stdout)

    def test_the_name_is_caught_inside_a_path_derived_from_it(self):
        """The realistic shape of the leak, and the reason the match is a SUBSTRING one:
        nobody writes the bare name, they write the path lib-instance.sh would have built
        for them. `kubeconfig-<name>`, `~/.kube/config-<name>-platform`,
        `~/.config/<name>-platform` are all the same failure — the value itself is what
        matches, and it matches wherever it is embedded."""
        root = self.vendored_instance()
        target = self.skill(root, "orbit-develop")
        for planted in ("kubeconfig-fixtureinst",
                        "export KUBECONFIG=~/.kube/config-fixtureinst-platform",
                        "GPW=$(cat ~/.config/fixtureinst-platform/grafana-admin-password)"):
            original = target.read_text()
            target.write_text(original + f"\n{planted}\n")
            proc = self.check(root)
            self.assertEqual(proc.returncode, 1,
                             f"{planted!r} was reported clean:\n"
                             + proc.stdout + proc.stderr)
            # The reported value is the NAME, not the string it was embedded in — which is
            # what tells the reader which config field the line has to stop hardcoding.
            self.assertIn("the value of instance.name in .claude/skills/orbit-develop/SKILL.md",
                          proc.stdout)
            target.write_text(original)

    def test_a_leak_in_a_vendored_script_that_is_not_a_skill_fails(self):
        """The scope hole review found: these files are vendored to every org and were
        outside the old hand-kept list entirely."""
        root = self.vendored_instance()
        for rel in ("scripts/pitr-drill.sh", "scripts/verify/stage3.sh",
                    "scripts/core-sync.sh"):
            f = root / rel
            original = f.read_text()
            f.write_text(original + "\n# drill against fixture.org\n")
            proc = self.check(root)
            self.assertEqual(proc.returncode, 1, f"{rel} is not checked:\n"
                             + proc.stdout + proc.stderr)
            self.assertIn(rel, proc.stdout)
            f.write_text(original)

    def test_it_fails_closed_without_a_config(self):
        """Exit 2, not 0: 'no output' must never be able to mean 'nothing ran'."""
        root = self.vendored_instance()
        (root / "instance" / "config.yaml").unlink()
        proc = self.check(root)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("did not run", proc.stderr)

    def test_an_unreadable_scope_file_is_refused_by_class_not_by_name(self):
        """Round 21 (breaker B2): the refusal repeated the file's path and the OS
        message (which repeats the path again); a file named after a token put the
        token on stderr. The exception class alone is reported now."""
        if os.geteuid() == 0:
            self.skipTest("root reads a mode-000 file")
        root = self.vendored_instance()
        f = root / ".claude" / "skills" / "orbit-develop" / "Bearer PLANTED_FILE_CREDENTIAL_0123456789.md"
        f.write_text("x\n"); f.chmod(0)
        self.addCleanup(lambda: f.chmod(0o644))
        proc = self.check(root)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("cannot read", proc.stderr); self.assertIn("(PermissionError)", proc.stderr)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_an_unlistable_scope_directory_is_refused(self):
        """Round 22 (breaker B4): os.walk swallowed a listing error, so a directory with
        mode 0111 hid its files and the check reported clean."""
        if os.geteuid() == 0:
            self.skipTest("root lists a mode-0111 directory")
        root = self.vendored_instance()
        hidden = root / ".claude" / "skills" / "orbit-develop" / "PLANTED_UNLISTED_DIR"
        hidden.mkdir(); (hidden / "leak.md").write_text("Query https://prometheus.fixture.org directly.\n")
        hidden.chmod(0o111); self.addCleanup(lambda: hidden.chmod(0o755))
        proc = self.check(root)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("a scope directory could not be listed (PermissionError; the path is not repeated)", proc.stderr)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_a_symlinked_directory_in_the_scope_is_refused(self):
        """Round 21 (breaker B5): rglob did not descend a symlinked directory, so a skill
        file reached through one was never read — a scope that silently shrank."""
        root = self.vendored_instance()
        real = root / "instance" / "PLANTED_LINK_TARGET"; real.mkdir()
        (real / "SKILL.md").write_text("Query https://prometheus.fixture.org directly.\n")
        os.symlink(real, root / ".claude" / "skills" / "orbit-develop" / "PLANTED_LINK_NAME")
        proc = self.check(root)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("a scope directory contains a symlinked directory (not repeated)", proc.stderr)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_it_fails_closed_when_the_scope_shrinks(self):
        """A scope path that vanished is an unchecked file, and silence about it is how a
        vendoring bug becomes a clean bill of health."""
        root = self.vendored_instance()
        shutil.rmtree(root / ".claude" / "skills" / "orbit-ship")
        proc = self.check(root)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("orbit-ship", proc.stderr)

    def test_it_fails_closed_without_the_manifest(self):
        """The scope is DERIVED from core-manifest.txt, so a tree without it has an empty
        scope — which would scan nothing and print nothing. Exit 2, not 0."""
        root = self.vendored_instance()
        (root / "core-manifest.txt").unlink()
        proc = self.check(root)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("core-manifest.txt", proc.stderr)

    def test_stage3_still_calls_the_check(self):
        """The suite is the only caller that runs on a live org. An extraction that left
        stage3 calling nothing would be green everywhere and prove nothing anywhere."""
        stage3 = (ROOT / "scripts" / "verify" / "stage3.sh").read_text()
        self.assertIn(CHECKER, stage3)


if __name__ == "__main__":
    unittest.main(verbosity=2)

