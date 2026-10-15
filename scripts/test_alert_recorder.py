"""The alert recorder (template monitoring/alert-recorder.yaml) proves an alert was
DELIVERED (docs/V1.md §6.3). It must record every label, because the label that names
which thing alerted (pod, service, job) is how a test tells its own alert from another
one of the same name: with only alertname/namespace/severity kept, the §6.3 driver could
not match its throwaway pods and read delivered alerts as undelivered (2026-10-05). It
must never record annotations: alert text stays out of the log store. Runs the real
recorder.py from the manifest."""
from __future__ import annotations

import json
import pathlib
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "template/provisioning/clusters/platform/config/monitoring/alert-recorder.yaml"


def recorder_source() -> str:
    for doc in yaml.safe_load_all(MANIFEST.read_text(encoding="utf-8")):
        if doc and doc.get("kind") == "ConfigMap":
            return doc["data"]["recorder.py"]
    raise AssertionError("no ConfigMap with recorder.py in the manifest")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class AlertRecorderTest(unittest.TestCase):
    def setUp(self):
        src = recorder_source()
        self.port = free_port()
        # the manifest runs it on a fixed port; the test serves it on a free one
        self.assertIn("HTTPServer", src)
        tmp = pathlib.Path(tempfile.mkdtemp())
        script = tmp / "recorder.py"
        script.write_text(src, encoding="utf-8")
        runner = tmp / "run.py"
        runner.write_text(
            "import runpy, http.server, sys\n"
            "real = http.server.HTTPServer\n"
            f"http.server.HTTPServer = lambda addr, h: real(('127.0.0.1', {self.port}), h)\n"
            f"runpy.run_path({str(script)!r}, run_name='__main__')\n", encoding="utf-8")
        self.proc = subprocess.Popen([sys.executable, str(runner)], stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.proc.kill)
        for _ in range(100):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{self.port}/", timeout=1)
                return
            except OSError:
                time.sleep(0.05)
        self.fail("the recorder did not start: " + (self.proc.stderr.read() if self.proc.poll() is not None else ""))

    def post(self, body: dict) -> list[dict]:
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/", data=json.dumps(body).encode(),
                                     method="POST", headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5)
        self.proc.kill()
        out = self.proc.communicate(timeout=5)[0]
        return [json.loads(line) for line in out.splitlines() if line.strip()]

    def test_every_label_is_recorded_and_no_annotation(self):
        recs = self.post({"alerts": [{
            "status": "firing", "startsAt": "2026-10-05T05:14:11Z", "endsAt": "0001-01-01T00:00:00Z",
            "labels": {"alertname": "PlatformContainerRestarted", "namespace": "monitoring",
                       "pod": "e2e-crashloop-6d9f", "container": "e2e-crashloop", "severity": "warning"},
            "annotations": {"summary": "SECRET-SUMMARY-TEXT", "description": "SECRET-DESCRIPTION-TEXT"}}]})
        self.assertEqual(len(recs), 1)
        r = recs[0]
        self.assertEqual(r["pod"], "e2e-crashloop-6d9f")
        self.assertEqual(r["container"], "e2e-crashloop")
        self.assertEqual((r["alertname"], r["namespace"], r["severity"], r["status"], r["recorded"]),
                         ("PlatformContainerRestarted", "monitoring", "warning", "firing", "alertmanager"))
        dumped = json.dumps(recs)
        self.assertNotIn("SECRET-SUMMARY-TEXT", dumped)
        self.assertNotIn("SECRET-DESCRIPTION-TEXT", dumped)

    def test_a_label_cannot_forge_the_records_own_keys(self):
        recs = self.post({"alerts": [{"status": "resolved", "labels": {
            "alertname": "X", "status": "firing", "recorded": "a-tenant"}}]})
        self.assertEqual((recs[0]["status"], recs[0]["recorded"]), ("resolved", "alertmanager"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
