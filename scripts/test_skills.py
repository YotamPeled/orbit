#!/usr/bin/env python3
"""
The operator skills (skills/*/SKILL.md, docs/SKILLS.md) against the orbitd tool registry.

A skill is a procedure over tools, so the one way it goes stale is by naming a tool the
server does not register (renamed, removed, never built) — and an agent following such a
skill fails at the first call with nothing to tell it why. So the registry is the oracle
here: the same `build_server` the tests in services/orbitd use, over the in-memory
backends, which is the only place the set of registered names exists. Run as
`python3 -m pytest scripts/test_skills.py -q` from the repo root.

The registry needs the pinned SDK. When this interpreter cannot import it, the names are
read under services/orbitd/.venv, built the way services/orbitd/conftest.py builds it, so
the check means the same thing on a machine with the SDK installed and on one without.
"""
import importlib.util
import pathlib
import re
import subprocess
import sys
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
SKILLS = ROOT / "skills"
ORBITD = ROOT / "services" / "orbitd"
OPERATOR = "orbit-operator"
TOOL_TOKEN = re.compile(r"\borbit_[a-z0-9_]+\b")

# Runs inside services/orbitd under an interpreter that can import the SDK. It is the
# server the tests build (tests/conftest.py), minus the HTTP layer: the registry is
# populated by build_server, which is the only place tools.register and
# tools_viewer.register are called.
DUMP_NAMES = r"""
import pathlib, tempfile
from orbitd import auth
from orbitd.config import Config
from orbitd.server import build_server
from orbitd.state import AppState
from orbitd.testing import FakeBackends
tmp = pathlib.Path(tempfile.mkdtemp())
key = tmp / "signing.pem"
auth.generate_key(str(key))
cfg = Config(mcp_url="https://mcp.fixture.invalid/mcp", signing_key_path=str(key),
             principals_path=str(tmp / "principals.json"), revocations_path=str(tmp / "revoked.txt"),
             issuer_url="https://mcp.fixture.invalid")
_, registry = build_server(AppState.build(cfg, backends=FakeBackends.build(tmp)))
print("\n".join(sorted(registry.names())))
"""


def _interpreter() -> str:
    """This interpreter if it can import the SDK; otherwise services/orbitd/.venv, built
    by the same helpers the orbitd conftest uses (one bootstrap, not two)."""
    spec = importlib.util.spec_from_file_location("orbitd_conftest", ORBITD / "conftest.py")
    conftest = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(conftest)
    if not conftest._deps_missing():
        return sys.executable
    py = conftest._venv_python()
    if not py.exists():
        subprocess.run([sys.executable, "-m", "venv", str(conftest.VENV)], check=True)
        subprocess.run([str(py), "-m", "pip", "install", "-q", "-e", f"{ORBITD}[test]"], check=True)
    return str(py)


def registered_tools() -> set[str]:
    proc = subprocess.run([_interpreter(), "-c", DUMP_NAMES], cwd=ORBITD,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError("could not read the orbitd registry:\n" + proc.stderr)
    names = set(proc.stdout.split())
    if not names:
        raise RuntimeError("the orbitd registry came back empty; the check would be vacuous")
    return names


def skill_files() -> dict[str, pathlib.Path]:
    return {d.name: d / "SKILL.md" for d in sorted(SKILLS.iterdir()) if d.is_dir()}


def frontmatter(path: pathlib.Path) -> dict:
    text = path.read_text()
    m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    if m is None:
        return {}
    data = yaml.safe_load(m.group(1))
    return data if isinstance(data, dict) else {}


class SkillsAgainstTheRegistry(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tools = registered_tools()
        cls.skills = skill_files()

    def test_the_skill_set_is_the_shipped_one(self):
        """Six directories, each with a SKILL.md; the operator skill among them. A
        directory without a SKILL.md is a skill a host cannot load."""
        self.assertIn(OPERATOR, self.skills)
        for name, path in self.skills.items():
            self.assertTrue(path.is_file(), f"skills/{name}/ has no SKILL.md")

    def test_every_tool_a_skill_names_is_registered(self):
        """THE check. A skill naming orbit_deploy_ship today sends the agent to a tool
        that does not exist; the failure names the skill and the token."""
        for name, path in self.skills.items():
            named = set(TOOL_TOKEN.findall(path.read_text()))
            unknown = sorted(named - self.tools)
            self.assertEqual(unknown, [],
                             f"skills/{name}/SKILL.md names tools the registry does not have: {unknown}")

    def test_every_registered_tool_is_named_by_some_skill(self):
        """The addendum's rule in the other direction: skills ship WITH the tools. A tool
        no skill mentions is one an agent has to discover by guessing."""
        named = set()
        for path in self.skills.values():
            named |= set(TOOL_TOKEN.findall(path.read_text()))
        orphans = sorted(self.tools - named)
        self.assertEqual(orphans, [], f"registered tools no skill names: {orphans}")

    def test_every_skill_has_name_and_description_frontmatter(self):
        """The two keys a host reads to list and route skills; `name` must be the
        directory name or the host loads it under one name and the operator table
        routes to another."""
        for name, path in self.skills.items():
            fm = frontmatter(path)
            self.assertEqual(fm.get("name"), name,
                             f"skills/{name}/SKILL.md frontmatter name is {fm.get('name')!r}")
            self.assertTrue(isinstance(fm.get("description"), str) and fm["description"].strip(),
                            f"skills/{name}/SKILL.md has no description in its frontmatter")

    def test_the_operator_skill_names_every_sibling(self):
        """orbit-operator is the router: a sibling it does not name is a skill the agent
        never reads."""
        text = self.skills[OPERATOR].read_text()
        missing = [n for n in self.skills if n != OPERATOR and n not in text]
        self.assertEqual(missing, [], f"skills/{OPERATOR}/SKILL.md does not name: {missing}")

    def test_the_docs_name_every_skill(self):
        """docs/SKILLS.md is where a host owner learns what to load."""
        text = (ROOT / "docs" / "SKILLS.md").read_text()
        missing = [n for n in self.skills if n not in text]
        self.assertEqual(missing, [], f"docs/SKILLS.md does not name: {missing}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
