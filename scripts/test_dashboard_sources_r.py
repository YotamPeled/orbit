#!/usr/bin/env python3
"""
The generated dashboards, round 1 repairs — run as
`python3 scripts/test_dashboard_sources_r.py`. No cluster, no network.

WHY. The commit that rebuilt every RED dashboard on Traefik and on span-metrics passed
its own contract and was then reviewed against the live instance. Eighteen of fifty-four
generated panels returned nothing, for three reasons, and my own contract had not caught
any of them because it asked whether a component's NAME appeared somewhere in the query
rather than whether the query could match what the producer actually writes.

  * The worker panels select `service="<component>"` and `namespace="<project>-<env>"`.
    The chart publishes `OTEL_SERVICE_NAME=<project>-<component>`, and Tempo's
    span-metrics generator runs default dimensions only, so there is no namespace label
    at all. Both halves of the selector are wrong, so every worker panel is empty for
    ever — and the new panel checker reports that as `no_data`, which is exactly the
    word it uses for a worker nobody has instrumented yet. The defect hides behind the
    tool built to find it.
  * Nothing in the span-metrics label set distinguishes one environment from another.
    Correcting the service value alone makes the dev and prod worker panels draw the
    same series, which is worse than empty: it is wrong and it looks right.
  * A panel whose selector pins a value that exists only when something is WRONG — a
    5xx response code, an OOMKilled termination reason — returns an empty vector on a
    healthy system, and Grafana renders that as "No data". A stat panel reading "No
    data" is indistinguishable from a broken query, which is the whole distinction this
    phase is built on.

THE RULE THIS CONTRACT ADOPTS, and the reason the tests below read the producers rather
than the queries: a generated selector is checked against the thing that PRODUCES the
label — the chart's environment variable for the service value, Tempo's configured
dimension list for every other label. A test that only greps for a substring passes on
a selector that can never match, and that is how eighteen empty panels shipped green.
"""
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
_BYTECODE, sys.dont_write_bytecode = sys.dont_write_bytecode, True
from test_catalog_quota import ROOT, Sandbox  # noqa: E402
from test_dashboard_sources import SPANS, TRAEFIK, dashboards, exprs  # noqa: E402
sys.dont_write_bytecode = _BYTECODE

CHART = ROOT / "charts" / "service"
HELM = shutil.which("helm")
TEMPO = (ROOT / "template" / "provisioning" / "clusters" / "platform"
         / "apps" / "tempo.yaml")
SKILLS = ROOT / ".claude" / "skills"
# The skills that ship with the tools; test_skills.py pairs every registered
# tool with a name in THIS tree, not in the operator-facing one above.
AGENT_SKILLS = ROOT / "skills"

# What a span-metrics series carries with no dimensions configured. Tempo writes these
# for every span it sees; anything else has to be asked for by name.
INTRINSIC = {"service", "span_name", "span_kind", "status_code", "job", "instance",
             "__name__", "le"}

# A selector value that only appears when something has gone wrong. A panel pinning one
# of these matches nothing on a healthy system.
ABSENCE_PRONE = ('code=~"5..', 'reason="OOMKilled"')


def label_selectors(expr: str, metric: re.Pattern) -> list[dict]:
    """{label: value} for every selector in `expr` attached to a matching metric."""
    out = []
    for m in re.finditer(r"(\w+)\{([^}]*)\}", expr):
        if not metric.fullmatch(m.group(1)):
            continue
        labels = {}
        for part in re.finditer(r'(\w+)\s*(=~|!~|!=|=)\s*"([^"]*)"', m.group(2)):
            labels[part.group(1)] = part.group(3)
        out.append(labels)
    return out


def tempo_overrides() -> dict:
    """Tempo's Argo application, with the instance tokens blanked so it parses."""
    text = re.sub(r"\{\{[^}]*\}\}", "TOKEN", TEMPO.read_text())
    for doc in yaml.safe_load_all(text):
        if not doc:
            continue
        found = _find_key(doc, "metrics_generator")
        if found is not None:
            return found
    raise AssertionError("tempo.yaml declares no metrics_generator overrides")


def _find_key(node, key):
    if isinstance(node, dict):
        if key in node:
            return node[key]
        for value in node.values():
            hit = _find_key(value, key)
            if hit is not None:
                return hit
    elif isinstance(node, list):
        for value in node:
            hit = _find_key(value, key)
            if hit is not None:
                return hit
    return None


def configured_dimensions() -> list[str]:
    gen = tempo_overrides()
    proc = ((gen.get("processor") or {}).get("span_metrics") or {})
    return list(proc.get("dimensions") or [])


def as_label(dimension: str) -> str:
    """The Prometheus label Tempo writes for a configured dimension: the attribute name
    with every character a label may not carry replaced by an underscore."""
    return re.sub(r"[^a-zA-Z0-9_]", "_", dimension)


class _Generated(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.box = Sandbox()
        proc = cls.box.generate()
        if proc.returncode != 0:
            cls.box.close()
            raise AssertionError("catalog-sync failed:\n" + proc.stdout + proc.stderr)
        cls.dash = dashboards(cls.box)

    @classmethod
    def tearDownClass(cls):
        cls.box.close()

    def span_selectors(self, pid, env):
        out = []
        for e in exprs(self.dash[(pid, env)]):
            out.extend(label_selectors(e, SPANS))
        return out


class WorkerSelectorTest(_Generated):
    """A worker panel's selector is checked against what writes the labels."""

    @unittest.skipUnless(HELM, "helm is not on PATH")
    def test_the_service_value_is_the_name_the_chart_publishes(self):
        """Tempo keys span-metrics on the span's service.name, which the chart sets from
        OTEL_SERVICE_NAME. Anything else matches nothing, for ever."""
        published = self.otel_service_name("widgets", "importer")
        selected = {s.get("service") for s in self.span_selectors("widgets", "prod")}
        self.assertEqual(selected, {published},
                         "the worker panels select "
                         f"{sorted(x for x in selected if x)} while the chart publishes "
                         f"{published!r} — the panel can never match")

    def otel_service_name(self, project, component):
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="orbit-j26r-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "values.yaml").write_text(yaml.safe_dump({
            "project": project, "env": "prod",
            "components": [{"name": component, "kind": "worker",
                            "image": "example/x"}]}))
        proc = subprocess.run([HELM, "template", "t", str(CHART),
                               "-f", str(tmp / "values.yaml")],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        for doc in yaml.safe_load_all(proc.stdout):
            if not doc or doc.get("kind") not in ("Deployment", "StatefulSet"):
                continue
            for container in doc["spec"]["template"]["spec"]["containers"]:
                for var in container.get("env") or []:
                    if var["name"] == "OTEL_SERVICE_NAME":
                        return var["value"]
        raise AssertionError("the chart sets no OTEL_SERVICE_NAME")

    def test_every_label_a_worker_panel_selects_actually_exists(self):
        """THE defect my first contract missed. `namespace=` is not a span-metrics
        label: Tempo runs default dimensions only unless one is configured, so a
        selector naming it matches nothing and reports as a quiet worker."""
        allowed = INTRINSIC | {as_label(d) for d in configured_dimensions()}
        bad = []
        for (pid, env), dash in self.dash.items():
            for e in exprs(dash):
                for sel in label_selectors(e, SPANS):
                    bad += [f"{pid}/{env}: {k}" for k in sel if k not in allowed]
        self.assertEqual(bad, [],
                         "a worker panel selects a label span-metrics does not write. "
                         "Tempo writes " + ", ".join(sorted(allowed)) + ":\n"
                         + "\n".join(sorted(set(bad))))


class EnvironmentTest(_Generated):
    """Two environments of one project must not draw the same series."""

    def test_the_dev_and_prod_worker_panels_select_differently(self):
        dev = self.span_selectors("widgets", "dev")
        prod = self.span_selectors("widgets", "prod")
        self.assertTrue(dev and prod, "one environment has no worker panel at all")
        self.assertNotEqual(dev, prod,
                            "the dev and prod worker panels carry identical selectors, "
                            "so both dashboards draw both environments' traffic and "
                            "each reads as if it were the other's")

    def test_the_environment_label_is_a_dimension_tempo_is_configured_to_write(self):
        dims = {as_label(d) for d in configured_dimensions()}
        sel = self.span_selectors("widgets", "prod")
        separating = {k for s in sel for k, v in s.items()
                      if k != "service" and "prod" in v}
        self.assertTrue(separating,
                        "no label in the worker panels carries the environment")
        self.assertTrue(separating <= dims,
                        f"the panels separate environments with {sorted(separating)}, "
                        f"which Tempo is not configured to write (dimensions: "
                        f"{sorted(dims) or 'none'}) — configuring the dashboard without "
                        "configuring the generator produces an empty panel")

    def test_the_dimension_is_an_attribute_the_chart_actually_sets(self):
        """A dimension Tempo is told to write is only written if the span carries that
        attribute. The chart is where every workload's resource attributes come from."""
        helper = (CHART / "templates" / "_helpers.tpl").read_text()
        missing = [d for d in configured_dimensions() if d not in helper]
        self.assertEqual(missing, [],
                         "Tempo is configured to write " + ", ".join(missing)
                         + ", which no workload sets — the label will be empty on "
                           "every series")


class HealthyIsNotBlankTest(_Generated):
    """A panel that pins a failure value must read zero, not "No data"."""

    def offenders(self):
        out = []
        for (pid, env), dash in self.dash.items():
            for panel in dash["panels"]:
                for target in panel.get("targets") or []:
                    e = target["expr"]
                    if not any(token in e for token in ABSENCE_PRONE):
                        continue
                    if re.search(r"\bor\b[^|]*vector\(0\)", e):
                        continue
                    out.append(f"{pid}/{env} · {panel['title']}: {e[:150]}")
        return out

    def test_a_panel_pinned_to_a_failure_value_falls_back_to_zero(self):
        bad = self.offenders()
        self.assertEqual(bad, [],
                         "these panels are empty on a healthy system, and Grafana draws "
                         "an empty result the same way it draws a broken query:\n"
                         + "\n".join(bad))

    def test_the_errors_stat_still_divides_by_a_guarded_denominator(self):
        """The zero fallback is added to the numerator; the clamp on the denominator
        that stops a quiet service reading NaN must survive it."""
        dash = self.dash[("widgets", "prod")]
        stats = [p for p in dash["panels"] if "errors %" in p["title"].lower()]
        self.assertTrue(stats, "the Errors % stat is gone")
        for panel in stats:
            for target in panel["targets"]:
                self.assertIn("clamp_min", target["expr"],
                              "the denominator guard was dropped: a service with no "
                              "traffic reads NaN")


class NoRouteTest(unittest.TestCase):
    """A web component with no public route has no Traefik service, so it must not be
    given Traefik panels — the agent plane is exactly this shape."""

    def build(self):
        box = Sandbox()
        self.addCleanup(box.close)
        cat = box.root / "catalog" / "projects.yaml"
        doc = yaml.safe_load(cat.read_text())
        doc["projects"] = [{
            "id": "inner", "name": "Inner", "hosted": "self",
            "environments": {"prod": {"hostnames": []}},
            "components": [{"name": "svc", "kind": "web", "image": "example/svc",
                            "port": 8080}],
        }]
        cat.write_text(yaml.safe_dump(doc, sort_keys=False))
        proc = box.generate()
        self.assertEqual(proc.returncode, 0, (proc.stdout + proc.stderr)[-2000:])
        return dashboards(box)[("inner", "prod")]

    def test_it_gets_no_traefik_panel(self):
        dash = self.build()
        hits = [e for e in exprs(dash) if TRAEFIK.search(e)]
        self.assertEqual(hits, [],
                         "a component with no Ingress was given panels selecting a "
                         "Traefik service label Traefik will never create:\n"
                         + "\n".join(hits))

    def test_it_is_not_left_invisible(self):
        """Dropping the panels it cannot fill must not drop the component. It still
        emits spans, and that is where its RED comes from."""
        dash = self.build()
        self.assertTrue([e for e in exprs(dash) if SPANS.search(e)],
                        "the component lost its Traefik panels and got nothing in "
                        "their place, so it is invisible")


class LabelLengthTest(unittest.TestCase):
    """The review raised the chart's `trunc 63` as a possible disagreement between the
    Service the chart renders and the label the generator selects. It cannot happen, and
    this pins WHY rather than deleting the question: a project id is capped at 40
    characters by the generator and a component name at 22 by the chart's own schema, so
    the longest name either side can build is exactly 63 and the truncation never fires.
    Widen either pattern and this goes red, which is the moment the generator would have
    to learn to truncate."""

    PROJECT_ID = re.compile(r"\{0,38\}")   # documented below, not parsed

    def bounds(self):
        gen = (ROOT / "scripts" / "catalog-sync.py").read_text()
        m = re.search(r"ID_RE = re\.compile\(r?\"\[a-z0-9\]\(\[a-z0-9-\]\{0,(\d+)\}",
                      gen)
        self.assertIsNotNone(m, "the generator's project-id pattern moved; re-derive "
                                "the bound before trusting this test")
        project = int(m.group(1)) + 2
        schema = json.loads((CHART / "values.schema.json").read_text())
        pattern = schema["properties"]["components"]["items"]["properties"]["name"]["pattern"]
        c = re.search(r"\{0,(\d+)\}", pattern)
        self.assertIsNotNone(c, "the chart's component-name pattern has no length bound, "
                                "so a name can overflow the Service name")
        return project, int(c.group(1)) + 2

    def test_the_longest_service_name_either_side_can_build_fits(self):
        project, component = self.bounds()
        self.assertLessEqual(
            project + 1 + component, 63,
            f"a project id of {project} and a component name of {component} build a "
            "Service name the chart truncates while the generated Traefik label does "
            "not, so the panel selects a service that does not exist")


class SkillTest(unittest.TestCase):
    """What an application author is told must match what the platform now sees."""

    def text(self):
        """BOTH skill trees. `.claude/skills/` holds the operator-facing skills a human
        reads; `skills/` holds the ones that ship WITH the tools, and the registry check
        in test_skills.py pairs a tool with a skill from that tree alone. Scanning only
        the first is how a tool passed this test while being an orphan to the rule that
        actually enforces the pairing."""
        return "\n".join(p.read_text() for p in sorted(self.paths()))

    @staticmethod
    def paths():
        return list(SKILLS.rglob("SKILL.md")) + list(AGENT_SKILLS.rglob("SKILL.md"))

    def test_no_skill_still_defers_to_the_deleted_floor(self):
        """With the floor gone nothing covers an HTTP or database call unless the
        application instruments it, so telling an author that instrumenting those
        produces duplicates is now advice to stay invisible."""
        offenders = []
        for path in sorted(self.paths()):
            for n, line in enumerate(path.read_text().splitlines(), 1):
                if re.search(r"eBPF|the floor already covers|produce doubles", line, re.I):
                    offenders.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()[:110]}")
        self.assertEqual(offenders, [],
                         "a skill still reasons about the deleted instrumentation "
                         "floor:\n" + "\n".join(offenders))

    def test_a_skill_names_the_panel_checker(self):
        self.assertIn("orbit_dashboards_check", self.text(),
                      "the tool that says which panels are empty is named in no skill, "
                      "so the agent that would run it cannot find it")

    def test_the_panel_checker_is_named_in_the_tree_the_registry_rule_reads(self):
        """test_skills.py requires every registered tool to be named by a skill under
        `skills/` — the tree that ships WITH the tools. A mention in the operator-facing
        tree does not satisfy that rule, and the tool ships as an orphan an agent has to
        guess at. My first contract scanned only the operator tree, which is how this
        passed here while the rule that enforces the pairing was red."""
        shipped = "\n".join(path.read_text()
                            for path in sorted(AGENT_SKILLS.rglob("SKILL.md")))
        self.assertIn("orbit_dashboards_check", shipped,
                      "no skill that ships with the tools names the panel checker")


if __name__ == "__main__":
    unittest.main(verbosity=2)
