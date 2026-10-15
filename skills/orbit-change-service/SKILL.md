---
name: orbit-change-service
description: "Change how a project runs by changing its catalog entry (replicas, memory, a per-environment override, a hostname, a component), wait until it is live and prove it; back up its database; remove a project completely. Needs the operator role; removing a project needs admin. Read orbit-operator first."
role: operator
also_uses:
  - orbit_project_remove
---

# Change a service

Input: "run two replicas of P's api", "give the worker more memory in dev", "serve P on
this hostname too", "back up P's database", "remove project P". Output: the change live in
every environment it touches, with the evidence, or a precise stop.

How a project runs is its catalog entry. You do not edit files or open a pull request:
`orbit_catalog_set` replaces the entry in one commit, behind a confirmation, and orbitd
applies it. Shipping a new image tag is not a catalog change (`orbit-ship`). Removing a
project is `orbit_project_remove(project)`, which needs the `admin` role: see the last
section.

## 1. Read the entry: `orbit_catalog_get(project)`

`data` is the entry. `orbit_catalog_schema()` says what an entry may hold: its `entry` is
the shape of an entry (its keys, and the keys an environment takes), its `body` the JSON
Schema one component and the database are validated against. Find the field you are about
to change there, do not guess its name. A change for one environment only goes under that
environment (`environments.<env>`), beside what the entry already overrides there.

Hostnames have three rules the generator enforces: `hostnames` are names under the
instance's own domains (a domain of the project's own goes under `customDomains`); a name
another project has is refused; and so is one of the platform's own (`grafana`, `orbit`,
`mcp`, `intake` under the primary domain). Left out, an environment gets
`<id>.<primary domain>` (prod) or `<id>.<dev domain>`.

`orbit_catalog_history(project)` lists the changes already made to the entry: the commit,
when, who (`actor`), and which paths. Read it when the question is "what changed, and
when".

### What size to give it: `orbit_size_recommend(project, env)`

Before changing a component's `memory`, `memoryLimit` or `cpu`, and whenever the task is
"is this sized sensibly", ask `orbit_size_recommend(project, env)`. Per component it
gives, for memory and for CPU separately, what is set today, what was used over the last
week, and a `verdict`: `lower` or `raise` with a `suggestion` to write into the entry
(`request_mi: 128` is `memory: 128Mi`, and `memoryLimit` the same where the entry names
one; `request_m: 50` is `cpu: 50m`), or `leave` (within a tenth of today's value:
do not touch it). `no_suggestion` means it will not guess and `reason` says why: too
little history, gaps in the data, or memory that sits at its limit without being
killed. In that last case memory use does not show the need (a runtime that manages its
own heap fails inside its limit); its error rate and heap metrics do, and raising it is
the change to consider, never lowering. A `lower` for memory says, at the end of its
`reason`, what the numbers cannot tell: a runtime that sizes its heap from the limit (.NET,
a JVM) can be failing far below the limit, where its use looks like room to spare. Before
you lower memory on such a service read its error rate (`orbit-diagnose`), and again after.
The tool only advises. You apply a suggestion
with `orbit_catalog_set` as below, change only the components whose verdict is `lower`
or `raise`, and ask again a week later: until then the window still holds the old size.

## 2. See what would change: `orbit_catalog_set(project, entry, dry_run=true)`

Pass the whole entry back with your change made, not a fragment. `evidence.changed` lists
the paths that differ, a list item named by its `name` (`components[api].replicas`). If it
lists anything you did not mean to change, fix the entry first. `evidence.unchanged` means
the entry is already what you asked for.

The dry run also checks the entry: its one step `check` runs the generator and the commit
gate's checks on it (up to a minute) and writes nothing. `status: dry_run` with `check`
`ok` means the real call will be accepted. `status: failed` means it would be refused, and
the step's `detail` is the refusing sentence (a field the generator does not know, more
memory than the node has, two components on one path, a secret nobody declared): fix the
entry and run the dry run again.

## 3. Change it: `orbit_catalog_set(project, entry)`

It asks for confirmation, listing the same paths. A catalog change reaches every
environment of the project, production included: answer only if the task gave you that.
It returns `status: running` with `evidence.job_id`; wait with `orbit_job_wait(job_id,
timeout_s=120)`. The job's steps are `confirm, commit, checkout`:

- `commit` failed: the generator or one of the commit gate's checks refused the entry,
  and nothing was written. `detail` is the refusing sentence (a field it does not know, a
  host outside the instance's domains, a database while the `databases` feature is off,
  reservations that no longer fit the node). Fix the entry and start again at step 2.
- `checkout` failed: the commit landed and orbitd's commit gate refused it all the same
  (rare: the checks run before the push; it means something else on main changed in
  between); `detail` carries the failing check's sentence. Nothing of it is applied, and
  **while main's newest commit is refused nothing can be shipped for any project**
  (`orbit_deploy_ship` answers `main_refused`). Undo it at once with this same tool: pass
  the entry `orbit_catalog_get` returns (it is the last accepted one). On a refused main
  that is not `unchanged`: the confirmation says the entry is committed over the refused
  one, and `evidence.restores` names the refused commit.
- `applied`: the commit is accepted. It is not live yet.

`orbit_deploy_status(project, env)` shows the gate at any time: `gate.verdict` is
`accepted` or `refused`, with `gate.check` and `gate.sentence`.

## 4. Wait until it is live: `orbit_deploy_wait(project, env, commit)`

Once per environment the change touches, with the job's `commit`. Call it again while it
answers `waiting`. `applied` means orbitd applied that commit to the pair; `failed` and
`refused` say why it will not be.

## 5. Prove it

Use the tool that shows the thing you changed, per environment:

- replicas, memory: `orbit_workload_describe(project, env, component)`: the deployment
  spec with its requests and limits, and the pods. Count the pods and read each one's
  state.
- a hostname: `orbit_render_get(project, env)` returns the generated manifest, whose
  values list the hostnames. A new host's certificate takes a minute or two;
  `orbit_platform_health()` lists each certificate with its `days_left` once issued.
  Whether the door answers from outside is the prober's to say, not yours to try: with
  the `uptime` feature on, `orbit_metrics_query(query='probe_success{instance=~".*<host>.*"}')`
  is 1 once the new host serves (the project's probe picks a new host up with the same
  commit). You have no shell; do not look for one.
- anything: `orbit_deploy_status(project, env)` must show `drift: false`.
- something you REMOVED (a component, a custom domain and its certificate): orbitd's loop
  applies what the catalog holds and deletes nothing. What the entry no longer renders
  stays on the cluster and is listed in `orbit_deploy_status`'s `reconcile.would_prune`
  (a removed component's Deployment keeps running until then). Deleting it is the tool
  `orbit_deploy_reconcile`, which needs the `admin` role and asks for confirmation naming
  every object it will delete (skill `orbit-deploy-review`). Report what `would_prune`
  lists and that it waits for `orbit_deploy_reconcile`; do not call the change done while
  `would_prune` is not empty.

A change that is applied and whose pods are not ready is not done: go to `orbit-diagnose`.

## 6. Back up a database: `orbit_db_backup(project, env)`

Before a risky change, or when asked. It confirms, then runs a job with steps `confirm,
backup`: the database is dumped, the dump is restored into a scratch database on the same
server and every table's row count compared, and the file is kept on the platform's
backups volume and in the bucket when there is one. `applied` with `evidence.backup` (the
file's name) and `evidence.verified` is a backup. `failed` with a `BACKUP REFUSED:`
sentence means there is no backup: report the sentence, do not go on to the risky change.
The tool exists while the `databases` feature is on. Restoring is `orbit-restore-db`.

## Removing a project: `orbit_project_remove` (admin)

`orbit_project_remove` needs the `admin` role; with a lower one it is not in your tool
list, and the answer is that the job needs an admin token. Call it with `dry_run: true`
first: `evidence.namespaces` and `evidence.databases` are what would be deleted. Then
without it: the confirmation names them. A production database needs a human go in so
many words. Wait with `orbit_job_wait`. Its steps are `confirm, commit, checkout,
monitors, backup, remove`: the catalog entry is deleted (the files of the project's
monitors go in the same commit), the rules and probes of those monitors are deleted, every
database is backed up as in step 6, and only then are the namespaces deleted. The
monitors go first so that none fires at the project's own removal. If a backup is refused,
`remove` is `skipped` and nothing on the cluster was deleted: report the `backup` step's
sentence and stop. Do not look for another way to delete the namespaces. On `applied`, `evidence.backups` names the
backup files that outlive the project. A production database's archive also stays in the
bucket; the removal's commit writes the name down in `catalog/retired.yaml`, and a project
made under that name again is given an archive path of its own. Do not edit that file.

## The answer

What changed (the paths), the commit, per environment what `orbit_deploy_wait` returned and
the evidence from step 5. For a backup: the file's name and the `verified` line. For a
removal: the namespaces that went and the backups that stayed.
