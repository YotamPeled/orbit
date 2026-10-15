---
name: orbit-swarm
description: "The Bun-rewrite method for Orbit: run large build/refactor phases with agent swarms and a verify suite as oracle. Read before authoring a script."
---

# The swarm method (from the Bun Zig→Rust rewrite, adopted 2026-08-25)

Bun rewrote ~1M lines in 11 days: one human supervising up to 64 parallel Claude agents
through ~50 workflow scripts, with the unmodified test suite as ground truth. We follow
that playbook **to the letter**, scaled to our size. docs/METHOD.md records the decision;
this skill is the operating manual. The original scripts (recovered from Bun's merge
commit `23427dbc`, deleted upstream same day) are in `research/bun-rust-workflows/` —
when in doubt about a pattern, read the original that used it.

## The laws (non-negotiable, in force on every phase)

1. **The oracle is untouchable.** `make verify` + tenant test suites are ground truth.
   No agent doing work may edit a check. A check changes only in its own dedicated
   commit with its own justification, never in the same change it would have failed.
   Keyed-off is legal (stage8 pattern: a check that keys on catalog state and re-arms
   itself); weakened is not.
2. **Every unit of work is a triad**: implement → adversarial review → fix.
   Reviewers see the diff + the spec, are told to ASSUME IT IS WRONG, and vote
   independently (2 votes for anything that can silently corrupt: verify scripts,
   generator, chart, data handling; 1 vote for low-risk mechanical files). Only
   blocking-severity findings dispatch a fixer. Bun's phrasing that works: "DEFAULT
   accept:true if no bugs. DO NOT edit/run." — reviewers review, never touch.
3. **Determinism lives in the orchestrator script; judgment lives in the agent.**
   File paths, unit boundaries, batch membership, phase gates: computed in workflow JS,
   never chosen by an agent (Bun computed every .rs path — `rsPathFor()` in
   phase-a-port — and overwrote whatever the implementer claimed). Agents fill bodies.
4. **One expensive operation per round.** Build/verify/deploy runs ONCE per round by
   the orchestrator (or by you); N fix-agents work from its written output in parallel
   with an explicit NO-BUILD rule in their prompts (see phase-g-test-swarm-v3's
   `NO_BUILD` constant — copy its wording). Then the next round.
5. **Process-first fixes.** A bug class appearing twice means the PROMPT or the guide
   is wrong — fix the generator of the mistake, then re-run the affected units. Bun
   hand-wrote PORTING.md + LIFETIMES.tsv (~3h prep) before unleashing anything;
   equivalent here: write the rules/spec section of the workflow prompt carefully,
   trial one unit, adversarially review it, THEN scale to the batch.
6. **Durable, non-destructive git.** Agents commit with explicit paths
   (`git -c core.hooksPath=/dev/null add <paths> && git commit -q`), never push unless
   told, NEVER reset/checkout/stash/rebase. Parallel agents that touch overlapping
   files get separate worktrees/branches (Bun: 4 worktrees, per-shard branches,
   rebase-never-reset — see phase-g-test-swarm-v3's isolate step).
7. **The human gate is behavioral.** The OWNER sees URLs and "try this" lists at
   milestones, never diffs, and has delegated all in-flight decisions (recorded
   2026-08-25: "i trust you and your best judgement completely... only prompt me if u
   really need me"). Prompt the owner ONLY for: things only a human can physically do
   (browser auth flows), nontrivial money, DNS cutovers, and destructive-irreversible
   actions outside the plan. Everything else: decide, execute, record the decision in
   the commit.

## Authoring a workflow script (our harness)

Use the Workflow tool. Skeleton that matches both Bun's scripts and our A2 sweep
(the A2 script is saved by the harness):

- `meta` with named phases; units array with per-unit files + special-instructions.
- `pipeline(units, implement, review, fix)` — pipeline, not barriers: unit A gets
  reviewed while unit B still implements.
- Structured output schemas on every agent (Bun's are worth copying verbatim):
  - implement: `{done/confidence, files_edited, notes}` — notes feed the reviewers.
  - review: `{accept, issues[{what, where, fix, severity}]}` with severity enums per
    domain (Bun used ub/leak/semantics/perf/style; we used must-fix/nit; pick per task
    and gate the fixer on the blocking subset only).
  - fix: `{applied, remaining, notes}` — fixer may reject hallucinated findings but
    must say so.
- Dedup findings across the 2 reviewers before fixing (key: where + what-prefix).
- `model: 'sonnet'` for mechanical implement/review/fix; hold the orchestrating
  context (you) on the main model. Escalate a unit to the main model only when its
  fixer reports `remaining > 0` twice.
- Batch big files: Bun chunked >1000-LOC writes into ≤800-line Write-then-Edit-append
  chunks (phase-a-port step 4) — reuse that instruction verbatim for any large file.
- Prompts state DO-NOTs explicitly: no builds, no verify runs, no git beyond the
  allowed commands, no reading "for context" beyond the named files, no edits outside
  the unit's files.

## The round loop (Bun phases C–H compressed)

```
while not green:
    run the oracle ONCE (make verify / tests / build)      # orchestrator only
    write failures to per-unit diagnostic files
    swarm: one fix-triad per failing unit, NO-BUILD rule   # parallel
    apply → loop
on green: adversarial DIFF review of the whole round        # phase-h-diff-review
          pattern: 2 voters per changed file vs spec, fix blocking, done
```

## Phase-shape catalog (which Bun script to crib per situation)

| Situation | Bun reference |
|---|---|
| Mechanical transform of many files by rule | `phase-a-port` (implement/verify/fix per file, guide-driven) |
| Pre-classify facts agents will need, cheaply, up-front | `lifetime-classify` (+ 2-vote refutation of low-confidence rows) |
| Breaking a dependency cycle / layering violation | `phase-b0-*` (move-out/move-in/verify with explicit vtable rules) |
| Get a broken tree compiling tier by tier | `phase-b1-tier`, `phase-b2-*` (gate-and-stub, ungate, verify, keystone) |
| Mass test-greening | `phase-g-test-swarm-v3` (ONE build/round, per-file .diag, NO-BUILD fixers, 2-vote review, shard worktrees) |
| Reviewing a finished diff range | `phase-h-diff-review` (2 voters/file, severity-gated fix) |
| Auditing a cross-cutting property (unsafe, idioms, TODOs) | `phase-d-unsafe-audit`, `phase-h-idioms-audit`, `phase-d-todo-sweep` |
| Platform-specific bughunt | `phase-h-windows-bughunt` (hypothesis-driven swarm over subsystem list) |

## Orbit-specific bindings

- Oracle = `make verify` (KUBECONFIG from lib-instance.sh) + `make check` for
  generator drift + **the tenants' own suites**, run unchanged. A tenant that ships
  parity or differential tests is the strongest oracle available for a refactor of it;
  read the instance's own skill for that tenant (an instance keeps its tenant skills
  beside the vendored `orbit-*` ones) before touching its code.
- The repo's own A2 sweep (workflow `instance-string-sweep`) is the local worked
  example: 8 units, 26 agents, then a NEW oracle check (stage3 no-instance-strings)
  caught 7 leaks the reviewers missed. Lesson recorded: after any sweep, STRENGTHEN
  the oracle to cover the sweep's property, then re-run — the check you add catches
  what the swarm missed.
- Before changing a tenant: that tenant's own skill. Before deploys: `orbit-ship`.
- Protecting live production outranks finishing anything — a swarm that has broken a
  serving tenant stops, and production is fixed first. Which hostnames that covers is a
  property of the instance's catalog, not of this file.
