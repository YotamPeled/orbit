#!/usr/bin/env python3
"""No verify stage passes a credential, or a log line, as a process argument.

A process's arguments are readable by every user on the host (`ps`, /proc/*/cmdline);
its stdin is not. The stages that sign in (the dashboard's admin, the dev basic-auth
user) hand curl the password as a config document on stdin (`-K -`), written by the
python from the credential FILE and escaped as curl's quoted syntax wants, so the value
is in no process's arguments at any point and a password with a quote or a backslash
still signs in. The same holds for a log line: the collection rows carry a pod's newest
stdout line, which may hold anything an application printed, so it travels between the
helpers in files of the run's own, reaches curl inside the same config document, is never
a shell value (a traced shell prints every value it holds), and a failing row never
prints it. This
contract holds all of that: no `-u`/`--user`/Authorization argument carries a shell
expansion; the credential variable never appears inside a command word other than the
config line; no python one-liner takes the log line as an argument; curl, when present,
reads the planted password shapes back unchanged. The onboarding verb runs these stages
as its acceptance step under the skill's guarantee that no value passes on a command
line, which is why the rule lives here and not in a review note.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STAGES = sorted((ROOT / "scripts" / "verify").glob("stage*.sh"))
STAGE5 = ROOT / "scripts" / "verify" / "stage5-platform.sh"
STAGE3 = ROOT / "scripts" / "verify" / "stage3.sh"

ARGV_AUTH = re.compile(r'''(?:^|\s)(?:-u|--user|--proxy-user|-H\s+["']?Authorization)\s*["']?[^"'\s]*\$''')
# The three shell lines, byte for byte, as the stages must carry them.
# The one helper both stages sign in through: a Python process reads the password FILE
# (its path is the only argument) and prints the `user = "..."` config line curl reads on
# stdin. The shell never holds the value, so a traced shell (bash -x) shows the path only.
CURLUSER = r'''curluser(){ python3 -c 'import sys;v=open(sys.argv[1]).read().replace("\n","").replace("\r","");v=v.replace("\\","\\\\").replace("\"","\\\"");print("user = \"%s:%s\"" % (sys.argv[2], v))' "$1" "$2"; }'''
GRAFAUTH = r'''grafauth(){ curluser "$GRAFPW_FILE" admin; }'''
DEV_SIGN_IN = r'''C_OK=$(curluser "$ORBIT_CONFIG_DIR/dev-basic-auth-password" orbit | curl -q -s -o /dev/null -w '%{http_code}' --max-time 15 -K - --noproxy '*' --resolve "$host:443:$ORBIT_CONTROL_PLANE_IP" "$url")'''
LOKI_QUERY = r'''LRANGE_GRAFANA(){ grafpinned || return 1; { grafauth; python3 -c 'import sys;v=open(sys.argv[1],"rb").read().decode("utf-8","surrogateescape");v=v.replace("\\","\\\\").replace("\"","\\\"");sys.stdout.buffer.write(("data-urlencode = \"query=%s\"\n" % v).encode("utf-8","surrogateescape"))' "$1"; } | curl -q -s --max-time 25 -K - "${GRAFPIN[@]}" --get \
'''
HELD_FILE = r'''HELD(){ local w; w=$(python3 -c '
'''
# A use of $PW that is not an assignment, an emptiness test or one of the lines above.
PASSWORD_FILES = ("grafana-admin-password", "dev-basic-auth-password")


class NoArgvCredentials(unittest.TestCase):
    def test_stages_exist(self):
        self.assertTrue(STAGES, "no verify stages found")

    def test_no_basic_auth_argument_carries_an_expansion(self):
        for stage in STAGES:
            for n, line in enumerate(stage.read_text().splitlines(), 1):
                self.assertIsNone(ARGV_AUTH.search(line),
                                  f"{stage.name}:{n}: a credential on curl's command line: {line.strip()[:80]}")

    def test_no_shell_variable_ever_holds_a_password(self):
        """A traced shell (`bash -x`) prints every assignment and every expanded word, so
        the value must never be a shell value at all: the password files are read only by
        the curluser helper, whose argument is the path. Sweep area 1 (2026-09-13)."""
        for stage in STAGES:
            text = stage.read_text()
            for n, line in enumerate(text.splitlines(), 1):
                s = line.strip()
                if s.startswith("#"):
                    continue
                self.assertNotIn("$PW", line, f"{stage.name}:{n}: a password in a shell variable")
                for f in PASSWORD_FILES:
                    if f in line and "curluser" not in line and re.search(r"(<|\bcat\b|\btr\b|\bread\b|\$\()", line.split("#")[0]) \
                            and not re.search(r'\b(ok|bad|keyoff\w*)\s+"', line):
                        self.fail(f"{stage.name}:{n}: the password file is read outside curluser: {s[:100]}")

    def test_a_traced_shell_shows_the_path_and_never_the_password(self):
        """`bash -x` on the sign-in helper: the trace carries the file's path and not one
        byte of its content."""
        if not shutil.which("bash"):
            self.skipTest("bash is needed")
        with tempfile.TemporaryDirectory() as td:
            pwfile = Path(td) / "grafana-admin-password"
            pwfile.write_text("PLANTED-TRACED-PASSWORD\n")
            script = ("source <(grep -E '^(curluser|grafauth)\\(\\)' \"$2\"); GRAFPW_FILE=\"$1\"; grafauth > /dev/null")
            proc = subprocess.run(["bash", "-x", "-c", script, "_", str(pwfile), str(STAGE5)], capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(str(pwfile), proc.stderr)
        self.assertNotIn("PLANTED", proc.stderr + proc.stdout)

    def test_the_stdin_form_is_present_where_sign_in_happens(self):
        """The dashboard stage signs in through one helper (a function whose output is the
        config document) piped into `curl -K -`; the dev stage inlines the same pipe; both
        escape through the same curlq."""
        s5, s3 = STAGE5.read_text(), STAGE3.read_text()
        self.assertIn(CURLUSER, s5)
        self.assertIn(CURLUSER, s3)
        self.assertIn(GRAFAUTH, s5)
        self.assertIn(DEV_SIGN_IN, s3)
        self.assertGreaterEqual(len(re.findall(r"grafauth \| curl [^\n]* -K -", s5)), 2)
        self.assertEqual(s5.count(LOKI_QUERY), 1)

    def test_a_log_line_never_reaches_an_argument_or_a_shell_value(self):
        """The collection rows' helpers: the query (which carries the line) is in curl's
        stdin document, not a `--data-urlencode query=` argument; the line travels between
        KLAST, HELD and LFIND in files under the run's scratch directory, so no shell
        variable ever holds it (sweep area 1 round 2, 2026-09-13, breaker: `bash -x` printed
        the line through CTL_TXT/STXT and the HELD arguments); the failing row does not
        print it."""
        s5 = STAGE5.read_text()
        self.assertNotIn('--data-urlencode "query=', s5)
        self.assertEqual(s5.count(HELD_FILE), 1)
        self.assertNotIn("printf '%.60s'", s5)
        for gone in ("$STXT", "$CTL_TXT", "${K#", "${CTL_K#"):
            self.assertNotIn(gone, s5, f"{gone}: a shell value holds the log line")
        calls = [l for l in s5.splitlines() if re.search(r"(^|[ (=])(HELD|LFIND|KLAST) ", l) and "(){" not in l
                 and not l.lstrip().startswith("#")]
        self.assertGreaterEqual(len(calls), 5, calls)
        for line in calls:
            self.assertIn('"$TMP/', line, f"a helper is called without a scratch file: {line.strip()[:100]}")

    @unittest.skipUnless(shutil.which("bash"), "bash is needed")
    def test_a_failed_log_read_is_not_a_silent_pod(self):
        """Round 2 (2026-09-13, auditor): KLAST swallowed kubectl's failure and the caller
        read empty output as a silent pod, so a tenant whose logs could not be read (a
        forbidden read, a transient API failure) was reported healthy. The per-namespace
        block, run with a kubectl whose log reads fail: the namespace's row is a FAIL that
        says the read failed, never [SILENT]; a namespace whose pods cannot be listed, and a
        cluster whose namespaces cannot be listed, each fail their own row."""
        s5 = STAGE5.read_text()
        helpers = s5[s5.index("  LRANGE_GRAFANA(){"):s5.index("  CTL_NS=")]
        defs = "\n".join(l for l in s5.splitlines() if re.match(r"^(curluser|grafauth|curlq)\(\)", l))
        nsfn = s5[s5.index("project_namespaces(){"):s5.index("\n}\n", s5.index("project_namespaces(){")) + 3]
        block = s5[s5.index("  NOW=$(date +%s)"):s5.index("  [ \"$NNS\" -eq 0 ]")]
        ns_json = '{"items":[{"metadata":{"name":"tenant-a","labels":{"orbit.dev/project":"a"}}}]}'
        # logs-gone (QA of v1.0.0, F14): the read fails because the pod has no log to
        # read, it ended and was deleted between the list and the read, or has not
        # started a container yet. That is not a failed read: no row fails for it.
        for mode, expect, absent in (("logs-fail", "could not be read", "[SILENT]"),
                                     ("logs-gone", None, "could not be read"),
                                     ("logs-pending", None, "could not be read"),
                                     ("pods-fail", "could not be listed", "[SILENT]"),
                                     ("ns-fail", "namespaces could not be listed", "manages no namespaces")):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as td:
                root = Path(td); (root / "bin").mkdir(); tmp = root / "tmp"; tmp.mkdir()
                (root / "bin" / "kubectl").write_text(
                    "#!/bin/sh\n"
                    'case "$*" in\n'
                    f'  *"get ns"*) [ "$MODE" = ns-fail ] && exit 1; printf \'%s\' \'{ns_json}\';;\n'
                    # one pod asked for by name: its phase. A read that fails counts only
                    # for a pod that is there and has started (Running)
                    '  *"get pod "*) [ "$MODE" = logs-gone ] && exit 1; '
                    '[ "$MODE" = logs-pending ] && { printf Pending; exit 0; }; printf Running;;\n'
                    '  *"get pods"*) [ "$MODE" = pods-fail ] && exit 1; printf \'%s\' "pod-1 pod-2";;\n'
                    '  *logs*) case "$MODE" in logs-fail|logs-gone|logs-pending) exit 1;; esac; '
                    'printf \'%s\\n\' "2026-09-13T10:00:00.000000000Z a line";;\n'
                    "esac\n")
                (root / "bin" / "curl").write_text("#!/bin/sh\ncat > /dev/null\necho '{\"status\":\"success\",\"data\":{\"result\":[{\"values\":[[\"1\",\"x\"]]}]}}'\n")
                for b in ("kubectl", "curl"): (root / "bin" / b).chmod(0o755)
                pwfile = root / "grafana-admin-password"; pwfile.write_text("x\n")
                script = ("ok(){ echo \"  ok    $1\"; }; bad(){ echo \"  FAIL  $1\"; }\n" + defs + "\n" + nsfn + "\n" + helpers + "\n"
                          "CTL_STATE=ALIVE\n" + block + "\n" 'echo "NNS=$NNS"\n')
                proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=120,
                                      env={**os.environ, "PATH": str(root / "bin") + os.pathsep + os.environ["PATH"], "MODE": mode,
                                           "TMP": str(tmp), "GRAFPW_FILE": str(pwfile), "GRAF": "http://127.0.0.1:9"})
                out = proc.stdout
                if expect is None:
                    # neither pod counts: the namespace has nothing to read, which with the
                    # pipeline proven alive is a silent namespace, not a failed one
                    self.assertTrue(any("[SILENT] tenant-a" in l and l.lstrip().startswith("ok") for l in out.splitlines()), out)
                else:
                    self.assertTrue(any(expect in l and l.lstrip().startswith("FAIL") for l in out.splitlines()), out + proc.stderr[-2000:])
                self.assertNotIn(absent, out, out)
                if mode == "ns-fail":
                    self.assertIn("NNS=-1", out)

    @unittest.skipUnless(shutil.which("bash"), "bash is needed")
    def test_a_traced_shell_never_shows_the_log_line(self):
        """The three helpers sourced from the stage as they are, under `bash -x`, with a
        kubectl that prints a planted line and a curl that dumps its stdin: the trace
        (every word the shell expanded) carries the scratch paths and not the line; the
        line reaches curl's config document, quoted, and nowhere else."""
        s5 = STAGE5.read_text()
        helpers = s5[s5.index("  LRANGE_GRAFANA(){"):s5.index("  CTL_NS=")]
        defs = "\n".join(l for l in s5.splitlines() if re.match(r"^(curluser|grafauth|curlq)\(\)", l))
        # round 14: every authenticated Grafana request is pinned to the control plane, so the
        # pin's definition rides along and the test names an address (a documentation one)
        defs = s5[s5.index("GRAFPIN=()"):s5.index("grafauth(){")] + "\n" + defs
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); (root / "bin").mkdir(); tmp = root / "tmp"; tmp.mkdir()
            # a RECENT line: the stage judges a line's age, and the fixed date this used
            # (2026-09-13) aged past the window and turned the test red by itself
            stamp = (datetime.now(timezone.utc) - timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%S.000000000Z")
            (root / "bin" / "kubectl").write_text("#!/bin/sh\nprintf '%s\\n' '" + stamp + " PLANTED-LOG-LINE \"q\" \\\\ end'\n")
            # Loki's answer holds the line (JSON: `\"` for a quote, `\\\\` for each backslash);
            # round 21: the comparison happens in LFIND's parser, so the answer must carry it.
            # printf, not echo: dash (/bin/sh on Debian/Ubuntu, CI included) has echo collapse
            # backslashes, which silently turned the answer's two backslashes into one
            (root / "bin" / "curl").write_text("#!/bin/sh\ncat >> \"$CURL_STDIN\"\nprintf '%s\\n' '{\"status\":\"success\",\"data\":{\"result\":[{\"values\":[[\"1\",\"x PLANTED-LOG-LINE \\\"q\\\" \\\\\\\\ end tail\"]]}]}}'\n")
            for b in ("kubectl", "curl"): (root / "bin" / b).chmod(0o755)
            pwfile = root / "grafana-admin-password"; pwfile.write_text("PLANTED-PASSWORD\n")
            # the stage's OWN control and per-namespace blocks, as written, under bash -x
            nsfn = s5[s5.index("project_namespaces(){"):s5.index("\n}\n", s5.index("project_namespaces(){")) + 3]
            blocks = s5[s5.index("  CTL_NS=traefik"):s5.index("  [ \"$NNS\" -eq 0 ]")]
            (root / "bin" / "kubectl").write_text(
                "#!/bin/sh\n"
                'case "$*" in\n'
                '  *"get ns"*) printf \'%s\' \'{"items":[{"metadata":{"name":"tenant-a","labels":{"orbit.dev/project":"a"}}}]}\';;\n'
                '  *"get pods"*"jsonpath={.items[0]"*) printf \'%s\' "traefik-1";;\n'
                '  *"get pods"*) printf \'%s\' "pod-1";;\n'
                "  *logs*) printf '%s\\n' '" + stamp + " PLANTED-LOG-LINE \"q\" \\\\ end';;\n"
                "esac\n")
            script = ("ok(){ echo \"  ok    $1\"; }; bad(){ echo \"  FAIL  $1\"; }\n" + defs + "\n" + nsfn + "\n" + helpers + "\n" + blocks + "\n")
            proc = subprocess.run(["bash", "-x", "-c", script], capture_output=True, text=True, timeout=120,
                                  env={**os.environ, "PATH": str(root / "bin") + os.pathsep + os.environ["PATH"],
                                       "TMP": str(tmp), "GRAFPW_FILE": str(pwfile), "GRAF": "http://127.0.0.1:9",
                                       "ORBIT_DOMAIN": "fixture.org", "ORBIT_CONTROL_PLANE_IP": "203.0.113.5",
                                       "CURL_STDIN": str(root / "curl.stdin"), "FEATURE_DASHBOARDS": "1"})
            stdin_doc = (root / "curl.stdin").read_text() if (root / "curl.stdin").exists() else ""
        self.assertEqual(proc.returncode, 0, proc.stderr[-3000:])
        self.assertIn("pipeline is alive right now", proc.stdout, proc.stdout)
        self.assertIn("[ACTIVE] tenant-a", proc.stdout.replace("[IDLE] tenant-a", "[ACTIVE] tenant-a"), proc.stdout)
        self.assertIn(str(tmp / "ctl.needle"), proc.stderr)
        self.assertNotIn("PLANTED", proc.stderr, "the trace carries a value:\n" + proc.stderr[-3000:])
        self.assertNotIn("PLANTED", proc.stdout)
        # round 21: the request carries the stream selector alone — the line is compared in
        # this process and never reaches curl's document (nor, through it, a request URI)
        query = '{namespace="traefik",pod="traefik-1"}'
        self.assertIn('data-urlencode = "query=%s"' % query.replace("\\", "\\\\").replace('"', '\\"'), stdin_doc, stdin_doc)
        self.assertNotIn("PLANTED-LOG", stdin_doc, stdin_doc)
        self.assertIn('user = "admin:PLANTED-PASSWORD"', stdin_doc)

    def test_a_failing_service_address_row_never_shows_a_url_password(self):
        """The in-repo service-address checker in stage5, run as it is over a planted
        ConfigMap whose URL carries userinfo: the finding names the host, not the
        credential."""
        s5 = STAGE5.read_text()
        code = s5.split("SVCPORT=$(python3 - . <<'PY'\n", 1)[1].split("\nPY\n)", 1)[0]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "clusters").mkdir(); (root / "catalog").mkdir()
            (root / "clusters/probe.yaml").write_text(
                "kind: ConfigMap\nmetadata: {name: probe}\ndata:\n"
                "  uri: redis://user:PLANTED-URL-PASSWORD@missing.monitoring.svc:6379/PLANTED-PATH?token=PLANTED-QUERY\n"
                "  q: http://missing2.monitoring.svc?token=PLANTED-QUERY-NO-SLASH\n"
                "  f: http://missing3.monitoring.svc#PLANTED-FRAGMENT\n"
                "  s: http://user:PLANTED-SEMI;colon@missing4.monitoring.svc:80/x\n"
                "  c: http://user:PLANTED-COMMA,part@missing5.monitoring.svc:80/x\n"
                "  a: \"http://user:PLANTED-APOS'part@missing6.monitoring.svc:80/x\"\n"
                "  p: http://user:PLANTED-PAREN)part@missing7.monitoring.svc:80/x\n")
            proc = subprocess.run([sys.executable, "-c", code, "."], cwd=td, capture_output=True, text=True, timeout=120)
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("redis://missing.monitoring.svc:6379 ", proc.stdout)
        self.assertIn("names Service missing2.monitoring", proc.stdout)
        self.assertIn("names Service missing3.monitoring", proc.stdout)
        self.assertIn("names Service missing4.monitoring", proc.stdout)
        self.assertIn("names Service missing5.monitoring", proc.stdout)
        self.assertIn("names Service missing6.monitoring", proc.stdout)
        self.assertIn("names Service missing7.monitoring", proc.stdout)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr)
        self.assertNotIn("@", proc.stdout.split("names Service")[0].rsplit(" ", 1)[-1] if "names Service" in proc.stdout else "")

    def test_a_password_file_with_a_second_line_yields_one_directive(self):
        """A curl config document is line-structured: a second line in the password file
        would be a second directive (another `url =`, say, signing in elsewhere). Both
        stages drop line ends when reading the file, so the document holds one `user` line
        and the URL curl is given; a planted second line is a second directive nowhere."""
        s3, s5 = STAGE3.read_text(), STAGE5.read_text()
        self.assertIn(DEV_SIGN_IN, s3)
        self.assertIn(GRAFAUTH, s5)
        if not (shutil.which("curl") and shutil.which("bash")):
            self.skipTest("curl and bash are needed")
        with tempfile.TemporaryDirectory() as td:
            pwfile = Path(td) / "dev-basic-auth-password"
            pwfile.write_text("PLANTED-PASSWORD\nurl = https://second.invalid/capture\n#\n")
            script = ("source <(grep -E '^(curluser)\\(\\)' \"$2\"); curluser \"$1\" orbit | "
                      "curl -s -K - --libcurl - -o /dev/null --max-time 1 http://127.0.0.1:9/ 2>/dev/null | grep -c CURLOPT_URL")
            out = subprocess.run(["bash", "-c", script, "_", str(pwfile), str(STAGE5)], capture_output=True, text=True, timeout=30).stdout
        self.assertEqual(out.strip(), "1", "a second line in the password file became a second curl directive")

    def test_a_protected_hostname_is_probed_only_once_it_has_the_shape_of_one(self):
        """stage3's protected-environment block, run as it is over a catalog whose dev
        hostname carries userinfo, with a recording curl on PATH: the row names the
        project, env and position, the value is printed nowhere, and curl is never run."""
        s3 = STAGE3.read_text()
        block = s3[s3.index("PROTBAD=$(mktemp)"):s3.index("\ndone\n", s3.index("for h in $PROTHOSTS; do")) + len("\ndone\n")]
        block = "ok(){ echo \"ok $1\"; }; bad(){ echo \"FAIL $1\"; }\n" + block
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "catalog").mkdir(); (root / "bin").mkdir()
            (root / "catalog/projects.yaml").write_text(
                "projects:\n  - id: p\n    environments:\n      dev: {enabled: true, hostnames: ['user:PLANTED-DEV-PASSWORD@dev.example']}\n")
            curl = root / "bin/curl"
            curl.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\" >> \"$CURL_LOG\"\necho 000\n"); curl.chmod(0o755)
            log = root / "curl.log"
            proc = subprocess.run(["bash", "-c", block], cwd=td, capture_output=True, text=True, timeout=60,
                                  env={**os.environ, "PATH": f"{root / 'bin'}:{os.environ['PATH']}", "CURL_LOG": str(log),
                                       "ORBIT_DEV_DOMAIN": "dev.example", "ORBIT_CONFIG_DIR": str(root)})
            ran = log.read_text() if log.exists() else ""
        # round 10 (A3): the entry is named by position, never by its id (catalog text)
        self.assertIn("FAIL the catalog's protected hostnames cannot be probed: projects[0]/dev hostname 1 is not a DNS name", proc.stdout)
        self.assertNotIn("PLANTED", proc.stdout + proc.stderr + ran)
        self.assertEqual(ran, "", "curl ran on an unvalidated hostname")

    def test_a_down_target_row_names_the_target_not_its_error(self):
        """The scrape-health row prints a down target's job and scrape host:port; the
        lastError body (the remote's text, which may echo a request) and the full scrapeUrl
        (path and query) stay in Prometheus, where the row sends the reader."""
        s5 = STAGE5.read_text()
        code = s5.split("count.txt\" <<'PY' 2>/dev/null\n", 1)[1].split("\nPY\n", 1)[0]
        self.assertNotIn("lastError'][:", code)
        self.assertNotIn("'scrapeUrl'])", code)
        targets = {"data": {"activeTargets": [{"health": "down", "labels": {"job": "j"},
                   "scrapeUrl": "http://user:PLANTED-USERINFO@t.monitoring.svc:80/PLANTED-PATH?k=PLANTED-QUERY",
                   "lastError": "PLANTED-ERROR-BODY"}]}}
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "t.json").write_text(json.dumps(targets))
            proc = subprocess.run([sys.executable, "-c", code, str(Path(td) / "t.json"), str(Path(td) / "down.txt"), str(Path(td) / "n.txt")],
                                  capture_output=True, text=True, timeout=60)
            down = (Path(td) / "down.txt").read_text() if (Path(td) / "down.txt").exists() else ""
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(down.strip(), "j t.monitoring.svc:80")

    def test_the_log_needle_is_a_go_string_that_keeps_every_byte(self):
        """KLAST prints the line as a quoted Go string, LogQL's own literal form: `"` and
        backslash escaped, NUL and every control byte as a two-digit hex byte escape, a
        byte that is not UTF-8 as that byte's escape, everything else (an emoji included)
        as itself. HELD refuses a
        needle of any other shape, so a raw line never becomes an operand."""
        s5 = STAGE5.read_text()
        klast = s5[s5.index("  KLAST(){"):s5.index("  # HELD <ns>")]
        held = s5[s5.index("  HELD(){"):s5.index("  CTL_NS=")]
        kcode = klast.split("python3 -c '", 1)[1].split("' < \"$TMP/klast.raw\" > \"$4\"; }", 1)[0]
        line = "2026-09-12T10:00:00.000000000Z PLANTED \x00 \"q\" \\ \t \U0001F600 ".encode() + b"\xff tail\n"
        out = subprocess.run([sys.executable, "-c", kcode], input=line, capture_output=True, timeout=30,
                             env={**os.environ, "LC_ALL": "C", "PYTHONIOENCODING": "ascii"}).stdout.decode("utf-8")
        epoch, _, needle = out.rstrip("\n").partition("\t")
        self.assertTrue(epoch.isdigit(), out)
        self.assertEqual(needle, '"PLANTED \\x00 \\"q\\" \\\\ \\x09 \U0001F600 \\xff tail"')
        hcode = held.split("python3 -c '", 1)[1].split("' \"$1\" \"$2\" \"$3\"", 1)[0]
        with tempfile.TemporaryDirectory() as td:
            nf, qf = Path(td) / "needle", Path(td) / "query"
            nf.write_bytes(f"{epoch}\t{needle}\n".encode("utf-8"))
            good = subprocess.run([sys.executable, "-c", hcode, "ns", "pod", str(nf), str(qf)], capture_output=True, timeout=30,
                                  env={**os.environ, "LC_ALL": "C", "PYTHONIOENCODING": "ascii"})
            self.assertEqual(good.returncode, 0, good.stderr)
            self.assertEqual(qf.read_bytes().decode("utf-8"), '{namespace="ns",pod="pod"}')     # the selector alone (round 21)
            self.assertEqual(good.stdout.split(), [str(int(epoch) - 5).encode(), str(int(epoch) + 5).encode()])
            for bad in ("not a go string", '"unterminated', '"bad \\u0000 escape"', '"raw \x01 control"', '"tab\there"'):
                nf.write_bytes(f"1\t{bad}\n".encode("utf-8")); qf.unlink(missing_ok=True)
                raw = subprocess.run([sys.executable, "-c", hcode, "ns", "pod", str(nf), str(qf)], capture_output=True, timeout=30)
                self.assertNotEqual(raw.returncode, 0, f"refused: {bad!r}")
                self.assertFalse(qf.exists(), f"a refused needle still wrote a query: {bad!r}")
            # round 21: LFIND's parser reads the same Go string back and compares bytes — every
            # byte the kubelet gave (NUL, a quote, a backslash, a tab, an emoji) round-trips
            lfind = s5[s5.index("  LFIND(){"):s5.index("  KLAST(){")]
            lcode = lfind[lfind.rindex("python3 -c '") + len("python3 -c '"):lfind.rindex("' \"$4\"; }")]
            plain = subprocess.run([sys.executable, "-c", kcode], input=b"2026-09-12T10:00:00.000000000Z PLANTED \x00 \"q\" \\ \t \xf0\x9f\x98\x80 tail\n",
                                   capture_output=True, timeout=30).stdout.decode("utf-8")
            nf.write_bytes(plain.encode("utf-8"))
            held_line = "before PLANTED \x00 \"q\" \\ \t \U0001F600 tail after"
            body = json.dumps({"status": "success", "data": {"result": [{"values": [["1", "other"], ["2", held_line]]}]}})
            got = subprocess.run([sys.executable, "-c", lcode, str(nf)], input=body.encode("utf-8"), capture_output=True, timeout=30)
            self.assertEqual(got.stdout.decode().strip(), "1", got.stderr)
            got = subprocess.run([sys.executable, "-c", lcode, str(nf)], input=body.replace("PLANTED", "PLANTEX").encode("utf-8"), capture_output=True, timeout=30)
            self.assertEqual(got.stdout.decode().strip(), "0", got.stderr)

    @unittest.skipUnless(shutil.which("curl") and shutil.which("bash"), "curl and bash are needed to read the config back")
    def test_curl_reads_the_escaped_password_back_unchanged(self):
        """The two helpers, sourced from the stage as they are, feed real curl; its
        --libcurl trace shows the credential it parsed. A backslash, a double quote, a
        space and a plain value come back byte for byte (the trace is C, so the expected
        text is the C literal of the planted value)."""
        script = ("source <(grep -E '^(curluser|grafauth)\\(\\)' \"$2\"); GRAFPW_FILE=\"$1\"; grafauth | "
                  "curl -s -K - --libcurl - -o /dev/null --max-time 1 http://127.0.0.1:9/ 2>/dev/null | grep CURLOPT_USERPWD")
        for planted in ("PLANTEDabc", "PLANTED\\abc", 'PLANTED"abc', "PLA NTED", 'a\\"b', "x=y;z"):
            with tempfile.TemporaryDirectory() as td:
                pwfile = Path(td) / "grafana-admin-password"
                pwfile.write_text(planted + "\n")
                out = subprocess.run(["bash", "-c", script, "_", str(pwfile), str(STAGE5)],
                                     capture_output=True, text=True, timeout=30).stdout
            c_literal = planted.replace("\\", "\\\\").replace('"', '\\"')
            self.assertIn(f'"admin:{c_literal}");', out, f"planted {planted!r}: {out.strip()!r}")


if __name__ == "__main__":
    unittest.main()
