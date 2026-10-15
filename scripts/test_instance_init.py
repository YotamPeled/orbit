#!/usr/bin/env python3
"""
Guard tests for scripts/instance-init.py — run as `python3 scripts/test_instance_init.py`.

The one that matters is the PLANE guard. sops is the only secret plane: `make init`
renders no Secret producer of its own, and an instance that still carries what the
retired plane rendered loses exactly those files on its next run — reported by path,
with a hand-written file beside them surviving. SopsPlaneTest goes red the moment a
store, vault or operator object is rendered again, or a retired file survives.

The other one that matters is the EXAMPLE guard. Every test below builds its config from
BASE_CONFIG, a hand-written dict — which is exactly why the suite stayed green while
template/instance/config.yaml.example, the file a new org actually copies, was missing
objectStorage.endpoint and exited on init's own guard.
test_a_faithful_copy_of_the_example_scaffolds runs the real script over the real template
and the real example instead, so the shipped example is checked as a config, not as prose.

The third family that matters is RE-RENDER ON EVERY INIT (ReRenderTest). Filling a
{{token}} in place is one-way — the substitution leaves a literal — so under the old
behaviour a value written on the day the repo was cut could never be corrected, and two
of those values are not knowable that day: the node's address (init runs before tofu
creates the server) and a core image tag (bumping it IS a later run).
Each test below changes one value in config.yaml, re-runs the real entry point, and
asserts the manifest MOVED. They are written against the specific failures review round 1
reproduced, so each names the stale value it would find.

The fourth is the GATES. A gate that only ever opens is half a feature: taking a bucket or
`intakeIngress` back out has to DELETE what it rendered, or the tree keeps
a public door or an S3-backed Application the config no longer asks for. Every gate test
drives the round trip in both directions.

Each test builds a throwaway instance repo in a tmpdir (the script keys off its own
location, so it is copied in) and runs the real entry point as a subprocess.
"""
import json
import pathlib
import shutil
import importlib.util
import subprocess
import sys
import tempfile
import unittest

import yaml

SCRIPT = pathlib.Path(__file__).resolve().parent / "instance-init.py"
# Core-only: instance-init.py is vendored (core-manifest.txt), this test is not, so
# template/ is always beside it here and never in an instance repo.
TEMPLATE = pathlib.Path(__file__).resolve().parent.parent / "template"

# The scaffold-token regex, IMPORTED from the script under test rather than restated here.
# Other tools also spell an expression `${{ ... }}`, so "no placeholder survives
# init" cannot be a search for `{{` — it has to be the script's own dotted, space-free
# shape, and a copy of that regex in the test would be a second thing to keep in step.
_SPEC = importlib.util.spec_from_file_location("instance_init", SCRIPT)
_INIT_MODULE = importlib.util.module_from_spec(_SPEC)
# No __pycache__ beside a vendored script: this repo has no .gitignore, so a stray
# bytecode directory here shows up as untracked noise in every `git status`.
_BYTECODE, sys.dont_write_bytecode = sys.dont_write_bytecode, True
_SPEC.loader.exec_module(_INIT_MODULE)
sys.dont_write_bytecode = _BYTECODE
TOKEN_RE = _INIT_MODULE.TOKEN_RE

BASE_CONFIG = {
    "instance": {"name": "fixtureinst"},
    "git": {"repoUrl": "git@github.com:fixture-org/fixture-instance.git",
            "branch": "main", "github_org": "fixture-org"},
    "domains": {"primary": "fixture.org", "dev": "dev.fixture.org",
                "dnsZones": ["fixture.org"]},
    "registry": "ghcr.io/fixture-org",
    "coreRegistry": {"owner": "ghcr.io/fixture-core", "hooksTag": "main-1111111",
                     "orbitdTag": "main-1111111"},
    # NOT 203.0.113.10 and NOT "Ada"/"Admin": those are placeholders the example config
    # ships, and init now refuses a config that still carries one (EXAMPLE_VALUES). A
    # fixture built out of sentinels would make every test below assert the refusal.
    "nodes": [{"name": "fixture-node-1", "ip": "198.51.100.10", "role": "control-plane"}],
    "secrets": {"provider": "sops", "environments": ["dev", "prod"]},
    "contacts": {"telegram": True, "telegramChatId": "1", "email": "ops@fixture.org",
                 "adminFirstName": "Fixture", "adminLastName": "Operator"},
}

class InitFixture:
    """A minimal instance repo: the script, a config, and the tree init writes into."""

    def __init__(self, root: pathlib.Path, config: dict):
        self.root = root
        (root / "scripts").mkdir()
        shutil.copy(SCRIPT, root / "scripts" / SCRIPT.name)
        # The staged tree's pristine sources. An instance repo carries them because the
        # template does; a fixture that omitted them would test a different tree.
        shutil.copytree(TEMPLATE / "provisioning", root / "provisioning")
        (root / "instance").mkdir()
        (root / "instance" / "config.yaml").write_text(yaml.safe_dump(config))
        # The static (unstaged) template artifacts an instance carries under clusters/ from
        # its first commit: the generator reads the blackbox Application there (round 24).
        shutil.copytree(TEMPLATE / "clusters", root / "clusters")
        self.static = {f.relative_to(root) for f in (root / "clusters").rglob("*") if f.is_file()}

    def run(self) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "scripts/" + SCRIPT.name],
                              cwd=self.root, capture_output=True, text=True)

    def files(self) -> list:
        """Every file init wrote under clusters/ — empty when a run was refused."""
        base = self.root / "clusters"
        return [f for f in sorted(base.rglob("*")) if f.is_file() and f.relative_to(self.root) not in self.static] if base.is_dir() else []

    def write_config(self, patch: dict) -> None:
        """Merge a patch into instance/config.yaml. A None value DELETES the key, which is
        how "the operator took the auth block back out" is expressed."""
        cfg = yaml.safe_load((self.root / "instance" / "config.yaml").read_text())
        for k, v in patch.items():
            if v is None:
                cfg.pop(k, None)
            else:
                cfg[k] = v
        (self.root / "instance" / "config.yaml").write_text(yaml.safe_dump(cfg))

    def rendered(self, rel: str) -> pathlib.Path:
        return self.root / "clusters" / rel

    def snapshot(self) -> dict:
        """Every generated file and its content — what a second init must not move."""
        base = self.root / "clusters"
        return {str(f.relative_to(base)): f.read_text()
                for f in sorted(base.rglob("*")) if f.is_file()}

    def kinds(self) -> dict:
        """Every rendered document's kind, keyed by file — what the secret plane
        must never add a store, vault or operator object to."""
        out = {}
        for f in sorted((self.root / "clusters").rglob("*.yaml")):
            for doc in yaml.safe_load_all(f.read_text()):
                if isinstance(doc, dict) and doc.get("kind"):
                    out.setdefault(str(f.relative_to(self.root / "clusters")),
                                   []).append(doc["kind"])
        return out


EXAMPLE_FILLED = {
    "instance.name": "fixtureinst",
    "git.repoUrl": "git@github.com:fixture-org/fixture-instance.git",
    "git.github_org": "fixture-org",
    "domains.primary": "fixture.org",
    "domains.dev": "dev.fixture.org",
    "domains.dnsZones": ["fixture.org"],
    "registry": "ghcr.io/fixture-org",
    "coreRegistry.owner": "ghcr.io/fixture-core",
    "coreRegistry.orbitdTag": "main-1111111",
    "agentRuntime.image": "ghcr.io/fixture-core/orbit-agent-runtime",
    "agentRuntime.tag": "main-1111111",
    "intakeRuntime.image": "ghcr.io/fixture-core/orbit-intake",
    "intakeRuntime.tag": "main-1111111",
    "objectStorage.buckets.platform": "fixtureplatform",
    "objectStorage.buckets.appData": "fixtureapps",
    "contacts.adminFirstName": "Fixture",
    "contacts.adminLastName": "Operator",
    "contacts.telegramChatId": "1",
    "contacts.email": "ops@fixture.org",
}


class ExampleConfigTest(unittest.TestCase):
    """The example config must be a config that WORKS — and must not be RUNNABLE as-is.

    Two halves, and the second one is the round-2 finding. The other tests here build a
    config from BASE_CONFIG, which is written by hand and kept in step with the script — so
    it can (and did) stay green while the file a new org actually copies is missing a field
    the script requires. These copy the real template/ and the real config.yaml.example and
    run the real entry point over it.

    The second half exists because init used to check the instance NAME and nothing else.
    A reviewer changed that one line, ran init, and got a complete scaffold rendered
    against example.org, an example git remote, an example bucket, the RFC 5737
    documentation node address, and a Telegram chat id of 111111111 — every one of which
    would have been committed and synced into a real cluster.
    """

    def scaffold(self, fill_everything: bool) -> pathlib.Path:
        """template/ + the script, with config.yaml.example copied to config.yaml."""
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        shutil.copytree(TEMPLATE, tmp, dirs_exist_ok=True)
        (tmp / "scripts").mkdir(exist_ok=True)
        shutil.copy(SCRIPT, tmp / "scripts" / SCRIPT.name)
        example = (tmp / "instance" / "config.yaml.example").read_text()
        if not fill_everything:
            # The ONE substitution a lazy operator makes. Text, not YAML, so that this is
            # literally "copied the example and changed the name".
            filled = example.replace("name: example", "name: fixture", 1)
            self.assertNotEqual(filled, example,
                                "config.yaml.example lost its instance.name")
            (tmp / "instance" / "config.yaml").write_text(filled)
            return tmp
        cfg = yaml.safe_load(example)
        for path, value in EXAMPLE_FILLED.items():
            *parents, leaf = path.split(".")
            target = cfg
            for key in parents:
                target = target[key]
            self.assertIn(leaf, target,
                          f"config.yaml.example no longer carries {path}")
            target[leaf] = value
        for n in cfg["nodes"]:
            n["ip"] = "198.51.100.10"
        (tmp / "instance" / "config.yaml").write_text(yaml.safe_dump(cfg))
        return tmp

    def init(self, root: pathlib.Path) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "scripts/" + SCRIPT.name],
                              cwd=root, capture_output=True, text=True)

    def test_a_faithful_copy_of_the_example_scaffolds(self):
        root = self.scaffold(fill_everything=True)
        proc = self.init(root)
        # Red before objectStorage.endpoint was added to the example: the script exits on
        # the declared-bucket-without-an-endpoint guard.
        self.assertEqual(proc.returncode, 0,
                         "a filled-in copy of config.yaml.example does not scaffold:\n"
                         + proc.stdout + proc.stderr)
        self.assertNotIn("UNRESOLVED", proc.stdout)
        # The example declares a platform bucket, so the bucket-backed apps must SURVIVE.
        # Without this, a future example that simply dropped objectStorage would satisfy
        # the exit-0 assertion above by pruning Loki and Tempo out of the scaffold.
        for rel in ("clusters/platform/apps/loki.yaml",
                    "clusters/platform/apps/tempo.yaml",
                    "clusters/platform/apps/alloy.yaml"):
            self.assertTrue((root / rel).exists(), f"{rel} was pruned: " + proc.stdout)
        # ...and the instance a new organization gets from the example APPLIES: orbitd is
        # the only thing that applies a tenant, and with the switch off a platform can
        # ship nothing (the rebuilt test box, docs/V1.md §8: S01's first ship ended
        # "ORBITD_APPLY=off: apply is disabled on this orbitd").
        deployment = (root / "clusters/platform/config/orbitd/deployment.yaml").read_text()
        self.assertIn('- {name: ORBITD_APPLY, value: "on"}', deployment)

    def test_changing_only_the_name_is_refused_and_writes_nothing(self):
        """The round-2 finding, run forwards: a config that is example everywhere but the
        name must not produce a scaffold, and must not produce HALF of one either."""
        root = self.scaffold(fill_everything=False)
        before = {str(f.relative_to(root)): f.read_text()
                  for f in sorted((root / "clusters").rglob("*")) if f.is_file()}
        proc = self.init(root)
        self.assertNotEqual(proc.returncode, 0,
                            "init scaffolded an org from an untouched example config:\n"
                            + proc.stdout)
        after = {str(f.relative_to(root)): f.read_text()
                 for f in sorted((root / "clusters").rglob("*")) if f.is_file()}
        self.assertEqual(before, after, "init wrote into clusters/ before refusing")
        # Every offending key in ONE error, not one per run.
        for key in ("git.repoUrl", "git.github_org", "domains.primary", "domains.dev",
                    "domains.dnsZones", "registry",
                    "objectStorage.buckets.platform", "objectStorage.buckets.appData",
                    "contacts.email", "contacts.telegramChatId", "nodes[0].ip"):
            self.assertIn(key, proc.stderr, f"the refusal does not name {key}")

    def test_a_dollar_prefixed_expression_is_not_a_placeholder(self):
        """The `$` guard on TOKEN_RE: `${{name.field}}` is another tool's expression written
        tight, and it has our exact placeholder shape one character in."""
        self.assertIsNone(TOKEN_RE.search("${{inputs.image}}"),
                          "TOKEN_RE reads a `${{...}}` expression as a placeholder")
        self.assertIsNotNone(TOKEN_RE.search("{{inputs.image}}"))

    def test_init_keeps_no_dot_github_tree(self):
        """The Actions build pipeline left the template (2026-10-06): a scaffolded instance
        gets no .github/ from core, and init leaves an instance's own .github/ alone —
        a file there is the org's, whatever it holds."""
        self.assertFalse((TEMPLATE / ".github").exists(), "template/ ships a .github/ tree")
        root = self.scaffold(fill_everything=True)
        own = root / ".github" / "workflows" / "own.yml"
        own.parent.mkdir(parents=True)
        body = "jobs:\n  x:\n    uses: a/b/.github/workflows/c.yml@main # orbit:managed # orbit:managed\n# {{domains.primary}}\n"
        own.write_text(body)
        proc = self.init(root)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("WARNING", proc.stdout)
        self.assertEqual(own.read_text(), body, "init rewrote a file under .github/")
        self.assertEqual(sorted(str(f.relative_to(root)) for f in (root / ".github").rglob("*") if f.is_file()),
                         [".github/workflows/own.yml"])

    def test_a_registry_that_does_not_belong_to_the_org_is_refused(self):
        """Round 1 (Sol 1 / Muse 2): the org's images are pushed to ghcr.io/<the org>,
        while the catalog's `image:` is written from `registry:`. Two owners in one config
        is an org whose images are real and whose deploys never happen, with nothing
        anywhere to say so."""
        root = self.scaffold(fill_everything=True)
        cfg = yaml.safe_load((root / "instance" / "config.yaml").read_text())
        cfg["registry"] = "ghcr.io/some-other-team"
        (root / "instance" / "config.yaml").write_text(yaml.safe_dump(cfg))
        proc = self.init(root)
        self.assertNotEqual(proc.returncode, 0,
                            "init accepted a registry owned by another org:\n"
                            + proc.stdout)
        # Both halves of the disagreement, or the operator cannot tell which is wrong.
        self.assertIn("ghcr.io/some-other-team", proc.stderr)
        self.assertIn("ghcr.io/fixture-org", proc.stderr)

    def test_a_refusal_never_echoes_the_value_it_refuses(self):
        """Sweep area 1 (2026-09-13): a pasted URL may carry userinfo; the refusal names
        the field and the shape it expects, never the value."""
        cases = [
            ("git.repoUrl", lambda c: c["git"].__setitem__("repoUrl", "https://user:PLANTED-PASSWORD@bad")),
            # round 2 (breaker): every refusal that repeated its value, by field
            ("git.repoUrl", lambda c: c["git"].__setitem__("repoUrl", "git@github.com:user:PLANTED@x/i.git")),
            # round 3 (breaker): userinfo before a well-formed owner/repo tail rendered into five manifests
            ("git.repoUrl", lambda c: c["git"].__setitem__("repoUrl", "https://user:PLANTED-CREDENTIAL@github.com/fixture-org/fixture-instance.git")),
            ("git.repoUrl", lambda c: c["git"].__setitem__("repoUrl", "https://PLANTED.example/fixture-org/fixture-instance.git")),
            ("monitoring.probes[0].url", lambda c: c.__setitem__("monitoring", {"probes": [{"url": "https://user:PLANTED@x.org/", "module": "http_2xx"}]})),
            ("monitoring.probes[0].url", lambda c: c.__setitem__("monitoring", {"probes": [{"url": "ftp://PLANTED.org/", "module": "http_2xx"}]})),
            ("secrets.environments", lambda c: c.setdefault("secrets", {}).__setitem__("environments", ["dev", "prod", "PLANTED"])),
            ("git.github_org", lambda c: c["git"].__setitem__("github_org", "user:PLANTED@x")),
            ("registry", lambda c: c.__setitem__("registry", "ghcr.io/user:PLANTED@x")),
            ("orbitd.applyMode", lambda c: c.__setitem__("orbitd", {"applyMode": "PLANTED-mode"})),
            ("orbitd.applyPairs[1]", lambda c: c.__setitem__("orbitd", {"applyMode": "off", "applyPairs": ["a/b", "PLANTED"]})),
            ("orbitd.applyPairs[0]", lambda c: c.__setitem__("orbitd", {"applyMode": "off", "applyPairs": ["PLANTED pair/x"]})),
            ("intakeIngress", lambda c: c.__setitem__("intakeIngress", "PLANTED")),
            ("secrets.provider", lambda c: c.setdefault("secrets", {}).__setitem__("provider", "PLANTED-vault")),
        ]
        for field, apply in cases:
            with self.subTest(field=field):
                root = self.scaffold(fill_everything=True)
                cfg = yaml.safe_load((root / "instance" / "config.yaml").read_text())
                apply(cfg)
                (root / "instance" / "config.yaml").write_text(yaml.safe_dump(cfg))
                proc = self.init(root)
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn(field, proc.stderr)
                self.assertNotIn("PLANTED", proc.stdout + proc.stderr)
                self.assertNotIn("Traceback", proc.stderr)

    def test_an_unreadable_config_is_refused_without_the_parsers_excerpt(self):
        """Round 2 (breaker): a YAML error's message quotes the text around the error, and
        a config may hold a pasted value there."""
        root = self.scaffold(fill_everything=True)
        (root / "instance" / "config.yaml").write_text("instance:\n  name: fx\n  PLANTED-secret: [unclosed\n")
        proc = self.init(root)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("cannot be read as YAML", proc.stderr)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_a_value_that_would_rewrite_the_document_around_it_is_refused_by_field(self):
        """Sweep area 1 (2026-09-13): every token lands in YAML text as-is, so a value with
        a line break or a quote would add a second field where one was meant. Refused by
        FIELD name before anything is written; the period must be a whole number."""
        for field, apply in (
            ("orbitd.reconcilePeriodS", lambda c: c.__setitem__("orbitd", {"applyMode": "off", "reconcilePeriodS": '180"}\n            - {name: PORT, value: "9999'})),
            ("orbitd.reconcilePeriodS", lambda c: c.__setitem__("orbitd", {"applyMode": "off", "reconcilePeriodS": 0})),
            ("contacts.adminFirstName", lambda c: c["contacts"].__setitem__("adminFirstName", 'Fix"ture')),
            ("domains.primary", lambda c: c["domains"].__setitem__("primary", "fixture.org\n  PLANTED: yes")),
            # text YAML would read as something else where it lands bare (breaker, 2026-09-13)
            ("git.branch", lambda c: c["git"].__setitem__("branch", "null")),
            # a flow indicator splits a `[a, b]` list or closes a `{}` map the token lands in (round 4)
            ("git.branch", lambda c: c["git"].__setitem__("branch", "main, PLANTED")),
            ("git.branch", lambda c: c["git"].__setitem__("branch", "main]PLANTED")),
            ("contacts.email", lambda c: c["contacts"].__setitem__("email", "ops: PLANTED@example.net")),
            # a backslash reads as an escape inside a double-quoted YAML scalar (round 2, breaker)
            ("domains.primary", lambda c: c["domains"].__setitem__("primary", "fixture\\q.org")),
        ):
            with self.subTest(field=field):
                root = self.scaffold(fill_everything=True)
                cfg = yaml.safe_load((root / "instance" / "config.yaml").read_text())
                apply(cfg)
                (root / "instance" / "config.yaml").write_text(yaml.safe_dump(cfg))
                before = {f: f.read_text() for f in root.rglob("*.yaml") if f.is_file()}
                proc = self.init(root)
                self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertIn(field, proc.stderr)
                self.assertNotIn("PORT", proc.stdout + proc.stderr); self.assertNotIn("PLANTED", proc.stdout + proc.stderr)
                after = {f: f.read_text() for f in root.rglob("*.yaml") if f.is_file()}
                self.assertEqual(after, before, "init wrote before refusing")

    def test_a_repo_url_owned_by_another_org_is_refused(self):
        """Review round 2 (Sol 3): the owner is spelled THREE times — git.repoUrl,
        git.github_org, registry — and round 1 made only the last two agree. A repoUrl
        naming another owner rendered a scaffold whose instance repo the GitHub App
        installed on git.github_org cannot reach."""
        root = self.scaffold(fill_everything=True)
        cfg = yaml.safe_load((root / "instance" / "config.yaml").read_text())
        cfg["git"]["repoUrl"] = "git@github.com:other-org/fixture-instance.git"
        (root / "instance" / "config.yaml").write_text(yaml.safe_dump(cfg))
        proc = self.init(root)
        self.assertNotEqual(proc.returncode, 0,
                            "init scaffolded an instance repo owned by another org:\n"
                            + proc.stdout)
        # All three, or the operator cannot tell which of the three is the wrong one.
        for named in ("other-org", "fixture-org", "ghcr.io/fixture-org"):
            self.assertIn(named, proc.stderr, f"the refusal does not name {named}")

    def test_the_https_form_of_repo_url_is_parsed_for_its_owner_too(self):
        """`https://github.com/owner/repo.git` is what the GitHub UI hands an operator, and
        the guard above must read the owner out of that form as well or half the configs in
        the world would be checked against nothing."""
        root = self.scaffold(fill_everything=True)
        cfg = yaml.safe_load((root / "instance" / "config.yaml").read_text())
        cfg["git"]["repoUrl"] = "https://github.com/other-org/fixture-instance.git"
        (root / "instance" / "config.yaml").write_text(yaml.safe_dump(cfg))
        self.assertNotEqual(self.init(root).returncode, 0,
                            "the https:// form of repoUrl escaped the owner check")
        cfg["git"]["repoUrl"] = "https://github.com/fixture-org/fixture-instance"
        (root / "instance" / "config.yaml").write_text(yaml.safe_dump(cfg))
        proc = self.init(root)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        # the owner/repo read out of the https form is what the rendered tree carries
        self.assertIn("https://github.com/fixture-org/fixture-instance.git",
                      (root / "clusters" / "platform" / "config" / "orbitd" / "deployment.yaml").read_text())

    def test_an_uppercase_org_still_agrees_with_its_lowercase_registry(self):
        """ghcr.io rejects an uppercase path element, so a pushed image's owner is lowercase.
        The guard above has to lowercase the same way or an org displayed `FixtureOrg`
        could never write a registry line init accepts."""
        root = self.scaffold(fill_everything=True)
        cfg = yaml.safe_load((root / "instance" / "config.yaml").read_text())
        cfg["git"]["github_org"] = "FixtureOrg"
        # The repoUrl owner is the third spelling of the same org and is held to the same
        # rule — case-insensitively, so the display casing here is the point, not a typo.
        cfg["git"]["repoUrl"] = "git@github.com:FixtureOrg/fixture-instance.git"
        cfg["registry"] = "ghcr.io/fixtureorg"
        (root / "instance" / "config.yaml").write_text(yaml.safe_dump(cfg))
        proc = self.init(root)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


class AuthRefusedTest(unittest.TestCase):
    """The identity provider is retired: a config that still carries its `auth:` block
    is refused by name before anything is written, and Grafana signs in with its
    local admin now."""

    def fixture(self, config: dict = None) -> InitFixture:
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        merged = json.loads(json.dumps(BASE_CONFIG))
        for k, v in (config or {}).items():
            if isinstance(v, dict) and isinstance(merged.get(k), dict):
                merged[k].update(v)
            else:
                merged[k] = v
        return InitFixture(pathlib.Path(tmp), merged)

    def test_a_config_that_still_carries_auth_is_refused_by_name(self):
        fx = self.fixture({"auth": {"issuer": "https://auth.fixture.org"}})
        proc = fx.run()
        out = proc.stdout + proc.stderr
        self.assertNotEqual(proc.returncode, 0, "an auth: block was accepted")
        self.assertIn("auth:", out, "the refusal must name the retired block")
        self.assertIn("local admin", out,
                      "the refusal must say where Grafana signs in now")
        self.assertEqual(fx.files(), [], "a refused config still wrote files")

    def test_a_bucketless_orbit_cannot_declare_a_wired_loki_ruler(self):
        """`lokiRuler: wired` asserts wiring that lives INSIDE apps/loki.yaml, and a
        bucket-less orbit has that file pruned — so the declaration would let the generator
        render a `logs` trigger whose rule nothing can ever evaluate."""
        fx = self.fixture({"objectStorage": None, "lokiRuler": "wired"})
        proc = fx.run()
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("lokiRuler", proc.stderr)


class ReRenderTest(unittest.TestCase):
    """Every config-derived file is re-rendered from its staged source on every run.

    The break this whole class catches is a value that can be written only ONCE. Review
    round 1 reproduced three of them; each test here is one of those, run forwards.
    """

    def fixture(self, config: dict = None) -> InitFixture:
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        merged = json.loads(json.dumps(BASE_CONFIG))
        for k, v in (config or {}).items():
            if isinstance(v, dict) and isinstance(merged.get(k), dict):
                merged[k].update(v)
            else:
                merged[k] = v
        return InitFixture(pathlib.Path(tmp), merged)

    def assertOk(self, proc):
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc

    def test_a_bumped_orbitd_tag_reaches_the_deployment(self):
        """Bumping the pin on a later `make init` must rewrite the image, not leave
        the first run's tag frozen in clusters/."""
        fx = self.fixture({"coreRegistry": {"owner": "ghcr.io/core-org",
                                            "hooksTag": "main-1111111",
                                            "orbitdTag": "main-1111111"}})
        self.assertOk(fx.run())
        dep = fx.rendered("platform/config/orbitd/deployment.yaml")
        self.assertIn("orbitd:main-1111111", dep.read_text())

        fx.write_config({"coreRegistry": {"owner": "ghcr.io/core-org",
                                          "hooksTag": "main-1111111",
                                          "orbitdTag": "main-abcdef0"}})
        self.assertOk(fx.run())
        body = dep.read_text()
        self.assertIn("orbitd:main-abcdef0", body)
        self.assertNotIn("main-1111111", body, "the deployment still names the old tag")

    def test_orbitd_renders_unconditionally_with_readonly_root_and_default_class_pvc(self):
        """orbitd is not gated: a config with no extra flags still gets the namespace,
        the read-only RBAC, the PVC on the default class, and a read-only-root pod.
        Its Secrets come from SopsSecrets the desk applies, so no Secret producer
        is rendered beside them."""
        fx = self.fixture()
        self.assertOk(fx.run())
        for name in ("namespace.yaml", "rbac.yaml", "pvc.yaml",
                     "deployment.yaml", "service.yaml"):
            self.assertTrue(fx.rendered(f"platform/config/orbitd/{name}").exists(),
                            f"orbitd/{name} was not rendered")
        self.assertFalse(fx.rendered("platform/config/orbitd/externalsecret.yaml").exists(),
                         "orbitd still renders a Secret producer on the sops plane")
        pvc = yaml.safe_load(fx.rendered("platform/config/orbitd/pvc.yaml").read_text())
        self.assertEqual(pvc["spec"]["accessModes"], ["ReadWriteOnce"])
        self.assertNotIn("storageClassName", pvc["spec"],
                         "orbitd storage must use the instance's default storage class")
        self.assertEqual(pvc["spec"]["resources"]["requests"]["storage"], "1Gi")
        dep = yaml.safe_load(
            fx.rendered("platform/config/orbitd/deployment.yaml").read_text())
        self.assertEqual(dep["spec"]["replicas"], 1)
        self.assertEqual(dep["spec"]["strategy"]["type"], "Recreate")
        pod = dep["spec"]["template"]["spec"]
        self.assertEqual(pod["serviceAccountName"], "orbitd")
        self.assertTrue(pod["automountServiceAccountToken"])
        container = pod["containers"][0]
        self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
        self.assertEqual(container["securityContext"]["runAsUser"], 10001)
        self.assertEqual(container["resources"]["requests"]["memory"], "128Mi")
        self.assertEqual(container["resources"]["limits"]["memory"], "192Mi")
        self.assertEqual(container["readinessProbe"]["httpGet"]["path"], "/healthz")
        self.assertEqual(container["livenessProbe"]["httpGet"]["path"], "/healthz")
        self.assertIn("orbitd:main-1111111", container["image"])
        self.assertIn("https://orbit.fixture.org/mcp",
                      [e["value"] for e in container["env"] if e["name"] == "ORBITD_MCP_URL"])
        self.assertEqual(
            [e["value"] for e in container["env"] if e["name"] == "ORBITD_INSTANCE_DIR"],
            ["/var/lib/instance/current"])
        self.assertIn({"name": "instance", "mountPath": "/var/lib/instance",
                       "readOnly": True},
                      container["volumeMounts"])
        sync = [c for c in pod["containers"] if c["name"] == "git-sync"][0]
        self.assertEqual(sync["image"], "registry.k8s.io/git-sync/git-sync:v4.7.1")
        self.assertEqual(sync["securityContext"]["runAsUser"], 10001)
        self.assertEqual(sync["resources"]["requests"]["memory"], "32Mi")
        self.assertEqual(sync["resources"]["limits"]["memory"], "64Mi")
        self.assertIn("--repo=https://github.com/fixture-org/fixture-instance.git",
                      sync["args"])
        self.assertIn("--ref=main", sync["args"])
        self.assertIn("--period=60s", sync["args"])
        self.assertIn("--depth=0", sync["args"])
        self.assertIn("--root=/var/lib/instance", sync["args"])
        self.assertIn("--link=current", sync["args"])
        self.assertIn("--github-app-private-key-file=/etc/github-app/private-key.pem",
                      sync["args"])
        self.assertIn({"name": "instance", "emptyDir": {}}, pod["volumes"])
        ingress = [d for d in yaml.safe_load_all(
            fx.rendered("platform/config/orbitd/service.yaml").read_text())
                   if d and d.get("kind") == "Ingress"][0]
        self.assertEqual(ingress["spec"]["rules"][0]["host"], "orbit.fixture.org")
        self.assertEqual(ingress["spec"]["ingressClassName"], "traefik")
        self.assertNotIn("tls", ingress["spec"])

    def test_orbitd_carries_the_desk_ship_path(self):
        """The pod must be able to invoke the desk's ship scripts: env names the
        scripts read, mounts at those paths, the github-app and registry Secrets
        the sops plane seals, and a ClusterRole whose only write is
        deployments/patch."""
        fx = self.fixture()
        self.assertOk(fx.run())
        dep = yaml.safe_load(
            fx.rendered("platform/config/orbitd/deployment.yaml").read_text())
        pod = dep["spec"]["template"]["spec"]
        container = pod["containers"][0]
        self.assertEqual(container["name"], "orbitd")
        self.assertEqual(container["resources"]["limits"]["memory"], "192Mi")
        env = {e["name"]: e.get("value") for e in container["env"]}
        self.assertEqual(env["ORBIT_CONFIG_DIR"], "/etc/github-app")
        self.assertEqual(env["ORBIT_REGISTRY_TOKEN_FILE"],
                         "/etc/registry/ghcr-pull-token")
        self.assertEqual(env["ORBIT_INSTANCE_CONFIG"],
                         "/var/lib/instance/current/instance/config.yaml")
        self.assertEqual(env["ORBIT_ACTOR"], "orbitd")
        self.assertEqual(env["TMPDIR"], "/scratch")
        self.assertIn({"name": "github-app", "mountPath": "/etc/github-app",
                       "readOnly": True},
                      container["volumeMounts"])
        self.assertIn({"name": "registry", "mountPath": "/etc/registry",
                       "readOnly": True},
                      container["volumeMounts"])
        self.assertIn({"name": "scratch", "mountPath": "/scratch"},
                      container["volumeMounts"])
        self.assertIn({"name": "github-app",
                       "secret": {"secretName": "orbitd-github-app"}},
                      pod["volumes"])
        self.assertIn({"name": "registry",
                       "secret": {"secretName": "orbitd-registry"}},
                      pod["volumes"])
        self.assertIn({"name": "scratch",
                       "emptyDir": {"sizeLimit": "256Mi"}},
                      pod["volumes"])

        docs = [d for d in yaml.safe_load_all(
            fx.rendered("platform/config/orbitd/rbac.yaml").read_text()) if d]
        roles = [d for d in docs if d.get("kind") == "ClusterRole"]
        self.assertEqual({d["metadata"]["name"] for d in roles},
                         {"orbitd-readonly", "orbitd-operate", "orbitd-applier"})
        # orbitd-applier's writes are asserted by test_orbitd_applier_rbac_writes_exactly_the_applied_kinds.
        roles = [d for d in roles if d["metadata"]["name"] != "orbitd-applier"]
        operate = [d for d in roles if d["metadata"]["name"] == "orbitd-operate"][0]
        self.assertEqual(operate["rules"],
                         [{"apiGroups": ["apps"], "resources": ["deployments"],
                           "verbs": ["patch"]}])
        bindings = [d for d in docs if d.get("kind") == "ClusterRoleBinding"]
        operate_binding = [d for d in bindings
                           if d["metadata"]["name"] == "orbitd-operate"][0]
        self.assertEqual(operate_binding["roleRef"],
                         {"apiGroup": "rbac.authorization.k8s.io",
                          "kind": "ClusterRole", "name": "orbitd-operate"})
        self.assertEqual(operate_binding["subjects"],
                         [{"kind": "ServiceAccount", "name": "orbitd",
                           "namespace": "orbit"}])
        write_verbs = {"create", "update", "patch", "delete", "deletecollection", "*"}
        for role in roles:
            for rule in role.get("rules") or []:
                writes = write_verbs.intersection(rule.get("verbs") or [])
                if not writes:
                    continue
                self.assertEqual(role["metadata"]["name"], "orbitd-operate",
                                 f"{role['metadata']['name']} has write verbs {writes}")
                self.assertEqual(writes, {"patch"})
                self.assertEqual(rule.get("apiGroups"), ["apps"])
                self.assertEqual(rule.get("resources"), ["deployments"])
        readonly = [d for d in roles if d["metadata"]["name"] == "orbitd-readonly"][0]
        by_group: dict[str, set[str]] = {}
        for rule in readonly.get("rules") or []:
            self.assertEqual(rule.get("verbs"), ["get", "list", "watch"], rule)
            for group in rule.get("apiGroups") or []:
                by_group.setdefault(group, set()).update(rule.get("resources") or [])
        required = {
            "": {"services", "serviceaccounts", "persistentvolumeclaims", "configmaps",
                 "resourcequotas", "namespaces"},
            "apps": {"deployments"},
            "batch": {"cronjobs", "jobs"},
            "rbac.authorization.k8s.io": {"roles", "rolebindings"},
            "networking.k8s.io": {"ingresses", "networkpolicies"},
            "traefik.io": {"middlewares"},
            "monitoring.coreos.com": {"servicemonitors", "probes"},
            "postgresql.cnpg.io": {"scheduledbackups", "objectstores", "clusters"},
            "barmancloud.cnpg.io": {"objectstores"},
            "opentelemetry.io": {"instrumentations"},
        }
        for group, names in required.items():
            missing = names - by_group.get(group, set())
            self.assertFalse(missing, f"orbitd-readonly {group or 'core'} missing {sorted(missing)}")

    def test_orbitd_applier_rbac_writes_exactly_the_applied_kinds(self):
        """The applier ClusterRole (PHASE5-DESIGN §2) may create/patch/delete exactly the
        tenant kinds minus configmaps: no RBAC kinds, no Secrets, no operator kinds,
        no update/deletecollection/wildcards, bound to the orbitd ServiceAccount.
        ConfigMap write verbs come only from namespaced Roles: the inventory in
        `orbit`, the dashboards in `monitoring`, and (while the logs feature is on) the
        monitors' ruler ConfigMap in `loki`."""
        fx = self.fixture({"objectStorage": {"provider": "hetzner", "endpoint": "https://fsn1.example.com",
                                             "buckets": {"platform": "fixture-platform"}}})
        self.assertOk(fx.run())
        # with logs off there is no `loki` namespace, so no Role is rendered into it
        bare = self.fixture()
        self.assertOk(bare.run())
        bare_roles = {d["metadata"]["namespace"] for d in yaml.safe_load_all(
            bare.rendered("platform/config/orbitd/rbac.yaml").read_text()) if d and d.get("kind") in ("Role", "RoleBinding")}
        self.assertEqual(bare_roles, {"orbit", "monitoring"})
        docs = [d for d in yaml.safe_load_all(
            fx.rendered("platform/config/orbitd/rbac.yaml").read_text()) if d]
        roles = [d for d in docs if d.get("kind") == "ClusterRole"
                 and d["metadata"]["name"] == "orbitd-applier"]
        self.assertEqual(len(roles), 1, "one orbitd-applier ClusterRole")
        applier = roles[0]
        granted: set = set()
        for rule in applier["rules"]:
            self.assertEqual(rule["verbs"], ["create", "patch", "delete"], rule)
            self.assertEqual(len(rule["apiGroups"]), 1, rule)
            for resource in rule["resources"]:
                self.assertNotIn(resource, ("*", "secrets", "roles", "rolebindings",
                                            "clusterroles", "clusterrolebindings"), rule)
                granted.add((rule["apiGroups"][0], resource))
        expected = {
            ("", "namespaces"), ("", "serviceaccounts"),
            ("", "resourcequotas"), ("", "persistentvolumeclaims"), ("", "services"),
            ("apps", "deployments"),
            ("batch", "jobs"), ("batch", "cronjobs"),
            ("networking.k8s.io", "networkpolicies"), ("networking.k8s.io", "ingresses"),
            ("traefik.io", "middlewares"),
            ("cert-manager.io", "certificates"),
            ("opentelemetry.io", "instrumentations"),
            ("postgresql.cnpg.io", "clusters"), ("postgresql.cnpg.io", "scheduledbackups"),
            ("barmancloud.cnpg.io", "objectstores"),
            ("monitoring.coreos.com", "servicemonitors"), ("monitoring.coreos.com", "probes"),
            ("monitoring.coreos.com", "prometheusrules"),
        }
        self.assertEqual(granted, expected)
        self.assertNotIn(("external-secrets.io", "externalsecrets"), granted,
                         "the applier still grants the retired operator's kind")
        binding = [d for d in docs if d.get("kind") == "ClusterRoleBinding"
                   and d["metadata"]["name"] == "orbitd-applier"]
        self.assertEqual(len(binding), 1)
        self.assertEqual(binding[0]["roleRef"],
                         {"apiGroup": "rbac.authorization.k8s.io",
                          "kind": "ClusterRole", "name": "orbitd-applier"})
        self.assertEqual(binding[0]["subjects"],
                         [{"kind": "ServiceAccount", "name": "orbitd", "namespace": "orbit"}])
        # No programmatic comparison against services/orbitd/orbitd/applier.py here:
        # the module still lists the retired operator kind while its own removal
        # lands in parallel, so the two cannot agree until that half is in. The
        # module-side exactness test holds the pair from that side.
        # ConfigMap write verbs only through Roles
        write_verbs = {"create", "update", "patch", "delete", "deletecollection", "*"}
        for d in docs:
            if d.get("kind") == "ClusterRole":
                for rule in d.get("rules") or []:
                    if "configmaps" in (rule.get("resources") or []):
                        self.assertFalse(write_verbs & set(rule["verbs"]),
                                         f"{d['metadata']['name']} writes configmaps cluster-wide")
        roles = {d["metadata"]["namespace"]: d for d in docs if d.get("kind") == "Role"}
        self.assertEqual(set(roles), {"orbit", "monitoring", "loki"})
        for ns, role in roles.items():
            self.assertEqual(len(role["rules"]), 1, role)
            self.assertEqual(role["rules"][0]["apiGroups"], [""])
            self.assertEqual(role["rules"][0]["resources"], ["configmaps"])
        self.assertEqual(roles["orbit"]["rules"][0]["verbs"], ["get", "list", "create", "patch"])
        self.assertEqual(roles["monitoring"]["rules"][0]["verbs"], ["create", "patch", "delete"])
        self.assertEqual(roles["loki"]["rules"][0]["verbs"], ["create", "patch", "delete"])
        role_bindings = {d["metadata"]["namespace"]: d for d in docs if d.get("kind") == "RoleBinding"}
        self.assertEqual(set(role_bindings), {"orbit", "monitoring", "loki"})
        for ns, rb in role_bindings.items():
            self.assertEqual(rb["roleRef"], {"apiGroup": "rbac.authorization.k8s.io",
                                             "kind": "Role", "name": roles[ns]["metadata"]["name"]})
            self.assertEqual(rb["subjects"],
                             [{"kind": "ServiceAccount", "name": "orbitd", "namespace": "orbit"}])

    def test_two_runs_with_the_same_config_produce_no_diff(self):
        """Re-rendering must not make `make init` churn the tree. If a second run moved a
        byte, every re-run would show up as a spurious commit and the operator would learn
        to stop running it — which is how the one-way behaviour survived in the first
        place."""
        fx = self.fixture()
        self.assertOk(fx.run())
        first = fx.snapshot()
        self.assertOk(fx.run())
        self.assertEqual(first, fx.snapshot(), "a second init with unchanged config "
                                               "changed the generated tree")

    def test_the_intake_door_tracks_its_attestation(self):
        """`intakeIngress: wired` is the instance ATTESTING that the intake's public door
        exists. Scaffolding the door unconditionally made the attestation meaningless — a
        fresh org got a public webhook Ingress without ever making the exposure decision
        (review round 1). Both directions, because a door that cannot be closed again is
        the same defect wearing the other face."""
        door = ["platform/config/orbit-intake/ingress.yaml",
                "platform/config/orbit-intake/netpol.yaml"]
        fx = self.fixture()
        self.assertOk(fx.run())
        for rel in door:
            self.assertFalse(fx.rendered(rel).exists(),
                             f"{rel} was scaffolded without intakeIngress: wired")

        fx.write_config({"intakeIngress": "wired"})
        self.assertOk(fx.run())
        for rel in door:
            self.assertTrue(fx.rendered(rel).exists(), f"{rel} missing though wired")
        self.assertIn("intake.fixture.org",
                      fx.rendered(door[0]).read_text(), "the door's host is unresolved")

        fx.write_config({"intakeIngress": None})
        proc = self.assertOk(fx.run())
        for rel in door:
            self.assertFalse(fx.rendered(rel).exists(), f"{rel} survived the attestation "
                                                        f"being withdrawn")
        self.assertIn("intakeIngress is not `wired`", proc.stdout)

    def test_a_typo_in_the_intake_flag_is_refused(self):
        """The lokiRuler lesson: `wired` is the only accepted value, so a typo is refused
        at generation rather than silently read as "not wired"."""
        fx = self.fixture({"intakeIngress": "true"})
        proc = fx.run()
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("intakeIngress", proc.stderr)

    def test_the_bucket_gate_closes_and_reopens(self):
        """Adding object storage to an existing orbit used to mean restoring files from
        the template by hand, because init could only delete. Now it is a config edit."""
        bucketed = {"provider": "hetzner",
                    "endpoint": "https://fsn1.your-objectstorage.com",
                    "buckets": {"platform": "fixture-platform"}}
        fx = self.fixture({"objectStorage": bucketed})
        self.assertOk(fx.run())
        for rel in ("platform/apps/loki.yaml", "platform/apps/alloy.yaml",
                    "platform/apps/tempo.yaml"):
            self.assertTrue(fx.rendered(rel).exists(), f"{rel} missing with a bucket")

        fx.write_config({"objectStorage": None})
        self.assertOk(fx.run())
        for rel in ("platform/apps/loki.yaml", "platform/apps/alloy.yaml",
                    "platform/apps/tempo.yaml"):
            self.assertFalse(fx.rendered(rel).exists(),
                             f"{rel} survived the bucket being removed")

        fx.write_config({"objectStorage": bucketed})
        self.assertOk(fx.run())
        self.assertTrue(fx.rendered("platform/apps/loki.yaml").exists(),
                        "Loki did not come back when the bucket did")

    def test_an_edit_to_a_rendered_file_is_reverted_and_the_source_wins(self):
        """The cost of this design, asserted so it is a decision and not a surprise:
        clusters/ is OUTPUT. A hand-edit there is reverted on the next run, and the same
        edit made in provisioning/ survives — which is where the banner in every rendered
        file's first line sends the reader."""
        fx = self.fixture()
        self.assertOk(fx.run())
        rel = "platform/config/orbitd/namespace.yaml"
        fx.rendered(rel).write_text(fx.rendered(rel).read_text() + "\n# hand edit\n")
        self.assertOk(fx.run())
        self.assertNotIn("# hand edit", fx.rendered(rel).read_text())

        staged = fx.root / "provisioning" / "clusters" / rel
        staged.write_text(staged.read_text() + "\n# staged edit\n")
        self.assertOk(fx.run())
        self.assertIn("# staged edit", fx.rendered(rel).read_text())

    def test_every_rendered_file_says_it_is_generated(self):
        """A reader who opens a file under clusters/ must be told, in the file, that their
        edit will not survive. Without this the revert above is a trap rather than a rule."""
        fx = self.fixture({"intakeIngress": "wired"})
        self.assertOk(fx.run())
        staged = (fx.root / "provisioning" / "clusters")
        for src in staged.rglob("*.yaml"):
            rel = str(src.relative_to(staged))
            if rel.startswith("platform/crds/"):
                # Verbatim chart CRDs. A GENERATED banner would make them not the
                # chart's own files; the README beside them is the provenance.
                continue
            self.assertTrue(src.read_text().startswith("# GENERATED into clusters/"),
                            f"{rel} carries no GENERATED banner")


class CoreSyncTest(unittest.TestCase):
    """The delivery half. `template/` seeds a NEW org and nothing else, so what must also
    reach an org that already exists is mapped in the manifest: today that is
    `template/provisioning -> provisioning`, a source path and a destination path that
    differ. This drives the real script over the real manifest.

    The Actions build pipeline was the first mapped line
    (`template/.github/workflows/build-service.yml -> .github/workflows/build-service.yml`)
    and left on 2026-10-06 (owner's rule: nothing runs in GitHub Actions). An instance that
    vendored it loses its copy on the next sync."""

    CORE = pathlib.Path(__file__).resolve().parent.parent
    VENDORED = "provisioning/features.yaml"
    RETIRED_PIPELINE = ".github/workflows/build-service.yml"

    def core_clone_source(self) -> pathlib.Path:
        """A git repo whose content is the working tree — so the test proves what is in the
        tree now, not what was committed. core-sync.sh clones by ref, so it needs one."""
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        core = tmp / "core"
        shutil.copytree(self.CORE, core,
                        ignore=shutil.ignore_patterns(".git", "__pycache__"))
        git = ["git", "-c", "user.email=t@t", "-c", "user.name=t",
               "-c", "commit.gpgsign=false"]
        subprocess.run(git + ["init", "-q", "-b", "vendor-test"], cwd=core, check=True)
        subprocess.run(git + ["add", "-A"], cwd=core, check=True,
                       capture_output=True)
        subprocess.run(git + ["commit", "-qm", "tree under test"], cwd=core, check=True,
                       capture_output=True)
        return core

    def test_the_real_manifest_vendors_and_drops_the_retired_pipeline(self):
        core = self.core_clone_source()
        inst = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, inst, ignore_errors=True)
        (inst / "orbit-core.lock").write_text(f"url: {core}\nref: vendor-test\n")
        sync = str(self.CORE / "scripts" / "core-sync.sh")
        # An instance as the previous pin left it: its manifest lists the pipeline and the
        # file is there. Beside it, a workflow of the org's own that core never listed.
        manifest = (self.CORE / "core-manifest.txt").read_text()
        self.assertNotIn(".github", manifest, "core-manifest.txt still vendors something under .github/")
        (inst / "core-manifest.txt").write_text(
            manifest + "template/" + self.RETIRED_PIPELINE + " -> " + self.RETIRED_PIPELINE + "\n")
        old = inst / self.RETIRED_PIPELINE
        old.parent.mkdir(parents=True)
        old.write_text("name: build-service\n")
        own = inst / ".github" / "workflows" / "own.yml"
        own.write_text("name: own\n")

        before = subprocess.run(["bash", sync, "--check"], cwd=inst, capture_output=True, text=True)
        self.assertNotEqual(before.returncode, 0, before.stdout)
        self.assertIn("DRIFT: " + self.RETIRED_PIPELINE + " leftover", before.stdout)

        proc = subprocess.run(["bash", sync], cwd=inst, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse(old.exists(), "the retired pipeline survived the sync")
        self.assertEqual(own.read_text(), "name: own\n", "a file core never listed was touched")
        landed = inst / self.VENDORED
        self.assertTrue(landed.exists(),
                        f"{self.VENDORED} did not vendor: " + proc.stdout + proc.stderr)
        self.assertEqual(landed.read_text(),
                         (self.CORE / "template" / self.VENDORED).read_text())

        check = subprocess.run(["bash", sync, "--check"], cwd=inst,
                               capture_output=True, text=True)
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)

        # ...and `make core-check` semantics: a local edit to a vendored file is DRIFT,
        # reported against the instance path, not the core one.
        landed.write_text(landed.read_text() + "\n# local edit\n")
        drift = subprocess.run(["bash", sync, "--check"], cwd=inst,
                               capture_output=True, text=True)
        self.assertNotEqual(drift.returncode, 0, drift.stdout)
        self.assertIn("DRIFT: provisioning", drift.stdout)


def _netpol_named(docs, name: str) -> dict:
    for doc in docs:
        if isinstance(doc, dict) and (doc.get("metadata") or {}).get("name") == name:
            return doc
    raise AssertionError(f"no NetworkPolicy named {name}")


def _orbit_ingress_ports(doc: dict) -> set:
    """Ports on ingress rules whose from-list admits namespace orbit."""
    ports = set()
    for rule in (doc.get("spec") or {}).get("ingress") or []:
        if not any(
            ((src.get("namespaceSelector") or {}).get("matchLabels") or {})
            .get("kubernetes.io/metadata.name") == "orbit"
            for src in (rule.get("from") or [])
            if isinstance(src, dict)
        ):
            continue
        for port in rule.get("ports") or []:
            ports.add(port.get("port"))
    return ports


class OrbitViewerNetpolTest(unittest.TestCase):
    """orbitd (ns orbit) must be admitted to Prometheus, Alertmanager, Loki and Tempo.

    The monitoring policy is seeded from template/clusters/ and is not re-rendered;
    Loki and Tempo policies are staged under provisioning/ and gated on a platform
    bucket, so they are asserted on the rendered tree.
    """

    MONITORING = (TEMPLATE / "provisioning" / "clusters" / "platform" / "config" / "monitoring"
                  / "netpol.yaml")

    def fixture(self) -> InitFixture:
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        merged = json.loads(json.dumps(BASE_CONFIG))
        merged["objectStorage"] = {
            "provider": "hetzner",
            "endpoint": "https://fsn1.your-objectstorage.com",
            "buckets": {"platform": "fixture-platform"},
        }
        return InitFixture(pathlib.Path(tmp), merged)

    def test_monitoring_netpol_admits_orbit_to_prometheus_and_alertmanager(self):
        docs = list(yaml.safe_load_all(self.MONITORING.read_text()))
        prometheus = _netpol_named(docs, "prometheus-ingress")
        alertmanager = _netpol_named(docs, "alertmanager-ingress")
        self.assertIn(9090, _orbit_ingress_ports(prometheus),
                      "prometheus-ingress does not admit ns orbit on :9090")
        self.assertIn(9093, _orbit_ingress_ports(alertmanager),
                      "alertmanager-ingress does not admit ns orbit on :9093")

    def test_rendered_loki_and_tempo_netpols_admit_orbit(self):
        fx = self.fixture()
        proc = fx.run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        loki = yaml.safe_load(
            fx.rendered("platform/config/loki/netpol.yaml").read_text())
        tempo = yaml.safe_load(
            fx.rendered("platform/config/tempo/netpol.yaml").read_text())
        self.assertEqual(loki["metadata"]["name"], "loki-ingress")
        self.assertEqual(tempo["metadata"]["name"], "tempo-ingress")
        self.assertIn(3100, _orbit_ingress_ports(loki),
                      "loki-ingress does not admit ns orbit on :3100")
        self.assertIn(3200, _orbit_ingress_ports(tempo),
                      "tempo-ingress does not admit ns orbit on :3200")


class TraefikCrdsTest(unittest.TestCase):
    """The template ships Traefik's own CRDs as a wave-0 Application.

    skipCrds on the chart Application stops a sync from downgrading the vendored
    Gateway API bundle; without a sibling Application the chart's tlsStore object
    has no CRD and a fresh cluster never brings ingress up.
    """

    def fixture(self, config: dict = None) -> InitFixture:
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        merged = json.loads(json.dumps(BASE_CONFIG))
        for k, v in (config or {}).items():
            if isinstance(v, dict) and isinstance(merged.get(k), dict):
                merged[k].update(v)
            else:
                merged[k] = v
        return InitFixture(pathlib.Path(tmp), merged)

    def assertOk(self, proc: subprocess.CompletedProcess):
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc

    def test_traefik_renders_both_the_chart_and_the_crds_applications(self):
        fx = self.fixture()
        self.assertOk(fx.run())
        docs = [d for d in yaml.safe_load_all(
            fx.rendered("platform/apps/traefik.yaml").read_text()) if d]
        names = [d["metadata"]["name"] for d in docs]
        self.assertEqual(names, ["traefik-crds", "traefik"],
                         "the rendered traefik file must carry both Applications")
        crds, chart = docs
        self.assertEqual(crds["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"],
                         "0")
        self.assertEqual(crds["spec"]["source"]["path"],
                         "clusters/platform/crds/traefik")
        self.assertEqual(crds["spec"]["source"]["repoURL"],
                         BASE_CONFIG["git"]["repoUrl"])
        self.assertEqual(crds["spec"]["source"]["targetRevision"],
                         BASE_CONFIG["git"]["branch"])
        self.assertEqual(chart["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"],
                         "1")
        self.assertTrue(chart["spec"]["source"]["helm"]["skipCrds"])
        crd_dir = fx.rendered("platform/crds/traefik")
        landed = sorted(p.name for p in crd_dir.glob("traefik.io_*.yaml"))
        self.assertEqual(len(landed), 10, landed)
        for name in landed:
            doc = yaml.safe_load((crd_dir / name).read_text())
            self.assertEqual(doc["kind"], "CustomResourceDefinition", name)
            self.assertTrue(doc["metadata"]["name"].endswith(".traefik.io"), name)


class MonitoringProbesTest(unittest.TestCase):
    """Extra static blackbox targets come from monitoring.probes, not hand-edits."""

    TEMPLATE_HTTP_2XX = [
        "https://grafana.fixture.org/api/health",
        "https://orbit.fixture.org/healthz",
    ]

    def fixture(self, config: dict = None) -> InitFixture:
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        merged = json.loads(json.dumps(BASE_CONFIG))
        for k, v in (config or {}).items():
            if isinstance(v, dict) and isinstance(merged.get(k), dict):
                merged[k].update(v)
            else:
                merged[k] = v
        return InitFixture(pathlib.Path(tmp), merged)

    def assertOk(self, proc: subprocess.CompletedProcess):
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc

    def targets_by_module(self, fx: InitFixture) -> dict:
        out = {}
        for doc in yaml.safe_load_all(
                fx.rendered("platform/monitoring/probes.yaml").read_text()):
            if not doc or doc.get("kind") != "Probe":
                continue
            mod = doc["spec"]["module"]
            static = doc["spec"]["targets"]["staticConfig"]["static"]
            out.setdefault(mod, []).extend(static)
        return out

    def test_two_configured_entries_render_under_the_named_modules(self):
        fx = self.fixture({"monitoring": {"probes": [
            {"url": "https://app.{{domains.primary}}/v1", "module": "http_2xx"},
            {"url": "https://app.{{domains.dev}}", "module": "http_401"},
        ]}})
        self.assertOk(fx.run())
        by_mod = self.targets_by_module(fx)
        self.assertIn("https://app.fixture.org/v1", by_mod["http_2xx"])
        self.assertIn("https://app.dev.fixture.org", by_mod["http_401"])
        for url in self.TEMPLATE_HTTP_2XX:
            self.assertIn(url, by_mod["http_2xx"],
                          f"template probe {url} was dropped when extras rendered")

    def test_an_empty_list_renders_the_template_probes_only(self):
        fx = self.fixture({"monitoring": {"probes": []}})
        self.assertOk(fx.run())
        by_mod = self.targets_by_module(fx)
        self.assertEqual(set(by_mod), {"http_2xx"})
        self.assertEqual(by_mod["http_2xx"], self.TEMPLATE_HTTP_2XX)
        default = self.fixture()
        self.assertOk(default.run())
        self.assertEqual(
            default.rendered("platform/monitoring/probes.yaml").read_text(),
            fx.rendered("platform/monitoring/probes.yaml").read_text(),
            "an empty monitoring.probes list rewrote the template probes file")


class OrbitdApplySwitchTest(unittest.TestCase):
    """orbitd's apply switch comes from instance config, defaulting to inert.

    The Deployment's ORBITD_APPLY / ORBITD_RECONCILE_PERIOD_S env is rendered
    from the top-level `orbitd:` map; an org that never declares it gets `off`
    and 180, so the applier stays a shadow until deliberately turned on."""

    def fixture(self, config: dict = None) -> InitFixture:
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        merged = json.loads(json.dumps(BASE_CONFIG))
        for k, v in (config or {}).items():
            if isinstance(v, dict) and isinstance(merged.get(k), dict):
                merged[k].update(v)
            else:
                merged[k] = v
        return InitFixture(pathlib.Path(tmp), merged)

    def assertOk(self, proc: subprocess.CompletedProcess):
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc

    def orbitd_env_lines(self, fx: InitFixture) -> str:
        """The rendered orbitd Deployment, still a valid manifest carrying the switch.

        Read as TEXT, not parsed YAML: the spec pins the env lines quoted
        (`value: "off"`), and YAML 1.1 parses a bare `off`/`on` as a boolean —
        so a parsed comparison would assert `False`/`True` and hide the very
        spelling the cluster will receive. The parse below only proves the
        file is still a Deployment carrying both env names."""
        body = fx.rendered("platform/config/orbitd/deployment.yaml").read_text()
        dep = yaml.safe_load(body)
        container = dep["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(container["name"], "orbitd")
        self.assertIn("ORBITD_APPLY", {e["name"] for e in container["env"]})
        self.assertIn("ORBITD_RECONCILE_PERIOD_S",
                      {e["name"] for e in container["env"]})
        self.assertIn("ORBITD_APPLY_PAIRS", {e["name"] for e in container["env"]})
        return body

    def write_catalog(self, fx: InitFixture, projects: list) -> None:
        catalog = fx.root / "catalog"
        catalog.mkdir(exist_ok=True)
        (catalog / "projects.yaml").write_text(
            yaml.safe_dump({"version": 3, "projects": projects}, sort_keys=False))

    def test_absent_keys_render_off_and_180(self):
        """The break: the switch stops defaulting to inert — a fresh org gets
        an applier that is live, or init exits on a key the org never set."""
        fx = self.fixture()
        self.assertOk(fx.run())
        body = self.orbitd_env_lines(fx)
        self.assertIn('- {name: ORBITD_APPLY, value: "off"}', body)
        self.assertIn('- {name: ORBITD_RECONCILE_PERIOD_S, value: "180"}', body)
        self.assertIn('- {name: ORBITD_APPLY_PAIRS, value: ""}', body)

    def test_on_and_60_render_as_given(self):
        """The break: declared values never reach the pod — hardcoded
        defaults, or token names the fill does not resolve."""
        fx = self.fixture({"orbitd": {"applyMode": "on", "reconcilePeriodS": 60}})
        self.assertOk(fx.run())
        body = self.orbitd_env_lines(fx)
        self.assertIn('- {name: ORBITD_APPLY, value: "on"}', body)
        self.assertIn('- {name: ORBITD_RECONCILE_PERIOD_S, value: "60"}', body)

    def test_unquoted_on_renders_quoted_on(self):
        """The break: `applyMode: on` written unquoted parses as YAML True
        and init exits naming orbitd.applyMode instead of shipping mode on."""
        fx = self.fixture({"orbitd": {"applyMode": True, "reconcilePeriodS": 60}})
        self.assertOk(fx.run())
        body = self.orbitd_env_lines(fx)
        self.assertIn('- {name: ORBITD_APPLY, value: "on"}', body)

    def test_unquoted_off_renders_quoted_off(self):
        """The break: `applyMode: off` written unquoted parses as YAML False
        and init exits instead of rendering the inert default."""
        fx = self.fixture({"orbitd": {"applyMode": False}})
        self.assertOk(fx.run())
        body = self.orbitd_env_lines(fx)
        self.assertIn('- {name: ORBITD_APPLY, value: "off"}', body)

    def test_an_invalid_mode_fails_naming_the_key(self):
        """The break: validation is dropped and a typo'd mode renders into
        the pod, where orbitd itself must refuse it at boot — or renders
        nothing and leaves an UNRESOLVED token behind."""
        fx = self.fixture({"orbitd": {"applyMode": "sometimes"}})
        proc = fx.run()
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("orbitd.applyMode", proc.stderr)

    def test_apply_pairs_render_comma_joined_when_in_the_catalog(self):
        """The break: applyPairs never reach ORBITD_APPLY_PAIRS, or a listed
        pair is not checked against the catalog."""
        fx = self.fixture({"orbitd": {"applyPairs": ["demo/dev", "demo/prod"]}})
        self.write_catalog(fx, [{
            "id": "demo",
            "environments": {"prod": {}, "dev": {"enabled": True}},
        }])
        self.assertOk(fx.run())
        body = self.orbitd_env_lines(fx)
        self.assertIn('- {name: ORBITD_APPLY_PAIRS, value: "demo/dev,demo/prod"}', body)

    def test_an_unknown_apply_pair_fails_naming_the_key(self):
        """The break: a pair the catalog does not declare is rendered into
        the pod, or the refusal does not name orbitd.applyPairs."""
        fx = self.fixture({"orbitd": {"applyPairs": ["ghost/dev"]}})
        self.write_catalog(fx, [{
            "id": "demo",
            "environments": {"prod": {}, "dev": {"enabled": True}},
        }])
        proc = fx.run()
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("orbitd.applyPairs", proc.stderr)
        self.assertIn("ghost/dev", proc.stderr)


class SopsPlaneTest(unittest.TestCase):
    """sops is the only secret plane: init renders no Secret producer of its own,
    and the next init removes what the retired plane rendered.

    Every test here runs the real entry point over a fixture instance and asserts
    the rendered tree — exit codes, documents, files — never the script's text.
    """

    OLD_PRODUCER_KINDS = {"ExternalSecret", "ClusterSecretStore", "SecretStore"}

    BUCKET = {"provider": "hetzner", "endpoint": "https://fsn1.your-objectstorage.com",
              "buckets": {"platform": "fixtureinst-platform"}}

    def fixture(self, config: dict = None) -> InitFixture:
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        merged = json.loads(json.dumps(BASE_CONFIG))
        merged["objectStorage"] = json.loads(json.dumps(self.BUCKET))
        for k, v in (config or {}).items():
            if isinstance(v, dict) and isinstance(merged.get(k), dict):
                merged[k].update(v)
            else:
                merged[k] = v
        return InitFixture(pathlib.Path(tmp), merged)

    def assertOk(self, proc: subprocess.CompletedProcess):
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc

    def test_sops_initializes(self):
        self.assertOk(self.fixture().run())

    def test_a_missing_provider_initializes_as_sops(self):
        fx = self.fixture()
        cfg = yaml.safe_load((fx.root / "instance" / "config.yaml").read_text())
        del cfg["secrets"]["provider"]
        (fx.root / "instance" / "config.yaml").write_text(yaml.safe_dump(cfg))
        self.assertOk(fx.run())

    def test_any_other_provider_is_refused_by_name_and_nothing_is_written(self):
        """The refused value is named, the one plane left is named, and the
        refusal happens before a byte moves — a half-scaffolded tree is worse
        than none."""
        for value in ("infisical", "infisical-cloud", "vault", "in-cluster"):
            with self.subTest(value=value):
                fx = self.fixture({"secrets": {"provider": value}})
                proc = fx.run()
                out = proc.stdout + proc.stderr
                self.assertNotEqual(proc.returncode, 0, f"{value!r} was accepted")
                self.assertIn("secrets.provider", out)
                self.assertIn("sops", out, "the refusal must name the one plane left")
                self.assertEqual(fx.files(), [],
                                 f"{value!r} was refused but init wrote files")

    def test_a_fresh_init_renders_no_secret_producer(self):
        fx = self.fixture()
        self.assertOk(fx.run())
        leaks = {f: k for f, k in fx.kinds().items()
                 if self.OLD_PRODUCER_KINDS & set(k)}
        self.assertEqual(leaks, {})
        for rel in _INIT_MODULE.RETIRED_SECRET_PLANE:
            self.assertFalse(fx.rendered(rel).exists(),
                             f"a fresh init rendered retired {rel}")

    def test_the_secrets_app_still_renders(self):
        fx = self.fixture()
        self.assertOk(fx.run())
        self.assertTrue(fx.rendered("platform/apps/secrets.yaml").exists(),
                        "the sops secrets app is the plane now and must render")

    def test_mixed_files_keep_only_their_other_documents(self):
        """Files that mixed producer documents with other objects keep the rest."""
        fx = self.fixture()
        self.assertOk(fx.run())
        kinds = fx.kinds()
        self.assertEqual(kinds["platform/config/monitoring/secrets.yaml"],
                         ["Namespace"])
        pc = yaml.safe_load(fx.rendered("platform/apps/platform-config.yaml").read_text())
        self.assertNotIn("exclude", pc["spec"]["source"]["directory"])

    def test_the_next_init_removes_what_the_old_plane_rendered(self):
        """An instance that rendered the retired files loses exactly those on its
        next init: removals reported by path, a hand-written file beside them
        surviving, emptied directories gone."""
        retired = _INIT_MODULE.RETIRED_SECRET_PLANE
        self.assertIn("platform/config/external-secrets/stores.yaml", retired)
        self.assertIn("platform/apps/infisical.yaml", retired)
        fx = self.fixture()
        for rel in retired:
            p = fx.rendered(rel)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("apiVersion: v1\nkind: ConfigMap\nmetadata: {name: left-over}\n")
        hand = fx.rendered("platform/config/team-notes/keep.yaml")
        hand.parent.mkdir(parents=True, exist_ok=True)
        hand.write_text("apiVersion: v1\nkind: ConfigMap\nmetadata: {name: keep}\n")
        proc = self.assertOk(fx.run())
        left = [rel for rel in retired if fx.rendered(rel).exists()]
        self.assertEqual(left, [], "init kept files the old plane rendered")
        for d in ("infisical", "external-secrets"):
            self.assertFalse(fx.rendered(f"platform/config/{d}").exists(),
                             f"empty platform/config/{d} left behind")
        self.assertTrue(hand.exists(), "init deleted a hand-written file")
        self.assertIn("external-secrets/stores.yaml", proc.stdout + proc.stderr,
                      "a removal must be reported by path")

    def test_a_hand_written_file_in_a_retired_directory_survives(self):
        """The retired list names files, never directories: init must not delete
        a file it merely did not produce."""
        fx = self.fixture()
        mine = fx.rendered("platform/config/external-secrets/mine.yaml")
        mine.parent.mkdir(parents=True, exist_ok=True)
        mine.write_text("apiVersion: v1\nkind: ConfigMap\nmetadata: {name: mine}\n")
        self.assertOk(fx.run())
        self.assertTrue(mine.exists(), "init deleted a file it never rendered")

    def test_a_third_environment_is_refused_and_nothing_is_written(self):
        """Exactly dev and prod, no third: an environment nothing reads is a place
        for a supplied value to be written to by mistake and never noticed."""
        fx = self.fixture({"secrets": {"environments": ["dev", "staging", "prod"]}})
        proc = fx.run()
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("secrets.environments", proc.stdout + proc.stderr)
        self.assertEqual(fx.files(), [],
                         "a refused environment set still wrote files")

    def test_dropping_an_environment_is_refused_too(self):
        fx = self.fixture({"secrets": {"environments": ["prod"]}})
        self.assertNotEqual(fx.run().returncode, 0)

    def test_the_order_written_in_config_does_not_matter(self):
        """A set, not a sequence: an operator who writes them the other way round
        has not made a mistake."""
        fx = self.fixture({"secrets": {"environments": ["prod", "dev"]}})
        self.assertOk(fx.run())



class SecretsFolderTest(unittest.TestCase):
    """The `secrets` Application (the sops operator's SopsSecrets) renders for every
    provider, because platform-made credentials such as the Alertmanager webhook token are
    sealed there on any plane. Its folder must therefore exist from the first init, or the
    platform plan fails to render the app ('clusters/platform/secrets is not a directory')."""

    def fixture(self, provider: str) -> InitFixture:
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        cfg = json.loads(json.dumps(BASE_CONFIG))
        cfg["secrets"]["provider"] = provider
        return InitFixture(tmp, cfg)

    def test_every_provider_gets_the_folder_and_the_app(self):
        for provider in ("sops",):
            fx = self.fixture(provider)
            proc = fx.run()
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            folder = fx.rendered("platform/secrets")
            self.assertTrue(folder.is_dir(), provider)
            # git keeps no empty folder: a placeholder file keeps it in a fresh clone
            self.assertTrue((folder / ".gitkeep").is_file(), provider)
            self.assertTrue(fx.rendered("platform/apps/secrets.yaml").is_file(), provider)
            self.assertEqual(sorted(folder.rglob("*.yaml")), [], provider)

    def test_a_symlinked_secrets_folder_is_refused_and_nothing_outside_is_written(self):
        fx = self.fixture("sops")
        outside = fx.root.parent / (fx.root.name + "-outside")
        outside.mkdir()
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        (outside / "keep.yaml").write_text("keep: 1\n")
        link = fx.rendered("platform/secrets")
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(outside, target_is_directory=True)
        proc = fx.run()
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("platform/secrets", proc.stdout + proc.stderr)
        self.assertEqual(sorted(p.name for p in outside.iterdir()), ["keep.yaml"])

    def test_a_symlinked_placeholder_is_refused_and_its_target_not_created(self):
        fx = self.fixture("sops")
        target = fx.root.parent / (fx.root.name + "-dangling-target")
        self.addCleanup(lambda: target.exists() and target.unlink())
        folder = fx.rendered("platform/secrets")
        folder.mkdir(parents=True)
        (folder / ".gitkeep").symlink_to(target)
        proc = fx.run()
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertFalse(target.exists())

    def test_init_never_touches_sealed_files_already_there(self):
        fx = self.fixture("sops")
        sealed = fx.rendered("platform/secrets/orbit/orbit-alert-webhook.yaml")
        sealed.parent.mkdir(parents=True)
        sealed.write_text("sealed: ENC[AES256_GCM,data:abc]\n")
        proc = fx.run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(sealed.read_text(), "sealed: ENC[AES256_GCM,data:abc]\n")
        proc = fx.run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(sealed.read_text(), "sealed: ENC[AES256_GCM,data:abc]\n")


class PlatformRulesStagedTest(unittest.TestCase):
    """The platform's own alert rules are staged and re-rendered on every init, like the
    probes: a copy vendored once at scaffold time never received rules added later (v0.29.0
    shipped three platform alerts no existing instance got)."""

    REL = "platform/monitoring/rules.yaml"

    def test_the_rules_are_staged_not_vendored(self):
        self.assertTrue((TEMPLATE / "provisioning" / "clusters" / self.REL).is_file())
        self.assertFalse((TEMPLATE / "clusters" / self.REL).exists())

    def test_an_existing_instance_gets_the_current_rules_and_prometheus_templates_survive(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        fx = InitFixture(tmp, json.loads(json.dumps(BASE_CONFIG)))
        old = fx.rendered(self.REL)
        old.parent.mkdir(parents=True, exist_ok=True)
        old.write_text("# an older core's vendored copy\napiVersion: monitoring.coreos.com/v1\n"
                       "kind: PrometheusRule\nmetadata: {name: orbit, namespace: monitoring}\n"
                       "spec: {groups: []}\n")
        proc = fx.run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        text = old.read_text()
        doc = yaml.safe_load(text)
        self.assertEqual(doc["metadata"], {"name": "orbit", "namespace": "monitoring"})
        groups = {g["name"] for g in doc["spec"]["groups"]}
        self.assertLessEqual({"orbit.node", "orbit.workloads", "orbit.public-surfaces",
                              "orbit.backups", "orbit.capacity"}, groups)
        # Prometheus's own templating is not init's: it reaches the cluster untouched
        self.assertIn("{{ $labels.instance }}", text)
        self.assertIn("{{ $value | humanizePercentage }}", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class Round4ShapeTest(ExampleConfigTest):
    """Area-1 sweep round 4 (auditor A4, breaker B3/B9): domain and name fields are held
    to the loader's shapes BEFORE anything is rendered; a probe URL's explicit port is
    a port."""

    def _refused(self, mutate):
        root = self.scaffold(fill_everything=True)
        cfg = yaml.safe_load((root / "instance" / "config.yaml").read_text())
        mutate(cfg)
        (root / "instance" / "config.yaml").write_text(yaml.safe_dump(cfg))
        before = sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())
        proc = self.init(root)
        after = sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())
        return proc, before == after

    def test_domain_and_name_shapes_are_refused_before_rendering(self):
        cases = (("domains.primary", lambda c: c["domains"].__setitem__("primary", "bad/name.example"), "domains.primary is not a DNS name"),
                 ("domains.primary", lambda c: c["domains"].__setitem__("primary", "acme.test,evil.test"), "domains.primary is not a DNS name"),
                 ("domains.dev", lambda c: c["domains"].__setitem__("dev", "dev fixture.org"), "domains.dev is not a DNS name"),
                 ("domains.dnsZones", lambda c: c["domains"].__setitem__("dnsZones", ["fixture.org", "PLANTED zone"]), "domains.dnsZones is not a list of DNS names"),
                 ("instance.name", lambda c: c["instance"].__setitem__("name", "Fix_ture"), "instance.name is not a lowercase label"))
        for field, mutate, needle in cases:
            with self.subTest(field=field):
                proc, untouched = self._refused(mutate)
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn(needle, proc.stderr + proc.stdout)
                self.assertNotIn("PLANTED", proc.stderr + proc.stdout); self.assertNotIn("evil", proc.stderr + proc.stdout)
                self.assertTrue(untouched, "a refused config must render nothing")

    def test_a_probe_url_with_a_malformed_port_is_refused(self):
        for url, needle in (("http://loki.loki.svc:not-a-port/ready", "monitoring.probes[0].url names a malformed port"),
                            ("http://loki.loki.svc:70000/ready", "monitoring.probes[0].url names a malformed port"),
                            ("http://loki.loki.svc:0/ready", "monitoring.probes[0].url names a port outside 1..65535")):
            with self.subTest(url=url):
                proc, _ = self._refused(lambda c, u=url: c.setdefault("monitoring", {}).__setitem__("probes", [{"url": u, "module": "http_2xx"}]))
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn(needle, proc.stderr + proc.stdout)
        root = self.scaffold(fill_everything=True)
        cfg = yaml.safe_load((root / "instance" / "config.yaml").read_text())
        cfg.setdefault("monitoring", {})["probes"] = [{"url": "http://loki.loki.svc:3100/ready", "module": "http_2xx"}]
        (root / "instance" / "config.yaml").write_text(yaml.safe_dump(cfg))
        self.assertEqual(self.init(root).returncode, 0)


class Round5InitTest(ExampleConfigTest):
    """Area-1 sweep round 5 (auditor A2, A4, A5, A6)."""

    def _mutated(self, mutate):
        root = self.scaffold(fill_everything=True)
        cfg = yaml.safe_load((root / "instance" / "config.yaml").read_text())
        mutate(cfg)
        (root / "instance" / "config.yaml").write_text(yaml.safe_dump(cfg))
        return root

    def test_a_symlink_in_the_staged_tree_is_refused_before_anything_is_read(self):
        root = self.scaffold(fill_everything=True)
        outside = root.parent / "outside-PLANTED.yaml"; outside.write_text("leak: PLANTED_CREDENTIAL\n")
        self.addCleanup(outside.unlink)
        (root / "provisioning/clusters/platform/config/leak.yaml").symlink_to(outside)
        proc = self.init(root)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("provisioning/clusters/platform/config/leak.yaml is a symlink", proc.stderr)
        self.assertFalse((root / "clusters/platform/config/leak.yaml").exists())
        self.assertNotIn("PLANTED_CREDENTIAL", proc.stdout + proc.stderr)

    def test_a_symlink_named_after_a_token_is_refused_without_repeating_its_name(self):
        """Round 19 (breaker B2): the refusal repeated the link's path verbatim."""
        root = self.scaffold(fill_everything=True)
        outside = root.parent / "outside-19.yaml"; outside.write_text("leak: PLANTED_CREDENTIAL\n")
        self.addCleanup(outside.unlink)
        (root / "provisioning/clusters/platform/config/Bearer PLANTED_LINK_CREDENTIAL.yaml").symlink_to(outside)
        proc = self.init(root)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("provisioning/clusters/platform/config/(a name not of a file-name shape, or of a credential's shape) is a symlink", proc.stderr)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_a_directory_in_the_way_of_a_staged_file_is_named_in_file_name_shape(self):
        """Round 20 (auditor A1): the refusal repeated the staged file's name verbatim."""
        root = self.scaffold(fill_everything=True)
        (root / "provisioning/clusters/Bearer PLANTED_STAGED_CREDENTIAL.yaml").write_text("{}\n")
        (root / "clusters/Bearer PLANTED_STAGED_CREDENTIAL.yaml").mkdir(parents=True)
        proc = self.init(root)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("clusters/(a name not of a file-name shape, or of a credential's shape) is a directory", proc.stderr); self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_the_dev_domain_is_held_to_the_loaders_limit_before_rendering(self):
        """Round 19 (breaker B4): a 243-character domains.dev rendered 33 files that the
        loader then refused — init and the loader hold every exported domain to 240."""
        long = ".".join(["a" * 60] * 4)
        proc = self.init(self._mutated(lambda c: c["domains"].__setitem__("dev", long)))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("domains.dev is longer than 240 characters", proc.stderr); self.assertNotIn("aaaa", proc.stderr)

    def test_a_domain_whose_derived_names_would_exceed_253_is_refused(self):
        """Round 17 (breaker B7): a 245-character primary domain rendered a Certificate
        named wildcard-<dashed> (254) and a Secret wildcard-<dashed>-tls (258)."""
        long = ".".join(["a" * 63] * 3 + ["b" * 53])
        proc = self.init(self._mutated(lambda c: c["domains"].__setitem__("primary", long)))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("domains.primary is longer than 240 characters", proc.stderr); self.assertNotIn("aaaa", proc.stderr)

    def test_a_malformed_catalog_is_refused_by_class(self):
        root = self._mutated(lambda c: c.__setitem__("orbitd", {"applyMode": "off", "applyPairs": ["demo/prod"]}))
        (root / "catalog" / "projects.yaml").write_text("projects:\n  credential: [PLANTED_CATALOG_TOKEN\n")
        proc = self.init(root)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("catalog/projects.yaml cannot be read as YAML (", proc.stderr)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr); self.assertNotIn("Traceback", proc.stderr)

    def test_an_object_storage_endpoint_with_userinfo_is_refused(self):
        for endpoint, needle in (("https://access:PLANTED_PASSWORD@fsn1.your-objectstorage.com", "objectStorage.endpoint is not https://<host>"),
                                 ("http://fsn1.your-objectstorage.com", "objectStorage.endpoint is not https://<host>"),
                                 ("https://fsn1.your-objectstorage.com/PLANTED?x=1", "objectStorage.endpoint is not https://<host>"),
                                 ("https://fsn1.your-objectstorage.com:notaport", "well-formed port")):
            with self.subTest(endpoint=endpoint[:30]):
                root = self._mutated(lambda c, e=endpoint: c.__setitem__("objectStorage", {"endpoint": e, "buckets": {"platform": "b"}}))
                proc = self.init(root)
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn(needle, proc.stderr); self.assertNotIn("PLANTED", proc.stdout + proc.stderr)
        root = self._mutated(lambda c: c.__setitem__("objectStorage", {"endpoint": "https://fsn1.your-objectstorage.com:8443", "buckets": {"platform": "b"}}))
        proc = self.init(root); self.assertEqual(proc.returncode, 0, proc.stderr)
        rendered = "\n".join(p.read_text() for p in (root / "clusters").rglob("*.yaml"))
        self.assertIn("fsn1.your-objectstorage.com:8443", rendered); self.assertNotIn("{{objectStorage", rendered)

    def test_domains_are_rendered_in_their_canonical_spelling(self):
        root = self._mutated(lambda c: c["domains"].__setitem__("primary", " Fixture.org. "))
        proc = self.init(root); self.assertEqual(proc.returncode, 0, proc.stderr)
        rendered = "\n".join(p.read_text() for p in (root / "clusters").rglob("*.yaml"))
        self.assertIn("wildcard-fixture-org-tls", rendered)
        self.assertNotIn("fixture-org-", rendered.replace("fixture-org-tls", "")); self.assertNotIn("Fixture.org", rendered); self.assertNotIn("fixture.org.", rendered)


    def test_a_probe_module_of_no_module_shape_is_refused(self):
        """B10: `HTTP_BAD / module` rendered a Probe named orbit-HTTP-BAD / module-surfaces."""
        for module in ("HTTP_BAD / module", "http-2xx", "PLANTED credential", ""):
            with self.subTest(module=module):
                root = self._mutated(lambda c, m=module: c.setdefault("monitoring", {}).__setitem__("probes", [{"url": "https://x.fixture.org/", "module": m}]))
                proc = self.init(root)
                self.assertNotEqual(proc.returncode, 0); self.assertIn("monitoring.probes[0]", proc.stderr); self.assertNotIn("PLANTED", proc.stderr)
        root = self._mutated(lambda c: c.setdefault("monitoring", {}).__setitem__("probes", [{"url": "https://x.fixture.org/", "module": "http_401"}]))
        self.assertEqual(self.init(root).returncode, 0)

    def test_a_branch_of_no_ref_shape_is_refused(self):
        """B14: `feature branch` rendered a ref of that text, which nothing can fetch."""
        for branch in ("feature branch", "-lead", "a..b", "x/", "y.lock", "z@{1}", "PLANTED~1", "p//q"):
            with self.subTest(branch=branch):
                root = self._mutated(lambda c, b=branch: c["git"].__setitem__("branch", b))
                proc = self.init(root)
                self.assertNotEqual(proc.returncode, 0); self.assertIn("git.branch", proc.stderr); self.assertNotIn("PLANTED", proc.stderr)
        for branch in ("main", "release/2026.09", "feat_x-1.2"):
            with self.subTest(branch=branch):
                root = self._mutated(lambda c, b=branch: c["git"].__setitem__("branch", b))
                self.assertEqual(self.init(root).returncode, 0, self.init(root).stderr)


    def test_a_branch_with_a_bad_component_is_refused(self):
        """B5 (round 6): feature/.hidden and feature.lock/topic passed the whole-string rules."""
        for branch in ("feature/.hidden", "feature.lock/topic", "a//b", "x/"):
            with self.subTest(branch=branch):
                proc = self.init(self._mutated(lambda c, b=branch: c["git"].__setitem__("branch", b)))
                self.assertNotEqual(proc.returncode, 0); self.assertIn("git.branch", proc.stderr)

    def test_probes_are_validated_before_the_tree_is_rewritten(self):
        """B6 (round 6): a second init with a malformed probe rewrote the rendered tree
        (custom targets gone) and then refused."""
        root = self._mutated(lambda c: c.setdefault("monitoring", {}).__setitem__("probes", [{"url": "https://x.fixture.org/health", "module": "http_2xx"}]))
        self.assertEqual(self.init(root).returncode, 0)
        probes = root / "clusters/platform/monitoring/probes.yaml"
        before = probes.read_text(); self.assertIn("x.fixture.org/health", before)
        cfg = yaml.safe_load((root / "instance" / "config.yaml").read_text())
        cfg["monitoring"]["probes"].append({"url": "http://loki.loki.svc:not-a-port/ready", "module": "http_2xx"})
        (root / "instance" / "config.yaml").write_text(yaml.safe_dump(cfg))
        proc = self.init(root)
        self.assertNotEqual(proc.returncode, 0); self.assertIn("monitoring.probes[1].url", proc.stderr)
        self.assertNotIn("rendered", proc.stdout)
        self.assertEqual(probes.read_text(), before, "a refused config must leave the rendered tree as it was")

    def test_a_probe_url_with_a_query_is_refused(self):
        """B7 (round 6): ?token=... rendered into probes.yaml."""
        for url in ("https://hooks.example.org/?token=PLANTED_CREDENTIAL_1234567890", "https://x.fixture.org/a#PLANTED"):
            with self.subTest(url=url[:30]):
                proc = self.init(self._mutated(lambda c, u=url: c.setdefault("monitoring", {}).__setitem__("probes", [{"url": u, "module": "http_2xx"}])))
                self.assertNotEqual(proc.returncode, 0); self.assertIn("monitoring.probes[0].url carries a query or fragment", proc.stderr); self.assertNotIn("PLANTED", proc.stderr)

    def test_the_core_image_reference_is_held_to_its_shape(self):
        """A7 (round 10): coreRegistry.owner / orbitdTag were only string-checked and
        land in `image:` bare; a value with a space or a quote broke the manifest."""
        for field, value, needle in (("owner", "ghcr.io/fixture core PLANTED", "coreRegistry.owner is not an image repository path"),
                                     ("owner", "not-a-path", "coreRegistry.owner is not an image repository path"),
                                     ("orbitdTag", "main-1111111 PLANTED", "coreRegistry.orbitdTag is not an image tag"),
                                     ("orbitdTag", "-leading", "coreRegistry.orbitdTag is not an image tag")):
            with self.subTest(field=field, value=value[:12]):
                proc = self.init(self._mutated(lambda c, f=field, v=value: c.setdefault("coreRegistry", {}).__setitem__(f, v)))
                self.assertNotEqual(proc.returncode, 0); self.assertIn(needle, proc.stderr); self.assertNotIn("PLANTED", proc.stderr)

    def test_a_bare_value_must_read_back_as_the_same_string_or_the_same_number(self):
        """B5 (round 10): `_yaml_keeps` accepted any non-string parse, so `012345` (octal
        5349 to a YAML 1.1 reader) and a branch named `2024` (an int) rendered as
        something else. A string field must round-trip as that string; a numeric
        field as the number it prints as."""
        for field, mutate in (("contacts.telegramChatId", lambda c: c["contacts"].__setitem__("telegramChatId", "012345")),
                              ("contacts.telegramChatId", lambda c: c["contacts"].__setitem__("telegramChatId", "1e3")),
                              ("git.branch", lambda c: c["git"].__setitem__("branch", "2024"))):
            with self.subTest(field=field):
                proc = self.init(self._mutated(mutate))
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("text YAML would read as something other than that string", proc.stderr); self.assertIn(field, proc.stderr)
        proc = self.init(self._mutated(lambda c: c["contacts"].__setitem__("telegramChatId", "-1001234567890")))
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_a_probe_path_is_inspected_decoded_as_well(self):
        """B6 (round 10): `/bot%31%32%33:token/` passed the credential-shaped-path check,
        which read the path as written; the decoded path is inspected too."""
        for url in ("https://x.fixture.org/bot%31%32%33%3APLANTEDTOKEN/sendMessage",
                    "https://x.fixture.org/%50LANTEDLONGCREDENTIALVALUE1234567890/x"):
            with self.subTest(url=url[:40]):
                proc = self.init(self._mutated(lambda c, u=url: c.setdefault("monitoring", {}).__setitem__("probes", [{"url": u, "module": "http_2xx"}])))
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("carries a path segment that reads like a credential", proc.stderr); self.assertNotIn("PLANTED", proc.stderr)

    def test_a_non_string_token_is_refused_by_field(self):
        """A7 (round 6): coreRegistry.orbitdTag: 1234 (a bare YAML integer) reached the
        substitution as an int and crashed after the tree was rewritten."""
        proc = self.init(self._mutated(lambda c: c.setdefault("coreRegistry", {}).__setitem__("orbitdTag", 1234)))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("these fields must be strings (quote the value): coreRegistry.orbitdTag", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)


    def test_the_staged_banner_says_provisioning_is_vendored(self):
        """A1 (round 9): the banner told operators to edit the staged source, which
        core-sync replaces on the next `make init`."""
        root = self.scaffold(fill_everything=True)
        proc = self.init(root)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        head = (root / "clusters/platform/monitoring/probes.yaml").read_text().splitlines()[:6]
        self.assertTrue(any("provisioning/ is vendored from orbit-core" in l for l in head), head)
        self.assertFalse(any("EDIT THIS FILE" in l for l in head), head)

    def test_a_probe_url_is_validated_after_token_substitution_too(self):
        """A7 (round 7): the URL was validated with tokens standing in, then rendered
        with their values unchecked; a branch token can carry a credential-shaped segment."""
        def mutate(c):
            c["git"]["branch"] = "feature/PLANTED_PROBE_TOKEN_1234567890abcdef"
            c.setdefault("monitoring", {})["probes"] = [{"url": "https://app.{{domains.primary}}/{{git.branch}}/health", "module": "http_2xx"}]
        proc = self.init(self._mutated(mutate))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("monitoring.probes[0].url (after token substitution) carries a path segment that reads like a credential", proc.stderr)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)
        proc = self.init(self._mutated(lambda c: c.setdefault("monitoring", {}).__setitem__("probes", [{"url": "https://app.{{domains.primary}}/{{instance.name}}/health", "module": "http_2xx"}])))
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_a_probe_path_that_reads_like_a_credential_is_refused(self):
        """B1 (round 7): /bot123:<token>/getMe rendered whole into probes.yaml."""
        for url in ("https://api.telegram.org/bot123456:PLANTED_PROBE_TOKEN_123456789/getMe",
                    "https://x.fixture.org/v1/PLANTED_PROBE_TOKEN_1234567890abcdef/health"):
            with self.subTest(url=url[:40]):
                proc = self.init(self._mutated(lambda c, u=url: c.setdefault("monitoring", {}).__setitem__("probes", [{"url": u, "module": "http_2xx"}])))
                self.assertNotEqual(proc.returncode, 0); self.assertIn("monitoring.probes[0].url carries a path segment that reads like a credential", proc.stderr); self.assertNotIn("PLANTED", proc.stderr)
        proc = self.init(self._mutated(lambda c: c.setdefault("monitoring", {}).__setitem__("probes", [{"url": "https://x.fixture.org/api/v1/health/ready", "module": "http_2xx"}])))
        self.assertEqual(proc.returncode, 0, proc.stderr)


class Round24InitTest(ExampleConfigTest):
    """Area-1 sweep round 24: the generator's repairs (the round-5 mutation helper reused)."""
    _mutated = Round5InitTest._mutated

    def test_an_all_digit_image_tag_is_a_tag(self):
        """A5: `_yaml_keeps("1234")` read the tag as an int and refused it, though `image:`
        carries it as text; the embedded fields skip the bare-YAML round-trip."""
        proc = self.init(self._mutated(lambda c: c["coreRegistry"].__setitem__("orbitdTag", "1234")))
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_a_name_the_platform_uses_as_a_literal_is_refused(self):
        """B2: instance.name `prod` collided with the `orbit-agents-prod` namespace and the
        `prod` environment the templates spell out."""
        for name in ("prod", "platform", "kube-system", "main", "default"):
            with self.subTest(name=name):
                proc = self.init(self._mutated(lambda c, n=name: c["instance"].__setitem__("name", n)))
                self.assertNotEqual(proc.returncode, 0); self.assertIn("instance.name is a word the platform itself uses", proc.stderr)
        proc = self.init(self._mutated(lambda c: c["instance"].__setitem__("name", "acme")))
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_a_probe_module_must_be_one_the_exporter_defines(self):
        """B5: a module of the right shape but no definition rendered a Probe the exporter
        answers with an error for ever."""
        proc = self.init(self._mutated(lambda c: c.setdefault("monitoring", {}).__setitem__("probes", [{"url": "https://status.{{domains.primary}}/", "module": "does_not_exist"}])))
        self.assertNotEqual(proc.returncode, 0); self.assertIn("not a module the blackbox exporter defines", proc.stderr); self.assertNotIn("does_not_exist", proc.stderr)
        proc = self.init(self._mutated(lambda c: c.setdefault("monitoring", {}).__setitem__("probes", [{"url": "https://status.{{domains.primary}}/", "module": "http_2xx"}])))
        self.assertEqual(proc.returncode, 0, proc.stderr)


class Round25InitTest(ExampleConfigTest):
    """Area-1 sweep round 25: the generator's repairs (the round-5 mutation helper reused)."""
    _mutated = Round5InitTest._mutated

    def test_an_owner_mismatch_repeats_only_owner_shaped_values(self):
        """B3: the three-spellings refusal interpolated git.github_org and registry
        verbatim; a value of a credential's shape was echoed."""
        proc = self.init(self._mutated(lambda c: c["git"].__setitem__("github_org", "PLANTED0123456789abcdef")))
        self.assertNotEqual(proc.returncode, 0); self.assertIn("(a value not of an owner shape, or of a credential's shape)", proc.stderr); self.assertNotIn("PLANTED", proc.stderr)
        proc = self.init(self._mutated(lambda c: c.__setitem__("registry", "ghcr.io/PLANTED0123456789abcdef")))
        self.assertNotEqual(proc.returncode, 0); self.assertIn("(a value not of an owner shape, or of a credential's shape)", proc.stderr); self.assertNotIn("PLANTED", proc.stderr)
