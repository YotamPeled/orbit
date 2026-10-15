#!/usr/bin/env python3
"""
What the operator skills SAY a reader may do, against the registry that decides it.

WHY THIS FILE REPLACES THE FIRST CONTRACT. The first one tried to detect a falsehood by
its wording. It required a sentence to deny something, to be about tools, and to pin the
denial to the build with one of three phrases. A reviewer defeated it in a line: "the
current release" walks straight past "this release". Two sentences of exactly the banned
class were sitting in skills it had already scanned, and two more rows in the router said
the same thing, and it saw none of them. Its guards passed because every reworded dodge
their author invented reused one of the phrases the check had started from.

That is the third oracle of mine to enumerate the instances I happened to see instead of
the property that makes them wrong, so this is a redesign and not a repair.

Three rules, none of which tries to recognise a lie.

  1. A skill may not mention a release or a version at all. Not "not in this release", not
     "the current release", not "shipped in this version". You cannot make a claim about
     the build without naming the build, so banning the noun cannot be reworded around,
     and there is no legitimate use of it in a procedure: what the reader may do is decided
     by their token, and what a tool returns is decided by the tool.

  2. A sentence that names a role must also name something that has it: a registered tool
     the gate holds at that role, or a sibling skill declared for it. This is what the
     first contract was missing, and it let the repair trade a false claim about the build
     for a false one about a role: two skills came out of it telling the reader to hand a
     limit change to someone holding an operator token, when no tool at any role changes a
     limit. A role claim now has to carry its evidence. Two skills are exempt because roles
     are their subject rather than a fact they cite — the router, which is the map of the
     surface, and the principals skill, whose whole job is deciding which role a principal
     gets; a test proves that exemption is load-bearing rather than decoration. A skill
     whose own steps all sit at one role may also name that role about itself, which the
     check verifies before allowing.

  3. A skill declares in its frontmatter the role its numbered steps need, and lists
     separately any higher-role tool an optional tail reaches for. One key was not enough:
     a walk whose five steps are all readable but whose optional tail is not was forced to
     declare the higher role, which sends away a reader who could have run the whole walk.

And two rules about the router's calling convention, both executable rather than spelled.
The first contract confirmed every flag it showed exists while never noticing that the
invocation it showed cannot run at all, because the client refuses before it sends anything
unless a login already happened. Its replacement required the login step and was one step
short in the same way: it never asked whether the ADDRESS the passage shows works. It does
not. The client turns a bare host into that host with a trailing slash, and the server
issues tokens only for its full interface address, so signing in with the address a reader
is handed is refused with a four hundred that explains nothing. Proved by running both
forms against the live server with an existing credential: the bare host is refused, the
host with the interface path succeeds. So the passage must show a concrete example address
and not only a placeholder, and every address it shows must be one the client leaves
unchanged when it canonicalises it — which is exactly the property the bare host fails.

Run as `python3 -m pytest scripts/test_skill_claims.py -q` from the repo root.
"""
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from test_skills import ROOT, ORBITD, TOOL_TOKEN, frontmatter, skill_files, _interpreter  # noqa: E402

ROLES = ("viewer", "operator", "admin")
RANK = {name: i for i, name in enumerate(ROLES)}
ROUTER = "orbit-operator"

# A tool the skill tells the reader to CALL: written with its argument list, the way every
# numbered step writes one. A tool merely named in prose — "resolve them with
# `orbit_project_list` first", or the router's table of the whole surface — is a mention,
# and a skill may mention a tool the reader cannot call; that is how it says so.
TOOL_CALL = re.compile(r"\borbit_[a-z0-9_]+(?=\()")

DUMP_ROLES = r"""
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
for name in sorted(registry.names()):
    print(name, registry.required_role(name).value)
"""


def registered_roles() -> dict[str, str]:
    proc = subprocess.run([_interpreter(), "-c", DUMP_ROLES], cwd=ORBITD,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError("could not read the orbitd registry:\n" + proc.stderr)
    table = dict(line.split() for line in proc.stdout.split("\n") if line.strip())
    if not table:
        raise RuntimeError("the orbitd registry came back empty; the check would be vacuous")
    return table


# --- rule 1: the build is not a subject a skill may discuss ------------------------
# Nouns, not phrasings. "release" and "version" have no legitimate use in a procedure;
# "build" does (a query gets built), so only the determiner form is refused.
BUILD_NOUN = re.compile(r"\b(releases?|versions?)\b|\bthis build\b", re.I)

# --- rule 2: a role claim carries a tool at that role ------------------------------
SKILL_NAME = re.compile(r"\borbit-(operator|admin|viewer)\b", re.I)
ROLE_WORD = re.compile(r"\b(viewer|operator|admin)\b", re.I)

SENTENCE_END = re.compile(r"(?<=[.!?])\s+|\n\s*\n|\n\s*[-*\d]+[.)]?\s+|\n\|")


def units(text: str) -> list[str]:
    """Sentence-ish units. A markdown bullet, a heading and a table row each end a claim as
    surely as a full stop does, so they end a unit too: a claim whose role is in one row and
    whose tool is in another is a claim a reader cannot check either."""
    body = re.sub(r"^---\n.*?\n---\n", "", text, flags=re.DOTALL)
    body = re.sub(r"```.*?```", " ", body, flags=re.DOTALL)
    return [s.strip() for s in SENTENCE_END.split(body) if s.strip()]


def roles_named(unit: str) -> set[str]:
    return {m.lower() for m in ROLE_WORD.findall(SKILL_NAME.sub(" ", unit))}


def tools_named(unit: str, registry: dict[str, str]) -> dict[str, str]:
    return {t: registry[t] for t in TOOL_TOKEN.findall(unit) if t in registry}


class BuildIsNotASubject(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.skills = skill_files()

    def test_no_skill_mentions_a_release_or_a_version(self):
        offenders = []
        for name, path in self.skills.items():
            for line_no, line in enumerate(path.read_text().splitlines(), 1):
                if BUILD_NOUN.search(line):
                    offenders.append(f"skills/{name}/SKILL.md:{line_no}: {line.strip()[:140]}")
        self.assertEqual(offenders, [],
                         "a skill discusses the build; say what the reader's role carries, "
                         "or where the change is made:\n" + "\n".join(offenders))

    def test_the_ban_is_on_the_noun_and_not_on_one_phrasing(self):
        """The defect that replaced the first contract: every rewording of a build claim
        still has to name the build, so all of these are caught by the same rule."""
        for text in ("the change tools are not in this release",
                     "the change tools are not in the current release",
                     "not available in this version",
                     "that verb is not shipped in this release",
                     "no principal delete in this release",
                     "not yet available in this release",
                     "the release you are talking to does not have it"):
            with self.subTest(text):
                self.assertTrue(BUILD_NOUN.search(text))

    def test_the_ban_leaves_ordinary_procedure_prose_alone(self):
        for text in ("Build the query from the label names the first series carries.",
                     "Read `orbit_deploy_status(project, env)` and find the component's row.",
                     "`pods: []` means nothing is running.",
                     "A tool that is not in your list does not exist for you."):
            with self.subTest(text):
                self.assertIsNone(BUILD_NOUN.search(text))


# Roles are the subject of these two, not a fact they cite: one is the map of the whole
# surface, the other decides which role a principal gets.
ROLE_IS_THE_SUBJECT = (ROUTER, "orbit-admin")


class ARoleClaimCarriesItsTool(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.skills = skill_files()
        cls.roles = registered_roles()
        cls.declared = {name: frontmatter(path).get("role")
                        for name, path in cls.skills.items()}

    def homogeneous_role(self, path) -> str | None:
        """The single role every tool this skill tells the reader to call sits at, or None
        if it calls tools at more than one. A skill like that may speak of its own role
        without naming a tool, because the claim is about the skill and this check has just
        verified it."""
        called = {self.roles[t] for t in TOOL_CALL.findall(path.read_text()) if t in self.roles}
        return called.pop() if len(called) == 1 else None

    def siblings_named(self, unit: str) -> set[str]:
        """The role of every sibling skill this unit names. Handing a job to `orbit-ship`
        and saying it needs an operator token is a role claim backed by something the
        reader can check, the same as naming a tool."""
        return {self.declared[name] for name in self.skills
                if name in unit and self.declared.get(name) in ROLES}

    def offenders(self, *, exempt=ROLE_IS_THE_SUBJECT) -> list[str]:
        found = []
        for name, path in self.skills.items():
            if name in exempt:
                continue
            homogeneous = self.homogeneous_role(path)
            for unit in units(path.read_text()):
                named = roles_named(unit)
                if not named:
                    continue
                have = set(tools_named(unit, self.roles).values()) | self.siblings_named(unit)
                for role in sorted(named):
                    if role in have or role == homogeneous:
                        continue
                    found.append(
                        f"skills/{name}/SKILL.md names the {role} role with nothing that has "
                        f"it: {' '.join(unit.split())[:150]}")
        return found

    def test_every_role_a_skill_names_is_one_a_tool_it_names_actually_has(self):
        """THE check. Without it a repair can move a false claim from the build to a role:
        telling the reader to hand a limit change to an operator is wrong in a way no
        spelling rule sees, because no tool at any role changes a limit."""
        offenders = self.offenders()
        self.assertEqual(offenders, [],
                         "a role is named without anything that has it:\n" + "\n".join(offenders))

    def test_a_claim_backed_by_a_tool_passes_and_a_bare_one_does_not(self):
        backed = "If `orbit_deploy_ship` is not in your tool list, your token is below `operator`."
        bare = "Monitor changes need an operator token."
        self.assertIn("operator", set(tools_named(backed, self.roles).values()))
        self.assertEqual(tools_named(bare, self.roles), {})
        self.assertEqual(roles_named(bare), {"operator"})

    def test_the_router_name_is_not_mistaken_for_the_role(self):
        """Every skill points at the router by name. If that counted, every unit in the
        corpus would name a role and the rule would collapse."""
        self.assertEqual(roles_named("Read orbit-operator first."), set())
        self.assertEqual(roles_named("Read orbit-operator first; you need the operator role."),
                         {"operator"})

    def test_the_two_exempt_skills_are_exempt_for_a_reason_they_earn(self):
        """Anti-vacuity for the exemption. Both exempted skills must actually be ones this
        rule would fire on, or the list is decoration hiding nothing; and exempting any
        third skill must not be needed, which is what the rule above already proves."""
        for name in ROLE_IS_THE_SUBJECT:
            self.assertIn(name, self.skills)
            others = tuple(n for n in ROLE_IS_THE_SUBJECT if n != name)
            with self.subTest(name):
                self.assertTrue([o for o in self.offenders(exempt=others) if name in o],
                                f"skills/{name}/SKILL.md is exempted from the role rule and "
                                f"the rule would not fire on it anyway; the exemption hides "
                                f"nothing and should go")

    def test_the_registry_gates_tools_at_more_than_one_role(self):
        """Anti-vacuity: if every tool sat at one role, backing a role claim with any tool
        at all would satisfy this rule."""
        self.assertGreater(len({r for r in self.roles.values()}), 1)
        self.assertIn("operator", set(self.roles.values()))
        self.assertIn("admin", set(self.roles.values()))


class SkillsDeclareTheirAudience(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.skills = skill_files()
        cls.roles = registered_roles()

    def steps_and_tail(self, path):
        """The tools the numbered steps call, and the tools called anywhere else. A skill's
        numbered steps are its walk; everything after them is optional by construction,
        which is what one key could not express."""
        text = path.read_text()
        body = re.sub(r"^---\n.*?\n---\n", "", text, flags=re.DOTALL)
        parts = re.split(r"^##\s+(?=\d)", body, flags=re.M)
        steps, tail = [], []
        for index, part in enumerate(parts):
            target = steps if index > 0 else tail
            target.extend(t for t in TOOL_CALL.findall(part) if t in self.roles)
        return steps, tail

    def test_every_skill_declares_the_role_its_steps_need(self):
        for name, path in self.skills.items():
            with self.subTest(name):
                self.assertIn(frontmatter(path).get("role"), ROLES,
                              f"skills/{name}/SKILL.md declares role "
                              f"{frontmatter(path).get('role')!r}")

    def test_the_declared_role_is_what_the_numbered_steps_need(self):
        for name, path in self.skills.items():
            declared = frontmatter(path).get("role")
            if declared not in RANK:
                continue
            steps, _ = self.steps_and_tail(path)
            needed = (max((self.roles[t] for t in steps), key=lambda r: RANK[r])
                      if steps else "viewer")
            with self.subTest(name):
                self.assertEqual(declared, needed,
                                 f"skills/{name}/SKILL.md declares {declared!r}; its numbered "
                                 f"steps need {needed!r}")

    def test_a_higher_role_tool_outside_the_steps_is_declared_separately(self):
        """The deploy-review shape: five readable steps and an optional tail that is not.
        The reader has to be able to see both facts, and the router reads the keys."""
        for name, path in self.skills.items():
            declared = frontmatter(path).get("role")
            if declared not in RANK:
                continue
            _, tail = self.steps_and_tail(path)
            higher = sorted({t for t in tail if RANK[self.roles[t]] > RANK[declared]})
            listed = frontmatter(path).get("also_uses") or []
            with self.subTest(name):
                self.assertEqual(higher, sorted(listed),
                                 f"skills/{name}/SKILL.md calls {higher} outside its numbered "
                                 f"steps and lists {sorted(listed)} in also_uses")

    def test_anything_listed_in_also_uses_is_a_real_tool_above_the_declared_role(self):
        for name, path in self.skills.items():
            declared = frontmatter(path).get("role")
            for tool in frontmatter(path).get("also_uses") or []:
                with self.subTest(f"{name}:{tool}"):
                    self.assertIn(tool, self.roles, f"{tool} is not a registered tool")
                    self.assertGreater(RANK[self.roles[tool]], RANK[declared],
                                       f"{tool} is not above {declared}; it belongs in the walk")

    def test_a_skill_whose_reader_needs_more_than_a_viewer_says_so_in_its_description(self):
        for name, path in self.skills.items():
            fm = frontmatter(path)
            declared, extra = fm.get("role"), fm.get("also_uses") or []
            if declared in (None, "viewer") and not extra:
                continue
            description = fm.get("description") or ""
            wanted = declared if declared != "viewer" else self.roles[sorted(extra)[0]]
            with self.subTest(name):
                self.assertRegex(description, rf"\b{wanted}\b",
                                 f"skills/{name}/SKILL.md needs a {wanted} token somewhere and "
                                 f"its description does not say so")


class TheDeskPathRuns(unittest.TestCase):
    """The convention is checked by running it, not by comparing spellings. The first
    contract confirmed that every flag shown exists, which was true, while the invocation
    as a whole could not get a request away."""

    @classmethod
    def setUpClass(cls):
        cls.text = skill_files()[ROUTER].read_text()

    INVOCATION = re.compile(r"orbit (?:call|login|tools)\b[^\n`]*")

    @staticmethod
    def run_client(argv: list[str], home: pathlib.Path):
        env = dict(os.environ, HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"))
        env.pop("ORBIT_URL", None)
        return subprocess.run([_interpreter(), "-c",
                               "import sys;from orbitd import client;sys.exit(client.main())",
                               *argv], cwd=ORBITD, capture_output=True, text=True, env=env)

    def test_the_router_shows_a_way_in_for_an_agent_holding_only_the_client(self):
        self.assertTrue(re.search(r"orbit call\b", self.text),
                        f"skills/{ROUTER}/SKILL.md never shows how to call a tool")

    def test_calling_without_the_step_the_router_must_also_show_fails(self):
        """Anti-vacuity for the test below: prove on this machine that a bare call with no
        prior session really is refused, so requiring the router to show that step is a
        requirement about the client and not an opinion."""
        with tempfile.TemporaryDirectory() as home:
            proc = self.run_client(
                ["call", "orbit_project_list", "--url", "https://mcp.fixture.invalid/mcp",
                 "--json", "{}"], pathlib.Path(home))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("login", (proc.stdout + proc.stderr).lower(),
                      "a call with no session failed for some other reason; this check no "
                      "longer proves what it claims")

    def test_the_router_shows_the_step_that_makes_a_call_possible(self):
        self.assertTrue(re.search(r"orbit login\b", self.text),
                        f"skills/{ROUTER}/SKILL.md shows how to call a tool but not the login "
                        f"that has to happen first; an agent following it gets a refusal "
                        f"before any request leaves the machine")

    def test_the_router_says_how_a_change_is_confirmed_without_a_terminal(self):
        """A tool that changes something asks for confirmation, and an agent has no
        terminal to answer on. The shipping and rollback walks the router points at are
        unreachable without this."""
        self.assertTrue(re.search(r"--yes\b", self.text),
                        f"skills/{ROUTER}/SKILL.md shows no way to answer the confirmation a "
                        f"changing tool asks for")

    ADDRESS = re.compile(r"https?://[^\s`\"'<>)]+")

    def test_the_router_shows_a_concrete_address_and_not_only_a_placeholder(self):
        """A reader substitutes a placeholder with the address they were handed, which is
        the host the surface answers on, and that address does not work. An example they
        can pattern-match against is the difference between signing in and a refusal that
        explains nothing."""
        self.assertTrue(self.ADDRESS.findall(self.text),
                        f"skills/{ROUTER}/SKILL.md shows how to sign in without ever showing "
                        f"what an address looks like")

    def test_every_address_the_router_shows_is_one_the_client_leaves_alone(self):
        """The check is the client's own canonicaliser, not a spelling rule: an address it
        rewrites is an address the server will refuse, because the server issues tokens
        only for its exact interface address."""
        for address in self.ADDRESS.findall(self.text):
            with self.subTest(address):
                self.assertEqual(canonical_address(address), address,
                                 f"the client rewrites this address before sending it, so "
                                 f"the server refuses it")

    def test_the_canonicaliser_really_does_rewrite_a_bare_host(self):
        """Anti-vacuity, and the defect written down: if this stopped holding, the rule
        above would pass on the address that is actually broken."""
        bare = "https://mcp.fixture.invalid"
        self.assertNotEqual(canonical_address(bare), bare)
        self.assertEqual(canonical_address(bare + "/mcp"), bare + "/mcp")

    def test_every_flag_the_router_shows_is_one_the_client_accepts(self):
        flags = client_flags()
        for invocation in self.INVOCATION.findall(self.text):
            for flag in re.findall(r"--[a-z][a-z0-9-]*", invocation):
                with self.subTest(invocation.strip()):
                    self.assertIn(flag, flags,
                                  f"skills/{ROUTER}/SKILL.md shows {flag}, which the client "
                                  f"does not accept")


DUMP_CANONICAL = r"""
import sys
from orbitd.client import canonical_mcp_url
print(canonical_mcp_url(sys.argv[1]))
"""


def canonical_address(address: str) -> str:
    """What the client actually sends, asked of the client. The server compares this
    against its own interface address, so any difference is a refusal."""
    proc = subprocess.run([_interpreter(), "-c", DUMP_CANONICAL, address], cwd=ORBITD,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError("could not canonicalise an address:\n" + proc.stderr)
    return proc.stdout.strip()


DUMP_FLAGS = r"""
from orbitd import client
parser = client._parser()
seen = set()
def walk(p):
    for action in p._actions:
        for option in action.option_strings:
            seen.add(option)
        for sub in (getattr(action, "choices", None) or {}).values():
            walk(sub)
walk(parser)
print("\n".join(sorted(seen)))
"""


def client_flags() -> set[str]:
    proc = subprocess.run([_interpreter(), "-c", DUMP_FLAGS], cwd=ORBITD,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError("could not read the desk client parser:\n" + proc.stderr)
    flags = {line.strip() for line in proc.stdout.split() if line.startswith("--")}
    if not flags:
        raise RuntimeError("the client advertised no flags; the check would be vacuous")
    return flags



# --- rule 5: a row that says nothing does this must say what does -----------------
# The router's capability table carries rows whose second cell says a capability has no
# tool behind it. Such a row is a claim about the whole product, and it goes stale in
# silence: the row saying nothing rotates a credential was written before the desk grew
# the verb that does, and nothing would have caught it. Inferring from the row's English
# which tool or verb would contradict it does not work -- "add a service" shares its
# nouns with the catalog readers and means something else entirely. So the rule is about
# the row's shape, not its meaning: a row may say a capability has no tool only if it
# also says where the capability does live. A row that names a desk command is then held
# to it -- the desk must really have that command.

ABSENCE = re.compile(r"\bno tool\b|\bnothing (?:does|behind)\b|\bnot? tool behind\b", re.I)
DESK_COMMAND = re.compile(r"`orbit ([a-z][a-z0-9 -]*?)`")
TABLE_ROW = re.compile(r"^\|(?P<subject>[^|]+)\|(?P<answer>[^|]+)\|\s*$", re.M)

DUMP_VERBS = """
import sys
sys.path.insert(0, ".")
from orbitd import client
paths = set()
def walk(p, prefix):
    for action in p._actions:
        for name, sub in (getattr(action, "choices", None) or {}).items():
            paths.add((prefix + " " + name).strip())
            walk(sub, prefix + " " + name)
walk(client._parser(), "")
print("\\n".join(sorted(paths)))
"""


def desk_commands() -> set[str]:
    proc = subprocess.run([_interpreter(), "-c", DUMP_VERBS], cwd=ORBITD,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError("could not read the desk client parser:\n" + proc.stderr)
    commands = {line.strip() for line in proc.stdout.split("\n") if line.strip()}
    if not commands:
        raise RuntimeError("the desk advertised no commands; the check would be vacuous")
    return commands


class AnAbsenceClaimSaysWhereTheThingLives(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.commands = desk_commands()

    def rows(self):
        for path in sorted(skill_files().values()):
            text = path.read_text()
            for match in TABLE_ROW.finditer(text):
                subject = match.group("subject").strip()
                answer = match.group("answer").strip()
                if set(subject) <= set("-: ") or not ABSENCE.search(answer):
                    continue
                yield path.relative_to(ROOT), subject, answer

    def test_the_table_really_does_carry_rows_that_claim_an_absence(self):
        """Anti-vacuity: if the rule ever stops finding rows, it is proving nothing."""
        found = list(self.rows())
        self.assertTrue(found, "no row in any skill claims a capability has no tool; "
                               "either the table changed shape or this check is dead")

    def test_no_row_claims_an_absence_without_saying_where_the_thing_lives(self):
        bare = []
        for path, subject, answer in self.rows():
            rest = ABSENCE.sub(" ", answer)
            rest = re.sub(r"\b(behind it|yet|for (it|this)|at all|here)\b", " ", rest, flags=re.I)
            if not re.search(r"[a-z]{3}", rest, re.I):
                bare.append(f"{path}: '{subject}' answers '{answer}' and names nothing that does it")
        self.assertEqual([], bare, "\n".join([
            "A row may say a capability has no tool only if it also says what does:",
            "a desk command in backticks, or the repository a person edits by hand.",
            "A bare 'no tool behind it yet' is the row that goes stale in silence.",
            *bare]))

    def test_a_row_that_names_a_desk_command_names_one_the_desk_has(self):
        wrong = []
        for path, subject, answer in self.rows():
            for command in DESK_COMMAND.findall(answer):
                if command.strip() not in self.commands:
                    wrong.append(f"{path}: '{subject}' points at `orbit {command}`, "
                                 f"which the desk does not offer")
        self.assertEqual([], wrong, "\n".join(wrong))


class ANameASkillGivesAReaderIsANameTheTreeHas(unittest.TestCase):
    """The fourth rule, and the one the last release nearly shipped without.

    Refusals moved from being an HTTP status to being a result a caller reads, and three
    skills went on describing the status. Nothing noticed, because the contracts here hold
    what a reader may DO -- the tools, the roles, the desk commands -- and say nothing about
    the constants a skill hands a reader to match on.

    An agent at the acceptance walk has these documents and nothing else. A skill that tells
    it to look for a word the surface never produces fails later and in front of an operator,
    which is worse than failing here.

    The rule is not a list of refusals. It is that a constant a skill names must be one this
    tree has: a refusal code the surface can produce, a variable the deployment sets, a
    status a tool returns. Renaming or removing any of them turns this red, which is what a
    list of the ones I happened to see would not do."""

    TREE = ("services", "scripts", "charts", "template", "tofu")
    # Both spellings a skill uses: a bare constant, and one introduced as a code.
    # Watching only the first is how the first draft of this passed while a skill
    # named a refusal the surface has never produced.
    CONSTANT = re.compile(r"`(?:code: )?([A-Z][A-Z_]{2,})`")

    @classmethod
    def setUpClass(cls):
        cls.blob = []
        for name in cls.TREE:
            here = ROOT / name
            if not here.exists():
                continue
            for f in here.rglob("*"):
                if not f.is_file() or f.suffix not in (".py", ".sh", ".yaml", ".yml",
                                                       ".json", ".toml"):
                    continue
                # Shipped code only. Reading the tests too would check the skills against
                # the checks rather than against the surface: renaming a refusal in the
                # server left this green, because the cases still carried the old name.
                if "tests" in f.parts or f.name.startswith("test_"):
                    continue
                try:
                    cls.blob.append(f.read_text(errors="ignore"))
                except OSError:
                    pass
        cls.blob = "\n".join(cls.blob)

    def named(self):
        for path in sorted(ROOT.glob("skills/*/SKILL.md")):
            for token in self.CONSTANT.findall(path.read_text()):
                yield path.relative_to(ROOT), token

    def test_every_constant_a_skill_names_exists(self):
        missing = [f"{path}: names `{token}`, which this tree does not have"
                   for path, token in self.named() if token not in self.blob]
        self.assertEqual([], missing, "\n".join([
            "A skill hands a reader a word to match on, and the word is not in the tree.",
            "An agent holding only these documents will wait for something that never comes.",
            *missing]))

    @staticmethod
    def _codes_the_surface_produces():
        """The refusal codes, read from the call sites that build them rather than from
        the text of the tree. A comment naming an old code is still text; it is not a
        refusal anyone will ever receive, and matching it is how the first draft of this
        stayed green while the server renamed one."""
        import ast

        codes = set()
        for f in (ROOT / "services/orbitd/orbitd").rglob("*.py"):
            for node in ast.walk(ast.parse(f.read_text())):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id == "failure" and node.args
                        and isinstance(node.args[0], ast.Constant)
                        and isinstance(node.args[0].value, str)):
                    codes.add(node.args[0].value)
        return codes

    def test_every_refusal_a_skill_names_is_one_the_surface_produces(self):
        """A skill tells an agent which word to match on when a call is refused. If the
        surface stops producing that word, the agent waits for something that never comes.

        Which tokens count as refusals is taken from the skills themselves: any constant
        one of them introduces as a code. Every mention of those, bare or not, is then held
        to the set the surface can actually build."""
        introduced = set()
        for path in sorted(ROOT.glob("skills/*/SKILL.md")):
            introduced |= set(re.findall(r"`code: ([A-Z][A-Z_]{2,})`", path.read_text()))
        self.assertTrue(introduced, "no skill names a refusal code, so this holds nothing")
        produced = self._codes_the_surface_produces()
        wrong = [f"{path}: names the refusal `{token}`, which this surface never produces"
                 for path, token in self.named() if token in introduced
                 and token not in produced]
        self.assertEqual([], wrong, "\n".join([
            "A skill tells a reader to match on a refusal the surface does not produce.",
            f"What it does produce: {sorted(produced)}",
            *wrong]))

    def test_the_scan_finds_the_constants_that_are_there(self):
        """Anti-vacuity: a pattern that matches nothing passes for ever."""
        found = {token for _, token in self.named()}
        self.assertIn("FORBIDDEN", found,
                      "the scan no longer sees the refusal every skill cites, so it is "
                      "looking for a shape the skills no longer use")


if __name__ == "__main__":
    unittest.main(verbosity=2)
