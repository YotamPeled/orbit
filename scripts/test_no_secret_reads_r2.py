#!/usr/bin/env python3
"""
The no-secret-reads oracle, judged on whole commands instead of on lines.

WHY THIS ROUND EXISTS. The oracle has now been walked past twice, and both holes were the
same mistake: it was written against the TEXT of a file rather than against the COMMANDS in
it. Round one, a variable carried the Secret's name to a second line and the read on that
line looked innocent. Round two, a single kubectl invocation was split across two lines with
a trailing backslash, and each half looked innocent on its own — in exactly the continuation
style the recovery drill already writes. Two rounds, one class, so the rule says the design is
wrong rather than the patch.

So the oracle stops reading lines. It normalises a file into a stream of whole commands first
— continuations joined, so a command is one string no matter how it is laid out — and only
then asks its questions of each command. The taint-following from round one survives; it just
operates on the normalised stream.

WHAT COUNTS AS A READ. Selecting a Secret object and rendering anything that carries its
`data` block. The rendering forms are open-ended by design: whole-document output, a path
expression naming `data`, a column selector naming `data`, a template expression indexing
`data`. A question about the caller's own permissions names no object and reads nothing. An
existence check that renders no data is what the board rule explicitly allows.

Run as `python3 scripts/test_no_secret_reads_r2.py` or under pytest.
"""
import importlib.util
import pathlib
import shutil
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
ORACLE = HERE / "test_no_secret_reads.py"


def _oracle():
    """The oracle under test, loaded as a module so its own judgement can be driven over
    fixtures rather than only over the tree it happens to ship with."""
    spec = importlib.util.spec_from_file_location("no_secret_reads_oracle", ORACLE)
    mod = importlib.util.module_from_spec(spec)
    saved, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.dont_write_bytecode = saved
    return mod


# ---------------------------------------------------------------- fixtures

# Each is a complete shell fragment. CAUGHT must be reported; ALLOWED must not.
CAUGHT = {
    "a plain whole-document read":
        "kubectl -n monitoring get secret grafana-admin -o yaml\n",
    "a path expression naming the data block":
        "kubectl -n widgets-prod get secret orbit-s3 -o jsonpath='{.data.TOKEN}' | base64 -d\n",
    "the same invocation split across lines, the drill's own style":
        'kubectl -n "$NS" get secret orbit-s3 \\\n'
        "  -o jsonpath='{.data.HETZNER_S3_SECRET_KEY}' | base64 -d\n",
    "a continuation that also carries the selector onto a third line":
        'kubectl -n "$NS" \\\n'
        "  get secret orbit-s3 \\\n"
        "  -o json\n",
    "a column selector naming the data block":
        "kubectl -n monitoring get secret grafana-admin "
        "-o custom-columns=V:.data.admin-password\n",
    "a template expression indexing the data block":
        "kubectl -n orbit get secret orbitd-github-app "
        "-o template --template='{{index .data \"key\"}}'\n",
    "a go template indexing the data block":
        "kubectl -n orbit get secret orbitd-registry "
        "-o go-template='{{index .data \".dockerconfigjson\"}}'\n",
    "the long output flag":
        "kubectl -n monitoring get secret grafana-admin --output=json\n",
    "a name carried by a variable to a later read":
        'AMSECRET=$(kubectl -n monitoring get secret -o name | head -1)\n'
        'kubectl -n monitoring get "$AMSECRET" -o json\n',
    "a name carried by a variable, read with braces, across a continuation":
        'S=$(kubectl -n monitoring get secret -o name | head -1)\n'
        'kubectl -n monitoring get "${S}" \\\n'
        "  -o jsonpath='{.data}'\n",
}

ALLOWED = {
    "an existence check that renders no data":
        "kubectl -n monitoring get secret grafana-admin\n",
    "an existence check rendering only the name":
        "kubectl -n monitoring get secret grafana-admin -o name\n",
    "a question about the caller's own permissions":
        "kubectl auth can-i get secrets -n monitoring\n",
    "a whole-document read of something that is not a Secret":
        "kubectl -n monitoring get configmap grafana-dashboards -o yaml\n",
    "a Cluster's own spec, which is how the drill finds the database name":
        "kubectl -n widgets-prod get cluster widgets-db "
        "-o jsonpath='{.spec.bootstrap.initdb.database}'\n",
    "a continuation that never reaches a Secret":
        'kubectl -n "$NS" get deploy orbitd \\\n'
        "  -o jsonpath='{.spec.template.spec.containers[0].image}'\n",
    "an ownership reference on a Secret, which is metadata":
        "kubectl -n monitoring get secret grafana-admin "
        "-o jsonpath='{.metadata.ownerReferences}'\n",
}


class _FixtureCase(unittest.TestCase):
    def setUp(self):
        self.oracle = _oracle()
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def judge(self, body: str, name: str = "fixture.sh"):
        path = self.tmp / name
        path.write_text("#!/bin/bash\n" + body)
        found = self.oracle.offences(path)
        return [str(item) for item in found]


class CaughtTest(_FixtureCase):
    def test_every_shape_that_opens_a_secret_is_reported(self):
        for label, body in sorted(CAUGHT.items()):
            with self.subTest(label):
                self.assertNotEqual(
                    self.judge(body), [],
                    "this walks past the oracle — %s:\n%s" % (label, body))


class AllowedTest(_FixtureCase):
    def test_nothing_harmless_is_reported(self):
        """An oracle that refuses existence checks is an oracle people switch off. The
        board rule allows checking that a Secret exists; it forbids selecting its data."""
        for label, body in sorted(ALLOWED.items()):
            with self.subTest(label):
                self.assertEqual(
                    self.judge(body), [],
                    "this is allowed and the oracle reported it — %s:\n%s" % (label, body))


class NormalisationTest(_FixtureCase):
    def test_a_command_is_judged_the_same_however_it_is_laid_out(self):
        """The property the redesign exists for, stated directly: line breaks inside one
        command carry no meaning, so wrapping a command must never change the verdict."""
        one_line = ("kubectl -n monitoring get secret grafana-admin "
                    "-o jsonpath='{.data.admin-password}'\n")
        wrapped = ("kubectl -n monitoring \\\n  get secret grafana-admin \\\n"
                   "  -o jsonpath='{.data.admin-password}'\n")
        flat, folded = self.judge(one_line), self.judge(wrapped, "wrapped.sh")
        self.assertNotEqual(flat, [], "the flat form is not caught; fix that first")
        self.assertNotEqual(folded, [],
                            "the same command, wrapped, walks past — the oracle is still "
                            "judging lines rather than commands")

    def test_a_continuation_does_not_merge_two_unrelated_commands(self):
        """The other direction: joining must follow the shell's rule, not glue every line
        to the next, or a harmless line inherits the previous line's verdict."""
        body = ("kubectl -n monitoring get secret grafana-admin\n"
                "kubectl -n monitoring get configmap dash -o yaml\n")
        self.assertEqual(self.judge(body), [],
                         "two separate harmless commands were judged as one")


class WholeTreeTest(unittest.TestCase):
    def test_the_platform_ships_no_command_that_opens_a_secret(self):
        oracle = _oracle()
        hits = {}
        for path in oracle.scanned_files():
            rel = str(path.relative_to(ROOT))
            if rel in oracle.EXEMPT:
                continue
            found = oracle.offences(path)
            if found:
                hits[rel] = [str(item) for item in found][:3]
        self.assertEqual(hits, {}, "commands that open a Secret: %s"
                                   % "; ".join("%s %s" % (k, v) for k, v in hits.items()))

    def test_the_exemption_list_is_only_contracts(self):
        """An operational file on the exemption list is a hole with a note attached."""
        oracle = _oracle()
        operational = sorted(rel for rel in oracle.EXEMPT
                             if not pathlib.PurePosixPath(rel).name.startswith("test_"))
        self.assertEqual(operational, [], "these are exempt and are not contracts: %s"
                                          % ", ".join(operational))

    def test_this_contract_is_exempt_because_it_quotes_the_forbidden_shapes(self):
        oracle = _oracle()
        self.assertIn("scripts/test_no_secret_reads_r2.py", oracle.EXEMPT,
                      "this file's fixtures ARE the forbidden shapes; without an exemption "
                      "the whole-tree test reports this file and nobody can tell a real "
                      "offence from the contract that proves the oracle works")


if __name__ == "__main__":
    unittest.main()
