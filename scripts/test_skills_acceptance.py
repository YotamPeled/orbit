#!/usr/bin/env python3
"""
scripts/skills-acceptance.sh against a claude double. Run as
`python3 -m pytest scripts/test_skills_acceptance.py -q` from the repo root.

The real run costs money and needs a live orbitd, so what is tested here is the harness
around it: the credential refusal, the session directory (skills copied, nothing else),
the exact command line the session is launched with, and the transcript rendering. The
double refuses what the real `claude -p` refuses that this harness could get wrong (a
missing prompt, a missing --mcp-config, an unknown flag) and answers with a canned
stream-json in the shape the real CLI writes.
"""
import json
import os
import pathlib
import shutil
import stat
import subprocess
import tempfile
import textwrap
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "skills-acceptance.sh"

KNOWN_FLAGS = {"--setting-sources", "--mcp-config", "--model", "--allowedTools", "--output-format"}
KNOWN_SWITCHES = {"-p", "--strict-mcp-config", "--verbose"}
PROXY = ROOT / "scripts" / "mcp-confirm-proxy.py"

DOUBLE = textwrap.dedent(r"""
    #!/usr/bin/env python3
    import json, os, sys
    args = sys.argv[1:]
    got, pos = {}, []
    i = 0
    while i < len(args):
        a = args[i]
        if a in %(switches)r:
            got[a] = True; i += 1
        elif a in %(flags)r:
            got[a] = args[i + 1]; i += 2
        elif a.startswith("-"):
            print("error: unknown option " + a, file=sys.stderr); sys.exit(1)
        else:
            pos.append(a); i += 1
    if len(pos) != 1 or "--mcp-config" not in got or "-p" not in got:
        print("error: bad invocation", file=sys.stderr); sys.exit(1)
    open(os.environ["DOUBLE_LOG"], "a").write(json.dumps({"cwd": os.getcwd(), "args": args}) + "\n")
    ev = [
      {"type": "system", "subtype": "init", "model": got["--model"], "tools": ["Skill", "mcp__orbit__orbit_project_list"],
       "mcp_servers": [{"name": "orbit", "status": "connected"}], "skills": ["orbit-operator"]},
      {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Skill", "input": {"skill": "orbit-operator"}}]}},
      {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "Launching skill: orbit-operator"}]}},
      {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t2", "name": "mcp__orbit__orbit_project_list", "input": {}}]}},
      {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t2",
        "content": [{"type": "text", "text": "\n".join("line %%d" %% k for k in range(60))}]}]}},
      {"type": "assistant", "message": {"content": [{"type": "text", "text": "One project."}]}},
      {"type": "result", "subtype": "success", "is_error": False, "num_turns": 3, "duration_ms": 10,
       "total_cost_usd": 0.5, "permission_denials": [], "result": "FINAL: one project."},
    ]
    for e in ev:
        print(json.dumps(e))
""").lstrip() % {"switches": sorted(KNOWN_SWITCHES), "flags": sorted(KNOWN_FLAGS)}


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        bin_ = self.tmp / "bin"
        bin_.mkdir()
        (bin_ / "claude").write_text(DOUBLE)
        (bin_ / "claude").chmod(0o755)
        self.log = self.tmp / "double.log"
        self.cfg = self.tmp / "mcp.json"
        self.cfg.write_text(json.dumps({"mcpServers": {"orbit": {"type": "http", "url": "https://mcp.fixture.invalid/mcp",
                                                                 "headers": {"Authorization": "Bearer fixture-secret"}}}}))
        self.cfg.chmod(0o600)
        self.skills = self.tmp / "skills"
        for name in ("orbit-operator", "orbit-diagnose"):
            (self.skills / name).mkdir(parents=True)
            (self.skills / name / "SKILL.md").write_text(f"---\nname: {name}\ndescription: x\n---\nbody\n")
        (self.skills / "notes.txt").write_text("not a skill")
        (self.skills / "empty-dir").mkdir()
        self.questions = self.tmp / "q.tsv"
        self.questions.write_text("explain\tWhat is this?\n\n# a comment\nreview\tWhat runs in a/b?\n")
        self.out = self.tmp / "out"
        self.env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}", "DOUBLE_LOG": str(self.log),
                    "ACCEPTANCE_MODEL": "fixture-model", "ACCEPTANCE_QUESTIONS": str(self.questions)}
        self.env.pop("ACCEPTANCE_ONLY", None)
        self.env.pop("ACCEPTANCE_ROUND", None)

    def run_it(self, *extra_env, cfg=None):
        env = dict(self.env)
        for e in extra_env:
            env.update(e)
        return subprocess.run(["bash", str(SCRIPT), str(cfg or self.cfg), str(self.skills), str(self.out)],
                              env=env, capture_output=True, text=True, cwd=ROOT)

    def test_a_world_readable_config_is_refused_before_anything_runs(self):
        self.cfg.chmod(0o644)
        proc = self.run_it()
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("world-readable", proc.stderr)
        self.assertFalse(self.log.exists(), "the double was launched despite the refusal")
        self.assertFalse((self.out / "session").exists())
        self.assertNotIn("fixture-secret", proc.stdout + proc.stderr)

    def test_the_session_holds_only_the_skills_and_is_launched_from_it(self):
        proc = self.run_it()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        session = self.out / "session"
        self.assertEqual(stat.S_IMODE(session.stat().st_mode), 0o700)
        self.assertEqual(sorted(p.name for p in session.iterdir()), [".claude"])
        self.assertEqual(sorted(p.name for p in (session / ".claude" / "skills").iterdir()),
                         ["orbit-diagnose", "orbit-operator"], "the copy took something that is not a skill")
        self.assertFalse((session / ".claude" / "skills" / "orbit-operator").is_symlink(), "copied, not linked")
        runs = [json.loads(l) for l in self.log.read_text().splitlines()]
        self.assertEqual(len(runs), 2)
        for r in runs:
            self.assertEqual(r["cwd"], str(session))
            a = r["args"]
            self.assertIn("-p", a)
            self.assertEqual(a[a.index("--mcp-config") + 1], str(self.cfg))
            self.assertIn("--strict-mcp-config", a)
            self.assertEqual(a[a.index("--allowedTools") + 1], "mcp__orbit__*")
            self.assertEqual(a[a.index("--output-format") + 1], "stream-json")
            self.assertIn("--verbose", a)
            self.assertEqual(a[a.index("--model") + 1], "fixture-model")
            self.assertEqual(a[a.index("--setting-sources") + 1], "project")
        self.assertTrue(runs[0]["args"][-1].startswith("What is this? "), runs[0]["args"][-1])
        self.assertIn("one MCP server named orbit", runs[0]["args"][-1])
        self.assertTrue(runs[1]["args"][-1].startswith("What runs in a/b? "))
        # Numbering follows the questions file with blanks and comments skipped.
        self.assertTrue((self.out / "1-explain.jsonl").is_file())
        self.assertTrue((self.out / "2-review.jsonl").is_file())
        self.assertNotIn("fixture-secret", proc.stdout + proc.stderr)

    def test_the_rendering_carries_calls_results_cut_and_the_final_answer(self):
        proc = self.run_it()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        md = (self.out / "1-explain.md").read_text()
        self.assertIn("# What is this?", md)
        self.assertIn("skills invoked: orbit-operator", md)
        self.assertIn("tools in order: mcp__orbit__orbit_project_list", md)
        self.assertIn("## call 2: mcp__orbit__orbit_project_list", md)
        self.assertIn("line 39", md)
        self.assertNotIn("line 40", md, "results are cut to 40 lines")
        self.assertIn("(20 more lines cut)", md)
        self.assertIn("FINAL: one project.", md)
        self.assertIn("cost: 0.5 USD", md)
        self.assertIn("mcp servers: orbit=connected", md)

    def test_only_and_round_select_and_suffix(self):
        proc = self.run_it({"ACCEPTANCE_ONLY": "2", "ACCEPTANCE_ROUND": "r2"})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse((self.out / "1-explain-r2.jsonl").exists())
        self.assertTrue((self.out / "2-review-r2.jsonl").is_file())
        self.assertTrue((self.out / "2-review-r2.md").is_file())
        self.assertEqual(len(self.log.read_text().splitlines()), 1)

    def test_a_malformed_questions_line_is_refused(self):
        self.questions.write_text("no tab here\n")
        proc = self.run_it()
        self.assertEqual(proc.returncode, 2)
        self.assertIn("<slug><TAB><question>", proc.stderr)

    def test_a_double_that_refuses_the_command_line_is_reported_not_hidden(self):
        """The stream is kept and rendered even when claude exits non-zero, and the exit
        is said out loud: a silent zero over an empty transcript would read as a session
        that answered nothing."""
        (self.tmp / "bin" / "claude").write_text("#!/bin/sh\necho 'error: boom' >&2\nexit 1\n")
        proc = self.run_it()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("claude exited 1 on question 1", proc.stderr)
        self.assertIn("error: boom", (self.out / "1-explain.stderr").read_text())
        self.assertIn("_(none)_", (self.out / "1-explain.md").read_text())

    def test_auto_confirm_puts_the_proxy_between_the_session_and_the_server(self):
        """With --auto-confirm the session's --mcp-config is a generated file naming a stdio
        server of the SAME name whose command is the proxy over the real config; the real
        config is read by the proxy only and its secret never reaches stdout or stderr."""
        env = dict(self.env)
        env["ACCEPTANCE_NEVER_CONFIRM"] = "env=prod"
        proc = subprocess.run(["bash", str(SCRIPT), "--auto-confirm", str(self.cfg), str(self.skills), str(self.out)],
                              env=env, capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        runs = [json.loads(l) for l in self.log.read_text().splitlines()]
        self.assertEqual(len(runs), 2)
        for n, r in zip((1, 2), runs):
            a = r["args"]
            generated = pathlib.Path(a[a.index("--mcp-config") + 1])
            self.assertNotEqual(generated, self.cfg, "the session was handed the real config")
            self.assertEqual(generated.parent, self.out / "proxy")
            self.assertEqual(stat.S_IMODE(generated.stat().st_mode), 0o600)
            spec = json.loads(generated.read_text())["mcpServers"]
            self.assertEqual(list(spec), ["orbit"], "the proxy must carry the real server's name")
            self.assertEqual(spec["orbit"]["type"], "stdio")
            args = spec["orbit"]["args"]
            self.assertEqual(args[0], str(PROXY))
            self.assertEqual(args[args.index("--config") + 1], str(self.cfg))
            self.assertEqual(args[args.index("--server") + 1], "orbit")
            self.assertIn("--auto-confirm", args)
            self.assertEqual(args[args.index("--never-confirm") + 1], "env=prod")
            self.assertTrue(args[args.index("--record") + 1].endswith(f"{n}-{'explain' if n == 1 else 'review'}.confirmations.jsonl"))
            self.assertNotIn("fixture-secret", generated.read_text(), "the generated config must not copy the credential")
            # The interpreter the proxy is launched with must carry the SDK: round 1 of the phase-4
            # acceptance died because a venv python resolved through its symlink is the system python.
            probe = subprocess.run([spec["orbit"]["command"], "-c", "import mcp, anyio"], capture_output=True, text=True)
            self.assertEqual(probe.returncode, 0, f"{spec['orbit']['command']} cannot import the SDK: {probe.stderr}")
        self.assertEqual(sorted(p.name for p in (self.out / "session").iterdir()), [".claude"])
        self.assertNotIn("fixture-secret", proc.stdout + proc.stderr)
        self.assertIn("confirmation rounds met by the proxy: none", (self.out / "1-explain.md").read_text())

    def test_the_rendering_counts_the_confirmations_the_proxy_recorded(self):
        """The record file is the evidence that a round was answered on the session's behalf;
        the transcript header must say so, or a headless ship reads like one nobody confirmed."""
        (self.out / "proxy").mkdir(parents=True)
        # A double that writes a record line where the proxy would, then answers as usual.
        double = (self.tmp / "bin" / "claude").read_text().replace(
            'open(os.environ["DOUBLE_LOG"], "a")',
            'open(os.path.join(os.environ["DOUBLE_OUT"], "1-explain.confirmations.jsonl"), "a").write(json.dumps('
            '{"tool": "orbit_deploy_ship", "arguments": {"env": "dev"}, "messages": ["Ship web of demo/dev to tag t1?"], '
            '"answered": True, "answer": {"confirm": True}}) + "\\n") or open(os.environ["DOUBLE_LOG"], "a")')
        (self.tmp / "bin" / "claude").write_text(double)
        proc = subprocess.run(["bash", str(SCRIPT), "--auto-confirm", str(self.cfg), str(self.skills), str(self.out)],
                              env={**self.env, "DOUBLE_OUT": str(self.out), "ACCEPTANCE_ONLY": "1"},
                              capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        md = (self.out / "1-explain.md").read_text()
        self.assertIn("confirmation rounds met by the proxy: 1 answered `confirm: true` by the harness: "
                      "orbit_deploy_ship (Ship web of demo/dev to tag t1?)", md)


UPSTREAM = textwrap.dedent(r"""
    # A stdio MCP server with the same confirmation round orbitd's mutating tools use: the
    # first call returns InputRequiredResult; the retry with the answer applies or refuses.
    import json, os
    from typing import Annotated
    from mcp.server.mcpserver import MCPServer, Context
    from mcp.server.request_state import RequestStateSecurity
    from mcp_types import ElicitRequest, ElicitRequestFormParams, ElicitResult, InputRequiredResult
    from pydantic import BaseModel, Field
    class Out(BaseModel):
        status: str
        detail: str
    server = MCPServer("upstream", request_state_security=RequestStateSecurity(keys=[b"k" * 32]))
    @server.tool()
    def demo_apply(env: Annotated[str, Field(description="environment")], ctx: Context = None) -> Out | InputRequiredResult:
        '''Apply something after a confirmation.'''
        with open(os.environ["UPSTREAM_LOG"], "a") as fh:
            fh.write(json.dumps({"env": env, "answered": ctx.input_responses is not None}) + "\n")
        if not ctx.input_responses or "confirm" not in ctx.input_responses:
            return InputRequiredResult(input_requests={"confirm": ElicitRequest(params=ElicitRequestFormParams(
                message=f"Apply to {env}?", requested_schema={"type": "object", "properties": {"confirm": {"type": "boolean"}}, "required": ["confirm"]}))},
                request_state="confirm")
        a = ctx.input_responses["confirm"]
        ok = isinstance(a, ElicitResult) and a.action == "accept" and bool((a.content or {}).get("confirm"))
        return Out(status="applied" if ok else "refused", detail=f"applied to {env}")
    @server.tool()
    def demo_read(name: Annotated[str, Field(description="a name")]) -> Out:
        '''Read something; no confirmation.'''
        return Out(status="ok", detail=f"read {name}")
    @server.tool()
    async def demo_slow(seconds: Annotated[float, Field(description="how long")]) -> Out:
        '''Answer late.'''
        import anyio
        await anyio.sleep(seconds)
        return Out(status="ok", detail="late")
    server.run(transport="stdio")
""").lstrip()

DRIVER = textwrap.dedent(r"""
    import anyio, json, sys
    from mcp import Client
    from mcp.client.stdio import StdioServerParameters, stdio_client
    py, proxy, cfg, record, mode = sys.argv[1:6]
    async def main():
        args = [proxy, "--config", cfg, "--server", "orbit", "--record", record]
        if mode in ("auto", "denied"):
            args.append("--auto-confirm")
        if mode == "denied":
            args += ["--never-confirm", "env=prod"]
        if mode == "slow":
            args += ["--call-timeout", "2"]
        out = {}
        async with Client(stdio_client(StdioServerParameters(command=py, args=args))) as c:
            listed = await c.list_tools()
            out["tools"] = sorted(t.name for t in listed.tools)
            if mode == "slow":
                import time
                t0 = time.monotonic()
                r = await c.session.call_tool("demo_slow", {"seconds": 60})
                out["slow"] = {"is_error": r.is_error, "structured": r.structured_content, "took": time.monotonic() - t0}
                r = await c.session.call_tool("demo_read", {"name": "after"})
                out["after"] = {"is_error": r.is_error, "structured": r.structured_content}
                print(json.dumps(out)); return
            out["schema"] = next(t.input_schema for t in listed.tools if t.name == "demo_apply")
            r = await c.session.call_tool("demo_read", {"name": "x"})
            out["read"] = {"is_error": r.is_error, "structured": r.structured_content}
            r = await c.session.call_tool("demo_apply", {"env": "prod" if mode == "denied" else "dev"})
            out["apply"] = {"is_error": r.is_error, "structured": r.structured_content,
                            "text": [b.text for b in r.content if getattr(b, "type", "") == "text"]}
            r = await c.session.call_tool("demo_read", {"name": "nope"})  # unknown-tool shape stays an error
        print(json.dumps(out))
    anyio.run(main)
""").lstrip()


class Proxy(unittest.TestCase):
    """scripts/mcp-confirm-proxy.py against a real MCP server (the pinned SDK, the same
    confirmation round orbitd uses) rather than a canned answer: what is proven is that the
    round is answered exactly once, recorded, and refused where the rail says so, with every
    other result passing through untouched. Runs under the interpreter test_skills.py
    resolves (this one, or services/orbitd/.venv)."""

    @classmethod
    def setUpClass(cls):
        from test_skills import _interpreter
        cls.py = _interpreter()

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / "upstream.py").write_text(UPSTREAM)
        (self.tmp / "drive.py").write_text(DRIVER)
        self.upstream_log = self.tmp / "upstream.log"
        self.cfg = self.tmp / "mcp.json"
        self.cfg.write_text(json.dumps({"mcpServers": {"orbit": {
            "type": "stdio", "command": self.py, "args": [str(self.tmp / "upstream.py")],
            "env": {"UPSTREAM_LOG": str(self.upstream_log)}}}}))
        self.cfg.chmod(0o600)
        self.record = self.tmp / "record.jsonl"

    def drive(self, mode: str) -> dict:
        proc = subprocess.run([self.py, str(self.tmp / "drive.py"), self.py, str(PROXY), str(self.cfg),
                               str(self.record), mode], capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr[-3000:])
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def records(self) -> list[dict]:
        return [json.loads(l) for l in self.record.read_text().splitlines()] if self.record.exists() else []

    def upstream_calls(self) -> list[dict]:
        return [json.loads(l) for l in self.upstream_log.read_text().splitlines()]

    def test_tools_and_plain_results_pass_through_unchanged(self):
        out = self.drive("auto")
        self.assertEqual(out["tools"], ["demo_apply", "demo_read", "demo_slow"])
        self.assertEqual(list(out["schema"]["properties"]), ["env"])
        self.assertEqual(out["read"], {"is_error": False, "structured": {"status": "ok", "detail": "read x"}})

    def test_auto_confirm_answers_the_round_once_and_records_it(self):
        out = self.drive("auto")
        self.assertEqual(out["apply"]["is_error"], False)
        self.assertEqual(out["apply"]["structured"], {"status": "applied", "detail": "applied to dev"})
        calls = [c for c in self.upstream_calls() if c["env"] == "dev"]
        self.assertEqual([c["answered"] for c in calls], [False, True], "exactly one round, one retry")
        recs = self.records()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["tool"], "demo_apply")
        self.assertEqual(recs[0]["arguments"], {"env": "dev"})
        self.assertEqual(recs[0]["messages"], ["Apply to dev?"])
        self.assertIs(recs[0]["answered"], True)
        self.assertEqual(recs[0]["answer"], {"confirm": True})

    def test_without_the_flag_the_round_is_refused_and_nothing_applies(self):
        out = self.drive("plain")
        self.assertEqual(out["apply"]["is_error"], True)
        self.assertEqual(out["apply"]["structured"]["code"], "confirmation_unanswered")
        self.assertIn("nothing was written", out["apply"]["text"][0])
        self.assertEqual([c["answered"] for c in self.upstream_calls()], [False], "no retry reached upstream")
        recs = self.records()
        self.assertEqual(len(recs), 1)
        self.assertIs(recs[0]["answered"], False)
        self.assertNotIn("denied", recs[0])

    def test_an_answer_that_never_arrives_is_said_at_once_and_the_next_call_still_works(self):
        """Measured on the test box (docs/V1.md §8): switching traces restarts the ingress,
        the answer of the wait in flight was lost, and the host waited 1800 s for it. The
        proxy now says so after its own ceiling, as a tool error, and stays usable."""
        out = self.drive("slow")
        self.assertIs(out["slow"]["is_error"], True)
        self.assertEqual(out["slow"]["structured"]["code"], "answer_lost")
        self.assertIs(out["slow"]["structured"]["retryable"], True)
        self.assertIn("orbit_job_status", out["slow"]["structured"]["next_action"])
        self.assertLess(out["slow"]["took"], 20, "the host was left waiting for the late answer")
        self.assertEqual(out["after"], {"is_error": False, "structured": {"status": "ok", "detail": "read after"}})
        self.assertEqual([r.get("lost") for r in self.records()], ["none within 2 s"])

    def test_never_confirm_refuses_the_matching_round_even_with_auto_confirm(self):
        out = self.drive("denied")
        self.assertEqual(out["apply"]["is_error"], True)
        self.assertIn("env=prod", out["apply"]["text"][0])
        self.assertEqual([c["answered"] for c in self.upstream_calls()], [False], "no retry reached upstream")
        recs = self.records()
        self.assertEqual(len(recs), 1)
        self.assertIs(recs[0]["answered"], False)
        self.assertEqual(recs[0]["denied"], "env=prod")


if __name__ == "__main__":
    unittest.main(verbosity=2)
