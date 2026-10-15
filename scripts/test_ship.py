#!/usr/bin/env python3
"""Tests for scripts/ship.sh and scripts/github-app-token.py.

No network: urllib is monkeypatched for the token and registry helpers, and the
ship.sh runs speak to a loopback fixture server. The instance repo is a local
bare + seed, never a real clone of anything remote.
"""
from __future__ import annotations

import base64
import contextlib
import http.server
import importlib.util
import io
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.error
from unittest import mock

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
SHIP = ROOT / "scripts" / "ship.sh"
TOKEN_PY = ROOT / "scripts" / "github-app-token.py"
GIT = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false"]

TOKEN_VALUE = "ghs_TESTTOKEN_do_not_leak"
DIGEST_VALUE = "sha256:" + "ab" * 32
PULL_TOKEN = "pull-token-not-for-logs"
BASIC_BLOB = base64.b64encode(f"x-access-token:{TOKEN_VALUE}".encode()).decode()
BASIC_HEADER = f"Authorization: Basic {BASIC_BLOB}"

SPEC = importlib.util.spec_from_file_location("orbit_github_app_token", TOKEN_PY)
tokenmod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tokenmod)


class FakeResponse:
    def __init__(self, body=b"", headers=None, status=200):
        self._body = body
        self.headers = headers or {}
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body

    def getcode(self):
        return self.status


def load_app_dir(tmp: pathlib.Path) -> pathlib.Path:
    app_dir = tmp / "app-config"
    app_dir.mkdir()
    key = subprocess.run(["openssl", "genrsa", "2048"],
                         capture_output=True, check=True).stdout
    (app_dir / "github-app-id").write_text("12345\n")
    (app_dir / "github-app-installation-id").write_text("67890\n")
    (app_dir / "github-app-key.pem").write_bytes(key)
    return app_dir


class TokenHelperTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.app_dir = load_app_dir(self.tmp)
        self.env = {"ORBIT_CONFIG_DIR": str(self.app_dir),
                    "GITHUB_API": "https://api.github.com"}

    def test_token_is_printed_to_stdout_and_nowhere_else(self):
        def fake_urlopen(req, timeout=None):
            self.assertIn("/app/installations/67890/access_tokens", req.get_full_url())
            self.assertTrue(req.get_header("Authorization").startswith("Bearer "))
            self.assertNotIn(TOKEN_VALUE, req.get_header("Authorization"))
            return FakeResponse(json.dumps({"token": TOKEN_VALUE}).encode())

        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(tokenmod.urllib.request, "urlopen", fake_urlopen), \
             mock.patch.dict(os.environ, self.env, clear=False), \
             contextlib.redirect_stdout(stdout), \
             contextlib.redirect_stderr(stderr):
            tokenmod.main([])
        self.assertEqual(stdout.getvalue(), TOKEN_VALUE + "\n")
        self.assertEqual(stderr.getvalue(), "")
        self.assertNotIn(TOKEN_VALUE, stderr.getvalue())

    def test_missing_config_dir_is_refused(self):
        env = os.environ.copy()
        env.pop("ORBIT_CONFIG_DIR", None)
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(SystemExit) as raised:
                tokenmod.installation_token()
        self.assertIn("ORBIT_CONFIG_DIR", str(raised.exception))

    def test_digest_returns_the_manifest_header(self):
        def fake_urlopen(req, timeout=None):
            url = req.get_full_url()
            if "/token" in url:
                return FakeResponse(json.dumps({"token": PULL_TOKEN}).encode())
            self.assertEqual(req.get_method(), "HEAD")
            self.assertIn("/v2/shipfix-org/widgets-web/manifests/main-abcdef0", url)
            return FakeResponse(headers={"Docker-Content-Digest": DIGEST_VALUE})

        with mock.patch.object(tokenmod.urllib.request, "urlopen", fake_urlopen), \
             mock.patch.dict(os.environ, {"ORBIT_REGISTRY_URL": "https://ghcr.io"}):
            digest = tokenmod.manifest_digest("ghcr.io/shipfix-org/widgets-web",
                                              "main-abcdef0")
        self.assertEqual(digest, DIGEST_VALUE)

    def test_missing_manifest_is_not_found(self):
        def fake_urlopen(req, timeout=None):
            url = req.get_full_url()
            if "/token" in url:
                return FakeResponse(json.dumps({"token": PULL_TOKEN}).encode())
            raise urllib.error.HTTPError(url, 404, "not found", hdrs=None, fp=None)

        with mock.patch.object(tokenmod.urllib.request, "urlopen", fake_urlopen):
            with self.assertRaises(SystemExit) as raised:
                tokenmod.manifest_digest("ghcr.io/shipfix-org/widgets-web",
                                         "main-abcdef0")
        self.assertEqual(str(raised.exception), "manifest not found")

    def test_digest_refuses_202_even_when_a_digest_header_is_present(self):
        def fake_urlopen(req, timeout=None):
            url = req.get_full_url()
            if "/token" in url:
                return FakeResponse(json.dumps({"token": PULL_TOKEN}).encode())
            return FakeResponse(headers={"Docker-Content-Digest": DIGEST_VALUE},
                                status=202)

        with mock.patch.object(tokenmod.urllib.request, "urlopen", fake_urlopen), \
             mock.patch.dict(os.environ, {"ORBIT_REGISTRY_URL": "https://ghcr.io"}):
            with self.assertRaises(SystemExit) as raised:
                tokenmod.manifest_digest("ghcr.io/shipfix-org/widgets-web",
                                         "main-abcdef0")
        self.assertEqual(str(raised.exception), "registry error")

    def test_digest_500_is_a_registry_error(self):
        def fake_urlopen(req, timeout=None):
            url = req.get_full_url()
            if "/token" in url:
                return FakeResponse(json.dumps({"token": PULL_TOKEN}).encode())
            raise urllib.error.HTTPError(url, 500, "error", hdrs=None, fp=None)

        with mock.patch.object(tokenmod.urllib.request, "urlopen", fake_urlopen):
            with self.assertRaises(SystemExit) as raised:
                tokenmod.manifest_digest("ghcr.io/shipfix-org/widgets-web",
                                         "main-abcdef0")
        self.assertEqual(str(raised.exception), "registry error")

    def test_a_registry_that_could_not_be_reached_once_is_asked_again(self):
        """One dropped connection between the cluster and the registry failed a ship
        outright (docs/V1.md §8): "registry unreachable" twice in seven minutes, the
        registry answering the next request each time."""
        heads = []

        def fake_urlopen(req, timeout=None):
            url = req.get_full_url()
            if "/token" in url:
                return FakeResponse(json.dumps({"token": PULL_TOKEN}).encode())
            heads.append(url)
            if len(heads) == 1:
                raise urllib.error.URLError("connection reset")
            return FakeResponse(headers={"Docker-Content-Digest": DIGEST_VALUE})

        with mock.patch.object(tokenmod.urllib.request, "urlopen", fake_urlopen), \
             mock.patch.dict(os.environ, {"ORBIT_REGISTRY_URL": "https://ghcr.io", "ORBIT_REGISTRY_RETRY_S": "0"}):
            digest = tokenmod.manifest_digest("ghcr.io/shipfix-org/widgets-web", "main-abcdef0")
        self.assertEqual(digest, DIGEST_VALUE)
        self.assertEqual(len(heads), 2)

    def test_a_registry_that_never_answers_is_unreachable_after_three_asks_and_an_answer_is_final(self):
        asks = {"n": 0}

        def never(req, timeout=None):
            asks["n"] += 1
            raise urllib.error.URLError("no route")

        env = {"ORBIT_REGISTRY_URL": "https://ghcr.io", "ORBIT_REGISTRY_RETRY_S": "0"}
        with mock.patch.object(tokenmod.urllib.request, "urlopen", never), mock.patch.dict(os.environ, env):
            with self.assertRaises(SystemExit) as raised:
                tokenmod.manifest_digest("ghcr.io/shipfix-org/widgets-web", "main-abcdef0")
        self.assertEqual(str(raised.exception), "registry unreachable")
        self.assertEqual(asks["n"], 3)

        for code, says, expected_heads in ((404, "manifest not found", 1), (403, "registry auth failed", 1),
                                           (400, "registry error", 1), (503, "registry error", 3), (429, "registry error", 3)):
            with self.subTest(code=code):
                heads = []

                def answer(req, timeout=None, code=code, heads=heads):
                    url = req.get_full_url()
                    if "/token" in url:
                        return FakeResponse(json.dumps({"token": PULL_TOKEN}).encode())
                    heads.append(url)
                    raise urllib.error.HTTPError(url, code, "x", hdrs=None, fp=None)

                with mock.patch.object(tokenmod.urllib.request, "urlopen", answer), mock.patch.dict(os.environ, env):
                    with self.assertRaises(SystemExit) as raised:
                        tokenmod.manifest_digest("ghcr.io/shipfix-org/widgets-web", "main-abcdef0")
                self.assertEqual(str(raised.exception), says)
                self.assertEqual(len(heads), expected_heads)

    def test_registry_token_file_is_sent_and_not_logged(self):
        tokfile = self.tmp / "pull.token"
        tokfile.write_text("file-pull-token\n")
        seen = {}

        def fake_urlopen(req, timeout=None):
            url = req.get_full_url()
            if "/token" in url:
                seen["auth"] = req.get_header("Authorization")
                return FakeResponse(json.dumps({"token": PULL_TOKEN}).encode())
            return FakeResponse(headers={"Docker-Content-Digest": DIGEST_VALUE})

        stdout, stderr = io.StringIO(), io.StringIO()
        env = {"ORBIT_REGISTRY_TOKEN_FILE": str(tokfile),
               "ORBIT_REGISTRY_URL": "https://ghcr.io"}
        with mock.patch.object(tokenmod.urllib.request, "urlopen", fake_urlopen), \
             mock.patch.dict(os.environ, env, clear=False), \
             contextlib.redirect_stdout(stdout), \
             contextlib.redirect_stderr(stderr):
            tokenmod.main(["--digest", "ghcr.io/shipfix-org/widgets-web", "main-abcdef0"])
        self.assertTrue(seen["auth"].startswith("Basic "))
        self.assertEqual(stdout.getvalue(), DIGEST_VALUE + "\n")
        self.assertNotIn("file-pull-token", stdout.getvalue())
        self.assertNotIn("file-pull-token", stderr.getvalue())
        self.assertNotIn(PULL_TOKEN, stdout.getvalue() + stderr.getvalue())


class FixtureHTTP(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        if self.path.endswith("/access_tokens"):
            self.server.token_posts += 1
            body = json.dumps({"token": TOKEN_VALUE}).encode()
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/token":
            body = json.dumps({"token": PULL_TOKEN}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def do_HEAD(self):
        self.server.manifest_heads += 1
        status = getattr(self.server, "manifest_status", 200)
        if status in (200, 202):
            self.send_response(status)
            self.send_header("Docker-Content-Digest", DIGEST_VALUE)
            self.end_headers()
            return
        self.send_error(status)


class ShipScriptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.app_dir = load_app_dir(self.tmp)
        self.argv_log = self.tmp / "git-argv.log"
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FixtureHTTP)
        self.httpd.manifest_status = 200
        self.httpd.token_posts = 0
        self.httpd.manifest_heads = 0
        thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.httpd.shutdown)
        self.port = self.httpd.server_address[1]
        self.origin = f"http://127.0.0.1:{self.port}"
        self.real_git = shutil.which("git")
        self.assertIsNotNone(self.real_git)
        bindir = self.tmp / "bin"
        bindir.mkdir()
        (bindir / "git").write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            f"real = {self.real_git!r}\n"
            "log = os.environ.get('SHIP_GIT_ARGV_LOG')\n"
            "if log:\n"
            "    with open(log, 'a') as fh:\n"
            "        fh.write('KEY=' + os.environ.get('GIT_CONFIG_KEY_0', '') + ' ')\n"
            "        fh.write('argv=' + '\\0'.join(sys.argv[1:]) + '\\n')\n"
            "os.execv(real, [real, *sys.argv[1:]])\n"
        )
        (bindir / "git").chmod(0o755)
        self.bindir = bindir
        (self.tmp / ".gitconfig").write_text(
            "[user]\n\tname = t\n\temail = t@t\n[commit]\n\tgpgsign = false\n")

    def make_remote(self, bump_source=None) -> tuple[pathlib.Path, pathlib.Path]:
        remote = self.tmp / "remote.git"
        seed = self.tmp / "desk"
        subprocess.run(GIT + ["init", "--bare", "--initial-branch=main", str(remote)],
                       check=True, capture_output=True)
        seed.mkdir()
        subprocess.run(GIT + ["init", "--initial-branch=main"], cwd=seed,
                       check=True, capture_output=True)
        (seed / "scripts").mkdir()
        if bump_source is None:
            shutil.copy(ROOT / "scripts" / "bump-and-push.sh",
                        seed / "scripts" / "bump-and-push.sh")
        else:
            dest = seed / "scripts" / "bump-and-push.sh"
            dest.write_text(bump_source)
            dest.chmod(0o755)
        for rel in ("bump-tag.py", "lib-instance.sh"):
            shutil.copy(ROOT / "scripts" / rel, seed / "scripts" / rel)
        (seed / "scripts" / "catalog-sync.py").write_text("#!/usr/bin/env python3\n")
        (seed / "catalog").mkdir()
        (seed / "catalog" / "projects.yaml").write_text(yaml.safe_dump({
            "projects": [{"id": "widgets",
                          "repo": "shipfix-org/widgets",
                          "components": [{"name": "web",
                                          "image": "ghcr.io/shipfix-org/widgets-web"}],
                          "environments": {"prod": {"branch": "main"},
                                           "dev": {"enabled": True,
                                                   "branch": "dev"}}}]}))
        (seed / "catalog" / "tags.yaml").write_text("tags: {}\n")
        (seed / "instance").mkdir()
        (seed / "instance" / "config.yaml").write_text(yaml.safe_dump({
            "instance": {"name": "shipfix"},
            "domains": {"primary": "shipfix.test", "dev": "dev.shipfix.test",
                        "dnsZones": ["shipfix.test"]},
            "registry": "ghcr.io/shipfix-org",
            # a GitHub URL, as the loader now requires (round 2, 2026-09-13); git's own
            # insteadOf (in this test's HOME gitconfig) points it at the local remote
            "git": {"repoUrl": "git@github.com:shipfix-org/widgets-instance.git", "github_org": "shipfix-org"}}))
        with open(self.tmp / ".gitconfig", "a") as fh:
            fh.write(f'[url "{remote}"]\n\tinsteadOf = https://github.com/shipfix-org/widgets-instance.git\n')
        subprocess.run(GIT + ["add", "."], cwd=seed, check=True, capture_output=True)
        subprocess.run(GIT + ["commit", "-q", "-m", "base"], cwd=seed,
                       check=True, capture_output=True)
        subprocess.run(GIT + ["remote", "add", "origin", str(remote)], cwd=seed,
                       check=True, capture_output=True)
        subprocess.run(GIT + ["push", "-q", "origin", "main"], cwd=seed,
                       check=True, capture_output=True)
        return remote, seed

    def ship_env(self, seed: pathlib.Path) -> dict:
        env = os.environ.copy()
        env.update({
            "ORBIT_INSTANCE_CONFIG": str(seed / "instance" / "config.yaml"),
            "ORBIT_CONFIG_DIR": str(self.app_dir),
            "GITHUB_API": self.origin,
            "ORBIT_REGISTRY_URL": self.origin,
            "ORBIT_ACTOR": "desk-operator",
            "SHIP_GIT_ARGV_LOG": str(self.argv_log),
            "PATH": str(self.bindir) + os.pathsep + env.get("PATH", ""),
            "HOME": str(self.tmp),
            "GIT_CONFIG_GLOBAL": str(self.tmp / ".gitconfig"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "desk-operator",
            "GIT_AUTHOR_EMAIL": "desk@invalid",
        })
        env.pop("GIT_TRAILERS", None)
        return env

    def run_ship(self, seed, *args, xtrace=False):
        argv = ["bash", "-x", str(SHIP), *args] if xtrace else ["bash", str(SHIP), *args]
        return subprocess.run(
            argv, cwd=seed, capture_output=True, text=True, env=self.ship_env(seed))

    def assert_credentials_absent(self, text):
        self.assertNotIn(TOKEN_VALUE, text)
        self.assertNotIn(BASIC_BLOB, text)
        self.assertNotIn(BASIC_HEADER, text)
        self.assertNotIn(PULL_TOKEN, text)

    def test_bump_commit_lands_with_six_trailers_and_token_stays_off_argv(self):
        remote, seed = self.make_remote()
        result = self.run_ship(seed, "widgets", "prod", "web", "main-abcdef0")
        out = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, out)
        for step in ("args", "config", "catalog", "registry", "token", "clone", "bump"):
            self.assertIn(f"STEP {step} OK", result.stdout, out)
        self.assert_credentials_absent(out)
        argv = self.argv_log.read_text() if self.argv_log.exists() else ""
        self.assertNotIn(TOKEN_VALUE, argv)
        self.assertIn("clone", argv)
        self.assertIn("KEY=http.extraHeader", argv)
        message = subprocess.run(
            ["git", "--git-dir", str(remote), "log", "-1", "--format=%B"],
            check=True, capture_output=True, text=True).stdout
        self.assertIn("Orbit-Action: ship", message)
        self.assertIn("Orbit-Actor: desk-operator", message)
        self.assertIn("Orbit-Role: operator", message)
        self.assertIn("Source-Commit: abcdef0", message)
        self.assertIn(f"Image-Digest: {DIGEST_VALUE}", message)
        self.assertIn("Render-Sha: n/a", message)
        self.assertNotIn(TOKEN_VALUE, message)
        tags = yaml.safe_load(subprocess.run(
            ["git", "--git-dir", str(remote), "show", "main:catalog/tags.yaml"],
            check=True, capture_output=True, text=True).stdout)
        self.assertEqual(tags["tags"]["widgets/prod/web"], "main-abcdef0")
        commit_lines = [ln for ln in result.stdout.splitlines() if ln.startswith("COMMIT ")]
        self.assertEqual(len(commit_lines), 1, out)
        sha = commit_lines[-1].split()[1]
        head = subprocess.run(
            ["git", "--git-dir", str(remote), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True).stdout.strip()
        self.assertEqual(sha, head)
        self.assertEqual(result.stdout.splitlines()[-1], f"COMMIT {sha}")

    def test_orbit_action_override_lands_in_the_trailer(self):
        remote, seed = self.make_remote()
        env = self.ship_env(seed)
        env["ORBIT_ACTION"] = "rollback"
        result = subprocess.run(
            ["bash", str(SHIP), "widgets", "prod", "web", "main-abcdef0"],
            cwd=seed, capture_output=True, text=True, env=env)
        out = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, out)
        message = subprocess.run(
            ["git", "--git-dir", str(remote), "log", "-1", "--format=%B"],
            check=True, capture_output=True, text=True).stdout
        self.assertIn("Orbit-Action: rollback", message)
        self.assertNotIn("Orbit-Action: ship", message)
        self.assertTrue(result.stdout.splitlines()[-1].startswith("COMMIT "), out)

    def test_missing_manifest_stops_before_any_clone(self):
        remote, seed = self.make_remote()
        self.httpd.manifest_status = 404
        result = self.run_ship(seed, "widgets", "prod", "web", "main-abcdef0")
        out = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0, out)
        self.assertIn("STEP registry FAIL", result.stdout)
        self.assertNotIn("STEP clone", result.stdout)
        self.assertEqual(self.httpd.token_posts, 0)
        argv = self.argv_log.read_text() if self.argv_log.exists() else ""
        self.assertNotIn("clone", argv)
        count = subprocess.run(
            ["git", "--git-dir", str(remote), "rev-list", "--count", "main"],
            check=True, capture_output=True, text=True).stdout.strip()
        self.assertEqual(count, "1")

    def test_manifest_202_with_digest_is_refused_before_clone(self):
        remote, seed = self.make_remote()
        self.httpd.manifest_status = 202
        result = self.run_ship(seed, "widgets", "prod", "web", "main-abcdef0")
        out = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0, out)
        self.assertIn("STEP registry FAIL", result.stdout)
        self.assertNotIn("STEP clone", result.stdout)
        self.assertEqual(self.httpd.token_posts, 0)
        argv = self.argv_log.read_text() if self.argv_log.exists() else ""
        self.assertNotIn("clone", argv)
        count = subprocess.run(
            ["git", "--git-dir", str(remote), "rev-list", "--count", "main"],
            check=True, capture_output=True, text=True).stdout.strip()
        self.assertEqual(count, "1")

    def test_manifest_500_stops_before_any_clone(self):
        remote, seed = self.make_remote()
        self.httpd.manifest_status = 500
        result = self.run_ship(seed, "widgets", "prod", "web", "main-abcdef0")
        out = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0, out)
        self.assertIn("STEP registry FAIL", result.stdout)
        self.assertNotIn("STEP clone", result.stdout)
        self.assertEqual(self.httpd.token_posts, 0)
        argv = self.argv_log.read_text() if self.argv_log.exists() else ""
        self.assertNotIn("clone", argv)

    def test_bash_x_does_not_print_token_or_basic(self):
        _, seed = self.make_remote()
        result = self.run_ship(seed, "widgets", "prod", "web", "main-abcdef0",
                               xtrace=True)
        out = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, out)
        self.assert_credentials_absent(out)

    def test_failed_bump_scrubs_token_and_basic_header(self):
        leak_bump = (
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "cd \"$(dirname \"$0\")/..\"\n"
            "git config --get http.extraHeader || true\n"
            "exit 1\n"
        )
        _, seed = self.make_remote(bump_source=leak_bump)
        result = self.run_ship(seed, "widgets", "prod", "web", "main-abcdef0")
        out = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0, out)
        self.assertIn("STEP bump FAIL", result.stdout)
        self.assert_credentials_absent(out)
        self.assertIn("<token>", result.stderr)

    def test_failed_clone_scrubs_basic_credential(self):
        _, seed = self.make_remote()
        (self.bindir / "git").write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            "if 'clone' in sys.argv:\n"
            "    sys.stderr.write(os.environ.get('GIT_CONFIG_VALUE_0', '') + '\\n')\n"
            "    raise SystemExit(1)\n"
            f"os.execv({self.real_git!r}, [{self.real_git!r}, *sys.argv[1:]])\n"
        )
        (self.bindir / "git").chmod(0o755)
        result = self.run_ship(seed, "widgets", "prod", "web", "main-abcdef0")
        out = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0, out)
        self.assertIn("STEP clone FAIL", result.stdout)
        self.assert_credentials_absent(out)
        self.assertIn("<token>", result.stderr)

    def test_bump_without_commit_line_uses_clone_head(self):
        silent_bump = (
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "cd \"$(dirname \"$0\")/..\"\n"
            "git config user.name \"${GIT_AUTHOR_NAME:-t}\"\n"
            "git config user.email \"${GIT_AUTHOR_EMAIL:-t@t}\"\n"
            "git config commit.gpgsign false\n"
            "echo silent >> catalog/tags.yaml\n"
            "git add catalog/tags.yaml\n"
            "git commit -qm \"vendored bump without COMMIT line\"\n"
            "git push origin HEAD:main\n"
        )
        remote, seed = self.make_remote(bump_source=silent_bump)
        result = self.run_ship(seed, "widgets", "prod", "web", "main-abcdef0")
        out = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, out)
        self.assertIn("STEP bump OK", result.stdout, out)
        self.assertNotIn("STEP bump FAIL", result.stdout, out)
        head = subprocess.run(
            ["git", "--git-dir", str(remote), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True).stdout.strip()
        commit_lines = [ln for ln in result.stdout.splitlines() if ln.startswith("COMMIT ")]
        self.assertEqual(commit_lines, [f"COMMIT {head}"], out)

    def test_failed_bump_reports_rc_and_last_line(self):
        fail_bump = (
            "#!/usr/bin/env bash\n"
            "echo \"could not push after 5 attempts\"\n"
            "exit 1\n"
        )
        _, seed = self.make_remote(bump_source=fail_bump)
        result = self.run_ship(seed, "widgets", "prod", "web", "main-abcdef0")
        out = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0, out)
        self.assertIn("STEP bump FAIL", result.stdout)
        self.assertIn("bump FAIL rc=1: could not push after 5 attempts", out)
        self.assertNotIn("COMMIT ", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
