# The build method — adopted from the Bun Zig→Rust migration

> **Operating manual: the `orbit-swarm` skill** (.claude/skills/orbit-swarm/) — read it
> before authoring any workflow. This document records the decision and its evidence;
> the skill is what loads into working context and binds the method to our harness.

*2026-08-25. Studied from primary sources (PR #30412, bun.com/blog/bun-in-rust, the
recovered orchestration scripts in research/bun-rust-workflows/) and community analysis.
This is HOW Orbit is built; the V1 run (docs/V1.md) is executed under it.*

## What Bun did (verified)

~1M lines Zig→Rust in 11 days (May 2026): one human supervising up to 64 parallel Claude
agents via Claude Code workflow scripts, ~$165K API spend, shipped stable as v1.4.0
(Aug 20, 2026). The 53 orchestration scripts are preserved in
`research/bun-rust-workflows/` (deleted upstream the day of merge; recovered from the
merge commit).

## The core principles, extracted and adopted

1. **An unmodified oracle.** Bun's ground truth was its existing test suite, run
   unchanged, zero tests skipped — validation independent of the code being written.
   *Ours is `make verify` + the tenant apps' own suites. They are NEVER edited by the
   agents doing the work; a check may only change through an owner-approved PR that
   touches nothing else.* (This was already Orbit law; Bun confirms it's the load-bearing
   piece.)
2. **Implement → adversarial verify → fix, as a triad per unit of work.** The reviewer
   agents see only the diff/spec, are instructed to assume the work is wrong, vote
   independently (2 votes), and only severity-blocking findings (bug classes named per
   task) dispatch a fixer. Not approval-seeking review — refutation-seeking.
3. **Determinism stays in the orchestrator.** Bun computed file paths, shard
   assignments, and phase gates in script code — agents never chose them. *Our
   generator/catalog plays this role: agents fill bodies; structure is computed.*
4. **Process-first fixes.** When a bug class recurs, fix the prompt/guide/workflow that
   generates it, not each instance. Prep artifacts (their PORTING.md pattern guide,
   mechanical pre-classification runs) are cheap and pay for themselves.
5. **One expensive operation per round.** Their test swarms rebuilt ONCE per round;
   dozens of fix agents worked from written diagnostics without building. *For us: one
   `make verify` / deploy per round; agents work from its output files.*
6. **Sharded worktrees, durable commits, no destructive git.** Parallel agents in
   isolated worktrees/branches; commit early, push branches, never reset.
7. **The human gate is behavioral, not code review (the owner, 2026-08-25: "i dont want to
   look at code here").** The owner never reads diffs. His gates are: (a) decision approvals
   — direction, spend, anything destructive, DNS — as always; (b) **manual acceptance
   testing at milestones** — he clicks the URLs, drives the console, tries the wizard,
   confirms the thing behaves; invited whenever a milestone is human-testable, and his
   findings enter the loop as ordinary bug reports for the swarm. The correctness burden
   therefore rests entirely on the oracle (verify + tests) and the adversarial review
   pass — which is Bun's actual model, and why principles 1–2 are non-negotiable here.

## The evidence rule — every claim carries its grade (owner-locked 2026-08-31)

*Locked by the owner 2026-08-31, after the PR #27/#28 review cycles. Three documented cases
of unproven assertions — by designers AND by reviewers, symmetrically — cost roughly three
full review rounds; the rounds that measured first converged fastest (SECRET-PROVISIONING.md
revisions 8–9, which measured the minting authority and then redesigned around it, are the
worked example). This is law, not guidance, and it binds principle 2's triad on both sides:
the reviewer is under it exactly as hard as the builder.*

**1. Two grades, no third.** Every factual assertion by any agent — builder or reviewer,
*"what I built does X"* or *"your document is wrong about Y"* — carries exactly one of:

- **CONFIRMED** — an executed check, published with its output.
- **PLAUSIBLE** — believed, not proven.

No unlabeled factual claims. No third grade. **Suspicions are welcome** — a PLAUSIBLE finding
is a legitimate contribution and gets triaged, not banned. What is banned is a suspicion
wearing the clothes of a fact.

**2. A fix agent's FIRST move on a PLAUSIBLE finding is to run the measurement — never to
start rewriting.** Answering prose with prose is the failure mode this rule exists to kill.
Run the check, publish it, and let the graded result decide whether there is anything to
rework at all.

**3. Factual disagreements are settled by the AUTHORITATIVE measurement, never by argument.**
Per fact class, what "authoritative" means is named — seeded with the classes that have
already cost us rounds:

| Fact class | The authoritative measurement |
| --- | --- |
| Kubernetes permissions | `SubjectAccessReview` against the API server's own authorizer — never `kubectl auth can-i` string parsing |
| Vault capability or behavior | An executed call on the live instance, or on a faithful throwaway twin, published with commands and outputs |
| Cluster / network state | A direct read of the live object — not documentation |

The table grows: a fact class two agents disagree about gets a row before it gets a verdict.

**4. Corollaries — already practiced, now law.**
- A claim promises no more than its published evidence proves.
- A new measurement is PUBLISHED to the evidence record before it is cited.
- An unproven claim is marked **UNPROVEN in place** — at the sentence, not in a footnote.

**Platform corollary.** Proving has to be cheaper than asserting or the rule decays into
paperwork, which is part of why orbitd's evidence-shaped tool results exist: an agent
proves a claim with one call.

## How this applies concretely

- **The V1 run** (docs/V1.md): the oracle is `make verify` plus the scenario and
  observability suites, written before the fixes they judge; a check is never weakened to
  get green, and a check change goes in its own commit saying why. V1.md is the one
  progress record (Bun's PORTING.md), recomputed from what the checks print, never from
  memory.
- **The product loop** (V2, docs/V1.md §9): an in-cluster coding loop with verify as the
  reward is this method productized; it is recorded, not built.

## What we explicitly do NOT copy

- Merging a mega-branch with per-line review skipped (their scale forced it; ours
  doesn't).
- 64-wide swarms on day one (subscription limits; scale swarm width to what the plan's
  5-hour windows sustain).
- Deleting the orchestration scripts afterward (ours are product artifacts — versioned
  in core, reusable per org).

## The operational model (researched 2026-08-26 — how it actually ran for 11 days)

Two research passes: forensics on the 53 recovered scripts, and the public record
(Sumner's blog, Anthropic's case study, the deleted-files commit fetched from the API).

**Bun was never human-free — it was human-cheap in a specific way.** Sumner, firsthand:
"For most of those 11 days (and after), I monitored workflows — manually reading the
outputs to check for issues and bugs, and prompting Claude to edit the loop to fix
things." One person, playing exactly one role: the operator. What that role did NOT do
is as important: never reviewed diffs, never fixed code by hand, never approved items.

The division of labor, evidenced in the scripts themselves:
- **Workflows are bounded single shots.** No scheduler, cron, or chaining inside any of
  the 53 files. Capped internal rounds; on a stuck condition they BAIL with a reason
  (`bailed: "dep_broken_3x"`) and expect relaunch later — never grind forever.
- **The queue writes itself from ground truth.** Deleted helper scripts prove the
  pipeline: CI/build output → results JSON → categorize → task items → next cycle.
  Every round recomputes "what's left" from compiler/test reality, never from memory.
- **The operator owns git pushes, rebuilds, relaunches, and process edits.** Agents are
  forbidden all of it. When something goes systemically wrong, the fix is an edit to
  the WORKFLOW ("never run git stash… no slow commands"), then re-run — "fix the
  process that generates the code instead of hand-fixing the code."
- **Governance changes don't auto-apply.** The one workflow that audits PORTING.md
  returns a proposed patch instead of committing — the sole approval gate in the set.

**Mapping to Orbit:** the operator role is played by the session itself under `/loop` —
launch workflows, read outputs, fix the loop, relaunch bailed work, keep docs/V1.md
(our PORTING.md) current. The human sits ABOVE the operator, reached only through hard
stops Bun didn't need: live production (tenant-a), money, DNS, irreversibles. That makes
Orbit's target autonomy strictly higher than Bun's — Bun had no prod to protect and one
person watching; we have prod to protect and nobody watching, which is why the hard-stop
list and the untouchable oracle carry the weight Sumner carried by reading outputs.
