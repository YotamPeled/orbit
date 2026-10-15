---
name: orbit-deploy-review
description: "What is running where and how it got there: deploy_status, deploy_history, ledger_get, render_get and the catalog; how to read drift and a missing desired tag. Needs the admin role for the diff and reconcile steps. Read orbit-operator first."
role: viewer
also_uses:
  - orbit_deploy_reconcile
  - orbit_platform_diff
---

# Review a deployment

Input: a project id and an environment (from `orbit_project_list`; never from memory).
The five review steps below need only a viewer token: they call
`orbit_deploy_status(project, env)` and the other viewer tools. The optional tail at
the end calls `orbit_platform_diff(project, env)` and
`orbit_deploy_reconcile(project, env)`, which need the admin role.
Desired state is the instance repository (the catalog,
the tags file, the rendered manifest); running state is the cluster. This review sets
the two side by side.

## 1. Desired vs running: `orbit_deploy_status(project, env)`

One row per catalog component: `desired_tag` (from the tags file), `running_images`
(the images the pod specs pin), `running_digests` (the content the runtime reports for
those pods, as evidence), `drift`, `pod_age` (the youngest pod; `now` is the server
clock), `generation` / `observed_generation`, `pods`.

`drift` compares `desired_tag` with the tag the pod specs pin: it is a statement about
the spec. Two other strings look like "the running image" and are not the spec:
`orbit_workload_list`'s `pods[].image` is the runtime's own name for the image it
runs, which is whichever tag first pulled that content -- a `dev-` tag, or another
repository's name when two components are built from one image -- and is not drift;
`running_digests` is what the pods actually run, by content, and is the thing to
compare by hand against the registry when the tag itself is in doubt (a tag can be
re-pushed under a running pod). No read-only tool resolves a tag to its digest; the
ship does, at ship time -- its `registry` step's `detail` and its `evidence.digest` carry
what the registry reported for the tag, for the last component the ship looked up (a
ship of several components keeps only that one's), so the job record of a
one-component ship is where that tag's digest of the moment is found.

- `drift: false` with a `desired_tag` and a pod-spec image that carries it: this
  component's spec is where the repo says it should be. That is the sentence "it is
  deployed"; a `pods[].image` naming another tag beside it is not a counter-finding
  (above), and `running_digests` is the evidence if content is the question.
- `drift: true`: the desired tag is set and no running image carries it. Either a
  rollout is under way (`generation` ahead of `observed_generation`, or a young
  `pod_age` beside an old one) or it failed and the old pods stayed. Step 2 shows when
  the tag changed; `orbit-diagnose` shows why the new pod is not up.
- `desired_tag: null`: **nothing is pinned** for this component in this env. The pods
  run whatever was last applied, and no ledger entry vouches for it. Drift cannot be
  computed, so `drift` is `false` here; do not read that as "in sync". Say "no desired
  tag recorded" and go to step 3 to see whether one ever was.
  If it is `null` for every component while step 3's ledger shows tags for this pair,
  the two tools are reading the tags file differently: trust the ledger for what is
  pinned and report the disagreement as a tool defect.
- `running_images: []`: nothing runs. Check step 4 for whether the pair is rendered
  at all.
- `pod_age` younger than the last tag change in step 2, with `drift: false`: the pods
  were replaced by something other than a deploy (a template change from the catalog,
  an eviction). Report the age and hand the "why" to `orbit-diagnose`; do not call it a
  restart.
- `not_found`: wrong project id. `backend_unconfigured`: no instance checkout on the
  server; every desired-state tool below will fail the same way, so say so once.

## 2. When did the tag change: `orbit_deploy_history(project, env, component?, limit, cursor)`

Newest first; each item is `{commit, at, subject, tag}` for a commit of the tags file
that changed what you asked about: this pair, or the one component you named. Another
project's or environment's deploy is not listed. The top item is the last change, and
the item under it holds what was pinned before. `orbit_ledger_get` lists the pinned map per commit. With `component` given, `tag` is that component's
value at that commit; without it, the whole env's map. `truncated: true` means older entries
exist; pass `next_cursor` as `cursor`. An empty listing with a `desired_tag` present
means the history is shorter than the tool's window or the file was rewritten; say so.

## 3. The ledger: `orbit_ledger_get(project, env, limit, cursor)`

The same history keyed the way the tags file stores it: `tags` is a map of
`project/env/component` to tag as each commit left it. Only commits that changed a tag
of this pair are listed. Use it to answer "what was pinned before"
(the previous entry's value) and "who pinned it" (the `subject`). `invalid_cursor`:
you passed something that was not a `next_cursor`; omit it and page again.

## 4. What the generator produced: `orbit_render_get(project, env)`

The generated Application manifest for the pair as `yaml`. `source` is
`{commit, age_s, lag_note}` for the git-sync checkout this read came from.
`not_found` means the pair was never generated: the catalog may declare the env, but
nothing will run until it is. Read only what the question needs from the YAML (the
image, the replica count, the resources); it is desired state, not observed.

## 5. What was declared: `orbit_catalog_get(project)` and `orbit_catalog_schema()`

The catalog entry is the source: components, environments, memory, database, domains.
When a field's meaning is unclear, `orbit_catalog_schema()` returns the JSON Schema the
generator validates against; read the property's description there rather than
guessing. `not_found` from the schema tool means the chart is not vendored into the
checkout; report it.

## The answer

Per component: desired tag, running tag, verdict (`deployed`, `rolling`, `drifted`,
`unpinned`, `not running`), and for anything but `deployed` the commit and time from
step 2 or 3 that explains it. Quote field values, name each tool once, and mark a step
whose tool failed with its `what_failed`. To change what runs, hand over to
`orbit-ship` or `orbit-rollback` (operator role); editing the catalog has no tool
behind it: describe the change a human with the instance repository would make.

After a ship, or when the rollout looks right here but the platform disagrees,
an admin can call `orbit_platform_diff`: it renders the desired objects for the
pair and diffs them against live cluster state; read-only, nothing is applied.
It is admin-only: a session that does not see `orbit_platform_diff` in its tool list holds a lower role and should ask an admin for the report rather than retry.
`empty: true` means `missing`, `extra` and `changed` are all empty. `extra` names
objects the live cluster has and the catalog does not.

When the diff shows `missing`, `changed` or `extra` and the pair should follow the
catalog, an admin can run `orbit_deploy_reconcile` (default
`dry_run=true`): it reports the apply plan in order, the prune set named in full, and
the objects a hard rule withheld, all with `dryRun=All`. Only `dry_run=false` on an
orbitd whose `ORBITD_APPLY` is `on` writes, after a confirmation; `refused` with
`ORBITD_APPLY=off` in the steps means this orbitd applied nothing: `ORBITD_APPLY`
off stops every pair, and changing it is not something any tool does — report the
setting and stop.
`orbit_deploy_status.reconcile` with `mode: on` is the healthy reading: `last_run` is
when this orbitd last applied the pair, `applied` how many objects it changed on that
run (`0` when nothing had drifted; a small number after a catalog or tags change),
`unchanged` the rest, `failed: 0` and `would_prune: []` the two that must hold.
A change someone made by hand with kubectl (an env var added, a field edited) is undone
by the loop within one period, so by the time you look the live object and the diff show
nothing. `orbit_deploy_status.reconcile.recent_hand_edits` keeps each one the loop
reverted in about the last hour — the object, the field by its keys (for example
`spec.template.spec.containers[name=api].env[name=X]`) and the field manager that wrote it
(`kubectl-set`, `kubectl-edit`) — and the `apply` step of a reconcile dry run names a hand
edit still in place (`would revert a hand edit: ...`). Read those before you run anything:
they are the answer to "what did someone change by hand".
A `mode` of `off (pair not enabled)` on `orbit_deploy_status.reconcile` means this pair is not in `ORBITD_APPLY_PAIRS` and this orbitd does not apply it: the pair was left out of the list, and adding it is not something any tool does — report that and stop.
`reconcile: null` means the loop is off (`ORBITD_APPLY=off`) or has not reached the pair
yet: no reading about this pair's application at all, which is not the same as
`off (pair not enabled)`, and not a fault of the pair.
