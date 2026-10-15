#!/usr/bin/env python3
"""
Static guards for scripts/verify/stage5-platform.sh — run as
`python3 scripts/test_verify_platform.py`. No cluster, no credentials, no network.

The stage is a VENDORED artifact (core-manifest.txt): every org runs the copy it pinned,
so a regression here ships to every orbit with a green repo, exactly as
scripts/test_instance_init.py says of the scaffolder. Nothing else in core CI reads this
file, and the failures it guards against are all invisible until a real org hits them:

  THE INSTANCE-STRING GUARD is the one that matters. The whole point of the stage is that
  it runs unchanged for any organisation, so a single hostname, tenant, namespace or
  address belonging to the org it was extracted from turns it into a check that fails for
  everybody else. stage3's own extraction check greps the values in instance/config.yaml
  over CORE_SCOPE and this file is in that scope — but stage3 needs an instance to run
  against, and core CI has none, so the same property is asserted here against the
  literal strings instead.

  THE TEMPLATE-ARTIFACT GUARD is the second. Every `clusters/platform/...` path the stage
  names must be something `template/` actually ships, at `template/clusters/...` or
  staged at `template/provisioning/clusters/...`. A row that asserts a file no org is
  scaffolded with is a row that is red on every fresh install, and there is no other way
  to notice that from inside core.

  THE MUTATION GUARD is the fourth, and the only one that runs the stage rather than
  reading it. Everything above is a property of the FILE; what an oracle is actually for
  is going red when the thing it watches breaks, and review round 1 found five separate
  rows that stayed green through the very defect they were written for — a Probe
  dispatched at a Service that does not exist, a bucket-gated Application removed with
  the bucket still declared, the MCP backends' NetworkPolicy deleted, `AllowRegister`
  flipped to true, and an unquoted duration. Each MutationTest below builds a real
  fresh-scaffold instance (the template, the filled example config, the vendored core
  paths, `instance-init.py`), runs the stage PRE-BOOT once to establish that it is
  green, then breaks exactly one thing and asserts a NAMED FAIL line appears. Asserting
  the named line and not merely a non-zero exit is the point: a mutation that goes red
  for an unrelated reason proves nothing.

  THE ORDER GUARD is the fifth, and the least obvious. `make verify` runs
  `scripts/verify/stage*.sh` in GLOB order and stops at the first failure, and glob order
  is locale collation, not byte order: under en_US the punctuation is ignored at the first
  level, so `stage4b-platform.sh` sorts BEFORE `stage4.sh` while `stage4z-platform.sh`
  sorts after it. The name here has to sort after stage4 and before an instance's own
  stage5 under BOTH collations, or the cumulative contract ("every later stage runs the
  earlier ones first") quietly stops holding on somebody's machine.
"""
import json
import locale
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
STAGE = ROOT / "scripts" / "verify" / "stage5-platform.sh"
REL = "scripts/verify/stage5-platform.sh"

# Platform files that are not in template/ because a CORE SCRIPT writes them rather than a
# staged source: they are loops over a config list, not token fills, so there is nothing to
# stage. Named one by one, with the producer, so this stays a fail-closed guard rather than
# a hole every unshipped path falls through.
GENERATED = {
    # scripts/instance-init.py renders one store and one sentinel per
    # secrets.environments entry on the vault-backed providers, and carries a
    # hand-written `conditions:` allowlist forward (ONBOARDING §5).
    # core-manifest.txt vendors that script, so every org has it.
    "clusters/platform/config/external-secrets/stores.yaml",
}

# The instance the stage was extracted from. Its values are what a careless port would have
# left behind. The test tenant's name is named here; the instance's own names are personal
# and are not written anywhere in this repository: scripts/personal_strings.py holds them
# only as salted hashes, and the test below asks it too.
INSTANCE_STRINGS = ["checkers"]


class StageFileTest(unittest.TestCase):
    def setUp(self):
        self.assertTrue(STAGE.is_file(), f"{REL} is missing")
        self.text = STAGE.read_text()

    def test_every_helper_the_stages_invoke_is_vendored(self):
        """Sweep area 1 (2026-09-13, breaker): the stage called a helper the manifest did
        not vendor, so a fresh instance's live row failed on a missing file. Every
        scripts/verify/*.py a stage names is a manifest entry."""
        manifest = {l.split(" -> ")[0].strip() for l in (ROOT / "core-manifest.txt").read_text().splitlines()
                    if l.strip() and not l.startswith("#")}
        for stage in sorted((ROOT / "scripts" / "verify").glob("stage*.sh")):
            for helper in set(re.findall(r"scripts/verify/[a-z0-9-]+\.py", stage.read_text())):
                self.assertIn(helper, manifest, f"{stage.name} invokes {helper}, which core-manifest.txt does not vendor")

    def test_a_redacted_url_a_global_credential_and_an_odd_receiver_name(self):
        """Round 3: (auditor) the running config shows a secret-typed URL as <secret>,
        which the checker read as clean — redaction is not cleanliness; (breaker) the
        global block's credentials are inherited by every receiver and were never read;
        (breaker) a receiver's name is config text and is shown only in its own shape."""
        checker = ROOT / "scripts" / "verify" / "am-inline-credentials.py"
        def run(cfg):
            with tempfile.TemporaryDirectory() as td:
                f = pathlib.Path(td) / "am.yaml"; f.write_text(yaml.safe_dump(cfg))
                return subprocess.run([sys.executable, str(checker), str(f)], capture_output=True, text=True, timeout=60)
        redacted = {"receivers": [{"name": "slack", "slack_configs": [{"api_url": "<secret>", "channel": "#x"}]}]}
        proc = run(redacted)
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("api_url", proc.stdout)
        glob = {"global": {"smtp_auth_password": "PLANTED-SMTP-PASSWORD"}, "receivers": [{"name": "mail", "email_configs": [{"to": "a@b"}]}]}
        proc = run(glob)
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("global block", proc.stdout); self.assertIn("smtp_auth_password", proc.stdout)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)
        odd = {"receivers": [{"name": "PLANTED token=abc", "webhook_configs": [{"url": "http://x/", "http_config": {"authorization": {"credentials": "PLANTED-CRED"}}}]}]}
        proc = run(odd)
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)
        self.assertIn("receiver '#0'", proc.stdout)
        # round 23 (auditor A3): a name that passed the old grammar but is a token (40 characters
        # of ghp_...) is shown as "#i" — a name is a letter first and at most 24 characters
        token = {"receivers": [{"name": "ghp_PLANTEDTOKEN0123456789abcdefghijklmn", "telegram_configs": [{"bot_token": "PLANTED-BOT", "chat_id": 1}]}]}
        proc = run(token)
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr); self.assertIn("receiver '#0'", proc.stdout)
        # round 25 (breaker B2): a name within the grammar's length but of a credential's shape
        # (a run of 16+ letters and digits) is shown by position too
        token = {"receivers": [{"name": "PLANTED0123456789abcdef", "webhook_configs": [{"url": "http://x/", "http_config": {"authorization": {"credentials": "PLANTED-CRED"}}}]}]}
        proc = run(token)
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr); self.assertIn("receiver '#0' webhook_configs[0] carries an inlined", proc.stdout)
        clean = {"receivers": [{"name": "hook", "webhook_configs": [{"url": "http://orbit-agents-intake.orbit-agents-prod.svc/webhook/alertmanager",
                                                                     "http_config": {"authorization": {"credentials_file": "/etc/x"}}}]}]}
        self.assertEqual(run(clean).returncode, 0, run(clean).stdout)

    def test_a_redacted_url_is_clean_only_when_git_is_clean_at_the_same_place(self):
        """Round 4 (auditor): the live inline check flagged every <secret> URL (the intake
        webhook, secret-typed since Alertmanager v0.33) — a regression from round 3.
        With --desired, a redacted URL is clean iff git's URL at the same receiver, block,
        index and field is clean; without a counterpart it stays red."""
        helper = ROOT / "scripts" / "verify" / "am-inline-credentials.py"
        def app(url):
            return yaml.safe_dump({"spec": {"source": {"helm": {"valuesObject": {"alertmanager": {"config": {
                "receivers": [{"name": "orbit-intake", "webhook_configs": [{"url": url}]}]}}}}}}})
        live = yaml.safe_dump({"receivers": [{"name": "orbit-intake", "webhook_configs": [{"url": "<secret>"}]}]})
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            (root / "live.yaml").write_text(live)
            def run(desired):
                args = [sys.executable, str(helper), str(root / "live.yaml")]
                if desired is not None:
                    (root / "app.yaml").write_text(desired); args += ["--desired", str(root / "app.yaml")]
                return subprocess.run(args, capture_output=True, text=True, timeout=60)
            proc = run(app("http://orbit-agents-intake.orbit-agents-prod.svc/webhook/alertmanager"))
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("is redacted in the running config and clean in git at the same place", proc.stdout)
            for desired in (None, app("https://hooks.example/?token=PLANTED_LONG_CREDENTIAL_1234567890"), app("<secret>"),
                            yaml.safe_dump({"spec": {"source": {"helm": {"valuesObject": {"alertmanager": {"config": {"receivers": [{"name": "other", "webhook_configs": [{"url": "http://a.b.svc/x"}]}]}}}}}}})):
                with self.subTest(desired=(desired or "none")[:60]):
                    proc = run(desired)
                    self.assertNotEqual(proc.returncode, 0)
                    self.assertNotIn("PLANTED", proc.stdout + proc.stderr)
            (root / "app.yaml").write_text("a: [PLANTED_CREDENTIAL\n")
            proc = subprocess.run([sys.executable, str(helper), str(root / "live.yaml"), "--desired", str(root / "app.yaml")], capture_output=True, text=True, timeout=60)
            self.assertNotEqual(proc.returncode, 0); self.assertIn("(ParserError; a repeated key counts)", proc.stdout + proc.stderr); self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_the_stage_hands_the_git_config_to_the_live_inline_check(self):
        self.assertIn('am-inline-credentials.py "$TMP/amconfig.yaml" --desired clusters/platform/apps/kube-prometheus-stack.yaml', self.text)

    def test_http_header_secret_lists_are_inline_credentials(self):
        """B2 (round 7): http_config.http_headers.X-Api-Key.secrets was walked past."""
        helper = ROOT / "scripts" / "verify" / "am-inline-credentials.py"
        def run(headers):
            with tempfile.TemporaryDirectory() as td:
                f = pathlib.Path(td) / "am.yaml"
                f.write_text(yaml.safe_dump({"receivers": [{"name": "r", "webhook_configs": [{"url": "http://a.b.svc/x", "http_config": {"http_headers": headers}}]}]}))
                return subprocess.run([sys.executable, str(helper), str(f)], capture_output=True, text=True, timeout=60)
        proc = run({"X-Api-Key": {"secrets": ["PLANTED_HEADER_CREDENTIAL_1234567890"]}})
        self.assertNotEqual(proc.returncode, 0); self.assertIn("field 'http_headers.X-Api-Key.secrets'", proc.stdout); self.assertNotIn("PLANTED", proc.stdout + proc.stderr)
        proc = run({"Authorization": {"values": ["Bearer PLANTED_HEADER_CREDENTIAL_1234567890"]}})
        self.assertNotEqual(proc.returncode, 0); self.assertIn("field 'http_headers.Authorization.values'", proc.stdout); self.assertNotIn("PLANTED", proc.stdout + proc.stderr)
        proc = run({"Bearer PLANTED_NAME_1234567890": {"secrets": ["x"]}})
        self.assertNotEqual(proc.returncode, 0); self.assertIn("field 'http_headers.#0.secrets'", proc.stdout); self.assertNotIn("PLANTED", proc.stdout + proc.stderr)
        proc = run({"X-Api-Key": {"files": ["/etc/alertmanager/secrets/k/key"]}, "X-Trace": {"values": ["1"]}})
        self.assertEqual(proc.returncode, 0, proc.stdout)
        # round 24 (breaker B1): a header name of a header's grammar but a credential's shape is not repeated
        proc = run({"Authorization-PLANTED0123456789abcdefghijklmnop": {"secrets": ["x"]}})
        self.assertNotEqual(proc.returncode, 0); self.assertIn("field 'http_headers.(a component of a credential's shape).secrets'", proc.stdout)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_a_field_name_is_shown_only_when_it_is_a_config_key(self):
        """B4 (round 7): a pasted line used as a YAML key was printed as the field name."""
        helper = ROOT / "scripts" / "verify" / "am-inline-credentials.py"
        with tempfile.TemporaryDirectory() as td:
            f = pathlib.Path(td) / "am.yaml"
            f.write_text(yaml.safe_dump({"receivers": [{"name": "r", "webhook_configs": [{"url": "http://a.b.svc/x", "Bearer PLANTED_FIELD_CREDENTIAL_123456789_token": "<secret>"}]}]}))
            proc = subprocess.run([sys.executable, str(helper), str(f)], capture_output=True, text=True, timeout=60)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("field '#0 (a key not of a config-key shape)'", proc.stdout); self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_a_credential_named_query_parameter_is_a_credential_whatever_its_length(self):
        """B6 (round 8): `?password=shortpass` passed the 24-character token rule."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("am_inline", ROOT / "scripts" / "verify" / "am-inline-credentials.py")
        mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
        for url in ("https://hooks.example/x?password=shortpass", "https://hooks.example/x?a=1&token=ab", "https://hooks.example/x?key=k1",
                    "https://hooks.example/x?api_key=", "https://hooks.example/x#access_token=z", "https://hooks.example/x?Secret=s"):
            self.assertTrue(mod.url_embeds_credential(url), url)
        for url in ("https://hooks.example/x?channel=ops&format=json", "http://orbit-agents-intake.orbit-agents-prod.svc/webhook/alertmanager"):
            self.assertFalse(mod.url_embeds_credential(url), url)

    def test_a_receiver_block_key_is_shown_only_when_it_is_a_config_key(self):
        """B7 (round 8): a scalar-valued `<pasted text>_configs` key was repeated in
        the refusal."""
        checker = ROOT / "scripts" / "verify" / "am-inline-credentials.py"
        with tempfile.TemporaryDirectory() as td:
            f = pathlib.Path(td) / "am.yaml"
            f.write_text(yaml.safe_dump({"receivers": [{"name": "human", "webhook_configs": [{"url": "http://x/"}], "PLANTED CREDENTIAL_configs": "scalar"}]}))
            proc = subprocess.run([sys.executable, str(checker), str(f)], capture_output=True, text=True, timeout=60)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("(a key not of a config-key shape)' is not a list", proc.stdout)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_a_list_valued_receiver_block_name_is_shown_only_by_shape(self):
        """B3 (round 11): a list-valued `<pasted text>_configs` block reached the
        inlined-credential rows verbatim (the scalar case was shaped in round 8)."""
        checker = ROOT / "scripts" / "verify" / "am-inline-credentials.py"
        with tempfile.TemporaryDirectory() as td:
            f = pathlib.Path(td) / "am.yaml"
            f.write_text(yaml.safe_dump({"receivers": [{"name": "r", "Bearer PLANTED_BLOCK_CREDENTIAL_configs": [{"password": "dummy"}]}]}))
            proc = subprocess.run([sys.executable, str(checker), str(f)], capture_output=True, text=True, timeout=60)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("receiver 'r' #0 (a key not of a config-key shape)[0] carries an inlined credential in field 'password'", proc.stdout)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_the_redacted_url_rows_show_the_field_by_shape(self):
        """B5 (round 12): the two redacted-URL rows interpolated the raw field name."""
        helper = ROOT / "scripts" / "verify" / "am-inline-credentials.py"
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            (root / "live.yaml").write_text(yaml.safe_dump({"receivers": [{"name": "hook", "webhook_configs": [{"PLANTED_CREDENTIAL with spaces_url": "<secret>"}]}]}))
            (root / "app.yaml").write_text(yaml.safe_dump({"spec": {"source": {"helm": {"valuesObject": {"alertmanager": {"config": {"receivers": [{"name": "hook", "webhook_configs": [{"url": "http://a.b.svc/hook"}]}]}}}}}}}))
            proc = subprocess.run([sys.executable, str(helper), str(root / "live.yaml"), "--desired", str(root / "app.yaml")], capture_output=True, text=True, timeout=60)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("field '#0 (a key not of a config-key shape)' is redacted in the running config and git carries no clean URL", proc.stdout)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_a_receiver_keeps_its_identity_for_the_git_lookup(self):
        """B6 (round 12): the display label (`#0` for an unshaped name) replaced the
        name used to find git's receiver, so `team alerts` never matched itself."""
        helper = ROOT / "scripts" / "verify" / "am-inline-credentials.py"
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            (root / "live.yaml").write_text(yaml.safe_dump({"receivers": [{"name": "team alerts", "webhook_configs": [{"url": "<secret>"}]}]}))
            (root / "app.yaml").write_text(yaml.safe_dump({"spec": {"source": {"helm": {"valuesObject": {"alertmanager": {"config": {"receivers": [{"name": "team alerts", "webhook_configs": [{"url": "http://a.b.svc/hook"}]}]}}}}}}}))
            proc = subprocess.run([sys.executable, str(helper), str(root / "live.yaml"), "--desired", str(root / "app.yaml")], capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("receiver '#0' webhook_configs[0] field 'url' is redacted in the running config and clean in git", proc.stdout)

    def test_a_repeated_key_in_the_git_config_is_refused(self):
        """B7 (round 12): a second `url` key hid a credential-bearing first one from the
        reader (YAML keeps the last), and git was certified clean at that place."""
        helper = ROOT / "scripts" / "verify" / "am-inline-credentials.py"
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            (root / "live.yaml").write_text(yaml.safe_dump({"receivers": [{"name": "hook", "webhook_configs": [{"url": "<secret>"}]}]}))
            for label, app in (("valuesObject", "spec:\n  source:\n    helm:\n      valuesObject:\n        alertmanager:\n          config:\n            receivers:\n            - name: hook\n              webhook_configs:\n              - url: https://user:PLANTED_PASSWORD@hooks.example/hook\n                url: http://a.b.svc/hook\n"),
                               ("values", "spec:\n  source:\n    helm:\n      values: |\n        alertmanager:\n          config:\n            receivers:\n            - name: hook\n              webhook_configs:\n              - url: https://user:PLANTED_PASSWORD@hooks.example/hook\n                url: http://a.b.svc/hook\n")):
                with self.subTest(label=label):
                    (root / "app.yaml").write_text(app)
                    proc = subprocess.run([sys.executable, str(helper), str(root / "live.yaml"), "--desired", str(root / "app.yaml")], capture_output=True, text=True, timeout=60)
                    self.assertNotEqual(proc.returncode, 0, proc.stdout)
                    self.assertIn("a repeated key counts", proc.stdout + proc.stderr); self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_an_inline_tls_private_key_is_a_credential(self):
        """B6 (round 13): tls_config.key (or its <secret> marker) passed as clean; only
        key_file is the sanctioned spelling."""
        checker = ROOT / "scripts" / "verify" / "am-inline-credentials.py"
        def run(tls):
            with tempfile.TemporaryDirectory() as td:
                f = pathlib.Path(td) / "am.yaml"
                f.write_text(yaml.safe_dump({"receivers": [{"name": "hook", "webhook_configs": [{"url": "http://a.b.svc/x", "http_config": {"tls_config": tls}}]}]}))
                return subprocess.run([sys.executable, str(checker), str(f)], capture_output=True, text=True, timeout=60)
        for tls in ({"key": "-----BEGIN PRIVATE KEY-----\nPLANTED\n-----END PRIVATE KEY-----"}, {"key": "<secret>"}):
            with self.subTest(tls=str(tls)[:20]):
                proc = run(tls); self.assertNotEqual(proc.returncode, 0, proc.stdout)
                self.assertIn("carries an inlined credential in field 'tls_config.key'", proc.stdout); self.assertNotIn("PLANTED", proc.stdout + proc.stderr)
        proc = run({"key_file": "/etc/alertmanager/secrets/x/key", "cert_file": "/etc/x/cert"}); self.assertEqual(proc.returncode, 0, proc.stdout)

    def test_a_credential_in_a_query_with_no_path_is_seen(self):
        """Sweep area 1 (2026-09-13, breaker): the inline-credential checker split the URL
        at the first slash, so a query that followed the authority directly was never
        read. The URL is parsed as a URL now."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("am_inline", ROOT / "scripts" / "verify" / "am-inline-credentials.py")
        mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
        for url in ("https://hooks.example?token=PLANTED_LONG_CREDENTIAL_1234567890",
                    "https://hooks.example/?token=PLANTED_LONG_CREDENTIAL_1234567890",
                    "https://hooks.example#PLANTED_LONG_CREDENTIAL_1234567890",
                    "https://user:PLANTED@hooks.example/x",
                    "https://api.telegram.org/bot123:PLANTEDTOKEN/sendMessage",
                    # percent-encoded (round 2, breaker): the same credential, escaped every other byte
                    "https://hooks.example/?token=P%4CA%4ET%45D%5FL%4FN%47_%43R%45D%45N%54I%41L%5F1%32%33%34%35%36%37%38%39%30",
                    "https://hooks.example/P%4CA%4ET%45D%5FL%4FN%47_%43R%45D%45N%54I%41L%5F1%32%33%34%35%36%37%38%39%30/x"):
            self.assertTrue(mod.url_embeds_credential(url), url)
        for url in ("http://orbit-agents-intake.orbit-agents-prod.svc/webhook/alertmanager",
                    "https://hooks.example/short?x=1"):
            self.assertFalse(mod.url_embeds_credential(url), url)

    def test_it_parses_and_is_executable(self):
        proc = subprocess.run(["bash", "-n", str(STAGE)], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(STAGE.stat().st_mode & 0o111,
                        f"{REL} is not executable — `make verify` runs it with bash, but "
                        "core-sync vendors the mode and an instance may invoke it directly")

    def test_shellcheck_finds_no_errors(self):
        """Advisory: the repo does not install shellcheck, so this runs only where it is
        present (a developer's box, or a CI job that adds it). Severity `error` only —
        style warnings on a 2,700-line script with embedded here-docs are noise, and a
        guard that cries wolf gets deleted."""
        try:
            proc = subprocess.run(["shellcheck", "-S", "error", str(STAGE)],
                                  capture_output=True, text=True)
        except FileNotFoundError:
            self.skipTest("shellcheck is not installed here")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_it_carries_no_instance_string(self):
        """The rg from the vendoring brief, as a test rather than as a habit."""
        hits = []
        for needle in INSTANCE_STRINGS:
            rx = re.compile(re.escape(needle), re.I)
            for n, line in enumerate(self.text.splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{REL}:{n}: {needle!r} in {line.strip()[:100]}")
        sys.path.insert(0, str(ROOT / "scripts"))
        import personal_strings
        hits += [f"{REL}:{n}: {why}" for n, why in personal_strings.scan_text(self.text)]
        self.assertEqual(hits, [], "the vendored stage carries instance strings:\n"
                                   + "\n".join(hits))

    def test_every_platform_path_it_names_is_shipped_by_the_template(self):
        """A row asserting a file no org is scaffolded with is red on every fresh install."""
        named = sorted(set(re.findall(r"clusters/platform/[A-Za-z0-9_./-]+\.ya?ml", self.text)))
        self.assertGreater(len(named), 5, "the stage names almost no platform artifact — "
                                          "this guard has nothing to assert")
        missing = [p for p in named
                   if p not in GENERATED
                   and not (ROOT / "template" / p).exists()
                   and not (ROOT / "template" / "provisioning" / p).exists()]
        self.assertEqual(missing, [],
                         "the stage names platform artifacts template/ does not ship "
                         "(neither directly nor staged under provisioning/): "
                         + ", ".join(missing))

    def test_it_is_vendored_and_in_the_extraction_scope(self):
        manifest = (ROOT / "core-manifest.txt").read_text().splitlines()
        self.assertIn(REL, [l.strip() for l in manifest],
                      "core-manifest.txt does not vendor the stage — no instance would "
                      "ever receive it")
        # The extraction check moved out of stage3.sh on 2026-09-07 and now DERIVES its
        # scope from core-manifest.txt, so being vendored is what puts the stage in it —
        # which is exactly the property worth asserting, since a hand-kept second list is
        # the thing that had already drifted. Asked of the checker itself rather than of
        # the manifest a second time, because the checker is what actually runs.
        scope = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "verify" / "no-instance-strings.py"),
             "--print-scope"], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(scope.returncode, 0, scope.stdout + scope.stderr)
        self.assertIn(REL, scope.stdout.split(),
                      "the stage is not in the extraction check's scope — an instance "
                      "string that leaked into it would never be caught on a real instance")

    def test_it_sorts_after_stage4_and_before_an_instance_stage5(self):
        """Glob order is COLLATION order, and the two collations disagree about punctuation.
        Assert the name works under both, because `make verify` runs whatever the operator's
        locale produces."""
        names = ["stage2.sh", "stage3.sh", "stage4.sh", STAGE.name, "stage5.sh", "stage6.sh"]
        for loc in ("C", "en_US.UTF-8"):
            with self.subTest(locale=loc):
                try:
                    locale.setlocale(locale.LC_COLLATE, loc)
                except locale.Error:
                    self.skipTest(f"locale {loc} is not available here")
                ordered = sorted(names, key=locale.strxfrm)
                locale.setlocale(locale.LC_COLLATE, "C")
                self.assertLess(ordered.index("stage4.sh"), ordered.index(STAGE.name),
                                f"{STAGE.name} sorts BEFORE stage4.sh under {loc} — it would "
                                "run before the stage it assumes has passed")
                self.assertLess(ordered.index(STAGE.name), ordered.index("stage5.sh"),
                                f"{STAGE.name} sorts after an instance's own stage5.sh under "
                                f"{loc} — the instance's tenant stages would run first")
        self.assertNotIn(STAGE.name, ("stage5.sh", "stage6.sh", "stage7.sh", "stage8.sh"),
                         "the vendored name collides with a stage an instance already owns")

    def test_every_live_row_can_key_off_before_bootstrap(self):
        """The stage3 pre-boot convention, asserted structurally: the stage must key off the
        kubeconfig FILE's existence, never a reachability probe. A stage that decided
        pre-boot by dialling the API server would report a bootstrapped-but-down cluster as
        a fresh scaffold, which is the one thing this convention exists to prevent."""
        self.assertRegex(self.text, r'PREBOOT=0\s*\n(#[^\n]*\n)*kubeconfig_present\(\)\{[^}]*\}\s*\nkubeconfig_present "\$KUBECONFIG" \|\| PREBOOT=1',
                         "the stage no longer derives PREBOOT from the kubeconfig file's "
                         "existence")
        # Since round 2 the announcement is CENTRALISED: every live section keys off through
        # LIVEOFF and words it through keyoff_live, which is what lets one dead cluster be
        # one FAIL instead of dozens. So the structural assertion is that the sections route
        # through the helper, and that the helper still tells the two reasons apart — a row
        # that vanishes silently, or that calls an unreachable cluster "pre-boot", is
        # indistinguishable from one that passed.
        self.assertGreaterEqual(self.text.count("keyoff_live "), 15,
                                "almost no live section keys off through keyoff_live — a live "
                                "row that vanishes silently is indistinguishable from one that "
                                "passed, and an unannounced one cannot say WHY it kept quiet")
        self.assertIn('LIVEOFF=1', self.text)
        helper = self.text.split("keyoff_live(){", 1)[-1].split("\n}", 1)[0]
        self.assertIn("PRE-BOOT:", helper)
        self.assertIn("UNREACHABLE:", helper)
        self.assertNotIn('ok "PRE-BOOT:', self.text.replace(helper, ""),
                         "a section still words its own pre-boot key-off, so it cannot report "
                         "the unreachable case")

    def test_the_probe_want_list_reads_the_tenant_probes_too(self):
        """The public-surface rows compare every live probe_success series against what
        git declares. The daemon applies a Probe per tenant pair from clusters/apps/, so
        those targets are declared too: the want-list python, run as it is over a tree with
        the platform file and one tenant Probe file, lists both targets."""
        text = STAGE.read_text()
        code = text.split("  python3 -c \"\nimport glob,sys,yaml", 1)[1].split("\" > \"$TMP/want.txt\"", 1)[0]
        code = ("import glob,sys,yaml" + code).replace("\\\\n", "\\n")
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            (root / "clusters/platform/monitoring").mkdir(parents=True); (root / "clusters/apps").mkdir()
            (root / "clusters/platform/monitoring/probes.yaml").write_text(
                "kind: Probe\nspec: {targets: {staticConfig: {static: [https://grafana.example]}}}\n")
            (root / "clusters/apps/tenant-probe-abc.yaml").write_text(
                "kind: Probe\nmetadata: {labels: {orbit.local/project: w}}\n"
                "spec: {targets: {staticConfig: {static: [https://w.example]}}}\n---\nkind: Ingress\n")
            (root / "clusters/apps/not-yaml.yaml").write_text(": : :\n")
            code = code.replace("$PROBEF", "clusters/platform/monitoring/probes.yaml")
            proc = subprocess.run([sys.executable, "-c", code], cwd=td, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.split(), ["https://grafana.example", "https://w.example"])

    def test_the_probe_want_list_reads_the_probe_monitors_too(self):
        """QA of v1.0.0, F13: orbitd renders a Probe for every probe monitor under
        monitors/, so one uptime monitor a user made was a live target git did not
        declare and the row was red for as long as the monitor existed. A probe monitor's
        target is declared; a metric monitor, and a file that is not YAML, declare none."""
        text = STAGE.read_text()
        code = text.split("  python3 -c \"\nimport glob,sys,yaml", 1)[1].split("\" > \"$TMP/want.txt\"", 1)[0]
        code = ("import glob,sys,yaml" + code).replace("\\\\n", "\\n")
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            (root / "clusters/platform/monitoring").mkdir(parents=True); (root / "monitors").mkdir()
            (root / "clusters/platform/monitoring/probes.yaml").write_text(
                "kind: Probe\nspec: {targets: {staticConfig: {static: [https://grafana.example]}}}\n")
            (root / "monitors/door.yaml").write_text(
                "name: door\nproject: w\nenv: prod\nkind: probe\ntarget: https://w.example/readyz\n")
            (root / "monitors/errors.yaml").write_text("name: errors\nkind: metric\nquery: up\ntarget: https://no.example\n")
            (root / "monitors/not-yaml.yaml").write_text(": : :\n")
            code = code.replace("$PROBEF", "clusters/platform/monitoring/probes.yaml")
            proc = subprocess.run([sys.executable, "-c", code], cwd=td, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.split(), ["https://grafana.example", "https://w.example/readyz"])

    def test_the_live_probe_list_counts_a_door_once(self):
        """A door probed by the platform's Probe and by a tenant's is one target; the
        have-list python, fed two series for one instance, prints it once."""
        text = STAGE.read_text()
        code = text.split("    promq 'probe_success' | python3 -c \"\n", 1)[1].split("\" > \"$TMP/have.txt\"", 1)[0].replace("\\\\n", "\\n")
        series = {"data": {"result": [{"metric": {"instance": "https://a.example", "job": "platform-probes"}},
                                      {"metric": {"instance": "https://a.example", "job": "tenant-probe-x"}},
                                      {"metric": {"instance": "https://b.example", "job": "platform-probes"}}]}}
        proc = subprocess.run([sys.executable, "-c", code], input=json.dumps(series), capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.split(), ["https://a.example", "https://b.example"])

    def test_the_probe_drift_row_names_hosts_and_never_a_target_url(self):
        """A declared or live target may carry a token in its path or query; the drift
        finding names hosts and counts only."""
        text = STAGE.read_text()
        code = text.split("    probe_drift(){ python3 - \"$1\" \"$2\" <<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
        with tempfile.TemporaryDirectory() as td:
            want = pathlib.Path(td) / "want.txt"; have = pathlib.Path(td) / "have.txt"
            want.write_text("https://a.example/path?token=PLANTED-WANT\nhttps://b.example\n")
            have.write_text("https://b.example\nhttps://user:PLANTED-HAVE@c.example:8443/x\n"
                            "https://user:PLANTED-UNPARSEABLE@host：80/\n")
            proc = subprocess.run([sys.executable, "-c", code, str(want), str(have)], capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "2 target(s) declared, 3 live; declared but unprobed host(s): a.example; "
                                              "probed but undeclared host(s): ?(unparseable), c.example")
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_a_service_port_that_is_not_a_number_is_named_by_object(self):
        """A rendered or committed Service whose port is not a number is a malformed
        Service: the row names it, and the value stays out of every stream."""
        text = STAGE.read_text()
        code = text.split("SVCPORT=$(python3 - . <<'PY'\n", 1)[1].split("\nPY\n)", 1)[0]
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            (root / "clusters/platform/apps").mkdir(parents=True); (root / "catalog").mkdir()
            (root / "clusters/svc.yaml").write_text(
                "kind: Service\nmetadata: {name: odd, namespace: monitoring}\nspec:\n  ports:\n    - port: PLANTED-RENDERED-VALUE\n")
            # round 19: the renderer's entrypoint is clusters/platform/apps/
            (root / "clusters/platform/apps/all.yaml").write_text(
                "kind: Application\nmetadata: {name: all, namespace: argocd}\nspec: {source: {path: clusters, directory: {recurse: true}}, destination: {namespace: monitoring}}\n")
            proc = subprocess.run([sys.executable, "-c", code, "."], cwd=td, capture_output=True, text=True, timeout=120)
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("FAIL Service monitoring/odd declares a port that is not an integer in 1..65535", proc.stdout)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_the_coverage_comparison_survives_an_unparseable_target(self):
        """The Ingress-coverage python, fed a probed target the parser refuses: no message
        on stderr, the parseable hosts still compared, an unprobed host still named."""
        text = STAGE.read_text()
        code = text.split("    UNCOV=$(python3 - \"$TMP/want.txt\" \"$TMP/inghosts.txt\" \"$ORBIT_DOMAIN\" <<'PY' 2>/dev/null\n", 1)[1].split("\nPY\n)", 1)[0]
        with tempfile.TemporaryDirectory() as td:
            want = pathlib.Path(td) / "want.txt"; ing = pathlib.Path(td) / "ing.txt"
            want.write_text("https://user:PLANTED-URL-PASSWORD@host：80/\nhttps://a.example/\n")
            ing.write_text("a.example\nb.example\nintake.example.org\n")
            proc = subprocess.run([sys.executable, "-c", code, str(want), str(ing), "example.org"], capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "b.example")
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)
        self.assertEqual(proc.stderr, "")

    def test_the_static_intake_guard_counts_path_rules_and_repeats_none(self):
        """The intake door's static guard over an Ingress publishing a token-bearing path
        beside the webhook: the finding counts rules and names the backend, never a path."""
        text = STAGE.read_text()
        start = text.index("hosts = [r.get(\"host\") for r in (spec.get(\"rules\") or [])]")
        end = text.index("if any((d.get(\"spec\") or {}).get(\"tls\") for d in ing):")
        body = text[start:end]
        helpers = text[text.index("def oname(v)"):text.index("ing = [d for d in docs if d.get(\"kind\") == \"Ingress\"]")]
        pre = ("import re\nimport shown as display\n" + helpers + "problems = []\nhost = 'intake.example.org'\n"
               "spec = {'rules': [{'host': host, 'http': {'paths': ["
               "{'path': '/webhook/github', 'pathType': 'Prefix', 'backend': {'service': {'name': 'orbit-agents-intake'}}},"
               "{'path': '/webhook/PLANTED-PATH-TOKEN', 'pathType': 'Prefix', 'backend': {'service': {'name': 'other'}}}]}}]}\n")
        proc = subprocess.run([sys.executable, "-c", pre + body + "\nprint('\\n'.join(problems))"], capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("a path rule backs Service other, not orbit-agents-intake", proc.stdout)
        self.assertIn("publishes 2 path rule(s)", proc.stdout)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_a_chart_that_will_not_render_is_named_without_helms_message(self):
        """The service resolver, run over an Application whose chart copy fails in a
        template that quotes a planted value: the finding names the file and helm's exit
        code, and the value stays in helm's stderr."""
        text = STAGE.read_text()
        code = text.split("SVCPORT=$(python3 - . <<'PY'\n", 1)[1].split("\nPY\n)", 1)[0]
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            shutil.copytree(ROOT / "charts" / "service", root / "charts" / "service")
            (root / "charts/service/templates/boom.yaml").write_text('{{ fail "PLANTED-HELM-CREDENTIAL" }}\n')
            (root / "clusters/platform/apps").mkdir(parents=True); (root / "catalog").mkdir()
            (root / "clusters/platform/apps/app.yaml").write_text(
                "apiVersion: argoproj.io/v1alpha1\nkind: Application\nmetadata: {name: a}\n"
                "spec:\n  destination: {namespace: a-prod}\n  source: {path: charts/service, helm: {valuesObject: {project: a, env: prod}}}\n")
            proc = subprocess.run([sys.executable, "-c", code, "."], cwd=td, capture_output=True, text=True, timeout=120)
        if "helm is not installed" in proc.stdout:
            self.skipTest("helm is not installed here")
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("could not render ./clusters/platform/apps/app.yaml", proc.stdout)
        self.assertIn("helm exited", proc.stdout)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)

    def test_the_firing_row_ignores_only_the_two_alerts_that_fire_by_design(self):
        """Watchdog and InfoInhibitor fire on every healthy kube-prometheus-stack; anything
        else firing is named."""
        text = STAGE.read_text()
        code = text.split("  FIRING=$(python3 -c \"\n", 1)[1].split("\" \"$TMP/alerts.json\" 2>/dev/null); FIRINGRC=$?", 1)[0]
        with tempfile.TemporaryDirectory() as td:
            alerts = pathlib.Path(td) / "alerts.json"
            alerts.write_text(json.dumps([{"labels": {"alertname": n}} for n in ("Watchdog", "InfoInhibitor", "KubeQuotaAlmostFull")]))
            proc = subprocess.run([sys.executable, "-c", code, str(alerts)], capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.stdout.strip(), "KubeQuotaAlmostFull", proc.stderr)
            # round 3 (auditor): an alertname is a label value from the cluster — one that is
            # not a name is counted, never printed
            alerts.write_text(json.dumps([{"labels": {"alertname": n}} for n in ("Watchdog", "KubeQuotaAlmostFull", "PLANTED token=x y")]))
            proc = subprocess.run([sys.executable, "-c", code, str(alerts)], capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.stdout.strip(), "KubeQuotaAlmostFull (+1 alert(s) not of an alert-name shape, or of a credential's shape)", proc.stderr)
            alerts.write_text(json.dumps([{"labels": {"alertname": n}} for n in ("Watchdog", "InfoInhibitor")]))
            proc = subprocess.run([sys.executable, "-c", code, str(alerts)], capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.stdout.strip(), "", proc.stderr)

    def test_it_reads_its_identity_from_the_instance_config(self):
        """Parameterisation, positively: the stage must source lib-instance.sh and use the
        exported values rather than any literal of its own."""
        self.assertIn('. "$(dirname "$0")/../lib-instance.sh"', self.text)
        for var in ("ORBIT_DOMAIN", "ORBIT_INSTANCE_NAME", "ORBIT_CONFIG_DIR"):
            # Either spelling: $VAR and ${VAR} are the same reference.
            self.assertRegex(self.text, r"\$\{?" + var + r"\b",
                             f"${var} is never used — the stage has stopped taking its "
                             "identity from instance/config.yaml")


# The values that turn template/instance/config.yaml.example into a config `make init`
# accepts. Deliberately an ORG THAT IS NOT THE ONE THIS STAGE CAME FROM: the fixture is
# also, incidentally, a second copy of the instance-string guard.
FIXTURE = {
    # A DISTINCTIVE name, not "fixture": the leak checker greps every value in this config
    # over everything core vendors, and the bare word appears in prose all over stage3 and
    # lib-instance.sh — so a short fixture name makes the check red before anything has
    # leaked, and a red that is always red proves nothing. Same reason and same spelling as
    # scripts/test_instance_init.py's.
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
    "secrets.projectSlug": "fixture-slug",
    "objectStorage.buckets.platform": "fixtureplatform",
    "objectStorage.buckets.appData": "fixtureapps",
    "contacts.adminFirstName": "Fixture",
    "contacts.adminLastName": "Operator",
    "contacts.telegramChatId": "1",
    "contacts.email": "ops@fixture.org",
}
# Every gate OPEN. A closed gate DELETES the artifact, so a mutation that deletes it too
# would be indistinguishable from the gate doing its job — the mutations below are only
# meaningful on a scaffold whose config asks for both planes.


def build_scaffold(dest: pathlib.Path, provider="sops") -> None:
    """A fresh org, born the way ONBOARDING says: copy template/, fill the example config,
    vendor the paths core-manifest.txt lists, run `make init`. No kubeconfig goes near it,
    which is what makes every run below a PRE-BOOT run."""
    shutil.copytree(ROOT / "template", dest, dirs_exist_ok=True)
    for line in (ROOT / "core-manifest.txt").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # `<core path> -> <instance path>` for the one kind of artifact whose path in core
        # is not its path here (a file under template/, which a new org gets by copying
        # that tree, that must also reach an org that already exists). scripts/core-sync.sh
        # is the authority on this grammar; this mirrors its split, and a line with more
        # than one arrow is core-sync's error to report, not this fixture's to guess at.
        src_rel, _, dst_rel = line.partition(" -> ")
        dst_rel = dst_rel or src_rel
        src, dst = ROOT / src_rel, dest / dst_rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        elif src.exists():
            shutil.copy2(src, dst)
        else:
            raise AssertionError(f"core-manifest.txt names a path core does not have: {line}")
    cfg = yaml.safe_load((dest / "instance" / "config.yaml.example").read_text())
    for path, value in FIXTURE.items():
        *parents, leaf = path.split(".")
        node = cfg
        for k in parents:
            node = node[k]
        node[leaf] = value
    for n in cfg["nodes"]:
        n["ip"] = "198.51.100.10"
    cfg["intakeIngress"] = "wired"
    cfg.setdefault("secrets", {})["storeConditions"] = {"prod": ["orbit-agents-prod"]}
    # THE SECRET PLANE. The stage under test checks the sops plane, so the fixture
    # asks for it by name: a scaffold on any other provider renders operator and
    # vault artifacts the gated-tree row expects gone.
    cfg["secrets"]["provider"] = provider
    (dest / "instance" / "config.yaml").write_text(yaml.safe_dump(cfg))
    init = subprocess.run([sys.executable, "scripts/instance-init.py"],
                          cwd=dest, capture_output=True, text=True)
    if init.returncode != 0:
        raise AssertionError("instance-init.py refused the fixture config:\n"
                             + init.stdout + init.stderr)


def _offline_getent_bindir():
    """A bindir whose getent always reports UNRESOLVABLE.

    Prepended to PATH, it makes every shared pre-boot fixture network-independent
    by default: no lookup can fall through to either ambient PATH or the system's
    NSS resolver."""
    bindir = pathlib.Path(tempfile.mkdtemp(prefix="orbit-offline-dns-"))
    (bindir / "getent").write_text("#!/bin/sh\nexit 1\n")
    (bindir / "getent").chmod(0o755)
    return bindir


def run_stage(tree: pathlib.Path):
    """The stage, pre-boot, in that tree. KUBECONFIG names a path that does not exist, which
    is exactly how stage3 decides pre-boot and how a scaffolded-not-bootstrapped org runs.

    Network-independent by default: an offline getent stub rejects every lookup,
    so no mutation case consults ambient DNS."""
    bindir = _offline_getent_bindir()
    try:
        env = dict(os.environ, KUBECONFIG=str(tree / "no-such-kubeconfig"),
                   PATH=str(bindir) + os.pathsep + os.environ.get("PATH", ""))
        return subprocess.run(["bash", "scripts/verify/stage5-platform.sh"],
                              cwd=tree, capture_output=True, text=True, env=env)
    finally:
        shutil.rmtree(bindir, ignore_errors=True)


class MutationTest(unittest.TestCase):
    """Behavioural, not textual: break one thing, assert the named FAIL line appears.

    The scaffold is built and the baseline run ONCE for the whole class — building it is
    milliseconds, but each stage run is a few seconds of python subprocesses and there are
    a handful of mutations."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="orbit-stage5-mutation-")
        cls.scaffold = pathlib.Path(cls._tmp) / "scaffold"
        # SOPS BY NAME: the stage under test checks the sops plane, and a fixture
        # that relied on the template's shipped provider would silently start
        # testing another plane the day it changed — which is exactly what happened.
        build_scaffold(cls.scaffold, provider="sops")
        cls.baseline = run_stage(cls.scaffold)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def mutate(self, apply):
        tree = pathlib.Path(tempfile.mkdtemp(dir=self._tmp)) / "t"
        shutil.copytree(self.scaffold, tree)
        apply(tree)
        return run_stage(tree)

    def assertRedWith(self, proc, needle, mutation):
        self.assertNotEqual(proc.returncode, 0,
                            f"{mutation} left the stage GREEN:\n{proc.stdout[-3000:]}")
        fails = [l for l in proc.stdout.splitlines() if l.lstrip().startswith("FAIL")]
        self.assertTrue(any(needle in l for l in fails),
                        f"{mutation} went red, but no FAIL line mentions {needle!r} — the "
                        f"mutation may be caught by an unrelated row, which proves nothing "
                        f"about the row it is meant to exercise.\nFAIL lines were:\n"
                        + "\n".join(fails))

    # ---- the baseline the mutations are measured against -------------------------------
    def test_a_fresh_scaffold_is_green_before_bootstrap(self):
        """Every mutation below is 'this run, minus one artifact'. If the unmutated run is
        not green the mutations say nothing, so it is asserted first and by name."""
        self.assertEqual(self.baseline.returncode, 0,
                         "a fresh scaffold is NOT green pre-boot:\n"
                         + self.baseline.stdout[-4000:] + self.baseline.stderr[-2000:])
        self.assertNotIn("FAIL", self.baseline.stdout)
        self.assertRegex(self.baseline.stdout, r"stage5-platform: \d+ passed, 0 failed")

    # ---- decision 1: the endpoint oracle ------------------------------------------------
    def test_a_probes_prober_naming_a_service_that_does_not_exist_is_red(self):
        """The defect this template shipped: the blackbox chart is installed with
        `fullnameOverride: blackbox-exporter`, and the Probe named the chart's DEFAULT
        Service instead. Every platform uptime probe was then dispatched at a name that does
        not resolve — dead on arrival, with no `probe_success` series and nothing saying so.
        It is SCHEMELESS, so no URL scan could ever see it; the row parses the field."""
        def apply(tree):
            f = tree / "clusters/platform/monitoring/probes.yaml"
            f.write_text(f.read_text().replace(
                "url: blackbox-exporter.monitoring.svc:9115",
                "url: kube-prometheus-stack-blackbox-exporter.monitoring.svc:9115"))
        self.assertRedWith(self.mutate(apply),
                           "names Service kube-prometheus-stack-blackbox-exporter.monitoring",
                           "renaming the Probe's prober Service to one nothing declares")

    def test_a_dead_cluster_is_one_loud_failure_and_not_a_cascade(self):
        """Round 2, Sol. A kubeconfig pointing at an unreachable server used to produce 38
        FAIL lines, 37 of which said things like "Prometheus is not ready" about a cluster
        nobody could reach — noise around the one line that matters, and an operator reading
        it starts debugging Prometheus. One cluster, one FAIL; every live row keyed off in
        the UNREACHABLE wording; every STATIC row still runs, because none of them needs a
        cluster."""
        dead = pathlib.Path(self._tmp) / "dead-kubeconfig"
        dead.write_text(
            "apiVersion: v1\nkind: Config\n"
            "clusters:\n- name: dead\n  cluster:\n    server: https://127.0.0.1:9\n"
            "    insecure-skip-tls-verify: true\n"
            "contexts:\n- name: dead\n  context: {cluster: dead, user: nobody}\n"
            "current-context: dead\n"
            "users:\n- name: nobody\n  user:\n    token: not-a-real-credential\n")
        tree = pathlib.Path(tempfile.mkdtemp(dir=self._tmp)) / "t"
        shutil.copytree(self.scaffold, tree)
        # curl must fail too, or the rows that dial a public hostname would reach the real
        # internet from a test — this is about the CLUSTER being unreachable, nothing else.
        # getent is stubbed offline here as well: no row under test should consult
        # ambient DNS.
        nonet = pathlib.Path(self._tmp) / "nonet"
        nonet.mkdir(exist_ok=True)
        (nonet / "curl").write_text("#!/bin/sh\nexit 1\n")
        (nonet / "curl").chmod(0o755)
        (nonet / "getent").write_text("#!/bin/sh\nexit 1\n")
        (nonet / "getent").chmod(0o755)
        env = dict(os.environ, KUBECONFIG=str(dead),
                   PATH=str(nonet) + os.pathsep + os.environ.get("PATH", ""))
        proc = subprocess.run(["bash", "scripts/verify/stage5-platform.sh"],
                              cwd=tree, capture_output=True, text=True, env=env)
        fails = [l for l in proc.stdout.splitlines() if l.lstrip().startswith("FAIL")]
        self.assertEqual(len(fails), 1,
                         "a dead cluster produced %d FAIL lines, not one:\n%s"
                         % (len(fails), "\n".join(fails)))
        self.assertIn("the cluster did not answer", fails[0])
        self.assertNotEqual(proc.returncode, 0, "a dead cluster must still be a red run")
        # The static half is the half that can still be answered, and it must have been.
        self.assertIn("every staged artifact whose gate is OPEN is rendered", proc.stdout)
        self.assertGreaterEqual(proc.stdout.count("UNREACHABLE:"), 10,
                                "live rows are not announcing WHY they keyed off")

    def test_a_missing_helm_names_itself(self):
        """Round 2, Muse. Half the Services in this repo exist only after Helm renders
        charts/service, so without helm the port table is incomplete and no URL naming one
        can be judged — red is right. But the call had no FileNotFoundError guard, so it
        tracebacked and printed a bare `FAIL static-parse:` with no detail: a red an
        operator cannot act on."""
        tree = pathlib.Path(tempfile.mkdtemp(dir=self._tmp)) / "t"
        shutil.copytree(self.scaffold, tree)
        # A catalog Application that renders through charts/service is what makes the
        # resolver reach for helm at all; a zero-project scaffold never calls it.
        appdir = tree / "clusters" / "apps"
        appdir.mkdir(parents=True, exist_ok=True)
        (appdir / "fixture-web.yaml").write_text(yaml.safe_dump({
            "apiVersion": "argoproj.io/v1alpha1", "kind": "Application",
            "metadata": {"name": "fixture-web", "namespace": "argocd"},
            "spec": {"project": "default",
                     "destination": {"server": "https://kubernetes.default.svc",
                                     "namespace": "fixture-prod"},
                     "source": {"path": "charts/service", "repoURL": "x",
                                "helm": {"valuesObject": {"name": "fixture-web"}}}},
        }))
        # Drop only the PATH entries that actually hold a helm, so everything else the
        # stage needs (python3, kubectl, coreutils) is exactly as it was. A stub named
        # `helm` would not do: it would RUN and fail with a return code, which is the
        # branch that already had a message. The branch under test is the one where the
        # binary is not there at all.
        nohelm = os.pathsep.join(
            d for d in os.environ.get("PATH", "").split(os.pathsep)
            if d and not os.path.exists(os.path.join(d, "helm")))
        # Offline DNS first: no row under test should consult ambient DNS. The stub
        # dir holds no helm, so the trimmed branch under test is preserved.
        _off = _offline_getent_bindir()
        self.addCleanup(shutil.rmtree, _off, ignore_errors=True)
        env = dict(os.environ, KUBECONFIG=str(tree / "no-such-kubeconfig"),
                   PATH=str(_off) + os.pathsep + nohelm)
        self.assertIsNone(shutil.which("helm", path=nohelm),
                          "helm is still reachable on the trimmed PATH — the test would "
                          "exercise the wrong branch")
        proc = subprocess.run(["bash", "scripts/verify/stage5-platform.sh"],
                              cwd=tree, capture_output=True, text=True, env=env)
        self.assertNotIn("Traceback", proc.stderr,
                         "the resolver still tracebacks when helm is absent")
        self.assertRedWith(proc, "helm is not installed",
                           "running the endpoint resolver with helm off PATH")

    # ---- decision 4: pre-boot coverage ---------------------------------------------------
    def test_deleting_a_bucket_gated_application_with_the_bucket_declared_is_red(self):
        """A gate CLOSED deletes the artifact and that is correct; a gate OPEN with the
        artifact gone is an org that asked for a log store and did not get one. Every live
        row about Loki keys off 'the app is not deployed', so before this row a scaffold
        with loki.yaml deleted was 40/0 green."""
        self.assertRedWith(
            self.mutate(lambda tree: (tree / "clusters/platform/apps/loki.yaml").unlink()),
            "clusters/platform/apps/loki.yaml is MISSING",
            "deleting the bucket-gated loki Application with the bucket still declared")

if __name__ == "__main__":
    unittest.main(verbosity=2, argv=[sys.argv[0]] + sys.argv[1:])
