#!/usr/bin/env python3
"""
The alert-route checker on the sealed plane — run as
`python3 scripts/test_am_routes_sealed.py`. No cluster, no credentials, no network.

WHY. `scripts/verify/am-routes.py` answers "where would an alert actually GO?", and its
last two rows answer the other half: does the credential Alertmanager PRESENTS to the
intake actually exist? Those rows were written against the retired secret plane — they
look for an ExternalSecret named alertmanager-intake in the monitoring namespace. That
producer is gone: the platform's Secrets are now rendered by the sops operator from sealed
files the desk applies, and core's own monitoring config ships no producer at all. So the
checker fails on the tree core now ships, which is a false red, and it cannot check the
sealed plane at all, which is a hole.

WHAT THIS PINS.
  * The Secrets to look for are DERIVED from the Alertmanager values (`secrets:` mounts
    them by name), never hard-coded — a mount added tomorrow is checked tomorrow.
  * Given sealed files, the checker proves each mounted Secret is rendered by one of them
    and that the intake's bearer carries the key the intake verifies.
  * Given a tree with no producer (core's own template), the rows KEY OFF with a visible
    reason and the run stays green — a check that cannot run says so.
  * No value ever reaches the output. A sealed file's ciphertext, and anything that looks
    like it, stays out of stdout: this runs in CI, where stdout is a log.
"""
import pathlib
import re
import copy
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
CHECKER = ROOT / "scripts" / "verify" / "am-routes.py"
STACK = (ROOT / "template" / "provisioning" / "clusters" / "platform" / "apps"
         / "kube-prometheus-stack.yaml")
CORE_SECRETS = (ROOT / "template" / "provisioning" / "clusters" / "platform" / "config"
                / "monitoring" / "secrets.yaml")

# The three Secrets the stack mounts today. The checker must read them from the values,
# not from a list like this one — this copy exists only to build the fixture.
MOUNTED = ["alertmanager-telegram", "alertmanager-intake", "orbit-alert-webhook"]
# The key is the file Alertmanager reads: the last element of credentials_file
# (/etc/alertmanager/secrets/alertmanager-intake/token), which the mount serves from the
# Secret key of that name. Sweep area 1 (2026-09-13): the checker used to demand a key no
# instance seals, and stage3 never reached this row because it passed the wrong path.
INTAKE_KEY = "token"
INTAKE_URL = "http://orbit-agents-intake.orbit-agents-prod.svc/webhook/alertmanager"
CIPHER = "ENC[AES256_GCM,data:fixture-ciphertext,iv:fixture-iv,tag:fixture-tag,type:str]"


def sealed(name: str, keys: list, template_name: str = None) -> str:
    """A sealed SopsSecret as the desk applies it: one secretTemplate, keys with
    ciphertext values, an age recipient block."""
    lines = ["apiVersion: isindir.github.com/v1alpha3",
             "kind: SopsSecret",
             "metadata:",
             f"  name: {name}",
             "  namespace: monitoring",
             "spec:",
             "  secretTemplates:",
             f"    - name: {template_name or name}",
             "      data:"]
    lines += [f"        {k}: {CIPHER}" for k in keys]
    lines += ["sops:",
              "  age:",
              "    - recipient: age1fixturepublickey000000000000000000000000000000000",
              "      enc: |",
              "        -----BEGIN AGE ENCRYPTED FILE-----",
              "        YWdlLWVuY3J5cHRpb24ub3JnL3YxCi0+IFgyNTUxOSBmaXh0dXJlCmZpeHR1cmU=",
              "        -----END AGE ENCRYPTED FILE-----"]
    return "\n".join(lines) + "\n"


def seal_tree(files: dict) -> pathlib.Path:
    """A sealed directory as an instance carries it: clusters/platform/secrets/monitoring/."""
    root = pathlib.Path(tempfile.mkdtemp(prefix="orbit-am-sealed-"))
    d = root / "clusters" / "platform" / "secrets" / "monitoring"
    d.mkdir(parents=True)
    for name, text in files.items():
        (d / f"{name}.yaml").write_text(text)
    return d


def run(secrets: pathlib.Path = None, producers_optional: bool = False):
    argv = [sys.executable, str(CHECKER), "app", str(STACK)]
    if secrets is not None:
        argv += ["--secrets", str(secrets)]
    if producers_optional:
        argv.append("--producers-optional")
    return subprocess.run(argv, cwd=ROOT, capture_output=True, text=True)


def fails(proc):
    return [l for l in proc.stdout.splitlines() if l.lstrip().startswith("FAIL")]


def run_on(stack_text: str, catalog: dict | None = None, secrets: pathlib.Path | None = None):
    """The checker over a MODIFIED stack (and a catalog): the texts are written to a scratch
    directory, so the shipped template is never touched."""
    root = pathlib.Path(tempfile.mkdtemp(prefix="orbit-am-stack-"))
    stack = root / "kube-prometheus-stack.yaml"; stack.write_text(stack_text)
    argv = [sys.executable, str(CHECKER), "app", str(stack)]
    if catalog is not None:
        cat = root / "projects.yaml"; cat.write_text(yaml.safe_dump(catalog)); argv += ["--catalog", str(cat)]
    if secrets is not None:
        argv += ["--secrets", str(secrets)]
    return subprocess.run(argv, cwd=ROOT, capture_output=True, text=True)


ONE_TRIGGER = {"agents": [{"name": "oom", "triggers": [{"type": "logs", "name": "spike", "labels": {"team": "sre"}}]}]}
# an alert-only catalog: no derived alert arms the plane, one declared alert must still be delivered
ALERT_ONLY = {"agents": [{"name": "kube", "triggers": [{"type": "alert", "match": {"alertname": "KubePodCrashLooping", "team": "sre"}}]}]}


def run_raw(live_cfg: dict, desired_text: str | None, catalog: dict | None = None):
    """The checker in raw mode over a RUNNING config (as /api/v2/status serves it), with
    the git config handed over as --desired when given."""
    root = pathlib.Path(tempfile.mkdtemp(prefix="orbit-am-raw-"))
    live = root / "am-live.yaml"; live.write_text(yaml.safe_dump(live_cfg))
    argv = [sys.executable, str(CHECKER), "raw", str(live)]
    if desired_text is not None:
        want = root / "kube-prometheus-stack.yaml"; want.write_text(desired_text); argv += ["--desired", str(want)]
    if catalog is not None:
        cat = root / "projects.yaml"; cat.write_text(yaml.safe_dump(catalog)); argv += ["--catalog", str(cat)]
    return subprocess.run(argv, cwd=ROOT, capture_output=True, text=True)


def live_view_of(stack_text: str) -> dict:
    """What Alertmanager v0.33 serves as config.original for this git config: the same
    structure, every secret-typed value (the webhook URL among them) shown as <secret>."""
    doc = yaml.safe_load(stack_text)
    helm = doc["spec"]["source"]["helm"]
    vals = helm.get("valuesObject") or yaml.safe_load(re.sub(r"\{\{[^{}]*\}\}", "PLACEHOLDER", helm["values"]))
    cfg = copy.deepcopy(vals["alertmanager"]["config"])
    for r in cfg.get("receivers") or []:
        for w in r.get("webhook_configs") or []:
            if "url" in w: w["url"] = "<secret>"
    return cfg


class LiveRedactedTest(unittest.TestCase):
    """Round 3 (2026-09-13, auditor): the running config redacts the webhook URL, so a
    correctly wired instance's live row read "no intake receiver". The live intake is
    the git config's intake (found by URL) matched by NAME, and its live URL must be
    the intake URL or the redaction marker."""

    def test_the_running_config_with_a_redacted_url_is_green_given_the_git_config(self):
        text = STACK.read_text()
        proc = run_raw(live_view_of(text), text, ONE_TRIGGER)
        self.assertEqual(proc.returncode, 0, proc.stdout[-3000:])
        self.assertTrue(any("carries the git config's intake receiver" in l for l in proc.stdout.splitlines()), proc.stdout)

    def test_without_the_git_config_a_redacted_url_is_no_intake(self):
        proc = run_raw(live_view_of(STACK.read_text()), None, ONE_TRIGGER)
        self.assertNotEqual(proc.returncode, 0, "a redacted URL passed as the intake with nothing to match it against:\n" + proc.stdout[-2000:])

    def test_a_live_receiver_of_the_right_name_but_another_door_is_red(self):
        text = STACK.read_text()
        live = live_view_of(text)
        for r in live["receivers"]:
            for w in r.get("webhook_configs") or []:
                w["url"] = "http://attacker.example/webhook/alertmanager"
        proc = run_raw(live, text, ONE_TRIGGER)
        self.assertNotEqual(proc.returncode, 0, "a renamed door passed by name alone:\n" + proc.stdout[-2000:])

    def test_a_git_config_whose_intake_was_renamed_still_identifies_the_live_one(self):
        text = STACK.read_text().replace("orbit-intake", "agents-webhook")
        proc = run_raw(live_view_of(text), text, ONE_TRIGGER)
        self.assertEqual(proc.returncode, 0, proc.stdout[-3000:])


class Round4Test(unittest.TestCase):
    """Area-1 sweep round 4: YAML errors abort by class, the mount path is the whole
    contract, a regex error names no pattern, and a runaway regex has a CPU budget."""

    def _run(self, args, cwd=None):
        return subprocess.run([sys.executable, str(CHECKER), *args], cwd=cwd, capture_output=True, text=True, timeout=120)

    def test_an_unreadable_config_or_catalog_aborts_by_class(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            (root / "bad.yaml").write_text("a: [PLANTED_CREDENTIAL_123456789\n")
            (root / "list.yaml").write_text("- PLANTED_CREDENTIAL_123456789\n")
            (root / "cat.yaml").write_text("a: [PLANTED_CREDENTIAL_123456789\n")
            for args, needle in ((["raw", "bad.yaml"], "the running Alertmanager config bad.yaml cannot be read as YAML (ParserError)"),
                                 (["raw", "list.yaml"], "the running Alertmanager config list.yaml is not a YAML map"),
                                 (["app", "bad.yaml"], "the Alertmanager Application bad.yaml cannot be read as YAML (ParserError)"),
                                 (["app", "list.yaml"], "does not carry alertmanager.config the way the chart values do"),
                                 (["raw", "missing.yaml"], "cannot be read as YAML (FileNotFoundError)")):
                with self.subTest(args=args):
                    proc = self._run(args, cwd=root)
                    self.assertNotEqual(proc.returncode, 0)
                    self.assertIn(needle, proc.stdout + proc.stderr)
                    self.assertNotIn("PLANTED", proc.stdout + proc.stderr)
            (root / "app.yaml").write_text(STACK.read_text())
            proc = self._run(["app", "app.yaml", "--catalog", "cat.yaml"], cwd=root)
            self.assertIn("the catalog cat.yaml cannot be read as YAML (ParserError)", proc.stdout + proc.stderr)
            self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_the_intake_authorization_scheme_must_be_bearer(self):
        """Round 5 (auditor): a correct credentials_file with `type: Token` passed; the
        intake accepts Bearer only (omitted = Bearer, Alertmanager's default)."""
        base = STACK.read_text()
        self.assertEqual(base.count("                        type: Bearer\n"), 2)
        for cfg in ("                        type: Token\n", "                        type: Basic\n"):
            with self.subTest(cfg=cfg.strip()):
                proc = run_on(base.replace("                        type: Bearer\n", cfg, 1) if "intake/token" in base.split(cfg.replace(cfg, "                        type: Bearer\n"))[0] else base.replace("                        type: Bearer\n", cfg))
                self.assertTrue(any("authorization scheme is Bearer" in l for l in fails(proc)), proc.stdout)
        proc = run_on(base.replace("                        type: Bearer\n", "", 1), catalog=None)
        self.assertFalse(any("authorization scheme is Bearer" in l for l in fails(proc)), proc.stdout)

    def test_an_alertname_or_receiver_is_repeated_only_with_a_label_shape(self):
        """A2 (round 7): the suppressed and unrouted rows interpolated the catalog's
        alertnames and the config's receiver lists verbatim."""
        catalog = {"agents": [{"name": "kube", "triggers": [
            {"type": "alert", "match": {"alertname": "PLANTED TOKEN 1234567890", "severity": "warning"}}]}]}
        # the root receiver (where an unmatched alert lands) renamed to a non-name: the
        # unrouted row shows the receiver list by shape, so the name is counted, not shown
        text = STACK.read_text()
        self.assertEqual(text.count("receiver: telegram\n"), 2); self.assertEqual(text.count("              - name: telegram\n"), 1)
        text = text.replace("receiver: telegram\n", "receiver: PLANTED receiver\n").replace("              - name: telegram\n", "              - name: PLANTED receiver\n")
        proc = run_on(text, catalog=catalog)
        rows = fails(proc)
        self.assertTrue(any(r'''(a value not of a label shape, or of a credential\'s shape) -> ["(+1 name(s) not of a name shape, or of a credential\'s shape)"]''' in l for l in rows), proc.stdout)
        self.assertTrue(any("(a value not of a label shape, or of a credential's shape)/severity=warning <- inhibit_rules[" in l for l in rows), proc.stdout)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_a_deprecated_match_on_an_absent_label_compares_against_the_empty_string(self):
        """A5 (round 8): Alertmanager reads an absent label as "" in a `match`, so
        `match: {namespace: ""}` catches every expression-only alert; the simulator
        skipped that route and certified the intake routing."""
        base = STACK.read_text()
        anchor = "                - matchers: ['alertname =~ \"^OrbitAgent.*\"']\n                  receiver: orbit-intake\n"
        self.assertEqual(base.count(anchor), 1)
        thief = "                - match: {namespace: \"\"}\n                  receiver: telegram\n" + anchor
        proc = run_on(base.replace(anchor, thief), catalog=ONE_TRIGGER)
        self.assertTrue(any("reaches the intake" in l for l in fails(proc)), proc.stdout)
        proc = run_on(base, catalog=ONE_TRIGGER)
        self.assertFalse(any("reaches the intake" in l for l in fails(proc)), proc.stdout)

    def test_a_sealed_producer_counts_only_in_the_namespace_alertmanager_mounts_from(self):
        """B8 (round 8): the sealed index was keyed by name alone, so Secrets sealed
        into another namespace satisfied Alertmanager's mounts."""
        files = {n: sealed(n, [INTAKE_KEY] if n == "alertmanager-intake" else ["value"]).replace("  namespace: monitoring", "  namespace: wrong-namespace")
                 for n in MOUNTED}
        proc = run(seal_tree(files))
        self.assertNotEqual(proc.returncode, 0, proc.stdout[-2000:])
        rows = fails(proc)
        for n in MOUNTED:
            self.assertTrue(any(f"mounted Secret {n} is rendered by a sealed SopsSecret in monitoring" in l for l in rows), "\n".join(rows))

    def test_a_mount_entry_reaches_a_row_only_as_a_secret_name(self):
        """A3 (round 9): alertmanagerSpec.secrets entries were interpolated into the
        mounted-Secret rows unvalidated; so was the destination namespace."""
        base = STACK.read_text()
        # the mount list is a block list (its Telegram entry sits in a feature block)
        anchor = "              - orbit-alert-webhook\n"
        self.assertEqual(base.count(anchor), 1)
        proc = run_on(base.replace(anchor, anchor + "              - \"Bearer PLANTED_MOUNT_1234567890\"\n"), catalog=ONE_TRIGGER, secrets=CORE_SECRETS.parent)
        self.assertNotEqual(proc.returncode, 0)
        self.assertTrue(any("alertmanagerSpec.secrets[" in l and "is a Secret name" in l for l in fails(proc)), proc.stdout)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)
        self.assertEqual(base.count("    namespace: monitoring\n"), 1)
        proc = run_on(base.replace("    namespace: monitoring\n", "    namespace: PLANTED ns\n"), catalog=ONE_TRIGGER)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("destination namespace that is not a namespace name", proc.stdout + proc.stderr)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_syntax_re2_lacks_is_refused_before_it_is_simulated(self):
        """B9 (round 9): a lookahead in the intake matcher compiled here (Python) while
        Alertmanager (RE2) would refuse the whole config."""
        base = STACK.read_text()
        anchor = "'alertname =~ \"^OrbitAgent.*\"'"
        self.assertEqual(base.count(anchor), 1)
        for pat in ("(?=OrbitAgent)PLANTED.*", "(?<!x)PLANTED.*", "(PLANTED)\\\\1", "PLANTED++", "PLANTED\\\\Z"):
            with self.subTest(pat=pat[:8]):
                proc = run_on(base.replace(anchor, "'alertname =~ \"" + pat + "\"'"), catalog=ONE_TRIGGER)
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("RE2 engine does not have", proc.stdout + proc.stderr)
                self.assertNotIn("PLANTED", proc.stdout + proc.stderr)
        proc = run_on(base.replace(anchor, "'alertname =~ \"^(?P<n>OrbitAgent).*\"'"), catalog=ONE_TRIGGER)
        self.assertNotIn("RE2 engine does not have", proc.stdout + proc.stderr)

    def test_the_intake_receiver_carries_exactly_one_webhook(self):
        """Round 5 (breaker): a second intake webhook entry (send_resolved: true, its own
        URL) was never read — every row reads entry [0]."""
        base = STACK.read_text()
        first = base.index("                  - url: http://orbit-agents-intake.orbit-agents-prod.svc/webhook/alertmanager\n")
        end = base.index("\n              - name:", first)
        entry = base[first:end]
        two = base[:end] + "\n" + entry.replace("send_resolved: false", "send_resolved: true") + base[end:]
        proc = run_on(two)
        self.assertTrue(any("exactly one webhook entry" in l for l in fails(proc)), proc.stdout)
        self.assertFalse(any("exactly one webhook entry" in l for l in fails(run_on(base))))

    def test_config_names_in_diagnostics_are_shown_only_when_name_shaped(self):
        """A3 (round 6): a mute interval named `Bearer PLANTED...` was printed verbatim in
        the "intervals on the path" row."""
        base = STACK.read_text()
        self.assertEqual(base.count("            route:\n"), 1); self.assertEqual(base.count(RealAlertsTest.ROUTE), 1)
        # the interval sits on the intake route itself: intervals are the terminal route's own
        # (round 18), and Alertmanager refuses one on the root route
        two = base.replace("            route:\n",
                           "            time_intervals:\n            - name: Bearer PLANTED_CREDENTIAL_123\n"
                           "              time_intervals:\n              - weekdays: ['monday:sunday']\n"
                           "            route:\n").replace(RealAlertsTest.ROUTE, RealAlertsTest.ROUTE + "                  mute_time_intervals: [Bearer PLANTED_CREDENTIAL_123]\n")
        proc = run_on(two)
        self.assertNotEqual(proc.returncode, 0)
        self.assertTrue(any("intervals on the path" in l and "(+1 name(s) not of a name shape, or of a credential's shape)" in l for l in fails(proc)), proc.stdout)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_every_matcher_is_parsed_before_any_route_is_simulated(self):
        """A4 (round 10): first-match-wins evaluation stopped at the first miss in a
        matcher list and never descended below an unmatched parent, so a matcher
        Alertmanager cannot read (a lookahead) behind either passed unread."""
        base = STACK.read_text()
        self.assertEqual(base.count("              routes:\n                # Watchdog"), 1)
        for label, text in (
            ("second matcher behind a miss", base.replace("              routes:\n                # Watchdog",
                 "              routes:\n                - matchers: ['alertname = \"NoSuchAlertEver\"', 'severity =~ \"(?=PLANTED)\"']\n                  receiver: \"null\"\n                # Watchdog")),
            ("child below an unmatched parent", base.replace("              routes:\n                # Watchdog",
                 "              routes:\n                - matchers: ['alertname = \"NoSuchAlertEver\"']\n                  receiver: \"null\"\n                  routes:\n                  - matchers: ['team =~ \"(?<=PLANTED)\"']\n                    receiver: \"null\"\n                # Watchdog")),
            ("inhibit rule", base.replace("                target_matchers: ['severity = info',", "                target_matchers: ['severity =~ \"(?!PLANTED)\"',")),
        ):
            with self.subTest(label=label):
                self.assertNotEqual(text, base, label)
                proc = run_on(text)
                self.assertNotEqual(proc.returncode, 0, proc.stdout)
                self.assertIn("every matcher is parsed before any route is simulated", proc.stdout + proc.stderr)
                self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_the_bearer_key_row_names_nothing_from_a_path_that_failed_the_contract(self):
        """B3 (round 6): a credentials_file ending in `/PLANTED password value` failed the
        mount-contract row and was then repeated by the sealed-key row."""
        base = STACK.read_text()
        self.assertEqual(base.count("credentials_file: /etc/alertmanager/secrets/alertmanager-intake/token\n"), 1)
        proc = run_on(base.replace("credentials_file: /etc/alertmanager/secrets/alertmanager-intake/token\n",
                                   "credentials_file: /etc/alertmanager/secrets/alertmanager-intake/PLANTED password value\n"),
                      secrets=CORE_SECRETS.parent)
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_a_regex_error_names_no_pattern(self):
        src = CHECKER.read_text()
        self.assertIn('raise Unparseable(expr, "a regex that does not compile (the engine\'s message is not "', src)
        self.assertNotIn("does not compile ({exc})", src)

    def test_the_checker_has_a_cpu_budget(self):
        """Round 12 (auditor A3): the budget bound only __main__, so the stage3 mutation
        harness, which imports the module and evaluates matchers itself, had none."""
        src = CHECKER.read_text()
        self.assertIn("resource.setrlimit(resource.RLIMIT_CPU, (want, want))", src)
        self.assertIn("\ncpu_budget()\n", src)
        proc = subprocess.run([sys.executable, "-c", "import importlib.util, resource, sys\n"
                               "spec = importlib.util.spec_from_file_location('amr', sys.argv[1]); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
                               "print(resource.getrlimit(resource.RLIMIT_CPU)[0])", str(CHECKER)], capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertLessEqual(int(proc.stdout.strip()), 60)

    def test_re2_repetition_and_posix_classes_are_read_like_alertmanager(self):
        """Round 14 (breaker B6): Python accepted `{0,1001}` that RE2 refuses (Alertmanager
        would reject the whole config), and rejected `[[:alnum:]]` that RE2 accepts."""
        proc = subprocess.run([sys.executable, "-c", "import importlib.util, sys\n"
                               "spec = importlib.util.spec_from_file_location('amr', sys.argv[1]); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
                               "print(bool(m.compile_re('[[:alnum:]]+', 'x').fullmatch('ab9')))\n"
                               "print(m.compile_re('[[:alnum:]]+', 'x').fullmatch('a-b') is None)\n"
                               "try: m.compile_re('a{0,1001}', 'x'); print('accepted')\n"
                               "except Exception as e: print(type(e).__name__, 'PLANTED' in str(e))\n"
                               "try: m.compile_re('[[:nope:]]', 'x'); print('accepted')\n"
                               "except Exception as e: print(type(e).__name__)\n"
                               "print(bool(m.compile_re('a{1000}', 'x')))", str(CHECKER)], capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.split("\n")[:5], ["True", "True", "Unparseable False", "Unparseable", "True"])

    def test_the_mount_path_is_the_whole_contract(self):
        src = CHECKER.read_text()
        self.assertNotIn("len(mount) > 4 and mount[4] in secrets", src)
        self.assertIn('len(mount) == 6 and mount[:4] == ["", "etc", "alertmanager", "secrets"]', src)


class IntakeUrlTest(unittest.TestCase):
    """Sweep area 1 (2026-09-13): the intake receiver is recognised by the WHOLE URL read as
    a URL (the in-cluster service, the path, nothing in front of the host), not by the
    words appearing somewhere in it."""

    def test_the_shipped_url_is_the_intake_and_a_lookalike_is_not(self):
        text = STACK.read_text()
        self.assertEqual(text.count(INTAKE_URL), 1)
        self.assertEqual(run_on(text).returncode, 0, run_on(text).stdout[-2000:])
        for bad in ("https://attacker.example/orbit-agents-intake/webhook/alertmanager",
                    "http://orbit-agents-intake.attacker.example.com/webhook/alertmanager",
                    "http://user@orbit-agents-intake.orbit-agents-prod.svc/webhook/alertmanager",
                    "http://orbit-agents-intake.orbit-agents-prod.svc/other/webhook/alertmanager",
                    # round 2 (auditor): another namespace's service, a bare name (which resolves
                    # in Alertmanager's own namespace), another port, another transport
                    "http://orbit-agents-intake.default.svc/webhook/alertmanager",
                    "http://orbit-agents-intake/webhook/alertmanager",
                    "http://orbit-agents-intake.orbit-agents-prod.svc:8080/webhook/alertmanager",
                    "https://orbit-agents-intake.orbit-agents-prod.svc/webhook/alertmanager"):
            with self.subTest(url=bad):
                proc = run_on(text.replace(INTAKE_URL, bad))
                self.assertNotEqual(proc.returncode, 0, "a lookalike intake URL stayed green:\n" + proc.stdout[-2000:])
        for good in ("http://orbit-agents-intake.orbit-agents-prod/webhook/alertmanager",
                     "http://orbit-agents-intake.orbit-agents-prod.svc:80/webhook/alertmanager",
                     "http://orbit-agents-intake.orbit-agents-prod.svc.cluster.local/webhook/alertmanager"):
            with self.subTest(url=good):
                self.assertEqual(run_on(text.replace(INTAKE_URL, good)).returncode, 0)


class RealAlertsTest(unittest.TestCase):
    """Sweep area 1 (2026-09-13): inhibition and time intervals are judged over the alerts
    this catalog's triggers actually fire, with their own labels, not only the probes."""

    STOCK_LAST = "              - target_matchers: ['alertname = InfoInhibitor']\n"
    ROUTE = "                - matchers: ['alertname =~ \"^OrbitAgent.*\"']\n                  receiver: orbit-intake\n"

    def test_an_inhibit_rule_keyed_on_a_trigger_label_is_red(self):
        text = STACK.read_text()
        self.assertEqual(text.count(self.STOCK_LAST), 1)
        self.assertEqual(run_on(text, ONE_TRIGGER).returncode, 0, run_on(text, ONE_TRIGGER).stdout[-2000:])
        rule = self.STOCK_LAST + "              - source_matchers: ['alertname = Watchdog']\n                target_matchers: ['team = sre']\n"
        proc = run_on(text.replace(self.STOCK_LAST, rule), ONE_TRIGGER)
        self.assertNotEqual(proc.returncode, 0, "a rule that holds the real alert stayed green:\n" + proc.stdout[-2000:])
        self.assertTrue(any("SUPPRESSED" in l and "OrbitAgentOomSpike" in l for l in fails(proc)), "\n".join(fails(proc)))

    def test_a_time_interval_on_the_intake_route_is_red(self):
        """A child route keyed on a label only the real alert carries, with a mute
        interval: the probes never see it, the trigger's alert is silenced by it."""
        text = STACK.read_text()
        self.assertEqual(text.count(self.ROUTE), 1)
        muted = self.ROUTE + ("                  routes:\n                    - matchers: ['team = sre']\n"
                              "                      receiver: orbit-intake\n                      mute_time_intervals: [offhours]\n")
        proc = run_on(text.replace(self.ROUTE, muted), ONE_TRIGGER)
        self.assertNotEqual(proc.returncode, 0, "a muted intake route stayed green:\n" + proc.stdout[-2000:])
        self.assertTrue(any("time interval" in l and "offhours" in l for l in fails(proc)), "\n".join(fails(proc)))

    def test_a_human_receiver_is_one_that_carries_a_notifier(self):
        """Round 20 (auditor A2): `human` was every receiver but the intake and null, by
        NAME — a telegram receiver emptied of its telegram_configs still counted as human
        delivery, and the intake carrying them passed the no-human assertion."""
        text = STACK.read_text()
        head, _, rest = text.partition("              - name: telegram\n")
        block_end = rest.index("\n              - name: ")
        tg_block, tail = rest[:block_end + 1], rest[block_end + 1:]
        self.assertIn("telegram_configs:", tg_block)
        moved = head + "              - name: telegram\n" + tail.replace("              - name: orbit-intake\n", "              - name: orbit-intake\n" + tg_block, 1)
        proc = run_on(moved, ONE_TRIGGER)
        self.assertNotEqual(proc.returncode, 0, proc.stdout[-2000:])
        self.assertTrue(any("the intake receiver carries no human notifier" in l and "telegram_configs" in l for l in fails(proc)), "\n".join(fails(proc)))
        self.assertTrue(any("default (human) receiver" in l for l in fails(proc)), "\n".join(fails(proc)))

    def test_a_regex_comment_group_is_refused_like_every_other_python_only_form(self):
        """Round 20 (auditor A4): RE2 has no (?#comment) groups; Python accepted one and
        routed as if it were not there."""
        text = STACK.read_text(); self.assertEqual(text.count(self.ROUTE), 1)
        proc = run_on(text.replace(self.ROUTE, self.ROUTE.replace('"^OrbitAgent.*"', '"^OrbitAgent(?#c).*"')), ONE_TRIGGER)
        self.assertNotEqual(proc.returncode, 0)
        self.assertTrue(any("RE2 engine does not have" in l and "comment group" in l for l in fails(proc)), "\n".join(fails(proc)))
        self.assertNotIn("(?#c)", proc.stdout)

    def test_a_time_interval_is_the_terminal_routes_own(self):
        """Round 18 (auditor A4): Alertmanager's NewRoute assigns each route's intervals
        from its own declaration (dispatch/route.go) — a child declaring none has none,
        whatever its parent declares; the simulation inherited the parent's and went red
        on a delivery Alertmanager makes. A root-route interval is refused outright."""
        text = STACK.read_text()
        self.assertEqual(text.count(self.ROUTE), 1)
        parent = ("                - matchers: ['alertname =~ \"^Orbit.*\"']\n                  receiver: orbit-intake\n"
                  "                  mute_time_intervals: [offhours]\n                  routes:\n"
                  "                    - matchers: ['alertname =~ \"^OrbitAgent.*\"']\n                      receiver: orbit-intake\n")
        proc = run_on(text.replace(self.ROUTE, parent), ONE_TRIGGER)
        self.assertEqual(proc.returncode, 0, "a parent's interval was inherited by the terminal route:\n" + proc.stdout[-2000:])
        self.assertFalse(any("time interval" in l and "offhours" in l for l in fails(proc)))
        base = text.replace("            route:\n", "            route:\n              mute_time_intervals: [offhours]\n")
        proc = run_on(base, ONE_TRIGGER)
        self.assertNotEqual(proc.returncode, 0)
        self.assertTrue(any("a time interval on the root route" in l for l in fails(proc)), "\n".join(fails(proc)))

    def test_an_inhibit_row_names_the_rule_by_index_never_by_its_matchers(self):
        """Round 3 (auditor): the red row printed the rule's source matchers, which are
        config text and may hold anything."""
        text = STACK.read_text()
        rule = self.STOCK_LAST + "              - source_matchers: ['api_key = \"PLANTED-MATCHER-VALUE\"']\n                target_matchers: ['team = sre']\n"
        proc = run_on(text.replace(self.STOCK_LAST, rule), ONE_TRIGGER)
        self.assertNotEqual(proc.returncode, 0, proc.stdout[-2000:])
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)
        self.assertTrue(any("inhibit_rules[" in l for l in fails(proc)), "\n".join(fails(proc)))

    def test_a_regex_matcher_is_read_with_alertmanagers_ascii_classes(self):
        """Round 3 (auditor): Alertmanager's RE2 treats \\d, \\w, \\s as ASCII; Python's
        are Unicode unless told. A thief route above the intake keyed on `team =~ \\D+`
        steals an alert whose team label is an Arabic-Indic digit — Python said no."""
        text = STACK.read_text()
        thief = ("                - match_re:\n                    team: '\\D+'\n                  receiver: telegram\n") + self.ROUTE
        catalog = {"agents": [{"name": "oom", "triggers": [{"type": "logs", "name": "spike", "labels": {"team": "\u0661"}}]}]}
        proc = run_on(text.replace(self.ROUTE, thief), catalog)
        self.assertNotEqual(proc.returncode, 0, "a route that steals the alert under RE2 semantics stayed green:\n" + proc.stdout[-2000:])

    def test_a_route_that_requires_the_namespace_label_is_red(self):
        """Round 2 (auditor): the simulator invented a namespace label the generator never
        sets (an alert carries one only when its series does; `vector(1)` carries none),
        so a route keyed on the plane's namespace passed simulation and lost every
        expression-only alert. Every alert is simulated both ways now."""
        text = STACK.read_text()
        narrowed = text.replace(self.ROUTE, "                - matchers: ['alertname =~ \"^OrbitAgent.*\"', 'namespace = orbit-agents-prod']\n                  receiver: orbit-intake\n")
        self.assertNotEqual(narrowed, text)
        proc = run_on(narrowed, ONE_TRIGGER)
        self.assertNotEqual(proc.returncode, 0, "a route requiring the namespace label stayed green:\n" + proc.stdout[-2000:])
        self.assertTrue(any("misrouted" in l or "unrouted" in l for l in fails(proc)), "\n".join(fails(proc)))

    def test_an_alert_only_catalog_needs_the_bearer_file_too(self):
        """Round 2 (auditor): the receiver's URL, resolve and credential rows sat under the
        armed branch; the intake authenticates every Alertmanager request, so an
        alert-only catalog inlining its credential must go red too."""
        declared_route = ("                - matchers: ['alertname = KubePodCrashLooping']\n"
                          "                  receiver: orbit-intake\n")
        text = STACK.read_text().replace(self.ROUTE, declared_route + self.ROUTE)
        self.assertIn("credentials_file:", text)
        inlined = text.replace("credentials_file:", "credentials:", 1)
        proc = run_on(inlined, ALERT_ONLY)
        self.assertNotEqual(proc.returncode, 0, "an inlined bearer stayed green for an alert-only catalog:\n" + proc.stdout[-2000:])
        self.assertTrue(any("never inlined" in l for l in fails(proc)), "\n".join(fails(proc)))

    def test_an_alert_only_catalog_still_gets_the_interval_and_inhibition_rows(self):
        """Round 2 (breaker): the interval and inhibition rows sat under the ARMED branch,
        which an alert-only catalog never enters, so its declared alert could be muted or
        suppressed with the checker green."""
        # the instance's own route delivering its declared alert, the way an alert-only orbit wires one
        declared_route = ("                - matchers: ['alertname = KubePodCrashLooping']\n"
                          "                  receiver: orbit-intake\n")
        text = STACK.read_text().replace(self.ROUTE, declared_route + self.ROUTE)
        self.assertEqual(run_on(text, ALERT_ONLY).returncode, 0, run_on(text, ALERT_ONLY).stdout[-2000:])
        muted = declared_route + ("                  routes:\n                    - matchers: ['team = sre']\n"
                                  "                      receiver: orbit-intake\n                      mute_time_intervals: [offhours]\n")
        proc = run_on(text.replace(declared_route, muted), ALERT_ONLY)
        self.assertNotEqual(proc.returncode, 0, "a muted route over a declared alert stayed green:\n" + proc.stdout[-2000:])
        self.assertTrue(any("time interval" in l and "offhours" in l for l in fails(proc)), "\n".join(fails(proc)))
        rule = self.STOCK_LAST + "              - source_matchers: ['alertname = Watchdog']\n                target_matchers: ['team = sre']\n"
        proc = run_on(text.replace(self.STOCK_LAST, rule), ALERT_ONLY)
        self.assertNotEqual(proc.returncode, 0, "a rule that holds the declared alert stayed green:\n" + proc.stdout[-2000:])
        self.assertTrue(any("SUPPRESSED" in l and "KubePodCrashLooping" in l for l in fails(proc)), "\n".join(fails(proc)))


class SealedProducerTest(unittest.TestCase):
    """Given the sealed files, the checker proves the bearer exists."""

    def test_a_bearer_sealed_under_stringdata_is_a_sealed_key(self):
        """Round 3 (breaker): the index read data: only; stringData: is the other key map
        the operator renders."""
        files = {n: sealed(n, ["value"]) for n in MOUNTED}
        files["alertmanager-intake"] = sealed("alertmanager-intake", [INTAKE_KEY]).replace("      data:", "      stringData:")
        proc = run(seal_tree(files))
        self.assertEqual(proc.returncode, 0, proc.stdout[-2000:])

    def test_the_key_is_the_file_the_bearer_path_names(self):
        """The old key name (INTAKE_ALERTMANAGER_TOKEN) is what the intake's environment
        variable is called, not what the mount serves; a tree sealing it instead of
        `token` is red, naming the key the path needs."""
        files = {n: sealed(n, ["INTAKE_ALERTMANAGER_TOKEN"] if n == "alertmanager-intake" else ["value"])
                 for n in MOUNTED}
        proc = run(seal_tree(files))
        self.assertNotEqual(proc.returncode, 0, proc.stdout[-2000:])
        self.assertTrue(any("'token'" in l for l in fails(proc)), "\n".join(fails(proc)))

    def full_tree(self, **override):
        files = {n: sealed(n, [INTAKE_KEY] if n == "alertmanager-intake" else ["value"])
                 for n in MOUNTED}
        files.update(override)
        return seal_tree(files)

    def test_two_sealed_producers_of_the_bearer_are_red(self):
        """Round 18 (auditor A3): two SopsSecrets rendering monitoring/alertmanager-intake
        had their keys unioned — a Secret neither of them renders."""
        second = sealed("alertmanager-intake-again", ["other"], template_name="alertmanager-intake")
        proc = run(self.full_tree(**{"alertmanager-intake-again": second}))
        self.assertNotEqual(proc.returncode, 0, proc.stdout[-2000:])
        self.assertTrue(any("two sealed producers render Secret monitoring/alertmanager-intake" in l for l in fails(proc)), "\n".join(fails(proc)))

    def test_a_sealed_file_name_is_shown_only_in_file_name_shape(self):
        """Round 21 (auditor A3): the parse-error row and the duplicate-producer row
        repeated a sealed file's path; a file named after a credential put it in a row
        stage3 forwards."""
        odd = "Authorization: Bearer PLANTED_CANARY_0123456789"
        proc = run(self.full_tree(**{odd: "a: ["}))
        self.assertNotEqual(proc.returncode, 0, proc.stdout[-2000:])
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)
        self.assertTrue(any("/(a name not of a file-name shape, or of a credential's shape) does not parse" in l for l in fails(proc)), "\n".join(fails(proc)))
        second = sealed("alertmanager-intake-again", ["other"], template_name="alertmanager-intake")
        proc = run(self.full_tree(**{odd: second}))
        self.assertNotEqual(proc.returncode, 0, proc.stdout[-2000:])
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)
        self.assertTrue(any("two sealed producers render Secret monitoring/alertmanager-intake" in l and "/(a name not of a file-name shape, or of a credential's shape)" in l for l in fails(proc)), "\n".join(fails(proc)))

    def test_a_complete_sealed_tree_is_green(self):
        proc = run(self.full_tree())
        self.assertEqual(proc.returncode, 0,
                         "a complete sealed tree is not green:\n" + proc.stdout[-3000:])
        self.assertIn("alertmanager-intake", proc.stdout,
                      "the checker never mentions the bearer's Secret — it is not checking it")

    def test_a_missing_sealed_file_is_red(self):
        files = {n: sealed(n, [INTAKE_KEY] if n == "alertmanager-intake" else ["value"])
                 for n in MOUNTED if n != "alertmanager-intake"}
        proc = run(seal_tree(files))
        self.assertNotEqual(proc.returncode, 0,
                            "the bearer's sealed file is absent and the run stayed green:\n"
                            + proc.stdout[-3000:])
        self.assertTrue(any("alertmanager-intake" in l for l in fails(proc)),
                        "no FAIL line names the missing Secret:\n" + "\n".join(fails(proc)))

    def test_a_file_that_renders_another_name_does_not_count(self):
        """The operator names the Secret after the TEMPLATE, not the file. A sealed file
        called alertmanager-intake that renders something else leaves the mount empty."""
        odd = sealed("alertmanager-intake", [INTAKE_KEY], template_name="intake-old")
        proc = run(self.full_tree(**{"alertmanager-intake": odd}))
        self.assertNotEqual(proc.returncode, 0,
                            "a sealed file rendering the wrong Secret name passed:\n"
                            + proc.stdout[-3000:])
        self.assertNotIn("ExternalSecret", proc.stdout,
                         "the run went red on the retired plane's row, not on the name")
        self.assertTrue(any("alertmanager-intake" in l for l in fails(proc)),
                        "no FAIL line names the Secret the mount expects:\n"
                        + "\n".join(fails(proc)))

    def test_the_bearer_carries_the_key_the_intake_verifies(self):
        wrong = sealed("alertmanager-intake", ["SOME_OTHER_KEY"])
        proc = run(self.full_tree(**{"alertmanager-intake": wrong}))
        self.assertNotEqual(proc.returncode, 0,
                            "the bearer seals the wrong key and the run stayed green:\n"
                            + proc.stdout[-3000:])
        self.assertTrue(any(INTAKE_KEY in l for l in fails(proc)),
                        "no FAIL line names the key the intake verifies:\n"
                        + "\n".join(fails(proc)))

    def test_every_mounted_secret_is_checked_not_just_the_bearer(self):
        """`secrets:` in the values is the list — a mount with no sealed file is a
        Alertmanager that will not start."""
        files = {n: sealed(n, [INTAKE_KEY] if n == "alertmanager-intake" else ["value"])
                 for n in MOUNTED if n != "orbit-alert-webhook"}
        proc = run(seal_tree(files))
        self.assertNotEqual(proc.returncode, 0,
                            "a mounted Secret with no sealed file passed:\n"
                            + proc.stdout[-3000:])
        self.assertTrue(any("orbit-alert-webhook" in l for l in fails(proc)),
                        "the checker only looks at the bearer — the other mounts are "
                        "unchecked:\n" + "\n".join(fails(proc)))


class NoProducerTest(unittest.TestCase):
    """Core's own TEMPLATE ships no producer: with --producers-optional (which only the
    template row passes) the rows key off, they do not fail. Without it — an INSTANCE —
    an empty producer index is a missing producer and the rows go red (round 2, 2026-09-13,
    auditor: an instance that lost its sealed tree keyed every producer row off)."""

    def test_the_repository_tree_is_green(self):
        proc = run(CORE_SECRETS, producers_optional=True)
        self.assertEqual(proc.returncode, 0,
                         "the checker fails on the tree core itself ships:\n"
                         + proc.stdout[-3000:])

    def test_an_instance_with_no_producer_is_red(self):
        proc = run(CORE_SECRETS)
        self.assertNotEqual(proc.returncode, 0, "an empty producer index stayed green without --producers-optional:\n" + proc.stdout[-3000:])
        self.assertTrue(any("mounted Secret" in l for l in fails(proc)), "\n".join(fails(proc)))
        self.assertTrue(any("intake token" in l for l in fails(proc)), "\n".join(fails(proc)))
        text = (ROOT / "scripts" / "verify" / "stage3.sh").read_text()
        self.assertEqual(text.count("--producers-optional"), 1, "stage3 passes the flag for the template row and nowhere else")
        self.assertRegex(text, r'am_rows "alertmanager\(template\)"[^\n]*\n[^\n]*--producers-optional')
        self.assertNotIn("[ -d \"$AM_SEALED\" ] ||", text, "the fallback to the rendered config file is back")

    def test_the_keyed_off_rows_say_why(self):
        proc = run(CORE_SECRETS, producers_optional=True)
        self.assertRegex(proc.stdout, r"(?i)(sealed|sops)",
                         "the producer rows key off without naming the sealed plane, so a "
                         "reader cannot tell a keyed-off row from a passing one")

    def test_with_no_secrets_argument_the_route_rows_still_run(self):
        proc = run(None)
        self.assertEqual(proc.returncode, 0, proc.stdout[-3000:])
        self.assertIn("route", proc.stdout.lower())


class NoValueTest(unittest.TestCase):
    """stdout is a CI log."""

    def test_no_ciphertext_reaches_the_output(self):
        files = {n: sealed(n, [INTAKE_KEY] if n == "alertmanager-intake" else ["value"])
                 for n in MOUNTED}
        for mutation in ({}, {"alertmanager-intake": sealed("alertmanager-intake", ["NOPE"])}):
            f = dict(files)
            f.update(mutation)
            proc = run(seal_tree(f))
            self.assertNotIn("ExternalSecret", proc.stdout,
                             "the checker still reports on the retired plane")
            self.assertNotIn("ENC[", proc.stdout + proc.stderr,
                             "the checker printed a sealed value")
            self.assertNotIn("AES256_GCM", proc.stdout + proc.stderr)


class RetiredPlaneTest(unittest.TestCase):
    """Nothing in the checker still expects the retired producer."""

    def test_the_checker_does_not_name_the_retired_kind(self):
        text = CHECKER.read_text()
        hits = [f"{n}: {l.strip()[:100]}" for n, l in enumerate(text.splitlines(), 1)
                if "ExternalSecret" in l or "externalsecret" in l]
        self.assertEqual(hits, [], "the checker still expects the retired plane:\n"
                                   + "\n".join(hits))

    def test_the_stack_comment_does_not_promise_the_retired_producer(self):
        text = STACK.read_text()
        hits = [f"{n}: {l.strip()[:100]}" for n, l in enumerate(text.splitlines(), 1)
                if "ExternalSecret" in l]
        self.assertEqual(hits, [], "the stack's own comment still says the bearer comes "
                                   "from the retired plane:\n" + "\n".join(hits))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class Round24Test(unittest.TestCase):
    """Area-1 sweep round 24: the routes checker's own repairs."""

    def _mod(self, code):
        return subprocess.run([sys.executable, "-c", "import importlib.util, sys\n"
                               "spec = importlib.util.spec_from_file_location('amr', sys.argv[1]); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
                               + code, str(CHECKER)], capture_output=True, text=True, timeout=60)

    def test_a_receiver_name_of_a_credentials_shape_is_never_repeated(self):
        """A2: `shown` repeated any name that passed the receiver grammar, and a token
        passes it."""
        proc = self._mod("print(m.shown(['orbit-intake', 'ghp_PLANTED0123456789abcdefghijklmn']))\n"
                         "print(m.tokenish('INTAKE_ALERTMANAGER_TOKEN'), m.tokenish('orbit-intake'), m.tokenish('deadbeef0123456789abcdef'))\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.splitlines(), ['''['orbit-intake', "(+1 name(s) not of a name shape, or of a credential's shape)"]''', "False False True"])
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_an_application_with_value_files_is_not_audited_from_its_inline_values(self):
        """A3: the app mode read spec.source.helm.values alone; a valueFiles/parameters/
        valuesFrom entry makes the effective config something else."""
        base = STACK.read_text()
        text = re.sub(r"^(\s+)helm:\n", r"\1helm:\n\1  valueFiles: [override.yaml]\n", base, count=1, flags=re.M)
        self.assertNotEqual(text, base)
        proc = run_on(text)
        self.assertNotEqual(proc.returncode, 0); self.assertIn("carries helm.valueFiles", proc.stdout + proc.stderr)
        self.assertEqual(run_on(base).returncode, 0, run_on(base).stdout)

    def test_matchers_fold_case_like_go_and_keep_the_ascii_classes(self):
        """A4: re.ASCII made `(?i)` fold ASCII only, where Go folds Unicode; without it
        `\\d\\w\\s` grew Unicode-wide, where Go's are ASCII. Each class is spelled out now."""
        proc = self._mod("print(bool(m.compile_re('(?i)k', 'x').fullmatch('K')))\n"
                         "print(m.compile_re(r'\\d+', 'x').fullmatch('١') is None)\n"
                         "print(bool(m.compile_re(r'[\\w-]+', 'x').fullmatch('a_b-9')), m.compile_re(r'[\\w-]+', 'x').fullmatch('é') is None)\n"
                         "print(bool(m.compile_re(r'\\bfoo\\b', 'x').search('a foo b')), m.compile_re(r'\\S+', 'x').fullmatch(' ') is None)\n"
                         "try: m.compile_re(r'[\\D]', 'x'); print('accepted')\n"
                         "except Exception as e: print(type(e).__name__)\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.splitlines(), ["True", "True", "True True", "True True", "Unparseable"])


class Round25Test(Round24Test):
    """Area-1 sweep round 25: the routes checker's repairs (the round-24 loader reused)."""

    INTAKE_ROUTE = "                - matchers: ['alertname =~ \"^OrbitAgent.*\"']\n"

    def test_a_label_value_or_bearer_key_of_a_credentials_shape_is_described(self):
        """A1: label_shown repeated any value of a label's shape, and the bearer key row
        repeated the key name; a token passes both grammars."""
        proc = self._mod("print(m.label_shown('ghp_PLANTED0123456789abcdefghijklmn'), '|', m.label_shown('OrbitAgentOomSpike'))\n"
                         "print(m.key_shown('PLANTED0123456789abcdef'), '|', m.key_shown('INTAKE_ALERTMANAGER_TOKEN'), '|', m.key_shown('a b'))\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.splitlines(), ["(a value not of a label shape, or of a credential's shape) | OrbitAgentOomSpike",
                                                    "(a key not of a key shape, or of a credential's shape) | 'INTAKE_ALERTMANAGER_TOKEN' | (a key not of a key shape, or of a credential's shape)"])
        self.assertNotIn("PLANTED", proc.stdout)

    def test_a_trigger_deriving_a_label_the_tree_tests_is_not_certified(self):
        """A2: variants simulated a fixed label set; a label_replace in the expression puts
        a namespace the catalog never fixed on the alert, and a namespace route ahead of
        the intake route catches it."""
        base = STACK.read_text()
        self.assertEqual(base.count(self.INTAKE_ROUTE), 1)
        shadow = base.replace(self.INTAKE_ROUTE, "                - matchers: ['namespace = \"tenant-prod\"']\n                  receiver: telegram\n" + self.INTAKE_ROUTE)
        def cat(expr, labels=None):
            return {"agents": [{"name": "oom", "triggers": [{"type": "metric", "name": "spike", "expr": expr, **({"labels": labels} if labels else {})}]}]}
        derived = 'label_replace(vector(1), "namespace", "tenant-prod", "", "")'
        row = "the alert keeps its series' value there"
        for expr in (derived, 'up{namespace="tenant-prod"} == 0', "vector(1)"):     # round 26: every expression, not only a derivation
            with self.subTest(expr=expr[:20]):
                proc = run_on(shadow, catalog=cat(expr))
                self.assertNotEqual(proc.returncode, 0)
                self.assertTrue(any("trigger oom/spike: a route on its alert's way" in l and "['namespace']" in l and row in l for l in fails(proc)), proc.stdout)
        # the rule's own labels override the series': a fixed namespace is simulated as fixed
        self.assertFalse(any(row in l for l in fails(run_on(shadow, catalog=cat(derived, {"namespace": "monitoring"})))))
        # a tree that tests only labels the rule fixes (the shipped one): nothing to say
        self.assertFalse(any(row in l for l in fails(run_on(base, catalog=cat(derived)))))
        self.assertFalse(any(row in l for l in fails(run_on(base, catalog=cat('up{namespace="tenant-prod"} == 0')))))
        # only the routes on the alert's way count: a sibling after the first terminal match is
        # never evaluated; the inhibit rules count on their target side only
        proc = self._mod("cfg = {'route': {'receiver': 'r', 'routes': [{'match': {'team': 'sre'}, 'match_re': {'PLANTED bad key': 'x'}, 'receiver': 'a'},\n"
                         "                                              {'matchers': ['alertname = \"x\"'], 'receiver': 'b', 'routes': [{'matchers': ['pod =~ \".+\"'], 'receiver': 'c'}]},\n"
                         "                                              {'matchers': ['region = \"eu\"'], 'receiver': 'd'}]},\n"
                         "       'inhibit_rules': [{'source_matchers': ['cluster = \"x\"'], 'target_match': {'severity': 'warning'}, 'equal': ['namespace']}]}\n"
                         "print(sorted(m.reachable_keys(cfg, {'alertname': 'x', 'team': 'ops'})))\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "[\"(a key not of a label name's shape)\", 'alertname', 'pod', 'severity', 'team']")

    def test_dotted_and_dotless_i_fold_to_nothing_like_go(self):
        """B5: Python's (?i) folds İ and ı onto i/I; Go's simple folding has no orbit for
        either, so a regex matcher disagreed with Alertmanager on those two characters."""
        proc = self._mod("r = m.compile_re('(?i)i', 'x')\n"
                         "print(r.fullmatch('\\u0131') is None, r.fullmatch('\\u0130') is None, bool(r.fullmatch('I')), bool(r.fullmatch('i')))\n"
                         "print(bool(m.compile_re('(?i)\\u0131', 'x').fullmatch('\\u0131')), m.compile_re('(?i)\\u0131', 'x').fullmatch('i') is None)\n"
                         "print(bool(m.compile_re('(?i)k', 'x').fullmatch('\\u212a')), bool(m.compile_re('(?i)s', 'x').fullmatch('\\u017f')), bool(m.compile_re('.+', 'x').fullmatch('\\u0131')))\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.splitlines(), ["True True True True", "True True", "True True True"])
