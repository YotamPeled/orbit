#!/usr/bin/env python3
"""
Guards for the cumulative `make verify` runner and stage2's generated-surface probe.

Run as `python3 scripts/test_verify_runner.py`. No cluster, no credentials, no
network: every external a stage touches (kubectl, ssh, curl, openssl, timeout, git)
is a stub on PATH, and the configs are synthetic fixtures in a tmpdir.

Two breaks, one file — both surfaced while building the second real org:

  THE RUNNER (template/Makefile `verify`) stopped at the first red stage
  (`bash $$s || exit 1`), so one known-red row hid every later stage's verdict and
  the tallies had to be collected stage by stage. MakefileRunnerTest runs the REAL
  target against stub stages and asserts the break directly: a failing first stage
  must still let the later stages run, the aggregate exit must be failure, and an
  all-green run must exit 0 — with the per-stage `== ... ==` headers, a combined
  verdict line, and deterministic (glob) order intact.

  THE GENERATED SURFACE (scripts/verify/stage2.sh) must probe exactly the prod
  hostnames the generator renders (scripts/catalog-sync.py env_hostnames with the
  instance domains as defaults). An omitted prod `hostnames` defaults to
  `<id>.<primary>` — never to the bare apex, which docs/ONBOARDING.md records as
  having no record (only wildcard project hosts are served). An explicit apex in
  the catalog is still probed; an empty catalog has no project surface.
  Stage2GeneratedSurfaceTest runs the REAL stage with stubbed externals and
  asserts: the generator default is probed, explicit overlap probes once in
  declaration order, expose:false/external/managed contribute no default,
  malformed/missing/null catalogs fail closed, whitespace-invalid hostnames fail
  closed without splitting into separate probes, normalized equivalents probe
  once, and any failure still runs to the oracle tail.

Every expectation is a hand-derived literal (exact markers, exact URLs, exact exit
codes) — never computed with the code under test.
"""
import copy
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
MAKEFILE = ROOT / "template" / "Makefile"
STAGE = ROOT / "scripts" / "verify" / "stage2.sh"

APEX = "fixture.org"
SEALED_TOKEN_FILE = 'apiVersion: isindir.github.com/v1alpha3\nkind: SopsSecret\nmetadata: {name: cloudflare-api-token, namespace: cert-manager}\nspec:\n  secretTemplates:\n  - name: cloudflare-api-token\n    stringData:\n      api-token: ENC[AES256_GCM,data:YWJj,iv:ZGVm,tag:Z2hp,type:str]\nsops: {age: [{recipient: age1x, enc: x}]}\n'
DEV = "dev.fixture.org"


_DIRAPP = {"apiVersion": "argoproj.io/v1alpha1", "kind": "Application", "metadata": {"name": "everything", "namespace": "argocd"},
           "spec": {"source": {"path": "clusters", "directory": {"recurse": True}}, "destination": {"namespace": "monitoring"}}}


def _dirapp(root: pathlib.Path, ns: str = "monitoring") -> None:
    """Round 16: the Service index and the policy gatherer apply the renderer's membership
    rule, so a fixture file is somebody's manifest only under a directory Application."""
    app = dict(_DIRAPP); app["spec"] = {"source": _DIRAPP["spec"]["source"], "destination": {"namespace": ns}}
    # round 19: the renderer's entrypoint — clusters/platform/apps/*.yaml, nowhere else
    _write(root / "clusters" / "platform" / "apps" / "_everything.yaml", yaml.safe_dump(app))


def _write(path: pathlib.Path, text: str, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Every stub and fixture is written with a trailing newline by construction;
    # without it a stub's last line could merge with a later append.
    path.write_text(text if text.endswith("\n") else text + "\n")
    if executable:
        path.chmod(0o755)


class MakefileRunnerTest(unittest.TestCase):
    """Behavioural, not textual: run the real `verify` target against stub stages.

    The break this catches is the early exit: with `bash $$s || exit 1` a red
    stage1 means stage2 and stage3 never execute, and their verdicts never appear.
    """

    def _sandbox(self, exits):
        """A bare instance tree: the real Makefile plus stub stages exiting `exits`.

        Each stub appends a literal marker to $RUN_LOG before exiting, so the test
        observes which stages ran and in what order without reading the Makefile.
        """
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="verify-runner-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        shutil.copy2(MAKEFILE, tmp / "Makefile")
        for i, code in enumerate(exits, 1):
            name = "stage%d.sh" % i
            _write(tmp / "scripts" / "verify" / name,
                   "#!/usr/bin/env bash\n"
                   'echo "marker-%s" >> "$RUN_LOG"\n'
                   "exit %d\n" % (name, code),
                   executable=True)
        return tmp

    def _run_verify(self, tmp):
        run_log = tmp / "run.log"
        run_log.write_text("")
        env = dict(os.environ, RUN_LOG=str(run_log))
        proc = subprocess.run(["make", "verify"], cwd=tmp,
                              capture_output=True, text=True, env=env)
        return proc, run_log.read_text().split()

    def test_first_failure_does_not_suppress_later_stages(self):
        """A red stage1 must still run stage2 and stage3, and the run must fail."""
        proc, markers = self._run_verify(self._sandbox([1, 0, 0]))
        self.assertEqual(markers, ["marker-stage1.sh",
                                   "marker-stage2.sh",
                                   "marker-stage3.sh"],
                         "a later stage never ran — the runner stopped at the "
                         "first failure:\n" + proc.stdout)
        self.assertNotEqual(proc.returncode, 0,
                            "a run with a red stage exited 0")
        for i in (1, 2, 3):
            self.assertIn("== scripts/verify/stage%d.sh ==" % i, proc.stdout)
        self.assertIn("verify:", proc.stdout)

    def test_all_pass_returns_zero(self):
        proc, markers = self._run_verify(self._sandbox([0, 0, 0]))
        self.assertEqual(markers, ["marker-stage1.sh",
                                   "marker-stage2.sh",
                                   "marker-stage3.sh"])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("verify:", proc.stdout)

    def test_failure_in_a_later_stage_still_fails_the_run(self):
        """Guards the verdict wiring against a future `|| true`: red anywhere
        must mean a red run, even when every earlier stage was green."""
        proc, markers = self._run_verify(self._sandbox([0, 0, 1]))
        self.assertEqual(markers, ["marker-stage1.sh",
                                   "marker-stage2.sh",
                                   "marker-stage3.sh"])
        self.assertNotEqual(proc.returncode, 0,
                            "a run with a red final stage exited 0")


# Every external scripts/verify/stage2.sh touches, stubbed. Each stub answers only
# the calls the stage makes; anything unexpected exits 0 so an unmodelled call can
# never fail the run for a reason the test is not asserting about.
KUBECTL_STUB = """\
#!/usr/bin/env bash
case "$*" in
  *--raw*) echo '{"kubeletConfig":{"memory.available":"100Mi","systemReserved":{"cpu":"100m"}}}' ;;
  *allocatable.memory*capacity.memory*) printf '1800Mi\n2000Mi\n' ;;
  *clusterissuer*) echo "True" ;;
  *certificate*) echo "True" ;;
  *cloudflare-api-token*) exit 0 ;;
  *alt-names*) echo "$ORBIT_DOMAIN,*.$ORBIT_DOMAIN,*.$ORBIT_DEV_DOMAIN" ;;
  *tls*) echo "anVuaw==" ;;
  *"logs deploy/coredns"*) i=0; while [ "$i" -lt "${COREDNS_UNREACHABLE:-0}" ]; do echo "[ERROR] plugin/errors: 2 ghcr.io. A: dial udp [2a01:db8::1]:53: connect: network is unreachable PLANTED_DNS_LINE"; i=$((i+1)); done; [ "${COREDNS_LOG_RC:-0}" = 0 ] || exit 1 ;;
  *deploy*) echo "1" ;;
  *) echo "Synced/Healthy" ;;
esac
exit 0
"""

SSH_STUB = """\
#!/usr/bin/env bash
case "$*" in
  *swapon*) echo 0 ;;
  *90-orbit-inotify.conf*) echo "fs.inotify.max_user_instances = 512" ;;
  *sysctl*) echo 576 ;;
  *"sshd -T"*) printf 'port 22\npasswordauthentication %s\npermitrootlogin yes\n' "${SSHD_PWAUTH:-no}" ;;
  *) exit 0 ;;
esac
exit 0
"""

CURL_STUB = """\
#!/usr/bin/env bash
# A header dump (-D) is the http->https redirect probe: answer a permanent
# redirect. A status-code probe (-w) is a public surface: log the https URL and
# answer 200, or 500 for the one host $CURL_FAIL_HOST names.
prev=""
for a in "$@"; do
  if [ "$prev" = "-D" ]; then
    printf 'HTTP/1.1 301 Moved Permanently\\r\\nlocation: https://probe.fixture.org/\\r\\n\\r\\n' > "$a"
    exit 0
  fi
  prev="$a"
done
url=""
for a in "$@"; do
  case "$a" in https://*) url="$a";; esac
done
if [ -n "$url" ]; then
  echo "$url" >> "$CURL_LOG/log"
  host="${url#https://}"
  host="${host%%/*}"
  if [ "$host" = "$CURL_FAIL_HOST" ]; then printf '500'; else printf '200'; fi
fi
exit 0
"""

OPENSSL_STUB = """\
#!/usr/bin/env bash
case "$*" in
  *s_client*) printf -- '-----BEGIN CERTIFICATE-----\\nZmFrZQ==\\n-----END CERTIFICATE-----\\nVerify return code: 0 (ok)\\n' ;;
  *issuer*) echo "issuer=C = US, O = Let's Encrypt, CN = R3" ;;
  *subjectAltName*) echo "X509v3 Subject Alternative Name:"; echo "DNS:$ORBIT_DOMAIN, DNS:*.$ORBIT_DOMAIN, DNS:*.$ORBIT_DEV_DOMAIN" ;;
esac
exit 0
"""

TIMEOUT_STUB = """\
#!/usr/bin/env bash
# A closed port fails at once; anything else runs without a limit.
case "$*" in
  */dev/tcp/*) exit 1 ;;
  *) shift; exec "$@" ;;
esac
"""

GIT_STUB = """\
#!/usr/bin/env bash
exit 0
"""

# The smallest instance/config.yaml scripts/lib-instance.sh accepts: every key it
# reads, nothing it does not. The apex is deliberately an org that is not the one
# the stage was extracted from.
CONFIG_YAML = """\
instance:
  name: fixtureinst
git:
  repoUrl: git@github.com:fixture-org/fixture-instance.git
  branch: main
  github_org: fixture-org
domains:
  primary: fixture.org
  dev: dev.fixture.org
  dnsZones: [fixture.org]
registry: ghcr.io/fixture-org
nodes:
  - name: fixture-node-1
    ip: 198.51.100.10
    role: control-plane
secrets:
  provider: infisical-cloud
  projectSlug: fixture-slug
contacts:
  email: ops@fixture.org
"""

EMPTY_CATALOG = "projects: []\n"

DEFAULT_CATALOG = """\
projects:
  - id: portal
    environments:
      prod: {}
"""

EXPLICIT_APEX_CATALOG = """\
projects:
  - id: portal
    environments:
      prod:
        hostnames: [fixture.org]
"""

DECLARATION_ORDER_CATALOG = """\
projects:
  - id: portal
    environments:
      prod:
        hostnames: [app.fixture.org, fixture.org]
        customDomains:
          - {host: shop.fixture.org}
"""

EXPOSE_FALSE_CATALOG = """\
projects:
  - id: portal
    environments:
      prod:
        expose: false
"""

EXPOSE_FALSE_CONFLICT_CATALOG = """\
projects:
  - id: portal
    environments:
      prod:
        expose: false
        hostnames: [app.fixture.org]
"""

EXTERNAL_CATALOG = """\
projects:
  - id: portal
    hosted: external
    environments:
      prod: {}
"""

MANAGED_CATALOG = """\
projects:
  - id: portal
    managed_elsewhere: true
    environments:
      prod: {}
"""

DEV_ONLY_CATALOG = """\
projects:
  - id: portal
    environments:
      dev:
        enabled: true
"""

MALFORMED_YAML_CATALOG = "projects: [\n"

MISSING_PROJECTS_CATALOG = "version: 3\n"

NULL_PROJECTS_CATALOG = "projects:\n"

NULL_CATALOG = "null\n"

NULL_ENTRY_CATALOG = "projects:\n  -\n"

MALFORMED_PROJECT_CATALOG = "projects:\n  - not-a-mapping\n"

WHITESPACE_CATALOG = """\
projects:
  - id: portal
    environments:
      prod:
        hostnames: ['bad host']
"""

NORMALIZED_OVERLAP_CATALOG = """\
projects:
  - id: portal
    environments:
      prod:
        hostnames: ['Fixture.Org.', 'fixture.org']
"""

DEFAULT_PLUS_EXPLICIT_CATALOG = """\
projects:
  - id: portal
    environments:
      prod: {}
  - id: other
    environments:
      prod:
        hostnames: [portal.fixture.org]
"""


class Stage3RaceSeedTest(unittest.TestCase):
    """The tag-bump race in stage3 seeds a bare origin and clones it four times. git names
    a new repository's first branch after the machine's setting; the seed pushes main, so
    the origin's HEAD and every clone must say main explicitly, or on a machine whose
    default is master the clones check out nothing and the race measures four empty
    checkouts. The block runs as it is, under a HOME whose git default is master."""

    def test_the_race_clones_check_out_main_whatever_the_default_branch_is(self):
        text = (ROOT / "scripts" / "verify" / "stage3.sh").read_text()
        block = text[text.index("seed_generator_inputs(){"):text.index("\nW=$(mktemp -d)")] + "\n"
        block += text[text.index("R=$(mktemp -d)\ngit init -q --bare"):text.index("( cd \"$R/a\" && IMAGE=")]
        block += "for c in a b c d; do git -C \"$R/$c\" rev-parse --abbrev-ref HEAD; ls \"$R/$c/catalog/projects.yaml\"; done\n"
        with tempfile.TemporaryDirectory() as td:
            home = pathlib.Path(td) / "home"; home.mkdir()
            (home / ".gitconfig").write_text("[init]\n\tdefaultBranch = master\n[user]\n\tname = t\n\temail = t@t\n")
            work = pathlib.Path(td) / "work"; work.mkdir()
            (work / "catalog").mkdir(); (work / "catalog/tags.yaml").write_text("tags: {}\n")
            shutil.copytree(ROOT / "scripts", work / "scripts")
            shutil.copytree(ROOT / "charts", work / "charts")
            shutil.copytree(ROOT / "template", work / "template")
            env = {**os.environ, "HOME": str(home), "GIT_CONFIG_GLOBAL": str(home / ".gitconfig"), "XDG_CONFIG_HOME": str(home)}
            proc = subprocess.run(["bash", "-c", block], cwd=work, env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        lines = proc.stdout.strip().splitlines()
        self.assertEqual([l for l in lines if not l.endswith("projects.yaml")], ["main"] * 4, proc.stdout + proc.stderr)
        self.assertEqual(len([l for l in lines if l.endswith("projects.yaml")]), 4, proc.stdout + proc.stderr)


class InstanceConfigLoaderTest(unittest.TestCase):
    """scripts/lib-instance.sh turns instance/config.yaml into exports through eval; a
    config value is data, never code. A value holding a command substitution, a
    backtick, a variable, a quote and a space comes back byte for byte, runs nothing,
    and an environment override still wins."""

    def test_a_config_value_is_never_evaluated(self):
        """A value full of shell syntax runs nothing: since round 2 (2026-09-13) every
        exported field has a shape, so such a value is refused by field name — and the
        refusal itself evaluates nothing (the planted file is never read) and exports
        nothing. A well-shaped value comes back byte for byte, an override still wins."""
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            secret = root / "secret.txt"; secret.write_text("PLANTED-FILE-CREDENTIAL\n")
            evil = "$(cat " + str(secret) + " >&2)`id`$HOME it's \"x\" a b"
            cfg = {"instance": {"name": "fx"}, "domains": {"primary": "fx.org", "dev": "dev.fx.org", "dnsZones": ["fx.org", "z2.org"]},
                   "registry": "ghcr.io/fx", "git": {"repoUrl": evil, "github_org": "fx"},
                   "nodes": [{"name": "n1", "ip": "203.0.113.5", "role": "control-plane"}]}
            (root / "instance").mkdir(); (root / "instance/config.yaml").write_text(yaml.safe_dump(cfg))
            script = ("source " + str(ROOT / "scripts" / "lib-instance.sh") + "; printf '%s\\n' \"rc=$?\" \"${ORBIT_REPO_URL:-unset}\" \"${ORBIT_DNS_ZONES:-unset}\" \"${ORBIT_DOMAIN:-unset}\" \"${ORBIT_CONTROL_PLANE_IP:-unset}\"")
            env = {k: v for k, v in os.environ.items() if not k.startswith("ORBIT_")}
            env["ORBIT_INSTANCE_CONFIG"] = str(root / "instance/config.yaml"); env["ORBIT_DOMAIN"] = "override.org"
            proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60, env=env)
            self.assertNotIn("PLANTED", proc.stdout + proc.stderr)
            self.assertIn("git.repoUrl is not a GitHub repository URL", proc.stderr)
            self.assertEqual(proc.stdout.splitlines(), ["rc=2", "unset", "unset", "override.org", "unset"])
            good = "git@github.com:fx/i.git"
            cfg["git"]["repoUrl"] = good
            (root / "instance/config.yaml").write_text(yaml.safe_dump(cfg))
            proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.splitlines(), ["rc=0", good, "fx.org z2.org", "override.org", "203.0.113.5"])
        self.assertEqual(proc.stderr, "")

    def test_a_config_that_is_not_utf8_is_refused_and_stops_the_script(self):
        """Round 3 (breaker): a config with invalid UTF-8 crashed the loader's Python with a
        traceback and no refusal; the eval of nothing succeeded, and the sourcing stage ran
        on with an unvalidated override. The Python's status is the gate now."""
        proc = self._load(None, raw=b"instance:\n  name: fx\n  x: \xff\xfe PLANTED\n", env_extra={"ORBIT_DOMAIN": "override.org"})
        self.assertIn("rc=2", proc.stdout, proc.stdout + proc.stderr)
        self.assertNotIn("Traceback", proc.stderr); self.assertNotIn("PLANTED", proc.stdout + proc.stderr)
        self.assertIn("instance/config.yaml: cannot be read as YAML (UnicodeDecodeError)", proc.stderr)
        self.assertNotIn("[203.", proc.stdout, "a value was exported after the refusal")

    def test_the_exported_value_is_the_one_that_passed(self):
        """Round 3 (breaker): validation stripped a trailing newline that the export kept,
        so a padded domain reached curl with the newline. The canonical value (stripped,
        no trailing dot) is what is exported, from the file and from an override alike."""
        cfg = {"instance": {"name": "fx"}, "domains": {"primary": " fixture.org.\n", "dev": "dev.fx.org", "dnsZones": ["fx.org"]},
               "registry": "ghcr.io/fx", "git": {"repoUrl": "git@github.com:fx/i.git", "github_org": "fx"},
               "nodes": [{"name": "n1", "ip": " 203.0.113.5 ", "role": "control-plane"}]}
        proc = self._load(cfg)
        self.assertEqual(proc.stdout.strip(), "rc=0 [fixture.org] [203.0.113.5]", proc.stdout + proc.stderr)
        proc = self._load(cfg, env_extra={"ORBIT_DOMAIN": "override.org.\n"})
        self.assertEqual(proc.stdout.strip(), "rc=0 [override.org] [203.0.113.5]", proc.stdout + proc.stderr)

    def test_the_git_fields_are_held_to_github_shapes(self):
        """Round 2 (auditor): the repository URL reaches `git clone` with the minted App
        credential attached, so a URL with userinfo or another origin is refused by field;
        the owner and the registry are held to their shapes too. Both GitHub forms pass."""
        good = {"instance": {"name": "fx"}, "domains": {"primary": "fx.org", "dev": "dev.fx.org", "dnsZones": ["fx.org"]},
                "registry": "ghcr.io/fx", "git": {"repoUrl": "git@github.com:fx/i.git", "github_org": "fx"},
                "nodes": [{"name": "n1", "ip": "203.0.113.5", "role": "control-plane"}]}
        import copy
        for field, value in (("git.repoUrl", "https://user:PLANTED@github.com/fx/i.git"),
                             ("git.repoUrl", "https://PLANTED.example/fx/i.git"),
                             ("git.repoUrl", "git@PLANTED.example:fx/i.git"),
                             ("git.repoUrl", "https://github.com/fx/i.git/../PLANTED"),
                             ("git.github_org", "fx PLANTED"),
                             ("registry", "docker.io/PLANTED")):
            with self.subTest(field=field, value=value):
                cfg = copy.deepcopy(good)
                node = cfg
                for k in field.split(".")[:-1]: node = node[k]
                node[field.split(".")[-1]] = value
                proc = self._load(cfg)
                self.assertIn("rc=2", proc.stdout); self.assertIn(field, proc.stderr)
                self.assertNotIn("PLANTED", proc.stdout + proc.stderr)
        for url in ("git@github.com:fx/i.git", "https://github.com/fx/i.git", "https://github.com/fx/i", "git@github.com:Fx-Org/my.repo_1.git"):
            with self.subTest(url=url):
                cfg = copy.deepcopy(good); cfg["git"]["repoUrl"] = url
                self.assertIn("rc=0", self._load(cfg).stdout)
        # an override is held to the same shape
        proc = self._load(good, env_extra={"ORBIT_REPO_URL": "https://user:PLANTED@github.com/fx/i.git"})
        self.assertIn("rc=2", proc.stdout); self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def _load(self, cfg, env_extra=None, raw=None):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            (root / "instance").mkdir()
            if isinstance(raw, bytes):
                (root / "instance/config.yaml").write_bytes(raw)
            else:
                (root / "instance/config.yaml").write_text(raw if raw is not None else yaml.safe_dump(cfg))
            script = ("source " + str(ROOT / "scripts" / "lib-instance.sh") + "; echo \"rc=$? [$ORBIT_DOMAIN] [$ORBIT_NODE_IP]\"")
            env = {k: v for k, v in os.environ.items() if not k.startswith("ORBIT_")}
            env["ORBIT_INSTANCE_CONFIG"] = str(root / "instance/config.yaml"); env.update(env_extra or {})
            return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60, env=env)

    def test_the_loader_lowercases_a_dns_name_like_init_renders_it(self):
        """B4 (round 6): init renders lowercase; the loader exported FIXTURE.ORG as
        typed, so every live host comparison failed."""
        cfg = {"instance": {"name": "fx"}, "domains": {"primary": " FIXTURE.ORG. ", "dev": "Dev.Fixture.org", "dnsZones": ["Fixture.ORG"]},
               "registry": "ghcr.io/fx", "git": {"repoUrl": "git@github.com:fx/i.git", "github_org": "fx"},
               "nodes": [{"name": "n1", "ip": "203.0.113.5", "role": "control-plane"}]}
        proc = self._load(cfg)
        self.assertIn("rc=0 [fixture.org]", proc.stdout, proc.stdout + proc.stderr)

    def test_the_ssh_key_pointer_is_exported_only_with_a_paths_shape(self):
        """A1 (round 7): the ssh-key pointer file was cat'ed straight into ORBIT_SSH_KEY,
        which a stage puts on an ssh command line. It is data now: one absolute line."""
        cfg = {"instance": {"name": "fx"}, "domains": {"primary": "fx.org", "dev": "dev.fx.org", "dnsZones": ["fx.org"]},
               "registry": "ghcr.io/fx", "git": {"repoUrl": "git@github.com:fx/i.git", "github_org": "fx"},
               "nodes": [{"name": "n1", "ip": "203.0.113.5", "role": "control-plane"}]}
        def load(pointer, env_extra=None):
            with tempfile.TemporaryDirectory() as td:
                root = pathlib.Path(td)
                (root / "instance").mkdir(); (root / "instance/config.yaml").write_text(yaml.safe_dump(cfg))
                home = root / "home"; (home / ".config" / "fx-platform").mkdir(parents=True)
                if pointer is not None:
                    (home / ".config" / "fx-platform" / "ssh-key-path").write_text(pointer)
                script = ("source " + str(ROOT / "scripts" / "lib-instance.sh") + "; echo \"rc=$? [${ORBIT_SSH_KEY:-unset}]\"")
                env = {k: v for k, v in os.environ.items() if not k.startswith("ORBIT_")}
                env["ORBIT_INSTANCE_CONFIG"] = str(root / "instance/config.yaml"); env["HOME"] = str(home); env.update(env_extra or {})
                proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60, env=env)
                return proc, home
        proc, home = load(None)
        self.assertIn(f"rc=0 [{home}/.ssh/id_ed25519]", proc.stdout, proc.stderr)
        proc, _ = load("/keys/fx-ed25519\n")
        self.assertIn("rc=0 [/keys/fx-ed25519]", proc.stdout, proc.stderr)
        for pointer in ("-----BEGIN OPENSSH PRIVATE KEY-----\nPLANTED_KEY_BODY\n", "PLANTED relative/path\n", "/x\x01PLANTED", ""):
            with self.subTest(pointer=pointer[:12]):
                proc, _ = load(pointer)
                self.assertIn("rc=2", proc.stdout); self.assertIn("ssh-key-path", proc.stderr)
                self.assertNotIn("PLANTED", proc.stdout + proc.stderr)
        # an override is data of the same kind
        proc, _ = load(None, {"ORBIT_SSH_KEY": "PLANTED\nsecond line"})
        self.assertIn("rc=2", proc.stdout); self.assertIn("ORBIT_SSH_KEY is not a filesystem path", proc.stderr)
        self.assertNotIn("PLANTED", proc.stderr)
        proc, _ = load("/ignored", {"ORBIT_SSH_KEY": "/override/key"})
        self.assertIn("rc=0 [/override/key]", proc.stdout, proc.stderr)

    def test_a_value_of_the_wrong_shape_stops_the_stage_naming_the_field_only(self):
        """Sweep area 1 (2026-09-13): the domains and addresses reach URLs, ssh and a
        program's text in every stage, so the loader refuses one that is not a DNS name,
        an IP address or a label, names the FIELD and never the value (a wrong value may
        be a pasted credential), and the sourcing script stops (rc 2, nothing exported)."""
        good = {"instance": {"name": "fx"}, "domains": {"primary": "fx.org", "dev": "dev.fx.org", "dnsZones": ["fx.org"]},
                "registry": "ghcr.io/fx", "git": {"repoUrl": "git@github.com:fx/i.git", "github_org": "fx"},
                "nodes": [{"name": "n1", "ip": "203.0.113.5", "role": "control-plane"}]}
        cases = [
            ("domains.primary", lambda c: c["domains"].__setitem__("primary", "grafana.fx.org@PLANTED.example")),
            ("domains.dev", lambda c: c["domains"].__setitem__("dev", 'x"]; PLANTED=1 #')),
            ("domains.dnsZones", lambda c: c["domains"].__setitem__("dnsZones", ["fx.org", "PLANTED zone"])),
            ("nodes[0].ip", lambda c: c["nodes"][0].__setitem__("ip", "203.0.113.5; PLANTED")),
            ("instance.name", lambda c: c["instance"].__setitem__("name", "../PLANTED")),
            ("domains.primary is missing", lambda c: c["domains"].pop("primary")),
        ]
        for field, apply in cases:
            with self.subTest(field=field):
                cfg = yaml.safe_load(yaml.safe_dump(good)); apply(cfg)
                proc = self._load(cfg)
                self.assertIn("rc=2 [] []", proc.stdout, proc.stderr)
                self.assertIn(field, proc.stderr)
                self.assertNotIn("PLANTED", proc.stdout + proc.stderr)
        # an environment override is data of the same kind
        proc = self._load(good, {"ORBIT_DOMAIN": "PLANTED host"})
        self.assertIn("rc=2", proc.stdout); self.assertIn("domains.primary", proc.stderr); self.assertNotIn("PLANTED", proc.stderr)
        # a file that is not YAML: the parser's exception type, never its excerpt
        proc = self._load(None, raw="instance: {name: fx\nPLANTED: [")
        self.assertIn("rc=2", proc.stdout); self.assertIn("cannot be read as YAML", proc.stderr); self.assertNotIn("PLANTED", proc.stderr)
        proc = self._load(good)
        self.assertIn("rc=0 [fx.org] [203.0.113.5]", proc.stdout, proc.stderr)


class LoaderRefusalStopsTheScriptTest(unittest.TestCase):
    """Sweep area 1 round 2 (2026-09-13, breaker): `. lib-instance.sh` returned 2 on a
    refused value and the stage carried on with the unexported (or overridden) value.
    Every script that sources the loader exits on its refusal, and a stage run with a
    malformed override never reaches a row and never prints the value."""

    SOURCING = ("scripts/verify/stage2.sh", "scripts/verify/stage3.sh", "scripts/verify/stage4.sh",
                "scripts/verify/stage5-platform.sh", "scripts/assert-deployed.sh", "scripts/bootstrap-node.sh",
                "scripts/ship.sh", "scripts/bump-and-push.sh", "scripts/monitor-commit.sh")

    def test_every_sourcing_script_exits_on_the_loaders_refusal(self):
        for rel in self.SOURCING:
            lines = [l for l in (ROOT / rel).read_text().splitlines() if re.search(r"^\s*\. .*lib-instance\.sh", l)]
            self.assertEqual(len(lines), 1, f"{rel} sources the loader {len(lines)} times")
            self.assertRegex(lines[0], r"lib-instance\.sh\" \|\| exit 2\s*$", f"{rel}: {lines[0].strip()}")

    def test_a_stage_with_a_malformed_override_stops_before_any_row(self):
        cfg = {"instance": {"name": "fx"}, "domains": {"primary": "fx.org", "dev": "dev.fx.org", "dnsZones": ["fx.org"]},
               "registry": "ghcr.io/fx", "git": {"repoUrl": "git@github.com:fx/i.git", "github_org": "fx"},
               "nodes": [{"name": "n1", "ip": "203.0.113.5", "role": "control-plane"}]}
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            (root / "instance").mkdir(); (root / "instance/config.yaml").write_text(yaml.safe_dump(cfg))
            env = {k: v for k, v in os.environ.items() if not k.startswith("ORBIT_")}
            env.update({"ORBIT_INSTANCE_CONFIG": str(root / "instance/config.yaml"), "ORBIT_DOMAIN": "user:PLANTED@fixture.org",
                        "KUBECONFIG": str(root / "no-such-kubeconfig"), "HOME": str(root)})
            proc = subprocess.run(["bash", str(ROOT / "scripts" / "verify" / "stage2.sh")], capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)
        self.assertIn("domains.primary", proc.stderr)
        self.assertNotIn("passed", proc.stdout, "the stage reached its rows after the refusal:\n" + proc.stdout)
        self.assertNotIn("  ok ", proc.stdout); self.assertNotIn("  FAIL ", proc.stdout)


class Stage2SpliceTest(unittest.TestCase):
    """Sweep area 1 (2026-09-13): the swap row hands ssh the key path and the address as
    quoted words expanded when the row runs; the pointer file's content is one argument
    to ssh, byte for byte, whatever shell syntax it carries. Round 2: no row in the stage
    splices a value into the evaluated text (the inotify row still did)."""

    def test_no_row_splices_a_value_into_the_evaluated_text(self):
        text = (ROOT / "scripts" / "verify" / "stage2.sh").read_text()
        for n, line in enumerate(text.splitlines(), 1):
            self.assertNotRegex(line, r"'\"\$\{?[A-Za-z_]", f"stage2.sh:{n}: a value is spliced: {line.strip()[:100]}")

    def test_the_inotify_row_hands_ssh_the_key_path_as_one_argument(self):
        text = (ROOT / "scripts" / "verify" / "stage2.sh").read_text()
        lines = text.splitlines()
        at = next(i for i, l in enumerate(lines) if "90-orbit-inotify.conf" in l and "ssh" in l)
        row = lines[at]                                  # the node's text lands in a private file (round 24)
        self.assertTrue(row.startswith("ssh "), row)
        self.assertIn('"$ORBIT_SSH_KEY"', row); self.assertIn('"root@$IP"', row); self.assertIn('> "$PERSF"', row)
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td); log = root / "argv.log"
            _write(root / "bin" / "ssh", "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$SSH_LOG\"\n", executable=True)
            key = "k y'\"$(echo PLANTED) `id` $HOME"
            script = ("ok(){ :; }; bad(){ :; }; check(){ if eval \"$2\" >/dev/null 2>&1; then echo ok; else echo bad; fi; }\n"
                      "IP=203.0.113.7; PERSF=$(mktemp)\n" + row + "\n")
            proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60,
                                  env={**os.environ, "PATH": str(root / "bin") + os.pathsep + os.environ["PATH"],
                                       "ORBIT_SSH_KEY": key, "SSH_LOG": str(log)})
            logged = log.read_text() if log.exists() else ""
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(key, logged.splitlines(), logged)
        self.assertNotIn("\nPLANTED\n", logged)

    def test_the_key_path_reaches_ssh_as_one_literal_argument(self):
        text = (ROOT / "scripts" / "verify" / "stage2.sh").read_text()
        # the row is the block that reads ssh into a private file and compares the count (round 21)
        row = text[text.index("SWAPF=$(mktemp)"):text.index("\n", text.index('check "swap is off"'))]
        self.assertNotIn("'\"$", row, "a value is spliced into the evaluated text")
        self.assertIn('"$ORBIT_SSH_KEY"', row); self.assertIn('"root@$IP"', row)
        closed = next(l for l in text.splitlines() if l.startswith('check "6443 still closed'))
        self.assertNotIn("'\"$", closed)
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td); log = root / "argv.log"
            _write(root / "bin" / "ssh", "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$SSH_LOG\"\necho 0\n", executable=True)
            key = "k y'\"$(echo PLANTED) `id` $HOME"
            script = ("ok(){ :; }; bad(){ :; }; check(){ if eval \"$2\" >/dev/null 2>&1; then echo ok; else echo bad; fi; }\n"
                      "IP=203.0.113.7\n" + row + "\n")
            proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60,
                                  env={**os.environ, "PATH": str(root / "bin") + os.pathsep + os.environ["PATH"],
                                       "ORBIT_SSH_KEY": key, "SSH_LOG": str(log)})
            logged = log.read_text() if log.exists() else ""
        self.assertEqual(proc.returncode, 0, proc.stderr)
        args = logged.splitlines()
        self.assertIn(key, args, args)
        self.assertIn("root@203.0.113.7", args)
        self.assertNotIn("\nPLANTED\n", logged)


class Stage2OutputShapeTest(unittest.TestCase):
    """Round 3 (breaker): stage2 repeated what ssh, openssl and the HTTP server printed
    in its failure rows, and compared memory quantities as strings."""

    def _stubs(self, root):
        _write(root / "bin" / "ssh", "#!/bin/sh\necho 'PLANTED-SSH-CREDENTIAL-1234567890'\n", executable=True)
        _write(root / "bin" / "openssl", "#!/bin/sh\ncat > /dev/null; echo 'issuer=CN=PLANTED-CERT-CREDENTIAL-1234567890'\n", executable=True)
        _write(root / "bin" / "curl", "#!/bin/sh\nprev=\"\"; for a in \"$@\"; do [ \"$prev\" = -D ] && printf 'HTTP/1.1 500 PLANTED-HTTP-CREDENTIAL-1234567890\\r\\nlocation: http://x\\r\\n\\r\\n' > \"$a\"; prev=\"$a\"; done\n", executable=True)
        _write(root / "bin" / "kubectl", "#!/bin/sh\ncase \"$*\" in *allocatable.memory*capacity.memory*) printf '%s\\n%s\\n' \"$ALLOC\" \"$CAP\";; *) echo '{}';; esac\n", executable=True)
        _write(root / "bin" / "timeout", "#!/bin/sh\nshift; exec \"$@\"\n", executable=True)

    def _run(self, block, alloc="100Gi", cap="10Gi"):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td); self._stubs(root)
            script = ("ok(){ echo \"  ok    $1\"; }; bad(){ echo \"  FAIL  $1\"; }; check(){ if eval \"$2\" >/dev/null 2>&1; then ok \"$1\"; else bad \"$1\"; fi; }\n"
                      "IP=203.0.113.7; ORBIT_NODE_NAME=n1; ORBIT_DOMAIN=fixture.org; ORBIT_NODE_IP=203.0.113.7; ORBIT_SSH_KEY=/k; WILDCARD=w\n" + block + "\n")
            return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=120,
                                  env={**os.environ, "PATH": str(root / "bin") + os.pathsep + os.environ["PATH"], "ALLOC": alloc, "CAP": cap})

    def test_no_row_repeats_what_a_dependency_printed(self):
        text = (ROOT / "scripts" / "verify" / "stage2.sh").read_text()
        node = text[text.index('echo "== node =="'):text.index('echo "== platform apps =="')]
        edge = text[text.index('echo "== edge =="'):text.index('echo "== live public surfaces =="')]
        for name, block in (("node", node), ("edge", edge)):
            with self.subTest(block=name):
                proc = self._run(block)
                self.assertNotIn("PLANTED", proc.stdout + proc.stderr, proc.stdout + proc.stderr[-2000:])
                self.assertIn("FAIL", proc.stdout)
        proc = self._run(node)
        self.assertIn("inotify max_user_instances = unreadable", proc.stdout, proc.stdout)
        self.assertIn("unrouted host served a certificate whose issuer is not Let's Encrypt", self._run(edge).stdout)
        self.assertIn("http did not permanently redirect to https on the same host (status 500", self._run(edge).stdout)

    def test_a_memory_quantity_never_reaches_an_argument_before_its_shape_is_known(self):
        """Round 4 (breaker): the two node quantities were python ARGUMENTS before the
        shape check ran, so an odd value stood in a process's argv. They go to a scratch
        file now, and the row is built from what the reader returned."""
        text = (ROOT / "scripts" / "verify" / "stage2.sh").read_text()
        node = text[text.index('echo "== node =="'):text.index('echo "== platform apps =="')]
        mem = node[node.index("MEMQ=$(mktemp)"):node.index('rm -f "$MEMQ"')]
        self.assertNotIn('"${ALLOC', mem); self.assertNotIn('"${CAP', mem)
        self.assertNotIn("ALLOC=$(kubectl", node); self.assertNotIn("CAP=$(kubectl", node)
        proc = self._run(node, "PLANTED_QUANTITY_1234567890", "8Gi")
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)
        self.assertIn("FAIL  allocatable is not below capacity", proc.stdout)

    def test_the_sealed_token_file_is_checked_structurally(self):
        """Round 4 (breaker): `! grep` for a 30-letter token passed on a MISSING file and
        on a base64 value (its `=` fails the letter class) — and the path it named was
        stale (the token lives in the sealed plane, clusters/platform/secrets/), so the
        row had passed on nothing for as long as it existed. The file must exist, read
        as YAML, and hold only sops envelopes under data:/stringData:."""
        text = (ROOT / "scripts" / "verify" / "stage2.sh").read_text()
        block = text[text.index("SOPSF=clusters/platform"):text.index('check "no key material tracked in git"')]
        def run(content):
            with tempfile.TemporaryDirectory() as td:
                root = pathlib.Path(td)
                if content is not None:
                    _write(root / "clusters/platform/secrets/cert-manager/cloudflare-api-token.yaml", content)
                script = ("ok(){ echo \"  ok    $1\"; }; bad(){ echo \"  FAIL  $1\"; }\n" + block)
                return subprocess.run(["bash", "-c", script], cwd=root, capture_output=True, text=True, timeout=60).stdout
        self.assertIn("ok    the sealed cert-manager token file holds only sops envelopes (1 value(s))", run(SEALED_TOKEN_FILE))
        self.assertIn("FAIL  the sealed cert-manager token file cannot be read as YAML (FileNotFoundError; a repeated key counts)", run(None))
        self.assertIn("FAIL  the sealed cert-manager token file cannot be read as YAML (ParserError; a repeated key counts)", run("a: [\n"))
        b64 = SEALED_TOKEN_FILE.replace("ENC[AES256_GCM,data:YWJj,iv:ZGVm,tag:Z2hp,type:str]", "UExBTlRFRC1QTEFJTlRFWFQtVE9LRU4=")
        out = run(b64)
        self.assertIn("FAIL  the sealed cert-manager token file holds 1 value(s) under data:/stringData: that are not a sops envelope", out)
        self.assertNotIn("UExBTlRFRC", out)
        bare = SEALED_TOKEN_FILE.replace("ENC[AES256_GCM,data:YWJj,iv:ZGVm,tag:Z2hp,type:str]", "PLANTEDPLANTEDPLANTEDPLANTEDPLANTED1234")
        self.assertIn("FAIL  the sealed cert-manager token file holds 1 value(s)", run(bare))
        self.assertNotIn("PLANTED", run(bare))
        self.assertIn("FAIL  the sealed cert-manager token file carries no data:/stringData: values", run("kind: SopsSecret\nspec: {}\n"))

    def test_the_redirect_must_be_permanent_and_to_the_same_host(self):
        """Round 4 (breaker): a 307 (temporary) and a 301 to another host both passed as
        "permanently redirects to https". 301/308 only, Location on the probed host."""
        text = (ROOT / "scripts" / "verify" / "stage2.sh").read_text()
        block = text[text.index("RHDR=$(mktemp)"):text.index('check "6443 still closed')]
        def run(headers, traced=False):
            with tempfile.TemporaryDirectory() as td:
                root = pathlib.Path(td)
                _write(root / "curl", "#!/bin/sh\nprev=\"\"; for a in \"$@\"; do [ \"$prev\" = -D ] && printf '%b' \"$CURL_HEADERS\" > \"$a\"; prev=\"$a\"; done\n", executable=True)
                script = ("ok(){ echo \"  ok    $1\"; }; bad(){ echo \"  FAIL  $1\"; }\nORBIT_DOMAIN=fixture.org; IP=203.0.113.7\n" + block)
                proc = subprocess.run(["bash", "-xc" if traced else "-c", script], capture_output=True, text=True, timeout=60,
                                      env={**os.environ, "PATH": str(root) + os.pathsep + os.environ["PATH"], "CURL_HEADERS": headers})
                return proc.stdout + proc.stderr if traced else proc.stdout
        good = "ok    http permanently redirects to https on the same host"
        self.assertIn(good, run("HTTP/1.1 301 Moved Permanently\r\nLocation: https://probe.fixture.org/\r\n\r\n"))
        self.assertIn(good, run("HTTP/1.1 308 Permanent Redirect\r\nlocation: https://probe.fixture.org\r\n\r\n"))
        self.assertIn(good, run("HTTP/1.1 301 Moved Permanently\r\nLocation: https://probe.fixture.org:443/x?y=1\r\n\r\n"))
        # B3 (round 8): the headers went through a shell variable a traced shell printed;
        # a Set-Cookie in the answer is a session credential
        out = run("HTTP/1.1 301 Moved Permanently\r\nSet-Cookie: session=PLANTED_SESSION_COOKIE_1234567890; HttpOnly\r\nLocation: https://probe.fixture.org/\r\n\r\n", traced=True)
        self.assertIn(good, out); self.assertNotIn("PLANTED", out)
        for hdr in ("HTTP/1.1 307 Temporary Redirect\r\nLocation: https://probe.fixture.org/\r\n\r\n",
                    "HTTP/1.1 302 Found\r\nLocation: https://probe.fixture.org/\r\n\r\n",
                    "HTTP/1.1 301 Moved Permanently\r\nLocation: https://wrong.fixture.org/\r\n\r\n",
                    "HTTP/1.1 301 Moved Permanently\r\nLocation: https://probe.fixture.org.evil.test/\r\n\r\n",
                    "HTTP/1.1 301 Moved Permanently\r\nLocation: https://probexfixture.org/\r\n\r\n",
                    "HTTP/1.1 301 Moved Permanently\r\nLocation: http://probe.fixture.org/\r\n\r\n"):
            with self.subTest(hdr=hdr.split("\\r")[0][:60]):
                out = run(hdr)
                self.assertIn("FAIL  http did not permanently redirect to https on the same host", out)
                self.assertNotIn("wrong", out); self.assertNotIn("evil", out)

    def test_memory_is_compared_as_a_quantity(self):
        text = (ROOT / "scripts" / "verify" / "stage2.sh").read_text()
        node = text[text.index('echo "== node =="'):text.index('echo "== platform apps =="')]
        self.assertIn("FAIL  allocatable is not below capacity", self._run(node, "100Gi", "10Gi").stdout)
        self.assertIn("FAIL  allocatable is not below capacity", self._run(node, "8Gi", "8Gi").stdout)
        self.assertIn("FAIL  allocatable is not below capacity", self._run(node, "PLANTED", "8Gi").stdout)
        self.assertIn("ok    allocatable (7500Mi) < capacity (8Gi)", self._run(node, "7500Mi", "8Gi").stdout)
        self.assertIn("ok    allocatable (7936000Ki) < capacity (8000000Ki)", self._run(node, "7936000Ki", "8000000Ki").stdout)


class Stage3SelectorsTest(unittest.TestCase):
    """Round 3 (auditor): the WAL selector queried a prod database for a project that
    declares no prod environment, and both it and the namespace count ignored the
    generator's managed-elsewhere exclusion."""

    def _snippet(self, start, end):
        text = (ROOT / "scripts" / "verify" / "stage3.sh").read_text()
        return text[text.index(start) + len(start):text.index(end, text.index(start))]

    def _run_py(self, code, cwd):
        return subprocess.run([sys.executable, "-c", code], cwd=cwd, capture_output=True, text=True, timeout=60).stdout.strip()

    def _tree(self, projects, bucket=True):
        root = pathlib.Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        (root / "instance").mkdir(); (root / "catalog").mkdir()
        (root / "instance/config.yaml").write_text(yaml.safe_dump({"objectStorage": {"buckets": {"platform": "b"}} if bucket else {}}))
        (root / "catalog/projects.yaml").write_text(yaml.safe_dump({"projects": projects}))
        return root

    def test_the_wal_selector_follows_the_generator(self):
        code = self._snippet('WAL_DBS=$(python3 -c "', '" 2>/dev/null)\nif [ -z "${WAL_DBS// /}" ]')
        both = {"id": "a", "database": True, "components": [{"name": "w"}], "environments": {"prod": {}, "dev": {"enabled": True}}}
        dev_only = {"id": "b", "database": True, "components": [{"name": "w"}], "environments": {"dev": {"enabled": True}}}
        elsewhere = {"id": "c", "database": True, "components": [{"name": "w"}], "managed_elsewhere": True, "environments": {"prod": {}}}
        self.assertEqual(self._run_py(code, self._tree([both, dev_only, elsewhere])), "a")
        self.assertEqual(self._run_py(code, self._tree([dev_only, elsewhere])), "NONE")

    def test_the_mutation_harness_finds_a_renamed_intake_receiver(self):
        """Round 3 (auditor): the harness required the receiver to be named orbit-intake,
        so an instance that renamed it (URL and bearer intact) failed with "no plane route
        to mutate". The harness finds the intake the way the checker does: by URL."""
        text = (ROOT / "scripts" / "verify" / "stage3.sh").read_text()
        start = text.index("  python3 - \"$AMSRC\" \"$MUT\" \"$AMCHECK\" 2>/dev/null <<'PY'\n") + len("  python3 - \"$AMSRC\" \"$MUT\" \"$AMCHECK\" 2>/dev/null <<'PY'\n")
        code = text[start:text.index("\nPY\n", start)]
        stack = ROOT / "template" / "provisioning" / "clusters" / "platform" / "apps" / "kube-prometheus-stack.yaml"
        checker = ROOT / "scripts" / "verify" / "am-routes.py"
        with tempfile.TemporaryDirectory() as td:
            src = pathlib.Path(td) / "stack.yaml"; src.write_text(stack.read_text().replace("orbit-intake", "agents-webhook"))
            out = pathlib.Path(td) / "mut"; out.mkdir()
            proc = subprocess.run([sys.executable, "-", str(src), str(out), str(checker)], input=code, capture_output=True, text=True, timeout=120)
            self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
            self.assertTrue(list(out.glob("*.yaml")), "no mutant was emitted")

    def test_the_namespace_count_follows_the_generator(self):
        code = self._snippet('EXPECT_NS=$(python3 -c "', '" 2>/dev/null)\n')
        both = {"id": "a", "components": [{"name": "w"}], "environments": {"prod": {}, "dev": {"enabled": True}}}
        elsewhere = {"id": "c", "components": [{"name": "w"}], "managed_elsewhere": True, "environments": {"prod": {}}}
        self.assertEqual(self._run_py(code, self._tree([both, elsewhere])), "2")


class Stage5DoorAndNetpolTest(unittest.TestCase):
    """Round 3 (auditor): the netpol row read an ingress rule with no `from:` as zero peers
    and ignored an additive policy with an empty podSelector; the intake door rows put
    response bodies in shell variables; a dashboard uid reached an authenticated URL
    unvalidated."""

    def _netpol(self, mutate):
        text = (ROOT / "scripts" / "verify" / "stage5-platform.sh").read_text()
        start = text.index("  WOUT=$(python3 - \"$NETPOL\" <<'PY'\n") + len("  WOUT=$(python3 - \"$NETPOL\" <<'PY'\n")
        code = text[start:text.index("\nPY\n", start)]
        src = ROOT / "template" / "provisioning" / "clusters" / "platform" / "config" / "monitoring" / "netpol.yaml"
        docs = [d for d in yaml.safe_load_all(src.read_text()) if isinstance(d, dict)]
        docs = mutate(docs)
        with tempfile.TemporaryDirectory() as td:
            f = pathlib.Path(td) / "netpol.yaml"; f.write_text(yaml.safe_dump_all(docs))
            return subprocess.run([sys.executable, "-c", code, str(f)], capture_output=True, text=True, timeout=60)

    def _am(self, docs):
        return next(d for d in docs if ((d.get("spec") or {}).get("podSelector") or {}).get("matchLabels", {}).get("app.kubernetes.io/name") == "alertmanager")

    def test_the_shipped_netpol_is_green(self):
        proc = self._netpol(lambda d: d)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_an_unrestricted_ingress_rule_is_red(self):
        def mutate(docs):
            self._am(docs)["spec"]["ingress"] = [{}]; return docs
        proc = self._netpol(mutate)
        self.assertNotEqual(proc.returncode, 0, proc.stdout); self.assertIn("admits every peer", proc.stdout)

    def test_an_additive_policy_selecting_every_pod_is_red(self):
        def mutate(docs):
            docs.append({"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": {"name": "wide", "namespace": "monitoring"},
                         "spec": {"podSelector": {}, "ingress": [{"ports": [{"port": 9093}]}]}}); return docs
        proc = self._netpol(mutate)
        self.assertNotEqual(proc.returncode, 0, proc.stdout); self.assertIn("'wide'", proc.stdout)

    def test_a_response_body_is_never_a_shell_value(self):
        text = (ROOT / "scripts" / "verify" / "stage5-platform.sh").read_text()
        self.assertNotIn("R=$(curl", text); self.assertNotIn("TR=$(curl", text); self.assertNotIn("sed '$d'", text)
        loop = text[text.index("    for p in healthz metrics; do"):text.index("    RC=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 \"https://$IHOST/\")")]
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td); tmp = root / "tmp"; tmp.mkdir()
            _write(root / "bin" / "curl", "#!/bin/sh\nout=; for a; do case \"$p\" in -o) out=$a;; esac; p=$a; done\nprintf 'PLANTED-BODY-CREDENTIAL orbit_intake' > \"$out\"; printf 200\n", executable=True)
            script = "ok(){ echo \"  ok    $1\"; }; bad(){ echo \"  FAIL  $1\"; }\nIHOST=door.fixture.org\n" + loop + "\n"
            proc = subprocess.run(["bash", "-x", "-c", script], capture_output=True, text=True, timeout=120,
                                  env={**os.environ, "PATH": str(root / "bin") + os.pathsep + os.environ["PATH"], "TMP": str(tmp)})
        self.assertIn("FAIL", proc.stdout)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr, proc.stderr[-2000:])

    def test_a_dashboard_uid_of_another_shape_is_neither_placed_nor_printed(self):
        text = (ROOT / "scripts" / "verify" / "stage5-platform.sh").read_text()
        start = text.index("      UIDS=$(python3 -c \"\n") + len("      UIDS=$(python3 -c \"\n")
        code = text[start:text.index("\" \"$f\" 2>/dev/null)", start)]
        with tempfile.TemporaryDirectory() as td:
            f = pathlib.Path(td) / "dash.yaml"
            f.write_text(yaml.safe_dump({"data": {"a.json": json.dumps({"uid": "good-uid_1"}),
                                                  "b.json": json.dumps({"uid": "../../datasources?token=PLANTED-UID-CREDENTIAL"})}}))
            proc = subprocess.run([sys.executable, "-c", code, str(f)], capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.stdout.split(), ["good-uid_1"], proc.stdout + proc.stderr)
        # and the static row reports it by file and key, never by value
        start = text.index("  DSHAPE=$(python3 - \"${DASHLIST[@]}\" <<'PY'\n") + len("  DSHAPE=$(python3 - \"${DASHLIST[@]}\" <<'PY'\n")
        static = text[start:text.index("\nPY\n", start)]
        with tempfile.TemporaryDirectory() as td:
            f = pathlib.Path(td) / "dashboard-x.yaml"
            f.write_text(yaml.safe_dump({"data": {"b.json": json.dumps({"uid": "../../datasources?token=PLANTED-UID-CREDENTIAL", "panels": []})}}))
            proc = subprocess.run([sys.executable, "-", str(f)], input=static, capture_output=True, text=True, timeout=60)
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("not a Grafana uid", proc.stdout); self.assertNotIn("PLANTED", proc.stdout + proc.stderr)


class Stage3PrebootAndSshTest(unittest.TestCase):
    """Round 2 (2026-09-13, auditor): two live sections of stage3 ran outside the pre-boot
    guard (the referenced-Secret rows and the protected-environment probes), so a fresh
    instance with a dev environment failed and made live requests before bootstrap; and
    the node ssh command was a word-split string, so a key path with a space broke it."""

    def test_the_live_sections_sit_inside_the_preboot_guard(self):
        text = (ROOT / "scripts" / "verify" / "stage3.sh").read_text()
        guard = text.index('if [ "$PREBOOT" = 1 ]; then\n  ok "pre-boot: the referenced-Secret and protected-environment')
        self.assertLess(guard, text.index('echo "== every referenced Secret actually exists =="'))
        self.assertLess(text.index('echo "== live: every protected environment is protected =="'),
                        text.index("fi  # PREBOOT: referenced Secrets + protected environments"))

    def test_a_preboot_instance_with_a_dev_environment_makes_no_probe(self):
        """The stage, pre-boot (no kubeconfig file), with a catalog declaring an enabled
        dev environment and a curl that records every call: no protected-host probe runs,
        the stage exits 0."""
        if not shutil.which("bash"):
            self.skipTest("bash is needed")
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            for rel in ("scripts/verify", "scripts", "template/provisioning/clusters/platform/apps",
                        "template/provisioning/clusters/platform/config/monitoring", "catalog", "instance", "bin", "clusters/platform/apps"):
                (root / rel).mkdir(parents=True, exist_ok=True)
            for rel in ("scripts/verify/stage3.sh", "scripts/lib-instance.sh", "scripts/verify/am-routes.py", "scripts/verify/no-instance-strings.py",
                        "scripts/verify/am-inline-credentials.py", "scripts/verify/shown.py", "scripts/catalog-sync.py", "core-manifest.txt"):
                if (ROOT / rel).exists():
                    shutil.copy(ROOT / rel, root / rel)
            (root / "catalog/projects.yaml").write_text(yaml.safe_dump({"projects": [{"id": "hub", "components": [], "environments": {
                "prod": {"branch": "main"}, "dev": {"enabled": True, "branch": "dev"}}}], "agents": []}))
            (root / "instance/config.yaml").write_text(yaml.safe_dump(
                {"instance": {"name": "fx"}, "domains": {"primary": "fx.org", "dev": "dev.fx.org", "dnsZones": ["fx.org"]},
                 "registry": "ghcr.io/fx", "git": {"repoUrl": "git@github.com:fx/i.git", "github_org": "fx"},
                 "nodes": [{"name": "n1", "ip": "203.0.113.5", "role": "control-plane"}]}))
            _write(root / "bin" / "curl", "#!/bin/sh\necho \"curl $*\" >> \"$CURL_LOG\"\necho 000\n", executable=True)
            env = {k: v for k, v in os.environ.items() if not k.startswith("ORBIT_")}
            env.update({"PATH": str(root / "bin") + os.pathsep + os.environ["PATH"], "KUBECONFIG": str(root / "absent"),
                        "HOME": str(root), "CURL_LOG": str(root / "curl.log"), "ORBIT_INSTANCE_CONFIG": str(root / "instance/config.yaml")})
            proc = subprocess.run(["bash", "scripts/verify/stage3.sh"], cwd=root, capture_output=True, text=True, timeout=300, env=env)
            log = (root / "curl.log").read_text() if (root / "curl.log").exists() else ""
        self.assertNotIn("hub.dev.fx.org", log, "a protected-host probe ran pre-boot:\n" + log)
        self.assertNotIn("without credentials", proc.stdout)
        self.assertIn("pre-boot: the referenced-Secret and protected-environment", proc.stdout)

    def test_the_node_ssh_command_is_an_array(self):
        text = (ROOT / "scripts" / "verify" / "stage3.sh").read_text()
        # round 22 (auditor A2): no legacy SSH_KEY/HOST override — only the loader's validated values
        self.assertIn('SSHN=(ssh -o BatchMode=yes -o ConnectTimeout=15 -o ConnectionAttempts=3 -i "$ORBIT_SSH_KEY" "root@$ORBIT_NODE_IP")', text)
        self.assertNotIn("${SSH_KEY:-", text); self.assertNotIn("${HOST:-", text)
        self.assertNotIn("$SSHN ", text); self.assertNotIn("$SSHN\n", text)
        self.assertGreaterEqual(text.count('"${SSHN[@]}"'), 5)
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td); log = root / "argv.log"
            _write(root / "bin" / "ssh", "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$SSH_LOG\"\n", executable=True)
            key = "k y'\"$(echo PLANTED) `id`"
            line = next(l for l in text.splitlines() if l.startswith("SSHN=("))
            proc = subprocess.run(["bash", "-c", line + '\n"${SSHN[@]}" "true"\n'], capture_output=True, text=True, timeout=60,
                                  env={**os.environ, "PATH": str(root / "bin") + os.pathsep + os.environ["PATH"],
                                       "ORBIT_SSH_KEY": key, "ORBIT_NODE_IP": "203.0.113.7", "SSH_LOG": str(log)})
            logged = log.read_text() if log.exists() else ""
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(key, logged.splitlines(), logged); self.assertIn("root@203.0.113.7", logged.splitlines())
        self.assertNotIn("\nPLANTED\n", logged)


class Stage3SealedProducerTest(unittest.TestCase):
    def test_the_instance_row_points_at_the_sealed_tree(self):
        """Sweep area 1 (2026-09-13): the bearer's producer lives in the instance's sealed
        tree; the rendered config file holds only a Namespace, so pointing the checker at
        it keyed every producer row off."""
        text = (ROOT / "scripts" / "verify" / "stage3.sh").read_text()
        self.assertIn("AM_SEALED=clusters/platform/secrets/monitoring\n", text)
        self.assertRegex(text, r'am_rows "alertmanager\(instance\)" app "\$AM_INSTANCE" catalog/projects.yaml "\$AM_SEALED"')


class Stage3ScratchTest(unittest.TestCase):
    def test_the_render_values_live_in_a_file_of_the_runs_own(self):
        """Sweep area 1 (2026-09-13): two verifications at once must not read each other's
        extracted values; the file comes from mktemp, never a fixed /tmp path."""
        text = (ROOT / "scripts" / "verify" / "stage3.sh").read_text()
        self.assertNotIn("/tmp/orbit-vals", text)
        self.assertIn("VALS=$(mktemp)", text)
        self.assertIn('-f "$VALS"', text)


class Stage5ArgvAndRulerTest(unittest.TestCase):
    def test_a_cluster_value_reaches_python_as_an_argument_never_as_program_text(self):
        """Sweep area 1 (2026-09-13, breaker): a value read from the cluster (a query
        result) was spliced into a python one-liner's source; a quote in it would have
        been code. Every numeric comparison takes the value as sys.argv[1]."""
        text = (ROOT / "scripts" / "verify" / "stage5-platform.sh").read_text()
        self.assertNotRegex(text, r"float\('\$")
        self.assertEqual(text.count("float(sys.argv[1])"), 5)
        proc = subprocess.run(["python3", "-c", "import sys;sys.exit(0 if float(sys.argv[1]) > 14 else 1)", "0')+(PLANTED"],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 1); self.assertNotIn("SyntaxError", proc.stderr)

    def _ruler(self, got, disc, err, sent="0"):
        text = (ROOT / "scripts" / "verify" / "stage5-platform.sh").read_text()
        block = text[text.index("      # A healthy installation has NO logs alert firing"):text.index("\n    else\n      keyoff_absent \"no logs trigger deployed")]
        script = ("ok(){ echo \"  ok    $1\"; }; bad(){ echo \"  FAIL  $1\"; }\n"
                  "promval(){ case \"$1\" in *discovered*) echo \"$RD\";; *sent_total*) echo \"$RS\";; *) echo \"$RE\";; esac; }\n"
                  f"WANT='OrbitAgentOomSpike'; HIT='{1 if 'OrbitAgentOomSpike' in got.split() else 0}'; RD='{disc}'; RE='{err}'; RS='{sent}'\n" + block + "\n")
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60).stdout

    def test_the_ruler_proof_does_not_need_an_incident_but_needs_traffic(self):
        """Sweep area 1 (2026-09-13): a healthy installation has no logs alert firing, so
        the positive half of the loki->:9093 proof cannot demand one. Round 2 (auditor):
        an idle notifier proves nothing either — a blocked path shows in no counter until
        something is sent. So: a firing alert, or sent > 0 with nothing failed, passes;
        discovered-but-silent is reported UNVERIFIED (neither pass nor fail); a failure
        counter, an undiscovered Alertmanager or an unanswered query fails."""
        self.assertIn("(the positive half)", self._ruler("OrbitAgentOomSpike", "0", "0"))
        self.assertIn("traffic counters", self._ruler("", "1", "0", sent="4"))
        self.assertIn("[UNVERIFIED]", self._ruler("", "1", "0", sent="0"))
        self.assertNotIn("FAIL", self._ruler("", "1", "0", sent="0"))
        self.assertIn("FAIL", self._ruler("", "0", "0"))
        self.assertIn("FAIL", self._ruler("", "1", "3", sent="4"))
        self.assertIn("FAIL", self._ruler("", "1", "3"))
        self.assertIn("FAIL", self._ruler("", "QUERYERR", "0"))
        self.assertIn("FAIL", self._ruler("", "1", "QUERYERR", sent="4"))


class Stage2PluginStderrTest(unittest.TestCase):
    """A kubeconfig's credential plugin writes to stderr, and stderr is the plugin's, not
    the stage's: the node rows run kubectl with a plugin that prints a planted value and
    fails, and the stage's own stderr carries none of it."""

    def test_the_node_rows_close_stderr_on_kubectl(self):
        text = (ROOT / "scripts" / "verify" / "stage2.sh").read_text()
        block = text[text.index("echo \"== node ==\""):text.index("\n", text.index("  bad \"allocatable is not below capacity"))]
        block = "ok(){ echo \"  ok    $1\"; }; bad(){ echo \"  FAIL  $1\"; }\nORBIT_NODE_NAME=n\n" + block
        if not shutil.which("kubectl"):
            self.skipTest("kubectl is not installed here")
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            plugin = root / "plugin.sh"
            _write(plugin, "#!/bin/sh\necho PLANTED-PLUGIN-PASSWORD >&2\nexit 1\n", executable=True)
            _write(root / "kubeconfig",
                   "apiVersion: v1\nkind: Config\nclusters:\n- name: c\n  cluster: {server: 'https://127.0.0.1:9', insecure-skip-tls-verify: true}\n"
                   "contexts:\n- name: c\n  context: {cluster: c, user: u}\ncurrent-context: c\nusers:\n- name: u\n  user:\n"
                   f"    exec: {{apiVersion: client.authentication.k8s.io/v1, command: '{plugin}', interactiveMode: Never}}\n")
            proc = subprocess.run(["bash", "-c", block], cwd=td, capture_output=True, text=True, timeout=120,
                                  env={**os.environ, "KUBECONFIG": str(root / "kubeconfig")})
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr, proc.stderr)


class Stage3IntakeGuardTest(unittest.TestCase):
    """The live intake guard's findings name the Ingress, host and backend, never a path
    (a path can carry a token); the project count for the managed-pod floor counts the
    projects this cluster serves."""

    def test_the_live_intake_guard_repeats_no_path(self):
        text = (ROOT / "scripts" / "verify" / "stage3.sh").read_text()
        start = text.index("        for p in ((rule.get(\"http\") or {}).get(\"paths\") or []):")
        end = text.index("if not items and wired:")
        helpers = text[text.index("NAME = re.compile(r\"[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?\")\nHOST = "):text.index("for ing in items:\n    name = shaped(")]
        body = text[start:end]
        pre = ("import re\nimport shown as display\n" + helpers + "bad = []\nINTAKE_SVC = 'orbit-agents-intake'\nname = 'door'\nhost = 'intake.example.org'\n"
               "rule = {'http': {'paths': ["
               "{'path': '/webhook/PLANTED-PATH-TOKEN', 'pathType': 'Prefix', 'backend': {'service': {'name': 'orbit-agents-intake'}}},"
               "{'path': '/PLANTED-OTHER', 'pathType': 'Exact', 'backend': {'service': {'name': 'impostor-intake'}}}]}}\n"
               "if True:\n    if True:\n")
        proc = subprocess.run([sys.executable, "-c", pre + body + "\nprint('\\n'.join(bad))"], capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("door publishes a path rule on intake.example.org that is not Prefix /webhook/github", proc.stdout)
        self.assertIn("-> Service impostor-intake, which is not the intake", proc.stdout)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_the_protected_hosts_skip_projects_managed_elsewhere(self):
        text = (ROOT / "scripts" / "verify" / "stage3.sh").read_text()
        code = text.split("PROTHOSTS=$(python3 -c \"\n", 1)[1].split("\" 2>\"$PROTBAD\"); PROTRC=$?", 1)[0].replace("\\\"", "\"")
        self.assertNotIn("$ORBIT_DEV_DOMAIN", code, "the dev domain is spliced into program text")
        with tempfile.TemporaryDirectory() as td:
            _write(pathlib.Path(td) / "catalog" / "projects.yaml",
                   "projects:\n  - {id: a, environments: {dev: {enabled: true}}}\n"
                   "  - {id: b, managed_elsewhere: true, environments: {dev: {enabled: true, hostnames: [outside.invalid]}}}\n"
                   "  - {id: c, hosted: external, environments: {dev: {enabled: true, hostnames: [ext.invalid]}}}\n"
                   # the generator's exposure semantics (catalog-sync env_hostnames): no door, no probe
                   "  - {id: d, environments: {dev: {enabled: true, expose: false}}}\n"
                   "  - {id: e, environments: {dev: {enabled: true, hostnames: []}}}\n"
                   "  - {id: f, environments: {dev: {enabled: true, hostnames: [f.dev.example], customDomains: [{host: F.custom.example}]}}}\n")
            env = {**os.environ, "ORBIT_DEV_DOMAIN": "dev.example"}
            proc = subprocess.run([sys.executable, "-c", code], cwd=td, capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(proc.stdout.split(), ["a.dev.example", "f.dev.example", "f.custom.example"], proc.stderr)

    def test_the_protected_hosts_carry_the_door_path(self):
        # V1 §8: a dev environment whose only web route is /api has no router (and so no
        # basic-auth middleware) at '/': the probe of the bare host read Traefik's 404 and
        # failed a correctly guarded door. The probe goes where the generator's door_path says.
        text = (ROOT / "scripts" / "verify" / "stage3.sh").read_text()
        code = text.split("PROTHOSTS=$(python3 -c \"\n", 1)[1].split("\" 2>\"$PROTBAD\"); PROTRC=$?", 1)[0].replace("\\\"", "\"")
        with tempfile.TemporaryDirectory() as td:
            _write(pathlib.Path(td) / "catalog" / "projects.yaml",
                   "projects:\n"
                   "  - {id: a, environments: {dev: {enabled: true}}, components: [{name: api, kind: web, port: 8080, path: /api, probePath: /healthz}, {name: w, kind: worker, port: 8080}]}\n"
                   "  - {id: b, environments: {dev: {enabled: true}}, components: [{name: site, kind: web, port: 80}, {name: api, kind: web, port: 8080, path: /api}]}\n"
                   "  - {id: c, environments: {dev: {enabled: true}}, components: [{name: api, kind: web, port: 8080, path: '/x?y=1'}]}\n")
            env = {**os.environ, "ORBIT_DEV_DOMAIN": "dev.example"}
            proc = subprocess.run([sys.executable, "-c", code], cwd=td, capture_output=True, text=True, timeout=60, env=env)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.split(), ["a.dev.example/api/healthz", "b.dev.example"], proc.stderr)
        self.assertIn("projects[2] door path is not a plain URL path", proc.stderr)
        self.assertNotIn("y=1", proc.stderr)

    def test_the_project_count_serves_only_self_hosted_projects(self):
        text = (ROOT / "scripts" / "verify" / "stage3.sh").read_text()
        code = text.split("PROJCOUNT=$(python3 -c \"\n", 1)[1].split("\" 2>/dev/null || echo 'UNREADABLE(no-python)')", 1)[0]
        with tempfile.TemporaryDirectory() as td:
            _write(pathlib.Path(td) / "catalog" / "projects.yaml",
                   "projects:\n  - {id: a}\n  - {id: b, hosted: external}\n  - {id: c, managed_elsewhere: true}\n"
                   "agents:\n  - {name: x, enabled: false}\n")
            proc = subprocess.run([sys.executable, "-c", code], cwd=td, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.stdout.strip(), "1", proc.stderr)


class Stage2CatalogRefusalTest(unittest.TestCase):
    """The hostname enumerator's refusals land in a failing row; a catalog entry can carry
    an env block or a pasted token, so a refusal names the file, entry and field only.
    The enumerator runs as the stage runs it, over four malformed catalogs."""

    def test_a_refusal_never_repeats_the_entry(self):
        text = (ROOT / "scripts" / "verify" / "stage2.sh").read_text()
        body = text[text.index("catalog_hosts() {"):]
        body = body[:body.index("\nPY\n}") + len("\nPY\n}")]
        planted = "PLANTED-CATALOG-VALUE"
        catalogs = {
            "no id": ("projects:\n  - env: {API_TOKEN: %s}\n" % planted, "has a project with no id (entry 1)"),
            "bad id": ("projects:\n  - id: '%s'\n" % planted, "entry 1: the project id is not an id"),
            "bad hostnames": ("projects:\n  - id: p\n    environments: {prod: {hostnames: '%s'}}\n" % planted, "p: hostnames is not a list"),
            "bad host": ("projects:\n  - id: p\n    environments: {prod: {hostnames: ['%s bad']}}\n" % planted, "p/prod: hostname 1 is not a DNS name"),
            "not yaml": ("projects: [%s\n" % planted, "did not parse ("),
        }
        for label, (cat, reason) in catalogs.items():
            with tempfile.TemporaryDirectory() as td:
                tmp = pathlib.Path(td)
                shutil.copytree(ROOT / "scripts", tmp / "scripts")
                _write(tmp / "catalog" / "projects.yaml", cat)
                proc = subprocess.run(["bash", "-c", body + "\ncatalog_hosts catalog/projects.yaml", "scripts/verify/stage2.sh"], cwd=tmp,
                                      env={**os.environ, "ORBIT_DOMAIN": "fixture.org"},
                                      capture_output=True, text=True, timeout=60)
            self.assertNotEqual(proc.returncode, 0, f"{label}: the enumerator accepted it: {proc.stdout}")
            self.assertNotIn(planted, proc.stdout + proc.stderr, f"{label}: the refusal repeats the entry: {proc.stderr}")
            self.assertIn(reason, proc.stderr, f"{label}: the refusal does not name the field: {proc.stderr}")


class Stage2GeneratedSurfaceTest(unittest.TestCase):
    """Behavioural: run the real stage2.sh with stubbed externals and synthetic
    configs, and assert which surfaces get probed from the stub's log.

    The surface is whatever scripts/catalog-sync.py env_hostnames renders for
    prod with the instance domains as defaults — never the bare apex unless the
    catalog explicitly declares it. An empty catalog therefore has no surface;
    expose:false, external and managed projects contribute no default.
    Malformed, missing and null structures fail closed; whitespace-invalid
    hostnames fail closed without splitting; normalised equivalents probe once.
    """

    def _fixture(self, catalog_text):
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="stage2-surface-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        _write(tmp / "instance" / "config.yaml", CONFIG_YAML)
        _write(tmp / "clusters" / "platform" / "secrets" / "cert-manager" / "cloudflare-api-token.yaml", SEALED_TOKEN_FILE)
        if catalog_text is not None:
            _write(tmp / "catalog" / "projects.yaml", catalog_text)
        stubs = {"kubectl": KUBECTL_STUB, "ssh": SSH_STUB, "curl": CURL_STUB,
                 "openssl": OPENSSL_STUB, "timeout": TIMEOUT_STUB,
                 "git": GIT_STUB}
        for name, body in stubs.items():
            _write(tmp / "stubs" / "bin" / name, body, executable=True)
        (tmp / "curllog").mkdir()
        # Non-empty so the stage takes the live path; every live call is stubbed.
        _write(tmp / "kubeconfig", "dummy")
        return tmp

    def _run_stage(self, tmp, fail_host=""):
        env = dict(os.environ,
                   PATH=str(tmp / "stubs" / "bin") + os.pathsep + os.environ["PATH"],
                   KUBECONFIG=str(tmp / "kubeconfig"),
                   ORBIT_INSTANCE_CONFIG=str(tmp / "instance" / "config.yaml"),
                   CURL_LOG=str(tmp / "curllog"),
                   CURL_FAIL_HOST=fail_host)
        return subprocess.run(["bash", str(STAGE)], cwd=tmp,
                              capture_output=True, text=True, env=env)

    def _probed(self, tmp):
        log = tmp / "curllog" / "log"
        return log.read_text().split() if log.exists() else []

    def test_it_parses(self):
        proc = subprocess.run(["bash", "-n", str(STAGE)],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_a_node_that_takes_passwords_on_ssh_is_red(self):
        """A rebuilt server comes from the provider with a root password and password
        sign-in on (docs/V1.md §8): sshd's own effective configuration is what is asked."""
        tmp = self._fixture(EMPTY_CATALOG)
        proc = self._run_stage(tmp)
        self.assertIn("ok    ssh on the node takes keys only (passwordauthentication no)", proc.stdout, proc.stdout[-1500:])
        os.environ["SSHD_PWAUTH"] = "yes"
        try:
            proc = self._run_stage(tmp)
        finally:
            del os.environ["SSHD_PWAUTH"]
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("FAIL  ssh on the node takes PASSWORDS (passwordauthentication yes)", proc.stdout)
        os.environ["SSHD_PWAUTH"] = "maybe PLANTED_NODE_TEXT"
        try:
            proc = self._run_stage(tmp)
        finally:
            del os.environ["SSHD_PWAUTH"]
        self.assertIn("FAIL  sshd's effective configuration on the node could not be read", proc.stdout)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_a_cluster_dns_that_cannot_reach_a_resolver_is_red(self):
        """The rebuilt test server (docs/V1.md §8): CoreDNS forwarding to IPv6 resolvers its
        pod network cannot reach. Counted from its own log; the log's text is not repeated."""
        tmp = self._fixture(EMPTY_CATALOG)
        proc = self._run_stage(tmp)
        self.assertIn("ok    the cluster DNS reached every resolver it asked in the last 10 minutes", proc.stdout, proc.stdout[-1500:])
        for env, says in (({"COREDNS_UNREACHABLE": "3"}, "FAIL  the cluster DNS could not reach a resolver 3 time(s) in the last 10 minutes"),
                          ({"COREDNS_LOG_RC": "1"}, "FAIL  the cluster DNS's log could not be read")):
            os.environ.update(env)
            try:
                proc = self._run_stage(tmp)
            finally:
                for k in env:
                    del os.environ[k]
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn(says, proc.stdout)
            self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_empty_catalog_probes_nothing(self):
        """An empty catalog has no project surface: nothing probed, stage green."""
        tmp = self._fixture(EMPTY_CATALOG)
        proc = self._run_stage(tmp)
        self.assertEqual(self._probed(tmp), [],
                         "an empty catalog probed something:\n" + proc.stdout)
        self.assertIn("no exposed prod hostnames declared", proc.stdout)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_generator_default_hostname_is_probed(self):
        """prod:{} defaults to <id>.<primary> per catalog-sync.py env_hostnames."""
        tmp = self._fixture(DEFAULT_CATALOG)
        proc = self._run_stage(tmp)
        self.assertEqual(self._probed(tmp), ["https://portal.fixture.org"],
                         proc.stdout)
        self.assertIn("portal.fixture.org answers (200)", proc.stdout)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_explicit_apex_is_preserved(self):
        """A catalog that genuinely declares the apex still probes it, once."""
        tmp = self._fixture(EXPLICIT_APEX_CATALOG)
        proc = self._run_stage(tmp)
        self.assertEqual(self._probed(tmp), ["https://fixture.org"],
                         proc.stdout)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_declaration_order_with_explicit_overlap_probes_once(self):
        """Catalog order wins: apex declared second stays second, each host once."""
        tmp = self._fixture(DECLARATION_ORDER_CATALOG)
        proc = self._run_stage(tmp)
        self.assertEqual(self._probed(tmp), ["https://app.fixture.org",
                                             "https://fixture.org",
                                             "https://shop.fixture.org"],
                         proc.stdout)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_no_default_for_expose_false(self):
        tmp = self._fixture(EXPOSE_FALSE_CATALOG)
        proc = self._run_stage(tmp)
        self.assertEqual(self._probed(tmp), [],
                         "expose:false grew a default surface:\n" + proc.stdout)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_expose_false_with_hostnames_fails_closed(self):
        tmp = self._fixture(EXPOSE_FALSE_CONFLICT_CATALOG)
        proc = self._run_stage(tmp)
        self.assertIn("could not enumerate", proc.stdout)
        self.assertNotEqual(proc.returncode, 0,
                            "expose:false with hostnames exited 0")
        self.assertIn("the host enumerator refuses a malformed catalog",
                      proc.stdout)

    def test_no_default_for_external(self):
        tmp = self._fixture(EXTERNAL_CATALOG)
        proc = self._run_stage(tmp)
        self.assertEqual(self._probed(tmp), [],
                         "an external project grew a local surface:\n" + proc.stdout)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_no_default_for_managed_elsewhere(self):
        tmp = self._fixture(MANAGED_CATALOG)
        proc = self._run_stage(tmp)
        self.assertEqual(self._probed(tmp), [],
                         "a managed_elsewhere project grew a local surface:\n" + proc.stdout)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_project_without_prod_has_no_surface(self):
        """A missing prod env contributes nothing — only prod:{} defaults."""
        tmp = self._fixture(DEV_ONLY_CATALOG)
        proc = self._run_stage(tmp)
        self.assertEqual(self._probed(tmp), [],
                         "a dev-only project grew a prod surface:\n" + proc.stdout)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_malformed_main_catalog_fails_closed_but_runs_tail(self):
        tmp = self._fixture(MALFORMED_YAML_CATALOG)
        proc = self._run_stage(tmp)
        self.assertIn("could not enumerate", proc.stdout)
        self.assertEqual(self._probed(tmp), [],
                         "a malformed catalog still probed:\n" + proc.stdout)
        self.assertNotEqual(proc.returncode, 0,
                            "a malformed catalog exited 0")
        self.assertIn("the host enumerator refuses a malformed catalog",
                      proc.stdout)

    def test_missing_main_catalog_fails_closed_but_runs_tail(self):
        tmp = self._fixture(None)
        proc = self._run_stage(tmp)
        self.assertIn("could not enumerate", proc.stdout)
        self.assertEqual(self._probed(tmp), [])
        self.assertNotEqual(proc.returncode, 0,
                            "a missing catalog exited 0")
        self.assertIn("the host enumerator refuses a malformed catalog",
                      proc.stdout)

    def test_null_main_catalog_fails_closed_but_runs_tail(self):
        tmp = self._fixture(NULL_CATALOG)
        proc = self._run_stage(tmp)
        self.assertIn("could not enumerate", proc.stdout)
        self.assertEqual(self._probed(tmp), [])
        self.assertNotEqual(proc.returncode, 0,
                            "a null catalog exited 0")
        self.assertIn("the host enumerator refuses a malformed catalog",
                      proc.stdout)

    def test_missing_projects_key_fails_closed(self):
        tmp = self._fixture(MISSING_PROJECTS_CATALOG)
        proc = self._run_stage(tmp)
        self.assertIn("could not enumerate", proc.stdout)
        self.assertEqual(self._probed(tmp), [])
        self.assertNotEqual(proc.returncode, 0,
                            "a catalog with no projects key exited 0")
        self.assertIn("the host enumerator refuses a malformed catalog",
                      proc.stdout)

    def test_null_projects_fails_closed(self):
        tmp = self._fixture(NULL_PROJECTS_CATALOG)
        proc = self._run_stage(tmp)
        self.assertIn("could not enumerate", proc.stdout)
        self.assertEqual(self._probed(tmp), [])
        self.assertNotEqual(proc.returncode, 0,
                            "a catalog with null projects exited 0")
        self.assertIn("the host enumerator refuses a malformed catalog",
                      proc.stdout)

    def test_null_project_entry_fails_closed(self):
        tmp = self._fixture(NULL_ENTRY_CATALOG)
        proc = self._run_stage(tmp)
        self.assertIn("could not enumerate", proc.stdout)
        self.assertEqual(self._probed(tmp), [])
        self.assertNotEqual(proc.returncode, 0,
                            "a catalog with a null project exited 0")
        self.assertIn("the host enumerator refuses a malformed catalog",
                      proc.stdout)

    def test_non_mapping_project_entry_fails_closed(self):
        tmp = self._fixture(MALFORMED_PROJECT_CATALOG)
        proc = self._run_stage(tmp)
        self.assertIn("could not enumerate", proc.stdout)
        self.assertEqual(self._probed(tmp), [])
        self.assertNotEqual(proc.returncode, 0,
                            "a non-mapping project exited 0")
        self.assertIn("the host enumerator refuses a malformed catalog",
                      proc.stdout)

    def test_whitespace_hostname_fails_closed_without_splitting(self):
        """'bad host' must fail the enumeration, never become two probes."""
        tmp = self._fixture(WHITESPACE_CATALOG)
        proc = self._run_stage(tmp)
        self.assertIn("could not enumerate", proc.stdout)
        probed = self._probed(tmp)
        self.assertNotIn("https://bad", probed)
        self.assertNotIn("https://host", probed)
        self.assertEqual(probed, [])
        self.assertNotEqual(proc.returncode, 0,
                            "a whitespace-invalid hostname exited 0")
        self.assertIn("the host enumerator refuses a malformed catalog",
                      proc.stdout)

    def test_normalized_overlap_probes_once(self):
        """'Fixture.Org.' and 'fixture.org' are one normalised surface."""
        tmp = self._fixture(NORMALIZED_OVERLAP_CATALOG)
        proc = self._run_stage(tmp)
        self.assertEqual(self._probed(tmp), ["https://fixture.org"],
                         proc.stdout)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_generator_default_deduped_against_explicit(self):
        """portal's default and other's explicit portal.fixture.org probe once."""
        tmp = self._fixture(DEFAULT_PLUS_EXPLICIT_CATALOG)
        proc = self._run_stage(tmp)
        self.assertEqual(self._probed(tmp), ["https://portal.fixture.org"],
                         proc.stdout)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_a_failing_surface_fails_the_stage_but_runs_tail(self):
        """Failures propagate: a dead generated surface is red, and the stage
        still runs to its oracle tail rather than stopping at the failure."""
        tmp = self._fixture(DEFAULT_CATALOG)
        proc = self._run_stage(tmp, fail_host="portal.fixture.org")
        self.assertIn("portal.fixture.org returned 500", proc.stdout)
        self.assertNotEqual(proc.returncode, 0,
                            "a stage with a dead surface exited 0")
        self.assertIn("the host enumerator refuses a malformed catalog",
                      proc.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class Round4Test(unittest.TestCase):
    """Area-1 sweep round 4 (auditor + breaker): each row pins one repair."""

    def _stage(self, name):
        return (ROOT / "scripts" / "verify" / name).read_text()

    def _bash(self, script, cwd=None, env=None):
        return subprocess.run(["bash", "-c", script], cwd=cwd, capture_output=True, text=True, timeout=120,
                              env={**os.environ, **(env or {})})

    def test_loki_alert_names_are_intersected_literally_and_only_shaped_names_shown(self):
        """Breaker: `grep -q " $a "` read a ConfigMap rule name as a regex, so
        `Unrelated.*` matched any alert and certified the ruler path. Python set
        membership now; a rule name not of an alert-name shape is counted, not shown."""
        text = self._stage("stage5-platform.sh")
        block = text[text.index('      kubectl get configmap -n loki orbit-agents-logs-triggers -o jsonpath'):text.index("      # A healthy installation has NO logs alert firing")]
        self.assertNotIn("grep -q", block)
        def run(rules, alerts):
            with tempfile.TemporaryDirectory() as td:
                root = pathlib.Path(td)
                _write(root / "kubectl", "#!/bin/sh\nprintf '%s' \"$RULES\"\n", executable=True)
                script = ("TMP=$(mktemp -d); amraw(){ printf '%s' \"$ALERTS\"; }\n" + block + "\necho \"HIT=$HIT WANT=$WANT\"\n")
                return self._bash(script, env={"PATH": str(root) + os.pathsep + os.environ["PATH"],
                                               "RULES": yaml.safe_dump({"groups": [{"rules": [{"alert": r} for r in rules]}]}),
                                               "ALERTS": json.dumps([{"labels": {"alertname": a}} for a in alerts])}).stdout
        self.assertIn("HIT=0 WANT=(+1 rule name(s) not of an alert-name shape, or of a credential's shape)", run(["Unrelated.*"], ["UnrelatedAlert"]))
        self.assertIn("HIT=0 WANT=OrbitAgentOomSpike", run(["OrbitAgentOomSpike"], ["Other"]))
        self.assertIn("HIT=1 WANT=OrbitAgentOomSpike", run(["OrbitAgentOomSpike"], ["OrbitAgentOomSpike"]))
        out = run(["PLANTED CREDENTIAL 1234567890", "Real_1"], ["Real_1"])
        self.assertIn("HIT=1 WANT=Real_1 (+1 rule name(s) not of an alert-name shape, or of a credential's shape)", out)
        self.assertNotIn("PLANTED", out)

    def test_stage3_pod_and_job_lists_are_red_when_kubectl_fails(self):
        """Auditor: the no-PriorityClass, not-Ready and failed-Jobs rows read an empty
        answer from a failed kubectl as "no offender"."""
        text = self._stage("stage3.sh")
        block = text[text.index("# Each query's STATUS is read before its output"):text.index("bad \"the Jobs could not be listed (kubectl failed)")]
        block = text[text.index("# Each query's STATUS is read before its output"):text.index("\n", text.index("bad \"the Jobs could not be listed (kubectl failed)"))] + "\nfi\n"
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            _write(root / "kubectl", "#!/bin/sh\nexit 1\n", executable=True)
            out = self._bash("ok(){ echo \"  ok    $1\"; }; bad(){ echo \"  FAIL  $1\"; }\n" + block,
                             env={"PATH": str(root) + os.pathsep + os.environ["PATH"]}).stdout
            self.assertIn("FAIL  the managed pods could not be listed for their PriorityClass", out)
            self.assertIn("FAIL  the managed pods could not be listed for readiness", out)
            self.assertIn("FAIL  the Jobs could not be listed", out)
            _write(root / "kubectl", "#!/bin/sh\ncase \"$*\" in *priorityClassName*) printf 'a/p=orbit-x\\n';; *conditions*) printf 'a/p Running True\\n';; *) printf '';; esac\n", executable=True)
            out = self._bash("ok(){ echo \"  ok    $1\"; }; bad(){ echo \"  FAIL  $1\"; }\n" + block,
                             env={"PATH": str(root) + os.pathsep + os.environ["PATH"]}).stdout
            self.assertIn("ok    every managed pod has an orbit PriorityClass", out)
            self.assertIn("ok    every managed pod is Ready", out)
            self.assertIn("ok    no failed Jobs", out)

    def test_the_agent_pin_is_shown_only_when_it_is_an_image_reference(self):
        """Auditor: agentRuntime.image/tag were printed in rows unshaped, and the live
        image (the cluster's text) was repeated on a mismatch."""
        text = self._stage("stage3.sh")
        self.assertNotIn("runs '${LIVE:-none}'", text)
        start = text.index("PIN=$(python3 -c \"")
        pin = text[start:text.index("\" 2>/dev/null)\n", start) + len("\" 2>/dev/null)\n")]
        gate_at = text.index("[ \"$PIN\" = NOT-AN-IMAGE-REFERENCE ]")
        gate = text[text.rindex("\n", 0, gate_at) + 1:text.index("\n", gate_at)].strip()
        block = pin + gate + "\n"
        def run(image, tag):
            with tempfile.TemporaryDirectory() as td:
                root = pathlib.Path(td)
                _write(root / "instance" / "config.yaml", yaml.safe_dump({"agentRuntime": {"image": image, "tag": tag}}))
                return self._bash("ok(){ echo \"  ok    $1\"; }; bad(){ echo \"  FAIL  $1\"; }\n" + block + "\necho \"PIN=$PIN\"\n", cwd=root).stdout
        self.assertIn("PIN=ghcr.io/fixture-org/orbit-agents:v1.2.3", run("ghcr.io/fixture-org/orbit-agents", "v1.2.3"))
        for image, tag in (("PLANTED CREDENTIAL", "v1"), ("ghcr.io/x/y", "PLANTED CREDENTIAL"),
                           ("https://ghcr.io/x/y", "v1"), ("user:PLANTED@ghcr.io/x/y", "v1"), ("ghcr.io", "v1")):
            out = run(image, tag)
            self.assertIn("FAIL  agentRuntime.image/tag is not an image reference", out)
            self.assertIn("PIN=\n", out); self.assertNotIn("PLANTED", out)

    def test_the_pin_is_required_only_when_an_agent_is_enabled(self):
        """Round 5 (auditor): an instance with no enabled agent may omit agentRuntime; the
        not-an-image-reference row fired before the no-agent branch."""
        text = self._stage("stage3.sh")
        start = text.index("PIN=$(python3 -c \"")
        gate_at = text.index("[ \"$PIN\" = NOT-AN-IMAGE-REFERENCE ]")
        block = text[start:text.index("\n", gate_at)] + "\nfi\n"
        def run(agentnames):
            root = pathlib.Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, root, ignore_errors=True)
            _write(root / "instance" / "config.yaml", "instance: {name: fx}\n")
            return self._bash("ok(){ echo \"  ok    $1\"; }; bad(){ echo \"  FAIL  $1\"; }\nPREBOOT=0; AGENTNAMES='%s'\n" % agentnames + block, cwd=root).stdout
        out = run("")
        self.assertIn("ok    no agent declared in the catalog", out); self.assertNotIn("FAIL", out)
        self.assertIn("FAIL  agentRuntime.image/tag is not an image reference", run("oom:deployment"))

    def test_an_ingress_rule_with_no_from_is_an_offender(self):
        """Auditor: the intake netpol reader walked `from` peers only, so a rule with no
        `from` (which admits every peer) had no offender."""
        text = self._stage("stage5-platform.sh")
        marker = "  WIOUT=$(python3 - \"$TMP/intake-netpols.yaml\" <<'PY'\n"
        start = text.index(marker) + len(marker)
        code = text[start:text.index("\nPY\n", start)]
        src = ROOT / "template" / "provisioning" / "clusters" / "platform" / "config" / "orbit-intake" / "netpol.yaml"
        docs = [d for d in yaml.safe_load_all(src.read_text().replace("{{", "").replace("}}", "")) if isinstance(d, dict)]
        def run(docs):
            with tempfile.TemporaryDirectory() as td:
                f = pathlib.Path(td) / "netpol.yaml"; f.write_text(yaml.safe_dump_all(docs))
                return subprocess.run([sys.executable, "-c", code, str(f)], capture_output=True, text=True, timeout=60)
        self.assertEqual(run(docs).returncode, 0, run(docs).stdout)
        pol = next(d for d in docs if d.get("kind") == "NetworkPolicy" and (d.get("metadata") or {}).get("namespace") == "orbit-agents-prod")
        for rule in ({"ports": [{"port": 8080, "protocol": "TCP"}]}, {"ports": [{"port": 8080, "protocol": "TCP"}], "from": []}):
            with self.subTest(rule=rule):
                pol["spec"]["ingress"] = [rule]
                proc = run(docs)
                self.assertNotEqual(proc.returncode, 0, proc.stdout)
                self.assertIn("admits every peer (no from:)", proc.stdout)

    def test_a_bare_service_name_resolves_in_the_file_namespace_only(self):
        """Auditor: a bare name resolved to "the one Service of that name anywhere", which
        is not how the kubelet resolves it. The file's workload namespace is carried; a
        bare name with no single namespace in the file is RED."""
        text = self._stage("stage5-platform.sh")
        self.assertIn("(dests_for(p) or [None])", text)
        self.assertIn("is a bare Service name in a file with no single namespace", text)
        self.assertNotIn("hit = [v for k, v in svcs.items() if k[0] == name]", text)

    def test_a_malformed_port_is_red_not_a_host(self):
        """Breaker: `loki.loki.svc:not-a-port` was read as a host with an odd name."""
        text = self._stage("stage5-platform.sh")
        self.assertIn("names a malformed port (text after the last colon ", text)
        self.assertIn("that is not 1..65535; it is not repeated) — nothing can dial this address", text)
        self.assertIn("if colon and not (port.isdigit() and 1 <= int(port) <= 65535):", text)

    def test_stage4_discovery_and_key_names_are_shown_only_when_shaped(self):
        """Breaker: the api-resources output and a data: key were repeated verbatim."""
        text = self._stage("stage4.sh")
        self.assertIn('display.shown_list(names, "object-name", "resource name")', text)
        self.assertIn("(its name is not of a Secret key's shape)", text)
        # round 10 (B1): the discovery output lands in a private file, never a shell variable
        block = text[text.index("  SERVED=$(python3 -c '"):text.index("\nfi\n", text.index("the retired plane's API group is still served"))]
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            (root / "disc").write_text("externalsecrets.external-secrets.io\nPLANTED_DISCOVERY_CREDENTIAL_123456789\n")
            out = self._bash("ok(){ echo \"  ok    $1\"; }; bad(){ echo \"  FAIL  $1\"; }\nDISCRC=0; DISCF=" + str(root / "disc") + "\n" + block + "\nfi\n").stdout
            self.assertIn("still served:externalsecrets.external-secrets.io (+1 resource name(s) not of an object-name shape, or of a credential's shape)", out)
            self.assertNotIn("PLANTED", out)


class Round5Test(unittest.TestCase):
    """Area-1 sweep round 5 (auditor): each row pins one repair."""

    def _stage(self, name):
        return (ROOT / "scripts" / "verify" / name).read_text()

    def _bash(self, script, cwd=None, env=None):
        return subprocess.run(["bash", "-c", script], cwd=cwd, capture_output=True, text=True, timeout=120,
                              env={**os.environ, **(env or {})})

    def _with_kubectl(self, body):
        root = pathlib.Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        _write(root / "kubectl", "#!/bin/sh\n" + body + "\n", executable=True)
        return root, {"PATH": str(root) + os.pathsep + os.environ["PATH"]}

    def test_the_grafana_limit_is_read_as_data(self):
        """A1: the limit was spliced into a python program; a hostile value ran."""
        text = self._stage("stage5-platform.sh")
        self.assertNotIn("s='${GLIM}'", text)
        block = text[text.index("  kubectl -n $MON get deploy kube-prometheus-stack-grafana"):text.index("  GPEAKB=$(promval")]
        root, env = self._with_kubectl("printf '%s' \"$GLIM\"")
        for glim, want in (("0';__import__('os').system('echo PLANTED_RAN');#", "GLIMB=0"), ("512Mi", "GLIMB=536870912"), ("1G", "GLIMB=1000000000"), ("", "GLIMB=0")):
            with self.subTest(glim=glim[:12]):
                out = self._bash("TMP=$(mktemp -d); MON=monitoring\n" + block + "\necho \"GLIMB=$GLIMB\"\n", env={**env, "GLIM": glim})
                self.assertIn(want, out.stdout + out.stderr); self.assertNotIn("PLANTED_RAN", out.stdout + out.stderr)

    def test_the_renderer_secret_reference_is_shown_only_as_a_secret_name(self):
        """A3: imageRenderer.existingSecret was echoed unchecked."""
        text = self._stage("stage5-platform.sh")
        block = text[text.index("PIN=$(python3 -c '\nimport sys, yaml"):text.index("\nesac\n", text.index("NOT-A-SECRET-NAME) bad")) + len("\nesac\n")]
        def run(ref):
            root = pathlib.Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, root, ignore_errors=True)
            _write(root / "clusters/platform/apps/kube-prometheus-stack.yaml",
                   yaml.safe_dump({"spec": {"source": {"helm": {"valuesObject": {"grafana": {"imageRenderer": {"existingSecret": ref}}}}}}}))
            return self._bash("ok(){ echo \"  ok    $1\"; }; bad(){ echo \"  FAIL  $1\"; }\nKPS=clusters/platform/apps/kube-prometheus-stack.yaml\n" + block, cwd=root).stdout
        self.assertIn("ok    imageRenderer.existingSecret is pinned in the values (grafana-renderer-token)", run("grafana-renderer-token"))
        self.assertIn("FAIL  imageRenderer.existingSecret is unset", run(""))
        out = run("Bearer PLANTED_RENDERER_CREDENTIAL")
        self.assertIn("FAIL  imageRenderer.existingSecret in clusters/platform/apps/kube-prometheus-stack.yaml is not a Secret name", out)
        self.assertNotIn("PLANTED", out)

    def test_a_redacted_url_is_unverified_by_design_not_bound_to_a_retired_argo(self):
        """A9 (round 5) then A1 (round 6): the Argo sync row was a regression — Argo CD is
        retired and the desk applies the platform layer, so a healthy instance has no
        Application to read. Equality of a redacted running URL with git needs the
        Secret's data, which no stage reads: an [UNVERIFIED] row says so."""
        text = self._stage("stage5-platform.sh")
        self.assertNotIn("get application kube-prometheus-stack", text)
        self.assertNotIn("-n argocd", text)
        self.assertIn('ok "[UNVERIFIED] a secret-typed URL the running config redacts is clean in git at the same place', text)

    def test_an_empty_ports_list_admits_every_port(self):
        """A10: `ports: []` was read as "no port matches 9093" and skipped the rule."""
        text = self._stage("stage5-platform.sh")
        start = text.index("  WOUT=$(python3 - \"$NETPOL\" <<'PY'\n") + len("  WOUT=$(python3 - \"$NETPOL\" <<'PY'\n")
        code = text[start:text.index("\nPY\n", start)]
        src = ROOT / "template" / "provisioning" / "clusters" / "platform" / "config" / "monitoring" / "netpol.yaml"
        docs = [d for d in yaml.safe_load_all(src.read_text()) if isinstance(d, dict)]
        am = next(d for d in docs if d.get("kind") == "NetworkPolicy" and "alertmanager" in yaml.safe_dump(d.get("spec", {}).get("podSelector", {})))
        for ports in ([], [{"port": "web"}], [{"protocol": "TCP"}]):
            with self.subTest(ports=ports):
                am["spec"]["ingress"] = [{"ports": ports, "from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "loki"}}}]}]
                with tempfile.TemporaryDirectory() as td:
                    f = pathlib.Path(td) / "netpol.yaml"; f.write_text(yaml.safe_dump_all(docs))
                    proc = subprocess.run([sys.executable, "-c", code, str(f)], capture_output=True, text=True, timeout=60)
                self.assertNotEqual(proc.returncode, 0, proc.stdout); self.assertIn("bare namespace-wide loki grant", proc.stdout)

    def test_declared_rule_names_are_compared_literally(self):
        """A11: `grep -q "^$a "` read a declared name as a pattern (a dot matched anything)."""
        text = self._stage("stage5-platform.sh")
        self.assertNotIn('grep -q "^$a " "$TMP/loaded.txt"', text)
        block = text[text.index("      MISS=$(python3 - \"$TMP/gitrules.txt\" \"$TMP/loaded.txt\""):text.index("      case \"$MISS\" in ''|*[!0-9]*) MISS=1;; esac\n") + len("      case \"$MISS\" in ''|*[!0-9]*) MISS=1;; esac\n")]
        def run(git, loaded):
            with tempfile.TemporaryDirectory() as td:
                t = pathlib.Path(td); (t / "gitrules.txt").write_text("\n".join(git) + "\n"); (t / "loaded.txt").write_text("\n".join(loaded) + "\n")
                return self._bash(block + "\necho \"MISS=$MISS\"\n", env={"TMP": td})
        self.assertIn("MISS=1", run(["Missing.Rule"], ["MissingXRule ok"]).stdout)
        self.assertIn("MISS=0", run(["Missing.Rule"], ["Missing.Rule ok"]).stdout)
        out = run(["PLANTED RULE NAME"], ["Other ok"])
        self.assertIn("MISS=1", out.stdout); self.assertNotIn("PLANTED", out.stdout + out.stderr)
        self.assertIn("not loaded: (a rule name not of an alert-name shape, or of a credential's shape)", out.stderr)

    def test_a_failed_loki_helper_is_never_a_held_line(self):
        """A12: a query answering {"status":"success","data":null} made the parser raise,
        left the count empty, and the wildcard case read empty as ALIVE."""
        text = self._stage("stage5-platform.sh")
        helper = text[text.index("  LRANGE_GRAFANA(){"):text.index("  KLAST(){")]
        py = helper[helper.rindex("python3 -c '") + len("python3 -c '"):helper.rindex("' \"$4\"; }")]
        with tempfile.TemporaryDirectory() as td:
            needle = pathlib.Path(td) / "needle"; needle.write_text('1700000000\t"GET /x"\n')
            for body, want in ('{"status":"success","data":null}', "QUERYERR"), ('{"status":"success","data":{"result":[{"values":[["1","a GET /x b"]]}]}}', "1"), ('{"status":"success","data":{"result":[1]}}', "QUERYERR"), ('{"status":"success","data":{}}', "QUERYERR"), ("nope", "QUERYERR"):
                with self.subTest(body=body):
                    proc = subprocess.run([sys.executable, "-c", py, str(needle)], input=body, capture_output=True, text=True, timeout=30)
                    self.assertEqual(proc.stdout.strip(), want)
        ctl = text[text.index("      case \"$CTL_H\" in"):text.index("\n      esac\n", text.index("      case \"$CTL_H\" in")) + len("\n      esac\n")]
        for h, state in (("", "UNANSWERABLE"), ("QUERYERR", "UNANSWERABLE"), ("x1", "UNANSWERABLE"), ("0", "BROKEN"), ("3", "ALIVE")):
            with self.subTest(h=h):
                out = self._bash(f"CTL_STATE=UNANSWERABLE; CTL_TS=0; CTL_H='{h}'\n" + ctl + "\necho \"STATE=$CTL_STATE\"\n").stdout
                self.assertIn(f"STATE={state}", out)

    def test_secret_reference_inventories_are_red_when_kubectl_fails(self):
        """A13: a failed listing yielded no references, and no references read as
        "every referenced Secret exists"."""
        text = self._stage("stage3.sh")
        block = text[text.index("# Each inventory's STATUS is read before it is walked"):text.index("\nfi\n", text.index("the Middlewares could not be listed")) + len("\nfi\n")]
        root, env = self._with_kubectl("exit 1")
        out = self._bash("ok(){ echo \"  ok    $1\"; }; bad(){ echo \"  FAIL  $1\"; }\n" + block, env=env).stdout
        self.assertIn("FAIL  the pods could not be listed for their imagePullSecrets", out)
        self.assertIn("FAIL  the Middlewares could not be listed", out)
        root, env = self._with_kubectl("case \"$*\" in *imagePullSecrets*) printf 'a/pull\\n';; *basicAuth*) printf 'b/auth\\nPLANTED junk\\n';; *'get secret'*) exit 0;; *) exit 0;; esac")
        out = self._bash("ok(){ echo \"  ok    $1\"; }; bad(){ echo \"  FAIL  $1\"; }\n" + block, env=env).stdout
        self.assertIn("ok    every referenced imagePullSecret exists", out)
        self.assertIn("ok    every basic-auth Middleware has its Secret", out)
        self.assertNotIn("PLANTED", out)


class Round5BreakerTest(unittest.TestCase):
    """Area-1 sweep round 5 (breaker): each row pins one repair."""

    def _stage(self, name):
        return (ROOT / "scripts" / "verify" / name).read_text()

    def _bash(self, script, cwd=None, env=None):
        return subprocess.run(["bash", "-c", script], cwd=cwd, capture_output=True, text=True, timeout=120,
                              env={**os.environ, **(env or {})})

    def _with_kubectl(self, body):
        root = pathlib.Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        _write(root / "kubectl", "#!/bin/sh\n" + body + "\n", executable=True)
        return root, {"PATH": str(root) + os.pathsep + os.environ["PATH"]}

    def test_a_reference_of_no_object_name_shape_never_reaches_kubectl(self):
        """B1: `tenant-a/--output=json` from a Middleware became a kubectl argument."""
        text = self._stage("stage3.sh")
        block = text[text.index("# Each inventory's STATUS is read before it is walked"):text.index("\nfi\n", text.index("the Middlewares could not be listed")) + len("\nfi\n")]
        root, env = self._with_kubectl("echo \"$*\" >> \"$ARGV\"; case \"$*\" in *basicAuth*) printf 'tenant-a/--output=json\\ntenant-a/-x\\n';; *imagePullSecrets*) printf 'a/--all\\n';; *) exit 0;; esac")
        argv = root / "argv"; argv.write_text("")
        out = self._bash("ok(){ echo \"  ok    $1\"; }; bad(){ echo \"  FAIL  $1\"; }\n" + block, env={**env, "ARGV": str(argv)}).stdout
        self.assertNotIn("--output", argv.read_text()); self.assertNotIn("-x", argv.read_text().replace("-A", "")); self.assertNotIn("--all", argv.read_text())
        self.assertIn("ok    every basic-auth Middleware has its Secret", out)

    def test_the_replicafailure_row_is_red_when_kubectl_fails(self):
        """B2: a failed ReplicaSet listing read as "no ReplicaFailure"."""
        text = self._stage("stage3.sh")
        helpers = text[text.index("shaped_refs(){"):text.index("BADRES=$(kubectl get pods -A")]
        block = helpers + text[text.index("RFF=$(mktemp)\n"):text.index("rm -f \"$RFF\"\n")]
        root, env = self._with_kubectl("exit 1")
        self.assertIn("FAIL  the ReplicaSets could not be listed", self._bash("ok(){ echo \"  ok    $1\"; }; bad(){ echo \"  FAIL  $1\"; }\n" + block, env=env).stdout)
        root, env = self._with_kubectl("printf ' '")
        self.assertIn("ok    no ReplicaFailure", self._bash("ok(){ echo \"  ok    $1\"; }; bad(){ echo \"  FAIL  $1\"; }\n" + block, env=env).stdout)

    def test_promval_prints_only_a_number(self):
        """B4: a sample value from Prometheus was printed in rows unchecked."""
        text = self._stage("stage5-platform.sh")
        block = text[text.index("promval(){ promq"):text.index("'; }\n", text.index("promval(){ promq")) + len("'; }\n")]
        def run(body):
            return self._bash("promq(){ printf '%s' \"$BODY\"; }\n" + block + "\npromval x\n", env={"BODY": body}).stdout.strip()
        self.assertEqual(run('{"status":"success","data":{"result":[{"value":[1,"PLANTED credential=query-value-123456789"]}]}}'), "QUERYERR")
        self.assertEqual(run('{"status":"success","data":{"result":[{"value":[1,"3"]}]}}'), "3")
        self.assertEqual(run('{"status":"success","data":{"result":[{"value":[1,"1.5e+03"]}]}}'), "1.5e+03")
        self.assertEqual(run('{"status":"success","data":{"result":[]}}'), "ABSENT")
        self.assertEqual(run('{"status":"success","data":null}'), "QUERYERR")

    def test_a_folded_url_in_a_quoted_scalar_is_one_url(self):
        """B9: the text scan read a YAML-folded quoted URL as `host:3100\\n\\` — a
        malformed port on a perfectly good address. Decoded values are scanned now."""
        text = self._stage("stage5-platform.sh")
        start = text.index("SVCPORT=$(python3 - . <<'PY'\n") + len("SVCPORT=$(python3 - . <<'PY'\n")
        code = text[start:text.index("\nPY\n", start)]
        root = pathlib.Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        (root / "clusters").mkdir(); (root / "catalog").mkdir()
        _write(root / "clusters" / "svc.yaml", yaml.safe_dump({"apiVersion": "v1", "kind": "Service", "metadata": {"name": "loki", "namespace": "loki"}, "spec": {"ports": [{"port": 3100}]}}))
        # a double-quoted scalar escaped across lines: the value is ONE url, the text is two halves
        dumped = ('apiVersion: argoproj.io/v1alpha1\nkind: Application\nmetadata: {name: a, namespace: argocd}\n'
                  'spec:\n  destination: {namespace: monitoring}\n  source:\n    helm:\n      valuesObject:\n'
                  '        clients:\n        - url: "http://loki.loki.svc:3100\\\n            /loki/api/v1/push"\n')
        self.assertEqual(yaml.safe_load(dumped)["spec"]["source"]["helm"]["valuesObject"]["clients"][0]["url"], "http://loki.loki.svc:3100/loki/api/v1/push")
        _write(root / "clusters" / "app.yaml", dumped)
        proc = subprocess.run([sys.executable, "-c", code, str(root)], cwd=root, capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("OK 1 in-cluster endpoint(s)", proc.stdout)
        _write(root / "clusters" / "bad.yaml", yaml.safe_dump({"kind": "ConfigMap", "metadata": {"name": "c", "namespace": "monitoring"}, "data": {"u": "http://loki.loki.svc:not-a-port/x"}}))
        proc = subprocess.run([sys.executable, "-c", code, str(root)], cwd=root, capture_output=True, text=True, timeout=120)
        self.assertNotEqual(proc.returncode, 0); self.assertIn("names a malformed port", proc.stdout)


class Round6Test(unittest.TestCase):
    """Area-1 sweep round 6 (auditor + breaker): each row pins one repair."""

    def _stage(self, name):
        return (ROOT / "scripts" / "verify" / name).read_text()

    def _bash(self, script, cwd=None, env=None):
        return subprocess.run(["bash", "-c", script], cwd=cwd, capture_output=True, text=True, timeout=120,
                              env={**os.environ, **(env or {})})

    def _with_kubectl(self, body):
        root = pathlib.Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        _write(root / "kubectl", "#!/bin/sh\n" + body + "\n", executable=True)
        return root, {"PATH": str(root) + os.pathsep + os.environ["PATH"]}

    def test_a_kubeconfig_list_is_read_the_way_kubectl_reads_it(self):
        """B1: KUBECONFIG=a:b with both files present was pre-boot (`-s` on the whole
        string), so every live row keyed off on a bootstrapped instance."""
        for stage in ("stage2.sh", "stage3.sh", "stage4.sh", "stage5-platform.sh"):
            with self.subTest(stage=stage):
                text = self._stage(stage)
                self.assertNotIn('[ -s "$KUBECONFIG" ]', text)
                root = pathlib.Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, root, ignore_errors=True)
                (root / "a").write_text("apiVersion: v1\n"); (root / "b").write_text("apiVersion: v1\n"); (root / "empty").write_text("")
                def run(kc):
                    fn = text[text.index("kubeconfig_present(){"):text.index("return $any; }\n", text.index("kubeconfig_present(){")) + len("return $any; }\n")]
                    script = "PREBOOT=0\n" + fn + "kubeconfig_present \"$KUBECONFIG\" || PREBOOT=1\necho \"PREBOOT=$PREBOOT\"\n"
                    return self._bash(script, env={"KUBECONFIG": kc})
                self.assertIn("PREBOOT=0", run(f"{root}/a:{root}/b").stdout)
                self.assertIn("PREBOOT=0", run(f"{root}/missing:{root}/b").stdout)
                self.assertIn("PREBOOT=1", run(f"{root}/missing:{root}/also-missing").stdout)
                self.assertIn("PREBOOT=1", run(f"{root}/missing").stdout)
                proc = run(f"{root}/empty:{root}/a")
                self.assertEqual(proc.returncode, 2); self.assertIn("exists but is empty or unreadable", proc.stderr)

    def test_malformed_endpoint_diagnostics_repeat_no_authority(self):
        """A2/B2: the malformed-port finding printed the authority, and a Service with an
        odd name was named verbatim in the not-a-number finding."""
        text = self._stage("stage5-platform.sh")
        start = text.index("SVCPORT=$(python3 - . <<'PY'\n") + len("SVCPORT=$(python3 - . <<'PY'\n")
        code = text[start:text.index("\nPY\n", start)]
        root = pathlib.Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        (root / "clusters").mkdir(); (root / "catalog").mkdir(); _dirapp(root)
        _write(root / "clusters" / "bad.yaml", yaml.safe_dump({"kind": "ConfigMap", "metadata": {"name": "c", "namespace": "monitoring"}, "data": {"u": "https://monitoring.svc:PLANTED_CREDENTIAL_1234567890"}}))
        proc = subprocess.run([sys.executable, "-c", code, str(root)], cwd=root, capture_output=True, text=True, timeout=120)
        self.assertNotEqual(proc.returncode, 0); self.assertIn("https://monitoring.svc:<not a port> names a malformed port", proc.stdout); self.assertNotIn("PLANTED", proc.stdout + proc.stderr)
        (root / "clusters" / "bad.yaml").unlink()
        _write(root / "clusters" / "svc.yaml", yaml.safe_dump({"apiVersion": "v1", "kind": "Service", "metadata": {"name": "PLANTED_PASSWORD_1234567890", "namespace": "loki"}, "spec": {"ports": [{"port": "http"}]}}))
        proc = subprocess.run([sys.executable, "-c", code, str(root)], cwd=root, capture_output=True, text=True, timeout=120)
        self.assertNotEqual(proc.returncode, 0); self.assertIn("Service loki/(a name not of an object-name shape, or of a credential's shape) declares a port that is not an integer in 1..65535", proc.stdout); self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_catalog_pair_names_reach_kubectl_only_when_label_shaped(self):
        """A4: a project id from the catalog became a kubectl argument unchecked."""
        text = self._stage("stage3.sh")
        block = text[text.index('  INVMISS=""; INVSEEN=0; INVODD=0'):text.index("\n", text.index('bad "$INVODD declared pair(s)'))]
        root, env = self._with_kubectl("echo \"$*\" >> \"$ARGV\"; exit 0")
        argv = root / "argv"; argv.write_text("")
        _write(root / "catalog" / "projects.yaml", yaml.safe_dump({"projects": [
            {"id": "token=PLANTED_CREDENTIAL", "components": ["a"], "environments": {"prod": {}}},
            {"id": "good", "components": ["a"], "environments": {"prod": {}, "Dev X": {"enabled": True}}}]}))
        out = self._bash("ok(){ echo \"  ok    $1\"; }; bad(){ echo \"  FAIL  $1\"; }\n" + block + "\necho \"SEEN=$INVSEEN ODD=$INVODD MISS=$INVMISS\"\n", cwd=root, env={**env, "ARGV": str(argv)}).stdout
        self.assertNotIn("PLANTED", out + argv.read_text()); self.assertNotIn("Dev X", argv.read_text())
        self.assertIn("orbit-inventory-good-prod", argv.read_text())
        self.assertIn("FAIL  2 declared pair(s) have an id or environment that is not a label", out)
        self.assertIn("SEEN=3 ODD=2", out)

    def test_a_namespace_selector_not_proven_to_exclude_loki_is_a_loki_peer(self):
        """A5: a NotIn expression (or any form this reader does not model) skipped the peer."""
        text = self._stage("stage5-platform.sh")
        start = text.index("  WOUT=$(python3 - \"$NETPOL\" <<'PY'\n") + len("  WOUT=$(python3 - \"$NETPOL\" <<'PY'\n")
        code = text[start:text.index("\nPY\n", start)]
        src = ROOT / "template" / "provisioning" / "clusters" / "platform" / "config" / "monitoring" / "netpol.yaml"
        docs = [d for d in yaml.safe_load_all(src.read_text()) if isinstance(d, dict)]
        am = next(d for d in docs if d.get("kind") == "NetworkPolicy" and "alertmanager" in yaml.safe_dump(d.get("spec", {}).get("podSelector", {})))
        def run(ns_sel):
            am["spec"]["ingress"] = [{"ports": [{"port": 9093}], "from": [{"namespaceSelector": ns_sel}]}]
            with tempfile.TemporaryDirectory() as td:
                f = pathlib.Path(td) / "netpol.yaml"; f.write_text(yaml.safe_dump_all(docs))
                return subprocess.run([sys.executable, "-c", code, str(f)], capture_output=True, text=True, timeout=60)
        for sel in ({"matchExpressions": [{"key": "kubernetes.io/metadata.name", "operator": "NotIn", "values": ["default"]}]},
                    {"matchExpressions": [{"key": "team", "operator": "Exists"}]},
                    {"matchLabels": {"team": "sre"}},
                    {"matchLabels": {"kubernetes.io/metadata.name": "other"}, "matchExpressions": [{"key": "x", "operator": "Exists"}]}):
            with self.subTest(sel=sel):
                proc = run(sel); self.assertNotEqual(proc.returncode, 0, proc.stdout); self.assertIn("bare namespace-wide loki grant", proc.stdout)
        for sel in ({"matchLabels": {"kubernetes.io/metadata.name": "other"}},
                    {"matchExpressions": [{"key": "kubernetes.io/metadata.name", "operator": "In", "values": ["other"]}]}):
            with self.subTest(sel=sel):
                proc = run(sel); self.assertEqual(proc.returncode, 0, proc.stdout)

    def test_the_door_limits_must_be_bounded_and_active(self):
        """A6: rateLimit.average 0 (disabled) and an inFlightReq amount in the millions
        passed as "the OOM guard is attached"."""
        text = self._stage("stage5-platform.sh")
        start = text.index("  MOUT=$(python3 - \"$IDIR/ingress.yaml\" <<'PY'\n") + len("  MOUT=$(python3 - \"$IDIR/ingress.yaml\" <<'PY'\n")
        code = text[start:text.index("\nPY\n", start)]
        src = ROOT / "template" / "provisioning" / "clusters" / "platform" / "config" / "orbit-intake" / "ingress.yaml"
        base = src.read_text().replace("{{", "").replace("}}", "")
        def run(t):
            with tempfile.TemporaryDirectory() as td:
                f = pathlib.Path(td) / "ingress.yaml"; f.write_text(t)
                return subprocess.run([sys.executable, "-c", code, str(f)], capture_output=True, text=True, timeout=60)
        self.assertEqual(run(base).returncode, 0, run(base).stdout)
        for a, b, needle in (("    average: 50\n", "    average: 0\n", "rateLimit.average is not in 1..10000"),
                             ("    amount: 16\n", "    amount: 1000000\n", "inFlightReq.amount is not in 1..1024"),
                             ("    burst: 100\n", "    burst: 0\n", "rateLimit.burst is not in 1..10000")):
            with self.subTest(needle=needle):
                self.assertEqual(base.count(a), 1)
                proc = run(base.replace(a, b)); self.assertNotEqual(proc.returncode, 0); self.assertIn(needle, proc.stdout)


class Round7Test(unittest.TestCase):
    """Area-1 sweep round 7 (breaker): each row pins one repair."""

    def _stage(self, name):
        return (ROOT / "scripts" / "verify" / name).read_text()

    def test_no_python_program_carries_a_shell_path_in_its_text(self):
        """B5: a scratch path (TMPDIR with an apostrophe) was spliced into python -c
        programs, which then failed to parse. Paths are argv now, in every stage."""
        for stage in ("stage2.sh", "stage3.sh", "stage4.sh", "stage5-platform.sh"):
            with self.subTest(stage=stage):
                text = self._stage(stage)
                self.assertNotRegex(text, r"open\('\$[A-Za-z_]")
                self.assertNotRegex(text, r"open\(\"\$[A-Za-z_]")
        # the stage3 values extractor, run with an apostrophe in the scratch path
        text = self._stage("stage3.sh")
        start = text.index('   && python3 -c "\nimport sys, yaml\napp = [d for d in yaml.safe_load_all(open(sys.argv[1])) if d][0]')
        snippet = text[start + len("   && "):text.index(" 2>/dev/null", start)]
        root = pathlib.Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        a = root / "quote'root"; (a / "clusters" / "apps").mkdir(parents=True)
        _write(a / "clusters" / "apps" / "orbit-agents-prod.yaml", yaml.safe_dump({"kind": "Application", "spec": {"source": {"helm": {"valuesObject": {"x": 1}}}}}))
        proc = subprocess.run(["bash", "-c", "A=$1\n" + snippet, "_", str(a)], capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(yaml.safe_load((a / "values.yaml").read_text()), {"x": 1})

    def test_a_probe_name_is_shown_only_when_it_is_an_object_name(self):
        """B3: a Probe named `Bearer PLANTED...` with no prober url was named verbatim."""
        text = self._stage("stage5-platform.sh")
        start = text.index("SVCPORT=$(python3 - . <<'PY'\n") + len("SVCPORT=$(python3 - . <<'PY'\n")
        code = text[start:text.index("\nPY\n", start)]
        root = pathlib.Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        (root / "clusters").mkdir(); (root / "catalog").mkdir()
        _write(root / "clusters" / "probe.yaml", yaml.safe_dump({"apiVersion": "monitoring.coreos.com/v1", "kind": "Probe",
                                                                  "metadata": {"name": "Bearer PLANTED_PROBE_CREDENTIAL_123456789", "namespace": "monitoring"}, "spec": {}}))
        proc = subprocess.run([sys.executable, "-c", code, str(root)], cwd=root, capture_output=True, text=True, timeout=120)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("Probe (document 0, a name not of an object-name shape, or of a credential's shape) declares no spec.prober.url", proc.stdout)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def _splice(self, stage, start, end):
        text = self._stage(stage)
        i = text.index(start) + len(start)
        return text[i:text.index(end, i)]

    def test_catalog_ids_and_agent_names_reach_kubectl_only_as_labels(self):
        """A3: a project id and an agent name went from the catalog into kubectl argv
        (a namespace, a selector, an object name) with no shape check."""
        wal = self._splice("stage3.sh", 'WAL_DBS=$(python3 -c "\n', '" 2>/dev/null)\n')
        agents = self._splice("stage3.sh", 'AGENTNAMES=$(python3 -c "\n', '" 2>/dev/null)\n')
        root = pathlib.Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        (root / "instance").mkdir(); (root / "catalog").mkdir()
        _write(root / "instance" / "config.yaml", yaml.safe_dump({"objectStorage": {"buckets": {"platform": "b"}}}))
        proj = lambda i: {"id": i, "database": True, "components": ["api"], "environments": {"prod": {}}}
        _write(root / "catalog" / "projects.yaml", yaml.safe_dump({
            "projects": [proj("shop"), proj("PLANTED id; x"), proj("../PLANTED")],
            "agents": [{"name": "watcher", "mode": "cronjob"}, {"name": "PLANTED name"}, {"name": "w2", "mode": "PLANTED mode"}]}))
        for code, want in ((wal, "shop UNSHAPED UNSHAPED"), (agents, "watcher:cronjob\nUNSHAPED:unshaped\nUNSHAPED:unshaped")):
            with self.subTest(code=code[:30]):
                proc = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True, timeout=60)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stdout.strip(), want)
                self.assertNotIn("PLANTED", proc.stdout + proc.stderr)
        text = self._stage("stage3.sh")
        self.assertIn('if [ "$p" = UNSHAPED ]; then', text)
        self.assertIn('if [ "$PAIR" = UNSHAPED:unshaped ]; then', text)

    def test_promtool_parses_the_rules_this_tree_renders_not_only_the_fixture(self):
        """A6: promtool ran over fixture-generated rules only; the instance's own
        PrometheusRules under clusters/ were never parsed."""
        code = self._splice("stage3.sh", "REALRULES=$(python3 - clusters \"$A/real-rules\" <<'PY'\n", "\nPY\n")
        root = pathlib.Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        (root / "clusters" / "platform" / "monitoring").mkdir(parents=True); (root / "out").mkdir()
        groups = {"groups": [{"name": "g", "rules": [{"alert": "A", "expr": "up == 0"}]}]}
        _write(root / "clusters" / "platform" / "monitoring" / "rules.yaml",
               yaml.safe_dump_all([{"kind": "ConfigMap", "data": {}}, {"kind": "PrometheusRule", "metadata": {"name": "r"}, "spec": groups}]))
        _write(root / "clusters" / "apps" / "PLANTED file.yaml", yaml.safe_dump({"kind": "PrometheusRule", "spec": groups}))
        _write(root / "clusters" / "apps" / "broken.yaml", "a: [\n")
        proc = subprocess.run([sys.executable, "-c", code, "clusters", str(root / "out")], cwd=root, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.splitlines(), ["2", "clusters/apps/(a file not of a file-name shape, or of a credential's shape) clusters/platform/monitoring/rules.yaml"])
        self.assertEqual(sorted(p.name for p in (root / "out").iterdir()), ["1.yaml", "2.yaml"])
        self.assertEqual(yaml.safe_load((root / "out" / "2.yaml").read_text()), groups)
        text = self._stage("stage3.sh")
        self.assertIn('for f in "$A"/real-rules/*.yaml; do\n        if ! "$PT" check rules "$f"', text)
        self.assertIn("promtool parses every PrometheusRule this tree renders", text)

    def test_the_intake_door_diagnostics_repeat_a_value_only_with_its_shape(self):
        """A4: the door row printed the namespace, class, hosts and backend Service names
        with %r, straight from the file."""
        code = self._splice("stage5-platform.sh", "DOUT=$(python3 - \"$IDIR/ingress.yaml\" \"$IHOST\" <<'PY'\n", "\nPY\n")
        root = pathlib.Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        ing = {"kind": "Ingress", "metadata": {"name": "orbit-intake-github", "namespace": "PLANTED ns"},
               "spec": {"ingressClassName": "PLANTED class", "defaultBackend": {"service": {"name": "PLANTED default"}},
                        "rules": [{"host": "PLANTED host", "http": {"paths": [{"path": "/webhook/github", "pathType": "Prefix",
                                                                                 "backend": {"service": {"name": "PLANTED svc"}}}]}}]}}
        _write(root / "ingress.yaml", yaml.safe_dump(ing))
        proc = subprocess.run([sys.executable, "-c", code, str(root / "ingress.yaml"), "hooks.fx.org"], capture_output=True, text=True, timeout=60)
        self.assertNotEqual(proc.returncode, 0)
        for needle in ("namespace is (a value not of an object-name shape, or of a credential's shape), not orbit-agents-prod",
                       "ingressClassName is (a value not of an object-name shape, or of a credential's shape), not traefik",
                       "spec.defaultBackend -> Service (a value not of an object-name shape, or of a credential's shape)",
                       "hosts are [(a value not of a hostname shape, or of a credential's shape)], expected exactly [hooks.fx.org]",
                       "a path rule backs Service (a value not of an object-name shape, or of a credential's shape), not orbit-agents-intake"):
            self.assertIn(needle, proc.stdout)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)
        ing["metadata"]["namespace"] = "other-ns"; ing["spec"]["rules"][0]["host"] = "other.fx.org"
        _write(root / "ingress.yaml", yaml.safe_dump(ing))
        proc = subprocess.run([sys.executable, "-c", code, str(root / "ingress.yaml"), "hooks.fx.org"], capture_output=True, text=True, timeout=60)
        self.assertIn("namespace is other-ns, not orbit-agents-prod", proc.stdout)
        self.assertIn("hosts are [other.fx.org], expected exactly [hooks.fx.org]", proc.stdout)

    def test_the_intake_grant_is_exactly_8080_tcp(self):
        """A5: an empty ports list (every port), a UDP entry and an endPort range all
        passed as '8080'; the offending ports and selectors were printed verbatim."""
        code = self._splice("stage5-platform.sh", "WIOUT=$(python3 - \"$TMP/intake-netpols.yaml\" <<'PY'\n", "\nPY\n")
        root = pathlib.Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        peer = {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "traefik"}}, "podSelector": {"matchLabels": {"app.kubernetes.io/name": "traefik"}}}
        def netpol(ports, peers=None):
            return yaml.safe_dump({"kind": "NetworkPolicy", "metadata": {"name": "intake-from-traefik", "namespace": "orbit-agents-prod"},
                                   "spec": {"podSelector": {"matchLabels": {"app.kubernetes.io/instance": "orbit-agents", "app.kubernetes.io/name": "intake"}},
                                            "ingress": [{"ports": ports, "from": peers if peers is not None else [peer]}]}})
        def run(text):
            _write(root / "netpol.yaml", text)
            return subprocess.run([sys.executable, "-c", code, str(root / "netpol.yaml")], capture_output=True, text=True, timeout=60)
        proc = run(netpol([{"port": 8080, "protocol": "TCP"}]))
        self.assertEqual(proc.returncode, 0, proc.stdout)
        proc = run(netpol([{"port": 8080}]))
        self.assertEqual(proc.returncode, 0, proc.stdout)
        for ports in ([], [{"port": 8080, "protocol": "UDP"}], [{"port": 8080, "endPort": 9090}], [{"port": 8080}, {"port": "PLANTED-port"}], "PLANTED"):
            with self.subTest(ports=str(ports)[:20]):
                proc = run(netpol(ports))
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("is not scoped to exactly 8080/TCP", proc.stdout)
                self.assertNotIn("PLANTED", proc.stdout + proc.stderr)
        proc = run(netpol([{"port": 8080}], [{"namespaceSelector": {"matchLabels": {"PLANTED": "x"}}}]))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("is not narrowed on BOTH sides in ONE element", proc.stdout)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)
        self.assertIn("this proves the edge only", self._stage("stage5-platform.sh"))


class Round8Test(unittest.TestCase):
    """Area-1 sweep round 8: each row pins one repair (auditor A*, breaker B*)."""

    def _stage(self, name):
        return (ROOT / "scripts" / "verify" / name).read_text()

    def _splice(self, stage, start, end):
        text = self._stage(stage)
        i = text.index(start) + len(start)
        return text[i:text.index(end, i)]

    def _root(self):
        root = pathlib.Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        return root

    def test_the_authenticated_dev_probe_is_pinned_to_this_instances_ingress(self):
        """A1: the request carrying the shared dev password went to whatever DNS said a
        catalog hostname was; it is pinned to the declared control-plane node now."""
        text = self._stage("stage3.sh")
        line = next(l for l in text.splitlines() if "C_OK=$(curluser" in l)
        self.assertIn('--resolve "$host:443:$ORBIT_CONTROL_PLANE_IP" "$url"', line)
        # the token is <host><door path>; the pin binds the host part alone
        self.assertIn('host=${h%%/*}; url="https://$h"', text)
        self.assertLess(text.index('if [ -z "${ORBIT_CONTROL_PLANE_IP:-}" ]; then'), text.index(line))
        self.assertIn("the authenticated probe was NOT sent", text)
        for l in text.splitlines():
            if "C_NO=$(curl" in l or "C_BAD=$(curl" in l:
                self.assertNotIn("dev-basic-auth-password", l)

    def test_the_ssh_key_override_never_reaches_a_command_line(self):
        """A2/B2: the override was handed to python as an argument before validation, so
        a traced shell printed it; python reads it from the environment now."""
        text = (ROOT / "scripts" / "lib-instance.sh").read_text()
        launch = next(l for l in text.splitlines() if "_ORBIT_SSH_KEY=$(python3 -" in l)
        self.assertNotIn("ORBIT_SSH_KEY:-", launch)
        self.assertIn('os.environ.get("ORBIT_SSH_KEY", "")', text)
        cfg = {"instance": {"name": "fx"}, "domains": {"primary": "fx.org", "dev": "dev.fx.org", "dnsZones": ["fx.org"]},
               "registry": "ghcr.io/fx", "git": {"repoUrl": "git@github.com:fx/i.git", "github_org": "fx"},
               "nodes": [{"name": "n1", "ip": "203.0.113.5", "role": "control-plane"}]}
        root = self._root(); (root / "instance").mkdir(); (root / "instance/config.yaml").write_text(yaml.safe_dump(cfg))
        env = {k: v for k, v in os.environ.items() if not k.startswith("ORBIT_")}
        env.update({"ORBIT_INSTANCE_CONFIG": str(root / "instance/config.yaml"), "HOME": str(root), "ORBIT_SSH_KEY": "PLANTED-KEY-BODY\nline2"})
        proc = subprocess.run(["bash", "-xc", "source " + str(ROOT / "scripts" / "lib-instance.sh") + "; echo rc=$?"], capture_output=True, text=True, timeout=60, env=env)
        self.assertIn("rc=2", proc.stdout)
        self.assertIn("ORBIT_SSH_KEY is not a filesystem path", proc.stderr)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_a_down_targets_job_label_is_shown_only_with_a_labels_shape(self):
        """A3 (a): the sustained-down row printed the scrape job label verbatim."""
        code = self._splice("stage5-platform.sh", "python3 - \"$TMP/targets.json\" \"$TMP/down.txt\" \"$TMP/count.txt\" <<'PY' 2>/dev/null\n", "\nPY\n")
        root = self._root()
        _write(root / "targets.json", json.dumps({"data": {"activeTargets": [
            {"health": "down", "scrapeUrl": "http://10.0.0.5:9100/metrics", "labels": {"job": "Bearer PLANTED_JOB_TOKEN_1234567890"}},
            {"health": "down", "scrapeUrl": "http://10.0.0.6:9100/metrics", "labels": {"job": "node-exporter"}},
            {"health": "up", "scrapeUrl": "http://10.0.0.7:9100/metrics", "labels": {"job": "x"}}]}}))
        proc = subprocess.run([sys.executable, "-c", code, str(root / "targets.json"), str(root / "down.txt"), str(root / "count.txt")], capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual((root / "down.txt").read_text().splitlines(), ["(a job label not of a label shape, or of a credential's shape) 10.0.0.5:9100", "node-exporter 10.0.0.6:9100"])
        self.assertEqual((root / "count.txt").read_text(), "3")
        self.assertNotIn("PLANTED", (root / "down.txt").read_text() + proc.stdout + proc.stderr)

    def test_a_middleware_annotation_ref_is_named_by_position(self):
        """A3 (b): an annotation ref that failed the shape was repeated verbatim."""
        code = self._splice("stage5-platform.sh", "MOUT=$(python3 - \"$IDIR/ingress.yaml\" <<'PY'\n", "\nPY\n")
        ann = re.search(r'ANN = "([^"]+)"', code).group(1)
        root = self._root()
        _write(root / "ingress.yaml", yaml.safe_dump({"kind": "Ingress", "metadata": {"name": "orbit-intake-github", "namespace": "orbit-agents-prod",
                                                       "annotations": {ann: "PLANTED ref token, orbit-agents-prod-PLANTED name@kubernetescrd"}}, "spec": {}}))
        proc = subprocess.run([sys.executable, "-c", code, str(root / "ingress.yaml")], capture_output=True, text=True, timeout=60)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("annotation ref #1 is not an orbit-agents-prod-<name>@kubernetescrd reference", proc.stdout)
        self.assertIn("annotation ref #2 names a middleware whose name is not an object name", proc.stdout)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_a_port_range_covering_9093_admits_alertmanager(self):
        """A4: `port: 9092, endPort: 9094` read as 'not 9093', so a namespace-wide grant
        to the unauthenticated Alertmanager API was skipped as harmless."""
        code = self._splice("stage5-platform.sh", "\ndef rule_admits_9093(rule):", "\ndef targets_alertmanager(doc):")
        code = "def rule_admits_9093(rule):" + code + "\nimport json, sys\nprint(json.dumps([rule_admits_9093(r) for r in json.loads(sys.argv[1])]))"
        rules = [{"ports": [{"port": 9092, "endPort": 9094}]}, {"ports": [{"port": 9092, "endPort": 9092}]},
                 {"ports": [{"port": 9092, "endPort": "PLANTED"}]}, {"ports": [{"port": 9093}]}, {"ports": [{"port": 9094}]},
                 {"ports": [{"port": 9000, "endPort": 9092}]}, {"ports": []}, {}]
        proc = subprocess.run([sys.executable, "-c", code, json.dumps(rules)], capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), [True, False, True, True, False, False, True, True])

    def test_a_generated_dashboard_uid_is_compared_after_the_generators_truncation(self):
        """A6: the generator truncates the uid to 40 characters; the row demanded the
        whole `orbit-red-<id>-<env>`, so a long project id was a permanent red."""
        text = self._stage("stage3.sh")
        start = text.index("for f in clusters/apps/dashboard-*.yaml; do")
        start = text.index('python3 -c "\n', start) + len('python3 -c "\n')
        code = text[start:text.index('\n" "$f" 2>/dev/null', start)]
        root = self._root()
        stem = "abcdefghijklmnopqrstuvwxyzabcdef-prod"
        def dash(uid):
            return yaml.safe_dump({"kind": "ConfigMap", "metadata": {"labels": {"grafana_dashboard": "1"}},
                                   "data": {"d.json": json.dumps({"uid": uid, "panels": [{"id": 1}]})}})
        f = root / f"dashboard-{stem}.yaml"
        _write(f, dash(f"orbit-red-{stem}"[:40]))
        self.assertEqual(subprocess.run([sys.executable, "-c", code, str(f)], capture_output=True, text=True, timeout=60).returncode, 0)
        _write(f, dash(f"orbit-red-{stem}"))
        self.assertNotEqual(subprocess.run([sys.executable, "-c", code, str(f)], capture_output=True, text=True, timeout=60).returncode, 0)

    def test_a_bare_service_name_resolves_in_its_own_documents_namespace(self):
        """A7: a file holding documents in two namespaces gave a bare Service name no
        home, so a correctly generated agent plane (a routing ConfigMap in the plane and
        a logs ConfigMap in loki, one file) was red."""
        code = self._splice("stage5-platform.sh", "SVCPORT=$(python3 - . <<'PY'\n", "\nPY\n")
        root = self._root(); (root / "catalog").mkdir(); _dirapp(root, "orbit-agents-prod")
        svc = {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "orbit-agents-x", "namespace": "orbit-agents-prod"}, "spec": {"ports": [{"port": 8080}]}}
        routing = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "routing", "namespace": "orbit-agents-prod"},
                   "data": {"routes.yaml": "x:\n  url: http://orbit-agents-x:8080/webhook\n"}}
        logs = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "logs-rules", "namespace": "loki"}, "data": {"r.yaml": "groups: []\n"}}
        _write(root / "clusters" / "apps" / "orbit-agents-prod.yaml", yaml.safe_dump_all([svc, routing, logs]))
        proc = subprocess.run([sys.executable, "-c", code, str(root)], cwd=root, capture_output=True, text=True, timeout=120)
        self.assertNotIn("bare Service name", proc.stdout + proc.stderr, proc.stdout)
        self.assertNotIn("orbit-agents-x", proc.stdout, proc.stdout)
        # the same URL in a document with NO namespace, in that two-namespace file: the
        # renderer applies it INTO the Application's destination (round 16: membership is
        # the renderer's), and there the bare name resolves — or does not
        routing["metadata"].pop("namespace"); _dirapp(root, "loki")
        _write(root / "clusters" / "apps" / "orbit-agents-prod.yaml", yaml.safe_dump_all([svc, routing, logs]))
        proc = subprocess.run([sys.executable, "-c", code, str(root)], cwd=root, capture_output=True, text=True, timeout=120)
        self.assertNotEqual(proc.returncode, 0); self.assertIn("names Service orbit-agents-x.loki, which this repo neither declares", proc.stdout)

    def test_a_catalog_components_bare_sibling_name_resolves_in_its_project_namespaces(self):
        """V1 §8: checkers-lab's api env `ENGINE_URL: http://checkers-lab-engine:8080` was
        read as a bare name 'in a file with no single namespace' and failed stage5 on the
        test box. A component's values render into <id>-<env> for every rendered env, and
        are judged there: a sibling that exists passes, one that does not stays RED."""
        code = self._splice("stage5-platform.sh", "SVCPORT=$(python3 - . <<'PY'\n", "\nPY\n")
        root = self._root(); _dirapp(root, "a-prod")
        svcs = [{"apiVersion": "v1", "kind": "Service", "metadata": {"name": "a-engine", "namespace": ns},
                 "spec": {"ports": [{"port": 8080}]}} for ns in ("a-prod", "a-dev")]
        _write(root / "clusters" / "apps" / "a.yaml", yaml.safe_dump_all(svcs))
        def catalog(url, dev=True):
            return yaml.safe_dump({"projects": [{"id": "a", "environments": {"prod": {}, "dev": {"enabled": dev}},
                                                 "components": [{"name": "api", "env": {"ENGINE_URL": url}}]}]})
        _write(root / "catalog" / "projects.yaml", catalog("http://a-engine:8080"))
        proc = subprocess.run([sys.executable, "-c", code, str(root)], cwd=root, capture_output=True, text=True, timeout=120)
        self.assertNotIn("bare Service name", proc.stdout + proc.stderr, proc.stdout)
        self.assertNotIn("a-engine", proc.stdout, proc.stdout)
        _write(root / "catalog" / "projects.yaml", catalog("http://a-nope:8080"))
        proc = subprocess.run([sys.executable, "-c", code, str(root)], cwd=root, capture_output=True, text=True, timeout=120)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("names Service a-nope.a-prod, which this repo neither declares", proc.stdout)
        self.assertIn("names Service a-nope.a-dev, which this repo neither declares", proc.stdout)
        # the port is judged too: the sibling exists but does not declare 9090
        _write(root / "catalog" / "projects.yaml", catalog("http://a-engine:9090", dev=False))
        proc = subprocess.run([sys.executable, "-c", code, str(root)], cwd=root, capture_output=True, text=True, timeout=120)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("a-engine", proc.stdout)
        self.assertNotIn("a-dev", proc.stdout)

    def test_a_sealed_file_repeating_a_key_is_refused(self):
        """B1: a plaintext first occurrence hid behind a sealed second one — YAML keeps
        the last value. The sealed reader refuses a repeated key."""
        text = self._stage("stage2.sh")
        block = text[text.index("SOPSF=clusters/platform/secrets/cert-manager/cloudflare-api-token.yaml"):text.index('check "no key material tracked in git"')]
        root = self._root()
        dup = SEALED_TOKEN_FILE.replace("      api-token: ENC[", "      api-token: PLANTED-PLAINTEXT-TOKEN-1234567890\n      api-token: ENC[")
        _write(root / "clusters/platform/secrets/cert-manager/cloudflare-api-token.yaml", dup)
        script = "ok(){ echo \"  ok    $1\"; }; bad(){ echo \"  FAIL  $1\"; }\n" + block
        proc = subprocess.run(["bash", "-c", script], cwd=root, capture_output=True, text=True, timeout=60)
        self.assertIn("FAIL  the sealed cert-manager token file cannot be read as YAML (ConstructorError; a repeated key counts)", proc.stdout)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_an_unreadable_door_file_is_reported_by_class_only(self):
        """B4: an undefined YAML alias in the door's ingress file put the parser's
        message, which quotes the alias, on stderr."""
        for start in ("DOUT=$(python3 - \"$IDIR/ingress.yaml\" \"$IHOST\" <<'PY'\n", "MOUT=$(python3 - \"$IDIR/ingress.yaml\" <<'PY'\n",
                      "WIOUT=$(python3 - \"$TMP/intake-netpols.yaml\" <<'PY'\n", "WOUT=$(python3 - \"$NETPOL\" <<'PY'\n"):
            with self.subTest(block=start[:6]):
                code = self._splice("stage5-platform.sh", start, "\nPY\n")
                root = self._root(); _write(root / "f.yaml", "kind: Ingress\nmetadata: *PLANTED_ALIAS_CREDENTIAL\n")
                proc = subprocess.run([sys.executable, "-c", code, str(root / "f.yaml"), "hooks.fx.org"], capture_output=True, text=True, timeout=60)
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("cannot be read as YAML (ComposerError)", proc.stdout)
                self.assertNotIn("PLANTED", proc.stdout + proc.stderr)


class Round9Test(unittest.TestCase):
    """Area-1 sweep round 9: each row pins one repair."""

    def _stage(self, name):
        return (ROOT / "scripts" / "verify" / name).read_text()

    def _splice(self, stage, start, end):
        text = self._stage(stage)
        i = text.index(start) + len(start)
        return text[i:text.index(end, i)]

    def _root(self):
        root = pathlib.Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        return root

    def test_the_middleware_rows_repeat_no_file_text(self):
        """A2: a rejected sourceCriterion scalar, an unexpected key and a type key were
        interpolated with %r."""
        code = self._splice("stage5-platform.sh", "MOUT=$(python3 - \"$IDIR/ingress.yaml\" <<'PY'\n", "\nPY\n")
        ann = re.search(r'ANN = "([^"]+)"', code).group(1)
        root = self._root()
        def mw(name, spec):
            return {"apiVersion": "traefik.io/v1alpha1", "kind": "Middleware", "metadata": {"name": name, "namespace": "orbit-agents-prod"}, "spec": spec}
        ing = {"kind": "Ingress", "metadata": {"name": "orbit-intake-github", "namespace": "orbit-agents-prod",
                                                "annotations": {ann: ",".join(f"orbit-agents-prod-{n}@kubernetescrd" for n in ("cap", "rate", "odd", "keyed"))}}, "spec": {}}
        docs = [ing, mw("cap", {"inFlightReq": {"amount": 10, "sourceCriterion": "Bearer PLANTED_SCALAR_1234567890"}}),
                mw("rate", {"rateLimit": {"average": 10, "burst": 10}}),
                mw("odd", {"PLANTED TYPE token": {}}),
                mw("keyed", {"inFlightReq": {"amount": 10, "sourceCriterion": {"PLANTED-key": True}}})]
        _write(root / "ingress.yaml", yaml.safe_dump_all(docs))
        proc = subprocess.run([sys.executable, "-c", code, str(root / "ingress.yaml")], capture_output=True, text=True, timeout=60)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("sets sourceCriterion to something that is not a map (its value is not repeated)", proc.stdout)
        self.assertIn("is of type (a type key not of a key shape; not repeated)", proc.stdout)
        self.assertIn("sourceCriterion has 1 unexpected key(s) (not repeated)", proc.stdout)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_lokis_secret_access_is_denied_for_get_list_and_watch(self):
        """A4: only `get` was asked; a Role granting `list` hands over every Secret."""
        text = self._stage("stage5-platform.sh")
        self.assertEqual(text.count("for verb in get list watch; do"), 2)
        for line in text.splitlines():
            if "kubectl auth can-i --as=system:serviceaccount:loki:loki" in line:
                self.assertIn('"$verb" secrets', line, line)

    def test_a_policy_selecting_alertmanager_by_expression_is_audited(self):
        """A5: only matchLabels app.kubernetes.io/name=alertmanager counted, so a policy
        selecting the same pod through matchExpressions (or another of its labels) was
        skipped and its namespace-wide grant certified narrow."""
        code = self._splice("stage5-platform.sh", "\ndef targets_alertmanager(doc):", "\npolicies = [d for d in docs")
        code = "def targets_alertmanager(doc):" + code + "\nimport json, sys\nprint(json.dumps([targets_alertmanager(d) for d in json.loads(sys.argv[1])]))"
        def pol(psel):
            return {"kind": "NetworkPolicy", "metadata": {"namespace": "monitoring"}, "spec": {"podSelector": psel}}
        cases = [pol({"matchLabels": {"app.kubernetes.io/name": "alertmanager"}}),                                   # True
                 pol({"matchExpressions": [{"key": "app.kubernetes.io/name", "operator": "In", "values": ["alertmanager"]}]}),  # True
                 pol({"matchLabels": {"alertmanager": "kube-prometheus-stack-alertmanager"}}),                       # True (a label it carries)
                 pol({"matchExpressions": [{"key": "app.kubernetes.io/name", "operator": "Exists"}]}),               # True
                 pol({"matchLabels": {"app.kubernetes.io/name": "grafana"}}),                                        # False
                 pol({"matchExpressions": [{"key": "app.kubernetes.io/name", "operator": "In", "values": ["grafana", "loki"]}]}),  # False
                 pol({"matchExpressions": [{"key": "app.kubernetes.io/name", "operator": "NotIn", "values": ["alertmanager"]}]}),  # False
                 pol({"matchExpressions": [{"key": "app.kubernetes.io/managed-by", "operator": "DoesNotExist"}]}),   # False
                 pol({}),                                                                                            # True
                 {"kind": "NetworkPolicy", "metadata": {"namespace": "loki"}, "spec": {"podSelector": {}}}]          # False (other ns)
        proc = subprocess.run([sys.executable, "-c", code, json.dumps(cases)], capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), [True, True, True, True, False, False, False, False, True, False])

    def test_the_intake_grant_pins_the_intake_target_and_the_traefik_source(self):
        """A6: any non-empty selector counted as narrowed, so `Exists` on both sides (every
        namespace, every pod) was certified as the traefik->intake grant."""
        code = self._splice("stage5-platform.sh", "WIOUT=$(python3 - \"$TMP/intake-netpols.yaml\" <<'PY'\n", "\nPY\n")
        root = self._root()
        target = {"matchLabels": {"app.kubernetes.io/instance": "orbit-agents", "app.kubernetes.io/name": "intake"}}
        peer = {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "traefik"}}, "podSelector": {"matchLabels": {"app.kubernetes.io/name": "traefik"}}}
        def netpol(psel, peers):
            return yaml.safe_dump({"kind": "NetworkPolicy", "metadata": {"name": "intake-from-traefik", "namespace": "orbit-agents-prod"},
                                   "spec": {"podSelector": psel, "ingress": [{"ports": [{"port": 8080, "protocol": "TCP"}], "from": peers}]}})
        def run(text):
            _write(root / "netpol.yaml", text)
            return subprocess.run([sys.executable, "-c", code, str(root / "netpol.yaml")], capture_output=True, text=True, timeout=60)
        self.assertEqual(run(netpol(target, [peer])).returncode, 0)
        expr_peer = {"namespaceSelector": {"matchExpressions": [{"key": "kubernetes.io/metadata.name", "operator": "In", "values": ["traefik"]}]},
                     "podSelector": {"matchExpressions": [{"key": "app.kubernetes.io/name", "operator": "In", "values": ["traefik"]}]}}
        self.assertEqual(run(netpol(target, [expr_peer])).returncode, 0)
        exists = {"namespaceSelector": {"matchExpressions": [{"key": "kubernetes.io/metadata.name", "operator": "Exists"}]},
                  "podSelector": {"matchExpressions": [{"key": "app.kubernetes.io/name", "operator": "Exists"}]}}
        for psel, peers, needle in ((target, [exists], "is not narrowed on BOTH sides in ONE element to the traefik namespace AND the traefik pod"),
                                    (target, [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "traefik"}}, "podSelector": {"matchLabels": {"app.kubernetes.io/name": "PLANTED"}}}], "is not narrowed on BOTH sides"),
                                    (target, [{"namespaceSelector": {"matchExpressions": [{"key": "kubernetes.io/metadata.name", "operator": "In", "values": ["traefik", "PLANTED"]}]}, "podSelector": {"matchLabels": {"app.kubernetes.io/name": "traefik"}}}], "is not narrowed on BOTH sides"),
                                    ({"matchExpressions": [{"key": "app.kubernetes.io/name", "operator": "Exists"}]}, [peer], "does not pin its target to the intake pod"),
                                    ({"matchLabels": {"app.kubernetes.io/name": "intake"}}, [peer], "does not pin its target to the intake pod")):
            with self.subTest(needle=needle[:30]):
                proc = run(netpol(psel, peers))
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn(needle, proc.stdout)
                self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_the_nodes_inotify_answer_never_sits_in_a_shell_variable_unchecked(self):
        """B1: ssh's output was captured into INST before the digit check, so a traced
        shell printed whatever the node said."""
        text = self._stage("stage2.sh")
        block = text[text.index("INSTF=$(mktemp)"):text.index('if [ -n "$INST" ] && [ "$INST" -ge 512 ]')]
        root = self._root()
        for answer, want in (("PLANTED-REMOTE-PASSWORD", "[]"), ("576", "[576]"), ("576\nPLANTED", "[]")):
            with self.subTest(answer=answer[:8]):
                _write(root / "ssh", "#!/bin/sh\nprintf '%s\\n' \"$SSH_ANSWER\"\n", executable=True)
                proc = subprocess.run(["bash", "-xc", "ORBIT_SSH_KEY=/k; IP=203.0.113.9\n" + block + '\necho "[$INST]"'], capture_output=True, text=True, timeout=60,
                                      env={**os.environ, "PATH": str(root) + os.pathsep + os.environ["PATH"], "SSH_ANSWER": answer})
                self.assertIn(want, proc.stdout)
                self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_a_live_secret_reference_is_shown_only_as_a_secret_name(self):
        """B2: the renderer's and Grafana's secretKeyRef names came back from kubectl
        into shell variables and were printed verbatim."""
        text = self._stage("stage5-platform.sh")
        fn = next(l for l in text.splitlines() if l.strip().startswith("secret_name_in(){"))
        self.assertIn('RS=$(secret_name_in "$RSF"); GS=$(secret_name_in "$GSF")', text)
        root = self._root()
        for content, want in (("PLANTED-RENDERER-PASSWORD value", "(not a Secret name)"), ("grafana-renderer-token\n", "grafana-renderer-token"), ("", "(not a Secret name)")):
            with self.subTest(content=content[:8]):
                f = root / "ans.txt"; f.write_text(content)
                proc = subprocess.run(["bash", "-c", fn + '\nsecret_name_in "$1"', "_", str(f)], capture_output=True, text=True, timeout=60)
                self.assertEqual(proc.stdout.strip(), want)
                self.assertFalse(f.exists())
                self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_a_malformed_authority_is_never_repeated(self):
        """B3: `http://[text].monitoring.svc:90/` was shown whole in the finding."""
        code = self._splice("stage5-platform.sh", "SVCPORT=$(python3 - . <<'PY'\n", "\nPY\n")
        root = self._root(); (root / "catalog").mkdir()
        cm = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "c", "namespace": "monitoring"},
              "data": {"u": "http://[PLANTED-PASSWORD].monitoring.svc:90/"}}
        _write(root / "clusters" / "c.yaml", yaml.safe_dump(cm))
        proc = subprocess.run([sys.executable, "-c", code, str(root)], cwd=root, capture_output=True, text=True, timeout=120)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("names a malformed host", proc.stdout)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def _monpol(self, name, workload, ingress=None, types=None):
        spec = {"podSelector": {"matchLabels": {"app.kubernetes.io/name": workload}}}
        if ingress is not None: spec["ingress"] = ingress
        if types is not None: spec["policyTypes"] = types
        return {"kind": "NetworkPolicy", "metadata": {"name": name, "namespace": "monitoring"}, "spec": spec}

    def test_a_monitoring_policy_is_named_by_shape_or_position(self):
        """B4: the Alertmanager audit printed a policy's name verbatim."""
        code = self._splice("stage5-platform.sh", "WOUT=$(python3 - \"$NETPOL\" <<'PY'\n", "\nPY\n")
        root = self._root()
        docs = [self._monpol("prom", "prometheus", []), self._monpol("graf", "grafana", []),
                self._monpol("PLANTED-PASSWORD value", "alertmanager", [{"ports": [{"port": 9093}]}])]
        _write(root / "netpol.yaml", yaml.safe_dump_all(docs))
        proc = subprocess.run([sys.executable, "-c", code, str(root / "netpol.yaml")], capture_output=True, text=True, timeout=60)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('''policy "(document #2, a name not of an object-name shape, or of a credential's shape)" ingress[0] admits every peer''', proc.stdout)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_default_deny_ingress_needs_ingress_in_the_policy_types(self):
        """B7: three policies with policyTypes [Egress] and no ingress field were
        counted as three default-deny INGRESS isolations."""
        code = self._splice("stage5-platform.sh", "WOUT=$(python3 - \"$NETPOL\" <<'PY'\n", "\nPY\n")
        root = self._root()
        def run(types):
            docs = [self._monpol(n, w, [], types) for n, w in (("prom", "prometheus"), ("graf", "grafana"), ("am", "alertmanager"))]
            _write(root / "netpol.yaml", yaml.safe_dump_all(docs))
            return subprocess.run([sys.executable, "-c", code, str(root / "netpol.yaml")], capture_output=True, text=True, timeout=60)
        self.assertEqual(run(None).returncode, 0, run(None).stdout)
        self.assertEqual(run(["Ingress"]).returncode, 0)
        self.assertEqual(run(["Ingress", "Egress"]).returncode, 0)
        proc = run(["Egress"])
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("for INGRESS (policyTypes without Ingress isolates nothing on the way in)", proc.stdout)


class Round10Test(unittest.TestCase):
    """Area-1 sweep round 10: each row pins one repair."""

    def _stage(self, name):
        return (ROOT / "scripts" / "verify" / name).read_text()

    def _splice(self, stage, start, end):
        text = self._stage(stage)
        i = text.index(start) + len(start)
        return text[i:text.index(end, i)]

    def _root(self):
        root = pathlib.Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        return root

    def _bash(self, script, cwd=None, env=None, trace=False):
        return subprocess.run(["bash"] + (["-x"] if trace else []) + ["-c", script], cwd=cwd, capture_output=True,
                              text=True, timeout=120, env={**os.environ, **(env or {})})

    def _py(self, code, *args):
        return subprocess.run([sys.executable, "-c", code] + [str(a) for a in args], capture_output=True, text=True, timeout=60)

    def test_the_authenticated_probe_uses_no_proxy(self):
        """A1: with a SOCKS5h proxy in the environment, curl hands the NAME to the proxy
        and --resolve pins nothing; the credential-bearing request goes direct."""
        text = self._stage("stage3.sh")
        line = next(l for l in text.splitlines() if 'curluser "$ORBIT_CONFIG_DIR/dev-basic-auth-password" orbit' in l)
        self.assertIn("--noproxy '*'", line); self.assertIn("--resolve", line)

    def test_the_ingress_list_never_becomes_a_shell_variable(self):
        """A2: the whole ingress JSON (annotations included) sat in a shell variable a
        traced shell prints; it goes to a private file the reader opens by path."""
        text = self._stage("stage3.sh")
        self.assertNotIn('ING=$(kubectl -n orbit-agents-prod get ingress', text)
        self.assertNotIn('printf \'%s\' "${ING:-null}"', text)
        block = text[text.index("  INGF=$(mktemp)"):text.index('  rm -f "$INGF"') + len('  rm -f "$INGF"')]
        root = self._root()
        _write(root / "bin" / "kubectl", "#!/bin/sh\nprintf '%s' '{\"items\": [{\"metadata\": {\"name\": \"door\", \"annotations\": {\"x\": \"PLANTED_ANNOTATION_CREDENTIAL_123\"}}, \"spec\": {\"rules\": []}}]}'\n", executable=True)
        proc = self._bash("ok(){ echo \"  ok    $1\"; }; bad(){ echo \"  FAIL  $1\"; }\nINTAKEWIRED=wired\n" + block + "\n  fi\n",
                          env={"PATH": str(root / "bin") + os.pathsep + os.environ["PATH"]}, trace=True)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)
        self.assertIn("IOUT='OK 1 ingress(es) in the plane namespace", proc.stderr)

    def test_a_protected_hostname_refusal_names_no_environment_key(self):
        """A3 (stage3): the refusal repeated the catalog's project id and environment key."""
        text = self._stage("stage3.sh")
        block = text[text.index("PROTBAD=$(mktemp)"):text.index("\ndone\n", text.index("for h in $PROTHOSTS; do")) + len("\ndone\n")]
        root = self._root(); (root / "catalog").mkdir()
        (root / "catalog/projects.yaml").write_text(
            "projects:\n  - id: PLANTED-ID\n    environments:\n      'PLANTED KEY': {enabled: true, hostnames: ['user:PLANTED-PW@dev.example']}\n")
        proc = self._bash("ok(){ echo \"ok $1\"; }; bad(){ echo \"FAIL $1\"; }\n" + block, cwd=root,
                          env={"ORBIT_DEV_DOMAIN": "dev.example", "ORBIT_CONFIG_DIR": str(root)})
        self.assertIn("projects[0]/(an environment key not of a label shape, or of a credential's shape) hostname 1 is not a DNS name", proc.stdout)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_a_dashboard_data_key_is_shown_only_when_shaped(self):
        """A3 (stage5): a dashboard ConfigMap's data key was interpolated with %r."""
        code = self._splice("stage5-platform.sh", "DSHAPE=$(python3 - \"${DASHLIST[@]}\" <<'PY'\n", "\nPY\n")
        root = self._root()
        _write(root / "d.yaml", "kind: ConfigMap\nmetadata: {name: dashboard-x}\ndata:\n  'PLANTED PASSWORD key': 'not json'\n  ok.json: '{\"title\": \"x\"}'\n")
        proc = self._py(code, root / "d.yaml")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("key #0 (a key not of a key shape, or of a credential's shape) is not parseable JSON", proc.stdout)
        self.assertIn("key ok.json has no uid", proc.stdout)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_the_intake_door_needs_one_effective_traefik_grant(self):
        """A5: a policy that isolated the intake and admitted nobody (no rules, or no
        Ingress in policyTypes) passed as 'every peer narrowed' — over zero peers."""
        code = self._splice("stage5-platform.sh", "WIOUT=$(python3 - \"$TMP/intake-netpols.yaml\" <<'PY'\n", "\nPY\n")
        src = ROOT / "template" / "provisioning" / "clusters" / "platform" / "config" / "orbit-intake" / "netpol.yaml"
        docs = [d for d in yaml.safe_load_all(src.read_text().replace("{{", "").replace("}}", "")) if isinstance(d, dict)]
        pol = next(d for d in docs if d.get("kind") == "NetworkPolicy" and (d.get("metadata") or {}).get("namespace") == "orbit-agents-prod")
        root = self._root()
        def run(docs):
            _write(root / "netpol.yaml", yaml.safe_dump_all(docs)); return self._py(code, root / "netpol.yaml")
        base = run(docs); self.assertEqual(base.returncode, 0, base.stdout); self.assertIn("1 effective traefik grant(s)", base.stdout)
        keep = copy.deepcopy(pol["spec"])
        pol["spec"]["ingress"] = []
        proc = run(docs); self.assertNotEqual(proc.returncode, 0); self.assertIn("no effective rule admits the traefik pod", proc.stdout)
        pol["spec"] = copy.deepcopy(keep); pol["spec"]["policyTypes"] = ["Egress"]
        proc = run(docs); self.assertNotEqual(proc.returncode, 0); self.assertIn("does not apply to Ingress", proc.stdout)

    def test_a_logs_trigger_read_that_failed_is_not_an_absent_trigger(self):
        """A6: any non-zero from `kubectl get configmap` keyed the loki->:9093 proof off
        as 'no logs trigger deployed'; only a NotFound does now."""
        text = self._stage("stage5-platform.sh")
        start = text.index("    kubectl get configmap -n loki orbit-agents-logs-triggers -o name")
        block = text[start:text.index('    elif [ "$LCMRC" -eq 0 ]; then', start)] + "    fi\n"
        root = self._root()
        for stderr, want in (("Error from server (Forbidden): PLANTED-DETAIL", "FAIL"), ("Error from server (NotFound): configmaps \"x\" not found", "")):
            with self.subTest(stderr=stderr[:30]):
                _write(root / "bin" / "kubectl", "#!/bin/sh\necho '%s' >&2; exit 1\n" % stderr, executable=True)
                proc = self._bash("ok(){ echo \"  ok    $1\"; }; bad(){ echo \"  FAIL  $1\"; }\nTMP=$(mktemp -d)\n" + block,
                                  env={"PATH": str(root / "bin") + os.pathsep + os.environ["PATH"]})
                self.assertEqual("FAIL" in proc.stdout, want == "FAIL", proc.stdout)
                self.assertNotIn("PLANTED", proc.stdout + proc.stderr)
        self.assertIn("could not read the logs-trigger ConfigMap", text)

    def test_discovery_output_never_becomes_a_shell_variable(self):
        """B1: the api-resources listing sat in a shell variable before the shape filter."""
        text = self._stage("stage4.sh")
        self.assertNotIn('SERVED=$(kubectl api-resources', text)
        block = text[text.index("  DISCF=$(mktemp)"):text.index('  rm -f "$DISCF"') + len('  rm -f "$DISCF"')]
        root = self._root()
        _write(root / "bin" / "kubectl", "#!/bin/sh\nprintf 'externalsecrets.external-secrets.io\\nPLANTED_DISCOVERY_CREDENTIAL_123\\n'\n", executable=True)
        proc = self._bash("old_group=external-secrets.io\n" + block + "\necho \"SERVED=$SERVED\"\n",
                          env={"PATH": str(root / "bin") + os.pathsep + os.environ["PATH"]}, trace=True)
        self.assertIn("SERVED=externalsecrets.external-secrets.io (+1 resource name(s) not of an object-name shape, or of a credential's shape)", proc.stdout)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_the_collectors_row_repeats_no_unshaped_object_name(self):
        """B2: the row printed every (kind, name, namespace) the file declares, verbatim."""
        code = self._splice("stage5-platform.sh", "SC_OUT=$(python3 - \"$SCOLL\" \"$FEATURE_LOGS\" <<'PY'\n", "\nPY\n")
        root = self._root()
        _write(root / "sc.yaml", "kind: PodMonitor\nmetadata: {name: 'PLANTED PASSWORD', namespace: traefik}\n---\nkind: ServiceMonitor\nmetadata: {name: alloy, namespace: monitoring}\n")
        proc = self._py(code, root / "sc.yaml", "0")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("declares [('ServiceMonitor', 'alloy', 'monitoring')] (+1 object(s) whose kind, name or namespace is not of an object-name shape, or of a credential's shape) but not", proc.stdout)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_a_probe_marker_is_a_status_or_unreadable(self):
        """B3: the probe pod's marker values were read into the case/arithmetic as-is."""
        text = self._stage("stage5-platform.sh")
        # round 15: the marker is shaped INSIDE the pipeline that reads the file — the raw
        # value never sits in a shell variable (a traced shell prints assignments)
        fn = next(l for l in text.splitlines() if l.startswith("marker(){"))
        self.assertNotIn("http3", text); self.assertNotIn("LNOUT", text)
        for name, key in (("OPPROM", "PROM"), ("OPLOKI", "LOKI"), ("OPAM", "AM"), ("OPTEMPO", "TEMPO"), ("OPCTL", "CONTROL")):
            self.assertIn('%s=$(marker %s "$OPF")' % (name, key), text)
        self.assertIn('LNAM=$(marker AM "$LNF"); LNCTL=$(marker CONTROL "$LNF")', text)
        root = self._root()
        out = ""
        for v in ("200", "503", "", "PLANTED;$(id)", "20", "2000", "abc"):
            _write(root / "m", "X=1\n" + ("PROM=%s\n" % v if v else ""))
            proc = self._bash(fn + "\nprintf '[%s]' \"$(marker PROM " + str(root / "m") + ")\"\n", trace=True)
            out += proc.stdout; self.assertNotIn("PLANTED", proc.stderr)
        self.assertEqual(out, "[200][503][][unreadable][unreadable][unreadable][unreadable]")

    def test_a_namespace_reaches_the_promql_exclusion_only_as_a_name(self):
        """B4: a project namespace's name was spliced into a PromQL regex unvalidated."""
        text = self._stage("stage5-platform.sh")
        start = text.index('  EXCL="kube-system"')
        block = text[start:text.index("over-reservation exclusion\"", start) + len("over-reservation exclusion\"")]
        out = self._bash("ok(){ echo \"  ok    $1\"; }; bad(){ echo \"  FAIL  $1\"; }\nproject_namespaces(){ printf '%s\\n' orbit-agents-prod 'bad\"}or{x=~\".*|PLANTED' 'Upper' 'trailing-'; }\n" + block + "\necho \"EXCL=$EXCL\"\n").stdout
        self.assertIn("EXCL=kube-system|orbit-agents-prod\n", out)
        self.assertIn("FAIL  3 project namespace(s) have a name that is not a namespace name", out)
        self.assertNotIn("PLANTED", out); self.assertNotIn("Upper", out)

    def test_every_monitoring_policy_under_clusters_is_audited(self):
        """B7: only one netpol file was read; a second file selecting Alertmanager
        widened the grant unseen. Every NetworkPolicy for ns monitoring is gathered."""
        gather = self._splice("stage5-platform.sh", "gather_netpols(){ python3 - clusters \"$2\" \"$1\" <<'PY'\n", "\nPY\n")
        audit = self._splice("stage5-platform.sh", "WOUT=$(python3 - \"$NETPOL\" <<'PY'\n", "\nPY\n")
        root = self._root(); _dirapp(root)
        src = ROOT / "template" / "provisioning" / "clusters" / "platform" / "config" / "monitoring" / "netpol.yaml"
        _write(root / "clusters" / "platform" / "config" / "monitoring" / "netpol.yaml", src.read_text())
        _write(root / "clusters" / "elsewhere" / "extra.yaml", yaml.safe_dump_all([
            {"kind": "Deployment", "metadata": {"name": "x", "namespace": "monitoring"}},
            {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": {"name": "wide", "namespace": "monitoring"},
             "spec": {"podSelector": {}, "ingress": [{"ports": [{"port": 9093}]}]}},
            {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": {"name": "other-ns", "namespace": "loki"},
             "spec": {"podSelector": {}, "ingress": [{}]}}]))
        proc = subprocess.run([sys.executable, "-c", gather, "clusters", str(root / "gathered.yaml"), "monitoring"], cwd=root, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        docs = [d for d in yaml.safe_load_all((root / "gathered.yaml").read_text()) if isinstance(d, dict)]
        self.assertTrue(all(d["kind"] == "NetworkPolicy" and d["metadata"]["namespace"] == "monitoring" for d in docs))
        self.assertIn("wide", [d["metadata"]["name"] for d in docs]); self.assertNotIn("other-ns", [d["metadata"]["name"] for d in docs])
        proc = self._py(audit, root / "gathered.yaml")
        self.assertNotEqual(proc.returncode, 0); self.assertIn("'wide'", proc.stdout)


class Round11Test(unittest.TestCase):
    """Area-1 sweep round 11: each row pins one repair."""

    def _stage(self, name):
        return (ROOT / "scripts" / "verify" / name).read_text()

    def _splice(self, stage, start, end):
        text = self._stage(stage)
        i = text.index(start) + len(start)
        return text[i:text.index(end, i)]

    def _root(self):
        root = pathlib.Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        return root

    def _bash(self, script, cwd=None, env=None, trace=False):
        return subprocess.run(["bash"] + (["-x"] if trace else []) + ["-c", script], cwd=cwd, capture_output=True,
                              text=True, timeout=120, env={**os.environ, **(env or {})})

    def test_a_red_ingress_checker_is_a_red_row(self):
        """A1: `rm -f "$INGF"` replaced the checker's exit status before the branch read
        it, so a FAIL from the checker was printed inside an ok row."""
        text = self._stage("stage3.sh")
        self.assertIn('\' "$INGF"); IRC=$?\n  rm -f "$INGF"\n    if [ "$IRC" -eq 0 ]; then', text)
        block = text[text.index("  INGF=$(mktemp)"):text.index('      bad "orbit-agents-prod: $IOUT"') + len('      bad "orbit-agents-prod: $IOUT"')] + "\n    fi\n  fi\n"
        root = self._root()
        _write(root / "bin" / "kubectl", "#!/bin/sh\nprintf '%s' '{\"items\": [{\"metadata\": {\"name\": \"agent\"}, \"spec\": {\"rules\": [{\"host\": \"a.fixture.org\", \"http\": {\"paths\": [{\"path\": \"/\", \"pathType\": \"Prefix\", \"backend\": {\"service\": {\"name\": \"orbit-agents-triage\"}}}]}}]}}]}'\n", executable=True)
        proc = self._bash("ok(){ echo \"  ok    $1\"; }; bad(){ echo \"  FAIL  $1\"; }\n" + block,
                          env={"PATH": str(root / "bin") + os.pathsep + os.environ["PATH"]})
        self.assertIn("  FAIL  orbit-agents-prod: FAIL", proc.stdout); self.assertNotIn("  ok ", proc.stdout)

    def test_every_yaml_suffix_the_renderer_reads_is_gathered(self):
        """A3: the gatherer read *.yaml only; the renderer reads .yml too."""
        gather = self._splice("stage5-platform.sh", "gather_netpols(){ python3 - clusters \"$2\" \"$1\" <<'PY'\n", "\nPY\n")
        root = self._root(); _dirapp(root)
        _write(root / "clusters" / "a.yml", yaml.safe_dump({"kind": "NetworkPolicy", "metadata": {"name": "widen", "namespace": "monitoring"}, "spec": {"podSelector": {}}}))
        _write(root / "clusters" / "b.yaml", yaml.safe_dump({"kind": "NetworkPolicy", "metadata": {"name": "plain", "namespace": "monitoring"}, "spec": {"podSelector": {}}}))
        _write(root / "clusters" / "c.txt", yaml.safe_dump({"kind": "NetworkPolicy", "metadata": {"name": "not-yaml", "namespace": "monitoring"}, "spec": {}}))
        proc = subprocess.run([sys.executable, "-c", gather, "clusters", str(root / "g.yaml"), "monitoring"], cwd=root, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        names = sorted(d["metadata"]["name"] for d in yaml.safe_load_all((root / "g.yaml").read_text()) if isinstance(d, dict))
        self.assertEqual(names, ["plain", "widen"])

    def test_a_loki_peer_is_narrowed_only_by_a_positive_pin(self):
        """A4: any non-empty matchExpressions counted as pod-narrowed, so DoesNotExist on
        a label no pod carries (every pod in loki) was certified narrow."""
        code = self._splice("stage5-platform.sh", "WOUT=$(python3 - \"$NETPOL\" <<'PY'\n", "\nPY\n")
        src = ROOT / "template" / "provisioning" / "clusters" / "platform" / "config" / "monitoring" / "netpol.yaml"
        docs = [d for d in yaml.safe_load_all(src.read_text()) if isinstance(d, dict)]
        am = next(d for d in docs if d.get("kind") == "NetworkPolicy" and "alertmanager" in yaml.safe_dump(d.get("spec", {}).get("podSelector", {})))
        root = self._root()
        def run(psel):
            am["spec"]["ingress"] = [{"ports": [{"port": 9093}], "from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "loki"}}, "podSelector": psel}]}]
            _write(root / "netpol.yaml", yaml.safe_dump_all(docs))
            return subprocess.run([sys.executable, "-c", code, str(root / "netpol.yaml")], capture_output=True, text=True, timeout=60)
        for psel in ({"matchExpressions": [{"key": "orbit-review-unused", "operator": "DoesNotExist"}]},
                     {"matchExpressions": [{"key": "app.kubernetes.io/name", "operator": "Exists"}]},
                     {"matchExpressions": [{"key": "app.kubernetes.io/name", "operator": "NotIn", "values": ["grafana"]}]},
                     {"matchExpressions": [{"key": "app.kubernetes.io/name", "operator": "In", "values": []}]},
                     {"matchLabels": {}}):
            with self.subTest(psel=psel):
                proc = run(psel); self.assertNotEqual(proc.returncode, 0); self.assertIn("bare namespace-wide loki grant", proc.stdout)
        for psel in ({"matchLabels": {"app.kubernetes.io/name": "loki"}},
                     {"matchExpressions": [{"key": "app.kubernetes.io/name", "operator": "In", "values": ["loki"]}]}):
            with self.subTest(psel=psel):
                proc = run(psel); self.assertEqual(proc.returncode, 0, proc.stdout); self.assertIn("positive label pin", proc.stdout)

    def test_a_nested_middleware_reference_is_shaped_before_it_is_named(self):
        """A5: a chain's nested middleware name was queued and interpolated with %r
        unvalidated; a multi-type middleware's keys were repeated verbatim."""
        code = self._splice("stage5-platform.sh", "MOUT=$(python3 - \"$IDIR/ingress.yaml\" <<'PY'\n", "\nPY\n")
        ann = re.search(r'ANN = "([^"]+)"', code).group(1)
        root = self._root()
        def mw(name, spec):
            return {"apiVersion": "traefik.io/v1alpha1", "kind": "Middleware", "metadata": {"name": name, "namespace": "orbit-agents-prod"}, "spec": spec}
        ing = {"kind": "Ingress", "metadata": {"name": "orbit-intake-github", "namespace": "orbit-agents-prod",
                                                "annotations": {ann: ",".join(f"orbit-agents-prod-{n}@kubernetescrd" for n in ("guard", "multi"))}}, "spec": {}}
        docs = [ing, mw("guard", {"chain": {"middlewares": [{"name": "Bearer PLANTED_NESTED_CREDENTIAL"}, {"name": "cap"}]}}),
                mw("cap", {"inFlightReq": {"amount": 10}}), mw("multi", {"rateLimit": {"average": 1}, "PLANTED KEY": {}})]
        _write(root / "ingress.yaml", yaml.safe_dump_all(docs))
        proc = subprocess.run([sys.executable, "-c", code, str(root / "ingress.yaml")], capture_output=True, text=True, timeout=60)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("middleware 'guard' chain entry #0 names a middleware whose name is not an object name (not repeated)", proc.stdout)
        self.assertIn("middleware 'multi' sets 2 types (rateLimit +1 not of a key shape)", proc.stdout)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_the_wal_archiver_row_carries_only_integers(self):
        """A6: the archiver query's output was split in the shell and printed as-is."""
        text = self._stage("stage3.sh")
        self.assertNotIn("ARCHROW=$(kubectl", text)
        block = text[text.index("    ARCHF=$(mktemp)"):text.index("    # The newest COMPLETED base backup")]
        root = self._root()
        for row, want in (("1|Bearer PLANTED_ARCHIVER_CREDENTIAL|0", "FAIL  p-db: WAL archiving is stuck (archived=unreadable waiting=unreadable failed=unreadable)"),
                          ("5|0|1", "ok    p-db: WAL archiver is live (5 archived, 1 waiting, 0 lifetime failures)"),
                          ("", "archived=unreadable")):
            with self.subTest(row=row[:12]):
                _write(root / "bin" / "kubectl", "#!/bin/sh\nprintf '%s\\n' '" + row + "'\n", executable=True)
                proc = self._bash("ok(){ echo \"  ok    $1\"; }; bad(){ echo \"  FAIL  $1\"; }\np=p; PRIMARY=p-db-1\n" + block,
                                  env={"PATH": str(root / "bin") + os.pathsep + os.environ["PATH"]}, trace=True)
                self.assertIn(want, proc.stdout); self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_the_kubelet_config_never_becomes_a_shell_variable(self):
        """B4: configz (staticPodURLHeader can carry a bearer token) sat in CFG, which a
        traced shell prints twice over."""
        text = self._stage("stage2.sh")
        self.assertNotIn("CFG=$(kubectl get --raw", text)
        block = text[text.index("CFGF=$(mktemp)"):text.index('rm -f "$CFGF"') + len('rm -f "$CFGF"')]
        root = self._root()
        _write(root / "bin" / "kubectl", "#!/bin/sh\nprintf '%s' '{\"kubeletconfig\": {\"staticPodURLHeader\": {\"Authorization\": [\"Bearer PLANTED_KUBELET_CREDENTIAL_123\"]}, \"evictionHard\": {\"memory.available\": \"100Mi\"}, \"systemReserved\": {\"memory\": \"1Gi\"}}}'\n", executable=True)
        proc = self._bash("ok(){ echo \"  ok    $1\"; }; bad(){ echo \"  FAIL  $1\"; }\nORBIT_NODE_NAME=n1\n" + block,
                          env={"PATH": str(root / "bin") + os.pathsep + os.environ["PATH"]}, trace=True)
        self.assertIn("ok    kubelet has a memory eviction threshold", proc.stdout); self.assertIn("ok    system/kube reserved set", proc.stdout)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_a_probe_drift_row_repeats_only_hostname_shaped_hosts(self):
        """B5: a live probe instance label's authority was printed as parsed."""
        code = self._splice("stage5-platform.sh", "    probe_drift(){ python3 - \"$1\" \"$2\" <<'PY'\n", "\nPY\n")
        root = self._root()
        _write(root / "want.txt", "https://door.fixture.org/\n")
        _write(root / "have.txt", "https://door.fixture.org/\nhttps://Bearer PLANTED_PROBE_CREDENTIAL_123/\nhttps://x_y.fixture.org/\n")
        proc = subprocess.run([sys.executable, "-c", code, str(root / "want.txt"), str(root / "have.txt")], capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("1 target(s) declared, 3 live; declared but unprobed host(s): none; probed but undeclared host(s): ?(a host not of a hostname shape, or of a credential's shape)", proc.stdout)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr); self.assertNotIn("x_y", proc.stdout)


class Round12Test(unittest.TestCase):
    """Area-1 sweep round 12: each row pins one repair."""

    def _stage(self, name):
        return (ROOT / "scripts" / "verify" / name).read_text()

    def _splice(self, stage, start, end):
        text = self._stage(stage)
        i = text.index(start) + len(start)
        return text[i:text.index(end, i)]

    def _root(self):
        root = pathlib.Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        return root

    def _bash(self, script, cwd=None, env=None, trace=False):
        return subprocess.run(["bash"] + (["-x"] if trace else []) + ["-c", script], cwd=cwd, capture_output=True,
                              text=True, timeout=120, env={**os.environ, **(env or {})})

    def _helpers(self):
        """The shaped_refs / with_project_namespaces helpers, as the stage defines them."""
        text = self._stage("stage3.sh")
        return text[text.index("shaped_refs(){"):text.index("BADRES=$(kubectl get pods -A")]

    def _kubectl(self, root, body):
        _write(root / "bin" / "kubectl", "#!/bin/sh\nprintf '%s\\n' \"$@\" >> \"$KLOG\"\n" + body + "\n", executable=True)
        return {"PATH": str(root / "bin") + os.pathsep + os.environ["PATH"], "KLOG": str(root / "kubectl.log")}

    def _load(self, cfg, env_extra=None):
        root = self._root(); (root / "instance").mkdir()
        (root / "instance/config.yaml").write_text(yaml.safe_dump(cfg))
        # the exports are printed only after a load that passed: a refused override must
        # not be echoed back by the test's own probe line
        script = "source " + str(ROOT / "scripts" / "lib-instance.sh") + "; rc=$?; if [ $rc = 0 ]; then echo \"rc=0 [$ORBIT_NODE_IP] [$ORBIT_DNS_ZONES]\"; else echo \"rc=$rc\"; fi"
        env = {k: v for k, v in os.environ.items() if not k.startswith("ORBIT_")}
        env["ORBIT_INSTANCE_CONFIG"] = str(root / "instance/config.yaml"); env.update(env_extra or {})
        return subprocess.run(["bash", "-x", "-c", script], capture_output=True, text=True, timeout=60, env=env)

    CFG = {"instance": {"name": "fx"}, "domains": {"primary": "fixture.org", "dev": "dev.fixture.org", "dnsZones": ["fixture.org"]},
           "registry": "ghcr.io/fx", "git": {"repoUrl": "git@github.com:fx/fx.git", "github_org": "fx", "branch": "main"},
           "nodes": [{"name": "n1", "ip": "203.0.113.5", "role": "control-plane"}]}

    # ---- breaker -------------------------------------------------------------------
    def test_cluster_listings_reach_rows_and_argv_only_as_shaped_references(self):
        """B1: pod, Job and namespace listings sat in shell variables, were printed
        verbatim in rows, and a namespace name became a kubectl argument."""
        text = self._stage("stage3.sh")
        for old in ('if PRIOLIST=$(kubectl', 'if READYLIST=$(kubectl', 'if FAILEDJOBS=$(kubectl', "for ns in $(kubectl get ns"):
            self.assertNotIn(old, text)
        block = text[text.index("PRIOF=$(mktemp)"):text.index('rm -f "$JOBF"') + len('rm -f "$JOBF"')]
        root = self._root()
        env = self._kubectl(root, "case \"$*\" in *jobs*) printf 'ns-a/job-ok \\nns-a/job-bad True\\nBearer PLANTED_PLUGIN_CREDENTIAL True\\n';; *priorityClassName*) printf 'ns-a/p1=orbit-x\\nns-a/p2=\\n--token=PLANTED_NAMESPACE_CREDENTIAL/p=\\n';; *) printf 'ns-a/p1 Running True\\nns-a/p2 Running False\\nBearer PLANTED_READY_CREDENTIAL Running False\\n';; esac")
        proc = self._bash("ok(){ echo \"  ok    $1\"; }; bad(){ echo \"  FAIL  $1\"; }\n" + self._helpers() + block, env=env, trace=True)
        out = proc.stdout + proc.stderr
        self.assertNotIn("PLANTED", out)
        self.assertIn("FAIL  no PriorityClass on: ns-a/p2 (+1 not of a name shape, or of a credential's shape)", proc.stdout)
        self.assertIn("FAIL  not Ready: ns-a/p2 (+1 not of a name shape, or of a credential's shape)", proc.stdout)
        self.assertIn("FAIL  failed Jobs: ns-a/job-bad (+1 not of a name shape, or of a credential's shape)", proc.stdout)
        # the namespace loops: an unshaped name is never an argument
        loop = "ok(){ echo \"  ok    $1\"; }; bad(){ echo \"  FAIL  $1\"; }\nQMISS=\"\"\nif with_project_namespaces x; then for ns in $PNS; do kubectl -n \"$ns\" get resourcequota orbit-quota >/dev/null 2>&1 || QMISS=\"$QMISS $ns\"; done; fi\necho \"QMISS=$QMISS\"\n"
        env = self._kubectl(root, "case \"$*\" in *'get ns'*) printf 'ns-a --token=PLANTED_NAMESPACE_CREDENTIAL ns-b';; *) exit 1;; esac")
        (root / "kubectl.log").write_text("")
        proc = self._bash(self._helpers() + loop, env=env, trace=True)
        self.assertIn("QMISS= ns-a ns-b", proc.stdout); self.assertNotIn("PLANTED", proc.stdout + proc.stderr + (root / "kubectl.log").read_text())

    def test_a_scoped_or_prefixed_address_is_not_a_node_ip(self):
        """B2: `fe80::1%token=...` passed ipaddress.ip_address and rode into ssh/curl argv."""
        for ip in ("fe80::1%token=PLANTED_SCOPE_CREDENTIAL", "203.0.113.5/24", "203.0.113.5%PLANTED"):
            with self.subTest(ip=ip[:12]):
                cfg = copy.deepcopy(self.CFG); cfg["nodes"][0]["ip"] = ip
                proc = self._load(cfg)
                self.assertIn("rc=2", proc.stdout); self.assertIn("nodes[0].ip is not a plain IP address", proc.stderr)
                self.assertNotIn("PLANTED", proc.stdout + proc.stderr)
        proc = self._load(self.CFG); self.assertIn("rc=0 [203.0.113.5]", proc.stdout)

    def test_the_dns_zone_override_meets_the_same_shape(self):
        """B3: ORBIT_DNS_ZONES from the environment was exported unvalidated."""
        proc = self._load(self.CFG, {"ORBIT_DNS_ZONES": "Bearer PLANTED_DNS_CREDENTIAL"})
        self.assertIn("rc=2", proc.stdout); self.assertIn("ORBIT_DNS_ZONES is not a space-separated list of DNS names", proc.stderr)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)
        proc = self._load(self.CFG, {"ORBIT_DNS_ZONES": " Example.ORG.  other.org "})
        self.assertIn("rc=0 [203.0.113.5] [example.org other.org]", proc.stdout)

    def test_a_managed_pod_the_reader_did_not_model_is_a_query_error(self):
        """B8: a pod document without metadata raised out of the walk; the empty output
        read as 'no offender' and the row went green."""
        text = self._stage("stage3.sh")
        start = text.index("BADRES=$(kubectl get pods -A -l app.kubernetes.io/managed-by=argocd -o json 2>/dev/null \\\n  | python3 -c '") + len("BADRES=$(kubectl get pods -A -l app.kubernetes.io/managed-by=argocd -o json 2>/dev/null \\\n  | python3 -c '")
        code = text[start:text.index("\n'); BADRC=$?", start)]
        def run(body):
            return subprocess.run([sys.executable, "-c", code], input=body, capture_output=True, text=True, timeout=60)
        self.assertEqual(run('{"items":[{"spec":{"containers":[]}}]}').stdout.strip(), "QUERYERR")
        self.assertEqual(run('{"items":[{"metadata":{"namespace":"a","name":"p"},"spec":{"containers":[{"name":"c","resources":{"requests":{"memory":"1Mi"},"limits":{"memory":"2Mi"}}}]}}]}').stdout.strip(), "")
        proc = run('{"items":[{"metadata":{"namespace":"Bearer PLANTED_NS","name":"p"},"spec":{"containers":[{"name":"c","resources":{"limits":{"cpu":"1"}}}]}}]}')
        self.assertIn("(unshaped)/p/c(cpu-limit)", proc.stdout); self.assertNotIn("PLANTED", proc.stdout)
        self.assertIn('if [ "$BADRES" = "QUERYERR" ] || [ "$BADRC" -ne 0 ]; then', text)

    # ---- auditor -------------------------------------------------------------------
    def test_the_intake_gate_is_read_as_one_of_three_states(self):
        """A1: intakeIngress reached a shell variable unvalidated."""
        text = self._stage("stage3.sh")
        start = text.index('  INTAKEWIRED=$(python3 -c "\n') + len('  INTAKEWIRED=$(python3 -c "\n')
        code = text[start:text.index('" 2>/dev/null)', start)]
        root = self._root(); (root / "instance").mkdir()
        for value, want in (("wired", "wired"), (None, ""), ("Bearer PLANTED_CREDENTIAL", "invalid"), (["wired"], "invalid")):
            with self.subTest(value=str(value)[:10]):
                (root / "instance/config.yaml").write_text(yaml.safe_dump({"intakeIngress": value} if value is not None else {}))
                proc = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True, timeout=60)
                self.assertEqual(proc.stdout.strip(), want); self.assertNotIn("PLANTED", proc.stdout + proc.stderr)
        self.assertIn('if [ "$INTAKEWIRED" = invalid ]; then', text)

    def test_the_invalid_uid_row_uses_the_shaped_key(self):
        """A2: the invalid-uid diagnostic formatted the raw key with %r."""
        code = self._splice("stage5-platform.sh", "DSHAPE=$(python3 - \"${DASHLIST[@]}\" <<'PY'\n", "\nPY\n")
        root = self._root()
        _write(root / "d.yaml", "kind: ConfigMap\nmetadata: {name: dashboard-x}\ndata:\n  'Bearer PLANTED_CREDENTIAL': '{\"uid\": \"bad/uid\"}'\n")
        proc = subprocess.run([sys.executable, "-c", code, str(root / "d.yaml")], capture_output=True, text=True, timeout=60)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("key #0 (a key not of a key shape, or of a credential's shape) has a uid that is not a Grafana uid", proc.stdout)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_the_rule_scan_reads_both_yaml_suffixes(self):
        """A4: promtool's real-rules scan read *.yaml only; the renderer reads .yml too."""
        code = self._splice("stage3.sh", "REALRULES=$(python3 - clusters \"$A/real-rules\" <<'PY'\n", "\nPY\n")
        root = self._root(); (root / "out").mkdir()
        rule = {"apiVersion": "monitoring.coreos.com/v1", "kind": "PrometheusRule", "metadata": {"name": "extra", "namespace": "monitoring"},
                "spec": {"groups": [{"name": "g", "rules": [{"alert": "ExtraBroken", "expr": "up == 0"}]}]}}
        _write(root / "clusters" / "platform" / "monitoring" / "extra.yml", yaml.safe_dump(rule))
        proc = subprocess.run([sys.executable, "-c", code, "clusters", str(root / "out")], cwd=root, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.split("\n")[0].strip(), "1", proc.stdout)

    def test_a_job_that_retried_and_completed_is_not_a_failed_job(self):
        """A5: `status.failed` counts attempts; a Job with failed=1 and Complete=True was
        reported as failed. The terminal Failed=True condition decides."""
        text = self._stage("stage3.sh")
        self.assertIn('{.status.conditions[?(@.type=="Failed")].status}', text[text.index("JOBF=$(mktemp)"):])
        block = text[text.index("JOBF=$(mktemp)"):text.index('rm -f "$JOBF"') + len('rm -f "$JOBF"')]
        root = self._root()
        env = self._kubectl(root, "printf 'ns-a/retried-then-ok \\nns-a/really-failed True\\nns-a/active False\\n'")
        proc = self._bash("ok(){ echo \"  ok    $1\"; }; bad(){ echo \"  FAIL  $1\"; }\n" + self._helpers() + block, env=env)
        self.assertIn("FAIL  failed Jobs: ns-a/really-failed ", proc.stdout); self.assertNotIn("retried-then-ok", proc.stdout)
        env = self._kubectl(root, "printf 'ns-a/retried-then-ok \\n'")
        proc = self._bash("ok(){ echo \"  ok    $1\"; }; bad(){ echo \"  FAIL  $1\"; }\n" + self._helpers() + block, env=env)
        self.assertIn("ok    no failed Jobs", proc.stdout)

    def test_a_namespace_less_service_lives_where_its_application_puts_it(self):
        """A6: a Service with no metadata.namespace was recorded as (name, None) and
        matched in EVERY namespace lookup, so a URL to relay.monitoring resolved against
        a Service the renderer applies into cert-manager."""
        text = self._stage("stage5-platform.sh")
        self.assertNotIn("for key in ((name, ns), (name, None)):", text)
        start = text.index("SVCPORT=$(python3 - . <<'PY'\n") + len("SVCPORT=$(python3 - . <<'PY'\n")
        code = text[start:text.index("\nPY\n", start)]
        root = self._root(); (root / "catalog").mkdir()
        app = ('apiVersion: argoproj.io/v1alpha1\nkind: Application\nmetadata: {name: platform-config, namespace: argocd}\n'
               'spec:\n  destination: {namespace: cert-manager}\n  source: {path: clusters/platform/config, directory: {recurse: true}}\n')
        _write(root / "clusters" / "platform" / "apps" / "platform-config.yaml", app)
        _write(root / "clusters" / "platform" / "config" / "relay.yaml", yaml.safe_dump({"apiVersion": "v1", "kind": "Service", "metadata": {"name": "relay"}, "spec": {"ports": [{"port": 9093}]}}))
        _write(root / "clusters" / "loose" / "orphan.yaml", yaml.safe_dump({"apiVersion": "v1", "kind": "Service", "metadata": {"name": "orphan"}, "spec": {"ports": [{"port": 80}]}}))
        def run(url):
            _write(root / "clusters" / "cm.yaml", yaml.safe_dump({"kind": "ConfigMap", "metadata": {"name": "c", "namespace": "monitoring"}, "data": {"u": url}}))
            return subprocess.run([sys.executable, "-c", code, str(root)], cwd=root, capture_output=True, text=True, timeout=120)
        proc = run("http://relay.cert-manager.svc.cluster.local:9093/")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        for url in ("http://relay.monitoring.svc.cluster.local:9093/", "http://orphan.monitoring.svc/x"):
            with self.subTest(url=url):
                proc = run(url); self.assertNotEqual(proc.returncode, 0, proc.stdout)


class Round13Test(unittest.TestCase):
    """Area-1 sweep round 13: each row pins one repair."""

    def _stage(self, name):
        return (ROOT / "scripts" / "verify" / name).read_text()

    def _splice(self, stage, start, end):
        text = self._stage(stage)
        i = text.index(start) + len(start)
        return text[i:text.index(end, i)]

    def _root(self):
        root = pathlib.Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        return root

    def _bash(self, script, cwd=None, env=None, trace=False):
        return subprocess.run(["bash"] + (["-x"] if trace else []) + ["-c", script], cwd=cwd, capture_output=True,
                              text=True, timeout=120, env={**os.environ, **(env or {})})

    def _helpers(self):
        text = self._stage("stage3.sh")
        return text[text.index("shaped_refs(){"):text.index("BADRES=$(kubectl get pods -A")]

    def _kubectl(self, root, body):
        _write(root / "bin" / "kubectl", "#!/bin/sh\nprintf '%s\\n' \"$@\" >> \"$KLOG\"\n" + body + "\n", executable=True)
        return {"PATH": str(root / "bin") + os.pathsep + os.environ["PATH"], "KLOG": str(root / "kubectl.log")}

    OKBAD = "ok(){ echo \"  ok    $1\"; }; bad(){ echo \"  FAIL  $1\"; }\n"

    # ---- auditor -------------------------------------------------------------------
    def test_the_over_reservation_row_carries_only_shaped_labels(self):
        """A1: namespace and container labels were interpolated unvalidated."""
        code = self._splice("stage5-platform.sh", "         python3 - \"$TMP/overreserved.json\" \"$TMP/overreserved.txt\" <<'PY'\n", "\nPY\n")
        root = self._root()
        def run(body):
            _write(root / "in.json", body)
            subprocess.run([sys.executable, "-c", code, str(root / "in.json"), str(root / "out.txt")], capture_output=True, text=True, timeout=60, check=True)
            return (root / "out.txt").read_text()
        self.assertEqual(run('{"status":"success","data":{"result":[{"metric":{"namespace":"monitoring","container":"Bearer PLANTED_CREDENTIAL"},"value":[1,"4"]}]}}'), "monitoring/(unshaped)=4.0x")
        self.assertEqual(run('{"status":"success","data":{"result":[{"metric":{"namespace":"monitoring","container":"c"},"value":[1,"abc"]}]}}'), "QUERYERR")
        self.assertEqual(run('{"status":"success","data":{"result":[{"metric":{"namespace":"monitoring","container":"c"},"value":[1,"4"]}]}}'), "monitoring/c=4.0x")

    def test_the_credential_scanner_row_counts_and_the_scanner_shapes_keys(self):
        """A2: the scanner's first diagnostic (which names a key) was copied into the row;
        the scanner itself printed a key pasted with a credential."""
        text = self._stage("stage3.sh")
        self.assertNotIn('bad "credential found in generated manifests ($(python3 scripts/no-credentials.py 2>&1 | head -1))"', text)
        self.assertIn("grep -c '^credential in a generated manifest'", text)
        root = self._root(); (root / "scripts").mkdir(); (root / "clusters" / "apps").mkdir(parents=True)
        shutil.copy(ROOT / "scripts" / "no-credentials.py", root / "scripts" / "no-credentials.py")
        _write(root / "clusters" / "apps" / "bait.yaml", "kind: ConfigMap\ndata: {password_ghp_PLANTEDCREDENTIALKEY: x, password: y}\n")
        proc = subprocess.run([sys.executable, "scripts/no-credentials.py"], cwd=root, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("credential in a generated manifest: bait.yaml.data.#0(a key not of a key shape, or of a credential's shape): a literal value under a credential-shaped name", proc.stdout)
        self.assertIn("bait.yaml.data.password: a literal value", proc.stdout)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_a_pod_name_from_the_cluster_becomes_an_argument_only_when_shaped(self):
        """A3: the traefik pod's and the CNPG primary's names went straight to argv."""
        text = self._stage("stage5-platform.sh")
        self.assertNotIn("CTL_POD=$(kubectl", text)
        block = text[text.index("  CTLF=$(mktemp)"):text.index('  CTL_STATE=UNANSWERABLE')]
        root = self._root()
        for name, want in (("traefik-abc12", "traefik-abc12"), ("Bearer PLANTED_POD_CREDENTIAL", ""), ("", "")):
            with self.subTest(name=name[:10]):
                env = self._kubectl(root, "printf '%s' '" + name + "'")
                proc = self._bash("TMP=$(mktemp -d); CTL_NS=traefik\n" + block + "\necho \"CTL_POD=[$CTL_POD]\"\n", env=env, trace=True)
                self.assertIn("CTL_POD=[%s]" % want, proc.stdout); self.assertNotIn("PLANTED", proc.stdout + proc.stderr)
        s3 = self._stage("stage3.sh")
        self.assertIn("PRIMARY=$(shaped_refs \"$PRIMF\" name | grep -v '^(+'); rm -f \"$PRIMF\"", s3)
        _write(root / "n", "Bearer PLANTED_PRIMARY\n")
        out = self._bash(self._helpers() + "shaped_refs " + str(root / "n") + " name | grep -v '^(+'; echo END").stdout
        self.assertEqual(out.strip(), "END")

    def test_a_failed_namespace_listing_is_red_not_an_empty_population(self):
        """A4: a failed `kubectl get ns` left the quota and isolation loops with zero
        iterations and a green 'every project namespace carries its ResourceQuota'."""
        text = self._stage("stage3.sh")
        self.assertNotIn("project_ns_list", text)
        root = self._root()
        script = self.OKBAD + self._helpers() + "if with_project_namespaces \"the quota row\"; then echo \"PNS=[$PNS]\"; else echo SKIPPED; fi\n"
        proc = self._bash(script, env=self._kubectl(root, "exit 1"))
        self.assertIn("FAIL  the project namespaces could not be listed (kubectl rc=1) — the quota row UNCHECKED", proc.stdout); self.assertIn("SKIPPED", proc.stdout)
        proc = self._bash(script, env=self._kubectl(root, "printf 'ns-a Bearer_PLANTED ns-b'"))
        self.assertIn("PNS=[ns-a\nns-b]", proc.stdout); self.assertIn("FAIL  project namespace(s) (+1 not of a name shape, or of a credential's shape) are skipped", proc.stdout)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)
        quota = text[text.index('  if with_project_namespaces "the per-project quota row"; then'):text.index("namespaces missing orbit-quota:$QMISS\"") + len("namespaces missing orbit-quota:$QMISS\"")] + "\n    fi\n  fi\n"
        proc = self._bash(self.OKBAD + self._helpers() + "PROJCOUNT=2\n" + quota, env=self._kubectl(root, "printf ''"))
        self.assertIn("FAIL  2 project(s) declared but zero project namespaces listed", proc.stdout)

    def test_a_consumer_with_no_namespace_lives_where_its_application_puts_it(self):
        """A5: the Service index used the directory Application's destination, the
        consumer scan did not — a valid http://relay:9093 was 'a bare Service name'."""
        text = self._stage("stage5-platform.sh")
        start = text.index("SVCPORT=$(python3 - . <<'PY'\n") + len("SVCPORT=$(python3 - . <<'PY'\n")
        code = text[start:text.index("\nPY\n", start)]
        root = self._root(); (root / "catalog").mkdir()
        _write(root / "clusters" / "platform" / "apps" / "platform-config.yaml",
               'apiVersion: argoproj.io/v1alpha1\nkind: Application\nmetadata: {name: platform-config, namespace: argocd}\n'
               'spec:\n  destination: {namespace: cert-manager}\n  source: {path: clusters/platform/config, directory: {recurse: true}}\n')
        _write(root / "clusters" / "platform" / "config" / "relay.yaml", yaml.safe_dump({"apiVersion": "v1", "kind": "Service", "metadata": {"name": "relay"}, "spec": {"ports": [{"port": 9093}]}}))
        _write(root / "clusters" / "platform" / "config" / "consumer.yaml", yaml.safe_dump({"kind": "ConfigMap", "metadata": {"name": "c"}, "data": {"u": "http://relay:9093/"}}))
        proc = subprocess.run([sys.executable, "-c", code, str(root)], cwd=root, capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr); self.assertIn("OK 1 in-cluster endpoint(s)", proc.stdout)
        _write(root / "clusters" / "platform" / "config" / "consumer.yaml", yaml.safe_dump({"kind": "ConfigMap", "metadata": {"name": "c"}, "data": {"u": "http://relay:9094/"}}))
        proc = subprocess.run([sys.executable, "-c", code, str(root)], cwd=root, capture_output=True, text=True, timeout=120)
        self.assertNotEqual(proc.returncode, 0)

    # ---- breaker -------------------------------------------------------------------
    def test_a_workload_is_isolated_only_by_a_selector_that_selects_it(self):
        """B1: matchLabels alone counted; a matchExpressions term contradicting the label
        selects no pod, and the three workloads were 'default-deny' by nothing."""
        code = self._splice("stage5-platform.sh", "WOUT=$(python3 - \"$NETPOL\" <<'PY'\n", "\nPY\n")
        root = self._root()
        def pol(name, expr=None):
            sel = {"matchLabels": {"app.kubernetes.io/name": name}}
            if expr: sel["matchExpressions"] = [expr]
            return {"kind": "NetworkPolicy", "metadata": {"name": name, "namespace": "monitoring"}, "spec": {"podSelector": sel, "ingress": [], "policyTypes": ["Ingress"]}}
        def run(expr):
            _write(root / "n.yaml", yaml.safe_dump_all([pol(n, expr) for n in ("prometheus", "grafana", "alertmanager")]))
            return subprocess.run([sys.executable, "-c", code, str(root / "n.yaml")], capture_output=True, text=True, timeout=60)
        self.assertEqual(run(None).returncode, 0, run(None).stdout)
        self.assertEqual(run({"key": "app.kubernetes.io/name", "operator": "Exists"}).returncode, 0)
        for expr in ({"key": "app.kubernetes.io/name", "operator": "DoesNotExist"},
                     {"key": "app.kubernetes.io/name", "operator": "NotIn", "values": ["prometheus", "grafana", "alertmanager"]},
                     {"key": "app.kubernetes.io/name", "operator": "In", "values": ["other"]}):
            with self.subTest(op=expr["operator"]):
                proc = run(expr); self.assertNotEqual(proc.returncode, 0); self.assertIn("no policy selects", proc.stdout)

    def test_remote_output_never_becomes_a_shell_variable(self):
        """B2: the probe pod's stdout and the certificate issuer sat in shell variables."""
        s5 = self._stage("stage5-platform.sh"); s2 = self._stage("stage2.sh")
        self.assertNotIn("OPOUT=$(kubectl run", s5); self.assertNotIn('echo "$OPOUT"', s5); self.assertNotIn("LEAF=$(echo | timeout", s2)
        block = next(l for l in s5.splitlines() if l.startswith("marker(){")) + "\n" + s5[s5.index('  OPF="$TMP/obsplane.out"'):s5.index('    case "$OPPROM" in')]
        root = self._root()
        env = self._kubectl(root, "printf 'Bearer PLANTED_PROBE_CREDENTIAL_123\\nPROM=000\\nAM=000\\nCONTROL=403\\nMARK=obsplane-complete\\n'")
        proc = self._bash(self.OKBAD + "TMP=$(mktemp -d); OPPOD=p; OPSCRIPT=x; LOKI_ON=0; TEMPO_ON=0\n" + block + "\n  fi\n", env=env, trace=True)
        self.assertIn("apiserver Service answered HTTP 403", proc.stdout); self.assertNotIn("PLANTED", proc.stdout + proc.stderr)
        block2 = s2[s2.index("LEAFF=$(mktemp)"):s2.index('rm -f "$LEAFF"') + len('rm -f "$LEAFF"')]
        _write(root / "bin" / "openssl", "#!/bin/sh\ncase \"$1\" in s_client) cat >/dev/null; echo cert;; x509) echo \"issuer=CN=Bearer PLANTED_CERT_CREDENTIAL_123\";; esac\n", executable=True)
        proc = self._bash(self.OKBAD + "IP=203.0.113.5; ORBIT_DOMAIN=fixture.org\n" + block2, env={"PATH": str(root / "bin") + os.pathsep + os.environ["PATH"]}, trace=True)
        self.assertIn("FAIL  unrouted host served a certificate whose issuer is not Let's Encrypt", proc.stdout); self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_a_down_target_host_is_shown_only_when_hostname_shaped(self):
        """B3: a down target's scrapeUrl authority was printed as parsed."""
        code = self._splice("stage5-platform.sh", "python3 - \"$TMP/targets.json\" \"$TMP/down.txt\" \"$TMP/count.txt\" <<'PY' 2>/dev/null\n", "\nPY\n")
        root = self._root()
        _write(root / "targets.json", json.dumps({"status": "success", "data": {"activeTargets": [
            {"health": "down", "scrapeUrl": "https://Bearer PLANTED_SCRAPE_CREDENTIAL_123/metrics", "labels": {"job": "j1"}, "lastError": "x"},
            {"health": "down", "scrapeUrl": "http://10.0.0.6:9100/metrics", "labels": {"job": "j2"}, "lastError": "x"},
            {"health": "down", "scrapeUrl": "http://node.fixture.org:9100/metrics", "labels": {"job": "j3"}, "lastError": "x"}]}}))
        proc = subprocess.run([sys.executable, "-c", code, str(root / "targets.json"), str(root / "down.txt"), str(root / "count.txt")], capture_output=True, text=True, timeout=60)
        self.assertEqual((root / "down.txt").read_text().splitlines(), ["j1 ?(a host not of a hostname shape, or of a credential's shape)", "j2 10.0.0.6:9100", "j3 node.fixture.org:9100"])
        self.assertNotIn("PLANTED", (root / "down.txt").read_text() + proc.stdout + proc.stderr)

    def test_an_unreadable_manifest_is_reported_by_class_only(self):
        """B5: two static readers let the parser's message (which quotes the input) reach stderr."""
        root = self._root()
        for start, args in (("SC_OUT=$(python3 - \"$SCOLL\" \"$FEATURE_LOGS\" <<'PY'\n", ["0"]), ("GS_OUT=$(python3 - \"$GSEC\" <<'PY'\n", [])):
            with self.subTest(block=start[:6]):
                code = self._splice("stage5-platform.sh", start, "\nPY\n")
                _write(root / "f.yaml", "kind: Namespace\nmetadata: *PLANTED_YAML_CREDENTIAL_123\n")
                proc = subprocess.run([sys.executable, "-c", code, str(root / "f.yaml")] + args, capture_output=True, text=True, timeout=60)
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("cannot be read as YAML (ComposerError)", proc.stdout); self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_the_positive_control_must_be_an_http_status(self):
        """B7: CONTROL=not-http (→ `unreadable`) passed the control and the two 000s were
        certified as policy denials."""
        s5 = self._stage("stage5-platform.sh")
        block = next(l for l in s5.splitlines() if l.startswith("marker(){")) + "\n" + s5[s5.index('  OPF="$TMP/obsplane.out"'):s5.index('    case "$OPPROM" in')]
        root = self._root()
        for ctl, want in (("not-http", "FAIL  the off-path probe pod could not reach even the apiserver Service"), ("", "missing marker(s) CONTROL"), ("403", "ok    the off-path probe pod reached")):
            with self.subTest(ctl=ctl):
                env = self._kubectl(root, "printf 'PROM=000\\nAM=000\\n" + ("CONTROL=" + ctl + "\\n" if ctl else "") + "MARK=obsplane-complete\\n'")
                proc = self._bash(self.OKBAD + "TMP=$(mktemp -d); OPPOD=p; OPSCRIPT=x; LOKI_ON=0; TEMPO_ON=0\n" + block + "\n  fi\n", env=env)
                self.assertIn(want, proc.stdout)


class Round14Test(unittest.TestCase):
    """Area-1 sweep round 14: each row pins one repair."""

    def _stage(self, name):
        return (ROOT / "scripts" / "verify" / name).read_text()

    def _splice(self, stage, start, end):
        text = self._stage(stage)
        i = text.index(start) + len(start)
        return text[i:text.index(end, i)]

    def _root(self):
        root = pathlib.Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        return root

    def _bash(self, script, cwd=None, env=None, trace=False):
        return subprocess.run(["bash"] + (["-x"] if trace else []) + ["-c", script], cwd=cwd, capture_output=True,
                              text=True, timeout=120, env={**os.environ, **(env or {})})

    def _py(self, code, *args, cwd=None):
        return subprocess.run([sys.executable, "-c", code] + [str(a) for a in args], cwd=cwd, capture_output=True, text=True, timeout=60)

    def _helpers(self):
        text = self._stage("stage3.sh")
        return text[text.index("shaped_refs(){"):text.index("BADRES=$(kubectl get pods -A")]

    def _stub(self, root, name, body):
        _write(root / "bin" / name, "#!/bin/sh\nprintf '%s\\n' \"$@\" >> \"$KLOG\"\n" + body + "\n", executable=True)
        return {"PATH": str(root / "bin") + os.pathsep + os.environ["PATH"], "KLOG": str(root / "stub.log")}

    OKBAD = "ok(){ echo \"  ok    $1\"; }; bad(){ echo \"  FAIL  $1\"; }\n"

    def _load(self, cfg, env_extra=None):
        root = self._root(); (root / "instance").mkdir()
        (root / "instance/config.yaml").write_text(yaml.safe_dump(cfg))
        script = "source " + str(ROOT / "scripts" / "lib-instance.sh") + "; rc=$?; if [ $rc = 0 ]; then echo \"rc=0 [$ORBIT_NODE_IP] [$ORBIT_CONTROL_PLANE_IP]\"; else echo \"rc=$rc\"; fi"
        env = {k: v for k, v in os.environ.items() if not k.startswith("ORBIT_")}
        env["ORBIT_INSTANCE_CONFIG"] = str(root / "instance/config.yaml"); env.update(env_extra or {})
        return subprocess.run(["bash", "-x", "-c", script], capture_output=True, text=True, timeout=60, env=env)

    CFG = {"instance": {"name": "fx"}, "domains": {"primary": "fixture.org", "dev": "dev.fixture.org", "dnsZones": ["fixture.org"]},
           "registry": "ghcr.io/fx", "git": {"repoUrl": "git@github.com:fx/fx.git", "github_org": "fx", "branch": "main"},
           "nodes": [{"name": "n1", "ip": "203.0.113.5", "role": "control-plane"}]}

    # ---- breaker -------------------------------------------------------------------
    def test_a_node_override_is_shape_checked_even_with_no_nodes_in_the_file(self):
        """B1: with `nodes: []` the override branch was skipped, so ORBIT_NODE_IP and
        ORBIT_CONTROL_PLANE_IP passed through unchecked."""
        cfg = copy.deepcopy(self.CFG); cfg["nodes"] = []
        for var in ("ORBIT_NODE_IP", "ORBIT_CONTROL_PLANE_IP"):
            with self.subTest(var=var):
                proc = self._load(cfg, {var: "203.0.113.5;PLANTED_OVERRIDE_CREDENTIAL"})
                self.assertNotIn("rc=0", proc.stdout); self.assertIn("rc=", proc.stdout)
                self.assertNotIn("PLANTED", proc.stdout + proc.stderr)
        proc = self._load(cfg, {"ORBIT_NODE_IP": "203.0.113.9", "ORBIT_CONTROL_PLANE_IP": "203.0.113.9"})
        self.assertIn("rc=0 [203.0.113.9] [203.0.113.9]", proc.stdout)

    def test_the_san_list_is_a_private_file_not_a_traced_variable(self):
        """B2: the certificate's SAN list sat in a shell variable, which `bash -x` prints."""
        block = self._splice("stage2.sh", "# No Secret is read anywhere in this section.\n", "rm -f \"$SANF\"\n")
        root = self._root()
        env = self._stub(root, "openssl", "case \"$1\" in s_client) cat >/dev/null; echo cert;; x509) echo 'X509v3 Subject Alternative Name:'; echo '    DNS:fixture.org, DNS:*.fixture.org, DNS:PLANTED-SAN-CREDENTIAL.example';; esac")
        proc = self._bash(self.OKBAD + "IP=203.0.113.5; ORBIT_DOMAIN=fixture.org; ORBIT_DEV_DOMAIN=dev.fixture.org\n" + block + "rm -f \"$SANF\"\n", env=env, trace=True)
        self.assertIn("ok    SAN fixture.org present", proc.stdout); self.assertIn("ok    SAN *.fixture.org present", proc.stdout)
        self.assertIn("FAIL  SAN *.dev.fixture.org MISSING", proc.stdout)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_replica_failures_reach_the_row_only_as_shaped_references(self):
        """B3: the ReplicaFailure listing was a shell variable printed verbatim."""
        block = self._splice("stage3.sh", "# failure lives only on the ReplicaSet.\n", "rm -f \"$RFF\"\n")
        root = self._root()
        env = self._stub(root, "kubectl", "printf 'team-a/api-5d9\\nBearer PLANTED_RS_CREDENTIAL\\n'")
        proc = self._bash(self.OKBAD + self._helpers() + block, env=env, trace=True)
        self.assertIn("FAIL  ReplicaFailure on: team-a/api-5d9 (+1 not of a name shape, or of a credential's shape)", proc.stdout)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_the_pod_security_label_read_is_silent_on_error(self):
        """B4: a failed label read put kubectl's message, which names the namespace, on stderr."""
        text = self._stage("stage3.sh")
        self.assertIn("jsonpath='{.metadata.labels.pod-security\\.kubernetes\\.io/enforce}' 2>/dev/null)\" = \"restricted\" ]", text)

    def test_the_metadata_probe_reads_only_fixed_tokens_and_a_failed_ssh_is_its_own_category(self):
        """B5: the node's stdout was a shell variable printed in the row, and an ssh that
        failed with output on stdout was read as an answer."""
        block = self._splice("stage3.sh", "# failed ssh is its own category, never its output\n", "# Assert the rules are in the RAW table.")
        root = self._root()
        cases = (("echo 'md=blocked net=200'; exit 0", "ok    metadata unreachable from a pod netns"),
                 ("echo 'md=blocked net=200 PLANTED_NODE_CREDENTIAL'; exit 0", "FAIL  could not run the metadata probe on the node (unreadable"),
                 ("echo 'md=blocked net=200'; exit 255", "FAIL  could not run the metadata probe on the node (ssh-failed(rc=255)"),
                 ("echo 'md=200 net=200'; exit 0", "FAIL  metadata service ANSWERED from inside a pod netns: md=200 net=200"),
                 ("echo no-netns; exit 0", "FAIL  could not run the metadata probe on the node (no-netns)"))
        for body, want in cases:
            with self.subTest(body=body[:24]):
                env = self._stub(root, "ssh", body)
                proc = self._bash(self.OKBAD + "SSHN=(ssh root@203.0.113.5)\n" + block, env=env, trace=True)
                self.assertIn(want, proc.stdout); self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_the_timer_fields_reach_the_row_only_in_shape(self):
        """B5: the timer's next-elapse and the run count were shell variables printed verbatim."""
        block = self._splice("stage3.sh", "TIMF=$(mktemp)\n", "rm -f \"$TIMF\"\n")
        root = self._root()
        env = self._stub(root, "ssh", "case \"$*\" in *NextElapse*) echo 'Sun 2026-09-13 12:00:00 UTC PLANTED_TIMER_CREDENTIAL';; *) echo 'PLANTED 12';; esac")
        proc = self._bash("TIMF=$(mktemp)\nSSHN=(ssh root@203.0.113.5)\n" + block + "echo \"NEXTEL=[$NEXTEL] RUNS=[$RUNS]\"\n", env=env, trace=True)
        self.assertIn("NEXTEL=[unreadable] RUNS=[0]", proc.stdout); self.assertNotIn("PLANTED", proc.stdout + proc.stderr)
        env = self._stub(root, "ssh", "case \"$*\" in *NextElapse*) echo 'Sun 2026-09-13 12:00:00 UTC';; *) echo 12;; esac")
        proc = self._bash("TIMF=$(mktemp)\nSSHN=(ssh root@203.0.113.5)\n" + block + "echo \"NEXTEL=[$NEXTEL] RUNS=[$RUNS]\"\n", env=env)
        self.assertIn("NEXTEL=[Sun 2026-09-13 12:00:00 UTC] RUNS=[12]", proc.stdout)

    def test_the_off_path_probe_output_is_a_private_file(self):
        """B7: the probe pod's output sat in a shell variable and `bash -x` printed it."""
        s5 = self._stage("stage5-platform.sh")
        block = next(l for l in s5.splitlines() if l.startswith("marker(){")) + "\n" + s5[s5.index('  OPF="$TMP/obsplane.out"'):s5.index('    case "$OPPROM" in')]
        root = self._root()
        env = self._stub(root, "kubectl", "printf 'PROM=000\\nAM=000\\nCONTROL=403\\nPLANTED_PROBE_CREDENTIAL\\nMARK=obsplane-complete\\n'")
        proc = self._bash(self.OKBAD + "TMP=$(mktemp -d); OPPOD=p; OPSCRIPT=x; LOKI_ON=0; TEMPO_ON=0\n" + block + "\n  fi\n", env=env, trace=True)
        self.assertIn("ok    the off-path probe pod reached", proc.stdout); self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_an_alert_list_that_is_not_a_list_is_unreadable_not_quiet(self):
        """B8: `null` (or a map) from the alerts API read as 'nothing firing'."""
        s5 = self._stage("stage5-platform.sh")
        block = s5[s5.index('  FIRING=$(python3 -c "'):s5.index('  case "$FIRING" in')]
        root = self._root()
        for body, want in (("null", "UNREADABLE"), ('{"a":1}', "UNREADABLE"), ("[]", ""), ('[{"labels":{"alertname":"KubeDown"}}]', "KubeDown"),
                           ('[{"labels":{"alertname":"Bearer PLANTED_ALERT_CREDENTIAL"}}]', " (+1 alert(s) not of an alert-name shape, or of a credential's shape)")):
            with self.subTest(body=body):
                _write(root / "alerts.json", body)
                proc = self._bash("TMP=" + str(root) + "\n" + block + "echo \"FIRING=[$FIRING]\"\n")
                self.assertIn("FIRING=[" + want + "]", proc.stdout); self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    # ---- auditor -------------------------------------------------------------------
    def test_every_authenticated_grafana_request_is_pinned_to_the_control_plane(self):
        """A1: grafapi, the Loki finder and the traces query sent the admin password to
        whatever DNS (and the proxy) resolved grafana.<domain> to."""
        text = self._stage("stage5-platform.sh")
        for line in text.splitlines():
            if "grafauth |" in line or "grafauth;" in line:
                self.assertIn('"${GRAFPIN[@]}"', line, line)
        self.assertIn('GRAFPIN=(--noproxy \'*\' --resolve "grafana.$ORBIT_DOMAIN:443:$ORBIT_CONTROL_PLANE_IP")', text)
        # without a control-plane address the request is not sent
        block = text[text.index("GRAFPIN=()"):text.index("grafauth(){")]
        proc = self._bash("ORBIT_DOMAIN=fixture.org\n" + block + "grafpinned; echo rc=$?\n")
        self.assertIn("rc=1", proc.stdout); self.assertIn("not sent", proc.stderr)
        proc = self._bash("ORBIT_DOMAIN=fixture.org; ORBIT_CONTROL_PLANE_IP=203.0.113.5\n" + block + "grafpinned; echo rc=$?; echo \"${GRAFPIN[*]}\"\n")
        self.assertIn("rc=0", proc.stdout); self.assertIn("--resolve grafana.fixture.org:443:203.0.113.5", proc.stdout)

    def test_the_live_image_is_compared_in_a_file_never_in_the_shell(self):
        """A2: the live container image (cluster text) sat in a shell variable."""
        text = self._stage("stage3.sh")
        self.assertNotIn("LIVE=$(kubectl", text)
        helpers = self._helpers(); root = self._root()
        for body, want in (("ghcr.io/fx/agents:v1", "match"), ("ghcr.io/fx/agents:v2 PLANTED", "mismatch"), ("", "empty")):
            with self.subTest(body=body[:12]):
                _write(root / "live", body)
                proc = self._bash(helpers + "image_cmp " + str(root / "live") + " ghcr.io/fx/agents:v1\n", trace=True)
                self.assertEqual(proc.stdout.strip(), want); self.assertNotIn("PLANTED", proc.stdout + proc.stderr)
                self.assertFalse((root / "live").exists())

    def test_an_intake_policy_name_reaches_the_row_only_in_shape(self):
        """A3: the intake audit interpolated metadata.name unvalidated."""
        code = self._splice("stage5-platform.sh", "WIOUT=$(python3 - \"$TMP/intake-netpols.yaml\" <<'PY'\n", "\nPY\n")
        root = self._root()
        _write(root / "f.yaml", yaml.safe_dump({"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
                                                 "metadata": {"name": "Bearer PLANTED_NAME_CREDENTIAL", "namespace": "orbit-agents-prod"},
                                                 "spec": {"podSelector": {}, "policyTypes": ["Ingress"], "ingress": [{"ports": [{"port": 8080}]}]}}))
        proc = self._py(code, root / "f.yaml")
        self.assertNotEqual(proc.returncode, 0); self.assertIn("(document #0, a name not of an object-name shape, or of a credential's shape)", proc.stdout)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_a_namespace_less_policy_under_a_directory_application_is_gathered(self):
        """A4: the gatherer required an explicit metadata.namespace; the renderer applies
        the directory Application's destination to a namespace-less object."""
        gather = self._splice("stage5-platform.sh", "gather_netpols(){ python3 - clusters \"$2\" \"$1\" <<'PY'\n", "\nPY\n")
        root = self._root()
        _write(root / "clusters" / "platform" / "apps" / "cfg.yaml", yaml.safe_dump({"kind": "Application", "metadata": {"name": "cfg"}, "spec": {
            "source": {"path": "clusters/platform/config", "directory": {"recurse": True}}, "destination": {"namespace": "monitoring"}}}))
        _write(root / "clusters" / "platform" / "config" / "x" / "np.yaml", yaml.safe_dump_all([
            {"kind": "NetworkPolicy", "metadata": {"name": "bare"}, "spec": {"podSelector": {}}},
            {"kind": "NetworkPolicy", "metadata": {"name": "explicit", "namespace": "loki"}, "spec": {"podSelector": {}}}]))
        _write(root / "clusters" / "other" / "np.yaml", yaml.safe_dump({"kind": "NetworkPolicy", "metadata": {"name": "outside"}, "spec": {"podSelector": {}}}))
        proc = self._py(gather, "clusters", root / "g.yaml", "monitoring", cwd=root)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        docs = [d for d in yaml.safe_load_all((root / "g.yaml").read_text()) if isinstance(d, dict)]
        self.assertEqual([d["metadata"]["name"] for d in docs], ["bare"]); self.assertEqual(docs[0]["metadata"]["namespace"], "monitoring")

    def test_the_intake_audit_reads_every_policy_for_its_namespace(self):
        """A4: only orbit-intake/netpol.yaml was audited; a second file admitting every
        peer to the intake was unseen."""
        text = self._stage("stage5-platform.sh")
        self.assertIn('gather_netpols orbit-agents-prod "$TMP/intake-netpols.yaml"', text)
        self.assertNotIn('WIOUT=$(python3 - "$IDIR/netpol.yaml"', text)
        gather = self._splice("stage5-platform.sh", "gather_netpols(){ python3 - clusters \"$2\" \"$1\" <<'PY'\n", "\nPY\n")
        audit = self._splice("stage5-platform.sh", "WIOUT=$(python3 - \"$TMP/intake-netpols.yaml\" <<'PY'\n", "\nPY\n")
        root = self._root(); _dirapp(root, "orbit-agents-prod")
        src = ROOT / "template" / "provisioning" / "clusters" / "platform" / "config" / "orbit-intake" / "netpol.yaml"
        _write(root / "clusters" / "platform" / "config" / "orbit-intake" / "netpol.yaml", src.read_text().replace("{{", "").replace("}}", ""))
        _write(root / "clusters" / "elsewhere" / "wide.yaml", yaml.safe_dump({"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
            "metadata": {"name": "wide-open", "namespace": "orbit-agents-prod"}, "spec": {"podSelector": {}, "policyTypes": ["Ingress"], "ingress": [{"ports": [{"port": 8080}]}]}}))
        proc = self._py(gather, "clusters", root / "g.yaml", "orbit-agents-prod", cwd=root)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self._py(audit, root / "g.yaml")
        self.assertNotEqual(proc.returncode, 0); self.assertIn("wide-open ingress[0] admits every peer", proc.stdout)

    def test_an_ip_range_peer_on_the_alertmanager_port_is_an_offender(self):
        """A5: a peer with only ipBlock was skipped as 'not loki', so a CIDR admitted to
        :9093 was certified narrow."""
        code = self._splice("stage5-platform.sh", "WOUT=$(python3 - \"$NETPOL\" <<'PY'\n", "\nPY\n")
        root = self._root()
        src = ROOT / "template" / "provisioning" / "clusters" / "platform" / "config" / "monitoring" / "netpol.yaml"
        docs = [d for d in yaml.safe_load_all(src.read_text()) if isinstance(d, dict)]
        for peer, want in (({"ipBlock": {"cidr": "10.0.0.0/8"}}, "admits an IP range (ipBlock) to :9093"), ({"unknown": {}}, "is a peer of no readable form")):
            with self.subTest(peer=list(peer)[0]):
                extra = {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": {"name": "range", "namespace": "monitoring"},
                         "spec": {"podSelector": {"matchLabels": {"app.kubernetes.io/name": "alertmanager"}}, "policyTypes": ["Ingress"],
                                  "ingress": [{"from": [peer], "ports": [{"port": 9093}]}]}}
                _write(root / "f.yaml", yaml.safe_dump_all(docs + [extra]))
                proc = self._py(code, root / "f.yaml")
                self.assertNotEqual(proc.returncode, 0); self.assertIn("policy 'range' ingress[0] from[0] " + want, proc.stdout)

    def test_a_pin_contradicted_by_another_term_on_the_same_key_is_not_a_pin(self):
        """A6: matchLabels app=traefik plus a NotIn/In-without/DoesNotExist expression on
        the same key selects nothing, yet counted as a traefik grant."""
        code = self._splice("stage5-platform.sh", "WIOUT=$(python3 - \"$TMP/intake-netpols.yaml\" <<'PY'\n", "\nPY\n")
        src = ROOT / "template" / "provisioning" / "clusters" / "platform" / "config" / "orbit-intake" / "netpol.yaml"
        docs = [d for d in yaml.safe_load_all(src.read_text().replace("{{", "").replace("}}", "")) if isinstance(d, dict)]
        pol = next(d for d in docs if d.get("kind") == "NetworkPolicy")
        peer = next(p for r in pol["spec"]["ingress"] for p in r.get("from", []) if p.get("podSelector"))
        key = next(iter(peer["podSelector"]["matchLabels"]))
        root = self._root()
        proc = self._py(code, (_write(root / "base.yaml", yaml.safe_dump_all(docs)), root / "base.yaml")[1])
        self.assertEqual(proc.returncode, 0, proc.stdout)
        for expr in ({"key": key, "operator": "NotIn", "values": [peer["podSelector"]["matchLabels"][key]]},
                     {"key": key, "operator": "In", "values": ["other"]}, {"key": key, "operator": "DoesNotExist"}):
            with self.subTest(op=expr["operator"]):
                d2 = copy.deepcopy(docs); p2 = next(d for d in d2 if d.get("kind") == "NetworkPolicy")
                pe = next(p for r in p2["spec"]["ingress"] for p in r.get("from", []) if p.get("podSelector"))
                pe["podSelector"]["matchExpressions"] = [expr]
                _write(root / "f.yaml", yaml.safe_dump_all(d2))
                proc = self._py(code, root / "f.yaml")
                self.assertNotEqual(proc.returncode, 0, proc.stdout); self.assertIn("no effective rule admits the traefik pod", proc.stdout)

    def test_a_rejected_rule_object_is_named_and_promtools_message_is_not_repeated(self):
        """A8: every failed promtool run overwrote one output file; the row showed the last
        one's first lines (which quote rule text) and named no object."""
        text = self._stage("stage3.sh")
        self.assertNotIn('$(head -3 "$A/pt-real-out"', text)
        block = self._splice("stage3.sh", "      RR_BAD=0; RR_WHICH=\"\"\n", "\n      [ \"$RR_BAD\" -eq 0 ]")
        root = self._root(); (root / "real-rules").mkdir()
        for i in range(3): _write(root / "real-rules" / ("%d.yaml" % i), "x")
        env = self._stub(root, "promtool", "case \"$3\" in *0.yaml|*2.yaml) echo 'error: PLANTED_RULE_TEXT'; exit 1;; *) exit 0;; esac")
        proc = self._bash("A=" + str(root) + "; PT=promtool\n" + block + "\necho \"BAD=$RR_BAD WHICH=[$RR_WHICH]\"\n", env=env)
        self.assertIn("BAD=2 WHICH=[ 0.yaml 2.yaml]", proc.stdout); self.assertNotIn("PLANTED", proc.stdout + proc.stderr)
        self.assertIn("PLANTED_RULE_TEXT", (root / "pt-real-fail").read_text())


class Round15Test(unittest.TestCase):
    """Area-1 sweep round 15: each row pins one repair."""

    def _stage(self, name):
        return (ROOT / "scripts" / "verify" / name).read_text()

    def _splice(self, stage, start, end):
        text = self._stage(stage)
        i = text.index(start) + len(start)
        return text[i:text.index(end, i)]

    def _root(self):
        root = pathlib.Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        return root

    def _bash(self, script, cwd=None, env=None, trace=False):
        return subprocess.run(["bash"] + (["-x"] if trace else []) + ["-c", script], cwd=cwd, capture_output=True,
                              text=True, timeout=120, env={**os.environ, **(env or {})})

    def _py(self, code, *args, cwd=None):
        return subprocess.run([sys.executable, "-c", code] + [str(a) for a in args], cwd=cwd, capture_output=True, text=True, timeout=60)

    def _stub(self, root, name, body):
        _write(root / "bin" / name, "#!/bin/sh\nprintf '%s\\n' \"$@\" >> \"$KLOG\"\n" + body + "\n", executable=True)
        return {"PATH": str(root / "bin") + os.pathsep + os.environ["PATH"], "KLOG": str(root / "stub.log")}

    OKBAD = "ok(){ echo \"  ok    $1\"; }; bad(){ echo \"  FAIL  $1\"; }\n"

    # ---- breaker -------------------------------------------------------------------
    def test_an_ingress_host_reaches_the_coverage_row_only_in_shape(self):
        """B1: the Ingress host listing was a shell variable and an unshaped host was
        printed verbatim in the row."""
        s5 = self._stage("stage5-platform.sh")
        self.assertNotIn("ING_RAW=$(kubectl", s5)
        block = s5[s5.index("    # the host listing is cluster text"):s5.index("); UNCOV_RC=$?") + len("); UNCOV_RC=$?")]
        root = self._root(); tmp = root / "tmp"; tmp.mkdir(); _write(tmp / "want.txt", "https://a.fixture.org/\n")
        env = self._stub(root, "kubectl", "printf 'a.fixture.org\\nb.fixture.org\\nBearer PLANTED_INGRESS_CREDENTIAL\\n'")
        proc = self._bash("TMP=" + str(tmp) + "; ORBIT_DOMAIN=fixture.org\n" + block + "\necho \"NING=$NING UNCOV=[$UNCOV] RC=$UNCOV_RC\"\n", env=env, trace=True)
        self.assertIn("NING=3 UNCOV=[b.fixture.org (+1 host(s) not of a hostname shape, or of a credential's shape — unprobed by construction)] RC=0", proc.stdout)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_a_loaded_rule_is_a_shaped_name_and_a_fixed_health(self):
        """B2: a failing rule's name and health (server text) were printed verbatim."""
        code = self._splice("stage5-platform.sh", "    python3 - \"$TMP/rules.json\" > \"$TMP/loaded.txt\" 2>/dev/null <<'PY'\n", "\nPY\n")
        root = self._root()
        _write(root / "rules.json", json.dumps({"data": {"groups": [{"rules": [
            {"type": "alerting", "name": "KubeDown", "health": "ok"},
            {"type": "alerting", "name": "Bearer PLANTED_RULE_CREDENTIAL", "health": "err"},
            {"type": "alerting", "name": "Odd", "health": "Bearer PLANTED_HEALTH_CREDENTIAL"},
            {"type": "recording", "name": "x:y", "health": "ok"}]}]}}))
        proc = self._py(code, root / "rules.json")
        self.assertEqual(proc.stdout.splitlines(), ["KubeDown ok", "(a-rule-name-not-of-an-alert-name-shape) err", "Odd unreadable"])
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)
        s5 = self._stage("stage5-platform.sh")
        self.assertIn('grep -Eq " (err|unreadable)$" "$TMP/loaded.txt"', s5)

    def test_a_probe_marker_never_sits_raw_in_a_shell_variable(self):
        """B3: the marker was captured raw, then shaped — a traced shell printed the raw
        capture. The loki probe's output was a shell variable outright."""
        s5 = self._stage("stage5-platform.sh")
        block = next(l for l in s5.splitlines() if l.startswith("marker(){")) + "\n" + s5[s5.index('  OPF="$TMP/obsplane.out"'):s5.index('    case "$OPPROM" in')]
        root = self._root()
        env = self._stub(root, "kubectl", "printf 'PROM=Bearer PLANTED_PROBE_CREDENTIAL\\nAM=000\\nCONTROL=403\\nMARK=obsplane-complete\\n'")
        proc = self._bash(self.OKBAD + "TMP=$(mktemp -d); OPPOD=p; OPSCRIPT=x; LOKI_ON=0; TEMPO_ON=0\n" + block + "\n  fi\n", env=env, trace=True)
        self.assertIn("ok    the off-path probe pod reached", proc.stdout); self.assertIn("OPPROM=unreadable", proc.stderr)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)
        self.assertIn('> "$LNF" 2>/dev/null', s5); self.assertIn('if [ ! -s "$LNF" ]; then', s5)

    def test_a_datasource_health_is_a_fixed_category(self):
        """B4: Grafana's health status text was printed in the row."""
        s5 = self._stage("stage5-platform.sh")
        code = s5[s5.index("TDS=$(grafapi /api/datasources/uid/tempo/health | python3 -c \"") + len("TDS=$(grafapi /api/datasources/uid/tempo/health | python3 -c \""):]
        code = code[:code.index("\")\n")]
        for body, want in (('{"status":"OK"}', "OK"), ('{"status":"Bearer PLANTED_TEMPO_CREDENTIAL"}', "not-OK"), ("null", "unreadable"), ("{", "unreadable"), ('{"status":""}', "unreadable")):
            with self.subTest(body=body):
                proc = subprocess.run([sys.executable, "-c", code], input=body, capture_output=True, text=True, timeout=60)
                self.assertEqual(proc.stdout.strip(), want); self.assertNotIn("PLANTED", proc.stdout + proc.stderr)
        self.assertIn("datasource health is $TDS (its status text is not repeated)", s5)

    def test_tenant_namespace_and_pod_names_reach_argv_only_in_shape(self):
        """B5: a namespace or pod name from the API became a kubectl argument as-is."""
        s5 = self._stage("stage5-platform.sh")
        nsfn = s5[s5.index("project_namespaces(){"):s5.index("\n}\n", s5.index("project_namespaces(){")) + 3]
        nsfn += next(l for l in s5.splitlines() if l.startswith("ns_odd(){")) + "\n"
        root = self._root(); tmp = root / "tmp"; tmp.mkdir()
        env = self._stub(root, "kubectl", "printf '%s' '{\"items\":[{\"metadata\":{\"name\":\"tenant-a\",\"labels\":{\"orbit.dev/project\":\"a\"}}},{\"metadata\":{\"name\":\"tenant\\\"PLANTED\",\"labels\":{\"orbit.dev/project\":\"b\"}}},{\"metadata\":{\"name\":\"plain\"}}]}'")
        proc = self._bash("TMP=" + str(tmp) + "\n" + nsfn + "NSLIST=$(project_namespaces); echo \"NS=[$NSLIST] ODD=$(ns_odd)\"\n", env=env, trace=True)
        self.assertIn("NS=[tenant-a] ODD=1", proc.stdout); self.assertNotIn("PLANTED", proc.stdout + proc.stderr)
        self.assertNotIn("PODS=$(kubectl", s5)
        lines = [l for l in s5.splitlines() if l.startswith("    PODS=$(tr ' '") or l.startswith("    PODODD=$((")]
        self.assertEqual(len(lines), 2)
        _write(tmp / "pods.raw", "pod-1 pod\"PLANTED api-7c9")
        proc = self._bash("TMP=" + str(tmp) + "\n" + "\n".join(lines) + "\necho \"PODS=[$PODS] ODD=$PODODD\"\n", trace=True)
        self.assertIn("PODS=[pod-1 api-7c9 ] ODD=1", proc.stdout); self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    # ---- auditor -------------------------------------------------------------------
    def test_only_a_rule_that_could_admit_traefik_is_held_to_the_doors_contract(self):
        """A1: the audit (which reads every policy in the namespace since round 14) held
        the plane's own-pods default-deny and the monitoring grant to the traefik door's
        contract, so a correct instance failed."""
        code = self._splice("stage5-platform.sh", "WIOUT=$(python3 - \"$TMP/intake-netpols.yaml\" <<'PY'\n", "\nPY\n")
        src = ROOT / "template" / "provisioning" / "clusters" / "platform" / "config" / "orbit-intake" / "netpol.yaml"
        door = [d for d in yaml.safe_load_all(src.read_text().replace("{{", "").replace("}}", "")) if isinstance(d, dict)]
        intake = {"app.kubernetes.io/name": "intake", "app.kubernetes.io/instance": "orbit-agents"}
        deny = {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": {"name": "default-deny-ingress", "namespace": "orbit-agents-prod"},
                "spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"], "ingress": [{"from": [{"podSelector": {}}]}],
                         "egress": [{"to": [{"ipBlock": {"cidr": "0.0.0.0/0"}}]}]}}
        mon = {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": {"name": "orbit-agents-intake-allow-monitoring", "namespace": "orbit-agents-prod"},
               "spec": {"podSelector": {"matchLabels": intake}, "policyTypes": ["Ingress"],
                        "ingress": [{"from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "monitoring"}}}], "ports": [{"protocol": "TCP", "port": 8080}]}]}}
        root = self._root()
        def run(docs):
            _write(root / "f.yaml", yaml.safe_dump_all(docs)); return self._py(code, root / "f.yaml")
        proc = run(door + [deny, mon])
        self.assertEqual(proc.returncode, 0, proc.stdout); self.assertIn("1 effective traefik grant(s)", proc.stdout)
        # a tenant-style default-deny (the whole traefik namespace to every pod) IS the door's business
        wide = copy.deepcopy(deny); wide["spec"]["ingress"][0]["from"].append({"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "traefik"}}})
        proc = run(door + [wide, mon])
        self.assertNotEqual(proc.returncode, 0); self.assertIn("default-deny-ingress does not pin its target to the intake pod", proc.stdout)
        # so is an unpinned namespaceSelector, an IP range, and a rule with no from
        for peer in ({"namespaceSelector": {}}, {"ipBlock": {"cidr": "10.0.0.0/8"}}, {"namespaceSelector": {"matchExpressions": [{"key": "kubernetes.io/metadata.name", "operator": "Exists"}]}}):
            with self.subTest(peer=str(peer)[:30]):
                extra = {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": {"name": "widen", "namespace": "orbit-agents-prod"},
                         "spec": {"podSelector": {"matchLabels": intake}, "policyTypes": ["Ingress"], "ingress": [{"from": [peer], "ports": [{"protocol": "TCP", "port": 8080}]}]}}
                proc = run(door + [deny, mon, extra])
                self.assertNotEqual(proc.returncode, 0); self.assertIn("widen ingress[0] from[0] is not narrowed", proc.stdout)
        extra = {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": {"name": "open", "namespace": "orbit-agents-prod"},
                 "spec": {"podSelector": {"matchLabels": intake}, "policyTypes": ["Ingress"], "ingress": [{"ports": [{"protocol": "TCP", "port": 8080}]}]}}
        proc = run(door + [deny, mon, extra])
        self.assertNotEqual(proc.returncode, 0); self.assertIn("open ingress[0] admits every peer (no from:)", proc.stdout)
        # a peer pinned to another namespace beside the traefik one is not counted
        both = copy.deepcopy(door); pol = next(d for d in both if d.get("kind") == "NetworkPolicy")
        pol["spec"]["ingress"][0]["from"].append({"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "monitoring"}}})
        proc = run(both + [deny])
        self.assertEqual(proc.returncode, 0, proc.stdout); self.assertIn("OK 1 peer(s)", proc.stdout)


class Round16Test(unittest.TestCase):
    """Area-1 sweep round 16: each row pins one repair."""

    def _stage(self, name):
        return (ROOT / "scripts" / "verify" / name).read_text()

    def _splice(self, stage, start, end):
        text = self._stage(stage)
        i = text.index(start) + len(start)
        return text[i:text.index(end, i)]

    def _root(self):
        root = pathlib.Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        return root

    def _bash(self, script, cwd=None, env=None, trace=False):
        return subprocess.run(["bash"] + (["-x"] if trace else []) + ["-c", script], cwd=cwd, capture_output=True,
                              text=True, timeout=120, env={**os.environ, **(env or {})})

    def _py(self, code, *args, cwd=None, stdin=None):
        return subprocess.run([sys.executable, "-c", code] + [str(a) for a in args], cwd=cwd, input=stdin, capture_output=True, text=True, timeout=60)

    def _stub(self, root, name, body):
        _write(root / "bin" / name, "#!/bin/sh\nprintf '%s\\n' \"$@\" >> \"$KLOG\"\n" + body + "\n", executable=True)
        return {"PATH": str(root / "bin") + os.pathsep + os.environ["PATH"], "KLOG": str(root / "stub.log")}

    OKBAD = "ok(){ echo \"  ok    $1\"; }; bad(){ echo \"  FAIL  $1\"; }\n"
    GATHER = "gather_netpols(){ python3 - clusters \"$2\" \"$1\" <<'PY'\n"
    SVC = "SVCPORT=$(python3 - . <<'PY'\n"

    def _app(self, root, path, ns, recurse=True, exclude=None, chart=None, name="app"):
        # a chart source that ALSO names a path: the renderer dispatches on `chart` first, so the path is not a directory source
        src = {"path": path} if chart is None else {"chart": chart, "path": path, "repoURL": "https://grafana.github.io/helm-charts"}
        if chart is None:
            src["directory"] = {"recurse": recurse, **({"exclude": exclude} if exclude else {})}
        _write(root / "clusters" / "platform" / "apps" / (name + ".yaml"), yaml.safe_dump({"apiVersion": "argoproj.io/v1alpha1", "kind": "Application",
               "metadata": {"name": name, "namespace": "argocd"}, "spec": {"source": src, "destination": {"namespace": ns}}}))

    # ---- auditor -------------------------------------------------------------------
    def test_an_unreadable_catalog_is_unreadable_never_zero_and_never_a_traceback(self):
        """A1: the population count fell back to 0 (every population row keyed off) and
        two readers let PyYAML's message, which quotes the input, reach stderr."""
        s3 = self._stage("stage3.sh")
        root = self._root(); (root / "catalog").mkdir(); (root / "instance").mkdir()
        _write(root / "catalog" / "projects.yaml", "projects:\n  - id: a\n    token: \"PLANTED_UNTERMINATED_CREDENTIAL\n")
        _write(root / "instance" / "config.yaml", "objectStorage: {buckets: {platform: b}}\n")
        for start, end, want in (("PROJCOUNT=$(python3 -c \"\n", "\" 2>/dev/null || echo 'UNREADABLE(no-python)')", "UNREADABLE(ScannerError)"),
                                 ("EXPECT_NS=$(python3 -c \"\n", "\" 2>/dev/null)\n", "UNREADABLE(ScannerError)"),
                                 ("WAL_DBS=$(python3 -c \"\n", "\" 2>/dev/null)\nif [ -z", "")):
            with self.subTest(block=start[:9]):
                proc = self._py(self._splice("stage3.sh", start, end), cwd=root)
                self.assertEqual(proc.stdout.strip(), want); self.assertNotIn("PLANTED", proc.stdout + proc.stderr); self.assertEqual(proc.stderr, "")
        block = s3[s3.index("PROJCOUNT=$(python3 -c \""):s3.index("esac\n", s3.index("PROJCOUNT=$(python3 -c \"")) + 5]
        proc = self._bash(self.OKBAD + block + "echo \"PROJCOUNT=[$PROJCOUNT]\"\n", cwd=root)
        self.assertIn("FAIL  catalog/projects.yaml could not be read (UNREADABLE(ScannerError)", proc.stdout); self.assertIn("PROJCOUNT=[UNREADABLE]", proc.stdout)
        self.assertNotIn('"$PROJCOUNT" -eq', s3)
        self.assertIn('if [ "$PROJCOUNT" = UNREADABLE ] || [ "$EXPECT_NS" = UNREADABLE ]; then', s3)

    def test_a_selector_constraining_a_label_the_pod_is_not_known_to_carry_is_not_a_pin(self):
        """A2: matchLabels app=intake plus a constraint on another key (a label the pod
        lacks, or one contradicted by a DoesNotExist) selects nothing, yet counted."""
        code = self._splice("stage5-platform.sh", "WIOUT=$(python3 - \"$TMP/intake-netpols.yaml\" <<'PY'\n", "\nPY\n")
        src = ROOT / "template" / "provisioning" / "clusters" / "platform" / "config" / "orbit-intake" / "netpol.yaml"
        docs = [d for d in yaml.safe_load_all(src.read_text().replace("{{", "").replace("}}", "")) if isinstance(d, dict)]
        root = self._root()
        def run(mut):
            d2 = copy.deepcopy(docs); pol = next(d for d in d2 if d.get("kind") == "NetworkPolicy"); mut(pol)
            _write(root / "f.yaml", yaml.safe_dump_all(d2)); return self._py(code, root / "f.yaml")
        self.assertEqual(run(lambda p: None).returncode, 0)
        def peer(p): return next(x for r in p["spec"]["ingress"] for x in r.get("from", []) if x.get("podSelector"))
        cases = {
            "target extra key": lambda p: p["spec"]["podSelector"]["matchLabels"].update({"review.example/blocked": "yes"}),
            "target contradicted": lambda p: (p["spec"]["podSelector"]["matchLabels"].update({"review.example/blocked": "yes"}),
                                              p["spec"]["podSelector"].update({"matchExpressions": [{"key": "review.example/blocked", "operator": "DoesNotExist"}]})),
            "peer pod extra key": lambda p: peer(p)["podSelector"].update({"matchExpressions": [{"key": "other", "operator": "Exists"}]}),
            "peer ns extra key": lambda p: peer(p)["namespaceSelector"]["matchLabels"].update({"team": "x"}),
        }
        for name, mut in cases.items():
            with self.subTest(case=name):
                proc = run(mut); self.assertNotEqual(proc.returncode, 0); self.assertIn("no effective rule admits the traefik pod", proc.stdout)

    def test_membership_is_the_renderers_recurse_exclude_and_chart_rules(self):
        """A3: the gatherer and the Service index read every file under clusters/; the
        renderer applies only what an Application's path, recurse and exclude select."""
        gather = self._splice("stage5-platform.sh", self.GATHER, "\nPY\n")
        pol = lambda name: {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": {"name": name}, "spec": {"podSelector": {}}}
        root = self._root()
        self._app(root, "clusters/platform/config", "monitoring", recurse=True, exclude="orbit-intake/**")
        self._app(root, "clusters/flat", "monitoring", recurse=False, name="flat")
        self._app(root, "clusters/charted", "monitoring", chart="loki", name="charted")
        _write(root / "clusters" / "platform" / "config" / "orbit-intake" / "netpol.yaml", yaml.safe_dump(pol("excluded")))
        _write(root / "clusters" / "platform" / "config" / "monitoring" / "netpol.yaml", yaml.safe_dump(pol("kept")))
        _write(root / "clusters" / "flat" / "top.yaml", yaml.safe_dump(pol("top")))
        _write(root / "clusters" / "flat" / "deep" / "x.yaml", yaml.safe_dump(pol("deep")))
        _write(root / "clusters" / "charted" / "np.yaml", yaml.safe_dump(pol("under-a-chart")))
        _write(root / "clusters" / "nowhere" / "np.yaml", yaml.safe_dump({**pol("explicit-but-unapplied"), "metadata": {"name": "explicit-but-unapplied", "namespace": "monitoring"}}))
        proc = self._py(gather, "clusters", root / "g.yaml", "monitoring", cwd=root)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        names = sorted(d["metadata"]["name"] for d in yaml.safe_load_all((root / "g.yaml").read_text()) if isinstance(d, dict))
        self.assertEqual(names, ["kept", "top"])
        # the Service index, the same rule
        code = self._splice("stage5-platform.sh", self.SVC, "\nPY\n"); (root / "catalog").mkdir()
        svc = lambda name: {"apiVersion": "v1", "kind": "Service", "metadata": {"name": name}, "spec": {"ports": [{"port": 80}]}}
        _write(root / "clusters" / "flat" / "deep" / "svc.yaml", yaml.safe_dump(svc("deep-svc")))
        _write(root / "clusters" / "platform" / "config" / "orbit-intake" / "svc.yaml", yaml.safe_dump(svc("excluded-svc")))
        _write(root / "clusters" / "platform" / "config" / "monitoring" / "cm.yaml", yaml.safe_dump({"kind": "ConfigMap", "metadata": {"name": "c"},
               "data": {"a": "http://deep-svc.monitoring.svc/", "b": "http://excluded-svc.monitoring.svc/"}}))
        proc = self._py(code, ".", cwd=root)
        self.assertNotEqual(proc.returncode, 0); self.assertIn("names Service deep-svc.monitoring, which this repo neither declares", proc.stdout)
        self.assertIn("names Service excluded-svc.monitoring, which this repo neither declares", proc.stdout)

    def test_promtool_is_fetched_privately_and_published_by_one_rename(self):
        """A4: two concurrent first runs extracted into the shared cache and raced on
        one `mv`; the loser found no source."""
        s3 = self._stage("stage3.sh")
        self.assertIn('work=$(mktemp -d "$PROMTOOL_CACHE/.fetch.XXXXXX" 2>/dev/null) || return 1', s3)
        self.assertIn('tar -xzf "$tgz" -C "$work" --strip-components=1', s3)
        self.assertNotIn('mv "$PROMTOOL_CACHE/promtool" "$dest"', s3)
        fn = s3[s3.index("publish_promtool(){"):s3.index("\n}\n", s3.index("publish_promtool(){")) + 3]
        root = self._root(); _write(root / "mine", "#!/bin/sh\necho mine\n", executable=True); _write(root / "theirs", "#!/bin/sh\necho theirs\n", executable=True)
        proc = self._bash(fn + "publish_promtool " + str(root / "mine") + " " + str(root / "dest") + "; echo rc=$?; " + str(root / "dest"))
        self.assertIn("rc=0\nmine", proc.stdout)
        _write(root / "mine2", "#!/bin/sh\necho mine2\n", executable=True)
        proc = self._bash(fn + "publish_promtool " + str(root / "mine2") + " " + str(root / "dest") + "; echo rc=$?; " + str(root / "dest"))
        self.assertIn("rc=0\nmine", proc.stdout); self.assertTrue((root / "mine2").exists())

    # ---- breaker -------------------------------------------------------------------
    def test_retention_is_the_field_itself_and_reaches_the_shell_only_as_a_duration(self):
        """B1: a grep for the words took a comment's value, and an unshaped value was
        printed in the row."""
        s5 = self._stage("stage5-platform.sh")
        fn = s5[s5.index("retention_of(){"):s5.index("\n}\n", s5.index("retention_of(){")) + 3]
        root = self._root()
        _write(root / "loki.yaml", "spec:\n  source:\n    helm:\n      valuesObject:\n        loki:\n          # retention_period: 14d is the documented floor\n          limits_config:\n            retention_period: 1h\n")
        _write(root / "bad.yaml", "limits_config:\n  retention_period: plantedretentioncredential0123456789\n")
        _write(root / "cfg.yaml", "limits_config:\n  retention_period: 14d\n")
        out = self._bash(fn + "printf '[%s]' \"$(retention_of " + str(root / "loki.yaml") + " spec.source.helm.valuesObject.loki.limits_config.retention_period)\" \"$(retention_of "
                         + str(root / "bad.yaml") + " limits_config.retention_period)\" \"$(retention_of " + str(root / "cfg.yaml") + " limits_config.retention_period)\" \"$(retention_of "
                         + str(root / "none.yaml") + " a.b)\" \"$(retention_of " + str(root / "cfg.yaml") + " limits_config.nothing)\"\n", trace=True)
        self.assertEqual(out.stdout, "[1h][unreadable][14d][][]"); self.assertNotIn("planted", out.stdout + out.stderr)
        self.assertNotIn('grep -o "retention_period', s5); self.assertIn('LIVERET=$(retention_of "$TMP/loki-config.yaml" limits_config.retention_period)', s5)

    def test_a_can_i_answer_reaches_the_shell_as_yes_no_or_unreadable(self):
        """B2: the authorization answer was captured raw (nine traced assignments)."""
        s5 = self._stage("stage5-platform.sh")
        fn = next(l for l in s5.splitlines() if l.startswith("cani(){"))
        self.assertNotIn("CANI=$(kubectl auth can-i", s5); self.assertNotIn("CANIOWN=$(kubectl auth can-i", s5)
        root = self._root()
        for body, want in (("echo yes", "yes"), ("echo no", "no"), ("echo 'Bearer PLANTED_CAN_I_CREDENTIAL'", "unreadable"), ("exit 1", "unreadable"), ("printf 'yes\\nmore'", "yes")):
            with self.subTest(body=body):
                env = self._stub(root, "kubectl", body)
                proc = self._bash(fn + "\nCANI=$(cani get secrets -n x); echo \"[$CANI]\"\n", env=env, trace=True)
                self.assertIn("[" + want + "]", proc.stdout); self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_a_udp_only_port_does_not_answer_a_tcp_dial(self):
        """B4: a Probe of :8080 passed against a Service exposing only UDP/8080."""
        code = self._splice("stage5-platform.sh", self.SVC, "\nPY\n")
        root = self._root(); (root / "catalog").mkdir(); _dirapp(root)
        def run(proto):
            _write(root / "clusters" / "x.yaml", yaml.safe_dump_all([
                {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "s", "namespace": "monitoring"}, "spec": {"ports": [{"port": 8080, "protocol": proto}]}},
                {"kind": "ConfigMap", "metadata": {"name": "c", "namespace": "monitoring"}, "data": {"u": "http://s.monitoring.svc:8080/"}}]))
            return self._py(code, ".", cwd=root)
        self.assertEqual(run("TCP").returncode, 0, run("TCP").stdout)
        proc = run("UDP"); self.assertNotEqual(proc.returncode, 0); self.assertIn("does not declare on TCP", proc.stdout)

    def test_an_applied_file_the_renderer_cannot_read_is_red_not_skipped(self):
        """B5: a malformed policy file under an applied directory was silently skipped."""
        gather = self._splice("stage5-platform.sh", self.GATHER, "\nPY\n")
        root = self._root(); _dirapp(root)
        _write(root / "clusters" / "np.yaml", "kind: NetworkPolicy\nmetadata: [\n")
        _write(root / "clusters" / "README.md", "not: [yaml\n")
        proc = self._py(gather, "clusters", root / "g.yaml", "monitoring", cwd=root)
        self.assertEqual(proc.returncode, 3); self.assertIn("1 applied manifest file(s) under clusters/ cannot be read as YAML", proc.stdout)
        s5 = self._stage("stage5-platform.sh")
        self.assertIn('if GERR=$(gather_netpols monitoring "$TMP/monitoring-netpols.yaml"); then :; else', s5)
        self.assertIn('if GERR=$(gather_netpols orbit-agents-prod "$TMP/intake-netpols.yaml"); then :; else', s5)
        code = self._splice("stage5-platform.sh", self.SVC, "\nPY\n"); (root / "catalog").mkdir()
        proc = self._py(code, ".", cwd=root)
        self.assertNotEqual(proc.returncode, 0); self.assertIn("1 applied manifest file(s) cannot be read as YAML", proc.stdout)
        (root / "clusters" / "np.yaml").unlink()
        self.assertEqual(self._py(code, ".", cwd=root).returncode, 0)

    def test_the_kubeconfig_list_is_split_never_glob_expanded(self):
        """B6: KUBECONFIG naming a nonexistent `glob-kube-*` matched a sibling file and
        the stage went live."""
        root = self._root(); _write(root / "glob-kube-real", "apiVersion: v1\n")
        for stage in ("stage2.sh", "stage3.sh", "stage4.sh", "stage5-platform.sh"):
            with self.subTest(stage=stage):
                text = self._stage(stage)
                fn = text[text.index("kubeconfig_present(){"):text.index("return $any; }\n", text.index("kubeconfig_present(){")) + len("return $any; }\n")]
                out = self._bash(fn + "kubeconfig_present '" + str(root / "glob-kube-*") + "' && echo LIVE || echo PREBOOT; kubeconfig_present '" + str(root / "glob-kube-real") + ":/nonexistent' && echo LIVE || echo PREBOOT; echo \"glob=$(echo *)\"\n", cwd=root).stdout
                self.assertEqual(out.splitlines()[:2], ["PREBOOT", "LIVE"]); self.assertIn("glob=glob-kube-real", out)


class Round17Test(unittest.TestCase):
    """Area-1 sweep round 17: each row pins one repair."""

    def _stage(self, name):
        return (ROOT / "scripts" / "verify" / name).read_text()

    def _splice(self, stage, start, end):
        text = self._stage(stage)
        i = text.index(start) + len(start)
        return text[i:text.index(end, i)]

    def _root(self):
        root = pathlib.Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        return root

    def _bash(self, script, cwd=None, env=None, trace=False):
        return subprocess.run(["bash"] + (["-x"] if trace else []) + ["-c", script], cwd=cwd, capture_output=True,
                              text=True, timeout=120, env={**os.environ, **(env or {})})

    def _py(self, code, *args, cwd=None):
        return subprocess.run([sys.executable, "-c", code] + [str(a) for a in args], cwd=cwd, capture_output=True, text=True, timeout=60)

    def _stub(self, root, name, body):
        _write(root / "bin" / name, "#!/bin/sh\nprintf '%s\\n' \"$@\" >> \"$KLOG\"\n" + body + "\n", executable=True)
        return {"PATH": str(root / "bin") + os.pathsep + os.environ["PATH"], "KLOG": str(root / "stub.log")}

    def _app(self, root, path, ns, name):
        _write(root / "clusters" / "platform" / "apps" / (name + ".yaml"), yaml.safe_dump({"apiVersion": "argoproj.io/v1alpha1", "kind": "Application",
               "metadata": {"name": name, "namespace": "argocd"}, "spec": {"source": {"path": path, "directory": {"recurse": True}}, "destination": {"namespace": ns}}}))

    OKBAD = "ok(){ echo \"  ok    $1\"; }; bad(){ echo \"  FAIL  $1\"; }\n"
    GATHER = "gather_netpols(){ python3 - clusters \"$2\" \"$1\" <<'PY'\n"
    SVC = "SVCPORT=$(python3 - . <<'PY'\n"
    WIOUT = "WIOUT=$(python3 - \"$TMP/intake-netpols.yaml\" <<'PY'\n"

    def _load(self, cfg, env_extra=None):
        root = self._root(); (root / "instance").mkdir()
        (root / "instance/config.yaml").write_text(yaml.safe_dump(cfg))
        script = "source " + str(ROOT / "scripts" / "lib-instance.sh") + "; rc=$?; if [ $rc = 0 ]; then echo \"rc=0 [$ORBIT_DOMAIN]\"; else echo \"rc=$rc\"; fi"
        env = {k: v for k, v in os.environ.items() if not k.startswith("ORBIT_")}
        env["ORBIT_INSTANCE_CONFIG"] = str(root / "instance/config.yaml"); env.update(env_extra or {})
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60, env=env)

    CFG = {"instance": {"name": "fx"}, "domains": {"primary": "fixture.org", "dev": "dev.fixture.org", "dnsZones": ["fixture.org"]},
           "registry": "ghcr.io/fx", "git": {"repoUrl": "git@github.com:fx/fx.git", "github_org": "fx", "branch": "main"},
           "nodes": [{"name": "n1", "ip": "203.0.113.5", "role": "control-plane"}]}

    # ---- A1 = B1 -------------------------------------------------------------------
    def test_cluster_reference_listings_are_private_files_and_unshaped_references_are_red(self):
        """A1/B1: the Middleware and imagePullSecret inventories sat in shell variables
        (traced), an unshaped basic-auth reference was silently dropped, and an agent's
        Available status was printed raw."""
        s3 = self._stage("stage3.sh")
        self.assertNotIn("MWREFS=$(kubectl", s3); self.assertNotIn("PULLREFS=$(kubectl", s3)
        root = self._root()
        mw = s3[s3.index("MWF=$(mktemp)"):s3.index('rm -f "$MWF"') + len('rm -f "$MWF"')]
        env = self._stub(root, "kubectl", "case \"$*\" in *'get secret'*) exit 0;; *) printf 'default/Bearer PLANTED_MW_CREDENTIAL\\ntraefik/dev-basic-auth\\n';; esac")
        proc = self._bash(self.OKBAD + mw, env=env, trace=True)
        self.assertIn("FAIL  1 basic-auth Middleware reference(s) are not of a namespace/name shape", proc.stdout)
        self.assertIn("ok    every basic-auth Middleware has its Secret", proc.stdout); self.assertNotIn("PLANTED", proc.stdout + proc.stderr)
        pull = s3[s3.index("PULLF=$(mktemp)"):s3.index('rm -f "$PULLF"') + len('rm -f "$PULLF"')]
        env = self._stub(root, "kubectl", "case \"$*\" in *'get secret'*) exit 1;; *) printf 'team-a/regcred\\nBearer PLANTED_PULL_CREDENTIAL\\n';; esac")
        proc = self._bash(self.OKBAD + pull, env=env, trace=True)
        self.assertIn("FAIL  1 imagePullSecret reference(s) are not of a namespace/name shape", proc.stdout)
        self.assertIn("FAIL  referenced but absent: team-a/regcred", proc.stdout); self.assertNotIn("PLANTED", proc.stdout + proc.stderr)
        avail = s3[s3.index("    # the condition status is cluster text"):s3.index('(status=$AVAIL)"') + len('(status=$AVAIL)"')]
        for body, want in (("echo True", "ok    agent a is Available"), ("echo 'Bearer PLANTED_AVAIL_CREDENTIAL'", "not Available (status=unreadable)"),
                           ("printf ''", "not Available (status=absent)"), ("echo False", "not Available (status=False)")):
            with self.subTest(body=body[:14]):
                env = self._stub(root, "kubectl", body)
                proc = self._bash(self.OKBAD + "AG=a\n" + avail + "\n", env=env, trace=True)
                self.assertIn(want, proc.stdout); self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    # ---- auditor -------------------------------------------------------------------
    def test_a_file_two_applications_apply_is_applied_into_both_destinations(self):
        """A2: one nested directory under two Applications was assigned to the longer
        prefix only, so its policy left the monitoring audit and its Service the index."""
        gather = self._splice("stage5-platform.sh", self.GATHER, "\nPY\n")
        root = self._root()
        self._app(root, "clusters/platform/monitoring", "monitoring", "monitoring-config"); self._app(root, "clusters/platform/monitoring/review", "sandbox", "review")
        _write(root / "clusters" / "platform" / "monitoring" / "review" / "np.yaml", yaml.safe_dump_all([
            {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": {"name": "wide"}, "spec": {"podSelector": {}}},
            {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "s"}, "spec": {"ports": [{"port": 80}]}},
            {"kind": "ConfigMap", "metadata": {"name": "c"}, "data": {"u": "http://only-in-monitoring:80/", "v": "http://only-in-sandbox:80/"}}]))
        for ns in ("monitoring", "sandbox"):
            with self.subTest(ns=ns):
                proc = self._py(gather, "clusters", root / "g.yaml", ns, cwd=root)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                docs = [d for d in yaml.safe_load_all((root / "g.yaml").read_text()) if isinstance(d, dict)]
                self.assertEqual([(d["metadata"]["name"], d["metadata"]["namespace"]) for d in docs], [("wide", ns)])
        code = self._splice("stage5-platform.sh", self.SVC, "\nPY\n"); (root / "catalog").mkdir()
        _write(root / "clusters" / "platform" / "monitoring" / "only.yaml", yaml.safe_dump({"apiVersion": "v1", "kind": "Service", "metadata": {"name": "only-in-monitoring"}, "spec": {"ports": [{"port": 80}]}}))
        self._app(root, "clusters/platform/sandbox-only", "sandbox", "sandbox-only")
        _write(root / "clusters" / "platform" / "sandbox-only" / "svc.yaml", yaml.safe_dump({"apiVersion": "v1", "kind": "Service", "metadata": {"name": "only-in-sandbox"}, "spec": {"ports": [{"port": 80}]}}))
        proc = self._py(code, ".", cwd=root)
        # the consumer is applied into BOTH namespaces: each URL resolves in one and not the other
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("names Service only-in-monitoring.sandbox, which this repo neither declares", proc.stdout)
        self.assertIn("names Service only-in-sandbox.monitoring, which this repo neither declares", proc.stdout)
        self.assertEqual(proc.stdout.count("neither declares"), 2, proc.stdout)

    def test_a_default_deny_selector_constraining_another_key_isolates_nothing_provable(self):
        """A3: the monitoring audit read only the app label's own terms; matchLabels x=yes
        plus DoesNotExist x selects no pod, yet counted as isolation."""
        code = self._splice("stage5-platform.sh", "WOUT=$(python3 - \"$NETPOL\" <<'PY'\n", "\nPY\n")
        src = ROOT / "template" / "provisioning" / "clusters" / "platform" / "config" / "monitoring" / "netpol.yaml"
        docs = [d for d in yaml.safe_load_all(src.read_text()) if isinstance(d, dict)]
        root = self._root()
        def run(mut):
            d2 = copy.deepcopy(docs); am = next(d for d in d2 if (d.get("spec") or {}).get("podSelector", {}).get("matchLabels", {}).get("app.kubernetes.io/name") == "alertmanager")
            mut(am["spec"]["podSelector"]); _write(root / "f.yaml", yaml.safe_dump_all(d2)); return self._py(code, root / "f.yaml")
        self.assertEqual(run(lambda p: None).returncode, 0)
        for name, mut in (("extra matchLabels key", lambda p: p["matchLabels"].update({"review.example/blocked": "yes"})),
                          ("contradicted", lambda p: (p["matchLabels"].update({"review.example/blocked": "yes"}), p.update({"matchExpressions": [{"key": "review.example/blocked", "operator": "DoesNotExist"}]}))),
                          ("expression on another key", lambda p: p.update({"matchExpressions": [{"key": "tier", "operator": "Exists"}]}))):
            with self.subTest(case=name):
                proc = run(mut); self.assertNotEqual(proc.returncode, 0); self.assertIn("no policy selects alertmanager for INGRESS", proc.stdout)

    def test_a_conflicting_matchlabels_value_is_not_rescued_by_an_in_expression(self):
        """A4: matchLabels name=other fell through to the In [intake] check and counted."""
        code = self._splice("stage5-platform.sh", self.WIOUT, "\nPY\n")
        src = ROOT / "template" / "provisioning" / "clusters" / "platform" / "config" / "orbit-intake" / "netpol.yaml"
        docs = [d for d in yaml.safe_load_all(src.read_text().replace("{{", "").replace("}}", "")) if isinstance(d, dict)]
        pol = next(d for d in docs if d.get("kind") == "NetworkPolicy")
        pol["spec"]["podSelector"]["matchLabels"]["app.kubernetes.io/name"] = "other"
        pol["spec"]["podSelector"]["matchExpressions"] = [{"key": "app.kubernetes.io/name", "operator": "In", "values": ["intake"]}]
        root = self._root(); _write(root / "f.yaml", yaml.safe_dump_all(docs))
        proc = self._py(code, root / "f.yaml")
        self.assertNotEqual(proc.returncode, 0); self.assertIn("does not pin its target to the intake pod", proc.stdout)

    def test_a_chain_entry_naming_another_namespace_is_refused(self):
        """A5: a chain entry with `namespace: traefik` resolved to the local middleware of
        the same name; the deployed traefik (allowCrossNamespace: false) refuses it."""
        code = self._splice("stage5-platform.sh", "MOUT=$(python3 - \"$IDIR/ingress.yaml\" <<'PY'\n", "\nPY\n")
        src = ROOT / "template" / "provisioning" / "clusters" / "platform" / "config" / "orbit-intake" / "ingress.yaml"
        docs = [d for d in yaml.safe_load_all(src.read_text().replace("{{", "").replace("}}", "")) if isinstance(d, dict)]
        root = self._root(); _write(root / "ok.yaml", yaml.safe_dump_all(docs))
        self.assertEqual(self._py(code, root / "ok.yaml").returncode, 0, self._py(code, root / "ok.yaml").stdout)
        chain = next(d for d in docs if (d.get("spec") or {}).get("chain"))
        chain["spec"]["chain"]["middlewares"][0]["namespace"] = "traefik"
        _write(root / "bad.yaml", yaml.safe_dump_all(docs))
        proc = self._py(code, root / "bad.yaml")
        self.assertNotEqual(proc.returncode, 0); self.assertIn("chain entry #0 references a middleware in another namespace", proc.stdout); self.assertNotIn("traefik'", proc.stdout)

    # ---- breaker -------------------------------------------------------------------
    def test_a_real_le_certificate_is_one_that_verifies(self):
        """B2: a self-signed certificate whose issuer line said Let's Encrypt passed."""
        s2 = self._stage("stage2.sh")
        block = s2[s2.index("LEAFF=$(mktemp); CHAINF=$(mktemp)"):s2.index('rm -f "$LEAFF" "$CHAINF"') + len('rm -f "$LEAFF" "$CHAINF"')]
        root = self._root()
        for verdict, want in (("Verify return code: 0 (ok)", "ok    unrouted host served a real LE cert that VERIFIES"),
                              ("Verify return code: 18 (self-signed certificate)", "FAIL  unrouted host served a certificate that names Let's Encrypt as issuer but does NOT verify"),
                              ("Verify return code: 62 (hostname mismatch)", "does NOT verify"), ("", "does NOT verify")):
            with self.subTest(verdict=verdict[:24]):
                env = self._stub(root, "openssl", "case \"$1\" in s_client) cat >/dev/null; printf -- '-----BEGIN CERTIFICATE-----\\nZmFrZQ==\\n-----END CERTIFICATE-----\\n%s\\n' '" + verdict + "';; x509) cat >/dev/null; echo \"issuer=C = US, O = Let's Encrypt, CN = R3\";; esac")
                proc = self._bash(self.OKBAD + "IP=203.0.113.5; ORBIT_DOMAIN=fixture.org\n" + block + "\n", env=env)
                self.assertIn(want, proc.stdout)
        self.assertIn('-verify_hostname "probe.$ORBIT_DOMAIN"', s2)

    def test_issuer_readiness_is_the_ready_condition(self):
        """B3: conditions[0] was read; an ACMEAccountRegistered=True ahead of Ready=False passed."""
        s2 = self._stage("stage2.sh")
        self.assertNotIn("conditions[0]", s2)
        self.assertIn('jsonpath="{.status.conditions[?(@.type==\\"Ready\\")].status}"', s2)

    def test_a_service_defined_twice_is_a_conflict_not_a_union(self):
        """B5: two applied definitions of one Service had their ports unioned."""
        code = self._splice("stage5-platform.sh", self.SVC, "\nPY\n")
        root = self._root(); (root / "catalog").mkdir(); _dirapp(root)
        _write(root / "clusters" / "a.yaml", yaml.safe_dump_all([
            {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "api", "namespace": "monitoring"}, "spec": {"ports": [{"port": 8080}]}},
            {"kind": "ConfigMap", "metadata": {"name": "c", "namespace": "monitoring"}, "data": {"u": "http://api.monitoring.svc:8080/"}}]))
        self.assertEqual(self._py(code, ".", cwd=root).returncode, 0)
        _write(root / "clusters" / "b.yaml", yaml.safe_dump({"apiVersion": "v1", "kind": "Service", "metadata": {"name": "api", "namespace": "monitoring"}, "spec": {"ports": [{"port": 9090}]}}))
        proc = self._py(code, ".", cwd=root)
        self.assertNotEqual(proc.returncode, 0); self.assertIn("Service api.monitoring is defined twice (clusters/", proc.stdout)
        line = next(l for l in proc.stdout.splitlines() if "defined twice" in l); self.assertIn("clusters/a.yaml", line); self.assertIn("clusters/b.yaml", line)
        self.assertIn("the cluster holds ONE of them, not the union of their ports", line)

    def test_a_dashboard_file_name_reaches_argv_and_rows_only_in_shape(self):
        """B6: a dashboard file named with `=` and a planted token was passed to python
        and printed in the row."""
        s3 = self._stage("stage3.sh")
        block = s3[s3.index("DASHODD=0\n"):s3.index('    || bad "broken generated dashboard(s):$DASHBAD"\nfi\n') + len('    || bad "broken generated dashboard(s):$DASHBAD"\nfi\n')]
        root = self._root(); (root / "clusters" / "apps").mkdir(parents=True)
        _write(root / "clusters" / "apps" / "dashboard-PLANTED_PASSWORD=fixture-value.yaml", "{}\n")
        _write(root / "clusters" / "apps" / "dashboard-good.yaml", yaml.safe_dump({"kind": "ConfigMap", "metadata": {"labels": {"grafana_dashboard": "1"}},
               "data": {"g.json": json.dumps({"uid": "orbit-red-good", "panels": [1]})}}))
        proc = self._bash(self.OKBAD + "DASHCOUNT=0; DASHBAD=\"\"\n" + block, cwd=root, trace=True)
        self.assertIn("FAIL  1 dashboard file(s) under clusters/apps have a name not of a file-name shape", proc.stdout)
        self.assertIn("ok    all 1 generated dashboards parse", proc.stdout); self.assertNotIn("PLANTED", proc.stdout)
        self.assertFalse([l for l in proc.stderr.splitlines() if "python3" in l and "PLANTED" in l], "the odd name reached python's argv")

    def test_a_domain_whose_derived_names_exceed_the_object_name_limit_is_refused(self):
        """B7: a 245-character primary domain rendered a 258-character Secret name."""
        long = ".".join(["a" * 63] * 3 + ["b" * 53]); self.assertEqual(len(long), 245)
        cfg = copy.deepcopy(self.CFG); cfg["domains"]["primary"] = long
        proc = self._load(cfg); self.assertNotIn("rc=0", proc.stdout); self.assertIn("longer than 240 characters", proc.stderr)
        proc = self._load(self.CFG, {"ORBIT_DOMAIN": long}); self.assertNotIn("rc=0", proc.stdout)
        ok = ".".join(["a" * 63] * 3 + ["b" * 48]); self.assertEqual(len(ok), 240)
        proc = self._load(self.CFG, {"ORBIT_DOMAIN": ok}); self.assertIn("rc=0 [" + ok + "]", proc.stdout)


class Round18Test(unittest.TestCase):
    """Area-1 sweep round 18: each row pins one repair."""

    def _stage(self, name):
        return (ROOT / "scripts" / "verify" / name).read_text()

    def _splice(self, stage, start, end):
        text = self._stage(stage)
        i = text.index(start) + len(start)
        return text[i:text.index(end, i)]

    def _root(self):
        root = pathlib.Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        return root

    def _bash(self, script, cwd=None, env=None, trace=False):
        return subprocess.run(["bash"] + (["-x"] if trace else []) + ["-c", script], cwd=cwd, capture_output=True,
                              text=True, timeout=120, env={**os.environ, **(env or {})})

    def _py(self, code, *args, cwd=None):
        return subprocess.run([sys.executable, "-c", code] + [str(a) for a in args], cwd=cwd, capture_output=True, text=True, timeout=60)

    def _stub(self, root, name, body):
        _write(root / "bin" / name, "#!/bin/sh\nprintf '%s\\n' \"$@\" >> \"$KLOG\"\n" + body + "\n", executable=True)
        return {"PATH": str(root / "bin") + os.pathsep + os.environ["PATH"], "KLOG": str(root / "stub.log")}

    OKBAD = "ok(){ echo \"  ok    $1\"; }; bad(){ echo \"  FAIL  $1\"; }\n"
    GATHER = "gather_netpols(){ python3 - clusters \"$2\" \"$1\" <<'PY'\n"
    SVC = "SVCPORT=$(python3 - . <<'PY'\n"

    def _pair(self, root, rel, ns, extra):
        app = {"apiVersion": "argoproj.io/v1alpha1", "kind": "Application", "metadata": {"name": pathlib.Path(rel).stem, "namespace": "argocd"},
               "spec": {"source": {"chart": "service", "repoURL": "https://charts.example.org", "targetRevision": "1"}, "destination": {"namespace": ns}}}
        _write(root / rel, yaml.safe_dump_all([app] + extra))

    def _gather(self, root, ns):
        proc = self._py(self._splice("stage5-platform.sh", self.GATHER, "\nPY\n"), "clusters", root / "g.yaml", ns, cwd=root)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return sorted((d["metadata"]["name"], d["metadata"]["namespace"]) for d in yaml.safe_load_all((root / "g.yaml").read_text()) if isinstance(d, dict))

    # ---- auditor -------------------------------------------------------------------
    def test_every_credential_bearing_curl_disables_the_default_config_first(self):
        """A1: curl reads ~/.curlrc before `-K -`; a trace-ascii there would log the
        Authorization header. `-q` is the first option on every credential-bearing call."""
        seen = 0
        for stage in ("stage3.sh", "stage5-platform.sh"):
            for line in self._stage(stage).splitlines():
                if "-K -" in line and "curl " in line and not line.lstrip().startswith("#"):
                    seen += 1
                    self.assertIn("curl -q ", line, "%s: a credential-bearing curl without -q first: %s" % (stage, line.strip()[:80]))
        self.assertGreaterEqual(seen, 4)

    def test_a_pair_files_companions_are_applied_into_its_destination(self):
        """A2: a NetworkPolicy beside the Application in clusters/orbit/<p>-<e>.yaml is
        applied by orbitd's renderer into that Application's destination; the audit
        gathered directory Applications only, so a widening policy there was unseen."""
        root = self._root(); (root / "clusters").mkdir()
        pol = {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": {"name": "wide"}, "spec": {"podSelector": {}, "ingress": [{}]}}
        self._pair(root, "clusters/orbit/x-prod.yaml", "orbit-agents-prod", [pol])
        self.assertEqual(self._gather(root, "orbit-agents-prod"), [("wide", "orbit-agents-prod")])
        self.assertEqual(self._gather(root, "monitoring"), [])
        # a platform Application file is NOT a pair file: its companions are nobody's
        self._pair(root, "clusters/platform/apps/foo.yaml", "orbit-agents-prod", [dict(pol, metadata={"name": "platform-side"})])
        self.assertEqual(self._gather(root, "orbit-agents-prod"), [("wide", "orbit-agents-prod")])
        # the tenant tree: a namespaces.yaml document whose namespace is a destination
        _write(root / "clusters" / "apps" / "namespaces.yaml", yaml.safe_dump_all([
            dict(pol, metadata={"name": "tenant-side", "namespace": "orbit-agents-prod"}),
            dict(pol, metadata={"name": "nobodys", "namespace": "elsewhere"})]))
        self.assertEqual(self._gather(root, "orbit-agents-prod"), [("tenant-side", "orbit-agents-prod"), ("wide", "orbit-agents-prod")])
        self.assertEqual(self._gather(root, "elsewhere"), [])
        # the Service index: a companion Service is defined in the destination
        (root / "catalog").mkdir()
        self._pair(root, "clusters/orbit/y-prod.yaml", "sandbox", [
            {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "svc"}, "spec": {"ports": [{"port": 80}]}},
            {"kind": "ConfigMap", "metadata": {"name": "c"}, "data": {"u": "http://svc:80/"}}])
        proc = self._py(self._splice("stage5-platform.sh", self.SVC, "\nPY\n"), ".", cwd=root)
        self.assertEqual(proc.returncode, 0, proc.stdout)

    # ---- breaker -------------------------------------------------------------------
    def test_every_single_quoted_program_in_the_stages_compiles(self):
        """B1: an apostrophe in a comment of a single-quoted `python3 -c '...'` program
        ended the shell quotation; the program failed to parse and the resource-policy
        row went red on every cluster. Bash ends a single-quoted word at the FIRST quote."""
        found = 0
        for stage in sorted((ROOT / "scripts" / "verify").glob("stage*.sh")):
            for m in re.finditer(r"python3 -c '\n(.*?)'", stage.read_text(), re.S):
                found += 1
                try: compile(m.group(1), stage.name, "exec")
                except SyntaxError as exc: self.fail("%s: a single-quoted program does not parse as bash hands it to python (line %s: %s)" % (stage.name, exc.lineno, exc.msg))
        self.assertGreaterEqual(found, 3)
        # and the resource-policy program RUNS over a pod list
        s3 = self._stage("stage3.sh")
        block = s3[s3.index("BADRES=$(kubectl get pods"):s3.index('  bad "policy violated by: $BADRES"\nfi\n') + len('  bad "policy violated by: $BADRES"\nfi\n')]
        root = self._root()
        pods = {"items": [{"metadata": {"namespace": "a", "name": "p"}, "spec": {"containers": [{"name": "c", "resources": {"limits": {"cpu": "1", "memory": "1Gi"}, "requests": {"memory": "1Gi"}}}]}}]}
        env = self._stub(root, "kubectl", "cat <<'J'\n" + json.dumps(pods) + "\nJ")
        proc = self._bash(self.OKBAD + block, env=env)
        self.assertIn("FAIL  policy violated by: a/p/c(cpu-limit)", proc.stdout)

    def test_a_backup_timestamp_is_dated_and_shown_only_in_timestamp_shape(self):
        """B2: the newest completed backup's timestamp went to `date -d` as an argument
        and into the row verbatim."""
        s3 = self._stage("stage3.sh")
        block = s3[s3.index("    BKF=$(mktemp)"):]
        block = block[:block.index("\n    fi\n") + len("\n    fi\n")]
        root = self._root()
        env = self._stub(root, "kubectl", "printf 'completed FIXTURE_BACKUP_CREDENTIAL alpha-db\\ncompleted 2026-01-01T00:00:00Z alpha-db\\nfailed 2026-09-13T00:00:00Z alpha-db\\n'")
        proc = self._bash(self.OKBAD + "p=alpha\n" + block, env=env, trace=True)
        self.assertIn("FAIL  alpha-db: no completed base backup in 26h (newest: 2026-01-01T00:00:00Z, plus 1 completed entry(ies) whose timestamp is not of a timestamp shape (not repeated))", proc.stdout)
        self.assertNotIn("FIXTURE", proc.stdout + proc.stderr)
        recent = subprocess.run(["date", "-u", "-d", "-1 hour", "+%Y-%m-%dT%H:%M:%SZ"], capture_output=True, text=True).stdout.strip()
        env = self._stub(root, "kubectl", "printf 'completed %s alpha-db\\n'" % recent)
        proc = self._bash(self.OKBAD + "p=alpha\n" + block, env=env)
        self.assertIn("ok    alpha-db: newest completed base backup is 1h old (<26h)", proc.stdout)

    def test_the_door_rows_show_ingress_fields_only_in_shape(self):
        """B3: an Ingress host, backend name and pathType were repeated verbatim in the
        FAIL row (%r) — cluster text."""
        text = self._stage("stage3.sh")
        start = text.index("        for p in ((rule.get(\"http\") or {}).get(\"paths\") or []):")
        end = text.index("if not items and wired:")
        helpers = text[text.index("NAME = re.compile(r\"[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?\")\nHOST = "):text.index("for ing in items:\n    name = shaped(")]
        pre = ("import re\nimport shown as display\n" + helpers + "bad = []\nINTAKE_SVC = 'orbit-agents-intake'\nname = 'door'\n"
               "host = shaped('FIXTURE_HOST_CREDENTIAL with spaces', HOST, 'host')\n"
               "rule = {'http': {'paths': ["
               "{'path': '/x', 'pathType': 'Prefix', 'backend': {'service': {'name': 'Bearer FIXTURE_SVC_CREDENTIAL'}}},"
               "{'path': '/x', 'pathType': 'Bearer FIXTURE_TYPE', 'backend': {'service': {'name': 'orbit-agents-intake'}}}]}}\n"
               "if True:\n    if True:\n")
        proc = subprocess.run([sys.executable, "-c", pre + text[start:end] + "\nprint('\\n'.join(bad))"], capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("door publishes a path rule on (a host not of a host shape, or of a credential's shape) -> Service (a name not of a name shape, or of a credential's shape), which is not the intake orbit-agents-intake", proc.stdout)
        self.assertIn("(pathType (a pathType not of a pathType shape, or of a credential's shape))", proc.stdout)
        self.assertNotIn("FIXTURE", proc.stdout + proc.stderr)
        self.assertNotIn("%r", text[text.index("wired = os.environ.get(\"INTAKEWIRED\""):end])

    def test_a_third_party_service_the_tree_also_defines_is_a_conflict(self):
        """B5: the hand table of chart-installed Services was unioned with a definition
        the tree applies; blackbox-exporter on 9116 plus the table's 9115 certified
        every Probe dispatched through 9115."""
        code = self._splice("stage5-platform.sh", self.SVC, "\nPY\n")
        root = self._root(); (root / "catalog").mkdir(); _dirapp(root)
        _write(root / "clusters" / "bb.yaml", yaml.safe_dump_all([
            {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "blackbox-exporter", "namespace": "monitoring"}, "spec": {"ports": [{"port": 9116}]}},
            {"kind": "ConfigMap", "metadata": {"name": "c", "namespace": "monitoring"}, "data": {"u": "http://blackbox-exporter.monitoring.svc:9115/probe"}}]))
        proc = self._py(code, ".", cwd=root)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("Service blackbox-exporter.monitoring is declared by hand as third-party AND defined by clusters/bb.yaml", proc.stdout)
        self.assertIn("dials port 9115, which Service blackbox-exporter.monitoring does not declare on TCP (it declares ['9116/TCP'])", proc.stdout)

    def test_a_service_port_is_an_integer_in_range(self):
        """B6: int(8080.9) is 8080 — a fractional, out-of-range or boolean port was
        indexed as a real one."""
        code = self._splice("stage5-platform.sh", self.SVC, "\nPY\n")
        for port in (8080.9, 70000, 0, True, "80a"):
            with self.subTest(port=port):
                root = self._root(); (root / "catalog").mkdir(); _dirapp(root)
                _write(root / "clusters" / "s.yaml", yaml.safe_dump({"apiVersion": "v1", "kind": "Service", "metadata": {"name": "api", "namespace": "monitoring"}, "spec": {"ports": [{"port": port}]}}))
                proc = self._py(code, ".", cwd=root)
                self.assertNotEqual(proc.returncode, 0); self.assertIn("FAIL Service monitoring/api declares a port that is not an integer in 1..65535", proc.stdout)

    def test_a_file_name_reaches_an_endpoint_diagnostic_only_in_file_name_shape(self):
        """B7: the offending file's path was printed verbatim — a file name is tree text."""
        code = self._splice("stage5-platform.sh", self.SVC, "\nPY\n")
        root = self._root(); (root / "catalog").mkdir(); _dirapp(root)
        _write(root / "clusters" / "platform" / "config" / "x" / "Bearer PLANTED_FILENAME_CREDENTIAL.yaml",
               yaml.safe_dump({"kind": "ConfigMap", "metadata": {"name": "c", "namespace": "monitoring"}, "data": {"u": "http://nowhere.monitoring.svc:80/"}}))
        _write(root / "clusters" / "platform" / "config" / "x" / "Bearer PLANTED_SVC_FILE.yaml",
               yaml.safe_dump({"apiVersion": "v1", "kind": "Service", "metadata": {"name": "dup", "namespace": "monitoring"}, "spec": {"ports": [{"port": 80}]}}))
        _write(root / "clusters" / "dup.yaml",
               yaml.safe_dump({"apiVersion": "v1", "kind": "Service", "metadata": {"name": "dup", "namespace": "monitoring"}, "spec": {"ports": [{"port": 81}]}}))
        proc = self._py(code, ".", cwd=root)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("./clusters/platform/config/x/(a name not of a file-name shape, or of a credential's shape) (a decoded value, line 1 of it) http://nowhere.monitoring.svc:80 names Service nowhere.monitoring, which this repo neither declares", proc.stdout)
        self.assertIn("is defined twice (", proc.stdout); self.assertIn("(a name not of a file-name shape, or of a credential's shape)", proc.stdout.split("defined twice")[1][:120])
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)


class Round19Test(unittest.TestCase):
    """Area-1 sweep round 19: each row pins one repair."""

    def _stage(self, name):
        return (ROOT / "scripts" / "verify" / name).read_text()

    def _splice(self, stage, start, end):
        text = self._stage(stage)
        i = text.index(start) + len(start)
        return text[i:text.index(end, i)]

    def _root(self):
        root = pathlib.Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        return root

    def _py(self, code, *args, cwd=None):
        return subprocess.run([sys.executable, "-c", code] + [str(a) for a in args], cwd=cwd, capture_output=True, text=True, timeout=60)

    def test_applications_are_discovered_at_the_renderers_entrypoints_only(self):
        """A1: an Application file anywhere under clusters/ (a dormant audit-only
        directory, a nested document) extended the inventory; platform.py
        load_applications reads clusters/platform/apps/*.yaml only."""
        gather = self._splice("stage5-platform.sh", "gather_netpols(){ python3 - clusters \"$2\" \"$1\" <<'PY'\n", "\nPY\n")
        svc = self._splice("stage5-platform.sh", "SVCPORT=$(python3 - . <<'PY'\n", "\nPY\n")
        app = {"apiVersion": "argoproj.io/v1alpha1", "kind": "Application", "metadata": {"name": "audit-only", "namespace": "argocd"},
               "spec": {"source": {"path": "clusters/audit-only", "directory": {"recurse": True}}, "destination": {"namespace": "monitoring"}}}
        pol = {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": {"name": "dormant"}, "spec": {"podSelector": {}}}
        svcdoc = {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "dormant"}, "spec": {"ports": [{"port": 80}]}}
        for where in ("clusters/audit-only-app.yaml", "clusters/audit-only/nested-app.yaml", "clusters/platform/config/app.yaml"):
            with self.subTest(where=where):
                root = self._root(); (root / "catalog").mkdir()
                _write(root / where, yaml.safe_dump(app))
                _write(root / "clusters" / "audit-only" / "np.yaml", yaml.safe_dump_all([pol, svcdoc]))
                proc = self._py(gather, "clusters", root / "g.yaml", "monitoring", cwd=root)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertEqual([d for d in yaml.safe_load_all((root / "g.yaml").read_text()) if isinstance(d, dict)], [])
                proc = self._py(svc, ".", cwd=root)
                self.assertEqual(proc.returncode, 0, proc.stdout)
                third = len(re.findall(r'^    \("', self._splice("stage5-platform.sh", "THIRD_PARTY = {\n", "\n}\n"), re.M))
                self.assertIn("%d Service(s)" % third, proc.stdout, proc.stdout)
        root = self._root(); (root / "catalog").mkdir()
        _write(root / "clusters" / "platform" / "apps" / "audit.yaml", yaml.safe_dump(app))
        _write(root / "clusters" / "audit-only" / "np.yaml", yaml.safe_dump_all([pol, svcdoc]))
        proc = self._py(gather, "clusters", root / "g.yaml", "monitoring", cwd=root)
        self.assertEqual([d["metadata"]["name"] for d in yaml.safe_load_all((root / "g.yaml").read_text()) if isinstance(d, dict)], ["dormant"])
        proc = self._py(svc, ".", cwd=root); self.assertIn("%d Service(s)" % (third + 1), proc.stdout)

    def test_dashboard_file_names_reach_argv_and_rows_only_in_shape(self):
        """A3: `ls clusters/apps/dashboard-*.yaml` handed every name to python's argv and
        the shape row repeated it."""
        s5 = self._stage("stage5-platform.sh")
        self.assertNotIn("$(ls clusters/apps/dashboard-", s5)
        block = s5[s5.index("DASHLIST=(); DASHODD=0"):]
        block = block[:block.index('|| bad "dashboard shape: ${DSHAPE#FAIL }"\nfi\n') + len('|| bad "dashboard shape: ${DSHAPE#FAIL }"\nfi\n')]
        root = self._root(); (root / "clusters" / "apps").mkdir(parents=True)
        _write(root / "clusters" / "apps" / "dashboard-123:PLANTED_CREDENTIAL_19.yaml", "{}\n")
        _write(root / "clusters" / "apps" / "dashboard-good.yaml", yaml.safe_dump({"kind": "ConfigMap", "data": {"g.json": json.dumps({"uid": "orbit-red-good", "panels": [1]})}}))
        proc = subprocess.run(["bash", "-x", "-c", "ok(){ echo \"  ok    $1\"; }; bad(){ echo \"  FAIL  $1\"; }\n" + block],
                              cwd=root, capture_output=True, text=True, timeout=60)
        self.assertIn("FAIL  1 dashboard file(s) under clusters/apps have a name not of a file-name shape", proc.stdout)
        self.assertIn("ok    every rendered dashboard parses and carries a uid [OK 1 dashboard(s) across 1 file(s) parse and carry a uid]", proc.stdout)
        self.assertNotIn("PLANTED", proc.stdout); self.assertFalse([l for l in proc.stderr.splitlines() if "python3" in l and "PLANTED" in l], "the odd name reached argv")

    def test_an_unreadable_catalog_never_reads_as_zero_pairs_or_zero_agents(self):
        """A5: the inventory and agent enumerations read the catalog bare — a PyYAML
        ConstructorError repeated an unknown tag on stderr and the empty enumeration
        passed as 'every pair has an inventory (0)' / 'no agent declared'."""
        s3 = self._stage("stage3.sh")
        self.assertIn("print('catalog/projects.yaml could not be read (the parser message is not repeated)', file=sys.stderr); sys.exit(2)", s3)
        root = self._root(); (root / "catalog").mkdir()
        (root / "catalog" / "projects.yaml").write_text("projects: !Bearer/PLANTED_TAG_CREDENTIAL_19 []\n")
        env = self._stubk(root)
        inv = s3[s3.index("  PAIRS=$(python3 -c \""):s3.index('      || bad "declared pair(s) with no tenant inventory:$INVMISS — the daemon has not applied them, or pruned them"\n  fi\n') + len('      || bad "declared pair(s) with no tenant inventory:$INVMISS — the daemon has not applied them, or pruned them"\n  fi\n')]
        proc = subprocess.run(["bash", "-c", self.OKBAD + "INVMISS=\"\"; INVSEEN=0; INVODD=0\n" + inv], cwd=root, capture_output=True, text=True, timeout=60, env=env)
        self.assertIn("FAIL  catalog/projects.yaml could not be read (the parser's message is not repeated) — the tenant inventory rows are UNCHECKED", proc.stdout)
        self.assertNotIn("every declared pair has a tenant inventory", proc.stdout); self.assertNotIn("PLANTED", proc.stdout + proc.stderr)
        ag = s3[s3.index("AGENTNAMES=$(python3 -c \""):s3.index("    if isinstance(a, dict) and a.get('enabled', True) and a.get('name'):")]
        proc = subprocess.run(["bash", "-c", ag + "    pass\n\" 2>/dev/null)\necho \"[$AGENTNAMES]\""], cwd=root, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.stdout.strip(), "[UNREADABLE]", proc.stdout + proc.stderr); self.assertNotIn("PLANTED", proc.stdout + proc.stderr)
        self.assertIn('elif [ "$AGENTNAMES" = UNREADABLE ]; then\n  bad "catalog/projects.yaml could not be read', s3)
        prot = s3[s3.index('PROTHOSTS=$(python3 -c "\n') + len('PROTHOSTS=$(python3 -c "\n'):s3.index('" 2>"$PROTBAD"); PROTRC=$?')].replace('\\"', '"')
        proc = subprocess.run([sys.executable, "-c", prot], cwd=root, capture_output=True, text=True, timeout=60, env={**os.environ, "ORBIT_DEV_DOMAIN": "dev.fixture.org"})
        self.assertEqual(proc.returncode, 2); self.assertEqual(proc.stderr.strip(), "catalog/projects.yaml could not be read (the parser message is not repeated)")

    def _stubk(self, root):
        _write(root / "bin" / "kubectl", "#!/bin/sh\nexit 1\n", executable=True)
        return {**os.environ, "PATH": str(root / "bin") + os.pathsep + os.environ["PATH"]}

    OKBAD = "ok(){ echo \"  ok    $1\"; }; bad(){ echo \"  FAIL  $1\"; }\n"

    def test_a_probe_with_no_url_is_reported_with_its_file_in_file_name_shape(self):
        """B2: the Probe-without-url finding repeated the file path verbatim."""
        code = self._splice("stage5-platform.sh", "SVCPORT=$(python3 - . <<'PY'\n", "\nPY\n")
        root = self._root(); (root / "catalog").mkdir(); _dirapp(root)
        _write(root / "clusters" / "platform" / "monitoring" / "Bearer PLANTED_PATH_CREDENTIAL_19.yaml",
               yaml.safe_dump({"apiVersion": "monitoring.coreos.com/v1", "kind": "Probe", "metadata": {"name": "extra", "namespace": "monitoring"}, "spec": {"prober": {}}}))
        proc = self._py(code, ".", cwd=root)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("./clusters/platform/monitoring/(a name not of a file-name shape, or of a credential's shape) Probe extra declares no spec.prober.url", proc.stdout)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_the_alloy_series_count_reaches_the_shell_only_validated(self):
        """B3: `promq | first` handed the raw sample to a shell test (traced) — promval
        answers a number, ABSENT or QUERYERR and nothing else."""
        s5 = self._stage("stage5-platform.sh")
        block = s5[s5.index("    ALLOYN=$(promval 'count({__name__=~\"alloy_.*\"})')"):]
        block = block[:block.index("    esac\n") + len("    esac\n")]
        self.assertNotIn("| first", block)
        for out, want in (("QUERYERR", "FAIL  could not answer the alloy_* series query"), ("ABSENT", "FAIL  no alloy_* series"),
                          ("0", "FAIL  no alloy_* series"), ("12", "ok    alloy_* metrics are landing (12 series")):
            with self.subTest(out=out):
                proc = subprocess.run(["bash", "-c", "ok(){ echo \"  ok    $1\"; }; bad(){ echo \"  FAIL  $1\"; }\npromval(){ echo \"$PV\"; }\n" + block],
                                      capture_output=True, text=True, timeout=60, env={**os.environ, "PV": out})
                self.assertIn(want, proc.stdout)


class Round20Test(unittest.TestCase):
    """Area-1 sweep round 20: each row pins one repair."""

    def _stage(self, name):
        return (ROOT / "scripts" / "verify" / name).read_text()

    def _splice(self, stage, start, end):
        text = self._stage(stage)
        i = text.index(start) + len(start)
        return text[i:text.index(end, i)]

    def _root(self):
        root = pathlib.Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        return root

    def _py(self, code, *args, cwd=None):
        return subprocess.run([sys.executable, "-c", code] + [str(a) for a in args], cwd=cwd, capture_output=True, text=True, timeout=120)

    GATHER = "gather_netpols(){ python3 - clusters \"$2\" \"$1\" <<'PY'\n"
    SVC = "SVCPORT=$(python3 - . <<'PY'\n"
    INTAKE = ROOT / "template" / "provisioning" / "clusters" / "platform" / "config" / "orbit-intake"

    def _intake_docs(self, name):
        return [d for d in yaml.safe_load_all((self.INTAKE / name).read_text().replace("{{", "").replace("}}", "")) if isinstance(d, dict)]

    def _app(self, root, rel, name, path, ns="monitoring"):
        _write(root / rel, yaml.safe_dump({"apiVersion": "argoproj.io/v1alpha1", "kind": "Application", "metadata": {"name": name, "namespace": "argocd"},
                                           "spec": {"source": {"path": path, "directory": {"recurse": True}}, "destination": {"namespace": ns}}}))

    # ---- auditor -------------------------------------------------------------------
    def test_the_gated_tree_rows_and_init_show_a_staged_file_in_file_name_shape(self):
        """A1: a staged file's name (provisioning/clusters/<name>) was repeated verbatim in the
        MISSING row and in init's directory refusal."""
        code = self._splice("stage5-platform.sh", "GATED=$(python3 - \"$GATE_BUCKET\" \"$GATE_INTAKE\" \"${ORBIT_INSTANCE_CONFIG:-instance/config.yaml}\" <<'PY'\n", "\nPY\n")
        root = self._root(); (root / "scripts").mkdir(); shutil.copy(ROOT / "scripts" / "instance-init.py", root / "scripts" / "instance-init.py")
        _write(root / "provisioning" / "clusters" / "Bearer PLANTED_CANARY_CREDENTIAL.yaml", "{}\n")
        # the row reads the feature gates through the scaffolder, which needs the registry
        # vendored beside the staged tree and a config to read them from
        shutil.copy(ROOT / "template" / "provisioning" / "features.yaml", root / "provisioning" / "features.yaml")
        _write(root / "instance" / "config.yaml", "{}\n")
        proc = self._py(code, "0", "0", "instance/config.yaml", cwd=root)
        self.assertNotEqual(proc.returncode, 0); self.assertIn("clusters/(a name not of a file-name shape, or of a credential's shape) is MISSING", proc.stdout)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)
        s5 = self._stage("stage5-platform.sh"); self.assertIn("shown = shown_rel(rel)", s5)
        init = (ROOT / "scripts" / "instance-init.py").read_text()
        self.assertIn('sys.exit(f"clusters/{shown_rel(CLUSTERS / rel)} is a directory', init)
        self.assertIn('print(f"removed {shown_rel(CLUSTERS / rel)}', init)

    def test_applications_are_one_per_name_the_last_in_path_order(self):
        """A3: two Applications of one name were both gathered; the renderer keeps the LAST in
        path order and never applies the other's source."""
        gather = self._splice("stage5-platform.sh", self.GATHER, "\nPY\n"); svc = self._splice("stage5-platform.sh", self.SVC, "\nPY\n")
        root = self._root(); (root / "catalog").mkdir()
        self._app(root, "clusters/platform/apps/platform-config.yaml", "platform-config", "clusters/platform/config")
        self._app(root, "clusters/platform/apps/zzz-platform-config.yaml", "platform-config", "clusters/platform/other")
        _write(root / "clusters" / "platform" / "config" / "np.yaml", yaml.safe_dump_all([
            {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": {"name": "old"}, "spec": {"podSelector": {}}},
            {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "old"}, "spec": {"ports": [{"port": 80}]}}]))
        _write(root / "clusters" / "platform" / "other" / "cm.yaml", yaml.safe_dump({"kind": "ConfigMap", "metadata": {"name": "c"}, "data": {"u": "http://old.monitoring.svc:80/"}}))
        proc = self._py(gather, "clusters", root / "g.yaml", "monitoring", cwd=root)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual([d for d in yaml.safe_load_all((root / "g.yaml").read_text()) if isinstance(d, dict)], [])
        proc = self._py(svc, ".", cwd=root)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("Application platform-config is declared twice (clusters/platform/apps/platform-config.yaml and clusters/platform/apps/zzz-platform-config.yaml) — the renderer applies the LAST in path order", proc.stdout)
        self.assertIn("names Service old.monitoring, which this repo neither declares", proc.stdout)

    def test_a_namespace_less_probe_lives_in_its_applications_destination(self):
        """A5: a Probe with no metadata.namespace under the monitoring directory Application
        resolved its bare `blackbox-exporter:9115` in no namespace — RED for a URL the
        renderer places in monitoring."""
        svc = self._splice("stage5-platform.sh", self.SVC, "\nPY\n")
        root = self._root(); (root / "catalog").mkdir(); _dirapp(root, "monitoring")
        _write(root / "clusters" / "probe.yaml", yaml.safe_dump({"apiVersion": "monitoring.coreos.com/v1", "kind": "Probe", "metadata": {"name": "p"}, "spec": {"prober": {"url": "blackbox-exporter:9115"}}}))
        proc = self._py(svc, ".", cwd=root)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        root = self._root(); (root / "catalog").mkdir(); _dirapp(root, "elsewhere")
        _write(root / "clusters" / "probe.yaml", yaml.safe_dump({"apiVersion": "monitoring.coreos.com/v1", "kind": "Probe", "metadata": {"name": "p"}, "spec": {"prober": {"url": "blackbox-exporter:9115"}}}))
        proc = self._py(svc, ".", cwd=root)
        self.assertNotEqual(proc.returncode, 0); self.assertIn("blackbox-exporter.elsewhere", proc.stdout)

    # ---- breaker -------------------------------------------------------------------
    def test_an_application_file_name_reaches_the_render_loop_only_in_shape(self):
        """B1: `clusters/apps/app-token=PLANTED.yaml` was handed to python and printed in
        the fails-to-render row."""
        s3 = self._stage("stage3.sh")
        block = s3[s3.index("VALS=$(mktemp)\nfor f in clusters/apps/*-*.yaml clusters/orbit/*.yaml; do"):s3.index('rm -f "$VALS"\n') + len('rm -f "$VALS"\n')]
        row = next(l for l in s3.splitlines() if l.startswith('[ "$RENDERODD" -eq 0 ] || bad'))
        root = self._root(); (root / "clusters" / "apps").mkdir(parents=True)
        _write(root / "clusters" / "apps" / "app-token=PLANTED_FILE_CREDENTIAL.yaml", "kind: Application\nspec: broken\n")
        _write(root / "bin" / "helm", "#!/bin/sh\nexit 0\n", executable=True)
        proc = subprocess.run(["bash", "-x", "-c", "ok(){ echo \"  ok    $1\"; }; bad(){ echo \"  FAIL  $1\"; }\nRENDERFAIL=\"\"; RENDERED=0; ATTEMPTED=0; RENDERODD=0\n" + block + row + "\necho \"fail=[$RENDERFAIL]\""],
                              cwd=root, capture_output=True, text=True, timeout=60, env={**os.environ, "PATH": str(root / "bin") + os.pathsep + os.environ["PATH"]})
        self.assertIn("FAIL  1 Application file(s) under clusters/apps or clusters/orbit have a name not of a file-name shape", proc.stdout)
        self.assertIn("fail=[]", proc.stdout); self.assertNotIn("PLANTED", proc.stdout)
        self.assertFalse([l for l in proc.stderr.splitlines() if "python3" in l and "PLANTED" in l], "the odd name reached python's argv")

    def test_helm_failures_and_middleware_rows_show_names_in_shape(self):
        """B2: the helm-failure row repeated the Application's file name; the inFlightReq
        sourceCriterion rows repeated any middleware's name."""
        s5 = self._stage("stage5-platform.sh")
        self.assertIn("% (shown_path(path), out.returncode)); sys.exit(1)", s5)
        code = self._splice("stage5-platform.sh", "MOUT=$(python3 - \"$IDIR/ingress.yaml\" <<'PY'\n", "\nPY\n")
        docs = self._intake_docs("ingress.yaml")
        docs.append({"apiVersion": "traefik.io/v1alpha1", "kind": "Middleware", "metadata": {"name": "token=PLANTED_MIDDLEWARE_NAME", "namespace": "orbit-agents-prod"},
                     "spec": {"inFlightReq": {"amount": 10, "sourceCriterion": "PLANTED_SCALAR"}}})
        root = self._root(); _write(root / "i.yaml", yaml.safe_dump_all(docs))
        proc = self._py(code, root / "i.yaml")
        self.assertNotEqual(proc.returncode, 0); self.assertIn("(a middleware not of an object-name shape, or of a credential's shape) sets sourceCriterion to something that is not a map", proc.stdout)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_the_door_backend_port_is_the_intake_http_port(self):
        """B4: an Ingress backing orbit-agents-intake on a port name the Service does not
        carry passed — traefik answers 503 and the door is dead."""
        code = self._splice("stage5-platform.sh", "DOUT=$(python3 - \"$IDIR/ingress.yaml\" \"$IHOST\" <<'PY'\n", "\nPY\n")
        docs = self._intake_docs("ingress.yaml"); ing = next(d for d in docs if d.get("kind") == "Ingress")
        host = ing["spec"]["rules"][0]["host"]
        root = self._root(); _write(root / "ok.yaml", yaml.safe_dump_all(docs))
        proc = self._py(code, root / "ok.yaml", host); self.assertEqual(proc.returncode, 0, proc.stdout)
        ing["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"]["port"] = {"name": "no-such-port"}
        _write(root / "bad.yaml", yaml.safe_dump_all(docs))
        proc = self._py(code, root / "bad.yaml", host)
        self.assertNotEqual(proc.returncode, 0); self.assertIn("backs a Service port that is not the intake's http port", proc.stdout); self.assertNotIn("no-such-port", proc.stdout)

    def test_a_rate_limit_is_a_map_with_a_bounded_effective_rate(self):
        """B5: a scalar rateLimit was skipped, and average 50 per 1ns passed as a limit."""
        code = self._splice("stage5-platform.sh", "MOUT=$(python3 - \"$IDIR/ingress.yaml\" <<'PY'\n", "\nPY\n")
        base = self._intake_docs("ingress.yaml")
        def run(mut):
            docs = copy.deepcopy(base); rl = next(d for d in docs if (d.get("spec") or {}).get("rateLimit") is not None)
            mut(rl["spec"]); root = self._root(); _write(root / "i.yaml", yaml.safe_dump_all(docs)); return self._py(code, root / "i.yaml")
        self.assertEqual(run(lambda s: None).returncode, 0)
        self.assertEqual(run(lambda s: s["rateLimit"].update({"period": "100ms"})).returncode, 0)
        for name, mut, want in (("scalar", lambda s: s.update({"rateLimit": "PLANTED_SCALAR"}), "rateLimit is not a map"),
                                ("1ns", lambda s: s["rateLimit"].update({"period": "1ns"}), "rateLimit.period is not a duration between 100ms and 1m"),
                                ("bare number", lambda s: s["rateLimit"].update({"period": 1}), "rateLimit.period is not a duration"),
                                ("5m", lambda s: s["rateLimit"].update({"period": "5m"}), "rateLimit.period is not a duration between 100ms and 1m"),
                                ("10ms", lambda s: s["rateLimit"].update({"period": "10ms"}), "rateLimit.period is not a duration between 100ms and 1m"),
                                ("effective", lambda s: s["rateLimit"].update({"average": 10000, "period": "100ms"}), "an effective rate outside 1..10000 requests per second")):
            with self.subTest(case=name):
                proc = run(mut); self.assertNotEqual(proc.returncode, 0); self.assertIn(want, proc.stdout); self.assertNotIn("PLANTED", proc.stdout)

    def test_a_udp_port_entry_admits_nothing_on_tcp_9093(self):
        """B6: a policy admitting UDP 9093 from loki was read as a bare grant to TCP :9093."""
        code = self._splice("stage5-platform.sh", "WOUT=$(python3 - \"$NETPOL\" <<'PY'\n", "\nPY\n")
        src = ROOT / "template" / "provisioning" / "clusters" / "platform" / "config" / "monitoring" / "netpol.yaml"
        docs = [d for d in yaml.safe_load_all(src.read_text()) if isinstance(d, dict)]
        def run(proto):
            d2 = copy.deepcopy(docs)
            d2.append({"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": {"name": "udp-only", "namespace": "monitoring"},
                       "spec": {"podSelector": {"matchLabels": {"app.kubernetes.io/name": "alertmanager"}}, "policyTypes": ["Ingress"],
                                "ingress": [{"from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "loki"}}}], "ports": [{"protocol": proto, "port": 9093}]}]}})
            root = self._root(); _write(root / "f.yaml", yaml.safe_dump_all(d2)); return self._py(code, root / "f.yaml")
        self.assertEqual(run("UDP").returncode, 0, run("UDP").stdout)
        proc = run("TCP"); self.assertNotEqual(proc.returncode, 0); self.assertIn("bare namespace-wide loki grant", proc.stdout)


class Round21Test(unittest.TestCase):
    """Area-1 sweep round 21: each row pins one repair."""

    def _stage(self, name):
        return (ROOT / "scripts" / "verify" / name).read_text()

    def _splice(self, stage, start, end):
        text = self._stage(stage)
        i = text.index(start) + len(start)
        return text[i:text.index(end, i)]

    def _root(self):
        root = pathlib.Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        return root

    def _py(self, code, *args, cwd=None):
        return subprocess.run([sys.executable, "-c", code] + [str(a) for a in args], cwd=cwd, capture_output=True, text=True, timeout=120)

    def _bash(self, script, cwd=None, env=None, trace=False):
        return subprocess.run(["bash"] + (["-x"] if trace else []) + ["-c", script], cwd=cwd, capture_output=True,
                              text=True, timeout=120, env={**os.environ, **(env or {})})

    OKBAD = "ok(){ echo \"  ok    $1\"; }; bad(){ echo \"  FAIL  $1\"; }\n"
    GATHER = "gather_netpols(){ python3 - clusters \"$2\" \"$1\" <<'PY'\n"
    SVC = "SVCPORT=$(python3 - . <<'PY'\n"

    def _app(self, root, rel, name, path, ns="monitoring"):
        _write(root / rel, yaml.safe_dump({"apiVersion": "argoproj.io/v1alpha1", "kind": "Application", "metadata": {"name": name, "namespace": "argocd"},
                                           "spec": {"source": {"path": path, "directory": {"recurse": True}}, "destination": {"namespace": ns}}}))

    def _git(self, root, *args):
        return subprocess.run(["git", "-c", "user.name=f", "-c", "user.email=f@f", "-c", "commit.gpgsign=false"] + list(args), cwd=root, capture_output=True, text=True, timeout=120)

    # ---- breaker -------------------------------------------------------------------
    def test_the_committed_file_list_is_read_whole(self):
        """B1: `git ls-files | grep -q` closed the pipe at the first match and a git killed by
        SIGPIPE was never noticed; a late .env in a long listing went unseen. The listing is
        a private file, read whole, and a failed listing is UNCHECKED."""
        block = self._splice("stage4.sh", "# .env files are the failure mode sealed Secrets replace; there must be none, anywhere.\n", "\nesac\n") + "\nesac\n"
        self.assertNotIn("grep -q", "\n".join(l for l in block.splitlines() if not l.lstrip().startswith("#")))
        root = self._root(); self._git(root, "init", "-q")
        for i in range(3000): _write(root / ("f%04d.txt" % i), "x")
        # round 22 (B2): git QUOTES a name with a byte outside ASCII in its line form
        _write(root / "caf\u00e9" / ".env", "SECRET=PLANTED"); self._git(root, "add", "-A"); self._git(root, "commit", "-qm", "m")
        out = self._bash(self.OKBAD + block, cwd=root).stdout
        self.assertIn("FAIL  a .env file is committed", out); self.assertNotIn("PLANTED", out)
        self._git(root, "rm", "-q", "caf\u00e9/.env"); self._git(root, "commit", "-qm", "m2")
        self.assertIn("ok    no .env file is committed anywhere in the repo", self._bash(self.OKBAD + block, cwd=root).stdout)
        bare = self._root()
        self.assertIn("FAIL  the committed file list could not be read (git ls-files failed) — the .env check is UNCHECKED", self._bash(self.OKBAD + block, cwd=bare).stdout)

    def test_the_swap_count_is_a_private_file_read_only_in_shape(self):
        """B3: the swap row expanded ssh's raw output inside a traced comparison; a node that
        answered with anything but digits put that text in the trace. The output lands in
        a file and becomes a shell value only as a count."""
        text = self._stage("stage2.sh")
        block = text[text.index("SWAPF=$(mktemp)"):text.index("\n", text.index('check "swap is off"'))]
        root = self._root()
        _write(root / "bin" / "ssh", "#!/bin/sh\nprintf '%s\\n' \"$SSH_OUT\"\n", executable=True)
        for out, want in (("0", "ok"), ("1", "bad"), ("PLANTED_SWAP_TEXT $(id)", "bad"), ("", "bad")):
            with self.subTest(out=out):
                proc = self._bash("ok(){ echo ok; }; bad(){ echo bad; }; check(){ if eval \"$2\" >/dev/null 2>&1; then ok \"$1\"; else bad \"$1\"; fi; }\nIP=203.0.113.7\n" + block + "\n",
                                  env={"PATH": str(root / "bin") + os.pathsep + os.environ["PATH"], "ORBIT_SSH_KEY": "k", "SSH_OUT": out}, trace=True)
                self.assertEqual(proc.stdout.strip(), want, proc.stderr[-800:])
                self.assertNotIn("PLANTED", proc.stdout + proc.stderr)
                if want == "bad" and out not in ("1",): self.assertIn("SWAPN=unreadable", proc.stderr)

    # ---- auditor -------------------------------------------------------------------
    def test_the_held_line_never_leaves_the_process(self):
        """A1: the newest log line was sent to Loki as a `|=` filter through Grafana's
        proxy with --get, so a credential an app printed landed in the request URI (and
        in Traefik's access log). The query is the pod's stream selector over a ten-second
        window; the line is compared inside the parser, from a file."""
        text = self._stage("stage5-platform.sh")
        helper = text[text.index("  LRANGE_GRAFANA(){"):text.index("  KLAST(){")]
        held = text[text.index("  HELD(){"):text.index("  CTL_NS=traefik")]
        code = "\n".join(l for l in (helper + held).splitlines() if not l.lstrip().startswith("#"))
        self.assertNotIn("|=", code); self.assertIn('--data-urlencode "end=${3}000000000"', helper)
        for call in ('HELD $CTL_NS "$CTL_POD" "$TMP/ctl.needle")', 'HELD "$ns" "$SPOD" "$TMP/newest.needle")'):
            self.assertEqual(text.count(call), 2, call)
        hp = held[held.index("python3 -c '") + len("python3 -c '"):held.index("' \"$1\"")]
        root = self._root()
        _write(root / "needle", '1700000000\t"GET /x PLANTED_LINE_TOKEN \\"q\\" \\x00 \\\\"\n')
        proc = self._py(hp, "ns", 'po"d', root / "needle", root / "q")
        self.assertEqual(proc.returncode, 0, proc.stderr); self.assertEqual(proc.stdout.split(), ["1699999995", "1700000005"])
        self.assertEqual((root / "q").read_text(), '{namespace="ns",pod="po\\"d"}')
        _write(root / "odd", '17x\t"a"\n'); self.assertEqual(self._py(hp, "ns", "pod", root / "odd", root / "q2").returncode, 3)
        _write(root / "odd2", '1700000000\tGET /x\n'); self.assertEqual(self._py(hp, "ns", "pod", root / "odd2", root / "q3").returncode, 3)
        py = helper[helper.rindex("python3 -c '") + len("python3 -c '"):helper.rindex("' \"$4\"; }")]
        held_line = "xx GET /x PLANTED_LINE_TOKEN \"q\" \x00 \\ tail"
        for body, want in ((json.dumps({"status": "success", "data": {"result": [{"values": [["1", "other"], ["2", held_line]]}]}}), "1"),
                           (json.dumps({"status": "success", "data": {"result": [{"values": [["1", "other"]]}]}}), "0"),
                           (json.dumps({"status": "success", "data": {"result": []}}), "0"),
                           ('{"status":"success","data":null}', "QUERYERR")):
            with self.subTest(want=want):
                proc = subprocess.run([sys.executable, "-c", py, str(root / "needle")], input=body, capture_output=True, text=True, timeout=30)
                self.assertEqual(proc.stdout.strip(), want, proc.stderr)

    def test_a_source_path_is_compared_in_the_renderers_normal_form(self):
        """A2: `./clusters/platform/config/extra` never matched a file's `clusters/...`
        relative path, so the policies (and Services) under it were not audited while the
        renderer (pathlib) applied them. The path is normalised the same way now; a path
        climbing out of the tree applies nothing here."""
        gather = self._splice("stage5-platform.sh", self.GATHER, "\nPY\n"); svc = self._splice("stage5-platform.sh", self.SVC, "\nPY\n")
        root = self._root(); (root / "catalog").mkdir()
        self._app(root, "clusters/platform/apps/extra.yaml", "extra", "./clusters/platform/config/extra")
        self._app(root, "clusters/platform/apps/climb.yaml", "climb", "../elsewhere")
        _write(root / "clusters" / "platform" / "config" / "extra" / "np.yaml", yaml.safe_dump_all([
            {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": {"name": "allow-all"}, "spec": {"podSelector": {}, "ingress": [{}]}},
            {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "extra-svc"}, "spec": {"ports": [{"port": 80}]}}]))
        _write(root / "clusters" / "platform" / "config" / "extra" / "cm.yaml", yaml.safe_dump({"kind": "ConfigMap", "metadata": {"name": "c"}, "data": {"u": "http://extra-svc.monitoring.svc:80/"}}))
        proc = self._py(gather, "clusters", root / "g.yaml", "monitoring", cwd=root)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual([(d["metadata"]["name"], d["metadata"]["namespace"]) for d in yaml.safe_load_all((root / "g.yaml").read_text()) if isinstance(d, dict)], [("allow-all", "monitoring")])
        proc = self._py(svc, ".", cwd=root)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr); self.assertNotIn("neither declares", proc.stdout)

    def test_a_cron_only_agent_plane_is_not_a_pod_floor(self):
        """A4: one enabled cron agent with no trigger raised the managed-pod floor by one,
        and between two Jobs the plane has no pod — a healthy instance went red. The floor
        counts the plane only when it renders a persistent workload."""
        code = self._splice("stage3.sh", "PROJCOUNT=$(python3 -c \"\n", "\" 2>/dev/null || echo 'UNREADABLE(no-python)')")
        for agents, want in (([{"name": "a", "mode": "cronjob", "schedule": "* * * * *"}], "0"),
                             ([{"name": "b"}], "1"),
                             ([{"name": "a", "mode": "cronjob", "schedule": "* * * * *", "triggers": [{"type": "logs"}]}], "1"),
                             ([{"name": "a", "mode": "cronjob", "schedule": "* * * * *", "enabled": False}, {"name": "b", "enabled": False}], "0")):
            with self.subTest(agents=agents):
                root = self._root(); _write(root / "catalog" / "projects.yaml", yaml.safe_dump({"projects": [], "agents": agents}))
                proc = self._py(code, cwd=root); self.assertEqual(proc.stdout.strip(), want, proc.stderr)
        root = self._root(); _write(root / "catalog" / "projects.yaml", yaml.safe_dump({"projects": [{"id": "p"}], "agents": [{"name": "a", "mode": "cronjob", "schedule": "* * * * *"}]}))
        self.assertEqual(self._py(code, cwd=root).stdout.strip(), "1")


class Round22Test(unittest.TestCase):
    """Area-1 sweep round 22: each row pins one repair."""

    def _stage(self, name):
        return (ROOT / "scripts" / "verify" / name).read_text()

    def _splice(self, stage, start, end):
        text = self._stage(stage)
        i = text.index(start) + len(start)
        return text[i:text.index(end, i)]

    def _root(self):
        root = pathlib.Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        return root

    def _py(self, code, *args, cwd=None):
        return subprocess.run([sys.executable, "-c", code] + [str(a) for a in args], cwd=cwd, capture_output=True, text=True, timeout=120)

    def _bash(self, script, cwd=None, env=None, trace=False):
        return subprocess.run(["bash"] + (["-x"] if trace else []) + ["-c", script], cwd=cwd, capture_output=True,
                              text=True, timeout=120, env={**os.environ, **(env or {})})

    def _git(self, root, *args):
        return subprocess.run(["git", "-c", "user.name=f", "-c", "user.email=f@f", "-c", "commit.gpgsign=false"] + list(args), cwd=root, capture_output=True, text=True, timeout=120)

    OKBAD = "ok(){ echo \"  ok    $1\"; }; bad(){ echo \"  FAIL  $1\"; }\n"
    SVC = "SVCPORT=$(python3 - . <<'PY'\n"

    def _app(self, root, rel, name, path, ns="monitoring"):
        _write(root / rel, yaml.safe_dump({"apiVersion": "argoproj.io/v1alpha1", "kind": "Application", "metadata": {"name": name, "namespace": "argocd"},
                                           "spec": {"source": {"path": path, "directory": {"recurse": True}}, "destination": {"namespace": ns}}}))

    # ---- breaker -------------------------------------------------------------------
    def test_a_documents_namespace_reaches_a_finding_only_in_namespace_shape(self):
        """B1: a bare Service name resolved in the document's own metadata.namespace and the
        finding repeated that namespace verbatim."""
        svc = self._splice("stage5-platform.sh", self.SVC, "\nPY\n")
        root = self._root(); (root / "catalog").mkdir()
        self._app(root, "clusters/platform/apps/platform-config.yaml", "platform-config", "clusters/platform/config")
        _write(root / "clusters" / "platform" / "config" / "cm.yaml", yaml.safe_dump({"kind": "ConfigMap", "metadata": {"name": "c", "namespace": "Bearer PLANTED_NAMESPACE_CREDENTIAL_0123456789"}, "data": {"u": "http://unknown:80"}}))
        proc = self._py(svc, ".", cwd=root)
        self.assertNotEqual(proc.returncode, 0); self.assertNotIn("PLANTED", proc.stdout + proc.stderr)
        self.assertIn("is a bare Service name in a file whose namespace is not of a namespace shape (not repeated)", proc.stdout)

    def test_the_key_material_row_reads_the_complete_listing(self):
        """B3: `! git ls-files | grep -q` closed the pipe at the first hit and git died on
        SIGPIPE unnoticed; on a long index the key was missed. The listing is a private
        NUL-delimited file counted by python, and a listing git could not take is RED."""
        text = self._stage("stage2.sh")
        block = text[text.index("KEYF=$(mktemp)"):text.index("\n", text.index('check "no key material tracked in git"'))]
        self.assertNotIn("grep -q", block)
        pre = "ok(){ echo ok; }; bad(){ echo bad; }; check(){ if eval \"$2\" >/dev/null 2>&1; then ok \"$1\"; else bad \"$1\"; fi; }\n"
        root = self._root(); self._git(root, "init", "-q")
        _write(root / "000-age.key", "PLANTED")
        for i in range(4000): _write(root / ("a-very-long-file-name-that-fills-the-pipe-%05d.txt" % i), "x")
        self._git(root, "add", "-A"); self._git(root, "commit", "-qm", "m")
        proc = self._bash(pre + block + "\n", cwd=root, trace=True)
        self.assertEqual(proc.stdout.strip(), "bad", proc.stderr[-500:]); self.assertNotIn("PLANTED", proc.stdout + proc.stderr)
        self._git(root, "rm", "-q", "000-age.key"); self._git(root, "commit", "-qm", "m2")
        self.assertEqual(self._bash(pre + block + "\n", cwd=root).stdout.strip(), "ok")
        self.assertEqual(self._bash(pre + block + "\n", cwd=self._root()).stdout.strip(), "bad")

    def test_an_unlistable_directory_is_a_scan_that_shrank(self):
        """B4: os.walk swallows a listing error, so a directory with mode 0111 hid its files
        from both the instance-string check and the endpoint scan, which reported clean."""
        if os.geteuid() == 0:
            self.skipTest("root lists a mode-0111 directory")
        svc = self._splice("stage5-platform.sh", self.SVC, "\nPY\n")
        root = self._root(); (root / "catalog").mkdir()
        self._app(root, "clusters/platform/apps/platform-config.yaml", "platform-config", "clusters/platform/config")
        hidden = root / "clusters" / "platform" / "config" / "PLANTED_UNLISTED_DIR"
        _write(hidden / "cm.yaml", yaml.safe_dump({"kind": "ConfigMap", "metadata": {"name": "c"}, "data": {"u": "http://nowhere.monitoring.svc:80/"}}))
        hidden.chmod(0o111); self.addCleanup(lambda: hidden.chmod(0o755))
        proc = self._py(svc, ".", cwd=root)
        self.assertNotEqual(proc.returncode, 0, proc.stdout); self.assertNotIn("PLANTED", proc.stdout + proc.stderr)
        self.assertIn("a directory under clusters could not be listed (PermissionError; the path is not repeated) — the endpoint scan is INCOMPLETE", proc.stdout)

    def test_a_failed_recovery_listing_is_unchecked_not_empty(self):
        """B5: `git ls-files recovery/*` failing left an empty file that read as "holds no
        files"."""
        block = "RECF=$(mktemp); REC_ROW=UNREADABLE\n" + self._splice("stage4.sh", "RECF=$(mktemp); REC_ROW=UNREADABLE\n", "no recovery backup is sanctioned: ${REC_ROW#* }\"\nfi\n") + "no recovery backup is sanctioned: ${REC_ROW#* }\"\nfi\n"
        root = self._root(); self._git(root, "init", "-q"); _write(root / "recovery" / "backup.txt", "x"); self._git(root, "add", "-A"); self._git(root, "commit", "-qm", "m")
        self.assertIn("FAIL  recovery/ holds 1 unsanctioned file(s) — delete them, no recovery backup is sanctioned: recovery/backup.txt", self._bash(self.OKBAD + block, cwd=root).stdout)
        _write(root / "bin" / "git", "#!/bin/sh\ncase \"$*\" in *recovery*) exit 1;; esac\nexec /usr/bin/git \"$@\"\n", executable=True)
        out = self._bash(self.OKBAD + block, cwd=root, env={"PATH": str(root / "bin") + os.pathsep + os.environ["PATH"]}).stdout
        self.assertIn("FAIL  the recovery/ listing could not be read (git ls-files failed) — the recovery-backup row is UNCHECKED, not clean", out)
        self.assertNotIn("holds no files", out)
        self._git(root, "rm", "-q", "recovery/backup.txt"); self._git(root, "commit", "-qm", "m2")
        self.assertIn("ok    recovery/ holds no files", self._bash(self.OKBAD + block, cwd=root).stdout)

    # ---- auditor -------------------------------------------------------------------
    def test_the_generators_dispatch_diagnostics_never_become_a_shell_value_or_a_row(self):
        """A1: the dispatch rows captured bump-tag's output in a shell variable and repeated
        its first line; the generator's refusal quotes the value it refused (instance text)."""
        s3 = self._stage("stage3.sh")
        fn = s3[s3.index("dispatch() { # <label>"):s3.index("\n}\n", s3.index("dispatch() { # <label>")) + 3]
        self.assertNotIn("out=$(", fn); self.assertNotIn("head -1", fn)
        root = self._root()
        _write(root / "w" / "scripts" / "bump-tag.py", "import sys; print('refusing: PLANTED_PROVIDER_CREDENTIAL'); sys.exit(1)\n")
        proc = self._bash(self.OKBAD + "W=%s\n" % (root / "w") + fn + 'dispatch "a refused one" refuse X=1\ndispatch "an accepted one" accept X=1\n', trace=True)
        self.assertIn("ok    dispatch: a refused one", proc.stdout)
        self.assertIn("FAIL  dispatch: an accepted one (rc=1; the generator's message is not repeated)", proc.stdout)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr, proc.stderr[-1500:])

    def test_the_pair_render_uses_the_applications_release_name_and_namespace(self):
        """A5: both renders used a placeholder release name, so Helm's refusal of a release
        over 53 characters (a long project id plus a long environment) was never seen."""
        s3 = self._stage("stage3.sh")
        block = s3[s3.index("VALS=$(mktemp)\nfor f in clusters/apps/*-*.yaml clusters/orbit/*.yaml; do"):s3.index('rm -f "$VALS"\n') + len('rm -f "$VALS"\n')]
        self.assertNotIn("helm template check", block)
        def app(name, ns, rel=None):
            helm = {"valuesObject": {"x": 1}}
            if rel: helm["releaseName"] = rel
            return yaml.safe_dump({"apiVersion": "argoproj.io/v1alpha1", "kind": "Application", "metadata": {"name": name, "namespace": "argocd"},
                                   "spec": {"source": {"path": "charts/service", "helm": helm}, "destination": {"namespace": ns}}})
        root = self._root()
        _write(root / "clusters" / "apps" / "a-prod.yaml", app("proj-a-prod", "proj-a-prod"))
        _write(root / "clusters" / "apps" / "b-prod.yaml", app("proj-b-prod", "proj-b-prod", rel="custom-rel"))
        _write(root / "clusters" / "apps" / "c-prod.yaml", app("Bearer PLANTED_NAME_CREDENTIAL", "proj-c-prod"))
        _write(root / "bin" / "helm", "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$HELM_LOG\"\nexit 0\n", executable=True)
        pre = self.OKBAD + "RENDERFAIL=\"\"; RENDERED=0; ATTEMPTED=0; RENDERODD=0\n"
        proc = self._bash(pre + block + "echo \"fail=[$RENDERFAIL] rendered=$RENDERED\"\n", cwd=root, trace=True,
                          env={"PATH": str(root / "bin") + os.pathsep + os.environ["PATH"], "HELM_LOG": str(root / "helm.log")})
        log = (root / "helm.log").read_text()
        self.assertIn("template proj-a-prod charts/service --namespace proj-a-prod -f ", log)
        self.assertIn("template custom-rel charts/service --namespace proj-b-prod -f ", log)
        self.assertIn("fail=[ clusters/apps/c-prod.yaml(unshaped-name-or-namespace)] rendered=2", proc.stdout)
        self.assertNotIn("PLANTED", log + proc.stdout + proc.stderr)
        if shutil.which("helm"):
            root = self._root(); long = "p" * 40 + "-" + "e" * 20
            _write(root / "clusters" / "apps" / "long-prod.yaml", app(long, "proj-long"))
            shutil.copytree(ROOT / "charts" / "service", root / "charts" / "service")
            proc = self._bash(pre + block + "echo \"fail=[$RENDERFAIL]\"\n", cwd=root)
            self.assertIn("fail=[ clusters/apps/long-prod.yaml]", proc.stdout, proc.stdout + proc.stderr[-800:])

    def test_the_service_index_renders_with_the_applications_release_name_and_namespace(self):
        """A5 (the stage5 half): the Service index rendered every chart Application as
        release `x` with no namespace."""
        svc = self._splice("stage5-platform.sh", self.SVC, "\nPY\n")
        root = self._root(); (root / "catalog").mkdir()
        _write(root / "clusters" / "orbit" / "a-prod.yaml", yaml.safe_dump({"apiVersion": "argoproj.io/v1alpha1", "kind": "Application", "metadata": {"name": "proj-a-prod", "namespace": "argocd"},
                                                                          "spec": {"source": {"path": "charts/service", "helm": {"valuesObject": {"x": 1}}}, "destination": {"namespace": "proj-a-prod"}}}))
        _write(root / "bin" / "helm", "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$HELM_LOG\"\nexit 0\n", executable=True)
        proc = subprocess.run([sys.executable, "-c", svc, "."], cwd=root, capture_output=True, text=True, timeout=120,
                              env={**os.environ, "PATH": str(root / "bin") + os.pathsep + os.environ["PATH"], "HELM_LOG": str(root / "helm.log")})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("template proj-a-prod ./charts/service --namespace proj-a-prod -f ", (root / "helm.log").read_text())
        _write(root / "clusters" / "orbit" / "a-prod.yaml", (root / "clusters" / "orbit" / "a-prod.yaml").read_text().replace("name: proj-a-prod", "name: Bearer PLANTED_RELEASE_CREDENTIAL"))
        proc = subprocess.run([sys.executable, "-c", svc, "."], cwd=root, capture_output=True, text=True, timeout=120,
                              env={**os.environ, "PATH": str(root / "bin") + os.pathsep + os.environ["PATH"], "HELM_LOG": str(root / "helm2.log")})
        self.assertNotEqual(proc.returncode, 0); self.assertIn("its release name or destination namespace is not of an object-name shape (not repeated)", proc.stdout)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr + (root / "helm2.log").read_text() if (root / "helm2.log").exists() else proc.stdout + proc.stderr)

    def test_a_failed_metadata_curl_reads_blocked_not_000blocked(self):
        """A6: `|| echo blocked` APPENDED to the 000 curl had already printed, so a correctly
        blocked metadata service read as `md=000blocked` and the probe row went unreadable."""
        s3 = self._stage("stage3.sh")
        remote = s3[s3.index("\"${SSHN[@]}\" 'CID=$(crictl ps") + len("\"${SSHN[@]}\" '"):s3.index("' > \"$PROBEF\"")]
        self.assertNotIn("|| echo blocked", remote); self.assertNotIn("|| echo unreachable", remote)
        root = self._root()
        _write(root / "bin" / "crictl", "#!/bin/sh\ncase \"$1\" in ps) echo cid1;; inspect) echo '{\"info\":{\"pid\":4242}}';; esac\n", executable=True)
        _write(root / "bin" / "nsenter", "#!/bin/sh\nshift 3; [ $# -eq 0 ] && exit 0; exec \"$@\"\n", executable=True)
        _write(root / "bin" / "curl", "#!/bin/sh\ncase \"$*\" in *169.254.169.254*) printf 000; exit \"$MD_RC\";; *) printf 200; exit 0;; esac\n", executable=True)
        env = {"PATH": str(root / "bin") + os.pathsep + os.environ["PATH"]}
        self.assertEqual(self._bash(remote, env={**env, "MD_RC": "7"}).stdout.strip(), "md=blocked net=200")
        self.assertEqual(self._bash(remote, env={**env, "MD_RC": "0"}).stdout.strip(), "md=000 net=200")


class Round23Test(unittest.TestCase):
    """Area-1 sweep round 23: each row pins one repair."""

    def _stage(self, name):
        return (ROOT / "scripts" / "verify" / name).read_text()

    def _splice(self, stage, start, end):
        text = self._stage(stage)
        i = text.index(start) + len(start)
        return text[i:text.index(end, i)]

    def _root(self):
        root = pathlib.Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        return root

    def _py(self, code, *args, cwd=None):
        return subprocess.run([sys.executable, "-c", code] + [str(a) for a in args], cwd=cwd, capture_output=True, text=True, timeout=120)

    def _bash(self, script, cwd=None, env=None, trace=False):
        return subprocess.run(["bash"] + (["-x"] if trace else []) + ["-c", script], cwd=cwd, capture_output=True,
                              text=True, timeout=120, env={**os.environ, **(env or {})})

    def _stub(self, root, name, body):
        _write(root / "bin" / name, "#!/bin/sh\nprintf '%s\\n' \"$@\" >> \"$KLOG\"\n" + body + "\n", executable=True)
        return {"PATH": str(root / "bin") + os.pathsep + os.environ["PATH"], "KLOG": str(root / "stub.log")}

    OKBAD = "ok(){ echo \"  ok    $1\"; }; bad(){ echo \"  FAIL  $1\"; }\n"
    INTAKE = ROOT / "template" / "provisioning" / "clusters" / "platform" / "config" / "orbit-intake"

    # ---- auditor -------------------------------------------------------------------
    def test_the_mutation_harness_reads_the_source_config_behind_a_class_only_refusal(self):
        """A1: the harness parsed the instance's Alertmanager Application with stderr inherited;
        PyYAML's constructor error repeats an unknown tag (pasted text) verbatim. A harness
        that could not build lands every mutant row RED, never vacuously green."""
        s3 = self._stage("stage3.sh")
        launch = "  python3 - \"$AMSRC\" \"$MUT\" \"$AMCHECK\" 2>/dev/null <<'PY'\n"
        code = self._splice("stage3.sh", launch, "\nPY\n")
        root = self._root(); (root / "mut").mkdir()
        _write(root / "src.yaml", "spec: {source: {helm: {valuesObject: !!PLANTED_TAG_CREDENTIAL x}}}\n")
        proc = self._py(code, root / "src.yaml", root / "mut", ROOT / "scripts" / "verify" / "am-routes.py")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr[-500:])
        self.assertIn("could not be read for mutation (ConstructorError)", proc.stdout)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)
        fn = s3[s3.index("  am_mutant() {"):s3.index("\n  }\n", s3.index("  am_mutant() {")) + 5]
        out = self._bash(self.OKBAD + "HARNESS_RC=2; MUT=%s; AMCHECK=/nonexistent\n" % (root / "mut") + fn + 'am_mutant decoy-clean green "a clean config"\nam_mutant narrow red "a narrowed route"\n').stdout
        self.assertEqual(out.count("FAIL  alertmanager(oracle):"), 2, out); self.assertIn("no mutant was built", out); self.assertNotIn("ok    ", out)

    def test_status_only_ssh_rows_discard_the_nodes_stdout(self):
        """A2: the iptables rows inherited ssh's stdout (and one both streams); a shell
        profile or a command wrapper on the node printing a line put it in the stage's output."""
        block = self._splice("stage3.sh", "# Assert the rules are in the RAW table.", "# And that the re-assertion timer")
        for line in block.splitlines():
            if line.startswith('"${SSHN[@]}"'):
                self.assertIn(">/dev/null 2>&1", line, line[:100])
        root = self._root()
        _write(root / "bin" / "ssh", "#!/bin/sh\necho 'PLANTED_NODE_LINE'; echo 'PLANTED_NODE_ERR' >&2; exit 0\n", executable=True)
        proc = self._bash(self.OKBAD + "SSHN=(ssh root@203.0.113.5)\n" + block, env={"PATH": str(root / "bin") + os.pathsep + os.environ["PATH"]})
        self.assertEqual(proc.stdout.count("  ok    "), 3, proc.stdout); self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_backup_freshness_counts_only_this_clusters_backups(self):
        """A4: the newest completed backup in the namespace dated the row whatever cluster it
        belonged to; another cluster's fresh backup hid this database's stale chain."""
        s3 = self._stage("stage3.sh")
        block = s3[s3.index("    BKF=$(mktemp)"):]
        block = block[:block.index("\n    fi\n") + len("\n    fi\n")]
        self.assertIn("{.spec.cluster.name}", block)
        root = self._root()
        recent = subprocess.run(["date", "-u", "-d", "-1 hour", "+%Y-%m-%dT%H:%M:%SZ"], capture_output=True, text=True).stdout.strip()
        env = self._stub(root, "kubectl", "printf 'completed %s other-db\\ncompleted 2026-01-01T00:00:00Z alpha-db\\n'" % recent)
        proc = self._bash(self.OKBAD + "p=alpha\n" + block, env=env)
        self.assertIn("FAIL  alpha-db: no completed base backup in 26h (newest: 2026-01-01T00:00:00Z)", proc.stdout)
        env = self._stub(root, "kubectl", "printf 'completed %s alpha-db\\ncompleted 2026-01-01T00:00:00Z other-db\\n'" % recent)
        self.assertIn("ok    alpha-db: newest completed base backup is 1h old (<26h)", self._bash(self.OKBAD + "p=alpha\n" + block, env=env).stdout)

    def test_a_cronjobs_absence_is_proven_by_notfound_never_by_a_failed_lookup(self):
        """A5: every non-zero lookup (forbidden, a transport failure) read as "the pg_dump
        CronJob is gone"."""
        block = "    CJF=$(mktemp)\n" + self._splice("stage3.sh", "    CJF=$(mktemp)\n", 'fi; rm -f "$CJF"\n') + 'fi; rm -f "$CJF"\n'
        root = self._root()
        for body, want in (("echo 'Error from server (NotFound): cronjobs.batch \"alpha-db-backup\" not found' >&2; exit 1", "ok    alpha-prod: pg_dump CronJob is gone"),
                           ("echo 'Error from server (Forbidden): PLANTED_LOOKUP_TEXT' >&2; exit 1", "FAIL  alpha-prod: the pg_dump CronJob could not be looked up (kubectl's message is not repeated) — its absence is UNPROVEN"),
                           ("exit 0", "FAIL  alpha-prod still runs the pg_dump CronJob")):
            with self.subTest(body=body[:30]):
                proc = self._bash(self.OKBAD + "p=alpha\n" + block, env=self._stub(root, "kubectl", body), trace=True)
                self.assertIn(want, proc.stdout, proc.stdout); self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_an_explicit_namespace_label_is_the_alerts_only_namespace(self):
        """A6: the simulated alert always got a second variant carrying the plane's
        namespace, so a trigger whose catalog labels name a namespace was judged
        against a namespace it never carries (a rule label overrides the series')."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("amr", ROOT / "scripts" / "verify" / "am-routes.py")
        amr = importlib.util.module_from_spec(spec); spec.loader.exec_module(amr)
        self.assertEqual(amr.variants({"alertname": "A", "team": "billing"}), [{"alertname": "A", "team": "billing"}, {"alertname": "A", "team": "billing", "namespace": amr.PLANE_NAMESPACE}])
        self.assertEqual(amr.variants({"alertname": "A", "namespace": "billing-prod"}), [{"alertname": "A", "namespace": "billing-prod"}])

    def test_the_attached_in_flight_cap_must_be_a_map_reached_by_the_chain(self):
        """A8: a scalar inFlightReq on the attached middleware counted as the OOM leaf, and an
        unused inFlightReq map elsewhere in the file satisfied the global-cap guard."""
        code = self._splice("stage5-platform.sh", "  MOUT=$(python3 - \"$IDIR/ingress.yaml\" <<'PY'\n", "\nPY\n")
        base = (self.INTAKE / "ingress.yaml").read_text().replace("{{", "").replace("}}", "")
        self.assertEqual(base.count("  inFlightReq:\n    amount: 16\n"), 1)
        root = self._root()
        def run(text):
            _write(root / "i.yaml", text); return self._py(code, root / "i.yaml")
        self.assertEqual(run(base).returncode, 0, run(base).stdout)
        scalar = base.replace("  inFlightReq:\n    amount: 16\n", "  inFlightReq: 16\n") + "---\napiVersion: traefik.io/v1alpha1\nkind: Middleware\nmetadata: {name: unused-cap, namespace: orbit-agents-prod}\nspec:\n  inFlightReq:\n    amount: 16\n"
        proc = run(scalar)
        self.assertNotEqual(proc.returncode, 0); self.assertIn("inFlightReq is not a map (its value is not repeated)", proc.stdout)
        self.assertIn("no inFlightReq middleware (as a map) in the door's chain", proc.stdout)

    # ---- breaker -------------------------------------------------------------------
    def test_workstation_path_overrides_are_validated_before_any_expansion(self):
        """B1: ORBIT_CONFIG_DIR and KUBECONFIG from the environment were expanded into an
        export, a python argv and stage2's pre-boot line unvalidated."""
        good = {"instance": {"name": "fx"}, "domains": {"primary": "fx.org", "dev": "dev.fx.org", "dnsZones": ["fx.org"]},
                "registry": "ghcr.io/fx", "git": {"repoUrl": "git@github.com:fx/i.git", "github_org": "fx"},
                "nodes": [{"name": "n1", "ip": "203.0.113.5", "role": "control-plane"}]}
        def load(env_extra, show=False):
            root = self._root(); (root / "instance").mkdir(); (root / "instance/config.yaml").write_text(yaml.safe_dump(good))
            # the refused value is still in this test's own environment: only the rc is echoed on a refusal
            script = "source " + str(ROOT / "scripts" / "lib-instance.sh") + ("; echo \"rc=$? [$ORBIT_CONFIG_DIR]\"" if show else "; echo \"rc=$?\"")
            env = {k: v for k, v in os.environ.items() if not k.startswith("ORBIT_") and k != "KUBECONFIG"}
            env["ORBIT_INSTANCE_CONFIG"] = str(root / "instance/config.yaml"); env.update(env_extra)
            return subprocess.run(["bash", "-x", "-c", script], capture_output=True, text=True, timeout=60, env=env)
        for env_extra, field in (({"ORBIT_CONFIG_DIR": "Bearer PLANTED_DIRECTORY_CREDENTIAL"}, "ORBIT_CONFIG_DIR is not an absolute filesystem path"),
                                 ({"KUBECONFIG": "https://user:PLANTED_KUBECONFIG_CREDENTIAL@cluster.example"}, "KUBECONFIG entry #1 is not an absolute filesystem path"),
                                 ({"KUBECONFIG": "/home/u/.kube/a:PLANTED relative"}, "KUBECONFIG entry #2 is not an absolute filesystem path"),
                                 # round 24 (A1): the pointer to the config was expanded into a python argv before any check
                                 ({"ORBIT_INSTANCE_CONFIG": "Bearer PLANTED_CONFIG_CREDENTIAL"}, "ORBIT_INSTANCE_CONFIG is not an absolute filesystem path")):
            with self.subTest(field=field):
                proc = load(env_extra)
                self.assertIn("rc=2", proc.stdout, proc.stderr[-800:]); self.assertIn(field, proc.stderr)
                self.assertNotIn("PLANTED", proc.stdout + proc.stderr, proc.stderr[-1500:])
        proc = load({"ORBIT_CONFIG_DIR": "/tmp/orbit-fx", "KUBECONFIG": "/home/u/.kube/a:/home/u/.kube/b"}, show=True)
        self.assertIn("rc=0 [/tmp/orbit-fx]", proc.stdout, proc.stderr[-800:])

    def test_a_scrape_count_above_one_is_a_healthy_collector(self):
        """B2: `1|1.*)` read two healthy Traefik (or Alloy) targets as "not being scraped"."""
        s5 = self._stage("stage5-platform.sh")
        block = s5[s5.index("  UPT=$(promval 'count(up{job=\"traefik/traefik\"} == 1) or vector(0)')"):]
        block = block[:block.index("\n  esac\n") + len("\n  esac\n")]
        for pv, want in (("2", "ok    Prometheus reports up==1 for the traefik PodMonitor (2 target(s))"), ("1", "ok    Prometheus reports up==1 for the traefik PodMonitor (1 target(s))"),
                         ("0", "FAIL  traefik is not being scraped (up==1 count is 0)"), ("QUERYERR", "FAIL  could not answer the traefik scrape query"),
                         ("PLANTED_TEXT", "FAIL  the traefik scrape count is not a number (not repeated)")):
            with self.subTest(pv=pv):
                out = self._bash(self.OKBAD + "SCOLL=x; promval(){ printf '%s' \"$PV\"; }\n" + block, env={"PV": pv}).stdout
                self.assertIn(want, out, out); self.assertNotIn("PLANTED", out)
        alloy = s5[s5.index("    UPA=$(promval 'count(up{job=\"alloy\"} == 1) or vector(0)')"):]
        alloy = alloy[:alloy.index("\n    esac\n") + len("\n    esac\n")]
        self.assertIn("ok    Prometheus reports up==1 for the alloy ServiceMonitor (3 target(s))", self._bash(self.OKBAD + "promval(){ printf 3; }\n" + alloy).stdout)

    def test_traefik_readiness_is_an_integer_read_from_a_private_file(self):
        """B3: `grep -q 1` on readyReplicas matched 11 and missed 2, and the API's text was
        a value in the traced comparison."""
        s2 = self._stage("stage2.sh")
        block = s2[s2.index("TRF=$(mktemp)"):s2.index("\n", s2.index('check "traefik pod running"'))]
        root = self._root()
        pre = "ok(){ echo ok; }; bad(){ echo bad; }; check(){ if eval \"$2\" >/dev/null 2>&1; then ok \"$1\"; else bad \"$1\"; fi; }\n"
        for out, want in (("2", "ok"), ("11", "ok"), ("1", "ok"), ("0", "bad"), ("", "bad"), ("PLANTED_API_TEXT", "bad")):
            with self.subTest(out=out):
                proc = self._bash(pre + block + "\n", env=self._stub(root, "kubectl", "printf '%s' \"$KOUT\"") | {"KOUT": out}, trace=True)
                self.assertEqual(proc.stdout.strip(), want, proc.stderr[-600:]); self.assertNotIn("PLANTED", proc.stdout + proc.stderr)


class Round24Test(Round23Test):
    """Area-1 sweep round 24: each row pins one repair (the round-23 helpers are reused)."""

    # ---- auditor -------------------------------------------------------------------
    def test_the_redirect_probe_reads_its_headers_from_a_private_file(self):
        """A6: the redirect row's curl wrote headers to a scratch path with no -D; a
        proxy or a slow node hung the stage."""
        s2 = self._stage("stage2.sh")
        self.assertIn('curl -s --max-time 15 -D "$RHDR" -o /dev/null --resolve "probe.$ORBIT_DOMAIN:80:$IP"', s2)

    # ---- breaker -------------------------------------------------------------------
    def test_the_in_flight_caps_fields_are_held_to_the_crds_types(self):
        """B3: a quoted amount ("16") and a quoted requestHost ("false") passed the
        middleware audit; the API server refuses both and disables the router."""
        code = self._splice("stage5-platform.sh", "  MOUT=$(python3 - \"$IDIR/ingress.yaml\" <<'PY'\n", "\nPY\n")
        base = (self.INTAKE / "ingress.yaml").read_text().replace("{{", "").replace("}}", "")
        cap = "  inFlightReq:\n    amount: 16\n"
        self.assertEqual(base.count(cap), 1)
        root = self._root()
        def run(text):
            _write(root / "i.yaml", text); return self._py(code, root / "i.yaml")
        self.assertEqual(run(base).returncode, 0, run(base).stdout)
        proc = run(base.replace(cap, "  inFlightReq:\n    amount: \"16\"\n"))
        self.assertNotEqual(proc.returncode, 0); self.assertIn("inFlightReq.amount is not in 1..1024", proc.stdout)
        proc = run(base.replace(cap, cap + "    sourceCriterion:\n      requestHost: \"false\"\n"))
        self.assertNotEqual(proc.returncode, 0); self.assertIn("sourceCriterion.requestHost is not a boolean (its value is not repeated)", proc.stdout)
        proc = run(base.replace(cap, cap + "    sourceCriterion:\n      requestHost: true\n"))
        self.assertEqual(proc.returncode, 0, proc.stdout)

    def test_the_persisted_inotify_raise_is_the_effective_assignment(self):
        """B4: `grep -q max_user_instances` on the node's sysctl file matched a
        commented-out line, and the file's text stood in a shell variable."""
        s2 = self._stage("stage2.sh")
        block = s2[s2.index("PERSF=$(mktemp)"):s2.index("\n", s2.index('check "the inotify raise is persisted'))]
        root = self._root()
        pre = "ok(){ echo ok; }; bad(){ echo bad; }; check(){ if eval \"$2\" >/dev/null 2>&1; then ok \"$1\"; else bad \"$1\"; fi; }\nIP=203.0.113.7\n"
        for text, want in (("fs.inotify.max_user_instances = 512\n", "ok"), ("# fs.inotify.max_user_instances = 512\n", "bad"),
                           ("fs.inotify.max_user_instances=1024\n", "ok"), ("fs.inotify.max_user_instances = 256\n", "bad"),
                           ("fs.inotify.max_user_instances = 512\nfs.inotify.max_user_instances = 128\n", "bad"),
                           ("fs.inotify.max_user_watches = 524288\n", "bad"), ("", "bad"), ("PLANTED_NODE_TEXT = 512\n", "bad")):
            with self.subTest(text=text[:40]):
                proc = self._bash(pre + block + "\n", env=self._stub(root, "ssh", "printf '%s' \"$SSHOUT\"") | {"SSHOUT": text, "ORBIT_SSH_KEY": "/k"}, trace=True)
                self.assertEqual(proc.stdout.strip(), want, proc.stderr[-600:]); self.assertNotIn("PLANTED", proc.stdout + proc.stderr)


class Round25Test(Round23Test):
    """Area-1 sweep round 25: each row pins one repair (the round-23 helpers are reused)."""

    # ---- auditor -------------------------------------------------------------------
    def test_a_policy_rendered_twice_is_the_last_one_and_a_defect(self):
        """A3: the gather kept both policies of one identity while the renderer applies only
        the last in file order; an Egress-only twin in a later file silently replaced the
        ingress isolation."""
        gather = self._splice("stage5-platform.sh", "gather_netpols(){ python3 - clusters \"$2\" \"$1\" <<'PY'\n", "\nPY\n")
        root = self._root()
        _write(root / "clusters" / "platform" / "apps" / "monitoring-config.yaml", yaml.safe_dump({"apiVersion": "argoproj.io/v1alpha1", "kind": "Application",
               "metadata": {"name": "monitoring-config", "namespace": "argocd"}, "spec": {"source": {"path": "clusters/platform/config/monitoring", "directory": {"recurse": True}},
                                                                                        "destination": {"namespace": "monitoring"}}}))
        base = root / "clusters" / "platform" / "config" / "monitoring"
        pol = lambda types: {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": {"name": "grafana-ingress"},
                             "spec": {"podSelector": {"matchLabels": {"app.kubernetes.io/name": "grafana"}}, "policyTypes": types}}
        _write(base / "netpol.yaml", yaml.safe_dump(pol(["Ingress"])))
        proc = self._py(gather, "clusters", root / "g.yaml", "monitoring", cwd=root)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        _write(base / "zz-grafana.yaml", yaml.safe_dump(pol(["Egress"])))
        proc = self._py(gather, "clusters", root / "g.yaml", "monitoring", cwd=root)
        self.assertNotEqual(proc.returncode, 0); self.assertIn("rendered more than once under clusters/", proc.stdout); self.assertNotIn("grafana-ingress", proc.stdout)
        docs = [d for d in yaml.safe_load_all((root / "g.yaml").read_text()) if isinstance(d, dict)]
        self.assertEqual([d["spec"]["policyTypes"] for d in docs], [["Egress"]])   # the effective set is the renderer's

    def test_an_agents_pin_is_proven_on_its_running_pods_after_a_complete_rollout(self):
        """A4: Available=True plus the TEMPLATE's image counted as "runs the pin" while a
        paused or mid-rollout Deployment served old pods."""
        s3 = self._stage("stage3.sh")
        block = s3[s3.index("    ROLLF=$(mktemp)"):s3.index("the template's pin is not what serves\"") + len("the template's pin is not what serves\"")]
        root = self._root()
        def run(deploy, pods, rc=0):
            body = ("case \"$*\" in *'get pods'*) printf '%s' \"$PODS\"; exit 0;; *) printf '%s' \"$DEP\"; exit " + str(rc) + ";; esac")
            env = self._stub(root, "kubectl", body) | {"DEP": deploy, "PODS": pods}
            return self._bash(self.OKBAD + "AG=a; PIN=ghcr.io/fx/agents:v1\n" + block + "\n", env=env, trace=True)
        good = "\n3\n3\n2\n2\n2\n"
        proc = run(good, "Running ghcr.io/fx/agents:v1\nRunning ghcr.io/fx/agents:v1\n")
        self.assertIn("ok    agent a rollout is complete", proc.stdout); self.assertIn("ok    agent a: every Running pod runs the agentRuntime pin", proc.stdout)
        for deploy, pods, want in (("true\n3\n3\n2\n2\n2\n", "Running ghcr.io/fx/agents:v1\n", "rollout is not complete (paused)"),
                                   ("\n4\n3\n2\n2\n2\n", "Running ghcr.io/fx/agents:v1\n", "rollout is not complete (the observed generation is behind the spec)"),
                                   ("\n3\n3\n2\n1\n2\n", "Running ghcr.io/fx/agents:v1\n", "rollout is not complete (1 updated, 2 ready of 2 desired)"),
                                   ("\n3\n3\n2\nPLANTED_TEXT\n2\n", "Running ghcr.io/fx/agents:v1\n", "rollout is not complete (unreadable)"),
                                   (good, "Running ghcr.io/fx/agents:v1\nRunning ghcr.io/fx/agents:v2-PLANTED\n", "do not all run the agentRuntime pin (mismatch;"),
                                   (good, "Succeeded ghcr.io/fx/agents:v1\n", "do not all run the agentRuntime pin (none;"),
                                   (good, "", "do not all run the agentRuntime pin (none;")):
            with self.subTest(deploy=deploy[:12], pods=pods[:30]):
                proc = run(deploy, pods)
                self.assertIn(want, proc.stdout, proc.stdout); self.assertNotIn("PLANTED", proc.stdout + proc.stderr)
        self.assertIn("rollout is not complete (unreadable)", run("", "", rc=1).stdout)
        self.assertNotIn("LIVE=$(kubectl", s3); self.assertIn('-l "app.kubernetes.io/name=$AG,app.kubernetes.io/instance=orbit-agents"', block)

    def test_a_cron_agents_missing_workloads_are_proven_by_notfound(self):
        """A5: `kubectl get deploy … && bad || ok` read a forbidden or failed lookup as
        "no Deployment"."""
        s3 = self._stage("stage3.sh")
        block = s3[s3.index("      ABSF=$(mktemp)"):s3.index('      rm -f "$ABSF"') + len('      rm -f "$ABSF"')]
        root = self._root()
        for body, wants in (("exit 0", ("FAIL  cron agent a also has a Deployment", "FAIL  cron agent a has a Service")),
                            ("echo 'Error from server (NotFound): deployments.apps \"x\" not found' >&2; exit 1", ("ok    cron agent a has no Deployment", "ok    cron agent a has no Service")),
                            ("echo 'Error from server (Forbidden): PLANTED_API_TEXT' >&2; exit 1", ("FAIL  cron agent a: the Deployment lookup failed some other way", "FAIL  cron agent a: the Service lookup failed some other way")),
                            ("exit 7", ("its absence is UNPROVEN", "its absence is UNPROVEN"))):
            with self.subTest(body=body[:20]):
                proc = self._bash(self.OKBAD + "AG=a\n" + block + "\n", env=self._stub(root, "kubectl", body), trace=True)
                for w in wants: self.assertIn(w, proc.stdout, proc.stdout)
                self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    # ---- breaker -------------------------------------------------------------------
    def test_a_recovery_name_of_a_credentials_shape_is_counted_not_shown(self):
        """B1: a tracked recovery file named like a token passed the path grammar and
        was repeated in the red row."""
        block = "RECF=$(mktemp); REC_ROW=UNREADABLE\n" + self._splice("stage4.sh", "RECF=$(mktemp); REC_ROW=UNREADABLE\n", "no recovery backup is sanctioned: ${REC_ROW#* }\"\nfi\n") + "no recovery backup is sanctioned: ${REC_ROW#* }\"\nfi\n"
        root = self._root()
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        _write(root / "recovery" / "ghp_PLANTED0123456789abcdefghijklmnop", "x\n"); _write(root / "recovery" / "plain.txt", "x\n")
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        out = self._bash(self.OKBAD + block, cwd=root).stdout
        self.assertIn("FAIL  recovery/ holds 2 unsanctioned file(s) — delete them, no recovery backup is sanctioned: recovery/plain.txt (+1 name(s) not of a path shape, or of a credential's shape)", out)
        self.assertNotIn("PLANTED", out)

    def test_the_operators_ready_count_is_a_whole_integer_from_a_checked_listing(self):
        """B4: kubectl's failure behind a pipe read as "no Deployment" or as available, and
        a line starting with a digit counted as a ready count."""
        block = self._splice("stage4.sh", "  # the listing goes to a private file;", "sealed files are not being decrypted into Secrets\"\n  fi\n") + "sealed files are not being decrypted into Secrets\"\n  fi\n"
        block = "  # the listing goes to a private file;" + block
        root = self._root()
        for body, want in (("printf '2\\n0\\n'", "ok    the sops operator is available"), ("printf '1\\n'", "ok    the sops operator is available"),
                           ("printf '1not-a-count\\n'", "FAIL  no ready Deployment in ns sops"), ("printf ''", "FAIL  no ready Deployment in ns sops"),
                           ("printf '0\\n'", "FAIL  no ready Deployment in ns sops"),
                           ("printf '1\\n'; exit 1", "FAIL  the Deployment listing in ns sops could not be read"),
                           ("echo PLANTED_API_TEXT >&2; exit 1", "FAIL  the Deployment listing in ns sops could not be read")):
            with self.subTest(body=body[:22]):
                proc = self._bash(self.OKBAD + block, env=self._stub(root, "kubectl", body), trace=True)
                self.assertIn(want, proc.stdout, proc.stdout); self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_the_memory_quantities_are_the_configured_nodes(self):
        """B6: items[0] was whichever node sorted first; on a two-node cluster the row
        described the wrong node."""
        s2 = self._stage("stage2.sh")
        self.assertIn('kubectl get node "$ORBIT_NODE_NAME" -o jsonpath=\'{.status.allocatable.memory}{"\\n"}{.status.capacity.memory}{"\\n"}\' > "$MEMQ"', s2)
        self.assertNotIn("items[0].status.allocatable", s2)


class Round26Test(Round23Test):
    """Area-1 sweep round 26: each row pins one repair (the round-23 helpers are reused)."""

    GATHER = "gather_netpols(){ python3 - clusters \"$2\" \"$1\" <<'PY'\n"
    SVC = "SVCPORT=$(python3 - . <<'PY'\n"

    def _app(self, root, path, ns):
        _write(root / "clusters" / "platform" / "apps" / (ns + "-app.yaml"), yaml.safe_dump({"apiVersion": "argoproj.io/v1alpha1", "kind": "Application",
               "metadata": {"name": ns + "-app", "namespace": "argocd"}, "spec": {"source": {"path": path, "directory": {"recurse": True}}, "destination": {"namespace": ns}}}))

    # ---- auditor -------------------------------------------------------------------
    def test_an_alert_or_rule_name_of_a_credentials_shape_is_counted(self):
        """A2: an alertname (Alertmanager's text) or a logs rule name (the ConfigMap's)
        of a credential's shape passed the name grammar and was repeated."""
        s5 = self._stage("stage5-platform.sh"); root = self._root()
        block = s5[s5.index('  FIRING=$(python3 -c "'):s5.index('  case "$FIRING" in')]
        _write(root / "alerts.json", '[{"labels":{"alertname":"ghp_PLANTED0123456789abcdefghijklmnop"}},{"labels":{"alertname":"KubeDown"}}]')
        proc = self._bash("TMP=" + str(root) + "\n" + block + "echo \"FIRING=[$FIRING]\"\n")
        self.assertIn("FIRING=[KubeDown (+1 alert(s) not of an alert-name shape, or of a credential's shape)]", proc.stdout); self.assertNotIn("PLANTED", proc.stdout + proc.stderr)
        code = s5[s5.index("import json, re, sys, yaml\nimport shown as display   # the shared display rule (scripts/verify/shown.py)\nNAME = re.compile"):]; code = code[:code.index("\nPY\n")]
        _write(root / "rules.yaml", yaml.safe_dump({"groups": [{"rules": [{"alert": "PLANTED0123456789abcdef"}, {"alert": "Real_1"}]}]}))
        proc = self._py(code, root / "rules.yaml", root / "alerts.json")
        self.assertEqual(proc.stdout.strip(), "0 Real_1 (+1 rule name(s) not of an alert-name shape, or of a credential's shape)", proc.stdout + proc.stderr); self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_a_service_protocol_is_one_of_the_apis_enum_or_named_by_position(self):
        """A3: `str(protocol).upper()` kept anything; a pasted value reached the
        port-mismatch row as `8080/PLANTED…`."""
        code = self._splice("stage5-platform.sh", self.SVC, "\nPY\n")
        for proto, want in (("PLANTEDCREDENTIAL0123456789", "FAIL Service monitoring/api declares a protocol that is not TCP, UDP or SCTP (the value is not repeated)"),
                            ("tcp", "FAIL Service monitoring/api declares a protocol that is not TCP, UDP or SCTP"), (7, "FAIL Service monitoring/api declares a protocol"),
                            ("UDP", "dials port 8080, which Service api.monitoring does not declare on TCP (it declares ['8080/UDP'])"), ("TCP", "OK ")):
            with self.subTest(proto=str(proto)[:8]):
                root = self._root(); (root / "catalog").mkdir(); _dirapp(root)
                _write(root / "clusters" / "s.yaml", yaml.safe_dump({"apiVersion": "v1", "kind": "Service", "metadata": {"name": "api", "namespace": "monitoring"}, "spec": {"ports": [{"port": 8080, "protocol": proto}]}}))
                _write(root / "clusters" / "cm.yaml", yaml.safe_dump({"kind": "ConfigMap", "metadata": {"name": "c"}, "data": {"u": "http://api.monitoring.svc:8080/"}}))
                proc = self._py(code, ".", cwd=root)
                self.assertIn(want, proc.stdout, proc.stdout); self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_an_unanswered_sent_counter_is_not_an_idle_ruler(self):
        """A5: the UNVERIFIED branch read RDISC and RERR only; a failed sent-counter query
        printed an ok row claiming the ruler sent nothing."""
        s5 = self._stage("stage5-platform.sh")
        block = s5[s5.index("      # A healthy installation has NO logs alert firing"):s5.index("\n    else\n      keyoff_absent \"no logs trigger deployed")]
        def ruler(disc, err, sent):
            return self._bash(self.OKBAD + "promval(){ case \"$1\" in *discovered*) echo \"$RD\";; *sent_total*) echo \"$RS\";; *) echo \"$RE\";; esac; }\n"
                              "WANT='OrbitAgentOomSpike'; HIT=0\n" + block + "\n", env={"RD": disc, "RE": err, "RS": sent}).stdout
        self.assertIn("[UNVERIFIED]", ruler("1", "0", "0"))
        for sent in ("QUERYERR", "ABSENT", "", "PLANTED_TEXT"):
            with self.subTest(sent=sent):
                out = ruler("1", "0", sent); self.assertIn("FAIL", out); self.assertNotIn("[UNVERIFIED]", out); self.assertNotIn("PLANTED_TEXT", out.replace("sent/1h=PLANTED_TEXT", ""))

    def test_a_source_directory_is_credited_only_when_the_renderer_can_open_it(self):
        """A6: normpath credited `clusters/no-such/../platform/monitoring` to the
        existing directory; the renderer opens the path as written and fails."""
        gather = self._splice("stage5-platform.sh", self.GATHER, "\nPY\n"); code = self._splice("stage5-platform.sh", self.SVC, "\nPY\n")
        root = self._root(); (root / "catalog").mkdir()
        _write(root / "clusters" / "platform" / "monitoring" / "cm.yaml", yaml.safe_dump({"kind": "ConfigMap", "metadata": {"name": "c"}, "data": {"u": "http://nowhere.monitoring.svc:80/"}}))
        for path, ok in (("clusters/no-such-source-dir/../platform/monitoring", False), ("clusters/platform/./monitoring", True), ("clusters/platform/missing", False)):
            with self.subTest(path=path):
                self._app(root, path, "monitoring")
                proc = self._py(gather, "clusters", root / "g.yaml", "monitoring", cwd=root)
                self.assertEqual(proc.returncode == 0, ok, proc.stdout); self.assertEqual("Application monitoring-app names a source directory the renderer cannot open" in proc.stdout, not ok)
                proc = self._py(code, ".", cwd=root)
                self.assertEqual("Application monitoring-app names a source directory the renderer cannot open" in proc.stdout, not ok, proc.stdout)
                self.assertNotIn("no-such-source-dir", proc.stdout)

    # ---- breaker -------------------------------------------------------------------
    def test_a_symlink_under_clusters_is_refused_by_both_scans(self):
        """B1: the renderer follows a symlinked source directory; rglob and os.walk do
        not, so a policy (or an endpoint) behind a link was silently outside the audit."""
        gather = self._splice("stage5-platform.sh", self.GATHER, "\nPY\n"); code = self._splice("stage5-platform.sh", self.SVC, "\nPY\n")
        root = self._root(); (root / "catalog").mkdir()
        self._app(root, "clusters/linked", "monitoring")
        _write(root / "elsewhere" / "np.yaml", yaml.safe_dump({"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": {"name": "wide"}, "spec": {"podSelector": {}, "ingress": [{}], "policyTypes": ["Ingress"]}}))
        _write(root / "elsewhere" / "cm.yaml", yaml.safe_dump({"kind": "ConfigMap", "metadata": {"name": "c"}, "data": {"u": "http://nowhere.monitoring.svc:80/"}}))
        (root / "clusters" / "linked").symlink_to(root / "elsewhere", target_is_directory=True)
        proc = self._py(gather, "clusters", root / "g.yaml", "monitoring", cwd=root)
        self.assertNotEqual(proc.returncode, 0); self.assertIn("clusters/ carries a symlink", proc.stdout); self.assertNotIn("elsewhere", proc.stdout)
        proc = self._py(code, ".", cwd=root)
        self.assertNotEqual(proc.returncode, 0); self.assertIn("1 symlink(s) under clusters (the renderer follows them; this scan does not", proc.stdout); self.assertNotIn("elsewhere", proc.stdout)
        (root / "clusters" / "linked").unlink(); shutil.copytree(root / "elsewhere", root / "clusters" / "linked")
        self.assertEqual(self._py(gather, "clusters", root / "g.yaml", "monitoring", cwd=root).returncode, 0)
        proc = self._py(code, ".", cwd=root)
        self.assertNotIn("symlink", proc.stdout); self.assertIn("names Service nowhere.monitoring, which this repo neither declares", proc.stdout)

    def test_an_endpoint_label_of_a_credentials_shape_is_described(self):
        """B3: the unresolvable-Service row repeated the authority and the Service name,
        and a hostname label passes the hostname grammar with a token in it."""
        code = self._splice("stage5-platform.sh", self.SVC, "\nPY\n")
        root = self._root(); (root / "catalog").mkdir()
        self._app(root, "clusters/platform/config/monitoring", "monitoring")
        _write(root / "clusters" / "platform" / "config" / "monitoring" / "cm.yaml", yaml.safe_dump({"kind": "ConfigMap", "metadata": {"name": "c"},
               "data": {"u": "http://planted0123456789abcdefghijklmnop.monitoring.svc:9999/", "v": "http://plain-svc.monitoring.svc:9999/"}}))
        proc = self._py(code, ".", cwd=root)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("http://(a label not of a host-label shape, or of a credential's shape).monitoring.svc:9999 names Service (a label not of a host-label shape, or of a credential's shape).monitoring, which this repo neither declares", proc.stdout)
        self.assertIn("http://plain-svc.monitoring.svc:9999 names Service plain-svc.monitoring, which this repo neither declares", proc.stdout)
        self.assertNotIn("planted0123", proc.stdout + proc.stderr)
