---
name: orbit-rollback
description: "Roll a component back to the tag that ran before (or an explicit one) and prove it; when a restart is the smaller hammer and when it is the wrong one. Needs the operator role. Read orbit-operator and orbit-ship first."
role: operator
---

# Roll back, or restart

Input: a project P, environment E and component C (ids from `orbit_project_list`),
and a reason. Output: the previous (or named) tag running and proven, or a precise
stop. The tools are `orbit_deploy_rollback` and `orbit_workload_restart` (operator
role; absent from your tool list means your token is below operator: say so and stop),
with `orbit_job_wait` and the same verdict as `orbit-ship`. Production needs the same
explicit human go as a ship: for an environment called `prod`/`production`, read and
dry-run, then stop and ask before any confirmation round.

## When to roll back

- A ship verdict of `LANDED NOT DEPLOYED <step>`: the tags-file commit landed, the
  cluster did not prove the new tag. The rollback point is the tag `orbit-ship` wrote
  down in its step 1.
- An alert or a regression after a ship: `orbit-investigate-alert` finds the alert's
  `starts_at` after the ship's ledger `at`, on the shipped component. Roll back first,
  investigate on the old tag.
- A human asks for it, naming the tag or "what ran before".

Not a rollback: a component that was never re-shipped (the tag did not change: this is
`orbit-diagnose`'s job); a rollout stuck for capacity (a rollback needs the same
capacity; report the pending pods); a wrong catalog value (the fix is a catalog change,
not a tag).

## 1. What runs now, and what ran before

`orbit_deploy_status(project, env)`: C's `desired_tag` and `running_images` are the
state you are leaving. `drift: true` with `generation == observed_generation` and a
`desired_tag` pinned a minute or two ago is a pin the cluster has not applied yet (the
applier polls), not a failed rollout: a rollback here races the applier. If the pin
is recent, say so and wait for `drift` to resolve (or for the requester's go) before
rolling back. `orbit_ledger_get(project, env, limit=10)`: entries newest
first; `tags["P/E/C"]` per entry. The previous tag is the value in the newest entry
where it differs from the current one; the entry's `subject` says who pinned it and
its `at` when. Say both tags out loud before changing anything.

## 2. The target: `to`

- **`to` omitted**: the tool itself takes the tag before the current one in the
  tags-file history. Use it when the ledger you read in step 1 agrees with what the
  requester means by "before". `no_previous_tag` (nothing before this pin) or
  `ledger_error`: pass `to` explicitly.
- **`to` given**: an explicit tag, from the requester or from step 1. Prefer it when
  you know the rollback point (a ship you just watched fail; a tag the requester
  named): explicit is checkable, "previous" moves when someone else ships.

Rollback is a new ship of an old tag: it writes a new tags-file commit (history is
never rewritten), resolves the digest, waits for the rollout and runs the same four
assertions. Rolling back to the tag that already runs is a no-op ship; say so instead.

## 3. Plan, confirm, wait, read

`orbit_deploy_rollback(project, env, component=C, to=…, dry_run=true)` shows the
plan (`confirm`, `catalog`, `registry` with `tag=`, `bump`, `rollout`, `image`,
`rollout`, `ready`, `log`, all `skipped`). Then the same call without `dry_run`: the
confirmation round names C, P/E and the tag ("Roll back …"); `status: running` with
`evidence.job_id`; `busy` means wait for the named job (when that job is a ship stuck waiting for a rollout that will not finish, and the requester wants the rollback now, `orbit_job_cancel(job_id)` stops its wait and frees the pair; it undoes nothing). `orbit_job_wait(job_id,
timeout_s=120)` until terminal, then read the verdict exactly as `orbit-ship` step 5
(`source` says how fresh the checkout was). `applied` only with `evidence.verdict:
DEPLOYED` and the four assertion steps `ok`;
`failed` with a `commit` is `LANDED NOT DEPLOYED <step>`: quote it, hand over
(`LANDED NOT DEPLOYED apply` is orbitd's own apply of the rendered objects, present
only when the instance runs with `ORBITD_APPLY=on`; its `detail` names the object
and reason, or `checkout lag` with both commits). A
`failed` at `bump` without a `commit` may still have written (`orbit-ship` step 5: the
ledger lags by up to a minute; read it late and compare with step 1 before saying
nothing was written). **Never call the rollback tool twice for one request**: a retry
is a new write behind a confirmation nobody gave. A rollback that fails leaves the
component on neither tag proven; that is a human's call now, with both tags in front
of them.

A rollback away from a tag whose pods never became ready starts no pod: the pods that
kept serving simply stay. Its `log` step then reads those pods: it names a line they
wrote in the last 15 minutes, or says they were not restarted and wrote nothing in that
time. Both are `ok`; a quiet service is not a failed rollback.

Prove it from the outside the same way: `orbit_deploy_status` (C's `desired_tag` is
the target, a running image carries it, `drift: false`, `rollout: complete`) and
`orbit_ledger_get` (the new `commit` on top with `tags["P/E/C"]` equal to the target).

## Restart: the smaller hammer, `orbit_workload_restart(project, env, component)`

A restart changes no desired state: it stamps the Deployment's pod template
(`orbit.dev/restarted-at`), waits for the rollout and runs the same four assertions
against the tag that is already pinned. No ledger commit (`commit` is null).

Right hammer when the tag is right (`drift: false`, the image carries the desired
tag) and the process is wrong: a wedged connection pool, stale cached state, a
Secret or ConfigMap value that the pod only reads at start. Wrong hammer when:

- the image is wrong or the last ship is what broke it: roll back;
- the pod is `OOMKilled`, `CrashLoopBackOff` or `ImagePullBackOff`: a restart replays
  the crash; `orbit-diagnose` first, then the catalog or the tag;
- a rollout is already under way (`generation` ahead of `observed_generation`): pods
  are being replaced anyway; wait or diagnose;
- `desired_tag: null` (`no_tag` failure): nothing is pinned to assert against; ship a
  tag first.

`dry_run: true` shows the plan (`restart` skipped with the annotation it would patch,
then `rollout` and the four assertions). The live call confirms ("Restart C of P/E in
<namespace>?"), returns `running` with a `job_id`, and the verdict reads the same:
`evidence.restarted_at` is the stamp, `applied` only with the four assertions `ok`.

## The answer

From the tools, nothing inferred: the tag you left and the tag you landed (or the
restart stamp), the new ledger `commit`, `evidence.digest`, `evidence.pods` and
`orbit_deploy_status`'s `running_images`, the four assertions with `status` and
`detail`, and for anything but `applied` the verdict string and the failing step.
Name each tool once.
