---
name: orbit-ship
description: "Ship a tag of a component to an environment and prove it landed: deploy_status for the rollback point, dry_run for the plan, the confirmation round, job_wait, then the verdict with its four assertions. Needs the operator role. Read orbit-operator first."
role: operator
---

# Ship a tag

Input: "ship tag T of component C to environment E" (project P, C and E as ids from
`orbit_project_list`, never from memory). Output: a proven landing or a precise stop.
The tools here are `orbit_deploy_ship` (operator role) and `orbit_job_wait`; the
proof is read back with viewer tools.

**Role first.** If `orbit_deploy_ship` is not in your tool list, your token is below
`operator`: nothing you can call ships anything. Say the job needs an operator token,
name the `orbit_deploy_ship()` call an operator would make, and stop.

**Production needs a human go.** An environment whose id is `prod` or `production`, or
that the catalog or the requester calls production, is never shipped on a request
alone: do steps 1 and 2 (reads and the dry run are safe), then stop and ask for an
explicit go naming the tag and the environment. Do not start the confirmation round
(step 3) for production until that go is in the conversation. A non-production
environment proceeds without asking.

## 0. No tag yet? Build one on this machine

When the request names a commit or a branch rather than a tag ("ship my latest change
to dev"), the tag does not exist until you build it. Images are built here, on this
machine, and pushed to the registry; nothing in the cluster builds, and no CI is in the
path. Build only from a commit anyone can check out again:

1. In the app's repository: `git status --porcelain` prints nothing (no uncommitted or
   untracked change), and `git fetch` then `git status -sb` shows the branch neither
   ahead of nor behind its remote (the commit is pushed). If either fails, stop and say
   which; do not commit or push someone's work for them to make it pass.
2. The tag is `<branch>-<sha7>`: `git rev-parse --abbrev-ref HEAD` and
   `git rev-parse --short=7 HEAD`. The image name is the component's `image` from
   `orbit_catalog_get` (never typed from memory).
3. `docker buildx build --platform linux/amd64 -t <image>:<tag> --push .` (add the
   build arguments the repository's own README names, e.g. one Dockerfile building
   several components with `--build-arg APP=<component>`). `--platform linux/amd64`
   because the node is amd64 whatever this machine is.
4. A push refused with `denied` or `unauthorized` is the registry credential: this
   machine's `gh` token needs `write:packages` (`gh auth refresh -s write:packages`,
   then `gh auth token | docker login ghcr.io -u <user> --password-stdin`). Say so and
   stop; that is the human's to grant.

Then ship that tag from step 1. A promotion to the next environment ships the same tag
again: it is never rebuilt, so the next environment runs the bytes this one ran.

## 1. What runs now: `orbit_deploy_status(project, env)`

Find C's row. Note `desired_tag` and `running_images`: **that tag is the rollback
point**; you will hand it over if anything goes wrong, so write it down before
anything changes. Then decide:

- `drift: false` and a running image carrying T already: nothing to ship. Report
  that and stop.
- `drift: true`, or `generation` ahead of `observed_generation`: a rollout is under
  way or stuck. Do not ship on top of it; report what you see and hand to
  `orbit-diagnose`.
- `desired_tag: null`: nothing is pinned. A ship still works (it writes the first
  pin), but there is no rollback point in the ledger; say so before continuing.
- `not_found` or `backend_unconfigured`: stop and quote `what_failed`.

## 2. The plan: `orbit_deploy_ship(project, env, components=[C], tag=T, dry_run=true)`

`status: dry_run` and every step `skipped` except `catalog`, in the order the job will
run them: `confirm` (detail `dry_run`), `catalog` (`ok`; detail names the namespace --
the one check a dry run really performs), `registry`
(detail `tag=T`), then per component `bump`, `rollout`, and the four assertions
`image`, `rollout`, `ready`, `log`. Nothing is written by a dry run; no confirmation
is asked. A `failed` `catalog` step here means an unknown project, environment or
component, or an environment the catalog does not enable: fix the ids from
`orbit_project_list` and `orbit_catalog_get`; do not guess.
`evidence` is `{}` on a dry run: nothing ran, so nothing was observed; the digest, pods
and verdict fields appear only on the live verdict.

`expect` is a registry digest (`sha256:…`). Pass it only when the requester gave one;
the job then fails at `registry` before any write if the tag resolves to a different
digest. There is no tool that returns a digest ahead of the ship.

## 3. Ship: `orbit_deploy_ship(project, env, components=[C], tag=T)`

The same call without `dry_run`. It asks for confirmation before writing: an input
request under the key `confirm` whose message names C, P/E and T. The host answers
it (a human, a `--yes`, or the harness); you do not fabricate an answer. Then:

- `status: running` with `evidence.job_id`: the ship started. Steps so far are
  `confirm` ok and `catalog` ok. Go to step 4.
- `status: refused`: the confirmation was declined (nothing was written), or the
  `apply` step was skipped after the bump landed (nothing was applied; the step's
  `detail` names `ORBITD_APPLY` or `ORBITD_APPLY_PAIRS`). The named setting is the
  cause: `ORBITD_APPLY` off means this orbitd applies nothing anywhere, a pair
  missing from `ORBITD_APPLY_PAIRS` means it does not apply this pair. Changing
  either setting is not something any tool does — they belong to whoever runs this
  instance, so report the named setting and stop; do not retry and do not wait for
  `applied`.
- failure `code: busy`: something holds P/E. With `evidence.job_id` it is another job:
  wait for that one with `orbit_job_wait`, re-run step 1 (the world changed), then retry
  once. With `evidence.holder: reconcile` it is the reconcile loop applying the pair for
  a few seconds: there is nothing to wait on with a tool, so retry once after about 15
  seconds.
- failure `code: main_refused`: the commit gate refused the instance repository's newest
  commit, so a tag bump on top of it would be refused too. Nothing was written. The
  message names the commit, the check and its sentence. It is not this ship's fault and
  no retry helps until a commit passes: report it, and if the refused commit is a catalog
  change, `orbit-change-service` step 3 says how it is undone.
- an error saying the host could not answer the confirmation (nothing was written):
  stop and hand over; the ship needs a host that answers confirmations.
- `status: failed` before a job starts: read `steps[].detail`; nothing was written.

## 4. Wait: `orbit_job_wait(job_id, timeout_s=120)`

A ship takes minutes (the rollout waits up to ten, the assertions about one more),
and one wait is capped at 120 s. Call it again while the record's `status` is
`pending` or `running` (`poll_after_ms` is set); stop when it is `completed`,
`failed` or `cancelled`. Do not call the ship tool again while waiting: that is what
`busy` guards. `not_found`: the job expired or the id is wrong; read the truth with
step 5 instead.

The wait does not always run its ten minutes. A tag whose pods crash at start
(CrashLoopBackOff, from the third crash) or whose image cannot be pulled ends the
`rollout` step at once, and the step's `detail` says which pod, which reason, how many
restarts and the last exit. A tag whose pods start and never become ready is waited
for the full ten minutes (they may only be slow); while it waits,
`orbit_deploy_status` shows the component with `rollout: progressing` or `stuck` and
`rollout_detail` naming the pod. `orbit_job_cancel(job_id)` stops that wait when the
requester asks for it: the job ends `cancelled`, the pair is free at once, and nothing is
undone (the tag bump stays landed, the old pods keep serving), so say what is next:
`orbit-rollback`, or a ship of a good tag.

## 5. Read the verdict

The record's `result` is the mutating envelope: `status`, `steps`, `commit`,
`evidence`, `source` (`commit`/`age_s`/`lag_note` for the git-sync checkout; desired
state can lag the instance repo by up to the note's bound). Read every field; the
prose comes last.

- `steps`, in order: `confirm`, `catalog`, `registry` (detail: the digest), `bump`
  (detail: the component; the tags-file commit), `rollout` (the job's rollout wait;
  detail: the pod counters), then **the four assertions**: `image` (every pod of C
  runs an image carrying T), `rollout` (generation observed, replicas updated ==
  ready == available, no OOMKilled / CrashLoopBackOff / ImagePullBackOff),
  `ready` (Ready held across the probe window, no restarts), `log` (a Loki line from
  the new pods since the ship, and the readiness HTTP check for a web component).
- `commit`: the tags-file commit the ship wrote. `evidence.digest`: the registry
  digest of T. `evidence.pods`: the pod images observed. `evidence.verdict`:
  `DEPLOYED` or `LANDED NOT DEPLOYED <step>`. `evidence.components` is one
  `{name, stage}` per requested component (`not_reached` | `rolled_out` |
  `asserted` | `failed` | `refused`); an empty list is not "none requested".

**`applied` means landed** only when `status: applied`, `evidence.verdict` is
`DEPLOYED` and all four assertion steps are `ok`. Then prove it from the outside:
`orbit_deploy_status` shows C with `desired_tag` T, a running image carrying T and
`drift: false`; `orbit_ledger_get(project, env, limit=3)` has `commit` at the top with
`tags["P/E/C"] == T`. Quote both.

One exception, by kind: a `cron` component (a scheduled job) has no pod until its next
run, so its `rollout` step proves the CronJob's template carries T and its four
assertions are `skipped` with the detail `kind cron: no pod until the next scheduled run`.
That is a landing, and `orbit_deploy_status` shows no running image for it until the run;
prove the run itself after its schedule fires, with `orbit_workload_list` (the run's pod)
and `orbit_logs_query` (its output).

**`failed` with a `commit`** is `LANDED NOT DEPLOYED <step>`: the desired state
landed in the repository but the cluster did not prove it. Quote the verdict string
and the failing step's `name` and `detail` verbatim. **Do not retry**, do not ship
another tag, do not restart: hand to a human with the rollback point from step 1 and
say that `orbit-rollback` is the next move if they want it.
`LANDED NOT DEPLOYED apply` is the `apply` step (orbitd applying the rendered objects
itself, only when the instance runs with `ORBITD_APPLY=on` and the pair is in
`ORBITD_APPLY_PAIRS`): its `detail` names the failing object and reason, or
`checkout lag` with the two commits when the checkout never caught up; same hand-over.
A `skipped` `apply` step instead refuses the job with nothing applied: its `detail`
names `ORBITD_APPLY` or `ORBITD_APPLY_PAIRS`, which is the setting that stopped the
job — `ORBITD_APPLY` off stops every pair, a pair left out of `ORBITD_APPLY_PAIRS`
stops only this one. No tool changes either setting; report the named one and take
the same hand-over, without retrying and without waiting for `applied`.

**`failed` without a `commit`** stopped before or during the write. `catalog` or
`registry` failed: nothing was written; its `detail` names the image, the tag and the
component it asked for (`manifest not found` on a tag you know exists: read the image
name, the catalog entry may carry a wrong one). `bump` failed: the write **may** have
happened; the step runs the repository's own push script and fails when that script
errors *or* when it returns no commit line, and its `detail` (the `STEP … OK/FAIL`
lines) does not tell you which. The ledger is the only proof, and it lags: the
desired-state tools read a checkout that refreshes about once a minute, so a
`orbit_ledger_get` whose top entry is still the one you saw in step 1 proves nothing
until `orbit_deploy_status`'s `now` is at least 60 s past the job record's
`created_at`. Do your other reads first, then read the ledger again and compare its
top `commit` and `at` with step 1: a new entry carrying T is a landed write with a
failed verdict, and you report it as `LANDED NOT DEPLOYED bump` with that commit.
Say "nothing was written" only after a read that late still shows the old top. You
have no tool that waits; if the host gives you one (a timer, a shell `sleep`), use it
and re-read. Otherwise, if your last read is still inside the minute, report the ledger
as **provisional**, give the human the job's `created_at` and the top commit you saw,
and ask them to re-read; do not fill the gap with a guess.

**One confirmation per request, no retries.** A `failed` ship is never re-run by you,
whatever it failed on and whether or not you believe it wrote: each live call is a
new write attempt behind a new confirmation, and the human who answered the first
did not answer the second. Report, hand over, stop.

`cancelled` with `error: busy` is the lock, not a failure of the ship; see step 3.
`cancelled` with a last step `cancel` is a job someone stopped with `orbit_job_cancel`
(`evidence.cancelled_by`): read `commit` to see whether its bump had landed.

## The answer

Four lines and a table, quoted from the tools, nothing inferred:

1. commit: the tags-file commit from `result.commit`, and the ledger entry that shows it;
2. digest: `evidence.digest`;
3. pod images: `evidence.pods` and `orbit_deploy_status`'s `running_images`;
4. the four assertions: `image`, `rollout`, `ready`, `log`, each with its `status`
   and `detail`;

plus the rollback point (the tag that ran before) and, for anything but `applied`,
the verdict string and the failing step. Name each tool once; mark a step whose tool
failed with its `what_failed`.
