---
name: orbit-features
description: "See which optional features of the platform are on (logs, traces, ...), what each needs and costs, and how an admin switches one with orbit_features_set. Read orbit-operator first."
role: viewer
also_uses:
  - orbit_features_set
---

# The platform's features

An Orbit platform is `core` plus optional features. `core` always runs: the doors and
certificates, sealed secrets, metrics and alerts, and orbitd itself. Everything else is a
feature that is switched on or off, because each one costs memory on the node. The two
numbered steps read; the last section changes, with `orbit_features_set(feature, enabled)`,
which only an `admin` token carries:

- `logs`: every pod's logs kept in Loki for 7 days and searchable. Needs a bucket.
- `traces`: request traces in Tempo, linked from logs and metrics. Needs a bucket.
- `dashboards`: Grafana, with a dashboard per project environment. Off, metrics and
  alerts still run and are read through the tools.
- `uptime`: every public door checked from outside once a minute, and the alerts for a
  door that is down or a certificate about to expire. Off, nothing watches the doors.
- `databases`: a project may declare a PostgreSQL database. Off, a catalog entry with
  `database:` is refused.
- `backups`: a prod database archives to the bucket and can be restored to a point in
  time. Needs a bucket and `databases`. Off, a database gets a nightly dump on the node.
- `alerts-telegram`: every alert is also sent to a Telegram chat. Off, alerts are still
  raised and read with `orbit_alerts_active`; nobody is messaged.

## 1. See the state: `orbit_features_list()`

One row per feature. Read these fields:

- `declared`: the feature is on in the instance repository.
- `applied`: its Applications are on the cluster. `declared` and `applied` disagreeing is
  a change that has not finished; `pending` says which way (`enable` or `disable`).
- `ready` and `not_ready`: whether its workloads are up, and which are not.
- `requires`: each thing it needs (a bucket, another feature, a sealed secret), with
  `met` and a sentence. A feature that is on with a requirement not met is broken, and
  the sentence says what is missing.
- `tools`: the tools that exist only while the feature is on.
- `memory_mib`: what its namespaces hold in memory right now.
- `says`: the row as one sentence. Quote it when you report.

`converge_job` names the change that is running now, if one is. `problem` says why the
instance repository or the cluster could not be read.

## 2. A tool that is not there

A feature that is off takes its tools with it. They are not in `tools/list`, and a call
to one is refused with `code: FEATURE_OFF`: `evidence.feature` names the feature and
`next_action` says how it is turned on (`orbit_features_set`, by an `admin`). This is not a missing role and not a
broken platform. Say which feature the job needs and that it is off; do not look for
another tool that returns the same data, and do not invent the answer.

`orbit_logs_query` is the exception: with `logs` off it still answers, from the node
instead of from Loki. Its `note` says so. What you get then is each container's current
run and the run before its last restart, for pods that still exist; a deleted pod's
lines are gone, and `query` may only be a chain of line filters (`|=`, `!=`, `|~`,
`!~`). That is enough to read why a pod crashed. It is not a search over history.

## Switching a feature (admin): `orbit_features_set`

`orbit_features_set` needs the `admin` role. With a lower role it is not in your tool
list; report that the job needs an admin token for `orbit_features_set` and stop.

1. Call `orbit_features_set` with `feature`, `enabled` and `dry_run: true` first. `evidence.steps`
   lists what would be done: the Applications applied, retired, and re-applied with
   prune. Nothing is written.
2. Call it without `dry_run`. It asks for confirmation, naming every Application it
   will apply or retire. Answer only if that list is what the task asked for.
3. It returns `status: running` with `evidence.job_id`. Wait with
   `orbit_job_wait` (`job_id`, `timeout_s: 120`) and call it again while the job runs; a
   switch takes several minutes, because charts are rendered and workloads start.
   Switching `traces` restarts the ingress once, because its trace export is part of its
   configuration, and your own connection to orbitd goes through that ingress: the wait
   that is in flight at that moment can lose its answer. A wait that comes back as an
   error, or not at all, says nothing about the job. Call `orbit_job_status` with the same
   `job_id`, then wait again.
4. Read the result's steps: `confirm, validate, commit, checkout, converge`. `applied`
   means every step passed. `failed` names the step: a failed `commit` wrote nothing;
   a failed `converge` (`evidence.verdict: LANDED NOT CONVERGED`) means the change is
   committed and the cluster is behind it. orbitd tries the converge again by itself;
   `orbit_features_list` shows `pending` until it succeeds. Report the failing step's
   `detail` as it is.
5. Finish with `orbit_features_list` and report the row's `says`.

A switch is refused before anything is written, with `code: refused` and a sentence,
when the feature does not exist, when it is turned on without what it requires (logs,
traces and backups without `objectStorage.buckets.platform`; backups without
`databases`), or when it is turned off while something still needs it: another feature
(`databases` while `backups` is on), a project that declares a database, a probe monitor
or `monitoring.probes` entry (`uptime`), a monitor that notifies telegram
(`alerts-telegram`). The sentence names what is in the way; remove that first. `core`
cannot be switched.

Turning a feature off deletes its workloads and their volumes on the node. What it
already wrote to the bucket stays, and is readable again when the feature is turned
back on. Turning `traces` off does not restart the products' pods: a pod that started
while traces was on keeps trying to send them until its next restart.

## The answer

Say which feature, which state it was in and is in now, and quote `says`. For a switch,
give the commit and name the Applications applied and retired. If a requirement is not
met, quote its sentence.
