---
name: orbit-diagnose
description: "Why is a component unhealthy, or why did it restart: the deploy_status → workload_describe → metrics_query → logs_query → alerts_active walk, with the answer shape. Read orbit-operator first."
role: viewer
---

# Diagnose a component

Input: a project id, an environment and a component name. If you were given words
rather than ids, resolve them with `orbit_project_list` first and use only what it
returned. Every step below names what to do when its tool fails; a `FORBIDDEN`
refusal cannot happen here (every tool is viewer), so if you see one, your token holds no
Orbit role at all: report that and stop.

## 1. Is the running state what was desired?

`orbit_deploy_status(project, env)`. Find the component's row: `desired_tag`,
`running_images`, `drift`, `pod_age`, `generation` vs `observed_generation`, `pods`.

- `drift: true`: the running image does not carry the desired tag. A restart loop on an
  old image and a rollout that never completed look the same here; step 2 tells them
  apart. `desired_tag: null` means nothing is pinned for this component; say so.
- `rollout`: `complete`, `progressing` (new pods are starting) or `stuck` (a new pod is
  in CrashLoopBackOff, cannot pull its image or cannot start: it will not finish by
  waiting), with `rollout_detail` naming the pod and the reason. `drift` is true while
  any pod still runs another tag than the desired one, so new pods that cannot start
  beside old ones that still serve read `drift: true, rollout: stuck`.
- `generation` ahead of `observed_generation`: a rollout is in progress or stuck.
- `pods: []`: nothing is running. Go to step 2 for the events.
- `not_found`: the project id is wrong; re-run `orbit_project_list`. `backend_error` with
  `retryable: true`: retry once, then report the step as unavailable and continue.

## 2. What do the pods and events say?

`orbit_workload_describe(project, env, component)`. Read, in this order:

- `pods[].restarts` and `pods[].last_terminated_reason`. `OOMKilled` is a memory limit
  hit; `Error` is a non-zero exit (the logs say why); `Completed` on a long-running
  component means it exited on its own. `restarts: 0` on every pod and `ready: true`
  means **the container did not restart**: say that, with the pod rows as evidence, and
  do not invent a cause. What the asker saw was then a **replacement**: a new pod under
  a new name (the middle hash in the pod name is the ReplicaSet, and a new one means the
  pod template changed: a tag, a limit, a probe) or the same ReplicaSet re-creating a
  pod after an eviction or a node event. `age` dates it against `now`; step 1's history
  says whether a tag moved then, and step 3's `kube_*` series say whether the template
  did. Answer "replaced at <time> by <cause>, not restarted".
- `events[]`: `reason` and `message` for `BackOff`, `Unhealthy` (a probe failed; the
  message names the probe and the response), `FailedScheduling` (no room; see
  `orbit-budget-review`), `Failed` (image pull, mount). Empty `events` on an older
  pod means they expired (the tool says so), not that nothing happened; step 3 covers
  that window.
- `usage_vs_limit[]`: `memory` against `memory_limit` per pod (this component's pods).
  Near the limit plus `OOMKilled` is the cause; `null` means the metrics API did not
  answer, use step 3.
- `spec.probes` and `spec.limits`: what the pod is being held to.
- `not_found` naming the component: it is not in this project's catalog entry; list.

`orbit_db_status` exists while the `databases` feature is on. A database's `orbit_db_status` reads `last_successful_backup: null` and
`first_recoverability_point: null` on a cluster whose backups run through the object
store plugin, which keeps its own catalogue: read `wal_archive` and the backup jobs'
phase for whether backups happen, and treat a restore as unproven either way.
A production database's scheduled backups leave no jobs to read (a `ScheduledBackup`
with an object store keeps its catalogue in the store), so from this surface only
`wal_archive` says anything; whether a base backup ever succeeded is not readable here.

## 3. Confirm with the series

`orbit_metrics_query` as a range query around the event. The namespace is the
`namespace` field step 1 returned; the pod names are step 2's `pods[].pod`. Start from
the shape below, then read the `metric` map of the first series that comes back and
use the label names it actually carries (they are Prometheus's, not this skill's):

```
container_memory_working_set_bytes{namespace="<namespace>", pod=~"<pod-prefix>.*", container!=""}
rate(container_cpu_usage_seconds_total{namespace="<namespace>", pod=~"<pod-prefix>.*", container!=""}[5m])
```

with `start`/`end` as unix seconds or RFC 3339 bracketing the restart and `step` of
`1m` or wider. `now` on `orbit_deploy_status` / `orbit_workload_describe` is the
server clock (RFC 3339); `end` omitted means now; `start` is now minus the pod's
`age` plus a margin, or a timestamp from the history or an alert. Series come back
stamped in unix seconds; report those as they are, or as an offset from `now`, and
do not reach for a shell to convert them.
Read `values` as `[timestamp, value]` pairs; a series climbing to the limit before the
restart confirms memory; a flat series says memory was not it. `truncated: true` means
the cap cut points: widen `step`. Empty `items` means no series matched: check the
label names before concluding there is no data. The same holds for a dashboard panel
reading "No data": run `orbit_dashboards_check(project, env)` first (it exists while the `dashboards` feature is on) — a `no_data` row
names the project, environment and panel whose component emits nothing yet, while a
`broken` row carries the reason Prometheus refused the query, which is how a renamed
metric sits unnoticed behind a worker everybody assumed was idle. An `ok` row is a
panel with data. `total: 0` means no panel with a query was checked: the pair has no generated
dashboard (not rendered, or `project`/`env` are not what `orbit_project_list`
returned) or its panels carry no queries. It is not "every panel is fine".
`backend_unconfigured` (no Prometheus) is reported as a gap, not as "no memory problem".

When step 2 showed no restart, the same tool dates the replacement from the cluster's
own state series (kube-state-metrics names, present wherever Prometheus scrapes it):
`kube_pod_container_status_restarts_total`, `kube_pod_start_time` and
`kube_pod_container_resource_limits` filtered by `namespace` and `container=
"<component>"`, and `kube_replicaset_created{namespace="<namespace>"}`. A new pod with
the same restart count of zero and a different limit, image or ReplicaSet is a
template change; a new pod under the same ReplicaSet is an eviction or a node event.

## 4. What did it log before it died?

An empty `items` is not yet a verdict. With `component` given, the tool first lists the
component's pods that exist now: none, and it returns empty without asking the store;
some, and it asks the store for those names only. Either way a predecessor pod's lines
are out of its reach, replacement running or not. So first ask again without
`component` (the whole namespace, same window). Its lines come from every pod in the
namespace, this component's current pods included. The namespace's current pods are
the union of step 2's `pods[].pod` over every component the project's catalog entry
lists (step 2 describes one component at a time, and lists at most a hundred of its pods:
`pods.truncated: true` means the union is incomplete and this reading does not apply).
With the union complete, a line whose `pod` is none of them came from a pod that no
longer exists or from nothing in the catalog, and the name alone does not say which
component it belonged to (components share prefixes: `demo-web-` begins
`demo-web-api-` too), so quote the name and the window rather than assign it. Then widen `since` to days and drop any text filter. Only then
weigh traffic, as supporting evidence: the request rate for a web component
(`orbit_metrics_query` over `traefik_service_requests_total` for its service) or its
span metrics. Lines from a vanished pod under the namespace form but none under the
component form is a pod-lifetime question, not a store fault. No line from this
component's current pods under either form, beside a zero request rate, reads as
idleness. The same beside traffic, or beside a restart in step 2, is either a store
that lost lines or a component that never logs, and the answer says which it could not
tell apart.

`orbit_logs_query(project, env, component, since=<window covering the restart>,
query=<optional LogQL filter such as |= "error">)`. `since` is one number and one unit
(`90m`, `3h`, `2d`), never a compound like `2h10m`, and it must reach back past the
pod's `age` to catch the predecessor. Items are `{ts, pod, line}` in time order. Read the last lines from the pod that restarted (`pod` matches step 2).
The result is capped at 200 lines: `truncated: true` with a `next_cursor` means page,
or narrow with `query` or a shorter `since`. Empty `items` over the right window is
itself evidence (the process died without logging, which fits an OOM kill).
A namespace page that one noisy pod fills to the cap (`truncated: true`) may hide other
pods' lines behind the page; the per-component form, or `query` narrowing, is the check.

## 5. Is anything already firing about it?

`orbit_alerts_active()`. Match `labels` against the namespace and component; a
matching alert's `summary` and `starts_at` go into the answer, and its `description`
says what the rule's author would look at next (a tool it names is worth calling). None matching is a
finding too (the monitors did not cover this). `backend_unconfigured`: note the gap.

## The answer

State it in this order, and nothing without its evidence:

1. **Cause**, in one sentence, or "no restart found" / "cause not determined".
2. **Evidence per step**: the fields you read and their values (restart count and
   reason, the event message, the series peak against the limit, the last log lines,
   the alert). Name each tool once. Mark any step whose tool failed and quote `what_failed`.
3. **What a human should decide**: raise the memory limit, fix the code path in the
   logs, accept the restart, or nothing. Do not act: this walk is written for a
   viewer, whose token carries no change tools; say what should change and leave it
   to a human with the instance repository to make.
